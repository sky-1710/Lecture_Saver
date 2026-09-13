"""The single authoritative transcript (§14).

Exactly one :class:`TranscriptStore` exists per open project. The slide view,
the full transcript view and every exporter read from this object - none of them
keeps its own copy - so they cannot drift apart.

Every change goes through a method here: replacing the transcript after
transcription, recording slide links, and the editing operations from §13 -
edit, restore original, insert, delete, merge, split, find and replace, and
resetting the whole transcript to the original transcription - each of them
undoable.

Two rules hold for every edit:

* ``original_text`` is Whisper's output and is never rewritten by editing text.
  Edits go to ``edited_text``; an edit that makes the text identical to the
  original clears ``edited_text`` again, so "edited" always means "differs".
* Merge and split keep the original too: a merged segment's original is both
  originals joined, and split divides the original alongside the text, so
  "restore original" still has something truthful to restore.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .atomic_io import read_json, write_json_atomic
from .edit_history import EditHistory, copy_segments
from .models import Transcript, TranscriptSegment, make_segment_id

__all__ = ["TextMatch", "TranscriptEditError", "TranscriptStore"]

#: A segment inserted by hand lasts this long unless the gap is shorter.
NEW_SEGMENT_SECONDS = 2.0
#: Below this, a gap is too small to hold an inserted segment on its own.
MIN_GAP_SECONDS = 1.0

_ID_NUMBER = re.compile(r"^segment_(\d+)$")


class TranscriptEditError(ValueError):
    """An edit could not be applied. The message is safe to show the user."""


@dataclass(frozen=True)
class TextMatch:
    """One occurrence of a search term in a segment's effective text."""

    segment_id: str
    #: Position of the segment in the transcript when the search ran.
    segment_index: int
    start: int
    length: int

    @property
    def end(self) -> int:
        return self.start + self.length


