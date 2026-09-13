"""Transcript exports: TXT, Markdown, SRT, VTT and JSON (§21, §22).

Pure functions from a :class:`LectureDocument` to text, so the formatting rules
are tested without writing files. Every format uses ``effective_text``; lines the
user blanked are left out of the readable formats, while JSON keeps every
segment with both its original and its edit.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..core.models import SCHEMA_VERSION, TranscriptSegment
from ..core.timecode import format_duration, format_hms, format_srt, format_vtt
from .document import LectureDocument, Section, paragraphs, spoken

__all__ = ["to_json", "to_markdown", "to_srt", "to_txt", "to_vtt"]

#: Subtitle players reject cues that end before they start.
MIN_CUE_SECONDS = 0.5


def _text(segment: TranscriptSegment) -> str:
    return segment.effective_text.strip()


def _section_title(section: Section) -> str:
    title = f"Slide {section.number}" if section.number is not None else "Transcript"
    return f"{title} · {section.label}" if section.label else title


# -- TXT ------------------------------------------------------------------------


def to_txt(document: LectureDocument, *, timestamps: bool = True) -> str:
    """Plain transcript, no slides (§21).

    With timestamps each segment is ``[HH:MM:SS]`` on its own line followed by
    its text; without, the speech is written as paragraphs.
    """
    segments = spoken(document.segments)
    if timestamps:
        blocks = [f"[{format_hms(s.start)}]\n{_text(s)}" for s in segments]
    else:
        blocks = [" ".join(_text(s) for s in group) for group in paragraphs(segments)]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


# -- Markdown -------------------------------------------------------------------


def to_markdown(
    document: LectureDocument, *, timestamps: bool = True, output_dir: Path | None = None
) -> str:
    """Slide-by-slide notes in Markdown, with images linked relative to the file."""
    lines = [f"# {_md_escape(document.title)}", ""]
    meta = [document.video_name]
    if document.duration:
        meta.append(format_duration(document.duration))
    lines += [f"*{_md_escape(' · '.join(meta))}*", ""]

    for section in document.sections:
        if document.has_slides:
            lines += [f"## {_section_title(section)}", ""]
            lines += [f"`{format_hms(section.start)} – {format_hms(section.end)}`", ""]
            if section.image is not None:
                target = _relative(section.image, output_dir)
                if any(char in target for char in " ()"):
                    target = f"<{target}>"
                lines += [f"![Slide {section.number}]({target})", ""]
        segments = section.spoken
        if timestamps:
            for segment in segments:
                lines += [f"**[{format_hms(segment.start)}]** {_md_escape(_text(segment))}", ""]
        else:
            for group in paragraphs(segments):
                lines += [_md_escape(" ".join(_text(s) for s in group)), ""]
        if document.has_slides and not segments:
            lines += ["*Nothing was said while this slide was on screen.*", ""]

    return "\n".join(lines).rstrip() + "\n"


def _md_escape(text: str) -> str:
    """Keep speech from being read as Markdown syntax at the start of a line."""
    escaped = text.replace("\\", "\\\\")
    # Underscores inside words never start emphasis in CommonMark, and file
    # names are full of them, so they are left readable.
    for marker in ("*", "`", "[", "]"):
        escaped = escaped.replace(marker, "\\" + marker)
    if escaped[:1] in {"#", ">", "-", "+"}:
        escaped = "\\" + escaped
    return escaped


def _relative(image: Path, output_dir: Path | None) -> str:
    if output_dir is None:
        return image.as_posix()
    try:
        return Path(os.path.relpath(image, output_dir)).as_posix()
    except ValueError:  # a different drive on Windows
        return image.as_posix()


# -- SRT / VTT --------------------------------------------------------------------


def _cues(document: LectureDocument) -> list[tuple[float, float, str]]:
    cues = []
    for segment in spoken(document.segments):
        end = max(segment.end, segment.start + MIN_CUE_SECONDS)
        cues.append((segment.start, end, _text(segment)))
    return cues


def to_srt(document: LectureDocument) -> str:
    blocks = [
        f"{number}\n{format_srt(start)} --> {format_srt(end)}\n{text}"
        for number, (start, end, text) in enumerate(_cues(document), start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def to_vtt(document: LectureDocument) -> str:
    blocks = ["WEBVTT"]
    for start, end, text in _cues(document):
        # "-->" inside cue text would be read as a timing line.
        safe = text.replace("-->", "->")
        blocks.append(f"{format_vtt(start)} --> {format_vtt(end)}\n{safe}")
    return "\n\n".join(blocks) + "\n"


# -- JSON ---------------------------------------------------------------------------


def _segment_json(segment: TranscriptSegment) -> dict[str, object]:
    return {
        "id": segment.id,
        "start": round(segment.start, 3),
        "end": round(segment.end, 3),
        "original_text": segment.original_text,
        "edited_text": segment.edited_text,
        "text": segment.effective_text,
    }


def to_json(document: LectureDocument, *, exported_at: datetime | None = None) -> str:
    """The structured export from §22, versioned like the project files."""
    exported = (exported_at or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    data: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "exported_at": exported,
        "lecture": document.title,
        "video": document.video_name,
        "duration": round(document.duration, 3),
        "language": document.language,
    }
    if document.has_slides:
        data["slides"] = [
            {
                "slide_id": section.slide_id,
                "appearance_id": section.number,
                "annotation_state": section.annotation_state,
                "start": round(section.start, 3),
                "end": round(section.end, 3),
                "image": section.image_ref or None,
                "transcript": [_segment_json(s) for s in section.segments],
            }
            for section in document.sections
        ]
    else:
        data["slides"] = []
        data["transcript"] = [_segment_json(s) for s in document.segments]
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
