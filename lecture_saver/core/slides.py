"""Slide data models and detection options (§7, §8, §9).

Two concepts are kept separate on purpose:

* :class:`Slide` - a *logical* slide: one image, one fingerprint.
* :class:`SlideAppearance` - one time interval during which that slide was on
  screen.

Phase 3 creates exactly one appearance per slide, so the two are 1:1 today. The
split is what lets Phase 7 recognise ``5 -> 6 -> 7 -> 6`` as the second
*appearance* of slide 6 rather than a fourth slide, without changing any
consumer of this data. Each slide already stores a perceptual ``fingerprint``
for that future matching; nothing compares fingerprints yet (§12).

Like the rest of ``core``, this module is pure data - no Qt, no OpenCV, no
FFmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import SCHEMA_VERSION, utc_now_iso
from .slide_options import SlideDetectionOptions

__all__ = [
    "ANNOTATION_STATES",
    "Slide",
    "SlideAppearance",
    "SlideSet",
    "SlideDetectionOptions",
    "SlideEditError",
    "delete_appearance",
    "merge_with_previous",
    "split_at",
    "move_boundary",
    "separate_slide",
    "set_slide_image",
]


# --------------------------------------------------------------------------
# Slides
# --------------------------------------------------------------------------

#: How an appearance relates to the professor's handwriting on that slide, and
#: the short label shown for it. A heavily annotated slide is saved as a clean
#: version and an annotated one; an annotated version the professor then erased
#: is marked as such.
ANNOTATION_STATES: dict[str, str] = {
    "before_annotation": "Before notes",
    "annotated": "With notes",
    "before_erase": "Before erasing",
}


@dataclass
class Slide:
    """A logical slide: the image plus what is needed to recognise it again."""

    slide_id: int
    #: Project-relative path, e.g. ``slides/001.jpg``.
    image: str
    #: Perceptual hash (64-bit dHash, hex) of the representative frame. Stored
    #: now so Phase 7 can match repeated slides without re-decoding the video.
    #: Nothing compares these yet (§12).
    fingerprint: str = ""

    def image_path(self, project_root: Path) -> Path:
        return Path(project_root) / self.image

    def to_dict(self) -> dict[str, Any]:
        return {"slide_id": self.slide_id, "image": self.image, "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Slide:
        return cls(
            slide_id=int(data["slide_id"]),
            image=str(data.get("image", "")),
            fingerprint=str(data.get("fingerprint", "")),
        )


@dataclass
class SlideAppearance:
    """One interval during which a slide was displayed.

    ``slide_id`` is the *logical* slide; several appearances may share one once
    Phase 7 recognises a returned-to slide. The provenance fields record where
    the interval came from, so a later re-run or export can tell a detected
    boundary from one a person put there.
    """

    appearance_id: int
    slide_id: int
    start: float
    end: float
    #: True when a person created this boundary rather than the detector.
    manually_added: bool = False
    #: True when a person moved or resized an otherwise detected interval.
    manually_modified: bool = False
    #: How strongly the detector believed in this boundary, 0-1. ``None`` for
    #: the opening appearance and for hand-made ones.
    detection_confidence: float | None = None
    #: Which path accepted it: "strong global transition", "content replaced",
    #: or "manual".
    detection_reason: str | None = None
    #: One of :data:`ANNOTATION_STATES`, or ``None`` for an ordinary slide.
    annotation_state: str | None = None

    @property
    def annotation_label(self) -> str:
        """Short user-facing label for :attr:`annotation_state`, or ``""``."""
        return ANNOTATION_STATES.get(self.annotation_state or "", "")

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def contains(self, seconds: float) -> bool:
        """Half-open ``[start, end)`` so adjacent appearances never both match."""
        return self.start <= seconds < self.end

    def overlap_with(self, start: float, end: float) -> float:
        """Seconds of overlap with ``[start, end)``. Used by transcript alignment."""
        return max(0.0, min(self.end, end) - max(self.start, start))

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id,
            "slide_id": self.slide_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "manually_added": self.manually_added,
            "manually_modified": self.manually_modified,
            "detection_confidence": (
                round(self.detection_confidence, 4)
                if self.detection_confidence is not None
                else None
            ),
            "detection_reason": self.detection_reason,
            "annotation_state": self.annotation_state,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlideAppearance:
        confidence = data.get("detection_confidence")
        return cls(
            appearance_id=int(data["appearance_id"]),
            slide_id=int(data["slide_id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            manually_added=bool(data.get("manually_added", False)),
            manually_modified=bool(data.get("manually_modified", False)),
            detection_confidence=(
                float(confidence) if confidence is not None else None
            ),
            detection_reason=data.get("detection_reason"),
            annotation_state=(
                state if (state := data.get("annotation_state")) in ANNOTATION_STATES
                else None
            ),
        )


@dataclass
class SlideSet:
    """Everything ``slides.json`` holds."""

    slides: list[Slide] = field(default_factory=list)
    appearances: list[SlideAppearance] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    detected_at: str | None = None
    video_duration: float = 0.0
    #: The options this result was produced with, so a re-run can start from
    #: what was used last time.
    options: dict[str, Any] = field(default_factory=dict)
    #: True once the user has corrected the detection by hand. Re-running
    #: detection discards those corrections, so the UI warns first.
    manually_edited: bool = False
    #: How many detected appearances a person removed. Kept because the
    #: appearance itself is gone, and it is useful to know the detector was
    #: over-producing on this lecture.
    manually_deleted: int = 0

    def __len__(self) -> int:
        return len(self.appearances)

    @property
    def is_empty(self) -> bool:
        return not self.appearances

    def slide(self, slide_id: int) -> Slide | None:
        for slide in self.slides:
            if slide.slide_id == slide_id:
                return slide
        return None

    def slide_for(self, appearance: SlideAppearance) -> Slide | None:
        return self.slide(appearance.slide_id)

    def repeat_of(self, appearance: SlideAppearance) -> SlideAppearance | None:
        """The earlier appearance of the same slide, if this is a return to it."""
        for other in self.appearances:
            if other.start >= appearance.start:
                break
            if other.slide_id == appearance.slide_id and other is not appearance:
                return other
        return None

    def label_for(self, appearance: SlideAppearance) -> str:
        """Short description next to the number: notes state, and a return."""
        parts = []
        if appearance.annotation_label:
            parts.append(appearance.annotation_label)
        earlier = self.repeat_of(appearance)
        if earlier is not None:
            parts.append(f"Same as Slide {earlier.appearance_id}")
        return " · ".join(parts)

    def appearance_at(self, seconds: float) -> SlideAppearance | None:
        """The appearance covering ``seconds``, if any."""
        for appearance in self.appearances:
            if appearance.contains(seconds):
                return appearance
        # Past the end of the last appearance, hold the last one.
        if self.appearances and seconds >= self.appearances[-1].end:
            return self.appearances[-1]
        return None

    def sort(self) -> None:
        self.appearances.sort(key=lambda a: a.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "detected_at": self.detected_at,
            "video_duration": round(self.video_duration, 3),
            "options": self.options,
            "manually_edited": self.manually_edited,
            "manually_deleted": self.manually_deleted,
            "slides": [s.to_dict() for s in self.slides],
            "appearances": [a.to_dict() for a in self.appearances],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlideSet:
        result = cls(
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
            detected_at=data.get("detected_at"),
            video_duration=float(data.get("video_duration", 0.0)),
            options=dict(data.get("options", {})),
            manually_edited=bool(data.get("manually_edited", False)),
            manually_deleted=int(data.get("manually_deleted", 0)),
            slides=[Slide.from_dict(d) for d in data.get("slides", [])],
            appearances=[SlideAppearance.from_dict(d) for d in data.get("appearances", [])],
        )
        result.sort()
        return result

    @classmethod
    def build(
        cls,
        slides: list[Slide],
        appearances: list[SlideAppearance],
        *,
        video_duration: float,
        options: SlideDetectionOptions,
    ) -> SlideSet:
        return cls(
            slides=slides,
            appearances=appearances,
            detected_at=utc_now_iso(),
            video_duration=video_duration,
            options=options.to_dict(),
        )


# --------------------------------------------------------------------------
# Manual correction (§ real-world robustness)
# --------------------------------------------------------------------------


class SlideEditError(ValueError):
    """A manual slide edit could not be applied. The message is user-facing."""


def _renumber(appearances: list[SlideAppearance]) -> None:
    """Appearance ids are display order, so they are resequenced after an edit.

    ``slide_id`` deliberately is *not* touched: it identifies the image on disk
    and, from Phase 7 on, the logical slide that several appearances share.
    """
    appearances.sort(key=lambda a: a.start)
    for position, appearance in enumerate(appearances, start=1):
        appearance.appearance_id = position


def delete_appearance(slide_set: SlideSet, appearance_id: int) -> SlideAppearance:
    """Remove a falsely detected slide, giving its time to its neighbour.

    The interval is absorbed by the preceding appearance so the timeline stays
    contiguous - which matters because alignment assigns speech by overlap
    and must not find a hole.
    """
    index = _index_of(slide_set, appearance_id)
    if len(slide_set.appearances) <= 1:
        raise SlideEditError("The only slide cannot be deleted.")

    removed = slide_set.appearances.pop(index)
    if index > 0:
        slide_set.appearances[index - 1].end = removed.end
    else:
        slide_set.appearances[0].start = removed.start

    _renumber(slide_set.appearances)
    slide_set.manually_edited = True
    slide_set.manually_deleted += 1
    if index > 0:
        slide_set.appearances[index - 1].manually_modified = True
    return removed


def merge_with_previous(slide_set: SlideSet, appearance_id: int) -> SlideAppearance:
    """Fold a duplicate back into the appearance before it.

    Identical in effect to deleting it, but named for the intent so the UI can
    offer the two separately.
    """
    index = _index_of(slide_set, appearance_id)
    if index == 0:
        raise SlideEditError("The first slide has nothing before it to merge into.")
    return delete_appearance(slide_set, appearance_id)


def split_at(slide_set: SlideSet, seconds: float, image: str = "") -> SlideAppearance:
    """Create a slide boundary at ``seconds``.

    This is how a missed slide change is corrected. The appearance covering
    that moment is cut in two; the new half gets a fresh ``slide_id`` so its
    image is stored separately.
    """
    if seconds < 0:
        raise SlideEditError("A slide boundary cannot be before the start of the video.")

    index = next(
        (i for i, a in enumerate(slide_set.appearances) if a.start < seconds < a.end),
        None,
    )
    if index is None:
        raise SlideEditError(
            "There is no slide to split at this point. Move the video to a "
            "moment inside an existing slide and try again."
        )

    existing = slide_set.appearances[index]
    new_slide_id = max((s.slide_id for s in slide_set.slides), default=0) + 1
    new_slide_id = max(new_slide_id, max(a.slide_id for a in slide_set.appearances) + 1)

    created = SlideAppearance(
        appearance_id=existing.appearance_id + 1,
        slide_id=new_slide_id,
        start=float(seconds),
        end=existing.end,
        manually_added=True,
        detection_reason="manual",
    )
    existing.end = float(seconds)
    existing.manually_modified = True
    slide_set.appearances.insert(index + 1, created)
    slide_set.slides.append(Slide(slide_id=new_slide_id, image=image))

    _renumber(slide_set.appearances)
    slide_set.manually_edited = True
    return created


def move_boundary(
    slide_set: SlideSet, appearance_id: int, seconds: float
) -> SlideAppearance:
    """Move the boundary at the start of ``appearance_id`` to ``seconds``.

    The neighbouring appearance is resized to match, so the timeline stays
    contiguous. The new time must leave both sides non-empty - a boundary
    dragged past its neighbour would silently delete a slide, which is what
    the delete and merge operations are for.
    """
    index = _index_of(slide_set, appearance_id)
    if index == 0:
        raise SlideEditError("The first slide always starts at the beginning.")

    previous = slide_set.appearances[index - 1]
    current = slide_set.appearances[index]
    seconds = float(seconds)

    if not previous.start < seconds < current.end:
        raise SlideEditError(
            "That would move the boundary past a neighbouring slide. Delete or "
            "merge the slide instead."
        )

    previous.end = seconds
    current.start = seconds
    previous.manually_modified = True
    current.manually_modified = True
    slide_set.manually_edited = True
    return current


def separate_slide(slide_set: SlideSet, appearance_id: int, image: str = "") -> SlideAppearance:
    """Undo a wrong "same slide" match: give the appearance its own slide.

    The caller captures a picture for it, as for a hand-made boundary.
    """
    appearance = slide_set.appearances[_index_of(slide_set, appearance_id)]
    if slide_set.repeat_of(appearance) is None:
        raise SlideEditError(
            f"Slide {appearance_id} is not marked as the same slide as an earlier one."
        )
    new_slide_id = max(
        max((s.slide_id for s in slide_set.slides), default=0),
        max(a.slide_id for a in slide_set.appearances),
    ) + 1
    appearance.slide_id = new_slide_id
    appearance.manually_modified = True
    slide_set.slides.append(Slide(slide_id=new_slide_id, image=image))
    slide_set.manually_edited = True
    return appearance


def set_slide_image(slide_set: SlideSet, slide_id: int, image: str, fingerprint: str = "") -> None:
    """Point a slide at a newly captured image."""
    slide = slide_set.slide(slide_id)
    if slide is None:
        slide = Slide(slide_id=slide_id, image=image, fingerprint=fingerprint)
        slide_set.slides.append(slide)
    else:
        slide.image = image
        slide.fingerprint = fingerprint
    slide_set.manually_edited = True


def _index_of(slide_set: SlideSet, appearance_id: int) -> int:
    for index, appearance in enumerate(slide_set.appearances):
        if appearance.appearance_id == appearance_id:
            return index
    raise SlideEditError(f"There is no slide numbered {appearance_id}.")
