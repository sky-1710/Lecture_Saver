"""Ask AI: questions about the lecture, answered beside the video.

The assistant is never really run: a fake engine records the prompt and
answers. What is tested is everything around it - which transcript goes with
a question, that edits are what is sent, that the conversation is kept with
the project, that citations in an answer lead back into the lecture, and that
a failed question is handed back rather than lost.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from lecture_saver.ask import ask_engine_from_settings
from lecture_saver.ask.answer import failure_message, prepare_answer
from lecture_saver.ask.prompt import (
    DEFAULT_QUESTION,
    SCOPE_LECTURE,
    SCOPE_NEARBY,
    Focus,
    build_prompt,
    question_text,
    transcript_excerpt,
)
from lecture_saver.core.chat import ROLE_ASSISTANT, ROLE_USER, ChatLog, ChatMessage, Quote
from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.export.document import build_document
from lecture_saver.translate.engine import (
    EngineAuthError,
    EngineAvailability,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

# -- fixtures ---------------------------------------------------------------------


def _lecture(tmp_path, *, slides: bool = True, count: int = 6) -> Project:
    """``count`` slides of ten seconds, each with a line naming its slide."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    info = VideoInfo(path=str(video), duration=float(count * 10), has_audio=True)
    project = Project.create(tmp_path / "projects", "ML 03", info)
    project.transcript.replace_all(
        [
            TranscriptSegment(
                id=f"segment_{i + 1:03d}", start=i * 10.0 + 1, end=i * 10.0 + 8,
                original_text=f"line of slide {i + 1}",
            )
            for i in range(count)
        ],
        language="en",
    )
    project.save()
    if slides:
        project.set_slides(SlideSet(
            slides=[Slide(i + 1, f"slides/{i + 1:03d}.jpg") for i in range(count)],
            appearances=[
                SlideAppearance(i + 1, i + 1, i * 10.0, (i + 1) * 10.0) for i in range(count)
            ],
        ))
        project.save_slides()
    return project


@pytest.fixture
def project(tmp_path):
    return _lecture(tmp_path)


class FakeEngine(TranslationEngine):
    key = "fake"
    label = "Fake engine"

    def __init__(self, answer: str = "답입니다 [00:00:21]", *, error=None, hang=False):
        self.prompts: list[str] = []
        self.answer = answer
        self.error = error
        self.hang = hang

    def availability(self):
        return EngineAvailability(ready=True, detail="fake")

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        self.prompts.append(prompt)
        if self.hang:
            while not cancel.is_set():
                cancel.wait(0.01)
            raise TranslationCancelled("stopped")
        if self.error is not None:
            raise self.error
        return self.answer


def pump_until(predicate, timeout: float = 10.0) -> bool:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


# -- the conversation on disk ------------------------------------------------------


class TestChatLog:
    def test_a_conversation_survives_a_round_trip(self):
        log = ChatLog()
        log.add(ChatMessage(
            role=ROLE_USER, text="왜?", position=12.5, slide=3, scope=SCOPE_NEARBY,
            quotes=(Quote("segment_003", 21.0, "line of slide 3"),),
        ))
        log.add(ChatMessage(role=ROLE_ASSISTANT, text="이유는 [00:00:21]"))

        again = ChatLog.from_dict(json.loads(json.dumps(log.to_dict())))
        assert again.messages == log.messages
        assert again.to_dict()["schema_version"] == 1

    def test_it_is_kept_with_the_project(self, project):
        project.chat.add(ChatMessage(role=ROLE_USER, text="질문"))
        project.save_chat()

        reopened = Project.open(project.root)
        assert [m.text for m in reopened.chat.messages] == ["질문"]

    def test_a_damaged_file_costs_the_conversation_not_the_project(self, project):
        project.chat_file.write_text("{not json", encoding="utf-8")
        reopened = Project.open(project.root)
        assert reopened.chat.is_empty
        assert len(reopened.transcript) == 6

    def test_odd_values_are_read_safely(self):
        log = ChatLog.from_dict({"messages": [
            {"role": "system", "text": None, "slide": True, "position": "x", "quotes": [1]},
            "junk",
        ]})
        message = log.messages[0]
        assert message.role == ROLE_USER
        assert message.text == ""
        assert message.slide is None and message.position is None
        assert message.quotes == ()


# -- what is sent ---------------------------------------------------------------------


