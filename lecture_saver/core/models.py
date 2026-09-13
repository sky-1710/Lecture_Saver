"""Core data models.

These are plain dataclasses with explicit (de)serialisation. Nothing here
imports Qt, FFmpeg or Whisper, which is what makes the rules in §33 testable
without a GUI or a lecture video.

The one invariant that matters most is on :class:`TranscriptSegment`:
``original_text`` is written once, at transcription time, and never again. User
edits go to ``edited_text``, and ``effective_text`` resolves the two. Because
that resolution is a property on the model rather than a rule each view
re-implements, no view or exporter can disagree about what the transcript says
(§14).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "TranscriptSegment",
    "Transcript",
    "VideoInfo",
    "StageStatus",
    "StageState",
    "TranscriptionSettings",
    "ProjectMeta",
    "make_segment_id",
    "utc_now_iso",
]

#: Bumped whenever the on-disk layout changes in a way loaders must know about.
SCHEMA_VERSION = 1


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_segment_id(index: int) -> str:
    """Stable, sortable, human-readable segment id: ``segment_001``."""
    return f"segment_{index:03d}"


# --------------------------------------------------------------------------
# Transcript
# --------------------------------------------------------------------------


@dataclass
class TranscriptSegment:
    """One timestamped chunk of speech."""

    id: str
    start: float
    end: float
    original_text: str
    #: ``None`` means "never edited". An empty string is a real edit (the user
    #: deliberately blanked the line) and must not be confused with ``None``.
    edited_text: str | None = None
    #: The logical slide on screen while this was said. Derived from the times
    #: by :mod:`lecture_saver.core.alignment` and rewritten whenever the
    #: transcript or the slides change, so it is never edited directly.
    slide_id: int | None = None

    @property
    def effective_text(self) -> str:
        """The authoritative text for display and every export (§14)."""
        return self.original_text if self.edited_text is None else self.edited_text

    @property
    def is_edited(self) -> bool:
        return self.edited_text is not None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def overlap_with(self, start: float, end: float) -> float:
        """Seconds of overlap with ``[start, end)``."""
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "original_text": self.original_text,
            "edited_text": self.edited_text,
            "slide_id": self.slide_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptSegment:
        return cls(
            id=str(data["id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            original_text=str(data.get("original_text", "")),
            edited_text=data.get("edited_text"),
            slide_id=data.get("slide_id"),
        )


@dataclass
class Transcript:
    """The whole transcript. Segments are kept sorted by start time."""

    segments: list[TranscriptSegment] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    #: Language Whisper reported, e.g. ``"ko"``. ``None`` before transcription.
    language: str | None = None

    def __len__(self) -> int:
        return len(self.segments)

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def sort(self) -> None:
        self.segments.sort(key=lambda s: (s.start, s.end))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "language": self.language,
            "segments": [s.to_dict() for s in self.segments],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            language=data.get("language"),
            segments=[TranscriptSegment.from_dict(d) for d in data.get("segments", [])],
        )


# --------------------------------------------------------------------------
# Video
# --------------------------------------------------------------------------


@dataclass
class VideoInfo:
    """What FFmpeg told us about the source video."""

    path: str
    duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    size_bytes: int = 0
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    has_audio: bool = False
    copied_into_project: bool = False

    @property
    def as_path(self) -> Path:
        return Path(self.path)

    @property
    def exists(self) -> bool:
        return self.as_path.is_file()

    @property
    def display_name(self) -> str:
        return self.as_path.name

    def to_dict(self) -> dict[str, Any]:
        return {
            # Stored POSIX-style so a project stays loadable regardless of which
            # shell or drive letter casing produced it.
            "path": self.as_path.as_posix(),
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "size_bytes": self.size_bytes,
            "container": self.container,
            "video_codec": self.video_codec,
            "audio_codec": self.audio_codec,
            "has_audio": self.has_audio,
            "copied_into_project": self.copied_into_project,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VideoInfo:
        return cls(
            path=str(Path(data["path"])),
            duration=float(data.get("duration", 0.0)),
            width=int(data.get("width", 0)),
            height=int(data.get("height", 0)),
            fps=float(data.get("fps", 0.0)),
            size_bytes=int(data.get("size_bytes", 0)),
            container=str(data.get("container", "")),
            video_codec=str(data.get("video_codec", "")),
            audio_codec=str(data.get("audio_codec", "")),
            has_audio=bool(data.get("has_audio", False)),
            copied_into_project=bool(data.get("copied_into_project", False)),
        )


# --------------------------------------------------------------------------
# Processing stages (§27)
# --------------------------------------------------------------------------


class StageStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class StageState:
    status: StageStatus = StageStatus.NOT_STARTED
    updated_at: str | None = None
    #: User-facing note - an error summary, or why a stage was skipped. Detailed
    #: technical output belongs in the log file, not here (§27).
    message: str | None = None

    def set(self, status: StageStatus, message: str | None = None) -> None:
        self.status = status
        self.message = message
        self.updated_at = utc_now_iso()

    @property
    def is_done(self) -> bool:
        return self.status is StageStatus.DONE

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "updated_at": self.updated_at,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StageState:
        try:
            status = StageStatus(data.get("status", "not_started"))
        except ValueError:
            status = StageStatus.NOT_STARTED
        return cls(
            status=status,
            updated_at=data.get("updated_at"),
            message=data.get("message"),
        )


#: Stage keys in pipeline order. ``alignment`` is DONE whenever a transcript and
#: slides both exist, and is recomputed each time either changes.
STAGE_KEYS = ("audio", "transcription", "slides", "alignment")


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass
class TranscriptionSettings:
    model_size: str = "small"
    device: str = "auto"
    compute_type: str = "auto"
    #: ``"auto"`` lets Whisper detect; ``"ko"``/``"en"`` force a language, which
    #: is usually better for mixed Korean/English lectures.
    language: str = "auto"
    vad_filter: bool = True
    beam_size: int = 5
    #: What actually ran, filled in after transcription.
    detected_language: str | None = None
    resolved_device: str | None = None
    resolved_compute_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_size": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "language": self.language,
            "vad_filter": self.vad_filter,
            "beam_size": self.beam_size,
            "detected_language": self.detected_language,
            "resolved_device": self.resolved_device,
            "resolved_compute_type": self.resolved_compute_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TranscriptionSettings:
        return cls(
            model_size=str(data.get("model_size", "small")),
            device=str(data.get("device", "auto")),
            compute_type=str(data.get("compute_type", "auto")),
            language=str(data.get("language", "auto")),
            vad_filter=bool(data.get("vad_filter", True)),
            beam_size=int(data.get("beam_size", 5)),
            detected_language=data.get("detected_language"),
            resolved_device=data.get("resolved_device"),
            resolved_compute_type=data.get("resolved_compute_type"),
        )


# --------------------------------------------------------------------------
# Project metadata
# --------------------------------------------------------------------------


@dataclass
class ProjectMeta:
    """Everything in ``project.json`` except the transcript itself."""

    name: str
    video: VideoInfo
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    schema_version: int = SCHEMA_VERSION
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    stages: dict[str, StageState] = field(
        default_factory=lambda: {key: StageState() for key in STAGE_KEYS}
    )
    transcription: TranscriptionSettings = field(default_factory=TranscriptionSettings)
    #: Slide detection thresholds last used, so a re-run starts from them (§10).
    #: Typed as Any to avoid a circular import; it is a SlideDetectionOptions.
    slide_detection: Any = None
    #: Phase 6 fills this in; kept so export choices survive a restart (§5).
    export_settings: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.slide_detection is None:
            from .slides import SlideDetectionOptions

            self.slide_detection = SlideDetectionOptions()

    def stage(self, key: str) -> StageState:
        return self.stages.setdefault(key, StageState())

    def touch(self) -> None:
        self.updated_at = utc_now_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "video": self.video.to_dict(),
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
            "transcription": self.transcription.to_dict(),
            "slide_detection": self.slide_detection.to_dict(),
            "export_settings": self.export_settings,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectMeta:
        from .slides import SlideDetectionOptions

        raw_stages = data.get("stages", {})
        stages = {key: StageState.from_dict(raw_stages.get(key, {})) for key in STAGE_KEYS}
        # Preserve stage keys written by a future version rather than dropping them.
        for key, value in raw_stages.items():
            if key not in stages:
                stages[key] = StageState.from_dict(value)

        return cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            project_id=str(data.get("project_id", uuid.uuid4().hex)),
            name=str(data.get("name", "Untitled lecture")),
            created_at=str(data.get("created_at", utc_now_iso())),
            updated_at=str(data.get("updated_at", utc_now_iso())),
            video=VideoInfo.from_dict(data["video"]),
            stages=stages,
            transcription=TranscriptionSettings.from_dict(data.get("transcription", {})),
            slide_detection=SlideDetectionOptions.from_dict(data.get("slide_detection")),
            export_settings=dict(data.get("export_settings", {})),
        )
