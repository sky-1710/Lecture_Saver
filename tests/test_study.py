"""The study pack: preview, notes and review quiz (§25, §28, §32, §33).

The assistant is never really called: a fake engine returns canned JSON. What
is tested is everything the application itself is responsible for - that a
bad answer is refused instead of printed, that each part is saved as it
arrives, that re-making the PDF costs nothing, that the answers are spread
over A-D, and that the PDF puts the parts in the order a student uses them.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.core.study import (
    QUIZ_LENGTH,
    Quiz,
    QuizQuestion,
    StudyError,
    StudyGuide,
    StudySet,
    transcript_fingerprint,
)
from lecture_saver.export.document import build_document
from lecture_saver.export.pdf import find_korean_font
from lecture_saver.study.generator import (
    PART_GUIDE,
    PART_QUIZ,
    balance_answers,
    parse_json_answer,
    run_study,
)
from lecture_saver.study.prompts import (
    GUIDE_SCHEMA,
    QUIZ_SCHEMA,
    guide_prompt,
    lecture_text,
    quiz_prompt,
)
from lecture_saver.translate.engine import (
    EngineAvailability,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)


# -- fixtures ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    info = VideoInfo(path=str(video), duration=40.0, has_audio=True)
    proj = Project.create(tmp_path / "projects", "OS05 Scheduling", info)
    proj.transcript.replace_all(
        [
            TranscriptSegment(id="segment_001", start=0.0, end=5.0,
                              original_text="Today we look at lottery scheduling."),
            TranscriptSegment(id="segment_002", start=12.0, end=18.0,
                              original_text="Each job holds tickets.",
                              edited_text="Each job holds lottery tickets."),
            TranscriptSegment(id="segment_003", start=22.0, end=30.0,
                              original_text="Stride scheduling is deterministic."),
        ],
        language="en",
    )
    proj.save()
    proj.set_slides(SlideSet(
        slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/002.jpg")],
        appearances=[SlideAppearance(1, 1, 0.0, 20.0), SlideAppearance(2, 2, 20.0, 40.0)],
    ))
    proj.save_slides()
    return proj


@pytest.fixture
def document(project):
    return build_document(project)


def _guide_dict(**overrides):
    data = {
        "summary": "운영체제가 CPU를 나눠 주는 방법을 다룹니다.",
        "key_points": ["lottery scheduling은 ticket으로 정합니다.", "stride는 정확합니다."],
        "terms": [{"term": "ticket", "meaning": "CPU를 받을 몫"}],
        "sections": [
            {"title": "복권 스케줄링", "slides": [1], "explanation": "첫 문단.\n\n둘째 문단.",
             "example": "피자를 나누는 것과 같습니다."},
            {"title": "stride", "slides": [2, 99], "explanation": "규칙대로 나눕니다.",
             "example": ""},
        ],
    }
    data.update(overrides)
    return data


def _question(n, answer="A", **overrides):
    data = {
        "question": f"Question number {n} about lottery scheduling?",
        "options": [f"Right {n}", f"Wrong one {n}", f"Wrong two {n}", f"Wrong three {n}"],
        "answer": answer,
        "explanation": "ticket이 많을수록 자주 뽑힙니다.",
        "slides": [1],
    }
    data.update(overrides)
    return data


def _quiz_dict(count=QUIZ_LENGTH):
    return {"questions": [_question(n) for n in range(1, count + 1)]}


class FakeEngine(TranslationEngine):
    """Answers each part with canned JSON and records what it was asked."""

    key = "fake"
    label = "Fake engine"

    def __init__(self, *, guide=None, quiz=None, fail=None, hang=False):
        self.calls: list[tuple[str, dict | None]] = []
        self.guide = _guide_dict() if guide is None else guide
        self.quiz = _quiz_dict() if quiz is None else quiz
        self.fail = fail or {}
        self.hang = hang

    def availability(self):
        return EngineAvailability(ready=True, detail="fake")

    def translate(self, prompt, *, cancel):  # pragma: no cover - not used here
        raise AssertionError("the study pack must ask with a schema")

    def ask(self, prompt, *, cancel, schema=None):
        part = PART_QUIZ if schema is QUIZ_SCHEMA else PART_GUIDE
        self.calls.append((part, schema))
        if self.hang:
            while not cancel.is_set():
                cancel.wait(0.01)
            raise TranslationCancelled("stopped")
        if part in self.fail:
            raise self.fail[part]
        payload = self.quiz if part == PART_QUIZ else self.guide
        return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)


# -- checking what the assistant returns -------------------------------------------------


class TestGuideValidation:
    def test_a_good_answer_is_kept(self):
        guide = StudyGuide.from_dict(_guide_dict(), known_slides={1, 2})
        assert guide.key_points[0].startswith("lottery")
        assert guide.sections[0].paragraphs == ["첫 문단.", "둘째 문단."]

    def test_slide_numbers_the_lecture_does_not_have_are_dropped(self):
        guide = StudyGuide.from_dict(_guide_dict(), known_slides={1, 2})
        assert guide.sections[1].slides == (2,)

    def test_without_key_points_it_is_refused(self):
        with pytest.raises(StudyError, match="key points"):
            StudyGuide.from_dict(_guide_dict(key_points=[]))

    def test_without_any_explanation_it_is_refused(self):
        with pytest.raises(StudyError):
            StudyGuide.from_dict(_guide_dict(sections=[{"title": "x", "explanation": ""}]))

    def test_not_an_object_is_refused(self):
        with pytest.raises(StudyError):
            StudyGuide.from_dict(["not", "a", "guide"])


class TestQuizValidation:
    def test_exactly_the_quiz_length_is_kept(self):
        quiz = Quiz.from_dict({"questions": [_question(n) for n in range(1, 21)]})
        assert len(quiz.questions) == QUIZ_LENGTH

    def test_too_few_usable_questions_is_refused_not_printed_short(self):
        with pytest.raises(StudyError, match="14 usable questions"):
            Quiz.from_dict(_quiz_dict(14))

    def test_letters_the_assistant_added_are_removed(self):
        lettered = ["A. one", "(B) two", "C) three", "d: four"]
        q = QuizQuestion.from_dict(_question(1, options=lettered))
        assert q is not None
        assert q.options == ("one", "two", "three", "four")

    @pytest.mark.parametrize(
        "broken",
        [
            {"options": ["only", "three", "options"]},
            {"answer": "E"},
            {"answer": ""},
            {"question": ""},
            {"options": ["same", "same", "other", "another"]},
        ],
    )
    def test_a_question_that_cannot_be_printed_safely_is_dropped(self, broken):
        assert QuizQuestion.from_dict(_question(1, **broken)) is None

    def test_a_lowercase_answer_is_accepted(self):
        q = QuizQuestion.from_dict(_question(1, answer="c"))
        assert q is not None and q.answer == "C"
        assert q.answer_text == "Wrong two 1"


class TestParsing:
    def test_bare_json(self):
        assert parse_json_answer('{"a": 1}') == {"a": 1}

    def test_json_in_a_code_fence(self):
        assert parse_json_answer('Here you go:\n```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_after_a_sentence(self):
        assert parse_json_answer('Sure! {"a": {"b": 2}} Hope this helps.') == {"a": {"b": 2}}

    def test_prose_is_refused(self):
        with pytest.raises(StudyError, match="expected format"):
            parse_json_answer("I could not do that.")


class TestBalancing:
    def test_every_answer_still_points_at_the_same_text(self):
        quiz = Quiz.from_dict(_quiz_dict())
        balanced = balance_answers(quiz, "seed")
        for before, after in zip(quiz.questions, balanced.questions, strict=True):
            assert after.answer_text == before.answer_text
            assert sorted(after.options) == sorted(before.options)

    def test_answers_that_were_all_a_are_spread_out(self):
        balanced = balance_answers(Quiz.from_dict(_quiz_dict()), "seed")
        letters = {q.answer for q in balanced.questions}
        assert len(letters) >= 3

    def test_the_same_transcript_gives_the_same_quiz(self):
        """Re-making the PDF must not reshuffle a quiz the student already did."""
        quiz = Quiz.from_dict(_quiz_dict())
        assert balance_answers(quiz, "seed") == balance_answers(quiz, "seed")

    def test_an_option_that_points_at_others_is_not_shuffled(self):
        options = ["Tickets", "Priority", "Both A and B", "None of the above"]
        data = {"questions": [_question(n, options=options, answer="C") for n in range(15)]}
        balanced = balance_answers(Quiz.from_dict(data), "seed")
        assert all(q.options == tuple(options) and q.answer == "C" for q in balanced.questions)


# -- what is sent ------------------------------------------------------------------------


class TestPrompts:
    def test_only_words_are_sent(self, document):
        text = lecture_text(document)
        assert "slides/" not in text and ".jpg" not in text
        assert "lecture.mp4" not in text

    def test_the_edited_transcript_is_what_is_sent(self, document):
        """§14: the corrected text, not Whisper's draft, is the lecture."""
        text = lecture_text(document)
        assert "lottery tickets" in text
        assert "Each job holds tickets." not in text

    def test_slide_headings_let_the_answer_point_back(self, document):
        text = lecture_text(document)
        assert "## Slide 1" in text and "## Slide 2" in text

    def test_the_preview_is_asked_for_simply_in_korean(self, document):
        prompt = guide_prompt(document)
        assert "중학생도 이해할 수 있는 쉬운 한국어" in prompt
        assert "지어내지 마세요" in prompt
        assert "빠짐없이" in prompt

    def test_the_quiz_is_asked_for_in_english_with_korean_explanations(self, document):
        prompt = quiz_prompt(document)
        assert f"정확히 {QUIZ_LENGTH}문제" in prompt
        assert "question과 options는 **영어**" in prompt
        assert "explanation은 **한국어**" in prompt
        assert "All of the above" in prompt  # named as forbidden

    @pytest.mark.parametrize("schema", [GUIDE_SCHEMA, QUIZ_SCHEMA])
    def test_schemas_follow_strict_mode(self, schema):
        """Every object lists every property as required and allows no others."""

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(schema)


