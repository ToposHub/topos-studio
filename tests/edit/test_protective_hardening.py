"""批 E 防护回归：R01 快照隔离 / R02 未知字段保留 / R03 领域不变量。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §4。
"""

from __future__ import annotations

import pytest

from src.features.edit.core.commands import SplitClipCommand
from src.features.edit.core.timeline_ops import validate_timeline
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineMarker,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          linked: str | None = None) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"a-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=0,
        source_duration=500,
        linked_clip_id=linked,
    )


def _seq(*clips: ClipInstance, track_id: str = "v1") -> TimelineSequence:
    track = EditTrack(track_id=track_id, name="V1", track_type="video", index=0)
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    for c in clips:
        timeline = timeline.with_clip_added(track_id, c)
    return timeline


# ---------------------------------------------------------------- R01

def test_split_snapshot_isolated_from_live_mutation() -> None:
    """命令快照深拷贝：修改活动片段的嵌套容器不污染 undo 载荷。"""
    container = {"key": "live"}
    clip = _clip("a", "v1", 0, 100)
    import dataclasses
    clip = dataclasses.replace(
        clip, effects=({"type": "x", "params": container},)
    )
    seq = _seq(clip)
    cmd = SplitClipCommand.create("v1", "a", 50)
    new = cmd.apply(seq)

    # 活动时间线上原地修改嵌套 dict（frozen 模型的已知风险路径）
    container["key"] = "mutated"

    restored = cmd.undo(new)
    restored_ids = [c.clip_id for c in restored.get_track_by_id("v1").clips]
    assert restored_ids == ["a"]  # 单一片段完整恢复
    # 深拷贝的 before 快照保留原始容器值，不被活动端污染
    before_effects = cmd._before_clips[0].effects
    assert before_effects[0]["params"]["key"] == "live"


# ---------------------------------------------------------------- R02

def test_clip_unknown_fields_roundtrip() -> None:
    clip = _clip("a", "v1", 0, 10)
    data = clip.to_dict()
    data["future_field"] = {"nested": [1, 2, 3]}
    data["future_scalar"] = 42

    restored = ClipInstance.from_dict(data)
    out = restored.to_dict()
    assert out["future_field"] == {"nested": [1, 2, 3]}
    assert out["future_scalar"] == 42
    # 已知字段不受 extras 影响
    assert restored.timeline_duration == 10


def test_clip_known_keys_not_shadowed_by_extras() -> None:
    clip = _clip("a", "v1", 5, 10)
    data = clip.to_dict()
    data["timeline_duration"] = 999  # 已知键：正常解析路径优先
    restored = ClipInstance.from_dict(data)
    assert restored.timeline_duration == 999
    assert "timeline_duration" not in restored._extra_fields


# ---------------------------------------------------------------- R03

def test_validate_reports_stale_transition_center() -> None:
    """时间变换后 center 留在旧 seam——validator 必须报告。"""
    seq = _seq(_clip("a", "v1", 0, 100), _clip("b", "v1", 100, 100))
    stale = TransitionInstance(
        transition_id="tr", transition_type="cross_dissolve",
        track_id="v1", left_clip_id="a", right_clip_id="b",
        center_frame=0, duration=20, alignment="centered",
    )
    # with_transition_added 会校验并拒绝非法 center；时间变换的残留
    # 状态走 _rebuild 直接构造（正是 validator 要抓的输入）
    seq = seq.with_track_updated(
        seq.get_track_by_id("v1")._rebuild(transitions=(stale,))
    )
    errors = validate_timeline(seq)
    assert any("center" in e or "seam" in e for e in errors), errors


def test_validate_reports_broken_reciprocal_link() -> None:
    seq = _seq(_clip("a", "v1", 0, 50, linked="ghost"))
    errors = validate_timeline(seq)
    assert any("ghost" in e for e in errors), errors


def test_validate_reports_marker_outside_owner() -> None:
    seq = _seq(_clip("a", "v1", 0, 50))
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m", frame=75, clip_id="a"),),
    )
    errors = validate_timeline(seq)
    assert any("m " in e and "outside" in e for e in errors), errors


def test_validate_clean_timeline_passes() -> None:
    seq = _seq(_clip("a", "v1", 0, 50))
    seq = seq._rebuild(markers=(
        TimelineMarker(marker_id="m", frame=10, clip_id="a"),),
    )
    assert validate_timeline(seq) == []
