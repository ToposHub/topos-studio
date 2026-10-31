"""M15.1 · 快照比较面板回归（UI 入口→compare_snapshots 真实链）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

pytestmark = pytest.mark.edit_core

from src.features.edit.ui.snapshot_compare_panel import SnapshotComparePanel


def _payload(timelines):
    return {"timelines": timelines, "full_data": {"timeline_sequences": timelines}}


class TestSnapshotComparePanel:
    def test_compare_shows_entries(self, qtbot):
        # 快照 A：一个 clip；快照 B：clip 修剪 + 新时间线
        a = _payload({"t1": {"timeline_id": "t1", "name": "T", "fps": 25.0,
                             "width": 1920, "height": 1080}})
        b = _payload({
            "t1": {"timeline_id": "t1", "name": "T", "fps": 25.0,
                   "width": 1920, "height": 1080},
            "t2": {"timeline_id": "t2", "name": "T2", "fps": 24.0,
                   "width": 1920, "height": 1080}})
        panel = SnapshotComparePanel({"a": a, "b": b})
        qtbot.addWidget(panel)
        panel._from_combo.setCurrentText("a")
        panel._to_combo.setCurrentText("b")
        panel._on_compare()
        assert panel.result is not None
        assert panel._list.count() >= 1
        assert "timeline_added" in panel.result.summary
        assert "差异" in panel._status.text() or "无差异" in panel._status.text()

    def test_same_snapshot_rejected(self, qtbot):
        panel = SnapshotComparePanel({"a": {}, "b": {}})
        qtbot.addWidget(panel)
        panel._from_combo.setCurrentText("a")
        panel._to_combo.setCurrentText("a")
        panel._on_compare()
        assert panel.result is None
        assert "不同" in panel._status.text()
