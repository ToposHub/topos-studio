"""P2 · 变声器计量校准（FX-21 · 2026-11-21 计划）。

验收（计划实验口径）：三模块关闭、−6.02 dB 输出衰减、右声道单边
有声输入 → 参考声道按能量选择（右），逐声道 RMS/峰值表计与独立
测量一致，输出 F0 读参考声道。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_effects.voice_changer import VoiceChangerEffect

RATE = 48000


def _right_only_fixture() -> np.ndarray:
    t = np.arange(RATE // 10) / RATE
    right = 0.25 * np.sin(2 * np.pi * 440.0 * t)
    return np.stack((np.zeros_like(right), right)).astype(np.float32)


class TestMetering:
    def test_reference_channel_by_energy(self) -> None:
        """右声道单边有声：F0 参考声道 = 1（左静音不误报无声）。"""
        fx = VoiceChangerEffect(RATE)
        fx.set_params({"enabled": True, "formant_enabled": False,
                       "pitch_enabled": False, "vocoder_enabled": False,
                       "output_gain_db": 20 * np.log10(0.5)})
        out = fx.process(_right_only_fixture())
        tel = fx.collect_telemetry()
        power = np.mean(out.astype(np.float64) ** 2, axis=1)
        assert int(np.argmax(power)) == 1
        assert tel["ref_channel_in"] == 1
        assert tel["ref_channel_out"] == 1
        assert tel["f0_out_hz"] == pytest.approx(440.0, abs=2.0)

    def test_per_channel_rms_and_peak(self) -> None:
        """逐声道 RMS/绝对峰值与独立测量一致（−6.02 dB 输出）。"""
        fx = VoiceChangerEffect(RATE)
        fx.set_params({"enabled": True, "formant_enabled": False,
                       "pitch_enabled": False, "vocoder_enabled": False,
                       "output_gain_db": 20 * np.log10(0.5)})
        pcm = _right_only_fixture()
        out = fx.process(pcm.copy())
        tel = fx.collect_telemetry()
        power = np.mean(out.astype(np.float64) ** 2, axis=1)
        ref = int(np.argmax(power))
        assert tel["out_rms"][ref] == pytest.approx(
            float(np.sqrt(power[ref])), abs=1e-6)
        assert tel["out_peak"][ref] == pytest.approx(
            float(np.max(np.abs(out[ref]))), abs=1e-6)
        assert tel["in_peak"][1] == pytest.approx(0.25, abs=1e-6)
        np.testing.assert_array_equal(out[0], pcm[0])   # 左声道原样
        np.testing.assert_allclose(out[1], pcm[1] * 0.5, atol=1e-7)

    def test_telemetry_before_process_safe(self) -> None:
        fx = VoiceChangerEffect(RATE)
        tel = fx.collect_telemetry()
        assert tel["in_peak"] == [0.0, 0.0]

    def test_f0_range_reported(self) -> None:
        fx = VoiceChangerEffect(RATE)
        fx.set_params({"enabled": True, "pitch_tracking": "wide"})
        tel = fx.collect_telemetry()
        assert len(tel["f0_range_hz"]) == 2
        assert tel["f0_range_hz"][0] < tel["f0_range_hz"][1]
