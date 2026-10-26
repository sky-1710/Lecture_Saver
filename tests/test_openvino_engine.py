"""The OpenVINO engine's window loop, without OpenVINO or a model (§6, §33).

A stub stands in for ``openvino_genai.WhisperPipeline`` and the audio and voice
activity detection are replaced, so what the engine asks for and what it hands
on can be pinned down directly. The real model is exercised by
``pytest -m model``.
"""

import sys
import types
from dataclasses import dataclass, field

import numpy as np
import pytest

from lecture_saver.transcription import openvino_engine
from lecture_saver.transcription.openvino_engine import (
    OpenVinoEngine,
    OpenVinoUnavailableError,
)
from lecture_saver.transcription.whisper_engine import (
    ENGINE_OPENVINO,
    SAMPLE_RATE,
    TURBO_MODEL,
    WhisperOptions,
)


@dataclass
class FakeChunk:
    start_ts: float
    end_ts: float
    text: str


@dataclass
class FakeWord:
    word: str
    start_ts: float
    end_ts: float


@dataclass
class FakeResult:
    chunks: list[FakeChunk]
    words: list[FakeWord] | None = None
    language: str = "ko"


def said(*items):
    """``("One", 0.0, 0.5), ...`` -> a result whose words and line say that."""
    words = [FakeWord(text, start, end) for text, start, end in items]
    text = " ".join(text for text, _, _ in items)
    return FakeResult(chunks=[FakeChunk(items[0][1], items[-1][2], text)], words=words)


@dataclass
class FakePipeline:
    """Returns the results it was given, one per window, and records each call."""

    results: list[FakeResult]
    calls: list[dict] = field(default_factory=list)
    fail: object = None  # (config) -> exception to raise, or None

    def get_generation_config(self):
        return types.SimpleNamespace(hotwords=None, language=None)

    def generate(self, samples, config):
        self.calls.append(
            {"samples": len(samples), "language": config.language, "hotwords": config.hotwords}
        )
        if self.fail is not None:
            error = self.fail(config)
            if error is not None:
                raise error
        return self.results.pop(0)


@pytest.fixture
def audio(monkeypatch):
    """Replace audio loading and VAD. Returns a setter for both."""
    state = {"seconds": 120.0, "regions": [(0.0, 10.0)], "vad_input": None}

    def load(path):
        return np.zeros(int(state["seconds"] * SAMPLE_RATE), dtype=np.float32)

    def regions(samples):
        state["vad_input"] = len(samples) / SAMPLE_RATE
        return list(state["regions"])

    monkeypatch.setattr(openvino_engine, "_load_audio", load)
    monkeypatch.setattr(openvino_engine, "_speech_regions", regions)
    return state


def engine_with(pipeline, **options):
    engine = OpenVinoEngine(WhisperOptions(model_size=TURBO_MODEL, **options))
    engine._pipeline = pipeline
    engine._device = "gpu"
    return engine


