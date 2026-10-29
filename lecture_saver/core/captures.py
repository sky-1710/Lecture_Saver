"""Screenshots taken of the lecture video, and the marks drawn on them.

The user turns on drawing over the video, underlines and highlights what
matters and types short memos while it keeps playing, and saves the frame on
screen with those marks as a screenshot. This module is what is kept of that:

* :class:`Mark` - one pen stroke, underline, highlight or memo. Positions are
  fractions of the picture (0..1 across and down), so a mark lands on the same
  spot of the small view in the panel, the full-size frame and an export, at
  any size.
* :class:`Capture` - one screenshot: the frame saved under ``captures/``, the
  moment it shows, where the marking for it began, and its marks. The marks
  are kept as data rather than burnt into the picture, so they stay editable;
  a copy with them drawn in is written beside it for printing.
* :class:`CaptureSet` - every screenshot of a lecture (``captures.json``).
* :class:`MarkSheet` - marks being edited, with undo and redo.

Pure data, no Qt: the video overlay, the screenshot panel and the exports all
read the same records.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "CAPTURES_SCHEMA_VERSION",
    "HIGHLIGHT",
    "MARK_KINDS",
    "MEMO",
    "PEN",
    "PLAY_LEAD_SECONDS",
    "UNDERLINE",
    "Capture",
    "CaptureSet",
    "Mark",
    "MarkSheet",
    "marking_start",
    "simplify_path",
]

CAPTURES_SCHEMA_VERSION = 1

PEN = "pen"
UNDERLINE = "underline"
HIGHLIGHT = "highlight"
MEMO = "memo"
MARK_KINDS = (PEN, UNDERLINE, HIGHLIGHT, MEMO)

#: Playing a screenshot starts this much before its marking began, so the
#: sentence that prompted it is heard from its start.
PLAY_LEAD_SECONDS = 2.0

#: Undo steps kept per sheet. A mark is a few numbers, so this is generous.
UNDO_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _unit(value: object) -> float | None:
    """A position inside the picture, or ``None`` if ``value`` is not a number."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return min(1.0, max(0.0, float(value)))


_HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")


def _colour(value: object) -> str:
    """``value`` as ``#rrggbb``, or "" - the drawing's default - if it is none."""
    return value.lower() if isinstance(value, str) and _HEX_COLOUR.fullmatch(value) else ""


def _text_size(value: object) -> float:
    """A memo's text height as a fraction of the picture's, or 0 - the default."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    return float(value) if 0.005 <= value <= 0.25 else 0.0


def _box_width(value: object) -> float:
    """A memo box's width as a fraction of the picture's, or 0 - fit the text."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return 0.0
    return float(value) if 0.01 <= value <= 1.0 else 0.0


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


Point = tuple[float, float]


