"""A1: 共享混合模式 shader 源（GPU 预览 / GPU 导出共用）。

fragment shader 引入 ``shaders/blend.glsl`` 的 ``blend_channel(src, dst, mode)``
公式（与 ``blend_modes.py`` 的 NumPy 实现保持一致），叠加 source-over：

    mixed = blend_channel(overlay.rgb, base.rgb, u_blend_mode)
    f_color = vec4(mix(base.rgb, mixed, u_opacity * overlay.a), 1.0)

normoal 模式（u_blend_mode = 0）时 blend_channel 返回 overlay 原色，
公式退化为既有纯 alpha over，行为与旧 shader 完全一致。
"""

from __future__ import annotations

import os

_BLEND_PROGRAM_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "shaders", "blend.glsl"
)

_BLEND_VERTEX_SHADER_SOURCE = """
#version 330
in vec2 in_vert;
in vec2 in_texcoord;
out vec2 v_texcoord;
void main() {
    gl_Position = vec4(in_vert, 0.0, 1.0);
    v_texcoord = in_texcoord;
}
"""


def _read_blend_glsl() -> str:
    try:
        with open(_BLEND_PROGRAM_PATH, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


_BLEND_FRAGMENT_SHADER_SOURCE = """
#version 330
uniform sampler2D u_base;
uniform sampler2D u_overlay;
uniform float u_opacity;
uniform int u_blend_mode;
in vec2 v_texcoord;
out vec4 f_color;

""" + _read_blend_glsl() + """

void main() {
    vec4 base = texture(u_base, v_texcoord);
    vec4 overlay = texture(u_overlay, v_texcoord);
    vec3 mixed = blend_channel(overlay.rgb, base.rgb, u_blend_mode);
    float a = u_opacity * overlay.a;
    f_color = vec4(mix(base.rgb, mixed, a), 1.0);
}
"""

__all__ = [
    "_BLEND_VERTEX_SHADER_SOURCE",
    "_BLEND_FRAGMENT_SHADER_SOURCE",
    "_BLEND_PROGRAM_PATH",
]
