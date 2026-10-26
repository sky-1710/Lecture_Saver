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
        panel._keywords_edit.setPlainText(" TLB ,, Naive  Bayes ")
        assert panel.options().keywords == "TLB, Naive Bayes"

    def test_defaults_without_keywords_clear_the_box(self, panel):
        panel._keywords_edit.setPlainText("from the last lecture")
        panel.set_defaults({"model_size": "medium"})
        assert panel._keywords_edit.toPlainText() == ""
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


class TestFromSlides:
    def test_unavailable_until_there_are_slides_and_says_why(self, panel):
        assert not panel._slides_button.isEnabled()
        assert "Detect slides" in panel._slides_button.toolTip()
        panel.set_slide_keywords_available(True)
        assert panel._slides_button.isEnabled()

    def test_not_while_transcribing_or_already_reading(self, panel):
        panel.set_slide_keywords_available(True)
        panel.begin_run()
        assert not panel._slides_button.isEnabled()
        panel.end_run("Done.")
        panel.begin_keyword_scan()
        assert not panel._slides_button.isEnabled()
        panel.end_keyword_scan([])
        assert panel._slides_button.isEnabled()

    def test_found_terms_follow_what_was_typed(self, panel):
        panel._keywords_edit.setPlainText("TLB, page table")
        added = panel.end_keyword_scan(["Page Table", "address space", "TLB"])
        assert panel.keywords_text == "TLB, page table, address space"
        assert added == 1


@pytest.fixture(scope="module")
def real_video(tmp_path_factory):
    """A genuine 2-second MP4: the player hangs on anything less."""
    import subprocess

    from lecture_saver.media.ffmpeg_tools import ffmpeg_available, find_ffmpeg

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not available")
    path = tmp_path_factory.mktemp("video") / "lecture.mp4"
    subprocess.run(
        [
            str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


def test_reading_the_slides_fills_the_keywords_and_keeps_them(
    qapp, tmp_path, real_video, monkeypatch
):
    import time

    from PIL import Image

    from lecture_saver.config import Settings
    from lecture_saver.core.keywords import TextLine
    from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
    from lecture_saver.media.ffmpeg_tools import probe_video
    from lecture_saver.ui import lecture_view, main_window
    from lecture_saver.ui.main_window import MainWindow

    project = Project.create(tmp_path / "p", "Lecture", probe_video(real_video))
    for name in ("001.jpg", "002.jpg"):
        Image.new("RGB", (64, 48), "white").save(project.slides_dir / name)
    project.set_slides(SlideSet(
        slides=[Slide(1, "slides/001.jpg"), Slide(2, "slides/002.jpg"), Slide(3, "")],
        appearances=[SlideAppearance(1, 1, 0.0, 1.0), SlideAppearance(2, 2, 1.0, 2.0)],
    ))
    project.save_slides()

    read: list[str] = []

    class FakeReader:
        def read(self, image):
            read.append(image.name)
            return [TextLine("Gradient Descent", 0.045), TextLine("the learning rate", 0.023)]

    monkeypatch.setattr(main_window, "SlideTextReader", FakeReader)
    monkeypatch.setattr(lecture_view, "unavailable_reason", lambda: None)

    window = MainWindow(Settings(tmp_path / "settings.json"))
    try:
        window._open_project_at(project.root)
        panel = window._lecture_view.panel
        panel._keywords_edit.setPlainText("TLB")
        assert panel._slides_button.isEnabled()

        panel._slides_button.click()
        deadline = time.monotonic() + 30
        while window._keywords_thread is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)

        assert sorted(read) == ["001.jpg", "002.jpg"]  # the imageless slide is skipped
        assert panel.keywords_text.startswith("TLB, ")
        assert "Gradient Descent" in panel.keywords_text
        assert "Added" in panel._detail_label.text()
        saved = Project.open(project.root).meta.transcription.keywords
        assert saved == panel.keywords_text
    finally:
        window.close()


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
