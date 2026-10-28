"""Screenshots of the lecture: look through them, change their marks, replay them.

The panel beside the video lists every screenshot in the order of the video,
each thumbnail with its marks on it. The one selected is shown large with the
same tools as drawing on the video, so its underlines, highlights and memos
can be changed afterwards; *Play* goes back to where its marking began and
plays on from there.

The panel owns the screenshots' storage, as the Ask AI panel owns the chat. A
new screenshot's picture goes under ``captures/`` on a background thread -
encoding two full-size JPEGs took ~150 ms, a visible stutter while the video
plays - and the list goes into ``captures.json`` once the pictures are on
disk, so the list never names a picture that is not there. Changed marks are
written shortly after the last change, together with a copy of the picture
with the marks drawn in (``captures/0001_marked.jpg``) - that copy is what the
notes PDF prints, and a file the user can open or print by itself.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QByteArray,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QImage,
    QImageWriter,
    QKeyEvent,
    QPainter,
    QPainterPath,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.atomic_io import replace_file
from ..core.captures import Capture, CaptureSet, Mark
from ..core.project import Project, ProjectError
from ..core.timecode import format_hms
from . import theme
from .ink import InkCanvas, InkToolBar, paint_marks, render_marked
from .theme import HintLabel, Role, key_hints, set_flag, set_role
from .video_player import FrameShot

log = logging.getLogger(__name__)

__all__ = ["CapturePanel", "write_image"]

THUMB_WIDTH = 136
THUMB_HEIGHT = 77
#: JPEG quality of saved screenshots: slide text stays crisp at about a
#: quarter of the size of a PNG.
JPEG_QUALITY = 92
#: Marks are written this long after the last change, so erasing along a
#: drag or a burst of edits is one write.
SAVE_DELAY_MS = 600

ROLE_CAPTURE = Qt.ItemDataRole.UserRole + 1


def write_image(image: QImage, path: Path) -> None:
    """Save ``image`` as a JPEG at ``path``, replacing it in one step.

    Written beside the target and renamed over it, like every other project
    file, so a crash never leaves half a picture.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.stem}.tmp{path.suffix}")
    writer = QImageWriter(str(temporary), QByteArray(b"jpg"))
    writer.setQuality(JPEG_QUALITY)
    if not writer.write(image):
        reason = writer.errorString()
        del writer  # closes the file, so it can be removed
        temporary.unlink(missing_ok=True)
        raise OSError(f"Could not write {path.name}: {reason}")
    del writer
    replace_file(temporary, path)


class ImageWriter(QObject):
    """Writes screenshots' pictures on one background thread, in order.

    Results are collected with :meth:`take_results` on the interface thread,
    after :attr:`finished` or after :meth:`wait`.
    """

    #: Some pictures have been written (or failed); collect the results.
    finished = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._condition = threading.Condition()
        self._jobs: deque[tuple[int, list[tuple[QImage, Path]]]] = deque()
        self._results: list[tuple[int, str]] = []
        self._pending = 0
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def submit(self, capture_id: int, files: list[tuple[QImage, Path]]) -> None:
        with self._condition:
            self._jobs.append((capture_id, files))
            self._pending += 1
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._work, name="screenshot-writer", daemon=True
                )
                self._thread.start()
            self._condition.notify_all()

    def wait(self, timeout: float = 30.0) -> bool:
        """Wait until everything submitted is written. False on timeout."""
        with self._condition:
            return self._condition.wait_for(lambda: self._pending == 0, timeout=timeout)

    def take_results(self) -> list[tuple[int, str]]:
        """(capture id, error - empty when written) for each finished screenshot."""
        with self._condition:
            results, self._results = self._results, []
        return results

    def _work(self) -> None:
        while True:
            with self._condition:
                while not self._jobs:
                    self._condition.wait()
                capture_id, files = self._jobs.popleft()
            error = ""
            try:
                for image, path in files:
                    write_image(image, path)
            except OSError as exc:
                error = str(exc)
            with self._condition:
                self._results.append((capture_id, error))
                self._pending -= 1
                self._condition.notify_all()
            self.finished.emit()


