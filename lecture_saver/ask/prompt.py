"""What the assistant is sent with a question about the lecture.

A lecture's conversation is one conversation for the assistant too: the first
question starts a session (see
:meth:`lecture_saver.translate.engine.TranslationEngine.converse`) and every
later one continues it, so the assistant has its own earlier answers in front
of it when the student says "그럼 이걸 간단히 해줘".

So there are two kinds of request:

:func:`build_prompt`
    Everything: the rules, the lecture's transcript, the conversation so far
    and the question. Starts a session - or is the whole request, for an
    engine that keeps none, or when a session could not be continued.
:func:`build_follow_up`
    The next turn of a session: only the question, where the student is if
    that changed, and transcript the session does not hold yet.

The transcript goes in pieces - a slide, or a minute of a lecture without
slides - and :class:`Request` says which pieces a request carried, with a
fingerprint of each, so a piece is sent once per session and again only if
the student corrected it since.

How much transcript goes with a question is the user's choice:

``nearby``
    The slide the question is about and the slides either side of it - or,
    for a lecture without slides, the few minutes around that moment. Quick,
    and enough for "what did the professor mean here".
``lecture``
    The whole transcript, for questions that span the lecture.

Each transcript line carries its time (``[00:12:34]``). The assistant is asked
to cite those, and :mod:`lecture_saver.ask.answer` turns them into links that
seek the video.

Same rule as everything else that leaves the computer: only words. No image,
no audio, no path or file name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..core.chat import ChatMessage, Quote
from ..core.timecode import format_duration, format_hms
from ..export.document import LectureDocument, Section, spoken
from ..export.text_formats import section_title

__all__ = [
    "DEFAULT_QUESTION",
    "SCOPE_LECTURE",
    "SCOPE_NEARBY",
    "Focus",
    "Request",
    "build_follow_up",
    "build_prompt",
    "question_text",
    "transcript_excerpt",
]

SCOPE_NEARBY = "nearby"
SCOPE_LECTURE = "lecture"

#: Slides either side of the one asked about that go with a "nearby" question.
NEARBY_SLIDES = 1
#: Without slides, the speech before and after the moment asked about.
NEARBY_BEFORE_SECONDS = 240.0
NEARBY_AFTER_SECONDS = 90.0
#: Without slides, the transcript goes in pieces of this many seconds.
PIECE_SECONDS = 60.0
#: The most transcript one request carries; hours of speech fit well inside.
MAX_TRANSCRIPT_CHARS = 300_000
#: Earlier turns sent again when a session starts on a conversation already
#: under way. The newest are kept.
HISTORY_CHARS = 12_000
#: Without slides, the student has moved on from the last question once the
#: video is this far from where it was.
MOVED_SECONDS = 60.0

#: Asked when the user points at lines and sends without typing anything.
DEFAULT_QUESTION = "이 부분이 무슨 뜻인지 쉽게 설명해 주세요."

_RULES = """\
당신은 대학 강의를 듣는 학생을 돕는 조교입니다. 학생이 강의 영상을 보다가 이해가 안 되는 \
부분을 묻습니다. 아래 강의 전사는 음성 인식으로 자동으로 만든 것이라 잘못 알아들은 단어가 \
섞여 있을 수 있습니다.

