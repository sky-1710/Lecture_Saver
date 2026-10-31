"""Drawing over the video, screenshots, and the screenshot panel - headless.

The canvas is driven with real mouse and key events; frames are made from
pictures rather than decoded, so no media pipeline is involved except where
a whole lecture view is opened.
"""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtMultimedia import QVideoFrame  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from lecture_saver.core.captures import HIGHLIGHT, MEMO, PEN, UNDERLINE, Mark  # noqa: E402
from lecture_saver.core.models import VideoInfo  # noqa: E402
from lecture_saver.core.project import Project  # noqa: E402
from lecture_saver.ui import ink  # noqa: E402
from lecture_saver.ui.capture_panel import CaptureCanvas, CapturePanel  # noqa: E402
from lecture_saver.ui.video_canvas import FrameRenderer, render_frame  # noqa: E402
from lecture_saver.ui.video_player import FrameShot, VideoPlayer  # noqa: E402


def pump_until(predicate, timeout: float = 10.0) -> bool:
    app = QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def picture(width: int = 1600, height: int = 900, colour: str = "white") -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(colour))
    return image


def frame_at(seconds: float, colour: str = "white") -> QVideoFrame:
    frame = QVideoFrame(picture(640, 360, colour))
    frame.setStartTime(int(seconds * 1_000_000))
    return frame


def show(player, frame: QVideoFrame) -> None:
    """Hand ``player`` a frame the way the media backend does: into its sink."""
    player.video_sink.setVideoFrame(frame)


def watching_keys(player) -> str:
    from lecture_saver.ui.theme import key_hints
    from lecture_saver.ui.video_player import WATCHING_KEYS

    return key_hints(WATCHING_KEYS)


def drag(widget, start: QPoint, end: QPoint, button=Qt.MouseButton.LeftButton) -> None:
    QTest.mousePress(widget, button, Qt.KeyboardModifier.NoModifier, start)
    QTest.mouseMove(widget, end)
    QTest.mouseRelease(widget, button, Qt.KeyboardModifier.NoModifier, end)


@pytest.fixture
def canvas(qapp):
    """A 1600x900 picture shown at exactly half size: pixel (x, y) is (x/800, y/450)."""
    view = CaptureCanvas()
    view.resize(800, 450)
    view.show_capture(picture(), ())
    view.show()
    QApplication.processEvents()
    assert view.picture_rect() == QRectF(0, 0, 800, 450)
    return view


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------


