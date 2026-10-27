"""Reading what the assistant answered, or why it did not.

An answer is Markdown. Before it is shown, the places it points into the
lecture become links the panel follows:

* a time such as ``[00:12:34]`` -> ``seek:754``, the video jumps there;
* ``Slide 8`` (or ``슬라이드 8``) -> ``slide:8``, the slide is shown.

A time past the end of the lecture, or a slide the lecture does not have, is
left as plain text: a link to the wrong place is worse than none.

Bold next to Korean is also rewritten. CommonMark only closes ``**`` that is
"right-flanking", so ``**학습률(learning rate)**은`` - a closing ``**``
between punctuation and a Hangul letter - would stay literal asterisks.
Korean attaches particles straight to the word, so this happens constantly;
pairs are turned into ``<b>`` tags, which Qt's Markdown reader accepts.

Code spans and fenced blocks are left exactly as written.
"""

from __future__ import annotations

import re
from collections.abc import Collection

from ..core.timecode import format_hms
from ..translate.engine import (
    EngineAuthError,
    EngineNotFoundError,
    EngineRateLimited,
    TranslationError,
)

__all__ = ["SEEK_SCHEME", "SLIDE_SCHEME", "failure_message", "prepare_answer"]

SEEK_SCHEME = "seek"
SLIDE_SCHEME = "slide"

#: A time beyond the end of the lecture by more than this is not a citation.
_END_SLACK_SECONDS = 5.0

_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"(`+)[^`]*?\1")
_BOLD_PAIR = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
#: ``00:12:34`` with or without brackets, and ``Slide 8`` / ``슬라이드 8``.
_CITATION = re.compile(
    r"\[?(?<![\d:])(?P<h>\d{1,2}):(?P<m>[0-5]\d):(?P<s>[0-5]\d)(?![\d:])\]?"
    r"|\[?(?<![\w\[])(?P<word>Slide|slide|슬라이드)\s?(?P<n>\d{1,4})(?!\d)\]?"
)


def _linked(text: str, duration: float | None, slides: Collection[int]) -> str:
    def link(match: re.Match[str]) -> str:
        if match.group("word"):
            number = int(match.group("n"))
            if number not in slides:
                return match.group(0)
            return f"[{match.group('word')} {number}]({SLIDE_SCHEME}:{number})"
        seconds = int(match.group("h")) * 3600 + int(match.group("m")) * 60 + int(match.group("s"))
        if duration is not None and duration > 0 and seconds > duration + _END_SLACK_SECONDS:
            return match.group(0)
        return f"[{format_hms(seconds)}]({SEEK_SCHEME}:{seconds})"

    return _CITATION.sub(link, text)


def _prose(chunk: str, duration: float | None, slides: Collection[int]) -> str:
    out: list[str] = []
    position = 0
    for match in _CODE_SPAN.finditer(chunk):
        out.append(_inline(chunk[position : match.start()], duration, slides))
        out.append(match.group(0))
        position = match.end()
    out.append(_inline(chunk[position:], duration, slides))
    return "".join(out)


def _inline(text: str, duration: float | None, slides: Collection[int]) -> str:
    return _BOLD_PAIR.sub(r"<b>\1</b>", _linked(text, duration, slides))


def prepare_answer(
    text: str, *, duration: float | None = None, slides: Collection[int] = ()
) -> str:
    """The answer's Markdown with its citations linked, ready for Qt to show.

    ``duration`` is the lecture's length in seconds and ``slides`` the slide
    numbers it has; citations outside them stay plain text.
    """
    out: list[str] = []
    prose: list[str] = []
    fence: str | None = None
    for line in text.replace("\r\n", "\n").split("\n"):
        marker = _FENCE.match(line)
        if fence is None and marker:
            if prose:
                out.append(_prose("\n".join(prose), duration, slides))
                prose = []
            fence = marker.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            if line.strip().startswith(fence):
                fence = None
        else:
            prose.append(line)
    if prose:
        out.append(_prose("\n".join(prose), duration, slides))
    return "\n".join(out)


def failure_message(error: BaseException) -> str:
    """What to tell the user when a question got no answer (§28).

    The engine's own messages talk about translating; the ones that would
    mislead here ("slides translated so far have been saved") are replaced.
    """
    if isinstance(error, EngineRateLimited):
        return (
            "Your ChatGPT usage limit has been reached, so the question could not be "
            "answered. Try again later - the question is still in the box."
        )
    if isinstance(error, EngineAuthError):
        return (
            "Codex is installed but not signed in.\n\n"
            "Open a terminal and run:\n    codex login\n\nThen ask again."
        )
    if isinstance(error, (EngineNotFoundError, TranslationError)):
        return str(error)
    return f"The question could not be asked:\n{error}"
