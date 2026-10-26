"""Reading the words on slide images, on this machine (§1, §29).

The keyword hints Whisper is given (keywords.py) are taken from the slides, and
the slides are pictures: their text has to be recognised. This uses the OCR
engine built into Windows (``Windows.Media.Ocr``) - part of the operating
system, local, offline, nothing to download. It reads with whichever of the
English and Korean recognisers Windows has installed; a Korean Windows has the
Korean one, which reads Latin text too, with confusions keywords.py undoes.

Off Windows, or without a recogniser, :func:`unavailable_reason` says so and
the feature is shown as unavailable rather than pretending (§32). Another
engine can take this module's place: the rest of the application only sees
:class:`~lecture_saver.core.keywords.TextLine`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..core.keywords import TextLine

log = logging.getLogger(__name__)

__all__ = ["SlideTextReader", "unavailable_reason"]

#: Recognisers used when installed: English reads Latin text best, Korean the
#: Hangul. Windows names them by BCP-47 tag.
_WANTED = ("en", "ko")


@lru_cache(maxsize=1)
def unavailable_reason() -> str | None:
    """Why slide text cannot be read here, or ``None`` when it can."""
    if sys.platform != "win32":
        return "Reading slide text uses the OCR built into Windows."
    try:
        from winrt.windows.media.ocr import OcrEngine
    except Exception as exc:  # noqa: BLE001 - a missing binding is just "unavailable"
        log.info("Windows OCR bindings unavailable: %s", exc)
        return "The Windows OCR bindings are not installed (pip install -r requirements.txt)."
    try:
        tags = [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    except Exception as exc:  # noqa: BLE001
        log.info("Windows OCR probe failed: %s", exc)
        return "Windows OCR could not be started."
    if not _pick(tags):
        return (
            "Windows has no English or Korean OCR installed. Add one under "
            "Settings > Time & Language > Language."
        )
    return None


def _pick(tags: list[str]) -> list[str]:
    """The installed recognisers to use, one per wanted language."""
    chosen: list[str] = []
    for wanted in _WANTED:
        match = next((tag for tag in tags if tag.split("-")[0].lower() == wanted), None)
        if match:
            chosen.append(match)
    return chosen


class SlideTextReader:
    """Reads one slide image at a time. Build it on the thread that uses it."""

    def __init__(self) -> None:
        from winrt.windows.globalization import Language
        from winrt.windows.media.ocr import OcrEngine

        tags = _pick([lang.language_tag for lang in OcrEngine.available_recognizer_languages])
        self._engines: list[Any] = [
            engine for engine in
            (OcrEngine.try_create_from_language(Language(tag)) for tag in tags)
            if engine is not None
        ]
        if not self._engines:
            raise RuntimeError(unavailable_reason() or "Windows OCR could not be started.")

    def read(self, image_path: Path) -> list[TextLine]:
        """Every line of text each recogniser found, with its height."""
        bitmap, height = _bitmap(image_path)
        lines: list[TextLine] = []
        for engine in self._engines:
            result = asyncio.run(_recognize(engine, bitmap))
            for line in result.lines:
                tallest = max((word.bounding_rect.height for word in line.words), default=0.0)
                lines.append(TextLine(text=str(line.text), height=tallest / height))
        return lines


async def _recognize(engine: Any, bitmap: Any) -> Any:
    return await engine.recognize_async(bitmap)


def _bitmap(image_path: Path) -> tuple[Any, int]:
    """The image as the BGRA bitmap Windows OCR takes, and its height."""
    from PIL import Image
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    with Image.open(image_path) as source:
        image = source.convert("RGBA")
    red, green, blue, alpha = image.split()
    writer = DataWriter()
    writer.write_bytes(Image.merge("RGBA", (blue, green, red, alpha)).tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        writer.detach_buffer(), BitmapPixelFormat.BGRA8, image.width, image.height
    )
    return bitmap, image.height
