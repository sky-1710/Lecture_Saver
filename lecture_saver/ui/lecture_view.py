"""The main three-pane lecture workspace (§11).

Left: slide navigator - thumbnails, numbers, timestamps, click-to-seek.
Centre: video player.
Right: transcription controls and the transcript, either for the current slide
(with its picture and time range) or for the whole lecture.

This is also where video/slide/transcript synchronisation policy lives (§12),
so the player, the navigator and the transcript list stay ignorant of each
other and the rules can be changed in one place:

* Playback crossing a slide boundary selects that slide and, on the
  current-slide tab, shows its transcript - while "Follow playback" is on.
* Clicking a slide shows it and seeks to its start.
* Clicking a segment seeks to it; the slide then follows from the position.
* While a segment is being typed in, playback never moves the list away from it.

Transcript edits made here are applied to the project's single transcript store
and announced with :attr:`LectureView.transcriptEdited`, so the window can
re-link slides and schedule autosave (§13-§16).
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.alignment import Alignment, align
from ..core.project import Project
from ..core.search import SearchHit, search_lecture
from ..core.slides import SlideAppearance, SlideSet
from ..core.timecode import format_duration, format_hms
from ..core.transcript_store import TextMatch, TranscriptEditError, TranscriptStore
from ..export.document import pair_timed_translation
from ..transcription.resume import resume_point
from ..transcription.whisper_engine import WhisperOptions
from .find_bar import FindBar
from .slide_panel import SlidePanel
from .theme import Role, set_flag, set_role
from .transcribe_panel import TranscribePanel
from .transcript_view import TranscriptView
from .video_player import VideoPlayer

log = logging.getLogger(__name__)

__all__ = ["LectureView"]

TAB_SLIDE = 0
TAB_FULL = 1
TAB_SEARCH = 2
PREVIEW_HEIGHT = 150


class ElidedLabel(QLabel):
    """One line that shortens itself with "…" instead of widening the window.

    A plain QLabel never gets narrower than its text, so a long lecture or
    file name in the header set the window's minimum width - past the screen
    on a laptop (§11). The whole text stays available as the tooltip.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)

    @property
    def full_text(self) -> str:
        return self._full_text

    def setText(self, text: str) -> None:  # noqa: N802 - Qt's name
        self._full_text = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt's name
        super().resizeEvent(event)  # type: ignore[arg-type]
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(
            self._full_text, Qt.TextElideMode.ElideRight, max(self.width(), 0)
        )
        super().setText(shown)


