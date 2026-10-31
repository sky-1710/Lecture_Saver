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

What never leaves, on any path: the video, the audio, the pictures taken from
the video. An engine is given a string and returns a string; it is not handed
the project or its paths. A lecture's attached PDF goes as the words on its
pages, in the prompt, never as the file. The one thing that is not a string is
for the preview and the quiz: pictures of those same pages, which a diagram
needs, sent only when the window says so and with the box for them ticked.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
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
        images: Sequence[Path] = (),
    ) -> str:
        """Send ``prompt``; with ``schema``, the answer must be JSON of that shape.

        The review's quiz uses this: a quiz is data, and an engine that can make
        the model keep to a JSON schema gives far more dependable data than
        one whose answer has to be dug out of prose. An engine that cannot
        enforce a schema says so rather than returning something unchecked.
        ``images`` go with the prompt, in order, for an engine that can show them.
        """
        if schema is not None:
            raise TranslationError(f"{self.label} cannot return structured answers.")
        if images:
            raise TranslationError(f"{self.label} cannot be shown pictures.")
        return self.translate(prompt, cancel=cancel)

    #: Whether :meth:`converse` can carry a conversation on in one session.
    supports_sessions: bool = False

    def converse(
        self, prompt: str, *, session: str | None = None, cancel: threading.Event
    ) -> tuple[str, str | None]:
        """One turn of a conversation: the answer, and the session it is in.

        Ask AI uses this. With ``session`` the turn continues that session,
        which already holds everything sent in it before, so ``prompt`` need
        only carry what is new. An engine without sessions answers each turn
        on its own and returns no session; its prompts must carry everything.
        """
        return self.ask(prompt, cancel=cancel), None

    def forget_session(self, session: str) -> None:  # noqa: B027 - nothing kept by default
        """Delete whatever this computer keeps of a conversation that ended."""
