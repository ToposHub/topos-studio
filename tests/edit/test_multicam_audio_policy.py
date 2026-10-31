"""F2.5 · 多机位音频切换策略——域模型/受限事务/切角跟随联动。

三模式：fixed_main（固定主音轨，现状声明化）/ video_only（仅切视频）/
follow_angle（音频跟随切角）。策略可撤销（受限元数据事务，冲突拒绝不
半写）；follow 模式切角 = 视频+链接音频同帧切换、单宏单步撤销；导出按
时间线最终音频片段一致渲染（音频轨即真相）。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    CommandStack,
    MacroCommand,
    SwitchMulticamAngleCommand,
)
from src.features.edit.core.multicam import (
    MULTICAM_AUDIO_POLICIES,
    METADATA_ASSET_GROUP_KEY,
    MulticamAngle,
    MulticamAngleGroup,
    SetMulticamAudioPolicyCommand,
    multicam_group_from_asset,
)
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _asset(asset_id: str, with_audio: bool = True) -> MediaAsset:
    meta = {"fps": 24.0, "duration": 10.0}
    if with_audio:
        meta["audio_info"] = {"channels": 2}
    return MediaAsset(
        asset_id, f"/tmp/{asset_id}.mp4", "video", asset_id,
        metadata=meta)


def _group(audio_policy: str = "fixed_main") -> MulticamAngleGroup:
    return MulticamAngleGroup(
        group_id="g1", name="Interview",
        angles=(
            MulticamAngle(angle_id="1", asset_id="cam1", label="Wide"),
            MulticamAngle(angle_id="2", asset_id="cam2", sync_offset=0),
        ),
        default_angle_id="1", audio_policy=audio_policy)


def _stamped_project(group: MulticamAngleGroup,
                     with_audio: bool = True) -> EditProject:
    import copy
    assets = {}
    for angle in group.angles:
        asset = _asset(angle.asset_id, with_audio=with_audio)
        meta = dict(asset.metadata)
        meta[METADATA_ASSET_GROUP_KEY] = copy.deepcopy(group.to_dict())
        from dataclasses import replace as _r
        assets[angle.asset_id] = _r(asset, metadata=meta)
    return EditProject("p", "P", assets=assets)


def _av_sequence() -> TimelineSequence:
    """链接 V/A 对（多机位视频片段 cam1 + 音频片段 cam1）。"""
    video = ClipInstance(
        "cv", "cam1", "v1", 0, 100, 0, 100, linked_clip_id="ca")
    audio = ClipInstance(
        "ca", "cam1", "a1", 0, 100, 0, 100, linked_clip_id="cv")
    return TimelineSequence(
        timeline_id="s", name="S", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0, clips=(audio,)),))


class TestPolicyModel:
    def test_field_roundtrip_and_legacy_default(self) -> None:
        group = _group(audio_policy="follow_angle")
        restored = MulticamAngleGroup.from_dict(group.to_dict())
        assert restored.audio_policy == "follow_angle"
        legacy = MulticamAngleGroup.from_dict({
            "group_id": "g", "name": "", "angles": [
                {"angle_id": "1", "asset_id": "a"}],
            "default_angle_id": "1"})
        assert legacy.audio_policy == "fixed_main"

    def test_invalid_policy_falls_back(self) -> None:
        restored = MulticamAngleGroup.from_dict(
            {**_group().to_dict(), "audio_policy": "bogus"})
        assert restored.audio_policy == "fixed_main"
        assert set(MULTICAM_AUDIO_POLICIES) == {
            "fixed_main", "video_only", "follow_angle"}


class TestPolicyCommand:
    def test_apply_undo_roundtrip(self) -> None:
        project = _stamped_project(_group())
        cmd = SetMulticamAudioPolicyCommand.create(
            project, _group(), "follow_angle")
        applied = cmd.apply(project)
        group = multicam_group_from_asset(applied.assets["cam1"])
        assert group is not None and group.audio_policy == "follow_angle"
        undone = cmd.undo(applied)
        group2 = multicam_group_from_asset(undone.assets["cam1"])
        assert group2 is not None and group2.audio_policy == "fixed_main"

    def test_conflicting_change_refused_no_half_write(self) -> None:
        project = _stamped_project(_group())
        first = SetMulticamAudioPolicyCommand.create(
            project, _group(), "video_only")
        project = first.apply(project)
        # 用过期快照（期望旧组）再 apply——必须整体拒绝
        stale = SetMulticamAudioPolicyCommand.create(
            _stamped_project(_group()), _group(), "follow_angle")
        with pytest.raises(ValueError, match="已被其他操作修改"):
            stale.apply(project)
        group = multicam_group_from_asset(project.assets["cam1"])
        assert group.audio_policy == "video_only"  # 零半写

    def test_unknown_policy_rejected(self) -> None:
        project = _stamped_project(_group())
        with pytest.raises(ValueError, match="unknown audio policy"):
            SetMulticamAudioPolicyCommand.create(
                project, _group(), "bogus")


class TestFollowAngleSwitchLinkage:
    def test_av_pair_switches_together_single_undo(self) -> None:
        """follow 模式：视频+音频同帧切到 cam2，一个宏一步撤销。"""
        timeline = _av_sequence()
        stack = CommandStack()
        video_cmd = SwitchMulticamAngleCommand.create(
            "v1", "cv", switch_frame=40, angle_asset_id="cam2")
        audio_cmd = SwitchMulticamAngleCommand.create(
            "a1", "ca", switch_frame=40, angle_asset_id="cam2")
        macro = MacroCommand.create("Switch angle (A/V)",
                                    [video_cmd, audio_cmd])
        switched = stack.execute(macro, timeline)
        video_clips = sorted(
            switched.video_tracks[0].clips, key=lambda c: c.timeline_start)
        audio_clips = sorted(
            switched.audio_tracks[0].clips, key=lambda c: c.timeline_start)
        assert len(video_clips) == 2 and len(audio_clips) == 2
        # 切点后两轨都指向 cam2（对齐成对）
        assert video_clips[1].asset_id == "cam2"
        assert audio_clips[1].asset_id == "cam2"
        assert video_clips[1].timeline_start == audio_clips[1].timeline_start
        # 单步撤销：两轨都回 cam1 且各恢复单片
        restored = stack.undo(switched)
        assert len(restored.video_tracks[0].clips) == 1
        assert len(restored.audio_tracks[0].clips) == 1
        assert restored.video_tracks[0].clips[0].asset_id == "cam1"
        assert restored.audio_tracks[0].clips[0].asset_id == "cam1"

    def test_video_only_policy_leaves_audio_untouched(self) -> None:
        """video_only：只切视频（音频轨不动——现状语义声明化）。"""
        timeline = _av_sequence()
        stack = CommandStack()
        cmd = SwitchMulticamAngleCommand.create(
            "v1", "cv", switch_frame=40, angle_asset_id="cam2")
        switched = stack.execute(cmd, timeline)
        assert len(switched.audio_tracks[0].clips) == 1
        assert switched.audio_tracks[0].clips[0].asset_id == "cam1"


# =====================================================================
# F2.5 片2 · 插入侧成对落轨（fixed_main/follow_angle 主角度带音频 →
# A/V 波纹插入；video_only → video-only；重建组沿用既有策略）
# =====================================================================

def _timeline() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="s", name="S", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),))


class TestInsertPairLanding:
    def test_default_policy_lands_av_pair(self) -> None:
        """有声主角度 + 默认 fixed_main：创建即落链接 A/V 对（主音轨=
        初始角度音频），单步 Undo 移除两片。"""
        from src.features.edit.core.multicam import plan_multicam_insert
        project = _stamped_project(_group())
        plan = plan_multicam_insert(project, _timeline(),
                                    ["cam1", "cam2"], "v1", 0)
        assert plan.group.audio_policy == "fixed_main"
        seq, prepared = plan.command.apply(_timeline())
        video_clips = [c for t in seq.video_tracks for c in t.clips]
        audio_clips = [c for t in seq.audio_tracks for c in t.clips]
        assert len(video_clips) == 1 and len(audio_clips) == 1
        video, audio = video_clips[0], audio_clips[0]
        assert video.asset_id == "cam1" and audio.asset_id == "cam1"
        assert video.linked_clip_id == audio.clip_id
        assert audio.linked_clip_id == video.clip_id
        assert video.timeline_start == audio.timeline_start == 0
        undone = prepared.undo(seq)
        assert not [c for t in undone.video_tracks for c in t.clips]
        assert not [c for t in undone.audio_tracks for c in t.clips]

    def test_video_only_policy_stays_video_only(self) -> None:
        """video_only：不落音轨（仅切视频语义从插入起成立）。"""
        from src.features.edit.core.multicam import plan_multicam_insert
        project = _stamped_project(_group(audio_policy="video_only"))
        plan = plan_multicam_insert(project, _timeline(),
                                    ["cam1", "cam2"], "v1", 0)
        seq, _prepared = plan.command.apply(_timeline())
        assert [c for t in seq.video_tracks for c in t.clips]
        assert not [c for t in seq.audio_tracks for c in t.clips]

    def test_rebuild_carries_existing_policy(self) -> None:
        """重建已同组素材：沿用既有策略（follow_angle 不被重置）。"""
        from src.features.edit.core.multicam import plan_multicam_insert
        project = _stamped_project(_group(audio_policy="follow_angle"))
        plan = plan_multicam_insert(project, _timeline(),
                                    ["cam1", "cam2"], "v1", 0)
        assert plan.group.audio_policy == "follow_angle"

    def test_silent_main_angle_stays_video_only(self) -> None:
        """主角度无声（纯视频机位）：保持 video-only 现状，不造空音轨。"""
        from src.features.edit.core.multicam import plan_multicam_insert
        project = _stamped_project(_group(), with_audio=False)
        plan = plan_multicam_insert(project, _timeline(),
                                    ["cam1", "cam2"], "v1", 0)
        seq, _prepared = plan.command.apply(_timeline())
        assert [c for t in seq.video_tracks for c in t.clips]
        assert not [c for t in seq.audio_tracks for c in t.clips]

    def test_follow_angle_pair_export_truth(self) -> None:
        """导出一致性锚点：follow_angle 组的音频轨即渲染真相——成对
        落轨后切角宏（片1）换的正是这条音轨上的片段。"""
        from src.features.edit.core.commands import (
            MacroCommand,
            SwitchMulticamAngleCommand,
        )
        from src.features.edit.core.multicam import plan_multicam_insert
        project = _stamped_project(_group(audio_policy="follow_angle"))
        plan = plan_multicam_insert(project, _timeline(),
                                    ["cam1", "cam2"], "v1", 0)
        seq, _prepared = plan.command.apply(_timeline())
        video = [c for t in seq.video_tracks for c in t.clips][0]
        audio = [c for t in seq.audio_tracks for c in t.clips][0]
        macro = MacroCommand.create("Switch angle (A/V)", [
            SwitchMulticamAngleCommand.create(
                "v1", video.clip_id, switch_frame=20,
                angle_asset_id="cam2"),
            SwitchMulticamAngleCommand.create(
                "a1", audio.clip_id, switch_frame=20,
                angle_asset_id="cam2"),
        ])
        switched, _updated = macro.apply(seq)
        v = sorted((c for t in switched.video_tracks for c in t.clips),
                   key=lambda c: c.timeline_start)
        a = sorted((c for t in switched.audio_tracks for c in t.clips),
                   key=lambda c: c.timeline_start)
        # 切点后 A/V 同刻同源（导出读同一批片段 = 一致）
        assert v[1].asset_id == a[1].asset_id == "cam2"
        assert v[1].timeline_start == a[1].timeline_start == 20
