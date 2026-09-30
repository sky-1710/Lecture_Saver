"""Importing an AI assistant's translation and exporting it with the slides."""

import json
from pathlib import Path

import pytest

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet, delete_appearance
from lecture_saver.core.translations import (
    SlideTranslation,
    TranslationSet,
    match_translations,
    parse_translation_text,
)
from lecture_saver.export.ai_request import ai_request_markdown
from lecture_saver.export.document import build_document
from lecture_saver.export.options import PDF_PRESETS, PdfOptions
from lecture_saver.export.pdf import find_korean_font, write_pdf
from lecture_saver.export.text_formats import to_json, to_markdown

ANSWER = """네, 요청하신 형식으로 번역했습니다.

## Slide 1
오늘은 machine learning의 기본 개념부터 시작하겠습니다.

## Slide 2 · With notes
learning rate가 너무 크면 모델이 발산할 수 있습니다.

반대로 너무 작으면 학습이 매우 느려집니다.

(Slide 3 전사의 한국어 직역)

이어서 받으려면 "계속"이라고 입력해 주세요.
"""


def appearances(*bounds):
    return [
        SlideAppearance(appearance_id=i + 1, slide_id=i + 1, start=a, end=b)
        for i, (a, b) in enumerate(zip(bounds, bounds[1:], strict=False))
    ]


class TestParsing:
    def test_sections_are_found_and_noise_removed(self):
        parsed = parse_translation_text(ANSWER)
        assert parsed.slides == {
            1: "오늘은 machine learning의 기본 개념부터 시작하겠습니다.",
            2: "learning rate가 너무 크면 모델이 발산할 수 있습니다.\n\n"
               "반대로 너무 작으면 학습이 매우 느려집니다.",
        }

    @pytest.mark.parametrize(
        "heading", ["## Slide 7", "### slide 7 · Before notes", "**Slide 7**",
                    "Slide 7", "슬라이드 7", "# Slide 7: 상표권"]
    )
    def test_heading_styles(self, heading):
        assert parse_translation_text(f"{heading}\n번역").slides == {7: "번역"}

    def test_rules_between_paragraphs_keep_them_apart(self):
        from lecture_saver.export.document import TranslationBlock, translation_blocks

        answer = (
            "---\n## Slide 1\n---\nFirst part.\n---\n첫 부분입니다.\n---\n"
            "Second part.\n---\n둘째 부분입니다.\n---\n\n"
            "---\n## Slide 2\n---\nMore.\n---\n더.\n---\n"
        )
        parsed = parse_translation_text(answer)
        assert parsed.slides == {
            1: "First part.\n\n첫 부분입니다.\n\nSecond part.\n\n둘째 부분입니다.",
            2: "More.\n\n더.",
        }
        assert translation_blocks(parsed.slides[1]) == [
            TranslationBlock("First part.", False), TranslationBlock("첫 부분입니다.", True),
            TranslationBlock("Second part.", False), TranslationBlock("둘째 부분입니다.", True),
        ]

    def test_a_sentence_starting_with_slide_is_not_a_heading(self):
        parsed = parse_translation_text("## Slide 1\nSlide 3에서 보셨듯이 그렇습니다.")
        assert parsed.slides == {1: "Slide 3에서 보셨듯이 그렇습니다."}

    def test_the_example_inside_the_request_is_ignored(self):
        text = "```\n## Slide 1\n(Slide 1 전사의 한국어 직역)\n```\n## Slide 2\n진짜 번역"
        assert parse_translation_text(text).slides == {2: "진짜 번역"}

    def test_a_continuation_pasted_after_replaces_the_same_slide(self):
        text = "## Slide 1\n앞부분\n## Slide 2\n둘\n\n## Slide 1\n고친 번역"
        assert parse_translation_text(text).slides == {1: "고친 번역", 2: "둘"}

    def test_the_whole_request_file_itself_translates_nothing(self, tmp_path):
        from lecture_saver.export.ai_request import TRANSLATION_REQUEST

        parsed = parse_translation_text(TRANSLATION_REQUEST)
        assert parsed.is_empty


