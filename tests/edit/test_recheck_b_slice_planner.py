"""复查批次 B-3 回归：R07 统一切分规划（Split/Insert/Overwrite/Roll/Trim）。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.1
（K07）。24 组合切分矩阵：v4 相位载荷保留全部组合的每一帧。慢放重复帧允许
两个子段引用同一源帧；时间线窗口必须精确分割。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.commands import (
    OverwriteClipCommand,
    RollTrimCommand,
    SplitClipCommand,
)
from src.features.edit.core.timeline_ops import (
    insert_clip_at_playhead,
    overwrite_clip_at_playhead,
    roll_trim_clips,
    trim_clip_left,
    trim_clip_right,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _mk(speed=1.0, reverse=False, src_fps=None, tl_fps=None,
        source_duration=120) -> ClipInstance:
    kw = {}
    if src_fps:
        kw.update(_source_fps_num=src_fps, _source_fps_den=1)
    if tl_fps:
        kw.update(_timeline_fps_num=tl_fps, _timeline_fps_den=1)
    ratio = 1.0
    if src_fps and tl_fps:
        ratio = tl_fps / src_fps  # 时间线时长 = 源时长/速度 × tl_fps/src_fps
    dur = max(1, round(source_duration / speed * ratio))
    return ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=dur,
        source_start=0, source_duration=source_duration,
        speed=SpeedParams(speed=speed, reverse=reverse),
        **kw,
    )


def _seq(*clips: ClipInstance) -> TimelineSequence:
    t = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    for c in clips:
        t = t.with_clip_added("v1", c)
    return t


# ---------------------------------------------------- 24 组合矩阵

@pytest.mark.parametrize("src_fps,tl_fps", [(None, None), (24, 24), (30, 24)])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("speed", [0.5, 1.0, 1.25, 2.0])
def test_split_matrix_preserves_frames(src_fps, tl_fps, reverse, speed) -> None:
    """逐帧比较切分前后左右子段的源帧序列（审计 24 矩阵）。"""
    clip = _mk(speed, reverse, src_fps, tl_fps)
    s = _seq(clip)
    dur = clip.timeline_duration
    cut = max(1, dur // 3)
    orig = [clip.timeline_to_source_frame(f) for f in range(dur)]

    cmd = SplitClipCommand.create("v1", "c", cut)
    new = cmd.apply(s)
    left = new.get_clip_by_id("c")
    right = new.get_clip_by_id(cmd._right_clip_id)
    got_left = [
        left.timeline_to_source_frame(left.timeline_start + i)
        for i in range(left.timeline_duration)
    ]
    got_right = [
        right.timeline_to_source_frame(right.timeline_start + i)
        for i in range(right.timeline_duration)
    ]
    assert got_left == orig[:cut]
    assert got_right == orig[cut:]
    # At repeat cadence both children may reference the same boundary source
    # frame. Their timeline windows still partition exactly; each source
    # window must contain every sample it owns (no ±1 tolerance).
    for child, frames in ((left, got_left), (right, got_right)):
        assert all(child.source_start <= frame <= child.source_end for frame in frames)
    assert left.timeline_end == right.timeline_start


# ---------------------------------------------------- Roll 方向感知

def test_roll_reverse_preserves_frames() -> None:
    """K07：reverse A/B roll +10——A 首帧保持 199、B 保留帧 189 不变。"""
    a = ClipInstance(
        clip_id="a", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    b = ClipInstance(
        clip_id="b", asset_id="as", track_id="v1",
        timeline_start=100, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    s = _seq(a, b)
    a_first = a.timeline_to_source_frame(0)          # 199
    b_at_110 = b.timeline_to_source_frame(110)       # 189
    assert (a_first, b_at_110) == (199, 189)

    new = roll_trim_clips(s, "a", "b", 10)
    na = new.get_clip_by_id("a")
    nb = new.get_clip_by_id("b")
    # roll 后切点 110：A 新首帧（timeline 0）仍是 199；B 保留的
    # timeline 110 帧仍播 189（右移只影响 B 的起点，不改变其内容）
    assert na.timeline_to_source_frame(0) == 199
    assert nb.timeline_to_source_frame(110) == 189


def test_roll_trim_command_reverse_roundtrip() -> None:
    a = ClipInstance(
        clip_id="a", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    b = ClipInstance(
        clip_id="b", asset_id="as", track_id="v1",
        timeline_start=100, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    s = _seq(a, b)
    cmd = RollTrimCommand.create("v1", "a", "b", 10)
    new_timeline, _updated = cmd.apply(s)
    restored = _updated.undo(new_timeline) if isinstance(
        cmd.apply(s), tuple
    ) else None
    # apply/undo 后帧域稳定
    assert new_timeline.get_clip_by_id("a").timeline_duration == 110
    if restored is not None:
        assert restored.get_clip_by_id("a").timeline_duration == 100


# ---------------------------------------------------- Insert 跨点切分

def test_insert_crossing_split_uses_mapper() -> None:
    """K07：Insert 跨点切分保留源内容——反向 1x [100,200) 在 40 插入
    10 帧，切分后左半首 3 帧仍是 199/198/197（旧公式变 139）。"""
    c = ClipInstance(
        clip_id="c", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    s = _seq(c)
    new = insert_clip_at_playhead(s, "new", 0, 9, 40, "v1", 1.0)
    track = new.get_track_by_id("v1")
    left = track.get_clip_by_id("c")
    assert left.timeline_duration == 40
    assert [left.timeline_to_source_frame(i) for i in range(3)] == [199, 198, 197]


# ---------------------------------------------------- Overwrite 残片

def test_overwrite_partial_coverage_residue_with_speed() -> None:
    """K07：2x 片段被覆盖掉头部 10 时间线帧——残片源区间从 20 开始
    （旧 effective_speed 公式对变速点/反向错切；此用例常速 2x）。"""
    c = ClipInstance(
        clip_id="c", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=200, speed=SpeedParams(speed=2.0),
    )
    s = _seq(c)
    new = overwrite_clip_at_playhead(s, "new", 0, 9, 0, "v1", 1.0)
    track = new.get_track_by_id("v1")
    residue = track.get_clip_by_id("c")
    assert residue.timeline_start == 10
    assert residue.source_start == 20
    assert residue.source_duration == 180
    # 残片首帧 = 原片段 timeline 10 的源帧
    assert residue.timeline_to_source_frame(10) == 20


def test_overwrite_middle_split_residue() -> None:
    """中间覆盖：2x 片段 [0,100tl) 在 20-30 覆盖——左残 [0,20)、
    右残从源 60 起。"""
    c = ClipInstance(
        clip_id="c", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=200, speed=SpeedParams(speed=2.0),
    )
    s = _seq(c)
    new = overwrite_clip_at_playhead(s, "new", 0, 9, 20, "v1", 1.0)
    track = new.get_track_by_id("v1")
    left = next(cl for cl in track.clips if cl.asset_id == "as" and cl.timeline_start == 0)
    assert left.timeline_duration == 20
    assert left.source_duration == 40
    right = next(
        cl for cl in track.clips
        if cl.asset_id == "as" and cl.timeline_start == 30
    )
    assert right.timeline_start == 30
    assert right.source_start == 60
    assert right.source_duration == 140


# ---------------------------------------------------- legacy trim 入口

def test_legacy_trim_left_mixed_fps() -> None:
    """R07：trim_clip_left 经方向/FPS 映射（30fps 源 24fps 序列修 10
    时间线帧 = 12.5 源帧 → 12/13；关键是不再用 int(10×1)=10）。"""
    c = ClipInstance(
        clip_id="c", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=80,
        source_start=0, source_duration=100,
        _source_fps_num=30, _source_fps_den=1,
        _timeline_fps_num=24, _timeline_fps_den=1,
    )
    s = _seq(c)
    new = trim_clip_left(s, "c", 10)
    t = new.get_clip_by_id("c")
    assert t.source_start == 13  # 12.5 → mapper half-up; phase -1/2 retains later cadence
    assert t.source_duration == 87
    assert [t.timeline_to_source_frame(f) for f in range(10, 80)] == [
        c.timeline_to_source_frame(f) for f in range(10, 80)]
    assert t.timeline_duration == 70


def test_legacy_trim_right_reverse() -> None:
    """R07：反向右修收缩低端源帧（旧公式只减 duration 不动 start）。"""
    c = ClipInstance(
        clip_id="c", asset_id="as", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=100, source_duration=100,
        speed=SpeedParams(speed=1.0, reverse=True),
    )
    s = _seq(c)
    new = trim_clip_right(s, "c", 90)
    t = new.get_clip_by_id("c")
    assert t.source_start == 110
    assert t.source_duration == 90
    assert t.timeline_to_source_frame(0) == 199  # 保留帧不变
