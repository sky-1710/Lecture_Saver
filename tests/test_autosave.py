"""Debounced autosave (§15, §33)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtTest import QTest  # noqa: E402

from lecture_saver.core.models import Transcript, TranscriptSegment  # noqa: E402
from lecture_saver.core.transcript_store import TranscriptStore  # noqa: E402
from lecture_saver.ui.autosave import DEFAULT_DELAY_MS, Autosave  # noqa: E402


class CountingSave:
    def __init__(self, result=True, error=None):
        self.calls = 0
        self.result = result
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_the_default_delay_is_within_the_specified_range():
    assert 500 <= DEFAULT_DELAY_MS <= 1000


def test_a_burst_of_changes_writes_once_after_the_pause(qapp):
    save = CountingSave()
    autosave = Autosave(save, delay_ms=60)
    for _ in range(10):
        autosave.schedule()
        QTest.qWait(10)
    assert save.calls == 0, "must not write while changes keep arriving"

    QTest.qWait(150)
    assert save.calls == 1


def test_nothing_is_written_without_a_change(qapp):
    save = CountingSave()
    Autosave(save, delay_ms=20)
    QTest.qWait(80)
    assert save.calls == 0


def test_flush_writes_immediately_and_cancels_the_timer(qapp):
    save = CountingSave()
    autosave = Autosave(save, delay_ms=5_000)
    autosave.schedule()
    assert autosave.pending
    assert autosave.flush()
    assert save.calls == 1
    assert not autosave.pending


def test_saved_is_only_announced_when_something_was_written(qapp):
    announced = []
    autosave = Autosave(CountingSave(result=False), delay_ms=10)
    autosave.saved.connect(lambda: announced.append(True))
    autosave.flush()
    assert announced == []


def test_a_failed_write_is_reported_not_raised(qapp):
    messages = []
    autosave = Autosave(CountingSave(error=OSError("disk full")), delay_ms=10)
    autosave.failed.connect(messages.append)
    assert autosave.flush() is False
    assert messages and "disk full" in messages[0]


def test_with_the_real_store_edits_reach_the_file(qapp, tmp_path):
    store = TranscriptStore(
        tmp_path / "transcript.json",
        Transcript(segments=[
            TranscriptSegment(id="segment_001", start=0, end=1, original_text="a"),
        ]),
    )
    store.save(force=True)
    autosave = Autosave(store.save, delay_ms=30)

    store.edit_text("segment_001", "edited", coalesce=True)
    autosave.schedule()
    QTest.qWait(120)

    assert not store.is_dirty
    assert TranscriptStore.load(store.path).get("segment_001").effective_text == "edited"