class TestQuestionText:
    def test_typed_text_is_the_question(self):
        assert question_text("  왜 발산하나요? ", ()) == "왜 발산하나요?"

    def test_pointing_at_a_line_without_typing_asks_for_an_explanation(self):
        assert question_text("", [Quote("s", 1.0, "x")]) == DEFAULT_QUESTION

    def test_nothing_typed_and_nothing_pointed_at_is_no_question(self):
        assert question_text("   ", ()) == ""


class TestWhichTranscriptGoes:
    def test_nearby_is_the_slide_asked_about_and_its_neighbours(self, project):
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(slide=4))
        assert "line of slide 3" in text
        assert "line of slide 4" in text
        assert "line of slide 5" in text
        assert "line of slide 2" not in text and "line of slide 6" not in text
        assert "Slide 4 (00:00:30–00:00:40)  ← 학생이 보고 있는 슬라이드" in text

    def test_a_quoted_line_decides_the_slide_over_the_one_shown(self, project):
        document = build_document(project)
        focus = Focus(slide=1, quotes=(Quote("segment_006", 51.0, "line of slide 6"),))
        text = transcript_excerpt(document, SCOPE_NEARBY, focus)
        assert "line of slide 5" in text and "line of slide 6" in text
        assert "line of slide 1" not in text

    def test_without_a_slide_shown_the_playing_slide_is_used(self, project):
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(position=12.0))
        assert "line of slide 1" in text and "line of slide 3" in text
        assert "line of slide 4" not in text

    def test_whole_lecture_sends_every_slide(self, project):
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_LECTURE, Focus(slide=1))
        for number in range(1, 7):
            assert f"line of slide {number}" in text

    def test_every_line_carries_its_time(self, project):
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(slide=2))
        assert "[00:00:11] line of slide 2" in text

    def test_the_edited_text_is_what_is_sent(self, project):
        project.transcript.edit_text("segment_002", "corrected learning rate")
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(slide=2))
        assert "corrected learning rate" in text
        assert "line of slide 2" not in text

    def test_a_blanked_line_is_not_sent(self, project):
        project.transcript.edit_text("segment_002", "")
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(slide=2))
        assert "[00:00:11]" not in text
        assert "말한 내용이 없습니다" in text

    def test_without_slides_nearby_is_a_window_of_time(self, tmp_path):
        project = _lecture(tmp_path, slides=False, count=60)  # ten minutes
        document = build_document(project)
        text = transcript_excerpt(document, SCOPE_NEARBY, Focus(position=400.0))
        assert "line of slide 17" in text  # 161 s: inside the four minutes before
        assert "line of slide 49" in text  # 481 s: inside the minute and a half after
        assert "line of slide 10 " not in text and "line of slide 60" not in text

    def test_only_words_are_sent(self, project):
        document = build_document(project)
        prompt = build_prompt(document, "질문", focus=Focus(slide=1), scope=SCOPE_LECTURE)
        assert ".jpg" not in prompt
        assert str(project.root) not in prompt
        assert "lecture.mp4" not in prompt


class TestThePrompt:
    def test_it_carries_the_question_the_quotes_and_where_the_user_is(self, project):
        document = build_document(project)
        focus = Focus(position=33.0, slide=4, quotes=(Quote("segment_004", 31.0, "slide 4"),))
        prompt = build_prompt(document, "", focus=focus, scope=SCOPE_NEARBY)

        assert prompt.rstrip().endswith(DEFAULT_QUESTION)
        assert "## 학생이 짚은 전사 부분\n[00:00:31] slide 4" in prompt
        assert "영상 위치 [00:00:33] · Slide 4 (00:00:30–00:00:40)" in prompt
        assert "제목: ML 03" in prompt

    def test_the_rules_and_transcript_come_before_what_changes(self, project):
        """The repeated part first, so the service can reuse it."""
        document = build_document(project)
        prompt = build_prompt(document, "질문", focus=Focus(), scope=SCOPE_LECTURE)
        assert prompt.index("## 강의 전사") < prompt.index("## 질문")

    def test_earlier_turns_go_along_for_a_follow_up(self, project):
        document = build_document(project)
        history = [
            ChatMessage(role=ROLE_USER, text="러닝레이트가 뭐예요?",
                        quotes=(Quote("segment_002", 11.0, "line of slide 2"),)),
            ChatMessage(role=ROLE_ASSISTANT, text="학습률입니다."),
        ]
        prompt = build_prompt(
            document, "그럼 너무 크면요?", focus=Focus(), scope=SCOPE_NEARBY, history=history
        )
        assert "학생: > [00:00:11] line of slide 2\n러닝레이트가 뭐예요?" in prompt
        assert "조교: 학습률입니다." in prompt

    def test_a_long_conversation_keeps_its_newest_turns(self, project):
        document = build_document(project)
        history = [ChatMessage(role=ROLE_USER, text=f"old {i} " + "x" * 3000) for i in range(10)]
        history.append(ChatMessage(role=ROLE_ASSISTANT, text="the latest answer"))
        prompt = build_prompt(document, "q", focus=Focus(), scope=SCOPE_NEARBY, history=history)
        assert "the latest answer" in prompt
        assert "old 9 " in prompt
        assert "old 0 " not in prompt


