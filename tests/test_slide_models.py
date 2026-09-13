"""Slide data models, options, and project save/load of detected slides."""

import json

import pytest

from lecture_saver.core.models import StageStatus, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slide_options import SlideDetectionOptions
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet


@pytest.fixture
def video(tmp_path) -> VideoInfo:
    path = tmp_path / "lecture.mp4"
    path.write_bytes(b"placeholder")
    return VideoInfo(path=str(path), duration=600.0, has_audio=True)


def sample_slide_set() -> SlideSet:
    return SlideSet.build(
        slides=[
            Slide(slide_id=1, image="slides/001.jpg", fingerprint="a1b2c3d4e5f60718"),
            Slide(slide_id=2, image="slides/002.jpg", fingerprint="00ff00ff00ff00ff"),
        ],
        appearances=[
            SlideAppearance(appearance_id=1, slide_id=1, start=0.0, end=83.4),
            SlideAppearance(appearance_id=2, slide_id=2, start=83.4, end=210.0),
        ],
        video_duration=210.0,
        options=SlideDetectionOptions(),
    )


class TestSlideAppearance:
    def test_duration(self):
        assert SlideAppearance(1, 1, 10.0, 25.0).duration == pytest.approx(15.0)

    def test_duration_is_never_negative(self):
        assert SlideAppearance(1, 1, 25.0, 10.0).duration == 0.0

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(9.9, False), (10.0, True), (17.0, True), (25.0, False), (30.0, False)],
    )
    def test_contains_is_half_open(self, seconds, expected):
        """[start, end) so two adjacent appearances never both claim a moment."""
        assert SlideAppearance(1, 1, 10.0, 25.0).contains(seconds) is expected

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [(0, 100, 15.0), (12, 20, 8.0), (25, 40, 0.0), (0, 10, 0.0), (20, 30, 5.0)],
    )
    def test_overlap(self, start, end, expected):
        assert SlideAppearance(1, 1, 10.0, 25.0).overlap_with(start, end) == pytest.approx(
            expected
        )

    def test_round_trip(self):
        appearance = SlideAppearance(appearance_id=12, slide_id=6, start=751.0, end=845.2)
        assert SlideAppearance.from_dict(appearance.to_dict()) == appearance


class TestSlide:
    def test_round_trip(self):
        slide = Slide(slide_id=8, image="slides/008.jpg", fingerprint="deadbeefdeadbeef")
        assert Slide.from_dict(slide.to_dict()) == slide

    def test_image_path_is_resolved_against_the_project(self, tmp_path):
        slide = Slide(slide_id=1, image="slides/001.jpg")
        assert slide.image_path(tmp_path) == tmp_path / "slides" / "001.jpg"

    def test_a_slide_without_an_image_round_trips(self):
        """A frame that could not be captured still keeps its record."""
        slide = Slide(slide_id=3, image="", fingerprint="")
        assert Slide.from_dict(slide.to_dict()) == slide


