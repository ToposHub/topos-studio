"""Versioned registry for editable Teaching Animation templates."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class TemplateCapabilities:
    """Declared preview/export capabilities for one template version."""

    preview: bool = True
    export: bool = True
    editable_fields: tuple[str, ...] = ()
    animation_properties: tuple[str, ...] = ()
    asset_fallback: str = "placeholder"


@dataclass(frozen=True)
class SceneTemplate:
    template_id: str
    version: int
    display_name: str
    default_parameters: Mapping[str, Any] = field(default_factory=dict)
    capabilities: TemplateCapabilities = field(default_factory=TemplateCapabilities)

    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("template_id cannot be empty")
        if self.version <= 0:
            raise ValueError("template version must be positive")
        if not self.display_name:
            raise ValueError("template display_name cannot be empty")

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "version": self.version,
            "display_name": self.display_name,
            "default_parameters": dict(self.default_parameters),
            "capabilities": {
                "preview": self.capabilities.preview,
                "export": self.capabilities.export,
                "editable_fields": list(self.capabilities.editable_fields),
                "animation_properties": list(self.capabilities.animation_properties),
                "asset_fallback": self.capabilities.asset_fallback,
            },
        }


class SceneTemplateRegistry:
    """Small deterministic registry with explicit duplicate handling.

    AUD-27718: 与同包 :class:`MotionGraphicGeneratorRegistry` 的线程安全
    基线对齐——增删查全部持 ``RLock``；``list_templates`` 在锁内构造快照
    元组，迭代期间并发 ``register`` 不会触发
    ``RuntimeError: dictionary changed size``。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._templates: dict[str, SceneTemplate] = {}

    def register(self, template: SceneTemplate, *, replace: bool = False) -> bool:
        with self._lock:
            if template.template_id in self._templates and not replace:
                return False
            self._templates[template.template_id] = template
            return True

    def unregister(self, template_id: str) -> bool:
        with self._lock:
            return self._templates.pop(template_id, None) is not None

    def get(self, template_id: str) -> SceneTemplate | None:
        with self._lock:
            return self._templates.get(template_id)

    def list_templates(self) -> tuple[SceneTemplate, ...]:
        with self._lock:
            return tuple(self._templates[key] for key in sorted(self._templates))

    def __contains__(self, template_id: str) -> bool:
        with self._lock:
            return template_id in self._templates


TemplateRegistry = SceneTemplateRegistry


def create_default_template_registry() -> SceneTemplateRegistry:
    registry = SceneTemplateRegistry()
    common = TemplateCapabilities(
        editable_fields=("content", "style", "transform"),
        animation_properties=("opacity", "position.x", "position.y", "scale", "rotation"),
    )
    for template_id, display_name in (
        ("title", "Title"),
        ("section_title", "Section Title"),
        ("definition_card", "Definition Card"),
        ("formula", "Formula"),
        ("arrow_step", "Arrow Step"),
    ):
        registry.register(SceneTemplate(
            template_id=template_id,
            version=1,
            display_name=display_name,
            capabilities=common,
        ))
    return registry
