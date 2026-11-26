"""Versioned, scene-referred Chromogen controls and pointwise color transform."""
from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import numpy as np

CHROMOGEN_KEY = "chromogen"
CHROMOGEN_VERSION = 1
CHROMOGEN_MAX_STAGES = 24
CHROMOGEN_STAGE_FLOATS = 24


@dataclass(frozen=True)
class ControlSpec:
    key: str
    label: str
    minimum: float
    maximum: float
    default: float
    step: float = 0.01
    decimals: int = 2
    suffix: str = ""


@dataclass(frozen=True)
class StageSpec:
    family: str
    label: str
    controls: tuple[ControlSpec, ...]
    supports_modulation: bool = True


_ZONE = ControlSpec("zone", "Zone", -1.0, 1.0, 0.0, decimals=3)
_PIVOT = ControlSpec("pivot", "Pivot", -6.0, 6.0, 0.0, suffix=" EV")
_CHROMA = ControlSpec("chroma", "Chroma", -1.0, 1.0, 0.0, decimals=3)
MODULATION_CONTROLS = (_ZONE, _PIVOT, _CHROMA)
_HUE = ControlSpec("hue", "Hue", -180.0, 180.0, 0.0, 0.1, 1, "°")
_ROTATE = ControlSpec("rotate", "Rotate", -180.0, 180.0, 0.0, 0.1, 1, "°")
_BRILLIANCE = ControlSpec("brilliance", "Brilliance", 0.0, 1.0, 0.5)
_FALLOFF = ControlSpec("falloff", "Falloff", 0.0, 1.0, 0.5, decimals=3)

STAGE_SPECS = {
    "colour_saturation": StageSpec("fundamental", "Colour Saturation", (
        ControlSpec("rg", "R/G", 0.0, 3.0, 1.0),
        ControlSpec("yb", "Y/B", 0.0, 3.0, 1.0), _ROTATE, _BRILLIANCE)),
    "colour_crosstalk": StageSpec("fundamental", "Colour Crosstalk", (
        ControlSpec("r_to_yb", "R → Y/B", -1.0, 1.0, 0.0),
        ControlSpec("g_to_yb", "G → Y/B", -1.0, 1.0, 0.0),
        ControlSpec("y_to_rg", "Y → R/G", -1.0, 1.0, 0.0),
        ControlSpec("b_to_rg", "B → R/G", -1.0, 1.0, 0.0), _ROTATE, _BRILLIANCE)),
    "highlight_bleach": StageSpec("fundamental", "Highlight Bleach", (
        ControlSpec("red", "R", 0.0, 2.0, 0.0),
        ControlSpec("green", "G", 0.0, 2.0, 0.0),
        ControlSpec("yellow", "Y", 0.0, 2.0, 0.0),
        ControlSpec("blue", "B", 0.0, 2.0, 0.0),
        ControlSpec("pivot", "Pivot", -6.0, 6.0, -2.0, suffix=" EV"),
        _FALLOFF, _CHROMA, _ROTATE, _BRILLIANCE), False),
    "contrast_boost": StageSpec("fundamental", "Contrast Boost", (
        ControlSpec("contrast", "Contrast Boost", -1.0, 2.0, 0.0),
        ControlSpec("grey_pivot", "Grey Pivot", -6.0, 3.0, 0.0, suffix=" EV"),
        ControlSpec("highlight_pivot", "Highlight Pivot", 1.0, 12.0, 6.0, suffix=" EV"),
        ControlSpec("chroma", "Chroma", 0.0, 1.0, 0.5)), False),
    "brilliance_reduction": StageSpec("advanced", "Brilliance Reduction", (
        ControlSpec("amount", "Amount", 0.0, 1.0, 0.0, decimals=3),
        ControlSpec("chroma", "Chroma", 0.0, 1.0, 0.6, decimals=3),
        ControlSpec("pivot", "Pivot", -2.0, 8.0, 0.35, suffix=" EV"),
        _FALLOFF), False),
    "neutral_tint": StageSpec("advanced", "Neutral Tint", (
        _HUE, ControlSpec("amount", "Amount", -2.0, 2.0, 0.0, decimals=3),
        ControlSpec("pivot", "Pivot", -6.0, 6.0, -0.7, suffix=" EV"),
        ControlSpec("falloff", "Falloff", 0.0, 3.0, 1.0, decimals=3),
        ControlSpec("chroma", "Chroma", 0.0, 3.0, 1.0, decimals=3), _BRILLIANCE), False),
    "sector_brightness": StageSpec("sector", "Sector Brightness", (
        _HUE, ControlSpec("brightness", "Brightness", -4.0, 4.0, 0.0),
        _FALLOFF, _BRILLIANCE)),
    "sector_saturation": StageSpec("sector", "Sector Saturation", (
        _HUE, ControlSpec("saturation", "Saturation", 0.0, 3.0, 1.0),
        _FALLOFF, _BRILLIANCE)),
    "sector_skew": StageSpec("sector", "Sector Skew", (
        _HUE, ControlSpec("skew", "Skew", -1.0, 1.0, 0.0), _FALLOFF, _BRILLIANCE)),
    "sector_squash": StageSpec("sector", "Sector Squash", (
        _HUE, ControlSpec("squash", "Squash", -1.0, 1.0, 0.0), _FALLOFF, _BRILLIANCE)),
}
_STAGE_CODES = {kind: index for index, kind in enumerate(STAGE_SPECS, 1)}
_AMOUNTS = {
    "colour_saturation": ("rg", "yb"),
    "colour_crosstalk": ("r_to_yb", "g_to_yb", "y_to_rg", "b_to_rg"),
    "highlight_bleach": ("red", "green", "yellow", "blue"),
    "contrast_boost": ("contrast",),
    "brilliance_reduction": ("amount",),
    "neutral_tint": ("amount",),
    "sector_brightness": ("brightness",),
    "sector_saturation": ("saturation",),
    "sector_skew": ("skew",),
    "sector_squash": ("squash",),
}
_PRIMARY_CONTROLS = {
    "colour_saturation": ("rg", "yb"),
    "colour_crosstalk": ("r_to_yb", "g_to_yb", "y_to_rg", "b_to_rg"),
    "highlight_bleach": ("red", "green", "yellow", "blue", "falloff"),
    "contrast_boost": ("contrast", "grey_pivot", "highlight_pivot"),
    "brilliance_reduction": ("amount", "falloff"),
    "neutral_tint": ("hue", "amount", "falloff"),
    "sector_brightness": ("hue", "brightness", "falloff"),
    "sector_saturation": ("hue", "saturation", "falloff"),
    "sector_skew": ("hue", "skew", "falloff"),
    "sector_squash": ("hue", "squash", "falloff"),
}


