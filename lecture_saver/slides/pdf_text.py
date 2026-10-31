"""Reading the words in a PDF, on this machine (§1, §29).

The lecture's PDF can be kept with it as background for the typo check
(core/reference.py). This reads it with Qt's PDF module, which is PDFium and
already comes with PySide6-Addons: local, offline, nothing else to install.

Only a text layer is read. Slides made in PowerPoint have one; a page that is
only a picture reads as empty, and a PDF that is all pictures - a scan - says
so rather than quietly contributing nothing (§32).

The document object is made and dropped here, so this runs on whichever thread
calls it.
"""

from __future__ import annotations

import logging
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["PdfReadError", "read_pdf_pages"]


class PdfReadError(RuntimeError):
    """The PDF could not be read. ``str(err)`` is always safe to show a user."""


def _tidy(text: str) -> str:
    """One page's text, without the blank lines and the runs of spaces.

    Composed, because a Korean PDF can carry Hangul as loose letters, and a
    prompt should not spend its room on the spacing of a layout.
    """
    lines = (" ".join(line.split()) for line in text.splitlines())
    return unicodedata.normalize("NFC", "\n".join(line for line in lines if line))


def read_pdf_pages(path: Path) -> tuple[str, ...]:
    """The text of every page, in order, "" for a page without any."""
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
