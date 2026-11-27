"""P2 · 加倍/和声（FX-20 · 2026-11-21 计划）。

验收：220 Hz +12 st 主峰 440 Hz ±2 Hz；创作延迟误差 ≤1 样本；
等功率声像（pan ±100 → 单边）；detune ±6 cents 频差 ≤2 cents；长度
保持；总偏移越界（±24 st + ±100 cents）诊断；dry 参照增益固定。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.doubler import (
    DoublerVoice,
    render_doubler,
)

RATE = 48000


def _peak_hz(pcm: np.ndarray, channel: int = 0, skip: int = 0) -> float:
    steady = pcm[channel, skip:]
    windowed = steady * np.hanning(steady.size)
    bins = np.fft.rfftfreq(steady.size, 1.0 / RATE)
    return float(bins[np.abs(np.fft.rfft(windowed)).argmax()])


class TestDoubler:
    def test_fixed_octave_up(self) -> None:
        """220 Hz +12 st 声部 → 主峰 440 Hz ±2 Hz（dry 静音参照）。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        out = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=12.0, pan_pct=40.0,
                                     delay_ms=15.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        assert _peak_hz(out, skip=RATE // 2 + 720) == pytest.approx(
            440.0, abs=2.0)
        assert out.shape == dry.shape  # 长度保持

    def test_delay_sample_exact(self) -> None:
        """15 ms 创作延迟 → 声部脉冲落位 720 ±1 样本。"""
        pulse = np.zeros((2, 2 * RATE), dtype=np.float32)
        pulse[:, 0] = 1.0
        out = render_doubler(
            pulse, RATE, [DoublerVoice(interval_st=0.0, delay_ms=15.0,
                                       pan_pct=100.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        assert int(np.argmax(np.abs(out[1]))) == pytest.approx(720, abs=1)

    def test_equal_power_pan_extremes(self) -> None:
        """pan ±100 → 单边；pan 0 → 等电平双声道。"""
        pulse = np.zeros((2, 2 * RATE), dtype=np.float32)
        pulse[:, 0] = 1.0
        right = render_doubler(
            pulse, RATE, [DoublerVoice(pan_pct=100.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        assert float(np.max(np.abs(right[0]))) < 5e-3
        assert float(np.max(np.abs(right[1]))) > 0.5
        left = render_doubler(
            pulse, RATE, [DoublerVoice(pan_pct=-100.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        assert float(np.max(np.abs(left[1]))) < 5e-3
        assert float(np.max(np.abs(left[0]))) > 0.5

    def test_detune_six_cents(self) -> None:
        """detune +6 cents → 频差 ≤2 cents（220 → 220.76 Hz）。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        out = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=0.0, detune_cents=6.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        measured = _peak_hz(out, skip=RATE // 2)
        expected = 220.0 * 2 ** (6.0 / 1200.0)
        assert 1200.0 * np.log2(measured / 220.0) == pytest.approx(
            6.0, abs=2.0)
        assert measured == pytest.approx(expected, abs=0.5)

    def test_total_offset_range_validated(self) -> None:
        """±24 st + ±100 cents 越界 → 诊断；±24 st 边缘可生成。"""
        t = np.arange(RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        with pytest.raises(ValueError):
            render_doubler(dry, RATE, [DoublerVoice(interval_st=24.0,
                                                    detune_cents=100.0)])
        edge = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=24.0, detune_cents=0.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        assert edge.shape == dry.shape

    def test_mute_and_dry_reference(self) -> None:
        """mute 声部不参与；dry 参照增益固定（增删声部不重归一化）。"""
        t = np.arange(RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        muted = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=12.0, mute=True)],
            dry_gain_db=0.0, wet_mix_pct=100.0)
        np.testing.assert_allclose(muted, dry, rtol=0, atol=1e-6)
        # 两个相同声部叠加 → 线性求和（固定参照，无重归一化；
        # pitch_shift 对稳态音有自身窗口缩放，峰值的绝对值由调用方控制）。
        one = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=0.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        doubled = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=0.0),
                        DoublerVoice(interval_st=0.0)],
            dry_gain_db=-120.0, wet_mix_pct=100.0)
        ratio = float(np.max(np.abs(doubled))) / max(
            float(np.max(np.abs(one))), 1e-9)
        assert ratio == pytest.approx(2.0, abs=0.02)

    def test_dry_delay_alignment(self) -> None:
        """带延迟声部 + dry：延迟只作用于声部（440 Hz 频带能量在延迟
        之后才开始出现；干声 220 Hz 自始存在）。"""
        t = np.arange(2 * RATE) / RATE
        dry = (0.2 * np.sin(2 * np.pi * 220.0 * t))[None, :].astype(
            np.float32)
        out = render_doubler(
            dry, RATE, [DoublerVoice(interval_st=12.0, delay_ms=15.0)],
            dry_gain_db=0.0, wet_mix_pct=100.0)

        def band_amp(start: int, end: int, hz: float) -> float:
            seg = out[0, start:end] * np.hanning(end - start)
            return float(2 * abs(np.fft.rfft(seg)[int(round(
                hz * (end - start) / RATE))]) / (end - start))

        before = band_amp(0, 700, 440.0)        # 延迟前：无 440 声部
        after = band_amp(1000, 90000, 440.0)    # 延迟后：440 声部内容
        assert before < after * 0.1
        dry_present = band_amp(0, 90000, 220.0)
        assert dry_present > 0.05               # 干声未被延迟/衰减

    def test_equal_intervals_share_shift_without_sharing_voice_controls(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.features.edit.core.audio_effects import doubler

        phase = 2.0 * np.pi * 220.0 * np.arange(RATE) / RATE
        source = np.stack([np.sin(phase), 0.5 * np.cos(phase)]).astype(np.float32)
        voices = [
            DoublerVoice(interval_st=3.0, detune_cents=6.0, pan_pct=-75.0, gain_db=-8.0),
            DoublerVoice(interval_st=3.06, delay_ms=20.0, pan_pct=35.0, gain_db=-4.0),
        ]
        bypass = render_doubler(source, RATE, [], dry_gain_db=-8.0)
        isolated = [render_doubler(source, RATE, [voice], dry_gain_db=-8.0) for voice in voices]
        actual_shift = doubler.pitch_shift_fixed_length
        calls: list[float] = []

        def count_shift(pcm: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
            calls.append(semitones)
            return actual_shift(pcm, sample_rate, semitones)

        monkeypatch.setattr(doubler, "pitch_shift_fixed_length", count_shift)
        result = render_doubler(source, RATE, voices, dry_gain_db=-8.0)
        assert calls == [3.06]
        np.testing.assert_allclose(result, isolated[0] + isolated[1] - bypass, atol=2e-7)

    @pytest.mark.parametrize(("wet_mix_pct", "delay_ms"), [(0.0, 0.0), (100.0, 100.0)])
    def test_inaudible_voice_skips_shift(
        self, monkeypatch: pytest.MonkeyPatch, wet_mix_pct: float, delay_ms: float,
    ) -> None:
        from src.features.edit.core.audio_effects import doubler

        def unexpected_shift(pcm: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
            pytest.fail("Inaudible voice reached the pitch shifter")

        monkeypatch.setattr(doubler, "pitch_shift_fixed_length", unexpected_shift)
        source = np.ones((2, RATE // 20), dtype=np.float32)
        voice = DoublerVoice(interval_st=12.0, delay_ms=delay_ms)
        out = render_doubler(source, RATE, [voice], wet_mix_pct=wet_mix_pct)
        np.testing.assert_array_equal(out, source)
        with pytest.raises(ValueError, match="voice interval"):
            render_doubler(source, RATE, [DoublerVoice(interval_st=25.0)], wet_mix_pct=0.0)

    def test_cancellation_during_final_voice_returns_source(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from src.features.edit.core.audio_effects import doubler

        actual_shift = doubler.pitch_shift_fixed_length
        cancelled = False

        def shift_and_cancel(pcm: np.ndarray, sample_rate: int, semitones: float) -> np.ndarray:
            nonlocal cancelled
            result = actual_shift(pcm, sample_rate, semitones)
            cancelled = True
            return result

        monkeypatch.setattr(doubler, "pitch_shift_fixed_length", shift_and_cancel)
        source = np.ones((2, RATE // 2), dtype=np.float32)
        out = render_doubler(source, RATE, [DoublerVoice(interval_st=3.0)], cancel_check=lambda: cancelled)
        assert cancelled and out is source

    @pytest.mark.parametrize(("duration_ms", "tolerance_hz"), [(25, 25.0), (40, 8.0), (60, 8.0)])
    def test_short_octave_voice_has_wet_audio_at_both_ends(
        self, duration_ms: int, tolerance_hz: float,
    ) -> None:
        phase = 2.0 * np.pi * 220.0 * np.arange(RATE * duration_ms // 1000) / RATE + 0.3
        source = (0.2 * np.sin(phase)).astype(np.float32)[None]
        out = render_doubler(source, RATE, [DoublerVoice(interval_st=12.0)], dry_gain_db=-120.0)
        assert out.shape == source.shape
        quarter = out.shape[-1] // 4
        level_start = np.sqrt(np.mean(out[0, :quarter] ** 2))
        level_end = np.sqrt(np.mean(out[0, -quarter:] ** 2))
        assert level_end == pytest.approx(level_start, rel=0.2)
        spectrum = np.abs(np.fft.rfft(out[0] * np.hanning(out.shape[-1]), n=16384))
        peak = spectrum.argmax() * RATE / 16384
        assert peak == pytest.approx(440.0, abs=tolerance_hz)