# -- reading the answer ---------------------------------------------------------------


class TestAnswerLinks:
    def test_a_cited_time_becomes_a_link_that_seeks(self):
        out = prepare_answer("근거는 [00:12:34]입니다.", duration=3600)
        assert out == "근거는 [00:12:34](seek:754)입니다."

    def test_a_time_without_brackets_is_linked_too(self):
        assert prepare_answer("at 0:01:05 he says", duration=600) == (
            "at [00:01:05](seek:65) he says"
        )

    def test_a_time_past_the_end_of_the_lecture_is_left_alone(self):
        assert prepare_answer("[02:00:00]", duration=600) == "[02:00:00]"

    def test_a_slide_the_lecture_has_becomes_a_link(self):
        out = prepare_answer("Slide 8에서 말했듯이, 슬라이드 99는 없음", slides={8})
        assert out == "[Slide 8](slide:8)에서 말했듯이, 슬라이드 99는 없음"

    def test_code_is_left_exactly_as_written(self):
        answer = "`00:00:10` and\n```\nSlide 1 at 00:00:20\n```\nthen [00:00:30]"
        out = prepare_answer(answer, duration=600, slides={1})
        assert "`00:00:10`" in out
        assert "Slide 1 at 00:00:20" in out
        assert out.endswith("[00:00:30](seek:30)")

    def test_bold_next_to_korean_is_kept_bold(self):
        assert prepare_answer("**학습률(learning rate)**은 중요") == (
            "<b>학습률(learning rate)</b>은 중요"
        )


class TestFailures:
    def test_a_usage_limit_does_not_talk_about_translating(self):
        message = failure_message(EngineRateLimited("Slides translated so far have been saved."))
        assert "usage limit" in message and "translated" not in message

    def test_signing_in_says_what_to_run(self):
        assert "codex login" in failure_message(EngineAuthError("x"))

    def test_an_engine_error_is_shown_as_is(self):
        assert failure_message(TranslationError("Codex failed (exit 1)")) == "Codex failed (exit 1)"


def test_questions_can_use_a_model_of_their_own(tmp_path):
    from lecture_saver.config import Settings

    settings = Settings(tmp_path / "settings.json")
    settings.set("translate_model", "cheap")
    assert ask_engine_from_settings(settings).model == "cheap"
    settings.set("ask_model", "smart")
    assert ask_engine_from_settings(settings).model == "smart"


# -- the panel --------------------------------------------------------------------------


@pytest.fixture
def panel(qapp, project, tmp_path):
    from lecture_saver.config import Settings
    from lecture_saver.ui.ask_panel import AskPanel

    settings = Settings(tmp_path / "settings.json")
    settings.translation_enabled = True  # agreed already: no dialog in a headless test
    widget = AskPanel()
    widget.set_settings(settings)
    widget.bind(project)
    widget.set_focus_provider(lambda: Focus(position=33.0, slide=4))
    widget.engine = FakeEngine()
    widget.engine_factory = lambda: widget.engine
    yield widget
    widget.shutdown()


