"""Slide + transcript PDF (§19, §20).

Built locally with ReportLab's Platypus layout engine - no browser, no service.
The goal is lecture notes rather than a debug dump:

* Each slide is a block: heading, time range and a large picture, kept together
  on one page. The picture fills the text width but is capped in height, so a
  few lines of transcript still fit beneath it.
* The transcript follows as paragraphs split at pauses in the speech, and flows
  onto as many pages as it needs.
* Korean, English and mixed text all come from one embedded TrueType font. If no
  font that can draw Korean is found the export fails with a clear message
  instead of producing a PDF full of empty boxes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.timecode import format_duration, format_hms
from .document import LectureDocument, Section, paragraphs
from .options import PdfOptions

__all__ = ["PdfExportError", "find_korean_font", "write_pdf"]

#: Fraction of the text frame's height a slide picture may take.
MAX_IMAGE_HEIGHT_FRACTION = 0.5

_REGULAR = "LectureSaverSans"
_BOLD = "LectureSaverSans-Bold"


class PdfExportError(RuntimeError):
    """The PDF could not be produced. The message is safe to show the user."""


# -- fonts --------------------------------------------------------------------------


def _font_dirs() -> list[Path]:
    dirs = []
    windir = os.environ.get("WINDIR")
    if windir:
        dirs.append(Path(windir) / "Fonts")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    if sys.platform == "darwin":
        dirs += [Path("/Library/Fonts"), Path.home() / "Library" / "Fonts"]
    dirs += [
        Path("/usr/share/fonts/truetype/nanum"),
        Path("/usr/share/fonts/nanum"),
        Path.home() / ".fonts",
    ]
    return dirs


#: (regular, bold) TrueType files that cover Hangul and Latin. Checked in order.
_CANDIDATES = [
    ("malgun.ttf", "malgunbd.ttf"),
    ("NanumGothic.ttf", "NanumGothicBold.ttf"),
    ("NanumBarunGothic.ttf", "NanumBarunGothicBold.ttf"),
]


def find_korean_font() -> tuple[Path, Path] | None:
    """Regular and bold font files able to draw Korean, or ``None``.

    When only a regular face exists it is used for bold too.
    """
    for directory in _font_dirs():
        for regular, bold in _CANDIDATES:
            regular_path = directory / regular
            if regular_path.is_file():
                bold_path = directory / bold
                return regular_path, bold_path if bold_path.is_file() else regular_path
    return None


def _register_fonts() -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if _REGULAR in pdfmetrics.getRegisteredFontNames():
        return
    found = find_korean_font()
    if found is None:
        raise PdfExportError(
            "No font that can display Korean was found, so the PDF would show "
            "empty boxes instead of text.\n\nInstall Malgun Gothic (included with "
            "Windows) or Nanum Gothic and try again."
        )
    regular, bold = found
    try:
        pdfmetrics.registerFont(TTFont(_REGULAR, str(regular)))
        pdfmetrics.registerFont(TTFont(_BOLD, str(bold)))
    except Exception as exc:  # noqa: BLE001 - any font parsing failure is fatal here
        raise PdfExportError(f"The font {regular.name} could not be loaded:\n{exc}") from exc
    from reportlab.lib.fonts import addMapping

    addMapping(_REGULAR, 0, 0, _REGULAR)
    addMapping(_REGULAR, 1, 0, _BOLD)


# -- layout -------------------------------------------------------------------------


def write_pdf(
    document: LectureDocument,
    options: PdfOptions,
    path: Path,
    *,
    preface: str | None = None,
) -> None:
    """Write the notes PDF to ``path``, atomically.

    ``preface`` is Markdown-like text printed before the notes on its own page:
    ``#``/``##`` lines become headings, fenced blocks are kept line by line.
    """
    if options.is_empty:
        raise PdfExportError("Choose at least one thing to include in the PDF.")
    if not document.has_slides and not options.transcript:
        raise PdfExportError(
            "This lecture has no detected slides, so a PDF without the transcript "
            "would be empty. Detect slides or include the transcript."
        )
    if not document.has_slides and not document.segments:
        raise PdfExportError("There is nothing to export yet: no transcript and no slides.")

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate
    except ImportError as exc:
        raise PdfExportError(
            "PDF export needs the 'reportlab' package. Install the requirements "
            "again with: pip install -r requirements.txt"
        ) from exc

    _register_fonts()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".lecture_notes_", suffix=".pdf", dir=path.parent)
    os.close(handle)
    temp = Path(temp_name)

    margin = 18 * mm
    doc = SimpleDocTemplate(
        str(temp),
        pagesize=A4,
        leftMargin=margin, rightMargin=margin, topMargin=margin, bottomMargin=margin + 4 * mm,
        title=document.title,
        creator="Lecture Saver",
    )
    try:
        story = _story(document, options, doc.width, doc.height)
        if preface:
            story = _preface(preface) + story
        doc.build(
            story,
            onFirstPage=lambda canvas, d: _footer(canvas, d, document.title),
            onLaterPages=lambda canvas, d: _footer(canvas, d, document.title),
        )
        os.replace(temp, path)
    except PdfExportError:
        temp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - report layout failures, never half a file
        temp.unlink(missing_ok=True)
        raise PdfExportError(f"The PDF could not be generated:\n{exc}") from exc


def _styles() -> dict[str, object]:
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle

    grey = colors.HexColor("#6B7280")
    return {
        "title": ParagraphStyle(
            "title", fontName=_BOLD, fontSize=20, leading=26, spaceAfter=4,
            textColor=colors.HexColor("#111827"),
        ),
        "meta": ParagraphStyle("meta", fontName=_REGULAR, fontSize=9, leading=13, textColor=grey),
        "heading": ParagraphStyle(
            "heading", fontName=_BOLD, fontSize=13, leading=18, spaceBefore=2,
            textColor=colors.HexColor("#111827"),
        ),
        "range": ParagraphStyle(
            "range", fontName=_REGULAR, fontSize=9, leading=12, textColor=grey, spaceAfter=6,
        ),
        "label": ParagraphStyle(
            "label", fontName=_BOLD, fontSize=9, leading=12, textColor=grey,
            spaceBefore=8, spaceAfter=3,
        ),
        "body": ParagraphStyle(
            "body", fontName=_REGULAR, fontSize=10.5, leading=17, spaceAfter=7,
            textColor=colors.HexColor("#1F2937"),
        ),
        "stamped": ParagraphStyle(
            "stamped", fontName=_REGULAR, fontSize=10.5, leading=16, spaceAfter=4,
            leftIndent=52, firstLineIndent=-52, textColor=colors.HexColor("#1F2937"),
        ),
        "note": ParagraphStyle(
            "note", fontName=_REGULAR, fontSize=9.5, leading=14, textColor=grey, spaceAfter=6,
        ),
    }


def _story(
    document: LectureDocument, options: PdfOptions, width: float, height: float
) -> list[object]:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer

    styles = _styles()
    story: list[object] = [Paragraph(escape(document.title), styles["title"])]
    meta = [document.video_name]
    if document.duration:
        meta.append(format_duration(document.duration))
    meta.append(f"Exported {datetime.now():%Y-%m-%d}")
    story += [Paragraph(escape("  ·  ".join(meta)), styles["meta"]), Spacer(1, 10)]

    slides = document.has_slides and options.shows_slides
    for position, section in enumerate(document.sections):
        if slides and position > 0:
            story += [
                Spacer(1, 6),
                HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D1D5DB")),
                Spacer(1, 10),
            ]

        block: list[object] = []
        if document.has_slides and options.slide_number:
            block.append(Paragraph(escape(_heading(section)), styles["heading"]))
        if document.has_slides and options.time_range:
            block.append(
                Paragraph(
                    f"{format_hms(section.start)} – {format_hms(section.end)}", styles["range"]
                )
            )
        if document.has_slides and options.slide_image:
            block.append(_picture(section, width, height, styles))
        if block:
            story.append(KeepTogether(block))

        if options.transcript:
            story += _transcript(section, options, styles, labelled=slides)

    return story


def _preface(text: str) -> list[object]:
    """Render the preface text, then start the notes on a fresh page."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import HRFlowable, PageBreak, Paragraph, Spacer

    styles = _styles()
    block = ParagraphStyle(
        "block", parent=styles["body"], backColor=colors.HexColor("#F3F4F6"),
        borderPadding=(4, 6, 4, 6), leftIndent=6, rightIndent=6, spaceAfter=0,
        leading=15,
    )
    flowables: list[object] = []
    fenced = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            fenced = not fenced
            flowables.append(Spacer(1, 4))
            continue
        if fenced:
            flowables.append(Paragraph(escape(line) or "&nbsp;", block))
        elif line.startswith("# "):
            flowables.append(Paragraph(escape(line[2:]), styles["title"]))
        elif line.startswith("## "):
            flowables.append(Paragraph(escape(line[3:]), styles["heading"]))
        elif line == "---":
            flowables.append(
                HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#D1D5DB"))
            )
        elif line:
            flowables.append(Paragraph(escape(line), styles["body"]))
    flowables.append(PageBreak())
    return flowables


