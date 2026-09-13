"""End-to-end slide detection against a real synthetic lecture video.

FFmpeg builds a short video whose "slides" are solid coloured cards with
distinct shapes, then the production pipeline decodes it, detects the changes,
and writes real JPEGs. Fast enough to run in the normal suite - no lecture
recording required (§33).
"""

import subprocess

import numpy as np
import pytest

from lecture_saver.core.models import StageStatus
from lecture_saver.core.project import Project
from lecture_saver.core.slide_options import SlideDetectionOptions
from lecture_saver.media.errors import Cancelled
from lecture_saver.media.ffmpeg_tools import (
    extract_frame,
    ffmpeg_available,
    find_ffmpeg,
    probe_video,
    stream_gray_frames,
)
from lecture_saver.slides.descriptors import FrameDescriptors, compute_descriptors
from lecture_saver.slides.pipeline import SlideDetectionPipeline

pytestmark = pytest.mark.ffmpeg


def _sampled_at(fps: float) -> SlideDetectionOptions:
    options = SlideDetectionOptions()
    options.sampling.sample_fps = fps
    return options

if not ffmpeg_available():  # pragma: no cover - environment dependent
    pytest.skip("FFmpeg is not available", allow_module_level=True)

SLIDE_SECONDS = 10
SLIDE_COUNT = 4


@pytest.fixture(scope="module")
def slide_video(tmp_path_factory):
    """A 40-second video: four visually distinct 'slides', ten seconds each.

    Each slide is a different background with a differently-placed white bar,
    so the change is structural rather than only a colour shift.
    """
    directory = tmp_path_factory.mktemp("slidevideo")
    ffmpeg = str(find_ffmpeg())

    parts = []
    layouts = [
        ("black", "x=40:y=40:w=400:h=90"),
        ("gray",  "x=120:y=200:w=380:h=140"),
        ("white", "x=60:y=120:w=460:h=70"),
        ("navy",  "x=200:y=60:w=260:h=250"),
    ]
    for index, (colour, box) in enumerate(layouts):
        part = directory / f"part{index}.mp4"
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"color=c={colour}:size=640x360:rate=10:duration={SLIDE_SECONDS}",
                "-vf", f"drawbox={box}:color=red@1.0:t=fill",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(part),
            ],
            check=True, capture_output=True,
        )
        parts.append(part)

    listing = directory / "parts.txt"
    listing.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in parts) + "\n", encoding="utf-8"
    )

    video = directory / "lecture.mp4"
    subprocess.run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video),
        ],
        check=True, capture_output=True,
    )
    return video


@pytest.fixture
def project(tmp_path, slide_video):
    return Project.create(tmp_path / "projects", "Slide test", probe_video(slide_video))


# --------------------------------------------------------------------------
# Frame sampling
# --------------------------------------------------------------------------


class TestFrameSampling:
    def test_samples_at_the_requested_rate(self, slide_video):
        seen: list[float] = []
        count = stream_gray_frames(
            slide_video, width=80, height=45, fps=2.0,
            on_frame=lambda i, t, raw: seen.append(t),
        )
        expected = SLIDE_SECONDS * SLIDE_COUNT * 2
        assert count == pytest.approx(expected, abs=4)
        assert seen[0] == 0.0
        assert seen[1] == pytest.approx(0.5)

    def test_frames_are_the_requested_size_and_grayscale(self, slide_video):
        sizes: list[int] = []
        stream_gray_frames(
            slide_video, width=80, height=45, fps=1.0,
            on_frame=lambda i, t, raw: sizes.append(len(raw)),
        )
        assert sizes
        assert set(sizes) == {80 * 45}

    def test_it_reports_progress(self, slide_video):
        seen: list[float] = []
        stream_gray_frames(
            slide_video, width=32, height=18, fps=1.0,
            on_frame=lambda i, t, raw: None,
            duration=40.0, on_progress=seen.append,
        )
        assert seen and seen[-1] == pytest.approx(1.0)
        assert all(0.0 <= value <= 1.0 for value in seen)

    def test_cancellation_stops_the_scan(self, slide_video):
        with pytest.raises(Cancelled):
            stream_gray_frames(
                slide_video, width=32, height=18, fps=2.0,
                on_frame=lambda i, t, raw: None,
                should_cancel=lambda: True,
            )

    def test_sampling_does_not_decode_every_frame(self, slide_video):
        """2 fps over a 10 fps source must yield a fifth of the frames (§7)."""
        counted = stream_gray_frames(
            slide_video, width=32, height=18, fps=2.0,
            on_frame=lambda i, t, raw: None,
        )
        assert counted < SLIDE_SECONDS * SLIDE_COUNT * 10 / 2