class TestDrawing:
    def test_the_free_pen_is_the_tool_to_start_with(self, canvas):
        assert canvas.tool == ink.TOOL_PEN

    def test_the_free_pen_follows_the_mouse(self, canvas):
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(100, 225))
        for x, y in ((150, 180), (200, 160), (250, 180), (300, 225)):
            QTest.mouseMove(canvas, QPoint(x, y))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(300, 225))
        (mark,) = canvas.marks
        assert mark.kind == PEN
        assert mark.points[0] == pytest.approx((0.125, 0.5))
        assert mark.points[-1] == pytest.approx((0.375, 0.5))
        assert len(mark.points) >= 3  # the arch is kept, not flattened
        assert mark.y1 == pytest.approx(160 / 450)

    def test_a_straight_pen_stroke_keeps_only_its_ends(self, canvas):
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(100, 100))
        for x in range(105, 500, 5):
            QTest.mouseMove(canvas, QPoint(x, 100))
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(500, 100))
        (mark,) = canvas.marks
        assert len(mark.points) == 2

    def test_a_pen_click_is_not_a_mark(self, canvas):
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(300, 300))
        assert canvas.marks == ()

    def test_esc_drops_the_stroke_being_drawn(self, canvas):
        canvas.setFocus()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(100, 100))
        QTest.mouseMove(canvas, QPoint(300, 200))
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                           QPoint(300, 200))
        assert canvas.marks == ()

    def test_the_eraser_removes_a_pen_stroke(self, canvas):
        canvas.set_marks([Mark.pen([(0.1, 0.1), (0.3, 0.5), (0.5, 0.1)])])
        canvas.set_tool(ink.TOOL_ERASER)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(160, 135))
        assert canvas.marks == ()

    def test_an_underline_is_placed_in_fractions_of_the_picture(self, canvas):
        canvas.set_tool(ink.TOOL_UNDERLINE)
        drag(canvas, QPoint(200, 225), QPoint(600, 225))
        (mark,) = canvas.marks
        assert mark.kind == UNDERLINE
        assert (mark.x1, mark.y1, mark.x2, mark.y2) == pytest.approx((0.25, 0.5, 0.75, 0.5))

    def test_a_nearly_level_underline_is_drawn_level(self, canvas):
        canvas.set_tool(ink.TOOL_UNDERLINE)
        drag(canvas, QPoint(200, 220), QPoint(600, 240))
        (mark,) = canvas.marks
        assert mark.y2 == mark.y1

    def test_a_steep_line_keeps_its_angle(self, canvas):
        canvas.set_tool(ink.TOOL_UNDERLINE)
        drag(canvas, QPoint(200, 100), QPoint(300, 300))
        (mark,) = canvas.marks
        assert mark.y2 > mark.y1

    def test_a_click_is_not_a_mark(self, canvas):
        canvas.set_tool(ink.TOOL_UNDERLINE)
        drag(canvas, QPoint(200, 200), QPoint(202, 201))
        assert canvas.marks == ()

    def test_a_highlight_is_the_box_dragged(self, canvas):
        canvas.set_tool(ink.TOOL_HIGHLIGHT)
        drag(canvas, QPoint(600, 300), QPoint(200, 100))
        (mark,) = canvas.marks
        assert mark.kind == HIGHLIGHT
        assert (mark.x1, mark.y1, mark.x2, mark.y2) == pytest.approx((0.25, 100 / 450, 0.75,
                                                                      300 / 450))

    def test_a_flat_highlight_covers_a_line_of_text(self, canvas):
        canvas.set_tool(ink.TOOL_HIGHLIGHT)
        drag(canvas, QPoint(100, 225), QPoint(500, 227))
        (mark,) = canvas.marks
        assert mark.y2 - mark.y1 == pytest.approx(ink.LINE_HIGHLIGHT_HEIGHT)
        assert (mark.y1 + mark.y2) / 2 == pytest.approx(226 / 450)

    def test_a_drag_starting_off_the_picture_draws_nothing(self, qapp):
        view = CaptureCanvas()
        view.resize(800, 600)  # letterboxed: the 16:9 picture leaves bands
        view.show_capture(picture(), ())
        view.show()
        QApplication.processEvents()
        drag(view, QPoint(400, 10), QPoint(600, 10))
        assert view.marks == ()

    def test_a_memo_is_typed_where_it_was_clicked(self, canvas):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 90))
        assert canvas.is_typing
        editor = canvas._memo_editor
        QTest.keyClicks(editor, "exam point")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        (mark,) = canvas.marks
        assert mark.kind == MEMO and mark.text == "exam point"
        assert (mark.x1, mark.y1) == pytest.approx((0.5, 0.2))
        assert not canvas.is_typing

    def test_esc_drops_the_memo_being_typed(self, canvas):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 90))
        QTest.keyClicks(canvas._memo_editor, "never mind")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Escape)
        assert canvas.marks == ()

    def test_clicking_a_memo_changes_it_and_emptying_it_removes_it(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "draft")])
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        editor = canvas._memo_editor
        assert editor.text() == "draft"
        editor.setText("러닝레이트 = 학습률")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert canvas.marks == (Mark.memo(0.5, 0.2, "러닝레이트 = 학습률"),)

        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        canvas._memo_editor.clear()
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks == ()

    @pytest.mark.parametrize("tool", [ink.TOOL_PEN, ink.TOOL_UNDERLINE, ink.TOOL_HIGHLIGHT])
    def test_a_memo_kept_with_enter_can_be_changed_by_double_clicking_it(self, canvas, tool):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 90))
        QTest.keyClicks(canvas._memo_editor, "fisrt")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert not canvas.is_typing

        canvas.set_tool(tool)
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        assert canvas.is_typing and canvas._memo_editor.text() == "fisrt"
        canvas._memo_editor.setText("first")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        # Changed in place - and the double-click drew nothing of its own.
        assert canvas.marks == (
            Mark.memo(0.5, 0.2, "first", size=ink.text_fraction(ink.DEFAULT_TEXT_POINTS)),
        )
        assert canvas.tool == tool

    def test_double_clicking_keeps_the_memos_own_ink(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "green", "#178a3e")])
        canvas.set_ink("#1f5fd6")
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        canvas._memo_editor.setText("still green")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks == (Mark.memo(0.5, 0.2, "still green", "#178a3e"),)

    def test_double_clicking_nothing_opens_nothing(self, canvas):
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(200, 300))
        assert not canvas.is_typing and canvas.marks == ()

    def test_the_eraser_does_not_open_a_memo_it_was_double_clicked_on(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "gone")])
        canvas.set_tool(ink.TOOL_ERASER)
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        assert not canvas.is_typing and canvas.marks == ()

    def test_clicking_elsewhere_keeps_what_was_typed(self, canvas):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 90))
        canvas._memo_editor.setText("kept")
        canvas.set_tool(ink.TOOL_UNDERLINE)
        assert [m.text for m in canvas.marks] == ["kept"]

    def test_clicking_elsewhere_keeps_the_memo_and_the_next_one_stays_open(self, canvas):
        canvas.activateWindow()
        QTest.qWaitForWindowActive(canvas)
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 90))
        QTest.keyClicks(canvas._memo_editor, "first")
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 300))
        # The first box's wait for the focus is over by now; it closed nothing.
        QTest.qWait(50)
        assert canvas.is_typing
        QTest.keyClicks(canvas._memo_editor, "second")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert [m.text for m in canvas.marks] == ["first", "second"]

    def test_the_eraser_removes_the_mark_clicked(self, canvas):
        canvas.set_marks([Mark.underline(0.25, 0.5, 0.75, 0.5), Mark.underline(0.25, 0.8,
                                                                               0.75, 0.8)])
        canvas.set_tool(ink.TOOL_ERASER)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 226))
        assert canvas.marks == (Mark.underline(0.25, 0.8, 0.75, 0.8),)

    def test_right_click_erases_with_any_tool(self, canvas):
        canvas.set_marks([Mark.highlight(0.1, 0.1, 0.5, 0.5)])
        QTest.mouseClick(canvas, Qt.MouseButton.RightButton, pos=QPoint(200, 100))
        assert canvas.marks == ()

    def test_ctrl_z_and_ctrl_y_undo_and_redo_marks(self, canvas):
        drag(canvas, QPoint(200, 225), QPoint(600, 225))
        canvas.setFocus()
        QTest.keyClick(canvas, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert canvas.marks == ()
        QTest.keyClick(canvas, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
        assert len(canvas.marks) == 1

    def test_clear_all_is_undoable(self, canvas):
        canvas.set_marks([Mark.underline(0, 0.5, 1, 0.5), Mark.memo(0.1, 0.1, "x")])
        bar = ink.InkToolBar(canvas)
        assert bar.clear_button.isEnabled()
        bar.clear_button.click()
        assert canvas.marks == ()
        assert not bar.clear_button.isEnabled()
        assert canvas.undo()
        assert len(canvas.marks) == 2

    def test_the_pen_holds_free_pen_underline_highlight_and_eraser(self, canvas):
        bar = ink.InkToolBar(canvas)
        kinds = [action.text() for action in bar.pen_button.menu().actions()]
        assert kinds == ["Free pen", "Underline", "Highlight", "Eraser"]
        assert bar.pen_button.isChecked()
        assert bar.pen_action(ink.TOOL_PEN).isChecked()

    def test_choosing_a_kind_of_pen_from_its_menu(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.pen_action(ink.TOOL_HIGHLIGHT).trigger()
        assert canvas.tool == ink.TOOL_HIGHLIGHT
        assert bar.pen_button.isChecked()
        assert bar.pen_button.accessibleName() == "Highlight"

    def test_the_pen_button_goes_back_to_the_kind_used_last(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.pen_action(ink.TOOL_UNDERLINE).trigger()
        bar.memo_button.click()
        assert canvas.tool == ink.TOOL_MEMO
        assert bar.memo_button.isChecked() and not bar.pen_button.isChecked()
        bar.pen_button.click()
        assert canvas.tool == ink.TOOL_UNDERLINE
        # Pressing it again keeps the pen rather than switching it off.
        bar.pen_button.click()
        assert bar.pen_button.isChecked()

    def test_the_toolbar_follows_tools_chosen_by_key(self, canvas):
        bar = ink.InkToolBar(canvas, keys=True)
        canvas.set_tool(ink.TOOL_ERASER)
        assert bar.pen_action(ink.TOOL_ERASER).isChecked()
        assert "(4)" in bar.pen_button.toolTip()
        assert "(5)" in bar.memo_button.toolTip()

    def test_loading_marks_is_not_an_edit(self, canvas):
        edits = []
        canvas.marksChanged.connect(lambda: edits.append(1))
        canvas.set_marks([Mark.underline(0, 0.5, 1, 0.5)])
        assert edits == []
        assert not canvas.sheet.can_undo


class TestInk:
    BLUE = "#1f5fd6"

    def test_red_to_start_with(self, canvas):
        assert canvas.ink == ink.DEFAULT_INK

    def test_new_marks_are_drawn_in_the_ink_chosen(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.ink_action(self.BLUE).trigger()
        assert canvas.ink == self.BLUE
        drag(canvas, QPoint(100, 100), QPoint(300, 160))  # free pen
        canvas.set_tool(ink.TOOL_UNDERLINE)
        drag(canvas, QPoint(100, 300), QPoint(500, 300))
        canvas.set_tool(ink.TOOL_HIGHLIGHT)
        drag(canvas, QPoint(100, 380), QPoint(500, 420))
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(600, 60))
        canvas._memo_editor.setText("blue note")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert [(m.kind, m.colour) for m in canvas.marks] == [
            (PEN, self.BLUE), (UNDERLINE, self.BLUE), (HIGHLIGHT, ""), (MEMO, self.BLUE),
        ]

    def test_marks_already_drawn_keep_their_ink(self, canvas):
        canvas.set_marks([Mark.underline(0, 0.5, 1, 0.5)])
        canvas.set_ink(self.BLUE)
        assert canvas.marks[0].colour == ""

    def test_the_default_ink_is_stored_as_the_default(self, canvas):
        drag(canvas, QPoint(100, 100), QPoint(300, 160))
        assert canvas.marks[0].colour == ""

    def test_a_memo_changed_keeps_its_ink(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "draft", "#178a3e")])
        canvas.set_ink(self.BLUE)
        canvas.set_tool(ink.TOOL_MEMO)
        # Clicking it takes the ink it has, not the one chosen for new marks...
        canvas.set_ink(ink.DEFAULT_INK)
        canvas.set_ink(self.BLUE)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        canvas._memo_editor.setText("final")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks[0] == Mark.memo(0.5, 0.2, "final", "#178a3e")

    def test_choosing_an_ink_while_typing_recolours_that_memo(self, canvas):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(600, 60))
        canvas._memo_editor.setText("note")
        canvas.set_ink(self.BLUE)
        assert self.BLUE in canvas._memo_editor.styleSheet()
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks[0].colour == self.BLUE

    def test_the_button_shows_the_ink(self, canvas):
        bar = ink.InkToolBar(canvas)
        assert bar.ink_button.accessibleName() == "Ink: Red"
        canvas.set_ink(self.BLUE)
        assert bar.ink_button.accessibleName() == "Ink: Blue"
        assert bar.ink_action(self.BLUE).isChecked()
        names = [a.text() for a in bar.ink_button.menu().actions()]
        assert names == ["Red", "Blue", "Black", "Green", "Orange", "Purple", "White"]

    def test_the_ink_is_what_gets_printed(self, qapp):
        marked = ink.render_marked(
            picture(320, 180), [Mark.underline(0.1, 0.5, 0.9, 0.5, self.BLUE)]
        )
        colour = QColor(marked.pixel(160, 90))
        assert colour.blue() > 150 and colour.red() < 100


class TestTextSize:
    def _type_memo(self, canvas, point, text):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=point)
        canvas._memo_editor.setText(text)
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)

    def test_a_number_smaller_than_memos_used_to_be(self, canvas):
        bar = ink.InkToolBar(canvas)
        assert bar.size_box.value() == ink.DEFAULT_TEXT_POINTS == 14
        assert ink.text_points(0.0) == 20  # what every memo from before is
        self._type_memo(canvas, QPoint(400, 90), "smaller")
        assert canvas.marks[0].size == ink.text_fraction(14)

    def test_new_memos_are_typed_in_the_number_chosen(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.size_box.setValue(24)
        assert canvas.text_size == ink.text_fraction(24)
        self._type_memo(canvas, QPoint(400, 90), "big")
        assert canvas.marks[0].size == ink.text_fraction(24)

    def test_minus_and_plus_step_the_number(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.larger_button.click()
        bar.larger_button.click()
        assert bar.size_box.value() == 16
        bar.smaller_button.click()
        assert canvas.text_size == ink.text_fraction(15)
        assert bar.larger_button.autoRepeat()

    def test_the_number_is_kept_inside_its_range(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.size_box.setValue(500)
        assert bar.size_box.value() == ink.MAX_TEXT_POINTS
        bar.size_box.setValue(1)
        assert bar.size_box.value() == ink.MIN_TEXT_POINTS

    def test_a_larger_memo_takes_more_room(self, canvas):
        rect = canvas.picture_rect()
        small = ink.memo_box(Mark.memo(0.1, 0.1, "same words", size=0.024), rect)
        large = ink.memo_box(Mark.memo(0.1, 0.1, "same words", size=0.064), rect)
        assert large.height() > small.height() * 2
        assert large.width() > small.width() * 2

    def test_a_memo_from_before_keeps_its_size(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "old")])
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 100))
        canvas._memo_editor.setText("old, changed")
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks[0].size == 0.0

    def test_while_a_memo_is_changed_its_number_is_shown(self, canvas):
        bar = ink.InkToolBar(canvas)
        canvas.set_marks([Mark.memo(0.5, 0.2, "big", size=ink.text_fraction(30))])
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 104))
        assert bar.size_box.value() == 30
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert bar.size_box.value() == ink.DEFAULT_TEXT_POINTS
        assert canvas.marks[0].size == ink.text_fraction(30)

    def test_changing_the_number_while_typing_resizes_that_memo(self, canvas):
        bar = ink.InkToolBar(canvas)
        bar.show()
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 90))
        canvas._memo_editor.setText("growing")
        before = canvas._memo_editor.height()
        # Going to the number to change it does not close the memo.
        bar.size_box.setFocus()
        QApplication.processEvents()
        QApplication.processEvents()
        assert canvas.is_typing
        bar.size_box.setValue(36)
        assert canvas._memo_editor.height() > before
        canvas.commit_memo()
        assert canvas.marks[0].size == ink.text_fraction(36)

    def test_leaving_the_memo_for_anything_else_keeps_it(self, canvas, qapp):
        from PySide6.QtWidgets import QLineEdit

        other = QLineEdit()
        other.show()
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(400, 90))
        canvas._memo_editor.setText("kept")
        other.setFocus()
        assert pump_until(lambda: not canvas.is_typing, timeout=2)
        assert [m.text for m in canvas.marks] == ["kept"]


