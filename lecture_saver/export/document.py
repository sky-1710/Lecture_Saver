"""The lecture as exports see it: sections of slide + speech.

Built once, on the UI thread, from the project's single transcript and slides.
It holds *copies*, so a background export is unaffected by edits made while it
runs, and every exporter formats the same grouping - the one the application
shows, from :func:`lecture_saver.core.alignment.align`.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.alignment import align
from ..core.models import TranscriptSegment
from ..core.project import Project

__all__ = ["LectureDocument", "Section", "build_document", "nfc", "paragraphs", "spoken"]

#: A pause at least this long in the speech starts a new paragraph, so a slide's
#: transcript reads as prose rather than one wall of text or one line per
#: Whisper segment.
PARAGRAPH_PAUSE_SECONDS = 2.5


@dataclass(frozen=True)
class Section:
    """One slide appearance and what was said while it was on screen.

    Without detected slides the whole lecture is a single section with no
    number and no image.
    """

    #: The number shown in the application ("Slide 8"), or ``None``.
    number: int | None
    slide_id: int | None
    #: "Before notes", "With notes", "Before erasing" or "".
    label: str
    annotation_state: str | None
    start: float
    end: float
    #: Absolute path of the slide picture, when there is one on disk.
    image: Path | None
    #: Project-relative image path as stored, e.g. ``slides/008.jpg``.
    image_ref: str
    segments: tuple[TranscriptSegment, ...]

    @property
    def spoken(self) -> tuple[TranscriptSegment, ...]:
        return spoken(self.segments)


@dataclass(frozen=True)
class LectureDocument:
    title: str
    video_name: str
    duration: float
    language: str | None
    project_root: Path
    has_slides: bool
    sections: tuple[Section, ...]
    #: Every segment in time order, including blanked ones.
    segments: tuple[TranscriptSegment, ...] = field(default=())

    @property
    def is_empty(self) -> bool:
        return not self.segments and not self.has_slides


def spoken(segments: tuple[TranscriptSegment, ...] | list[TranscriptSegment]) -> tuple[
    TranscriptSegment, ...
]:
    """Segments with something to say. A line the user blanked is not exported."""
    return tuple(s for s in segments if s.effective_text.strip())


def paragraphs(segments: tuple[TranscriptSegment, ...]) -> list[list[TranscriptSegment]]:
    """Group consecutive segments into paragraphs at pauses in the speech."""
    groups: list[list[TranscriptSegment]] = []
    for segment in spoken(segments):
        if groups and segment.start - groups[-1][-1].end < PARAGRAPH_PAUSE_SECONDS:
            groups[-1].append(segment)
        else:
            groups.append([segment])
    return groups


def nfc(text: str) -> str:
    """Compose Hangul into whole syllables.

    File names from macOS archives store Korean decomposed into jamo. Qt joins
    those back up on screen, but a PDF or a text file would show the loose
    letters, so everything an export writes is normalised first.
    """
    return unicodedata.normalize("NFC", text)


def _normalised(segment: TranscriptSegment) -> TranscriptSegment:
    return replace(
        segment,
        original_text=nfc(segment.original_text),
        edited_text=None if segment.edited_text is None else nfc(segment.edited_text),
    )


def build_document(project: Project) -> LectureDocument:
    segments = tuple(_normalised(s) for s in project.transcript.segments)
    appearances = project.slides.appearances
    duration = project.video.duration or (segments[-1].end if segments else 0.0)

    if not appearances:
        sections: tuple[Section, ...] = (
            Section(
                number=None, slide_id=None, label="", annotation_state=None,
                start=0.0, end=duration, image=None, image_ref="", segments=segments,
            ),
        )
    else:
        alignment = align(segments, appearances)
        built = []
        for index, appearance in enumerate(appearances):
            slide = project.slides.slide_for(appearance)
            image_ref = slide.image if slide else ""
            image_path = project.root / image_ref if image_ref else None
            built.append(
                Section(
                    number=appearance.appearance_id,
                    slide_id=appearance.slide_id,
                    label=appearance.annotation_label,
                    annotation_state=appearance.annotation_state,
                    start=appearance.start,
                    end=appearance.end,
                    image=image_path if image_path and image_path.is_file() else None,
                    image_ref=image_ref,
                    segments=tuple(segments[i] for i in alignment.segments_for(index)),
                )
            )
        sections = tuple(built)

    return LectureDocument(
        title=nfc(project.name),
        video_name=nfc(project.video.display_name),
        duration=duration,
        language=project.transcript.language,
        project_root=project.root,
        has_slides=bool(appearances),
        sections=sections,
        segments=segments,
    )
