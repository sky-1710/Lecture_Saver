"""What to export and how (§20, §21, §23).

Plain dataclasses that round-trip through ``project.json``'s ``export_settings``,
so the choices made on the export screen are still there next time (§5).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from pathlib import Path
from typing import Any

__all__ = [
    "ExportFormat",
    "ExportSettings",
    "FILE_NAMES",
    "PDF_PRESETS",
    "PdfOptions",
    "default_output_dir",
    "export_file_name",
    "preset_name_for",
    "resolve_output_dir",
    "safe_file_stem",
]

#: Used when the lecture has no usable name at all.
_FALLBACK_NAME = "lecture"

#: Everything Windows forbids in a file name, plus whitespace.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f\s]+')


def safe_file_stem(title: str) -> str:
    """``"Machine Learning 04"`` -> ``Machine_Learning_04``.

    Anything a file or folder name cannot hold becomes an underscore.
    """
    safe = _UNSAFE.sub("_", title).strip("_. ")
    # A name made only of separators (or only dots) leaves nothing to use.
    return safe or _FALLBACK_NAME


def default_output_dir(project_exports: Path, title: str, default_dir: Path | None) -> Path:
    """Where a lecture exports when it has no folder of its own.

    The app-wide default folder (an iCloud Drive folder, say) when one is set -
    every file name starts with the lecture's name, so lectures sit side by
    side there without overwriting each other. Otherwise the project's own
    ``exports`` folder.
    """
    return project_exports if default_dir is None else Path(default_dir)


def export_file_name(title: str, export_format: ExportFormat) -> str:
    """``("OS02 Limited", PDF)`` -> ``OS02_Limited_lecture_notes.pdf``."""
    return f"{safe_file_stem(title)}_{FILE_NAMES[export_format]}"


def resolve_output_dir(
    stored: str | None, project_exports: Path, title: str, default_dir: Path | None
) -> Path:
    """The folder an export writes to: the lecture's own choice, else the default."""
    if stored:
        return Path(stored)
    return default_output_dir(project_exports, title, default_dir)


class ExportFormat(str, Enum):
    PDF = "pdf"
    #: The user's screenshots on their own, with their marks - for printing.
    SCREENSHOTS = "screenshots"
    TXT = "txt"
    MARKDOWN = "md"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"

    @property
    def label(self) -> str:
        return _LABELS[self]


_LABELS = {
    ExportFormat.PDF: "Slide + Transcript PDF",
    ExportFormat.SCREENSHOTS: "Screenshots PDF",
    ExportFormat.TXT: "Transcript TXT",
    ExportFormat.MARKDOWN: "Markdown",
    ExportFormat.SRT: "SRT subtitles",
    ExportFormat.VTT: "VTT subtitles",
    ExportFormat.JSON: "JSON",
}

#: File names inside the output folder, as in the project layout (§5). Written
#: with the lecture's name in front - see :func:`export_file_name`.
FILE_NAMES = {
    ExportFormat.PDF: "lecture_notes.pdf",
    ExportFormat.SCREENSHOTS: "screenshots.pdf",
    ExportFormat.TXT: "transcript.txt",
    ExportFormat.MARKDOWN: "transcript.md",
    ExportFormat.SRT: "transcript.srt",
    ExportFormat.VTT: "transcript.vtt",
    ExportFormat.JSON: "transcript.json",
}


@dataclass(frozen=True)
class PdfOptions:
    """What each slide's block in the PDF contains (§20)."""

    slide_image: bool = True
    slide_number: bool = True
    time_range: bool = True
    transcript: bool = True
    segment_timestamps: bool = False
    #: The imported translation under each slide's transcript, when there is one.
    translation: bool = True
    #: The user's screenshots in the notes too, where they were taken. Off by
    #: default: they have a PDF of their own (:attr:`ExportFormat.SCREENSHOTS`).
    screenshots: bool = False

    @property
    def shows_slides(self) -> bool:
        return self.slide_image or self.slide_number or self.time_range

    @property
    def is_empty(self) -> bool:
        return not (self.shows_slides or self.transcript or self.translation
                    or self.screenshots)


PDF_PRESETS: dict[str, PdfOptions] = {
    "Slide + Transcript": PdfOptions(),
    "Slide + Timestamped Transcript": PdfOptions(segment_timestamps=True),
    "Slides Only": PdfOptions(transcript=False, translation=False),
    "Transcript Only": PdfOptions(slide_image=False, slide_number=False, time_range=False),
}


def preset_name_for(options: PdfOptions) -> str | None:
    for name, preset in PDF_PRESETS.items():
        if preset == options:
            return name
    return None


@dataclass
class ExportSettings:
    """Everything the export screen chooses."""

    formats: list[ExportFormat] = field(
        default_factory=lambda: [ExportFormat.PDF, ExportFormat.TXT]
    )
    #: Timestamps in the TXT and Markdown transcript. SRT and VTT always have
    #: them, and JSON always carries both times.
    include_timestamps: bool = True
    pdf: PdfOptions = field(default_factory=PdfOptions)
    #: ``None`` means the default: the app-wide export folder when one is set,
    #: otherwise the project's own ``exports`` folder.
    output_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "formats": [f.value for f in self.formats],
            "include_timestamps": self.include_timestamps,
            "pdf": asdict(self.pdf),
            "output_dir": self.output_dir,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExportSettings:
        """Tolerant of missing or unknown keys, like every stored setting."""
        settings = cls()
        if not data:
            return settings
        raw_formats = data.get("formats")
        if isinstance(raw_formats, list):
            known = {f.value: f for f in ExportFormat}
            settings.formats = [known[v] for v in raw_formats if v in known]
        if "include_timestamps" in data:
            settings.include_timestamps = bool(data["include_timestamps"])
        raw_pdf = data.get("pdf")
        if isinstance(raw_pdf, dict):
            names = {f.name for f in fields(PdfOptions)}
            settings.pdf = replace(
                PdfOptions(), **{k: bool(v) for k, v in raw_pdf.items() if k in names}
            )
        output = data.get("output_dir")
        settings.output_dir = str(output) if output else None
        return settings

    @classmethod
    def for_lecture(
        cls, stored: dict[str, Any] | None, app_defaults: dict[str, Any] | None
    ) -> ExportSettings:
        """The lecture's own choices, or else the last ones made for any lecture.

        A folder is never inherited: a lecture without one of its own follows
        the app-wide default export folder.
        """
        if stored:
            return cls.from_dict(stored)
        settings = cls.from_dict(app_defaults)
        settings.output_dir = None
        return settings

    def as_app_defaults(self) -> dict[str, Any]:
        """These choices for other lectures to start from - without the folder."""
        return {**self.to_dict(), "output_dir": None}
