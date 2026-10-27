"""Asking an AI assistant about the lecture while watching it.

Like :mod:`lecture_saver.translate` and :mod:`lecture_saver.study`, this sends
lecture *text* off the computer, and only once the user has agreed to that.
It reuses their engine (see :mod:`lecture_saver.translate.engine`): a question
is a prompt in and an answer out.

``prompt``
    Which part of the transcript goes with a question, and how it is asked.
``answer``
    The answer's citations turned into links back into the lecture.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..translate.engine import TranslationEngine

__all__ = ["ask_engine_from_settings"]


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
