"""A concept map as a picture for the screen.

The layout is the one the PDF uses (:mod:`lecture_saver.export.flowchart`), so
the lecture and the PDF show the same drawing. Only the drawing itself differs:
the PDF has no painter and the screen has no ReportLab.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)

from ..export.flowchart import (
    LABEL_WIDTH,
    PAD_X,
    PAD_Y,
    TEXT_WIDTH,
    Flowchart,
    Layout,
    Route,
    best_fit,
    is_readable,
)
from . import theme

__all__ = ["chart_picture"]

_BOX_FLAGS = Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap
_LINE = QColor("#6B7280")


def _fonts() -> tuple[QFont, QFont]:
    box, label = QFont(), QFont()
    box.setPointSizeF(9.0)
    label.setPointSizeF(8.0)
    return box, label


def _measure(label: str) -> tuple[float, float]:
    font, _ = _fonts()
    room = QRectF(0, 0, TEXT_WIDTH, 10000)
    size = QFontMetricsF(font).boundingRect(room, _BOX_FLAGS, label)
    return size.width() + 2 * PAD_X + 2, size.height() + 2 * PAD_Y


def _draw_route(painter: QPainter, route: Route) -> None:
    points = [QPointF(x, y) for x, y in route.points]
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(_LINE, 0.9))
    painter.drawPath(path)
    if not route.arrow:
        return
    tip, start = points[-1], points[-2]
    along = tip - start
    length = max((along.x() ** 2 + along.y() ** 2) ** 0.5, 1.0)
    unit = QPointF(along.x() / length, along.y() / length)
    back = tip - unit * 7
    side = QPointF(-unit.y(), unit.x()) * 3.2
    painter.setBrush(QBrush(_LINE))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPolygon(QPolygonF([tip, back + side, back - side]))


def _draw_label(painter: QPainter, route: Route, font: QFont) -> None:
    middle = (len(route.points) - 1) // 2
    (x1, y1), (x2, y2) = route.points[middle], route.points[middle + 1]
    centre = QPointF((x1 + x2) / 2, (y1 + y2) / 2)
    size = QFontMetricsF(font).boundingRect(
        QRectF(0, 0, LABEL_WIDTH, 1000), _BOX_FLAGS, route.label
    )
    place = QRectF(centre.x() - size.width() / 2, centre.y() - size.height() / 2,
                   size.width(), size.height())
    painter.setFont(font)
    painter.fillRect(place.adjusted(-1, -1, 1, 1), QColor("white"))
    painter.setPen(QColor(theme.ACCENT))
    painter.drawText(place, _BOX_FLAGS, route.label)


def _draw(painter: QPainter, chart: Flowchart, layout: Layout) -> None:
    box_font, label_font = _fonts()
    for route in layout.routes:
        _draw_route(painter, route)
    painter.setFont(box_font)
    for node, box in layout.boxes.items():
        rect = QRectF(box.x - box.width / 2, box.y - box.height / 2, box.width, box.height)
        painter.setBrush(QBrush(QColor(theme.ACCENT_SOFT)))
        painter.setPen(QPen(QColor(theme.ACCENT), 0.8))
        painter.drawRoundedRect(rect, 5, 5)
        painter.setPen(QColor(theme.TEXT))
        painter.drawText(rect.adjusted(PAD_X, PAD_Y, -PAD_X, -PAD_Y), _BOX_FLAGS,
                         chart.nodes[node])
    for route in layout.routes:
        if route.label:
            _draw_label(painter, route, label_font)


def chart_picture(
    chart: Flowchart, width: float, height: float, *, pixel_ratio: float = 1.0
) -> QImage | None:
    """``chart`` within ``width`` by ``height``, or ``None`` if too big to read.

    Drawn at the size it will be shown, ``pixel_ratio`` being the screen's: a
    text browser's scaling of a picture leaves small Korean text unreadable.
    """
    layout, scale, drawn = best_fit(chart, width, height, _measure)
    if not is_readable(scale):
        return None
    pixels = scale * pixel_ratio
    # A bit of room: a line or a box's edge can sit right on the layout's edge.
    margin = 2
    picture = QImage(
        round((layout.width + 2 * margin) * pixels),
        round((layout.height + 2 * margin) * pixels),
        QImage.Format.Format_RGB32,
    )
    picture.fill(QColor("white"))
    painter = QPainter(picture)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.scale(pixels, pixels)
    painter.translate(margin, margin)
    _draw(painter, drawn, layout)
    painter.end()
    picture.setDevicePixelRatio(pixel_ratio)
    return picture
