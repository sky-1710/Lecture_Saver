import json

import pytest

from lecture_saver.core.atomic_io import read_json, write_json_atomic
from lecture_saver.core.models import StageStatus, TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project, ProjectError, is_project_dir, safe_project_dirname


@pytest.fixture
def video(tmp_path) -> VideoInfo:
    path = tmp_path / "lecture01.mp4"
    path.write_bytes(b"not really a video, but it exists")
    return VideoInfo(
        path=str(path),
        duration=3612.4, width=1920, height=1080, fps=30.0,
        size_bytes=path.stat().st_size,
        container="mov,mp4", video_codec="h264", audio_codec="aac", has_audio=True,
    )


class TestSafeDirname:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Lecture 01", "Lecture 01"),
            ("ML: week 3/4", "ML_ week 3_4"),
            ("  spaced  out  ", "spaced out"),
            ("기계학습 1주차", "기계학습 1주차"),
        ],
    )
    def test_sanitises(self, name, expected):
        assert safe_project_dirname(name) == expected

    def test_windows_reserved_names_are_escaped(self):
        assert safe_project_dirname("CON") != "CON"

    def test_empty_name_gets_a_fallback(self):
        assert safe_project_dirname("///") not in ("", "___")

    def test_trailing_dots_are_stripped(self):
        # Windows silently drops trailing dots from directory names.
        assert not safe_project_dirname("lecture...").endswith(".")