# -- running -----------------------------------------------------------------------------


class TestRunStudy:
    def test_both_parts_are_made_and_handed_back_one_at_a_time(self, document):
        got = []
        report = run_study(document, FakeEngine(), on_part=lambda p, v: got.append(p))
        assert got == [PART_GUIDE, PART_QUIZ]
        assert report.ok

    def test_every_request_carries_its_schema(self, document):
        engine = FakeEngine()
        run_study(document, engine)
        assert engine.calls == [(PART_GUIDE, GUIDE_SCHEMA), (PART_QUIZ, QUIZ_SCHEMA)]

    def test_only_the_missing_part_is_asked_for(self, document):
        engine = FakeEngine()
        run_study(document, engine, parts=(PART_QUIZ,))
        assert [c[0] for c in engine.calls] == [PART_QUIZ]

    def test_a_failed_quiz_keeps_the_preview(self, document):
        got = []
        engine = FakeEngine(fail={PART_QUIZ: TranslationError("network down")})
        report = run_study(document, engine, on_part=lambda p, v: got.append(p))
        assert got == [PART_GUIDE]
        assert "network down" in report.errors[0]

    def test_a_malformed_quiz_is_refused_with_a_reason(self, document):
        report = run_study(document, FakeEngine(quiz=_quiz_dict(9)))
        assert report.made == [PART_GUIDE]
        assert "9 usable questions" in report.errors[0]

    def test_a_usage_limit_stops_before_the_second_request(self, document):
        limit = EngineRateLimited("Your usage limit has been reached.")
        engine = FakeEngine(fail={PART_GUIDE: limit})
        run_study(document, engine)
        assert [c[0] for c in engine.calls] == [PART_GUIDE]

    def test_stopping_is_not_an_error(self, document):
        cancel = threading.Event()
        timer = threading.Timer(0.05, cancel.set)
        timer.start()
        report = run_study(document, FakeEngine(hang=True), cancel=cancel)
        timer.cancel()
        assert report.cancelled and not report.errors


