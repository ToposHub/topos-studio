"""HDR-preserving whole-frame stabilization, shared by preview and export."""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from src.shared.tracking.stabilization_state import stabilization_matrix

_FRAGMENT = '''#version 330
uniform sampler2D u_texture;
uniform vec3 u_row0;
uniform vec3 u_row1;
uniform vec3 u_row2;
in vec2 v_uv;
out vec4 f_color;
void main() {
    vec3 p = vec3(v_uv.x, 1.0 - v_uv.y, 1.0);
    vec3 q = vec3(dot(u_row0, p), dot(u_row1, p), dot(u_row2, p));
    if (abs(q.z) <= 0.0000001) { f_color = vec4(0.0, 0.0, 0.0, 1.0); return; }
    vec2 uv = q.xy / q.z;
    vec2 size = vec2(textureSize(u_texture, 0));
    vec2 half_pixel = vec2(0.5) / size;
    if (any(lessThan(uv, -half_pixel)) || any(greaterThan(uv, vec2(1.0) + half_pixel))) {
        f_color = vec4(0.0, 0.0, 0.0, 1.0); return;
    }
    // Constant black border with subpixel blending, matching CPU warpPerspective.
    // Clamp to texel centers so an input texture's repeat mode cannot wrap edges.
    vec2 coverage = clamp(min(uv * size + vec2(0.5),
                             (vec2(1.0) - uv) * size + vec2(0.5)), vec2(0.0), vec2(1.0));
    uv = clamp(uv, half_pixel, vec2(1.0) - half_pixel);
    vec4 sample_color = texture(u_texture, vec2(uv.x, 1.0 - uv.y));
    f_color = mix(vec4(0.0, 0.0, 0.0, 1.0), sample_color, coverage.x * coverage.y);
}
'''


def stabilization_spec() -> Any:
    """Internal registered pass: complete 3x3 inverse-UV parameter schema, v1."""
    from src.features.composite.nodes.base_node import ParamDef
    from src.features.shared.effects.effect_spec import EffectSpec, ShaderKind
    return EffectSpec(
        effect_type='video_stabilization', name='Video Stabilization', category='transform',
        param_defs=tuple(ParamDef(f'u_row{i}', 'vec3', default, hidden=True)
                         for i, default in enumerate(((1., 0., 0.), (0., 1., 0.), (0., 0., 1.)))),
        shader_kind=ShaderKind.SINGLE_PASS, shader_sources=(_FRAGMENT,),
        dispatch_key='video_stabilization', preview_cost='low', internal=True,
        description='Clip camera stabilization v1; HDR GPU preview and CPU/OpenGL export.',
        plugin_capabilities=('gpu_preview', 'export_opengl', 'export_cpu'))


def _uv_inverse(state: dict, matrix: np.ndarray,
                output_rect: tuple[float, float, float, float] | None = None) -> np.ndarray:
    width, height = float(state['source_width']), float(state['source_height'])
    source = np.diag([width, height, 1.0])
    x, y, w, h = output_rect or (0., 0., width, height)
    output = np.array([[w, 0., x], [0., h, y], [0., 0., 1.]])
    return np.linalg.inv(source) @ np.linalg.inv(matrix) @ output


def apply_stabilization_gpu(runner: Any, input_glo: int, state: dict, source_frame: int,
                            width: int, height: int, *, dtype: str = 'f2',
                            output_rect: tuple[float, float, float, float] | None = None) -> int:
    """Run on the context-owning thread; output is immediate-use runner scratch."""
    matrix = stabilization_matrix(state, source_frame)
    if matrix is None:
        return input_glo
    inverse = _uv_inverse(state, matrix, output_rect)
    from src.features.shared.effects.effect_catalog import get_effect_catalog
    spec = get_effect_catalog().get_spec('video_stabilization')
    if spec is None:
        raise RuntimeError('Video stabilization effect is not registered')
    result = runner.run(spec, input_glo,
                        {f'u_row{i}': tuple(float(v) for v in row)
                         for i, row in enumerate(inverse)}, width, height, dtype=dtype)
    if result == input_glo:
        raise RuntimeError('Video stabilization GPU pass failed')
    return result


def apply_stabilization_cpu(image: np.ndarray, state: dict, source_frame: int) -> np.ndarray:
    """Warp without clamping float HDR data; input and output size are identical."""
    matrix = stabilization_matrix(state, source_frame)
    if matrix is None:
        return image
    height, width = image.shape[:2]
    scaling = np.diag([width / state['source_width'], height / state['source_height'], 1.])
    matrix = scaling @ matrix @ np.linalg.inv(scaling)
    border = (0., 0., 0., 1.) if image.ndim == 3 and image.shape[2] == 4 else 0.
    return cv2.warpPerspective(image, matrix, (width, height),
                               flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                               borderValue=border)


def clip_stabilization_state(project: Any, clip: Any) -> dict:
    """Resolve a clip-owned state without consulting live Color UI/engine."""
    stacks = getattr(project, 'color_stacks', {}) if project is not None else {}
    stack = stacks.get(getattr(clip, 'color_stack_id', None))
    state = getattr(stack, 'video_stabilization', {}) if stack is not None else {}
    return state if isinstance(state, dict) else {}
