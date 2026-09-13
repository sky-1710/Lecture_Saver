"""Heavily annotated slides are kept both clean and annotated.

Runs the production detector and snapshot pass over hand-built frames, so the
split points, the captured moments and the erase handling are all tested
without decoding a video.
"""

import numpy as np

import tests.slide_fixtures as F
from lecture_saver.slides.annotation_snapshots import add_annotation_snapshots
from lecture_saver.slides.detector import detect_appearances


def snapshots(frames, options=None):
    options = options or F.options()
    d = F.descriptors(frames, options)
    result = detect_appearances(
        d.thumbnails, d.times, d.hashes, duration=float(len(frames)),
        options=options, ink=d,
    )
    return result, add_annotation_snapshots(result, d, options)


def writing(base, strokes, seconds=10, thickness=2):
    """The professor writing progressively more on ``base``."""
    out = []
    for step in range(1, strokes + 1):
        out += F.hold(F.with_pen(base, step, thickness=thickness), seconds)
    return out


def summary(result):
    return [
        (round(a.start), round(a.end), a.annotation_state)
        for a in result.appearances
    ]


def captured(frames, result, position):
    """The frame the image for ``position`` would be taken from."""
    return frames[int(round(result.representative_times[position]))]


class TestSlidesWithoutNotes:
    def test_an_ordinary_lecture_is_unchanged(self):
        frames = F.hold(F.slide(1), 30) + F.hold(F.slide(2), 30)
        detected, saved = snapshots(frames)
        assert saved.appearances == detected.appearances
        assert saved.representative_times == detected.representative_times

    def test_a_cursor_is_not_notes(self):
        base = F.slide(3)
        frames = F.hold(base, 20) + [F.with_cursor(base) for _ in range(20)]
        _, saved = snapshots(frames)
        assert summary(saved) == [(0, 40, None)]

    def test_it_can_be_switched_off(self):
        base = F.slide(4)
        frames = F.hold(base, 20) + writing(base, 5)
        options = F.options(**{"annotation_snapshots.enabled": False})
        detected, saved = snapshots(frames, options)
        assert saved is detected


class TestBeforeAndAfterNotes:
    base = F.slide(21)

    def frames(self):
        return F.hold(self.base, 20) + writing(self.base, 5) + F.hold(F.slide(22), 30)

    def test_the_slide_is_split_where_the_writing_starts(self):
        _, saved = snapshots(self.frames())
        assert summary(saved) == [
            (0, 20, "before_annotation"),
            (20, 70, "annotated"),
            (70, 100, None),
        ]

    def test_the_first_image_is_the_clean_slide(self):
        frames = self.frames()
        _, saved = snapshots(frames)
        assert np.array_equal(captured(frames, saved, 0), self.base)

    def test_the_second_image_is_the_fullest_board(self):
        frames = self.frames()
        _, saved = snapshots(frames)
        assert np.array_equal(captured(frames, saved, 1), F.with_pen(self.base, 5, thickness=2))

    def test_ids_are_sequential_and_intervals_contiguous(self):
        _, saved = snapshots(self.frames())
        assert [a.appearance_id for a in saved.appearances] == [1, 2, 3]
        assert [a.slide_id for a in saved.appearances] == [1, 2, 3]
        for before, after in zip(saved.appearances, saved.appearances[1:], strict=False):
            assert before.end == after.start
        assert saved.appearances[0].start == 0.0

    def test_the_annotated_half_says_where_it_came_from(self):
        _, saved = snapshots(self.frames())
        assert saved.appearances[1].detection_reason == "annotation started"
        assert saved.appearances[1].annotation_label

    def test_writing_from_the_first_moment_is_one_annotated_slide(self):
        frames = F.hold(self.base, 2) + writing(self.base, 5)
        _, saved = snapshots(frames)
        assert summary(saved) == [(0, 52, "annotated")]

    def test_bold_slides_with_notes_are_split_too(self):
        bold = F.bold_slide(90)
        frames = F.hold(bold, 20) + writing(bold, 4, thickness=3) + F.hold(F.bold_slide(91), 20)
        _, saved = snapshots(frames)
        assert [a.annotation_state for a in saved.appearances] == [
            "before_annotation", "annotated", None,
        ]

    def test_the_transition_into_the_next_slide_is_not_the_fullest_board(self):
        """A frame already showing the next slide must not be captured."""
        next_slide = F.add_full_screen_change(F.slide(23))
        frames = (
            F.hold(self.base, 20) + writing(self.base, 5)
            + [F.blend(F.with_pen(self.base, 5, thickness=2), next_slide, 0.6)]
            + F.hold(next_slide, 30)
        )
        _, saved = snapshots(frames)
        annotated = next(
            i for i, a in enumerate(saved.appearances) if a.annotation_state == "annotated"
        )
        assert saved.representative_times[annotated] < 70


class TestErasing:
    base = F.slide(31)

    def test_the_board_before_erasing_is_kept(self):
        frames = F.hold(self.base, 20) + writing(self.base, 5) + F.hold(self.base, 30)
        _, saved = snapshots(frames)
        assert summary(saved) == [
            (0, 20, "before_annotation"),
            (20, 70, "before_erase"),
            (70, 100, None),
        ]
        assert np.array_equal(captured(frames, saved, 1), F.with_pen(self.base, 5, thickness=2))

    def test_writing_again_is_not_a_second_clean_slide(self):
        frames = (
            F.hold(self.base, 20) + writing(self.base, 5)
            + F.hold(self.base, 30) + writing(self.base, 3)
        )
        _, saved = snapshots(frames)
        assert summary(saved) == [
            (0, 20, "before_annotation"),
            (20, 70, "before_erase"),
            (70, 130, "annotated"),
        ]
        assert np.array_equal(captured(frames, saved, 2), F.with_pen(self.base, 3, thickness=2))

    def test_moving_to_a_different_slide_is_not_erasing(self):
        frames = F.hold(self.base, 20) + writing(self.base, 5) + F.hold(F.slide(32), 30)
        _, saved = snapshots(frames)
        assert "before_erase" not in [a.annotation_state for a in saved.appearances]
