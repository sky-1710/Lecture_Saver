"""Find & replace bar for the transcript (§13).

A thin strip above the transcript, like an editor's find bar rather than a
dialog, so the matches stay visible while stepping through them. It only
collects what the user wants; searching and replacing happen in the
:class:`~lecture_saver.core.transcript_store.TranscriptStore`.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QToolButton,
    QWidget,
)

from .theme import Role, set_flag, set_role

__all__ = ["FindBar"]


class FindBar(QFrame):
    """Find field, replace field, and the buttons that act on them."""

    #: The query or its case sensitivity changed.
    searchChanged = Signal()
    nextRequested = Signal()
    previousRequested = Signal()
    replaceRequested = Signal()
    replaceAllRequested = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        set_flag(self, "rule", "bottom")

        self._find = QLineEdit()
        self._find.setPlaceholderText("Find in transcript")
        self._find.setClearButtonEnabled(True)
        self._replace = QLineEdit()
        self._replace.setPlaceholderText("Replace with")

        self._case = QCheckBox("Match case")
        self._count = QLabel("")
        set_role(self._count, Role.MUTED)
        self._count.setMinimumWidth(60)

        self._previous = QToolButton()
        self._previous.setText("▲")
        self._previous.setToolTip("Previous match (Shift+Enter)")
        self._next = QToolButton()
        self._next.setText("▼")
        self._next.setToolTip("Next match (Enter)")
        self._close = QToolButton()
        self._close.setText("✕")
        self._close.setToolTip("Close (Esc)")

        self._replace_button = QPushButton("Replace")
        self._replace_all_button = QPushButton("Replace all")

        find_row = QHBoxLayout()
        find_row.setSpacing(4)
        find_row.addWidget(self._find, 1)
        find_row.addWidget(self._previous)
        find_row.addWidget(self._next)
        find_row.addWidget(self._close)

        replace_row = QHBoxLayout()
        replace_row.setSpacing(4)
        replace_row.addWidget(self._replace, 1)
        replace_row.addWidget(self._replace_button)
        replace_row.addWidget(self._replace_all_button)

        options_row = QHBoxLayout()
        options_row.addWidget(self._case)
        options_row.addStretch(1)
        options_row.addWidget(self._count)

        layout = QGridLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setVerticalSpacing(6)
        layout.addLayout(find_row, 0, 0)
        layout.addLayout(replace_row, 1, 0)
        layout.addLayout(options_row, 2, 0)

        # Shift+Enter must step backwards; QLineEdit would report it as Enter.
        self._find.installEventFilter(self)

        self._find.textChanged.connect(self.searchChanged)
        self._case.toggled.connect(self.searchChanged)
        self._find.returnPressed.connect(self.nextRequested)
        self._replace.returnPressed.connect(self.replaceRequested)
        self._next.clicked.connect(self.nextRequested)
        self._previous.clicked.connect(self.previousRequested)
        self._replace_button.clicked.connect(self.replaceRequested)
        self._replace_all_button.clicked.connect(self.replaceAllRequested)
        self._close.clicked.connect(self.close_bar)

        self.set_result(0, -1)

    # -- values ---------------------------------------------------------------

    @property
    def query(self) -> str:
        return self._find.text()

    @property
    def replacement(self) -> str:
        return self._replace.text()

    @property
    def case_sensitive(self) -> bool:
        return self._case.isChecked()

    def set_query(self, text: str) -> None:
        self._find.setText(text)

    def set_replacement(self, text: str) -> None:
        self._replace.setText(text)

    # -- state ----------------------------------------------------------------

    def open_bar(self, *, replace: bool = False, query: str = "") -> None:
        if query:
            self._find.setText(query)
        self.setVisible(True)
        target = self._replace if replace and self._find.text() else self._find
        target.setFocus()
        target.selectAll()

    def close_bar(self) -> None:
        self.setVisible(False)
        self.closed.emit()

    def set_result(self, count: int, current: int) -> None:
        """Show "3 of 12", or why nothing can be stepped through."""
        has_query = bool(self.query)
        if not has_query:
            self._count.setText("")
        elif count == 0:
            self._count.setText("No matches")
        elif current < 0:
            self._count.setText(f"{count} match{'es' if count != 1 else ''}")
        else:
            self._count.setText(f"{current + 1} of {count}")
        for button in (self._next, self._previous, self._replace_button, self._replace_all_button):
            button.setEnabled(count > 0)

    @property
    def result_text(self) -> str:
        return self._count.text()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if (
            watched is self._find
            and event.type() == QEvent.Type.KeyPress
            and isinstance(event, QKeyEvent)
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.previousRequested.emit()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        super().keyPressEvent(event)
