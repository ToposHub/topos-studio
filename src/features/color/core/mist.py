"""Mist parameters within the Color corrector's registered blur/sharpen stage.

The optional fields extend the existing effects mapping. Older grades bypass
Mist, and the same filter is used for GPU preview and full-frame delivery.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import floor, isfinite
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np


MIST_DEFAULT_RADIUS = 0.5
MIST_DEFAULT_MIX = 100.0
MIST_MAX_RADIUS = 10.0
MIST_MAX_MIX = 100.0
MIST_CALIBRATION_VERSION = 2
MIST_KEYS = frozenset(("mist_enabled", "mist_radius", "mist_mix", "mist_version"))


def mist_is_calibrated(effects: Mapping[str, Any]) -> bool:
    return effects.get("mist_version") == MIST_CALIBRATION_VERSION


def mist_defaults(calibrated: bool) -> tuple[float, float]:
    return (MIST_DEFAULT_RADIUS, MIST_DEFAULT_MIX) if calibrated else (0.0, 33.0)


def _bounded(value: Any, default: float, maximum: float) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(max(value, 0.0), maximum) if isfinite(value) else default


def mist_params(effects: Mapping[str, Any]) -> tuple[bool, float, float]:
    """Read the enabled flag, normalized radius, and 0–100 Mixer value."""
    calibrated = mist_is_calibrated(effects) or MIST_KEYS.isdisjoint(effects)
    radius, mix = mist_defaults(calibrated)
    return (
        effects.get("mist_enabled") is True,
        _bounded(effects.get("mist_radius"), radius, MIST_MAX_RADIUS if calibrated else 1.0),
        _bounded(effects.get("mist_mix"), mix, MIST_MAX_MIX),
    )


def mist_is_active(effects: Mapping[str, Any]) -> bool:
    return mist_render_params(effects)[1] != 0.0


def mist_render_params(effects: Mapping[str, Any]) -> tuple[float, float]:
    """Resolve spread in reference pixels and signed diffusion into uniforms."""
    enabled, radius, mixer = mist_params(effects)
    if not enabled or radius == 0.5:
        return 0.0, 0.0
    # A radius of 0.5 is neutral. Above it the radius controls direct softness.
    # Below it, Mixer moves continuously from diffusion to edge reinforcement.
    spread = abs(radius - 0.5) * 22.5
    amount = 1.0 if radius > 0.5 else 1.0 - 2.0 * mixer / MIST_MAX_MIX
    return spread, amount


# Staggered symmetric taps approximate a broad Gaussian in 32 reads. Swapping
# the axes on the second grid preserves symmetry without a second GPU pass.
_INNER_OFFSETS = (-1.17, -0.23, 0.23, 1.17)
_OUTER_OFFSETS = (-1.78, -0.70, 0.70, 1.78)
_WEIGHTS = (0.16, 0.34, 0.34, 0.16)
_REFERENCE_WIDTH = 2048.0
_NARROW_SIGMA_SCALE = 22.8 / 22.5
_NARROW_BLEND_START = 1.1
_NARROW_BLEND_END = 1.6


def _mist_shader(*, metal: bool) -> str:
    """Generate the same filter for GLSL and both Metal corrector paths."""
    vec2, vec3 = ("float2", "float3") if metal else ("vec2", "vec3")
    texture_args = "texture2d<float> tex, sampler smp" if metal else "sampler2D tex"
    texture_size = ("float2(tex.get_width(), tex.get_height())" if metal
                    else "vec2(textureSize(tex, 0))")
    arrays = []
    for name, values in (("inner", _INNER_OFFSETS), ("outer", _OUTER_OFFSETS),
                         ("weights", _WEIGHTS)):
        numbers = ", ".join(map(str, values))
        initializer = f"{{{numbers}}}" if metal else f"float[4]({numbers})"
        arrays.append(f"    const float {name}[4] = {initializer};")
    sample = "tex.sample(smp, {uv}).rgb" if metal else "texture(tex, {uv}).rgb"
    declarations = "\n".join(arrays)
    return f"""
{vec3} mist_delta({texture_args}, {vec2} uv, {vec3} center,
                  {vec2} tex_size, float spread, float amount, bool calibrated) {{
{declarations}
    {vec2} step_uv = (spread / {_REFERENCE_WIDTH}) * {vec2}(1.0, tex_size.x / tex_size.y);
    {vec2} size = {texture_size};
    {vec2} inset = 0.5 / size;
    {vec2} max_uv = {vec2}(1.0) - inset;
    float sigma = spread * size.x / {_REFERENCE_WIDTH} * {_NARROW_SIGMA_SCALE};
    float narrow_mix = calibrated ? 1.0 - smoothstep({_NARROW_BLEND_START}, {_NARROW_BLEND_END}, sigma) : 0.0;
    {vec3} narrow = {vec3}(0.0);
    if (narrow_mix > 0.0) {{
        // Pair neighbouring Gaussian weights into one bilinear read per axis.
        float t = 0.5 / max(sigma * sigma, 1e-8);
        float w1 = exp(-t), w2 = exp(-4.0 * t), w3 = exp(-9.0 * t), w4 = exp(-16.0 * t);
        float inner_weight = w1 + w2, outer_weight = w3 + w4;
        float inner_offset = 1.0 + w2 / max(inner_weight, 1e-12);
        float outer_offset = 3.0 + w4 / max(outer_weight, 1e-12);
        float normal = 1.0 + 2.0 * (inner_weight + outer_weight);
        float offsets[5];
        offsets[0] = -outer_offset; offsets[1] = -inner_offset; offsets[2] = 0.0;
        offsets[3] = inner_offset; offsets[4] = outer_offset;
        float weights_near[5];
        weights_near[0] = outer_weight / normal; weights_near[1] = inner_weight / normal;
        weights_near[2] = 1.0 / normal; weights_near[3] = weights_near[1]; weights_near[4] = weights_near[0];
        for (int y = 0; y < 5; ++y) {{
            for (int x = 0; x < 5; ++x) {{
                if (x == 2 && y == 2) continue;
                {vec2} nearby = clamp(uv + {vec2}(offsets[x], offsets[y]) / size, inset, max_uv);
                narrow += ({sample.format(uv="nearby")} - center) * (weights_near[x] * weights_near[y]);
            }}
        }}
        if (narrow_mix >= 1.0) return amount * narrow;
    }}
    {vec3} delta = {vec3}(0.0);
    for (int y = 0; y < 4; ++y) {{
        for (int x = 0; x < 4; ++x) {{
            {vec2} uv_a = clamp(uv + {vec2}(inner[x], outer[y]) * step_uv, inset, max_uv);
            {vec2} uv_b = clamp(uv + {vec2}(outer[y], inner[x]) * step_uv, inset, max_uv);
            float weight = 0.5 * weights[x] * weights[y];
            delta += (({sample.format(uv="uv_a")} - center)
                    + ({sample.format(uv="uv_b")} - center)) * weight;
        }}
    }}
    return amount * mix(delta, narrow, narrow_mix);
}}
"""


MIST_GLSL = _mist_shader(metal=False)
MIST_MSL = _mist_shader(metal=True)


def _axis_kernel(offsets: tuple[float, ...], spread: float) -> np.ndarray:
    import numpy as np

    extent = -floor(offsets[0] * spread)
    kernel = np.zeros(2 * extent + 1, dtype=np.float32)
    for offset, weight in zip(offsets, _WEIGHTS):
        position = offset * spread
        left = floor(position)
        fraction = position - left
        kernel[extent + left] += weight * (1.0 - fraction)
        if fraction:
            kernel[extent + left + 1] += weight * fraction
    return kernel


def apply_mist_cpu(
    color: np.ndarray, spread: float, amount: float, *, calibrated: bool = False,
) -> np.ndarray:
    """Evaluate the same two bilinear grids using separable CPU filters."""
    import cv2
    import numpy as np

    pixel_spread = spread * color.shape[1] / _REFERENCE_WIDTH
    narrow_mix = 0.0
    sigma = pixel_spread * _NARROW_SIGMA_SCALE
    if calibrated and sigma < _NARROW_BLEND_END:
        t = max(0.0, (sigma - _NARROW_BLEND_START) / (_NARROW_BLEND_END - _NARROW_BLEND_START))
        narrow_mix = 1.0 - t * t * (3.0 - 2.0 * t)
        positions = np.arange(-4, 5, dtype=np.float32)
        weights = np.exp(-0.5 * positions**2 / max(sigma**2, 1e-8))
        weights /= weights.sum()
        narrow = cv2.sepFilter2D(color, -1, weights, weights, borderType=cv2.BORDER_REPLICATE)
        if narrow_mix == 1.0:
            return color + amount * (narrow - color)
    inner = _axis_kernel(_INNER_OFFSETS, pixel_spread)
    outer = _axis_kernel(_OUTER_OFFSETS, pixel_spread)
    blurred = cv2.sepFilter2D(color, -1, inner, outer, borderType=cv2.BORDER_REPLICATE)
    blurred += cv2.sepFilter2D(color, -1, outer, inner, borderType=cv2.BORDER_REPLICATE)
    blurred *= 0.5
    if narrow_mix:
        blurred += (narrow - blurred) * narrow_mix
    blurred -= color
    blurred *= amount
    blurred += color
    return blurred
