"""Markdown, as an assistant writes it, laid out for the PDF.

The preview is a long lesson in Markdown: headings, lists, tables, quotes, code
and Mermaid flowcharts. This reads that much of the language, which no library
in the application already does, and turns it into flowables.

Nothing the assistant wrote is dropped. A flowchart that cannot be drawn is
printed as its source, a table that does not line up is padded, and text this
does not recognise is simply a paragraph.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .flowchart import FlowchartImage, parse_flowchart
from .pdf import FONT_BOLD, FONT_REGULAR, printable

__all__ = ["headings", "inline", "markdown_story", "parse_blocks"]

_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([\w-]*)")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")

#: ``**bold**``, ``__bold__``, `` `code` ``, and ``*this*`` when it is not a
#: multiplication. Malgun Gothic has no italic, so it is printed plainly.
_INLINE = re.compile(
    r"\*\*(?P<bold>.+?)\*\*|__(?P<under>.+?)__|`(?P<code>[^`]+)`"
    r"|(?<![\w*])\*(?P<plain>[^\s*](?:[^*]*[^\s*])?)\*(?![\w*])"
)
_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
#: Where a line of the lesson says it came from: which source, the slide's page,
#: the time it was said.
_MARKER = re.compile(
    r"\[(?:교안 내용|교수님 설명|AI 보충 설명|교안 p\.[^\]]*|\d{1,2}:\d{2}(?::\d{2})?)\]"
)

_INK = "#111827"
_BODY = "#1F2937"
_GREY = "#6B7280"
_ACCENT = "#1D4ED8"
_ACCENT_SOFT = "#EFF6FF"
_RULE_COLOUR = "#D1D5DB"
_CODE_BACK = "#F3F4F6"
_DECK = "#B45309"
_MARKER_COLOURS = {"교안 내용": _DECK, "교수님 설명": "#047857", "AI 보충 설명": "#7C3AED"}

#: Answers to the check questions start a page. An assistant told to write this
#: heading at one level has been seen writing it a level down.
_ANSWERS_WORD = "정답"
_ANSWERS_DEEPEST = 3
#: How much of a page a flowchart may take.
_MAX_CHART_HEIGHT = 0.7
#: Only as many levels of list as a page can show.
_MAX_LIST_DEPTH = 3
_LIST_INDENT = 16


@dataclass(frozen=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Quote:
    text: str


@dataclass(frozen=True)
class Item:
    depth: int
    marker: str
    text: str


@dataclass(frozen=True)
class TableBlock:
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Code:
    language: str
    text: str


@dataclass(frozen=True)
class Rule:
    pass


Block = Heading | Text | Quote | Item | TableBlock | Code | Rule


def _starts_block(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(
        _HEADING.match(line) or _RULE.match(line) or _ITEM.match(line)
        or _FENCE.match(line) or _QUOTE.match(line) or _is_table(lines, index)
    )


def _is_table(lines: list[str], index: int) -> bool:
    return (
        "|" in lines[index]
        and index + 1 < len(lines)
        and "-" in lines[index + 1]
        and "|" in lines[index + 1]
        and bool(_TABLE_RULE.match(lines[index + 1]))
    )


def _cells(line: str) -> tuple[str, ...]:
    text = line.strip().removeprefix("|").removesuffix("|")
    # A pipe written \| belongs to the cell.
    return tuple(c.strip().replace("\0", "|") for c in text.replace(r"\|", "\0").split("|"))


def parse_blocks(markdown: str) -> list[Block]:
    """Split the text into blocks. Anything else is a paragraph."""
    lines = markdown.splitlines()
    blocks: list[Block] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
        elif fence := _FENCE.match(line):
            closing = fence.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(closing):
                body.append(lines[index])
                index += 1
            index += 1
            blocks.append(Code(fence.group(2).lower(), "\n".join(body)))
        elif heading := _HEADING.match(line):
            blocks.append(Heading(len(heading.group(1)), heading.group(2)))
            index += 1
        elif _RULE.match(line):
            blocks.append(Rule())
            index += 1
        elif _is_table(lines, index):
            rows = [_cells(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_cells(lines[index]))
                index += 1
            columns = max(len(row) for row in rows)
            blocks.append(
                TableBlock(tuple(row + ("",) * (columns - len(row)) for row in rows))
            )
        elif _QUOTE.match(line):
            quoted: list[str] = []
            while index < len(lines) and (found := _QUOTE.match(lines[index])):
                quoted.append(found.group(1))
                index += 1
            blocks.append(Quote("\n".join(quoted).strip()))
        elif item := _ITEM.match(line):
            words = [item.group(3)]
            index += 1
            # A line indented under an item goes on with it.
            while (
                index < len(lines)
                and lines[index].startswith(("  ", "\t"))
                and lines[index].strip()
                and not _starts_block(lines, index)
            ):
                words.append(lines[index].strip())
                index += 1
            depth = min(len(item.group(1).expandtabs(4)) // 2, _MAX_LIST_DEPTH)
            blocks.append(Item(depth, item.group(2), "\n".join(words)))
        else:
            words = [line.strip()]
            index += 1
            while (
                index < len(lines) and lines[index].strip() and not _starts_block(lines, index)
            ):
                words.append(lines[index].strip())
                index += 1
            blocks.append(Text("\n".join(words)))
    return blocks


def headings(markdown: str, level: int = 2) -> list[str]:
    """The headings of one level, as plain text, for a table of contents."""
    return [
        _INLINE.sub(lambda m: next(g for g in m.groups() if g is not None), block.text)
        for block in parse_blocks(markdown)
        if isinstance(block, Heading) and block.level == level
    ]


# -- words -------------------------------------------------------------------------------


def _coloured_marker(found: str) -> str:
    source = found.strip("[]")
    colour = _MARKER_COLOURS.get(source, _DECK if source.startswith("교안") else _GREY)
    return f'<font color="{colour}"><b>{printable(found)}</b></font>'


def _words(text: str) -> str:
    """Plain words: escaped, with the source markers picked out."""
    parts: list[str] = []
    position = 0
    for marker in _MARKER.finditer(text):
        parts.append(printable(text[position : marker.start()]))
        parts.append(_coloured_marker(marker.group()))
        position = marker.end()
    parts.append(printable(text[position:]))
    return "".join(parts)


def _code_font(text: str) -> str:
    """Courier has no Korean, so code with Korean in it stays in the body font."""
    return "Courier" if text.isascii() else FONT_REGULAR


def inline(text: str) -> str:
    """Bold, code and line breaks as ReportLab's markup; the rest escaped."""
    text = _BREAK.sub("\n", text)
    parts: list[str] = []
    position = 0
    for found in _INLINE.finditer(text):
        parts.append(_words(text[position : found.start()]))
        if (bold := found["bold"] or found["under"]) is not None:
            parts.append(f"<b>{inline(bold)}</b>")
        elif (code := found["code"]) is not None:
            parts.append(f'<font name="{_code_font(code)}">{printable(code)}</font>')
        else:
            parts.append(_words(found["plain"]))
        position = found.end()
    parts.append(_words(text[position:]))
    return "".join(parts).replace("\n", "<br/>")


