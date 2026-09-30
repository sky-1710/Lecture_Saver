"""Driving an engine through a lecture, one batch at a time.

The runner is deliberately ignorant of Qt and of the project. It is handed a
document snapshot and an engine, and it calls back with each answer as that
answer arrives. Saving is the caller's job, which is what makes the whole
thing resumable: by the time a later batch fails, the earlier ones are
already on disk (§26).

Nothing is retried automatically. A failed batch has already cost the user
part of their quota, and the usual cause - the usage limit - would fail again
straight away. Running the translation a second time skips whatever is
already translated, so a re-run is both the retry and the resume.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from ..core.translations import parse_translation_text
from ..export.ai_request import (
    REQUEST_EXAMPLE,
    REQUEST_INTRO,
    REQUEST_RULES,
)
from ..export.document import LectureDocument
from .batching import Batch, plan_batches, translatable_sections
from .engine import (
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

log = logging.getLogger(__name__)

__all__ = [
    "BatchOutcome",
    "TranslationReport",
    "batch_prompt",
    "run_translation",
]

#: Replaces the "make me a .md and a .pdf" section of the hand-carried
#: request. Nothing needs to be produced as a file here - the answer is read
#: straight back into the project - and asking for files would have Codex try
#: to write them into its sandbox instead of answering.
_AUTOMATED_DELIVERABLE = """
## 결과물

번역 결과만 출력해 주세요. 파일을 만들지 말고, 설명이나 인사말도 붙이지 마세요.
아래에 주어진 슬라이드를 하나도 빠뜨리지 말고 전부 번역해 주세요.
"""


def batch_prompt(lecture: str) -> str:
    """The full instruction sent for one batch.

    The rules are repeated in every batch on purpose. Each batch is its own
    Codex run with no memory of the last one, so instructions given once would
    reach only the first.
    """
    return (
        REQUEST_INTRO
        + _AUTOMATED_DELIVERABLE
        + REQUEST_RULES
        + REQUEST_EXAMPLE
        + "\n---\n\n아래부터 강의 전사입니다.\n\n"
        + lecture
    )


@dataclass(frozen=True)
class BatchOutcome:
    """What came of one request."""

    batch: Batch
    #: The engine's answer, as text. Empty when the batch failed.
    answer: str = ""
    #: Slide numbers the answer actually carried.
    translated: tuple[int, ...] = ()
    #: Numbers that were asked for and did not come back.
    missing: tuple[int, ...] = ()
    #: A message fit to show the user, or "" when the batch succeeded.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


@dataclass
class TranslationReport:
    """The whole run, for the message shown at the end."""

    total_batches: int = 0
    translated: list[int] = field(default_factory=list)
    missing: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    #: Slides that already had a translation and were not sent again.
    skipped: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.translated) and not self.errors and not self.missing


#: Called with each answer the moment it arrives, so the caller can save it.
BatchCallback = Callable[[BatchOutcome], None]
#: Called with (batches finished, batches in total) before each request.
ProgressCallback = Callable[[int, int], None]


def run_translation(
    document: LectureDocument,
    engine: TranslationEngine,
    *,
    skip: set[int] | None = None,
    on_batch: BatchCallback | None = None,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
    max_chars: int | None = None,
    max_slides: int | None = None,
) -> TranslationReport:
    """Translate every slide of ``document`` that is not in ``skip``."""
    stop = cancel or threading.Event()
    report = TranslationReport(skipped=tuple(sorted(skip or ())))

    sections = translatable_sections(document, skip=skip)
    if not sections:
        return report

    sizes = {k: v for k, v in (("max_chars", max_chars), ("max_slides", max_slides)) if v}
    batches = plan_batches(sections, **sizes)
    report.total_batches = len(batches)

    for index, batch in enumerate(batches):
        if stop.is_set():
            report.cancelled = True
            break
        if on_progress is not None:
            on_progress(index, len(batches))

        outcome = _run_batch(batch, engine, stop)
        if isinstance(outcome, TranslationCancelled):
            report.cancelled = True
            break

        report.translated += list(outcome.translated)
        report.missing += list(outcome.missing)
        if outcome.error:
            report.errors.append(outcome.error)
        if on_batch is not None and outcome.answer:
            on_batch(outcome)

        # The usage limit will not clear within a run, so stopping keeps the
        # remaining batches from failing one after another for the same reason.
        if outcome.error and _is_fatal(outcome.error):
            break

    if on_progress is not None:
        on_progress(len(batches), len(batches))
    return report


#: Errors that will hit every remaining batch just the same.
_FATAL_MARKERS = ("usage limit", "not signed in", "could not be found", "codex login")


def _is_fatal(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _FATAL_MARKERS)


def _run_batch(
    batch: Batch, engine: TranslationEngine, stop: threading.Event
) -> BatchOutcome | TranslationCancelled:
    from .batching import batch_markdown

    try:
        answer = engine.translate(batch_prompt(batch_markdown(batch)), cancel=stop)
    except TranslationCancelled as exc:
        return exc
    except TranslationError as exc:
        log.warning("Translation batch failed: %s", exc)
        return BatchOutcome(batch=batch, missing=batch.numbers, error=str(exc))
    except Exception as exc:  # noqa: BLE001 - a batch must not kill the run
        log.exception("Unexpected failure translating a batch")
        return BatchOutcome(
            batch=batch, missing=batch.numbers, error=f"Unexpected failure:\n{exc}"
        )

    # Parsing here, on the worker thread, means the caller is told which
    # slides really came back rather than having to trust the request.
    parsed = parse_translation_text(answer)
    asked = set(batch.numbers)
    translated = tuple(sorted(n for n in parsed.slides if n in asked))
    missing = tuple(sorted(asked - set(translated)))
    return BatchOutcome(batch=batch, answer=answer, translated=translated, missing=missing)
