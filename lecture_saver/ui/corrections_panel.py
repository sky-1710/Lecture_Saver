"""Going through what the assistant thinks the transcription got wrong.

The panel only shows the suggestions and reports what the user decided; the
lecture view applies that to the project's one transcript (§14). It is a list
of links rather than widgets, like the search results, and sits beside the
video on purpose: the way to settle a doubtful word is to hear it, so *Listen*
plays the line while the list stays open.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape as html_escape

from PySide6.QtCore import QUrl, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.corrections import Correction
from ..core.timecode import format_hms
from . import theme
from .theme import Role, set_role

__all__ = ["CorrectionsPanel", "TypoEntry"]


@dataclass(frozen=True)
class TypoEntry:
    """One open suggestion, with what is needed to judge it."""

    correction: Correction
    #: The line as it reads now.
    line: str
    #: The number of the slide it was said on, if there are slides.
    slide: int | None = None


class CorrectionsPanel(QWidget):
    """The suggestions in lecture order. Every signal carries a Correction."""

    #: Change the line.
    fixRequested = Signal(object)
    #: Leave the line as it is, and stop suggesting otherwise.
    keepRequested = Signal(object)
    #: Play the video from the line.
    listenRequested = Signal(object)
    #: Show the line in the transcript, to edit it by hand.
    lineRequested = Signal(object)
    #: Accept every suggestion the assistant was sure of.
    fixAllSureRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._entries: tuple[TypoEntry, ...] = ()

        self._summary = QLabel("")
        self._summary.setWordWrap(True)
        set_role(self._summary, Role.MUTED)
        self._fix_sure_button = QPushButton("Fix the sure ones")
        self._fix_sure_button.setToolTip(
            "Accept every suggestion the assistant was sure of, as one step that "
            "Undo takes back. The ones it doubted stay for you to judge."
        )
        self._fix_sure_button.clicked.connect(self.fixAllSureRequested)
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(self._summary, 1)
        top.addWidget(self._fix_sure_button)

        self._list = QTextBrowser()
        self._list.setOpenLinks(False)
        self._list.anchorClicked.connect(self._on_link)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(top)
        layout.addWidget(self._list, 1)

    @property
    def entries(self) -> tuple[TypoEntry, ...]:
        return self._entries

    @property
    def sure_count(self) -> int:
        return sum(1 for entry in self._entries if entry.correction.sure)

    def set_entries(self, entries: Sequence[TypoEntry]) -> None:
        self._entries = tuple(entries)
        count = len(self._entries)
        self._summary.setText(
            f"{count} word{'s' if count != 1 else ''} the assistant doubts. "
            "The transcript changes only when you fix one."
            if count else "Nothing left to look through."
        )
        sure = self.sure_count
        self._fix_sure_button.setText(f"Fix {sure} sure" if sure else "Fix the sure ones")
        self._fix_sure_button.setEnabled(sure > 0)
        # Deciding one must not throw the reader back to the top of the list.
        bar = self._list.verticalScrollBar()
        position = bar.value()
        self._list.setHtml("".join(_entry_html(i, e) for i, e in enumerate(self._entries)))
        bar.setValue(position)

    def _on_link(self, url: QUrl) -> None:
        kind, _, number = url.toString().partition(":")
        signals = {
            "fix": self.fixRequested,
            "keep": self.keepRequested,
            "listen": self.listenRequested,
            "line": self.lineRequested,
        }
        if kind in signals and number.isdigit() and int(number) < len(self._entries):
            signals[kind].emit(self._entries[int(number)].correction)


def _line_html(entry: TypoEntry) -> str:
    """The line, with the words in question picked out."""
    parts, cursor = [], 0
    for start, end in entry.correction.spans(entry.line):
        parts.append(html_escape(entry.line[cursor:start]))
        parts.append(
            f'<b style="background-color:{theme.WARNING_BG}; color:{theme.WARNING_TEXT};">'
            f"{html_escape(entry.line[start:end])}</b>"
        )
        cursor = end
    parts.append(html_escape(entry.line[cursor:]))
    return "".join(parts)


def _entry_html(index: int, entry: TypoEntry) -> str:
    correction = entry.correction
    where = [format_hms(correction.start)]
    if entry.slide is not None:
        where.insert(0, f"Slide {entry.slide}")
    certainty = (
        f'<span style="color:{theme.SUCCESS};">sure</span>' if correction.sure
        else f'<span style="color:{theme.WARNING_TEXT};">not sure, listen</span>'
    )
    reason = (
        f'<br/><span style="color:{theme.MUTED};">{html_escape(correction.reason)}</span>'
        if correction.reason else ""
    )
    link = f'style="color:{theme.ACCENT}; text-decoration:none;"'
    return (
        '<p style="margin:0 0 14px 0;">'
        f'<span style="color:{theme.MUTED};">{" · ".join(where)} · </span>{certainty}<br/>'
        f'<a href="line:{index}" style="color:{theme.TEXT}; text-decoration:none;">'
        f"{_line_html(entry)}</a><br/>"
        f"→ <b>{html_escape(correction.right)}</b>{reason}<br/>"
        f'<a href="fix:{index}" {link}>Fix</a> &nbsp;·&nbsp; '
        f'<a href="keep:{index}" {link}>Keep as it is</a> &nbsp;·&nbsp; '
        f'<a href="listen:{index}" {link}>Listen</a>'
        "</p>"
    )
