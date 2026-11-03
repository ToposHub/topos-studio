"""N03：非 primary 工具域（Qualifier / Window）的图命令。

验收（计划 N03）：Tracker/Qualifier/窗口工具参数**实际落正确 node**
（typed handle 写入，不按 root active_index 猜）；本文件覆盖服务端 op
语义——merge patch、嵌套群组定位、身份稳定、结构整体替换、schema
安全（路径只在 legacy 载体豁免）、compound 下行。UI 路由另有
tests/app/test_tool_domain_routing.py。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import node_layer_payload
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph():
    project, sid = EditProject.create(name="n03").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def _corrector(graph):
    return next(n for n in graph.nodes if n.node_type == "corrector")


# ── Qualifier 域 ─────────────────────────────────────────────────────────


def test_set_node_qualifier_merges_and_projects() -> None:
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService()
    first = service.apply_intent(graph, "set_node_qualifier", {
        "node_id": node.node_id,
        "qualifier": {"enabled": True, "hue_center": 0.1}})
    assert first.ok, first.diagnostics
    second = service.apply_intent(first.graph, "set_node_qualifier", {
        "node_id": node.node_id,
        "qualifier": {"hue_width": 0.2}})  # 手势只 patch 变化键
    assert second.ok, second.diagnostics
    patched = next(n for n in second.graph.nodes
                   if n.node_id == node.node_id)
    assert dict(patched.params["qualifier"]) == {
        "enabled": True, "hue_center": 0.1, "hue_width": 0.2}
    # 投影：params.qualifier 并入 legacy qualifier_params（覆盖同名键）
    payload = node_layer_payload(patched)
    assert payload["qualifier_params"]["enabled"] is True
    assert payload["qualifier_params"]["hue_width"] == pytest.approx(0.2)
    assert patched.params["qualifier"] is not None


def test_set_node_qualifier_rejects_non_corrector_and_lock() -> None:
    graph = _graph()
    service = ColorGraphService()
    output = next(n for n in graph.nodes if n.node_type == "output")
    bad = service.apply_intent(graph, "set_node_qualifier", {
        "node_id": output.node_id, "qualifier": {"enabled": True}})
    assert not bad.ok and bad.diagnostics[0].code == "node_not_found"
    node = _corrector(graph)
    locked = service.apply_intent(graph, "set_edit_lock",
                                  {"node_ids": [node.node_id], "locked": True})
    assert locked.ok
    rejected = service.apply_intent(locked.graph, "set_node_qualifier", {
        "node_id": node.node_id, "qualifier": {"enabled": True}})
    assert not rejected.ok and rejected.diagnostics[0].code == "node_locked"


# ── Window 域 ────────────────────────────────────────────────────────────


def _shapes_with_group(shape_id: str = "shape-1"):
    return [
        {"id": "group-1", "type": "group", "name": "G", "opacity": 1.0,
         "visible": True, "children": [
             {"id": shape_id, "type": "circle", "name": "C",
              "center": [0.5, 0.5], "size": [0.2, 0.2], "rotation": 0.0,
              "feather": 2.0, "opacity": 1.0, "visible": True,
              "invert": False, "operation": "add"}]},
        {"id": "shape-2", "type": "rect", "name": "R",
         "center": [0.3, 0.3], "size": [0.1, 0.1]},
    ]


def test_set_node_window_shapes_replace_and_identity() -> None:
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService(id_factory=lambda: "fresh-id")
    result = service.apply_intent(graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": _shapes_with_group()})
    assert result.ok, result.diagnostics
    patched = next(n for n in result.graph.nodes
                   if n.node_id == node.node_id)
    shapes = patched.legacy_payload["mask_shapes"]
    assert shapes[0]["id"] == "group-1"          # 给定 id 保留
    assert shapes[0]["children"][0]["id"] == "shape-1"
    # 缺 id 的项 → 分配新身份
    no_id = service.apply_intent(result.graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": [{"type": "rect", "center": [0, 0]}]})
    assert no_id.ok
    fresh = next(n for n in no_id.graph.nodes if n.node_id == node.node_id)
    assert fresh.legacy_payload["mask_shapes"][0]["id"] == "fresh-id"
    # 清空
    cleared = service.apply_intent(no_id.graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": []})
    assert cleared.ok
    empty = next(n for n in cleared.graph.nodes if n.node_id == node.node_id)
    assert list(empty.legacy_payload["mask_shapes"]) == []


def test_update_window_shape_patches_nested_group_child() -> None:
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": _shapes_with_group()})
    assert seeded.ok
    updated = service.apply_intent(seeded.graph, "update_node_window_shape", {
        "node_id": node.node_id, "shape_id": "shape-1",
        "attributes": {"center": [0.25, 0.75], "feather": 5.0}})
    assert updated.ok, updated.diagnostics
    patched = next(n for n in updated.graph.nodes
                   if n.node_id == node.node_id)
    child = patched.legacy_payload["mask_shapes"][0]["children"][0]
    assert list(child["center"]) == [0.25, 0.75]
    assert child["feather"] == pytest.approx(5.0)
    assert child["id"] == "shape-1"              # 身份不变（绑定/选中稳定）
    # 群组自身属性（opacity 白名单域）
    grouped = service.apply_intent(updated.graph, "update_node_window_shape", {
        "node_id": node.node_id, "shape_id": "group-1",
        "attributes": {"opacity": 0.5}})
    assert grouped.ok
    top = next(n for n in grouped.graph.nodes if n.node_id == node.node_id)
    assert top.legacy_payload["mask_shapes"][0]["opacity"] == pytest.approx(0.5)


def test_update_window_shape_unknown_and_bad_values() -> None:
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService()
    seeded = service.apply_intent(graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": _shapes_with_group()})
    missing = service.apply_intent(seeded.graph, "update_node_window_shape", {
        "node_id": node.node_id, "shape_id": "ghost",
        "attributes": {"feather": 1.0}})
    assert not missing.ok and missing.diagnostics[0].code == "shape_not_found"
    # NaN 经 schema 冻结拒绝（fail-closed，不产生半写状态）
    bad = service.apply_intent(seeded.graph, "update_node_window_shape", {
        "node_id": node.node_id, "shape_id": "shape-1",
        "attributes": {"feather": float("nan")}})
    assert not bad.ok and bad.diagnostics[0].code == "non_finite_number"


def test_window_shape_bitmap_path_only_in_legacy_carrier() -> None:
    """bitmap 引用只能走 legacy 载体（路径豁免=迁移语义，保存期闭包迁移）；
    params 域（qualifier）禁路径——schema 双载体分界。"""
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService()
    shapes = [{"id": "bmp-1", "type": "bitmap", "bitmap_file": "bmp-1.png",
               "center": [0.5, 0.5]}]
    result = service.apply_intent(graph, "set_node_window_shapes", {
        "node_id": node.node_id, "shapes": shapes})
    assert result.ok, result.diagnostics
    patched = next(n for n in result.graph.nodes
                   if n.node_id == node.node_id)
    assert patched.legacy_payload["mask_shapes"][0]["bitmap_file"] == "bmp-1.png"
    # legacy 载体豁免绝对路径（迁移期旧工程窗口位图；R09 式闭包在保存期迁移）
    legacy_abs = service.apply_intent(result.graph, "set_node_window_shapes", {
        "node_id": node.node_id,
        "shapes": [{"id": "b2", "bitmap_file": "/abs/path.png"}]})
    assert legacy_abs.ok
    # params 域（qualifier）带路径 → fail-closed
    bad = service.apply_intent(graph, "set_node_qualifier", {
        "node_id": node.node_id, "qualifier": {"lut": "/abs/x.cube"}})
    assert not bad.ok and bad.diagnostics[0].code == "absolute_path"


def test_tool_writes_descend_compound_path() -> None:
    graph = _graph()
    node = _corrector(graph)
    service = ColorGraphService()
    grouped = service.apply_intent(graph, "group_subgraph",
                                   {"member_ids": [node.node_id]})
    assert grouped.ok
    compound = next(n for n in grouped.graph.nodes
                    if n.node_type == "compound")
    result = service.apply_intent(grouped.graph, "set_node_qualifier", {
        "node_id": node.node_id, "qualifier": {"enabled": True},
        "_compound_path": (compound.node_id,)})
    assert result.ok, result.diagnostics
    compound_after = next(n for n in result.graph.nodes
                          if n.node_type == "compound")
    inner = compound_after.compound_children.nodes
    target = next(n for n in inner if n.node_id == node.node_id)
    assert target.params["qualifier"]["enabled"] is True
