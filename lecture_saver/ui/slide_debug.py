"""Developer panel for inspecting why a slide boundary was or was not made.

Reading numbers in a terminal only goes so far. When a real lecture produces a
duplicate or misses a change, the fastest way to understand it is to see the two
frames the detector compared, what it thought had changed, and which regions it
was ignoring - side by side with the gate values.

Deliberately a debug tool rather than production UI: it is reached from
**Help -> Slide detection debug**, and it replays detection from the cached
frame scan, so opening it costs about a second and never re-decodes the video.
"""

from __future__ import annotations

import logging

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.project import Project
from ..core.slide_options import SlideDetectionOptions
from ..core.timecode import format_hms
from ..slides.descriptors import FrameDescriptors, source_key_for
from ..slides.detector import compute_noise_mask, detect_appearances
from ..slides.diagnostics import Candidate, Outcome
from ..slides.ink_analysis import compare_ink

log = logging.getLogger(__name__)

__all__ = ["SlideDebugDialog"]

_PREVIEW_WIDTH = 300


def _to_pixmap(array: np.ndarray, width: int = _PREVIEW_WIDTH) -> QPixmap:
    """Turn a grayscale or boolean array into a scaled pixmap."""
    if array.dtype == bool:
        array = np.where(array, np.uint8(255), np.uint8(40))
    data = np.ascontiguousarray(array.astype(np.uint8))
    height, cols = data.shape
    image = QImage(data.data, cols, height, cols, QImage.Format.Format_Grayscale8)
    return QPixmap.fromImage(image.copy()).scaledToWidth(
        width, Qt.TransformationMode.FastTransformation
    )


