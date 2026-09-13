"""Shape analysis of a change, on the high-resolution ink masks.

This module exists because of a finding from real lectures: **magnitude cannot
distinguish annotation from a slide change**. Measured on rendered decks, a
professor's handwriting changes 1-6 % of ink pixels, while a slide whose body
differs by one text block changes 0.16 %. The annotation is the *larger* event.
Any threshold that catches the real change also catches the handwriting.

What does separate them is the **shape** of the changed region:

* A pen stroke is several pixels thick and survives an erosion.
* Edited text is a one-pixel glyph edge and never survives one.

Measured across every fixture: annotation 0.17-0.53 of the changed ink survives
erosion, every genuine slide change 0.000-0.008. That signal only ever *rejects*
a candidate, so unlike a magnitude threshold it cannot cause a missed slide.

The second signal is what happened to the content that was already there. A
slide change destroys the previous slide's ink; annotation leaves it in place,
even when the pen is opaque and dark - because dark ink drawn over dark text is
still ink.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.slide_options import SlideDetectionOptions

__all__ = ["InkComparison", "compare_ink", "erode", "expand_cells"]


@dataclass(frozen=True)
class InkComparison:
    """Shape and content measurements for one pair of ink masks."""

    #: Share of all pixels whose ink state differs.
    change: float
    #: Share of ink-grid cells that changed.
    cells: float
    #: Distinct grid rows and columns containing a changed cell.
    rows: int
    cols: int
    #: Share of *all* changed ink surviving one erosion.
    thickness_all: float
    #: Share of the *newly added* ink surviving one erosion.
    thickness_added: float
    #: Share of *all* changed ink surviving one erosion.
    #:
    #: The two are reported separately because they disagree in exactly the
    #: cases that matter, and :attr:`thickness` - the smaller of them - is what
    #: the annotation gate uses:
    #:
    #: * Drawing on a slide makes **both** large: thick marks appeared, and the
    #:   overall change is those marks.
    #: * Moving on from an annotated slide makes only the symmetric one large:
    #:   the erased strokes are thick, but what *appeared* is ordinary text.
    #: * A pointer at a new position makes both large, which is correct - it is
    #:   a solid mark either way.
    #:
    #: Taking the minimum therefore suppresses annotation while still letting
    #: the lecturer leave an annotated slide.
    #:
    #: Drawing marks and clearing them look alike here, which is deliberate:
    #: thickness alone never decides anything. It is only consulted once
    #: ``replaced`` has shown that the underlying content survived, and moving
    #: on from an annotated slide destroys that content.
    #: Share of the baseline's ink inside the changed cells that is now gone.
    #: High means the previous content was genuinely replaced.
    replaced: float
    #: The gated figure: ``min(thickness_all, thickness_added)``. Measured on
    #: real clips, every genuine slide change scored 0.000-0.012 and every
    #: annotation or pointer 0.027-0.451.
    thickness: float
    #: Baseline ink pixels the ``replaced`` ratio was measured over. When this
    #: is tiny the ratio is not meaningful, because the change landed in empty
    #: space - which is what a build animation looks like.
    replaced_support: int

    @property
    def is_empty(self) -> bool:
        return self.change <= 0.0


def erode(mask: np.ndarray) -> np.ndarray:
    """4-connected erosion: a pixel survives only if its neighbours are set.

    One pass removes anything a single pixel wide, which at the ink resolution
    is exactly what text strokes are and pen strokes are not.
    """
    out = mask.copy()
    out[1:, :] &= mask[:-1, :]
    out[:-1, :] &= mask[1:, :]
    out[:, 1:] &= mask[:, :-1]
    out[:, :-1] &= mask[:, 1:]
    return out


def expand_cells(
    cells: np.ndarray, shape: tuple[int, int], rows: int, cols: int
) -> np.ndarray:
    """Blow a cell mask back up to a full-resolution pixel mask."""
    height, width = shape
    cell_h, cell_w = height // rows, width // cols
    expanded = np.kron(cells, np.ones((cell_h, cell_w), dtype=bool))
    return np.pad(
        expanded, ((0, height - expanded.shape[0]), (0, width - expanded.shape[1]))
    )


def _cell_fractions(mask: np.ndarray, rows: int, cols: int) -> np.ndarray:
    height, width = mask.shape
    cell_h, cell_w = height // rows, width // cols
    trimmed = mask[: rows * cell_h, : cols * cell_w].astype(np.float32)
    return trimmed.reshape(rows, cell_h, cols, cell_w).mean(axis=(1, 3))


def compare_ink(
    baseline: np.ndarray,
    candidate: np.ndarray,
    options: SlideDetectionOptions,
    busy: np.ndarray | None = None,
) -> InkComparison:
    """Measure the shape of the difference between two ink masks.

    ``busy`` is the coarse thumbnail-grid mask of permanently active regions -
    a webcam overlay, an embedded video. Those pixels are excluded here too,
    not just from the thumbnail comparison; without that a talking head passes
    every shape gate, because a face is neither thick like a pen stroke nor
    preserved like annotated text.
    """
    changed = baseline ^ candidate
    if busy is not None and busy.any():
        changed = changed & ~expand_cells(busy, changed.shape, *busy.shape)
    changed_count = int(changed.sum())
    if changed_count == 0:
        return InkComparison(0.0, 0.0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0)

    rows, cols = options.sampling.ink_grid_rows, options.sampling.ink_grid_cols
    cell_mask = _cell_fractions(changed, rows, cols) > options.sampling.ink_cell_ratio

    thickness_all = float(erode(changed).sum()) / changed_count
    added = candidate & changed
    added_count = int(added.sum())
    thickness_added = (
        float(erode(added).sum()) / added_count if added_count else 0.0
    )

    pixels = expand_cells(cell_mask, changed.shape, rows, cols)
    support = baseline & pixels
    support_count = int(support.sum())
    replaced = (
        float((support & ~candidate).sum()) / support_count if support_count else 0.0
    )

    return InkComparison(
        change=float(changed.mean()),
        cells=float(cell_mask.mean()),
        rows=int(cell_mask.any(axis=1).sum()),
        cols=int(cell_mask.any(axis=0).sum()),
        thickness_all=thickness_all,
        thickness_added=thickness_added,
        thickness=min(thickness_all, thickness_added),
        replaced=replaced,
        replaced_support=support_count,
    )
