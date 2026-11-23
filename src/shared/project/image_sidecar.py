"""Resolve a standalone image's adjacent project without confusing same-stem photos."""
from __future__ import annotations

import os

from src.shared.project.library import read_project_meta


def _belongs_to_image(candidate: str, file_path: str) -> bool:
    try:
        meta = read_project_meta(candidate, require_manifest_mode=True)
    except (AttributeError, EOFError, RuntimeError, TypeError, ValueError):
        return False
    if not meta or meta.project_mode != "standalone_image" or not meta.preview_path:
        return False
    if "\x00" in meta.preview_path:
        return False
    try:
        return os.path.samefile(meta.preview_path, file_path)
    except OSError:
        return os.path.realpath(meta.preview_path) == os.path.realpath(file_path)


def resolve_image_sidecar(file_path: str, *, extension: str = ".topos") -> tuple[str, bool]:
    """Return the owned sidecar or an available write path, with ownership status."""
    base_path = os.path.splitext(file_path)[0] + extension
    base_exists = os.path.lexists(base_path)
    if base_exists and _belongs_to_image(base_path, file_path):
        return base_path, True
    alternate = file_path + extension
    index = 1
    while os.path.lexists(alternate):
        if _belongs_to_image(alternate, file_path):
            return alternate, True
        index += 1
        alternate = f"{file_path}.{index}{extension}"
    return (alternate if base_exists else base_path), False
