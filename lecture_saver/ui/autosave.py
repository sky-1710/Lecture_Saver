"""Debounced autosave for transcript edits (§15).

Writing ``transcript.json`` after every keystroke would hammer the disk on a long
lecture; waiting for an explicit save would lose work on a crash. Instead every
change restarts a short timer, and the file is written once typing pauses.

The write itself is atomic (see :mod:`lecture_saver.core.atomic_io`), so a crash
mid-save leaves the previous complete file, never a truncated one. Closing a
project or the application flushes immediately rather than waiting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Signal

log = logging.getLogger(__name__)

__all__ = ["Autosave", "DEFAULT_DELAY_MS"]

#: Inside the 500-1000 ms the specification suggests: long enough to cover a
#: burst of typing, short enough that a crash costs under a second of work.
DEFAULT_DELAY_MS = 750


class Autosave(QObject):
    """Calls ``save`` once changes have stopped for ``delay_ms``."""

    #: Emitted after a successful write.
    saved = Signal()
    #: Emitted with a user-facing message when a write fails. The changes stay
    #: pending, so the next change or flush tries again.
    failed = Signal(str)

    def __init__(
        self,
        save: Callable[[], bool],
        delay_ms: int = DEFAULT_DELAY_MS,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._save = save
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self.flush)

    @property
    def pending(self) -> bool:
        return self._timer.isActive()

    def schedule(self) -> None:
        """A change happened: (re)start the countdown."""
        self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()

    def flush(self) -> bool:
        """Write now if there is anything to write. Returns False on failure."""
        self._timer.stop()
        try:
            wrote = self._save()
        except Exception as exc:  # noqa: BLE001 - a failed save must be reported, not raised
            log.warning("Autosave failed", exc_info=True)
            self.failed.emit(f"Could not save your edits: {exc}")
            return False
        if wrote:
            self.saved.emit()
        return True
