"""Which engine a run uses, and falling back when OpenVINO cannot start (§28)."""

import pytest

from lecture_saver.core.models import TranscriptionSettings, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.transcription import pipeline as pipeline_module
from lecture_saver.transcription.openvino_engine import OpenVinoUnavailableError
from lecture_saver.transcription.pipeline import TranscriptionPipeline
from lecture_saver.transcription.whisper_engine import (
    ENGINE_AUTO,
    ENGINE_FASTER_WHISPER,
    ENGINE_OPENVINO,
    KEYWORD_CHAR_LIMIT,
    TURBO_MODEL,
    RawSegment,
    TranscriptionResult,
    WhisperOptions,
    engine_for_model,
    estimate_seconds,
    normalize_keywords,
)


class FakeEngine:
    """Records which options it was built with; streams two fixed lines."""

    built: list[tuple[str, WhisperOptions]] = []
    engine_name = ENGINE_FASTER_WHISPER
    load_error: Exception | None = None

    def __init__(self, options):
        self.options = options
        self.note = None
        FakeEngine.built.append((self.engine_name, options))

    def load(self, on_status=None):
        if self.load_error is not None:
            raise self.load_error

    def unload(self):
        pass

    def note_fallback(self, note):
        self.note = note

    def transcribe(self, audio_path, *, duration=0.0, on_segment=None, should_cancel=None,
                   start_at=0.0):
        raw = [RawSegment(0.0, 2.0, "하나."), RawSegment(2.0, 4.0, "둘.")]
        for segment in raw:
            if on_segment:
                on_segment(segment, 0.5)
        return TranscriptionResult(raw, "ko", "cpu", "int8", fallback_note=self.note,
                                   engine=self.engine_name)


class FakeWhisper(FakeEngine):
    engine_name = ENGINE_FASTER_WHISPER


class FakeOpenVino(FakeEngine):
    engine_name = ENGINE_OPENVINO


@pytest.fixture
def run(monkeypatch, tmp_path):
    FakeEngine.built = []
    monkeypatch.setattr(FakeOpenVino, "load_error", None)
    monkeypatch.setattr(pipeline_module, "WhisperEngine", FakeWhisper)
    monkeypatch.setattr(pipeline_module, "OpenVinoEngine", FakeOpenVino)
    monkeypatch.setattr(TranscriptionPipeline, "_prepare", lambda self: None)
    monkeypatch.setattr(TranscriptionPipeline, "_ensure_audio", lambda self: None)
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    project = Project.create(
        tmp_path / "p", "Lecture", VideoInfo(path=str(video), duration=60.0, has_audio=True)
    )

    def go(**options):
        result = TranscriptionPipeline(project, WhisperOptions(**options)).run()
        return project, result

    return go


class TestEngineChoice:
    def test_turbo_runs_on_openvino(self, run):
        project, result = run(model_size=TURBO_MODEL, keywords="TLB")
        assert [name for name, _ in FakeEngine.built] == [ENGINE_OPENVINO]
        settings = project.meta.transcription
        assert settings.resolved_engine == ENGINE_OPENVINO
        assert settings.keywords == "TLB"
        assert result.fallback_note is None

    def test_the_other_models_run_on_faster_whisper(self, run):
        project, _ = run(model_size="medium")
        assert [name for name, _ in FakeEngine.built] == [ENGINE_FASTER_WHISPER]
        assert project.meta.transcription.resolved_engine == ENGINE_FASTER_WHISPER

    def test_openvino_that_cannot_start_falls_back_and_says_why(self, run, monkeypatch):
        monkeypatch.setattr(
            FakeOpenVino, "load_error", OpenVinoUnavailableError("OpenVINO is not installed.")
        )
        project, result = run(model_size=TURBO_MODEL, keywords="TLB")

        assert [name for name, _ in FakeEngine.built] == [ENGINE_OPENVINO, ENGINE_FASTER_WHISPER]
        fallback_options = FakeEngine.built[1][1]
        assert fallback_options.engine == ENGINE_FASTER_WHISPER
        assert fallback_options.model_size == TURBO_MODEL
        assert fallback_options.keywords == "TLB"
        assert "CPU engine" in result.fallback_note
        assert "not installed" in result.fallback_note
        assert project.meta.transcription.resolved_engine == ENGINE_FASTER_WHISPER
        assert len(project.transcript) == 2

    def test_a_model_that_cannot_be_downloaded_is_not_a_fallback(self, run, monkeypatch):
        from lecture_saver.transcription.pipeline import PipelineError
        from lecture_saver.transcription.whisper_engine import ModelLoadError

        monkeypatch.setattr(FakeOpenVino, "load_error", ModelLoadError("no network"))
        with pytest.raises(PipelineError, match="no network"):
            run(model_size=TURBO_MODEL)
        assert [name for name, _ in FakeEngine.built] == [ENGINE_OPENVINO]


class TestOptions:
    @pytest.mark.parametrize(
        ("model", "engine"),
        [(TURBO_MODEL, ENGINE_OPENVINO), ("medium", ENGINE_FASTER_WHISPER),
         ("small", ENGINE_FASTER_WHISPER)],
    )
    def test_the_engine_follows_the_model(self, model, engine):
        assert engine_for_model(model) == engine
        assert WhisperOptions(model_size=model).resolved_engine == engine

    def test_an_explicit_engine_wins(self):
        options = WhisperOptions(model_size=TURBO_MODEL, engine=ENGINE_FASTER_WHISPER)
        assert options.resolved_engine == ENGINE_FASTER_WHISPER
        assert WhisperOptions().engine == ENGINE_AUTO

    def test_the_gpu_estimate_is_faster_for_turbo_only(self):
        assert estimate_seconds(3600, TURBO_MODEL, gpu=True) < estimate_seconds(3600, TURBO_MODEL)
        assert estimate_seconds(3600, "medium", gpu=True) == estimate_seconds(3600, "medium")


class TestKeywords:
    def test_terms_are_cleaned_and_deduplicated(self):
        text = " TLB ,  Naive   Bayes\n크로스 밸리데이션,, tlb , "
        assert normalize_keywords(text) == "TLB, Naive Bayes, 크로스 밸리데이션"

    def test_only_whole_terms_fit_under_the_limit(self):
        terms = [f"term{i:03d}" for i in range(200)]
        result = normalize_keywords(", ".join(terms))
        assert len(result) <= KEYWORD_CHAR_LIMIT
        assert all(term in terms for term in result.split(", "))

    def test_nothing_is_nothing(self):
        assert normalize_keywords("  , \n ") == ""


class TestSettings:
    def test_engine_and_keywords_survive_the_round_trip(self):
        settings = TranscriptionSettings(
            model_size=TURBO_MODEL, keywords="TLB", resolved_engine=ENGINE_OPENVINO
        )
        restored = TranscriptionSettings.from_dict(settings.to_dict())
        assert restored == settings

    def test_a_project_from_before_engines_loads(self):
        old = {"model_size": "medium", "language": "ko", "resolved_device": "cpu"}
        settings = TranscriptionSettings.from_dict(old)
        assert settings.engine == "auto"
        assert settings.keywords == ""
        assert settings.resolved_engine is None
