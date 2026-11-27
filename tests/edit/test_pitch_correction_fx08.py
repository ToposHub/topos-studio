"""P2 · 音高校正（FX-08 · 2026-11-21 计划）。

验收：443 Hz → 440（中段误差 ≤5 cents）；A3 音阶内不动；白噪声
（无可信 F0）逐样本直通；bypass 音名直通；±40 cents 颤音收敛到同一
目标；静音峰值 ≤1e-7；长度严格保持；strength 0 精确直通；经
audio_edit 处理栈端到端渲染。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.pitch_correction import (
    quantize_cents,
    render_pitch_correction,
    track_f0,
)

RATE = 48000


def _tone(hz: float, seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    return (0.2 * np.sin(2 * np.pi * hz * t))[None, :].astype(np.float32)


def _peak_hz(pcm: np.ndarray, start: int, end: int) -> float:
    steady = pcm[0, start:end] * np.hanning(end - start)
    bins = np.fft.rfftfreq(steady.size, 1.0 / RATE)
    return float(bins[np.abs(np.fft.rfft(windowed := steady)).argmax()])


class TestTracking:
    def test_steady_tone(self) -> None:
        frames = track_f0(_tone(443.0), RATE)
        voiced = [f for f in frames if f.voiced]
        assert voiced
        assert voiced[len(voiced) // 2].f0_hz == pytest.approx(
            443.0, abs=2.0)

    def test_silence_has_no_f0(self) -> None:
        frames = track_f0(np.zeros((1, RATE), np.float32), RATE)
        assert all(not f.voiced for f in frames)

    def test_dc_does_not_hide_low_voice_in_another_channel(self) -> None:
        tone = _tone(65.0, 0.5)[0]
        dc = np.full_like(tone, 0.8)
        assert not any(frame.voiced for frame in track_f0(dc, RATE))
        frames = track_f0(np.stack([dc, tone]), RATE)
        assert frames and all(frame.voiced for frame in frames)
        assert float(np.median([frame.f0_hz for frame in frames])) == pytest.approx(65.0, abs=0.2)

    def test_vibrato_converges_to_one_target(self) -> None:
        """±40 cents 颤音：帧目标收敛到同一音名。"""
        t = np.arange(2 * RATE) / RATE
        vib = (0.2 * np.sin(2 * np.pi * 443.0 * t
                            + 0.3 * np.sin(2 * np.pi * 5.0 * t))
               )[None, :].astype(np.float32)
        targets = {quantize_cents(f.f0_hz)[0]
                   for f in track_f0(vib, RATE) if f.voiced}
        assert targets == {69}


class TestQuantization:
    def test_443_to_a4(self) -> None:
        note, cents = quantize_cents(443.0)
        assert note == 69
        assert -13.0 < cents < -10.0

    def test_bypass_note_passthrough(self) -> None:
        note, cents = quantize_cents(443.0, bypass_notes=("A",))
        assert cents == 0.0

    def test_reference_hz_shifts_target(self) -> None:
        """参考 A4 升到 442：440 Hz 不再是音准 → 校正方向翻转（+cents）。"""
        note_440, cents_440 = quantize_cents(440.0, ref_a4_hz=440.0)
        note_442, cents_442 = quantize_cents(440.0, ref_a4_hz=442.0)
        assert note_440 == 69 and cents_440 == 0.0
        assert note_442 == 69           # 68 = G# 不在 C 大调音阶内
        assert cents_442 > 2.0          # 需向上校正


class TestRender:
    def test_correction_to_scale(self) -> None:
        """443 → 440：中段误差 ≤5 cents（计划门槛）。"""
        note = _tone(443.0)
        out = render_pitch_correction(note, RATE)
        assert out.shape == note.shape
        peak = _peak_hz(out, RATE // 2, RATE // 2 + RATE)
        assert 1200.0 * np.log2(peak / 440.0) == pytest.approx(
            0.0, abs=5.0)

    def test_in_scale_note_untouched(self) -> None:
        """A3（音阶内）保持 220 Hz。"""
        out = render_pitch_correction(_tone(220.0), RATE)
        assert _peak_hz(out, RATE // 2, RATE // 2 + RATE) == pytest.approx(
            220.0, abs=2.0)

    def test_noise_passthrough(self) -> None:
        """白噪声（无可信 F0）逐样本直通。"""
        rng = np.random.default_rng(3)
        noise = (rng.standard_normal((1, RATE)) * 0.2).astype(np.float32)
        out = render_pitch_correction(noise, RATE)
        np.testing.assert_allclose(out, noise, atol=1e-6)

    def test_bypass_note_not_corrected(self) -> None:
        out = render_pitch_correction(_tone(443.0), RATE,
                                      bypass_notes=("A",))
        assert _peak_hz(out, RATE // 2, RATE // 2 + RATE) == pytest.approx(
            443.0, abs=2.0)

    def test_bypass_note_is_preserved_next_to_the_same_quantized_target(self) -> None:
        source = np.concatenate([_tone(415.3, 1.0), _tone(443.0, 1.0)], axis=-1)
        fixed = render_pitch_correction(source, RATE, scale_notes=(9,), bypass_notes=("A",))
        assert _peak_hz(fixed, RATE // 4, RATE * 3 // 4) == pytest.approx(440.0, abs=2.0)
        np.testing.assert_array_equal(fixed[:, RATE + RATE // 10:], source[:, RATE + RATE // 10:])

    def test_cancellation_during_final_shift_preserves_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core.audio_effects import pitch_correction

        shifting = pitch_correction.pitch_shift_fixed_length
        cancelled = False

        def shift_and_cancel(pcm: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
            nonlocal cancelled
            output = shifting(pcm, sample_rate, semitones)
            cancelled = True
            return output

        monkeypatch.setattr(pitch_correction, "pitch_shift_fixed_length", shift_and_cancel)
        source = _tone(443.0)
        output = render_pitch_correction(source, RATE, cancel_check=lambda: cancelled)
        assert cancelled and output is source

    def test_short_voiced_run_retains_shifted_audio_until_its_end(self) -> None:
        source = _tone(293.665, 0.07)
        fixed = render_pitch_correction(source, RATE, scale_notes=(7,), retune_ms=0.0)
        lo, hi = RATE // 50, RATE // 20
        tail = fixed[0, hi - RATE // 200:hi]
        assert np.sqrt(np.mean(tail * tail)) >= 0.10
        np.testing.assert_array_equal(fixed[:, :lo], source[:, :lo])
        np.testing.assert_array_equal(fixed[:, hi:], source[:, hi:])

    def test_silent_stays_silent(self) -> None:
        out = render_pitch_correction(np.zeros((1, RATE), np.float32), RATE)
        assert float(np.max(np.abs(out))) <= 1e-7

    def test_strength_zero_exact(self) -> None:
        note = _tone(443.0)
        out = render_pitch_correction(note, RATE, strength=0.0)
        np.testing.assert_array_equal(out, note)

    def test_length_preserved(self) -> None:
        note = _tone(443.0, 1.7)
        out = render_pitch_correction(note, RATE)
        assert out.shape == note.shape
