"""Attach the lecture's PDF: a line saying what is attached, and its buttons.

The Translate dialog and the study dialog share one PDF per lecture, so they
share this. The PDF is read off the UI thread. Its text, pictures of its pages
and a copy of the file are kept with the lecture, so the original can move.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QWidget,
)

from ..config import Settings
from ..core.project import REFERENCE_PDF_NAME, Project, ProjectError
from ..core.reference import ReferenceText
from ..slides.pdf_text import read_pdf_pages, render_pdf_pages
from .theme import Role, set_role
from .workers import JobWorker, ProgressReport, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["ReferenceRow"]

#: A deck is a few megabytes. Past this it is a textbook, and not worth a copy.
MAX_KEPT_PDF_BYTES = 200 * 1024 * 1024


class ReferenceRow(QWidget):
    """Says what is attached, and attaches, changes or removes it."""

    #: The attached PDF changed, or a read of one started or finished.
    changed = Signal()

    def __init__(
        self, project: Project, settings: Settings, nothing_attached: str, tooltip: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._project = project
        self._settings = settings
        self._nothing_attached = nothing_attached
        self._usable = True
        #: The PDF being read, while it is.
        self._reading: QThread | None = None
        self._reader: JobWorker | None = None
        self._reading_path: Path | None = None

        self.label = QLabel()
        self.label.setWordWrap(True)
        set_role(self.label, Role.MUTED)
        self.attach_button = QPushButton("Attach PDF...")
        self.attach_button.setToolTip(tooltip)
        self.attach_button.clicked.connect(self._choose)
        self.detach_button = QPushButton("Remove")
        self.detach_button.clicked.connect(self.remove)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.attach_button)
        layout.addWidget(self.detach_button)
        self.refresh()

    @property
    def is_reading(self) -> bool:
        return self._reading is not None

    def set_usable(self, usable: bool) -> None:
        """Whether the buttons work now: not during a run, nor with the PDF unused."""
        self._usable = usable
        self.refresh()

    def refresh(self) -> None:
        reference = self._project.reference
        if self.is_reading:
            text = "Reading the PDF..."
        elif reference.is_empty:
            text = self._nothing_attached
        else:
            pages = len(reference.pages)
            text = (
                f"{reference.source}: {pages} page{'s' if pages != 1 else ''}, "
                f"{reference.characters:,} characters of text."
            )
        self.label.setText(text)
        idle = self._usable and not self.is_reading
        self.attach_button.setText("Attach PDF..." if reference.is_empty else "Change PDF...")
        self.attach_button.setEnabled(idle)
        self.detach_button.setEnabled(idle and not reference.is_empty)

    @property
    def _incoming(self) -> Path:
        """Where the pictures are drawn, until the PDF has been read whole."""
        return self._project.root / "reference_pages.new"

    def _choose(self) -> None:
        start = str(self._settings.get("last_pdf_dir", str(Path.home())))
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the lecture's PDF", start, "PDF (*.pdf)"
        )
        if path:
            self._settings.set("last_pdf_dir", str(Path(path).parent))
            self.read(Path(path))

    def read(self, path: Path) -> None:
        """Read the PDF in the background: a textbook takes a moment (§26)."""
        if self.is_reading:
            return

        incoming = self._incoming
        shutil.rmtree(incoming, ignore_errors=True)

        def job(_report: ProgressReport) -> object:
            pages = read_pdf_pages(path)
            render_pdf_pages(path, incoming)
            if path.stat().st_size <= MAX_KEPT_PDF_BYTES:
                shutil.copyfile(path, incoming / REFERENCE_PDF_NAME)
            return pages

        self._reading_path = path
        self._reading = QThread(self)
        self._reader = JobWorker(job)
        self._reader.finished.connect(self._on_read)
        run_in_thread(self._reader, self._reading)
        self.refresh()
        self.changed.emit()

    def _on_read(self, result: object) -> None:
        if self._reading is not None:
            self._reading.quit()
            self._reading.wait(20000)
        name = self._reading_path.name if self._reading_path else ""
        self._reading = self._reader = self._reading_path = None
        problem = str(result) if isinstance(result, Exception) else ""
        if isinstance(result, tuple):
            try:
                self._project.set_reference(
                    ReferenceText(source=name, pages=result), pictures=self._incoming
                )
            except ProjectError as exc:
                problem = str(exc)
        shutil.rmtree(self._incoming, ignore_errors=True)
        if problem:
            QMessageBox.warning(self, "Could not use the PDF", problem)
        self.refresh()
        self.changed.emit()

    def remove(self) -> None:
        try:
            self._project.clear_reference()
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not remove the PDF", str(exc))
        self.refresh()
        self.changed.emit()
