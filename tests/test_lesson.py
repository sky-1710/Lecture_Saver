"""The preview as a lecture: its steps, its voice and its tab (§11, §12, §32).

Nothing here speaks or sends: a stand-in takes the place of the speech service.
What is tested is what the application is responsible for - where the steps
fall, which page each shows, what a voice is given to say, that it goes on by
itself and stops short of the answers, and that the tab is there only when
there is a preview to teach from.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QLocale, QObject, QUrl, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtTextToSpeech import QTextToSpeech

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.reference import ReferenceText
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.core.study import StudyGuide
from lecture_saver.export.flowchart import parse_flowchart
from lecture_saver.export.pdf import find_korean_font
from lecture_saver.study.lesson import cited_pages, cited_times, lesson_steps, spoken
from lecture_saver.ui.flowchart_picture import chart_picture
from lecture_saver.ui.lesson_panel import SPEEDS, LessonPanel
from lecture_saver.ui.narrator import NO_VOICE, Narrator

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)

LESSON = """## PART 1. 큰 그림

### 이번 강의의 질문

**[교수님 설명]** 복권 스케줄링(Lottery Scheduling)을 다룹니다 [00:01:20]. [교안 p.2–3]

### 왜 필요한가

앞 단계의 쪽을 그대로 보여 줍니다.

## PART 2. 구조도

```mermaid
flowchart TD
    A["CPU는 하나"] --> B["누구에게 줄까?"]
```

| 방식 | 뜻 |
|---|---|
| 복권 | p.5의 추첨 |

## 최종 정리

### 이해 확인 질문

1. 왜 필요할까요?

### 확인 문제 정답

