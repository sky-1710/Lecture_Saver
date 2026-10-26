"""Local Whisper inference on the Intel GPU via OpenVINO (the Turbo model).

faster-whisper (whisper_engine.py) runs on the CPU on a machine like this one:
CTranslate2's GPU support is CUDA-only, so an Intel integrated GPU sits idle.
OpenVINO, Intel's own inference runtime, runs Whisper on that GPU. Measured on
an i7-1165G7 with Iris Xe, Whisper large-v3-turbo transcribes Korean lecture
audio about four times faster than faster-whisper's ``medium`` on the CPU, and
at least as well. Without a usable GPU it runs on the CPU, still faster than
``medium``.

Everything stays on this machine. The model - Intel's pre-converted int8 build
of large-v3-turbo - is downloaded from Hugging Face once and read from the
local cache after that.

The audio goes in one window at a time (chunking.py): voice activity detection
finds the speech, and each window of at most 30 seconds is decoded on its own.
That gives progress and cancellation between windows, skips silence, and keeps
Whisper from inventing text in silence - which it does when handed nothing but.
"""

from __future__ import annotations

import bisect
import itertools
import logging
import zlib
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .chunking import WINDOW_SECONDS, Window, plan_windows
from .segmentation import SentenceSegmenter, Word
from .whisper_engine import (
    ENGINE_OPENVINO,
    SAMPLE_RATE,
    CancelCheck,
    ModelLoadError,
    RawSegment,
    SegmentCallback,
    StatusCallback,
    TranscriptionResult,
    WhisperOptions,
)

log = logging.getLogger(__name__)

__all__ = [
    "COMPRESSION_RATIO_LIMIT",
    "OPENVINO_MODELS",
    "OpenVinoEngine",
    "OpenVinoUnavailableError",
    "openvino_gpu_available",
]

#: Intel's pre-converted builds on Hugging Face. int8 weights are half the size
#: of fp16 and a little faster on Iris Xe, with the same output on the lecture
#: they were compared on (97 % of words identical).
OPENVINO_MODELS: dict[str, str] = {
    "large-v3-turbo": "OpenVINO/whisper-large-v3-turbo-int8-ov",
}

#: Whisper output that compresses better than this is a loop ("리리리리..."),
#: not speech. The same test and threshold faster-whisper uses.
COMPRESSION_RATIO_LIMIT = 2.4

_LOADING = {
    "GPU": "Preparing the Turbo model for the Intel GPU (about a minute the first time)...",
    "CPU": "Loading the Turbo model (cpu)...",
}


class OpenVinoUnavailableError(ModelLoadError):
    """OpenVINO is not installed, or could not run the model on any device."""


@lru_cache(maxsize=1)
def openvino_gpu_available() -> bool:
    """Whether OpenVINO sees a GPU it can run on. Probed once per session."""
    try:
        import openvino

        return any(device.startswith("GPU") for device in openvino.Core().available_devices)
    except Exception:  # noqa: BLE001 - probing must never break the UI
        log.debug("OpenVINO GPU probe failed", exc_info=True)
        return False


