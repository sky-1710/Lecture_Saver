"""Transcript-to-slide alignment (§10).

Speech belongs to the slide that was on screen while it was said. Both inputs
are time intervals, so the rule is purely temporal:

* A segment inside one slide appearance belongs to that appearance.
* A segment crossing a boundary belongs to the appearance it overlaps **most**.
  On an exact tie it stays with the earlier one, where the sentence started.
* A segment overlapping no appearance - past the end of the last one, or in a
  gap left by hand-edited intervals - belongs to the appearance covering or
  preceding its start.

The result is never the source of truth. Segment times and appearance times are,
and alignment is recomputed from them whenever either changes, so a slide edit
can never leave speech linked to a slide that no longer covers it. The
``slide_id`` written onto each segment is that computed result, stored so
exports and ``transcript.json`` carry it.

Pure functions over the core models: no Qt, no I/O. Replacing the rule means
replacing :func:`assign_segments` and nothing else.
"""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass

from .models import TranscriptSegment
from .slides import SlideAppearance

__all__ = ["Alignment", "align", "assign_segments"]


def assign_segments(
    segments: Sequence[TranscriptSegment],
    appearances: Sequence[SlideAppearance],
) -> list[int | None]:
    """For each segment, the index of the appearance it belongs to.

    ``None`` only when there are no appearances. Both sequences are expected in
    start order, as the stores keep them; the walk is then linear rather than
    comparing every segment with every slide.
    """
    if not appearances:
        return [None] * len(segments)

    ends = [a.end for a in appearances]
    starts = [a.start for a in appearances]
    result: list[int | None] = []

    for segment in segments:
        # First appearance that has not ended by the time the segment starts.
        first = bisect.bisect_right(ends, segment.start)
        best: int | None = None
        best_overlap = 0.0
        position = first
        while position < len(appearances) and starts[position] < segment.end:
            overlap = appearances[position].overlap_with(segment.start, segment.end)
            if overlap > best_overlap:
                best, best_overlap = position, overlap
            position += 1

        if best is None:
            best = _covering_or_preceding(starts, segment.start)
        result.append(best)

    return result


def _covering_or_preceding(starts: list[float], seconds: float) -> int:
    """The last appearance starting at or before ``seconds``, else the first."""
    return max(0, bisect.bisect_right(starts, seconds) - 1)


@dataclass(frozen=True)
class Alignment:
    """Which segments belong to which appearance, in both directions."""

    #: ``by_segment[i]`` is the appearance index for segment ``i``, or ``None``.
    by_segment: tuple[int | None, ...]
    #: ``by_appearance[j]`` lists the segment indices for appearance ``j``, in
    #: time order.
    by_appearance: tuple[tuple[int, ...], ...]

    @property
    def is_empty(self) -> bool:
        return not self.by_appearance

    def segments_for(self, appearance_index: int) -> tuple[int, ...]:
        if 0 <= appearance_index < len(self.by_appearance):
            return self.by_appearance[appearance_index]
        return ()

    def appearance_for(self, segment_index: int) -> int | None:
        if 0 <= segment_index < len(self.by_segment):
            return self.by_segment[segment_index]
        return None


def align(
    segments: Sequence[TranscriptSegment],
    appearances: Sequence[SlideAppearance],
) -> Alignment:
    """Assign every segment and group the result per appearance."""
    by_segment = assign_segments(segments, appearances)
    groups: list[list[int]] = [[] for _ in appearances]
    for segment_index, appearance_index in enumerate(by_segment):
        if appearance_index is not None:
            groups[appearance_index].append(segment_index)
    return Alignment(
        by_segment=tuple(by_segment),
        by_appearance=tuple(tuple(group) for group in groups),
    )
