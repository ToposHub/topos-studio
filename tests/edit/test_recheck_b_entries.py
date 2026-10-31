"""复查批次 B-4/5/6 回归：R15 Properties Speed / R18 修剪预览源域 /
R20 字幕轨 FPS / R21 Bezier handle / U01 设置事务。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md
§3.1–3.3（RI-04/RI-07/F-R01/F-R02/F-U01）。
"""

from __future__ import annotations

from typing import Any

import pytest
from dataclasses import replace

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    ChangeClipSpeedCommand,
    UpdateClipParamCommand,
    UpdateTimelineSettingsCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    SubtitleClip,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 96,
          **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=kw.pop("asset_id", f"a-{clip_id}"),
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 120),
        **kw,
    )


def _av_timeline() -> TimelineSequence:
    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_track_added(EditTrack(track_id="a1", name="A1", track_type="audio", index=0))
    t = t.with_clip_added("v1", _clip("vc", "v1", source_duration=120, linked_clip_id="ac"))
    t = t.with_clip_added("a1", _clip("ac", "a1", source_duration=120, linked_clip_id="vc"))
    return t


# ------------------------------------------------------------------ R15

def test_change_speed_mixed_fps_updates_both_linked() -> None:
    """RI-04：30fps 源 96 帧 linked V/A@1x，2x → 两片都 48 帧
    （Properties 改道 ChangeClipSpeedCommand 后的领域语义）。"""
    timeline = _av_timeline()
    for cid in ("vc", "ac"):
        clip = timeline.get_clip_by_id(cid)
        timeline = timeline.with_track_updated(
            timeline.get_track_by_id(clip.track_id).with_clip_updated(
                clip.__class__(**{**clip.__dict__,
                                  "_source_fps_num": 30, "_source_fps_den": 1,
                                  "_timeline_fps_num": 24, "_timeline_fps_den": 1})
                if False else
                ClipInstance(
                    clip_id=clip.clip_id, asset_id=clip.asset_id,
                    track_id=clip.track_id,
                    timeline_start=clip.timeline_start,
                    timeline_duration=clip.timeline_duration,
                    source_start=clip.source_start,
                    source_duration=clip.source_duration,
                    linked_clip_id=clip.linked_clip_id,
                    _source_fps_num=30, _source_fps_den=1,
                    _timeline_fps_num=24, _timeline_fps_den=1,
                )
            )
        )
    cmd = ChangeClipSpeedCommand.create("vc", "v1", 2.0)
    new_timeline, updated = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("vc").timeline_duration == 48
    assert new_timeline.get_clip_by_id("ac").timeline_duration == 48


def test_update_clip_param_speed_uses_unified_duration() -> None:
    """RI-04 纵深防御：UpdateClipParamCommand 的 speed 分支同公式。"""
    timeline = _av_timeline()
    clip = timeline.get_clip_by_id("vc")
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_clip_updated(
            ClipInstance(
                clip_id="vc", asset_id=clip.asset_id, track_id="v1",
                timeline_start=0, timeline_duration=96,
                source_start=0, source_duration=120,
                linked_clip_id="ac",
                _source_fps_num=30, _source_fps_den=1,
                _timeline_fps_num=24, _timeline_fps_den=1,
            )
        )
    )
    cmd = UpdateClipParamCommand(
        command_id="cmd", label="s", track_id="v1", clip_id="vc",
        param_type="speed", new_value=SpeedParams(speed=2.0),
        old_value=SpeedParams(speed=1.0),
    )
    new_timeline = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("vc").timeline_duration == 48


# ------------------------------------------------------------------ R18

def test_trim_preview_matches_commit_domain_2x(qtbot: Any) -> None:
    """RI-07：2x 片段拖中预览的源区间 = release 提交值（preview 190 →
    统一映射 180）。"""
    from PyQt6.QtCore import QPointF

    from src.features.edit.ui.timeline_interaction import InteractionState
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    )
    timeline = timeline.with_clip_added(
        "v1",
        ClipInstance(
            clip_id="c", asset_id="a", track_id="v1",
            timeline_start=0, timeline_duration=100,
            source_start=0, source_duration=200,
            speed=SpeedParams(speed=2.0),
        ),
    )
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    item = view._items_map["c"]

    view._interaction.begin(
        InteractionState.TRIMMING_LEFT, QPointF(0, 0),
        primary_clip_id="c", primary_track_id="v1",
    )
    item._original_start = 0
    item._original_duration = 100
    item._original_source_start = 0
    item._original_source_duration = 200

    view._on_clip_trim_left("c", "v1", 10)
    # 预览：10 时间线帧 × 2x = 20 源帧（旧预览只吃 10 源帧）
    assert (item._source_start, item._source_duration) == (20, 180)
    # 与 release 提交的领域换算一致
    clip = view._timeline.get_clip_by_id("c")
    assert clip.with_left_edge_source_values(10) == (20, 180)


# ------------------------------------------------------------------ R20

