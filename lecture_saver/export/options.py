"""What to export and how (§20, §21, §23).

Plain dataclasses that round-trip through ``project.json``'s ``export_settings``,
so the choices made on the export screen are still there next time (§5).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, replace
from enum import Enum
from typing import Any

__all__ = [
    "ExportFormat",
    "ExportSettings",
    "FILE_NAMES",
    "PDF_PRESETS",
    "PdfOptions",
    "preset_name_for",
]


class ExportFormat(str, Enum):
    PDF = "pdf"
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
    ExportFormat.TXT: "Transcript TXT",
    ExportFormat.MARKDOWN: "Markdown",
    ExportFormat.SRT: "SRT subtitles",
    ExportFormat.VTT: "VTT subtitles",
    ExportFormat.JSON: "JSON",
}

#: File names inside the output folder, as in the project layout (§5).
FILE_NAMES = {
    ExportFormat.PDF: "lecture_notes.pdf",
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

    @property
    def shows_slides(self) -> bool:
        return self.slide_image or self.slide_number or self.time_range

    @property
    def is_empty(self) -> bool:
        return not (self.shows_slides or self.transcript or self.translation)


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
    #: ``None`` means the project's own ``exports`` folder.
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
