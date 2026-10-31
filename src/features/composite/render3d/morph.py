"""Blend-shape/morph-target support for imported composite meshes.

Morph targets are stored as position and optional normal *deltas* in
``MeshData``.  Applying them before skinning matches the glTF deformation
order (morph -> skin) and keeps the GPU path simple: the resulting mesh can
use the existing plain or skinned vertex shader.
"""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

import numpy as np


class MorphError(ValueError):
    """Raised when morph data cannot be represented safely."""


def decode_morph_weights(value: Any) -> tuple[dict[str, float], tuple[str, ...]]:
    """Decode the JSON-safe Model3D ``morph_weights`` parameter."""
    raw = value
    if isinstance(value, str):
        if not value.strip():
            return {}, ()
        try:
            raw = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}, ("model.morph_weights_invalid",)
    if raw is None:
        return {}, ()
    if not isinstance(raw, Mapping):
        return {}, ("model.morph_weights_invalid",)

    decoded: dict[str, float] = {}
    diagnostics: list[str] = []
    for key, raw_weight in raw.items():
        name = str(key).strip()
        try:
            weight = float(raw_weight)
        except (TypeError, ValueError):
            if "model.morph_weights_invalid" not in diagnostics:
                diagnostics.append("model.morph_weights_invalid")
            continue
        if not name or not math.isfinite(weight):
            if "model.morph_weights_invalid" not in diagnostics:
                diagnostics.append("model.morph_weights_invalid")
            continue
        decoded[name] = weight
    return dict(sorted(decoded.items())), tuple(diagnostics)


def encode_morph_weights(weights: Mapping[str, float]) -> str:
    """Serialize morph weights deterministically for project persistence."""
    normalized, _ = decode_morph_weights(weights)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def apply_morph_targets(mesh: Any, weights: Mapping[str, float]) -> Any:
    """Apply active morph deltas and return a new ``MeshData``.

    Unknown target names are ignored so one control document can be shared by
    variants of an asset.  The returned mesh preserves skin attributes (morph
    is evaluated before skinning) but removes the consumed target data.
    """
    from .primitives import MeshData

    targets = getattr(mesh, "morph_targets", {}) or {}
    if not targets:
        return mesh
    normal_targets = getattr(mesh, "morph_normals", {}) or {}
    active = [
        (str(name), float(weight))
        for name, weight in weights.items()
        if str(name) in targets and float(weight) != 0.0
    ]
    if not active:
        return mesh

    vertices = np.asarray(mesh.vertices, dtype=np.float32).copy()
    normals = np.asarray(mesh.normals, dtype=np.float32).copy()
    for name, weight in active:
        vertices += np.asarray(targets[name], dtype=np.float32) * weight
        if name in normal_targets:
            normals += np.asarray(normal_targets[name], dtype=np.float32) * weight

    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(
        normals,
        np.maximum(lengths, 1.0e-8),
        out=np.zeros_like(normals),
        where=lengths > 1.0e-8,
    )
    return MeshData(
        vertices=vertices,
        normals=normals,
        uvs=np.asarray(mesh.uvs, dtype=np.float32).copy(),
        indices=np.asarray(mesh.indices, dtype=np.uint32).copy(),
        bone_indices=(
            np.asarray(mesh.bone_indices, dtype=np.uint32).copy()
            if getattr(mesh, "bone_indices", None) is not None else None
        ),
        bone_weights=(
            np.asarray(mesh.bone_weights, dtype=np.float32).copy()
            if getattr(mesh, "bone_weights", None) is not None else None
        ),
    )


__all__ = [
    "MorphError",
    "apply_morph_targets",
    "decode_morph_weights",
    "encode_morph_weights",
]