def _controls(kind: str) -> tuple[ControlSpec, ...]:
    spec = STAGE_SPECS[kind]
    return spec.controls + (MODULATION_CONTROLS if spec.supports_modulation else ())


def new_chromogen_stage(kind: str) -> dict[str, Any]:
    return {"id": uuid4().hex, "kind": kind, "enabled": True,
            "params": {control.key: control.default for control in _controls(kind)}}


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def chromogen_compatibility_error(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return "Look Dev data must be an object"
    if type(raw.get("version")) is not int or raw["version"] != CHROMOGEN_VERSION:
        return "Unsupported Look Dev version"
    stages = raw.get("stages")
    if not isinstance(stages, (tuple, list)) or len(stages) > CHROMOGEN_MAX_STAGES:
        return f"Look Dev requires at most {CHROMOGEN_MAX_STAGES} stages"
    if not isinstance(raw.get("enabled", True), bool) or not _finite(raw.get("amount", 1.0)):
        return "Invalid Look Dev controls"
    ids: set[str] = set()
    for stage in stages:
        if not isinstance(stage, Mapping):
            return "Invalid Look Dev stage"
        kind, identity = stage.get("kind"), stage.get("id")
        if not isinstance(kind, str) or kind not in STAGE_SPECS:
            return f"Unsupported Look Dev stage: {kind!s}"
        if not isinstance(identity, str) or not identity or identity in ids or len(identity) > 128:
            return "Invalid or duplicate Look Dev stage ID"
        ids.add(identity)
        params = stage.get("params", {})
        if (not isinstance(params, Mapping) or not isinstance(stage.get("enabled", True), bool)
                or ("name" in stage and not isinstance(stage["name"], str))
                or any(not _finite(params.get(control.key, control.default)) for control in _controls(kind))):
            return "Invalid Look Dev stage controls"
    return None


def normalize_chromogen(raw: Any) -> dict[str, Any] | None:
    if raw is None or chromogen_compatibility_error(raw):
        return None
    stages = []
    for stage in raw["stages"]:
        params = stage.get("params", {})
        values = {control.key: min(control.maximum, max(
            control.minimum, float(params.get(control.key, control.default))))
            for control in _controls(stage["kind"])}
        stages.append({**stage, "enabled": stage.get("enabled", True),
                       "params": {**params, **values}})
    return {**raw, "enabled": raw.get("enabled", True),
            "amount": min(1.0, max(0.0, float(raw.get("amount", 1.0)))), "stages": stages}


def _active_stages(state: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    if not state["enabled"] or state["amount"] == 0.0:
        return ()
    return tuple(stage for stage in state["stages"] if stage["enabled"] and any(
        stage["params"][control.key] != control.default
        for control in STAGE_SPECS[stage["kind"]].controls
        if control.key in _AMOUNTS[stage["kind"]]))


def chromogen_active(params: Mapping[str, Any]) -> bool:
    state = normalize_chromogen(params.get(CHROMOGEN_KEY))
    return state is not None and bool(_active_stages(state))


def _preset(*definitions: tuple[str, Mapping[str, float]]) -> dict[str, Any]:
    stages = []
    for index, (kind, values) in enumerate(definitions):
        params = {control.key: control.default for control in _controls(kind)}
        stages.append({"id": f"{kind}-{index}", "kind": kind, "enabled": True,
                       "params": {**params, **values}})
    return {"version": CHROMOGEN_VERSION, "enabled": True, "amount": 1.0, "stages": stages}


CHROMOGEN_PRESETS = {
    "Neutral": _preset(("colour_saturation", {}), ("colour_crosstalk", {})),
    "Soft Print": _preset(
        ("contrast_boost", {"contrast": 0.16, "grey_pivot": -0.4}),
        ("highlight_bleach", {"red": 0.28, "green": 0.22, "yellow": 0.44, "blue": 0.24}),
        ("colour_saturation", {"rg": 1.13, "yb": 0.94})),
    "Cool Shadows": _preset(
        ("neutral_tint", {"hue": -145.0, "amount": -0.44, "pivot": -1.0}),
        ("sector_saturation", {"hue": 37.0, "saturation": 1.18}),
        ("sector_skew", {"hue": -120.0, "skew": -0.15}),
        ("contrast_boost", {"contrast": 0.12})),
    "Warm Silver": _preset(
        ("colour_saturation", {"rg": 0.55, "yb": 0.62}),
        ("neutral_tint", {"hue": 42.0, "amount": 0.57}),
        ("highlight_bleach", {"red": 0.18, "green": 0.35, "yellow": 0.38, "blue": 0.30})),
    "Deep Colour": _preset(
        ("colour_saturation", {"rg": 1.24, "yb": 1.21}),
        ("brilliance_reduction", {"amount": 0.28}),
        ("sector_squash", {"hue": -32.0, "squash": 0.22}),
        ("sector_brightness", {"hue": 60.0, "brightness": -0.23})),
}


def _stage_values(stage: Mapping[str, Any]) -> tuple[float, ...]:
    kind, values = stage["kind"], stage["params"]
    primary = tuple(values[key] for key in _PRIMARY_CONTROLS[kind])
    return (float(_STAGE_CODES[kind]), values.get("zone", 0.0),
            values.get("pivot", 0.0), values.get("chroma", 0.0),
            math.radians(values.get("rotate", 0.0)), values.get("brilliance", 0.5), 0.0, 0.0,
            *primary, *((0.0,) * (CHROMOGEN_STAGE_FLOATS - 8 - len(primary))))


def chromogen_uniform_rows(raw: Any) -> tuple[tuple[float, ...], ...]:
    """Pack the enabled, nonidentity stages in processing order."""
    state = normalize_chromogen(raw)
    stages = _active_stages(state) if state is not None else ()
    rows = tuple(values[index:index + 4] for stage in stages for values in (_stage_values(stage),)
                 for index in range(0, CHROMOGEN_STAGE_FLOATS, 4))
    capacity = CHROMOGEN_MAX_STAGES * CHROMOGEN_STAGE_FLOATS // 4
    return rows + ((0.0, 0.0, 0.0, 0.0),) * (capacity - len(rows))


# Colorimetry from the Eab v2 interchange definition in an exported
# reference grade. The implementation keeps extended working values by carrying
# a common LMS offset separately when an input extends below the cone domain.
_TO_LMS = np.array(((.31395619, .63953670, .04650710),
                    (.15535103, .75792165, .08672731),
                    (.01775004, .10944762, .87280234)), np.float32)
_FROM_LMS = np.linalg.inv(_TO_LMS)
_TO_OPPONENT = np.array(((1.011441, -.673950), (.312388, .660035)), np.float32)
_FROM_OPPONENT = np.linalg.inv(_TO_OPPONENT)
_LMS_BLACK = 0.001
_COMPRESSION_FLOOR = 0.022747
_GREY = 0.18 + _LMS_BLACK + _COMPRESSION_FLOOR
_HUE_ZERO = math.pi * 0.5


def _dot_rgb(rgb: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return np.einsum("...j,ij->...i", rgb, matrix)


def _lms_intensity(l: np.ndarray, m: np.ndarray, s: np.ndarray) -> np.ndarray:
    scale = np.maximum(1.0, np.maximum(np.maximum(np.abs(l), np.abs(m)), np.abs(s)))
    l, m, s = l / scale, m / scale, s / scale
    return scale * np.sqrt(0.4 * (l * l + m * m) + 0.2 * s * s)


def _opponent(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lms = _dot_rgb(rgb, _TO_LMS) + _LMS_BLACK
    residual = np.maximum(0.0, -lms.min(axis=-1) + 1e-7)
    l, m, s = np.moveaxis(lms + residual[..., None], -1, 0)
    ratios = np.stack((3.0 * (l - m) / np.maximum(l + 0.2 * s, 1e-7),
                       (l + m - 2.0 * s) / np.maximum(l + m + s, 1e-7)), axis=-1)
    return _lms_intensity(l, m, s), _dot_rgb(ratios, _TO_OPPONENT), residual


def _scene_rgb(intensity: np.ndarray, ab: np.ndarray, residual: np.ndarray) -> np.ndarray:
    u, v = np.moveaxis(_dot_rgb(ab, _FROM_OPPONENT), -1, 0)
    # Hue edits can push saturated samples past the opponent inverse pole.
    # Bound the ratio, retaining the signed, unbounded RGB result.
    u = np.minimum(u, 5.4)
    s = 1.0 - v
    l = (9.0 + (0.2 * u - 3.0) * s) / (6.0 - u)
    m = 3.0 - l - s
    scale = intensity / _lms_intensity(l, m, s)
    lms = np.stack((l, m, s), axis=-1) * scale[..., None] - (_LMS_BLACK + residual[..., None])
    return _dot_rgb(lms, _FROM_LMS)


def chromogen_coordinates(rgb: np.ndarray) -> np.ndarray:
    """Plot coordinates E, a, b for scene-linear Rec.709 colors."""
    intensity, ab, _ = _opponent(np.asarray(rgb, dtype=np.float32))
    exposure = np.log2((intensity + _COMPRESSION_FLOOR) / _GREY)
    return np.concatenate((exposure[..., None], ab), axis=-1)


def chromogen_rgb_from_coordinates(eab: np.ndarray) -> np.ndarray:
    """Sample the scope's Eab volume as scene-linear Rec.709 colors."""
    exposure = eab[..., 0]
    light = np.maximum(0.0, _GREY * np.exp2(exposure) - _COMPRESSION_FLOOR)
    return _scene_rgb(light, eab[..., 1:], np.zeros_like(exposure))


def chromogen_rgb_from_lms(lms: np.ndarray) -> np.ndarray:
    """Convert linear LMS scope samples into the renderer's grading basis."""
    return _dot_rgb(lms, _FROM_LMS)


def chromogen_hue(rgb: np.ndarray) -> np.ndarray:
    """Return the Hue control value with zero at yellow and positive toward red."""
    coordinates = chromogen_coordinates(rgb)
    return (np.degrees(_HUE_ZERO - np.arctan2(coordinates[..., 2], coordinates[..., 1]))
            + 180.0) % 360.0 - 180.0


def _smooth(edge0: float, edge1: float, value: np.ndarray) -> np.ndarray:
    t = np.clip((value - edge0) / (edge1 - edge0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _rotate(a: np.ndarray, b: np.ndarray, angle: float | np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cos, sin = np.cos(angle), np.sin(angle)
    return cos * a - sin * b, sin * a + cos * b


def _contrast_response(e: np.ndarray, pivot: float, highlight: float) -> np.ndarray:
    shoulder = max(highlight - 1.82, pivot + 0.25)
    return ((e - pivot + 0.108) * _smooth(-3.5, -0.337, e)
            - 1.009 * np.maximum(e - shoulder, 0.0))


def _apply_stage(rgb: np.ndarray, values: tuple[float, ...]) -> np.ndarray:
    stage, zone, pivot, chroma, rotate, brilliance, _, _, x, y, z, w, extra, *_ = values
    intensity, ab, residual = _opponent(rgb)
    e = np.log2((intensity + _COMPRESSION_FLOOR) / _GREY)
    orig_a, orig_b = _rotate(ab[..., 0], ab[..., 1], -rotate)
    a, b = orig_a, orig_b
    c = np.hypot(a, b)
    weight = 1.0 - abs(zone) * (1.0 - _smooth(-1.75, 0.25, np.sign(zone) * (e - pivot)))
    if stage not in (4, 5, 6):
        chromatic = np.exp(-2.0 * c) if chroma >= 0.0 else _smooth(0.2, 1.0, c)
        weight *= 1.0 + abs(chroma) * (chromatic - 1.0)
    exposure = np.zeros_like(intensity)
    if stage == 1:
        a = a * (1.0 + weight * (x - 1.0))
        b = b * (1.0 + weight * (y - 1.0))
    elif stage == 2:
        cross = weight * (0.2 + 0.42 * _smooth(-2.2, 1.9, e))
        a = a + cross * b * np.where(b >= 0.0, z, w)
        b = b + cross * orig_a * np.where(orig_a >= 0.0, x, y)
    elif stage == 3:
        start = pivot - 0.556 + 0.843 * (extra - 0.5)
        shoulder = 0.318 * _smooth(start, start + 7.0, e) ** 0.795 * weight
        radial = 1.0 + 0.67 * c ** 0.30
        bleach_a = np.where(a >= 0.0, x, y) ** 1.024
        bleach_b = np.where(b >= 0.0, z, w) ** 1.024
        a = a * (1.0 - np.minimum(0.98, bleach_a * (radial + 0.275 * np.abs(a)) * shoulder))
        b = b * (1.0 - np.minimum(0.98, bleach_b * (radial + 0.275 * np.abs(b)) * shoulder))
    elif stage == 4:
        gain = np.clip(0.1713 * x * np.exp(1.62 * x), -0.85, 2.0)
        exposure = gain * _contrast_response(e, y, z)
        if chroma != 0.0:
            slope = gain * (_contrast_response(e + 0.08, y, z)
                            - _contrast_response(e - 0.08, y, z)) / 0.16
            correction = 1.038 * slope * np.exp(-2.166 * c ** 1.47) + 0.0658 * exposure * c
            factor = np.maximum(0.0, 1.0 + chroma * correction)
            a, b = a * factor, b * factor
    elif stage == 5:
        threshold = (pivot - 0.85 - (0.15 + 0.37 * chroma) * np.minimum(c, 2.0) ** 1.33
                     - (0.10 * a + 0.06 * b) / np.maximum(c, 1e-8))
        over = np.maximum(e - threshold, 0.0)
        radial = np.sqrt(_smooth(max(0.1, 0.71 + (chroma - 0.6) * 0.6), 1.6, c))
        exposure = -0.272 * x * over * radial * _smooth(0.0, 0.05 + 1.4 * abs(y - 0.5), over)
    else:
        angle = _HUE_ZERO - math.radians(x)
        if stage == 6:
            cos, sin = math.cos(angle), math.sin(angle)
            tone = np.maximum(np.sign(y) * (e - pivot) + 0.225, 0.0)
            floor = min(0.7 * abs(y), 0.00984 * 2.0 ** (5.0 * (1.0 - z)))
            tint = np.maximum(floor, 0.106 * abs(y) * tone * 2.0 ** (2.29 * (1.0 - z)))
            along_tint = a * cos + b * sin
            distance = (c * np.maximum(0.02, 1.0 + 2.95 * along_tint)
                        / (1.0 + 1.66 * _smooth(-2.0, 2.0, e)))
            tint *= 1.0 - min(chroma, 1.0) * _smooth(0.01, 2.04, distance)
            a = a + cos * tint
            b = b + sin * tint
            exposure = 0.15 * brilliance * tint * _smooth(-0.5, 2.0, e)
        else:
            along, across = _rotate(a, b, -angle)
            cosine = np.clip(along / np.maximum(c, 1e-8), 0.0, 1.0)
            if stage == 7:
                radial = (1.0 - np.exp(-1.18 * c ** 3.38)) ** (0.38 + 0.146 * (z - 0.5))
                exposure = (y * weight * cosine ** 1.715 * radial
                            * (0.004 + 0.752 * _smooth(-4.10, 4.19, e)))
            elif stage == 8:
                radial = (1.0 - np.exp(-20.0 * c ** 2.93)) ** (0.30 + 0.50 * (z - 0.5))
                along = along * (1.0 + (y - 1.0) * weight * 0.83 * cosine ** 0.74 * radial)
            elif stage == 9:
                radial = (1.0 - np.exp(-2.34 * c ** 0.91)) ** (3.0 + 2.0 * (z - 0.5))
                across = across - y * weight * c * cosine ** 1.165 * radial
            elif stage == 10:
                radial = (1.0 - np.exp(-20.0 * c ** 3.31)) ** (0.174 + 0.24 * (z - 0.5))
                across = across * (1.0 - y * weight * 0.736 * cosine ** 0.734 * radial)
            a, b = _rotate(along, across, angle)
    if stage not in (4, 5, 7) and brilliance != 0.0:
        hue_bias = 1.0 + (0.557 * orig_a * orig_b + 0.146 * orig_b ** 2) / np.maximum(c ** 2, 1e-8)
        chroma_delta = c ** 0.755 - np.hypot(a, b) ** 0.755
        light_weight = np.clip(e + 2.878, 0.0, 12.0) ** 0.791
        exposure = (exposure + 0.2833 * light_weight * chroma_delta * hue_bias
                    * brilliance / (1.0 + 0.0572 * brilliance))
    a, b = _rotate(a, b, rotate)
    output_intensity = np.maximum(0.0, (intensity + _COMPRESSION_FLOOR)
                                  * np.exp2(np.clip(exposure, -8.0, 8.0)) - _COMPRESSION_FLOOR)
    return _scene_rgb(output_intensity, np.stack((a, b), axis=-1), residual)

def apply_chromogen_linear(rgb: np.ndarray, raw: Any) -> np.ndarray:
    """Evaluate the ordered point transform in linear light, preserving extended range."""
    state = normalize_chromogen(raw)
    stages = _active_stages(state) if state is not None else ()
    if not stages:
        return rgb
    source = np.asarray(rgb, dtype=np.float32)
    result = source
    for stage in stages:
        result = _apply_stage(result, _stage_values(stage))
    amount = np.float32(state["amount"])
    return result if amount == 1.0 else source + amount * (result - source)


def _glsl_matrix(name: str, matrix: np.ndarray) -> str:
    size = len(matrix)
    rows = ",\n        ".join("dot(rgb, vec" + str(size) + "("
                              + ", ".join(f"{float(value):.10f}" for value in row) + "))"
                              for row in matrix)
    return f"vec{size} {name}(vec{size} rgb) {{ return vec{size}({rows}); }}\n"


CHROMOGEN_GLSL_STAGE = (
    _glsl_matrix("topos_chromogen_to_lms", _TO_LMS)
    + _glsl_matrix("topos_chromogen_from_lms", _FROM_LMS)
    + _glsl_matrix("topos_chromogen_to_opponent", _TO_OPPONENT)
    + _glsl_matrix("topos_chromogen_from_opponent", _FROM_OPPONENT)
    + f"""
const float TOPOS_CHROMOGEN_LMS_BLACK = {_LMS_BLACK};
const float TOPOS_CHROMOGEN_COMPRESSION_FLOOR = {_COMPRESSION_FLOOR};
const float TOPOS_CHROMOGEN_GREY = {_GREY};
const float TOPOS_CHROMOGEN_HUE_ZERO = {_HUE_ZERO};
"""
    + """
float topos_chromogen_intensity(vec3 lms) {
    vec3 magnitude = abs(lms);
    float scale = max(1.0, max(magnitude.x, max(magnitude.y, magnitude.z)));
    lms /= scale;
    return scale * sqrt(dot(lms * lms, vec3(0.4, 0.4, 0.2)));
}

vec2 topos_chromogen_rotate(vec2 ab, float angle) {
    float cs = cos(angle);
    float sn = sin(angle);
    return vec2(cs * ab.x - sn * ab.y, sn * ab.x + cs * ab.y);
}

float topos_chromogen_contrast(float exposure, float pivot, float highlight) {
    float shoulder = max(highlight - 1.82, pivot + 0.25);
    return (exposure - pivot + 0.108) * smoothstep(-3.5, -0.337, exposure)
        - 1.009 * max(exposure - shoulder, 0.0);
}

vec3 chromogen_apply_stage(vec3 rgb, vec4 p0, vec4 p1, vec4 p2, vec4 p3, vec4 p4, vec4 p5) {
    int stage = int(p0.x);
    float zone = p0.y;
    float pivot = p0.z;
    float chroma = p0.w;
    float rotate = p1.x;
    float brilliance = p1.y;
    vec3 lms = topos_chromogen_to_lms(rgb) + TOPOS_CHROMOGEN_LMS_BLACK;
    float residual = max(0.0, -min(lms.x, min(lms.y, lms.z)) + 1e-7);
    lms += residual;
    float intensity = topos_chromogen_intensity(lms);
    float e = log2((intensity + TOPOS_CHROMOGEN_COMPRESSION_FLOOR) / TOPOS_CHROMOGEN_GREY);
    vec2 ratios = vec2(3.0 * (lms.x - lms.y) / max(lms.x + 0.2 * lms.z, 1e-7),
                       (lms.x + lms.y - 2.0 * lms.z) / max(lms.x + lms.y + lms.z, 1e-7));
    vec2 ab = topos_chromogen_rotate(topos_chromogen_to_opponent(ratios), -rotate);
    vec2 original_ab = ab;
    float c = length(ab);
    float weight = 1.0 - abs(zone) * (1.0 - smoothstep(-1.75, 0.25, sign(zone) * (e - pivot)));
    if (stage != 4 && stage != 5 && stage != 6) {
        float chromatic = chroma >= 0.0 ? exp(-2.0 * c) : smoothstep(0.2, 1.0, c);
        weight *= 1.0 + abs(chroma) * (chromatic - 1.0);
    }
    float exposure = 0.0;
    if (stage == 1) {
        ab *= 1.0 + weight * (p2.xy - 1.0);
    } else if (stage == 2) {
        float cross_weight = weight * (0.2 + 0.42 * smoothstep(-2.2, 1.9, e));
        vec2 cross_signal = vec2(ab.y * (ab.y >= 0.0 ? p2.z : p2.w),
                                 ab.x * (ab.x >= 0.0 ? p2.x : p2.y));
        ab += cross_weight * cross_signal;
    } else if (stage == 3) {
        float start = pivot - 0.556 + 0.843 * (p3.x - 0.5);
        float shoulder = 0.318 * pow(smoothstep(start, start + 7.0, e), 0.795) * weight;
        vec2 bleach = vec2(ab.x >= 0.0 ? p2.x : p2.y, ab.y >= 0.0 ? p2.z : p2.w);
        vec2 radial = 1.0 + 0.67 * pow(c, 0.30) + 0.275 * abs(ab);
        ab *= 1.0 - min(vec2(0.98), pow(bleach, vec2(1.024)) * radial * shoulder);
    } else if (stage == 4) {
        float gain = clamp(0.1713 * p2.x * exp(1.62 * p2.x), -0.85, 2.0);
        exposure = gain * topos_chromogen_contrast(e, p2.y, p2.z);
        if (chroma != 0.0) {
            float slope = gain * (topos_chromogen_contrast(e + 0.08, p2.y, p2.z)
                                  - topos_chromogen_contrast(e - 0.08, p2.y, p2.z)) / 0.16;
            float correction = 1.038 * slope * exp(-2.166 * pow(c, 1.47)) + 0.0658 * exposure * c;
            ab *= max(0.0, 1.0 + chroma * correction);
        }
    } else if (stage == 5) {
        float threshold = pivot - 0.85 - (0.15 + 0.37 * chroma) * pow(min(c, 2.0), 1.33)
            - dot(vec2(0.10, 0.06), ab) / max(c, 1e-8);
        float over = max(e - threshold, 0.0);
        float radial = sqrt(smoothstep(max(0.1, 0.71 + (chroma - 0.6) * 0.6), 1.6, c));
        exposure = -0.272 * p2.x * over * radial * smoothstep(0.0, 0.05 + 1.4 * abs(p2.y - 0.5), over);
    } else {
        float angle = TOPOS_CHROMOGEN_HUE_ZERO - p2.x * 0.0174532925199433;
        if (stage == 6) {
            vec2 direction = vec2(cos(angle), sin(angle));
            float tone = max(sign(p2.y) * (e - pivot) + 0.225, 0.0);
            float tint_floor = min(0.7 * abs(p2.y), 0.00984 * exp2(5.0 * (1.0 - p2.z)));
            float tint = max(tint_floor, 0.106 * abs(p2.y) * tone * exp2(2.29 * (1.0 - p2.z)));
            float distance = c * max(0.02, 1.0 + 2.95 * dot(ab, direction))
                / (1.0 + 1.66 * smoothstep(-2.0, 2.0, e));
            tint *= 1.0 - min(chroma, 1.0) * smoothstep(0.01, 2.04, distance);
            ab += direction * tint;
            exposure = 0.15 * brilliance * tint * smoothstep(-0.5, 2.0, e);
        } else {
            vec2 axis = topos_chromogen_rotate(ab, -angle);
            float cosine = clamp(axis.x / max(c, 1e-8), 0.0, 1.0);
            if (stage == 7) {
                float radial = pow(1.0 - exp(-1.18 * pow(c, 3.38)), 0.38 + 0.146 * (p2.z - 0.5));
                exposure = p2.y * weight * pow(cosine, 1.715) * radial
                    * (0.004 + 0.752 * smoothstep(-4.10, 4.19, e));
            } else if (stage == 8) {
                float radial = pow(1.0 - exp(-20.0 * pow(c, 2.93)), 0.30 + 0.50 * (p2.z - 0.5));
                axis.x *= 1.0 + (p2.y - 1.0) * weight * 0.83 * pow(cosine, 0.74) * radial;
            } else if (stage == 9) {
                float radial = pow(1.0 - exp(-2.34 * pow(c, 0.91)), 3.0 + 2.0 * (p2.z - 0.5));
                axis.y -= p2.y * weight * c * pow(cosine, 1.165) * radial;
            } else if (stage == 10) {
                float radial = pow(1.0 - exp(-20.0 * pow(c, 3.31)), 0.174 + 0.24 * (p2.z - 0.5));
                axis.y *= 1.0 - p2.y * weight * 0.736 * pow(cosine, 0.734) * radial;
            }
            ab = topos_chromogen_rotate(axis, angle);
        }
    }
    if (stage != 4 && stage != 5 && stage != 7 && brilliance != 0.0) {
        float hue_bias = 1.0 + (0.557 * original_ab.x * original_ab.y + 0.146 * original_ab.y * original_ab.y)
            / max(c * c, 1e-8);
        float chroma_delta = pow(c, 0.755) - pow(length(ab), 0.755);
        float brightness_weight = pow(clamp(e + 2.878, 0.0, 12.0), 0.791);
        exposure += 0.2833 * brightness_weight * chroma_delta * hue_bias * brilliance / (1.0 + 0.0572 * brilliance);
    }
    ab = topos_chromogen_rotate(ab, rotate);
    intensity = max(0.0, (intensity + TOPOS_CHROMOGEN_COMPRESSION_FLOOR)
                         * exp2(clamp(exposure, -8.0, 8.0)) - TOPOS_CHROMOGEN_COMPRESSION_FLOOR);
    vec2 uv = topos_chromogen_from_opponent(ab);
    float u = min(uv.x, 5.4);
    float s = 1.0 - uv.y;
    float l = (9.0 + (0.2 * u - 3.0) * s) / (6.0 - u);
    lms = vec3(l, 3.0 - l - s, s);
    lms = lms * (intensity / topos_chromogen_intensity(lms)) - (TOPOS_CHROMOGEN_LMS_BLACK + residual);
    return topos_chromogen_from_lms(lms);
}
"""
)

CHROMOGEN_MSL_STAGE = (CHROMOGEN_GLSL_STAGE
                       .replace("vec", "float")
                       .replace("const float TOPOS_", "constant float TOPOS_")
                       .replace("atan(axis.y, axis.x)", "atan2(axis.y, axis.x)"))
