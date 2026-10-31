"""The lecture's PDF, as what the typo check knows about the course.

Whether "process date" is a mishearing is hard to tell from the transcript
alone. A slide that says "process state" settles it. So the words of the PDF
the professor handed out can be kept with the lecture and shown to the
assistant as it checks the transcript. Only the text is kept: the PDF itself
stays where it is (§4), and text is all that a prompt can carry.

A deck is usually small enough to go along whole. A longer document would cost
more with every batch than the batch itself, so each batch gets the pages that
share the most words with its speech. Words on half the pages or more are the
deck's title and footer, which match everything and so say nothing.

Pure: pages and speech in, pages out.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .models import SCHEMA_VERSION

__all__ = ["DEFAULT_MAX_CHARS", "ReferenceText"]

#: How much of the PDF goes with one batch, in characters. About two thirds of
#: a batch's own transcript, and a whole deck of 25 pages.
DEFAULT_MAX_CHARS = 8000

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'-]{2,}|[가-힣]{2,}")


def _words(text: str) -> set[str]:
    return {word.lower() for word in _WORD.findall(text)}


@dataclass(frozen=True)
class ReferenceText:
    """Everything ``reference.json`` holds. Replaced whole, never changed."""

    #: The PDF's file name, to show. Not its path, which is nobody's business.
    source: str = ""
    #: The text of each page, "" for one that is only a picture.
    pages: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @property
    def is_empty(self) -> bool:
        return not any(self.pages)

    @property
    def characters(self) -> int:
        return sum(len(page) for page in self.pages)

    def pages_for(
        self, speech: str, *, max_chars: int = DEFAULT_MAX_CHARS
    ) -> list[tuple[int, str]]:
        """The pages to show with ``speech``: (page number, text), in page order."""
        pages = [(number, text) for number, text in enumerate(self.pages, 1) if text]
        if sum(len(text) for _, text in pages) <= max_chars:
            return pages

        said = _words(speech)
        words = [_words(text) for _, text in pages]
        shown = Counter(word for page in words for word in page)
        common = max(2, len(pages) // 2)
        scores = [
            sum(1 for word in page if word in said and shown[word] < common)
            for page in words
        ]

        chosen: list[tuple[int, str]] = []
        room = max_chars
        for position in sorted(range(len(pages)), key=lambda i: (-scores[i], i)):
            if not scores[position]:
                break
            if len(pages[position][1]) <= room:
                chosen.append(pages[position])
                room -= len(pages[position][1])
        return sorted(chosen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "pages": list(self.pages),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ReferenceText:
        if not data:
            return cls()
        return cls(
            source=str(data.get("source", "")),
            pages=tuple(str(page) for page in data.get("pages", [])),
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )
