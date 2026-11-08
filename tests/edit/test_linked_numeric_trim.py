"""B19：互链 A/V 数值 trim 共同边界（域级计划，UI/service 共用）。

反例（独立探针复现）：互链 100 帧对，源余量视频 +10、音频 +3；
ripple+5 逐成员独立钳制成 +5/+3，宏提交后 105/103 失步。修复后由
``build_linked_numeric_trim_command`` 统一求共同区间：reject 策略整次
拒绝、clamp 策略全组同一 delta；单步 undo；两侧/锁轨/单向链接覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.commands import CommandStack
from src.features.edit.core.timeline_ops import (
    build_linked_numeric_trim_command,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _pair_timeline(*, audio_source_start: int = 897,
                   video_source_start: int = 890,
                   locked_audio: bool = False) -> TimelineSequence:
    video = ClipInstance(
        "v", "asset", "v1", 100, 100, video_source_start, 100,
        linked_clip_id="a")
    audio = ClipInstance(
        "a", "asset", "a1", 100, 100, audio_source_start, 100,
        linked_clip_id="v")
    return TimelineSequence(
        "linked-numeric", "Linked", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(video,)),),
        audio_tracks=(EditTrack(
            "a1", "A1", "audio", 0, clips=(audio,),
            is_locked=locked_audio),),
    )


def _unwrap(result: Any) -> TimelineSequence:
    return result[0] if isinstance(result, tuple) else result


def test_b19_reject_policy_refuses_unequal_headroom(qtbot: Any) -> None:
    """反例：请求 +5 超出音频 +3 余量 → 整次拒绝，零写入。"""
    timeline = _pair_timeline()
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "right", 5, mode="ripple",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="reject")
    assert cmd is None and common == 0
    assert timeline.get_clip_by_id("v").timeline_duration == 100


def test_b19_clamp_policy_moves_group_with_common_delta(qtbot: Any) -> None:
    """clamp：全组钳到共同 +3，A/V 同终点，单步 undo。"""
    timeline = _pair_timeline()
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "right", 5, mode="ripple",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="clamp")
    assert cmd is not None and common == 3
    stack = CommandStack()
    changed = stack.execute(cmd, timeline)
    assert changed.get_clip_by_id("v").timeline_duration == 103
    assert changed.get_clip_by_id("a").timeline_duration == 103
    assert len(stack._undo_stack) == 1
    undone = stack.undo(changed)
    assert undone.get_clip_by_id("v").timeline_duration == 100
    assert undone.get_clip_by_id("a").timeline_duration == 100


def test_b19_within_common_bounds_commits_requested_delta(
        qtbot: Any) -> None:
    """余量充足时按请求 delta 提交（不无端钳制）。"""
    timeline = _pair_timeline(audio_source_start=800,
                              video_source_start=800)
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "right", 5, mode="ripple",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="reject")
    assert common == 5
    changed = _unwrap(cmd.apply(timeline))
    assert changed.get_clip_by_id("v").timeline_duration == 105
    assert changed.get_clip_by_id("a").timeline_duration == 105


def test_b19_left_side_common_bounds(qtbot: Any) -> None:
    """左侧 trim：源起点限制构成共同区间，同样整组一致。"""
    # 视频源起点 5（左移上限 -5），音频源起点 2（左移上限 -2）
    timeline = _pair_timeline(video_source_start=5, audio_source_start=2)
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "left", -5, mode="trim",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="reject")
    assert cmd is None  # -5 超出音频 -2
    cmd2, common2 = build_linked_numeric_trim_command(
        timeline, "v1", "v", "left", -2, mode="trim",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="reject")
    assert common2 == -2
    changed = _unwrap(cmd2.apply(timeline))
    # 左移 -2 = 左缘向外扩展：start 100→98，时长 100→102
    assert changed.get_clip_by_id("v").timeline_start == 98
    assert changed.get_clip_by_id("a").timeline_start == 98
    assert changed.get_clip_by_id("v").timeline_duration == 102
    assert changed.get_clip_by_id("a").timeline_duration == 102


def test_b19_locked_member_track_rejected(qtbot: Any) -> None:
    """锁轨成员：命令层轨锁守卫触发 → 计划拒绝（build 返回钳后命令失败）。"""
    timeline = _pair_timeline(locked_audio=True)
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "right", 3, mode="ripple",
        max_source_frames=1000, linked_max_source_frames=1000,
        mismatch="clamp")
    # 共同区间合法，但宏提交时锁轨守卫拒绝且零写入
    if cmd is not None:
        with pytest.raises(ValueError):
            cmd.apply(timeline)
        assert timeline.get_clip_by_id("v").timeline_duration == 100


def test_b19_dangling_link_plans_single_member(qtbot: Any) -> None:
    """单向/悬空链接不参与计划（主片单命令）。"""
    timeline = _pair_timeline()
    video = timeline.get_clip_by_id("v")
    timeline = timeline.with_track_updated(
        timeline.video_tracks[0]._rebuild(clips=(
            type(video).from_dict(
                {**video.to_dict(), "linked_clip_id": "ghost"}),)))
    cmd, common = build_linked_numeric_trim_command(
        timeline, "v1", "v", "right", 5, mode="ripple",
        max_source_frames=1000, mismatch="reject")
    assert cmd is not None and common == 5
    changed = _unwrap(cmd.apply(timeline))
    assert changed.get_clip_by_id("v").timeline_duration == 105
    assert changed.get_clip_by_id("a").timeline_duration == 100


def test_b19_view_step_trim_uses_common_plan(qtbot: Any) -> None:
    """键盘步进入口复用域级计划：失步请求整次拒绝并提示。"""
    from src.features.edit.core.project import EditProject, MediaAsset
    from src.features.edit.ui.timeline_view import TimelineView

    asset = MediaAsset(
        asset_id="asset", path="/tmp/asset.mp4", media_type="video",
        display_name="asset",
        metadata={"fps": 24.0, "total_frames": 1000})
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_pair_timeline())
    view.set_project(EditProject("p", "P", assets={"asset": asset}))
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    before = view.get_timeline()

    ok = view._apply_step_trim("out", 5, clip_id="v", mode="ripple")

    assert ok is False
    assert messages
    assert view.get_timeline() is before

    ok2 = view._apply_step_trim("out", 3, clip_id="v", mode="ripple")
    assert ok2 is True
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("v").timeline_duration == 103
    assert timeline.get_clip_by_id("a").timeline_duration == 103
    view.undo()
    timeline = view.get_timeline()
    assert timeline.get_clip_by_id("v").timeline_duration == 100
    assert timeline.get_clip_by_id("a").timeline_duration == 100
