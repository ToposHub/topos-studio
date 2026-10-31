"""P7-02：composition 管线的结构化 uniform 绑定（ADR 0004 决策 2）。

单一参数 manifest（``name → {glsl_type, msl_type, offset, components}``）
驱动两侧代码生成：

- **GL 侧**：``CompositionUniformManifest.glsl_declarations()`` 生成
  uniform 声明（文本与重构前逐行一致——G1 位级门禁）；
  :func:`upload_composition_uniforms` 是唯一的类型化上传出口。
- **MSL 侧**（P7-03 消费）：offset 按 P6-06 已验证的对齐规则计算
  （平铺标量、4 字节紧排、bool→uint、vec→逐分量展开，
  见 ``src/shared/gpu/metal/kernels_color.py`` 的 FilmGrainParams）。

值提取（:func:`composition_uniform_values`）与上传（uploader）分离：
提取产出最终 Python 值（float/int/bool/tuple），上传按 ``name in prog``
守卫逐项赋值——uniform 被 GLSL 编译器剔除（参数未被 process 使用）时
静默跳过，其余失败记 debug 日志不中断渲染帧。

语义基线（与重构前 async_renderer._update_composition_uniforms 一致，
两处手写实现的差异按以下裁决统一）：

- grading 标量用 ``or 0.0`` 惯用法（容忍 None——比 image_engine 的
  ``get(k, 0.0)`` 更稳健，对正常 float 值二者等价）。
- fx/blur 组**总是**写入默认值。旧 async 实现在 ``effects_params`` 为空
  时整组跳过——多个 layer 共享同一缓存 program 时，前一个 layer 的
  fx 值会**残留**到下一个 layer（隐性串参数 bug）；统一后按中性默认
  覆盖，行为修正已在台账 0004a 登记。
- ``u_aspect_ratio`` 总是写入（旧 async 仅在 fx 非空时写——fx 关闭时
  该 uniform 无人消费，写入中性值语义不变）。
- ``u_tex_size`` 由调用方显式传入（async 用原图尺寸保证缩放一致性，
  engine 用渲染尺寸——两者语义不同，不在此裁决）。
- grain 默认值沿用历史 update_uniforms 契约（shadows/highlights 0.2、
  midtones 1.0、monochrome True）；engine 的 grain-only pass 刻意只设置
  grain 相关 uniform（u_opacity=0 使 grading 结果被 mix 丢弃），不经过
  本提取器。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.features.color.core.layer import Layer
from src.features.shared.effects.effect_spec import PluginBinding

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    import numpy as np

logger = logging.getLogger(__name__)


# glsl_type → (MSL 标量基类型, 分量数, numpy dtype 基名)。
# P6-06 对齐规则：无 vec 类型，struct 全部平铺标量、4 字节紧排；
# bool → uint（GLSL bool uniform 按 4 字节整型语义上传）。
_MSL_SCALAR: Dict[str, Tuple[str, int, str]] = {
    "float": ("float", 1, "f4"),
    "int": ("int", 1, "i4"),
    "bool": ("uint", 1, "u4"),
    "vec2": ("float", 2, "f4"),
    "vec3": ("float", 3, "f4"),
    "vec4": ("float", 4, "f4"),
}


@dataclass(frozen=True)
class UniformDecl:
    """manifest 单项。

    kind:
        ``"value"``     —— 数值 uniform（参与 MSL struct 布局）。
        ``"sampler2D"`` / ``"sampler3D"`` —— 纹理声明（不参与 struct，
        纹理槽位由渲染器在绑定时刻分配）。
    snippet_owned: GLSL 已由 SNIPPET 常量声明（如 u_tex_size 归
        GRADING_SNIPPET、u_visible_rect 归 FILM_GRAIN_SNIPPET）——GL
        emitter 不得重复发射，但上传与 MSL struct 布局仍由 manifest 承载。
    """

    name: str
    glsl_type: str
    kind: str = "value"
    snippet_owned: bool = False
    # GLSL 源内的发射区段：header（u_input_tex/u_matte_tex，在 SNIPPET 之前）
    # 或 body（基础调色及之后，在 SNIPPET 之后）——与重构前手写文本的
    # 物理顺序逐行一致。
    section: str = "body"
    # MSL 布局（value kind 专用，由 build_composition_uniform_manifest 计算）
    msl_type: str = "float"
    components: int = 1
    offset: int = -1  # -1 = 未布局（sampler）

    def glsl_declaration(self) -> str:
        """生成单条 GLSL uniform 声明行（sampler 以 ``kind`` 作类型名）。"""
        if self.kind == "value":
            return f"uniform {self.glsl_type} {self.name};"
        return f"uniform {self.kind} {self.name};"


class CompositionUniformManifest:
    """有序 UniformDecl 集合：GL 声明生成 + 类型化上传 + MSL struct 布局。"""

    def __init__(self, decls: Sequence[UniformDecl]) -> None:
        """按传入顺序固化声明清单并建立按名索引。"""
        self.decls: Tuple[UniformDecl, ...] = tuple(decls)
        self._by_name: Dict[str, UniformDecl] = {d.name: d for d in self.decls}

    def decl(self, name: str) -> Optional[UniformDecl]:
        """按名查声明，不存在时返回 None。"""
        return self._by_name.get(name)

    def declarations(self, section: str) -> List[str]:
        """按 manifest 顺序生成指定区段的 GLSL 声明行（snippet-owned 除外）。"""
        return [d.glsl_declaration() for d in self.decls
                if not d.snippet_owned and d.section == section]

    # ── MSL struct 布局（P7-03 emitter 直接消费）────────────────────────

    def value_decls(self) -> Tuple[UniformDecl, ...]:
        """参与 MSL struct 布局的 value uniform（含 snippet-owned），
        布局顺序 = GLSL 物理声明顺序：snippet 内声明在前。"""
        stable = [d for d in self.decls if d.kind == "value"
                  and d.snippet_owned]
        stable += [d for d in self.decls if d.kind == "value"
                   and not d.snippet_owned]
        return tuple(stable)

    def to_msl_struct(self, struct_name: str) -> str:
        """生成与 numpy dtype 逐字段对齐的 MSL struct（P6-06 惯例）。"""
        lines = [f"struct {struct_name} {{"]
        for d in self.value_decls():
            base, ncomp, _ = _MSL_SCALAR[d.glsl_type]
            if ncomp == 1:
                lines.append(f"    {base} {d.name};")
            else:
                for i in range(ncomp):
                    comp = "xyzw"[i]
                    lines.append(f"    {base} {d.name}_{comp};")
        lines.append("};")
        return "\n".join(lines)

    def to_numpy_dtype(self) -> "np.dtype":
        """生成与 MSL struct 逐字段对齐的 numpy dtype（4 字节紧排，P6-06）。"""
        import numpy as np
        fields = []
        for d in self.value_decls():
            base, ncomp, np_base = _MSL_SCALAR[d.glsl_type]
            if ncomp == 1:
                fields.append((d.name, f"<{np_base}"))
            else:
                for i in range(ncomp):
                    comp = "xyzw"[i]
                    fields.append((f"{d.name}_{comp}", f"<{np_base}"))
        return np.dtype(fields)


def _layout(decls: List[UniformDecl]) -> List[UniformDecl]:
    """为 value uniform 计算 MSL struct 偏移（4 字节紧排，P6-06 规则）。

    snippet-owned 声明物理在前（GLSL 中 SNIPPET 先于 Python 声明发射），
    布局顺序与其一致。
    """
    ordered = [d for d in decls if d.snippet_owned] + \
              [d for d in decls if not d.snippet_owned]
    offset = 0
    laid_out: List[UniformDecl] = []
    for d in ordered:
        if d.kind == "value":
            _, ncomp, _ = _MSL_SCALAR[d.glsl_type]
            d = UniformDecl(
                name=d.name, glsl_type=d.glsl_type, kind=d.kind,
                snippet_owned=d.snippet_owned, msl_type=_MSL_SCALAR[d.glsl_type][0],
                components=ncomp, offset=offset,
            )
            offset += 4 * ncomp
        laid_out.append(d)
    return laid_out


# ── composition 管线的声明清单（顺序敏感：与重构前手写文本逐行一致）────

_BASE_DECLS = [
    ("u_opacity", "float"),
    ("u_blend_mode", "int"),
    ("u_lift", "vec3"),
    ("u_gamma", "vec3"),
    ("u_gain", "vec3"),
    ("u_exposure", "float"),
    ("u_temp", "float"),
    ("u_tint", "float"),
    ("u_contrast", "float"),
    ("u_saturation", "float"),
    ("u_vibrance", "float"),
    ("u_shadow", "float"),
    ("u_highlights", "float"),
    ("u_mid_detail", "float"),
    ("u_sharpness", "float"),
    ("u_hue_shift", "float"),
    ("u_fx_mode", "int"),
    ("u_fx_sharpen", "float"),
    ("u_fx_texture", "float"),
    ("u_fx_halo", "float"),
    ("u_fx_radius", "float"),
    ("u_fx_iterations", "int"),
    ("u_fx_enabled", "bool"),
    ("u_blur_enabled", "bool"),
    ("u_aspect_ratio", "float"),
    ("u_blur_mode", "int"),
    ("u_blur_intensity", "float"),
    ("u_blur_detail", "float"),
    ("u_blur_radius", "float"),
    ("u_blur_bokeh", "float"),
    ("u_blur_angle", "float"),
    ("u_sc_enabled", "bool"),
    ("u_sc_high", "float"),
    ("u_sc_white_point", "float"),
    ("u_sc_shoulder_strength", "float"),
    ("u_sc_low", "float"),
    ("u_sc_black_point", "float"),
    ("u_sc_toe_strength", "float"),
    ("u_sc_exposure", "float"),
    ("u_sc_contrast", "float"),
    ("u_red_p", "vec3"),
    ("u_orange_p", "vec3"),
    ("u_yellow_p", "vec3"),
    ("u_green_p", "vec3"),
    ("u_aqua_p", "vec3"),
    ("u_blue_p", "vec3"),
    ("u_purple_p", "vec3"),
    ("u_magenta_p", "vec3"),
    # 色调区间饱和度（Lift/Gamma/Gain 各自的饱和度偏置，-100..100）。
    # 与移动端 mega.frag 的 tonal_saturation_adjust_g 同款数学。追加在
    # 清单末尾：既有 uniform 的布局偏移全部保持不变，仅向后扩展。
    ("u_lift_saturation", "float"),
    ("u_gamma_saturation", "float"),
    ("u_gain_saturation", "float"),
    # 曲线面板四参数柔化裁切（每通道 R/G/B 三元组；指数软膝，默认恒等）。
    # 无开关 uniform——编译期门控（has_curves_soft_clip 旗标进 program
    # 缓存键），非恒等才编译进管线。
    ("u_cs_low_clip", "vec3"),
    ("u_cs_low_soft", "vec3"),
    ("u_cs_high_clip", "vec3"),
    ("u_cs_high_soft", "vec3"),
]

_GRAIN_DECLS = [
    ("u_grain_intensity", "float"),
    ("u_grain_size", "float"),
    ("u_grain_monochrome", "bool"),
    ("u_grain_shadows", "float"),
    ("u_grain_midtones", "float"),
    ("u_grain_highlights", "float"),
    ("u_grain_seed", "float"),
    ("u_grain_aspect_ratio", "float"),
]

_MIXER_CHANNELS = ("red", "orange", "yellow", "green",
                   "aqua", "blue", "purple", "magenta")


def build_composition_uniform_manifest(
    *, has_curves: bool = False, has_hsl_curve: bool = False,
    has_warper: bool = False, has_lut3d: bool = False,
    grain_enabled: bool = False,
    bindings: Sequence[PluginBinding] = (),
) -> CompositionUniformManifest:
    """组装 composition 管线的完整 uniform manifest。

    声明顺序与重构前 ``build_composition_shader`` 的手写文本逐行一致：
    输入/遮罩 sampler → 基础调色 → fx/blur → soft clip → mixer →
    可选 sampler（curves/hsl/warper/lut）→ grain → 插件。
    ``u_tex_size``（GRADING_SNIPPET）与 ``u_visible_rect``
    （FILM_GRAIN_SNIPPET）为 snippet-owned，参与布局但不重复发射。
    """
    decls: List[UniformDecl] = [
        UniformDecl(name="u_tex_size", glsl_type="vec2", snippet_owned=True),
        UniformDecl(name="u_visible_rect", glsl_type="vec4", snippet_owned=True),
        UniformDecl(name="u_input_tex", glsl_type="sampler2D",
                    kind="sampler2D", section="header"),
        UniformDecl(name="u_matte_tex", glsl_type="sampler2D",
                    kind="sampler2D", section="header"),
    ]
    decls += [UniformDecl(name=n, glsl_type=t) for n, t in _BASE_DECLS]
    if has_curves:
        decls.append(UniformDecl(name="u_curves_tex", glsl_type="sampler2D",
                                 kind="sampler2D"))
    if has_hsl_curve:
        decls.append(UniformDecl(name="u_hsl_curve_tex", glsl_type="sampler2D",
                                 kind="sampler2D"))
    if has_warper:
        decls.append(UniformDecl(name="u_warper_tex", glsl_type="sampler2D",
                                 kind="sampler2D"))
        decls.append(UniformDecl(name="u_warper_grid_size", glsl_type="float"))
    if has_lut3d:
        decls.append(UniformDecl(name="u_lut_tex", glsl_type="sampler3D",
                                 kind="sampler3D"))
        decls.append(UniformDecl(name="u_lut_intensity", glsl_type="float"))
    if grain_enabled:
        decls += [UniformDecl(name=n, glsl_type=t) for n, t in _GRAIN_DECLS]
    for binding in bindings:
        if not binding.enabled:
            continue
        p_idx = binding.index
        for ub in binding.uniform_manifest(prefix=f"u_P{p_idx}_"):
            decls.append(UniformDecl(name=ub.uniform_name,
                                     glsl_type=ub.glsl_type))
    return CompositionUniformManifest(_layout(decls))


# ── 值提取（唯一实现；layer 兼容 dict 与 Layer 对象）────────────────────


def params_to_rgb(hue: float, sat: float, bright: float) -> List[float]:
    """Converts wheel Hue/Sat/Brightness to normalized RGB factors.

    自 ``ShaderPipeline._params_to_rgb`` 迁入（纯函数，无 self 依赖）；
    ShaderPipeline 保留同名委托以兼容既有调用方。算法与数值行为逐行
    保持不变（G1 约束：不允许重排任何浮点表达式）。
    """
    # --- 亮度映射算法优化 ---
    # 使用平方非线性映射：在中心区域保持细腻手感，在边缘区域快速扩大范围以实现全黑
    # 映射范围：输入 +/-1.0 对应 +/-6 档曝光 (2^-6 ≈ 0.015)
    stops = (bright ** 2) * 6.0
    if bright < 0: stops = -stops
    b_mult = math.pow(2.0, stops)

    if sat == 0: return [b_mult, b_mult, b_mult]

    h = hue / 60.0
    i = math.floor(h)
    f = h - i
    p = 1.0 * (1.0 - sat)
    q = 1.0 * (1.0 - sat * f)
    t = 1.0 * (1.0 - sat * (1.0 - f))

    ii = int(i) % 6
    if ii == 0: r, g, b = 1.0, t, p
    elif ii == 1: r, g, b = q, 1.0, p
    elif ii == 2: r, g, b = p, 1.0, t
    elif ii == 3: r, g, b = p, q, 1.0
    elif ii == 4: r, g, b = t, p, 1.0
    else: r, g, b = 1.0, p, q

    avg = max(1e-6, (r + g + b) / 3.0)
    return [(r/avg) * b_mult, (g/avg) * b_mult, (b/avg) * b_mult]


def _layer_get(layer: Any, key: str, default: Any = None) -> Any:
    """dict / Layer 对象双形态访问（与 shader_compiler 内部约定一致）。"""
    if isinstance(layer, dict):
        return layer.get(key, default)
    return getattr(layer, key, default)


def _get_v3(d: Any, k: str, default: Any = (0, 0, 0)) -> Any:
    """取长度≥3 的序列值，形态不符时回退 ``default``。"""
    v = d.get(k, default)
    return v if isinstance(v, (list, tuple)) and len(v) >= 3 else default


def _wheel_rgb(p: Any, key: str) -> Tuple[float, float, float]:
    """取色轮 ``key`` 的 (hue, sat, bright) 三元组并映射为 RGB 增益。"""
    v = _get_v3(p, key, (0, 0, 0))
    return tuple(params_to_rgb(*v))


def composition_uniform_values(
    layer: Layer,
    *,
    grain_params: Optional[dict] = None,
    width: int,
    height: int,
    aspect_ratio: Optional[float] = None,
    tex_size: Optional[Tuple[float, float]] = None,
    visible_rect: Optional[Tuple[float, float, float, float]] = None,
    bindings: Sequence[PluginBinding] = (),
) -> Dict[str, Any]:
    """从 layer 状态提取 composition shader 的全部 uniform 值。

    产出最终 Python 类型（float/int/bool/tuple），可直接交给
    :func:`upload_composition_uniforms`。uniform 名以
    ``build_composition_uniform_manifest`` 的清单为准。
    """
    values: Dict[str, Any] = {}
    values["u_opacity"] = float(_layer_get(layer, "opacity", 1.0))
    bm_name = _layer_get(layer, "blend_mode", "Normal")
    try:
        bm_idx = Layer.BLEND_MODES.index(bm_name)
    except (ValueError, AttributeError, TypeError):
        bm_idx = 0  # [R3-1] 未知混合模式回退 Normal
    values["u_blend_mode"] = int(bm_idx)

    p = _layer_get(layer, "grading_params")
    if not isinstance(p, dict):
        p = {}
    values["u_lift"] = _wheel_rgb(p, "lift")
    values["u_gamma"] = _wheel_rgb(p, "gamma")
    values["u_gain"] = _wheel_rgb(p, "gain")
    values["u_exposure"] = float(p.get("exposure") or 0.0)
    values["u_temp"] = float(p.get("temp") or 0.0)
    values["u_tint"] = float(p.get("tint") or 0.0)
    values["u_contrast"] = float(p.get("contrast") or 0.0)
    values["u_saturation"] = float(p.get("saturation") or 0.0)
    values["u_vibrance"] = float(p.get("vibrance") or 0.0)
    values["u_shadow"] = float(p.get("shadow") or 0.0)
    values["u_highlights"] = float(p.get("highlights") or 0.0)
    values["u_mid_detail"] = float(p.get("mid_detail") or 0.0)
    values["u_sharpness"] = float(p.get("sharpness") or 0.0)
    values["u_hue_shift"] = float(p.get("hue_shift") or 0.0)
    values["u_lift_saturation"] = float(p.get("lift_saturation") or 0.0)
    values["u_gamma_saturation"] = float(p.get("gamma_saturation") or 0.0)
    values["u_gain_saturation"] = float(p.get("gain_saturation") or 0.0)

    m = _layer_get(layer, "mixer_params")
    if not isinstance(m, dict):
        m = {}
    for color in _MIXER_CHANNELS:
        val = _get_v3(m, color, (0, 0, 0))
        values[f"u_{color}_p"] = tuple(val[:3])

    wp = _layer_get(layer, "warper_params")
    if wp is not None and hasattr(wp, "shape"):
        values["u_warper_grid_size"] = float(wp.shape[0])
    values["u_lut_intensity"] = float(_layer_get(layer, "lut_intensity", 1.0))

    # fx/blur：总是写中性默认（共享缓存 program 的残留 uniforms 会被
    # 覆盖——旧 async 实现跳过空 fx 组导致跨 layer 串参数，见模块文档）。
    fx = _layer_get(layer, "effects_params")
    if not isinstance(fx, dict):
        fx = {}
    values["u_fx_mode"] = int(fx.get("mode", 0))
    values["u_fx_sharpen"] = float(fx.get("sharpen", 0.0))
    values["u_fx_texture"] = float(fx.get("texture", 0.0))
    values["u_fx_halo"] = float(fx.get("halo", 0.0))
    values["u_fx_radius"] = float(fx.get("radius", 0.0))
    values["u_fx_iterations"] = int(fx.get("iterations", 1))
    values["u_fx_enabled"] = bool(fx.get("enabled", False))
    values["u_blur_enabled"] = bool(fx.get("blur_enabled", False))
    values["u_blur_mode"] = int(fx.get("blur_mode", 0))
    values["u_blur_intensity"] = float(fx.get("blur_intensity", 0.0))
    values["u_blur_detail"] = float(fx.get("blur_detail", 0.0))
    values["u_blur_radius"] = float(fx.get("blur_radius", 0.0))
    values["u_blur_bokeh"] = float(fx.get("blur_bokeh", 0.0))
    values["u_blur_angle"] = float(fx.get("blur_angle", 0.0))
    values["u_aspect_ratio"] = float(aspect_ratio
                                     if aspect_ratio is not None
                                     else width / height)
    values["u_tex_size"] = (tuple(float(v) for v in tex_size)
                            if tex_size else (float(width), float(height)))

    scp = _layer_get(layer, "soft_clip_params") or {}
    if not isinstance(scp, dict):
        scp = {}
    values["u_sc_enabled"] = bool(scp.get("enabled", False))
    values["u_sc_high"] = float(scp.get("high", 1.0))
    values["u_sc_white_point"] = float(scp.get("white_point", 1.1))
    values["u_sc_shoulder_strength"] = float(scp.get("shoulder_strength", 1.0))
    values["u_sc_low"] = float(scp.get("low", 0.0))
    values["u_sc_black_point"] = float(scp.get("black_point", -0.1))
    values["u_sc_toe_strength"] = float(scp.get("toe_strength", 1.0))
    values["u_sc_exposure"] = float(scp.get("exposure", 0.0))
    values["u_sc_contrast"] = float(scp.get("contrast", 0.0))

    # 曲线面板四参数柔化裁切：每通道读值组 vec3，缺键回退恒等默认
    # （共享缓存 program 的残留 uniforms 被覆盖，与 fx 组同一防串参口径）。
    csc = _layer_get(layer, "curves_soft_clip") or {}
    if not isinstance(csc, dict):
        csc = {}
    csc_channels = csc.get("channels")
    if not isinstance(csc_channels, dict):
        csc_channels = {}
    _CS_DEFAULTS = {
        "low_clip": (0.0, 0.0, 0.0),
        "low_soft": (0.0, 0.0, 0.0),
        "high_clip": (1.0, 1.0, 1.0),
        "high_soft": (0.0, 0.0, 0.0),
    }
    for key, defaults in _CS_DEFAULTS.items():
        triple = []
        for ch_idx, ch_name in enumerate("RGB"):
            ch_values = csc_channels.get(ch_name)
            value = None
            if isinstance(ch_values, dict):
                value = ch_values.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(float(value)):
                triple.append(float(value))
            else:
                triple.append(defaults[ch_idx])
        values[f"u_cs_{key}"] = tuple(triple)

    if grain_params is not None:
        gp = grain_params or {}
        values["u_grain_intensity"] = float(gp.get("intensity") or 0.0)
        values["u_grain_size"] = float(gp.get("size", 1.0))
        values["u_grain_monochrome"] = bool(gp.get("monochrome", True))
        values["u_grain_shadows"] = float(gp.get("shadows", 0.2))
        values["u_grain_midtones"] = float(gp.get("midtones", 1.0))
        values["u_grain_highlights"] = float(gp.get("highlights", 0.2))
        values["u_grain_seed"] = float(gp.get("seed", 0.0))
        values["u_grain_aspect_ratio"] = float(aspect_ratio
                                               if aspect_ratio is not None
                                               else width / height)
        if visible_rect and len(visible_rect) == 4:
            values["u_visible_rect"] = tuple(float(v) for v in visible_rect)
        else:
            # 回退：使用整个图像
            values["u_visible_rect"] = (0.0, 0.0, float(width), float(height))

    # 插件（P7-01 权威 binding）：value → 原始默认 → 类型中性值
    for binding in bindings:
        if not binding.enabled:
            continue
        prefix = f"u_P{binding.index}_"
        for ub in binding.uniform_manifest(prefix=prefix):
            current = binding.values.get(ub.param_name,
                                         binding.defaults.get(ub.param_name))
            if ub.glsl_type == "float":
                if current is None:
                    current = 0.0
                values[ub.uniform_name] = float(current)
            elif ub.glsl_type == "vec3":
                if current is None or not isinstance(current, (list, tuple)):
                    current = (1.0, 1.0, 1.0)
                values[ub.uniform_name] = tuple(current[:3])
            else:  # int / bool（DCTL checkbox/select ABI = int）
                if current is None:
                    current = 0
                values[ub.uniform_name] = int(current)
    return values


def upload_composition_uniforms(prog: Any, values: Mapping[str, Any]) -> None:
    """类型化上传：manifest 清单内的 uniform 逐项赋值。

    - ``name not in prog``：该 uniform 未编译进 shader（旗标未开或参数
      被 GLSL 编译器剔除）——静默跳过，属预期路径。
    - 其余赋值失败记 debug 日志但不抛出（渲染帧不得因单个 uniform 中断）。
    """
    if not prog:
        return
    for name, value in values.items():
        if name not in prog:
            continue
        try:
            prog[name].value = value
        except Exception:
            logger.debug(f"Failed to set composition uniform '{name}'",
                         exc_info=True)
