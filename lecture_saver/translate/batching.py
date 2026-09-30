"""Cutting a lecture into batches small enough to translate in one run.

A two-hour lecture is far too much for one request: the answer alone would be
the whole transcript again plus its Korean, and a single failure anywhere
would lose all of it. So slides are sent a few at a time.

Batches break on slide boundaries and never inside one. A slide is the unit
the answer is addressed by (``## Slide 8``) and the unit a translation is
stored against, so half a slide's speech in one request and half in another
would give two partial answers for one slide, the second overwriting the
first.

What goes into a prompt is only the speech: the slide's number, its label and
its timestamped lines. Not the image, not its path, not the video's name. An engine
that sends text to a service is given the smallest thing that can be
translated.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.timecode import format_hms
from ..export.document import LectureDocument, Section
from ..export.text_formats import section_title

__all__ = [
    "Batch",
    "DEFAULT_MAX_CHARS",
    "DEFAULT_MAX_SLIDES",
    "batch_markdown",
    "plan_batches",
    "translatable_sections",
]

#: Roughly how much transcript to put in one request, in characters. English
#: prose runs about four characters to the token, so this is on the order of
#: 3k tokens in and, with the English repeated and the Korean added, perhaps
#: three times that back. Small enough to be well inside any answer limit and
#: to lose little when a batch fails; large enough that a slide's neighbours
#: give the assistant context.
DEFAULT_MAX_CHARS = 12000

#: A hard ceiling regardless of length, so a run of near-empty slides does not
#: produce one request covering forty headings.
DEFAULT_MAX_SLIDES = 12


@dataclass(frozen=True)
class Batch:
    """The slides of one request, in lecture order."""

    sections: tuple[Section, ...]

    @property
    def numbers(self) -> tuple[int, ...]:
        """The slide numbers this batch should come back with."""
        return tuple(s.number for s in self.sections if s.number is not None)

    @property
    def characters(self) -> int:
        return sum(_section_characters(s) for s in self.sections)


def _section_characters(section: Section) -> int:
    return sum(len(s.effective_text) for s in section.spoken)


def translatable_sections(
    document: LectureDocument, *, skip: set[int] | None = None
) -> list[Section]:
    """Sections worth sending: numbered, with speech, not already done.

    ``skip`` holds slide numbers that already have a translation. Passing the
    numbers already stored is what makes a second run resume rather than pay
    to redo the whole lecture.
    """
    done = skip or set()
    return [
        section
        for section in document.sections
        if section.number is not None
        and section.number not in done
        and _section_characters(section) > 0
    ]


def plan_batches(
    sections: list[Section] | tuple[Section, ...],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_slides: int = DEFAULT_MAX_SLIDES,
) -> list[Batch]:
    """Group ``sections`` into batches, in order, without splitting a slide.

    A single slide longer than ``max_chars`` becomes a batch on its own rather
    than being cut: it is better to send one oversized request than to get
    back two answers claiming the same slide number.
    """
    batches: list[Batch] = []
    current: list[Section] = []
    size = 0

    for section in sections:
        length = _section_characters(section)
        too_long = current and (size + length > max_chars or len(current) >= max_slides)
        if too_long:
            batches.append(Batch(tuple(current)))
            current, size = [], 0
        current.append(section)
        size += length

    if current:
        batches.append(Batch(tuple(current)))
    return batches


def batch_markdown(batch: Batch) -> str:
    """The batch's slides as the prompt presents them.

    Headings come from :func:`section_title`, the same function the exports
    use, so the answer's ``## Slide N`` lines match what the importer looks
    for. Each spoken segment is one ``[HH:MM:SS]`` line, so each Korean line
    that comes back can be put under the segment it translates.
    """
    lines: list[str] = []
    for section in batch.sections:
        lines += [f"## {section_title(section)}", ""]
        for segment in section.spoken:
            lines += [f"[{format_hms(segment.start)}] {segment.effective_text.strip()}", ""]
    return "\n".join(lines).rstrip() + "\n"
