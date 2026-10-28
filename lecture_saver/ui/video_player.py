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
or a memo is untouched. Left and Right (a second back or forward), D (draw),
S (screenshot) and 1-5 (the drawing tools) work the same way.

Holding Left or Right keeps the video moving at a steady pace until the key
comes up. The keyboard's own repeat is not used for it: it fires about 30
times a second, every repeat a seek, and each seek was cancelled by the next
before its picture could be drawn - the video stood still while the key was
held and jumped when it was let go. Instead a timer moves the target a second
at a time, and a new seek is only sent once the last one's frame has arrived.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QImage, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoFrame, QVideoSink
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
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
from .ink import ICON_SIZE, TOOL_LABELS, TOOLS, InkToolBar, draw_icon
from .theme import Role, key_hints, set_flag, set_role
from .video_canvas import VideoCanvas

log = logging.getLogger(__name__)

__all__ = ["FrameShot", "VideoPlayer"]

SEEK_STEP_MS = 10_000
PLAYBACK_RATES = (0.75, 1.0, 1.25, 1.5, 2.0)
#: How far Left and Right move the video. Held down, they keep moving it.
ARROW_STEP_S = 1.0
#: Steps closer together than this go on from the last step's target, not
#: from the position: quick presses come faster than a seek lands.
STEP_CHAIN_S = 0.6
#: A held arrow starts moving the video after this long, then moves it a
#: second every HOLD_INTERVAL_MS - about seven times normal speed.
HOLD_DELAY_MS = 350
HOLD_INTERVAL_MS = 150
#: A hold ends if the key's repeats stop for this long, in case its release
#: never arrives (the window lost the keyboard meanwhile). The first repeat
#: comes after the keyboard's own delay, up to a second.
HOLD_LOST_FIRST_S = 1.2
HOLD_LOST_S = 0.4
#: A seek whose frame has not come in this long is not waited for any more.
SEEK_FRAME_WAIT_S = 1.0

#: Windows' codes for the arrow keys, to ask the keyboard itself.
_VIRTUAL_KEYS = {Qt.Key.Key_Left: 0x25, Qt.Key.Key_Right: 0x27}


def key_is_down(key: Qt.Key) -> bool | None:
    """Whether ``key`` is held down now, by the keyboard's own account.

    ``None`` where that cannot be asked (not Windows): the key's repeats are
    the sign of it being held there. Qt may keep a key's repeats to its
    shortcut, so on Windows the keyboard is asked rather than trusted to them.
    """
    code = _VIRTUAL_KEYS.get(key)
    try:
        import ctypes

        user32 = ctypes.windll.user32
    except (AttributeError, ImportError, OSError):
        return None
    if code is None:
        return None
    return bool(user32.GetAsyncKeyState(code) & 0x8000)