class TestMemoWidth:
    TEXT = "a memo with quite a few words in it to wrap"

    def _edge(self, canvas, memo):
        box = ink.memo_box(memo, canvas.picture_rect())
        return box, QPoint(int(box.right()) - 1, int(box.center().y()))

    def test_a_set_width_is_the_box_width_and_the_text_wraps_to_it(self, canvas):
        rect = canvas.picture_rect()
        wide = ink.memo_box(Mark.memo(0.1, 0.1, self.TEXT, width=0.6), rect)
        narrow = ink.memo_box(Mark.memo(0.1, 0.1, self.TEXT, width=0.15), rect)
        assert wide.width() == pytest.approx(0.6 * rect.width())
        assert narrow.width() == pytest.approx(0.15 * rect.width())
        assert narrow.height() > wide.height() * 2  # more lines

    def test_dragging_the_right_edge_sets_the_width(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        box, edge = self._edge(canvas, memo)
        drag(canvas, edge, edge + QPoint(-150, 0))
        (changed,) = canvas.marks
        assert changed.width * 800 == pytest.approx(box.width() - 150, abs=2)
        # Only its width: where it is and what it says stay.
        assert (changed.x1, changed.y1, changed.text) == (memo.x1, memo.y1, memo.text)
        assert not canvas.is_typing

    def test_a_narrower_box_is_taller(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        box, edge = self._edge(canvas, memo)
        drag(canvas, edge, edge + QPoint(-int(box.width() * 0.6), 0))
        assert ink.memo_box(canvas.marks[0], canvas.picture_rect()).height() > box.height()

    def test_never_narrower_than_a_few_letters_nor_past_the_picture(self, canvas):
        memo = Mark.memo(0.5, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        rect = canvas.picture_rect()
        _box, edge = self._edge(canvas, memo)
        drag(canvas, edge, QPoint(0, edge.y()))
        smallest = ink.memo_min_width(memo, rect)
        assert canvas.marks[0].width * rect.width() == pytest.approx(smallest, abs=1)

        _box, edge = self._edge(canvas, canvas.marks[0])
        drag(canvas, edge, QPoint(5000, edge.y()))
        box = ink.memo_box(canvas.marks[0], rect)
        assert box.right() <= rect.right() + 0.5

    def test_a_width_change_is_undone_like_any_change(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        _box, edge = self._edge(canvas, memo)
        drag(canvas, edge, edge + QPoint(-120, 0))
        assert canvas.undo()
        assert canvas.marks == (memo,)

    def test_clicking_the_edge_without_dragging_opens_the_memo(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        _box, edge = self._edge(canvas, memo)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=edge)
        assert canvas.is_typing and canvas._memo_editor.text() == self.TEXT

    def test_the_edge_shows_it_can_be_dragged_and_the_memo_is_framed(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        canvas.set_tool(ink.TOOL_MEMO)
        box, edge = self._edge(canvas, memo)
        QTest.mouseMove(canvas, edge)
        assert canvas.cursor().shape() == Qt.CursorShape.SizeHorCursor
        assert canvas._hovered == 0
        QTest.mouseMove(canvas, QPoint(int(box.left()) + 10, int(box.center().y())))
        assert canvas.cursor().shape() == Qt.CursorShape.SizeAllCursor
        QTest.mouseMove(canvas, QPoint(700, 420))
        assert canvas._hovered == -1

    def test_with_another_tool_the_edge_is_nothing_special(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT)
        canvas.set_marks([memo])
        _box, edge = self._edge(canvas, memo)
        drag(canvas, edge, edge + QPoint(-150, 40))  # the free pen draws
        assert canvas.marks[0] == memo
        assert [m.kind for m in canvas.marks] == [MEMO, PEN]

    def test_the_memo_is_changed_in_a_box_as_wide_as_it(self, canvas):
        memo = Mark.memo(0.1, 0.2, self.TEXT, width=0.3)
        canvas.set_marks([memo])
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 100))
        assert canvas.is_typing
        assert canvas._memo_editor.width() == pytest.approx(0.3 * 800, abs=2)
        QTest.keyClick(canvas._memo_editor, Qt.Key.Key_Return)
        assert canvas.marks[0].width == 0.3


class TestMemoLines:
    @pytest.fixture
    def editor(self, canvas):
        """The box of a new memo, opened by a click, ready for keys."""
        canvas.activateWindow()
        QTest.qWaitForWindowActive(canvas)
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 90))
        return canvas._memo_editor

    @staticmethod
    def new_line(editor, key=Qt.Key.Key_Return):
        QTest.keyClick(editor, key, Qt.KeyboardModifier.ShiftModifier)

    def test_shift_enter_starts_a_new_line(self, canvas, editor):
        QTest.keyClicks(editor, "first")
        self.new_line(editor)
        assert canvas.is_typing and editor.text() == "first\n"
        QTest.keyClicks(editor, "second")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert [m.text for m in canvas.marks] == ["first\nsecond"]
        assert not canvas.is_typing

    def test_enter_alone_still_keeps_the_memo(self, canvas, editor):
        QTest.keyClicks(editor, "kept")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert not canvas.is_typing and [m.text for m in canvas.marks] == ["kept"]

    def test_enter_after_a_space_keeps_the_memo_too(self, canvas, editor):
        QTest.keyClicks(editor, "kept ")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert not canvas.is_typing and [m.text for m in canvas.marks] == ["kept"]

    def test_the_keypads_enter_does_the_same(self, canvas, editor):
        QTest.keyClicks(editor, "first")
        self.new_line(editor, Qt.Key.Key_Enter)
        assert canvas.is_typing and editor.text() == "first\n"
        QTest.keyClick(editor, Qt.Key.Key_Enter)
        assert not canvas.is_typing

    def test_enter_on_the_empty_new_line_keeps_the_memo_without_it(self, canvas, editor):
        QTest.keyClicks(editor, "first")
        self.new_line(editor)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert not canvas.is_typing and [m.text for m in canvas.marks] == ["first"]

    def test_a_new_line_can_go_in_the_middle(self, canvas, editor):
        QTest.keyClicks(editor, "firstsecond")
        for _ in "second":
            QTest.keyClick(editor, Qt.Key.Key_Left)
        self.new_line(editor)
        assert editor.text() == "first\nsecond"

    def test_the_box_grows_with_each_line_and_shows_them_all(self, canvas, editor):
        one_line = editor.height()
        QTest.keyClicks(editor, "first")
        self.new_line(editor)
        two_lines = editor.height()
        assert two_lines > one_line
        QTest.keyClicks(editor, "second")
        self.new_line(editor)
        assert editor.height() > two_lines
        assert editor.verticalScrollBar().maximum() == 0

    def test_a_box_at_the_bottom_grows_upwards_inside_the_picture(self, canvas):
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 440))
        editor = canvas._memo_editor
        for _ in range(4):
            QTest.keyClicks(editor, "line")
            self.new_line(editor)
        assert editor.geometry().bottom() < 450
        assert editor.y() < 440 - editor.height() / 2

    def test_a_memo_of_two_lines_takes_the_room_of_two_lines(self, canvas):
        rect = canvas.picture_rect()
        one = ink.memo_box(Mark.memo(0.1, 0.1, "first"), rect)
        two = ink.memo_box(Mark.memo(0.1, 0.1, "first\nsecond"), rect)
        assert two.height() > one.height() * 1.5

    def test_it_comes_back_in_lines_with_the_cursor_after_them(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "first\nsecond")])
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(410, 98))
        editor = canvas._memo_editor
        assert editor.text() == "first\nsecond"
        QTest.keyClicks(editor, "!")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert canvas.marks[0].text == "first\nsecond!"

    def test_the_lines_survive_being_saved(self):
        memo = Mark.memo(0.1, 0.1, "러닝레이트가 크면\n발산")
        assert Mark.from_dict(memo.to_dict()) == memo

    def test_the_picture_shows_through_the_box_and_both_lines_are_painted(self, canvas, editor):
        QTest.keyClicks(editor, "first")
        self.new_line(editor)
        QTest.keyClicks(editor, "second")
        QApplication.processEvents()
        shot = editor.geometry()
        image = canvas.grab(shot).toImage()
        white = QColor("white")
        corner = image.pixelColor(image.width() - 1, image.height() - 1)
        assert corner == white  # no box of its own over the picture
        rows = [any(image.pixelColor(x, y) != white for x in range(image.width()))
                for y in range(image.height())]
        assert rows[: image.height() // 2].count(True) and rows[image.height() // 2:].count(True)


class TestMovingMemos:
    def test_a_memo_dragged_with_the_memo_tool_moves(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "move me", "#1f5fd6", 0.03)])
        canvas.set_tool(ink.TOOL_MEMO)
        drag(canvas, QPoint(410, 98), QPoint(210, 323))
        (memo,) = canvas.marks
        assert (memo.x1, memo.y1) == pytest.approx((0.25, 0.7))
        # Only its place changed, and opening it was not what the drag did.
        assert (memo.text, memo.colour, memo.size) == ("move me", "#1f5fd6", 0.03)
        assert not canvas.is_typing

    def test_a_move_is_undone_like_any_change(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "here")])
        canvas.set_tool(ink.TOOL_MEMO)
        drag(canvas, QPoint(410, 98), QPoint(100, 300))
        assert canvas.undo()
        assert canvas.marks == (Mark.memo(0.5, 0.2, "here"),)

    def test_a_click_without_moving_opens_it(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "open me")])
        canvas.set_tool(ink.TOOL_MEMO)
        drag(canvas, QPoint(410, 98), QPoint(412, 99))
        assert canvas.is_typing and canvas._memo_editor.text() == "open me"
        assert canvas.marks == (Mark.memo(0.5, 0.2, "open me"),)

    def test_it_cannot_be_dragged_off_the_picture(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "edge")])
        canvas.set_tool(ink.TOOL_MEMO)
        drag(canvas, QPoint(410, 98), QPoint(2000, -900))
        (memo,) = canvas.marks
        assert (memo.x1, memo.y1) == (1.0, 0.0)
        assert canvas.picture_rect().contains(ink.memo_box(memo, canvas.picture_rect()))

    def test_with_the_pen_a_drag_over_a_memo_draws(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "stays")])
        drag(canvas, QPoint(410, 98), QPoint(600, 300))
        assert [m.kind for m in canvas.marks] == [MEMO, PEN]
        assert canvas.marks[0] == Mark.memo(0.5, 0.2, "stays")

    def test_the_memo_tool_shows_a_memo_can_be_moved(self, canvas):
        canvas.set_marks([Mark.memo(0.5, 0.2, "hover")])
        canvas.set_tool(ink.TOOL_MEMO)
        QTest.mouseMove(canvas, QPoint(410, 98))
        assert canvas.cursor().shape() == Qt.CursorShape.SizeAllCursor
        QTest.mouseMove(canvas, QPoint(100, 400))
        assert canvas.cursor().shape() == Qt.CursorShape.IBeamCursor


