"""P7-02：结构化 uniform 绑定单元测试（ADR 0004 决策 2）。

覆盖：
1. manifest 声明生成与重构前手写文本逐行一致（G1 位级约束的静态锚点；
   动态像素级由 test_shader_golden_corpus 保证）；
2. MSL struct 布局：P6-06 对齐规则（4 字节紧排 / bool→uint / vec 逐分量
   展开），numpy dtype 与 struct 逐字段对齐；
3. 值提取语义基线（含 fx 空组总是写默认的串参数修复钉）；
4. 类型化上传的守卫与容错。
"""

from __future__ import annotations

import os

import numpy as np
import pytest

from src.features.color.core.composition_uniforms import (  # noqa: E402
    CompositionUniformManifest,
    UniformDecl,
    build_composition_uniform_manifest,
    composition_uniform_values,
    params_to_rgb,
    upload_composition_uniforms,
)
from src.features.color.core.shader_compiler import (  # noqa: E402
    plugin_binding_from_instance,
)
from src.features.shared.effects.effect_spec import (  # noqa: E402
    EffectSpec,
    PluginBinding,
    ShaderKind,
)

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _scan_plugins():
    from src.features.color.core.plugin_manager import get_plugin_manager
    get_plugin_manager().scan_plugins(os.path.join(_REPO, "plugins"))


# ── 1. 声明生成与手写基线逐行一致 ───────────────────────────────────────

_HEADER_BASELINE = [
    "uniform sampler2D u_input_tex;",
    "uniform sampler2D u_matte_tex;",
]

_BODY_BASELINE_MINIMAL = [
    "uniform float u_opacity;",
    "uniform int u_blend_mode;",
    "uniform vec3 u_lift;",
    "uniform vec3 u_gamma;",
    "uniform vec3 u_gain;",
    "uniform float u_exposure;",
    "uniform float u_temp;",
    "uniform float u_tint;",
    "uniform float u_contrast;",
    "uniform float u_saturation;",
    "uniform float u_vibrance;",
    "uniform float u_shadow;",
    "uniform float u_highlights;",
    "uniform float u_mid_detail;",
    "uniform float u_sharpness;",
    "uniform float u_hue_shift;",
    "uniform int u_fx_mode;",
    "uniform float u_fx_sharpen;",
    "uniform float u_fx_texture;",
    "uniform float u_fx_halo;",
    "uniform float u_fx_radius;",
    "uniform int u_fx_iterations;",
    "uniform bool u_fx_enabled;",
    "uniform bool u_blur_enabled;",
    "uniform float u_aspect_ratio;",
    "uniform int u_blur_mode;",
    "uniform float u_blur_intensity;",
    "uniform float u_blur_detail;",
    "uniform float u_blur_radius;",
    "uniform float u_blur_bokeh;",
    "uniform float u_blur_angle;",
    "uniform bool u_sc_enabled;",
    "uniform float u_sc_high;",
    "uniform float u_sc_white_point;",
    "uniform float u_sc_shoulder_strength;",
    "uniform float u_sc_low;",
    "uniform float u_sc_black_point;",
    "uniform float u_sc_toe_strength;",
    "uniform float u_sc_exposure;",
    "uniform float u_sc_contrast;",
    "uniform vec3 u_red_p;",
    "uniform vec3 u_orange_p;",
    "uniform vec3 u_yellow_p;",
    "uniform vec3 u_green_p;",
    "uniform vec3 u_aqua_p;",
    "uniform vec3 u_blue_p;",
    "uniform vec3 u_purple_p;",
    "uniform vec3 u_magenta_p;",
    # 色调区间饱和度（追加在基础段末尾，既有偏移不变）
    "uniform float u_lift_saturation;",
    "uniform float u_gamma_saturation;",
    "uniform float u_gain_saturation;",
]


def test_header_declarations_match_baseline():
    m = build_composition_uniform_manifest()
    assert m.declarations("header") == _HEADER_BASELINE


def test_body_declarations_match_baseline():
    m = build_composition_uniform_manifest()
    assert m.declarations("body") == _BODY_BASELINE_MINIMAL


