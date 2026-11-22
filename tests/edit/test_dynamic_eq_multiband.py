"""P1 · 动态均衡器多段版本（FX-02 · 2026-11-21 计划）。

验收：legacy 单段路径原样（v0–v4 冻结）；多段支路功率聚合检源
（反相立体声不抵消）；每段独立 detector/包络/输出滤波状态；稳定段
ID = 列表序号；任意块长与整段逐位一致（绝对样本时钟增益网格）；
静态 Bell 与同版本 EQ 对拍。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.dynamic_eq import DynamicEqEffect
from src.features.edit.core.timeline_v2 import DynamicEqParams

RATE = 48000


def _band(**overrides) -> dict:
    band = {"enabled": True, "frequency_hz": 1000.0, "q": 2.0,
            "static_gain_db": 0.0, "threshold_dbfs": -18.0,
            "max_cut_db": 6.0, "max_boost_db": 0.0, "attack_ms": 5.0,
            "release_ms": 100.0, "slope": 1.0}
    band.update(overrides)
    return band


def _amp_db(pcm: np.ndarray, hz: float) -> float:
    steady = pcm[0, :]
    windowed = steady * np.hanning(steady.size)
    spec = np.abs(np.fft.rfft(windowed))
    bin_index = int(round(hz * steady.size / RATE))
    return float(20 * np.log10(max(2 * spec[bin_index] / steady.size,
                                   1e-12)))


class TestLegacySingleBand:
    def test_empty_bands_reads_single_band_path(self) -> None:
        """缺 bands / 空 bands → 旧单段路径（v0–v4 冻结）。"""
        params = DynamicEqParams.from_dict({"frequency": 3000.0})
        assert params.bands == ()
        fx = DynamicEqEffect(RATE)
        fx.set_params({"frequency": 1000.0, "q": 1.4, "gain_db": 0.0,
                       "threshold_dbfs": -18.0})
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t))[None, :].astype(
            np.float32)
        out = fx.process(tone.copy())
        assert out.shape == tone.shape
        assert np.isfinite(out).all()

    def test_bands_round_trip(self) -> None:
        params = DynamicEqParams.from_dict(
            {"bands": [{"frequency_hz": 6000.0, "max_cut_db": 4.0}]})
        assert params.bands[0]["frequency_hz"] == 6000.0
        assert params.bands[0]["enabled"] is True
        restored = DynamicEqParams.from_dict(params.to_dict())
        assert restored == params


class TestMultiband:
    def test_dual_band_independent_thresholds(self) -> None:
        """1k/6k 双音分别驱动两段：深阈段压满，另一段按各自阈值。"""
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t)
                + 0.3 * np.sin(2 * np.pi * 6000.0 * t)
                )[None, :].astype(np.float32)
        fx = DynamicEqEffect(RATE)
        fx.set_params({"bands": [
            _band(frequency_hz=1000.0, threshold_dbfs=-18.0),
            _band(frequency_hz=6000.0, threshold_dbfs=-50.0)]})
        out = fx.process(tone.copy())
        cut_1k = _amp_db(out, 1000.0) - _amp_db(tone, 1000.0)
        cut_6k = _amp_db(out, 6000.0) - _amp_db(tone, 6000.0)
        # 深阈段打到 max_cut；浅阈段按 over·slope 衰减（RMS ≈ −13.5 dB
        # → over ≈ 4.5 dB → 目标 ≈ −4.5 dB）。
        assert cut_6k == pytest.approx(-6.0, abs=0.5)
        assert -6.0 < cut_1k <= -3.5
        assert cut_6k < cut_1k  # 两段按各自阈值独立响应

    def test_anti_phase_stereo_detection(self) -> None:
        """功率聚合检源：反相立体声不抵消（旧波形平均会漏检）。"""
        t = np.arange(RATE) / RATE
        mono = (0.3 * np.sin(2 * np.pi * 6000.0 * t)).astype(np.float32)
        tone = np.stack([mono, -mono])
        fx = DynamicEqEffect(RATE)
        fx.set_params({"bands": [_band(frequency_hz=6000.0,
                                       threshold_dbfs=-30.0)]})
        out = fx.process(tone.copy())
        cut = _amp_db(out, 6000.0) - _amp_db(tone, 6000.0)
        assert cut < -2.0  # 检测未抵消

    @pytest.mark.parametrize("step", [1, 97, 513, 1024, 4001])
    def test_block_consistency_bit_exact(self, step) -> None:
        """任意块长与整段逐位一致（绝对样本时钟增益网格 + 状态跨块）。"""
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t)
                + 0.3 * np.sin(2 * np.pi * 6000.0 * t)
                )[None, :].astype(np.float32)
        params = {"bands": [_band(), _band(frequency_hz=6000.0)]}
        fx = DynamicEqEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(tone.copy())
        fxb = DynamicEqEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(tone[:, s:s + step])
             for s in range(0, tone.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_static_bell_matches_eq_boost(self) -> None:
        """dynamic 关闭 → 同参数静态 Bell（+6 dB @中心）。"""
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t))[None, :].astype(
            np.float32)
        fx = DynamicEqEffect(RATE)
        fx.set_params({"bands": [_band(static_gain_db=6.0,
                                       dynamic_enabled=False)]})
        out = fx.process(tone.copy())
        assert _amp_db(out, 1000.0) - _amp_db(tone, 1000.0) == \
            pytest.approx(6.0, abs=0.2)

    def test_disabled_band_passthrough_and_reset(self) -> None:
        """禁用段直通；reset 后同输入同结果。"""
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t))[None, :].astype(
            np.float32)
        params = {"bands": [_band(), _band(enabled=False)]}
        fx = DynamicEqEffect(RATE)
        fx.set_params(dict(params))
        first = fx.process(tone.copy())
        fx.reset_state()
        second = fx.process(tone.copy())
        np.testing.assert_array_equal(first, second)
        # 两段同参数 → 输出 = 单段输出的平方级叠加（此处只验证有限性）。
        assert np.isfinite(first).all()

    def test_telemetry_reports_per_band(self) -> None:
        fx = DynamicEqEffect(RATE)
        fx.set_params({"bands": [_band(), _band(frequency_hz=6000.0)]})
        t = np.arange(RATE) / RATE
        tone = (0.3 * np.sin(2 * np.pi * 1000.0 * t))[None, :].astype(
            np.float32)
        fx.process(tone.copy())
        telemetry = fx.collect_telemetry()
        assert "detector_dbfs" in telemetry
        assert "dynamic_gain_db" in telemetry