class TestWindows:
    def test_word_times_land_on_the_lecture_timeline(self, audio, tmp_path):
        # Two speech regions with 30 s of silence between them, one window.
        audio["regions"] = [(10.0, 20.0), (50.0, 55.0)]
        pipeline = FakePipeline([said(("First", 1.0, 1.6), ("line.", 1.8, 3.0),
                                      ("Second", 10.5, 11.2), ("line.", 11.4, 12.0))])
        result = engine_with(pipeline).transcribe(tmp_path / "a.wav", duration=120.0)

        # 10.5 s into the window is 0.5 s into the second piece: 50.5 s.
        assert [(s.start, s.end, s.text) for s in result.segments] == [
            (11.0, 13.0, "First line."),
            (50.5, 52.0, "Second line."),
        ]
        assert result.engine == ENGINE_OPENVINO
        assert result.device == "gpu"

    def test_each_window_is_one_call_with_only_its_speech(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 55.0)]
        pipeline = FakePipeline([said(("One.", 0.0, 2.0)), said(("Two.", 0.0, 2.0))])
        engine_with(pipeline).transcribe(tmp_path / "a.wav")
        assert [call["samples"] for call in pipeline.calls] == [25 * SAMPLE_RATE] * 2

    def test_progress_follows_the_windows(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 54.0)]
        pipeline = FakePipeline([said(("One.", 0.0, 2.0)), said(("Two.", 0.0, 2.0))])
        reported = []
        engine_with(pipeline).transcribe(
            tmp_path / "a.wav", duration=60.0,
            on_segment=lambda segment, fraction: reported.append(fraction),
        )
        assert reported == [pytest.approx(25 / 60), pytest.approx(54 / 60)]

    def test_whispers_line_ends_break_unpunctuated_speech(self, audio, tmp_path):
        # Edge-to-edge word times and no full stops: only Whisper's own line
        # boundaries say where the lecturer paused.
        pipeline = FakePipeline([FakeResult(
            chunks=[FakeChunk(0.0, 2.0, "선택을 하는데 사용을 합니다"),
                    FakeChunk(2.0, 4.0, "그 다음에 테스트 셋은")],
            words=[FakeWord(text, i * 0.5, (i + 1) * 0.5) for i, text in enumerate(
                ["선택을", "하는데", "사용을", "합니다", "그", "다음에", "테스트", "셋은"])],
        )])
        result = engine_with(pipeline).transcribe(tmp_path / "a.wav")
        assert [s.text for s in result.segments] == [
            "선택을 하는데 사용을 합니다",
            "그 다음에 테스트 셋은",
        ]

    def test_words_meet_whispers_lines_by_letters_not_times(self, audio, tmp_path):
        # The word times lag: "%" straddles the end of the first line. Matching
        # by time would end the line one word early; the letters say where.
        pipeline = FakePipeline([FakeResult(
            chunks=[FakeChunk(0.0, 2.0, "선택을 하는데 50%"), FakeChunk(2.0, 4.0, "그 다음에")],
            words=[FakeWord("선택을", 0.3, 0.8), FakeWord("하는데", 0.8, 1.4),
                   FakeWord("50", 1.4, 1.8), FakeWord("%", 1.8, 2.3),
                   FakeWord("그", 2.3, 2.8), FakeWord("다음에", 2.8, 4.0)],
        )])
        result = engine_with(pipeline).transcribe(tmp_path / "a.wav")
        assert [s.text for s in result.segments] == ["선택을 하는데 50 %", "그 다음에"]

    def test_a_window_without_word_times_uses_whispers_lines(self, audio, tmp_path):
        audio["regions"] = [(100.0, 110.0)]
        pipeline = FakePipeline([FakeResult(chunks=[FakeChunk(1.0, 4.0, " As it came. ")])])
        result = engine_with(pipeline).transcribe(tmp_path / "a.wav")
        assert [(s.start, s.end, s.text) for s in result.segments] == [
            (101.0, 104.0, "As it came."),
        ]

    def test_vad_off_feeds_the_whole_audio_in_windows(self, audio, tmp_path):
        audio["seconds"] = 70.0
        pipeline = FakePipeline([FakeResult(chunks=[])] * 3)
        engine_with(pipeline, vad_filter=False).transcribe(tmp_path / "a.wav")
        assert [call["samples"] for call in pipeline.calls] == [
            30 * SAMPLE_RATE, 30 * SAMPLE_RATE, 10 * SAMPLE_RATE,
        ]
        assert audio["vad_input"] is None

    def test_start_at_skips_the_audio_and_keeps_lecture_times(self, audio, tmp_path):
        audio["seconds"] = 160.0
        audio["regions"] = [(5.0, 15.0)]  # relative to the cut audio
        pipeline = FakePipeline([said(("Later.", 0.0, 2.0))])
        result = engine_with(pipeline).transcribe(
            tmp_path / "a.wav", duration=160.0, start_at=100.0
        )
        assert audio["vad_input"] == pytest.approx(60.0)
        assert (result.segments[0].start, result.segments[0].end) == (105.0, 107.0)

    def test_cancelling_keeps_what_was_already_decoded(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 55.0)]
        pipeline = FakePipeline([said(("Kept.", 0.0, 2.0)), said(("Never.", 0.0, 2.0))])
        checks = []

        def cancel():
            checks.append(1)
            return len(checks) > 1

        result = engine_with(pipeline).transcribe(tmp_path / "a.wav", should_cancel=cancel)
        assert [s.text for s in result.segments] == ["Kept."]
        assert len(pipeline.calls) == 1


class TestLanguage:
    def test_detected_on_the_first_window_then_held(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 55.0)]
        pipeline = FakePipeline([said(("하나.", 0.0, 2.0)), said(("둘.", 0.0, 2.0))])
        result = engine_with(pipeline, language="auto").transcribe(tmp_path / "a.wav")
        assert [call["language"] for call in pipeline.calls] == [None, "<|ko|>"]
        assert result.language == "ko"

    def test_a_chosen_language_is_used_from_the_start(self, audio, tmp_path):
        pipeline = FakePipeline([said(("Hello.", 0.0, 2.0))])
        result = engine_with(pipeline, language="en").transcribe(tmp_path / "a.wav")
        assert pipeline.calls[0]["language"] == "<|en|>"
        assert result.language == "en"


