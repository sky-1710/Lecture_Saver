"""Continuing a transcription that did not finish (§26).

A long lecture can take hours on a CPU. The pipeline already flushes the
transcript to disk as it goes, so a cancel, a failure or a crash leaves most of
the work behind. This module decides whether that partial transcript can be
continued, and from where, so the remaining audio is transcribed instead of the
whole lecture again.

The last kept segment may have been cut off mid-sentence when the run stopped,
so transcription restarts at its *start* and it is replaced - unless the user
has already corrected it, in which case it is kept and transcription restarts
after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..core.models import StageStatus, TranscriptSegment
from ..core.project import Project

__all__ = ["MIN_REMAINING_SECONDS", "ResumePoint", "next_segment_number", "resume_point"]

#: Less audio than this left is not worth offering to continue.
MIN_REMAINING_SECONDS = 20.0

_UNFINISHED = {StageStatus.CANCELLED, StageStatus.FAILED, StageStatus.RUNNING}
_NUMBER = re.compile(r"^segment_(\d+)$")


@dataclass(frozen=True)
class ResumePoint:
    #: Seconds into the audio where transcription continues.
    start: float
    #: Segments kept from the interrupted run, in order.
    keep: tuple[TranscriptSegment, ...]


def resume_point(project: Project) -> ResumePoint | None:
    """Where an unfinished transcription can continue, or ``None``."""
    segments = project.transcript.segments
    if not segments or project.stage_status("transcription") not in _UNFINISHED:
        return None
    duration = project.video.duration
    last = segments[-1]
    if duration and duration - last.end < MIN_REMAINING_SECONDS:
        return None
    if last.is_edited:
        return ResumePoint(start=last.end, keep=tuple(segments))
    return ResumePoint(start=last.start, keep=tuple(segments[:-1]))


def next_segment_number(segments: tuple[TranscriptSegment, ...] | list[TranscriptSegment]) -> int:
    """The first ``segment_NNN`` number not used by ``segments``."""
    highest = 0
    for segment in segments:
        match = _NUMBER.match(segment.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1
