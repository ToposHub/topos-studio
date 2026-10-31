"""复查批次 C 回归：R23 切角时长/fps 换装 / R24 Nest Bus 闭包 /
R25 Project 栈 revision 单调。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.3
（F-R04/F-R05/F-R06）。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    ProjectCommandStack,
    SwitchMulticamAngleCommand,
    UpdateClipSourceCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int, **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=kw.pop("asset_id", f"asset-{clip_id}"),
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 400),
        **kw,
    )


# ------------------------------------------------------------------ R23

def test_switch_mixed_fps_preserves_window_and_swaps_fps() -> None:
    """F-R04：24fps A 切 48fps B——源窗口按 fps 比例保持时长，fps 元数据
    换装（旧公式 duration 72 应 144）。"""
    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    t = t.with_clip_added("v1", ClipInstance(
        clip_id="c", asset_id="assetA", track_id="v1",
        timeline_start=24, timeline_duration=72,
        source_start=24, source_duration=200,
        _source_fps_num=24, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    ))
    cmd = SwitchMulticamAngleCommand.create(
        track_id="v1", clip_id="c", switch_frame=24,
        angle_asset_id="assetB", angle_sync_offset=0,
        angle_fps_num=48, angle_fps_den=1,
    )
    new_timeline, _ = cmd.apply(t)
    switched = new_timeline.get_clip_by_id("c")
    assert switched.asset_id == "assetB"
    # 时间线窗口不变
    assert switched.timeline_duration == 72
    # 源窗口 = 72 × (48/24) = 144（旧公式 72）
    assert switched.source_duration == 144
    assert switched._source_fps_num == 48


# ------------------------------------------------------------------ R24

def test_nest_child_contains_bus_closure() -> None:
    """F-R05：a1 输出到 bus 并 send bus——child 内含 Bus 本地副本，
    路由不悬空。"""
    from src.features.edit.core.nested_sequence import build_nested_from_selection

    t = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    a1 = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        output_bus_id="bus-main",
        sends=(Send(send_id="s1", target_bus_id="bus-fx", level=0.5),),
    )
    bus_main = EditTrack(
        track_id="bus-main", name="BusMain", track_type="audio", index=1,
        is_bus=True,
    )
    bus_fx = EditTrack(
        track_id="bus-fx", name="BusFX", track_type="audio", index=2,
        is_bus=True, output_bus_id="bus-main",
    )
    t = t.with_track_added(video).with_track_added(a1)
    t = t.with_track_added(bus_main).with_track_added(bus_fx)
    t = t.with_clip_added("v1", _clip("vc", "v1", 0, 100))
    t = t.with_clip_added("a1", _clip("ac", "a1", 0, 100))

    result = build_nested_from_selection(
        timeline=t,
        clip_ids=("vc", "ac"),
        child_timeline_id="child-1",
    )
    child = result.child_timeline
    audio_tracks = child.audio_tracks
    track_ids = [tr.track_id for tr in audio_tracks]
    # a1 的子副本 + 两个 Bus 副本
    assert len(audio_tracks) == 3
    child_a1 = next(tr for tr in audio_tracks if not tr.is_bus)
    child_buses = [tr for tr in audio_tracks if tr.is_bus]
    # 子 a1 的路由指向子本地 Bus
    assert child_a1.output_bus_id in {tr.track_id for tr in child_buses}
    assert child_a1.sends[0].target_bus_id in {tr.track_id for tr in child_buses}
    # bus→bus 路由同样子本地
    fx = next(
        tr for tr in child_buses
        if tr.output_bus_id and tr.output_bus_id in track_ids
    )
    assert fx.output_bus_id != "bus-main"  # 不再指父轨 ID


# ------------------------------------------------------------------ R25

def test_project_stack_revision_monotonic() -> None:
    """F-R06：execute/undo/redo 均推进 revision（旧序列 [7,7,7,7,7]）。"""
    from src.features.edit.core.project import EditProject

    project = EditProject.create("p1")

    class _Touch:
        def __init__(self):
            self.command_id = "cmd"
            self.label = "touch"

        def apply(self, project):
            from dataclasses import replace as _r
            return _r(project, name="touched")

        def undo(self, project):
            from dataclasses import replace as _r
            return _r(project, name="original")

    stack = ProjectCommandStack()
    r0 = project.revision
    p1 = stack.execute(_Touch(), project)
    assert p1.revision == r0 + 1
    p2 = stack.undo(p1)
    assert p2.revision == r0 + 2  # 内容回滚，发布 revision 不回退
    p3 = stack.redo(p2)
    assert p3.revision == r0 + 3
