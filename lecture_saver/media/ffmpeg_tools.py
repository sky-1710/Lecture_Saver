"""Local FFmpeg: discovery, probing, and audio extraction.

Everything here shells out to a local FFmpeg binary. Nothing is uploaded.

Two details are Windows-specific and both matter in practice:

* ``CREATE_NO_WINDOW`` - without it a console window flashes on screen for
  every single FFmpeg invocation.
* ``ffprobe`` may not exist. The ``imageio-ffmpeg`` wheel ships ``ffmpeg.exe``
  only, so :func:`probe_video` falls back to parsing the banner that
  ``ffmpeg -i`` prints on stderr. That parser is a pure function
  (:func:`parse_ffmpeg_banner`) so it can be tested against recorded output
  without running a binary at all.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.models import VideoInfo
from .errors import (
    Cancelled,
    FFmpegFailed,
    FFmpegNotFoundError,
    InsufficientDiskSpaceError,
    MissingVideoFileError,
    VideoProbeError,
)

__all__ = [
    "find_ffmpeg",
    "find_ffprobe",
    "ffmpeg_available",
    "probe_video",
    "parse_ffmpeg_banner",
    "extract_audio",
    "estimated_wav_bytes",
    "SUPPORTED_VIDEO_SUFFIXES",
    "VIDEO_FILE_FILTER",
    "is_supported_video",
    "stream_gray_frames",
    "extract_frame",
]

#: Containers we advertise in the file dialog. FFmpeg reads far more than this;
#: these are the ones lecture recordings actually arrive in (§4).
SUPPORTED_VIDEO_SUFFIXES = (
    ".mp4", ".mov", ".mkv", ".avi", ".m4v", ".webm", ".wmv", ".flv", ".mpg", ".mpeg", ".ts",
)

VIDEO_FILE_FILTER = (
    "Lecture video ("
    + " ".join(f"*{s}" for s in SUPPORTED_VIDEO_SUFFIXES)
    + ");;All files (*)"
)

def is_supported_video(path: Path) -> bool:
    """Whether ``path`` looks like a lecture video we offer to import.

    Extension-based on purpose: this decides what a drag-and-drop or a file
    dialog accepts, and probing every dragged file would be far too slow.
    FFmpeg has the final say when the file is actually probed.
    """
    return path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES and path.is_file()


_AUDIO_SAMPLE_RATE = 16_000  # exactly Whisper's input rate - no resampling later
_AUDIO_CHANNELS = 1
_AUDIO_BYTES_PER_SAMPLE = 2  # pcm_s16le

ProgressCallback = Callable[[float], None]
CancelCheck = Callable[[], bool]


# --------------------------------------------------------------------------
# Binary discovery
# --------------------------------------------------------------------------

_ffmpeg_cache: Path | None = None
_ffprobe_cache: Path | None = None
_probe_searched = False


def _no_window() -> dict[str, Any]:
    """Keep FFmpeg from flashing a console window on Windows."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return {"creationflags": flags} if flags else {}


