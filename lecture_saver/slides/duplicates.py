"""Recognising a slide the professor went back to (§9).

Lectures move 5 -> 6 -> 7 -> 6. The second visit to slide 6 is a new
*appearance* - its own interval, its own speech - of the same *slide*. This pass
runs after detection and gives such appearances the ``slide_id`` of the slide
they return to, so they share its identity and its picture.

Two captured frames are the same slide when their ink masks are nearly
identical *and* mostly overlap. Measured on a real lecture, the two genuine
returns differed in 0.10 % and 0.30 % of pixels with 0.99 overlap, while the
closest different pair - a clean slide and the same slide with notes - differed
in 1.5 %, and two sparse title slides that looked alike overlapped only 0.58.
The overlap requirement is what keeps near-empty slides from being merged.

Pure and deterministic, run from the frame cache like the detector.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from ..core.slide_options import SlideDetectionOptions
from .detector import DetectionResult
from .ink_analysis import expand_cells

__all__ = ["link_returned_slides"]


def link_returned_slides(
    result: DetectionResult,
    descriptors: object,
    options: SlideDetectionOptions,
) -> DetectionResult:
    """Give appearances of an already-seen slide that slide's ``slide_id``.

    Slide ids are renumbered so distinct slides are 1, 2, 3... in order of first
    appearance; appearance ids are left alone.
    """
    settings = options.duplicate_slides
    appearances = result.appearances
    if (
        not settings.enabled
        or len(appearances) < 2
        or not bool(getattr(descriptors, "has_ink", False))
    ):
        return result

    times = np.asarray(descriptors.times, dtype=np.float64)  # type: ignore[attr-defined]
    ink_at = descriptors.ink_at  # type: ignore[attr-defined]
    hashes = descriptors.hashes  # type: ignore[attr-defined]
    keep = _outside_busy(result, ink_at(0).shape)

    frames = []
    for capture in result.representative_times:
        index = int(np.argmin(np.abs(times - capture)))
        frames.append((ink_at(index) & keep, int(hashes[index])))

    # (frame index of the slide's first capture, new slide id)
    distinct: list[tuple[int, int]] = []
    linked = []
    for position, appearance in enumerate(appearances):
        ink, fingerprint = frames[position]
        match = next(
            (
                slide_id for first, slide_id in distinct
                if _same_slide(ink, fingerprint, *frames[first], settings)
            ),
            None,
        )
        if match is None:
            match = len(distinct) + 1
            distinct.append((position, match))
        linked.append(replace(appearance, slide_id=match))

    return replace(result, appearances=linked)


def _same_slide(
    ink_a: np.ndarray,
    hash_a: int,
    ink_b: np.ndarray,
    hash_b: int,
    settings: object,
) -> bool:
    if bin(hash_a ^ hash_b).count("1") > settings.max_hash_distance:  # type: ignore[attr-defined]
        return False
    difference = float((ink_a ^ ink_b).sum()) / ink_a.size
    if difference > settings.max_ink_difference:  # type: ignore[attr-defined]
        return False
    union = int((ink_a | ink_b).sum())
    overlap = 1.0 if union == 0 else float((ink_a & ink_b).sum()) / union
    return overlap >= settings.min_ink_overlap  # type: ignore[attr-defined]


def _outside_busy(result: DetectionResult, shape: tuple[int, ...]) -> np.ndarray:
    height, width = int(shape[0]), int(shape[1])
    busy = np.array(result.busy.mask, dtype=bool)
    if busy.ndim != 2 or not busy.any():
        return np.ones((height, width), dtype=bool)
    return ~expand_cells(busy, (height, width), *busy.shape)
