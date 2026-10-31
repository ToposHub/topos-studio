"""Trusted HTML/SVG overlay emitted for native MG layers over HTML scenes.

The live editor and the browser capture both use this representation.  Text is
real HTML/CSS, while geometric layers are compact inline SVG.  No authored HTML
is parsed or executed here: all values come from the validated scene model.
Frame containers materialize as CSS Flex/Grid/absolute divs, and children of a
frame render inside their container so parent transforms compose in the DOM.
"""

from __future__ import annotations

from dataclasses import replace
from html import escape
import hashlib
import json
import re
from typing import Any, Callable, Iterable, Mapping

from ..core.model import SceneElement
from ..core.vector_shapes import regular_polygon_points, sanitize_path_data, star_points
from .advanced import ChartData, formula_to_plain_text, render_chart_svg, render_draw_svg


_HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{3,8}$")
_FONT = re.compile(r"^[A-Za-z0-9 ,_\-]{1,80}$")
_FONT_WEIGHT = re.compile(r"^(normal|bold|bolder|lighter|[1-9]00)$")
_TEXT_ALIGN = frozenset({"left", "center", "right", "justify"})
_BLEND_MODES = frozenset({
    "normal", "multiply", "screen", "overlay", "darken", "lighten", "difference",
})
_JUSTIFY_CSS = {
    "start": "flex-start", "center": "center", "end": "flex-end",
    "between": "space-between", "around": "space-around", "evenly": "space-evenly",
}
_ALIGN_CSS = {
    "start": "flex-start", "center": "center", "end": "flex-end", "stretch": "stretch",
}


AssetLookup = Callable[[str], "str | None"]


def native_overlay_markup(
    elements: Iterable[SceneElement],
    hierarchy: Mapping[str, str] | None = None,
    tokens: Any = None,
    asset_lookup: AssetLookup | None = None,
) -> str:
    """Return browser-safe DOM markup for visible native layers.

    ``hierarchy`` maps child element id → parent frame id.  Children of a
    frame render inside their container; children of non-frame parents and
    parentless elements render at document level exactly as before.
    ``tokens`` is a DesignTokenSet: ``$palette.accent`` style references in
    element styles resolve to their current values before materialization,
    matching the SVG evaluator's token resolution.  ``asset_lookup`` maps an
    element ``asset_ref`` to a data URI; image elements without a resolvable
    asset materialize a visible placeholder instead of silently vanishing.
    """
    from ..core.design_tokens import resolve_style
    ordered = sorted(enumerate(elements), key=lambda item: (item[1].z_index, item[0]))
    if tokens is not None:
        ordered = [(_index, replace(element, style=resolve_style(element.style, tokens)))
                   for _index, element in ordered]
    parents = dict(hierarchy) if hierarchy else {}
    known = {element.element_id for _index, element in ordered}
    children: dict[str, list[SceneElement]] = {}
    roots: list[SceneElement] = []
    for _index, element in ordered:
        parent = parents.get(element.element_id)
        if parent in known:
            children.setdefault(parent, []).append(element)
        else:
            roots.append(element)
    return "".join(
        _markup_with_children(element, children, asset_lookup=asset_lookup)
        for element in roots if element.visible
    )


def _markup_with_children(
    element: SceneElement,
    children: dict[str, list[SceneElement]],
    *,
    flow: bool = False,
    asset_lookup: AssetLookup | None = None,
) -> str:
    own = _element_markup(element, flow=flow, asset_lookup=asset_lookup)
    if not own:
        return ""
    kids = children.get(element.element_id)
    if not kids:
        return own
    if element.type == "frame":
        layout = element.layout if isinstance(element.layout, Mapping) else {}
        child_flow = str(layout.get("mode", "absolute")) in {"flex-row", "flex-column", "grid"}
        inner = "".join(
            _markup_with_children(kid, children, flow=child_flow, asset_lookup=asset_lookup)
            for kid in kids if kid.visible
        )
        return _wrap_frame_children(own, inner) if inner else own
    # Non-frame parents cannot legally contain HTML children (e.g. an <svg>
    # root): render them as siblings at the parent level instead.
    return own + "".join(
        _markup_with_children(kid, children, flow=flow, asset_lookup=asset_lookup)
        for kid in kids if kid.visible
    )


