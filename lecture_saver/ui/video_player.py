"""Video player widget built on QtMultimedia.

Qt's FFmpeg media backend (forced on in ``app.py``) decodes MP4/MOV/MKV/AVI
without any system codec pack, which is why this is a Qt app rather than a
browser-based one.

The widget owns playback state and exposes it as signals; it knows nothing
about transcripts or slides, so the synchronisation policy lives in
``lecture_view.py`` where it can be changed in one place (§12).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal, Slot
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..core.timecode import format_clock

log = logging.getLogger(__name__)

__all__ = ["VideoPlayer"]

SEEK_STEP_MS = 10_000
PLAYBACK_RATES = (0.75, 1.0, 1.25, 1.5, 2.0)


class VideoPlayer(QWidget):
    """Video surface plus transport controls."""

    #: Current playback position, in seconds. Emitted continuously.
    positionChanged = Signal(float)
    #: Media duration in seconds, once known.
    durationChanged = Signal(float)
    #: Playback could not start; carries a user-facing message.
    errorOccurred = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(0.8)

        self._video = QVideoWidget(self)
        self._video.setMinimumHeight(220)
        self._video.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._video.setStyleSheet("background: #101013;")
        self._player.setVideoOutput(self._video)

        self._duration_s = 0.0
        #: True while the user drags the scrubber, so position updates from the
        #: player do not fight the handle under their cursor.
        self._scrubbing = False

        self._build_controls()
        self._connect()

    # -- construction -------------------------------------------------------

    def _build_controls(self) -> None:
        self._play_button = QPushButton("Play")
        self._play_button.setFixedWidth(76)
        self._play_button.setEnabled(False)

        self._back_button = QPushButton("-10s")
        self._forward_button = QPushButton("+10s")
        for button in (self._back_button, self._forward_button):
            button.setFixedWidth(58)
            button.setEnabled(False)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.setEnabled(False)

        self._time_label = QLabel("00:00 / 00:00")
        self._time_label.setMinimumWidth(110)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._rate_box = QComboBox()
        for rate in PLAYBACK_RATES:
            self._rate_box.addItem(f"{rate:g}x", rate)
        self._rate_box.setCurrentIndex(PLAYBACK_RATES.index(1.0))
        self._rate_box.setFixedWidth(66)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)
        controls.addWidget(self._play_button)
        controls.addWidget(self._back_button)
        controls.addWidget(self._forward_button)
        controls.addWidget(self._slider, 1)
        controls.addWidget(self._time_label)
        controls.addWidget(self._rate_box)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self._video, 1)
        layout.addLayout(controls)

    def _connect(self) -> None:
        self._play_button.clicked.connect(self.toggle_play)
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

    # -- public API ---------------------------------------------------------

    def load(self, path: Path) -> None:
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        for widget in (self._play_button, self._back_button, self._forward_button, self._slider):
            widget.setEnabled(True)

    def clear(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self._duration_s = 0.0
        for widget in (self._play_button, self._back_button, self._forward_button, self._slider):
            widget.setEnabled(False)
        self._slider.setRange(0, 0)
        self._time_label.setText("00:00 / 00:00")

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