class TestKeywords:
    def test_every_window_gets_the_keywords(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 55.0)]
        pipeline = FakePipeline([said(("One.", 0.0, 2.0)), said(("Two.", 0.0, 2.0))])
        engine_with(pipeline, keywords="TLB, 크로스 밸리데이션").transcribe(tmp_path / "a.wav")
        assert [call["hotwords"] for call in pipeline.calls] == ["TLB, 크로스 밸리데이션"] * 2

    def test_keywords_that_overflow_the_prompt_are_halved(self, audio, tmp_path):
        audio["regions"] = [(0.0, 25.0), (30.0, 55.0)]

        def too_long(config):
            if config.hotwords and config.hotwords.count(",") >= 2:
                return RuntimeError("max_length should be bigger than number of prompt tokens")
            return None

        pipeline = FakePipeline(
            [said(("One.", 0.0, 2.0)), said(("Two.", 0.0, 2.0))], fail=too_long
        )
        result = engine_with(pipeline, keywords="a, b, c, d").transcribe(tmp_path / "a.wav")

        assert [call["hotwords"] for call in pipeline.calls] == ["a, b, c, d", "a, b", "a, b"]
        assert [s.text for s in result.segments] == ["One.", "Two."]

    def test_other_errors_are_not_mistaken_for_long_keywords(self, audio, tmp_path):
        pipeline = FakePipeline([], fail=lambda config: RuntimeError("device lost"))
        with pytest.raises(RuntimeError, match="device lost"):
            engine_with(pipeline, keywords="a, b").transcribe(tmp_path / "a.wav")


class TestLoops:
    def test_a_looping_line_and_its_words_are_dropped(self, audio, tmp_path):
        loop = "리" * 120
        pipeline = FakePipeline([FakeResult(
            chunks=[FakeChunk(0.0, 3.0, "Real speech."), FakeChunk(3.0, 28.0, loop)],
            words=[FakeWord("Real", 0.0, 1.0), FakeWord("speech.", 1.2, 3.0),
                   FakeWord(loop, 3.0, 28.0)],
        )])
        result = engine_with(pipeline).transcribe(tmp_path / "a.wav")
        assert [s.text for s in result.segments] == ["Real speech."]

    @pytest.mark.parametrize(
        "text",
        [
            "그래서 일반적으로 이렇게 데이터를 트레이닝셋, 밸리데이션셋, 테스트셋으로 나눠서 "
            "트레이닝셋은 모델을 훈련하는데 밸리데이션셋은 모델을 선택하는데 사용이 됩니다.",
            "TLB is typically small because it is a cache for the page table.",
        ],
    )
    def test_ordinary_speech_is_not_a_loop(self, text):
        assert not openvino_engine._is_loop(text)


class TestLineEndsByText:
    def test_whitespace_differences_are_ignored(self):
        ends = openvino_engine._ends_by_text(
            ["선택을 하는데 50%", "그 다음에"], ["선택을", "하는데", "50", "%", "그", "다음에"]
        )
        assert ends == {3, 5}

    def test_totals_that_disagree_give_no_answer(self):
        assert openvino_engine._ends_by_text(["one two"], ["one", "twos"]) is None
        assert openvino_engine._ends_by_text([], ["one"]) is None


class FakeOpenVino:
    """A stand-in ``openvino_genai`` module whose GPU may refuse to load."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.built = []
        outer = self

        class WhisperPipeline:
            def __init__(self, path, device, **properties):
                outer.built.append((device, properties))
                if device in outer.failing:
                    raise RuntimeError(f"{device} is not happy")

        self.WhisperPipeline = WhisperPipeline


class TestLoading:
    @pytest.fixture
    def openvino(self, monkeypatch, tmp_path):
        def install(fake, gpu=True):
            monkeypatch.setitem(sys.modules, "openvino_genai", fake)
            monkeypatch.setattr(openvino_engine, "openvino_gpu_available", lambda: gpu)
            monkeypatch.setattr(openvino_engine, "_model_dir", lambda repo, status: tmp_path)
            return OpenVinoEngine(WhisperOptions(model_size=TURBO_MODEL), cache_dir=tmp_path)
        return install

    def test_the_gpu_is_tried_first(self, openvino, tmp_path):
        fake = FakeOpenVino()
        engine = openvino(fake)
        engine.load()
        device, properties = fake.built[0]
        assert device == "GPU"
        assert properties["word_timestamps"] is True
        assert properties["CACHE_DIR"] == str(tmp_path / "GPU")
        assert engine._device == "gpu" and engine._fallback_note is None

    def test_a_failing_gpu_falls_back_to_the_cpu_and_says_so(self, openvino):
        fake = FakeOpenVino(failing={"GPU"})
        engine = openvino(fake)
        engine.load()
        assert [device for device, _ in fake.built] == ["GPU", "CPU"]
        assert engine._device == "cpu"
        assert "CPU" in engine._fallback_note

    def test_no_gpu_goes_straight_to_the_cpu_without_a_note(self, openvino):
        fake = FakeOpenVino()
        engine = openvino(fake, gpu=False)
        engine.load()
        assert [device for device, _ in fake.built] == ["CPU"]
        assert engine._fallback_note is None

    def test_nothing_working_is_reported_as_unavailable(self, openvino):
        engine = openvino(FakeOpenVino(failing={"GPU", "CPU"}))
        with pytest.raises(OpenVinoUnavailableError, match="GPU is not happy"):
            engine.load()

    def test_openvino_not_installed_is_reported_as_unavailable(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "openvino_genai", None)
        engine = OpenVinoEngine(WhisperOptions(model_size=TURBO_MODEL))
        with pytest.raises(OpenVinoUnavailableError, match="not installed"):
            engine.load()
