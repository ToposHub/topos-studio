"""MSL 组合 emitter（计划 P7-03，ADR 0004 决策 3）。

从同一份 manifest（``composition_uniforms.build_composition_uniform_manifest``
，P7-02）生成 Metal fragment 源：struct 布局 + 已迁移效果的翻译函数 +
composition main 骨架。与 GL emitter（``shader_compiler``）共享管线语义，
按效果逐个迁移；**未迁移效果不得静默跳过**——管线包含未翻译效果时
显式 ValueError（G4：无静默部分状态）。

状态机（台账 0004a）：``gl_only → dual → msl_verified``；``dual`` = 双
emitter 均可编译该效果（本模块收录即 dual），``msl_verified`` = G2/G3
通过且台账留证。

布局契约：
- fragment 入口 ``fs_composition``：``texture(0)=u_input_tex``、
  ``sampler(0)``（linear/clamp）、``buffer(0)=CompositionParams``。
- struct 由 ``CompositionUniformManifest.to_msl_struct`` 生成（P6-06
  对齐规则：4 字节紧排、bool→uint、vec 逐分量展开）；numpy 侧用
  ``to_numpy_dtype`` 逐字段填充。
- 输出 float4(color, tex.a)，与 GL composition main 尾句一致
  （blend/matte 语义按迁移顺序后续并入——对拍 harness 以
  Normal blend + 白 matte + opacity 1 的 GL 输出为基准，二者数学等价）。

线程边界：纯字符串源生成模块（manifest → MSL 源文本），无状态、
无线程、无 GL/Metal 资源；可在任意线程安全调用（产物交由调用方
的 Metal 编译/渲染基础设施消费）。
"""

from __future__ import annotations

from typing import Sequence

from src.features.color.core.composition_uniforms import (
    CompositionUniformManifest,
)
from src.features.color.core.shader_nodes_msl import (
    MSL_BLEND_MODES,
    MSL_BLUR_SHARPEN,
    MSL_CURVES,
    MSL_CURVES_HELPERS,
    MSL_FILM_GRAIN,
    MSL_GRADING,
    MSL_GRADING_HELPERS,
    MSL_HSV_FUNCS,
    MSL_LUT3D,
    MSL_MIXER,
    MSL_SOFT_CLIP,
    MSL_SOFT_CLIP_HELPERS,
    MSL_WARPER,
)

# 已完成翻译的效果（P7-03 SNIPPET 集合全部收口：grading → curves/hsl_curve
# → lut3d → mixer/warper → blur/sharpen → blend modes → soft_clip/film_grain。
# 插件 manifest MSL 待设计裁决（受限 GLSL→MSL 源改写器 vs 双源插件，
# 见台账 0004a）——裁决前含插件的管线在此显式 ValueError（G4），
# qualifier 归属 matte 生成侧（P6-02~04）。
MIGRATED_EFFECTS = ("grading", "curves", "hsl_curve", "lut3d",
                    "mixer", "warper", "blur_sharpen", "blend_modes",
                    "soft_clip", "film_grain")

_MSL_HEADER = """\
#include <metal_stdlib>
using namespace metal;
"""

# 顶点覆盖矩形（bench_render_ab_metal_gl 方法学：uv.y 翻转使输出
# 内存行序与 GL/输入图像一致——row 0 = 图像顶部）
_MSL_VERTEX = """
struct Raster {
    float4 pos [[position]];
    float2 uv;
};

vertex Raster vs_composition(uint vid [[vertex_id]]) {
    float2 positions[6] = {
        float2(-1.0, -1.0), float2(1.0, -1.0), float2(-1.0, 1.0),
        float2(-1.0, 1.0), float2(1.0, -1.0), float2(1.0, 1.0)
    };
    float2 p = positions[vid];
    Raster r;
    r.pos = float4(p, 0.0, 1.0);
    r.uv = p * 0.5 + 0.5;
    r.uv.y = 1.0 - r.uv.y;
    return r;
}
"""

_MSL_FRAGMENT_HEADER = """
fragment float4 fs_composition(
"""

_MSL_FRAGMENT_PROLOG = """
    ) {
    float4 tex_color = u_input_tex.sample(smp, r.uv);
    float3 color = tex_color.rgb;
"""

# blend_modes 迁移后启用：GL composition main 起始的 matte 采样
_MSL_FRAGMENT_MASK_SAMPLE = """\
    float mask = u_matte_tex.sample(smp, r.uv).r;
"""