def find_ffmpeg() -> Path:
    """Locate ffmpeg: system PATH first, then the ``imageio-ffmpeg`` binary.

    A system FFmpeg is preferred because it is usually a fuller build; the
    bundled one exists so that the application works with no manual setup.
    """
    global _ffmpeg_cache
    if _ffmpeg_cache is not None:
        return _ffmpeg_cache

    which = shutil.which("ffmpeg")
    if which:
        _ffmpeg_cache = Path(which)
        return _ffmpeg_cache

    try:
        import imageio_ffmpeg
    except ImportError as exc:
        raise FFmpegNotFoundError() from exc

    try:
        bundled = Path(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # imageio raises a bare RuntimeError when absent
        raise FFmpegNotFoundError() from exc

    if not bundled.is_file():
        raise FFmpegNotFoundError()

    _ffmpeg_cache = bundled
    return _ffmpeg_cache


def find_ffprobe() -> Path | None:
    """Locate ffprobe, or ``None``. Absence is normal and handled."""
    global _ffprobe_cache, _probe_searched
    if _probe_searched:
        return _ffprobe_cache
    _probe_searched = True

    which = shutil.which("ffprobe")
    if which:
        _ffprobe_cache = Path(which)
        return _ffprobe_cache

    # A sibling of ffmpeg, when ffmpeg came from a full distribution.
    try:
        sibling = find_ffmpeg().with_name("ffprobe.exe")
    except FFmpegNotFoundError:
        return None
    if sibling.is_file():
        _ffprobe_cache = sibling
    return _ffprobe_cache


def ffmpeg_available() -> bool:
    try:
        find_ffmpeg()
    except FFmpegNotFoundError:
        return False
    return True


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------

_RE_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_RE_INPUT = re.compile(r"Input #\d+,\s*(.*?),\s*from\s+'")
_RE_STREAM_VIDEO = re.compile(r"Stream #\d+:\d+.*?:\s*Video:\s*([A-Za-z0-9_\-]+)")
_RE_STREAM_AUDIO = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:\s*([A-Za-z0-9_\-]+)")
_RE_RESOLUTION = re.compile(r"(?<![\d.])(\d{2,5})x(\d{2,5})(?![\d.])")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


def parse_ffmpeg_banner(text: str) -> dict[str, Any]:
    """Extract stream metadata from the banner ``ffmpeg -i`` writes to stderr.

    Pure function over text so it is testable without FFmpeg installed.
    Missing fields simply stay at their defaults rather than raising - a video
    with an odd banner should still import and play.
    """
    info: dict[str, Any] = {
        "duration": 0.0,
        "width": 0,
        "height": 0,
        "fps": 0.0,
        "container": "",
        "video_codec": "",
        "audio_codec": "",
        "has_audio": False,
    }

    match = _RE_INPUT.search(text)
    if match:
        info["container"] = match.group(1).strip()

    match = _RE_DURATION.search(text)
    if match:
        hours, minutes, seconds = match.groups()
        info["duration"] = int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("Stream #"):
            continue

        video = _RE_STREAM_VIDEO.search(stripped)
        if video and not info["video_codec"]:
            info["video_codec"] = video.group(1)
            # Resolution appears before any [SAR .. DAR ..] block, and the
            # negative lookarounds keep DAR ratios out of the match.
            head = stripped.split("[SAR")[0]
            res = _RE_RESOLUTION.search(head)
            if res:
                info["width"] = int(res.group(1))
                info["height"] = int(res.group(2))
            fps = _RE_FPS.search(stripped)
            if fps:
                info["fps"] = float(fps.group(1))
            continue

        audio = _RE_STREAM_AUDIO.search(stripped)
        if audio:
            info["has_audio"] = True
            if not info["audio_codec"]:
                info["audio_codec"] = audio.group(1)

    return info


def _probe_with_ffprobe(ffprobe: Path, video_path: Path) -> dict[str, Any] | None:
    cmd = [
        str(ffprobe), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, **_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    fmt = data.get("format", {})
    info: dict[str, Any] = {
        "duration": float(fmt.get("duration", 0.0) or 0.0),
        "container": str(fmt.get("format_name", "")),
        "width": 0, "height": 0, "fps": 0.0,
        "video_codec": "", "audio_codec": "", "has_audio": False,
    }

    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and not info["video_codec"]:
            info["video_codec"] = str(stream.get("codec_name", ""))
            info["width"] = int(stream.get("width", 0) or 0)
            info["height"] = int(stream.get("height", 0) or 0)
            info["fps"] = _parse_rational(stream.get("avg_frame_rate")) or _parse_rational(
                stream.get("r_frame_rate")
            )
            if not info["duration"]:
                info["duration"] = float(stream.get("duration", 0.0) or 0.0)
        elif kind == "audio":
            info["has_audio"] = True
            if not info["audio_codec"]:
                info["audio_codec"] = str(stream.get("codec_name", ""))

    return info


def _parse_rational(value: Any) -> float:
    """``"30000/1001"`` -> ``29.97``. Returns 0.0 for ``"0/0"`` and junk."""
    if not value or not isinstance(value, str) or "/" not in value:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    numerator, _, denominator = value.partition("/")
    try:
        den = float(denominator)
        return float(numerator) / den if den else 0.0
    except ValueError:
        return 0.0


def probe_video(video_path: Path) -> VideoInfo:
    """Read metadata for ``video_path``.

    Prefers ffprobe; falls back to parsing ``ffmpeg -i``. Raises
    :class:`VideoProbeError` if the file cannot be understood at all.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise MissingVideoFileError(str(video_path))

    ffmpeg = find_ffmpeg()  # raises FFmpegNotFoundError with a readable message

    info: dict[str, Any] | None = None
    ffprobe = find_ffprobe()
    if ffprobe is not None:
        info = _probe_with_ffprobe(ffprobe, video_path)

    banner = ""
    if info is None:
        # `ffmpeg -i` with no output file always exits 1 after printing the
        # banner. That is the expected path here, not an error.
        try:
            result = subprocess.run(
                [str(ffmpeg), "-hide_banner", "-i", str(video_path)],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120, **_no_window(),
            )
        except OSError as exc:
            raise VideoProbeError(f"Could not run FFmpeg:\n{exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise VideoProbeError(
                "FFmpeg timed out while reading this video. The file may be "
                "corrupted or on a very slow drive."
            ) from exc
        banner = result.stderr or ""
        info = parse_ffmpeg_banner(banner)

    if not info.get("video_codec") and not info.get("has_audio"):
        tail = "\n".join(banner.strip().splitlines()[-6:])
        raise VideoProbeError(
            f"'{video_path.name}' could not be read as a video file.\n"
            "It may be corrupted or in an unsupported format."
            + (f"\n\nFFmpeg said:\n{tail}" if tail else "")
        )

    return VideoInfo(
        path=str(video_path),
        duration=float(info.get("duration", 0.0)),
        width=int(info.get("width", 0)),
        height=int(info.get("height", 0)),
        fps=float(info.get("fps", 0.0)),
        size_bytes=video_path.stat().st_size,
        container=str(info.get("container", "")),
        video_codec=str(info.get("video_codec", "")),
        audio_codec=str(info.get("audio_codec", "")),
        has_audio=bool(info.get("has_audio", False)),
    )


# --------------------------------------------------------------------------
# Audio extraction
# --------------------------------------------------------------------------


def estimated_wav_bytes(duration_seconds: float) -> int:
    """Size of the extracted WAV: ~115 MB per hour."""
    return int(
        max(0.0, duration_seconds)
        * _AUDIO_SAMPLE_RATE
        * _AUDIO_CHANNELS
        * _AUDIO_BYTES_PER_SAMPLE
    ) + 44


_RE_OUT_TIME_US = re.compile(r"^out_time_(?:ms|us)=(-?\d+)$")


def extract_audio(
    video_path: Path,
    output_wav: Path,
    *,
    duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> Path:
    """Extract 16 kHz mono PCM audio, reporting progress as a 0..1 fraction.

    Progress comes from ``-progress pipe:1``, which emits ``out_time_us`` as
    encoding advances - real progress rather than a spinner (§27).
    """
    video_path = Path(video_path)
    output_wav = Path(output_wav)

    if not video_path.is_file():
        raise MissingVideoFileError(str(video_path))

    ffmpeg = find_ffmpeg()
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    needed = estimated_wav_bytes(duration) if duration > 0 else 0
    if needed:
        free = shutil.disk_usage(output_wav.parent).free
        # A little headroom so we do not fill the volume completely.
        if free < needed + 200_000_000:
            raise InsufficientDiskSpaceError(needed, free)

    # Write to a temporary name so a cancelled run never leaves a truncated
    # WAV that a later run would mistake for a finished extraction.
    partial = output_wav.with_suffix(".partial.wav")
    partial.unlink(missing_ok=True)

    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-y",
        "-i", str(video_path),
        "-vn",                      # drop video
        "-map", "0:a:0",            # first audio track only
        "-ac", str(_AUDIO_CHANNELS),
        "-ar", str(_AUDIO_SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        "-nostats",
        "-progress", "pipe:1",
        str(partial),
    ]

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **_no_window(),
        )
    except OSError as exc:
        raise FFmpegFailed(f"Could not start FFmpeg:\n{exc}") from exc

    # Drain stderr on a thread; otherwise a full pipe buffer would deadlock the
    # process once FFmpeg has logged enough.
    stderr_tail: deque[str] = deque(maxlen=40)

    def _drain() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_tail.append(line.rstrip())

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    cancelled = False
    try:
        assert process.stdout is not None
        for line in process.stdout:
            if should_cancel is not None and should_cancel():
                cancelled = True
                process.terminate()
                break

            line = line.strip()
            match = _RE_OUT_TIME_US.match(line)
            if match and duration > 0 and on_progress is not None:
                # Both out_time_ms and out_time_us are reported in microseconds
                # by FFmpeg, despite the older key's name.
                seconds = int(match.group(1)) / 1_000_000
                on_progress(max(0.0, min(1.0, seconds / duration)))
    finally:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        drainer.join(timeout=5)

    if cancelled:
        partial.unlink(missing_ok=True)
        raise Cancelled("Audio extraction cancelled.")

    if process.returncode != 0 or not partial.is_file() or partial.stat().st_size <= 44:
        partial.unlink(missing_ok=True)
        details = "\n".join(stderr_tail)
        raise FFmpegFailed(
            f"Audio could not be extracted from '{video_path.name}'.\n"
            "The file may be corrupted, or its audio track may be unreadable.",
            details,
        )

    partial.replace(output_wav)
    if on_progress is not None:
        on_progress(1.0)
    return output_wav


# --------------------------------------------------------------------------
# Frame sampling (Phase 3 slide detection)
# --------------------------------------------------------------------------

FrameCallback = Callable[[int, float, bytes], None]


def stream_gray_frames(
    video_path: Path,
    *,
    width: int,
    height: int,
    fps: float,
    on_frame: FrameCallback,
    duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Decode ``video_path`` once, yielding small grayscale frames at ``fps``.

    One long-running FFmpeg process does the seeking, decoding, scaling and
    grayscale conversion, and writes raw bytes to a pipe. That matters for long
    lectures: the alternative - seeking per frame with a capture library - is
    far slower, and decoding every frame would be pointless when slides are on
    screen for tens of seconds (§7, §26).

    ``on_frame`` receives ``(index, timestamp_seconds, raw_bytes)`` where
    ``raw_bytes`` is exactly ``width * height`` bytes of 8-bit grayscale. The
    buffer is reused between calls, so a callback that needs to keep the data
    must copy it.

    Returns the number of frames delivered.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise MissingVideoFileError(str(video_path))
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("width, height and fps must all be positive")

    ffmpeg = find_ffmpeg()
    frame_bytes = width * height

    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error",
        "-i", str(video_path),
        "-an", "-sn", "-dn",                     # video only
        "-vf", f"fps={fps},scale={width}:{height}:flags=area,format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray",
        "pipe:1",
    ]

    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **_no_window()
        )
    except OSError as exc:
        raise FFmpegFailed(f"Could not start FFmpeg:\n{exc}") from exc

    stderr_tail: deque[str] = deque(maxlen=40)

    def _drain() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            stderr_tail.append(line.decode("utf-8", "replace").rstrip())

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    buffer = bytearray(frame_bytes)
    view = memoryview(buffer)
    index = 0
    cancelled = False

    try:
        assert process.stdout is not None
        stdout = process.stdout
        while True:
            if should_cancel is not None and should_cancel():
                cancelled = True
                process.terminate()
                break

            filled = 0
            while filled < frame_bytes:
                read = stdout.readinto(view[filled:])  # type: ignore[attr-defined]
                if not read:
                    break
                filled += read
            if filled < frame_bytes:
                break  # end of stream (a trailing partial frame is discarded)

            timestamp = index / fps
            on_frame(index, timestamp, bytes(buffer))
            index += 1

            if on_progress is not None and duration > 0:
                on_progress(max(0.0, min(1.0, timestamp / duration)))
    finally:
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        drainer.join(timeout=5)

    if cancelled:
        raise Cancelled("Slide detection cancelled.")

    if index == 0:
        raise FFmpegFailed(
            f"No video frames could be read from '{video_path.name}'.\n"
            "The file may have no video track, or it may be corrupted.",
            "\n".join(stderr_tail),
        )

    if on_progress is not None:
        on_progress(1.0)
    return index


def extract_frame(
    video_path: Path,
    timestamp: float,
    output_path: Path,
    *,
    max_width: int = 1280,
    quality: int = 3,
) -> Path:
    """Save a single JPEG of the frame at ``timestamp``.

    ``-ss`` is placed before ``-i`` so FFmpeg seeks rather than decoding from
    the start - the difference between instant and minutes on a long lecture.
    Modern FFmpeg still lands on the exact frame, decoding forward from the
    preceding keyframe.
    """
    video_path = Path(video_path)
    if not video_path.is_file():
        raise MissingVideoFileError(str(video_path))

    ffmpeg = find_ffmpeg()
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    filters = []
    if max_width > 0:
        # Never upscale, and keep height even so the encoder stays happy.
        filters.append(f"scale='min({max_width},iw)':-2")

    cmd = [
        str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(video_path),
        "-frames:v", "1",
        *(["-vf", ",".join(filters)] if filters else []),
        "-q:v", str(quality),
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=120, **_no_window(),
        )
    except OSError as exc:
        raise FFmpegFailed(f"Could not start FFmpeg:\n{exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegFailed("FFmpeg timed out while capturing a slide image.") from exc

    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise FFmpegFailed(
            f"Could not capture a slide image at {timestamp:.1f}s.",
            (result.stderr or "").strip()[-2000:],
        )
    return output_path
