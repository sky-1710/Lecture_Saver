import pytest

from lecture_saver.core.models import (
    SCHEMA_VERSION,
    ProjectMeta,
    StageState,
    StageStatus,
    Transcript,
    TranscriptionSettings,
    TranscriptSegment,
    VideoInfo,
    make_segment_id,
)


def segment(**kwargs) -> TranscriptSegment:
    defaults = {
        "id": "segment_001",
        "start": 12.34,
        "end": 16.21,
        "original_text": "여기서 러닝레이트를 보시면",
    }
    return TranscriptSegment(**{**defaults, **kwargs})


class TestEffectiveText:
    """The §14 single-source-of-truth rule."""

    def test_falls_back_to_original_when_never_edited(self):
        assert segment().effective_text == "여기서 러닝레이트를 보시면"

    def test_edited_text_wins(self):
        seg = segment(edited_text="여기서 learning rate를 보시면")
        assert seg.effective_text == "여기서 learning rate를 보시면"

    def test_editing_never_destroys_the_original(self):
        seg = segment()
        seg.edited_text = "corrected"
        assert seg.original_text == "여기서 러닝레이트를 보시면"

    def test_restoring_the_original_is_setting_edited_text_to_none(self):
        seg = segment(edited_text="corrected")
        seg.edited_text = None
        assert seg.effective_text == seg.original_text

    def test_empty_edit_is_a_real_edit_not_a_reset(self):
        # A user deliberately blanking a line must not silently resurrect the
        # Whisper output.
        seg = segment(edited_text="")
        assert seg.effective_text == ""
        assert seg.is_edited is True

    def test_is_edited_reflects_none_not_equality(self):
        assert segment().is_edited is False
        assert segment(edited_text="여기서 러닝레이트를 보시면").is_edited is True


class TestSegmentGeometry:
    def test_duration(self):
        assert segment(start=10, end=15).duration == pytest.approx(5.0)

    def test_duration_never_negative(self):
        assert segment(start=15, end=10).duration == 0.0

    @pytest.mark.parametrize(
        ("seg_start", "seg_end", "win_start", "win_end", "expected"),
        [
            (10, 20, 0, 30, 10.0),   # fully inside
            (10, 20, 15, 30, 5.0),   # partial
            (10, 20, 20, 30, 0.0),   # touching, exclusive end
            (10, 20, 25, 30, 0.0),   # disjoint
            (10, 20, 12, 14, 2.0),   # window inside segment
        ],
    )
    def test_overlap(self, seg_start, seg_end, win_start, win_end, expected):
        seg = segment(start=seg_start, end=seg_end)
        assert seg.overlap_with(win_start, win_end) == pytest.approx(expected)


class TestSegmentSerialisation:
    def test_round_trip_preserves_everything(self):
        seg = segment(edited_text="corrected", slide_id=8)
        assert TranscriptSegment.from_dict(seg.to_dict()) == seg

    def test_null_edited_text_survives_the_round_trip(self):
        restored = TranscriptSegment.from_dict(segment().to_dict())
        assert restored.edited_text is None

    def test_missing_optional_fields_default_safely(self):
        restored = TranscriptSegment.from_dict(
            {"id": "segment_001", "start": 0, "end": 1, "original_text": "hi"}
        )
        assert restored.edited_text is None
        assert restored.slide_id is None


def test_make_segment_id_is_zero_padded_and_sorts():
    ids = [make_segment_id(i) for i in (1, 2, 10)]
    assert ids == ["segment_001", "segment_002", "segment_010"]
    assert sorted(ids) == ids


class TestTranscript:
    def test_round_trip(self):
        transcript = Transcript(
            segments=[segment(id="segment_002", start=20, end=25, original_text="b"), segment()],
            language="ko",
        )
        restored = Transcript.from_dict(transcript.to_dict())
        assert restored.language == "ko"
        assert [s.id for s in restored.segments] == ["segment_002", "segment_001"]

    def test_sort_orders_by_start_time(self):
        transcript = Transcript(
            segments=[segment(id="b", start=20, end=25), segment(id="a", start=5, end=9)]
        )
        transcript.sort()
        assert [s.id for s in transcript.segments] == ["a", "b"]

    def test_schema_version_is_recorded(self):
        assert Transcript().to_dict()["schema_version"] == SCHEMA_VERSION


class TestVideoInfo:
    def test_round_trip(self, tmp_path):
        info = VideoInfo(
            path=str(tmp_path / "lecture01.mp4"),
            duration=3612.4, width=1920, height=1080, fps=29.97,
            size_bytes=123, container="mov,mp4", video_codec="h264",
            audio_codec="aac", has_audio=True,
        )
        assert VideoInfo.from_dict(info.to_dict()) == info

    def test_missing_file_is_detected(self, tmp_path):
        assert VideoInfo(path=str(tmp_path / "gone.mp4")).exists is False


class TestStageState:
    def test_set_records_a_timestamp(self):
        state = StageState()
        assert state.updated_at is None
        state.set(StageStatus.RUNNING, "working")
        assert state.status is StageStatus.RUNNING
        assert state.message == "working"
        assert state.updated_at is not None

    def test_unknown_status_from_disk_degrades_safely(self):
        assert StageState.from_dict({"status": "nonsense"}).status is StageStatus.NOT_STARTED


class TestProjectMeta:
    def test_round_trip(self, tmp_path):
        meta = ProjectMeta(name="lecture_01", video=VideoInfo(path=str(tmp_path / "v.mp4")))
        meta.stage("audio").set(StageStatus.DONE)
        meta.transcription = TranscriptionSettings(model_size="medium", language="ko")

        restored = ProjectMeta.from_dict(meta.to_dict())
        assert restored.name == "lecture_01"
        assert restored.stage("audio").status is StageStatus.DONE
        assert restored.transcription.model_size == "medium"
        assert restored.project_id == meta.project_id

    def test_phase_three_and_four_stages_exist_but_are_not_started(self):
        meta = ProjectMeta(name="x", video=VideoInfo(path="x.mp4"))
        assert meta.stage("slides").status is StageStatus.NOT_STARTED
        assert meta.stage("alignment").status is StageStatus.NOT_STARTED

    def test_unknown_stage_keys_from_a_newer_version_are_preserved(self):
        meta = ProjectMeta(name="x", video=VideoInfo(path="x.mp4"))
        data = meta.to_dict()
        data["stages"]["future_stage"] = {"status": "done"}
        assert ProjectMeta.from_dict(data).stage("future_stage").status is StageStatus.DONE
