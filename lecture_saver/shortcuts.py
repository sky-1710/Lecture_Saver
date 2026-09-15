"""Desktop and Start-menu shortcuts, so Lecture Saver opens like any other app.

    python run.py --create-shortcuts     add them (again, to refresh)
    python run.py --remove-shortcuts     take them away

The shortcuts start the project's own ``pythonw.exe`` - no console window - on
this checkout's ``run.py``, so they always run the current code: nothing is
copied, and an update needs no rebuild. The icon is the one the window already
draws for itself, written once as an ``.ico`` into the per-user data folder, so
the repository still carries no binary assets.

Windows only. Everything here is local: the shortcut files are written with the
Windows Script Host that ships with Windows, and nothing is downloaded.
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

from .config import APP_DISPLAY_NAME, app_data_dir

__all__ = [
    "ICON_SIZES",
    "create_shortcut",
    "create_shortcuts",
    "icon_bytes",
    "remove_shortcuts",
    "shortcut_folders",
]

#: Every size Windows asks an icon for, from list views to the jumbo tiles.
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)

SHORTCUT_NAME = f"{APP_DISPLAY_NAME}.lnk"
ICON_NAME = "lecture_saver.ico"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# -- the icon -------------------------------------------------------------------


def icon_bytes(sizes: tuple[int, ...] = ICON_SIZES) -> bytes:
    """The window icon as a multi-size ``.ico`` file.

    Each size is stored as PNG, which Windows reads in icon files and which
    keeps the small sizes crisp instead of shrinking one large picture.
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize

    from .ui.theme import app_icon

    icon = app_icon()
    images: list[tuple[int, bytes]] = []
    for size in sizes:
        pixmap = icon.pixmap(QSize(size, size))
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buffer, "PNG")
        buffer.close()
        images.append((size, bytes(data.data())))

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries = b""
    body = b""
    for size, png in images:
        dimension = 0 if size >= 256 else size  # 0 means 256 in an icon entry
        entries += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(png), offset + len(body)
        )
        body += png
    return header + entries + body


def _ensure_icon() -> Path:
    path = app_data_dir() / ICON_NAME
    path.write_bytes(icon_bytes())
    return path


# -- where shortcuts go -----------------------------------------------------------


def shortcut_folders() -> dict[str, Path]:
    """The user's Desktop and Start-menu Programs folders, as Windows reports them.

    Asked of Windows rather than guessed, because a Desktop moved into OneDrive
    is not at ``%USERPROFILE%\\Desktop``.
    """
    script = (
        "[Environment]::GetFolderPath('Desktop');"
        "[Environment]::GetFolderPath('Programs')"
    )
    output = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
    ).stdout.splitlines()
    desktop, programs = (line.strip() for line in output[:2])
    return {"Desktop": Path(desktop), "Start menu": Path(programs)}


# -- making them ------------------------------------------------------------------


def _launcher() -> tuple[Path, Path, Path]:
    """``pythonw.exe``, ``run.py`` and the folder to start in."""
    python = Path(sys.executable)
    windowless = python.with_name("pythonw.exe")
    target = windowless if windowless.is_file() else python
    root = Path(__file__).resolve().parents[1]
    return target, root / "run.py", root


def create_shortcut(link: Path, *, icon: Path) -> Path:
    """Write one ``.lnk`` at ``link`` that starts Lecture Saver."""
    target, script, root = _launcher()
    link.parent.mkdir(parents=True, exist_ok=True)
    # Values travel as environment variables so no path - with its spaces,
    # Korean folder names or quotes - is ever pasted into the script text.
    environment = dict(
        os.environ,
        LS_LINK=str(link),
        LS_TARGET=str(target),
        LS_ARGS=f'"{script}"',
        LS_WORKDIR=str(root),
        LS_ICON=f"{icon},0",
        LS_DESCRIPTION="Local lecture transcription and slide-linked notes",
    )
    command = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:LS_LINK);"
        "$s.TargetPath = $env:LS_TARGET;"
        "$s.Arguments = $env:LS_ARGS;"
        "$s.WorkingDirectory = $env:LS_WORKDIR;"
        "$s.IconLocation = $env:LS_ICON;"
        "$s.Description = $env:LS_DESCRIPTION;"
        "$s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        env=environment, capture_output=True, text=True, check=True,
        creationflags=_NO_WINDOW,
    )
    return link


def create_shortcuts() -> int:
    """``--create-shortcuts``: a shortcut on the Desktop and in the Start menu."""
    if sys.platform != "win32":
        print("Shortcuts are only made on Windows.")
        return 1
    icon = _ensure_icon()
    for place, folder in shortcut_folders().items():
        link = create_shortcut(folder / SHORTCUT_NAME, icon=icon)
        print(f"{place:<11}: {link}")
    print(f"Icon       : {icon}")
    return 0


def remove_shortcuts() -> int:
    """``--remove-shortcuts``: take away what :func:`create_shortcuts` made."""
    if sys.platform != "win32":
        return 1
    for place, folder in shortcut_folders().items():
        link = folder / SHORTCUT_NAME
        if link.is_file():
            link.unlink()
            print(f"Removed {place.lower()} shortcut: {link}")
    (app_data_dir() / ICON_NAME).unlink(missing_ok=True)
    return 0