class SlideDebugDialog(QDialog):
    """Shows each candidate transition with the evidence behind the decision."""

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Slide detection debug - {project.name}")
        self.resize(1080, 760)

        self._project = project
        self._options: SlideDetectionOptions = (
            project.meta.slide_detection or SlideDetectionOptions()
        )
        self._descriptors: FrameDescriptors | None = None
        self._candidates: list[Candidate] = []

        self._build()
        self._load()

    # -- construction -------------------------------------------------------

    def _build(self) -> None:
        self._summary = QPlainTextEdit()
        self._summary.setReadOnly(True)
        self._summary.setMaximumHeight(190)
        self._summary.setStyleSheet("font-family: Consolas, monospace;")

        self._picker = QComboBox()
        self._picker.setMinimumWidth(420)
        self._filter = QComboBox()
        self._filter.addItem("Near misses first", "near")
        self._filter.addItem("In time order", "time")
        self._filter.addItem("Accepted only", "committed")
        self._filter.addItem("Absorbed only", "absorbed")

        self._refresh = QPushButton("Re-run from cache")
        self._refresh.setToolTip(
            "Replays detection over the cached frame scan using the settings\n"
            "currently in project.json. Edit a threshold there, then press this."
        )

        top = QHBoxLayout()
        top.addWidget(QLabel("Candidate:"))
        top.addWidget(self._picker, 1)
        top.addWidget(self._filter)
        top.addWidget(self._refresh)

        self._images: dict[str, QLabel] = {}
        grid = QGridLayout()
        for column, (key, title) in enumerate(
            (
                ("before", "Slide baseline"),
                ("after", "Candidate frame"),
                ("changed", "Changed ink"),
                ("busy", "Ignored (busy) regions"),
            )
        ):
            caption = QLabel(title)
            caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
            caption.setStyleSheet("font-weight: 600;")
            view = QLabel()
            view.setAlignment(Qt.AlignmentFlag.AlignCenter)
            view.setMinimumHeight(180)
            view.setStyleSheet("background: #202024; border: 1px solid #3a3a40;")
            grid.addWidget(caption, 0, column)
            grid.addWidget(view, 1, column)
            self._images[key] = view

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setStyleSheet("font-family: Consolas, monospace;")

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addLayout(top)
        layout.addLayout(grid)
        layout.addWidget(self._detail, 1)

        self._picker.currentIndexChanged.connect(self._show_selected)
        self._filter.currentIndexChanged.connect(self._populate)
        self._refresh.clicked.connect(self._load)

    # -- data ---------------------------------------------------------------

    def _load(self) -> None:
        self._options = self._project.meta.slide_detection or SlideDetectionOptions()
        cached = FrameDescriptors.load(self._project.frame_cache_file)
        if cached is None or not cached.matches(
            sample_signature=self._options.sample_signature,
            source_key=source_key_for(self._project.video.as_path),
        ):
            self._summary.setPlainText(
                "No usable frame cache for this project.\n\n"
                "Run slide detection once from the main window; after that this "
                "panel replays it without decoding the video again."
            )
            self._picker.clear()
            return

        self._descriptors = cached
        duration = self._project.video.duration or float(cached.times[-1])
        result = detect_appearances(
            cached.thumbnails, cached.times, cached.hashes,
            duration=duration, options=self._options, ink=cached,
        )
        self._candidates = result.candidates
        self._mask = compute_noise_mask(cached.thumbnails, self._options)

        strong = sum(1 for c in result.committed if c.path == "strong global")
        lines = [
            f"{len(result.appearances)} slides from {len(cached)} sampled frames "
            f"at {self._options.sampling.sample_fps} fps",
            f"{len(result.candidates)} candidates: {len(result.committed)} accepted "
            f"({strong} by the strong-global path), {len(result.absorbed)} absorbed, "
            f"{len(result.rejected)} rejected",
            "",
            result.busy.describe(),
        ]
        summary = result.rejection_summary()
        if summary:
            lines.append("")
            lines.append("Reasons candidates did not become slides:")
            lines += [f"  {count:5d}  {reason}" for reason, count in summary.items()]
        self._summary.setPlainText("\n".join(lines))
        self._populate()

    def _populate(self) -> None:
        mode = self._filter.currentData()
        if mode == "committed":
            shown = [c for c in self._candidates if c.outcome == Outcome.COMMITTED]
        elif mode == "absorbed":
            shown = [c for c in self._candidates if c.outcome == Outcome.ABSORBED]
        elif mode == "near":
            shown = sorted(
                (c for c in self._candidates if c.reason != "no ink changed"),
                key=lambda c: -c.strength,
            )
        else:
            shown = [c for c in self._candidates if c.reason != "no ink changed"]

        self._shown = shown
        self._picker.blockSignals(True)
        self._picker.clear()
        for candidate in shown:
            marker = {
                Outcome.COMMITTED: "ACCEPTED",
                Outcome.ABSORBED: "absorbed",
            }.get(candidate.outcome, "rejected")
            self._picker.addItem(
                f"{format_hms(candidate.time)}  [{marker}]  "
                f"{candidate.reason or candidate.path}"
            )
        self._picker.blockSignals(False)
        self._show_selected()

    # -- display ------------------------------------------------------------

    def _show_selected(self) -> None:
        row = self._picker.currentIndex()
        if self._descriptors is None or not (0 <= row < len(self._shown)):
            self._detail.setPlainText("")
            for view in self._images.values():
                view.clear()
            return

        candidate = self._shown[row]
        self._detail.setPlainText(
            candidate.describe()
            + "\n\nMeasurements:\n"
            + "\n".join(
                f"  {name:<24} {value:.5f}"
                for name, value in sorted(candidate.metrics.items())
            )
        )

        index = int(np.argmin(np.abs(self._descriptors.times - candidate.time)))
        baseline = self._baseline_index(index)

        self._images["before"].setPixmap(_to_pixmap(self._descriptors.thumbnails[baseline]))
        self._images["after"].setPixmap(_to_pixmap(self._descriptors.thumbnails[index]))

        if self._descriptors.has_ink:
            before_ink = self._descriptors.ink_at(baseline)
            after_ink = self._descriptors.ink_at(index)
            self._images["changed"].setPixmap(_to_pixmap(before_ink ^ after_ink))
            # Recomputed here purely to keep the panel self-contained.
            compare_ink(before_ink, after_ink, self._options, self._mask)
        else:
            self._images["changed"].setText("no ink masks in this cache")

        self._images["busy"].setPixmap(_to_pixmap(np.kron(self._mask, np.ones((24, 24)))))

    def _baseline_index(self, index: int) -> int:
        """The frame the detector was comparing against at this candidate.

        Approximated as the start of the appearance the candidate falls in,
        which is what the detector's reference tracks between absorbs.
        """
        starts = [c.time for c in self._candidates if c.outcome == Outcome.COMMITTED]
        time = float(self._descriptors.times[index])  # type: ignore[union-attr]
        previous = [s for s in starts if s < time]
        anchor = previous[-1] if previous else 0.0
        assert self._descriptors is not None
        return int(np.argmin(np.abs(self._descriptors.times - anchor)))
