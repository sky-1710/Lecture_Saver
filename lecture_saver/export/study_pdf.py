"""The study pack PDF: preview, lecture notes, quiz, then the answers.

One file, in the order a student uses it::

    Cover        what is in the pack, and that the AI parts need checking
    Part 1       예습 - summary, key points, terms, the lecture explained simply
    Part 2       강의 노트 - the slide + transcript notes, exactly as exported
    Part 3       Review quiz - 15 questions in English, no answers in sight
    (new page)   정답과 해설 - an answer grid, then each answer explained

The notes in Part 2 are not re-laid-out here: they come from
:func:`lecture_saver.export.pdf.notes_story`, so the study pack and the plain
notes PDF can never drift apart, and the edited transcript (§14) is what
appears in both. The answers start on a fresh page so a student working
through the quiz does not see them by accident.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.study import QuizQuestion, StudySet
from ..core.timecode import format_duration
from .document import LectureDocument
from .options import PdfOptions, safe_file_stem
from .pdf import FONT_BOLD, FONT_REGULAR, PdfExportError, build_pdf, notes_story, pdf_styles

__all__ = ["STUDY_PACK_SUFFIX", "study_pack_name", "write_study_pack"]

#: Added to the lecture's name: ``OS05_Scheduling2_study_pack.pdf``.
STUDY_PACK_SUFFIX = "study_pack"

_ACCENT = "#1D4ED8"
_ACCENT_SOFT = "#EFF6FF"
_INK = "#111827"
_BODY = "#1F2937"
_GREY = "#6B7280"
_RULE = "#D1D5DB"
_EXAMPLE_BG = "#F0FDF4"
_EXAMPLE_INK = "#166534"


def study_pack_name(title: str) -> str:
    return f"{safe_file_stem(title)}_{STUDY_PACK_SUFFIX}.pdf"


def write_study_pack(
    document: LectureDocument,
    study: StudySet,
    options: PdfOptions,
    path: Path,
) -> None:
    """Write the study pack to ``path``, atomically.

    ``options`` are the lecture's notes options (slide image, timestamps...);
    they shape Part 2 only. A part the study set does not have yet is left out
    rather than printed empty.
    """
    if study.is_empty:
        raise PdfExportError("There is no study material to put in the PDF yet.")
    notes_options = options if not options.is_empty else PdfOptions()

    def story(width: float, height: float) -> list[object]:
        from reportlab.platypus import PageBreak

        styles = _styles()
        flowables = _cover(document, study, styles)
        if study.guide is not None:
            flowables += [PageBreak(), *_preview(study, styles, width)]
        if document.segments or document.has_slides:
            flowables += [
                PageBreak(),
                *_part_banner("PART 2", "강의 노트", "슬라이드와 교수님 설명 (STT)", styles),
                *notes_story(document, notes_options, width, height, with_title=False),
            ]
        if study.quiz is not None:
            flowables += [PageBreak(), *_quiz(study, styles)]
            flowables += [PageBreak(), *_answers(study, styles, width)]
        return flowables

    build_pdf(path, f"{document.title} - Study Pack", story)


# -- styles ------------------------------------------------------------------------------


def _styles() -> dict[str, object]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    styles = dict(pdf_styles())
    c = colors.HexColor
    styles.update(
        {
            "cover_title": ParagraphStyle(
                "cover_title", fontName=FONT_BOLD, fontSize=26, leading=34,
                textColor=c(_INK), spaceAfter=6,
            ),
            "cover_sub": ParagraphStyle(
                "cover_sub", fontName=FONT_REGULAR, fontSize=13, leading=19,
                textColor=c(_ACCENT), spaceAfter=18,
            ),
            "part_tag": ParagraphStyle(
                "part_tag", fontName=FONT_BOLD, fontSize=9, leading=12,
                textColor=c(_ACCENT), spaceAfter=2,
            ),
            "part_title": ParagraphStyle(
                "part_title", fontName=FONT_BOLD, fontSize=22, leading=28,
                textColor=c(_INK), spaceAfter=2,
            ),
            "part_sub": ParagraphStyle(
                "part_sub", fontName=FONT_REGULAR, fontSize=10.5, leading=15,
                textColor=c(_GREY), spaceAfter=10,
            ),
            "h2": ParagraphStyle(
                "h2", fontName=FONT_BOLD, fontSize=14, leading=20,
                textColor=c(_INK), spaceBefore=14, spaceAfter=6,
            ),
            "h3": ParagraphStyle(
                "h3", fontName=FONT_BOLD, fontSize=12, leading=17,
                textColor=c(_INK), spaceBefore=12, spaceAfter=1,
            ),
            "slides_ref": ParagraphStyle(
                "slides_ref", fontName=FONT_REGULAR, fontSize=8.5, leading=12,
                textColor=c(_GREY), spaceAfter=5,
            ),
            "summary": ParagraphStyle(
                "summary", fontName=FONT_REGULAR, fontSize=11, leading=18,
                textColor=c(_BODY), backColor=c(_ACCENT_SOFT),
                borderPadding=(8, 10, 8, 10), leftIndent=10, rightIndent=10,
                spaceBefore=6, spaceAfter=14,
            ),
            "point": ParagraphStyle(
                "point", fontName=FONT_REGULAR, fontSize=10.5, leading=17,
                textColor=c(_BODY), leftIndent=20, firstLineIndent=-20, spaceAfter=5,
            ),
            "example": ParagraphStyle(
                "example", fontName=FONT_REGULAR, fontSize=10, leading=16,
                textColor=c(_EXAMPLE_INK), backColor=c(_EXAMPLE_BG),
                borderPadding=(6, 8, 6, 8), leftIndent=8, rightIndent=8,
                spaceBefore=4, spaceAfter=10,
            ),
            "cell": ParagraphStyle(
                "cell", fontName=FONT_REGULAR, fontSize=9.5, leading=14, textColor=c(_BODY),
            ),
            "cell_term": ParagraphStyle(
                "cell_term", fontName=FONT_BOLD, fontSize=9.5, leading=14, textColor=c(_INK),
            ),
            "question": ParagraphStyle(
                "question", fontName=FONT_BOLD, fontSize=10.5, leading=16,
                textColor=c(_INK), leftIndent=22, firstLineIndent=-22,
                spaceBefore=10, spaceAfter=4,
            ),
            "option": ParagraphStyle(
                "option", fontName=FONT_REGULAR, fontSize=10.5, leading=15,
                textColor=c(_BODY), leftIndent=40, firstLineIndent=-18, spaceAfter=2,
            ),
            "answer_head": ParagraphStyle(
                "answer_head", fontName=FONT_BOLD, fontSize=10.5, leading=16,
                textColor=c(_ACCENT), spaceBefore=10, spaceAfter=2,
            ),
            "answer_q": ParagraphStyle(
                "answer_q", fontName=FONT_REGULAR, fontSize=9, leading=13,
                textColor=c(_GREY), spaceAfter=3,
            ),
            "grid": ParagraphStyle(
                "grid", fontName=FONT_BOLD, fontSize=10, leading=13,
                textColor=c(_INK), alignment=1,
            ),
            "disclaimer": ParagraphStyle(
                "disclaimer", fontName=FONT_REGULAR, fontSize=9, leading=14,
                textColor=c(_GREY), spaceBefore=18,
            ),
        }
    )
    return styles


# -- building blocks --------------------------------------------------------------------


def _markup(text: str) -> str:
    """Escape for ReportLab, keeping single line breaks as line breaks."""
    return escape(text.strip()).replace("\n", "<br/>")


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def describe_slides(numbers: tuple[int, ...] | list[int]) -> str:
    """``(3, 4, 5, 8)`` -> ``Slides 3–5, 8``; ``(2,)`` -> ``Slide 2``."""
    values = sorted(set(numbers))
    if not values:
        return ""
    runs: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        runs.append(f"{start}–{previous}" if start != previous else str(start))
        start = previous = value
    runs.append(f"{start}–{previous}" if start != previous else str(start))
    label = "Slide" if len(values) == 1 else "Slides"
    return f"{label} {', '.join(runs)}"


def _rule() -> object:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable

    return HRFlowable(
        width="100%", thickness=0.6, color=colors.HexColor(_RULE), spaceBefore=4, spaceAfter=8
    )


def _part_banner(tag: str, title: str, subtitle: str, styles: dict[str, object]) -> list[object]:
    from reportlab.platypus import Paragraph

    return [
        Paragraph(escape(tag), styles["part_tag"]),
        Paragraph(escape(title), styles["part_title"]),
        Paragraph(escape(subtitle), styles["part_sub"]),
        _rule(),
    ]


# -- cover ---------------------------------------------------------------------------------


def _cover(document: LectureDocument, study: StudySet, styles: dict[str, object]) -> list[object]:
    from reportlab.platypus import Paragraph, Spacer

    meta = [document.video_name]
    if document.duration:
        meta.append(format_duration(document.duration))
    meta.append(f"{datetime.now():%Y-%m-%d}")

    contents = []
    if study.guide is not None:
        contents.append(("PART 1", "예습", "강의를 듣기 전에 — 핵심, 용어, 쉬운 설명"))
    contents.append(("PART 2", "강의 노트", "슬라이드와 교수님 설명 (STT)"))
    if study.quiz is not None:
        count = len(study.quiz.questions)
        contents.append(("PART 3", "복습 퀴즈", f"Review quiz — {count} questions (English)"))
        contents.append(("", "정답과 해설", "퀴즈 다음 페이지부터"))

    flowables: list[object] = [
        Spacer(1, 40),
        Paragraph(escape(document.title), styles["cover_title"]),
        Paragraph("Study Pack · 예습 · 강의 노트 · 복습", styles["cover_sub"]),
        Paragraph(escape("  ·  ".join(meta)), styles["meta"]),
        Spacer(1, 26),
        Paragraph("이 자료의 구성", styles["h2"]),
    ]
    for tag, title, note in contents:
        label = f'<font color="{_ACCENT}"><b>{escape(tag)}</b></font>&nbsp;&nbsp;' if tag else ""
        flowables.append(
            Paragraph(
                f'{label}<b>{escape(title)}</b>&nbsp;&nbsp;'
                f'<font color="{_GREY}">{escape(note)}</font>',
                styles["point"],
            )
        )
    flowables.append(
        Paragraph(
            "예습 자료와 퀴즈는 AI가 이 강의의 전사를 바탕으로 만들었습니다. "
            "틀린 내용이 있을 수 있으니, 헷갈리는 부분은 강의 노트(PART 2)에서 "
            "교수님 설명으로 확인하세요.",
            styles["disclaimer"],
        )
    )
    return flowables


# -- part 1: the preview ------------------------------------------------------------------


def _preview(study: StudySet, styles: dict[str, object], width: float) -> list[object]:
    from reportlab.lib import colors
    from reportlab.platypus import KeepTogether, Paragraph, Table, TableStyle

    guide = study.guide
    assert guide is not None
    flowables = _part_banner("PART 1", "예습", "강의를 듣기 전에 읽어 보세요", styles)

    if guide.summary:
        flowables += [
            Paragraph("한눈에 보기", styles["h2"]),
            Paragraph(_markup(guide.summary), styles["summary"]),
        ]

    flowables.append(Paragraph("먼저 알아야 할 핵심", styles["h2"]))
    for number, point in enumerate(guide.key_points, start=1):
        flowables.append(
            Paragraph(
                f'<font color="{_ACCENT}"><b>{number}.</b></font>&nbsp;&nbsp;{_markup(point)}',
                styles["point"],
            )
        )

    if guide.terms:
        flowables.append(Paragraph("꼭 알아야 할 용어", styles["h2"]))
        rows = [[Paragraph("용어", styles["cell_term"]), Paragraph("쉬운 뜻", styles["cell_term"])]]
        rows += [
            [
                Paragraph(_markup(t.term), styles["cell_term"]),
                Paragraph(_markup(t.meaning), styles["cell"]),
            ]
            for t in guide.terms
        ]
        table = Table(rows, colWidths=[width * 0.3, width * 0.7], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(_ACCENT_SOFT)),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor(_RULE)),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        flowables.append(table)

    flowables.append(Paragraph("강의 내용 쉽게 이해하기", styles["h2"]))
    for number, section in enumerate(guide.sections, start=1):
        head: list[object] = [Paragraph(f"{number}. {_markup(section.title)}", styles["h3"])]
        where = describe_slides(section.slides)
        head.append(Paragraph(escape(where) if where else "&nbsp;", styles["slides_ref"]))
        body = [Paragraph(_markup(p), styles["body"]) for p in section.paragraphs]
        # The heading stays with the first paragraph; the rest may flow on.
        flowables.append(KeepTogether(head + body[:1]))
        flowables += body[1:]
        if section.example:
            flowables.append(
                Paragraph(
                    f"<b>예를 들면</b>&nbsp;&nbsp;{_markup(section.example)}", styles["example"]
                )
            )
    return flowables


# -- part 3: the quiz, and the answers on the next page -----------------------------------


def _quiz(study: StudySet, styles: dict[str, object]) -> list[object]:
    from reportlab.platypus import KeepTogether

    quiz = study.quiz
    assert quiz is not None
    flowables = _part_banner(
        "PART 3", "복습 퀴즈",
        f"Review Quiz — Choose the best answer. ({len(quiz.questions)} questions)",
        styles,
    )
    for number, question in enumerate(quiz.questions, start=1):
        flowables.append(KeepTogether(_question_block(number, question, styles)))
    return flowables


def _question_block(number: int, question: QuizQuestion, styles: dict[str, object]) -> list[object]:
    from reportlab.platypus import Paragraph

    block: list[object] = [
        Paragraph(f"{number}.&nbsp;&nbsp;{_markup(question.question)}", styles["question"])
    ]
    for letter, option in zip("ABCD", question.options, strict=True):
        block.append(
            Paragraph(f"<b>{letter}.</b>&nbsp;&nbsp;{_markup(option)}", styles["option"])
        )
    return block


def _answers(study: StudySet, styles: dict[str, object], width: float) -> list[object]:
    from reportlab.lib import colors
    from reportlab.platypus import KeepTogether, Paragraph, Spacer, Table, TableStyle

    quiz = study.quiz
    assert quiz is not None
    flowables = _part_banner("ANSWERS", "정답과 해설", "Answers & Explanations", styles)

    # A grid first, for checking a whole attempt at a glance.
    columns = 5
    cells = [
        Paragraph(f'{n}&nbsp;&nbsp;<font color="{_ACCENT}">{q.answer}</font>', styles["grid"])
        for n, q in enumerate(quiz.questions, start=1)
    ]
    while len(cells) % columns:
        cells.append(Paragraph("", styles["grid"]))
    rows = [cells[i : i + columns] for i in range(0, len(cells), columns)]
    grid = Table(rows, colWidths=[width / columns] * columns)
    grid.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(_RULE)),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(_RULE)),
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(_ACCENT_SOFT)),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    flowables += [grid, Spacer(1, 10)]

    for number, question in enumerate(quiz.questions, start=1):
        block: list[object] = [
            Paragraph(
                f"{number}.&nbsp;&nbsp;정답 {question.answer}&nbsp;&nbsp;—&nbsp;&nbsp;"
                f"{_markup(question.answer_text)}",
                styles["answer_head"],
            ),
            Paragraph(_markup(question.question), styles["answer_q"]),
        ]
        block += [Paragraph(_markup(p), styles["body"]) for p in _paragraphs(question.explanation)]
        where = describe_slides(question.slides)
        if where:
            block.append(Paragraph(f"관련: {escape(where)}", styles["slides_ref"]))
        flowables.append(KeepTogether(block))
    return flowables
