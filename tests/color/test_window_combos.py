"""N15：Resolve 窗口组合（CPW/LPW/PPW/PCW 一键 = Serial+默认形状窗口）。

验收（计划 N15）：一键组合原子创建（service op：Serial+Window 一命令
一 undo）；默认中性、形状/身份正确可跟踪；菜单走同一命令路径；未知
组合显式拒绝；不以单独 Window 可用冒充组合完成。实机 Resolve 人工
对照登记为待办（API 不支持窗口脚本化创建——见 n15 README）。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph():
    project, sid = EditProject.create(name="n15").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def _corrector_ids(graph):
    return [n.node_id for n in graph.nodes if n.node_type == "corrector"]


@pytest.mark.parametrize("combo,shape_type,min_shapes", [
    ("cpw", "circle", 1),
    ("lpw", "gradient", 1),
    ("ppw", "curve", 1),
    ("pcw", "curve", 1),
])
def test_combo_creates_serial_with_default_window(combo, shape_type,
                                                  min_shapes) -> None:
    graph = _graph()
    before = _corrector_ids(graph)
    service = ColorGraphService(id_factory=lambda: f"n15-{combo}")
    result = service.apply_intent(graph, "add_window_combo",
                                  {"combo": combo})
    assert result.ok, result.diagnostics
    new_ids = [n for n in _corrector_ids(result.graph)
               if n not in before]
    assert len(new_ids) == 1  # 恰一个新 Serial
    node = next(n for n in result.graph.nodes if n.node_id == new_ids[0])
    shapes = list(node.legacy_payload.get("mask_shapes") or [])
    assert len(shapes) >= min_shapes
    assert shapes[0]["type"] == shape_type
    assert shapes[0]["id"] == f"n15-{combo}"     # 跟踪绑定身份稳定
    assert list(shapes[0]["center"]) == [0.5, 0.5]  # 默认居中（freeze 元组）
    assert node.legacy_payload.get("grading_params") in (None, {})  # 中性
    # 结构：新节点串在链上（有入边/出边）
    srcs = {e.src_node for e in result.graph.edges if e.dst_node == node.node_id}
    dsts = {e.dst_node for e in result.graph.edges if e.src_node == node.node_id}
    assert srcs and dsts


def test_combo_shapes_distinct_per_type() -> None:
    graph = _graph()
    counter = iter(range(100))
    service = ColorGraphService(id_factory=lambda: f"x{next(counter)}")
    ppw = service.apply_intent(graph, "add_window_combo", {"combo": "ppw"})
    pcw = service.apply_intent(ppw.graph, "add_window_combo",
                               {"combo": "pcw"})
    assert pcw.ok
    windows = [n.legacy_payload["mask_shapes"][0]
               for n in pcw.graph.nodes
               if n.node_type == "corrector"
               and n.legacy_payload.get("mask_shapes")]
    by_name = {w["name"]: w for w in windows}
    assert len(by_name["Polygon Power Window"]["points"]) == 4
    assert len(by_name["Power Curve Window"]["points"]) == 8


def test_unknown_combo_rejected() -> None:
    graph = _graph()
    service = ColorGraphService()
    result = service.apply_intent(graph, "add_window_combo",
                                  {"combo": "magic"})
    assert not result.ok
    assert result.diagnostics[0].code == "illegal_param_key"


def test_combo_contract_registered() -> None:
    from src.app.controllers.color_graph_controller import ACTION_CONTRACTS
    contract = ACTION_CONTRACTS.get("add_window_combo")
    assert contract is not None
    assert contract.command == "add_window_combo"
    assert "combo" in contract.required_arguments
