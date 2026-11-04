"""Pure presentation projection of tools that have effective node adjustments.

No renderer, Qt or project mutation. Defaults mirror Layer's zero-based primary
parameters, inverted-Y curve coordinates and warper delta payload. Source/global
NR and sizing settings are deliberately not attributed to individual Correctors.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from numbers import Real
import math
from typing import Any

TOOL_LABELS: dict[str, str] = {
    "primary": "Primary correction", "curves": "Curves", "color_mapping": "Color mapping",
    "qualifier": "Qualifier", "window": "Power Window", "blur": "Blur / Sharpen",
    "lut": "LUT", "ofx": "Effects", "key": "Key / Opacity", "keyframes": "Keyframes",
}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _data(value: Any) -> Any:
    return value.tolist() if hasattr(value, "tolist") else value


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, Real) and math.isfinite(float(value)) else default


def _nonzero(value: Any) -> bool:
    value = _data(value)
    if isinstance(value, Mapping):
        return any(_nonzero(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_nonzero(item) for item in value)
    return abs(_number(value)) > 1e-6


def _curves_changed(data: Any, *, hsl: bool = False) -> bool:
    if not isinstance(data, Mapping):
        return False
    for points in data.values():
        points = _data(points)
        if not isinstance(points, (list, tuple)) or len(points) < 2:
            continue
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                continue
            x, y = _number(point[0]), _number(point[1])
            if abs(y - (127.5 if hsl else 255.0 - x)) > 1e-5:
                return True
    return False


def _soft_clip_changed(raw: Any) -> bool:
    if not isinstance(raw, Mapping):
        return False
    channels = raw.get("channels")
    if not isinstance(channels, Mapping):
        return False
    defaults = {"low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0}
    return any(isinstance(channel, Mapping) and any(
        abs(_number(channel.get(key), default) - default) > 1e-6
        for key, default in defaults.items()) for channel in channels.values())


def _has_window(items: Any) -> bool:
    if not isinstance(items, (list, tuple)):
        return False
    return any(isinstance(item, Mapping) and item.get("visible", True)
               and _number(item.get("opacity"), 1.0) > 0.0
               and (_has_window(item.get("children")) if "children" in item else True)
               for item in items)


def detect_tool_indicators(layer_payload: Mapping[str, Any] | None,
                           node_params: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Return ordered usage badges, ignoring neutral state and disabled tools."""
    payload = dict(_mapping(layer_payload))
    params = _mapping(node_params)
    for graph_key, layer_key in (("grade", "grading_params"), ("qualifier", "qualifier_params"),
                                 ("effects", "effects_params"), ("mixer", "mixer_params"),
                                 ("soft_clip", "soft_clip_params")):
        merged = dict(_mapping(payload.get(layer_key)))
        merged.update(_mapping(params.get(graph_key)))
        payload[layer_key] = merged
    # N10：图命令域关键帧进徽标判据（此前只识别 legacy_payload 元数据）
    params_kf = _mapping(params.get("grade_keyframes"))
    if params_kf:
        merged_kf = dict(_mapping(payload.get("grade_keyframes")))
        merged_kf.update(params_kf)
        payload["grade_keyframes"] = merged_kf
    for key in ("opacity", "blend_mode"):
        if key in params:
            payload[key] = params[key]
    grade = _mapping(payload.get("grading_params"))
    result: list[str] = []
    primary_keys = ("lift", "gamma", "gain", "exposure", "temp", "temperature", "tint", "contrast",
                    "shadow", "shadows", "highlights", "saturation", "vibrance", "mid_detail",
                    "hue_shift", "lift_saturation", "gamma_saturation", "gain_saturation")
    if any(_nonzero(grade.get(key)) for key in primary_keys):
        result.append("primary")
    if (_curves_changed(payload.get("curves_data"))
            or _curves_changed(payload.get("hsl_curve_data"), hsl=True)
            or _soft_clip_changed(payload.get("curves_soft_clip"))
            or bool(_mapping(payload.get("soft_clip_params")).get("enabled", False))):
        result.append("curves")
    if _nonzero(payload.get("mixer_params")) or _nonzero(payload.get("warper_params")):
        result.append("color_mapping")
    qualifier = payload.get("qualifier_params") or {}
    if isinstance(qualifier, Mapping) and qualifier.get("enabled", False):
        result.append("qualifier")
    if _has_window(payload.get("mask_shapes")):
        result.append("window")
    fx = payload.get("effects_params") or {}
    if _nonzero(grade.get("sharpness")) or isinstance(fx, Mapping) and (
        (fx.get("enabled", False) and any(_nonzero(fx.get(key)) for key in ("sharpen", "texture", "halo", "radius")))
        or (fx.get("blur_enabled", False) and any(_nonzero(fx.get(key)) for key in ("blur_intensity", "blur_radius", "blur_detail", "blur_bokeh")))
    ):
        result.append("blur")
    if payload.get("active_lut_path") and _number(payload.get("lut_intensity"), 1.0) != 0.0:
        result.append("lut")
    plugins = payload.get("plugins") or ()
    if isinstance(plugins, (list, tuple)) and any(isinstance(plugin, Mapping) and plugin.get("enabled", True) for plugin in plugins):
        result.append("ofx")
    if abs(_number(payload.get("opacity"), 1.0) - 1.0) > 1e-6 or str(payload.get("blend_mode") or "Normal").lower() != "normal":
        result.append("key")
    if isinstance(payload.get("grade_keyframes"), Mapping) and payload["grade_keyframes"]:
        result.append("keyframes")
    return tuple(result)
