"""SG-02 / SG-04: composition schema — SceneInstance (Precomp) + Mask/Matte.

These let a scene reuse another scene by reference (a Precomp/Instance) and
apply vector/alpha/luma masks + track mattes to layers.  They are pure data
types stored in ``scene.metadata`` (under versioned keys, like the motion plans)
so they round-trip without forcing a schema-version bump; a later schema wave
can promote them to first-class model fields once the renderer consumes them.

A ``SceneInstance`` references a source ``scene_id`` (never copies its JSON —
the plan's invariant §3.1.1: "实例不得复制场景 JSON") with per-instance params
and a local time range.  A ``Mask`` declares how a target layer is masked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class MaskKind(str, Enum):
    ALPHA = "alpha"
    LUMA = "luma"
    VECTOR = "vector"


class MatteMode(str, Enum):
    """Track-matte alpha sourcing."""

    NONE = "none"
    ALPHA = "alpha"
    LUMA = "luma"
    INVERTED = "inverted"


@dataclass(frozen=True)
class SceneInstance:
    """A reusable reference to another scene (Precomp), with per-instance params."""

    instance_id: str
    source_scene_id: str
    start_frame: int = 0
    duration_frames: int | None = None       # None = source duration
    trim_in: int = 0
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("instance_id cannot be empty")
        if not self.source_scene_id:
            raise ValueError("source_scene_id cannot be empty")
        if self.start_frame < 0 or self.trim_in < 0:
            raise ValueError("start_frame and trim_in must be non-negative")
        if self.duration_frames is not None and self.duration_frames <= 0:
            raise ValueError("duration_frames must be positive when set")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id, "source_scene_id": self.source_scene_id,
            "start_frame": self.start_frame, "duration_frames": self.duration_frames,
            "trim_in": self.trim_in, "params": dict(self.params),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SceneInstance":
        return cls(
            instance_id=str(data.get("instance_id", "")),
            source_scene_id=str(data.get("source_scene_id", "")),
            start_frame=int(data.get("start_frame", 0)),
            duration_frames=(int(data["duration_frames"]) if data.get("duration_frames") is not None else None),
            trim_in=int(data.get("trim_in", 0)),
            params=dict(data.get("params", {})),
        )


@dataclass(frozen=True)
class Mask:
    """A mask applied to a target layer/element."""

    mask_id: str
    target: str                       # element_id or instance_id
    kind: MaskKind = MaskKind.ALPHA
    invert: bool = False
    feather: float = 0.0
    opacity: float = 1.0
    # For VECTOR masks: an opaque path reference resolved by the renderer.
    path_ref: str = ""

    def __post_init__(self) -> None:
        if not self.mask_id or not self.target:
            raise ValueError("mask_id and target cannot be empty")
        if self.feather < 0.0:
            raise ValueError("feather must be non-negative")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("opacity must be in [0,1]")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mask_id": self.mask_id, "target": self.target, "kind": self.kind.value,
            "invert": self.invert, "feather": self.feather, "opacity": self.opacity,
            "path_ref": self.path_ref,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Mask":
        try:
            kind = MaskKind(data.get("kind", "alpha"))
        except ValueError:
            kind = MaskKind.ALPHA
        return cls(
            mask_id=str(data.get("mask_id", "")), target=str(data.get("target", "")),
            kind=kind, invert=bool(data.get("invert", False)),
            feather=float(data.get("feather", 0.0)), opacity=float(data.get("opacity", 1.0)),
            path_ref=str(data.get("path_ref", "")),
        )


@dataclass(frozen=True)
class TrackMatte:
    """A layer that supplies another layer's alpha/luma (track matte)."""

    matte_id: str
    target: str
    source: str                       # element_id used as the matte
    mode: MatteMode = MatteMode.ALPHA

    def __post_init__(self) -> None:
        if not self.matte_id or not self.target or not self.source:
            raise ValueError("matte_id, target and source cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {"matte_id": self.matte_id, "target": self.target, "source": self.source, "mode": self.mode.value}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TrackMatte":
        try:
            mode = MatteMode(data.get("mode", "alpha"))
        except ValueError:
            mode = MatteMode.ALPHA
        return cls(matte_id=str(data.get("matte_id", "")), target=str(data.get("target", "")),
                   source=str(data.get("source", "")), mode=mode)


# ---------------------------------------------------------------------------
# scene.metadata storage helpers (no schema-version bump)
# ---------------------------------------------------------------------------

_INSTANCES_KEY = "scene_instances"
_MASKS_KEY = "scene_masks"
_MATTES_KEY = "scene_mattes"


def read_instances(metadata: Mapping[str, Any]) -> tuple[SceneInstance, ...]:
    raw = metadata.get(_INSTANCES_KEY, []) if isinstance(metadata, Mapping) else []
    return tuple(SceneInstance.from_dict(item) for item in raw if isinstance(item, Mapping))


def read_masks(metadata: Mapping[str, Any]) -> tuple[Mask, ...]:
    raw = metadata.get(_MASKS_KEY, []) if isinstance(metadata, Mapping) else []
    return tuple(Mask.from_dict(item) for item in raw if isinstance(item, Mapping))


def read_mattes(metadata: Mapping[str, Any]) -> tuple[TrackMatte, ...]:
    raw = metadata.get(_MATTES_KEY, []) if isinstance(metadata, Mapping) else []
    return tuple(TrackMatte.from_dict(item) for item in raw if isinstance(item, Mapping))


def write_composition_into(
    metadata: Mapping[str, Any] | None,
    *,
    instances: tuple[SceneInstance, ...] = (),
    masks: tuple[Mask, ...] = (),
    mattes: tuple[TrackMatte, ...] = (),
) -> dict[str, Any]:
    """Write composition references as detached, versioned metadata."""
    updated = dict(metadata or {})
    updated[_INSTANCES_KEY] = [item.to_dict() for item in instances]
    updated[_MASKS_KEY] = [item.to_dict() for item in masks]
    updated[_MATTES_KEY] = [item.to_dict() for item in mattes]
    return updated


__all__ = [
    "Mask", "MaskKind", "MatteMode", "SceneInstance", "TrackMatte",
    "read_instances", "read_mattes", "read_masks", "write_composition_into",
]
