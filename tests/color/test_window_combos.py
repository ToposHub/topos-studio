"""N15：Resolve 窗口组合（CPW/LPW/PPW/PCW 一键 = Serial+默认形状窗口）。

验收（计划 N15）：一键组合原子创建（service op：Serial+Window 一命令
一 undo）；默认中性、形状/身份正确可跟踪；菜单走同一命令路径；未知
组合显式拒绝；不以单独 Window 可用冒充组合完成。实机 Resolve 人工
对照：Resolve 20.2.1 Color 页实机操作（2026-11-05）。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_migration import project_legacy_layers
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.layer import MaskShape
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
    ("lpw", "rect", 1),
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
    assert node.legacy_payload["id"] == node.node_id
    assert list(shapes[0]["center"]) == [0.5, 0.5]  # 默认居中（freeze 元组）
    assert node.legacy_payload.get("grading_params") in (None, {})  # 中性
    assert not node.label  # Resolve 新节点只显示编号与 Window 图标
    # 结构：新节点串在链上（有入边/出边）
    srcs = {e.src_node for e in result.graph.edges if e.dst_node == node.node_id}
    dsts = {e.dst_node for e in result.graph.edges if e.src_node == node.node_id}
    assert srcs and dsts
    assert any(layer["id"] == node.node_id for layer in
               project_legacy_layers(result.graph))


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
    polygon = by_name["Polygon"]
    assert len(polygon["points"]) == 4
    assert all(p["type"] == "corner" and p["pos"] == p["c1"] == p["c2"]
               for p in polygon["points"])
    assert tuple(by_name["Power Curve Window"]["points"]) == ()
    # 从图命令写入的几何可以交给生产 MaskShape 缓存/光栅化协议消费。
    restored = MaskShape.from_dict(dict(polygon))
    assert restored._get_render_cache_key(128, 72, 128)


def test_combo_defaults_do_not_share_mutable_geometry() -> None:
    graph = _graph()
    counter = iter(range(100))
    service = ColorGraphService(id_factory=lambda: f"x{next(counter)}")
    first = service.apply_intent(graph, "add_window_combo", {"combo": "ppw"})
    second = service.apply_intent(first.graph, "add_window_combo",
                                  {"combo": "ppw"})
    windows = [n.legacy_payload["mask_shapes"][0]
               for n in second.graph.nodes
               if n.node_type == "corrector"
               and any(s.get("name") == "Polygon" for s in
                       n.legacy_payload.get("mask_shapes", []))]
    assert len(windows) == 2
    assert windows[0]["points"] is not windows[1]["points"]
    assert tuple(windows[1]["points"][0]["pos"]) == (0.3, 0.3)
    assert ColorGraphService._WINDOW_COMBO_DEFAULTS["ppw"]["points"][0]["pos"][0] == 0.3


def test_linear_combo_has_resolve_rectangle_softness() -> None:
    result = ColorGraphService(id_factory=iter(["node", "window"]).__next__).apply_intent(
        _graph(), "add_window_combo", {"combo": "lpw"})
    assert result.ok
    node = next(n for n in result.graph.nodes if n.node_id == "node")
    window = node.legacy_payload["mask_shapes"][0]
    assert tuple(window["edge_softness"]) == (1.25,) * 4
    assert tuple(window["size"]) == (0.4, 0.4)
    assert MaskShape.from_dict(dict(window)).edge_softness == [1.25] * 4


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
