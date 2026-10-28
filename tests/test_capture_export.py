"""Screenshots in PDFs: their own PDF, and - when asked for - in the notes.

Which slide each goes with, and where it is printed.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from lecture_saver.core.captures import Capture, CaptureSet, Mark
from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.export.document import build_document, screenshots_by_section
from lecture_saver.export.exporter import run_export
from lecture_saver.export.options import (
    PDF_PRESETS,
    ExportFormat,
    ExportSettings,
    PdfOptions,
    safe_file_stem,
)
from lecture_saver.export.pdf import (
    PdfExportError,
    find_korean_font,
    write_pdf,
    write_screenshots_pdf,
)

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)


def _jpeg(path: Path, colour=(40, 120, 160)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), colour).save(path, "JPEG")


def _add(project: Project, time: float, *, marks=(), start=None, marked_copy=True,
         picture=True) -> Capture:
    capture_id = project.captures.new_id()
    capture = Capture(
        capture_id=capture_id, time=time, start=time if start is None else start,
        image=CaptureSet.image_name(capture_id), width=320, height=180, marks=list(marks),
    )
    if picture:
        _jpeg(project.root / capture.image)
    if marks and marked_copy:
        _jpeg(project.root / capture.marked_image, colour=(200, 60, 60))
    project.captures.add(capture)
    return capture


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    proj = Project.create(tmp_path / "projects", "머신러닝 1주차",
                          VideoInfo(path=str(video), duration=95.0, has_audio=True))
    proj.transcript.replace_all(
        [
            TranscriptSegment(id="segment_001", start=0.0, end=4.0,
                              original_text="Today we begin with the basics."),
            TranscriptSegment(id="segment_002", start=4.5, end=9.0,
                              original_text="Here, look at the learning rate."),
            TranscriptSegment(id="segment_003", start=31.0, end=38.0,
                              original_text="If this value is too large, the model diverges."),
            TranscriptSegment(id="segment_004", start=60.0, end=66.0,
                              original_text="Too small, and training becomes very slow."),
        ],
        language="en",
    )
    _jpeg(proj.slides_dir / "001.jpg")
    proj.set_slides(SlideSet(
        slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/001.jpg")],
        appearances=[SlideAppearance(1, 1, 0.0, 30.0), SlideAppearance(2, 2, 30.0, 95.0)],
    ))
    return proj


class TestGrouping:
    def test_each_goes_with_the_slide_on_screen_when_it_was_taken(self, project):
        _add(project, 5.0)
        _add(project, 40.0)
        _add(project, 29.9)
        groups = screenshots_by_section(project)
        assert [[s.time for s in group] for group in groups] == [[5.0, 29.9], [40.0]]

    def test_after_the_last_slide_ends_it_goes_with_the_last(self, project):
        _add(project, 120.0)
        assert [len(group) for group in screenshots_by_section(project)] == [0, 1]

    def test_the_marked_copy_is_printed(self, project):
        capture = _add(project, 5.0, marks=[Mark.underline(0, 0.5, 1, 0.5)])
        (shot,) = screenshots_by_section(project)[0]
        assert shot.image == project.root / capture.marked_image
        assert shot.marks == 1

    def test_a_missing_marked_copy_falls_back_to_the_plain_picture(self, project):
        capture = _add(project, 5.0, marks=[Mark.underline(0, 0.5, 1, 0.5)], marked_copy=False)
        (shot,) = screenshots_by_section(project)[0]
        assert shot.image == project.root / capture.image

    def test_one_whose_picture_is_gone_is_left_out(self, project):
        _add(project, 5.0, picture=False)
        assert screenshots_by_section(project) == [[], []]

    def test_without_slides_they_all_go_in_the_one_section(self, project):
        project.set_slides(SlideSet())
        _add(project, 5.0)
        _add(project, 70.0)
        document = build_document(project)
        assert len(document.sections) == 1
        assert [s.time for s in document.sections[0].screenshots] == [5.0, 70.0]

    def test_the_document_knows_it_has_them(self, project):
        assert not build_document(project).has_screenshots
        _add(project, 5.0)
        assert build_document(project).has_screenshots


class TestOptions:
    def test_the_notes_leave_them_out_unless_asked(self):
        # They have a PDF of their own.
        assert not PdfOptions().screenshots
        stored = {"pdf": {"slide_image": True, "transcript": True}}
        assert not ExportSettings.from_dict(stored).pdf.screenshots
        assert ExportSettings.from_dict({"pdf": {"screenshots": True}}).pdf.screenshots

    def test_their_own_pdf_is_an_export_format(self):
        assert ExportFormat.SCREENSHOTS.label == "Screenshots PDF"
        assert ExportSettings.from_dict(
            {"formats": ["screenshots"]}
        ).formats == [ExportFormat.SCREENSHOTS]

    def test_transcript_only_means_no_pictures_at_all(self):
        assert not PDF_PRESETS["Transcript Only"].screenshots


def _pdf_pages(path: Path) -> list[str]:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    pdf = QPdfDocument()
    pdf.load(str(path))
    pages = [pdf.getAllText(page).text() for page in range(pdf.pageCount())]
    pdf.close()
    return pages


def _pdf_text(path: Path) -> str:
    return "\n".join(_pdf_pages(path))


IN_NOTES = PdfOptions(screenshots=True)


@needs_korean_font
class TestPdf:
    def test_printed_after_what_was_being_said(self, project, tmp_path):
        _add(project, 6.0, marks=[Mark.memo(0.1, 0.1, "exam")], start=4.0)
        target = tmp_path / "notes.pdf"
        write_pdf(build_document(project), IN_NOTES, target)
        text = _pdf_text(target)
        caption = "Screenshot · 00:00:06"
        assert caption in text
        assert "marked from 00:00:04" in text
        # After the paragraph spoken then, before the next slide.
        assert text.index("learning rate") < text.index(caption) < text.index("Slide 2")

    def test_with_timestamps_it_goes_between_the_lines(self, project, tmp_path):
        _add(project, 50.0)
        target = tmp_path / "notes.pdf"
        write_pdf(build_document(project), replace(IN_NOTES, segment_timestamps=True), target)
        text = _pdf_text(target)
        caption = "Screenshot · 00:00:50"
        assert text.index("model diverges") < text.index(caption) < text.index("Too small")

    def test_each_picture_is_embedded(self, project, tmp_path):
        _add(project, 6.0)
        _add(project, 50.0, marks=[Mark.underline(0, 0.5, 1, 0.5)])
        with_shots, without = tmp_path / "with.pdf", tmp_path / "without.pdf"
        write_pdf(build_document(project), IN_NOTES, with_shots)
        write_pdf(build_document(project), PdfOptions(), without)
        images = with_shots.read_bytes().count(b"/Subtype /Image")
        assert images == without.read_bytes().count(b"/Subtype /Image") + 2
        assert "Screenshot" not in _pdf_text(without)

    def test_slides_only_still_shows_them_after_the_slide(self, project, tmp_path):
        _add(project, 6.0)
        target = tmp_path / "slides.pdf"
        write_pdf(build_document(project), replace(PDF_PRESETS["Slides Only"],
                                                   screenshots=True), target)
        text = _pdf_text(target)
        assert "Screenshot · 00:00:06" in text
        assert "learning rate" not in text


@needs_korean_font
class TestScreenshotsPdf:
    def test_only_the_screenshots_with_their_time_and_slide(self, project, tmp_path):
        _add(project, 6.0, marks=[Mark.memo(0.1, 0.1, "exam")], start=4.0)
        _add(project, 50.0)
        target = tmp_path / "shots.pdf"
        write_screenshots_pdf(build_document(project), target)
        text = _pdf_text(target)
        assert "머신러닝 1주차" in text and "2 screenshots" in text
        assert "1 · 00:00:06 · Slide 1 · marked from 00:00:04" in text
        assert "2 · 00:00:50 · Slide 2" in text
        assert text.index("00:00:06") < text.index("00:00:50")
        # Nothing of the notes.
        assert "learning rate" not in text and "Transcript" not in text

    def test_two_to_a_page(self, project, tmp_path):
        for second in (5.0, 10.0, 40.0, 50.0, 55.0):
            _add(project, second)
        target = tmp_path / "shots.pdf"
        write_screenshots_pdf(build_document(project), target)
        pages = _pdf_pages(target)
        assert len(pages) == 3
        assert [page.count(" · 00:00:") for page in pages] == [2, 2, 1]

    def test_every_picture_is_printed_with_its_marks(self, project, tmp_path):
        marked = _add(project, 6.0, marks=[Mark.underline(0, 0.5, 1, 0.5)])
        _add(project, 50.0)
        target = tmp_path / "shots.pdf"
        write_screenshots_pdf(build_document(project), target)
        assert target.read_bytes().count(b"/Subtype /Image") == 2
        groups = screenshots_by_section(project)
        assert groups[0][0].image == project.root / marked.marked_image

    def test_without_screenshots_it_says_how_to_take_one(self, project, tmp_path):
        with pytest.raises(PdfExportError, match="Press S"):
            write_screenshots_pdf(build_document(project), tmp_path / "shots.pdf")
        assert not (tmp_path / "shots.pdf").exists()

    def test_needs_neither_slides_nor_a_transcript(self, project, tmp_path):
        project.set_slides(SlideSet())
        project.transcript.replace_all([])
        _add(project, 5.0)
        settings = ExportSettings(formats=[ExportFormat.SCREENSHOTS])
        result = run_export(build_document(project), settings, tmp_path / "out")
        assert result.ok, result.failures
        (written,) = result.written
        assert written.name == f"{safe_file_stem(project.name)}_screenshots.pdf"
        text = _pdf_text(written)
        assert "1 · 00:00:05" in text and "Slide" not in text
