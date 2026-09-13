"""Shared test setup.

Qt is configured for the ``offscreen`` platform before any PySide6 import so the
UI tests run without a display.

The widget-cleanup fixture matters more than it looks: a ``QMediaPlayer`` that is
still holding a decode pipeline when the interpreter exits will hang Qt's
shutdown indefinitely. Destroying every top-level widget after each test keeps
the suite from wedging.
"""

import gc
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")
# Qt's FFmpeg backend is chatty on stderr about unusual inputs; the tests
# deliberately feed it some, and the noise is not useful here.
os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.*=false")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()


@pytest.fixture(autouse=True)
def _destroy_widgets_after_each_test():
    """Close and delete every top-level widget a test left behind."""
    yield

    try:
        from PySide6.QtMultimedia import QMediaPlayer
        from PySide6.QtWidgets import QApplication
    except ImportError:  # pragma: no cover - PySide6 not installed
        return

    app = QApplication.instance()
    if app is None:
        return

    for widget in list(app.topLevelWidgets()):
        # Stop any media pipeline before the widget goes away, otherwise Qt can
        # block waiting for a decode thread that is still reading.
        for player in widget.findChildren(QMediaPlayer):
            player.stop()
            player.setSource(player.source().__class__())
        widget.close()
        widget.deleteLater()

    app.processEvents()
    gc.collect()
    app.processEvents()
