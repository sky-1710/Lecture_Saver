"""Make the preview (예습) or the review (복습): one dialog, two PDFs.

They are read days apart, so each is made when it is wanted. The preview is a
lesson the assistant teaches from the lecture's PDF and transcript. The review
is the notes Lecture Saver already makes, and a quiz the assistant writes from
the same. Pictures of the PDF's pages go with both. They are the one thing
beyond words that leaves the computer, so the panel at the top says so whenever
they would go, and a box here keeps them home.

The assistant is asked only for what the project does not have yet. Once the
preview or the quiz is saved, pressing the button again rebuilds the PDF from
disk in seconds and costs no quota - which is what happens after correcting a
transcript line or changing the slide options.

When the transcript has been edited since the material was made, the dialog
says so and offers to make it again; it does not do that on its own, because
most edits are a fixed word that changes nothing the lesson says.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..core.project import Project, ProjectError
from ..core.study import QUIZ_LENGTH, StudySet, transcript_fingerprint
from ..export.document import LectureDocument
from ..export.options import PdfOptions
from ..export.study_pdf import preview_pdf_name, review_pdf_name
from ..study.generator import PART_GUIDE
from ..translate import engine_from_settings
from ..translate.engine import TranslationEngine
from .ai_consent import ensure_ai_consent
from .reference_row import ReferenceRow
from .theme import Role, set_role
from .workers import StudyResult, StudyWorker, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["StudyDialog"]


def _when(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


class StudyDialog(QDialog):
    """Say what exists, what would be asked for, then do it."""

    #: Emitted after what arrived was saved into the project.
    changed = Signal()

    def __init__(
        self,
        project: Project,
        settings: Settings,
        document: LectureDocument,
        pdf_options: PdfOptions,
        output_dir: Path,
        part: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._preview = part == PART_GUIDE
        self.setWindowTitle("Preview (예습)" if self._preview else "Review (복습)")
        self.resize(580, 440)
        self._project = project
        self._settings = settings
        self._document = document
        self._pdf_options = pdf_options
        self._part = part
        name = preview_pdf_name if self._preview else review_pdf_name
        self._output = Path(output_dir) / name(document.title)
        self._engine: TranslationEngine = engine_from_settings(settings)
        self._fingerprint = transcript_fingerprint(document.segments)
        self._thread: QThread | None = None
        self._worker: StudyWorker | None = None
        self._save_errors: list[str] = []

        intro = QLabel(
            "<b>A PDF to read before the lecture.</b> The assistant teaches it from the "
            "lecture's PDF and transcript: the big picture, a concept map, the background "
            "the professor skips, each idea in turn, and one worked example."
            if self._preview else
            "<b>A PDF for after the lecture</b>, in this order:<br>"
            "1. <b>강의 노트</b> — each slide with what was said<br>"
            f"2. <b>복습 퀴즈</b> — {QUIZ_LENGTH} questions in English, "
            "answers and explanations on the next page"
        )
        intro.setWordWrap(True)

        self._notice = QLabel()
        self._notice.setWordWrap(True)
        notice_frame = QFrame()
        notice_layout = QVBoxLayout(notice_frame)
        notice_layout.setContentsMargins(12, 10, 12, 10)
        notice_layout.addWidget(self._notice)

        self._reference = ReferenceRow(
            project, settings,
            "No PDF attached: it is taught from the transcript alone." if self._preview
            else "No PDF attached: the quiz is written from the transcript alone.",
            "The professor's slides or notes for this lecture. What the assistant writes "
            "is based on them, and their text and pictures are kept with the lecture.",
        )
        self._reference.changed.connect(self._refresh)

        self._send_pictures = QCheckBox("Also send pictures of the PDF's pages")
        self._send_pictures.setChecked(settings.study_sends_page_pictures)
        self._send_pictures.setToolTip(
            "So that the assistant can see diagrams, which have no text to read.\n"
            "This uses more of your usage than the text does."
        )
        self._send_pictures.toggled.connect(self._on_send_pictures)

        self._saved_label = QLabel()
        self._stale_label = QLabel()
        self._stale_label.setWordWrap(True)
        set_role(self._stale_label, Role.WARNING)
        self._plan_label = QLabel()
        self._plan_label.setWordWrap(True)
        self._output_label = QLabel()
        self._output_label.setWordWrap(True)
        set_role(self._output_label, Role.MUTED)

        self._redo = QCheckBox(f"Write the {self._thing} again")
        self._redo.toggled.connect(self._refresh)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        set_role(self._detail, Role.MUTED)

        self._start_button = QPushButton("Create PDF")
        self._start_button.setDefault(True)
        self._start_button.clicked.connect(self._start)
        self._stop_button = QPushButton("Stop")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._stop)
        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        buttons.addWidget(self._stop_button)
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(intro)
        layout.addWidget(notice_frame)
        layout.addWidget(self._reference)
        layout.addWidget(self._send_pictures)
        layout.addWidget(self._saved_label)
        layout.addWidget(self._stale_label)
        layout.addWidget(self._redo)
        layout.addWidget(self._plan_label)
        layout.addWidget(self._output_label)
        layout.addStretch(1)
        layout.addWidget(self._progress)
        layout.addWidget(self._detail)
        layout.addLayout(buttons)

        self._refresh()

    # -- state ----------------------------------------------------------------

    @property
    def _thing(self) -> str:
        return "preview" if self._preview else "quiz"

    @property
    def study(self) -> StudySet:
        return self._project.study

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    @property
    def _saved(self) -> bool:
        return (self.study.guide if self._preview else self.study.quiz) is not None

    @property
    def _made_at(self) -> str | None:
        return self.study.guide_made_at if self._preview else self.study.quiz_made_at

    @property
    def will_ask(self) -> bool:
        """Whether the assistant would be asked if the button were pressed now."""
        return self._redo.isChecked() or not self._saved

    @property
    def _pictures(self) -> tuple[Path, ...]:
        """The pictures a run would send."""
        if not self._send_pictures.isChecked():
            return ()
        return tuple(self._project.reference_pictures)

    def _on_send_pictures(self, checked: bool) -> None:
        self._settings.study_sends_page_pictures = checked
        self._refresh()

    def _update_notice(self) -> None:
        """Say what a run would send, now, not what it usually sends."""
        reference = self._project.reference
        pictures = len(self._pictures)
        if not self.will_ask:
            sent = "This one is saved, so nothing is sent."
        elif pictures:
            pages = (
                "its pages" if pictures >= len(reference.pages)
                else f"its first {pictures} pages"
            )
            sent = (
                "The transcript <b>text</b>, the <b>text of every page of the attached "
                f"PDF</b> and <b>pictures of {pages}</b> are sent to the assistant. The "
                "video, the audio and the pictures taken from the video are not sent."
            )
        elif not reference.is_empty:
            sent = (
                "The transcript <b>text</b> and the <b>text of every page of the attached "
                "PDF</b> are sent to the assistant. The video, the audio and the slide "
                "images are not sent."
            )
        else:
            sent = (
                f"The {self._thing} is written by the assistant from the transcript "
                "<b>text</b>. The video, the audio and the slide images are not sent."
            )
        self._notice.setText(sent)

    def _refresh(self) -> None:
        self._update_notice()
        self._reference.set_usable(not self.is_running)
        self._send_pictures.setVisible(bool(self._project.reference_pictures) and self.will_ask)
        self._close_button.setEnabled(not self.is_running and not self._reference.is_reading)
        name = "예습 (preview)" if self._preview else "복습 퀴즈 (quiz)"
        self._saved_label.setText(
            f"{name}: <b>ready</b> — made {_when(self._made_at)}"
            if self._saved else f"{name}: not made yet"
        )
        stale = self._thing in self.study.stale_parts(self._fingerprint)
        self._stale_label.setVisible(stale and not self._redo.isChecked())
        if stale:
            self._stale_label.setText(
                f"The transcript was edited after this {self._thing} was made. Tick the "
                "box below to write it again from the corrected transcript."
            )
        self._redo.setVisible(self._saved)
        self._redo.setEnabled(not self.is_running)

        if not self._document.segments:
            self._plan_label.setText("There is no transcript yet. Transcribe the lecture first.")
            set_role(self._plan_label, Role.DANGER)
            self._start_button.setEnabled(False)
            return

        asking = self.will_ask
        availability = self._engine.availability() if asking else None
        if availability is not None and not availability.ready:
            self._plan_label.setText(availability.remedy or "The assistant is not available.")
            set_role(self._plan_label, Role.DANGER)
            self._start_button.setEnabled(False)
            return

        if asking:
            self._plan_label.setText(
                f"{self._engine.label} will be asked to write the {self._thing}. "
                "It is saved as soon as it arrives."
            )
            self._start_button.setText(f"Create {'preview' if self._preview else 'review'}")
        else:
            self._plan_label.setText(
                f"The {self._thing} is saved, so the PDF is made straight away — nothing "
                "is sent and no usage is spent."
            )
            self._start_button.setText("Create PDF")
        set_role(self._plan_label, None)
        self._output_label.setText(f"Saves to: {self._output}")
        self._start_button.setEnabled(not self.is_running and not self._reference.is_reading)

    # -- running ---------------------------------------------------------------

    def _start(self) -> None:
        if self.is_running or self._reference.is_reading:
            return
        asking = self.will_ask
        if asking and not ensure_ai_consent(self, self._settings, self._engine.label):
            return

        # The worker builds the PDF from its own copy; what it makes is handed
        # back through part_ready to be saved into the live project.
        snapshot = StudySet.from_dict(self.study.to_dict())

        self._save_errors = []
        self._thread = QThread(self)
        self._set_running(True)
        self._detail.setText("Starting...")
        self._worker = StudyWorker(
            self._document, self._engine, snapshot, self._part, self._pdf_options, self._output,
            ask=asking, reference=self._project.reference, pictures=self._pictures,
        )
        self._worker.stage.connect(self._on_stage)
        self._worker.part_ready.connect(self._on_part)
        self._worker.finished.connect(self._on_finished)
        run_in_thread(self._worker, self._thread)

    def _set_running(self, running: bool) -> None:
        self._progress.setVisible(running)
        self._start_button.setEnabled(not running)
        self._stop_button.setEnabled(running)
        self._close_button.setEnabled(not running)
        self._redo.setEnabled(not running)
        self._send_pictures.setEnabled(not running)
        self._reference.set_usable(not running)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._stop_button.setEnabled(False)
            self._detail.setText("Stopping...")

    def _on_stage(self, stage: str) -> None:
        if stage == "pdf":
            self._detail.setText("Laying out the PDF...")
        elif self._preview:
            self._detail.setText("Writing the preview (예습)... this can take several minutes.")
        else:
            self._detail.setText(f"Writing the {QUIZ_LENGTH}-question quiz...")

    def _on_part(self, part: str, value: object) -> None:
        """Save what arrived. Runs on the UI thread, which owns the project."""
        if part == PART_GUIDE:
            self.study.set_guide(value, source=self._fingerprint, engine=self._engine.key)  # type: ignore[arg-type]
        else:
            self.study.set_quiz(value, source=self._fingerprint, engine=self._engine.key)  # type: ignore[arg-type]
        try:
            self._project.save_study()
        except ProjectError as exc:
            log.warning("Could not save the study material: %s", exc)
            self._save_errors.append(str(exc))
            return
        self.changed.emit()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(20000)
        self._thread = None
        self._worker = None
        self._set_running(False)
        self._detail.setText("")

    def _on_finished(self, result: StudyResult) -> None:
        self._teardown()
        self._redo.setChecked(False)
        self._refresh()
        self.show_result(result)

    def show_result(self, result: StudyResult) -> None:
        report = result.report
        lines: list[str] = []
        if result.pdf is not None:
            lines.append(f"Saved:\n{result.pdf}")
        if report.cancelled:
            lines.append("Stopped.")
        lines += report.errors
        lines += self._save_errors
        if result.pdf_error:
            lines.append(result.pdf_error)

        box = QMessageBox(self)
        box.setWindowTitle(self.windowTitle())
        failed = result.pdf is None
        box.setIcon(QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information)
        box.setText("\n\n".join(lines) or "Nothing to do.")
        open_pdf = open_folder = None
        if result.pdf is not None:
            open_pdf = box.addButton("Open PDF", QMessageBox.ButtonRole.ActionRole)
            open_folder = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        clicked = box.clickedButton()
        if result.pdf is not None and clicked is open_pdf:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.pdf)))
        elif result.pdf is not None and clicked is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.pdf.parent)))

    # -- closing ----------------------------------------------------------------

    def reject(self) -> None:
        if self._reference.is_reading:
            return  # a moment: the PDF is saved to this lecture when it is read
        if self.is_running:
            answer = QMessageBox.question(
                self, f"Stop making the {self._thing}?",
                f"The {self._thing} is being made. Stop?\n\n"
                "What is being written now would be lost.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self._worker is not None:
                # The dialog is going away: no result message after it has
                # closed. What still arrives is saved all the same.
                self._worker.finished.disconnect(self._on_finished)
            self._stop()
            self._teardown()
        super().reject()
