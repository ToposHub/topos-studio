"""Base Grade controls and the linear-light operator shared by the render backends.

The response coefficients were measured against Baselight M 7.0.3 in Rec.709.
See docs/validation/baselight_base_grade_calibration.md for the comparison.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

import numpy as np

ZONES = ("dim", "light", "dark", "bright")
_ZONE_ORDER = ("bright", "light", "dim", "dark")
_SETTINGS = {
    "flare": (0.0, -0.1, 0.1),
    "contrast": (1.0, 0.1, 3.0),
    "contrast_pivot": (0.0, -4.0, 4.0),
    "saturation": (1.0, 0.0, 3.0),
    "balance_exposure": (0.0, -8.0, 8.0),
    "balance_a": (0.0, -0.25, 0.25),
    "balance_b": (0.0, -0.5, 0.5),
}
for _zone, _pivot, _falloff in (
    ("dim", 1.0, 0.75), ("light", -1.0, 0.75),
    ("dark", -1.0, 0.5), ("bright", 2.0, 0.5),
):
    _SETTINGS.update({
        f"{_zone}_exposure": (0.0, -3.0, 3.0),
        f"{_zone}_a": (0.0, -0.25, 0.25),
        f"{_zone}_b": (0.0, -0.5, 0.5),
        f"{_zone}_pivot": (_pivot, -8.0, 8.0),
        f"{_zone}_falloff": (_falloff, 0.0, 1.0),
        f"{_zone}_saturation": (1.0, 0.0, 3.0),
    })

BASE_GRADE_DEFAULTS = {f"base_grade_{key}": values[0] for key, values in _SETTINGS.items()}
BASE_GRADE_LIMITS = {f"base_grade_{key}": values[1:] for key, values in _SETTINGS.items()}
_TONE_SETTINGS = tuple(key for key in _SETTINGS if not key.endswith(("_pivot", "_falloff")))
_LUMINANCE = (0.1635, 0.5105, 0.3260)
# The tone zones use the maximum of three adapted linear components.
_TONE_MATRIX = (
    (0.402999, 0.583943, 0.013058),
    (0.069554, 0.844134, 0.086312),
    (0.015870, 0.106447, 0.877683),
)
_TINT_A = (
    (1.282520, 3.314063, 0.218427),
    (-0.193648, -1.292261, -0.123542),
    (-0.001263, 0.094706, 0.011389),
)
_TINT_B = (
    (0.656740, -0.031655, -0.253335),
    (0.004611, 0.690990, 0.230160),
    (-0.039507, -0.249420, -1.347400),
)
# Powers for the shadow toe at four measured falloffs, by exposure.
_SHADOW_FALLOFFS = (0.0, 0.35, 0.75, 1.0)
_SHADOW_KNOTS = (
    (-3.0, (1.850, 1.377, 0.828, 0.472)),
    (-0.5, (1.862, 1.387, 0.836, 0.479)),
    (0.5, (1.582, 1.183, 0.720, 0.416)),
    (1.0, (1.105, 0.836, 0.520, 0.308)),
    (1.5, (0.776, 0.595, 0.379, 0.230)),
    (2.0, (0.591, 0.458, 0.298, 0.185)),
    (3.0, (0.389, 0.308, 0.207, 0.132)),
)
_HIGHLIGHT_SHAPE = (
    -0.157191, 0.054644, 0.000107, 0.836099, -0.099144,
    5.434107, 0.169341, -3.042128, 2.032937, 0.151277,
    0.004061, -0.906451, -0.077149, 0.446629,
)


def base_grade_value(params: Mapping[str, object], key: str) -> float:
    """Get a validated scalar by its suffix, for renderers and uniform binding."""
    default, lo, hi = _SETTINGS[key]
    raw = params.get(f"base_grade_{key}", default)
    if not isinstance(raw, (int, float, np.integer, np.floating)) or not isfinite(raw):
        return default
    return min(max(float(raw), lo), hi)


def base_grade_active(params: Mapping[str, object]) -> bool:
    """Pivot and falloff edits have no effect until a grading control changes."""
    return any(base_grade_value(params, key) != _SETTINGS[key][0] for key in _TONE_SETTINGS
               if f"base_grade_{key}" in params)


def _dot(color: np.ndarray, weights: tuple[float, float, float]) -> np.ndarray:
    return color[..., 0] * weights[0] + color[..., 1] * weights[1] + color[..., 2] * weights[2]


def _tone(color: np.ndarray) -> np.ndarray:
    a, b, c = (_dot(color, row) for row in _TONE_MATRIX)
    return np.maximum(np.maximum(a, b), c)


def _tint(color: np.ndarray, a: float | np.ndarray, b: float | np.ndarray) -> np.ndarray:
    if np.any(a):
        # Cast the coefficient matrix to avoid promoting a float32 preview.
        color = color + (color @ np.asarray(_TINT_A, dtype=color.dtype).T) * np.expand_dims(a, -1)
    if np.any(b):
        color = color + (color @ np.asarray(_TINT_B, dtype=color.dtype).T) * np.expand_dims(
            b / (1.0 + 0.33 * b), -1,
        )
    return color


def _saturate(color: np.ndarray, saturation: float | np.ndarray) -> np.ndarray:
    neutral = _dot(color, _LUMINANCE)[..., None]
    return neutral + (color - neutral) * np.expand_dims(saturation, -1)


def _shadow_weight(ratio: np.ndarray, exposure: float, falloff: float) -> np.ndarray:
    exposures, coefficients = zip(*_SHADOW_KNOTS)
    powers = [np.interp(falloff, _SHADOW_FALLOFFS, row) for row in coefficients]
    power = np.interp(exposure, exposures, powers)
    shoulder = 2.0 - (1.28 / 1.75) * max(exposure - 1.25, 0.0)
    v = np.power(np.clip(ratio, 0.0, 1.0), power)
    return (1.0 - v) ** 2 * (1.0 + shoulder * v)


def _highlight_weight(ratio: np.ndarray, exposure: float, falloff: float) -> np.ndarray:
    p = _HIGHLIGHT_SHAPE
    e, f = exposure, falloff
    edge = np.exp(p[0] + e * (p[1] + e * p[2])
                  + f * (p[3] + e * p[4] + f * (p[5] + e * p[6] + f * p[7])))
    power = max(1.0, p[8] + e * (p[9] + e * p[10]) + f * (p[11] + e * p[12] + f * p[13]))
    above = np.maximum(ratio - 1.0, 0.0)
    curved = edge / power * np.power(np.minimum(above / edge, 1.0), power)
    # An integrated ramp matches the slope at the join and approaches full gain in HDR.
    return (curved + np.maximum(above - edge, 0.0)) / np.maximum(ratio, 1.0)


def _color_weight(ratio: np.ndarray, falloff: float, *, shadow: bool) -> np.ndarray:
    if shadow:
        linear = np.clip(ratio, 0.0, 1.0)
        return (1.0 - linear) ** 2 * (1.0 + 2.0 * linear)
    width = np.exp2(falloff * (2.0 + 4.0 * falloff))
    return np.clip((ratio - 1.0) / width, 0.0, 1.0)


def apply_base_grade_linear(rgb: np.ndarray, params: Mapping[str, object]) -> np.ndarray:
    """Apply Base Grade to signed linear RGB without clipping HDR or negatives."""
    if not base_grade_active(params):
        return rgb
    return _apply_base_grade_linear(rgb, params)


def _apply_base_grade_linear(
    rgb: np.ndarray, params: Mapping[str, object], *,
    stage_levels: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    def value(key: str) -> float:
        return base_grade_value(params, key)

    balance = np.exp2(value("balance_exposure"))
    color = _tint(rgb, value("balance_a"), value("balance_b"))
    if balance != 1.0:
        color = color * balance
    contrast = value("contrast")
    if contrast != 1.0:
        pivot = 0.18 * np.exp2(2.0 * value("contrast_pivot"))
        level = _tone(color)
        # Keep the response through signed RGB and anchor exact black.
        magnitude = np.maximum(np.maximum(np.abs(level), np.max(np.abs(color), axis=-1) * 1e-4), 1e-8)
        color = color * np.power(magnitude / pivot, contrast - 1.0)[..., None]
    flare = value("flare")
    if flare:
        color = color + flare * balance

    if stage_levels is not None:
        stage_levels["global"] = _dot(color, _LUMINANCE)

    for zone in _ZONE_ORDER:
        if stage_levels is not None:
            stage_levels[zone] = _tone(color)
        exposure, a, b, saturation = (value(f"{zone}_{key}") for key in ("exposure", "a", "b", "saturation"))
        if exposure == a == b == 0.0 and saturation == 1.0:
            continue
        level = _tone(color) if stage_levels is None else stage_levels[zone]
        ratio = level / (0.18 * np.exp2(value(f"{zone}_pivot")))
        shadow = zone in ("dim", "dark")
        weight_fn = _shadow_weight if shadow else _highlight_weight
        falloff = value(f"{zone}_falloff")
        if exposure:
            weight = weight_fn(ratio, exposure, falloff)
            color = color * (1.0 + np.expm1(exposure * np.log(2.0)) * weight[..., None])
        if a or b or saturation != 1.0:
            weight = _color_weight(ratio, falloff, shadow=shadow)
            if a or b:
                color = _tint(color, a * weight, b * weight)
            if saturation != 1.0:
                color = _saturate(color, 1.0 + (saturation - 1.0) * weight)
    saturation = value("saturation")
    if saturation != 1.0:
        color = _saturate(color, saturation)
    return color.astype(rgb.dtype, copy=False)


def base_grade_curve(
    input_linear_grey: np.ndarray, params: Mapping[str, object], *,
    stage_levels: dict[str, np.ndarray] | None = None,
) -> np.ndarray:
    """Sample output luminance and optionally the global response and each zone's input tone."""
    grey = np.repeat(np.asarray(input_linear_grey, dtype=np.float32)[..., None], 3, axis=-1)
    color = (apply_base_grade_linear(grey, params) if stage_levels is None
             else _apply_base_grade_linear(grey, params, stage_levels=stage_levels))
    return _dot(color, _LUMINANCE)


