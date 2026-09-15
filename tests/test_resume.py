"""Continuing a transcription that did not finish (§26)."""

import numpy as np
import pytest

from lecture_saver.core.models import StageStatus, TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.transcription import pipeline as pipeline_module
from lecture_saver.transcription.pipeline import PipelineError, TranscriptionPipeline
from lecture_saver.transcription.resume import next_segment_number, resume_point
from lecture_saver.transcription.whisper_engine import (
    SAMPLE_RATE,
    RawSegment,
    TranscriptionResult,
    WhisperEngine,
    WhisperOptions,
)


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    proj = Project.create(
        tmp_path / "p", "Long", VideoInfo(path=str(video), duration=600.0, has_audio=True)
    )
    proj.transcript.replace_all([
        TranscriptSegment(id=f"segment_{i:03d}", start=i * 10.0, end=i * 10.0 + 8,
                          original_text=f"line {i}")
        for i in range(1, 11)
    ])
    proj.save()
    proj.set_stage("transcription", StageStatus.CANCELLED)
    return proj


class TestResumePoint:
    def test_restarts_at_the_possibly_cut_off_last_segment(self, project):
        point = resume_point(project)
        assert point is not None
        assert point.start == 100.0
        assert [s.id for s in point.keep] == [f"segment_{i:03d}" for i in range(1, 10)]

    def test_a_corrected_last_segment_is_kept(self, project):
        project.transcript.edit_text("segment_010", "fixed by hand")
        point = resume_point(project)
        assert point.start == 108.0
        assert len(point.keep) == 10

    @pytest.mark.parametrize("status", [StageStatus.FAILED, StageStatus.RUNNING])
    def test_a_failure_or_a_crash_can_be_continued(self, project, status):
        project.set_stage("transcription", status)
        assert resume_point(project) is not None

    def test_a_finished_transcription_is_not_offered(self, project):
        project.set_stage("transcription", StageStatus.DONE)
        assert resume_point(project) is None

    def test_nearly_finished_is_not_worth_continuing(self, project):
        project.meta.video.duration = 115.0
        assert resume_point(project) is None

    def test_nothing_transcribed_is_not_a_resume(self, project):
        project.transcript.replace_all([])
        assert resume_point(project) is None

    def test_next_number(self):
        segments = [TranscriptSegment(id=i, start=0, end=1, original_text="")
                    for i in ("segment_003", "segment_010", "custom")]
        assert next_segment_number(segments) == 11


class TestEngineOffset:
    def test_audio_is_cut_and_times_stay_relative_to_the_lecture(self, monkeypatch, tmp_path):
        import faster_whisper.audio as fw_audio

        seen = {}
        monkeypatch.setattr(
            fw_audio, "decode_audio",
            lambda path, sampling_rate: np.zeros(SAMPLE_RATE * 120, dtype=np.float32),
        )

        class FakeModel:
            def transcribe(self, source, **kwargs):
                seen["samples"] = len(source)
                items = [type("S", (), {"start": 1.0, "end": 3.0, "text": " hello "})()]
                return iter(items), type("I", (), {"duration": 20.0, "language": "en"})()

        engine = WhisperEngine(WhisperOptions())
        engine._model = FakeModel()
        segments = []
        result = engine.transcribe(
            tmp_path / "a.wav", duration=120.0, start_at=100.0,
            on_segment=lambda seg, fraction: segments.append((seg, fraction)),
        )

        assert seen["samples"] == SAMPLE_RATE * 20
        assert (result.segments[0].start, result.segments[0].end) == (101.0, 103.0)
        assert segments[0][1] == pytest.approx(103.0 / 120.0)


