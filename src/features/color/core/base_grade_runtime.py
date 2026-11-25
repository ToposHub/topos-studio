"""Render binding for Base Grade's flat, per-Corrector controls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from functools import lru_cache
from pathlib import PurePath
from typing import Any

import numpy as np

from src.features.color.core.base_grade import (
    apply_base_grade_linear,
    base_grade_active,
    base_grade_value,
)
from src.shared.color.color_space_descriptor import (
    LINEAR_SCENE, SRGB_IMAGE, ColorSpaceDescriptor, descriptor_for_space_preset,
)
from src.shared.color.color_transform_provider import BuiltinColorProvider, VideoSignalLuminance
from src.shared.color.project_video_signal import legacy_video_timeline_space
from src.shared.color.video_signal_gl import (
    VIDEO_SIGNAL_TRANSFER_GLSL, VIDEO_SIGNAL_TRANSFER_MSL,
    video_signal_luminance_uniform, video_signal_uniform_values,
)
from src.shared.project.color_graph import ColorGraphData

BASE_GRADE_SIGNAL_KEY = "base_grade_working_signal"
_REC709_G24 = descriptor_for_space_preset("rec709-g24")
_COLOR_PROVIDER = BuiltinColorProvider()


def base_grade_timeline_space(
    space: ColorSpaceDescriptor | None, settings: Mapping[str, Any] | None,
    project: Any = None, asset_id: str | None = None,
) -> ColorSpaceDescriptor:
    """Resolve old timeline pixels in the source's established display encoding."""
    if space is not None:
        return space
    resolve_asset = getattr(project, "get_asset_by_id", None)
    asset = resolve_asset(asset_id) if asset_id and callable(resolve_asset) else None
    if getattr(asset, "media_type", None) in ("image", "image_sequence", "solid"):
        suffix = PurePath(str(getattr(asset, "path", ""))).suffix.lower()
        return LINEAR_SCENE if suffix in (".exr", ".hdr") else SRGB_IMAGE
    return legacy_video_timeline_space(settings)

_ZONES = ("dim", "light", "dark", "bright")
_CONTROL_GROUPS = (
    ("global", ("flare", "contrast", "contrast_pivot", "saturation")),
    *((zone, tuple(f"{zone}_{control}" for control in ("exposure", "a", "b")))
      for zone in ("balance", *_ZONES)),
    *((f"{zone}_range", tuple(f"{zone}_{control}" for control in ("pivot", "falloff", "saturation")))
      for zone in _ZONES),
)
BASE_GRADE_UNIFORMS = tuple(
    (f"u_base_grade_{name}", f"vec{len(keys)}") for name, keys in _CONTROL_GROUPS)
BASE_GRADE_SIGNAL_VECTORS = tuple(f"u_base_grade_{key}" for key in (
    "luminance", "to_linear_r", "to_linear_g", "to_linear_b",
    "from_linear_r", "from_linear_g", "from_linear_b",
))
BASE_GRADE_SIGNAL_UNIFORMS = (("u_base_grade_transfer", "int"), *(
    (name, "vec3") for name in BASE_GRADE_SIGNAL_VECTORS))


def base_grade_working_signal(
    space: ColorSpaceDescriptor | None, luminance: VideoSignalLuminance | None = None,
    *, explicit_rec709: bool = False,
) -> dict[str, Any] | None:
    """Describe a non-Rec.709 working signal for a render snapshot."""
    if space is None or space == LINEAR_SCENE or (space == _REC709_G24 and not explicit_rec709):
        return None
    return {"space": space.to_dict(), "luminance": video_signal_luminance_uniform(luminance)}


def base_grade_graph_signal(
    graph: ColorGraphData, space: ColorSpaceDescriptor | None,
    luminance: VideoSignalLuminance | None = None,
    *, explicit_rec709: bool = False,
) -> ColorGraphData:
    """Attach colorimetry only to temporary execution graphs with Base Grade."""
    signal = base_grade_working_signal(space, luminance, explicit_rec709=explicit_rec709)
    if signal is None:
        return graph
    from src.features.color.core.base_grade import BASE_GRADE_DEFAULTS
    from src.features.color.core.color_graph_param_source import _iter_corrector_nodes

    affected = any(
        base_grade_active(node.legacy_payload.get("grading_params") or {})
        or base_grade_active(node.params.get("grade") or {})
        or any(key in (node.params.get("grade_keyframes") or {})
               or key in (node.legacy_payload.get("grade_keyframes") or {})
               for key in BASE_GRADE_DEFAULTS)
        for node in _iter_corrector_nodes(graph)
    )
    if not affected:
        return graph
    return replace(graph, working_color_context={**graph.working_color_context, BASE_GRADE_SIGNAL_KEY: signal})


@lru_cache(maxsize=32)
def _signal_matrices(space: ColorSpaceDescriptor) -> tuple[int, tuple[tuple[float, ...], ...]]:
    mode, _, to_linear = video_signal_uniform_values(space, LINEAR_SCENE)
    _, _, from_linear = video_signal_uniform_values(LINEAR_SCENE, space)
    # The source helper returns column-major values. Each dot product gets a row.
    rows = tuple(tuple(matrix[row::3]) for matrix in (to_linear, from_linear) for row in range(3))
    return mode, rows


