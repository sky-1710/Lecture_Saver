"""Start screen: create a project from a video, or reopen a recent one (§4)."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..media.ffmpeg_tools import SUPPORTED_VIDEO_SUFFIXES, is_supported_video
from . import theme
from .theme import Role, set_flag, set_role

__all__ = ["StartView"]


class _DropZone(QFrame):
    """The drag-and-drop target for lecture videos."""

    videoDropped = Signal(Path)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setFixedHeight(188)

        self._headline = QLabel("Drag a lecture video here")
        self._headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        set_role(self._headline, Role.SECTION)

        formats = QLabel(
            ", ".join(s.lstrip(".").upper() for s in SUPPORTED_VIDEO_SUFFIXES[:6])
            + " and other FFmpeg-readable formats"
        )
        formats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        formats.setWordWrap(True)
        set_role(formats, Role.SUBTLE)

        mark = QLabel()
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setPixmap(theme.app_icon().pixmap(QSize(44, 44)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(6)
        layout.addStretch(1)
        layout.addWidget(mark)
        layout.addSpacing(6)
        layout.addWidget(self._headline)
        layout.addWidget(formats)
        layout.addStretch(1)

        self._set_active(False)

    def _set_active(self, active: bool) -> None:
        colour = theme.ACCENT if active else theme.BORDER_STRONG
        background = theme.ACCENT_SOFT if active else theme.SURFACE
        self.setStyleSheet(
            f"QFrame {{ border: 2px dashed {colour}; border-radius: {theme.RADIUS}px; "
            f"background: {background}; }}"
            "QLabel { border: none; background: transparent; }"
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
        set_role(title, Role.DISPLAY)
        subtitle = QLabel(
            "Transcribe lectures and turn them into slide-linked notes. "
            "Everything runs on this computer - nothing is uploaded."
        )
        set_role(subtitle, Role.MUTED)
        subtitle.setWordWrap(True)

        privacy = QLabel("Fully offline · no account · no cloud service")
        set_role(privacy, Role.SUBTLE)

        self._drop_zone = _DropZone()

        new_button = QPushButton("Choose a video...")
        new_button.setMinimumHeight(36)
        set_flag(new_button, "accent")
        open_button = QPushButton("Open an existing project...")
        open_button.setMinimumHeight(36)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(new_button)
        buttons.addWidget(open_button)
        buttons.addStretch(1)

        self._recent_label = QLabel("Recent projects")
        set_role(self._recent_label, Role.SECTION)
        self._recent_list = QListWidget()
        self._recent_list.setFrameShape(QFrame.Shape.NoFrame)
        self._recent_list.setMaximumHeight(184)
        self._recent_list.setSpacing(1)
        self._recent_list.setWordWrap(True)
        self._recent_list.setCursor(Qt.CursorShape.PointingHandCursor)

        recent_card = QFrame()
        set_flag(recent_card, "card")
        recent_layout = QVBoxLayout(recent_card)
        recent_layout.setContentsMargins(8, 8, 8, 8)
        recent_layout.addWidget(self._recent_list)
        self._recent_card = recent_card

        # One centred column, so the screen does not stretch into a wall of
        # white on a wide monitor.
        column = QWidget()
        column.setMaximumWidth(680)
        column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        column_layout = QVBoxLayout(column)
        column_layout.setContentsMargins(0, 0, 0, 0)
        column_layout.setSpacing(10)
        column_layout.addStretch(2)
        column_layout.addWidget(title)
        column_layout.addWidget(subtitle)
        column_layout.addWidget(privacy)
        column_layout.addSpacing(10)
        column_layout.addWidget(self._drop_zone)
        column_layout.addLayout(buttons)
        column_layout.addSpacing(6)
        column_layout.addWidget(self._recent_label)
        column_layout.addWidget(recent_card)
        column_layout.addStretch(3)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)
        layout.addStretch(1)
        layout.addWidget(column, 4)
        layout.addStretch(1)

        new_button.clicked.connect(self.newProjectRequested)
        open_button.clicked.connect(self.openProjectRequested)
        self._drop_zone.videoDropped.connect(self.videoDropped)
        self._recent_list.itemActivated.connect(self._on_recent)
        self._recent_list.itemClicked.connect(self._on_recent)

    def set_recent(self, roots: list[Path]) -> None:
        self._recent_list.clear()
        for root in roots:
            item = QListWidgetItem(f"{root.name}\n{root.parent}")
            item.setData(Qt.ItemDataRole.UserRole, str(root))
            item.setToolTip(str(root))
            item.setSizeHint(QSize(0, 46))
            self._recent_list.addItem(item)
        # The card is as tall as its contents, not a half-empty box.
        self._recent_list.setFixedHeight(min(len(roots), 4) * 48 + 4)
        has_any = bool(roots)
        self._recent_label.setVisible(has_any)
        self._recent_card.setVisible(has_any)

    def _on_recent(self, item: QListWidgetItem) -> None:
        self.recentSelected.emit(Path(str(item.data(Qt.ItemDataRole.UserRole))))
