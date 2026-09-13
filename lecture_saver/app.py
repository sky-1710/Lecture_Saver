"""Application bootstrap.

The media backend is selected *before* Qt is imported, because QtMultimedia
reads ``QT_MEDIA_BACKEND`` at import time. Forcing ``ffmpeg`` means Windows
Media Foundation - which is pickier about containers and codecs - is never
silently used instead.
"""

from __future__ import annotations

import os
import sys

# Must precede any PySide6 import.
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")

from .config import APP_DISPLAY_NAME, APP_NAME, Settings  # noqa: E402
from .logging_setup import setup_logging  # noqa: E402


def run(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    verbose = "--verbose" in argv
    setup_logging(verbose=verbose)

    if "--selftest" in argv:
        return selftest()

    if "--diagnose" in argv:
        from .slides.diagnose import diagnose

        index = argv.index("--diagnose")
        return diagnose(argv[index + 1 :])

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow

    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setOrganizationName(APP_NAME)

    settings = Settings()
    window = MainWindow(settings)
    window.show()
    return app.exec()


def selftest() -> int:
    """Non-GUI environment check, so setup problems are diagnosable headlessly."""
    import shutil
    from pathlib import Path

    ok = True
    print(f"Python           : {sys.version.split()[0]}")

    try:
        from .media.ffmpeg_tools import find_ffmpeg, find_ffprobe

        print(f"FFmpeg           : {find_ffmpeg()}")
        print(f"ffprobe          : {find_ffprobe() or 'not found (banner parsing is used)'}")
    except Exception as exc:  # noqa: BLE001
        print(f"FFmpeg           : FAILED - {exc}")
        ok = False

    try:
        from PySide6.QtCore import qVersion

        print(f"Qt               : {qVersion()}")
    except Exception as exc:  # noqa: BLE001
        print(f"Qt               : FAILED - {exc}")
        ok = False

    try:
        import PySide6
        from PySide6.QtMultimedia import QMediaPlayer  # noqa: F401

        plugins = Path(PySide6.__file__).parent / "plugins" / "multimedia"
        backends = sorted(p.stem for p in plugins.iterdir()) if plugins.is_dir() else []
        has_ffmpeg_backend = any("ffmpeg" in b for b in backends)
        print(f"Media backends   : {', '.join(backends) or 'none found'}")
        backend_note = "yes" if has_ffmpeg_backend else "NO - playback may be limited"
        print(f"FFmpeg backend   : {backend_note}")
        if not has_ffmpeg_backend:
            ok = False
    except Exception as exc:  # noqa: BLE001
        print(f"QtMultimedia     : FAILED - {exc}")
        ok = False

    try:
        import faster_whisper

        print(f"faster-whisper   : {faster_whisper.__version__}")
    except Exception as exc:  # noqa: BLE001
        print(f"faster-whisper   : FAILED - {exc}")
        ok = False

    try:
        import ctranslate2

        cuda = ctranslate2.get_cuda_device_count()
    except Exception:  # noqa: BLE001
        cuda = 0
    print(f"CUDA devices     : {cuda} ({'GPU' if cuda else 'CPU-only'})")

    free_gb = shutil.disk_usage(Path.home()).free / 1e9
    print(f"Free disk        : {free_gb:.1f} GB")
    if free_gb < 2:
        print("                   WARNING: very little space for models and audio")

    print("\nSelf-test:", "PASSED" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1
