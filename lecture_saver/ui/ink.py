"""Drawing over a picture: pen strokes, underlines, highlights and typed memos.

The video (drawing while it plays) and the screenshot panel (editing a saved
screenshot) both draw with :class:`InkCanvas`, so a mark looks and behaves the
same in each; :func:`render_marked` burns marks into a picture with the very
same painting code, for printing.

Marks are placed in fractions of the picture (see
:mod:`lecture_saver.core.captures`) and sized from the picture's height, so a
memo is as large next to the slide on the panel's small view as on the
full-size frame.

Made for a mouse: the pen draws freely - to circle, tick or join things up -
an underline is a straight drag that snaps level when it is nearly level, a
highlight is a dragged box - or a drag along one line of text, which marks
that line - and a memo is typed rather than handwritten.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import replace

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, QSizeF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFocusEvent,
    QFont,
    QFontMetricsF,
    QHideEvent,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QPixmap,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QToolButton,
    QWidget,
)

from ..core.captures import HIGHLIGHT, MEMO, PEN, UNDERLINE, Mark, MarkSheet, simplify_path
from . import theme

__all__ = [
    "PEN_TOOLS",
    "TOOLS",
    "TOOL_ERASER",
    "TOOL_HIGHLIGHT",
    "TOOL_MEMO",
    "TOOL_PEN",
    "TOOL_UNDERLINE",
    "InkCanvas",
    "InkToolBar",
    "fit_rect",
    "hit_test",
    "paint_marks",
    "render_marked",
]

TOOL_PEN = PEN
TOOL_UNDERLINE = UNDERLINE
TOOL_HIGHLIGHT = HIGHLIGHT
TOOL_MEMO = MEMO
TOOL_ERASER = "eraser"
#: What the pen button holds: everything drawn or rubbed out by dragging.
PEN_TOOLS = (TOOL_PEN, TOOL_UNDERLINE, TOOL_HIGHLIGHT, TOOL_ERASER)
#: In the order of the keys 1-5.
TOOLS = (*PEN_TOOLS, TOOL_MEMO)

TOOL_LABELS = {
    TOOL_PEN: "Free pen",
    TOOL_UNDERLINE: "Underline",
    TOOL_HIGHLIGHT: "Highlight",
    TOOL_MEMO: "Memo",
    TOOL_ERASER: "Eraser",
}
TOOL_TIPS = {
    TOOL_PEN: "Draw freely: circle, tick, arrow or join things up.",
    TOOL_UNDERLINE: "Drag under words to underline them. A nearly level drag is drawn level.",
    TOOL_HIGHLIGHT: "Drag a box over what matters - or drag along one line of text to "
                    "highlight that line.",
    TOOL_MEMO: "Click where the note goes and type; Enter keeps it, Esc drops it.\n"
               "To change a memo later, click it with this tool - or double-click it "
               "with any tool.",
    TOOL_ERASER: "Click or drag over marks to remove them.\n"
                 "Right-click removes a mark whichever tool is chosen.",
}

#: The inks a pen stroke, an underline and a memo can be in, as the colour
#: button offers them. Red is the default, and what every mark drawn before
#: there was a choice is shown in. White is for dark slides.
INKS = (
    ("Red", "#e5262e"),
    ("Blue", "#1f5fd6"),
    ("Black", "#1b1d22"),
    ("Green", "#178a3e"),
    ("Orange", "#f07a12"),
    ("Purple", "#7b3fc4"),
    ("White", "#ffffff"),
)
DEFAULT_INK = INKS[0][1]
UNDERLINE_COLOUR = QColor(DEFAULT_INK)
PEN_COLOUR = UNDERLINE_COLOUR
HIGHLIGHT_COLOUR = QColor(255, 214, 0, 105)
#: A memo is just text on the picture, no box, in an ink like the pen's -
#: red unless another is chosen, so it never reads as part of the slide.
MEMO_TEXT_COLOUR = PEN_COLOUR

#: Sizes, as fractions of the picture's height (width for the memo's wrap).
UNDERLINE_WIDTH = 0.0045
MEMO_TEXT_SIZE = 0.033
MEMO_MAX_WIDTH = 0.42
#: A highlight dragged flatter than this many pixels marks a line of text.
LINE_HIGHLIGHT_BELOW_PX = 7
LINE_HIGHLIGHT_HEIGHT = 0.034
#: An underline within this angle of level is drawn level (tan 12 degrees).
LEVEL_SNAP = math.tan(math.radians(12))
#: Drags shorter than this are clicks, not marks.
MIN_DRAG_PX = 5
#: Points of a pen stroke closer than this to the line it would be drawn
#: along anyway are left out (pixels on screen).
PEN_TOLERANCE_PX = 0.8

#: Highlights go under the other marks, memos over them, whatever the order
#: they were drawn in - a later highlight must not wash out a memo.
_LAYER = {HIGHLIGHT: 0, PEN: 1, UNDERLINE: 1, MEMO: 2}


# -- geometry -----------------------------------------------------------------


def fit_rect(content: QSize | QSizeF, area: QRectF) -> QRectF:
    """``content`` scaled to fit ``area`` without distortion, centred in it."""
    width, height = float(content.width()), float(content.height())
    if width <= 0 or height <= 0 or area.width() <= 0 or area.height() <= 0:
        return QRectF()
    scale = min(area.width() / width, area.height() / height)
    fitted = QSizeF(width * scale, height * scale)
    return QRectF(
        area.x() + (area.width() - fitted.width()) / 2,
        area.y() + (area.height() - fitted.height()) / 2,
        fitted.width(), fitted.height(),
    )


def to_unit(point: QPointF, rect: QRectF) -> tuple[float, float]:
    """A point on screen as fractions of the picture, kept inside it."""
    x = (point.x() - rect.x()) / rect.width() if rect.width() else 0.0
    y = (point.y() - rect.y()) / rect.height() if rect.height() else 0.0
    return min(1.0, max(0.0, x)), min(1.0, max(0.0, y))


def to_pixels(x: float, y: float, rect: QRectF) -> QPointF:
    return QPointF(rect.x() + x * rect.width(), rect.y() + y * rect.height())


def ink_of(mark: Mark) -> QColor:
    """What ``mark`` is drawn in; a highlight's yellow is not an ink."""
    return QColor(mark.colour or DEFAULT_INK)


