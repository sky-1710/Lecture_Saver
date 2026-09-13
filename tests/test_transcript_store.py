import json

import pytest

from lecture_saver.core.models import Transcript, TranscriptSegment
from lecture_saver.core.transcript_store import TranscriptStore


def make_store(tmp_path, spans=((0, 5), (5, 10), (20, 25))) -> TranscriptStore:
    segments = [
        TranscriptSegment(id=f"segment_{i:03d}", start=s, end=e, original_text=f"line {i}")
        for i, (s, e) in enumerate(spans, start=1)
    ]
    return TranscriptStore(tmp_path / "transcript.json", Transcript(segments=segments))


class TestTimeLookup:
    """Drives the playback highlight (§12)."""

    def test_start_of_a_segment_is_inclusive(self, tmp_path):
        assert make_store(tmp_path).index_at(5.0) == 1

    def test_inside_a_segment(self, tmp_path):
        assert make_store(tmp_path).index_at(2.5) == 0

    def test_before_the_first_segment_returns_none(self, tmp_path):
        store = make_store(tmp_path, spans=((4, 9),))
        assert store.index_at(1.0) is None

    def test_a_gap_holds_the_previous_segment(self, tmp_path):
        # 10-20 s is silence. The highlight should stay on the segment that
        # ended at 10 rather than flicker off.
        assert make_store(tmp_path).index_at(15.0) == 1

    def test_past_the_end_holds_the_last_segment(self, tmp_path):
        assert make_store(tmp_path).index_at(9999.0) == 2

    def test_empty_transcript_returns_none(self, tmp_path):
        assert TranscriptStore(tmp_path / "t.json").index_at(1.0) is None

    def test_segment_at_returns_the_matching_object(self, tmp_path):
        store = make_store(tmp_path)
        assert store.segment_at(6.0).id == "segment_002"
        assert store.segment_at(-1.0) is None


class TestPersistence:
    def test_missing_file_loads_as_empty(self, tmp_path):
        store = TranscriptStore.load(tmp_path / "transcript.json")
        assert store.is_empty
        assert len(store) == 0

    def test_save_then_load_round_trip(self, tmp_path):
        store = make_store(tmp_path)
        store.replace_all(list(store.segments), language="ko")
        store.save()

        reloaded = TranscriptStore.load(tmp_path / "transcript.json")
        assert len(reloaded) == 3
        assert reloaded.language == "ko"
        assert reloaded.segments[0].original_text == "line 1"

    def test_save_is_skipped_when_nothing_changed(self, tmp_path):
        store = make_store(tmp_path)
        store.replace_all(list(store.segments))
        assert store.save() is True
        assert store.save() is False  # not dirty any more
        assert store.save(force=True) is True

    def test_saved_file_is_readable_utf8_json(self, tmp_path):
        store = TranscriptStore(tmp_path / "transcript.json")
        store.replace_all(
            [TranscriptSegment(id="segment_001", start=0, end=1, original_text="러닝레이트")]
        )
        store.save()
        raw = (tmp_path / "transcript.json").read_text(encoding="utf-8")
        assert "러닝레이트" in raw  # not escaped to \uXXXX
        assert json.loads(raw)["segments"][0]["edited_text"] is None

    def test_segments_load_sorted_even_if_written_out_of_order(self, tmp_path):
        path = tmp_path / "transcript.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "segments": [
                        {"id": "b", "start": 30, "end": 35, "original_text": "later"},
                        {"id": "a", "start": 1, "end": 5, "original_text": "earlier"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = TranscriptStore.load(path)
        assert [s.id for s in store.segments] == ["a", "b"]
        assert store.index_at(2.0) == 0


class TestReplaceAll:
    def test_marks_dirty_and_reindexes(self, tmp_path):
        store = TranscriptStore(tmp_path / "t.json")
        assert store.is_dirty is False

        store.replace_all(
            [TranscriptSegment(id="segment_001", start=40, end=45, original_text="x")]
        )
        assert store.is_dirty is True
        assert store.index_at(42.0) == 0

    def test_build_segments_assigns_sequential_ids(self, tmp_path):
        built = TranscriptStore.build_segments([(0.0, 1.0, "a"), (1.0, 2.0, "b")])
        assert [s.id for s in built] == ["segment_001", "segment_002"]
        assert all(s.edited_text is None for s in built)


def test_segments_view_cannot_mutate_the_store(tmp_path):
    store = make_store(tmp_path)
    view = store.segments
    with pytest.raises((AttributeError, TypeError)):
        view.append(TranscriptSegment(id="x", start=0, end=1, original_text="x"))  # type: ignore[attr-defined]
    assert len(store) == 3


class TestSlideLinks:
    """Alignment results are recorded through the store, never by poking segments."""

    def test_links_are_recorded_in_order(self, tmp_path):
        store = make_store(tmp_path)
        assert store.set_slide_ids([1, 1, 2]) == 3
        assert [s.slide_id for s in store.segments] == [1, 1, 2]
        assert store.is_dirty

    def test_unchanged_links_do_not_dirty_the_transcript(self, tmp_path):
        store = make_store(tmp_path)
        store.set_slide_ids([1, 1, 2])
        store.save()
        assert store.set_slide_ids([1, 1, 2]) == 0
        assert not store.is_dirty

    def test_links_never_touch_the_text(self, tmp_path):
        store = make_store(tmp_path)
        store.segments[0].edited_text = "corrected"
        store.set_slide_ids([3, None, 4])
        assert store.segments[0].original_text == "line 1"
        assert store.segments[0].effective_text == "corrected"

    def test_a_mismatched_count_is_rejected(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(ValueError):
            store.set_slide_ids([1])

    def test_links_survive_a_save_and_reload(self, tmp_path):
        store = make_store(tmp_path)
        store.set_slide_ids([1, 2, 2])
        store.save()
        reloaded = TranscriptStore.load(store.path)
        assert [s.slide_id for s in reloaded.segments] == [1, 2, 2]
