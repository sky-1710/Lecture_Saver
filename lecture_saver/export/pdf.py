"""Slide + transcript PDF (§19, §20).

Built locally with ReportLab's Platypus layout engine - no browser, no service.
The goal is lecture notes rather than a debug dump:

* Each slide is a block: heading, time range and a large picture, kept together
  on one page. The picture fills the text width but is capped in height, so a
  few lines of transcript still fit beneath it.
* The transcript follows as paragraphs split at pauses in the speech, and flows
  onto as many pages as it needs.
* An imported translation made line by line puts each Korean line directly
  under the timestamped transcript line it translates.
* One that pairs English with Korean paragraphs replaces the plain transcript,
  with a light rule between every paragraph so the original and its
  translation never run together.
* The user's screenshots of the video, with their marks drawn in, can sit in the
  transcript where they were taken: after the paragraph being spoken then.
  They also have a PDF of their own, :func:`write_screenshots_pdf`: only the
  screenshots, each page exactly one, edge to edge, to print and study from.
* Korean, English and mixed text all come from one embedded TrueType font. If no
  font that can draw Korean is found the export fails with a clear message
  instead of producing a PDF full of empty boxes.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from itertools import groupby
from pathlib import Path
from xml.sax.saxutils import escape

from ..core.atomic_io import replace_file
from ..core.timecode import format_duration, format_hms
from .document import LectureDocument, Screenshot, Section, paragraphs
from .options import PdfOptions

__all__ = [
    "FONT_BOLD",
    "FONT_REGULAR",
    "PdfExportError",
    "build_pdf",
    "find_korean_font",
    "notes_story",
    "pdf_styles",
    "printable",
    "write_pdf",
    "write_screenshots_pdf",
]

#: Fraction of the text frame's height a slide picture may take.
MAX_IMAGE_HEIGHT_FRACTION = 0.5
#: Width of a page of the screenshots PDF, in points: a widescreen PowerPoint
#: slide's (13.33 in), so a 16:9 lecture prints the way a slide does. Each
#: page is as high as its picture's shape makes it.
SCREENSHOT_PAGE_WIDTH = 960.0

_REGULAR = "LectureSaverSans"
_BOLD = "LectureSaverSans-Bold"
#: For what the Korean font has no shape for: an assistant's check mark or a
#: subscript would otherwise print as an empty box. Optional, so it is only a
#: loss of those few characters on a computer without one of these fonts.
_SYMBOLS = "LectureSaverSymbols"
_SYMBOL_FILES = ("seguisym.ttf", "DejaVuSans.ttf")
#: Public names for other PDFs built on this module (the preview and review).
FONT_REGULAR = _REGULAR
FONT_BOLD = _BOLD

#: Characters that shape the one before them and have no shape of their own.
_INVISIBLE = re.compile("[\ufe0e\ufe0f\u200d]")


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
    _register_symbols()


def _register_symbols() -> None:
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    for directory in _font_dirs():
        for name in _SYMBOL_FILES:
            path = directory / name
            if not path.is_file():
                continue
            try:
                pdfmetrics.registerFont(TTFont(_SYMBOLS, str(path)))
            except Exception:  # noqa: BLE001 - the PDF is still fine without it
                continue
            for bold in (0, 1):
                addMapping(_SYMBOLS, bold, 0, _SYMBOLS)
            return


def printable(text: str) -> str:
    """``text`` escaped for a Paragraph, in fonts that can draw every character."""
    from reportlab.pdfbase import pdfmetrics

    text = _INVISIBLE.sub("", text)
    if _SYMBOLS not in pdfmetrics.getRegisteredFontNames():
        return escape(text)
    korean = pdfmetrics.getFont(_REGULAR).face.charToGlyph
    symbols = pdfmetrics.getFont(_SYMBOLS).face.charToGlyph

    def needs_symbols(character: str) -> bool:
        return ord(character) not in korean and ord(character) in symbols

    parts: list[str] = []
    for missing, run in groupby(text, key=needs_symbols):
        words = escape("".join(run))
        parts.append(f'<font name="{_SYMBOLS}">{words}</font>' if missing else words)
    return "".join(parts)


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

    def story(width: float, height: float) -> list[object]:
        flowables = _story(document, options, width, height)
        return _preface(preface) + flowables if preface else flowables

    build_pdf(path, document.title, story)


def write_screenshots_pdf(document: LectureDocument, path: Path) -> None:
    """Write only the screenshots, in the order of the lecture, to ``path``.

    One page per screenshot, the page the shape of the picture and the picture
    filling it: no margins, no title, no captions - each page is the frame as
    it was taken, with its marks. Which screenshot a page is, its time, its
    slide and where its marking began are the PDF's bookmarks instead, where
    they cover nothing. The JPEGs go in as they are, not re-compressed.
    """
    shots = [
        (section.number, shot) for section in document.sections for shot in section.screenshots
    ]
    if not shots:
        raise PdfExportError(
            "There are no screenshots to export yet. Press S while the video plays "
            "to take one."
        )
    try:
        from reportlab import rl_config
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise PdfExportError(
            "PDF export needs the 'reportlab' package. Install the requirements "
            "again with: pip install -r requirements.txt"
        ) from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=".screenshots_", suffix=".pdf", dir=path.parent)
    os.close(handle)
    temp = Path(temp_name)
    # The pictures are the whole file: kept binary, not spelled out in ASCII
    # (reportlab's default), which made the PDF a quarter larger.
    ascii_images, rl_config.useA85 = rl_config.useA85, 0
    try:
        pdf = canvas.Canvas(str(temp))
        pdf.setTitle(f"{document.title} - Screenshots")
        pdf.setCreator("Lecture Saver")
        pages = 0
        for position, (slide, shot) in enumerate(shots, start=1):
            size = _picture_size(shot.image)
            if size is None:
                continue
            height = SCREENSHOT_PAGE_WIDTH * size[1] / size[0]
            pdf.setPageSize((SCREENSHOT_PAGE_WIDTH, height))
            pdf.drawImage(str(shot.image), 0, 0, SCREENSHOT_PAGE_WIDTH, height)
            parts = [f"{position}", format_hms(shot.time)]
            if slide is not None:
                parts.append(f"Slide {slide}")
            if shot.start < shot.time:
                parts.append(f"marked from {format_hms(shot.start)}")
            key = f"screenshot-{position}"
            pdf.bookmarkPage(key)
            pdf.addOutlineEntry("  ·  ".join(parts), key, level=0)
            pdf.showPage()
            pages += 1
        if not pages:
            raise PdfExportError("None of the screenshots' pictures could be read.")
        pdf.save()
        replace_file(temp, path)
    except PdfExportError:
        temp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - report failures, never half a file
        temp.unlink(missing_ok=True)
        raise PdfExportError(f"The PDF could not be generated:\n{exc}") from exc
    finally:
        rl_config.useA85 = ascii_images


def _picture_size(path: Path) -> tuple[int, int] | None:
    """A picture's size in pixels, or ``None`` if it cannot be read."""
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as picture:
            width, height = picture.size
    except Exception:  # noqa: BLE001 - an unreadable picture must not sink the PDF
        return None
    return (width, height) if width > 0 and height > 0 else None


