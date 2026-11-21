"""P2 · 次谐波发生器（FX-16 · 2026-11-21 计划）。

验收：120 Hz 单音锁定后 60/30 Hz 两分量精确（增益 = dB 线性）；
锁相前静音（生成支路门控）；反相立体声经能量选道仍锁定；分块位
精确；无 F0 内容（噪声/静音）不误触发；telemetry 报告锁定状态。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    ALGORITHM_VERSION,
    EffectChain,
)
from src.features.edit.core.audio_effects.subharmonic import (
    estimate_subharmonic_f0,
)
from src.features.edit.core.timeline_v2 import SubharmonicParams

RATE = 48000


def _chain(params: SubharmonicParams) -> EffectChain:
    chain = EffectChain(RATE)
    chain.set_params([("subharmonic", params.to_dict(), True, "sh",
                       ALGORITHM_VERSION)], sample_rate=RATE)
    return chain


class TestDetector:
    def test_known_tone(self) -> None:
        t = np.arange(4800) / RATE
        assert estimate_subharmonic_f0(
            0.3 * np.sin(2 * np.pi * 120.0 * t), RATE) == pytest.approx(
                120.0, abs=2.0)

    def test_silent_rejected(self) -> None:
        assert estimate_subharmonic_f0(np.zeros(4800), RATE) is None


class TestSynthesis:
    def test_octave_gains_exact(self) -> None:
        """锁定后 60/30 Hz 分量 = dB 增益线性值（−3/−9 dB）。"""
        t = np.arange(3 * RATE) / RATE
        src = (0.3 * np.sin(2 * np.pi * 120.0 * t))[None, :].astype(
            np.float32)
        params = SubharmonicParams.from_dict({
            "sh_mix": 1.0, "sh_octave1_gain_db": -3.0,
            "sh_octave2_gain_db": -9.0, "sh_sub_lpf_hz": 400.0})
        out = _chain(params).process(src.copy())
        steady = out[0, int(1.5 * RATE):]
        n = steady.size

        def amp(hz):
            return float(2 * abs(np.fft.rfft(steady)[int(round(hz * n / RATE))])
                         / n)

        assert amp(60) == pytest.approx(10 ** (-3.0 / 20.0), abs=0.02)
        assert amp(30) == pytest.approx(10 ** (-9.0 / 20.0), abs=0.02)
        assert amp(120) < 0.05  # mix=1：干声衰减

    def test_anti_phase_stereo_locks(self) -> None:
        """能量选道：反相立体声仍锁定（检测不抵消）。"""
        t = np.arange(3 * RATE) / RATE
        mono = (0.3 * np.sin(2 * np.pi * 120.0 * t)).astype(np.float32)
        src = np.stack([mono, -mono])
        params = SubharmonicParams.from_dict({
            "sh_mix": 1.0, "sh_octave1_gain_db": 0.0,
            "sh_octave2_gain_db": -36.0, "sh_sub_lpf_hz": 400.0})
        out = _chain(params).process(src)
        steady = out[0, int(1.5 * RATE):]
        n = steady.size
        amp60 = float(2 * abs(np.fft.rfft(steady)[int(round(60 * n / RATE))])
                      / n)
        assert amp60 == pytest.approx(1.0, abs=0.05)

    def test_noise_does_not_lock(self) -> None:
        """宽带噪声不锁定（生成支路保持静音，mix=1）。"""
        rng = np.random.default_rng(3)
        src = (rng.standard_normal((2, 3 * RATE)) * 0.3).astype(np.float32)
        params = SubharmonicParams.from_dict({
            "sh_mix": 1.0, "sh_octave1_gain_db": 0.0,
            "sh_octave2_gain_db": -36.0, "sh_sub_lpf_hz": 400.0})
        out = _chain(params).process(src)
        tail = out[0, int(2 * RATE):]
        assert float(np.max(np.abs(tail))) < 1e-4

    def test_mix_zero_passthrough(self) -> None:
        rng = np.random.default_rng(5)
        x = (rng.uniform(-0.2, 0.2, (2, 4000))).astype(np.float32)
        params = SubharmonicParams.from_dict({
            "sh_mix": 0.0, "sh_octave1_gain_db": 0.0})
        out = _chain(params).process(x.copy())
        # mix=0：干声直通（生成支路静音且未被混入）。
        np.testing.assert_allclose(out, x, rtol=0, atol=1e-6)

    @pytest.mark.parametrize("step", [513, 1024])
    def test_block_consistency_bit_exact(self, step) -> None:
        t = np.arange(RATE) / RATE
        src = (0.3 * np.sin(2 * np.pi * 120.0 * t))[None, :].astype(
            np.float32)
        params = SubharmonicParams.from_dict({
            "sh_mix": 1.0, "sh_octave1_gain_db": -6.0,
            "sh_sub_lpf_hz": 400.0})
        whole = _chain(params).process(src.copy())
        fxb = _chain(params)
        stitched = np.concatenate(
            [fxb.process(src[:, s:s + step])
             for s in range(0, src.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_param_round_trip(self) -> None:
        params = SubharmonicParams.from_dict({"sh_mix": 0.7,
                                              "sh_sub_lpf_hz": 150.0})
        restored = SubharmonicParams.from_dict(params.to_dict())
        assert restored == params
