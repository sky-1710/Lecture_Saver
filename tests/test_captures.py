"""Screenshots and their marks: the data kept for them, and the project files."""

import json

import pytest

from lecture_saver.core.captures import (
    HIGHLIGHT,
    MEMO,
    PEN,
    PLAY_LEAD_SECONDS,
    UNDERLINE,
    Capture,
    CaptureSet,
    Mark,
    MarkSheet,
    marking_start,
    simplify_path,
)
from lecture_saver.core.models import VideoInfo
from lecture_saver.core.project import CAPTURES_FILE, Project


def _capture(capture_id: int, time: float, marks=(), start=None) -> Capture:
    return Capture(
        capture_id=capture_id, time=time, start=time if start is None else start,
        image=CaptureSet.image_name(capture_id), width=1920, height=1080, marks=list(marks),
    )


class TestMark:
    def test_positions_stay_inside_the_picture(self):
        mark = Mark.underline(-0.2, 0.5, 1.4, 0.5)
        assert (mark.x1, mark.x2) == (0.0, 1.0)

    def test_a_highlight_is_the_same_whichever_way_it_was_dragged(self):
        assert Mark.highlight(0.8, 0.6, 0.2, 0.4) == Mark.highlight(0.2, 0.4, 0.8, 0.6)
        mark = Mark.highlight(0.8, 0.6, 0.2, 0.4)
        assert (mark.x1, mark.y1, mark.x2, mark.y2) == (0.2, 0.4, 0.8, 0.6)

    @pytest.mark.parametrize(
        "mark",
        [
            Mark.underline(0.1, 0.2, 0.7, 0.2),
            Mark.highlight(0.1, 0.2, 0.7, 0.3),
            Mark.memo(0.4, 0.5, "러닝레이트가 크면 발산"),
            Mark.pen([(0.1, 0.1), (0.2, 0.3), (0.4, 0.2)]),
        ],
    )
    def test_round_trips(self, mark):
        assert Mark.from_dict(json.loads(json.dumps(mark.to_dict()))) == mark

    def test_a_pen_stroke_knows_the_box_around_it(self):
        mark = Mark.pen([(0.4, 0.2), (0.1, 0.6), (0.3, 0.9)])
        assert (mark.x1, mark.y1, mark.x2, mark.y2) == (0.1, 0.2, 0.4, 0.9)

    def test_a_pen_stroke_is_stored_as_its_points(self):
        data = Mark.pen([(0.1, 0.2), (0.3, 0.4)]).to_dict()
        assert data == {"kind": PEN, "points": [[0.1, 0.2], [0.3, 0.4]]}

    def test_a_colour_is_kept_and_saved(self):
        mark = Mark.pen([(0.1, 0.1), (0.2, 0.2)], "#1F5FD6")
        assert mark.colour == "#1f5fd6"
        assert Mark.from_dict(mark.to_dict()) == mark
        assert Mark.memo(0.1, 0.1, "x", "#178a3e").to_dict()["colour"] == "#178a3e"

    def test_the_default_ink_is_left_unsaid(self):
        # Marks saved before colours could be chosen load as the default.
        assert "colour" not in Mark.underline(0, 0.5, 1, 0.5).to_dict()
        data = {"kind": "underline", "x1": 0, "y1": 0.5, "x2": 1, "y2": 0.5}
        assert Mark.from_dict(data).colour == ""

    @pytest.mark.parametrize("colour", ["red", "#12345", "#gggggg", 7, None])
    def test_what_is_not_a_colour_is_the_default(self, colour):
        data = {"kind": "memo", "x": 0.1, "y": 0.1, "text": "x", "colour": colour}
        assert Mark.from_dict(data).colour == ""

    def test_a_memo_keeps_its_text_size(self):
        memo = Mark.memo(0.1, 0.1, "big", size=0.064)
        assert Mark.from_dict(memo.to_dict()) == memo
        assert memo.to_dict()["size"] == 0.064
        assert memo.with_text("bigger").size == 0.064

    def test_the_default_text_size_is_left_unsaid(self):
        assert "size" not in Mark.memo(0.1, 0.1, "x").to_dict()
        old = {"kind": "memo", "x": 0.1, "y": 0.1, "text": "from before"}
        assert Mark.from_dict(old).size == 0.0

    @pytest.mark.parametrize("size", [0.0, -1, 2.0, "large", True])
    def test_what_is_not_a_text_size_is_the_default(self, size):
        data = {"kind": "memo", "x": 0.1, "y": 0.1, "text": "x", "size": size}
        assert Mark.from_dict(data).size == 0.0

    def test_changing_a_memo_keeps_its_colour(self):
        memo = Mark.memo(0.1, 0.1, "draft", "#7b3fc4")
        assert memo.with_text("final").colour == "#7b3fc4"

    def test_a_memo_is_stored_with_its_text(self):
        data = Mark.memo(0.4, 0.5, "시험 범위").to_dict()
        assert data == {"kind": MEMO, "x": 0.4, "y": 0.5, "text": "시험 범위"}

    @pytest.mark.parametrize(
        "data",
        [
            None,
            {"kind": "scribble", "x1": 0, "y1": 0, "x2": 1, "y2": 1},
            {"kind": UNDERLINE, "x1": 0, "y1": 0, "x2": "far", "y2": 1},
            {"kind": HIGHLIGHT, "x1": 0, "y1": 0, "x2": 1},
            {"kind": MEMO, "x": 0.1, "y": 0.1, "text": "   "},
            {"kind": MEMO, "x": True, "y": 0.1, "text": "note"},
            {"kind": PEN, "points": [[0.1, 0.1]]},
            {"kind": PEN, "points": [[0.1, 0.1], [0.2]]},
            {"kind": PEN, "points": "0.1,0.1 0.2,0.2"},
        ],
    )
    def test_what_is_not_a_mark_is_ignored(self, data):
        assert Mark.from_dict(data) is None


