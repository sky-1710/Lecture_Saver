"""What a translation engine is, and the errors one may raise.

An engine takes a prompt and gives back the assistant's answer as text. That
is the whole contract: everything above it - how the lecture is cut into
batches, how the answer is parsed, how it reaches the slides - is the same
whichever engine runs (§25). Adding another assistant means adding one class
here, not touching the runner or the UI.

THE ONE EXCEPTION TO §1
-----------------------
Every other part of Lecture Saver runs on this machine. An engine does not:
it hands the lecture's *text* to an assistant, which for a CLI-backed engine
means the transcript leaves the computer. That is why translation is off until
the user turns it on, and why the confirmation before the first run says so
plainly.

What never leaves, on any path: the video, the audio, the slide images. An
engine is given a string and returns a string; it is not handed the project,
its files or their paths.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

__all__ = [
    "EngineAvailability",
    "EngineAuthError",
    "EngineNotFoundError",
    "EngineRateLimited",
    "TranslationCancelled",
    "TranslationEngine",
    "TranslationError",
]


class TranslationError(RuntimeError):
    """Base class. ``str(err)`` is always safe to show a user (§28)."""


class EngineNotFoundError(TranslationError):
    """The assistant's program is not installed, or could not be located."""


class EngineAuthError(TranslationError):
    """The program is there but is not logged in."""


class EngineRateLimited(TranslationError):
    """The account's usage limit was reached.

    Worth its own class because it is the one failure that is neither the
    user's mistake nor a bug: the work done so far is kept and the rest can be
    picked up later, so the message says that rather than apologising.
    """


class TranslationCancelled(TranslationError):
    """The user stopped the translation. Not a failure."""


@dataclass(frozen=True)
class EngineAvailability:
    """Whether an engine can run right now, and what to say if it cannot."""

    ready: bool
    #: Shown under the engine's name: the version, or why it is unusable.
    detail: str = ""
    #: What the user would have to do about it, when there is something.
    remedy: str = ""


class TranslationEngine(ABC):
    """One assistant, reachable as ``prompt in -> answer out``."""

    #: Stable identifier stored in settings and in ``translations.json``.
    key: str = ""
    #: Shown in the engine picker.
    label: str = ""
    #: Whether using this engine sends the transcript off this computer.
    #: An engine that runs a local model would set this False and the warning
    #: before the first run would not appear.
    sends_text_externally: bool = True

    @abstractmethod
    def availability(self) -> EngineAvailability:
        """Check without running anything that costs the user quota."""

    @abstractmethod
    def translate(self, prompt: str, *, cancel: threading.Event) -> str:
        """Send ``prompt``, return the answer as text.

        Implementations poll ``cancel`` while waiting and raise
        :class:`TranslationCancelled` once it is set, so stopping a long
        lecture takes effect in seconds rather than at the end of the batch.
        """

    def ask(
        self,
        prompt: str,
        *,
        cancel: threading.Event,
        schema: dict[str, Any] | None = None,
    ) -> str:
        """Send ``prompt``; with ``schema``, the answer must be JSON of that shape.

        The study pack uses this: a quiz is data, and an engine that can make
        the model keep to a JSON schema gives far more dependable data than
        one whose answer has to be dug out of prose. An engine that cannot
        enforce a schema says so rather than returning something unchecked.
        """
        if schema is not None:
            raise TranslationError(f"{self.label} cannot return structured answers.")
        return self.translate(prompt, cancel=cancel)
