"""Thumbnail output paths for the shared player's "set cover" action.

This is deliberately a small, dependency-free subset of the legacy
``thumb_engine.get_thumb_path`` contract: prefer the source folder's
``.cache`` directory, fall back to a user cache when the source is read-only.
The native panel does not use this path for browsing; it only keeps the player's
cover action working after ``external/file_explorer`` is removed.
"""
from __future__ import annotations

import hashlib
import os
import unicodedata
from pathlib import Path

_SUFFIX = {"small": ".webp", "large": "_lg.webp", "preview": "_preview.webp"}


def thumbnail_path(source_path: str, size_type: str = "small") -> str:
    source_path = unicodedata.normalize("NFC", source_path.replace("\\", "/"))
    suffix = _SUFFIX.get(size_type, ".webp")
    source_dir = os.path.dirname(source_path)
    filename = os.path.basename(source_path)
    local_dir = os.path.join(source_dir, ".cache")
    local_path = os.path.join(local_dir, filename + suffix)

    # Existing caches win regardless of writability (read-only media included).
    if os.path.exists(local_path):
        return local_path
    if os.path.isdir(local_dir) and os.access(local_dir, os.W_OK):
        return local_path

    name_hash = hashlib.md5(source_path.encode("utf-8")).hexdigest()
    global_dir = Path.home() / ".cache" / "topos" / "thumbnails"
    try:
        global_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(global_dir / f"{name_hash}{suffix}")
