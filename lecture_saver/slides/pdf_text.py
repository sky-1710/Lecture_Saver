"""Reading a PDF, on this machine (§1, §29).

The lecture's PDF can be kept with it, for the typo check, the preview and the
review (core/reference.py). This reads it with Qt's PDF module, which is PDFium
and already comes with PySide6-Addons: local, offline, nothing else to install.

Words come from the text layer. Slides made in PowerPoint have one; a page that
is only a picture reads as empty, and a PDF that is all pictures - a scan - says
so rather than quietly contributing nothing (§32).

Pictures of the pages are drawn when the PDF is attached and kept with the
lecture, because the PDF itself is not: it may have moved by the time the review
is made, and only its name is remembered. A diagram has no words to read, which
is what the pictures are for.

The document object is made and dropped here, so this runs on whichever thread
calls it.
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "MAX_PAGE_PICTURES",
    "PdfReadError",
    "page_picture_name",
    "read_pdf_pages",
    "render_pdf_pages",
]

#: How many pages get a picture. Lecture decks are shorter than this; a textbook
#: of a thousand pages would take minutes to draw and would not fit in a request.
MAX_PAGE_PICTURES = 60

#: The long side of a page's picture, in pixels: enough to read a slide's small
#: print, and what an assistant's image limit would scale a larger one down to.
_PICTURE_SIZE = 1280
_PICTURE_QUALITY = 80


class PdfReadError(RuntimeError):
    """The PDF could not be read. ``str(err)`` is always safe to show a user."""


def _tidy(text: str) -> str:
    """One page's text, without the blank lines and the runs of spaces.

    Composed, because a Korean PDF can carry Hangul as loose letters, and a
    prompt should not spend its room on the spacing of a layout.
    """
    lines = (" ".join(line.split()) for line in text.splitlines())
    return unicodedata.normalize("NFC", "\n".join(line for line in lines if line))


def _open(path: Path) -> Any:
    """The loaded document. Its error, if any, is one that can be shown."""
    from PySide6.QtPdf import QPdfDocument

    document = QPdfDocument()
    error = document.load(str(path))
    if error == QPdfDocument.Error.FileNotFound:
        raise PdfReadError(f"{Path(path).name} could not be found.")
    if error in (
        QPdfDocument.Error.IncorrectPassword,
        QPdfDocument.Error.UnsupportedSecurityScheme,
    ):
        raise PdfReadError(
            f"{Path(path).name} is protected by a password, so its text cannot be read."
        )
    if error != QPdfDocument.Error.None_:
        log.warning("Could not load a PDF: %s", error)
        raise PdfReadError(f"{Path(path).name} could not be read as a PDF.")
    return document


def read_pdf_pages(path: Path) -> tuple[str, ...]:
    """The text of every page, in order, "" for a page without any."""
    document = _open(path)
    pages = tuple(
        _tidy(document.getAllText(page).text()) for page in range(document.pageCount())
    )
    document.close()
    if not any(pages):
        raise PdfReadError(
            f"No text could be read from {Path(path).name}. Its pages are probably "
            "pictures, as in a scan, and only a PDF's text can be used."
        )
    return pages


def page_picture_name(page: int) -> str:
    """``3`` -> ``p003.jpg``: sorts in page order, and says nothing else."""
    return f"p{page:03d}.jpg"


def _draw_page(document: Any, page: int, pixels: Any) -> Any:
    """A page of a loaded document as an image, on white.

    A page's background is often simply unpainted, which as a JPEG is black.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage, QPainter

    picture = QImage(pixels, QImage.Format.Format_RGB32)
    picture.fill(Qt.GlobalColor.white)
    painter = QPainter(picture)
    painter.drawImage(0, 0, document.render(page, pixels))
    painter.end()
    return picture


def render_pdf_pages(path: Path, folder: Path, *, limit: int = MAX_PAGE_PICTURES) -> int:
    """Draw the first ``limit`` pages into ``folder``. Returns how many were drawn."""
    from PySide6.QtCore import QSize

    document = _open(path)
    folder.mkdir(parents=True, exist_ok=True)
    count = min(document.pageCount(), limit)
    for page in range(count):
        size = document.pagePointSize(page)
        scale = _PICTURE_SIZE / max(size.width(), size.height(), 1.0)
        pixels = QSize(round(size.width() * scale), round(size.height() * scale))
        picture = _draw_page(document, page, pixels)
        if not picture.save(str(folder / page_picture_name(page + 1)), quality=_PICTURE_QUALITY):
            document.close()
            raise PdfReadError(f"A picture of page {page + 1} could not be saved.")
    document.close()
    return count
