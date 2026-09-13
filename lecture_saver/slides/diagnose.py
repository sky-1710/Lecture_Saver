"""Explain, for a real lecture, why each slide boundary was or was not made.

Run against an existing project:

    python run.py --diagnose "path\\to\\project" [--at MM:SS] [--all] [--busy]

It replays detection over the **cached** frame descriptors, so it costs about a
second and never re-decodes the video. That is what makes real-world tuning
practical: change a threshold in ``project.json``, run this again, and see
exactly which candidates moved.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..core.project import Project, ProjectError
from ..core.slides import SlideDetectionOptions
from ..core.timecode import format_hms, parse_timestamp
from .annotation_snapshots import add_annotation_snapshots
from .descriptors import FrameDescriptors, source_key_for
from .detector import detect_appearances

__all__ = ["diagnose"]


def diagnose(argv: list[str]) -> int:
    """``--diagnose <project> [--at MM:SS] [--all]``."""
    args = [a for a in argv if not a.startswith("--")]
    root = Path(args[0]) if args else None
    if root is None:
        print("Usage: python run.py --diagnose <project folder> [--at MM:SS] [--all]")
        return 2

    try:
        project = Project.open(root)
    except ProjectError as exc:
        print(f"Could not open the project:\n{exc}")
        return 1

    at_seconds = None
    if "--at" in argv:
        index = argv.index("--at")
        if index + 1 < len(argv):
            try:
                at_seconds = parse_timestamp(argv[index + 1])
            except ValueError:
                print(f"Not a timestamp: {argv[index + 1]!r}")
                return 2

    options = project.meta.slide_detection or SlideDetectionOptions()
    cached = FrameDescriptors.load(project.frame_cache_file)
    if cached is None or not cached.matches(
        sample_signature=options.sample_signature,
        source_key=source_key_for(project.video.as_path),
    ):
        print(
            "No usable frame cache for this project.\n"
            "Run slide detection once in the application first; after that this\n"
            "command replays it without decoding the video again."
        )
        return 1

    duration = project.video.duration or float(cached.times[-1])
    result = detect_appearances(
        cached.thumbnails, cached.times, cached.hashes,
        duration=duration, options=options, ink=cached,
    )

    strong = sum(1 for c in result.committed if c.path == "strong global")
    print(f"Project       : {project.name}")
    print(f"Video         : {project.video.display_name}  ({format_hms(duration)})")
    print(f"Frames        : {len(cached)} sampled at "
          f"{options.sampling.sample_fps} fps")
    print(f"Slides found  : {len(result.appearances)}")
    print(f"Candidates    : {len(result.candidates)} considered, "
          f"{len(result.committed)} committed "
          f"({strong} by the strong-global path), "
          f"{len(result.absorbed)} absorbed, {len(result.rejected)} rejected")
    print()
    print(result.busy.describe())

    summary = result.rejection_summary()
    if summary:
        print("\nWhy candidates did not become slides:")
        for reason, count in summary.items():
            print(f"  {count:5d}  {reason}")
        print(
            "\nThe most common reason is the threshold to adjust first. Each one\n"
            "is a field under \"slide_detection\" in project.json."
        )

    print("\nStrongest candidates that did NOT become slides "
          "(a missed slide will be near the top):")
    near_misses = result.strongest_rejected(limit=8)
    if not near_misses:
        print("  none")
    for candidate in near_misses:
        failed = candidate.failed_gate
        gate = f"{failed.name} {failed.value:.4f} vs {failed.limit:.4f}" if failed else "-"
        print(f"  {format_hms(candidate.time)}  strength {candidate.strength:6.3f}  "
              f"{candidate.reason or '-':<34} {gate}")

    show_all = "--all" in argv
    if "--busy" in argv:
        return 0
    if at_seconds is not None:
        window = [c for c in result.candidates if abs(c.time - at_seconds) <= 15]
        print(f"\nCandidates within 15 s of {format_hms(at_seconds)}:")
        if not window:
            print("  none - nothing changed enough there to be considered.")
        for candidate in window:
            print("\n" + _indent(candidate.describe()))
    else:
        # A frame identical to the current slide is the normal case and says
        # nothing useful; the interesting candidates are those that got past
        # the first gate and then had to be decided.
        interesting = [
            c for c in result.candidates
            if show_all or c.reason != "no ink changed"
        ]
        shown = interesting if show_all else interesting[:25]
        print(
            f"\nCandidates that changed something "
            f"({len(shown)} of {len(interesting)} shown; --all for every one):"
        )
        if not interesting:
            print("  none - the picture never changed enough to be considered.")
        for candidate in shown:
            print("\n" + _indent(candidate.describe()))

    print("\nSlide boundaries:")
    for appearance in result.appearances:
        print(f"  {appearance.appearance_id:3d}  {format_hms(appearance.start)}"
              f" - {format_hms(appearance.end)}")

    saved = add_annotation_snapshots(result, cached, options)
    if len(saved.appearances) != len(result.appearances):
        print("\nSaved slides, with annotated slides kept before and after the notes:")
        for appearance, capture in zip(
            saved.appearances, saved.representative_times, strict=True
        ):
            label = appearance.annotation_label
            print(f"  {appearance.appearance_id:3d}  {format_hms(appearance.start)}"
                  f" - {format_hms(appearance.end)}  image {format_hms(capture)}"
                  f"{'  ' + label if label else ''}")
    return 0


def _indent(text: str) -> str:
    return "\n".join("  " + line for line in text.splitlines())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(diagnose(sys.argv[1:]))
