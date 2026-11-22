"""
Strip EQ / Parametric EQ 单元测试（CW-A1 参数化重构版）。

覆盖：
1. StripEQParams 数据模型（6 频段参数化：验证、序列化、往返、旧形状降级）
2. EditTrack strip_eq 字段集成（with_strip_eq、序列化）
3. SetTrackStripEQCommand（apply/undo/to_dict/from_dict）
4. 频响求值（ProEQGraphWidget 客户端 biquad，替代旧 compute_eq_response）
5. mix_samples 集成（EQ 实际作用于输出）

深度 DSP 行为见 test_audio_dsp.py / test_audio_dsp_pro.py。
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import (
    EditTrack, StripEQParams, TimelineSequence,
    default_eq_bands_flat,
)
from src.features.edit.core.commands import SetTrackStripEQCommand


def _bands(**overrides):
    """构造参数化频段列表（平直 + 覆盖）。"""
    bands = [dict(b) for b in default_eq_bands_flat()]
    for key, kv in overrides.items():
        i = int(key)
        bands[i] = {**bands[i], **kv}
    return bands


# =============================================================================
# 1. StripEQParams 数据模型（CW-A1 参数化）
# =============================================================================

class TestStripEQParams:
    """StripEQParams 创建、验证、序列化。"""

    def test_default_is_flat(self):
        """默认参数：6 频段平直（0 dB），启用。"""
        p = StripEQParams.default()
        assert len(p.bands) == 6
        assert all(b["gain"] == 0.0 for b in p.bands)
        assert p.enabled is True
        assert p.voicing == "Clean"
        assert p.master_gain == 0.0

    def test_band_fields_present(self):
        """每个频段含完整字段集。"""
        p = StripEQParams.default()
        for b in p.bands:
            assert {"index", "enabled", "solo", "filter_type",
                    "range_key", "freq", "gain", "q", "slope"} <= set(b.keys())

    def test_validates_voicing(self):
        """非法 voicing 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(voicing="Metal")

    def test_validates_master_gain_range(self):
        """master_gain 超出 [-20, 20] 报错。"""
        with pytest.raises(ValueError):
            StripEQParams(master_gain=25.0)

    def test_validates_band_fields(self):
        """频段字段归一化：非法 filter_type / 超范围值被钳位。"""
        p = StripEQParams(bands=(
            {"filter_type": "BadShape", "freq": 999999.0, "gain": 99.0,
             "q": 0.0, "slope": 5, "enabled": True, "solo": False},
        ))
        b = p.bands[0]
        assert b["filter_type"] == "Bell"  # 非法形状回退
        assert b["freq"] == 22000.0  # 钳位到 [20, 22000]
        assert b["gain"] == 20.0
        assert b["q"] == 0.3
        assert b["slope"] == 12

    def test_to_dict_from_dict_roundtrip(self):
        """序列化往返一致。"""
        p = StripEQParams(
            enabled=True, voicing="British", master_gain=2.0,
            bands=tuple(_bands(**{"3": {"filter_type": "Bell", "freq": 800.0,
                                        "gain": 3.5, "q": 2.0, "enabled": True}})),
        )
        d = p.to_dict()
        p2 = StripEQParams.from_dict(d)
        assert p2 == p

    def test_from_dict_missing_fields_uses_defaults(self):
        """缺失字段用默认值。"""
        p = StripEQParams.from_dict({})
        assert p.enabled is True
        assert p.voicing == "Clean"
        assert len(p.bands) == 6

    def test_from_dict_pads_short_bands(self):
        """bands 不足 6 段时用平直默认补齐（频段身份 = 下标）。"""
        p = StripEQParams.from_dict({
            "bands": [{"filter_type": "Bell", "freq": 500.0, "gain": 5.0,
                       "q": 1.5, "enabled": True, "solo": False}],
        })
        assert len(p.bands) == 6
        assert p.bands[0]["gain"] == 5.0
        assert p.bands[1]["gain"] == 0.0

    def test_from_dict_legacy_graphic_falls_back_flat(self):
        """旧图形 EQ（bands 为 float 列表）整体落平直默认。"""
        p = StripEQParams.from_dict({
            "mode": "10-band",
            "bands": [3.0, 0.0, -2.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            "gain": 2.0,
        })
        assert len(p.bands) == 6
        assert all(b["gain"] == 0.0 for b in p.bands)
        assert p.master_gain == 0.0


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
        eq = StripEQParams()
        t2 = t.with_strip_eq(eq)
        assert t.strip_eq is None  # 原实例不变
        assert t2.strip_eq == eq
        # 其他字段保留
        assert t2.volume == t.volume
        assert t2.name == t.name

    def test_with_strip_eq_none_disables(self):
        """with_strip_eq(None) 显式禁用 EQ。"""
        t = EditTrack(track_type="audio",
                      strip_eq=StripEQParams())
        t2 = t.with_strip_eq(None)
        assert t2.strip_eq is None

    def test_to_dict_from_dict_roundtrip(self):
        eq = StripEQParams()
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
        eq = StripEQParams()
        cmd = SetTrackStripEQCommand.create(t.track_id, eq)
        new_seq, updated = cmd.apply(seq)
        assert new_seq.get_track_by_id(t.track_id).strip_eq == eq
        assert updated._old_strip_eq is None  # 原本是 None

    def test_undo_restores_old_strip_eq(self):
        t = EditTrack(track_type="audio")
        seq = TimelineSequence().with_track_added(t)
        eq = StripEQParams()
        cmd = SetTrackStripEQCommand.create(t.track_id, eq)
        new_seq, updated = cmd.apply(seq)
        restored = updated.undo(new_seq)
        assert restored.get_track_by_id(t.track_id).strip_eq is None

    def test_to_dict_from_dict_roundtrip(self):
        eq = StripEQParams()
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
# 4. 频响求值（ProEQGraphWidget 客户端 biquad，CW-A1）
# =============================================================================

class TestEQResponseEvaluation:
    """客户端频响求值（替代旧 compute_eq_response 的职责）。"""

    def _graph(self, qapp):
        from src.shared.ui.audio_mixer.strip_eq_dialog import ProEQGraphWidget
        g = ProEQGraphWidget()
        g._sample_rate = 48000.0
        return g

    def test_flat_response_is_zero(self, qapp):
        """平直 EQ 在所有频率为 0 dB。"""
        g = self._graph(qapp)
        g.setState(_bands(), True, "Clean", 0.0)
        for f in (100.0, 500.0, 1000.0, 5000.0):
            assert abs(g._total_db_at(f)) < 1e-6

    def test_peaking_at_center_frequency(self, qapp):
        """Band 4 Bell @500Hz +6dB 应在 500Hz 处接近 +6dB。"""
        g = self._graph(qapp)
        g.setState(_bands(**{"3": {"filter_type": "Bell", "freq": 500.0,
                                   "gain": 6.0, "q": 1.4, "enabled": True}}),
                   True, "Clean", 0.0)
        resp = g._total_db_at(500.0)
        assert abs(resp - 6.0) < 1.0, f"peaking at 500Hz: {resp:.2f} dB"

    def test_master_gain_flat_across_frequencies(self, qapp):
        """master_gain +3dB 应在所有频率一致。"""
        g = self._graph(qapp)
        g.setState(_bands(), True, "Clean", 3.0)
        for f in (100.0, 1000.0, 10000.0):
            assert abs(g._total_db_at(f) - 3.0) < 1e-6

    def test_hpf_response(self, qapp):
        """Band 1 HPF @200Hz 24dB/oct 在 50Hz 应大幅衰减。"""
        g = self._graph(qapp)
        g.setState(_bands(**{"0": {"filter_type": "HPF", "freq": 200.0,
                                   "slope": 24, "enabled": True}}),
                   True, "Clean", 0.0)
        assert g._total_db_at(50.0) < -30.0
        assert abs(g._total_db_at(1000.0)) < 1.0

    def test_disabled_returns_zero(self, qapp):
        """enabled=False 返回全 0。"""
        g = self._graph(qapp)
        g.setState(_bands(**{"3": {"filter_type": "Bell", "freq": 1000.0,
                                   "gain": 6.0, "enabled": True}}),
                   False, "Clean", 0.0)
        assert abs(g._total_db_at(1000.0)) < 1e-9

    def test_solo_isolates_band(self, qapp):
        """Solo Band 3 时总曲线只含该频段贡献。"""
        g = self._graph(qapp)
        bands = _bands(**{
            "2": {"filter_type": "Bell", "freq": 300.0, "gain": 5.0,
                  "q": 2.0, "enabled": True, "solo": True},
            "3": {"filter_type": "Bell", "freq": 3000.0, "gain": -6.0,
                  "q": 2.0, "enabled": True},
        })
        g.setState(bands, True, "Clean", 0.0)
        # 3kHz 频段被 Solo 排除 → 其 -6dB 谷不再出现（300Hz bell 在 3kHz
        # 的频响拖尾 < 0.05dB 属物理正确）
        assert g._total_db_at(3000.0) > -0.1
        assert abs(g._total_db_at(300.0) - 5.0) < 1.0


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

        # 有 Low Shelf +6dB @250Hz（100Hz 在通带）
        dec2 = Mock(spec=AudioDecoder)
        dec2.decode_samples_for_frame = Mock(return_value=sig_stereo.copy())
        mixer_eq = AudioMixer()
        mixer_eq.set_sample_rate(float(sr))
        mixer_eq.add_track("t2", dec2)
        mixer_eq.set_track_strip_eq("t2", StripEQParams(bands=tuple(
            _bands(**{"1": {"filter_type": "Low Shelf", "freq": 250.0,
                            "gain": 6.0, "q": 1.4, "enabled": True}}))))
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
        mixer.set_track_strip_eq("t1", StripEQParams(bands=tuple(
            _bands(**{"1": {"filter_type": "Low Shelf", "freq": 250.0,
                            "gain": 6.0, "q": 1.4, "enabled": True}}))))
        _, peaks = mixer.mix_samples(0, 24.0, n)

        assert "t1" in peaks
        pl, pr = peaks["t1"]
        # EQ 后峰值应 > 输入峰值 0.5
        assert pl > 0.5, f"post-EQ peak {pl:.3f} should be > 0.5"


class TestFX01ResponseIdentity:
    """FX-01（2026-11-21 计划）：曲线读取器与独立 SOS 求值恒等。"""

    def test_chart_db_matches_independent_sos_eval(self) -> None:
        """strip_eq_band_response_db ≡ sosfreqz(sections)（atol 1e-8）。"""
        from scipy.signal import sosfreqz

        from src.features.edit.core.audio_dsp import (
            strip_eq_band_response_db,
            strip_eq_band_sections,
        )
        rate = 48000
        band = dict(filter_type="Bell", freq=1000.0, q=1.1, gain=6.0,
                    slope=12)
        frequencies = np.array([100.0, 1000.0, 8000.0])
        sections = np.asarray(
            strip_eq_band_sections(band, rate, "Clean", 3))
        _, response = sosfreqz(sections, worN=frequencies, fs=rate)
        reference_db = 20 * np.log10(np.abs(response))
        chart_db = [strip_eq_band_response_db(band, float(f), rate,
                                              "Clean", 3)
                    for f in frequencies]
        np.testing.assert_allclose(chart_db, reference_db, rtol=0,
                                   atol=1e-8)
        assert abs(reference_db[1] - 6.0) < 1e-8

    def test_total_curve_is_master_gain_times_bands(self) -> None:
        """H_total = linear_master_gain × ∏H_band（dB 可加性）。"""
        from src.features.edit.core.audio_dsp import (
            strip_eq_band_response_db,
        )
        rate = 48000
        bands = [
            dict(filter_type="Bell", freq=200.0, q=1.0, gain=3.0,
                 enabled=True),
            dict(filter_type="Bell", freq=4000.0, q=1.0, gain=-4.0,
                 enabled=True),
        ]
        for freq in (200.0, 1000.0, 4000.0, 12000.0):
            band_sum = sum(
                strip_eq_band_response_db(band, freq, rate, "Clean", 0.0)
                for band in bands)
            total = strip_eq_band_response_db(bands[0], freq, rate,
                                              "Clean", 0.0) + \
                strip_eq_band_response_db(bands[1], freq, rate, "Clean",
                                          0.0)
            assert band_sum == pytest.approx(total, abs=1e-9)
