"""批 B1 回归：C01 Roll 撤销 / C02 Insert 撤销 / C03 批量波纹累计位移。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §9。
验收以完整 to_dict() 比较为准，不做 ID equality。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import (
    BatchDeleteCommand,
    InsertClipCommand,
    RollTrimCommand,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          source_start: int = 0) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=source_start,
        source_duration=duration,
    )


def _timeline() -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0, is_sync_locked=True
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    # b 留源余量：向左 Roll 会吃 b 的源入点（source_start 须 > |delta|）
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100, source_start=50))
    return timeline


# ---------------------------------------------------------------- C01

@pytest.mark.parametrize("delta", [-10, -1, 10, 1])
def test_roll_undo_restores_full_state(delta: int) -> None:
    timeline = _timeline()
    before = timeline.to_dict()

    cmd = RollTrimCommand.create("v1", "a", "b", delta)
    result = cmd.apply(timeline)
    new_timeline, updated = result

    # 应用确实改变了两片
    if delta != 0:
        assert new_timeline.to_dict() != before

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before


def test_roll_undo_three_cycles() -> None:
    timeline = _timeline()
    before = timeline.to_dict()

    cmd = RollTrimCommand.create("v1", "a", "b", -10)
    new_timeline, updated = cmd.apply(timeline)
    for _ in range(3):
        new_timeline = updated.undo(new_timeline)
        assert new_timeline.to_dict() == before
        new_timeline, updated = cmd.apply(new_timeline)
    assert new_timeline.to_dict() != before


def test_roll_left_negative_undo_no_exception() -> None:
    """审计实测：Roll -10 后 undo 抛 "Updated clip overlaps"，历史被堵死。"""
    timeline = _timeline()
    cmd = RollTrimCommand.create("v1", "a", "b", -10)
    new_timeline, updated = cmd.apply(timeline)
    restored = updated.undo(new_timeline)  # 不抛 ValueError 即通过
    a = restored.get_clip_by_id("a")
    b = restored.get_clip_by_id("b")
    assert (a.timeline_start, a.timeline_duration) == (0, 100)
    assert (b.timeline_start, b.timeline_duration) == (100, 100)


# ---------------------------------------------------------------- C02

def test_insert_middle_undo_restores_single_clip() -> None:
    """审计实测：原 A=[0,100)，40 处插入 10 帧，undo 留下 40–50 的洞。"""
    timeline = _timeline()
    # 只留一个片段，插入目标在它的中间
    timeline = _timeline().with_track_updated(
        _timeline().get_track_by_id("v1")
    )
    before = timeline.to_dict()

    cmd = InsertClipCommand.create(
        asset_id="new-asset", in_frame=0, out_frame=9,
        playhead=40, track_id="v1",
    )
    new_timeline, updated = cmd.apply(timeline)

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before
    # 具体验收：单一 100 帧片段，无洞
    clips = restored.get_track_by_id("v1").clips
    assert len(clips) == 2  # a + b（保留原时间线结构）
    assert restored.get_clip_by_id("a").timeline_duration == 100


def test_insert_playhead_middle_undo_single_clip_no_hole() -> None:
    """精确复现审计：100 帧片在 40 插 10，undo 恢复单一 100 帧且无洞。"""
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video)
    timeline = timeline.with_clip_added("v1", _clip("solo", "v1", 0, 100))
    before = timeline.to_dict()

    cmd = InsertClipCommand.create(
        asset_id="new-asset", in_frame=0, out_frame=9,
        playhead=40, track_id="v1",
    )
    new_timeline, updated = cmd.apply(timeline)

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before
    clips = restored.get_track_by_id("v1").clips
    assert len(clips) == 1
    assert clips[0].timeline_start == 0 and clips[0].timeline_duration == 100


def test_insert_undo_redo_stable_ids() -> None:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video)
    timeline = timeline.with_clip_added("v1", _clip("solo", "v1", 0, 100))

    cmd = InsertClipCommand.create(
        asset_id="new-asset", in_frame=0, out_frame=9,
        playhead=40, track_id="v1",
    )
    applied, updated = cmd.apply(timeline)
    inserted_id = updated._inserted_clip_id
    assert inserted_id is not None

    undone = updated.undo(applied)
    redone_timeline, redone_cmd = updated.apply(undone)
    # redo 复用同一插入片段 ID，不产生残片
    assert redone_cmd._inserted_clip_id == inserted_id
    ids = {c.clip_id for c in redone_timeline.get_track_by_id("v1").clips}
    assert inserted_id in ids
    assert len(redone_timeline.get_track_by_id("v1").clips) == len(
        applied.get_track_by_id("v1").clips
    )


# ---------------------------------------------------------------- C03

def _timeline_with_sync_audio(audio_starts: list[int]) -> TimelineSequence:
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0, is_sync_locked=True
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    timeline = timeline.with_clip_added("v1", _clip("d1", "v1", 0, 10))
    timeline = timeline.with_clip_added("v1", _clip("d2", "v1", 20, 10))
    for i, start in enumerate(audio_starts):
        timeline = timeline.with_clip_added("a1", _clip(f"af{i}", "a1", start, 50))
    return timeline


def test_batch_ripple_delete_cumulative_cross_track_shift() -> None:
    """审计触发1：删 V[0,10)+V[20,30)，同步锁音频 30 应累计到 10。"""
    timeline = _timeline_with_sync_audio([30])
    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1", "d2"), ripple=True
    )
    new_timeline, updated = cmd.apply(timeline)
    audio_clip = new_timeline.get_clip_by_id("af0")
    assert audio_clip.timeline_start == 10  # 30 - (10 + 10)

    restored = updated.undo(new_timeline)
    assert restored.get_clip_by_id("af0").timeline_start == 30
    assert restored.to_dict() == timeline.to_dict()


def test_batch_ripple_delete_follower_after_all_spans() -> None:
    """审计触发2：音频 50 apply 后 30；旧 undo 恢复 40（最后 old_start 覆盖）。"""
    timeline = _timeline_with_sync_audio([50])
    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1", "d2"), ripple=True
    )
    new_timeline, updated = cmd.apply(timeline)
    assert new_timeline.get_clip_by_id("af0").timeline_start == 30

    restored = updated.undo(new_timeline)
    assert restored.get_clip_by_id("af0").timeline_start == 50
    assert restored.to_dict() == timeline.to_dict()


def test_batch_ripple_delete_between_spans_and_after() -> None:
    """30（第一跨度后）与 50（全部跨度后）混合、含三个跨度。"""
    video = EditTrack(track_id="v1", name="V1", track_type="video", index=0)
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0, is_sync_locked=True
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    timeline = timeline.with_clip_added("v1", _clip("d1", "v1", 0, 10))
    timeline = timeline.with_clip_added("v1", _clip("d2", "v1", 20, 10))
    timeline = timeline.with_clip_added("v1", _clip("d3", "v1", 40, 10))
    timeline = timeline.with_clip_added("a1", _clip("af0", "a1", 30, 20))
    timeline = timeline.with_clip_added("a1", _clip("af1", "a1", 60, 20))
    before = timeline.to_dict()

    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1", "d2", "d3"), ripple=True
    )
    new_timeline, updated = cmd.apply(timeline)
    # af0 覆盖跨度1（end 10 ≤ 30）与跨度2（end 30 ≤ 30）→ 30-20=10；
    # af1 覆盖三个 → 60-30=30
    assert new_timeline.get_clip_by_id("af0").timeline_start == 10
    assert new_timeline.get_clip_by_id("af1").timeline_start == 30

    restored = updated.undo(new_timeline)
    assert restored.to_dict() == before
