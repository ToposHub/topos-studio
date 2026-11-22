"""B08 · 波形金字塔与预算化解码窗口回归（主计划 §14 步骤 1/2/3）。

- 金字塔聚合与 numpy 直算 oracle 一致（min/max/RMS，整 tile 与跨 tile）；
  极值保留（父级聚合不丢峰）；概览列边界保守包含。
- plan_decode_window：视窗必含、预读预算、裁剪语义、字节核算。
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from src.features.edit.core.waveform_pyramid import (
    BASE_TILE_SAMPLES,
    WaveformPyramid,
    plan_decode_window,
)


class TestWaveformPyramid:
    def _pcm(self, total: int = 48000, seed: int = 7) -> np.ndarray:
        rng = np.random.default_rng(seed)
        pcm = rng.standard_normal((2, total)).astype(np.float32) * 0.3
        pcm[0, 12345] = 1.0   # 尖峰（极值保留探针）
        pcm[1, 23456] = -1.0
        return pcm

    def test_full_range_matches_numpy_oracle(self) -> None:
        pcm = self._pcm()
        pyr = WaveformPyramid(pcm, levels=4)
        mn, mx, rms = pyr.range_extremes(0, pyr.total_samples, 0)
        ch = pcm[0].astype(np.float64)
        assert mn == pytest.approx(float(ch.min()), abs=1e-6)
        assert mx == pytest.approx(float(ch.max()), abs=1e-6)
        assert rms == pytest.approx(float(np.sqrt(np.mean(ch ** 2))), rel=1e-5)

    def test_extreme_preserved_in_sub_range(self) -> None:
        pcm = self._pcm()
        pyr = WaveformPyramid(pcm, levels=4)
        # 尖峰在 12345；跨 tile 查询必须保留 1.0（不被父级聚合吞掉）
        mn, mx, _ = pyr.range_extremes(12000, 13000, 0,
                                       raw_pcm=pcm)
        assert mx == pytest.approx(1.0, abs=1e-6)
        mn2, mx2, _ = pyr.range_extremes(23000, 24000, 1, raw_pcm=pcm)
        assert mn2 == pytest.approx(-1.0, abs=1e-6)

    def test_whole_tile_and_partial_tile_agree_with_oracle(self) -> None:
        pcm = self._pcm(total=32000, seed=11)
        pyr = WaveformPyramid(pcm, levels=2)
        ch = pcm[0].astype(np.float64)
        # 整 tile 对齐区间
        lo, hi = 2 * BASE_TILE_SAMPLES, 5 * BASE_TILE_SAMPLES
        mn, mx, rms = pyr.range_extremes(lo, hi, 0, raw_pcm=pcm)
        seg = ch[lo:hi]
        assert mn == pytest.approx(float(seg.min()), abs=1e-6)
        assert mx == pytest.approx(float(seg.max()), abs=1e-6)
        assert rms == pytest.approx(float(np.sqrt(np.mean(seg ** 2))), rel=1e-5)
        # 任意非对齐区间（raw 精确）
        lo, hi = 777, 4567
        mn, mx, rms = pyr.range_extremes(lo, hi, 0, raw_pcm=pcm)
        seg = ch[lo:hi]
        assert mn == pytest.approx(float(seg.min()), abs=1e-6)
        assert rms == pytest.approx(float(np.sqrt(np.mean(seg ** 2))), rel=1e-5)

    def test_overview_columns_superset_of_oracle(self) -> None:
        """概览（无 raw）保守包含：列 min ≤ oracle min，max ≥ oracle max。"""
        pcm = self._pcm(96000)
        pyr = WaveformPyramid(pcm, levels=3)
        width = 64
        mn_col, mx_col, _ = pyr.overview(width, 0)
        bounds = np.linspace(0, 96000, width + 1).astype(int)
        ch = pcm[0].astype(np.float64)
        for i in range(width):
            seg = ch[bounds[i]:bounds[i + 1]]
            assert mn_col[i] <= float(seg.min()) + 1e-6
            assert mx_col[i] >= float(seg.max()) - 1e-6

    def test_level_for_width_monotonic(self) -> None:
        pcm = self._pcm()
        pyr = WaveformPyramid(pcm, levels=4)
        lv1 = pyr.level_for_width(16)     # 每屏极少像素 → 粗级
        lv2 = pyr.level_for_width(10000)  # 大放大 → 细级
        assert lv1.tile_samples > lv2.tile_samples

    def test_memory_budget_truncates_levels(self) -> None:
        pcm = self._pcm(48000 * 60)  # 长素材
        pyr_full = WaveformPyramid(pcm, levels=8)
        pyr_budget = WaveformPyramid(pcm, levels=8, max_bytes=4096)
        assert pyr_budget.level_count < pyr_full.level_count
        assert pyr_budget.level_count >= 1


class TestPlanDecodeWindow:
    def test_view_inside_extent(self) -> None:
        plan = plan_decode_window(0, 48000 * 100, 48000 * 10, 48000 * 12,
                                  2, prefetch_samples=48000)
        assert plan.window_start == 48000 * 10
        assert plan.window_end == 48000 * 12
        assert plan.prefetch_start == 48000 * 9
        assert plan.prefetch_end == 48000 * 13
        assert not plan.clamped
        assert plan.bytes_window == 48000 * 2 * 2 * 4

    def test_view_clamped_to_extent(self) -> None:
        plan = plan_decode_window(1000, 50000, 0, 60000, 1)
        assert plan.window_start == 1000
        assert plan.window_end == 50000
        assert plan.prefetch_start == 1000
        assert plan.prefetch_end == 50000

    def test_budget_truncates_prefetch_first(self) -> None:
        total = 48000 * 600
        view = (48000 * 100, 48000 * 102)
        plan = plan_decode_window(0, total, *view, 2,
                                  prefetch_samples=48000 * 50,
                                  max_bytes=48000 * 4 * 2 * 4)
        assert plan.window_start == view[0] and plan.window_end == view[1]
        assert plan.prefetch_start == view[0]  # 预读被预算裁掉
        assert plan.prefetch_end == view[1]
        assert plan.clamped and "prefetch" in plan.reason

    def test_budget_truncates_window_as_last_resort(self) -> None:
        total = 48000 * 600
        plan = plan_decode_window(0, total, 0, 48000 * 100, 2,
                                  prefetch_samples=0,
                                  max_bytes=48000 * 10 * 2 * 4)
        assert plan.window_end == 48000 * 10  # 保视窗头部
        assert plan.clamped and "budget" in plan.reason

    def test_empty_extent_rejected(self) -> None:
        with pytest.raises(ValueError):
            plan_decode_window(100, 100, 0, 10, 2)
