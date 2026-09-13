"""Start screen: create a project from a video, or reopen a recent one (§4)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..media.ffmpeg_tools import SUPPORTED_VIDEO_SUFFIXES, is_supported_video

__all__ = ["StartView"]


class _DropZone(QFrame):
    """The drag-and-drop target for lecture videos."""

    videoDropped = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(150)
        self._set_active(False)

        label = QLabel(
            "Drag a lecture video here\n\n"
            + ", ".join(s.lstrip(".").upper() for s in SUPPORTED_VIDEO_SUFFIXES[:6])
            + " and other FFmpeg-readable formats"
        )
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet("color: #777; border: none;")

        layout = QVBoxLayout(self)
        layout.addWidget(label)

    def _set_active(self, active: bool) -> None:
        colour = "#2E6DF6" if active else "#C4C4CC"
        background = "#EEF3FE" if active else "transparent"
        self.setStyleSheet(
            f"QFrame {{ border: 2px dashed {colour}; border-radius: 8px; "
            f"background: {background}; }}"
        )

    @staticmethod
    def first_video_url(urls: Iterable[QUrl]) -> Path | None:
        """First dropped URL that is a local lecture video, if any."""
        for url in urls:
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if is_supported_video(path):
                return path
        return None

    @classmethod
    def _video_from(cls, event: QDragEnterEvent | QDropEvent) -> Path | None:
        mime = event.mimeData()
        # PySide6 sometimes hands back an untyped QObject here, so ask by
        # duck-typing rather than assuming the QMimeData interface is present.
        urls = getattr(mime, "urls", None)
        if urls is None:
            return None
        return cls.first_video_url(urls())

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        if self._video_from(event) is not None:
            self._set_active(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        self._set_active(False)
        super().dragLeaveEvent(event)  # type: ignore[arg-type]

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        self._set_active(False)
        path = self._video_from(event)
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.videoDropped.emit(path)


class StartView(QWidget):
    """Landing screen shown when no project is open."""

    newProjectRequested = Signal()
    openProjectRequested = Signal()
    videoDropped = Signal(Path)
    recentSelected = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        title = QLabel("Lecture Saver")
        title.setStyleSheet("font-size: 24px; font-weight: 600;")
        subtitle = QLabel(
            "Transcribe lectures and turn them into slide-linked notes. "
            "Everything runs on this computer - nothing is uploaded."
        )
        subtitle.setStyleSheet("color: #666;")
        subtitle.setWordWrap(True)

        self._drop_zone = _DropZone()

        new_button = QPushButton("Choose a video...")
        new_button.setMinimumHeight(34)
        open_button = QPushButton("Open an existing project...")
        open_button.setMinimumHeight(34)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(new_button)
        buttons.addWidget(open_button)
        buttons.addStretch(1)

        self._recent_label = QLabel("Recent projects")
        self._recent_label.setStyleSheet("font-weight: 600; margin-top: 6px;")
        self._recent_list = QListWidget()
        self._recent_list.setFrameShape(QFrame.Shape.NoFrame)
        self._recent_list.setMaximumHeight(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 36, 40, 36)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        layout.addWidget(self._drop_zone)
        layout.addLayout(buttons)
        layout.addWidget(self._recent_label)
        layout.addWidget(self._recent_list, 1)

        new_button.clicked.connect(self.newProjectRequested)
        open_button.clicked.connect(self.openProjectRequested)
        self._drop_zone.videoDropped.connect(self.videoDropped)
        self._recent_list.itemActivated.connect(self._on_recent)
        self._recent_list.itemClicked.connect(self._on_recent)

    def set_recent(self, roots: list[Path]) -> None:
        self._recent_list.clear()
        for root in roots:
            item = QListWidgetItem(f"{root.name}    —    {root.parent}")
            item.setData(Qt.ItemDataRole.UserRole, str(root))
            self._recent_list.addItem(item)
        has_any = bool(roots)
        self._recent_label.setVisible(has_any)
        self._recent_list.setVisible(has_any)

    def _on_recent(self, item: QListWidgetItem) -> None:
        self.recentSelected.emit(Path(str(item.data(Qt.ItemDataRole.UserRole))))
