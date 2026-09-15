"""The export screen (§23).

Collects an :class:`ExportSettings`, runs the export off the UI thread, and
reports exactly what was written where - or which format failed and why.
The choices are stored in the project, so they are the defaults next time.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..core.project import Project, ProjectError
from ..export.document import LectureDocument, build_document
from ..export.exporter import ExportResult
from ..export.options import (
    FILE_NAMES,
    PDF_PRESETS,
    ExportFormat,
    ExportSettings,
    PdfOptions,
    preset_name_for,
)
from .theme import Role, set_role
from .workers import ExportWorker, run_in_thread

log = logging.getLogger(__name__)

__all__ = ["ExportDialog"]

_CUSTOM = "Custom"


class ExportDialog(QDialog):
    """Choose outputs and options, export, and show the result."""

    #: Emitted after an export finishes, with the result.
    exported = Signal(object)

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")
        self.setMinimumWidth(560)
        self._project = project
        self._thread: QThread | None = None
        self._worker: ExportWorker | None = None
        self._last_result: ExportResult | None = None
        self._syncing_preset = False

        settings = ExportSettings.from_dict(project.meta.export_settings)
        self._output_dir = (
            Path(settings.output_dir) if settings.output_dir else project.exports_dir
        )
        self._build(settings)
        self._update_availability()

    # -- construction ---------------------------------------------------------

    def _build(self, settings: ExportSettings) -> None:
        # Outputs
        outputs = QGroupBox("Output")
        outputs_layout = QGridLayout(outputs)
        outputs_layout.setVerticalSpacing(6)
        outputs_layout.setColumnMinimumWidth(0, 230)
        outputs_layout.setColumnStretch(1, 1)
        self._format_checks: dict[ExportFormat, QCheckBox] = {}
        for row, export_format in enumerate(ExportFormat):
            check = QCheckBox(export_format.label)
            check.setChecked(export_format in settings.formats)
            check.toggled.connect(self._update_availability)
            file_label = QLabel(FILE_NAMES[export_format])
            set_role(file_label, Role.SUBTLE)
            outputs_layout.addWidget(check, row, 0)
            outputs_layout.addWidget(file_label, row, 1)
            self._format_checks[export_format] = check

        # Transcript timestamps (TXT and Markdown)
        stamps = QGroupBox("Transcript timestamps (TXT and Markdown)")
        stamps_layout = QHBoxLayout(stamps)
        self._stamps_include = QRadioButton("Include")
        self._stamps_exclude = QRadioButton("Exclude")
        group = QButtonGroup(self)
        group.addButton(self._stamps_include)
        group.addButton(self._stamps_exclude)
        (self._stamps_include if settings.include_timestamps else self._stamps_exclude).setChecked(
            True
        )
        stamps_layout.addWidget(self._stamps_include)
        stamps_layout.addWidget(self._stamps_exclude)
        stamps_layout.addStretch(1)
        self._stamps_box = stamps

        # PDF options
        pdf = QGroupBox("PDF options")
        pdf_layout = QVBoxLayout(pdf)
        pdf_layout.setSpacing(7)
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        preset_row.addWidget(QLabel("Preset:"))
        self._preset = QComboBox()
        self._preset.addItems([*PDF_PRESETS.keys(), _CUSTOM])
        preset_row.addWidget(self._preset, 1)
        pdf_layout.addLayout(preset_row)
        pdf_layout.addSpacing(2)

        self._pdf_checks = {
            "slide_image": QCheckBox("Slide image"),
            "slide_number": QCheckBox("Slide number"),
            "time_range": QCheckBox("Slide time range"),
            "transcript": QCheckBox("Transcript"),
            "segment_timestamps": QCheckBox("Transcript timestamps"),
            "translation": QCheckBox("Korean translation (when imported)"),
        }
        for name, check in self._pdf_checks.items():
            check.setChecked(getattr(settings.pdf, name))
            check.toggled.connect(self._on_pdf_option_changed)
            pdf_layout.addWidget(check)
        self._slides_note = QLabel(
            "This lecture has no detected slides yet, so the PDF contains the "
            "transcript only."
        )
        self._slides_note.setWordWrap(True)
        set_role(self._slides_note, Role.WARNING)
        pdf_layout.addWidget(self._slides_note)
        self._pdf_box = pdf
        self._preset.currentTextChanged.connect(self._on_preset_chosen)
        self._sync_preset()

        # Destination
        destination = QGroupBox("Save to")
        destination_layout = QHBoxLayout(destination)
        destination_layout.setSpacing(8)
        self._folder_label = QLabel()
        self._folder_label.setMinimumWidth(220)
        self._folder_label.setToolTip(str(self._output_dir))
        set_role(self._folder_label, Role.MUTED)
        change = QPushButton("Change...")
        change.clicked.connect(self._choose_folder)
        reset = QPushButton("Project folder")
        reset.setToolTip("Use the exports folder inside the lecture project.")
        reset.clicked.connect(self._use_project_folder)
        destination_layout.addWidget(self._folder_label, 1)
        destination_layout.addWidget(change)
        destination_layout.addWidget(reset)

        # Result and buttons
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._open_folder = QPushButton("Open folder")
        self._open_folder.setVisible(False)
        self._open_folder.clicked.connect(self._reveal)
        self._export_button = QPushButton("Export")
        self._export_button.setDefault(True)
        self._export_button.clicked.connect(self.start_export)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self._open_folder)
        buttons.addStretch(1)
        buttons.addWidget(self._export_button)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(outputs)
        layout.addWidget(stamps)
        layout.addWidget(pdf)
        layout.addWidget(destination)
        layout.addWidget(self._status)
        layout.addLayout(buttons)
        self._show_output_dir()

    # -- settings -------------------------------------------------------------

    def settings(self) -> ExportSettings:
        project_exports = self._project.exports_dir
        return ExportSettings(
            formats=[f for f, check in self._format_checks.items() if check.isChecked()],
            include_timestamps=self._stamps_include.isChecked(),
            pdf=self.pdf_options(),
            output_dir=None if self._output_dir == project_exports else str(self._output_dir),
        )

    def pdf_options(self) -> PdfOptions:
        return PdfOptions(**{name: check.isChecked() for name, check in self._pdf_checks.items()})

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def set_output_dir(self, path: Path) -> None:
        self._output_dir = Path(path)
        self._show_output_dir()

    def _show_output_dir(self) -> None:
        """Show the destination, shortened in the middle if it is a long path."""
        text = str(self._output_dir)
        if len(text) > 58:
            text = f"{text[:26]}...{text[-29:]}"
        self._folder_label.setText(text)
        self._folder_label.setToolTip(str(self._output_dir))

    def _on_preset_chosen(self, name: str) -> None:
        if self._syncing_preset or name not in PDF_PRESETS:
            return
        preset = PDF_PRESETS[name]
        self._syncing_preset = True
        try:
            for option, check in self._pdf_checks.items():
                check.setChecked(getattr(preset, option))
        finally:
            self._syncing_preset = False
        self._update_availability()

    def _on_pdf_option_changed(self) -> None:
        if not self._syncing_preset:
            self._sync_preset()
        self._update_availability()

    def _sync_preset(self) -> None:
        self._syncing_preset = True
        try:
            self._preset.setCurrentText(preset_name_for(self.pdf_options()) or _CUSTOM)
        finally:
            self._syncing_preset = False

    def _update_availability(self) -> None:
        pdf_on = self._format_checks[ExportFormat.PDF].isChecked()
        self._pdf_box.setEnabled(pdf_on)
        stamps_on = any(
            self._format_checks[f].isChecked() for f in (ExportFormat.TXT, ExportFormat.MARKDOWN)
        )
        self._stamps_box.setEnabled(stamps_on)
        self._pdf_checks["segment_timestamps"].setEnabled(
            self._pdf_checks["transcript"].isChecked()
        )
        self._slides_note.setVisible(not self._project.has_slides)

        chosen = any(check.isChecked() for check in self._format_checks.values())
        usable = not (pdf_on and self.pdf_options().is_empty)
        self._export_button.setEnabled(chosen and usable and self._thread is None)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Export to", str(self._output_dir))
        if path:
            self.set_output_dir(Path(path))

    def _use_project_folder(self) -> None:
        self.set_output_dir(self._project.exports_dir)

    # -- running ---------------------------------------------------------------

    def start_export(self) -> None:
        """Snapshot the lecture and export it on a background thread."""
        if self._thread is not None:
            return
        settings = self.settings()
        self._remember(settings)
        document = build_document(self._project)
        self._set_running(True)

        self._thread = QThread(self)
        self._worker = ExportWorker(document, settings, self._output_dir)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        run_in_thread(self._worker, self._thread)

    def export_now(self) -> ExportResult:
        """Run the export on the calling thread. For tests and scripting."""
        from ..export.exporter import run_export

        settings = self.settings()
        self._remember(settings)
        document: LectureDocument = build_document(self._project)
        result = run_export(document, settings, self._output_dir)
        self._show_result(result)
        return result

    def _remember(self, settings: ExportSettings) -> None:
        self._project.meta.export_settings = settings.to_dict()
        try:
            self._project.save_meta()
        except ProjectError:
            log.warning("Could not save export settings", exc_info=True)

    def _set_running(self, running: bool) -> None:
        self._export_button.setEnabled(not running)
        self._open_folder.setVisible(False)
        if running:
            set_role(self._status, None)
            self._status.setText("Exporting...")

    def _on_progress(self, label: str, position: int, total: int) -> None:
        self._status.setText(f"Exporting {label} ({position + 1} of {total})...")

    def _on_finished(self, result: object) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(15000)
        self._thread = None
        self._worker = None
        assert isinstance(result, ExportResult)
        self._show_result(result)
        self._update_availability()

    def _show_result(self, result: ExportResult) -> None:
        self._last_result = result
        lines = []
        if result.written:
            lines.append(f"Saved to {result.output_dir}:")
            lines += [f"  • {path.name}" for path in result.written]
        for export_format, message in result.failures:
            lines.append(f"{export_format.label} failed: {message}")
        self._status.setText("\n".join(lines))
        set_role(self._status, None if result.ok else Role.DANGER)
        self._open_folder.setVisible(bool(result.written))
        self.exported.emit(result)

    @property
    def result_text(self) -> str:
        return self._status.text()

    def _reveal(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._output_dir)))

    def reject(self) -> None:
        if self._thread is not None:
            # Let a running export finish writing rather than leave half a file.
            self._thread.quit()
            self._thread.wait(30000)
        super().reject()
