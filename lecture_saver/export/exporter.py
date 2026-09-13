"""Run a set of exports and report what happened (§23).

Each format is written independently: a failed PDF does not cost the TXT, and
the result lists every file written and every format that failed, with a
message fit for the user. Text files are written atomically; the PDF is built
beside its destination and moved into place.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.atomic_io import write_text_atomic
from .document import LectureDocument
from .options import FILE_NAMES, ExportFormat, ExportSettings
from .pdf import PdfExportError, write_pdf
from .text_formats import to_json, to_markdown, to_srt, to_txt, to_vtt

log = logging.getLogger(__name__)

__all__ = ["ExportResult", "run_export"]

ProgressCallback = Callable[[ExportFormat, int, int], None]


@dataclass
class ExportResult:
    output_dir: Path
    written: list[Path] = field(default_factory=list)
    #: (format, user-facing message) for each format that could not be written.
    failures: list[tuple[ExportFormat, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and bool(self.written)


def run_export(
    document: LectureDocument,
    settings: ExportSettings,
    output_dir: Path,
    *,
    on_progress: ProgressCallback | None = None,
) -> ExportResult:
    output_dir = Path(output_dir)
    result = ExportResult(output_dir=output_dir)
    formats = [f for f in ExportFormat if f in settings.formats]  # stable order
    if not formats:
        result.failures.append((ExportFormat.TXT, "Choose at least one format to export."))
        return result

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        for export_format in formats:
            result.failures.append((export_format, f"Could not create {output_dir}:\n{exc}"))
        return result

    for position, export_format in enumerate(formats):
        if on_progress is not None:
            on_progress(export_format, position, len(formats))
        target = output_dir / FILE_NAMES[export_format]
        try:
            _write_one(document, settings, export_format, target)
        except PdfExportError as exc:
            result.failures.append((export_format, str(exc)))
            continue
        except OSError as exc:
            log.warning("Export to %s failed", target, exc_info=True)
            result.failures.append((export_format, f"Could not write {target.name}:\n{exc}"))
            continue
        except Exception as exc:  # noqa: BLE001 - one format must not sink the rest
            log.exception("Unexpected failure exporting %s", export_format.value)
            result.failures.append((export_format, f"Unexpected error:\n{exc}"))
            continue
        result.written.append(target)

    return result


def _write_one(
    document: LectureDocument,
    settings: ExportSettings,
    export_format: ExportFormat,
    target: Path,
) -> None:
    if export_format is ExportFormat.PDF:
        write_pdf(document, settings.pdf, target)
        return

    if not document.segments:
        raise PdfExportError("There is no transcript to export yet. Transcribe the lecture first.")

    if export_format is ExportFormat.TXT:
        text = to_txt(document, timestamps=settings.include_timestamps)
    elif export_format is ExportFormat.MARKDOWN:
        text = to_markdown(
            document, timestamps=settings.include_timestamps, output_dir=target.parent
        )
    elif export_format is ExportFormat.SRT:
        text = to_srt(document)
    elif export_format is ExportFormat.VTT:
        text = to_vtt(document)
    else:
        text = to_json(document)
    write_text_atomic(target, text)
