"""复查批次 E 回归：R27 真峰流式边界与取消。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.3
（F-R08）：文件尾漏峰、首块随分块变化、扫描无取消。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.loudness_analyzer import (
    LoudnessAnalysisCancelled,
    _TruePeakEstimator,
    analyze_loudness,
)

pytestmark = pytest.mark.edit_core


def test_tail_intersample_pulse_not_missed() -> None:
    """F-R08：64 样本末 4 个 [0,1,1,0]——真峰 ~1.85dBTP（旧 0.0154→0dBTP
    回退样本峰）。"""
    sig = np.zeros((1, 64))
    sig[0, -4:] = [0.0, 1.0, 1.0, 0.0]
    report = analyze_loudness(sig, 48000.0)
    # 镜像延拓约定：线性 ~1.234（审计零填充口径 1.2379 同量级），
    # 旧缺陷回退样本峰报 0 dBTP
    assert report.true_peak_dbtp > 1.5
    assert report.true_peak_dbtp == pytest.approx(1.745, abs=0.05)


def test_chunk_invariance_whole_vs_arbitrary() -> None:
    """whole 与任意分块（含首块 1 样本、末块 < 滤波器长度）一致。"""
    rng = np.random.default_rng(42)
    sig = rng.standard_normal((2, 500)) * 0.5
    ref = analyze_loudness(sig, 48000.0, block_size=500).true_peak_dbtp
    for bs in (1, 7, 64, 97, 499, 500):
        got = analyze_loudness(sig, 48000.0, block_size=bs).true_peak_dbtp
        assert got == pytest.approx(ref, abs=1e-9), bs


def test_constant_signal_no_startup_overshoot() -> None:
    """常量信号：无阶跃假过冲（历史 1.119 类问题），分块无关。"""
    sig = np.ones((1, 64))
    ref = analyze_loudness(sig, 48000.0, block_size=1000).true_peak_dbtp
    assert ref < 0.05  # dBTP ≈ 0（线性 ~1.0003）
    for bs in (1, 2, 3, 4, 64):
        got = analyze_loudness(sig, 48000.0, block_size=bs).true_peak_dbtp
        assert got == pytest.approx(ref, abs=1e-9), bs


def test_head_middle_tail_pulses_consistent() -> None:
    """头/中/尾单脉冲：估计值只依赖信号本身，边界不漏报不假峰。"""
    def peak_at(pos: int) -> float:
        sig = np.zeros((1, 96))
        sig[0, pos] = 1.0
        return analyze_loudness(sig, 48000.0).true_peak_dbtp

    head = peak_at(1)
    middle = peak_at(48)
    tail = peak_at(94)
    # 位置无关：头/中/尾的孤立脉冲真峰一致（孤立脉冲 ≈ 0dBTP 本身）
    assert abs(head - middle) < 0.5
    assert abs(tail - middle) < 0.5


def test_progress_cancel_raises() -> None:
    sig = np.random.default_rng(1).standard_normal((1, 200))
    with pytest.raises(LoudnessAnalysisCancelled):
        analyze_loudness(sig, 48000.0, block_size=10,
                         progress=lambda frac: frac < 0.5)


def test_estimator_finalize_idempotent() -> None:
    est = _TruePeakEstimator(channels=1)
    est.process(np.ones((1, 32)))
    est.finalize()
    peak1 = est.peak
    est.finalize()
    assert est.peak == peak1
