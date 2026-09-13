"""Manual slide correction.

Automatic detection is a first draft. These are the operations that let a user
fix it - and, just as importantly, the guarantees the timeline keeps afterwards,
because Phase 4 will assign speech to slides by overlap and must never find a
hole or an overlap.
"""

import pytest

from lecture_saver.core.models import VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import (
    Slide,
    SlideAppearance,
    SlideEditError,
    SlideSet,
    delete_appearance,
    merge_with_previous,
    move_boundary,
    set_slide_image,
    split_at,
)


def make_set(spans=((0.0, 30.0), (30.0, 60.0), (60.0, 100.0))) -> SlideSet:
    return SlideSet(
        slides=[
            Slide(slide_id=i, image=f"slides/{i:03d}.jpg")
            for i in range(1, len(spans) + 1)
        ],
        appearances=[
            SlideAppearance(appearance_id=i, slide_id=i, start=s, end=e)
            for i, (s, e) in enumerate(spans, start=1)
        ],
    )


def assert_contiguous(slide_set: SlideSet, total: float) -> None:
    """The property every edit must preserve."""
    appearances = slide_set.appearances
    assert appearances[0].start == 0.0
    for earlier, later in zip(appearances, appearances[1:], strict=False):
        assert earlier.end == later.start, "a gap or overlap opened in the timeline"
        assert earlier.start < earlier.end
    assert appearances[-1].end == pytest.approx(total)
    assert [a.appearance_id for a in appearances] == list(
        range(1, len(appearances) + 1)
    )


class TestDelete:
    def test_removes_the_appearance(self):
        slides = make_set()
        removed = delete_appearance(slides, 2)
        assert removed.appearance_id == 2
        assert len(slides) == 2

    def test_the_previous_appearance_absorbs_the_time(self):
        slides = make_set()
        delete_appearance(slides, 2)
        assert slides.appearances[0].end == pytest.approx(60.0)
        assert_contiguous(slides, 100.0)

    def test_deleting_the_first_extends_what_follows_back_to_zero(self):
        slides = make_set()
        delete_appearance(slides, 1)
        assert slides.appearances[0].start == 0.0
        assert_contiguous(slides, 100.0)

    def test_deleting_the_last_keeps_the_end_of_the_video(self):
        slides = make_set()
        delete_appearance(slides, 3)
        assert_contiguous(slides, 100.0)

    def test_the_only_slide_cannot_be_deleted(self):
        slides = make_set(spans=((0.0, 50.0),))
        with pytest.raises(SlideEditError, match="only slide"):
            delete_appearance(slides, 1)

    def test_an_unknown_id_is_reported(self):
        with pytest.raises(SlideEditError, match="no slide numbered"):
            delete_appearance(make_set(), 99)

    def test_it_marks_the_set_as_hand_edited(self):
        slides = make_set()
        assert slides.manually_edited is False
        delete_appearance(slides, 2)
        assert slides.manually_edited is True


class TestMerge:
    def test_folds_into_the_previous_appearance(self):
        slides = make_set()
        merge_with_previous(slides, 3)
        assert len(slides) == 2
        assert slides.appearances[-1].end == pytest.approx(100.0)
        assert_contiguous(slides, 100.0)

    def test_the_first_appearance_has_nothing_to_merge_into(self):
        with pytest.raises(SlideEditError, match="nothing before it"):
            merge_with_previous(make_set(), 1)


class TestSplit:
    def test_creates_a_boundary_at_the_requested_time(self):
        slides = make_set()
        created = split_at(slides, 45.0)
        assert created.start == pytest.approx(45.0)
        assert len(slides) == 4
        assert_contiguous(slides, 100.0)

    def test_the_new_appearance_gets_a_fresh_slide_id(self):
        slides = make_set()
        created = split_at(slides, 45.0)
        existing = {s.slide_id for s in slides.slides if s.slide_id != created.slide_id}
        assert created.slide_id not in existing

    def test_a_slide_record_is_created_for_it(self):
        slides = make_set()
        created = split_at(slides, 45.0, image="slides/004.jpg")
        slide = slides.slide(created.slide_id)
        assert slide is not None
        assert slide.image == "slides/004.jpg"

    def test_splitting_outside_any_appearance_is_refused(self):
        with pytest.raises(SlideEditError, match="no slide to split"):
            split_at(make_set(), 500.0)

    def test_splitting_exactly_on_a_boundary_is_refused(self):
        """There is already a boundary there; silently doing nothing would confuse."""
        with pytest.raises(SlideEditError):
            split_at(make_set(), 30.0)

    def test_a_negative_time_is_refused(self):
        with pytest.raises(SlideEditError):
            split_at(make_set(), -5.0)

    def test_appearance_order_survives_a_split(self):
        slides = make_set()
        split_at(slides, 75.0)
        starts = [a.start for a in slides.appearances]
        assert starts == sorted(starts)


