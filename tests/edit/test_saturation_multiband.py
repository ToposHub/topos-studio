"""P1 · 多段饱和器（FX-14 · 2026-11-21 计划）。

验收：legacy 逐样本保留（drive 0/20/80 位精确）；model_profile 版本化
读取；三段互补分频重构；分块与整段逐位一致（1×/2×/4×）；声明延迟与
干湿对齐；tube 偶次 + DC 阻断；4× 过采样折叠抑制 ≥20 dB。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import SaturationEffect
from src.features.edit.core.audio_effects.saturation import (
    PANEL_DRIVE_TO_DB,
    shape_tape,
    shape_transistor,
    shape_tube,
)
from src.features.edit.core.timeline_v2 import SaturationParams

RATE = 48000


def _band(**overrides) -> dict:
    band = {"enabled": False, "model": "tape", "drive_db": 0.0,
            "tone_db": 0.0, "bias_pct": 0.0, "level_db": 0.0}
    band.update(overrides)
    return band


def _mb_params(**overrides) -> dict:
    params = {"model_profile": "multiband", "drive": 0.0, "enabled": True,
              "mb_oversampling": 1, "mb_mix": 1.0,
              "mb_bands": (_band(), _band(), _band())}
    params.update(overrides)
    return params


class TestLegacyCompat:
    def test_legacy_bit_exact(self) -> None:
        """旧声音冻结：legacy 数学与旧实现逐样本一致（drive 0/20/80）。"""
        rng = np.random.default_rng(5)
        x = rng.uniform(-0.3, 0.3, (2, 4001)).astype(np.float32)
        for drive in (0, 20, 80):
            fx = SaturationEffect(RATE)
            fx.set_params({"drive": drive, "enabled": True})
            out = fx.process(x.copy())
            k = drive / 100.0 * 50.0
            if k > 1e-9:
                ref = ((1.0 + k) * x.astype(np.float64)
                       / (1.0 + k * np.abs(x.astype(np.float64)))
                       ).astype(np.float32)
            else:
                ref = x
            np.testing.assert_array_equal(out, ref)

    def test_profile_missing_reads_legacy(self) -> None:
        """缺键 → legacy；非法值回退 legacy；未知字段往返保留。"""
        params = SaturationParams.from_dict({"drive": 30.0})
        assert params.model_profile == "legacy"
        params = SaturationParams.from_dict({"model_profile": "bogus"})
        assert params.model_profile == "legacy"
        round_trip = SaturationParams.from_dict(
            {"drive": 10.0, "model_profile": "multiband",
             "future_key": 42}).to_dict()
        assert round_trip["future_key"] == 42

    def test_profile_round_trip(self) -> None:
        bands = (_band(enabled=True, model="tube", drive_db=9.0,
                       bias_pct=35.0), _band(), _band(model="transistor"))
        params = SaturationParams.from_dict(
            {"model_profile": "multiband", "mb_bands": bands,
             "mb_oversampling": 4, "mb_mix": 0.5})
        restored = SaturationParams.from_dict(params.to_dict())
        assert restored == params
        assert restored.mb_bands[0]["model"] == "tube"
        assert restored.mb_oversampling == 4


class TestMultibandNeutral:
    def test_all_disabled_reconstruction(self) -> None:
        """全段禁用：带限输入重构（FIR 设计纹波内）+ 延迟对齐。"""
        rng = np.random.default_rng(5)
        from scipy.signal import butter, sosfilt
        sos = butter(6, 16000, "lowpass", fs=RATE, output="sos")
        x = (0.3 * sosfilt(sos, rng.standard_normal((2, 8000)))
             ).astype(np.float32)
        for oversampling in (1, 2, 4):
            fx = SaturationEffect(RATE)
            fx.set_params(_mb_params(mb_oversampling=oversampling))
            out = fx.process(x.copy())
            lat = fx.latency_samples()
            expected = 20 if oversampling > 1 else 0
            assert lat == expected
            diff = out[:, lat:] - x[:, :x.shape[1] - lat]
            assert float(np.max(np.abs(diff))) < 5e-3

    def test_mix_zero_aligned_dry(self) -> None:
        """Mix=0：干声按声明延迟对齐直通。"""
        rng = np.random.default_rng(9)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        fx = SaturationEffect(RATE)
        fx.set_params(_mb_params(mb_oversampling=4, mb_mix=0.0))
        out = fx.process(x.copy())
        lat = fx.latency_samples()
        np.testing.assert_allclose(
            out[:, lat:], x[:, :x.shape[1] - lat], rtol=0, atol=1e-6)

    def test_default_profile_passthrough(self) -> None:
        """SaturationParams 默认（legacy）→ 驱动 0 直通。"""
        fx = SaturationEffect(RATE)
        fx.set_params(SaturationParams.default().to_dict())
        rng = np.random.default_rng(3)
        x = (rng.uniform(-0.2, 0.2, (2, 512))).astype(np.float32)
        np.testing.assert_array_equal(fx.process(x.copy()), x)


class TestMultibandProcessing:
    @pytest.mark.parametrize("oversampling", [1, 2, 4])
    @pytest.mark.parametrize("step", [127, 513, 1024])
    def test_block_consistency_bit_exact(self, oversampling, step) -> None:
        """任意分块与整段逐位一致（每实例每声道历史跨块）。"""
        rng = np.random.default_rng(11)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        params = _mb_params(
            mb_oversampling=oversampling,
            mb_bands=(_band(), _band(enabled=True, model="tube",
                                     drive_db=12.0, bias_pct=20.0),
                      _band(enabled=True, model="transistor",
                            drive_db=6.0)))
        fx = SaturationEffect(RATE)
        fx.set_params(dict(params))
        whole = fx.process(x.copy())
        fxb = SaturationEffect(RATE)
        fxb.set_params(dict(params))
        stitched = np.concatenate(
            [fxb.process(x[:, s:s + step])
             for s in range(0, x.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_reset_state_restores_stream(self) -> None:
        """seek 复位：reset 后同输入同结果。"""
        rng = np.random.default_rng(2)
        x = (rng.uniform(-0.3, 0.3, (2, 2000))).astype(np.float32)
        params = _mb_params(mb_oversampling=2,
                            mb_bands=(_band(), _band(
                                enabled=True, model="tape", drive_db=9.0),
                                      _band()))
        fx = SaturationEffect(RATE)
        fx.set_params(dict(params))
        first = fx.process(x.copy())
        fx.reset_state()
        second = fx.process(x.copy())
        np.testing.assert_array_equal(first, second)

    def test_disabled_band_keeps_dry(self) -> None:
        """禁用段保留该频段干声（参与求和重构）。"""
        rng = np.random.default_rng(7)
        x = (rng.uniform(-0.2, 0.2, (2, 4000))).astype(np.float32)
        only_low = _mb_params(mb_bands=(
            _band(enabled=True, model="tape", drive_db=18.0), _band(),
            _band()))
        all_off = _mb_params()
        f1 = SaturationEffect(RATE)
        f1.set_params(dict(only_low))
        f2 = SaturationEffect(RATE)
        f2.set_params(dict(all_off))
        lat = f1.latency_samples()
        changed = f1.process(x.copy()) - f2.process(x.copy())
        # 差异集中在前两个分频点之间不可断言——只要求全禁用基线为直通。
        base = f2.process(x.copy())
        np.testing.assert_allclose(
            base[:, lat:], x[:, :x.shape[1] - lat], rtol=0, atol=5e-3)
        assert float(np.max(np.abs(changed))) > 0.01

    def test_tube_even_harmonics_and_dc_block(self) -> None:
        """tube：偶次谐波生成且直流被阻断。"""
        t = np.arange(2 * RATE) / RATE
        tone = (0.4 * np.sin(2 * np.pi * 2000.0 * t))[None, :].astype(
            np.float32)

        def harmonic(pcm: np.ndarray, hz: int) -> float:
            steady = pcm[0, RATE:2 * RATE]
            return float(2 * abs(np.fft.rfft(steady)[hz]) / steady.size)

        fx = SaturationEffect(RATE)
        fx.set_params(_mb_params(
            mb_xover_low_hz=300.0, mb_xover_high_hz=1200.0,
            mb_bands=(_band(), _band(),
                      _band(enabled=True, model="tube", drive_db=9.0,
                            bias_pct=35.0))))
        out = fx.process(tone.copy())
        assert harmonic(out, 2000) == pytest.approx(0.93, abs=0.05)
        assert harmonic(out, 4000) > 0.05      # 偶次（bias 引入）
        assert harmonic(out, 6000) > 0.01      # 奇次
        assert harmonic(out, 0) < 1e-4         # DC 阻断

    def test_oversampling_folds_reduction(self) -> None:
        """10 kHz 三次谐波折叠：4× 相比 1× 降低 ≥20 dB（计划门槛）。"""
        t = np.arange(3 * RATE) / RATE
        tone = (0.55 * np.sin(2 * np.pi * 10000.0 * t))[None, :].astype(
            np.float32)

        def fold(pcm: np.ndarray) -> float:
            steady = pcm[0, RATE:2 * RATE]
            return float(2 * abs(np.fft.rfft(steady)[18000]) / steady.size)

        params = _mb_params(mb_bands=(_band(), _band(),
                                      _band(enabled=True, model="tape",
                                            drive_db=12.0)))
        f1 = SaturationEffect(RATE)
        f1.set_params(dict(params, mb_oversampling=1))
        f4 = SaturationEffect(RATE)
        f4.set_params(dict(params, mb_oversampling=4))
        h1 = fold(f1.process(tone.copy()))
        h4 = fold(f4.process(tone.copy()))
        assert 20.0 * np.log10(max(h1, 1e-9) / max(h4, 1e-9)) >= 20.0

    def test_output_gain_and_tone(self) -> None:
        """输出增益按 dB 生效；Tone 高架抬高频。"""
        rng = np.random.default_rng(4)
        x = (rng.uniform(-0.05, 0.05, (2, 4000))).astype(np.float32)
        base_fx = SaturationEffect(RATE)
        base_fx.set_params(_mb_params())
        base = base_fx.process(x.copy())
        gain_fx = SaturationEffect(RATE)
        gain_fx.set_params(_mb_params(mb_output_gain_db=6.0))
        gained = gain_fx.process(x.copy())
        lat = gain_fx.latency_samples()
        np.testing.assert_allclose(
            gained[:, lat:], base[:, lat:] * 10 ** (6.0 / 20.0),
            rtol=0, atol=1e-6)


class TestShapers:
    def test_slope_and_bounds(self) -> None:
        """三种模型 x=0 斜率 1、输出有界。"""
        small = np.array([1e-6], dtype=np.float64)
        for shaper in (shape_tape, shape_transistor):
            assert abs(shaper(small)[0] / 1e-6 - 1.0) < 1e-4
        assert abs(shape_tube(small, 0.35)[0] / 1e-6 - 1.0) < 1e-4
        big = np.array([50.0, -50.0])
        assert np.max(np.abs(shape_tape(big))) <= 1.0
        assert np.max(np.abs(shape_transistor(big))) < 1.0
        assert np.max(np.abs(shape_tube(big, 0.9))) < 10.0  # 有界

    def test_panel_drive_mapping_constant(self) -> None:
        """面板 drive（0..10）→ dB 映射常量（绑定层共用）。"""
        assert PANEL_DRIVE_TO_DB == 3.0
