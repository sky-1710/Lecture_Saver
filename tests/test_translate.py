"""In-app AI translation (§25, §28, §33).

The engine is never really run here: every test substitutes a fake one. What
is worth testing is everything around it - how a lecture is cut into batches,
that the prompt asks for headings the importer can read back, that a failure
part-way keeps what came before, and that a second run resumes rather than
paying to redo the lecture.

One test does locate the real Codex program, and is skipped when it is not
installed. It only reads a version string; it never starts a translation.
"""

from __future__ import annotations

import threading

import pytest

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import Project
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.core.translations import parse_translation_text
from lecture_saver.export.document import build_document
from lecture_saver.translate.batching import (
    batch_markdown,
    plan_batches,
    translatable_sections,
)
from lecture_saver.translate.engine import (
    EngineAvailability,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)
from lecture_saver.translate.runner import batch_prompt, run_translation

# -- fixtures ---------------------------------------------------------------------


def _slide_project(tmp_path, slide_count=6, words=40):
    """A lecture with ``slide_count`` slides, each with speech of its own."""
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    info = VideoInfo(path=str(video), duration=float(slide_count * 10), has_audio=True)
    project = Project.create(tmp_path / "projects", "Lecture", info)

    segments = []
    for index in range(slide_count):
        start = index * 10.0
        segments.append(
            TranscriptSegment(
                id=f"segment_{index + 1:03d}",
                start=start,
                end=start + 8.0,
                original_text=" ".join([f"word{index}"] * words) + ".",
            )
        )
    project.transcript.replace_all(segments, language="en")
    project.save()
    project.set_slides(SlideSet(
        slides=[Slide(i + 1, f"slides/{i + 1:03d}.jpg") for i in range(slide_count)],
        appearances=[
            SlideAppearance(i + 1, i + 1, i * 10.0, (i + 1) * 10.0)
            for i in range(slide_count)
        ],
    ))
    project.save_slides()
    return project


@pytest.fixture
def project(tmp_path):
    return _slide_project(tmp_path)


@pytest.fixture
def document(project):
    return build_document(project)


class FakeEngine(TranslationEngine):
    """Answers every batch, recording the prompts it was given."""

    key = "fake"
    label = "Fake engine"

    def __init__(self, *, fail_on=(), answer_for=None, hang=False):
        self.prompts: list[str] = []
        self.fail_on = set(fail_on)  # 1-based call numbers that raise
        self.answer_for = answer_for
        self.hang = hang

    def availability(self):
        return EngineAvailability(ready=True, detail="fake")

    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        self.prompts.append(prompt)
        if self.hang:
            while not cancel.is_set():
                cancel.wait(0.01)
            raise TranslationCancelled("Translation stopped.")
        if len(self.prompts) in self.fail_on:
            raise TranslationError("the engine broke")
        if self.answer_for is not None:
            return self.answer_for(prompt)
        return _answer_for(prompt)


def _answer_for(prompt: str, skip: set[int] | None = None) -> str:
    """A well-formed answer for whatever slides the prompt asked about."""
    omit = skip or set()
    numbers = sorted(parse_translation_text(prompt.split("아래부터 강의 전사입니다.")[-1]).slides)
    return "\n\n".join(
        f"## Slide {n}\n\nword{n - 1} 입니다." for n in numbers if n not in omit
    )


# -- batching ----------------------------------------------------------------------


class TestBatching:
    def test_slides_without_speech_are_not_sent(self, project):
        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=0.0, end=5.0, original_text="Only here.")],
            language="en",
        )
        sections = translatable_sections(build_document(project))
        assert [s.number for s in sections] == [1]

    def test_slides_already_translated_are_skipped(self, document):
        sections = translatable_sections(document, skip={2, 4})
        assert [s.number for s in sections] == [1, 3, 5, 6]

    def test_a_batch_never_splits_a_slide(self, document):
        batches = plan_batches(translatable_sections(document), max_chars=1, max_slides=10)
        # Every slide is over the limit on its own, so each gets its own batch
        # rather than being cut in half.
        assert [b.numbers for b in batches] == [(1,), (2,), (3,), (4,), (5,), (6,)]

    def test_batches_are_capped_by_slide_count(self, document):
        batches = plan_batches(translatable_sections(document), max_chars=10**9, max_slides=2)
        assert [b.numbers for b in batches] == [(1, 2), (3, 4), (5, 6)]

    def test_every_slide_appears_exactly_once(self, document):
        batches = plan_batches(translatable_sections(document), max_chars=600, max_slides=3)
        seen = [n for batch in batches for n in batch.numbers]
        assert sorted(seen) == [1, 2, 3, 4, 5, 6]
        assert len(seen) == len(set(seen))

    def test_the_prompt_carries_no_image_path_or_file_name(self, document):
        """Only words are sent: no slide picture, no path, no video name."""
        batch = plan_batches(translatable_sections(document))[0]
        text = batch_markdown(batch)
        assert "slides/" not in text
        assert ".jpg" not in text
        assert "lecture.mp4" not in text
        assert "![" not in text