# fragment 纹理槽位契约（索引恒定，与渲染器绑定侧约定一致；GL 侧槽位
# 由调用方运行期分配，无此约束——这是 Metal 管线的 ABI）：
#   texture(0)=u_input_tex  texture(1)=u_curves_tex
#   texture(2)=u_hsl_curve_tex  texture(3)=u_lut_tex(3D)
#   texture(4)=u_warper_tex
#   sampler(0)=linear/clamp  buffer(0)=CompositionParams
_EFFECT_TEXTURE_ARGS = {
    "curves": "texture2d<float> u_curves_tex [[texture(1)]]",
    "hsl_curve": "texture2d<float> u_hsl_curve_tex [[texture(2)]]",
    "lut3d": "texture3d<float> u_lut_tex [[texture(3)]]",
    "warper": "texture2d<float> u_warper_tex [[texture(4)]]",
    # blend_modes：matte 纹理（main 尾部 mix 权重所需）
    "blend_modes": "texture2d<float> u_matte_tex [[texture(5)]]",
}

# mixer 的 8 个 vec3 通道参数在 struct 中逐分量平铺，调用点重建 float3
_MIXER_FIELDS = ("u_red_p", "u_orange_p", "u_yellow_p", "u_green_p",
                 "u_aqua_p", "u_blue_p", "u_purple_p", "u_magenta_p")


def _vec3_expr(field: str) -> str:
    """生成调用点表达式：从 struct 平铺标量重建 float3。

    Args:
        field: struct 基字段名（如 "u_red_p"，对应 _x/_y/_z 三分量）。

    Returns:
        MSL 表达式字符串 ``float3(params.<field>_x, ..._y, ..._z)``。
    """
    return f"float3(params.{field}_x, params.{field}_y, params.{field}_z)"


# 管线位 → 调用语句。顺序 = composition main 的效果顺序（GL 侧：
# blur_sharpen(fx 关→直通) → grading → curves → hsl_curve → soft_clip
# → mixer → warper → lut3d）——blur_sharpen 恒为管线第一位。
_PIPELINE_CALLS = {
    "grading": "color = msl_apply_grading(color, r.uv, params, u_input_tex, smp);",
    "blur_sharpen": (
        "color = msl_apply_blur_sharpen(color, u_input_tex, smp, r.uv, "
        "params.u_fx_mode, params.u_fx_sharpen, params.u_fx_texture, "
        "params.u_fx_halo, params.u_fx_radius, params.u_fx_iterations, "
        "float2(params.u_tex_size_x, params.u_tex_size_y), "
        "params.u_fx_enabled != 0u, params.u_blur_enabled != 0u, "
        "params.u_blur_mode, params.u_blur_intensity, params.u_blur_detail, "
        "params.u_blur_radius, params.u_blur_bokeh, params.u_blur_angle);"),
    "curves": "color = msl_apply_curves(color, u_curves_tex, smp);",
    "hsl_curve": "color = msl_apply_hsl_curve(color, u_hsl_curve_tex, smp);",
    "lut3d": "color = msl_apply_3dlut(color, u_lut_tex, smp, params.u_lut_intensity);",
    "mixer": ("color = msl_apply_mixer(color, "
              + ", ".join(_vec3_expr(f) for f in _MIXER_FIELDS) + ");"),
    "warper": ("color = msl_apply_warper(color, u_warper_tex, smp, "
               "params.u_warper_grid_size);"),
    # soft_clip：GL main 同款启用守卫（if (u_sc_enabled)）
    "soft_clip": ("if (params.u_sc_enabled != 0u) { "
                  "color = msl_apply_soft_clip(color, params.u_sc_high, "
                  "params.u_sc_white_point, params.u_sc_shoulder_strength, "
                  "params.u_sc_low, params.u_sc_black_point, "
                  "params.u_sc_toe_strength, params.u_sc_exposure, "
                  "params.u_sc_contrast); }"),
    # blend_modes 无管线位调用——语义在 main 尾部 epilog（见
    # _MSL_FRAGMENT_EPILOG_BLEND）。
    # film_grain 位于 blend/mix 之后（GL main 末位），由 epilog 组装段
    # 特殊处理，不在此发射。
}