# -- flowables ---------------------------------------------------------------------------


def _styles() -> dict[str, object]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    c = colors.HexColor
    style = ParagraphStyle
    return {
        "h1": style("md_h1", fontName=FONT_BOLD, fontSize=19, leading=26, textColor=c(_INK),
                    spaceBefore=6, spaceAfter=8, keepWithNext=1),
        "h2": style("md_h2", fontName=FONT_BOLD, fontSize=16, leading=23, textColor=c(_ACCENT),
                    spaceBefore=20, spaceAfter=3, keepWithNext=1),
        "h3": style("md_h3", fontName=FONT_BOLD, fontSize=12.5, leading=18, textColor=c(_INK),
                    spaceBefore=14, spaceAfter=5, keepWithNext=1),
        "h4": style("md_h4", fontName=FONT_BOLD, fontSize=11, leading=16, textColor=c(_INK),
                    spaceBefore=10, spaceAfter=3, keepWithNext=1),
        "body": style("md_body", fontName=FONT_REGULAR, fontSize=10.5, leading=17.5,
                      textColor=c(_BODY), spaceAfter=7),
        "quote": style("md_quote", fontName=FONT_REGULAR, fontSize=10.5, leading=17,
                       textColor=c(_BODY), backColor=c(_ACCENT_SOFT), leftIndent=10,
                       rightIndent=10, borderPadding=(6, 8, 6, 8), spaceBefore=6, spaceAfter=12),
        "code": style("md_code", fontName=FONT_REGULAR, fontSize=9, leading=13,
                      textColor=c(_INK), backColor=c(_CODE_BACK), leftIndent=8, rightIndent=8,
                      borderPadding=(5, 6, 5, 6), spaceBefore=6, spaceAfter=12),
        "cell": style("md_cell", fontName=FONT_REGULAR, fontSize=9.5, leading=14,
                      textColor=c(_BODY)),
        "head": style("md_head", fontName=FONT_BOLD, fontSize=9.5, leading=14,
                      textColor=c(_INK)),
    }


