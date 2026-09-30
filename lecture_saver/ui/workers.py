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

from PySide6.QtCore import QObject, QThread, Signal, Slot

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

#: What a :class:`JobWorker` job may call to say how far along it is.
ProgressReport = Callable[[str, float], None]

__all__ = [
    "ExportWorker",
    "JobWorker",
    "ProgressReport",
    "ProbeWorker",
    "SlideDetectionWorker",
    "StudyPackResult",
    "StudyPackWorker",
    "TranscriptionWorker",
    "TranslationWorker",
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

    @Slot()
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

    def __init__(
        self, project: object, options: WhisperOptions, *, resume: bool = False
    ) -> None:
        super().__init__()
        self._project = project
        self._options = options
        self._resume = resume
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self) -> None:
        def on_update(update: PipelineUpdate) -> None:
            fraction = -1.0 if update.fraction is None else float(update.fraction)
            self.progress.emit(update.stage.label, fraction, update.message)

        pipeline = TranscriptionPipeline(
            self._project,  # type: ignore[arg-type]
            self._options,
            on_update=on_update,
            should_cancel=self._cancel.is_set,
            resume=self._resume,
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

    @Slot()
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

    @Slot()
    def run(self) -> None:
        def on_progress(export_format: ExportFormat, position: int, total: int) -> None:
            self.progress.emit(export_format.label, position, total)

        result = run_export(
            self._document, self._settings, self._output_dir, on_progress=on_progress
        )
        self.finished.emit(result)


class JobWorker(QObject):
    """Runs one callable on a background thread and hands back its result.

    The job is handed a ``report(stage, fraction)`` callable it may use to say
    how far along it is; the window turns that into a progress bar. A job that
    has nothing to report simply ignores it. The callable must not touch
    widgets or the live project: give it a snapshot.
    """

    progress = Signal(str, float)  # stage label, fraction (-1 = unknown)
    finished = Signal(object)

    def __init__(self, job: Callable[[ProgressReport], object]) -> None:
        super().__init__()
        self._job = job

    def _report(self, stage: str, fraction: float = -1.0) -> None:
        self.progress.emit(stage, fraction)

    @Slot()
    def run(self) -> None:
        try:
            result = self._job(self._report)
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Background job failed")
            result = exc
        self.finished.emit(result)


class TranslationWorker(QObject):
    """Runs an AI translation over a lecture, batch by batch.

    Every finished batch is emitted on :attr:`batch_ready` rather than being
    saved here. The worker is on a background thread and the project belongs
    to the UI thread; Qt delivers the signal there, and the window writes it.
    That ordering is also what makes a long translation survive a failure -
    each batch is on disk before the next is asked for.

    The document handed in must be a snapshot. It is read on this thread while
    the user may well be editing the transcript on the other one.
    """

    #: One finished batch: a BatchOutcome carrying the answer to save.
    batch_ready = Signal(object)
    progress = Signal(int, int)  # batches finished, batches in total
    finished = Signal(object)  # TranslationReport
    failed = Signal(str)

    def __init__(
        self,
        document: LectureDocument,
        engine: object,
        *,
        skip: set[int] | None = None,
    ) -> None:
        super().__init__()
        self._document = document
        self._engine = engine
        self._skip = skip or set()
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    @Slot()
    def run(self) -> None:
        from ..translate.runner import run_translation

        try:
            report = run_translation(
                self._document,
                self._engine,  # type: ignore[arg-type]
                skip=self._skip,
                on_batch=self.batch_ready.emit,
                on_progress=self.progress.emit,
                cancel=self._cancel,
            )
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Translation failed")
            self.failed.emit(str(exc))
            return
        self.finished.emit(report)


class StudyPackWorker(QObject):
    """Makes the study pack: asks for what is missing, then writes the PDF.

    Parts are emitted on :attr:`part_ready` as each arrives so the window -
    which owns the project - saves it before the next request goes out. The
    worker keeps its own copy of the study set to build the PDF from, so it
    never reads the live project from this thread.
    """

    stage = Signal(str)  # "guide", "quiz" or "pdf"
    part_ready = Signal(str, object)  # ("guide", StudyGuide) / ("quiz", Quiz)
    finished = Signal(object)  # StudyPackResult

    def __init__(
        self,
        document: LectureDocument,
        engine: object,
        study: object,
        parts: tuple[str, ...],
        pdf_options: object,
        output_path: Path,
    ) -> None:
        super().__init__()
        self._document = document
        self._engine = engine
        self._study = study
        self._parts = parts
        self._pdf_options = pdf_options
        self._output_path = output_path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        from ..core.study import StudySet, transcript_fingerprint
        from ..export.pdf import PdfExportError
        from ..export.study_pdf import write_study_pack
        from ..study.generator import PART_GUIDE, StudyReport, run_study

        study: StudySet = self._study  # type: ignore[assignment]
        fingerprint = transcript_fingerprint(self._document.segments)
        result = StudyPackResult()

        def on_part(part: str, value: object) -> None:
            if part == PART_GUIDE:
                study.set_guide(value, source=fingerprint)  # type: ignore[arg-type]
            else:
                study.set_quiz(value, source=fingerprint)  # type: ignore[arg-type]
            self.part_ready.emit(part, value)

        try:
            report = (
                run_study(
                    self._document,
                    self._engine,  # type: ignore[arg-type]
                    parts=self._parts,
                    on_part=on_part,
                    on_stage=self.stage.emit,
                    cancel=self._cancel,
                )
                if self._parts else StudyReport()
            )
            result.report = report
            if report.cancelled or not study.is_complete:
                self.finished.emit(result)
                return
            self.stage.emit("pdf")
            write_study_pack(
                self._document, study, self._pdf_options,  # type: ignore[arg-type]
                self._output_path,
            )
            result.pdf = self._output_path
        except PdfExportError as exc:
            result.pdf_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a worker must never crash the app
            log.exception("Study pack failed")
            result.pdf_error = f"Unexpected failure:\n{exc}"
        self.finished.emit(result)


class StudyPackResult:
    """What the study pack run produced, for the message at the end."""

    def __init__(self) -> None:
        from ..study.generator import StudyReport

        self.report = StudyReport()
        self.pdf: Path | None = None
        self.pdf_error = ""
