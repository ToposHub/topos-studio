"""MX-16 校准回归：Peak/RMS/LUFS 表计可信度（阶段G）。

验收（计划 §5 MX-16）：
- 满幅正弦 sample peak ≈ 0 dBFS、未偏置 RMS ≈ -3.01 dBFS；
- LUFS 对照公开参考：1 kHz 正弦 @ -23 dBFS 双单声道 → -23 LUFS ±0.1
  （EBU R128/BS.1770-4 校准点；K 加权在 1kHz 的 +0.69dB 与 -0.691
  偏移相抵，0 dBFS 双单声道 → ≈0 LUFS）；
- 反相立体声（L=-R）逐声道测量不相消（相消只允许发生在显式 mono 折叠）；
- 引擎 TrackLevel 峰值/RMS 与解析公式一致（payload dB 转换的单一真源）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState
from src.features.edit.core.loudness_meter import LoudnessMeter

SR = 48000.0
FPS = 24.0
BLOCK = 480


class _SineDecoder:
    def __init__(self, amp, freq=997.0, frames=(0,)):
        self.amp = amp
        self.freq = freq
        self.frames = set(frames)

    def decode_samples_for_frame(self, frame, fps, num_samples):
        if frame not in self.frames:
            return None
        t = (np.arange(num_samples) + frame * num_samples) / SR
        sig = (self.amp * np.sin(2 * np.pi * self.freq * t)).astype(np.float32)
        return np.stack([sig, sig])

    def close(self):
        pass


class TestMx16LufsReference:
    @pytest.mark.parametrize("dbfs,expected", [
        (-23.0, -23.0),   # EBU R128 校准点
        (-20.0, -20.0),
        (-6.0, -6.0),
    ])
    def test_1khz_reference_tones(self, dbfs, expected):
        """997/1000 Hz 正弦 @X dBFS 双单声道 → X LUFS ±0.1（公开参考）。"""
        n = int(1.0 * SR)
        t = np.arange(n) / SR
        a = 10.0 ** (dbfs / 20.0)
        sig = a * np.sin(2 * np.pi * 1000.0 * t)
        lm = LoudnessMeter(SR, 2)
        lm.process(np.stack([sig, sig]))
        assert lm.momentary_lufs == pytest.approx(expected, abs=0.1)

    def test_silence_reads_floor(self):
        lm = LoudnessMeter(SR, 2)
        lm.process(np.zeros((2, int(0.6 * SR)), dtype=np.float32))
        assert lm.momentary_lufs == -70.0


class TestMx16PeakRmsCalibration:
    def test_full_scale_sine_peak_and_rms(self, qapp):
        """满幅 997Hz 正弦：sample peak 0dBFS、RMS -3.01dBFS（±0.05）。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.add_track("t1", _SineDecoder(1.0, 997.0, tuple(range(16))),
                        AudioTrackState(track_id="t1"))
        lvl = None
        for f in range(16):
            _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            if "t1" in peaks:
                lvl = peaks["t1"]
        assert lvl is not None
        # 线性域：peak ≈ 1.0，RMS ≈ 1/sqrt(2)
        assert lvl.peak_l == pytest.approx(1.0, abs=0.01)
        assert lvl.rms_l == pytest.approx(1.0 / np.sqrt(2.0), abs=0.01)
        # payload dB 转换（audio_playback 4093 行公式）：
        peak_db = max(-100.0, 20.0 * np.log10(max(lvl.peak_l, 1e-10)))
        rms_db = max(-100.0, 20.0 * np.log10(max(lvl.rms_l, 1e-10)))
        assert peak_db == pytest.approx(0.0, abs=0.05)
        assert rms_db == pytest.approx(-3.01, abs=0.05)

    def test_half_scale_sine_minus_6db(self, qapp):
        """-6dBFS 正弦：peak -6dBFS、RMS -9.03dBFS（增益定律线性一致）。"""
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.add_track("t1", _SineDecoder(0.5, 997.0, tuple(range(16))),
                        AudioTrackState(track_id="t1"))
        lvl = None
        for f in range(16):
            _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            if "t1" in peaks:
                lvl = peaks["t1"]
        peak_db = 20.0 * np.log10(max(lvl.peak_l, 1e-10))
        rms_db = 20.0 * np.log10(max(lvl.rms_l, 1e-10))
        assert peak_db == pytest.approx(-6.02, abs=0.05)
        assert rms_db == pytest.approx(-9.03, abs=0.05)


class TestMx16AntiphaseStereo:
    def test_antiphase_not_cancelled_per_channel(self, qapp):
        """L=-R：逐声道 peak/RMS 保持满能量（不相消）——相消只允许发生在
        显式 mono 折叠检查（MX-09 频谱功率合成同一规则）。
        """
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)

        class _AntiDecoder:
            def __init__(self):
                self.frames = set(range(8))

            def decode_samples_for_frame(self, frame, fps, num_samples):
                if frame not in self.frames:
                    return None
                t = (np.arange(num_samples) + frame * num_samples) / SR
                sig = (0.5 * np.sin(2 * np.pi * 997.0 * t)).astype(np.float32)
                return np.stack([sig, -sig])

            def close(self):
                pass

        mixer.add_track("t1", _AntiDecoder(),
                        AudioTrackState(track_id="t1"))
        lvl = None
        for f in range(8):
            _, peaks = mixer.mix_samples(f, FPS, BLOCK, 2)
            if "t1" in peaks:
                lvl = peaks["t1"]
        # 每声道独立测量：两声道峰值都 ≈0.5（mono 折叠才会 ≈0）
        assert lvl.peak_l == pytest.approx(0.5, abs=0.01)
        assert lvl.peak_r == pytest.approx(0.5, abs=0.01)

    def test_lufs_antiphase_dual_channel_still_measured(self):
        """反相双声道 LUFS：两声道独立加权求和（不因反相抵消为 -inf）。"""
        n = int(1.0 * SR)
        t = np.arange(n) / SR
        sig = 0.5 * np.sin(2 * np.pi * 1000.0 * t)
        lm = LoudnessMeter(SR, 2)
        lm.process(np.stack([sig, -sig]))
        assert lm.momentary_lufs > -20.0, (
            "antiphase stereo must not cancel in BS.1770 measurement")


class TestMx16MeasurementWindowDocs:
    def test_momentary_window_is_400ms(self):
        """Momentary 窗 = 400ms（R128 定义）：0.3s 信号不足以填满窗。"""
        n = int(0.3 * SR)
        t = np.arange(n) / SR
        sig = 10 ** (-23 / 20) * np.sin(2 * np.pi * 1000.0 * t)
        lm = LoudnessMeter(SR, 2)
        lm.process(np.stack([sig, sig]))
        # 300ms < 400ms 窗：momentary 仍按已积累样本计算（部分窗语义），
        # 但读数必须有限且高于 floor
        assert np.isfinite(lm.momentary_lufs)
        assert lm.momentary_lufs > -70.0
