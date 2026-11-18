"""P2-7 变速与音高：长度合同、主频保持与时长保持（roadmap §6.3）。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_timepitch import (
    dominant_frequency,
    pitch_shift,
    retime_nopreserve,
    retime_preserve,
)

SR = 48000


def sine_pcm(freq: float, seconds: float, channels: int = 1) -> np.ndarray:
    t = np.arange(int(SR * seconds), dtype=np.float64) / SR
    signal = 0.4 * np.sin(2 * np.pi * freq * t)
    return np.stack([signal] * channels).astype(np.float32)


class TestRetime:
    def test_output_length_contract(self) -> None:
        pcm = sine_pcm(440.0, 2.0)
        out = retime_nopreserve(pcm, SR, 150.0)
        assert out.shape == (1, round(pcm.shape[1] / 1.5))
        out2 = retime_preserve(pcm, SR, 150.0)
        assert abs(out2.shape[1] - round(pcm.shape[1] / 1.5)) <= 1

    def test_preserve_keeps_pitch_at_150(self) -> None:
        pcm = sine_pcm(440.0, 3.0)
        out = retime_preserve(pcm, SR, 150.0)
        assert abs(dominant_frequency(out, SR) - 440.0) <= 5.0

    def test_nopreserve_shifts_pitch(self) -> None:
        pcm = sine_pcm(440.0, 3.0)
        out = retime_nopreserve(pcm, SR, 150.0)
        # 变调重采样：150% 速度 → 频率 ×1.5。
        assert abs(dominant_frequency(out, SR) - 660.0) <= 8.0

    def test_identity_and_range_checks(self) -> None:
        pcm = sine_pcm(440.0, 0.5)
        out = retime_preserve(pcm, SR, 100.0)
        np.testing.assert_array_equal(out, pcm)
        with pytest.raises(ValueError):
            retime_preserve(pcm, SR, 500.0)
        with pytest.raises(ValueError):
            retime_nopreserve(pcm, SR, 10.0)

    def test_stereo_length_matches_mono(self) -> None:
        stereo = sine_pcm(440.0, 1.0, channels=2)
        mono = stereo[:1]
        out_s = retime_preserve(stereo, SR, 130.0)
        out_m = retime_preserve(mono, SR, 130.0)
        assert out_s.shape[1] == out_m.shape[1]


class TestPitch:
    def test_plus_12_semitones_doubles_frequency_keeps_duration(self) -> None:
        pcm = sine_pcm(440.0, 2.0)
        out = pitch_shift(pcm, SR, +12.0)
        assert abs(out.shape[1] - pcm.shape[1]) <= max(64, pcm.shape[1] // 1000)
        assert abs(dominant_frequency(out, SR) - 880.0) <= 12.0

    def test_minus_12_semitones(self) -> None:
        pcm = sine_pcm(880.0, 2.0)
        out = pitch_shift(pcm, SR, -12.0)
        assert abs(dominant_frequency(out, SR) - 440.0) <= 10.0

    def test_zero_is_identity(self) -> None:
        pcm = sine_pcm(440.0, 0.4)
        out = pitch_shift(pcm, SR, 0.0)
        np.testing.assert_array_equal(out, pcm)

    def test_range_check(self) -> None:
        pcm = sine_pcm(440.0, 0.4)
        with pytest.raises(ValueError):
            pitch_shift(pcm, SR, 36.0)