class CaptureCanvas(InkCanvas):
    """A saved screenshot, large, with its marks editable."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._message = "Choose a screenshot above."
        #: The picture scaled for the size it is shown at, made once per size.
        self._scaled = QImage()

    @property
    def image(self) -> QImage:
        return self._image

    def show_capture(self, image: QImage, marks: Sequence[Mark], message: str = "") -> None:
        self._image = image
        self._scaled = QImage()
        self._message = message
        self.set_marks(marks)

    def backdrop(self) -> QColor:
        return QColor(theme.SURFACE_SUNKEN)

    def picture_size(self) -> QSize:
        return self._image.size() if not self._image.isNull() else QSize()

    def paint_picture(self, painter: QPainter, rect: QRectF) -> None:
        ratio = self.devicePixelRatioF()
        wanted = QSize(round(rect.width() * ratio), round(rect.height() * ratio))
        if self._scaled.size() != wanted:
            self._scaled = self._image.scaled(
                wanted, Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._scaled.setDevicePixelRatio(ratio)
        painter.drawImage(rect.topLeft(), self._scaled)

    def paint_placeholder(self, painter: QPainter) -> None:
        painter.setPen(QColor(theme.MUTED))
        painter.drawText(
            self.rect().adjusted(16, 16, -16, -16),
            int(Qt.AlignmentFlag.AlignCenter) | int(Qt.TextFlag.TextWordWrap),
            self._message,
        )


class CaptureModel(QAbstractListModel):
    """The lecture's screenshots, with thumbnails that show their marks."""

    def __init__(self, unwritten: dict[int, QImage] | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._captures: list[Capture] = []
        self._root = Path()
        #: Pictures of screenshots still being written, by capture id.
        self._unwritten = unwritten if unwritten is not None else {}
        #: Unmarked thumbnails, by capture id: the slow part, made once.
        self._plain: dict[int, QImage] = {}
        #: Thumbnails with the marks on them, by capture id.
        self._marked: dict[int, QImage] = {}

    def set_captures(self, captures: CaptureSet | None, root: Path) -> None:
        self.beginResetModel()
        self._captures = list(captures.captures) if captures is not None else []
        if Path(root) != self._root:
            # Thumbnails of the same lecture stay: a lecture with many
            # screenshots must not decode them all again for each new one.
            self._plain.clear()
            self._marked.clear()
        self._root = Path(root)
        self.endResetModel()

    def capture_at(self, row: int) -> Capture | None:
        return self._captures[row] if 0 <= row < len(self._captures) else None

    def row_of(self, capture_id: int) -> int:
        return next(
            (i for i, c in enumerate(self._captures) if c.capture_id == capture_id), -1
        )

    def marks_changed(self, capture_id: int) -> None:
        """Redraw one thumbnail after its marks changed."""
        self._marked.pop(capture_id, None)
        row = self.row_of(capture_id)
        if row >= 0:
            index = self.index(row, 0)
            self.dataChanged.emit(index, index)

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._captures)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        capture = self.capture_at(index.row()) if index.isValid() else None
        if capture is None:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return format_hms(capture.time)
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(capture)
        if role == ROLE_CAPTURE:
            return capture
        if role == Qt.ItemDataRole.ToolTipRole:
            count = len(capture.marks)
            marks = f"{count} mark{'s' if count != 1 else ''}" if count else "No marks"
            return (
                f"Screenshot at {format_hms(capture.time)} · {marks}\n"
                "Double-click to play from here"
            )
        return None

    def _thumbnail(self, capture: Capture) -> QImage:
        cached = self._marked.get(capture.capture_id)
        if cached is not None:
            return cached
        plain = self._plain.get(capture.capture_id)
        if plain is None:
            loaded = self._unwritten.get(capture.capture_id)
            if loaded is None:
                loaded = QImage(str(capture.image_path(self._root)))
            if loaded.isNull():
                plain = QImage(THUMB_WIDTH, THUMB_HEIGHT, QImage.Format.Format_RGB32)
                plain.fill(QColor(theme.SURFACE_SUNKEN))
            else:
                plain = loaded.scaled(
                    THUMB_WIDTH * 2, THUMB_HEIGHT * 2, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                ).convertToFormat(QImage.Format.Format_RGB32)
            self._plain[capture.capture_id] = plain
        thumbnail = plain.copy()
        if capture.marks:
            painter = QPainter(thumbnail)
            paint_marks(painter, capture.marks, QRectF(0, 0, thumbnail.width(),
                                                       thumbnail.height()))
            painter.end()
        thumbnail.setDevicePixelRatio(2.0)
        self._marked[capture.capture_id] = thumbnail
        return thumbnail


