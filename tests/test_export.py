"""Exports (§19-§23, §33).

Text formats are checked character for character; the PDF is checked by reading
it back with Qt's PDF module, so Korean text is proven to be in the file as text
rather than trusted to have rendered.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.export.document import build_document, nfc, paragraphs
from lecture_saver.export.exporter import run_export
from lecture_saver.export.options import (
    PDF_PRESETS,
    ExportFormat,
    ExportSettings,
    PdfOptions,
    export_file_name,
    preset_name_for,
)
from lecture_saver.export.pdf import PdfExportError, find_korean_font, write_pdf
from lecture_saver.export.text_formats import to_json, to_markdown, to_srt, to_txt, to_vtt

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)


def _jpeg(path: Path, width=320, height=180):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (width, height), (40, 120, 160)).save(path, "JPEG")


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "강의01.mp4"
    video.write_bytes(b"x")
    info = VideoInfo(path=str(video), duration=95.0, has_audio=True)
    proj = Project.create(tmp_path / "projects", "머신러닝 1주차", info)
    proj.transcript.replace_all(
        [
            TranscriptSegment(id="segment_001", start=0.0, end=4.0,
                              original_text="오늘은 머신러닝의 기본 개념을 봅니다."),
            TranscriptSegment(id="segment_002", start=4.5, end=9.0,
                              original_text="여기서 러닝레이트를 보시면",
                              edited_text="여기서 learning rate를 보시면"),
            TranscriptSegment(id="segment_003", start=20.0, end=24.0,
                              original_text="지워질 문장", edited_text=""),
            TranscriptSegment(id="segment_004", start=31.0, end=38.0,
                              original_text="If this value is too large, the model --> diverges."),
        ],
        language="ko",
    )
    proj.save()
    _jpeg(proj.slides_dir / "001.jpg")
    proj.set_slides(SlideSet(
        slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/002.jpg")],
        appearances=[
            SlideAppearance(1, 1, 0.0, 30.0, annotation_state="before_annotation"),
            SlideAppearance(2, 2, 30.0, 95.0),
        ],
    ))
    proj.save_slides()
    return proj


@pytest.fixture
def document(project):
    return build_document(project)


# -- the document -----------------------------------------------------------------


class TestDocument:
    def test_sections_follow_the_slides_and_their_speech(self, document):
        assert [s.number for s in document.sections] == [1, 2]
        assert [[x.id for x in s.segments] for s in document.sections] == [
            ["segment_001", "segment_002", "segment_003"],
            ["segment_004"],
        ]
        assert document.sections[0].label == "Before notes"

    def test_a_missing_slide_image_is_none_not_an_error(self, document):
        assert document.sections[0].image is not None
        assert document.sections[1].image is None
        assert document.sections[1].image_ref == "slides/002.jpg"

    def test_it_is_a_snapshot(self, project, document):
        project.transcript.edit_text("segment_001", "changed after export started")
        assert document.segments[0].effective_text.startswith("오늘은")

    def test_without_slides_the_lecture_is_one_section(self, project):
        project.set_slides(SlideSet())
        document = build_document(project)
        assert not document.has_slides
        assert len(document.sections) == 1
        assert len(document.sections[0].segments) == 4

    def test_decomposed_korean_is_composed(self, project):
        """macOS archives store Hangul as loose jamo; exports must not."""
        decomposed = "\u1112\u1161\u11ab\u1100\u1173\u11af"  # 한글 as jamo
        project.meta.name = decomposed
        assert build_document(project).title == "한글" == nfc(decomposed)

    def test_paragraphs_break_at_pauses(self, document):
        groups = paragraphs(document.segments)
        assert [[s.id for s in g] for g in groups] == [
            ["segment_001", "segment_002"], ["segment_004"],
        ]


# -- text formats -----------------------------------------------------------------


class TestTxt:
    def test_with_timestamps_matches_the_specification(self, document):
        assert to_txt(document) == (
            "[00:00:00]\n오늘은 머신러닝의 기본 개념을 봅니다.\n\n"
            "[00:00:04]\n여기서 learning rate를 보시면\n\n"
            "[00:00:31]\nIf this value is too large, the model --> diverges.\n"
        )

    def test_without_timestamps_is_paragraphs(self, document):
        assert to_txt(document, timestamps=False) == (
            "오늘은 머신러닝의 기본 개념을 봅니다. 여기서 learning rate를 보시면\n\n"
            "If this value is too large, the model --> diverges.\n"
        )

    def test_edited_text_is_used_and_blanked_lines_left_out(self, document):
        text = to_txt(document)
        assert "learning rate" in text and "러닝레이트" not in text
        assert "지워질" not in text


class TestMarkdown:
    def test_slides_images_and_timestamps(self, document, project):
        text = to_markdown(document, output_dir=project.exports_dir)
        assert text.startswith("# 머신러닝 1주차\n")
        assert "## Slide 1 · Before notes" in text
        assert "`00:00:00 – 00:00:30`" in text
        assert "![Slide 1](../slides/001.jpg)" in text
        assert "**[00:00:04]** 여기서 learning rate를 보시면" in text

    def test_a_silent_slide_says_so(self, project):
        project.transcript.delete_segment("segment_004")
        text = to_markdown(build_document(project))
        assert "*Nothing was said while this slide was on screen.*" in text

    def test_markdown_syntax_in_speech_is_escaped(self, project):
        project.transcript.edit_text("segment_001", "# not a heading *nor bold*")
        text = to_markdown(build_document(project), timestamps=False)
        assert "\\# not a heading \\*nor bold\\*" in text

    def test_image_paths_with_spaces_are_bracketed(self, document, tmp_path):
        text = to_markdown(document, output_dir=tmp_path / "my notes")
        assert "![Slide 1](<" in text


class TestSubtitles:
    def test_srt(self, document):
        srt = to_srt(document)
        assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\n오늘은")
        assert "\n\n3\n00:00:31,000 --> 00:00:38,000\n" in srt
        assert "지워질" not in srt

    def test_vtt(self, document):
        vtt = to_vtt(document)
        assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:04.000\n")
        assert "the model -> diverges." in vtt, "'-->' inside text would break the cue"

    def test_a_cue_never_ends_before_it_starts(self, project):
        project.transcript.insert_segment_after("segment_004", "zero")
        segment = project.transcript.segments[-1]
        segment.end = segment.start
        srt = to_srt(build_document(project))
        assert "00:00:38,000 --> 00:00:38,500" in srt


class TestJson:
    def test_structure_follows_the_specification(self, document):
        data = json.loads(to_json(document, exported_at=datetime(2026, 9, 14, tzinfo=timezone.utc)))
        assert data["video"] == "강의01.mp4"
        assert data["exported_at"] == "2026-09-14T00:00:00+00:00"
        first = data["slides"][0]
        assert first["slide_id"] == 1 and first["start"] == 0.0 and first["end"] == 30.0
        assert first["image"] == "slides/001.jpg"
        segment = first["transcript"][1]
        assert segment == {
            "id": "segment_002", "start": 4.5, "end": 9.0,
            "original_text": "여기서 러닝레이트를 보시면",
            "edited_text": "여기서 learning rate를 보시면",
            "text": "여기서 learning rate를 보시면",
        }

    def test_json_keeps_blanked_lines_with_their_original(self, document):
        data = json.loads(to_json(document))
        blanked = data["slides"][0]["transcript"][2]
        assert blanked["original_text"] == "지워질 문장" and blanked["edited_text"] == ""

    def test_without_slides_the_transcript_is_top_level(self, project):
        project.set_slides(SlideSet())
        data = json.loads(to_json(build_document(project)))
        assert data["slides"] == [] and len(data["transcript"]) == 4


# -- options ------------------------------------------------------------------------


class TestOptions:
    def test_the_four_presets_from_the_specification(self):
        assert set(PDF_PRESETS) == {
            "Slide + Transcript", "Slide + Timestamped Transcript",
            "Slides Only", "Transcript Only",
        }
        assert PDF_PRESETS["Slides Only"].transcript is False
        assert PDF_PRESETS["Transcript Only"].slide_image is False
        assert preset_name_for(PdfOptions()) == "Slide + Transcript"
        assert preset_name_for(PdfOptions(slide_number=False)) is None

    def test_settings_round_trip_and_tolerate_junk(self):
        settings = ExportSettings(
            formats=[ExportFormat.SRT, ExportFormat.JSON], include_timestamps=False,
            pdf=PdfOptions(segment_timestamps=True), output_dir="D:/notes",
        )
        assert ExportSettings.from_dict(settings.to_dict()) == settings
        odd = ExportSettings.from_dict({"formats": ["pdf", "docx"], "pdf": {"bogus": 1}})
        assert odd.formats == [ExportFormat.PDF]


class TestOutputFolder:
    EXPORTS = Path("C:/Projects/ml/exports")

    def test_without_a_default_the_project_folder_is_used(self):
        from lecture_saver.export.options import resolve_output_dir

        assert resolve_output_dir(None, self.EXPORTS, "ML 01", None) == self.EXPORTS

    def test_with_a_default_lectures_share_it_under_their_own_names(self):
        from lecture_saver.export.options import resolve_output_dir

        icloud = Path("C:/Users/me/iCloudDrive/Lecture_Saver")
        assert resolve_output_dir(None, self.EXPORTS, "Machine Learning 04", icloud) == icloud
        assert export_file_name("Machine Learning 04", ExportFormat.PDF) == (
            "Machine_Learning_04_lecture_notes.pdf"
        )
        korean = export_file_name("9주차: 상표권", ExportFormat.MARKDOWN)
        assert korean == "9주차_상표권_transcript.md"

    def test_a_lecture_without_choices_starts_from_the_last_export(self):
        last = ExportSettings(
            formats=[ExportFormat.PDF], pdf=PdfOptions(translation=True), output_dir="D:/x"
        )
        fresh = ExportSettings.for_lecture({}, last.as_app_defaults())
        assert fresh.formats == [ExportFormat.PDF]
        assert fresh.output_dir is None, "a folder is never inherited"
        own = ExportSettings(formats=[ExportFormat.SRT]).to_dict()
        assert ExportSettings.for_lecture(own, last.as_app_defaults()).formats == [
            ExportFormat.SRT
        ]
        assert ExportSettings.for_lecture(None, None) == ExportSettings()

    def test_a_folder_the_lecture_chose_wins_over_the_default(self):
        from lecture_saver.export.options import resolve_output_dir

        icloud = Path("C:/Users/me/iCloudDrive")
        assert resolve_output_dir("D:/notes", self.EXPORTS, "ML", icloud) == Path("D:/notes")

    def test_the_default_folder_is_remembered_by_the_app(self, tmp_path):
        from lecture_saver.config import Settings

        settings = Settings(tmp_path / "settings.json")
        assert settings.default_export_dir is None
        settings.default_export_dir = tmp_path / "iCloudDrive"
        assert Settings(tmp_path / "settings.json").default_export_dir == tmp_path / "iCloudDrive"
        settings.default_export_dir = None
        assert Settings(tmp_path / "settings.json").default_export_dir is None


# -- PDF ----------------------------------------------------------------------------


def _pdf_text(path: Path) -> tuple[int, str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    pdf = QPdfDocument()
    pdf.load(str(path))
    pages = pdf.pageCount()
    text = "\n".join(pdf.getAllText(page).text() for page in range(pages))
    pdf.close()
    return pages, text


@needs_korean_font
class TestPdf:
    def test_korean_english_and_edits_are_in_the_pdf_as_text(self, document, tmp_path):
        target = tmp_path / "notes.pdf"
        write_pdf(document, PdfOptions(), target)

        assert target.read_bytes().startswith(b"%PDF")
        pages, text = _pdf_text(target)
        assert pages >= 1
        for expected in ("머신러닝 1주차", "Slide 1", "Before notes", "00:00:00 – 00:00:30",
                         "learning rate", "If this value is too large"):
            assert expected in text
        assert "러닝레이트" not in text, "the edited text must replace the original"
        assert "지워질" not in text

    def test_the_font_is_embedded(self, document, tmp_path):
        target = tmp_path / "notes.pdf"
        write_pdf(document, PdfOptions(), target)
        assert b"FontFile2" in target.read_bytes()

    def test_long_transcripts_flow_across_pages(self, project, tmp_path):
        sentence = "학습률이 너무 크면 모델이 발산하고, too small means slow training. "
        project.transcript.replace_all([
            TranscriptSegment(id=f"segment_{i:03d}", start=i * 0.2, end=i * 0.2 + 0.19,
                              original_text=sentence * 3)
            for i in range(1, 140)
        ])
        target = tmp_path / "long.pdf"
        write_pdf(build_document(project), PdfOptions(), target)
        pages, text = _pdf_text(target)
        assert pages >= 4
        assert text.count("학습률이") >= 139 * 3 - 5

    @pytest.mark.parametrize("preset", list(PDF_PRESETS))
    def test_every_preset_produces_a_pdf(self, document, tmp_path, preset):
        target = tmp_path / "preset.pdf"
        write_pdf(document, PDF_PRESETS[preset], target)
        _, text = _pdf_text(target)
        has_transcript = "learning rate" in text
        assert has_transcript == PDF_PRESETS[preset].transcript
        assert ("Slide 1" in text) == PDF_PRESETS[preset].slide_number

    def test_timestamps_appear_only_when_asked(self, document, tmp_path):
        plain, stamped = tmp_path / "plain.pdf", tmp_path / "stamped.pdf"
        write_pdf(document, PdfOptions(), plain)
        write_pdf(document, PdfOptions(segment_timestamps=True), stamped)
        assert "00:00:31" not in _pdf_text(plain)[1]
        assert "00:00:31" in _pdf_text(stamped)[1]

    def test_without_slides_the_pdf_is_the_transcript(self, project, tmp_path):
        project.set_slides(SlideSet())
        target = tmp_path / "t.pdf"
        write_pdf(build_document(project), PdfOptions(), target)
        _, text = _pdf_text(target)
        assert "learning rate" in text and "Slide 1" not in text

    def test_slides_only_without_slides_is_refused(self, project, tmp_path):
        project.set_slides(SlideSet())
        with pytest.raises(PdfExportError):
            write_pdf(build_document(project), PDF_PRESETS["Slides Only"], tmp_path / "x.pdf")
        assert not (tmp_path / "x.pdf").exists()

    def test_a_failed_build_leaves_no_partial_file(self, document, tmp_path, monkeypatch):
        import lecture_saver.export.pdf as pdf_module

        def explode(*args, **kwargs):
            raise RuntimeError("layout exploded")

        monkeypatch.setattr(pdf_module, "_story", explode)
        with pytest.raises(PdfExportError):
            write_pdf(document, PdfOptions(), tmp_path / "broken.pdf")
        assert list(tmp_path.glob("*.pdf")) == []


def test_a_missing_korean_font_fails_loudly(document, tmp_path, monkeypatch):
    from reportlab.pdfbase import pdfmetrics

    import lecture_saver.export.pdf as pdf_module

    monkeypatch.setattr(pdf_module, "find_korean_font", lambda: None)
    monkeypatch.setattr(pdf_module, "_REGULAR", "UnregisteredFontForTest")
    assert "UnregisteredFontForTest" not in pdfmetrics.getRegisteredFontNames()
    with pytest.raises(PdfExportError, match="Korean"):
        write_pdf(document, PdfOptions(), tmp_path / "x.pdf")


# -- running an export ----------------------------------------------------------------


class TestRunExport:
    @needs_korean_font
    def test_every_format_is_written_to_the_output_folder(self, project):
        from lecture_saver.core.captures import Capture, CaptureSet

        # A screenshot, so the screenshots PDF has something to print.
        capture = Capture(capture_id=1, time=5.0, start=5.0, image=CaptureSet.image_name(1))
        _jpeg(project.root / capture.image)
        project.captures.add(capture)
        document = build_document(project)
        settings = ExportSettings(formats=list(ExportFormat))
        result = run_export(document, settings, project.exports_dir)
        assert result.ok
        assert sorted(p.name for p in result.written) == sorted(
            export_file_name(document.title, f) for f in ExportFormat
        )

    def test_one_failing_format_does_not_stop_the_others(self, project, tmp_path):
        project.set_slides(SlideSet())
        settings = ExportSettings(
            formats=[ExportFormat.PDF, ExportFormat.TXT], pdf=PDF_PRESETS["Slides Only"]
        )
        result = run_export(build_document(project), settings, tmp_path)
        txt = export_file_name(project.name, ExportFormat.TXT)
        assert [p.name for p in result.written] == [txt]
        assert [f for f, _ in result.failures] == [ExportFormat.PDF]
        assert not result.ok

    def test_text_formats_without_a_transcript_explain_why(self, project, tmp_path):
        project.transcript.replace_all([])
        result = run_export(
            build_document(project), ExportSettings(formats=[ExportFormat.SRT]), tmp_path
        )
        assert result.failures and "no transcript" in result.failures[0][1]

    def test_nothing_chosen_is_reported(self, document, tmp_path):
        result = run_export(document, ExportSettings(formats=[]), tmp_path)
        assert not result.written and result.failures


# -- AI translation request ------------------------------------------------------------


class TestAiTranslationRequest:
    def test_the_request_is_on_top_once_and_the_whole_lecture_follows(self, document, project):
        from lecture_saver.export.ai_request import TRANSLATION_REQUEST, ai_request_markdown

        text = ai_request_markdown(document, output_dir=project.exports_dir)
        assert text.startswith("# AI 직역 요청")
        assert text.count("# AI 직역 요청") == 1
        request_end = text.index("아래부터 강의 전사입니다.")
        lecture = text[request_end:]
        assert "# 머신러닝 1주차" in lecture
        assert "## Slide 1 · Before notes" in lecture and "## Slide 2" in lecture
        assert "learning rate를 보시면" in lecture
        assert TRANSLATION_REQUEST.strip() in text

    def test_the_request_asks_for_korean_under_each_timestamped_line(self):
        from lecture_saver.export.ai_request import TRANSLATION_REQUEST

        for rule in (
            "바로 다음 줄에 그 줄의 한국어 직역",               # the shape asked for
            "`[HH:MM:SS]` 형태로 옮기세요",                     # stamps kept for matching
            "영어를 지우면 안 됩니다",                          # the English is kept
            "모든 줄을 빠짐없이",                                # nothing is dropped
            "줄을 합치거나 나누지 마세요",                       # the grouping is kept
            "전공 용어는 영어로",
            "요약하거나",
            "[00:12:34] If the learning rate is too large,\nlearning rate가 너무 크면",
            "Markdown(.md) 파일",                                # comes back into the app
            "PDF 파일",                                          # and one to read
        ):
            assert rule in TRANSLATION_REQUEST

    def test_the_lecture_part_has_a_timestamp_on_every_line(self, document):
        from lecture_saver.export.ai_request import ai_request_markdown
        from lecture_saver.export.document import spoken

        text = ai_request_markdown(document)
        lecture = text[text.index("아래부터 강의 전사입니다."):]
        assert lecture.count("**[00:") == len(spoken(document.segments))

    @needs_korean_font
    def test_both_files_are_written_with_the_request_first(self, document, tmp_path):
        from lecture_saver.export.ai_request import ai_request_name, run_ai_request_export

        markdown = ai_request_name(document.title, "md")
        pdf = ai_request_name(document.title, "pdf")
        result = run_ai_request_export(document, PdfOptions(), tmp_path)

        assert result.ok
        # The lecture's own name leads, so a folder of requests stays readable.
        assert sorted(p.name for p in result.written) == sorted([markdown, pdf])
        assert markdown.endswith("_ai_translation.md")
        pages, _ = _pdf_text(tmp_path / pdf)
        first, rest = _pdf_pages(tmp_path / pdf)
        assert "AI 직역 요청" in first
        # Only the lecture's own content proves where the notes start: the
        # request itself mentions slides and labels.
        for lecture_only in ("오늘은 머신러닝", "learning rate를 보시면", "00:00:00 – 00:00:30"):
            assert lecture_only not in first
            assert lecture_only in rest
        assert pages >= 2

    @needs_korean_font
    def test_the_pdf_keeps_the_transcript_even_if_slides_only_was_chosen(
        self, document, tmp_path
    ):
        from lecture_saver.export.ai_request import ai_request_name, run_ai_request_export

        run_ai_request_export(document, PDF_PRESETS["Slides Only"], tmp_path)
        _, rest = _pdf_pages(tmp_path / ai_request_name(document.title, "pdf"))
        assert "learning rate" in rest

    def test_without_a_transcript_nothing_is_written(self, project, tmp_path):
        from lecture_saver.export.ai_request import run_ai_request_export

        project.transcript.replace_all([])
        result = run_ai_request_export(build_document(project), PdfOptions(), tmp_path)
        assert not result.written and result.failures


def _pdf_pages(path: Path) -> tuple[str, str]:
    """Text of the first page, and of every later page."""
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    pdf = QPdfDocument()
    pdf.load(str(path))
    texts = [pdf.getAllText(page).text() for page in range(pdf.pageCount())]
    pdf.close()
    return texts[0], "\n".join(texts[1:])


def test_the_ai_request_export_reports_each_file(document, tmp_path, monkeypatch):
    """The window draws a progress bar from these calls, so they must arrive."""
    from lecture_saver.export.ai_request import run_ai_request_export

    seen = []

    # The PDF needs a Korean-capable font; the request is about the callbacks.
    monkeypatch.setattr(
        "lecture_saver.export.ai_request.write_pdf",
        lambda *args, **kwargs: Path(args[2]).write_bytes(b"%PDF-1.4\n"),
    )

    run_ai_request_export(
        document, PdfOptions(), tmp_path,
        on_progress=lambda fmt, position, total: seen.append((fmt, position, total)),
    )

    assert [(position, total) for _, position, total in seen] == [(0, 2), (1, 2), (2, 2)]
    assert seen[0][0] is ExportFormat.MARKDOWN


class TestAiRequestFileNames:
    """The lecture's own name leads, so a folder of requests stays readable."""

    def test_the_lecture_name_comes_first(self):
        from lecture_saver.export.ai_request import ai_request_name

        assert ai_request_name("OS02-LimitedDirectExecution", "md") == (
            "OS02-LimitedDirectExecution_ai_translation.md"
        )
        assert ai_request_name("OS02-LimitedDirectExecution", "pdf") == (
            "OS02-LimitedDirectExecution_ai_translation.pdf"
        )

    def test_spaces_become_underscores(self):
        from lecture_saver.export.ai_request import ai_request_name

        assert ai_request_name("Machine Learning 04", "md") == (
            "Machine_Learning_04_ai_translation.md"
        )

    def test_korean_names_are_kept(self):
        from lecture_saver.export.ai_request import ai_request_name

        assert ai_request_name("9주차_상표권", "md") == "9주차_상표권_ai_translation.md"

    def test_characters_a_file_name_cannot_hold_are_replaced(self):
        from lecture_saver.export.ai_request import ai_request_name

        messy = r'a/b\c:d*e?f"g<h>i|j'
        assert ai_request_name(messy, "md") == "a_b_c_d_e_f_g_h_i_j_ai_translation.md"

    def test_a_name_with_nothing_usable_still_makes_a_file(self):
        from lecture_saver.export.ai_request import ai_request_name

        assert ai_request_name("   ", "md") == "lecture_ai_translation.md"
        assert ai_request_name("...", "md") == "lecture_ai_translation.md"

    def test_a_dotted_extension_is_accepted(self):
        from lecture_saver.export.ai_request import ai_request_name

        assert ai_request_name("Lecture", ".md") == "Lecture_ai_translation.md"


