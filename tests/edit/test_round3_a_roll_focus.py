"""第三轮复查批A回归：Roll 正常提交与 TrimMonitor 抢焦（N15）+ 链接
锁轨 trim release 收敛（N09，视图层）。

对应 topos_round3_interaction_report.md T3-07 / extended_tools 证据。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QFocusEvent

from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)
from src.features.edit.ui.timeline_interaction import InteractionState
from src.features.edit.ui.timeline_items import ClipItem

pytestmark = pytest.mark.edit_core


def _clip(clip_id: str, track_id: str, start: int, duration: int,
          **kw) -> ClipInstance:
    return ClipInstance(
        clip_id=clip_id,
        asset_id=f"asset-{clip_id}",
        track_id=track_id,
        timeline_start=start,
        timeline_duration=duration,
        source_start=kw.pop("source_start", 0),
        source_duration=kw.pop("source_duration", 500),
        **kw,
    )


def _three_clip_timeline() -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    timeline = timeline.with_clip_added("v1", _clip("a", "v1", 0, 100))
    timeline = timeline.with_clip_added("v1", _clip("b", "v1", 100, 100))
    return timeline


def _linked_timeline(audio_locked: bool = False) -> TimelineSequence:
    timeline = TimelineSequence(timeline_id="t1", name="T", fps=24.0)
    timeline = timeline.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    timeline = timeline.with_track_added(EditTrack(
        track_id="a1", name="A1", track_type="audio", index=1,
        is_locked=audio_locked))
    timeline = timeline.with_clip_added(
        "v1", _clip("vc", "v1", 0, 100, linked_clip_id="ac"))
    timeline = timeline.with_clip_added(
        "a1", _clip("ac", "a1", 0, 100, linked_clip_id="vc"))
    return timeline


# ------------------------------------------------------------------ N15

def test_n15_trim_monitor_is_non_activating(qtbot: Any) -> None:
    """Trim Monitor 构造即带 WA_ShowWithoutActivating（被动显示窗）。"""
    from src.features.edit.ui.trim_numeric_dialog import TrimMonitorDialog

    monitor = TrimMonitorDialog()
    qtbot.addWidget(monitor)
    assert monitor.testAttribute(
        Qt.WidgetAttribute.WA_ShowWithoutActivating)


def test_embedded_monitor_has_no_focus_exception_for_external_window(qtbot: Any) -> None:
    """A viewer child never activates a Tool window; real focus loss still cancels."""
    from src.features.edit.ui.timeline_view import TimelineView
    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_three_clip_timeline())
    view._on_roll_trim_started("a", "b", 100)
    monitor = view._trim_monitor()
    assert not monitor.isWindow()
    assert monitor.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert monitor.isHidden()  # Standalone timelines do not open a monitor window.
    view.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut, Qt.FocusReason.ActiveWindowFocusReason))
    assert view._interaction.is_idle


def test_n15_normal_roll_commits_after_monitor_shown(
        qtbot: Any) -> None:
    """监视器已显示的完整 Roll：preview → release 正常提交一次。"""
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_three_clip_timeline())

    view._on_roll_trim_started("a", "b", 100)
    assert view._trim_monitor_dialog is not None
    view._on_roll_trim_dragged(10)
    view._on_roll_trim_finished("a", "b", 10)

    assert view._timeline.get_clip_by_id("a").timeline_duration == 110
    assert view._timeline.get_clip_by_id("b").timeline_start == 110
    assert view._timeline.get_clip_by_id("b").timeline_duration == 90
    assert len(view._command_stack._undo_stack) == 1


# ------------------------------------------------------------------ N09（视图层）

def test_n09_linked_locked_trim_release_reverts_visuals(
        qtbot: Any) -> None:
    """开放 V 链接锁 A：release 组内预检拒绝，双片 Item 回模型、零历史。"""
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.load_timeline(_linked_timeline(audio_locked=True))
    view._linked_selection_enabled = True

    vc_item = view._items_map.get("vc")
    ac_item = view._items_map.get("ac")
    assert isinstance(vc_item, ClipItem) and isinstance(ac_item, ClipItem)

    # 拖拽预览已把两片 Item 带离模型（模拟 live trim 左移 10 帧）
    vc_item._start_frame = 10
    vc_item._duration_frames = 90
    vc_item.update_source_range(10, 490)
    ac_item._start_frame = 10
    ac_item._duration_frames = 90
    ac_item.update_source_range(10, 490)
    vc_item._update_geometry()
    ac_item._update_geometry()

    # 正常 release（此前宏 apply 冒未捕获 ValueError 且预览残留）
    view._on_trim_left_finished(
        "vc", "v1",
        old_start=0, old_duration=100, old_source_start=0,
        new_start=10, new_source_start=10)

    # 模型零变更、零历史
    assert view._timeline.get_clip_by_id("vc").timeline_start == 0
    assert view._timeline.get_clip_by_id("vc").timeline_duration == 100
    assert view._timeline.get_clip_by_id("ac").timeline_start == 0
    assert len(view._command_stack._undo_stack) == 0
    # 双片 Item 从 canonical 恢复
    assert vc_item._start_frame == 0 and vc_item._duration_frames == 100
    assert vc_item._source_start == 0
    assert ac_item._start_frame == 0 and ac_item._duration_frames == 100
    assert ac_item._source_start == 0
