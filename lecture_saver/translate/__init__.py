"""Translating a lecture with an AI assistant, in the application.

This package is the one part of Lecture Saver that can send lecture content
off the computer, and it only does so when the user turns it on. See
:mod:`lecture_saver.translate.engine` for what an engine is allowed to touch.

Everything else - importing an answer the user got elsewhere, storing it,
showing it, exporting it - lives in :mod:`lecture_saver.core.translations` and
works whether or not this package is ever used.
"""

from __future__ import annotations

from .engine import (
    EngineAuthError,
    EngineAvailability,
    EngineNotFoundError,
    EngineRateLimited,
    TranslationCancelled,
    TranslationEngine,
    TranslationError,
)

__all__ = [
    "EngineAuthError",
    "EngineAvailability",
    "EngineNotFoundError",
    "EngineRateLimited",
    "TranslationCancelled",
    "TranslationEngine",
    "TranslationError",
    "available_engines",
    "engine_from_settings",
]


def available_engines() -> list[type[TranslationEngine]]:
    """The engines this build knows about, in the order they are offered.

    A list rather than a hard-coded class so a second assistant is one entry
    here plus its module: the runner, the worker and the dialog all work
    against :class:`TranslationEngine` and need no change.
    """
    from .codex_cli import CodexEngine

    return [CodexEngine]


def engine_from_settings(settings: object) -> TranslationEngine:
    """Build the engine the user chose, with the options they set."""
    from .codex_cli import CodexEngine

    get = getattr(settings, "get", lambda *_: None)
    return CodexEngine(
        binary=get("translate_codex_path", "") or None,
        model=get("translate_model", "") or "",
    )
