"""Transcription controls: options, progress and cancellation (§27).

The panel shows the honest cost up front - a CPU-only machine transcribing an
hour of lecture with the `medium` model takes hours, and the user deserves to
know that before they start rather than after.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.timecode import format_duration, format_hms
from ..transcription.openvino_engine import gpu_probe
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


def _terms(keywords: str) -> set[str]:
    return {term.casefold() for term in keywords.split(", ") if term}


class TranscribePanel(QFrame):
    """Model/language pickers, a start button, and live progress."""

    startRequested = Signal(object)  # WhisperOptions
    #: Continue an unfinished transcription with these options.
    resumeRequested = Signal(object)  # WhisperOptions
    cancelRequested = Signal()
    #: Read the slide images and fill the keywords with their terms.
    keywordsFromSlidesRequested = Signal()
    #: The GPU probe answered, from its thread.
    _gpuProbed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: Whether OpenVINO sees a GPU: None until the probe has answered.
        self._gpu: bool | None = None
        self._probing = False
        self._gpuProbed.connect(self._on_gpu_probed)
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

        # Several lines, wrapped: terms read off the slides need checking, and
        # forty of them in a one-line box cannot be seen.
        self._keywords_edit = QPlainTextEdit()
        self._keywords_edit.setTabChangesFocus(True)
        self._keywords_edit.setPlaceholderText("e.g. cross validation, Naive Bayes, TLB")
        # Three lines, plus the theme's padding and the document's own margin.
        self._keywords_edit.setFixedHeight(self._keywords_edit.fontMetrics().lineSpacing() * 3 + 26)
        self._keywords_edit.setToolTip(
            "Terms, names and acronyms this lecture uses, separated by commas.\n"
            "Whisper is given them as hints, so it spells them the way you\n"
            f"write them here. Optional; up to {KEYWORD_CHAR_LIMIT} characters are used."
        )
        self._slides_button = QPushButton("From slides")
        #: Whether the lecture has slide images to read, and why not if not.
        self._slides_ready = False
        self._slides_reason = "Detect slides first, then the terms on them can be read."
        self._scanning = False
        self._video_ok = True
        self._keywords_row = QWidget()
        keywords_row = QHBoxLayout(self._keywords_row)
        keywords_row.setContentsMargins(0, 0, 0, 0)
        keywords_row.setSpacing(6)
        keywords_row.addWidget(self._keywords_edit, 1)
        keywords_row.addWidget(self._slides_button, 0, Qt.AlignmentFlag.AlignTop)

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
        form.addRow("Keywords:", self._keywords_row)
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
        self._slides_button.clicked.connect(self.keywordsFromSlidesRequested)
        self._model_box.currentIndexChanged.connect(self._update_estimate)
        self._update_estimate()
        self._refresh_slides_button()

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
        self._keywords_edit.setPlainText(str(values.get("keywords", "") or ""))

    def options(self) -> WhisperOptions:
        return WhisperOptions(
            model_size=str(self._model_box.currentData()),
            language=str(self._language_box.currentData()),
            vad_filter=self._vad_check.isChecked(),
            keywords=normalize_keywords(self.keywords_text),
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
        self._video_ok = enabled
        for widget in self._option_widgets:
            widget.setEnabled(enabled)
        self._refresh_slides_button()
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
        self._refresh_slides_button()

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
        self._refresh_slides_button()

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

    # -- keywords from the slides -------------------------------------------

    def set_slide_keywords_available(self, available: bool, reason: str = "") -> None:
        """Whether there are slide images to read terms off, and why not."""
        self._slides_ready = available
        self._slides_reason = reason
        self._refresh_slides_button()

    def begin_keyword_scan(self) -> None:
        self._scanning = True
        self._slides_button.setText("Reading...")
        self._refresh_slides_button()

    def update_keyword_scan(self, message: str) -> None:
        self._detail_label.setText(message)

    def end_keyword_scan(self, terms: list[str], message: str = "") -> int:
        """Add ``terms`` after whatever was typed; how many were new.

        Typed terms come first, so they are the last to go if the hint runs
        out of room.
        """
        self._scanning = False
        self._slides_button.setText("From slides")
        typed = normalize_keywords(self.keywords_text)
        merged = normalize_keywords(", ".join([typed, *terms]))
        self._keywords_edit.setPlainText(merged)
        self._detail_label.setText(message)
        self._refresh_slides_button()
        return len(_terms(merged) - _terms(typed))

    @property
    def keywords_text(self) -> str:
        return self._keywords_edit.toPlainText()

    def _refresh_slides_button(self) -> None:
        usable = self._slides_ready and self._video_ok and not self._running
        self._slides_button.setEnabled(usable and not self._scanning)
        if self._slides_ready:
            self._slides_button.setToolTip(
                "Read the text on this lecture's slides and add the terms it uses\n"
                "to the keywords. Done on this computer with Windows' own OCR.\n"
                "Check the list before transcribing: OCR can misspell a term."
            )
        else:
            self._slides_button.setToolTip(self._slides_reason)

    # -- internals ----------------------------------------------------------

    def _on_start(self) -> None:
        self.startRequested.emit(self.options())

    def _on_cancel(self) -> None:
        self._cancel_button.setEnabled(False)
        self._cancel_button.setText("Cancelling...")
        self.cancelRequested.emit()

    def _probe_gpu(self) -> None:
        """Ask once, in the background, whether there is a GPU to run on.

        In a separate process and off this thread: the probe takes about a
        second, and OpenVINO used for the GPU here as well as on the
        transcription thread left the application unable to end.
        """
        if self._probing:
            return
        self._probing = True
        threading.Thread(
            target=lambda: self._gpuProbed.emit(gpu_probe()), name="gpu-probe", daemon=True
        ).start()

    def _on_gpu_probed(self, gpu: bool) -> None:
        self._gpu = gpu
        self._probing = False
        self._update_estimate()

    def _update_estimate(self) -> None:
        if self._video_duration <= 0:
            self._estimate_label.setText("")
            return
        model = str(self._model_box.currentData())
        # Say which hardware this machine will actually use rather than assuming.
        if engine_for_model(model) == ENGINE_OPENVINO:
            gpu = self._gpu
            if gpu is None:
                self._probe_gpu()
                self._estimate_label.setText("Checking this computer's GPU for the estimate...")
                return
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