class TestMatching:
    def test_numbers_are_matched_to_the_current_slides(self):
        parsed = parse_translation_text("## Slide 1\na\n## Slide 3\nc\n## Slide 9\nx")
        result = match_translations(parsed, appearances(0, 10, 20, 30))
        assert [m.appearance_id for m in result.matched] == [1, 3]
        assert result.unknown == (9,)
        assert result.missing == (2,)

    def test_a_translation_only_follows_the_same_stretch_of_lecture(self):
        entry = SlideTranslation(appearance_id=2, slide_id=2, start=10.0, end=20.0, text="t")
        assert entry.covers(SlideAppearance(2, 2, 10.0, 20.0))
        assert entry.covers(SlideAppearance(1, 1, 0.0, 20.0)), "merged into a neighbour"
        assert not entry.covers(SlideAppearance(2, 2, 20.0, 40.0)), "a different slide now"

    def test_merging_replaces_only_the_same_slides(self):
        translations = TranslationSet(entries=[
            SlideTranslation(1, 1, 0, 10, "old one"), SlideTranslation(2, 2, 10, 20, "two"),
        ])
        translations.merge([SlideTranslation(1, 1, 0, 10, "new one")])
        texts = {e.appearance_id: e.text for e in translations.entries}
        assert texts == {1: "new one", 2: "two"}


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    proj = Project.create(tmp_path / "p", "ML 101", VideoInfo(path=str(video), duration=30.0))
    proj.transcript.replace_all([
        TranscriptSegment(id="segment_001", start=1, end=5,
                          original_text="Today we start with the basics of machine learning."),
        TranscriptSegment(id="segment_002", start=12, end=18,
                          original_text="If the learning rate is too large, the model diverges."),
    ], language="en")
    proj.save()
    proj.set_slides(SlideSet(
        slides=[Slide(1, ""), Slide(2, "")],
        appearances=[SlideAppearance(1, 1, 0.0, 10.0), SlideAppearance(2, 2, 10.0, 30.0)],
    ))
    proj.save_slides()
    return proj


class TestProjectTranslations:
    def test_import_saves_and_survives_reopening(self, project):
        result = project.import_translation(ANSWER, source="answer.md")
        assert len(result.matched) == 2

        reopened = Project.open(project.root)
        second = reopened.slides.appearances[1]
        assert reopened.translations.for_appearance(second).startswith("learning rate가")
        assert reopened.translations.source == "answer.md"

    def test_parts_can_be_imported_one_after_another(self, project):
        project.import_translation("## Slide 1\n첫 부분")
        project.import_translation("## Slide 2\n둘째 부분")
        first, second = project.slides.appearances
        assert project.translations.for_appearance(first) == "첫 부분"
        assert project.translations.for_appearance(second) == "둘째 부분"

    def test_preview_saves_nothing(self, project):
        project.preview_translation(ANSWER)
        assert not project.translations_file.exists()

    def test_after_slides_are_detected_differently_no_translation_is_misplaced(self, project):
        project.import_translation(ANSWER)
        # Detection now finds one slide covering 0-5 and another from 5 to the end.
        project.set_slides(SlideSet(
            slides=[Slide(1, ""), Slide(2, "")],
            appearances=[SlideAppearance(1, 1, 0.0, 5.0), SlideAppearance(2, 2, 5.0, 30.0)],
        ))
        first, second = project.slides.appearances
        assert project.translations.for_appearance(first) is not None  # inside the old slide 1
        assert project.translations.for_appearance(second).startswith("learning rate")

    def test_a_deleted_slide_gives_its_neighbour_the_longer_translation(self, project):
        project.import_translation(ANSWER)
        delete_appearance(project.slides, 1)
        only = project.slides.appearances[0]
        assert project.translations.for_appearance(only).startswith("learning rate")

    def test_clear(self, project):
        project.import_translation(ANSWER)
        project.clear_translations()
        assert project.translations.is_empty
        assert not project.translations_file.exists()
        assert Project.open(project.root).translations.is_empty

    def test_an_unreadable_file_does_not_stop_the_project_opening(self, project):
        project.translations_file.write_text("{ broken", encoding="utf-8")
        assert Project.open(project.root).translations.is_empty