class TestSimplifyPath:
    def test_points_on_a_straight_line_go(self):
        line = [(float(x), 0.0) for x in range(50)]
        assert simplify_path(line, 0.5) == [(0.0, 0.0), (49.0, 0.0)]

    def test_corners_stay(self):
        corner = [(float(x), 0.0) for x in range(10)] + [(9.0, float(y)) for y in range(1, 10)]
        assert simplify_path(corner, 0.5) == [(0.0, 0.0), (9.0, 0.0), (9.0, 9.0)]

    def test_a_wobble_inside_the_tolerance_is_smoothed_away(self):
        wobbly = [(float(x), 0.3 if x % 2 else -0.3) for x in range(20)]
        assert len(simplify_path(wobbly, 0.8)) == 2

    def test_a_closed_loop_keeps_its_shape(self):
        loop = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        assert simplify_path(loop, 0.5) == loop

    def test_short_paths_are_returned_as_they_are(self):
        assert simplify_path([(1.0, 1.0)], 0.5) == [(1.0, 1.0)]


class TestMarkingStart:
    def test_starts_where_the_first_mark_was_drawn(self):
        assert marking_start(752.0, 760.0) == 752.0

    def test_a_plain_screenshot_covers_its_own_moment(self):
        assert marking_start(None, 760.0) == 760.0

    def test_a_mark_after_the_moment_shown_is_not_a_start(self):
        # Drawn, then the video was moved back before the screenshot.
        assert marking_start(800.0, 760.0) == 760.0


class TestCapture:
    def test_plays_from_a_little_before_the_marking_began(self):
        capture = _capture(1, 760.0, start=752.0)
        assert capture.play_from == pytest.approx(752.0 - PLAY_LEAD_SECONDS)

    def test_playing_never_starts_before_the_video(self):
        assert _capture(1, 0.5).play_from == 0.0

    def test_the_marked_copy_sits_beside_the_picture(self):
        capture = _capture(7, 10.0, marks=[Mark.underline(0, 0.5, 1, 0.5)])
        assert capture.image == "captures/0007.jpg"
        assert capture.marked_image == "captures/0007_marked.jpg"
        assert capture.printed_image == "captures/0007_marked.jpg"

    def test_without_marks_the_plain_picture_is_printed(self):
        assert _capture(7, 10.0).printed_image == "captures/0007.jpg"

    def test_round_trips_with_its_marks(self):
        capture = _capture(
            3, 125.5, start=118.0,
            marks=[Mark.highlight(0.1, 0.1, 0.5, 0.2), Mark.memo(0.6, 0.7, "중요")],
        )
        again = Capture.from_dict(json.loads(json.dumps(capture.to_dict())))
        assert again == capture

    def test_a_start_after_the_picture_is_brought_back_to_it(self):
        data = _capture(1, 50.0).to_dict() | {"start": 90.0}
        assert Capture.from_dict(data).start == 50.0

    def test_a_record_without_a_picture_is_dropped(self):
        assert Capture.from_dict({"capture_id": 1, "time": 3.0}) is None

    def test_unreadable_marks_are_dropped_but_the_rest_kept(self):
        data = _capture(1, 50.0).to_dict() | {
            "marks": [{"kind": "nonsense"}, Mark.underline(0, 0.5, 1, 0.5).to_dict()]
        }
        assert Capture.from_dict(data).marks == [Mark.underline(0, 0.5, 1, 0.5)]


