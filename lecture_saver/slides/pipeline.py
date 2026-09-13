"""Pass C and orchestration: sample -> detect -> capture images -> save (§26, §27).

Qt-free, like the transcription pipeline; the UI wraps it in a worker thread.

Two behaviours matter for long lectures:

* **Descriptors are cached.** Re-running detection on an unchanged video skips
  decoding entirely, so trying a different sensitivity costs milliseconds
  rather than minutes (§10).
* **Old slide images are replaced, not orphaned.** A re-run cleans up the
  images it previously wrote so the project folder does not accumulate them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import StageStatus
from ..core.project import Project
from ..core.slides import Slide, SlideDetectionOptions, SlideSet
from ..media.errors import Cancelled, MediaError, MissingVideoFileError
from ..media.ffmpeg_tools import extract_frame
from .annotation_snapshots import add_annotation_snapshots
from .descriptors import (
    FrameDescriptors,
    compute_descriptors,
    dhash_from_thumbnail,
    hash_to_hex,
    source_key_for,
)
from .detector import detect_appearances

log = logging.getLogger(__name__)

__all__ = [
    "SlideStage",
    "SlideUpdate",
    "SlideDetectionPipeline",
    "SlidePipelineError",
    "capture_slide_image",
]

_SLIDE_IMAGE = re.compile(r"^\d{3,}\.jpg$")


class SlidePipelineError(RuntimeError):
    """Slide detection failed. The message is safe to show the user."""


@dataclass(frozen=True)
class SlideStage:
    key: str
    label: str


STAGE_PREPARE = SlideStage("prepare", "Preparing video")
STAGE_SAMPLE = SlideStage("sample", "Scanning video for slide changes")
STAGE_DETECT = SlideStage("detect", "Identifying slides")
STAGE_CAPTURE = SlideStage("capture", "Capturing slide images")
STAGE_SAVE = SlideStage("save", "Saving project")
STAGE_READY = SlideStage("ready", "Ready")


@dataclass
class SlideUpdate:
    stage: SlideStage
    #: 0.0-1.0, or ``None`` when the stage cannot report meaningful progress.
    fraction: float | None = None
    message: str = ""


UpdateCallback = Callable[[SlideUpdate], None]
CancelCheck = Callable[[], bool]


class SlideDetectionPipeline:
    """Detects slides for one project and stores the result."""

    def __init__(
        self,
        project: Project,
        options: SlideDetectionOptions | None = None,
        *,
        on_update: UpdateCallback | None = None,
        should_cancel: CancelCheck | None = None,
        use_cache: bool = True,
    ) -> None:
        self.project = project
        self.options = options or SlideDetectionOptions()
        self._on_update = on_update
        self._should_cancel = should_cancel or (lambda: False)
        self._use_cache = use_cache
        #: The most recent detection, including per-candidate diagnostics.
        self.last_result: object | None = None

    # -- helpers ------------------------------------------------------------

    def _emit(self, stage: SlideStage, fraction: float | None = None, message: str = "") -> None:
        if self._on_update is not None:
            self._on_update(SlideUpdate(stage, fraction, message))

    def _check_cancel(self) -> None:
        if self._should_cancel():
            raise Cancelled("Slide detection cancelled.")

    # -- entry point --------------------------------------------------------

    def run(self) -> SlideSet:
        try:
            self._prepare()
            descriptors = self._descriptors()
            self._check_cancel()
            slide_set = self._detect_and_capture(descriptors)
        except Cancelled:
            self.project.set_stage("slides", StageStatus.CANCELLED, "Cancelled by user.")
            raise
        except MediaError as exc:
            self.project.set_stage("slides", StageStatus.FAILED, str(exc).split("\n")[0])
            raise SlidePipelineError(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - a worker must never die silently
            log.exception("Unexpected slide detection failure")
            self.project.set_stage("slides", StageStatus.FAILED, "Unexpected error.")
            raise SlidePipelineError(
                f"An unexpected error occurred while detecting slides:\n{exc}"
            ) from exc

        self._emit(STAGE_READY, 1.0, f"{len(slide_set)} slides detected")
        return slide_set

    # -- stages -------------------------------------------------------------

    def _prepare(self) -> None:
        self._emit(STAGE_PREPARE, None, "Checking the video file...")
        if not self.project.video.exists:
            raise MissingVideoFileError(self.project.video.path)
        self._check_cancel()

    def _descriptors(self) -> FrameDescriptors:
        """Load cached descriptors when they are still valid, else build them."""
        cache_path = self.project.frame_cache_file
        signature = self.options.sample_signature
        source = source_key_for(self.project.video.as_path)

        if self._use_cache:
            cached = FrameDescriptors.load(cache_path)
            if cached is not None and cached.matches(
                sample_signature=signature, source_key=source
            ):
                self._emit(
                    STAGE_SAMPLE, 1.0,
                    f"Reusing {len(cached)} frames scanned earlier.",
                )
                return cached

        self._emit(STAGE_SAMPLE, 0.0, "Scanning video...")
        self.project.set_stage("slides", StageStatus.RUNNING)

        descriptors = compute_descriptors(
            self.project.video.as_path,
            options=self.options,
            duration=self.project.video.duration,
            on_progress=lambda f: self._emit(STAGE_SAMPLE, f),
            should_cancel=self._should_cancel,
        )

        if self._use_cache:
            descriptors.save(cache_path)

        self._emit(STAGE_SAMPLE, 1.0, f"{len(descriptors)} frames scanned.")
        return descriptors

    def _detect_and_capture(self, descriptors: FrameDescriptors) -> SlideSet:
        self._emit(STAGE_DETECT, None, "Identifying slide changes...")

        duration = self.project.video.duration
        if duration <= 0 and len(descriptors):
            duration = float(descriptors.times[-1])

        result = detect_appearances(
            descriptors.thumbnails,
            descriptors.times,
            descriptors.hashes,
            duration=duration,
            options=self.options,
            ink=descriptors,
        )
        result = add_annotation_snapshots(result, descriptors, self.options)
        self.last_result = result
        if result.busy.masked_cells:
            log.info(
                "Ignored %d of %d grid cells as permanently busy%s",
                result.busy.masked_cells, result.busy.total_cells,
                " (WARNING: that is a large share of the frame)"
                if result.busy.warn else "",
            )
        self._check_cancel()

        self._clear_previous_images()

        slides: list[Slide] = []
        total = len(result.appearances)
        self._emit(STAGE_CAPTURE, 0.0, f"Capturing {total} slide images...")

        for position, appearance in enumerate(result.appearances):
            self._check_cancel()

            name = f"{appearance.slide_id:03d}.jpg"
            target = self.project.slides_dir / name
            timestamp = result.representative_times[position]

            try:
                extract_frame(
                    self.project.video.as_path,
                    timestamp,
                    target,
                    max_width=self.options.image.max_width,
                    quality=self.options.image.quality,
                )
            except MediaError:
                # One unreadable frame must not throw away a whole detection
                # run; the slide keeps its interval and simply has no image.
                log.warning("Could not capture a frame at %.2fs", timestamp, exc_info=True)
                slides.append(Slide(slide_id=appearance.slide_id, image="", fingerprint=""))
                continue

            fingerprint = self._fingerprint(descriptors, timestamp)
            slides.append(
                Slide(
                    slide_id=appearance.slide_id,
                    image=f"slides/{name}",
                    fingerprint=fingerprint,
                )
            )
            self._emit(
                STAGE_CAPTURE,
                (position + 1) / total if total else 1.0,
                f"{position + 1} of {total}",
            )

        self._emit(STAGE_SAVE, None, "Saving slides...")
        slide_set = SlideSet.build(
            slides,
            result.appearances,
            video_duration=duration,
            options=self.options,
        )
        self.project.set_slides(slide_set)
        self.project.meta.slide_detection = self.options
        self.project.save_slides()
        self.project.set_stage("slides", StageStatus.DONE)
        return slide_set

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _fingerprint(descriptors: FrameDescriptors, timestamp: float) -> str:
        """Perceptual hash of the sampled frame nearest the captured image.

        Recorded for Phase 7 duplicate matching; nothing compares these yet.
        """
        if descriptors.is_empty:
            return ""
        index = int(min(range(len(descriptors)),
                        key=lambda i: abs(float(descriptors.times[i]) - timestamp)))
        return hash_to_hex(dhash_from_thumbnail(descriptors.thumbnails[index]))

    def _clear_previous_images(self) -> None:
        """Remove slide images from an earlier run so re-runs do not accumulate.

        A re-run replaces the whole detection, including any manual
        corrections; the UI confirms that with the user first.
        """
        directory = self.project.slides_dir
        if not directory.is_dir():
            return
        for path in directory.iterdir():
            if path.is_file() and _SLIDE_IMAGE.match(path.name):
                try:
                    path.unlink()
                except OSError:
                    log.warning("Could not remove the old slide image %s", path, exc_info=True)


def capture_slide_image(
    project: Project,
    slide_id: int,
    timestamp: float,
    options: SlideDetectionOptions | None = None,
) -> str:
    """Capture (or replace) the representative image for one slide.

    Used by the manual corrections in the slide navigator: re-capturing a bad
    frame, and giving a hand-made slide boundary a picture. Returns the
    project-relative image path.
    """
    options = options or SlideDetectionOptions()
    name = f"{slide_id:03d}.jpg"
    extract_frame(
        project.video.as_path,
        max(0.0, timestamp),
        project.slides_dir / name,
        max_width=options.image.max_width,
        quality=options.image.quality,
    )
    return f"slides/{name}"