# -- storage -------------------------------------------------------------------------------


class TestStorage:
    def _study(self):
        study = StudySet()
        study.set_guide(StudyGuide.from_dict(_guide_dict()), source="abc", engine="codex")
        study.set_quiz(Quiz.from_dict(_quiz_dict()), source="abc")
        return study

    def test_it_survives_closing_the_project(self, project):
        project.study = self._study()
        project.save_study()
        reopened = Project.open(project.root)
        assert reopened.study.to_dict() == project.study.to_dict()
        assert reopened.study.is_complete

    def test_a_damaged_file_costs_the_material_not_the_project(self, project):
        project.study_file.write_text("{ not json", encoding="utf-8")
        reopened = Project.open(project.root)
        assert reopened.study.is_empty

    def test_a_damaged_quiz_keeps_the_preview(self, project):
        data = self._study().to_dict()
        data["quiz"] = {"questions": [{"question": "broken"}]}
        project.study_file.write_text(json.dumps(data), encoding="utf-8")
        reopened = Project.open(project.root)
        assert reopened.study.guide is not None
        assert reopened.study.quiz is None

    def test_editing_the_text_makes_it_stale_but_retiming_does_not(self, project):
        from dataclasses import replace

        segments = list(project.transcript.segments)
        study = self._study()
        study.guide_source = study.quiz_source = transcript_fingerprint(segments)

        retimed = [replace(segments[0], start=1.0, end=6.0), *segments[1:]]
        assert study.stale_parts(transcript_fingerprint(retimed)) == []

        edited = [
            replace(segments[0], edited_text="Today we look at stride scheduling."),
            *segments[1:],
        ]
        assert study.stale_parts(transcript_fingerprint(edited)) == ["preview", "quiz"]


