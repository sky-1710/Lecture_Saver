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
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.alignment import Alignment, align
from ..core.project import Project
from ..core.slides import SlideAppearance, SlideSet
from ..core.timecode import format_duration, format_hms
from ..core.transcript_store import TextMatch, TranscriptEditError, TranscriptStore
from ..transcription.whisper_engine import WhisperOptions
from .find_bar import FindBar
from .slide_panel import SlidePanel
from .transcribe_panel import TranscribePanel
from .transcript_view import TranscriptView
from .video_player import VideoPlayer

log = logging.getLogger(__name__)

__all__ = ["LectureView"]

TAB_SLIDE = 0
TAB_FULL = 1
PREVIEW_HEIGHT = 150


class LectureView(QWidget):
    """Everything the user works with once a project is open."""

    #: Emitted when the user asks to start transcription.
    transcribeRequested = Signal(object)  # WhisperOptions
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
    #: The project's source video is missing and the user wants to relocate it.
    relocateRequested = Signal()
    #: The transcript changed. ``True`` when segments were added, removed,
    #: merged or split - i.e. the slide links may have changed too.
    transcriptEdited = Signal(bool)
    #: A short message for the status bar.
    statusMessage = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._project: Project | None = None
        self._syncing = False
        #: Which segments belong to which slide, recomputed from the times
        #: whenever the transcript or the slides are re-bound.
        self._alignment = Alignment((), ())
        self._appearance_starts: list[float] = []
        #: Index of the appearance shown on the current-slide tab, or -1.
        self._shown = -1
        #: Global segment index -> row in the transcript list as filtered now.
        self._rows: dict[int, int] = {}
        self._preview_cache: dict[str, QPixmap] = {}
        self._matches: list[TextMatch] = []
        self._match_index = -1

        self._build()
        self._build_edit_actions()
        self._connect()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        # --- header ---
        self._title_label = QLabel()
        self._title_label.setStyleSheet("font-size: 15px; font-weight: 600;")
        self._subtitle_label = QLabel()
        self._subtitle_label.setStyleSheet("color: #666;")

        self._warning_bar = QFrame()
        self._warning_bar.setStyleSheet(
            "background: #FFF4E5; border: 1px solid #F0C36D; border-radius: 4px;"
        )
        self._warning_label = QLabel()
        self._warning_label.setWordWrap(True)
        self._relocate_button = QPushButton("Locate video...")
        warning_layout = QHBoxLayout(self._warning_bar)
        warning_layout.setContentsMargins(10, 6, 10, 6)
        warning_layout.addWidget(self._warning_label, 1)
        warning_layout.addWidget(self._relocate_button)
        self._warning_bar.setVisible(False)

        header = QVBoxLayout()
        header.setContentsMargins(12, 10, 12, 4)
        header.setSpacing(2)
        header.addWidget(self._title_label)
        header.addWidget(self._subtitle_label)

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
        centre_layout.setContentsMargins(6, 6, 6, 6)
        centre_layout.addWidget(self._player, 1)
        centre_layout.addLayout(follow_bar)

        # --- right: transcription + transcript ---
        self._panel = TranscribePanel()

        self._transcript_header = QLabel("Transcript")
        self._transcript_header.setStyleSheet("font-weight: 600;")

        self._tabs = QTabBar()
        self._tabs.addTab("Current slide")
        self._tabs.addTab("Full transcript")
        self._tabs.setDrawBase(False)
        self._tabs.setExpanding(False)
        # Two short tabs: scroll arrows would hide one of them in a narrow pane.
        self._tabs.setUsesScrollButtons(False)
        self._tabs.setVisible(False)

        transcript_bar = QHBoxLayout()
        transcript_bar.setContentsMargins(12, 8, 12, 4)
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
        self._save_label.setStyleSheet("color: #888;")
        self._edit_hint = QLabel("Double-click a line to correct it")
        self._edit_hint.setStyleSheet("color: #888;")
        self._edit_bar = QWidget()
        edit_layout = QHBoxLayout(self._edit_bar)
        edit_layout.setContentsMargins(12, 0, 12, 4)
        edit_layout.setSpacing(4)
        edit_layout.addWidget(self._undo_button)
        edit_layout.addWidget(self._redo_button)
        edit_layout.addWidget(self._find_button)
        edit_layout.addWidget(self._reset_button)
        edit_layout.addSpacing(8)
        edit_layout.addWidget(self._edit_hint)
        edit_layout.addStretch(1)
        edit_layout.addWidget(self._save_label)
        self._edit_bar.setVisible(False)

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
        self._slide_title.setStyleSheet("font-weight: 600;")
        self._slide_detail = QLabel()
        self._slide_detail.setStyleSheet("color: #666;")
        slide_header_layout = QVBoxLayout(self._slide_header)
        slide_header_layout.setContentsMargins(12, 4, 12, 6)
        slide_header_layout.setSpacing(2)
        slide_header_layout.addWidget(self._slide_preview)
        slide_header_layout.addWidget(self._slide_title)
        slide_header_layout.addWidget(self._slide_detail)
        self._slide_header.setVisible(False)

        self._transcript = TranscriptView()
        self._empty_label = QLabel(
            "No transcript yet.\n\nChoose a model above and select "
            "“Transcribe lecture”. Everything runs on this computer."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.setStyleSheet("color: #888;")

        right = QWidget()
        right.setMinimumWidth(300)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._panel)
        right_layout.addLayout(transcript_bar)
        right_layout.addWidget(self._edit_bar)
        right_layout.addWidget(self._find_bar)
        right_layout.addWidget(self._slide_header)
        right_layout.addWidget(self._empty_label, 1)
        right_layout.addWidget(self._transcript, 1)
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
        self._splitter.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self._warning_bar)
        layout.addWidget(self._splitter, 1)

    def _connect(self) -> None:
        self._player.positionChanged.connect(self._on_position)
        self._player.errorOccurred.connect(self._on_player_error)
        self._transcript.seekRequested.connect(self._on_seek_requested)
        self._follow_check.toggled.connect(self._transcript.set_follow_playback)
        self._panel.startRequested.connect(self.transcribeRequested)
        self._panel.cancelRequested.connect(self.cancelRequested)
        self._relocate_button.clicked.connect(self.relocateRequested)

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
        self._slides_pane.set_slides(SlideSet(), Path())
        self._alignment = Alignment((), ())
        self._appearance_starts = []
        self._shown = -1
        self._rows = {}
        self._preview_cache.clear()
        self._tabs.setVisible(False)
        self._slide_header.setVisible(False)
        self._edit_bar.setVisible(False)
        self._find_bar.setVisible(False)
        self._matches, self._match_index = [], -1
        self._save_label.setText("")
        self._update_edit_actions()

    # -- transcript / slide binding ----------------------------------------

    @property
    def showing_current_slide(self) -> bool:
        """True when the transcript is filtered to one slide."""
        return not self._tabs.isHidden() and self._tabs.currentIndex() == TAB_SLIDE

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

        both = bool(segments) and bool(appearances)
        if both and self._tabs.isHidden():
            # The slide-linked view is the point of having both; start there.
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(TAB_SLIDE)
            self._tabs.blockSignals(False)
        self._tabs.setVisible(both)

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

        if self.showing_current_slide and self._shown >= 0:
            indices = self._alignment.segments_for(self._shown)
            self._transcript.set_segments(tuple(segments[i] for i in indices))
            self._rows = {segment: row for row, segment in enumerate(indices)}
            self._update_slide_header(len(indices))
            self._slides_pane.select_row(self._shown)
        else:
            self._transcript.set_segments(segments)
            self._rows = {i: i for i in range(len(segments))}
            self._slide_header.setVisible(False)

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

        label = appearance.annotation_label
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
        """Undo, redo, find & replace and reset, for the window's Edit menu."""
        return [self._undo_action, self._redo_action, self._find_action, self._reset_action]

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
        self._reset_action = QAction("Reset to original transcription...", self)
        self._reset_action.setToolTip(
            "Discard every correction and go back to the transcript exactly as it "
            "was transcribed. Undo brings your corrections back."
        )
        self._reset_action.triggered.connect(self.reset_to_original)

        self._undo_button.setDefaultAction(self._undo_action)
        self._redo_button.setDefaultAction(self._redo_action)
        self._find_button.setDefaultAction(self._find_action)
        self._find_button.setText("Find")
        self._reset_button.setDefaultAction(self._reset_action)
        self._reset_button.setText("Reset")
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

    def set_save_status(self, text: str, *, error: bool = False) -> None:
        self._save_label.setText(text)
        self._save_label.setStyleSheet("color: #C0392B;" if error else "color: #888;")

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
