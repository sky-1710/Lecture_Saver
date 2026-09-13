"""Slide-change detection.

Runs the production detector over hand-built frames, so every failure mode seen
on a real lecture - handwriting, opaque pen strokes over text, cursors, laser
pointers, webcam overlays, crossfades, builds - is tested directly,
deterministically, and without decoding a video.

The scenarios named A-F match the real-world validation cases: A and B are the
annotation false positives that were reported, C and D the missed genuine
slides, E a mixture, F rapid navigation.
"""

import numpy as np
import pytest

import tests.slide_fixtures as F
from lecture_saver.slides.detector import (
    cell_activity,
    cell_change_map,
    classify_candidate,
    compute_noise_mask,
    detect_appearances,
)
from lecture_saver.slides.diagnostics import Candidate, Outcome
from lecture_saver.slides.global_analysis import compare_global
from lecture_saver.slides.ink_analysis import compare_ink, erode

_GLOBAL_GATES = {"low frequency change", "histogram change", "baseline similarity"}


def detect(frames, *, duration=None, options=None, sample_fps=1.0):
    options = options or F.options()
    d = F.descriptors(frames, options, sample_fps)
    if duration is None:
        duration = len(frames) / sample_fps
    return detect_appearances(
        d.thumbnails, d.times, d.hashes, duration=duration, options=options, ink=d
    )


def ink_of(frame, options=None):
    from lecture_saver.slides.descriptors import ink_mask

    options = options or F.options()
    return ink_mask(frame, options.sampling.ink_tolerance)


def compare(frame_a, frame_b, options=None):
    options = options or F.options()
    return compare_ink(ink_of(frame_a, options), ink_of(frame_b, options), options)


