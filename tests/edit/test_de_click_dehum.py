"""P1 · De-Click / De-Hum（2026-11-19 计划第三阶段 #2）。

验收（计划原文）：50/60 谐波抑制 ≥15 dB、有用频带 ≤0.5 dB，定位
稀疏点击，鼓点及爆破音保真。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.de_click_dehum import (
    ClickCandidate,
    detect_clicks,
    detect_hum_base,
    remove_clicks,
    remove_hum,
)

RATE = 48000


def _hum_tone(seconds: float = 3.0, base: float = 50.0,
              hum_amp: float = 0.05, tone_amp: float = 0.2):
    t = np.arange(int(seconds * RATE)) / RATE
    hum = np.zeros_like(t)
    for harmonic in range(1, 9):
        hum += hum_amp / harmonic * np.sin(2 * np.pi * base * harmonic * t)
    tone = tone_amp * np.sin(2 * np.pi * 440.0 * t)
    sig = (hum + tone).astype(np.float32)
    return np.stack([sig, sig])


def _band_energy(sig: np.ndarray, freq: float, bw: float = 6.0) -> float:
    mono = sig[0] if sig.ndim > 1 else sig
    spec = np.abs(np.fft.rfft(mono * np.hanning(mono.shape[-1])))
    freqs = np.fft.rfftfreq(mono.shape[-1], 1.0 / RATE)
    mask = np.abs(freqs - freq) <= bw
    return float(np.max(spec[mask])) if mask.any() else 0.0


class TestDeHum:
    def test_harmonics_suppressed_15db_speech_band_intact(self) -> None:
        pcm = _hum_tone()
        cleaned = remove_hum(pcm, RATE, base_hz=50.0)
        # 50/100/150 Hz 谐波抑制 ≥15 dB。
        for freq in (50.0, 100.0, 150.0):
            before = _band_energy(pcm, freq)
            after = _band_energy(cleaned, freq)
            assert 20.0 * np.log10(before / max(after, 1e-9)) >= 15.0, freq
        # 有用频带（440 Hz）≤0.5 dB。
        tone_before = _band_energy(pcm, 440.0, bw=20.0)
        tone_after = _band_energy(cleaned, 440.0, bw=20.0)
        delta = abs(20.0 * np.log10(tone_after / tone_before))
        assert delta <= 0.5

    def test_auto_detect_50_vs_60(self) -> None:
        assert detect_hum_base(_hum_tone(base=50.0), RATE) == 50.0
        assert detect_hum_base(_hum_tone(base=60.0), RATE) == 60.0
        # 无哼声（纯乐音）不误检。
        t = np.arange(RATE * 2) / RATE
        clean = np.stack([(0.2 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)] * 2)
        assert detect_hum_base(clean, RATE) is None

    def test_drift_follows_measured_peak(self) -> None:
        # 电网 50.02 Hz：容差内跟随实测峰（notch 打在实际频点上）。
        t = np.arange(RATE * 2) / RATE
        hum = 0.05 * np.sin(2 * np.pi * 50.02 * t)
        tone = 0.2 * np.sin(2 * np.pi * 440.0 * t)
        sig = (hum + tone).astype(np.float32)
        pcm = np.stack([sig, sig])
        cleaned = remove_hum(pcm, RATE, base_hz=50.0, harmonics=(1,))
        before = _band_energy(pcm, 50.02, bw=0.5)
        after = _band_energy(cleaned, 50.02, bw=0.5)
        assert 20.0 * np.log10(before / max(after, 1e-9)) >= 12.0

    @pytest.mark.parametrize("freq", [55.0, 58.0])
    def test_auto_hum_preserves_nearby_bass_tones(self, freq: float) -> None:
        t = np.arange(RATE * 2 // 5) / RATE
        pcm = np.sin(2 * np.pi * freq * t).astype(np.float32)[None]
        assert detect_hum_base(pcm, RATE) is None
        assert remove_hum(pcm, RATE) is pcm

    def test_single_window_noise_does_not_look_like_a_mains_tone(self) -> None:
        pcm = np.random.default_rng(11).normal(0.0, 0.04, (2, RATE)).astype(np.float32)
        assert remove_hum(pcm, RATE) is pcm

    def test_high_q_notches_follow_one_grid_clock(self) -> None:
        t = np.arange(RATE * 8) / RATE
        base = 59.72
        clean = 0.2 * np.sin(2 * np.pi * 437.0 * t)
        hum = 0.05 * np.sin(2 * np.pi * base * t) + 0.07 * np.sin(2 * np.pi * (3 * base) * t)
        pcm = np.stack([clean, clean + hum]).astype(np.float32)
        assert detect_hum_base(pcm, RATE) == 60.0
        fixed = remove_hum(pcm, RATE, harmonics=(1, 3), q=120.0)
        for freq in (base, 3 * base):
            before = _band_energy(pcm[1:, RATE:-RATE], freq, bw=0.2)
            after = _band_energy(fixed[1:, RATE:-RATE], freq, bw=0.2)
            assert 20.0 * np.log10(before / after) >= 24.0
        original_tone = _band_energy(pcm[1:], 437.0, bw=0.5)
        result_tone = _band_energy(fixed[1:], 437.0, bw=0.5)
        assert abs(20.0 * np.log10(original_tone / result_tone)) < 0.1

    def test_identity_paths_preserve_double_precision_samples(self) -> None:
        data = np.random.default_rng(31).normal(0.0, 0.01, (2, RATE))
        assert remove_hum(data, RATE, base_hz=50.0, reduction_db=0.0) is data
        assert remove_hum(data, RATE, base_hz=50.0, harmonics=()) is data
        assert remove_hum(data, RATE, base_hz=50.0, region=(100, 100)) is data
        fixed = remove_hum(data, RATE, base_hz=50.0, region=(1000, 2000))
        assert fixed.dtype == data.dtype
        np.testing.assert_array_equal(fixed[:, :1000], data[:, :1000])
        np.testing.assert_array_equal(fixed[:, 2000:], data[:, 2000:])

    def test_cancelled_after_final_filter_returns_original(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core import de_click_dehum

        original_filter = de_click_dehum.sosfiltfilt
        cancelled = False

        def filter_and_cancel(sos: np.ndarray, signal: np.ndarray, *, padlen: int) -> np.ndarray:
            nonlocal cancelled
            filtered = original_filter(sos, signal, padlen=padlen)
            cancelled = True
            return filtered

        monkeypatch.setattr(de_click_dehum, "sosfiltfilt", filter_and_cancel)
        pcm = _hum_tone()[:1]
        fixed = remove_hum(pcm, RATE, base_hz=50.0, harmonics=(1,), cancel_check=lambda: cancelled)
        assert cancelled
        assert fixed is pcm


class TestDeClick:
    def _clean_with_clicks(self, positions):
        t = np.arange(2 * RATE) / RATE
        sig = 0.1 * np.sin(2 * np.pi * 300.0 * t)
        pcm = np.stack([sig.astype(np.float32)] * 2)
        for pos in positions:
            pcm[:, pos:pos + 6] += 0.8
        return pcm

    def test_sparse_clicks_located_and_repaired(self) -> None:
        pcm = self._clean_with_clicks([30000, 60000])
        candidates = detect_clicks(pcm, RATE)
        found = [c.position for c in candidates]
        assert any(abs(p - 30000) < 64 for p in found)
        assert any(abs(p - 60000) < 64 for p in found)
        cleaned, handled = remove_clicks(pcm, RATE)
        assert len(handled) == len(candidates)
        # 修复后点击位置峰值显著回落。
        around = cleaned[:, 29990:30020]
        assert float(np.max(np.abs(around))) < 0.4

    def test_drum_transient_preserved(self) -> None:
        """宽带瞬态（鼓点）不被误修：全带能量比值触发 veto。"""
        t = np.arange(2 * RATE) / RATE
        sig = 0.05 * np.sin(2 * np.pi * 300.0 * t)
        pcm = np.stack([sig.astype(np.float32)] * 2)
        # 鼓点：宽带持续瞬态（τ≈8 ms、~25 ms 长度——真实鼓击衰减）。
        for start in (48000, 72000):
            decay = np.exp(-np.arange(1200) / 400.0).astype(np.float32)
            noise = np.random.default_rng(start).standard_normal(1200)
            burst = (0.7 * decay * noise).astype(np.float32)
            pcm[:, start:start + 1200] += burst
        candidates = detect_clicks(pcm, RATE)
        # 鼓点位置不出现在候选里（宽带持续瞬态 veto）。
        assert not any(abs(c.position - 48000) < 1200 for c in candidates)
        assert not any(abs(c.position - 72000) < 1200 for c in candidates)

    def test_no_clicks_clean_passthrough(self) -> None:
        t = np.arange(RATE) / RATE
        sig = (0.1 * np.sin(2 * np.pi * 300.0 * t)).astype(np.float32)
        pcm = np.stack([sig, sig])
        cleaned, handled = remove_clicks(pcm, RATE)
        assert handled == []
        np.testing.assert_array_equal(cleaned, pcm)

    def test_broadband_noise_is_preserved_but_an_injected_click_is_repaired(self) -> None:
        noise = np.random.default_rng(778).normal(0.0, 0.03, (2, 4 * RATE)).astype(np.float32)
        assert not detect_clicks(noise, RATE)
        clean, handled = remove_clicks(noise, RATE)
        assert clean is noise and not handled

        dirty = noise.copy()
        dirty[1, RATE:RATE + 6] += .45
        fixed, handled = remove_clicks(dirty, RATE)
        assert [candidate.position for candidate in handled] == [RATE]
        assert handled[0].confidence == pytest.approx(.75)
        assert np.max(np.abs(fixed[1, RATE:RATE + 6] - noise[1, RATE:RATE + 6])) < .09
        np.testing.assert_array_equal(fixed[0], noise[0])
        np.testing.assert_array_equal(fixed[:, :RATE - 128], noise[:, :RATE - 128])
        np.testing.assert_array_equal(fixed[:, RATE + 128:], noise[:, RATE + 128:])

    def test_isolated_click_in_digital_silence(self) -> None:
        pcm = np.zeros((2, RATE), dtype=np.float32)
        pcm[0, 20000:20006] = 0.8
        fixed, handled = remove_clicks(pcm, RATE)
        assert [candidate.position for candidate in handled] == [20000]
        assert handled[0].confidence == pytest.approx(0.75)
        assert not np.any(fixed)
        np.testing.assert_array_equal(pcm[0, 20000:20006], np.full(6, 0.8, dtype=np.float32))

    def test_long_repair_reuses_spectral_context_locally(self) -> None:
        from src.features.edit.core.audio_repair import repair_spectral

        pcm = self._clean_with_clicks([])
        position, width = 1720, 700
        lo, hi = position - width, position + width
        pcm[0, lo:hi] *= 0.2
        candidate = ClickCandidate(position, width, 1.0)
        fixed, handled = remove_clicks(pcm, RATE, candidates=[candidate])
        low_hz, high_hz = 20.0, RATE * 0.45
        expected = repair_spectral(
            pcm, RATE,
            [{"sample": position, "hz": np.sqrt(low_hz * high_hz), "time_radius": width,
              "bandwidth_oct": np.log2(high_hz / low_hz)}],
            (lo, hi), amount_db=24.0, mode="heal")
        assert handled == [candidate]
        np.testing.assert_array_equal(fixed, expected)

    def test_cancellation_rolls_back_short_repair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core import de_click_dehum

        original_repair = de_click_dehum._interpolate_repair
        cancelled = False

        def repair_and_cancel(
            signal: np.ndarray, lo: int, hi: int, *, out: np.ndarray | None = None,
            channels: tuple[int, ...] | None = None,
        ) -> np.ndarray:
            nonlocal cancelled
            repaired = original_repair(signal, lo, hi, out=out, channels=channels)
            cancelled = True
            return repaired

        monkeypatch.setattr(de_click_dehum, "_interpolate_repair", repair_and_cancel)
        pcm = self._clean_with_clicks([20000])
        fixed, handled = remove_clicks(
            pcm, RATE, candidates=[ClickCandidate(20000, 16, 1.0)], cancel_check=lambda: cancelled)
        assert cancelled
        assert fixed is pcm
        assert handled == []


class TestFX07:
    """FX-07（2026-11-21 计划）：衰减受限 hum、region 写回、坐标修复。"""

    def test_hum_reduction_limited(self) -> None:
        """reduction_db 上限：目标频点保留 10^(−red/20) 幅度。"""
        from src.features.edit.core.de_click_dehum import remove_hum
        t = np.arange(2 * RATE) / RATE
        clean = 0.2 * np.sin(2 * np.pi * 220.0 * t)
        hum = 0.15 * np.sin(2 * np.pi * 50.0 * t) + \
            0.08 * np.sin(2 * np.pi * 100.0 * t)
        dirty = (clean + hum)[None, :].astype(np.float32)

        def amp(pcm: np.ndarray, hz: float) -> float:
            steady = pcm[0, RATE // 4:]
            window = np.hanning(steady.size)
            phase = np.exp(-2j * np.pi * hz * np.arange(steady.size) / RATE)
            return float(2 * abs(np.sum(steady * window * phase)) / window.sum())

        out = remove_hum(dirty, RATE, base_hz=50.0, reduction_db=48.0)
        reduction = 20 * np.log10(amp(dirty, 50) / max(amp(out, 50), 1e-12))
        assert reduction >= 20.0
        loss = 20 * np.log10(amp(dirty, 220) / max(amp(out, 220), 1e-12))
        assert loss <= 0.5
        capped = remove_hum(dirty, RATE, base_hz=50.0, reduction_db=20.0)
        retained = amp(capped, 50) / max(amp(dirty, 50), 1e-12)
        assert retained == pytest.approx(10 ** (-20.0 / 20.0), abs=0.02)

    def test_region_write_back(self) -> None:
        """region 写回：选区外逐样本一致，选区内与全段处理一致。"""
        from src.features.edit.core.de_click_dehum import remove_hum
        t = np.arange(2 * RATE) / RATE
        dirty = (0.2 * np.sin(2 * np.pi * 220.0 * t)
                 + 0.15 * np.sin(2 * np.pi * 50.0 * t)
                 )[None, :].astype(np.float32)
        kwargs = dict(base_hz=50.0, reduction_db=48.0)
        region = remove_hum(dirty, RATE, region=(1000, 5000), **kwargs)
        full = remove_hum(dirty, RATE, **kwargs)
        np.testing.assert_array_equal(region[:, :1000], dirty[:, :1000])
        np.testing.assert_array_equal(region[:, 5000:], dirty[:, 5000:])
        np.testing.assert_array_equal(region[:, 1000:5000],
                                      full[:, 1000:5000])

    def test_ramp_clicks_detected_and_repaired(self) -> None:
        """斜坡六点污染：≥9/10 命中率口径（此处 6/6）、修复误差极小。"""
        from src.features.edit.core.de_click_dehum import detect_clicks, \
            remove_clicks
        ref = np.linspace(-0.15, 0.15, RATE, dtype=np.float32)
        pcm = np.stack([ref, ref]).copy()
        positions = (2000, 8000, 20000, 30000, 40000, 45000)
        for pos in positions:
            pcm[0, pos:pos + 6] += 0.8
            pcm[1, pos:pos + 6] += 0.5
        candidates = detect_clicks(pcm, RATE, threshold_sigma=8.0)
        hits = [c.position for c in candidates]
        hit_count = sum(1 for p in positions
                        if any(abs(h - p) <= 2 for h in hits))
        assert hit_count == len(positions)
        fixed, _ = remove_clicks(pcm, RATE, candidates=candidates)
        for pos in positions:
            err = float(np.max(np.abs(fixed[1, pos:pos + 6] - ref[pos:pos + 6])))
            assert err < 0.05