class TestRendering:
    def test_marks_are_drawn_into_a_full_size_copy(self, qapp):
        image = picture(320, 180)
        marked = ink.render_marked(
            image, [Mark.highlight(0.0, 0.0, 0.5, 0.5), Mark.memo(0.6, 0.6, "note")]
        )
        assert marked.size() == image.size()
        assert QColor(marked.pixel(40, 40)) != QColor("white")
        assert QColor(marked.pixel(300, 20)) == QColor("white")
        assert QColor(image.pixel(40, 40)) == QColor("white")  # the original is untouched

    def test_a_memo_near_the_edge_stays_inside_the_picture(self, qapp):
        rect = QRectF(0, 0, 400, 225)
        box = ink.memo_box(Mark.memo(0.98, 0.98, "a longer note near the corner"), rect)
        assert rect.contains(box)

    def test_hit_testing_finds_the_memo_on_top(self, qapp):
        rect = QRectF(0, 0, 800, 450)
        marks = [Mark.memo(0.5, 0.5, "top"), Mark.highlight(0.4, 0.4, 0.9, 0.9)]
        # The highlight was drawn later but is painted under the memo.
        assert ink.hit_test(marks, QPointF(405, 230), rect) == 0
        assert ink.hit_test(marks, QPointF(700, 400), rect) == 1
        assert ink.hit_test(marks, QPointF(10, 10), rect) == -1