# -- the PDF -------------------------------------------------------------------------------


def _pdf_page_texts(path: Path) -> list[str]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])  # noqa: F841
    pdf = QPdfDocument()
    pdf.load(str(path))
    pages = [pdf.getAllText(i).text() for i in range(pdf.pageCount())]
    pdf.close()
    return pages


@needs_korean_font
class TestStudyPdf:
    def _pack(self, document, tmp_path, *, quiz=True, guide=True):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.export.study_pdf import write_study_pack

        study = StudySet()
        if guide:
            study.set_guide(StudyGuide.from_dict(_guide_dict()), source="x")
        if quiz:
            study.set_quiz(balance_answers(Quiz.from_dict(_quiz_dict()), "s"), source="x")
        target = tmp_path / "pack.pdf"
        write_study_pack(document, study, PdfOptions(slide_image=False), target)
        return target, study

    def test_the_parts_come_in_the_order_a_student_uses_them(self, document, tmp_path):
        target, _ = self._pack(document, tmp_path)
        text = "\n".join(_pdf_page_texts(target))
        order = ["이 자료의 구성", "먼저 알아야 할 핵심", "강의 노트", "복습 퀴즈", "정답과 해설"]
        positions = [text.index(marker) for marker in order[:1]]
        for marker in order[1:]:
            positions.append(text.index(marker, positions[-1]))
        assert positions == sorted(positions)

    def test_the_answers_start_on_a_page_of_their_own(self, document, tmp_path):
        """A student working through the quiz must not see the answers."""
        target, study = self._pack(document, tmp_path)
        pages = _pdf_page_texts(target)
        # The cover's contents also say "정답과 해설"; the banner is unique.
        answer_page = next(i for i, p in enumerate(pages) if "Answers & Explanations" in p)
        quiz_page = next(i for i, p in enumerate(pages) if "Choose the best answer" in p)
        assert quiz_page < answer_page
        assert "Choose the best answer" not in pages[answer_page]
        first = study.quiz.questions[0]
        assert f"정답 {first.answer}" in pages[answer_page]
        # No answer is printed anywhere from the quiz up to the answer page.
        assert all("정답 " not in p for p in pages[quiz_page:answer_page])

    def test_the_notes_use_the_edited_transcript(self, document, tmp_path):
        target, _ = self._pack(document, tmp_path)
        text = "\n".join(_pdf_page_texts(target))
        assert "lottery tickets" in text
        assert "Each job holds tickets." not in text

    def test_korean_and_english_are_both_real_text(self, document, tmp_path):
        target, _ = self._pack(document, tmp_path)
        text = "\n".join(_pdf_page_texts(target))
        assert "운영체제가 CPU를" in text
        assert "Question number 1 about lottery scheduling?" in text

    def test_a_pack_without_a_quiz_leaves_the_quiz_out(self, document, tmp_path):
        target, _ = self._pack(document, tmp_path, quiz=False)
        text = "\n".join(_pdf_page_texts(target))
        assert "먼저 알아야 할 핵심" in text
        assert "정답과 해설" not in text

    def test_nothing_to_print_is_an_error_not_an_empty_pdf(self, document, tmp_path):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.export.pdf import PdfExportError
        from lecture_saver.export.study_pdf import write_study_pack

        with pytest.raises(PdfExportError):
            write_study_pack(document, StudySet(), PdfOptions(), tmp_path / "x.pdf")
        assert not (tmp_path / "x.pdf").exists()

    def test_slide_ranges_read_naturally(self):
        from lecture_saver.export.study_pdf import describe_slides

        assert describe_slides((3, 4, 5, 8)) == "Slides 3–5, 8"
        assert describe_slides((2,)) == "Slide 2"
        assert describe_slides(()) == ""


# -- the worker and the dialog ------------------------------------------------------------


