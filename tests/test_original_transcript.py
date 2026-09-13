"""Resetting a transcript to the original transcription.

The original is Whisper's output kept by the transcription run in
``transcript_original.json``. Resetting restores it exactly - structure as well
as text - and is itself undoable. Projects transcribed before originals were kept
fall back to discarding text edits only.
"""

import pytest

from lecture_saver.core.models import StageStatus, Transcript, TranscriptSegment, VideoInfo
from lecture_saver.core.project import ORIGINAL_TRANSCRIPT_FILE, Project
from lecture_saver.media.errors import Cancelled
from lecture_saver.transcription import pipeline as pipeline_module
from lecture_saver.transcription.pipeline import TranscriptionPipeline
from lecture_saver.transcription.whisper_engine import (
    RawSegment,
    TranscriptionResult,
    WhisperOptions,
)


def whisper_output():
    texts = ["첫 문장입니다.", "러닝레이트를 보시면", "모델이 발산합니다."]
    spans = [(0.0, 4.0), (4.0, 9.0), (9.0, 15.0)]
    return [
        TranscriptSegment(id=f"segment_{i:03d}", start=a, end=b, original_text=text)
        for i, ((a, b), text) in enumerate(zip(spans, texts, strict=True), start=1)
    ]


def state(store):
    return [(s.id, s.start, s.end, s.original_text, s.edited_text) for s in store.segments]


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "lecture.mp4"
    path.write_bytes(b"x")
    return VideoInfo(path=str(path), duration=60.0, has_audio=True)


@pytest.fixture
def project(tmp_path, video):
    project = Project.create(tmp_path / "projects", "Lecture", video)
    project.transcript.replace_all(whisper_output(), language="ko")
    project.save()
    project.save_original_transcript()
    return project


def mangle(store):
    """Every kind of correction at once."""
    store.edit_text("segment_002", "learning rate를 보시면")
    store.merge_with_next("segment_001")
    store.split_segment("segment_003", len("모델이 "))
    store.insert_segment_after("segment_001", "추가한 줄")
    store.delete_segment(store.segments[-1].id)


class TestStoreReset:
    def test_reset_restores_structure_and_text_exactly(self, project):
        before = state(project.transcript)
        mangle(project.transcript)
        assert state(project.transcript) != before

        assert project.transcript.reset_to(whisper_output())
        assert state(project.transcript) == before

    def test_the_reset_can_be_undone(self, project):
        store = project.transcript
        mangle(store)
        corrected = state(store)
        store.reset_to(whisper_output())

        assert store.undo_label == "Reset to original"
        store.undo()
        assert state(store) == corrected
        store.redo()
        assert state(store) == state(project.transcript)

    def test_resetting_an_unchanged_transcript_does_nothing(self, project):
        assert not project.transcript.reset_to(whisper_output())
        assert not project.transcript.can_undo

    def test_edits_in_the_source_are_never_carried_over(self, project):
        source = whisper_output()
        source[0].edited_text = "should not survive"
        project.transcript.edit_text("segment_002", "changed")
        project.transcript.reset_to(source)
        assert all(s.edited_text is None for s in project.transcript.segments)

    def test_restore_all_originals_discards_text_edits_as_one_step(self, project):
        store = project.transcript
        store.edit_text("segment_001", "a")
        store.edit_text("segment_003", "b")
        assert store.restore_all_originals() == 2
        assert all(s.edited_text is None for s in store.segments)
        store.undo()
        assert [s.edited_text for s in store.segments] == ["a", None, "b"]


