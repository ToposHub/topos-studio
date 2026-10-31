"""MT-07: MotionBinding — one driver value fanned out to many properties.

A single motion source (a spring, an interpolated value, an expression) can
drive several properties at once — e.g. one spring drives a card's scale *and*
y-offset *and* opacity, each with its own scale/offset mapping.  ``MotionBinding``
expresses that as data so the AI/editor states intent ("this entrance drives
scale, y and opacity") rather than three parallel keyframe tracks that can
drift apart.

Bindings are pure functions of the driver value; resolution is deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class BindingTarget:
    """One (element, property) pair driven by a binding, with a linear mapping."""

    element_id: str
    property: str               # e.g. "scale_x", "y", "opacity"
    scale: float = 1.0          # output = driver * scale + offset
    offset: float = 0.0

    def __post_init__(self) -> None:
        if not self.element_id:
            raise ValueError("element_id cannot be empty")
        if not self.property:
            raise ValueError("property cannot be empty")

    @property
    def key(self) -> str:
        return f"{self.element_id}.{self.property}"


@dataclass(frozen=True)
class MotionBinding:
    """A driver source fanned out to one or more targets."""

    binding_id: str
    driver: str                 # driver key, e.g. "entrance_spring" or an expression id
    targets: tuple[BindingTarget, ...] = ()

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("binding_id cannot be empty")
        if not self.driver:
            raise ValueError("driver cannot be empty")
        keys = [t.key for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("binding targets must be unique")

    def evaluate(self, driver_value: float) -> dict[str, float]:
        """Map one driver value to {target_key: mapped_value}."""
        return {t.key: driver_value * t.scale + t.offset for t in self.targets}

    def to_dict(self) -> dict[str, object]:
        return {
            "binding_id": self.binding_id, "driver": self.driver,
            "targets": [{"element_id": t.element_id, "property": t.property,
                         "scale": t.scale, "offset": t.offset} for t in self.targets],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> "MotionBinding":
        targets = tuple(
            BindingTarget(element_id=str(t.get("element_id", "")), property=str(t.get("property", "")),
                          scale=float(t.get("scale", 1.0)), offset=float(t.get("offset", 0.0)))
            for t in data.get("targets", []) if isinstance(t, Mapping)
        )
        return cls(binding_id=str(data.get("binding_id", "")), driver=str(data.get("driver", "")), targets=targets)


__all__ = ["BindingTarget", "MotionBinding"]
