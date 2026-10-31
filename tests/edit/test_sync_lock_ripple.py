"""P1-05 跨轨 Sync Lock 跟随回归测试。

专业语义（Premiere 等价）：一条轨道上的波纹编辑（波纹删除 / 波纹修剪 /
插入）发生时，其余开启同步锁、未锁定、非字幕轨道上位于波纹点之后的片段
跟随相同位移，下游内容跨轨保持对齐；关闭同步锁的轨道保持原位（有意失步）。

关键回归：
1. 波纹删除/波纹 Trim/插入后其他同步锁轨道跟随，undo 全部还原。
2. 关闭同步锁、锁定的轨道、字幕轨不跟随。
3. 链接 A/V 同时波纹删除（BatchDelete）只跟随一次，不双重位移。
4. 负向位移按跟随轨可用间隙钳制（顶住前片，不抛重叠异常）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)
from src.features.edit.core.commands import (
    BatchDeleteCommand,
    InsertClipCommand,
    RippleDeleteCommand,
    RippleTrimCommand,
)
from src.features.edit.core.timeline_ops import (
    merged_ripple_spans,
    restore_sync_lock_shifts,
    shift_sync_lock_followers,
)
from src.shared.media.metadata import Rational


def _clip(clip_id: str, track_id: str, start: int, duration: int = 100,
          linked_id: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id, asset_id=f"a_{clip_id}", track_id=track_id,
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration + 500,
        linked_clip_id=linked_id,
    )


def _timeline(v_clips=(), a_clips=(), v_sync=True, a_sync=True,
              v_locked=False) -> TimelineSequence:
    v1 = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0,
        clips=tuple(v_clips), is_sync_locked=v_sync, is_locked=v_locked,
    )
    a1 = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        clips=tuple(a_clips), is_sync_locked=a_sync,
    )
    return TimelineSequence(
        timeline_id="tl1", name="Sync", fps=Rational(24, 1),
        video_tracks=(v1,), audio_tracks=(a1,),
    )


def _start(tl: TimelineSequence, clip_id: str) -> int:
    clip = tl.get_clip_by_id(clip_id)
    assert clip is not None, f"clip {clip_id} missing"
    return clip.timeline_start


# ─────────────────────────────────────────────────────────────────────────────
# 原语：shift_sync_lock_followers / restore / merged_ripple_spans
# ─────────────────────────────────────────────────────────────────────────────

class TestShiftSyncLockFollowers:

    def test_shifts_sync_locked_other_track(self):
        tl = _timeline(v_clips=[_clip("v_c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        new_tl, moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=100, delta=-50)
        assert _start(new_tl, "a_x") == 50
        assert len(moves) == 1
        assert moves[0]["old_start"] == 100 and moves[0]["new_start"] == 50
        # restore
        restored = restore_sync_lock_shifts(new_tl, moves)
        assert _start(restored, "a_x") == 100

    def test_skips_unlocked_sync_track(self):
        tl = _timeline(v_clips=[_clip("v_c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)],
                       a_sync=False)
        new_tl, moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=100, delta=-50)
        assert _start(new_tl, "a_x") == 100
        assert moves == ()

    def test_skips_locked_and_excluded_tracks(self):
        tl = _timeline(v_clips=[_clip("v_c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)],
                       v_locked=True)
        new_tl, moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["a1"], from_frame=100, delta=-50)
        assert moves == ()  # v1 locked + a1 excluded
        assert _start(new_tl, "v_c") == 0

    def test_negative_shift_clamps_to_gap(self):
        # a1 上 a_x 之前有片段到 80，可用间隙只有 20，请求左移 50 → 只移 20
        tl = _timeline(v_clips=[_clip("v_c", "v1", 0, 100)],
                       a_clips=[_clip("a_prev", "a1", 0, 80),
                                _clip("a_x", "a1", 100, 50)])
        new_tl, moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=100, delta=-50)
        assert _start(new_tl, "a_x") == 80  # 顶住 a_prev
        assert moves[0]["new_start"] == 80
        restored = restore_sync_lock_shifts(new_tl, moves)
        assert _start(restored, "a_x") == 100

    def test_zero_delta_noop(self):
        tl = _timeline(v_clips=[_clip("v_c", "v1", 0)],
                       a_clips=[_clip("a_x", "a1", 100)])
        new_tl, moves = shift_sync_lock_followers(
            tl, exclude_track_ids=["v1"], from_frame=0, delta=0)
        assert new_tl is tl
        assert moves == ()


class TestMergedRippleSpans:

    def test_linked_pair_merges_to_single_event(self):
        spans = merged_ripple_spans([
            _clip("v_c", "v1", 100, 100),
            _clip("a_c", "a1", 100, 100),
        ])
        assert spans == [(100, 200)]

    def test_disjoint_spans_stay_separate(self):
        spans = merged_ripple_spans([
            _clip("a_c", "a1", 300, 100),
            _clip("v_c", "v1", 100, 100),
        ])
        assert spans == [(100, 200), (300, 400)]

    def test_same_track_contiguous_merges(self):
        spans = merged_ripple_spans([
            _clip("c1", "v1", 100, 50),
            _clip("c2", "v1", 150, 50),
        ])
        assert spans == [(100, 200)]


# ─────────────────────────────────────────────────────────────────────────────
# RippleDeleteCommand：跨轨跟随 + undo
# ─────────────────────────────────────────────────────────────────────────────

class TestRippleDeleteSyncLock:

    def test_other_track_follows_and_undo_restores(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        cmd = RippleDeleteCommand(
            command_id="c1", label="t", track_id="v1", clip_id="v1c")
        new_tl, updated = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 0  # 左移 100
        restored = updated.undo(new_tl)
        assert restored.get_clip_by_id("v1c") is not None
        assert _start(restored, "a_x") == 100

    def test_sync_lock_off_track_keeps_position(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)],
                       a_sync=False)
        cmd = RippleDeleteCommand(
            command_id="c1", label="t", track_id="v1", clip_id="v1c")
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 100  # 有意失步

    def test_clip_before_ripple_point_does_not_move(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 100, 100)],
                       a_clips=[_clip("a_before", "a1", 0, 90),
                                _clip("a_x", "a1", 200, 50)])
        cmd = RippleDeleteCommand(
            command_id="c1", label="t", track_id="v1", clip_id="v1c")
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_before") == 0
        assert _start(new_tl, "a_x") == 100


# ─────────────────────────────────────────────────────────────────────────────
# RippleTrimCommand：跨轨跟随（右缩短/延伸、左缩短不动 followers）
# ─────────────────────────────────────────────────────────────────────────────

class TestRippleTrimSyncLock:

    def test_right_shorten_follows_other_track(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        cmd = RippleTrimCommand.create_right_trim("v1", "v1c", new_end=80)
        new_tl, updated = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 80  # 右移点 100，左移 20
        restored = updated.undo(new_tl)
        assert _start(restored, "a_x") == 100
        assert restored.get_clip_by_id("v1c").timeline_duration == 100

    def test_right_extend_pushes_other_track(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        cmd = RippleTrimCommand.create_right_trim("v1", "v1c", new_end=120)
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 120

    def test_left_shorten_follows_at_removed_head_end(self):
        # 左缩短 30 = 等价删除头部 [0, 30)：其他轨 start >= 30 左移 30
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_mid", "a1", 40, 20),
                                _clip("a_x", "a1", 100, 50)])
        cmd = RippleTrimCommand.create_left_trim("v1", "v1c", new_start=30)
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_mid") == 10
        assert _start(new_tl, "a_x") == 70

    def test_left_extend_does_not_move_followers(self):
        # 入点锚定语义：左延伸不动 followers，其他轨也不动
        v1c = _clip("v1c", "v1", 50, 100)
        v1c = ClipInstance(
            clip_id="v1c", asset_id="a_v1c", track_id="v1",
            timeline_start=50, timeline_duration=120,
            source_start=50, source_duration=620,
        )
        tl = _timeline(v_clips=[v1c],
                       a_clips=[_clip("a_x", "a1", 170, 50)])
        cmd = RippleTrimCommand.create_left_trim("v1", "v1c", new_start=30)
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 170

    def test_apply_sync_lock_false_disables_follow(self):
        # 链接 A/V 宏的联动命令不施加跨轨跟随
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        cmd = RippleTrimCommand.create_right_trim(
            "v1", "v1c", new_end=80, apply_sync_lock=False)
        new_tl, _ = cmd.apply(tl)
        assert _start(new_tl, "a_x") == 100

    def test_serialization_round_trip_keeps_moves(self):
        tl = _timeline(v_clips=[_clip("v1c", "v1", 0, 100)],
                       a_clips=[_clip("a_x", "a1", 100, 50)])
        cmd = RippleTrimCommand.create_right_trim("v1", "v1c", new_end=80)
        new_tl, updated = cmd.apply(tl)
        data = updated.to_dict()
        restored_cmd = RippleTrimCommand.from_dict(data)
        restored = restored_cmd.undo(new_tl)
        assert _start(restored, "a_x") == 100


# ─────────────────────────────────────────────────────────────────────────────
# BatchDeleteCommand：链接对单次跟随
# ─────────────────────────────────────────────────────────────────────────────

class TestBatchDeleteSyncLock:

    def _three_track_timeline(self) -> TimelineSequence:
        v1 = EditTrack(track_id="v1", name="V1", track_type="video", index=0,
                       clips=(_clip("vc", "v1", 0, 100, linked_id="ac"),))
        a1 = EditTrack(track_id="a1", name="A1", track_type="audio", index=0,
                       clips=(_clip("ac", "a1", 0, 100, linked_id="vc"),))
        v2 = EditTrack(track_id="v2", name="V2", track_type="video", index=1,
                       clips=(_clip("v2x", "v2", 100, 50),))
        return TimelineSequence(
            timeline_id="tl1", name="Sync", fps=Rational(24, 1),
            video_tracks=(v1, v2), audio_tracks=(a1,),
        )

    def test_linked_pair_deletion_shifts_v2_once(self):
        tl = self._three_track_timeline()
        cmd = BatchDeleteCommand.create(tl, ["vc", "ac"], ripple=True)
        new_tl, updated = cmd.apply(tl)
        # 链接对同跨度合并为一个事件：v2 只左移 100，不双重位移
        assert _start(new_tl, "v2x") == 0
        restored = updated.undo(new_tl)
        assert _start(restored, "v2x") == 100
        assert restored.get_clip_by_id("vc") is not None
        assert restored.get_clip_by_id("ac") is not None


# ─────────────────────────────────────────────────────────────────────────────
# InsertClipCommand：跨轨跟随
# ─────────────────────────────────────────────────────────────────────────────

class TestInsertSyncLock:

    def test_insert_pushes_other_track_and_undo_restores(self):
        # 在 v1 的间隙（100-140）插入 40 帧片段：本轨 v_tail 与同步锁轨
        # a1 的 a_x（与 v_tail 同步在 140）都右移 40。
        tl = _timeline(v_clips=[_clip("v_tail", "v1", 140, 50)],
                       a_clips=[_clip("a_x", "a1", 140, 50)])
        cmd = InsertClipCommand(
            command_id="i1", label="t", asset_id="a_new",
            in_frame=0, out_frame=39, playhead=100, track_id="v1")
        new_tl, updated = cmd.apply(tl)
        assert _start(new_tl, "v_tail") == 180  # 本轨右移 40
        assert _start(new_tl, "a_x") == 180     # 同步锁跟随
        restored = updated.undo(new_tl)
        assert _start(restored, "a_x") == 140
        assert restored.get_clip_by_id(updated._inserted_clip_id) is None
