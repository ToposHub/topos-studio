"""P2 · 混响响应分析（FX-10 · 2026-11-21 计划）。

验收：Schroeder 逆积分 RT60 对理想指数 IR 精确；快照分析（全新
ReverbEffect，立体声脉冲探针）带限 T30 与目标 RT60 误差 ≤10%
（rv03 同参数口径）；只有干信号时明确报原因；生产引擎状态不参与。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.reverb_analysis import (
    analyze_reverb_snapshot,
    measure_rt60,
    measure_t30_band_limited,
)

RATE = 48000


def _rv03_params(target_s: float) -> dict:
    return {
        "enabled": True, "dry_wet": 100.0, "direct_db": -100.0,
        "er_db": -100.0, "reverb_db": 0.0, "pre_delay": 0.0,
        "distance": 0.0, "reverb_time": target_s * 1000.0,
        "modulation": 0.0, "brightness": 50.0,
        "rev_lo_gain": 0.0, "rev_hi_gain": 0.0,
    }


class TestSchroeder:
    def test_ideal_exponential_ir(self) -> None:
        """理想指数衰减 IR：RT60 精确（计划实验口径）。"""
        t = np.arange(4 * RATE) / RATE
        rt = measure_rt60(10.0 ** (-3.0 * t / 1.2), RATE)
        assert rt == pytest.approx(1.2, abs=1e-3)

    def test_no_decay_raises(self) -> None:
        with pytest.raises(ValueError):
            measure_rt60(np.ones(1000), RATE)

    def test_silent_raises(self) -> None:
        with pytest.raises(ValueError):
            measure_rt60(np.zeros(1000), RATE)


class TestSnapshot:
    @pytest.mark.parametrize("target_s", [0.5, 1.0, 2.0, 4.0])
    def test_rt60_within_10_percent(self, target_s) -> None:
        """快照分析：目标 RT60 误差 ≤10%（rv03 同参数、T30 口径）。"""
        snap = analyze_reverb_snapshot(_rv03_params(target_s), RATE)
        assert snap["rt60_error"] is None
        assert snap["rt60_seconds"] == pytest.approx(target_s, rel=0.10)

    def test_onset_and_length_reported(self) -> None:
        snap = analyze_reverb_snapshot(_rv03_params(2.0), RATE)
        assert snap["onset_samples"] >= 100  # 脉冲在样本 100 注入
        assert snap["ir_length_samples"] > 0

    def test_dry_only_reports_reason(self) -> None:
        """只有干信号：RT60 = None 且带原因（计划合同）。"""
        snap = analyze_reverb_snapshot(
            dict(_rv03_params(2.0), dry_wet=0.0), RATE)
        assert snap["rt60_seconds"] is None
        assert "dry-only" in snap["rt60_error"]

    def test_engine_state_not_touched(self) -> None:
        """分析建立全新实例：两次分析结果一致（确定性）。"""
        a = analyze_reverb_snapshot(_rv03_params(2.0), RATE)
        b = analyze_reverb_snapshot(_rv03_params(2.0), RATE)
        assert a["rt60_seconds"] == b["rt60_seconds"]

    def test_t30_helper_direct(self) -> None:
        # 衰减噪声 IR（纯指数是 DC 信号，会被带限移除）。
        rng = np.random.default_rng(5)
        t = np.arange(4 * RATE) / RATE
        ir = rng.standard_normal(4 * RATE) * 10.0 ** (-3.0 * t / 1.0)
        rt = measure_t30_band_limited(ir[None, :], RATE)
        assert rt == pytest.approx(1.0, rel=0.15)
