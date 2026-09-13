"""Application directories and user settings.

Settings live in a small local JSON file. There is no cloud sync, no telemetry
and no analytics anywhere in this application (§29) - this file holds window
geometry, the recent-projects list and the last transcription options, and
nothing else.
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

    def recent_projects(self) -> list[Path]:
        """Recent project roots, most recent first, filtered to ones that exist."""
        entries = [Path(p) for p in self._data.get("recent_projects", [])]
        return [p for p in entries if (p / "project.json").is_file()]

    def add_recent_project(self, root: Path) -> None:
        root = Path(root).resolve()
        existing = [str(p) for p in self.recent_projects() if p.resolve() != root]
        self.set("recent_projects", [str(root), *existing][:MAX_RECENT])

    # -- transcription defaults --------------------------------------------

    def transcription_defaults(self) -> dict[str, Any]:
        stored = self._data.get("transcription_defaults", {})
        return stored if isinstance(stored, dict) else {}

    def set_transcription_defaults(self, values: dict[str, Any]) -> None:
        self.set("transcription_defaults", values)
