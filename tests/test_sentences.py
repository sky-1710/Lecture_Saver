"""Where a line can be cut so sentences survive a slide change (§10, §33)."""

from lecture_saver.core.models import TranscriptSegment
from lecture_saver.core.sentences import (
    cut_offset,
    cuts_for_boundaries,
    sentence_offsets,
)


def seg(start, end, text, index=1):
    return TranscriptSegment(
        id=f"segment_{index:03d}", start=start, end=end, original_text=text
    )


class TestSentenceOffsets:
    def test_it_finds_the_end_of_each_sentence_inside_a_line(self):
        text = "One thing. Then another. And a third"
        assert [text[:o] for o in sentence_offsets(text)] == [
            "One thing.",
            "One thing. Then another.",
        ]

    def test_the_end_of_the_line_is_not_a_place_to_cut(self):
        assert sentence_offsets("A single finished sentence.") == []

    def test_decimals_and_versions_are_not_sentence_ends(self):
        assert sentence_offsets("The value 0.5 is used here") == []

    def test_question_and_exclamation_marks_count(self):
        text = "Is that clear? Good. Moving on"
        assert len(sentence_offsets(text)) == 2

    def test_a_closing_quote_after_the_full_stop_stays_with_it(self):
        text = 'He said "wait." Then he left'
        offset = sentence_offsets(text)[0]
        assert text[:offset] == 'He said "wait."'

    def test_korean_sentences(self):
        text = "여기서 러닝레이트를 보시면 됩니다. 값이 너무 크면 발산합니다"
        assert len(sentence_offsets(text)) == 1


class TestCutOffset:
    def test_it_picks_the_sentence_end_nearest_the_slide_change(self):
        # 20 characters over 10 s: the full stop at 10 chars is at about 5 s.
        segment = seg(0.0, 10.0, "Ends here. Goes on!!")
        assert cut_offset(segment, 5.0) == 10

    def test_a_sentence_end_too_far_from_the_change_is_left_alone(self):
        segment = seg(0.0, 60.0, "Ends here. Goes on for a very long time indeed and on")
        assert cut_offset(segment, 55.0) is None

    def test_a_line_without_a_sentence_end_cannot_be_cut(self):
        assert cut_offset(seg(0.0, 10.0, "no end in sight at all"), 5.0) is None


class TestCutsForBoundaries:
    def test_only_lines_a_slide_change_runs_through_are_cut(self):
        segments = [
            seg(0.0, 10.0, "Done. Next one", 1),
            seg(10.0, 20.0, "Untouched. Still untouched", 2),
        ]
        # The change falls inside the first line only.
        assert cuts_for_boundaries(segments, [3.0]) == [("segment_001", 5)]

    def test_a_change_between_lines_needs_no_cut(self):
        segments = [seg(0.0, 10.0, "One. Two", 1), seg(10.0, 20.0, "Three. Four", 2)]
        assert cuts_for_boundaries(segments, [10.0]) == []

    def test_one_cut_per_line_even_with_two_changes_inside_it(self):
        segments = [seg(0.0, 20.0, "First part. Second part. Third part", 1)]
        cuts = cuts_for_boundaries(segments, [6.0, 7.0])
        assert len(cuts) == 1

    def test_nothing_to_do_is_an_empty_list(self):
        assert cuts_for_boundaries([], [10.0]) == []
        assert cuts_for_boundaries([seg(0.0, 5.0, "Words here")], []) == []
