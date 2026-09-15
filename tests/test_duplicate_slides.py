"""Recognising a slide the professor went back to (§9)."""

import json

import pytest

import tests.slide_fixtures as F
from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import (
    Slide,
    SlideAppearance,
    SlideEditError,
    SlideSet,
    separate_slide,
)
from lecture_saver.slides.annotation_snapshots import add_annotation_snapshots
from lecture_saver.slides.detector import detect_appearances
from lecture_saver.slides.duplicates import link_returned_slides


def linked(frames, options=None):
    options = options or F.options()
    d = F.descriptors(frames, options)
    result = detect_appearances(
        d.thumbnails, d.times, d.hashes, duration=float(len(frames)), options=options, ink=d
    )
    result = add_annotation_snapshots(result, d, options)
    return link_returned_slides(result, d, options)


def ids(result):
    return [(a.appearance_id, a.slide_id) for a in result.appearances]


class TestLinking:
    def test_going_back_to_a_slide_reuses_its_identity(self):
        a, b, c = F.slide(1), F.slide(2), F.slide(3)
        result = linked(F.hold(a, 20) + F.hold(b, 20) + F.hold(c, 20) + F.hold(a, 20))
        assert ids(result) == [(1, 1), (2, 2), (3, 3), (4, 1)]

    def test_distinct_slides_are_numbered_in_order_of_first_appearance(self):
        a, b = F.slide(4), F.slide(5)
        result = linked(F.hold(a, 20) + F.hold(b, 20) + F.hold(a, 20) + F.hold(F.slide(6), 20))
        assert [s for _, s in ids(result)] == [1, 2, 1, 3]

    def test_different_slides_are_never_merged(self):
        frames = []
        for seed in range(10, 16):
            frames += F.hold(F.slide(seed), 15)
        assert [s for _, s in ids(linked(frames))] == [1, 2, 3, 4, 5, 6]

    def test_a_slide_and_its_annotated_version_stay_separate(self):
        base = F.slide(21)
        frames = F.hold(base, 20)
        for step in range(1, 6):
            frames += F.hold(F.with_pen(base, step, thickness=2), 10)
        frames += F.hold(F.slide(22), 20)
        result = linked(frames)
        states = [a.annotation_state for a in result.appearances]
        assert states[:2] == ["before_annotation", "annotated"]
        assert len({a.slide_id for a in result.appearances}) == len(result.appearances)

    def test_sparse_slides_that_only_look_alike_are_not_merged(self):
        first, second = F.titled_slide(), F.blank()
        second[12:36, 60:276] = 40  # a title bar in a different place
        result = linked(F.hold(first, 20) + F.hold(F.slide(30), 20) + F.hold(second, 20))
        assert len({a.slide_id for a in result.appearances}) == 3

    def test_it_can_be_switched_off(self):
        a, b = F.slide(40), F.slide(41)
        options = F.options(**{"duplicate_slides.enabled": False})
        result = linked(F.hold(a, 20) + F.hold(b, 20) + F.hold(a, 20), options)
        assert [s for _, s in ids(result)] == [1, 2, 3]


class TestModel:
    @pytest.fixture
    def slides(self):
        return SlideSet(
            slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/002.jpg")],
            appearances=[
                SlideAppearance(1, 1, 0, 10), SlideAppearance(2, 2, 10, 20),
                SlideAppearance(3, 1, 20, 30, annotation_state="annotated"),
            ],
        )

    def test_repeat_and_label(self, slides):
        first, second, third = slides.appearances
        assert slides.repeat_of(first) is None and slides.repeat_of(second) is None
        assert slides.repeat_of(third) is first
        assert slides.label_for(third) == "With notes · Same as Slide 1"
        assert slides.label_for(second) == ""

    def test_separating_gives_a_new_identity(self, slides):
        appearance = separate_slide(slides, 3, image="slides/003.jpg")
        assert appearance.slide_id == 3
        assert slides.repeat_of(appearance) is None
        assert slides.slide(3).image == "slides/003.jpg"
        assert slides.manually_edited

    def test_separating_a_slide_that_is_not_a_repeat_is_refused(self, slides):
        with pytest.raises(SlideEditError):
            separate_slide(slides, 2)

    def test_shared_identity_round_trips(self, slides):
        restored = SlideSet.from_dict(slides.to_dict())
        assert restored.repeat_of(restored.appearances[2]) is restored.appearances[0]


