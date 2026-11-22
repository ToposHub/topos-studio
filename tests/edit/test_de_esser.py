"""P1 · De-Esser（2026-11-19 计划第三阶段 #1）。

验收（计划原文）：用对白和 1 kHz+6 kHz 双音验收，齿音组能衰减 6 dB、
Split 下低频 ≤0.5 dB 变化，普通对白保持且旁路无跳变。增益控制按
离线 oracle：over_db·(1−1/ratio)，钳制 max_reduction。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.de_esser import DeEsserEffect

RATE = 48000


def _dual_tone(seconds: float = 2.0, amp_1k: float = 0.25,
               amp_6k: float = 0.25):
    t = np.arange(int(seconds * RATE)) / RATE
    low = amp_1k * np.sin(2 * np.pi * 1000.0 * t)
    high = amp_6k * np.sin(2 * np.pi * 6000.0 * t)
    sig = (low + high).astype(np.float32)
    return np.stack([sig, sig])


def _band_rms(sig: np.ndarray, freq: float, bw: float = 800.0) -> float:
    mono = sig[0] if sig.ndim > 1 else sig
    spec = np.abs(np.fft.rfft(mono * np.hanning(mono.shape[-1]))) ** 2
    freqs = np.fft.rfftfreq(mono.shape[-1], 1.0 / RATE)
    mask = np.abs(freqs - freq) <= bw
    return float(np.sqrt(np.sum(spec[mask])))


def _make(params: dict) -> DeEsserEffect:
    fx = DeEsserEffect(float(RATE))
    merged = {"frequency": 6000.0, "q": 2.0, "attack_ms": 5.0,
              "release_ms": 60.0}
    merged.update(params)
    fx.set_params(merged)
    return fx


class TestDeEsserOracle:
    def test_sibilance_band_attenuated_6db_split(self) -> None:
        """6 kHz @ −12 dBFS，阈值 −20、ratio 4、max 12 → GR 恰 6 dB。"""
        pcm = _dual_tone(amp_6k=0.25)   # 检测带 RMS ≈ −15 dBFS
        fx = _make({"threshold_dbfs": -23.0, "ratio": 4.0,
                    "max_reduction_db": 12.0, "mode": "split"})
        out = fx.process(pcm.copy())
        # 稳态段（避开 attack 包络建立）测量带内 RMS 比例。
        steady = slice(RATE // 2, None)
        before = _band_rms(pcm[:, steady], 6000.0)
        after = _band_rms(out[:, steady], 6000.0)
        reduction_db = 20.0 * np.log10(before / max(after, 1e-12))
        assert reduction_db == pytest.approx(6.0, abs=1.2)

    def test_split_low_band_unchanged_within_half_db(self) -> None:
        pcm = _dual_tone()
        fx = _make({"threshold_dbfs": -20.0, "ratio": 4.0, "mode": "split"})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)
        low_before = _band_rms(pcm[:, steady], 1000.0)
        low_after = _band_rms(out[:, steady], 1000.0)
        delta = abs(20.0 * np.log10(low_after / low_before))
        assert delta <= 0.5  # Split 下低频 ≤0.5 dB 变化

    def test_wide_attenuates_both_bands(self) -> None:
        pcm = _dual_tone()
        fx = _make({"threshold_dbfs": -20.0, "ratio": 4.0, "mode": "wide"})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)
        low_delta = 20.0 * np.log10(
            _band_rms(pcm[:, steady], 1000.0)
            / _band_rms(out[:, steady], 1000.0))
        high_delta = 20.0 * np.log10(
            _band_rms(pcm[:, steady], 6000.0)
            / _band_rms(out[:, steady], 6000.0))
        assert low_delta > 3.0   # Wide：全带一起压
        assert high_delta == pytest.approx(low_delta, abs=0.8)

    def test_below_threshold_no_reduction(self) -> None:
        """普通对白电平低于阈值：保持（旁路无跳变语义）。"""
        pcm = _dual_tone(amp_1k=0.05, amp_6k=0.02)   # 远低于阈值
        fx = _make({"threshold_dbfs": -12.0, "mode": "split"})
        out = fx.process(pcm.copy())
        np.testing.assert_allclose(out, pcm, atol=1e-3)

    def test_bypass_passthrough(self) -> None:
        pcm = _dual_tone()
        fx = _make({"enabled": False})
        out = fx.process(pcm.copy())
        np.testing.assert_array_equal(out, pcm)

    def test_listen_outputs_detector_band(self) -> None:
        pcm = _dual_tone()
        fx = _make({"listen": True, "threshold_dbfs": -20.0})
        out = fx.process(pcm.copy())
        # 监听输出：6 kHz 保留、1 kHz 被带通滤除。
        steady = slice(RATE // 2, None)
        high = _band_rms(out[:, steady], 6000.0)
        low = _band_rms(out[:, steady], 1000.0)
        assert high > 10.0 * low

    def test_max_reduction_clamped(self) -> None:
        pcm = _dual_tone(amp_6k=0.5)  # −6 dBFS，远超阈值
        fx = _make({"threshold_dbfs": -30.0, "ratio": 8.0,
                    "max_reduction_db": 6.0, "mode": "split"})
        out = fx.process(pcm.copy())
        steady = slice(RATE // 2, None)
        reduction_db = 20.0 * np.log10(
            _band_rms(pcm[:, steady], 6000.0)
            / _band_rms(out[:, steady], 6000.0))
        assert reduction_db <= 6.8  # 钳制在 max 6 dB（+容差）

    def test_state_continuity_across_blocks(self) -> None:
        """分块处理与整段一致（包络/滤波器状态跨块连续）。"""
        pcm = _dual_tone()
        fx_whole = _make({"threshold_dbfs": -20.0, "mode": "split"})
        whole = fx_whole.process(pcm.copy())
        fx_blocks = _make({"threshold_dbfs": -20.0, "mode": "split"})
        chunks = []
        for start in range(0, pcm.shape[1], 7919):
            chunks.append(fx_blocks.process(
                pcm[:, start:start + 7919].copy()))
        stitched = np.concatenate(chunks, axis=1)
        # 稳态段逐样本一致（首块包络建立期允许差异）。
        np.testing.assert_allclose(
            whole[:, RATE // 2:], stitched[:, RATE // 2:], atol=1e-4)

    def test_registered_in_effect_chain(self) -> None:
        from src.features.edit.core.audio_dsp import EffectChain

        chain = EffectChain(float(RATE))
        chain.set_params([("de_esser", {
            "threshold_dbfs": -20.0, "ratio": 4.0, "mode": "split"}, True)])
        pcm = _dual_tone()
        out = chain.process(pcm.copy())
        steady = slice(RATE // 2, None)
        reduction_db = 20.0 * np.log10(
            _band_rms(pcm[:, steady], 6000.0)
            / _band_rms(out[:, steady], 6000.0))
        assert reduction_db > 3.0
        assert chain.latency_samples() == 0
        assert chain.tail_estimate_samples() == 0


@pytest.mark.parametrize("extra", [{}, {"detector_shape": "bandpass", "lookahead_ms": 3.0}])
@pytest.mark.parametrize("mode", ["split", "wide"])
def test_transients_are_consistent_across_render_block_sizes(extra: dict, mode: str) -> None:
    # Bursts alternate in duration and level so both detector slopes change
    # inside blocks. A stationary tone rarely exposes an incorrect block-start
    # comparison for the attack/release branch.
    rng = np.random.default_rng(502)
    levels = np.repeat([0.035, 0.24, 0.7, 0.01, 0.55, 0.1, 0.03],
                       [701, 1901, 3431, 1771, 1999, 1121, 4777])
    t = np.arange(levels.size) / RATE
    signal = levels * np.sin(2 * np.pi * 6500 * t)
    pcm = np.stack((signal, 0.4 * signal + 0.05 * rng.standard_normal(t.size))).astype(np.float32)
    params = dict(extra, threshold_dbfs=-36.0, mode=mode)
    fx = _make(params)
    expected = fx.process(pcm)
    telemetry = fx.collect_telemetry()
    delay = fx.latency_samples()
    assert np.max(np.abs(pcm[:, :pcm.shape[1] - delay] - expected[:, delay:])) > 0.01

    for block_size in (512, 2000, 8192):
        fx.reset_state()
        blocks = [fx.process(pcm[:, start:start + block_size])
                  for start in range(0, pcm.shape[1], block_size)]
        np.testing.assert_allclose(np.concatenate(blocks, axis=1), expected,
                                   rtol=0.0, atol=6e-8)
        assert fx.collect_telemetry() == telemetry

    fx.reset_state()
    blocks = []
    start = 0
    while start < pcm.shape[1]:
        stop = min(start + int(rng.integers(1, 3001)), pcm.shape[1])
        blocks.append(fx.process(pcm[:, start:stop]))
        start = stop
    np.testing.assert_allclose(np.concatenate(blocks, axis=1), expected,
                               rtol=0.0, atol=6e-8)


@pytest.mark.parametrize("extra", [{}, {"detector_shape": "bandpass"}])
def test_attack_and_release_shape_gain_on_each_detector_path(extra: dict) -> None:
    # Keep a quiet probe under threshold after a loud burst. Wide mode lets
    # the output RMS ratio reveal the actual envelope gain on that probe.
    pcm = _dual_tone(seconds=0.5, amp_1k=0.0, amp_6k=0.4)
    pcm[:, :RATE // 10] *= 0.04
    pcm[:, RATE // 4:] *= 0.04
    params = dict(extra, threshold_dbfs=-27.0, mode="wide")
    fast = _make(dict(params, attack_ms=2.0, release_ms=20.0)).process(pcm)
    slow = _make(dict(params, attack_ms=60.0, release_ms=300.0)).process(pcm)

    early_burst = slice(5500, 6000)
    after_burst = slice(17000, 17500)
    assert np.linalg.norm(fast[:, early_burst]) < 0.9 * np.linalg.norm(slow[:, early_burst])
    assert np.linalg.norm(slow[:, after_burst]) < 0.9 * np.linalg.norm(fast[:, after_burst])