def test_optional_and_grain_declarations():
    m = build_composition_uniform_manifest(
        has_curves=True, has_hsl_curve=True, has_warper=True,
        has_lut3d=True, grain_enabled=True)
    body = m.declarations("body")
    # 可选 sampler 段在 mixer 之后、grain 段在其后（插件除外）
    assert body[body.index("uniform float u_gain_saturation;") + 1:] == [
        "uniform sampler2D u_curves_tex;",
        "uniform sampler2D u_hsl_curve_tex;",
        "uniform sampler2D u_warper_tex;",
        "uniform float u_warper_grid_size;",
        "uniform sampler3D u_lut_tex;",
        "uniform float u_lut_intensity;",
        "uniform float u_grain_intensity;",
        "uniform float u_grain_size;",
        "uniform bool u_grain_monochrome;",
        "uniform float u_grain_shadows;",
        "uniform float u_grain_midtones;",
        "uniform float u_grain_highlights;",
        "uniform float u_grain_seed;",
        "uniform float u_grain_aspect_ratio;",
    ]


def test_plugin_declarations_from_binding():
    _scan_plugins()
    b = plugin_binding_from_instance({
        "plugin_id": "HighPass_Retouch", "enabled": True,
        "params": {"mix_amount": 0.85},
    })
    m = build_composition_uniform_manifest(bindings=[b])
    body = m.declarations("body")
    assert "uniform float u_P0_smooth_radius;" in body
    assert "uniform float u_P0_detail_preserve;" in body
    assert "uniform float u_P0_mix_amount;" in body


# ── 2. MSL struct 布局（P6-06 对齐规则）─────────────────────────────────

def test_msl_layout_offsets_packed_4byte():
    """snippet-owned 物理在前；4 字节紧排无 padding。"""
    m = build_composition_uniform_manifest()
    assert m.decl("u_tex_size").offset == 0        # snippet vec2
    assert m.decl("u_tex_size").components == 2
    assert m.decl("u_visible_rect").offset == 8    # snippet vec4
    assert m.decl("u_opacity").offset == 24        # 首个 body 标量
    assert m.decl("u_blend_mode").offset == 28
    assert m.decl("u_lift").offset == 32           # vec3 = 12B
    assert m.decl("u_gain").offset == 32 + 24      # lift+gamma 之后
    # offset 无空洞：每个 value 递增 4×components
    values = m.value_decls()
    cursor = 0
    for d in values:
        assert d.offset == cursor, d.name
        cursor += 4 * d.components


def test_msl_struct_and_dtype_aligned():
    m = build_composition_uniform_manifest(grain_enabled=True)
    dtype = m.to_numpy_dtype()
    struct_text = m.to_msl_struct("CompositionParams")
    # bool → uint，vec → 逐分量展开
    assert "uint u_grain_monochrome;" in struct_text
    assert "float u_lift_x;" in struct_text
    assert "float u_tex_size_x;" in struct_text
    # dtype 与 struct 字段一一对应且 4 字节步进
    names = list(dtype.names)
    assert names[0] == "u_tex_size_x" and names[1] == "u_tex_size_y"
    assert dtype["u_tex_size_x"].itemsize == 4
    # 总体积 = 全部分量 × 4
    total = sum(4 * d.components for d in m.value_decls())
    assert dtype.itemsize == total


# ── 3. 值提取语义基线 ───────────────────────────────────────────────────

def _manual_binding(params=None, **kw) -> PluginBinding:
    from src.features.composite.nodes.base_node import ParamDef
    spec = EffectSpec(
        effect_type="plugin:manual", name="manual", category="plugin",
        param_defs=(
            ParamDef(name="amount", ptype="float", default=0.5),
            ParamDef(name="mode", ptype="option", labels=("a", "b")),
            ParamDef(name="tint", ptype="color"),
        ),
        shader_kind=ShaderKind.DCTL_PROCESS,
        shader_sources=("vec3 process(vec3 c, vec2 uv) { return c; }",),
        dispatch_key="plugin:manual",
    )
    kw.setdefault("values", params or {})
    return PluginBinding(spec=spec, **kw)