def _wrap_frame_children(frame_markup: str, inner: str) -> str:
    """Place child markup inside a frame div immediately after its opening tag."""
    open_end = frame_markup.find(">", frame_markup.find("<div"))
    if open_end < 0:
        return frame_markup + inner
    return frame_markup[:open_end + 1] + inner + frame_markup[open_end + 1:]


def _frame_css(element: SceneElement) -> str:
    """Container CSS for a frame: CSS layout mode + background/radius."""
    layout = element.layout if isinstance(element.layout, Mapping) else {}
    mode = str(layout.get("mode", "absolute"))
    css = ""
    if mode in {"flex-row", "flex-column"}:
        css += (
            f"display:flex;flex-direction:{'row' if mode == 'flex-row' else 'column'};"
            f"gap:{_clamp(layout.get('gap', 0.0), 0.0, 500.0, 0.0):.4f}px;"
        )
        justify = str(layout.get("justify", "start"))
        css += f"justify-content:{_JUSTIFY_CSS.get(justify, 'flex-start')};"
        align = str(layout.get("align", "stretch"))
        css += f"align-items:{_ALIGN_CSS.get(align, 'stretch')};"
    elif mode == "grid":
        columns = int(_clamp(layout.get("columns", 2), 1.0, 16.0, 2.0))
        rows = layout.get("rows")
        css += (
            f"display:grid;grid-template-columns:repeat({columns},1fr);"
            f"gap:{_clamp(layout.get('gap', 0.0), 0.0, 500.0, 0.0):.4f}px;"
        )
        if isinstance(rows, int) and not isinstance(rows, bool) and 1 <= rows <= 16:
            css += f"grid-template-rows:repeat({rows},1fr);"
        justify = str(layout.get("justify", "start"))
        css += f"justify-content:{_JUSTIFY_CSS.get(justify, 'flex-start')};"
        align = str(layout.get("align", "stretch"))
        css += f"align-items:{_ALIGN_CSS.get(align, 'stretch')};"
    padding = _clamp(layout.get("padding", 0.0), 0.0, 500.0, 0.0)
    if padding > 0.0:
        css += f"padding:{padding:.4f}px;"
    background = _color(element.style.get("fill", element.style.get("color")), "")
    if background:
        css += f"background:{background};"
    radius = _clamp(element.style.get("corner_radius", 0.0), 0.0, 500.0, 0.0)
    if radius > 0.0:
        css += f"border-radius:{radius:.4f}px;"
    return css


def _flow_css(element: SceneElement) -> str:
    """Item CSS for a child placed by a flex/grid frame instead of x/y."""
    layout = element.layout if isinstance(element.layout, Mapping) else {}
    css = "position:relative;"
    width_mode = str(layout.get("width_mode", "fixed"))
    height_mode = str(layout.get("height_mode", "fixed"))
    transform = element.transform
    if width_mode == "fill":
        css += "flex:1 1 auto;width:auto;"
    elif width_mode == "hug":
        css += "width:auto;"
    else:
        css += f"width:{_number(transform.width)}px;"
    if height_mode == "fill":
        css += "align-self:stretch;height:auto;"
    elif height_mode == "hug":
        css += "height:auto;"
    else:
        css += f"height:{_number(transform.height)}px;"
    return css


