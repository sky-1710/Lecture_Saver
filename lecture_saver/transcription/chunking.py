"""Packing a lecture's speech into Whisper-sized windows (§26).

Whisper reads 30 seconds of audio at a time. faster-whisper does its own
windowing, but the OpenVINO engine is handed one window per call - which is
also what lets it report progress and stop between windows instead of going
silent for ten minutes.

Voice activity detection finds where the lecturer is speaking. The windows here
pack that speech, with the silence between regions taken out, into pieces of at
most :data:`WINDOW_SECONDS`, and map a time inside a window back onto the
lecture. Leaving silence out matters twice over: it is not decoded, and Whisper
handed nothing but silence invents text rather than returning none.

Pure - regions in, windows out - so the packing and the time mapping can be
tested without audio.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["WINDOW_SECONDS", "Window", "plan_windows"]

#: Whisper's input length.
WINDOW_SECONDS = 30.0


@dataclass(frozen=True)
class Window:
    """Speech pieces decoded together, in lecture order."""

    #: ``(start, end)`` of each piece, in seconds of the source audio.
    pieces: tuple[tuple[float, float], ...]

    @property
    def start(self) -> float:
        return self.pieces[0][0]

    @property
    def end(self) -> float:
        return self.pieces[-1][1]

    @property
    def duration(self) -> float:
        """Seconds of audio in the window - the pieces, not the gaps."""
        return sum(end - start for start, end in self.pieces)

    def to_source(self, seconds: float) -> float:
        """A time in the window's audio -> the same moment in the lecture."""
        elapsed = 0.0
        for start, end in self.pieces:
            length = end - start
            if seconds <= elapsed + length:
                return start + max(0.0, seconds - elapsed)
            elapsed += length
        # Whisper can put a final timestamp a little past the audio it was given.
        return self.end


def plan_windows(
    regions: Iterable[tuple[float, float]], max_seconds: float = WINDOW_SECONDS
) -> list[Window]:
    """Pack speech ``regions`` (in order) into windows of at most ``max_seconds``.

    A region is never split across windows unless it is longer than a window on
    its own - voice activity detection is asked to keep regions shorter than
    that, so the split here is only a safety net.
    """
    windows: list[Window] = []
    current: list[tuple[float, float]] = []
    used = 0.0

    def close() -> None:
        nonlocal current, used
        if current:
            windows.append(Window(tuple(current)))
        current, used = [], 0.0

    for start, end in regions:
        if end <= start:
            continue
        while end - start > max_seconds:
            close()
            windows.append(Window(((start, start + max_seconds),)))
            start += max_seconds
        length = end - start
        if used + length > max_seconds:
            close()
        current.append((start, end))
        used += length
    close()
    return windows
