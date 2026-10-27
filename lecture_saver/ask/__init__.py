"""Asking an AI assistant about the lecture while watching it.

Like :mod:`lecture_saver.translate` and :mod:`lecture_saver.study`, this sends
lecture *text* off the computer, and only once the user has agreed to that.
It reuses their engine (see :mod:`lecture_saver.translate.engine`): a question
is a prompt in and an answer out - and, with an engine that keeps sessions,
a turn of one conversation that goes on until *New chat*.

``prompt``
    Which part of the transcript goes with a question, and how it is asked.
``answer``
    The answer's citations turned into links back into the lecture.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.chat import ChatSession
from ..translate.engine import (
    EngineAuthError,
    EngineNotFoundError,
    EngineRateLimited,
    TranslationCancelled,
    TranslationError,
)

if TYPE_CHECKING:
    from ..translate.engine import TranslationEngine
    from .prompt import Request

log = logging.getLogger(__name__)

__all__ = ["Reply", "ask_engine_from_settings", "ask_question", "forget_session"]


def ask_engine_from_settings(settings: object) -> TranslationEngine:
    """The engine questions go to: the translation engine, with the model set
    for questions (``ask_model``) when there is one.

    Answering a question can deserve a stronger model than translating a
    line, so it can be chosen apart from the translation's affordable default.
    """
    from ..translate import engine_from_settings
    from ..translate.codex_cli import CodexEngine

    engine = engine_from_settings(settings)
    get = getattr(settings, "get", lambda *_: None)
    model = get("ask_model", "") or ""
    if model and isinstance(engine, CodexEngine):
        engine.model = model
    return engine


@dataclass(frozen=True)
class Reply:
    """The answer to a question, and the session the conversation is now in."""

    text: str
    #: Where the next question continues; ``None`` for an engine that keeps none.
    session: ChatSession | None
    #: The session given up on, if one was: its files can go.
    dropped: ChatSession | None = None


def ask_question(
    engine: TranslationEngine,
    fresh: Request,
    *,
    follow_up: Request | None = None,
    session: ChatSession | None = None,
    cancel: threading.Event,
) -> Reply:
    """Ask in the conversation's session if it has one, else start one.

    ``follow_up`` is the question as a turn of ``session``; ``fresh`` the same
    question carrying everything. A session that cannot be continued - its
    file gone, a newer Codex, a broken run - is replaced by a fresh one rather
    than failing the question. A usage limit, a missing sign-in or a stop
    would end a fresh start the same way, so those are not tried again.
    """
    if follow_up is not None and session is not None:
        try:
            text, continued = engine.converse(follow_up.prompt, session=session.id, cancel=cancel)
            return Reply(text, ChatSession(continued or session.id, engine.key, follow_up.sent))
        except (TranslationCancelled, EngineRateLimited, EngineAuthError, EngineNotFoundError):
            raise
        except TranslationError as exc:
            log.info("The conversation could not be continued; starting it afresh: %s", exc)
    text, started = engine.converse(fresh.prompt, cancel=cancel)
    new = ChatSession(started, engine.key, fresh.sent) if started else None
    return Reply(text, new, dropped=session)


def forget_session(session: ChatSession | None) -> None:
    """Delete what this computer keeps of a conversation that has ended."""
    if session is None or not session.id:
        return
    if session.engine == "codex":
        from ..translate.codex_cli import forget_codex_session

        forget_codex_session(session.id)
