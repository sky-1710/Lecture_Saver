"""Asking the assistant for the preview or the quiz, and checking what comes back.

One request per PDF: the preview before the lecture and the quiz after it are
made when each is wanted. Whatever arrives is saved by the caller at once, the
same arrangement as translation and for the same reason: a failure in writing
the PDF must not cost the quota already spent.

Like the translation runner, this module knows nothing of Qt or the project.
It is handed a document snapshot and an engine, and reports the finished part
through a callback.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..core.reference import ReferenceText
from ..core.study import (
    ANSWER_LETTERS,
    Quiz,
    QuizQuestion,
    StudyError,
    StudyGuide,
    transcript_fingerprint,
)
from ..export.document import LectureDocument
from ..translate.engine import TranslationCancelled, TranslationEngine, TranslationError
from .prompts import QUIZ_SCHEMA, preview_prompt, quiz_prompt

log = logging.getLogger(__name__)

__all__ = [
    "PART_GUIDE",
    "PART_QUIZ",
    "StudyReport",
    "balance_answers",
    "make_guide",
    "make_quiz",
    "parse_json_answer",
    "run_study",
]

PART_GUIDE = "guide"
PART_QUIZ = "quiz"

#: Options that point at other options. Shuffling would break "Both A and B",
#: so a question containing one keeps the order it was written in.
_REFERS_TO_OPTIONS = re.compile(
    r"\b(all|none|both|neither)\s+of\s+(the\s+)?(above|these|them)\b"
    r"|\bboth\s+[A-D]\s+and\s+[A-D]\b|\b[A-D]\s+(and|or)\s+[A-D]\b",
    re.IGNORECASE,
)


def parse_json_answer(answer: str) -> Any:
    """The JSON in an answer.

    With a schema Codex returns bare JSON, but an answer wrapped in a code
    fence or preceded by a sentence is still accepted rather than thrown away
    after the quota has been spent on it.
    """
    text = answer.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    raise StudyError(
        "The assistant's answer was not in the expected format, so nothing was "
        "saved. Try again."
    )


def balance_answers(quiz: Quiz, seed: str) -> Quiz:
    """Shuffle each question's options so the answers spread over A to D.

    Language models put the right answer in the same place far more often
    than chance - a quiz where most answers are "B" teaches the letter, not
    the lecture. The shuffle is seeded from the transcript, so making the PDF
    again prints the same quiz in the same order.
    """
    rng = random.Random(seed)
    balanced: list[QuizQuestion] = []
    for question in quiz.questions:
        if any(_REFERS_TO_OPTIONS.search(o) for o in question.options):
            balanced.append(question)
            continue
        order = list(range(len(question.options)))
        rng.shuffle(order)
        options = tuple(question.options[i] for i in order)
        answer = ANSWER_LETTERS[order.index(question.answer_index)]
        balanced.append(replace(question, options=options, answer=answer))
    return Quiz(questions=tuple(balanced))


def _known_slides(document: LectureDocument) -> set[int] | None:
    if not document.has_slides:
        return set()
    return {s.number for s in document.sections if s.number is not None}


def make_guide(
    document: LectureDocument,
    engine: TranslationEngine,
    cancel: threading.Event,
    reference: ReferenceText | None = None,
    pictures: Sequence[Path] = (),
) -> StudyGuide:
    prompt = preview_prompt(document, reference, len(pictures))
    return StudyGuide.from_answer(engine.ask(prompt, cancel=cancel, images=pictures))


def make_quiz(
    document: LectureDocument,
    engine: TranslationEngine,
    cancel: threading.Event,
    reference: ReferenceText | None = None,
    pictures: Sequence[Path] = (),
) -> Quiz:
    answer = engine.ask(
        quiz_prompt(document, reference, len(pictures)),
        cancel=cancel, schema=QUIZ_SCHEMA, images=pictures,
    )
    quiz = Quiz.from_dict(parse_json_answer(answer), known_slides=_known_slides(document))
    return balance_answers(quiz, transcript_fingerprint(document.segments))


@dataclass
class StudyReport:
    made: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors and not self.cancelled


#: Called with ("guide", StudyGuide) or ("quiz", Quiz) the moment it is ready.
PartCallback = Callable[[str, object], None]


def run_study(
    document: LectureDocument,
    engine: TranslationEngine,
    part: str,
    *,
    reference: ReferenceText | None = None,
    pictures: Sequence[Path] = (),
    on_part: PartCallback | None = None,
    cancel: threading.Event | None = None,
) -> StudyReport:
    """Make ``part``, handing it back as soon as it is checked.

    ``reference`` is the text of the lecture's PDF, which both are written from,
    and ``pictures`` are of its pages.
    """
    stop = cancel or threading.Event()
    report = StudyReport()
    if stop.is_set():
        report.cancelled = True
        return report
    value: object
    try:
        if part == PART_GUIDE:
            value = make_guide(document, engine, stop, reference, pictures)
        else:
            value = make_quiz(document, engine, stop, reference, pictures)
    except TranslationCancelled:
        report.cancelled = True
        return report
    except (TranslationError, StudyError) as exc:
        log.warning("Study %s failed: %s", part, exc)
        report.errors.append(str(exc))
        return report
    except Exception as exc:  # noqa: BLE001 - a failure must reach the user as a message
        log.exception("Unexpected failure making the study %s", part)
        report.errors.append(f"Unexpected failure:\n{exc}")
        return report
    report.made.append(part)
    if on_part is not None:
        on_part(part, value)
    return report