# --------------------------------------------------------------------------
# Frames on background threads
# --------------------------------------------------------------------------


class TestFrameRendering:
    def test_a_frame_is_scaled_to_the_view_at_its_pixel_ratio(self, qapp):
        converted, size = render_frame(frame_at(1.0), QSize(320, 320), 2.0)
        assert size == QSize(640, 360)
        assert (converted.width(), converted.height()) == (640, 360)
        assert converted.devicePixelRatio() == 2.0

    def test_an_invalid_frame_gives_nothing(self, qapp):
        converted, size = render_frame(QVideoFrame(), QSize(320, 180), 1.0)
        assert converted.isNull() and size.isEmpty()

    def test_renders_off_the_interface_thread_and_reports_back(self, qapp):
        renderer = FrameRenderer()
        results = []
        renderer.rendered.connect(lambda seq, image, size: results.append((seq, size)))
        renderer.start()
        try:
            sequence = renderer.submit(frame_at(2.0), QSize(320, 180), 1.0)
            assert pump_until(lambda: results)
            assert results[-1] == (sequence, QSize(640, 360))
        finally:
            renderer.stop()
        assert not renderer.running

    def test_only_the_newest_waiting_frame_is_converted(self, qapp):
        renderer = FrameRenderer()
        results = []
        renderer.rendered.connect(lambda seq, image, size: results.append(seq))
        # Queue up while stopped: each submission replaces the last.
        for second in range(5):
            last = renderer.submit(frame_at(second), QSize(160, 90), 1.0)
        renderer.start()
        try:
            assert pump_until(lambda: results)
            QTest.qWait(50)
        finally:
            renderer.stop()
        assert results == [last]


# --------------------------------------------------------------------------
# The player
# --------------------------------------------------------------------------


@pytest.fixture
def player(qapp):
    """A player as if a video were loaded, fed frames by hand."""
    view = VideoPlayer()
    view.resize(800, 560)
    view._set_controls_enabled(True)
    view.show()
    QApplication.processEvents()
    show(view, frame_at(30.0))
    return view


