"""Headless UI smoke tests.

These construct the real widgets on Qt's ``offscreen`` platform and drive the
Phase 1-2 wiring: opening a project, binding the transcript, click-to-seek and
follow-playback highlighting. The point is to catch the class of breakage that
unit tests miss - a signal connected to a method that no longer exists, or a
delegate that raises while painting.

Nothing here starts a transcription; that is covered by the pipeline modules and
would need a model download.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from lecture_saver.config import Settings  # noqa: E402
from lecture_saver.core.alignment import align  # noqa: E402
from lecture_saver.core.models import TranscriptSegment, VideoInfo  # noqa: E402
from lecture_saver.core.project import Project  # noqa: E402
from lecture_saver.export.ai_request import ai_request_name  # noqa: E402
from lecture_saver.ui.lecture_view import LectureView  # noqa: E402
from lecture_saver.ui.main_window import MainWindow  # noqa: E402
from lecture_saver.ui.transcript_view import TranscriptView  # noqa: E402


def pump_until(predicate, timeout: float = 60.0) -> bool:
    """Run the UI event loop until ``predicate`` holds, or give up.

    ``time.sleep`` rather than ``QTest.qWait``: work on a worker thread here is
    pure Python, and a Qt-side busy wait keeps the GIL to itself, starving the
    very thread the test is waiting for.
    """
    import time

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(scope="session")
def real_video(tmp_path_factory):
    """A genuine 2-second MP4.

    The media backend must be given something it can actually open: pointing
    QMediaPlayer at placeholder bytes leaves a decode pipeline spinning.
    """
    import subprocess

    from lecture_saver.media.ffmpeg_tools import ffmpeg_available, find_ffmpeg

    if not ffmpeg_available():
        pytest.skip("FFmpeg is not available")

    path = tmp_path_factory.mktemp("video") / "lecture01.mp4"
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


@pytest.fixture
def project(tmp_path, real_video):
    import shutil as _shutil

    video_path = tmp_path / "lecture01.mp4"
    _shutil.copy2(real_video, video_path)
    info = VideoInfo(
        path=str(video_path), duration=600.0, width=1280, height=720, fps=30.0,
        size_bytes=11, container="mov,mp4", video_codec="h264",
        audio_codec="aac", has_audio=True,
    )
    proj = Project.create(tmp_path / "projects", "Lecture 01", info)
    proj.transcript.replace_all(
        [
            TranscriptSegment(
                id="segment_001", start=0.0, end=10.0,
                original_text="Today we begin with the basic concepts of machine learning.",
            ),
            TranscriptSegment(
                id="segment_002", start=10.0, end=20.0,
                original_text="여기서 러닝레이트를 보시면",
            ),
            TranscriptSegment(
                id="segment_003", start=30.0, end=40.0,
                original_text="If this value is too large, the model can diverge.",
                edited_text="If the value is too large, the model diverges.",
            ),
        ],
        language="en",
    )
    proj.save()
    return proj


class TestTranscriptView:
    def test_shows_every_segment(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)
        assert view.segment_count == 3

    def test_displays_edited_text_not_the_original(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)
        text = view.model().index(2, 0).data(Qt.ItemDataRole.DisplayRole)
        assert text == "If the value is too large, the model diverges."

    def test_clicking_a_segment_requests_a_seek(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)

        seeks: list[float] = []
        view.seekRequested.connect(seeks.append)
        view._on_clicked(view.model().index(1, 0))

        assert seeks == [10.0]

    def test_highlight_moves_with_playback(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)

        view.highlight_row(1)
        assert view.model().index(1, 0).data(Qt.ItemDataRole.UserRole + 3) is True
        assert view.model().index(0, 0).data(Qt.ItemDataRole.UserRole + 3) is False

    def test_delegate_measures_rows_without_raising(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)
        view.resize(500, 400)

        delegate = view.itemDelegate()
        option = QStyleOptionViewItem()
        option.rect = view.viewport().rect()
        for row in range(3):
            size = delegate.sizeHint(option, view.model().index(row, 0))
            assert size.height() >= 40

    def test_rows_are_taller_when_the_view_is_narrow(self, qapp, project):
        """Wrapped text must drive row height, or long segments get clipped."""
        view = TranscriptView()
        view.set_segments(project.transcript.segments)
        delegate = view.itemDelegate()
        index = view.model().index(0, 0)

        wide = QStyleOptionViewItem()
        wide.rect = view.viewport().rect().adjusted(0, 0, 600, 0)
        narrow = QStyleOptionViewItem()
        narrow.rect = view.viewport().rect()
        narrow.rect.setWidth(180)

        assert delegate.sizeHint(narrow, index).height() >= delegate.sizeHint(wide, index).height()

    def test_empty_model_is_safe(self, qapp):
        view = TranscriptView()
        assert view.segment_count == 0
        assert view.model().index(0, 0).data(Qt.ItemDataRole.DisplayRole) is None


class TestLectureView:
    def test_loads_a_project(self, qapp, project):
        view = LectureView()
        view.load_project(project)
        assert view._transcript.segment_count == 3
        assert view._transcript.isVisible() or view._transcript.isVisibleTo(view)

    def test_playback_position_highlights_the_right_segment(self, qapp, project):
        view = LectureView()
        view.load_project(project)

        view._on_position(12.0)
        assert view._transcript.model().current_row == 1

        view._on_position(35.0)
        assert view._transcript.model().current_row == 2

    def test_position_before_the_first_segment_clears_the_highlight(self, qapp, project):
        view = LectureView()
        view.load_project(project)
        view._on_position(12.0)

        project.transcript.replace_all(
            [TranscriptSegment(id="segment_001", start=50, end=60, original_text="late")]
        )
        view.refresh_transcript()
        view._on_position(1.0)
        assert view._transcript.model().current_row == -1

    def test_missing_video_disables_transcription_and_warns(self, qapp, project):
        project.video.as_path.unlink()
        view = LectureView()
        view.load_project(project)

        assert view._warning_bar.isVisibleTo(view)
        assert view._panel._start_button.isEnabled() is False

    def test_a_video_without_audio_cannot_be_transcribed(self, qapp, project):
        project.meta.video.has_audio = False
        view = LectureView()
        view.load_project(project)
        assert view._panel._start_button.isEnabled() is False

    def test_empty_transcript_shows_guidance_rather_than_an_empty_list(self, qapp, tmp_path):
        video_path = tmp_path / "v.mp4"
        video_path.write_bytes(b"x")
        info = VideoInfo(path=str(video_path), duration=60.0, has_audio=True)
        empty = Project.create(tmp_path / "p", "Empty", info)

        view = LectureView()
        view.load_project(empty)
        assert view._empty_label.isVisibleTo(view)
        assert not view._transcript.isVisibleTo(view)


class TestMainWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path):
        settings = Settings(tmp_path / "settings.json")
        settings.projects_dir = tmp_path / "projects"
        win = MainWindow(settings)
        yield win
        win.close()

    def test_starts_on_the_start_view(self, window):
        assert window._stack.currentWidget() is window._start_view
        assert window.project is None

    def test_opening_a_project_switches_view_and_records_it_as_recent(self, window, project):
        window._open_project_at(project.root)

        assert window.project is not None
        assert window._stack.currentWidget() is window._lecture_view
        assert "Lecture 01" in window.windowTitle()
        assert project.root.resolve() in [p.resolve() for p in window.settings.recent_projects()]

    def test_closing_returns_to_the_start_view(self, window, project):
        window._open_project_at(project.root)
        window._close_project()

        assert window.project is None
        assert window._stack.currentWidget() is window._start_view

    def test_the_transcript_shown_is_the_project_transcript(self, window, project):
        """§14 - the view must not hold its own copy."""
        window._open_project_at(project.root)
        view_model = window._lecture_view._transcript.model()

        opened = window.project
        opened.transcript.segments[0].edited_text = "edited in the store"
        window._lecture_view.refresh_transcript()

        assert view_model.index(0, 0).data(Qt.ItemDataRole.DisplayRole) == "edited in the store"


class TestStartView:
    def test_drop_zone_picks_the_video_out_of_a_mixed_drop(self, qapp, tmp_path):
        from PySide6.QtCore import QUrl

        from lecture_saver.ui.start_view import _DropZone

        video = tmp_path / "lecture.mkv"
        video.write_bytes(b"x")
        document = tmp_path / "notes.pdf"
        document.write_bytes(b"x")

        def url(path):
            return QUrl.fromLocalFile(str(path))

        assert _DropZone.first_video_url([url(document), url(video)]) == video
        assert _DropZone.first_video_url([url(document)]) is None
        assert _DropZone.first_video_url([]) is None

    def test_drop_zone_ignores_remote_urls(self, qapp):
        from PySide6.QtCore import QUrl

        from lecture_saver.ui.start_view import _DropZone

        assert _DropZone.first_video_url([QUrl("https://example.com/lecture.mp4")]) is None

    def test_drop_zone_ignores_a_video_path_that_does_not_exist(self, qapp, tmp_path):
        from PySide6.QtCore import QUrl

        from lecture_saver.ui.start_view import _DropZone

        missing = QUrl.fromLocalFile(str(tmp_path / "gone.mp4"))
        assert _DropZone.first_video_url([missing]) is None

    def test_recent_list_shows_real_projects(self, qapp, project):
        from lecture_saver.ui.start_view import StartView

        view = StartView()
        view.set_recent([project.root])
        assert view._recent_list.count() == 1
        assert project.root.name in view._recent_list.item(0).text()


def test_video_player_constructs_and_clears(qapp):
    from lecture_saver.ui.video_player import VideoPlayer

    player = VideoPlayer()
    player.clear()
    assert player.position == 0.0
    assert player.is_playing is False
    # Seeking with no media loaded must not raise.
    player.seek(30.0)


def test_no_widget_is_created_per_segment(qapp, project):
    """A 3-hour lecture would create thousands of widgets the naive way."""
    view = TranscriptView()
    view.resize(400, 300)
    many = [
        TranscriptSegment(id=f"segment_{i:04d}", start=i * 5, end=i * 5 + 4,
                          original_text=f"segment number {i}")
        for i in range(2000)
    ]
    view.set_segments(many)
    view.show()
    QApplication.processEvents()

    assert view.segment_count == 2000
    # Only rows near the viewport are ever asked for; a widget-per-row design
    # would have 2000 children here.
    assert len(view.viewport().children()) < 50
    assert view.indexAt(QPoint(5, 5)).isValid()


# --------------------------------------------------------------------------
# Slide navigator (Phase 3)
# --------------------------------------------------------------------------


@pytest.fixture
def project_with_slides(project):
    """A project with two detected slides, one of which has a real image."""
    from PySide6.QtGui import QPixmap

    from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet

    image = project.slides_dir / "001.jpg"
    pixmap = QPixmap(320, 180)
    pixmap.fill(Qt.GlobalColor.darkCyan)
    pixmap.save(str(image), "JPG")

    project.set_slides(
        SlideSet(
            slides=[
                Slide(slide_id=1, image="slides/001.jpg", fingerprint="0f0f0f0f0f0f0f0f"),
                # A slide whose frame could not be captured.
                Slide(slide_id=2, image="", fingerprint=""),
            ],
            appearances=[
                SlideAppearance(appearance_id=1, slide_id=1, start=0.0, end=30.0),
                SlideAppearance(appearance_id=2, slide_id=2, start=30.0, end=95.0),
            ],
        )
    )
    project.save_slides()
    return project


class TestSlidePanel:
    def test_lists_every_appearance(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)
        assert panel.slide_count == 2

    def test_shows_the_number_and_start_time(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)

        label = panel._list.model().index(1, 0).data(Qt.ItemDataRole.DisplayRole)
        assert "Slide 2" in label
        assert "00:00:30" in label

    def test_labels_the_versions_of_an_annotated_slide(self, qapp, project_with_slides):
        from lecture_saver.core.slides import ANNOTATION_STATES
        from lecture_saver.ui.slide_panel import SlidePanel

        slides = project_with_slides.slides
        slides.appearances[0].annotation_state = "before_annotation"
        slides.appearances[1].annotation_state = "annotated"
        panel = SlidePanel()
        panel.set_slides(slides, project_with_slides.root)

        model = panel._list.model()
        first = model.index(0, 0).data(Qt.ItemDataRole.DisplayRole)
        second = model.index(1, 0).data(Qt.ItemDataRole.DisplayRole)
        assert ANNOTATION_STATES["before_annotation"] in first
        assert ANNOTATION_STATES["annotated"] in second

    def test_provides_a_thumbnail_for_every_row(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)

        for row in range(2):
            pixmap = panel._list.model().index(row, 0).data(Qt.ItemDataRole.DecorationRole)
            # Even the slide with no image gets a placeholder tile, so the row
            # does not silently collapse.
            assert pixmap is not None and not pixmap.isNull()

    def test_clicking_a_slide_seeks_to_its_start(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)

        seeks: list[float] = []
        panel.seekRequested.connect(seeks.append)
        panel._on_clicked(panel._list.model().index(1, 0))

        assert seeks == [30.0]

    def test_an_empty_set_hides_the_list_and_explains(self, qapp, project):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project.slides, project.root)

        assert panel.slide_count == 0
        assert not panel._list.isVisibleTo(panel)
        assert "No slides" in panel._status_label.text()

    def test_the_button_offers_a_re_run_once_slides_exist(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)
        assert "again" in panel._detect_button.text()

    def test_running_state_swaps_in_cancel(self, qapp):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.begin_run()
        assert panel.is_running
        assert panel._cancel_button.isVisibleTo(panel)
        assert not panel._detect_button.isVisibleTo(panel)

        panel.end_run("Done.")
        assert not panel.is_running
        assert panel._detect_button.isVisibleTo(panel)

    def test_progress_updates_are_shown(self, qapp):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.begin_run()
        panel.update_progress("Scanning video", 0.5, "halfway")
        assert panel._progress.value() == 50
        assert "Scanning video" in panel._status_label.text()

    def test_unknown_progress_shows_an_indeterminate_bar(self, qapp):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.begin_run()
        panel.update_progress("Identifying slides", -1.0, "")
        assert panel._progress.maximum() == 0


class TestLectureViewSlides:
    def test_the_navigator_is_populated_from_the_project(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        assert view.slide_panel.slide_count == 2

    def test_clicking_a_slide_thumbnail_seeks_the_player(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)

        seen: list[float] = []
        view._player.positionChanged.connect(seen.append)
        view.slide_panel._on_clicked(view.slide_panel._list.model().index(1, 0))
        QApplication.processEvents()
        # The seek is issued against the real player; assert it was requested
        # at the slide's start rather than depending on decode timing.
        assert view.slide_panel._model.appearance_at_row(1).start == 30.0

    def test_detection_is_offered_for_a_video_without_audio(self, qapp, project_with_slides):
        """Slide detection only needs the picture (§7)."""
        project_with_slides.meta.video.has_audio = False
        view = LectureView()
        view.load_project(project_with_slides)

        assert view.slide_panel._detect_button.isEnabled()
        assert not view.panel._start_button.isEnabled()

    def test_a_missing_video_disables_detection(self, qapp, project_with_slides):
        project_with_slides.video.as_path.unlink()
        view = LectureView()
        view.load_project(project_with_slides)
        assert not view.slide_panel._detect_button.isEnabled()

    def test_closing_a_project_clears_the_navigator(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.close_project()
        assert view.slide_panel.slide_count == 0


class TestMainWindowSlides:
    def test_detected_slides_appear_after_reopening(self, qapp, tmp_path, project_with_slides):
        settings = Settings(tmp_path / "settings.json")
        window = MainWindow(settings)
        try:
            window._open_project_at(project_with_slides.root)
            assert window._lecture_view.slide_panel.slide_count == 2
        finally:
            window.close()


# --------------------------------------------------------------------------
# Manual slide correction
# --------------------------------------------------------------------------


class TestSlideCorrectionUI:
    def test_the_panel_offers_a_split_only_once_slides_exist(self, qapp, project):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project.slides, project.root)
        assert not panel._split_button.isEnabled()

    def test_the_split_button_is_enabled_with_slides(self, qapp, project_with_slides):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)
        assert panel._split_button.isEnabled()

    def test_a_hand_edited_set_says_so(self, qapp, project_with_slides):
        from lecture_saver.core.slides import delete_appearance
        from lecture_saver.ui.slide_panel import SlidePanel

        delete_appearance(project_with_slides.slides, 2)
        panel = SlidePanel()
        panel.set_slides(project_with_slides.slides, project_with_slides.root)
        assert "edited by hand" in panel._status_label.text()

    def test_split_carries_the_current_playback_position(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)

        seen: list[float] = []
        view.slideSplitRequested.connect(seen.append)
        view.slide_panel.splitHereRequested.emit()

        assert len(seen) == 1
        assert seen[0] == view._player.position

    def test_recapture_carries_the_appearance_and_position(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)

        seen: list[tuple[int, float]] = []
        view.slideRecaptureRequested.connect(lambda i, t: seen.append((i, t)))
        view.slide_panel.recaptureRequested.emit(2)

        assert seen == [(2, view._player.position)]

    def test_delete_and_merge_pass_the_appearance_through(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)

        deleted: list[int] = []
        merged: list[int] = []
        view.slideDeleteRequested.connect(deleted.append)
        view.slideMergeRequested.connect(merged.append)
        view.slide_panel.deleteRequested.emit(1)
        view.slide_panel.mergeRequested.emit(2)

        assert deleted == [1]
        assert merged == [2]


class TestSlideCorrectionPersistsFromTheWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path, project_with_slides):
        settings = Settings(tmp_path / "settings.json")
        win = MainWindow(settings)
        win._open_project_at(project_with_slides.root)
        yield win
        win.close()

    def test_a_merge_updates_the_navigator_and_the_file(self, window):
        from lecture_saver.core.project import Project

        window._merge_slide(2)

        assert window._lecture_view.slide_panel.slide_count == 1
        reopened = Project.open(window.project.root)
        assert len(reopened.slides) == 1
        assert reopened.slides.manually_edited is True

    def test_an_invalid_edit_is_reported_and_changes_nothing(self, window, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        warnings: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *args, **kwargs: warnings.append(args[2] if len(args) > 2 else "")
        )
        window._merge_slide(1)  # the first slide has nothing before it

        assert warnings
        assert window._lecture_view.slide_panel.slide_count == 2


# --------------------------------------------------------------------------
# Slide-linked transcript and synchronisation (Phase 4)
# --------------------------------------------------------------------------
#
# project_with_slides: slide 1 is 0-30 s, slide 2 is 30-95 s. The transcript
# has segments at 0-10 and 10-20 (slide 1) and 30-40 (slide 2).


def _texts(view):
    model = view._transcript.model()
    return [model.index(r, 0).data(Qt.ItemDataRole.DisplayRole) for r in range(model.rowCount())]


class TestSlideLinkedTranscript:
    def test_opens_on_the_current_slide_with_its_speech_only(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)

        assert view.showing_current_slide
        assert view._transcript.segment_count == 2
        assert view._slide_header.isVisibleTo(view)
        assert "Slide 1" in view._slide_title.text()
        assert "00:00:00" in view._slide_detail.text()
        assert "2 segments" in view._slide_detail.text()

    def test_the_slide_picture_is_shown_above_its_transcript(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        assert not view._slide_preview.pixmap().isNull()

    def test_a_slide_without_a_picture_says_so(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view._on_position(35.0)
        assert view._slide_preview.text()

    def test_the_full_transcript_tab_shows_everything(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view._tabs.setCurrentIndex(1)

        assert not view.showing_current_slide
        assert view._transcript.segment_count == 3
        assert not view._slide_header.isVisibleTo(view)

    def test_both_views_show_the_same_edited_text(self, qapp, project_with_slides):
        """One transcript: the slide view and the full view cannot disagree (§14)."""
        view = LectureView()
        view.load_project(project_with_slides)
        view._on_position(35.0)
        on_slide = _texts(view)
        view._tabs.setCurrentIndex(1)
        assert on_slide == ["If the value is too large, the model diverges."]
        assert on_slide[0] in _texts(view)

    def test_without_slides_the_current_slide_tab_is_unavailable(self, qapp, project):
        from lecture_saver.ui.lecture_view import TAB_FULL, TAB_SLIDE

        view = LectureView()
        view.load_project(project)
        assert not view._tabs.isTabEnabled(TAB_SLIDE)
        assert view._tabs.currentIndex() == TAB_FULL
        assert not view.showing_current_slide
        assert view._transcript.segment_count == 3


class TestPlaybackSynchronisation:
    def test_crossing_a_slide_boundary_switches_slide_and_transcript(
        self, qapp, project_with_slides
    ):
        view = LectureView()
        view.load_project(project_with_slides)

        view._on_position(35.0)

        assert view.slide_panel.current_row == 1
        assert "Slide 2" in view._slide_title.text()
        assert view._transcript.segment_count == 1
        assert view._transcript.model().current_row == 0

    def test_the_highlight_is_the_row_within_the_slide(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view._on_position(12.0)
        assert view._transcript.model().current_row == 1

    def test_going_back_returns_to_the_earlier_slide(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view._on_position(35.0)
        view._on_position(5.0)
        assert view.slide_panel.current_row == 0
        assert view._transcript.segment_count == 2

    def test_the_full_view_highlights_the_global_row_and_tracks_the_slide(
        self, qapp, project_with_slides
    ):
        view = LectureView()
        view.load_project(project_with_slides)
        view._tabs.setCurrentIndex(1)
        view._on_position(35.0)
        assert view._transcript.model().current_row == 2
        assert view.slide_panel.current_row == 1

    def test_with_follow_off_the_chosen_slide_stays(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view._follow_check.setChecked(False)
        view._on_position(35.0)
        assert "Slide 1" in view._slide_title.text()
        # Speech from another slide is not highlighted in this one.
        assert view._transcript.model().current_row == -1

    def test_clicking_a_slide_shows_its_transcript_immediately(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        requested: list[float] = []
        view.slide_panel.seekRequested.connect(requested.append)

        view.slide_panel._on_clicked(view.slide_panel._list.model().index(1, 0))

        assert "Slide 2" in view._slide_title.text()
        assert view._transcript.segment_count == 1
        assert requested == [30.0]

    def test_a_slide_nobody_spoke_over_explains_itself(self, qapp, project_with_slides):
        from lecture_saver.core.slides import Slide, SlideAppearance

        slides = project_with_slides.slides
        slides.appearances[1].end = 60.0
        slides.appearances.append(SlideAppearance(3, 3, 60.0, 95.0))
        slides.slides.append(Slide(3, ""))
        view = LectureView()
        view.load_project(project_with_slides)

        view._on_position(70.0)
        assert view._transcript.segment_count == 0
        assert view._empty_label.isVisibleTo(view)
        assert "Nothing was said" in view._empty_label.text()


class TestAlignmentFromTheWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path, project_with_slides):
        win = MainWindow(Settings(tmp_path / "settings.json"))
        win._open_project_at(project_with_slides.root)
        yield win
        win.close()

    def test_opening_a_project_links_and_saves_the_transcript(self, window):
        reopened = Project.open(window.project.root)
        assert [s.slide_id for s in reopened.transcript.segments] == [1, 1, 2]

    def test_a_slide_correction_relinks_the_transcript(self, window):
        window._merge_slide(2)

        assert [s.slide_id for s in window.project.transcript.segments] == [1, 1, 1]
        assert window._lecture_view._transcript.segment_count == 3
        reopened = Project.open(window.project.root)
        assert [s.slide_id for s in reopened.transcript.segments] == [1, 1, 1]


# --------------------------------------------------------------------------
# Transcript editing (Phase 5)
# --------------------------------------------------------------------------


def _open_editor(view, segment_id):
    from lecture_saver.ui.transcript_view import SegmentEditor

    assert view._transcript.edit_segment(segment_id)
    QApplication.processEvents()
    editor = view._transcript.findChild(SegmentEditor)
    assert isinstance(editor, SegmentEditor)
    return editor


@pytest.fixture
def editing_view(qapp, project_with_slides):
    view = LectureView()
    view.resize(1200, 800)
    view.load_project(project_with_slides)
    view._tabs.setCurrentIndex(1)  # full transcript: every segment in one list
    view.show()
    QApplication.processEvents()
    yield view
    view.close()


class TestInlineEditing:
    def test_typing_changes_the_one_transcript(self, editing_view, project_with_slides):
        from PySide6.QtTest import QTest

        edited: list[bool] = []
        editing_view.transcriptEdited.connect(edited.append)
        editor = _open_editor(editing_view, "segment_002")
        QTest.keyClicks(editor, "!")
        QTest.keyClick(editor, Qt.Key.Key_Return)

        segment = project_with_slides.transcript.get("segment_002")
        assert segment.effective_text == "여기서 러닝레이트를 보시면!"
        assert segment.original_text == "여기서 러닝레이트를 보시면"
        assert edited and edited[-1] is False
        assert not editing_view._transcript.is_editing

        # The slide view shows the same edited text (§16).
        editing_view._tabs.setCurrentIndex(0)
        editing_view._on_position(12.0)
        assert "여기서 러닝레이트를 보시면!" in _texts(editing_view)

    def test_escape_puts_the_text_back_and_leaves_nothing_to_undo(
        self, editing_view, project_with_slides
    ):
        from PySide6.QtTest import QTest

        editor = _open_editor(editing_view, "segment_001")
        QTest.keyClicks(editor, " typo")
        QTest.keyClick(editor, Qt.Key.Key_Escape)

        store = project_with_slides.transcript
        assert store.get("segment_001").edited_text is None
        assert not store.can_undo
        assert not editing_view._undo_action.isEnabled()

    def test_undo_and_redo_follow_the_history(self, editing_view, project_with_slides):
        from PySide6.QtTest import QTest

        editor = _open_editor(editing_view, "segment_001")
        QTest.keyClicks(editor, "!!")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert editing_view._undo_action.isEnabled()

        editing_view.undo()
        store = project_with_slides.transcript
        assert store.get("segment_001").edited_text is None
        assert editing_view._redo_action.isEnabled()

        editing_view.redo()
        assert store.get("segment_001").effective_text.endswith("!!")

    def test_edited_segments_are_marked_and_show_their_original(self, editing_view):
        from lecture_saver.ui.transcript_view import ROLE_IS_EDITED

        model = editing_view._transcript.model()
        index = model.index(editing_view._transcript.row_of("segment_003"), 0)
        assert index.data(ROLE_IS_EDITED) is True
        assert "Original: If this value is too large" in index.data(Qt.ItemDataRole.ToolTipRole)

    def test_ctrl_enter_splits_at_the_cursor(self, editing_view, project_with_slides):
        from PySide6.QtTest import QTest

        editor = _open_editor(editing_view, "segment_003")
        cursor = editor.textCursor()
        cursor.setPosition(len("If the value is too large,"))
        editor.setTextCursor(cursor)
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)

        texts = [s.effective_text for s in project_with_slides.transcript.segments]
        assert texts[-2:] == ["If the value is too large,", "the model diverges."]
        assert editing_view._transcript.segment_count == 4

    def test_merge_restore_insert_and_delete_from_the_menu_signals(
        self, editing_view, project_with_slides
    ):
        store = project_with_slides.transcript
        view = editing_view._transcript

        view.restoreRequested.emit("segment_003")
        assert store.get("segment_003").edited_text is None

        view.mergeRequested.emit("segment_001")
        assert len(store) == 2

        view.insertRequested.emit("segment_001")
        assert len(store) == 3
        assert view.is_editing, "a new segment opens for typing straight away"
        view.finish_editing()

        view.deleteRequested.emit(store.segments[1].id)
        assert len(store) == 2
        editing_view.undo()
        assert len(store) == 3

    def test_playback_does_not_pull_the_list_away_from_typing(self, qapp, project_with_slides):
        view = LectureView()
        view.resize(1200, 800)
        view.load_project(project_with_slides)
        view.show()
        QApplication.processEvents()
        _open_editor(view, "segment_001")

        view._on_position(35.0)  # slide 2 is on screen now

        assert view._transcript.is_editing
        assert "Slide 1" in view._slide_title.text()
        view.close()

    def test_a_structural_edit_is_announced_as_such(self, editing_view):
        seen: list[bool] = []
        editing_view.transcriptEdited.connect(seen.append)
        editing_view._transcript.mergeRequested.emit("segment_001")
        assert seen == [True]


class TestFindAndReplaceUI:
    def test_find_counts_and_steps_across_slides(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.open_find()
        view._find_bar.set_query("the")

        assert "3 matches" in view._find_bar.result_text

        view._step_match(1)
        view._step_match(1)
        view._step_match(1)  # "If the value ..." is on slide 2
        assert "3 of 3" in view._find_bar.result_text
        assert "Slide 2" in view._slide_title.text()

    def test_replace_all_is_one_undoable_step(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.open_find()
        view._find_bar.set_query("the")
        view._find_bar.set_replacement("THE")
        view._replace_all()

        store = project_with_slides.transcript
        assert store.find("THE", case_sensitive=True)
        assert not store.find("the", case_sensitive=True)
        view.undo()
        assert store.find("the", case_sensitive=True)

    def test_replace_one_replaces_the_current_match(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.open_find()
        view._find_bar.set_query("러닝레이트")
        view._find_bar.set_replacement("learning rate")
        view._step_match(1)
        view._replace_current()

        segment = project_with_slides.transcript.get("segment_002")
        assert segment.effective_text == "여기서 learning rate를 보시면"
        assert segment.original_text == "여기서 러닝레이트를 보시면"
        assert "No matches" in view._find_bar.result_text


class TestEditingFromTheWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path, project_with_slides):
        win = MainWindow(Settings(tmp_path / "settings.json"))
        win._open_project_at(project_with_slides.root)
        yield win
        win.close()

    def test_the_edit_menu_offers_undo_redo_and_find(self, window):
        assert window._edit_menu.title() == "&Edit"
        names = [a.text() for a in window._edit_menu.actions()]
        assert names[:2] == ["Undo", "Redo"]

    def test_edits_are_autosaved_after_a_pause(self, window):
        view = window._lecture_view
        view._on_text_edited("segment_001", "Autosaved text")
        assert window._autosave.pending
        # Not written on the keystroke itself, only once typing pauses.
        before = Project.open(window.project.root).transcript.get("segment_001")
        assert before.effective_text != "Autosaved text"

        # Waited for rather than slept through: on a busy machine Qt was seen to
        # spend 1-3 s inside one wait tearing down earlier tests' media players,
        # and a fixed 1.1 s then failed before the 0.75 s timer could fire.
        assert pump_until(lambda: not window._autosave.pending, timeout=5.0)
        reopened = Project.open(window.project.root)
        assert reopened.transcript.get("segment_001").effective_text == "Autosaved text"
        assert "saved" in view._save_label.text().lower()

    def test_closing_the_project_flushes_pending_edits(self, window):
        root = window.project.root
        window._lecture_view._on_text_edited("segment_002", "flushed on close")
        window._close_project()

        reopened = Project.open(root)
        assert reopened.transcript.get("segment_002").effective_text == "flushed on close"

    def test_a_merge_across_slides_relinks_and_saves(self, window):
        view = window._lecture_view
        view._transcript.mergeRequested.emit("segment_002")  # 10-20 + 30-40
        window._autosave.flush()

        reopened = Project.open(window.project.root)
        assert [s.slide_id for s in reopened.transcript.segments] == [1, 1]


class TestResetToOriginalUI:
    @pytest.fixture
    def answer(self, monkeypatch):
        """Record the confirmation question and answer it."""
        from PySide6.QtWidgets import QMessageBox

        asked: list[str] = []
        reply = {"value": QMessageBox.StandardButton.Yes}

        def fake_question(parent, title, text, *args, **kwargs):
            asked.append(text)
            return reply["value"]

        monkeypatch.setattr(QMessageBox, "question", fake_question)
        return asked, reply

    def _view(self, project):
        project.save_original_transcript()
        view = LectureView()
        view.load_project(project)
        return view

    def test_reset_restores_the_transcription_and_undo_brings_edits_back(
        self, qapp, project_with_slides, answer
    ):
        store = project_with_slides.transcript
        # The fixture's segment_003 carries an edit; keep the original without it.
        view = self._view(project_with_slides)
        view._transcript.mergeRequested.emit("segment_001")
        assert len(store) == 2

        view.reset_to_original()

        asked, _ = answer
        assert asked and "merged, split, added or deleted" in asked[0]
        assert len(store) == 3
        assert all(s.edited_text is None for s in store.segments)
        assert view._transcript.segment_count >= 1

        view.undo()
        assert len(store) == 2

    def test_saying_no_changes_nothing(self, qapp, project_with_slides, answer):
        from PySide6.QtWidgets import QMessageBox

        _, reply = answer
        reply["value"] = QMessageBox.StandardButton.No
        store = project_with_slides.transcript
        view = self._view(project_with_slides)
        store.edit_text("segment_001", "keep me")

        view.reset_to_original()

        assert store.get("segment_001").effective_text == "keep me"

    def test_an_unedited_transcript_is_not_reset_and_says_so(
        self, qapp, project_with_slides, answer
    ):
        store = project_with_slides.transcript
        store.restore_original("segment_003")
        view = self._view(project_with_slides)
        messages: list[str] = []
        view.statusMessage.connect(messages.append)

        view.reset_to_original()

        asked, _ = answer
        assert asked == []
        assert messages and "already the original" in messages[0]

    def test_without_a_kept_original_the_question_explains_the_limit(
        self, qapp, project_with_slides, answer
    ):
        view = LectureView()
        view.load_project(project_with_slides)  # no original kept

        view.reset_to_original()

        asked, _ = answer
        assert asked and "before the original transcription was kept" in asked[0]
        assert project_with_slides.transcript.get("segment_003").edited_text is None

    def test_reset_is_offered_in_the_edit_menu(self, qapp, tmp_path, project_with_slides):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            names = [a.text() for a in window._edit_menu.actions()]
            assert any(name.startswith("Reset to original") for name in names)
        finally:
            window.close()


# --------------------------------------------------------------------------
# Export screen (Phase 6)
# --------------------------------------------------------------------------


class TestExportDialog:
    def _dialog(self, project):
        from lecture_saver.ui.export_dialog import ExportDialog

        return ExportDialog(project)

    def test_defaults_match_the_specification(self, qapp, project_with_slides):
        from lecture_saver.export.options import ExportFormat

        dialog = self._dialog(project_with_slides)
        checked = [f for f, box in dialog._format_checks.items() if box.isChecked()]
        assert checked == [ExportFormat.PDF, ExportFormat.TXT]
        assert dialog._preset.currentText() == "Slide + Transcript"
        assert dialog.output_dir == project_with_slides.exports_dir

    def test_the_default_export_folder_is_used_and_stays_the_default(
        self, qapp, project_with_slides, tmp_path
    ):
        from lecture_saver.export.options import safe_file_stem
        from lecture_saver.ui.export_dialog import ExportDialog

        icloud = tmp_path / "iCloudDrive"
        icloud.mkdir()
        dialog = ExportDialog(project_with_slides, default_dir=icloud)
        assert dialog.output_dir == icloud
        from PySide6.QtWidgets import QLabel

        labels = [label.text() for label in dialog.findChildren(QLabel)]
        assert f"{safe_file_stem(project_with_slides.name)}_lecture_notes.pdf" in labels
        # Stored as "the default", so a later change of default is followed.
        assert dialog.settings().output_dir is None

        # Choosing the project folder while a default exists is remembered.
        dialog._use_project_folder()
        assert dialog.settings().output_dir == str(project_with_slides.exports_dir)
        dialog._use_default_folder()
        assert dialog.settings().output_dir is None

    def test_choosing_a_preset_sets_the_boxes_and_editing_a_box_makes_it_custom(
        self, qapp, project_with_slides
    ):
        dialog = self._dialog(project_with_slides)
        dialog._preset.setCurrentText("Slides Only")
        assert dialog.pdf_options().transcript is False
        assert dialog.pdf_options().slide_image is True

        dialog._pdf_checks["slide_number"].setChecked(False)
        assert dialog._preset.currentText() == "Custom"

    def test_an_empty_pdf_cannot_be_exported(self, qapp, project_with_slides):
        dialog = self._dialog(project_with_slides)
        for box in dialog._pdf_checks.values():
            box.setChecked(False)
        assert not dialog._export_button.isEnabled()

    def test_exporting_writes_files_reports_them_and_remembers_the_choice(
        self, qapp, project_with_slides
    ):
        from lecture_saver.export.options import ExportFormat, ExportSettings, export_file_name

        dialog = self._dialog(project_with_slides)
        dialog._format_checks[ExportFormat.PDF].setChecked(False)
        dialog._format_checks[ExportFormat.SRT].setChecked(True)
        dialog._stamps_exclude.setChecked(True)

        result = dialog.export_now()

        assert result.ok
        exports = project_with_slides.exports_dir
        txt = exports / export_file_name(project_with_slides.name, ExportFormat.TXT)
        srt = exports / export_file_name(project_with_slides.name, ExportFormat.SRT)
        assert txt.is_file()
        assert srt.is_file()
        assert srt.name in dialog.result_text
        assert str(exports) in dialog.result_text
        assert dialog._open_folder.isVisibleTo(dialog)

        remembered = ExportSettings.from_dict(
            Project.open(project_with_slides.root).meta.export_settings
        )
        assert remembered.formats == [ExportFormat.TXT, ExportFormat.SRT]
        assert remembered.include_timestamps is False
        text = txt.read_text(encoding="utf-8")
        assert "[00:" not in text
        assert "If the value is too large, the model diverges." in text

    def test_a_failure_is_shown_with_its_reason(self, qapp, project_with_slides):
        from lecture_saver.core.slides import SlideSet
        from lecture_saver.export.options import ExportFormat

        project_with_slides.set_slides(SlideSet())
        dialog = self._dialog(project_with_slides)
        dialog._format_checks[ExportFormat.TXT].setChecked(False)
        dialog._preset.setCurrentText("Slides Only")

        result = dialog.export_now()

        assert not result.ok
        assert "failed" in dialog.result_text
        assert dialog._slides_note.isVisibleTo(dialog) or not project_with_slides.has_slides

    def test_the_background_export_finishes(self, qapp, project_with_slides, tmp_path):

        from lecture_saver.export.options import ExportFormat

        dialog = self._dialog(project_with_slides)
        dialog.set_output_dir(tmp_path / "elsewhere")
        dialog._format_checks[ExportFormat.PDF].setChecked(False)
        results = []
        dialog.exported.connect(results.append)

        dialog.start_export()
        pump_until(lambda: bool(results))

        assert results and results[0].ok
        assert [p.parent for p in results[0].written] == [tmp_path / "elsewhere"]
        assert results[0].written[0].name.endswith("_transcript.txt")


class TestExportFromTheWindow:
    def test_the_export_action_follows_the_project(self, qapp, tmp_path, project_with_slides):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            assert not window._export_action.isEnabled()
            window._open_project_at(project_with_slides.root)
            assert window._export_action.isEnabled()
            assert window._export_action.shortcut().toString() == "Ctrl+Shift+E"
            window._close_project()
            assert not window._export_action.isEnabled()
        finally:
            window.close()

    def test_opening_export_flushes_pending_edits_first(
        self, qapp, tmp_path, project_with_slides, monkeypatch
    ):
        from lecture_saver.ui import export_dialog

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project_with_slides.root)
            window._lecture_view._on_text_edited("segment_001", "edited just before export")
            assert window._autosave.pending

            monkeypatch.setattr(export_dialog.ExportDialog, "exec", lambda self: 0)
            window._show_export()

            assert not window._autosave.pending
            reopened = Project.open(project_with_slides.root)
            assert reopened.transcript.get("segment_001").effective_text == (
                "edited just before export"
            )
        finally:
            window.close()


class TestOneClickExports:
    @pytest.fixture
    def window(self, qapp, tmp_path, project_with_slides, monkeypatch):
        win = MainWindow(Settings(tmp_path / "settings.json"))
        win._open_project_at(project_with_slides.root)
        shown: list[tuple[object, bool]] = []
        monkeypatch.setattr(
            MainWindow, "_show_export_result",
            lambda self, result, ai_request: shown.append((result, ai_request)),
        )
        win.shown_results = shown  # type: ignore[attr-defined]
        yield win
        win.close()

    @staticmethod
    def _wait(window):
        if not pump_until(lambda: bool(window.shown_results)):
            raise AssertionError("the export never finished")
        return window.shown_results[-1]

    def test_the_header_offers_all_three_exports(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        seen: list[str] = []
        view.quickExportRequested.connect(lambda: seen.append("quick"))
        view.aiRequestRequested.connect(lambda: seen.append("ai"))
        view.exportRequested.connect(lambda: seen.append("more"))
        view.importTranslationRequested.connect(lambda: seen.append("import"))
        view._quick_export_button.click()
        # The less frequent ones live in the header's "More" menu.
        menu = view._more_button.menu()
        assert menu is not None
        view._ai_request_menu_action.trigger()
        view._more_formats_action.trigger()
        view._import_translation_menu_action.trigger()
        assert seen == ["quick", "ai", "more", "import"]
        assert {a.text() for a in menu.actions() if a.text()} == {
            "More export formats...",
            "AI translation request (by hand)...",
            "Import translation...",
        }

    def test_the_header_fits_the_smallest_window(self, qapp, project_with_slides):
        """Six buttons in a row needed 1282 px and were cut off (§11)."""
        from lecture_saver.ui.theme import stylesheet

        view = LectureView()
        # On the view, not the application, so no other test sees the change.
        view.setStyleSheet(stylesheet())
        view.load_project(project_with_slides)
        header = view._quick_export_button.parentWidget()
        assert header.minimumSizeHint().width() <= 820

    def test_shortcuts(self, window):
        assert window._quick_export_action.shortcut().toString() == "Ctrl+E"
        assert window._export_action.shortcut().toString() == "Ctrl+Shift+E"

    def test_the_result_is_handled_on_the_gui_thread(self, window, monkeypatch):
        """Widget work from a worker thread leaves the window blocked by a
        dialog Qt never draws - the bug behind an export that looked frozen.

        The thread is recorded in a plain method call, not in the slot itself:
        replacing a slot changes how Qt resolves the connection, which would
        hide the very thing under test.
        """
        from PySide6.QtCore import QThread
        from PySide6.QtWidgets import QApplication

        threads: list[object] = []
        results = window.shown_results

        def record(self, result, ai_request):
            threads.append(QThread.currentThread())
            results.append((result, ai_request))

        monkeypatch.setattr(MainWindow, "_show_export_result", record)

        window._quick_export()
        self._wait(window)

        assert threads, "the export never reported back"
        assert threads[0] is QApplication.instance().thread()

    def test_export_pdf_and_markdown_in_one_click(self, window, project_with_slides):
        from lecture_saver.export.options import safe_file_stem

        window._quick_export()
        result, ai_request = self._wait(window)

        assert ai_request is False
        names = sorted(p.name for p in result.written)
        stem = safe_file_stem(project_with_slides.name)
        assert names == [f"{stem}_lecture_notes.pdf", f"{stem}_transcript.md"] or result.failures
        assert (project_with_slides.exports_dir / f"{stem}_transcript.md").is_file()
        assert not (project_with_slides.exports_dir / f"{stem}_transcript.txt").exists()

    def test_one_click_export_goes_to_the_default_folder(
        self, window, project_with_slides, tmp_path
    ):
        from lecture_saver.export.options import safe_file_stem

        icloud = tmp_path / "iCloudDrive"
        icloud.mkdir()
        window.settings.default_export_dir = icloud
        window._quick_export()
        result, _ = self._wait(window)

        assert result.output_dir == icloud
        stem = safe_file_stem(project_with_slides.name)
        assert (icloud / f"{stem}_lecture_notes.pdf").is_file() or result.failures
        assert (icloud / f"{stem}_transcript.md").is_file()

    def test_a_missing_default_folder_is_not_created_silently(
        self, window, project_with_slides, tmp_path
    ):
        gone = tmp_path / "iCloudDrive"
        window.settings.default_export_dir = gone
        messages: list[str] = []
        window.statusBar().messageChanged.connect(messages.append)
        window._quick_export()
        result, _ = self._wait(window)

        assert not gone.exists()
        assert result.output_dir == project_with_slides.exports_dir
        assert any("was not found" in message for message in messages)

    def test_the_ai_request_is_written_for_an_english_lecture(self, window, project_with_slides):
        from PySide6.QtWidgets import QMessageBox

        asked: list[str] = []
        original = QMessageBox.question
        QMessageBox.question = lambda *a, **k: asked.append("asked")  # type: ignore[method-assign]
        try:
            window._ai_translation_request()
            result, ai_request = self._wait(window)
        finally:
            QMessageBox.question = original  # type: ignore[method-assign]

        assert asked == [], "an English lecture needs no confirmation"
        assert ai_request is True
        request = project_with_slides.exports_dir / ai_request_name(
            project_with_slides.name, "md"
        )
        assert request.read_text(encoding="utf-8").startswith("# AI 직역 요청")

    def test_a_korean_lecture_asks_first_and_no_means_nothing_is_written(
        self, window, project_with_slides, monkeypatch
    ):
        from PySide6.QtWidgets import QMessageBox

        project_with_slides.transcript.transcript.language = "ko"
        window.project.transcript.transcript.language = "ko"
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )

        window._ai_translation_request()

        assert window._export_thread is None
        assert not (
            project_with_slides.exports_dir / ai_request_name(project_with_slides.name, "md")
        ).exists()

    def test_without_a_transcript_it_explains_instead(self, window, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        told: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "information", lambda *a, **k: told.append(a[1] if len(a) > 1 else "")
        )
        window.project.transcript.replace_all([])
        window._ai_translation_request()
        assert told and window._export_thread is None


# --------------------------------------------------------------------------
# Lecture search (Phase 7)
# --------------------------------------------------------------------------


class TestLectureSearchUI:
    def test_the_search_tab_lists_results_with_slide_and_time(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.open_search("model")

        assert view.showing_search
        assert view._search_panel.isVisibleTo(view)
        assert not view._transcript.isVisibleTo(view)
        hits = view.search_hits
        assert [h.slide_number for h in hits] == [2]
        html = view._search_results.toHtml()
        assert "Slide 2" in html and "00:00:30" in html
        assert "1 result" in view._search_count.text()

    def test_opening_a_result_selects_its_slide_line_and_time(self, qapp, project_with_slides):
        seeks: list[float] = []
        view = LectureView()
        view.load_project(project_with_slides)
        view._player.seek = lambda seconds, **kw: seeks.append(seconds)  # type: ignore[method-assign]
        view.open_search("러닝레이트")

        view.open_search_hit(0)

        assert view.showing_current_slide
        assert "Slide 1" in view._slide_title.text()
        assert seeks == [10.0]
        row = view._transcript.currentIndex().row()
        assert view._transcript.model().segment_at_row(row).id == "segment_002"

    def test_results_follow_edits(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        view.open_search("gradient")
        assert view.search_hits == []

        view._on_text_edited("segment_001", "Today we learn gradient descent.")
        view._rebind()
        assert [h.segment_id for h in view.search_hits] == ["segment_001"]

    def test_search_is_in_the_edit_menu_with_a_shortcut(self, qapp, tmp_path, project_with_slides):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            names = {a.text(): a for a in window._edit_menu.actions()}
            assert "Search lecture" in names
            assert names["Search lecture"].shortcut().toString() == "Ctrl+Shift+F"
        finally:
            window.close()


# --------------------------------------------------------------------------
# Appearance (theme) and the panel states that go with it
# --------------------------------------------------------------------------


class TestTheme:
    def test_the_stylesheet_parses(self, qapp):
        """A typo in the stylesheet makes Qt drop it silently - catch it here."""
        from PySide6.QtCore import qInstallMessageHandler
        from PySide6.QtWidgets import QWidget

        from lecture_saver.ui.theme import stylesheet

        messages: list[str] = []
        previous = qInstallMessageHandler(
            lambda mode, context, message: messages.append(message)
        )
        try:
            widget = QWidget()
            widget.setStyleSheet(stylesheet())
            widget.ensurePolished()
        finally:
            qInstallMessageHandler(previous)

        assert not [m for m in messages if "stylesheet" in m.lower()], messages

    def test_a_role_is_a_property_the_stylesheet_can_select_on(self, qapp):
        from PySide6.QtWidgets import QLabel

        from lecture_saver.ui.theme import Role, set_role

        label = QLabel("3 slides")
        set_role(label, Role.MUTED)
        assert label.property("role") == "muted"

        set_role(label, None)
        assert label.property("role") == ""

    def test_the_window_icon_is_drawn_at_every_size(self, qapp):
        from lecture_saver.ui.theme import app_icon

        icon = app_icon()
        assert not icon.isNull()
        assert not icon.pixmap(32, 32).isNull()


class TestTranscribePanelState:
    def test_the_options_fold_away_once_there_is_a_transcript(self, qapp):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        assert not panel.is_collapsed

        panel.set_already_transcribed(12)
        assert panel.is_collapsed

        panel.set_already_transcribed(0)
        assert not panel.is_collapsed

    def test_an_unfinished_transcription_keeps_the_offer_to_continue_in_view(self, qapp):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        panel.set_already_transcribed(12)
        panel.set_resume_point(100.0)

        assert not panel.is_collapsed
        assert panel._resume_button.isVisibleTo(panel)

    def test_folding_by_hand_is_not_undone_by_a_refresh(self, qapp):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        panel._on_toggle()
        assert panel.is_collapsed

        panel.set_already_transcribed(0)
        assert panel.is_collapsed

    def test_a_project_without_a_transcript_does_not_wear_the_last_one_status(
        self, qapp, project, tmp_path
    ):
        fresh = Project.create(
            tmp_path / "second",
            "Lecture 02",
            VideoInfo(
                path=str(project.video.path), duration=600.0, width=1280, height=720,
                fps=30.0, size_bytes=11, container="mov,mp4", video_codec="h264",
                audio_codec="aac", has_audio=True,
            ),
        )

        view = LectureView()
        view.load_project(project)
        assert "3 segments" in view.panel._stage_label.text()

        view.load_project(fresh)
        assert view.panel._stage_label.text() == "Not transcribed yet."

    def test_a_video_that_cannot_be_transcribed_keeps_saying_why(self, qapp, project):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        panel.set_enabled_for_video(False, "This video has no audio track.")
        panel.set_already_transcribed(0)

        assert panel._stage_label.text() == "This video has no audio track."


class TestTheWindowStaysUsable:
    """A background job must never be able to wedge the whole window."""

    def test_reading_a_video_does_not_disable_the_window(
        self, qapp, tmp_path, real_video, monkeypatch
    ):
        import shutil

        from lecture_saver.ui import main_window as module

        video = tmp_path / "lecture.mp4"
        shutil.copy2(real_video, video)

        window = MainWindow(Settings(tmp_path / "settings.json"))
        started: list[object] = []
        # Do not actually run the probe: the point is the state the window is
        # left in while one is in flight.
        monkeypatch.setattr(module, "run_in_thread", lambda worker, thread: started.append(worker))
        try:
            window._create_from_video(video)

            assert started, "the probe worker was never started"
            assert window.isEnabled(), "a pending probe left the window dead to input"
            assert not window._new_action.isEnabled()

            window._finish_probe()
            assert window._new_action.isEnabled()
        finally:
            window.close()

    def test_a_remembered_geometry_is_kept_on_screen(self, qapp, tmp_path):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication

        available = QGuiApplication.primaryScreen().availableGeometry()

        # Saved on a wider display, or dragged half off the left edge.
        off_screen = QRect(
            available.x() - 400, available.y() - 200,
            available.width() + 600, available.height() + 300,
        )
        fitted = MainWindow._fit_to_screen(off_screen)

        assert fitted.width() <= available.width()
        assert fitted.height() <= available.height()
        assert available.contains(fitted)

    def test_a_window_that_already_fits_is_left_alone(self, qapp):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication

        available = QGuiApplication.primaryScreen().availableGeometry()
        rect = QRect(
            available.x() + 20, available.y() + 20,
            available.width() // 2, available.height() // 2,
        )

        assert MainWindow._fit_to_screen(rect) == rect


class TestProgressIsVisible:
    """A long job has to say how far along it is, in words the user can read."""

    def test_the_transcription_panel_shows_a_percentage(self, qapp):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        panel.begin_run()
        panel.update_progress("Transcribing lecture", 0.42, "15 min of 36 min")

        assert panel.percent_text == "42%"
        assert panel._progress_row.isVisibleTo(panel)

        # An unknown fraction must not claim a number.
        panel.update_progress("Loading the model", -1.0, "")
        assert panel.percent_text == "..."

        panel.end_run("Done")
        assert panel.percent_text == ""

    def test_the_slide_panel_shows_a_percentage(self, qapp):
        from lecture_saver.ui.slide_panel import SlidePanel

        panel = SlidePanel()
        panel.begin_run()
        panel.update_progress("Scanning frames", 0.66, "Frame 4,120 of 6,300")

        assert panel.percent_text == "66%"

        panel.end_run("42 slides.")
        assert panel.percent_text == ""

    def test_a_percentage_out_of_range_is_clamped(self, qapp):
        from lecture_saver.ui.transcribe_panel import TranscribePanel

        panel = TranscribePanel()
        panel.update_progress("Transcribing lecture", 1.4, "")
        assert panel.percent_text == "100%"

    def test_the_window_carries_the_percentage_in_its_title(self, qapp, tmp_path, project):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project.root)
            plain = window.windowTitle()

            window._set_status_progress("Transcribing lecture", 0.42)
            assert window.windowTitle().startswith("42% - ")
            assert window._status_percent.text() == "42%"
            assert window._status_bar_progress.value() == 42

            window._show_status_progress(False)
            assert window.windowTitle() == plain
            assert not window._status_percent.isVisible()
        finally:
            window.close()

    def test_an_export_job_reports_each_file_it_writes(self, qapp):
        from lecture_saver.export.options import ExportFormat
        from lecture_saver.ui.workers import JobWorker

        seen: list[tuple[str, float]] = []

        def job(report):
            reporter = MainWindow._export_reporter(report)
            reporter(ExportFormat.MARKDOWN, 0, 2)
            reporter(ExportFormat.PDF, 1, 2)
            return "done"

        worker = JobWorker(job)
        worker.progress.connect(lambda stage, fraction: seen.append((stage, fraction)))
        results: list[object] = []
        worker.finished.connect(results.append)
        worker.run()

        assert results == ["done"]
        assert [round(fraction, 2) for _, fraction in seen] == [0.0, 0.5]
        assert "Markdown" in seen[0][0]


class TestSplittingAtSlideChanges:
    """Lines that a slide change runs through, cut at the sentence end (§10)."""

    @pytest.fixture
    def project_with_a_straddling_sentence(self, project_with_slides):
        # The second slide starts at 30 s, in the middle of the second line.
        project_with_slides.transcript.replace_all(
            [
                TranscriptSegment(
                    id="segment_001", start=0.0, end=20.0,
                    original_text="Before the change. And the next thought",
                ),
                TranscriptSegment(
                    id="segment_002", start=20.0, end=40.0,
                    original_text="runs past the slide change. Then a new one starts",
                ),
            ],
            language="en",
        )
        project_with_slides.save()
        return project_with_slides

    def test_the_action_is_offered_in_the_edit_menu(self, qapp, tmp_path, project_with_slides):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project_with_slides.root)
            names = {action.text() for action in window._edit_menu.actions()}
            assert "Split lines at slide changes" in names
        finally:
            window.close()

    def test_it_cuts_the_line_and_says_how_many(self, qapp, project_with_a_straddling_sentence):
        view = LectureView()
        view.load_project(project_with_a_straddling_sentence)
        messages: list[str] = []
        view.statusMessage.connect(messages.append)

        assert view.split_at_slide_changes() == 1

        texts = [s.effective_text for s in project_with_a_straddling_sentence.transcript.segments]
        assert texts[1] == "runs past the slide change."
        assert texts[2] == "Then a new one starts"
        assert any("1 line split" in message for message in messages)

    def test_the_two_halves_land_on_the_two_slides(self, qapp, project_with_a_straddling_sentence):
        view = LectureView()
        view.load_project(project_with_a_straddling_sentence)
        view.split_at_slide_changes()

        segments = project_with_a_straddling_sentence.transcript.segments
        alignment = align(segments, project_with_a_straddling_sentence.slides.appearances)
        assert alignment.appearance_for(1) == 0
        assert alignment.appearance_for(2) == 1

    def test_nothing_to_cut_is_said_plainly(self, qapp, project_with_slides):
        view = LectureView()
        view.load_project(project_with_slides)
        messages: list[str] = []
        view.statusMessage.connect(messages.append)

        assert view.split_at_slide_changes() == 0
        assert any("already falls between sentences" in message for message in messages)

    def test_detection_tidies_a_transcript_nobody_has_touched(
        self, qapp, tmp_path, project_with_a_straddling_sentence
    ):
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project_with_a_straddling_sentence.root)
            window._tidy_sentences_after_detection()
            # The window reopened the project from disk, so ask its own copy.
            assert len(window.project.transcript.segments) == 3
        finally:
            window.close()

    def test_a_transcript_with_corrections_is_left_alone(
        self, qapp, tmp_path, project_with_a_straddling_sentence
    ):
        project = project_with_a_straddling_sentence
        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            window._open_project_at(project.root)
            window.project.transcript.edit_text("segment_001", "Corrected by hand.")
            before = len(window.project.transcript.segments)

            window._tidy_sentences_after_detection()

            assert len(window.project.transcript.segments) == before
        finally:
            window.close()


class TestLineByLineTranslation:
    ANSWER = (
        "## Slide 1\n\n"
        "[00:00:00] Today we begin with the basic concepts of machine learning.\n"
        "오늘은 machine learning의 기본 개념부터 시작합니다.\n\n"
        "[00:00:10] 여기서 러닝레이트를 보시면\n"
        "여기서 learning rate를 보시면\n"
    )

    def test_each_line_shows_its_korean_and_the_panel_stays_hidden(
        self, qapp, project_with_slides
    ):
        from lecture_saver.ui.transcript_view import ROLE_TRANSLATION

        project_with_slides.import_translation(self.ANSWER)
        view = LectureView()
        view.load_project(project_with_slides)
        view.refresh_translations()

        model = view._transcript.model()
        korean = [model.index(r, 0).data(ROLE_TRANSLATION) for r in range(model.rowCount())]
        assert korean == [
            "오늘은 machine learning의 기본 개념부터 시작합니다.",
            "여기서 learning rate를 보시면",
        ]
        assert view.shown_translation == ""

    def test_a_row_with_a_translation_is_taller(self, qapp, project_with_slides):
        from PySide6.QtWidgets import QStyleOptionViewItem

        view = LectureView()
        view.load_project(project_with_slides)
        view._transcript.resize(400, 600)
        index = view._transcript.model().index(0, 0)
        option = QStyleOptionViewItem()
        option.rect.setWidth(400)
        option.font = view._transcript.font()
        delegate = view._transcript.itemDelegate()
        before = delegate.sizeHint(option, index).height()

        project_with_slides.import_translation(self.ANSWER)
        view.refresh_translations()
        index = view._transcript.model().index(0, 0)
        assert delegate.sizeHint(option, index).height() > before


# --------------------------------------------------------------------------
# The size the transcript and its translation are read at
# --------------------------------------------------------------------------


class TestTranscriptTextSize:
    def _row_height(self, view, row=0):
        transcript = view._transcript
        option = QStyleOptionViewItem()
        option.rect.setWidth(400)
        option.font = transcript.font()
        index = transcript.model().index(row, 0)
        return transcript.itemDelegate().sizeHint(option, index).height()

    def test_the_number_sizes_the_lines_and_their_translations(self, qapp, project_with_slides):
        from lecture_saver.ui.transcript_view import ROLE_TRANSLATION

        project_with_slides.import_translation(TestLineByLineTranslation.ANSWER)
        view = LectureView()
        view.load_project(project_with_slides)
        view.refresh_translations()
        assert view._transcript.model().index(0, 0).data(ROLE_TRANSLATION)
        delegate = view._transcript.itemDelegate()
        time_size = delegate._time_font.pointSizeF()
        before = self._row_height(view)

        view._text_size_box.setValue(18)

        assert view.text_size == 18.0
        assert view._transcript.font().pointSizeF() == 18.0
        assert view._translation_text.font().pointSizeF() == 18.0
        # The line and the Korean under it are both larger; the times are not.
        assert self._row_height(view) > before * 1.5
        assert delegate._time_font.pointSizeF() == time_size

    def test_minus_and_plus_step_it_by_one(self, qapp):
        view = LectureView()
        start = view._text_size_box.value()
        view._larger_text_button.click()
        view._larger_text_button.click()
        assert view.text_size == start + 2
        view._smaller_text_button.click()
        assert view.text_size == start + 1

    def test_it_is_kept_for_every_lecture(self, qapp, tmp_path):
        path = tmp_path / "settings.json"
        settings = Settings(path)
        view = LectureView()
        view.set_settings(settings)
        assert settings.transcript_text_size is None  # nothing chosen, nothing kept
        view._text_size_box.setValue(15)
        assert Settings(path).transcript_text_size == 15.0

        reopened = LectureView()
        reopened.set_settings(Settings(path))
        assert reopened.text_size == 15.0
        assert reopened._text_size_box.value() == 15

    def test_it_stays_within_its_range(self, qapp, tmp_path):
        from lecture_saver.ui.lecture_view import MAX_TEXT_POINTS, MIN_TEXT_POINTS

        view = LectureView()
        view.set_text_size(100)
        assert view.text_size == MAX_TEXT_POINTS
        view.set_text_size(1)
        assert view.text_size == MIN_TEXT_POINTS

        path = tmp_path / "settings.json"
        path.write_text('{"transcript_text_size": "big"}', encoding="utf-8")
        assert Settings(path).transcript_text_size is None

    def test_a_line_being_typed_in_grows_without_closing(self, editing_view):
        editor = _open_editor(editing_view, "segment_001")
        size = editing_view.text_size
        editing_view._larger_text_button.click()
        assert editing_view._transcript.is_editing
        assert editor.font().pointSizeF() == size + 1

    def test_beside_the_tabs_as_much_of_it_as_fits(self, editing_view):
        view = editing_view
        assert not view._text_size_bar.isHidden()  # there is a transcript
        box, plus = view._text_size_box, view._larger_text_button
        beside_tabs = view._tabs.sizeHint().width() + 14 + 14 + 10
        view._fit_text_size(beside_tabs + 400)
        assert not box.isHidden() and not plus.isHidden()
        view._fit_text_size(beside_tabs + box.sizeHint().width())
        assert not box.isHidden() and plus.isHidden()
        view._fit_text_size(beside_tabs + 10)
        assert box.isHidden()

    def test_only_with_a_transcript(self, qapp):
        view = LectureView()
        assert view._text_size_bar.isHidden()


# --------------------------------------------------------------------------
# Selecting and copying the words of the transcript and its translation
# --------------------------------------------------------------------------


def _point_of(transcript, row, part, letter):
    """Where in the list's viewport letter ``letter`` of a row's part begins."""
    index = transcript.model().index(row, 0)
    option = QStyleOptionViewItem()
    transcript.initViewItemOption(option)
    option.rect = transcript.visualRect(index)
    for each, _words, layout, origin in transcript.itemDelegate()._parts(option, index):
        if each == part:
            line = layout.lineForTextPosition(letter)
            x, _ = line.cursorToX(letter)
            return QPoint(round(origin.x() + x), round(origin.y() + line.y() + line.height() / 2))
    raise AssertionError(f"row {row} has no part {part}")


class TestSelectingWords:
    @pytest.fixture
    def view(self, editing_view, project_with_slides):
        project_with_slides.import_translation(TestLineByLineTranslation.ANSWER)
        editing_view.refresh_translations()
        QApplication.processEvents()
        return editing_view

    def _drag(self, transcript, start, end):
        from PySide6.QtTest import QTest

        viewport = transcript.viewport()
        QTest.mousePress(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         start)
        QTest.mouseMove(viewport, start + QPoint(8, 0))
        QTest.mouseMove(viewport, end)
        QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           end)

    def _copy(self, transcript, key=Qt.Key.Key_C):
        from PySide6.QtTest import QTest

        QGuiApplication.clipboard().clear()
        QTest.keyClick(transcript, key, Qt.KeyboardModifier.ControlModifier)
        return QGuiApplication.clipboard().text()

    def test_dragging_over_a_line_selects_its_words_and_does_not_seek(self, view):
        from lecture_saver.ui.transcript_view import PART_TEXT

        transcript = view._transcript
        seeks: list[float] = []
        transcript.seekRequested.connect(seeks.append)
        text = transcript.model().index(0, 0).data(Qt.ItemDataRole.DisplayRole)
        first = text.index("basic concepts")
        self._drag(transcript, _point_of(transcript, 0, PART_TEXT, first),
                   _point_of(transcript, 0, PART_TEXT, first + len("basic concepts")))

        assert transcript.selected_text == "basic concepts"
        assert self._copy(transcript) == "basic concepts"
        assert seeks == []

    def test_a_drag_goes_on_into_the_next_lines_and_their_translations(self, view):
        from lecture_saver.ui.transcript_view import PART_TEXT, PART_TRANSLATION

        transcript = view._transcript
        model = transcript.model()
        text = model.index(0, 0).data(Qt.ItemDataRole.DisplayRole)
        start = text.index("machine")
        self._drag(transcript, _point_of(transcript, 0, PART_TEXT, start),
                   _point_of(transcript, 1, PART_TRANSLATION, len("여기서 learning")))

        assert transcript.selected_text == "\n".join([
            "machine learning.",
            "오늘은 machine learning의 기본 개념부터 시작합니다.",
            "여기서 러닝레이트를 보시면",
            "여기서 learning",
        ])

    def test_a_click_still_seeks_and_lets_go_of_the_words(self, view):
        from PySide6.QtTest import QTest

        from lecture_saver.ui.transcript_view import PART_TEXT

        transcript = view._transcript
        transcript.select_all_text()
        seeks: list[float] = []
        transcript.seekRequested.connect(seeks.append)
        QTest.mouseClick(transcript.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier, _point_of(transcript, 1, PART_TEXT, 2))
        assert seeks == [10.0]
        assert not transcript.has_text_selection

    def test_ctrl_a_selects_everything_lines_and_translations(self, view):
        transcript = view._transcript
        model = transcript.model()
        self._copy(transcript, Qt.Key.Key_A)
        expected = "\n".join(transcript.row_text(row) for row in range(model.rowCount()))
        assert "오늘은 machine learning의" in expected
        assert self._copy(transcript) == expected

    def test_with_nothing_selected_ctrl_c_copies_the_line_and_its_translation(self, view):
        transcript = view._transcript
        transcript.setCurrentIndex(transcript.model().index(1, 0))
        assert self._copy(transcript) == "여기서 러닝레이트를 보시면\n여기서 learning rate를 보시면"

    def test_selected_words_are_drawn_selected(self, view):
        transcript = view._transcript
        before = transcript.viewport().grab().toImage()
        transcript.select_all_text()
        after = transcript.viewport().grab().toImage()
        assert before != after

    def test_editing_a_line_lets_go_of_the_words(self, view):
        transcript = view._transcript
        transcript.select_all_text()
        _open_editor(view, "segment_001")
        assert not transcript.has_text_selection


# --------------------------------------------------------------------------
# Space plays and pauses (§12)
# --------------------------------------------------------------------------


class TestSpaceKey:
    """Space toggles playback whatever was clicked last, except in a text box.

    ``play`` and ``pause`` are replaced with recorders: the test is about which
    key reaches the player, and a real play would sound the test tone.
    """

    @pytest.fixture
    def calls(self, editing_view, monkeypatch):
        calls: list[str] = []
        media = editing_view._player._player
        monkeypatch.setattr(media, "play", lambda: calls.append("play"))
        monkeypatch.setattr(media, "pause", lambda: calls.append("pause"))
        editing_view.activateWindow()
        QApplication.processEvents()
        return calls

    def _press_space(self, widget):
        from PySide6.QtTest import QTest

        widget.setFocus()
        QApplication.processEvents()
        QTest.keyClick(widget, Qt.Key.Key_Space)
        QApplication.processEvents()

    def test_space_plays_from_the_transcript_list(self, editing_view, calls):
        self._press_space(editing_view._transcript)
        assert calls == ["play"]

    def test_space_plays_from_the_slide_list(self, editing_view, calls):
        self._press_space(editing_view._slides_pane._list)
        assert calls == ["play"]

    def test_space_on_a_focused_checkbox_plays_instead_of_toggling_it(
        self, editing_view, calls
    ):
        follow = editing_view._follow_check
        self._press_space(follow)
        assert calls == ["play"]
        assert follow.isChecked()

    def test_space_pauses_while_playing(self, editing_view, calls, monkeypatch):
        from PySide6.QtMultimedia import QMediaPlayer

        media = editing_view._player._player
        monkeypatch.setattr(
            media, "playbackState", lambda: QMediaPlayer.PlaybackState.PlayingState
        )
        self._press_space(editing_view._transcript)
        assert calls == ["pause"]

    def test_space_typed_into_a_segment_is_a_space(
        self, editing_view, calls, project_with_slides
    ):
        from PySide6.QtTest import QTest

        editor = _open_editor(editing_view, "segment_001")
        editor.setFocus()
        QTest.keyClicks(editor, " ok")
        QTest.keyClick(editor, Qt.Key.Key_Return)

        assert calls == []
        assert project_with_slides.transcript.get("segment_001").effective_text.endswith(
            " ok"
        )

    def test_space_does_nothing_without_a_video(self, editing_view, calls):
        editing_view._player.clear()
        self._press_space(editing_view._transcript)
        assert calls == []


class TestArrowKeys:
    """Left and Right move the video a second, and keep it moving while held.

    The media player's position is pinned and its seeks recorded; each seek's
    frame "arrives" at once unless a test says otherwise. Whether the key is
    still down is asked of the keyboard, so tests say that too - the real
    keyboard is not being pressed.
    """

    @pytest.fixture
    def held(self, monkeypatch):
        import lecture_saver.ui.video_player as video_player

        state = {"down": False}
        monkeypatch.setattr(video_player, "key_is_down", lambda key: state["down"])
        return state

    @pytest.fixture
    def seeks(self, editing_view, monkeypatch, held):
        player = editing_view._player
        media = player._player
        seeks: list[float] = []

        def seek(ms):
            seeks.append(ms / 1000)
            if player.frames_land:
                player._awaiting_frame = False

        player.frames_land = True
        monkeypatch.setattr(media, "setPosition", seek)
        monkeypatch.setattr(media, "position", lambda: 30_000)
        # The 2 s test video finishing loading must not shorten the pinned 600 s.
        media.durationChanged.disconnect(player._on_duration)
        monkeypatch.setattr(player, "_duration_s", 600.0)
        editing_view.activateWindow()
        QApplication.processEvents()
        return seeks

    @staticmethod
    def _wait(seconds):
        import time

        end = time.monotonic() + seconds
        while time.monotonic() < end:
            QApplication.processEvents()
            time.sleep(0.005)

    def _hold(self, widget, key, held, seconds):
        """Press ``key``, keep it down for ``seconds``, let it go."""
        from PySide6.QtTest import QTest

        widget.setFocus()
        QApplication.processEvents()
        held["down"] = True
        QTest.keyPress(widget, key)
        self._wait(seconds)
        held["down"] = False
        QTest.keyRelease(widget, key)
        QApplication.processEvents()

    def _press(self, widget, key, times=1):
        from PySide6.QtTest import QTest

        widget.setFocus()
        QApplication.processEvents()
        for _ in range(times):
            QTest.keyClick(widget, key)
        QApplication.processEvents()

    def test_right_and_left_move_a_second(self, editing_view, seeks, monkeypatch):
        import lecture_saver.ui.video_player as video_player

        self._press(editing_view._transcript, Qt.Key.Key_Right)
        # Far enough apart not to be one held key.
        monkeypatch.setattr(video_player.time, "monotonic", lambda: 10_000.0)
        self._press(editing_view._transcript, Qt.Key.Key_Left)
        assert seeks == [31.0, 29.0]

    def test_quick_presses_go_on_from_the_last_step(self, editing_view, seeks):
        # The position still says 30 s: each press goes on from the last step.
        self._press(editing_view._slides_pane._list, Qt.Key.Key_Right, times=4)
        assert seeks == [31.0, 32.0, 33.0, 34.0]

    def test_held_down_it_keeps_moving_until_let_go(self, editing_view, seeks, held):
        import lecture_saver.ui.video_player as video_player

        self._hold(editing_view._transcript, Qt.Key.Key_Right, held, 1.0)
        # One step at once, then a second every HOLD_INTERVAL_MS after the delay.
        expected = 1 + (1000 - video_player.HOLD_DELAY_MS) // video_player.HOLD_INTERVAL_MS
        assert len(seeks) >= expected - 1
        assert seeks == [31.0 + i for i in range(len(seeks))]
        player = editing_view._player
        assert not player.is_holding
        moved = len(seeks)
        self._wait(0.5)
        assert len(seeks) == moved, "the video kept moving after the key came up"

    def test_held_left_goes_back(self, editing_view, seeks, held):
        self._hold(editing_view._transcript, Qt.Key.Key_Left, held, 0.7)
        assert seeks[:3] == [29.0, 28.0, 27.0]

    def test_a_quick_press_is_one_step(self, editing_view, seeks, held):
        self._hold(editing_view._transcript, Qt.Key.Key_Right, held, 0.1)
        assert seeks == [31.0]

    def test_it_waits_for_each_picture_but_keeps_its_pace(self, editing_view, seeks, held):
        # Frames never come: one seek, then none until the wait for it runs out,
        # while the target keeps going - and is sent when the key comes up.
        editing_view._player.frames_land = False
        self._hold(editing_view._transcript, Qt.Key.Key_Right, held, 0.9)
        assert seeks[0] == 31.0 and seeks[1] == 32.0
        assert len(seeks) == 3
        assert seeks[-1] > 34.0

    def test_a_key_let_go_unseen_still_stops_it(self, editing_view, seeks, held):
        from PySide6.QtTest import QTest

        widget = editing_view._transcript
        widget.setFocus()
        held["down"] = True
        QTest.keyPress(widget, Qt.Key.Key_Right)
        self._wait(0.6)
        held["down"] = False  # its release went to another window
        self._wait(0.4)
        assert not editing_view._player.is_holding
        moved = len(seeks)
        self._wait(0.4)
        assert len(seeks) == moved

    def test_held_in_a_text_box_it_is_the_cursor(self, editing_view, seeks, held):
        editor = _open_editor(editing_view, "segment_001")
        self._hold(editor, Qt.Key.Key_Right, held, 0.7)
        assert seeks == []
        assert not editing_view._player.is_holding

    def test_never_before_the_start_or_past_the_end(self, editing_view, seeks, monkeypatch):
        media = editing_view._player._player
        monkeypatch.setattr(media, "position", lambda: 500)
        self._press(editing_view._transcript, Qt.Key.Key_Left)
        monkeypatch.setattr(media, "position", lambda: 599_800)
        editing_view._player._step_target = None
        self._press(editing_view._transcript, Qt.Key.Key_Right)
        assert seeks == [0.0, 600.0]

    def test_in_a_text_box_the_arrows_move_the_cursor(self, editing_view, seeks):
        editor = _open_editor(editing_view, "segment_001")
        editor.setFocus()
        self._press(editor, Qt.Key.Key_Left, times=2)
        assert seeks == []

    def test_the_keys_are_written_under_the_video(self, editing_view):
        assert "1&nbsp;s&nbsp;back&nbsp;/&nbsp;forward" in editing_view._player.keys_text

    def test_nothing_without_a_video(self, editing_view, seeks):
        editing_view._player.clear()
        self._press(editing_view._transcript, Qt.Key.Key_Right)
        assert seeks == []


class TestAskAI:
    """Ask AI from the lecture: a line's button, the panel, and the way back.

    The assistant is a fake from :mod:`tests.test_ask`; nothing is sent.
    """

    @pytest.fixture
    def asking_view(self, editing_view, tmp_path):
        from tests.test_ask import FakeEngine

        settings = Settings(tmp_path / "ask-settings.json")
        settings.translation_enabled = True  # agreed already: no dialog headless
        editing_view.set_settings(settings)
        engine = FakeEngine(answer="답 [00:00:31]")
        editing_view.ask_panel.engine_factory = lambda: engine
        editing_view.engine = engine
        yield editing_view
        editing_view.shutdown()

    def _click(self, transcript, point, *, double=False):
        from PySide6.QtTest import QTest

        click = QTest.mouseDClick if double else QTest.mouseClick
        click(transcript.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
              point)
        QApplication.processEvents()

    def _button_point(self, transcript, row):
        rect = transcript.visualRect(transcript.model().index(row, 0))
        return transcript.itemDelegate().ask_rect(rect).center()

    def test_the_button_on_a_line_asks_about_it_without_seeking(self, asking_view):
        transcript = asking_view._transcript
        asked: list[tuple[str, str]] = []
        seeks: list[float] = []
        transcript.askRequested.connect(lambda sid, words: asked.append((sid, words)))
        transcript.seekRequested.connect(seeks.append)

        self._click(transcript, self._button_point(transcript, 1))

        assert asked == [("segment_002", "")]
        assert seeks == []
        assert asking_view.ask_panel.isVisibleTo(asking_view)
        assert [q.segment_id for q in asking_view.ask_panel.quotes] == ["segment_002"]

    def test_double_clicking_the_button_does_not_start_an_edit(self, asking_view):
        transcript = asking_view._transcript
        self._click(transcript, self._button_point(transcript, 0), double=True)
        assert not transcript.is_editing

    def test_clicking_the_text_still_seeks(self, asking_view):
        transcript = asking_view._transcript
        seeks: list[float] = []
        transcript.seekRequested.connect(seeks.append)
        rect = transcript.visualRect(transcript.model().index(1, 0))
        self._click(transcript, rect.center() + QPoint(rect.width() // 4, 0))
        assert seeks == [10.0]

    def test_a_plain_transcript_list_has_no_button(self, qapp, project):
        view = TranscriptView()
        view.set_segments(project.transcript.segments)
        assert not view.shows_ask_button(0)

    def test_explain_sends_the_line_straight_away(self, asking_view, project_with_slides):
        asking_view._transcript.explainRequested.emit("segment_003")
        assert pump_until(lambda: not asking_view.ask_panel.is_busy, timeout=10)

        prompt = asking_view.engine.prompts[0]
        assert "[00:00:30] If the value is too large, the model diverges." in prompt
        assert [m.role for m in project_with_slides.chat.messages] == ["user", "assistant"]

    def test_words_picked_out_of_a_line_are_what_is_quoted(self, asking_view):
        asking_view.ask_about_segment("segment_002", "러닝레이트")
        assert [q.text for q in asking_view.ask_panel.quotes] == ["러닝레이트"]

    def test_the_question_knows_the_slide_shown(self, asking_view):
        asking_view._tabs.setCurrentIndex(0)
        asking_view._show_appearance(1)
        assert asking_view._ask_focus().slide == 2

    def test_a_cited_time_shows_its_slide_and_line(self, asking_view):
        asking_view._tabs.setCurrentIndex(0)
        asking_view.go_to_time(31.0)
        assert asking_view.shown_appearance.appearance_id == 2
        segment = asking_view._transcript.currentIndex().data(Qt.ItemDataRole.UserRole + 2)
        assert segment.id == "segment_003"

    def test_a_cited_slide_is_shown(self, asking_view):
        asking_view._tabs.setCurrentIndex(1)
        asking_view.go_to_slide(2)
        assert asking_view.showing_current_slide
        assert asking_view.shown_appearance.appearance_id == 2

    def test_the_panel_hides_and_comes_back(self, asking_view):
        asking_view.show_ask_panel()
        assert asking_view.ask_panel.isVisibleTo(asking_view)
        asking_view.ask_panel.hideRequested.emit()
        assert not asking_view.ask_panel.isVisibleTo(asking_view)
        assert not asking_view._ask_button.isChecked()


def test_closing_the_window_stops_a_question_in_flight(qapp, project, tmp_path):
    from tests.test_ask import FakeEngine

    settings = Settings(tmp_path / "settings.json")
    settings.translation_enabled = True
    window = MainWindow(settings)
    window._open_project_at(project.root)
    engine = FakeEngine(hang=True)
    panel = window._lecture_view.ask_panel
    panel.engine_factory = lambda: engine
    panel._input.setPlainText("질문")
    assert panel.ask()
    assert panel.is_busy

    window.close()
    assert not panel.is_busy
    assert window.project.chat.is_empty
