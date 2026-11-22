"""P1 · 颤音/自动声像波形合同（FX-19 · 2026-11-21 计划）。

验收：legacy 正弦路径冻结（位精确）；新波形 triangle/square/saw_up/
saw_down/random 相位连续、绝对样本相位积分跨块位精确；random 按
seed 确定性；rate 变化在切块点相位连续；auto_pan depth 0 → √0.5。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.modulation_tools import (
    TremoloAutoPanEffect,
    _waveform_values,
)
from src.features.edit.core.timeline_v2 import TremoloAutoPanParams

RATE = 48000


def _signal(seconds: float = 0.5) -> np.ndarray:
    rng = np.random.default_rng(8)
    return (rng.uniform(-0.3, 0.3, (2, int(seconds * RATE)))
            ).astype(np.float32)


class TestLegacy:
    def test_sine_path_unchanged(self) -> None:
        """缺 waveform / 'sine' → 旧正弦路径（冻结）。"""
        params = TremoloAutoPanParams.from_dict({"rate_hz": 4.0})
        assert params.waveform == ""
        x = _signal()
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"mode": "tremolo", "rate_hz": 4.0, "depth": 0.7})
        out = fx.process(x.copy())
        lfo = np.sin(2 * np.pi * 4.0 * np.arange(x.shape[1]) / RATE)
        ref = x * (1.0 - 0.7 * (0.5 - 0.5 * lfo))[None, :]
        np.testing.assert_allclose(out, ref.astype(np.float32), atol=1e-6)

    def test_auto_pan_depth_zero_center_gain(self) -> None:
        """auto_pan depth 0 → 两路 √0.5（旧版本中心增益保留）。"""
        x = _signal()
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"mode": "auto_pan", "depth": 0.0,
                       "waveform": "square"})
        out = fx.process(x.copy())
        np.testing.assert_allclose(out[0] / x[0], np.sqrt(0.5), atol=1e-6)
        np.testing.assert_allclose(out[1] / x[1], np.sqrt(0.5), atol=1e-6)


class TestWaveformContract:
    def test_waveform_value_functions(self) -> None:
        frac = np.array([0.0, 0.25, 0.5, 0.75])
        np.testing.assert_allclose(
            _waveform_values("triangle", frac, np.zeros(4), 0),
            [-1.0, 0.0, 1.0, 0.0])
        np.testing.assert_allclose(
            _waveform_values("square", frac, np.zeros(4), 0),
            [1.0, 1.0, -1.0, -1.0])
        np.testing.assert_allclose(
            _waveform_values("saw_up", frac, np.zeros(4), 0),
            [-1.0, -0.5, 0.0, 0.5])
        np.testing.assert_allclose(
            _waveform_values("saw_down", frac, np.zeros(4), 0),
            [1.0, 0.5, 0.0, -0.5])

    def test_unknown_waveform_falls_back_sine(self) -> None:
        frac = np.array([0.0, 0.25])
        np.testing.assert_allclose(
            _waveform_values("mystery", frac, np.zeros(2), 0),
            np.sin(2 * np.pi * frac))

    def test_random_deterministic_by_seed(self) -> None:
        frac = np.array([0.3, 0.7, 0.9, 1.2 % 1.0])
        cycle = np.array([0, 0, 0, 1])
        a = _waveform_values("random", frac, cycle, 42)
        b = _waveform_values("random", frac, cycle, 42)
        c = _waveform_values("random", frac, cycle, 43)
        np.testing.assert_array_equal(a, b)
        assert not np.array_equal(a, c)
        # 同周期内 S&H：值恒定。
        assert a[0] == a[1] == a[2]


class TestChunkInvariance:
    @pytest.mark.parametrize("waveform", [
        "triangle", "square", "saw_up", "saw_down", "random"])
    def test_bit_exact_across_blocks(self, waveform) -> None:
        x = _signal(0.5)
        params = {"mode": "tremolo", "rate_hz": 3.0, "depth": 0.8,
                  "waveform": waveform, "random_seed": 42}
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(x.copy())
        fxb = TremoloAutoPanEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(x[:, s:s + 513])
             for s in range(0, x.shape[1], 513)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_rate_change_at_chunk_boundary(self) -> None:
        """rate 变化点切块：相位连续，两次运行一致。"""
        x = _signal(0.5)
        fx = TremoloAutoPanEffect(RATE)
        fx.set_params({"mode": "tremolo", "rate_hz": 2.0, "depth": 0.8,
                       "waveform": "triangle"})
        first = fx.process(x[:, :x.shape[1] // 2].copy())
        fx.set_params({"rate_hz": 3.0})
        second = fx.process(x[:, x.shape[1] // 2:].copy())
        combined_a = np.concatenate([first, second], axis=1)

        fx2 = TremoloAutoPanEffect(RATE)
        fx2.set_params({"mode": "tremolo", "rate_hz": 2.0, "depth": 0.8,
                        "waveform": "triangle"})
        first_b = fx2.process(x[:, :x.shape[1] // 2].copy())
        fx2.set_params({"rate_hz": 3.0})
        second_b = fx2.process(x[:, x.shape[1] // 2:].copy())
        combined_b = np.concatenate([first_b, second_b], axis=1)
        np.testing.assert_array_equal(combined_a, combined_b)
