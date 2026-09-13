"""Project management: the on-disk layout of one lecture (§5).

A project is a plain directory. It is readable and diffable without the
application, and it references the source video by path rather than copying
multi-gigabyte files around (§4).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .alignment import Alignment, align
from .atomic_io import read_json, write_json_atomic
from .models import ProjectMeta, StageStatus, VideoInfo
from .slides import SlideSet
from .transcript_store import TranscriptStore

__all__ = ["Project", "ProjectError", "safe_project_dirname", "is_project_dir"]

log = logging.getLogger(__name__)

PROJECT_FILE = "project.json"
TRANSCRIPT_FILE = "transcript.json"
SLIDES_FILE = "slides.json"


class ProjectError(RuntimeError):
    """A project could not be created, loaded or saved."""


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_project_dirname(name: str) -> str:
    """Turn a lecture title into a directory name Windows will accept."""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)[:80].strip().strip(".")
    # A name made entirely of separators ("///" -> "___") is technically a legal
    # directory name but useless to a person, so it gets the fallback too.
    if not any(ch.isalnum() for ch in cleaned):
        return "lecture"
    if cleaned.upper() in _WINDOWS_RESERVED:
        return f"lecture_{cleaned}"
    return cleaned


def is_project_dir(path: Path) -> bool:
    return (path / PROJECT_FILE).is_file()


class Project:
    """An open lecture project: metadata plus the authoritative transcript."""

    def __init__(
        self,
        root: Path,
        meta: ProjectMeta,
        transcript: TranscriptStore,
        slides: SlideSet | None = None,
    ) -> None:
        self.root = root
        self.meta = meta
        self.transcript = transcript
        #: Detected slides (Phase 3). Empty until detection has been run.
        self.slides = slides if slides is not None else SlideSet()

    # -- paths --------------------------------------------------------------

    @property
    def project_file(self) -> Path:
        return self.root / PROJECT_FILE

    @property
    def transcript_file(self) -> Path:
        return self.root / TRANSCRIPT_FILE

    @property
    def slides_file(self) -> Path:
        return self.root / SLIDES_FILE

    @property
    def cache_dir(self) -> Path:
        """Regenerable intermediates. Safe to delete; costs only a re-scan."""
        return self.root / "cache"

    @property
    def frame_cache_file(self) -> Path:
        """Cached frame descriptors, so re-detection skips decoding (§10)."""
        return self.cache_dir / "frame_descriptors.npz"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def audio_file(self) -> Path:
        """16 kHz mono WAV. Regenerable, so it is safe to delete."""
        return self.audio_dir / "lecture.wav"

    @property
    def slides_dir(self) -> Path:
        """Representative slide images."""
        return self.root / "slides"

    @property
    def exports_dir(self) -> Path:
        """Phase 6 output."""
        return self.root / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    # -- convenience --------------------------------------------------------

    @property
    def name(self) -> str:
        return self.meta.name

    @property
    def video(self) -> VideoInfo:
        return self.meta.video

    @property
    def video_missing(self) -> bool:
        """The source video was moved or deleted since the project was made (§28)."""
        return not self.video.exists

    @property
    def has_audio_file(self) -> bool:
        return self.audio_file.is_file() and self.audio_file.stat().st_size > 0

    @property
    def has_transcript(self) -> bool:
        return not self.transcript.is_empty

    @property
    def has_slides(self) -> bool:
        return not self.slides.is_empty

    def relocate_video(self, new_path: Path) -> None:
        """Point the project at a moved source video and persist the change."""
        self.meta.video.path = str(new_path)
        self.save_meta()

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def create(cls, parent_dir: Path, name: str, video: VideoInfo) -> Project:
        """Create a new project directory.

        ``video`` comes from :func:`lecture_saver.media.ffmpeg_tools.probe_video`,
        which keeps this module free of any media dependency.
        """
        parent_dir = Path(parent_dir)
        root = parent_dir / safe_project_dirname(name)
        if root.exists():
            root = cls._unique_sibling(root)

        try:
            root.mkdir(parents=True)
            (root / "audio").mkdir()
            (root / "slides").mkdir()
            (root / "exports").mkdir()
            (root / "logs").mkdir()
            (root / "cache").mkdir()
        except OSError as exc:
            raise ProjectError(f"Could not create the project folder:\n{exc}") from exc

        meta = ProjectMeta(name=name, video=video)
        project = cls(root, meta, TranscriptStore(root / TRANSCRIPT_FILE))
        project.save(force_transcript=True)
        return project

    @classmethod
    def open(cls, root: Path) -> Project:
        """Load an existing project, restoring the previous editing state (§5)."""
        root = Path(root)
        project_file = root / PROJECT_FILE

        if not project_file.is_file():
            raise ProjectError(
                f"'{root.name}' is not a Lecture Saver project "
                f"(no {PROJECT_FILE} inside it)."
            )
        try:
            raw = read_json(project_file)
        except json.JSONDecodeError as exc:
            raise ProjectError(
                f"{PROJECT_FILE} is damaged and could not be read:\n{exc}"
            ) from exc
        except OSError as exc:
            raise ProjectError(f"Could not read {PROJECT_FILE}:\n{exc}") from exc

        try:
            meta = ProjectMeta.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectError(f"{PROJECT_FILE} has an unexpected structure:\n{exc}") from exc

        if meta.schema_version > 1:
            raise ProjectError(
                f"This project was written by a newer version of Lecture Saver "
                f"(schema {meta.schema_version}). Please update the application."
            )

        try:
            transcript = TranscriptStore.load(root / TRANSCRIPT_FILE)
        except json.JSONDecodeError as exc:
            raise ProjectError(
                f"{TRANSCRIPT_FILE} is damaged and could not be read:\n{exc}"
            ) from exc

        slides = cls._load_slides(root)

        for sub in ("audio", "slides", "exports", "logs", "cache"):
            (root / sub).mkdir(exist_ok=True)

        return cls(root, meta, transcript, slides)

    @staticmethod
    def _load_slides(root: Path) -> SlideSet:
        """Read slides.json. A damaged file costs a re-detection, not the project."""
        path = root / SLIDES_FILE
        if not path.is_file():
            return SlideSet()
        try:
            return SlideSet.from_dict(read_json(path))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
            log.warning("Ignoring an unreadable %s", SLIDES_FILE, exc_info=True)
            return SlideSet()

    # -- persistence --------------------------------------------------------

    def save_meta(self) -> None:
        self.meta.touch()
        try:
            write_json_atomic(self.project_file, self.meta.to_dict())
        except OSError as exc:
            raise ProjectError(f"Could not save {PROJECT_FILE}:\n{exc}") from exc

    def set_slides(self, slides: SlideSet) -> None:
        self.slides = slides

    def save_slides(self) -> None:
        """Persist detected slides so reopening never re-detects them (§9)."""
        try:
            write_json_atomic(self.slides_file, self.slides.to_dict())
        except OSError as exc:
            raise ProjectError(f"Could not save {SLIDES_FILE}:\n{exc}") from exc
        self.save_meta()

    def save(self, *, force_transcript: bool = False) -> None:
        """Persist metadata and, when it has changed, the transcript."""
        try:
            self.transcript.save(force=force_transcript)
        except OSError as exc:
            raise ProjectError(f"Could not save {TRANSCRIPT_FILE}:\n{exc}") from exc
        self.save_meta()

    # -- transcript-to-slide alignment (§10) -------------------------------

    def align(self) -> Alignment:
        """Link every transcript segment to its slide, and persist the links.

        Called whenever the transcript or the slides change - after
        transcription, after detection, after every manual slide correction,
        and on opening - so the links are always derived from the current
        times rather than trusted from an earlier run.
        """
        segments = self.transcript.segments
        appearances = self.slides.appearances
        alignment = align(segments, appearances)

        slide_ids = [
            appearances[index].slide_id if index is not None else None
            for index in alignment.by_segment
        ]
        changed = self.transcript.set_slide_ids(slide_ids)
        if changed:
            try:
                self.transcript.save()
            except OSError as exc:
                raise ProjectError(f"Could not save {TRANSCRIPT_FILE}:\n{exc}") from exc

        if segments and appearances:
            status, message = (
                StageStatus.DONE,
                f"{len(segments)} segments linked to {len(appearances)} slides.",
            )
        else:
            status, message = StageStatus.NOT_STARTED, None
        stage = self.meta.stage("alignment")
        if changed or stage.status is not status or stage.message != message:
            self.set_stage("alignment", status, message)
        return alignment

    # -- stage bookkeeping (§26, §27) ---------------------------------------

    def set_stage(self, key: str, status: StageStatus, message: str | None = None) -> None:
        self.meta.stage(key).set(status, message)
        self.save_meta()

    def stage_status(self, key: str) -> StageStatus:
        return self.meta.stage(key).status

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _unique_sibling(root: Path) -> Path:
        for suffix in range(2, 1000):
            candidate = root.with_name(f"{root.name}_{suffix}")
            if not candidate.exists():
                return candidate
        raise ProjectError(f"Too many projects named like '{root.name}'.")