class OpenVinoEngine:
    """Loads the Turbo model into OpenVINO and transcribes window by window.

    The same interface as :class:`WhisperEngine`, so the pipeline can use either.
    """

    def __init__(self, options: WhisperOptions, cache_dir: Path | None = None) -> None:
        self.options = options
        #: Where OpenVINO keeps compiled GPU kernels, so only the first load
        #: pays for compiling them.
        self._cache_dir = cache_dir
        self._pipeline: Any = None
        self._device = ""
        self._fallback_note: str | None = None
        self._hotwords: str | None = options.keywords or None

    # -- loading ------------------------------------------------------------

    def load(self, on_status: StatusCallback | None = None) -> None:
        """Load the model on the GPU, else the CPU.

        Raises :class:`OpenVinoUnavailableError` when OpenVINO cannot run here
        at all - the caller falls back to faster-whisper - and plain
        :class:`ModelLoadError` when the model cannot be had.
        """
        try:
            import openvino_genai
        except ImportError as exc:
            raise OpenVinoUnavailableError(f"OpenVINO is not installed ({exc}).") from exc

        repo = OPENVINO_MODELS.get(self.options.model_size)
        if repo is None:
            raise ModelLoadError(
                f"There is no OpenVINO build of the {self.options.model_size} model."
            )
        model_dir = _model_dir(repo, on_status)

        use_gpu = self.options.device != "cpu" and openvino_gpu_available()
        devices = ["GPU", "CPU"] if use_gpu else ["CPU"]
        cache_dir = self._cache_dir or _default_cache_dir()

        errors: list[str] = []
        for index, device in enumerate(devices):
            if on_status is not None:
                on_status(_LOADING[device])
            properties: dict[str, Any] = {"CACHE_DIR": str(cache_dir / device)}
            if self.options.sentence_lines:
                # Word times need the model's attention outputs, which have to
                # be asked for when the pipeline is built.
                properties["word_timestamps"] = True
            try:
                self._pipeline = openvino_genai.WhisperPipeline(
                    str(model_dir), device, **properties
                )
            except Exception as exc:  # noqa: BLE001 - any backend failure is a fallback
                log.warning("OpenVINO load failed on %s: %s", device, exc)
                errors.append(f"{device}: {exc}")
                continue

            self._device = device.lower()
            if index > 0:
                self._fallback_note = (
                    "The Intel GPU could not run the Turbo model, so it ran on the "
                    "CPU instead, which is slower."
                )
            return

        raise OpenVinoUnavailableError(
            "OpenVINO could not run the Turbo model.\n\nTried:\n  " + "\n  ".join(errors)
        )

    # -- transcription ------------------------------------------------------

    def transcribe(
        self,
        audio_path: Path,
        *,
        duration: float = 0.0,
        on_segment: SegmentCallback | None = None,
        should_cancel: CancelCheck | None = None,
        start_at: float = 0.0,
    ) -> TranscriptionResult:
        """Transcribe ``audio_path``, streaming lines as each window is decoded.

        Same contract as :meth:`WhisperEngine.transcribe`: ``start_at``
        continues an interrupted run with times still relative to the whole
        lecture, and cancellation is checked between windows.
        """
        if self._pipeline is None:
            raise ModelLoadError("The Whisper model has not been loaded yet.")

        offset = max(0.0, float(start_at))
        audio = _load_audio(audio_path)
        if offset > 0:
            audio = audio[int(offset * SAMPLE_RATE):]
        total = duration or offset + len(audio) / SAMPLE_RATE

        if self.options.vad_filter:
            regions = _speech_regions(audio)
        else:
            regions = [(0.0, len(audio) / SAMPLE_RATE)]
        windows = plan_windows(regions)

        config = self._pipeline.get_generation_config()
        config.task = "transcribe"
        config.return_timestamps = True
        config.word_timestamps = self.options.sentence_lines
        language = self.options.whisper_language
        if language:
            config.language = f"<|{language}|>"

        collected: list[RawSegment] = []
        segmenter = SentenceSegmenter() if self.options.sentence_lines else None

        def emit(segments: list[RawSegment], progress: float) -> None:
            for segment in segments:
                collected.append(segment)
                if on_segment is not None:
                    on_segment(segment, progress)

        def lines_of(found: list[Any]) -> list[RawSegment]:
            return [RawSegment(start=line.start, end=line.end, text=line.text) for line in found]

        for window in windows:
            if should_cancel is not None and should_cancel():
                break

            samples = np.concatenate(
                [audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)]
                 for start, end in window.pieces]
            )
            result = self._generate(samples, config)

            if language is None:
                language = str(getattr(result, "language", "") or "") or None
                if language:
                    # Detected on the first window, then held: a mixed
                    # Korean/English lecture must not switch language from one
                    # window to the next.
                    config.language = f"<|{language}|>"

            progress = min(1.0, (window.end + offset) / total) if total > 0 else 0.0
            words, chunks = _usable(result, window, offset)

            if segmenter is not None and words:
                emit(lines_of(segmenter.feed(words)), progress)
                continue

            # No word times for this window: use Whisper's own lines, after
            # whatever sentence was still pending.
            if segmenter is not None and segmenter.pending:
                emit(lines_of(segmenter.flush()), progress)
            emit(chunks, progress)

        if segmenter is not None:
            # A cancelled run keeps the tail too, so continuing later starts
            # after it rather than repeating it.
            emit(
                lines_of(segmenter.flush()),
                1.0 if should_cancel is None or not should_cancel() else 0.0,
            )

        return TranscriptionResult(
            segments=collected,
            language=language,
            device=self._device,
            compute_type="int8",
            fallback_note=self._fallback_note,
            engine=ENGINE_OPENVINO,
        )

    def unload(self) -> None:
        self._pipeline = None

    def _generate(self, samples: np.ndarray, config: Any) -> Any:
        """Decode one window, shortening the keyword hints if they do not fit.

        Whisper's prompt has room for about 220 tokens; how many characters
        that is depends on the script, so the hints are halved until it fits
        rather than guessed at up front.
        """
        while True:
            config.hotwords = self._hotwords
            try:
                return self._pipeline.generate(samples, config)
            except RuntimeError as exc:
                if not self._hotwords or "prompt" not in str(exc):
                    raise
                shorter = _halve(self._hotwords)
                log.warning(
                    "Keyword hints too long for Whisper's prompt; keeping %d of %d characters",
                    len(shorter), len(self._hotwords),
                )
                self._hotwords = shorter or None


