"""Typed media errors, each carrying a message fit to show a user (§28)."""

from __future__ import annotations

__all__ = [
    "MediaError",
    "FFmpegNotFoundError",
    "FFmpegFailed",
    "VideoProbeError",
    "NoAudioTrackError",
    "MissingVideoFileError",
    "InsufficientDiskSpaceError",
    "Cancelled",
]


class MediaError(RuntimeError):
    """Base class. ``str(err)`` is always safe to display."""


class FFmpegNotFoundError(MediaError):
    def __init__(self) -> None:
        super().__init__(
            "FFmpeg could not be found.\n\n"
            "Lecture Saver normally uses the copy bundled with the "
            "'imageio-ffmpeg' package. Reinstall the dependencies with:\n"
            "    pip install -r requirements.txt\n"
            "or install FFmpeg system-wide and make sure it is on your PATH."
        )


class FFmpegFailed(MediaError):
    """FFmpeg ran but exited non-zero. ``details`` holds its stderr tail."""

    def __init__(self, message: str, details: str = "") -> None:
        super().__init__(message)
        self.details = details


class VideoProbeError(MediaError):
    pass


class NoAudioTrackError(MediaError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"'{name}' has no audio track, so it cannot be transcribed.\n"
            "Please choose a recording that contains the lecturer's audio."
        )


class MissingVideoFileError(MediaError):
    def __init__(self, path: str) -> None:
        super().__init__(
            f"The source video is no longer at:\n{path}\n\n"
            "It may have been moved, renamed, or be on a drive that is not connected."
        )
        self.path = path


class InsufficientDiskSpaceError(MediaError):
    def __init__(self, needed_bytes: int, free_bytes: int) -> None:
        super().__init__(
            f"Not enough free disk space.\n"
            f"About {needed_bytes / 1e9:.1f} GB is needed but only "
            f"{free_bytes / 1e9:.1f} GB is available."
        )


class Cancelled(MediaError):
    """The user cancelled a long-running operation. Not a failure."""

    def __init__(self, message: str = "Cancelled.") -> None:
        super().__init__(message)
