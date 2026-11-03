"""N12: 选择性域粘贴（仅选定域、身份重建、undo 完整恢复）。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


def _graph():
    project, sid = EditProject.create(name="n12").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def test_paste_only_curves_leaves_lut_and_window() -> None:
    service = ColorGraphService(
        effect_schema_provider=lambda rid: [],
        lut_reference_validator=lambda ref: ref.startswith("topos-res://"))
    graph = _graph()
    nodes = [n for n in graph.nodes if n.node_type == "corrector"]
    source, target = nodes[0], nodes[1]
    # 源节点持 LUT + 窗口；目标持旧 curves
    lutted = service.apply_intent(graph, "set_node_lut", {
        "node_id": source.node_id, "lut_ref": "topos-res://lut_x",
        "strength": 0.6})
    assert lutted.ok
    payload = {
        "grade": {"exposure": 42.0},
        "curves": {"points": [[0, 0], [1, 1]]},
        "lut": {"lut_ref": "topos-res://lut_x", "lut_strength": 0.6},
        "window": [{"id": "win-src", "type": "circle"}],
        "effects": [{"instance_id": "src-inst", "plugin_id": "grain",
                     "params": {}, "enabled": True}],
    }
    result = service.apply_intent(lutted.graph, "paste_selected_domains", {
        "node_ids": [target.node_id], "payload": payload,
        "domains": ["curves"]})
    assert result.ok, result.diagnostics
    node = next(n for n in result.graph.nodes if n.node_id == target.node_id)
    # 仅 curves 粘贴：grade/LUT/窗口未动
    curves = dict(node.legacy_payload.get("curves_data") or {})
    pasted_points = [list(p) for p in curves.get("points", [])]
    assert pasted_points == [[0, 0], [1, 1]]  # 冻结载荷形态（元组化）
    assert not node.params.get("resources", {}).get("lut_ref")
    assert not (node.legacy_payload or {}).get("mask_shapes")
    assert (node.params.get("grade") or {}) == (target.params.get("grade") or {})


def test_effects_paste_rebuilds_instance_identity() -> None:
    service = ColorGraphService(effect_schema_provider=lambda rid: [])
    graph = _graph()
    target = next(n for n in graph.nodes if n.node_type == "corrector")
    payload = {"effects": [{"instance_id": "original", "plugin_id": "grain",
                            "params": {"amount": 0.3}, "enabled": True}]}
    result = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id], "payload": payload,
        "domains": ["effects"]})
    assert result.ok
    node = next(n for n in result.graph.nodes if n.node_id == target.node_id)
    plugins = node.legacy_payload["plugins"]
    assert len(plugins) == 1
    assert plugins[0]["instance_id"] != "original"  # 身份重建
    assert plugins[0]["params"] == {"amount": 0.3}   # 参数保留
    # 窗口身份同样重建
    result = service.apply_intent(result.graph, "paste_selected_domains", {
        "node_ids": [target.node_id],
        "payload": {"window": [{"id": "w1", "type": "rect"}]},
        "domains": ["window"]})
    node = next(n for n in result.graph.nodes if n.node_id == target.node_id)
    assert node.legacy_payload["mask_shapes"][0]["id"] != "w1"


def test_locked_target_rejected_and_unknown_domain_rejected() -> None:
    service = ColorGraphService(effect_schema_provider=lambda rid: [])
    graph = _graph()
    target = next(n for n in graph.nodes if n.node_type == "corrector")
    bad = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id], "payload": {"grade": {}},
        "domains": ["quantum"]})
    assert not bad.ok and bad.primary_code == "illegal_param_key"
    from dataclasses import replace as _r
    locked = _r(graph, nodes=tuple(
        _r(n, params={**n.params, "locked": True})
        if n.node_id == target.node_id else n for n in graph.nodes))
    rejected = service.apply_intent(locked, "paste_selected_domains", {
        "node_ids": [target.node_id], "payload": {"grade": {"exposure": 1}},
        "domains": ["grade"]})
    assert not rejected.ok and rejected.primary_code == "node_locked"
