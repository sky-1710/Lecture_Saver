"""The preview as a lecture, beside the video.

One step at a time: the pages of the lecture's PDF it is about above, its words
below, and a voice to read them. It teaches from the saved preview, so going
through it sends nothing and spends no quota. Only asking about it does, through
the Ask AI panel, as any question there does. The lecture is in Korean, and
unlike the rest of the application its controls are too.

It stops of its own accord before the answers to the check questions, as the
PDF puts them on a page of their own. The lecture view owns the project; this
is told what to show and says what the user asked for.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape as html_escape
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QPixmap, QResizeEvent, QTextDocument
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings
from ..export.flowchart import parse_flowchart
from ..export.markdown_pdf import Block, Code, Heading, Item, Quote, TableBlock, Text, inline
from ..study.lesson import Step, lesson_steps
from . import theme
from .flowchart_picture import chart_picture
from .narrator import Narrator
from .theme import Role, set_role

__all__ = ["LessonPanel"]

#: Offered speeds, and what each is to the speech service, -1 to 1.
SPEEDS = (("느리게", -0.25), ("보통", 0.0), ("빠르게", 0.3), ("더 빠르게", 0.6))
_DEFAULT_SPEED = 1

#: The narrowest and the widest a concept map is drawn, and its tallest, in pixels.
_CHART_MIN_WIDTH = 300
_CHART_MAX_WIDTH = 620
_CHART_HEIGHT = 620.0

_TIME_IN_MARKUP = re.compile(r"\[(\d{1,2}:\d{2}(?::\d{2})?)\]")
_TIME_SCHEME = "time"
_CHART_SCHEME = "chart"


def _with_time_links(markup: str) -> str:
    """Make each cited time a link. The times are already in ``inline``'s markup."""

    def link(found: re.Match[str]) -> str:
        parts = [int(part) for part in found.group(1).split(":")]
        seconds = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else (
            parts[0] * 60 + parts[1]
        )
        return (
            f'<a href="{_TIME_SCHEME}:{seconds}" style="text-decoration:none;">'
            f"{found.group(0)}</a>"
        )

    return _TIME_IN_MARKUP.sub(link, markup)


def _table_html(block: TableBlock) -> str:
    rows = []
    for number, row in enumerate(block.rows):
        tag = "th" if number == 0 else "td"
        cells = "".join(f"<{tag}>{_with_time_links(inline(cell))}</{tag}>" for cell in row)
        rows.append(f"<tr>{cells}</tr>")
    return (
        '<table border="1" cellspacing="0" cellpadding="5" '
        f'style="border-color:{theme.BORDER}; margin:6px 0 12px 0;">{"".join(rows)}</table>'
    )


def _block_html(block: Block, charts: dict[str, object], width: int, ratio: float) -> str:
    if isinstance(block, Heading):
        size = {1: 5, 2: 5, 3: 4}.get(block.level, 3)
        title = f'<font size="{size}"><b>{inline(block.text)}</b></font>'
        return f'<p style="margin:12px 0 6px 0;">{title}</p>'
    if isinstance(block, Text):
        return f'<p style="margin:0 0 9px 0;">{_with_time_links(inline(block.text))}</p>'
    if isinstance(block, Quote):
        return (
            f'<table width="100%" cellpadding="8" style="background-color:{theme.ACCENT_SOFT}; '
            f'margin:4px 0 10px 0;"><tr><td>{_with_time_links(inline(block.text))}</td></tr>'
            "</table>"
        )
    if isinstance(block, Item):
        indent = 18 * block.depth + 4
        bullet = block.marker if block.marker[0].isdigit() else "•"
        return (
            f'<p style="margin:0 0 5px {indent}px;">{html_escape(bullet)} '
            f"{_with_time_links(inline(block.text))}</p>"
        )
    if isinstance(block, TableBlock):
        return _table_html(block)
    if isinstance(block, Code):
        chart = parse_flowchart(block.text) if block.language == "mermaid" else None
        picture = (
            chart_picture(chart, width, _CHART_HEIGHT, pixel_ratio=ratio) if chart else None
        )
        if picture is not None:
            name = f"{_CHART_SCHEME}:{len(charts)}"
            charts[name] = picture
            shown = round(picture.width() / ratio)
            return f'<p align="center"><img src="{name}" width="{shown}"/></p>'
        return f"<pre>{html_escape(block.text)}</pre>"
    return ""


