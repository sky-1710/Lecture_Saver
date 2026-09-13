"""Timestamp formatting and parsing.

Every timestamp shown in the UI or written to an export goes through this
module, so a rounding decision is made in exactly one place.
"""

from __future__ import annotations

import math

__all__ = [
    "format_hms",
    "format_clock",
    "format_srt",
    "format_vtt",
    "format_range",
    "format_duration",
    "parse_timestamp",
]


def _split(seconds: float) -> tuple[int, int, int, int]:
    """Split seconds into (h, m, s, ms), clamping negatives to zero.

    Milliseconds are rounded, and a carry out of 999.5 ms is propagated so that
    59.9999 becomes 00:01:00.000 rather than the invalid 00:00:60.000.
    """
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return total_s // 3600, (total_s % 3600) // 60, total_s % 60, ms


def format_hms(seconds: float) -> str:
    """``HH:MM:SS`` - the canonical display form (§13, §17)."""
    h, m, s, _ = _split(seconds)
    return f"{h:02d}:{m:02d}:{s:02d}"


def format_clock(seconds: float) -> str:
    """``MM:SS``, widening to ``H:MM:SS`` past the hour. For compact controls."""
    h, m, s, _ = _split(seconds)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_srt(seconds: float) -> str:
    """``HH:MM:SS,mmm`` - SubRip uses a comma before milliseconds."""
    h, m, s, ms = _split(seconds)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_vtt(seconds: float) -> str:
    """``HH:MM:SS.mmm`` - WebVTT uses a period."""
    h, m, s, ms = _split(seconds)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def format_range(start: float, end: float) -> str:
    """``HH:MM:SS - HH:MM:SS`` for slide and segment intervals."""
    return f"{format_hms(start)} - {format_hms(end)}"


def format_duration(seconds: float) -> str:
    """Human phrasing for estimates and metadata, e.g. ``1 h 12 min``."""
    if not math.isfinite(seconds) or seconds < 0:
        seconds = 0.0
    h, m, s, _ = _split(seconds)
    if h:
        return f"{h} h {m} min"
    if m:
        return f"{m} min {s} s"
    return f"{s} s"


def parse_timestamp(text: str) -> float:
    """Parse ``SS``/``MM:SS``/``HH:MM:SS`` with ``.mmm`` or ``,mmm`` fractions.

    Raises ``ValueError`` on anything else, so callers can report a bad input
    rather than silently seeking to zero.
    """
    raw = text.strip().replace(",", ".")
    if not raw:
        raise ValueError("empty timestamp")

    parts = raw.split(":")
    if len(parts) > 3:
        raise ValueError(f"too many ':' groups in timestamp: {text!r}")

    try:
        values = [float(p) for p in parts]
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {text!r}") from exc

    if any(v < 0 for v in values):
        raise ValueError(f"negative timestamp: {text!r}")
    # Only the last (seconds) group may carry a fraction.
    if any(v != int(v) for v in values[:-1]):
        raise ValueError(f"invalid timestamp: {text!r}")

    total = 0.0
    for value in values:
        total = total * 60 + value
    return total