def build_pdf(
    path: Path, title: str, make_story: Callable[[float, float], list[object]]
) -> None:
    """Lay out ``make_story(width, height)`` as an A4 PDF at ``path``, atomically.

    The one place a PDF is written: the Korean font, the margins, the footer
    and the never-half-a-file rule are the same for the notes and for the
    study PDFs. ``make_story`` is given the text frame's size so pictures can
    be scaled to fit it.
    """
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
        title=title,
        creator="Lecture Saver",
    )
    try:
        story = make_story(doc.width, doc.height)
        doc.build(
            story,
            onFirstPage=lambda canvas, d: _footer(canvas, d, title),
            onLaterPages=lambda canvas, d: _footer(canvas, d, title),
        )
        replace_file(temp, path)
    except PdfExportError:
        temp.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 - report layout failures, never half a file
        temp.unlink(missing_ok=True)
        raise PdfExportError(f"The PDF could not be generated:\n{exc}") from exc


def pdf_styles() -> dict[str, object]:
    """The notes' paragraph styles, for other PDFs that should match them."""
    return _styles()


def notes_story(
    document: LectureDocument,
    options: PdfOptions,
    width: float,
    height: float,
    *,
    with_title: bool = True,
) -> list[object]:
    """The slide-by-slide notes as flowables, to place inside a larger PDF."""
    return _story(document, options, width, height, with_title=with_title)


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
        "translation": ParagraphStyle(
            "translation", fontName=_REGULAR, fontSize=10.5, leading=17,
            textColor=colors.HexColor("#1E3A8A"),
        ),
        # The Korean under one timestamped line, lined up with its text.
        "translation_line": ParagraphStyle(
            "translation_line", fontName=_REGULAR, fontSize=10.5, leading=16,
            leftIndent=52, spaceAfter=8, textColor=colors.HexColor("#1E3A8A"),
        ),
        # The English an answer repeats above each Korean paragraph.
        "translation_source": ParagraphStyle(
            "translation_source", fontName=_REGULAR, fontSize=10.5, leading=17,
            textColor=colors.HexColor("#1F2937"),
        ),
        "note": ParagraphStyle(
            "note", fontName=_REGULAR, fontSize=9.5, leading=14, textColor=grey, spaceAfter=6,
        ),
    }


