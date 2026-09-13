"""Keep a clean and an annotated version of slides the professor wrote on.

The detector deliberately absorbs handwriting into the slide it was written on,
and captures the image after the last mark - so the clean slide was lost. This
pass runs on the detector's result and, for every slide whose annotation became
substantial, splits the appearance where the writing started:

* ``before_annotation`` - from the slide's start until the first real mark,
  captured on the clean frame.
* ``annotated`` - from the first mark to the end, captured on the fullest board.

Erasing is already a boundary: wiping the board removes ink the detector was
tracking, and it commits a new appearance there. What this pass adds is
recognising that the new appearance opens on the *same* clean slide. The
annotated version before it is then marked ``before_erase``, and the clean
slide is not saved a second time.

Splitting in time, rather than attaching a second picture to one appearance,
means speech during the writing is later aligned with the annotated image, and
every consumer of appearances keeps working unchanged.

Pure and deterministic like the detector, so a re-run from the frame cache
gives the same result.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ..core.slide_options import SlideDetectionOptions
from ..core.slides import SlideAppearance
from .detector import DetectionResult
from .ink_analysis import expand_cells

__all__ = ["add_annotation_snapshots"]


def add_annotation_snapshots(
    result: DetectionResult,
    descriptors: object,
    options: SlideDetectionOptions,
) -> DetectionResult:
    """Return ``result`` with heavily annotated slides split in two.

    ``descriptors`` is the :class:`~lecture_saver.slides.descriptors.FrameDescriptors`
    the detection ran on. Without ink masks there is nothing to measure and the
    result is returned unchanged.
    """
    settings = options.annotation_snapshots
    if (
        not settings.enabled
        or not result.appearances
        or not bool(getattr(descriptors, "has_ink", False))
    ):
        return result

    times = np.asarray(descriptors.times, dtype=np.float64)  # type: ignore[attr-defined]
    ink_at = descriptors.ink_at  # type: ignore[attr-defined]
    keep = _outside_busy(result, ink_at(0).shape)
    pixels = max(1, int(keep.sum()))

    def amount(a: np.ndarray, b: np.ndarray) -> float:
        return float(((a ^ b) & keep).sum()) / pixels

    spans = [
        np.flatnonzero((times >= a.start) & (times < a.end)) for a in result.appearances
    ]
    openings = [
        _opening(indices, times, a.start + options.timing.settle_seconds)
        for a, indices in zip(result.appearances, spans, strict=True)
    ]

    out: list[tuple[SlideAppearance, float]] = []
    previous_clean: np.ndarray | None = None
    previous_annotated = False

    for position, (appearance, representative) in enumerate(
        zip(result.appearances, result.representative_times, strict=True)
    ):
        indices = spans[position]
        opening = openings[position]
        if opening is None:
            out.append((replace(appearance), representative))
            previous_clean, previous_annotated = None, False
            continue

        opening_ink = ink_at(opening)

        erased = (
            previous_annotated
            and previous_clean is not None
            and amount(opening_ink, previous_clean) <= settings.same_slide_tolerance
        )
        if erased:
            out[-1][0].annotation_state = "before_erase"
            clean = previous_clean
        else:
            clean = opening_ink
        assert clean is not None

        # Only frames from the settled opening on, and not the tail of the
        # transition into the next slide: the detector commits a boundary once
        # the picture settles, so the last samples before it can already show
        # the next slide, and that would read as a very full board.
        following = next(
            (openings[p] for p in range(position + 1, len(openings))
             if openings[p] is not None),
            None,
        )
        next_ink = ink_at(following) if following is not None else None
        kept: list[int] = []
        raw: list[float] = []
        for i in indices[indices >= opening]:
            frame = ink_at(int(i))
            level = amount(frame, clean)
            if next_ink is not None and amount(frame, next_ink) < level:
                continue
            kept.append(int(i))
            raw.append(level)
        if not kept:
            out.append((replace(appearance), representative))
            previous_clean, previous_annotated = clean, False
            continue

        indices = np.array(kept, dtype=np.int64)
        levels = _smooth(np.array(raw), options.timing.stable_samples)
        peak = float(levels.max())

        if peak < settings.min_annotation_ink:
            out.append((replace(appearance), representative))
            previous_clean, previous_annotated = clean, False
            continue

        fullest = int(np.flatnonzero(levels >= peak * settings.fullest_ratio)[-1])
        fullest_time = float(times[indices[fullest]])

        onset = int(np.flatnonzero(levels >= settings.min_annotation_ink)[0])
        while onset > 0 and levels[onset - 1] >= settings.onset_ink:
            onset -= 1
        onset_time = float(times[indices[onset]])

        minimum = options.timing.min_slide_seconds
        clean_time = float(times[opening])
        can_split = (
            not erased
            and onset_time - appearance.start >= minimum
            and appearance.end - onset_time >= minimum
            and clean_time < onset_time
        )

        if can_split:
            out.append((
                replace(appearance, end=onset_time, annotation_state="before_annotation"),
                clean_time,
            ))
            out.append((
                replace(
                    appearance,
                    start=onset_time,
                    detection_confidence=None,
                    detection_reason="annotation started",
                    annotation_state="annotated",
                ),
                _within(fullest_time, onset_time, appearance.end),
            ))
        else:
            out.append((
                replace(appearance, annotation_state="annotated"),
                _within(fullest_time, appearance.start, appearance.end),
            ))
        previous_clean, previous_annotated = clean, True

    appearances: list[SlideAppearance] = []
    representatives: list[float] = []
    for position, (appearance, capture) in enumerate(out, start=1):
        appearance.appearance_id = position
        appearance.slide_id = position
        appearances.append(appearance)
        representatives.append(capture)

    return replace(result, appearances=appearances, representative_times=representatives)


def _opening(indices: np.ndarray, times: np.ndarray, settle: float) -> int | None:
    """The first sample after the transition has settled, if the span has any."""
    if len(indices) == 0:
        return None
    return int(next((i for i in indices if times[i] >= settle), indices[-1]))


def _outside_busy(result: DetectionResult, shape: tuple[int, ...]) -> np.ndarray:
    """Pixel mask of what may be measured: everything but busy regions."""
    height, width = int(shape[0]), int(shape[1])
    busy = np.array(result.busy.mask, dtype=bool)
    if busy.ndim != 2 or not busy.any():
        return np.ones((height, width), dtype=bool)
    return ~expand_cells(busy, (height, width), *busy.shape)


def _smooth(values: np.ndarray, window: int) -> np.ndarray:
    """Running median, so a cursor passing through for a sample is ignored."""
    window = max(1, int(window))
    if window == 1 or len(values) < window:
        return values
    before = window // 2
    padded = np.pad(values, (before, window - 1 - before), mode="edge")
    stacked = np.lib.stride_tricks.sliding_window_view(padded, window)
    return np.asarray(np.median(stacked, axis=1))


def _within(seconds: float, start: float, end: float) -> float:
    """Clamp a capture time inside ``[start, end)``."""
    latest = end - 0.1
    if latest <= start:
        return start
    return min(max(seconds, start), latest)
