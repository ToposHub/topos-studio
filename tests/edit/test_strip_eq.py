"""
Strip EQ / Graphic EQ 单元测试（W6/W7/W26）。

覆盖：
1. StripEQParams 数据模型（多频段：验证、序列化、往返、mode 切换）
2. EditTrack strip_eq 字段集成（with_strip_eq、序列化）
3. SetTrackStripEQCommand（apply/undo/to_dict/from_dict）
4. compute_eq_response（多频段频响曲线）
5. mix_samples 集成（EQ 实际作用于输出）
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import EditTrack, StripEQParams, TimelineSequence
from src.features.edit.core.commands import SetTrackStripEQCommand


# =============================================================================
# 1. StripEQParams 数据模型（多频段）
# =============================================================================

class TestStripEQParams:
    """StripEQParams 创建、验证、序列化。"""

    def test_default_is_flat(self):
        """默认参数：10-band，所有频段 0 dB，启用。"""
        p = StripEQParams.default()
        assert p.mode == "10-band"
        assert len(p.bands) == 10
        assert all(b == 0.0 for b in p.bands)
        assert p.enabled is True

    def test_default_per_mode(self):
        """default(mode) 返回对应频段数。"""
        assert len(StripEQParams.default("3-band").bands) == 3
        assert len(StripEQParams.default("7-band").bands) == 7
        assert len(StripEQParams.default("10-band").bands) == 10
        assert len(StripEQParams.default("15-band").bands) == 15

    def test_validates_mode(self):
        """非法 mode 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(mode="5-band", bands=(0.0,) * 5)

    def test_validates_gain_range(self):
        """整体增益超出 [-12, 12] 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(mode="3-band", bands=(0.0, 0.0, 0.0), gain=15.0)

    def test_validates_band_gain_range(self):
        """单频段增益超出 [-12, 12] 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(mode="3-band", bands=(15.0, 0.0, 0.0))

    def test_validates_low_cut_range(self):
        """low_cut 超出 [20, 1000] 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(mode="3-band", bands=(0, 0, 0), low_cut=10.0)

    def test_validates_high_cut_range(self):
        """high_cut 超出 [1000, 20000] 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(mode="3-band", bands=(0, 0, 0), high_cut=50000.0)

    def test_to_dict_from_dict_roundtrip(self):
        """序列化往返一致。"""
        p = StripEQParams(
            mode="10-band",
            bands=(3.5, -2.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            gain=2.0, low_cut=100.0, high_cut=12000.0,
            low_cut_enable=True, high_cut_enable=False,
        )
        d = p.to_dict()
        p2 = StripEQParams.from_dict(d)
        assert p2 == p

    def test_from_dict_missing_fields_uses_defaults(self):
        """缺失字段用默认值。"""
        p = StripEQParams.from_dict({"mode": "3-band"})
        assert p.mode == "3-band"
        assert len(p.bands) == 3
        assert all(b == 0.0 for b in p.bands)
        assert p.enabled is True  # 默认

    def test_from_dict_pads_short_bands(self):
        """bands 长度不足时补零到 mode 频段数。"""
        p = StripEQParams.from_dict({"mode": "10-band", "bands": [5.0, 3.0]})
        assert len(p.bands) == 10
        assert p.bands[0] == 5.0
        assert p.bands[1] == 3.0
        assert p.bands[2] == 0.0


# =============================================================================
# 2. EditTrack strip_eq 字段集成
# =============================================================================

class TestEditTrackStripEQ:
    """EditTrack 的 strip_eq 字段 + with_strip_eq + 序列化。"""

    def test_default_strip_eq_is_none(self):
        t = EditTrack(track_type="audio")
        assert t.strip_eq is None

    def test_with_strip_eq_creates_new_instance(self):
        t = EditTrack(track_type="audio")
        eq = StripEQParams(mode="3-band", bands=(5.0, 0.0, 0.0))
        t2 = t.with_strip_eq(eq)
        assert t.strip_eq is None  # 原实例不变
        assert t2.strip_eq == eq
        # 其他字段保留
        assert t2.volume == t.volume
        assert t2.name == t.name

    def test_with_strip_eq_none_disables(self):
        """with_strip_eq(None) 显式禁用 EQ。"""
        t = EditTrack(track_type="audio",
                      strip_eq=StripEQParams(mode="3-band", bands=(5.0, 0.0, 0.0)))
        t2 = t.with_strip_eq(None)
        assert t2.strip_eq is None

    def test_to_dict_from_dict_roundtrip(self):
        eq = StripEQParams(mode="10-band",
                           bands=(3.0, 0, 0, 0, 0, 0, 0, 0, 0, -2.0))
        t = EditTrack(track_type="audio", name="Voice", volume=0.8, strip_eq=eq)
        d = t.to_dict()
        t2 = EditTrack.from_dict(d)
        assert t2.strip_eq == eq

    def test_old_project_without_strip_eq_loads_as_none(self):
        """旧项目文件（无 strip_eq 字段）加载为 None。"""
        old_data = {
            "track_id": "abc", "name": "Old", "track_type": "audio",
            "volume": 1.0, "pan": 0.0,
        }
        t = EditTrack.from_dict(old_data)
        assert t.strip_eq is None


# =============================================================================
# 3. SetTrackStripEQCommand
# =============================================================================

class TestSetTrackStripEQCommand:
    """命令 apply/undo/序列化。"""

    def test_apply_sets_strip_eq(self):
        t = EditTrack(track_type="audio")
        seq = TimelineSequence().with_track_added(t)
        eq = StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0))
        cmd = SetTrackStripEQCommand.create(t.track_id, eq)
        new_seq, updated = cmd.apply(seq)
        assert new_seq.get_track_by_id(t.track_id).strip_eq == eq
        assert updated._old_strip_eq is None  # 原本是 None

    def test_undo_restores_old_strip_eq(self):
        t = EditTrack(track_type="audio")
        seq = TimelineSequence().with_track_added(t)
        eq = StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0))
        cmd = SetTrackStripEQCommand.create(t.track_id, eq)
        new_seq, updated = cmd.apply(seq)
        restored = updated.undo(new_seq)
        assert restored.get_track_by_id(t.track_id).strip_eq is None

    def test_to_dict_from_dict_roundtrip(self):
        eq = StripEQParams(mode="3-band", bands=(3.0, -2.0, 0.0))
        cmd = SetTrackStripEQCommand(command_id="c1", label="test",
                                     track_id="t1", strip_eq=eq,
                                     _old_strip_eq=None)
        d = cmd.to_dict()
        cmd2 = SetTrackStripEQCommand.from_dict(d)
        assert cmd2.strip_eq == eq
        assert cmd2.track_id == "t1"

    def test_serializes_none_strip_eq(self):
        """None strip_eq 也能正确序列化。"""
        cmd = SetTrackStripEQCommand(command_id="c1", label="test",
                                     track_id="t1", strip_eq=None)
        d = cmd.to_dict()
        assert d["strip_eq"] is None
        cmd2 = SetTrackStripEQCommand.from_dict(d)
        assert cmd2.strip_eq is None


