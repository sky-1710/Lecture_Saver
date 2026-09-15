"""Local Whisper inference via faster-whisper (CTranslate2).

Runs entirely on this machine. The only network traffic anywhere near this
module is huggingface_hub fetching the model weights the first time a given
model size is used; once cached, transcription works offline.

faster-whisper was chosen over openai-whisper because it is roughly 4x faster
and uses about half the memory on CPU with int8 quantisation - and this machine
has no CUDA GPU, so CPU speed is the entire story.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .segmentation import SentenceSegmenter, Word

log = logging.getLogger(__name__)

__all__ = [
    "MODEL_SIZES",
    "LANGUAGE_CHOICES",
    "WhisperOptions",
    "WhisperEngine",
    "RawSegment",
    "TranscriptionResult",
    "WhisperUnavailableError",
    "ModelLoadError",
    "estimate_seconds",
]


class WhisperUnavailableError(RuntimeError):
    """faster-whisper is not importable."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "The local transcription engine (faster-whisper) is not installed.\n\n"
            "Install the dependencies with:\n"
            "    pip install -r requirements.txt"
            + (f"\n\nDetails: {detail}" if detail else "")
        )


class ModelLoadError(RuntimeError):
    """The Whisper model could not be loaded on any device we tried."""


#: (id, label, approximate download size, rough speed factor vs realtime on a
#: 4-core laptop CPU with int8). The speed factors drive the time estimate the
#: UI shows before a run; they are deliberately conservative.
#: key, label, download size, speed as a multiple of real time on the CPU.
#:
#: Measured on a 4-core/8-thread laptop (i7-1165G7, int8, AVX-512 VNNI) with
#: word timestamps on: a 36-minute lecture takes about 18 minutes with
#: ``medium``. They are deliberately a little pessimistic, since an older or
#: busier machine is slower, and an estimate that overshoots is kinder than one
#: that runs out.
MODEL_SIZES: tuple[tuple[str, str, str, float], ...] = (
    ("tiny", "Tiny - fastest, lowest quality", "75 MB", 18.0),
    ("base", "Base - fast, weak on Korean", "145 MB", 10.0),
    ("small", "Small - balanced (recommended)", "480 MB", 6.0),
    ("medium", "Medium - good quality, slow", "1.5 GB", 2.0),
    ("large-v3", "Large v3 - best quality, very slow", "3.1 GB", 0.7),
)

LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("auto", "Detect automatically"),
    ("ko", "Korean"),
    ("en", "English"),
    ("ja", "Japanese"),
    ("zh", "Chinese"),
)


def estimate_seconds(video_duration: float, model_size: str) -> float:
    """Rough wall-clock estimate for a CPU transcription run."""
    factor = next((f for key, _, _, f in MODEL_SIZES if key == model_size), 1.0)
    return video_duration / factor if factor > 0 else 0.0


def cuda_device_count() -> int:
    """Number of usable CUDA devices. 0 means transcription runs on the CPU."""
    try:
        import ctranslate2

        return int(ctranslate2.get_cuda_device_count())
    except Exception:  # noqa: BLE001 - probing must never break the UI
        log.debug("CUDA probe failed", exc_info=True)
        return 0


@dataclass
class WhisperOptions:
    model_size: str = "small"
    #: ``"auto"`` picks CUDA when available and falls back to CPU.
    device: str = "auto"
    compute_type: str = "auto"
    #: ``"auto"`` lets Whisper detect the language; forcing ``"ko"`` usually
    #: works better for mixed Korean/English lectures because auto-detect
    #: samples only the first 30 seconds and then commits to one language.
    language: str = "auto"
    vad_filter: bool = True
    beam_size: int = 5
    #: Ask Whisper for word times and cut the transcript where sentences end,
    #: rather than wherever its decoder stopped (§10). Costs a little decoding
    #: time and makes every line a sentence, which is what the slide matching
    #: and the notes need.
    sentence_lines: bool = True

    @property
    def whisper_language(self) -> str | None:
        return None if self.language == "auto" else self.language


#: faster-whisper works on 16 kHz mono audio.
SAMPLE_RATE = 16000


@dataclass
class RawSegment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    segments: list[RawSegment]
    language: str | None
    device: str
    compute_type: str
    #: Set when we had to fall back from the requested device/precision, so the
    #: UI can tell the user why it is slower than they asked for (§28).
    fallback_note: str | None = None


SegmentCallback = Callable[[RawSegment, float], None]
CancelCheck = Callable[[], bool]
StatusCallback = Callable[[str], None]


def _resolve_device(requested: str) -> list[tuple[str, str]]:
    """Ordered (device, compute_type) candidates to try.

    CPU int8 is always last so there is a working fallback on any machine (§28).
    """
    if requested == "cpu":
        return [("cpu", "int8")]

    candidates: list[tuple[str, str]] = []
    if requested in ("auto", "cuda"):
        try:
            import ctranslate2

            if ctranslate2.get_cuda_device_count() > 0:
                candidates.append(("cuda", "float16"))
                candidates.append(("cuda", "int8_float16"))
        except Exception:  # noqa: BLE001 - probing must never break startup
            log.debug("CUDA probe failed; continuing with CPU", exc_info=True)

    candidates.append(("cpu", "int8"))
    return candidates