class RecordingEngine:
    """Stands in for faster-whisper and remembers where it was asked to start."""

    started_at: list[float] = []

    def __init__(self, options):
        pass

    def load(self, on_status=None):
        pass

    def unload(self):
        pass

    def transcribe(self, audio_path, *, duration=0.0, on_segment=None, should_cancel=None,
                   start_at=0.0):
        RecordingEngine.started_at.append(start_at)
        raw = [RawSegment(start_at + i * 10.0, start_at + i * 10.0 + 8, f"new {i}")
               for i in range(3)]
        for index, segment in enumerate(raw):
            if on_segment:
                on_segment(segment, (index + 1) / len(raw))
        return TranscriptionResult(raw, "en", "cpu", "int8")


@pytest.fixture
def fake_engine(monkeypatch):
    RecordingEngine.started_at = []
    monkeypatch.setattr(pipeline_module, "WhisperEngine", RecordingEngine)
    monkeypatch.setattr(TranscriptionPipeline, "_prepare", lambda self: None)
    monkeypatch.setattr(TranscriptionPipeline, "_ensure_audio", lambda self: None)


class TestPipelineResume:
    def test_continuing_keeps_the_work_done_and_adds_the_rest(self, project, fake_engine):
        project.transcript.edit_text("segment_002", "a correction made meanwhile")
        project.save()

        TranscriptionPipeline(project, WhisperOptions(), resume=True).run()

        assert RecordingEngine.started_at == [100.0]
        segments = project.transcript.segments
        texts = [s.effective_text for s in segments]
        assert texts[:9] == ["line 1", "a correction made meanwhile"] + [
            f"line {i}" for i in range(3, 10)
        ]
        assert texts[9:] == ["new 0", "new 1", "new 2"]
        assert [s.start for s in segments[9:]] == [100.0, 110.0, 120.0]
        ids = [s.id for s in segments]
        assert len(set(ids)) == len(ids)
        assert project.stage_status("transcription") is StageStatus.DONE
        assert resume_point(project) is None

    def test_continuing_a_complete_transcript_is_refused(self, project, fake_engine):
        project.set_stage("transcription", StageStatus.DONE)
        with pytest.raises(PipelineError, match="nothing left"):
            TranscriptionPipeline(project, WhisperOptions(), resume=True).run()
        assert len(project.transcript) == 10

    def test_a_normal_run_still_starts_over(self, project, fake_engine):
        TranscriptionPipeline(project, WhisperOptions()).run()
        assert RecordingEngine.started_at == [0.0]
        assert [s.effective_text for s in project.transcript.segments] == [
            "new 0", "new 1", "new 2"
        ]


class TestResumeUI:
    def test_the_panel_offers_to_continue(self, qapp, project):
        from lecture_saver.ui.lecture_view import LectureView

        view = LectureView()
        view.load_project(project)
        panel = view.panel
        assert panel.resume_at == 100.0
        assert "00:01:40" in panel._resume_button.text()
        assert panel._resume_button.isVisibleTo(panel)

    def test_a_finished_lecture_has_no_continue_button(self, qapp, project):
        from lecture_saver.ui.lecture_view import LectureView

        project.set_stage("transcription", StageStatus.DONE)
        view = LectureView()
        view.load_project(project)
        assert view.panel.resume_at is None
        assert not view.panel._resume_button.isVisibleTo(view.panel)

    def test_continuing_from_the_window_asks_nothing_and_resumes(
        self, qapp, tmp_path, project, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        from lecture_saver.config import Settings
        from lecture_saver.ui import main_window as main_window_module
        from lecture_saver.ui.main_window import MainWindow

        started: list[bool] = []

        class FakeWorker:
            def __init__(self, project, options, *, resume=False):
                started.append(resume)
                for name in ("progress", "finished", "failed", "cancelled"):
                    setattr(self, name, type("S", (), {"connect": lambda *_: None})())

        monkeypatch.setattr(main_window_module, "TranscriptionWorker", FakeWorker)
        monkeypatch.setattr(main_window_module, "run_in_thread", lambda worker, thread: None)
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: pytest.fail("continuing must not ask to replace the transcript"),
        )
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project.root)
            window._lecture_view.resumeTranscriptionRequested.emit(WhisperOptions())
            assert started == [True]
        finally:
            window._transcribe_thread = None
            window.close()
