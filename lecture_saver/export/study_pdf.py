"""The study PDFs: the preview before the lecture, and the review after it.

Two files, because they are read days apart::

    OS05_preview.pdf
        Cover        what is in it, and that it needs checking against the lecture
        The lesson   the assistant's Markdown, with its concept maps drawn
        (new page)   the answers to its check questions

    OS05_review.pdf
        Cover        what is in it, and that the AI part needs checking
        Part 1       강의 노트 - the slide + transcript notes, exactly as exported
        Part 2       Review quiz - 15 questions in English, no answers in sight
        (new page)   정답과 해설 - an answer grid, then each answer explained

The notes in the review are not re-laid-out here: they come from
:func:`lecture_saver.export.pdf.notes_story`, so the review and the plain notes
PDF can never drift apart, and the edited transcript (§14) is what appears in
both. The answers start on a fresh page so a student working through the quiz
does not see them by accident.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.study import QuizQuestion, StudySet
from ..core.timecode import format_duration
from .document import LectureDocument
from .markdown_pdf import headings, markdown_story
from .options import PdfOptions, safe_file_stem
from .pdf import FONT_BOLD, FONT_REGULAR, PdfExportError, build_pdf, notes_story, pdf_styles

__all__ = [
    "PREVIEW_SUFFIX",
    "REVIEW_SUFFIX",
    "preview_pdf_name",
    "review_pdf_name",
    "write_preview_pdf",
    "write_review_pdf",
]

#: Added to the lecture's name: ``OS05_Scheduling2_preview.pdf``.
PREVIEW_SUFFIX = "preview"
REVIEW_SUFFIX = "review"

_ACCENT = "#1D4ED8"
_INK = "#111827"
_BODY = "#1F2937"
_GREY = "#6B7280"
_RULE = "#D1D5DB"
_ACCENT_SOFT = "#EFF6FF"


def preview_pdf_name(title: str) -> str:
    return f"{safe_file_stem(title)}_{PREVIEW_SUFFIX}.pdf"


def review_pdf_name(title: str) -> str:
    return f"{safe_file_stem(title)}_{REVIEW_SUFFIX}.pdf"


def write_preview_pdf(document: LectureDocument, study: StudySet, path: Path) -> None:
    """Write the preview to ``path``, atomically."""
    guide = study.guide
    if guide is None:
        raise PdfExportError("There is no preview to put in the PDF yet.")

    def story(width: float, height: float) -> list[object]:
        from reportlab.platypus import PageBreak

        contents = [("", part, "") for part in headings(guide.markdown)]
        cover = _cover(
            document, "예습 · 강의를 듣기 전에", contents,
            "이 자료는 AI가 교안과 강의 전사를 바탕으로 만들었습니다. 틀린 내용이 있을 수 "
            "있으니, 헷갈리는 부분은 교안과 교수님 설명으로 확인하세요.",
            _styles(),
        )
        return [*cover, PageBreak(), *markdown_story(guide.markdown, width, height)]

    build_pdf(path, f"{document.title} - 예습", story)


def write_review_pdf(
    document: LectureDocument,
    study: StudySet,
    options: PdfOptions,
    path: Path,
) -> None:
    """Write the review to ``path``, atomically.

    ``options`` are the lecture's notes options (slide image, timestamps...);
    they shape the notes only.
    """
    if study.quiz is None:
        raise PdfExportError("There is no quiz to put in the PDF yet.")
    notes_options = options if not options.is_empty else PdfOptions()
    count = len(study.quiz.questions)

    def story(width: float, height: float) -> list[object]:
        from reportlab.platypus import PageBreak

        styles = _styles()
        contents = [
            ("PART 1", "강의 노트", "슬라이드와 교수님 설명 (STT)"),
            ("PART 2", "복습 퀴즈", f"Review quiz — {count} questions (English)"),
            ("", "정답과 해설", "퀴즈 다음 페이지부터"),
        ]
        flowables = _cover(
            document, "복습 · 강의 노트 · 퀴즈", contents,
            "퀴즈는 AI가 이 강의의 전사를 바탕으로 만들었습니다. 틀린 내용이 있을 수 있으니, "
            "헷갈리는 부분은 강의 노트(PART 1)에서 교수님 설명으로 확인하세요.",
            styles,
        )
        if document.segments or document.has_slides:
            flowables += [
                PageBreak(),
                *_part_banner("PART 1", "강의 노트", "슬라이드와 교수님 설명 (STT)", styles),
                *notes_story(document, notes_options, width, height, with_title=False),
            ]
        flowables += [PageBreak(), *_quiz(study, styles)]
        flowables += [PageBreak(), *_answers(study, styles, width)]
        return flowables

    build_pdf(path, f"{document.title} - 복습", story)


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
            "slides_ref": ParagraphStyle(
                "slides_ref", fontName=FONT_REGULAR, fontSize=8.5, leading=12,
                textColor=c(_GREY), spaceAfter=5,
            ),
            "point": ParagraphStyle(
                "point", fontName=FONT_REGULAR, fontSize=10.5, leading=17,
                textColor=c(_BODY), leftIndent=20, firstLineIndent=-20, spaceAfter=5,
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


def _cover(
    document: LectureDocument,
    subtitle: str,
    contents: list[tuple[str, str, str]],
    caution: str,
    styles: dict[str, object],
) -> list[object]:
    """``contents`` is (tag, title, note) for each thing in the file."""
    from reportlab.platypus import Paragraph, Spacer

    meta = [document.video_name]
    if document.duration:
        meta.append(format_duration(document.duration))
    meta.append(f"{datetime.now():%Y-%m-%d}")

    flowables: list[object] = [
        Spacer(1, 40),
        Paragraph(escape(document.title), styles["cover_title"]),
        Paragraph(escape(subtitle), styles["cover_sub"]),
        Paragraph(escape("  ·  ".join(meta)), styles["meta"]),
        Spacer(1, 26),
        Paragraph("이 자료의 구성", styles["h2"]),
    ]
    for tag, title, note in contents:
        label = f'<font color="{_ACCENT}"><b>{escape(tag)}</b></font>&nbsp;&nbsp;' if tag else ""
        words = f'&nbsp;&nbsp;<font color="{_GREY}">{escape(note)}</font>' if note else ""
        flowables.append(
            Paragraph(f"{label}<b>{escape(title)}</b>{words}", styles["point"])
        )
    flowables.append(Paragraph(escape(caution), styles["disclaimer"]))
    return flowables


# -- part 2: the quiz, and the answers on the next page -----------------------------------


def _quiz(study: StudySet, styles: dict[str, object]) -> list[object]:
    from reportlab.platypus import KeepTogether

    quiz = study.quiz
    assert quiz is not None
    flowables = _part_banner(
        "PART 2", "복습 퀴즈",
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