# -- the prompt ---------------------------------------------------------------------


class TestPrompt:
    def test_headings_round_trip_through_the_importer(self, document):
        """The answer's headings are read by the same parser, so they must match."""
        batch = plan_batches(translatable_sections(document), max_slides=3)[0]
        parsed = parse_translation_text(batch_markdown(batch))
        assert tuple(sorted(parsed.slides)) == batch.numbers

    def test_the_rules_are_repeated_in_every_batch(self, document):
        """Each batch is a fresh run, so instructions given once would be lost."""
        for batch in plan_batches(translatable_sections(document), max_slides=2):
            prompt = batch_prompt(batch_markdown(batch))
            assert "영어를 지우면 안 됩니다" in prompt
            assert "전공 용어는 영어로" in prompt

    def test_no_files_are_asked_for(self, document):
        """The hand-carried request wants a .md; this one wants only an answer."""
        prompt = batch_prompt(batch_markdown(plan_batches(translatable_sections(document))[0]))
        assert "파일을 만들지 말고" in prompt
        assert "Markdown(.md) 파일" not in prompt

    def test_the_continuation_rule_is_left_out(self, document):
        """Batches are sized to fit, so 'stop and wait for 계속' must not appear."""
        prompt = batch_prompt(batch_markdown(plan_batches(translatable_sections(document))[0]))
        assert "계속" not in prompt

    def test_the_hand_carried_request_still_asks_for_files(self):
        """Splitting the prompt into shared parts must not have changed it."""
        from lecture_saver.export.ai_request import TRANSLATION_REQUEST

        assert "Markdown(.md) 파일" in TRANSLATION_REQUEST
        assert "계속" in TRANSLATION_REQUEST


# -- running -----------------------------------------------------------------------


class TestRunner:
    def test_every_slide_comes_back_translated(self, document):
        engine = FakeEngine()
        report = run_translation(document, engine, max_slides=2)
        assert sorted(report.translated) == [1, 2, 3, 4, 5, 6]
        assert report.missing == []
        assert report.errors == []
        assert report.total_batches == 3

    def test_answers_arrive_one_batch_at_a_time(self, document):
        """So the caller can save each before the next is asked for (§26)."""
        seen = []
        run_translation(
            document, FakeEngine(), max_slides=2,
            on_batch=lambda outcome: seen.append(outcome.translated),
        )
        assert seen == [(1, 2), (3, 4), (5, 6)]

    def test_a_failed_batch_does_not_lose_the_ones_before_it(self, document):
        saved = []
        report = run_translation(
            document, FakeEngine(fail_on={2}), max_slides=2,
            on_batch=lambda outcome: saved.append(outcome.translated),
        )
        assert saved == [(1, 2), (5, 6)]
        assert report.missing == [3, 4]
        assert "the engine broke" in report.errors[0]

    def test_slides_the_engine_left_out_are_reported(self, document):
        engine = FakeEngine(answer_for=lambda p: _answer_for(p, skip={3}))
        report = run_translation(document, engine, max_slides=3)
        assert report.missing == [3]
        assert 3 not in report.translated

    def test_a_usage_limit_stops_the_run_instead_of_burning_every_batch(self, document):
        from lecture_saver.translate.engine import EngineRateLimited

        class Limited(FakeEngine):
            def translate(self, prompt, *, cancel):
                self.prompts.append(prompt)
                raise EngineRateLimited("Your ChatGPT usage limit has been reached.")

        engine = Limited()
        report = run_translation(document, engine, max_slides=2)
        assert len(engine.prompts) == 1, "should not have tried the other batches"
        assert len(report.errors) == 1

    def test_a_second_run_only_sends_what_is_left(self, document):
        engine = FakeEngine()
        run_translation(document, engine, skip={1, 2, 3, 4}, max_slides=2)
        sent = [
            n for prompt in engine.prompts
            for n in parse_translation_text(prompt.split("강의 전사입니다.")[-1]).slides
        ]
        assert sorted(sent) == [5, 6]

    def test_nothing_to_do_is_not_an_error(self, document):
        engine = FakeEngine()
        report = run_translation(document, engine, skip={1, 2, 3, 4, 5, 6})
        assert engine.prompts == []
        assert report.translated == []
        assert report.errors == []

    def test_cancelling_stops_within_a_batch(self, document):
        cancel = threading.Event()
        engine = FakeEngine(hang=True)

        def stop_soon():
            cancel.set()

        timer = threading.Timer(0.05, stop_soon)
        timer.start()
        report = run_translation(document, engine, cancel=cancel, max_slides=2)
        timer.cancel()
        assert report.cancelled
        assert report.translated == []

    def test_progress_is_reported_for_every_batch(self, document):
        seen = []
        run_translation(
            document, FakeEngine(), max_slides=2,
            on_progress=lambda done, total: seen.append((done, total)),
        )
        assert seen == [(0, 3), (1, 3), (2, 3), (3, 3)]


