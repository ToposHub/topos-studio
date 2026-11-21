"""P1 · 齿音消除新版支路（FX-06 · 2026-11-21 计划）。

验收：逐声道能量联动检源（反相立体声不抵消）；Wide 两段同降；
Split 带外仅输出增益；lookahead 声明延迟与脉冲对齐（2/5 ms →
96/240 样本）；detector_shape bandpass/high_shelf；output_gain_db；
分块逐位一致；legacy 参数组合走旧路径（自身分块一致冻结）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.de_esser import DeEsserEffect

RATE = 48000


def _sibilance_fixture() -> np.ndarray:
    """6.5 kHz 齿音 + 1 kHz 元音参考。"""
    t = np.arange(2 * RATE) / RATE
    sibilance = (0.3 * np.sin(2 * np.pi * 6500.0 * t)).astype(np.float32)
    voice = (0.2 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    return (voice + sibilance * 0.8)[None, :].astype(np.float32)


def _amp_db(pcm: np.ndarray, hz: float) -> float:
    windowed = pcm[0, RATE:] * np.hanning(RATE)
    return float(20 * np.log10(
        max(2 * abs(np.fft.rfft(windowed)[hz]) / RATE, 1e-12)))


V2 = {"detector_shape": "bandpass", "lookahead_ms": 0.0,
      "output_gain_db": 0.0, "threshold_dbfs": -30.0,
      "max_reduction_db": 12.0, "mode": "wide"}


class TestNewPath:
    def test_anti_phase_stereo_detected(self) -> None:
        """能量联动检源：反相立体声齿音仍被抑制。"""
        sig = _sibilance_fixture()
        anti = np.stack([sig[0], -sig[0]])
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2))
        out = fx.process(anti.copy())
        cut = _amp_db(out, 6500) - _amp_db(anti, 6500)
        assert cut < -5.0

    def test_wide_reduces_both_bands(self) -> None:
        """Wide：全带同降（齿音与参考同比例）。"""
        sig = _sibilance_fixture()
        anti = np.stack([sig[0], -sig[0]])
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2))
        out = fx.process(anti.copy())
        cut_sib = _amp_db(out, 6500) - _amp_db(anti, 6500)
        cut_ref = _amp_db(out, 1000) - _amp_db(anti, 1000)
        assert cut_sib == pytest.approx(cut_ref, abs=0.5)

    def test_split_preserves_low_band(self) -> None:
        """Split：带外仅受 output_gain_db 影响（低频变化 ≤0.5 dB 于增益）。"""
        sig = _sibilance_fixture()
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2, mode="split", output_gain_db=-3.0))
        out = fx.process(sig.copy())
        low_change = _amp_db(out, 1000) - _amp_db(sig, 1000)
        assert low_change == pytest.approx(-3.0, abs=0.5)

    @pytest.mark.parametrize("ms,expected", [(0.0, 0), (2.0, 96), (5.0, 240)])
    def test_lookahead_latency_alignment(self, ms, expected) -> None:
        """前视声明延迟 = 实测 onset（48 kHz：2/5 ms → 96/240 样本）。"""
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2, lookahead_ms=ms, threshold_dbfs=0.0,
                           max_reduction_db=0.0))
        assert fx.latency_samples() == expected
        pulse = np.zeros((2, 480), dtype=np.float32)
        pulse[:, 0] = 1.0
        out = fx.process(pulse)
        nonzero = np.flatnonzero(np.abs(out[0]) > 1e-6)
        assert nonzero[0] == expected

    def test_high_shelf_detector_shape(self) -> None:
        """high_shelf 检测形状可用并受 max_reduction 限制。"""
        sig = _sibilance_fixture()
        anti = np.stack([sig[0], -sig[0]])
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2, detector_shape="high_shelf"))
        out = fx.process(anti.copy())
        cut = _amp_db(out, 6500) - _amp_db(anti, 6500)
        assert -12.5 <= cut <= -6.0

    def test_output_gain_db(self) -> None:
        sig = _sibilance_fixture()
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(V2, output_gain_db=-6.0, threshold_dbfs=0.0,
                           max_reduction_db=0.0))
        out = fx.process(sig.copy())
        change = _amp_db(out, 1000) - _amp_db(sig, 1000)
        assert change == pytest.approx(-6.0, abs=0.2)

    @pytest.mark.parametrize("step", [513, 1024])
    def test_block_consistency_bit_exact(self, step) -> None:
        sig = _sibilance_fixture()
        params = dict(V2, lookahead_ms=2.0, output_gain_db=-3.0,
                      mode="split")
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(sig.copy())
        fxb = DeEsserEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(sig[:, s:s + step])
             for s in range(0, sig.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)


class TestLegacyFrozen:
    def test_legacy_path_unchanged(self) -> None:
        """legacy 参数组合（无新键）走旧路径且自身分块一致。"""
        sig = _sibilance_fixture()
        legacy = {"threshold_dbfs": -32.0, "ratio": 4.0, "frequency": 6500.0,
                  "q": 2.0, "attack_ms": 5.0, "release_ms": 60.0,
                  "max_reduction_db": 12.0, "mode": "split",
                  "listen": False, "enabled": True}
        fx = DeEsserEffect(RATE)
        fx.set_params(dict(legacy))
        assert not fx._uses_v2()
        whole = fx.process(sig.copy())
        fxb = DeEsserEffect(RATE)
        fxb.set_params(dict(legacy))
        stitched = np.concatenate(
            [fxb.process(sig[:, s:s + 513])
             for s in range(0, sig.shape[1], 513)], axis=1)
        np.testing.assert_array_equal(whole, stitched)
