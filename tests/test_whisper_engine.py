"""The engine's streaming loop, without a model (§6, §33).

A stub stands in for faster-whisper so the lines the engine hands on - and the
options it asks for - can be pinned down without downloading anything. The real
model is exercised by ``pytest -m model``.
"""

from dataclasses import dataclass
from pathlib import Path

from lecture_saver.transcription.whisper_engine import WhisperEngine, WhisperOptions


@dataclass
class FakeWord:
    word: str
    start: float
    end: float


@dataclass
class FakeSegment:
    start: float
    end: float
    text: str
    words: list[FakeWord] | None = None


class FakeInfo:
    duration = 60.0
    language = "en"


class FakeModel:
    """Returns the segments it was given, and remembers how it was called."""

    def __init__(self, segments):
        self._segments = segments
        self.kwargs: dict = {}

    def transcribe(self, source, **kwargs):
        self.kwargs = kwargs
        return iter(self._segments), FakeInfo()


def words(*items):
    """``("Hello", 0.0, 0.5), ...`` -> fake words."""
    return [FakeWord(word=text, start=start, end=end) for text, start, end in items]


def engine_with(model, **options):
    engine = WhisperEngine(WhisperOptions(**options))
    engine._model = model
    return engine


class TestSentenceLines:
    def test_word_timestamps_are_requested(self):
        model = FakeModel([])
        engine_with(model, sentence_lines=True).transcribe(Path("lecture.wav"))
        assert model.kwargs["word_timestamps"] is True

    def test_keywords_are_given_as_hotwords(self):
        model = FakeModel([])
        engine_with(model, keywords="TLB, Naive Bayes").transcribe(Path("lecture.wav"))
        assert model.kwargs["hotwords"] == "TLB, Naive Bayes"

    def test_no_keywords_means_no_hotwords(self):
        model = FakeModel([])
        engine_with(model).transcribe(Path("lecture.wav"))
        assert model.kwargs["hotwords"] is None

    def test_lines_are_rebuilt_at_the_sentence_ends(self):
        # Whisper's own chunks split the second sentence down the middle.
        model = FakeModel(
            [
                FakeSegment(0.0, 6.0, "One thing is true. And the second", words=words(
                    ("One", 0.0, 0.5), ("thing", 0.6, 1.1), ("is", 1.2, 1.5),
                    ("true.", 1.6, 2.2), ("And", 2.4, 2.8), ("the", 2.9, 3.2),
                    ("second", 3.3, 6.0),
                )),
                FakeSegment(6.0, 9.0, "thought ends here.", words=words(
                    ("thought", 6.1, 6.7), ("ends", 6.8, 7.3), ("here.", 7.4, 9.0),
                )),
            ]
        )
        result = engine_with(model, sentence_lines=True).transcribe(Path("lecture.wav"))

        assert [segment.text for segment in result.segments] == [
            "One thing is true.",
            "And the second thought ends here.",
        ]
        assert result.segments[1].start == 2.4
        assert result.segments[1].end == 9.0

    def test_start_at_shifts_every_time(self):
        model = FakeModel(
            [FakeSegment(0.0, 2.0, "A line.", words=words(("A", 0.0, 0.5), ("line.", 0.6, 2.0)))]
        )
        engine = engine_with(model, sentence_lines=True)
        result = engine.transcribe(Path("lecture.wav"), start_at=0.0)
        assert result.segments[0].start == 0.0

    def test_a_chunk_without_word_times_is_used_as_it_is(self):
        model = FakeModel(
            [
                FakeSegment(0.0, 3.0, "Words we have.", words=words(
                    ("Words", 0.0, 0.5), ("we", 0.6, 1.0), ("have.", 1.1, 3.0),
                )),
                FakeSegment(3.0, 6.0, "No word times here.", words=None),
            ]
        )
        result = engine_with(model, sentence_lines=True).transcribe(Path("lecture.wav"))

        assert [segment.text for segment in result.segments] == [
            "Words we have.",
            "No word times here.",
        ]

    def test_the_tail_is_never_dropped(self):
        model = FakeModel(
            [FakeSegment(0.0, 4.0, "Nothing ends this", words=words(
                ("Nothing", 0.0, 0.8), ("ends", 0.9, 1.5), ("this", 1.6, 4.0),
            ))]
        )
        result = engine_with(model, sentence_lines=True).transcribe(Path("lecture.wav"))
        assert [segment.text for segment in result.segments] == ["Nothing ends this"]

    def test_cancelling_keeps_what_was_already_decoded(self):
        model = FakeModel(
            [
                FakeSegment(0.0, 3.0, "First line.", words=words(
                    ("First", 0.0, 0.8), ("line.", 0.9, 3.0),
                )),
                FakeSegment(3.0, 6.0, "Never reached.", words=words(
                    ("Never", 3.1, 3.8), ("reached.", 3.9, 6.0),
                )),
            ]
        )
        seen: list[int] = []

        def cancel():
            seen.append(1)
            return len(seen) > 1  # let the first chunk through, then stop

        result = engine_with(model, sentence_lines=True).transcribe(
            Path("lecture.wav"), should_cancel=cancel
        )
        assert [segment.text for segment in result.segments] == ["First line."]


class TestWithoutSentenceLines:
    def test_whispers_own_chunks_are_kept(self):
        model = FakeModel(
            [
                FakeSegment(0.0, 6.0, "One thing is true. And the second"),
                FakeSegment(6.0, 9.0, "thought ends here."),
            ]
        )
        result = engine_with(model, sentence_lines=False).transcribe(Path("lecture.wav"))

        assert model.kwargs["word_timestamps"] is False
        assert [segment.text for segment in result.segments] == [
            "One thing is true. And the second",
            "thought ends here.",
        ]

    def test_progress_reaches_the_end_of_the_audio(self):
        model = FakeModel([FakeSegment(0.0, 30.0, "Half way.")])
        reported: list[float] = []
        engine_with(model, sentence_lines=False).transcribe(
            Path("lecture.wav"),
            duration=60.0,
            on_segment=lambda segment, fraction: reported.append(fraction),
        )
        assert reported == [0.5]