class TestCaptureSet:
    def test_kept_in_the_order_of_the_video(self):
        captures = CaptureSet()
        captures.add(_capture(1, 300.0))
        captures.add(_capture(2, 100.0))
        captures.add(_capture(3, 200.0))
        assert [c.capture_id for c in captures] == [2, 3, 1]

    def test_ids_are_never_given_out_twice(self):
        captures = CaptureSet()
        for _ in range(3):
            captures.add(_capture(captures.new_id(), 10.0))
        captures.remove(3)
        # A new screenshot must not take over the deleted one's file name.
        assert captures.new_id() == 4

    def test_the_id_counter_survives_saving(self):
        captures = CaptureSet()
        captures.add(_capture(captures.new_id(), 10.0))
        captures.add(_capture(captures.new_id(), 20.0))
        captures.remove(2)
        again = CaptureSet.from_dict(json.loads(json.dumps(captures.to_dict())))
        assert again.new_id() == 3

    def test_adding_an_id_twice_is_refused(self):
        captures = CaptureSet()
        captures.add(_capture(1, 10.0))
        with pytest.raises(ValueError):
            captures.add(_capture(1, 20.0))

    def test_between_is_half_open(self):
        captures = CaptureSet()
        for capture_id, time in ((1, 10.0), (2, 20.0), (3, 30.0)):
            captures.add(_capture(capture_id, time))
        assert [c.capture_id for c in captures.between(10.0, 30.0)] == [1, 2]

    def test_duplicate_and_broken_records_are_skipped_on_load(self):
        data = {
            "captures": [
                _capture(1, 10.0).to_dict(), _capture(1, 99.0).to_dict(),
                {"capture_id": "two"}, _capture(5, 5.0).to_dict(),
            ]
        }
        captures = CaptureSet.from_dict(data)
        assert [c.capture_id for c in captures] == [5, 1]
        assert captures.get(1).time == 10.0
        assert captures.new_id() == 6

    def test_a_file_that_is_not_an_object_is_an_error(self):
        with pytest.raises(ValueError):
            CaptureSet.from_dict([1, 2])


class TestMarkSheet:
    def test_undo_and_redo_every_kind_of_change(self):
        a, b = Mark.underline(0, 0.5, 1, 0.5), Mark.memo(0.2, 0.2, "note")
        sheet = MarkSheet()
        sheet.add(a)
        sheet.add(b)
        sheet.replace(1, b.with_text("better note"))
        sheet.remove(0)
        assert sheet.marks == (b.with_text("better note"),)
        assert sheet.undo() and sheet.marks == (a, b.with_text("better note"))
        assert sheet.undo() and sheet.marks == (a, b)
        assert sheet.redo() and sheet.marks == (a, b.with_text("better note"))

    def test_clear_is_one_step_undo_brings_back(self):
        sheet = MarkSheet([Mark.underline(0, 0.5, 1, 0.5), Mark.highlight(0, 0, 1, 1)])
        assert sheet.clear()
        assert not sheet
        assert sheet.undo()
        assert len(sheet) == 2

    def test_clearing_nothing_is_not_a_step(self):
        sheet = MarkSheet()
        assert not sheet.clear()
        assert not sheet.can_undo

    def test_a_new_change_drops_what_could_be_redone(self):
        sheet = MarkSheet()
        sheet.add(Mark.underline(0, 0.1, 1, 0.1))
        sheet.undo()
        sheet.add(Mark.underline(0, 0.2, 1, 0.2))
        assert not sheet.can_redo

    def test_replacing_a_mark_with_itself_changes_nothing(self):
        mark = Mark.memo(0.2, 0.2, "same")
        sheet = MarkSheet([mark])
        sheet.replace(0, mark)
        assert not sheet.can_undo

    def test_loading_other_marks_starts_the_history_again(self):
        sheet = MarkSheet()
        sheet.add(Mark.underline(0, 0.1, 1, 0.1))
        sheet.load([Mark.memo(0.5, 0.5, "other")])
        assert not sheet.can_undo
        assert sheet.marks == (Mark.memo(0.5, 0.5, "other"),)


@pytest.fixture
def project(tmp_path):
    info = VideoInfo(
        path=str(tmp_path / "lecture.mp4"), duration=600.0, width=1920, height=1080,
        fps=30.0, size_bytes=1, container="mp4", video_codec="h264", audio_codec="aac",
        has_audio=True,
    )
    return Project.create(tmp_path / "projects", "Lecture", info)


class TestProjectCaptures:
    def test_screenshots_survive_reopening(self, project):
        capture = _capture(project.captures.new_id(), 42.0,
                           marks=[Mark.memo(0.1, 0.1, "여기 시험")])
        project.captures.add(capture)
        project.save_captures()

        reopened = Project.open(project.root)
        assert list(reopened.captures) == [capture]

    def test_a_new_project_has_none_and_no_file(self, project):
        assert project.captures.is_empty
        assert not (project.root / CAPTURES_FILE).exists()

    def test_a_damaged_list_does_not_stop_the_project_opening(self, project):
        (project.root / CAPTURES_FILE).write_text("{ not json", encoding="utf-8")
        assert Project.open(project.root).captures.is_empty

    def test_deleting_removes_both_pictures(self, project):
        capture = _capture(project.captures.new_id(), 42.0,
                           marks=[Mark.underline(0, 0.5, 1, 0.5)])
        project.captures_dir.mkdir()
        for name in (capture.image, capture.marked_image):
            (project.root / name).write_bytes(b"jpeg")
        project.captures.add(capture)
        project.save_captures()

        assert project.delete_capture(capture.capture_id)
        assert not (project.root / capture.image).exists()
        assert not (project.root / capture.marked_image).exists()
        assert Project.open(project.root).captures.is_empty

    def test_deleting_an_unknown_screenshot_says_so(self, project):
        assert project.delete_capture(99) is False
