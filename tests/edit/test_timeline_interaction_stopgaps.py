"""批 A2 交互止血回归：INT-01 波纹单击 / INT-02 Esc 取消 / INT-03 平移
路由 / INT-04 刀片按键与锁轨 / INT-09 选区去重。

对应 docs/edit_page_full_audit_optimization_plan_2026-10-14.md §3/§10。
ClipItem 事件链用真实 QGraphicsView + QTest 驱动（press→move→release），
不走 MagicMock，避免"只测 helper 不测交互"的假阳性。
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QGraphicsScene, QGraphicsView

from src.features.edit.core.selection import (
    SelectionItem,
    SelectionType,
    TimelineSelection,
)
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import ClipItem, EditTool

pytestmark = pytest.mark.edit_core


def _make_ripple_clip(start: int = 100, duration: int = 100) -> ClipItem:
    return ClipItem(
        clip_id="clip-1",
        track_id="v1",
        asset_name="source.mov",
        start_frame=start,
        duration_frames=duration,
        pixels_per_frame=2.0,
        track_index=0,
        source_start=0,
        source_duration=duration,
        track_height=60,
        media_type="video",
        asset_id="asset-1",
        asset_path="/tmp/source.mov",
    )


class _Collector:
    def __init__(self, clip: ClipItem) -> None:
        self.events: list[tuple[str, tuple]] = []
        clip.ripple_trim_left_finished.connect(
            lambda *a: self.events.append(("ripple_left", a))
        )
        clip.ripple_trim_right_finished.connect(
            lambda *a: self.events.append(("ripple_right", a))
        )
        clip.drag_finished.connect(lambda *a: self.events.append(("drag", a)))
        clip.interaction_started.connect(
            lambda *a: self.events.append(("started", a))
        )
        clip.interaction_finished.connect(
            lambda *a: self.events.append(("finished", a))
        )


def _drive(
    view: QGraphicsView,
    item: ClipItem,
    points: list[tuple[float, float]],
    press: bool = True,
    release: bool = True,
) -> None:
    vp = lambda p: view.viewport().mapFrom(
        view, view.mapFromScene(item.mapToScene(QPointF(*p)))
    )
    first = vp(points[0])
    if press:
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=first)
    elif points:
        QTest.mouseMove(view.viewport(), pos=first)
    for pt in points[1:]:
        QTest.mouseMove(view.viewport(), pos=vp(pt))
    if release:
        QTest.mouseRelease(
            view.viewport(), Qt.MouseButton.LeftButton, pos=vp(points[-1])
        )


def _clip_view(qtbot):
    scene = QGraphicsScene()
    clip = _make_ripple_clip()
    scene.addItem(clip)
    view = QGraphicsView(scene)
    view.resize(900, 300)
    qtbot.addWidget(view)
    view.show()
    return view, clip


# ---------------------------------------------------------------- INT-01

def test_ripple_right_click_without_drag_emits_nothing(qtbot: Any) -> None:
    """右边缘单击（无移动）不得把 new_end=0 发进命令路径。"""
    view, clip = _clip_view(qtbot)
    clip.set_active_tool(EditTool.RIPPLE)
    collector = _Collector(clip)

    edge = clip._width - 2.0
    _drive(view, clip, [(edge, 30.0)])

    kinds = [e[0] for e in collector.events]
    assert kinds == ["started", "finished"]
    assert clip._final_new_end == 200  # press 已重置，无残留 0


def test_ripple_left_click_without_drag_emits_nothing(qtbot: Any) -> None:
    view, clip = _clip_view(qtbot)
    clip.set_active_tool(EditTool.RIPPLE)
    collector = _Collector(clip)

    _drive(view, clip, [(2.0, 30.0)])

    kinds = [e[0] for e in collector.events]
    assert kinds == ["started", "finished"]
    assert clip._final_new_start == 100


def test_ripple_stale_final_not_replayed_on_next_click(qtbot: Any) -> None:
    """完成一次真实拖拽后，随后的纯单击不得重放旧 final 值。"""
    view, clip = _clip_view(qtbot)
    clip.set_active_tool(EditTool.RIPPLE)
    collector = _Collector(clip)

    edge = clip._width - 2.0
    _drive(view, clip, [(edge, 30.0), (edge + 20.0, 30.0)])
    assert [e for e in collector.events if e[0] == "ripple_right"], "真实拖拽应提交一次"

    collector.events.clear()
    _drive(view, clip, [(edge, 30.0)])
    assert not [e for e in collector.events if e[0] == "ripple_right"]


# ---------------------------------------------------------------- INT-02

def test_cancel_active_drag_restores_state_and_blocks_release(qtbot: Any) -> None:
    """Esc 取消后松开鼠标不得提交任何 finished 命令信号。"""
    view, clip = _clip_view(qtbot)
    clip.set_active_tool(EditTool.SELECT)
    collector = _Collector(clip)

    edge = clip._width - 2.0
    QTest.mousePress(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=view.viewport().mapFrom(
            view, view.mapFromScene(clip.mapToScene(QPointF(50.0, 30.0)))
        ),
    )
    QTest.mouseMove(
        view.viewport(),
        pos=view.viewport().mapFrom(
            view, view.mapFromScene(clip.mapToScene(QPointF(110.0, 30.0)))
        ),
    )

    # Esc 取消（view 路径最终调用 item.cancel_active_drag）
    clip.cancel_active_drag()
    assert clip._start_frame == 100
    assert clip._drag_mode is None
    assert clip._is_dragging is False

    QTest.mouseRelease(
        view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=view.viewport().mapFrom(
            view, view.mapFromScene(clip.mapToScene(QPointF(110.0, 30.0)))
        ),
    )
    kinds = [e[0] for e in collector.events]
    # 取消后的 release 不产生任何命令信号（只有会话起止通知）
    assert kinds == ["started", "finished"]


def test_interaction_started_emitted_for_drag_modes(qtbot: Any) -> None:
    """ClipItem press 需要通知 view 登记 InteractionSession（Esc 可取消）。"""
    view, clip = _clip_view(qtbot)
    collector = _Collector(clip)
    _drive(view, clip, [(50.0, 30.0)])
    assert ("started", ("clip-1", "v1", "move")) in collector.events


# ---------------------------------------------------------------- INT-04

def _timeline_with_locked_clip(locked: bool = True) -> TimelineSequence:
    track = EditTrack(
        track_id="v1", name="V1", track_type="video", index=0, is_locked=locked
    )
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(track)
    return timeline.with_clip_added(
        track.track_id,
        ClipInstance(
            clip_id="c1",
            asset_id="a1",
            track_id=track.track_id,
            timeline_start=0,
            timeline_duration=100,
            source_start=0,
            source_duration=100,
        ),
    )


def _view_stub(timeline: TimelineSequence):
    view = MagicMock()
    view._timeline = timeline
    view._is_track_locked = lambda track_id: any(
        t.is_locked and t.track_id == track_id for t in timeline.all_tracks
    )
    view._pixels_per_frame = 2.0
    executed: list[object] = []
    view._execute_command.side_effect = executed.append
    view._layout_engine.track_at_y_with_area.return_value = ("v1", None)
    return view, executed


def test_blade_ignores_right_click(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_locked_clip(locked=False)
    view, executed = _view_stub(timeline)

    event = MagicMock()
    event.button.return_value = Qt.MouseButton.RightButton
    view.mapToScene.return_value = QPointF(300.0, 40.0)

    TimelineView._handle_blade_click(view, event)
    assert executed == []


def test_blade_ignores_middle_click(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_locked_clip(locked=False)
    view, executed = _view_stub(timeline)
    event = MagicMock()
    event.button.return_value = Qt.MouseButton.MiddleButton
    view.mapToScene.return_value = QPointF(300.0, 40.0)

    TimelineView._handle_blade_click(view, event)
    assert executed == []


def test_blade_respects_locked_track(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_locked_clip(locked=True)
    view, executed = _view_stub(timeline)
    event = MagicMock()
    event.button.return_value = Qt.MouseButton.LeftButton
    view.mapToScene.return_value = QPointF(300.0, 40.0)

    TimelineView._handle_blade_click(view, event)
    assert executed == []


def test_ripple_right_guard_rejects_invalid_new_end(qtbot: Any) -> None:
    """view 端领域防护：new_end<=start 直接拒绝，不发命令。"""
    from src.features.edit.ui.timeline_view import TimelineView

    timeline = _timeline_with_locked_clip(locked=False)
    view, executed = _view_stub(timeline)

    view._updating = False
    view._snap_enabled = False
    view._items_map = {}
    TimelineView._on_ripple_trim_right_finished(view, "c1", "v1", 100, 0)
    assert executed == []


# ---------------------------------------------------------------- INT-09

def test_selection_dedup_invariant() -> None:
    item = SelectionItem(
        item_type=SelectionType.CLIP,
        item_id="c",
        track_id="v1",
        timeline_id="t1",
    )
    selection = TimelineSelection(items=(item, item, item), primary_item=item)
    assert selection.items == (item,)
    assert selection.selected_clip_ids == ("c",)
    assert not selection.is_multi


def test_selection_shift_union_merge_dedups() -> None:
    item = SelectionItem(
        item_type=SelectionType.CLIP,
        item_id="c",
        track_id="v1",
        timeline_id="t1",
    )
    merged = TimelineSelection(items=(item, item), primary_item=item)
    assert len(merged.items) == 1


def test_selection_primary_normalized_to_existing_item() -> None:
    item = SelectionItem(
        item_type=SelectionType.CLIP,
        item_id="c",
        track_id="v1",
        timeline_id="t1",
    )
    other = SelectionItem(
        item_type=SelectionType.CLIP,
        item_id="d",
        track_id="v1",
        timeline_id="t1",
    )
    selection = TimelineSelection(items=(other,), primary_item=item)
    assert selection.primary_item == other


def test_selection_cross_type_same_id_not_deduped() -> None:
    clip = SelectionItem(
        item_type=SelectionType.CLIP, item_id="x", timeline_id="t1"
    )
    track = SelectionItem(
        item_type=SelectionType.TRACK, item_id="x", timeline_id="t1"
    )
    selection = TimelineSelection(items=(clip, track))
    assert len(selection.items) == 2