class LectureView(QWidget):
    """Everything the user works with once a project is open."""

    #: Emitted when the user asks to start transcription.
    transcribeRequested = Signal(object)  # WhisperOptions
    #: Continue an unfinished transcription.
    resumeTranscriptionRequested = Signal(object)  # WhisperOptions
    cancelRequested = Signal()
    #: Emitted when the user asks to detect slides.
    detectSlidesRequested = Signal()
    cancelSlidesRequested = Signal()
    #: Manual slide corrections. Those that need a timestamp carry the
    #: player's current position, so the panel does not need to know about it.
    slideDeleteRequested = Signal(int)
    slideMergeRequested = Signal(int)
    slideRecaptureRequested = Signal(int, float)
    slideSplitRequested = Signal(float)
    slideMoveBoundaryRequested = Signal(int, float)
    slideSeparateRequested = Signal(int)
    #: The project's source video is missing and the user wants to relocate it.
    relocateRequested = Signal()
    #: The transcript changed. ``True`` when segments were added, removed,
    #: merged or split - i.e. the slide links may have changed too.
    transcriptEdited = Signal(bool)
    #: A short message for the status bar.
    statusMessage = Signal(str)
    #: The user wants the export screen with every format.
    exportRequested = Signal()
    #: One-click export of the notes PDF and Markdown.
    quickExportRequested = Signal()
    #: Make the study pack: preview, notes, review quiz.
    studyPackRequested = Signal()
    #: Translate the lecture here, with an AI assistant.
    translateRequested = Signal()
    #: Export the lecture with a Korean translation request on top.
    aiRequestRequested = Signal()
    #: Bring an assistant's translation back in.
    importTranslationRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: Project | None = None
        self._syncing = False
        #: Which segments belong to which slide, recomputed from the times
        #: whenever the transcript or the slides are re-bound.
        self._alignment = Alignment((), ())
        #: Segment id -> its line of an imported translation (see _rebind).
        self._line_translations: dict[str, str] = {}
        self._appearance_starts: list[float] = []
        #: Index of the appearance shown on the current-slide tab, or -1.
        self._shown = -1
        #: Global segment index -> row in the transcript list as filtered now.
        self._rows: dict[int, int] = {}
        self._preview_cache: dict[str, QPixmap] = {}
        self._matches: list[TextMatch] = []
        self._search_hits: list[SearchHit] = []
        self._match_index = -1

        self._build()
        self._build_edit_actions()
        self._connect()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        # --- header ---
        self._title_label = ElidedLabel()
        set_role(self._title_label, Role.TITLE)
        self._subtitle_label = ElidedLabel()
        set_role(self._subtitle_label, Role.MUTED)

        self._warning_bar = QFrame()
        set_flag(self._warning_bar, "banner", "warning")
        self._warning_label = QLabel()
        self._warning_label.setWordWrap(True)
        self._relocate_button = QPushButton("Locate video...")
        warning_layout = QHBoxLayout(self._warning_bar)
        warning_layout.setContentsMargins(12, 8, 12, 8)
        warning_layout.addWidget(self._warning_label, 1)
        warning_layout.addWidget(self._relocate_button)
        self._warning_bar.setVisible(False)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self._title_label)
        titles.addWidget(self._subtitle_label)
        self._quick_export_button = QPushButton("Export PDF + MD")
        set_flag(self._quick_export_button, "accent")
        self._quick_export_button.setToolTip(
            "Save the lecture notes as lecture_notes.pdf and transcript.md in the "
            "project's exports folder, with the options last used."
        )
        self._study_pack_button = QPushButton("Study pack")
        self._study_pack_button.setToolTip(
            "One PDF to study from: a simple preview to read before the lecture, "
            "the slide notes, and a 15-question review quiz with answers.\n"
            "The preview and quiz are written by the assistant from the "
            "transcript text; the video, audio and slide images are never sent."
        )
        self._translate_button = QPushButton("Translate with AI")
        self._translate_button.setToolTip(
            "Translate the lecture into Korean here, slide by slide.\n"
            "This sends the transcript text to the assistant; the video, the "
            "audio and the slide images are never sent."
        )
        # Less frequent actions share one menu. As separate buttons the header
        # needed more than the default window width and was cut off (§11).
        self._more_formats_action = QAction("More export formats...", self)
        self._more_formats_action.setToolTip(
            "Choose formats and options: PDF, TXT, Markdown, SRT, VTT, JSON."
        )
        self._ai_request_menu_action = QAction("AI translation request (by hand)...", self)
        self._ai_request_menu_action.setToolTip(
            "Save the whole lecture as PDF and Markdown with a Korean translation "
            "request at the top, ready to give to an AI assistant yourself. "
            "Nothing is sent anywhere."
        )
        self._import_translation_menu_action = QAction("Import translation...", self)
        self._import_translation_menu_action.setToolTip(
            "Paste or load an AI assistant's answer; each slide's translation "
            "is shown under its transcript and included in exports."
        )
        more_menu = QMenu(self)
        more_menu.setToolTipsVisible(True)
        more_menu.addAction(self._more_formats_action)
        more_menu.addSeparator()
        more_menu.addAction(self._ai_request_menu_action)
        more_menu.addAction(self._import_translation_menu_action)
        self._more_button = QPushButton("More")
        self._more_button.setMenu(more_menu)
        self._more_button.setToolTip("Other export formats and the by-hand translation")
        header = QHBoxLayout()
        header.setContentsMargins(16, 12, 16, 12)
        header.setSpacing(8)
        header.addLayout(titles, 1)
        header.addWidget(self._quick_export_button)
        header.addWidget(self._study_pack_button)
        header.addWidget(self._translate_button)
        header.addWidget(self._more_button)
        header_bar = QFrame()
        set_flag(header_bar, "rule", "bottom")
        set_flag(header_bar, "surface")
        header_bar.setLayout(header)

        # --- left: slide navigator ---
        self._slides_pane = SlidePanel()
        self._slides_pane.setMinimumWidth(210)

        # --- centre: video ---
        self._player = VideoPlayer()

        # It governs the slide list and the transcript alike, and the centre has
        # the width to spare that the transcript pane does not.
        self._follow_check = QCheckBox("Follow playback")
        self._follow_check.setChecked(True)
        self._follow_check.setToolTip(
            "Keep the selected slide and the transcript in step with the video "
            "as it plays."
        )
        follow_bar = QHBoxLayout()
        follow_bar.setContentsMargins(0, 0, 0, 0)
        follow_bar.addStretch(1)
        follow_bar.addWidget(self._follow_check)

        centre = QWidget()
        centre_layout = QVBoxLayout(centre)
        centre_layout.setContentsMargins(0, 0, 0, 0)
        centre_layout.setSpacing(6)
        centre_layout.addWidget(self._player, 1)
        centre_layout.addLayout(follow_bar)

        # --- right: transcription + transcript ---
        self._panel = TranscribePanel()

        self._transcript_header = QLabel("Transcript")
        set_role(self._transcript_header, Role.SECTION)

        self._tabs = QTabBar()
        self._tabs.addTab("Current slide")
        self._tabs.addTab("Full transcript")
        self._tabs.addTab("Search")
        self._tabs.setDrawBase(False)
        self._tabs.setExpanding(False)
        # Two short tabs: scroll arrows would hide one of them in a narrow pane.
        self._tabs.setUsesScrollButtons(False)
        self._tabs.setVisible(False)

        transcript_bar = QHBoxLayout()
        transcript_bar.setContentsMargins(14, 10, 14, 2)
        transcript_bar.setSpacing(10)
        transcript_bar.addWidget(self._transcript_header)
        transcript_bar.addWidget(self._tabs)
        transcript_bar.addStretch(1)

        # Editing controls. The buttons are bound to the actions built in
        # _build_edit_actions, which also carry the keyboard shortcuts.
        self._undo_button = QToolButton()
        self._redo_button = QToolButton()
        self._find_button = QToolButton()
        self._reset_button = QToolButton()
        self._save_label = QLabel("")
        set_role(self._save_label, Role.SUBTLE)
        self._edit_hint = QLabel("Double-click to edit")
        set_role(self._edit_hint, Role.SUBTLE)
        self._edit_bar = QWidget()
        edit_layout = QHBoxLayout(self._edit_bar)
        edit_layout.setContentsMargins(10, 2, 12, 6)
        edit_layout.setSpacing(2)
        edit_layout.addWidget(self._undo_button)
        edit_layout.addWidget(self._redo_button)
        edit_layout.addWidget(self._find_button)
        edit_layout.addWidget(self._reset_button)
        edit_layout.addSpacing(8)
        edit_layout.addWidget(self._edit_hint)
        edit_layout.addStretch(1)
        edit_layout.addWidget(self._save_label)
        self._edit_bar.setVisible(False)
        # The hint is the first thing to go when the pane gets narrow: the
        # buttons beside it are the part that has to stay reachable.
        self._edit_bar.installEventFilter(self)

        self._find_bar = FindBar()
        self._find_bar.setVisible(False)

        # The slide the transcript below belongs to: picture, number, range.
        self._slide_header = QFrame()
        self._slide_preview = QLabel()
        self._slide_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._slide_preview.setMinimumHeight(60)
        self._slide_preview.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        self._slide_title = QLabel()
        set_role(self._slide_title, Role.SECTION)
        self._slide_detail = QLabel()
        set_role(self._slide_detail, Role.MUTED)
        set_flag(self._slide_header, "rule", "bottom")
        slide_header_layout = QVBoxLayout(self._slide_header)
        slide_header_layout.setContentsMargins(14, 6, 14, 10)
        slide_header_layout.setSpacing(3)
        slide_header_layout.addWidget(self._slide_preview)
        slide_header_layout.addWidget(self._slide_title)
        slide_header_layout.addWidget(self._slide_detail)
        self._slide_header.setVisible(False)

        self._transcript = TranscriptView()

        # The imported translation of the slide shown, under its transcript.
        self._translation_panel = QFrame()
        set_flag(self._translation_panel, "rule", "top")
        translation_title = QLabel("한국어 직역")
        set_role(translation_title, Role.SECTION)
        self._translation_text = QTextBrowser()
        self._translation_text.setOpenLinks(False)
        self._translation_text.setMaximumHeight(170)
        set_role(self._translation_text, Role.TRANSLATION)
        translation_layout = QVBoxLayout(self._translation_panel)
        translation_layout.setContentsMargins(14, 8, 14, 8)
        translation_layout.setSpacing(3)
        translation_layout.addWidget(translation_title)
        translation_layout.addWidget(self._translation_text)
        self._translation_panel.setVisible(False)

        # Lecture search (§18): results with slide number and time.
        self._search_panel = QWidget()
        self._search_field = QLineEdit()
        self._search_field.setPlaceholderText(
            "Search the lecture - words in any order, spacing ignored"
        )
        self._search_field.setClearButtonEnabled(True)
        self._search_count = QLabel("")
        set_role(self._search_count, Role.MUTED)
        self._search_results = QTextBrowser()
        self._search_results.setOpenLinks(False)
        search_layout = QVBoxLayout(self._search_panel)
        search_layout.setContentsMargins(14, 8, 10, 8)
        search_layout.setSpacing(6)
        search_layout.addWidget(self._search_field)
        search_layout.addWidget(self._search_count)
        search_layout.addWidget(self._search_results, 1)
        self._search_panel.setVisible(False)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)

        self._empty_label = QLabel(
            "No transcript yet.\n\nChoose a model above and select "
            "“Transcribe lecture”. Everything runs on this computer."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setMargin(18)
        set_role(self._empty_label, Role.SUBTLE)

        # The transcript and everything that acts on it live in one card, so
        # the pane reads as a single surface rather than a stack of strips.
        transcript_card = QFrame()
        set_flag(transcript_card, "card")
        card_layout = QVBoxLayout(transcript_card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        card_layout.addLayout(transcript_bar)
        card_layout.addWidget(self._edit_bar)
        card_layout.addWidget(self._find_bar)
        card_layout.addWidget(self._slide_header)
        card_layout.addWidget(self._empty_label, 1)
        card_layout.addWidget(self._transcript, 1)
        card_layout.addWidget(self._translation_panel)
        card_layout.addWidget(self._search_panel, 1)

        right = QWidget()
        right.setMinimumWidth(300)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)
        right_layout.addWidget(self._panel)
        right_layout.addWidget(transcript_card, 1)
        self._transcript.setVisible(False)

        # --- assembly ---
        self._splitter = QSplitter(Qt.Orientation.Horizontal)
        self._splitter.addWidget(self._slides_pane)
        self._splitter.addWidget(centre)
        self._splitter.addWidget(right)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 3)
        self._splitter.setStretchFactor(2, 2)
        self._splitter.setSizes([240, 590, 410])
        # Keeps the window usable when it is made small (§11).
        self._splitter.setChildrenCollapsible(True)
        self._splitter.setHandleWidth(10)
        self._splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # The panes are cards on the window colour: the gap between them is the
        # splitter handle, and these margins are the gap around the outside.
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 12, 12, 12)
        body_layout.setSpacing(10)
        body_layout.addWidget(self._warning_bar)
        body_layout.addWidget(self._splitter, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header_bar)
        layout.addWidget(body, 1)

    def _connect(self) -> None:
        self._player.positionChanged.connect(self._on_position)
        self._player.errorOccurred.connect(self._on_player_error)
        self._transcript.seekRequested.connect(self._on_seek_requested)
        self._follow_check.toggled.connect(self._transcript.set_follow_playback)
        self._panel.startRequested.connect(self.transcribeRequested)
        self._panel.resumeRequested.connect(self.resumeTranscriptionRequested)
        self._panel.cancelRequested.connect(self.cancelRequested)
        self._relocate_button.clicked.connect(self.relocateRequested)
        self._quick_export_button.clicked.connect(self.quickExportRequested)
        self._study_pack_button.clicked.connect(self.studyPackRequested)
        self._translate_button.clicked.connect(self.translateRequested)
        self._more_formats_action.triggered.connect(self.exportRequested)
        self._ai_request_menu_action.triggered.connect(self.aiRequestRequested)
        self._import_translation_menu_action.triggered.connect(self.importTranslationRequested)

        self._transcript.textEdited.connect(self._on_text_edited)
        self._transcript.editingFinished.connect(self._on_editing_finished)
        self._transcript.splitRequested.connect(self._on_split_segment)
        self._transcript.restoreRequested.connect(
            lambda sid: self._edit(lambda st: st.restore_original(sid), sid, "Original restored")
        )
        self._transcript.mergeRequested.connect(
            lambda sid: self._edit(lambda st: st.merge_with_next(sid), sid, "Segments merged",
                                   structural=True)
        )
        self._transcript.insertRequested.connect(self._on_insert_segment)
        self._transcript.deleteRequested.connect(self._on_delete_segment)
        self._find_bar.searchChanged.connect(self._refresh_matches)
        self._find_bar.nextRequested.connect(lambda: self._step_match(1))
        self._find_bar.previousRequested.connect(lambda: self._step_match(-1))
        self._find_bar.replaceRequested.connect(self._replace_current)
        self._find_bar.replaceAllRequested.connect(self._replace_all)
        self._find_bar.closed.connect(self._on_find_closed)

        self._search_field.textChanged.connect(self._search_timer.start)
        self._search_field.returnPressed.connect(self._run_search)
        self._search_timer.timeout.connect(self._run_search)
        self._search_results.anchorClicked.connect(self._on_search_link)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._slides_pane.slideActivated.connect(self._on_slide_activated)
        self._slides_pane.seekRequested.connect(self._on_seek_requested)
        self._slides_pane.detectRequested.connect(self.detectSlidesRequested)
        self._slides_pane.cancelRequested.connect(self.cancelSlidesRequested)
        self._slides_pane.deleteRequested.connect(self.slideDeleteRequested)
        self._slides_pane.mergeRequested.connect(self.slideMergeRequested)
        self._slides_pane.recaptureRequested.connect(self._on_recapture)
        self._slides_pane.splitHereRequested.connect(self._on_split_here)
        self._slides_pane.moveBoundaryRequested.connect(self._on_move_boundary)
        self._slides_pane.separateRequested.connect(self.slideSeparateRequested)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if watched is self._edit_bar and event.type() == QEvent.Type.Resize:
            self._edit_hint.setVisible(self._edit_bar.width() >= 430)
        return super().eventFilter(watched, event)

    # -- project binding ----------------------------------------------------

    def load_project(self, project: Project) -> None:
        self._project = project
        video = project.video

        self._title_label.setText(project.name)
        details = [video.display_name]
        if video.duration:
            details.append(format_duration(video.duration))
        if video.width and video.height:
            details.append(f"{video.width}x{video.height}")
        if video.video_codec:
            details.append(video.video_codec)
        self._subtitle_label.setText("  •  ".join(details))

        self._panel.set_video_duration(video.duration)
        self._panel.set_defaults(
            {
                "model_size": project.meta.transcription.model_size,
                "language": project.meta.transcription.language,
                "vad_filter": project.meta.transcription.vad_filter,
            }
        )

        if project.video_missing:
            self._warning_label.setText(
                f"The source video is missing:\n{video.path}\n"
                "Playback, transcription and slide detection are unavailable "
                "until it is found."
            )
            self._warning_bar.setVisible(True)
            self._player.clear()
            self._panel.set_enabled_for_video(False, "The source video is missing.")
            self._slides_pane.set_enabled_for_video(False, "The source video is missing.")
        elif not video.has_audio:
            self._warning_bar.setVisible(False)
            self._player.load(video.as_path)
            self._panel.set_enabled_for_video(
                False, "This video has no audio track, so it cannot be transcribed."
            )
            # Slide detection only needs the picture, so it stays available.
            self._slides_pane.set_enabled_for_video(True)
        else:
            self._warning_bar.setVisible(False)
            self._player.load(video.as_path)
            self._panel.set_enabled_for_video(True)
            self._slides_pane.set_enabled_for_video(True)

        self.refresh_transcript()
        self.refresh_slides()

    def refresh_transcript(self) -> None:
        """Re-bind the view to the project's transcript - the one source (§14)."""
        if self._project is None:
            return
        self._panel.set_already_transcribed(len(self._project.transcript))
        point = resume_point(self._project)
        self._panel.set_resume_point(point.start if point is not None else None)
        self._rebind()

    def refresh_slides(self) -> None:
        """Re-bind the navigator to the project's detected slides."""
        if self._project is None:
            return
        self._slides_pane.set_slides(self._project.slides, self._project.root)
        self._preview_cache.clear()
        self._rebind()

    def close_project(self) -> None:
        self._project = None
        self._player.clear()
        self._transcript.clear_segments()
        self._line_translations = {}
        self._transcript.set_translations({})
        self._slides_pane.set_slides(SlideSet(), Path())
        self._alignment = Alignment((), ())
        self._appearance_starts = []
        self._shown = -1
        self._rows = {}
        self._preview_cache.clear()
        self._tabs.setVisible(False)
        self._slide_header.setVisible(False)
        self._translation_panel.setVisible(False)
        self._edit_bar.setVisible(False)
        self._find_bar.setVisible(False)
        self._matches, self._match_index = [], -1
        self._save_label.setText("")
        self._search_hits = []
        self._search_field.clear()
        self._search_results.clear()
        self._search_panel.setVisible(False)
        self._update_edit_actions()

    # -- transcript / slide binding ----------------------------------------

    @property
    def showing_current_slide(self) -> bool:
        """True when the transcript is filtered to one slide."""
        return (
            not self._tabs.isHidden()
            and self._tabs.currentIndex() == TAB_SLIDE
            and bool(self._appearance_starts)
        )

    @property
    def showing_search(self) -> bool:
        return not self._tabs.isHidden() and self._tabs.currentIndex() == TAB_SEARCH

    @property
    def shown_appearance(self) -> SlideAppearance | None:
        if self._project is None:
            return None
        appearances = self._project.slides.appearances
        return appearances[self._shown] if 0 <= self._shown < len(appearances) else None

    def _rebind(self, keep_segment: str | None = None) -> None:
        """Recompute the alignment and show whichever transcript is selected.

        ``keep_segment`` is a segment the user just worked on: the view stays on
        its slide and selects it, instead of jumping back to playback.
        """
        assert self._project is not None
        segments = self._project.transcript.segments
        appearances = self._project.slides.appearances
        self._alignment = align(segments, appearances)
        self._appearance_starts = [a.start for a in appearances]
        self._line_translations = self._pair_line_translations()

        has_slides = bool(appearances)
        self._tabs.blockSignals(True)
        if segments and self._tabs.isHidden():
            # The slide-linked view is the point of having both; start there.
            self._tabs.setCurrentIndex(TAB_SLIDE if has_slides else TAB_FULL)
        self._tabs.setTabEnabled(TAB_SLIDE, has_slides)
        if not has_slides and self._tabs.currentIndex() == TAB_SLIDE:
            self._tabs.setCurrentIndex(TAB_FULL)
        self._tabs.blockSignals(False)
        self._tabs.setVisible(bool(segments))

        kept = self._project.transcript.index_of(keep_segment) if keep_segment else None
        kept_slide = self._alignment.appearance_for(kept) if kept is not None else None
        if not appearances:
            self._shown = -1
        elif kept_slide is not None:
            self._shown = kept_slide
        elif self._follow_check.isChecked() or self._shown < 0:
            self._shown = self._appearance_index_at(self._player.position)
        else:
            self._shown = min(self._shown, len(appearances) - 1)
        self._show_transcript()
        self._edit_bar.setVisible(bool(segments))
        if keep_segment:
            row = self._transcript.row_of(keep_segment)
            if row >= 0:
                self._transcript.select_row(row)
        self._update_edit_actions()

    def _show_transcript(self, seconds: float | None = None) -> None:
        """Fill the transcript list for the current tab and slide.

        ``seconds`` is the playback position to highlight; the player's own
        position when omitted.
        """
        assert self._project is not None
        segments = self._project.transcript.segments

        searching = self.showing_search
        self._search_panel.setVisible(searching)
        if searching:
            for widget in (self._transcript, self._empty_label, self._slide_header,
                           self._translation_panel, self._transcript_header):
                widget.setVisible(False)
            self._run_search()
            return

        self._transcript.set_translations(self._line_translations)
        if self.showing_current_slide and self._shown >= 0:
            indices = self._alignment.segments_for(self._shown)
            self._transcript.set_segments(tuple(segments[i] for i in indices))
            self._rows = {segment: row for row, segment in enumerate(indices)}
            self._update_slide_header(len(indices))
            self._slides_pane.select_row(self._shown)
            self._update_translation()
        else:
            self._transcript.set_segments(segments)
            self._rows = {i: i for i in range(len(segments))}
            self._slide_header.setVisible(False)
            self._translation_panel.setVisible(False)

        shown = self._transcript.segment_count
        self._transcript.setVisible(shown > 0)
        self._empty_label.setVisible(shown == 0)
        if not segments:
            self._transcript_header.setVisible(True)
            self._transcript_header.setText("Transcript")
            self._empty_label.setText(
                "No transcript yet.\n\nChoose a model above and select "
                "“Transcribe lecture”. Everything runs on this computer."
            )
        else:
            # With the tabs showing, they name the view; the count is in the
            # slide header or here.
            self._transcript_header.setVisible(self._tabs.isHidden())
            self._transcript_header.setText(f"Transcript  ({len(segments)} segments)")
            self._empty_label.setText("Nothing was said while this slide was on screen.")

        self._highlight(self._player.position if seconds is None else seconds)

    def _update_slide_header(self, count: int) -> None:
        appearance = self.shown_appearance
        if appearance is None or self._project is None:
            self._slide_header.setVisible(False)
            return

        label = self._project.slides.label_for(appearance)
        self._slide_title.setText(
            f"Slide {appearance.appearance_id}" + (f"  ·  {label}" if label else "")
        )
        spoken = f"{count} segment{'s' if count != 1 else ''}"
        self._slide_detail.setText(
            f"{format_hms(appearance.start)} – {format_hms(appearance.end)}  ·  {spoken}"
        )

        slide = self._project.slides.slide_for(appearance)
        pixmap = self._preview(slide.image if slide else "")
        if pixmap is None:
            self._slide_preview.setPixmap(QPixmap())
            self._slide_preview.setText("No image for this slide.")
        else:
            self._slide_preview.setText("")
            self._slide_preview.setPixmap(pixmap)
        self._slide_header.setVisible(True)

    def _pair_line_translations(self) -> dict[str, str]:
        """Segment id -> Korean, for every slide translated line by line."""
        if self._project is None:
            return {}
        segments = self._project.transcript.segments
        pairs: dict[str, str] = {}
        for index, appearance in enumerate(self._project.slides.appearances):
            text = self._project.translations.for_appearance(appearance)
            if text:
                own = tuple(segments[i] for i in self._alignment.segments_for(index))
                pairs.update(pair_timed_translation(own, text))
        return pairs

    def _update_translation(self) -> None:
        """The slide's translation in the panel - unless it was made line by
        line, in which case each line already shows under its own segment."""
        appearance = self.shown_appearance
        text = (
            self._project.translations.for_appearance(appearance)
            if self._project is not None and appearance is not None
            else None
        )
        if text and self._shown >= 0:
            own = self._alignment.segments_for(self._shown)
            segments = self._project.transcript.segments if self._project else []
            if any(segments[i].id in self._line_translations for i in own):
                text = None
        self._translation_text.setPlainText(text or "")
        self._translation_panel.setVisible(bool(text))

    @property
    def shown_translation(self) -> str:
        if self._translation_panel.isHidden():
            return ""
        return self._translation_text.toPlainText()

    def refresh_translations(self) -> None:
        if self._project is not None:
            self._line_translations = self._pair_line_translations()
            self._show_transcript()

    def _preview(self, image: str) -> QPixmap | None:
        if not image or self._project is None:
            return None
        cached = self._preview_cache.get(image)
        if cached is not None:
            return cached
        source = QPixmap(str(self._project.root / image))
        if source.isNull():
            return None
        scaled = source.scaledToHeight(
            PREVIEW_HEIGHT, Qt.TransformationMode.SmoothTransformation
        )
        self._preview_cache[image] = scaled
        return scaled

    def _appearance_index_at(self, seconds: float) -> int:
        """Appearance on screen at ``seconds``; the first before it starts."""
        if not self._appearance_starts:
            return -1
        return max(0, bisect.bisect_right(self._appearance_starts, seconds) - 1)

    def _show_appearance(self, index: int, seconds: float | None = None) -> None:
        if index == self._shown or self._project is None:
            return
        self._shown = index
        if self.showing_current_slide:
            self._show_transcript(seconds)
        else:
            self._slides_pane.select_row(index)

    # -- transcript editing (§13) -------------------------------------------

    @property
    def edit_actions(self) -> list[QAction]:
        """Undo, redo, find & replace, search, tidy and reset, for the Edit menu."""
        return [
            self._undo_action, self._redo_action, self._find_action,
            self._search_action, self._split_action, self._reset_action,
        ]

    def _build_edit_actions(self) -> None:
        self._undo_action = QAction("Undo", self)
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self._undo_action.triggered.connect(self.undo)
        self._redo_action = QAction("Redo", self)
        self._redo_action.setShortcuts(
            [QKeySequence(QKeySequence.StandardKey.Redo), QKeySequence("Ctrl+Shift+Z")]
        )
        self._redo_action.triggered.connect(self.redo)
        self._find_action = QAction("Find && replace...", self)
        self._find_action.setShortcuts(
            [
                QKeySequence(QKeySequence.StandardKey.Find),
                QKeySequence(QKeySequence.StandardKey.Replace),
            ]
        )
        self._find_action.triggered.connect(self.open_find)
        self._search_action = QAction("Search lecture", self)
        self._search_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self._search_action.triggered.connect(self.open_search)
        self._split_action = QAction("Split lines at slide changes", self)
        self._split_action.setToolTip(
            "Where a slide changes in the middle of a sentence, cut the line at "
            "the nearest full stop, so each slide keeps whole sentences.\n"
            "Undo with Ctrl+Z."
        )
        self._split_action.triggered.connect(self.split_at_slide_changes)
        self._reset_action = QAction("Reset to original transcription...", self)
        self._reset_action.setToolTip(
            "Discard every correction and go back to the transcript exactly as it "
            "was transcribed. Undo brings your corrections back."
        )
        self._reset_action.triggered.connect(self.reset_to_original)

        self._undo_button.setDefaultAction(self._undo_action)
        self._redo_button.setDefaultAction(self._redo_action)
        self._find_button.setDefaultAction(self._find_action)
        # Buttons show the short icon text; menus keep the full action name.
        self._find_action.setIconText("Find")
        self._reset_button.setDefaultAction(self._reset_action)
        self._reset_action.setIconText("Reset")
        self._update_edit_actions()

    def _store(self) -> TranscriptStore | None:
        return self._project.transcript if self._project is not None else None

    def _update_edit_actions(self) -> None:
        store = self._store()
        can_undo = bool(store and store.can_undo)
        can_redo = bool(store and store.can_redo)
        self._undo_action.setEnabled(can_undo)
        self._redo_action.setEnabled(can_redo)
        self._undo_action.setToolTip(
            f"Undo {store.undo_label.lower()}" if can_undo and store and store.undo_label
            else "Nothing to undo"
        )
        self._redo_action.setToolTip(
            f"Redo {store.redo_label.lower()}" if can_redo and store and store.redo_label
            else "Nothing to redo"
        )
        self._find_action.setEnabled(bool(store and not store.is_empty))
        self._reset_action.setEnabled(bool(store and not store.is_empty))
        self._search_action.setEnabled(bool(store and not store.is_empty))
        self._split_action.setEnabled(
            bool(store and not store.is_empty and self._appearance_starts)
        )

    def set_save_status(self, text: str, *, error: bool = False) -> None:
        self._save_label.setText(text)
        set_role(self._save_label, Role.DANGER if error else Role.SUBTLE)

    def _edit(
        self,
        action: Callable[[TranscriptStore], object],
        keep_segment: str | None,
        success: str,
        *,
        structural: bool = False,
    ) -> object:
        """Apply one edit command to the store and bring every view up to date."""
        store = self._store()
        if store is None:
            return None
        self._transcript.finish_editing()
        try:
            result = action(store)
        except TranscriptEditError as exc:
            QMessageBox.warning(self, "Cannot change the transcript", str(exc))
            return None
        if result is False:
            return result
        self._rebind(keep_segment)
        self._refresh_matches(keep_position=True)
        self.transcriptEdited.emit(structural)
        self.statusMessage.emit(success)
        return result

    def _on_text_edited(self, segment_id: str, text: str) -> None:
        """Live typing: applied at once, grouped into one undo step per segment."""
        store = self._store()
        if store is None:
            return
        try:
            changed = store.edit_text(segment_id, text, coalesce=True)
        except TranscriptEditError:
            log.warning("Typing into a segment that no longer exists", exc_info=True)
            return
        if changed:
            self._update_edit_actions()
            self.transcriptEdited.emit(False)

    def _on_editing_finished(self, segment_id: str) -> None:
        store = self._store()
        if store is None:
            return
        store.end_typing()
        self._update_edit_actions()
        self._refresh_matches(keep_position=True)

    def _on_split_segment(self, segment_id: str, offset: int) -> None:
        result = self._edit(
            lambda st: st.split_segment(segment_id, offset), None,
            "Segment split", structural=True,
        )
        if isinstance(result, tuple):
            self._rebind(result[1].id)

    def _on_insert_segment(self, segment_id: str) -> None:
        result = self._edit(
            lambda st: st.insert_segment_after(segment_id), None,
            "Segment added - type what was said", structural=True,
        )
        if result is not None and hasattr(result, "id"):
            self._rebind(result.id)
            self._transcript.edit_segment(result.id)

    def _on_delete_segment(self, segment_id: str) -> None:
        store = self._store()
        index = store.index_of(segment_id) if store else None
        following = None
        if store is not None and index is not None:
            segments = store.segments
            neighbour = index + 1 if index + 1 < len(segments) else index - 1
            if 0 <= neighbour < len(segments):
                following = segments[neighbour].id
        self._edit(
            lambda st: st.delete_segment(segment_id), following,
            "Segment deleted - Undo brings it back", structural=True,
        )

    def undo(self) -> None:
        self._step_history(undo=True)

    def redo(self) -> None:
        self._step_history(undo=False)

    def _step_history(self, *, undo: bool) -> None:
        store = self._store()
        if store is None:
            return
        self._transcript.finish_editing()
        selected = self._transcript.currentIndex()
        keep = None
        if selected.isValid():
            segment = self._transcript.model().data(selected, Qt.ItemDataRole.UserRole + 2)
            keep = getattr(segment, "id", None)
        label = store.undo() if undo else store.redo()
        if label is None:
            return
        self._rebind(keep)
        self._refresh_matches(keep_position=True)
        self.transcriptEdited.emit(True)
        self.statusMessage.emit(f"{'Undid' if undo else 'Redid'}: {label.lower()}")

    def split_at_slide_changes(self, *, quiet: bool = False) -> int:
        """Cut lines a slide change runs through at the nearest sentence end.

        One undoable step. ``quiet`` reports nothing when there was nothing to
        do, for the automatic pass after slide detection.
        """
        project = self._project
        if project is None or project.transcript.is_empty or not self._appearance_starts:
            return 0
        starts = list(self._appearance_starts)
        done = self._edit(
            lambda store: store.split_at_slide_changes(starts),
            None,
            "",
            structural=True,
        )
        count = done if isinstance(done, int) else 0
        if count:
            self.statusMessage.emit(
                f"{count} line{'s' if count != 1 else ''} split at slide changes"
            )
        elif not quiet:
            self.statusMessage.emit("Every slide change already falls between sentences.")
        return count

    def reset_to_original(self) -> None:
        """Go back to the transcription as Whisper produced it, after asking."""
        project = self._project
        if project is None or project.transcript.is_empty:
            return
        self._transcript.finish_editing()
        project.transcript.end_typing()
        if project.is_original_transcript():
            self.statusMessage.emit("The transcript is already the original transcription.")
            return

        if project.has_original_transcript:
            detail = (
                "Every correction will be discarded and the transcript restored "
                "exactly as it was transcribed - including lines you merged, "
                "split, added or deleted."
            )
        else:
            detail = (
                "Every text correction will be discarded.\n\nThis lecture was "
                "transcribed before the original transcription was kept, so lines "
                "you merged, split, added or deleted stay as they are. Transcribing "
                "again keeps the original for a full reset."
            )
        answer = QMessageBox.question(
            self,
            "Reset to the original transcription?",
            f"{detail}\n\nYou can undo this with Ctrl+Z.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._edit(
            lambda _store: project.reset_transcript(), None,
            "Transcript reset to the original transcription - Undo brings your "
            "corrections back",
            structural=True,
        )

    # -- lecture search (§18) -------------------------------------------------

    def open_search(self, query: str | None = None) -> None:
        store = self._store()
        if store is None or store.is_empty:
            return
        self._transcript.finish_editing()
        if query is not None:
            self._search_field.setText(query)
        self._tabs.setCurrentIndex(TAB_SEARCH)
        self._search_field.setFocus()
        self._search_field.selectAll()
        self._run_search()

    @property
    def search_hits(self) -> list[SearchHit]:
        return list(self._search_hits)

    def _run_search(self) -> None:
        self._search_timer.stop()
        if self._project is None:
            return
        query = self._search_field.text()
        self._search_hits = search_lecture(
            query, self._project.transcript.segments,
            self._project.slides.appearances, self._project.translations,
        )
        if not query.strip():
            self._search_count.setText("")
        elif not self._search_hits:
            self._search_count.setText("No results")
        else:
            count = len(self._search_hits)
            self._search_count.setText(f"{count} result{'s' if count != 1 else ''}")
        self._search_results.setHtml(
            "".join(self._hit_html(i, hit) for i, hit in enumerate(self._search_hits))
        )

    @staticmethod
    def _hit_html(index: int, hit: SearchHit) -> str:
        where = []
        if hit.slide_number is not None:
            where.append(f"Slide {hit.slide_number}")
        where.append(format_hms(hit.start))
        tag = ""
        if hit.kind == "translation":
            tag = ' <span style="color:#1E3A8A;">· 한국어 직역</span>'
        elif hit.in_original_only:
            tag = ' <span style="color:#B26B00;">· original transcription</span>'
        parts, cursor = [], 0
        for start, end in hit.spans:
            if start < cursor:
                continue
            parts.append(html_escape(hit.excerpt[cursor:start]))
            parts.append(
                '<b style="background-color:#FFF3B0;">'
                + html_escape(hit.excerpt[start:end]) + '</b>'
            )
            cursor = end
        parts.append(html_escape(hit.excerpt[cursor:]))
        return (
            f'<p style="margin:0 0 10px 0;"><a href="hit:{index}" '
            'style="text-decoration:none; color:#1F2937;">'
            f'<span style="color:#6B7280;">{" · ".join(where)}</span>{tag}<br/>'
            f'{"".join(parts)}</a></p>'
        )

    def _on_search_link(self, url: QUrl) -> None:
        text = url.toString()
        if text.startswith("hit:"):
            self.open_search_hit(int(text[4:]))

    def open_search_hit(self, index: int) -> None:
        """Go to a result: its slide, the video at its time, its line selected."""
        if self._project is None or not 0 <= index < len(self._search_hits):
            return
        hit = self._search_hits[index]
        target = hit.start
        if hit.appearance_index is not None:
            appearance = self._project.slides.appearances[hit.appearance_index]
            # A sentence begun just before its slide appeared still belongs to
            # that slide; seek inside the slide so playback does not switch away.
            target = max(hit.start, appearance.start)
            self._shown = hit.appearance_index
            self._tabs.setCurrentIndex(TAB_SLIDE)
        else:
            self._tabs.setCurrentIndex(TAB_FULL)
        self._show_transcript(target)
        self._on_seek_requested(target)
        if hit.segment_id is not None:
            row = self._transcript.row_of(hit.segment_id)
            if row >= 0:
                self._transcript.select_row(row)

    # -- find & replace -------------------------------------------------------

    def open_find(self) -> None:
        store = self._store()
        if store is None or store.is_empty:
            return
        self._transcript.finish_editing()
        self._find_bar.open_bar()
        self._refresh_matches()

    def _on_find_closed(self) -> None:
        self._matches, self._match_index = [], -1
        self._transcript.setFocus()

    def _refresh_matches(self, *, keep_position: bool = False) -> None:
        store = self._store()
        if store is None or not self._find_bar.isVisibleTo(self) and not self._find_bar.query:
            self._matches, self._match_index = [], -1
            return
        previous = self._match_index if keep_position else -1
        self._matches = store.find(
            self._find_bar.query, case_sensitive=self._find_bar.case_sensitive
        )
        self._match_index = min(previous, len(self._matches) - 1) if previous >= 0 else -1
        self._find_bar.set_result(len(self._matches), self._match_index)

    def _step_match(self, direction: int) -> None:
        if not self._matches:
            self._refresh_matches()
        if not self._matches:
            return
        if self._match_index < 0:
            self._match_index = 0 if direction > 0 else len(self._matches) - 1
        else:
            self._match_index = (self._match_index + direction) % len(self._matches)
        self._find_bar.set_result(len(self._matches), self._match_index)
        self._reveal_match(self._matches[self._match_index])

    def _reveal_match(self, match: TextMatch) -> None:
        """Show the segment holding a match, switching slide if needed."""
        slide = self._alignment.appearance_for(match.segment_index)
        if self.showing_current_slide and slide is not None and slide != self._shown:
            self._shown = slide
            self._show_transcript()
        row = self._transcript.row_of(match.segment_id)
        if row >= 0:
            self._transcript.select_row(row)

    def _replace_current(self) -> None:
        if not self._matches:
            return
        if self._match_index < 0:
            self._step_match(1)
            return
        match = self._matches[self._match_index]
        query, replacement = self._find_bar.query, self._find_bar.replacement
        case = self._find_bar.case_sensitive
        replaced = self._edit(
            lambda st: st.replace_match(match, query, replacement, case_sensitive=case),
            match.segment_id, "Replaced",
        )
        if replaced:
            # Step to the next occurrence after the replaced one.
            self._match_index -= 1
            if self._matches:
                self._step_match(1)

    def _replace_all(self) -> None:
        query, replacement = self._find_bar.query, self._find_bar.replacement
        case = self._find_bar.case_sensitive
        store = self._store()
        if store is None or not query:
            return
        count = len(store.find(query, case_sensitive=case))
        if count == 0:
            return
        self._edit(
            lambda st: st.replace_everywhere(query, replacement, case_sensitive=case),
            None, f"Replaced {count} occurrence{'s' if count != 1 else ''}",
        )

    # -- transcription run state -------------------------------------------

    @property
    def panel(self) -> TranscribePanel:
        return self._panel

    @property
    def slide_panel(self) -> SlidePanel:
        return self._slides_pane

    def begin_transcription(self) -> None:
        self._panel.begin_run()

    def end_transcription(self, message: str) -> None:
        self._panel.end_run(message)

    def update_progress(self, stage: str, fraction: float, message: str) -> None:
        self._panel.update_progress(stage, fraction, message)

    def options(self) -> WhisperOptions:
        return self._panel.options()

    # -- slide detection run state -----------------------------------------

    def begin_slide_detection(self) -> None:
        self._slides_pane.begin_run()

    def end_slide_detection(self, message: str) -> None:
        self._slides_pane.end_run(message)

    def update_slide_progress(self, stage: str, fraction: float, message: str) -> None:
        self._slides_pane.update_progress(stage, fraction, message)

    def pause_playback(self) -> None:
        self._player.pause()

    def finish_editing(self) -> None:
        """Close an open line editor and its typing session."""
        self._transcript.finish_editing()
        store = self._store()
        if store is not None:
            store.end_typing()

    def set_export_enabled(self, enabled: bool) -> None:
        for button in (
            self._quick_export_button, self._study_pack_button,
            self._translate_button, self._more_button,
        ):
            button.setEnabled(enabled)

    # -- synchronisation (§12) ---------------------------------------------

    def _on_position(self, seconds: float) -> None:
        """Follow playback: select the slide on screen, highlight the segment."""
        if self._project is None or self._syncing:
            return

        editing = self._transcript.is_editing
        if self._follow_check.isChecked() and self._appearance_starts and not editing:
            self._show_appearance(self._appearance_index_at(seconds), seconds)
        self._highlight(seconds)

    def _highlight(self, seconds: float) -> None:
        """Mark the segment being spoken, if it is in the list shown now."""
        if self._project is None:
            return
        segment = self._project.transcript.index_at(seconds)
        row = self._rows.get(segment, -1) if segment is not None else -1
        self._transcript.highlight_row(row)

    def _on_slide_activated(self, row: int) -> None:
        """The user picked a slide: show it now, whatever playback is doing."""
        self._show_appearance(row)

    def _on_tab_changed(self, _index: int) -> None:
        if self._project is not None:
            self._show_transcript()

    def _on_seek_requested(self, seconds: float) -> None:
        # Clicking a segment re-arms follow mode: the user has just told us
        # where they want to be.
        self._syncing = True
        try:
            self._player.seek(seconds)
        finally:
            self._syncing = False

    def _on_recapture(self, appearance_id: int) -> None:
        self.slideRecaptureRequested.emit(appearance_id, self._player.position)

    def _on_split_here(self) -> None:
        self.slideSplitRequested.emit(self._player.position)

    def _on_move_boundary(self, appearance_id: int) -> None:
        self.slideMoveBoundaryRequested.emit(appearance_id, self._player.position)

    def _on_player_error(self, message: str) -> None:
        QMessageBox.warning(self, "Playback problem", message)