class TestExportsCarryTheTranslation:
    def test_markdown_puts_it_under_the_transcript(self, project):
        project.import_translation(ANSWER)
        text = to_markdown(build_document(project), timestamps=False)
        slide2 = text[text.index("## Slide 2"):]
        assert slide2.index("If the learning rate") < slide2.index("learning rate가 너무 크면")
        # Every paragraph stands between rules, never touching the next one.
        assert "---\n\nlearning rate가 너무 크면 모델이 발산할 수 있습니다.\n\n---" in slide2
        assert "---\n\n반대로 너무 작으면 학습이 매우 느려집니다.\n\n---" in slide2

    def test_markdown_pairs_english_and_korean_between_rules(self, project):
        answer = (
            "---\n## Slide 1\n---\nToday we look at the basics.\n---\n"
            "오늘은 기본을 봅니다.\n---\n\n"
            "---\n## Slide 2\n---\nFirst part.\n---\n첫 부분입니다.\n---\n"
            "Second part.\n---\n둘째 부분입니다.\n---\n"
        )
        project.import_translation(answer)
        text = to_markdown(build_document(project), timestamps=False)

        slide2 = text[text.index("## Slide 2"):]
        body = slide2[slide2.index("---"):]
        assert body == (
            "---\n\nFirst part.\n\n---\n\n첫 부분입니다.\n\n---\n\n"
            "Second part.\n\n---\n\n둘째 부분입니다.\n\n---\n"
        )
        # The heading sits between rules too, and the transcript is not repeated.
        assert "---\n\n## Slide 1" in text
        assert text.count("Today we look at the basics.") == 1
        # A rule never follows a paragraph directly, which would make it a heading.
        assert "basics.\n---" not in text

    def test_markdown_without_the_translation_keeps_the_plain_layout(self, project):
        project.import_translation(ANSWER)
        text = to_markdown(build_document(project), timestamps=False, translation=False)
        assert "---" not in text
        assert "learning rate가" not in text

    def test_json_has_it_per_slide(self, project):
        project.import_translation("## Slide 2\n번역 둘")
        data = json.loads(to_json(build_document(project)))
        assert [s["translation"] for s in data["slides"]] == [None, "번역 둘"]

    def test_the_ai_request_never_includes_an_old_translation(self, project):
        project.import_translation(ANSWER)
        text = ai_request_markdown(build_document(project))
        assert "모델이 발산할 수 있습니다" not in text

    @pytest.mark.skipif(find_korean_font() is None, reason="no Korean font")
    def test_the_pdf_has_slide_transcript_and_translation(self, project, tmp_path):
        from tests.test_export import _pdf_text

        project.import_translation(ANSWER)
        target = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(), target)
        _, text = _pdf_text(target)
        english = text.index("If the learning rate")
        assert english < text.index("learning rate가 너무 크면")
        assert english < text.rindex("한국어 직역")

    @pytest.mark.skipif(find_korean_font() is None, reason="no Korean font")
    def test_the_translation_can_be_left_out(self, project, tmp_path):
        from tests.test_export import _pdf_text

        project.import_translation(ANSWER)
        for options in (PdfOptions(translation=False), PDF_PRESETS["Slides Only"]):
            target = tmp_path / "notes.pdf"
            write_pdf(build_document(project), options, target)
            assert "한국어 직역" not in _pdf_text(target)[1]


