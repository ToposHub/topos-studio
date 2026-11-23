"""受限 GLSL → MSL 转译器（``tglsl_msl_transpiler``）单元测试。

纯 Python，不触达 GPU。钉住四件事：
- 40 个仓库插件的结论：哪些可转译（及其用到的特性），哪些因哪种结构被拒；
- 每条诊断都带插件 ID、版本、阶段、后端、行列与源码片段；
- 生成的 MSL 把纹理、sampler、uniform、输出格式显式绑定，且没有 GLSL 残留；
- GLSL 与 MSL 语义不同之处全部被改写（stpq、向量三目、mod、radians、atan2、
  构造器多余分量……），以及注册表 helper 的整函数体匹配。
"""

from __future__ import annotations

import pathlib
import re

import pytest

from src.features.composite.nodes.base_node import ParamDef
from src.features.shared.effects import msl_effect_registry as registry
from src.features.shared.effects import tglsl_msl_transpiler as T
from src.features.shared.effects.effect_catalog import _dctl_param_to_pdef
from src.features.shared.effects.effect_spec import EffectSpec, PluginBinding, ShaderKind
from src.features.shared.effects.metal_abi import (
    COORD_GL_ROW0_IS_V0,
    COORD_TOP_DOWN,
    SAMPLER_LINEAR_REPEAT,
    build_params_layout,
)
from src.features.shared.effects.tglsl_front import (
    BACKEND_METAL,
    TranspileStage,
    TglslMetalError,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PLUGINS_DIR = REPO_ROOT / "plugins"
ENTRY = "vec3 process(vec3 color, vec2 uv) {\n"


def _real(plugin_id: str):
    """用生产的 PluginManager 解析一个仓库插件 → (解析产物, ParamDef 元组)。"""
    from src.features.color.core.plugin_manager import PluginManager

    data = PluginManager.__new__(PluginManager)._parse_plugin_file(
        str(PLUGINS_DIR / f"{plugin_id}.tglsl"), plugin_id)
    param_defs = tuple(pd for pd in (_dctl_param_to_pdef(p) for p in data["params"]) if pd is not None)
    return data, param_defs


def _transpile_real(plugin_id: str, **options):
    data, param_defs = _real(plugin_id)
    return T.transpile_tglsl(data["shader_code"], param_defs, plugin_id=plugin_id,
                             plugin_version=str(data.get("version", "")), **options)


def _msl(body: str, params=(("gain", "float"),), source: str = "", **options):
    param_defs = tuple(ParamDef(name, ptype, 0.0) for name, ptype in params)
    return T.transpile_tglsl(ENTRY + body + "\n}\n" if not source else source, param_defs,
                             plugin_id="synthetic", plugin_version="0.0.0", **options)


# ── A. 仓库插件的结论表 ─────────────────────────────────────────────────

_TRANSPILABLE = {
    "Bleach_Bypass": (),
    "CRT_Scanlines": ("texture_size",),
    "Channel_Mixer": (),
    "Cross_Process": (),
    "Day_Night_Converter": (),
    "Duotone_Toner": (),
    "Edge_Ink_Outline": ("sampling", "texture_size"),
    "Fake_Infrared": (),
    "Film_Grain": (),
    "Film_Halation": (),
    "Frequency_Split": ("sampling",),
    "Glitch_Blocks": ("sampling", "texture_size"),
    "Grad_Filter_Exposure": (),
    "HighPass_Retouch": ("host_helper:fast_blur", "sampling", "texture_size"),
    "Kaleido_Mirror": ("sampling", "texture_size"),
    "LIB1_Gradient_Map": (),
    "Lens_Distortion": ("sampling", "texture_size"),
    "Light_Leak_Corner": (),
    "Sky_Replace_Matte": (),
    "Three_Way_Tint": (),
    "Vibrance_Saturation": (),
    "Vignette_Shapes": (),
    "White_Balance_Advanced": (),
    "warm_tint": (),
}

#: 结构在子集外而被拒的插件 → 前端诊断代码。
_REJECTED = {
    "Anamorphic_Streak": "unsupported_construct:preprocessor",
    "Bokeh_Defocus": "unsupported_construct:array",
    "Color_Space_Transform": "unsupported_construct:matrix",
    "God_Rays": "unsupported_construct:loop",
    "Halation_Extended": "unsupported_construct:array",
    "Halftone_Print": "unsupported_construct:matrix",
    "LIB1_Median_Rank": "unsupported_construct:array",
    "Motion_Blur_Linear": "unsupported_construct:preprocessor",
    "Pixel_Blocks": "unsupported_construct:array",
    "Prism_Aberration": "unsupported_construct:matrix",
    "Radial_Blur_Pro": "unsupported_construct:preprocessor",
    "Skin_Even_Matte": "unsupported_construct:array",
    "Soft_Glow_Bloom": "unsupported_construct:array",
    "Tilt_Shift_Focus": "unsupported_construct:array",
    "Unsharp_Sharpen": "unsupported_construct:preprocessor",
    "bilateral_skin_smooth": "unsupported_construct:loop",
}


def test_repository_plugins_are_fully_partitioned():
    files = {p.stem for p in PLUGINS_DIR.glob("*.tglsl")}
    assert files == set(_TRANSPILABLE) | set(_REJECTED)
    assert not set(_TRANSPILABLE) & set(_REJECTED)


@pytest.mark.parametrize("plugin_id", sorted(_TRANSPILABLE))
def test_transpilable_repository_plugin(plugin_id):
    effect = _transpile_real(plugin_id)
    assert effect.features == _TRANSPILABLE[plugin_id]
    assert effect.process_kind == "vec3"
    assert effect.plugin_id == plugin_id
    assert effect.msl_fingerprint and effect.source_fingerprint
    # 生成的源没有 GLSL 类型名 / GLSL 纹理函数残留（`[[texture(n)]]` 是 Metal 的显式绑定属性）
    assert not re.search(r"\bvec[234]\b", effect.msl_source)
    assert not re.search(r"(?<!\[\[)(?<![\w.])texture\(", effect.msl_source)


@pytest.mark.parametrize("plugin_id", sorted(_REJECTED))
def test_rejected_repository_plugin_has_explicit_diagnostic(plugin_id):
    with pytest.raises(TglslMetalError) as info:
        _transpile_real(plugin_id)
    diagnostic = info.value.diagnostic
    assert diagnostic.code == _REJECTED[plugin_id], diagnostic.format()
    assert diagnostic.plugin_id == plugin_id
    assert diagnostic.stage == TranspileStage.PARSE.value
    assert diagnostic.backend == BACKEND_METAL
    assert diagnostic.line > 0 and diagnostic.snippet
    text = diagnostic.format()
    for needle in (f"plugin={plugin_id}", "stage=parse", "backend=metal", diagnostic.code):
        assert needle in text


def test_rejected_plugin_keeps_its_opengl_identity():
    """被拒只影响 Metal：同一插件的 GLSL wrapper 产物（OpenGL 路径）不受影响。"""
    from src.features.shared.effects.dctl_wrapper import wrap_dctl_process

    data, _ = _real("God_Rays")
    wrapped = wrap_dctl_process(data["shader_code"], data["params"])
    assert "#version 330" in wrapped and "for (int i = 0; i < RAY_STEPS; i++)" in wrapped


def test_diagnostic_reports_version_from_header():
    with pytest.raises(TglslMetalError) as info:
        _transpile_real("Radial_Blur_Pro")
    assert info.value.diagnostic.plugin_version == "1.0"


# ── B. 生成的 MSL：显式绑定 ─────────────────────────────────────────────


def test_kernel_binds_textures_sampler_uniforms_and_output_explicitly():
    source = _transpile_real("Channel_Mixer").msl_source
    assert "#include <metal_stdlib>" in source and "using namespace metal;" in source
    assert "struct ToposFxParams {" in source
    assert "    float u_tex_size_x;\n    float u_tex_size_y;" in source
    assert "    float p_red_gain;" in source and "    float p_mixer_strength;" in source
    assert "texture2d<float> u_input_tex [[texture(0)]]," in source
    assert "texture2d<float, access::write> u_output_tex [[texture(1)]]," in source
    assert "constant ToposFxParams& params [[buffer(0)]]," in source
    assert "uint2 gid [[thread_position_in_grid]]) {" in source
    assert ("constexpr sampler u_sampler(coord::normalized, address::clamp_to_edge, "
            "filter::linear);") in source
    # 宿主保持 Alpha
    assert "u_output_tex.write(float4(result, src.a), gid);" in source
    assert "const float4 src = u_input_tex.read(gid);" in source
    # 插件参数经结构体字段读取，而不是全局 uniform
    assert "params.p_red_gain" in source and "params.p_mixer_strength" in source
    assert not re.search(r"^\s*uniform\b", source, re.MULTILINE)


def test_param_types_map_to_struct_fields():
    effect = _transpile_real("Duotone_Toner")
    struct = effect.params_struct
    assert "    float p_shadow_color_x;\n    float p_shadow_color_y;\n    float p_shadow_color_z;" in struct
    assert "    int p_luma_mode;" in struct                       # select ABI = int
    assert "    float p_tone_amount;" in struct
    assert "float3(params.p_shadow_color_x, params.p_shadow_color_y, params.p_shadow_color_z)" \
        in effect.msl_source
    checkbox = _transpile_real("Vibrance_Saturation").params_struct
    assert "    int p_lum_preserve;" in checkbox                  # checkbox ABI = int
    assert "params.p_lum_preserve == 1" in _transpile_real("Vibrance_Saturation").msl_source


def test_user_identifiers_are_namespaced_and_prototypes_come_first():
    source = _transpile_real("Three_Way_Tint").msl_source
    assert "constexpr constant float3 t_LUMA_WEIGHTS = " in source
    prototype = "inline float3 f_zone_gain(float3 t_tint_color, float t_weight, float t_strength);"
    assert prototype in source
    assert source.index(prototype) < source.index(prototype[:-1] + " {")
    # 形参 shadow 了同名的插件参数：函数体用的是形参，不是 params.p_*
    body = source[source.index(prototype[:-1] + " {"):]
    body = body[:body.index("\n}")]
    assert "t_tint_color * 2.0" in body and "params." not in body
    assert "kernel void topos_fx_kernel(" in source


@pytest.mark.parametrize(("coord", "uv_expr", "sample_expr"), [
    (COORD_GL_ROW0_IS_V0, "const float2 uv = px / u_tex_size;",
     "u_input_tex.sample(u_sampler, (t_uv + float2(0.1, 0.0)))"),
    (COORD_TOP_DOWN,
     "const float2 uv = float2(px.x / u_tex_size.x, (u_tex_size.y - px.y) / u_tex_size.y);",
     "u_input_tex.sample(u_sampler, float2(((t_uv + float2(0.1, 0.0))).x, "
     "1.0 - ((t_uv + float2(0.1, 0.0))).y))"),
])
def test_coordinate_convention_is_explicit(coord, uv_expr, sample_expr):
    """两种坐标约定都显式出现在生成的 kernel 里（uv 的定义 + 采样坐标换算）。"""
    source = _msl("return texture(u_input_tex, uv + vec2(0.1, 0.0)).rgb;",
                  coord_convention=coord).msl_source
    assert uv_expr in source
    assert sample_expr in source
    assert f"coord={coord}" in source          # 头注释记录本次使用的约定


def test_engine_size_and_aspect_are_lifted_explicitly():
    source = _msl("float a = u_aspect_ratio; vec2 s = u_tex_size; return color * a * s.x;").msl_source
    assert "float t_a = (u_tex_size.x / u_tex_size.y);" in source
    assert "float2 t_s = u_tex_size;" in source
    assert "f_process(float3 t_color, float2 t_uv, float2 u_tex_size) {" in source


def test_sampler_spec_is_emitted_and_changes_fingerprint():
    clamp = _transpile_real("Lens_Distortion")
    repeat = _transpile_real("Lens_Distortion", sampler_spec=SAMPLER_LINEAR_REPEAT)
    assert "address::clamp_to_edge" in clamp.msl_source
    assert "address::repeat" in repeat.msl_source
    assert clamp.msl_fingerprint != repeat.msl_fingerprint


# ── C. 语义改写规则 ─────────────────────────────────────────────────────
# 期望字符串逐条对照发射器的实际输出核对过语义（stpq→xyzw、向量三目→select、
# mod/radians/degrees 展开、atan2、rint/rsqrt、构造器多余分量截断……）。

@pytest.mark.parametrize(("body", "must_contain", "must_not_contain"), [
    ("vec4 v = vec4(color, 1.0); return v.bgra.xyz + v.stpq.xyz;",
     ("t_v.zyxw.xyz + t_v.xyzw.xyz",), (".stpq", ".bgra")),
    ("vec2 q = uv.st; return color * q.y;", ("float2 t_q = t_uv.xy;",), (".st",)),
    ("vec4 v = color.rgbr; return v.rgb;", ("float4 t_v = t_color.xyzx;",), ()),
    ("vec4 v = vec4(color, color.rg); return v.rgb;", ("float4(t_color, t_color.x)",), ()),
    ("vec4 v = vec4(color, uv); return v.rgb;", ("float4(t_color, (t_uv).x)",), ()),
    ("vec2 v = vec2(color.bgr); return vec3(v, 0.0);", ("float2 t_v = float2(t_color.zy);",), ()),  # 单向量实参截断
    ("vec3 r = lessThan(color, vec3(0.5)) ? color * 2.0 : color; return r;",
     ("float3 t_r = select(t_color, (t_color * 2.0), (t_color < float3(0.5)));",), ("? ",)),
    ("vec3 r = gain > 0.5 ? color : color * 0.5; return r;",
     ("((params.p_gain > 0.5) ? t_color : (t_color * 0.5))",), ()),
    ("return mod(color, 2.5);", ("(t_color - 2.5 * floor((t_color) / (2.5)))",), ("mod(",)),
    ("return color * radians(gain) + degrees(gain);",
     ("(t_color * (params.p_gain * 0.017453292519943295))",
      "(params.p_gain * 57.29577951308232)"), ()),
    ("return color * atan(gain, 2.0) + atan(gain);",
     ("atan2(params.p_gain, 2.0)", "atan(params.p_gain)"), ()),
    ("return color * round(gain) + roundEven(gain) + inversesqrt(gain);",
     ("rint(params.p_gain)", "rsqrt(params.p_gain)"), ("round(", "inversesqrt")),
    ("return vec3(1, 0, 0) * gain + vec2(1, 0).x;", ("float3(1, 0, 0)", "float2(1, 0).x"), ()),
    ("return mix(color, color * 2.0, lessThan(color, vec3(0.25)));",
     ("select(t_color, (t_color * 2.0), (t_color < float3(0.25)))",), ()),
    ("bvec3 b = equal(color, vec3(0.0)); return any(b) || all(not(b)) ? color : vec3(1.0);",
     ("bool3 t_b = (t_color == float3(0.0));",
      "((any(t_b) || all(bool3(!t_b.x, !t_b.y, !t_b.z))) ? t_color : float3(1.0))"), ()),
    ("return color * (u_tex_size.x / u_tex_size.y);", ("(u_tex_size.x / u_tex_size.y)",), ()),
    ("return texture(u_input_tex, uv + vec2(0.1, 0.0)).rgb;",
     ("u_input_tex.sample(u_sampler, (t_uv + float2(0.1, 0.0))).xyz",), ("texture(u_input_tex",)),
    ("vec2 ts = vec2(textureSize(u_input_tex, 0)); return color * ts.x;",
     ("float2 t_ts = float2(int2(u_input_tex.get_width(), u_input_tex.get_height()));",), ()),
    ("return rgb2hsv(color) + hsv2rgb(color);", ("msl_rgb2hsv(t_color)", "msl_hsv2rgb(t_color)",), ()),
    ("return color * fast_blur(u_input_tex, uv, 1.0, 4.0, u_tex_size);",
     ("msl_fast_blur(u_input_tex, u_sampler, t_uv, 1.0, 4.0, u_tex_size)",), ()),
])
def test_glsl_to_msl_rewrites(body, must_contain, must_not_contain):
    effect = _msl(body)
    for text in must_contain:
        assert text in effect.msl_source, (text, effect.msl_source)
    for text in must_not_contain:
        assert text not in effect.msl_source, text


def test_invalid_glsl_is_rejected_rather_than_translated():
    """非法 GLSL（向量 swizzle 越界、多出完整实参）显式拒绝，不做猜测性翻译。"""
    with pytest.raises(TglslMetalError) as info:
        _msl("return color.bgra;")
    assert info.value.diagnostic.code == "swizzle_out_of_range"
    with pytest.raises(TglslMetalError) as info:
        _msl("vec3 v = vec3(color, 1.0); return v;")
    assert info.value.diagnostic.code == "constructor_excess_arguments"


def test_vector_condition_cannot_use_ternary_in_msl():
    source = _msl("vec3 r = lessThan(color, vec3(0.5)) ? color * 2.0 : color; return r;").msl_source
    assert "select(" in source and "?" not in source.split("inline float3 f_process")[1]


def test_logical_xor_is_rewritten_to_not_equal():
    body = ("return (gain > 0.5 ^^ gain < 0.25) ? color : color * 0.5;")
    source = _msl(body).msl_source
    assert "(params.p_gain > 0.5) != (params.p_gain < 0.25)" in source and "^^" not in source


def test_sampling_other_than_engine_texture_is_refused():
    with pytest.raises(TglslMetalError) as info:
        T.transpile_tglsl("uniform sampler2D u_other;\n" + ENTRY + "return texture(u_other, uv).rgb;\n}\n",
                          (), plugin_id="p")
    assert info.value.diagnostic.code == "unsupported_construct:uniform"
    with pytest.raises(TglslMetalError) as info:
        T.transpile_tglsl(ENTRY + "return textureLod(u_input_tex, uv, 1.0).rgb;\n}\n", (), plugin_id="p")
    assert info.value.diagnostic.code == "unknown_function"


# ── D. 作用域与引擎量提升 ───────────────────────────────────────────────


def test_helper_using_plugin_param_gets_explicit_params_argument():
    source = "float scaled(float x) { return x * gain; }\n" \
             "vec3 process(vec3 color, vec2 uv) { return vec3(scaled(color.r)); }\n"
    msl = _msl("", params=(("gain", "float"),), source=source).msl_source
    assert "inline float f_scaled(float t_x, constant ToposFxParams& params);" in msl
    assert "return (t_x * params.p_gain);" in msl
    # process 自身不直接引用参数，但调用方必须把 params 传下去
    assert "inline float3 f_process(float3 t_color, float2 t_uv, constant ToposFxParams& params) {" in msl
    assert "return float3(f_scaled(t_color.x, params));" in msl


def test_engine_dependencies_propagate_through_call_graph():
    source = ("float helper2(vec2 p) { return texture(u_input_tex, p).r; }\n"
              "float helper1(vec2 p) { return helper2(p) * 2.0; }\n"
              "vec3 process(vec3 color, vec2 uv) { return color * helper1(uv); }\n")
    msl = _msl("", params=(), source=source).msl_source
    for name in ("f_helper1(float2 t_p", "f_process(float3 t_color, float2 t_uv"):
        assert f"inline float3 {name}" in msl or f"inline float {name}" in msl
    assert msl.count("texture2d<float> u_input_tex, sampler u_sampler") == 6   # 3 个原型 + 3 个定义
    assert "return (f_helper2(t_p, u_input_tex, u_sampler) * 2.0);" in msl
    assert "return (t_color * f_helper1(t_uv, u_input_tex, u_sampler));" in msl
    assert "params" not in msl.split("struct ToposFxParams")[1].split("kernel void")[0].replace(
        "ToposFxParams", "")


def test_function_without_engine_needs_gets_no_extra_arguments():
    source = ("float twice(float x) { return 2.0 * x; }\n"
              "vec3 process(vec3 color, vec2 uv) { return color * twice(0.5); }\n")
    msl = _msl("", params=(), source=source).msl_source
    assert "inline float f_twice(float t_x);" in msl
    assert "inline float3 f_process(float3 t_color, float2 t_uv) {" in msl
    assert "return (t_color * f_twice(0.5));" in msl


def test_function_parameter_shadows_plugin_parameter():
    source = ("float f(float gain) { return gain * 2.0; }\n"
              "vec3 process(vec3 color, vec2 uv) { return color * f(0.5); }\n")
    msl = _msl("", params=(("gain", "float"),), source=source).msl_source
    assert "inline float f_f(float t_gain);" in msl
    assert "return (t_gain * 2.0);" in msl
    assert "params.p_gain" not in msl.split("inline float f_f(float t_gain) {")[1].split("\n}")[0]


def test_rename_prefix_avoids_collisions_with_user_identifiers():
    source = ("vec3 process(vec3 color, vec2 uv) {\n"
              "    float t_x = 1.0;\n    float x = 2.0;\n"
              "    return color * t_x * x;\n}\n")
    msl = _msl("", params=(), source=source).msl_source
    assert "float tt_t_x = 1.0;" in msl and "float tt_x = 2.0;" in msl
    assert "return ((tt_color * tt_t_x) * tt_x);" in msl
    assert "inline float3 f_process(float3 tt_color, float2 tt_uv);" in msl


# ── E. 注册表 helper 的整函数体匹配 ────────────────────────────────────

def _helper(**overrides):
    base = dict(helper_id="test:h", version="1.0.0", msl_name="msl_test_h",
                msl_params=("float3 c", "float amount"), msl_body="return c * amount;",
                glsl_pattern="vec3 process(vec3 color, vec2 uv) { return color * amount; }\n",
                glsl_param_bindings=("amount",), param_binding_types=("float",))
    base.update(overrides)
    return T.MslHelper(**base)




def test_plan_6b_sample_is_served_by_the_registered_helper():
    effect = _msl("", params=(("exposure", "float"),), source=T.registry_pattern_source(),
                  registry_helpers=(registry.EXPOSURE_HELPER,))
    assert effect.matched_helper == "builtin:exposure"
    assert effect.helper_ids == ("builtin:exposure",)
    assert ("inline float3 migration_exposure(float3 linear_rgb, float exposure) {\n"
            "    return linear_rgb * exp2(exposure);\n}") in effect.msl_source
    assert ("inline float3 f_process(float3 t_color, float2 t_uv, constant ToposFxParams& params) {\n"
            "    return migration_exposure(t_color, params.p_exposure);\n}") in effect.msl_source


def test_helper_matches_after_parameter_rename():
    effect = _msl("", params=(("ev", "float"),),
                  source="vec3 process(vec3 color, vec2 uv) { return color * exp2(ev); }\n",
                  registry_helpers=(registry.EXPOSURE_HELPER,))
    assert effect.matched_helper == "builtin:exposure"
    assert "return migration_exposure(t_color, params.p_ev);" in effect.msl_source


def test_helper_does_not_match_a_different_expression():
    effect = _msl("", params=(("exposure", "float"),),
                  source="vec3 process(vec3 color, vec2 uv) { return color * exp2(exposure * 0.5); }\n",
                  registry_helpers=(registry.EXPOSURE_HELPER,))
    assert effect.matched_helper is None
    assert "return (t_color * exp2((params.p_exposure * 0.5)));" in effect.msl_source
    assert "migration_exposure" not in effect.msl_source


def test_helper_binding_requires_matching_parameter_type():
    """结构相同但被绑定的标识符类型不同（vec3 对 float）→ 不命中，逐语句转译。"""
    tint = _helper(msl_params=("float3 c", "float3 tint"), msl_body="return c * tint;",
                   glsl_pattern="vec3 process(vec3 color, vec2 uv) { return color * tint; }\n",
                   glsl_param_bindings=("tint",), param_binding_types=("vec3",))
    source = "vec3 process(vec3 color, vec2 uv) { return color * gain; }\n"
    miss = _msl("", params=(("gain", "float"),), source=source, registry_helpers=(tint,))
    assert miss.matched_helper is None and "return (t_color * params.p_gain);" in miss.msl_source
    hit = _msl("", params=(("warmth", "color"),),
               source="vec3 process(vec3 color, vec2 uv) { return color * warmth; }\n",
               registry_helpers=(tint,))
    assert hit.matched_helper == "test:h" and "params.p_warmth_x" in hit.msl_source


def test_helper_requires_vec3_entry():
    pattern = registry.EXPOSURE_HELPER.glsl_pattern.replace("vec3 ", "vec4 ")
    effect = _msl("", params=(("exposure", "float"),), source=pattern,
                  registry_helpers=(registry.EXPOSURE_HELPER,))
    assert effect.process_kind == "vec4" and effect.matched_helper is None


def test_generic_translation_is_mathematically_the_helper():
    generic = _msl("", params=(("exposure", "float"),), source=T.registry_pattern_source()).msl_source
    assert "return (t_color * exp2(params.p_exposure));" in generic
    assert "migration_exposure" not in generic


def test_helper_match_does_not_depend_on_plugin_identity():
    """匹配只看结构：第三方插件用同样的函数体同样命中，没有任何插件 ID 参与。"""
    effect = T.transpile_tglsl(T.registry_pattern_source(), (ParamDef("exposure", "float", 0.0),),
                               plugin_id="third_party_ev", plugin_version="9.9")
    assert effect.matched_helper is None            # 未提供注册表 helper → 逐语句转译
    effect = T.transpile_tglsl(T.registry_pattern_source(), (ParamDef("exposure", "float", 0.0),),
                               plugin_id="third_party_ev", plugin_version="9.9",
                               registry_helpers=registry.get_msl_effect_registry().helpers())
    assert effect.matched_helper == "builtin:exposure"


def test_registry_helper_pattern_source_is_the_plan_sample():
    assert T.registry_pattern_source().strip() in registry.EXPOSURE_GLSL
    assert "@param slider exposure \"Exposure\" 0.0 -5.0 5.0" in registry.EXPOSURE_GLSL


# ── F. 确定性、选项与入口形态 ───────────────────────────────────────────


def test_transpilation_is_deterministic():
    first, second = _transpile_real("Grad_Filter_Exposure"), _transpile_real("Grad_Filter_Exposure")
    assert first.msl_source == second.msl_source
    assert first.msl_fingerprint == second.msl_fingerprint == T._sha256(first.msl_source)
    assert first.source_fingerprint == second.source_fingerprint


def test_coord_and_sampler_are_part_of_the_fingerprint():
    base = _transpile_real("Lens_Distortion")
    top_down = _transpile_real("Lens_Distortion", coord_convention=COORD_TOP_DOWN)
    assert base.msl_fingerprint != top_down.msl_fingerprint
    assert "coord=top_down" in top_down.msl_source


def test_with_kernel_name_replaces_only_the_kernel_name():
    effect = _transpile_real("warm_tint")
    renamed = effect.with_kernel_name("topos_fx_abc")
    assert renamed.count("topos_fx_abc") == 1 and "kernel void topos_fx_abc(" in renamed
    assert renamed.replace("topos_fx_abc", effect.kernel_name) == effect.msl_source


def test_vec4_process_entry_writes_result_directly():
    effect = _msl("", params=(), source="vec4 process(vec4 color, vec2 uv) { return color * 1.5; }\n")
    assert effect.process_kind == "vec4"
    assert "const float4 result = f_process(src, uv);" in effect.msl_source
    assert "u_output_tex.write(result, gid);" in effect.msl_source


def test_transpile_binding_accepts_only_plugin_binding():
    with pytest.raises(TypeError, match="PluginBinding"):
        T.transpile_binding({"plugin_id": "warm_tint", "params": {}})


def test_transpile_binding_from_catalog_spec():
    from src.features.shared.effects import get_effect_catalog

    spec = get_effect_catalog().get_spec("plugin:warm_tint")
    binding = PluginBinding(spec=spec, values={"strength": 0.8}, defaults={"strength": 0.35}, index=0)
    effect = T.transpile_binding(binding)
    assert effect.plugin_id == "warm_tint" and effect.plugin_version == "1.1"
    assert effect.source_fingerprint == spec.plugin_content_hash
    assert "params.p_strength" in effect.msl_source


# ── G. 校验与类型错误：显式诊断 ─────────────────────────────────────────
# 期望的处理代码逐条取自转译器的实际输出。

_ENTRY_OK = "vec3 process(vec3 color, vec2 uv) {\n"


def _reject(body, params=(("gain", "float"),), prefix=""):
    param_defs = tuple(ParamDef(name, ptype, 0.0) for name, ptype in params)
    with pytest.raises(TglslMetalError) as info:
        T.transpile_tglsl(prefix + _ENTRY_OK + body + "\n}\n", param_defs,
                          plugin_id="synthetic", plugin_version="0.0.0")
    return info.value.diagnostic


@pytest.mark.parametrize(("body", "params", "prefix", "stage", "code"), [
    ("return color * gain;", (("mix", "float"),), "", "validate", "reserved_param_name"),
    ("return color;", (), "vec3 rgb2hsv(vec3 c) { return c; }\n", "validate", "host_helper_redefined"),
    ("return color * other;", (("gain", "float"),), "", "typecheck", "undeclared_identifier"),
    ("return color + uv;", (("gain", "float"),), "", "typecheck", "vector_size_mismatch"),
    ("return uv;", (("gain", "float"),), "", "typecheck", "return_type"),
    ("if (gain) return color; return color;", (("gain", "float"),), "", "typecheck", "condition_type"),
    ("gain = 1.0; return color;", (("gain", "float"),), "", "typecheck", "assignment_target"),
    ("const float k = 1.0; k = 2.0; return color;", (("gain", "float"),), "", "typecheck", "assignment_target"),
    ("float a = 1.0; float a = 2.0; return color;", (("gain", "float"),), "", "typecheck", "redeclaration"),
    ("return color * dFdx(color.r);", (("gain", "float"),), "", "typecheck", "unknown_function"),
    ("return texture(gain, uv).rgb;", (("gain", "float"),), "", "typecheck", "uncontrolled_sampler"),
    ("return color * fast_blur(uv, 1.0, 1.0, u_tex_size);", (("gain", "float"),), "", "typecheck", "host_helper_signature"),
    ("return color;", (("exposure", "float"),), "float exposure(float x) { return x; }\n",
     "validate", "name_collision"),
    ("return color;", (("gain", "float"),), "float g = 1.0;\n", "parse", "unsupported_construct:global_mutable"),
    ("return color;", (("gain", "float"),), "const float K = vec2(1.0);\n", "typecheck", "const_type_mismatch"),
    ("return color * K;", (("gain", "float"),), "const float K = gain;\n", "typecheck", "non_constant_initializer"),
    ("return vec4(color, 1.0);", (("gain", "float"),), "", "typecheck", "return_type"),
    ("return color;", (("path", "file"),), "", "validate", "param_manifest_invalid"),
])
def test_validation_and_type_errors_carry_identity_and_stage(body, params, prefix, stage, code):
    diagnostic = _reject(body, params, prefix)
    assert diagnostic.code == code, diagnostic.format()
    assert diagnostic.stage == stage
    assert diagnostic.backend == BACKEND_METAL and diagnostic.plugin_id == "synthetic"
    assert diagnostic.plugin_version == "0.0.0"
    text = diagnostic.format()
    assert "plugin=synthetic" in text and f"stage={stage}" in text and "backend=metal" in text


@pytest.mark.parametrize(("source", "code"), [
    ("float f(float x) { return x; }\n", "entry_point"),                      # 没有 process
    ("vec3 process(vec3 color) { return color; }\n", "entry_signature"),       # 形参个数
    ("vec3 process(vec3 color, float s) { return color; }\n", "entry_signature"),
    ("vec3 process(vec2 uv, vec3 color) { return color; }\n", "entry_signature"),
    ("vec4 process(vec3 color, vec2 uv) { return color; }\n", "entry_signature"),   # 返回类型
    ("void main() {}\n" + _ENTRY_OK + "return color;\n}\n", "entry_point"),   # 多入口（main 不被允许）
    (_ENTRY_OK + "return color;\n}\n" + _ENTRY_OK + "return color;\n}\n", "duplicate_function"),
    ("vec3 process(vec3 gain, vec2 uv) { return gain; }\n", "reserved_param_name"),
])
def test_entry_point_contract(source, code):
    params = (("gain", "float"),) if "vec3 gain" in source else ()
    with pytest.raises(TglslMetalError) as info:
        T.transpile_tglsl(source, tuple(ParamDef(n, t, 0.0) for n, t in params),
                          plugin_id="entry", plugin_version="1")
    assert info.value.diagnostic.code == code, info.value.diagnostic.format()
    assert info.value.diagnostic.stage == "validate"


def test_empty_source_is_an_explicit_diagnostic():
    with pytest.raises(TglslMetalError) as info:
        T.transpile_tglsl("   \n", (), plugin_id="empty", plugin_version="1")
    assert info.value.diagnostic.code == "missing_plugin_source"


def test_unknown_coordinate_is_rejected():
    with pytest.raises(ValueError):
        _transpile_real("warm_tint", coord_convention="upside_down")


def test_layout_rejects_param_without_glsl_projection():
    with pytest.raises(ValueError, match="G5"):
        build_params_layout((ParamDef("path", "file"),))


# ── H. 宿主 helper：与 dctl_wrapper / 调色域 MSL 同源 ───────────────────


def _function_text(snippet: str, signature: str) -> str:
    start = snippet.index(signature)
    text = snippet[start:snippet.index("\n}\n", start) + 3]
    return "\n".join(line for line in text.splitlines()
                     if line.strip() and not line.strip().startswith("//"))


@pytest.mark.parametrize(("msl_name", "ours", "color_domain"), [
    ("float3 msl_rgb2hsv(", T.MSL_HOST_HSV, "MSL_HSV_FUNCS"),
    ("float3 msl_hsv2rgb(", T.MSL_HOST_HSV, "MSL_HSV_FUNCS"),
    ("float msl_blur_rand(", T.MSL_HOST_FAST_BLUR, "MSL_BLUR_SHARPEN"),
    ("float3 msl_fast_blur(", T.MSL_HOST_FAST_BLUR, "MSL_BLUR_SHARPEN"),
])
def test_host_helper_msl_matches_color_domain_translation(msl_name, ours, color_domain):
    """转译器引用的 MSL helper 必须与调色域已验证（P7-03）的翻译逐字一致。"""
    from src.features.color.core import shader_nodes_msl

    assert _function_text(ours, msl_name) == _function_text(getattr(shader_nodes_msl, color_domain), msl_name)


def test_plugin_may_define_its_own_blur_rand_like_on_the_gl_path():
    """GL 路径允许插件自定义 blur_rand（它不是宿主注入的 helper）；Metal 路径同策略。"""
    source = ("float blur_rand(vec2 co) { return fract(co.x * 0.5); }\n"
              "vec3 process(vec3 color, vec2 uv) { return color * blur_rand(uv); }\n")
    msl = _msl("", params=(), source=source).msl_source
    assert "inline float f_blur_rand(float2 t_co);" in msl
    assert "f_blur_rand(t_uv)" in msl and "msl_blur_rand" not in msl


def test_host_helper_catalog_matches_dctl_wrapper_injection():
    from src.features.shared.effects import dctl_wrapper

    assert set(T.HOST_HELPERS) == set(dctl_wrapper._HOST_HELPERS) == {"rgb2hsv", "hsv2rgb", "fast_blur"}
    assert "for (int i = 0; i < 12; i++)" in T.MSL_HOST_FAST_BLUR     # 循环在 helper 内，不在插件体内


def test_host_helpers_sharing_one_source_block_are_emitted_once():
    """rgb2hsv 与 hsv2rgb 同在 MSL_HOST_HSV：两个都用到时该段只发射一次（否则 MSL 重定义）。"""
    msl = _msl("return hsv2rgb(rgb2hsv(color));").msl_source
    assert msl.count("float3 msl_rgb2hsv(float3 c) {") == 1
    assert msl.count("float3 msl_hsv2rgb(float3 c) {") == 1
    both = _msl("return color * fast_blur(u_input_tex, uv, 1.0, 4.0, u_tex_size) + hsv2rgb(color);").msl_source
    assert both.count("float msl_blur_rand(float2 co) {") == 1 and both.count("float3 msl_hsv2rgb(float3 c) {") == 1


def test_only_referenced_host_helpers_are_emitted():
    plain = _transpile_real("Channel_Mixer").msl_source
    assert "msl_rgb2hsv" not in plain and "msl_fast_blur" not in plain
    hsv = _msl("return hsv2rgb(rgb2hsv(color));").msl_source
    assert "msl_rgb2hsv" in hsv and "msl_hsv2rgb" in hsv and "msl_fast_blur" not in hsv
    blur = _transpile_real("HighPass_Retouch").msl_source
    assert "msl_fast_blur" in blur and "msl_blur_rand" in blur and "msl_rgb2hsv" not in blur


# ── I. MSL 效果注册表 ───────────────────────────────────────────────────


def test_default_registry_has_exposure_helper_and_entry():
    reg = registry.get_msl_effect_registry()
    assert registry.get_msl_effect_registry() is reg
    assert [h.helper_id for h in reg.helpers()] == ["builtin:exposure"]
    entry = reg.entry("builtin:exposure")
    assert entry is not None and entry.helper_id == "builtin:exposure"
    assert [(p.name, p.ptype, p.default, p.min_val, p.max_val) for p in entry.param_defs] == [
        ("exposure", "float", 0.0, -5.0, 5.0)]
    assert entry.preserves_alpha and entry.deterministic and entry.hdr
    assert entry.verification is not None and entry.verification.status == registry.STATUS_VERIFIED
    assert entry.glsl_source == registry.EXPOSURE_GLSL
    assert entry.effect_id not in dict.fromkeys(e.effect_id for e in reg.entries() if e is not entry)


def test_exposure_helper_is_verbatim_plan_6b():
    helper = registry.EXPOSURE_HELPER
    assert helper.msl_name == "migration_exposure"
    assert helper.msl_params == ("float3 linear_rgb", "float exposure")
    assert helper.msl_body.strip() == "return linear_rgb * exp2(exposure);"
    assert helper.glsl_param_bindings == ("exposure",) and helper.param_binding_types == ("float",)


@pytest.mark.parametrize("overrides", [
    {"glsl_pattern": "vec3 process(vec3 color, vec2 uv) { return color * "},
    {"glsl_pattern": "float f(float x) { return x; }\n"},
    {"glsl_pattern": "vec3 process(vec3 amount, vec2 uv) { return amount * 2.0; }\n"},
    {"glsl_pattern": "vec3 process(vec3 color, vec2 uv) { return color * 2.0; }\n"},
    {"param_binding_types": ("sampler2D",)},
])
def test_registry_rejects_inconsistent_helpers(overrides):
    with pytest.raises(ValueError):
        registry.MslEffectRegistry().register_helper(_helper(**overrides))


def test_msl_helper_declares_consistent_arity():
    with pytest.raises(ValueError, match="形参数"):
        _helper(msl_params=("float3 c",))


def test_entry_requires_registered_helper():
    reg = registry.MslEffectRegistry()
    entry = registry._build_exposure_entry(None)
    with pytest.raises(ValueError, match="尚未登记"):
        reg.register_entry(entry)
    reg.register_helper(registry.EXPOSURE_HELPER)
    reg.register_entry(entry)
    assert reg.entry("builtin:exposure") is entry
    assert reg.entry("builtin:none") is None


def test_reset_registry_rebuilds_singleton():
    reg = registry.get_msl_effect_registry()
    try:
        registry.reset_msl_effect_registry()
        assert registry.get_msl_effect_registry() is not reg
    finally:
        registry.reset_msl_effect_registry()


def test_registry_serializes_to_plain_data():
    data = registry.get_msl_effect_registry().to_dict()
    assert data["helpers"][0]["msl_name"] == "migration_exposure"
    assert data["entries"][0]["effect_id"] == "builtin:exposure"
    verified = registry.get_msl_effect_registry().verified_plugins()
    assert len(verified) == 21 and "warm_tint" in verified
    assert set(verified) == {k for k, r in data["plugin_verification"].items() if r["status"] == "verified"}
