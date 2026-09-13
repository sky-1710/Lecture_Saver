"""Transcription controls: options, progress and cancellation (§27).

The panel shows the honest cost up front - a CPU-only machine transcribing an
hour of lecture with the `medium` model takes hours, and the user deserves to
know that before they start rather than after.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.timecode import format_duration
from ..transcription.whisper_engine import (
    LANGUAGE_CHOICES,
    MODEL_SIZES,
    WhisperOptions,
    cuda_device_count,
    estimate_seconds,
)

__all__ = ["TranscribePanel"]


class TranscribePanel(QFrame):
    """Model/language pickers, a start button, and live progress."""

    startRequested = Signal(object)  # WhisperOptions
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._video_duration = 0.0
        self._running = False

        self._model_box = QComboBox()
        for key, label, size, _ in MODEL_SIZES:
            self._model_box.addItem(f"{label}  ({size})", key)
        self._model_box.setCurrentIndex([m[0] for m in MODEL_SIZES].index("small"))

        self._language_box = QComboBox()
        for key, label in LANGUAGE_CHOICES:
            self._language_box.addItem(label, key)

        self._vad_check = QCheckBox("Skip silent parts (recommended)")
        self._vad_check.setChecked(True)
        self._vad_check.setToolTip(
            "Uses voice-activity detection to skip silence.\n"
            "Faster, and it reduces Whisper's tendency to invent text during "
            "long pauses."
        )

        self._estimate_label = QLabel()
        self._estimate_label.setWordWrap(True)
        self._estimate_label.setStyleSheet("color: #666;")

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.addRow("Model:", self._model_box)
        form.addRow("Language:", self._language_box)
        form.addRow("", self._vad_check)

        self._start_button = QPushButton("Transcribe lecture")
        self._start_button.setMinimumHeight(32)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setMinimumHeight(32)
        self._cancel_button.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self._start_button, 1)
        buttons.addWidget(self._cancel_button)

        self._stage_label = QLabel("Not transcribed yet.")
        self._stage_label.setWordWrap(True)
        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        self._detail_label.setStyleSheet("color: #666;")

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setTextVisible(True)
        self._progress.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        title = QLabel("Transcription")
        title.setStyleSheet("font-weight: 600;")
        layout.addWidget(title)
        layout.addLayout(form)
        layout.addWidget(self._estimate_label)
        layout.addLayout(buttons)
        layout.addWidget(self._progress)
        layout.addWidget(self._stage_label)
        layout.addWidget(self._detail_label)

        self._start_button.clicked.connect(self._on_start)
        self._cancel_button.clicked.connect(self._on_cancel)
        self._model_box.currentIndexChanged.connect(self._update_estimate)
        self._update_estimate()

    # -- configuration ------------------------------------------------------

    def set_video_duration(self, seconds: float) -> None:
        self._video_duration = seconds
        self._update_estimate()

    def set_defaults(self, values: dict[str, object]) -> None:
        model = str(values.get("model_size", "small"))
        index = self._model_box.findData(model)
        if index >= 0:
            self._model_box.setCurrentIndex(index)

        language = str(values.get("language", "auto"))
        index = self._language_box.findData(language)
        if index >= 0:
            self._language_box.setCurrentIndex(index)

        self._vad_check.setChecked(bool(values.get("vad_filter", True)))

    def options(self) -> WhisperOptions:
        return WhisperOptions(
            model_size=str(self._model_box.currentData()),
            language=str(self._language_box.currentData()),
            vad_filter=self._vad_check.isChecked(),
        )

    def set_already_transcribed(self, count: int) -> None:
        self._start_button.setText("Transcribe again" if count else "Transcribe lecture")
        if count:
            self._stage_label.setText(f"Transcript ready - {count} segments.")

    def set_enabled_for_video(self, enabled: bool, reason: str = "") -> None:
        """Disable transcription when it genuinely cannot run, saying why (§32)."""
        self._start_button.setEnabled(enabled)
        self._model_box.setEnabled(enabled)
        self._language_box.setEnabled(enabled)
        self._vad_check.setEnabled(enabled)
        if not enabled and reason:
            self._stage_label.setText(reason)

    # -- run state ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def begin_run(self) -> None:
        self._running = True
        self._start_button.setVisible(False)
        self._cancel_button.setVisible(True)
        self._cancel_button.setEnabled(True)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)  # indeterminate until a fraction arrives
        self._detail_label.setText("")
        for widget in (self._model_box, self._language_box, self._vad_check):
            widget.setEnabled(False)

    def end_run(self, final_message: str) -> None:
        self._running = False
        self._start_button.setVisible(True)
        self._cancel_button.setVisible(False)
        self._progress.setVisible(False)
        self._stage_label.setText(final_message)
        for widget in (self._model_box, self._language_box, self._vad_check):
            widget.setEnabled(True)

    def update_progress(self, stage: str, fraction: float, message: str) -> None:
        self._stage_label.setText(stage)
        if fraction < 0:
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(fraction * 100))
        self._detail_label.setText(message)

    # -- internals ----------------------------------------------------------

    def _on_start(self) -> None:
        self.startRequested.emit(self.options())

    def _on_cancel(self) -> None:
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelling...")
        self.cancelRequested.emit()

    def _update_estimate(self) -> None:
        if self._video_duration <= 0:
            self._estimate_label.setText("")
            return
        model = str(self._model_box.currentData())
        estimate = estimate_seconds(self._video_duration, model)
        # The estimate model is calibrated for CPU int8, so say which one this
        # machine will actually use rather than assuming.
        if cuda_device_count() > 0:
            hardware = "a CUDA GPU was found, so this should be faster"
        else:
            hardware = "no CUDA GPU was found, so this runs on the CPU"
        self._estimate_label.setText(
            f"Roughly {format_duration(estimate)} on this computer "
            f"({hardware}). You can cancel at any time."
        )
