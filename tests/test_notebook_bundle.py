"""The folder for Gemini Notebook, and the copy of the PDF it needs (§14, §29).

Nothing here goes near the site: the folder is written and looked at, and that
is all the application does. The user uploads it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lecture_saver.core.models import TranscriptSegment, VideoInfo
from lecture_saver.core.project import REFERENCE_PDF_NAME, Project
from lecture_saver.core.reference import ReferenceText
from lecture_saver.core.slides import Slide, SlideAppearance, SlideSet
from lecture_saver.core.study import StudyGuide, StudySet
from lecture_saver.export.document import build_document
from lecture_saver.export.notebook_bundle import (
    REQUEST_FILE,
    bundle_folder_name,
    video_request,
    write_bundle,
)
from lecture_saver.export.pdf import find_korean_font

needs_korean_font = pytest.mark.skipif(
    find_korean_font() is None, reason="no TrueType font with Korean glyphs installed"
)

PREVIEW = "## PART 1. 큰 그림\n\n" + "어려운 개념을 여러 단계로 설명합니다. " * 60


@pytest.fixture
def project(tmp_path):
    video = tmp_path / "lecture.mp4"
    video.write_bytes(b"x")
    info = VideoInfo(path=str(video), duration=40.0, has_audio=True)
    project = Project.create(tmp_path / "projects", "OS03 Process", info)
    project.transcript.replace_all(
        [
            TranscriptSegment(id="segment_001", start=4.0, end=9.0,
                              original_text="Each process has a state.",
                              edited_text="Each process has a process state."),
        ],
        language="en",
    )
    project.save()
    project.set_slides(SlideSet(
        slides=[Slide(1, "slides/001.jpg")], appearances=[SlideAppearance(1, 1, 0.0, 40.0)],
    ))
    project.save_slides()
    return project


def _study():
    study = StudySet()
    study.set_guide(StudyGuide.from_answer(PREVIEW), source="x")
    return study


def _deck(tmp_path):
    deck = tmp_path / "somewhere" / "OS03-Process (1).pdf"
    deck.parent.mkdir()
    deck.write_bytes(b"%PDF the deck")
    return deck


def _names(folder: Path) -> set[str]:
    return {path.name for path in folder.iterdir()}


class TestTheWordsToPaste:
    def test_they_ask_for_a_korean_lecture_before_the_professors(self):
        words = video_request(has_deck=True, has_preview=True)
        assert "강의를 듣기 전에 보는 예습 강의" in words
        assert "모든 설명은 한국어로" in words

    def test_they_name_only_what_is_in_the_folder(self):
        full = video_request(has_deck=True, has_preview=True)
        assert "교안을 기준으로" in full and "예습 자료 PDF" in full
        bare = video_request(has_deck=False, has_preview=False)
        assert "교안" not in bare and "예습 자료" not in bare

    def test_the_ai_is_not_the_professor(self):
        assert "[AI 보충 설명]" in video_request(has_deck=True, has_preview=True)
        assert "교수님이 말하지 않은 내용" in video_request(has_deck=False, has_preview=False)


@needs_korean_font
class TestTheFolder:
    def test_everything_is_in_one_folder_named_after_the_lecture(self, project, tmp_path):
        out = tmp_path / "out"
        bundle = write_bundle(build_document(project), _study(), _deck(tmp_path), out)
        assert bundle.folder == out / "OS03_Process_gemini_notebook"
        assert _names(bundle.folder) == {
            "OS03_Process_교안.pdf", "OS03_Process_전사문.txt", "OS03_Process_예습.pdf",
            REQUEST_FILE,
        }

    def test_the_transcript_is_the_edited_one_with_its_times(self, project, tmp_path):
        bundle = write_bundle(build_document(project), StudySet(), None, tmp_path)
        text = (bundle.folder / "OS03_Process_전사문.txt").read_text(encoding="utf-8")
        assert "Each process has a process state." in text
        assert "Each process has a state." not in text
        assert "00:00:04" in text

    def test_the_pdf_is_a_copy_of_the_one_given(self, project, tmp_path):
        deck = _deck(tmp_path)
        bundle = write_bundle(build_document(project), StudySet(), deck, tmp_path)
        assert (bundle.folder / "OS03_Process_교안.pdf").read_bytes() == deck.read_bytes()
        assert deck.is_file()

    def test_the_words_on_disk_are_the_words_handed_back(self, project, tmp_path):
        bundle = write_bundle(build_document(project), _study(), _deck(tmp_path), tmp_path)
        assert (bundle.folder / REQUEST_FILE).read_text(encoding="utf-8") == bundle.request

    def test_what_is_missing_is_left_out(self, project, tmp_path):
        bundle = write_bundle(build_document(project), StudySet(), None, tmp_path)
        assert _names(bundle.folder) == {"OS03_Process_전사문.txt", REQUEST_FILE}
        assert "교안" not in bundle.request

    def test_making_it_again_leaves_no_old_copy(self, project, tmp_path):
        document = build_document(project)
        write_bundle(document, _study(), _deck(tmp_path), tmp_path / "out")
        bundle = write_bundle(document, StudySet(), None, tmp_path / "out")
        assert _names(bundle.folder) == {"OS03_Process_전사문.txt", REQUEST_FILE}

    def test_making_it_again_leaves_the_users_own_files(self, project, tmp_path):
        document = build_document(project)
        bundle = write_bundle(document, StudySet(), None, tmp_path)
        (bundle.folder / "내 메모.txt").write_text("메모", encoding="utf-8")
        write_bundle(document, StudySet(), None, tmp_path)
        assert (bundle.folder / "내 메모.txt").is_file()

    def test_an_awkward_lecture_name_still_makes_a_folder(self):
        assert bundle_folder_name('OS: 03 "Process"') == "OS_03_Process_gemini_notebook"


class TestTheKeptCopy:
    def _pictures(self, tmp_path, *, copy=True):
        folder = tmp_path / "drawn"
        folder.mkdir()
        (folder / "p001.jpg").write_bytes(b"one")
        if copy:
            (folder / REFERENCE_PDF_NAME).write_bytes(b"%PDF the deck")
        return folder

    def test_it_is_kept_with_the_lecture(self, project, tmp_path):
        project.set_reference(ReferenceText("a.pdf", ("x",)), pictures=self._pictures(tmp_path))
        assert Project.open(project.root).reference_pdf.read_bytes() == b"%PDF the deck"

    def test_it_is_not_taken_for_a_page(self, project, tmp_path):
        project.set_reference(ReferenceText("a.pdf", ("x",)), pictures=self._pictures(tmp_path))
        assert [p.name for p in project.reference_pictures] == ["p001.jpg"]

    def test_with_no_copy_it_says_so(self, project, tmp_path):
        assert project.reference_pdf is None
        project.set_reference(
            ReferenceText("a.pdf", ("x",)), pictures=self._pictures(tmp_path, copy=False)
        )
        assert project.reference_pdf is None

    def test_removing_the_pdf_removes_it(self, project, tmp_path):
        project.set_reference(ReferenceText("a.pdf", ("x",)), pictures=self._pictures(tmp_path))
        project.clear_reference()
        assert project.reference_pdf is None

    def test_a_textbook_is_not_copied(self, qapp, project, tmp_path, monkeypatch):
        from PySide6.QtCore import QCoreApplication, QElapsedTimer

        from lecture_saver.config import Settings
        from lecture_saver.ui import reference_row
        from lecture_saver.ui.reference_row import ReferenceRow

        from .test_corrections import TestReadingAPdf

        monkeypatch.setattr(reference_row, "MAX_KEPT_PDF_BYTES", 10)
        row = ReferenceRow(project, Settings(tmp_path / "s.json"), "none", "tip")
        row.read(TestReadingAPdf()._pdf(tmp_path / "deck.pdf", [["words"]]))
        timer = QElapsedTimer()
        timer.start()
        while row.is_reading and timer.elapsed() < 10000:
            QCoreApplication.processEvents()
        assert project.reference_pictures and project.reference_pdf is None

    def test_attaching_a_real_pdf_keeps_a_copy(self, qapp, project, tmp_path):
        from PySide6.QtCore import QCoreApplication, QElapsedTimer

        from lecture_saver.config import Settings
        from lecture_saver.ui.reference_row import ReferenceRow

        from .test_corrections import TestReadingAPdf

        original = TestReadingAPdf()._pdf(tmp_path / "deck.pdf", [["words"]])
        row = ReferenceRow(project, Settings(tmp_path / "s.json"), "none", "tip")
        row.read(original)
        timer = QElapsedTimer()
        timer.start()
        while row.is_reading and timer.elapsed() < 10000:
            QCoreApplication.processEvents()
        assert project.reference_pdf.read_bytes() == original.read_bytes()


class TestTheDialog:
    def _dialog(self, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.ui.bundle_dialog import BundleDialog

        return BundleDialog(
            project, Settings(tmp_path / "settings.json"), build_document(project),
            tmp_path / "out",
        )

    def _attach(self, project, tmp_path):
        folder = tmp_path / "drawn"
        folder.mkdir()
        (folder / REFERENCE_PDF_NAME).write_bytes(b"%PDF the deck")
        project.set_reference(ReferenceText("OS03.pdf", ("x",)), pictures=folder)

    def test_it_says_what_is_missing(self, qapp, project, tmp_path):
        dialog = self._dialog(project, tmp_path)
        text = dialog._contents.text()
        assert "교안 PDF: attach it above" in text and "예습 PDF: not made yet" in text
        assert "✓ 전사문" in text
        assert dialog._start_button.isEnabled()

    def test_it_says_what_is_there(self, qapp, project, tmp_path):
        self._attach(project, tmp_path)
        project.study = _study()
        dialog = self._dialog(project, tmp_path)
        assert "✓ 교안 PDF (OS03.pdf)" in dialog._contents.text()
        assert "✓ 예습 PDF" in dialog._contents.text()

    def test_a_pdf_attached_with_no_copy_is_asked_for_again(self, qapp, project, tmp_path):
        project.set_reference(ReferenceText("OS03.pdf", ("x",)))
        dialog = self._dialog(project, tmp_path)
        assert "no copy was kept" in dialog._contents.text()

    def test_it_says_where_it_goes(self, qapp, project, tmp_path):
        dialog = self._dialog(project, tmp_path)
        assert dialog.folder == tmp_path / "out" / "OS03_Process_gemini_notebook"
        assert str(dialog.folder) in dialog._output_label.text()

    @needs_korean_font
    def test_a_whole_run_on_its_real_thread(self, qapp, project, tmp_path, monkeypatch):
        from PySide6.QtCore import QCoreApplication, QElapsedTimer
        from PySide6.QtWidgets import QMessageBox

        self._attach(project, tmp_path)
        project.study = _study()
        shown: list[str] = []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
        dialog = self._dialog(project, tmp_path)
        dialog._start()
        assert dialog.is_running and not dialog._close_button.isEnabled()
        timer = QElapsedTimer()
        timer.start()
        while not shown and timer.elapsed() < 10000:
            QCoreApplication.processEvents()
        assert shown and str(dialog.folder) in shown[0]
        assert len(list(dialog.folder.iterdir())) == 4
        assert not dialog.is_running and dialog._close_button.isEnabled()

    def test_a_failure_is_said(self, qapp, project, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        warned: list[str] = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
        dialog = self._dialog(project, tmp_path)
        dialog._on_finished(OSError("no room"))
        assert warned == ["no room"]

    def test_the_buttons_keep_the_box_open_for_the_next(self, qapp, project, tmp_path, monkeypatch):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QApplication, QMessageBox

        from lecture_saver.export.notebook_bundle import Bundle
        from lecture_saver.ui.bundle_dialog import NOTEBOOK_URL

        opened: list[str] = []
        monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))

        def press_each(box):
            for button in box.buttons():
                if button.text() != "Close":
                    button.click()

        monkeypatch.setattr(QMessageBox, "exec", press_each)
        dialog = self._dialog(project, tmp_path)
        dialog.show_result(Bundle(tmp_path / "folder", "붙여넣을 말"))
        assert QApplication.clipboard().text() == "붙여넣을 말"
        assert NOTEBOOK_URL in opened and len(opened) == 2


    def test_a_real_box_stays_open_after_a_button_and_closes_with_close(
        self, qapp, project, tmp_path, monkeypatch
    ):
        from PySide6.QtCore import QTimer
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QApplication

        from lecture_saver.export.notebook_bundle import Bundle

        monkeypatch.setattr(QDesktopServices, "openUrl", lambda _url: True)
        seen: list[bool] = []

        def press():
            box = QApplication.activeModalWidget()
            buttons = {button.text(): button for button in box.buttons()}
            buttons["Copy the text to paste"].click()
            buttons["Open folder"].click()
            QApplication.processEvents()
            seen.append(box.isVisible())
            buttons["Close"].click()

        dialog = self._dialog(project, tmp_path)
        QTimer.singleShot(50, press)
        dialog.show_result(Bundle(tmp_path / "folder", "붙여넣을 말"))
        assert seen == [True]


class TestTheButton:
    @pytest.fixture
    def window(self, qapp, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        window._open_project_at(project.root)
        yield window
        window.close()

    def test_it_is_there_with_a_lecture_open(self, qapp, project, tmp_path):
        from lecture_saver.config import Settings
        from lecture_saver.ui.main_window import MainWindow

        window = MainWindow(Settings(tmp_path / "settings.json"))
        try:
            assert not window._bundle_action.isEnabled()
            window._open_project_at(project.root)
            assert window._bundle_action.isEnabled()
        finally:
            window.close()

    def test_it_waits_for_what_comes_first(self, window, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        import lecture_saver.ui.bundle_dialog as bundle_dialog

        shown, opened = [], []
        monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
        monkeypatch.setattr(bundle_dialog.BundleDialog, "exec", lambda self: opened.append(1))
        window._lecture_view._bundle_action.trigger()
        assert "1 of 1 slides have no translation yet" in shown[0]
        assert not opened

    def test_a_translated_lecture_opens_it(self, window, monkeypatch):
        import lecture_saver.ui.bundle_dialog as bundle_dialog

        opened = []
        monkeypatch.setattr(bundle_dialog.BundleDialog, "exec", lambda self: opened.append(1))
        window.project.import_translation("## Slide 1\n하나")
        window._lecture_view._bundle_action.trigger()
        assert opened == [1]
