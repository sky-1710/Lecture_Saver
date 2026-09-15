"""Whisper words -> lines that end where sentences do (§6, §10, §33)."""

from lecture_saver.transcription.segmentation import Line, SentenceSegmenter, Word


def words(*items, start=0.0, step=0.5, gap=0.0):
    """``items`` are word texts; times run one after another."""
    made = []
    at = start
    for text in items:
        made.append(Word(text=text, start=at, end=at + step))
        at += step + gap
    return made


def texts(lines):
    return [line.text for line in lines]


class TestSentenceEnds:
    def test_a_line_ends_at_the_full_stop(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        lines = segmenter.feed(words("This", "is", "one.", "And", "another."))
        assert texts(lines) == ["This is one.", "And another."]

    def test_the_tail_waits_for_the_rest_of_its_sentence(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        assert texts(segmenter.feed(words("A", "sentence", "that"))) == []
        assert texts(segmenter.feed(words("carries", "on.", start=1.5))) == [
            "A sentence that carries on."
        ]

    def test_what_is_left_comes_out_of_flush(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        segmenter.feed(words("No", "full", "stop", "here"))
        assert texts(segmenter.flush()) == ["No full stop here"]
        assert segmenter.flush() == []

    def test_a_very_short_sentence_joins_what_follows(self):
        # "Yes." on its own line is not useful; min_seconds keeps it attached.
        segmenter = SentenceSegmenter(min_seconds=1.5)
        lines = segmenter.feed(words("Yes.", "Now", "the", "real", "point."))
        assert texts(lines) == ["Yes. Now the real point."]

    def test_question_and_exclamation_marks_end_a_line(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        assert len(segmenter.feed(words("Clear?", "Good!", "Next."))) == 3

    def test_korean_sentences(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        lines = segmenter.feed(words("여기서", "보시면", "됩니다.", "값이", "큽니다."))
        assert texts(lines) == ["여기서 보시면 됩니다.", "값이 큽니다."]


class TestPauses:
    def test_a_silence_ends_the_line_even_without_punctuation(self):
        segmenter = SentenceSegmenter(min_seconds=0.0, pause=0.8)
        spoken = words("the", "lecturer", "stops") + words("then", "goes", "on", start=6.0)
        assert texts(segmenter.feed(spoken)) == ["the lecturer stops"]
        assert texts(segmenter.flush()) == ["then goes on"]

    def test_a_short_gap_is_not_a_break(self):
        segmenter = SentenceSegmenter(min_seconds=0.0, pause=0.8)
        assert segmenter.feed(words("still", "one", "line", gap=0.2)) == []


class TestOverLongLines:
    def test_it_is_cut_at_the_last_comma(self):
        segmenter = SentenceSegmenter(min_seconds=0.0, max_seconds=2.0)
        lines = segmenter.feed(words("one", "two,", "three", "four", "five"))
        assert texts(lines) == ["one two,"]

    def test_without_a_comma_it_is_cut_at_the_longest_silence(self):
        segmenter = SentenceSegmenter(min_seconds=0.0, max_seconds=2.0, pause=5.0)
        spoken = [
            Word("one", 0.0, 0.5),
            Word("two", 0.5, 1.0),
            Word("three", 2.0, 2.5),  # the longest gap is before this one
            Word("four", 2.5, 3.0),
            Word("five", 3.0, 3.5),
        ]
        assert texts(segmenter.feed(spoken))[0] == "one two"

    def test_a_line_never_swallows_the_whole_buffer(self):
        segmenter = SentenceSegmenter(min_seconds=0.0, max_seconds=1.0)
        segmenter.feed(words("a", "b", "c", "d"))
        # Something is always left pending, so the next sentence keeps its start.
        assert segmenter.pending


class TestTimesAndText:
    def test_a_line_carries_the_times_of_its_words(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        line = segmenter.feed(words("hello", "there.", start=10.0, step=0.5))[0]
        assert line.start == 10.0
        assert line.end == 11.0

    def test_nothing_spoken_is_lost(self):
        segmenter = SentenceSegmenter(min_seconds=1.5)
        spoken = words(
            "Yes.", "Now", "the", "real", "point.", "And", "one", "more", "thing"
        )
        produced = segmenter.feed(spoken) + segmenter.flush()

        rejoined = " ".join(line.text for line in produced)
        assert rejoined == " ".join(word.text for word in spoken)

    def test_blank_words_are_ignored(self):
        segmenter = SentenceSegmenter(min_seconds=0.0)
        spoken = [Word(" ", 0.0, 0.1), Word("real.", 0.1, 0.6)]
        assert texts(segmenter.feed(spoken)) == ["real."]

    def test_an_empty_stream_produces_nothing(self):
        segmenter = SentenceSegmenter()
        assert segmenter.feed([]) == []
        assert segmenter.flush() == []
        assert not segmenter.pending

    def test_a_line_is_a_plain_value(self):
        assert Line(1.0, 2.0, "x") == Line(1.0, 2.0, "x")