# =============================================================================
# 4. compute_eq_response（多频段频响曲线）
# =============================================================================

class TestComputeEQResponse:
    """频响曲线计算。"""

    def test_flat_response_is_zero(self):
        """平直 EQ 在所有频率为 0 dB。"""
        from src.features.edit.core.audio_dsp import compute_eq_response
        freqs = np.array([100.0, 500.0, 1000.0, 5000.0])
        resp = compute_eq_response(StripEQParams.default(), 48000.0, freqs)
        assert np.allclose(resp, 0.0, atol=1e-6)

    def test_peaking_at_center_frequency(self):
        """10-band peaking @500Hz +6dB 应在 500Hz 处 +6dB。"""
        from src.features.edit.core.audio_dsp import compute_eq_response
        bands = [0.0] * 10
        bands[4] = 6.0
        eq = StripEQParams(mode="10-band", bands=tuple(bands))
        freqs = np.array([500.0])
        resp = compute_eq_response(eq, 48000.0, freqs)
        assert abs(resp[0] - 6.0) < 0.5, f"peaking at 500Hz: {resp[0]:.2f} dB (expected ~+6)"

    def test_output_gain_flat_across_frequencies(self):
        """整体增益 +3dB 应在所有频率一致。"""
        from src.features.edit.core.audio_dsp import compute_eq_response
        eq = StripEQParams(mode="3-band", bands=(0.0, 0.0, 0.0), gain=3.0)
        freqs = np.array([100.0, 1000.0, 10000.0])
        resp = compute_eq_response(eq, 48000.0, freqs)
        assert np.allclose(resp, 3.0, atol=1e-6)

    def test_low_cut_response(self):
        """Low-Cut @200Hz 在 50Hz 应大幅衰减。"""
        from src.features.edit.core.audio_dsp import compute_eq_response
        eq = StripEQParams(mode="3-band", bands=(0, 0, 0),
                           low_cut=200.0, low_cut_enable=True)
        freqs = np.array([50.0, 1000.0])
        resp = compute_eq_response(eq, 48000.0, freqs)
        assert resp[0] < -10.0  # 50Hz 大幅衰减
        assert abs(resp[1]) < 1.0  # 1kHz 几乎不变

    def test_disabled_returns_zero(self):
        """enabled=False 返回全 0。"""
        from src.features.edit.core.audio_dsp import compute_eq_response
        eq = StripEQParams(mode="3-band", bands=(6.0, 0.0, 0.0), enabled=False)
        freqs = np.array([100.0, 1000.0])
        resp = compute_eq_response(eq, 48000.0, freqs)
        assert np.allclose(resp, 0.0, atol=1e-9)


