"""Whole-frame structural signals: the strong-global transition path.

The local, cell-level evidence in :mod:`~lecture_saver.slides.ink_analysis` is
what separates a pen stroke from an edited line of text. But combining every
gate with a hard AND meant an unmistakable full-screen change could still be
rejected by one weak local signal - a slide that only *adds* text replaces
nothing, so the content-replacement gate vetoed it even though the entire
screen had visibly changed.

These signals answer a different, coarser question: *has the overall structure
of the picture changed?* Measured on rendered lectures:

===========================  ==================  ==================
signal                       full-screen change  worst distractor
===========================  ==================  ==================
low-frequency difference     0.746 - 0.749       0.050
histogram distance           0.976 - 0.978       0.119
global SSIM                  -0.088              0.950
===========================  ==================  ==================

The gap is more than an order of magnitude, which is why this path is allowed
to override the local gates - including annotation suppression, since a
lecturer moving on from an annotated slide is exactly a strong global change.

Everything here is computed on the coarse thumbnail grid, excluding cells the
busy-region mask has claimed, so a full-screen webcam or an embedded video
cannot trigger the override.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.slide_options import SlideDetectionOptions

__all__ = ["GlobalComparison", "compare_global", "cell_means"]

_SSIM_C1 = (0.01 * 255) ** 2
_SSIM_C2 = (0.03 * 255) ** 2


@dataclass(frozen=True)
class GlobalComparison:
    """Coarse structural difference between two thumbnails."""

    #: Mean absolute difference of the low-frequency (per-cell mean) image,
    #: 0-1. Thin handwriting barely moves a cell mean; a new layout moves most.
    low_frequency: float
    #: Total-variation distance between the two brightness histograms, 0-1.
    #: Catches a change of theme or background that keeps its layout.
    histogram: float
    #: Structural similarity of the low-frequency image, -1 to 1.
    ssim: float
    #: Share of the grid the busy mask excluded from all of the above.
    masked_fraction: float

    def is_strong(self, options: SlideDetectionOptions) -> bool:
        """Whether this is an unmistakable whole-screen change.

        Any one of the three is enough. They fail in different ways - a dark
        theme swap moves the histogram but not necessarily the layout, a
        rearranged white slide moves the layout but not the histogram - so
        requiring all three would reintroduce the hard-AND problem this path
        exists to solve.
        """
        strong = options.strong_global_transition
        if not strong.enabled:
            return False
        return (
            self.low_frequency >= strong.min_low_frequency_change
            or self.histogram >= strong.min_histogram_change
            or self.ssim <= strong.max_global_ssim
        )


def cell_means(frame: np.ndarray, rows: int, cols: int) -> np.ndarray:
    """Per-cell mean: the low-frequency representation of a frame."""
    height, width = frame.shape
    cell_h, cell_w = height // rows, width // cols
    trimmed = frame[: rows * cell_h, : cols * cell_w].astype(np.float32)
    return trimmed.reshape(rows, cell_h, cols, cell_w).mean(axis=(1, 3))


def _histogram(frame: np.ndarray, keep: np.ndarray | None) -> np.ndarray:
    values = frame[keep] if keep is not None else frame.ravel()
    if values.size == 0:
        return np.zeros(32, dtype=np.float64)
    counts = np.histogram(values, bins=32, range=(0, 256))[0].astype(np.float64)
    total = counts.sum()
    return counts / total if total else counts


def _expand(cells: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    rows, cols = cells.shape
    height, width = shape
    cell_h, cell_w = height // rows, width // cols
    expanded = np.kron(cells, np.ones((cell_h, cell_w), dtype=bool))
    return np.pad(
        expanded, ((0, height - expanded.shape[0]), (0, width - expanded.shape[1]))
    )


def compare_global(
    baseline: np.ndarray,
    candidate: np.ndarray,
    options: SlideDetectionOptions,
    busy: np.ndarray | None = None,
) -> GlobalComparison:
    """Measure the coarse structural difference, ignoring busy regions."""
    rows = options.sampling.grid_rows
    cols = options.sampling.grid_cols

    means_a = cell_means(baseline, rows, cols)
    means_b = cell_means(candidate, rows, cols)

    if busy is not None and busy.shape == means_a.shape and busy.any():
        usable = ~busy
        masked_fraction = float(busy.mean())
    else:
        usable = np.ones_like(means_a, dtype=bool)
        masked_fraction = 0.0

    if not usable.any():
        return GlobalComparison(0.0, 0.0, 1.0, masked_fraction)

    a = means_a[usable]
    b = means_b[usable]

    low_frequency = float(np.abs(a - b).mean() / 255.0)

    keep = _expand(usable, baseline.shape) if masked_fraction else None
    histogram = float(
        0.5 * np.abs(_histogram(baseline, keep) - _histogram(candidate, keep)).sum()
    )

    mean_a, mean_b = float(a.mean()), float(b.mean())
    var_a, var_b = float(a.var()), float(b.var())
    covariance = float(((a - mean_a) * (b - mean_b)).mean())
    ssim = float(
        ((2 * mean_a * mean_b + _SSIM_C1) * (2 * covariance + _SSIM_C2))
        / ((mean_a**2 + mean_b**2 + _SSIM_C1) * (var_a + var_b + _SSIM_C2))
    )

    return GlobalComparison(
        low_frequency=low_frequency,
        histogram=histogram,
        ssim=ssim,
        masked_fraction=masked_fraction,
    )
