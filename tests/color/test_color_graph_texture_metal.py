"""阶段 4.2 —— Metal 纹理原生执行器定向测试（LUT/Primary/曲线/HSL/Mixer/Warper）。

分两层：

- 结构层（无 Metal 也运行）：参数 dtype 布局、kernel 源完整性、能力
  目录、参数打包器（hue 域换算、非有限值拒绝）。
- 真机层（无 Metal 设备整类跳过 = NOT RUN）：与 ``run_plan_cpu`` 生产
  oracle 逐像素对拍（G2 双容差：SDR ≤1/255 abs、HDR ≤2e-3 rel——CPU
  oracle 已与 GL parity，传递性即三后端一致）；CPU 无实现的 mixer /
  warper 用测试内 numpy 移植对拍；极值（负值 / >1 高光 / NaN）、LUT
  边界、节点顺序、多实例组合、池复用、逐 pass 取消、fail-closed。

输入量化注意（与 test_color_graph_metal 同规约）：kernel 从 RGBA16F
纹理读入，CPU 参考必须用**先过 fp16 量化的输入**。
"""

from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_texture_metal import (
    METAL_TEXTURE_CAPABILITY_CATALOG,
    ColorGraphMetalTextureExecutor,
    texture_layer_domain_errors,
)
from src.features.color.core.layer import build_curves_lut_from_data
from src.features.color.core.metal_texture_kernels import (
    METAL_TEX_GRAPH_OPS_MSL,
    TEX_EXTRA_PARAMS_DTYPE,
    TEX_QUALIFIER_PARAMS_DTYPE,
    make_tex_extra_params,
    make_tex_qualifier_params,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.project.color_graph import NodeType
from tests.color.fixtures.color_graph import (
    _neutral_layer,
    f01_hdr_ramp,
    f02_patches,
)
from tests.color.test_color_graph_migration import _stack_with_layers

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3


# ── 公共工具 ─────────────────────────────────────────────────────────────

def _quantized(source: np.ndarray) -> np.ndarray:
    return source.astype(np.float16).astype(np.float32)


def _assert_dual(metal: np.ndarray, ref: np.ndarray, label: str) -> None:
    metal32 = np.asarray(metal, dtype=np.float32)
    ref = np.asarray(ref, dtype=np.float32)
    finite = np.isfinite(ref) & np.isfinite(metal32)
    diff = np.abs(metal32[finite].astype(np.float64)
                  - ref[finite].astype(np.float64))
    mag = np.maximum(np.abs(ref[finite].astype(np.float64)), 1e-6)
    sdr = np.abs(ref[finite]) <= 1.0
    assert diff[sdr].max(initial=0.0) <= SDR_TOL, (
        f"{label}: SDR abs {diff[sdr].max(initial=0.0):.3e}")
    rel = (diff[~sdr] / mag[~sdr]).max(initial=0.0) if (~sdr).any() else 0.0
    assert rel <= HDR_REL, f"{label}: HDR rel {rel:.3e}"
    # NaN 位必须逐像素一致（NaN 不被吞成任意数，也不扩散）
    np.testing.assert_array_equal(np.isnan(metal32), np.isnan(ref))


def _rich_layer():
    layer = _neutral_layer("Rich")
    layer.grading_params.update({
        "exposure": 25.0, "temp": 12.0, "tint": -6.0, "contrast": 30.0,
        "saturation": -18.0, "vibrance": 25.0, "shadow": 12.0,
        "highlights": -20.0, "hue_shift": 40.0,
        "lift_saturation": 10.0, "gamma_saturation": -10.0,
        "gain_saturation": 5.0,
        "lift": (20.0, 0.3, 0.05), "gamma": (200.0, 0.2, -0.04),
        "gain": (120.0, 0.15, 0.08),
    })
    return layer


def _curves_layer():
    layer = _neutral_layer("Curve")
    layer.curves_data = {"M": [(0, 255), (64, 209), (191, 46), (255, 0)]}
    layer.curves_lut = build_curves_lut_from_data(layer.curves_data)
    layer.grading_params["exposure"] = 8.0
    return layer


def _hsl_layer():
    layer = _neutral_layer("HSL")
    # y=127.5 → 乘子 1.0 / HueHue 位移 0；此处压暗低端亮度饱和、抬色相偏移
    layer.hsl_curve_data = {
        "LumSat": [(0, 60), (128, 140), (255, 250)],
        "HueHue": [(0, 100), (128, 150), (255, 90)],
        "HueSat": [(0, 200), (255, 110)],
        "SatLum": [(0, 90), (255, 180)],
    }
    layer.grading_params["saturation"] = 10.0
    return layer


def _soft_clip_layer():
    layer = _neutral_layer("SC")
    layer.soft_clip_params.update({
        "enabled": True, "high": 0.9, "white_point": 1.4,
        "shoulder_strength": 0.8, "low": 0.03, "black_point": -0.05,
        "toe_strength": 0.9, "exposure": 0.05, "contrast": 0.1,
    })
    layer.curves_soft_clip = {
        "linked": True,
        "channels": {
            "R": {"low_clip": 0.01, "low_soft": 0.05,
                  "high_clip": 1.2, "high_soft": 0.1},
            "G": {"low_clip": 0.0, "low_soft": 0.0,
                  "high_clip": 1.1, "high_soft": 0.05},
            "B": {"low_clip": 0.02, "low_soft": 0.02,
                  "high_clip": 1.3, "high_soft": 0.15},
        },
    }
    return layer


def _graph_for(layers, *, graph=None):
    base = graph if graph is not None else migrate_legacy_stack(
        _stack_with_layers(layers))
    return base


def _provider_for(graph, frame: int | None = None):
    return graph_node_layer_provider(graph, frame=frame)


def _write_cube(path, size, fn) -> str:
    """生成 .cube（行序：b 慢 → r 快，与 lut_loader 的 reshape 同构）。"""
    axis = np.linspace(0.0, 1.0, size)
    lines = [f"TITLE tex", f"LUT_3D_SIZE {size}"]
    for b in axis:
        for g in axis:
            for r in axis:
                out = fn(r, g, b)
                lines.append(f"{out[0]:.6f} {out[1]:.6f} {out[2]:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


# ── mixer / warper 的 numpy 移植参考（CPU oracle 无此两域） ─────────────

def _rgb_to_hsv(rgb: np.ndarray) -> np.ndarray:
    from src.features.color.core.advanced_grade_cpu import _rgb_to_hsv as f
    return f(rgb)


def _hsv_to_rgb(hsv: np.ndarray) -> np.ndarray:
    from src.features.color.core.advanced_grade_cpu import _hsv_to_rgb as f
    return f(hsv)


_MIXER_CENTERS = {"red": 0.0, "orange": 30.0, "yellow": 60.0, "green": 120.0,
                  "aqua": 180.0, "blue": 240.0, "purple": 270.0,
                  "magenta": 300.0}


def mixer_reference(rgb: np.ndarray, mixer_params: dict) -> np.ndarray:
    """msl_apply_mixer 的 numpy 同式（GLSL 权威 MIXER_SNIPPET 的翻译）。"""
    sign = np.sign(rgb)
    hsv = _rgb_to_hsv(np.abs(rgb)).astype(np.float64)
    h = hsv[..., 0] * 360.0
    total_shift = np.zeros_like(h)
    for channel, center in _MIXER_CENTERS.items():
        p = tuple(float(v) for v in (
            mixer_params.get(channel) or (0.0, 0.0, 0.0)))[:3]
        dist = np.minimum(np.abs(h - center), 360.0 - np.abs(h - center))
        mask = np.clip(1.0 - dist / 30.0, 0.0, 1.0)
        if p[0] != 0.0:
            total_shift = total_shift + p[0] * 0.3 * mask
        if p[1] != 0.0:
            hsv[..., 1] = hsv[..., 1] * (1.0 + (p[1] / 100.0) * mask)
        if p[2] != 0.0:
            hsv[..., 2] = hsv[..., 2] * (1.0 + (p[2] / 100.0) * mask)
    if np.any(total_shift != 0.0):
        hsv[..., 0] = np.mod((h + total_shift) / 360.0, 1.0)
    return sign * _hsv_to_rgb(hsv.astype(np.float32)).astype(np.float64)


def _bilinear(tex: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """clamp_to_edge + linear 的 numpy 等价（Metal 采样器语义）。"""
    height, width = tex.shape
    x = np.clip(u.astype(np.float64) * width - 0.5, 0.0, width - 1.0)
    y = np.clip(v.astype(np.float64) * height - 0.5, 0.0, height - 1.0)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, width - 1)
    y1 = np.minimum(y0 + 1, height - 1)
    fx = x - x0
    fy = y - y0
    top = tex[y0, x0] * (1 - fx) + tex[y0, x1] * fx
    bottom = tex[y1, x0] * (1 - fx) + tex[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def warper_reference(rgb: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """msl_apply_warper 的 numpy 同式（legacy HSV 空间）。"""
    sign = np.sign(rgb)
    hsv = _rgb_to_hsv(np.abs(rgb)).astype(np.float64)
    sat = hsv[..., 1]
    delta_h = _bilinear(grid[..., 0], hsv[..., 0], sat)
    delta_s = _bilinear(grid[..., 1], hsv[..., 0], sat)
    fade = _smoothstep(0.02, 0.15, sat)
    out_hue = np.mod(hsv[..., 0] + delta_h * fade, 1.0)
    out_sat = np.clip(sat + delta_s * fade, 0.0, 1.0)
    result = sign * _hsv_to_rgb(np.stack(
        [out_hue, out_sat, hsv[..., 2]], axis=-1).astype(np.float32)
    ).astype(np.float64)
    return np.where((sat < 0.02)[..., None], rgb, result)


def hsl_curves_reference(rgb: np.ndarray, hsl_lut: np.ndarray) -> np.ndarray:
    """``msl_apply_hsl_curves`` 的 numpy 同式（GL CURVES_SNIPPET 权威）。

    注意：CPU oracle 的 ``apply_hsl_curves_image`` 是另一套近似（HSV 域
    直接缩放）——与 GL/MSL fragment 本就不同（既有跨后端分歧）；Metal
    纹理路径贴 GL 语义，参考必须用本移植。
    """
    hsl_lut = np.asarray(hsl_lut, dtype=np.float64).reshape(6, 256)
    centers = (np.arange(256, dtype=np.float64) + 0.5) / 256.0

    def curve(idx: int, x: np.ndarray) -> np.ndarray:
        return np.interp(x, centers, hsl_lut[idx])

    sign = np.sign(np.asarray(rgb)[..., :3])
    abs_color = np.abs(np.asarray(rgb, dtype=np.float64)[..., :3])
    hsv = _rgb_to_hsv(abs_color.astype(np.float32)).astype(np.float64)
    luma = np.abs(abs_color @ np.array([0.299, 0.587, 0.114]))
    luma_norm = np.clip(luma, 0.0, 1.0)
    hue_norm = hsv[..., 0]
    sat_norm = np.clip(hsv[..., 1], 0.0, 1.0)

    lum_sat = np.clip(curve(0, luma_norm), 0.0, 2.0)
    hue_hue = np.clip(curve(1, hue_norm), -0.5, 0.5)
    hue_sat = np.clip(curve(2, hue_norm), 0.0, 2.0)
    hue_lum = np.clip(curve(3, hue_norm), 0.0, 2.0)
    sat_sat = np.clip(curve(4, sat_norm), 0.0, 2.0)
    sat_lum = np.clip(curve(5, sat_norm), 0.0, 2.0)

    shifted_hue = np.mod(hsv[..., 0] + hue_hue, 1.0)
    final_sat = np.clip(lum_sat * hue_sat * sat_sat, 0.0, 2.0)
    gray = np.broadcast_to(luma[..., None], abs_color.shape)
    sat_adjusted = gray + (abs_color - gray) * final_sat[..., None]
    hsv2 = _rgb_to_hsv(sat_adjusted.astype(np.float32)).astype(np.float64)
    hsv2[..., 0] = shifted_hue
    final_lum = np.clip(1.0 * hue_lum * sat_lum, 0.0, 2.0)
    hsv2[..., 2] = np.maximum(hsv2[..., 2] * final_lum, 0.0)
    return sign * _hsv_to_rgb(hsv2.astype(np.float32)).astype(np.float64)


def _corrector_prefix_reference(image: np.ndarray, layer) -> np.ndarray:
    """grading → curves 前缀（无 epilogue）——mixer/warper 组合参考用。"""
    from src.features.color.core.color_graph_cpu import (
        apply_curves_cpu,
        apply_grading_uniforms_cpu,
    )
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
    )
    h, w = image.shape[:2]
    values = composition_uniform_values(
        dict(layer.to_dict(None)), width=w, height=h)
    out = apply_grading_uniforms_cpu(
        np.ascontiguousarray(image[..., :3]), values)
    lut = layer.curves_lut
    if lut is not None:
        out = apply_curves_cpu(out, lut)
    return out.astype(np.float64)


# ---------------------------------------------------------------------------
# 结构层（无需 Metal）
# ---------------------------------------------------------------------------

class TestStructural:
    def test_param_dtypes_layout(self) -> None:
        # 29 标量（6 uint + 3 int/float 槽 + 8 sc + 12 cs）+ mixer(24)
        assert TEX_EXTRA_PARAMS_DTYPE.itemsize == 212
        assert TEX_QUALIFIER_PARAMS_DTYPE.itemsize == 60
        for name in TEX_EXTRA_PARAMS_DTYPE.names:
            offset = (TEX_EXTRA_PARAMS_DTYPE.fields[name][1]
                      if name != "mixer"
                      else TEX_EXTRA_PARAMS_DTYPE.fields["mixer"][1])
            assert offset % 4 == 0, name

    def test_kernels_present_in_msl(self) -> None:
        for kernel in ("tg_copy", "tg_corrector", "tg_qualifier",
                       "tg_key_mix", "tg_key_complement",
                       "tg_key_passthrough", "tg_splitter", "tg_combiner",
                       "tg_layer_mix", "tg_parallel_mix"):
            assert f"kernel void {kernel}(" in METAL_TEX_GRAPH_OPS_MSL, kernel

    def test_corrector_segment_order_matches_gl_main(self) -> None:
        source = METAL_TEX_GRAPH_OPS_MSL
        body = source[source.index("kernel void tg_corrector("):]
        order = [body.index(f"graph_apply_grading("),
                 body.index("graph_apply_curves("),
                 body.index("msl_apply_hsl_curves("),
                 body.index("msl_apply_soft_clip("),
                 body.index("msl_apply_curves_soft_clip("),
                 body.index("msl_apply_mixer("),
                 body.index("msl_apply_warper("),
                 body.index("msl_apply_3dlut("),
                 body.index("msl_apply_blend_mode(")]
        assert order == sorted(order), "corrector 段序必须与 GL main 一致"

    def test_texture_catalog_key_ops_exact(self) -> None:
        assert METAL_TEXTURE_CAPABILITY_CATALOG["key_mixer"] == "exact"
        assert METAL_TEXTURE_CAPABILITY_CATALOG["alpha_output"] == "exact"
        assert METAL_TEXTURE_CAPABILITY_CATALOG["outside"] == "exact"
        assert METAL_TEXTURE_CAPABILITY_CATALOG["compound"] == "unsupported"

    def test_qualifier_params_hue_domain_shift(self) -> None:
        # CPU 域 center → shader 域 (c+0.5)%1（M06 对拍口径）
        params = make_tex_qualifier_params(
            {"enabled": True, "hue_center": 0.0})
        assert params["h_center"].item() == pytest.approx(0.5)
        params = make_tex_qualifier_params(
            {"enabled": True, "hue_center": 0.75})
        assert params["h_center"].item() == pytest.approx(0.25)

    def test_extra_params_pack_values(self) -> None:
        values = {
            "u_blend_mode": 3, "u_lut_intensity": 0.7,
            "u_red_p": (1.0, 2.0, 3.0),
            "u_sc_high": 0.9, "u_cs_high_clip": (1.2, 1.1, 1.3),
        }
        params = make_tex_extra_params(
            values, has_mixer=True, has_lut3d=True)
        assert params["blend_mode"].item() == 3
        assert params["lut_intensity"].item() == pytest.approx(0.7)
        assert params["has_mixer"].item() == 1
        assert params["has_lut3d"].item() == 1
        assert params["mixer"][0, :3].tolist() == [1.0, 2.0, 3.0]
        assert params["cs_high_clip_x"].item() == pytest.approx(1.2)

    def test_extra_params_reject_non_finite(self) -> None:
        with pytest.raises(ValueError, match="非有限"):
            make_tex_extra_params({"u_lut_intensity": float("nan")})

    def test_domain_errors_reject_unwired_and_allow_wired(self) -> None:
        # 已接线域放行
        layer = {"mixer_params": {"red": (10, 0, 0)},
                 "warper_params": np.zeros((4, 4, 2), np.float32),
                 "hsl_curve_data": {"HueSat": [(0, 128), (255, 128)]},
                 "active_lut_path": "/tmp/x.cube",
                 "blend_mode": "Overlay"}
        assert texture_layer_domain_errors(layer) == []
        # 未接线域显式拒绝
        assert any("插件" in e for e in texture_layer_domain_errors(
            {"plugins": [{"name": "x"}]}))
        assert any("邻域" in e for e in texture_layer_domain_errors(
            {"grading_params": {"sharpness": 5.0}}))
        assert any("后处理" in e for e in texture_layer_domain_errors(
            {"qualifier_params": {"enabled": True, "blur": 10.0}}))
        assert any("多空间" in e for e in texture_layer_domain_errors(
            {"warper_params": np.zeros((4, 4, 2), np.float32),
             "warper_space": 2}))
        assert any("fx/blur" in e for e in texture_layer_domain_errors(
            {"effects_params": {"blur_enabled": True,
                                "blur_intensity": 5.0}}))


# ---------------------------------------------------------------------------
# 真机层：G2 对拍（AMD 6900 XT / Apple Silicon 实测）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx
    ctx.finish()


def _run_graph(ctx, executor, graph, image, *, frame=None, request_id="t",
               cancel_check=None, size=None, with_cpu=True):
    import Metal

    height, width = image.shape[:2]
    snapshot = freeze_graph(graph, session_id="tex")
    plan = compile_graph(snapshot, size=size or (width, height))
    provider = graph_node_layer_provider(snapshot.graph, frame=frame)
    cpu = None
    if with_cpu:
        cpu = run_plan_cpu(plan, _quantized(image),
                           lambda p: provider(p.node_id))
    data = np.ascontiguousarray(image, dtype=np.float16)
    source = ctx.create_texture_from_numpy(
        data, Metal.MTLPixelFormatRGBA16Float, width, height)
    result = executor.execute_plan(
        plan, source, request_id=request_id, layer_param_provider=provider,
        cancel_check=cancel_check)
    return result, cpu


@requires_metal
class TestTextureGraphVsCpu:
    def test_neutral_identity(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_neutral_layer("N")]), src)
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, _quantized(src), "neutral")
        executor.release_revision(result.request_id)

    def test_full_basic_domain(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_rich_layer()]), src)
        assert result.published
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "rich")

    def test_curves(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_curves_layer()]), src)
        assert result.published
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "curves")

    def test_hsl_curves(self, metal_ctx):
        """HSL 曲线 vs MSL 同式移植（CPU oracle 是另一套近似，既有跨后端
        分歧——Metal 纹理路径贴 GL fragment 权威语义）。"""
        layer = _hsl_layer()
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, _cpu = _run_graph(
            metal_ctx, executor, _graph_for([layer]), src, with_cpu=False)
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        from src.features.color.core.layer import (
            build_hsl_curve_lut_from_data,
        )
        hsl_lut = build_hsl_curve_lut_from_data(dict(layer.hsl_curve_data))
        q = _quantized(src)
        prefix = _corrector_prefix_reference(q, layer).astype(np.float32)
        graded = hsl_curves_reference(prefix, hsl_lut).astype(np.float32)
        expected = q.copy()
        expected[..., :3] = graded
        _assert_dual(got, expected, "hsl")

    def test_soft_clip_and_curves_soft_clip(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_soft_clip_layer()]), src)
        assert result.published, result.diagnostic_message
        _assert_dual(executor.read_output_rgba16f(result), cpu.output, "sc")

    def test_lut3d_typical_and_boundary(self, metal_ctx, tmp_path):
        # 33³ 解析 LUT：通道交换 + 提亮（覆盖边界 0/1 与域内）
        size = 33

        def fn(r, g, b):
            return (0.9 * g + 0.05, 0.8 * b + 0.02, 0.7 * r)

        cube = _write_cube(tmp_path / "t.cube", size, fn)
        layer = _neutral_layer("LUT")
        layer.active_lut_path = cube
        layer.lut_intensity = 0.85
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(metal_ctx, executor, _graph_for([layer]),
                                 src)
        assert result.published, result.diagnostic_message
        _assert_dual(executor.read_output_rgba16f(result), cpu.output, "lut")

        # LUT 边界：0 / 1 / >1（HDR 比例重建）/ 负值（负偏移保留）定点
        probe = np.array([[[0.0, 0.0, 0.0, 1.0],
                           [1.0, 1.0, 1.0, 1.0],
                           [3.0, 2.0, 2.0, 1.0],
                           [-0.25, 0.1, 0.1, 0.75]]], dtype=np.float32)
        result, cpu = _run_graph(metal_ctx, executor, _graph_for([layer]),
                                 probe)
        assert result.published
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "lut-boundary")
        # 边界语义锚点：0 → LUT(0)；负值偏移按强度保留（不被 clamp 吞）
        assert got[0, 0, 0] == pytest.approx(
            cpu.output[0, 0, 0], abs=SDR_TOL)
        assert got[0, 3, 0] < 0.0, "LUT 负偏移必须保留"
        assert cpu.output[0, 3, 0] < 0.0
        # HDR 像素按 max 比例重建（>1 输出不塌回 SDR）
        assert got[0, 2, 0] > 2.0

    def test_serial_order_matters_both_orders(self, metal_ctx):
        """节点顺序：A→B 与 B→A 各自与 CPU 一致（非交换参数组）。"""
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        a = _rich_layer()
        b = _curves_layer()
        result_ab, cpu_ab = _run_graph(
            metal_ctx, executor, _graph_for([a, b]), src, request_id="ab")
        result_ba, cpu_ba = _run_graph(
            metal_ctx, executor, _graph_for([b, a]), src, request_id="ba")
        assert result_ab.published and result_ba.published
        got_ab = executor.read_output_rgba16f(result_ab)
        got_ba = executor.read_output_rgba16f(result_ba)
        _assert_dual(got_ab, cpu_ab.output, "ab")
        _assert_dual(got_ba, cpu_ba.output, "ba")
        # 非交换性自检（顺序敏感的参数组下两序输出必须不同）
        assert np.abs(got_ab - got_ba).max() > 1e-3

    def test_parallel_branch_graph(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(200))
        service = ColorGraphService(id_factory=lambda: f"pb{next(counter)}")
        grown = service.apply_intent(graph, "add_parallel_branch", {})
        assert grown.ok
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, cpu = _run_graph(metal_ctx, executor, grown.graph, src)
        assert result.published, result.diagnostic_message
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "parallel")

    def test_layer_branch_graph_blend_modes(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(200))
        service = ColorGraphService(id_factory=lambda: f"lb{next(counter)}")
        grown = service.apply_intent(graph, "add_layer_branch", {})
        assert grown.ok
        mixer = next(n for n in grown.graph.nodes
                     if n.node_type == "layer_mixer")
        grown = service.apply_intent(
            grown.graph, "set_node_option",
            {"node_id": mixer.node_id, "name": "blend_mode",
             "value": "Soft Light"})
        assert grown.ok, grown.diagnostics
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, cpu = _run_graph(metal_ctx, executor, grown.graph, src)
        assert result.published, result.diagnostic_message
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "layer-mix")

    def test_splitter_pair_graph(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        counter = iter(range(200))
        service = ColorGraphService(id_factory=lambda: f"sp{next(counter)}")
        grown = service.apply_intent(graph, "add_splitter_pair", {})
        assert grown.ok
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(metal_ctx, executor, grown.graph, src)
        assert result.published, result.diagnostic_message
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "splitter")

    def test_bypass_visible_false(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        cs = [n for n in graph.nodes if n.node_type == "corrector"]
        disabled = _replace(graph, nodes=tuple(
            _replace(n, enabled=False) if n.node_id == cs[0].node_id else n
            for n in graph.nodes))
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, cpu = _run_graph(metal_ctx, executor, disabled, src)
        assert result.published
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "bypass")

    def test_nan_pixel_propagates_without_spreading(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32).astype(np.float32)
        src[4, 5, 1] = np.nan
        src[10, 12, 0] = np.nan
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_rich_layer()]), src)
        assert result.published
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "nan")

    def test_non_divisible_threadgroup_size(self, metal_ctx):
        """33×17 尺寸（16×16 组不整除）与 CPU 一致。"""
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        rng = np.random.default_rng(7)
        src = rng.uniform(-0.2, 2.5, (17, 33, 4)).astype(np.float32)
        result, cpu = _run_graph(
            metal_ctx, executor, _graph_for([_rich_layer()]), src)
        assert result.published
        _assert_dual(executor.read_output_rgba16f(result), cpu.output,
                     "odd-size")