class TestPlayerDrawing:
    def test_without_a_video_there_is_nothing_to_draw_on(self, qapp):
        view = VideoPlayer()
        view.set_drawing(True)
        assert not view.is_drawing
        assert view.take_screenshot() is None

    def test_drawing_swaps_in_the_canvas_with_the_same_picture(self, player):
        player.set_drawing(True)
        assert player.is_drawing
        assert player._surface.currentWidget() is player.canvas
        assert player.ink_bar.isVisible()
        # Converted at once, so the switch shows no blank frame.
        assert player.canvas.picture_size() == QSize(640, 360)

        player.set_drawing(False)
        assert player._surface.currentWidget() is player._video
        assert not player.ink_bar.isVisible()

    def test_the_draw_button_and_the_mode_agree(self, player):
        player._draw_button.click()
        assert player.is_drawing
        player.toggle_drawing()
        assert not player._draw_button.isChecked()

    def test_frames_keep_coming_while_drawing(self, player):
        player.set_drawing(True)
        shown = player.canvas._shown
        show(player, frame_at(31.0, "black"))
        assert pump_until(lambda: player.canvas._shown > shown)
        assert player.canvas.frame.startTime() == 31_000_000

    def test_only_the_surface_showing_is_given_frames(self, player):
        player.set_drawing(True)
        show(player, frame_at(32.0))
        player.set_drawing(False)
        show(player, frame_at(33.0))
        assert player.canvas.frame.startTime() == 32_000_000
        assert player._video.videoSink().videoFrame().startTime() == 33_000_000

    def test_keys_typed_in_a_memo_stay_in_the_memo(self, player):
        played = []
        player._play_shortcut.activated.connect(lambda: played.append(True))
        player.activateWindow()
        QTest.qWaitForWindowActive(player)
        player.set_drawing(True)
        canvas = player.canvas
        canvas.set_tool(ink.TOOL_MEMO)
        canvas.open_memo(0.2, 0.2)
        QApplication.processEvents()
        editor = canvas._memo_editor
        QTest.keyClicks(editor, "s1 d")
        QTest.keyClick(editor, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
        QTest.keyClicks(editor, "next")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        assert [m.text for m in canvas.marks] == ["s1 d\nnext"]
        assert played == []
        assert player.is_drawing and canvas.tool == ink.TOOL_MEMO

    def test_a_paused_video_stays_where_drawing_left_it(self, player):
        player.set_drawing(True)
        show(player, frame_at(37.0))
        player.set_drawing(False)
        assert player.current_frame.startTime() == 37_000_000

    def test_switching_drawing_on_and_off_again_keeps_working(self, player):
        for second in (34.0, 35.0):
            player.set_drawing(True)
            show(player, frame_at(second))
            assert player.canvas.frame.startTime() == int(second * 1_000_000)
            player.set_drawing(False)

    def test_a_screenshot_without_drawing_is_the_frame_on_screen(self, player):
        show(player, frame_at(36.0))
        shot = player.take_screenshot()
        assert shot.time == pytest.approx(36.0) and shot.marks == ()

    def test_a_screenshot_is_the_full_frame_with_the_marks(self, player):
        shots = []
        player.screenshotTaken.connect(shots.append)
        player.set_drawing(True)
        player.canvas.set_marks([Mark.underline(0.1, 0.5, 0.9, 0.5)])
        shot = player.take_screenshot()
        assert shots == [shot]
        assert isinstance(shot, FrameShot)
        assert shot.image.size() == QSize(640, 360)
        assert shot.time == pytest.approx(30.0)
        assert shot.marks == (Mark.underline(0.1, 0.5, 0.9, 0.5),)

    def test_hidden_marks_are_not_in_a_screenshot(self, player):
        player.set_drawing(True)
        player.canvas.set_marks([Mark.underline(0.1, 0.5, 0.9, 0.5)])
        player.set_drawing(False)
        assert player.take_screenshot().marks == ()
        # Still there when drawing comes back on.
        player.set_drawing(True)
        assert len(player.canvas.marks) == 1

    def test_the_stretch_starts_at_the_first_mark_since_the_last_screenshot(
        self, player, monkeypatch
    ):
        now = {"t": 20.0}
        monkeypatch.setattr(VideoPlayer, "position", property(lambda self: now["t"]))
        player.set_drawing(True)
        drag(player.canvas, QPoint(200, 100), QPoint(500, 100))
        now["t"] = 25.0
        drag(player.canvas, QPoint(200, 150), QPoint(500, 150))
        assert player.take_screenshot().start == pytest.approx(20.0)

        # Marks kept on screen belong to the screenshot already taken; the
        # next stretch begins with the next new mark.
        show(player, frame_at(40.0))
        assert player.take_screenshot().start == pytest.approx(40.0)
        now["t"] = 35.0
        drag(player.canvas, QPoint(200, 200), QPoint(500, 200))
        assert player.take_screenshot().start == pytest.approx(35.0)

    def test_the_keys_are_written_under_the_buttons(self, player):
        assert player._keys_label.isVisible()
        watching = player.keys_text
        for key, what in (("Space", "play / pause"), ("D", "draw"), ("S", "screenshot")):
            assert f">{key}</b>&nbsp;{what}".replace(" ", "&nbsp;") in watching

    def test_while_drawing_the_keys_are_the_tools(self, player):
        player.set_drawing(True)
        drawing = player.keys_text
        for key, what in (("1", "free pen"), ("2", "underline"), ("3", "highlight"),
                          ("4", "eraser"), ("5", "memo"), ("Ctrl+Z", "undo"),
                          ("D / Esc", "stop drawing")):
            assert f">{key}</b>&nbsp;{what}".replace(" ", "&nbsp;") in drawing
        player.set_drawing(False)
        assert player.keys_text == watching_keys(player)

    def test_without_a_video_no_keys_are_promised(self, qapp):
        view = VideoPlayer()
        view.show()
        QApplication.processEvents()
        assert not view._keys_label.isVisible()

    def test_esc_on_the_canvas_leaves_drawing(self, player):
        player.set_drawing(True)
        player.canvas.setFocus()
        QTest.keyClick(player.canvas, Qt.Key.Key_Escape)
        assert not player.is_drawing

    def test_clearing_the_player_forgets_the_marks(self, player):
        player.set_drawing(True)
        player.canvas.set_marks([Mark.underline(0.1, 0.5, 0.9, 0.5)])
        player.clear()
        assert not player.is_drawing
        assert player.canvas.marks == ()
        assert not player._draw_button.isEnabled()

    def test_a_screenshot_with_no_picture_explains(self, qapp):
        view = VideoPlayer()
        view._set_controls_enabled(True)
        reasons = []
        view.screenshotFailed.connect(reasons.append)
        assert view.take_screenshot() is None
        assert reasons and "no picture" in reasons[0]


# --------------------------------------------------------------------------
# The screenshot panel
# --------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path):
    info = VideoInfo(
        path=str(tmp_path / "lecture.mp4"), duration=600.0, width=1920, height=1080,
        fps=30.0, size_bytes=1, container="mp4", video_codec="h264", audio_codec="aac",
        has_audio=True,
    )
    return Project.create(tmp_path / "projects", "Lecture", info)


def shot(seconds: float, marks=(), start=None) -> FrameShot:
    return FrameShot(image=picture(640, 360), time=seconds,
                     start=seconds if start is None else start, marks=tuple(marks))


@pytest.fixture
def panel(qapp, project):
    view = CapturePanel()
    view.resize(380, 520)
    view.bind(project)
    view.show()
    QApplication.processEvents()
    return view


class TestCapturePanel:
    def test_starts_empty_and_says_how_to_take_one(self, panel):
        assert panel.count == 0
        assert panel._empty_label.isVisible()

    def test_a_screenshot_is_saved_into_the_project(self, panel, project):
        capture = panel.add_screenshot(
            shot(75.0, [Mark.memo(0.2, 0.2, "외울 것")], start=70.0)
        )
        assert panel.count == 1
        assert panel.selected == capture
        # Written in the background; the list follows the pictures.
        assert pump_until(lambda: (project.root / "captures.json").is_file())
        assert (project.root / capture.image).is_file()
        assert (project.root / capture.marked_image).is_file()
        reopened = Project.open(project.root)
        assert [c.marks for c in reopened.captures] == [[Mark.memo(0.2, 0.2, "외울 것")]]
        assert reopened.captures.get(capture.capture_id).start == 70.0

    def test_without_marks_no_marked_copy_is_written(self, panel, project):
        capture = panel.add_screenshot(shot(75.0))
        panel.flush()
        assert (project.root / capture.image).is_file()
        assert not (project.root / capture.marked_image).exists()

    def test_taking_one_does_not_wait_for_the_disk(self, panel, project, monkeypatch):
        import threading

        from lecture_saver.ui import capture_panel

        gate = threading.Event()
        real = capture_panel.write_image
        monkeypatch.setattr(capture_panel, "write_image",
                            lambda image, path: (gate.wait(5), real(image, path)))
        capture = panel.add_screenshot(shot(75.0))
        # Listed and shown at once, from the picture in memory.
        assert panel.count == 1
        assert panel.canvas.picture_size() == QSize(640, 360)
        assert not (project.root / capture.image).exists()
        assert not (project.root / "captures.json").exists()
        gate.set()
        assert panel.flush()
        assert (project.root / capture.image).is_file()
        assert len(Project.open(project.root).captures) == 1

    def test_one_deleted_while_being_written_leaves_nothing(
        self, panel, project, monkeypatch
    ):
        import threading

        from lecture_saver.ui import capture_panel

        gate = threading.Event()
        real = capture_panel.write_image
        monkeypatch.setattr(capture_panel, "write_image",
                            lambda image, path: (gate.wait(5), real(image, path)))
        capture = panel.add_screenshot(shot(75.0, [Mark.underline(0, 0.5, 1, 0.5)]))
        project.delete_capture(capture.capture_id)
        gate.set()
        panel.flush()
        assert not (project.root / capture.image).exists()
        assert not (project.root / capture.marked_image).exists()

    def test_a_picture_that_cannot_be_written_is_reported_and_dropped(
        self, panel, project, monkeypatch
    ):
        from lecture_saver.ui import capture_panel

        def fail(image, path):
            raise OSError("disk full")

        monkeypatch.setattr(capture_panel, "write_image", fail)
        warnings = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
        panel.add_screenshot(shot(75.0))
        panel.flush()
        assert panel.count == 0
        assert project.captures.is_empty
        assert warnings and "disk full" in warnings[0][2]

    def test_the_list_is_in_the_order_of_the_video(self, panel):
        panel.add_screenshot(shot(300.0))
        panel.add_screenshot(shot(100.0))
        times = [panel._model.capture_at(row).time for row in range(panel.count)]
        assert times == [100.0, 300.0]

    def test_marks_changed_in_the_panel_are_saved(self, panel, project):
        capture = panel.add_screenshot(shot(75.0))
        panel.flush()
        drag(panel.canvas, *_across(panel.canvas))
        assert panel.flush()
        reopened = Project.open(project.root)
        assert len(reopened.captures.get(capture.capture_id).marks) == 1
        assert (project.root / capture.marked_image).is_file()

        panel.canvas.clear_marks()
        panel.flush()
        assert Project.open(project.root).captures.get(capture.capture_id).marks == []
        assert not (project.root / capture.marked_image).exists()

    def test_edits_are_written_before_another_screenshot_is_shown(self, panel, project):
        first = panel.add_screenshot(shot(10.0))
        second = panel.add_screenshot(shot(20.0))
        panel.flush()
        panel.select(first.capture_id)
        drag(panel.canvas, *_across(panel.canvas))
        panel.select(second.capture_id)
        assert len(Project.open(project.root).captures.get(first.capture_id).marks) == 1

    def _type_memo(self, panel, text):
        canvas = panel.canvas
        canvas.set_tool(ink.TOOL_MEMO)
        rect = canvas.picture_rect()
        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton,
                         pos=QPoint(int(rect.left()) + 40, int(rect.top()) + 40))
        # Set rather than typed: QTest cannot type Hangul (the process ends).
        canvas._memo_editor.setText(text)
        assert canvas.is_typing

    def test_a_memo_being_typed_stays_when_another_screenshot_is_clicked(
        self, panel, project
    ):
        first = panel.add_screenshot(shot(10.0))
        second = panel.add_screenshot(shot(20.0))
        panel.flush()
        self._type_memo(panel, "시험 범위")
        strip = panel._strip
        tile = strip.visualRect(panel._model.index(0, 0)).center()
        QTest.mouseClick(strip.viewport(), Qt.MouseButton.LeftButton, pos=tile)
        assert panel.selected == first
        assert panel.flush()
        # On the screenshot it was typed on, not the one chosen.
        reopened = Project.open(project.root).captures
        assert [m.text for m in reopened.get(second.capture_id).marks] == ["시험 범위"]
        assert reopened.get(first.capture_id).marks == []

    def test_a_memo_being_typed_stays_when_a_screenshot_is_taken(self, panel, project):
        first = panel.add_screenshot(shot(10.0))
        panel.flush()
        self._type_memo(panel, "before the next")
        panel.add_screenshot(shot(20.0))
        assert panel.flush()
        reopened = Project.open(project.root).captures
        assert [m.text for m in reopened.get(first.capture_id).marks] == ["before the next"]

    def test_a_memo_being_typed_is_written_when_the_lecture_closes(self, panel, project):
        capture = panel.add_screenshot(shot(10.0))
        panel.flush()
        self._type_memo(panel, "not lost")
        assert panel.flush()
        assert [m.text for m in Project.open(project.root).captures.get(
            capture.capture_id).marks] == ["not lost"]

    def test_play_starts_a_little_before_the_marking(self, panel):
        panel.add_screenshot(shot(75.0, start=70.0))
        wanted = []
        panel.playRequested.connect(wanted.append)
        panel._play_button.click()
        assert wanted == [pytest.approx(68.0)]
        assert "00:01:08" in panel._play_button.text()

    def test_deleting_asks_then_removes_the_pictures(self, panel, project, monkeypatch):
        capture = panel.add_screenshot(shot(75.0, [Mark.underline(0, 0.5, 1, 0.5)]))
        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No
        )
        panel.delete_selected()
        assert panel.count == 1

        monkeypatch.setattr(
            QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
        )
        panel.delete_selected()
        assert panel.count == 0
        assert not (project.root / capture.image).exists()
        assert not (project.root / capture.marked_image).exists()
        assert panel._empty_label.isVisible()

    def test_a_lost_marked_copy_is_drawn_again(self, panel, project):
        capture = panel.add_screenshot(shot(75.0, [Mark.underline(0, 0.5, 1, 0.5)]))
        panel.flush()
        (project.root / capture.marked_image).unlink()
        panel.bind(None)
        panel.bind(Project.open(project.root))
        assert (project.root / capture.marked_image).is_file()

    def test_the_panel_says_its_keys(self, panel):
        panel.add_screenshot(shot(1.0))
        assert panel._keys_label.isVisible()
        for key in ("Ctrl+Z", "Delete", "Double-click a memo"):
            assert f">{key}</b>".replace(" ", "&nbsp;") in panel._keys_label.text()

    def test_the_count_is_announced(self, panel):
        counts = []
        panel.countChanged.connect(counts.append)
        panel.add_screenshot(shot(1.0))
        assert counts[-1] == 1

    def test_export_pdf_is_offered_once_there_is_something_to_export(self, panel):
        asked = []
        panel.exportRequested.connect(lambda: asked.append(True))
        assert not panel.export_button.isEnabled()
        panel.add_screenshot(shot(1.0))
        assert panel.export_button.isEnabled()
        panel.export_button.click()
        assert asked == [True]

    def test_export_pdf_waits_while_another_export_runs(self, panel):
        panel.add_screenshot(shot(1.0))
        panel.set_export_enabled(False)
        assert not panel.export_button.isEnabled()
        panel.set_export_enabled(True)
        assert panel.export_button.isEnabled()


