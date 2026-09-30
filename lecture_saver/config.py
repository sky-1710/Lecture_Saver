"""Application directories and user settings.

Settings live in a small local JSON file. There is no cloud sync, no telemetry
and no analytics anywhere in this application (§29) - this file holds window
geometry, the recent-projects list, the last transcription options and the
default export folder and choices, and nothing else.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .core.atomic_io import read_json, write_json_atomic

log = logging.getLogger(__name__)

APP_NAME = "LectureSaver"
APP_DISPLAY_NAME = "Lecture Saver"
MAX_RECENT = 10


def app_data_dir() -> Path:
    """Per-user config directory (``%APPDATA%\\LectureSaver`` on Windows)."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_projects_dir() -> Path:
    """Where new lecture projects are created unless the user picks elsewhere."""
    documents = Path.home() / "Documents"
    base = documents if documents.is_dir() else Path.home()
    return base / APP_DISPLAY_NAME / "Projects"


def icloud_drive_dir() -> Path | None:
    """The iCloud Drive folder iCloud for Windows keeps in sync, if installed.

    Only used as the place the folder picker starts; the application itself
    never uploads anything - iCloud syncs whatever the user saves there.
    """
    folder = Path.home() / "iCloudDrive"
    return folder if folder.is_dir() else None


def log_file() -> Path:
    return app_data_dir() / "lecture_saver.log"


class Settings:
    """Local user settings, loaded once and saved atomically."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (app_data_dir() / "settings.json")
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._data = {}
            return
        try:
            loaded = read_json(self.path)
            self._data = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            # Corrupt settings must never stop the app from starting.
            log.warning("Could not read settings; starting with defaults", exc_info=True)
            self._data = {}

    def save(self) -> None:
        try:
            write_json_atomic(self.path, self._data)
        except OSError:
            log.warning("Could not save settings", exc_info=True)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    # -- projects -----------------------------------------------------------

    @property
    def projects_dir(self) -> Path:
        stored = self._data.get("projects_dir")
        return Path(stored) if stored else default_projects_dir()

    @projects_dir.setter
    def projects_dir(self, value: Path) -> None:
        self.set("projects_dir", str(value))

    @property
    def default_export_dir(self) -> Path | None:
        """Where every lecture exports unless it has chosen a folder of its own."""
        stored = self._data.get("default_export_dir")
        return Path(stored) if stored else None

    @default_export_dir.setter
    def default_export_dir(self, value: Path | None) -> None:
        self.set("default_export_dir", str(value) if value else None)

    @property
    def export_defaults(self) -> dict[str, Any]:
        """The last export choices, for lectures that have none of their own."""
        stored = self._data.get("export_defaults", {})
        return stored if isinstance(stored, dict) else {}

    @export_defaults.setter
    def export_defaults(self, values: dict[str, Any]) -> None:
        self.set("export_defaults", values)

    def recent_projects(self) -> list[Path]:
        """Recent project roots, most recent first, filtered to ones that exist."""
        entries = [Path(p) for p in self._data.get("recent_projects", [])]
        return [p for p in entries if (p / "project.json").is_file()]

    def add_recent_project(self, root: Path) -> None:
        root = Path(root).resolve()
        existing = [str(p) for p in self.recent_projects() if p.resolve() != root]
        self.set("recent_projects", [str(root), *existing][:MAX_RECENT])

    # -- in-app translation -------------------------------------------------
    #
    # The only setting in this file that governs anything leaving the machine.
    # It starts off, and turning it on is a deliberate act: the window asks
    # once, in as many words, before the first lecture is ever sent.

    @property
    def translation_enabled(self) -> bool:
        """Whether 'Translate with AI' is offered at all. Off until agreed to."""
        return bool(self._data.get("translation_enabled", False))

    @translation_enabled.setter
    def translation_enabled(self, value: bool) -> None:
        self.set("translation_enabled", bool(value))

    @property
    def translation_engine(self) -> str:
        return str(self._data.get("translation_engine", "codex"))

    @translation_engine.setter
    def translation_engine(self, value: str) -> None:
        self.set("translation_engine", str(value))

    # -- transcription defaults --------------------------------------------

    def transcription_defaults(self) -> dict[str, Any]:
        stored = self._data.get("transcription_defaults", {})
        return stored if isinstance(stored, dict) else {}

    def set_transcription_defaults(self, values: dict[str, Any]) -> None:
        self.set("transcription_defaults", values)
