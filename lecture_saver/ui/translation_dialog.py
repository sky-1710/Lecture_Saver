"""Bring an AI assistant's translation back into the lecture.

Paste the answer, or load it from a file, and see before importing which slides
it matches. Parts of a long answer can be imported one after another; each adds
to what is already there. Nothing is sent anywhere - the text is only read.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.project import Project, ProjectError
from .theme import Role, set_role

log = logging.getLogger(__name__)

__all__ = ["TranslationDialog", "describe_numbers"]


def describe_numbers(numbers: tuple[int, ...] | list[int]) -> str:
    """``1, 2, 3, 5, 7, 8`` -> ``1-3, 5, 7-8``."""
    values = sorted(set(numbers))
    ranges: list[str] = []
    start = previous = None
    for value in values:
        if start is None:
            start = previous = value
        elif previous is not None and value == previous + 1:
            previous = value
        else:
            ranges.append(f"{start}-{previous}" if start != previous else f"{start}")
            start = previous = value
    if start is not None:
        ranges.append(f"{start}-{previous}" if start != previous else f"{start}")
    return ", ".join(ranges)


class TranslationDialog(QDialog):
    """Paste or load an answer, preview the matching, import."""

    #: Emitted after translations were imported or removed.
    changed = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Import translation")
        self.resize(640, 560)
        self._project = project
        self._source = ""

        intro = QLabel(
            "Paste the AI assistant's answer to the translation request below, or "
            "load it from a file. Each <b>## Slide N</b> section is attached to that "
            "slide. An answer that came in several parts can be imported part by part."
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("## Slide 1\n(translation)\n\n## Slide 2\n(translation)")
        self._preview = QLabel("")
        self._preview.setWordWrap(True)
        self._existing = QLabel("")
        set_role(self._existing, Role.MUTED)

        load = QPushButton("Load file...")
        load.clicked.connect(self._load_file)
        self._clear_button = QPushButton("Remove imported translations")
        self._clear_button.clicked.connect(self._clear)
        self._import_button = QPushButton("Import")
        self._import_button.setDefault(True)
        self._import_button.clicked.connect(self.import_text)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(load)
        buttons.addWidget(self._clear_button)
        buttons.addStretch(1)
        buttons.addWidget(self._import_button)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(intro)
        layout.addWidget(self._text, 1)
        layout.addWidget(self._preview)
        layout.addWidget(self._existing)
        layout.addLayout(buttons)

        # Re-match shortly after typing stops rather than on every keystroke.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self.update_preview)
        self._text.textChanged.connect(self._debounce.start)

        self.update_preview()

    # -- text -----------------------------------------------------------------

    def set_text(self, text: str, *, source: str = "") -> None:
        self._source = source
        self._text.setPlainText(text)
        self.update_preview()

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load the translated answer", str(self._project.exports_dir),
            "Text (*.md *.txt);;All files (*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            QMessageBox.warning(self, "Could not read the file", str(exc))
            return
        self.set_text(text, source=Path(path).name)

    # -- preview and import -----------------------------------------------------------

    @property
    def preview_text(self) -> str:
        return self._preview.text()

    def update_preview(self) -> None:
        self._debounce.stop()
        total = len(self._project.slides.appearances)
        existing = sum(
            1 for a in self._project.slides.appearances
            if self._project.translations.for_appearance(a)
        )
        self._existing.setText(
            f"{existing} of {total} slides already have a translation." if existing else ""
        )
        self._clear_button.setEnabled(not self._project.translations.is_empty)

        text = self._text.toPlainText()
        if not total:
            self._preview.setText("Detect slides first: translations are attached to slides.")
            self._import_button.setEnabled(False)
            return
        if not text.strip():
            self._preview.setText("")
            self._import_button.setEnabled(False)
            return

        result = self._project.preview_translation(text)
        if not result.matched:
            self._preview.setText(
                "No “Slide N” sections that match this lecture were found. The "
                "answer should have a heading such as “## Slide 3” before each "
                "slide's translation."
            )
            set_role(self._preview, Role.DANGER)
            self._import_button.setEnabled(False)
            return

        lines = [
            f"Translations for {len(result.matched)} of {total} slides: "
            f"{describe_numbers([m.appearance_id for m in result.matched])}."
        ]
        if result.unknown:
            lines.append(
                f"Not in this lecture, will be skipped: Slide {describe_numbers(result.unknown)}."
            )
        if result.missing:
            lines.append(f"Not translated in this text: {describe_numbers(result.missing)}.")
        self._preview.setText("\n".join(lines))
        set_role(self._preview, None)
        self._import_button.setEnabled(True)

    def import_text(self) -> bool:
        text = self._text.toPlainText()
        try:
            result = self._project.import_translation(text, source=self._source)
        except ProjectError as exc:
            QMessageBox.critical(self, "Could not save the translation", str(exc))
            return False
        if not result.matched:
            return False
        self.changed.emit()
        self._text.clear()
        self._preview.setText(
            f"Imported translations for {len(result.matched)} slides "
            f"({describe_numbers([m.appearance_id for m in result.matched])}). "
            "Paste the next part, or close."
        )
        set_role(self._preview, Role.SUCCESS)
        self._existing.setText("")
        self._clear_button.setEnabled(True)
        return True

    def _clear(self) -> None:
        answer = QMessageBox.question(
            self, "Remove translations?",
            "Remove every imported translation from this lecture?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self._project.clear_translations()
        except ProjectError as exc:
            QMessageBox.critical(self, "Could not remove the translations", str(exc))
            return
        self.changed.emit()
        self.update_preview()
