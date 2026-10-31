"""
Routing Matrix + Spill 测试（§4.2）。

验证（计划表 §4.2）：
- Routing Matrix：行=源轨，列=Bus/Master，单元格标记 output/send 关系。
- 点击单元格重路由（output 切换）。
- Spill：展开某 Bus 的上游轨道。
- 环/未知目标即时告警。
- routing_changed 信号发出完整路由快照。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make_tracks():
    """构造 (track_id, name, is_bus) 列表。"""
    return [
        ("t1", "Dialogue", False),
        ("t2", "Music", False),
        ("t3", "Effects", False),
        ("busA", "Bus A", True),
        ("busB", "Bus B", True),
    ]


def _make_routing():
    """构造路由快照。"""
    return {
        "t1": {"output_bus_id": "busA", "sends": [], "is_bus": False},
        "t2": {"output_bus_id": None, "sends": [("busB", 0.5)], "is_bus": False},
        "t3": {"output_bus_id": "busA", "sends": [], "is_bus": False},
        "busA": {"output_bus_id": None, "sends": [], "is_bus": True},
        "busB": {"output_bus_id": None, "sends": [], "is_bus": True},
    }


class TestRoutingMatrixConstruction:
    """RoutingMatrixDialog 构造。"""

    def test_construction(self, qapp):
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        assert dlg._table is not None
        # 列：Master + 2 Bus = 3
        assert dlg._table.columnCount() == 3
        # 行：3 源轨
        assert dlg._table.rowCount() == 3

    def test_matrix_shows_output(self, qapp):
        """output 关系显示为 OUT 单元格。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        # t1 output→busA，busA 是第 2 列（col=1：Master=0, busA=1, busB=2）
        item = dlg._table.item(0, 1)
        assert item is not None
        assert item.text() == "OUT"

    def test_matrix_shows_master_output(self, qapp):
        """直送 Master（output_bus_id=None）在 Master 列显示 OUT。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        # t2 output=None（直送 Master），但 t2 有 send 到 busB
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        # t2 是第 1 行（row=1），Master 列 col=0
        item = dlg._table.item(1, 0)
        assert item.text() == "OUT"

    def test_matrix_shows_send(self, qapp):
        """send 关系显示为 SND 单元格。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        # t2 send→busB，busB 是第 3 列（col=2：Master=0, busA=1, busB=2）
        item = dlg._table.item(1, 2)
        assert item.text() == "SND"


class TestRoutingMatrixInteraction:
    """矩阵交互：点击重路由。"""

    def test_click_reroutes_output(self, qapp):
        """点击单元格切换 output 路由。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        # 点击 t1（row=0）的 busB 列（col=2）→ t1 output 改为 busB
        dlg._on_cell_clicked(0, 2)
        routing = dlg.get_routing()
        assert routing["t1"]["output_bus_id"] == "busB"

    def test_click_master_reroutes_to_master(self, qapp):
        """点击 Master 列直送 Master。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        # t1 当前 output=busA，点击 Master 列（col=0）→ 改为 None
        dlg._on_cell_clicked(0, 0)
        routing = dlg.get_routing()
        assert routing["t1"]["output_bus_id"] is None

    def test_routing_changed_signal(self, qapp):
        """routing_changed 信号发出。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        received = []
        dlg.routing_changed.connect(lambda r: received.append(r))
        dlg._on_cell_clicked(0, 2)
        assert len(received) == 1
        assert received[0]["t1"]["output_bus_id"] == "busB"


class TestSpill:
    """Spill：展开 Bus 上游。"""

    def test_spill_returns_upstream(self, qapp):
        """spill 返回路由到 Bus 的上游轨道。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        upstream = dlg.spill("busA")
        # t1, t3 都 output→busA
        assert "t1" in upstream
        assert "t3" in upstream
        assert "t2" not in upstream

    def test_spill_includes_sends(self, qapp):
        """spill 包含 send 到该 Bus 的轨道。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        upstream = dlg.spill("busB")
        # t2 send→busB
        assert "t2" in upstream

    def test_spill_empty_bus(self, qapp):
        """无上游的 Bus 返回空列表。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        # 构造一个无上游的 busC
        tracks = _make_tracks() + [("busC", "Bus C", True)]
        routing = _make_routing()
        routing["busC"] = {"output_bus_id": None, "sends": [], "is_bus": True}
        dlg = RoutingMatrixDialog(tracks, routing)
        upstream = dlg.spill("busC")
        assert upstream == []

    def test_spill_updates_label(self, qapp):
        """spill 更新标签显示上游轨道名。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        dlg.spill("busA")
        text = dlg._spill_label.text()
        assert "Dialogue" in text or "上游" in text

    def test_spill_signal(self, qapp):
        """spill_requested 信号发出。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        received = []
        dlg.spill_requested.connect(lambda bid: received.append(bid))
        dlg.spill("busA")
        assert received == ["busA"]


class TestMatrixWarnings:
    """矩阵告警。"""

    def test_valid_no_warning(self, qapp):
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        dlg = RoutingMatrixDialog(_make_tracks(), _make_routing())
        assert dlg._warning_label.isHidden()

    def test_cycle_shows_warning(self, qapp):
        """环路显示告警。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        routing = _make_routing()
        # 制造环：busA→busB, busB→busA
        routing["busA"]["output_bus_id"] = "busB"
        routing["busB"]["output_bus_id"] = "busA"
        dlg = RoutingMatrixDialog(_make_tracks(), routing)
        assert not dlg._warning_label.isHidden()
        assert "环" in dlg._warning_label.text() or "cycle" in dlg._warning_label.text()

    def test_unknown_target_shows_warning(self, qapp):
        """未知目标显示告警。"""
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        routing = _make_routing()
        routing["t1"]["output_bus_id"] = "ghostBus"
        dlg = RoutingMatrixDialog(_make_tracks(), routing)
        assert not dlg._warning_label.isHidden()


class TestGetRouting:
    """get_routing 返回快照。"""

    def test_get_routing_snapshot(self, qapp):
        from src.shared.ui.audio_mixer.routing_matrix_dialog import RoutingMatrixDialog
        original = _make_routing()
        dlg = RoutingMatrixDialog(_make_tracks(), original)
        snapshot = dlg.get_routing()
        # 快照独立于内部状态
        assert snapshot["t1"]["output_bus_id"] == "busA"
