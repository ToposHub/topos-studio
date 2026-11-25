"""GPU sampling for the shared X Grade response field."""
from __future__ import annotations

from collections.abc import Iterable

from src.features.color.core.warper_chroma709 import (
    _CW_LUMA, _CW_PLOT_INVERSE, _CW_PLOT_POWER, _CW_PLOT_WEIGHTS, _CW_VERTICES,
)
from src.features.color.core.warper_color import (
    RESOLVE_HS709_SPACE, RESOLVE_HS709_V2_SPACE, RESOLVE_HS709_V3_SPACE, WARPER_CHROMA709_SPACE,
)
from src.features.color.core.x_grade_render import _TONAL_LOG_MAX, _TONAL_SCALE


def _constant_vector(vector_type: str, values: Iterable[float]) -> str:
    return f"{vector_type}({', '.join(str(float(v)) for v in values)})"


def _color_math(vec2: str, vec3: str) -> str:
    def v2(values: Iterable[float]) -> str:
        return _constant_vector(vec2, values)

    def v3(values: Iterable[float]) -> str:
        return _constant_vector(vec3, values)

    return f"""
{vec2} xgrade_position({vec3} linear) {{
    float peak = max(linear.r, max(linear.g, linear.b));
    {vec3} weights = pow(linear / peak, {vec3}({_CW_PLOT_POWER / 2.4})) * {v3(_CW_PLOT_WEIGHTS)};
    return (weights.r * {v2(_CW_VERTICES[0])} + weights.g * {v2(_CW_VERTICES[1])}
          + weights.b * {v2(_CW_VERTICES[2])}) / (weights.r + weights.g + weights.b);
}}

{vec3} xgrade_chromaticity({vec2} uv, float luma) {{
    {vec3} point = {vec3}(uv, 1.0);
    {vec3} barycentric = {vec3}(dot(point, {v3(_CW_PLOT_INVERSE[0])}),
                               dot(point, {v3(_CW_PLOT_INVERSE[1])}),
                               dot(point, {v3(_CW_PLOT_INVERSE[2])}));
    {vec3} ratio = pow(max(barycentric, 0.0) / {v3(_CW_PLOT_WEIGHTS)}, {vec3}({2.4 / _CW_PLOT_POWER}));
    return ratio * (luma / max(dot(ratio, {v3(_CW_LUMA)}), 1e-12));
}}

float xgrade_tone(float luma, float levels) {{
    return min(log2(1.0 + luma * {_TONAL_SCALE}) / {_TONAL_LOG_MAX}, 1.0) * (levels - 1.0);
}}
"""


GLSL_X_GRADE = _color_math("vec2", "vec3") + f"""
vec3 apply_x_grade(vec3 color, sampler2D field) {{
    if (any(isnan(color)) || any(isinf(color))) return color;
    vec3 meta = texelFetch(field, ivec2(0, 0), 0).rgb;
    vec3 atlas = texelFetch(field, ivec2(1, 0), 0).rgb;
    if (meta.y > 0.0) {{
        vec2 origin = vec2(0.0, atlas.z);
        int mode = int(meta.x);
        if (mode == {WARPER_CHROMA709_SPACE}) {{
            color = apply_warper_chroma709_region(color, field, meta.yz, origin);
        }} else if (mode == {RESOLVE_HS709_SPACE}) {{
            color = apply_resolve_hs709_region(color, field, ivec2(meta.yz), ivec2(origin));
        }} else if (mode == {RESOLVE_HS709_V2_SPACE} || mode == {RESOLVE_HS709_V3_SPACE}) {{
            color = apply_resolve_hs709_v2_region(color, field, meta.yz, origin);
        }} else {{
            color = apply_warper_v2_region(color, field, mode, meta.yz, origin);
        }}
    }}
    vec3 positive = max(color, 0.0);
    bool protect_neutral = texelFetch(field, ivec2(2, 0), 0).r > 0.0;
    float spread = max(positive.r, max(positive.g, positive.b)) - min(positive.r, min(positive.g, positive.b));
    vec3 linear = u_rec709_calibration_encoded ? pow(positive, vec3(2.4)) : positive;
    float luma = dot(linear, {_constant_vector("vec3", _CW_LUMA)});
    if (luma <= 0.0) return color;
    vec2 uv = xgrade_position(linear);
    float tone = xgrade_tone(luma, atlas.y);
    vec2 pixel = clamp(uv, 0.0, 1.0) * (atlas.x - 1.0) + vec2(0.5, 1.5);
    vec2 low = pixel + vec2(0.0, floor(tone) * atlas.x);
    vec2 high = pixel + vec2(0.0, min(floor(tone) + 1.0, atlas.y - 1.0) * atlas.x);
    vec2 dims = vec2(textureSize(field, 0));
    vec3 delta = mix(texture(field, low / dims).rgb, texture(field, high / dims).rgb, fract(tone));
    if (protect_neutral && spread <= 1e-6) delta.xy = vec2(0.0);
    if (all(equal(delta, vec3(0.0)))) return color;
    if (any(notEqual(delta.xy, vec2(0.0)))) linear = xgrade_chromaticity(uv + delta.xy, luma);
    linear *= exp2(clamp(delta.z, -16.0, 16.0));
    return (u_rec709_calibration_encoded ? pow(linear, vec3(1.0 / 2.4)) : linear) + min(color, 0.0);
}}
"""


