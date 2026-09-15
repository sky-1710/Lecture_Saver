"""Transcript-to-slide alignment (§10, §33).

The rule is small but everything downstream - the slide view, the PDF, the JSON
export - trusts it, so the boundary cases are pinned down here.
"""

import time

import pytest

from lecture_saver.core.alignment import align, assign_segments, snapped_starts
from lecture_saver.core.models import TranscriptSegment
from lecture_saver.core.slides import SlideAppearance


def seg(start, end, index=1):
    return TranscriptSegment(id=f"segment_{index:03d}", start=start, end=end, original_text="x")


def slides(*bounds):
    """Contiguous appearances from boundary times: slides(0, 30, 60) -> 2 slides."""
    return [
        SlideAppearance(appearance_id=i + 1, slide_id=i + 1, start=a, end=b)
        for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
    ]


class TestAssignment:
    def test_a_segment_inside_a_slide_belongs_to_it(self):
        assert assign_segments([seg(35, 40)], slides(0, 30, 60)) == [1]

    def test_the_example_from_the_specification(self):
        """Slide 8 on screen 12:31-14:05; three sentences said during it."""
        appearances = slides(0, 751, 845.2, 900)
        segments = [seg(754, 760, 1), seg(772, 778, 2), seg(801, 806, 3)]
        assert assign_segments(segments, appearances) == [1, 1, 1]

    def test_a_segment_crossing_a_boundary_goes_where_it_overlaps_most(self):
        # 28-40 overlaps slide 1 for 2 s and slide 2 for 10 s.
        assert assign_segments([seg(28, 40)], slides(0, 30, 60)) == [1]
        # 20-32 overlaps slide 1 for 10 s and slide 2 for 2 s.
        assert assign_segments([seg(20, 32)], slides(0, 30, 60)) == [0]

    def test_an_exact_tie_stays_with_the_earlier_slide(self):
        assert assign_segments([seg(25, 35)], slides(0, 30, 60)) == [0]

    def test_a_segment_spanning_three_slides_goes_to_the_longest_overlap(self):
        assert assign_segments([seg(8, 32)], slides(0, 10, 25, 60)) == [1]

    def test_a_segment_starting_exactly_on_a_boundary_belongs_to_the_new_slide(self):
        assert assign_segments([seg(30, 34)], slides(0, 30, 60)) == [1]

    def test_a_segment_ending_exactly_on_a_boundary_belongs_to_the_old_slide(self):
        assert assign_segments([seg(26, 30)], slides(0, 30, 60)) == [0]

    def test_a_segment_after_the_last_slide_belongs_to_the_last_slide(self):
        assert assign_segments([seg(70, 75)], slides(0, 30, 60)) == [1]

    def test_a_segment_in_a_gap_belongs_to_the_slide_before_it(self):
        appearances = [
            SlideAppearance(1, 1, 0.0, 20.0),
            SlideAppearance(2, 2, 40.0, 60.0),
        ]
        assert assign_segments([seg(25, 30)], appearances) == [0]

    def test_a_zero_length_segment_belongs_to_the_slide_covering_it(self):
        assert assign_segments([seg(45, 45)], slides(0, 30, 60)) == [1]

    def test_no_slides_means_no_assignment(self):
        assert assign_segments([seg(0, 5), seg(5, 9)], []) == [None, None]

    def test_no_segments_is_fine(self):
        assert assign_segments([], slides(0, 30)) == []

    def test_every_segment_is_assigned_exactly_once(self):
        appearances = slides(0, 12.5, 40, 41, 90, 200)
        segments = [seg(t, t + 4.5, i) for i, t in enumerate(range(0, 200, 3))]
        assigned = assign_segments(segments, appearances)
        assert len(assigned) == len(segments)
        assert all(a is not None for a in assigned)

    def test_long_lectures_align_quickly(self):
        """Three hours: ~2,500 segments and several hundred slide changes."""
        appearances = slides(*[float(t) for t in range(0, 10_801, 20)])
        segments = [seg(t * 4.3, t * 4.3 + 4.0, t) for t in range(2_500)]
        began = time.perf_counter()
        assign_segments(segments, appearances)
        assert time.perf_counter() - began < 1.0


