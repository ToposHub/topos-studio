"""Deterministic SVG renderer for preview and export."""

from __future__ import annotations

from html import escape
import json
import re
from typing import Any, Mapping, MutableMapping
from xml.etree import ElementTree

from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame, SceneJson
from ..core.hierarchy import read_hierarchy, sort_for_paint
from ..core.model import TeachingSceneDocument
from ..core.composition_schema import MaskKind, read_instances, read_masks
from ..core.professional import particle_positions, read_particle_emitters, read_scene_color_pipeline
from ..core.vector_shapes import (
    regular_polygon_points,
    sanitize_path_data,
    star_points,
)
from .asset_resolver import AssetResolver
from .advanced import ChartData, formula_to_plain_text, render_chart_svg, render_draw_svg, resolve_localized_text
from .evaluator import EvaluatedElement, evaluate
from .effect_capabilities import (
    RenderProfile,
    ResolvedEffectChain,
    read_effect_declarations,
    resolve_effect_chain,
)
from .scene_renderer import CancellationToken, RenderCancelled


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_SAFE_TAGS = frozenset({
    "svg", "g", "defs", "linearGradient", "radialGradient", "stop", "clipPath",
    "rect", "circle", "ellipse", "line", "polyline", "polygon", "path", "text", "tspan",
})
_SAFE_ATTRS = frozenset({
    "d", "cx", "cy", "fill", "fill-opacity", "font-family", "font-size", "font-style",
    "font-weight", "height", "letter-spacing", "offset", "opacity", "points",
    "data-element-id", "filter", "href", "id", "mask", "opacity", "preserveAspectRatio",
    "r", "rx", "ry", "stop-color", "stop-opacity", "stroke", "stroke-linecap", "stroke-linejoin",
    "stroke-opacity", "stroke-width", "text-anchor", "transform", "viewBox", "width", "x", "x1",
    "x2", "y", "y1", "y2", "xlink:href",
})
_SAFE_PATH = re.compile(r"[0-9A-Za-z .,+\-eE]+$")

#: AUD-30401: precomp (scene instance) expansion recursion guard.  Deeper
#: nesting than this is a pathological composition (e.g. a store swap
#: mid-render re-introducing a cycle) and fails closed with a structured
#: error instead of silently dropping content or exhausting the stack.
_PRECOMP_MAX_DEPTH = 8


def render_svg_demo(frame: AnimationFrame, context: RenderContext, scene: SceneJson) -> bytes:
    """Render the Phase 0 title + arrow demo from explicit frame data."""
    title = escape(str(scene.get("title", "Teaching Animation")))
    arrow_color = escape(str(scene.get("arrow_color", "#3b82f6")))
    arrow_progress = max(0.0, min(1.0, frame.frame / max(1.0, context.fps)))
    x2 = 160.0 + (260.0 * arrow_progress)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{context.width}" '
        f'height="{context.height}" viewBox="0 0 {context.width} {context.height}">'
        '<rect width="100%" height="100%" fill="#111827"/>'
        f'<text x="40" y="80" fill="#f9fafb" font-family="sans-serif" '
        f'font-size="36">{title}</text>'
        f'<line x1="160" y1="180" x2="{x2:.6f}" y2="180" '
        f'stroke="{arrow_color}" stroke-width="12" stroke-linecap="round"/>'
        f'<polygon points="{x2:.6f},180 {x2 - 28.0:.6f},164 {x2 - 28.0:.6f},196" '
        f'fill="{arrow_color}"/>'
        f'<text x="40" y="320" fill="#9ca3af" font-family="monospace" '
        f'font-size="22">frame={frame.frame} fps={context.fps:g}</text>'
        '</svg>'
    )
    return svg.encode("utf-8")


def _number(value: Any, default: float) -> str:
    try:
        return f"{float(value):.6f}"
    except (TypeError, ValueError):
        return f"{default:.6f}"


def _color(value: Any, default: str) -> str:
    candidate = str(value or default)
    return candidate if _HEX_COLOR.fullmatch(candidate) else default


def _font_family(value: Any, default: str = "sans-serif") -> str:
    candidate = str(value or default)
    if re.fullmatch(r"[A-Za-z0-9 ,_-]{1,80}", candidate):
        return candidate
    return default


def _attrs(attrs: Mapping[str, Any]) -> str:
    return " ".join(
        f'{name}="{escape(str(value), quote=True)}"'
        for name, value in attrs.items()
        if name in _SAFE_ATTRS
    )


