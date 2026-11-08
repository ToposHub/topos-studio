"""R08：速度点把手接线——独立命中/拖动/取消 + 松手单命令。

缺口（复审 R08）：B15 的 ``MoveSpeedPointCommand`` 存在但
timeline_items/view 从未引用；速度点标记没有独立拖动状态——不能宣称
达到 Resolve 速度点把手拖动体验。

契约：

- 把手独立命中（10px 命中区，优先于分段菜单/片段移动）；
- 拖动 = 本地预览（item 内钳制：邻点不交叉、整数源帧）；
- 松手 = 单命令 ``MoveSpeedPointCommand``（linked 同步 + 转场快照
  undo 由 R02/B15 保证）；一次 undo 还原；
- Esc/隐藏控件取消；冻结/锁轨/位置锁明确拒绝并提示；
- 初载（窗口投影，F05）与多点场景的把手几何一致。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.clip_params import SpeedParams, SpeedPoint
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
    TransitionInstance,
)

pytestmark = pytest.mark.edit_ui


def _clip(**kwargs: Any) -> ClipInstance:
    base = dict(
        clip_id="c1", asset_id="asset", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100,
        speed=SpeedParams(speed=1.0, speed_points=(
            SpeedPoint(source_offset=50, speed=2.0),)),
    )
    base.update(kwargs)
    return ClipInstance(**base)


def _timeline(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(
        "r08", "R08", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "V1", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _view_item(qtbot: Any, clip: ClipInstance):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline(clip))
    item = view._items_map["c1"]
    item.set_retime_controls_visible(True)
    return view, item


class _Pos:
    def __init__(self, x: float, y: float) -> None:
        self._x, self._y = x, y

    def x(self) -> float:
        return self._x

    def y(self) -> float:
        return self._y


def _strip_pos(item: Any, x: float) -> _Pos:
    return _Pos(x, item._track_height - 10)


# ── 命中与几何 ───────────────────────────────────────────────────────


def test_r08_handle_hit_test_and_projection(qtbot):
    """单点 50（1x→2x）：把手 x = 50/(50+25)×width；命中返回源偏移 50。"""
    view, item = _view_item(qtbot, _clip())
    item._width = 200.0
    # segments: (0,50,1.0,t0=0) len 50; (50,100,2.0,t=50) len 25; total 75
    assert item._retime_point_handle_at(_strip_pos(item, -100)) is None
    hit = item._retime_point_handle_at(_strip_pos(item, 200 * 50 / 75))
    assert hit == 50
    # 命中区外（±6px 之外）
    assert item._retime_point_handle_at(_strip_pos(item, 200 * 50 / 75 + 9)) \
        is None
    # 控件隐藏时不命中
    item.set_retime_controls_visible(False)
    assert item._retime_point_handle_at(_strip_pos(item, 200 * 50 / 75)) is None


def test_r08_window_projected_points_hit(qtbot):
    """切片窗口（curve_offset 16）：投影后的窗口坐标命中（F05 同源）。"""
    clip = _clip(speed=SpeedParams(
        speed=1.0, curve_offset=16, curve_length=100,
        speed_points=(SpeedPoint(source_offset=50, speed=2.0),)))
    view, item = _view_item(qtbot, clip)
    item.set_retime_presentation(clip.speed)
    # 窗口坐标：50−16=34；segments (0,34,1.0)(34,100,2.0)
    # total = 34 + 33 = 67；x = width×34/67
    item._width = 201.0
    hit = item._retime_point_handle_at(_strip_pos(item, 201 * 34 / 67))
    assert hit == 34
    # 提交命令需要的正是窗口坐标（MoveSpeedPointCommand 契约）
    params = view.get_timeline().get_clip_by_id("c1").speed
    assert params.speed_points[0].source_offset == 50
    assert params.curve_offset == 16


def test_r08_inverse_mapping_roundtrip(qtbot):
    view, item = _view_item(qtbot, _clip())
    item._width = 300.0
    # 正逆互逆：offset 40 → x → offset
    x = item._retime_point_x_for_offset(40)
    assert item._retime_point_offset_at_x(x) == 40
    x2 = item._retime_point_x_for_offset(70)
    assert item._retime_point_offset_at_x(x2) == 70


# ── 拖动状态机（本地预览/钳制/取消） ─────────────────────────────────


def test_r08_drag_preview_clamped_between_neighbors(qtbot):
    """多点 (30, 2x)(70, 0.5x)：拖 30 不能越过邻点 70，也不低于 1。"""
    clip = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=30, speed=2.0),
        SpeedPoint(source_offset=70, speed=0.5),
    )))
    view, item = _view_item(qtbot, clip)
    item._width = 400.0

    item._begin_retime_point_drag(30)
    # 拖到最左（x=0）→ 钳到下界 1
    item._update_retime_point_drag(0.0)
    assert item._retime_point_drag_offset == 1
    # 拖到最右（x=width）→ 钳到上界 69（邻点 70 − 1）
    item._update_retime_point_drag(400.0)
    assert item._retime_point_drag_offset == 69


def test_r08_dragged_signal_and_finish_signal(qtbot):
    view, item = _view_item(qtbot, _clip())
    item._width = 300.0
    previews: list[tuple] = []
    finished: list[tuple] = []
    item.retime_point_dragged.connect(
        lambda *a: previews.append(a))
    item.retime_point_move_finished.connect(
        lambda *a: finished.append(a))

    item._begin_retime_point_drag(50)
    target_x = item._retime_point_x_for_offset(40)
    item._update_retime_point_drag(target_x)
    assert previews == [("c1", "v1", 50, 40)]
    item._finish_retime_point_drag()
    assert finished == [("c1", "v1", 50, 40)]
    # 状态已清
    assert item._retime_point_drag_origin is None


def test_r08_zero_delta_finish_emits_nothing(qtbot):
    view, item = _view_item(qtbot, _clip())
    finished: list[tuple] = []
    item.retime_point_move_finished.connect(lambda *a: finished.append(a))
    item._begin_retime_point_drag(50)
    item._update_retime_point_drag(
        item._retime_point_x_for_offset(50))
    item._finish_retime_point_drag()
    assert finished == []


def test_r08_cancel_clears_preview_without_commit(qtbot):
    view, item = _view_item(qtbot, _clip())
    finished: list[tuple] = []
    item.retime_point_move_finished.connect(lambda *a: finished.append(a))
    item._begin_retime_point_drag(50)
    item._update_retime_point_drag(0.0)  # 预览到 1
    assert item._retime_point_drag_offset == 1
    item._cancel_retime_point_drag()
    assert item._retime_point_drag_origin is None
    assert finished == []
    # 隐藏控件同样只取消
    item._begin_retime_point_drag(50)
    item.set_retime_controls_visible(False)
    assert item._retime_point_drag_origin is None
    assert finished == []


# ── 视图提交（单命令/拒绝路径） ──────────────────────────────────────


def _commit_view(qtbot, clip=None, messages=None):
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_timeline(clip if clip is not None else _clip()))
    view._items_map["c1"].set_retime_controls_visible(True)
    if messages is not None:
        view.status_message_requested.connect(messages.append)
    return view


def test_r08_release_commits_single_command_with_transition_undo(qtbot):
    """松手 → MoveSpeedPointCommand 提交；一次 undo 还原 clip + 转场。"""
    clip = _clip()
    timeline = TimelineSequence(
        "r08", "R08", width=64, height=48, fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(clip,),
            transitions=(TransitionInstance(
                transition_id="t1", track_id="v1", left_clip_id="c1",
                center_frame=clip.timeline_end, duration=80,
                alignment="end_on_cut"),)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(timeline)
    view._items_map["c1"].set_retime_controls_visible(True)

    view._on_retime_point_move_finished("c1", "v1", 50, 40)

    changed = view.get_timeline()
    params = changed.get_clip_by_id("c1").speed
    assert [p.source_offset for p in params.speed_points] == [40]
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    undone = view.get_timeline()
    assert [p.source_offset
            for p in undone.get_clip_by_id("c1").speed.speed_points] == [50]
    # R02：转场快照还原（时长 80 不被钳制值洗掉）
    assert undone.get_track_by_id("v1").transitions[0].duration == 80


def test_r08_frozen_clip_rejected_with_status(qtbot):
    messages: list[str] = []
    view = _commit_view(qtbot, clip=_clip().with_freeze_frame(5),
                        messages=messages)
    before = view.get_timeline()
    view._on_retime_point_move_finished("c1", "v1", 50, 40)
    assert messages and "frozen" in messages[0].lower()
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r08_invalid_target_rejected_zero_write(qtbot):
    """越过邻点的落点（绕过 item 钳制直调）→ 命令拒绝 + 状态诊断。"""
    clip = _clip(speed=SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=30, speed=2.0),
        SpeedPoint(source_offset=70, speed=0.5),
    )))
    messages: list[str] = []
    view = _commit_view(qtbot, clip=clip, messages=messages)
    before = view.get_timeline()
    view._on_retime_point_move_finished("c1", "v1", 30, 70)  # 与邻点重合
    assert messages
    assert view.get_timeline() is before
    assert not view._command_stack._undo_stack


def test_r08_drag_status_message(qtbot):
    messages: list[str] = []
    view = _commit_view(qtbot, messages=messages)
    view._on_retime_point_dragged("c1", "v1", 50, 40)
    assert messages and "50" in messages[0] and "40" in messages[0]