class TestCreate:
    def test_creates_the_expected_layout(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "Lecture 01", video)

        assert project.project_file.is_file()
        assert project.transcript_file.is_file()
        for directory in (project.audio_dir, project.slides_dir,
                          project.exports_dir, project.logs_dir):
            assert directory.is_dir()
        assert is_project_dir(project.root)

    def test_does_not_copy_the_video(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "Lecture 01", video)
        assert project.video.path == video.path
        assert project.video.copied_into_project is False
        # No stray copy of the source file inside the project folder.
        assert not (project.root / "lecture01.mp4").exists()

    def test_a_second_project_with_the_same_name_gets_its_own_folder(self, tmp_path, video):
        parent = tmp_path / "projects"
        first = Project.create(parent, "Lecture 01", video)
        second = Project.create(parent, "Lecture 01", video)
        assert first.root != second.root
        assert second.root.is_dir()

    def test_new_project_stages_start_not_started(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        for key in ("audio", "transcription", "slides", "alignment"):
            assert project.stage_status(key) is StageStatus.NOT_STARTED


class TestOpen:
    def test_round_trip_restores_state(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "Lecture 01", video)
        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=1.0, end=4.0, original_text="안녕하세요")],
            language="ko",
        )
        project.set_stage("audio", StageStatus.DONE)
        project.save()

        reopened = Project.open(project.root)
        assert reopened.name == "Lecture 01"
        assert reopened.video.duration == pytest.approx(3612.4)
        assert reopened.stage_status("audio") is StageStatus.DONE
        assert len(reopened.transcript) == 1
        assert reopened.transcript.segments[0].original_text == "안녕하세요"
        assert reopened.transcript.language == "ko"

    def test_user_edits_survive_reopening(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=0, end=2, original_text="러닝레이트")]
        )
        project.transcript.segments[0].edited_text = "learning rate"
        project.transcript.mark_dirty()
        project.save()

        segment = Project.open(project.root).transcript.segments[0]
        assert segment.original_text == "러닝레이트"
        assert segment.edited_text == "learning rate"
        assert segment.effective_text == "learning rate"

    def test_a_non_project_folder_is_rejected(self, tmp_path):
        (tmp_path / "random").mkdir()
        with pytest.raises(ProjectError, match="not a Lecture Saver project"):
            Project.open(tmp_path / "random")

    def test_damaged_project_json_reports_clearly(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        project.project_file.write_text("{ not json", encoding="utf-8")
        with pytest.raises(ProjectError, match="damaged"):
            Project.open(project.root)

    def test_a_newer_schema_is_refused_rather_than_misread(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        data = read_json(project.project_file)
        data["schema_version"] = 99
        write_json_atomic(project.project_file, data)
        with pytest.raises(ProjectError, match="newer version"):
            Project.open(project.root)

    def test_missing_subfolders_are_recreated(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        project.slides_dir.rmdir()
        assert Project.open(project.root).slides_dir.is_dir()


class TestMissingVideo:
    def test_detected(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        assert project.video_missing is False
        video.as_path.unlink()
        assert Project.open(project.root).video_missing is True

    def test_relocation_is_persisted(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "L", video)
        moved = tmp_path / "elsewhere.mp4"
        moved.write_bytes(b"x")
        project.relocate_video(moved)
        assert Project.open(project.root).video.as_path == moved


class TestAtomicWrites:
    def test_a_failed_serialisation_leaves_the_old_file_intact(self, tmp_path):
        path = tmp_path / "data.json"
        write_json_atomic(path, {"good": 1})

        class Unserialisable:
            pass

        with pytest.raises(TypeError):
            write_json_atomic(path, {"bad": Unserialisable()})

        assert read_json(path) == {"good": 1}

    def test_no_temporary_files_are_left_behind(self, tmp_path):
        path = tmp_path / "data.json"
        write_json_atomic(path, {"a": 1})
        assert [p.name for p in tmp_path.iterdir()] == ["data.json"]

    def test_korean_is_written_unescaped(self, tmp_path):
        path = tmp_path / "data.json"
        write_json_atomic(path, {"text": "러닝레이트"})
        assert "러닝레이트" in path.read_text(encoding="utf-8")

    def test_overwrites_a_longer_existing_file_completely(self, tmp_path):
        path = tmp_path / "data.json"
        write_json_atomic(path, {"padding": "x" * 500})
        write_json_atomic(path, {"a": 1})
        assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}

    def test_a_rename_refused_for_a_moment_is_tried_again(self, tmp_path, monkeypatch):
        """An antivirus scan or a sync client holding the file briefly (Windows)."""
        import os

        from lecture_saver.core import atomic_io

        real, refusals = os.replace, []

        def busy_twice(source, target):
            if len(refusals) < 2:
                refusals.append(target)
                raise PermissionError(13, "Access is denied")
            real(source, target)

        monkeypatch.setattr(atomic_io.os, "replace", busy_twice)
        monkeypatch.setattr(atomic_io.time, "sleep", lambda _seconds: None)
        path = tmp_path / "data.json"
        write_json_atomic(path, {"a": 1})
        assert len(refusals) == 2
        assert read_json(path) == {"a": 1}

    def test_a_rename_refused_for_good_still_fails_and_cleans_up(self, tmp_path, monkeypatch):
        from lecture_saver.core import atomic_io

        def always_busy(source, target):
            raise PermissionError(13, "Access is denied")

        monkeypatch.setattr(atomic_io.os, "replace", always_busy)
        monkeypatch.setattr(atomic_io.time, "sleep", lambda _seconds: None)
        with pytest.raises(PermissionError):
            write_json_atomic(tmp_path / "data.json", {"a": 1})
        assert list(tmp_path.iterdir()) == []


class TestAlignment:
    """``Project.align`` keeps transcript links derived from the current times (§10)."""

    @pytest.fixture
    def project(self, tmp_path, video):
        from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet

        project = Project.create(tmp_path / "projects", "Lecture", video)
        project.transcript.replace_all([
            TranscriptSegment(id="segment_001", start=2.0, end=8.0, original_text="one"),
            TranscriptSegment(id="segment_002", start=28.0, end=40.0, original_text="two"),
            TranscriptSegment(id="segment_003", start=70.0, end=75.0, original_text="three"),
        ])
        project.save()
        project.set_slides(SlideSet(
            slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/002.jpg"),
                    Slide(3, "slides/003.jpg")],
            appearances=[
                SlideAppearance(1, 1, 0.0, 30.0),
                SlideAppearance(2, 2, 30.0, 60.0),
                SlideAppearance(3, 3, 60.0, 90.0),
            ],
        ))
        project.save_slides()
        return project

    def test_links_every_segment_and_marks_the_stage_done(self, project):
        alignment = project.align()
        assert [s.slide_id for s in project.transcript.segments] == [1, 2, 3]
        assert alignment.by_appearance == ((0,), (1,), (2,))
        assert project.stage_status("alignment") is StageStatus.DONE

    def test_links_are_saved_with_the_transcript(self, project):
        project.align()
        reopened = Project.open(project.root)
        assert [s.slide_id for s in reopened.transcript.segments] == [1, 2, 3]
        assert reopened.stage_status("alignment") is StageStatus.DONE

    def test_a_slide_edit_moves_the_speech_with_it(self, project):
        from lecture_saver.core.slides import delete_appearance

        project.align()
        delete_appearance(project.slides, 2)  # slide 1 now runs 0-60
        project.save_slides()
        project.align()
        assert [s.slide_id for s in project.transcript.segments] == [1, 1, 3]

    def test_realigning_an_unchanged_project_writes_nothing(self, project):
        project.align()
        before = project.transcript_file.stat().st_mtime_ns
        project.align()
        assert project.transcript_file.stat().st_mtime_ns == before
        assert not project.transcript.is_dirty

    def test_without_slides_nothing_is_linked(self, tmp_path, video):
        project = Project.create(tmp_path / "projects", "No slides", video)
        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=0, end=3, original_text="x")]
        )
        project.align()
        assert project.transcript.segments[0].slide_id is None
        assert project.stage_status("alignment") is StageStatus.NOT_STARTED

    def test_removing_the_slides_clears_stale_links(self, project):
        from lecture_saver.core.slides import SlideSet

        project.align()
        project.set_slides(SlideSet())
        project.align()
        assert all(s.slide_id is None for s in project.transcript.segments)
        assert project.stage_status("alignment") is StageStatus.NOT_STARTED