@dataclass(frozen=True)
class Mark:
    """One pen stroke, underline, highlight or memo, in fractions of the picture."""

    kind: str
    #: Underline: from (x1, y1) to (x2, y2). Highlight: its top-left and
    #: bottom-right corners. Memo: (x1, y1) is the note's top-left corner and
    #: x2, y2 repeat it. Pen: the box around the stroke.
    x1: float
    y1: float
    x2: float
    y2: float
    #: What a memo says; empty for the other kinds.
    text: str = ""
    #: The points a pen stroke passes through, in order; empty otherwise.
    points: tuple[Point, ...] = ()
    #: The ink, as ``#rrggbb``; "" is the default red - and what every mark
    #: drawn before colours could be chosen has. Highlights are always yellow.
    colour: str = ""
    #: A memo's text height as a fraction of the picture's; 0 is the default
    #: size, and what memos from before sizes could be chosen have.
    size: float = 0.0
    #: A memo box's width as a fraction of the picture's, its text wrapped to
    #: it; 0 - every memo before widths could be set - fits the text.
    width: float = 0.0

    @classmethod
    def pen(cls, points: Iterable[Point], colour: str = "") -> Mark:
        """A freehand stroke through ``points``."""
        kept = tuple((min(1.0, max(0.0, float(x))), min(1.0, max(0.0, float(y))))
                     for x, y in points)
        if not kept:
            raise ValueError("A pen stroke needs at least one point.")
        xs, ys = [x for x, _ in kept], [y for _, y in kept]
        return cls(PEN, min(xs), min(ys), max(xs), max(ys), points=kept,
                   colour=_colour(colour))

    @classmethod
    def underline(cls, x1: float, y1: float, x2: float, y2: float, colour: str = "") -> Mark:
        return cls(UNDERLINE, *_clamped(x1, y1, x2, y2), colour=_colour(colour))

    @classmethod
    def highlight(cls, x1: float, y1: float, x2: float, y2: float) -> Mark:
        """The rectangle between two corners, whichever way it was dragged."""
        left, top, right, bottom = _clamped(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        return cls(HIGHLIGHT, left, top, right, bottom)

    @classmethod
    def memo(
        cls, x: float, y: float, text: str, colour: str = "", size: float = 0.0,
        width: float = 0.0,
    ) -> Mark:
        left, top, _, _ = _clamped(x, y, x, y)
        return cls(MEMO, left, top, left, top, text, colour=_colour(colour),
                   size=_text_size(size), width=_box_width(width))

    def with_text(self, text: str) -> Mark:
        return replace(self, text=text)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any]
        if self.kind == PEN:
            data = {"kind": PEN,
                    "points": [[round(x, 5), round(y, 5)] for x, y in self.points]}
        elif self.kind == MEMO:
            data = {"kind": MEMO, "x": round(self.x1, 5), "y": round(self.y1, 5),
                    "text": self.text}
            if self.size:
                data["size"] = round(self.size, 4)
            if self.width:
                data["width"] = round(self.width, 4)
        else:
            data = {
                "kind": self.kind,
                "x1": round(self.x1, 5), "y1": round(self.y1, 5),
                "x2": round(self.x2, 5), "y2": round(self.y2, 5),
            }
        if self.colour:
            data["colour"] = self.colour
        return data

    @classmethod
    def from_dict(cls, data: object) -> Mark | None:
        """The mark ``data`` describes, or ``None`` when it describes none."""
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        colour = _colour(data.get("colour"))
        if kind == PEN:
            raw = data.get("points")
            if not isinstance(raw, list):
                return None
            points: list[Point] = []
            for pair in raw:
                if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                    return None
                x, y = _unit(pair[0]), _unit(pair[1])
                if x is None or y is None:
                    return None
                points.append((x, y))
            return cls.pen(points, colour) if len(points) >= 2 else None
        if kind == MEMO:
            x, y = _unit(data.get("x")), _unit(data.get("y"))
            text = data.get("text")
            if x is None or y is None or not isinstance(text, str) or not text.strip():
                return None
            return cls.memo(x, y, text, colour, _text_size(data.get("size")),
                            _box_width(data.get("width")))
        if kind not in (UNDERLINE, HIGHLIGHT):
            return None
        corners = [_unit(data.get(key)) for key in ("x1", "y1", "x2", "y2")]
        if any(c is None for c in corners):
            return None
        x1, y1, x2, y2 = (float(c) for c in corners)  # type: ignore[arg-type]
        return cls.underline(x1, y1, x2, y2, colour) if kind == UNDERLINE else cls.highlight(
            x1, y1, x2, y2
        )


def _clamped(*values: float) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = (min(1.0, max(0.0, float(v))) for v in values)
    return x1, y1, x2, y2