# =============================================================================
# 5. mix_samples 集成
# =============================================================================

class TestMixSamplesWithEQ:
    """AudioMixer.mix_samples 在有 EQ 时正确作用于输出。"""

    def test_eq_applied_to_track_output(self):
        """轨道设了 EQ 后，mix 输出反映 EQ。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder

        sr = 48000
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)

        # 无 EQ
        mixer_no_eq = AudioMixer()
        mixer_no_eq.set_sample_rate(float(sr))
        mixer_no_eq.add_track("t1", dec)
        out_no_eq, _ = mixer_no_eq.mix_samples(0, 24.0, n)

        # 有 lowshelf +6dB（3-band @320Hz，100Hz 在通带）
        dec2 = Mock(spec=AudioDecoder)
        dec2.decode_samples_for_frame = Mock(return_value=sig_stereo.copy())
        mixer_eq = AudioMixer()
        mixer_eq.set_sample_rate(float(sr))
        mixer_eq.add_track("t2", dec2)
        mixer_eq.set_track_strip_eq("t2", StripEQParams(
            mode="3-band", bands=(6.0, 0.0, 0.0)))
        out_eq, _ = mixer_eq.mix_samples(0, 24.0, n)

        no_eq_peak = float(np.max(np.abs(out_no_eq[0, 200:])))
        eq_peak = float(np.max(np.abs(out_eq[0, 200:])))
        # EQ 后应明显大于无 EQ
        assert eq_peak > no_eq_peak * 1.5, \
            f"EQ peak {eq_peak:.3f} should be > 1.5x no-EQ peak {no_eq_peak:.3f}"

    def test_eq_reflected_in_per_track_peaks(self):
        """EQ 后的信号反映在 per_track_peaks。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder

        sr = 48000
        n = 4096
        t = np.arange(n) / sr
        sig = (0.5 * np.sin(2 * np.pi * 100.0 * t)).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)

        mixer = AudioMixer()
        mixer.set_sample_rate(float(sr))
        mixer.add_track("t1", dec)
        mixer.set_track_strip_eq("t1", StripEQParams(
            mode="3-band", bands=(6.0, 0.0, 0.0)))
        _, peaks = mixer.mix_samples(0, 24.0, n)

        assert "t1" in peaks
        pl, pr = peaks["t1"]
        # EQ 后峰值应 > 输入峰值 0.5
        assert pl > 0.5, f"post-EQ peak {pl:.3f} should be > 0.5"
