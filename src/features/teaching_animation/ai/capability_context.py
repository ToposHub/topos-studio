"""TM-06: AI capability context provider.

The plan calls for the prompt to *not* hand-maintain a redundant list of
available templates, commands, motion presets, effects and error codes.
Instead the available surface should be **derived from the registries** so
that retiring a capability automatically stops the model from proposing it,
and a stale scene gets a migration / diagnostic instead of a silent failure.

This module produces a single, serialisable :class:`AICapabilityManifest`
from the live registries:

* templates      <- :class:`ExecutableTemplateRegistry` (one schema each)
* motion presets <- :class:`MotionPresetRegistry` (id + version + params)
* commands       <- the high-level TM-02 vocabulary (static, derives from the
                    command classes so it stays in sync with what the
                    :class:`SceneCommandStack` can actually execute)
* error codes    <- the canonical :class:`RenderErrorCode` set + quality-gate
                    codes, each with a short human-readable meaning

Two renderings are provided: ``to_dict()`` for structured tools (JSON schema
constraints, function-calling) and ``to_prompt_text()`` for a compact,
deterministic prompt section.  Both are pure functions of the registries, so
the output is stable for a given build and can participate in the cache key.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..core.contracts import Capability, RenderErrorCode
from ..core.executable_template import ExecutableTemplateRegistry
from ..motion.presets import MotionPresetRegistry


CAPABILITY_CONTEXT_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Command vocabulary (TM-02 high-level + core scene commands)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CommandParam:
    """One declared parameter of an AI command."""

    name: str
    type: str                       # string | number | integer | boolean | color | enum | asset_id | list | object
    required: bool = False
    description: str = ""
    enum: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "type": self.type,
                                "required": self.required, "description": self.description}
        if self.enum:
            data["enum"] = list(self.enum)
        return data


@dataclass(frozen=True)
class CommandDescriptor:
    """Static description of one AI command the model may emit."""

    name: str
    category: str                   # scene | motion | template | asset | brand | timeline
    params: tuple[CommandParam, ...] = ()
    description: str = ""
    reversible: bool = True         # every command maps onto the undoable stack

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "description": self.description,
            "reversible": self.reversible,
            "params": [p.to_dict() for p in self.params],
        }


# The high-level TM-02 vocabulary.  Every descriptor below MUST mirror the
# argument keys actually read by the compiler in ai/commands.py (base 4 names)
# and ai/command_catalog.py (extended names) — the compiler is the single
# source of truth, and a parameter name that only exists here produces
# proposals that can never compile.  tests/teaching_animation/
# test_fix_wd12g70_aud.py cross-checks names, required flags and compilability.
# Commands without a compiler (set_beat_timing / set_transition /
# set_stagger_interval were removed in the AUD-26702 fix) must not be
# advertised here; add the compiler first, then the descriptor.
DEFAULT_COMMANDS: tuple[CommandDescriptor, ...] = (
    CommandDescriptor(
        "apply_template", "template",
        (CommandParam("template_id", "string", required=True, description="A template_id from the manifest."),
         CommandParam("params", "object", required=False, description="JSON object of template parameters.")),
        "Instantiate an executable template into the scene.",
    ),
    CommandDescriptor(
        "set_brand_theme", "brand",
        (CommandParam("theme", "object", required=True,
                      description="BrandTheme JSON object: theme_id, palette, type_scale, spacing, motion, safe_area, source."),),
        "Overlay brand tokens (palette/type/motion); locked elements are preserved.",
    ),
    CommandDescriptor(
        "apply_motion_preset", "motion",
        (CommandParam("element_id", "string", required=True, description="Element id to animate."),
         CommandParam("preset_id", "string", required=True, description="A preset id from the manifest."),
         CommandParam("param_name", "string", required=True, description="Animated property (opacity/x/y/scale/rotation)."),
         CommandParam("start_frame", "integer", required=False),
         CommandParam("end_frame", "integer", required=False),
         CommandParam("params", "object", required=False, description="JSON object of preset parameters.")),
        "Bind a parameterized motion preset to one property of one element.",
    ),
    CommandDescriptor(
        "stagger_elements", "motion",
        (CommandParam("element_ids", "list", required=True, description="Ordered element ids."),
         CommandParam("preset_id", "string", required=True),
         CommandParam("param_name", "string", required=True, description="Animated property (opacity/x/y/scale/rotation)."),
         CommandParam("interval", "integer", required=False, description="Frames between each target (0 = simultaneous)."),
         CommandParam("duration", "integer", required=False, description="Frames each target animates for."),
         CommandParam("delay", "integer", required=False),
         CommandParam("anchor_frame", "integer", required=False),
         CommandParam("params", "object", required=False, description="JSON object of preset parameters.")),
        "Apply a preset to several elements with a constant inter-target delay.",
    ),
    CommandDescriptor(
        "replace_asset_slot", "asset",
        (CommandParam("element_id", "string", required=True),
         CommandParam("asset", "object", required=True,
                      description="SceneAssetRef JSON object: asset_id, kind, path, fallback, metadata.")),
        "Bind a project asset to an element's asset slot.",
    ),
    # Core scene commands (re-exposed so the model can edit structure too).
    CommandDescriptor(
        "add_element", "scene",
        (CommandParam("element", "object", required=True, description="Element JSON object (id, type, transform, ...)."),),
        "Add a single scene element.",
    ),
    CommandDescriptor(
        "update_element", "scene",
        (CommandParam("element_id", "string", required=True),
         CommandParam("updates", "object", required=True, description="JSON object of field updates.")),
        "Update transform/style/content of one element.",
    ),
    CommandDescriptor(
        "set_duration", "timeline",
        (CommandParam("duration_frames", "integer", required=True),),
        "Set the total scene duration in frames.",
    ),
)


# ---------------------------------------------------------------------------
# Error code catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorCodeDoc:
    """A documented failure code the model should expect and reason about."""

    code: str
    meaning: str
    when_raised: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "meaning": self.meaning, "when_raised": self.when_raised}


def _default_error_codes() -> tuple[ErrorCodeDoc, ...]:
    """Canonical codes reused from :class:`RenderErrorCode` + quality gates."""
    render_codes = {
        RenderErrorCode.CANCELLED: ("Render was cancelled", "User cancel, timeout, or shutdown."),
        RenderErrorCode.INVALID_SCENE: ("Scene failed validation",
                                        "Schema/cycle/range violation detected before render."),
        RenderErrorCode.MISSING_ASSET: ("Referenced asset could not be resolved",
                                        "Asset id missing from project or provider decode failed."),
        RenderErrorCode.MISSING_MODULE: ("A required provider/renderer module is absent",
                                         "Lottie/Rive/GPU runtime not installed in this build."),
        RenderErrorCode.UNSUPPORTED_BACKEND: ("Requested backend is not available",
                                              "Capability (html_renderer/gpu_effect/...) unsupported here."),
        RenderErrorCode.RENDER_FAILED: ("Renderer raised an unrecoverable error",
                                        "Unexpected exception inside the render pass."),
    }
    docs = tuple(
        ErrorCodeDoc(code=code.value, meaning=meaning, when_raised=when)
        for code, (meaning, when) in render_codes.items()
    )
    # Quality-gate / capability codes that supplement the render codes.
    quality = (
        ErrorCodeDoc("font_fallback", "A glyph is not covered by the bound font set.",
                     "FontRegistry coverage check; project may render with a fallback face."),
        ErrorCodeDoc("overshoot_out_of_range", "A spring overshoots beyond the allowed envelope.",
                     "Motion quality gate on first/middle/last/marker frames."),
        ErrorCodeDoc("cache_mismatch", "preview and export diverge on a sampled frame.",
                     "Preview/export consistency gate."),
        ErrorCodeDoc("css_budget_exceeded", "HTML/CSS complexity budget exceeded.",
                     "HTML complexity budget enforcer (FH-07)."),
    )
    return docs + quality


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AICapabilityManifest:
    """The full, derived capability surface offered to the model."""

    schema_version: int
    templates: tuple[dict[str, Any], ...]
    presets: tuple[dict[str, Any], ...]
    commands: tuple[dict[str, Any], ...]
    error_codes: tuple[dict[str, str], ...]
    capabilities: tuple[str, ...] = ()      # Capability values supported in this build

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "templates": list(self.templates),
            "motion_presets": list(self.presets),
            "commands": list(self.commands),
            "error_codes": list(self.error_codes),
            "capabilities": list(self.capabilities),
        }

    def template_ids(self) -> tuple[str, ...]:
        return tuple(t["template_id"] for t in self.templates)

    def preset_ids(self) -> tuple[str, ...]:
        return tuple(p["preset_id"] for p in self.presets)

    def command_names(self) -> tuple[str, ...]:
        return tuple(c["name"] for c in self.commands)

    def to_prompt_text(self) -> str:
        """Compact, deterministic text section for an LLM system prompt.

        Rendering capability lists from the live registries means retiring a
        template/preset/command automatically removes it from the prompt — no
        hand-maintained lists to drift out of sync (TM-06 acceptance).
        """
        lines: list[str] = [
            "# Topos Motion Graphic — available capabilities",
            f"manifest_schema_version: {self.schema_version}",
            "",
            "## Templates (apply_template):",
        ]
        for t in self.templates:
            param_names = ", ".join(p["name"] for p in t.get("params", []))
            lines.append(f"- {t['template_id']} (v{t['version']}): {t.get('description', '')} "
                         f"[params: {param_names}]")
        lines.append("")
        lines.append("## Motion presets (apply_motion_preset):")
        for p in self.presets:
            lines.append(f"- {p['preset_id']} (v{p['version']}): {p.get('description', '')}")
        lines.append("")
        lines.append("## Commands:")
        for c in self.commands:
            lines.append(f"- {c['name']} [{c['category']}]: {c.get('description', '')}")
        lines.append("")
        lines.append("## Capability support (this build):")
        lines.append(", ".join(self.capabilities) if self.capabilities else "(svg fallback only)")
        lines.append("")
        lines.append("## Error codes you may receive:")
        for e in self.error_codes:
            lines.append(f"- {e['code']}: {e['meaning']}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_capability_manifest(
    template_registry: ExecutableTemplateRegistry,
    preset_registry: MotionPresetRegistry,
    *,
    commands: Iterable[CommandDescriptor] = DEFAULT_COMMANDS,
    error_codes: Iterable[ErrorCodeDoc] | None = None,
    capabilities: Iterable[str] = (Capability.SVG_RASTER.value,),
) -> AICapabilityManifest:
    """Derive a manifest from the live registries.

    The registries are passed in (dependency injection) so callers can supply a
    build-specific or test fixture registry; the default command/error tables
    are module-level constants so the manifest is reproducible for a given code
    revision.
    """
    templates = tuple(template_registry.schema_for_ai(t.template_id)
                      for t in template_registry.list_templates())

    presets: list[dict[str, Any]] = []
    for preset_id in preset_registry.ids():
        preset = preset_registry.get(preset_id)
        assert preset is not None  # ids() only returns registered keys
        presets.append({
            "preset_id": preset.preset_id,
            "version": preset.version,
            "description": preset.description,
            "default_params": dict(preset.default_params),
        })

    command_dicts = tuple(c.to_dict() for c in commands)
    code_docs = tuple(c.to_dict() for c in (error_codes if error_codes is not None else _default_error_codes()))

    return AICapabilityManifest(
        schema_version=CAPABILITY_CONTEXT_SCHEMA_VERSION,
        templates=templates,
        presets=tuple(presets),
        commands=command_dicts,
        error_codes=code_docs,
        capabilities=tuple(dict.fromkeys(capabilities)),  # dedupe, preserve order
    )


def default_capability_manifest() -> AICapabilityManifest:
    """Manifest built from the project's default registries (TM-06 convenience)."""
    # Local import avoids a hard dependency cycle at module import time.
    from ..core.builtin_templates import create_builtin_template_registry
    from ..motion.presets import create_default_preset_registry
    return build_capability_manifest(
        create_builtin_template_registry(),
        create_default_preset_registry(),
    )


__all__ = [
    "AICapabilityManifest",
    "CAPABILITY_CONTEXT_SCHEMA_VERSION",
    "CommandDescriptor",
    "CommandParam",
    "DEFAULT_COMMANDS",
    "ErrorCodeDoc",
    "build_capability_manifest",
    "default_capability_manifest",
]
