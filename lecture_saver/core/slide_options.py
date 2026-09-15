"""Slide detection settings, grouped by what they control.

The grouping is not cosmetic. Real-world tuning means finding *which kind* of
evidence rejected a transition, and the groups match the paths in the detector:

``sampling``
    What is extracted from the video. Changing anything here invalidates the
    cached frame scan and forces a re-decode; nothing else does.
``normal_transition``
    The ordinary path: local, cell-level evidence that content was replaced.
``strong_global_transition``
    The override for an unmistakable whole-screen change, which is allowed to
    bypass the weaker local gates.
``annotation_filter``
    Suppression of handwriting, highlighter and pen strokes.
``annotation_snapshots``
    Saving a heavily annotated slide as a clean and an annotated version.
``duplicate_slides``
    Recognising a slide the professor returned to.
``busy_region_mask``
    Ignoring webcam overlays, clocks and embedded video.
``timing``
    Stability, debounce, and when the representative frame is captured.
``image``
    How the saved slide pictures are written.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

__all__ = [
    "SamplingOptions",
    "NormalTransitionOptions",
    "StrongGlobalOptions",
    "AnnotationFilterOptions",
    "AnnotationSnapshotOptions",
    "DuplicateSlideOptions",
    "BusyRegionOptions",
    "TimingOptions",
    "ImageOptions",
    "SlideDetectionOptions",
]


@dataclass
class SamplingOptions:
    """What is pulled out of the video. The only group that affects the cache."""

    #: Frames analysed per second. A slide is on screen for tens of seconds, so
    #: 1 fps is ample and bounds transition timing error to one second.
    sample_fps: float = 1.0

    #: Coarse thumbnail, derived from the ink-resolution frame. Drives the
    #: busy-region mask, the perceptual hash and the global structural signals.
    thumb_width: int = 80
    thumb_height: int = 45
    #: Grid over the thumbnail. Doubles as the low-frequency representation
    #: used by the strong-global path.
    grid_cols: int = 8
    grid_rows: int = 5

    #: Frames are decoded at this size and also kept as a 1-bit "ink" mask
    #: (packed, 7.2 KB a frame). Two resolutions are needed because they answer
    #: different questions: the thumbnail says *how much* changed, the ink mask
    #: says *what shape* the change is - and shape is the only thing that
    #: separates a pen stroke from an edited line of text.
    ink_width: int = 320
    ink_height: int = 180
    #: A pixel is "ink" when it differs from the frame background by this many
    #: grey levels. Generous on purpose: body text, figures and pen strokes
    #: should all count.
    ink_tolerance: float = 30.0
    #: Grid over the ink mask, much finer than the thumbnail grid so a change
    #: confined to a single line of text still registers.
    ink_grid_cols: int = 24
    ink_grid_rows: int = 16
    #: Share of an ink cell's pixels that must change for the cell to count.
    ink_cell_ratio: float = 0.02

    #: Thumbnail-level pixel/cell thresholds, used for the busy-region mask and
    #: the stability check.
    pixel_threshold: float = 0.03
    cell_change_ratio: float = 0.10

    @property
    def ink_scale(self) -> int:
        return max(1, self.ink_width // self.thumb_width)

    @property
    def signature(self) -> str:
        """Identifies a descriptor cache built with these settings."""
        return (
            f"{self.sample_fps}:{self.thumb_width}x{self.thumb_height}"
            f":{self.ink_width}x{self.ink_height}:{self.ink_tolerance}"
        )


@dataclass
class NormalTransitionOptions:
    """The ordinary path: local evidence that content was actually replaced."""

    #: Minimum share of ink pixels that must change. Deliberately low: a slide
    #: whose body differs by one text block changes only ~0.16 % of ink pixels,
    #: while heavy handwriting changes up to 6 %. Magnitude cannot separate
    #: those, so this only has to reject noise.
    min_ink_change: float = 0.0008
    #: Minimum share of ink grid cells that must change.
    min_ink_cell_fraction: float = 0.004
    #: The change must span this many ink-grid columns. A cursor or laser dot
    #: occupies one. Rows are deliberately *not* gated: a slide differing by a
    #: single line of text occupies exactly one row, and gating rows is what
    #: made real changes disappear.
    min_ink_cols: int = 2

    #: Share of the previous slide's ink, inside the cells that changed, that
    #: must be gone. This is the positive evidence for a slide change.
    min_content_replaced: float = 0.15
    #: Baseline ink the ratio must be measured over before a *low* value means
    #: anything. Below it the change landed in empty space, which is what a
    #: build animation looks like.
    min_replacement_support: int = 60


@dataclass
class StrongGlobalOptions:
    """The override for an unmistakable whole-screen change.

    Combining every gate with a hard AND meant an obvious full-screen
    transition could still be rejected by one weak local signal - a slide that
    only *adds* text replaces nothing, so ``min_content_replaced`` vetoed it.
    This path exists so overwhelming global evidence can bypass those.

    Measured on rendered lectures: a genuine full-screen change scores 0.75
    low-frequency difference and 0.98 histogram distance, while the worst
    distractor tested (heavy opaque handwriting) reaches 0.05 and 0.12. The
    defaults sit far above the distractors and far below the real changes.
    """

    enabled: bool = True
    #: Any one of these three is enough, provided stability also holds. They
    #: fail in different ways - a dark-theme swap moves the histogram but not
    #: necessarily the layout, a rearranged white slide moves the layout but
    #: not the histogram - so requiring all three would reintroduce the
    #: hard-AND problem this path exists to solve.
    #:
    #: Measured, worst distractor (very heavy opaque handwriting) against a
    #: genuine full-screen change:
    #:   low frequency  0.13 vs 0.73
    #:   histogram      0.24 vs 1.00
    #:   global SSIM    0.23 vs 0.02   (lower means less similar)
    min_low_frequency_change: float = 0.25
    min_histogram_change: float = 0.50
    max_global_ssim: float = 0.10
    #: The override still requires the change to survive busy-region masking,
    #: so a full-screen webcam or an embedded video cannot trigger it.
    require_outside_busy: bool = True


@dataclass
class AnnotationFilterOptions:
    """Suppression of handwriting, highlighter and pen strokes."""

    #: Share of the changed ink surviving one erosion. A solid pen stroke is
    #: several pixels thick and survives (measured 0.13-0.48); edited text is a
    #: one-pixel glyph edge and never does (measured 0.000-0.013 across every
    #: genuine change tested).
    #:
    #: This is checked on *every* normal-path candidate, including ones whose
    #: content-replacement score looks convincing: a laser dot crossing text
    #: can score 0.28 replacement while being unmistakably pen-shaped.
    max_stroke_thickness: float = 0.05
    #: Thick marks that *replaced* most of the previous content over a wide area
    #: are the next slide, not annotation. Bold titles, photos and double-line
    #: shapes survive the erosion at analysis resolution, so on a real lecture
    #: five genuine changes scored thickness 0.09-0.22 and were absorbed.
    #: What separates them is what happened underneath: they replaced 0.69-0.88
    #: of the baseline across 0.31-0.72 of the ink cells, while every pen
    #: stroke replaced at most 0.05, and a cursor or laser dot that did score
    #: high replacement covered at most 0.016 of the cells.
    thick_content_min_replaced: float = 0.5
    #: Share of ink cells that must have changed for the override above.
    thick_content_min_cells: float = 0.15
    #: Below this much low-frequency change, the slide's overall structure is
    #: intact. Combined with nothing having been replaced, that is annotation
    #: accumulating on the same slide rather than a new one.
    max_low_frequency_change: float = 0.12
    #: An overlay cannot cover most of the grid; past this it is a new slide
    #: whatever shape the marks were.
    max_overlay_changed_cells: float = 0.55


@dataclass
class AnnotationSnapshotOptions:
    """Saving a heavily annotated slide as a clean and an annotated version.

    Amounts are the share of analysis pixels (outside busy regions) whose ink
    differs from the clean slide. Measured on a real lecture, slides the
    professor wrote on reached 0.015-0.038, while cursor movement and noise on
    untouched slides stayed under 0.002.
    """

    enabled: bool = True
    #: Annotation must reach this amount before the slide is split.
    min_annotation_ink: float = 0.008
    #: The annotation is taken to start where the amount last rose past this,
    #: so the clean version ends at the first real mark rather than when the
    #: slide finally became heavily annotated.
    onset_ink: float = 0.002
    #: A new appearance whose opening frame is within this amount of the
    #: previous slide's clean frame is that slide with its annotation erased.
    same_slide_tolerance: float = 0.002
    #: The annotated image is the last moment the amount was within this share
    #: of its peak - the fullest board, just before an erase or slide change.
    fullest_ratio: float = 0.95


@dataclass
class DuplicateSlideOptions:
    """When a later appearance is the same slide as an earlier one (§9).

    Measured on a real lecture: genuine returns differed in 0.10-0.30 % of
    ink pixels with 0.99 overlap; the closest different captures differed in
    1.5 %, and two alike sparse title slides overlapped only 0.58.
    """

    enabled: bool = True
    #: Share of analysis pixels whose ink may differ.
    max_ink_difference: float = 0.006
    #: Intersection over union of the two ink masks. Keeps near-empty slides
    #: that differ in a title from being merged.
    min_ink_overlap: float = 0.9
    #: Bits of the 64-bit perceptual hash that may differ; a cheap first check.
    max_hash_distance: int = 10


@dataclass
class BusyRegionOptions:
    """Ignoring regions that change constantly: webcams, clocks, video."""

    #: Cells changing in more than this share of all sampled intervals are
    #: treated as permanently busy. A slide cell changes in ~2 % of intervals,
    #: a webcam cell in ~90 %.
    noisy_cell_activity: float = 0.30
    #: Never mask more than this share of the grid, so a busy recording cannot
    #: end up with nothing left to compare.
    max_masked_cell_fraction: float = 0.40
    #: Fringe cells: a cell touching a busy one is also masked when its own
    #: activity exceeds ``noisy_cell_activity * fringe_activity_ratio``.
    #:
    #: A webcam or an embedded video rarely lands on cell boundaries, so its
    #: edge falls in partly-covered cells that score well under the main
    #: threshold - measured 0.13 beside a core cell at 0.94. Those still carry
    #: moving content, and leaving them in was enough to make a real slide
    #: change look pen-shaped. Growing the mask by a whole ring instead was too
    #: blunt: it swallowed static slide area and pushed the mask into the cap.
    fringe_activity_ratio: float = 0.3
    #: Masking needs a reasonable sample count to mean anything.
    min_samples_for_masking: int = 60
    #: Above this share the mask is reported as a warning in diagnostics: it is
    #: probably hiding slide content, not just an overlay.
    warn_masked_fraction: float = 0.25


@dataclass
class TimingOptions:
    """Stability, debounce, and when the representative frame is captured."""

    #: Consecutive samples that must agree with the candidate before a change
    #: is committed. Rejects crossfades and wipes, whose frames differ from
    #: each other.
    stable_samples: int = 3
    #: Score below which two thumbnails count as "the same slide" for the
    #: stability check.
    stability_threshold: float = 0.03
    #: A new slide cannot be committed sooner than this after the previous one.
    min_slide_seconds: float = 4.0
    #: The representative frame is captured this far after the transition, so
    #: it is post-animation rather than mid-wipe.
    settle_seconds: float = 1.0


@dataclass
class ImageOptions:
    """How the saved slide pictures are written."""

    max_width: int = 1280
    quality: int = 3


@dataclass
class SlideDetectionOptions:
    """Every setting the detector uses, grouped by what it controls."""

    sampling: SamplingOptions = field(default_factory=SamplingOptions)
    normal_transition: NormalTransitionOptions = field(
        default_factory=NormalTransitionOptions
    )
    strong_global_transition: StrongGlobalOptions = field(
        default_factory=StrongGlobalOptions
    )
    annotation_filter: AnnotationFilterOptions = field(
        default_factory=AnnotationFilterOptions
    )
    annotation_snapshots: AnnotationSnapshotOptions = field(
        default_factory=AnnotationSnapshotOptions
    )
    duplicate_slides: DuplicateSlideOptions = field(default_factory=DuplicateSlideOptions)
    busy_region_mask: BusyRegionOptions = field(default_factory=BusyRegionOptions)
    timing: TimingOptions = field(default_factory=TimingOptions)
    image: ImageOptions = field(default_factory=ImageOptions)

    # -- convenience -------------------------------------------------------

    @property
    def cell_count(self) -> int:
        return self.sampling.grid_cols * self.sampling.grid_rows

    @property
    def sample_signature(self) -> str:
        """Changing a threshold must not invalidate the descriptor cache."""
        return self.sampling.signature

    # -- persistence -------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {name: asdict(getattr(self, name)) for name in _GROUPS}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SlideDetectionOptions:
        """Build from stored settings.

        Accepts both the grouped layout and the flat one written by earlier
        versions, and ignores keys this version does not know, so an older
        project keeps working and a newer one does not crash.
        """
        if not data:
            return cls()

        options = cls()
        grouped = any(name in data for name in _GROUPS)
        for name in _GROUPS:
            group = getattr(options, name)
            values = data.get(name, {}) if grouped else data
            if not isinstance(values, dict):
                continue
            known = {f.name for f in fields(group)}
            for key, value in values.items():
                if key in known:
                    setattr(group, key, value)

        if not grouped:
            _apply_legacy_aliases(options, data)
        return options


_GROUPS = (
    "sampling",
    "normal_transition",
    "strong_global_transition",
    "annotation_filter",
    "annotation_snapshots",
    "duplicate_slides",
    "busy_region_mask",
    "timing",
    "image",
)

#: Field names that moved when the settings were grouped.
_LEGACY_ALIASES = {
    "image_max_width": ("image", "max_width"),
    "image_quality": ("image", "quality"),
}


def _apply_legacy_aliases(options: SlideDetectionOptions, data: dict[str, Any]) -> None:
    for old, (group_name, new) in _LEGACY_ALIASES.items():
        if old in data:
            setattr(getattr(options, group_name), new, data[old])
