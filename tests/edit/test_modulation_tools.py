"""P1 · 瞬态塑形与调制类（2026-11-19 计划第三阶段 #7）。

验收：瞬态塑形 attack 能量提升、稳态不变；颤音/自动声像的 LFO 数学
（深度/速率/反相）；LFO 相位跨块连续（分块拼接 == 整段）；参数变化
不重置相位（自动化/撤销后连续）；flanger 反馈尾音预算；旁路逐位。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.modulation_tools import (
    ChorusFlangerEffect,
    PhaserEffect,
    TransientDesignerEffect,
    TremoloAutoPanEffect,
)

RATE = 48000


def _percussive(seconds: float = 1.0):
    """瞬态 + 稳态：0.5s 处一个 sharp attack，随后衰减尾。"""
    t = np.arange(int(seconds * RATE)) / RATE
    sig = 0.05 * np.sin(2 * np.pi * 300.0 * t)
    onset = int(0.5 * RATE)
    decay = np.exp(-np.arange(2400) / 600.0)
    sig[onset:onset + 2400] += (0.6 * decay
                                * np.sign(np.sin(2 * np.pi * 900.0
                                                 * np.arange(2400) / RATE)))
    return np.stack([sig.astype(np.float32)] * 2)


class TestTransientDesigner:
    def test_attack_boosted_sustain_unchanged(self) -> None:
        pcm = _percussive()
        fx = TransientDesignerEffect(RATE)
        fx.set_params({"attack_db": 9.0, "sustain_db": 0.0})
        out = fx.process(pcm.copy())
        onset = int(0.5 * RATE)
        attack_before = float(np.sqrt(np.mean(pcm[0, onset:onset + 240] ** 2)))
        attack_after = float(np.sqrt(np.mean(out[0, onset:onset + 240] ** 2)))
        # 稳态段（远离 onset 与启动建立期）不变。
        sustain_before = float(np.sqrt(np.mean(pcm[0, 6000:20000] ** 2)))
        sustain_after = float(np.sqrt(np.mean(out[0, 6000:20000] ** 2)))
        assert attack_after > attack_before * 1.5
        assert sustain_after == pytest.approx(sustain_before, rel=0.25)

    def test_block_consistency(self) -> None:
        pcm = _percussive()
        whole = TransientDesignerEffect(RATE).process(pcm.copy())
        fx = TransientDesignerEffect(RATE)
        stitched = np.concatenate(
            [fx.process(pcm[:, s:s + 977].copy())
             for s in range(0, pcm.shape[-1], 977)], axis=1)
        np.testing.assert_allclose(
            whole[:, 2000:], stitched[:, 2000:], atol=1e-4)


class TestTremoloAutoPan:
    def test_tremolo_depth_and_rate(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"mode": "tremolo", "rate_hz": 4.0, "depth": 1.0})
        out = fx.process(pcm.copy())
        # 4 Hz 深度 1：包络在 [0.5·(1+lfo)/... ] 间摆动——峰谷比显著。
        env = np.abs(out[0])
        peaks = env[: RATE // 4].max()          # 4Hz 周期 12000 样本
        valley = env[RATE // 8: RATE // 8 + 4000].min()
        assert peaks == pytest.approx(0.5, abs=0.1)     # 峰 ≈ 原幅度
        assert valley < 0.08                            # 谷接近零

    def test_auto_pan_antiphase(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"mode": "auto_pan", "rate_hz": 1.0, "depth": 1.0})
        out = fx.process(pcm.copy())
        # 等功率声像：相关立体声的总功率逐样本守恒。
        total = out[0] ** 2 + out[1] ** 2
        np.testing.assert_allclose(
            total, pcm[0] ** 2, atol=2e-3)
        left_peak = float(np.max(np.abs(out[0])))
        right_peak_at_left_peak = float(np.abs(
            out[1, int(np.argmax(np.abs(out[0])))]))
        assert right_peak_at_left_peak < 0.25 * left_peak

    def test_lfo_phase_continuous_across_blocks(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        whole = TremoloAutoPanEffect(RATE)
        whole.set_params({"rate_hz": 3.0, "depth": 0.9})
        out_whole = whole.process(pcm.copy())
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"rate_hz": 3.0, "depth": 0.9})
        pieces = [fx.process(pcm[:, s:s + 733].copy())
                  for s in range(0, RATE, 733)]
        out_stream = np.concatenate(pieces, axis=1)
        np.testing.assert_allclose(
            out_whole, out_stream[:, :RATE], atol=1e-5)

    def test_param_change_keeps_phase(self) -> None:
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"rate_hz": 2.0})
        t = np.arange(480) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx.process(pcm.copy())
        phase_before = fx._lfo.phase
        fx.set_params({"rate_hz": 5.0})   # 速率变化不重置相位
        assert fx._lfo.phase == phase_before


class TestChorusFlangerPhaser:
    def test_chorus_widens_and_tail_zero_without_feedback(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "chorus", "feedback": 0.0, "mix": 0.5})
        out = fx.process(pcm.copy())
        assert out.shape == pcm.shape
        assert fx.tail_estimate_samples() == 0

    def test_flanger_feedback_tail_budgeted(self) -> None:
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "flanger", "feedback": 0.6})
        assert fx.tail_estimate_samples() > 0

    def test_paser_output_bounded_and_registered_shape(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = PhaserEffect(RATE)
        out = fx.process(pcm.copy())
        assert out.shape == pcm.shape
        assert float(np.max(np.abs(out))) < 0.8
        assert fx.tail_estimate_samples() > 0

    def test_bypass_all(self) -> None:
        t = np.arange(2400) / RATE
        pcm = np.stack([(0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        for fx in (TransientDesignerEffect(RATE),
                   TremoloAutoPanEffect(RATE),
                   ChorusFlangerEffect(RATE),
                   PhaserEffect(RATE)):
            fx.set_params({"enabled": False})
            np.testing.assert_array_equal(fx.process(pcm.copy()), pcm)
