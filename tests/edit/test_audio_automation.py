"""
Audio Automation 测试（W19-W23）。

覆盖：
1. EditTrack automation 字段（add/remove/update/clear keyframe）
2. Automation 命令（Add/Remove/Clear）
3. 引擎读取 automation（mix_samples 按帧查询覆盖 volume/pan/mute）
4. 序列化往返（automation dict 持久化）
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.features.edit.core.commands import (
    AddAutomationKeyframeCommand, RemoveAutomationKeyframeCommand, ClearAutomationCommand,
)
from src.shared.keyframe import Keyframe, KeyframeTrack, InterpolationType


# =============================================================================
# 1. EditTrack automation 字段
# =============================================================================

class TestEditTrackAutomation:
    """automation 字段的 with_* 访问器。"""

    def test_default_no_automation(self):
        t = EditTrack(track_type="audio")
        assert not t.has_automation("volume")
        assert t.get_automation_value("volume", 0, 1.0) == 1.0  # default

    def test_add_keyframe_creates_track(self):
        t = EditTrack(track_type="audio", volume=1.0)
        t2 = t.with_automation_keyframe_added(
            "volume", Keyframe(frame=0, value=0.5), min_val=0.0, max_val=2.0
        )
        assert t2.has_automation("volume")
        assert not t.has_automation("volume")  # 原不变

    def test_linear_interpolation(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added(
            "volume", Keyframe(frame=0, value=0.0), 0.0, 2.0
        )
        t = t.with_automation_keyframe_added(
            "volume", Keyframe(frame=100, value=2.0), 0.0, 2.0
        )
        assert t.get_automation_value("volume", 0, 1.0) == 0.0
        assert t.get_automation_value("volume", 50, 1.0) == pytest.approx(1.0)
        assert t.get_automation_value("volume", 100, 1.0) == 2.0

    def test_remove_keyframe(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=0.5), 0.0, 2.0)
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=100, value=1.5), 0.0, 2.0)
        t2 = t.with_automation_keyframe_removed("volume", 0)
        assert t2.has_automation("volume")
        # 剩 1 个关键帧
        assert len(t2.automation["volume"].keyframes) == 1

    def test_clear_automation(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=0.5), 0.0, 2.0)
        t2 = t.with_automation_cleared("volume")
        assert not t2.has_automation("volume")

    def test_multiple_params_independent(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=1.5), 0.0, 2.0)
        t = t.with_automation_keyframe_added("pan", Keyframe(frame=0, value=0.5), -1.0, 1.0)
        assert t.has_automation("volume") and t.has_automation("pan")
        assert t.get_automation_value("volume", 0, 1.0) == 1.5
        assert t.get_automation_value("pan", 0, 0.0) == 0.5

    def test_serialization_roundtrip(self):
        t = EditTrack(track_type="audio", name="V")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=0.5), 0.0, 2.0)
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=100, value=1.5, interpolation=InterpolationType.BEZIER), 0.0, 2.0)
        seq = TimelineSequence().with_track_added(t)
        d = seq.to_dict()
        seq2 = TimelineSequence.from_dict(d)
        t2 = seq2.audio_tracks[0]
        assert t2.has_automation("volume")
        assert len(t2.automation["volume"].keyframes) == 2
        assert t2.get_automation_value("volume", 50, 1.0) == pytest.approx(1.0, abs=0.5)

    def test_old_project_no_automation_loads_empty(self):
        old = {"track_id": "x", "name": "old", "track_type": "audio", "volume": 1.0}
        t = EditTrack.from_dict(old)
        assert not t.automation


# =============================================================================
# 2. Automation 命令
# =============================================================================

class TestAutomationCommands:
    """Add/Remove/Clear automation 命令 apply/undo。"""

    def test_add_command_apply_undo(self):
        t = EditTrack(track_type="audio")
        seq = TimelineSequence().with_track_added(t)
        cmd = AddAutomationKeyframeCommand.create(t.track_id, "volume", 0, 0.5, min_val=0.0, max_val=2.0)
        seq2, upd = cmd.apply(seq)
        assert seq2.get_track_by_id(t.track_id).has_automation("volume")
        seq3 = upd.undo(seq2)
        assert not seq3.get_track_by_id(t.track_id).has_automation("volume")

    def test_remove_command_apply_undo(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=0.5), 0.0, 2.0)
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=100, value=1.5), 0.0, 2.0)
        seq = TimelineSequence().with_track_added(t)
        cmd = RemoveAutomationKeyframeCommand.create(t.track_id, "volume", 0)
        seq2, upd = cmd.apply(seq)
        kfs = seq2.get_track_by_id(t.track_id).automation["volume"].keyframes
        assert len(kfs) == 1
        assert kfs[0].frame == 100
        # undo 恢复
        seq3 = upd.undo(seq2)
        kfs2 = seq3.get_track_by_id(t.track_id).automation["volume"].keyframes
        assert len(kfs2) == 2

    def test_clear_command_apply_undo(self):
        t = EditTrack(track_type="audio")
        t = t.with_automation_keyframe_added("volume", Keyframe(frame=0, value=0.5), 0.0, 2.0)
        seq = TimelineSequence().with_track_added(t)
        cmd = ClearAutomationCommand.create(t.track_id, "volume")
        seq2, upd = cmd.apply(seq)
        assert not seq2.get_track_by_id(t.track_id).has_automation("volume")
        seq3 = upd.undo(seq2)
        assert seq3.get_track_by_id(t.track_id).has_automation("volume")

    def test_serialization(self):
        cmd = AddAutomationKeyframeCommand.create("t1", "pan", 50, 0.3, min_val=-1.0, max_val=1.0)
        d = cmd.to_dict()
        cmd2 = AddAutomationKeyframeCommand.from_dict(d)
        assert cmd2.param == "pan" and cmd2.frame == 50 and cmd2.value == 0.3


# =============================================================================
# 3. 引擎读取 automation
# =============================================================================

class TestEngineAutomationRead:
    """mix_samples 按 automation 曲线覆盖 volume/pan/mute。"""

    def test_volume_automation_overrides_static(self):
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        t_arr = np.arange(n) / sr
        sig = (np.sin(2 * np.pi * 1000 * t_arr) * 0.5).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)

        # 静态 volume=1.0
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        out1, _ = m.mix_samples(0, 24.0, n)
        p1 = float(np.max(np.abs(out1[0])))

        # automation: frame 0 volume=0.25
        dec2 = Mock(spec=AudioDecoder)
        dec2.decode_samples_for_frame = Mock(return_value=sig_stereo.copy())
        m2 = AudioMixer()
        m2.set_sample_rate(float(sr))
        m2.add_track("t2", dec2, AudioTrackState(track_id="t2", volume=1.0))
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.25, interpolation=InterpolationType.LINEAR))
        m2.set_track_automation("t2", {"volume": vt})
        m2.set_track_automation_mode("t2", "read")  # AM-07: Read 模式才读取曲线
        out2, _ = m2.mix_samples(0, 24.0, n)
        p2 = float(np.max(np.abs(out2[0])))

        assert p2 < p1 * 0.4, f"automation volume 0.25 should reduce output: {p2} vs {p1}"

    def test_mute_automation_silences(self):
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        t_arr = np.arange(n) / sr
        sig = (np.sin(2 * np.pi * 1000 * t_arr) * 0.5).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        mt = KeyframeTrack(param_name="mute", min_val=0.0, max_val=1.0)
        mt.add_keyframe(Keyframe(frame=0, value=1.0, interpolation=InterpolationType.LINEAR))
        m.set_track_automation("t1", {"mute": mt})
        m.set_track_automation_mode("t1", "read")  # AM-07: Read 模式才读取曲线
        out, peaks = m.mix_samples(0, 24.0, n)
        assert float(np.max(np.abs(out[0]))) < 0.01
        assert "t1" not in peaks  # muted track 不出现

    def test_mute_automation_gates_inside_block_without_click(self):
        """mute 切点落在 block 内时，实时轨必须逐样本而非整块提前静音。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState

        sr = 48_000
        fps = 24.0
        n = 2_000  # 恰好一帧；mute 在该 block 内从 0 线性过渡到 1
        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(
            return_value=np.full((2, n), 0.5, dtype=np.float32)
        )
        mixer = AudioMixer()
        mixer.set_sample_rate(float(sr))
        mixer.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        mute = KeyframeTrack(param_name="mute", min_val=0.0, max_val=1.0)
        mute.add_keyframe(Keyframe(frame=0, value=0.0, interpolation=InterpolationType.LINEAR))
        mute.add_keyframe(Keyframe(frame=1, value=1.0, interpolation=InterpolationType.LINEAR))
        mixer.set_track_automation("t1", {"mute": mute})
        mixer.set_track_automation_mode("t1", "read")

        output, _ = mixer.mix_samples(0, fps, n)
        # 起点有声、终点静音；切点附近有 1ms de-click ramp，不能是整个 block 直接归零。
        assert output[0, 0] == pytest.approx(0.5, abs=1e-5)
        assert output[0, -1] == pytest.approx(0.0, abs=1e-5)
        assert np.any((output[0] > 0.0) & (output[0] < 0.5))

    def test_pan_automation_changes_balance(self):
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        t_arr = np.arange(n) / sr
        sig = (np.sin(2 * np.pi * 1000 * t_arr) * 0.5).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        pt = KeyframeTrack(param_name="pan", min_val=-1.0, max_val=1.0)
        pt.add_keyframe(Keyframe(frame=0, value=1.0, interpolation=InterpolationType.LINEAR))  # 全右
        m.set_track_automation("t1", {"pan": pt})
        m.set_track_automation_mode("t1", "read")  # AM-07: Read 模式才读取曲线
        out, peaks = m.mix_samples(0, 24.0, n)
        # pan=1.0 → left_gain=0, right_gain=1.0
        assert float(np.max(np.abs(out[0]))) < 0.01  # 左静音
        assert float(np.max(np.abs(out[1]))) > 0.3   # 右有声


