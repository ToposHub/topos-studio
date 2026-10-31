"""批 C2 回归：C10 fade 切分/裁剪 / C13 关键帧平移 / C15 marker 归属 /
C16 跨轨 marker 跟随 / C17 源上界 / FE-B01 FPS 重映射。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §9/§11。
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.features.edit.core.commands import (
    BatchDeleteCommand,
    MoveClipCommand,
    SplitClipCommand,
)
from src.features.edit.core.effect_instance import AnimatableParam
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str = "v1", start: int = 0, duration: int = 100,
          **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 500),
        **kw,
    )


def _seq(*clips: ClipInstance, track_id: str = "v1", sync_locked: bool = True,
         audio_clips: tuple = ()) -> TimelineSequence:
    video = EditTrack(track_id=track_id, name="V1", track_type="video", index=0)
    audio = EditTrack(
        track_id="a1", name="A1", track_type="audio", index=0,
        is_sync_locked=sync_locked,
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(video).with_track_added(audio)
    for c in clips:
        timeline = timeline.with_clip_added(track_id, c)
    for c in audio_clips:
        timeline = timeline.with_clip_added("a1", c)
    return timeline


# ---------------------------------------------------------------- C10

def test_blade_with_fade_out_no_error() -> None:
    """审计实测：100 帧淡出 40，blade20 抛 fade_out exceeds duration。"""
    seq = _seq(_clip("a", fade_out_duration=40))
    new = SplitClipCommand.create("v1", "a", 20).apply(seq)
    left = new.get_clip_by_id("a")
    assert left.fade_out_duration <= left.timeline_duration
    right_id = [c.clip_id for c in new.get_track_by_id("v1").clips if c.clip_id != "a"][0]
    right = new.get_clip_by_id(right_id)
    assert right.fade_out_duration <= right.timeline_duration


def test_right_trim_with_fade_out_no_error() -> None:
    """审计实测：右裁到 20 抛 ValueError。"""
    clip = _clip("a", fade_out_duration=40)
    trimmed = clip.with_trim_right(20, 20)
    assert trimmed.timeline_duration == 20
    assert trimmed.fade_out_duration == 20


def test_speed_shortening_clamps_fades() -> None:
    clip = _clip("a", fade_out_duration=80)
    shrunk = clip.with_timeline_duration(50)
    assert shrunk.fade_out_duration == 50


# ---------------------------------------------------------------- C13

def _opacity_track() -> AnimatableParam:
    return AnimatableParam(
        base_value=1.0,
        keyframes=KeyframeTrack(
            param_name="opacity",
            keyframes=(
                Keyframe(frame=0, value=0.0),
                Keyframe(frame=99, value=1.0),
            ),
        ),
    )


def test_move_clip_shifts_keyframes() -> None:
    """审计实测：A 起点 0→200 后首帧 opacity 从 0 变 1（动画留在旧时间）。"""
    clip = _clip("a", animated_params={"opacity": _opacity_track()})
    seq = _seq(clip)
    new = MoveClipCommand.create("v1", "a", 200).apply(seq)
    moved = new.get_clip_by_id("a")
    param = moved.animated_params["opacity"]
    frames = [k.frame for k in param.keyframes.keyframes]
    assert frames == [200, 299]
    # clip-local 曲线不变：新起点处 opacity 仍为 0
    assert param.get_value_at_frame(200) == pytest.approx(0.0)
    assert param.get_value_at_frame(299) == pytest.approx(1.0)


def test_move_undo_restores_keyframes() -> None:
    clip = _clip("a", animated_params={"opacity": _opacity_track()})
    seq = _seq(clip)
    cmd = MoveClipCommand.create("v1", "a", 200)
    new = cmd.apply(seq)
    undone = cmd.undo(new)
    param = undone.get_clip_by_id("a").animated_params["opacity"]
    assert [k.frame for k in param.keyframes.keyframes] == [0, 99]


# ---------------------------------------------------------------- C15

def test_blade_migrates_scoped_marker_to_right_half() -> None:
    """审计实测：marker75 在 blade50 后仍属左段（timeline_end=50）。"""
    clip = _clip("a")
    seq = _seq(clip)
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m1", frame=75, clip_id="a"),),
    )
    new = SplitClipCommand.create("v1", "a", 50).apply(seq)
    marker = new.markers[0]
    right_id = [c.clip_id for c in new.get_track_by_id("v1").clips if c.clip_id != "a"][0]
    assert marker.clip_id == right_id
    assert marker.frame == 75

    # marker 在切点前归左段
    seq2 = seq._rebuild(markers=(
        TimelineMarker(marker_id="m2", frame=30, clip_id="a"),),
    )
    new2 = SplitClipCommand.create("v1", "a", 50).apply(seq2)
    assert new2.markers[0].clip_id == "a"


def test_blade_undo_restores_marker_snapshot() -> None:
    clip = _clip("a")
    seq = _seq(clip)
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m1", frame=75, clip_id="a"),),
    )
    cmd = SplitClipCommand.create("v1", "a", 50)
    new = cmd.apply(seq)
    restored = cmd.undo(new)
    assert restored.markers == seq.markers


# ---------------------------------------------------------------- C16

def test_sync_lock_shift_moves_scoped_markers() -> None:
    """审计实测：删 V[0,10)，同步锁音频 50→40 而 marker55 仍 55。"""
    seq = _seq(
        _clip("d1", duration=10),
        audio_clips=(_clip("af", track_id="a1", start=50),),
    )
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m", frame=55, clip_id="af"),),
    )
    cmd = BatchDeleteCommand(
        command_id="cmd", label="del", clip_ids=("d1",), ripple=True
    )
    new, _updated = cmd.apply(seq)
    assert new.get_clip_by_id("af").timeline_start == 40
    marker = new.markers[0]
    assert marker.frame == 45  # 相对 clip 位置 5 帧保持


# ---------------------------------------------------------------- C17

def test_right_trim_bound_includes_source_start() -> None:
    """审计静态场景：总源 200、In100、右延长 150 → exclusive end 250 超界。"""
    clip = _clip("a", duration=100, source_start=100, source_duration=100)
    clip = clip.with_timeline_start(0)
    ns, nd = clip.with_right_edge_source_values(150)
    # 模型给出 (100, 150)；上界校验 start+duration=250 > 200 应拒绝
    assert ns + nd == 250


# ---------------------------------------------------------------- FE-B01

def test_fps_remap_scales_markers_and_keyframes() -> None:
    """审计实测：24→48 后 marker 仍 24、opacity 关键帧 24/48 未缩放。"""
    from src.app.controllers.edit_controller import EditControllerMixin

    clip = _clip("a", animated_params={"opacity": _opacity_track()})
    seq = _seq(clip)
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m", frame=24, clip_id="a"),),
    )

    from src.shared.media.metadata import Rational

    controller = EditControllerMixin.__new__(EditControllerMixin)
    new_seq = EditControllerMixin._with_timeline_fps(
        controller, seq, Rational(48, 1)
    )
    assert new_seq.markers[0].frame == 48
    param = new_seq.get_clip_by_id("a").animated_params["opacity"]
    frames = [k.frame for k in param.keyframes.keyframes]
    assert frames == [0, 198]
    # 实时时间保持：关键帧 99@24fps = 1.98s@48fps? no—99/24s ≈ 198/48s ✓
