"""The single authoritative transcript (§14).

Exactly one :class:`TranscriptStore` exists per open project. The slide view,
the full transcript view and every exporter read from this object - none of them
keeps its own copy - so they cannot drift apart.

It provides load, save, replace-after-transcription, lookup-by-time, and
recording the transcript-to-slide links computed by alignment.
The editing operations from §13 (edit / add / delete / merge / split / undo /
redo / find & replace / restore original) are Phase 5 and are deliberately not
implemented here yet.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterator, Sequence
from pathlib import Path

from .atomic_io import read_json, write_json_atomic
from .models import Transcript, TranscriptSegment, make_segment_id

__all__ = ["TranscriptStore"]


class TranscriptStore:
    """Owns the transcript for one project and persists it to ``transcript.json``."""

    def __init__(self, path: Path, transcript: Transcript | None = None) -> None:
        self._path = path
        self._transcript = transcript if transcript is not None else Transcript()
        self._dirty = False
        self._starts: list[float] = []
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

    def __len__(self) -> int:
        return len(self._transcript.segments)

    def __iter__(self) -> Iterator[TranscriptSegment]:
        return iter(self._transcript.segments)

    def get(self, segment_id: str) -> TranscriptSegment | None:
        for segment in self._transcript.segments:
            if segment.id == segment_id:
                return segment
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

    # -- mutation -----------------------------------------------------------

    def replace_all(
        self,
        segments: list[TranscriptSegment],
        *,
        language: str | None = None,
    ) -> None:
        """Install a fresh transcript, e.g. the result of a transcription run.

        This discards user edits, so callers must confirm with the user first.
        """
        self._transcript.segments = list(segments)
        self._transcript.sort()
        if language is not None:
            self._transcript.language = language
        self._reindex()
        self._dirty = True

    def set_slide_ids(self, slide_ids: Sequence[int | None]) -> int:
        """Record the alignment result, one slide id per segment in order.

        Only the link changes; no text is touched. Returns how many segments
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

    def _reindex(self) -> None:
        self._starts = [s.start for s in self._transcript.segments]