class TestExtractFrame:
    def test_writes_a_jpeg(self, slide_video, tmp_path):
        target = tmp_path / "slides" / "001.jpg"
        extract_frame(slide_video, 5.0, target)

        assert target.is_file()
        assert target.stat().st_size > 500
        assert target.read_bytes()[:2] == b"\xff\xd8"  # JPEG magic

    def test_respects_the_width_cap(self, slide_video, tmp_path):
        target = tmp_path / "capped.jpg"
        extract_frame(slide_video, 5.0, target, max_width=320)
        assert probe_video(target).width == 320

    def test_native_size_is_kept_when_uncapped(self, slide_video, tmp_path):
        target = tmp_path / "full.jpg"
        extract_frame(slide_video, 5.0, target, max_width=-1)
        assert probe_video(target).width == 640

    def test_the_cap_never_upscales(self, slide_video, tmp_path):
        target = tmp_path / "big.jpg"
        extract_frame(slide_video, 5.0, target, max_width=4000)
        assert probe_video(target).width == 640

    def test_frames_from_different_slides_differ(self, slide_video, tmp_path):
        first = tmp_path / "a.jpg"
        second = tmp_path / "b.jpg"
        extract_frame(slide_video, 5.0, first)
        extract_frame(slide_video, 25.0, second)
        assert first.read_bytes() != second.read_bytes()


# --------------------------------------------------------------------------
# Descriptors
# --------------------------------------------------------------------------


class TestDescriptors:
    def test_shape_and_length(self, slide_video):
        options = _sampled_at(2.0)
        descriptors = compute_descriptors(slide_video, options=options, duration=40.0)

        assert len(descriptors) > 60
        assert descriptors.shape == (
            options.sampling.thumb_height, options.sampling.thumb_width
        )
        assert descriptors.has_ink
        assert descriptors.thumbnails.dtype == np.uint8
        assert len(descriptors.times) == len(descriptors)
        assert len(descriptors.hashes) == len(descriptors)

    def test_frames_are_copied_not_aliased(self, slide_video):
        """The sampler reuses its buffer; descriptors must not all be the last frame."""
        descriptors = compute_descriptors(
            slide_video, options=_sampled_at(1.0), duration=40.0
        )
        assert not np.array_equal(descriptors.thumbnails[0], descriptors.thumbnails[-1])

    def test_cache_round_trip(self, slide_video, tmp_path):
        options = _sampled_at(1.0)
        original = compute_descriptors(slide_video, options=options, duration=40.0)

        path = tmp_path / "cache" / "frames.npz"
        original.save(path)
        loaded = FrameDescriptors.load(path)

        assert loaded is not None
        assert len(loaded) == len(original)
        assert np.array_equal(loaded.thumbnails, original.thumbnails)
        assert np.array_equal(loaded.hashes, original.hashes)
        assert loaded.matches(
            sample_signature=options.sample_signature, source_key=original.source_key
        )
        assert np.array_equal(loaded.ink, original.ink)
        assert loaded.ink_shape == original.ink_shape

    def test_a_cache_from_different_sampling_is_rejected(self, slide_video, tmp_path):
        original = compute_descriptors(
            slide_video, options=_sampled_at(1.0), duration=40.0
        )
        path = tmp_path / "frames.npz"
        original.save(path)

        loaded = FrameDescriptors.load(path)
        assert loaded is not None
        assert not loaded.matches(
            sample_signature=_sampled_at(4.0).sample_signature,
            source_key=original.source_key,
        )

    def test_a_missing_or_corrupt_cache_is_ignored(self, tmp_path):
        assert FrameDescriptors.load(tmp_path / "absent.npz") is None
        broken = tmp_path / "broken.npz"
        broken.write_bytes(b"not an npz file")
        assert FrameDescriptors.load(broken) is None


# --------------------------------------------------------------------------
# Whole pipeline
# --------------------------------------------------------------------------