def test_extract_neutral_layer_defaults():
    layer = {"opacity": 1.0, "blend_mode": "Normal"}
    v = composition_uniform_values(layer, width=64, height=48)
    assert v["u_opacity"] == 1.0
    assert v["u_blend_mode"] == 0
    assert v["u_lift"] == (1.0, 1.0, 1.0)   # 车轮 (0,0,0) → gain=1
    assert v["u_gain"] == (1.0, 1.0, 1.0)
    assert v["u_exposure"] == 0.0
    assert v["u_saturation"] == 0.0
    # fx 空组总是写中性默认（跨 layer 串参数修复钉）
    assert v["u_fx_mode"] == 0 and v["u_fx_enabled"] is False
    assert v["u_fx_iterations"] == 1
    assert v["u_blur_enabled"] is False
    assert v["u_aspect_ratio"] == 64 / 48
    assert v["u_tex_size"] == (64.0, 48.0)
    assert v["u_sc_enabled"] is False
    assert v["u_sc_high"] == 1.0
    assert v["u_red_p"] == (0.0, 0.0, 0.0)


def test_extract_grading_values_and_none_tolerance():
    layer = {"grading_params": {
        "lift": (30, 0.5, 0.0), "exposure": None,  # None → or 0.0
        "contrast": 0.2,
    }}
    v = composition_uniform_values(layer, width=64, height=48)
    assert v["u_exposure"] == 0.0
    assert v["u_contrast"] == 0.2
    # 车轮 hue=30 sat=0.5 bright=0 → params_to_rgb 的已知行为
    expected = tuple(params_to_rgb(30, 0.5, 0.0))
    assert v["u_lift"] == expected


def test_params_to_rgb_identity_and_bright():
    assert params_to_rgb(0, 0, 0) == [1.0, 1.0, 1.0]
    # bright 2 倍 → 2**12 档映射（平方非线性，行为不变钉）
    out = params_to_rgb(0, 0, 0.5)
    assert out[0] == pytest.approx(2 ** 1.5)


def test_extract_mixer_truncates_and_lut_intensity():
    layer = {
        "mixer_params": {"red": (1.1, -0.05, 0.2, 9.9), "blue": "bad"},
        "lut_intensity": 0.7,
    }
    v = composition_uniform_values(layer, width=64, height=48)
    assert v["u_red_p"] == (1.1, -0.05, 0.2)
    assert v["u_blue_p"] == (0.0, 0.0, 0.0)
    assert v["u_lut_intensity"] == 0.7


def test_extract_grain_and_visible_rect():
    layer = {}
    v = composition_uniform_values(
        layer, grain_params={"enabled": True, "intensity": 0.3},
        width=64, height=48, aspect_ratio=1.5)
    assert v["u_grain_intensity"] == 0.3
    assert v["u_grain_size"] == 1.0
    assert v["u_grain_monochrome"] is True
    assert v["u_grain_shadows"] == 0.2      # 历史契约默认
    assert v["u_grain_aspect_ratio"] == 1.5
    assert v["u_visible_rect"] == (0.0, 0.0, 64.0, 48.0)  # 回退全图
    v2 = composition_uniform_values(
        layer, grain_params={}, width=64, height=48,
        visible_rect=(1.0, 2.0, 3.0, 4.0))
    assert v2["u_visible_rect"] == (1.0, 2.0, 3.0, 4.0)


def test_extract_tex_size_override():
    v = composition_uniform_values({}, width=64, height=48,
                                   tex_size=(1920.0, 1080.0))
    assert v["u_tex_size"] == (1920.0, 1080.0)


def test_extract_plugin_values_defaults_and_fallbacks():
    binding = _manual_binding(
        params={"amount": 0.9},
        defaults={"amount": 0.5, "mode": 1, "tint": None},
        index=2)
    v = composition_uniform_values({}, width=64, height=48,
                                   bindings=[binding])
    assert v["u_P2_amount"] == 0.9                       # 实例值优先
    # mode：实例无值 → 原始默认 int 索引 1（非 ParamDef 的 label）
    assert v["u_P2_mode"] == 1
    # tint：默认 None → color 中性 (1,1,1)
    assert v["u_P2_tint"] == (1.0, 1.0, 1.0)