def simplify_path(points: Sequence[Point], tolerance: float) -> list[Point]:
    """``points`` with those that add nothing to the shape left out.

    Ramer-Douglas-Peucker: a point within ``tolerance`` of the straight line
    between its neighbours that are kept is dropped. A stroke dragged slowly
    produces a point per pixel; this keeps the dozens that make its shape.
    The first and last points are always kept.
    """
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        (ax, ay), (bx, by) = points[first], points[last]
        dx, dy = bx - ax, by - ay
        length = (dx * dx + dy * dy) ** 0.5
        farthest, distance = -1, tolerance
        for index in range(first + 1, last):
            px, py = points[index]
            if length == 0:
                away = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            else:
                away = abs(dy * (px - ax) - dx * (py - ay)) / length
            if away > distance:
                farthest, distance = index, away
        if farthest >= 0:
            keep[farthest] = True
            stack += [(first, farthest), (farthest, last)]
    return [point for point, kept in zip(points, keep, strict=True) if kept]


def marking_start(first_mark_at: float | None, time: float) -> float:
    """Where the stretch of the lecture a screenshot covers begins.

    That is when its first mark was drawn - the moment the user started
    picking things out - or the screenshot's own time when nothing was drawn,
    or when the video was moved back past the mark before the screenshot.
    """
    if first_mark_at is None or first_mark_at > time:
        return time
    return max(0.0, first_mark_at)


@dataclass
class Capture:
    """One screenshot of the video and the marks on it."""

    capture_id: int
    #: The moment of the video the picture shows, in seconds.
    time: float
    #: Where the marking for it began; equal to ``time`` for a plain screenshot.
    start: float
    #: Project-relative path of the frame as the video showed it, without marks.
    image: str
    width: int = 0
    height: int = 0
    marks: list[Mark] = field(default_factory=list)
    created: str = field(default_factory=_now)

    @property
    def has_marks(self) -> bool:
        return bool(self.marks)

    @property
    def play_from(self) -> float:
        """Where playing this screenshot's stretch of the lecture starts."""
        return max(0.0, min(self.start, self.time) - PLAY_LEAD_SECONDS)

    @property
    def marked_image(self) -> str:
        """Project-relative path of the copy with the marks drawn in."""
        path = Path(self.image)
        return (path.parent / f"{path.stem}_marked{path.suffix}").as_posix()

    @property
    def printed_image(self) -> str:
        """The picture to print: with the marks when there are any."""
        return self.marked_image if self.marks else self.image

    def image_path(self, project_root: Path) -> Path:
        return Path(project_root) / self.image

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "time": round(self.time, 3),
            "start": round(self.start, 3),
            "image": self.image,
            "width": self.width,
            "height": self.height,
            "created": self.created,
            "marks": [mark.to_dict() for mark in self.marks],
        }

    @classmethod
    def from_dict(cls, data: object) -> Capture | None:
        if not isinstance(data, dict):
            return None
        capture_id = data.get("capture_id")
        image = data.get("image")
        if not isinstance(capture_id, int) or isinstance(capture_id, bool) or capture_id < 1:
            return None
        if not isinstance(image, str) or not image:
            return None
        time = max(0.0, _number(data.get("time")))
        marks = [m for m in (Mark.from_dict(raw) for raw in data.get("marks") or []) if m]
        return cls(
            capture_id=capture_id,
            time=time,
            start=min(time, max(0.0, _number(data.get("start"), time))),
            image=image,
            width=int(_number(data.get("width"))),
            height=int(_number(data.get("height"))),
            marks=marks,
            created=str(data.get("created") or ""),
        )


