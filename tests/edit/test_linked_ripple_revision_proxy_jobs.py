"""批 B3 回归：C08 链接 A/V 波纹 / FE-B10 revision 保留 / FE-B11 代理任务 token。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §9(C08)/§11。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.commands import (
    MacroCommand,
    RippleTrimCommand,
)
from src.features.edit.core.project import (
    EditProject,
    MediaAsset,
    relink_asset,
    replace_media,
    update_asset_proxy,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          linked: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=500,  # 源余量充足
        linked_clip_id=linked,
    )


def _linked_timeline() -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio = EditTrack(track_id="a1", name="A1", track_type="audio", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    timeline = timeline.with_clip_added("v1", _clip("vc", "v1", 0, 100, linked="ac"))
    return timeline.with_clip_added("a1", _clip("ac", "a1", 0, 100, linked="vc"))


def _view_stub(timeline: TimelineSequence):
    view = MagicMock()
    view._timeline = timeline
    view._updating = False
    view._snap_enabled = False
    view._linked_selection_enabled = True
    view._items_map = {}
    view._is_track_locked = lambda track_id: any(
        t.is_locked and t.track_id == track_id for t in timeline.all_tracks
    )
    executed: list[object] = []
    view._execute_command.side_effect = executed.append
    return view, executed


# ---------------------------------------------------------------- C08

def test_ripple_right_trims_linked_av_atomically() -> None:
    """审计实测：V/A 各 100 帧链接，视频缩到 80 音频仍 100。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    view, executed = _view_stub(timeline)

    TimelineView._on_ripple_trim_right_finished(view, "vc", "v1", 100, 80)

    assert len(executed) == 1
    cmd = executed[0]
    assert isinstance(cmd, MacroCommand)
    new_timeline = cmd.apply(timeline)
    if isinstance(new_timeline, tuple):
        new_timeline = new_timeline[0]
    v = new_timeline.get_clip_by_id("vc")
    a = new_timeline.get_clip_by_id("ac")
    assert v.timeline_duration == 80
    assert a.timeline_duration == 80  # 伙伴同步


def test_ripple_left_trims_linked_av_atomically() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    view, executed = _view_stub(timeline)

    TimelineView._on_ripple_trim_left_finished(view, "vc", "v1", 0, 20)

    assert len(executed) == 1
    cmd = executed[0]
    new_timeline = cmd.apply(timeline)
    if isinstance(new_timeline, tuple):
        new_timeline = new_timeline[0]
    # 波纹左修：头部 20 帧消失，片段拉回起点 0，源入点 +20
    v = new_timeline.get_clip_by_id("vc")
    a = new_timeline.get_clip_by_id("ac")
    assert (v.timeline_start, v.timeline_duration, v.source_start) == (0, 80, 20)
    assert (a.timeline_start, a.timeline_duration, a.source_start) == (0, 80, 20)


