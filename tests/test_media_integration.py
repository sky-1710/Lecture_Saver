"""End-to-end media tests against a tiny synthetic video.

FFmpeg generates a 3-second clip, which is then probed and had its audio
extracted for real. No lecture recording is needed, and the whole module runs in
a couple of seconds (§33).
"""

import subprocess
import wave

import pytest

from lecture_saver.media.errors import Cancelled, MissingVideoFileError, VideoProbeError
from lecture_saver.media.ffmpeg_tools import (
    extract_audio,
    ffmpeg_available,
    find_ffmpeg,
    probe_video,
)

pytestmark = pytest.mark.ffmpeg

if not ffmpeg_available():  # pragma: no cover - environment dependent
    pytest.skip("FFmpeg is not available", allow_module_level=True)


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    """A 3-second 320x240 clip with a 440 Hz tone."""
    path = tmp_path_factory.mktemp("media") / "sample.mp4"
    subprocess.run(
        [
            str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="module")
def silent_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "silent.mp4"
    subprocess.run(
        [
            str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class TestProbe:
    def test_reads_metadata(self, sample_video):
        info = probe_video(sample_video)
        assert info.duration == pytest.approx(3.0, abs=0.3)
        assert (info.width, info.height) == (320, 240)
        assert info.video_codec == "h264"
        assert info.has_audio is True
        assert info.size_bytes > 0

    def test_video_without_audio(self, silent_video):
        assert probe_video(silent_video).has_audio is False

    def test_missing_file(self, tmp_path):
        with pytest.raises(MissingVideoFileError):
            probe_video(tmp_path / "nope.mp4")

    def test_corrupt_file_raises_a_readable_error(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"\x00" * 4096)
        with pytest.raises(VideoProbeError):
            probe_video(broken)


class TestExtractAudio:
    def test_produces_16khz_mono_pcm(self, sample_video, tmp_path):
        output = tmp_path / "audio" / "lecture.wav"
        extract_audio(sample_video, output, duration=3.0)

        assert output.is_file()
        with wave.open(str(output), "rb") as handle:
            assert handle.getframerate() == 16_000
            assert handle.getnchannels() == 1
            assert handle.getsampwidth() == 2
            assert handle.getnframes() > 16_000  # more than a second of audio

    def test_reports_progress_ending_at_one(self, sample_video, tmp_path):
        seen: list[float] = []
        extract_audio(
            sample_video, tmp_path / "a.wav", duration=3.0, on_progress=seen.append
        )
        assert seen, "no progress was reported"
        assert seen[-1] == pytest.approx(1.0)
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen == sorted(seen)

    def test_cancel_leaves_no_partial_file(self, sample_video, tmp_path):
        output = tmp_path / "cancelled.wav"
        with pytest.raises(Cancelled):
            extract_audio(sample_video, output, duration=3.0, should_cancel=lambda: True)

        assert not output.exists()
        assert not list(tmp_path.glob("*.partial.wav"))

    def test_missing_source(self, tmp_path):
        with pytest.raises(MissingVideoFileError):
            extract_audio(tmp_path / "gone.mp4", tmp_path / "out.wav")
