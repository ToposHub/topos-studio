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

        def amp(pcm, hz):
            w = pcm[0, RATE // 4:] * np.hanning(pcm.shape[-1] - RATE // 4)
            return 2 * abs(np.fft.rfft(w)[hz]) / w.size

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