class TestPipeline:
    @pytest.fixture
    def detected(self, project):
        updates: list[tuple[str, float | None, str]] = []
        pipeline = SlideDetectionPipeline(
            project,
            on_update=lambda u: updates.append((u.stage.label, u.fraction, u.message)),
        )
        return project, pipeline.run(), updates

    def test_it_finds_the_four_slides(self, detected):
        _, slide_set, _ = detected
        assert len(slide_set) == SLIDE_COUNT

    def test_stages_are_reported_in_order(self, detected):
        _, _, updates = detected
        stages = list(dict.fromkeys(label for label, _, _ in updates))
        assert stages == [
            "Preparing video",
            "Scanning video for slide changes",
            "Identifying slides",
            "Capturing slide images",
            "Saving project",
            "Ready",
        ]

    def test_scan_progress_reaches_one(self, detected):
        _, _, updates = detected
        scan = [f for label, f, _ in updates
                if label == "Scanning video for slide changes" and f is not None]
        assert scan and scan[-1] == pytest.approx(1.0)

    def test_boundaries_land_near_the_real_transitions(self, detected):
        _, slide_set, _ = detected
        starts = [a.start for a in slide_set.appearances]
        assert starts[0] == 0.0
        for position, start in enumerate(starts[1:], start=1):
            assert start == pytest.approx(position * SLIDE_SECONDS, abs=1.5)

    def test_intervals_are_contiguous_and_cover_the_video(self, detected):
        project, slide_set, _ = detected
        for earlier, later in zip(slide_set.appearances, slide_set.appearances[1:], strict=False):
            assert earlier.end == later.start
        assert slide_set.appearances[-1].end == pytest.approx(
            project.video.duration, abs=1.0
        )

    def test_every_slide_gets_a_real_image(self, detected):
        project, slide_set, _ = detected
        for slide in slide_set.slides:
            assert slide.image, f"slide {slide.slide_id} has no image"
            path = slide.image_path(project.root)
            assert path.is_file()
            assert path.stat().st_size > 500

    def test_captured_images_differ_between_slides(self, detected):
        project, slide_set, _ = detected
        contents = {
            slide.image_path(project.root).read_bytes() for slide in slide_set.slides
        }
        assert len(contents) == len(slide_set.slides)

    def test_a_fingerprint_is_stored_for_phase_seven(self, detected):
        _, slide_set, _ = detected
        for slide in slide_set.slides:
            assert len(slide.fingerprint) == 16
            int(slide.fingerprint, 16)  # valid hex

    def test_the_stage_is_marked_done(self, detected):
        project, _, _ = detected
        assert project.stage_status("slides") is StageStatus.DONE

    def test_results_survive_reopening_the_project(self, detected):
        """§9 - reopening must not require detecting again."""
        project, slide_set, _ = detected
        reopened = Project.open(project.root)

        assert len(reopened.slides) == len(slide_set)
        assert [a.to_dict() for a in reopened.slides.appearances] == [
            a.to_dict() for a in slide_set.appearances
        ]
        for slide in reopened.slides.slides:
            assert slide.image_path(reopened.root).is_file()

    def test_transcript_stages_are_untouched(self, detected):
        """Slide detection must not claim work it did not do (§32)."""
        project, _, _ = detected
        assert project.stage_status("transcription") is StageStatus.NOT_STARTED
        assert project.stage_status("alignment") is StageStatus.NOT_STARTED


class TestRerun:
    def test_a_second_run_reuses_the_cached_scan(self, project):
        """§10 - re-tuning must not re-decode the video."""
        SlideDetectionPipeline(project).run()
        assert project.frame_cache_file.is_file()

        messages: list[str] = []
        SlideDetectionPipeline(
            project, on_update=lambda u: messages.append(u.message)
        ).run()
        assert any("earlier" in message for message in messages)

    def test_a_rerun_with_new_thresholds_uses_the_cache_and_changes_results(self, project):
        SlideDetectionPipeline(project).run()

        insensitive = SlideDetectionOptions()
        insensitive.timing.min_slide_seconds = 600.0
        messages: list[str] = []
        result = SlideDetectionPipeline(
            project, insensitive, on_update=lambda u: messages.append(u.message)
        ).run()

        assert any("earlier" in message for message in messages)
        assert len(result) == 1  # debounce longer than the video

    def test_a_rerun_replaces_old_images_instead_of_accumulating(self, project):
        SlideDetectionPipeline(project).run()
        first = sorted(p.name for p in project.slides_dir.glob("*.jpg"))
        assert len(first) == SLIDE_COUNT

        blunt = SlideDetectionOptions()
        blunt.timing.min_slide_seconds = 600.0
        SlideDetectionPipeline(project, blunt).run()
        second = sorted(p.name for p in project.slides_dir.glob("*.jpg"))
        assert second == ["001.jpg"]

    def test_changing_the_sample_rate_rebuilds_the_cache(self, project):
        SlideDetectionPipeline(project).run()

        messages: list[str] = []
        SlideDetectionPipeline(
            project,
            _sampled_at(4.0),
            on_update=lambda u: messages.append(u.message),
        ).run()
        assert not any("earlier" in message for message in messages)

    def test_the_options_used_are_recorded_for_next_time(self, project):
        options = SlideDetectionOptions()
        options.normal_transition.min_content_replaced = 0.31
        SlideDetectionPipeline(project, options).run()

        stored = Project.open(project.root).meta.slide_detection
        assert stored.normal_transition.min_content_replaced == 0.31


class TestFailureHandling:
    def test_a_missing_video_reports_clearly(self, project, tmp_path):
        from lecture_saver.slides.pipeline import SlidePipelineError

        # Point the project at a path that does not exist rather than deleting
        # the shared fixture video.
        project.meta.video.path = str(tmp_path / "moved-away.mp4")
        with pytest.raises(SlidePipelineError, match="no longer at"):
            SlideDetectionPipeline(project).run()
        assert project.stage_status("slides") is StageStatus.FAILED

    def test_cancelling_marks_the_stage_and_raises(self, project):
        with pytest.raises(Cancelled):
            SlideDetectionPipeline(project, should_cancel=lambda: True).run()
        assert project.stage_status("slides") is StageStatus.CANCELLED
