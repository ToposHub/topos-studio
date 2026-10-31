"""M23.1b · Tracker 修正面板回归（节点委托真实链）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.composite_core

from src.features.composite.nodes.tracker_node import TrackerNode
from src.features.color.ui.tracker_correction_panel import (
    TrackerCorrectionPanel,
)


def _node_with_failures():
    node = TrackerNode("t1")
    node.set_track_data({
        "version": 1, "transforms": {
            "0": {"tx": 0.0, "ty": 0.0, "status": "valid"},
            "1": {"tx": 1.0, "ty": 0.0, "status": "failed"},
            "2": {"tx": 0.0, "ty": 0.0, "status": "failed"},
            "3": {"tx": 3.0, "ty": 1.0, "status": "valid"},
        }})
    return node


class TestTrackerCorrectionPanel:
    def test_segments_listed_with_anchor(self, qtbot):
        panel = TrackerCorrectionPanel(_node_with_failures())
        qtbot.addWidget(panel)
        assert panel._segments.count() == 1
        assert "帧 1–2" in panel._segments.item(0).text()
        assert "锚@0" in panel._segments.item(0).text()
        assert "1 帧" in panel._status.text() or "失败段 1" in panel._status.text()

    def test_manual_fix_via_dialog(self, qtbot, monkeypatch):
        from PyQt6.QtWidgets import QInputDialog
        node = _node_with_failures()
        panel = TrackerCorrectionPanel(node)
        qtbot.addWidget(panel)
        answers = iter([(2, True), (5.5, True), (-1.0, True)])
        monkeypatch.setattr(
            QInputDialog, "getInt",
            staticmethod(lambda *a, **k: next(answers)))
        monkeypatch.setattr(
            QInputDialog, "getDouble",
            staticmethod(lambda *a, **k: next(answers)))
        panel._on_manual_fix()
        corr = node.get_manual_corrections()
        assert corr["2"] == {"tx": 5.5, "ty": -1.0}
        # 修正后帧 2 成为锚——失败段只剩帧 1
        plan = node.plan_incremental_retrack()
        spans = [(s.start_frame, s.end_frame) for s in plan.segments]
        assert spans == [(1, 1)]

    def test_retrack_via_injected_engine(self, qtbot):
        node = _node_with_failures()
        calls = []

        def engine(segment, is_cancelled=None):
            calls.append((segment.start_frame, segment.end_frame))
            return {1: {"tx": 1.5, "ty": 0.5},
                    2: {"tx": 2.5, "ty": 0.5}}

        panel = TrackerCorrectionPanel(node, retrack_fn=engine)
        qtbot.addWidget(panel)
        panel._segments.setCurrentRow(0)   # 选中失败段
        panel._on_retrack()
        assert panel._worker is not None  # N13：异步 worker
        assert panel._worker.wait(5000)
        for _ in range(10):  # 投递结果槽（合并/刷新在 UI 线程）
            __import__("PyQt6.QtWidgets", fromlist=["QApplication"]) \
                .QApplication.processEvents()
        assert calls == [(1, 2)]
        data = node.get_track_data()
        assert data["transforms"]["1"]["tx"] == pytest.approx(1.5)
        assert data["transforms"]["2"]["tx"] == pytest.approx(2.5)
        assert node.plan_incremental_retrack().segments == ()

    def test_no_engine_callback_refuses(self, qtbot):
        panel = TrackerCorrectionPanel(_node_with_failures())
        qtbot.addWidget(panel)
        panel._on_retrack()
        assert "未提供跟踪引擎回调" in panel._status.text()