class TestPipeline:
    def test_one_picture_per_distinct_slide(self, tmp_path, monkeypatch):
        from lecture_saver.slides import pipeline as pipeline_module
        from lecture_saver.slides.pipeline import SlideDetectionPipeline

        a, b = F.slide(50), F.slide(51)
        frames = F.hold(a, 20) + F.hold(b, 20) + F.hold(a, 20)
        options = F.options()
        descriptors = F.descriptors(frames, options)

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        project = Project.create(
            tmp_path / "p", "Returns", VideoInfo(path=str(video), duration=60.0)
        )
        captured: list[float] = []

        def fake_extract(path, timestamp, target, **kwargs):
            captured.append(timestamp)
            target.write_bytes(b"jpeg")

        monkeypatch.setattr(pipeline_module, "extract_frame", fake_extract)
        monkeypatch.setattr(SlideDetectionPipeline, "_prepare", lambda self: None)
        monkeypatch.setattr(SlideDetectionPipeline, "_descriptors", lambda self: descriptors)

        slide_set = SlideDetectionPipeline(project, options).run()

        assert [a.slide_id for a in slide_set.appearances] == [1, 2, 1]
        assert len(slide_set.slides) == 2
        assert len(captured) == 2
        reopened = Project.open(project.root)
        assert reopened.slides.label_for(reopened.slides.appearances[2]) == "Same as Slide 1"


class TestExports:
    @pytest.fixture
    def project(self, tmp_path):
        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        proj = Project.create(tmp_path / "p", "Returns", VideoInfo(path=str(video), duration=30.0))
        proj.transcript.replace_all([
            TranscriptSegment(id="segment_001", start=1, end=4, original_text="first visit"),
            TranscriptSegment(id="segment_002", start=21, end=24, original_text="back again"),
        ])
        proj.set_slides(SlideSet(
            slides=[Slide(1, ""), Slide(2, "")],
            appearances=[SlideAppearance(1, 1, 0, 10), SlideAppearance(2, 2, 10, 20),
                         SlideAppearance(3, 1, 20, 30)],
        ))
        return proj

    def test_json_records_the_shared_slide(self, project):
        from lecture_saver.export.document import build_document
        from lecture_saver.export.text_formats import to_json

        slides = json.loads(to_json(build_document(project)))["slides"]
        assert [s["slide_id"] for s in slides] == [1, 2, 1]
        assert [s["same_slide_as"] for s in slides] == [None, None, 1]

    def test_markdown_heading_says_it_is_a_return(self, project):
        from lecture_saver.export.document import build_document
        from lecture_saver.export.text_formats import to_markdown

        assert "## Slide 3 · Same as Slide 1" in to_markdown(build_document(project))

    def test_the_pdf_does_not_repeat_the_picture(self, project, tmp_path):
        from lecture_saver.export.document import build_document
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.export.pdf import find_korean_font, write_pdf

        if find_korean_font() is None:
            pytest.skip("no Korean font")
        from tests.test_export import _pdf_text

        target = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(), target)
        text = _pdf_text(target)[1]
        assert "Same slide as Slide 1" in text
        assert "back again" in text


class TestUI:
    def test_the_navigator_labels_a_return(self, qapp, tmp_path):
        from PySide6.QtCore import Qt

        from lecture_saver.ui.slide_panel import SlidePanel

        slides = SlideSet(
            slides=[Slide(1, ""), Slide(2, "")],
            appearances=[SlideAppearance(1, 1, 0, 10), SlideAppearance(2, 2, 10, 20),
                         SlideAppearance(3, 1, 20, 30)],
        )
        panel = SlidePanel()
        panel.set_slides(slides, tmp_path)
        label = panel._list.model().index(2, 0).data(Qt.ItemDataRole.DisplayRole)
        assert "Same as Slide 1" in label

    def test_separating_from_the_window_captures_a_new_picture(
        self, qapp, tmp_path, monkeypatch
    ):
        from lecture_saver.config import Settings
        from lecture_saver.ui import main_window as main_window_module
        from lecture_saver.ui.main_window import MainWindow

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        info = VideoInfo(path=str(video), duration=30.0)
        project = Project.create(tmp_path / "p", "Returns", info)
        project.set_slides(SlideSet(
            slides=[Slide(1, ""), Slide(2, "")],
            appearances=[SlideAppearance(1, 1, 0, 10), SlideAppearance(2, 2, 10, 20),
                         SlideAppearance(3, 1, 20, 30)],
        ))
        project.save_slides()
        moments: list[float] = []
        monkeypatch.setattr(
            main_window_module, "capture_slide_image",
            lambda proj, slide_id, seconds, options: moments.append(seconds)
            or f"slides/{slide_id:03d}.jpg",
        )

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project.root)
            window._separate_slide(3)
            reopened = Project.open(project.root)
            third = reopened.slides.appearances[2]
            assert reopened.slides.repeat_of(third) is None
            assert reopened.slides.slide_for(third).image == "slides/003.jpg"
            assert moments == [21.0]
        finally:
            window.close()
