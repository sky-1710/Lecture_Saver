"""Transcript list view with inline editing.

A three-hour lecture produces on the order of 2,500 segments. This uses a
``QListView`` over a ``QAbstractListModel`` with a custom delegate, so Qt only
ever creates paint calls for the handful of rows actually on screen. Building a
real widget per segment - the obvious approach, and what a DOM-based UI does by
default - is what makes applications like this crawl on long lectures. The one
exception is the row being edited, which gets a real text editor for as long as
the edit lasts.

Each line can also be asked about: an "Ask AI" button appears in the
timestamp gutter of the line under the mouse and of the selected line, and the
right-click menu has the same. The view only reports which line (or which
words of it) - the lecture view decides what asking means.

The words can be selected and copied as in a document, though no row is a text
widget: a drag over them selects, across lines and their translations, and
Ctrl+C copies. Each row's text is laid out once by :func:`text_layout` for
painting, measuring and finding the letter under the mouse, so the three agree.

Words the assistant doubts carry a wavy underline, as a spelling checker's do,
with the suggestion in the line's tooltip and the choice in its right-click
menu. The underline is found in the text as each row is painted, so it goes
the moment the words do, even while they are being typed over.

The view never changes the transcript itself. It reports what the user did -
typed text, asked to merge, split, delete - as signals, and the owner applies
those to the project's single :class:`TranscriptStore` (§14). That keeps every
edit undoable and every view consistent, whichever view it was made in.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from PySide6.QtCore import (
    QAbstractItemModel,
    QAbstractListModel,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
    QHelpEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPalette,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import (
    QAbstractItemDelegate,
    QAbstractItemView,
    QApplication,
    QListView,
    QMenu,
    QPlainTextEdit,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QWidget,
)

from ..core.corrections import Correction
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
#: The "Ask AI" button, under the timestamp. Fits the shortest row (44 px).
_ASK_TOP = 18
_ASK_WIDTH = 52
_ASK_HEIGHT = 16
ASK_LABEL = "Ask AI"
_ACCENT = QColor(theme.ACCENT)
_ACCENT_SOFT = QColor(theme.ACCENT_SOFT)
_EDITED = QColor(theme.WARNING_TEXT)
#: The wavy underline under words the assistant is sure of, and those it doubts.
_TYPO_SURE = QColor(theme.DANGER)
_TYPO_DOUBT = QColor(theme.WARNING_TEXT)
_TRANSLATION = QColor(theme.TRANSLATION)
_SURFACE = QColor(theme.SURFACE)
#: Selected words, in the selection colours of the theme's text boxes.
_SELECTION = QColor(theme.ACCENT)
_SELECTED_TEXT = QColor("#FFFFFF")

#: The parts of a row whose words can be selected: its text, the translation.
PART_TEXT = 0
PART_TRANSLATION = 1


def text_layout(text: str, font: QFont, width: int) -> QTextLayout:
    """``text`` wrapped to ``width`` as a row shows it.

    Lines are spaced as ``QPainter.drawText`` spaces them - the font's
    leading between them, each on a whole pixel - which the rows used before
    they could be selected, so they look as they did.
    """
    layout = QTextLayout(text, font)
    option = QTextOption()
    # A word too long for the row (a link, say) breaks rather than spill out.
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    leading = QFontMetricsF(font).leading()
    y = -leading
    layout.beginLayout()
    while True:
        line = layout.createLine()
        if not line.isValid():
            break
        line.setLineWidth(width)
        y = math.ceil(y + leading)
        line.setPosition(QPointF(0, y))
        y += line.height()
    layout.endLayout()
    return layout


def layout_height(layout: QTextLayout) -> int:
    count = layout.lineCount()
    if count == 0:
        return 0
    last = layout.lineAt(count - 1)
    return math.ceil(last.y() + last.height())


def _typo_marks(
    corrections: Sequence[Correction], text: str
) -> list[QTextLayout.FormatRange]:
    """A wavy underline under the words each suggestion is about."""
    marks = []
    for correction in corrections:
        for start, end in correction.spans(text):
            mark = QTextLayout.FormatRange()
            mark.start, mark.length = start, end - start
            look = QTextCharFormat()
            look.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            look.setUnderlineColor(_TYPO_SURE if correction.sure else _TYPO_DOUBT)
            mark.format = look
            marks.append(mark)
    return marks


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
        #: Segment id -> what the assistant doubts in that line. Display only.
        self._corrections: dict[str, tuple[Correction, ...]] = {}

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

    def set_corrections(self, corrections: dict[str, tuple[Correction, ...]]) -> None:
        """Mark the words each line's suggestions are about; ``{}`` for none.

        Only repaints: the marks change no row's height.
        """
        if corrections == self._corrections:
            return
        self._corrections = dict(corrections)
        if self._segments:
            last = self.index(len(self._segments) - 1, 0)
            self.dataChanged.emit(self.index(0, 0), last, [Qt.ItemDataRole.ToolTipRole])

    def corrections_at(self, row: int) -> tuple[Correction, ...]:
        segment = self.segment_at_row(row)
        return self._corrections.get(segment.id, ()) if segment is not None else ()

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
            for correction in self._corrections.get(segment.id, ()):
                doubt = "" if correction.sure else " (not sure)"
                tip += f"\nPossibly “{correction.right}”, not “{correction.wrong}”{doubt}"
                if correction.reason:
                    tip += f": {correction.reason}"
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
    #: Ask the assistant about the selected words.
    askRequested = Signal(str)

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
        #: Whether the right-click menu offers asking about a selection.
        self.can_ask = False

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
        selected = self.textCursor().selectedText().strip()
        if self.can_ask and selected:
            shown = selected if len(selected) <= 24 else selected[:23] + "…"
            ask = QAction(f"Ask AI about “{shown}”", menu)
            ask.triggered.connect(lambda: self.askRequested.emit(selected))
            menu.addAction(ask)
        menu.exec(self.mapToGlobal(point))


class SegmentDelegate(QStyledItemDelegate):
    """Paints one segment: timestamp gutter on the left, wrapped text beside it."""

    #: The user typed; carries the segment id and the whole new text.
    textEdited = Signal(str, str)
    #: Split the segment at a character offset of its text.
    splitRequested = Signal(str, int)
    #: Ask about words selected in the editor: segment id, the words.
    askRequested = Signal(str, str)

    def __init__(self, view: QListView) -> None:
        super().__init__(view)
        self._view = view
        self._time_font = QFont("Consolas")
        self._time_font.setPointSizeF(max(7.5, view.font().pointSizeF() - 1.0))
        self._marker_font = QFont(view.font())
        self._marker_font.setPointSizeF(max(7.0, view.font().pointSizeF() - 1.5))

    @staticmethod
    def ask_rect(row_rect: QRect) -> QRect:
        """Where a row's "Ask AI" button is: in the gutter, under the time."""
        return QRect(
            row_rect.left() + _PAD_X, row_rect.top() + _PAD_Y + _ASK_TOP, _ASK_WIDTH, _ASK_HEIGHT
        )

    def _paint_ask(self, painter: QPainter, row_rect: QRect, hovered: bool) -> None:
        rect = self.ask_rect(row_rect)
        painter.setPen(_ACCENT)
        painter.setBrush(_ACCENT if hovered else _SURFACE)
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 7, 7)
        painter.setFont(self._marker_font)
        painter.setPen(_SURFACE if hovered else _ACCENT)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), ASK_LABEL)

    def _text_width(self, option: QStyleOptionViewItem) -> int:
        width = option.rect.width()
        if width <= 0:
            width = self._view.viewport().width()
        return max(80, width - (_PAD_X * 2) - _TIME_WIDTH - _GAP)

    @staticmethod
    def _text_height(text: str, width: int, option: QStyleOptionViewItem) -> int:
        return layout_height(text_layout(text or " ", option.font, width))

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
        height = self._text_height(text, width, opt) + (_PAD_Y * 2) + spare
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
        return self._text_height(translation, width, option) + _TRANSLATION_GAP

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

        view = self._view
        if isinstance(view, TranscriptView) and view.shows_ask_button(index.row()):
            # Takes the "edited" marker's place; the tooltip still says so.
            self._paint_ask(painter, opt.rect, view.ask_button_hovered(index.row()))
        elif index.data(ROLE_IS_EDITED):
            painter.setFont(self._marker_font)
            painter.setPen(_EDITED)
            painter.drawText(
                QRect(opt.rect.left() + _PAD_X, opt.rect.top() + _PAD_Y + 19, _TIME_WIDTH, 16),
                int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop),
                "edited",
            )

        # Segment text, and the imported translation right under it.
        painter.setFont(opt.font)
        for part, words, layout, origin in self._parts(opt, index):
            # The selection fill is a lightened highlight, so ordinary text
            # colour reads better on it than the white meant for a full one.
            painter.setPen(
                palette.color(QPalette.ColorRole.Text) if part == PART_TEXT else _TRANSLATION
            )
            chosen_words = []
            if part == PART_TEXT and isinstance(view, TranscriptView):
                chosen_words += _typo_marks(view.corrections_at(index.row()), words)
            span = (
                view.selected_span(index.row(), part, len(words))
                if isinstance(view, TranscriptView) else None
            )
            if span is not None:
                chosen = QTextLayout.FormatRange()
                chosen.start, chosen.length = span[0], span[1] - span[0]
                look = QTextCharFormat()
                look.setBackground(_SELECTION)
                look.setForeground(_SELECTED_TEXT)
                chosen.format = look
                chosen_words.append(chosen)
            layout.draw(painter, origin, chosen_words)

        painter.restore()

    def _parts(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> list[tuple[int, str, QTextLayout, QPointF]]:
        """The row's text and its translation (when it has one) as laid out on
        screen: which part, its words, their layout, and the layout's top left."""
        text_rect = self._editor_rect(option, index)
        width = text_rect.width()
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        parts = [(PART_TEXT, text, text_layout(text, option.font, width),
                  QPointF(text_rect.topLeft()))]
        translation = str(index.data(ROLE_TRANSLATION) or "")
        if translation:
            top = text_rect.top() + text_rect.height() + _TRANSLATION_GAP
            parts.append((PART_TRANSLATION, translation,
                          text_layout(translation, option.font, width),
                          QPointF(text_rect.left(), top)))
        return parts

    def position_at(
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
        point: QPointF,
    ) -> tuple[int, int, bool]:
        """Where ``point`` falls among the row's words: the part, the letter
        before which it is, and whether it is on the words themselves.

        A point beside the words is the nearest place in them - left of a line
        its start, right of it its end, above or below, the line nearest.
        """
        parts = self._parts(option, index)
        part, words, layout, origin = parts[0]
        for candidate in parts[1:]:
            # The gap between the text and its translation is halved.
            if point.y() >= candidate[3].y() - _TRANSLATION_GAP / 2:
                part, words, layout, origin = candidate
        x, y = point.x() - origin.x(), point.y() - origin.y()
        line = layout.lineAt(0)
        for number in range(layout.lineCount()):
            line = layout.lineAt(number)
            if y < line.y() + line.height():
                break
        letter = min(line.xToCursor(x), len(words))
        on_words = 0 <= x <= line.width() and 0 <= y < layout_height(layout) and bool(words)
        return part, letter, on_words

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
        if isinstance(self._view, TranscriptView):
            # However the edit began (F2, the menu): words are selected in it
            # from now on, not across the list.
            self._view.clear_text_selection()
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
        editor.can_ask = isinstance(self._view, TranscriptView) and self._view.ask_enabled
        editor.askRequested.connect(lambda words: self.askRequested.emit(segment_id, words))
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
    #: Ask the assistant about a line: segment id, and the words picked out
    #: of it ("" for the whole line).
    askRequested = Signal(str, str)
    #: Have the assistant explain a line straight away.
    explainRequested = Signal(str)
    #: Accept a suggestion for a line (a Correction).
    fixRequested = Signal(object)
    #: Leave the line as it is (a Correction).
    keepRequested = Signal(object)

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
        #: Whether lines offer "Ask AI". Off until someone handles the request.
        self._ask_enabled = False
        #: The row under the mouse, and whether the mouse is on its button.
        self._hover_row = -1
        self._hover_ask = False
        #: A press on an "Ask AI" button: its row, so the release can match it.
        self._ask_press_row: int | None = None
        #: Words selected by dragging over them: where the drag began and
        #: where it is, each (row, part, letter). Both None when none are.
        self._text_anchor: tuple[int, int, int] | None = None
        self._text_focus: tuple[int, int, int] | None = None
        #: Where the left button went down, while it is down: a press that
        #: moves far enough selects words instead of clicking.
        self._press_pos: QPoint | None = None
        self._dragging_text = False
        #: The press just let go of selected words: it was no click to seek.
        self._chose_words = False
        #: Scrolls the list while words are dragged past its top or bottom.
        self._drag_scroll = QTimer(self)
        self._drag_scroll.setInterval(30)
        self._drag_scroll.timeout.connect(self._on_drag_scroll)

        self.clicked.connect(self._on_clicked)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self._delegate.textEdited.connect(self._on_text_edited)
        self._delegate.splitRequested.connect(self.splitRequested)
        self._delegate.askRequested.connect(self.askRequested)

    # -- content ------------------------------------------------------------

    def set_segments(self, segments: Sequence[TranscriptSegment]) -> None:
        self.finish_editing()
        self._hover_row, self._hover_ask = -1, False
        self.clear_text_selection()
        self._model.set_segments(segments)
        self.scheduleDelayedItemsLayout()

    def set_translations(self, translations: dict[str, str]) -> None:
        """Segment id -> Korean, painted under each line. Not part of the text."""
        if self.is_editing:
            self.finish_editing()
        self.clear_text_selection()
        self._model.set_translations(translations)
        self.scheduleDelayedItemsLayout()

    def set_corrections(self, corrections: dict[str, tuple[Correction, ...]]) -> None:
        """Segment id -> what the assistant doubts in it, marked in the line."""
        self._model.set_corrections(corrections)

    def corrections_at(self, row: int) -> tuple[Correction, ...]:
        return self._model.corrections_at(row)

    def clear_segments(self) -> None:
        self.set_segments(())

    @property
    def segment_count(self) -> int:
        return self._model.rowCount()

    def refresh_segment(self, segment_id: str) -> None:
        """A segment's text changed in place: repaint it and re-measure heights."""
        row = self._model.row_of(segment_id)
        if row >= 0:
            # Letters selected in a line that changed may no longer be there.
            self.clear_text_selection()
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
        editor = self._open_editor()
        if editor is not None:
            self.closeEditor(editor, QAbstractItemDelegate.EndEditHint.NoHint)

    def _open_editor(self) -> QWidget | None:
        if not self.is_editing:
            return None
        index = self.currentIndex()
        editor = self.indexWidget(index) if index.isValid() else None
        if editor is None:
            focus = self.viewport().focusWidget()
            editor = focus if isinstance(focus, SegmentEditor) else None
        return editor

    # -- text size ----------------------------------------------------------

    @property
    def text_size(self) -> float:
        """Point size of the lines and their translations."""
        return self.font().pointSizeF()

    def set_text_size(self, points: float) -> None:
        """Show the lines, and the translations under them, at ``points``.

        The times and the "Ask AI" button keep their size: the gutter is as
        wide as a time, whatever the text beside it.
        """
        if abs(points - self.text_size) < 1e-9:
            return
        font = QFont(self.font())
        font.setPointSizeF(points)
        self.setFont(font)
        # A line being typed in grows with the rest, without closing.
        editor = self._open_editor()
        if editor is not None:
            editor.setFont(font)
        self.scheduleDelayedItemsLayout()

    # -- selecting words ----------------------------------------------------

    @property
    def has_text_selection(self) -> bool:
        return self._selection_bounds() is not None

    def _selection_bounds(
        self,
    ) -> tuple[tuple[int, int, int], tuple[int, int, int]] | None:
        anchor, focus = self._text_anchor, self._text_focus
        if anchor is None or focus is None or anchor == focus:
            return None
        return (anchor, focus) if anchor < focus else (focus, anchor)

    def selected_span(self, row: int, part: int, length: int) -> tuple[int, int] | None:
        """The letters selected in one part of a row, as (start, end), or None."""
        bounds = self._selection_bounds()
        if bounds is None:
            return None
        start, end = bounds
        if (row, part) < start[:2] or (row, part) > end[:2]:
            return None
        first = start[2] if (row, part) == start[:2] else 0
        last = end[2] if (row, part) == end[:2] else length
        return (first, last) if first < last else None

    def _parts_of(self, row: int) -> list[tuple[int, str]]:
        index = self._model.index(row, 0)
        parts = [(PART_TEXT, str(index.data(Qt.ItemDataRole.DisplayRole) or ""))]
        translation = str(index.data(ROLE_TRANSLATION) or "")
        if translation:
            parts.append((PART_TRANSLATION, translation))
        return parts

    @property
    def selected_text(self) -> str:
        """The words selected, a line of the transcript or of a translation
        to each line; "" when none are."""
        bounds = self._selection_bounds()
        if bounds is None:
            return ""
        pieces = []
        for row in range(bounds[0][0], bounds[1][0] + 1):
            for part, words in self._parts_of(row):
                span = self.selected_span(row, part, len(words))
                if span is not None:
                    pieces.append(words[span[0]:span[1]])
        return "\n".join(pieces)

    def row_text(self, row: int) -> str:
        """A row as it reads: its line, and its translation under it."""
        if not 0 <= row < self._model.rowCount():
            return ""
        return "\n".join(words for _part, words in self._parts_of(row) if words)

    def select_all_text(self) -> None:
        rows = self._model.rowCount()
        if rows == 0:
            return
        part, words = self._parts_of(rows - 1)[-1]
        self._text_anchor, self._text_focus = (0, PART_TEXT, 0), (rows - 1, part, len(words))
        self.viewport().update()

    def clear_text_selection(self) -> None:
        if self._text_anchor is not None or self._text_focus is not None:
            self._text_anchor = self._text_focus = None
            self.viewport().update()

    def copy_text(self) -> bool:
        """Copy the words selected - or, with none, the current row as it reads."""
        current = self.currentIndex()
        text = self.selected_text or (self.row_text(current.row()) if current.isValid() else "")
        if text:
            QGuiApplication.clipboard().setText(text)
        return bool(text)

    def _position_at(self, pos: QPoint) -> tuple[int, int, int, bool] | None:
        """(row, part, letter, on the words) at ``pos`` in the viewport.

        Above or below the list, the row at that edge; in the empty space
        under the last row, the end of it.
        """
        rows = self._model.rowCount()
        if rows == 0:
            return None
        area = self.viewport().rect()
        inside = QPoint(min(max(pos.x(), 0), area.width() - 1),
                        min(max(pos.y(), 0), area.height() - 1))
        index = self.indexAt(inside)
        if not index.isValid():
            part, words = self._parts_of(rows - 1)[-1]
            return rows - 1, part, len(words), False
        option = QStyleOptionViewItem()
        self.initViewItemOption(option)
        option.rect = self.visualRect(index)
        self._delegate.initStyleOption(option, index)
        part, letter, on_words = self._delegate.position_at(
            option, index, QPointF(pos.x(), inside.y())
        )
        return index.row(), part, letter, on_words

    def _drag_words_to(self, pos: QPoint) -> None:
        found = self._position_at(pos)
        if found is not None and found[:3] != self._text_focus:
            self._text_focus = found[:3]
            self.viewport().update()
        outside = pos.y() < 0 or pos.y() >= self.viewport().height()
        if outside and not self._drag_scroll.isActive():
            self._drag_scroll.start()
        elif not outside:
            self._drag_scroll.stop()

    def _on_drag_scroll(self) -> None:
        """Held past the top or bottom, the list scrolls and the words follow -
        faster the further the mouse is past the edge."""
        if not self._dragging_text:
            self._drag_scroll.stop()
            return
        pos = self.viewport().mapFromGlobal(QCursor.pos())
        height = self.viewport().height()
        past = -pos.y() if pos.y() < 0 else pos.y() - height + 1
        if past <= 0:
            self._drag_scroll.stop()
            return
        bar = self.verticalScrollBar()
        step = max(4, min(60, past))
        bar.setValue(bar.value() + (step if pos.y() > 0 else -step))
        self._drag_words_to(pos)

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

    # -- asking about a line --------------------------------------------------

    @property
    def ask_enabled(self) -> bool:
        return self._ask_enabled

    def set_ask_enabled(self, enabled: bool) -> None:
        self._ask_enabled = enabled
        self.viewport().update()

    def shows_ask_button(self, row: int) -> bool:
        """The button shows on the line under the mouse and the selected one,
        but not on a line being typed in."""
        if not self._ask_enabled or row < 0:
            return False
        current = self.currentIndex().row() if self.currentIndex().isValid() else -1
        if self.is_editing and row == current:
            return False
        return row in (self._hover_row, current)

    def ask_button_hovered(self, row: int) -> bool:
        return self._hover_ask and row == self._hover_row

    def ask_button_at(self, pos: QPoint) -> int:
        """The row whose "Ask AI" button is at ``pos`` (viewport coordinates), or -1."""
        if not self._ask_enabled:
            return -1
        index = self.indexAt(pos)
        if not index.isValid() or not self._delegate.ask_rect(self.visualRect(index)).contains(pos):
            return -1
        if self.is_editing and index == self.currentIndex():
            return -1
        return index.row()

    def ask_about_row(self, row: int) -> None:
        segment = self._model.segment_at_row(row)
        if segment is None:
            return
        self.finish_editing()
        self.setCurrentIndex(self._model.index(row, 0))
        self.askRequested.emit(segment.id, "")

    def _set_hover(self, row: int, on_button: bool) -> None:
        if (row, on_button) == (self._hover_row, self._hover_ask):
            return
        for changed in {self._hover_row, row}:
            if changed >= 0:
                self.viewport().update(self.visualRect(self._model.index(changed, 0)))
        self._hover_row, self._hover_ask = row, on_button

    def _show_cursor(self, pos: QPoint, on_button: bool) -> None:
        """A hand on the "Ask AI" button, a text cursor on words to select."""
        found = None if on_button else self._position_at(pos)
        if on_button:
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        elif found is not None and found[3]:
            self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        else:
            self.viewport().unsetCursor()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        pos = event.position().toPoint()
        if self._press_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (
                not self._dragging_text
                and (pos - self._press_pos).manhattanLength() >= QApplication.startDragDistance()
            ):
                start = self._position_at(self._press_pos)
                if start is not None:
                    self._dragging_text = True
                    self._text_anchor = start[:3]
            if self._dragging_text:
                # The words follow the mouse; the list's own drag, which would
                # move the selected row along, is left out.
                self._drag_words_to(pos)
                return
        index = self.indexAt(pos)
        on_button = self.ask_button_at(pos) >= 0
        self._set_hover(index.row() if index.isValid() else -1, on_button)
        self._show_cursor(pos, on_button)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt naming
        self._set_hover(-1, False)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        # A press on the button is the button's alone: it must not select,
        # seek or start an edit underneath it.
        row = self.ask_button_at(event.position().toPoint())
        if row >= 0 and event.button() == Qt.MouseButton.LeftButton:
            self._ask_press_row = row
            event.accept()
            return
        self._ask_press_row = None
        if event.button() == Qt.MouseButton.LeftButton:
            # A new press lets go of the words selected; the right button
            # keeps them, for its menu's Copy.
            self.clear_text_selection()
            self._press_pos = event.position().toPoint()
            self._dragging_text = False
            self._chose_words = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if self._ask_press_row is not None:
            row, self._ask_press_row = self._ask_press_row, None
            if row >= 0 and self.ask_button_at(event.position().toPoint()) == row:
                self.ask_about_row(row)
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            # A drag over the words chose them: the click it ends in does not
            # seek (see _on_clicked).
            self._chose_words = self._dragging_text
            self._dragging_text = False
            self._press_pos = None
            self._drag_scroll.stop()
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_text()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all_text()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        if self.ask_button_at(event.position().toPoint()) >= 0:
            # The second click of a double click: swallow it and its release.
            self._ask_press_row = -1
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802 - Qt naming
        if (
            event.type() == QEvent.Type.ToolTip
            and isinstance(event, QHelpEvent)
            and self.ask_button_at(event.pos()) >= 0
        ):
            QToolTip.showText(
                event.globalPos(),
                "Ask AI about this line. Type a question, or just press Enter to "
                "have it explained.",
                self.viewport(),
            )
            return True
        return super().viewportEvent(event)

    # -- synchronisation ----------------------------------------------------

    def highlight_row(self, row: int, *, scroll: bool = True) -> None:
        self._model.set_current_row(row)
        # Never pull the list away from a row the user is typing in, nor from
        # under words being dragged over.
        if (scroll and row >= 0 and self._follow_playback and not self.is_editing
                and not self._dragging_text):
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
        if self.is_editing or self._chose_words:
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
        typo_choices = []
        for correction in self._model.corrections_at(index.row()):
            typo_choices.append((
                menu.addAction(f"Change “{correction.wrong}” to “{correction.right}”"),
                self.fixRequested, correction,
            ))
            typo_choices.append((
                menu.addAction(f"Keep “{correction.wrong}” as it is"),
                self.keepRequested, correction,
            ))
        if typo_choices:
            menu.addSeparator()
        copy = menu.addAction("Copy\tCtrl+C") if self.has_text_selection else None
        copy_line = menu.addAction("Copy this line")
        translation = str(index.data(ROLE_TRANSLATION) or "")
        copy_translation = menu.addAction("Copy its translation") if translation else None
        menu.addSeparator()
        edit = menu.addAction("Edit text\tF2")
        restore = menu.addAction("Restore original transcription")
        restore.setEnabled(segment.is_edited)
        menu.addSeparator()
        merge = menu.addAction("Merge with next segment")
        merge.setEnabled(index.row() + 1 < self._model.rowCount())
        insert = menu.addAction("Add segment after")
        menu.addSeparator()
        delete = menu.addAction("Delete segment")
        ask = explain = None
        if self._ask_enabled:
            menu.addSeparator()
            ask = menu.addAction("Ask AI about this line...")
            explain = menu.addAction("Explain this line with AI")

        chosen = menu.exec(self.viewport().mapToGlobal(point))
        if chosen is None:
            return
        for action, signal, correction in typo_choices:
            if chosen is action:
                signal.emit(correction)
                return
        if chosen is copy:
            self.copy_text()
        elif chosen is copy_line:
            QGuiApplication.clipboard().setText(segment.effective_text)
        elif chosen is copy_translation:
            QGuiApplication.clipboard().setText(translation)
        elif chosen is ask:
            self.askRequested.emit(segment.id, "")
        elif chosen is explain:
            self.explainRequested.emit(segment.id)
        elif chosen is edit:
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
