"""复查批次 A 回归：R22/F-R03 源 A/V 插入/覆盖/追加统一规划。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.3
（F-R03），复现见 docs/audit/2026-10-14-edit-recheck/reports/
topos_recheck_features.md：内部插入回退 video-only、覆盖音床抛异常、
Append 各轨独立尾错位、混合 FPS 时长错误。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.commands import (
    InsertLinkedAssetRippleCommand,
    OverwriteClipCommand,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
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


def _av_timeline(v_clips=(), a_clips=(), a_locked: bool = False) -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(
        EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    )
    timeline = timeline.with_track_added(
        EditTrack(
            track_id="a1", name="A1", track_type="audio", index=0,
            is_locked=a_locked,
        )
    )
    for c in v_clips:
        timeline = timeline.with_clip_added("v1", c)
    for c in a_clips:
        timeline = timeline.with_clip_added("a1", c)
    return timeline


# ------------------------------------------------- R22-2：内部插入 A/V

def test_av_ripple_insert_inside_existing_clips() -> None:
    """F-R03-2：已有 V/A [0,96)，在 24 插入 48 帧——V/A 同步切分+波纹，
    新音频必须存在（此前回退 video-only，新素材无音频）。"""
    timeline = _av_timeline(
        v_clips=[_clip("vc", "v1", 0, 96, asset_id="src")],
        a_clips=[_clip("ac", "a1", 0, 96, asset_id="src", linked_clip_id="vc")],
    )
    timeline = timeline.with_track_updated(
        timeline.get_track_by_id("v1").with_clip_updated(
            timeline.get_track_by_id("v1").get_clip_by_id("vc").with_linked_clip_id("ac")
        )
    )

    cmd = InsertLinkedAssetRippleCommand.create(
        asset_id="new",
        in_frame=0,
        out_frame=47,  # 48 帧素材
        playhead=24,
        video_track_id="v1",
        audio_track_id="a1",
    )
    new_timeline, updated = cmd.apply(timeline)

    v_track = new_timeline.get_track_by_id("v1")
    a_track = new_timeline.get_track_by_id("a1")
    # 两轨同构：3 段（左半 [0,24)、新片 [24,72)、右半 [72,144)）
    assert [(c.timeline_start, c.timeline_end) for c in v_track.clips] == [
        (0, 24), (24, 72), (72, 144),
    ]
    assert [(c.timeline_start, c.timeline_end) for c in a_track.clips] == [
        (0, 24), (24, 72), (72, 144),
    ]
    # 新 A/V 互为双向链接
    new_v = next(c for c in v_track.clips if c.asset_id == "new")
    new_a = next(c for c in a_track.clips if c.asset_id == "new")
    assert new_v.linked_clip_id == new_a.clip_id
    assert new_a.linked_clip_id == new_v.clip_id

    # 单步 Undo 完整恢复
    restored = updated.undo(new_timeline)
    assert restored.to_dict() == timeline.to_dict()


def test_av_ripple_insert_locked_audio_rejects_whole_group() -> None:
    """R01/R22 矩阵：锁 A 轨时整组拒绝，不允许只落 V 留失步对白。"""
    timeline = _av_timeline(
        v_clips=[_clip("vc", "v1", 0, 96)],
        a_clips=[_clip("ac", "a1", 100, 96)],
        a_locked=True,
    )
    cmd = InsertLinkedAssetRippleCommand.create(
        asset_id="new", in_frame=0, out_frame=47, playhead=0,
        video_track_id="v1", audio_track_id="a1",
    )
    with pytest.raises(ValueError, match="locked"):
        cmd.apply(timeline)


def test_av_ripple_insert_sync_followers_shift_once() -> None:
    """其余同步锁轨只位移一次（两条独立 Insert 会双重位移）。"""
    timeline = _av_timeline(v_clips=[], a_clips=[])
    timeline = timeline.with_track_added(
        EditTrack(
            track_id="v2", name="V2", track_type="video", index=1,
            is_sync_locked=True,
        )
    )
    timeline = timeline.with_clip_added("v2", _clip("follow", "v2", 50, 10))

    cmd = InsertLinkedAssetRippleCommand.create(
        asset_id="new", in_frame=0, out_frame=9, playhead=0,
        video_track_id="v1", audio_track_id="a1",
    )
    new_timeline, _ = cmd.apply(timeline)
    follower = new_timeline.get_clip_by_id("follow")
    # 插入 10 帧：50 → 60（一次），不是 70（两次）
    assert follower.timeline_start == 60


# ------------------------------------------------- R22-1：混合 FPS 时长

def test_overwrite_mixed_fps_duration_uses_unified_helper() -> None:
    """48fps 源 48 帧覆盖 24fps 序列：占 24 帧（此前 int(48/1)=48）。"""
    timeline = _av_timeline(v_clips=[_clip("base", "v1", 0, 200)])
    cmd = OverwriteClipCommand.create(
        asset_id="new", in_frame=0, out_frame=47, playhead=10,
        track_id="v1", speed=1.0,
        source_fps_num=48, source_fps_den=1,
        timeline_fps_num=24, timeline_fps_den=1,
    )
    new_timeline, _ = cmd.apply(timeline)
    inserted = next(
        c for c in new_timeline.get_track_by_id("v1").clips if c.asset_id == "new"
    )
    assert inserted.timeline_duration == 24
    assert inserted.timeline_start == 10


# ------------------------------------------------- 视图三入口（真实 view）

def _content(timeline: TimelineSequence) -> dict:
    """内容载荷（不含 revision——Undo 发布单调递增是 APP-01 契约）。"""
    data = timeline.to_dict()
    data.pop("revision", None)
    return data


class _AssetStub:
    def __init__(self, media_type: str, has_audio: bool, fps: float = 48.0):
        self.media_type = media_type
        self.audio_channels = 2 if has_audio else None
        self.fps = fps
        self.display_name = f"{media_type}-asset"
        self.name = f"{media_type}-asset"
        self.path = "/tmp/recheck-av.mp4"
        self.effective_path = "/tmp/recheck-av.mp4"
        self.duration_frames = 400
        self.total_frames = 400
        self.duration_seconds = None
        self.metadata = {}


class _ProjectStub:
    def __init__(self, assets: dict):
        self.assets = assets


def _view_with_av(qtbot: Any, timeline: TimelineSequence, assets: dict):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    view._project = _ProjectStub(assets)
    return view


def test_view_insert_inside_clip_places_av_pair(qtbot: Any) -> None:
    """F-R03-2 视图入口：播放头在片段内部时源插入不再 video-only。"""
    timeline = _av_timeline(
        v_clips=[_clip("vc", "v1", 0, 96, asset_id="src")],
        a_clips=[_clip("ac", "a1", 0, 96, asset_id="src")],
    )
    view = _view_with_av(
        qtbot, timeline,
        {"src-av": _AssetStub("video", True)},
    )
    view._playhead_frame = 24

    ok = view.insert_clip("src-av", 0, 47, playhead=24, track_id="v1")
    assert ok

    a_track = view._timeline.get_track_by_id("a1")
    assert any(c.asset_id == "src-av" for c in a_track.clips)
    new_a = next(c for c in a_track.clips if c.asset_id == "src-av")
    new_v = view._timeline.get_clip_by_id(new_a.linked_clip_id)
    assert new_v is not None
    assert new_v.timeline_start == new_a.timeline_start == 24

    # 单步 Undo
    view.undo()
    assert _content(view._timeline) == _content(timeline)


def test_view_overwrite_onto_occupied_audio_bed(qtbot: Any) -> None:
    """F-R03-3：音床 [0,96) 上 overwrite at 24 不再抛异常。"""
    timeline = _av_timeline(
        v_clips=[_clip("vc", "v1", 0, 96, asset_id="src")],
        a_clips=[_clip("bed", "a1", 0, 96, asset_id="music")],
    )
    view = _view_with_av(
        qtbot, timeline,
        {"src-av": _AssetStub("video", True)},
    )

    ok = view.overwrite_clip("src-av", 0, 47, playhead=24, track_id="v1")
    assert ok

    a_track = view._timeline.get_track_by_id("a1")
    # 音床被切分/覆盖而不是整体失败
    starts = sorted(c.timeline_start for c in a_track.clips)
    assert 24 in starts
    new_a = next(c for c in a_track.clips if c.asset_id == "src-av")
    assert new_a.timeline_duration == 24  # 48src/48fps → 24tl@24fps

    view.undo()
    assert _content(view._timeline) == _content(timeline)


def test_view_append_uses_common_landing(qtbot: Any) -> None:
    """F-R03-4：V 尾 96、A 尾 48——新 V/A 共同落在 96，不双向错位。"""
    timeline = _av_timeline(
        v_clips=[_clip("vc", "v1", 0, 96, asset_id="src")],
        a_clips=[_clip("ac", "a1", 0, 48, asset_id="src")],
    )
    view = _view_with_av(
        qtbot, timeline,
        {"src-av": _AssetStub("video", True)},
    )

    ok = view.append_clip("src-av", 0, 47, track_id="v1")
    assert ok

    v_track = view._timeline.get_track_by_id("v1")
    a_track = view._timeline.get_track_by_id("a1")
    new_v = next(c for c in v_track.clips if c.asset_id == "src-av")
    new_a = next(c for c in a_track.clips if c.asset_id == "src-av")
    assert new_v.timeline_start == new_a.timeline_start == 96
    assert new_v.linked_clip_id == new_a.clip_id
    assert new_a.linked_clip_id == new_v.clip_id

    view.undo()
    assert _content(view._timeline) == _content(timeline)


def test_view_insert_mixed_fps_duration(qtbot: Any) -> None:
    """F-R03-1：48fps 源 48 帧插 24fps 序列，时长 24 而非 48。"""
    timeline = _av_timeline()
    view = _view_with_av(
        qtbot, timeline, {"src-av": _AssetStub("video", True, fps=48.0)},
    )

    ok = view.insert_clip("src-av", 0, 47, playhead=0, track_id="v1")
    assert ok
    new_v = next(
        c for c in view._timeline.get_track_by_id("v1").clips
        if c.asset_id == "src-av"
    )
    assert new_v.timeline_duration == 24
