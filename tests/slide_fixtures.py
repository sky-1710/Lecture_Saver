"""Synthetic frames for slide-detection tests.

Frames are built at the ink resolution (320x180) - the same thing the real
pipeline gets from FFmpeg - and turned into a genuine
:class:`~lecture_saver.slides.descriptors.FrameDescriptors`, so the detector
under test is the production one with no video decoding involved.

The distractor helpers are sized as they appear on a real 1280x720 recording
once scaled down: a mouse cursor is a few pixels, a laser dot barely more, a
highlighted line one text row, a pen stroke a few pixels thick.
"""

from __future__ import annotations

import numpy as np

from lecture_saver.core.slide_options import SlideDetectionOptions
from lecture_saver.slides.descriptors import (
    FrameDescriptors,
    box_reduce,
    dhash_from_thumbnail,
    ink_mask,
)

WIDTH = 320
HEIGHT = 180


def options(**overrides) -> SlideDetectionOptions:
    """Default options with dotted overrides, e.g. ``timing.min_slide_seconds``."""
    built = SlideDetectionOptions()
    for key, value in overrides.items():
        group, _, field_name = key.partition(".")
        if field_name:
            setattr(getattr(built, group), field_name, value)
        else:  # a bare name: find whichever group owns it
            for name in ("sampling", "normal_transition", "strong_global_transition",
                         "annotation_filter", "annotation_snapshots", "busy_region_mask",
                         "timing", "image"):
                target = getattr(built, name)
                if hasattr(target, key):
                    setattr(target, key, value)
                    break
            else:
                raise AttributeError(f"no such detection option: {key}")
    return built


def blank(value: int = 240) -> np.ndarray:
    """An empty slide: a light background, like projected white."""
    return np.full((HEIGHT, WIDTH), value, dtype=np.uint8)


def text_line(frame: np.ndarray, row: int, seed: int, *, left: int = 24) -> None:
    """Draw one line of word-shaped marks in place.

    Text is drawn two pixels tall because that is what body text becomes at the
    ink resolution. The height matters: the detector separates handwriting from
    edited text by whether the changed region survives an erosion, and a
    fixture that drew solid blocks would look like a pen stroke rather than
    like text.
    """
    rng = np.random.default_rng(seed)
    x = left
    while x < WIDTH - 40:
        word = int(rng.integers(10, 34))
        if x + word > WIDTH - 24:
            break
        frame[row : row + 2, x : x + word] = int(rng.integers(20, 70))
        x += word + int(rng.integers(5, 11))


def slide(seed: int, *, lines: int = 6) -> np.ndarray:
    """A distinct slide-like frame: lines of text on a light background."""
    frame = blank()
    for index in range(lines):
        text_line(frame, 30 + index * 22, seed * 100 + index)
    return frame


def bold_slide(seed: int, *, lines: int = 6) -> np.ndarray:
    """A slide of large, bold lettering: headings, pictures, double-line shapes.

    Drawn four pixels tall, which is what a big bold heading becomes at the ink
    resolution. Unlike :func:`slide` its strokes survive an erosion, so a change
    to it is pen-shaped - the case that was absorbed as annotation on a real
    lecture.
    """
    frame = blank()
    rng = np.random.default_rng(seed)
    # Different slides put their lines in different places; identical rows
    # would make two unrelated slides overlap far more than real ones do.
    offset = int(rng.integers(0, 12))
    for index in range(lines):
        row = 20 + offset + index * 24
        x = 24
        while x < WIDTH - 40:
            word = int(rng.integers(12, 40))
            if x + word > WIDTH - 24:
                break
            frame[row : row + 4, x : x + word] = int(rng.integers(20, 70))
            x += word + int(rng.integers(6, 14))
    return frame


def titled_slide() -> np.ndarray:
    """A slide with a title bar and an otherwise empty body."""
    frame = blank()
    frame[12:36, 24:240] = 40
    return frame


def with_bullets(frame: np.ndarray, count: int, seed: int = 5) -> np.ndarray:
    """``count`` bullet lines revealed in the empty body of a titled slide."""
    out = frame.copy()
    for step in range(count):
        text_line(out, 56 + step * 24, seed * 100 + step, left=32)
    return out


# --------------------------------------------------------------------------
# Things that must NOT create a slide
# --------------------------------------------------------------------------


def with_cursor(frame: np.ndarray, x: int = 240, y: int = 120) -> np.ndarray:
    """A mouse pointer: a small dark mark."""
    out = frame.copy()
    out[y : y + 6, x : x + 4] = 0
    return out


def with_laser(frame: np.ndarray, x: int = 80, y: int = 48) -> np.ndarray:
    """A laser dot: a small bright spot."""
    out = frame.copy()
    out[y : y + 7, x : x + 7] = 255
    return out


