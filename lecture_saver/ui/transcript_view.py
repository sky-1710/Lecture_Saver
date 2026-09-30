"""Transcript list view with inline editing.

A three-hour lecture produces on the order of 2,500 segments. This uses a
``QListView`` over a ``QAbstractListModel`` with a custom delegate, so Qt only
ever creates paint calls for the handful of rows actually on screen. Building a
real widget per segment - the obvious approach, and what a DOM-based UI does by
default - is what makes applications like this crawl on long lectures. The one
exception is the row being edited, which gets a real text editor for as long as
the edit lasts.

The view never changes the transcript itself. It reports what the user did -
typed text, asked to merge, split, delete - as signals, and the owner applies
those to the project's single :class:`TranscriptStore` (§14). That keeps every
edit undoable and every view consistent, whichever view it was made in.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QFont, QFontMetrics, QKeyEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QListView,
    QMenu,
    QPlainTextEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)

from ..core.models import TranscriptSegment
from ..core.timecode import format_hms
from . import theme

__all__ = ["TranscriptView", "TranscriptModel"]

# Roles beyond DisplayRole that the delegate needs.
ROLE_START = Qt.ItemDataRole.UserRole + 1
ROLE_SEGMENT = Qt.ItemDataRole.UserRole + 2
ROLE_IS_CURRENT = Qt.ItemDataRole.UserRole + 3
ROLE_IS_EDITED = Qt.ItemDataRole.UserRole + 4
ROLE_TRANSLATION = Qt.ItemDataRole.UserRole + 5

_PAD_X = 14
_PAD_Y = 9
_TIME_WIDTH = 74
_GAP = 10
#: Space between a line's text and its translation underneath.
_TRANSLATION_GAP = 3
_ACCENT = QColor(theme.ACCENT)
_ACCENT_SOFT = QColor(theme.ACCENT_SOFT)
_EDITED = QColor(theme.WARNING_TEXT)
_TRANSLATION = QColor(theme.TRANSLATION)


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
        #: Segment id -> imported Korean for that line. Display only.
        self._translations: dict[str, str] = {}

    def set_segments(self, segments: Sequence[TranscriptSegment]) -> None:
        self.beginResetModel()
        self._segments = segments
        self._current_row = -1
        self.endResetModel()

    def set_translations(self, translations: dict[str, str]) -> None:
        """Show each line's imported translation under it; ``{}`` for none."""
        if translations == self._translations:
            return
        self.beginResetModel()
        self._translations = dict(translations)
        self.endResetModel()

    def segment_at_row(self, row: int) -> TranscriptSegment | None:
        if 0 <= row < len(self._segments):
            return self._segments[row]
        return None

    def row_of(self, segment_id: str) -> int:
        for row, segment in enumerate(self._segments):
            if segment.id == segment_id:
                return row
        return -1

    def refresh_row(self, row: int) -> None:
        """The segment in ``row`` changed in place; repaint and re-measure it."""
        if 0 <= row < len(self._segments):
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)

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

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return segment.effective_text
        if role == ROLE_START:
            return segment.start
        if role == ROLE_SEGMENT:
            return segment
        if role == ROLE_IS_CURRENT:
            return index.row() == self._current_row
        if role == ROLE_IS_EDITED:
            return segment.is_edited
        if role == ROLE_TRANSLATION:
            return self._translations.get(segment.id, "")
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{format_hms(segment.start)} - {format_hms(segment.end)}"
            if segment.is_edited:
                original = segment.original_text or "(added by hand - no original)"
                tip += f"\nOriginal: {original}"
            return tip + "\nClick to jump here · double-click or F2 to edit"
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsEditable
        )

    def setData(  # noqa: N802 - Qt naming
        self,
        index: QModelIndex | QPersistentModelIndex,
        value: object,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        # Text is applied live through the store while typing, never here, so
        # the store stays the only writer.
        return False


class SegmentEditor(QPlainTextEdit):
    """The text box that replaces a row while it is being edited.

    Enter finishes, Escape puts the text back as it was, Ctrl+Enter splits the
    segment at the cursor. Shift+Enter is not a line break: a segment is one
    run of speech.
    """

    finishRequested = Signal()
    cancelRequested = Signal()
    splitRequested = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.setStyleSheet(
            f"background: {theme.EDIT_TINT}; border: none; padding: 0;"
        )
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        #: Text when editing began, for Escape.
        self.initial_text = ""
        #: The segment being edited.
        self.segment_id = ""

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.splitRequested.emit(self.textCursor().position())
            else:
                self.finishRequested.emit()
            return
        if key == Qt.Key.Key_Escape:
            self.cancelRequested.emit()
            return
        super().keyPressEvent(event)

    def _on_context_menu(self, point: QPoint) -> None:
        menu = self.createStandardContextMenu()
        menu.addSeparator()
        split = QAction("Split segment here\tCtrl+Enter", menu)
        split.triggered.connect(
            lambda: self.splitRequested.emit(self.cursorForPosition(point).position())
        )
        menu.addAction(split)
        menu.exec(self.mapToGlobal(point))


class SegmentDelegate(QStyledItemDelegate):
    """Paints one segment: timestamp gutter on the left, wrapped text beside it."""

    #: The user typed; carries the segment id and the whole new text.
    textEdited = Signal(str, str)
    #: Split the segment at a character offset of its text.
    splitRequested = Signal(str, int)

    def __init__(self, view: QListView) -> None:
        super().__init__(view)
        self._view = view
        self._time_font = QFont("Consolas")
        self._time_font.setPointSizeF(max(7.5, view.font().pointSizeF() - 1.0))
        self._marker_font = QFont(view.font())
        self._marker_font.setPointSizeF(max(7.0, view.font().pointSizeF() - 1.5))

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
        # An open editor needs a spare line so the text never jumps while typing.
        spare = QFontMetrics(opt.font).height() if self._is_editing(index) else 0
        height = self._text_rect(text, width, opt).height() + (_PAD_Y * 2) + spare
        height += self._translation_height(index, width, opt)
        return QSize(opt.rect.width(), max(44, height))

    def _translation_height(
        self,
        index: QModelIndex | QPersistentModelIndex,
        width: int,
        option: QStyleOptionViewItem,
    ) -> int:
        """Room for the line's translation under its text, 0 when there is none."""
        translation = str(index.data(ROLE_TRANSLATION) or "")
        if not translation:
            return 0
        return self._text_rect(translation, width, option).height() + _TRANSLATION_GAP

    def _is_editing(self, index: QModelIndex | QPersistentModelIndex) -> bool:
        view = self._view
        return (
            view.state() == QAbstractItemView.State.EditingState
            and view.currentIndex() == index
        )

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

        row = opt.rect.adjusted(4, 2, -4, -2)
        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_ACCENT_SOFT)
            painter.drawRoundedRect(row, 7, 7)
        elif current:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_ACCENT.red(), _ACCENT.green(), _ACCENT.blue(), 20))
            painter.drawRoundedRect(row, 7, 7)

        if current:
            # A 3 px accent bar makes "where playback is" readable at a glance,
            # even when the user has selected a different row.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(_ACCENT)
            painter.drawRoundedRect(
                QRect(row.left(), row.top() + 4, 3, row.height() - 8), 1.5, 1.5
            )

        # Timestamp gutter.
        start = float(index.data(ROLE_START) or 0.0)
        painter.setFont(self._time_font)
        if current:
            painter.setPen(_ACCENT)
        elif selected:
            painter.setPen(palette.color(QPalette.ColorRole.Text))
        else:
            painter.setPen(palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text))
        painter.drawText(
            QRect(opt.rect.left() + _PAD_X, opt.rect.top() + _PAD_Y + 1, _TIME_WIDTH, 18),
            int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop),
            format_hms(start),
        )

        if index.data(ROLE_IS_EDITED):
            painter.setFont(self._marker_font)
            painter.setPen(_EDITED)
            painter.drawText(
                QRect(opt.rect.left() + _PAD_X, opt.rect.top() + _PAD_Y + 19, _TIME_WIDTH, 16),
                int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop),
                "edited",
            )

        # Segment text.
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        painter.setFont(opt.font)
        # The selection fill is a lightened highlight, so ordinary text colour
        # reads better on it than the white meant for a full highlight.
        painter.setPen(palette.color(QPalette.ColorRole.Text))
        text_rect = self._editor_rect(opt, index)
        painter.drawText(
            text_rect,
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignTop),
            text,
        )

        # The imported translation, right under the line it translates.
        translation = str(index.data(ROLE_TRANSLATION) or "")
        if translation:
            painter.setPen(_TRANSLATION)
            painter.drawText(
                QRect(
                    text_rect.left(),
                    text_rect.bottom() + 1 + _TRANSLATION_GAP,
                    text_rect.width(),
                    opt.rect.bottom() - text_rect.bottom(),
                ),
                int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignTop),
                translation,
            )

        painter.restore()

    def _editor_rect(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QRect:
        """The line's own text; the translation, if any, sits below this."""
        width = self._text_width(option)
        below = self._translation_height(index, width, option)
        return QRect(
            option.rect.left() + _PAD_X + _TIME_WIDTH + _GAP,
            option.rect.top() + _PAD_Y,
            width,
            option.rect.height() - (_PAD_Y * 2) - below,
        )

    # -- editing ------------------------------------------------------------

    def createEditor(  # noqa: N802 - Qt naming
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QWidget:
        segment = index.data(ROLE_SEGMENT)
        editor = SegmentEditor(parent)
        editor.setFont(option.font)
        editor.document().setDocumentMargin(0)
        editor.initial_text = segment.effective_text if segment else ""
        editor.setPlainText(editor.initial_text)
        editor.moveCursor(editor.textCursor().MoveOperation.End)

        segment_id = segment.id if segment else ""
        editor.segment_id = segment_id
        editor.textChanged.connect(
            lambda: self.textEdited.emit(segment_id, editor.toPlainText())
        )
        editor.finishRequested.connect(lambda: self._close(editor))
        editor.cancelRequested.connect(lambda: self._cancel(editor))
        editor.splitRequested.connect(
            lambda offset: self._split(editor, segment_id, offset)
        )
        return editor

    def setEditorData(  # noqa: N802 - Qt naming
        self, editor: QWidget, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        # The editor is the source of truth while it is open; re-applying the
        # model's text here would move the cursor under the user's fingers.
        return

    def setModelData(  # noqa: N802 - Qt naming
        self,
        editor: QWidget,
        model: QAbstractItemModel,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        # Already applied live via textEdited.
        return

    def updateEditorGeometry(  # noqa: N802 - Qt naming
        self,
        editor: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        editor.setGeometry(self._editor_rect(option, index))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        # The base filter would close the editor on Escape and Tab before the
        # editor saw them; the editor handles its own keys.
        if event.type() == QEvent.Type.KeyPress and isinstance(watched, SegmentEditor):
            return False
        return super().eventFilter(watched, event)

    def _close(self, editor: SegmentEditor) -> None:
        self.closeEditor.emit(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _cancel(self, editor: SegmentEditor) -> None:
        if editor.toPlainText() != editor.initial_text:
            editor.setPlainText(editor.initial_text)
        self._close(editor)

    def _split(self, editor: SegmentEditor, segment_id: str, offset: int) -> None:
        self._close(editor)
        self.splitRequested.emit(segment_id, offset)


class TranscriptView(QListView):
    """Transcript list. Click seeks the video (§12); double-click or F2 edits (§13)."""

    seekRequested = Signal(float)
    textEdited = Signal(str, str)
    editingFinished = Signal(str)
    splitRequested = Signal(str, int)
    restoreRequested = Signal(str)
    mergeRequested = Signal(str)
    insertRequested = Signal(str)
    deleteRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._model = TranscriptModel(self)
        self.setModel(self._model)
        self._delegate = SegmentDelegate(self)
        self.setItemDelegate(self._delegate)

        self.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setUniformItemSizes(False)
        self.setWordWrap(True)
        self.setMouseTracking(True)
        self.setFrameShape(QListView.Shape.NoFrame)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        #: Suppresses follow-playback auto-scroll while the user is reading
        #: elsewhere; re-armed when they click a segment or playback is sought.
        self._follow_playback = True

        self.clicked.connect(self._on_clicked)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self._delegate.textEdited.connect(self._on_text_edited)
        self._delegate.splitRequested.connect(self.splitRequested)

    # -- content ------------------------------------------------------------

    def set_segments(self, segments: Sequence[TranscriptSegment]) -> None:
        self.finish_editing()
        self._model.set_segments(segments)
        self.scheduleDelayedItemsLayout()

    def set_translations(self, translations: dict[str, str]) -> None:
        """Segment id -> Korean, painted under each line. Not part of the text."""
        if self.is_editing:
            self.finish_editing()
        self._model.set_translations(translations)
        self.scheduleDelayedItemsLayout()

    def clear_segments(self) -> None:
        self.set_segments(())

    @property
    def segment_count(self) -> int:
        return self._model.rowCount()

    def refresh_segment(self, segment_id: str) -> None:
        """A segment's text changed in place: repaint it and re-measure heights."""
        row = self._model.row_of(segment_id)
        if row >= 0:
            self._model.refresh_row(row)
            self.scheduleDelayedItemsLayout()

    def row_of(self, segment_id: str) -> int:
        return self._model.row_of(segment_id)

    # -- editing ------------------------------------------------------------

    @property
    def is_editing(self) -> bool:
        return self.state() == QAbstractItemView.State.EditingState

    def edit_segment(self, segment_id: str) -> bool:
        """Open the inline editor on a segment, e.g. one just added."""
        row = self._model.row_of(segment_id)
        if row < 0:
            return False
        index = self._model.index(row, 0)
        self.setCurrentIndex(index)
        self.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)
        self.edit(index)
        return self.is_editing

    def finish_editing(self) -> None:
        """Close an open editor. Its text is already in the store."""
        if not self.is_editing:
            return
        index = self.currentIndex()
        editor = self.indexWidget(index) if index.isValid() else None
        if editor is None:
            focus = self.viewport().focusWidget()
            editor = focus if isinstance(focus, SegmentEditor) else None
        if editor is not None:
            self.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _on_text_edited(self, segment_id: str, text: str) -> None:
        self.textEdited.emit(segment_id, text)
        # Grow or shrink the row as the text wraps onto more or fewer lines.
        self.refresh_segment(segment_id)

    def closeEditor(  # noqa: N802 - Qt naming
        self, editor: QWidget, hint: QAbstractItemDelegate.EndEditHint
    ) -> None:
        # Every way an edit ends - Enter, Escape, focus leaving, a rebind -
        # passes through here, synchronously, unlike the editor's destruction.
        segment_id = editor.segment_id if isinstance(editor, SegmentEditor) else ""
        was_editing = self.is_editing
        super().closeEditor(editor, hint)
        if segment_id and was_editing:
            self.editingFinished.emit(segment_id)
            self.refresh_segment(segment_id)

    # -- synchronisation ----------------------------------------------------

    def highlight_row(self, row: int, *, scroll: bool = True) -> None:
        self._model.set_current_row(row)
        # Never pull the list away from a row the user is typing in.
        if scroll and row >= 0 and self._follow_playback and not self.is_editing:
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
        if self.is_editing:
            return
        segment = self._model.segment_at_row(index.row())
        if segment is not None:
            self.seekRequested.emit(segment.start)

    def _on_context_menu(self, point: QPoint) -> None:
        index = self.indexAt(point)
        segment = self._model.segment_at_row(index.row()) if index.isValid() else None
        if segment is None:
            return
        self.finish_editing()
        self.setCurrentIndex(index)

        menu = QMenu(self)
        edit = menu.addAction("Edit text\tF2")
        restore = menu.addAction("Restore original transcription")
        restore.setEnabled(segment.is_edited)
        menu.addSeparator()
        merge = menu.addAction("Merge with next segment")
        merge.setEnabled(index.row() + 1 < self._model.rowCount())
        insert = menu.addAction("Add segment after")
        menu.addSeparator()
        delete = menu.addAction("Delete segment")

        chosen = menu.exec(self.viewport().mapToGlobal(point))
        if chosen is edit:
            self.edit_segment(segment.id)
        elif chosen is restore:
            self.restoreRequested.emit(segment.id)
        elif chosen is merge:
            self.mergeRequested.emit(segment.id)
        elif chosen is insert:
            self.insertRequested.emit(segment.id)
        elif chosen is delete:
            self.deleteRequested.emit(segment.id)

    def resizeEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        # Row heights depend on the wrapped text width, so they must be
        # recomputed whenever the viewport changes width.
        super().resizeEvent(event)  # type: ignore[arg-type]
        self.scheduleDelayedItemsLayout()
