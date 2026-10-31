"""
Automation Recorder 测试（§3.1 Write/Touch/Latch 录制）。

验证（计划表 §3.1）：
- Write：播放期间持续写入。
- Touch：仅按住推子时写入，松手停止。
- Latch：首次触碰后持续写入。
- 单次 undo：一次录制只产生一个 OverwriteAutomationCommand（AUD-15609）。
- 简化：连续采样按阈值简化为关键帧。
- 端到端：录制 → 命令 → apply → 关键帧落库 → undo 恢复。
- 重开后恢复：命令修改持久化的 automation dict。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.automation_recorder import (
    AutomationRecorder, OverwriteAutomationCommand, simplify_samples, _FaderSample,
)
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence


class TestSimplifySamples:
    """关键帧简化。"""

    def test_two_samples_unchanged(self):
        samples = [_FaderSample(0, 0.5), _FaderSample(10, 1.0)]
        result = simplify_samples(samples)
        assert len(result) == 2

    def test_linear_collapses_to_endpoints(self):
        samples = [_FaderSample(f, 0.5 + f * 0.05) for f in range(0, 21)]
        result = simplify_samples(samples, threshold=0.005)
        assert len(result) == 2
        assert result[0].frame == 0
        assert result[-1].frame == 20

    def test_curve_preserves_deviation(self):
        samples = [
            _FaderSample(0, 1.0),
            _FaderSample(5, 0.2),
            _FaderSample(10, 1.0),
        ]
        result = simplify_samples(samples, threshold=0.005)
        assert len(result) == 3

    def test_dedup_same_frame(self):
        samples = [
            _FaderSample(0, 0.5), _FaderSample(0, 0.6),
            _FaderSample(10, 1.0),
        ]
        result = simplify_samples(samples)
        frames = [s.frame for s in result]
        assert frames.count(0) == 1


class TestWriteMode:
    def test_write_records_all_frames(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        for f in range(10):
            rec.record(f, 0.5 + f * 0.05)
        cmd = rec.stop()
        assert cmd is not None
        assert len(cmd.keyframes) == 2  # 线性简化

    def test_write_ignores_fader_down(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5, fader_down=False)
        cmd = rec.stop()
        assert cmd is not None
        assert len(cmd.keyframes) >= 1

    def test_write_overwrites_on_new_start(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.cancel()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.8)
        cmd = rec.stop()
        assert cmd.keyframes[0][1] == 0.8


class TestTouchMode:
    def test_touch_only_when_fader_down(self):
        rec = AutomationRecorder()
        rec.start("volume", "touch", "t1")
        rec.record(0, 0.5, fader_down=True)
        rec.record(1, 0.6, fader_down=True)
        rec.record(2, 0.7, fader_down=False)
        rec.record(3, 0.8, fader_down=False)
        cmd = rec.stop()
        frames = [f for f, _ in cmd.keyframes]
        assert 0 in frames and 1 in frames
        assert 2 not in frames and 3 not in frames
        # AUD-15609：录制段只覆盖按住区间 [0, 1]
        assert cmd.overwrite_ranges == ((0, 1),)


class TestLatchMode:
    def test_latch_after_first_touch(self):
        rec = AutomationRecorder()
        rec.start("volume", "latch", "t1")
        rec.record(0, 0.5, fader_down=False)
        rec.record(1, 0.6, fader_down=True)
        rec.record(2, 0.7, fader_down=False)
        rec.record(3, 0.8, fader_down=False)
        cmd = rec.stop()
        frames = [f for f, _ in cmd.keyframes]
        assert 0 not in frames
        assert 1 in frames and 3 in frames


class TestSingleUndo:
    def test_produces_single_command(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        for f in range(50):
            rec.record(f, 0.3 + 0.01 * f)
        cmd = rec.stop()
        # AUD-15609：一次录制 = 一个覆盖式写入命令（单次 undo）
        assert isinstance(cmd, OverwriteAutomationCommand)
        assert len(cmd.keyframes) >= 2

    def test_undo_restores(self):
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.record(10, 1.0)
        cmd = rec.stop()
        new_seq, updated_cmd = cmd.apply(seq)
        new_track = new_seq.get_track_by_id("t1")
        assert "volume" in new_track.automation
        restored = updated_cmd.undo(new_seq)
        restored_track = restored.get_track_by_id("t1")
        assert "volume" not in restored_track.automation or not restored_track.automation.get("volume", []).keyframes


class TestEndToEnd:
    def test_recorded_keyframes_persisted(self):
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        for f in range(0, 101):
            rec.record(f, 0.5 + (f / 100.0) * 0.5)
        cmd = rec.stop()
        new_seq, _ = cmd.apply(seq)
        new_track = new_seq.get_track_by_id("t1")
        vt = new_track.automation["volume"]
        assert len(vt.keyframes) <= 5
        assert abs(vt.keyframes[0].value - 0.5) < 0.01
        assert abs(vt.keyframes[-1].value - 1.0) < 0.01

    def test_recorded_automation_serializes(self):
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.record(10, 1.0)
        cmd = rec.stop()
        new_seq, _ = cmd.apply(seq)
        d = new_seq.to_dict()
        seq2 = TimelineSequence.from_dict(d)
        track2 = seq2.get_track_by_id("t1")
        assert "volume" in track2.automation
        assert len(track2.automation["volume"].keyframes) >= 2

    def test_pan_recording(self):
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        rec = AutomationRecorder()
        rec.start("pan", "write", "t1")
        rec.record(0, -1.0)
        rec.record(10, 1.0)
        cmd = rec.stop()
        new_seq, _ = cmd.apply(seq)
        track2 = new_seq.get_track_by_id("t1")
        assert "pan" in track2.automation

    def test_mute_recording(self):
        track = EditTrack(track_id="t1", track_type="audio", index=0)
        seq = TimelineSequence(audio_tracks=(track,))
        rec = AutomationRecorder()
        rec.start("mute", "write", "t1")
        rec.record(0, 0.0)
        rec.record(10, 1.0)
        cmd = rec.stop()
        new_seq, _ = cmd.apply(seq)
        track2 = new_seq.get_track_by_id("t1")
        assert "mute" in track2.automation


class TestRecorderLifecycle:
    def test_cancel_no_command(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        rec.record(0, 0.5)
        rec.cancel()
        assert not rec.is_recording

    def test_stop_when_not_active_returns_none(self):
        rec = AutomationRecorder()
        assert rec.stop() is None

    def test_stop_no_samples_returns_none(self):
        rec = AutomationRecorder()
        rec.start("volume", "write", "t1")
        assert rec.stop() is None
