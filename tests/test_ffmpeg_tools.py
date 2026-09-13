"""FFmpeg helpers.

The banner parser is tested against recorded FFmpeg output, so these tests need
no binary and no video file.
"""

import pytest

from lecture_saver.media.ffmpeg_tools import (
    estimated_wav_bytes,
    parse_ffmpeg_banner,
)

MP4_BANNER = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:\\lectures\\lecture01.mp4':
  Metadata:
    major_brand     : isom
    encoder         : Lavf60.16.100
  Duration: 01:02:33.42, start: 0.000000, bitrate: 1421 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(progressive), \
1920x1080 [SAR 1:1 DAR 16:9], 1287 kb/s, 29.97 fps, 29.97 tbr, 90k tbn (default)
  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo, fltp, \
128 kb/s (default)
At least one output file must be specified
"""

MKV_BANNER = """\
Input #0, matroska,webm, from 'D:\\rec\\class.mkv':
  Duration: 00:48:02.11, start: 0.000000, bitrate: 2103 kb/s
  Stream #0:0: Video: hevc (Main), yuv420p(tv), 1280x720, 25 fps, 25 tbr, 1k tbn
  Stream #0:1: Audio: opus, 48000 Hz, mono, fltp
"""

NO_AUDIO_BANNER = """\
Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'silent.mp4':
  Duration: 00:00:30.00, start: 0.000000, bitrate: 400 kb/s
  Stream #0:0[0x1](und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 640x480, \
30 fps, 30 tbr, 15360 tbn (default)
"""

GARBAGE_BANNER = """\
[mov,mp4,m4a,3gp,3g2,mj2 @ 000001] moov atom not found
broken.mp4: Invalid data found when processing input
"""


@pytest.fixture(scope="module")
def mp4_info():
    return parse_ffmpeg_banner(MP4_BANNER)


@pytest.fixture(scope="module")
def mkv_info():
    return parse_ffmpeg_banner(MKV_BANNER)


class TestParseMp4:

    def test_duration(self, mp4_info):
        assert mp4_info["duration"] == pytest.approx(3753.42)

    def test_resolution(self, mp4_info):
        assert (mp4_info["width"], mp4_info["height"]) == (1920, 1080)

    def test_dar_ratio_is_not_mistaken_for_a_resolution(self, mp4_info):
        # "[SAR 1:1 DAR 16:9]" sits right after the resolution and must not win.
        assert mp4_info["width"] == 1920

    def test_fps(self, mp4_info):
        assert mp4_info["fps"] == pytest.approx(29.97)

    def test_codecs(self, mp4_info):
        assert mp4_info["video_codec"] == "h264"
        assert mp4_info["audio_codec"] == "aac"

    def test_container(self, mp4_info):
        assert mp4_info["container"] == "mov,mp4,m4a,3gp,3g2,mj2"

    def test_has_audio(self, mp4_info):
        assert mp4_info["has_audio"] is True


class TestParseMkv:
    def test_container_and_codecs(self, mkv_info):
        assert mkv_info["container"] == "matroska,webm"
        assert mkv_info["video_codec"] == "hevc"
        assert mkv_info["audio_codec"] == "opus"

    def test_duration_and_resolution(self, mkv_info):
        assert mkv_info["duration"] == pytest.approx(2882.11)
        assert (mkv_info["width"], mkv_info["height"]) == (1280, 720)

    def test_integer_fps(self, mkv_info):
        assert mkv_info["fps"] == pytest.approx(25.0)


def test_video_without_an_audio_track_is_reported():
    info = parse_ffmpeg_banner(NO_AUDIO_BANNER)
    assert info["has_audio"] is False
    assert info["audio_codec"] == ""
    assert info["video_codec"] == "h264"


def test_unreadable_input_yields_no_streams():
    info = parse_ffmpeg_banner(GARBAGE_BANNER)
    assert info["video_codec"] == ""
    assert info["has_audio"] is False


def test_empty_input_does_not_raise():
    info = parse_ffmpeg_banner("")
    assert info["duration"] == 0.0
    assert info["container"] == ""


class TestWavSizeEstimate:
    def test_one_hour_is_about_115_mb(self):
        assert estimated_wav_bytes(3600) == pytest.approx(115_200_044, rel=1e-6)

    def test_zero_and_negative_are_safe(self):
        assert estimated_wav_bytes(0) == 44
        assert estimated_wav_bytes(-10) == 44
