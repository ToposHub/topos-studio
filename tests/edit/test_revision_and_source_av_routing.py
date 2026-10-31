"""批 B4 回归：APP-01 手动编辑推进 revision / FE-B02 设置提交 revision /
FE-B03 源编辑 A/V 路由。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §8/§11。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.commands import DeleteClipCommand, MoveClipCommand
from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id="asset-x",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=500,
    )


def _timeline() -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video)
    return timeline.with_clip_added("v1", _clip("c1", "v1", 0, 100))


def _view(timeline: TimelineSequence):
    from src.features.edit.ui.timeline_view import TimelineView
    from src.features.edit.core.commands import CommandStack

    view = MagicMock()
    view._timeline = timeline
    view._command_stack = CommandStack()
    view._batch_drop_commands = None
    view._clip_change_detector = lambda old, new: MagicMock(full_reload_needed=True)
    view._update_range_visuals = lambda tl: None
    view.load_timeline = lambda tl: None
    view.command_executed = MagicMock()
    view.timeline_changed = MagicMock()
    view._update_cache_indicator = lambda: None
    view._gc_orphan_generator_assets = lambda: None
    view._bump_revision_at_commit = lambda new: TimelineView._bump_revision_at_commit(view, new)
    view._prepare_project_transaction = lambda *args, **kwargs: None
    view._publish_timeline_history_change = lambda tl, transaction: view.timeline_changed.emit(tl)
    return view


# ---------------------------------------------------------------- APP-01

def test_manual_edit_bumps_sequence_revision() -> None:
    """审计实测：多步手动编辑后 timeline revision 恒 0。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline()
    assert timeline.revision == 0
    view = _view(timeline)

    # 手动移动（命令提交）
    TimelineView._execute_command(
        view, MoveClipCommand.create("v1", "c1", 50)
    )
    assert view._timeline.revision == 1

    TimelineView._execute_command(
        view, MoveClipCommand.create("v1", "c1", 60)
    )
    assert view._timeline.revision == 2


def test_undo_redo_revision_monotonic() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline()
    view = _view(timeline)
    TimelineView._execute_command(view, MoveClipCommand.create("v1", "c1", 50))
    TimelineView._execute_command(view, MoveClipCommand.create("v1", "c1", 60))
    assert view._timeline.revision == 2

    TimelineView.undo(view)
    assert view._timeline.get_clip_by_id("c1").timeline_start == 50
    assert view._timeline.revision == 3  # 不回旧值

    TimelineView.redo(view)
    assert view._timeline.get_clip_by_id("c1").timeline_start == 60
    assert view._timeline.revision == 4


def test_noop_edit_does_not_bump_revision() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline()
    view = _view(timeline)
    TimelineView._execute_command(view, MoveClipCommand.create("v1", "c1", 50))
    assert view._timeline.revision == 1

    # 移回原位：内容与 revision 均未变化，不增
    view2_timeline = view._timeline
    view._timeline = view2_timeline
    TimelineView._execute_command(view, MoveClipCommand.create("v1", "c1", 50))
    assert view._timeline.revision == 1


# ---------------------------------------------------------------- FE-B03

def _av_project_and_timeline():
    video_track = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio_track = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video_track).with_track_added(audio_track)

    asset = MediaAsset(
        asset_id="av1",
        path="/tmp/av.mov",
        media_type="video",
        display_name="av.mov",
        metadata={"audio_info": {"channels": 2}},
    )
    project = EditProject(project_id="p1", name="P").with_media_asset_added(asset)
    return timeline, project, asset


def _source_view(timeline, project, asset):
    from src.features.edit.ui.timeline_view import TimelineView

    view = MagicMock()
    view._timeline = timeline
    view._project = project
    view._playhead_frame = 0
    view.get_target_track_id = lambda: "v1"
    view.get_target_audio_track_id = lambda: "a1"
    executed = []
    view._execute_command.side_effect = executed.append
    # R22：overwrite/append 现经统一 fps 元数据解析（MagicMock 需显式绑定）
    view._resolve_source_fps_metadata = (
        lambda asset_id: TimelineView._resolve_source_fps_metadata(view, asset_id)
    )
    # W07：insert/overwrite 的三/四点计划步骤（_apply_source_edit_plan）
    # 需真实实现——MagicMock 兜底返回不可解包对象，插入路径 unpack 即炸。
    view._apply_source_edit_plan = (
        lambda *a, **k: TimelineView._apply_source_edit_plan(view, *a, **k)
    )
    return view, executed


def test_source_insert_with_audio_routes_av() -> None:
    """审计实测：源 Append/Insert 只有 V，音轨为空。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline, project, asset = _av_project_and_timeline()
    view, executed = _source_view(timeline, project, asset)

    ok = TimelineView.insert_clip(
        view, asset.asset_id, 0, 23, playhead=200, track_id="v1"
    )
    assert ok is True
    assert len(executed) == 1
    # 宏应用于真实时间线后 V/A 成对且互链
    result = executed[0].apply(timeline)
    new_timeline = result[0] if isinstance(result, tuple) else result
    v_clips = new_timeline.get_track_by_id("v1").clips
    a_clips = new_timeline.get_track_by_id("a1").clips
    assert len(v_clips) == 1
    assert len(a_clips) == 1  # FE-B03：音频片段存在
    new_audio = a_clips[0]
    new_video = v_clips[0]
    assert new_video.linked_clip_id == new_audio.clip_id
    assert new_audio.linked_clip_id == new_video.clip_id


def test_source_insert_audio_only_goes_to_audio_track() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline, project, _ = _av_project_and_timeline()
    audio_asset = MediaAsset(
        asset_id="au1", path="/tmp/a.wav", media_type="audio",
        display_name="a.wav", metadata={},
    )
    project = project.with_media_asset_added(audio_asset)
    view, executed = _source_view(timeline, project, audio_asset)

    ok = TimelineView.insert_clip(
        view, audio_asset.asset_id, 0, 47, playhead=0, track_id="v1"
    )
    assert ok is True
    cmd = executed[0]
    # InsertClipCommand 的目标轨应已切到音频轨
    assert cmd.track_id == "a1"


def test_source_overwrite_with_audio_adds_audio() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline, project, asset = _av_project_and_timeline()
    view, executed = _source_view(timeline, project, asset)

    ok = TimelineView.overwrite_clip(
        view, asset.asset_id, 0, 23, playhead=150, track_id="v1"
    )
    assert ok is True
    result = executed[0].apply(timeline)
    new_timeline = result[0] if isinstance(result, tuple) else result
    assert len(new_timeline.get_track_by_id("a1").clips) == 1
