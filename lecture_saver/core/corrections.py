"""Words the assistant thinks the transcription got wrong, for the user to judge.

Whisper mishears: "a learning program" for "a running program", "cloud time"
for "clock time". No spelling check catches that - every word is spelt right -
but something reading the lecture can, and a translation run is reading it
anyway. So the run can report what it doubts: one line under the Korean, in the
format :mod:`lecture_saver.export.ai_request` asks for and
:func:`parse_fix_line` reads.

A suggestion stays a suggestion. The assistant can be wrong, so nothing here
touches the transcript: a fix reaches it when the user accepts it, as an
ordinary undoable edit to ``edited_text`` (§13), and ``original_text`` stays as
Whisper wrote it.

Nothing records which fixes were made. Whether a suggestion is still open is
read off the text each time - its words are in its line or they are not - so
undoing a fix, restoring the original and correcting the word by hand all do
the right thing with no bookkeeping to fall out of step. Only "keep it as it
is" must be remembered, because that leaves the words where they were.

An answer is checked before it is believed: words that are not in the
transcript near the line they were reported under are dropped, so a suggestion
can never point at something the lecture does not say.

Nothing here talks to any service - it only reads text it is given.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .models import SCHEMA_VERSION, TranscriptSegment

log = logging.getLogger(__name__)

__all__ = [
    "Correction",
    "CorrectionSet",
    "ProposedFix",
    "corrected_texts",
    "is_fix_line",
    "match_corrections",
    "parse_fix_line",
]

#: ``!오탈자 | 틀린 말 | 고친 말 | 확실 | 근거``. The ``!`` and the bold an
#: assistant may add are optional: a line left unrecognised would be stored as
#: part of the translation.
_FIX_LINE = re.compile(
    r"^\s*(?:[-*>]\s+)?(?:\*\*)?!?\s*오탈자(?:\*\*)?\s*[|:｜]\s*(?P<fields>.*)$"
)
#: The word in the third field that means the assistant has no doubt.
SURE_WORD = "확실"
#: Quoting an assistant may put around the words.
_QUOTES = "`\"'“”‘’ "

#: How far from the line it was reported under a fix's words may be. An answer
#: that lists its fixes at the end of a slide stamps them late, so the nearest
#: line holding the words is taken rather than only the stamped one.
REACH_SECONDS = 180


@dataclass(frozen=True)
class ProposedFix:
    """One fix as an answer gave it, before it is found in the transcript."""

    #: The timestamp of the line it was written under, in whole seconds.
    stamp: int
    wrong: str
    right: str
    sure: bool = False
    reason: str = ""


def is_fix_line(line: str) -> bool:
    return _FIX_LINE.match(line) is not None


def parse_fix_line(line: str, stamp: int | None) -> ProposedFix | None:
    """Read one fix line, or ``None`` if it says nothing usable.

    "!오탈자 | 없음" and a fix under no timestamp both end up here: there is
    nothing to place, though the line must still not reach the translation.
    """
    found = _FIX_LINE.match(line)
    if found is None or stamp is None:
        return None
    fields = [part.strip() for part in found.group("fields").split("|", 3)]
    if len(fields) < 2:
        return None
    wrong, right = fields[0].strip(_QUOTES), fields[1].strip(_QUOTES)
    if not wrong or not right:
        return None
    return ProposedFix(
        stamp=stamp,
        wrong=wrong,
        right=right,
        sure=len(fields) > 2 and SURE_WORD in fields[2],
        reason=fields[3] if len(fields) > 3 else "",
    )


def _words(text: str) -> str:
    """A pattern for ``text``, in a line of the transcript.

    Latin words match whole, so "rate" is not found in "generate". Hangul does
    not: particles join the word they follow ("러닝레이트를").
    """
    body = r"\s+".join(re.escape(word) for word in text.split())
    head = r"(?<![A-Za-z0-9])" if text[0].isascii() and text[0].isalnum() else ""
    tail = r"(?![A-Za-z0-9])" if text[-1].isascii() and text[-1].isalnum() else ""
    return head + body + tail


@dataclass(frozen=True)
class Correction:
    """The assistant thinks ``wrong`` in this line should say ``right``."""

    segment_id: str
    #: When the line starts, in seconds.
    start: float
    wrong: str
    right: str
    #: False when the assistant says only hearing the lecture can settle it.
    sure: bool = False
    #: Why, in the assistant's words - a page of the PDF, say.
    reason: str = ""
    #: The user chose to leave the words as they are.
    kept: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.segment_id, self.wrong.lower()

    def _finder(self) -> re.Pattern[str]:
        """Finds the words, and the right words so those are stepped over:
        "rate" is nothing to fix inside "learning rate" once a line says so."""
        options = [f"(?P<right>{_words(self.right)})", f"(?P<wrong>{_words(self.wrong)})"]
        if len(self.wrong) > len(self.right):
            options.reverse()
        return re.compile("|".join(options), re.IGNORECASE)

    def spans(self, text: str) -> list[tuple[int, int]]:
        """Where its words are in ``text``. Empty once there is nothing to fix."""
        return [
            found.span() for found in self._finder().finditer(text)
            if found.lastgroup == "wrong"
        ]

    def applied_to(self, text: str) -> str:
        """``text`` with its words replaced, everywhere in the line."""
        return self._finder().sub(
            lambda found: self.right if found.lastgroup == "wrong" else found.group(), text
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "start": round(self.start, 3),
            "wrong": self.wrong,
            "right": self.right,
            "sure": self.sure,
            "reason": self.reason,
            "kept": self.kept,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Correction:
        return cls(
            segment_id=str(data["segment_id"]),
            start=float(data["start"]),
            wrong=str(data["wrong"]),
            right=str(data["right"]),
            sure=bool(data.get("sure", False)),
            reason=str(data.get("reason", "")),
            kept=bool(data.get("kept", False)),
        )


def corrected_texts(
    corrections: Sequence[Correction], segments: Sequence[TranscriptSegment]
) -> dict[str, str]:
    """Segment id -> what the line says with ``corrections`` made.

    Two fixes to one line both land, each applied to the other's result.
    """
    current = {segment.id: segment.effective_text for segment in segments}
    texts: dict[str, str] = {}
    for correction in corrections:
        text = texts.get(correction.segment_id, current.get(correction.segment_id))
        if text is not None:
            texts[correction.segment_id] = correction.applied_to(text)
    return texts


@dataclass
class CorrectionSet:
    """Everything ``corrections.json`` holds."""

    entries: list[Correction] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def merge(self, new: Sequence[Correction]) -> None:
        """Add suggestions. Asked again, a word the user kept stays kept."""
        known = {entry.key: entry for entry in self.entries}
        for correction in new:
            earlier = known.get(correction.key)
            known[correction.key] = (
                replace(correction, kept=True) if earlier and earlier.kept else correction
            )
        self.entries = sorted(known.values(), key=lambda entry: entry.start)

    def keep(self, correction: Correction) -> None:
        """Leave these words as they are, and stop suggesting otherwise."""
        self.entries = [
            replace(entry, kept=True) if entry.key == correction.key else entry
            for entry in self.entries
        ]

    def open_for(self, segments: Sequence[TranscriptSegment]) -> list[Correction]:
        """The suggestions still to be judged, in lecture order."""
        lines = {segment.id: segment for segment in segments}
        return [
            entry for entry in self.entries
            if not entry.kept
            and entry.segment_id in lines
            and entry.spans(lines[entry.segment_id].effective_text)
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "corrections": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CorrectionSet:
        if not data:
            return cls()
        return cls(
            entries=[Correction.from_dict(d) for d in data.get("corrections", [])],
            schema_version=int(data.get("schema_version", SCHEMA_VERSION)),
        )


def match_corrections(
    proposed: Sequence[ProposedFix], segments: Sequence[TranscriptSegment]
) -> tuple[Correction, ...]:
    """Find each proposed fix in the transcript; drop those that are not there.

    Each goes to the nearest line that really says its words. A fix that only
    changes capitals is dropped too: a line would have to be read to tell
    whether it was right, and it is not what this is for.
    """
    found: list[Correction] = []
    for fix in proposed:
        if fix.wrong.casefold() == fix.right.casefold():
            continue
        correction = Correction(
            segment_id="", start=0.0, wrong=fix.wrong, right=fix.right,
            sure=fix.sure, reason=fix.reason,
        )
        holding = [
            segment for segment in segments
            if abs(segment.start - fix.stamp) <= REACH_SECONDS
            and correction.spans(segment.effective_text)
        ]
        if not holding:
            log.info("A suggested fix was not found near its line, and was dropped")
            continue
        line = min(holding, key=lambda segment: abs(segment.start - fix.stamp))
        found.append(replace(correction, segment_id=line.id, start=line.start))
    return tuple(found)
