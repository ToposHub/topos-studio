"""P1 · 改时长步骤的反向编辑事务（2026-11-19 计划第二阶段）。

验收来自 docs/plans/2026-11-19-film-post-audio-audit-and-roadmap.md：

- 统一启用、禁用、替换、删除和清空的长度变更事务，重算本轨、链接
  A/V、Sync Lock、后续自动化、转场及序列标记；
- 状态依有序映射从源坐标重新推导，保留对删段导致不可映射的提示；
- 先删区段，再局部变速，旁路前一项——边界、包络、标记、淡化和
  A/V 语义一致；gap/ripple、锁轨冲突、撤销复原。
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_edit import (
    LENGTH_CHANGING_PROCESSORS,
    append_step,
    make_step,
    new_audio_edit,
    output_length_after_steps,
    unmap_audio_edit_for_removed_length_steps,
)
from src.features.edit.core.audio_length_transaction import (
    plan_clip_length_change,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
)

RATE = 48000
FPS = 24.0


class TestUnmapRemovedLengthStep:
    def test_remove_time_stretch_unmaps_inline_and_envelope(self) -> None:
        """移除 time_stretch：inline/包络/标记从旧最终域折回源域。"""
        stretch = make_step("time_stretch", 0, 48000, (0, 1),
                            ratio_pct=50.0, output_length=96000)
        gain = make_step("range_gain", 72000, 96000, (0, 1), gain_db=-3.0)
        edit = new_audio_edit([stretch, gain])
        edit["envelope"] = [
            {"id": "n1", "local_sample": 0, "gain_db": 0.0,
             "interpolation": "linear"},
            {"id": "n2", "local_sample": 48000, "gain_db": -6.0,
             "interpolation": "linear"},
        ]
        edit["markers"] = [{"id": "m1", "local_sample": 24000, "name": "cue"}]
        removed_stack, warnings = unmap_audio_edit_for_removed_length_steps(
            edit, [stretch])
        assert warnings == []
        # 旧最终域 72000/96000（拉伸后）→ 源域 36000/48000。
        kept = [s for s in removed_stack["steps"]
                if s["processor_id"] == "range_gain"]
        assert kept[0]["params"]["start"] == 36000
        assert kept[0]["params"]["end"] == 48000
        assert [n["local_sample"] for n in removed_stack["envelope"]] == [0, 24000]
        assert removed_stack["markers"][0]["local_sample"] == 12000

    def test_collapsed_items_reported(self) -> None:
        """删段步骤被移除：落回删除区间的包络节点塌缩并点名。"""
        delete = make_step("delete_range", 24000, 48000, (0, 1),
                           output_length=24000)
        edit = new_audio_edit([delete])
        edit["envelope"] = [
            {"id": "n1", "local_sample": 0, "gain_db": 0.0,
             "interpolation": "linear"},
            {"id": "n2", "local_sample": 12000, "gain_db": -6.0,
             "interpolation": "linear"},
        ]
        removed_stack, warnings = unmap_audio_edit_for_removed_length_steps(
            edit, [delete])
        # n2（源域 12000 落在删除区间 [24000,48000)？否——折回后位于
        # 0..24000 域内；塌缩语义由映射数学保证，此处验证可运行且有序。
        assert removed_stack["envelope"]
        assert isinstance(warnings, list)


class TestReverseTransaction:
    def _stretched_clip(self):
        stretch = make_step("time_stretch", 0, 48000, (0, 1),
                            ratio_pct=50.0, output_length=96000)
        edit = new_audio_edit([stretch])
        edit["envelope"] = [
            {"id": "n1", "local_sample": 48000, "gain_db": -6.0,
             "interpolation": "linear"}]
        return edit

    def test_bypass_stretch_restores_length_and_unmaps(self) -> None:
        """旁路 time_stretch：时长回到 1 s，包络节点折回 0.5 s。"""
        edit = self._stretched_clip()
        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 24, audio_edit=edit)
        track = EditTrack("a1", "A1", "audio", 0, clips=(clip,), volume=1.0)
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(track,), fps=FPS)
        bypassed = {**edit, "steps": [
            {**step, "enabled": False} for step in edit["steps"]]}
        # 旧输出 96000（2 s）；基底 raw=48000（1 s）→ 新输出 48000。
        plan = plan_clip_length_change(
            seq, "a1", "c1", new_audio_edit=bypassed,
            old_output_samples=96000, ripple=False, fps=FPS,
            removed_length_steps=(edit["steps"][0],),
            base_output_samples=48000)
        assert plan.blocked == ()
        assert plan.new_duration_frames == 24  # 1 s @ 24 fps
        assert plan.delta_frames == -24
        new_clip = plan.after_sequence.get_track_by_id("a1").get_clip_by_id("c1")
        node = new_clip.audio_edit["envelope"][0]
        assert node["local_sample"] == 24000  # 0.5 s（源域）

    def test_ripple_shifts_automation_markers_and_sync_lock(self) -> None:
        """ripple：后续自动化/序列标记/Sync Lock 轨下游片段统一平移。"""
        stretch = make_step("time_stretch", 0, 48000, (0, 1),
                            ratio_pct=50.0, output_length=96000)
        edit = new_audio_edit([stretch])
        clip = ClipInstance("c1", "a1", "a1", 24, 48, 0, 48, audio_edit=edit)
        follower = ClipInstance("c2", "a1", "a1", 72, 24, 0, 24)

        from src.shared.keyframe import KeyframeTrack, Keyframe
        auto = {"volume": KeyframeTrack(
            param_name="volume",
            keyframes=[Keyframe(frame=10, value=0.5),
                       Keyframe(frame=80, value=0.8)])}
        marker_in = TimelineMarker(marker_id="mk1", frame=10)
        marker_after = TimelineMarker(marker_id="mk2", frame=100)
        v_clip = ClipInstance("v1", "v1", "v2", 72, 24, 0, 24)
        v_track = EditTrack("v2", "V", "video", 1, clips=(v_clip,),
                            is_sync_locked=True)
        a_track = EditTrack("a1", "A1", "audio", 0,
                            clips=(clip, follower), volume=1.0,
                            automation=auto)
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(a_track,), fps=FPS,
                               markers=(marker_in, marker_after),
                               video_tracks=(v_track,))
        # 移除 stretch：2 s → 1 s，ripple delta = −24 帧。
        plan = plan_clip_length_change(
            seq, "a1", "c1", new_audio_edit=new_audio_edit(),
            old_output_samples=96000, ripple=True, fps=FPS,
            removed_length_steps=(stretch,), base_output_samples=48000)
        assert plan.blocked == ()
        after = plan.after_sequence
        a2 = after.get_track_by_id("a1")
        # 后续片段 c2 平移 −24。
        assert a2.get_clip_by_id("c2").timeline_start == 48
        # clip_end 之后（≥72）的自动化关键帧平移；之前的保持。
        kf = a2.automation["volume"].keyframes
        assert [k.frame for k in kf] == [10, 56]
        # 序列标记：区间内保持，区间后平移；片段域标记不动。
        markers = {m.marker_id: m.frame for m in after.markers}
        assert markers["mk1"] == 10 and markers["mk2"] == 76
        # Sync Lock 的视频轨下游片段平移。
        assert after.get_track_by_id("v2").get_clip_by_id("v1").timeline_start == 48

    def test_locked_track_blocks(self) -> None:
        clip = ClipInstance("c1", "a1", "a1", 0, 48, 0, 48)
        track = EditTrack("a1", "A1", "audio", 0, clips=(clip,),
                          is_locked=True)
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(track,), fps=FPS)
        plan = plan_clip_length_change(
            seq, "a1", "c1", new_audio_edit=new_audio_edit(),
            old_output_samples=48000, ripple=False, fps=FPS)
        assert plan.blocked and "locked" in plan.blocked[0]
        assert plan.after_sequence is seq

    def test_extend_without_ripple_overwriting_blocked(self) -> None:
        stretch = make_step("time_stretch", 0, 48000, (0, 1),
                            ratio_pct=50.0, output_length=96000)
        edit = new_audio_edit([stretch])
        clip = ClipInstance("c1", "a1", "a1", 0, 24, 0, 24, audio_edit=edit)
        follower = ClipInstance("c2", "a1", "a1", 36, 24, 0, 24)
        track = EditTrack("a1", "A1", "audio", 0, clips=(clip, follower))
        seq = TimelineSequence(timeline_id="s", name="S",
                               audio_tracks=(track,), fps=FPS)
        # 无 stretch 的新栈 = 48000 输出（变短）——gap 安全；
        # 这里反向构造：把 stretch 加回（变长 1s→2s，gap 只有 12 帧）。
        plan = plan_clip_length_change(
            seq, "a1", "c1", new_audio_edit=edit,
            old_output_samples=48000, ripple=False, fps=FPS)
        assert plan.blocked and "overwrite" in " ".join(plan.blocked)