def emit_composition_msl(
    manifest: CompositionUniformManifest,
    active_effects: Sequence[str] = ("grading",),
) -> str:
    """生成完整 MSL 源（vs + fs + struct + 已迁移效果函数）。

    Args:
        manifest: composition uniform manifest（GL 侧同一实例语义）。
        active_effects: 本管线启用的效果位（按 composition 顺序）。

    Raises:
        ValueError: 管线包含未迁移效果（G4：显式失败，不静默降级）。
    """
    unknown = [e for e in active_effects if e not in MIGRATED_EFFECTS]
    if unknown:
        raise ValueError(
            f"效果 {unknown} 尚无 MSL 翻译（已迁移：{list(MIGRATED_EFFECTS)}）；"
            "按台账 0004a 逐效果迁移，禁止静默跳过（G4）"
        )

    parts: list[str] = []
    if "film_grain" in active_effects:
        # film grain 的 fract-hash 链对 FMA 收缩极端敏感（P6-06 实测：
        # contract(fast) 下几乎全部采样点不一致）。MSL 不支持
        # push/pop 作用域化（实测），语句拆分也被 AIR 跨语句重关联
        # 击穿（实测）——唯一手段是文件级 contract(off)。仅在 grain
        # 激活时注入，其余管线变体保持既有数值形态。
        parts.append("#pragma clang fp contract(off)")
    parts += [_MSL_HEADER, manifest.to_msl_struct("CompositionParams")]
    # MSL_CURVES 同时承载 apply_hsl_curves（RGB curves 与 HSL curves 同在
    # CURVES_SNIPPET）；mixer/warper/blend modes 也依赖 HSV 函数——任一
    # 启用即发射。
    if any(e in active_effects for e in ("grading", "curves", "hsl_curve",
                                         "mixer", "warper", "blend_modes")):
        parts.append(MSL_HSV_FUNCS)
    if "grading" in active_effects:
        parts += [MSL_GRADING_HELPERS, MSL_GRADING]
    if "curves" in active_effects or "hsl_curve" in active_effects:
        parts += [MSL_CURVES_HELPERS, MSL_CURVES]
    if "lut3d" in active_effects:
        parts.append(MSL_LUT3D)
    if "mixer" in active_effects:
        parts.append(MSL_MIXER)
    if "warper" in active_effects:
        parts.append(MSL_WARPER)
    if "blur_sharpen" in active_effects:
        parts.append(MSL_BLUR_SHARPEN)
    if "blend_modes" in active_effects:
        parts.append(MSL_BLEND_MODES)
    if "soft_clip" in active_effects:
        parts += [MSL_SOFT_CLIP_HELPERS, MSL_SOFT_CLIP]
    if "film_grain" in active_effects:
        parts.append(MSL_FILM_GRAIN)

    # fragment 签名按启用效果追加纹理参数（槽位见 _EFFECT_TEXTURE_ARGS）
    args = [
        "Raster r [[stage_in]]",
        "texture2d<float> u_input_tex [[texture(0)]]",
        "sampler smp [[sampler(0)]]",
        "constant CompositionParams& params [[buffer(0)]]",
    ]
    args += [_EFFECT_TEXTURE_ARGS[e] for e in active_effects
             if e in _EFFECT_TEXTURE_ARGS]
    signature = ",\n                                   ".join(args)
    # blend_modes 语义在 main 尾部（matte 采样 + blend/mix），film_grain
    # 在 mix 之后（GL main 末位）；其余效果按 composition 顺序发射调用。
    body_lines = [f"    {_PIPELINE_CALLS[e]}" for e in active_effects
                  if e in _PIPELINE_CALLS]
    if "blend_modes" in active_effects:
        body_lines.insert(0, _MSL_FRAGMENT_MASK_SAMPLE)
        body_lines.append(
            "    float3 blended = msl_apply_blend_mode(tex_color.rgb, "
            "color, params.u_blend_mode);")
        body_lines.append(
            "    color = mix(tex_color.rgb, blended, "
            "mask * params.u_opacity);")
    if "film_grain" in active_effects:
        body_lines.append(
            "    color = msl_apply_film_grain(color, r.uv, "
            "params.u_grain_intensity, params.u_grain_size, "
            "params.u_grain_monochrome != 0u, params.u_grain_shadows, "
            "params.u_grain_midtones, params.u_grain_highlights, "
            "params.u_grain_seed, params.u_grain_aspect_ratio, "
            "float2(params.u_tex_size_x, params.u_tex_size_y), "
            "float4(params.u_visible_rect_x, params.u_visible_rect_y, "
            "params.u_visible_rect_z, params.u_visible_rect_w));")
    body_lines.append("    return float4(color, tex_color.a);")
    parts += [_MSL_VERTEX,
              _MSL_FRAGMENT_HEADER,
              signature,
              _MSL_FRAGMENT_PROLOG,
              *body_lines,
              "}"]
    return "\n".join(parts)
