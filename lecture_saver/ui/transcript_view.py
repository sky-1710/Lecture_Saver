"""Transcript list view.

A three-hour lecture produces on the order of 2,500 segments. This uses a
``QListView`` over a ``QAbstractListModel`` with a custom delegate, so Qt only
ever creates paint calls for the handful of rows actually on screen. Building a
real widget per segment - the obvious approach, and what a DOM-based UI does by
default - is what makes applications like this crawl on long lectures.

Phase 2 scope: display, click-to-seek, and follow-playback highlighting (§12).
Inline editing is Phase 5; the view is explicitly read-only until then.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ..core.models import TranscriptSegment
from ..core.timecode import format_hms

__all__ = ["TranscriptView", "TranscriptModel"]

# Roles beyond DisplayRole that the delegate needs.
ROLE_START = Qt.ItemDataRole.UserRole + 1
ROLE_SEGMENT = Qt.ItemDataRole.UserRole + 2
ROLE_IS_CURRENT = Qt.ItemDataRole.UserRole + 3

_PAD_X = 12
_PAD_Y = 8
_TIME_WIDTH = 74
_GAP = 10


class TranscriptModel(QAbstractListModel):
    """Adapts a sequence of segments to Qt's model interface.

    Holds a reference to the segments owned by the project's
    :class:`~lecture_saver.core.transcript_store.TranscriptStore`; it never
    copies the text, so there is still exactly one transcript (§14).
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._segments: Sequence[TranscriptSegment] = ()
        self._current_row = -1

    def set_segments(self, segments: Sequence[TranscriptSegment]) -> None:
        self.beginResetModel()
        self._segments = segments
        self._current_row = -1
        self.endResetModel()

    def segment_at_row(self, row: int) -> TranscriptSegment | None:
        if 0 <= row < len(self._segments):
            return self._segments[row]
        return None

    @property
    def current_row(self) -> int:
        return self._current_row

    def set_current_row(self, row: int) -> None:
        """Move the playback highlight, repainting only the two rows involved."""
        if row == self._current_row:
            return
        previous = self._current_row
        self._current_row = row
        for changed in (previous, row):
            if 0 <= changed < len(self._segments):
                index = self.index(changed, 0)
                self.dataChanged.emit(index, index, [ROLE_IS_CURRENT])

    # -- QAbstractListModel -------------------------------------------------

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._segments)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        segment = self._segments[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return segment.effective_text
        if role == ROLE_START:
            return segment.start
        if role == ROLE_SEGMENT:
            return segment
        if role == ROLE_IS_CURRENT:
            return index.row() == self._current_row
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{format_hms(segment.start)} - {format_hms(segment.end)}\nClick to jump here"
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        # Not editable yet - inline editing arrives in Phase 5.
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class SegmentDelegate(QStyledItemDelegate):
    """Paints one segment: timestamp gutter on the left, wrapped text beside it."""

    def __init__(self, view: QListView) -> None:
        super().__init__(view)
        self._view = view
        self._time_font = QFont("Consolas")
        self._time_font.setPointSizeF(max(7.5, view.font().pointSizeF() - 1.0))

    def _text_width(self, option: QStyleOptionViewItem) -> int:
        width = option.rect.width()
        if width <= 0:
            width = self._view.viewport().width()
        return max(80, width - (_PAD_X * 2) - _TIME_WIDTH - _GAP)

    def _text_rect(self, text: str, width: int, option: QStyleOptionViewItem) -> QRect:
        metrics = QFontMetrics(option.font)
        return metrics.boundingRect(
            QRect(0, 0, width, 10_000),
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignTop),
            text or " ",
        )

    def sizeHint(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QSize:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        width = self._text_width(opt)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        height = self._text_rect(text, width, opt).height() + (_PAD_Y * 2)
        return QSize(opt.rect.width(), max(40, height))

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        palette = opt.palette
        selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        current = bool(index.data(ROLE_IS_CURRENT))

        if selected:
            painter.fillRect(opt.rect, palette.color(QPalette.ColorRole.Highlight).lighter(160))
        elif current:
            painter.fillRect(opt.rect, QColor(0x2E, 0x6D, 0xF6, 26))

        if current:
            # A 3 px accent bar makes "where playback is" readable at a glance,
            # even when the user has selected a different row.
            painter.fillRect(
                QRect(opt.rect.left(), opt.rect.top() + 4, 3, opt.rect.height() - 8),
                QColor(0x2E, 0x6D, 0xF6),
            )

        # Timestamp gutter.
        start = float(index.data(ROLE_START) or 0.0)
        painter.setFont(self._time_font)
        painter.setPen(
            palette.color(QPalette.ColorRole.Highlight)
            if current
            else palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text)
        )
        painter.drawText(
            QRect(opt.rect.left() + _PAD_X, opt.rect.top() + _PAD_Y + 1, _TIME_WIDTH, 18),
            int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop),
            format_hms(start),
        )

        # Segment text.
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setFont(opt.font)
        painter.setPen(
            palette.color(QPalette.ColorRole.HighlightedText)
            if selected
            else palette.color(QPalette.ColorRole.Text)
        )
        text_left = opt.rect.left() + _PAD_X + _TIME_WIDTH + _GAP
        painter.drawText(
            QRect(
                text_left,
                opt.rect.top() + _PAD_Y,
                self._text_width(opt),
                opt.rect.height() - (_PAD_Y * 2),
            ),
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignTop),
            text,
        )

        painter.restore()


class TranscriptView(QListView):
    """Read-only transcript list. Clicking a segment seeks the video (§12)."""

    seekRequested = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = TranscriptModel(self)
        self.setModel(self._model)
        self.setItemDelegate(SegmentDelegate(self))

        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        self.setMouseTracking(True)
        self.setFrameShape(QListView.Shape.NoFrame)

        #: Suppresses follow-playback auto-scroll while the user is reading
        #: elsewhere; re-armed when they click a segment or playback is sought.
        self._follow_playback = True

        self.clicked.connect(self._on_clicked)

    # -- content ------------------------------------------------------------

    def set_segments(self, segments: Sequence[TranscriptSegment]) -> None:
        self._model.set_segments(segments)
        self.scheduleDelayedItemsLayout()

    def clear_segments(self) -> None:
        self._model.set_segments(())

    @property
    def segment_count(self) -> int:
        return self._model.rowCount()

    # -- synchronisation ----------------------------------------------------

    def highlight_row(self, row: int, *, scroll: bool = True) -> None:
        self._model.set_current_row(row)
        if scroll and row >= 0 and self._follow_playback:
            self.scrollTo(
                self._model.index(row, 0),
                QAbstractItemView.ScrollHint.EnsureVisible,
            )

    def select_row(self, row: int) -> None:
        if row < 0:
            return
        index = self._model.index(row, 0)
        self.setCurrentIndex(index)
        self.scrollTo(index, QAbstractItemView.ScrollHint.PositionAtCenter)

    def set_follow_playback(self, follow: bool) -> None:
        self._follow_playback = follow

    # -- events -------------------------------------------------------------

    def _on_clicked(self, index: QModelIndex) -> None:
        segment = self._model.segment_at_row(index.row())
        if segment is not None:
            self.seekRequested.emit(segment.start)

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        # Row heights depend on the wrapped text width, so they must be
        # recomputed whenever the viewport changes width.
        super().resizeEvent(event)  # type: ignore[arg-type]
        self.scheduleDelayedItemsLayout()
