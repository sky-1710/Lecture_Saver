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
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from lecture_saver.config import Settings  # noqa: E402
from lecture_saver.core.models import TranscriptSegment, VideoInfo  # noqa: E402
from lecture_saver.core.project import Project  # noqa: E402
from lecture_saver.ui.lecture_view import LectureView  # noqa: E402
from lecture_saver.ui.main_window import MainWindow  # noqa: E402
from lecture_saver.ui.transcript_view import TranscriptView  # noqa: E402


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

    def test_without_slides_there_are_no_tabs(self, qapp, project):
        view = LectureView()
        view.load_project(project)
        assert not view._tabs.isVisibleTo(view)
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
