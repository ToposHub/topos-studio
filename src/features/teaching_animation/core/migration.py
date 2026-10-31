"""Explicit migrations for Teaching Animation scene and payload JSON."""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import math
from typing import Any, Mapping

from .model import PAYLOAD_SCHEMA_VERSION, SCENE_SCHEMA_VERSION


class SceneMigrationError(ValueError):
    """Raised when a scene cannot be safely migrated."""


def _fps_rational(data: Mapping[str, Any]) -> tuple[int, int]:
    if "fps_num" in data or "fps_den" in data:
        return int(data.get("fps_num", 24)), int(data.get("fps_den", 1))
    fps = data.get("fps", 24.0)
    try:
        value = float(fps)
    except (TypeError, ValueError) as exc:
        raise SceneMigrationError("fps must be numeric") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise SceneMigrationError("fps must be finite and positive")
    rational = Fraction(value).limit_denominator(1001)
    return rational.numerator, rational.denominator


def _migrate_v0_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Version 0 (Phase 0/demo) → 1: explicit frame units and field names.

    Version 0 could use ``fps`` and ``duration_seconds`` and called the element
    track collection ``animation_tracks``.  This step does NOT stamp the final
    schema version; the caller's migration chain stamps it at the end.
    """
    fps_num, fps_den = _fps_rational(data)
    data["fps_num"] = fps_num
    data["fps_den"] = fps_den
    if "duration_frames" not in data:
        duration_seconds = data.get("duration_seconds", data.get("duration", 10.0))
        try:
            duration_value = float(duration_seconds)
        except (TypeError, ValueError) as exc:
            raise SceneMigrationError("duration must be numeric") from exc
        if not math.isfinite(duration_value) or duration_value <= 0.0:
            raise SceneMigrationError("duration must be finite and positive")
        data["duration_frames"] = max(
            1, int(round(duration_value * fps_num / fps_den))
        )

    if "elements" not in data and isinstance(data.get("layers"), list):
        data["elements"] = data.pop("layers")
    elements = data.get("elements", [])
    if not isinstance(elements, list):
        raise SceneMigrationError("elements must be an array")
    migrated_elements: list[dict[str, Any]] = []
    for index, element in enumerate(elements):
        if not isinstance(element, Mapping):
            raise SceneMigrationError(f"elements[{index}] must be an object")
        item = dict(element)
        item.setdefault("element_id", f"legacy_element_{index + 1}")
        item.setdefault("type", "text")
        if "transform" not in item and isinstance(item.get("layout"), Mapping):
            item["transform"] = dict(item["layout"])
        if "local_animation_tracks" not in item and "animation_tracks" in item:
            item["local_animation_tracks"] = item.pop("animation_tracks")
        migrated_elements.append(item)
    data["elements"] = migrated_elements
    data.setdefault("animations", [])
    data.setdefault("markers", [])
    data.setdefault("asset_refs", [])
    data.setdefault("background", {"color": "#111827"})
    data.setdefault("name", "Teaching Scene")
    data.setdefault("scene_id", "legacy_scene")
    data["schema_version"] = 1
    return data


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Version 1 → 2: additive ``canvas`` contract (SG-08), no semantic change.

    ``canvas`` is an optional authoring-space descriptor.  When absent it is
    left empty and derived from ``width``/``height`` at read time, so every v1
    scene round-trips through v2 with no loss and no behaviour change.  All
    unknown fields are preserved (AQ-06 round-trip/deprecation policy).
    """
    data.setdefault("canvas", {})
    data.setdefault("cues", [])
    data.setdefault("expressions", [])
    data["schema_version"] = 2
    return data


def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Version 2 → 3: additive ``frame`` element type and ``layout`` mapping.

    WP-3 introduces container frames (Flex/Grid/absolute).  Every element
    gains an optional ``layout`` object; absent means ``{}`` which renders
    exactly like v2.  Unknown element fields keep round-tripping untouched.
    """
    elements = data.get("elements", [])
    if isinstance(elements, list):
        for element in elements:
            if isinstance(element, Mapping):
                item = dict(element)
                item.setdefault("layout", {})
                element.clear()
                element.update(item)
    data["schema_version"] = 3
    return data


def migrate_scene_json(
    raw: Mapping[str, Any],
    *,
    target_version: int = SCENE_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Migrate a scene to the current schema without dropping unknown fields.

    Migration chain: 0 → 1 (legacy frame units) → 2 (additive canvas contract)
    → 3 (additive frame/layout contract).  Each step is purely additive or
    normalizing; unknown fields are preserved.  Versions newer than the
    supported target are rejected, never downgraded.
    """
    if not isinstance(raw, Mapping):
        raise SceneMigrationError("scene JSON root must be an object")
    if target_version != SCENE_SCHEMA_VERSION:
        raise SceneMigrationError(f"unsupported target schema: {target_version}")

    data = deepcopy(dict(raw))
    try:
        version = int(data.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise SceneMigrationError("schema_version must be an integer") from exc
    if version > target_version:
        raise SceneMigrationError(
            f"scene schema {version} is newer than supported {target_version}"
        )

    if version <= 0:
        data = _migrate_v0_to_v1(data)
        version = 1
    if version == 1:
        data = _migrate_v1_to_v2(data)
        version = 2
    if version == 2:
        data = _migrate_v2_to_v3(data)
        version = 3
    data["schema_version"] = target_version
    return data


def migrate_payload_json(
    raw: Mapping[str, Any],
    *,
    target_version: int = PAYLOAD_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Migrate the independent project payload and each contained scene."""
    if not isinstance(raw, Mapping):
        raise SceneMigrationError("teaching payload root must be an object")
    data = deepcopy(dict(raw))
    try:
        version = int(data.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise SceneMigrationError("payload schema_version must be an integer") from exc
    if version > target_version:
        raise SceneMigrationError(
            f"payload schema {version} is newer than supported {target_version}"
        )

    scenes = data.get("scenes", [])
    if isinstance(scenes, Mapping):
        scenes = list(scenes.values())
    if not isinstance(scenes, list):
        raise SceneMigrationError("payload scenes must be an array")
    data["scenes"] = [
        migrate_scene_json(scene)
        for scene in scenes
        if isinstance(scene, Mapping)
    ]
    data.setdefault("clips", [])
    data.setdefault("nodes", [])
    data.setdefault("metadata", {})
    data["schema_version"] = target_version
    return data