답하는 방법:
- 학생이 질문한 언어로 답하세요. 한국어로 물으면 한국어로 답하고, 전문 용어는 처음 나올 때 \
영어 원어를 괄호에 함께 적으세요.
- 질문에 먼저 바로 답한 뒤, 필요한 만큼만 자세히 설명하세요. 쉬운 비유나 간단한 예시가 \
도움이 되면 쓰세요.
- 강의에서 근거가 된 부분은 전사 줄의 시각을 [00:12:34]처럼 대괄호에 넣어 적으세요. 학생이 \
누르면 영상이 그 시점으로 이동합니다. 슬라이드는 "Slide 8"처럼 가리키세요.
- 전사가 잘못 인식된 것으로 보이면 문맥상 맞는 말로 이해하고, 그렇게 읽었다고 짧게 밝히세요.
- 강의에 나오지 않은 내용도 일반 지식으로 설명해도 되지만, 강의에서 나온 내용이 아니라고 \
밝히세요. 교수님이 하지 않은 말을 했다고 하지 마세요.
- Markdown(굵게, 목록, 코드)을 써도 됩니다. 수식은 LaTeX 대신 x², Σ, √ 같은 일반 문자로 쓰세요.
- 대화는 이어집니다. 학생이 새로 짚은 전사 부분 없이 "이거", "그럼", "더 쉽게"처럼 앞의 말을 \
받아 물으면, 대개 바로 앞의 대화, 특히 당신의 직전 답을 가리킵니다. 학생이 보고 있는 곳이 \
바뀌었을 때에만 그곳을 가리키는 말일 수 있습니다.
- 강의 전사는 대화를 시작할 때 보내고, 그 뒤로는 새로 필요해진 부분만 더 보냅니다. 앞에서 받은 \
전사도 계속 참고하세요.
- 파일을 읽거나 명령을 실행하지 말고, 여기서 받은 내용만으로 답하세요."""


@dataclass(frozen=True)
class Focus:
    """Where the user was when they asked."""

    #: Playback position, in seconds.
    position: float | None = None
    #: The number of the slide shown in the transcript pane ("Slide 8").
    slide: int | None = None
    #: Transcript lines the user pointed at.
    quotes: tuple[Quote, ...] = ()


def question_text(question: str, quotes: Sequence[Quote]) -> str:
    """The question as typed, or :data:`DEFAULT_QUESTION` when the user only
    pointed at lines. Empty when there is nothing to ask."""
    text = question.strip()
    if text:
        return text
    return DEFAULT_QUESTION if quotes else ""


# -- which transcript goes ------------------------------------------------------------


@dataclass(frozen=True)
class Piece:
    """A piece of transcript that goes to the assistant whole: a slide, or a
    stretch of time in a lecture without slides."""

    key: str
    text: str

    @property
    def fingerprint(self) -> str:
        return hashlib.sha1(self.text.encode("utf-8")).hexdigest()[:12]


def _section_of_segment(document: LectureDocument, segment_id: str) -> int | None:
    for index, section in enumerate(document.sections):
        if any(s.id == segment_id for s in section.segments):
            return index
    return None


def _section_at(document: LectureDocument, seconds: float) -> int | None:
    """The section on screen at ``seconds``; the last one that started before it."""
    found = None
    for index, section in enumerate(document.sections):
        if section.start <= seconds:
            found = index
    return found


def _anchor_sections(document: LectureDocument, focus: Focus) -> list[int]:
    """The sections the question is about: the lines quoted, else the slide
    shown, else the slide playing."""
    anchors = [
        index for q in focus.quotes
        if (index := _section_of_segment(document, q.segment_id)) is not None
    ]
    if not anchors and focus.slide is not None:
        anchors = [
            index for index, section in enumerate(document.sections)
            if section.number == focus.slide
        ]
    if not anchors and focus.position is not None:
        at = _section_at(document, focus.position)
        anchors = [at] if at is not None else []
    return sorted(set(anchors)) or [0]


def _nearby_sections(document: LectureDocument, focus: Focus) -> list[int]:
    chosen: set[int] = set()
    last = len(document.sections) - 1
    for anchor in _anchor_sections(document, focus):
        chosen.update(range(max(0, anchor - NEARBY_SLIDES), min(last, anchor + NEARBY_SLIDES) + 1))
    return sorted(chosen)


def _line(start: float, text: str) -> str:
    return f"[{format_hms(start)}] {text.strip()}"


def _section_block(section: Section) -> str:
    span = f"{format_hms(section.start)}–{format_hms(section.end)}"
    lines = [f"### {section_title(section)} ({span})"]
    lines += [_line(s.start, s.effective_text) for s in spoken(section.segments)]
    if len(lines) == 1:
        lines.append("(이 슬라이드에서는 말한 내용이 없습니다)")
    return "\n".join(lines)


def _time_pieces(document: LectureDocument, scope: str, focus: Focus) -> list[Piece]:
    """A lecture without slides, a minute a piece: all of it, or the minutes
    around the moment asked about."""
    by_minute: dict[int, list[str]] = {}
    for segment in spoken(document.segments):
        index = int(segment.start // PIECE_SECONDS)
        by_minute.setdefault(index, []).append(_line(segment.start, segment.effective_text))
    if scope != SCOPE_LECTURE:
        times = [q.start for q in focus.quotes] or [focus.position or 0.0]
        first = int(max(0.0, min(times) - NEARBY_BEFORE_SECONDS) // PIECE_SECONDS)
        last = int((max(times) + NEARBY_AFTER_SECONDS) // PIECE_SECONDS)
        by_minute = {i: lines for i, lines in by_minute.items() if first <= i <= last}
    return [Piece(f"time:{i}", "\n".join(lines)) for i, lines in sorted(by_minute.items())]


def transcript_pieces(document: LectureDocument, scope: str, focus: Focus) -> list[Piece]:
    """The transcript a question is about, in the pieces it is sent in."""
    if not document.has_slides:
        return _time_pieces(document, scope, focus)
    indices = (
        range(len(document.sections)) if scope == SCOPE_LECTURE
        else _nearby_sections(document, focus)
    )
    pieces = []
    for index in indices:
        section = document.sections[index]
        key = f"slide:{section.number}@{section.start:.1f}"
        pieces.append(Piece(key, _section_block(section)))
    return pieces


def _joined(
    pieces: Sequence[Piece], notes: dict[str, str] | None = None
) -> tuple[str, list[Piece]]:
    """The pieces' text within :data:`MAX_TRANSCRIPT_CHARS`, and the pieces
    that fitted. ``notes`` puts a line before a piece."""
    parts: list[str] = []
    fitted: list[Piece] = []
    used = 0
    separator = "\n\n" if any(p.key.startswith("slide:") for p in pieces) else "\n"
    for piece in pieces:
        text = piece.text
        note = (notes or {}).get(piece.key)
        if note:
            text = f"{note}\n{text}"
        if used + len(text) > MAX_TRANSCRIPT_CHARS:
            if not fitted:  # one piece longer than everything: its start
                parts.append(text[:MAX_TRANSCRIPT_CHARS].rstrip())
                fitted.append(piece)
            parts.append("…(이하 생략)")
            break
        parts.append(text)
        fitted.append(piece)
        used += len(text) + len(separator)
    return separator.join(parts), fitted


def transcript_excerpt(document: LectureDocument, scope: str, focus: Focus) -> str:
    """The transcript that goes with a question, one timed line per segment."""
    text, _ = _joined(transcript_pieces(document, scope, focus))
    return text or "(전사된 내용이 없습니다)"


# -- the rest of the request ---------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One question as it goes to the assistant."""

    prompt: str
    #: What the session holds once this has gone: piece key -> fingerprint.
    sent: dict[str, str] = field(default_factory=dict)


