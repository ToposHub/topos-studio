"""EQ-02 / EQ-03 补齐回归：Voicing 带宽-增益耦合与频谱分析仪语义（阶段G）。

EQ-02 验收：
- 每一可用控件（freq/gain/Q/slope/shape/voicing）改变实际频响；
- 所有形状 × 44.1/48/96 kHz 响应 finite；
- 0dB Bell 直通；
- 非 Clean voicing 的带宽/增益耦合可测：boost→Q 窄、cut→Q 宽、单调有界；
  Clean 与 0dB 恒直通（默认工程声音不变，ALGORITHM_VERSION=3）。

EQ-03 验收：
- 1kHz 纯音在频谱上定位于 1kHz bin（±1 bin）；
- 静音频谱贴地板；停止清频谱（MX-01 已锁，此处锁分析定义）；
- 频谱测量点=轨链输出（增益变化后分析随之变化）。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    ALGORITHM_VERSION,
    VOICING_GAIN_Q_COUPLING,
    _effective_band_q,
    strip_eq_band_response_db,
    strip_eq_band_sections,
)
from src.features.edit.core.audio_playback import AudioMixer

SR = 48000.0


def _band(**over):
    b = {"enabled": True, "solo": False, "index": 3,
         "filter_type": "Bell", "freq": 1000.0, "gain": 6.0,
         "q": 1.1, "slope": 12}
    b.update(over)
    return b


def _response_db(band, voicing="Clean", freqs=None):
    if freqs is None:
        freqs = np.geomspace(20.0, 20000.0, 240)
    return np.array([strip_eq_band_response_db(band, float(f), SR, voicing)
                     for f in freqs])


class TestEq02GainQCoupling:
    def test_clean_and_zero_gain_are_passthrough(self):
        """Clean k=0 与 gain=0 → 耦合因子恒 1（默认声音不变）。"""
        assert _effective_band_q(2.0, 12.0, "Clean", "Bell") == 2.0
        assert _effective_band_q(2.0, 0.0, "British", "Bell") == 2.0
        # 无增益形状不耦合
        assert _effective_band_q(0.7, 12.0, "Punch", "HPF") == 0.7
        assert _effective_band_q(0.7, 12.0, "Warm", "Notch") == 0.7

    def test_boost_narrows_cut_widens(self):
        """British：+12dB → Q 变大（窄）；-12dB → Q 变小（宽）。"""
        q0 = 1.1
        q_boost = _effective_band_q(q0, 12.0, "British", "Bell")
        q_cut = _effective_band_q(q0, -12.0, "British", "Bell")
        assert q_boost > q0 > q_cut

    def test_monotone_and_bounded(self):
        k = VOICING_GAIN_Q_COUPLING["British"]
        base = 1.1
        qs = [_effective_band_q(base, g, "British", "Bell")
              for g in range(-24, 25, 2)]
        assert all(qs[i] < qs[i + 1] for i in range(len(qs) - 1)), \
            "coupling must be monotone in gain"
        lo = base * 10.0 ** (k * -24.0 / 60.0)
        hi = base * 10.0 ** (k * 24.0 / 60.0)
        assert qs[0] == pytest.approx(lo)
        assert qs[-1] == pytest.approx(hi)

    def test_coupling_changes_measured_bandwidth(self):
        """同一 +12dB Bell：British（耦合）比 Clean（无耦合）-3dB 带宽窄。"""
        freqs = np.geomspace(200.0, 5000.0, 400)
        r_clean = _response_db(_band(gain=12.0, q=1.1), "Clean", freqs)
        r_brit = _response_db(_band(gain=12.0, q=1.1), "British", freqs)
        peak_c = float(np.max(r_clean))
        peak_b = float(np.max(r_brit))
        width_c = float(np.sum(r_clean > peak_c - 3.0))
        width_b = float(np.sum(r_brit > peak_b - 3.0))
        assert width_b < width_c, (
            f"British boost must be narrower: {width_b} vs {width_c} bins")

    def test_algorithm_version_bumped(self):
        """耦合改声 → ALGORITHM_VERSION=3（ST-03 契约）。"""
        assert ALGORITHM_VERSION >= 3


class TestEq02EveryControlChangesResponse:
    def test_freq_gain_q_slope_voicing_each_matter(self):
        base = _band(gain=6.0, q=1.1, freq=1000.0)
        r0 = _response_db(base, "Clean")
        assert not np.allclose(_response_db(_band(freq=2000.0), "Clean"), r0)
        assert not np.allclose(_response_db(_band(gain=-6.0), "Clean"), r0)
        assert not np.allclose(_response_db(_band(q=3.0), "Clean"), r0)
        # voicing 影响（Q 比例 + 耦合）
        assert not np.allclose(_response_db(base, "Punch"), r0)
        # slope 只对 HPF/LPF 有效
        rh6 = _response_db(_band(filter_type="HPF", freq=1000.0, slope=6))
        rh24 = _response_db(_band(filter_type="HPF", freq=1000.0, slope=24))
        assert not np.allclose(rh6, rh24)

    @pytest.mark.parametrize("shape", ["HPF", "Low Shelf", "Bell",
                                       "High Shelf", "Notch", "LPF"])
    @pytest.mark.parametrize("sr", [44100.0, 48000.0, 96000.0])
    def test_all_shapes_finite_all_sample_rates(self, shape, sr):
        r = [strip_eq_band_response_db(
            _band(filter_type=shape, freq=1000.0, gain=6.0, q=1.1),
            float(f), sr, "British") for f in np.geomspace(20, 16000, 60)]
        assert np.all(np.isfinite(r))

    def test_zero_db_bell_is_passthrough(self):
        r = _response_db(_band(gain=0.0))
        assert np.allclose(r, 0.0, atol=1e-9)
        # 0dB 在任意 voicing 下仍直通（耦合因子 1，RBJ gain≈0 段中性）
        r2 = _response_db(_band(gain=0.0), "Punch")
        assert np.allclose(r2, 0.0, atol=1e-9)


class TestEq03AnalyzerSemantics:
    def _mixer_with_tone(self, amp, freq=1000.0):
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.set_spectrum_metering(True)

        class _Dec:
            def __init__(self):
                self.frames = set(range(20))

            def decode_samples_for_frame(self, frame, fps, num_samples):
                if frame not in self.frames:
                    return None
                t = (np.arange(num_samples) + frame * num_samples) / SR
                sig = (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)
                return np.stack([sig, sig])

            def close(self):
                pass

        from src.features.edit.core.audio_playback import AudioTrackState
        mixer.add_track("t1", _Dec(), AudioTrackState(track_id="t1"))
        return mixer

    def test_1khz_tone_located_in_spectrum(self, qapp):
        mixer = self._mixer_with_tone(0.5)
        for f in range(20):
            mixer.mix_samples(f, 24.0, 480, 2)
        spectra = mixer.collect_spectra()
        assert "t1" in spectra
        freqs, dbs = spectra["t1"]
        peak_bin = int(np.argmax(dbs))
        expected_bin = int(np.argmin(np.abs(np.asarray(freqs) - 1000.0)))
        assert abs(peak_bin - expected_bin) <= 1, \
            f"1kHz tone peak located at {freqs[peak_bin]:.0f} Hz"
        assert dbs[peak_bin] > -40.0  # 0.5 正弦（-6dBFS）应显著可见

    def test_silence_spectrum_at_floor(self, qapp):
        mixer = AudioMixer()
        mixer.set_sample_rate(SR)
        mixer.set_spectrum_metering(True)
        from src.features.edit.core.audio_playback import AudioTrackState

        class _Silent:
            frames = set(range(10))

            def decode_samples_for_frame(self, frame, fps, num_samples):
                if frame in self.frames:
                    return np.zeros((2, num_samples), dtype=np.float32)
                return None

            def close(self):
                pass

        mixer.add_track("t1", _Silent(), AudioTrackState(track_id="t1"))
        for f in range(10):
            mixer.mix_samples(f, 24.0, 480, 2)
        spectra = mixer.collect_spectra()
        # 全零轨：窗能量 0 → dB 贴地板（-120 聚合初值或更低）
        if "t1" in spectra:
            _, dbs = spectra["t1"]
            assert float(np.max(dbs)) <= -100.0

    def test_spectrum_follows_gain_change(self, qapp):
        """EQ-03：增益 -12dB 后频谱随之下降 ~12dB（分析点=轨链输出）。"""
        def _peak_db(volume):
            mixer = self._mixer_with_tone(0.5)
            mixer.update_track_volume("t1", volume)
            for f in range(20):
                mixer.mix_samples(f, 24.0, 480, 2)
            _, dbs = mixer.collect_spectra()["t1"]
            return float(np.max(dbs))

        loud = _peak_db(1.0)
        quiet = _peak_db(10 ** (-12 / 20))
        drop = loud - quiet
        assert drop == pytest.approx(12.0, abs=1.5), \
            f"spectrum must follow output level (drop={drop:.1f} dB)"
