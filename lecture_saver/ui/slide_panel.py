"""Slide navigator: thumbnails, numbers, timestamps, click-to-seek (§11, §12).

Thumbnails are scaled from the saved slide JPEGs on demand and cached, rather
than written as separate files. A lecture with a few hundred slides therefore
costs a few hundred small pixmaps at most, and only for rows the user actually
scrolls past.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QMenu,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.slides import SlideAppearance, SlideSet
from ..core.timecode import format_hms

log = logging.getLogger(__name__)

__all__ = ["SlidePanel"]

THUMB_WIDTH = 152
THUMB_HEIGHT = 86

ROLE_START = Qt.ItemDataRole.UserRole + 1
ROLE_APPEARANCE = Qt.ItemDataRole.UserRole + 2


class SlideModel(QAbstractListModel):
    """Adapts a :class:`SlideSet` to Qt, with a lazy thumbnail cache."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._appearances: Sequence[SlideAppearance] = ()
        self._slides = SlideSet()
        self._root = Path()
        self._pixmaps: dict[int, QPixmap] = {}

    def set_slides(self, slide_set: SlideSet, project_root: Path) -> None:
        self.beginResetModel()
        self._slides = slide_set
        self._appearances = tuple(slide_set.appearances)
        self._root = Path(project_root)
        self._pixmaps.clear()
        self.endResetModel()

    def appearance_at_row(self, row: int) -> SlideAppearance | None:
        if 0 <= row < len(self._appearances):
            return self._appearances[row]
        return None

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._appearances)

    def data(
        self,
        index: QModelIndex | QPersistentModelIndex,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if not index.isValid():
            return None
        appearance = self._appearances[index.row()]

        label = appearance.annotation_label
        if role == Qt.ItemDataRole.DisplayRole:
            first = f"Slide {appearance.appearance_id}" + (f" · {label}" if label else "")
            return f"{first}\n{format_hms(appearance.start)}"
        if role == Qt.ItemDataRole.DecorationRole:
            return self._thumbnail(appearance)
        if role == ROLE_START:
            return appearance.start
        if role == ROLE_APPEARANCE:
            return appearance
        if role == Qt.ItemDataRole.ToolTipRole:
            return (
                f"Slide {appearance.appearance_id}"
                f"{f' ({label})' if label else ''}\n"
                f"{format_hms(appearance.start)} - {format_hms(appearance.end)}\n"
                "Click to jump here"
            )
        return None

    def flags(self, index: QModelIndex | QPersistentModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def _thumbnail(self, appearance: SlideAppearance) -> QPixmap:
        cached = self._pixmaps.get(appearance.appearance_id)
        if cached is not None:
            return cached

        pixmap = QPixmap()
        slide = self._slides.slide_for(appearance)
        if slide is not None and slide.image:
            path = slide.image_path(self._root)
            if path.is_file():
                loaded = QPixmap(str(path))
                if not loaded.isNull():
                    pixmap = loaded.scaled(
                        THUMB_WIDTH, THUMB_HEIGHT,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )

        if pixmap.isNull():
            # A slide whose frame could not be captured still needs a tile, or
            # the row would silently collapse.
            pixmap = QPixmap(THUMB_WIDTH, THUMB_HEIGHT)
            pixmap.fill(QColor(0xE4, 0xE4, 0xE8))

        self._pixmaps[appearance.appearance_id] = pixmap
        return pixmap


class SlidePanel(QFrame):
    """The left-hand slide navigator plus its detection controls."""

    seekRequested = Signal(float)
    #: The user picked a slide; carries its row. Emitted with ``seekRequested``
    #: so the transcript can switch to that slide even before the player has
    #: finished seeking.
    slideActivated = Signal(int)
    detectRequested = Signal()
    cancelRequested = Signal()
    #: Manual corrections (§ real-world robustness). Automatic detection is a
    #: first draft; these let the user fix it before alignment and export.
    deleteRequested = Signal(int)        # appearance id
    mergeRequested = Signal(int)         # appearance id
    recaptureRequested = Signal(int)     # appearance id
    splitHereRequested = Signal()        # split at the current playback time
    moveBoundaryRequested = Signal(int)  # move this slide's start to now

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._running = False

        title = QLabel("Slides")
        title.setStyleSheet("font-weight: 600;")
        self._count_label = QLabel("")
        self._count_label.setStyleSheet("color: #666;")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._count_label)

        self._detect_button = QPushButton("Detect slides")
        self._detect_button.setMinimumHeight(30)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setMinimumHeight(30)
        self._cancel_button.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self._detect_button, 1)
        buttons.addWidget(self._cancel_button)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setVisible(False)

        self._status_label = QLabel("No slides detected yet.")
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet("color: #666;")

        self._model = SlideModel(self)
        self._list = QListView()
        self._list.setModel(self._model)
        # Icon mode puts the caption under the thumbnail instead of squeezing
        # it into the remaining width, which is what a narrow navigator needs.
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setFlow(QListView.Flow.TopToBottom)
        self._list.setWrapping(False)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setIconSize(QSize(THUMB_WIDTH, THUMB_HEIGHT))
        self._list.setGridSize(QSize(THUMB_WIDTH + 16, THUMB_HEIGHT + 46))
        self._list.setUniformItemSizes(True)
        self._list.setWordWrap(True)
        self._list.setSpacing(2)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setFrameShape(QListView.Shape.NoFrame)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.setVisible(False)

        self._split_button = QPushButton("Add slide here")
        self._split_button.setToolTip(
            "Create a slide boundary at the current position in the video.\n"
            "Use this when a real slide change was not detected."
        )
        self._split_button.setEnabled(False)
        self._edit_hint = QLabel(
            "Right-click a slide to delete, merge, move its start, or re-capture it."
        )
        self._edit_hint.setWordWrap(True)
        self._edit_hint.setStyleSheet("color: #888;")
        self._edit_hint.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(buttons)
        layout.addWidget(self._progress)
        layout.addWidget(self._status_label)
        layout.addWidget(self._list, 1)
        layout.addWidget(self._split_button)
        layout.addWidget(self._edit_hint)

        self._detect_button.clicked.connect(self.detectRequested)
        self._cancel_button.clicked.connect(self._on_cancel)
        self._list.clicked.connect(self._on_clicked)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._split_button.clicked.connect(self.splitHereRequested)

    # -- content ------------------------------------------------------------

    def set_slides(self, slide_set: SlideSet, project_root: Path) -> None:
        self._model.set_slides(slide_set, project_root)
        count = len(slide_set)
        self._list.setVisible(count > 0)
        self._edit_hint.setVisible(count > 0)
        self._split_button.setEnabled(count > 0)
        self._count_label.setText(f"{count}" if count else "")
        self._detect_button.setText("Detect slides again" if count else "Detect slides")
        if count:
            edited = " (edited by hand)" if slide_set.manually_edited else ""
            self._status_label.setText(f"{count} slides{edited}.")
        else:
            self._status_label.setText("No slides detected yet.")

    @property
    def slide_count(self) -> int:
        return self._model.rowCount()

    @property
    def current_row(self) -> int:
        index = self._list.currentIndex()
        return index.row() if index.isValid() else -1

    def select_row(self, row: int) -> None:
        """Select a slide without seeking - used when playback moves on (§12)."""
        if not 0 <= row < self._model.rowCount():
            self._list.clearSelection()
            return
        index = self._model.index(row, 0)
        if self._list.currentIndex() != index:
            self._list.setCurrentIndex(index)
        self._list.scrollTo(index, QAbstractItemView.ScrollHint.EnsureVisible)

    def set_enabled_for_video(self, enabled: bool, reason: str = "") -> None:
        """Disable detection when it genuinely cannot run, saying why (§32)."""
        self._detect_button.setEnabled(enabled)
        if not enabled and reason:
            self._status_label.setText(reason)

    # -- run state ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def begin_run(self) -> None:
        self._running = True
        self._detect_button.setVisible(False)
        self._cancel_button.setVisible(True)
        self._cancel_button.setEnabled(True)
        self._cancel_button.setText("Cancel")
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)

    def end_run(self, message: str) -> None:
        self._running = False
        self._detect_button.setVisible(True)
        self._cancel_button.setVisible(False)
        self._progress.setVisible(False)
        self._status_label.setText(message)

    def update_progress(self, stage: str, fraction: float, message: str) -> None:
        if fraction < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(fraction * 100))
        self._status_label.setText(f"{stage}\n{message}" if message else stage)

    # -- events -------------------------------------------------------------

    def _on_cancel(self) -> None:
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelling...")
        self.cancelRequested.emit()

    def _on_clicked(self, index: QModelIndex) -> None:
        appearance = self._model.appearance_at_row(index.row())
        if appearance is not None:
            self.slideActivated.emit(index.row())
            self.seekRequested.emit(appearance.start)

    def _on_context_menu(self, point: QPoint) -> None:
        index = self._list.indexAt(point)
        appearance = self._model.appearance_at_row(index.row()) if index.isValid() else None
        if appearance is None:
            return

        menu = QMenu(self)
        recapture = QAction("Re-capture image from current position", menu)
        recapture.setToolTip(
            "Replace this slide's picture with the frame the video is showing now."
        )
        move = QAction("Move this slide's start to the current position", menu)
        move.setEnabled(appearance.appearance_id > 1)
        move.setToolTip(
            "Use this when the boundary was found a little early or late."
        )
        merge = QAction("Merge into previous slide", menu)
        merge.setEnabled(appearance.appearance_id > 1)
        delete = QAction("Delete this slide", menu)

        menu.addAction(recapture)
        menu.addAction(move)
        menu.addSeparator()
        menu.addAction(merge)
        menu.addAction(delete)

        chosen = menu.exec(self._list.viewport().mapToGlobal(point))
        if chosen is recapture:
            self.recaptureRequested.emit(appearance.appearance_id)
        elif chosen is move:
            self.moveBoundaryRequested.emit(appearance.appearance_id)
        elif chosen is merge:
            self.mergeRequested.emit(appearance.appearance_id)
        elif chosen is delete:
            self.deleteRequested.emit(appearance.appearance_id)