#: The keys shown under the buttons, as the player answers to them now.
WATCHING_KEYS = (
    ("Space", "play / pause"),
    ("← / →", "1 s back / forward"),
    ("D", "draw on the video"),
    ("S", "screenshot"),
)
DRAWING_KEYS = (
    *((str(number), TOOL_LABELS[tool].lower()) for number, tool in enumerate(TOOLS, start=1)),
    ("Ctrl+Z", "undo"),
    ("S", "screenshot"),
    ("D / Esc", "stop drawing"),
    ("Space", "play / pause"),
    ("← / →", "1 s back / forward"),
)


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
        #: Where the last arrow-key step went, and when - see :meth:`step`.
        self._step_target: float | None = None
        self._step_at = 0.0
        #: The arrow being held, if one is: its key and direction (+1 / -1).
        self._hold_key: Qt.Key | None = None
        self._hold_direction = 0.0
        #: When the held key was last heard (its press or a repeat of it).
        self._hold_heard = 0.0
        self._hold_repeating = False
        self._hold_timer = QTimer(self)
        self._hold_timer.timeout.connect(self._on_hold_tick)
        #: A seek sent by the hold whose frame has not arrived yet, and when.
        self._awaiting_frame = False
        self._seek_sent = 0.0
        #: Where the hold's last seek went.
        self._hold_sent: float | None = None

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
        # The keys, always in sight: none of them has a menu to be found in.
        self._keys_label = QLabel()
        self._keys_label.setTextFormat(Qt.TextFormat.RichText)
        self._keys_label.setWordWrap(True)
        set_role(self._keys_label, Role.SUBTLE)
        self._keys_label.setVisible(False)
        self._show_keys()

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
        controls.addWidget(self._keys_label)

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
        # A press steps once; holding is followed by _on_hold_tick, not by
        # the keyboard's repeats (see the module docstring).
        self._back_key = self._window_shortcut(Qt.Key.Key_Left)
        self._forward_key = self._window_shortcut(Qt.Key.Key_Right)
        #: 1-5 choose a drawing tool, only while drawing.
        self._tool_shortcuts = [
            self._window_shortcut(Qt.Key(int(Qt.Key.Key_1) + offset))
            for offset in range(len(TOOLS))
        ]

    def _window_shortcut(self, key: Qt.Key, *, repeat: bool = False) -> QShortcut:
        shortcut = QShortcut(QKeySequence(key), self)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.setAutoRepeat(repeat)
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
        self._back_key.activated.connect(lambda: self._press_arrow(Qt.Key.Key_Left, -1.0))
        self._forward_key.activated.connect(lambda: self._press_arrow(Qt.Key.Key_Right, 1.0))
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
        self._end_hold()
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
        for shortcut in (
            self._play_shortcut, self._draw_shortcut, self._shot_shortcut,
            self._back_key, self._forward_key,
        ):
            shortcut.setEnabled(enabled)
        self._keys_label.setVisible(enabled)

    def _show_keys(self) -> None:
        self._keys_label.setText(key_hints(DRAWING_KEYS if self._drawing else WATCHING_KEYS))

    @property
    def keys_text(self) -> str:
        """The keys line as the user reads it."""
        return self._keys_label.text()

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

    def step(self, seconds: float) -> None:
        """Move by ``seconds``: the arrow keys, once per press and per repeat.

        A step soon after another goes on from where that one went, as long as
        the position has not passed it yet: a held key repeats every ~30 ms,
        faster than a seek lands, and stepping from the position each time
        would leave the video where the first step put it.
        """
        now = time.monotonic()
        base = self.position
        if self._step_target is not None and now - self._step_at < STEP_CHAIN_S:
            base = max(base, self._step_target) if seconds > 0 else min(
                base, self._step_target
            )
        target = max(0.0, base + seconds)
        if self._duration_s > 0:
            target = min(target, self._duration_s)
        self._step_target, self._step_at = target, now
        self.seek(target)

    # -- holding an arrow -----------------------------------------------------

    @property
    def is_holding(self) -> bool:
        return self._hold_key is not None

    def _press_arrow(self, key: Qt.Key, direction: float) -> None:
        """An arrow went down: one step now, and more while it stays down."""
        self._end_hold()
        self.step(direction * ARROW_STEP_S)
        self._hold_key, self._hold_direction = key, direction
        self._hold_heard = time.monotonic()
        self._hold_repeating = False
        self._hold_sent = self._step_target
        # Watching the whole application only while a key is held: it is the
        # one way to hear the key come up wherever the focus is.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._hold_timer.start(HOLD_DELAY_MS)

    def _on_hold_tick(self) -> None:
        now = time.monotonic()
        if self._hold_key is None:
            return
        down = key_is_down(self._hold_key)
        if down is None:
            lost = HOLD_LOST_S if self._hold_repeating else HOLD_LOST_FIRST_S
            down = now - self._hold_heard <= lost
        if not down:
            self._end_hold()
            return
        self._hold_timer.setInterval(HOLD_INTERVAL_MS)
        base = self._step_target if self._step_target is not None else self.position
        target = max(0.0, base + self._hold_direction * ARROW_STEP_S)
        if self._duration_s > 0:
            target = min(target, self._duration_s)
        self._step_target, self._step_at = target, now
        # The target moves at a steady pace; the video follows as fast as its
        # frames come, never cancelling a seek that has not been drawn yet.
        if self._awaiting_frame and now - self._seek_sent < SEEK_FRAME_WAIT_S:
            return
        self._awaiting_frame, self._seek_sent = True, now
        self._hold_sent = target
        self.seek(target)

    def _end_hold(self) -> None:
        if self._hold_key is None:
            return
        self._hold_key = None
        self._hold_timer.stop()
        self._awaiting_frame = False
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        # Where the target got to while the last seek was being drawn.
        if self._step_target is not None and self._step_target != self._hold_sent:
            self.seek(self._step_target)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        if self._hold_key is not None:
            kind = event.type()
            if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and isinstance(
                event, QKeyEvent
            ) and event.key() == self._hold_key:
                if event.isAutoRepeat():
                    self._hold_heard = time.monotonic()
                    self._hold_repeating = True
                    # The hold moves the video; the repeats must not also
                    # move, say, the selection in the list that has the focus.
                    return True
                if kind == QEvent.Type.KeyRelease:
                    self._end_hold()
            elif kind in (QEvent.Type.ApplicationDeactivate, QEvent.Type.WindowDeactivate):
                self._end_hold()
        return super().eventFilter(watched, event)

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
            self._show_keys()
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
        self._awaiting_frame = False
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