def test_extract_plugin_none_value_fallbacks():
    binding = _manual_binding(
        params={"amount": None, "mode": None, "tint": None},
        defaults={})
    v = composition_uniform_values({}, width=64, height=48,
                                   bindings=[binding])
    assert v["u_P0_amount"] == 0.0
    assert v["u_P0_mode"] == 0
    assert v["u_P0_tint"] == (1.0, 1.0, 1.0)


def test_extract_skips_disabled_binding():
    binding = _manual_binding(params={"amount": 0.9}, enabled=False)
    v = composition_uniform_values({}, width=64, height=48,
                                   bindings=[binding])
    assert "u_P0_amount" not in v


# ── 4. 类型化上传 ───────────────────────────────────────────────────────

class _RecordingProgram:
    """moderngl Program 形状的记录器：prog[name].value = v 按名记录。"""

    def __init__(self, present=(), fail_names=()):
        self.present = set(present)
        self.fail_names = set(fail_names)
        self.set_values = {}

    def __contains__(self, name):
        return name in self.present

    def __getitem__(self, name):
        if name not in self.present:
            raise KeyError(name)
        return _UniformSlot(self.set_values, name, self.fail_names)


class _UniformSlot:
    def __init__(self, store, name, fail_names):
        self._store, self._name, self._fail = store, name, fail_names

    @property
    def value(self):
        return self._store[self._name]

    @value.setter
    def value(self, v):
        if self._name in self._fail:
            raise RuntimeError("boom")
        self._store[self._name] = v


def test_upload_skips_absent_uniforms():
    prog = _RecordingProgram(present=("u_opacity",))
    upload_composition_uniforms(prog, {
        "u_opacity": 0.5,
        "u_not_compiled": 1.0,  # 被 GLSL 编译器剔除/旗标未开 → 跳过
    })
    assert prog.set_values == {"u_opacity": 0.5}


def test_upload_swallows_assignment_errors():
    prog = _RecordingProgram(present=("u_opacity", "u_gamma"),
                             fail_names=("u_gamma",))
    upload_composition_uniforms(prog, {"u_opacity": 0.5, "u_gamma": (1.0, 1.0, 1.0)})
    assert prog.set_values == {"u_opacity": 0.5}  # 失败项记日志，不抛出、不中断渲染帧


def test_upload_none_prog_is_noop():
    upload_composition_uniforms(None, {"u_opacity": 0.5})  # 不抛出


def test_grain_only_pass_values_for_standalone_stage():
    """全局颗粒独立 pass（async 合成循环后的 grain-only 段）的 uniform 契约。

    空 layer + grain_params 必须产出全套 u_grain_*（async_renderer 的
    独立颗粒 pass 用 u_opacity=0 丢弃层调色、只保留输入+颗粒——见
    legacy 引擎 Step 3 的同款写法）。
    """
    gp = {"intensity": 0.4, "size": 2.0, "monochrome": False,
          "shadows": 0.1, "midtones": 0.9, "highlights": 0.3,
          "seed": 7.5}
    v = composition_uniform_values(
        {}, grain_params=gp, width=1920, height=1080,
        visible_rect=(0.0, 0.0, 1920.0, 1080.0))
    assert v["u_grain_intensity"] == 0.4
    assert v["u_grain_size"] == 2.0
    assert v["u_grain_monochrome"] is False
    assert v["u_grain_shadows"] == 0.1
    assert v["u_grain_midtones"] == 0.9
    assert v["u_grain_highlights"] == 0.3
    assert v["u_grain_seed"] == 7.5
    assert v["u_visible_rect"] == (0.0, 0.0, 1920.0, 1080.0)
    # 调用方随后覆写 u_opacity=0（mix 丢弃层调色），上传前默认为 1.0
    assert v["u_opacity"] == 1.0
