"""Transcript editing (§13, §14, §33).

The rules every view and export depends on: edits never destroy Whisper's
output, every operation is undoable, and structure changes keep timestamps and
originals truthful.
"""

import pytest

from lecture_saver.core.models import Transcript, TranscriptSegment
from lecture_saver.core.transcript_store import TranscriptEditError, TranscriptStore


def make_store(tmp_path, *items):
    """items: (start, end, original[, edited]) tuples."""
    items = items or (
        (0.0, 4.0, "Today we begin with the basics."),
        (4.0, 10.0, "여기서 러닝레이트를 보시면"),
        (12.0, 20.0, "If this value is too large, the model can diverge."),
    )
    segments = [
        TranscriptSegment(
            id=f"segment_{i:03d}", start=item[0], end=item[1], original_text=item[2],
            edited_text=item[3] if len(item) > 3 else None,
        )
        for i, item in enumerate(items, start=1)
    ]
    return TranscriptStore(tmp_path / "transcript.json", Transcript(segments=segments))


def texts(store):
    return [s.effective_text for s in store.segments]


def ids(store):
    return [s.id for s in store.segments]


class TestEditText:
    def test_an_edit_goes_to_edited_text_and_keeps_the_original(self, tmp_path):
        store = make_store(tmp_path)
        assert store.edit_text("segment_002", "여기서 learning rate를 보시면")
        segment = store.get("segment_002")
        assert segment.original_text == "여기서 러닝레이트를 보시면"
        assert segment.edited_text == "여기서 learning rate를 보시면"
        assert segment.effective_text == "여기서 learning rate를 보시면"
        assert store.is_dirty

    def test_typing_the_original_back_clears_the_edit(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "changed")
        store.edit_text("segment_001", "Today we begin with the basics.")
        assert store.get("segment_001").edited_text is None
        assert not store.get("segment_001").is_edited

    def test_blanking_a_line_is_a_real_edit(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "")
        assert store.get("segment_001").edited_text == ""
        assert store.get("segment_001").effective_text == ""

    def test_an_unchanged_edit_records_nothing(self, tmp_path):
        store = make_store(tmp_path)
        assert not store.edit_text("segment_001", "Today we begin with the basics.")
        assert not store.can_undo
        assert not store.is_dirty

    def test_restore_original(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_003", "corrected")
        assert store.restore_original("segment_003")
        assert store.get("segment_003").effective_text.startswith("If this value")
        assert not store.restore_original("segment_003")

    def test_editing_a_missing_segment_is_reported(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(TranscriptEditError):
            store.edit_text("segment_999", "x")


class TestTypingIsOneUndoStep:
    def test_consecutive_typing_in_one_segment_undoes_together(self, tmp_path):
        store = make_store(tmp_path)
        for partial in ("T", "To", "Tod", "Toda", "Today!"):
            store.edit_text("segment_001", partial, coalesce=True)
        store.end_typing()

        assert store.undo() == "Edit text"
        assert texts(store)[0] == "Today we begin with the basics."
        assert not store.can_undo

    def test_typing_in_another_segment_is_a_separate_step(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "one", coalesce=True)
        store.edit_text("segment_002", "two", coalesce=True)
        store.end_typing()

        store.undo()
        assert texts(store)[:2] == ["one", "여기서 러닝레이트를 보시면"]

    def test_a_session_that_ends_where_it_began_leaves_no_step(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "draft", coalesce=True)
        store.edit_text("segment_001", "Today we begin with the basics.", coalesce=True)
        store.end_typing()
        assert not store.can_undo

    def test_undo_during_typing_takes_back_the_typing(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "draft", coalesce=True)
        store.undo()
        assert texts(store)[0] == "Today we begin with the basics."


class TestDeleteAndInsert:
    def test_delete(self, tmp_path):
        store = make_store(tmp_path)
        removed = store.delete_segment("segment_002")
        assert removed.id == "segment_002"
        assert ids(store) == ["segment_001", "segment_003"]

    def test_insert_fills_a_gap_after_a_segment(self, tmp_path):
        store = make_store(tmp_path)
        created = store.insert_segment_after("segment_002", "missed words")
        assert (created.start, created.end) == (10.0, 12.0)
        assert ids(store) == ["segment_001", "segment_002", created.id, "segment_003"]
        assert created.original_text == ""
        assert created.effective_text == "missed words"

    def test_insert_without_a_gap_takes_a_short_default(self, tmp_path):
        store = make_store(tmp_path)
        created = store.insert_segment_after("segment_001")
        assert created.start == 4.0
        assert created.end == pytest.approx(6.0)

    def test_insert_after_the_last_segment(self, tmp_path):
        store = make_store(tmp_path)
        created = store.insert_segment_after("segment_003")
        assert ids(store)[-1] == created.id
        assert created.start == 20.0

    def test_insert_at_the_very_start(self, tmp_path):
        store = make_store(tmp_path, (5.0, 9.0, "late start"))
        created = store.insert_segment_after(None, "intro")
        assert ids(store)[0] == created.id
        assert (created.start, created.end) == (0.0, 5.0)

    def test_new_ids_never_collide(self, tmp_path):
        store = make_store(tmp_path)
        first = store.insert_segment_after("segment_001")
        second = store.insert_segment_after(first.id)
        assert len(set(ids(store))) == len(ids(store))
        assert first.id != second.id


class TestMerge:
    def test_merge_joins_text_times_and_originals(self, tmp_path):
        store = make_store(tmp_path)
        merged = store.merge_with_next("segment_001")
        assert merged.id == "segment_001"
        assert (merged.start, merged.end) == (0.0, 10.0)
        assert merged.original_text == "Today we begin with the basics. 여기서 러닝레이트를 보시면"
        assert merged.edited_text is None
        assert len(store) == 2

    def test_merge_keeps_edits_and_the_originals_behind_them(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_002", "여기서 learning rate를 보시면")
        merged = store.merge_with_next("segment_001")
        assert merged.effective_text == (
            "Today we begin with the basics. 여기서 learning rate를 보시면"
        )
        assert "러닝레이트" in merged.original_text

        store.restore_original("segment_001")
        assert "러닝레이트" in store.get("segment_001").effective_text

    def test_the_last_segment_cannot_merge_forward(self, tmp_path):
        store = make_store(tmp_path)
        with pytest.raises(TranscriptEditError):
            store.merge_with_next("segment_003")


class TestSplit:
    def test_split_divides_text_and_time_in_proportion(self, tmp_path):
        store = make_store(tmp_path, (10.0, 20.0, "first half second half"))
        left, right = store.split_segment("segment_001", len("first half "))
        assert (left.effective_text, right.effective_text) == ("first half", "second half")
        assert left.end == right.start
        assert left.start == 10.0 and right.end == 20.0
        assert 10.0 < left.end < 20.0
        assert (left.original_text, right.original_text) == ("first half", "second half")
        assert not left.is_edited and not right.is_edited

    def test_splitting_an_edited_segment_divides_the_original_at_a_word(self, tmp_path):
        store = make_store(tmp_path, (0.0, 8.0, "alpha beta gamma delta", "ALPHA BETA GAMMA DELTA"))
        left, right = store.split_segment("segment_001", len("ALPHA BETA "))
        assert (left.effective_text, right.effective_text) == ("ALPHA BETA", "GAMMA DELTA")
        assert (left.original_text, right.original_text) == ("alpha beta", "gamma delta")

    def test_the_right_half_gets_a_new_id_the_left_keeps_its_own(self, tmp_path):
        store = make_store(tmp_path, (0.0, 8.0, "one two"))
        left, right = store.split_segment("segment_001", 4)
        assert left.id == "segment_001"
        assert right.id != left.id

    @pytest.mark.parametrize("offset", [0, 3, 100])
    def test_splitting_at_an_edge_is_refused(self, tmp_path, offset):
        store = make_store(tmp_path, (0.0, 8.0, "one"))
        with pytest.raises(TranscriptEditError):
            store.split_segment("segment_001", offset)

    def test_korean_text_splits_cleanly(self, tmp_path):
        store = make_store(tmp_path, (0.0, 6.0, "학습률이 크면 발산합니다"))
        left, right = store.split_segment("segment_001", len("학습률이 크면 "))
        assert (left.effective_text, right.effective_text) == ("학습률이 크면", "발산합니다")


class TestUndoRedo:
    @pytest.mark.parametrize(
        "operation",
        [
            lambda s: s.edit_text("segment_002", "changed"),
            lambda s: s.delete_segment("segment_002"),
            lambda s: s.insert_segment_after("segment_002", "new"),
            lambda s: s.merge_with_next("segment_001"),
            lambda s: s.split_segment("segment_003", len("If this value ")),
            lambda s: s.replace_everywhere("the", "a"),
        ],
        ids=["edit", "delete", "insert", "merge", "split", "replace-all"],
    )
    def test_every_operation_undoes_exactly_and_redoes(self, tmp_path, operation):
        store = make_store(tmp_path)
        before = [(s.id, s.start, s.end, s.original_text, s.edited_text) for s in store.segments]
        operation(store)
        after = [(s.id, s.start, s.end, s.original_text, s.edited_text) for s in store.segments]
        assert after != before

        store.undo()
        assert [(s.id, s.start, s.end, s.original_text, s.edited_text)
                for s in store.segments] == before
        store.redo()
        assert [(s.id, s.start, s.end, s.original_text, s.edited_text)
                for s in store.segments] == after

    def test_a_new_edit_clears_redo(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "a")
        store.undo()
        assert store.can_redo
        store.edit_text("segment_002", "b")
        assert not store.can_redo

    def test_labels_name_the_step(self, tmp_path):
        store = make_store(tmp_path)
        store.merge_with_next("segment_001")
        assert store.undo_label == "Merge segments"
        store.undo()
        assert store.redo_label == "Merge segments"

    def test_undo_with_nothing_to_undo_is_harmless(self, tmp_path):
        store = make_store(tmp_path)
        assert store.undo() is None
        assert store.redo() is None

    def test_undo_does_not_leak_later_edits_into_the_snapshot(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "first")
        store.edit_text("segment_001", "second")
        store.undo()
        assert texts(store)[0] == "first"

    def test_a_new_transcription_clears_the_history(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_001", "edited")
        store.replace_all(
            [TranscriptSegment(id="segment_001", start=0, end=1, original_text="new")]
        )
        assert not store.can_undo

    def test_each_change_bumps_the_revision(self, tmp_path):
        store = make_store(tmp_path)
        start = store.revision
        store.edit_text("segment_001", "x")
        store.undo()
        assert store.revision == start + 2


class TestFindAndReplace:
    def test_find_is_case_insensitive_by_default(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "The model"), (1, 2, "the THE the"))
        matches = store.find("the")
        assert [(m.segment_index, m.start) for m in matches] == [(0, 0), (1, 0), (1, 4), (1, 8)]

    def test_find_can_match_case(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "The model"), (1, 2, "the THE the"))
        assert len(store.find("THE", case_sensitive=True)) == 1

    def test_find_searches_the_edited_text(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "러닝레이트", "learning rate"))
        assert store.find("learning")
        assert not store.find("러닝레이트")

    def test_find_treats_the_query_literally(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "cost (J) = 0.5 * x"))
        assert len(store.find("(J)")) == 1
        assert len(store.find("*")) == 1

    def test_an_empty_query_finds_nothing(self, tmp_path):
        assert make_store(tmp_path).find("") == []

    def test_replace_one_match(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "러닝레이트 and 러닝레이트"))
        match = store.find("러닝레이트")[1]
        assert store.replace_match(match, "러닝레이트", "learning rate")
        assert texts(store) == ["러닝레이트 and learning rate"]
        assert store.get("segment_001").original_text == "러닝레이트 and 러닝레이트"

    def test_a_stale_match_is_not_replaced(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "abc abc"))
        match = store.find("abc")[1]
        store.edit_text("segment_001", "xyz")
        assert not store.replace_match(match, "abc", "!")
        assert texts(store) == ["xyz"]

    def test_replace_everywhere_is_one_undo_step(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "러닝레이트"), (1, 2, "또 러닝레이트, 러닝레이트"))
        assert store.replace_everywhere("러닝레이트", "learning rate") == 3
        assert texts(store) == ["learning rate", "또 learning rate, learning rate"]
        store.undo()
        assert texts(store) == ["러닝레이트", "또 러닝레이트, 러닝레이트"]
        assert not store.can_undo

    def test_replacement_text_is_literal(self, tmp_path):
        store = make_store(tmp_path, (0, 1, "a b"))
        store.replace_everywhere("b", r"\1 $0")
        assert texts(store) == [r"a \1 $0"]

    def test_replacing_nothing_records_nothing(self, tmp_path):
        store = make_store(tmp_path)
        assert store.replace_everywhere("zzz", "y") == 0
        assert not store.can_undo


class TestPersistence:
    def test_edits_survive_saving_and_reloading(self, tmp_path):
        store = make_store(tmp_path)
        store.edit_text("segment_002", "learning rate")
        store.merge_with_next("segment_002")
        store.save()

        reloaded = TranscriptStore.load(store.path)
        segment = reloaded.get("segment_002")
        assert segment.effective_text == (
            "learning rate If this value is too large, the model can diverge."
        )
        assert "러닝레이트" in segment.original_text
