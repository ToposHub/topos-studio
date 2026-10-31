"""Authoring helpers for code-defined MG presets (``build.py`` modules).

Everything a preset declares goes through these factories so every bundled
preset produces schema-valid, renderer-verified scene JSON.  All animation is
authored as per-element ``local_animation_tracks`` — a preset then grafts as
pure elements (no scene-level animation merge), which is what makes
drag-and-drop insertion a single reversible command.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterable, Sequence

from ..core.model import (
    SCENE_SCHEMA_VERSION,
    SceneElement,
    SceneTransform,
    TeachingSceneDocument,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

_INTERPOLATIONS = {
    "linear": InterpolationType.LINEAR,
    "hold": InterpolationType.HOLD,
    "bezier": InterpolationType.BEZIER,
    "ease_in": InterpolationType.EASE_IN,
    "ease_out": InterpolationType.EASE_OUT,
    "ease_in_out": InterpolationType.EASE_IN_OUT,
}
#: Properties the evaluator can animate (evaluator._ANIMATABLE equivalent).
ANIMATABLE_PROPERTIES = frozenset({
    "x", "y", "width", "height", "scale_x", "scale_y", "rotation", "opacity",
})


def key(frame: int, value: float, interpolation: str = "linear") -> Keyframe:
    try:
        mode = _INTERPOLATIONS[interpolation]
    except KeyError:
        raise ValueError(f"unsupported interpolation: {interpolation!r}") from None
    return Keyframe(frame=int(frame), value=float(value), interpolation=mode)


def track(param: str, *keyframes: Keyframe, min_val: float | None = None,
          max_val: float | None = None) -> KeyframeTrack:
    """Build a track whose clamp range always covers its own keyframes.

    ``KeyframeTrack`` defaults to ``[0, 1]``; forgetting to widen it silently
    clamps positional values, so the helper derives bounds from the keys.
    """
    if param not in ANIMATABLE_PROPERTIES:
        raise ValueError(f"non-animatable preset property: {param!r}")
    if not keyframes:
        raise ValueError("a preset track needs at least one keyframe")
    frames = [item.frame for item in keyframes]
    if frames != sorted(frames) or len(set(frames)) != len(frames):
        raise ValueError(f"track {param!r} keyframes must be sorted and unique")
    values = [float(item.value) for item in keyframes]
    lo = min(values) if min_val is None else float(min_val)
    hi = max(values) if max_val is None else float(max_val)
    if hi <= lo:
        hi = lo + 1.0
    return KeyframeTrack(param_name=param, keyframes=list(keyframes),
                         min_val=min(lo, 0.0), max_val=max(hi, 1.0))


def glow_effect(color: str, radius: float = 18.0, opacity: float = 0.85) -> dict[str, Any]:
    """Coloured zero-offset drop shadow — the SVG-safe stand-in for a glow."""
    return {"effect_type": "shadow", "version": 1,
            "params": {"dx": 0.0, "dy": 0.0, "radius": float(radius),
                       "opacity": float(opacity), "color": color}}


def shadow_effect(color: str = "#000000", dx: float = 0.0, dy: float = 6.0,
                  radius: float = 14.0, opacity: float = 0.45) -> dict[str, Any]:
    return {"effect_type": "shadow", "version": 1,
            "params": {"dx": float(dx), "dy": float(dy), "radius": float(radius),
                       "opacity": float(opacity), "color": color}}


def blur_effect(radius: float) -> dict[str, Any]:
    return {"effect_type": "blur", "version": 1, "params": {"radius": float(radius)}}


def linear_gradient(*stops: tuple[float, str], angle: float = 90.0,
                    opacities: Sequence[float] | None = None) -> dict[str, Any]:
    parsed = [
        {"position": float(position), "color": color,
         "opacity": 1.0 if opacities is None else float(opacities[index])}
        for index, (position, color) in enumerate(stops)
    ]
    return {"kind": "linear", "angle": float(angle), "stops": parsed}


def radial_gradient(*stops: tuple[float, str],
                    opacities: Sequence[float] | None = None) -> dict[str, Any]:
    parsed = [
        {"position": float(position), "color": color,
         "opacity": 1.0 if opacities is None else float(opacities[index])}
        for index, (position, color) in enumerate(stops)
    ]
    return {"kind": "radial", "stops": parsed}


def text_element(element_id: str, x: float, y: float, content: str, *,
                 size: float = 48.0, color: str = "#ffffff", weight: str = "400",
                 family: str = "sans-serif", anchor: str = "start",
                 letter_spacing: float | None = None, z: int = 0,
                 width: float | None = None, height: float | None = None,
                 effects: Iterable[dict[str, Any]] = (), tracks: Iterable[KeyframeTrack] = (),
                 opacity: float | None = None) -> SceneElement:
    style: dict[str, Any] = {
        "color": color, "font_size": float(size), "font_family": family,
        "font_weight": weight, "text_anchor": anchor,
    }
    if letter_spacing is not None:
        style["letter_spacing"] = float(letter_spacing)
    if opacity is not None:
        style["opacity"] = float(opacity)
    effects = tuple(effects)
    if effects:
        style["effects"] = list(effects)
    return SceneElement(
        element_id=element_id, type="text", z_index=int(z),
        transform=SceneTransform(x=float(x), y=float(y),
                                 width=float(width if width is not None else size * max(len(content), 1) * 0.6),
                                 height=float(height if height is not None else size * 1.4)),
        style=style, content=str(content), local_animation_tracks=_as_tracks(tracks),
    )


def shape_element(element_id: str, x: float, y: float, w: float, h: float, *,
                  kind: str = "rect", fill: str = "#ff8800", z: int = 0,
                  stroke: str | None = None, stroke_width: float = 0.0,
                  corner_radius: float | None = None, fill_opacity: float | None = None,
                  gradient: dict[str, Any] | None = None, rotation: float = 0.0,
                  scale: float = 1.0, sides: int | None = None, points: int | None = None,
                  inner_ratio: float | None = None, d: str | None = None,
                  label: str | None = None, effects: Iterable[dict[str, Any]] = (),
                  tracks: Iterable[KeyframeTrack] = (), visible: bool = True) -> SceneElement:
    content: dict[str, Any] = {"kind": kind}
    if corner_radius is not None:
        content["corner_radius"] = float(corner_radius)
    if sides is not None:
        content["sides"] = int(sides)
    if points is not None:
        content["points"] = int(points)
    if inner_ratio is not None:
        content["inner_ratio"] = float(inner_ratio)
    if d is not None:
        content["d"] = d
    if label is not None:
        content["label"] = label
    style: dict[str, Any] = {"fill": fill, "stroke": stroke or "#000000",
                             "stroke_width": float(stroke_width)}
    if fill_opacity is not None:
        style["fill_opacity"] = float(fill_opacity)
    if gradient is not None:
        style["gradient"] = gradient
    effects = tuple(effects)
    if effects:
        style["effects"] = list(effects)
    return SceneElement(
        element_id=element_id, type="shape", z_index=int(z), visible=visible,
        transform=SceneTransform(x=float(x), y=float(y), width=float(w), height=float(h),
                                 scale_x=float(scale), scale_y=float(scale),
                                 rotation=float(rotation)),
        style=style, content=content, local_animation_tracks=_as_tracks(tracks),
    )


def _as_tracks(tracks: Any) -> tuple[KeyframeTrack, ...]:
    """Accept a single track, a tuple or a list — `(track(...))` is a classic
    missing-comma trap that would otherwise raise deep inside SceneElement."""
    if isinstance(tracks, KeyframeTrack):
        return (tracks,)
    return tuple(tracks)


def scene_document(elements: Sequence[SceneElement], *, name: str,
                   scene_id: str | None = None, duration_frames: int = 72,
                   fps: int = 24, width: int = 1920, height: int = 1080,
                   background: str = "#0b1220") -> TeachingSceneDocument:
    # The validator requires every keyframe inside [0, duration_frames); a
    # loop that ends exactly on the last frame (key at frame == duration) is a
    # natural authoring pattern, so grow the duration instead of rejecting it.
    max_frame = max(
        (keyframe.frame for element in elements
         for track_item in _as_tracks(element.local_animation_tracks)
         for keyframe in track_item.keyframes),
        default=-1,
    )
    return TeachingSceneDocument(
        scene_id=scene_id or f"preset_{uuid.uuid4().hex}",
        schema_version=SCENE_SCHEMA_VERSION,
        name=str(name), width=int(width), height=int(height),
        fps_num=int(fps), fps_den=1,
        duration_frames=max(int(duration_frames), max_frame + 1),
        background={"color": background}, elements=tuple(elements),
        animations=(), metadata={},
    )


__all__ = [
    "ANIMATABLE_PROPERTIES", "blur_effect", "glow_effect", "key", "linear_gradient",
    "radial_gradient", "scene_document", "shadow_effect", "shape_element", "text_element",
    "track",
]
