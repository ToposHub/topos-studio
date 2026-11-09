"""批 A3 回归：C09/INT-05 统一轨锁守卫 + INT-08 菜单状态 + FE-B05 源范围信号。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §3/§9(C09)/§10/§11(FE-B05)。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    ChangeClipSpeedCommand,
    SetClipLinkedCommand,
    SplitClipCommand,
    ToggleClipEnabledCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int = 0, duration: int = 100,
          linked: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration,
        linked_clip_id=linked,
    )


def _timeline(video_locked: bool = False, audio_locked: bool = False,
              linked: bool = True) -> TimelineSequence:
    video = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        is_locked=video_locked,
    )
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        is_locked=audio_locked,
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    v = _clip("vc", "v1")
    a = _clip("ac", "a1")
    if linked:
        v = _clip("vc", "v1", linked="ac")
        a = _clip("ac", "a1", linked="vc")
    timeline = timeline.with_clip_added("v1", v)
    return timeline.with_clip_added("a1", a)


# ------------------------------------------------------------- C09/INT-05

def test_toggle_enabled_rejected_on_locked_track() -> None:
    timeline = _timeline(video_locked=True)
    cmd = ToggleClipEnabledCommand.create(
        "v1", "vc", new_enabled=False, old_enabled=True
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_speed_rejected_when_linked_partner_track_locked() -> None:
    """主轨未锁、链接音轨锁定：变速不得暗改锁伙伴（审计实测 100→50）。"""
    timeline = _timeline(audio_locked=True)
    cmd = ChangeClipSpeedCommand.create("vc", "v1", 2.0)
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_speed_allowed_when_unlocked() -> None:
    timeline = _timeline()
    cmd = ChangeClipSpeedCommand.create("vc", "v1", 2.0)
    result = cmd.apply(timeline)
    new_timeline = result[0] if isinstance(result, tuple) else result
    v = new_timeline.get_clip_by_id("vc")
    a = new_timeline.get_clip_by_id("ac")
    assert v.timeline_duration == 50
    assert a.timeline_duration == 50  # 链接伙伴同速


def test_split_rejected_on_locked_track() -> None:
    timeline = _timeline(video_locked=True)
    cmd = SplitClipCommand.create("v1", "vc", 50)
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_unlink_rejected_when_partner_track_locked() -> None:
    timeline = _timeline(audio_locked=True)
    cmd = SetClipLinkedCommand.create_unlink(
        clip_a_id="vc", clip_a_track_id="v1",
        clip_b_id="ac", clip_b_track_id="a1",
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_undo_not_blocked_by_current_lock() -> None:
    """撤销必须能恢复合法历史——锁只拦 apply，不拦 undo。"""
    timeline = _timeline()
    cmd = ChangeClipSpeedCommand.create("vc", "v1", 2.0)
    result = cmd.apply(timeline)
    new_timeline, updated = result
    # 用户随后锁轨再 undo：仍可恢复
    v1 = new_timeline.get_track_by_id("v1")
    locked = new_timeline.with_track_updated(
        v1._rebuild(is_locked=True)
    )
    restored = updated.undo(locked)
    assert restored.get_clip_by_id("vc").timeline_duration == 100


# ------------------------------------------------------------- FE-B05

def test_source_range_changed_emitted_on_all_paths(qtbot) -> None:
    from src.features.edit.core.project import MediaAsset
    from src.features.edit.ui.source_viewer import SourceViewerWidget

    widget = SourceViewerWidget()
    qtbot.addWidget(widget)
    widget._sync_scrub_audio_for_asset = lambda: None  # type: ignore[method-assign]
    # I02：mark 点经时间域校验——真实打开素材（合法帧 0..119）后再标记
    widget.open_asset(MediaAsset(
        asset_id="feb05", path="/tmp/feb05.mov", media_type="video",
        display_name="feb05.mov",
        metadata={"fps": 24.0, "total_frames": 120, "width": 640,
                  "height": 480}))
    events: list[tuple] = []
    widget.range_changed.connect(lambda i, o: events.append((i, o)))

    widget.set_in_point(10)
    widget.set_out_point(20)
    assert events[-1] == (10, 20)

    # 单帧范围 I=O 合法
    widget.set_out_point(10)
    assert widget.toolbar._can_edit is True

    widget.clear_in_out()
    assert events[-1] == (None, None)
    assert widget.toolbar._can_edit is False
    assert widget.toolbar.btn_clear.isEnabled() is False


def test_source_edit_params_accept_single_frame_range(qtbot) -> None:
    """I=O 单帧范围可插入 1 帧（Out 为 inclusive）。"""
    from src.features.edit.ui.edit_page import EditPage

    page = EditPage.__new__(EditPage)
    viewer = type("V", (), {})()
    viewer.current_asset = type("A", (), {"asset_id": "x"})()
    viewer.in_point = 5
    viewer.out_point = 5
    page._source_viewer = viewer
    asset_id, i, o = EditPage._get_source_edit_params(page)
    assert (asset_id, i, o) == ("x", 5, 5)

    viewer.in_point = 6
    asset_id, i, o = EditPage._get_source_edit_params(page)
    assert asset_id is None
