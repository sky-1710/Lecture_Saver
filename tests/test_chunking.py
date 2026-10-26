"""Packing speech into Whisper windows, and mapping times back (§26, §33)."""

import pytest

from lecture_saver.transcription.chunking import WINDOW_SECONDS, Window, plan_windows


class TestPlanWindows:
    def test_regions_are_packed_up_to_a_window(self):
        windows = plan_windows([(0.0, 10.0), (12.0, 22.0), (25.0, 35.0), (40.0, 45.0)])
        # 10 + 10 + 10 fills the first window; the 4th region starts the next.
        assert [w.pieces for w in windows] == [
            ((0.0, 10.0), (12.0, 22.0), (25.0, 35.0)),
            ((40.0, 45.0),),
        ]

    def test_no_window_holds_more_than_its_length_of_audio(self):
        regions = [(i * 7.0, i * 7.0 + 6.5) for i in range(40)]
        for window in plan_windows(regions):
            assert window.duration <= WINDOW_SECONDS

    def test_a_region_is_kept_whole_when_it_fits(self):
        windows = plan_windows([(0.0, 20.0), (21.0, 41.0)])
        assert [w.pieces for w in windows] == [((0.0, 20.0),), ((21.0, 41.0),)]

    def test_an_overlong_region_is_split(self):
        windows = plan_windows([(100.0, 170.0)])
        assert [w.pieces for w in windows] == [
            ((100.0, 130.0),),
            ((130.0, 160.0),),
            ((160.0, 170.0),),
        ]

    def test_empty_and_backwards_regions_are_ignored(self):
        assert plan_windows([(5.0, 5.0), (9.0, 3.0)]) == []
        assert plan_windows([]) == []


class TestWindowTimes:
    WINDOW = Window(((10.0, 20.0), (50.0, 55.0)))

    def test_span_and_duration(self):
        assert (self.WINDOW.start, self.WINDOW.end) == (10.0, 55.0)
        assert self.WINDOW.duration == 15.0

    @pytest.mark.parametrize(
        ("inside", "lecture"),
        [
            (0.0, 10.0),
            (4.5, 14.5),
            (10.0, 20.0),  # the junction belongs to the end of the first piece
            (10.5, 50.5),  # the silence between pieces is skipped over
            (15.0, 55.0),
        ],
    )
    def test_window_time_maps_onto_the_lecture(self, inside, lecture):
        assert self.WINDOW.to_source(inside) == pytest.approx(lecture)

    def test_a_time_past_the_audio_is_held_at_the_end(self):
        assert self.WINDOW.to_source(29.98) == 55.0