class TestSetImage:
    def test_replaces_the_image_and_fingerprint(self):
        slides = make_set()
        set_slide_image(slides, 2, "slides/002.jpg", "abcdef0123456789")
        slide = slides.slide(2)
        assert slide.image == "slides/002.jpg"
        assert slide.fingerprint == "abcdef0123456789"

    def test_creates_a_record_when_the_slide_has_none(self):
        slides = SlideSet(
            appearances=[SlideAppearance(appearance_id=1, slide_id=7, start=0.0, end=5.0)]
        )
        set_slide_image(slides, 7, "slides/007.jpg")
        assert slides.slide(7) is not None


class TestPersistence:
    @pytest.fixture
    def project(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"placeholder")
        info = VideoInfo(path=str(video), duration=100.0, has_audio=True)
        project = Project.create(tmp_path / "p", "Edited", info)
        project.set_slides(make_set())
        project.save_slides()
        return project

    def test_a_deletion_survives_reopening(self, project):
        delete_appearance(project.slides, 2)
        project.save_slides()

        reopened = Project.open(project.root)
        assert len(reopened.slides) == 2
        assert reopened.slides.appearances[0].end == pytest.approx(60.0)

    def test_a_split_survives_reopening(self, project):
        split_at(project.slides, 45.0, image="slides/004.jpg")
        project.save_slides()

        reopened = Project.open(project.root)
        assert len(reopened.slides) == 4
        assert any(a.start == pytest.approx(45.0) for a in reopened.slides.appearances)

    def test_the_hand_edited_flag_survives_reopening(self, project):
        """The UI warns before a re-run throws manual corrections away."""
        delete_appearance(project.slides, 3)
        project.save_slides()
        assert Project.open(project.root).slides.manually_edited is True

    def test_an_untouched_set_is_not_flagged(self, project):
        assert Project.open(project.root).slides.manually_edited is False


class TestMoveBoundary:
    """Correcting a boundary the detector placed slightly early or late."""

    def test_moves_the_start_and_resizes_the_neighbour(self):
        slides = make_set()
        move_boundary(slides, 2, 40.0)

        assert slides.appearances[0].end == pytest.approx(40.0)
        assert slides.appearances[1].start == pytest.approx(40.0)
        assert_contiguous(slides, 100.0)

    def test_both_sides_are_marked_modified(self):
        slides = make_set()
        move_boundary(slides, 2, 40.0)
        assert slides.appearances[0].manually_modified is True
        assert slides.appearances[1].manually_modified is True
        assert slides.manually_edited is True

    def test_the_first_slide_has_no_boundary_to_move(self):
        with pytest.raises(SlideEditError, match="always starts at the beginning"):
            move_boundary(make_set(), 1, 5.0)

    @pytest.mark.parametrize("seconds", [-1.0, 0.0, 60.0, 95.0])
    def test_moving_past_a_neighbour_is_refused(self, seconds):
        """Silently swallowing a slide is what delete and merge are for."""
        with pytest.raises(SlideEditError, match="past a neighbouring slide"):
            move_boundary(make_set(), 2, seconds)

    def test_it_survives_reopening(self, tmp_path):
        from lecture_saver.core.models import VideoInfo

        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"x")
        project = Project.create(
            tmp_path / "p", "Moved", VideoInfo(path=str(video), duration=100.0)
        )
        project.set_slides(make_set())
        move_boundary(project.slides, 3, 55.0)
        project.save_slides()

        reopened = Project.open(project.root)
        assert reopened.slides.appearances[2].start == pytest.approx(55.0)
        assert reopened.slides.appearances[2].manually_modified is True


class TestDeletionIsCounted:
    def test_the_count_records_over_detection(self):
        slides = make_set()
        delete_appearance(slides, 2)
        assert slides.manually_deleted == 1

    def test_it_survives_a_round_trip(self):
        slides = make_set()
        delete_appearance(slides, 2)
        assert SlideSet.from_dict(slides.to_dict()).manually_deleted == 1
