"""W6 professional capability data: particles and explicit colour pipeline."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


@dataclass(frozen=True)
class SceneColorPipeline:
    """Scene working-space contract; renderers must report any downgrade."""

    working_space: str = "srgb"
    bit_depth: int = 8
    tone_map: str = "none"
    alpha_mode: str = "straight"

    def __post_init__(self) -> None:
        if self.working_space not in {"srgb", "linear", "rec709", "rec2020", "acescg"}:
            raise ValueError("unsupported scene working space")
        if self.bit_depth not in {8, 10, 12, 16, 32}:
            raise ValueError("bit_depth must be one of 8, 10, 12, 16 or 32")
        if self.tone_map not in {"none", "reinhard", "aces"}:
            raise ValueError("unsupported tone map")
        if self.alpha_mode not in {"straight", "premult"}:
            raise ValueError("alpha_mode must be straight or premult")

    @property
    def is_hdr(self) -> bool:
        return self.bit_depth > 8 or self.working_space in {"rec2020", "acescg"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "working_space": self.working_space,
            "bit_depth": self.bit_depth,
            "tone_map": self.tone_map,
            "alpha_mode": self.alpha_mode,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> "SceneColorPipeline":
        values = data or {}
        try:
            return cls(
                working_space=str(values.get("working_space", "srgb")),
                bit_depth=int(values.get("bit_depth", 8)),
                tone_map=str(values.get("tone_map", "none")),
                alpha_mode=str(values.get("alpha_mode", "straight")),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass(frozen=True)
class ParticleEmitter:
    """A deterministic, bounded particle emitter for safe motion graphics."""

    emitter_id: str
    count: int = 32
    seed: int = 1
    origin: tuple[float, float] = (0.5, 0.5)
    spread: float = 0.25
    speed: float = 0.25
    gravity: float = 0.0
    size: float = 4.0
    color: str = "#ffffff"
    opacity: float = 1.0
    lifetime_frames: int = 48

    def __post_init__(self) -> None:
        if not self.emitter_id:
            raise ValueError("emitter_id cannot be empty")
        if not 0 <= self.count <= 2000:
            raise ValueError("particle count must be between 0 and 2000")
        if self.spread < 0.0 or self.speed < 0.0 or self.size < 0.0:
            raise ValueError("particle spread, speed and size must be non-negative")
        if not 0.0 <= self.opacity <= 1.0:
            raise ValueError("particle opacity must be in [0,1]")
        if self.lifetime_frames <= 0:
            raise ValueError("particle lifetime must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "emitter_id": self.emitter_id, "count": self.count, "seed": self.seed,
            "origin": list(self.origin), "spread": self.spread, "speed": self.speed,
            "gravity": self.gravity, "size": self.size, "color": self.color,
            "opacity": self.opacity, "lifetime_frames": self.lifetime_frames,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ParticleEmitter":
        origin = data.get("origin", (0.5, 0.5))
        return cls(
            emitter_id=str(data.get("emitter_id", "")), count=int(data.get("count", 32)),
            seed=int(data.get("seed", 1)),
            origin=(float(origin[0]), float(origin[1])) if isinstance(origin, (list, tuple)) and len(origin) == 2 else (0.5, 0.5),
            spread=float(data.get("spread", 0.25)), speed=float(data.get("speed", 0.25)),
            gravity=float(data.get("gravity", 0.0)), size=float(data.get("size", 4.0)),
            color=str(data.get("color", "#ffffff")), opacity=float(data.get("opacity", 1.0)),
            lifetime_frames=int(data.get("lifetime_frames", 48)),
        )


def particle_positions(emitter: ParticleEmitter, frame: int) -> tuple[tuple[float, float, float], ...]:
    """Return stable (x, y, alpha) positions; no wall clock or mutable RNG."""
    if frame < 0:
        raise ValueError("frame must be non-negative")
    positions: list[tuple[float, float, float]] = []
    for index in range(emitter.count):
        state = (emitter.seed * 1664525 + index * 1013904223) & 0xFFFFFFFF
        angle = ((state % 3600) / 3600.0) * math.tau
        radial = (((state >> 8) & 0xFFFF) / 65535.0) * emitter.spread
        phase = frame / max(1, emitter.lifetime_frames)
        x = emitter.origin[0] + math.cos(angle) * radial + math.cos(angle) * emitter.speed * phase
        y = emitter.origin[1] + math.sin(angle) * radial + math.sin(angle) * emitter.speed * phase + emitter.gravity * phase * phase
        age = (frame + index * 3) % emitter.lifetime_frames
        alpha = emitter.opacity * max(0.0, 1.0 - age / emitter.lifetime_frames)
        positions.append((x, y, alpha))
    return tuple(positions)


_COLOR_PIPELINE_KEY = "color_pipeline"
_PARTICLE_KEY = "particle_emitters"


def read_scene_color_pipeline(metadata: Mapping[str, Any] | None) -> SceneColorPipeline:
    raw = metadata.get(_COLOR_PIPELINE_KEY, {}) if isinstance(metadata, Mapping) else {}
    return SceneColorPipeline.from_dict(raw if isinstance(raw, Mapping) else {})


def write_scene_color_pipeline_into(metadata: Mapping[str, Any] | None, pipeline: SceneColorPipeline) -> dict[str, Any]:
    updated = dict(metadata or {})
    updated[_COLOR_PIPELINE_KEY] = pipeline.to_dict()
    return updated


def read_particle_emitters(metadata: Mapping[str, Any] | None) -> tuple[ParticleEmitter, ...]:
    raw = metadata.get(_PARTICLE_KEY, ()) if isinstance(metadata, Mapping) else ()
    emitters: list[ParticleEmitter] = []
    for item in raw if isinstance(raw, (list, tuple)) else ():
        if not isinstance(item, Mapping):
            continue
        try:
            emitters.append(ParticleEmitter.from_dict(item))
        except (TypeError, ValueError, IndexError):
            continue
    return tuple(emitters)


def write_particle_emitters_into(metadata: Mapping[str, Any] | None, emitters: tuple[ParticleEmitter, ...]) -> dict[str, Any]:
    updated = dict(metadata or {})
    updated[_PARTICLE_KEY] = [item.to_dict() for item in emitters]
    return updated


__all__ = [
    "ParticleEmitter", "SceneColorPipeline", "particle_positions",
    "read_particle_emitters", "read_scene_color_pipeline",
    "write_particle_emitters_into", "write_scene_color_pipeline_into",
]