def stored_ink(ink: str) -> str:
    """How an ink is kept on a mark: the default is left unsaid."""
    return "" if ink == DEFAULT_INK else ink


def underline_width(rect: QRectF) -> float:
    return max(2.0, rect.height() * UNDERLINE_WIDTH)


def memo_font(rect: QRectF) -> QFont:
    font = QFont()
    font.setPixelSize(max(9, round(rect.height() * MEMO_TEXT_SIZE)))
    # Without a box behind it, a little weight keeps it readable on video.
    font.setWeight(QFont.Weight.DemiBold)
    return font


def _memo_padding(font: QFont) -> float:
    return max(3.0, font.pixelSize() * 0.35)


def memo_box(mark: Mark, rect: QRectF) -> QRectF:
    """Where a memo's note is drawn: its text wrapped, kept inside the picture."""
    font = memo_font(rect)
    pad = _memo_padding(font)
    wrap = max(rect.width() * MEMO_MAX_WIDTH, font.pixelSize() * 5.0)
    text = QFontMetricsF(font).boundingRect(
        QRectF(0, 0, wrap, 100_000),
        int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap),
        mark.text,
    )
    box = QRectF(to_pixels(mark.x1, mark.y1, rect), QSizeF(text.width() + 2 * pad,
                                                           text.height() + 2 * pad))
    # A note clicked near the right or bottom edge grows back into the picture.
    if box.right() > rect.right():
        box.moveRight(rect.right())
    if box.bottom() > rect.bottom():
        box.moveBottom(rect.bottom())
    if box.left() < rect.left():
        box.moveLeft(rect.left())
    if box.top() < rect.top():
        box.moveTop(rect.top())
    return box


def paint_order(marks: Sequence[Mark]) -> list[int]:
    """Indices of ``marks`` from the bottom layer up."""
    return sorted(range(len(marks)), key=lambda i: _LAYER.get(marks[i].kind, 1))


def hit_test(marks: Sequence[Mark], point: QPointF, rect: QRectF) -> int:
    """Index of the topmost mark under ``point``, or -1."""
    tolerance = max(6.0, underline_width(rect) * 2)
    for index in reversed(paint_order(marks)):
        mark = marks[index]
        if mark.kind == UNDERLINE:
            a = to_pixels(mark.x1, mark.y1, rect)
            b = to_pixels(mark.x2, mark.y2, rect)
            if _distance_to_segment(point, a, b) <= tolerance:
                return index
        elif mark.kind == PEN:
            box = QRectF(to_pixels(mark.x1, mark.y1, rect), to_pixels(mark.x2, mark.y2, rect))
            if not box.adjusted(-tolerance, -tolerance, tolerance, tolerance).contains(point):
                continue
            stroke = [to_pixels(x, y, rect) for x, y in mark.points]
            if any(_distance_to_segment(point, a, b) <= tolerance
                   for a, b in zip(stroke, stroke[1:] or stroke, strict=False)):
                return index
        elif mark.kind == HIGHLIGHT:
            box = QRectF(to_pixels(mark.x1, mark.y1, rect), to_pixels(mark.x2, mark.y2, rect))
            if box.adjusted(-2, -2, 2, 2).contains(point):
                return index
        elif mark.kind == MEMO and memo_box(mark, rect).contains(point):
            return index
    return -1


def _distance_to_segment(p: QPointF, a: QPointF, b: QPointF) -> float:
    dx, dy = b.x() - a.x(), b.y() - a.y()
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((p.x() - a.x()) * dx
                                                   + (p.y() - a.y()) * dy) / length))
    return math.hypot(p.x() - (a.x() + t * dx), p.y() - (a.y() + t * dy))


# -- painting -----------------------------------------------------------------


