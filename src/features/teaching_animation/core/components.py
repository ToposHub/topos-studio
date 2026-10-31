"""UX-13/15: serializable component, instance and responsive-variant state.

Components are references and overrides, never copied scene documents.  The
metadata representation is additive so old ``TeachingSceneDocument`` files
round-trip unchanged.  The evaluator consumes the same helpers as preview and
export, keeping master/instance/variant semantics renderer-neutral.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
import copy


COMPONENTS_KEY = "components"
RESPONSIVE_VARIANTS_KEY = "responsive_variants"


def _json(value: Any) -> Any:
    return copy.deepcopy(value)


@dataclass(frozen=True)
class ComponentMaster:
    component_id: str
    display_name: str
    element_ids: tuple[str, ...]
    version: int = 1
    variants: Mapping[str, Mapping[str, Mapping[str, Any]]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.component_id or not self.display_name:
            raise ValueError("component_id and display_name cannot be empty")
        if self.version <= 0 or not self.element_ids:
            raise ValueError("component version and element_ids must be valid")
        if len(set(self.element_ids)) != len(self.element_ids):
            raise ValueError("component element_ids must be unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "display_name": self.display_name,
            "element_ids": list(self.element_ids),
            "version": self.version,
            "variants": _json(self.variants),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComponentMaster":
        raw_variants = data.get("variants", {})
        variants = raw_variants if isinstance(raw_variants, Mapping) else {}
        return cls(
            component_id=str(data.get("component_id", "")),
            display_name=str(data.get("display_name", "Component")),
            element_ids=tuple(str(item) for item in data.get("element_ids", ())),
            version=int(data.get("version", 1)),
            variants=_json(variants),
        )


@dataclass(frozen=True)
class ComponentInstance:
    instance_id: str
    component_id: str
    element_ids: tuple[str, ...]
    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.instance_id or not self.component_id:
            raise ValueError("instance_id and component_id cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "component_id": self.component_id,
            "element_ids": list(self.element_ids),
            "overrides": _json(self.overrides),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComponentInstance":
        raw_overrides = data.get("overrides", {})
        overrides = raw_overrides if isinstance(raw_overrides, Mapping) else {}
        return cls(
            instance_id=str(data.get("instance_id", "")),
            component_id=str(data.get("component_id", "")),
            element_ids=tuple(str(item) for item in data.get("element_ids", ())),
            overrides=_json(overrides),
        )


@dataclass(frozen=True)
class ResponsiveVariant:
    """An explicit canvas-size variant with deterministic element overrides."""

    variant_id: str
    min_width: int = 0
    max_width: int | None = None
    min_height: int = 0
    max_height: int | None = None
    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.variant_id:
            raise ValueError("variant_id cannot be empty")
        if self.min_width < 0 or self.min_height < 0:
            raise ValueError("variant minimums cannot be negative")
        if self.max_width is not None and self.max_width < self.min_width:
            raise ValueError("max_width must be >= min_width")
        if self.max_height is not None and self.max_height < self.min_height:
            raise ValueError("max_height must be >= min_height")

    def matches(self, width: int, height: int) -> bool:
        return (
            width >= self.min_width
            and (self.max_width is None or width <= self.max_width)
            and height >= self.min_height
            and (self.max_height is None or height <= self.max_height)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant_id,
            "min_width": self.min_width,
            "max_width": self.max_width,
            "min_height": self.min_height,
            "max_height": self.max_height,
            "overrides": _json(self.overrides),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ResponsiveVariant":
        raw = data.get("overrides", {})
        return cls(
            variant_id=str(data.get("variant_id", "")),
            min_width=int(data.get("min_width", 0)),
            max_width=(int(data["max_width"]) if data.get("max_width") is not None else None),
            min_height=int(data.get("min_height", 0)),
            max_height=(int(data["max_height"]) if data.get("max_height") is not None else None),
            overrides=_json(raw if isinstance(raw, Mapping) else {}),
        )


def read_component_masters(metadata: Mapping[str, Any]) -> tuple[ComponentMaster, ...]:
    state = metadata.get(COMPONENTS_KEY, {}) if isinstance(metadata, Mapping) else {}
    raw = state.get("masters", []) if isinstance(state, Mapping) else []
    return tuple(ComponentMaster.from_dict(item) for item in raw if isinstance(item, Mapping))


def read_component_instances(metadata: Mapping[str, Any]) -> tuple[ComponentInstance, ...]:
    state = metadata.get(COMPONENTS_KEY, {}) if isinstance(metadata, Mapping) else {}
    raw = state.get("instances", []) if isinstance(state, Mapping) else []
    return tuple(ComponentInstance.from_dict(item) for item in raw if isinstance(item, Mapping))


def read_active_variants(metadata: Mapping[str, Any]) -> dict[str, str]:
    state = metadata.get(COMPONENTS_KEY, {}) if isinstance(metadata, Mapping) else {}
    raw = state.get("active_variants", {}) if isinstance(state, Mapping) else {}
    return {str(key): str(value) for key, value in raw.items()} if isinstance(raw, Mapping) else {}


def write_component_state_into(
    metadata: Mapping[str, Any],
    masters: tuple[ComponentMaster, ...],
    instances: tuple[ComponentInstance, ...],
    active_variants: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    active = dict(read_active_variants(metadata))
    if active_variants is not None:
        active = dict(active_variants)
    new[COMPONENTS_KEY] = {
        "version": 1,
        "masters": [master.to_dict() for master in masters],
        "instances": [instance.to_dict() for instance in instances],
        "active_variants": active,
    }
    return new


def read_responsive_variants(metadata: Mapping[str, Any]) -> tuple[ResponsiveVariant, ...]:
    raw = metadata.get(RESPONSIVE_VARIANTS_KEY, []) if isinstance(metadata, Mapping) else []
    return tuple(ResponsiveVariant.from_dict(item) for item in raw if isinstance(item, Mapping))


def write_responsive_variants_into(
    metadata: Mapping[str, Any], variants: tuple[ResponsiveVariant, ...]
) -> dict[str, Any]:
    new = dict(metadata) if isinstance(metadata, Mapping) else {}
    new[RESPONSIVE_VARIANTS_KEY] = [variant.to_dict() for variant in variants]
    return new


def _merge_update(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        if key == "style" and isinstance(value, Mapping):
            result[key] = {**dict(result.get(key, {})), **_json(value)}
        else:
            result[key] = _json(value)
    return result


def resolve_component_updates(metadata: Mapping[str, Any], element_id: str) -> dict[str, Any]:
    """Return active master/instance updates for one rendered element."""
    masters = {master.component_id: master for master in read_component_masters(metadata)}
    active = read_active_variants(metadata)
    result: dict[str, Any] = {}
    for master in masters.values():
        variant_id = active.get(master.component_id)
        if variant_id is None:
            continue
        variant = master.variants.get(variant_id, {})
        update = variant.get(element_id)
        if isinstance(update, Mapping):
            result = _merge_update(result, update)
    for instance in read_component_instances(metadata):
        if element_id not in instance.element_ids:
            continue
        master = masters.get(instance.component_id)
        index = instance.element_ids.index(element_id)
        if master is not None and index < len(master.element_ids):
            variant_id = active.get(master.component_id)
            if variant_id is not None:
                update = master.variants.get(variant_id, {}).get(master.element_ids[index])
                if isinstance(update, Mapping):
                    result = _merge_update(result, update)
        override = instance.overrides.get(element_id)
        if isinstance(override, Mapping):
            result = _merge_update(result, override)
    return result


def resolve_responsive_updates(metadata: Mapping[str, Any], width: int, height: int,
                               element_id: str) -> dict[str, Any]:
    matches = [variant for variant in read_responsive_variants(metadata)
               if variant.matches(width, height) and element_id in variant.overrides]
    if not matches:
        return {}
    # Most specific range wins; ties are deterministic by variant id.
    selected = sorted(matches, key=lambda item: (
        item.max_width is None, item.max_height is None,
        (item.max_width or width) - item.min_width,
        (item.max_height or height) - item.min_height,
        item.variant_id,
    ))[0]
    return dict(selected.overrides[element_id])


__all__ = [
    "COMPONENTS_KEY", "RESPONSIVE_VARIANTS_KEY", "ComponentInstance",
    "ComponentMaster", "ResponsiveVariant", "read_active_variants",
    "read_component_instances", "read_component_masters", "read_responsive_variants",
    "resolve_component_updates", "resolve_responsive_updates",
    "write_component_state_into", "write_responsive_variants_into",
]
