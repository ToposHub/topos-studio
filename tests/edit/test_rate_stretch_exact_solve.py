"""R04：拉伸按权威源时基反解速度（统一 planner）。

反例（S05）：源 5 帧、3x、时间线 2 帧（round(5/3)），拉到 10 帧：
旧换算 ``s' = s × old/new = 3×2/10 = 0.6x`` → 实际时长
round(5/0.6)=8 帧 ≠ 10。权威解 ``s = base/target = 5/10 = 0.5x`` →
10 帧，松手图形边缘 = 提交边缘。

纯函数 ``solve_speed_for_timeline_duration``（全程 Fraction 无舍入）
+ 视图统一 planner（rate-stretch 工具与 retime 顶部拉伸共用）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.clip_params import SpeedParams  # noqa: E402
from src.features.edit.core.timeline_ops import (  # noqa: E402
    solve_speed_for_timeline_duration,
    timeline_duration_for_source,
)
from src.features.edit.core.timeline_v2 import (  # noqa: E402
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


# ── 纯函数：权威反解 ────────────────────────────────────────────────


def test_r04_counterexample_five_frames_three_x():
    """S05：源 5、3x、时间线 2，拉到 10 → 0.5x 且时长精确 10。"""
    speed, achievable, reason = solve_speed_for_timeline_duration(
        5, 10)
    assert speed == pytest.approx(0.5)
    assert achievable == 10
    assert reason is None
    # 权威时长换算闭环：5 / 0.5 = 10
    assert timeline_duration_for_source(
        5, SpeedParams(speed=speed)) == 10


def test_r04_mixed_fps_exact_solve():
    """48fps 源 5 帧在 24fps 时间线（base=2.5），拉到 10 → 0.25x=10 帧。

    旧换算：3×1/10=0.3 → round(2.5/0.3)=8 ✗。
    """
    speed, achievable, reason = solve_speed_for_timeline_duration(
        5, 10, source_fps_num=48, source_fps_den=1,
        timeline_fps_num=24, timeline_fps_den=1)
    assert speed == pytest.approx(0.25)
    assert achievable == 10
    assert reason is None


def test_r04_clamp_bounds_report_reason_and_achievable():
    # 下界：源 2 帧拉到 40 → 0.05 → 钳 0.1，实际 20 帧
    speed, achievable, reason = solve_speed_for_timeline_duration(
        2, 40)
    assert speed == pytest.approx(0.1)
    assert achievable == 20
    assert reason == "min"
    # 上界：源 100 拉到 12 → 8.33 → 钳 8，实际 round(12.5)=12 帧
    speed, achievable, reason = solve_speed_for_timeline_duration(
        100, 12)
    assert speed == pytest.approx(8.0)
    assert achievable == 12
    assert reason == "max"
    # 界内反解精确命中：100/13 ≈ 7.69 ≤ 8 → 13 帧
    speed, achievable, reason = solve_speed_for_timeline_duration(
        100, 13)
    assert reason is None
    assert achievable == 13


def test_r04_fractional_fps_no_rounding_drift():
    """23.976 源 1001 帧在 23.976 时间线（base=1001），拉到 2002 → 0.5x。"""
    speed, achievable, reason = solve_speed_for_timeline_duration(
    1001, 2002, source_fps_num=24000, source_fps_den=1001,
        timeline_fps_num=24000, timeline_fps_den=1001)
    assert speed == pytest.approx(0.5)
    assert achievable == 2002
    assert reason is None


def test_r04_short_source_high_rate_representable():
    """短源高倍率：源 3 拉到 21 → 1/7x，round(3/(1/7))=21 精确命中。"""
    speed, achievable, reason = solve_speed_for_timeline_duration(3, 21)
    assert speed == pytest.approx(1 / 7)
    assert achievable == 21
    assert reason is None


def test_r04_invalid_target_rejected():
    with pytest.raises(ValueError):
        solve_speed_for_timeline_duration(5, 0)


# ── 视图统一 planner（工具 + retime 顶部共用） ──────────────────────


def _view(clip: ClipInstance, qtbot: Any):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(TimelineSequence(
        "r04", "R04", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    ))
    return view


def _short_clip() -> ClipInstance:
    """源 5 帧、3x → 时间线 round(5/3)=2 帧（S05 反例几何）。"""
    return ClipInstance(
        "c1", "asset", "v1", 0, 2, 0, 5,
        speed=SpeedParams(speed=3.0))


def test_r04_rate_stretch_hits_target_duration(qtbot):
    view = _view(_short_clip(), qtbot)
    view._on_rate_stretch_finished("c1", "v1", 2, 10)
    clip = view.get_timeline().get_clip_by_id("c1")
    assert clip.speed.speed == pytest.approx(0.5)
    assert clip.timeline_duration == 10  # 松手边缘 = 提交边缘
    assert len(view._command_stack._undo_stack) == 1


def test_r04_retime_header_stretch_same_planner(qtbot):
    """retime 顶部拉伸与工具走同一 planner：同几何同结果。"""
    view = _view(_short_clip(), qtbot)
    view._on_retime_stretch_finished("c1", "v1", 2, 10)
    clip = view.get_timeline().get_clip_by_id("c1")
    assert clip.speed.speed == pytest.approx(0.5)
    assert clip.timeline_duration == 10


def test_r04_mixed_fps_view_stretch_exact(qtbot):
    """48fps 源（fps 元数据在 clip 上）在 24fps 时间线：2.5 ticks 拉到 10。"""
    clip = ClipInstance(
        "c1", "asset", "v1", 0, 1, 0, 5,
        speed=SpeedParams(speed=3.0),
        _source_fps_num=48, _source_fps_den=1)
    view = _view(clip, qtbot)
    view._on_rate_stretch_finished("c1", "v1", 1, 10)
    changed = view.get_timeline().get_clip_by_id("c1")
    assert changed.speed.speed == pytest.approx(0.25)
    assert changed.timeline_duration == 10


def test_r04_clamped_stretch_commits_achievable(qtbot):
    """钳制时提交实际可实现时长（几何回落）。"""
    clip = ClipInstance(
        "c1", "asset", "v1", 0, 2, 0, 2, speed=SpeedParams(speed=1.0))
    view = _view(clip, qtbot)
    # 源 2 帧 1x，拉到 40 → 0.1x、实际 20 帧
    view._on_rate_stretch_finished("c1", "v1", 2, 40)
    changed = view.get_timeline().get_clip_by_id("c1")
    assert changed.speed.speed == pytest.approx(0.1)
    assert changed.timeline_duration == 20  # 几何回落到可实现值


def test_r04_advanced_payload_carries_achievable_and_reason(qtbot):
    """带动画的复杂片段走高级请求：载荷含精确速度/可实现时长/钳制原因。

    源 5 拉到 60 → 5/60 ≈ 0.083 < 0.1 → 钳 0.1x，实际 round(5/0.1)=50。
    """
    requests: list[tuple] = []
    clip = _short_clip().with_keyframe_added("opacity", 1, 0.5)
    view = _view(clip, qtbot)
    view.clip_speed_settings_requested.connect(
        lambda *args: requests.append(args))
    view._on_rate_stretch_finished("c1", "v1", 2, 60)
    assert len(requests) == 1
    _cid, _tid, payload = requests[0]
    assert payload["action"] == "stretch"
    value = payload["value"]
    assert value["speed"] == pytest.approx(0.1)
    assert value["achievable_duration"] == 50
    assert value["clamped"] == "min"
    # 零写入：模型未变、无历史
    assert view.get_timeline().get_clip_by_id("c1").timeline_duration == 2
    assert not view._command_stack._undo_stack


def test_r04_noop_and_cancel_zero_history(qtbot):
    # 取消：new == original → 直接返回，零历史
    view = _view(_short_clip(), qtbot)
    view._on_rate_stretch_finished("c1", "v1", 2, 2)
    assert not view._command_stack._undo_stack
    # 有变化：源 5 拉到 15 → 1/3x（当前 3x）
    view2 = _view(_short_clip(), qtbot)
    view2._on_rate_stretch_finished("c1", "v1", 2, 15)
    assert view2.get_timeline().get_clip_by_id("c1").speed.speed == \
        pytest.approx(1 / 3)
    # no-op：解出速度与当前相同（源 5 @1x 拉到 5 → new==old 返回）
    view3 = _view(ClipInstance(
        "c1", "asset", "v1", 0, 5, 0, 5, speed=SpeedParams(speed=1.0)),
        qtbot)
    view3._on_rate_stretch_finished("c1", "v1", 5, 5)
    assert not view3._command_stack._undo_stack