def _clamp(value: object, minimum: float, maximum: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _common_style(element: SceneElement, *, flow: bool = False) -> str:
    """Position/size/effect CSS shared by every native overlay node."""
    transform = element.transform
    style = element.style
    if flow:
        css = _flow_css(element)
    else:
        css = (
            f'position:absolute;left:{_number(transform.x)}px;top:{_number(transform.y)}px;'
            f'width:{_number(transform.width)}px;height:{_number(transform.height)}px;'
        )
    css += f'opacity:{_clamp(style.get("opacity", 1.0), 0.0, 1.0, 1.0):.4f};'
    css += (
        f'transform-origin:0 0;transform:rotate({_number(transform.rotation)}deg) '
        f'scale({_number(transform.scale_x, 1.0)}, {_number(transform.scale_y, 1.0)});'
    )
    blend = str(style.get("blend_mode", "normal")).lower()
    if blend in _BLEND_MODES and blend != "normal":
        css += f"mix-blend-mode:{blend};"
    blur = _clamp(style.get("blur", 0.0), 0.0, 100.0, 0.0)
    if blur > 0.0:
        css += f"filter:blur({blur:.4f}px);"
    shadow = _shadow_css(style)
    if shadow:
        css += shadow
    return css + "pointer-events:none;"


def _shadow_css(style: dict) -> str:
    """Emit a box-shadow only when the layer actually configures one."""
    color = str(style.get("shadow_color", "") or "")
    if not _HEX_COLOR.fullmatch(color):
        return ""
    x = _clamp(style.get("shadow_x", 0.0), -300.0, 300.0, 0.0)
    y = _clamp(style.get("shadow_y", 0.0), -300.0, 300.0, 0.0)
    blur = _clamp(style.get("shadow_blur", 0.0), 0.0, 200.0, 0.0)
    if x == 0.0 and y == 0.0 and blur == 0.0:
        return ""
    return f"box-shadow:{x:.4f}px {y:.4f}px {blur:.4f}px {color};"


def _number(value: object, default: float = 0.0) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return f"{default:.4f}"


def _int(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _color(value: object, default: str) -> str:
    candidate = str(value or default)
    return candidate if _HEX_COLOR.fullmatch(candidate) else default


def _element_markup(
    element: SceneElement,
    *,
    flow: bool = False,
    asset_lookup: AssetLookup | None = None,
) -> str:
    style = element.style
    common = _common_style(element, flow=flow)
    element_id = escape(element.element_id, quote=True)
    if element.type == "frame":
        return (
            f'<div data-topos-native-element="{element_id}" '
            f'style="{common}{_frame_css(element)}"></div>'
        )
    if element.type in {"text", "formula"}:
        if element.type == "formula":
            text = formula_to_plain_text(element.content)
            default_family, default_size = "serif", 28.0
        else:
            text = element.content.get("text", "") if isinstance(element.content, dict) else element.content
            default_family, default_size = "sans-serif", 32.0
        font = str(style.get("font_family", default_family))
        font = font if _FONT.fullmatch(font) else default_family
        weight = str(style.get("font_weight", "normal"))
        weight = weight if _FONT_WEIGHT.fullmatch(weight) else "normal"
        align = str(style.get("text_align", "left")).lower()
        align = align if align in _TEXT_ALIGN else "left"
        return (
            f'<div data-topos-native-element="{element_id}" style="{common}'
            f'color:{_color(style.get("color", style.get("fill")), "#ffffff")};'
            f'font-family:{escape(font, quote=True)};'
            f'font-size:{_clamp(style.get("font_size", default_size), 1.0, 512.0, default_size):.4f}px;'
            f'font-weight:{weight};'
            f'text-align:{align};'
            f'line-height:{_clamp(style.get("line_height", 1.2), 0.1, 8.0, 1.2):.4f};'
            f'letter-spacing:{_clamp(style.get("letter_spacing", 0.0), -50.0, 200.0, 0.0):.4f}px;'
            'white-space:pre-wrap;">'
            f'{escape(str(text or ""))}</div>'
        )
    if element.type == "image":
        uri = asset_lookup(element.asset_ref or "") if asset_lookup and element.asset_ref else None
        if uri:
            return (
                f'<img data-topos-native-element="{element_id}" src="{escape(uri, quote=True)}" '
                f'style="{common}object-fit:contain;" alt="" draggable="false"/>'
            )
        # A missing asset must stay visible and diagnosable, never silently
        # drop the layer: mirror the SVG renderer's placeholder plate.
        ref = escape(str(element.asset_ref or "?"), quote=True)
        return (
            f'<div data-topos-native-element="{element_id}" style="{common}'
            'background:#7f1d1d;border:2px solid #fca5a5;box-sizing:border-box;'
            'display:flex;align-items:center;justify-content:center;'
            'color:#fca5a5;font-family:sans-serif;font-size:14px;text-align:center;">'
            f'missing image asset: {ref}</div>'
        )
    if element.type == "shape" and isinstance(element.content, dict):
        inner = _shape_markup(element)
    elif element.type in {"svg", "chart", "draw"}:
        inner = _vector_fragment_markup(element)
    else:
        return ""
    transform_width, transform_height = element.transform.width, element.transform.height
    return (
        f'<svg data-topos-native-element="{element_id}" style="{common}overflow:visible;" '
        f'viewBox="0 0 {_number(transform_width)} {_number(transform_height)}" '
        'preserveAspectRatio="none" xmlns="http://www.w3.org/2000/svg">'
        f'{inner}</svg>'
    )


def _vector_fragment_markup(element: SceneElement) -> str:
    """Inline SVG payload for svg/chart/draw elements, matching the SVG path."""
    from .svg_renderer import sanitize_inline_svg
    if element.type == "svg":
        return sanitize_inline_svg(str(element.content))
    if element.type == "chart":
        content = element.content if isinstance(element.content, Mapping) else {}
        try:
            chart = ChartData.from_records(content.get("records", [])) if "records" in content \
                else ChartData.from_json(json.dumps(content.get("data", content)))
            chart_type = str(content.get("chart_type", "bar"))
            color = _color(content.get("color", element.style.get("color", "#3b82f6")), "#3b82f6")
            return render_chart_svg(
                chart, element.transform.width, element.transform.height,
                chart_type=chart_type, color=color,
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
    if element.type == "draw":
        content = element.content if isinstance(element.content, Mapping) else {}
        color = _color(content.get("color", element.style.get("color", "#f59e0b")), "#f59e0b")
        return render_draw_svg(
            content, element.transform.width, element.transform.height, color=color,
        )
    return ""


def _shape_markup(element: SceneElement) -> str:
    content = element.content
    assert isinstance(content, dict)
    width, height = element.transform.width, element.transform.height
    kind = str(content.get("kind", "rect"))
    style = element.style
    fill = _color(style.get("fill", style.get("color")), "#ff8800")
    stroke = _color(style.get("stroke"), "#000000")
    stroke_width = _number(style.get("stroke_width", 0.0))
    fill_opacity = _number(style.get("fill_opacity", 1.0), 1.0)
    attrs = f'fill="{fill}" fill-opacity="{fill_opacity}" stroke="{stroke}" stroke-width="{stroke_width}"'
    if kind == "rounded_rect":
        radius = _number(content.get("corner_radius", content.get("radius", 0.0)))
        return f'<rect x="0" y="0" width="{_number(width)}" height="{_number(height)}" rx="{radius}" ry="{radius}" {attrs}/>'
    if kind == "ellipse":
        return f'<ellipse cx="{_number(width / 2)}" cy="{_number(height / 2)}" rx="{_number(width / 2)}" ry="{_number(height / 2)}" {attrs}/>'
    if kind == "line":
        return f'<line x1="0" y1="0" x2="{_number(width)}" y2="{_number(height)}" {attrs} fill="none"/>'
    if kind == "arrow":
        head = min(width, height, 28.0)
        return (
            f'<line x1="0" y1="0" x2="{_number(width)}" y2="{_number(height)}" {attrs} fill="none"/>'
            f'<polygon points="{_number(width)},{_number(height)} {_number(width - head)},{_number(height)} '
            f'{_number(width)},{_number(height - head)}" fill="{stroke}"/>'
        )
    if kind == "triangle":
        return f'<polygon points="{_number(width / 2)},0 {_number(width)},{_number(height)} 0,{_number(height)}" {attrs}/>'
    if kind == "polygon":
        points = regular_polygon_points(_int(content.get("sides", 6), 6), width, height)
        return f'<polygon points="{_points(points)}" {attrs}/>'
    if kind == "star":
        points = star_points(
            _int(content.get("points", 5), 5),
            _float(content.get("inner_ratio", 0.45), 0.45), width, height,
        )
        return f'<polygon points="{_points(points)}" {attrs}/>'
    if kind == "path":
        path = sanitize_path_data(content.get("d", ""))
        return f'<path d="{escape(path, quote=True)}" {attrs}/>' if path else ""
    return f'<rect x="0" y="0" width="{_number(width)}" height="{_number(height)}" {attrs}/>'


def _points(points: Iterable[tuple[float, float]]) -> str:
    return " ".join(f"{_number(x)},{_number(y)}" for x, y in points)


_ANIMATABLE_SINGLE = {
    "x": ("left", "px"), "y": ("top", "px"),
    "width": ("width", "px"), "height": ("height", "px"),
    "opacity": ("opacity", ""),
    "font_size": ("font-size", "px"),
}
_ANIMATABLE_TRANSFORM = ("scale_x", "scale_y", "rotation")
# Style properties animated through CSS on the element node itself.  Values
# come from the same KeyframeTrack sampling the evaluator uses, clamped to the
# evaluator's semantic ranges so preview and capture agree.
_STYLE_ANIMATABLE = {
    "font_size": (32.0, 0.1, 2048.0),
    "blur": (0.0, 0.0, 100.0),
    "fill_opacity": (1.0, 0.0, 1.0),
    "stroke_width": (0.0, 0.0, 100.0),
}
# fill/stroke presentation lives on the inline-SVG shape *children*; CSS on
# the wrapper would be beaten by their presentation attributes, so these
# properties target the child selector instead (CSS rules override
# presentation attributes).
_SVG_WRAPPED_TYPES = frozenset({"shape", "svg", "chart", "draw"})
_CHILD_ONLY_STYLE_PROPS = frozenset({"fill_opacity", "stroke_width"})
# Characters that are safe inside a CSS identifier (animation names are
# idents; a leading digit or hyphen would also be invalid, which the fixed
# ``topos-a-`` prefix below already rules out).
_CSS_IDENT_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def _css_safe_identifier(value: str) -> str:
    """Map an arbitrary string onto a CSS-identifier-safe token."""
    text = str(value)
    return _CSS_IDENT_UNSAFE.sub("_", text) if text else "_"


def _animation_name(element_id: str, prop: str, used: set[str]) -> str:
    """Deterministic CSS-safe ``@keyframes`` name for one element/property.

    ``element_id`` is external scene data: imported/persisted scenes may carry
    ids containing spaces, quotes, braces or semicolons, and interpolating the
    raw id into an animation name can break — or inject rules into — the whole
    overlay stylesheet (AUD-30100).  The id is mapped onto ``[A-Za-z0-9_-]``
    first; when that changes the id, or when another element already claimed
    the same safe token, a short stable hash of the raw id disambiguates so
    distinct elements never share an animation.
    """
    safe = _css_safe_identifier(element_id)
    base = f"topos-a-{safe}-{prop}"
    name = base
    if base != f"topos-a-{element_id}-{prop}" or name in used:
        digest = hashlib.sha256(f"{element_id}\x00{prop}".encode("utf-8")).hexdigest()[:8]
        name = f"{base}-{digest}"
    while name in used:  # pragma: no cover - hash-collision paranoia
        name += "x"
    used.add(name)
    return name


def native_animation_css(scene: Any, sample_step: int = 1) -> str:
    """Materialize native-layer keyframe animations as scrubbable CSS.

    Keyframes are sampled from the same ``KeyframeTrack.get_value`` the
    renderer evaluates, so the live preview at any frame tick matches the
    captured/exported frame exactly.  Playback position is driven by the
    ``--topos-time`` custom property on the overlay root: the editor updates
    one variable per timeline tick instead of patching each element.
    """
    fps = max(0.001, float(scene.fps))
    duration_frames = max(1, int(scene.duration_frames))
    blocks: list[str] = [
        "#topos-native-vector-overlay{--topos-time:0s;}"
    ]
    # CSS animation names are document-global; two distinct element ids that
    # sanitize to the same token must not collide (see _animation_name).
    used_animation_names: set[str] = set()
    for element in scene.elements:
        tracks: dict[str, Any] = {
            track.param_name: track for track in element.local_animation_tracks
        }
        for animation in getattr(scene, "animations", ()):
            if animation.element_id == element.element_id:
                tracks[animation.property] = animation.track
        groups: dict[str, tuple[tuple[str, ...], str]] = {}
        selector = f'[data-topos-native-element="{escape(element.element_id, quote=True)}"]'
        for prop in _ANIMATABLE_SINGLE:
            if prop in tracks:
                groups[_animation_name(element.element_id, prop, used_animation_names)] = ((prop,), selector)
        for prop in _STYLE_ANIMATABLE:
            if prop not in tracks or prop in _ANIMATABLE_SINGLE:
                continue
            if prop in _CHILD_ONLY_STYLE_PROPS and element.type not in _SVG_WRAPPED_TYPES:
                continue  # fill/stroke have no meaning on div/img nodes
            groups[_animation_name(element.element_id, prop, used_animation_names)] = (
                (prop,), selector + " > *" if prop in _CHILD_ONLY_STYLE_PROPS else selector,
            )
        if any(prop in tracks for prop in _ANIMATABLE_TRANSFORM):
            groups[_animation_name(element.element_id, "transform", used_animation_names)] = (
                _ANIMATABLE_TRANSFORM, selector,
            )
        if not groups:
            continue
        duration_seconds = duration_frames / fps
        for name, (props, selector) in groups.items():
            frames_css: list[str] = []
            previous: tuple[float, ...] | None = None
            values: list[tuple[int, tuple[float, ...], tuple[float, ...]]] = []
            for frame in range(0, duration_frames + 1, max(1, sample_step)):
                sampled = tuple(_sample_track_value(tracks, prop, element, frame) for prop in props)
                easing = _track_easing(tracks, props, frame)
                values.append((frame, sampled, easing))
            # Drop redundant interior samples (holds) to keep the CSS compact.
            compact: list[tuple[int, tuple[float, ...], tuple[float, ...]]] = []
            for index, item in enumerate(values):
                if 0 < index < len(values) - 1:
                    if item[1] == values[index - 1][1] == values[index + 1][1]:
                        continue
                compact.append(item)
            del previous
            for frame, sampled, easing in compact:
                pct = 100.0 * frame / duration_frames
                declaration = _css_declaration(props, sampled, element)
                frames_css.append(
                    f"{pct:.4f}%{{transform-box:view-box;{declaration}{'animation-timing-function:'+easing+';' if easing else ''}}}"
                )
            if not frames_css:
                continue
            blocks.append(f"@keyframes {name}{{{''.join(frames_css)}}}")
            blocks.append(
                f"{selector}{{animation:{name} {duration_seconds:.4f}s linear both paused;"
                f"animation-delay:calc(var(--topos-time,0s)*-1);}}"
            )
    return "".join(blocks[1:]) if len(blocks) > 1 else ""


def _sample_track_value(tracks: dict[str, Any], prop: str, element: SceneElement, frame: int) -> float:
    track = tracks.get(prop)
    if track is None:
        return float(_static_value(element, prop))
    try:
        return float(track.get_value(frame, default=float(_static_value(element, prop))))
    except (TypeError, ValueError):
        return float(_static_value(element, prop))


def _track_easing(tracks: dict[str, Any], props: tuple[str, ...], frame: int) -> str:
    """CSS timing function for the segment starting at ``frame``."""
    from src.shared.keyframe import normalized_segment_controls
    for prop in props:
        track = tracks.get(prop)
        if track is None:
            continue
        keys = track.keyframes
        for index, key in enumerate(keys):
            if key.frame != frame:
                continue
            kind = str(getattr(key.interpolation, "value", key.interpolation))
            if kind == "bezier" and index + 1 < len(keys):
                controls = normalized_segment_controls(key, keys[index + 1])
                if controls is not None:
                    x1, y1, x2, y2 = controls
                    # x is segment-clamped to [0,1] already; y may overshoot,
                    # which CSS cubic-bezier permits.
                    return f"cubic-bezier({x1:.6f},{y1:.6f},{x2:.6f},{y2:.6f})"
                return "linear"
            return {
                "linear": "linear",
                "ease_in": "cubic-bezier(0.42,0,1,1)",
                "ease_out": "cubic-bezier(0,0,0.58,1)",
                "ease_in_out": "cubic-bezier(0.42,0,0.58,1)",
                "hold": "step-end",
            }.get(kind, "linear")
        return "linear"
    return "linear"


def _static_value(element: SceneElement, prop: str) -> float:
    if prop == "opacity":
        try:
            return float(element.style.get("opacity", 1.0))
        except (TypeError, ValueError):
            return 1.0
    if prop in _STYLE_ANIMATABLE:
        default, _lo, _hi = _STYLE_ANIMATABLE[prop]
        try:
            return float(element.style.get(prop, default))
        except (TypeError, ValueError):
            return default
    return float(getattr(element.transform, prop, 0.0))


def _css_declaration(props: tuple[str, ...], sampled: tuple[float, ...], element: SceneElement) -> str:
    if props == _ANIMATABLE_TRANSFORM:
        values = dict(zip(props, sampled))
        rotation = values.get("rotation", element.transform.rotation)
        scale_x = values.get("scale_x", element.transform.scale_x)
        scale_y = values.get("scale_y", element.transform.scale_y)
        return (
            f"transform-origin:0 0;transform:rotate({rotation:.4f}deg) "
            f"scale({scale_x:.4f},{scale_y:.4f});"
        )
    prop = props[0]
    value = sampled[0]
    if prop in _STYLE_ANIMATABLE:
        _default, lo, hi = _STYLE_ANIMATABLE[prop]
        value = max(lo, min(hi, value))
        if prop == "blur":
            return f"filter:blur({value:.4f}px);"
        if prop == "fill_opacity":
            return f"fill-opacity:{value:.4f};"
        if prop == "stroke_width":
            return f"stroke-width:{value:.4f}px;"
    css_name, unit = _ANIMATABLE_SINGLE[prop]
    if prop == "opacity":
        value = max(0.0, min(1.0, value))
    return f"{css_name}:{value:.4f}{unit};"


def scene_asset_uri_lookup(scene: Any, context: Any) -> AssetLookup | None:
    """Best-effort ``asset_ref → data URI`` resolver for overlay markup.

    Mirrors the SVG renderer's resolution rules (paths must stay inside the
    render context's ``asset_root``).  Unresolvable assets never raise here:
    the overlay falls back to a visible placeholder so layers cannot vanish.
    """
    from .asset_resolver import AssetResolver
    if context is None or not getattr(context, "asset_root", None):
        return None
    try:
        resolver = AssetResolver(context)
        try:
            resolver.preflight(scene)
        except ValueError:
            pass  # keep the partial resolution table; placeholders cover the rest
    except Exception:  # noqa: BLE001 — diagnostics path must never break rendering
        return None

    def lookup(asset_ref: str) -> str | None:
        asset = resolver.asset_by_id(scene, asset_ref or "")
        if asset is None:
            return None
        try:
            return resolver.data_uri(asset)
        except OSError:
            return None

    return lookup


# A custom-property name is a plain CSS ident prefixed with "--"; values are
# either the palette's hex colours or the numeric ``<n>px`` lengths that
# ``css_variables`` emits for spacing/radius/type tokens.
_CSS_VAR_NAME = re.compile(r"^--[A-Za-z0-9_-]{1,96}$")
_CSS_VAR_VALUE = re.compile(r"^(?:#[0-9a-fA-F]{3,8}|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?(?:px|%|s|ms)?)$")


def native_overlay_css_variables(tokens: Any) -> str:
    """A trusted style rule exposing scene tokens as --topos-* variables.

    Token names and values originate from scene metadata / brand theme
    packages, i.e. external data.  Each declaration is validated before it
    enters the stylesheet (AUD-30101): names must be plain custom-property
    identifiers and values must be a hex colour or a plain number/length —
    anything else is dropped instead of being given the chance to escape its
    declaration and break (or inject into) the whole rule.
    """
    from ..core.design_tokens import css_variables
    declarations = ";".join(
        f"{name}:{value}"
        for name, value in css_variables(tokens).items()
        if _CSS_VAR_NAME.fullmatch(str(name))
        and _CSS_VAR_VALUE.fullmatch(str(value).strip())
    )
    return f"#topos-native-vector-overlay{{{declarations}}}" if declarations else ""


__all__ = [
    "native_animation_css",
    "native_overlay_css_variables",
    "native_overlay_markup",
    "scene_asset_uri_lookup",
]
