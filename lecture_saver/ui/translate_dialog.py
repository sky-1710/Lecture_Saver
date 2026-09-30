"""Run the translation here, instead of carrying it to an assistant by hand.

This is the one screen in Lecture Saver that sends lecture content off the
computer, so it says so before it does: the first run asks for agreement in
plain words, and the panel at the top states what is sent and what is not
every time thereafter.

Each batch is saved as it arrives, on this thread. A run that stops - the
usage limit, a dropped connection, the Stop button - keeps everything already
translated, and starting again picks up from the first untranslated slide.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QThread, Signal
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
from ..translate import engine_from_settings
from ..translate.engine import TranslationEngine
from .ai_consent import consent_text, ensure_ai_consent
from .theme import Role, set_role
from .workers import TranslationWorker, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["TranslateDialog", "consent_text"]


class TranslateDialog(QDialog):
    """Pick up where the last run stopped, then run it."""

    #: Emitted after any batch was saved, so the lecture view redraws.
    changed = Signal()

    def __init__(
        self, project: Project, settings: Settings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Translate with AI")
        self.resize(560, 400)
        self._project = project
        self._settings = settings
        self._engine: TranslationEngine = engine_from_settings(settings)
        self._thread: QThread | None = None
        self._worker: TranslationWorker | None = None
        self._saved = 0
        self._save_errors: list[str] = []

        self._notice = QLabel()
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.RichText)
        self._notice.setText(
            "The transcript <b>text</b> is sent to the assistant to be translated. "
            "The video, the audio and the slide images are not sent."
        )
        notice_frame = QFrame()
        notice_layout = QVBoxLayout(notice_frame)
        notice_layout.setContentsMargins(12, 10, 12, 10)
        notice_layout.addWidget(self._notice)

        self._engine_label = QLabel()
        self._engine_label.setWordWrap(True)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._detail = QLabel("")
        self._detail.setWordWrap(True)
        set_role(self._detail, Role.MUTED)

        self._redo = QCheckBox("Translate slides that already have a translation again")
        self._redo.toggled.connect(self._refresh)

        self._progress = QProgressBar()
        self._progress.setVisible(False)

        self._start_button = QPushButton("Translate")
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
        layout.addWidget(notice_frame)
        layout.addWidget(self._engine_label)
        layout.addWidget(self._status)
        layout.addWidget(self._redo)
        layout.addStretch(1)
        layout.addWidget(self._progress)
        layout.addWidget(self._detail)
        layout.addLayout(buttons)

        self._refresh()

    # -- state ----------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def _translated_numbers(self) -> set[int]:
        """Slides that already have a translation covering their interval."""
        return {
            appearance.appearance_id
            for appearance in self._project.slides.appearances
            if self._project.translations.for_appearance(appearance)
        }

    def _pending(self) -> list[int]:
        """The slide numbers this run would send."""
        from ..export.document import build_document
        from ..translate.batching import translatable_sections

        skip = set() if self._redo.isChecked() else self._translated_numbers()
        document = build_document(self._project)
        sections = translatable_sections(document, skip=skip)
        return [s.number for s in sections if s.number is not None]

    def _refresh(self) -> None:
        availability = self._engine.availability()
        self._engine_label.setText(
            f"<b>{self._engine.label}</b><br>{availability.detail}"
        )
        if not availability.ready:
            self._status.setText(availability.remedy or "This assistant is not available.")
            set_role(self._status, Role.DANGER)
            self._start_button.setEnabled(False)
            return

        total = len(self._project.slides.appearances)
        if not total:
            self._status.setText(
                "Detect slides first: a translation is stored against the slide it "
                "belongs to."
            )
            set_role(self._status, Role.DANGER)
            self._start_button.setEnabled(False)
            self._redo.setEnabled(False)
            return

        done = len(self._translated_numbers())
        pending = len(self._pending())
        self._redo.setEnabled(done > 0)
        if pending:
            already = f", {done} already translated" if done and not self._redo.isChecked() else ""
            self._status.setText(
                f"{pending} of {total} slides will be translated{already}."
            )
            set_role(self._status, None)
            self._start_button.setEnabled(True)
        else:
            self._status.setText(
                "Every slide with speech already has a translation. Tick the box "
                "above to translate them again."
                if done else "There is no transcript to translate yet."
            )
            set_role(self._status, Role.MUTED)
            self._start_button.setEnabled(False)

    # -- running ---------------------------------------------------------------

    def _agreed(self) -> bool:
        return ensure_ai_consent(self, self._settings, self._engine.label)

    def _start(self) -> None:
        if self.is_running or not self._agreed():
            return
        from ..export.document import build_document

        skip = set() if self._redo.isChecked() else self._translated_numbers()
        # A snapshot: the worker reads this while the user may edit the live
        # transcript in the window behind.
        document = build_document(self._project)

        self._saved = 0
        self._save_errors = []
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._redo.setEnabled(False)
        self._close_button.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._detail.setText("Starting...")

        self._worker = TranslationWorker(document, self._engine, skip=skip)
        self._thread = QThread(self)
        self._worker.batch_ready.connect(self._on_batch)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        run_in_thread(self._worker, self._thread)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._stop_button.setEnabled(False)
            self._detail.setText("Stopping after the batch in flight...")

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        if done < total:
            self._detail.setText(f"Translating batch {done + 1} of {total}...")

    def _on_batch(self, outcome: object) -> None:
        """Save one answer. Runs on the UI thread, which owns the project."""
        answer = getattr(outcome, "answer", "")
        if not answer:
            return
        try:
            result = self._project.import_translation(answer, source=self._engine.key)
        except ProjectError as exc:
            log.warning("Could not save a translated batch: %s", exc)
            self._save_errors.append(str(exc))
            return
        self._saved += len(result.matched)
        if result.matched:
            self.changed.emit()

    def _on_failed(self, message: str) -> None:
        self._teardown()
        QMessageBox.critical(self, "Translation failed", message)
        self._refresh()

    def _on_finished(self, report: object) -> None:
        self._teardown()
        self._refresh()
        self._show_report(report)

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(20000)
        self._thread = None
        self._worker = None
        self._progress.setVisible(False)
        self._stop_button.setEnabled(False)
        self._close_button.setEnabled(True)
        self._detail.setText("")

    def _show_report(self, report: object) -> None:
        translated = list(getattr(report, "translated", []))
        missing = list(getattr(report, "missing", []))
        errors = list(getattr(report, "errors", []))
        cancelled = bool(getattr(report, "cancelled", False))

        lines = []
        if self._saved:
            lines.append(f"{self._saved} slides translated and saved.")
        elif not translated:
            lines.append("No slides were translated.")
        if cancelled:
            lines.append("Stopped. What was translated before stopping is saved.")
        if missing:
            from .translation_dialog import describe_numbers

            lines.append(
                f"The assistant did not return: slides {describe_numbers(missing)}. "
                "Run the translation again to try those."
            )
        lines += self._save_errors
        lines += errors

        box = QMessageBox(self)
        box.setWindowTitle("Translation")
        box.setIcon(
            QMessageBox.Icon.Warning if (errors or missing) else QMessageBox.Icon.Information
        )
        box.setText("\n\n".join(lines) or "Nothing to do.")
        box.exec()

    # -- closing ----------------------------------------------------------------

    def reject(self) -> None:
        if self.is_running:
            answer = QMessageBox.question(
                self, "Stop the translation?",
                "A translation is running. Stop it?\n\n"
                "Slides already translated are saved either way.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._stop()
            if self._thread is not None:
                self._thread.quit()
                self._thread.wait(20000)
            self._teardown()
        super().reject()
