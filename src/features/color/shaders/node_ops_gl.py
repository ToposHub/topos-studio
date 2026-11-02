"""节点分支/键/通道算子的 GL shader（B10，spec v1 逐条对应）。

每个 shader 与 ``docs/color_nodes_mixer_spec.md`` 公式一一对应；CPU
oracle（N09）为等价参考。HDR 不截断；key 不写 image alpha。
"""

from __future__ import annotations

NODE_OPS_VERT = """
#version 330
in vec2 in_vert; in vec2 in_texcoord;
out vec2 v_uv;
void main(){ gl_Position = vec4(in_vert, 0.0, 1.0); v_uv = in_texcoord; }
"""

_OPS_HEADER = "#version 330\nin vec2 v_uv;\n"

#: Parallel Mixer 单分支折叠（spec §1）：out = base*(1-k) + branch*k
PARALLEL_MIX_FRAG = _OPS_HEADER + """
uniform sampler2D u_base;
uniform sampler2D u_branch;
uniform sampler2D u_key;
out vec4 f_color;
void main(){
    vec4 base = texture(u_base, v_uv);
    vec4 branch = texture(u_branch, v_uv);
    float k = clamp(texture(u_key, v_uv).r, 0.0, 1.0);
    f_color = vec4(mix(base.rgb, branch.rgb, k), base.a);
}
"""

#: Layer Mixer（spec §2）：mode 0 Normal / 1 Add / 2 Subtract / 3 Multiply / 4 Screen
LAYER_MIX_FRAG = _OPS_HEADER + """
uniform sampler2D u_bottom;
uniform sampler2D u_top;
uniform float u_opacity;
uniform int u_mode;
out vec4 f_color;
void main(){
    vec4 bottom = texture(u_bottom, v_uv);
    vec4 top = texture(u_top, v_uv);
    vec3 a = bottom.rgb; vec3 b = top.rgb;
    vec3 outc;
    if (u_mode == 0)      { outc = a * (1.0 - u_opacity) + b * u_opacity; }
    else if (u_mode == 1) { outc = a + b * u_opacity; }
    else if (u_mode == 2) { outc = a - b * u_opacity; }
    else if (u_mode == 3) { outc = a * (1.0 - u_opacity + u_opacity * b); }
    else                  { outc = 1.0 - (1.0 - a) * (1.0 - b * u_opacity); }
    f_color = vec4(outc, bottom.a);
}
"""

#: Key Mixer（spec §3）：mode 0 add / 1 subtract / 2 intersect（输出 R 通道）
KEY_MIX_FRAG = _OPS_HEADER + """
uniform sampler2D u_a;
uniform sampler2D u_b;
uniform int u_mode;
out vec4 f_color;
void main(){
    float a = clamp(texture(u_a, v_uv).r, 0.0, 1.0);
    float b = clamp(texture(u_b, v_uv).r, 0.0, 1.0);
    float k;
    if (u_mode == 0)      { k = max(a, b); }
    else if (u_mode == 1) { k = clamp(a - b, 0.0, 1.0); }
    else                  { k = a * b; }
    f_color = vec4(k, k, k, 1.0);
}
"""

#: Splitter（spec §5）：channel 0/1/2 → 灰度载体
SPLITTER_FRAG = _OPS_HEADER + """
uniform sampler2D u_image;
uniform int u_channel;
out vec4 f_color;
void main(){
    vec4 texel = texture(u_image, v_uv);
    float v = texel.r;
    if (u_channel == 1) { v = texel.g; }
    else if (u_channel == 2) { v = texel.b; }
    f_color = vec4(vec3(v), texel.a);
}
"""

#: Combiner（spec §5）：R/G/B 载体重组
COMBINER_FRAG = _OPS_HEADER + """
uniform sampler2D u_r;
uniform sampler2D u_g;
uniform sampler2D u_b;
out vec4 f_color;
void main(){
    float r = texture(u_r, v_uv).r;
    float g = texture(u_g, v_uv).g;
    float b = texture(u_b, v_uv).b;
    float a = texture(u_r, v_uv).a;
    f_color = vec4(r, g, b, a);
}
"""

#: key 补集（Outside §4 / key 工具）
KEY_COMPLEMENT_FRAG = _OPS_HEADER + """
uniform sampler2D u_key;
out vec4 f_color;
void main(){
    float k = clamp(texture(u_key, v_uv).r, 0.0, 1.0);
    float c = 1.0 - k;
    f_color = vec4(c, c, c, 1.0);
}
"""

LAYER_MODE_INDEX = {"Normal": 0, "Add": 1, "Subtract": 2,
                    "Multiply": 3, "Screen": 4}
KEY_MODE_INDEX = {"add": 0, "subtract": 1, "intersect": 2}

#: 执行器分派表：op 名 → fragment 源
OP_FRAGMENTS = {
    "parallel_mix": PARALLEL_MIX_FRAG,
    "layer_mix": LAYER_MIX_FRAG,
    "key_mix": KEY_MIX_FRAG,
    "splitter": SPLITTER_FRAG,
    "combiner": COMBINER_FRAG,
    "key_complement": KEY_COMPLEMENT_FRAG,
    "key_passthrough": KEY_COMPLEMENT_FRAG.replace(
        "float c = 1.0 - k;", "float c = k;"),
}
