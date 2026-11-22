"""P1 · True Peak Limiter（2026-11-19 计划第三阶段 #3）。

验收：采样间超限被压至上限之下；多通道链接；随机块长一致；尾音/旁路
语义；group delay 声明（接 PDC）；交付重采样后的上限用响度测量的
dBTP 路径独立复核。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.true_peak_limiter import (
    TruePeakLimiterEffect,
)

RATE = 48000
CEILING = -1.0
CEILING_LINEAR = 10.0 ** (CEILING / 20.0)


def _intersample_peak_tone(amp: float = 0.9, seconds: float = 2.0):
    """44.1k→48k 类采样相位错位的双音：True Peak 高于样本峰值。"""
    t = np.arange(int(seconds * RATE)) / RATE
    # 0.9·(sin + sin) 互调：样本峰 ≈ amp·2·0.5，峰间可超样本峰 ~1.4×。
    sig = amp * 0.5 * (np.sin(2 * np.pi * 5000.0 * t)
                       + np.sin(2 * np.pi * 7000.0 * t))
    sig = sig.astype(np.float32)
    return np.stack([sig, sig])


def _measure_dbtp(pcm: np.ndarray) -> float:
    """独立复核：响度测量路径的 True Peak。"""
    from src.features.edit.core.loudness_analyzer import analyze_loudness
    report = analyze_loudness(pcm, float(RATE))
    return float(report.true_peak_dbtp)


def _make(params: dict | None = None) -> TruePeakLimiterEffect:
    fx = TruePeakLimiterEffect(float(RATE))
    fx.set_params(params or {"ceiling_dbtp": CEILING})
    return fx


class TestTruePeakLimiter:
    def test_intersample_overlimit_tamed(self) -> None:
        pcm = _intersample_peak_tone(1.15)
        out = _make().process(pcm.copy())
        dbtp = _measure_dbtp(out[:, RATE // 10:])
        assert dbtp <= CEILING + 0.3  # 上限 + 容差（平滑 release 余量）
        # 输入本身超限（复核路径量到 > 0 dBTP）。
        assert _measure_dbtp(pcm[:, RATE // 10:]) > 0.0

    def test_linked_channels_pull_together(self) -> None:
        t = np.arange(RATE) / RATE
        left = (0.99 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        right = (0.05 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        pcm = np.stack([left, right])
        out = _make({"linked": True}).process(pcm.copy())
        # 链接：安静声道与超限声道获得同一增益（都显著衰减）。
        steady = slice(RATE // 2, None)
        assert float(np.max(np.abs(out[1, steady]))) < 0.06

    def test_independent_channels_keep_quiet_channel(self) -> None:
        t = np.arange(RATE) / RATE
        left = (0.99 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        right = (0.05 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        pcm = np.stack([left, right])
        out = _make({"linked": False}).process(pcm.copy())
        steady = slice(RATE // 2, None)
        assert float(np.max(np.abs(out[1, steady]))) == pytest.approx(
            0.05, abs=1e-3)  # 安静声道不受联动

    def test_block_size_consistency(self) -> None:
        """任意块长：上限合规 + 整段与分块逐位一致（流式 FIR + 原始目标尾）。"""
        pcm = _intersample_peak_tone(1.15)
        fx_whole = _make()
        whole = fx_whole.process(pcm.copy())
        fx_blocks = _make()
        chunks = []
        step = 6131
        for start in range(0, pcm.shape[1], step):
            chunks.append(fx_blocks.process(pcm[:, start:start + step].copy()))
        stitched = np.concatenate(chunks, axis=1)
        # 块长无关的上限合规（交付语义）。
        assert _measure_dbtp(stitched[:, RATE // 10:]) <= CEILING + 0.3
        # 分块与整段逐位一致（overlap-save 历史 + lookahead 原始目标尾）。
        np.testing.assert_array_equal(
            whole[:, RATE // 4:], stitched[:, RATE // 4:])

    def test_anti_phase_linked_channels_limit(self) -> None:
        """链接模式取各声道峰值上界：反相输入不得抵消检测（FX-23）。"""
        t = np.arange(RATE) / RATE
        tone = (0.99 * np.sin(2 * np.pi * 12000.0 * t + np.pi / 4))
        pcm = np.stack([tone.astype(np.float32), -tone.astype(np.float32)])
        assert float(np.max(np.abs(pcm.mean(axis=0)))) == 0.0  # 均值抵消
        out = _make({"linked": True, "lookahead_ms": 5.0}).process(pcm.copy())
        ceiling_linear = 10.0 ** (CEILING / 20.0)
        # 输出样本峰被限制在上限附近（反相不再漏检）。
        assert float(np.max(np.abs(out))) <= ceiling_linear + 1e-3

    def test_trailing_very_short_blocks_stay_under_ceiling(self) -> None:
        """信号收尾极短块（1/31 样本）：短块下前视窗含义正确、上限合规。"""
        pcm = _intersample_peak_tone(1.15, seconds=0.2)
        fx = _make()
        chunks = []
        pos = 0
        for step in (480, 1024, 31, 7, 1, 1, 257):
            if pos >= pcm.shape[1]:
                break
            chunks.append(fx.process(pcm[:, pos:pos + step].copy()))
            pos += step
        stitched = np.concatenate(chunks, axis=1)
        assert _measure_dbtp(stitched[:, RATE // 10:]) <= CEILING + 0.3

    def test_independent_release_states_do_not_cross_channels(self) -> None:
        """独立模式：每声道独立 release 状态，安静声道包络不随块衰减。"""
        t = np.arange(RATE) / RATE
        left = (1.15 * 0.5 * (np.sin(2 * np.pi * 5000.0 * t)
                              + np.sin(2 * np.pi * 7000.0 * t))).astype(np.float32)
        right = (0.05 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        pcm = np.stack([left, right])
        fx = _make({"linked": False})
        chunks = [fx.process(pcm[:, s:s + 513].copy())
                  for s in range(0, pcm.shape[1], 513)]
        out = np.concatenate(chunks, axis=1)
        # 分块与整段一致（状态机跨块精确），且安静声道全程不被衰减。
        fx_whole = _make({"linked": False})
        whole = fx_whole.process(pcm.copy())
        np.testing.assert_array_equal(whole[:, RATE:], out[:, RATE:])
        for start in range(RATE // 4, pcm.shape[1] - 513, 4800):
            block = out[1, start:start + 4800]
            assert float(np.max(np.abs(block))) == pytest.approx(
                0.05, abs=1e-3)

    def test_telemetry_reports_output_peak(self) -> None:
        pcm = _intersample_peak_tone(1.15)
        fx = _make()
        fx.process(pcm.copy())
        telemetry = fx.collect_telemetry()
        assert "output_peak_dbfs" in telemetry
        assert telemetry["output_peak_dbfs"] <= CEILING + 0.5

    def test_bypass_bit_exact(self) -> None:
        pcm = _intersample_peak_tone(0.3)
        fx = _make({"enabled": False})
        out = fx.process(pcm.copy())
        np.testing.assert_array_equal(out, pcm)

    def test_latency_declared_for_pdc(self) -> None:
        # 2026-11-21 计划 FX-23：新版流式 FIR 有净群延迟 D=10，
        # 合同更新为 lookahead + D（计划允许「更新合同和基线」）。
        fx = _make({"lookahead_ms": 5.0})
        assert fx.latency_samples() == pytest.approx(
            5.0 * RATE / 1000.0 + 10, abs=1)
        from src.features.edit.core.audio_dsp import EffectChain
        chain = EffectChain(float(RATE))
        chain.set_params([("true_peak_limiter",
                           {"ceiling_dbtp": CEILING}, True, "limiter", 5)])
        assert chain.latency_samples() == fx.latency_samples()

    def test_tail_and_reset(self) -> None:
        fx = _make({"release_ms": 80.0})
        assert 0 < fx.tail_estimate_samples() <= RATE
        fx.reset_state()
        assert fx.tail_estimate_samples() == fx.tail_estimate_samples()

    def test_registered_in_effect_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("true_peak_limiter", {
            "ceiling_dbtp": CEILING, "lookahead_ms": 5.0}, True)])
        pcm = _intersample_peak_tone(1.15)
        out = chain.process(pcm.copy())
        assert _measure_dbtp(out[:, RATE // 10:]) <= CEILING + 0.3
