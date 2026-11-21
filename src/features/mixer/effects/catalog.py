"""Versioned catalog for parameter-panel UIs, without DSP capabilities."""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import json
from pathlib import Path
from typing import Any, Mapping
import uuid

from PyQt6.QtWidgets import QWidget


@dataclass(frozen=True)
class EffectDescriptor:
    effect_id: str
    name_zh: str
    name_en: str
    group: str
    panel_module: str
    panel_class: str
    scope: tuple[str, ...]
    version: str = "1.0.0-ui"
    backend_status: str = "not_connected"
    supports_preview: bool = False
    supports_export: bool = False
    parameter_schema: Mapping[str, Any] = field(default_factory=dict)
    compatibility: str = "UI state only; production DSP mapping and migration required"

    @property
    def name(self) -> str:
        return self.name_zh


@dataclass
class EffectInstance:
    instance_id: str
    effect_id: str
    owner_id: str
    enabled: bool = True
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, effect_id: str, owner_id: str) -> EffectInstance:
        get_effect(effect_id)
        if not owner_id:
            raise ValueError("Effect owner_id must not be empty")
        return cls(uuid.uuid4().hex, effect_id, owner_id)


def _load_catalog() -> tuple[EffectDescriptor, ...]:
    rows = json.loads(Path(__file__).with_name("source_manifest.json").read_text())
    schemas_path = Path(__file__).with_name("parameter_schemas.json")
    schemas = json.loads(schemas_path.read_text()) if schemas_path.exists() else {}
    return tuple(EffectDescriptor(
        effect_id=row["effect_id"], name_zh=row["name_zh"], name_en=row["name_en"],
        group=row["group"], panel_module=row["panel_module"], panel_class=row["panel_class"],
        scope=tuple(row["scope"]), parameter_schema=schemas.get(row["effect_id"], {
            "type": "object", "additionalProperties": True,
            "description": "Local UI parameter state; no DSP implementation is registered",
        }),
    ) for row in rows)


EFFECT_CATALOG = _load_catalog()
_BY_ID = {entry.effect_id: entry for entry in EFFECT_CATALOG}


def effect_catalog() -> tuple[EffectDescriptor, ...]:
    return EFFECT_CATALOG


def get_effect(effect_id: str) -> EffectDescriptor:
    return _BY_ID[effect_id]


def create_panel(effect_id: str, owner_name: str = "", parent: QWidget | None = None) -> QWidget:
    """Import only a catalog-owned module and construct its QWidget, never its main."""
    descriptor = get_effect(effect_id)
    module = import_module(descriptor.panel_module)
    panel_class = getattr(module, descriptor.panel_class)
    panel = panel_class(owner_name or "Mixer", parent=parent)
    if not isinstance(panel, QWidget):
        raise TypeError(f"{descriptor.panel_class} is not a QWidget")
    from .panel_style import style_reference_panel
    style_reference_panel(panel)
    return panel


def create_effect_panel(effect_id: str, owner_name: str = "", parent: QWidget | None = None) -> QWidget:
    """Public spelling for callers embedding a panel into a dock instead of a window."""
    return create_panel(effect_id, owner_name, parent)
