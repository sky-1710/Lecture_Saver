"""Transcript-to-slide alignment (§10).

Speech belongs to the slide that was on screen while it was said. Both inputs
are time intervals, so the rule is mostly temporal - with one correction that
matters more than it sounds:

* Each boundary is first moved to the nearest **end of a sentence** within a few
  seconds of it (:func:`snapped_starts`). A slide changes when the lecturer
  clicks or starts writing, which is almost never when they finish a sentence -
  and on a deck shown twice, before and after annotation, that lands
  mid-sentence at nearly every change. Without this, half a thought is left on
  one slide and half on the next.
* A segment inside one slide appearance belongs to that appearance.
* A segment crossing a boundary belongs to the appearance it overlaps **most**.
  On an exact tie it stays with the earlier one, where the sentence started.
* A segment overlapping no appearance - past the end of the last one, or in a
  gap left by hand-edited intervals - belongs to the appearance covering or
  preceding its start.

A boundary never moves more than half way into either of its neighbours, so a
slide that was only on screen briefly still keeps whatever was said over it
rather than having it pulled onto its neighbour.

Only the *reading* of the transcript moves. Slide start and end times are left
exactly as detected, so clicking a slide still seeks to the frame where it
appeared, and the time range printed under it stays true to the video.

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

__all__ = [
    "MIN_PAUSE",
    "SNAP_WINDOW",
    "Alignment",
    "align",
    "assign_segments",
    "snapped_starts",
]

#: How far a boundary may move to land between sentences, in seconds. Long
#: enough for a sentence that runs over the slide change, short enough that
#: speech plainly belonging to the next slide cannot be dragged backwards.
SNAP_WINDOW = 6.0

#: A gap in the speech counts as a break from here up. Whisper leaves no gap at
#: all inside one breath, so anything this size is a real stop.
MIN_PAUSE = 0.35

#: What a sentence can end on, ignoring quotes or brackets after it.
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_TRAILING = "\"'”’)]}」』 "


def _ends_a_sentence(text: str) -> bool:
    return text.rstrip(_TRAILING).endswith(_SENTENCE_END)


def snapped_starts(
    segments: Sequence[TranscriptSegment],
    appearances: Sequence[SlideAppearance],
    *,
    window: float = SNAP_WINDOW,
) -> list[float]:
    """Each appearance's start, moved to the nearest break in the speech.

    Breaks are ranked: the end of a sentence beats a pause, and both beat the
    join between two segments of one running sentence, which is no break at all
    and is never used. Ties go to the break nearest the real boundary.

    A boundary moves at most half way towards either neighbouring boundary, so
    the order is preserved and a short appearance cannot be emptied.
    """
    starts = [appearance.start for appearance in appearances]
    if window <= 0 or len(appearances) < 2 or len(segments) < 2:
        return starts

    # Every junction between two segments, with how strong a break it is.
    breaks: list[float] = []
    ranks: list[int] = []
    for previous, following in zip(segments, segments[1:], strict=False):
        rank = 0
        if _ends_a_sentence(previous.effective_text):
            rank += 2
        if following.start - previous.end >= MIN_PAUSE:
            rank += 1
        if rank:
            breaks.append(following.start)
            ranks.append(rank)
    if not breaks:
        return starts

    snapped = list(starts)
    for index in range(1, len(appearances)):
        original = starts[index]
        room = min(
            window,
            max(0.0, (original - starts[index - 1]) / 2),
            max(0.0, (appearances[index].end - original) / 2),
        )
        if room <= 0:
            continue

        first = bisect.bisect_left(breaks, original - room)
        last = bisect.bisect_right(breaks, original + room)
        best_time: float | None = None
        best_key: tuple[int, float] | None = None
        for position in range(first, last):
            key = (ranks[position], -abs(breaks[position] - original))
            if best_key is None or key > best_key:
                best_key, best_time = key, breaks[position]
        if best_time is not None:
            snapped[index] = best_time

    # The half-way limit makes crossings impossible, but never hand back an
    # order the callers would have to re-check.
    for index in range(1, len(snapped)):
        snapped[index] = max(snapped[index], snapped[index - 1])
    return snapped


def _effective_ends(
    appearances: Sequence[SlideAppearance], starts: Sequence[float]
) -> list[float]:
    """Ends that follow the snapped starts, keeping neighbours touching.

    Appearances from the detector run edge to edge, so a start that moved takes
    the previous end with it. A gap the user made by hand is left alone: it
    means "no slide was on screen here", and that is still true.
    """
    ends = [appearance.end for appearance in appearances]
    for index in range(len(appearances) - 1):
        touching = abs(appearances[index].end - appearances[index + 1].start) < 0.001
        if touching:
            ends[index] = starts[index + 1]
        ends[index] = max(ends[index], starts[index])
    if ends:
        ends[-1] = max(ends[-1], starts[-1])
    return ends


def assign_segments(
    segments: Sequence[TranscriptSegment],
    appearances: Sequence[SlideAppearance],
    *,
    snap: float = SNAP_WINDOW,
) -> list[int | None]:
    """For each segment, the index of the appearance it belongs to.

    ``None`` only when there are no appearances. Both sequences are expected in
    start order, as the stores keep them; the walk is then linear rather than
    comparing every segment with every slide. ``snap=0`` assigns against the
    detected boundaries exactly, without looking for a break in the speech.
    """
    if not appearances:
        return [None] * len(segments)

    starts = snapped_starts(segments, appearances, window=snap)
    ends = _effective_ends(appearances, starts)
    result: list[int | None] = []

    for segment in segments:
        # First appearance that has not ended by the time the segment starts.
        position = bisect.bisect_right(ends, segment.start)
        best: int | None = None
        best_overlap = 0.0
        while position < len(appearances) and starts[position] < segment.end:
            overlap = min(ends[position], segment.end) - max(starts[position], segment.start)
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
