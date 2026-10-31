"""M11 UI · 口头语删除面板回归（面板→真实 plan 链）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ai_editing.filler_removal import (
    FillerDetectionResult,
    FillerHit,
)
from src.features.edit.ui.filler_removal_panel import FillerRemovalPanel


def _timeline():
    from src.features.edit.core.timeline_v2 import (
        ClipInstance, EditTrack, Rational, TimelineSequence,
    )
    clip = ClipInstance(
        clip_id="c-main", asset_id="a1", track_id="v1",
        timeline_start=0, timeline_duration=100,
        source_start=0, source_duration=100, speed=1.0)
    return TimelineSequence(
        timeline_id="tl1", name="TL", fps=Rational(25, 1),
        video_tracks=(EditTrack(track_id="v1", track_type="video",
                                name="V1", clips=(clip,)),))


class TestFillerRemovalPanel:
    def test_checked_hits_build_real_plan(self, qtbot):
        from types import SimpleNamespace as NS
        detection = FillerDetectionResult(hits=(
            FillerHit("um", 10, 15, 0.95),
            FillerHit("呃", 40, 44, 0.9)))
        project = NS(project_id="p1")
        panel = FillerRemovalPanel(
            detection, project=project, timeline=_timeline(),
            timeline_id="tl1", base_revision=7)
        qtbot.addWidget(panel)
        panel._on_build()
        assert panel.plan is not None
        assert len(panel.plan.operations) == 2
        # 真实 track/clip（M11.1 语义）
        assert all(op.params["track_id"] == "v1"
                   and op.params["clip_id"] == "c-main"
                   for op in panel.plan.operations)

    def test_unchecked_excluded(self, qtbot):
        from PyQt6.QtCore import Qt
        from types import SimpleNamespace as NS
        detection = FillerDetectionResult(hits=(
            FillerHit("um", 10, 15, 0.95),
            FillerHit("uh", 60, 64, 0.8)))
        panel = FillerRemovalPanel(
            detection, project=NS(project_id="p"),
            timeline=_timeline(), timeline_id="tl1", base_revision=1)
        qtbot.addWidget(panel)
        # 取消第二项
        panel._list.item(1).setCheckState(Qt.CheckState.Unchecked)
        panel._on_build()
        assert len(panel.plan.operations) == 1
        assert panel.plan.operations[0].params["start_frame"] == 10


# ================================================================
# M11.3 · 差异预览（勾选 → 真实 clip/区间呈现 → 审批）
# ================================================================

class TestDiffPreview:
    def test_preview_lists_mapped_ranges(self, qtbot):
        from types import SimpleNamespace as NS
        detection = FillerDetectionResult(hits=(
            FillerHit("um", 10, 15, 0.95),
            FillerHit("呃", 40, 44, 0.9)))
        panel = FillerRemovalPanel(
            detection, project=NS(project_id="p1"), timeline=_timeline(),
            timeline_id="tl1", base_revision=7)
        qtbot.addWidget(panel)
        assert panel._preview_list.isHidden()
        panel._on_preview()
        assert not panel._preview_list.isHidden()
        assert panel._preview_list.count() == 2
        texts = [panel._preview_list.item(i).text()
                 for i in range(panel._preview_list.count())]
        assert any("filler:um" in t and "v1/c-main" in t
                   and "帧 10–15" in t for t in texts)
        assert any("filler:呃" in t and "帧 40–44" in t for t in texts)
        assert "差异预览：2 个区间" in panel._status.text()

    def test_preview_no_selection_hidden(self, qtbot):
        from PyQt6.QtCore import Qt
        from types import SimpleNamespace as NS
        detection = FillerDetectionResult(hits=(FillerHit("um", 10, 15, 0.95),))
        panel = FillerRemovalPanel(
            detection, project=NS(project_id="p1"), timeline=_timeline(),
            timeline_id="tl1", base_revision=7)
        qtbot.addWidget(panel)
        panel._list.item(0).setCheckState(Qt.CheckState.Unchecked)
        panel._on_preview()
        assert panel._preview_list.isHidden()
        assert "未勾选" in panel._status.text()

    def test_preview_whole_clip_span(self, qtbot):
        """整段命中（覆盖片段两界）预览显示'整段删除'。"""
        from types import SimpleNamespace as NS
        detection = FillerDetectionResult(hits=(
            FillerHit("all", 0, 100, 0.99),))
        panel = FillerRemovalPanel(
            detection, project=NS(project_id="p1"), timeline=_timeline(),
            timeline_id="tl1", base_revision=7)
        qtbot.addWidget(panel)
        panel._on_preview()
        assert "整段删除" in panel._preview_list.item(0).text()