def _shader_source(vector: str, vector4: str, function: str) -> str:
    def row(weights: tuple[float, float, float]) -> str:
        return f"{vector}({', '.join(f'{value:.6f}' for value in weights)})"

    def dots(matrix: tuple[tuple[float, float, float], ...]) -> str:
        return ", ".join(f"dot(color, {row(weights)})" for weights in matrix)

    shadow_power = []
    for index, ((left, powers), (right, following)) in enumerate(zip(_SHADOW_KNOTS, _SHADOW_KNOTS[1:])):
        start = ", ".join(f"{value:.6f}" for value in powers)
        end = ", ".join(f"{value:.6f}" for value in following)
        check = f"if (e <= {right:.6f}) " if index < len(_SHADOW_KNOTS) - 2 else ""
        shadow_power.append(f"    {check}return bg_shadow_interval(e, f, {left:.6f}, "
                            f"{right:.6f}, {vector4}({start}), {vector4}({end}));")
    src = """
FN VEC bg_tint(VEC color, float a, float b) {
    if (a != 0.0) color += a * VEC(TINT_A);
    if (b != 0.0) color += (b / (1.0 + 0.33 * b)) * VEC(TINT_B);
    return color;
}

FN VEC bg_saturate(VEC color, float amount) {
    VEC neutral = VEC(dot(color, LUMINANCE));
    return neutral + (color - neutral) * amount;
}

FN float bg_tone(VEC color) {
    VEC adapted = VEC(TONE_MATRIX);
    return max(max(adapted.x, adapted.y), adapted.z);
}

FN float bg_shadow_interval(float e, float f, float left, float right, VEC4 start, VEC4 end) {
    VEC4 powers = mix(start, end, clamp((e - left) / (right - left), 0.0, 1.0));
    if (f <= 0.35) return mix(powers.x, powers.y, f / 0.35);
    if (f <= 0.75) return mix(powers.y, powers.z, (f - 0.35) / 0.40);
    return mix(powers.z, powers.w, (f - 0.75) / 0.25);
}

FN float bg_shadow_power(float e, float f) {
SHADOW_POWER
}

FN float bg_shadow_weight(float ratio, float e, float f) {
    float power = bg_shadow_power(e, f);
    float shoulder = 2.0 - (1.28 / 1.75) * max(e - 1.25, 0.0);
    float v = pow(clamp(ratio, 0.0, 1.0), power);
    return (1.0 - v) * (1.0 - v) * (1.0 + shoulder * v);
}

FN float bg_highlight_weight(float ratio, float e, float f) {
    float edge = exp(H0 + e * (H1 + e * H2) + f * (H3 + e * H4 + f * (H5 + e * H6 + f * H7)));
    float power = max(1.0, H8 + e * (H9 + e * H10) + f * (H11 + e * H12 + f * H13));
    float above = max(ratio - 1.0, 0.0);
    float curved = edge / power * pow(min(above / edge, 1.0), power);
    return (curved + max(above - edge, 0.0)) / max(ratio, 1.0);
}

FN float bg_weight(float ratio, float e, float f, bool shadow) {
    return shadow ? bg_shadow_weight(ratio, e, f) : bg_highlight_weight(ratio, e, f);
}

FN float bg_color_weight(float ratio, float falloff, bool shadow) {
    if (shadow) {
        float linear_value = clamp(ratio, 0.0, 1.0);
        return (1.0 - linear_value) * (1.0 - linear_value) * (1.0 + 2.0 * linear_value);
    }
    float width = exp2(falloff * (2.0 + 4.0 * falloff));
    return clamp((ratio - 1.0) / width, 0.0, 1.0);
}

FN VEC bg_zone(VEC color, VEC wheel, VEC range, bool shadow) {
    if (wheel.x == 0.0 && wheel.y == 0.0 && wheel.z == 0.0 && range.z == 1.0) return color;
    float ratio = bg_tone(color) / (0.18 * exp2(range.x));
    if (wheel.x != 0.0) {
        float exposure_weight = bg_weight(ratio, wheel.x, range.y, shadow);
        color *= 1.0 + (exp2(wheel.x) - 1.0) * exposure_weight;
    }
    if (wheel.y != 0.0 || wheel.z != 0.0 || range.z != 1.0) {
        float weight = bg_color_weight(ratio, range.y, shadow);
        color = bg_tint(color, wheel.y * weight, wheel.z * weight);
        if (range.z != 1.0) color = bg_saturate(color, 1.0 + (range.z - 1.0) * weight);
    }
    return color;
}

FN VEC apply_base_grade(VEC color, bool encoded_input, VEC4 settings, VEC balance, VEC dim, VEC light,
                        VEC dark, VEC bright, VEC dim_range, VEC light_range, VEC dark_range, VEC bright_range) {
    if (encoded_input) color = sign(color) * pow(abs(color), VEC(2.4));
    float gain = exp2(balance.x);
    color = bg_tint(color, balance.y, balance.z) * gain;
    if (settings.y != 1.0) {
        float pivot = 0.18 * exp2(2.0 * settings.z);
        VEC m = abs(color);
        float magnitude = max(max(abs(bg_tone(color)), max(max(m.x, m.y), m.z) * 0.0001), 0.00000001);
        color *= pow(magnitude / pivot, settings.y - 1.0);
    }
    color += VEC(settings.x * gain);
    color = bg_zone(color, bright, bright_range, false);
    color = bg_zone(color, light, light_range, false);
    color = bg_zone(color, dim, dim_range, true);
    color = bg_zone(color, dark, dark_range, true);
    if (settings.w != 1.0) color = bg_saturate(color, settings.w);
    if (encoded_input) color = sign(color) * pow(abs(color), VEC(1.0 / 2.4));
    return color;
}
"""
    for index in reversed(range(len(_HIGHLIGHT_SHAPE))):
        src = src.replace(f"H{index}", f"({_HIGHLIGHT_SHAPE[index]:.6f})")
    src = src.replace("SHADOW_POWER", "\n".join(shadow_power))
    return (src.replace("VEC4", vector4).replace("VEC", vector).replace("FN ", function)
            .replace("LUMINANCE", row(_LUMINANCE)).replace("TONE_MATRIX", dots(_TONE_MATRIX))
            .replace("TINT_A", dots(_TINT_A)).replace("TINT_B", dots(_TINT_B)))


GLSL_BASE_GRADE = _shader_source("vec3", "vec4", "")
MSL_BASE_GRADE = _shader_source("float3", "float4", "static inline ")