class TestTranslationInThePdf:
    """An imported answer, English paragraph then Korean, under each slide."""

    #: The shape the request asks for: English, a blank line, then Korean.
    ANSWER = (
        "## Slide 1\n"
        "Today we look at the basics.\n\n"
        "오늘은 기본을 봅니다.\n\n"
        "## Slide 2\n"
        "If this value is too large, the model diverges.\n\n"
        "이 값이 너무 크면 모델이 발산합니다.\n"
    )

    @needs_korean_font
    def test_both_languages_are_in_the_notes_under_their_slides(self, project, tmp_path):
        result = project.import_translation(self.ANSWER, source="answer.md")
        assert len(result.matched) == 2

        pdf = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(transcript=False, translation=True), pdf)
        _, text = _pdf_text(pdf)

        for line in ("Today we look at the basics.", "오늘은 기본을 봅니다.",
                     "If this value is too large, the model diverges.",
                     "이 값이 너무 크면 모델이 발산합니다."):
            assert line in text
        # English comes first, its Korean right after, and only once.
        assert text.index("Today we look") < text.index("오늘은 기본을")
        assert text.count("Today we look at the basics.") == 1
        assert "원문 · 한국어 직역" not in text

    @needs_korean_font
    def test_the_pairs_replace_the_plain_transcript(self, project, tmp_path):
        project.import_translation(self.ANSWER, source="answer.md")

        pdf = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(), pdf)
        _, text = _pdf_text(pdf)
        assert "If this value is too large, the model diverges." in text
        assert text.count("오늘은 기본을 봅니다.") == 1
        assert "Transcript" not in text

    @needs_korean_font
    def test_a_korean_only_translation_keeps_its_plain_label(self, project, tmp_path):
        project.import_translation("## Slide 1\n오늘은 기본을 봅니다.\n", source="answer.md")

        pdf = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(transcript=False, translation=True), pdf)
        _, text = _pdf_text(pdf)

        assert "한국어 직역" in text
        assert "원문 · 한국어 직역" not in text

    def test_every_english_paragraph_keeps_its_korean_one(self, project):
        import re

        hangul = re.compile(r"[\uac00-\ud7a3]")
        project.import_translation(self.ANSWER, source="answer.md")
        sections = [s for s in build_document(project).sections if s.translation]

        assert len(sections) == 2
        for section in sections:
            lines = [line for line in section.translation.splitlines() if line.strip()]
            english = [line for line in lines if not hangul.search(line)]
            korean = [line for line in lines if hangul.search(line)]
            assert len(english) == len(korean) == 1
            # Stored as two paragraphs, so a Markdown viewer shows them apart.
            assert "\n\n" in section.translation

    @needs_korean_font
    def test_an_answer_without_the_blank_line_still_reads_the_same(self, project, tmp_path):
        """Answers made with the earlier request put Korean on the very next line."""
        tight = self.ANSWER.replace(".\n\n오늘", ".\n오늘").replace(".\n\n이 값", ".\n이 값")
        project.import_translation(tight, source="answer.md")

        pdf = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(transcript=False, translation=True), pdf)
        _, text = _pdf_text(pdf)
        assert "오늘은 기본을 봅니다." in text
        assert text.index("Today we look") < text.index("오늘은 기본을")
