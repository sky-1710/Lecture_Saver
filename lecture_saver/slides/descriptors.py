"""Pass A: reduce a video to a sequence of compact frame descriptors.

The video is decoded exactly once. Each sampled frame becomes an 80x45
grayscale thumbnail plus a 64-bit dHash - about 3.6 KB per frame, so a
three-hour lecture at 2 fps is roughly 78 MB in memory and far less on disk
once compressed.

Keeping descriptors rather than raw frames is what makes detection re-runnable:
changing a threshold replays :mod:`~lecture_saver.slides.detector` over the
cached descriptors in milliseconds instead of decoding the video again (§10).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np

from ..core.slide_options import SlideDetectionOptions
from ..media.ffmpeg_tools import stream_gray_frames

log = logging.getLogger(__name__)

__all__ = [
    "FrameDescriptors",
    "compute_descriptors",
    "dhash_from_thumbnail",
    "hash_to_hex",
    "ink_mask",
    "box_reduce",
]

#: Bumped when the descriptor layout changes so stale caches are ignored.
CACHE_VERSION = 2

ProgressCallback = Callable[[float], None]
CancelCheck = Callable[[], bool]


def dhash_from_thumbnail(thumbnail: np.ndarray) -> np.uint64:
    """64-bit difference hash of one thumbnail.

    dHash compares horizontally adjacent pixels of a 9x8 reduction, so it
    encodes the *gradient* structure of the frame rather than its brightness.
    That makes it stable under exposure and compression changes while still
    reacting to a different slide layout.
    """
    small = _resize_mean(thumbnail.astype(np.float32), 9, 8)
    bits = small[:, 1:] > small[:, :-1]  # 8x8 booleans
    packed = np.uint64(0)
    for bit in bits.reshape(-1):
        packed = np.uint64(packed << np.uint64(1)) | np.uint64(1 if bit else 0)
    return packed


def hash_to_hex(value: np.uint64 | int) -> str:
    return f"{int(value):016x}"


def _resize_mean(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    """Area-average resize. Exact when the source divides evenly, else nearest.

    Only used for the small dHash reduction, where a bilinear resample would
    add nothing but a dependency.
    """
    source_h, source_w = frame.shape
    if source_h % height == 0 and source_w % width == 0:
        return frame.reshape(height, source_h // height, width, source_w // width).mean(axis=(1, 3))

    rows = (np.arange(height) * source_h // height).clip(0, source_h - 1)
    cols = (np.arange(width) * source_w // width).clip(0, source_w - 1)
    return frame[np.ix_(rows, cols)]


def box_reduce(frame: np.ndarray, factor: int) -> np.ndarray:
    """Average ``factor`` x ``factor`` blocks. Used to derive the thumbnail."""
    if factor <= 1:
        return frame
    height = (frame.shape[0] // factor) * factor
    width = (frame.shape[1] // factor) * factor
    block = frame[:height, :width].reshape(
        height // factor, factor, width // factor, factor
    )
    return block.mean(axis=(1, 3)).round().astype(np.uint8)


def ink_mask(frame: np.ndarray, tolerance: float) -> np.ndarray:
    """Boolean mask of where the frame carries content.

    The reference level is the frame's own median, so this works whether the
    slide is light or dark. Text, figures and pen strokes are all ink; blank
    areas and a uniform background are not.
    """
    values = frame.astype(np.float32)
    return np.abs(values - float(np.median(values))) > tolerance


class FrameDescriptors:
    """The descriptor sequence for one video.

    ``thumbnails`` is ``(n, h, w)`` uint8, used for magnitude measurements.
    ``ink`` holds the same frames as 1-bit masks at twice that resolution,
    bit-packed, for the shape measurements that separate annotation from a
    genuine slide change. ``times`` is ``(n,)`` seconds and ``hashes``
    ``(n,)`` uint64.

    Two resolutions are kept because they answer different questions: the
    thumbnail says *how much* changed, the ink mask says *what shape* the
    change is - and shape is the only thing that distinguishes a pen stroke
    from an edited line of text.
    """

    def __init__(
        self,
        thumbnails: np.ndarray,
        times: np.ndarray,
        hashes: np.ndarray,
        ink: np.ndarray | None = None,
        ink_shape: tuple[int, int] = (0, 0),
        *,
        sample_signature: str = "",
        source_key: str = "",
    ) -> None:
        self.thumbnails = thumbnails
        self.times = times
        self.hashes = hashes
        #: ``(n, packed_bytes)`` uint8, or an empty array when unavailable.
        self.ink = ink if ink is not None else np.zeros((len(times), 0), dtype=np.uint8)
        self.ink_shape = ink_shape
        self.sample_signature = sample_signature
        self.source_key = source_key

    @property
    def has_ink(self) -> bool:
        return self.ink.size > 0 and self.ink_shape[0] > 0

    def ink_at(self, index: int) -> np.ndarray:
        """Unpack one ink mask back to a boolean ``(h, w)`` array."""
        height, width = self.ink_shape
        bits = np.unpackbits(self.ink[index], count=height * width)
        return bits.reshape(height, width).astype(bool)

    def __len__(self) -> int:
        return int(self.thumbnails.shape[0])

    @property
    def is_empty(self) -> bool:
        return len(self) == 0

    @property
    def shape(self) -> tuple[int, int]:
        """``(height, width)`` of one thumbnail."""
        return int(self.thumbnails.shape[1]), int(self.thumbnails.shape[2])

    def matches(self, *, sample_signature: str, source_key: str) -> bool:
        """Whether this cache is still valid for the given video and sampling."""
        return (
            self.sample_signature == sample_signature
            and self.source_key == source_key
            and not self.is_empty
        )

    # -- persistence --------------------------------------------------------

    def save(self, path: Path) -> None:
        """Write the cache. Best-effort: a failure here must not fail detection."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp.npz")
        try:
            np.savez_compressed(
                temporary,
                version=np.array([CACHE_VERSION]),
                thumbnails=self.thumbnails,
                times=self.times,
                hashes=self.hashes,
                ink=self.ink,
                ink_shape=np.array(self.ink_shape),
                sample_signature=np.array([self.sample_signature]),
                source_key=np.array([self.source_key]),
            )
            temporary.replace(path)
        except (OSError, ValueError):
            log.warning("Could not write the frame descriptor cache", exc_info=True)
            temporary.unlink(missing_ok=True)

    @classmethod
    def load(cls, path: Path) -> FrameDescriptors | None:
        """Read a cache, or ``None`` if it is missing, stale or unreadable."""
        path = Path(path)
        if not path.is_file():
            return None
        try:
            with np.load(path, allow_pickle=False) as data:
                if int(data["version"][0]) != CACHE_VERSION:
                    return None
                shape = data["ink_shape"]
                return cls(
                    thumbnails=data["thumbnails"],
                    times=data["times"],
                    hashes=data["hashes"],
                    ink=data["ink"],
                    ink_shape=(int(shape[0]), int(shape[1])),
                    sample_signature=str(data["sample_signature"][0]),
                    source_key=str(data["source_key"][0]),
                )
        except (OSError, ValueError, KeyError, EOFError):
            log.warning("Ignoring an unreadable frame descriptor cache", exc_info=True)
            return None