def _safe_float(value: Any, default: float, *, minimum: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if minimum is not None:
        result = max(minimum, result)
    return result


def _filter_fragment(effect_type: str, params: Mapping[str, Any]) -> str:
    """Build one inert SVG filter primitive from a catalog declaration."""
    if effect_type == "blur":
        radius = _safe_float(params.get("radius", params.get("radius_x", 4.0)), 4.0, minimum=0.0)
        return f'<feGaussianBlur stdDeviation="{_number(radius, 4.0)}"/>'
    if effect_type == "shadow":
        dx = _safe_float(params.get("dx", 0.0), 0.0)
        dy = _safe_float(params.get("dy", 4.0), 4.0)
        radius = _safe_float(params.get("radius", 4.0), 4.0, minimum=0.0)
        opacity = max(0.0, min(1.0, _safe_float(params.get("opacity", 0.45), 0.45)))
        color = _color(params.get("color", "#000000"), "#000000")
        return (
            f'<feDropShadow dx="{_number(dx, 0.0)}" dy="{_number(dy, 4.0)}" '
            f'stdDeviation="{_number(radius, 4.0)}" flood-color="{color}" '
            f'flood-opacity="{_number(opacity, 0.45)}"/>'
        )
    if effect_type == "color_matrix":
        values = params.get("values")
        if not isinstance(values, (list, tuple)) or len(values) != 20:
            return ""
        numbers = " ".join(_number(value, 0.0) for value in values)
        return f'<feColorMatrix type="matrix" values="{escape(numbers, quote=True)}"/>'
    return ""


def _effect_filter(
    element: EvaluatedElement,
    profile: RenderProfile,
    id_scope: str = "",
) -> tuple[str | None, ResolvedEffectChain]:
    declarations = read_effect_declarations(element.style)
    if not declarations:
        return None, ResolvedEffectChain((), (), False)
    chain = resolve_effect_chain(declarations, "svg", profile)
    fragments = [
        _filter_fragment(resolved.applied_effect_type, resolved.declaration.params)
        for resolved in chain.applied
        if resolved.status in {"run", "approximated", "fallback"}
    ]
    fragments = [fragment for fragment in fragments if fragment]
    # AUD-30405: element ids are scene-local, so an inlined referenced scene
    # whose ids collide with the parent's would emit duplicate <filter> ids
    # and silently pick up the parent's filter.  ``id_scope`` namespaces the
    # generated definition ids per instance ("" keeps the parent's legacy ids).
    return (f"effect_{id_scope}{element.source.element_id}" if fragments else None), chain


def _effect_defs(
    elements: tuple[EvaluatedElement, ...],
    profile: RenderProfile,
    id_scope: str = "",
) -> tuple[str, dict[str, ResolvedEffectChain], dict[str, str | None]]:
    """Build the ``<defs>`` block plus per-element effect lookups.

    AUD-30407: the paint loops used to re-run ``_effect_filter`` per element
    (which rebuilds the filter primitive fragments a second time after
    ``_effect_defs`` already built and discarded them once).  The returned
    ``filter_ids`` map (element_id -> filter id or None) lets the paint loops
    look the id up instead of recomputing the whole effect chain.
    """
    definitions: list[str] = []
    chains: dict[str, ResolvedEffectChain] = {}
    filter_ids: dict[str, str | None] = {}
    for element in elements:
        filter_id, chain = _effect_filter(element, profile, id_scope=id_scope)
        chains[element.source.element_id] = chain
        filter_ids[element.source.element_id] = filter_id
        if filter_id is None:
            continue
        fragments = [
            _filter_fragment(resolved.applied_effect_type, resolved.declaration.params)
            for resolved in chain.applied
            if resolved.status in {"run", "approximated", "fallback"}
        ]
        fragments = [fragment for fragment in fragments if fragment]
        if fragments:
            definitions.append(f'<filter id="{filter_id}" x="-50%" y="-50%" width="200%" height="200%">{"".join(fragments)}</filter>')
    return (
        (f'<defs>{"".join(definitions)}</defs>' if definitions else ""),
        chains,
        filter_ids,
    )


def _safe_id(value: str, prefix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", value)
    return f"{prefix}_{cleaned[:80]}"


def _id_scope(instance_id: str) -> str:
    """Namespace prefix for a referenced scene's definition ids (AUD-30405)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", instance_id)[:64]
    return f"{cleaned}__" if cleaned else ""


def _composition_defs(
    scene: TeachingSceneDocument,
) -> tuple[str, dict[str, str], tuple[str, ...]]:
    """Build safe SVG mask definitions and target→mask bindings."""
    definitions: list[str] = []
    bindings: dict[str, str] = {}
    diagnostics: list[str] = []
    known_targets = {element.element_id for element in scene.elements}
    known_targets.update(item.instance_id for item in read_instances(scene.metadata))
    for mask in read_masks(scene.metadata):
        if mask.target not in known_targets:
            diagnostics.append(f"mask {mask.mask_id}: target not found")
            continue
        if not mask.path_ref or _SAFE_PATH.fullmatch(mask.path_ref) is None:
            diagnostics.append(f"mask {mask.mask_id}: path_ref is missing or unsafe")
            continue
        mask_id = _safe_id(mask.mask_id, "mask")
        fill = "#000000" if mask.invert else "#ffffff"
        path_fill = "#ffffff" if mask.invert else "#000000"
        mask_type = "luminance" if mask.kind == MaskKind.LUMA else "alpha"
        definitions.append(
            f'<mask id="{mask_id}" mask-type="{mask_type}">'
            f'<rect width="100%" height="100%" fill="{fill}"/>'
            f'<path d="{escape(mask.path_ref, quote=True)}" fill="{path_fill}" '
            f'fill-opacity="{_number(mask.opacity, 1.0)}"/></mask>'
        )
        bindings[mask.target] = mask_id
    return (f'<defs>{"".join(definitions)}</defs>' if definitions else ""), bindings, tuple(diagnostics)


def _sanitize_svg(value: str) -> str:
    """Keep a small inert SVG subset and discard scripts/external references."""
    try:
        root = ElementTree.fromstring(value)
    except ElementTree.ParseError:
        return ""

    def render_node(node: ElementTree.Element) -> str:
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in _SAFE_TAGS:
            return "".join(render_node(child) for child in node)
        safe_attributes = {
            key.rsplit("}", 1)[-1]: val
            for key, val in node.attrib.items()
            if key.rsplit("}", 1)[-1] in _SAFE_ATTRS
            and not key.lower().endswith("href")
        }
        children = escape(node.text or "") + "".join(render_node(child) + escape(child.tail or "") for child in node)
        if tag == "svg":
            return children
        return f"<{tag}{(' ' + _attrs(safe_attributes)) if safe_attributes else ''}>{children}</{tag}>"

    return render_node(root)


def sanitize_inline_svg(value: str) -> str:
    """Public wrapper so the DOM overlay shares the same inert-SVG subset."""
    return _sanitize_svg(value)


def _transform_attributes(element: EvaluatedElement) -> str:
    transform = element.transform
    return (
        f'translate({_number(transform.x, 0.0)} {_number(transform.y, 0.0)}) '
        f'rotate({_number(transform.rotation, 0.0)}) '
        f'scale({_number(transform.scale_x, 1.0)} {_number(transform.scale_y, 1.0)})'
    )


def _gradient_fill(element: EvaluatedElement, fallback: str, id_scope: str = "") -> tuple[str, str]:
    """Return a safe fill value plus an optional per-element gradient definition.

    The scene style schema already carries structured gradients, but the old SVG
    path ignored them and flattened every shape to one solid colour.  That made
    model-authored designs look like wireframes even when they requested rich
    web-style fills.  Keep the definition local to the element so generated ids
    never collide across layers (``id_scope`` extends that guarantee across the
    precomp inline boundary, AUD-30405).
    """
    style = element.style if isinstance(element.style, Mapping) else {}
    raw = style.get("gradient")
    if not isinstance(raw, Mapping):
        return fallback, ""
    stops = raw.get("stops")
    if not isinstance(stops, (list, tuple)) or len(stops) < 2:
        return fallback, ""
    gradient_id = _safe_id(f"{id_scope}{element.source.element_id}", "gradient")
    kind = str(raw.get("kind", "linear")).lower()
    if kind == "radial":
        opening = f'<radialGradient id="{gradient_id}" cx="50%" cy="50%" r="70%">'
    else:
        angle = _safe_float(raw.get("angle", 0.0), 0.0)
        # SVG's object-bounding-box coordinates are deliberately simple and
        # deterministic; the angle is represented by endpoint selection for
        # the common horizontal/vertical/diagonal cases.
        angle_mod = angle % 360.0
        if 45.0 <= angle_mod < 135.0:
            coords = ("0%", "100%", "0%", "0%")
        elif 135.0 <= angle_mod < 225.0:
            coords = ("100%", "0%", "0%", "0%")
        elif 225.0 <= angle_mod < 315.0:
            coords = ("100%", "0%", "100%", "100%")
        else:
            coords = ("0%", "0%", "100%", "0%")
        opening = (
            f'<linearGradient id="{gradient_id}" x1="{coords[0]}" y1="{coords[1]}" '
            f'x2="{coords[2]}" y2="{coords[3]}">'
        )
    rendered_stops: list[str] = []
    for stop in stops[:16]:
        if not isinstance(stop, Mapping):
            continue
        position = max(0.0, min(1.0, _safe_float(stop.get("position", 0.0), 0.0)))
        colour = _color(stop.get("color", fallback), fallback)
        opacity = max(0.0, min(1.0, _safe_float(stop.get("opacity", 1.0), 1.0)))
        rendered_stops.append(
            f'<stop offset="{_number(position * 100.0, 0.0)}%" stop-color="{colour}" '
            f'stop-opacity="{_number(opacity, 1.0)}"/>'
        )
    if len(rendered_stops) < 2:
        return fallback, ""
    return f"url(#{gradient_id})", opening + "".join(rendered_stops) + "</" + ("radialGradient" if kind == "radial" else "linearGradient") + ">"


def _render_shape(element: EvaluatedElement, id_scope: str = "") -> str:
    content = element.source.content if isinstance(element.source.content, Mapping) else {}
    kind = str(content.get("kind", content.get("shape", "rect"))).lower()
    style = element.style
    solid_fill = _color(content.get("fill", style.get("fill", style.get("color", "#3b82f6"))), "#3b82f6")
    fill, gradient_definition = _gradient_fill(element, solid_fill, id_scope=id_scope)
    stroke = _color(content.get("stroke", style.get("stroke", "#000000")), "#000000")
    stroke_width = _number(content.get("stroke_width", style.get("stroke_width", 0.0)), 0.0)
    common = {"fill": fill, "stroke": stroke, "stroke-width": stroke_width}
    # Preserve the byte-stable legacy SVG for ordinary elements.  Rich model
    # styles opt into the extra attributes only when they explicitly request
    # them, so existing release fixtures and cache hashes remain unchanged.
    if "fill_opacity" in content or "fill_opacity" in style:
        common["fill-opacity"] = _number(
            content.get("fill_opacity", style.get("fill_opacity")), 1.0,
        )
    if "stroke_opacity" in content or "stroke_opacity" in style:
        common["stroke-opacity"] = _number(
            content.get("stroke_opacity", style.get("stroke_opacity")), 1.0,
        )
    width = element.transform.width
    height = element.transform.height
    radius = _safe_float(
        content.get("corner_radius", content.get("radius", style.get("corner_radius", style.get("radius", 0.0)))),
        0.0,
        minimum=0.0,
    )
    if kind in {"rounded_rect", "round_rect", "card"} and radius <= 0.0:
        radius = min(width, height) * 0.12
    if kind in {"circle", "ellipse"}:
        if kind == "circle":
            radius = min(width, height) / 2.0
            attrs = {"cx": _number(radius, 0.0), "cy": _number(radius, 0.0), "r": _number(radius, 0.0), **common}
        else:
            attrs = {"cx": _number(width / 2, 0.0), "cy": _number(height / 2, 0.0),
                     "rx": _number(width / 2, 0.0), "ry": _number(height / 2, 0.0), **common}
        rendered = f"<{kind} {_attrs(attrs)}/>"
        return (f"<defs>{gradient_definition}</defs>" if gradient_definition else "") + rendered
    if kind in {"line", "arrow"}:
        x2 = width
        line = f'<line {_attrs({"x1": 0, "y1": height / 2, "x2": x2, "y2": height / 2, **common})}/>'
        if kind == "arrow":
            points = f"{_number(x2, 0)},{_number(height / 2, 0)} {_number(x2 - 18, 0)},{_number(height / 2 - 10, 0)} {_number(x2 - 18, 0)},{_number(height / 2 + 10, 0)}"
            line += f'<polygon {_attrs({"points": points, "fill": fill})}/>'
        rendered = line
    elif kind == "triangle":
        points = ((width / 2.0, 0.0), (width, height), (0.0, height))
        point_data = " ".join(f"{_number(x, 0.0)},{_number(y, 0.0)}" for x, y in points)
        rendered = f'<polygon {_attrs({"points": point_data, **common})}/>'
    elif kind == "polygon":
        points = regular_polygon_points(content.get("sides", style.get("sides", 6)), width, height)
        point_data = " ".join(f"{_number(x, 0.0)},{_number(y, 0.0)}" for x, y in points)
        rendered = f'<polygon {_attrs({"points": point_data, **common})}/>'
    elif kind == "star":
        points = star_points(
            content.get("points", style.get("points", 5)),
            content.get("inner_ratio", style.get("inner_ratio", 0.45)),
            width,
            height,
        )
        point_data = " ".join(f"{_number(x, 0.0)},{_number(y, 0.0)}" for x, y in points)
        rendered = f'<polygon {_attrs({"points": point_data, **common})}/>'
    elif kind == "path":
        path_data = sanitize_path_data(content.get("d", ""))
        if not path_data:
            return ""
        rendered = f'<path {_attrs({"d": path_data, **common})}/>'
    else:
        attrs = {"x": 0, "y": 0, "width": width, "height": height, **common}
        if radius > 0.0:
            attrs["rx"] = radius
            attrs["ry"] = radius
        rendered = f'<rect {_attrs(attrs)}/>'
    return (f"<defs>{gradient_definition}</defs>" if gradient_definition else "") + rendered


def _render_element(
    element: EvaluatedElement,
    resolver: AssetResolver,
    scene: TeachingSceneDocument,
    locale: str = "en-US",
    filter_id: str | None = None,
    mask_id: str | None = None,
    id_scope: str = "",
    uri_cache: MutableMapping[tuple[str, str], str | None] | None = None,
) -> str:
    source = element.source
    style = element.style
    opacity = _number(element.opacity, 1.0)
    group_attrs = {"data-element-id": source.element_id, "opacity": opacity, "transform": _transform_attributes(element)}
    if filter_id is not None:
        group_attrs["filter"] = f"url(#{filter_id})"
    if mask_id is not None:
        group_attrs["mask"] = f"url(#{mask_id})"
    group_open = f"<g {_attrs(group_attrs)}>"
    if source.type == "text" or source.type == "formula":
        raw_value = formula_to_plain_text(source.content) if source.type == "formula" else resolve_localized_text(source.content, locale)
        value = escape(raw_value)
        font_size = _number(style.get("font_size", 32.0 if source.type == "text" else 28.0), 32.0)
        color = _color(style.get("color", style.get("fill", "#f9fafb")), "#f9fafb")
        family = _font_family(style.get("font_family", "serif" if source.type == "formula" else "sans-serif"))
        text_attrs = {
            "x": 0,
            "y": font_size,
            "fill": color,
            "font-family": family,
            "font-size": font_size,
        }
        if "fill_opacity" in style:
            text_attrs["fill-opacity"] = _number(style.get("fill_opacity"), 1.0)
        if "font_weight" in style or "fontWeight" in style:
            text_attrs["font-weight"] = str(style.get("font_weight", style.get("fontWeight")))
        if "font_style" in style or "fontStyle" in style:
            text_attrs["font-style"] = str(style.get("font_style", style.get("fontStyle")))
        if "letter_spacing" in style:
            text_attrs["letter-spacing"] = _number(style.get("letter_spacing"), 0.0)
        if "text_anchor" in style or "textAnchor" in style:
            text_attrs["text-anchor"] = str(style.get("text_anchor", style.get("textAnchor")))
        lines = str(raw_value).splitlines() or [""]
        if len(lines) == 1 and len(text_attrs) == 5:
            # Keep the original compact output for the common text path.
            text = (
                f'<text x="0" y="{font_size}" fill="{color}" '
                f'font-family="{escape(family, quote=True)}" font-size="{font_size}">'
                f'{value}</text>'
            )
        elif len(lines) == 1:
            text = f'<text {_attrs(text_attrs)}>{value}</text>'
        else:
            tspans = [
                f'<tspan x="0" dy="{_number(0.0 if index == 0 else float(font_size) * 1.2, 0.0)}">{escape(line)}</tspan>'
                for index, line in enumerate(lines)
            ]
            text = f'<text {_attrs(text_attrs)}>{"".join(tspans)}</text>'
        return group_open + text + "</g>"
    if source.type == "shape":
        return group_open + _render_shape(element, id_scope=id_scope) + "</g>"
    if source.type == "frame":
        # SVG scenes flatten frame children through the evaluator's world
        # composition; the frame itself only contributes a background plate.
        background = _color(style.get("fill", style.get("color")), "")
        if not background:
            return group_open + "</g>"
        radius = _number(style.get("corner_radius", 0.0), 0.0)
        plate = f'<rect {_attrs({"width": element.transform.width, "height": element.transform.height, "rx": radius, "fill": background})}/>'
        return group_open + plate + "</g>"
    if source.type == "image":
        # AUD-30404: data URIs are pure functions of (scene, asset), but each
        # one costs a full disk read + base64 encode.  Cache per renderer task
        # so a render loop resolves every asset exactly once instead of once
        # per frame.
        cache_key = (scene.scene_id, source.asset_ref or "")
        if uri_cache is not None and cache_key in uri_cache:
            uri = uri_cache[cache_key]
        else:
            asset = resolver.asset_by_id(scene, source.asset_ref or "")
            uri = resolver.data_uri(asset) if asset is not None else None
            if uri_cache is not None:
                uri_cache[cache_key] = uri
        if uri is None:
            placeholder = f'<rect {_attrs({"width": element.transform.width, "height": element.transform.height, "fill": "#7f1d1d", "stroke": "#fca5a5", "stroke-width": 2})}/>'
            return group_open + placeholder + "</g>"
        image = f'<image {_attrs({"width": element.transform.width, "height": element.transform.height, "href": uri, "preserveAspectRatio": "xMidYMid meet"})}/>'
        return group_open + image + "</g>"
    if source.type == "svg":
        return group_open + _sanitize_svg(str(source.content)) + "</g>"
    if source.type == "chart":
        content = source.content if isinstance(source.content, Mapping) else {}
        try:
            chart = ChartData.from_records(content.get("records", [])) if "records" in content else ChartData.from_json(json.dumps(content.get("data", content)))
            chart_type = str(content.get("chart_type", "bar"))
            color = _color(content.get("color", style.get("color", "#3b82f6")), "#3b82f6")
            return group_open + render_chart_svg(chart, element.transform.width, element.transform.height, chart_type=chart_type, color=color) + "</g>"
        except (TypeError, ValueError, json.JSONDecodeError):
            return group_open + "</g>"
    if source.type == "draw":
        content = source.content if isinstance(source.content, Mapping) else {}
        color = _color(content.get("color", style.get("color", "#f59e0b")), "#f59e0b")
        return group_open + render_draw_svg(content, element.transform.width, element.transform.height, color=color) + "</g>"
    return ""


def _render_particles(scene: TeachingSceneDocument, frame: int) -> list[str]:
    output: list[str] = []
    for emitter in read_particle_emitters(scene.metadata):
        for x, y, alpha in particle_positions(emitter, frame):
            if not 0.0 <= alpha:
                continue
            output.append(
                f'<circle data-emitter-id="{escape(emitter.emitter_id, quote=True)}" '
                f'cx="{_number(x * scene.width, 0.0)}" cy="{_number(y * scene.height, 0.0)}" '
                f'r="{_number(emitter.size, 0.0)}" fill="{_color(emitter.color, "#ffffff")}" '
                f'fill-opacity="{_number(alpha, 0.0)}"/>'
            )
    return output


class SvgSceneRenderer:
    """Reference renderer shared by preview and export paths."""

    def __init__(self, cancellation: CancellationToken | None = None) -> None:
        self._scene: SceneJson | None = None
        self._document: TeachingSceneDocument | None = None
        self._context: RenderContext | None = None
        self._assets: AssetResolver | None = None
        self._cancellation = cancellation
        # AUD-30404: per-task caches — one AssetResolver per referenced scene
        # and one resolved data URI per (scene, asset).  Both are rebuilt on
        # prepare() so a reused renderer never serves another scene's data.
        self._nested_resolvers: dict[str, AssetResolver] = {}
        self._data_uri_cache: dict[tuple[str, str], str | None] = {}

    def prepare(self, scene: SceneJson, context: RenderContext) -> None:
        if hasattr(scene, "to_dict"):
            scene = scene.to_dict()
        if not isinstance(scene, dict):
            raise TypeError("scene must be a JSON object")
        self._scene = dict(scene)
        self._context = context
        self._document = None
        self._assets = None
        self._nested_resolvers = {}
        self._data_uri_cache = {}
        if "elements" in scene or "scene_id" in scene:
            self._document = TeachingSceneDocument.from_dict(scene)
            self._assets = AssetResolver(context)
            self._assets.preflight(self._document)

    def _resolver_for(self, scene: TeachingSceneDocument) -> AssetResolver:
        """Return this task's cached resolver for a referenced scene (AUD-30404).

        Built and preflew once per referenced scene per renderer task, instead
        of once per instance per frame.
        """
        if self._context is None:
            raise RuntimeError("renderer must be prepared before rendering")
        resolver = self._nested_resolvers.get(scene.scene_id)
        if resolver is None:
            resolver = AssetResolver(self._context)
            resolver.preflight(scene)
            self._nested_resolvers[scene.scene_id] = resolver
        return resolver

    def render_frame(self, frame: AnimationFrame) -> RenderedFrame:
        if self._scene is None or self._context is None:
            raise RuntimeError("renderer must be prepared before rendering")
        if self._cancellation is not None and self._cancellation.is_cancelled:
            raise RenderCancelled("render cancelled")
        color_pipeline = read_scene_color_pipeline(
            self._document.metadata if self._document is not None else self._scene.get("metadata", {})
        )
        if self._document is None:
            data = render_svg_demo(frame, self._context, self._scene)
        else:
            evaluated = evaluate(self._document, frame)
            profile = {
                "draft": RenderProfile.draft,
                "interactive": RenderProfile.interactive,
                "preview": RenderProfile.preview,
                "final": RenderProfile.final,
            }.get(self._context.render_profile, RenderProfile.preview)()
            effect_defs, effect_chains, effect_filter_ids = _effect_defs(evaluated.elements, profile)
            composition_defs, mask_bindings, composition_diagnostics = _composition_defs(self._document)
            blocked = tuple(
                diagnostic
                for chain in effect_chains.values()
                if chain.blocked
                for diagnostic in chain.diagnostics
            )
            if blocked:
                raise RuntimeError("unsupported SVG effect in final profile: " + "; ".join(blocked))
            background = dict(self._document.background) if isinstance(self._document.background, Mapping) else {}
            page_index = self._document.page_index_for_frame(frame.frame)
            if self._document.pages and page_index < len(self._document.pages):
                page_background = self._document.pages[page_index].background.strip()
                if page_background:
                    background["color"] = page_background
            color = _color(background.get("color", "#111827"), "#111827")
            body = [item for item in (effect_defs, composition_defs) if item]
            if not self._context.transparent:
                body.append(f'<rect width="100%" height="100%" fill="{color}"/>')
            # Hierarchy-aware paint order: a frame's plate stays below the
            # layers nested inside it, matching the live DOM overlay.
            for element in sort_for_paint(
                    evaluated.elements, read_hierarchy(self._document.metadata).parents,
                    id_of=lambda item: item.source.element_id,
                    z_of=lambda item: item.source.z_index):
                if element.source.visible and element.opacity > 0.0:
                    # AUD-30407: look the filter id up instead of re-running
                    # the effect-chain resolution per painted element.
                    filter_id = effect_filter_ids.get(element.source.element_id)
                    body.append(_render_element(
                        element, self._assets, self._document, self._context.locale,
                        filter_id, mask_bindings.get(element.source.element_id),
                        uri_cache=self._data_uri_cache,
                    ))
            body.extend(self._render_precomp_instances(frame, profile, mask_bindings))
            body.extend(_render_particles(self._document, frame.frame))
            data = (
                f'<svg xmlns="http://www.w3.org/2000/svg" width="{self._context.width}" '
                f'height="{self._context.height}" viewBox="0 0 {self._context.width} {self._context.height}">'
                + "".join(body) + "</svg>"
            ).encode("utf-8")
        return RenderedFrame(
            frame=frame.frame,
            width=self._context.width,
            height=self._context.height,
            pixel_format="svg",
            data=data,
            backend=self._context.backend,
            metadata={
                "renderer_version": self._context.renderer_version,
                "frame": frame.frame,
                "color_pipeline": color_pipeline.to_dict(),
                "hdr_fallback": "svg_8bit_tonemap" if color_pipeline.is_hdr else "",
                "expression_diagnostics": list(evaluated.diagnostics) if self._document is not None else [],
            },
        )

    def _render_precomp_instances(
        self,
        frame: AnimationFrame,
        profile: RenderProfile,
        mask_bindings: Mapping[str, str],
    ) -> list[str]:
        """Render scene references without copying their JSON into the parent.

        AUD-30401: expansion is recursive — a referenced scene's own instances
        render too.  Cycles are cut by a per-path visited set and runaway
        nesting fails closed instead of silently dropping content.
        """
        if self._document is None or self._context is None:
            return []
        return self._expand_instances(
            self._document, frame, profile, mask_bindings,
            visited=frozenset({self._document.scene_id}), depth=0,
        )

    def _expand_instances(
        self,
        scene: TeachingSceneDocument,
        frame: AnimationFrame,
        profile: RenderProfile,
        mask_bindings: Mapping[str, str],
        *,
        visited: frozenset[str],
        depth: int,
    ) -> list[str]:
        """Expand every instance one scene declares, recursing into sources."""
        from ..scene_store import get_shared_scene_store

        output: list[str] = []
        for instance in sorted(read_instances(scene.metadata), key=lambda item: item.start_frame):
            if frame.frame < instance.start_frame:
                continue
            local_frame = instance.trim_in + frame.frame - instance.start_frame
            if instance.duration_frames is not None and local_frame >= instance.trim_in + instance.duration_frames:
                continue
            source = get_shared_scene_store().get(instance.source_scene_id)
            if source is None or source.scene_id in visited:
                continue
            if depth >= _PRECOMP_MAX_DEPTH:
                raise RuntimeError(
                    f"precomp instance nesting exceeds {_PRECOMP_MAX_DEPTH} levels "
                    f"at instance '{instance.instance_id}'"
                )
            source_frame = min(max(0, local_frame), source.duration_frames - 1)
            pieces = self._render_referenced_scene(
                source, source_frame, profile, _id_scope(instance.instance_id),
                visited=visited | {source.scene_id}, depth=depth + 1,
            )
            params = instance.params
            x = _safe_float(params.get("x", 0.0), 0.0)
            y = _safe_float(params.get("y", 0.0), 0.0)
            scale = _safe_float(params.get("scale", 1.0), 1.0, minimum=0.0001)
            opacity = max(0.0, min(1.0, _safe_float(params.get("opacity", 1.0), 1.0)))
            # AUD-30402: a mask may target an instance id; apply the parent
            # scene's binding to the instance group instead of ignoring it.
            # Only the rendered document's own instances carry bindings here —
            # masks of transitively referenced scenes are not inlined.
            mask_attr = ""
            if depth == 0:
                mask_id = mask_bindings.get(instance.instance_id)
                if mask_id:
                    mask_attr = f' mask="url(#{escape(mask_id, quote=True)})"'
            output.append(
                f'<g data-instance-id="{escape(instance.instance_id, quote=True)}" '
                f'opacity="{_number(opacity, 1.0)}" transform="translate({_number(x, 0.0)} {_number(y, 0.0)}) scale({_number(scale, 1.0)})"{mask_attr}>'
                + "".join(pieces) + "</g>"
            )
        return output

    def _render_referenced_scene(
        self,
        source: TeachingSceneDocument,
        frame_number: int,
        profile: RenderProfile,
        id_scope: str,
        *,
        visited: frozenset[str],
        depth: int,
    ) -> list[str]:
        """Render one referenced scene's body: defs, elements, nested instances."""
        if self._context is None:
            raise RuntimeError("renderer must be prepared before rendering")
        nested = evaluate(source, frame_number)
        nested_resolver = self._resolver_for(source)
        nested_effect_defs, nested_chains, nested_filter_ids = _effect_defs(nested.elements, profile, id_scope=id_scope)
        blocked = tuple(
            diagnostic
            for chain in nested_chains.values()
            if chain.blocked
            for diagnostic in chain.diagnostics
        )
        if blocked:
            # AUD-30403: a referenced scene fails closed under the final
            # profile exactly like the parent scene — an unsupported effect is
            # an export error, never a silent drop with wrong pixels.
            raise RuntimeError("unsupported SVG effect in final profile: " + "; ".join(blocked))
        pieces: list[str] = [nested_effect_defs] if nested_effect_defs else []
        for element in sort_for_paint(
                nested.elements, read_hierarchy(source.metadata).parents,
                id_of=lambda item: item.source.element_id,
                z_of=lambda item: item.source.z_index):
            if element.source.visible and element.opacity > 0.0:
                # AUD-30407: same lookup table as the parent paint loop.
                filter_id = nested_filter_ids.get(element.source.element_id)
                pieces.append(_render_element(
                    element, nested_resolver, source, self._context.locale, filter_id,
                    id_scope=id_scope, uri_cache=self._data_uri_cache,
                ))
        pieces.extend(self._expand_instances(
            source, AnimationFrame(frame_number, self._context.fps), profile, {},
            visited=visited, depth=depth,
        ))
        return pieces

    def cleanup(self) -> None:
        self._scene = None
        self._document = None
        self._context = None
        self._assets = None
        self._nested_resolvers = {}
        self._data_uri_cache = {}