def paint_marks(painter: QPainter, marks: Sequence[Mark], rect: QRectF, *,
                skip: int = -1) -> None:
    """Draw ``marks`` over the picture occupying ``rect``; ``skip`` is left out."""
    if rect.isEmpty() or not marks:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setClipRect(rect)
    line = QPen(UNDERLINE_COLOUR, underline_width(rect))
    line.setCapStyle(Qt.PenCapStyle.RoundCap)
    font = memo_font(rect)
    for index in paint_order(marks):
        if index == skip:
            continue
        mark = marks[index]
        if mark.kind == HIGHLIGHT:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(HIGHLIGHT_COLOUR)
            painter.drawRect(QRectF(to_pixels(mark.x1, mark.y1, rect),
                                    to_pixels(mark.x2, mark.y2, rect)))
        elif mark.kind == UNDERLINE:
            line.setColor(ink_of(mark))
            painter.setPen(line)
            painter.drawLine(to_pixels(mark.x1, mark.y1, rect), to_pixels(mark.x2, mark.y2, rect))
        elif mark.kind == PEN:
            line.setColor(ink_of(mark))
            painter.setPen(line)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(stroke_path([to_pixels(x, y, rect) for x, y in mark.points]))
        elif mark.kind == MEMO:
            _paint_memo(painter, mark, rect, font)
    painter.restore()


def stroke_path(points: Sequence[QPointF]) -> QPainterPath:
    """A smooth curve through a pen stroke's points.

    Each corner of the polyline is rounded by a quadratic curve through the
    midpoints either side of it, so a stroke kept to a few dozen points still
    looks hand-drawn rather than faceted.
    """
    path = QPainterPath()
    if not points:
        return path
    path.moveTo(points[0])
    if len(points) == 1:
        path.lineTo(points[0])
        return path
    for corner, following in zip(points[1:-1], points[2:], strict=True):
        path.quadTo(corner, QPointF((corner.x() + following.x()) / 2,
                                    (corner.y() + following.y()) / 2))
    path.lineTo(points[-1])
    return path


def _paint_memo(painter: QPainter, mark: Mark, rect: QRectF, font: QFont) -> None:
    # The box only places and wraps the text, and is what a click hits.
    box = memo_box(mark, rect)
    pad = _memo_padding(font)
    painter.setFont(font)
    painter.setPen(ink_of(mark))
    painter.drawText(
        box.adjusted(pad, pad, -pad, -pad),
        int(Qt.AlignmentFlag.AlignLeft) | int(Qt.TextFlag.TextWordWrap),
        mark.text,
    )


def render_marked(image: QImage, marks: Sequence[Mark]) -> QImage:
    """A copy of ``image`` with ``marks`` drawn in, at its full size."""
    out = image.convertToFormat(QImage.Format.Format_RGB32)
    out.setDevicePixelRatio(1.0)
    painter = QPainter(out)
    paint_marks(painter, marks, QRectF(0, 0, out.width(), out.height()))
    painter.end()
    return out


ICON_SIZE = 20


def tool_icon(tool: str) -> QIcon:
    """A small picture of what each tool draws, for its button."""
    scale = 2
    pixmap = QPixmap(ICON_SIZE * scale, ICON_SIZE * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    ink = QColor(theme.MUTED)
    round_cap = (Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
    if tool == TOOL_PEN:
        # A pencil leaning right, over the squiggle it has just drawn.
        squiggle = QPainterPath(QPointF(2.5, 16.5))
        squiggle.cubicTo(QPointF(5, 12.5), QPointF(7, 19.5), QPointF(10, 15.5))
        painter.setPen(QPen(PEN_COLOUR, 1.8, *round_cap))
        painter.drawPath(squiggle)
        painter.translate(13.5, 7.5)
        painter.rotate(45)
        painter.setPen(QPen(ink, 1.4, *round_cap))
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(QRectF(-2.2, -7.5, 4.4, 10.5), 1.0, 1.0)
        tip = QPainterPath(QPointF(-2.2, 3.0))
        tip.lineTo(QPointF(0, 7.0))
        tip.lineTo(QPointF(2.2, 3.0))
        tip.closeSubpath()
        painter.setBrush(ink)
        painter.drawPath(tip)
    elif tool == TOOL_UNDERLINE:
        painter.setPen(QPen(ink, 1.6, *round_cap))
        painter.drawLine(QPointF(4, 5), QPointF(16, 5))
        painter.drawLine(QPointF(4, 10), QPointF(13, 10))
        painter.setPen(QPen(UNDERLINE_COLOUR, 2.4, *round_cap))
        painter.drawLine(QPointF(3, 15.5), QPointF(17, 15.5))
    elif tool == TOOL_HIGHLIGHT:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 214, 0, 210))
        painter.drawRoundedRect(QRectF(2, 6.5, 16, 7.5), 1.5, 1.5)
        painter.setPen(QPen(ink, 1.6, *round_cap))
        painter.drawLine(QPointF(5, 10.25), QPointF(15, 10.25))
    elif tool == TOOL_MEMO:
        # A "T": text typed straight onto the picture, in the memo's red.
        painter.setPen(QPen(MEMO_TEXT_COLOUR, 2.2, *round_cap))
        painter.drawLine(QPointF(4.5, 4.5), QPointF(15.5, 4.5))
        painter.drawLine(QPointF(10, 4.5), QPointF(10, 16))
        painter.setPen(QPen(ink, 1.4, *round_cap))
        painter.drawLine(QPointF(7.5, 16), QPointF(12.5, 16))
    else:
        painter.setPen(QPen(ink, 1.4, *round_cap))
        painter.drawLine(QPointF(3, 17.5), QPointF(17, 17.5))
        painter.translate(10, 9.5)
        painter.rotate(-40)
        painter.setBrush(QColor(theme.SURFACE))
        painter.drawRoundedRect(QRectF(-6.5, -3.5, 13, 7), 1.5, 1.5)
        painter.setBrush(QColor(theme.BORDER_STRONG))
        painter.drawRoundedRect(QRectF(-6.5, -3.5, 5, 7), 1.5, 1.5)
    painter.end()
    return QIcon(pixmap)


