"""Where a line of transcript can be cut so that sentences stay whole (§10).

A slide changes when the lecturer clicks or starts writing, which is almost
never when they finish a sentence - and Whisper does not help: it emits
segments of a few seconds each that routinely start and end mid-sentence, with
the full stop buried in the middle of one of them.

:func:`cuts_for_boundaries` finds, for every slide change that lands inside a
line, the sentence end nearest to it. Splitting the line there gives the
alignment a join to work with, so one slide ends on a finished thought and the
next begins a new one.

Times inside a line are estimated from the position of the text, the same
proportional rule the manual split uses: a segment-level transcript knows
nothing finer. Pure functions over strings and times - no Qt, no store.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence

from .models import TranscriptSegment

__all__ = ["WINDOW", "cut_offset", "cuts_for_boundaries", "sentence_offsets"]

#: How far from the slide change a sentence end may be and still be used, in
#: seconds. Beyond this the cut would move speech to the wrong slide, which is
#: worse than a sentence in two halves.
WINDOW = 6.0

_ENDINGS = ".!?…。！？"
_TRAILING = "\"'”’)]}」』"


def sentence_offsets(text: str) -> list[int]:
    """Character offsets just after each sentence end inside ``text``.

    Only ends followed by a space and more text count, so decimals ("0.5") and
    an end that is simply the end of the line are left alone.
    """
    offsets: list[int] = []
    length = len(text)
    for index, character in enumerate(text):
        if character not in _ENDINGS:
            continue
        end = index + 1
        while end < length and text[end] in _TRAILING:
            end += 1
        if end >= length or not text[end].isspace():
            continue
        if text[:end].strip() and text[end:].strip():
            offsets.append(end)
    return offsets


def cut_offset(
    segment: TranscriptSegment, boundary: float, *, window: float = WINDOW
) -> int | None:
    """Offset in ``segment`` to cut at so ``boundary`` falls between sentences.

    ``None`` when the line holds no sentence end near enough to be worth it.
    """
    text = segment.effective_text
    duration = segment.duration
    if duration <= 0 or not text.strip():
        return None

    best: int | None = None
    best_distance = window
    for offset in sentence_offsets(text):
        at = segment.start + duration * (offset / len(text))
        distance = abs(at - boundary)
        if distance < best_distance:
            best, best_distance = offset, distance
    return best


def cuts_for_boundaries(
    segments: Sequence[TranscriptSegment],
    boundaries: Sequence[float],
    *,
    window: float = WINDOW,
) -> list[tuple[str, int]]:
    """``(segment id, offset)`` for every slide change that splits a sentence.

    One cut per line at most - the nearest boundary wins - so the result can be
    applied in one pass.
    """
    if not segments or not boundaries:
        return []

    starts = [segment.start for segment in segments]
    chosen: dict[str, tuple[int, float]] = {}

    for boundary in boundaries:
        index = bisect.bisect_right(starts, boundary) - 1
        if not 0 <= index < len(segments):
            continue
        segment = segments[index]
        if not segment.start < boundary < segment.end:
            continue
        offset = cut_offset(segment, boundary, window=window)
        if offset is None:
            continue
        at = segment.start + segment.duration * (offset / len(segment.effective_text))
        distance = abs(at - boundary)
        previous = chosen.get(segment.id)
        if previous is None or distance < previous[1]:
            chosen[segment.id] = (offset, distance)

    order = {segment.id: index for index, segment in enumerate(segments)}
    return sorted(
        ((segment_id, offset) for segment_id, (offset, _) in chosen.items()),
        key=lambda cut: order[cut[0]],
    )
