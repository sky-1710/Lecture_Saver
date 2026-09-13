import pytest

from lecture_saver.core.timecode import (
    format_clock,
    format_duration,
    format_hms,
    format_range,
    format_srt,
    format_vtt,
    parse_timestamp,
)


class TestFormatHms:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (0, "00:00:00"),
            (7.4, "00:00:07"),
            (59, "00:00:59"),
            (60, "00:01:00"),
            (751.0, "00:12:31"),
            (3600, "01:00:00"),
            (3661.9, "01:01:01"),  # truncates, matching the player clock
            (36000, "10:00:00"),
        ],
    )
    def test_values(self, seconds, expected):
        assert format_hms(seconds) == expected

    def test_carry_does_not_produce_sixty_seconds(self):
        # 59.9996 rounds to 60.000 s; it must roll over into the next minute
        # rather than emitting the invalid 00:00:60.
        assert format_hms(59.9996) == "00:01:00"
        assert format_srt(59.9996) == "00:01:00,000"

    def test_negative_and_nan_clamp_to_zero(self):
        assert format_hms(-5) == "00:00:00"
        assert format_hms(float("nan")) == "00:00:00"
        assert format_hms(float("inf")) == "00:00:00"


class TestSubtitleFormats:
    def test_srt_uses_comma(self):
        assert format_srt(12.345) == "00:00:12,345"

    def test_vtt_uses_period(self):
        assert format_vtt(12.345) == "00:00:12.345"

    def test_milliseconds_are_rounded_not_truncated(self):
        assert format_srt(1.2349) == "00:00:01,235"
        assert format_vtt(0.0004) == "00:00:00.000"

    def test_hours_are_zero_padded_to_two_digits(self):
        assert format_srt(3600) == "01:00:00,000"


class TestFormatClock:
    def test_omits_hours_under_an_hour(self):
        assert format_clock(75) == "01:15"

    def test_includes_hours_past_an_hour(self):
        assert format_clock(3675) == "1:01:15"


def test_format_range():
    assert format_range(751.0, 845.2) == "00:12:31 - 00:14:05"


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(9, "9 s"), (90, "1 min 30 s"), (4320, "1 h 12 min")],
    )
    def test_values(self, seconds, expected):
        assert format_duration(seconds) == expected


class TestParseTimestamp:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12", 12.0),
            ("01:15", 75.0),
            ("00:12:31", 751.0),
            ("1:01:15", 3675.0),
            ("00:00:12.345", 12.345),
            ("00:00:12,345", 12.345),
            ("  00:01:00  ", 60.0),
        ],
    )
    def test_valid(self, text, expected):
        assert parse_timestamp(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "abc", "1:2:3:4", "-5", "00:-1:00", "1.5:00"])
    def test_invalid_raises(self, text):
        with pytest.raises(ValueError):
            parse_timestamp(text)

    @pytest.mark.parametrize("seconds", [0.0, 12.345, 751.0, 3675.5])
    def test_round_trip_through_srt(self, seconds):
        assert parse_timestamp(format_srt(seconds)) == pytest.approx(seconds, abs=1e-3)