def ink_icon(ink: str) -> QIcon:
    """A dot of ``ink``, ringed so that white shows on a white menu."""
    scale = 2
    pixmap = QPixmap(ICON_SIZE * scale, ICON_SIZE * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(QColor(theme.BORDER_HOVER), 1.2))
    painter.setBrush(QColor(ink))
    painter.drawEllipse(QRectF(4, 4, 12, 12))
    painter.end()
    return QIcon(pixmap)


def draw_icon() -> QIcon:
    """The pen, for the button that turns drawing over the video on."""
    return tool_icon(TOOL_PEN)


def history_icon(*, redo: bool) -> QIcon:
    """A hooked arrow back (undo) or forward (redo)."""
    scale = 2
    pixmap = QPixmap(ICON_SIZE * scale, ICON_SIZE * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    if redo:
        painter.translate(ICON_SIZE, 0)
        painter.scale(-1, 1)
    painter.setPen(QPen(QColor(theme.MUTED), 1.7, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    hook = QPainterPath(QPointF(5, 7.5))
    hook.lineTo(QPointF(11.5, 7.5))
    hook.cubicTo(QPointF(16.5, 7.5), QPointF(16.5, 15.5), QPointF(11.5, 15.5))
    hook.lineTo(QPointF(7, 15.5))
    painter.drawPath(hook)
    painter.drawLine(QPointF(5, 7.5), QPointF(8, 4.5))
    painter.drawLine(QPointF(5, 7.5), QPointF(8, 10.5))
    painter.end()
    return QIcon(pixmap)


# -- the canvas ---------------------------------------------------------------


class _MemoEditor(QLineEdit):
    """The box a memo is typed into, placed where it will appear."""

    committed = Signal()
    cancelled = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._done = False
        self.returnPressed.connect(self._commit)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() == Qt.Key.Key_Escape:
            self._done = True
            self.cancelled.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt naming
        super().focusOutEvent(event)
        # Clicking anywhere else keeps what was typed, as in any note app. A
        # context menu of the box itself takes the focus too; that is no exit.
        if event.reason() != Qt.FocusReason.PopupFocusReason:
            self._commit()

    def _commit(self) -> None:
        if not self._done:
            self._done = True
            self.committed.emit()


class InkCanvas(QWidget):
    """A picture with marks over it that the user can add to and edit.

    Subclasses say what the picture is (:meth:`picture_size`,
    :meth:`paint_picture`); this class does the marks, the tools and the keys.
    """

    #: The user changed the marks: drew, erased, edited a memo, undid, redid
    #: or cleared. Not emitted by :meth:`set_marks`.
    marksChanged = Signal()
    #: The user drew a new mark (also emitted with ``marksChanged``).
    markAdded = Signal()
    #: Marks were loaded from elsewhere; the history starts again.
    marksLoaded = Signal()
    toolChanged = Signal(str)
    #: The ink new marks are drawn in changed; carries ``#rrggbb``.
    inkChanged = Signal(str)
    #: Esc with nothing in progress - the place to leave drawing.
    escapePressed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._sheet = MarkSheet()
        self._tool = TOOL_PEN
        self._ink = DEFAULT_INK
        #: The ink of the memo being typed: its own when an old one is changed.
        self._memo_ink = DEFAULT_INK
        self._editable = True
        self._drag_from: QPointF | None = None
        #: The pen stroke being drawn, in pixels on screen.
        self._stroke: list[QPointF] | None = None
        self._drag_to = QPointF()
        self._erasing = False
        self._memo_editor: _MemoEditor | None = None
        #: The memo being changed, or -1 while a new one is typed.
        self._memo_index = -1
        self._memo_at = (0.0, 0.0)
        self._flash = 0.0
        self._flash_timer = QTimer(self)
        self._flash_timer.setInterval(16)
        self._flash_timer.timeout.connect(self._fade_flash)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self._update_cursor()

    # -- what the marks are drawn on (for subclasses) ---------------------------

    def picture_size(self) -> QSize:
        """Size of the picture in its own pixels; empty when there is none."""
        return QSize()

    def paint_picture(self, painter: QPainter, rect: QRectF) -> None:
        """Draw the picture into ``rect``, which already has its proportions."""

    def paint_placeholder(self, painter: QPainter) -> None:
        """What to show when there is no picture."""

    def backdrop(self) -> QColor:
        return QColor(theme.VIDEO_BACKDROP)

    def picture_rect(self) -> QRectF:
        return fit_rect(self.picture_size(), QRectF(self.rect()))

    # -- marks ------------------------------------------------------------------

    @property
    def sheet(self) -> MarkSheet:
        return self._sheet

    @property
    def marks(self) -> tuple[Mark, ...]:
        return self._sheet.marks

    def set_marks(self, marks: Sequence[Mark]) -> None:
        """Show other marks - another screenshot's, say. Not an edit."""
        self.cancel_memo()
        self._drag_from = None
        self._stroke = None
        self._sheet.load(marks)
        self.update()
        self.marksLoaded.emit()

    def undo(self) -> bool:
        self.cancel_memo()
        return self._changed(self._sheet.undo())

    def redo(self) -> bool:
        self.cancel_memo()
        return self._changed(self._sheet.redo())

    def clear_marks(self) -> bool:
        self.cancel_memo()
        return self._changed(self._sheet.clear())

    def _changed(self, changed: bool, *, added: bool = False) -> bool:
        if changed:
            self.update()
            self.marksChanged.emit()
            if added:
                self.markAdded.emit()
        return changed

    # -- tools ------------------------------------------------------------------

    @property
    def tool(self) -> str:
        return self._tool

    def set_tool(self, tool: str) -> None:
        if tool not in TOOLS or tool == self._tool:
            return
        if tool != TOOL_MEMO:
            self.commit_memo()
        self._tool = tool
        self._update_cursor()
        self.toolChanged.emit(tool)

    @property
    def ink(self) -> str:
        return self._ink

    def set_ink(self, ink: str) -> None:
        """Draw new marks in ``ink`` (``#rrggbb``). A memo being typed takes it too."""
        ink = QColor(ink).name()
        if ink == self._ink:
            return
        self._ink = ink
        if self._memo_editor is not None:
            self._memo_ink = ink
            self._style_memo_editor()
        self.inkChanged.emit(ink)

    @property
    def editable(self) -> bool:
        return self._editable

    def set_editable(self, editable: bool) -> None:
        if not editable:
            self.commit_memo()
            self._drag_from = None
            self._stroke = None
        self._editable = editable
        self._update_cursor()

    def _update_cursor(self) -> None:
        if not self._editable:
            self.unsetCursor()
        elif self._tool == TOOL_MEMO:
            self.setCursor(Qt.CursorShape.IBeamCursor)
        elif self._tool == TOOL_ERASER:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.setCursor(Qt.CursorShape.CrossCursor)

    def flash(self) -> None:
        """A brief white flash: the picture was just taken."""
        self._flash = 0.55
        self._flash_timer.start()
        self.update()

    def _fade_flash(self) -> None:
        self._flash = max(0.0, self._flash - 0.06)
        if self._flash == 0.0:
            self._flash_timer.stop()
        self.update()

    # -- painting ---------------------------------------------------------------

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.backdrop())
        rect = self.picture_rect()
        if rect.isEmpty():
            self.paint_placeholder(painter)
            return
        self.paint_picture(painter, rect)
        paint_marks(painter, self._sheet.marks, rect, skip=self._memo_index)
        self._paint_drag(painter, rect)
        if self._flash:
            painter.fillRect(rect, QColor(255, 255, 255, int(255 * self._flash)))

    def _paint_drag(self, painter: QPainter, rect: QRectF) -> None:
        if self._stroke:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setClipRect(rect)
            pen = QPen(QColor(self._ink), underline_width(rect), Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(stroke_path(self._stroke))
            painter.restore()
            return
        mark = self._drag_mark(rect)
        if mark is not None:
            paint_marks(painter, [mark], rect)

    # -- mouse ------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        rect = self.picture_rect()
        if not self._editable or rect.isEmpty():
            super().mousePressEvent(event)
            return
        point = event.position()
        if self._memo_editor is not None:
            self.commit_memo()
        if event.button() == Qt.MouseButton.RightButton:
            self._erase_at(point, rect)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton or not rect.contains(point):
            super().mousePressEvent(event)
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._tool == TOOL_ERASER:
            self._erasing = True
            self._erase_at(point, rect)
        elif self._tool == TOOL_PEN:
            self._stroke = [point]
        elif self._tool == TOOL_MEMO:
            index = hit_test(self._sheet.marks, point, rect)
            if index >= 0 and self._sheet.marks[index].kind == MEMO:
                existing = self._sheet.marks[index]
                self.open_memo(existing.x1, existing.y1, existing.text, index=index)
            else:
                self.open_memo(*to_unit(point, rect))
        else:
            self._drag_from = point
            self._drag_to = point
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        rect = self.picture_rect()
        if self._stroke is not None:
            point = _clamp(event.position(), rect)
            last = self._stroke[-1]
            if abs(point.x() - last.x()) + abs(point.y() - last.y()) >= 1.0:
                self._stroke.append(point)
                self.update()
        elif self._drag_from is not None:
            self._drag_to = event.position()
            self.update()
        elif self._erasing and not rect.isEmpty():
            self._erase_at(event.position(), rect)
        else:
            super().mouseMoveEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        """A memo double-clicked is opened to be changed, whichever tool is chosen."""
        rect = self.picture_rect()
        if (
            self._editable and not rect.isEmpty()
            and event.button() == Qt.MouseButton.LeftButton
            and self._tool != TOOL_ERASER
        ):
            index = hit_test(self._sheet.marks, event.position(), rect)
            mark = self._sheet.marks[index] if index >= 0 else None
            if mark is not None and mark.kind == MEMO:
                # The first click of the two may have begun a stroke or a drag.
                self._stroke = None
                self._drag_from = None
                if not (self._memo_editor is not None and self._memo_index == index):
                    self.open_memo(mark.x1, mark.y1, mark.text, index=index)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if self._stroke is not None and event.button() == Qt.MouseButton.LeftButton:
            mark = self._stroke_mark(self.picture_rect())
            self._stroke = None
            if mark is not None:
                self._sheet.add(mark)
                self._changed(True, added=True)
            else:
                self.update()
        elif self._drag_from is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_to = event.position()
            mark = self._drag_mark(self.picture_rect())
            self._drag_from = None
            if mark is not None:
                self._sheet.add(mark)
                self._changed(True, added=True)
            else:
                self.update()
        self._erasing = False
        super().mouseReleaseEvent(event)

    def _stroke_mark(self, rect: QRectF) -> Mark | None:
        """The pen stroke just drawn as a mark, or ``None`` if it was a click."""
        stroke = self._stroke or []
        if rect.isEmpty() or len(stroke) < 2:
            return None
        length = sum(
            math.hypot(b.x() - a.x(), b.y() - a.y()) for a, b in zip(stroke, stroke[1:],
                                                                    strict=False)
        )
        if length < MIN_DRAG_PX:
            return None
        kept = simplify_path([(p.x(), p.y()) for p in stroke], PEN_TOLERANCE_PX)
        return Mark.pen((to_unit(QPointF(x, y), rect) for x, y in kept),
                        stored_ink(self._ink))

    def _drag_mark(self, rect: QRectF) -> Mark | None:
        """The mark the drag in progress would make, or ``None``."""
        if self._drag_from is None or rect.isEmpty():
            return None
        start, end = self._drag_from, self._drag_to
        dx, dy = end.x() - start.x(), end.y() - start.y()
        if abs(dx) < MIN_DRAG_PX and abs(dy) < MIN_DRAG_PX:
            return None
        x1, y1 = to_unit(start, rect)
        x2, y2 = to_unit(end, rect)
        if self._tool == TOOL_UNDERLINE:
            if abs(dy) <= abs(dx) * LEVEL_SNAP:
                y2 = y1
            return Mark.underline(x1, y1, x2, y2, stored_ink(self._ink))
        if self._tool == TOOL_HIGHLIGHT:
            if abs(dy) < LINE_HIGHLIGHT_BELOW_PX:
                # Dragged along a line of text: a band that covers the line.
                if abs(dx) < MIN_DRAG_PX:
                    return None
                half = LINE_HIGHLIGHT_HEIGHT / 2
                middle = (y1 + y2) / 2
                return Mark.highlight(x1, middle - half, x2, middle + half)
            return Mark.highlight(x1, y1, x2, y2)
        return None

    def _erase_at(self, point: QPointF, rect: QRectF) -> None:
        index = hit_test(self._sheet.marks, point, rect)
        if index >= 0:
            self._sheet.remove(index)
            self._changed(True)

    # -- memos ------------------------------------------------------------------

    @property
    def is_typing(self) -> bool:
        return self._memo_editor is not None

    def open_memo(self, x: float, y: float, text: str = "", *, index: int = -1) -> None:
        """Start typing a memo at ``(x, y)``, or change memo ``index``."""
        self.commit_memo()
        rect = self.picture_rect()
        if rect.isEmpty():
            return
        self._memo_at = (x, y)
        self._memo_index = index
        marks = self._sheet.marks
        self._memo_ink = (
            ink_of(marks[index]).name() if 0 <= index < len(marks) else self._ink
        )
        editor = _MemoEditor(self)
        editor.setFont(memo_font(rect))
        editor.setPlaceholderText("Type a memo, Enter to keep it")
        editor.setText(text)
        editor.committed.connect(self.commit_memo)
        editor.cancelled.connect(self.cancel_memo)
        self._memo_editor = editor
        self._style_memo_editor()
        self._place_memo_editor()
        editor.show()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    def _style_memo_editor(self) -> None:
        """Typed as it will look: no box, the memo's own ink, size and weight -
        set in the style sheet too, which would otherwise put back the
        application's font."""
        editor = self._memo_editor
        rect = self.picture_rect()
        if editor is None or rect.isEmpty():
            return
        font = memo_font(rect)
        pad = round(_memo_padding(font))
        editor.setStyleSheet(
            f"QLineEdit, QLineEdit:focus {{ background: transparent; border: none;"
            f" color: {self._memo_ink}; padding: {pad}px {max(0, pad - 2)}px;"
            f" font-size: {font.pixelSize()}px; font-weight: {int(font.weight())}; }}"
        )

    def _place_memo_editor(self) -> None:
        editor = self._memo_editor
        rect = self.picture_rect()
        if editor is None or rect.isEmpty():
            return
        editor.setFont(memo_font(rect))
        width = int(max(rect.width() * MEMO_MAX_WIDTH, 150))
        height = editor.sizeHint().height()
        point = to_pixels(*self._memo_at, rect)
        left = int(min(max(point.x(), rect.left()), rect.right() - width))
        top = int(min(max(point.y(), rect.top()), rect.bottom() - height))
        editor.setGeometry(max(0, left), max(0, top), width, height)

    def commit_memo(self) -> None:
        """Keep what is typed in the open memo; an emptied memo is removed."""
        editor = self._memo_editor
        if editor is None:
            return
        text = editor.text().strip()
        index = self._memo_index
        self._close_memo_editor()
        if index >= 0:
            if index >= len(self._sheet.marks):
                return
            if text:
                existing = self._sheet.marks[index]
                changed = replace(existing, text=text, colour=stored_ink(self._memo_ink))
                if changed != existing:
                    self._sheet.replace(index, changed)
                    self._changed(True)
            else:
                self._sheet.remove(index)
                self._changed(True)
        elif text:
            self._sheet.add(Mark.memo(*self._memo_at, text, stored_ink(self._memo_ink)))
            self._changed(True, added=True)

    def cancel_memo(self) -> None:
        if self._memo_editor is not None:
            self._close_memo_editor()

    def _close_memo_editor(self) -> None:
        editor = self._memo_editor
        self._memo_editor = None
        self._memo_index = -1
        if editor is not None:
            editor.hide()
            editor.deleteLater()
            if self.isVisible():
                self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.update()

    # -- keys -------------------------------------------------------------------

    def event(self, event: QEvent) -> bool:
        # Undo, redo and Esc belong to the marks while the canvas has the
        # focus; everything else (Space, the drawing keys) stays a window
        # shortcut.
        if event.type() == QEvent.Type.ShortcutOverride and self._editable:
            key_event = event  # a QKeyEvent
            assert isinstance(key_event, QKeyEvent)
            if self._claims(key_event):
                event.accept()
                return True
        return super().event(event)

    @staticmethod
    def _claims(event: QKeyEvent) -> bool:
        return _is_undo(event) or _is_redo(event) or event.key() == Qt.Key.Key_Escape

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if not self._editable:
            super().keyPressEvent(event)
            return
        if _is_undo(event):
            self.undo()
        elif _is_redo(event):
            self.redo()
        elif event.key() == Qt.Key.Key_Escape:
            if self._drag_from is not None or self._stroke is not None:
                self._drag_from = None
                self._stroke = None
                self.update()
            else:
                self.escapePressed.emit()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self._place_memo_editor()

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt naming
        self.commit_memo()
        self._drag_from = None
        self._stroke = None
        self._erasing = False
        super().hideEvent(event)


def _clamp(point: QPointF, rect: QRectF) -> QPointF:
    return QPointF(min(max(point.x(), rect.left()), rect.right()),
                   min(max(point.y(), rect.top()), rect.bottom()))


def _is_undo(event: QKeyEvent) -> bool:
    return event.matches(QKeySequence.StandardKey.Undo)


def _is_redo(event: QKeyEvent) -> bool:
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    return event.matches(QKeySequence.StandardKey.Redo) or (
        event.key() == Qt.Key.Key_Z and event.modifiers() == ctrl_shift
    )


class InkToolBar(QWidget):
    """The pen (free pen, underline, highlight, eraser), the memo, undo, redo
    and *Clear all*, for one :class:`InkCanvas`.

    The pen is one button: pressing it goes back to the kind of pen used last,
    its arrow chooses another, and it shows the one in use.
    """

    def __init__(self, canvas: InkCanvas, *, keys: bool = False,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._canvas = canvas
        self._keys = keys
        #: The kind of pen the pen button goes back to.
        self._pen = canvas.tool if canvas.tool in PEN_TOOLS else TOOL_PEN

        self._pen_menu = QMenu(self)
        self._pen_menu.setToolTipsVisible(True)
        group = QActionGroup(self)
        group.setExclusive(True)
        self._pen_actions: dict[str, QAction] = {}
        for tool in PEN_TOOLS:
            action = QAction(tool_icon(tool), TOOL_LABELS[tool], self._pen_menu)
            if keys:
                action.setShortcut(self._key(tool))
                # The player owns the keys; the menu only shows them.
                action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.setToolTip(TOOL_TIPS[tool])
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, t=tool: canvas.set_tool(t))
            group.addAction(action)
            self._pen_menu.addAction(action)
            self._pen_actions[tool] = action

        self._pen_button = self._tool_button()
        self._pen_button.setMenu(self._pen_menu)
        self._pen_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._pen_button.clicked.connect(lambda: self._choose(self._pen))
        self._memo_button = self._tool_button()
        self._memo_button.setIcon(tool_icon(TOOL_MEMO))
        self._memo_button.setAccessibleName(TOOL_LABELS[TOOL_MEMO])
        self._memo_button.setToolTip(self._tip(TOOL_MEMO))
        self._memo_button.clicked.connect(lambda: self._choose(TOOL_MEMO))

        self._ink_menu = QMenu(self)
        inks = QActionGroup(self)
        inks.setExclusive(True)
        self._ink_actions: dict[str, QAction] = {}
        for name, ink in INKS:
            action = QAction(ink_icon(ink), name, self._ink_menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, i=ink: canvas.set_ink(i))
            inks.addAction(action)
            self._ink_menu.addAction(action)
            self._ink_actions[ink] = action
        self._ink_button = QToolButton()
        self._ink_button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._ink_button.setMenu(self._ink_menu)
        self._ink_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._ink_button.setToolTip(
            "Ink colour of the pen, the underline and memos.\n"
            "Marks already drawn keep theirs; highlights stay yellow."
        )

        self._undo_button = QToolButton()
        self._undo_button.setIcon(history_icon(redo=False))
        self._undo_button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._undo_button.setAccessibleName("Undo")
        self._undo_button.setToolTip("Undo the last change to the marks (Ctrl+Z)")
        self._redo_button = QToolButton()
        self._redo_button.setIcon(history_icon(redo=True))
        self._redo_button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        self._redo_button.setAccessibleName("Redo")
        self._redo_button.setToolTip("Redo (Ctrl+Y)")
        self._clear_button = QToolButton()
        self._clear_button.setText("Clear all")
        self._clear_button.setToolTip("Remove every mark. Undo brings them back.")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._pen_button)
        layout.addWidget(self._memo_button)
        layout.addWidget(self._ink_button)
        layout.addSpacing(6)
        for button in (self._undo_button, self._redo_button, self._clear_button):
            layout.addWidget(button)
        layout.addStretch(1)

        self._undo_button.clicked.connect(canvas.undo)
        self._redo_button.clicked.connect(canvas.redo)
        self._clear_button.clicked.connect(canvas.clear_marks)
        canvas.toolChanged.connect(self._show_tool)
        canvas.inkChanged.connect(self._show_ink)
        canvas.marksChanged.connect(self.refresh)
        canvas.marksLoaded.connect(self.refresh)
        self._show_tool(canvas.tool)
        self._show_ink(canvas.ink)
        self.refresh()

    @staticmethod
    def _tool_button() -> QToolButton:
        button = QToolButton()
        button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setCheckable(True)
        return button

    @staticmethod
    def _key(tool: str) -> QKeySequence:
        return QKeySequence(str(TOOLS.index(tool) + 1))

    def _tip(self, tool: str) -> str:
        key = f" ({TOOLS.index(tool) + 1})" if self._keys else ""
        return f"{TOOL_LABELS[tool]}{key}\n{TOOL_TIPS[tool]}"

    @property
    def pen_button(self) -> QToolButton:
        return self._pen_button

    @property
    def memo_button(self) -> QToolButton:
        return self._memo_button

    def pen_action(self, tool: str) -> QAction:
        return self._pen_actions[tool]

    @property
    def ink_button(self) -> QToolButton:
        return self._ink_button

    def ink_action(self, ink: str) -> QAction:
        return self._ink_actions[ink]

    def _show_ink(self, ink: str) -> None:
        self._ink_button.setIcon(ink_icon(ink))
        action = self._ink_actions.get(ink)
        if action is not None:
            action.setChecked(True)
        name = next((n for n, i in INKS if i == ink), ink)
        self._ink_button.setAccessibleName(f"Ink: {name}")

    @property
    def clear_button(self) -> QToolButton:
        return self._clear_button

    def _choose(self, tool: str) -> None:
        self._canvas.set_tool(tool)
        # Choosing the tool already in use changes nothing on the canvas, but
        # the click has flipped the button: show the tool as it is.
        self._show_tool(self._canvas.tool)

    def _show_tool(self, tool: str) -> None:
        if tool in PEN_TOOLS:
            self._pen = tool
            self._pen_actions[tool].setChecked(True)
        self._pen_button.setIcon(tool_icon(self._pen))
        self._pen_button.setAccessibleName(TOOL_LABELS[self._pen])
        self._pen_button.setToolTip(
            f"{self._tip(self._pen)}\n\nThe arrow chooses free pen, underline, "
            "highlight or eraser."
        )
        # Checkable buttons flip themselves when clicked; the tool decides.
        self._pen_button.setChecked(tool in PEN_TOOLS)
        self._memo_button.setChecked(tool == TOOL_MEMO)

    def refresh(self) -> None:
        sheet = self._canvas.sheet
        self._undo_button.setEnabled(sheet.can_undo)
        self._redo_button.setEnabled(sheet.can_redo)
        self._clear_button.setEnabled(bool(sheet))
