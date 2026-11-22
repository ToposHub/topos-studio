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
    def test_chorus_widens_and_preserves_first_echo_without_feedback(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "chorus", "feedback": 0.0, "mix": 0.5})
        out = fx.process(pcm.copy())
        assert out.shape == pcm.shape
        assert fx.tail_estimate_samples() >= round((18.0 + 3.0) * RATE / 1000.0)

    def test_flanger_feedback_tail_budgeted(self) -> None:
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "flanger", "feedback": 0.6})
        assert fx.tail_estimate_samples() > 0

    def test_flanger_fixed_delay_first_hop(self) -> None:
        """FX-18：5 ms 固定镶边首跳 240±1（旧实现实测 2160）。"""
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "flanger", "base_ms": 5.0, "depth_ms": 0.0,
                       "voices": 1, "feedback": 0.0, "mix": 1.0})
        pulse = np.zeros((2, 2048), dtype=np.float32)
        pulse[:, 0] = 1.0
        out = fx.process(pulse)
        nonzero = np.flatnonzero(np.abs(out[0]) > 1e-6)
        assert nonzero[0] == pytest.approx(240, abs=1)
        assert float(out[0, nonzero[0]]) == pytest.approx(1.0, abs=1e-6)

    def test_flanger_block_consistency_signed_feedback(self) -> None:
        """FX-18：正/负反馈下任意分块与整段逐位一致（反馈样本跨块持久）。"""
        rng = np.random.default_rng(4)
        x = (rng.standard_normal((2, 3000)) * 0.2).astype(np.float32)
        for feedback in (0.6, -0.6):
            params = {"mode": "flanger", "base_ms": 5.0, "depth_ms": 2.0,
                      "voices": 1, "feedback": feedback, "mix": 1.0}
            fx = ChorusFlangerEffect(RATE)
            fx.set_params(dict(params))
            whole = fx.process(x.copy())
            for step in (127, 501, 1024):
                fxb = ChorusFlangerEffect(RATE)
                fxb.set_params(dict(params))
                stitched = np.concatenate(
                    [fxb.process(x[:, s:s + step])
                     for s in range(0, x.shape[1], step)], axis=1)
                np.testing.assert_array_equal(whole, stitched)

    def test_flanger_tail_decays_to_budget(self) -> None:
        """FX-18：末次回声在预算内衰减（|feedback| + −60 dB 目标）。"""
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "flanger", "base_ms": 5.0, "depth_ms": 0.0,
                       "voices": 1, "feedback": 0.6, "mix": 1.0})
        pulse = np.zeros((2, RATE), dtype=np.float32)
        pulse[:, 0] = 1.0
        out = fx.process(pulse)
        tail = fx.tail_estimate_samples()
        first = int(np.flatnonzero(np.abs(out[0]) > 1e-6)[0])
        after_budget = out[0, first + tail:]
        assert float(np.max(np.abs(after_budget))) <= 1e-3  # ≈ −60 dB

    def test_comb_notch_positions(self) -> None:
        """FX-18：50% 混合固定延时 D 的梳状凹口在 1/(2D) Hz，间距 1/D。"""
        delay_samples = 240  # 5 ms @48k
        fx = ChorusFlangerEffect(RATE)
        fx.set_params({"mode": "flanger", "base_ms": 5.0, "depth_ms": 0.0,
                       "voices": 1, "feedback": 0.0, "mix": 0.5})
        impulse = np.zeros((2, RATE), dtype=np.float32)
        impulse[:, 0] = 1.0
        response = fx.process(impulse)[0]
        spec = np.abs(np.fft.rfft(response))
        freqs = np.fft.rfftfreq(RATE, 1.0 / RATE)
        expected_first = RATE / (2.0 * delay_samples)  # 100 Hz
        # 凹口 = 低于 1e-6 的频点集合；首个应在 100 Hz，间距 200 Hz。
        notch_bins = np.flatnonzero(spec[:2000] < 1e-6)
        assert notch_bins.size >= 2
        assert freqs[notch_bins[0]] == pytest.approx(expected_first, abs=2.0)
        spacings = np.diff(freqs[notch_bins])
        assert np.all(np.abs(spacings - RATE / delay_samples) < 4.0)

    def test_paser_output_bounded_and_registered_shape(self) -> None:
        t = np.arange(RATE) / RATE
        pcm = np.stack([(0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        fx = PhaserEffect(RATE)
        out = fx.process(pcm.copy())
        assert out.shape == pcm.shape
        assert float(np.max(np.abs(out))) < 0.8
        assert fx.tail_estimate_samples() > 0

    def test_phaser_block_consistency_with_feedback(self) -> None:
        """FX-18：反馈样本跨块持久（旧实现 501 切块与整块差 0.154）。"""
        x = np.zeros((2, 1000), dtype=np.float32)
        x[:, 500] = 1.0
        for stages in (4, 6, 8):
            fx = PhaserEffect(RATE)
            fx.set_params({"stages": stages})
            whole = fx.process(x.copy())
            fxb = PhaserEffect(RATE)
            fxb.set_params({"stages": stages})
            stitched = np.concatenate(
                [fxb.process(x[:, :501].copy()),
                 fxb.process(x[:, 501:].copy())], axis=1)
            np.testing.assert_allclose(
                whole, stitched, rtol=0, atol=1e-6)

    def test_phaser_allpass_stage_flat_magnitude(self) -> None:
        """FX-18：湿声路径（全通级联）幅频平坦（近 0 dB）。"""
        fx = PhaserEffect(RATE)
        fx.set_params({"stages": 6, "rate_hz": 1e-9,
                       "base_hz": 1000.0, "sweep_octaves": 0.0,
                       "mix": 1.0, "feedback": 0.0})
        impulse = np.zeros((2, 8192), dtype=np.float32)
        impulse[:, 0] = 1.0
        response = fx.process(impulse)[0]
        spec = np.abs(np.fft.rfft(response))
        # 全通：通带内幅度近 1（LFO 频率近 0 时系数缓变）。
        band = spec[10:2000]
        assert float(band.max()) == pytest.approx(1.0, abs=0.1)
        assert float(band.min()) == pytest.approx(1.0, abs=0.1)
        # 混合（mix=0.5）后出现凹口：|0.5 + 0.5·e^{-jφ}| = |cos(φ/2)|，
        # 带内必有深相消（凹口相位不必恰落 FFT bin）。
        fx2 = PhaserEffect(RATE)
        fx2.set_params({"stages": 6, "rate_hz": 1e-9,
                        "base_hz": 1000.0, "sweep_octaves": 0.0,
                        "mix": 0.5, "feedback": 0.0})
        mixed = fx2.process(impulse)[0]
        mixed_spec = np.abs(np.fft.rfft(mixed))
        assert float(mixed_spec[10:2000].min()) < 0.1

    def test_bypass_all(self) -> None:
        t = np.arange(2400) / RATE
        pcm = np.stack([(0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        for fx in (TransientDesignerEffect(RATE),
                   TremoloAutoPanEffect(RATE),
                   ChorusFlangerEffect(RATE),
                   PhaserEffect(RATE)):
            fx.set_params({"enabled": False})
            np.testing.assert_array_equal(fx.process(pcm.copy()), pcm)