def _across(canvas) -> tuple[QPoint, QPoint]:
    rect = canvas.picture_rect()
    y = int(rect.center().y())
    return QPoint(int(rect.left() + 20), y), QPoint(int(rect.right() - 20), y)


# --------------------------------------------------------------------------
# In the lecture view
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_video(tmp_path_factory):
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
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


class TestLectureViewScreenshots:
    @pytest.fixture
    def view(self, qapp, tmp_path, real_video):
        from lecture_saver.ui.lecture_view import LectureView

        info = VideoInfo(
            path=str(real_video), duration=2.0, width=160, height=120, fps=10.0,
            size_bytes=1, container="mp4", video_codec="h264", audio_codec="aac",
            has_audio=True,
        )
        project = Project.create(tmp_path / "projects", "Lecture", info)
        lecture = LectureView()
        lecture.resize(1400, 800)
        lecture.load_project(project)
        lecture.show()
        QApplication.processEvents()
        return lecture, project

    def test_a_screenshot_lands_in_the_panel_and_the_project(self, view):
        lecture, project = view
        messages = []
        lecture.statusMessage.connect(messages.append)
        lecture.player.screenshotTaken.emit(shot(1.0, [Mark.memo(0.1, 0.1, "note")]))
        assert lecture.capture_panel.count == 1
        assert len(project.captures) == 1
        assert lecture._captures_button.text() == "Screenshots (1)"
        assert "Screenshot saved at 00:00:01" in messages[-1]

    def test_the_panel_opens_beside_the_video_with_room(self, view):
        lecture, _project = view
        lecture._captures_button.click()
        QApplication.processEvents()
        assert lecture.capture_panel.isVisible()
        assert lecture._splitter.sizes()[2] >= lecture.capture_panel.minimumWidth()
        lecture.capture_panel._hide_button.click()
        assert not lecture._captures_button.isChecked()
        assert not lecture.capture_panel.isVisible()

    def test_play_seeks_and_plays(self, view, monkeypatch):
        lecture, _project = view
        lecture.player.screenshotTaken.emit(shot(1.5, start=1.0))
        went, played = [], []
        monkeypatch.setattr(lecture, "go_to_time", went.append)
        monkeypatch.setattr(lecture.player, "play", lambda: played.append(True))
        lecture.capture_panel._play_button.click()
        assert went == [0.0] and played == [True]

    def test_reopening_shows_the_screenshots_again(self, view):
        lecture, project = view
        lecture.player.screenshotTaken.emit(shot(1.0))
        lecture.close_project()
        assert lecture.capture_panel.count == 0
        lecture.load_project(Project.open(project.root))
        assert lecture.capture_panel.count == 1


class TestScreenshotsExportFromTheWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path, real_video, monkeypatch):
        from lecture_saver.config import Settings
        from lecture_saver.core.captures import Capture, CaptureSet
        from lecture_saver.ui.main_window import MainWindow

        info = VideoInfo(
            path=str(real_video), duration=2.0, width=160, height=120, fps=10.0,
            size_bytes=1, container="mp4", video_codec="h264", audio_codec="aac",
            has_audio=True,
        )
        project = Project.create(tmp_path / "projects", "Lecture 07", info)
        capture_id = project.captures.new_id()
        capture = Capture(capture_id=capture_id, time=1.0, start=1.0,
                          image=CaptureSet.image_name(capture_id), width=640, height=360)
        project.captures_dir.mkdir()
        picture(640, 360).save(str(project.root / capture.image), "JPG")
        project.captures.add(capture)
        project.save_captures()

        shown = []
        monkeypatch.setattr(MainWindow, "_show_export_result",
                            lambda self, result, ai_request: shown.append(result))
        win = MainWindow(Settings(tmp_path / "settings.json"))
        win._open_project_at(project.root)
        win.shown_results = shown
        yield win
        win._wait_for_export()
        win.close()

    def test_the_panel_button_writes_the_screenshots_pdf(self, window):
        from lecture_saver.export.options import safe_file_stem

        view = window._lecture_view
        assert view.capture_panel.export_button.isEnabled()
        view.capture_panel.export_button.click()
        assert pump_until(lambda: window.shown_results, timeout=60)
        (result,) = window.shown_results
        assert result.ok, result.failures
        assert [p.name for p in result.written] == [
            f"{safe_file_stem('Lecture 07')}_screenshots.pdf"
        ]

    def test_the_file_menu_offers_it_with_a_project(self, window):
        assert window._screenshots_export_action.isEnabled()
        window._close_project()
        assert not window._screenshots_export_action.isEnabled()

    def test_with_no_screenshots_it_explains_instead(self, window, monkeypatch):
        told = []
        monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[1]))
        for capture in list(window.project.captures):
            window.project.delete_capture(capture.capture_id)
        window._export_screenshots()
        assert told == ["No screenshots yet"]
        assert window.shown_results == []