class LessonPanel(QWidget):
    """Goes through the steps. Says what it wants of the video and of Ask AI."""

    #: Go to this moment of the lecture.
    seekRequested = Signal(float)
    #: Ask about this step: its words, and the moment it cites or 0.
    askRequested = Signal(str, float)
    #: The voice is about to start, so the video should stop talking.
    readingStarted = Signal()

    def __init__(self, parent: QWidget | None = None, narrator: Narrator | None = None) -> None:
        super().__init__(parent)
        self._steps: list[Step] = []
        self._pictures: tuple[Path, ...] = ()
        self._index = 0
        self._page = 0
        self._settings: Settings | None = None
        self._following = False
        self._pixmap = QPixmap()
        self._narrator = narrator if narrator is not None else Narrator(self)
        self._narrator.finished.connect(self._on_read_to_the_end)
        self._narrator.stateChanged.connect(self._update_controls)

        self._where = QLabel("")
        self._where.setWordWrap(True)
        set_role(self._where, Role.MUTED)

        self._picture = QLabel()
        self._picture.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._picture.setMinimumHeight(60)
        self._page_label = QLabel("")
        set_role(self._page_label, Role.MUTED)
        self._earlier_page = QPushButton("이전 쪽")
        self._earlier_page.clicked.connect(lambda: self._flip_page(-1))
        self._later_page = QPushButton("다음 쪽")
        self._later_page.clicked.connect(lambda: self._flip_page(1))
        pages = QHBoxLayout()
        pages.setSpacing(6)
        pages.addStretch(1)
        pages.addWidget(self._earlier_page)
        pages.addWidget(self._page_label)
        pages.addWidget(self._later_page)
        pages.addStretch(1)
        self._deck = QWidget()
        deck_layout = QVBoxLayout(self._deck)
        deck_layout.setContentsMargins(0, 0, 0, 0)
        deck_layout.setSpacing(4)
        deck_layout.addWidget(self._picture, 1)
        deck_layout.addLayout(pages)

        self._text = QTextBrowser()
        self._text.setOpenLinks(False)
        self._text.anchorClicked.connect(self._on_link)

        self._split = QSplitter(Qt.Orientation.Vertical)
        self._split.addWidget(self._deck)
        self._split.addWidget(self._text)
        self._split.setStretchFactor(0, 2)
        self._split.setStretchFactor(1, 3)
        self._split.setChildrenCollapsible(False)

        self._back_button = QPushButton("이전")
        self._back_button.clicked.connect(self.back)
        self._read_button = QPushButton("읽어 주기")
        self._read_button.clicked.connect(self.toggle_reading)
        self._stop_button = QPushButton("멈춤")
        self._stop_button.clicked.connect(self.stop)
        self._next_button = QPushButton("다음")
        self._next_button.clicked.connect(self.forward)
        self._speed = QComboBox()
        self._speed.addItems([name for name, _ in SPEEDS])
        self._speed.setCurrentIndex(_DEFAULT_SPEED)
        self._speed.setToolTip("읽는 속도")
        self._speed.currentIndexChanged.connect(self._on_speed)
        self._ask_button = QPushButton("이 부분 질문")
        self._ask_button.setToolTip(
            "이 부분을 Ask AI 입력칸 위에 올립니다. 강의를 듣는 동안에는 아무것도 "
            "보내지 않고, 질문할 때만 보냅니다."
        )
        self._ask_button.clicked.connect(self._ask)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        for button in (self._back_button, self._read_button, self._stop_button, self._next_button):
            controls.addWidget(button)
        controls.addWidget(self._speed)
        controls.addStretch(1)
        controls.addWidget(self._ask_button)
        self._voice_note = QLabel("")
        self._voice_note.setWordWrap(True)
        set_role(self._voice_note, Role.MUTED)
        self._voice_note.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(self._where)
        layout.addWidget(self._split, 1)
        layout.addLayout(controls)
        layout.addWidget(self._voice_note)
        self._update_controls()

    # -- what it shows ----------------------------------------------------------

    @property
    def steps(self) -> list[Step]:
        return self._steps

    @property
    def step_index(self) -> int:
        return self._index

    @property
    def is_reading(self) -> bool:
        return self._narrator.is_reading

    def set_settings(self, settings: Settings) -> None:
        self._settings = settings
        for number, (name, _rate) in enumerate(SPEEDS):
            if name == settings.lesson_speed:
                self._speed.setCurrentIndex(number)

    def set_text_size(self, points: float) -> None:
        font = self._text.font()
        font.setPointSizeF(points)
        self._text.setFont(font)

    def set_lesson(self, markdown: str, pictures: Sequence[Path]) -> None:
        """Show this preview from where the user was, if it is the same one."""
        steps = lesson_steps(markdown)
        same = [step.blocks for step in steps] == [step.blocks for step in self._steps]
        self._pictures = tuple(pictures)
        if not same:
            self.stop()
            self._steps = steps
            self._index = 0
        self._show_step()

    def clear(self) -> None:
        self.stop()
        self._steps = []
        self._pictures = ()
        self._index = 0
        self._text.clear()
        self._picture.clear()
        self._update_controls()

    def _show_step(self) -> None:
        self._page = 0
        step = self._current
        if step is None:
            self._where.setText("")
            self._text.clear()
            self._update_controls()
            return
        self._where.setText(f"{self._index + 1} / {len(self._steps)}  ·  {step.part}")
        self._text.setHtml(self._step_html(step))
        self._text.verticalScrollBar().setValue(0)
        self._show_page()
        self._update_controls()

    @property
    def _current(self) -> Step | None:
        return self._steps[self._index] if 0 <= self._index < len(self._steps) else None

    def _step_html(self, step: Step) -> str:
        charts: dict[str, object] = {}
        room = self._text.viewport().width() - 40
        width = max(_CHART_MIN_WIDTH, min(room, _CHART_MAX_WIDTH))
        ratio = self.devicePixelRatioF()
        parts = [_block_html(block, charts, width, ratio) for block in step.blocks]
        document = self._text.document()
        for name, picture in charts.items():
            document.addResource(QTextDocument.ResourceType.ImageResource, QUrl(name), picture)
        return "".join(parts)

    # -- the PDF's pages ---------------------------------------------------------

    def _pages_to_show(self) -> tuple[int, ...]:
        """The pages this step cites that have a picture, else the first page."""
        step = self._current
        count = len(self._pictures)
        cited = tuple(p for p in step.pages if 1 <= p <= count) if step is not None else ()
        return cited or ((1,) if count else ())

    def _show_page(self) -> None:
        pages = self._pages_to_show()
        self._deck.setVisible(bool(pages))
        if not pages:
            return
        page = pages[min(self._page, len(pages) - 1)]
        self._pixmap = QPixmap(str(self._pictures[page - 1]))
        self._fit_picture()
        self._page_label.setText(f"p.{page}")
        self._earlier_page.setVisible(len(pages) > 1)
        self._later_page.setVisible(len(pages) > 1)
        self._earlier_page.setEnabled(self._page > 0)
        self._later_page.setEnabled(self._page < len(pages) - 1)

    def _flip_page(self, by: int) -> None:
        self._page = max(0, min(self._page + by, len(self._pages_to_show()) - 1))
        self._show_page()

    def _fit_picture(self) -> None:
        if self._pixmap.isNull():
            return
        size = self._picture.size()
        if size.width() < 10 or size.height() < 10:
            return
        self._picture.setPixmap(
            self._pixmap.scaled(
                size, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._fit_picture()

    # -- going through it ---------------------------------------------------------

    def go_to(self, index: int) -> None:
        """Show step ``index``, and keep reading if it was being read."""
        if not 0 <= index < len(self._steps):
            return
        reading = self.is_reading
        self._narrator.stop()
        self._index = index
        self._show_step()
        if reading:
            self._read_step()

    def back(self) -> None:
        self.go_to(self._index - 1)

    def forward(self) -> None:
        self.go_to(self._index + 1)

    def _read_step(self) -> None:
        step = self._current
        if step is not None:
            self._narrator.read(step.paragraphs)

    def toggle_reading(self) -> None:
        """Start reading from this step, or pause or go on with what is being read."""
        if self._narrator.is_reading:
            self._narrator.toggle_pause()
            return
        reason = self._narrator.unavailable_reason()
        self._voice_note.setText(reason)
        self._voice_note.setVisible(bool(reason))
        if reason:
            return
        self.readingStarted.emit()
        self._read_step()

    def stop(self) -> None:
        self._narrator.stop()

    def _on_read_to_the_end(self) -> None:
        """Go on to the next step, stopping short of the answers."""
        following = self._index + 1
        if following >= len(self._steps) or self._steps[following].is_answers:
            self._update_controls()
            return
        self._index = following
        self._show_step()
        self._read_step()

    def _update_controls(self) -> None:
        has_steps = bool(self._steps)
        reading = self._narrator.is_reading
        self._back_button.setEnabled(has_steps and self._index > 0)
        self._next_button.setEnabled(has_steps and self._index < len(self._steps) - 1)
        self._read_button.setEnabled(has_steps)
        self._stop_button.setEnabled(reading)
        self._ask_button.setEnabled(has_steps)
        if not reading:
            self._read_button.setText("읽어 주기")
        elif self._narrator.is_paused:
            self._read_button.setText("이어서")
        else:
            self._read_button.setText("일시정지")

    def _on_speed(self, number: int) -> None:
        name, rate = SPEEDS[number]
        self._narrator.set_rate(rate)
        if self._settings is not None:
            self._settings.lesson_speed = name

    # -- the rest of the application -----------------------------------------------

    def _on_link(self, url: QUrl) -> None:
        kind, _, value = url.toString().partition(":")
        if kind == _TIME_SCHEME and value.isdigit():
            self.seekRequested.emit(float(value))

    def _ask(self) -> None:
        step = self._current
        if step is None:
            return
        words = " ".join(step.paragraphs)
        self.askRequested.emit(words, step.times[0] if step.times else 0.0)