def source_key_for(video_path: Path) -> str:
    """Identifies the exact video file a cache was built from.

    Size plus modification time is enough to notice that the source was
    replaced, and costs nothing compared to hashing gigabytes.
    """
    try:
        stat = Path(video_path).stat()
    except OSError:
        return ""
    return f"{stat.st_size}:{int(stat.st_mtime)}"


def compute_descriptors(
    video_path: Path,
    *,
    options: SlideDetectionOptions,
    duration: float = 0.0,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> FrameDescriptors:
    """Decode ``video_path`` once and build its descriptor sequence."""
    # Frames arrive at the ink resolution and the thumbnail is derived from
    # them, so the video is still decoded exactly once.
    sampling = options.sampling
    ink_w, ink_h = sampling.ink_width, sampling.ink_height
    scale = sampling.ink_scale
    thumb_h, thumb_w = ink_h // scale, ink_w // scale

    thumbnails: list[np.ndarray] = []
    inks: list[np.ndarray] = []
    times: list[float] = []
    hashes: list[np.uint64] = []

    def on_frame(index: int, timestamp: float, raw: bytes) -> None:
        # frombuffer is a view over the reused buffer; box_reduce and packbits
        # both allocate, so nothing aliases it afterwards.
        frame = np.frombuffer(raw, dtype=np.uint8).reshape(ink_h, ink_w)
        thumbnail = box_reduce(frame, scale)
        thumbnails.append(thumbnail)
        inks.append(np.packbits(ink_mask(frame, sampling.ink_tolerance).ravel()))
        times.append(timestamp)
        hashes.append(dhash_from_thumbnail(thumbnail))

    stream_gray_frames(
        video_path,
        width=ink_w,
        height=ink_h,
        fps=sampling.sample_fps,
        on_frame=on_frame,
        duration=duration,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )

    return FrameDescriptors(
        thumbnails=(
            np.stack(thumbnails) if thumbnails
            else np.zeros((0, thumb_h, thumb_w), dtype=np.uint8)
        ),
        times=np.asarray(times, dtype=np.float64),
        hashes=np.asarray(hashes, dtype=np.uint64),
        ink=(
            np.stack(inks) if inks
            else np.zeros((0, (ink_h * ink_w + 7) // 8), dtype=np.uint8)
        ),
        ink_shape=(ink_h, ink_w),
        sample_signature=options.sample_signature,
        source_key=source_key_for(video_path),
    )