class TestSlideSet:
    def test_round_trip(self):
        original = sample_slide_set()
        restored = SlideSet.from_dict(original.to_dict())

        assert len(restored) == 2
        assert restored.video_duration == pytest.approx(210.0)
        assert restored.slides[1].fingerprint == "00ff00ff00ff00ff"
        assert restored.appearances[0].end == pytest.approx(83.4)

    def test_lookup_by_time(self):
        slide_set = sample_slide_set()
        assert slide_set.appearance_at(50.0).appearance_id == 1
        assert slide_set.appearance_at(83.4).appearance_id == 2
        assert slide_set.appearance_at(200.0).appearance_id == 2

    def test_time_past_the_end_holds_the_last_slide(self):
        assert sample_slide_set().appearance_at(9999.0).appearance_id == 2

    def test_lookup_before_the_first_slide_returns_nothing(self):
        slide_set = SlideSet(
            appearances=[SlideAppearance(appearance_id=1, slide_id=1, start=10.0, end=20.0)]
        )
        assert slide_set.appearance_at(1.0) is None

    def test_lookup_on_an_empty_set_returns_nothing(self):
        assert SlideSet().appearance_at(5.0) is None

    def test_slide_for_appearance(self):
        slide_set = sample_slide_set()
        slide = slide_set.slide_for(slide_set.appearances[1])
        assert slide is not None
        assert slide.image == "slides/002.jpg"

    def test_a_missing_slide_record_is_reported_as_none(self):
        slide_set = SlideSet(
            appearances=[SlideAppearance(appearance_id=1, slide_id=99, start=0.0, end=1.0)]
        )
        assert slide_set.slide_for(slide_set.appearances[0]) is None

    def test_appearances_load_sorted(self):
        restored = SlideSet.from_dict(
            {
                "appearances": [
                    {"appearance_id": 2, "slide_id": 2, "start": 80.0, "end": 120.0},
                    {"appearance_id": 1, "slide_id": 1, "start": 0.0, "end": 80.0},
                ]
            }
        )
        assert [a.appearance_id for a in restored.appearances] == [1, 2]

    def test_the_options_used_are_recorded(self):
        stored = sample_slide_set().to_dict()["options"]
        assert stored["normal_transition"]["min_content_replaced"] == (
            SlideDetectionOptions().normal_transition.min_content_replaced
        )

    def test_several_appearances_may_share_one_slide(self):
        """The model Phase 7 needs: 5 -> 6 -> 7 -> 6 (§9, §12)."""
        slide_set = SlideSet(
            slides=[Slide(slide_id=6, image="slides/006.jpg")],
            appearances=[
                SlideAppearance(appearance_id=1, slide_id=6, start=0.0, end=10.0),
                SlideAppearance(appearance_id=2, slide_id=6, start=50.0, end=60.0),
            ],
        )
        restored = SlideSet.from_dict(slide_set.to_dict())

        assert len(restored.slides) == 1
        assert len(restored.appearances) == 2
        assert all(r.slide_id == 6 for r in restored.appearances)
        assert restored.slide_for(restored.appearances[1]).image == "slides/006.jpg"


class TestDetectionOptions:
    """Settings are grouped by what they control, and must survive a round trip."""

    GROUPS = (
        "sampling",
        "normal_transition",
        "strong_global_transition",
        "annotation_filter",
        "annotation_snapshots",
        "busy_region_mask",
        "timing",
        "image",
    )

    def test_the_groups_are_stable(self):
        assert tuple(SlideDetectionOptions().to_dict()) == self.GROUPS

    def test_round_trip(self):
        options = SlideDetectionOptions()
        options.normal_transition.min_content_replaced = 0.42
        options.strong_global_transition.min_low_frequency_change = 0.31
        assert SlideDetectionOptions.from_dict(options.to_dict()) == options

    def test_defaults_when_absent(self):
        assert SlideDetectionOptions.from_dict(None) == SlideDetectionOptions()
        assert SlideDetectionOptions.from_dict({}) == SlideDetectionOptions()

    def test_unknown_keys_from_a_newer_version_are_ignored(self):
        options = SlideDetectionOptions.from_dict(
            {"normal_transition": {"min_content_replaced": 0.3, "future": 1},
             "invented_group": {"x": 1}}
        )
        assert options.normal_transition.min_content_replaced == 0.3

    def test_a_flat_settings_file_from_an_earlier_version_still_loads(self):
        options = SlideDetectionOptions.from_dict(
            {"sample_fps": 2.0, "min_content_replaced": 0.4, "image_max_width": 640}
        )
        assert options.sampling.sample_fps == 2.0
        assert options.normal_transition.min_content_replaced == 0.4
        assert options.image.max_width == 640

    def test_cell_count(self):
        options = SlideDetectionOptions()
        options.sampling.grid_cols, options.sampling.grid_rows = 8, 5
        assert options.cell_count == 40

    def test_sample_signature_tracks_only_sampling_parameters(self):
        """Changing a threshold must not invalidate the descriptor cache."""
        base = SlideDetectionOptions()

        retuned = SlideDetectionOptions()
        retuned.normal_transition.min_content_replaced = 0.9
        retuned.timing.min_slide_seconds = 30.0
        retuned.annotation_filter.max_stroke_thickness = 0.2
        assert retuned.sample_signature == base.sample_signature

        resampled = SlideDetectionOptions()
        resampled.sampling.sample_fps = 4.0
        assert resampled.sample_signature != base.sample_signature