@requires_metal
class TestTextureGraphMixerWarper:
    """CPU oracle 无 mixer/warper——用测试内 numpy 同式参考对拍。"""

    def _warper_grid(self):
        n = 16
        u = np.linspace(0.0, 1.0, n, dtype=np.float32)
        uu, vv = np.meshgrid(u, u)
        delta_hue = 0.06 * np.sin(2 * np.pi * uu) * np.cos(np.pi * vv)
        delta_sat = 0.18 * (vv - 0.5)
        return np.ascontiguousarray(
            np.stack([delta_hue, delta_sat], axis=-1), dtype=np.float32)

    def _epilogue(self, src, graded, opacity=1.0):
        out = src.copy()
        out[..., :3] = src[..., :3] * (1.0 - opacity) + graded * opacity
        return out

    def test_mixer_vs_numpy_reference(self, metal_ctx):
        layer = _rich_layer()
        layer.mixer_params = {
            "red": (30.0, 10.0, -5.0), "yellow": (0.0, 20.0, 0.0),
            "blue": (-12.0, 0.0, 15.0), "green": (8.0, -10.0, 4.0),
            "aqua": (0.0, 12.0, -8.0),
        }
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, _ = _run_graph(metal_ctx, executor, _graph_for([layer]),
                               src, with_cpu=False)
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        q = _quantized(src)
        prefix = _corrector_prefix_reference(q, layer)
        mixed = mixer_reference(prefix.astype(np.float32), layer.mixer_params)
        expected = self._epilogue(q, mixed.astype(np.float32))
        _assert_dual(got, expected, "mixer")

    def test_warper_vs_numpy_reference(self, metal_ctx):
        layer = _neutral_layer("W")
        layer.grading_params["saturation"] = 12.0
        layer.warper_params = self._warper_grid()
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, _ = _run_graph(metal_ctx, executor, _graph_for([layer]),
                               src, with_cpu=False)
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        q = _quantized(src)
        prefix = _corrector_prefix_reference(q, layer)
        warped = warper_reference(prefix.astype(np.float32),
                                  layer.warper_params.astype(np.float64))
        expected = self._epilogue(q, warped.astype(np.float32))
        _assert_dual(got, expected, "warper")

    def test_grading_mixer_warper_multi_instance(self, metal_ctx):
        """多实例组合：两个 Corrector（各带 mixer/warper）串联——后者的
        mixer 输入是前者的输出（顺序 = GL main 段序）。"""
        first = _neutral_layer("M1")
        first.grading_params["exposure"] = 10.0
        first.mixer_params = {"red": (25.0, 8.0, 0.0),
                              "aqua": (0.0, -15.0, 10.0)}
        second = _neutral_layer("M2")
        second.grading_params["contrast"] = 15.0
        second.warper_params = self._warper_grid()
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f02_patches(32)
        result, _ = _run_graph(
            metal_ctx, executor, _graph_for([first, second]), src,
            with_cpu=False)
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        q = _quantized(src)
        stage1 = self._epilogue(
            q, mixer_reference(
                _corrector_prefix_reference(q, first).astype(np.float32),
                first.mixer_params).astype(np.float32))
        prefix2 = _corrector_prefix_reference(stage1.astype(np.float32),
                                              second)
        stage2 = self._epilogue(
            stage1, warper_reference(prefix2.astype(np.float32),
                                     second.warper_params.astype(
                                         np.float64)).astype(np.float32))
        _assert_dual(got, stage2, "multi-instance")

    def test_mixer_extreme_values(self, metal_ctx):
        """极值 mixer 参数（±100 饱和度 / ±50 色相）不炸不产生 NaN。"""
        layer = _neutral_layer("MX")
        layer.mixer_params = {channel: (45.0, -100.0, 100.0)
                              for channel in
                              ("red", "orange", "yellow", "green", "aqua",
                               "blue", "purple", "magenta")}
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        result, _ = _run_graph(metal_ctx, executor, _graph_for([layer]),
                               src, with_cpu=False)
        assert result.published
        got = executor.read_output_rgba16f(result)
        assert np.isfinite(got[np.isfinite(_quantized(src))]).all()