# =============================================================================
# AM-07: 自动化模式（Off/Read/Write/Touch/Latch）
# =============================================================================

class TestAutomationMode:
    """AM-07: 自动化模式持久化与引擎模式判断。"""

    def test_off_mode_ignores_automation(self):
        """AM-07: Off 模式不读取 automation 曲线（用静态值）。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        t_arr = np.arange(n) / sr
        sig = (np.sin(2 * np.pi * 1000 * t_arr) * 0.5).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        # 设置 volume automation（frame 0 → 0.25）
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.25, interpolation=InterpolationType.LINEAR))
        m.set_track_automation("t1", {"volume": vt})
        m.set_track_automation_mode("t1", "off")  # Off 模式
        out, _ = m.mix_samples(0, 24.0, n)
        # Off 模式：用静态 volume=1.0，不读 automation 0.25
        peak = float(np.max(np.abs(out[0])))
        assert peak > 0.3, f"Off 模式应用静态 volume=1.0，peak 应≈0.5×PAN_CENTER，实际 {peak}"

    def test_read_mode_applies_automation(self):
        """AM-07: Read 模式读取 automation 曲线覆盖静态值。"""
        from src.features.edit.core.audio_playback import AudioMixer, AudioDecoder, AudioTrackState
        sr = 48000
        n = 4800
        t_arr = np.arange(n) / sr
        sig = (np.sin(2 * np.pi * 1000 * t_arr) * 0.5).astype(np.float32)
        sig_stereo = np.stack([sig, sig])

        dec = Mock(spec=AudioDecoder)
        dec.decode_samples_for_frame = Mock(return_value=sig_stereo)
        m = AudioMixer()
        m.set_sample_rate(float(sr))
        m.add_track("t1", dec, AudioTrackState(track_id="t1", volume=1.0))
        vt = KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0)
        vt.add_keyframe(Keyframe(frame=0, value=0.25, interpolation=InterpolationType.LINEAR))
        m.set_track_automation("t1", {"volume": vt})
        m.set_track_automation_mode("t1", "read")
        out, _ = m.mix_samples(0, 24.0, n)
        peak_read = float(np.max(np.abs(out[0])))

        # 对比 Off 模式
        m.set_track_automation_mode("t1", "off")
        out2, _ = m.mix_samples(0, 24.0, n)
        peak_off = float(np.max(np.abs(out2[0])))
        # Read 应比 Off 更安静（automation 0.25 < 静态 1.0）
        assert peak_read < peak_off * 0.5, \
            f"Read 应读 automation 0.25，比 Off 静态 1.0 安静"

    def test_default_mode_is_off(self):
        """AM-07: 默认模式为 off（向后兼容）。"""
        from src.features.edit.core.audio_playback import AudioMixer
        m = AudioMixer()
        m.add_track("t1", Mock(spec=__import__('src.features.edit.core.audio_playback', fromlist=['AudioDecoder']).AudioDecoder),
                    __import__('src.features.edit.core.audio_playback', fromlist=['AudioTrackState']).AudioTrackState(track_id="t1"))
        # 未设模式时，引擎默认 off
        assert m._automation_modes.get("t1", "off") == "off"

    def test_invalid_mode_normalized_to_off(self):
        """AM-07: 无效模式归一化为 off。"""
        from src.features.edit.core.audio_playback import AudioMixer
        m = AudioMixer()
        m.set_track_automation_mode("t1", "INVALID")
        assert m._automation_modes["t1"] == "off"


class TestAutomationModePersistence:
    """AM-07: automation_mode 持久化（EditTrack 字段 + 序列化）。"""

    def test_edit_track_automation_mode_field(self):
        """EditTrack 有 automation_mode 字段，默认 off。"""
        t = EditTrack(track_type="audio")
        assert t.automation_mode == "off"

    def test_with_automation_mode(self):
        """with_automation_mode 返回新模式实例。"""
        t = EditTrack(track_type="audio")
        t2 = t.with_automation_mode("read")
        assert t2.automation_mode == "read"
        assert t.automation_mode == "off"  # immutable

    def test_mutation_preserves_automation_mode(self):
        """其他 mutation（rename 等）保留 automation_mode（防 AM-04 类字段丢失）。"""
        t = EditTrack(track_type="audio").with_automation_mode("read")
        t2 = t.with_name("Renamed")
        assert t2.automation_mode == "read"

    def test_serialization_roundtrip(self):
        """automation_mode 序列化往返保留。"""
        t = EditTrack(track_type="audio").with_automation_mode("write")
        seq = TimelineSequence().with_track_added(t)
        seq2 = TimelineSequence.from_dict(seq.to_dict())
        t2 = seq2.audio_tracks[0]
        assert t2.automation_mode == "write"
