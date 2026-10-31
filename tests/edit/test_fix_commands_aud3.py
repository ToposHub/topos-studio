"""批 A Critical 修复回归测试（AUD-16201 / AUD-16202 / AUD-16203）。

覆盖 src/features/edit/core/commands.py 三条审查发现：

- AUD-16201: AddTransition / RemoveTransition / UpdateTransition 的
  apply/undo 必须保留轨道全部字段（strip_eq/automation/sends 等），
  不得手工 EditTrack 重建导致 14+ 字段回落默认值。
- AUD-16202: BatchDeleteCommand(ripple=True) 的 undo 必须把跟随片段
  移回原位（move_cmd.apply），随后恢复被删片段不得产生重叠。
- AUD-16203: OverwriteClipCommand.undo 只删除 apply 记录的分裂残留
  白名单（_split_clip_ids），不得误删同轨未受影响的其他素材片段。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.edit_core

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Send,
    StripEQParams,
    TimelineSequence,
    TransitionInstance,
)
from src.shared.keyframe import KeyframeTrack
from src.features.edit.core.commands import (
    AddTransitionCommand,
    BatchDeleteCommand,
    OverwriteClipCommand,
    RemoveTransitionCommand,
    UpdateTransitionCommand,
)


# ================================================================
# Helpers
# ================================================================

def _clip(
    clip_id: str,
    asset_id: str,
    track_id: str,
    start: int,
    duration: int = 100,
    linked_id: str | None = None,
) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=asset_id,
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=duration + 500,
        linked_clip_id=linked_id,
    )


# 全部"会被手工 EditTrack 重建丢掉"的字段（AUD-16201 清单）
_TRACK_FIELDS: tuple[str, ...] = (
    "name", "track_type", "index", "clips", "subtitle_clips",
    "is_enabled", "is_locked", "is_sync_locked", "is_visible",
    "is_muted", "is_solo", "volume", "pan", "strip_eq", "color",
    "automation", "automation_mode", "is_bus", "output_bus_id",
    "sends", "effect_chain", "stem_group", "track_role",
    "caption_metadata",
)


def _assert_track_fields_preserved(before: EditTrack, after: EditTrack) -> None:
    for field_name in _TRACK_FIELDS:
        assert getattr(after, field_name) == getattr(before, field_name), (
            f"track field {field_name!r} was reset by transition command "
            f"(AUD-16201 regression)"
        )


def _populated_audio_track(clips: tuple[ClipInstance, ...]) -> EditTrack:
    """带满非默认混音/路由/元数据字段的音频轨（转场命令不得重置）。"""
    return EditTrack(
        track_id="a1",
        name="A1",
        track_type="audio",
        index=0,
        clips=clips,
        is_enabled=False,
        is_locked=True,
        is_sync_locked=False,
        is_visible=False,
        is_muted=True,
        is_solo=True,
        volume=1.5,
        pan=-0.5,
        color="#ff5722",
        strip_eq=StripEQParams(mode="3-band", bands=(1.5, 0.0, -2.0), gain=2.0),
        automation={
            "volume": KeyframeTrack(param_name="volume", min_val=0.0, max_val=2.0),
        },
        automation_mode="read",
        is_bus=False,
        output_bus_id="bus_9",
        sends=(Send(send_id="s1", target_bus_id="bus_9", level=0.5, enabled=True),),
        stem_group="dialogue",
    )


def _timeline_with(track: EditTrack) -> TimelineSequence:
    if track.track_type == "audio":
        return TimelineSequence(
            timeline_id="tl1", name="T", fps=24, width=1920, height=1080,
            sample_rate=48000, audio_tracks=(track,),
        )
    return TimelineSequence(
        timeline_id="tl1", name="T", fps=24, width=1920, height=1080,
        sample_rate=48000, video_tracks=(track,),
    )


def _video_timeline(clips: tuple[ClipInstance, ...]) -> TimelineSequence:
    track = EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=clips)
    return TimelineSequence(
        timeline_id="tl1", name="T", fps=24, width=1920, height=1080,
        sample_rate=48000, video_tracks=(track,),
    )


def _video_two_track_timeline(
    v_clips: tuple[ClipInstance, ...],
    a_clips: tuple[ClipInstance, ...],
) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="tl1", name="T", fps=24, width=1920, height=1080,
        sample_rate=48000,
        video_tracks=(EditTrack(track_id="v1", name="V1", track_type="video",
                                index=0, clips=v_clips),),
        audio_tracks=(EditTrack(track_id="a1", name="A1", track_type="audio",
                                index=0, clips=a_clips),),
    )


def _start(tl: TimelineSequence, clip_id: str) -> int:
    clip = tl.get_clip_by_id(clip_id)
    assert clip is not None, f"clip {clip_id} missing"
    return clip.timeline_start


# ================================================================
# AUD-16201: 转场命令不得重置轨道字段
# ================================================================

class TestTransitionCommandsPreserveTrackFields:
    """Add/Remove/Update Transition 的 apply/undo 均走 _rebuild。"""

    def _audio_clips(self) -> tuple[ClipInstance, ...]:
        return (_clip("c1", "asset_a", "a1", 0, 100),
                _clip("c2", "asset_b", "a1", 100, 100))

    def _transition(self, transition_id: str = "tr1") -> TransitionInstance:
        return TransitionInstance(
            transition_id=transition_id,
            transition_type="audio:constant_power",
            track_id="a1",
            left_clip_id="c1",
            right_clip_id="c2",
            center_frame=100,
            duration=10,
            alignment="centered",
        )

    def test_add_transition_apply_preserves_fields(self) -> None:
        track = _populated_audio_track(self._audio_clips())
        tl = _timeline_with(track)
        cmd = AddTransitionCommand(
            command_id="cmd_add", label="add", transition=self._transition(),
        )

        applied = cmd.apply(tl)
        after = applied.get_track_by_id("a1")
        assert after is not None
        _assert_track_fields_preserved(track, after)
        assert any(t.transition_id == "tr1" for t in after.transitions)

    def test_add_transition_undo_preserves_fields(self) -> None:
        track = _populated_audio_track(self._audio_clips())
        tl = _timeline_with(track)
        cmd = AddTransitionCommand(
            command_id="cmd_add", label="add", transition=self._transition(),
        )

        applied = cmd.apply(tl)
        undone = cmd.undo(applied)
        after = undone.get_track_by_id("a1")
        assert after is not None
        _assert_track_fields_preserved(track, after)
        assert after.transitions == ()

    def test_remove_transition_apply_preserves_fields(self) -> None:
        track = _populated_audio_track(self._audio_clips()) \
            ._rebuild(transitions=(self._transition(),))
        tl = _timeline_with(track)
        cmd = RemoveTransitionCommand.create(tl, "tr1", "a1")

        applied = cmd.apply(tl)
        after = applied.get_track_by_id("a1")
        assert after is not None
        _assert_track_fields_preserved(track, after)
        assert after.transitions == ()

    def test_remove_transition_undo_preserves_fields(self) -> None:
        transition = self._transition()
        track = _populated_audio_track(self._audio_clips()) \
            ._rebuild(transitions=(transition,))
        tl = _timeline_with(track)
        cmd = RemoveTransitionCommand.create(tl, "tr1", "a1")

        applied = cmd.apply(tl)
        undone = cmd.undo(applied)
        after = undone.get_track_by_id("a1")
        assert after is not None
        _assert_track_fields_preserved(track, after)
        assert [t.transition_id for t in after.transitions] == ["tr1"]
        assert after.transitions[0] == transition

    def test_update_transition_apply_and_undo_preserve_fields(self) -> None:
        track = _populated_audio_track(self._audio_clips()) \
            ._rebuild(transitions=(self._transition(),))
        tl = _timeline_with(track)
        cmd = UpdateTransitionCommand.create_type(
            tl, "tr1", "a1", "audio:equal_power",
        )

        applied = cmd.apply(tl)
        after_apply = applied.get_track_by_id("a1")
        assert after_apply is not None
        _assert_track_fields_preserved(track, after_apply)
        assert after_apply.transitions[0].transition_type == "audio:equal_power"

        undone = cmd.undo(applied)
        after_undo = undone.get_track_by_id("a1")
        assert after_undo is not None
        _assert_track_fields_preserved(track, after_undo)
        assert after_undo.transitions[0].transition_type == "audio:constant_power"


# ================================================================
# AUD-16202: BatchDelete(ripple=True) undo 移回跟随片段
# ================================================================

class TestBatchDeleteRippleUndo:
    def test_single_clip_ripple_undo_restores_followers(self) -> None:
        tl = _video_timeline((
            _clip("a", "asset_a", "v1", 0, 100),
            _clip("b", "asset_b", "v1", 100, 100),
            _clip("c", "asset_c", "v1", 200, 100),
        ))
        cmd = BatchDeleteCommand.create(tl, ["a"], ripple=True)

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result

        # ripple 左移跟随片段
        assert applied.get_clip_by_id("a") is None
        assert _start(applied, "b") == 0
        assert _start(applied, "c") == 100

        # undo：跟随片段移回原位，被删片段恢复，无重叠
        undone = updated.undo(applied)
        assert _start(undone, "a") == 0
        assert _start(undone, "b") == 100
        assert _start(undone, "c") == 200
        track = undone.get_track_by_id("v1")
        assert track is not None
        assert len(track.clips) == 3

    def test_multi_clip_ripple_undo_via_factory(self) -> None:
        tl = _video_timeline((
            _clip("a", "asset_a", "v1", 0, 100),
            _clip("b", "asset_b", "v1", 100, 100),
            _clip("c", "asset_c", "v1", 200, 100),
        ))
        cmd = BatchDeleteCommand.create(tl, ["a", "b"], ripple=True)

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result

        assert applied.get_clip_by_id("a") is None
        assert applied.get_clip_by_id("b") is None
        assert _start(applied, "c") == 0

        undone = updated.undo(applied)
        assert _start(undone, "a") == 0
        assert _start(undone, "b") == 100
        assert _start(undone, "c") == 200

    def test_linked_av_pair_ripple_undo_restores_both_tracks(self) -> None:
        tl = _video_two_track_timeline(
            v_clips=(_clip("vc", "asset_v", "v1", 0, 100),
                     _clip("vf", "asset_f", "v1", 100, 100)),
            a_clips=(_clip("ac", "asset_v", "a1", 0, 100, linked_id="vc"),
                     _clip("af", "asset_g", "a1", 100, 100)),
        )
        cmd = BatchDeleteCommand.create(tl, ["vc", "ac"], ripple=True)

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result

        assert _start(applied, "vf") == 0
        assert _start(applied, "af") == 0

        undone = updated.undo(applied)
        assert _start(undone, "vc") == 0
        assert _start(undone, "ac") == 0
        assert _start(undone, "vf") == 100
        assert _start(undone, "af") == 100


# ================================================================
# AUD-16203: Overwrite undo 分裂残留白名单
# ================================================================

class TestOverwriteUndoSplitWhitelist:
    def test_undo_keeps_unaffected_clips_and_removes_split_residues(self) -> None:
        # p 在 playhead 之前、m 被 playhead 中间劈开、q 在覆盖终点之后：
        # 三者中 p/q 不受影响，undo 后必须全部保留且 m 恢复原状。
        tl = _video_timeline((
            _clip("p", "asset_p", "v1", 0, 50),      # [0, 50)
            _clip("m", "asset_m", "v1", 50, 100),    # [50, 150)
            _clip("q", "asset_q", "v1", 200, 50),    # [200, 250)
        ))
        cmd = OverwriteClipCommand.create(
            asset_id="new_asset", in_frame=0, out_frame=29,
            playhead=70, track_id="v1",
        )

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result

        # apply：插入片段 [70,100)，m 被分裂为左右两段。N08/C3-08 起
        # 左半**保留原 ID**（head 转场/链接/marker 引用不悬空），只有
        # 右半是新 ID —— split 残留白名单 = 右半 1 个。
        track_after = applied.get_track_by_id("v1")
        assert track_after is not None
        assert updated._inserted_clip_id is not None
        split_ids = set(updated._split_clip_ids or ())
        assert len(split_ids) == 1  # m 的右半段
        all_ids_after = {c.clip_id for c in track_after.clips}
        assert {"p", "q"} <= all_ids_after
        assert "m" in all_ids_after  # 左半保留原 ID
        m_left = track_after.get_clip_by_id("m")
        assert m_left is not None and m_left.timeline_end == 70

        # undo：只删插入片段 + 白名单内的分裂残留
        undone = updated.undo(applied)
        track = undone.get_track_by_id("v1")
        assert track is not None
        ids = {c.clip_id for c in track.clips}
        assert {"p", "m", "q"} <= ids, (
            f"unaffected clips were destroyed by undo (AUD-16203): {ids}"
        )
        assert updated._inserted_clip_id not in ids
        assert not (split_ids & ids)

        # m 恢复原始时间窗口
        m = track.get_clip_by_id("m")
        assert m is not None
        assert m.timeline_start == 50 and m.timeline_duration == 100
        p = track.get_clip_by_id("p")
        q = track.get_clip_by_id("q")
        assert p is not None and q is not None
        assert p.timeline_start == 0 and p.timeline_duration == 50
        assert q.timeline_start == 200 and q.timeline_duration == 50

    def test_undo_restores_fully_covered_clip(self) -> None:
        tl = _video_timeline((
            _clip("f", "asset_f", "v1", 80, 10),    # [80, 90) 完全被覆盖
            _clip("q", "asset_q", "v1", 200, 50),   # [200, 250) 不受影响
        ))
        cmd = OverwriteClipCommand.create(
            asset_id="new_asset", in_frame=0, out_frame=29,
            playhead=70, track_id="v1",
        )

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result
        track_after = applied.get_track_by_id("v1")
        assert track_after is not None
        assert "f" not in {c.clip_id for c in track_after.clips}
        assert updated._split_clip_ids == ()

        undone = updated.undo(applied)
        track = undone.get_track_by_id("v1")
        assert track is not None
        ids = {c.clip_id for c in track.clips}
        assert {"f", "q"} <= ids
        assert updated._inserted_clip_id not in ids
        f = track.get_clip_by_id("f")
        assert f is not None
        assert f.timeline_start == 80 and f.timeline_duration == 10

    def test_undo_keeps_same_asset_clip_at_playhead(self) -> None:
        # 同素材片段恰好在 playhead 处：插入片段识别必须用 apply 前后的
        # ID 差集，undo 不得删掉这个既有片段。
        tl = _video_timeline((
            _clip("e", "new_asset", "v1", 70, 50),   # [70, 120) 与插入同素材
            _clip("q", "asset_q", "v1", 200, 50),
        ))
        cmd = OverwriteClipCommand.create(
            asset_id="new_asset", in_frame=0, out_frame=29,
            playhead=70, track_id="v1",
        )

        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        applied, updated = result
        track_after = applied.get_track_by_id("v1")
        assert track_after is not None

        undone = updated.undo(applied)
        track = undone.get_track_by_id("v1")
        assert track is not None
        ids = {c.clip_id for c in track.clips}
        assert {"e", "q"} <= ids
        assert updated._inserted_clip_id not in ids
        e = track.get_clip_by_id("e")
        assert e is not None
        assert e.timeline_start == 70 and e.timeline_duration == 50

    def test_split_clip_ids_survive_serialization_roundtrip(self) -> None:
        tl = _video_timeline((
            _clip("m", "asset_m", "v1", 50, 100),
        ))
        cmd = OverwriteClipCommand.create(
            asset_id="new_asset", in_frame=0, out_frame=29,
            playhead=70, track_id="v1",
        )
        result = cmd.apply(tl)
        assert isinstance(result, tuple)
        _, updated = result
        assert updated._split_clip_ids

        restored = OverwriteClipCommand.from_dict(updated.to_dict())
        assert set(restored._split_clip_ids or ()) == set(updated._split_clip_ids)
        assert restored._inserted_clip_id == updated._inserted_clip_id