class TestGrouping:
    def test_groups_are_in_time_order_per_slide(self):
        segments = [seg(1, 4, 1), seg(5, 9, 2), seg(31, 35, 3), seg(40, 45, 4)]
        result = align(segments, slides(0, 30, 60))
        assert result.by_appearance == ((0, 1), (2, 3))
        assert result.by_segment == (0, 0, 1, 1)

    def test_a_slide_nobody_spoke_over_has_an_empty_group(self):
        result = align([seg(1, 4), seg(61, 65, 2)], slides(0, 30, 60, 90))
        assert result.segments_for(1) == ()

    def test_out_of_range_lookups_are_safe(self):
        result = align([seg(1, 4)], slides(0, 30))
        assert result.segments_for(5) == ()
        assert result.appearance_for(9) is None

    def test_annotated_slide_versions_split_the_speech_by_time(self):
        """A slide saved before and with notes gets what was said during each."""
        appearances = [
            SlideAppearance(1, 1, 0.0, 20.0, annotation_state="before_annotation"),
            SlideAppearance(2, 2, 20.0, 70.0, annotation_state="annotated"),
        ]
        segments = [seg(3, 9, 1), seg(12, 19, 2), seg(22, 30, 3), seg(50, 60, 4)]
        assert align(segments, appearances).by_appearance == ((0, 1), (2, 3))


@pytest.mark.parametrize("start,end", [(0, 1), (29.9, 30.1), (59, 61)])
def test_the_result_is_deterministic(start, end):
    appearances = slides(0, 30, 60)
    assert assign_segments([seg(start, end)], appearances) == assign_segments(
        [seg(start, end)], appearances
    )


def spoken(start, end, text, index=1):
    return TranscriptSegment(
        id=f"segment_{index:03d}", start=start, end=end, original_text=text
    )


class TestBoundariesFollowTheSentences:
    """A slide changes mid-sentence; the speech must not be cut in half (§10)."""

    #: "The lecturer keeps going / and finishes the idea." is one sentence over
    #: two lines, and the slide changes 4 s before that sentence ends.
    SEGMENTS = [
        spoken(0.0, 10.0, "First thought here.", 1),
        spoken(10.0, 20.0, "The lecturer keeps going", 2),
        spoken(20.0, 30.0, "and finishes the idea.", 3),
        spoken(30.0, 40.0, "A new thought begins.", 4),
    ]

    def test_a_sentence_running_over_the_change_stays_on_one_slide(self):
        assert assign_segments(self.SEGMENTS, slides(0, 24, 40)) == [0, 0, 0, 1]

    def test_without_snapping_it_is_split_down_the_middle(self):
        assert assign_segments(self.SEGMENTS, slides(0, 24, 40), snap=0) == [0, 0, 1, 1]

    def test_the_boundary_only_moves_as_far_as_a_real_break(self):
        starts = snapped_starts(self.SEGMENTS, slides(0, 24, 40))
        assert starts == [0.0, 30.0]

    def test_a_change_far_from_any_sentence_end_is_left_where_it_is(self):
        # Sentences end at 10 s and 30 s; a change at 17 s is out of reach of
        # both, so it stays where the detector put it.
        starts = snapped_starts(self.SEGMENTS, slides(0, 17, 40))
        assert starts == [0.0, 17.0]

    def test_a_change_inside_a_sentence_moves_back_to_where_it_began(self):
        # 15 s is inside the sentence that starts at 10 s: the whole sentence
        # belongs to the slide that was up while it was said.
        assert snapped_starts(self.SEGMENTS, slides(0, 15, 40)) == [0.0, 10.0]

    def test_a_short_slide_cannot_be_emptied_by_the_snap(self):
        # A two-second appearance: the boundary may move at most one second,
        # so the speech said over it stays with it.
        appearances = slides(0, 28, 30, 40)
        starts = snapped_starts(self.SEGMENTS, appearances)
        assert starts[1] == 28.0
        assert 28.0 in [a.start for a in appearances]

    def test_snapped_starts_stay_in_order(self):
        segments = [
            spoken(0.0, 5.0, "One.", 1),
            spoken(5.0, 10.0, "Two.", 2),
            spoken(10.0, 15.0, "Three.", 3),
            spoken(15.0, 20.0, "Four.", 4),
        ]
        starts = snapped_starts(segments, slides(0, 6, 9, 12, 20))
        assert starts == sorted(starts)

    def test_speech_is_never_left_unassigned(self):
        assignment = assign_segments(self.SEGMENTS, slides(0, 24, 40))
        assert all(index is not None for index in assignment)