def test_ripple_linked_rejected_when_partner_locked() -> None:
    """锁住链接音轨：整组拒绝，无命令、无半提交。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    a1 = timeline.get_track_by_id("a1")
    timeline = timeline.with_track_updated(a1._rebuild(is_locked=True))
    view, executed = _view_stub(timeline)

    TimelineView._on_ripple_trim_right_finished(view, "vc", "v1", 100, 80)
    assert executed == []
    assert view.status_message_requested.emit.called


def test_ripple_independent_when_linked_selection_off() -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _linked_timeline()
    view, executed = _view_stub(timeline)
    view._linked_selection_enabled = False

    TimelineView._on_ripple_trim_right_finished(view, "vc", "v1", 100, 80)
    assert len(executed) == 1
    assert isinstance(executed[0], RippleTrimCommand)
    new_timeline = executed[0].apply(timeline)
    if isinstance(new_timeline, tuple):
        new_timeline = new_timeline[0]
    assert new_timeline.get_clip_by_id("vc").timeline_duration == 80
    assert new_timeline.get_clip_by_id("ac").timeline_duration == 100


# ---------------------------------------------------------------- FE-B10

def _project_with_revision(revision: int = 7) -> EditProject:
    asset = MediaAsset(
        asset_id="a1",
        path="/tmp/old.mov",
        media_type="video",
        display_name="old.mov",
        metadata={},
    )
    project = EditProject(project_id="p1", name="P")
    object.__setattr__(project, "revision", revision)
    project = project.with_media_asset_added(asset)
    # with_media_asset_added 保留 revision
    assert project.revision == revision
    return project


def test_relink_preserves_revision() -> None:
    project = _project_with_revision(7)
    new_project = relink_asset(project, "a1", "/tmp/new.mov")
    assert new_project.revision == 7


def test_update_asset_proxy_preserves_revision() -> None:
    project = _project_with_revision(7)
    new_project = update_asset_proxy(project, "a1", "/tmp/proxy.mov", "ready")
    assert new_project.revision == 7


def test_replace_media_preserves_revision(tmp_path) -> None:
    project = _project_with_revision(7)
    target = tmp_path / "new_source.mov"
    target.write_bytes(b"x")
    new_project = replace_media(project, "a1", str(target))
    assert new_project.revision == 7


# ---------------------------------------------------------------- FE-B11

def _panel_stub(project: EditProject):
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    panel = MediaPoolPanel.__new__(MediaPoolPanel)
    panel._project = project
    panel._proxy_jobs = {}
    panel._proxy_job_tokens = {}
    panel._proxy_token_counter = 0
    # R26 会话代次
    panel._proxy_job_generations = {}
    panel._proxy_session_generation = 0
    return panel


def test_stale_proxy_completion_rejected_after_invalidation(tmp_path) -> None:
    """换源后旧代理任务完成回调不得把旧 proxy 标到新素材。"""
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    project = _project_with_revision(3)
    proxy_file = tmp_path / "proxy.mov"
    proxy_file.write_bytes(b"p")

    panel = _panel_stub(project)
    # 注册任务 token=1
    panel._proxy_jobs["a1"] = "/tmp/old.mov"
    panel._proxy_token_counter = 1
    panel._proxy_job_tokens["a1"] = 1

    # 换源 → 任务注销（token 失效）
    panel._invalidate_proxy_job("a1")

    import os
    assert os.path.exists(str(proxy_file))
    # 旧任务完成回调（token=1）到达：必须被拒绝，不改工程
    MediaPoolPanel._on_proxy_finished(panel, "a1", str(proxy_file), token=1)
    assert panel._project.assets["a1"].proxy_status != "ready"


def test_stale_proxy_error_rejected_after_invalidation() -> None:
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    project = _project_with_revision(3)
    panel = _panel_stub(project)
    panel._proxy_jobs["a1"] = "/tmp/old.mov"
    panel._proxy_token_counter = 1
    panel._proxy_job_tokens["a1"] = 1
    panel._invalidate_proxy_job("a1")

    MediaPoolPanel._on_proxy_error(panel, "a1", "boom", token=1)
    assert panel._project.assets["a1"].proxy_status != "failed"


def test_valid_proxy_completion_accepted(tmp_path) -> None:
    from src.features.edit.ui.media_pool_panel import MediaPoolPanel

    project = _project_with_revision(3)
    proxy_file = tmp_path / "proxy_ok.mov"
    proxy_file.write_bytes(b"p")

    panel = _panel_stub(project)
    panel._proxy_jobs = {"a1": "/tmp/old.mov"}
    panel._proxy_job_tokens = {"a1": 5}
    panel._proxy_job_generations = {"a1": 0}  # R26：注册携带代次

    # 合法在册任务回调被接受
    calls = []
    panel.project_changed = MagicMock()
    panel.project_changed.emit.side_effect = lambda p: calls.append(p)
    panel._refresh_tree = lambda: None
    panel._update_disk_proxy_services = lambda *a, **k: None

    MediaPoolPanel._on_proxy_finished(panel, "a1", str(proxy_file), token=5)
    assert panel._project.assets["a1"].proxy_status == "ready"
