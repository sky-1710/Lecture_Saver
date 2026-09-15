"""Turning Whisper's words into lines that end where sentences do (§6, §10).

Whisper hands back chunks of a few seconds that start and end wherever its
decoder happened to stop - routinely mid-sentence, with the full stop buried in
the middle of the next chunk. Those chunks are what the transcript is made of,
what the user edits, and what gets matched to slides, so their shape decides how
the lecture notes read: a slide change that lands inside a chunk leaves half a
thought on one slide and half on the next.

With word-level timestamps the line can be cut where the sentence actually ends,
to the word. This module does that cutting, as a stream: words arrive as each
chunk is decoded, finished sentences come out, and the tail waits for the rest
of itself. It is pure - no Whisper types, no I/O - so the rules can be tested
directly.

Rules, in order:

* A sentence ends at a word ending in ``.``/``?``/``!``/``…`` (or their
  full-width forms), unless the line so far is shorter than
  :data:`MIN_SECONDS` - "Yes." on its own is not a useful line, so it joins
  what follows.
* A silence of :data:`PAUSE` or more ends the line too. The lecturer stopped;
  wherever that is, it is a real break.
* Failing both, a line is closed at :data:`MAX_SECONDS` so speech that is never
  punctuated cannot become one enormous paragraph. The cut is taken at the last
  comma if there is one, else at the longest silence inside the line.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "MAX_SECONDS",
    "MIN_SECONDS",
    "PAUSE",
    "Line",
    "SentenceSegmenter",
    "Word",
]

#: A line is closed at the first sentence end after this many seconds.
MIN_SECONDS = 1.5

#: A line is never allowed to run longer than this. Measured on real lectures:
#: a genuine spoken sentence reaches about 20 s, so cutting below that chops
#: sentences the lecturer did finish, and far above it one unpunctuated run can
#: swallow a whole slide.
MAX_SECONDS = 22.0

#: A silence this long ends the line, punctuation or not. Below about a second
#: the lecturer is only drawing breath mid-sentence.
PAUSE = 1.2

_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？")
_CLAUSE_END = (",", ";", ":", "，", "、")
_TRAILING = "\"'”’)]}」』"


@dataclass(frozen=True)
class Word:
    """One word with the times Whisper gave it."""

    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Line:
    """A finished line of transcript."""

    start: float
    end: float
    text: str


def _strip(text: str) -> str:
    return text.strip().rstrip(_TRAILING)


def _ends_sentence(word: Word) -> bool:
    return _strip(word.text).endswith(_SENTENCE_END)


def _ends_clause(word: Word) -> bool:
    return _strip(word.text).endswith(_CLAUSE_END)


class SentenceSegmenter:
    """Collects words and hands back lines that end where sentences do.

    Feed it words in order; take the finished lines it returns. Whatever is
    still pending at the end of the lecture comes out of :meth:`flush`.
    """

    def __init__(
        self,
        *,
        min_seconds: float = MIN_SECONDS,
        max_seconds: float = MAX_SECONDS,
        pause: float = PAUSE,
    ) -> None:
        self._min = min_seconds
        self._max = max_seconds
        self._pause = pause
        self._pending: list[Word] = []

    @property
    def pending(self) -> bool:
        return bool(self._pending)

    def feed(self, words: Iterable[Word]) -> list[Line]:
        """Add ``words``; return every line they completed."""
        lines: list[Line] = []
        for word in words:
            if not word.text.strip():
                continue
            # A silence before this word closes what came before it.
            if self._pending and word.start - self._pending[-1].end >= self._pause:
                lines.append(self._take(len(self._pending)))

            self._pending.append(word)
            duration = self._pending[-1].end - self._pending[0].start

            if _ends_sentence(word) and duration >= self._min:
                lines.append(self._take(len(self._pending)))
            elif duration > self._max:
                lines.append(self._take(self._cut_point()))
        return [line for line in lines if line.text]

    def flush(self) -> list[Line]:
        """Whatever is left, as one line."""
        if not self._pending:
            return []
        line = self._take(len(self._pending))
        return [line] if line.text else []

    # -- internals ----------------------------------------------------------

    def _take(self, count: int) -> Line:
        """Remove the first ``count`` pending words and make a line of them."""
        taken, self._pending = self._pending[:count], self._pending[count:]
        return _line(taken)

    def _cut_point(self) -> int:
        """Where to cut an over-long line: a clause end, else the longest gap.

        Never the very last word - that would leave nothing pending and cut the
        following sentence off from its start.
        """
        words = self._pending
        for index in range(len(words) - 2, 0, -1):
            if _ends_clause(words[index]):
                return index + 1

        best_index, best_gap = len(words) - 1, -1.0
        for index in range(1, len(words)):
            gap = words[index].start - words[index - 1].end
            if gap > best_gap:
                best_index, best_gap = index, gap
        return best_index


def _line(words: Sequence[Word]) -> Line:
    if not words:
        return Line(0.0, 0.0, "")
    text = " ".join(word.text.strip() for word in words if word.text.strip())
    # Whisper's word tokens carry their own leading spaces; joining them can
    # leave doubles where a token was only punctuation.
    text = " ".join(text.split())
    return Line(start=words[0].start, end=max(word.end for word in words), text=text)