def base_grade_signal_uniform_values(signal: Mapping[str, Any] | None) -> dict[str, Any]:
    if not signal:
        return {"u_base_grade_transfer": -1, **dict.fromkeys(BASE_GRADE_SIGNAL_VECTORS, (0.0,) * 3)}
    mode, rows = _signal_matrices(ColorSpaceDescriptor.from_dict(signal["space"]))
    return {"u_base_grade_transfer": mode,
            **dict(zip(BASE_GRADE_SIGNAL_VECTORS, (tuple(signal["luminance"]), *rows)))}


def base_grade_uniform_values(params: Mapping[str, Any]) -> dict[str, tuple[float, ...]]:
    """Pack the same checked values in whole vectors for GLSL and Metal."""
    return {f"u_base_grade_{name}": tuple(base_grade_value(params, key) for key in keys)
            for name, keys in _CONTROL_GROUPS}


def base_grade_shader_call(color: str, encoded: str, *, metal_layout: str | None = None) -> str:
    """Generate a call for uniforms, a composition struct, or a packed buffer."""
    if metal_layout == "graph":
        components = iter(range(sum(len(keys) for _, keys in _CONTROL_GROUPS)))
        arguments = [f"float{len(keys)}(" + ", ".join(
            f"p.base_grade[{next(components)}]" for _ in keys) + ")"
            for _, keys in _CONTROL_GROUPS]
        transfer = "p.base_grade_transfer"
        arguments += ["float3(" + ", ".join(f"p.base_grade_signal[{index + i}]" for i in range(3)) + ")"
                      for index in range(0, 21, 3)]
    elif metal_layout == "composition":
        arguments = [f"float{len(keys)}(" + ", ".join(
            f"params.u_base_grade_{name}_{component}" for component in "xyzw"[:len(keys)]) + ")"
            for name, keys in _CONTROL_GROUPS]
        transfer = "params.u_base_grade_transfer"
        arguments += ["float3(" + ", ".join(f"params.{name}_{c}" for c in "xyz") + ")"
                      for name in BASE_GRADE_SIGNAL_VECTORS]
    else:
        transfer = "u_base_grade_transfer"
        arguments = [name for name, _ in BASE_GRADE_UNIFORMS] + list(BASE_GRADE_SIGNAL_VECTORS)
    return f"apply_base_grade_in_signal({color}, {encoded}, {transfer}, {', '.join(arguments)})"


def apply_base_grade_working(
    rgb: np.ndarray, params: Mapping[str, Any], *, encoded_input: bool = False,
    signal: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Apply in linear Rec.709, round-tripping the working signal as needed."""
    if not base_grade_active(params):
        return rgb
    if signal:
        space = ColorSpaceDescriptor.from_dict(signal["space"])
        luminance = VideoSignalLuminance(*signal["luminance"])
        linear = _COLOR_PROVIDER.transform_signal(rgb, space, LINEAR_SCENE, luminance=luminance)
        graded = apply_base_grade_linear(linear, params)
        return _COLOR_PROVIDER.transform_signal(graded, LINEAR_SCENE, space, luminance=luminance)
    if not encoded_input:
        return apply_base_grade_linear(rgb, params)
    linear = np.copysign(np.power(np.abs(rgb), 2.4), rgb)
    graded = apply_base_grade_linear(linear, params)
    return np.copysign(np.power(np.abs(graded), 1.0 / 2.4), graded)


_SIGNAL_WRAPPER_GLSL = """
vec3 apply_base_grade_in_signal(vec3 color, bool encoded, int transfer,
    vec4 settings, vec3 balance, vec3 dim, vec3 light, vec3 dark, vec3 bright,
    vec3 dim_range, vec3 light_range, vec3 dark_range, vec3 bright_range,
    vec3 luminance, vec3 to_r, vec3 to_g, vec3 to_b, vec3 from_r, vec3 from_g, vec3 from_b) {
    if (transfer >= 0) {
        color = topos_video_decode(color, transfer, luminance);
        color = vec3(dot(color, to_r), dot(color, to_g), dot(color, to_b));
    }
    color = apply_base_grade(color, encoded && transfer < 0, settings, balance, dim, light,
                             dark, bright, dim_range, light_range, dark_range, bright_range);
    if (transfer >= 0) {
        color = vec3(dot(color, from_r), dot(color, from_g), dot(color, from_b));
        color = topos_video_encode(color, transfer, luminance);
    }
    return color;
}
"""


def base_grade_glsl_source() -> str:
    from src.features.color.core.base_grade import GLSL_BASE_GRADE

    return VIDEO_SIGNAL_TRANSFER_GLSL + GLSL_BASE_GRADE + _SIGNAL_WRAPPER_GLSL


def base_grade_msl_source() -> str:
    from src.features.color.core.base_grade import MSL_BASE_GRADE

    wrapper = _SIGNAL_WRAPPER_GLSL.replace("vec3", "float3").replace("vec4", "float4")
    return VIDEO_SIGNAL_TRANSFER_MSL + MSL_BASE_GRADE + "\nstatic inline " + wrapper.lstrip()


def metal_graph_base_grade_source() -> str:
    """Add Base Grade to shared Metal pointwise kernels via their optional hook."""
    apply = base_grade_shader_call("color", "p.base_grade_encoded != 0u", metal_layout="graph")
    return (
        "#define TOPOS_GRAPH_BASE_GRADE\n"
        + base_grade_msl_source()
        + "\nstatic inline float3 graph_apply_base_grade(float3 color, constant GraphGradingParams& p) {\n"
        + f"    return p.base_grade_enabled != 0u ? {apply} : color;\n}}\n"
    )
