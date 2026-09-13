"""Undo / redo for transcript edits (§13).

Each command records the transcript as it was *before* the command. That is
simpler and far harder to get wrong than a pair of do/undo functions per
operation: merge, split, delete, insert and replace-all all undo the same way,
by putting the previous segments back.

A lecture transcript is a few thousand small records, so a snapshot per command
is cheap, and the history is capped.

Typing is the exception to "one command, one step". A person editing a sentence
produces many changes to one segment; those are *coalesced* into a single undo
step until the editing session ends, so undo takes back the sentence rather
than one character.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .models import TranscriptSegment

__all__ = ["EditHistory", "HistoryEntry", "copy_segments"]

#: Enough for a long editing session; each entry is one transcript snapshot.
DEFAULT_LIMIT = 200


def copy_segments(segments: list[TranscriptSegment]) -> list[TranscriptSegment]:
    """Independent copies, so later edits cannot reach into a snapshot."""
    return [replace(segment) for segment in segments]


@dataclass
class HistoryEntry:
    #: Short user-facing name, e.g. "Merge segments", for "Undo Merge segments".
    label: str
    segments: list[TranscriptSegment]


class EditHistory:
    """Two stacks of snapshots plus the currently open coalescing session."""

    def __init__(self, limit: int = DEFAULT_LIMIT) -> None:
        self._limit = max(1, limit)
        self._undo: list[HistoryEntry] = []
        self._redo: list[HistoryEntry] = []
        self._open_key: str | None = None

    # -- queries --------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    @property
    def open_key(self) -> str | None:
        return self._open_key

    # -- recording --------------------------------------------------------------

    def record(
        self,
        label: str,
        before: list[TranscriptSegment],
        *,
        coalesce_key: str | None = None,
    ) -> None:
        """Remember ``before`` as the state a new command will change.

        With a ``coalesce_key`` matching the open session nothing is recorded:
        the earlier snapshot already holds the state before the typing began.
        """
        if coalesce_key is not None and coalesce_key == self._open_key:
            return
        self._open_key = coalesce_key
        self._undo.append(HistoryEntry(label, copy_segments(before)))
        if len(self._undo) > self._limit:
            del self._undo[0]
        self._redo.clear()

    def close_session(self, current: list[TranscriptSegment]) -> None:
        """End a coalescing session, dropping it if it changed nothing."""
        if self._open_key is None:
            return
        self._open_key = None
        if self._undo and self._undo[-1].segments == current:
            self._undo.pop()

    def discard_last(self) -> None:
        """Forget the most recent record - for a command that turned out a no-op."""
        if self._undo:
            self._undo.pop()
        self._open_key = None

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._open_key = None

    # -- stepping ---------------------------------------------------------------

    def undo(self, current: list[TranscriptSegment]) -> HistoryEntry | None:
        """Pop the last command; ``current`` becomes redoable."""
        self.close_session(current)
        if not self._undo:
            return None
        entry = self._undo.pop()
        self._redo.append(HistoryEntry(entry.label, copy_segments(current)))
        return entry

    def redo(self, current: list[TranscriptSegment]) -> HistoryEntry | None:
        self.close_session(current)
        if not self._redo:
            return None
        entry = self._redo.pop()
        self._undo.append(HistoryEntry(entry.label, copy_segments(current)))
        return entry
