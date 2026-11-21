"""P1 · M/S 立体声成像（FX-13 · 2026-11-21 计划）。

验收：新字段全中性 → 旧快速路径位精确；Width 0/100/200 → Side
0×/1×/2× 且 Mid 不变；Mono Maker 衰减低频 Side（低频 mono）并保留
高频；分块逐位一致；M/S/输出增益按 dB 生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import ImagerEffect
from src.features.edit.core.timeline_v2 import ImagerParams

RATE = 48000


def _stereo_fixture() -> tuple[np.ndarray, float, float]:
    """纯 Mid（1 kHz）+ 纯 Side（2 kHz）的立体声信号。"""
    t = np.arange(RATE) / RATE
    mid = (0.3 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
    side = (0.3 * np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)
    stereo = np.stack([(mid + side) * 0.5, (mid - side) * 0.5])
    return stereo, 1000.0, 2000.0


def _amp_db(pcm: np.ndarray, ch: int, hz: float) -> float:
    windowed = pcm[ch] * np.hanning(pcm.shape[1])
    spec = np.abs(np.fft.rfft(windowed))
    bin_index = int(round(hz * pcm.shape[1] / RATE))
    return float(20 * np.log10(max(2 * spec[bin_index] / pcm.shape[1],
                                   1e-12)))


class TestLegacyCompat:
    def test_neutral_fields_use_legacy_fast_path(self) -> None:
        """新字段全中性 → 旧快速路径（位精确兼容基准）。"""
        params = ImagerParams.from_dict({"pan": 25.0, "width": 150.0})
        assert params.mid_gain_db == 0.0 and not params.mono_maker_on
        rng = np.random.default_rng(7)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        fx = ImagerEffect(RATE)
        fx.set_params(params.to_dict())
        assert fx._legacy_fast_path()
        out = fx.process(x.copy())
        # 旧实现数学：M/S width + linear pan。
        pan_norm = 0.25   # 偏右：L=1−0.25, R=1.0
        side_gain = 1.5
        m = (x[0] + x[1]) * 0.5
        s = (x[0] - x[1]) * 0.5
        l = (m + s * side_gain) * (1.0 - pan_norm)
        r = (m - s * side_gain) * 1.0
        np.testing.assert_allclose(out[0], l, rtol=0, atol=1e-6)
        np.testing.assert_allclose(out[1], r, rtol=0, atol=1e-6)

    def test_slope_enum_validated(self) -> None:
        with pytest.raises(ValueError):
            ImagerParams(mono_maker_slope="18")


class TestWidthContract:
    @pytest.mark.parametrize("width,expected_side_db", [
        (0.0, -60.0), (100.0, 0.0), (200.0, 6.0)])
    def test_width_scales_side_only(self, width, expected_side_db) -> None:
        """Width 0/100/200 → Side 0×/1×/2×，Mid 不变（计划验收）。"""
        stereo, mid_hz, side_hz = _stereo_fixture()
        fx = ImagerEffect(RATE)
        fx.set_params({"width": width, "enabled": True})
        out = fx.process(stereo.copy())
        assert _amp_db(out, 0, mid_hz) - _amp_db(stereo, 0, mid_hz) == \
            pytest.approx(0.0, abs=0.05)
        side_change = _amp_db(out, 0, side_hz) - _amp_db(stereo, 0, side_hz)
        if width == 0.0:
            assert side_change <= -60.0  # 完全 mono
        else:
            assert side_change == pytest.approx(expected_side_db, abs=0.5)


class TestMonoMaker:
    def test_low_side_attenuated_high_preserved(self) -> None:
        """Mono Maker：低频 Side 衰减（低频 mono），高频保留。"""
        t = np.arange(RATE) / RATE
        mid = (0.3 * np.sin(2 * np.pi * 1000.0 * t)).astype(np.float32)
        low_side = (0.2 * np.sin(2 * np.pi * 20.0 * t)).astype(np.float32)
        high_side = (0.2 * np.sin(2 * np.pi * 2000.0 * t)).astype(np.float32)
        stereo = np.stack([(mid + low_side + high_side) * 0.5,
                           (mid - low_side - high_side) * 0.5])
        fx = ImagerEffect(RATE)
        fx.set_params({"mono_maker_on": True, "mono_maker_hz": 200.0,
                       "enabled": True})
        out = fx.process(stereo.copy())
        low_removed = _amp_db(out, 0, 20.0) - _amp_db(stereo, 0, 20.0)
        high_kept = _amp_db(out, 0, 2000.0) - _amp_db(stereo, 0, 2000.0)
        assert low_removed < -20.0   # 200 Hz 一阶高通在 20 Hz 处 ≥ 20 dB
        assert abs(high_kept) < 0.5

    @pytest.mark.parametrize("step", [513, 1024, 4001])
    def test_block_consistency_bit_exact(self, step) -> None:
        stereo, _, _ = _stereo_fixture()
        params = {"mono_maker_on": True, "mono_maker_hz": 200.0,
                  "mono_maker_slope": "12", "enabled": True}
        fx = ImagerEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(stereo.copy())
        fxb = ImagerEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(stereo[:, s:s + step])
             for s in range(0, stereo.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_reset_state_clears_filter(self) -> None:
        stereo, _, _ = _stereo_fixture()
        params = {"mono_maker_on": True, "mono_maker_hz": 200.0,
                  "enabled": True}
        fx = ImagerEffect(RATE)
        fx.set_params(dict(params))
        first = fx.process(stereo.copy())
        fx.reset_state()
        second = fx.process(stereo.copy())
        np.testing.assert_array_equal(first, second)


class TestGains:
    def test_mid_side_output_gains(self) -> None:
        """M/S/输出增益按 dB 生效。"""
        stereo, mid_hz, side_hz = _stereo_fixture()
        fx = ImagerEffect(RATE)
        fx.set_params({"mid_gain_db": -6.0, "side_gain_db": 6.0,
                       "output_gain_db": -6.0, "enabled": True})
        out = fx.process(stereo.copy())
        mid_change = _amp_db(out, 0, mid_hz) - _amp_db(stereo, 0, mid_hz)
        side_change = _amp_db(out, 0, side_hz) - _amp_db(stereo, 0, side_hz)
        assert mid_change == pytest.approx(-12.0, abs=0.2)   # −6 −6 输出
        assert side_change == pytest.approx(0.0, abs=0.2)    # +6 −6 输出

    def test_multichannel_others_untouched(self) -> None:
        """多声道：前两声道处理，其余原样。"""
        rng = np.random.default_rng(2)
        x = (rng.uniform(-0.2, 0.2, (4, 2000))).astype(np.float32)
        fx = ImagerEffect(RATE)
        fx.set_params({"width": 180.0, "mono_maker_on": True,
                       "mono_maker_hz": 300.0, "enabled": True})
        out = fx.process(x.copy())
        np.testing.assert_array_equal(out[2:], x[2:])