def _item_style(base: object, depth: int) -> object:
    from reportlab.lib.styles import ParagraphStyle

    indent = _LIST_INDENT * depth
    return ParagraphStyle(
        f"md_item_{depth}", parent=base, spaceAfter=4, bulletFontName=FONT_REGULAR,
        bulletFontSize=10.5, bulletIndent=indent + 2, leftIndent=indent + _LIST_INDENT + 2,
    )


def _code_block(text: str, styles: dict[str, object]) -> object:
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph

    lines = [printable(line).replace(" ", "&nbsp;") for line in text.splitlines()]
    style = ParagraphStyle("md_code_block", parent=styles["code"], fontName=_code_font(text))
    return Paragraph("<br/>".join(lines) or "&nbsp;", style)


def _table(block: TableBlock, width: float, styles: dict[str, object]) -> object:
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle

    columns = len(block.rows[0])
    # As wide as a column's longest cell, within reason: a column of a few
    # words must not be squeezed by a column of sentences, or the reverse.
    weights = [
        min(50, max(8, max(len(row[column]) for row in block.rows)))
        for column in range(columns)
    ]
    total = sum(weights)
    widths = [width * weight / total for weight in weights]
    rows = [
        [
            Paragraph(inline(cell) or "&nbsp;", styles["head" if number == 0 else "cell"])
            for cell in row
        ]
        for number, row in enumerate(block.rows)
    ]
    table = Table(
        rows, colWidths=widths, repeatRows=1, splitInRow=1, spaceBefore=4, spaceAfter=10
    )
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_ACCENT_SOFT)),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(_RULE_COLOUR)),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return table


def _flowchart(block: Code, width: float, height: float, styles: dict[str, object]) -> object:
    """The picture, or the source when it cannot be drawn so that it can be read."""
    chart = parse_flowchart(block.text)
    if chart is not None:
        image = FlowchartImage(chart, width, height * _MAX_CHART_HEIGHT)
        if image.readable:  # type: ignore[attr-defined]
            # On the flowable, not a Spacer: a heading stays with what is next.
            image.spaceBefore, image.spaceAfter = 6, 12  # type: ignore[attr-defined]
            return image
    return _code_block(block.text, styles)


def _rule(*, keep_with_next: bool = False) -> object:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable

    rule = HRFlowable(
        width="100%", thickness=0.6, color=colors.HexColor(_RULE_COLOUR),
        spaceBefore=6, spaceAfter=10,
    )
    rule.keepWithNext = keep_with_next
    return rule


def markdown_story(markdown: str, width: float, height: float) -> list[object]:
    """The lesson as flowables, in a frame ``width`` by ``height``.

    The answers to the check questions start on a page of their own, as the
    quiz's do, so a student answering does not see them by accident.
    """
    from reportlab.platypus import PageBreak, Paragraph

    styles = _styles()
    story: list[object] = []
    for block in parse_blocks(markdown):
        if isinstance(block, Heading):
            if block.level <= _ANSWERS_DEEPEST and _ANSWERS_WORD in block.text:
                story.append(PageBreak())
            style = styles[f"h{min(block.level, 4)}"]
            story.append(Paragraph(inline(block.text), style))
            if block.level == 2:
                story.append(_rule(keep_with_next=True))
        elif isinstance(block, Text):
            story.append(Paragraph(inline(block.text), styles["body"]))
        elif isinstance(block, Quote):
            story.append(Paragraph(inline(block.text), styles["quote"]))
        elif isinstance(block, Item):
            bullet = block.marker if block.marker[0].isdigit() else "•"
            style = _item_style(styles["body"], block.depth)
            story.append(Paragraph(inline(block.text), style, bulletText=bullet))
        elif isinstance(block, TableBlock):
            story.append(_table(block, width, styles))
        elif isinstance(block, Code):
            if block.language == "mermaid":
                story.append(_flowchart(block, width, height, styles))
            else:
                story.append(_code_block(block.text, styles))
        else:
            story.append(_rule())
    return story
