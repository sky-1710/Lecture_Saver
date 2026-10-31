"""Study material made from a lecture: a preview to read first, and a quiz.

The assistant writes it (see :mod:`lecture_saver.study`); this module only
defines what it is, checks it, and stores it in ``study.json`` next to the
transcript. Nothing here talks to any service.

Two rules shape everything below.

**An answer is checked, not trusted.** A quiz whose answer letter points at no
option, or a "slide 40" in a 29-slide lecture, would go straight into a PDF a
student studies from. So the quiz is validated field by field on the way in,
and anything that cannot be repaired safely is refused with a message saying
what was wrong - never silently printed. The preview is a teacher's long
explanation rather than data, so it is kept as the Markdown it was written in,
and only refused when it plainly is not the document that was asked for.

**Saved means free.** The preview and the quiz are each stored the moment
they arrive. Making a PDF again - after correcting the transcript, or with
different slide options - reads them back from disk and costs no quota.
The transcript's fingerprint is stored with them, so the application can say
when they were made from a transcript that has since been edited.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .models import SCHEMA_VERSION, TranscriptSegment, utc_now_iso

__all__ = [
    "ANSWER_LETTERS",
    "QUIZ_LENGTH",
    "Quiz",
    "QuizQuestion",
    "StudyError",
    "StudyGuide",
    "StudySet",
    "transcript_fingerprint",
]

#: How many questions the review quiz has.
QUIZ_LENGTH = 15
ANSWER_LETTERS = ("A", "B", "C", "D")

#: "A. text", "(B) text", "C) text", "d: text" - a letter an assistant put in
#: front of an option despite being asked not to. The PDF adds its own.
_OPTION_PREFIX = re.compile(r"^\s*\(?([A-Da-d])\s*[.):]\s+")


class StudyError(ValueError):
    """The assistant's answer cannot be used. ``str(err)`` is fit to show."""


def transcript_fingerprint(
    segments: list[TranscriptSegment] | tuple[TranscriptSegment, ...],
) -> str:
    """A short hash of the text the study material was made from.

    Uses ``effective_text``, so correcting a word changes it and re-timing a
    segment does not: only an edit to what was *said* can make the material
    out of date.
    """
    digest = hashlib.sha256()
    for segment in segments:
        digest.update(segment.effective_text.strip().encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:16]


# -- small helpers for untrusted input ------------------------------------------------


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v) for v in value) if t]


def _slides(value: object, known: set[int] | None) -> list[int]:
    """Slide numbers, keeping only ones this lecture has, in order, once each."""
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        if (known is None or number in known) and number not in numbers:
            numbers.append(number)
    return sorted(numbers)


# -- the preview ------------------------------------------------------------------------

#: An answer shorter than this cannot be the long lesson that was asked for: it
#: is an apology, a question back, or the start of something cut off.
MIN_PREVIEW_CHARS = 1000

