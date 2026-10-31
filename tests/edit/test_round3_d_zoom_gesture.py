"""第三轮复查批D回归：N16/T3-08 长缩放手势累计漂移。

手势级连续锚帧缓存——量化误差不回灌（20+20 次实测从 2.10px 降到
≤0.5px，受整数滚动条半像素界约束且不随事件数累计）。
"""

from __future__ import annotations

import pytest
from PyQt6.QtCore import QPoint

from src.features.edit.core.timeline_v2 import (
    ClipInstance, EditTrack, TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _long_timeline() -> TimelineSequence:
    tl = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    tl = tl.with_track_added(EditTrack(
        track_id="v1", name="V1", track_type="video", index=0))
    return tl.with_clip_added("v1", ClipInstance(
        clip_id="c", asset_id="a", track_id="v1",
        timeline_start=0, timeline_duration=100000,
        source_start=0, source_duration=100000))


def test_n16_long_gesture_no_accumulated_drift(qtbot) -> None:
    from src.features.edit.ui.timeline_view import (
        TRACK_HEADER_WIDTH, TimelineView,
    )

    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 400)
    view.load_timeline(_long_timeline())
    pos = QPoint(813, 200)

    worst = 0.0
    for ppf_start in (0.2, 1.0, 3.0, 10.0):
        view.set_zoom(ppf_start)
        view._zoom_gesture_anchor_frame = None
        hbar = view.horizontalScrollBar()
        hbar.setValue(min(800, hbar.maximum()))
        for units in (40, -40):
            for _ in range(20):
                view._zoom_anchored_at(pos, units)
                target = (
                    TRACK_HEADER_WIDTH
                    + view._zoom_gesture_anchor_frame
                    * view._pixels_per_frame
                )
                worst = max(worst, abs(view.mapToScene(pos).x() - target))
    # 整数滚动条半像素量化上界；关键是不随事件数累计（旧实现 2.10px）
    assert worst <= 0.5 + 1e-6, worst


def test_n16_gesture_anchor_expires_after_pause(qtbot) -> None:
    """停顿超窗后重测锚帧（pan/其他滚动后的首次缩放不得用旧锚）。"""
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 400)
    view.load_timeline(_long_timeline())
    pos = QPoint(400, 200)
    view.set_zoom(1.0)

    view._zoom_anchored_at(pos, 40)
    stale_anchor = view._zoom_gesture_anchor_frame
    assert stale_anchor is not None

    # 手动平移后超窗（模拟时间流逝）
    view._zoom_gesture_last_t -= 10.0
    hbar = view.horizontalScrollBar()
    hbar.setValue(hbar.value() + 300)
    view._zoom_anchored_at(pos, 40)
    assert view._zoom_gesture_anchor_frame != stale_anchor, (
        "超窗后必须从当前视图重测锚帧"
    )
