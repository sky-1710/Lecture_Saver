"""The lecture as exports see it: sections of slide + speech.

Built once, on the UI thread, from the project's single transcript and slides.
It holds *copies*, so a background export is unaffected by edits made while it
runs, and every exporter formats the same grouping - the one the application
shows, from :func:`lecture_saver.core.alignment.align`.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.alignment import align
from ..core.models import TranscriptSegment
from ..core.project import Project
from ..core.translations import parse_timed_lines

__all__ = [
    "LectureDocument",
    "Screenshot",
    "Section",
    "TranslationBlock",
    "build_document",
    "nfc",
    "pair_timed_translation",
    "screenshots_by_section",
    "paragraphs",
    "spoken",
    "translation_blocks",
]

#: A pause at least this long in the speech starts a new paragraph, so a slide's
#: transcript reads as prose rather than one wall of text or one line per
#: Whisper segment.
PARAGRAPH_PAUSE_SECONDS = 2.5

#: Text with any Hangul in it is the translation; text without is the English
#: original the assistant copied above it.
_HANGUL = re.compile(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]")


@dataclass(frozen=True)
class TranslationBlock:
    """One paragraph of an imported translation."""

    text: str
    korean: bool


def translation_blocks(translation: str) -> list[TranslationBlock]:
    """Split a translation into paragraphs, English and Korean kept apart.

    Paragraphs are separated by blank lines. Answers made with an earlier
    request put the Korean on the very next line, so lines of a different
    language also start a new paragraph; lines of the same language are one
    paragraph the assistant happened to wrap.
    """
    blocks: list[TranslationBlock] = []
    for chunk in re.split(r"\n\s*\n", translation):
        run: list[str] = []
        korean = False
        for line in (line.strip() for line in chunk.splitlines()):
            if not line:
                continue
            is_korean = bool(_HANGUL.search(line))
            if run and is_korean != korean:
                blocks.append(TranslationBlock(" ".join(run), korean))
                run = []
            run.append(line)
            korean = is_korean
        if run:
            blocks.append(TranslationBlock(" ".join(run), korean))
    return blocks


#: How far a timestamp in the answer may be from a segment's start and still
#: be that segment's. The answer copies ``HH:MM:SS``, which drops the fraction.
_STAMP_TOLERANCE_SECONDS = 1


def pair_timed_translation(
    segments: tuple[TranscriptSegment, ...], translation: str
) -> dict[str, str]:
    """Segment id -> its Korean line, for a translation written per timestamp.

    Each answer line is matched to the first spoken segment not yet taken whose
    start, as ``HH:MM:SS`` shows it, equals the line's timestamp - so two segments
    starting within the same second are paired in order. A line whose exact
    second matches nothing goes to the nearest free segment within a second.
    Empty for a translation written as paragraphs.
    """
    lines = parse_timed_lines(translation)
    if not lines:
        return {}
    candidates = spoken(segments)
    taken: set[str] = set()
    pairs: dict[str, str] = {}
    for line in lines:
        exact = [s for s in candidates if s.id not in taken and _whole_second(s) == line.start]
        if exact:
            match = exact[0]
        else:
            near = [
                s for s in candidates
                if s.id not in taken
                and abs(_whole_second(s) - line.start) <= _STAMP_TOLERANCE_SECONDS
            ]
            if not near:
                continue
            match = min(near, key=lambda s: abs(s.start - line.start))
        taken.add(match.id)
        pairs[match.id] = line.text
    return pairs


def _whole_second(segment: TranscriptSegment) -> int:
    """The second :func:`format_hms` shows for the segment's start."""
    return int(round(max(segment.start, 0.0) * 1000)) // 1000


@dataclass(frozen=True)
class Screenshot:
    """A screenshot the user took of the video, as it is printed."""

    #: The moment of the video it shows, in seconds.
    time: float
    #: Where its marking began.
    start: float
    #: The picture to print - with the marks drawn in when there are any.
    image: Path
    marks: int


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
    #: Imported translation of this slide, or "".
    translation: str = ""
    #: The displayed number of the earlier appearance of this same slide, when
    #: the professor returned to it.
    repeat_of: int | None = None
    #: Screenshots taken while this slide was on screen, in order.
    screenshots: tuple[Screenshot, ...] = ()

    @property
    def spoken(self) -> tuple[TranscriptSegment, ...]:
        return spoken(self.segments)

    @property
    def translation_blocks(self) -> list[TranslationBlock]:
        return translation_blocks(self.translation)

    @property
    def timed_translation(self) -> dict[str, str]:
        """Segment id -> Korean, when the translation was made line by line.

        Such a translation is shown under each timestamped line of the
        transcript instead of as paragraphs after it.
        """
        return pair_timed_translation(self.segments, self.translation)

    @property
    def bilingual(self) -> bool:
        """The translation pairs each English paragraph with its Korean one.

        Such a translation already carries the transcript, so exports show the
        pairs in its place rather than the same English twice.
        """
        kinds = {block.korean for block in self.translation_blocks}
        return kinds == {True, False}


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

    @property
    def has_translation(self) -> bool:
        return any(section.translation for section in self.sections)

    @property
    def has_screenshots(self) -> bool:
        return any(section.screenshots for section in self.sections)


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


def screenshots_by_section(project: Project) -> list[list[Screenshot]]:
    """The lecture's screenshots, grouped by the slide on screen when each was taken.

    One group per slide appearance - or a single group without slides. A
    screenshot from before the first slide goes with it, one from after the
    last slide's end with the last. Screenshots whose picture is missing are
    left out; a missing marked copy falls back to the plain picture.
    """
    appearances = project.slides.appearances
    starts = [a.start for a in appearances]
    groups: list[list[Screenshot]] = [[] for _ in range(max(1, len(appearances)))]
    for capture in project.captures:
        picture = project.root / capture.printed_image
        if not picture.is_file():
            picture = capture.image_path(project.root)
        if not picture.is_file():
            continue
        index = max(0, bisect.bisect_right(starts, capture.time) - 1) if starts else 0
        groups[index].append(
            Screenshot(time=capture.time, start=capture.start, image=picture,
                       marks=len(capture.marks))
        )
    return groups


def build_document(project: Project) -> LectureDocument:
    segments = tuple(_normalised(s) for s in project.transcript.segments)
    appearances = project.slides.appearances
    duration = project.video.duration or (segments[-1].end if segments else 0.0)
    screenshots = screenshots_by_section(project)

    if not appearances:
        sections: tuple[Section, ...] = (
            Section(
                number=None, slide_id=None, label="", annotation_state=None,
                start=0.0, end=duration, image=None, image_ref="", segments=segments,
                screenshots=tuple(screenshots[0]),
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
                    label=project.slides.label_for(appearance),
                    annotation_state=appearance.annotation_state,
                    start=appearance.start,
                    end=appearance.end,
                    image=image_path if image_path and image_path.is_file() else None,
                    image_ref=image_ref,
                    segments=tuple(segments[i] for i in alignment.segments_for(index)),
                    translation=nfc(project.translations.for_appearance(appearance) or ""),
                    repeat_of=(
                        earlier.appearance_id
                        if (earlier := project.slides.repeat_of(appearance)) else None
                    ),
                    screenshots=tuple(screenshots[index]),
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
