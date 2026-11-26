"""Render bindings for the ordered Chromogen grade."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np

from src.features.color.core.base_grade_runtime import (
    BASE_GRADE_SIGNAL_VECTORS, apply_linear_grade_in_working_space,
    metal_graph_base_grade_source,
)
from src.features.color.core.chromogen import (
    CHROMOGEN_KEY, CHROMOGEN_MAX_STAGES, CHROMOGEN_STAGE_FLOATS,
    apply_chromogen_linear, chromogen_active, chromogen_compatibility_error,
    chromogen_uniform_rows, normalize_chromogen,
)
from src.shared.color.video_signal_gl import VIDEO_SIGNAL_TRANSFER_GLSL, VIDEO_SIGNAL_TRANSFER_MSL

if TYPE_CHECKING:
    from src.features.shared.effects.effect_spec import EffectSpec


_ROWS_PER_STAGE = CHROMOGEN_STAGE_FLOATS // 4
CHROMOGEN_UNIFORM_ROWS = CHROMOGEN_MAX_STAGES * _ROWS_PER_STAGE


def apply_chromogen_working(
    rgb: np.ndarray, params: Mapping[str, Any], *, encoded_input: bool = False,
    signal: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Evaluate the stack in linear Rec.709 and restore the working encoding."""
    if error := chromogen_compatibility_error(params.get(CHROMOGEN_KEY)):
        raise ValueError(error)
    if not chromogen_active(params):
        return rgb
    return apply_linear_grade_in_working_space(
        np.asarray(rgb, dtype=np.float32), lambda color: apply_chromogen_linear(color, params[CHROMOGEN_KEY]),
        encoded_input=encoded_input, signal=signal)


def chromogen_uniform_values(params: Mapping[str, Any]) -> dict[str, Any]:
    state = normalize_chromogen(params[CHROMOGEN_KEY])
    rows = chromogen_uniform_rows(state)
    count = max((index + 1 for index, row in enumerate(rows[::_ROWS_PER_STAGE]) if row[0]), default=0)
    return {"u_chromogen_stage_count": count, "u_chromogen_stages": rows,
            "u_chromogen_amount": state["amount"] if state is not None else 0.0}


def _signal_args(metal: bool) -> str:
    vector = "float3" if metal else "vec3"
    return ", ".join(f"{vector} {name}" for name in (
        "luminance", "to_r", "to_g", "to_b", "from_r", "from_g", "from_b",
    ))


_GLSL_STACK = f"""
vec3 apply_chromogen_working(vec3 color, bool encoded, int transfer, {_signal_args(False)}) {{
    if (u_chromogen_stage_count == 0 || u_chromogen_amount == 0.0) return color;
    if (transfer >= 0) {{
        color = topos_video_decode(color, transfer, luminance);
        color = vec3(dot(color, to_r), dot(color, to_g), dot(color, to_b));
    }} else if (encoded) {{
        color = topos_video_signed_power(color, 2.4);
    }}
    vec3 source = color;
    for (int index = 0; index < {CHROMOGEN_MAX_STAGES}; ++index) {{
        if (index >= u_chromogen_stage_count) break;
        int offset = index * {_ROWS_PER_STAGE};
        color = chromogen_apply_stage(color, u_chromogen_stages[offset], u_chromogen_stages[offset + 1],
            u_chromogen_stages[offset + 2], u_chromogen_stages[offset + 3],
            u_chromogen_stages[offset + 4], u_chromogen_stages[offset + 5]);
    }}
    if (u_chromogen_amount != 1.0) color = source + u_chromogen_amount * (color - source);
    if (transfer >= 0) {{
        color = vec3(dot(color, from_r), dot(color, from_g), dot(color, from_b));
        color = topos_video_encode(color, transfer, luminance);
    }} else if (encoded) {{
        color = topos_video_signed_power(color, 1.0 / 2.4);
    }}
    return color;
}}
"""


_MSL_STACK = f"""
static inline float4 chromogen_stage_row(constant float *rows, int start) {{
    return float4(rows[start], rows[start + 1], rows[start + 2], rows[start + 3]);
}}

__attribute__((noinline)) static float3 apply_chromogen_working(float3 color, bool encoded, int transfer,
    {_signal_args(True)}, int count, float amount, constant float *rows) {{
    if (count == 0 || amount == 0.0) return color;
    if (transfer >= 0) {{
        color = topos_video_decode(color, transfer, luminance);
        color = float3(dot(color, to_r), dot(color, to_g), dot(color, to_b));
    }} else if (encoded) {{
        color = topos_video_signed_power(color, 2.4);
    }}
    float3 source = color;
    int stage_count = min(count, {CHROMOGEN_MAX_STAGES});
#pragma clang loop unroll(disable)
    for (int index = 0; index < stage_count; ++index) {{
        int offset = index * {CHROMOGEN_STAGE_FLOATS};
        color = chromogen_apply_stage(color,
            chromogen_stage_row(rows, offset), chromogen_stage_row(rows, offset + 4),
            chromogen_stage_row(rows, offset + 8), chromogen_stage_row(rows, offset + 12),
            chromogen_stage_row(rows, offset + 16), chromogen_stage_row(rows, offset + 20));
    }}
    if (amount != 1.0) color = source + amount * (color - source);
    if (transfer >= 0) {{
        color = float3(dot(color, from_r), dot(color, from_g), dot(color, from_b));
        color = topos_video_encode(color, transfer, luminance);
    }} else if (encoded) {{
        color = topos_video_signed_power(color, 1.0 / 2.4);
    }}
    return color;
}}
"""