def _story(
    document: LectureDocument,
    options: PdfOptions,
    width: float,
    height: float,
    *,
    with_title: bool = True,
) -> list[object]:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer

    styles = _styles()
    story: list[object] = []
    if with_title:
        story.append(Paragraph(escape(document.title), styles["title"]))
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

        shots = list(section.screenshots) if options.screenshots else []
        translated = options.translation and bool(section.translation)
        lines = section.timed_translation if translated else {}
        if (lines or (translated and section.bilingual) or not options.transcript) and shots:
            # Without a plain transcript to place them in, they follow the slide.
            story += [_screenshot(shot, width, height, styles) for shot in shots]
            shots = []
        if lines:
            story += _interleaved(section, lines, styles, labelled=slides)
            continue
        if translated and section.bilingual:
            # The pairs carry the English, so it is not printed twice.
            story += _translation(section, styles)
            continue
        if options.transcript:
            story += _transcript(section, options, styles, labelled=slides,
                                 shots=[_screenshot_at(s, width, height, styles) for s in shots])
        if translated:
            story += _translation(section, styles)

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

    if section.repeat_of is not None:
        return Paragraph(
            f"(Same slide as Slide {section.repeat_of} - its picture is shown there.)",
            styles["note"],
        )
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
    section: Section, options: PdfOptions, styles: dict[str, object], *, labelled: bool,
    shots: list[tuple[float, object]] | None = None,
) -> list[object]:
    """The section's speech; ``shots`` - (time, flowable) - go in where taken."""
    from reportlab.platypus import Paragraph

    flowables: list[object] = []
    pending = list(shots or [])
    segments = section.spoken
    if labelled:
        flowables.append(Paragraph("Transcript", styles["label"]))
    if not segments:
        if labelled:
            flowables.append(
                Paragraph("Nothing was said while this slide was on screen.", styles["note"])
            )
        return flowables + [shot for _, shot in pending]

    def taken_before(start: float) -> None:
        # A screenshot comes after the paragraph being spoken when it was
        # taken, so it goes in before the first one to begin after it.
        while pending and pending[0][0] < start:
            flowables.append(pending.pop(0)[1])

    if options.segment_timestamps:
        for segment in segments:
            taken_before(segment.start)
            stamp = f'<font color="#6B7280">{format_hms(segment.start)}</font>'
            text = escape(segment.effective_text.strip())
            flowables.append(Paragraph(f"{stamp}&nbsp;&nbsp;&nbsp;{text}", styles["stamped"]))
    else:
        for group in paragraphs(segments):
            taken_before(group[0].start)
            text = " ".join(s.effective_text.strip() for s in group)
            flowables.append(Paragraph(escape(text), styles["body"]))
    return flowables + [shot for _, shot in pending]