# -- saving into the project ---------------------------------------------------------


class TestSaving:
    def test_a_batch_answer_attaches_to_the_right_slides(self, project):
        document = build_document(project)
        saved: list[int] = []

        def save(outcome):
            result = project.import_translation(outcome.answer, source="fake")
            saved.extend(t.appearance_id for t in result.matched)

        run_translation(document, FakeEngine(), max_slides=2, on_batch=save)
        assert sorted(saved) == [1, 2, 3, 4, 5, 6]
        for appearance in project.slides.appearances:
            assert project.translations.for_appearance(appearance)

    def test_what_was_saved_before_a_failure_survives_a_reload(self, project, tmp_path):
        document = build_document(project)
        run_translation(
            document, FakeEngine(fail_on={2}), max_slides=2,
            on_batch=lambda o: project.import_translation(o.answer, source="fake"),
        )
        reopened = Project.open(project.root)
        done = {
            a.appearance_id for a in reopened.slides.appearances
            if reopened.translations.for_appearance(a)
        }
        assert done == {1, 2, 5, 6}

    def test_the_engine_is_recorded_as_the_source(self, project):
        document = build_document(project)
        run_translation(
            document, FakeEngine(), max_slides=6,
            on_batch=lambda o: project.import_translation(o.answer, source="codex"),
        )
        assert project.translations.source == "codex"


# -- the Codex engine -----------------------------------------------------------------


