"""Slide-by-slide translations imported from an AI assistant.

The application writes an "AI translation request" (see
:mod:`lecture_saver.export.ai_request`); the user gives it to an assistant and
brings the answer back. The answer is Markdown with one ``## Slide N`` heading
per slide. This module turns that text into translations attached to the
slides, so exports can put each slide's translation under its transcript.

Nothing here talks to any service - it only reads text the user supplies.

A translation is stored against the slide it was made for: its number *and*
its time interval at import time. Slide numbers are display order and change
when slides are corrected or detected again, so a translation is only shown for
a slide that still covers the same stretch of the lecture. It can never quietly
end up under a different slide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import SCHEMA_VERSION, utc_now_iso
from .slides import SlideAppearance

__all__ = [
    "ParsedTranslation",
    "SlideTranslation",
    "TranslationSet",
    "match_translations",
    "parse_translation_text",
]

#: A heading for one slide: ``## Slide 3``, ``### Slide 3 · With notes``,
#: ``**Slide 3**``, or a line holding nothing but ``Slide 3`` / ``슬라이드 3``.
#: A sentence that merely begins with "Slide 3에서..." is not a heading.
_HEADING = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s*(?:slide|슬라이드)\s*(?P<hash>\d{1,4})\b.*"
    r"|\*\*\s*(?:slide|슬라이드)\s*(?P<bold>\d{1,4})\b[^*]*\*\*\s*"
    r"|(?:slide|슬라이드)\s*(?P<bare>\d{1,4})\s*(?:[·:\-–—].*)?"
    r")$",
    re.IGNORECASE,
)
#: Lines an assistant adds that are not translation.
_NOISE = re.compile(
    r"^\s*(?:\(?\s*slide\s*\d+\s*전사의\s*한국어\s*직역\s*\)?|"
    r".*[\"'“]?계속[\"'”]?\s*이라고\s*(?:입력|하면).*)\s*$",
    re.IGNORECASE,
)
#: A ``---`` rule the answer puts between paragraphs. It separates them just
#: as a blank line does, even when no blank line surrounds it.
_RULE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
#: Two appearances cover "the same stretch" when they overlap this much of the
#: shorter one.
SAME_INTERVAL_OVERLAP = 0.5


@dataclass(frozen=True)
class ParsedTranslation:
    """What was found in the pasted or loaded text."""

    #: Slide number -> translated text, in the order the slides appeared.
    slides: dict[int, str]
    #: Slide headings that had no text under them.
    empty: tuple[int, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.slides


def parse_translation_text(text: str) -> ParsedTranslation:
    """Find ``Slide N`` sections and the text under each.

    Fenced code blocks are skipped, so the example in the request itself is
    never mistaken for an answer. When a slide appears more than once - an
    answer pasted together with its continuation - the later text wins.
    """
    slides: dict[int, str] = {}
    current: int | None = None
    lines: list[str] = []
    fenced = False

    def close() -> None:
        if current is None:
            return
        body = _clean(lines)
        if body:
            slides[current] = body
        elif current not in slides:
            slides.setdefault(current, "")

    for raw in text.splitlines():
        if raw.strip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        heading = _HEADING.match(raw)
        if heading:
            close()
            current = int(heading.group("hash") or heading.group("bold") or heading.group("bare"))
            lines = []
            continue
        if current is not None:
            lines.append(raw)
    close()

    empty = tuple(number for number, body in slides.items() if not body)
    return ParsedTranslation(
        slides={n: body for n, body in slides.items() if body}, empty=empty
    )


def _clean(lines: list[str]) -> str:
    kept = ["" if _RULE.match(line) else line.rstrip() for line in lines if not _NOISE.match(line)]
    text = "\n".join(kept).strip()
    # Collapse runs of blank lines to one paragraph break.
    return re.sub(r"\n\s*\n+", "\n\n", text)


@dataclass(frozen=True)
class SlideTranslation:
    appearance_id: int
    slide_id: int
    start: float
    end: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "appearance_id": self.appearance_id,
            "slide_id": self.slide_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SlideTranslation:
        return cls(
            appearance_id=int(data["appearance_id"]),
            slide_id=int(data["slide_id"]),
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data.get("text", "")),
        )

    def covers(self, appearance: SlideAppearance) -> bool:
        """True when ``appearance`` is still the slide this was made for."""
        overlap = appearance.overlap_with(self.start, self.end)
        shorter = min(appearance.duration, self.end - self.start)
        return shorter > 0 and overlap >= shorter * SAME_INTERVAL_OVERLAP


@dataclass
class TranslationSet:
    """Everything ``translations.json`` holds."""

    entries: list[SlideTranslation] = field(default_factory=list)
    language: str = "ko"
    source: str = ""
    imported_at: str | None = None
    schema_version: int = SCHEMA_VERSION

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def for_appearance(self, appearance: SlideAppearance) -> str | None:
        """The translation of ``appearance``, if it still covers the same time."""
        best: SlideTranslation | None = None
        for entry in self.entries:
            if entry.covers(appearance):
                overlap = appearance.overlap_with(entry.start, entry.end)
                if best is None or overlap > appearance.overlap_with(best.start, best.end):
                    best = entry
        return best.text if best is not None else None

    def merge(self, new: list[SlideTranslation], *, source: str = "") -> None:
        """Add translations, replacing any for the same slide interval."""
        kept = [
            old for old in self.entries
            if not any(n.start == old.start and n.end == old.end for n in new)
        ]
        self.entries = sorted([*kept, *new], key=lambda e: e.start)
        self.imported_at = utc_now_iso()
        if source:
            self.source = source

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "language": self.language,
            "source": self.source,
            "imported_at": self.imported_at,
            "slides": [e.to_dict() for e in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TranslationSet:
        if not data:
            return cls()
        return cls(
            entries=[SlideTranslation.from_dict(d) for d in data.get("slides", [])],
            language=str(data.get("language", "ko")),
            source=str(data.get("source", "")),
            imported_at=data.get("imported_at"),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


@dataclass(frozen=True)
class MatchResult:
    matched: list[SlideTranslation]
    #: Slide numbers in the text that no current slide has.
    unknown: tuple[int, ...]
    #: Current slide numbers the text did not translate.
    missing: tuple[int, ...]


def match_translations(
    parsed: ParsedTranslation, appearances: list[SlideAppearance]
) -> MatchResult:
    """Attach parsed text to the current slides by their displayed number."""
    by_number = {a.appearance_id: a for a in appearances}
    matched = [
        SlideTranslation(
            appearance_id=number,
            slide_id=by_number[number].slide_id,
            start=by_number[number].start,
            end=by_number[number].end,
            text=text,
        )
        for number, text in parsed.slides.items()
        if number in by_number
    ]
    unknown = tuple(n for n in parsed.slides if n not in by_number)
    missing = tuple(a.appearance_id for a in appearances if a.appearance_id not in parsed.slides)
    return MatchResult(matched=matched, unknown=unknown, missing=missing)
