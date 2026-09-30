"""Main window: project lifecycle, menus, and orchestration of workers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QRect, Qt, QThread, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStackedWidget,
)

from ..config import Settings, icloud_drive_dir, log_file
from ..core.models import VideoInfo
from ..core.project import Project, ProjectError, is_project_dir
from ..core.slides import (
    SlideDetectionOptions,
    SlideEditError,
    SlideSet,
    delete_appearance,
    merge_with_previous,
    move_boundary,
    separate_slide,
    set_slide_image,
    split_at,
)
from ..core.timecode import format_hms
from ..export.document import LectureDocument, build_document
from ..export.options import ExportFormat, ExportSettings, resolve_output_dir
from ..media.errors import FFmpegNotFoundError, MediaError
from ..media.ffmpeg_tools import VIDEO_FILE_FILTER, find_ffmpeg, find_ffprobe
from ..slides.pipeline import capture_slide_image
from ..transcription.whisper_engine import WhisperOptions
from .autosave import Autosave
from .lecture_view import LectureView
from .start_view import StartView
from .theme import Role, set_role
from .workers import (
    JobWorker,
    ProbeWorker,
    SlideDetectionWorker,
    TranscriptionWorker,
    run_in_thread,
)

log = logging.getLogger(__name__)

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings
        self.project: Project | None = None

        self._probe_thread: QThread | None = None
        self._probe_worker: ProbeWorker | None = None
        self._transcribe_thread: QThread | None = None
        self._transcribe_worker: TranscriptionWorker | None = None
        self._slides_thread: QThread | None = None
        self._slides_worker: SlideDetectionWorker | None = None
        self._export_thread: QThread | None = None
        self._export_worker: JobWorker | None = None
        #: Whether the export in flight is the AI translation request. Kept here
        #: rather than captured in a lambda - see _run_export_job.
        self._export_is_ai_request = False

        self.setWindowTitle("Lecture Saver")
        self.setMinimumSize(820, 560)
        self.resize(1280, 800)

        self._stack = QStackedWidget()
        self._start_view = StartView()
        self._lecture_view = LectureView()
        self._stack.addWidget(self._start_view)
        self._stack.addWidget(self._lecture_view)
        self.setCentralWidget(self._stack)

        #: Transcript edits are written once typing pauses (§15).
        self._autosave = Autosave(self._save_transcript, parent=self)

        self._build_menus()
        self._build_status_bar()
        self._connect()
        self._restore_geometry()
        self._refresh_recent()
        self.statusBar().showMessage("Ready")

    # -- construction -------------------------------------------------------

    def _build_status_bar(self) -> None:
        """A percentage and a bar that any long job can drive (§27)."""
        self._status_percent = QLabel("")
        set_role(self._status_percent, Role.SECTION)
        self._status_percent.setMinimumWidth(38)
        self._status_bar_progress = QProgressBar()
        self._status_bar_progress.setFixedWidth(140)
        self._status_bar_progress.setTextVisible(False)
        self.statusBar().addPermanentWidget(self._status_percent)
        self.statusBar().addPermanentWidget(self._status_bar_progress)
        self._show_status_progress(False)

    def _show_status_progress(self, visible: bool) -> None:
        self._status_percent.setVisible(visible)
        self._status_bar_progress.setVisible(visible)
        if not visible:
            self._status_percent.setText("")
            self._set_title_progress(None)

    def _set_status_progress(self, stage: str, fraction: float) -> None:
        """Drive the status-bar bar, its percentage, and the window title."""
        self._show_status_progress(True)
        if fraction < 0:
            self._status_bar_progress.setRange(0, 0)
            self._status_percent.setText("...")
            self._set_title_progress(None)
        else:
            percent = max(0, min(100, int(fraction * 100)))
            self._status_bar_progress.setRange(0, 100)
            self._status_bar_progress.setValue(percent)
            self._status_percent.setText(f"{percent}%")
            self._set_title_progress(percent)
        if stage:
            self.statusBar().showMessage(stage)

    def _set_title_progress(self, percent: int | None) -> None:
        """Put the percentage in the title, so it is readable when minimised."""
        name = self.project.name if self.project is not None else None
        base = f"{name} - Lecture Saver" if name else "Lecture Saver"
        self.setWindowTitle(f"{percent}% - {base}" if percent is not None else base)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        self._new_action = QAction("&New project from video...", self)
        self._new_action.setShortcut(QKeySequence.StandardKey.New)
        file_menu.addAction(self._new_action)

        self._open_action = QAction("&Open project...", self)
        self._open_action.setShortcut(QKeySequence.StandardKey.Open)
        file_menu.addAction(self._open_action)

        self._recent_menu = file_menu.addMenu("Open &recent")
        file_menu.addSeparator()

        self._close_action = QAction("&Close project", self)
        self._close_action.setEnabled(False)
        file_menu.addAction(self._close_action)

        self._reveal_action = QAction("Show project &folder", self)
        self._reveal_action.setEnabled(False)
        file_menu.addAction(self._reveal_action)

        file_menu.addSeparator()
        self._quick_export_action = QAction("&Export PDF + Markdown", self)
        self._quick_export_action.setShortcut(QKeySequence("Ctrl+E"))
        self._quick_export_action.setEnabled(False)
        file_menu.addAction(self._quick_export_action)
        self._study_pack_action = QAction("Create &study pack...", self)
        self._study_pack_action.setEnabled(False)
        file_menu.addAction(self._study_pack_action)
        self._translate_action = QAction("&Translate with AI...", self)
        self._translate_action.setEnabled(False)
        file_menu.addAction(self._translate_action)
        self._ai_request_action = QAction("Create &AI translation request", self)
        self._ai_request_action.setEnabled(False)
        file_menu.addAction(self._ai_request_action)
        self._import_translation_action = QAction("&Import translation...", self)
        self._import_translation_action.setEnabled(False)
        file_menu.addAction(self._import_translation_action)
        self._export_action = QAction("More export &formats...", self)
        self._export_action.setShortcut(QKeySequence("Ctrl+Shift+E"))
        self._export_action.setEnabled(False)
        file_menu.addAction(self._export_action)
        self._default_export_action = QAction("&Default export folder...", self)
        self._default_export_action.triggered.connect(self._choose_default_export_dir)
        file_menu.addAction(self._default_export_action)

        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        self._edit_menu = self.menuBar().addMenu("&Edit")
        for action in self._lecture_view.edit_actions:
            self._edit_menu.addAction(action)

        help_menu = self.menuBar().addMenu("&Help")
        diagnostics = QAction("&Diagnostics...", self)
        diagnostics.triggered.connect(self._show_diagnostics)
        help_menu.addAction(diagnostics)

        self._debug_action = QAction("&Slide detection debug...", self)
        self._debug_action.setEnabled(False)
        self._debug_action.setToolTip(
            "Inspect why each slide boundary was or was not made, replayed from "
            "the cached frame scan."
        )
        self._debug_action.triggered.connect(self._show_slide_debug)
        help_menu.addAction(self._debug_action)

        show_log = QAction("Open &log file", self)
        show_log.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(log_file())))
        )
        help_menu.addAction(show_log)

        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _connect(self) -> None:
        self._new_action.triggered.connect(self._choose_video)
        self._open_action.triggered.connect(self._choose_project)
        self._close_action.triggered.connect(self._close_project)
        self._reveal_action.triggered.connect(self._reveal_project)
        self._export_action.triggered.connect(self._show_export)
        self._quick_export_action.triggered.connect(self._quick_export)
        self._study_pack_action.triggered.connect(self._study_pack)
        self._translate_action.triggered.connect(self._translate_with_ai)
        self._ai_request_action.triggered.connect(self._ai_translation_request)
        self._import_translation_action.triggered.connect(self._import_translation)

        self._start_view.newProjectRequested.connect(self._choose_video)
        self._start_view.openProjectRequested.connect(self._choose_project)
        self._start_view.videoDropped.connect(self._create_from_video)
        self._start_view.recentSelected.connect(self._open_project_at)

        self._lecture_view.transcribeRequested.connect(self._start_transcription)
        self._lecture_view.resumeTranscriptionRequested.connect(
            lambda options: self._start_transcription(options, resume=True)
        )
        self._lecture_view.cancelRequested.connect(self._cancel_transcription)
        self._lecture_view.relocateRequested.connect(self._relocate_video)
        self._lecture_view.detectSlidesRequested.connect(self._start_slide_detection)
        self._lecture_view.cancelSlidesRequested.connect(self._cancel_slide_detection)
        self._lecture_view.slideDeleteRequested.connect(self._delete_slide)
        self._lecture_view.slideMergeRequested.connect(self._merge_slide)
        self._lecture_view.slideRecaptureRequested.connect(self._recapture_slide)
        self._lecture_view.slideSplitRequested.connect(self._split_slide)
        self._lecture_view.slideMoveBoundaryRequested.connect(self._move_slide_boundary)
        self._lecture_view.slideSeparateRequested.connect(self._separate_slide)
        self._lecture_view.transcriptEdited.connect(self._on_transcript_edited)
        self._lecture_view.exportRequested.connect(self._show_export)
        self._lecture_view.quickExportRequested.connect(self._quick_export)
        self._lecture_view.studyPackRequested.connect(self._study_pack)
        self._lecture_view.translateRequested.connect(self._translate_with_ai)
        self._lecture_view.aiRequestRequested.connect(self._ai_translation_request)
        self._lecture_view.importTranslationRequested.connect(self._import_translation)
        self._lecture_view.statusMessage.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )
        self._autosave.saved.connect(
            lambda: self._lecture_view.set_save_status("All changes saved")
        )
        self._autosave.failed.connect(self._on_autosave_failed)

    # -- project lifecycle --------------------------------------------------

    def _choose_video(self) -> None:
        start_dir = str(self.settings.get("last_video_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a lecture video", start_dir, VIDEO_FILE_FILTER
        )
        if path:
            self.settings.set("last_video_dir", str(Path(path).parent))
            self._create_from_video(Path(path))

    def _create_from_video(self, video_path: Path) -> None:
        if self._busy:
            QMessageBox.information(
                self, "Busy", "Please wait for the current processing to finish."
            )
            return
        if not video_path.is_file():
            QMessageBox.warning(self, "File not found", f"There is no file at:\n{video_path}")
            return

        self.statusBar().showMessage(f"Reading {video_path.name}...")
        # Only the actions that would start a second job are held back. The
        # window itself stays usable: a worker that never reports back must not
        # be able to leave the whole application dead to the mouse.
        self._set_project_actions_enabled(False)
        self._start_view.setEnabled(False)

        self._probe_thread = QThread(self)
        self._probe_worker = ProbeWorker(video_path)
        self._probe_worker.finished.connect(self._on_probe_finished)
        self._probe_worker.failed.connect(self._on_probe_failed)
        run_in_thread(self._probe_worker, self._probe_thread)

    def _finish_probe(self) -> None:
        self._start_view.setEnabled(True)
        self._set_project_actions_enabled(True)
        if self._probe_thread is not None:
            self._probe_thread.quit()
            self._probe_thread.wait(5000)
            self._probe_thread = None
        self._probe_worker = None

    def _on_probe_failed(self, message: str) -> None:
        self._finish_probe()
        self.statusBar().showMessage("Ready")
        QMessageBox.critical(self, "Could not read the video", message)

    def _on_probe_finished(self, info: VideoInfo) -> None:
        self._finish_probe()
        self.statusBar().showMessage("Ready")

        default_name = info.as_path.stem
        name, accepted = QInputDialog.getText(
            self, "New project", "Lecture name:", text=default_name
        )
        if not accepted:
            return
        name = name.strip() or default_name

        if not info.has_audio:
            QMessageBox.warning(
                self,
                "No audio track",
                f"'{info.display_name}' has no audio track.\n\n"
                "The project will be created and the video will play, but it "
                "cannot be transcribed.",
            )

        projects_dir = self.settings.projects_dir
        try:
            projects_dir.mkdir(parents=True, exist_ok=True)
            project = Project.create(projects_dir, name, info)
        except (ProjectError, OSError) as exc:
            QMessageBox.critical(self, "Could not create the project", str(exc))
            return

        self._set_project(project)
        self.statusBar().showMessage(f"Project created in {project.root}", 8000)

    def _choose_project(self) -> None:
        start_dir = str(self.settings.projects_dir)
        path = QFileDialog.getExistingDirectory(self, "Open a lecture project", start_dir)
        if path:
            self._open_project_at(Path(path))

    def _open_project_at(self, root: Path) -> None:
        if not is_project_dir(root):
            QMessageBox.warning(
                self,
                "Not a project folder",
                f"'{root.name}' does not contain a project.json file.\n\n"
                "Choose the folder that was created for the lecture.",
            )
            return
        try:
            project = Project.open(root)
        except ProjectError as exc:
            QMessageBox.critical(self, "Could not open the project", str(exc))
            return
        self._set_project(project)
        self.statusBar().showMessage(f"Opened {project.name}", 5000)

    def _set_project(self, project: Project) -> None:
        self._flush_edits()
        self.project = project
        # Links written by an older version, or before a crash, are rebuilt
        # from the times rather than trusted.
        self._align()
        self.settings.add_recent_project(project.root)
        self._refresh_recent()

        self._lecture_view.load_project(project)
        self._stack.setCurrentWidget(self._lecture_view)
        self.setWindowTitle(f"{project.name} - Lecture Saver")
        self._close_action.setEnabled(True)
        self._reveal_action.setEnabled(True)
        self._export_action.setEnabled(True)
        self._quick_export_action.setEnabled(True)
        self._study_pack_action.setEnabled(True)
        self._translate_action.setEnabled(True)
        self._ai_request_action.setEnabled(True)
        self._import_translation_action.setEnabled(True)
        self._debug_action.setEnabled(True)

        if project.video_missing:
            QMessageBox.warning(
                self,
                "Video not found",
                f"The source video for this project is missing:\n{project.video.path}\n\n"
                "Use “Locate video...” to point the project at its new location.",
            )

    def _close_project(self) -> None:
        if not self._confirm_stop_transcription():
            return
        self._wait_for_export()
        self._flush_edits()
        if self.project is not None:
            try:
                self.project.save()
            except ProjectError:
                log.warning("Could not save project on close", exc_info=True)
        self.project = None
        self._lecture_view.close_project()
        self._stack.setCurrentWidget(self._start_view)
        self.setWindowTitle("Lecture Saver")
        self._close_action.setEnabled(False)
        self._reveal_action.setEnabled(False)
        self._export_action.setEnabled(False)
        self._quick_export_action.setEnabled(False)
        self._study_pack_action.setEnabled(False)
        self._translate_action.setEnabled(False)
        self._ai_request_action.setEnabled(False)
        self._import_translation_action.setEnabled(False)
        self._debug_action.setEnabled(False)
        self._refresh_recent()

    def _reveal_project(self) -> None:
        if self.project is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.project.root)))

    def _relocate_video(self) -> None:
        if self.project is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate the lecture video", str(Path.home()), VIDEO_FILE_FILTER
        )
        if not path:
            return
        self.project.relocate_video(Path(path))
        self._lecture_view.load_project(self.project)
        self.statusBar().showMessage("Video location updated", 5000)

    def _refresh_recent(self) -> None:
        roots = self.settings.recent_projects()
        self._start_view.set_recent(roots)
        self._recent_menu.clear()
        self._recent_menu.setEnabled(bool(roots))
        for root in roots:
            action = QAction(root.name, self)
            action.setToolTip(str(root))
            action.triggered.connect(lambda _checked=False, r=root: self._open_project_at(r))
            self._recent_menu.addAction(action)

    # -- transcription ------------------------------------------------------

    def _start_transcription(self, options: WhisperOptions, *, resume: bool = False) -> None:
        if self.project is None or self._busy:
            return

        self._flush_edits()
        # Continuing keeps what is there, so there is nothing to confirm.
        if self.project.has_transcript and not resume:
            answer = QMessageBox.question(
                self,
                "Replace the existing transcript?",
                "This lecture already has a transcript. Transcribing again will "
                "replace it, including any manual edits.\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.settings.set_transcription_defaults(
            {
                "model_size": options.model_size,
                "language": options.language,
                "vad_filter": options.vad_filter,
            }
        )

        self._lecture_view.pause_playback()
        self._lecture_view.begin_transcription()
        self._set_project_actions_enabled(False)

        self._transcribe_thread = QThread(self)
        self._transcribe_worker = TranscriptionWorker(self.project, options, resume=resume)
        self._transcribe_worker.progress.connect(self._on_transcription_progress)
        self._transcribe_worker.finished.connect(self._on_transcription_finished)
        self._transcribe_worker.failed.connect(self._on_transcription_failed)
        self._transcribe_worker.cancelled.connect(self._on_transcription_cancelled)
        run_in_thread(self._transcribe_worker, self._transcribe_thread)

    def _on_transcription_progress(self, stage: str, fraction: float, message: str) -> None:
        self._set_status_progress(stage, fraction)
        self._lecture_view.update_progress(stage, fraction, message)
        self.statusBar().showMessage(f"{stage} — {message}" if message else stage)

    def _finish_transcription(self) -> None:
        self._show_status_progress(False)
        if self._transcribe_thread is not None:
            self._transcribe_thread.quit()
            self._transcribe_thread.wait(15000)
            self._transcribe_thread = None
        self._transcribe_worker = None
        self._set_project_actions_enabled(True)

    def _on_transcription_finished(self, result: object) -> None:
        self._finish_transcription()
        self._align()
        self._lecture_view.refresh_transcript()

        count = len(self.project.transcript) if self.project else 0
        language = getattr(result, "language", None)
        summary = f"Transcript ready — {count} segments"
        if language:
            summary += f" ({language})"
        self._lecture_view.end_transcription(summary + ".")
        self.statusBar().showMessage(summary, 10000)

        note = getattr(result, "fallback_note", None)
        if note:
            QMessageBox.information(self, "Transcription finished", note)

    def _on_transcription_failed(self, message: str) -> None:
        self._finish_transcription()
        self._align()
        self._lecture_view.refresh_transcript()
        self._lecture_view.end_transcription("Transcription failed.")
        self.statusBar().showMessage("Transcription failed", 8000)
        QMessageBox.critical(self, "Transcription failed", message)

    def _on_transcription_cancelled(self) -> None:
        self._finish_transcription()
        self._align()
        self._lecture_view.refresh_transcript()
        count = len(self.project.transcript) if self.project else 0
        self._lecture_view.end_transcription(
            f"Cancelled. {count} segments were kept." if count else "Cancelled."
        )
        self.statusBar().showMessage("Transcription cancelled", 6000)

    def _cancel_transcription(self) -> None:
        if self._transcribe_worker is not None:
            self._transcribe_worker.cancel()
            self.statusBar().showMessage("Cancelling...")

    def _confirm_stop_transcription(self) -> bool:
        if self._transcribe_thread is None:
            return True
        answer = QMessageBox.question(
            self,
            "Transcription in progress",
            "A transcription is still running. Stop it?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._cancel_transcription()
        if self._transcribe_thread is not None:
            self._transcribe_thread.quit()
            self._transcribe_thread.wait(20000)
            self._transcribe_thread = None
        return True

    # -- slide detection (§8) ----------------------------------------------

    def _start_slide_detection(self) -> None:
        if self.project is None or self._busy:
            return
        if self.project.video_missing:
            QMessageBox.warning(
                self, "Video not found",
                "The source video is missing, so slides cannot be detected.",
            )
            return

        if self.project.has_slides:
            warning = (
                "This lecture already has detected slides. Running detection "
                "again replaces them and their images."
            )
            if self.project.slides.manually_edited:
                warning += "\n\nYour manual corrections to the slides will be lost."
            answer = QMessageBox.question(
                self,
                "Detect slides again?",
                warning + "\n\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        options = self.project.meta.slide_detection or SlideDetectionOptions()

        self._lecture_view.pause_playback()
        self._lecture_view.begin_slide_detection()
        self._set_project_actions_enabled(False)

        self._slides_thread = QThread(self)
        self._slides_worker = SlideDetectionWorker(self.project, options)
        self._slides_worker.progress.connect(self._on_slides_progress)
        self._slides_worker.finished.connect(self._on_slides_finished)
        self._slides_worker.failed.connect(self._on_slides_failed)
        self._slides_worker.cancelled.connect(self._on_slides_cancelled)
        run_in_thread(self._slides_worker, self._slides_thread)

    def _on_slides_progress(self, stage: str, fraction: float, message: str) -> None:
        self._set_status_progress(stage, fraction)
        self._lecture_view.update_slide_progress(stage, fraction, message)
        self.statusBar().showMessage(f"{stage} — {message}" if message else stage)

    def _finish_slide_detection(self) -> None:
        self._show_status_progress(False)
        if self._slides_thread is not None:
            self._slides_thread.quit()
            self._slides_thread.wait(15000)
            self._slides_thread = None
        self._slides_worker = None
        self._set_project_actions_enabled(True)

    def _on_slides_finished(self, slide_set: object) -> None:
        self._finish_slide_detection()
        self._align()
        self._lecture_view.refresh_slides()
        self._tidy_sentences_after_detection()

        count = len(self.project.slides) if self.project else 0
        summary = f"{count} slides detected"
        self._lecture_view.end_slide_detection(summary + ".")
        self.statusBar().showMessage(summary, 10000)

    def _tidy_sentences_after_detection(self) -> None:
        """Split lines that the new slide boundaries run through (§10).

        Only on a transcript nobody has corrected yet: this is a real edit to
        the transcript, undoable like any other, and it must never rearrange
        work the user has already done by hand.
        """
        project = self.project
        if project is None or project.transcript.is_empty:
            return
        if project.transcript.can_undo or not project.is_original_transcript():
            return
        if self._lecture_view.split_at_slide_changes(quiet=True):
            self._save_transcript()

    def _on_slides_failed(self, message: str) -> None:
        self._finish_slide_detection()
        self._lecture_view.end_slide_detection("Slide detection failed.")
        self.statusBar().showMessage("Slide detection failed", 8000)
        QMessageBox.critical(self, "Slide detection failed", message)

    def _on_slides_cancelled(self) -> None:
        self._finish_slide_detection()
        self._align()
        self._lecture_view.refresh_slides()
        self._lecture_view.end_slide_detection("Slide detection cancelled.")
        self.statusBar().showMessage("Slide detection cancelled", 6000)

    def _cancel_slide_detection(self) -> None:
        if self._slides_worker is not None:
            self._slides_worker.cancel()
            self.statusBar().showMessage("Cancelling...")

    # -- manual slide correction -------------------------------------------
    #
    # Automatic detection is a first draft. No detector gets every lecture
    # style right, so the user must be able to fix it before the transcript is
    # aligned to slides and exported.

    def _edit_slides(self, action: Callable[[SlideSet], object], success: str) -> None:
        """Apply a slide edit, persist it, and refresh the navigator.

        Every correction goes through here, so none of them can change memory
        without also reaching disk.
        """
        if self.project is None or self._busy:
            return
        try:
            action(self.project.slides)
        except SlideEditError as exc:
            QMessageBox.warning(self, "Cannot change the slides", str(exc))
            return
        except MediaError as exc:
            QMessageBox.warning(self, "Cannot capture that frame", str(exc))
            return

        try:
            self.project.save_slides()
        except ProjectError as exc:
            QMessageBox.critical(self, "Could not save the slides", str(exc))
            return

        # Speech follows the corrected intervals immediately.
        self._align()
        self._lecture_view.refresh_slides()
        self.statusBar().showMessage(success, 6000)

    def _delete_slide(self, appearance_id: int) -> None:
        answer = QMessageBox.question(
            self,
            "Delete this slide?",
            f"Slide {appearance_id} will be removed and its time given to the "
            "slide before it.\n\nThe transcript text is not affected; what was "
            "said on this slide moves to its neighbour.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._edit_slides(
            lambda slides: delete_appearance(slides, appearance_id),
            f"Slide {appearance_id} deleted",
        )

    def _merge_slide(self, appearance_id: int) -> None:
        self._edit_slides(
            lambda slides: merge_with_previous(slides, appearance_id),
            f"Slide {appearance_id} merged into the previous slide",
        )

    def _split_slide(self, seconds: float) -> None:
        def apply(slides: SlideSet) -> None:
            assert self.project is not None
            created = split_at(slides, seconds)
            image = capture_slide_image(
                self.project, created.slide_id, seconds, self.project.meta.slide_detection
            )
            set_slide_image(slides, created.slide_id, image)

        self._edit_slides(apply, f"New slide added at {format_hms(seconds)}")

    def _move_slide_boundary(self, appearance_id: int, seconds: float) -> None:
        self._edit_slides(
            lambda slides: move_boundary(slides, appearance_id, seconds),
            f"Slide {appearance_id} now starts at {format_hms(seconds)}",
        )

    def _separate_slide(self, appearance_id: int) -> None:
        def apply(slides: SlideSet) -> None:
            assert self.project is not None
            appearance = separate_slide(slides, appearance_id)
            options = self.project.meta.slide_detection
            settle = options.timing.settle_seconds if options else 1.0
            moment = min(appearance.start + settle, max(appearance.start, appearance.end - 0.1))
            image = capture_slide_image(self.project, appearance.slide_id, moment, options)
            set_slide_image(slides, appearance.slide_id, image)

        self._edit_slides(apply, f"Slide {appearance_id} is now its own slide")

    def _recapture_slide(self, appearance_id: int, seconds: float) -> None:
        def apply(slides: SlideSet) -> None:
            assert self.project is not None
            appearance = next(
                (a for a in slides.appearances if a.appearance_id == appearance_id), None
            )
            if appearance is None:
                raise SlideEditError(f"There is no slide numbered {appearance_id}.")
            image = capture_slide_image(
                self.project, appearance.slide_id, seconds,
                self.project.meta.slide_detection,
            )
            set_slide_image(slides, appearance.slide_id, image)

        self._edit_slides(apply, f"Slide {appearance_id} image re-captured")

    # -- transcript-to-slide alignment (§10) -------------------------------

    def _align(self, *, save: bool = True) -> None:
        """Re-link the transcript to the slides after either changes.

        Failing to save the links is reported but never blocks the work that
        triggered it: the links are rebuilt from the times on the next change.
        """
        if self.project is None:
            return
        try:
            self.project.align(save=save)
        except ProjectError as exc:
            log.warning("Could not save the transcript-to-slide links", exc_info=True)
            self.statusBar().showMessage(f"Could not save slide links: {exc}", 8000)

    # -- transcript editing and autosave (§13, §15) ------------------------

    def _on_transcript_edited(self, structural: bool) -> None:
        if self.project is None:
            return
        if structural:
            # Merge, split, add and delete change what each slide holds; the
            # write is left to autosave like any other edit.
            self._align(save=False)
        self._lecture_view.set_save_status("Saving...")
        self._autosave.schedule()

    def _save_transcript(self) -> bool:
        if self.project is None:
            return False
        return self.project.transcript.save()

    def _flush_edits(self) -> None:
        """Write pending edits now - before closing, switching or replacing."""
        if self._autosave.pending or (self.project and self.project.transcript.is_dirty):
            self._autosave.flush()

    # -- export (§19-§23) -----------------------------------------------------

    def _show_export(self) -> None:
        if self.project is None or self._busy:
            return
        # Export what the user sees: the line being typed is part of it.
        self._lecture_view.finish_editing()
        self._flush_edits()
        from .export_dialog import ExportDialog

        dialog = ExportDialog(
            self.project, self,
            default_dir=self._usable_default_export_dir(), app_settings=self.settings,
        )
        dialog.exported.connect(self._on_exported)
        dialog.exec()

    def _usable_default_export_dir(self) -> Path | None:
        """The default export folder, or ``None`` - with a warning - if it is gone.

        A missing iCloud Drive folder must not be silently recreated as a plain
        folder that never syncs, so exports fall back to the project folder and
        say so.
        """
        folder = self.settings.default_export_dir
        if folder is None:
            return None
        if folder.is_dir():
            return folder
        self.statusBar().showMessage(
            f"The default export folder {folder} was not found - exporting to the "
            "project folder instead. Is iCloud for Windows running?",
            15000,
        )
        return None

    def _choose_default_export_dir(self) -> None:
        """Pick the folder every lecture exports to, e.g. an iCloud Drive folder."""
        current = self.settings.default_export_dir
        if current is not None:
            box = QMessageBox(self)
            box.setWindowTitle("Default export folder")
            box.setText(
                f"Lectures export to:\n{current}\n\n"
                "Each file starts with the lecture's name, e.g. "
                "<lecture name>_lecture_notes.pdf. A lecture that has chosen its "
                "own folder in the export window keeps using that folder."
            )
            change = box.addButton("Change...", QMessageBox.ButtonRole.AcceptRole)
            stop = box.addButton("Use project folders", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            if box.clickedButton() is stop:
                self.settings.default_export_dir = None
                self.statusBar().showMessage(
                    "Lectures now export to their own project folders.", 8000
                )
                return
            if box.clickedButton() is not change:
                return

        start = current or icloud_drive_dir() or Path.home()
        path = QFileDialog.getExistingDirectory(self, "Default export folder", str(start))
        if not path:
            return
        self.settings.default_export_dir = Path(path)
        self.statusBar().showMessage(
            f"Lectures now export to {Path(path)}.", 10000
        )

    def _export_snapshot(
        self, *, use_default_folder: bool = True
    ) -> tuple[LectureDocument, ExportSettings, Path] | None:
        """Everything a background export needs, taken on the UI thread.

        Without ``use_default_folder`` a lecture with no folder of its own
        writes to its project folder rather than the app-wide export folder.
        """
        if self.project is None:
            return None
        if self._export_thread is not None:
            # Silence here reads as a frozen application; say what is going on.
            self.statusBar().showMessage(
                "An export is already running - one moment...", 5000
            )
            return None
        if self._busy:
            self.statusBar().showMessage(
                "Wait for the current processing to finish, then export.", 5000
            )
            return None
        self._lecture_view.finish_editing()
        self._flush_edits()
        settings = ExportSettings.for_lecture(
            self.project.meta.export_settings, self.settings.export_defaults
        )
        default_dir = (
            self._usable_default_export_dir()
            if use_default_folder and not settings.output_dir
            else None
        )
        output = resolve_output_dir(
            settings.output_dir, self.project.exports_dir, self.project.name, default_dir
        )
        return build_document(self.project), settings, output

    def _quick_export(self) -> None:
        """One click: the notes PDF and the Markdown, with the last-used options."""
        snapshot = self._export_snapshot()
        if snapshot is None:
            return
        from ..export.exporter import run_export

        document, settings, output = snapshot
        settings.formats = [ExportFormat.PDF, ExportFormat.MARKDOWN]
        self._run_export_job(
            lambda report: run_export(
                document, settings, output, on_progress=self._export_reporter(report)
            ),
            ai_request=False,
        )

    def _ai_translation_request(self) -> None:
        """Export the lecture with a Korean translation request at the top."""
        if self.project is None:
            return
        if self.project.transcript.is_empty:
            QMessageBox.information(
                self, "No transcript yet",
                "Transcribe the lecture first - the request asks for the transcript "
                "to be translated.",
            )
            return
        if self.project.transcript.language == "ko":
            answer = QMessageBox.question(
                self,
                "This lecture is in Korean",
                "This lecture was transcribed as Korean. The translation request is "
                "meant for English lectures, so an assistant would have nothing to "
                "translate.\n\nCreate the request anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        # The request is a working file for the assistant, not notes to read, so
        # it stays out of the default export folder (an iCloud folder, say).
        snapshot = self._export_snapshot(use_default_folder=False)
        if snapshot is None:
            return
        from ..export.ai_request import run_ai_request_export

        document, settings, output = snapshot
        self._run_export_job(
            lambda report: run_ai_request_export(
                document, settings.pdf, output, on_progress=self._export_reporter(report)
            ),
            ai_request=True,
        )

    def _study_pack(self) -> None:
        """Preview, notes and review quiz in one PDF."""
        if self.project is None or self._busy:
            return
        if self.project.transcript.is_empty:
            QMessageBox.information(
                self, "No transcript yet",
                "Transcribe the lecture first - the study pack is written from it.",
            )
            return
        # The same snapshot an export takes: pending edits flushed, the notes
        # options last used, and the folder the notes PDF goes to.
        snapshot = self._export_snapshot()
        if snapshot is None:
            return
        from .study_dialog import StudyPackDialog

        document, settings, output = snapshot
        dialog = StudyPackDialog(
            self.project, self.settings, document, settings.pdf, output, self
        )
        dialog.changed.connect(
            lambda: self.statusBar().showMessage("Study material saved", 6000)
        )
        dialog.exec()

    def _translate_with_ai(self) -> None:
        """Translate the lecture here rather than carrying it to an assistant."""
        if self.project is None or self._busy:
            return
        if self.project.transcript.is_empty:
            QMessageBox.information(
                self, "No transcript yet",
                "Transcribe the lecture first - there is nothing to translate.",
            )
            return
        if not self.project.slides.appearances:
            QMessageBox.information(
                self, "No slides yet",
                "Detect slides first: a translation is stored against the slide "
                "it belongs to.",
            )
            return
        from .translate_dialog import TranslateDialog

        dialog = TranslateDialog(self.project, self.settings, self)
        dialog.changed.connect(self._lecture_view.refresh_translations)
        dialog.changed.connect(
            lambda: self.statusBar().showMessage("Translation updated", 6000)
        )
        dialog.exec()

    def _import_translation(self) -> None:
        """Bring an assistant's translation back and attach it to the slides."""
        if self.project is None or self._busy:
            return
        from .translation_dialog import TranslationDialog

        dialog = TranslationDialog(self.project, self)
        dialog.changed.connect(self._lecture_view.refresh_translations)
        dialog.changed.connect(
            lambda: self.statusBar().showMessage("Translation updated", 6000)
        )
        dialog.exec()

    @staticmethod
    def _export_reporter(report: Callable[[str, float], None]) -> Callable[..., None]:
        """Adapt an exporter's (format, position, total) to (stage, fraction)."""

        def on_progress(export_format: object, position: int, total: int) -> None:
            label = getattr(export_format, "label", str(export_format))
            fraction = position / total if total else -1.0
            report(f"Writing {label}...", fraction)

        return on_progress

    def _run_export_job(
        self, job: Callable[[Callable[[str, float], None]], object], *, ai_request: bool
    ) -> None:
        self._lecture_view.set_export_enabled(False)
        self.statusBar().showMessage(
            "Writing the export files... this can take a moment for a long lecture."
        )
        self._set_status_progress("Preparing the export...", 0.0)
        # A cursor that says "working" is the difference between a slow export
        # and an application the user believes has died.
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        self._export_is_ai_request = ai_request
        self._export_thread = QThread(self)
        self._export_worker = JobWorker(job)
        # Both slots must be bound methods of this window, never lambdas: a
        # plain callable has no receiver object, so Qt takes its thread from the
        # *sender* - the worker - and runs it there. That would put widget work
        # (a message box, the status bar) on a background thread, where Qt
        # refuses to draw it and the window is left blocked by a dialog that
        # never appears.
        self._export_worker.progress.connect(self._set_status_progress)
        self._export_worker.finished.connect(self._on_export_job_finished)
        run_in_thread(self._export_worker, self._export_thread)

    def _wait_for_export(self) -> None:
        """Let a running export finish writing before the project goes away."""
        if self._export_thread is not None:
            self._export_thread.quit()
            self._export_thread.wait(60000)

    def _on_export_job_finished(self, result: object) -> None:
        ai_request = self._export_is_ai_request
        QApplication.restoreOverrideCursor()
        self._show_status_progress(False)
        if self._export_thread is not None:
            self._export_thread.quit()
            self._export_thread.wait(15000)
        self._export_thread = None
        self._export_worker = None
        self._lecture_view.set_export_enabled(True)
        if isinstance(result, Exception):
            QMessageBox.critical(self, "Export failed", f"The export failed:\n{result}")
            return
        self._on_exported(result)
        self._show_export_result(result, ai_request=ai_request)

    def _show_export_result(self, result: object, *, ai_request: bool) -> None:
        from PySide6.QtWidgets import QApplication

        written = list(getattr(result, "written", []))
        failures = list(getattr(result, "failures", []))
        output = getattr(result, "output_dir", "")
        lines = []
        if written:
            lines.append(f"Saved to {output}:")
            lines += [f"  • {Path(path).name}" for path in written]
        for export_format, message in failures:
            lines.append(f"\n{getattr(export_format, 'label', export_format)} failed:\n{message}")
        if ai_request and written:
            lines.append(
                "\nGive the Markdown (or PDF) to an AI assistant. The request is at the "
                "top of the file. Nothing has been sent anywhere."
            )

        box = QMessageBox(self)
        box.setWindowTitle("AI translation request" if ai_request else "Export")
        box.setWindowModality(Qt.WindowModality.WindowModal)
        box.setIcon(QMessageBox.Icon.Information if not failures else QMessageBox.Icon.Warning)
        box.setText("\n".join(lines) or "Nothing was exported.")
        open_button = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        copy_button = None
        markdown = next((Path(p) for p in written if Path(p).suffix == ".md"), None)
        if ai_request and markdown is not None:
            copy_button = box.addButton("Copy request text", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        # The export runs in the background, so the user may well be in another
        # window by now: bring the answer to the front and flash the taskbar
        # rather than leaving a modal dialog hidden behind the main window.
        box.show()
        box.raise_()
        box.activateWindow()
        QApplication.alert(box, 3000)
        box.exec()

        clicked = box.clickedButton()
        if clicked is open_button and written:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(output)))
        elif copy_button is not None and clicked is copy_button and markdown is not None:
            QApplication.clipboard().setText(markdown.read_text(encoding="utf-8"))
            self.statusBar().showMessage("Request copied - paste it into the AI assistant", 8000)

    def _on_exported(self, result: object) -> None:
        written = getattr(result, "written", [])
        failures = getattr(result, "failures", [])
        if written and not failures:
            self.statusBar().showMessage(
                f"Exported {len(written)} file{'s' if len(written) != 1 else ''} to "
                f"{getattr(result, 'output_dir', '')}", 10000,
            )
        elif failures:
            self.statusBar().showMessage("Some exports failed - see the export window", 10000)

    def _on_autosave_failed(self, message: str) -> None:
        self._lecture_view.set_save_status("Not saved", error=True)
        self.statusBar().showMessage(message, 15000)

    # -- shared run-state helpers ------------------------------------------

    @property
    def _busy(self) -> bool:
        """True while any background pipeline is running."""
        return (
            self._transcribe_thread is not None
            or self._slides_thread is not None
            or self._probe_thread is not None
        )

    def _set_project_actions_enabled(self, enabled: bool) -> None:
        self._new_action.setEnabled(enabled)
        self._open_action.setEnabled(enabled)
        self._close_action.setEnabled(enabled)
        has_project = enabled and self.project is not None
        for action in (
            self._export_action, self._quick_export_action, self._translate_action,
            self._study_pack_action,
            self._ai_request_action, self._import_translation_action,
        ):
            action.setEnabled(has_project)
        self._lecture_view.set_export_enabled(enabled)

    # -- help ---------------------------------------------------------------

    def _show_diagnostics(self) -> None:
        from PySide6.QtCore import qVersion
        from PySide6.QtMultimedia import QMediaDevices

        try:
            ffmpeg = str(find_ffmpeg())
        except FFmpegNotFoundError:
            ffmpeg = "NOT FOUND"
        ffprobe = find_ffprobe()

        try:
            import faster_whisper

            whisper_line = f"faster-whisper {faster_whisper.__version__}"
        except Exception as exc:  # noqa: BLE001
            whisper_line = f"faster-whisper unavailable ({exc})"

        import shutil

        free = shutil.disk_usage(Path.home()).free

        outputs = QMediaDevices.audioOutputs()
        lines = [
            f"Qt: {qVersion()}",
            f"FFmpeg: {ffmpeg}",
            f"ffprobe: {ffprobe or 'not found (banner parsing will be used)'}",
            whisper_line,
            f"Free disk: {free / 1e9:.1f} GB",
            f"Audio outputs: {len(outputs)}",
            f"Log file: {log_file()}",
            f"Projects folder: {self.settings.projects_dir}",
            f"Default export folder: {self.settings.default_export_dir or 'project folders'}",
        ]
        QMessageBox.information(self, "Diagnostics", "\n".join(lines))

    def _show_slide_debug(self) -> None:
        if self.project is None:
            return
        from .slide_debug import SlideDebugDialog

        SlideDebugDialog(self.project, self).show()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Lecture Saver",
            "<h3>Lecture Saver</h3>"
            "<p>Local lecture transcription and slide-linked notes.</p>"
            "<p><b>Everything runs on this computer.</b> Video, audio and "
            "transcripts are never uploaded. The only time an internet "
            "connection is needed is the first download of a Whisper model.</p>"
            "<p>Currently implemented: project management, video playback, "
            "local transcription, slide detection, transcript-to-slide "
            "alignment, transcript editing and export (Phases 1-6).</p>",
        )

    # -- window state -------------------------------------------------------

    def _restore_geometry(self) -> None:
        geometry = self.settings.get("window_geometry")
        if not (isinstance(geometry, list) and len(geometry) == 4):
            return
        try:
            rect = QRect(*(int(value) for value in geometry))
        except (TypeError, ValueError):
            return
        self.setGeometry(self._fit_to_screen(rect))

    @staticmethod
    def _fit_to_screen(rect: QRect) -> QRect:
        """Keep a remembered window inside the screen it will open on.

        A window saved on a larger or differently arranged display can otherwise
        come back half off-screen, with the buttons at its edges unreachable.
        """
        screen = QGuiApplication.screenAt(rect.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return rect
        available = screen.availableGeometry()
        fitted = QRect(rect)
        fitted.setWidth(min(fitted.width(), available.width()))
        fitted.setHeight(min(fitted.height(), available.height()))
        # QRect.right()/bottom() are inclusive; compute the limits from the
        # size instead, or a maximised window comes back one pixel too high.
        right_limit = available.x() + available.width() - fitted.width()
        bottom_limit = available.y() + available.height() - fitted.height()
        fitted.moveLeft(min(max(fitted.left(), available.left()), right_limit))
        fitted.moveTop(min(max(fitted.top(), available.top()), bottom_limit))
        return fitted

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        if not self._confirm_stop_transcription():
            event.ignore()
            return

        rect = self.geometry()
        self.settings.set("window_geometry", [rect.x(), rect.y(), rect.width(), rect.height()])

        self._wait_for_export()
        self._flush_edits()
        if self.project is not None:
            try:
                self.project.save()
            except ProjectError:
                log.warning("Could not save project on exit", exc_info=True)
        event.accept()
