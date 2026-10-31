"""T-P0-31: TOPOS 4:2:2 R16 texture -> NV12/P010 VideoToolbox surface kernels.

卡验证 1（单元）：输入可控 4:2:2 ramps/color bars，验证 NV12/P010 平面
尺寸、采样位置、范围、色彩误差。CPU 参考实现与 MSL kernel 的采样契约
逐条对齐（见 renderer 内 kernel 注释）：

  - 输入三平面 R16Uint（4:2:2，chroma 与 luma 同高、半宽）
  - luma 1:1；chroma 输出 (cx,cy) 取四个 luma 位置编码后色度的均值
  - chroma 水平双线性（rint 后再 range 解码），垂直 1:1
  - limited/full range 解码 -> BT.709 RGB [0,1] -> 8-bit RGB code 量化
    （与 buffer 路径同一中间约定）-> BT.709 limited NV12/P010 编码
  - 奇数输出尺寸 host 侧拒绝

需要真实 Metal 设备（本机 RX 6800 无头可用；桌面窗口语义仍受
X-TEST-10 BLOCKED: DESKTOP_GPU_NOT_VERIFIED 约束）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_W, _H = 32, 16  # 4:2:0 输出尺寸（偶数）；4:2:2 源同高

_KR, _KB = 0.2126, 0.0722
_KG = 1.0 - _KR - _KB


def _rint(v):
    return np.floor(np.asarray(v, dtype=np.float64) + 0.5)


# ── CPU 参考实现（与 MSL 逐条对齐）──────────────────────────────────────

def _sample_planar_pixel(y, u, v, gx, gy, full_range):
    """prepare_planar_texture_source_pixel + to_u8 RGB code 量化。"""
    y_code = float(y[gy, gx])
    cx = (gx + 0.5) / 2.0 - 0.5
    cy = int(gy)
    x0 = int(np.floor(cx))
    fx = cx - x0
    hi = u.shape[1] - 1
    x0c = min(max(x0, 0), hi)
    x1c = min(max(x0 + 1, 0), hi)
    u_code = float(np.rint(
        float(u[cy, x0c]) * (1.0 - fx) + float(u[cy, x1c]) * fx))
    v_code = float(np.rint(
        float(v[cy, x0c]) * (1.0 - fx) + float(v[cy, x1c]) * fx))
    if full_range:
        yc = y_code / 1023.0
        cb = (u_code - 1023.0 * 0.5) / 1023.0
        cr = (v_code - 1023.0 * 0.5) / 1023.0
    else:
        scale_8 = float(1 << (10 - 8))
        yc = (y_code - 16.0 * scale_8) / (219.0 * scale_8)
        cb = (u_code - 128.0 * scale_8) / (224.0 * scale_8)
        cr = (v_code - 128.0 * scale_8) / (224.0 * scale_8)
    rgb = np.clip(np.array([
        yc + 2.0 * (1.0 - _KR) * cr,
        yc - 2.0 * _KB * (1.0 - _KB) / _KG * cb
        - 2.0 * _KR * (1.0 - _KR) / _KG * cr,
        yc + 2.0 * (1.0 - _KB) * cb,
    ], dtype=np.float64), 0.0, 1.0)
    return np.clip(_rint(rgb * 255.0), 0, 255)  # to_u8


def _encode_420(rgb_code, p010):
    """rgb_code_to_yuv420p8 / rgb_code_to_yuv420p10（BT.709 limited）。"""
    rgb = np.clip(rgb_code / 255.0, 0.0, 1.0)
    yv = float(np.dot(rgb, (_KR, _KG, _KB)))
    cb = (rgb[2] - yv) / 1.8556
    cr = (rgb[0] - yv) / 1.5748
    if p010:
        return (
            float(np.clip(64.0 + 876.0 * yv, 64.0, 940.0)),
            float(np.clip(512.0 + 896.0 * cb, 64.0, 960.0)),
            float(np.clip(512.0 + 896.0 * cr, 64.0, 960.0)),
        )
    return (
        float(np.clip(16.0 + 219.0 * yv, 16.0, 235.0)),
        float(np.clip(128.0 + 224.0 * cb, 16.0, 240.0)),
        float(np.clip(128.0 + 224.0 * cr, 16.0, 240.0)),
    )


def _cpu_reference(y, u, v, p010=False):
    out_y = np.zeros((_H, _W), dtype=np.uint16 if p010 else np.uint8)
    out_u = np.zeros((_H // 2, _W // 2), dtype=np.uint16 if p010 else np.uint8)
    out_v = np.zeros_like(out_u)
    for gy in range(_H):
        for gx in range(_W):
            code = _sample_planar_pixel(y, u, v, gx, gy, False)
            yv, _, _ = _encode_420(code, p010)
            out_y[gy, gx] = int(_rint(yv))
            if gx % 2 == 0 and gy % 2 == 0:
                acc = [0.0, 0.0]
                for dx in (0, 1):
                    for dy in (0, 1):
                        c = _sample_planar_pixel(
                            y, u, v, gx + dx, gy + dy, False)
                        _, ecb, ecr = _encode_420(c, p010)
                        acc[0] += ecb
                        acc[1] += ecr
                out_u[gy // 2, gx // 2] = int(_rint(acc[0] / 4.0))
                out_v[gy // 2, gx // 2] = int(_rint(acc[1] / 4.0))
    return out_y, out_u, out_v


def _fixture_source():
    """确定性 ramps + color bars（10-bit limited 代码值域内）。"""
    yy, xx = np.mgrid[0:_H, 0:_W]
    y = (64 + (940 - 64) * (xx * 31 + yy * 7) // (_W * 31 + _H * 7)).astype(
        np.uint16)
    y = np.clip(y, 64, 940)
    cols = np.arange(_W // 2)
    u = (512 + (cols * 13) % 401 - 200).astype(np.uint16)
    u = np.repeat(u[None, :], _H, axis=0)
    v = (512 + (cols * 29 + 5) % 401 - 200).astype(np.uint16)
    v = np.repeat(v[None, :], _H, axis=0)
    # color bars：前 8 列放饱和测试块
    for i, (uc, vc) in enumerate(((512, 960), (512, 64), (64, 512),
                                  (960, 512), (200, 200), (824, 824),
                                  (64, 64), (960, 960))):
        u[:, i] = uc
        v[:, i] = vc
    return np.ascontiguousarray(y), np.ascontiguousarray(u), \
        np.ascontiguousarray(v)


# ── Metal kernel 对拍 ────────────────────────────────────────────────────

def _make_context():
    from src.shared.gpu.metal.context import MetalComputeContext

    if not MetalComputeContext.is_supported():
        pytest.skip("requires an available Metal device")
    return MetalComputeContext()


def _dispatch_kernel(ctx, y, u, v, p010):
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer, _TIMELINE_METAL_MSL,
    )

    metal = ctx._metal
    fmt_y_in = metal.MTLPixelFormatR16Uint
    tex_y = ctx.create_texture_from_numpy(y, fmt_y_in, _W, _H)
    tex_u = ctx.create_texture_from_numpy(u, fmt_y_in, _W // 2, _H)
    tex_v = ctx.create_texture_from_numpy(v, fmt_y_in, _W // 2, _H)
    if p010:
        out_y = ctx.create_texture_2d(
            metal.MTLPixelFormatR16Unorm, _W, _H, shader_write=True)
        out_uv = ctx.create_texture_2d(
            metal.MTLPixelFormatRG16Unorm, _W // 2, _H // 2,
            shader_write=True)
    else:
        out_y = ctx.create_texture_2d(
            metal.MTLPixelFormatR8Unorm, _W, _H, shader_write=True)
        out_uv = ctx.create_texture_2d(
            metal.MTLPixelFormatRG8Unorm, _W // 2, _H // 2,
            shader_write=True)

    params = TimelineMetalExportRenderer._make_params(
        _W, _H, _W, _H,
        _W // 2, _H, _W // 2, _H,
        10, 2, 1, True, False,  # limited range
        1.0, False, True,       # opacity 1、无 grade、输出 420
        _KR, _KB, None,
    )
    name = ("timeline_fused_export_topos_texture_p010_surface" if p010
            else "timeline_fused_export_topos_texture_yuv420_surface")
    kernel = ctx.compile_kernel(name, _TIMELINE_METAL_MSL)
    ctx.dispatch(
        kernel,
        buffers={0: ctx.create_buffer_from_numpy(params)},
        textures={0: tex_y, 1: tex_u, 2: tex_v, 3: out_y, 4: out_uv},
        threads=(_W, _H, 1),
    )
    ctx.finish()

    dt = np.uint16 if p010 else np.uint8
    got_y = ctx.read_texture(out_y, _W, _H, dtype=dt)
    # UV 平面行宽按元素数 = 像素数 × 2（Cb/Cr 交错）；region 用像素宽。
    got_uv = ctx.read_texture(
        out_uv, _W, _H // 2, dtype=dt, region_width=_W // 2).reshape(
        _H // 2, _W // 2, 2)
    if p010:
        # R16Unorm 存储的是 code/1023 * 65535（unorm 编码）；反归一到
        # 10-bit 代码域再对拍（P010 高位对齐语义，误差 ≤ 1 LSB）。
        got_y = np.rint(got_y.astype(np.float64) * 1023.0 / 65535.0)
        got_uv = np.rint(got_uv.astype(np.float64) * 1023.0 / 65535.0)
    return got_y, got_uv[..., 0], got_uv[..., 1]


def _assert_close(got, want, label, max_err=2, psnr_min=50.0):
    diff = np.abs(got.astype(np.int64) - want.astype(np.int64))
    mse = float((diff.astype(np.float64) ** 2).mean())
    psnr = 99.0 if mse == 0.0 else 10.0 * np.log10(255.0 ** 2 / max(mse, 1e-9))
    if diff.max() > max_err or psnr < psnr_min:
        artifacts = _save_diff_artifacts(got, want, label, {
            "max_err": int(diff.max()),
            "max_err_at": [int(v) for v in np.unravel_index(
                int(diff.argmax()), diff.shape)],
            "psnr": round(psnr, 3),
            "max_err_threshold": max_err,
            "psnr_min": psnr_min,
        })
        assert diff.max() <= max_err and psnr >= psnr_min, (
            f"{label}: max err {diff.max()} at "
            f"{np.unravel_index(int(diff.argmax()), diff.shape)}, "
            f"PSNR {psnr:.1f} < {psnr_min}; diff artifacts: {artifacts or '(PIL unavailable)'}")


# 验证 3 固定门槛：8-bit NV12 与 10-bit P010 各自定义；超阈值保存 diff 图。
MAX_ERR_8BIT = 2
MAX_ERR_10BIT = 2  # P010 unorm 读回量化引入 ≤1 LSB，仍按 2 收口
PSNR_MIN_8BIT = 50.0
PSNR_MIN_10BIT = 50.0


def _save_diff_artifacts(got, want, label: str, stats: dict) -> str:
    """T-P0-31 验证 3：对拍超阈值时保存 got/want/放大差值图 + 机器可读统计。

    证据目录遵循计划 §3.1（bench_out/ai_upgrade/ 下、不与用户素材混放）。
    保存失败不掩盖对拍失败本身——返回路径仅用于断言消息可追溯。
    """
    import json
    from datetime import datetime

    repo_root = Path(__file__).resolve().parents[2]
    out_dir = (repo_root / "bench_out" / "ai_upgrade" / "T-P0-31_kernel_diff" /
               datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)

        def to_u8(arr: np.ndarray) -> np.ndarray:
            a = np.asarray(arr, dtype=np.float64)
            if a.max(initial=0.0) > 255.0:  # 10-bit 代码域 → 8-bit 可视化
                a = a / 4.0
            return np.clip(a, 0.0, 255.0).astype(np.uint8)

        diff_amp = np.clip(
            (np.asarray(got, dtype=np.float64)
             - np.asarray(want, dtype=np.float64)) * 16.0, 0.0, 255.0
        ).astype(np.uint8)  # 16× 放大让 ≤2 LSB 的差异在图上可见
        Image.fromarray(to_u8(got)).save(out_dir / f"{label}_got.png")
        Image.fromarray(to_u8(want)).save(out_dir / f"{label}_want.png")
        Image.fromarray(diff_amp).save(out_dir / f"{label}_diff_x16.png")
        (out_dir / f"{label}_stats.json").write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        return str(out_dir)
    except Exception:
        return ""


    @pytest.mark.skipif(
        os.environ.get("TOPOS_METAL_KERNEL_TESTS", "0") != "1",
        reason="Metal kernel tests are opt-in (TOPOS_METAL_KERNEL_TESTS=1)")
    class TestTopos422SurfaceKernels:
        def test_nv12_surface_matches_cpu_reference(self):
            ctx = _make_context()
            y, u, v = _fixture_source()
            got = _dispatch_kernel(ctx, y, u, v, p010=False)
            want = _cpu_reference(y, u, v, p010=False)
            _assert_close(got[0], want[0], "nv12 Y",
                          max_err=MAX_ERR_8BIT, psnr_min=PSNR_MIN_8BIT)
            _assert_close(got[1], want[1], "nv12 Cb",
                          max_err=MAX_ERR_8BIT, psnr_min=PSNR_MIN_8BIT)
            _assert_close(got[2], want[2], "nv12 Cr",
                          max_err=MAX_ERR_8BIT, psnr_min=PSNR_MIN_8BIT)

        def test_p010_surface_matches_cpu_reference(self):
            ctx = _make_context()
            y, u, v = _fixture_source()
            got = _dispatch_kernel(ctx, y, u, v, p010=True)
            want = _cpu_reference(y, u, v, p010=True)
            _assert_close(got[0], want[0], "p010 Y",
                          max_err=MAX_ERR_10BIT, psnr_min=PSNR_MIN_10BIT)
            _assert_close(got[1], want[1], "p010 Cb",
                          max_err=MAX_ERR_10BIT, psnr_min=PSNR_MIN_10BIT)
            _assert_close(got[2], want[2], "p010 Cr",
                          max_err=MAX_ERR_10BIT, psnr_min=PSNR_MIN_10BIT)


def test_topos_422_buffer_mode_still_fails_closed():
    """422 源 + 420 无 surface 仍拒绝（CPU buffer 422→420 不存在）。"""
    pytestmark_skip = os.environ.get("TOPOS_METAL_KERNEL_TESTS", "0") != "1"
    if pytestmark_skip:
        pytest.skip("Metal kernel tests are opt-in")
    from src.features.edit.core.timeline_metal_export_renderer import (
        TimelineMetalExportRenderer,
    )
    from src.shared.export.gpu_trail import ExportGpuTrail

    ctx = _make_context()
    trail = ExportGpuTrail("topos-buffer-gate")
    renderer = TimelineMetalExportRenderer(trail)
    try:
        result = renderer.render_metal_texture_frame(
            object(), None, _W, _H, 1.0, output_yuv420p=True)
        assert result is None
        assert renderer.last_fallback_reason is not None
    finally:
        renderer.release()
