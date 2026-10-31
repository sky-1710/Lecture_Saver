"""Run the translation here, instead of carrying it to an assistant by hand.

This is the one screen in Lecture Saver that sends lecture content off the
computer, so it says so before it does: the first run asks for agreement in
plain words, and the panel at the top states what is sent and what is not
every time thereafter.

Each batch is saved as it arrives, on this thread. A run that stops - the
usage limit, a dropped connection, the Stop button - keeps everything already
translated, and starting again picks up from the first untranslated slide.

A run can also report what the transcription got wrong, judged against the
text of the lecture's PDF when one is attached. That text is sent too, so the
panel at the top says so whenever it would be. What comes back is suggestions:
nothing in the transcript changes until the user accepts them.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
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
from ..core.reference import ReferenceText
from ..slides.pdf_text import read_pdf_pages
from ..translate import engine_from_settings
from ..translate.engine import TranslationEngine
from .ai_consent import consent_text, ensure_ai_consent
from .theme import Role, set_role
from .workers import JobWorker, ProgressReport, TranslationWorker, run_in_thread

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
        self.resize(560, 460)
        self._project = project
        self._settings = settings
        self._engine: TranslationEngine = engine_from_settings(settings)
        self._thread: QThread | None = None
        self._worker: TranslationWorker | None = None
        self._saved = 0
        self._fixes = 0
        self._save_errors: list[str] = []
        #: The PDF being read, while it is.
        self._reading: QThread | None = None
        self._reader: JobWorker | None = None
        self._reading_path: Path | None = None

        self._notice = QLabel()
        self._notice.setWordWrap(True)
        self._notice.setTextFormat(Qt.TextFormat.RichText)
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

        self._check_typos = QCheckBox("Also look for words the transcription got wrong")
        self._check_typos.setChecked(settings.translation_checks_typos)
        self._check_typos.setToolTip(
            "The assistant reports what looks misheard, and the translation "
            "follows what was meant.\nThey are suggestions on the Typos tab: the "
            "transcript does not change until you accept one."
        )
        self._check_typos.toggled.connect(self._on_check_typos)

        self._reference_label = QLabel()
        self._reference_label.setWordWrap(True)
        set_role(self._reference_label, Role.MUTED)
        self._attach_button = QPushButton("Attach PDF...")
        self._attach_button.setToolTip(
            "The professor's slides or notes for this lecture. Their text is what "
            "typos are judged against, and is kept with the lecture."
        )
        self._attach_button.clicked.connect(self._choose_reference)
        self._detach_button = QPushButton("Remove")
        self._detach_button.clicked.connect(self._remove_reference)
        reference_row = QHBoxLayout()
        reference_row.setContentsMargins(24, 0, 0, 0)
        reference_row.setSpacing(8)
        reference_row.addWidget(self._reference_label, 1)
        reference_row.addWidget(self._attach_button)
        reference_row.addWidget(self._detach_button)

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
        layout.addWidget(self._check_typos)
        layout.addLayout(reference_row)
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

    @property
    def _sends_reference(self) -> bool:
        return self._check_typos.isChecked() and not self._project.reference.is_empty

    def _update_notice(self) -> None:
        """Say what a run would send, now, not what it usually sends."""
        if self._sends_reference:
            self._notice.setText(
                "The transcript <b>text</b> and the <b>text of the attached PDF</b> "
                "are sent to the assistant. The video, the audio and the slide "
                "images are not sent."
            )
        else:
            self._notice.setText(
                "The transcript <b>text</b> is sent to the assistant to be "
                "translated. The video, the audio and the slide images are not sent."
            )

    def _update_reference(self) -> None:
        reference = self._project.reference
        if self._reading is not None:
            text = "Reading the PDF..."
        elif reference.is_empty:
            text = "No PDF attached: words are judged from the lecture alone."
        else:
            pages = len(reference.pages)
            text = (
                f"{reference.source}: {pages} page{'s' if pages != 1 else ''}, "
                f"{reference.characters:,} characters of text."
            )
        self._reference_label.setText(text)
        checking = self._check_typos.isChecked() and not self.is_running
        idle = checking and self._reading is None
        self._reference_label.setEnabled(self._check_typos.isChecked())
        self._attach_button.setText("Attach PDF..." if reference.is_empty else "Change PDF...")
        self._attach_button.setEnabled(idle)
        self._detach_button.setEnabled(idle and not reference.is_empty)

    def _refresh(self) -> None:
        self._update_notice()
        self._update_reference()
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
            again = ", which checks them for typos too" if self._check_typos.isChecked() else ""
            self._status.setText(
                "Every slide with speech already has a translation. Tick the box "
                f"below to translate them again{again}."
                if done else "There is no transcript to translate yet."
            )
            set_role(self._status, Role.MUTED)
            self._start_button.setEnabled(False)

    # -- the lecture's PDF -------------------------------------------------------

    def _on_check_typos(self, checked: bool) -> None:
        self._settings.translation_checks_typos = checked
        self._refresh()

    def _choose_reference(self) -> None:
        start = str(self._settings.get("last_pdf_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the lecture's PDF", start, "PDF (*.pdf)"
        )
        if path:
            self._settings.set("last_pdf_dir", str(Path(path).parent))
            self.read_reference(Path(path))

    def read_reference(self, path: Path) -> None:
        """Read the PDF in the background: a textbook takes a moment (§26)."""
        if self._reading is not None or self.is_running:
            return

        def job(_report: ProgressReport) -> object:
            return read_pdf_pages(path)

        self._reading_path = path
        self._reading = QThread(self)
        self._reader = JobWorker(job)
        self._reader.finished.connect(self._on_reference_read)
        self._start_button.setEnabled(False)
        self._close_button.setEnabled(False)
        run_in_thread(self._reader, self._reading)
        self._update_reference()

    def _on_reference_read(self, result: object) -> None:
        if self._reading is not None:
            self._reading.quit()
            self._reading.wait(20000)
        name = self._reading_path.name if self._reading_path else ""
        self._reading = self._reader = self._reading_path = None
        self._close_button.setEnabled(True)
        problem = str(result) if isinstance(result, Exception) else ""
        if isinstance(result, tuple):
            try:
                self._project.set_reference(ReferenceText(source=name, pages=result))
            except ProjectError as exc:
                problem = str(exc)
        if problem:
            QMessageBox.warning(self, "Could not use the PDF", problem)
        self._refresh()

    def _remove_reference(self) -> None:
        try:
            self._project.clear_reference()
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not remove the PDF", str(exc))
        self._refresh()

    # -- running ---------------------------------------------------------------

    def _agreed(self) -> bool:
        return ensure_ai_consent(self, self._settings, self._engine.label)

    def _start(self) -> None:
        if self.is_running or self._reading is not None or not self._agreed():
            return
        from ..export.document import build_document

        skip = set() if self._redo.isChecked() else self._translated_numbers()
        # A snapshot: the worker reads this while the user may edit the live
        # transcript in the window behind.
        document = build_document(self._project)

        self._saved = 0
        self._fixes = 0
        self._save_errors = []
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        self._redo.setEnabled(False)
        self._check_typos.setEnabled(False)
        self._attach_button.setEnabled(False)
        self._detach_button.setEnabled(False)
        self._close_button.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._detail.setText("Starting...")

        self._worker = TranslationWorker(
            document, self._engine, skip=skip,
            check_typos=self._check_typos.isChecked(),
            reference=self._project.reference,
        )
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
        self._fixes += len(result.corrections)
        if result.matched or result.corrections:
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
        self._check_typos.setEnabled(True)
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
        if self._fixes:
            lines.append(
                f"{self._fixes} possible typo{'s' if self._fixes != 1 else ''} to look "
                "through on the Typos tab. The transcript has not been changed."
            )
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
        if self._reading is not None:
            return  # a moment: the PDF is saved to this lecture when it is read
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
