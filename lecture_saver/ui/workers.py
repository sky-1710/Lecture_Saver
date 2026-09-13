"""Background workers.

Transcription can run for hours, so it must never touch the UI thread (§26).
Each worker is a plain ``QObject`` moved onto a ``QThread``; results and
progress come back as signals, which Qt delivers to the UI thread safely.

Cancellation uses a ``threading.Event`` checked by the pipeline between
segments, so a cancel takes effect within seconds rather than at the end of
the file.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from ..core.models import VideoInfo
from ..core.slides import SlideDetectionOptions
from ..export.document import LectureDocument
from ..export.exporter import run_export
from ..export.options import ExportFormat, ExportSettings
from ..media.errors import Cancelled, MediaError
from ..media.ffmpeg_tools import probe_video
from ..slides.pipeline import SlideDetectionPipeline, SlideUpdate
from ..transcription.pipeline import PipelineUpdate, TranscriptionPipeline
from ..transcription.whisper_engine import WhisperOptions

log = logging.getLogger(__name__)

__all__ = [
    "ExportWorker",
    "JobWorker",
    "ProbeWorker",
    "SlideDetectionWorker",
    "TranscriptionWorker",
    "run_in_thread",
]


def run_in_thread(worker: QObject, thread: QThread) -> None:
    """Move ``worker`` onto ``thread`` and start it via its ``run`` slot."""
    worker.moveToThread(thread)
    thread.started.connect(worker.run)  # type: ignore[attr-defined]
    thread.start()


class ProbeWorker(QObject):
    """Reads video metadata off the UI thread - FFmpeg can take a moment."""

    finished = Signal(object)  # VideoInfo
    failed = Signal(str)

    def __init__(self, video_path: Path) -> None:
        super().__init__()
        self._path = video_path

    def run(self) -> None:
        try:
            info: VideoInfo = probe_video(self._path)
        except MediaError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected probe failure")
            self.failed.emit(f"This video could not be read:\n{exc}")
            return
        self.finished.emit(info)


class TranscriptionWorker(QObject):
    """Runs :class:`TranscriptionPipeline` on a background thread."""

    progress = Signal(str, float, str)  # stage label, fraction (-1 = unknown), message
    finished = Signal(object)  # TranscriptionResult
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, project: object, options: WhisperOptions) -> None:
        super().__init__()
        self._project = project
        self._options = options
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        def on_update(update: PipelineUpdate) -> None:
            fraction = -1.0 if update.fraction is None else float(update.fraction)
            self.progress.emit(update.stage.label, fraction, update.message)

        pipeline = TranscriptionPipeline(
            self._project,  # type: ignore[arg-type]
            self._options,
            on_update=on_update,
            should_cancel=self._cancel.is_set,
        )
        try:
            result = pipeline.run()
        except Cancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Transcription failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


class SlideDetectionWorker(QObject):
    """Runs :class:`SlideDetectionPipeline` on a background thread (§8)."""

    progress = Signal(str, float, str)  # stage label, fraction (-1 = unknown), message
    finished = Signal(object)  # SlideSet
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, project: object, options: SlideDetectionOptions) -> None:
        super().__init__()
        self._project = project
        self._options = options
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self) -> None:
        def on_update(update: SlideUpdate) -> None:
            fraction = -1.0 if update.fraction is None else float(update.fraction)
            self.progress.emit(update.stage.label, fraction, update.message)

        pipeline = SlideDetectionPipeline(
            self._project,  # type: ignore[arg-type]
            self._options,
            on_update=on_update,
            should_cancel=self._cancel.is_set,
        )
        try:
            slide_set = pipeline.run()
        except Cancelled:
            self.cancelled.emit()
            return
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Slide detection failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(slide_set)


class ExportWorker(QObject):
    """Writes exports on a background thread (§26).

    It is given a :class:`LectureDocument` snapshot rather than the project, so
    the user can keep editing while a long PDF is built.
    """

    progress = Signal(str, int, int)  # format label, position, total
    finished = Signal(object)  # ExportResult

    def __init__(
        self, document: LectureDocument, settings: ExportSettings, output_dir: Path
    ) -> None:
        super().__init__()
        self._document = document
        self._settings = settings
        self._output_dir = output_dir

    def run(self) -> None:
        def on_progress(export_format: ExportFormat, position: int, total: int) -> None:
            self.progress.emit(export_format.label, position, total)

        result = run_export(
            self._document, self._settings, self._output_dir, on_progress=on_progress
        )
        self.finished.emit(result)


class JobWorker(QObject):
    """Runs one callable on a background thread and hands back its result.

    For short jobs that report nothing along the way, such as the one-click
    exports. The callable must not touch widgets or the live project: give it a
    snapshot.
    """

    finished = Signal(object)

    def __init__(self, job: Callable[[], object]) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            result = self._job()
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Background job failed")
            result = exc
        self.finished.emit(result)