def thumb_of(frame, options=None):
    from lecture_saver.slides.descriptors import box_reduce

    options = options or F.options()
    return box_reduce(frame, max(1, F.WIDTH // options.sampling.thumb_width))


def classify(frame_a, frame_b, options=None):
    """Run the real decision and return (is_change, diagnostic record)."""
    options = options or F.options()
    record = Candidate(time=0.0)
    ink = compare(frame_a, frame_b, options)
    glob = compare_global(thumb_of(frame_a, options), thumb_of(frame_b, options), options)
    decided = classify_candidate(ink, glob, options, record)
    return decided, record


# --------------------------------------------------------------------------
# The shape signals
# --------------------------------------------------------------------------


class TestStrokeThickness:
    """The signal that fixes opaque handwriting being read as new slides."""

    def test_a_thick_pen_stroke_survives_erosion(self):
        base = F.slide(4)
        result = compare(base, F.with_pen(base, 3, thickness=5))
        assert result.thickness_added > 0.05

    def test_edited_text_does_not(self):
        result = compare(F.slide(1), F.slide(2))
        assert result.thickness_added < 0.05

    def test_erosion_removes_a_one_pixel_line(self):
        mask = np.zeros((40, 40), dtype=bool)
        mask[20, 5:35] = True
        assert not erode(mask).any()

    def test_erosion_keeps_the_core_of_a_thick_block(self):
        mask = np.zeros((40, 40), dtype=bool)
        mask[10:30, 10:30] = True
        assert erode(mask).sum() > 0.7 * mask.sum()

    def test_thickness_separates_pen_from_text_with_margin(self):
        base = F.slide(6)
        pen = compare(base, F.with_pen(base, 4, thickness=5)).thickness_added
        text = compare(F.slide(6), F.slide(7)).thickness_added
        assert pen > text * 3


class TestContentReplacement:
    def test_a_new_slide_destroys_the_previous_content(self):
        assert compare(F.slide(1), F.slide(2)).replaced > 0.15

    def test_annotation_leaves_the_content_in_place(self):
        base = F.slide(8)
        assert compare(base, F.with_highlight(base)).replaced < 0.15

    def test_an_addition_into_empty_space_has_no_support(self):
        titled = F.titled_slide()
        result = compare(F.with_bullets(titled, 1), F.with_bullets(titled, 3))
        assert result.replaced_support < F.options().normal_transition.min_replacement_support


# --------------------------------------------------------------------------
# The decision, with its diagnostics
# --------------------------------------------------------------------------


class TestClassification:
    def test_a_real_change_is_committed(self):
        decided, record = classify(F.slide(1), F.slide(2))
        assert decided
        assert record.path == "normal"
        # The strong-global gates are recorded for information and legitimately
        # fail here; what matters is that no *normal-path* gate failed.
        local = [g for g in record.gates if g.name not in _GLOBAL_GATES]
        assert all(gate.passed for gate in local)

    @pytest.mark.parametrize(
        ("name", "make"),
        [
            ("mouse cursor", F.with_cursor),
            ("laser pointer", F.with_laser),
            ("highlighted line", F.with_highlight),
            ("thin handwriting", lambda f: F.with_pen(f, 2, thickness=2)),
            ("opaque handwriting", lambda f: F.with_pen(f, 4, thickness=5)),
        ],
    )
    def test_distractors_never_become_slides(self, name, make):
        base = F.slide(4)
        decided, record = classify(base, make(base))
        assert not decided, f"{name} was treated as a slide change"
        assert record.reason, "a rejection must say which gate stopped it"

    def test_opaque_handwriting_is_rejected_by_thickness_specifically(self):
        """The reported real-world failure: opaque pen over existing text."""
        base = F.slide(9)
        _, record = classify(base, F.with_pen(base, 4, thickness=5))
        assert record.outcome == Outcome.ABSORBED
        assert record.reason == "annotation (thick strokes)"

    def test_every_gate_is_recorded_with_its_limit(self):
        _, record = classify(F.slide(1), F.slide(2))
        names = [gate.name for gate in record.gates]
        assert "ink change" in names
        assert "stroke thickness" in names
        assert "content replaced" in names
        for gate in record.gates:
            assert gate.limit >= 0
            assert gate.describe()

    def test_the_failing_gate_is_identifiable(self):
        base = F.slide(4)
        _, record = classify(base, F.with_cursor(base))
        failed = record.failed_gate
        assert failed is not None
        assert not failed.passed

    def test_identical_frames_report_no_ink_change(self):
        frame = F.slide(3)
        decided, record = classify(frame, frame)
        assert not decided
        assert record.reason == "no ink changed"


# --------------------------------------------------------------------------
# Detection over a sequence
# --------------------------------------------------------------------------


class TestNoChange:
    def test_a_static_lecture_is_one_slide(self):
        assert len(detect(F.hold(F.slide(1), 60)).appearances) == 1

    def test_the_single_slide_spans_the_whole_video(self):
        result = detect(F.hold(F.slide(1), 30), duration=30.0)
        assert result.appearances[0].start == 0.0
        assert result.appearances[0].end == pytest.approx(30.0)

    def test_a_moving_cursor_never_splits_a_slide(self):
        base = F.slide(2)
        frames = [F.with_cursor(base, 40 + (i * 7) % 200, 30 + (i * 11) % 120)
                  for i in range(90)]
        assert len(detect(frames).appearances) == 1

    def test_an_empty_sequence_yields_nothing(self):
        result = detect([])
        assert result.appearances == []
        assert result.candidates == []


class TestScenarioA:
    """One slide with progressive handwriting. Expected: ONE appearance."""

    @staticmethod
    def frames(thickness=3):
        out = F.hold(F.slide(20), 20)
        for step in range(1, 6):
            out += F.hold(F.with_pen(F.slide(20), step, thickness=thickness), 20)
        return out

    def test_thin_handwriting_stays_one_slide(self):
        assert len(detect(self.frames(thickness=2)).appearances) == 1

    def test_the_annotation_is_recorded_as_absorbed_not_ignored(self):
        result = detect(self.frames(thickness=3))
        assert len(result.appearances) == 1
        assert result.absorbed, "the marks should be seen and explained, not invisible"


class TestScenarioB:
    """Opaque annotation covering existing text. Expected: ONE appearance.

    This is the reported real-world false positive.
    """

    @staticmethod
    def frames():
        out = F.hold(F.slide(21), 20)
        for step in range(1, 6):
            out += F.hold(F.with_pen(F.slide(21), step, thickness=5), 20)
        return out

    def test_opaque_annotation_stays_one_slide(self):
        assert len(detect(self.frames()).appearances) == 1

    def test_it_is_the_thickness_gate_that_holds_it_together(self):
        result = detect(self.frames())
        reasons = result.rejection_summary()
        assert "annotation (thick strokes)" in reasons

    def test_a_real_change_after_the_annotation_is_still_found(self):
        frames = self.frames() + F.hold(F.slide(22), 30)
        assert len(detect(frames).appearances) == 2


class TestBoldSlidesAreNotAnnotation:
    """Bold headings and pictures survive erosion like a pen stroke does.

    On a real lecture five genuine slide changes were absorbed as "thick
    strokes". They replaced most of the previous content over a wide area,
    which annotation never does.
    """

    def test_changing_to_a_bold_slide_is_a_new_slide(self):
        decided, record = classify(F.slide(3), F.bold_slide(71))
        assert decided, record.describe()

    def test_one_bold_slide_to_another_is_a_new_slide(self):
        decided, record = classify(F.bold_slide(72), F.bold_slide(73))
        assert decided, record.describe()

    def test_it_is_the_thick_content_override_that_lets_it_through(self):
        before, after = F.bold_slide(72), F.bold_slide(73)
        _, record = classify(before, after)
        gates = {gate.name: gate.passed for gate in record.gates}
        assert gates["stroke thickness"] is False
        assert gates["thick content replaced"] is True

        strict = F.options(**{"annotation_filter.thick_content_min_replaced": 1.01})
        decided, record = classify(before, after, strict)
        assert not decided
        assert record.reason == "annotation (thick strokes)"

    def test_opaque_pen_on_a_bold_slide_is_still_annotation(self):
        base = F.bold_slide(75)
        decided, record = classify(base, F.with_pen(base, 4, thickness=5))
        assert not decided
        assert record.reason == "annotation (thick strokes)"

    def test_a_wide_pen_scribble_that_replaced_nothing_is_still_annotation(self):
        base = F.slide(76)
        decided, record = classify(base, F.with_pen(base, 6, thickness=5))
        assert not decided
        assert record.reason == "annotation (thick strokes)"

    def test_a_sequence_of_annotated_bold_slides_is_split_correctly(self):
        frames = []
        for seed in (80, 81, 82):
            base = F.bold_slide(seed)
            frames += F.hold(base, 20)
            frames += F.hold(F.with_pen(base, 3, thickness=5), 20)
        result = detect(frames)
        assert [round(a.start) for a in result.appearances] == [0, 40, 80]


class TestScenarioC:
    """Same template, one text block differs. Expected: TWO appearances."""

    @staticmethod
    def one_block_rewritten():
        """Two slides on one template, differing in a single line of text."""
        base = F.titled_slide()
        first = F.with_bullets(base, 3)
        second = first.copy()
        second[104:106, :] = 240                     # erase the third bullet
        F.text_line(second, 104, seed=987, left=32)  # write a different one
        return first, second

    def test_a_single_changed_block_is_detected(self):
        first, second = self.one_block_rewritten()
        frames = F.hold(first, 30) + F.hold(second, 30)
        assert len(detect(frames).appearances) == 2

    def test_a_change_confined_to_one_row_is_not_rejected_for_that(self):
        """Gating on rows is what made real single-line changes disappear."""
        first, second = self.one_block_rewritten()
        assert compare(first, second).rows <= 2
        decided, _ = classify(first, second)
        assert decided


class TestScenarioE:
    """Cursor + handwriting + webcam, then a genuine change. Expected: TWO."""

    def test_only_the_real_change_creates_a_slide(self):
        base = F.slide(30)
        frames = []
        for tick in range(40):
            annotated = F.with_pen(base, 1 + tick // 15, thickness=4)
            frames.append(F.with_cursor(F.with_webcam(annotated, tick),
                                        40 + tick * 4, 60))
        for tick in range(30):
            frames.append(F.with_webcam(F.slide(31), 100 + tick))
        assert len(detect(frames).appearances) == 2


class TestIgnoredChanges:
    def test_a_highlight_appearing_does_not_create_a_slide(self):
        base = F.slide(6)
        frames = F.hold(base, 20) + F.hold(F.with_highlight(base), 20)
        assert len(detect(frames).appearances) == 1

    def test_a_laser_pointer_sweeping_does_not_create_slides(self):
        base = F.slide(8)
        frames = F.hold(base, 10)
        frames += [F.with_laser(base, 10 + step * 3, 10 + (step % 60))
                   for step in range(60)]
        assert len(detect(frames).appearances) == 1

    def test_a_webcam_overlay_alone_does_not_create_slides(self):
        base = F.slide(12)
        assert len(detect([F.with_webcam(base, t) for t in range(120)]).appearances) == 1

    def test_slides_are_still_found_underneath_a_webcam_overlay(self):
        frames = [F.with_webcam(F.slide(13), t) for t in range(60)]
        frames += [F.with_webcam(F.slide(14), 60 + t) for t in range(60)]
        assert len(detect(frames).appearances) == 2


class TestTransitionsAndDebounce:
    def test_a_crossfade_produces_one_change_not_several(self):
        first, second = F.slide(18), F.slide(19)
        frames = F.hold(first, 20)
        frames += [F.blend(first, second, a) for a in (0.25, 0.5, 0.75)]
        frames += F.hold(second, 20)
        assert len(detect(frames).appearances) == 2

    def test_a_build_animation_does_not_split_a_slide(self):
        base = F.titled_slide()
        frames = F.hold(F.with_bullets(base, 1), 20)
        for step in (2, 3, 4):
            frames += F.hold(F.with_bullets(base, step), 6)
        frames += F.hold(F.with_bullets(base, 4), 20)
        assert len(detect(frames).appearances) == 1

    def test_debounce_enforces_a_minimum_gap(self):
        options = F.options(**{'timing.min_slide_seconds': 8.0})
        frames = []
        for seed in range(40, 48):
            frames += F.hold(F.slide(seed), 2)
        frames += F.hold(F.slide(49), 20)

        appearances = detect(frames, options=options).appearances
        for earlier, later in zip(appearances, appearances[1:], strict=False):
            assert later.start - earlier.start >= options.timing.min_slide_seconds - 1e-6

    def test_a_change_in_the_final_frame_is_not_committed(self):
        frames = F.hold(F.slide(27), 20) + [F.slide(28)]
        assert len(detect(frames).appearances) == 1


class TestBoundaries:
    def test_intervals_are_contiguous_and_ordered(self):
        frames = F.hold(F.slide(1), 20) + F.hold(F.slide(2), 20) + F.hold(F.slide(3), 20)
        appearances = detect(frames, duration=60.0).appearances

        assert appearances[0].start == 0.0
        for earlier, later in zip(appearances, appearances[1:], strict=False):
            assert earlier.end == later.start
            assert earlier.start < earlier.end
        assert appearances[-1].end == pytest.approx(60.0)

    def test_ids_are_sequential_from_one(self):
        frames = F.hold(F.slide(1), 20) + F.hold(F.slide(2), 20) + F.hold(F.slide(3), 20)
        assert [a.appearance_id for a in detect(frames).appearances] == [1, 2, 3]

    def test_a_returning_slide_is_a_separate_appearance_for_now(self):
        frames = F.hold(F.slide(1), 20) + F.hold(F.slide(2), 20) + F.hold(F.slide(1), 20)
        appearances = detect(frames).appearances
        assert len(appearances) == 3
        assert len({a.slide_id for a in appearances}) == 3


class TestNoiseMask:
    def test_a_busy_corner_is_masked_out(self):
        base = F.slide(15)
        d = F.descriptors([F.with_webcam(base, tick) for tick in range(120)])
        mask = compute_noise_mask(d.thumbnails, F.options())
        assert mask.any()
        assert mask[-1, -1]

    def test_a_static_lecture_masks_nothing(self):
        d = F.descriptors(F.hold(F.slide(16), 90))
        assert not compute_noise_mask(d.thumbnails, F.options()).any()

    def test_masking_needs_enough_samples(self):
        options = F.options(**{'busy_region_mask.min_samples_for_masking': 60})
        d = F.descriptors([F.with_webcam(F.slide(17), t) for t in range(10)])
        assert not compute_noise_mask(d.thumbnails, options).any()

    def test_masking_never_swallows_the_whole_grid(self):
        options = F.options()
        d = F.descriptors([F.slide(100 + tick) for tick in range(120)])
        mask = compute_noise_mask(d.thumbnails, options)
        assert int(mask.sum()) <= options.busy_region_mask.max_masked_cell_fraction * mask.size


class TestCellMap:
    def test_marks_only_the_cells_that_changed(self):
        options = F.options()
        base = F.descriptors([F.blank()]).thumbnails[0]
        changed = base.copy()
        cell_h = base.shape[0] // options.sampling.grid_rows
        cell_w = base.shape[1] // options.sampling.grid_cols
        changed[:cell_h, :cell_w] = 0

        cells = cell_change_map(base, changed, options)
        assert cells[0, 0]
        assert int(cells.sum()) == 1


class TestDiagnostics:
    def test_every_committed_slide_has_a_candidate_record(self):
        frames = F.hold(F.slide(1), 20) + F.hold(F.slide(2), 20)
        result = detect(frames)
        # The first appearance starts the video and is not a "change".
        assert len(result.committed) == len(result.appearances) - 1

    def test_the_summary_counts_reasons(self):
        base = F.slide(21)
        frames = F.hold(base, 20) + F.hold(F.with_pen(base, 4, thickness=5), 40)
        summary = detect(frames).rejection_summary()
        assert summary
        assert sum(summary.values()) >= 1

    def test_a_candidate_serialises_for_logging(self):
        _, record = classify(F.slide(1), F.slide(2))
        data = record.to_dict()
        assert data["outcome"] in {"committed", "rejected", "absorbed"}
        assert data["gates"]


class TestDeterminism:
    def test_the_same_input_always_gives_the_same_result(self):
        frames = F.hold(F.slide(40), 20) + F.hold(F.slide(41), 20)
        first = [a.to_dict() for a in detect(frames).appearances]
        second = [a.to_dict() for a in detect(frames).appearances]
        assert first == second

    def test_detection_works_without_ink_masks(self):
        """Caches written before ink masks existed must still load and run."""
        frames = F.hold(F.slide(1), 20) + F.hold(F.slide(2), 20)
        d = F.descriptors(frames)
        result = detect_appearances(
            d.thumbnails, d.times, d.hashes, duration=40.0, options=F.options()
        )
        assert len(result.appearances) >= 1


# --------------------------------------------------------------------------
# The strong-global transition path
# --------------------------------------------------------------------------


class TestStrongGlobalTransition:
    """An obvious whole-screen change must never be blocked by a local gate."""

    def test_a_full_screen_change_is_committed(self):
        before, after = F.slide(50), F.add_full_screen_change(F.slide(50))
        decided, record = classify(before, after)
        assert decided
        assert record.path == "strong global"

    def test_it_bypasses_content_replacement(self):
        """The gate that caused the reported false negatives."""
        options = F.options(**{"normal_transition.min_content_replaced": 0.99})
        before, after = F.slide(51), F.add_full_screen_change(F.slide(51))
        decided, _ = classify(before, after, options)
        assert decided

    def test_it_bypasses_annotation_suppression(self):
        """Leaving an annotated slide is itself a strong global change."""
        options = F.options(**{"annotation_filter.max_stroke_thickness": 0.0})
        before, after = F.slide(52), F.add_full_screen_change(F.slide(52))
        decided, _ = classify(before, after, options)
        assert decided

    def test_annotation_never_triggers_it(self):
        base = F.slide(53)
        for strokes in (1, 3, 5):
            _, record = classify(base, F.with_pen(base, strokes, thickness=5))
            assert record.path == "normal", f"{strokes} strokes looked global"

    def test_it_can_be_switched_off(self):
        options = F.options(**{"strong_global_transition.enabled": False})
        before, after = F.slide(54), F.add_full_screen_change(F.slide(54))
        _, record = classify(before, after, options)
        assert record.path == "normal"

    def test_a_full_screen_change_still_needs_stability(self):
        """A single flash frame is not a slide."""
        base = F.slide(55)
        frames = F.hold(base, 20) + [F.add_full_screen_change(base)] + F.hold(base, 20)
        assert len(detect(frames).appearances) == 1

    def test_it_records_a_confidence(self):
        before, after = F.slide(56), F.add_full_screen_change(F.slide(56))
        _, record = classify(before, after)
        assert record.confidence is not None
        assert 0.0 < record.confidence <= 1.0


class TestEmbeddedVideo:
    """A video playing inside a slide must not produce a slide per frame."""

    def test_it_does_not_create_slides(self):
        base = F.slide(60)
        frames = [F.embedded_video(base, tick) for tick in range(120)]
        assert len(detect(frames).appearances) == 1

    def test_the_busy_mask_covers_it(self):
        base = F.slide(61)
        d = F.descriptors([F.embedded_video(base, tick) for tick in range(120)])
        mask = compute_noise_mask(d.thumbnails, F.options())
        assert mask.any()

    def test_a_real_change_underneath_is_still_found(self):
        frames = [F.embedded_video(F.slide(62), t) for t in range(60)]
        frames += [F.embedded_video(F.slide(63), 60 + t) for t in range(60)]
        assert len(detect(frames).appearances) == 2


class TestBusyRegionAudit:
    def test_the_report_counts_and_maps_the_mask(self):
        base = F.slide(70)
        result = detect([F.with_webcam(base, tick) for tick in range(120)])

        assert result.busy.total_cells == F.options().cell_count
        assert len(result.busy.activity) == F.options().sampling.grid_rows
        assert result.busy.describe()

    def test_a_static_lecture_masks_nothing_and_does_not_warn(self):
        result = detect(F.hold(F.slide(71), 90))
        assert result.busy.masked_cells == 0
        assert result.busy.warn is False

    def test_it_warns_when_a_large_share_is_hidden(self):
        options = F.options(**{"busy_region_mask.warn_masked_fraction": 0.01})
        base = F.slide(72)
        result = detect([F.with_webcam(base, tick) for tick in range(120)],
                        options=options)
        assert result.busy.warn is True
        assert "WARNING" in result.busy.describe()

    def test_activity_is_reported_per_cell(self):
        base = F.slide(73)
        d = F.descriptors([F.with_webcam(base, tick) for tick in range(90)])
        activity = cell_activity(d.thumbnails, F.options())
        assert activity.shape == (F.options().sampling.grid_rows,
                                  F.options().sampling.grid_cols)
        assert activity.max() > 0.5   # the overlay corner
        assert activity.min() == 0.0  # the static slide body


class TestNearMissRanking:
    def test_strongest_rejected_is_sorted_and_excludes_noise(self):
        base = F.slide(80)
        frames = F.hold(base, 20) + F.hold(F.with_pen(base, 4, thickness=5), 40)
        near = detect(frames).strongest_rejected(limit=5)

        assert near
        assert all(c.reason != "no ink changed" for c in near)
        strengths = [c.strength for c in near]
        assert strengths == sorted(strengths, reverse=True)


class TestProvenance:
    def test_committed_appearances_record_how_they_were_found(self):
        frames = F.hold(F.slide(90), 20) + F.hold(F.slide(91), 20)
        appearances = detect(frames).appearances

        assert appearances[0].detection_reason is None      # opens the video
        assert appearances[1].detection_reason == "content replaced"
        assert appearances[1].detection_confidence is not None

    def test_a_strong_global_change_says_so(self):
        base = F.slide(92)
        frames = F.hold(base, 20) + F.hold(F.add_full_screen_change(base), 20)
        appearances = detect(frames).appearances
        assert appearances[1].detection_reason == "strong global transition"
