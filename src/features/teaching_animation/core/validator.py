"""Schema, range, reference and animation validation for scene JSON."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from src.shared.keyframe import InterpolationType

from .constraints import HORIZONTAL_CONSTRAINTS, VERTICAL_CONSTRAINTS
from .model import (
    LAYOUT_ALIGN,
    LAYOUT_JUSTIFY,
    LAYOUT_MODES,
    SIZE_POLICIES,
    SCENE_SCHEMA_VERSION,
    SUPPORTED_ELEMENT_TYPES,
)
from ..motion.expression import ExpressionError, validate_expression


# Exported JSON Schema contract for tooling and AI dry-runs.  Runtime
# validation below adds cross-reference and frame-range checks that plain
# JSON Schema cannot express without duplicating scene state.
SCENE_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos://teaching-animation/scene/3",
    "type": "object",
    "additionalProperties": True,
    "required": [
        "scene_id", "schema_version", "name", "width", "height",
        "fps_num", "fps_den", "duration_frames", "background",
        "elements", "animations", "markers", "asset_refs",
    ],
    "properties": {
        "scene_id": {"type": "string", "minLength": 1},
        "schema_version": {"type": "integer", "const": SCENE_SCHEMA_VERSION},
        "name": {"type": "string", "minLength": 1},
        "width": {"type": "integer", "minimum": 1},
        "height": {"type": "integer", "minimum": 1},
        "fps_num": {"type": "integer", "minimum": 1},
        "fps_den": {"type": "integer", "minimum": 1},
        "duration_frames": {"type": "integer", "minimum": 1},
        "background": {"type": "object"},
        "elements": {"type": "array", "items": {"$ref": "#/$defs/element"}},
        "pages": {"type": "array", "items": {"$ref": "#/$defs/page"}},
        "animations": {"type": "array", "items": {"$ref": "#/$defs/animation"}},
        "markers": {"type": "array", "items": {"$ref": "#/$defs/marker"}},
        "asset_refs": {"type": "array", "items": {"$ref": "#/$defs/asset_ref"}},
        "cues": {"type": "array", "items": {"$ref": "#/$defs/cue"}},
        "expressions": {"type": "array", "items": {"$ref": "#/$defs/expression"}},
        "html_layer": {"type": ["object", "null"]},
        "metadata": {"type": "object"},
    },
    "$defs": {
        "element": {"type": "object", "required": ["element_id", "type"],
                     "properties": {"element_id": {"type": "string", "minLength": 1},
                                    "type": {"type": "string", "enum": sorted(SUPPORTED_ELEMENT_TYPES)},
                                    "page_id": {"type": "string", "minLength": 1},
                                    "z_index": {"type": "integer"}, "transform": {"type": "object"},
                                    "layout": {"type": "object"}}},
        "page": {"type": "object", "required": ["page_id", "duration_frames"],
                 "properties": {"page_id": {"type": "string", "minLength": 1},
                                "name": {"type": "string"},
                                "duration_frames": {"type": "integer", "minimum": 1},
                                "background": {"type": "string"}}},
        "animation": {"type": "object", "required": ["animation_id", "element_id", "keyframes"],
                       "properties": {"animation_id": {"type": "string", "minLength": 1},
                                      "element_id": {"type": "string", "minLength": 1},
                                      "keyframes": {"type": "array", "minItems": 1}}},
        "marker": {"type": "object", "required": ["marker_id", "frame"],
                    "properties": {"marker_id": {"type": "string", "minLength": 1},
                                   "frame": {"type": "integer", "minimum": 0},
                                   "label": {"type": "string"}}},
        "asset_ref": {"type": "object", "required": ["asset_id"],
                       "properties": {"asset_id": {"type": "string", "minLength": 1},
                                      "path": {"type": "string"}}},
        "cue": {"type": "object", "required": ["cue_id", "frame", "duration_frames"],
                "properties": {"cue_id": {"type": "string", "minLength": 1},
                               "frame": {"type": "integer", "minimum": 0},
                               "duration_frames": {"type": "integer", "minimum": 1},
                               "asset_ref": {"type": ["string", "null"]}}},
        "expression": {"type": "object", "required": ["expression_id", "element_id", "property", "source"],
                        "properties": {"expression_id": {"type": "string", "minLength": 1},
                                       "element_id": {"type": "string", "minLength": 1},
                                       "property": {"type": "string", "minLength": 1},
                                       "source": {"type": "string", "minLength": 1},
                                       "default": {"type": "number"},
                                       "seed": {"type": "integer"},
                                       "version": {"type": "integer", "minimum": 1}}},
    },
}

PAYLOAD_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "topos://teaching-animation/payload/1",
    "type": "object",
    "additionalProperties": True,
    "required": ["schema_version", "scenes", "clips", "nodes"],
    "properties": {
        "schema_version": {"type": "integer", "minimum": 1},
        "scenes": {"type": "array"},
        "clips": {"type": "array"},
        "nodes": {"type": "array"},
        "metadata": {"type": "object"},
    },
}

# Short alias for callers that use the plan's terminology.
SCENE_SCHEMA = SCENE_JSON_SCHEMA


@dataclass(frozen=True)
class ValidationIssue:
    path: str
    code: str
    message: str
    severity: str = "error"


@dataclass(frozen=True)
class ValidationResult:
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity == "warning")

    @property
    def is_valid(self) -> bool:
        return not self.errors


class SceneValidationError(ValueError):
    """Raised by ``assert_valid_scene_json`` for command/save boundaries."""

    def __init__(self, result: ValidationResult) -> None:
        self.result = result
        detail = "; ".join(f"{item.path}: {item.message}" for item in result.errors)
        super().__init__(detail or "scene validation failed")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _validate_layout(layout: Any, path: str, issues: list[ValidationIssue]) -> None:
    """Check the WP-3 layout mapping; unknown keys pass through untouched."""
    if not isinstance(layout, Mapping):
        issues.append(ValidationIssue(f"{path}.layout", "type", "layout must be an object"))
        return
    mode = layout.get("mode", "absolute")
    if mode not in LAYOUT_MODES:
        issues.append(ValidationIssue(f"{path}.layout.mode", "enum", "unsupported layout mode"))
    for key in ("gap", "padding"):
        if key in layout and (not _number(layout[key]) or float(layout[key]) < 0.0
                              or float(layout[key]) > 500.0):
            issues.append(ValidationIssue(f"{path}.layout.{key}", "range", f"layout {key} must be 0-500"))
    for key in ("justify",):
        if key in layout and layout[key] not in LAYOUT_JUSTIFY:
            issues.append(ValidationIssue(f"{path}.layout.{key}", "enum", f"unsupported layout {key}"))
    for key in ("align",):
        if key in layout and layout[key] not in LAYOUT_ALIGN:
            issues.append(ValidationIssue(f"{path}.layout.{key}", "enum", f"unsupported layout {key}"))
    for key in ("columns", "rows"):
        value = layout.get(key)
        if key in layout and (not isinstance(value, int) or isinstance(value, bool)
                              or not 1 <= value <= 16):
            issues.append(ValidationIssue(f"{path}.layout.{key}", "range", f"layout {key} must be 1-16"))
    for key in ("width_mode", "height_mode"):
        if key in layout and layout[key] not in SIZE_POLICIES:
            issues.append(ValidationIssue(f"{path}.layout.{key}", "enum", f"unsupported {key}"))
    constraints = layout.get("constraints")
    if constraints is not None:
        if not isinstance(constraints, Mapping):
            issues.append(ValidationIssue(f"{path}.layout.constraints", "type",
                                          "constraints must be an object"))
        else:
            if constraints.get("horizontal", "left") not in HORIZONTAL_CONSTRAINTS:
                issues.append(ValidationIssue(f"{path}.layout.constraints.horizontal", "enum",
                                              "unsupported horizontal constraint"))
            if constraints.get("vertical", "top") not in VERTICAL_CONSTRAINTS:
                issues.append(ValidationIssue(f"{path}.layout.constraints.vertical", "enum",
                                              "unsupported vertical constraint"))
            if "keep_aspect" in constraints and not isinstance(constraints["keep_aspect"], bool):
                issues.append(ValidationIssue(f"{path}.layout.constraints.keep_aspect", "type",
                                              "keep_aspect must be a boolean"))


def _validate_track(
    track: Any,
    path: str,
    duration_frames: int,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(track, Mapping):
        issues.append(ValidationIssue(path, "type", "animation track must be an object"))
        return
    param_name = track.get("param_name", track.get("property"))
    if not isinstance(param_name, str) or not param_name:
        issues.append(ValidationIssue(f"{path}.param_name", "required", "parameter name is required"))
    keyframes = track.get("keyframes")
    if not isinstance(keyframes, list) or not keyframes:
        issues.append(ValidationIssue(f"{path}.keyframes", "required", "at least one keyframe is required"))
        return
    previous_frame: int | None = None
    for index, keyframe in enumerate(keyframes):
        item_path = f"{path}.keyframes[{index}]"
        if not isinstance(keyframe, Mapping):
            issues.append(ValidationIssue(item_path, "type", "keyframe must be an object"))
            continue
        frame = keyframe.get("frame")
        if not isinstance(frame, int) or isinstance(frame, bool):
            issues.append(ValidationIssue(f"{item_path}.frame", "type", "frame must be an integer"))
        else:
            if frame < 0 or frame >= duration_frames:
                issues.append(ValidationIssue(f"{item_path}.frame", "range", "frame is outside the scene"))
            if previous_frame is not None and frame <= previous_frame:
                issues.append(ValidationIssue(f"{item_path}.frame", "order", "keyframes must be strictly ordered"))
            previous_frame = frame
        if not _number(keyframe.get("value")):
            issues.append(ValidationIssue(f"{item_path}.value", "type", "value must be finite numeric"))
        interpolation = keyframe.get("interpolation", "linear")
        try:
            InterpolationType(interpolation)
        except (TypeError, ValueError):
            issues.append(ValidationIssue(f"{item_path}.interpolation", "enum", "unsupported interpolation"))


def validate_scene_json(
    data: Mapping[str, Any],
    *,
    template_registry: Any = None,
) -> ValidationResult:
    """Return all validation issues without mutating or constructing a scene."""
    issues: list[ValidationIssue] = []
    if not isinstance(data, Mapping):
        return ValidationResult((ValidationIssue("$", "type", "scene root must be an object"),))

    required_strings = ("scene_id", "name")
    for key in required_strings:
        if not isinstance(data.get(key), str) or not data.get(key):
            issues.append(ValidationIssue(key, "required", f"{key} must be a non-empty string"))
    version = data.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        issues.append(ValidationIssue("schema_version", "type", "schema_version must be a positive integer"))
    elif version > SCENE_SCHEMA_VERSION:
        issues.append(ValidationIssue("schema_version", "future", "scene schema is newer than supported"))
    for key in ("width", "height", "fps_num", "fps_den", "duration_frames"):
        value = data.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            issues.append(ValidationIssue(key, "range", f"{key} must be a positive integer"))

    duration_frames = data.get("duration_frames", 1)
    if not isinstance(duration_frames, int) or duration_frames <= 0:
        duration_frames = 1

    # Pages are optional for legacy JSON, but once present they define the
    # scene's complete artboard sequence.  Validate their identity and total
    # span here so a malformed file cannot produce ambiguous frame ownership.
    pages = data.get("pages")
    page_ids: set[str] = set()
    page_by_element_id: dict[str, str] = {}
    if pages is not None:
        if not isinstance(pages, list) or not pages:
            issues.append(ValidationIssue("pages", "type", "pages must be a non-empty array"))
            pages = []
        total_page_frames = 0
        for index, page in enumerate(pages):
            path = f"pages[{index}]"
            if not isinstance(page, Mapping):
                issues.append(ValidationIssue(path, "type", "page must be an object"))
                continue
            page_id = page.get("page_id")
            if not isinstance(page_id, str) or not page_id:
                issues.append(ValidationIssue(f"{path}.page_id", "required", "page_id is required"))
            elif page_id in page_ids:
                issues.append(ValidationIssue(f"{path}.page_id", "duplicate", "page_id must be unique"))
            else:
                page_ids.add(page_id)
            page_duration = page.get("duration_frames")
            if not isinstance(page_duration, int) or isinstance(page_duration, bool) or page_duration <= 0:
                issues.append(ValidationIssue(
                    f"{path}.duration_frames", "range", "page duration must be positive",
                ))
            else:
                total_page_frames += page_duration
            if "name" in page and not isinstance(page.get("name"), str):
                issues.append(ValidationIssue(f"{path}.name", "type", "page name must be a string"))
            if "background" in page and not isinstance(page.get("background"), str):
                issues.append(ValidationIssue(
                    f"{path}.background", "type", "page background must be a string",
                ))
        if total_page_frames != duration_frames:
            issues.append(ValidationIssue(
                "pages", "duration", "page durations must add up to scene duration_frames",
            ))

    elements = data.get("elements")
    element_ids: set[str] = set()
    if not isinstance(elements, list):
        issues.append(ValidationIssue("elements", "type", "elements must be an array"))
        elements = []
    for index, element in enumerate(elements):
        path = f"elements[{index}]"
        if not isinstance(element, Mapping):
            issues.append(ValidationIssue(path, "type", "element must be an object"))
            continue
        element_id = element.get("element_id")
        if not isinstance(element_id, str) or not element_id:
            issues.append(ValidationIssue(f"{path}.element_id", "required", "element_id is required"))
        elif element_id in element_ids:
            issues.append(ValidationIssue(f"{path}.element_id", "duplicate", "element_id must be unique"))
        else:
            element_ids.add(element_id)
        element_page_id = element.get("page_id")
        if page_ids:
            if not isinstance(element_page_id, str) or not element_page_id:
                issues.append(ValidationIssue(
                    f"{path}.page_id", "required", "page_id is required when pages are present",
                ))
            elif element_page_id not in page_ids:
                issues.append(ValidationIssue(
                    f"{path}.page_id", "reference", "element page does not exist",
                ))
            elif isinstance(element_id, str) and element_id:
                page_by_element_id[element_id] = element_page_id
        if element.get("type") not in SUPPORTED_ELEMENT_TYPES:
            issues.append(ValidationIssue(f"{path}.type", "enum", "unsupported element type"))
        if element.get("type") == "frame":
            for child_key in ("content",):
                if child_key in element and not isinstance(element[child_key], (str, Mapping)):
                    issues.append(ValidationIssue(f"{path}.{child_key}", "type", "frame content must be a string or object"))
        if "layout" in element:
            _validate_layout(element.get("layout"), path, issues)
        if not isinstance(element.get("z_index", 0), int):
            issues.append(ValidationIssue(f"{path}.z_index", "type", "z_index must be an integer"))
        transform = element.get("transform", {})
        if not isinstance(transform, Mapping):
            issues.append(ValidationIssue(f"{path}.transform", "type", "transform must be an object"))
        else:
            for key in ("x", "y", "width", "height", "scale_x", "scale_y", "rotation"):
                if key in transform and not _number(transform[key]):
                    issues.append(ValidationIssue(f"{path}.transform.{key}", "type", "transform value must be finite numeric"))
            for key in ("width", "height"):
                if _number(transform.get(key)) and float(transform[key]) < 0.0:
                    issues.append(ValidationIssue(f"{path}.transform.{key}", "range", "size cannot be negative"))
            for key in ("scale_x", "scale_y"):
                if _number(transform.get(key)) and float(transform[key]) <= 0.0:
                    issues.append(ValidationIssue(f"{path}.transform.{key}", "range", "scale must be positive"))
        template_id = element.get("template_id")
        if template_id and template_registry is not None and template_registry.get(template_id) is None:
            issues.append(ValidationIssue(f"{path}.template_id", "reference", "template is not registered"))
        tracks = element.get("local_animation_tracks", [])
        if not isinstance(tracks, list):
            issues.append(ValidationIssue(f"{path}.local_animation_tracks", "type", "tracks must be an array"))
        else:
            for track_index, track in enumerate(tracks):
                _validate_track(track, f"{path}.local_animation_tracks[{track_index}]", duration_frames, issues)

    metadata = data.get("metadata")
    hierarchy_data = metadata.get("hierarchy") if isinstance(metadata, Mapping) else None
    parents = hierarchy_data.get("parents", {}) if isinstance(hierarchy_data, Mapping) else {}
    if page_ids and isinstance(parents, Mapping):
        for child_id, parent_id in parents.items():
            child_page = page_by_element_id.get(child_id)
            parent_page = page_by_element_id.get(parent_id)
            if child_page and parent_page and child_page != parent_page:
                issues.append(ValidationIssue(
                    "metadata.hierarchy.parents", "cross_page",
                    "parent and child elements must belong to the same page",
                ))

    animations = data.get("animations", [])
    if not isinstance(animations, list):
        issues.append(ValidationIssue("animations", "type", "animations must be an array"))
    else:
        animation_ids: set[str] = set()
        for index, animation in enumerate(animations):
            path = f"animations[{index}]"
            if not isinstance(animation, Mapping):
                issues.append(ValidationIssue(path, "type", "animation must be an object"))
                continue
            animation_id = animation.get("animation_id")
            if animation_id in animation_ids:
                issues.append(ValidationIssue(f"{path}.animation_id", "duplicate", "animation_id must be unique"))
            if isinstance(animation_id, str):
                animation_ids.add(animation_id)
            if animation.get("element_id") not in element_ids:
                issues.append(ValidationIssue(f"{path}.element_id", "reference", "animation element does not exist"))
            _validate_track(animation, path, duration_frames, issues)

    asset_refs = data.get("asset_refs", [])
    asset_ids: set[str] = set()
    if not isinstance(asset_refs, list):
        issues.append(ValidationIssue("asset_refs", "type", "asset_refs must be an array"))
    else:
        for index, asset in enumerate(asset_refs):
            path = f"asset_refs[{index}]"
            if not isinstance(asset, Mapping):
                issues.append(ValidationIssue(path, "type", "asset reference must be an object"))
                continue
            asset_id = asset.get("asset_id")
            if not isinstance(asset_id, str) or not asset_id:
                issues.append(ValidationIssue(f"{path}.asset_id", "required", "asset_id is required"))
            elif asset_id in asset_ids:
                issues.append(ValidationIssue(f"{path}.asset_id", "duplicate", "asset_id must be unique"))
            else:
                asset_ids.add(asset_id)
            asset_path = asset.get("path", "")
            if not isinstance(asset_path, str) or asset_path.startswith("/"):
                issues.append(ValidationIssue(f"{path}.path", "path", "asset path must be relative"))

    markers = data.get("markers", [])
    marker_ids: set[str] = set()
    if not isinstance(markers, list):
        issues.append(ValidationIssue("markers", "type", "markers must be an array"))
    else:
        for index, marker in enumerate(markers):
            path = f"markers[{index}]"
            if not isinstance(marker, Mapping):
                issues.append(ValidationIssue(path, "type", "marker must be an object"))
                continue
            marker_id = marker.get("marker_id")
            if not isinstance(marker_id, str) or not marker_id:
                issues.append(ValidationIssue(f"{path}.marker_id", "required", "marker_id is required"))
            elif marker_id in marker_ids:
                issues.append(ValidationIssue(f"{path}.marker_id", "duplicate", "marker_id must be unique"))
            else:
                marker_ids.add(marker_id)
            frame = marker.get("frame")
            if not isinstance(frame, int) or isinstance(frame, bool) or not 0 <= frame < duration_frames:
                issues.append(ValidationIssue(f"{path}.frame", "range", "marker frame is outside the scene"))

    cues = data.get("cues", [])
    cue_ids: set[str] = set()
    if not isinstance(cues, list):
        issues.append(ValidationIssue("cues", "type", "cues must be an array"))
    else:
        for index, cue in enumerate(cues):
            path = f"cues[{index}]"
            if not isinstance(cue, Mapping):
                issues.append(ValidationIssue(path, "type", "cue must be an object"))
                continue
            cue_id = cue.get("cue_id")
            if not isinstance(cue_id, str) or not cue_id:
                issues.append(ValidationIssue(f"{path}.cue_id", "required", "cue_id is required"))
            elif cue_id in cue_ids:
                issues.append(ValidationIssue(f"{path}.cue_id", "duplicate", "cue_id must be unique"))
            else:
                cue_ids.add(cue_id)
            frame = cue.get("frame")
            duration = cue.get("duration_frames")
            if not isinstance(frame, int) or isinstance(frame, bool) or frame < 0:
                issues.append(ValidationIssue(f"{path}.frame", "range", "cue frame must be non-negative"))
            if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                issues.append(ValidationIssue(f"{path}.duration_frames", "range", "cue duration must be positive"))
            elif isinstance(frame, int) and frame + duration > duration_frames:
                issues.append(ValidationIssue(path, "range", "cue extends beyond the scene"))
            asset_ref = cue.get("asset_ref")
            if asset_ref is not None and (not isinstance(asset_ref, str) or asset_ref not in asset_ids):
                issues.append(ValidationIssue(f"{path}.asset_ref", "reference", "cue asset_ref does not exist"))

    expressions = data.get("expressions", [])
    expression_ids: set[str] = set()
    if not isinstance(expressions, list):
        issues.append(ValidationIssue("expressions", "type", "expressions must be an array"))
    else:
        for index, expression in enumerate(expressions):
            path = f"expressions[{index}]"
            if not isinstance(expression, Mapping):
                issues.append(ValidationIssue(path, "type", "expression must be an object"))
                continue
            expression_id = expression.get("expression_id")
            if not isinstance(expression_id, str) or not expression_id:
                issues.append(ValidationIssue(f"{path}.expression_id", "required", "expression_id is required"))
            elif expression_id in expression_ids:
                issues.append(ValidationIssue(f"{path}.expression_id", "duplicate", "expression_id must be unique"))
            else:
                expression_ids.add(expression_id)
            if expression.get("element_id") not in element_ids:
                issues.append(ValidationIssue(f"{path}.element_id", "reference", "expression element does not exist"))
            if not isinstance(expression.get("property"), str) or not expression.get("property"):
                issues.append(ValidationIssue(f"{path}.property", "required", "expression property is required"))
            source = expression.get("source")
            if not isinstance(source, str) or not source.strip() or len(source) > 4096:
                issues.append(ValidationIssue(f"{path}.source", "range", "expression source must be 1-4096 characters"))
            else:
                try:
                    validate_expression(source)
                except ExpressionError as exc:
                    issues.append(ValidationIssue(f"{path}.source", "unsafe", str(exc)))
            if not _number(expression.get("default", 0.0)):
                issues.append(ValidationIssue(f"{path}.default", "type", "expression default must be finite numeric"))

    # FH-01: the optional FreeformHtmlMotionLayer.  It is additive; when absent
    # the scene is a structured/SVG scene.  When present, every bound asset MUST
    # resolve to a declared asset_ref and the provenance hashes must be present.
    html_layer = data.get("html_layer")
    if html_layer is not None:
        if not isinstance(html_layer, Mapping):
            issues.append(ValidationIssue("html_layer", "type", "html_layer must be an object or null"))
        else:
            if not str(html_layer.get("sanitized_html", "")).strip():
                issues.append(ValidationIssue("html_layer.sanitized_html", "required",
                                              "html_layer.sanitized_html cannot be empty"))
            for field_name in ("viewport_width", "viewport_height", "duration_frames"):
                value = html_layer.get(field_name)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    issues.append(ValidationIssue(f"html_layer.{field_name}", "range",
                                                  f"html_layer.{field_name} must be positive"))
            for hash_field in ("source_hash", "sanitizer_hash"):
                value = html_layer.get(hash_field)
                if not isinstance(value, str) or not value:
                    issues.append(ValidationIssue(f"html_layer.{hash_field}", "required",
                                                  f"html_layer.{hash_field} must be present"))
            bound = html_layer.get("bound_asset_ids", [])
            if not isinstance(bound, list):
                issues.append(ValidationIssue("html_layer.bound_asset_ids", "type",
                                              "bound_asset_ids must be an array"))
            else:
                unbound = [aid for aid in bound if aid not in asset_ids]
                if unbound:
                    issues.append(ValidationIssue(
                        "html_layer.bound_asset_ids", "reference",
                        f"html_layer references unbound asset ids: {unbound}",
                    ))

    return ValidationResult(tuple(issues))


def validate_scene(scene: Any, *, template_registry: Any = None) -> ValidationResult:
    """Validate a model or JSON mapping through the same public contract."""
    data = scene.to_dict() if hasattr(scene, "to_dict") else scene
    return validate_scene_json(data, template_registry=template_registry)


def validate_project_payload(
    data: Mapping[str, Any],
    *,
    template_registry: Any = None,
) -> ValidationResult:
    """Validate scenes plus Clip/Node scene references in the project payload."""
    if not isinstance(data, Mapping):
        return ValidationResult((ValidationIssue("$", "type", "payload root must be an object"),))
    issues: list[ValidationIssue] = []
    scenes = data.get("scenes", [])
    if not isinstance(scenes, list):
        return ValidationResult((ValidationIssue("scenes", "type", "scenes must be an array"),))
    scene_ids: set[str] = set()
    for index, scene in enumerate(scenes):
        if not isinstance(scene, Mapping):
            issues.append(ValidationIssue(f"scenes[{index}]", "type", "scene must be an object"))
            continue
        result = validate_scene_json(scene, template_registry=template_registry)
        issues.extend(
            ValidationIssue(f"scenes[{index}].{item.path}", item.code, item.message, item.severity)
            for item in result.issues
        )
        scene_id = scene.get("scene_id")
        if isinstance(scene_id, str):
            if scene_id in scene_ids:
                issues.append(ValidationIssue(f"scenes[{index}].scene_id", "duplicate", "scene_id must be unique"))
            scene_ids.add(scene_id)
    for collection_name, id_name in (("clips", "clip_id"), ("nodes", "node_id")):
        collection = data.get(collection_name, [])
        if not isinstance(collection, list):
            issues.append(ValidationIssue(collection_name, "type", f"{collection_name} must be an array"))
            continue
        ids: set[str] = set()
        for index, item in enumerate(collection):
            path = f"{collection_name}[{index}]"
            if not isinstance(item, Mapping):
                issues.append(ValidationIssue(path, "type", "reference must be an object"))
                continue
            item_id = item.get(id_name)
            if not isinstance(item_id, str) or not item_id:
                issues.append(ValidationIssue(f"{path}.{id_name}", "required", f"{id_name} is required"))
            elif item_id in ids:
                issues.append(ValidationIssue(f"{path}.{id_name}", "duplicate", f"{id_name} must be unique"))
            else:
                ids.add(item_id)
            if item.get("scene_id") not in scene_ids:
                issues.append(ValidationIssue(f"{path}.scene_id", "reference", "scene_id does not exist"))
    return ValidationResult(tuple(issues))


def assert_valid_scene_json(data: Mapping[str, Any], *, template_registry: Any = None) -> None:
    result = validate_scene_json(data, template_registry=template_registry)
    if not result.is_valid:
        raise SceneValidationError(result)
