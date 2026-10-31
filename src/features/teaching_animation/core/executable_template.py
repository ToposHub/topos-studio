"""TM-01: ExecutableTemplate — a parameterized scene compiler.

An ``ExecutableTemplate`` turns a JSON parameter object into a complete
``TeachingSceneDocument`` deterministically.  This closes the loop the old
metadata-only registry left open: a template is now "instantiable, replaceable,
migratable" rather than a description plus default values.

An instance stores only ``template_id + version + params``; the scene is
compiled on demand, so editing params or upgrading the template re-generates
the scene without duplicating JSON into every Clip/Node.  A migration hook
adapts an old instance's params to a new template version.

Templates use the Auto Layout resolver + design tokens + motion presets, so an
AI "apply the steps template with 4 cards, brand blue" is a single parameterized
command rather than a hand-built scene.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping
import uuid

from .layout import Rect
from .model import TeachingSceneDocument
from .design_tokens import DesignTokenSet, default_brand_tokens
from .template_registry import TemplateCapabilities


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True)
class TemplateBuildContext:
    """Immutable inputs handed to a template's build function."""

    canvas_width: int
    canvas_height: int
    fps_num: int = 24
    fps_den: int = 1
    duration_frames: int = 240
    tokens: DesignTokenSet = field(default_factory=default_brand_tokens)

    @property
    def canvas(self) -> Rect:
        return Rect(x=0.0, y=0.0, width=float(self.canvas_width), height=float(self.canvas_height))


TemplateBuildFn = Callable[[Mapping[str, Any], TemplateBuildContext], TeachingSceneDocument]


@dataclass(frozen=True)
class ParamSpec:
    """One declared template parameter for AI schema export + validation."""

    name: str
    type: str                       # string | number | integer | boolean | color | enum | asset_id
    default: Any = None
    required: bool = False
    enum: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "type": self.type, "default": self.default,
                                "required": self.required, "description": self.description}
        if self.enum:
            data["enum"] = list(self.enum)
        return data


@dataclass(frozen=True)
class ExecutableTemplate:
    """A versioned, parameterized scene compiler."""

    template_id: str
    version: int
    display_name: str
    params_schema: tuple[ParamSpec, ...]
    build: TemplateBuildFn = field(repr=False)
    capabilities: TemplateCapabilities = field(default_factory=TemplateCapabilities)
    migrate: Callable[[int, Mapping[str, Any]], Mapping[str, Any]] | None = field(default=None, repr=False)
    description: str = ""

    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("template_id cannot be empty")
        if self.version <= 0:
            raise ValueError("template version must be positive")
        names = [p.name for p in self.params_schema]
        if len(names) != len(set(names)):
            raise ValueError("param names must be unique")

    def default_params(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params_schema}

    def validate_params(self, params: Mapping[str, Any]) -> list[str]:
        """Return a list of human-readable validation errors (empty == valid)."""
        errors: list[str] = []
        known = {p.name for p in self.params_schema}
        for spec in self.params_schema:
            if spec.required and params.get(spec.name) is None:
                errors.append(f"missing required parameter: {spec.name}")
        for key, value in params.items():
            if key not in known:
                # Unknown params are preserved (round-trip) but reported.
                errors.append(f"unknown parameter: {key}")
                continue
            spec = next(p for p in self.params_schema if p.name == key)
            if value is None:
                continue
            if spec.enum and value not in spec.enum:
                errors.append(f"{key} must be one of {list(spec.enum)}")
            elif spec.type == "integer" and not (isinstance(value, int) and not isinstance(value, bool)):
                errors.append(f"{key} must be an integer")
            elif spec.type == "number" and not (isinstance(value, (int, float)) and not isinstance(value, bool)):
                errors.append(f"{key} must be a number")
            elif spec.type == "boolean" and not isinstance(value, bool):
                errors.append(f"{key} must be a boolean")
            elif spec.type == "string" and not isinstance(value, str):
                errors.append(f"{key} must be a string")
        return errors

    def instantiate(
        self,
        params: Mapping[str, Any] | None = None,
        context: TemplateBuildContext | None = None,
    ) -> "TemplateInstance":
        merged = {**self.default_params(), **dict(params or {})}
        errors = self.validate_params(merged)
        if errors:
            raise ValueError(f"invalid params for template {self.template_id}: {'; '.join(errors)}")
        return TemplateInstance(
            instance_id=_new_id("tplinst"),
            template_id=self.template_id, version=self.version,
            params=merged,
            context=context or TemplateBuildContext(canvas_width=1920, canvas_height=1080),
        )

    def compile(self, instance: "TemplateInstance", context: TemplateBuildContext | None = None) -> TeachingSceneDocument:
        build_context = context or instance.context
        params = instance.params
        if instance.version != self.version and self.migrate is not None:
            params = dict(self.migrate(instance.version, params))
        return self.build(params, build_context)


