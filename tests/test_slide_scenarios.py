"""Real-world validation: the failure modes seen on an actual lecture.

Each scenario is rendered as a genuine 1280x720 H.264 clip with real text and
real pen strokes, then put through the whole pipeline. Synthetic arrays cannot
catch these: what broke on a real lecture was the *appearance* of downscaled
text and ink, not the logic over idealised shapes.

Deselected by default because rendering the clips takes about half a minute.
Run it explicitly:

    pytest -m scenario

The expectations encode what a person would say looking at the video:

    A  one slide, progressive handwriting                  -> 1 slide
    B  one slide, opaque strokes covering text             -> 1 slide
    C  cursor + laser + handwriting + webcam, then change  -> 2 slides
    D  same template, one text block differs               -> 2 slides
    E  same template, only a number differs                -> known limitation
    F  obvious full-screen change                          -> 2 slides
    G  crossfade between slides                            -> 2 slides
    H  fast navigation, six slides                         -> most kept
    I  full-screen embedded video                          -> 1 slide
    J  opaque annotation, then a similar-looking slide     -> 2 slides
    K  opaque annotation, then a full-screen change        -> 2 slides
"""

import math
import os
import subprocess

import numpy as np
import pytest

from lecture_saver.core.slide_options import SlideDetectionOptions
from lecture_saver.media.ffmpeg_tools import ffmpeg_available, find_ffmpeg
from lecture_saver.slides.descriptors import compute_descriptors
from lecture_saver.slides.detector import detect_appearances

pytestmark = [pytest.mark.scenario, pytest.mark.ffmpeg]

if not ffmpeg_available():  # pragma: no cover - environment dependent
    pytest.skip("FFmpeg is not available", allow_module_level=True)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtGui")

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

WIDTH, HEIGHT, FPS = 1280, 720, 5


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


def _render(title, bullets, background=(250, 250, 248), bar=(28, 42, 84)):
    image = QImage(WIDTH, HEIGHT, QImage.Format.Format_RGB888)
    image.fill(QColor(*background))
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    painter.fillRect(QRect(0, 0, WIDTH, 118), QColor(*bar))
    painter.setPen(QColor(255, 255, 255))
    painter.setFont(QFont("Segoe UI", 30, QFont.Weight.Bold))
    painter.drawText(QRect(60, 22, WIDTH - 120, 78), Qt.AlignmentFlag.AlignVCenter, title)
    painter.setPen(QColor(40, 40, 45))
    painter.setFont(QFont("Segoe UI", 22))
    for index, text in enumerate(bullets):
        painter.drawText(
            QRect(110, 190 + index * 74, WIDTH - 200, 58),
            Qt.AlignmentFlag.AlignVCenter,
            "•  " + text,
        )
    painter.end()
    return image


def _to_array(image):
    row = image.bytesPerLine()
    return (
        np.array(image.constBits())
        .reshape(HEIGHT, row)[:, : WIDTH * 3]
        .reshape(HEIGHT, WIDTH, 3)
        .copy()
    )