def test_fps_change_scales_real_subtitle_track_and_words() -> None:
    """F-R01：真实 subtitle_tracks 的 cue 与 word_timings 同域缩放。"""
    from src.features.edit.core.captions.model import CaptionWordTiming
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.shared.media.metadata import Rational

    class _Host(EditControllerMixin):
        pass

    timeline = TimelineSequence(timeline_id="t1", name="T", fps=Rational(24, 1))
    sub_track = EditTrack(track_id="s1", name="S1", track_type="subtitle", index=0)
    timeline = timeline.with_track_added(sub_track)
    sub = SubtitleClip(
        clip_id="sc", track_id="s1",
        timeline_start=24, timeline_duration=24, text="cue",
    )
    timeline = timeline.with_subtitle_clip_added("s1", sub)

    from src.shared.media.metadata import Rational
    rebuilt = _Host()._with_timeline_fps(timeline, Rational(48, 1))
    sub_tracks = rebuilt.subtitle_tracks
    assert len(sub_tracks) == 1
    scaled_sub = sub_tracks[0].subtitle_clips[0]
    assert (scaled_sub.timeline_start, scaled_sub.timeline_duration) == (48, 48)


def test_fps_change_scales_word_timings_when_present() -> None:
    """F-R01/N17（RF01 修正）：词级时间码真实存放在 SubtitleClip 的
    caption_metadata.word_timings——此前测试用 fake 顶层字段，生产路
    径 getattr(sub, "word_timings") 永远 None，24→48 后词级仍 24–48。
    真实模型 24↔48 词级与 cue 同秒。"""
    from src.features.edit.core.captions.model import (
        CaptionCueMetadata, CaptionWordTiming,
    )
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.shared.media.metadata import Rational

    class _Host2(EditControllerMixin):
        pass

    seq = TimelineSequence(timeline_id="t1", name="T", fps=Rational(24, 1))
    sub_track = EditTrack(track_id="s1", name="S1", track_type="subtitle", index=0)
    seq = seq.with_track_added(sub_track)
    sub = SubtitleClip(
        clip_id="sc", track_id="s1",
        timeline_start=24, timeline_duration=24, text="cue",
        caption_metadata=CaptionCueMetadata(
            speaker="spk",
            word_timings=(
                CaptionWordTiming(text="w", start_frame=24, end_frame=48),
            ),
        ),
    )
    seq = seq.with_subtitle_clip_added("s1", sub)
    rebuilt = _Host2()._with_timeline_fps(seq, Rational(48, 1))
    scaled = rebuilt.subtitle_tracks[0].subtitle_clips[0]
    # 词级时间码随 FPS 同域缩放
    word = scaled.caption_metadata.word_timings[0]
    assert (word.start_frame, word.end_frame) == (48, 96)
    # 其余 metadata 字段保留
    assert scaled.caption_metadata.speaker == "spk"


# ------------------------------------------------------------------ R21

def test_time_scaled_scales_absolute_bezier_handles() -> None:
    """F-R02：绝对 handle.x 随 FPS 倍率缩放，同秒曲线值保持。"""
    from src.shared.keyframe import (
        InterpolationType,
        Keyframe,
        KeyframeTrack,
    )

    track = KeyframeTrack(
        param_name="x",
        keyframes=[
            Keyframe(
                frame=0, value=0.0,
                interpolation=InterpolationType.BEZIER,
                tangent_mode="broken",
                handle_left=(-2.0, 0.0),
                handle_right=(20.0, 0.0),
            ),
            Keyframe(frame=24, value=1.0),
        ],
    )
    scaled = track.time_scaled(2)
    k0 = scaled.keyframes[0]
    assert k0.frame == 0
    assert k0.handle_right == (40.0, 0.0)
    assert k0.handle_left == (-4.0, 0.0)


# ------------------------------------------------------------------ U01

def test_update_timeline_settings_command_roundtrip() -> None:
    """U01 领域事务：apply/undo 全 payload 交换，revision 单调。"""
    from src.features.edit.core.project import EditProject, TimelineSequenceRef

    before = _av_timeline()
    after = TimelineSequence(
        timeline_id="t1", name="T48", fps=48.0,
    )
    project = EditProject(project_id="p1", name="P")
    ref = TimelineSequenceRef(
        timeline_id="t1", name="T", fps=24.0,
        width=1920, height=1080, sample_rate=48000,
    )
    project = project.__class__(
        **{**project.__dict__, "timelines": {"t1": ref}, "revision": 7}
    )

    # N20/RF04：命令返回结构化 ProjectTransactionResult（内容快照不携
    # 发布 revision，project revision 由 ProjectCommandStack 单调推进）
    cmd = UpdateTimelineSettingsCommand.create("t1", before, after)
    result = cmd.apply(project)
    assert float(result.sequence_map["t1"].fps) == 48.0

    # 真实 ProjectStack 接受该命令（此前栈对三元组 replace() 抛
    # TypeError，设置事务从不能经真实历史提交）
    from src.features.edit.core.commands import ProjectCommandStack

    stack = ProjectCommandStack()
    p2 = stack.execute(cmd, project)
    assert p2.revision == 8
    p3 = stack.undo(p2)
    # 发布 revision 单调：undo 后 9（不回退到 7）
    assert p3.revision == 9
    assert float(stack.last_sequence_updates["t1"].fps) == 24.0
    p4 = stack.redo(p3)
    assert p4.revision == 10
    assert float(stack.last_sequence_updates["t1"].fps) == 48.0
