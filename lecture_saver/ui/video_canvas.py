"""The video picture painted by the application, so marks can be drawn on it.

On Windows a ``QVideoWidget`` is a native window: nothing can be painted over
it. While the user draws, the frames are therefore painted here instead, with
the marks on top; the rest of the time the player shows them in its
``QVideoWidget``, which scales on the GPU for free.

Turning a decoded 1080p frame into a picture the size of the view costs about
15 ms - too much for the interface thread 60 times a second, which is what 2x
playback of a 30 fps lecture asks for. :class:`FrameRenderer` does it on two
background threads, always on the newest frame: when frames arrive faster
than they can be converted, the ones in between are skipped rather than
queued, so the picture never falls behind the sound and the interface stays
responsive. Qt's conversion and painting release the GIL, which is what makes
plain threads worth it (measured on 1080p at 2x: 56 of 59 frames a second
painted, the interface thread still answering within its timer resolution).

The threads are started on first use and never end. Qt keeps a GPU context
for each thread that converts frames, and a converting thread that ended was
measured to leave the next ``QMediaPlayer.setSource`` - clearing the player
when a lecture is closed - hanging for good. Idle, they cost nothing.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from PySide6.QtCore import QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QHideEvent, QImage, QPainter, QResizeEvent, QShowEvent
from PySide6.QtMultimedia import QVideoFrame
from PySide6.QtWidgets import QWidget

from . import theme
from .ink import InkCanvas, fit_rect

log = logging.getLogger(__name__)

__all__ = ["FrameRenderer", "VideoCanvas", "render_frame"]

#: Enough for 1080p at 2x (60 frames a second) with room to spare.
RENDER_THREADS = 2


def render_frame(frame: QVideoFrame, view: QSize, ratio: float) -> tuple[QImage, QSize]:
    """``frame`` as a picture that fills ``view`` (logical pixels) at ``ratio``.

    Returns the picture - already the size it is shown at, so painting it is a
    copy - and the frame's own size. Both are empty for an unusable frame.
    """
    full = frame.toImage() if frame.isValid() else QImage()
    if full.isNull() or view.isEmpty():
        return QImage(), full.size() if not full.isNull() else QSize()
    rect = fit_rect(full.size(), QRectF(0, 0, view.width(), view.height()))
    width = max(1, round(rect.width() * ratio))
    height = max(1, round(rect.height() * ratio))
    picture = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(picture)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QRectF(0, 0, width, height), full)
    painter.end()
    picture.setDevicePixelRatio(ratio)
    return picture, full.size()


class _RenderPool:
    """Conversion threads shared by every renderer; they live as long as the app."""

    def __init__(self, size: int) -> None:
        self.size = size
        self.condition = threading.Condition()
        #: Renderers with a frame waiting, oldest first.
        self.waiting: deque[FrameRenderer] = deque()
        self._threads: list[threading.Thread] = []

    def post(self, renderer: FrameRenderer) -> None:
        """``renderer`` has a frame waiting. Call with :attr:`condition` held."""
        if not self._threads:
            self._threads = [
                threading.Thread(target=self._work, name=f"frame-render-{i}", daemon=True)
                for i in range(self.size)
            ]
            for thread in self._threads:
                thread.start()
        if renderer not in self.waiting:
            self.waiting.append(renderer)
        self.condition.notify()

    def _work(self) -> None:
        while True:
            with self.condition:
                while not self.waiting:
                    self.condition.wait()
                renderer = self.waiting.popleft()
                job = renderer._take()
                if job is None:
                    continue
            sequence, frame, view, ratio = job
            try:
                picture, size = render_frame(frame, view, ratio)
            except Exception:  # noqa: BLE001 - one bad frame must not end drawing
                log.warning("Could not convert a video frame", exc_info=True)
                picture, size = QImage(), QSize()
            with self.condition:
                renderer._finished(sequence, picture, size)


_POOL = _RenderPool(RENDER_THREADS)


class FrameRenderer(QObject):
    """Converts the newest submitted frame on the shared background threads.

    Only the latest submission waits: submitting again replaces it. Results
    carry their submission number, so a slow conversion finishing after a
    quicker, newer one can be recognised and dropped.
    """

    #: (submission number, picture, the frame's own size)
    rendered = Signal(int, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._job: tuple[int, QVideoFrame, QSize, float] | None = None
        self._sequence = 0
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        with _POOL.condition:
            self._running = True
            if self._job is not None:
                _POOL.post(self)

    def stop(self) -> None:
        """Take no more frames. Nothing is emitted once this returns.

        It never waits for a frame being converted - that one is dropped when
        it is done. Waiting here once held the interface thread for two
        seconds: Qt can need that very thread to finish a conversion.
        """
        with _POOL.condition:
            self._running = False
            self._job = None
            if self in _POOL.waiting:
                _POOL.waiting.remove(self)

    def submit(self, frame: QVideoFrame, view: QSize, ratio: float) -> int:
        with _POOL.condition:
            self._sequence += 1
            self._job = (self._sequence, frame, QSize(view), ratio)
            if self._running:
                _POOL.post(self)
            return self._sequence

    # Called by the pool's threads with the pool's condition held.

    def _take(self) -> tuple[int, QVideoFrame, QSize, float] | None:
        job, self._job = self._job, None
        return job if self._running else None

    def _finished(self, sequence: int, picture: QImage, size: QSize) -> None:
        # Checked with the condition held, as stop() sets it: a renderer
        # stopped meanwhile gets nothing.
        if self._running and not picture.isNull():
            # Queued to the interface thread, which owns the receivers.
            self.rendered.emit(sequence, picture, size)


class VideoCanvas(InkCanvas):
    """The video's current frame, with the marks being drawn over it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._frame = QVideoFrame()
        self._picture = QImage()
        self._frame_size = QSize()
        #: Number of the submission shown, so older results are ignored.
        self._shown = 0
        self._renderer = FrameRenderer()
        self._renderer.rendered.connect(self._on_rendered)

    # -- frames -------------------------------------------------------------

    @property
    def frame(self) -> QVideoFrame:
        return self._frame

    def set_frame(self, frame: QVideoFrame) -> None:
        """The newest frame from the player."""
        self._frame = frame
        if not frame.isValid():
            self._picture = QImage()
            self._frame_size = QSize()
            self.update()
        elif self.isVisible():
            self._renderer.submit(frame, self.size(), self.devicePixelRatioF())

    def show_now(self) -> None:
        """Convert the current frame here and now.

        Used when drawing is switched on, so the canvas takes over from the
        video widget showing the same picture instead of a blank one.
        """
        picture, size = render_frame(self._frame, self.size(), self.devicePixelRatioF())
        self._picture = picture
        self._frame_size = size
        self.update()

    def _on_rendered(self, sequence: int, picture: object, size: object) -> None:
        if sequence <= self._shown or not isinstance(picture, QImage):
            return
        self._shown = sequence
        self._picture = picture
        if isinstance(size, QSize):
            self._frame_size = size
        self.update()

    # -- InkCanvas ----------------------------------------------------------

    def picture_size(self) -> QSize:
        if not self._frame_size.isEmpty():
            return self._frame_size
        return self._frame.size() if self._frame.isValid() else QSize()

    def paint_picture(self, painter: QPainter, rect: QRectF) -> None:
        if self._picture.isNull():
            return
        ratio = self._picture.devicePixelRatio() or 1.0
        shown = QSize(round(self._picture.width() / ratio), round(self._picture.height() / ratio))
        if abs(shown.width() - rect.width()) <= 1 and abs(shown.height() - rect.height()) <= 1:
            painter.drawImage(rect.topLeft(), self._picture)
        else:
            # The view was resized and the next conversion has not arrived.
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(rect, self._picture)

    def paint_placeholder(self, painter: QPainter) -> None:
        painter.setPen(QColor(theme.SUBTLE))
        painter.drawText(
            self.rect(), int(Qt.AlignmentFlag.AlignCenter),
            "Play the video to draw on it.",
        )

    # -- lifetime -----------------------------------------------------------

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self._renderer.start()
        if self._frame.isValid():
            self._renderer.submit(self._frame, self.size(), self.devicePixelRatioF())

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt naming
        # Threads run only while the canvas is on screen: nothing is converted
        # while the video widget is showing, and nothing outlives the window.
        self._renderer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        if self.isVisible() and self._frame.isValid():
            self._renderer.submit(self._frame, self.size(), self.devicePixelRatioF())

    def shutdown(self) -> None:
        self._renderer.stop()