class TestProjectReset:
    def test_the_kept_original_has_no_edits_or_links(self, project):
        project.transcript.edit_text("segment_001", "edited")
        project.transcript.set_slide_ids([1, 1, 2])
        project.save_original_transcript()

        original = project.load_original_transcript()
        assert original is not None
        assert all(s.edited_text is None and s.slide_id is None for s in original.segments)

    def test_full_reset_from_the_kept_original(self, project):
        before = state(project.transcript)
        mangle(project.transcript)
        assert not project.is_original_transcript()

        assert project.reset_transcript()
        assert state(project.transcript) == before
        assert project.is_original_transcript()

    def test_the_original_survives_reopening(self, project):
        mangle(project.transcript)
        project.save()
        reopened = Project.open(project.root)
        assert reopened.has_original_transcript
        reopened.reset_transcript()
        assert [s.original_text for s in reopened.transcript.segments] == [
            s.original_text for s in whisper_output()
        ]

    def test_without_a_kept_original_only_text_edits_are_discarded(self, project):
        project.original_transcript_file.unlink()
        store = project.transcript
        store.edit_text("segment_002", "edited")
        store.merge_with_next("segment_001")

        assert not project.has_original_transcript
        assert project.reset_transcript()
        assert len(store) == 2, "structure cannot be recovered without the original"
        assert all(s.edited_text is None for s in store.segments)

    def test_an_unreadable_original_falls_back_rather_than_failing(self, project):
        project.original_transcript_file.write_text("{ not json", encoding="utf-8")
        assert project.load_original_transcript() is None
        project.transcript.edit_text("segment_001", "x")
        assert project.reset_transcript()

    def test_nothing_to_reset_is_reported(self, project):
        assert project.is_original_transcript()
        assert not project.reset_transcript()


class FakeEngine:
    """Stands in for faster-whisper: streams fixed segments, no model needed."""

    count = 3

    def __init__(self, options):
        self.options = options

    def load(self, on_status=None):
        pass

    def unload(self):
        pass

    def transcribe(self, audio_path, *, duration=0.0, on_segment=None, should_cancel=None):
        raw = [RawSegment(float(i), float(i) + 0.9, f"문장 {i}") for i in range(self.count)]
        for index, segment in enumerate(raw):
            if on_segment:
                on_segment(segment, (index + 1) / len(raw))
        return TranscriptionResult(raw, "ko", "cpu", "int8")


@pytest.fixture
def fake_run(monkeypatch, tmp_path, video):
    monkeypatch.setattr(pipeline_module, "WhisperEngine", FakeEngine)
    monkeypatch.setattr(TranscriptionPipeline, "_prepare", lambda self: None)
    monkeypatch.setattr(TranscriptionPipeline, "_ensure_audio", lambda self: None)
    project = Project.create(tmp_path / "run", "Run", video)
    return project


class TestTranscriptionKeepsTheOriginal:
    def test_a_finished_run_writes_the_original(self, fake_run):
        TranscriptionPipeline(fake_run, WhisperOptions()).run()

        assert (fake_run.root / ORIGINAL_TRANSCRIPT_FILE).is_file()
        original = fake_run.load_original_transcript()
        assert [s.original_text for s in original.segments] == ["문장 0", "문장 1", "문장 2"]
        assert fake_run.stage_status("transcription") is StageStatus.DONE

    def test_transcribing_again_replaces_the_original(self, fake_run, monkeypatch):
        TranscriptionPipeline(fake_run, WhisperOptions()).run()
        monkeypatch.setattr(FakeEngine, "count", 5)
        TranscriptionPipeline(fake_run, WhisperOptions()).run()
        assert len(fake_run.load_original_transcript().segments) == 5

    def test_a_cancelled_run_keeps_the_original_of_what_was_kept(self, fake_run, monkeypatch):
        monkeypatch.setattr(FakeEngine, "count", pipeline_module.FLUSH_EVERY + 5)
        calls = {"n": 0}

        def cancel_after_engine():
            calls["n"] += 1
            return calls["n"] > 2  # prepare and load pass; the check after transcribing cancels

        with pytest.raises(Cancelled):
            TranscriptionPipeline(
                fake_run, WhisperOptions(), should_cancel=cancel_after_engine
            ).run()

        original = fake_run.load_original_transcript()
        assert original is not None
        assert len(original.segments) == pipeline_module.FLUSH_EVERY
        assert fake_run.transcript.matches(original.segments)

    def test_failing_to_keep_the_original_does_not_fail_transcription(
        self, fake_run, monkeypatch
    ):
        from lecture_saver.core.project import ProjectError

        def refuse(self):
            raise ProjectError("disk full")

        monkeypatch.setattr(Project, "save_original_transcript", refuse)
        TranscriptionPipeline(fake_run, WhisperOptions()).run()
        assert fake_run.stage_status("transcription") is StageStatus.DONE


def test_the_original_file_is_plain_json(project):
    data = Transcript.from_dict(__import__("json").loads(
        project.original_transcript_file.read_text(encoding="utf-8")
    ))
    assert len(data.segments) == 3