@needs_korean_font
class TestWorker:
    def test_it_makes_the_parts_then_the_pdf(self, qapp, document, tmp_path):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.ui.workers import StudyPackWorker

        target = tmp_path / "out" / "pack.pdf"
        worker = StudyPackWorker(
            document, FakeEngine(), StudySet(), (PART_GUIDE, PART_QUIZ),
            PdfOptions(slide_image=False), target,
        )
        parts, results = [], []
        worker.part_ready.connect(lambda p, v: parts.append(p))
        worker.finished.connect(results.append)
        worker.run()  # directly, on this thread
        assert parts == [PART_GUIDE, PART_QUIZ]
        assert results[0].pdf == target and target.is_file()

    def test_with_everything_saved_it_asks_nothing(self, qapp, document, tmp_path):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.ui.workers import StudyPackWorker

        study = StudySet()
        study.set_guide(StudyGuide.from_dict(_guide_dict()), source="x")
        study.set_quiz(Quiz.from_dict(_quiz_dict()), source="x")
        engine = FakeEngine()
        worker = StudyPackWorker(
            document, engine, study, (), PdfOptions(slide_image=False), tmp_path / "p.pdf"
        )
        results = []
        worker.finished.connect(results.append)
        worker.run()
        assert engine.calls == []
        assert results[0].pdf is not None

    def test_a_missing_part_means_no_pdf(self, qapp, document, tmp_path):
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.ui.workers import StudyPackWorker

        engine = FakeEngine(fail={PART_QUIZ: TranslationError("broke")})
        worker = StudyPackWorker(
            document, engine, StudySet(), (PART_GUIDE, PART_QUIZ),
            PdfOptions(slide_image=False), tmp_path / "p.pdf",
        )
        results = []
        worker.finished.connect(results.append)
        worker.run()
        assert results[0].pdf is None
        assert not (tmp_path / "p.pdf").exists()


class TestDialog:
    def _dialog(self, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.export.options import PdfOptions
        from lecture_saver.ui.study_dialog import StudyPackDialog

        settings = Settings(tmp_path / "settings.json")
        return StudyPackDialog(
            project, settings, build_document(project), PdfOptions(), tmp_path / "out"
        )

    def test_a_new_lecture_asks_for_both_parts(self, qapp, project, tmp_path):
        dialog = self._dialog(project, tmp_path)
        assert dialog.parts_to_make() == (PART_GUIDE, PART_QUIZ)
        assert dialog._start_button.text() == "Create study pack"

    def test_with_both_saved_the_pdf_is_free(self, qapp, project, tmp_path):
        project.study.set_guide(StudyGuide.from_dict(_guide_dict()),
                                source=transcript_fingerprint(project.transcript.segments))
        project.study.set_quiz(Quiz.from_dict(_quiz_dict()),
                               source=transcript_fingerprint(project.transcript.segments))
        dialog = self._dialog(project, tmp_path)
        assert dialog.parts_to_make() == ()
        assert dialog._start_button.text() == "Create PDF"
        assert "nothing is sent" in dialog._plan_label.text()
        assert dialog._stale_label.isHidden()

    def test_after_an_edit_it_offers_to_write_them_again(self, qapp, project, tmp_path):
        project.study.set_guide(StudyGuide.from_dict(_guide_dict()), source="old")
        project.study.set_quiz(Quiz.from_dict(_quiz_dict()), source="old")
        dialog = self._dialog(project, tmp_path)
        assert not dialog._stale_label.isHidden()
        dialog._redo.setChecked(True)
        assert dialog.parts_to_make() == (PART_GUIDE, PART_QUIZ)

    def test_a_saved_part_reaches_the_project_file(self, qapp, project, tmp_path):
        dialog = self._dialog(project, tmp_path)
        dialog._on_part(PART_GUIDE, StudyGuide.from_dict(_guide_dict()))
        assert Project.open(project.root).study.guide is not None

    def test_the_file_is_named_after_the_lecture(self, qapp, project, tmp_path):
        dialog = self._dialog(project, tmp_path)
        assert dialog._output.name == "OS05_Scheduling_study_pack.pdf"

    def test_the_agreement_covers_the_study_pack_too(self):
        from lecture_saver.ui.ai_consent import consent_text

        text = consent_text("Codex CLI")
        assert "study material" in text
        assert "never sent" in text and "slide images" in text


class TestWindow:
    def test_the_button_and_the_menu_open_the_study_pack(self, qapp, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            assert not window._study_pack_action.isEnabled()
            window._open_project_at(project.root)
            assert window._study_pack_action.isEnabled()
            seen = []
            window._study_pack = lambda: seen.append(True)  # type: ignore[method-assign]
            window._lecture_view.studyPackRequested.disconnect()
            window._lecture_view.studyPackRequested.connect(window._study_pack)
            window._lecture_view._study_pack_button.click()
            assert seen == [True]
        finally:
            window.close()
