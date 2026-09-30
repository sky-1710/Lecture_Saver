"""Make the study pack: preview (예습), lecture notes, and a review quiz.

The assistant is asked only for what the project does not have yet. Once the
preview and the quiz are saved, pressing the button again rebuilds the PDF
from disk in seconds and costs no quota - which is what happens after
correcting a transcript line or changing the slide options.

When the transcript has been edited since the material was made, the dialog
says so and offers to make it again; it does not do that on its own, because
most edits are a fixed word that changes nothing the quiz asks about.
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
from ..export.study_pdf import study_pack_name
from ..study.generator import PART_GUIDE, PART_QUIZ
from ..translate import engine_from_settings
from ..translate.engine import TranslationEngine
from .ai_consent import ensure_ai_consent
from .theme import Role, set_role
from .workers import StudyPackResult, StudyPackWorker, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["StudyPackDialog"]

_STAGE_TEXT = {
    PART_GUIDE: "Writing the preview (예습)... this takes a minute or two.",
    PART_QUIZ: f"Writing the {QUIZ_LENGTH}-question quiz...",
    "pdf": "Laying out the PDF...",
}


def _when(stamp: str | None) -> str:
    if not stamp:
        return ""
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


class StudyPackDialog(QDialog):
    """Say what exists, what would be asked for, then do it."""

    #: Emitted after a part was saved into the project.
    changed = Signal()

    def __init__(
        self,
        project: Project,
        settings: Settings,
        document: LectureDocument,
        pdf_options: PdfOptions,
        output_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Study pack")
        self.resize(580, 440)
        self._project = project
        self._settings = settings
        self._document = document
        self._pdf_options = pdf_options
        self._output = Path(output_dir) / study_pack_name(document.title)
        self._engine: TranslationEngine = engine_from_settings(settings)
        self._fingerprint = transcript_fingerprint(document.segments)
        self._thread: QThread | None = None
        self._worker: StudyPackWorker | None = None
        self._save_errors: list[str] = []

        intro = QLabel(
            "<b>One PDF to study from</b>, in this order:<br>"
            "1. <b>예습</b> — key points, terms, and the lecture explained simply<br>"
            "2. <b>강의 노트</b> — each slide with what was said<br>"
            f"3. <b>복습 퀴즈</b> — {QUIZ_LENGTH} questions in English, "
            "answers and explanations on the next page"
        )
        intro.setWordWrap(True)

        notice = QLabel(
            "The preview and quiz are written by the assistant from the transcript "
            "<b>text</b>. The video, the audio and the slide images are not sent."
        )
        notice.setWordWrap(True)
        notice_frame = QFrame()
        notice_layout = QVBoxLayout(notice_frame)
        notice_layout.setContentsMargins(12, 10, 12, 10)
        notice_layout.addWidget(notice)

        self._guide_label = QLabel()
        self._quiz_label = QLabel()
        self._stale_label = QLabel()
        self._stale_label.setWordWrap(True)
        set_role(self._stale_label, Role.WARNING)
        self._plan_label = QLabel()
        self._plan_label.setWordWrap(True)
        self._output_label = QLabel()
        self._output_label.setWordWrap(True)
        set_role(self._output_label, Role.MUTED)

        self._redo = QCheckBox("Write the preview and quiz again")
        self._redo.toggled.connect(self._refresh)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        set_role(self._detail, Role.MUTED)

        self._start_button = QPushButton("Create study pack")
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
        layout.addWidget(self._guide_label)
        layout.addWidget(self._quiz_label)
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
    def study(self) -> StudySet:
        return self._project.study

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def parts_to_make(self) -> tuple[str, ...]:
        """What the assistant would be asked for if the button were pressed now."""
        if self._redo.isChecked():
            return (PART_GUIDE, PART_QUIZ)
        missing = []
        if self.study.guide is None:
            missing.append(PART_GUIDE)
        if self.study.quiz is None:
            missing.append(PART_QUIZ)
        return tuple(missing)

    def _refresh(self) -> None:
        study = self.study
        self._guide_label.setText(
            f"예습 (preview): <b>ready</b> — made {_when(study.guide_made_at)}"
            if study.guide else "예습 (preview): not made yet"
        )
        self._quiz_label.setText(
            f"복습 퀴즈 (quiz): <b>ready</b> — made {_when(study.quiz_made_at)}"
            if study.quiz else "복습 퀴즈 (quiz): not made yet"
        )
        stale = study.stale_parts(self._fingerprint)
        self._stale_label.setVisible(bool(stale) and not self._redo.isChecked())
        if stale:
            self._stale_label.setText(
                "The transcript was edited after these were made. Tick the box below "
                "to write them again from the corrected transcript."
            )
        self._redo.setEnabled(not study.is_empty and not self.is_running)

        if not self._document.segments:
            self._plan_label.setText("There is no transcript yet. Transcribe the lecture first.")
            set_role(self._plan_label, Role.DANGER)
            self._start_button.setEnabled(False)
            return

        parts = self.parts_to_make()
        availability = self._engine.availability() if parts else None
        if availability is not None and not availability.ready:
            self._plan_label.setText(availability.remedy or "The assistant is not available.")
            set_role(self._plan_label, Role.DANGER)
            self._start_button.setEnabled(False)
            return

        if not parts:
            self._plan_label.setText(
                "Both are saved, so the PDF is made straight away — nothing is sent "
                "and no usage is spent."
            )
            self._start_button.setText("Create PDF")
        else:
            names = {PART_GUIDE: "the preview", PART_QUIZ: "the quiz"}
            asked = " and ".join(names[p] for p in parts)
            self._plan_label.setText(
                f"{self._engine.label} will be asked to write {asked}. "
                "Each is saved as soon as it arrives."
            )
            self._start_button.setText("Create study pack")
        set_role(self._plan_label, None)
        self._output_label.setText(f"Saves to: {self._output}")
        self._start_button.setEnabled(not self.is_running)

    # -- running ---------------------------------------------------------------

    def _start(self) -> None:
        if self.is_running:
            return
        parts = self.parts_to_make()
        if parts and not ensure_ai_consent(self, self._settings, self._engine.label):
            return

        # The worker builds the PDF from its own copy; parts it makes are
        # handed back through part_ready to be saved into the live project.
        snapshot = StudySet.from_dict(self.study.to_dict())
        if self._redo.isChecked():
            snapshot = StudySet()

        self._save_errors = []
        self._set_running(True)
        self._detail.setText("Starting...")
        self._worker = StudyPackWorker(
            self._document, self._engine, snapshot, parts, self._pdf_options, self._output
        )
        self._thread = QThread(self)
        self._worker.stage.connect(self._on_stage)
        self._worker.part_ready.connect(self._on_part)
        self._worker.finished.connect(self._on_finished)
        run_in_thread(self._worker, self._thread)

    def _set_running(self, running: bool) -> None:
        self._progress.setVisible(running)
        self._start_button.setEnabled(not running)
        self._stop_button.setEnabled(running)
        self._close_button.setEnabled(not running)
        self._redo.setEnabled(not running and not self.study.is_empty)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._stop_button.setEnabled(False)
            self._detail.setText("Stopping...")

    def _on_stage(self, stage: str) -> None:
        self._detail.setText(_STAGE_TEXT.get(stage, stage))

    def _on_part(self, part: str, value: object) -> None:
        """Save one finished part. Runs on the UI thread, which owns the project."""
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
        self._refresh()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(20000)
        self._thread = None
        self._worker = None
        self._set_running(False)
        self._detail.setText("")

    def _on_finished(self, result: StudyPackResult) -> None:
        self._teardown()
        self._redo.setChecked(False)
        self._refresh()
        self.show_result(result)

    def show_result(self, result: StudyPackResult) -> None:
        report = result.report
        lines: list[str] = []
        if result.pdf is not None:
            lines.append(f"Saved:\n{result.pdf}")
        if report.cancelled:
            lines.append("Stopped. Anything finished before stopping is saved.")
        lines += report.errors
        lines += self._save_errors
        if result.pdf_error:
            lines.append(result.pdf_error)
        if result.pdf is None and not report.cancelled and not self.study.is_complete:
            lines.append(
                "The PDF was not made because part of the material is missing. "
                "What did arrive is saved - press the button again to ask only "
                "for the rest."
            )

        box = QMessageBox(self)
        box.setWindowTitle("Study pack")
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
        if self.is_running:
            answer = QMessageBox.question(
                self, "Stop making the study pack?",
                "The study pack is being made. Stop?\n\n"
                "Anything already finished is saved either way.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self._worker is not None:
                # The dialog is going away: no result message after it has
                # closed. Parts still arriving are saved all the same.
                self._worker.finished.disconnect(self._on_finished)
            self._stop()
            self._teardown()
        super().reject()
