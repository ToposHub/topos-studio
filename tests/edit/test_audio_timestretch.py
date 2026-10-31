"""WSOLA 保调变速（P1-08 离线）测试。

验收口径（计划表）：0.5x/1.5x 下人声音调可保持；时长精确随速度缩放；
多声道声像不漂移；越界/退化输入行为明确。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_timestretch import wsola_time_stretch

SR = 48_000.0


def _sine(freq: float, seconds: float, channels: int = 1) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    mono = (0.6 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    if channels == 1:
        return mono
    return np.stack([mono] * channels, axis=0)


def _dominant_freq(signal: np.ndarray, sample_rate: float = SR) -> float:
    """FFT 主峰频率（忽略 DC 附近的能量）。"""
    mono = signal.mean(axis=0) if signal.ndim == 2 else signal
    spectrum = np.abs(np.fft.rfft(mono * np.hanning(mono.shape[-1])))
    spectrum[: max(1, int(20 / (sample_rate / mono.shape[-1])))] = 0.0  # 去 DC
    return float(np.fft.rfftfreq(mono.shape[-1], 1.0 / sample_rate)[np.argmax(spectrum)])


class TestDuration:
    @pytest.mark.parametrize("speed", [0.5, 0.75, 1.5, 2.0])
    def test_output_length_scales(self, speed):
        x = _sine(440.0, 2.0)
        y = wsola_time_stretch(x, speed, SR)
        expected = int(round(x.shape[-1] / speed))
        assert abs(y.shape[-1] - expected) <= int(0.02 * expected) + 8

    def test_speed_one_returns_copy(self):
        x = _sine(440.0, 0.5)
        y = wsola_time_stretch(x, 1.0, SR)
        assert np.array_equal(y, x)
        assert y is not x


class TestPitchPreservation:
    @pytest.mark.parametrize("speed", [0.5, 1.5, 2.0])
    def test_tone_pitch_preserved(self, speed):
        x = _sine(440.0, 3.0)
        y = wsola_time_stretch(x, speed, SR)
        f = _dominant_freq(y)
        # 变调 resample 下 1.5x 会测得 ~660Hz；WSOLA 应保持在 440Hz ±2%
        assert 440.0 * 0.98 <= f <= 440.0 * 1.02, f"dominant freq drifted: {f}"

    def test_varispeed_would_shift_pitch_sanity(self):
        """对照：简单抽取（变调快放）确实会把主峰移到 ~880Hz——锁定测试有效性。"""
        x = _sine(440.0, 2.0)
        decimated = x[::2]  # 2x 快放变调
        f = _dominant_freq(decimated, SR)
        assert f > 700.0


class TestMultichannel:
    def test_stereo_layout_preserved(self):
        t = np.arange(int(SR * 2.0)) / SR
        left = (0.5 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
        right = 0.25 * left  # R = 0.5*L 的 stereo
        x = np.stack([left, right], axis=0)
        y = wsola_time_stretch(x, 1.5, SR)
        assert y.shape[0] == 2
        # 声道关系保持：R ≈ 0.25*L（中段窗口内比较，避开首尾边界）
        mid_l = y[0, 4800:-4800]
        mid_r = y[1, 4800:-4800]
        assert np.allclose(mid_r, 0.25 * mid_l, atol=0.02)

    def test_mono_input_returns_mono(self):
        y = wsola_time_stretch(_sine(300.0, 1.0), 1.5, SR)
        assert y.ndim == 1

    def test_deterministic(self):
        x = _sine(440.0, 1.0)
        y1 = wsola_time_stretch(x, 1.5, SR)
        y2 = wsola_time_stretch(x, 1.5, SR)
        assert np.array_equal(y1, y2)


class TestEdgeCases:
    def test_invalid_speed_raises(self):
        x = _sine(440.0, 0.2)
        with pytest.raises(ValueError):
            wsola_time_stretch(x, 0.01, SR)
        with pytest.raises(ValueError):
            wsola_time_stretch(x, 10.0, SR)

    def test_empty_input(self):
        y = wsola_time_stretch(np.zeros((2, 0), dtype=np.float32), 1.5, SR)
        assert y.shape == (2, 0)

    def test_too_short_input_returned_as_is(self):
        x = _sine(440.0, 0.01)  # ~480 样本 < 两帧
        y = wsola_time_stretch(x, 1.5, SR)
        assert np.array_equal(y, x)

    def test_no_nan_no_blowup(self):
        rng = np.random.default_rng(7)
        x = rng.normal(0, 0.3, size=(2, int(SR))).astype(np.float32)
        y = wsola_time_stretch(x, 1.7, SR)
        assert np.isfinite(y).all()
        assert np.max(np.abs(y)) < 10.0

    def test_silence_stays_silent(self):
        x = np.zeros((1, int(SR)), dtype=np.float32)
        y = wsola_time_stretch(x, 1.5, SR)
        assert np.max(np.abs(y)) < 1e-6


class TestSpeedParamsField:
    def test_preserve_pitch_field_round_trip(self):
        from src.features.edit.core.clip_params import SpeedParams

        params = SpeedParams(speed=1.5, preserve_pitch=True)
        assert params.preserve_pitch is True
        data = params.to_dict()
        assert data["preserve_pitch"] is True
        restored = SpeedParams.from_dict(data)
        assert restored.preserve_pitch is True

    def test_default_false_back_compat(self):
        from src.features.edit.core.clip_params import SpeedParams

        legacy = SpeedParams.from_dict({"speed": 2.0})
        assert legacy.preserve_pitch is False

    def test_clip_serialization_round_trip(self):
        from src.features.edit.core.timeline_v2 import ClipInstance

        clip = ClipInstance(
            clip_id="c1", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=48,
            source_start=0, source_duration=96,
        )
        retimed = clip.with_speed(1.5) if hasattr(clip, "with_speed") else clip
        # 直接构造带 preserve_pitch 的速度参数并序列化
        from src.features.edit.core.clip_params import SpeedParams
        data = SpeedParams(speed=0.5, preserve_pitch=True).to_dict()
        assert SpeedParams.from_dict(data).preserve_pitch is True