def chromogen_glsl_source(*, include_transfer: bool) -> str:
    from src.features.color.core.chromogen import CHROMOGEN_GLSL_STAGE

    return (VIDEO_SIGNAL_TRANSFER_GLSL if include_transfer else "") + CHROMOGEN_GLSL_STAGE + _GLSL_STACK


def chromogen_msl_source(*, include_transfer: bool) -> str:
    from src.features.color.core.chromogen import CHROMOGEN_MSL_STAGE

    return (VIDEO_SIGNAL_TRANSFER_MSL if include_transfer else "") + CHROMOGEN_MSL_STAGE + _MSL_STACK


def chromogen_shader_call(color: str, encoded: str, *, metal_layout: str | None = None) -> str:
    """Use the existing managed-primary colorimetry in each backend layout."""
    if metal_layout == "graph":
        vectors = ["float3(" + ", ".join(f"p.base_grade_signal[{row + i}]" for i in range(3)) + ")"
                   for row in range(0, 21, 3)]
        vectors += ["int(p.chromogen_stage_count)", "p.chromogen_amount", "p.chromogen_stages"]
        transfer = "(p.base_grade_transfer - 1)"
    elif metal_layout == "composition":
        vectors = ["float3(" + ", ".join(f"params.{name}_{c}" for c in "xyz") + ")"
                   for name in BASE_GRADE_SIGNAL_VECTORS]
        vectors += ["params.u_chromogen_stage_count", "params.u_chromogen_amount", "params.u_chromogen_stages"]
        transfer = "params.u_base_grade_transfer"
    else:
        vectors = list(BASE_GRADE_SIGNAL_VECTORS)
        transfer = "u_base_grade_transfer"
    return f"apply_chromogen_working({color}, {encoded}, {transfer}, {', '.join(vectors)})"


def metal_graph_chromogen_source() -> str:
    """Extend the native shared pointwise kernels after Base Grade's signal helpers."""
    call = chromogen_shader_call("color", "p.base_grade_encoded != 0u", metal_layout="graph")
    return (
        "\n#define TOPOS_GRAPH_CHROMOGEN\n"
        + chromogen_msl_source(include_transfer=False)
        + "\nstatic inline float3 graph_apply_chromogen(float3 color, constant GraphGradingParams& p) {\n"
        + f"    return {call};\n}}\n"
    )


def metal_managed_grading_source() -> str:
    """Emit the native pointwise look stages with one shared transfer library."""
    return metal_graph_base_grade_source() + metal_graph_chromogen_source()


def chromogen_effect_spec() -> EffectSpec:
    """Internal Color operator with a versioned stack schema and native backends."""
    from src.features.composite.nodes.base_node import ParamDef
    from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind

    signal_declarations = "\n".join(f"uniform vec3 {name};" for name in BASE_GRADE_SIGNAL_VECTORS)
    fragment = f"""#version 330
in vec2 v_texcoord;
out vec4 f_color;
uniform sampler2D u_input_tex;
uniform int u_chromogen_stage_count;
uniform float u_chromogen_amount;
uniform vec4 u_chromogen_stages[{CHROMOGEN_UNIFORM_ROWS}];
uniform bool u_rec709_calibration_encoded;
uniform int u_base_grade_transfer;
{signal_declarations}
{chromogen_glsl_source(include_transfer=True)}
void main() {{
    vec4 source = texture(u_input_tex, v_texcoord);
    f_color = vec4({chromogen_shader_call('source.rgb', 'u_rec709_calibration_encoded')}, source.a);
}}
"""
    return EffectSpec(
        effect_type="chromogen", name="Look Dev", category="filter", internal=True,
        param_defs=(ParamDef(CHROMOGEN_KEY, "dict", None, hidden=True, allow_expression=False),),
        shader_kind=ShaderKind.SINGLE_PASS, shader_sources=(fragment,), dispatch_key="chromogen",
        description="Ordered version 1 Color look stages with CPU, OpenGL and Metal rendering.",
        plugin_capabilities=("gpu_preview", "export_opengl", "export_cpu", "export_metal"),
        graphics_backends=("opengl", "metal"),
        compute_backends=("glsl_fragment", "metal_compute", "cpu"),
    )