class TestAskPanel:
    def test_a_question_is_answered_and_kept_with_the_lecture(self, panel, project):
        panel._input.setPlainText("러닝레이트가 뭐예요?")
        assert panel.ask()
        assert pump_until(lambda: not panel.is_busy)

        assert [m.role for m in project.chat.messages] == [ROLE_USER, ROLE_ASSISTANT]
        assert project.chat.messages[0].text == "러닝레이트가 뭐예요?"
        assert project.chat.messages[0].slide == 4
        assert project.chat.messages[1].text == "답입니다 [00:00:21]"
        saved = json.loads(project.chat_file.read_text(encoding="utf-8"))
        assert len(saved["messages"]) == 2
        assert panel._input.toPlainText() == ""

    def test_a_pointed_line_goes_with_the_question_then_leaves_the_box(self, panel):
        panel.add_quote(Quote("segment_002", 11.0, "line of slide 2"))
        assert panel.ask()  # nothing typed: explain it
        assert pump_until(lambda: not panel.is_busy)

        prompt = panel.engine.prompts[0]
        assert "## 학생이 짚은 전사 부분\n[00:00:11] line of slide 2" in prompt
        assert prompt.rstrip().endswith(DEFAULT_QUESTION)
        assert panel.quotes == []

    def test_the_same_line_is_not_quoted_twice(self, panel):
        quote = Quote("segment_002", 11.0, "line of slide 2")
        panel.add_quote(quote)
        panel.add_quote(quote)
        assert panel.quotes == [quote]

    def test_nothing_to_ask_sends_nothing(self, panel):
        assert not panel.ask()
        assert panel.engine.prompts == []

    def test_a_failed_question_is_handed_back(self, panel, project):
        panel.engine.error = EngineRateLimited("limit")
        quote = Quote("segment_002", 11.0, "line of slide 2")
        panel.add_quote(quote)
        panel._input.setPlainText("왜요?")
        panel.ask()
        assert pump_until(lambda: not panel.is_busy)

        assert project.chat.is_empty, "a question with no answer must not stay in the chat"
        assert panel._input.toPlainText() == "왜요?"
        assert panel.quotes == [quote]
        assert "usage limit" in panel._error

    def test_stopping_hands_the_question_back(self, panel, project):
        panel.engine.hang = True
        panel._input.setPlainText("긴 질문")
        panel.ask()
        assert panel.is_busy
        panel.stop()
        assert pump_until(lambda: not panel.is_busy)
        assert project.chat.is_empty
        assert panel._input.toPlainText() == "긴 질문"

    def test_nothing_is_sent_without_the_agreement(self, qapp, project):
        from lecture_saver.ui.ask_panel import AskPanel

        engine = FakeEngine()
        widget = AskPanel()  # no settings: there is nowhere the agreement could be
        widget.bind(project)
        widget.engine_factory = lambda: engine
        widget._input.setPlainText("질문")
        assert not widget.ask()
        assert engine.prompts == []

    def test_switching_lectures_stops_the_question_in_flight(self, panel, project, tmp_path):
        panel.engine.hang = True
        panel._input.setPlainText("질문")
        panel.ask()
        other = _lecture(tmp_path / "other")
        panel.bind(other)
        assert not panel.is_busy
        assert project.chat.is_empty and other.chat.is_empty

    def test_new_chat_forgets_the_conversation(self, panel, project):
        project.chat.add(ChatMessage(role=ROLE_USER, text="q"))
        project.save_chat()
        panel.clear_conversation(confirm=False)
        assert project.chat.is_empty
        assert Project.open(project.root).chat.is_empty

    def test_citations_in_an_answer_lead_back_into_the_lecture(self, panel, project):
        from PySide6.QtCore import QUrl

        project.chat.add(ChatMessage(role=ROLE_ASSISTANT, text="see [00:00:21] and Slide 3"))
        panel.refresh()
        html = panel._view.toHtml()
        assert 'href="seek:21"' in html and 'href="slide:3"' in html

        seeks: list[float] = []
        slides: list[int] = []
        panel.seekRequested.connect(seeks.append)
        panel.slideRequested.connect(slides.append)
        panel._on_link(QUrl("seek:21"))
        panel._on_link(QUrl("slide:3"))
        assert seeks == [21.0] and slides == [3]

    def test_the_scope_is_remembered(self, panel):
        panel._scope_box.setCurrentIndex(panel._scope_box.findData(SCOPE_LECTURE))
        assert panel._settings.get("ask_scope") == SCOPE_LECTURE

    def test_the_empty_panel_says_what_is_and_is_not_sent(self, panel):
        text = panel._view.toPlainText()
        assert "transcript text" in text
        assert "never sent" in text and "slide images" in text


def test_the_agreement_covers_questions_too():
    from lecture_saver.ui.ai_consent import consent_text

    text = consent_text("Codex CLI")
    assert "questions" in text and "study material" in text