def _quote_lines(quotes: Sequence[Quote]) -> list[str]:
    return [_line(q.start, q.text) for q in quotes]


def _history(history: Sequence[ChatMessage], budget: int = HISTORY_CHARS) -> str:
    """Earlier turns, newest kept when they do not all fit."""
    turns: list[str] = []
    used = 0
    for message in reversed(history):
        if message.is_user:
            body = "\n".join([*(f"> {q}" for q in _quote_lines(message.quotes)), message.text])
            turn = f"학생: {body}"
        else:
            turn = f"조교: {message.text.strip()}"
        if used + len(turn) > budget:
            if not turns:
                turns.append(turn[: budget].rstrip() + " …")
            break
        turns.append(turn)
        used += len(turn)
    return "\n\n".join(reversed(turns))


def _where(document: LectureDocument, focus: Focus) -> str:
    parts = []
    if focus.position is not None:
        parts.append(f"영상 위치 [{format_hms(focus.position)}]")
    if focus.slide is not None:
        section = next((s for s in document.sections if s.number == focus.slide), None)
        if section is not None:
            parts.append(
                f"{section_title(section)} ({format_hms(section.start)}–{format_hms(section.end)})"
            )
    return " · ".join(parts)


def _previous_question(history: Sequence[ChatMessage]) -> ChatMessage | None:
    return next((m for m in reversed(history) if m.is_user), None)


