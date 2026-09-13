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
MODEL_SIZES: tuple[tuple[str, str, str, float], ...] = (
    ("tiny", "Tiny - fastest, lowest quality", "75 MB", 4.0),
    ("base", "Base - fast, weak on Korean", "145 MB", 2.5),
    ("small", "Small - balanced (recommended)", "480 MB", 1.2),
    ("medium", "Medium - good quality, slow", "1.5 GB", 0.45),
    ("large-v3", "Large v3 - best quality, very slow", "3.1 GB", 0.2),
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

    @property
    def whisper_language(self) -> str | None:
        return None if self.language == "auto" else self.language


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
    ) -> TranscriptionResult:
        """Transcribe ``audio_path``, streaming results as they are produced.

        ``on_segment`` receives each segment plus a 0..1 progress fraction.
        Cancellation is checked between segments, so it takes effect within a
        few seconds rather than at the end of the file.
        """
        if self._model is None:
            raise ModelLoadError("The Whisper model has not been loaded yet.")

        segments_iter, info = self._model.transcribe(
            str(audio_path),
            language=self.options.whisper_language,
            beam_size=self.options.beam_size,
            vad_filter=self.options.vad_filter,
            # Whisper is prone to looping on silence in long lectures; these are
            # faster-whisper's own guards against that.
            condition_on_previous_text=False,
            temperature=[0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
        )

        total = duration or float(getattr(info, "duration", 0.0) or 0.0)
        language = getattr(info, "language", None)

        collected: list[RawSegment] = []
        for item in segments_iter:
            if should_cancel is not None and should_cancel():
                break

            text = (item.text or "").strip()
            if not text:
                continue

            segment = RawSegment(start=float(item.start), end=float(item.end), text=text)
            collected.append(segment)

            progress = min(1.0, segment.end / total) if total > 0 else 0.0
            if on_segment is not None:
                on_segment(segment, progress)

        return TranscriptionResult(
            segments=collected,
            language=language,
            device=self._device,
            compute_type=self._compute_type,
            fallback_note=self._fallback_note,
        )

    def unload(self) -> None:
        self._model = None
