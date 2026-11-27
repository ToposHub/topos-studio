"""P2 · 激励器（FX-15 · 2026-11-21 计划）。

验收：2.5 kHz 输入 → 谐波支路在预定频点（5 kHz even / 7.5 kHz odd）
出现分量，幅度自洽于分频后带内振幅（减法分频在交接区有 |1−H| 增益
峰——计划理想值 0.01/0.002 对应未分频口径）；DC 稳态 < −100 dBFS
量级；Amount=0/默认 → 精确干声；amount 线性；分块位精确。
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
from src.features.edit.core.timeline_v2 import ExciterParams

RATE = 48000


def _chain(params: ExciterParams) -> EffectChain:
    chain = EffectChain(RATE)
    chain.set_params([("exciter", params.to_dict(), True, "ex",
                       ALGORITHM_VERSION)], sample_rate=RATE)
    return chain


def _amp(pcm: np.ndarray, hz: int) -> float:
    steady = pcm[0, RATE:]
    return float(2 * abs(np.fft.rfft(steady)[hz]) / steady.size)


def _high_band_amplitude(hz: float) -> float:
    """分频后 high 带在 hz 处的振幅（与 DSP 同款 4 阶减法分频）。"""
    from scipy.signal import butter, sosfilt
    t = np.arange(2 * RATE) / RATE
    dry = (0.2 * np.sin(2 * np.pi * hz * t))[None, :]
    sos = butter(4, 2000.0, btype="lowpass", fs=RATE, output="sos")
    low = sosfilt(sos, dry, axis=-1)
    high = dry - low
    return float(2 * abs(np.fft.rfft(high[0, RATE:])[int(hz)]) / RATE)


class TestExciter:
    def test_even_harmonic_at_2f(self) -> None:
        """even：谐波分量 ∝ amount·(band_high 振幅)²/2 @2f。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 2500.0 * t))[None, :].astype(
            np.float32)
        params = ExciterParams.from_dict({"ex_bands": [
            {"enabled": False},
            {"enabled": True, "mode": "even", "amount": 0.5}]})
        out = _chain(params).process(dry.copy())
        high_amp = _high_band_amplitude(2500.0)
        expected = 0.5 * high_amp ** 2 / 2.0
        assert _amp(out, 5000) == pytest.approx(expected, rel=0.2)
        assert _amp(out, 2500) == pytest.approx(0.2, abs=0.02)

    def test_odd_harmonic_at_3f(self) -> None:
        """odd：谐波分量 ∝ amount·(band_high 振幅)³/4 @3f。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 2500.0 * t))[None, :].astype(
            np.float32)
        params = ExciterParams.from_dict({"ex_bands": [
            {"enabled": False},
            {"enabled": True, "mode": "odd", "amount": 1.0}]})
        out = _chain(params).process(dry.copy())
        high_amp = _high_band_amplitude(2500.0)
        expected = high_amp ** 3 / 4.0
        assert _amp(out, 7500) == pytest.approx(expected, rel=0.25)

    def test_amount_linearity(self) -> None:
        """谐波幅度随 amount 线性。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 2500.0 * t))[None, :].astype(
            np.float32)
        h_05 = _amp(_chain(ExciterParams.from_dict({"ex_bands": [
            {"enabled": False},
            {"enabled": True, "mode": "even", "amount": 0.5}]})).process(
                dry.copy()), 5000)
        h_10 = _amp(_chain(ExciterParams.from_dict({"ex_bands": [
            {"enabled": False},
            {"enabled": True, "mode": "even", "amount": 1.0}]})).process(
                dry.copy()), 5000)
        assert h_10 == pytest.approx(2.0 * h_05, rel=0.05)

    def test_dc_suppressed(self) -> None:
        """偶次整形的 DC 被高通消除：稳态均值 < −100 dBFS 量级。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 2500.0 * t))[None, :].astype(
            np.float32)
        params = ExciterParams.from_dict({"ex_bands": [
            {"enabled": False},
            {"enabled": True, "mode": "even", "amount": 1.0}]})
        out = _chain(params).process(dry.copy())
        assert abs(float(np.mean(out[0, RATE:]))) < 1e-5

    def test_default_is_exact_passthrough(self) -> None:
        """默认（Amount 0）→ 精确干声（计划：发布默认值前保证直通）。"""
        rng = np.random.default_rng(6)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        out = _chain(ExciterParams.default()).process(x.copy())
        np.testing.assert_array_equal(out, x)

    def test_wet_zero_exact_dry(self) -> None:
        """wet=0 → 精确干声（干湿内部对齐）。"""
        rng = np.random.default_rng(6)
        x = (rng.uniform(-0.3, 0.3, (2, 4000))).astype(np.float32)
        params = ExciterParams.from_dict({
            "ex_wet": 0.0,
            "ex_bands": [{"enabled": False},
                         {"enabled": True, "mode": "even", "amount": 1.0}]})
        out = _chain(params).process(x.copy())
        np.testing.assert_array_equal(out, x)

    @pytest.mark.parametrize("step", [513, 1024])
    def test_block_consistency_bit_exact(self, step) -> None:
        rng = np.random.default_rng(9)
        x = (rng.uniform(-0.3, 0.3, (2, 6000))).astype(np.float32)
        params = ExciterParams.from_dict({
            "ex_oversampling": 2,
            "ex_bands": [{"enabled": False},
                         {"enabled": True, "mode": "even", "amount": 0.7}]})
        whole = _chain(params).process(x.copy())
        fxb = _chain(params)
        stitched = np.concatenate(
            [fxb.process(x[:, s:s + step])
             for s in range(0, x.shape[1], step)], axis=1)
        np.testing.assert_array_equal(whole, stitched)

    def test_param_round_trip(self) -> None:
        params = ExciterParams.from_dict({
            "ex_bands": [{"enabled": True, "mode": "odd", "amount": 0.4}]})
        assert params.ex_bands[0]["mode"] == "odd"
        restored = ExciterParams.from_dict(params.to_dict())
        assert restored == params


def test_exciter_neutral_2x_delays_the_dry_path_without_shaping() -> None:
    pcm = np.zeros((2, 80), np.float32)
    pcm[:, 0] = .25
    delayed = np.roll(pcm, 20, axis=1)
    clean = {"ex_quality": "clean", "ex_oversampling": 2,
             "ex_bands": [{"enabled": True, "amount": 1.0, "mix": 0.0}]}
    for params in (clean, {"ex_oversampling": 2}, clean | {
        "ex_wet": 0.0, "ex_bands": [{"enabled": True, "amount": 1.0}],
    }):
        fx = _chain(ExciterParams.from_dict(params))
        assert fx.latency_samples() == fx.tail_estimate_samples() == 20
        np.testing.assert_array_equal(fx.process(pcm), delayed)
        trim = _chain(ExciterParams.from_dict(params | {"ex_output_gain_db": -6.020599913279624}))
        np.testing.assert_array_equal(trim.process(pcm), delayed * .5)
        bypass = _chain(ExciterParams.from_dict(params | {"enabled": False}))
        assert bypass.latency_samples() == 0
        np.testing.assert_array_equal(bypass.process(pcm), pcm)


def test_exciter_dry_delay_remains_continuous_when_wet_crosses_zero() -> None:
    pcm = np.random.default_rng(318).normal(0.0, .12, (2, 140)).astype(np.float32)
    fx = EffectChain(RATE)
    params = {"ex_quality": "clean", "ex_oversampling": 2, "ex_wet": 0.0,
              "ex_bands": [{"enabled": True, "amount": .75}]}
    fx.set_params([("exciter", params, True, "exciter")])
    np.testing.assert_array_equal(fx.process(pcm[:, :100])[:, 20:], pcm[:, :80])
    fx.set_params([("exciter", params | {"ex_wet": .6}, True, "exciter")])
    assert fx.latency_samples() == 20
    actual = fx.process(pcm[:, 100:])
    np.testing.assert_array_equal(actual[:, 0], pcm[:, 80])


@pytest.mark.parametrize("quality", ["legacy", "clean"])
def test_exciter_live_mix_and_trim_keep_filter_and_fir_history(quality: str) -> None:
    pcm = np.random.default_rng(51).normal(0.0, .1, (3, 1372)).astype(np.float32)
    params = ExciterParams.from_dict({
        "ex_quality": quality, "ex_oversampling": 2,
        "ex_bands": [{"enabled": True, "mode": "even", "amount": .4},
                     {"enabled": True, "mode": "odd", "amount": .7}],
    }).to_dict()
    streamed = EffectChain(RATE)
    streamed.set_params([("exciter", params, True, "exciter")])
    start = streamed.process(pcm[:, :513])
    updated = params | {"ex_output_gain_db": -6.020599913279624, "ex_wet": .38}
    streamed.set_params([("exciter", updated, True, "exciter")])
    rest = streamed.process(pcm[:, 513:])
    dry_reference = _chain(ExciterParams.from_dict(params)).process(pcm)
    adjusted = _chain(ExciterParams.from_dict(updated)).process(pcm)
    np.testing.assert_array_equal(start, dry_reference[:, :513])
    np.testing.assert_array_equal(rest, adjusted[:, 513:])
    assert streamed.latency_samples() == 20
    assert streamed.tail_estimate_samples() > 2 * streamed.latency_samples()


def test_clean_2x_exciter_preserves_dry_high_frequencies() -> None:
    t = np.arange(2 * RATE) / RATE
    pcm = (.12 * np.sin(2 * np.pi * 21_000.0 * t))[None, :].astype(np.float32)
    params = {"ex_oversampling": 2, "ex_bands": [{},
              {"enabled": True, "mode": "even", "amount": .01}]}
    legacy = _chain(ExciterParams.from_dict(params)).process(pcm)
    clean = _chain(ExciterParams.from_dict(params | {"ex_quality": "clean"})).process(pcm)
    assert _amp(clean, 21_000) == pytest.approx(.12, abs=1e-7)
    assert _amp(legacy, 21_000) < .115
    assert ExciterParams.from_dict(params).ex_quality == "legacy"
    assert ExciterParams.default().ex_quality == "clean"


def test_clean_2x_exciter_reduces_folded_third_harmonic() -> None:
    t = np.arange(2 * RATE) / RATE
    pcm = (.35 * np.sin(2 * np.pi * 13_000.0 * t))[None, :].astype(np.float32)
    params = {"ex_quality": "clean", "ex_bands": [{},
              {"enabled": True, "mode": "odd", "amount": .8}]}
    full_rate = _chain(ExciterParams.from_dict(params)).process(pcm)
    oversampled = _chain(ExciterParams.from_dict(params | {"ex_oversampling": 2})).process(pcm)
    assert _amp(full_rate, 9000) > .007
    assert _amp(oversampled, 9000) < _amp(full_rate, 9000) / 100.0