class TestProjectPersistence:
    def test_a_new_project_has_no_slides(self, tmp_path, video):
        project = Project.create(tmp_path / "p", "L", video)
        assert project.has_slides is False
        assert len(project.slides) == 0
        assert project.stage_status("slides") is StageStatus.NOT_STARTED

    def test_slides_survive_a_reopen(self, tmp_path, video):
        project = Project.create(tmp_path / "p", "L", video)
        project.set_slides(sample_slide_set())
        project.set_stage("slides", StageStatus.DONE)
        project.save_slides()

        reopened = Project.open(project.root)
        assert reopened.has_slides
        assert len(reopened.slides) == 2
        assert reopened.slides.appearances[1].start == pytest.approx(83.4)
        assert reopened.slides.slides[0].fingerprint == "a1b2c3d4e5f60718"
        assert reopened.stage_status("slides") is StageStatus.DONE

    def test_slides_json_is_written_where_expected(self, tmp_path, video):
        project = Project.create(tmp_path / "p", "L", video)
        project.set_slides(sample_slide_set())
        project.save_slides()

        assert project.slides_file.name == "slides.json"
        data = json.loads(project.slides_file.read_text(encoding="utf-8"))
        assert len(data["appearances"]) == 2
        assert data["slides"][0]["image"] == "slides/001.jpg"

    def test_a_damaged_slides_file_costs_a_re_detection_not_the_project(
        self, tmp_path, video
    ):
        project = Project.create(tmp_path / "p", "L", video)
        project.set_slides(sample_slide_set())
        project.save_slides()
        project.slides_file.write_text("{ not json", encoding="utf-8")

        reopened = Project.open(project.root)
        assert reopened.has_slides is False
        assert reopened.has_transcript is False  # and the project still opened

    def test_detection_settings_persist_for_a_re_run(self, tmp_path, video):
        project = Project.create(tmp_path / "p", "L", video)
        project.meta.slide_detection.normal_transition.min_content_replaced = 0.33
        project.meta.slide_detection.strong_global_transition.enabled = False
        project.save_meta()

        reopened = Project.open(project.root).meta.slide_detection
        assert reopened.normal_transition.min_content_replaced == 0.33
        assert reopened.strong_global_transition.enabled is False

    def test_the_cache_directory_exists(self, tmp_path, video):
        project = Project.create(tmp_path / "p", "L", video)
        assert project.cache_dir.is_dir()
        assert project.frame_cache_file.parent == project.cache_dir

    def test_an_older_project_without_slides_still_opens(self, tmp_path, video):
        """Projects created before Phase 3 must keep working."""
        project = Project.create(tmp_path / "p", "L", video)
        project.slides_file.unlink(missing_ok=True)

        reopened = Project.open(project.root)
        assert reopened.has_slides is False
        assert reopened.meta.slide_detection is not None


class TestAppearanceProvenance:
    """Fields Phase 4 and the UI need to tell detected from hand-made."""

    def test_defaults_are_detected_not_manual(self):
        appearance = SlideAppearance(1, 1, 0.0, 10.0)
        assert appearance.manually_added is False
        assert appearance.manually_modified is False
        assert appearance.detection_confidence is None

    def test_round_trip_keeps_provenance(self):
        appearance = SlideAppearance(
            appearance_id=3, slide_id=3, start=10.0, end=20.0,
            manually_added=True, manually_modified=True,
            detection_confidence=0.87, detection_reason="strong global transition",
        )
        assert SlideAppearance.from_dict(appearance.to_dict()) == appearance

    def test_an_older_record_without_provenance_loads(self):
        restored = SlideAppearance.from_dict(
            {"appearance_id": 1, "slide_id": 1, "start": 0.0, "end": 5.0}
        )
        assert restored.manually_added is False
        assert restored.detection_reason is None
        assert restored.annotation_state is None
        assert restored.annotation_label == ""

    def test_the_annotation_state_round_trips(self):
        for state in ("before_annotation", "annotated", "before_erase"):
            appearance = SlideAppearance(1, 1, 0.0, 5.0, annotation_state=state)
            restored = SlideAppearance.from_dict(appearance.to_dict())
            assert restored.annotation_state == state
            assert restored.annotation_label

    def test_an_unknown_annotation_state_is_ignored(self):
        restored = SlideAppearance.from_dict(
            {"appearance_id": 1, "slide_id": 1, "start": 0.0, "end": 5.0,
             "annotation_state": "something newer"}
        )
        assert restored.annotation_state is None
