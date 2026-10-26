"""End-to-end transcription against a real Whisper model.

Deselected by default because the first run downloads the `tiny` model
(~75 MB) and inference takes several seconds. Run it explicitly with:

    pytest -m model

On Windows the spoken audio is synthesised with the built-in SAPI voice, which
gives real speech to transcribe without shipping an audio fixture. Where SAPI is
unavailable the test falls back to a tone and only asserts that the pipeline
completes - the point is that the pipeline genuinely runs, not that Whisper is
accurate.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lecture_saver.core.models import StageStatus
from lecture_saver.core.project import Project
from lecture_saver.media.ffmpeg_tools import ffmpeg_available, find_ffmpeg, probe_video
from lecture_saver.transcription.pipeline import TranscriptionPipeline
from lecture_saver.transcription.whisper_engine import WhisperOptions

pytestmark = [pytest.mark.model, pytest.mark.ffmpeg]

if not ffmpeg_available():  # pragma: no cover - environment dependent
    pytest.skip("FFmpeg is not available", allow_module_level=True)

SPOKEN_TEXT = (
    "Today we are going to begin with the basic concepts of machine learning. "
    "Here you can see supervised learning and unsupervised learning."
)


def _synthesise_speech(target: Path) -> bool:
    """Render SPOKEN_TEXT to a WAV with Windows SAPI. False if unavailable."""
    if sys.platform != "win32":
        return False
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        f"$s.SetOutputToWaveFile('{target}'); $s.Rate = -1; "
        f"$s.Speak('{SPOKEN_TEXT}'); $s.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True, capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return target.is_file() and target.stat().st_size > 10_000


@pytest.fixture(scope="module")
def lecture_video(tmp_path_factory):
    """A short video whose audio track is real speech where possible."""
    directory = tmp_path_factory.mktemp("e2e")
    speech = directory / "speech.wav"
    has_speech = _synthesise_speech(speech)

    audio_input = (
        ["-i", str(speech)] if has_speech
        else ["-f", "lavfi", "-i", "sine=frequency=300:duration=8"]
    )
    video = directory / "lecture.mp4"
    subprocess.run(
        [
            str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=640x480:rate=15",
            *audio_input,
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(video),
        ],
        check=True, capture_output=True,
    )
    return video, has_speech


@pytest.fixture(scope="module")
def transcribed(tmp_path_factory, lecture_video):
    video, has_speech = lecture_video
    project = Project.create(tmp_path_factory.mktemp("proj"), "E2E lecture", probe_video(video))

    updates: list[tuple[str, float | None]] = []
    pipeline = TranscriptionPipeline(
        project,
        WhisperOptions(model_size="tiny", language="en"),
        on_update=lambda u: updates.append((u.stage.label, u.fraction)),
    )
    result = pipeline.run()
    return project, result, updates, has_speech


class TestPipelineRuns:
    def test_it_completes_and_reports_the_stages_in_order(self, transcribed):
        _, _, updates, _ = transcribed
        stages = list(dict.fromkeys(label for label, _ in updates))
        assert stages == [
            "Preparing video",
            "Extracting audio",
            "Transcribing lecture",
            "Saving project",
            "Ready",
        ]

    def test_audio_progress_reaches_one(self, transcribed):
        _, _, updates, _ = transcribed
        audio = [f for label, f in updates if label == "Extracting audio" and f is not None]
        assert audio and audio[-1] == pytest.approx(1.0)

    def test_it_ran_locally_on_a_real_backend(self, transcribed):
        _, result, _, _ = transcribed
        assert result.device in ("cpu", "cuda")
        assert result.compute_type

    def test_the_extracted_audio_is_16khz_mono(self, transcribed):
        import wave

        project, _, _, _ = transcribed
        with wave.open(str(project.audio_file), "rb") as handle:
            assert handle.getframerate() == 16_000
            assert handle.getnchannels() == 1

    def test_both_stages_are_marked_done(self, transcribed):
        project, _, _, _ = transcribed
        assert project.stage_status("audio") is StageStatus.DONE
        assert project.stage_status("transcription") is StageStatus.DONE

    def test_slide_stages_remain_untouched(self, transcribed):
        """Phases 3-4 must not be silently claimed as complete (§32)."""
        project, _, _, _ = transcribed
        assert project.stage_status("slides") is StageStatus.NOT_STARTED
        assert project.stage_status("alignment") is StageStatus.NOT_STARTED


class TestTranscriptOutput:
    def test_segments_persist_and_reload(self, transcribed):
        project, _, _, _ = transcribed
        reopened = Project.open(project.root)
        assert len(reopened.transcript) > 0

    def test_timestamps_are_ordered_and_within_the_video(self, transcribed):
        project, _, _, _ = transcribed
        segments = Project.open(project.root).transcript.segments
        duration = project.video.duration

        assert all(s.end >= s.start for s in segments)
        assert all(s.start <= duration + 1 for s in segments)
        assert [s.start for s in segments] == sorted(s.start for s in segments)

    def test_segments_start_unedited_so_originals_are_preserved(self, transcribed):
        project, _, _, _ = transcribed
        segments = Project.open(project.root).transcript.segments
        assert all(s.edited_text is None for s in segments)
        assert all(s.effective_text == s.original_text for s in segments)

    def test_ids_are_unique_and_sequential(self, transcribed):
        project, _, _, _ = transcribed
        ids = [s.id for s in Project.open(project.root).transcript.segments]
        assert len(set(ids)) == len(ids)
        assert ids[0] == "segment_001"

    def test_it_recognised_some_of_the_actual_speech(self, transcribed):
        project, _, _, has_speech = transcribed
        if not has_speech:
            pytest.skip("no speech synthesiser available on this platform")

        text = " ".join(s.effective_text for s in project.transcript.segments).lower()
        # `tiny` is inaccurate, so this asserts only that recognisable content
        # came through - not a transcript match.
        assert any(word in text for word in ("learning", "machine", "concepts", "today"))

    def test_the_settings_that_actually_ran_are_recorded(self, transcribed):
        project, _, _, _ = transcribed
        settings = Project.open(project.root).meta.transcription
        assert settings.model_size == "tiny"
        assert settings.resolved_device in ("cpu", "cuda")


def test_audio_extraction_is_reused_on_a_second_run(transcribed):
    """A completed extraction must not be redone (§26)."""
    project, _, _, _ = transcribed
    mtime_before = project.audio_file.stat().st_mtime_ns

    updates: list[str] = []
    TranscriptionPipeline(
        project,
        WhisperOptions(model_size="tiny", language="en"),
        on_update=lambda u: updates.append(u.message),
    )._ensure_audio()

    assert project.audio_file.stat().st_mtime_ns == mtime_before
    assert any("earlier" in message for message in updates)


def test_turbo_runs_on_openvino(tmp_path, lecture_video):
    """The Turbo model through OpenVINO, end to end.

    Only when its 830 MB model is already downloaded - a test should not start
    that download on its own.
    """
    pytest.importorskip("openvino_genai")
    from huggingface_hub import snapshot_download

    from lecture_saver.transcription.openvino_engine import OPENVINO_MODELS
    from lecture_saver.transcription.whisper_engine import ENGINE_OPENVINO, TURBO_MODEL

    try:
        snapshot_download(OPENVINO_MODELS[TURBO_MODEL], local_files_only=True)
    except Exception:  # noqa: BLE001
        pytest.skip("the Turbo model is not downloaded")

    video, has_speech = lecture_video
    project = Project.create(tmp_path / "turbo", "Turbo", probe_video(video))
    result = TranscriptionPipeline(
        project, WhisperOptions(model_size=TURBO_MODEL, language="en")
    ).run()

    assert result.engine == ENGINE_OPENVINO
    assert result.device in ("gpu", "cpu")
    assert project.meta.transcription.resolved_engine == ENGINE_OPENVINO
    if has_speech:
        text = " ".join(s.effective_text for s in project.transcript.segments).lower()
        assert "machine learning" in text


def test_cancelling_before_work_starts_raises_cancelled(tmp_path, lecture_video):
    from lecture_saver.media.errors import Cancelled

    video, _ = lecture_video
    project = Project.create(tmp_path / "cancel", "Cancelled", probe_video(video))

    pipeline = TranscriptionPipeline(
        project, WhisperOptions(model_size="tiny"), should_cancel=lambda: True
    )
    with pytest.raises(Cancelled):
        pipeline.run()

    assert project.stage_status("transcription") is StageStatus.CANCELLED
    assert os.path.getsize(project.transcript_file) > 0  # file still valid JSON
