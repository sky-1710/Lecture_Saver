"""Video player widget built on QtMultimedia.

Qt's FFmpeg media backend (forced on in ``app.py``) decodes MP4/MOV/MKV/AVI
without any system codec pack, which is why this is a Qt app rather than a
browser-based one.

The widget owns playback state and exposes it as signals; it knows nothing
about transcripts or slides, so the synchronisation policy lives in
``lecture_view.py`` where it can be changed in one place (§12).

Frames go to a sink of the player's own first, and from there to whichever
surface is showing: the ``QVideoWidget`` normally, or - while the user draws
over the video - the :class:`~lecture_saver.ui.video_canvas.VideoCanvas` that
paints them with the marks on top. Only the surface showing is given frames:
a hidden video widget kept busy cost a tenth of the frames at 2x, and
changing the player's output instead stalled playback for over a second.
Playback carries on either way, at any speed. A screenshot is the frame on
screen at its full size, with the marks that were on it when drawing is on;
what is done with it is up to the owner.

Space plays and pauses from anywhere in the window, as in any media player.
It is a window shortcut rather than a key handler on the widget, so it works
whatever was clicked last - a slide, a transcript row, a button - while text
boxes still get their spaces: Qt lets a focused text input claim a key before
any shortcut sees it, so typing in a segment, the search field, the find bar
or a memo is untouched. D (draw), S (screenshot) and 1-5 (the drawing tools)
work the same way.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.captures import Mark, marking_start
from ..core.timecode import format_clock
from . import theme
from .ink import ICON_SIZE, TOOLS, InkToolBar, draw_icon
from .theme import Role, set_flag, set_role
from .video_canvas import VideoCanvas

log = logging.getLogger(__name__)

__all__ = ["FrameShot", "VideoPlayer"]

SEEK_STEP_MS = 10_000
PLAYBACK_RATES = (0.75, 1.0, 1.25, 1.5, 2.0)


@dataclass(frozen=True)
class FrameShot:
    """A screenshot as taken: the frame and the marks that were on it."""

    #: The frame at its full size, without the marks.
    image: QImage
    #: The moment of the video it shows, in seconds.
    time: float
    #: When the first of its marks was drawn; ``time`` when there are none.
    start: float
    marks: tuple[Mark, ...]


class VideoPlayer(QWidget):
    """Video surface plus transport controls."""

    #: Current playback position, in seconds. Emitted continuously.
    positionChanged = Signal(float)
    #: Media duration in seconds, once known.
    durationChanged = Signal(float)
    #: Playback could not start; carries a user-facing message.
    errorOccurred = Signal(str)
    #: Drawing over the video was switched on or off.
    drawingChanged = Signal(bool)
    #: A screenshot was taken; carries a :class:`FrameShot`.
    screenshotTaken = Signal(object)
    #: A screenshot could not be taken; carries why, for the user.
    screenshotFailed = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(0.8)

        # Every frame comes here first and is handed on to the surface showing.
        self._sink = QVideoSink(self)
        self._player.setVideoOutput(self._sink)
        self._frame = QVideoFrame()

        self._video = QVideoWidget()
        self._video.setStyleSheet(f"background: {theme.VIDEO_BACKDROP};")
        self._canvas = VideoCanvas()
        self._surface = QStackedWidget()
        self._surface.addWidget(self._video)
        self._surface.addWidget(self._canvas)
        self._surface.setMinimumHeight(220)
        self._surface.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._drawing = False
        #: When the first mark since the last screenshot was drawn: where the
        #: stretch of the lecture the next screenshot covers begins.
        self._marking_since: float | None = None

        self._duration_s = 0.0
        #: True while the user drags the scrubber, so position updates from the
        #: player do not fight the handle under their cursor.
        self._scrubbing = False

        self._build_controls()
        self._build_shortcuts()
        self._connect()

    # -- construction -------------------------------------------------------

    def _build_controls(self) -> None:
        self._play_button = QPushButton("Play")
        self._play_button.setFixedWidth(80)
        self._play_button.setMinimumHeight(30)
        self._play_button.setEnabled(False)

        self._back_button = QPushButton("-10s")
        self._forward_button = QPushButton("+10s")
        for button in (self._back_button, self._forward_button):
            button.setFixedWidth(58)
            button.setMinimumHeight(30)
            button.setEnabled(False)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setEnabled(False)
        self._slider.setMinimumWidth(80)

        self._time_label = QLabel("00:00 / 00:00")
        set_role(self._time_label, Role.MUTED)
        self._time_label.setMinimumWidth(110)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._rate_box = QComboBox()
        for rate in PLAYBACK_RATES:
            self._rate_box.addItem(f"{rate:g}x", rate)
        self._rate_box.setCurrentIndex(PLAYBACK_RATES.index(1.0))
        self._rate_box.setFixedWidth(72)
        self._rate_box.setToolTip("Playback speed")
        self._play_button.setToolTip("Play or pause (Space)")

        self._draw_button = QPushButton("Draw")
        self._draw_button.setIcon(draw_icon())
        self._draw_button.setIconSize(QSize(ICON_SIZE - 2, ICON_SIZE - 2))
        self._draw_button.setCheckable(True)
        self._draw_button.setMinimumHeight(30)
        self._draw_button.setEnabled(False)
        self._draw_button.setToolTip(
            "Draw over the video while it plays (D): the pen - free pen, underline, "
            "highlight, eraser - and typed memos.\n"
            "Space still pauses. Switching off hides the marks; they are back "
            "when you switch on again."
        )
        self._shot_button = QPushButton("Screenshot")
        self._shot_button.setMinimumHeight(30)
        self._shot_button.setEnabled(False)
        self._shot_button.setToolTip(
            "Save the picture on screen, with your marks on it, to this lecture's "
            "screenshots (S)."
        )
        self._ink_bar = InkToolBar(self._canvas, keys=True)
        self._ink_bar.setVisible(False)

        # The transport sits on its own card under the picture, in two rows:
        # the scrubber keeps its full width however narrow the pane gets.
        scrub_row = QHBoxLayout()
        scrub_row.setContentsMargins(0, 0, 0, 0)
        scrub_row.setSpacing(10)
        scrub_row.addWidget(self._slider, 1)
        scrub_row.addWidget(self._time_label)

        button_row = QHBoxLayout()
        button_row.setContentsMargins(0, 0, 0, 0)
        button_row.setSpacing(8)
        button_row.addWidget(self._play_button)
        button_row.addWidget(self._back_button)
        button_row.addWidget(self._forward_button)
        button_row.addStretch(1)
        button_row.addWidget(self._draw_button)
        button_row.addWidget(self._shot_button)
        button_row.addWidget(self._rate_box)

        self._controls = QFrame()
        set_flag(self._controls, "card")
        controls = QVBoxLayout(self._controls)
        controls.setContentsMargins(12, 10, 12, 10)
        controls.setSpacing(8)
        controls.addLayout(scrub_row)
        controls.addLayout(button_row)
        controls.addWidget(self._ink_bar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._surface, 1)
        layout.addWidget(self._controls)

    def _build_shortcuts(self) -> None:
        self._play_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self._play_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        # Holding Space down would otherwise flicker between play and pause.
        self._play_shortcut.setAutoRepeat(False)
        # Off until there is a video, so Space keeps its usual meaning then.
        self._play_shortcut.setEnabled(False)

        self._draw_shortcut = self._window_shortcut(Qt.Key.Key_D)
        self._shot_shortcut = self._window_shortcut(Qt.Key.Key_S)
        #: 1-5 choose a drawing tool, only while drawing.
        self._tool_shortcuts = [
            self._window_shortcut(Qt.Key(int(Qt.Key.Key_1) + offset))
            for offset in range(len(TOOLS))
        ]

    def _window_shortcut(self, key: Qt.Key) -> QShortcut:
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setAutoRepeat(False)
        shortcut.setEnabled(False)
        return shortcut

    def _connect(self) -> None:
        self._play_button.clicked.connect(self.toggle_play)
        self._play_shortcut.activated.connect(self.toggle_play)
        self._back_button.clicked.connect(lambda: self.skip(-SEEK_STEP_MS / 1000))
        self._forward_button.clicked.connect(lambda: self.skip(SEEK_STEP_MS / 1000))
        self._rate_box.currentIndexChanged.connect(self._on_rate_changed)

        self._slider.sliderPressed.connect(self._on_scrub_start)
        self._slider.sliderReleased.connect(self._on_scrub_end)
        self._slider.sliderMoved.connect(self._on_scrub_move)

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)
        self._sink.videoFrameChanged.connect(self._show_frame)

        self._draw_button.toggled.connect(self.set_drawing)
        self._shot_button.clicked.connect(self.take_screenshot)
        self._draw_shortcut.activated.connect(self.toggle_drawing)
        self._shot_shortcut.activated.connect(self.take_screenshot)
        for tool, shortcut in zip(TOOLS, self._tool_shortcuts, strict=True):
            shortcut.activated.connect(lambda t=tool: self._canvas.set_tool(t))
        self._canvas.markAdded.connect(self._on_mark_added)
        self._canvas.marksChanged.connect(self._on_marks_changed)
        self._canvas.escapePressed.connect(lambda: self.set_drawing(False))

    # -- public API ---------------------------------------------------------

    def load(self, path: Path) -> None:
        # Marks drawn on another lecture's video mean nothing on this one.
        self._forget_marks()
        self._show_frame(QVideoFrame())
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self._set_controls_enabled(True)

    def clear(self) -> None:
        self.set_drawing(False)
        self._player.stop()
        self._player.setSource(QUrl())
        self._duration_s = 0.0
        self._set_controls_enabled(False)
        self._forget_marks()
        self._show_frame(QVideoFrame())
        self._slider.setRange(0, 0)
        self._time_label.setText("00:00 / 00:00")

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self._play_button, self._back_button, self._forward_button, self._slider,
            self._draw_button, self._shot_button,
        ):
            widget.setEnabled(enabled)
        for shortcut in (self._play_shortcut, self._draw_shortcut, self._shot_shortcut):
            shortcut.setEnabled(enabled)

    def _forget_marks(self) -> None:
        self._canvas.set_marks(())
        self._marking_since = None

    @property
    def position(self) -> float:
        return self._player.position() / 1000.0

    @property
    def duration(self) -> float:
        return self._duration_s

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    @Slot(float)
    def seek(self, seconds: float, *, autoplay: bool = False) -> None:
        """Jump to ``seconds``. Used by transcript clicks (§12)."""
        target = max(0, int(seconds * 1000))
        if self._duration_s > 0:
            target = min(target, int(self._duration_s * 1000))
        self._player.setPosition(target)
        if autoplay and not self.is_playing:
            self._player.play()

    def skip(self, delta_seconds: float) -> None:
        self.seek(self.position + delta_seconds)

    @Slot()
    def toggle_play(self) -> None:
        if self.is_playing:
            self._player.pause()
        else:
            self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def play(self) -> None:
        self._player.play()

    # -- drawing and screenshots ---------------------------------------------

    @property
    def is_drawing(self) -> bool:
        return self._drawing

    @property
    def canvas(self) -> VideoCanvas:
        return self._canvas

    @property
    def ink_bar(self) -> InkToolBar:
        return self._ink_bar

    @property
    def can_draw(self) -> bool:
        return self._draw_button.isEnabled()

    @Slot()
    def toggle_drawing(self) -> None:
        self.set_drawing(not self._drawing)

    @Slot(bool)
    def set_drawing(self, on: bool) -> None:
        """Switch drawing over the video on or off. Playback is not touched."""
        on = bool(on) and self.can_draw
        if on != self._drawing:
            self._drawing = on
            if on:
                # The same picture on the canvas before it replaces the video
                # widget, so switching shows no blank frame.
                self._canvas.set_frame(self._frame)
                self._canvas.show_now()
                self._surface.setCurrentWidget(self._canvas)
                self._canvas.setFocus(Qt.FocusReason.ShortcutFocusReason)
            else:
                self._canvas.commit_memo()
                # The frame drawn on last, so a paused video stays where it was.
                self._video.videoSink().setVideoFrame(self._frame)
                self._surface.setCurrentWidget(self._video)
            self._ink_bar.setVisible(on)
            for shortcut in self._tool_shortcuts:
                shortcut.setEnabled(on)
            self.drawingChanged.emit(on)
        if self._draw_button.isChecked() != self._drawing:
            self._draw_button.blockSignals(True)
            self._draw_button.setChecked(self._drawing)
            self._draw_button.blockSignals(False)
        # The theme draws no "checked" state for buttons, so say it in colour.
        set_flag(self._draw_button, "accent", self._drawing)

    @Slot()
    def take_screenshot(self) -> FrameShot | None:
        """Take the picture on screen, with the marks when drawing is on."""
        if not self._shot_button.isEnabled():
            return None
        frame = self.current_frame
        image = frame.toImage() if frame.isValid() else QImage()
        if image.isNull():
            self.screenshotFailed.emit(
                "There is no picture to take yet - play the video for a moment first."
            )
            return None
        self._canvas.commit_memo()
        started = frame.startTime()
        time = started / 1_000_000 if started >= 0 else self.position
        marks = self._canvas.marks if self._drawing else ()
        shot = FrameShot(
            image=image,
            time=time,
            start=marking_start(self._marking_since, time) if marks else time,
            marks=tuple(marks),
        )
        # Marks drawn from here on belong to the next screenshot's stretch.
        self._marking_since = None
        if self._drawing:
            self._canvas.flash()
        self.screenshotTaken.emit(shot)
        return shot

    def _on_mark_added(self) -> None:
        if self._marking_since is None:
            self._marking_since = self.position

    def _on_marks_changed(self) -> None:
        if not self._canvas.marks:
            self._marking_since = None

    @property
    def current_frame(self) -> QVideoFrame:
        """The frame on screen."""
        return self._frame

    @property
    def video_sink(self) -> QVideoSink:
        """Where the player sends its frames."""
        return self._sink

    def _show_frame(self, frame: QVideoFrame) -> None:
        self._frame = frame
        if self._drawing:
            self._canvas.set_frame(frame)
        else:
            self._video.videoSink().setVideoFrame(frame)

    def shutdown(self) -> None:
        """Stop the drawing threads; for closing the window."""
        self._canvas.shutdown()

    # -- player signals -----------------------------------------------------

    def _on_position(self, ms: int) -> None:
        if not self._scrubbing:
            self._slider.setValue(ms)
        self._update_time_label(ms / 1000.0)
        self.positionChanged.emit(ms / 1000.0)

    def _on_duration(self, ms: int) -> None:
        self._duration_s = ms / 1000.0
        self._slider.setRange(0, ms)
        self._update_time_label(self.position)
        self.durationChanged.emit(self._duration_s)

    def _on_state(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self._play_button.setText("Pause" if playing else "Play")

    def _on_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        log.error("Media player error %s: %s", error, message)
        self.errorOccurred.emit(
            "This video could not be played.\n\n"
            f"{message or 'The format may not be supported by the media backend.'}\n\n"
            "Transcription may still work, because it uses FFmpeg directly "
            "rather than the media player."
        )

    def _on_rate_changed(self, index: int) -> None:
        rate = self._rate_box.itemData(index)
        if rate:
            self._player.setPlaybackRate(float(rate))

    # -- scrubbing ----------------------------------------------------------

    def _on_scrub_start(self) -> None:
        self._scrubbing = True

    def _on_scrub_move(self, value: int) -> None:
        self._update_time_label(value / 1000.0)

    def _on_scrub_end(self) -> None:
        self._scrubbing = False
        self._player.setPosition(self._slider.value())

    def _update_time_label(self, seconds: float) -> None:
        self._time_label.setText(f"{format_clock(seconds)} / {format_clock(self._duration_s)}")
