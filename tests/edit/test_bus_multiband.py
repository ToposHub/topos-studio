"""P1 · Bus / Multiband Compressor（2026-11-19 计划第三阶段 #5）。

验收：等增益时合成响应平坦（LR4 和为全通）；跨带独立 envelope
（低段压缩不影响安静高段）；分块一致；旁路逐位；Bus 模板参数与
检源高通；链注册。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.bus_multiband import (
    BUS_COMP_TEMPLATES,
    BusCompEffect,
    MultibandCompressorEffect,
    _LR4Section,
)

RATE = 48000


def _tone(freq: float, amp: float, seconds: float = 1.5):
    t = np.arange(int(seconds * RATE)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestLR4Crossover:
    def test_flat_sum_allpass(self) -> None:
        """等增益合成：low+high ≈ 原信号（LR4 全通和）。"""
        rng = np.random.default_rng(5)
        sig = (0.4 * rng.standard_normal(RATE)).astype(np.float32)
        pcm = np.stack([sig, sig])
        section = _LR4Section(180.0, RATE, 2)
        low, high = section.split(pcm)
        summed = low + high
        # 跳过滤波器建立期。
        steady = slice(2000, None)
        np.testing.assert_allclose(
            summed[:, steady], pcm[:, steady], atol=2e-3)

    def test_block_consistency(self) -> None:
        rng = np.random.default_rng(6)
        pcm = np.stack(
            [(0.3 * rng.standard_normal(RATE)).astype(np.float32)] * 2)
        section = _LR4Section(180.0, RATE, 2)
        low_a, high_a = section.split(pcm)
        section2 = _LR4Section(180.0, RATE, 2)
        lows, highs = [], []
        for start in range(0, RATE, 7771):
            lo, hi = section2.split(pcm[:, start:start + 7771])
            lows.append(lo)
            highs.append(hi)
        np.testing.assert_allclose(
            low_a[:, 2000:], np.concatenate(lows, axis=1)[:, 2000:],
            atol=1e-4)


class TestMultibandCompressor:
    def test_loud_low_band_compressed_quiet_high_untouched(self) -> None:
        low = _tone(80.0, 0.6)
        high = _tone(9000.0, 0.05)
        pcm = np.stack([low + high] * 2)
        fx = MultibandCompressorEffect(RATE)
        fx.set_params({
            "crossovers": [400.0, 6000.0],
            "bands": [
                {"threshold_db": -14.0, "ratio": 4.0, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
                {"threshold_db": 0.0, "ratio": 1.5, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
                {"threshold_db": 0.0, "ratio": 1.5, "attack_ms": 5.0,
                 "release_ms": 120.0, "mix": 100.0, "makeup_db": 0.0},
            ]})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)

        def band_amp(x, freq, bw=120.0):
            spec = np.abs(np.fft.rfft(x[0, steady] * np.hanning(
                x.shape[-1] - RATE // 2)))
            freqs = np.fft.rfftfreq(x.shape[-1] - RATE // 2, 1.0 / RATE)
            mask = np.abs(freqs - freq) <= bw
            return float(np.max(spec[mask]))

        low_cut = 20.0 * np.log10(band_amp(pcm, 80.0)
                                  / band_amp(out, 80.0))
        high_delta = abs(20.0 * np.log10(
            band_amp(out, 9000.0) / band_amp(pcm, 9000.0)))
        assert low_cut > 3.0          # 低段压缩
        assert high_delta < 1.5       # 高段（1.0001 比率）基本不动

    def test_bypass_bit_exact(self) -> None:
        pcm = np.stack([_tone(500.0, 0.2)] * 2)
        fx = MultibandCompressorEffect(RATE)
        fx.set_params({"enabled": False})
        np.testing.assert_array_equal(fx.process(pcm.copy()), pcm)

    def test_registered_in_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("multiband_comp", {"crossovers": [200.0, 5000.0]},
                           True)])
        pcm = np.stack([_tone(80.0, 0.5)] * 2)
        out = chain.process(pcm.copy())
        assert out.shape == pcm.shape
        assert float(np.max(np.abs(out))) > 0.05


class TestBusComp:
    def test_glue_templates_have_distinct_params(self) -> None:
        dx = BUS_COMP_TEMPLATES["dx_glue"]
        mx = BUS_COMP_TEMPLATES["mx_glue"]
        assert dx["ratio"] != mx["ratio"]
        assert dx["release_ms"] < mx["release_ms"]

    def test_sc_highpass_reduces_bass_triggering(self) -> None:
        """检源高通：低频鼓组能量不触发 ducking（对比无高通）。"""
        bass = _tone(50.0, 0.7)
        pcm = np.stack([bass] * 2)
        with_hpf = BusCompEffect(RATE)
        with_hpf.set_params({"template": "dx_glue", "sc_hpf_hz": 150.0})
        out_hpf = with_hpf.process(pcm.copy())
        no_hpf = BusCompEffect(RATE)
        no_hpf.set_params({"template": "dx_glue", "sc_hpf_hz": 0.0})
        out_flat = no_hpf.process(pcm.copy())
        steady = slice(RATE // 3, None)
        hpf_level = float(np.sqrt(np.mean(out_hpf[:, steady] ** 2)))
        flat_level = float(np.sqrt(np.mean(out_flat[:, steady] ** 2)))
        # 无高通时低频触发压缩（输出更低）；有高通则基本不受影响。
        assert flat_level < hpf_level

    def test_registered_in_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("bus_comp", {"template": "mx_glue"}, True)])
        pcm = np.stack([_tone(440.0, 0.5)] * 2)
        out = chain.process(pcm.copy())
        assert out.shape == pcm.shape
