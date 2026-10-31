"""Normalize 百分位链路的 Metal 路径测试（计划 P4-03）。

验收目标（计划 §0.4）：
① macOS 不再返回空直方图；
② CDF/百分位下移 GPU；
③ 无法支持时必须明确诊断。

两层：kernel 奇偶校验（真机 vs numpy 参考）与端到端（standalone GL 4.1
context 上 HistogramManager 自动走 Metal 路径，percentile 不再是空操作）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.features.composite.evaluators.histogram_manager import HistogramManager
from src.shared.gpu.metal import MetalComputeContext
from src.shared.gpu.metal.context import MetalComputeContext as _Ctx
from src.shared.gpu.metal.kernels_histogram import (
    METAL_HISTOGRAM_MSL,
    PERCENTILE_BIN_SENTINEL,
    PERCENTILE_PARAMS_DTYPE,
    cdf_reference,
    decode_percentile_bins,
    histogram_reference,
    percentile_bins_reference,
)

requires_metal = pytest.mark.skipif(
    not _Ctx.is_supported(), reason="无 Metal 设备"
)

ATOL = 1e-6


def _random_image(h: int, w: int, seed: int = 29) -> np.ndarray:
    """HDR 范围随机图（>1 与 <0 的值触发 clamp 语义）。"""
    rng = np.random.default_rng(seed)
    return (rng.random((h, w, 4)) * 1.6 - 0.3).astype(np.float16)


def _run_kernels(ctx: MetalComputeContext, img: np.ndarray,
                 channel_mask: int, low_p: float, high_p: float):
    h, w = img.shape[:2]
    metal = ctx
    src = metal.create_buffer_from_numpy(img)
    hist_buf = metal.create_buffer_from_numpy(np.zeros(1024, dtype="<u4"))
    total_buf = metal.create_buffer_from_numpy(np.zeros(1, dtype="<u4"))
    res_buf = metal.create_buffer_from_numpy(np.array([w, h], dtype="<u4"))
    mask_buf = metal.create_buffer_from_numpy(np.array([channel_mask], dtype="<u4"))
    hist_kernel = metal.compile_kernel("histogram", METAL_HISTOGRAM_MSL)
    metal.dispatch(hist_kernel, buffers={
        0: src, 1: hist_buf, 2: total_buf, 3: res_buf, 4: mask_buf,
    }, threads=(w, h, 1))

    params = np.zeros(1, dtype=PERCENTILE_PARAMS_DTYPE)
    params["low_percentile"] = low_p
    params["high_percentile"] = high_p
    params["channel_mask"] = channel_mask
    # cdf 必须零初始化（与 _MetalHistogramPath.compute 一致）：mask 外
    # 通道 kernel 提前 return 不写，未初始化内存会读到 GPU 垃圾。
    cdf_buf = metal.create_buffer_from_numpy(np.zeros(1024, dtype="<f4"))
    bins_buf = metal.create_buffer_from_numpy(
        np.full(8, PERCENTILE_BIN_SENTINEL, dtype="<u4")
    )
    cdf_kernel = metal.compile_kernel("histogram_cdf", METAL_HISTOGRAM_MSL)
    metal.dispatch(cdf_kernel, buffers={
        0: hist_buf, 1: cdf_buf, 2: bins_buf, 3: metal.create_buffer_from_numpy(params),
    }, threads=(256, 4, 1))
    metal.finish()

    hist = np.frombuffer(metal.read_buffer(hist_buf), dtype="<u4").copy()
    total = int(np.frombuffer(metal.read_buffer(total_buf), dtype="<u4")[0])
    cdf = np.frombuffer(metal.read_buffer(cdf_buf), dtype="<f4").reshape(4, 256).copy()
    raw_bins = np.frombuffer(metal.read_buffer(bins_buf), dtype="<u4").copy()
    bins = np.empty(8, dtype="<u4")
    for ch in range(4):
        low_bin, high_bin = decode_percentile_bins(raw_bins, ch)
        bins[ch * 2] = low_bin
        bins[ch * 2 + 1] = high_bin
    return hist, total, cdf, bins


@requires_metal
class TestHistogramKernelParity:
    @pytest.mark.parametrize("channel_mask", [15, 5, 8])
    def test_histogram_bins_match_reference_exactly(self, channel_mask):
        """装箱为确定性整数运算，必须与 numpy 参考完全一致。"""
        img = _random_image(48, 64)
        ctx = MetalComputeContext()
        hist, total, _, _ = _run_kernels(ctx, img, channel_mask, 0.01, 0.99)
        ref_hist, ref_total = histogram_reference(img, channel_mask)
        np.testing.assert_array_equal(hist, ref_hist)
        assert total == ref_total == 64 * 48

    def test_cdf_matches_reference(self):
        img = _random_image(32, 40, seed=31)
        ctx = MetalComputeContext()
        hist, _, cdf, _ = _run_kernels(ctx, img, 15, 0.02, 0.98)
        ref_cdf = cdf_reference(hist, 15)
        np.testing.assert_allclose(cdf, ref_cdf, atol=ATOL)

    @pytest.mark.parametrize("channel_mask", [15, 5, 8])
    def test_cdf_skipped_channels_stay_zero(self, channel_mask):
        """mask 外通道的 cdf 必须是精确零（零初始化 + kernel 提前 return）。

        契约回归：`_percentiles_from_result` 依赖 ``cdf.sum()==0 → (0,1)``
        兜底；未初始化 buffer 读到 GPU 垃圾时该兜底被绕过。
        """
        img = _random_image(32, 40, seed=53)
        ctx = MetalComputeContext()
        hist, _, cdf, _ = _run_kernels(ctx, img, channel_mask, 0.02, 0.98)
        ref_cdf = cdf_reference(hist, channel_mask)
        for ch in range(4):
            if channel_mask & (1 << ch):
                np.testing.assert_allclose(cdf[ch], ref_cdf[ch], atol=ATOL)
            else:
                assert cdf[ch].sum() == 0.0

    @requires_metal
    def test_metal_path_compute_zeroes_skipped_channels(self):
        """生产路径 `_MetalHistogramPath.compute` 的同一契约（mask=5）。"""
        from src.features.composite.evaluators.histogram_manager import (
            _MetalHistogramPath,
        )

        path = _MetalHistogramPath(gl_ctx=None)
        img = _random_image(24, 32, seed=59)
        hist, cdf, total, bins = path.compute(img, 5, 0.02, 0.98)
        assert total == 24 * 32
        assert cdf[1].sum() == 0.0  # G 未入选 mask
        assert cdf[3].sum() == 0.0  # A 未入选 mask
        ref_hist, ref_total = histogram_reference(img, 5)
        np.testing.assert_array_equal(hist, ref_hist)
        assert total == ref_total

    @pytest.mark.parametrize("low_p,high_p", [(0.01, 0.99), (0.05, 0.95)])
    def test_percentile_bins_match_reference(self, low_p, high_p):
        img = _random_image(40, 56, seed=37)
        ctx = MetalComputeContext()
        hist, _, cdf, bins = _run_kernels(ctx, img, 15, low_p, high_p)
        ref_cdf = cdf_reference(hist, 15)
        for ch in range(4):
            ref_low, ref_high = percentile_bins_reference(ref_cdf, ch, low_p, high_p)
            assert int(bins[ch * 2]) == ref_low
            assert int(bins[ch * 2 + 1]) == ref_high


# ---------------------------------------------------------------------------
# Manager 集成层
# ---------------------------------------------------------------------------


class TestManagerGating:
    def _mgr(self):
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": "4.1 ATI-6"}
        with patch.object(HistogramManager, "_maybe_init_metal_path") as m:
            mgr = HistogramManager(ctx)
        return mgr, m

    def test_metal_path_makes_is_available_true(self):
        """①验收前置：GL 4.1 + Metal 可用 → is_available() 为 True。"""
        if not MetalComputeContext.is_supported():
            pytest.skip("无 Metal 设备")
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": "4.1 ATI-6"}
        mgr = HistogramManager(ctx)
        assert not mgr._available  # GL compute 不可用
        assert mgr.is_available()  # Metal 路径解锁
        assert mgr.get_stats()["compute_backend"] == "metal"

    @pytest.mark.parametrize("version,available", [
        # Mesa 风格带括号后缀的版本串：旧 split('.') 解析抛 ValueError，
        # 曾把 GL 4.3+ 机器静默判为不可用（跳过试编译直接 False）。
        ("4.6 (Core Profile) Mesa 23.0.4", True),
        ("4.3 (Core Profile) Mesa 22.0", True),
        ("4.6.0 NVIDIA 535.154.05", True),
        ("4.1 ATI-6.1.13", False),
        ("3.3 Core Profile", False),
    ])
    def test_gl_version_parse_is_vendor_string_safe(self, version, available):
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": version}
        with patch.object(HistogramManager, "_maybe_init_metal_path"):
            mgr = HistogramManager(ctx)
        assert mgr._available is available, version

    def test_no_backend_reports_explicit_diagnostic(self):
        """③验收：两者皆不可用时给出明确诊断（非静默）。"""
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": "4.1 ATI-6"}
        with patch(
            "src.features.composite.evaluators.histogram_manager."
            "MetalComputeContext"
        ) as mctx:
            mctx.is_supported.return_value = False
            mgr = HistogramManager(ctx)
        assert not mgr.is_available()
        assert mgr.get_diagnostic() != ""
        assert "percentile" in mgr.get_stats()["diagnostic"]

    def test_metal_init_failure_not_fatal(self):
        ctx = MagicMock()
        ctx.info = {"GL_VERSION": "4.1 ATI-6"}
        with patch(
            "src.features.composite.evaluators.histogram_manager."
            "MetalComputeContext"
        ) as mctx:
            mctx.is_supported.return_value = True
            mctx.side_effect = RuntimeError("no device")
            mgr = HistogramManager(ctx)
        assert not mgr.is_available()
        assert "no device" in mgr.get_diagnostic()


# ---------------------------------------------------------------------------
# 端到端：standalone GL 4.1 + 真机 Metal
# ---------------------------------------------------------------------------

try:
    import moderngl
    _HAS_GL = True
except Exception:  # pragma: no cover
    _HAS_GL = False


@pytest.mark.gpu_renderer
@requires_metal
class TestEndToEndPercentileOnMacOSPath:
    def test_percentile_no_longer_noop_on_gl41(self):
        """①验收：macOS GL 4.1 上 percentile 返回真实区间而非 (0,1)。"""
        gl = moderngl.create_standalone_context()
        try:
            mgr = HistogramManager(gl)
            assert mgr.is_available()
            w, h = 64, 48
            rng = np.random.default_rng(41)
            # 低动态范围图：真实 percentile 区间必然 ≠ (0, 1)
            img = (0.2 + rng.random((h, w, 4)) * 0.3).astype(np.float16)
            tex = gl.texture((w, h), 4, dtype="f2")
            tex.write(img.tobytes())

            result = mgr.compute_percentiles(
                tex, w, h, low_percentile=1.0, high_percentile=99.0,
                channel_mask=[1.0, 1.0, 1.0, 0.0], use_cache=False,
            )
            assert result["low"] > 0.0   # 旧路径恒为 (0.0, 1.0) 的静默空操作
            assert result["high"] < 1.0
            assert result["low"] < result["high"]

            # 与 numpy 参考闭环
            ref_hist, _ = histogram_reference(img, 7)  # R|G|B
            ref_cdf = cdf_reference(ref_hist, 7)
            ref_low, ref_high = percentile_bins_reference(ref_cdf, 0, 0.01, 0.99)
            assert abs(result["low"] - (ref_low + 0.5) / 256.0) <= 1 / 256 + 1e-6
            assert abs(result["high"] - (ref_high + 0.5) / 256.0) <= 1 / 256 + 1e-6
            tex.release()
        finally:
            gl.release()