def with_highlight(frame: np.ndarray, row: int = 80) -> np.ndarray:
    """One line highlighted with a translucent marker: the text stays put."""
    out = frame.copy()
    band = out[row : row + 12, 32:176]
    band[band > 200] = 170
    return out


def with_pen(frame: np.ndarray, strokes: int = 1, *, thickness: int = 3) -> np.ndarray:
    """Progressive handwriting: solid strokes drawn over the slide.

    This is the case that failed on a real lecture. The strokes are opaque and
    several pixels thick, so they cover existing text - which is exactly why
    magnitude and content-replacement tests alone cannot reject them.

    ``thickness`` is in analysis pixels: 3 here corresponds to a ~12 px pen on
    a 1280x720 recording. Drawing it much fatter than that makes the fixture
    unrepresentative, because *erasing* an implausibly thick stroke reads as
    thick too.
    """
    out = frame.copy()
    for step in range(strokes):
        row = 40 + step * 22
        for x in range(24, 280):
            y = row + int(6 * np.sin(x / 9.0))
            out[y : y + thickness, x : x + thickness] = 15
    return out


def with_webcam(frame: np.ndarray, tick: int) -> np.ndarray:
    """A talking-head overlay in the corner that changes every frame."""
    out = frame.copy()
    rng = np.random.default_rng(10_000 + tick)
    out[HEIGHT - 44 : HEIGHT - 4, WIDTH - 68 : WIDTH - 4] = rng.integers(
        0, 255, size=(40, 64), dtype=np.uint8
    )
    return out


def blend(frame_a: np.ndarray, frame_b: np.ndarray, alpha: float) -> np.ndarray:
    """A crossfade frame, ``alpha`` of the way from A to B."""
    mixed = frame_a.astype(np.float64) * (1 - alpha) + frame_b.astype(np.float64) * alpha
    return mixed.round().clip(0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# Turning frames into descriptors
# --------------------------------------------------------------------------


def hold(frame: np.ndarray, seconds: float, sample_fps: float = 1.0) -> list[np.ndarray]:
    """``seconds`` worth of an unchanging slide."""
    return [frame.copy() for _ in range(max(1, int(round(seconds * sample_fps))))]


def descriptors(
    frames: list[np.ndarray],
    opts: SlideDetectionOptions | None = None,
    sample_fps: float = 1.0,
) -> FrameDescriptors:
    """Build the real descriptor object the detector consumes."""
    opts = opts or options()
    scale = max(1, WIDTH // opts.sampling.thumb_width)
    packed_bytes = (HEIGHT * WIDTH + 7) // 8

    if not frames:
        return FrameDescriptors(
            thumbnails=np.zeros(
                (0, opts.sampling.thumb_height, opts.sampling.thumb_width), np.uint8
            ),
            times=np.zeros(0, np.float64),
            hashes=np.zeros(0, np.uint64),
            ink=np.zeros((0, packed_bytes), np.uint8),
            ink_shape=(HEIGHT, WIDTH),
        )

    thumbnails = [box_reduce(f, scale) for f in frames]
    return FrameDescriptors(
        thumbnails=np.stack(thumbnails),
        times=np.arange(len(frames), dtype=np.float64) / sample_fps,
        hashes=np.asarray([dhash_from_thumbnail(t) for t in thumbnails], dtype=np.uint64),
        ink=np.stack(
            [np.packbits(ink_mask(f, opts.sampling.ink_tolerance).ravel()) for f in frames]
        ),
        ink_shape=(HEIGHT, WIDTH),
    )


def add_full_screen_change(frame: np.ndarray) -> np.ndarray:
    """A different slide entirely: inverted theme and a different layout.

    Used for the strong-global transition path, which must fire on an obvious
    whole-screen change even when the local evidence is weak.
    """
    out = np.full((HEIGHT, WIDTH), 30, dtype=np.uint8)
    out[12:36, 24:260] = 200
    for index in range(4):
        text_line(out, 60 + index * 26, 4242 + index, left=40)
    out[out < 40] = 30
    return out


def embedded_video(frame: np.ndarray, tick: int) -> np.ndarray:
    """A video playing inside an otherwise unchanged slide.

    Sized to about a sixth of the frame, which is what a video embedded in a
    slide actually looks like. Filling half the screen would push the busy mask
    into its own size cap, and the cap exists precisely so that a recording
    which is mostly moving cannot mask itself into silence.
    """
    out = frame.copy()
    rng = np.random.default_rng(500 + tick)
    out[66:114, 100:196] = rng.integers(
        20, 235, size=(48, 96), dtype=np.uint8
    )
    return out
