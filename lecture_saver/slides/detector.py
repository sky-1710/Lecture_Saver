"""Pass B: turn a descriptor sequence into slide appearances.

A pure function over arrays. No I/O, no video, no Qt - which is what makes the
awkward real-lecture cases directly testable.

Two paths, not one chain of ANDs
--------------------------------
The first version combined every gate with a hard AND. That is what let an
*obvious* full-screen slide change be rejected by a single weak local signal:
a slide that only adds text replaces nothing, so the content-replacement gate
vetoed it. So the decision is now two paths:

**Strong global transition.** The whole picture changed - a different layout, a
different theme. Measured separation against the worst distractor tested is
more than an order of magnitude (see :mod:`global_analysis`), so this path may
bypass the local gates *and* annotation suppression. It still requires temporal
stability, and still requires the change to survive busy-region masking.

**Normal transition.** Everything else. Local, cell-level evidence that the
previous slide's content was actually replaced, with annotation suppression
applied to every candidate.

Annotation suppression
----------------------
Magnitude cannot separate handwriting from a slide change: on real lectures
annotation changes 1-6 % of ink pixels while a slide differing by one text
block changes 0.16 %. The annotation is the *larger* event. What separates them
is shape - a pen stroke survives an erosion, text does not - together with
whether the underlying slide structure is still there.

The shape test runs on **every** normal-path candidate, including ones whose
replacement score looks convincing. A laser dot crossing text can score 0.28
replacement while being unmistakably pen-shaped; an earlier version skipped the
check whenever replacement passed, and duplicated annotated slides because
of it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np

from ..core.slide_options import SlideDetectionOptions
from ..core.slides import SlideAppearance
from .diagnostics import BusyRegionReport, Candidate, Outcome
from .global_analysis import GlobalComparison, compare_global
from .ink_analysis import InkComparison, compare_ink

log = logging.getLogger(__name__)

__all__ = [
    "FrameComparison",
    "DetectionResult",
    "compare_frames",
    "cell_change_map",
    "cell_activity",
    "compute_noise_mask",
    "classify_candidate",
    "detect_appearances",
]

_SSIM_C1 = (0.01 * 255) ** 2
_SSIM_C2 = (0.03 * 255) ** 2

_WEIGHT_CELLS = 0.5
_WEIGHT_HAMMING = 0.3
_WEIGHT_SSIM = 0.2


@dataclass(frozen=True)
class FrameComparison:
    """Coarse thumbnail difference, used for stability and pre-filtering."""

    changed_cells: float
    hamming: float
    ssim: float
    score: float
    changed_rows: int = 0
    changed_cols: int = 0

    @property
    def dissimilarity(self) -> float:
        return 1.0 - self.ssim


@dataclass
class DetectionResult:
    appearances: list[SlideAppearance]
    #: Representative-frame timestamp chosen for each appearance, same order.
    representative_times: list[float]
    samples: int
    busy: BusyRegionReport
    #: Every moment the detector considered a boundary, with the gate results
    #: that decided it. This is what makes a real lecture tunable.
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def masked_cells(self) -> int:
        return self.busy.masked_cells

    @property
    def total_cells(self) -> int:
        return self.busy.total_cells

    @property
    def committed(self) -> list[Candidate]:
        return [c for c in self.candidates if c.outcome == Outcome.COMMITTED]

    @property
    def rejected(self) -> list[Candidate]:
        return [c for c in self.candidates if c.outcome == Outcome.REJECTED]

    @property
    def absorbed(self) -> list[Candidate]:
        return [c for c in self.candidates if c.outcome == Outcome.ABSORBED]

    def rejection_summary(self) -> dict[str, int]:
        """How many candidates each reason accounted for."""
        counts: dict[str, int] = {}
        for candidate in self.candidates:
            if candidate.outcome != Outcome.COMMITTED and candidate.reason:
                counts[candidate.reason] = counts.get(candidate.reason, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def strongest_rejected(self, limit: int = 20) -> list[Candidate]:
        """Near misses, strongest first - where a missed slide will be found."""
        interesting = [
            c
            for c in self.candidates
            if c.outcome != Outcome.COMMITTED and c.reason != "no ink changed"
        ]
        return sorted(interesting, key=lambda c: -c.strength)[:limit]


# --------------------------------------------------------------------------
# Coarse thumbnail comparison
# --------------------------------------------------------------------------


def _as_cells(frame: np.ndarray, rows: int, cols: int) -> np.ndarray:
    height, width = frame.shape
    cell_h, cell_w = height // rows, width // cols
    if cell_h == 0 or cell_w == 0:
        raise ValueError(f"grid {rows}x{cols} is finer than the {height}x{width} frame")
    trimmed = frame[: rows * cell_h, : cols * cell_w]
    return trimmed.reshape(rows, cell_h, cols, cell_w).swapaxes(1, 2)


def cell_change_map(
    frame_a: np.ndarray, frame_b: np.ndarray, options: SlideDetectionOptions
) -> np.ndarray:
    """Boolean ``(rows, cols)`` map of which thumbnail cells changed."""
    sampling = options.sampling
    a = _as_cells(frame_a.astype(np.int16), sampling.grid_rows, sampling.grid_cols)
    b = _as_cells(frame_b.astype(np.int16), sampling.grid_rows, sampling.grid_cols)
    moved = np.abs(a - b) > (sampling.pixel_threshold * 255.0)
    return moved.mean(axis=(2, 3)) > sampling.cell_change_ratio


def _mean_cell_ssim(
    frame_a: np.ndarray, frame_b: np.ndarray, options: SlideDetectionOptions
) -> float:
    sampling = options.sampling
    a = _as_cells(frame_a.astype(np.float64), sampling.grid_rows, sampling.grid_cols)
    b = _as_cells(frame_b.astype(np.float64), sampling.grid_rows, sampling.grid_cols)

    mu_a, mu_b = a.mean(axis=(2, 3)), b.mean(axis=(2, 3))
    var_a, var_b = a.var(axis=(2, 3)), b.var(axis=(2, 3))
    covariance = (a * b).mean(axis=(2, 3)) - (mu_a * mu_b)

    numerator = (2 * mu_a * mu_b + _SSIM_C1) * (2 * covariance + _SSIM_C2)
    denominator = (mu_a**2 + mu_b**2 + _SSIM_C1) * (var_a + var_b + _SSIM_C2)
    return float(np.mean(numerator / denominator))


def _hamming(hash_a: int, hash_b: int) -> int:
    return int(bin(int(hash_a) ^ int(hash_b)).count("1"))


def compare_frames(
    frame_a: np.ndarray,
    frame_b: np.ndarray,
    hash_a: int,
    hash_b: int,
    options: SlideDetectionOptions,
    mask: np.ndarray | None = None,
) -> FrameComparison:
    """Coarse difference between two thumbnails, ignoring busy cells."""
    changed = cell_change_map(frame_a, frame_b, options)

    if mask is not None:
        usable = ~mask
        usable_count = int(usable.sum())
        changed = changed & usable
        changed_fraction = float(changed.sum()) / usable_count if usable_count else 0.0
    else:
        changed_fraction = float(changed.mean())

    hamming = _hamming(hash_a, hash_b) / 64.0
    ssim = _mean_cell_ssim(frame_a, frame_b, options)
    score = (
        _WEIGHT_CELLS * changed_fraction
        + _WEIGHT_HAMMING * hamming
        + _WEIGHT_SSIM * (1.0 - ssim)
    )
    return FrameComparison(
        changed_cells=changed_fraction,
        hamming=hamming,
        ssim=ssim,
        score=score,
        changed_rows=int(changed.any(axis=1).sum()),
        changed_cols=int(changed.any(axis=0).sum()),
    )


# --------------------------------------------------------------------------
# Busy-region masking
# --------------------------------------------------------------------------


def cell_activity(thumbnails: np.ndarray, options: SlideDetectionOptions) -> np.ndarray:
    """How often each cell changes between consecutive samples, 0-1."""
    count = int(thumbnails.shape[0])
    shape = (options.sampling.grid_rows, options.sampling.grid_cols)
    if count < 2:
        return np.zeros(shape, dtype=np.float64)

    activity = np.zeros(shape, dtype=np.float64)
    for index in range(1, count):
        activity += cell_change_map(thumbnails[index - 1], thumbnails[index], options)
    return activity / (count - 1)


def compute_noise_mask(
    thumbnails: np.ndarray, options: SlideDetectionOptions
) -> np.ndarray:
    """Mark cells that change so often they cannot be slide content."""
    busy = options.busy_region_mask
    shape = (options.sampling.grid_rows, options.sampling.grid_cols)
    empty = np.zeros(shape, dtype=bool)

    count = int(thumbnails.shape[0])
    if count < max(2, busy.min_samples_for_masking):
        return empty

    activity = cell_activity(thumbnails, options)
    mask = activity > busy.noisy_cell_activity
    mask = _include_fringe(mask, activity, busy)

    # Refuse to mask so much that nothing meaningful is left to compare; that
    # would silently disable detection on a busy recording.
    limit = int(busy.max_masked_cell_fraction * mask.size)
    if int(mask.sum()) > limit:
        if limit <= 0:
            return empty
        keep = np.argsort(activity.ravel())[-limit:]
        mask = np.zeros(activity.size, dtype=bool)
        mask[keep] = True
        mask = mask.reshape(activity.shape)

    return mask


def _neighbours(mask: np.ndarray) -> np.ndarray:
    """Cells touching a set cell (4-connected)."""
    out = np.zeros_like(mask)
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def _include_fringe(
    mask: np.ndarray, activity: np.ndarray, busy: object
) -> np.ndarray:
    """Add partly-covered cells at the edge of a busy region.

    Hysteresis rather than plain dilation: a neighbouring cell joins only if it
    is itself moving, at a lower bar. Dilating unconditionally swallowed static
    slide area and pushed the mask into its own size cap, which then left the
    real busy cells uncovered.
    """
    ratio = getattr(busy, "fringe_activity_ratio", 0.0)
    if ratio <= 0:
        return mask
    lower = getattr(busy, "noisy_cell_activity", 1.0) * ratio

    grown = mask.copy()
    for _ in range(mask.size):  # settles long before this bound
        candidates = _neighbours(grown) & ~grown & (activity > lower)
        if not candidates.any():
            break
        grown |= candidates
    return grown


def _busy_report(
    thumbnails: np.ndarray, mask: np.ndarray, options: SlideDetectionOptions
) -> BusyRegionReport:
    activity = cell_activity(thumbnails, options)
    fraction = float(mask.mean()) if mask.size else 0.0
    return BusyRegionReport(
        mask=[[bool(v) for v in row] for row in mask.tolist()],
        activity=[[round(float(v), 3) for v in row] for row in activity.tolist()],
        masked_cells=int(mask.sum()),
        total_cells=int(mask.size),
        warn=fraction >= options.busy_region_mask.warn_masked_fraction,
    )


# --------------------------------------------------------------------------
# The decision
# --------------------------------------------------------------------------


def classify_candidate(
    ink: InkComparison,
    glob: GlobalComparison,
    options: SlideDetectionOptions,
    candidate: Candidate,
) -> bool:
    """Decide whether a difference is a new slide, recording every gate.

    Returns True to commit. A False result leaves ``candidate`` marked either
    ``REJECTED`` (not enough happened) or ``ABSORBED`` (something happened, but
    it belongs to the same slide).
    """
    normal = options.normal_transition
    annotation = options.annotation_filter
    strong = options.strong_global_transition

    candidate.metrics.update(
        {
            "ink_change": ink.change,
            "ink_cells": ink.cells,
            "ink_rows": float(ink.rows),
            "ink_cols": float(ink.cols),
            "stroke_thickness": ink.thickness,
            "stroke_thickness_all": ink.thickness_all,
            "stroke_thickness_added": ink.thickness_added,
            "content_replaced": ink.replaced,
            "replacement_support": float(ink.replaced_support),
            "low_frequency_change": glob.low_frequency,
            "histogram_change": glob.histogram,
            "baseline_similarity": glob.ssim,
            "busy_masked_fraction": glob.masked_fraction,
        }
    )

    # -- path 1: an unmistakable whole-screen change -----------------------
    candidate.add(
        "low frequency change", glob.low_frequency,
        strong.min_low_frequency_change,
        glob.low_frequency >= strong.min_low_frequency_change,
    )
    candidate.add(
        "histogram change", glob.histogram, strong.min_histogram_change,
        glob.histogram >= strong.min_histogram_change,
    )
    candidate.add(
        "baseline similarity", glob.ssim, strong.max_global_ssim,
        glob.ssim <= strong.max_global_ssim, "<=",
    )

    if glob.is_strong(options):
        candidate.path = "strong global"
        # How far past the trigger it went, capped. A full-screen change lands
        # at 1.0; something that only just qualified reads lower.
        headroom = max(
            glob.low_frequency / max(1e-6, strong.min_low_frequency_change),
            glob.histogram / max(1e-6, strong.min_histogram_change),
        )
        candidate.confidence = min(1.0, 0.6 + headroom / 5.0)
        return True

    candidate.path = "normal"

    # -- path 2: local evidence -------------------------------------------
    if ink.is_empty:
        candidate.reject("no ink changed")
        return False

    if not candidate.add(
        "ink change", ink.change, normal.min_ink_change,
        ink.change >= normal.min_ink_change,
    ):
        candidate.reject("min_ink_change")
        return False

    if not candidate.add(
        "ink cells", ink.cells, normal.min_ink_cell_fraction,
        ink.cells >= normal.min_ink_cell_fraction,
    ):
        candidate.reject("min_ink_cell_fraction")
        return False

    if not candidate.add(
        "ink columns", ink.cols, normal.min_ink_cols,
        ink.cols >= normal.min_ink_cols,
    ):
        candidate.reject("min_ink_cols")
        return False

    # Annotation suppression, applied to every normal-path candidate. A pen
    # stroke is thick; text is not. Skipping this when replacement looked
    # convincing is what duplicated annotated slides on a real lecture - but
    # only replacement alone. Thick content that replaced most of what was
    # there across a wide area is a new slide with bold text or pictures.
    if not candidate.add(
        "stroke thickness", ink.thickness, annotation.max_stroke_thickness,
        ink.thickness < annotation.max_stroke_thickness, "<",
    ):
        wide = candidate.add(
            "thick content cells", ink.cells, annotation.thick_content_min_cells,
            ink.cells >= annotation.thick_content_min_cells,
        )
        replaced = wide and candidate.add(
            "thick content replaced", ink.replaced,
            annotation.thick_content_min_replaced,
            ink.replaced >= annotation.thick_content_min_replaced,
        )
        if not replaced:
            candidate.absorb("annotation (thick strokes)")
            return False

    support_ok = ink.replaced_support >= normal.min_replacement_support
    candidate.add(
        "replacement support", ink.replaced_support,
        normal.min_replacement_support, support_ok,
    )

    if not candidate.add(
        "content replaced", ink.replaced, normal.min_content_replaced,
        ink.replaced >= normal.min_content_replaced,
    ):
        # Nothing was destroyed. Either the slide's structure is still intact
        # and this is annotation, or the change landed in empty space, which is
        # what a build animation looks like.
        if not support_ok:
            candidate.absorb("addition into empty space")
        elif glob.low_frequency < annotation.max_low_frequency_change:
            candidate.absorb("annotation (content preserved)")
        else:
            candidate.absorb("no content replaced")
        return False

    candidate.confidence = min(1.0, 0.5 + ink.replaced / 2.0)
    return True


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def detect_appearances(
    thumbnails: np.ndarray,
    times: np.ndarray,
    hashes: np.ndarray,
    *,
    duration: float,
    options: SlideDetectionOptions | None = None,
    ink: object | None = None,
) -> DetectionResult:
    """Find slide appearances in a descriptor sequence.

    ``ink`` is a :class:`~lecture_saver.slides.descriptors.FrameDescriptors`.
    Without its masks only the coarse signals are available, and annotation
    cannot be told from a slide change.

    Pure and deterministic: the same descriptors and options always produce the
    same result, which is what makes re-running with new thresholds instant.
    """
    options = options or SlideDetectionOptions()
    count = int(thumbnails.shape[0])

    if count == 0:
        shape = (options.sampling.grid_rows, options.sampling.grid_cols)
        empty = np.zeros(shape, dtype=bool)
        return DetectionResult([], [], 0, _busy_report(thumbnails, empty, options), [])

    has_ink = bool(getattr(ink, "has_ink", False))
    mask = compute_noise_mask(thumbnails, options)
    busy_report = _busy_report(thumbnails, mask, options)
    end_of_video = float(max(duration, times[-1]))

    starts: list[float] = [0.0]
    capture_times: list[float | None] = [None]
    confidences: list[float | None] = [None]
    reasons: list[str | None] = [None]
    candidates: list[Candidate] = []
    reference = 0

    index = 1
    while index < count:
        candidate_time = float(times[index])

        if candidate_time - starts[-1] < options.timing.min_slide_seconds:
            index += 1
            continue

        glob = compare_global(thumbnails[reference], thumbnails[index], options, mask)
        coarse = compare_frames(
            thumbnails[reference], thumbnails[index],
            int(hashes[reference]), int(hashes[index]), options, mask,
        )

        # Cheap pre-filter so the ink work only runs where something moved.
        if coarse.changed_cells <= 0.0 and glob.low_frequency <= 0.0:
            index += 1
            continue

        record = Candidate(time=candidate_time)

        if has_ink:
            ink_diff = compare_ink(
                ink.ink_at(reference),  # type: ignore[union-attr]
                ink.ink_at(index),  # type: ignore[union-attr]
                options,
                mask,
            )
            accepted = classify_candidate(ink_diff, glob, options, record)
        else:
            accepted = glob.is_strong(options)
            record.path = "coarse fallback"
            record.add(
                "low frequency change", glob.low_frequency,
                options.strong_global_transition.min_low_frequency_change, accepted,
            )
            if not accepted:
                record.reject("coarse gates (no ink masks available)")

        if not accepted:
            candidates.append(record)
            if record.outcome == Outcome.ABSORBED:
                settled = _settled_index(thumbnails, hashes, index, count, options, mask)
                if settled is not None:
                    # Track the fuller frame so further marks are measured
                    # against what is on screen, and capture the image there.
                    reference = settled
                    capture_times[-1] = float(times[settled])
                    index = settled + 1
                    continue
            index += 1
            continue

        settled = _settled_index(thumbnails, hashes, index, count, options, mask)
        if settled is None:
            record.add("stable", 0.0, 1.0, False)
            record.reject("stable_samples (still changing)")
            candidates.append(record)
            index += 1
            continue
        record.add("stable", 1.0, 1.0, True)

        starts.append(candidate_time)
        capture_times.append(None)
        confidences.append(record.confidence)
        reasons.append(
            "strong global transition"
            if record.path == "strong global"
            else "content replaced"
        )
        record.commit()
        candidates.append(record)
        reference = settled
        index = settled + 1

    return _build_result(
        starts, capture_times, confidences, reasons,
        end_of_video, options, busy_report, count, candidates,
    )


def _settled_index(
    thumbnails: np.ndarray,
    hashes: np.ndarray,
    index: int,
    count: int,
    options: SlideDetectionOptions,
    mask: np.ndarray,
) -> int | None:
    """Index of the frame the picture settles on, or ``None`` if it never does.

    Every frame in the look-ahead window must match the candidate. A candidate
    in the final frame has nothing to confirm it and is rejected.
    """
    available = min(options.timing.stable_samples, count - 1 - index)
    if available <= 0:
        return None

    for offset in range(1, available + 1):
        following = compare_frames(
            thumbnails[index], thumbnails[index + offset],
            int(hashes[index]), int(hashes[index + offset]), options, mask,
        )
        if following.score > options.timing.stability_threshold:
            return None

    return index + available


def _build_result(
    starts: list[float],
    capture_times: list[float | None],
    confidences: list[float | None],
    reasons: list[str | None],
    end_of_video: float,
    options: SlideDetectionOptions,
    busy: BusyRegionReport,
    samples: int,
    candidates: list[Candidate],
) -> DetectionResult:
    appearances: list[SlideAppearance] = []
    representatives: list[float] = []

    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else end_of_video
        end = max(end, start)

        appearances.append(
            SlideAppearance(
                appearance_id=position + 1,
                # Phase 3 gives every appearance its own logical slide; Phase 7
                # can point several at one slide_id without changing anything.
                slide_id=position + 1,
                start=start,
                end=end,
                detection_confidence=confidences[position],
                detection_reason=reasons[position],
            )
        )
        representatives.append(
            _representative_time(start, end, capture_times[position], options)
        )

    return DetectionResult(
        appearances=appearances,
        representative_times=representatives,
        samples=samples,
        busy=busy,
        candidates=candidates,
    )


def _representative_time(
    start: float,
    end: float,
    settled_at: float | None,
    options: SlideDetectionOptions,
) -> float:
    """Pick a frame shortly after the transition, never a transition frame.

    When annotation or a build was absorbed into this appearance, the capture
    point moves to just after the last of it, so the saved image shows the
    finished slide rather than its first line.
    """
    anchor = start if settled_at is None else max(start, settled_at)
    target = anchor + options.timing.settle_seconds
    latest = end - 0.1
    if latest <= start:
        return start
    return min(max(target, start), latest)