1. 공정하게 나누기 위해서입니다.
"""


class TestSteps:
    def test_a_step_starts_at_each_big_and_middle_heading(self):
        titles = [step.title for step in lesson_steps(LESSON)]
        assert titles == [
            "이번 강의의 질문", "왜 필요한가", "PART 2. 구조도",
            "이해 확인 질문", "확인 문제 정답",
        ]

    def test_a_small_heading_stays_inside_its_step(self):
        steps = lesson_steps("### 위\n\n내용\n\n#### A. 배경\n\n더 많은 내용")
        assert len(steps) == 1 and "더 많은 내용" in steps[0].paragraphs

    def test_a_step_knows_the_big_heading_it_is_under(self):
        steps = lesson_steps(LESSON)
        assert steps[0].part == "PART 1. 큰 그림" and steps[1].part == "PART 1. 큰 그림"
        assert steps[2].part == "PART 2. 구조도"

    def test_a_heading_with_nothing_under_it_is_said_with_the_next(self):
        first = lesson_steps(LESSON)[0]
        assert first.paragraphs[:2] == ("PART 1. 큰 그림.", "이번 강의의 질문.")

    def test_a_step_shows_the_pages_it_cites(self):
        assert lesson_steps(LESSON)[0].pages == (2, 3)

    def test_a_step_that_cites_none_keeps_the_page_before(self):
        assert lesson_steps(LESSON)[1].pages == (2, 3)

    def test_pages_are_found_in_tables_too(self):
        assert lesson_steps(LESSON)[2].pages == (5,)

    def test_it_knows_the_times_it_cites(self):
        assert lesson_steps(LESSON)[0].times == (80.0,)

    def test_the_answers_are_marked(self):
        assert [step.is_answers for step in lesson_steps(LESSON)] == [
            False, False, False, False, True,
        ]

    def test_text_before_any_heading_is_a_step(self):
        assert len(lesson_steps("첫 문단\n\n## 제목\n\n둘째 문단")) == 2

    def test_an_empty_lesson_has_no_steps(self):
        assert lesson_steps("") == []

    @pytest.mark.parametrize(
        ("text", "pages"),
        [
            ("p.3", (3,)),
            ("[교안 p.2–4]", (2, 3, 4)),
            ("p.2-p.3", (2, 3)),
            ("p.1, p.5와 p.1", (1, 5)),
            ("p. 7", (7,)),
            ("p.1-200", (1,)),
            ("step.2", ()),
            ("쪽 없음", ()),
        ],
    )
    def test_reading_pages(self, text, pages):
        assert cited_pages(text) == pages

    @pytest.mark.parametrize(
        ("text", "times"),
        [("[00:12:34]", (754.0,)), ("[12:34]", (754.0,)), ("[01:00:00] [01:00:00]", (3600.0,))],
    )
    def test_reading_times(self, text, times):
        assert cited_times(text) == times


class TestWhatIsSaid:
    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("**중요** 합니다", "중요 합니다"),
            ("`stride` 값", "stride 값"),
            ("[교수님 설명] 말했습니다", "교수님 설명: 말했습니다"),
            ("**[AI 보충 설명]** 덧붙입니다", "AI 보충 설명: 덧붙입니다"),
            ("여기서 [00:12:34] 말합니다", "여기서 말합니다"),
            ("교안에 있습니다 [교안 p.3]", "교안에 있습니다"),
            ("p.3을 보세요", "3쪽을 보세요"),
            ("p.3–5를 보세요", "3쪽에서 5쪽을 보세요"),
            ("p.5는 중요합니다", "5쪽은 중요합니다"),
            ("p.2로 갑니다", "2쪽으로 갑니다"),
            ("p.2가운데", "2쪽가운데"),
            ("A → B", "A , B"),
            ("w_1 값", "w 1 값"),
            ("하나<br>둘", "하나 둘"),
            ("2 * x 곱", "2 * x 곱"),
        ],
    )
    def test_written_for_the_eye_said_for_the_ear(self, written, said):
        assert spoken(written) == said

    @pytest.mark.parametrize(
        ("written", "said"),
        [
            ("복권 스케줄링(Lottery Scheduling)을 다룹니다", "복권 스케줄링을 다룹니다"),
            ("**비례 할당 스케줄링(Proportional Share Scheduling)** 입니다",
             "비례 할당 스케줄링 입니다"),
            ("가상 실행 시간 (vruntime)입니다", "가상 실행 시간입니다"),
            ("다단계 큐(MLFQ, Multi-Level Feedback Queue)", "다단계 큐"),
        ],
    )
    def test_the_english_beside_a_korean_term_is_not_read(self, written, said):
        assert spoken(written) == said

    @pytest.mark.parametrize(
        "written",
        ["vruntime(virtual runtime) 값", "괄호(한국어 설명)입니다", "함수 f(x)는"],
    )
    def test_other_brackets_stay(self, written):
        assert spoken(written) == written

    def test_a_chart_is_pointed_at_and_not_read(self):
        step = lesson_steps(LESSON)[2]
        assert "구조도는 화면을 봐 주세요." in step.paragraphs
        assert not any("flowchart" in line for line in step.paragraphs)

    def test_a_table_is_said_a_row_at_a_time(self):
        assert "복권, 5쪽의 추첨" in lesson_steps(LESSON)[2].paragraphs

    def test_code_is_pointed_at_and_not_read(self):
        step = lesson_steps("## 제목\n\n```c\nint x;\n```")[0]
        assert step.paragraphs[-1] == "코드는 화면을 봐 주세요."


class FakeSpeech(QObject):
    """What the application asks of the speech service, with no sound."""

    stateChanged = Signal(object)

    def __init__(self, *, locales=None):
        super().__init__()
        self.said: list[str] = []
        self.stopped = self.paused = self.resumed = 0
        self.rate = 0.0
        self.locale = None
        korean = QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea)
        self.locales = [korean] if locales is None else locales

    def availableLocales(self):  # noqa: N802 - Qt naming
        return self.locales

    def setLocale(self, locale):  # noqa: N802 - Qt naming
        self.locale = locale

    def setRate(self, rate):  # noqa: N802 - Qt naming
        self.rate = rate

    def say(self, text):
        self.said.append(text)

    def stop(self):
        self.stopped += 1
        self.stateChanged.emit(QTextToSpeech.State.Ready)

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1

    def errorString(self):  # noqa: N802 - Qt naming
        return "broken"

    def finish(self):
        """The paragraph being said comes to its end."""
        self.stateChanged.emit(QTextToSpeech.State.Ready)


class TestNarrator:
    def _narrator(self, qapp, **kwargs):
        speech = FakeSpeech(**kwargs)
        return Narrator(speech=speech), speech

    def test_it_says_the_paragraphs_one_after_another(self, qapp):
        narrator, speech = self._narrator(qapp)
        done = []
        narrator.finished.connect(lambda: done.append(True))
        narrator.read(["하나", "둘"])
        assert speech.said == ["하나"] and narrator.is_reading
        speech.finish()
        assert speech.said == ["하나", "둘"] and not done
        speech.finish()
        assert done and not narrator.is_reading

    def test_it_speaks_korean_at_the_speed_chosen(self, qapp):
        narrator, speech = self._narrator(qapp)
        narrator.set_rate(0.3)
        narrator.read(["하나"])
        assert speech.locale.language() == QLocale.Language.Korean
        assert speech.rate == 0.3

    def test_stopping_does_not_read_as_having_finished(self, qapp):
        narrator, speech = self._narrator(qapp)
        done = []
        narrator.finished.connect(lambda: done.append(True))
        narrator.read(["하나", "둘"])
        narrator.stop()
        assert speech.said == ["하나"] and not done and not narrator.is_reading
        assert speech.stopped == 1

    def test_a_pause_waits_and_goes_on(self, qapp):
        narrator, speech = self._narrator(qapp)
        narrator.read(["하나", "둘"])
        narrator.toggle_pause()
        assert narrator.is_paused and speech.paused == 1
        speech.finish()  # a pause must not count as the paragraph's end
        assert speech.said == ["하나"]
        narrator.toggle_pause()
        assert not narrator.is_paused and speech.resumed == 1

    def test_reading_again_replaces_what_was_being_read(self, qapp):
        narrator, speech = self._narrator(qapp)
        narrator.read(["하나", "둘"])
        narrator.read(["셋"])
        assert speech.said == ["하나", "셋"]

    def test_a_failure_stops_it(self, qapp):
        narrator, speech = self._narrator(qapp)
        narrator.read(["하나", "둘"])
        speech.stateChanged.emit(QTextToSpeech.State.Error)
        assert not narrator.is_reading

    def test_with_a_korean_voice_there_is_nothing_to_say(self, qapp):
        narrator, _ = self._narrator(qapp)
        assert narrator.unavailable_reason() == ""

    def test_without_one_it_says_so(self, qapp):
        narrator, _ = self._narrator(qapp, locales=[QLocale(QLocale.Language.English)])
        assert narrator.unavailable_reason() == NO_VOICE


def _picture(path: Path) -> Path:
    image = QImage(64, 36, QImage.Format.Format_RGB32)
    image.fill(QColor("white"))
    image.save(str(path))
    return path


class TestPanel:
    @pytest.fixture
    def panel(self, qapp, tmp_path):
        self.speech = FakeSpeech()
        panel = LessonPanel(narrator=Narrator(speech=self.speech))
        pictures = [_picture(tmp_path / f"p{n:03d}.jpg") for n in range(1, 7)]
        panel.set_lesson(LESSON, pictures)
        panel.resize(500, 700)
        return panel

    def test_it_opens_on_the_first_step(self, panel):
        assert panel.step_index == 0
        assert panel._where.text() == "1 / 5  ·  PART 1. 큰 그림"
        assert "복권 스케줄링" in panel._text.toPlainText()

    def test_its_controls_are_in_korean(self, panel):
        assert panel._read_button.text() == "읽어 주기"
        assert panel._back_button.text() == "이전" and panel._next_button.text() == "다음"

    def test_next_and_back(self, panel):
        panel.forward()
        assert panel.step_index == 1
        assert "앞 단계의 쪽" in panel._text.toPlainText()
        panel.back()
        assert panel.step_index == 0
        assert not panel._back_button.isEnabled()

    def test_the_last_step_has_no_next(self, panel):
        panel.go_to(4)
        assert not panel._next_button.isEnabled()

    def test_it_shows_the_page_a_step_cites(self, panel):
        assert panel._page_label.text() == "p.2"
        panel._flip_page(1)
        assert panel._page_label.text() == "p.3"
        assert not panel._later_page.isEnabled()

    def test_a_page_with_no_picture_falls_back_to_the_first(self, qapp, tmp_path):
        panel = LessonPanel(narrator=Narrator(speech=FakeSpeech()))
        panel.set_lesson(LESSON, [_picture(tmp_path / "p001.jpg")])
        assert panel._page_label.text() == "p.1"

    def test_with_no_pdf_there_is_only_the_text(self, qapp):
        panel = LessonPanel(narrator=Narrator(speech=FakeSpeech()))
        panel.set_lesson(LESSON, [])
        assert panel._deck.isHidden()
        assert "복권 스케줄링" in panel._text.toPlainText()

    def test_reading_quiets_the_video_first(self, panel):
        started = []
        panel.readingStarted.connect(lambda: started.append(True))
        panel.toggle_reading()
        assert started
        assert self.speech.said[0] == "PART 1. 큰 그림."
        assert panel._read_button.text() == "일시정지"

    def test_it_pauses_and_goes_on(self, panel):
        panel.toggle_reading()
        panel.toggle_reading()
        assert panel._read_button.text() == "이어서"
        panel.toggle_reading()
        assert panel._read_button.text() == "일시정지"

    def test_it_goes_on_to_the_next_step_by_itself(self, panel):
        panel.go_to(0)
        panel.toggle_reading()
        for _ in panel.steps[0].paragraphs:
            self.speech.finish()
        assert panel.step_index == 1
        assert self.speech.said[-1] == panel.steps[1].paragraphs[0]

    def test_it_stops_short_of_the_answers(self, panel):
        panel.go_to(3)
        panel.toggle_reading()
        for _ in panel.steps[3].paragraphs:
            self.speech.finish()
        assert panel.step_index == 3 and not panel.is_reading
        assert panel._read_button.text() == "읽어 주기"

    def test_stepping_while_reading_reads_the_new_step(self, panel):
        panel.toggle_reading()
        panel.forward()
        assert panel.is_reading
        assert self.speech.said[-1] == panel.steps[1].paragraphs[0]

    def test_stepping_while_not_reading_stays_quiet(self, panel):
        panel.forward()
        assert not panel.is_reading and self.speech.said == []

    def test_stop(self, panel):
        panel.toggle_reading()
        panel.stop()
        assert not panel.is_reading and not panel._stop_button.isEnabled()

    def test_no_voice_is_said_and_nothing_starts(self, qapp):
        speech = FakeSpeech(locales=[])
        panel = LessonPanel(narrator=Narrator(speech=speech))
        panel.set_lesson(LESSON, [])
        panel.toggle_reading()
        assert not panel.is_reading and not speech.said
        assert panel._voice_note.text() == NO_VOICE and not panel._voice_note.isHidden()

    def test_the_speed_is_remembered(self, panel, tmp_path):
        from lecture_saver.config import Settings

        settings = Settings(tmp_path / "settings.json")
        panel.set_settings(settings)
        assert panel._speed.currentText() == "보통"
        panel._speed.setCurrentIndex(2)
        assert settings.lesson_speed == SPEEDS[2][0] and self.speech.rate == SPEEDS[2][1]
        other = LessonPanel(narrator=Narrator(speech=FakeSpeech()))
        other.set_settings(settings)
        assert other._speed.currentIndex() == 2

    def test_a_cited_time_goes_to_the_video(self, panel):
        html = panel._text.toHtml()
        assert 'href="time:80"' in html
        moments = []
        panel.seekRequested.connect(moments.append)
        panel._on_link(QUrl("time:80"))
        assert moments == [80.0]

    def test_asking_gives_the_words_and_the_moment(self, panel):
        asked = []
        panel.askRequested.connect(lambda words, seconds: asked.append((words, seconds)))
        panel._ask()
        assert "복권 스케줄링" in asked[0][0] and asked[0][1] == 80.0

    def test_the_same_preview_keeps_the_place(self, panel, tmp_path):
        panel.go_to(2)
        panel.set_lesson(LESSON, [])
        assert panel.step_index == 2

    def test_a_new_preview_starts_again_and_stops_the_voice(self, panel):
        panel.go_to(2)
        panel.toggle_reading()
        panel.set_lesson("## 새 강의\n\n새로운 내용", [])
        assert panel.step_index == 0 and not panel.is_reading

    def test_clearing_stops_the_voice(self, panel):
        panel.toggle_reading()
        panel.clear()
        assert not panel.is_reading and panel.steps == []


@needs_korean_font
class TestConceptMaps:
    def test_a_concept_map_is_a_picture(self, qapp):
        chart = parse_flowchart('flowchart TD\nA["CPU는 하나"] --> B["누구에게 줄까?"]')
        picture = chart_picture(chart, 460, 560)
        assert picture is not None and not picture.isNull()
        assert picture.width() > 50 and picture.height() > 50

    def test_the_picture_has_the_chart_on_white(self, qapp):
        chart = parse_flowchart('flowchart TD\nA["하나"] --> B["둘"]')
        picture = chart_picture(chart, 460, 560)
        assert picture.pixelColor(0, 0) == QColor("white")
        colours = {
            picture.pixelColor(x, picture.height() // 2).name()
            for x in range(picture.width())
        }
        assert len(colours) > 1

    def test_it_is_drawn_at_the_size_it_is_shown(self, qapp):
        chart = parse_flowchart("flowchart TD\n" + "\n".join(
            f'R --> N{n}["상자 {n}"]' for n in range(6)
        ))
        picture = chart_picture(chart, 400, 560)
        assert picture.width() <= 410

    def test_a_sharper_screen_gets_more_pixels_in_the_same_size(self, qapp):
        chart = parse_flowchart('flowchart TD\nA["하나"] --> B["둘"]')
        plain = chart_picture(chart, 400, 560)
        sharp = chart_picture(chart, 400, 560, pixel_ratio=2.0)
        assert sharp.width() == pytest.approx(plain.width() * 2, abs=2)
        assert sharp.devicePixelRatio() == 2.0

    def test_a_chart_too_big_to_read_is_said_to_be(self, qapp):
        boxes = "\n".join(f'R --> N{n}["상자 번호 {n} 입니다 길게 적은 설명"]' for n in range(60))
        assert chart_picture(parse_flowchart(f"flowchart TD\n{boxes}"), 460, 560) is None

    def test_a_step_shows_its_chart_as_a_picture(self, qapp):
        panel = LessonPanel(narrator=Narrator(speech=FakeSpeech()))
        panel.set_lesson(LESSON, [])
        panel.go_to(2)
        assert 'src="chart:0"' in panel._text.toHtml()
        assert "flowchart" not in panel._text.toPlainText()

    def test_a_chart_that_cannot_be_drawn_is_printed_as_written(self, qapp):
        panel = LessonPanel(narrator=Narrator(speech=FakeSpeech()))
        panel.set_lesson("## 제목\n\n```mermaid\nsequenceDiagram\nA->>B: 안녕\n```", [])
        assert "sequenceDiagram" in panel._text.toPlainText()


class TestTheTab:
    @pytest.fixture
    def project(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"x")
        info = VideoInfo(path=str(video), duration=40.0, has_audio=True)
        project = Project.create(tmp_path / "projects", "OS05", info)
        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=0.0, end=5.0, original_text="Hello.")],
            language="en",
        )
        project.save()
        project.set_slides(SlideSet(
            slides=[Slide(1, "slides/001.jpg")], appearances=[SlideAppearance(1, 1, 0.0, 40.0)],
        ))
        project.save_slides()
        return project

    @pytest.fixture
    def window(self, qapp, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        window._lecture_view._lesson_panel._narrator = Narrator(speech=FakeSpeech())
        yield window
        window.close()

    def _save_preview(self, project):
        project.study.set_guide(StudyGuide.from_answer(LESSON + "내용 " * 400), source="x")
        project.save_study()

    def _tab(self, view):
        from lecture_saver.ui.lecture_view import TAB_LESSON

        return view._tabs.isTabVisible(TAB_LESSON)

    def test_with_no_preview_there_is_no_tab(self, window, project):
        window._open_project_at(project.root)
        view = window._lecture_view
        assert not self._tab(view) and not view._lesson_action.isEnabled()

    def test_a_saved_preview_brings_the_tab(self, window, project):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        assert self._tab(view) and view._lesson_action.isEnabled()
        assert view._lesson_panel.steps

    def test_a_preview_made_now_brings_it_at_once(self, window, project):
        window._open_project_at(project.root)
        view = window._lecture_view
        self._save_preview(window.project)
        view.refresh_lesson()
        assert self._tab(view)

    def test_the_menu_opens_it_and_hides_the_transcript(self, window, project):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        view._lesson_action.trigger()
        assert view.showing_lesson and not view._lesson_panel.isHidden()
        assert view._transcript.isHidden()

    def test_leaving_it_brings_the_transcript_back(self, window, project):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        view.open_lesson()
        view._tabs.setCurrentIndex(0)
        assert view._lesson_panel.isHidden() and not view._transcript.isHidden()

    def test_the_lesson_shows_the_pages_of_the_attached_pdf(self, window, project, tmp_path):
        folder = tmp_path / "drawn"
        folder.mkdir()
        for page in range(1, 4):
            _picture(folder / f"p{page:03d}.jpg")
        project.set_reference(ReferenceText("OS05.pdf", ("x", "y", "z")), pictures=folder)
        self._save_preview(project)
        window._open_project_at(project.root)
        assert window._lecture_view._lesson_panel._page_label.text() == "p.2"

    def test_closing_the_lecture_takes_it_away(self, window, project):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        view.close_project()
        assert not self._tab(view) and view._lesson_panel.steps == []

    def test_asking_puts_the_part_above_the_question_box(self, window, project):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        view._lesson_panel._ask()
        assert view._ask_panel.quotes[0].start == 80.0
        assert "복권 스케줄링" in view._ask_panel.quotes[0].text

    def test_reading_pauses_the_video(self, window, project, monkeypatch):
        self._save_preview(project)
        window._open_project_at(project.root)
        view = window._lecture_view
        paused = []
        view._lesson_panel.readingStarted.disconnect()
        view._lesson_panel.readingStarted.connect(lambda: paused.append(True))
        view._lesson_panel.toggle_reading()
        assert paused