class TestCodexEngine:
    def test_the_command_reads_the_prompt_from_stdin(self, tmp_path):
        """A batch is far longer than Windows allows in a command line."""
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(tmp_path / "codex.exe", tmp_path / "answer.md")
        assert command[-1] == "-"
        assert "exec" in command

    def test_the_command_keeps_codex_out_of_the_users_files(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(tmp_path / "codex.exe", tmp_path / "answer.md")
        assert "--sandbox" in command and "read-only" in command
        # No session log of the lecture is left behind on disk.
        assert "--ephemeral" in command

    def test_each_picture_has_its_own_flag_and_cannot_swallow_the_prompt(self, tmp_path):
        """--image takes as many values as follow it, "-" included."""
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(
            tmp_path / "codex.exe", tmp_path / "answer.md", images=["p001.jpg", "p002.jpg"]
        )
        first = command.index("--image")
        assert command[first:first + 4] == ["--image", "p001.jpg", "--image", "p002.jpg"]
        assert command[first + 4] == "-m"
        assert command[-1] == "-"

    def test_no_pictures_no_flag(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(tmp_path / "codex.exe", tmp_path / "answer.md")
        assert "--image" not in command

    def test_the_users_own_codex_config_is_not_applied(self, tmp_path):
        """Their default model and MCP servers would both break a translation."""
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(tmp_path / "codex.exe", tmp_path / "answer.md")
        assert "--ignore-user-config" in command

    def test_a_chosen_model_is_passed_through(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine(model="gpt-5.1")._command(
            tmp_path / "codex.exe", tmp_path / "answer.md"
        )
        assert command[command.index("-m") + 1] == "gpt-5.1"

    def test_an_affordable_model_is_chosen_when_none_was(self, tmp_path):
        """Codex's own default can be refused for a ChatGPT sign-in."""
        import json

        from lecture_saver.translate.codex_cli import CodexEngine

        (tmp_path / "models_cache.json").write_text(
            json.dumps({"models": [{"slug": "gpt-6-astra"}, {"slug": "gpt-5.6-luna"}]}),
            encoding="utf-8",
        )
        command = CodexEngine(home=tmp_path)._command(
            tmp_path / "codex.exe", tmp_path / "answer.md"
        )
        assert command[command.index("-m") + 1] == "gpt-5.6-luna"

    def test_a_model_is_passed_even_without_a_model_list(self, tmp_path):
        from lecture_saver.translate.codex_cli import AFFORDABLE_MODELS, CodexEngine

        command = CodexEngine(home=tmp_path / "missing")._command(
            tmp_path / "codex.exe", tmp_path / "answer.md"
        )
        assert command[command.index("-m") + 1] == AFFORDABLE_MODELS[0]

    def test_a_question_starts_a_session_that_is_kept(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine()._command(tmp_path / "codex.exe", tmp_path / "answer.md", keep=True)
        assert "--json" in command and "--ephemeral" not in command
        assert "read-only" in command and command[-1] == "-"

    def test_a_follow_up_resumes_the_session(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        command = CodexEngine(model="gpt-5.1")._command(
            tmp_path / "codex.exe", tmp_path / "answer.md", resume="0199-abcd-ef01"
        )
        assert command[1:3] == ["exec", "resume"]
        assert command[-2:] == ["0199-abcd-ef01", "-"]
        assert command[command.index("-m") + 1] == "gpt-5.1"
        assert "--ignore-user-config" in command and "--sandbox" not in command

    def test_the_session_id_is_read_from_the_json_events(self):
        from lecture_saver.translate.codex_cli import _thread_id

        assert _thread_id('noise\n{"type":"thread.started","thread_id":"0199-ab"}\n') == "0199-ab"
        assert _thread_id("no events") is None

    def test_forgetting_a_session_deletes_only_its_file(self, tmp_path, monkeypatch):
        from lecture_saver.translate.codex_cli import forget_codex_session

        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        day = tmp_path / "sessions" / "2026" / "10" / "27"
        day.mkdir(parents=True)
        mine = day / "rollout-2026-10-27T23-22-42-0199abcd-0000-1111-2222-333344445555.jsonl"
        other = day / "rollout-2026-10-27T23-22-42-0199ffff-0000-1111-2222-333344445555.jsonl"
        mine.write_text("{}", encoding="utf-8")
        other.write_text("{}", encoding="utf-8")

        assert forget_codex_session("0199abcd-0000-1111-2222-333344445555") == 1
        assert not mine.exists() and other.exists()
        assert forget_codex_session("../*") == 0

    def test_a_conversation_runs_in_one_session(self, tmp_path, monkeypatch):
        """Through a stand-in for Codex: the id the first turn reports is the
        session the next turn resumes."""
        from pathlib import Path

        from lecture_saver.translate import codex_cli
        from lecture_saver.translate.codex_cli import CodexEngine

        commands: list[list[str]] = []

        class FakeProcess:
            returncode = 0

            def __init__(self, cmd, **kwargs):
                commands.append(cmd)
                answer = Path(cmd[cmd.index("--output-last-message") + 1])
                answer.write_text(f"answer {len(commands)}", encoding="utf-8")

            def communicate(self, input=None, timeout=None):
                return '{"type":"thread.started","thread_id":"0199-abcd-ef01"}\n', ""

        monkeypatch.setattr(codex_cli.subprocess, "Popen", FakeProcess)
        (tmp_path / "codex.exe").write_bytes(b"")
        engine = CodexEngine(binary=tmp_path / "codex.exe", home=tmp_path)
        cancel = threading.Event()

        assert engine.converse("first", cancel=cancel) == ("answer 1", "0199-abcd-ef01")
        assert engine.converse("next", session="0199-abcd-ef01", cancel=cancel) == (
            "answer 2", "0199-abcd-ef01",
        )
        assert "resume" not in commands[0]
        assert commands[1][1:3] == ["exec", "resume"]

    def test_pictures_go_by_name_from_a_folder_of_their_own(self, tmp_path, monkeypatch):
        """Codex is told no path to the project, or to this user's home."""
        from pathlib import Path

        from lecture_saver.translate import codex_cli
        from lecture_saver.translate.codex_cli import CodexEngine

        seen: dict[str, object] = {}

        class FakeProcess:
            returncode = 0

            def __init__(self, cmd, **kwargs):
                seen["command"] = cmd
                seen["copied"] = (Path(kwargs["cwd"]) / "p001.jpg").read_bytes()
                Path(cmd[cmd.index("--output-last-message") + 1]).write_text(
                    "answer", encoding="utf-8"
                )

            def communicate(self, input=None, timeout=None):
                return "", ""

        monkeypatch.setattr(codex_cli.subprocess, "Popen", FakeProcess)
        (tmp_path / "codex.exe").write_bytes(b"")
        picture = tmp_path / "secret project" / "p001.jpg"
        picture.parent.mkdir()
        picture.write_bytes(b"page one")

        engine = CodexEngine(binary=tmp_path / "codex.exe", home=tmp_path)
        assert engine.ask("prompt", cancel=threading.Event(), images=[picture]) == "answer"
        assert seen["copied"] == b"page one"
        command = seen["command"]
        assert command[command.index("--image") + 1] == "p001.jpg"
        assert not any("secret project" in part for part in command)

    def test_an_engine_that_cannot_show_pictures_says_so(self):
        from pathlib import Path

        from lecture_saver.translate.engine import EngineAvailability, TranslationEngine

        class Plain(TranslationEngine):
            label = "Plain"

            def availability(self):
                return EngineAvailability(ready=True)

            def translate(self, prompt, *, cancel):
                return "answer"

        with pytest.raises(TranslationError, match="cannot be shown pictures"):
            Plain().ask("prompt", cancel=threading.Event(), images=[Path("p001.jpg")])

    def test_the_answer_is_read_from_the_file_not_the_chatter(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        answer = tmp_path / "answer.md"
        answer.write_text("## Slide 1\n번역", encoding="utf-8")
        got = CodexEngine()._read_answer(answer, stdout="thinking... tokens used: 123")
        assert got == "## Slide 1\n번역"

    def test_stdout_is_used_when_codex_wrote_no_file(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine

        got = CodexEngine()._read_answer(tmp_path / "missing.md", stdout="## Slide 1\n번역")
        assert got == "## Slide 1\n번역"

    @pytest.mark.parametrize(
        "stderr, expected",
        [
            ("Error: not logged in", "codex login"),
            ("429 rate limit exceeded", "usage limit"),
            ("dns error: could not resolve", "internet connection"),
            (
                "The 'gpt-5.6-sol' model requires a newer version of Codex.",
                "update codex",
            ),
            (
                "The 'gpt-5.4' model is not supported when using Codex with a "
                "ChatGPT account.",
                "chatgpt sign-in",
            ),
            ("panic: something else", "exit code"),
        ],
    )
    def test_failures_are_explained_rather_than_dumped(self, stderr, expected):
        from lecture_saver.translate.codex_cli import CodexEngine

        error = CodexEngine()._failure(1, "", stderr)
        assert expected in str(error).lower()

    def test_a_missing_codex_says_where_it_looked(self, tmp_path):
        from lecture_saver.translate.codex_cli import CodexEngine, EngineNotFoundError

        engine = CodexEngine(binary=tmp_path / "nowhere" / "codex.exe")
        with pytest.raises(EngineNotFoundError, match="could not be found"):
            engine.resolve()
        assert engine.availability().ready is False

    def test_the_real_codex_reports_its_version_if_installed(self):
        from lecture_saver.translate.codex_cli import CodexEngine, find_codex

        if find_codex() is None:
            pytest.skip("Codex is not installed on this machine")
        availability = CodexEngine().availability()
        assert availability.ready
        assert "codex" in availability.detail.lower()


# -- the dialog ------------------------------------------------------------------------


class TestTranslateDialog:
    """The screen is the last place the privacy promise can be broken quietly,
    so what it says and when it asks is tested, not just that it opens."""

    def _dialog(self, project, tmp_path, **settings_values):
        from lecture_saver.config import Settings
        from lecture_saver.ui.translate_dialog import TranslateDialog

        settings = Settings(tmp_path / "settings.json")
        for key, value in settings_values.items():
            setattr(settings, key, value)
        return TranslateDialog(project, settings), settings

    def test_it_counts_what_would_be_sent(self, qapp, project, tmp_path):
        dialog, _ = self._dialog(project, tmp_path)
        assert "6 of 6 slides will be translated" in dialog._status.text()

    def test_slides_already_translated_are_not_offered_again(self, qapp, project, tmp_path):
        project.import_translation("## Slide 1\n번역 하나\n\n## Slide 2\n번역 둘")
        dialog, _ = self._dialog(project, tmp_path)
        assert dialog._pending() == [3, 4, 5, 6]
        assert "2 already translated" in dialog._status.text()

    def test_ticking_the_box_offers_them_again(self, qapp, project, tmp_path):
        project.import_translation("## Slide 1\n번역 하나")
        dialog, _ = self._dialog(project, tmp_path)
        dialog._redo.setChecked(True)
        assert dialog._pending() == [1, 2, 3, 4, 5, 6]

    def test_a_lecture_with_no_slides_is_refused_with_a_reason(self, qapp, tmp_path):
        from lecture_saver.core.models import VideoInfo

        video = tmp_path / "v.mp4"
        video.write_bytes(b"x")
        bare = Project.create(
            tmp_path / "p", "Bare", VideoInfo(path=str(video), duration=10.0, has_audio=True)
        )
        dialog, _ = self._dialog(bare, tmp_path)
        assert not dialog._start_button.isEnabled()
        assert "Detect slides first" in dialog._status.text()

    def test_the_panel_says_what_is_and_is_not_sent(self, qapp, project, tmp_path):
        dialog, _ = self._dialog(project, tmp_path)
        notice = dialog._notice.text()
        assert "text" in notice
        assert "video" in notice and "not sent" in notice

    def test_the_agreement_names_what_leaves_the_computer(self):
        from lecture_saver.ui.translate_dialog import consent_text

        text = consent_text("Codex CLI")
        assert "transcript" in text and "leaves this computer" in text
        assert "never sent" in text and "slide images" in text
        assert "Codex CLI" in text

    def test_translation_is_off_until_it_is_agreed_to(self, qapp, project, tmp_path):
        """Nothing may be sent on a fresh install without an explicit yes (§29)."""
        from lecture_saver.config import Settings

        settings = Settings(tmp_path / "fresh.json")
        assert settings.translation_enabled is False

    def test_agreeing_once_is_remembered(self, qapp, project, tmp_path):
        dialog, settings = self._dialog(project, tmp_path, translation_enabled=True)
        # Already agreed, so _agreed() must not raise a dialog in a headless test.
        assert dialog._agreed() is True
        assert settings.translation_enabled is True

    def test_a_saved_batch_reaches_the_project_and_announces_itself(
        self, qapp, project, tmp_path
    ):
        from lecture_saver.translate.batching import Batch
        from lecture_saver.translate.runner import BatchOutcome

        dialog, _ = self._dialog(project, tmp_path, translation_enabled=True)
        changed: list[bool] = []
        dialog.changed.connect(lambda: changed.append(True))

        dialog._on_batch(BatchOutcome(batch=Batch(()), answer="## Slide 3\n번역 셋"))
        assert changed
        assert dialog._saved == 1
        appearance = project.slides.appearances[2]
        assert project.translations.for_appearance(appearance) == "번역 셋"


class TestWindowWiring:
    def test_the_window_offers_the_translation_once_a_project_is_open(
        self, qapp, project, tmp_path
    ):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            assert not window._translate_action.isEnabled()
            window._open_project_at(project.root)
            assert window._translate_action.isEnabled()
        finally:
            window.close()
