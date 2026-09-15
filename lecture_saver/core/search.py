"""Full-text search across the lecture (§18).

Built for studying rather than editing, so it is forgiving:

* every word of the query must appear, in any order;
* case is ignored;
* spacing is ignored, because Whisper's Korean word spacing is inconsistent -
  "러닝 레이트" finds "러닝레이트" and the other way round;
* the corrected text is searched, and the original transcription too, so a
  word the user has since corrected can still be found by what was heard;
* an imported translation is searched as well, reported against its slide.

Results come back in lecture order with the slide number, the time and a short
excerpt marking where each word matched. Pure: no Qt, no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .alignment import align
from .models import TranscriptSegment
from .slides import SlideAppearance
from .translations import TranslationSet

__all__ = ["SearchHit", "search_lecture"]

#: Characters of context kept on each side of the first match in an excerpt.
EXCERPT_CONTEXT = 36
#: A search never returns more than this many hits.
MAX_HITS = 500


@dataclass(frozen=True)
class SearchHit:
    #: ``"transcript"`` or ``"translation"``.
    kind: str
    start: float
    #: Displayed slide number, or ``None`` when the lecture has no slides.
    slide_number: int | None
    #: Index of the appearance, for navigating to it.
    appearance_index: int | None
    #: The segment the match is in; ``None`` for a translation hit.
    segment_id: str | None
    #: The excerpt shown, and ``(start, end)`` spans in it to emphasise.
    excerpt: str
    spans: tuple[tuple[int, int], ...]
    #: True when only the original transcription matched, not the corrected text.
    in_original_only: bool = False


def _compact(text: str) -> tuple[str, list[int]]:
    """Text without whitespace, lower-cased, plus each character's original index."""
    kept: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(text):
        if not char.isspace():
            kept.append(char.casefold())
            positions.append(index)
    return "".join(kept), positions


def _terms(query: str) -> list[str]:
    return [t for t in (_compact(word)[0] for word in query.split()) if t]


def _find_spans(text: str, terms: list[str]) -> list[tuple[int, int]] | None:
    """Original-text spans of every term, or ``None`` if any term is missing.

    Terms are matched both as typed and with their words run together, so a
    query "러닝 레이트" matches "러닝레이트" as well as "러닝 레이트".
    """
    compact, positions = _compact(text)
    spans: list[tuple[int, int]] = []
    for term in terms:
        found = [m.start() for m in re.finditer(re.escape(term), compact)]
        if not found:
            return None
        for at in found:
            spans.append((positions[at], positions[at + len(term) - 1] + 1))
    return sorted(spans)


def _matches(text: str, query: str) -> list[tuple[int, int]] | None:
    terms = _terms(query)
    if not terms:
        return None
    spans = _find_spans(text, terms)
    if spans is None and len(terms) > 1:
        # "러닝 레이트" typed with a space should find "러닝레이트" too.
        spans = _find_spans(text, ["".join(terms)])
    return spans


def _excerpt(text: str, spans: list[tuple[int, int]]) -> tuple[str, tuple[tuple[int, int], ...]]:
    first = spans[0][0]
    begin = max(0, first - EXCERPT_CONTEXT)
    end = min(len(text), spans[0][1] + EXCERPT_CONTEXT * 2)
    prefix = "…" if begin > 0 else ""
    suffix = "…" if end < len(text) else ""
    excerpt = prefix + text[begin:end].replace("\n", " ") + suffix
    shift = len(prefix) - begin
    shown = tuple(
        (s + shift, e + shift) for s, e in spans if s >= begin and e <= end
    )
    return excerpt, shown


def search_lecture(
    query: str,
    segments: Sequence[TranscriptSegment],
    appearances: Sequence[SlideAppearance],
    translations: TranslationSet | None = None,
) -> list[SearchHit]:
    """Every place ``query`` occurs, in lecture order."""
    if not _terms(query):
        return []

    alignment = align(segments, appearances)
    hits: list[SearchHit] = []

    for index, segment in enumerate(segments):
        text = segment.effective_text
        spans = _matches(text, query)
        in_original_only = False
        if spans is None and segment.is_edited and segment.original_text:
            spans = _matches(segment.original_text, query)
            if spans is not None:
                text, in_original_only = segment.original_text, True
        if spans is None:
            continue
        appearance_index = alignment.appearance_for(index)
        excerpt, shown = _excerpt(text, spans)
        hits.append(
            SearchHit(
                kind="transcript",
                start=segment.start,
                slide_number=(
                    appearances[appearance_index].appearance_id
                    if appearance_index is not None else None
                ),
                appearance_index=appearance_index,
                segment_id=segment.id,
                excerpt=excerpt,
                spans=shown,
                in_original_only=in_original_only,
            )
        )

    if translations is not None and not translations.is_empty:
        for appearance_index, appearance in enumerate(appearances):
            translated = translations.for_appearance(appearance)
            if not translated:
                continue
            spans = _matches(translated, query)
            if spans is None:
                continue
            excerpt, shown = _excerpt(translated, spans)
            hits.append(
                SearchHit(
                    kind="translation",
                    start=appearance.start,
                    slide_number=appearance.appearance_id,
                    appearance_index=appearance_index,
                    segment_id=None,
                    excerpt=excerpt,
                    spans=shown,
                )
            )

    hits.sort(key=lambda hit: (hit.start, hit.kind != "transcript"))
    return hits[:MAX_HITS]
