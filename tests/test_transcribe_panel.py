"""The transcription options: the Turbo default, keywords, and which choices a
lecture opens with (§27)."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6.QtWidgets")

from lecture_saver.core.models import StageStatus, VideoInfo  # noqa: E402
from lecture_saver.core.project import Project  # noqa: E402
from lecture_saver.transcription import openvino_engine  # noqa: E402
from lecture_saver.transcription.whisper_engine import (  # noqa: E402
    ENGINE_OPENVINO,
    TURBO_MODEL,
)
from lecture_saver.ui import transcribe_panel  # noqa: E402
from lecture_saver.ui.lecture_view import _transcription_choices  # noqa: E402
from lecture_saver.ui.transcribe_panel import TranscribePanel  # noqa: E402


@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(transcribe_panel, "openvino_gpu_available", lambda: True)
    return TranscribePanel()


class TestPanel:
    def test_turbo_is_the_default(self, panel):
        options = panel.options()
        assert options.model_size == TURBO_MODEL
        assert options.resolved_engine == ENGINE_OPENVINO

    def test_keywords_reach_the_options_cleaned(self, panel):
        panel._keywords_edit.setText(" TLB ,, Naive  Bayes ")
        assert panel.options().keywords == "TLB, Naive Bayes"

    def test_defaults_without_keywords_clear_the_box(self, panel):
        panel._keywords_edit.setText("from the last lecture")
        panel.set_defaults({"model_size": "medium"})
        assert panel._keywords_edit.text() == ""
        assert panel.options().model_size == "medium"

    def test_the_estimate_names_the_intel_gpu_for_turbo(self, panel, monkeypatch):
        panel.set_video_duration(3600.0)
        assert "Intel GPU" in panel._estimate_label.text()
        monkeypatch.setattr(transcribe_panel, "openvino_gpu_available", lambda: False)
        panel._update_estimate()
        assert "no Intel GPU" in panel._estimate_label.text()

    def test_options_are_locked_while_running(self, panel):
        panel.begin_run()
        assert not panel._keywords_edit.isEnabled()
        panel.end_run("Done.")
        assert panel._keywords_edit.isEnabled()


class TestOpeningChoices:
    @pytest.fixture
    def project(self, tmp_path):
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"x")
        return Project.create(
            tmp_path / "p", "Lecture", VideoInfo(path=str(video), duration=60.0, has_audio=True)
        )

    def test_a_new_lecture_starts_from_the_last_choices(self, project):
        defaults = {"model_size": TURBO_MODEL, "language": "ko", "vad_filter": True}
        assert _transcription_choices(project, defaults) == {**defaults, "keywords": ""}

    def test_a_new_lecture_without_saved_choices_uses_the_panel_defaults(self, project):
        assert _transcription_choices(project, None) == {"keywords": ""}

    def test_a_transcribed_lecture_shows_what_it_was_transcribed_with(self, project):
        project.meta.transcription.model_size = "medium"
        project.meta.transcription.keywords = "TLB"
        project.set_stage("transcription", StageStatus.DONE)
        choices = _transcription_choices(project, {"model_size": TURBO_MODEL})
        assert choices["model_size"] == "medium"
        assert choices["keywords"] == "TLB"


def test_the_gpu_probe_never_raises(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "openvino":
            raise OSError("driver exploded")
        return real_import(name, *args, **kwargs)

    openvino_engine.openvino_gpu_available.cache_clear()
    monkeypatch.setattr(builtins, "__import__", broken)
    try:
        assert openvino_engine.openvino_gpu_available() is False
    finally:
        openvino_engine.openvino_gpu_available.cache_clear()
