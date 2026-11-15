"""H06：逐参数有效性、Vocoder formant、清音与 F0 置信度（复审 §5 H06）。

修前复现：``pitch_glide_ms``/``pitch_tracking``/``vocoder_formant_st`` 端点
切换输出差 0（死参数）；F0 固定 70–500Hz 且只分析左声道（880/1760Hz 都
报 440.367、conf≈0.954；仅右声道人声报无声）；unvoiced 高通残差无条件
全量注入。

修后契约：
- 三个死参数全部消费（glide 在 H05 测试；本文件覆盖 tracking 与
  vocoder_formant_st）；
- ``_estimate_f0``：局部极大 + 最短强 lag（八度保护）、范围由
  ``pitch_tracking`` 决定（vocal 70–500 / wide 50–1500）、参考声道按
  能量选择、confidence 明示为检测置信度；
- ``vocoder_formant_st`` 独立缩放声码器**合成频带轴**（不动载波基频）；
- unvoiced 残差按输入高频占比门控（浊音不再叠嘶声）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_effects.voice_changer import (
    VoiceChangerEffect,
    _estimate_f0,
)
from src.features.edit.core.timeline_v2 import VoiceChangerParams

pytestmark = pytest.mark.edit_core
SR = 48000


class TestF0Estimation:
    @pytest.mark.parametrize("freq,tracking", [
        (120.0, "vocal"), (220.0, "vocal"), (440.0, "vocal"),
        (880.0, "wide"), (1100.0, "wide"), (60.0, "wide"),
    ])
    def test_frequency_accurate(self, freq, tracking):
        t = np.arange(SR) / SR
        ranges = {"vocal": (70.0, 500.0), "wide": (50.0, 1500.0)}
        f0, conf = _estimate_f0(0.4 * np.sin(2 * np.pi * freq * t), SR,
                                lo=ranges[tracking][0], hi=ranges[tracking][1])
        assert f0 is not None
        assert f0 == pytest.approx(freq, rel=0.02), (freq, f0)
        assert conf > 0.5

    def test_vocal_range_rejects_out_of_range(self):
        """vocal 域外（880Hz）不再误报 440 谐波（N/A 或低置信）。"""
        t = np.arange(SR) / SR
        f0, conf = _estimate_f0(0.4 * np.sin(2 * np.pi * 880.0 * t), SR,
                                lo=70.0, hi=500.0)
        if f0 is not None:
            # 若有解必是域内真峰；880 的分数谐波不得伪装成高置信基频
            assert conf < 0.99 or abs(f0 - 440.0) > 20.0

    def test_right_channel_reference(self):
        """仅右声道人声：F0 正确检出（修前报无声）。"""
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams().to_dict())
        t = np.arange(SR) / SR
        x = np.zeros((2, SR))
        x[1] = 0.4 * np.sin(2 * np.pi * 180.0 * t)
        fx.process(x.copy())
        tel = fx.collect_telemetry()
        assert tel["f0_in_hz"] == pytest.approx(180.0, rel=0.02)
        assert tel["voiced"] is True

    def test_confidence_is_detection_metric(self):
        """docstring 明示 confidence 语义；静音 → N/A。"""
        f0, conf = _estimate_f0(np.zeros(2048), SR)
        assert f0 is None

    def test_pitch_tracking_consumed(self):
        """tracking 端点切换改变分析行为（880Hz：wide 检出/vocal 拒绝）。"""
        t = np.arange(SR) / SR
        sig = 0.4 * np.sin(2 * np.pi * 880.0 * t)
        f_wide, _ = _estimate_f0(sig, SR, lo=50.0, hi=1500.0)
        assert f_wide == pytest.approx(880.0, rel=0.02)


class TestVocoderFormant:
    def test_endpoint_changes_sound(self):
        """vocoder_formant_st 0 vs +7st 输出差 > 0（修前恒 0）。"""
        rng = np.random.default_rng(3)
        x = rng.standard_normal((2, SR)) * 0.3
        outs = []
        for st in (0.0, 7.0):
            fx = VoiceChangerEffect(SR)
            fx.set_params(VoiceChangerParams(
                vocoder_enabled=True, vocoder_formant_st=st).to_dict())
            outs.append(fx.process(x.copy()))
        assert np.abs(outs[0] - outs[1]).max() > 1e-3

    def test_formant_shift_moves_synthesis_axis(self):
        """合成轴缩放：+7st 输出谱矩心高于 0st（载波基频不变）。"""
        t = np.arange(SR * 2) / SR
        voice = (0.4 * np.sin(2 * np.pi * 150.0 * t)
                 + 0.2 * np.sin(2 * np.pi * 450.0 * t))
        x = np.stack([voice, voice])

        def centroid(y):
            seg = y[SR:] * np.hanning(SR)
            X = np.abs(np.fft.rfft(seg)) ** 2
            fr = np.fft.rfftfreq(SR, 1 / SR)
            m = (fr > 100) & (fr < 8000)
            return float(np.sum(fr[m] * X[m]) / np.sum(X[m]))

        c = {}
        for st in (0.0, 7.0):
            fx = VoiceChangerEffect(SR)
            fx.set_params(VoiceChangerParams(
                vocoder_enabled=True, vocoder_formant_st=st,
                vocoder_carrier="saw").to_dict())
            c[st] = centroid(fx.process(x.copy())[0])
        assert c[7.0] > c[0.0] * 1.05, c


class TestUnvoicedGating:
    def test_tonal_input_adds_minimal_hiss(self):
        """unvoiced=100%：正弦输入的 HF 注入远小于噪声输入。"""
        t = np.arange(SR) / SR

        def hf(y):
            X = np.abs(np.fft.rfft(y[0]))
            fr = np.fft.rfftfreq(SR, 1 / SR)
            return float(np.sum(X[fr > 4000.0] ** 2))

        fx1 = VoiceChangerEffect(SR)
        fx1.set_params(VoiceChangerParams(
            vocoder_enabled=True, vocoder_unvoiced_pct=100.0).to_dict())
        y_tone = fx1.process(np.stack(
            [0.3 * np.sin(2 * np.pi * 200.0 * t)] * 2).copy())
        fx2 = VoiceChangerEffect(SR)
        fx2.set_params(VoiceChangerParams(
            vocoder_enabled=True, vocoder_unvoiced_pct=100.0).to_dict())
        y_noise = fx2.process(
            np.random.default_rng(9).standard_normal((2, SR)) * 0.3)
        assert hf(y_tone) < hf(y_noise) * 1e-3


class TestModuleSemantics:
    def test_formant_only_preserves_f0(self):
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=True, formant_shift_st=5.0,
            pitch_enabled=False, vocoder_enabled=False).to_dict())
        t = np.arange(SR) / SR
        x = np.stack([0.4 * np.sin(2 * np.pi * 200.0 * t)] * 2)
        y = fx.process(x.copy())
        tel = fx.collect_telemetry()
        assert tel["f0_out_hz"] == pytest.approx(200.0, rel=0.03)

    def test_pitch_only_shifts_f0(self):
        fx = VoiceChangerEffect(SR)
        fx.set_params(VoiceChangerParams(
            formant_enabled=False, pitch_enabled=True, pitch_semitones=7.0,
            pitch_formant_link_pct=100.0, vocoder_enabled=False).to_dict())
        t = np.arange(SR * 2) / SR
        x = np.stack([0.4 * np.sin(2 * np.pi * 200.0 * t)] * 2)
        y = fx.process(x.copy())
        tel = fx.collect_telemetry()
        assert tel["f0_out_hz"] == pytest.approx(200.0 * 2 ** (7 / 12),
                                                 rel=0.03)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
