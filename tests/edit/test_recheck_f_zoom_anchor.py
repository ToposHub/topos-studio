"""复查批次 F 回归：R19 缩放锚定连续帧精度。

对应 docs/edit_page_followup_audit_optimization_plan_2026-10-14.md §3.2
（RI-08）：ppf0.1 五次缩放累计 -22.73 帧的方向性漂移。整数滚动条量
化 ≤1px 为平台精度（审计已声明低 ppf 需亚像素原点机制），本批消除
的是截断方向性累积。
"""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QPoint

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_core


def _long_timeline() -> TimelineSequence:
    t = TimelineSequence(timeline_id="t", name="T", fps=24.0)
    t = t.with_track_added(EditTrack(track_id="v1", name="V1", track_type="video", index=0))
    for i in range(500):
        t = t.with_clip_added("v1", ClipInstance(
            clip_id=f"c{i}", asset_id="a", track_id="v1",
            timeline_start=i * 200, timeline_duration=200,
            source_start=0, source_duration=400,
        ))
    return t


@pytest.mark.parametrize("ppf", [0.1, 0.2, 0.5, 1.0, 3.0, 10.0])
def test_zoom_anchor_no_directional_drift(qtbot: Any, ppf: float) -> None:
    """连续缩放正反各 5 次：锚定帧漂移 ≤1px（无系统性累积）。"""
    from src.features.edit.ui.timeline_view import (
        TRACK_HEADER_WIDTH,
        TimelineView,
    )

    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1440, 800)
    view.load_timeline(_long_timeline())
    view.set_zoom(ppf)

    vp = QPoint(813, 300)

    def anchor_frame() -> float:
        scene_x = view.mapToScene(vp).x()
        return (scene_x - TRACK_HEADER_WIDTH) / view._pixels_per_frame

    ref = anchor_frame()
    drifts = []
    for _ in range(5):
        view._zoom_anchored_at(vp, 40)
        drifts.append(anchor_frame() - ref)
    for _ in range(5):
        view._zoom_anchored_at(vp, -40)
        drifts.append(anchor_frame() - ref)

    worst_px = max(abs(d) for d in drifts) * view._pixels_per_frame
    assert worst_px <= 1.0 + 1e-6, (ppf, drifts)
    # 无系统性单向累积：缩放回落后锚定帧复位（审计基线残留 -22.73 帧）
    assert abs(drifts[-1]) * view._pixels_per_frame <= 1.0
    if ppf >= 1.0:
        assert max(abs(d) for d in drifts) < 1.0


def test_zoom_anchor_zero_delta_no_op(qtbot: Any) -> None:
    from src.features.edit.ui.timeline_view import TimelineView

    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1440, 800)
    view.load_timeline(_long_timeline())
    view.set_zoom(1.0)
    ppf_before = view._pixels_per_frame
    view._zoom_anchored_at(QPoint(400, 200), 0.0)
    assert view._pixels_per_frame == ppf_before
