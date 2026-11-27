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
    SubharmonicEffect,
    estimate_subharmonic_f0,
)
from src.features.edit.core.effect_registry import create_effect_instance
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


def _tone(frequency: float, length: int = RATE // 2, rate: int = RATE) -> np.ndarray:
    samples = (0.3 * np.sin(2.0 * np.pi * frequency * np.arange(length) / rate)).astype(np.float32)
    return np.stack((samples, -samples))


def _focused(rate: int = RATE, **custom: float) -> SubharmonicEffect:
    effect = SubharmonicEffect(rate)
    effect.set_params(SubharmonicParams.default().to_dict() | custom)
    return effect


def test_only_new_effects_default_to_focused_and_sparse_restore_reselects_legacy() -> None:
    assert SubharmonicParams.default().sh_detection_mode == "focused"
    instance = create_effect_instance("subharmonic")
    assert instance is not None and instance.params["sh_detection_mode"] == "focused"
    assert SubharmonicParams.from_dict({}).sh_detection_mode == "legacy"
    malformed = SubharmonicParams.from_dict({"sh_attack_ms": float("nan"), "sh_mix": "invalid"})
    assert malformed.sh_attack_ms == 20.0 and malformed.sh_mix == 0.5
    effect = _focused(sh_source_center_hz=160.0, sh_source_bandwidth_hz=16.0)
    signal = _tone(90.0)
    np.testing.assert_array_equal(effect.process(signal), signal)
    effect.set_params({})
    assert effect.latency_samples() == 0
    effect.process(signal)
    tap = effect.collect_telemetry()
    assert tap["detection_mode"] == "legacy"
    assert tap["locked"]


@pytest.mark.parametrize(
    "frequency,center,width,should_lock",
    [(70.0, 70.0, 20.0, True), (70.0, 150.0, 20.0, False),
     (150.0, 90.0, 160.0, True), (240.0, 120.0, 40.0, False),
     (45.0, 45.0, 10.0, True), (180.0, 180.0, 10.0, True)],
)
def test_focused_source_controls_reject_outside_and_harmonic_aliases(
    frequency: float, center: float, width: float, should_lock: bool,
) -> None:
    effect = _focused(sh_source_center_hz=center, sh_source_bandwidth_hz=width, sh_mix=1.0)
    signal = _tone(frequency)
    out = effect.process(signal)
    tap = effect.collect_telemetry()
    assert tap["source_low_hz"] == max(45.0, center - width * 0.5)
    assert tap["source_high_hz"] == min(180.0, center + width * 0.5)
    assert tap["locked"] is should_lock
    if should_lock:
        assert tap["f0_hz"] == pytest.approx(frequency, abs=0.2)
        assert tap["confidence"] > 0.8
        assert tap["sub_peak_db"] > -18.0
    else:
        assert tap["sub_peak_db"] == -100.0
        np.testing.assert_array_equal(out, signal)


def test_focused_preview_hysteresis_holds_a_real_lock_at_lower_confidence() -> None:
    signal = _tone(90.0, RATE)
    noisy = signal + np.random.default_rng(333).normal(0, 0.15, signal.shape).astype(np.float32)
    cfg = {"sh_source_center_hz": 90.0, "sh_source_bandwidth_hz": 34.0, "sh_mix": 1.0}
    cold = _focused(**cfg)
    np.testing.assert_array_equal(cold.process(noisy), noisy)
    assert not cold.collect_telemetry()["locked"]
    held = _focused(**cfg)
    held.process(signal)
    assert held.collect_telemetry()["locked"]
    held.process(noisy)
    tap = held.collect_telemetry()
    assert tap["locked"]
    assert 0.55 < tap["confidence"] < 0.8
    assert tap["f0_hz"] == pytest.approx(90.0, abs=2.0)


def test_focused_band_edits_keep_a_lock_until_the_existing_pitch_is_excluded() -> None:
    cfg = SubharmonicParams.default().to_dict()
    effect = _focused()
    signal = _tone(90.0)
    effect.process(signal)
    assert effect.collect_telemetry()["locked"]
    effect.set_params(cfg | {"sh_source_bandwidth_hz": 75.0})
    effect.process(signal[:, :1024])
    assert effect.collect_telemetry()["locked"]
    effect.set_params(cfg | {"sh_source_center_hz": 145.0, "sh_source_bandwidth_hz": 10.0})
    effect.process(signal[:, 1024:2048])
    assert not effect.collect_telemetry()["locked"]


def test_focused_release_keeps_oscillator_moving_and_returns_to_dry() -> None:
    effect = _focused(sh_release_ms=110.0, sh_mix=1.0, sh_octave2_gain_db=-120.0)
    effect.process(_tone(90.0))
    assert effect.collect_telemetry()["locked"]
    release = effect.process(np.zeros((2, RATE // 5), dtype=np.float32))[0]
    assert np.count_nonzero(np.diff(np.signbit(release[1200:4500]))) >= 4
    assert np.max(np.abs(release[-1024:])) < 0.001
    assert not effect.collect_telemetry()["locked"]
    signal = _tone(240.0, RATE)
    output = effect.process(signal)
    np.testing.assert_array_equal(output[:, -1024:], signal[:, -1024:])


def test_focused_split_at_44100_and_unmonitored_pcm_are_identical() -> None:
    rate = 44100
    signal = np.concatenate((_tone(90.0, rate // 2, rate),
                             np.zeros((2, rate // 3), dtype=np.float32)), axis=-1)
    whole = _focused(rate, sh_mix=0.75, sh_attack_ms=3.0, sh_release_ms=87.0)
    split = _focused(rate, sh_mix=0.75, sh_attack_ms=3.0, sh_release_ms=87.0)
    split.set_monitoring(False)
    expected = whole.process(signal)
    actual = np.concatenate([split.process(signal[:, pos:pos + 367])
                             for pos in range(0, signal.shape[-1], 367)], axis=-1)
    np.testing.assert_array_equal(actual, expected)
    assert whole.latency_samples() == split.latency_samples() == 0
    assert split.collect_telemetry()["sub_peak_db"] == -100.0


def test_focused_finished_release_rearms_without_block_size_dependent_phase() -> None:
    first = _tone(90.0)
    silence = np.zeros((2, RATE // 4), dtype=np.float32)
    second = _tone(70.0)
    signal = np.concatenate((first, silence, second), axis=-1)
    engine = _focused(sh_mix=1.0, sh_release_ms=20.0)
    engine.process(first)
    engine.process(silence)
    assert engine.collect_telemetry()["env"] == 0.0
    assert engine.collect_telemetry()["f0_hz"] == 0.0
    engine.process(second)
    assert engine.collect_telemetry()["f0_hz"] == pytest.approx(70.0, abs=0.2)

    renders = []
    for block in (512, 2048):
        effect = _focused(sh_mix=1.0, sh_release_ms=20.0)
        renders.append(np.concatenate([effect.process(signal[:, i:i + block])
                                       for i in range(0, signal.shape[-1], block)], axis=-1))
    np.testing.assert_array_equal(*renders)


def test_focused_muted_octaves_dont_reduce_dry() -> None:
    effect = _focused(sh_mix=1.0, sh_octave1_gain_db=-120.0, sh_octave2_gain_db=-120.0)
    pcm = _tone(90.0)
    np.testing.assert_array_equal(effect.process(pcm), pcm)
    assert effect.collect_telemetry()["locked"]
    assert effect.collect_telemetry()["sub_peak_db"] == -100.0


def test_focused_tail_budget_includes_lowpass_filter_ringout() -> None:
    effect = _focused(sh_mix=1.0, sh_release_ms=20.0, sh_sub_lpf_hz=40.0,
                      sh_octave1_gain_db=0.0, sh_octave2_gain_db=-120.0)
    steady = effect.process(_tone(100.0))
    assert effect.collect_telemetry()["locked"]
    budget = effect.tail_estimate_samples()
    assert budget > (0.020 + 2 * 512 / RATE) * RATE
    tail = effect.process(np.zeros((2, budget + 512), dtype=np.float32))
    assert float(np.max(np.abs(tail[:, budget:]))) < 1e-3 * float(np.max(np.abs(steady)))


@pytest.mark.parametrize("channels", [8, 24])
def test_focused_multichannel_probe_finds_quiet_tone_beyond_loud_noise(channels: int) -> None:
    rate = 96000
    rng = np.random.default_rng(61)
    pcm = rng.normal(0, 0.3, size=(channels, rate // 4)).astype(np.float32)
    effect = _focused(rate, sh_mix=1.0)
    np.testing.assert_array_equal(effect.process(pcm), pcm)
    assert not effect.collect_telemetry()["locked"]
    effect.reset_state()
    pcm[-1] = _tone(90.0, pcm.shape[-1], rate)[0] * 0.19
    wet = np.concatenate([effect.process(pcm[:, i:i + 512])
                          for i in range(0, pcm.shape[-1], 512)], axis=-1)
    assert effect.collect_telemetry()["locked"]
    assert effect.collect_telemetry()["f0_hz"] == pytest.approx(90.0, abs=0.2)
    assert np.max(np.abs(wet - pcm)) > 0.01


def test_internal_activation_does_not_reuse_a_stale_focused_note() -> None:
    params = SubharmonicParams.default().to_dict()
    effect = _focused(sh_mix=1.0)
    effect.process(_tone(90.0))
    assert effect.collect_telemetry()["locked"]
    zeros = np.zeros((2, 512), np.float32)
    effect.set_params(params | {"enabled": False})
    for _ in range(8):
        np.testing.assert_array_equal(effect.process(zeros), zeros)
    effect.set_params(params | {"sh_mix": 1.0})
    np.testing.assert_array_equal(effect.process(zeros), zeros)
    assert not effect.collect_telemetry()["locked"]