@dataclass
class CaptureSet:
    """Every screenshot of one lecture, in the order of the video."""

    captures: list[Capture] = field(default_factory=list)
    #: The highest id ever given out. Ids - and so image file names - are never
    #: reused, so a deleted screenshot's picture can never stand in for a new one.
    last_id: int = 0

    def __len__(self) -> int:
        return len(self.captures)

    def __iter__(self) -> Iterator[Capture]:
        return iter(self.captures)

    @property
    def is_empty(self) -> bool:
        return not self.captures

    def get(self, capture_id: int) -> Capture | None:
        return next((c for c in self.captures if c.capture_id == capture_id), None)

    def index_of(self, capture_id: int) -> int:
        return next(
            (i for i, c in enumerate(self.captures) if c.capture_id == capture_id), -1
        )

    def new_id(self) -> int:
        """Reserve the id for a screenshot about to be saved."""
        self.last_id = max(self.last_id, *(c.capture_id for c in self.captures), 0) + 1
        return self.last_id

    @staticmethod
    def image_name(capture_id: int) -> str:
        """Where the picture of screenshot ``capture_id`` goes, project-relative."""
        return f"captures/{capture_id:04d}.jpg"

    def add(self, capture: Capture) -> None:
        if self.get(capture.capture_id) is not None:
            raise ValueError(f"There is already a screenshot {capture.capture_id}.")
        self.captures.append(capture)
        self.last_id = max(self.last_id, capture.capture_id)
        self.sort()

    def remove(self, capture_id: int) -> Capture | None:
        index = self.index_of(capture_id)
        return self.captures.pop(index) if index >= 0 else None

    def sort(self) -> None:
        self.captures.sort(key=lambda c: (c.time, c.capture_id))

    def between(self, start: float, end: float) -> list[Capture]:
        """Screenshots of the stretch ``[start, end)``, in order."""
        return [c for c in self.captures if start <= c.time < end]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPTURES_SCHEMA_VERSION,
            "last_id": self.last_id,
            "captures": [capture.to_dict() for capture in self.captures],
        }

    @classmethod
    def from_dict(cls, data: object) -> CaptureSet:
        if not isinstance(data, dict):
            raise ValueError("captures.json does not hold an object")
        captures: list[Capture] = []
        seen: set[int] = set()
        for raw in data.get("captures") or []:
            capture = Capture.from_dict(raw)
            if capture is not None and capture.capture_id not in seen:
                seen.add(capture.capture_id)
                captures.append(capture)
        last_id = data.get("last_id")
        result = cls(
            captures=captures,
            last_id=max(
                last_id if isinstance(last_id, int) and not isinstance(last_id, bool) else 0,
                max(seen, default=0),
            ),
        )
        result.sort()
        return result


class MarkSheet:
    """The marks on one picture while they are being drawn, with undo and redo.

    Every change keeps the marks as they were before it, so undo is exact
    whatever the change was - a new mark, an erased one, an edited memo or
    clearing everything.
    """

    def __init__(self, marks: Iterable[Mark] = ()) -> None:
        self._marks: tuple[Mark, ...] = tuple(marks)
        self._undo: list[tuple[Mark, ...]] = []
        self._redo: list[tuple[Mark, ...]] = []

    @property
    def marks(self) -> tuple[Mark, ...]:
        return self._marks

    def __len__(self) -> int:
        return len(self._marks)

    def __bool__(self) -> bool:
        return bool(self._marks)

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def load(self, marks: Iterable[Mark]) -> None:
        """Start over on other marks; the history belongs to the old ones."""
        self._marks = tuple(marks)
        self._undo.clear()
        self._redo.clear()

    def add(self, mark: Mark) -> None:
        self._change(self._marks + (mark,))

    def remove(self, index: int) -> Mark:
        mark = self._marks[index]
        self._change(self._marks[:index] + self._marks[index + 1:])
        return mark

    def replace(self, index: int, mark: Mark) -> None:
        if self._marks[index] != mark:
            self._change(self._marks[:index] + (mark,) + self._marks[index + 1:])

    def clear(self) -> bool:
        """Remove every mark, as one step undo brings back. False if none."""
        if not self._marks:
            return False
        self._change(())
        return True

    def undo(self) -> bool:
        if not self._undo:
            return False
        self._redo.append(self._marks)
        self._marks = self._undo.pop()
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        self._undo.append(self._marks)
        self._marks = self._redo.pop()
        return True

    def _change(self, marks: tuple[Mark, ...]) -> None:
        self._undo.append(self._marks)
        del self._undo[:-UNDO_LIMIT]
        self._redo.clear()
        self._marks = marks
