"""The preview as a lecture: steps to go through, and words to read aloud.

The saved preview is already a teacher's lesson, so the lecture asks the
assistant for nothing more. This cuts its Markdown into steps at the big and
middle headings, works out which pages of the lecture's PDF each step is about
from the pages it cites, and turns what is written for the eye into what can be
said: a voice should not read out "별표 별표" or "피 점 삼". The lecture is in
Korean, so the English a term is glossed with stays on the screen and is not
read: the Korean voice makes a poor job of it, and says the term twice.

Pure: Markdown in, steps out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..export.markdown_pdf import (
    Block,
    Code,
    Heading,
    Item,
    Quote,
    Rule,
    TableBlock,
    Text,
    parse_blocks,
)

__all__ = ["Step", "cited_pages", "cited_times", "lesson_steps", "spoken"]

#: Headings at this level or above begin a step.
_STEP_LEVEL = 3
#: The most pages one citation is taken to mean: "p.1-200" is the whole deck.
_MAX_RANGE = 12

_PAGE = re.compile(r"\bp\.\s*(\d+)(?:\s*[-–~]\s*(?:p\.\s*)?(\d+))?", re.IGNORECASE)
_TIME = re.compile(r"\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")
_DECK_CITATION = re.compile(r"\[교안 p\.[^\]]*\]")
_SOURCE = re.compile(r"\[(교안 내용|교수님 설명|AI 보충 설명)\]")
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_MARKS = re.compile(r"\*\*|__|`")
#: ``*this*``, but not the star of a multiplication.
_EMPHASIS = re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])")
_SPACES = re.compile(r"[ \t]+")
#: "p.5를" was written for 오, and said as 5쪽 it needs the other particle.
_AFTER_PAGE = {"를": "을", "는": "은", "가": "이", "와": "과", "로": "으로"}
_PARTICLE = re.compile(r"(?<=\d쪽)([를는가와로])(?![가-힣])")
#: "비례 할당(Proportional Share)": English in brackets straight after Korean.
_ENGLISH_GLOSS = re.compile(r"(?<=[가-힣])\s*\([A-Za-z][A-Za-z0-9 ,.'/&-]*\)")

_SEE_THE_CHART = "구조도는 화면을 봐 주세요."
_SEE_THE_CODE = "코드는 화면을 봐 주세요."


@dataclass(frozen=True)
class Step:
    """One stop in the lecture."""

    #: The big heading it is under, "" before the first.
    part: str
    #: Its own heading, the same as ``part`` where a big heading has its own text.
    title: str
    blocks: tuple[Block, ...]
    #: Pages of the PDF to show: those it cites, else those of the step before.
    pages: tuple[int, ...]
    #: Times of the lecture it cites, in seconds, in the order they appear.
    times: tuple[float, ...]
    #: What is read aloud, a paragraph at a time.
    paragraphs: tuple[str, ...]
    #: The answers to the check questions, which nobody should be read by accident.
    is_answers: bool


def cited_pages(text: str) -> tuple[int, ...]:
    """The pages ``text`` cites, once each, in the order it cites them."""
    pages: list[int] = []
    for found in _PAGE.finditer(text):
        first = int(found.group(1))
        last = int(found.group(2) or first)
        if last < first or last - first >= _MAX_RANGE:
            last = first
        pages += [page for page in range(first, last + 1) if page not in pages]
    return tuple(pages)


def cited_times(text: str) -> tuple[float, ...]:
    """The times ``text`` cites, in seconds, once each."""
    times: list[float] = []
    for found in _TIME.finditer(text):
        first, second, third = found.groups()
        seconds = (
            int(first) * 3600 + int(second) * 60 + int(third) if third is not None
            else int(first) * 60 + int(second)
        )
        if seconds not in times:
            times.append(float(seconds))
    return tuple(times)


def spoken(text: str) -> str:
    """``text`` as a voice should say it."""
    said = _EMPHASIS.sub(r"\1", _MARKS.sub("", text))
    said = _DECK_CITATION.sub("", said)
    said = _ENGLISH_GLOSS.sub("", said)
    said = _TIME.sub("", said)
    said = _SOURCE.sub(lambda found: f"{found.group(1)}:", said)
    said = _PAGE.sub(_page_in_words, said)
    said = _PARTICLE.sub(lambda found: _AFTER_PAGE[found.group(1)], said)
    said = _BREAK.sub(" ", said)
    said = said.replace("→", ",").replace("_", " ").replace("\n", " ")
    return _SPACES.sub(" ", said).strip()


def _page_in_words(found: re.Match[str]) -> str:
    first, last = found.groups()
    return f"{first}쪽에서 {last}쪽" if last else f"{first}쪽"


def _block_words(block: Block) -> str:
    """Everything a block says, for finding what it cites."""
    if isinstance(block, (Heading, Text, Quote, Item)):
        return block.text
    if isinstance(block, TableBlock):
        return " ".join(cell for row in block.rows for cell in row)
    return ""


def _said(block: Block) -> list[str]:
    if isinstance(block, (Text, Quote, Item)):
        words = spoken(block.text)
        return [words] if words else []
    if isinstance(block, Heading):
        words = spoken(block.text)
        return [f"{words}."] if words else []
    if isinstance(block, TableBlock):
        return [words for row in block.rows if (words := spoken(", ".join(row)))]
    if isinstance(block, Code):
        return [_SEE_THE_CHART if block.language == "mermaid" else _SEE_THE_CODE]
    assert isinstance(block, Rule)
    return []


def _is_answers(heading: str) -> bool:
    return "정답" in heading


def lesson_steps(markdown: str) -> list[Step]:
    """The lesson's steps, in order. A heading with nothing under it joins the next."""
    groups: list[tuple[str, str, list[Block]]] = []
    part = title = ""
    held: list[str] = []  # headings waiting for something to be said under them
    current: list[Block] | None = None

    for block in parse_blocks(markdown):
        if isinstance(block, Rule):
            continue
        if isinstance(block, Heading) and block.level <= _STEP_LEVEL:
            if block.level <= 2:
                part = block.text
            title = block.text
            held.append(block.text)
            current = None
            continue
        if current is None:
            current = [Heading(_STEP_LEVEL, heading) for heading in held]
            held = []
            groups.append((part, title, current))
        current.append(block)

    steps: list[Step] = []
    pages: tuple[int, ...] = ()
    for part, title, blocks in groups:
        words = "\n".join(_block_words(block) for block in blocks)
        pages = cited_pages(words) or pages
        steps.append(
            Step(
                part=part,
                title=title,
                blocks=tuple(blocks),
                pages=pages,
                times=cited_times(words),
                paragraphs=tuple(line for block in blocks for line in _said(block)),
                is_answers=_is_answers(title),
            )
        )
    return steps