def _annotate(image, step, *, opaque):
    """Progressive handwriting: underline, circle, arrow, equation, scribble."""
    out = image.copy()
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    ink = QColor(20, 20, 20) if opaque else QColor(200, 30, 30)
    painter.setPen(
        QPen(ink, 13 if opaque else 4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
    )
    if step >= 1:
        painter.drawLine(112, 232, 700, 236)
    if step >= 2:
        painter.drawEllipse(QRect(100, 258, 640, 62))
    if step >= 3:
        painter.drawLine(760, 300, 900, 360)
        painter.drawLine(900, 360, 872, 348)
    if step >= 4:
        for k, x in enumerate(range(880, 1180, 30)):
            y = 470 + int(26 * math.sin(k * 1.3))
            painter.drawLine(x, y, x + 22, y + (18 if k % 2 else -18))
    if step >= 5:
        points = [
            QPoint(150 + i * 16, 500 + int(30 * math.sin(i * 0.9))) for i in range(34)
        ]
        for start, end in zip(points, points[1:], strict=False):
            painter.drawLine(start, end)
    painter.end()
    return out


def _webcam(frame, t):
    box = frame[HEIGHT - 190 : HEIGHT - 30, WIDTH - 250 : WIDTH - 40]
    box[:] = (70, 70, 80)
    cx = 105 + int(38 * math.sin(t * 1.7))
    cy = 80 + int(24 * math.cos(t * 1.1))
    yy, xx = np.ogrid[:160, :210]
    box[((xx - cx) ** 2 + (yy - cy) ** 2) < 45**2] = (205, 175, 150)
    return frame


def _laser(frame, t):
    x = 400 + int(300 * math.sin(t * 0.9))
    y = 400 + int(120 * math.cos(t * 1.3))
    frame[y : y + 10, x : x + 10] = (255, 40, 40)
    return frame


def _embedded_video(frame, t):
    """A video playing inside the slide: a big moving region, slide unchanged."""
    box = frame[200:620, 300:1000]
    rng = np.random.default_rng(int(t * 5))
    box[:] = (30, 30, 40)
    cx = 350 + int(220 * math.sin(t * 0.8))
    cy = 210 + int(120 * math.cos(t * 1.1))
    yy, xx = np.ogrid[:420, :700]
    box[((xx - cx) ** 2 + (yy - cy) ** 2) < 90**2] = rng.integers(60, 240, 3)
    return frame


def _cursor(frame, t):
    x = 200 + int(600 * abs(((t * 0.35) % 2) - 1))
    y = 300 + int(280 * abs(((t * 0.21) % 2) - 1))
    frame[y : y + 18, x : x + 12] = 0
    return frame


def _write_clip(path, frame_for, seconds):
    process = subprocess.Popen(
        [
            str(find_ffmpeg()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", str(FPS), "-i", "pipe:0",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(path),
        ],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None
    for n in range(int(seconds * FPS)):
        process.stdin.write(frame_for(n / FPS).tobytes())
    process.stdin.close()
    process.wait()
    return path


@pytest.fixture(scope="module")
def clips(qt_app, tmp_path_factory):
    directory = tmp_path_factory.mktemp("scenarios")

    s1 = _render("Gradient Descent", ["Follow the negative gradient",
                                      "Choose a learning rate",
                                      "Batch, mini-batch, stochastic",
                                      "Convergence criteria"])
    s2 = _render("Gradient Descent", ["Follow the negative gradient",
                                      "Choose a learning rate",
                                      "Momentum smooths the path",
                                      "Convergence criteria"])
    s3 = _render("Regularisation", ["L1 and L2 penalties", "Dropout during training",
                                    "Early stopping", "Data augmentation"])
    s4 = _render("Learning Rate", ["Typical value 0.001", "Too large diverges",
                                   "Too small crawls"])
    s5 = _render("Learning Rate", ["Typical value 0.010", "Too large diverges",
                                   "Too small crawls"])
    # A different theme and layout: the unmistakable whole-screen change.
    s6 = _render("Results", ["Accuracy 94.2 %", "Precision 0.91", "Recall 0.88",
                             "F1 0.895"],
                 background=(18, 20, 26), bar=(120, 30, 40))

    thin = [_to_array(_annotate(s1, step, opaque=False)) for step in range(6)]
    opaque = [_to_array(_annotate(s1, step, opaque=True)) for step in range(6)]
    a1, a2, a3, a4, a5, a6 = (_to_array(s) for s in (s1, s2, s3, s4, s5, s6))

    def fade(t, start, span, before, after):
        alpha = (t - start) / span
        return (before * (1 - alpha) + after * alpha).astype(np.uint8)

    paths = {}
    paths["A"] = _write_clip(directory / "A.mp4",
                             lambda t: thin[min(int(t // 10), 5)].copy(), 60)
    paths["B"] = _write_clip(directory / "B.mp4",
                             lambda t: opaque[min(int(t // 10), 5)].copy(), 60)
    paths["C"] = _write_clip(
        directory / "C.mp4",
        lambda t: _laser(_cursor(_webcam(
            (thin[min(int(t // 12), 3)] if t < 48 else a3).copy(), t), t), t), 76)
    paths["D"] = _write_clip(directory / "D.mp4",
                             lambda t: (a1 if t < 20 else a2).copy(), 40)
    paths["E"] = _write_clip(directory / "E.mp4",
                             lambda t: (a4 if t < 20 else a5).copy(), 40)
    paths["F"] = _write_clip(directory / "F.mp4",
                             lambda t: (a1 if t < 20 else a6).copy(), 40)
    paths["G"] = _write_clip(
        directory / "G.mp4",
        lambda t: (a1 if t < 20 else
                   (a3 if t > 22 else fade(t, 20, 2, a1, a3))).copy(), 44)
    decks = [a1, a3, a4, a2, a6, a3]
    paths["H"] = _write_clip(directory / "H.mp4",
                             lambda t: decks[min(int(t // 7), 5)].copy(), 42)
    paths["I"] = _write_clip(directory / "I.mp4",
                             lambda t: _embedded_video(a1.copy(), t), 56)
    paths["J"] = _write_clip(directory / "J.mp4",
                             lambda t: (opaque[min(int(t // 10), 4)]
                                        if t < 50 else a3).copy(), 76)
    paths["K"] = _write_clip(directory / "K.mp4",
                             lambda t: (opaque[min(int(t // 10), 4)]
                                        if t < 50 else a6).copy(), 76)
    return paths


def run(path, options=None):
    options = options or SlideDetectionOptions()
    descriptors = compute_descriptors(path, options=options)
    duration = float(descriptors.times[-1]) + 1
    return detect_appearances(
        descriptors.thumbnails, descriptors.times, descriptors.hashes,
        duration=duration, options=options, ink=descriptors,
    )


class TestAnnotationStaysOneSlide:
    """Problem A: annotated slides were being saved several times over."""

    def test_a_progressive_handwriting_is_one_slide(self, clips):
        assert len(run(clips["A"]).appearances) == 1

    def test_b_opaque_strokes_covering_text_are_one_slide(self, clips):
        assert len(run(clips["B"]).appearances) == 1

    def test_b_the_marks_are_recognised_rather_than_unnoticed(self, clips):
        result = run(clips["B"])
        assert result.absorbed, "the strokes should be seen and explained"
        assert "annotation (thick strokes)" in result.rejection_summary()

    def test_c_cursor_laser_handwriting_and_webcam_stay_one_slide(self, clips):
        """Every distractor at once, then one genuine change at 48 s."""
        result = run(clips["C"])
        assert len(result.appearances) == 2
        assert result.appearances[1].start == pytest.approx(48.0, abs=3.0)

    def test_i_a_full_screen_embedded_video_makes_no_slides(self, clips):
        assert len(run(clips["I"]).appearances) == 1

    def test_i_the_video_region_is_masked_and_reported(self, clips):
        result = run(clips["I"])
        assert result.busy.masked_cells > 0
        assert result.busy.describe()


class TestGenuineChangesAreFound:
    """Problem B: real slide changes were being missed."""

    def test_d_one_changed_text_block_is_two_slides(self, clips):
        result = run(clips["D"])
        assert len(result.appearances) == 2
        assert result.appearances[1].start == pytest.approx(20.0, abs=2.0)

    def test_f_an_obvious_full_screen_change_is_always_found(self, clips):
        result = run(clips["F"])
        assert len(result.appearances) == 2
        assert result.appearances[1].start == pytest.approx(20.0, abs=2.0)

    def test_f_it_is_the_strong_global_path_that_finds_it(self, clips):
        committed = run(clips["F"]).committed
        assert [c.path for c in committed] == ["strong global"]

    def test_f_it_survives_an_impossible_local_threshold(self, clips):
        """The override must not be reachable only by luck."""
        options = SlideDetectionOptions()
        options.normal_transition.min_content_replaced = 0.999
        options.normal_transition.min_ink_change = 0.9
        assert len(run(clips["F"], options).appearances) == 2

    def test_g_a_crossfade_produces_one_transition(self, clips):
        result = run(clips["G"])
        assert len(result.appearances) == 2
        # Committed after the picture settles, not during the fade.
        assert result.appearances[1].start >= 20.5

    def test_h_most_of_a_rapid_sequence_is_kept(self, clips):
        assert len(run(clips["H"]).appearances) >= 5

    def test_j_leaving_an_annotated_slide_is_detected(self, clips):
        """Erasing thick strokes must not look like drawing them."""
        result = run(clips["J"])
        assert len(result.appearances) == 2
        assert result.appearances[1].start == pytest.approx(50.0, abs=3.0)

    def test_k_leaving_annotation_for_a_full_screen_change(self, clips):
        result = run(clips["K"])
        assert len(result.appearances) == 2
        assert result.committed[0].path == "strong global"


class TestKnownLimitations:
    """Cases no descriptor at this resolution can resolve, kept honest."""

    def test_e_a_number_only_change_is_not_detected(self, clips):
        """"0.001" -> "0.010" is a handful of pixels once the frame is scaled
        down for analysis; the two frames are identical as ink masks.
        Correcting this is what "Add slide here" is for."""
        assert len(run(clips["E"]).appearances) == 1

    def test_h_documents_the_additive_limitation(self, clips):
        """A slide that only *adds* text is structurally identical to a build
        animation, so one of H's transitions is absorbed by design."""
        result = run(clips["H"])
        assert len(result.appearances) <= 6
        overlay = {"addition into empty space", "annotation (content preserved)"}
        assert overlay & set(result.rejection_summary())


class TestDiagnosticsOnRealVideo:
    def test_every_absorbed_candidate_names_its_reason(self, clips):
        for candidate in run(clips["B"]).absorbed:
            assert candidate.reason
            assert candidate.gates

    def test_committed_candidates_record_path_and_confidence(self, clips):
        for candidate in run(clips["F"]).committed:
            assert candidate.path in {"strong global", "normal"}
            assert candidate.confidence is not None

    def test_near_misses_are_ranked(self, clips):
        near = run(clips["B"]).strongest_rejected(limit=5)
        assert near
        assert [c.strength for c in near] == sorted(
            (c.strength for c in near), reverse=True
        )

    def test_the_summary_points_at_a_tunable_threshold(self, clips):
        summary = run(clips["B"]).rejection_summary()
        assert summary
        assert all(isinstance(count, int) for count in summary.values())

    def test_a_stricter_setting_is_visible_in_the_gates(self, clips):
        """Re-running with a different setting is how a real lecture is tuned."""
        options = SlideDetectionOptions()
        options.normal_transition.min_content_replaced = 0.95
        options.strong_global_transition.enabled = False

        strict = run(clips["D"], options)
        assert len(strict.appearances) == 1
        assert strict.rejection_summary()

    def test_appearances_carry_their_provenance(self, clips):
        appearances = run(clips["F"]).appearances
        assert appearances[1].detection_reason == "strong global transition"
        assert appearances[1].detection_confidence is not None
        assert appearances[1].manually_added is False