@requires_metal
class TestTextureGraphLifecycle:
    def test_pool_reuse_across_executions(self, metal_ctx):
        graph = _graph_for([_rich_layer()])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        src = f01_hdr_ramp(32)
        first, _ = _run_graph(metal_ctx, executor, graph, src,
                              request_id="p1")
        assert first.published
        executor.release_revision(first.request_id)
        allocated = executor.pool_allocated()
        second, _ = _run_graph(metal_ctx, executor, graph, src,
                               request_id="p2")
        assert second.published
        executor.release_revision(second.request_id)
        assert executor.pool_allocated() == allocated, (
            "第二次执行应完全复用池化纹理（无持续增长）")

    def test_cancel_mid_plan_no_publish(self, metal_ctx):
        graph = _graph_for([_rich_layer(), _curves_layer()])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        calls = {"n": 0}

        def cancel_after_first(_rid):
            calls["n"] += 1
            return calls["n"] > 1

        result, _ = _run_graph(metal_ctx, executor, graph,
                               f01_hdr_ramp(32), request_id="cx",
                               cancel_check=cancel_after_first)
        assert not result.published
        assert result.diagnostic_code == "cancelled"
        assert result.executed_passes == 0
        # 取消后执行器不被毒化：下一帧正常发布（scratch 全部退栈）
        followup, _ = _run_graph(metal_ctx, executor, graph,
                                 f01_hdr_ramp(32), request_id="cx2")
        assert followup.published

    def test_plugin_params_refused_fail_closed(self, metal_ctx):
        graph = migrate_legacy_stack(_stack_with_layers([_rich_layer()]))
        node = next(n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        payload = dict(node.legacy_payload or {})
        payload["plugins"] = [{"name": "nope", "plugin_id": "x",
                               "instance_id": "i"}]
        plugged = _replace(graph, nodes=tuple(
            _replace(n, legacy_payload=payload) if n.node_id == node.node_id
            else n for n in graph.nodes))
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, _ = _run_graph(metal_ctx, executor, plugged,
                               f01_hdr_ramp(32), request_id="plug",
                               with_cpu=False)
        assert not result.published
        assert result.diagnostic_code == "param_domain_unsupported"
        assert "fail-closed" in result.diagnostic_message

    def test_bad_source_format_rejected(self, metal_ctx):
        import Metal

        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        bad = metal_ctx.create_texture_2d(
            Metal.MTLPixelFormatRGBA8Unorm, 4, 4, shader_write=False)
        result = executor.execute_plan(None, bad, request_id="bad")
        assert not result.published
        assert result.diagnostic_code in ("bad_source_format",
                                          "execution_error")