# Legacy HSL and HSP meshes in a packed atlas use the same transform as GLSL.
# The original Metal legacy path remains separately available.
MSL_X_GRADE = _color_math("float2", "float3") + f"""
float3 msl_xgrade_legacy(float3 color, texture2d<float> field, int mode, uint2 size, uint2 origin) {{
    float3 magnitude = abs(color);
    float3 hsv = msl_rgb2hsv(magnitude);
    float lower = min(magnitude.r, min(magnitude.g, magnitude.b));
    float lightness = (hsv.z + lower) * 0.5;
    if (mode == 1 && hsv.y != 0.0 && lightness >= 1.0) return color;
    float sat = mode == 1 ? (hsv.z - lower) / max(1.0 - abs(2.0 * lightness - 1.0), 1e-10) : hsv.y;
    if (sat < 0.02) return color;
    uint2 extent = size - 1u;
    float2 cell = clamp(float2(hsv.x, sat) * float2(size) - 0.5, 0.0, float2(extent));
    uint2 base = uint2(cell);
    uint2 next = min(base + 1u, extent);
    float2 delta = msl_hs709_response_tile(field, base + origin, next + origin, fract(cell)).rg;
    delta *= smoothstep(0.02, 0.15, sat);
    float hue = fract(hsv.x + delta.x);
    sat = clamp(sat + delta.y, 0.0, 1.0);
    float3 mapped = msl_hsv2rgb(float3(hue, sat, 1.0));
    if (mode == 1) {{
        float chroma = (1.0 - abs(2.0 * lightness - 1.0)) * sat;
        mapped = msl_hsv2rgb(float3(hue, 1.0, chroma)) + lightness - chroma * 0.5;
    }} else if (mode == 2) {{
        const float3 weights = float3(0.299, 0.587, 0.114);
        mapped *= sqrt(dot(weights, magnitude * magnitude) / max(dot(weights, mapped * mapped), 1e-10));
    }} else {{
        mapped *= hsv.z;
    }}
    return mapped * sign(color);
}}

float3 msl_apply_x_grade(float3 color, texture2d<float> field, bool encoded_input) {{
    if (!all(isfinite(color))) return color;
    float3 meta = field.read(uint2(0u, 0u)).rgb;
    float3 atlas = field.read(uint2(1u, 0u)).rgb;
    if (meta.y > 0.0) {{
        uint2 origin = uint2(0u, uint(atlas.z));
        int mode = int(meta.x);
        if (mode == {WARPER_CHROMA709_SPACE}) {{
            color = msl_apply_warper_chroma709_region(color, field, encoded_input, meta.yz, float2(origin));
        }} else if (mode == {RESOLVE_HS709_SPACE}) {{
            color = msl_apply_resolve_hs709_region(color, field, encoded_input, int2(meta.yz), origin);
        }} else if (mode == {RESOLVE_HS709_V2_SPACE} || mode == {RESOLVE_HS709_V3_SPACE}) {{
            color = msl_apply_resolve_hs709_v2_region(color, field, encoded_input, uint2(meta.yz), origin);
        }} else {{
            color = msl_xgrade_legacy(color, field, mode, uint2(meta.yz), origin);
        }}
    }}
    float3 positive = max(color, 0.0);
    bool protect_neutral = field.read(uint2(2u, 0u)).r > 0.0;
    float spread = max(positive.r, max(positive.g, positive.b)) - min(positive.r, min(positive.g, positive.b));
    float3 linear = encoded_input ? pow(positive, float3(2.4)) : positive;
    float luma = dot(linear, {_constant_vector("float3", _CW_LUMA)});
    if (luma <= 0.0) return color;
    float2 uv = xgrade_position(linear);
    float tone = xgrade_tone(luma, atlas.y);
    uint size = uint(atlas.x);
    uint levels = uint(atlas.y);
    float2 cell = clamp(uv, 0.0, 1.0) * (atlas.x - 1.0);
    uint2 base = uint2(cell);
    uint2 next = min(base + 1u, size - 1u);
    uint2 low = uint2(0u, 1u + uint(tone) * size);
    uint2 high = uint2(0u, 1u + min(uint(tone) + 1u, levels - 1u) * size);
    float3 first = msl_hs709_response_tile(field, base + low, next + low, fract(cell));
    float3 second = msl_hs709_response_tile(field, base + high, next + high, fract(cell));
    float3 delta = mix(first, second, fract(tone));
    if (protect_neutral && spread <= 1e-6) delta.xy = float2(0.0);
    if (all(delta == float3(0.0))) return color;
    if (any(delta.xy != float2(0.0))) linear = xgrade_chromaticity(uv + delta.xy, luma);
    linear *= exp2(clamp(delta.z, -16.0, 16.0));
    return (encoded_input ? pow(linear, float3(1.0 / 2.4)) : linear) + min(color, 0.0);
}}
"""