def _usable(result: Any, window: Window, offset: float) -> tuple[list[Word], list[RawSegment]]:
    """One window's words and Whisper lines, in lecture time, loops removed."""
    chunks = list(getattr(result, "chunks", None) or [])

    def span(chunk: Any) -> tuple[float, float]:
        start = float(chunk.start_ts)
        end = float(chunk.end_ts)
        # The last line of a window can come without a closing timestamp.
        return start, end if end >= start else window.duration

    loops = [span(chunk) for chunk in chunks if _is_loop(str(chunk.text))]
    if loops:
        log.info("Dropped %d looping line(s) near %.1fs", len(loops), window.start + offset)

    def lecture_time(seconds: float) -> float:
        return window.to_source(seconds) + offset

    kept: list[tuple[float, float, str]] = []
    for item in getattr(result, "words", None) or []:
        text = str(getattr(item, "word", "") or "").strip()
        if not text:
            continue
        start, end = float(item.start_ts), float(item.end_ts)
        middle = (start + end) / 2
        if any(low <= middle <= high for low, high in loops):
            continue
        kept.append((start, end, text))

    lines: list[RawSegment] = []
    line_ends: list[float] = []  # in window time, to compare with word times
    for chunk in chunks:
        text = str(chunk.text).strip()
        if not text or _is_loop(text):
            continue
        start, end = span(chunk)
        lines.append(RawSegment(start=lecture_time(start), end=lecture_time(end), text=text))
        line_ends.append(end)

    # These word times run edge to edge, with no silences for the line breaking
    # to find; where Whisper ended its own lines stands in for one. The words
    # are matched to those lines by their letters, since word times can lag
    # the line times by a word; by time only if the letters disagree.
    chunk_ends = _ends_by_text([line.text for line in lines], [text for _, _, text in kept])
    if chunk_ends is None:
        middles = [(start + end) / 2 for start, end, _ in kept]
        chunk_ends = {bisect.bisect_right(middles, end) - 1 for end in line_ends} - {-1}

    words = [
        Word(text=text, start=lecture_time(start), end=lecture_time(end),
             ends_chunk=index in chunk_ends)
        for index, (start, end, text) in enumerate(kept)
    ]
    return words, lines


def _ends_by_text(line_texts: list[str], word_texts: list[str]) -> set[int] | None:
    """Indexes of the words that finish each line, matched by letter counts.

    Whitespace is ignored - Whisper writes "50%" in a line and "50", "%" as
    words. Both come from the same decoded tokens, so their letters are the
    same; if the totals still disagree, ``None`` is returned.
    """
    def letters(text: str) -> int:
        return len("".join(text.split()))

    targets = list(itertools.accumulate(letters(text) for text in line_texts))
    if not targets or targets[-1] != sum(letters(text) for text in word_texts):
        return None

    ends: set[int] = set()
    seen, target = 0, 0
    for index, text in enumerate(word_texts):
        seen += letters(text)
        while target < len(targets) and seen >= targets[target]:
            ends.add(index)
            target += 1
    return ends


def _is_loop(text: str) -> bool:
    data = text.strip().encode("utf-8")
    if not data:
        return False
    return len(data) / len(zlib.compress(data)) > COMPRESSION_RATIO_LIMIT


def _halve(keywords: str) -> str:
    """The first half of the comma-separated terms."""
    terms = [term.strip() for term in keywords.split(",") if term.strip()]
    return ", ".join(terms[: len(terms) // 2])


def _model_dir(repo: str, on_status: StatusCallback | None) -> Path:
    """The local copy of ``repo``, downloading it the first time."""
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(repo, local_files_only=True))
    except Exception:  # noqa: BLE001 - not cached yet
        log.info("%s is not cached; downloading", repo)

    if on_status is not None:
        on_status("Downloading the Turbo model (830 MB, first time only)...")
    try:
        return Path(snapshot_download(repo))
    except Exception as exc:  # noqa: BLE001 - network, disk, or hub failure
        raise ModelLoadError(
            "The Turbo model could not be downloaded.\n\n"
            "The first run downloads the model, so an internet connection is "
            "needed once. Check your connection and free disk space, then try "
            f"again.\n\nDetails: {exc}"
        ) from exc


def _default_cache_dir() -> Path:
    from ..config import app_data_dir

    return app_data_dir() / "openvino_cache"


def _load_audio(path: Path) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    return decode_audio(str(path), sampling_rate=SAMPLE_RATE)


def _speech_regions(audio: np.ndarray) -> list[tuple[float, float]]:
    """Where there is speech, in seconds, each region shorter than a window."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    options = VadOptions(max_speech_duration_s=WINDOW_SECONDS - 1.0)
    stamps = get_speech_timestamps(audio, options, sampling_rate=SAMPLE_RATE)
    return [(stamp["start"] / SAMPLE_RATE, stamp["end"] / SAMPLE_RATE) for stamp in stamps]