#: The whole answer wrapped in one code block, against the instructions.
_WRAPPED = re.compile(r"^```(?:markdown|md)?[ \t]*\n(.*)\n```$", re.DOTALL | re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


@dataclass(frozen=True)
class StudyGuide:
    """What to read before the lecture: Markdown, as the assistant wrote it."""

    markdown: str

    def to_dict(self) -> dict[str, Any]:
        return {"markdown": self.markdown}

    @classmethod
    def from_answer(cls, answer: str) -> StudyGuide:
        """Check an assistant's answer; refuse one that is not the document."""
        text = answer.strip()
        wrapped = _WRAPPED.match(text)
        if wrapped:
            text = wrapped.group(1).strip()
        if len(text) < MIN_PREVIEW_CHARS or not _HEADING.search(text):
            raise StudyError(
                "The preview came back too short to be the lesson that was asked "
                "for, so it was not saved. Try again."
            )
        return cls(markdown=text)

    @classmethod
    def from_dict(cls, data: object) -> StudyGuide:
        """Read what was stored. A preview of an older shape reads as damaged."""
        markdown = _text(data.get("markdown")) if isinstance(data, dict) else ""
        if not markdown:
            raise StudyError("The saved preview could not be read.")
        return cls(markdown=markdown)


# -- the quiz ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuizQuestion:
    question: str
    #: Exactly four, without letters - the PDF adds "A." to "D.".
    options: tuple[str, ...]
    #: "A" to "D".
    answer: str
    #: Why the answer is right, in Korean.
    explanation: str
    slides: tuple[int, ...] = ()

    @property
    def answer_index(self) -> int:
        return ANSWER_LETTERS.index(self.answer)

    @property
    def answer_text(self) -> str:
        return self.options[self.answer_index]

    @classmethod
    def from_dict(
        cls, data: object, *, known_slides: set[int] | None = None
    ) -> QuizQuestion | None:
        """A question, or ``None`` if it cannot be printed without guessing."""
        if not isinstance(data, dict):
            return None
        question = _text(data.get("question"))
        options = tuple(_OPTION_PREFIX.sub("", o) for o in _texts(data.get("options")))
        answer = _text(data.get("answer")).upper()[:1]
        # A question with three options, or an answer pointing past the last
        # one, cannot be fixed without inventing something. Drop it.
        if not question or len(options) != len(ANSWER_LETTERS) or answer not in ANSWER_LETTERS:
            return None
        if len(set(options)) != len(options):
            return None
        return cls(
            question=question,
            options=options,
            answer=answer,
            explanation=_text(data.get("explanation")),
            slides=tuple(_slides(data.get("slides"), known_slides)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": list(self.options),
            "answer": self.answer,
            "explanation": self.explanation,
            "slides": list(self.slides),
        }


@dataclass(frozen=True)
class Quiz:
    questions: tuple[QuizQuestion, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"questions": [q.to_dict() for q in self.questions]}

    @classmethod
    def from_dict(
        cls, data: object, *, known_slides: set[int] | None = None, length: int = QUIZ_LENGTH
    ) -> Quiz:
        """Keep the first ``length`` usable questions; refuse if there are fewer."""
        raw = data.get("questions", []) if isinstance(data, dict) else []
        usable = [
            q for q in (QuizQuestion.from_dict(item, known_slides=known_slides) for item in raw)
            if q is not None
        ]
        if len(usable) < length:
            raise StudyError(
                f"The quiz came back with {len(usable)} usable questions instead of "
                f"{length}, so it was not saved. Try again."
            )
        return cls(questions=tuple(usable[:length]))


# -- storage -----------------------------------------------------------------------------


@dataclass
class StudySet:
    """Everything ``study.json`` holds. Either part may be missing."""

    guide: StudyGuide | None = None
    quiz: Quiz | None = None
    #: Fingerprint of the transcript each part was made from.
    guide_source: str = ""
    quiz_source: str = ""
    guide_made_at: str | None = None
    quiz_made_at: str | None = None
    engine: str = ""
    schema_version: int = field(default=SCHEMA_VERSION)

    @property
    def is_empty(self) -> bool:
        return self.guide is None and self.quiz is None

    def set_guide(self, guide: StudyGuide, *, source: str, engine: str = "") -> None:
        self.guide = guide
        self.guide_source = source
        self.guide_made_at = utc_now_iso()
        if engine:
            self.engine = engine

    def set_quiz(self, quiz: Quiz, *, source: str, engine: str = "") -> None:
        self.quiz = quiz
        self.quiz_source = source
        self.quiz_made_at = utc_now_iso()
        if engine:
            self.engine = engine

    def stale_parts(self, fingerprint: str) -> list[str]:
        """Which saved parts were made from a transcript that has since changed."""
        stale = []
        if self.guide is not None and self.guide_source != fingerprint:
            stale.append("preview")
        if self.quiz is not None and self.quiz_source != fingerprint:
            stale.append("quiz")
        return stale

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "engine": self.engine,
            "guide": self.guide.to_dict() if self.guide else None,
            "guide_source": self.guide_source,
            "guide_made_at": self.guide_made_at,
            "quiz": self.quiz.to_dict() if self.quiz else None,
            "quiz_source": self.quiz_source,
            "quiz_made_at": self.quiz_made_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> StudySet:
        """Read ``study.json``. A damaged part is dropped, not the whole file."""
        if not data:
            return cls()
        guide = quiz = None
        try:
            if data.get("guide"):
                guide = StudyGuide.from_dict(data["guide"])
        except StudyError:
            guide = None
        try:
            if data.get("quiz"):
                quiz = Quiz.from_dict(data["quiz"])
        except StudyError:
            quiz = None
        return cls(
            guide=guide,
            quiz=quiz,
            guide_source=str(data.get("guide_source", "")) if guide else "",
            quiz_source=str(data.get("quiz_source", "")) if quiz else "",
            guide_made_at=data.get("guide_made_at") if guide else None,
            quiz_made_at=data.get("quiz_made_at") if quiz else None,
            engine=str(data.get("engine", "")),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
