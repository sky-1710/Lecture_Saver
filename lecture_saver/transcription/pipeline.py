"""The Phase 2 processing pipeline: video -> audio -> transcript (§2, §27).

This module is deliberately Qt-free. It reports progress through callbacks, and
the UI layer wraps it in a worker thread (``ui/workers.py``) so the window never
freezes (§26).

Two things protect a long run:

* **Audio extraction is skipped** when a valid WAV is already present, so a
  failed transcription does not force a re-extract (§26).
* **The transcript is flushed to disk every 25 segments**, so a crash or a
  cancel at 80 % leaves 80 % of the work on disk instead of nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import StageStatus, TranscriptSegment, make_segment_id
from ..core.project import Project, ProjectError
from ..media.errors import Cancelled, MediaError, MissingVideoFileError, NoAudioTrackError
from ..media.ffmpeg_tools import extract_audio
from .whisper_engine import (
    ModelLoadError,
    TranscriptionResult,
    WhisperEngine,
    WhisperOptions,
    WhisperUnavailableError,
)

log = logging.getLogger(__name__)

__all__ = ["Stage", "PipelineUpdate", "TranscriptionPipeline", "PipelineError"]

#: How many segments to accumulate before writing transcript.json again.
FLUSH_EVERY = 25


class PipelineError(RuntimeError):
    """A pipeline stage failed. The message is safe to show the user."""


@dataclass(frozen=True)
class Stage:
    key: str
    label: str


#: The user-facing stage sequence from §27. `slides` and `alignment` are Phase
#: 3-4 and are not part of this pipeline yet.
STAGE_PREPARE = Stage("prepare", "Preparing video")
STAGE_AUDIO = Stage("audio", "Extracting audio")
STAGE_TRANSCRIBE = Stage("transcription", "Transcribing lecture")
STAGE_SAVE = Stage("save", "Saving project")
STAGE_READY = Stage("ready", "Ready")


@dataclass
class PipelineUpdate:
    stage: Stage
    #: 0.0-1.0, or ``None`` when the stage cannot report meaningful progress.
    fraction: float | None = None
    #: Short user-facing note. Technical detail belongs in the log file (§27).
    message: str = ""


UpdateCallback = Callable[[PipelineUpdate], None]
CancelCheck = Callable[[], bool]


class TranscriptionPipeline:
    """Runs audio extraction and transcription for one project."""

    def __init__(
        self,
        project: Project,
        options: WhisperOptions,
        *,
        on_update: UpdateCallback | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> None:
        self.project = project
        self.options = options
        self._on_update = on_update
        self._should_cancel = should_cancel or (lambda: False)

    # -- helpers ------------------------------------------------------------

    def _emit(self, stage: Stage, fraction: float | None = None, message: str = "") -> None:
        if self._on_update is not None:
            self._on_update(PipelineUpdate(stage, fraction, message))

    def _check_cancel(self) -> None:
        if self._should_cancel():
            raise Cancelled("Processing cancelled.")

    # -- entry point --------------------------------------------------------

    def run(self) -> TranscriptionResult:
        try:
            self._prepare()
            self._ensure_audio()
            result = self._transcribe()
        except Cancelled:
            self.project.set_stage("transcription", StageStatus.CANCELLED, "Cancelled by user.")
            raise
        except (MediaError, WhisperUnavailableError, ModelLoadError) as exc:
            self.project.set_stage("transcription", StageStatus.FAILED, str(exc).split("\n")[0])
            raise PipelineError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - never let a worker die silently
            log.exception("Unexpected pipeline failure")
            self.project.set_stage("transcription", StageStatus.FAILED, "Unexpected error.")
            raise PipelineError(
                f"An unexpected error occurred while processing this lecture:\n{exc}"
            ) from exc

        self._emit(STAGE_READY, 1.0, "Ready")
        return result

    # -- stages -------------------------------------------------------------

    def _prepare(self) -> None:
        self._emit(STAGE_PREPARE, None, "Checking the video file...")
        video = self.project.video

        if not video.exists:
            raise MissingVideoFileError(video.path)
        if not video.has_audio:
            raise NoAudioTrackError(video.display_name)

        self._check_cancel()

    def _ensure_audio(self) -> None:
        audio_path = self.project.audio_file

        # Resume: a completed extraction from an earlier run is reused (§26).
        if self.project.has_audio_file and self.project.stage_status("audio") is StageStatus.DONE:
            self._emit(STAGE_AUDIO, 1.0, "Using audio extracted earlier.")
            return

        self._emit(STAGE_AUDIO, 0.0, "Extracting audio...")
        self.project.set_stage("audio", StageStatus.RUNNING)

        try:
            extract_audio(
                self.project.video.as_path,
                audio_path,
                duration=self.project.video.duration,
                on_progress=lambda f: self._emit(STAGE_AUDIO, f),
                should_cancel=self._should_cancel,
            )
        except Cancelled:
            self.project.set_stage("audio", StageStatus.CANCELLED)
            raise
        except MediaError as exc:
            self.project.set_stage("audio", StageStatus.FAILED, str(exc).split("\n")[0])
            raise

        self.project.set_stage("audio", StageStatus.DONE)
        self._emit(STAGE_AUDIO, 1.0, "Audio ready.")

    def _transcribe(self) -> TranscriptionResult:
        self._check_cancel()
        self.project.set_stage("transcription", StageStatus.RUNNING)

        engine = WhisperEngine(self.options)
        self._emit(STAGE_TRANSCRIBE, None, "Loading the transcription model...")
        engine.load(on_status=lambda msg: self._emit(STAGE_TRANSCRIBE, None, msg))

        self._check_cancel()
        self._emit(STAGE_TRANSCRIBE, 0.0, "Transcribing...")

        collected: list[TranscriptSegment] = []

        def on_segment(raw: object, fraction: float) -> None:
            start = getattr(raw, "start", 0.0)
            end = getattr(raw, "end", 0.0)
            text = getattr(raw, "text", "")
            collected.append(
                TranscriptSegment(
                    id=make_segment_id(len(collected) + 1),
                    start=float(start),
                    end=float(end),
                    original_text=str(text),
                )
            )
            if len(collected) % FLUSH_EVERY == 0:
                self._flush(collected)
            self._emit(
                STAGE_TRANSCRIBE,
                fraction,
                f"{len(collected)} segments transcribed",
            )

        try:
            result = engine.transcribe(
                self.project.audio_file,
                duration=self.project.video.duration,
                on_segment=on_segment,
                should_cancel=self._should_cancel,
            )
        finally:
            engine.unload()

        # A cancel mid-transcription still keeps whatever was produced, so the
        # user can look at it rather than losing the run entirely.
        self._check_cancel()

        self._emit(STAGE_SAVE, None, "Saving transcript...")
        self.project.transcript.replace_all(collected, language=result.language)

        settings = self.project.meta.transcription
        settings.model_size = self.options.model_size
        settings.device = self.options.device
        settings.compute_type = self.options.compute_type
        settings.language = self.options.language
        settings.vad_filter = self.options.vad_filter
        settings.beam_size = self.options.beam_size
        settings.detected_language = result.language
        settings.resolved_device = result.device
        settings.resolved_compute_type = result.compute_type

        self.project.save(force_transcript=True)
        self._keep_original()
        self.project.set_stage("transcription", StageStatus.DONE)
        return result

    def _keep_original(self) -> None:
        """Record Whisper's output so the transcript can be reset to it later.

        Losing this only costs the full reset, never the transcription, so a
        failure is logged rather than raised.
        """
        try:
            self.project.save_original_transcript()
        except ProjectError:
            log.warning("Could not keep the original transcription", exc_info=True)

    def _flush(self, collected: list[TranscriptSegment]) -> None:
        """Persist partial results so a crash does not cost the whole run."""
        try:
            self.project.transcript.replace_all(list(collected))
            self.project.transcript.save(force=True)
            self._keep_original()
        except OSError:
            # A failed intermediate flush must not abort a long transcription;
            # the final save will surface any real persistent problem.
            log.warning("Could not flush partial transcript", exc_info=True)
