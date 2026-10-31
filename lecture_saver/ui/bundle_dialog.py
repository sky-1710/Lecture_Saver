"""Gather what Gemini Notebook needs for a video into one folder.

It says what will go in and what is missing, then writes the folder; the user
uploads it to the site. Nothing here sends anything, which is why this asks for
no agreement. It opens the site only when the user presses the button for it.
"""

from __future__ import annotations

from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import QThread, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..core.project import Project
from ..core.study import StudySet
from ..export.document import LectureDocument
from ..export.notebook_bundle import Bundle, bundle_folder_name, write_bundle
from . import theme
from .reference_row import ReferenceRow
from .theme import Role, set_role
from .workers import JobWorker, ProgressReport, run_in_thread

__all__ = ["BundleDialog", "NOTEBOOK_URL"]

#: Goes on to Gemini Notebook, the service's new name.
NOTEBOOK_URL = "https://notebooklm.google"


class BundleDialog(QDialog):
    """Say what goes in, then write the folder."""

    def __init__(
        self,
        project: Project,
        settings: Settings,
        document: LectureDocument,
        output_dir: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Files for Gemini Notebook")
        self.resize(560, 360)
        self._project = project
        self._document = document
        self._output_dir = Path(output_dir)
        self._thread: QThread | None = None
        self._worker: JobWorker | None = None

        intro = QLabel(
            "<b>Everything for a video, in one folder.</b> Upload the files to Gemini "
            "Notebook yourself and paste the text that comes with them. Lecture Saver "
            "sends nothing; what you upload goes to Google."
        )
        intro.setWordWrap(True)

        self._reference = ReferenceRow(
            project, settings,
            "No PDF attached.",
            "The professor's slides or notes for this lecture. A copy is kept with the "
            "lecture, so that it can go in the folder.",
        )
        self._reference.changed.connect(self._refresh)

        self._contents = QLabel()
        self._contents.setWordWrap(True)
        self._output_label = QLabel(f"Saves to: {self.folder}")
        self._output_label.setWordWrap(True)
        set_role(self._output_label, Role.MUTED)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setVisible(False)

        self._start_button = QPushButton("Create folder")
        self._start_button.setDefault(True)
        self._start_button.clicked.connect(self._start)
        self._close_button = QPushButton("Close")
        self._close_button.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        buttons.addWidget(self._start_button)
        buttons.addWidget(self._close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(intro)
        layout.addWidget(self._reference)
        layout.addWidget(self._contents)
        layout.addWidget(self._output_label)
        layout.addStretch(1)
        layout.addWidget(self._progress)
        layout.addLayout(buttons)
        self._refresh()

    @property
    def folder(self) -> Path:
        return self._output_dir / bundle_folder_name(self._document.title)

    @property
    def is_running(self) -> bool:
        return self._thread is not None

    def _deck_line(self) -> str:
        if self._project.reference_pdf is not None:
            return f"✓ 교안 PDF ({html_escape(self._project.reference.source)})"
        if self._project.reference.is_empty:
            return (
                f'<span style="color:{theme.WARNING_TEXT};">– 교안 PDF: attach it above '
                "to put it in</span>"
            )
        return (
            f'<span style="color:{theme.WARNING_TEXT};">– 교안 PDF: no copy was kept. '
            "Change PDF above to attach it again</span>"
        )

    def _preview_line(self) -> str:
        if self._project.study.guide is not None:
            return "✓ 예습 PDF"
        return (
            f'<span style="color:{theme.WARNING_TEXT};">– 예습 PDF: not made yet. Make it '
            "with Study → Preview to put it in</span>"
        )

    def _refresh(self) -> None:
        self._contents.setText(
            "<br>".join(
                [self._deck_line(), "✓ 전사문 (the edited transcript)", self._preview_line(),
                 "✓ 붙여넣을 문구"]
            )
        )
        reading = self._reference.is_reading
        self._reference.set_usable(not self.is_running)
        self._start_button.setEnabled(not self.is_running and not reading)
        self._close_button.setEnabled(not self.is_running and not reading)

    def _start(self) -> None:
        if self.is_running or self._reference.is_reading:
            return
        # Copies: the job must not touch the live project.
        document = self._document
        study = StudySet.from_dict(self._project.study.to_dict())
        deck = self._project.reference_pdf
        output = self._output_dir

        def job(_report: ProgressReport) -> object:
            return write_bundle(document, study, deck, output)

        self._thread = QThread(self)
        self._worker = JobWorker(job)
        self._worker.finished.connect(self._on_finished)
        self._progress.setVisible(True)
        self._refresh()
        run_in_thread(self._worker, self._thread)

    def _on_finished(self, result: object) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(20000)
        self._thread = self._worker = None
        self._progress.setVisible(False)
        self._refresh()
        if isinstance(result, Bundle):
            self.show_result(result)
        else:
            QMessageBox.warning(self, "Could not make the folder", str(result))

    def show_result(self, bundle: Bundle) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Files for Gemini Notebook")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"Saved:\n{bundle.folder}")
        open_folder = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
        copy_text = box.addButton("Copy the text to paste", QMessageBox.ButtonRole.ActionRole)
        open_site = box.addButton("Open Gemini Notebook", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        # The three can all be wanted: each leaves the box open for the next.
        for button in (open_folder, copy_text, open_site):
            button.clicked.disconnect()
        open_folder.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(bundle.folder)))
        )
        copy_text.clicked.connect(lambda: QApplication.clipboard().setText(bundle.request))
        open_site.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(NOTEBOOK_URL)))
        box.exec()

    def reject(self) -> None:
        if self.is_running or self._reference.is_reading:
            return  # a moment: both finish on their own
        super().reject()