class CaptureDelegate(QStyledItemDelegate):
    """One tile of the strip: the thumbnail, and its time under it."""

    PAD = 6
    GAP = 4

    def __init__(self, view: QListView) -> None:
        super().__init__(view)
        self._view = view
        self._time_font = QFont("Consolas")
        self._time_font.setPointSizeF(max(7.5, view.font().pointSizeF() - 1.0))

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> QSize:
        caption = QFontMetrics(self._time_font).height()
        return QSize(THUMB_WIDTH + self.PAD * 2, THUMB_HEIGHT + self.GAP + caption + self.PAD * 2)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect: QRect = option.rect  # type: ignore[attr-defined]
        state = option.state  # type: ignore[attr-defined]
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        tile = rect.adjusted(2, 2, -2, -2)
        painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            painter.setBrush(QColor(theme.ACCENT_SOFT if selected else theme.SURFACE_HOVER))
            painter.drawRoundedRect(tile, 8, 8)

        image = index.data(Qt.ItemDataRole.DecorationRole)
        width, height = THUMB_WIDTH, THUMB_HEIGHT
        if isinstance(image, QImage) and not image.isNull():
            ratio = image.devicePixelRatio() or 1.0
            width = round(image.width() / ratio)
            height = round(image.height() / ratio)
        picture = QRect(
            tile.left() + (tile.width() - width) // 2,
            tile.top() + self.PAD + (THUMB_HEIGHT - height) // 2,
            width, height,
        )
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(picture), 5, 5)
        painter.save()
        painter.setClipPath(clip)
        if isinstance(image, QImage):
            painter.drawImage(picture, image)
        painter.restore()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(theme.ACCENT if selected else theme.BORDER_STRONG))
        painter.drawRoundedRect(QRectF(picture), 5, 5)

        caption = QRect(
            tile.left(), tile.top() + self.PAD + THUMB_HEIGHT + self.GAP,
            tile.width(), QFontMetrics(self._time_font).height(),
        )
        painter.setFont(self._time_font)
        painter.setPen(QColor(theme.ACCENT if selected else theme.MUTED))
        painter.drawText(
            caption, int(Qt.AlignmentFlag.AlignCenter),
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )
        painter.restore()


