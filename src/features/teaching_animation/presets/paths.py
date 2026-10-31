"""Disk locations for the Motion Graphic preset library.

The library intentionally lives in plain folders — one folder per preset,
grouped by category — so a whole library can be copied, synced, reviewed in
git and batch-migrated by simply moving the directory (see presets/README.md
for the on-disk contract).

Three roots are merged at scan time:

* bundled — ships with the app, versioned in git, read-only for users;
* user — the per-user global library (``TOPOS_MG_PRESET_ROOT`` overrides);
* project — ``<project dir>/mg_presets/`` next to the open ``.topos`` file,
  set by the host while a project is open so a preset collection travels
  with its project (copy the project folder and the presets follow).
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

_APP_DIR_NAME = "Topos"
_LIBRARY_DIR_NAME = "mg_presets"

_STATE_LOCK = threading.Lock()
_ACTIVE_PROJECT_DIR: Path | None = None


def bundled_preset_root() -> Path:
    """Presets shipped with the app (code-authored, versioned in git)."""
    return Path(__file__).resolve().parent / "library"


def user_preset_root() -> Path:
    """Writable per-user library root (``TOPOS_MG_PRESET_ROOT`` overrides)."""
    override = os.environ.get("TOPOS_MG_PRESET_ROOT", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / _APP_DIR_NAME
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Roaming" / _APP_DIR_NAME
    else:
        base = Path.home() / ".config" / "topos"
    return base / _LIBRARY_DIR_NAME


def set_active_project_dir(project_dir: str | os.PathLike | None) -> Path | None:
    """Set (or clear with ``None``) the open project's directory.

    While set, ``<project_dir>/mg_presets/`` joins the scan roots as the
    last (highest-priority, writable) root; the folder itself is created
    lazily on the first save, not by setting this value.
    """
    global _ACTIVE_PROJECT_DIR
    raw = "" if project_dir is None else str(project_dir).strip()
    with _STATE_LOCK:
        _ACTIVE_PROJECT_DIR = Path(raw).expanduser().resolve() if raw else None
        return _ACTIVE_PROJECT_DIR


def set_active_project_file(project_file: str | os.PathLike | None) -> Path | None:
    """Convenience wrapper: derive the project dir from a ``.topos`` path."""
    raw = "" if project_file is None else str(project_file).strip()
    return set_active_project_dir(Path(raw).parent if raw else None)


def active_project_dir() -> Path | None:
    with _STATE_LOCK:
        return _ACTIVE_PROJECT_DIR


def project_preset_root() -> Path | None:
    """The project-local library root, or ``None`` when no project is open."""
    base = active_project_dir()
    return base / _LIBRARY_DIR_NAME if base is not None else None


def default_preset_roots() -> list[Path]:
    """Scan order: bundled, user, then project (later roots win id clashes)."""
    roots = [bundled_preset_root()]
    user = user_preset_root()
    if user not in roots:
        roots.append(user)
    project = project_preset_root()
    if project is not None and project not in roots:
        roots.append(project)
    return roots


__all__ = [
    "active_project_dir",
    "bundled_preset_root",
    "default_preset_roots",
    "project_preset_root",
    "set_active_project_dir",
    "set_active_project_file",
    "user_preset_root",
]