def _screenshot_at(
    shot: Screenshot, width: float, height: float, styles: dict[str, object]
) -> tuple[float, object]:
    return shot.time, _screenshot(shot, width, height, styles)


def _screenshot(
    shot: Screenshot, width: float, height: float, styles: dict[str, object]
) -> object:
    """A screenshot with its time above it, kept on one page."""
    from reportlab.platypus import Image, KeepTogether, Paragraph

    caption = f"Screenshot · {format_hms(shot.time)}"
    if shot.start < shot.time:
        caption += f"  ·  marked from {format_hms(shot.start)}"
    try:
        from PIL import Image as PILImage

        with PILImage.open(shot.image) as picture:
            pixel_width, pixel_height = picture.size
    except Exception:  # noqa: BLE001 - an unreadable picture must not sink the PDF
        return Paragraph(f"({caption}: the picture could not be read.)", styles["note"])
    scale = min(width / pixel_width, height * MAX_IMAGE_HEIGHT_FRACTION / pixel_height)
    image = Image(str(shot.image), width=pixel_width * scale, height=pixel_height * scale)
    image.hAlign = "CENTER"
    return KeepTogether([Paragraph(escape(caption), styles["label"]), image])


def _interleaved(
    section: Section, lines: dict[str, str], styles: dict[str, object], *, labelled: bool
) -> list[object]:
    """Every transcript line with its timestamp, its Korean right beneath it.

    The timestamps are always shown here: they are what ties each Korean line
    to its original. A line the answer skipped is left with its English only.
    """
    from reportlab.platypus import KeepTogether, Paragraph

    flowables: list[object] = []
    if labelled:
        flowables.append(Paragraph("Transcript · 한국어 직역", styles["label"]))
    for segment in section.spoken:
        stamp = f'<font color="#6B7280">{format_hms(segment.start)}</font>'
        text = escape(segment.effective_text.strip())
        pair: list[object] = [Paragraph(f"{stamp}&nbsp;&nbsp;&nbsp;{text}", styles["stamped"])]
        korean = lines.get(segment.id)
        if korean:
            pair.append(Paragraph(escape(korean), styles["translation_line"]))
        flowables.append(KeepTogether(pair))
    return flowables


def _translation(section: Section, styles: dict[str, object]) -> list[object]:
    """The imported translation under a slide, a rule before every paragraph.

    An answer in the requested shape alternates English and Korean paragraphs;
    the English keeps the body colour and the Korean the translation colour,
    with a rule between each so the two never read as one block.
    """
    from reportlab.platypus import Paragraph

    flowables: list[object] = []
    if not section.bilingual:
        flowables += [_separator(), Paragraph("한국어 직역", styles["label"])]
    for position, block in enumerate(section.translation_blocks):
        if section.bilingual or position > 0:
            flowables.append(_separator())
        style = styles["translation"] if block.korean else styles["translation_source"]
        flowables.append(Paragraph(escape(block.text), style))
    return flowables


def _separator() -> object:
    from reportlab.lib import colors
    from reportlab.platypus import HRFlowable

    return HRFlowable(
        width="100%", thickness=0.6, color=colors.HexColor("#C4C9D2"),
        spaceBefore=10, spaceAfter=10, dash=(3, 2),
    )


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