class WhisperEngine:
    """Loads a Whisper model and streams segments out of it."""

    def __init__(self, options: WhisperOptions) -> None:
        self.options = options
        self._model: Any = None
        self._device = ""
        self._compute_type = ""
        self._fallback_note: str | None = None

    # -- loading ------------------------------------------------------------

    def load(self, on_status: StatusCallback | None = None) -> None:
        """Load the model, walking the device fallback chain.

        Downloads the weights on first use for a given model size, which is the
        one moment the application needs the internet.
        """
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise WhisperUnavailableError(str(exc)) from exc

        candidates = _resolve_device(self.options.device)
        if self.options.compute_type != "auto":
            candidates = [(dev, self.options.compute_type) for dev, _ in candidates]

        errors: list[str] = []
        for index, (device, compute_type) in enumerate(candidates):
            if on_status is not None:
                on_status(
                    f"Loading the {self.options.model_size} model ({device}, {compute_type})..."
                )
            try:
                self._model = WhisperModel(
                    self.options.model_size,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=0,  # 0 lets CTranslate2 choose per the machine
                )
            except Exception as exc:  # noqa: BLE001 - any backend failure is a fallback
                log.warning("Model load failed on %s/%s: %s", device, compute_type, exc)
                errors.append(f"{device}/{compute_type}: {exc}")
                continue

            self._device = device
            self._compute_type = compute_type
            if index > 0:
                self._fallback_note = (
                    f"Could not use {candidates[0][0]} ({candidates[0][1]}); "
                    f"running on {device} ({compute_type}) instead."
                )
            return

        raise ModelLoadError(
            "The Whisper model could not be loaded.\n\n"
            "The first run downloads the model, so an internet connection is "
            "needed once. Check your connection and free disk space, then try "
            "again.\n\nTried:\n  " + "\n  ".join(errors)
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
        """Transcribe ``audio_path``, streaming results as they are produced.

        ``on_segment`` receives each segment plus a 0..1 progress fraction.
        Cancellation is checked between segments, so it takes effect within a
        few seconds rather than at the end of the file.

        ``start_at`` continues an interrupted run: the audio before it is
        skipped and every timestamp is still relative to the whole lecture.
        The audio is cut rather than using faster-whisper's clip option,
        which would switch voice activity filtering off.
        """
        if self._model is None:
            raise ModelLoadError("The Whisper model has not been loaded yet.")

        offset = max(0.0, float(start_at))
        source: object = str(audio_path)
        if offset > 0:
            from faster_whisper.audio import decode_audio

            audio = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
            source = audio[int(offset * SAMPLE_RATE):]

        segments_iter, info = self._model.transcribe(
            source,
            language=self.options.whisper_language,
            beam_size=self.options.beam_size,
            vad_filter=self.options.vad_filter,
            # Whisper is prone to looping on silence in long lectures; these are
            # faster-whisper's own guards against that.
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
            # Word times are what let a line be cut where the sentence ends,
            # instead of wherever the decoder happened to stop (§10).
            word_timestamps=self.options.sentence_lines,
        )

        total = duration or offset + float(getattr(info, "duration", 0.0) or 0.0)
        language = getattr(info, "language", None)

        collected: list[RawSegment] = []
        segmenter = SentenceSegmenter() if self.options.sentence_lines else None

        def emit(segments: list[RawSegment], progress: float) -> None:
            for segment in segments:
                collected.append(segment)
                if on_segment is not None:
                    on_segment(segment, progress)

        for item in segments_iter:
            if should_cancel is not None and should_cancel():
                break

            text = (item.text or "").strip()
            words = _words_of(item, offset)
            reached = float(item.end) + offset
            progress = min(1.0, reached / total) if total > 0 else 0.0

            if segmenter is not None and words:
                emit(
                    [
                        RawSegment(start=line.start, end=line.end, text=line.text)
                        for line in segmenter.feed(words)
                    ],
                    progress,
                )
                continue

            # No word times - an older faster-whisper, or a chunk it could not
            # align. Fall back to Whisper's own line, after whatever is pending.
            if segmenter is not None and segmenter.pending:
                emit(
                    [
                        RawSegment(start=line.start, end=line.end, text=line.text)
                        for line in segmenter.flush()
                    ],
                    progress,
                )
            if not text:
                continue
            emit(
                [RawSegment(start=float(item.start) + offset, end=reached, text=text)],
                progress,
            )

        if segmenter is not None:
            # A cancelled run keeps the tail too, so continuing later starts
            # after it rather than repeating it.
            emit(
                [
                    RawSegment(start=line.start, end=line.end, text=line.text)
                    for line in segmenter.flush()
                ],
                1.0 if should_cancel is None or not should_cancel() else 0.0,
            )

        return TranscriptionResult(
            segments=collected,
            language=language,
            device=self._device,
            compute_type=self._compute_type,
            fallback_note=self._fallback_note,
        )

    def unload(self) -> None:
        self._model = None


def _words_of(item: object, offset: float) -> list[Word]:
    """The words of one Whisper segment, shifted by ``offset``.

    Returns nothing when the build or the chunk has no word times, which is
    what the caller falls back on.
    """
    raw = getattr(item, "words", None) or []
    words: list[Word] = []
    for word in raw:
        text = str(getattr(word, "word", "") or "").strip()
        start = getattr(word, "start", None)
        end = getattr(word, "end", None)
        if not text or start is None or end is None:
            continue
        words.append(Word(text=text, start=float(start) + offset, end=float(end) + offset))
    return words
