"""Frame-accurate evaluation of Teaching Scene documents."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Mapping

from ..core.contracts import AnimationFrame
from ..core.model import SceneElement, SceneTransform, TeachingSceneDocument
from src.shared.keyframe import KeyframeTrack


@dataclass(frozen=True)
class EvaluatedElement:
    """An element with all frame-dependent values resolved."""

    source: SceneElement
    transform: SceneTransform
    opacity: float
    style: Mapping[str, Any]


@dataclass(frozen=True)
class EvaluatedScene:
    """Immutable scene snapshot consumed by preview and export renderers."""

    source: TeachingSceneDocument
    frame: AnimationFrame
    elements: tuple[EvaluatedElement, ...]
    diagnostics: tuple[str, ...] = ()


# Style properties keyframes/expressions may drive in addition to transform
# fields.  Sampling them here means the SVG renderer, the DOM overlay capture
# (which re-builds markup from this evaluated style every frame) and the CSS
# animation path all read one per-frame truth.  (lo, hi) are semantic clamps
# mirroring the DOM overlay's static ranges.
ANIMATABLE_STYLE_PROPERTIES: dict[str, tuple[float, float]] = {
    "font_size": (0.1, 2048.0),
    "blur": (0.0, 100.0),
    "fill_opacity": (0.0, 1.0),
    "stroke_width": (0.0, 100.0),
}
_STYLE_DEFAULTS: dict[str, float] = {
    "font_size": 32.0, "blur": 0.0, "fill_opacity": 1.0, "stroke_width": 0.0,
}


def _track_value(track: KeyframeTrack, frame: int, default: float) -> float:
    return float(track.get_value(frame, default=default))


def _float_or(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _tracks_for(element: SceneElement, scene: TeachingSceneDocument) -> dict[str, KeyframeTrack]:
    tracks = {track.param_name: track for track in element.local_animation_tracks}
    for animation in scene.animations:
        if animation.element_id == element.element_id:
            tracks[animation.property] = animation.track
    return tracks


def _value(tracks: Mapping[str, KeyframeTrack], names: tuple[str, ...], frame: int, default: float) -> float:
    for name in names:
        track = tracks.get(name)
        if track is not None:
            return _track_value(track, frame, default)
    return default


def _decompose_affine(m: Any) -> tuple[float, float, float, float, float]:
    """Recover (x, y, scale_x, scale_y, rotation_deg) from an Affine2d.

    Exact for rotate + non-uniform-scale compositions (the only transforms the
    scene model can express); shear, if it ever appeared, is folded into the
    scale estimate.  Used to bring a composed world-space transform back into the
    SceneTransform fields the SVG renderer reads.
    """
    x, y = m.e, m.f
    scale_x = math.sqrt(m.a * m.a + m.b * m.b)
    scale_y = math.sqrt(m.c * m.c + m.d * m.d)
    rotation = math.degrees(math.atan2(m.b, m.a))
    return x, y, scale_x, scale_y, rotation


def evaluate(scene: TeachingSceneDocument, frame: AnimationFrame | int) -> EvaluatedScene:
    """Resolve one integer frame without consulting a wall clock.

    Frames are deliberately bounded to the scene's half-open duration. This
    catches an off-by-one at the shared preview/export boundary early.

    Two scene-graph behaviours are resolved here so every renderer sees the same
    per-frame truth:

    * **Design tokens (TM-05)** — ``$category.key`` style refs are resolved
      against the default token set, so neither renderer emits a literal
      ``$...`` string.  A no-op for already-literal styles; ``__locked__`` is
      honoured.
    * **Hierarchy (SG-01)** — when the scene carries a Hierarchy (parent links
      in ``scene.metadata``), a child's world transform composes its ancestors'
      transforms ("move a parent group and all children follow") and a hidden
      ancestor hides its descendants.  Scenes without a Hierarchy — the common
      case — take the same code path with zero composition, so their output is
      byte-identical to the flat-element behaviour.
    """
    if not isinstance(scene, TeachingSceneDocument):
        raise TypeError("evaluate expects a TeachingSceneDocument")
    current = frame if isinstance(frame, AnimationFrame) else AnimationFrame(frame, scene.fps)
    if abs(current.fps - scene.fps) > 1e-9:
        raise ValueError("frame fps must match scene fps")
    if current.frame >= scene.duration_frames:
        raise ValueError("frame is outside the scene duration")

    from ..core.design_tokens import default_brand_tokens, resolve_style
    from ..core.components import resolve_component_updates, resolve_responsive_updates
    from ..core.constraints import read_base_canvas, read_constraints, resolve_constraint_transform
    from ..core.hierarchy import effective_visibility, read_hierarchy, world_affine
    from ..core.layout import read_scene_layout
    tokens = default_brand_tokens()
    hierarchy = read_hierarchy(scene.metadata)
    has_hierarchy = bool(hierarchy.parents) or bool(hierarchy.hidden) or bool(hierarchy.solo)
    scene_layout = read_scene_layout(scene.metadata)
    from ..motion.expression import ExpressionContext, ExpressionError, evaluate_expression
    marker_values = {marker.label: marker.frame for marker in scene.markers if marker.label}
    expression_diagnostics: list[str] = []

    # A full scene is a sequence of artboards.  At any frame only the page
    # owning that frame is renderable; editor-only page views already contain
    # one page and therefore naturally take the same path.
    render_elements = scene.elements
    if scene.pages:
        active_page = scene.pages[scene.page_index_for_frame(current.frame)]
        render_elements = tuple(
            element for element in scene.elements
            if element.page_id == active_page.page_id
        )

    # First pass: each element's LOCAL transform + opacity (from keyframes).  The
    # local transforms are also the ancestor inputs for world-space composition.
    local_transforms: dict[str, SceneTransform] = {}
    local_opacity: dict[str, float] = {}
    local_style_overrides: dict[str, dict[str, float]] = {}
    effective_elements: dict[str, SceneElement] = {}
    for element in render_elements:
        updates = resolve_component_updates(scene.metadata, element.element_id)
        responsive = resolve_responsive_updates(scene.metadata, scene.width, scene.height, element.element_id)
        updates = {**updates, **responsive}
        normalized: dict[str, Any] = {}
        for key, value in updates.items():
            if key == "transform" and isinstance(value, Mapping):
                normalized[key] = SceneTransform.from_dict(value)
            elif key == "style" and isinstance(value, Mapping):
                # Same dirty-data tolerance as the merged-style pass below:
                # authored ``"style": null`` must not crash dict(None) here.
                base = dict(element.style) if isinstance(element.style, Mapping) else {}
                normalized[key] = {**base, **dict(value)}
            elif key in {"content", "visible", "z_index", "asset_ref", "template_id"}:
                normalized[key] = value
        effective = replace(element, **normalized) if normalized else element
        effective_elements[element.element_id] = effective
        # AUD-29903/29904: ``from_dict`` persists authored ``"style": null``
        # verbatim, and non-numeric opacity is unvalidated.  Degrade both to
        # "no style" / "default value" like every other dirty input instead of
        # crashing the whole frame evaluation.
        style_map: Mapping[str, Any] = effective.style if isinstance(effective.style, Mapping) else {}
        tracks = _tracks_for(effective, scene)
        expression_values: dict[str, float] = {}
        for expression in scene.expressions:
            if expression.element_id != element.element_id or not expression.enabled:
                continue
            try:
                value = evaluate_expression(
                    expression.source,
                    ExpressionContext(
                        frame=current.frame,
                        fps=current.fps,
                        seed=expression.seed,
                        markers=marker_values,
                    ),
                )
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                    raise ExpressionError("expression result must be a finite number")
                expression_values[expression.property] = float(value)
            except ExpressionError as exc:
                expression_values[expression.property] = float(expression.default)
                expression_diagnostics.append(
                    f"expression {expression.expression_id} fallback at frame {current.frame}: {exc}"
                )

        def resolved_value(names: tuple[str, ...], default: float) -> float:
            for name in names:
                if name in expression_values:
                    return expression_values[name]
            return _value(tracks, names, current.frame, default)

        transform = effective.transform
        x = resolved_value(("x", "position.x", "transform.x"), transform.x)
        y = resolved_value(("y", "position.y", "transform.y"), transform.y)
        width = resolved_value(("width", "transform.width"), transform.width)
        height = resolved_value(("height", "transform.height"), transform.height)
        scale_x = resolved_value(("scale_x", "scale.x", "transform.scale_x"), transform.scale_x)
        scale_y = resolved_value(("scale_y", "scale.y", "transform.scale_y"), transform.scale_y)
        rotation = resolved_value(("rotation", "transform.rotation"), transform.rotation)
        opacity = max(0.0, min(1.0, resolved_value(("opacity",), _float_or(style_map.get("opacity", 1.0), 1.0))))
        local_transforms[element.element_id] = SceneTransform(
            x=x, y=y, width=max(0.0, width), height=max(0.0, height),
            scale_x=max(0.0001, scale_x), scale_y=max(0.0001, scale_y), rotation=rotation)
        local_opacity[element.element_id] = opacity
        style_overrides: dict[str, float] = {}
        for style_prop, (lo, hi) in ANIMATABLE_STYLE_PROPERTIES.items():
            if style_prop not in tracks and style_prop not in expression_values:
                continue
            static_default = _float_or(style_map.get(style_prop), _STYLE_DEFAULTS[style_prop])
            animated = resolved_value((style_prop,), static_default)
            style_overrides[style_prop] = max(lo, min(hi, animated))
        if style_overrides:
            local_style_overrides[element.element_id] = style_overrides

    # Auto Layout (SG-03): if the scene carries a SceneLayout, re-resolve the
    # group's rects against the live canvas every frame.  Preferred (HUG) sizes
    # are the elements' own transform dimensions.  A laid-out element takes its
    # position+size from the resolved rect; its scale/rotation/opacity still
    # animate, so an entrance (fade/scale) composes on top of the laid-out box.
    layout_rects: dict[str, Any] = {}
    if scene_layout is not None:
        preferred = {eid: (t.width, t.height) for eid, t in local_transforms.items()}
        try:
            layout_rects = scene_layout.resolve(scene.width, scene.height, preferred)
        except Exception:  # noqa: BLE001 - a layout error must never break rendering
            layout_rects = {}

    # R1 canvas constraints: when the live canvas differs from the authored
    # one (breakpoint / quick ratio preview), declared constraints re-place
    # the element's local transform, so hierarchy composition and every
    # renderer downstream see the constrained truth.  Auto Layout members
    # (already re-resolved against the live canvas above) are skipped.
    # Legacy elements without constraints — and scenes without a recorded
    # base canvas — keep their absolute coordinates byte-for-byte.
    base_canvas = read_base_canvas(scene.metadata)
    if base_canvas is not None and (
        abs(base_canvas[0] - scene.width) > 1e-6 or abs(base_canvas[1] - scene.height) > 1e-6
    ):
        for eid, source in effective_elements.items():
            if eid in layout_rects:
                continue
            constraints = read_constraints(source.layout)
            if constraints is not None:
                local_transforms[eid] = resolve_constraint_transform(
                    base_canvas[0], base_canvas[1], scene.width, scene.height,
                    local_transforms[eid], constraints)

    # Second pass: compose world transform (hierarchy), apply visibility and
    # resolve token refs.  Parentless elements keep their local transform exactly.
    resolved: list[EvaluatedElement] = []
    for element in render_elements:
        local_t = local_transforms[element.element_id]
        opacity = local_opacity[element.element_id]
        if element.element_id in layout_rects:
            # Auto Layout owns position+size for this element; the element's own
            # scale/rotation still apply (entrance animations compose on top).
            rect = layout_rects[element.element_id]
            transform = SceneTransform(x=rect.x, y=rect.y, width=max(0.0, rect.width),
                                       height=max(0.0, rect.height),
                                       scale_x=local_t.scale_x, scale_y=local_t.scale_y,
                                       rotation=local_t.rotation)
        elif has_hierarchy and hierarchy.ancestors(element.element_id):
            world = world_affine(element.element_id, local_transforms, hierarchy)
            wx, wy, wsx, wsy, wrot = _decompose_affine(world)
            transform = SceneTransform(x=wx, y=wy, width=local_t.width, height=local_t.height,
                                       scale_x=max(0.0001, wsx), scale_y=max(0.0001, wsy), rotation=wrot)
        else:
            transform = local_t
        if has_hierarchy and not effective_visibility(element.element_id, hierarchy):
            opacity = 0.0
        base_style = effective_elements[element.element_id].style
        # AUD-29904: non-Mapping styles (authored ``"style": null``) degrade to
        # "no style" here too — the old ``dict(base_style)`` fallback raised
        # TypeError for exactly the dirty value it was defending against.
        if not isinstance(base_style, Mapping):
            base_style = {}
        merged_style = dict(resolve_style(base_style, tokens))
        merged_style.update(local_style_overrides.get(element.element_id, {}))
        resolved.append(EvaluatedElement(
            source=effective_elements[element.element_id],
            transform=transform,
            opacity=opacity,
            style=merged_style,
        ))
    return EvaluatedScene(
        source=scene,
        frame=current,
        elements=tuple(resolved),
        diagnostics=tuple(expression_diagnostics),
    )
