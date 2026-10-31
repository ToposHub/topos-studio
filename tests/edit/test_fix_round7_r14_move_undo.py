"""第七轮审计 0B 回归：旧跨轨移动命令 Undo 回 0（R14）。

``MoveClipToExistingTrackCommand.create`` 的旧注册入口允许省略
``old_start``；apply 已知真实原位置却不上填，undo 在缺省时把片段回退
到第 0 帧（100→300→Undo 变 0 而非 100）。序列化重放（from_dict 后
old 为 None）同样命中。验收同时断言位置与关键帧。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.commands import (
    AddKeyframeCommand,
    MoveClipToExistingTrackCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_core


def _timeline(with_keyframe: bool = True) -> TimelineSequence:
    clip = ClipInstance("c1", "asset", "v1", 100, 50, 0, 50)
    track_v1 = EditTrack("v1", "V1", "video", 0, clips=(clip,))
    track_v2 = EditTrack("v2", "V2", "video", 1)
    seq = TimelineSequence(
        timeline_id="seq-r14", name="R14", width=1920, height=1080,
        fps=Rational(25, 1), video_tracks=(track_v1, track_v2),
    )
    if with_keyframe:
        cmd = AddKeyframeCommand(
            command_id="k", label="kf", track_id="v1", clip_id="c1",
            param_name="transform.translate_x", frame=100, value=10.0)
        seq = cmd.apply(seq)
        cmd = AddKeyframeCommand(
            command_id="k2", label="kf2", track_id="v1", clip_id="c1",
            param_name="transform.translate_x", frame=149, value=40.0)
        seq = cmd.apply(seq)
    return seq


def _clip_of(timeline: TimelineSequence) -> ClipInstance:
    return timeline.get_track_by_id("v1" if timeline.get_track_by_id(
        "v1").get_clip_by_id("c1") else "v2").get_clip_by_id("c1")


def _locate(timeline: TimelineSequence):
    for track in timeline.video_tracks:
        clip = track.get_clip_by_id("c1")
        if clip is not None:
            return track.track_id, clip
    raise AssertionError("clip c1 not found")


class TestR14LegacyCrossTrackMoveUndo:
    def test_undo_without_old_start_restores_position_and_keyframes(self):
        """缺省 old_start：100→300→Undo 回 100（不是 0），关键帧帧位
        随动恢复（100/149），值不丢。"""
        timeline = _timeline()
        command = MoveClipToExistingTrackCommand.create(
            source_track_id="v1", target_track_id="v2",
            clip_id="c1", new_start=300)  # 旧入口：old_start 缺省

        moved = command.apply(timeline)
        track_id, moved_clip = _locate(moved)
        assert track_id == "v2" and moved_clip.timeline_start == 300

        undone = command.undo(moved)
        track_id, clip = _locate(undone)
        assert track_id == "v1"
        assert clip.timeline_start == 100, \
            f"Undo 应回原位置 100，实测 {clip.timeline_start}"
        kf_frames = [kf.frame for kf in
                     clip.animated_params["transform.translate_x"]
                     .keyframes.keyframes]
        assert kf_frames == [100, 149], f"关键帧应回到 100/149：{kf_frames}"
        values = [kf.value for kf in
                  clip.animated_params["transform.translate_x"]
                  .keyframes.keyframes]
        assert values == [10.0, 40.0]

    def test_serialized_replay_undo_restores_position(self):
        """to_dict/from_dict 重放（old 序列化为 None 或旧值）同样正确。"""
        timeline = _timeline()
        command = MoveClipToExistingTrackCommand.create(
            source_track_id="v1", target_track_id="v2",
            clip_id="c1", new_start=300)
        replayed = MoveClipToExistingTrackCommand.from_dict(command.to_dict())

        moved = replayed.apply(timeline)
        undone = replayed.undo(moved)
        _, clip = _locate(undone)
        assert clip.timeline_start == 100

    def test_repeated_undo_redo_stable(self):
        """重复 Apply/Undo 循环逐字段稳定。"""
        timeline = _timeline()
        command = MoveClipToExistingTrackCommand.create(
            source_track_id="v1", target_track_id="v2",
            clip_id="c1", new_start=300)
        current = timeline
        for _ in range(3):
            current = command.apply(current)
            _, clip = _locate(current)
            assert clip.timeline_start == 300
            kf = [k.frame for k in clip.animated_params[
                "transform.translate_x"].keyframes.keyframes]
            assert kf == [300, 349], f"apply 后关键帧 300/349：{kf}"
            current = command.undo(current)
            _, clip = _locate(current)
            assert clip.timeline_start == 100
            kf = [k.frame for k in clip.animated_params[
                "transform.translate_x"].keyframes.keyframes]
            assert kf == [100, 149]

    def test_explicit_old_start_still_wins(self):
        """显式 old_start 不被 apply 覆盖（既有行为保持）。"""
        timeline = _timeline()
        command = MoveClipToExistingTrackCommand.create(
            source_track_id="v1", target_track_id="v2",
            clip_id="c1", new_start=300, old_start=100)
        undone = command.undo(command.apply(timeline))
        _, clip = _locate(undone)
        assert clip.timeline_start == 100