class _Strip(QListView):
    """The row of thumbnails. Delete asks to delete the one selected."""

    deletePressed = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.deletePressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CapturePanel(QFrame):
    """The lecture's screenshots. Bind a project, then add to it."""

    #: Play the video from a screenshot's stretch: seconds.
    playRequested = Signal(float)
    #: The panel's own close button.
    hideRequested = Signal()
    #: A short message for the status bar.
    statusMessage = Signal(str)
    #: How many screenshots the lecture has now.
    countChanged = Signal(int)
    #: Export the screenshots on their own as a PDF.
    exportRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        set_flag(self, "card")
        self._project: Project | None = None
        #: Screenshots whose marks changed and are not written yet.
        self._dirty: set[int] = set()
        #: Pictures of new screenshots until they are on disk, by capture id.
        self._unwritten: dict[int, QImage] = {}
        # Not a child of the panel: its thread holds it, and must be able to
        # report back even while the panel is being destroyed.
        self._writer = ImageWriter()
        self._writer.finished.connect(self._collect_written)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DELAY_MS)
        self._save_timer.timeout.connect(self._write_soon)

        title = QLabel("Screenshots")
        set_role(title, Role.SECTION)
        self._count_label = QLabel("")
        set_role(self._count_label, Role.MUTED)
        self._export_button = QToolButton()
        self._export_button.setText("Export PDF")
        self._export_button.setToolTip(
            "Save every screenshot, with its marks, as one PDF: each page is one "
            "screenshot and nothing else, edge to edge, in the order of the lecture.\n"
            "Each one's time and slide are in the PDF's bookmarks."
        )
        self._hide_button = QToolButton()
        self._hide_button.setText("✕")
        self._hide_button.setToolTip("Hide the panel. The screenshots are kept.")
        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 8, 4)
        header.setSpacing(6)
        header.addWidget(title)
        header.addWidget(self._count_label)
        header.addStretch(1)
        header.addWidget(self._export_button)
        header.addWidget(self._hide_button)
        #: Off while another export runs; see :meth:`set_export_enabled`.
        self._export_allowed = True

        self._model = CaptureModel(self._unwritten, self)
        self._strip = _Strip()
        self._strip.setModel(self._model)
        delegate = CaptureDelegate(self._strip)
        self._strip.setItemDelegate(delegate)
        self._strip.setFlow(QListView.Flow.LeftToRight)
        self._strip.setWrapping(False)
        self._strip.setUniformItemSizes(True)
        self._strip.setMouseTracking(True)
        self._strip.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._strip.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._strip.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._strip.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._strip.setFrameShape(QFrame.Shape.NoFrame)
        tile = delegate.sizeHint(QStyleOptionViewItem(), QModelIndex())
        self._strip.setFixedHeight(tile.height() + self._strip.horizontalScrollBar()
                                   .sizeHint().height() + 6)

        self._empty_label = QLabel(
            "No screenshots yet.\n\nPress S while watching - or Screenshot under "
            "the video - to keep the picture on screen. Turn on Draw (D) first "
            "to underline, highlight and write memos on it while the video plays."
        )
        self._empty_label.setWordWrap(True)
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setMargin(16)
        set_role(self._empty_label, Role.SUBTLE)

        self._title = QLabel("")
        set_role(self._title, Role.SECTION)
        self._detail = QLabel("")
        set_role(self._detail, Role.MUTED)
        self._detail.setWordWrap(True)

        self._canvas = CaptureCanvas()
        self._canvas.setMinimumHeight(140)
        self._tools = InkToolBar(self._canvas, two_rows=True)
        self._play_button = QPushButton("Play")
        set_flag(self._play_button, "accent")
        self._play_button.setToolTip(
            "Play the video from where the marking of this screenshot began"
        )
        self._delete_button = QPushButton("Delete...")
        set_flag(self._delete_button, "quiet")
        self._delete_button.setToolTip("Delete this screenshot and its picture (Delete)")
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(6)
        actions.addWidget(self._play_button)
        actions.addStretch(1)
        actions.addWidget(self._delete_button)

        self._keys_label = HintLabel(key_hints((
            ("Ctrl+Z", "undo"), ("Ctrl+Y", "redo"), ("Delete", "delete screenshot"),
            ("Double-click a thumbnail", "play"),
            ("Double-click a memo", "change it"),
        )))

        self._editor = QWidget()
        editor = QVBoxLayout(self._editor)
        editor.setContentsMargins(12, 4, 12, 10)
        editor.setSpacing(6)
        editor.addWidget(self._title)
        editor.addWidget(self._detail)
        editor.addWidget(self._canvas, 1)
        editor.addWidget(self._tools)
        editor.addLayout(actions)
        editor.addWidget(self._keys_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self._strip)
        layout.addWidget(self._empty_label, 1)
        layout.addWidget(self._editor, 1)

        self._hide_button.clicked.connect(self.hideRequested)
        self._export_button.clicked.connect(self.exportRequested)
        self._strip.selectionModel().currentChanged.connect(self._on_current_changed)
        self._strip.doubleClicked.connect(lambda _index: self.play_selected())
        self._strip.deletePressed.connect(self.delete_selected)
        self._play_button.clicked.connect(self.play_selected)
        self._delete_button.clicked.connect(self.delete_selected)
        self._canvas.marksChanged.connect(self._on_marks_edited)
        self._refresh()

    # -- binding --------------------------------------------------------------

    def bind(self, project: Project | None) -> None:
        """Show ``project``'s screenshots, after writing the last one's edits."""
        self.flush()
        self._project = project
        self._model.set_captures(project.captures if project else None,
                                 project.root if project else Path())
        if project is not None:
            # A marked copy lost to a crash before it was written is redrawn.
            self._dirty = {
                c.capture_id for c in project.captures
                if c.marks and not (project.root / c.marked_image).is_file()
            }
            if self._dirty:
                self.flush()
        self._refresh()
        latest = self._model.capture_at(self._model.rowCount() - 1)
        if latest is not None:
            self.select(latest.capture_id)
        else:
            self._on_current_changed(QModelIndex(), QModelIndex())

    @property
    def count(self) -> int:
        return self._model.rowCount()

    @property
    def selected(self) -> Capture | None:
        index = self._strip.currentIndex()
        return self._model.capture_at(index.row()) if index.isValid() else None

    @property
    def canvas(self) -> CaptureCanvas:
        return self._canvas

    @property
    def tools(self) -> InkToolBar:
        return self._tools

    def select(self, capture_id: int) -> None:
        row = self._model.row_of(capture_id)
        if row >= 0:
            index = self._model.index(row, 0)
            self._strip.setCurrentIndex(index)
            self._strip.scrollTo(index)

    # -- adding ---------------------------------------------------------------

    def add_screenshot(self, shot: FrameShot) -> Capture | None:
        """Keep a screenshot just taken: its picture, its marks, the list.

        It is listed at once; its pictures are written in the background and
        the list saved when they are.
        """
        project = self._project
        if project is None:
            return None
        capture_id = project.captures.new_id()
        image = CaptureSet.image_name(capture_id)
        capture = Capture(
            capture_id=capture_id, time=shot.time, start=shot.start, image=image,
            width=shot.image.width(), height=shot.image.height(), marks=list(shot.marks),
        )
        files = [(shot.image, project.root / image)]
        if capture.marks:
            files.append((render_marked(shot.image, capture.marks),
                          project.root / capture.marked_image))
        self._unwritten[capture_id] = shot.image
        project.captures.add(capture)
        self._writer.submit(capture_id, files)
        self._model.set_captures(project.captures, project.root)
        self._refresh()
        self.select(capture_id)
        return capture

    def _collect_written(self) -> None:
        """Save the list for screenshots now on disk; drop any that failed."""
        project = self._project
        changed = False
        failures = []
        for capture_id, error in self._writer.take_results():
            self._unwritten.pop(capture_id, None)
            if project is None:
                continue
            capture = project.captures.get(capture_id)
            if capture is None:
                # Deleted while its pictures were being written.
                self._remove_pictures(project, CaptureSet.image_name(capture_id))
                continue
            if error:
                log.warning("Could not save screenshot %s: %s", capture_id, error)
                project.captures.remove(capture_id)
                self._dirty.discard(capture_id)
                self._remove_pictures(project, capture.image)
                failures.append(error)
            changed = True
        if not changed or project is None:
            return
        if self._dirty:
            self._write_marks()  # saves the list too
        else:
            self._save_list()
        if failures:
            self._model.set_captures(project.captures, project.root)
            self._refresh()
            if self.selected is None:
                self._on_current_changed(QModelIndex(), QModelIndex())
            QMessageBox.warning(
                self, "Could not save the screenshot",
                f"The picture could not be written to the project folder:\n{failures[0]}",
            )

    @staticmethod
    def _remove_pictures(project: Project, image: str) -> None:
        marked = Capture(capture_id=0, time=0.0, start=0.0, image=image).marked_image
        for name in (image, marked):
            try:
                (project.root / name).unlink(missing_ok=True)
            except OSError:
                log.warning("Could not delete %s", name, exc_info=True)

    # -- the selected screenshot ----------------------------------------------

    def _on_current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        # Edits to the previous screenshot are written before another is shown
        # - unless a new screenshot is still being written, which must not be
        # waited for here: this runs when a screenshot is taken during playback.
        self._write_soon()
        capture = self._model.capture_at(current.row()) if current.isValid() else None
        if capture is None or self._project is None:
            self._canvas.show_capture(QImage(), (), "Choose a screenshot above.")
        else:
            image = self._unwritten.get(capture.capture_id)
            if image is None:
                image = QImage(str(capture.image_path(self._project.root)))
            message = "" if not image.isNull() else (
                f"The picture of this screenshot is missing:\n{capture.image}"
            )
            self._canvas.show_capture(image, capture.marks, message)
            self._canvas.set_editable(not image.isNull())
        self._update_details()

    def _update_details(self) -> None:
        capture = self.selected
        self._play_button.setEnabled(capture is not None)
        self._delete_button.setEnabled(capture is not None)
        self._tools.setEnabled(capture is not None and not self._canvas.image.isNull())
        if capture is None:
            self._title.setText("")
            self._detail.setText("")
            return
        position = self._model.row_of(capture.capture_id) + 1
        self._title.setText(f"Screenshot {position}  ·  {format_hms(capture.time)}")
        parts = []
        if capture.start < capture.time:
            parts.append(f"Marked from {format_hms(capture.start)}")
        slide = self._slide_number(capture.time)
        if slide is not None:
            parts.append(f"Slide {slide}")
        count = len(capture.marks)
        parts.append(f"{count} mark{'s' if count != 1 else ''}" if count else "No marks")
        self._detail.setText("  ·  ".join(parts))
        self._play_button.setText(f"Play from {format_hms(capture.play_from)}")

    def _slide_number(self, seconds: float) -> int | None:
        if self._project is None:
            return None
        appearance = self._project.slides.appearance_at(seconds)
        return appearance.appearance_id if appearance is not None else None

    def play_selected(self) -> None:
        capture = self.selected
        if capture is not None:
            self.playRequested.emit(capture.play_from)

    def delete_selected(self) -> None:
        capture = self.selected
        project = self._project
        if capture is None or project is None:
            return
        answer = QMessageBox.question(
            self, "Delete this screenshot?",
            f"The screenshot at {format_hms(capture.time)} and its marks will be "
            "deleted. This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.flush()
        row = self._model.row_of(capture.capture_id)
        try:
            project.delete_capture(capture.capture_id)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not delete the screenshot", str(exc))
            return
        self._model.set_captures(project.captures, project.root)
        self._refresh()
        neighbour = self._model.capture_at(min(row, self._model.rowCount() - 1))
        if neighbour is not None:
            self.select(neighbour.capture_id)
        else:
            self._on_current_changed(QModelIndex(), QModelIndex())
        self.statusMessage.emit("Screenshot deleted")

    # -- saving edits ---------------------------------------------------------

    def _on_marks_edited(self) -> None:
        capture = self.selected
        if capture is None:
            return
        capture.marks = list(self._canvas.marks)
        self._dirty.add(capture.capture_id)
        self._model.marks_changed(capture.capture_id)
        self._update_details()
        self._save_timer.start()

    def flush(self) -> bool:
        """Write everything now: new screenshots' pictures, changed marks.

        False if something could not be written.
        """
        self._save_timer.stop()
        if self._writer.busy:
            self._writer.wait()
        self._collect_written()
        return self._write_marks()

    def _write_soon(self) -> None:
        """Write changed marks, or try again shortly while pictures are written.

        The list is only ever saved with every picture it names on disk.
        """
        self._save_timer.stop()
        if self._writer.busy:
            if self._dirty:
                self._save_timer.start()
            return
        self._collect_written()
        self._write_marks()

    def _write_marks(self) -> bool:
        project = self._project
        if project is None or not self._dirty:
            self._dirty.clear()
            return True
        shown = self.selected
        ok = True
        for capture_id in sorted(self._dirty):
            capture = project.captures.get(capture_id)
            if capture is None:
                continue
            marked = project.root / capture.marked_image
            try:
                if capture.marks:
                    in_memory = self._unwritten.get(capture_id)
                    if shown is not None and shown.capture_id == capture_id:
                        image = self._canvas.image
                    elif in_memory is not None:
                        image = in_memory
                    else:
                        image = QImage(str(capture.image_path(project.root)))
                    if not image.isNull():
                        write_image(render_marked(image, capture.marks), marked)
                else:
                    marked.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not write the marked copy of %s", capture.image,
                            exc_info=True)
                ok = False
        self._dirty.clear()
        return self._save_list() and ok

    def _save_list(self) -> bool:
        if self._project is None:
            return True
        try:
            self._project.save_captures()
        except ProjectError as exc:
            log.warning("Could not save the screenshots", exc_info=True)
            self.statusMessage.emit(f"Could not save the screenshots: {exc}")
            return False
        return True

    @property
    def export_button(self) -> QToolButton:
        return self._export_button

    def set_export_enabled(self, enabled: bool) -> None:
        self._export_allowed = enabled
        self._export_button.setEnabled(enabled and self._model.rowCount() > 0)

    def _refresh(self) -> None:
        count = self._model.rowCount()
        self._count_label.setText(str(count) if count else "")
        self._export_button.setEnabled(self._export_allowed and count > 0)
        self._strip.setVisible(count > 0)
        self._editor.setVisible(count > 0)
        self._empty_label.setVisible(count == 0)
        self.countChanged.emit(count)