def _moved(focus: Focus, before: ChatMessage | None) -> bool:
    """Whether the student is somewhere else than at the last question."""
    if before is None:
        return True
    if focus.slide is not None or before.slide is not None:
        return focus.slide != before.slide
    if focus.position is None or before.position is None:
        return focus.position != before.position
    return abs(focus.position - before.position) > MOVED_SECONDS


def _asked(question: str, focus: Focus) -> list[str]:
    sections = []
    if focus.quotes:
        sections.append("## 학생이 짚은 전사 부분\n" + "\n".join(_quote_lines(focus.quotes)))
    sections.append(f"## 질문\n{question_text(question, focus.quotes)}")
    return sections


def build_prompt(
    document: LectureDocument,
    question: str,
    *,
    focus: Focus,
    scope: str,
    history: Sequence[ChatMessage] = (),
) -> Request:
    """The whole request for a question: what starts a session, or all there
    is for an engine that keeps none."""
    if scope == SCOPE_LECTURE:
        scope_note = "강의 전체"
    elif document.has_slides:
        scope_note = (
            "학생이 묻는 슬라이드와 그 앞뒤 슬라이드입니다. 다른 부분은 필요해지면 더 보냅니다"
        )
    else:
        scope_note = "학생이 묻는 시점 앞뒤 몇 분입니다. 다른 부분은 필요해지면 더 보냅니다"

    transcript, pieces = _joined(transcript_pieces(document, scope, focus))
    sections = [
        _RULES,
        f"## 강의\n제목: {document.title}\n길이: {format_duration(document.duration)}",
        f"## 강의 전사 ({scope_note})\n{transcript or '(전사된 내용이 없습니다)'}",
    ]
    earlier = _history(history)
    if earlier:
        sections.append(f"## 지금까지의 대화\n{earlier}")
    where = _where(document, focus)
    if where:
        same = "" if _moved(focus, _previous_question(history)) else " (앞 질문 때와 같음)"
        sections.append(f"## 학생이 지금 보고 있는 곳{same}\n{where}")
    sections += _asked(question, focus)
    return Request("\n\n".join(sections) + "\n", {p.key: p.fingerprint for p in pieces})


def build_follow_up(
    document: LectureDocument,
    question: str,
    *,
    focus: Focus,
    scope: str,
    history: Sequence[ChatMessage],
    sent: dict[str, str],
) -> Request:
    """The next turn of a session that holds ``sent``: only what is new."""
    fresh = [
        p for p in transcript_pieces(document, scope, focus)
        if sent.get(p.key) != p.fingerprint
    ]
    notes = {
        p.key: "(학생이 이 부분의 전사를 고쳤습니다. 앞서 받은 것 대신 이것을 보세요)"
        for p in fresh if p.key in sent
    }
    transcript, pieces = _joined(fresh, notes)

    sections = []
    if transcript:
        sections.append(
            f"## 더 보내는 강의 전사 (이 질문에 필요한데 아직 보내지 않은 부분)\n{transcript}"
        )
    where = _where(document, focus)
    if where and _moved(focus, _previous_question(history)):
        sections.append(f"## 학생이 이제 보고 있는 곳\n{where}")
    sections += _asked(question, focus)
    return Request(
        "\n\n".join(sections) + "\n",
        {**sent, **{p.key: p.fingerprint for p in pieces}},
    )
