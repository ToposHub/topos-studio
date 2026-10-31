"""FH-09: a reusable template distilled from a verified HTML motion layer.

The 15 built-in :class:`ExecutableTemplate`\\ s compile structured
:class:`SceneElement`\\ s — none of them produce an ``html_layer``.  A
freeform HTML page the user has approved is a different kind of reusable
artifact: its "shape" is the sanitized HTML with its current text/asset
slots and CSS tokens turned into parameters.  :class:`HtmlTemplate`
captures that shape without perturbing the structured-template machinery,
so the AI (or the user) can later re-apply it with different copy, a
different brand colour, or a swapped logo and get a fresh
:class:`FreeformHtmlMotionLayer` that still passes the sanitizer.

Promotion is a **pure transform** (``render/html_layer/template_promote.py``):
it never mutates the source scene.  The original layer stays in place as
the audited fallback (this template records its ``source_hash`` +
``provenance_layer_revision`` so a reviewer can trace any instance back to
the verified original — FH-09 "keep the original layer as a fallback
version").

The dataclass deliberately mirrors :class:`ExecutableTemplate`'s public
shape (``template_id`` / ``version`` / ``display_name`` / ``params_schema``
/ ``description`` / ``default_params`` / ``validate_params``) so the two
template kinds can flow through one AI capability surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .executable_template import ParamSpec
from .html_layer import HtmlEditSlot


@dataclass(frozen=True)
class HtmlTemplate:
    """A frozen, parameterized HTML motion layer awaiting re-application.

    ``html_template`` is the source layer's sanitized HTML with each
    editable current value replaced by a placeholder marker:

    - ``{{slot:NODE_ID}}`` — inner text of a content node
    - ``{{asset:NODE_ID}}`` — the asset id in a node's ``topos-asset://`` ref
    - ``{{token:NAME}}`` — the value of a ``--NAME`` CSS custom property

    ``apply_html_template`` substitutes the markers back out and re-sanitizes
    before rebuilding a layer, so an attacker-controlled parameter value can
    never bypass the allowlist.
    """

    template_id: str
    display_name: str
    html_template: str
    params_schema: tuple[ParamSpec, ...]
    viewport_width: int
    viewport_height: int
    version: int = 1
    description: str = ""
    fps_num: int = 24
    fps_den: int = 1
    duration_frames: int = 240
    bound_asset_ids: tuple[str, ...] = ()
    # Preserved verbatim so re-applied layers keep their authored edit
    # contract (the inspector / HtmlPatch surface).
    editable_slots: tuple[HtmlEditSlot, ...] = ()
    editable_tokens: tuple[str, ...] = ()
    sanitizer_version: str = ""
    # Browser profile/CSP flags needed when the template is instantiated from
    # the Effects panel.  Empty keeps legacy templates on the conservative
    # sanitizer path; Motion Graphic templates carry their freeform profile.
    metadata: Mapping[str, Any] = field(default_factory=dict)
    # Audit trail back to the verified source layer (FH-09 fallback).
    source_hash: str = ""
    provenance_layer_revision: int = 1

    def __post_init__(self) -> None:
        if not self.template_id:
            raise ValueError("template_id cannot be empty")
        if not self.display_name:
            raise ValueError("display_name cannot be empty")
        if self.version <= 0:
            raise ValueError("template version must be positive")
        names = [p.name for p in self.params_schema]
        if len(names) != len(set(names)):
            raise ValueError("param names must be unique")

    def default_params(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params_schema}

    def validate_params(self, params: Mapping[str, Any]) -> list[str]:
        """Return human-readable validation errors (empty == valid).

        Mirrors :meth:`ExecutableTemplate.validate_params` so html templates
        drop into the same AI validation path.
        """
        errors: list[str] = []
        known = {p.name for p in self.params_schema}
        for spec in self.params_schema:
            if spec.required and params.get(spec.name) is None:
                errors.append(f"missing required parameter: {spec.name}")
        for key, value in params.items():
            if key not in known:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "display_name": self.display_name,
            "html_template": self.html_template,
            "params_schema": [p.to_dict() for p in self.params_schema],
            "viewport_width": self.viewport_width,
            "viewport_height": self.viewport_height,
            "version": self.version,
            "description": self.description,
            "fps_num": self.fps_num,
            "fps_den": self.fps_den,
            "duration_frames": self.duration_frames,
            "bound_asset_ids": list(self.bound_asset_ids),
            "editable_slots": [
                {"node_id": s.node_id, "edit_mode": s.edit_mode, "kind": s.kind, "label": s.label}
                for s in self.editable_slots
            ],
            "editable_tokens": list(self.editable_tokens),
            "sanitizer_version": self.sanitizer_version,
            "metadata": dict(self.metadata),
            "source_hash": self.source_hash,
            "provenance_layer_revision": self.provenance_layer_revision,
            "kind": "html",
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HtmlTemplate":
        slots = tuple(
            HtmlEditSlot(
                node_id=str(s["node_id"]),
                edit_mode=str(s.get("edit_mode", "content")),
                kind=str(s.get("kind", "text")),
                label=str(s.get("label", "")),
            )
            for s in data.get("editable_slots", [])
        )
        return cls(
            template_id=str(data["template_id"]),
            display_name=str(data["display_name"]),
            html_template=str(data["html_template"]),
            params_schema=tuple(
                ParamSpec(
                    name=str(p["name"]),
                    type=str(p["type"]),
                    default=p.get("default"),
                    required=bool(p.get("required", False)),
                    enum=tuple(p.get("enum", ())),
                    description=str(p.get("description", "")),
                )
                for p in data.get("params_schema", [])
            ),
            viewport_width=int(data["viewport_width"]),
            viewport_height=int(data["viewport_height"]),
            version=int(data.get("version", 1)),
            description=str(data.get("description", "")),
            fps_num=int(data.get("fps_num", 24)),
            fps_den=int(data.get("fps_den", 1)),
            duration_frames=int(data.get("duration_frames", 240)),
            bound_asset_ids=tuple(data.get("bound_asset_ids", ())),
            editable_slots=slots,
            editable_tokens=tuple(data.get("editable_tokens", ())),
            sanitizer_version=str(data.get("sanitizer_version", "")),
            metadata=dict(data.get("metadata", {})),
            source_hash=str(data.get("source_hash", "")),
            provenance_layer_revision=int(data.get("provenance_layer_revision", 1)),
        )


# ``FreeformHtmlMotionLayer`` is imported above for type completeness; the
# registry below stores templates, not layers (layers are produced by
# ``apply_html_template`` in ``render/html_layer/template_promote.py``).


class HtmlTemplateRegistry:
    """Versioned in-memory registry for promoted HTML templates.

    Mirrors :class:`ExecutableTemplateRegistry`: keyed by
    ``(template_id, version)``, tracks the latest version per id, and exposes
    ``schema_for_ai`` so promoted templates reach the AI capability catalog
    alongside the structured builtins.  Like the structured registry it is
    rebuilt per session (no disk persistence yet); a later batch can add a
    project-scoped store.
    """

    def __init__(self) -> None:
        self._templates: dict[tuple[str, int], HtmlTemplate] = {}
        self._latest: dict[str, int] = {}

    def register(self, template: HtmlTemplate) -> None:
        key = (template.template_id, template.version)
        self._templates[key] = template
        if template.version >= self._latest.get(template.template_id, 0):
            self._latest[template.template_id] = template.version

    def get(self, template_id: str, version: int | None = None) -> HtmlTemplate | None:
        if version is None:
            version = self._latest.get(template_id)
            if version is None:
                return None
        return self._templates.get((template_id, version))

    def list_templates(self) -> tuple[HtmlTemplate, ...]:
        """Return the latest version of each registered template."""
        out: list[HtmlTemplate] = []
        for template_id, version in self._latest.items():
            tpl = self._templates.get((template_id, version))
            if tpl is not None:
                out.append(tpl)
        return tuple(out)

    def schema_for_ai(self, template_id: str) -> dict[str, Any] | None:
        tpl = self.get(template_id)
        if tpl is None:
            return None
        return {
            "template_id": tpl.template_id,
            "version": tpl.version,
            "display_name": tpl.display_name,
            "description": tpl.description,
            "kind": "html",
            "params": [p.to_dict() for p in tpl.params_schema],
        }


__all__ = [
    "HtmlTemplate",
    "HtmlTemplateRegistry",
]