@dataclass(frozen=True)
class TemplateInstance:
    """Serializable reference to a compiled template: id + version + params."""

    instance_id: str
    template_id: str
    version: int
    params: Mapping[str, Any]
    context: TemplateBuildContext

    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("template_id cannot be empty")
        if self.version <= 0:
            raise ValueError("instance version must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "template_id": self.template_id,
            "version": self.version,
            "params": dict(self.params),
            "context": {
                "canvas_width": self.context.canvas_width,
                "canvas_height": self.context.canvas_height,
                "fps_num": self.context.fps_num,
                "fps_den": self.context.fps_den,
                "duration_frames": self.context.duration_frames,
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TemplateInstance":
        ctx_data = data.get("context", {})
        context = TemplateBuildContext(
            canvas_width=int(ctx_data.get("canvas_width", 1920)),
            canvas_height=int(ctx_data.get("canvas_height", 1080)),
            fps_num=int(ctx_data.get("fps_num", 24)),
            fps_den=int(ctx_data.get("fps_den", 1)),
            duration_frames=int(ctx_data.get("duration_frames", 240)),
        )
        return cls(
            instance_id=str(data.get("instance_id", _new_id("tplinst"))),
            template_id=str(data.get("template_id", "")),
            version=int(data.get("version", 1)),
            params=dict(data.get("params", {})),
            context=context,
        )


class ExecutableTemplateRegistry:
    """Versioned registry of executable templates."""

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], ExecutableTemplate] = {}
        self._latest: dict[str, int] = {}

    def register(self, template: ExecutableTemplate) -> None:
        key = (template.template_id, template.version)
        self._templates[key] = template
        latest = self._latest.get(template.template_id, 0)
        if template.version > latest:
            self._latest[template.template_id] = template.version

    def get(self, template_id: str, version: int | None = None) -> ExecutableTemplate | None:
        if version is not None:
            return self._templates.get((template_id, version))
        latest = self._latest.get(template_id)
        if latest is None:
            return None
        return self._templates.get((template_id, latest))

    def list_templates(self) -> tuple[ExecutableTemplate, ...]:
        return tuple(self._templates[(tid, self._latest[tid])] for tid in sorted(self._latest))

    def instantiate(
        self, template_id: str, params: Mapping[str, Any] | None = None,
        *, version: int | None = None, context: TemplateBuildContext | None = None,
    ) -> TemplateInstance:
        template = self.get(template_id, version)
        if template is None:
            raise KeyError(f"unknown template: {template_id}@{version}")
        return template.instantiate(params, context)

    def compile(self, instance: TemplateInstance, context: TemplateBuildContext | None = None) -> TeachingSceneDocument:
        # Prefer the instance's version, falling back to latest so an old
        # instance still compiles after an upgrade (migration handles params).
        template = self.get(instance.template_id, instance.version) or self.get(instance.template_id)
        if template is None:
            raise KeyError(f"unknown template: {instance.template_id}")
        return template.compile(instance, context)

    def schema_for_ai(self, template_id: str) -> dict[str, Any]:
        """Export a template's parameter schema for AI command generation (TM-06)."""
        template = self.get(template_id)
        if template is None:
            raise KeyError(f"unknown template: {template_id}")
        return {
            "template_id": template.template_id,
            "version": template.version,
            "display_name": template.display_name,
            "description": template.description,
            "params": [p.to_dict() for p in template.params_schema],
            "capabilities": {
                "preview": template.capabilities.preview,
                "export": template.capabilities.export,
                "editable_fields": list(template.capabilities.editable_fields),
            },
        }


__all__ = [
    "ExecutableTemplate",
    "ExecutableTemplateRegistry",
    "ParamSpec",
    "TemplateBuildContext",
    "TemplateBuildFn",
    "TemplateInstance",
]
