"""B22 核心回归：大工程保存/重开声音身份稳定 与 命令可逆不变量。

主计划 §17 验收的自动化部分（CI 可跑）：

- 30× 序列 to_dict/from_dict 往返：sound hash 不变（含 trim/VCA/sends
  等全部声音字段——用 B00 参考工程）。
- 100× undo/redo（CoalesceAutomationTrimCommand / SetTrackSoloSafeCommand /
  重放序列化快照）：模型求值与轨迹回到原始状态。
- 保存/重开不该保留的运行态不序列化（PCM 缓存/延迟线是引擎态，模型
  序列化不含——由 to_dict 白名单保证）。

完整 30×磁盘工程 + 90 分钟 64 轨负载属夜间长压（B22 手册），不在单测。
"""
from __future__ import annotations

import math

import pytest

from src.features.edit.core.audio_graph_snapshot import build_audio_graph_snapshot
from src.features.edit.core.commands import SetTrackSoloSafeCommand
from src.features.edit.core.automation_recorder import (
    CoalesceAutomationTrimCommand,
)
from tests.support.reference_project import build_reference_sequence


class TestSaveReopenHashStability:
    def test_thirty_roundtrips_preserve_sound_hash(self) -> None:
        """30 次保存/重开（模型序列化往返）→ sound hash 不变。"""
        sequence = build_reference_sequence()
        base_hash = build_audio_graph_snapshot(sequence).stable_hash
        current = sequence
        for i in range(30):
            payload = current.to_dict()
            reopened = type(current).from_dict(payload)
            snapshot = build_audio_graph_snapshot(reopened)
            assert snapshot.is_valid, snapshot.validation_errors
            assert snapshot.stable_hash == base_hash, f"roundtrip {i} drifted"
            current = reopened
        # 快照对象级相等（往返两次互相比对）
        again = build_audio_graph_snapshot(
            type(current).from_dict(current.to_dict()))
        assert again.stable_hash == base_hash

    def test_unknown_fields_survive_roundtrip(self) -> None:
        """其他模块 payload（未知键）不丢（B22 步骤 5）。"""
        sequence = build_reference_sequence()
        payload = sequence.to_dict()
        payload["future_feature"] = {"enabled": True, "payload": [1, 2]}
        restored = type(sequence).from_dict(payload)
        assert restored.to_dict().get("future_feature") == {
            "enabled": True, "payload": [1, 2]}


class TestCommandReversibility:
    def _timeline(self):
        from src.features.edit.core.timeline_v2 import (
            ClipInstance, EditTrack, TimelineSequence, VcaGroup)
        clip = ClipInstance(clip_id="c", asset_id="a", track_id="t",
                            timeline_start=0, timeline_duration=48,
                            source_start=0, source_duration=48)
        track = (EditTrack("t", "T", "audio", 0, clips=(clip,), volume=0.8)
                 .with_automation_trim("volume", 0.5).with_solo_safe(True))
        return TimelineSequence(
            audio_tracks=(track,),
            vcas=(VcaGroup(vca_id="v", member_track_ids=("t",), gain_db=-3.0),),
        )

    def test_hundred_undo_redo_returns_original(self) -> None:
        """100× undo/redo 循环 → trim/solo_safe/曲线与初始完全一致。"""
        timeline = self._timeline()
        initial_track = timeline.get_track_by_id("t")
        initial = {
            "trim": initial_track.automation_trim.get("volume"),
            "solo_safe": initial_track.solo_safe,
            "volume": initial_track.volume,
            "value": initial_track.get_automation_value("volume", 12, 1.0),
        }
        coalesce = CoalesceAutomationTrimCommand.create("t")
        timeline, applied_coalesce = coalesce.apply(timeline)
        solo = SetTrackSoloSafeCommand.create("t", False)
        timeline, applied_solo = solo.apply(timeline)
        # 100 次交替 undo/redo
        for i in range(100):
            if i % 2 == 0:
                timeline = applied_solo.undo(timeline)
                timeline = applied_coalesce.undo(timeline)
            else:
                timeline, _ = coalesce.apply(timeline)
                timeline, _ = solo.apply(timeline)
        # 最终回到（redo 应用后的）提交态；再 undo 回原始态
        timeline = applied_solo.undo(timeline)
        timeline = applied_coalesce.undo(timeline)
        track = timeline.get_track_by_id("t")
        assert track.automation_trim.get("volume") == initial["trim"]
        assert track.solo_safe == initial["solo_safe"]
        assert math.isclose(track.volume, initial["volume"])
        assert math.isclose(track.get_automation_value("volume", 12, 1.0),
                            initial["value"], rel_tol=1e-12)

    def test_trim_coalesce_value_parity(self) -> None:
        """coalesce 前后 120 个采样点的模型求值一致（声音不变合同）。"""
        timeline = self._timeline()
        track = timeline.get_track_by_id("t")
        frames = list(range(0, 120, 1))
        # 引擎求值口径：default = 该轨 volume（实时 state.volume 同语义）
        before = [track.get_automation_value("volume", f, track.volume)
                  for f in frames]
        cmd = CoalesceAutomationTrimCommand.create("t")
        new_timeline, _applied = cmd.apply(timeline)
        after_track = new_timeline.get_track_by_id("t")
        after = [after_track.get_automation_value("volume", f, after_track.volume)
                 for f in frames]
        for b, a in zip(before, after):
            assert math.isclose(b, a, rel_tol=1e-12), (b, a)