def test_describe_numbers():
    from lecture_saver.ui.translation_dialog import describe_numbers

    assert describe_numbers([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8"
    assert describe_numbers([]) == ""


class TestTranslationUI:
    def test_the_dialog_previews_then_imports(self, qapp, project):
        from lecture_saver.ui.translation_dialog import TranslationDialog

        dialog = TranslationDialog(project)
        changed: list[bool] = []
        dialog.changed.connect(lambda: changed.append(True))
        assert not dialog._import_button.isEnabled()

        dialog.set_text("## Slide 2\n번역 둘\n## Slide 9\n없는 슬라이드")
        assert "1 of 2 slides" in dialog.preview_text
        assert "Slide 9" in dialog.preview_text
        assert not project.translations_file.exists()

        assert dialog.import_text()
        assert changed and project.translations_file.exists()

    def test_text_without_slide_sections_cannot_be_imported(self, qapp, project):
        from lecture_saver.ui.translation_dialog import TranslationDialog

        dialog = TranslationDialog(project)
        dialog.set_text("그냥 번역문만 있고 슬라이드 제목이 없음")
        assert not dialog._import_button.isEnabled()
        assert "Slide N" in dialog.preview_text

    def test_the_slide_view_shows_the_translation_of_the_slide_shown(self, qapp, project):
        from lecture_saver.ui.lecture_view import LectureView

        project.import_translation("## Slide 2\n번역 둘")
        view = LectureView()
        view.load_project(project)
        assert view.shown_translation == ""  # slide 1 has none

        view._on_position(15.0)
        assert view.shown_translation == "번역 둘"

    def test_the_window_offers_import(self, qapp, tmp_path, project):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            assert not window._import_translation_action.isEnabled()
            window._open_project_at(Path(project.root))
            assert window._import_translation_action.isEnabled()
        finally:
            window.close()


# -- line-by-line translations ------------------------------------------------------

TIMED_ANSWER = """## Slide 1

[00:00:01] Today we start with the basics of machine learning.
오늘은 machine learning의 기본부터 시작하겠습니다.

## Slide 2

**[00:00:12]** If the learning rate is too large, the model diverges.
→ learning rate가 너무 크면 모델이 발산합니다.
"""


class TestTimedLines:
    def test_the_korean_under_each_stamp_is_read(self):
        from lecture_saver.core.translations import TimedLine, parse_timed_lines

        text = "[00:12:34] If it is large,\n크면\n\n[12:38] 한국어만\n[00:13:00] Okay."
        assert parse_timed_lines(text) == [
            TimedLine(754, "크면"), TimedLine(758, "한국어만"),
        ]

    def test_paragraph_answers_and_times_in_sentences_are_not_lines(self):
        from lecture_saver.core.translations import parse_timed_lines

        assert parse_timed_lines("Plain paragraph.\n\n한국어 문단.") == []
        assert parse_timed_lines("10:30분에 시작합니다.") == []

    def test_lines_are_paired_with_segments_in_order(self):
        from lecture_saver.export.document import pair_timed_translation

        segments = (
            TranscriptSegment(id="a", start=5.2, end=5.6, original_text="One"),
            TranscriptSegment(id="b", start=5.7, end=7.0, original_text="Two"),
            TranscriptSegment(id="c", start=9.9996, end=11, original_text="Three"),
        )
        text = "[00:00:05] One\n하나\n[00:00:05] Two\n둘\n[00:00:10] Three\n셋"
        assert pair_timed_translation(segments, text) == {"a": "하나", "b": "둘", "c": "셋"}

    def test_markdown_puts_each_korean_line_under_its_original(self, project):
        project.import_translation(TIMED_ANSWER)
        text = to_markdown(build_document(project), timestamps=False)
        assert (
            "**[00:00:12]** If the learning rate is too large, the model diverges.\n\n"
            "> learning rate가 너무 크면 모델이 발산합니다.\n"
        ) in text
        assert "---" not in text

    def test_json_carries_each_line(self, project):
        project.import_translation(TIMED_ANSWER)
        data = json.loads(to_json(build_document(project)))
        assert data["slides"][1]["line_translations"] == {
            "segment_002": "learning rate가 너무 크면 모델이 발산합니다."
        }

    @pytest.mark.skipif(find_korean_font() is None, reason="no Korean font")
    def test_the_pdf_interleaves_lines(self, project, tmp_path):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.export.pdf import write_pdf

        project.import_translation(TIMED_ANSWER)
        pdf = tmp_path / "notes.pdf"
        write_pdf(build_document(project), PdfOptions(translation=True), pdf)
        assert pdf.stat().st_size > 0
