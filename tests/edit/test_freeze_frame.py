"""A2: 冻结帧（Freeze Frame）测试。

覆盖计划 A2 验证清单：
1. 冻结后右侧片段时长不变、所有 timeline 帧映射到同一源帧；
2. 命令 undo 后时间线与冻结前逐字段一致；
3. 序列化往返（freeze_source_frame 保留）；
4. from_dict 缺失字段 → None（正常播放，旧工程兼容）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (  # noqa: E402
    FreezeFrameAtPlayheadCommand,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    Rational,
    TimelineSequence,
    EditTrack,
)

FPS = 24.0


def _clip(
    clip_id: str = "c1",
    timeline_start: int = 0,
    timeline_duration: int = 96,
    source_start: int = 100,
    source_duration: int = 96,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset_a2",
        track_id="v1",
        timeline_start=timeline_start,
        timeline_duration=timeline_duration,
        source_start=source_start,
        source_duration=source_duration,
    )


def _sequence(clip: ClipInstance) -> TimelineSequence:
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=(clip,),
    )
    return TimelineSequence(video_tracks=(track,), fps=Rational(24, 1))


class TestFreezeModel:
    def test_freeze_maps_every_frame_to_same_source_frame(self) -> None:
        clip = _clip().with_freeze_frame(140)
        assert clip.freeze_source_frame == 140
        for frame in range(48, 96):
            assert clip.timeline_to_source_frame(frame) == 140

    def test_none_freeze_keeps_normal_mapping(self) -> None:
        clip = _clip()
        assert clip.freeze_source_frame is None
        assert clip.timeline_to_source_frame(24) == 124

    def test_negative_freeze_rejected(self) -> None:
        with pytest.raises(ValueError, match="freeze_source_frame"):
            _clip().with_freeze_frame(-1)

    def test_serialization_roundtrip(self) -> None:
        clip = _clip().with_freeze_frame(120)
        restored = ClipInstance.from_dict(clip.to_dict())
        assert restored.freeze_source_frame == 120
        # 旧工程 dict：无 freeze_source_frame → None
        plain = ClipInstance.from_dict(
            {**clip.to_dict(), "freeze_source_frame": None})
        assert plain.freeze_source_frame is None


class TestFreezeFrameCommand:
    def _applied(self):
        clip = _clip()
        seq = _sequence(clip)
        cmd = FreezeFrameAtPlayheadCommand.create("c1", 48)
        new_seq = cmd.apply(seq)
        return seq, new_seq, cmd

    def test_split_and_freeze(self) -> None:
        seq, new_seq, cmd = self._applied()
        track = new_seq.video_tracks[0]
        assert len(track.clips) == 2
        left = track.clips[0]
        right = track.clips[1]
        # 右段时长不变（96-48=48），从播放头开始
        assert right.timeline_start == 48
        assert right.timeline_duration == 48
        # 左段保留原 ID
        assert left.clip_id == "c1"
        assert left.timeline_duration == 48
        # 冻结：右段所有帧映射到分割点的源帧（= source_start 100 + 48 = 148）
        assert right.freeze_source_frame == 148
        for frame in range(48, 96):
            assert right.timeline_to_source_frame(frame) == 148
        # 左段正常播放
        assert left.freeze_source_frame is None
        assert left.timeline_to_source_frame(24) == 124

    def test_undo_restores_original_timeline(self) -> None:
        seq, new_seq, cmd = self._applied()
        undone = cmd.undo(new_seq)
        track = undone.video_tracks[0]
        assert len(track.clips) == 1
        restored = track.clips[0]
        # 逐字段还原
        assert restored == seq.video_tracks[0].clips[0]

    def test_command_serialization_roundtrip(self) -> None:
        seq, new_seq, cmd = self._applied()
        restored = FreezeFrameAtPlayheadCommand.from_dict(cmd.to_dict())
        assert restored.clip_id == "c1"
        assert restored.playhead_frame == 48
        assert restored._right_clip_id == cmd._right_clip_id
        # 序列化往返后 undo 仍可用
        undone = restored.undo(new_seq)
        assert len(undone.video_tracks[0].clips) == 1

    def test_playhead_outside_rejected(self) -> None:
        seq = _sequence(_clip())
        cmd = FreezeFrameAtPlayheadCommand.create("c1", 200)
        with pytest.raises(ValueError, match="strictly inside"):
            cmd.apply(seq)

    def test_frozen_segment_inherits_speed_mapping(self) -> None:
        """2x 变速片段冻结：源帧取分割点映射，右段时长按速度折算。"""
        clip = ClipInstance(
            clip_id="c_fast",
            asset_id="asset_a2",
            track_id="v1",
            timeline_start=0,
            timeline_duration=48,
            source_start=0,
            source_duration=96,
            speed=2.0,
        )
        seq = _sequence(clip)
        cmd = FreezeFrameAtPlayheadCommand.create("c_fast", 24)
        new_seq = cmd.apply(seq)
        right = new_seq.video_tracks[0].clips[1]
        assert right.freeze_source_frame == 48  # 24 * 2 = 源帧 48
        for frame in range(24, 48):
            assert right.timeline_to_source_frame(frame) == 48
