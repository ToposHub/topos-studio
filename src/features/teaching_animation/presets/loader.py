"""Load a preset package into a validated scene document."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from src.shared.keyframe import KeyframeTrack
from ..core.model import SceneElement, TeachingSceneDocument
from ..core.validator import validate_scene
from .manifest import PRESET_ENTRY_CODE, PresetError, PresetManifest

_MAX_CODE_BYTES = 256 * 1024


def _load_code_scene(path: Path) -> dict[str, Any]:
    """Execute a preset ``build.py`` and return its scene payload.

    Code presets share the trust model of the existing ``.tglsl`` plugin
    system: local files the user (or the app bundle) deliberately placed on
    disk.  Size is capped and the module never stays in ``sys.modules``.
    """
    if path.stat().st_size > _MAX_CODE_BYTES:
        raise PresetError(f"preset code exceeds {_MAX_CODE_BYTES} bytes: {path.name}")
    module_name = f"topos_mg_preset_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PresetError(f"cannot import preset module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
        build = getattr(module, "build", None)
        if not callable(build):
            raise PresetError(f"{path.name} does not define build()")
        result = build()
    except PresetError:
        raise
    except Exception as exc:  # noqa: BLE001 — module boundary reports author errors
        raise PresetError(f"preset build failed: {exc}") from exc
    finally:
        sys.modules.pop(module_name, None)
    if isinstance(result, TeachingSceneDocument):
        return result.to_dict()
    if isinstance(result, Mapping):
        return dict(result)
    raise PresetError(f"{path.name} build() must return a scene dict, got {type(result).__name__}")


def _normalize_preset_scene(scene: TeachingSceneDocument) -> TeachingSceneDocument:
    """Fold scene-level animations into per-element local tracks.

    Presets must graft as pure elements: every animation travels with its
    element so insertion needs no scene-level merge and undo stays a single
    command.  Animations pointing at missing elements are dropped here rather
    than failing validation later.
    """
    if not scene.animations and scene.html_layer is None:
        return scene
    by_element: dict[str, list[KeyframeTrack]] = {}
    for animation in scene.animations:
        by_element.setdefault(animation.element_id, []).append(animation.track)
    elements: list[SceneElement] = []
    changed = False
    for element in scene.elements:
        extras = by_element.get(element.element_id)
        if extras:
            merged = tuple(element.local_animation_tracks) + tuple(extras)
            element = replace(element, local_animation_tracks=merged)
            changed = True
        elements.append(element)
    if not changed and not scene.animations and scene.html_layer is None:
        # AUD-29701: ``changed`` only tracks folded-in animations — when every
        # animation dangles (element renamed/removed) the early return used
        # to keep them on the scene, contradicting the drop contract above
        # and failing validation with "animation element does not exist".
        return scene
    return replace(scene, elements=tuple(elements), animations=(), html_layer=None)


def load_preset_scene(manifest: PresetManifest, folder: Path) -> TeachingSceneDocument:
    """Read + validate one preset package into an editable scene document."""
    entry = folder / manifest.entry
    if not entry.is_file():
        raise PresetError(f"missing preset entry file: {manifest.entry}")
    if manifest.entry == PRESET_ENTRY_CODE:
        data = _load_code_scene(entry)
    else:
        try:
            data = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PresetError(f"unreadable scene.json: {exc}") from exc
    try:
        scene = TeachingSceneDocument.from_dict(data)
    except (TypeError, ValueError) as exc:
        # AUD-29702: callers only catch PresetError; a structurally valid
        # JSON payload that is not a valid scene document must not escape
        # as a raw ValueError/TypeError into the Qt event loop.
        raise PresetError(f"preset scene document is malformed: {exc}") from exc
    scene = _normalize_preset_scene(scene)
    result = validate_scene(scene)
    if not result.is_valid:
        details = "; ".join(issue.message for issue in result.errors[:4])
        raise PresetError(f"preset scene failed validation: {details}")
    return scene


__all__ = ["PresetError", "load_preset_scene"]