class TranscriptStore:
    """Owns the transcript for one project and persists it to ``transcript.json``."""

    def __init__(self, path: Path, transcript: Transcript | None = None) -> None:
        self._path = path
        self._transcript = transcript if transcript is not None else Transcript()
        self._dirty = False
        self._starts: list[float] = []
        self._history = EditHistory()
        #: Bumped on every change, so a view can tell whether it is stale.
        self._revision = 0
        self._reindex()

    # -- construction -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> TranscriptStore:
        """Load from disk, or start empty when the file does not exist yet."""
        if not path.is_file():
            return cls(path)
        transcript = Transcript.from_dict(read_json(path))
        transcript.sort()
        return cls(path, transcript)

    # -- read access --------------------------------------------------------

    @property
    def path(self) -> Path:
        return self._path

    @property
    def transcript(self) -> Transcript:
        return self._transcript

    @property
    def segments(self) -> Sequence[TranscriptSegment]:
        """Read-only view. Mutations must go through this store's methods."""
        return tuple(self._transcript.segments)

    @property
    def language(self) -> str | None:
        return self._transcript.language

    @property
    def is_empty(self) -> bool:
        return self._transcript.is_empty

    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def revision(self) -> int:
        return self._revision

    def __len__(self) -> int:
        return len(self._transcript.segments)

    def __iter__(self) -> Iterator[TranscriptSegment]:
        return iter(self._transcript.segments)

    def get(self, segment_id: str) -> TranscriptSegment | None:
        for segment in self._transcript.segments:
            if segment.id == segment_id:
                return segment
        return None

    def index_of(self, segment_id: str) -> int | None:
        for index, segment in enumerate(self._transcript.segments):
            if segment.id == segment_id:
                return index
        return None

    # -- time lookup (video <-> transcript synchronisation, §12) ------------

    def index_at(self, seconds: float) -> int | None:
        """Index of the segment covering ``seconds``.

        A segment covers ``[start, end)``. When ``seconds`` falls in a gap
        between two segments, the preceding segment is returned so the
        highlight does not flicker off during a pause in speech. ``None`` only
        when ``seconds`` precedes the very first segment.
        """
        if not self._starts:
            return None
        # Rightmost segment whose start is <= seconds.
        index = bisect.bisect_right(self._starts, seconds) - 1
        if index < 0:
            return None
        return index

    def segment_at(self, seconds: float) -> TranscriptSegment | None:
        index = self.index_at(seconds)
        return self._transcript.segments[index] if index is not None else None

    # -- whole-transcript replacement ---------------------------------------

    def replace_all(
        self,
        segments: list[TranscriptSegment],
        *,
        language: str | None = None,
    ) -> None:
        """Install a fresh transcript, e.g. the result of a transcription run.

        This discards user edits and their undo history, so callers must
        confirm with the user first.
        """
        self._transcript.segments = list(segments)
        self._transcript.sort()
        if language is not None:
            self._transcript.language = language
        self._history.clear()
        self._changed()

    def set_slide_ids(self, slide_ids: Sequence[int | None]) -> int:
        """Record the alignment result, one slide id per segment in order.

        Only the link changes; no text is touched, and it is not an undoable
        edit - links are derived from the times. Returns how many segments
        changed, and marks the transcript dirty only when that is non-zero, so
        re-aligning an unchanged project never rewrites the file.
        """
        segments = self._transcript.segments
        if len(slide_ids) != len(segments):
            raise ValueError(
                f"Expected {len(segments)} slide ids, got {len(slide_ids)}."
            )
        changed = 0
        for segment, slide_id in zip(segments, slide_ids, strict=True):
            if segment.slide_id != slide_id:
                segment.slide_id = slide_id
                changed += 1
        if changed:
            self._dirty = True
        return changed

    @staticmethod
    def build_segments(
        raw: Sequence[tuple[float, float, str]],
    ) -> list[TranscriptSegment]:
        """Turn ``(start, end, text)`` triples into segments with stable ids."""
        return [
            TranscriptSegment(
                id=make_segment_id(index),
                start=start,
                end=end,
                original_text=text,
            )
            for index, (start, end, text) in enumerate(raw, start=1)
        ]

    # -- editing (§13) ------------------------------------------------------

    def edit_text(self, segment_id: str, text: str, *, coalesce: bool = False) -> bool:
        """Set what a segment says. Returns False when nothing changed.

        ``coalesce`` groups consecutive edits to the same segment into one undo
        step - pass it for live typing, and call :meth:`end_typing` when the
        editor closes.
        """
        segment = self._require(segment_id)
        if segment.effective_text == text:
            return False
        self._history.record(
            "Edit text", self._transcript.segments,
            coalesce_key=f"edit:{segment_id}" if coalesce else None,
        )
        _set_text(segment, text)
        self._changed()
        return True

    def end_typing(self) -> None:
        """Close a coalesced typing session; a session that changed nothing vanishes."""
        self._history.close_session(self._transcript.segments)

    def restore_original(self, segment_id: str) -> bool:
        """Discard the user's edit and show Whisper's text again."""
        segment = self._require(segment_id)
        if segment.edited_text is None:
            return False
        self._history.record("Restore original", self._transcript.segments)
        segment.edited_text = None
        self._changed()
        return True

    def delete_segment(self, segment_id: str) -> TranscriptSegment:
        index = self._require_index(segment_id)
        self._history.record("Delete segment", self._transcript.segments)
        removed = self._transcript.segments.pop(index)
        self._changed()
        return removed

    def insert_segment_after(
        self, segment_id: str | None, text: str = ""
    ) -> TranscriptSegment:
        """Add a segment for speech Whisper missed, after ``segment_id``.

        ``None`` inserts before the first segment. The new segment fills the
        gap to the next one when there is room, otherwise it takes a short
        default duration. It has no Whisper output, so its original is empty.
        """
        segments = self._transcript.segments
        if segment_id is None:
            start = 0.0
            following = segments[0] if segments else None
        else:
            index = self._require_index(segment_id)
            start = segments[index].end
            following = segments[index + 1] if index + 1 < len(segments) else None

        if following is not None and following.start - start >= MIN_GAP_SECONDS:
            end = following.start
        else:
            end = start + NEW_SEGMENT_SECONDS

        created = TranscriptSegment(
            id=self._next_id(), start=start, end=end, original_text=""
        )
        _set_text(created, text)

        self._history.record("Add segment", segments)
        segments.append(created)
        self._transcript.sort()
        self._changed()
        return created

    def merge_with_next(self, segment_id: str) -> TranscriptSegment:
        """Join a segment with the one after it into a single segment."""
        index = self._require_index(segment_id)
        segments = self._transcript.segments
        if index + 1 >= len(segments):
            raise TranscriptEditError("The last segment has nothing after it to merge with.")

        first, second = segments[index], segments[index + 1]
        edited = first.is_edited or second.is_edited
        merged = TranscriptSegment(
            id=first.id,
            start=min(first.start, second.start),
            end=max(first.end, second.end),
            original_text=_join(first.original_text, second.original_text),
            edited_text=(
                _join(first.effective_text, second.effective_text) if edited else None
            ),
            slide_id=first.slide_id,
        )
        if merged.edited_text == merged.original_text:
            merged.edited_text = None

        self._history.record("Merge segments", segments)
        segments[index : index + 2] = [merged]
        self._changed()
        return merged

    def split_segment(
        self, segment_id: str, offset: int
    ) -> tuple[TranscriptSegment, TranscriptSegment]:
        """Cut a segment in two at character ``offset`` of its displayed text.

        The time is divided in proportion to the text, which is as close as a
        segment-level transcript can get. The original is divided at the
        matching point too, snapped to a word boundary.
        """
        index = self._require_index(segment_id)
        segment = self._transcript.segments[index]
        text = segment.effective_text
        left_text, right_text = text[:offset].rstrip(), text[offset:].lstrip()
        if not 0 < offset < len(text) or not left_text or not right_text:
            raise TranscriptEditError(
                "Place the cursor between two words to split the segment there."
            )

        ratio = offset / len(text)
        cut = segment.start + segment.duration * ratio

        if segment.is_edited:
            original = segment.original_text
            at = _nearest_space(original, round(len(original) * ratio))
            left_original, right_original = original[:at].rstrip(), original[at:].lstrip()
        else:
            left_original, right_original = left_text, right_text

        left = TranscriptSegment(
            id=segment.id, start=segment.start, end=cut,
            original_text=left_original, slide_id=segment.slide_id,
        )
        right = TranscriptSegment(
            id=self._next_id(), start=cut, end=segment.end,
            original_text=right_original, slide_id=segment.slide_id,
        )
        _set_text(left, left_text)
        _set_text(right, right_text)

        self._history.record("Split segment", self._transcript.segments)
        self._transcript.segments[index : index + 1] = [left, right]
        self._changed()
        return left, right

    # -- reset to the original transcription -------------------------------

    def reset_to(self, segments: Sequence[TranscriptSegment]) -> bool:
        """Replace the transcript with ``segments`` as one undoable step.

        Used to go back to the transcription exactly as Whisper produced it,
        undoing merges, splits, additions and deletions as well as text edits.
        Unlike :meth:`replace_all` this keeps the history, so the reset itself
        can be undone. Returns False when the transcript already matches.
        """
        restored = copy_segments(list(segments))
        for segment in restored:
            segment.edited_text = None
        restored.sort(key=lambda s: (s.start, s.end))
        if _content(restored) == _content(self._transcript.segments):
            return False
        self._history.record("Reset to original", self._transcript.segments)
        self._transcript.segments = restored
        self._changed()
        return True

    def restore_all_originals(self) -> int:
        """Discard every text edit as one undoable step. Returns how many.

        The fallback when the original transcription was not kept: structure
        changes stay, but every line shows Whisper's text again.
        """
        edited = [s for s in self._transcript.segments if s.edited_text is not None]
        if not edited:
            return 0
        self._history.record("Reset to original", self._transcript.segments)
        for segment in edited:
            segment.edited_text = None
        self._changed()
        return len(edited)

    def matches(self, segments: Sequence[TranscriptSegment]) -> bool:
        """True when the transcript is exactly ``segments``, unedited."""
        return _content(self._transcript.segments) == _content(
            sorted(segments, key=lambda s: (s.start, s.end))
        ) and not any(s.is_edited for s in self._transcript.segments)

    # -- find & replace -----------------------------------------------------

    def find(self, query: str, *, case_sensitive: bool = False) -> list[TextMatch]:
        """Every occurrence of ``query`` in the displayed text, in order."""
        pattern = _pattern(query, case_sensitive)
        if pattern is None:
            return []
        return [
            TextMatch(segment.id, index, match.start(), match.end() - match.start())
            for index, segment in enumerate(self._transcript.segments)
            for match in pattern.finditer(segment.effective_text)
        ]

    def replace_match(
        self, match: TextMatch, query: str, replacement: str, *, case_sensitive: bool = False
    ) -> bool:
        """Replace one found occurrence, if it is still there."""
        segment = self.get(match.segment_id)
        pattern = _pattern(query, case_sensitive)
        if segment is None or pattern is None:
            return False
        text = segment.effective_text
        if not pattern.fullmatch(text, match.start, match.end):
            return False
        self._history.record("Replace", self._transcript.segments)
        _set_text(segment, text[: match.start] + replacement + text[match.end :])
        self._changed()
        return True

    def replace_everywhere(
        self, query: str, replacement: str, *, case_sensitive: bool = False
    ) -> int:
        """Replace every occurrence as one undoable step. Returns the count."""
        pattern = _pattern(query, case_sensitive)
        if pattern is None:
            return 0

        changes: list[tuple[TranscriptSegment, str]] = []
        total = 0
        for segment in self._transcript.segments:
            new_text, count = pattern.subn(lambda _m: replacement, segment.effective_text)
            if count:
                total += count
                if new_text != segment.effective_text:
                    changes.append((segment, new_text))
        if not changes:
            return 0

        self._history.record("Replace all", self._transcript.segments)
        for segment, new_text in changes:
            _set_text(segment, new_text)
        self._changed()
        return total

    # -- undo / redo ----------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return self._history.can_undo

    @property
    def can_redo(self) -> bool:
        return self._history.can_redo

    @property
    def undo_label(self) -> str | None:
        return self._history.undo_label

    @property
    def redo_label(self) -> str | None:
        return self._history.redo_label

    def undo(self) -> str | None:
        """Take back the last edit. Returns its label, or ``None`` if none."""
        entry = self._history.undo(self._transcript.segments)
        if entry is None:
            return None
        self._restore(entry.segments)
        return entry.label

    def redo(self) -> str | None:
        entry = self._history.redo(self._transcript.segments)
        if entry is None:
            return None
        self._restore(entry.segments)
        return entry.label

    # -- persistence --------------------------------------------------------

    def save(self, *, force: bool = False) -> bool:
        """Write ``transcript.json`` atomically. Returns True if it wrote."""
        if not self._dirty and not force:
            return False
        write_json_atomic(self._path, self._transcript.to_dict())
        self._dirty = False
        return True

    def mark_dirty(self) -> None:
        self._dirty = True

    # -- internals ----------------------------------------------------------

    def _require(self, segment_id: str) -> TranscriptSegment:
        return self._transcript.segments[self._require_index(segment_id)]

    def _require_index(self, segment_id: str) -> int:
        index = self.index_of(segment_id)
        if index is None:
            raise TranscriptEditError("That segment no longer exists.")
        return index

    def _next_id(self) -> str:
        highest = 0
        for segment in self._transcript.segments:
            match = _ID_NUMBER.match(segment.id)
            if match:
                highest = max(highest, int(match.group(1)))
        return make_segment_id(highest + 1)

    def _restore(self, segments: list[TranscriptSegment]) -> None:
        self._transcript.segments = copy_segments(segments)
        self._transcript.sort()
        self._changed()

    def _changed(self) -> None:
        self._reindex()
        self._dirty = True
        self._revision += 1

    def _reindex(self) -> None:
        self._starts = [s.start for s in self._transcript.segments]


def _content(
    segments: Sequence[TranscriptSegment],
) -> list[tuple[str, float, float, str, str | None]]:
    """What a segment list says and when - slide links deliberately excluded."""
    return [(s.id, s.start, s.end, s.original_text, s.edited_text) for s in segments]


def _set_text(segment: TranscriptSegment, text: str) -> None:
    """Store ``text`` as the edit, or clear the edit when it matches the original."""
    segment.edited_text = None if text == segment.original_text else text


def _join(first: str, second: str) -> str:
    return " ".join(part for part in (first.strip(), second.strip()) if part)


def _nearest_space(text: str, position: int) -> int:
    """The word boundary closest to ``position``, or ``position`` if there is none."""
    position = min(max(position, 0), len(text))
    spaces = [i for i, char in enumerate(text) if char.isspace()]
    if not spaces:
        return position
    return min(spaces, key=lambda i: abs(i - position))


def _pattern(query: str, case_sensitive: bool) -> re.Pattern[str] | None:
    if not query:
        return None
    return re.compile(re.escape(query), 0 if case_sensitive else re.IGNORECASE)