def _heading(section: Section) -> str:
    heading = f"Slide {section.number}"
    return f"{heading}  ·  {section.label}" if section.label else heading


def _picture(section: Section, width: float, height: float, styles: dict[str, object]) -> object:
    from reportlab.platypus import Image, Paragraph

    if section.image is None:
        return Paragraph("(No image was captured for this slide.)", styles["note"])
    try:
        from PIL import Image as PILImage

        with PILImage.open(section.image) as picture:
            pixel_width, pixel_height = picture.size
    except Exception:  # noqa: BLE001 - an unreadable picture must not sink the PDF
        return Paragraph("(The image for this slide could not be read.)", styles["note"])

    scale = min(width / pixel_width, height * MAX_IMAGE_HEIGHT_FRACTION / pixel_height)
    image = Image(str(section.image), width=pixel_width * scale, height=pixel_height * scale)
    image.hAlign = "CENTER"
    return image


def _transcript(
    section: Section, options: PdfOptions, styles: dict[str, object], *, labelled: bool
) -> list[object]:
    from reportlab.platypus import Paragraph

    flowables: list[object] = []
    segments = section.spoken
    if labelled:
        flowables.append(Paragraph("Transcript", styles["label"]))
    if not segments:
        if labelled:
            flowables.append(
                Paragraph("Nothing was said while this slide was on screen.", styles["note"])
            )
        return flowables

    if options.segment_timestamps:
        for segment in segments:
            stamp = f'<font color="#6B7280">{format_hms(segment.start)}</font>'
            text = escape(segment.effective_text.strip())
            flowables.append(Paragraph(f"{stamp}&nbsp;&nbsp;&nbsp;{text}", styles["stamped"]))
    else:
        for group in paragraphs(segments):
            text = " ".join(s.effective_text.strip() for s in group)
            flowables.append(Paragraph(escape(text), styles["body"]))
    return flowables


def _footer(canvas: object, doc: object, title: str) -> None:
    from reportlab.lib import colors
    from reportlab.lib.units import mm

    canvas.saveState()  # type: ignore[attr-defined]
    canvas.setFont(_REGULAR, 8)  # type: ignore[attr-defined]
    canvas.setFillColor(colors.HexColor("#9CA3AF"))  # type: ignore[attr-defined]
    left = doc.leftMargin  # type: ignore[attr-defined]
    right = doc.pagesize[0] - doc.rightMargin  # type: ignore[attr-defined]
    y = 10 * mm
    canvas.drawString(left, y, title[:90])  # type: ignore[attr-defined]
    canvas.drawRightString(right, y, str(doc.page))  # type: ignore[attr-defined]
    canvas.restoreState()  # type: ignore[attr-defined]
