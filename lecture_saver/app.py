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

#: How Windows groups the running window on the taskbar. Without it the window
#: is filed under ``pythonw.exe``, with Python's icon instead of this one.
APP_USER_MODEL_ID = "LectureSaver.App"


def ensure_output_streams() -> None:
    """Give a console-less start somewhere to write.

    Started from a shortcut through ``pythonw.exe`` there is no console, and
    ``sys.stdout``/``sys.stderr`` are ``None``. Anything that writes to them -
    the progress bar of a first model download, a library warning - would then
    crash the app. They are pointed at the null device instead; the log file
    still receives everything worth keeping.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


def _set_taskbar_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:  # noqa: BLE001 - cosmetic; never worth failing a start over
        pass


def run(argv: list[str] | None = None) -> int:
    ensure_output_streams()
    argv = list(sys.argv if argv is None else argv)
    verbose = "--verbose" in argv
    setup_logging(verbose=verbose)

    if "--selftest" in argv:
        return selftest()

    if "--create-shortcuts" in argv or "--remove-shortcuts" in argv:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtGui import QGuiApplication

        from .shortcuts import create_shortcuts, remove_shortcuts

        _app = QGuiApplication.instance() or QGuiApplication(argv)  # noqa: F841 - icon drawing
        if "--remove-shortcuts" in argv:
            return remove_shortcuts()
        return create_shortcuts()

    if "--diagnose" in argv:
        from .slides.diagnose import diagnose

        index = argv.index("--diagnose")
        return diagnose(argv[index + 1 :])

    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import apply_theme

    _set_taskbar_identity()
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setOrganizationName(APP_NAME)
    apply_theme(app)

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

    # Optional: without it the Turbo model falls back to faster-whisper.
    try:
        import openvino
        import openvino_genai

        devices = ", ".join(openvino.Core().available_devices)
        print(f"OpenVINO         : {openvino_genai.__version__.split('-')[0]} ({devices})")
    except Exception as exc:  # noqa: BLE001
        print(f"OpenVINO         : unavailable - {exc} (Turbo will use the CPU engine)")

    free_gb = shutil.disk_usage(Path.home()).free / 1e9
    print(f"Free disk        : {free_gb:.1f} GB")
    if free_gb < 2:
        print("                   WARNING: very little space for models and audio")

    print("\nSelf-test:", "PASSED" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1
