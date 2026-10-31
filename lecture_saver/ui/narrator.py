"""Reading a lesson aloud, with the voice this computer already has.

Qt speaks through Windows' own speech service: nothing is sent anywhere, and
nothing is installed. A computer without a Korean voice says so rather than
reading Korean as English or not at all (§32).

A paragraph is said at a time, so that a pause or a step to the next part takes
effect at once and the screen can follow.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from PySide6.QtCore import QLocale, QObject, Signal

log = logging.getLogger(__name__)

__all__ = ["NO_VOICE", "Narrator"]

NO_VOICE = "이 컴퓨터에는 한국어 음성이 없습니다. Windows 음성 설정에서 추가해 주세요."

#: Windows' newer speech service first; both use the voices Windows has.
_ENGINES = ("winrt", "sapi")


def _korean() -> QLocale:
    return QLocale(QLocale.Language.Korean, QLocale.Country.SouthKorea)


def _make_speech(parent: QObject) -> object | None:
    from PySide6.QtTextToSpeech import QTextToSpeech

    engines = QTextToSpeech.availableEngines()
    for name in _ENGINES:
        if name in engines:
            return QTextToSpeech(name, parent)
    return None


class Narrator(QObject):
    """Says paragraphs one after another, and says when it has finished."""

    #: The last paragraph has been said.
    finished = Signal()
    #: The paragraph about to be said, by its place in what was given.
    paragraphStarted = Signal(int)
    #: Reading started, paused, resumed or stopped.
    stateChanged = Signal()

    def __init__(self, parent: QObject | None = None, speech: object | None = None) -> None:
        super().__init__(parent)
        self._speech = speech
        self._made = speech is not None
        self._paragraphs: tuple[str, ...] = ()
        self._next = 0
        self._reading = False
        self._paused = False
        self._rate = 0.0
        self._ready = False

    @property
    def is_reading(self) -> bool:
        """Between being given paragraphs and finishing or being stopped."""
        return self._reading

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _engine(self) -> object | None:
        if not self._made:
            self._made = True
            try:
                self._speech = _make_speech(self)
            except Exception:  # noqa: BLE001 - no speech must not take the lecture down
                log.warning("Could not start speech", exc_info=True)
                self._speech = None
        speech = self._speech
        if speech is not None and not self._ready:
            speech.stateChanged.connect(self._on_state)  # type: ignore[attr-defined]
            self._ready = True
        return speech

    def unavailable_reason(self) -> str:
        """Why nothing can be read aloud, or "" when it can."""
        speech = self._engine()
        if speech is None:
            return NO_VOICE
        korean = _korean()
        locales = speech.availableLocales()  # type: ignore[attr-defined]
        if not any(locale.language() == korean.language() for locale in locales):
            return NO_VOICE
        return ""

    def set_rate(self, rate: float) -> None:
        """-1 is slowest and 1 fastest; 0 is the voice's own speed."""
        self._rate = rate
        speech = self._engine()
        if speech is not None:
            speech.setRate(rate)  # type: ignore[attr-defined]

    def read(self, paragraphs: Sequence[str]) -> None:
        """Start saying ``paragraphs``, instead of whatever was being said."""
        speech = self._engine()
        if speech is None:
            return
        self.stop()
        self._paragraphs = tuple(paragraphs)
        self._next = 0
        self._reading = True
        speech.setLocale(_korean())  # type: ignore[attr-defined]
        speech.setRate(self._rate)  # type: ignore[attr-defined]
        self.stateChanged.emit()
        self._say_next()

    def toggle_pause(self) -> None:
        speech = self._speech
        if speech is None or not self._reading:
            return
        self._paused = not self._paused
        if self._paused:
            speech.pause()  # type: ignore[attr-defined]
        else:
            speech.resume()  # type: ignore[attr-defined]
        self.stateChanged.emit()

    def stop(self) -> None:
        if not self._reading:
            return
        # First, or the voice's stopping would read as a paragraph having ended.
        self._reading = self._paused = False
        self._paragraphs = ()
        if self._speech is not None:
            self._speech.stop()  # type: ignore[attr-defined]
        self.stateChanged.emit()

    def _say_next(self) -> None:
        if self._next >= len(self._paragraphs):
            self._reading = False
            self.stateChanged.emit()
            self.finished.emit()
            return
        index = self._next
        self._next += 1
        self.paragraphStarted.emit(index)
        self._speech.say(self._paragraphs[index])  # type: ignore[union-attr]

    def _on_state(self, state: object) -> None:
        from PySide6.QtTextToSpeech import QTextToSpeech

        if not self._reading or self._paused:
            return
        if state == QTextToSpeech.State.Ready:
            self._say_next()
        elif state == QTextToSpeech.State.Error:
            log.warning("Speech failed: %s", self._speech.errorString())  # type: ignore[union-attr]
            self.stop()
