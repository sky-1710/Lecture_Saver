"""Transcription controls: options, progress and cancellation (§27).

The panel shows the honest cost up front - a CPU-only machine transcribing an
hour of lecture with the `medium` model takes hours, and the user deserves to
know that before they start rather than after.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.timecode import format_duration, format_hms
from ..transcription.openvino_engine import openvino_gpu_available
from ..transcription.whisper_engine import (
    ENGINE_OPENVINO,
    KEYWORD_CHAR_LIMIT,
    LANGUAGE_CHOICES,
    MODEL_SIZES,
    TURBO_MODEL,
    WhisperOptions,
    cuda_device_count,
    engine_for_model,
    estimate_seconds,
    normalize_keywords,
)
from .theme import Role, set_flag, set_role

__all__ = ["TranscribePanel"]


class TranscribePanel(QFrame):
    """Model/language pickers, a start button, and live progress."""

    startRequested = Signal(object)  # WhisperOptions
    #: Continue an unfinished transcription with these options.
    resumeRequested = Signal(object)  # WhisperOptions
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._resume_at: float | None = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        set_flag(self, "card")
        self._video_duration = 0.0
        self._running = False
        #: Once the user has collapsed or expanded by hand, stop doing it for them.
        self._collapse_chosen = False
        self._collapsed = False
        #: Why transcription cannot run, when it cannot - shown instead of the
        #: "not transcribed yet" line, which would be misleading.
        self._blocked_reason = ""

        self._model_box = QComboBox()
        for key, label, size, _ in MODEL_SIZES:
            self._model_box.addItem(f"{label}  ({size})", key)
        self._model_box.setCurrentIndex([m[0] for m in MODEL_SIZES].index(TURBO_MODEL))

        self._language_box = QComboBox()
        for key, label in LANGUAGE_CHOICES:
            self._language_box.addItem(label, key)

        self._keywords_edit = QLineEdit()
        self._keywords_edit.setMaxLength(KEYWORD_CHAR_LIMIT * 2)
        self._keywords_edit.setClearButtonEnabled(True)
        self._keywords_edit.setPlaceholderText("e.g. cross validation, Naive Bayes, TLB")
        self._keywords_edit.setToolTip(
            "Terms, names and acronyms this lecture uses, separated by commas.\n"
            "Whisper is given them as hints, so it spells them the way you\n"
            f"write them here. Optional; up to {KEYWORD_CHAR_LIMIT} characters are used."
        )

        self._vad_check = QCheckBox("Skip silent parts (recommended)")
        self._vad_check.setChecked(True)
        self._vad_check.setToolTip(
            "Uses voice-activity detection to skip silence.\n"
            "Faster, and it reduces Whisper's tendency to invent text during "
            "long pauses."
        )

        self._estimate_label = QLabel()
        self._estimate_label.setWordWrap(True)
        set_role(self._estimate_label, Role.MUTED)

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        # A narrow pane puts the label above its field instead of squeezing both.
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Model:", self._model_box)
        form.addRow("Language:", self._language_box)
        form.addRow("Keywords:", self._keywords_edit)
        form.addRow("", self._vad_check)

        self._start_button = QPushButton("Transcribe lecture")
        self._start_button.setMinimumHeight(34)
        set_flag(self._start_button, "accent")
        self._resume_button = QPushButton("Continue")
        self._resume_button.setMinimumHeight(34)
        set_flag(self._resume_button, "accent")
        self._resume_button.setToolTip(
            "The last transcription stopped before the end. Continue from where "
            "it stopped instead of transcribing the whole lecture again."
        )
        self._resume_button.setVisible(False)
        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.setMinimumHeight(34)
        self._cancel_button.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self._resume_button, 1)
        buttons.addWidget(self._start_button, 1)
        buttons.addWidget(self._cancel_button)

        self._stage_label = QLabel("Not transcribed yet.")
        self._stage_label.setWordWrap(True)
        self._detail_label = QLabel("")
        self._detail_label.setWordWrap(True)
        set_role(self._detail_label, Role.MUTED)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        # The percentage is a label beside the bar rather than text inside it:
        # once the chunk covers it, text in the bar is unreadable.
        self._progress.setTextVisible(False)
        self._percent_label = QLabel("")
        self._percent_label.setMinimumWidth(38)
        self._percent_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        set_role(self._percent_label, Role.SECTION)
        self._progress_row = QWidget()
        progress_row = QHBoxLayout(self._progress_row)
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(8)
        progress_row.addWidget(self._progress, 1)
        progress_row.addWidget(self._percent_label)
        self._progress_row.setVisible(False)

        # Options and buttons fold away once there is a transcript, so a short
        # window keeps its room for the transcript itself.
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(9)
        body_layout.addLayout(form)
        body_layout.addWidget(self._estimate_label)
        body_layout.addLayout(buttons)

        title = QLabel("Transcription")
        set_role(title, Role.SECTION)
        self._toggle = QToolButton()
        self._toggle.setArrowType(Qt.ArrowType.DownArrow)
        self._toggle.setAutoRaise(True)
        self._toggle.setToolTip("Hide the transcription options")
        self._toggle.clicked.connect(self._on_toggle)

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self._toggle)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(9)
        layout.addLayout(title_row)
        layout.addWidget(self._body)
        layout.addWidget(self._progress_row)
        layout.addWidget(self._stage_label)
        layout.addWidget(self._detail_label)

        self._start_button.clicked.connect(self._on_start)
        self._resume_button.clicked.connect(
            lambda: self.resumeRequested.emit(self.options())
        )
        self._cancel_button.clicked.connect(self._on_cancel)
        self._model_box.currentIndexChanged.connect(self._update_estimate)
        self._update_estimate()

    # -- configuration ------------------------------------------------------

    def set_video_duration(self, seconds: float) -> None:
        self._video_duration = seconds
        self._update_estimate()

    def set_defaults(self, values: dict[str, object]) -> None:
        model = str(values.get("model_size", TURBO_MODEL))
        index = self._model_box.findData(model)
        if index >= 0:
            self._model_box.setCurrentIndex(index)

        language = str(values.get("language", "auto"))
        index = self._language_box.findData(language)
        if index >= 0:
            self._language_box.setCurrentIndex(index)

        self._vad_check.setChecked(bool(values.get("vad_filter", True)))
        # Keywords belong to a lecture, so a set without them clears the box
        # rather than carrying the last lecture's terms over.
        self._keywords_edit.setText(str(values.get("keywords", "") or ""))

    def options(self) -> WhisperOptions:
        return WhisperOptions(
            model_size=str(self._model_box.currentData()),
            language=str(self._language_box.currentData()),
            vad_filter=self._vad_check.isChecked(),
            keywords=normalize_keywords(self._keywords_edit.text()),
        )

    def set_already_transcribed(self, count: int) -> None:
        self._start_button.setText("Transcribe again" if count else "Transcribe lecture")
        # Transcribing is the thing to do on an untouched lecture, and rarely the
        # thing to do afterwards - so the button stops shouting once it is done.
        set_flag(self._start_button, "accent", not count)
        if count:
            self._stage_label.setText(f"Transcript ready - {count} segments.")
        else:
            # Opening a second project must not leave the first one's status here.
            self._stage_label.setText(self._blocked_reason or "Not transcribed yet.")
        if not self._collapse_chosen:
            self.set_collapsed(bool(count))

    # -- collapsing ---------------------------------------------------------

    @property
    def is_collapsed(self) -> bool:
        # Tracked rather than read back from the widget, which reports itself
        # hidden while the panel as a whole has not been shown yet.
        return self._collapsed

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self._body.setVisible(not collapsed)
        self._toggle.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self._toggle.setToolTip(
            "Show the transcription options" if collapsed
            else "Hide the transcription options"
        )

    def _on_toggle(self) -> None:
        self._collapse_chosen = True
        self.set_collapsed(not self.is_collapsed)

    def set_resume_point(self, seconds: float | None) -> None:
        """Offer to continue an unfinished transcription from ``seconds``."""
        self._resume_at = seconds
        if seconds is None:
            self._resume_button.setVisible(False)
            return
        self._resume_button.setText(f"Continue from {format_hms(seconds)}")
        self._resume_button.setVisible(not self._running)
        # An unfinished transcription is the one thing here that still needs
        # doing, so never leave the offer to continue folded away.
        self.set_collapsed(False)
        self._stage_label.setText(
            f"The transcription stopped at {format_hms(seconds)}. Continue to "
            "transcribe the rest, or transcribe the whole lecture again."
        )

    @property
    def resume_at(self) -> float | None:
        return self._resume_at

    def set_enabled_for_video(self, enabled: bool, reason: str = "") -> None:
        """Disable transcription when it genuinely cannot run, saying why (§32)."""
        self._blocked_reason = "" if enabled else reason
        self._start_button.setEnabled(enabled)
        self._resume_button.setEnabled(enabled)
        for widget in self._option_widgets:
            widget.setEnabled(enabled)
        if not enabled and reason:
            self._stage_label.setText(reason)

    # -- run state ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    def begin_run(self) -> None:
        self._running = True
        self.set_collapsed(False)
        self._start_button.setVisible(False)
        self._resume_button.setVisible(False)
        self._cancel_button.setVisible(True)
        self._cancel_button.setEnabled(True)
        self._progress_row.setVisible(True)
        self._progress.setRange(0, 0)  # indeterminate until a fraction arrives
        self._percent_label.setText("")
        self._detail_label.setText("")
        for widget in self._option_widgets:
            widget.setEnabled(False)

    def end_run(self, final_message: str) -> None:
        self._running = False
        self._start_button.setVisible(True)
        self._resume_button.setVisible(self._resume_at is not None)
        self._cancel_button.setVisible(False)
        self._progress_row.setVisible(False)
        self._percent_label.setText("")
        self._stage_label.setText(final_message)
        for widget in self._option_widgets:
            widget.setEnabled(True)

    def update_progress(self, stage: str, fraction: float, message: str) -> None:
        self._stage_label.setText(stage)
        if fraction < 0:
            self._progress.setRange(0, 0)
            self._percent_label.setText("...")
        else:
            percent = max(0, min(100, int(fraction * 100)))
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
            self._percent_label.setText(f"{percent}%")
        self._detail_label.setText(message)

    @property
    def percent_text(self) -> str:
        return self._percent_label.text()

    @property
    def _option_widgets(self) -> tuple[QWidget, ...]:
        return (self._model_box, self._language_box, self._keywords_edit, self._vad_check)

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
        # Say which hardware this machine will actually use rather than assuming.
        if engine_for_model(model) == ENGINE_OPENVINO:
            gpu = openvino_gpu_available()
            estimate = estimate_seconds(self._video_duration, model, gpu=gpu)
            hardware = (
                "it runs on the Intel GPU" if gpu
                else "no Intel GPU was found, so it runs on the CPU"
            )
        else:
            # The estimate is calibrated for CPU int8.
            estimate = estimate_seconds(self._video_duration, model)
            if cuda_device_count() > 0:
                hardware = "a CUDA GPU was found, so this should be faster"
            else:
                hardware = "no CUDA GPU was found, so this runs on the CPU"
        self._estimate_label.setText(
            f"Roughly {format_duration(estimate)} on this computer "
            f"({hardware}). You can cancel at any time."
        )
