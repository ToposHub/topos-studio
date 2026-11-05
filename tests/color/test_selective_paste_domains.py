"""N12: 选择性域粘贴（仅选定域、身份重建、undo 完整恢复）。"""
from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.edit.core.project import EditProject
from tests.color.fixtures.color_graph import f03_layer_stack


@pytest.fixture
def n12b_graph():
    """单 corrector 图 + 目标节点（N12-B 用例）。"""
    graph = _graph()
    target = next(n for n in graph.nodes if n.node_type == "corrector")
    return graph, target


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


# ═══ N12-B：域表扩全 + 资源校验 + 缺插件占位 ═════════════════════

def _n12b_service(tmp_path=None):
    """lut/bake 校验接 fake 会话（registered 白名单）。"""
    return ColorGraphService(
        effect_schema_provider=lambda rid: (
            [{"var_name": "amount", "type": "slider", "default": 0.2,
              "min": 0.0, "max": 1.0}] if rid == "known_fx" else None),
        lut_reference_validator=lambda ref: ref == "topos-res://lut_ok",
        bake_reference_validator=lambda ref: ref == "topos-res://bake_ok")


def test_new_domains_paste_and_leave_others(n12b_graph):
    """扩表域（qualifier/soft_clip/mixer/hsl/keyframes）只写选定域。"""
    service = _n12b_service()
    graph, target = n12b_graph
    payload = {
        "grade": {"exposure": 9.0},
        "qualifier": {"enabled": True, "lum_low": 0.2},
        "soft_clip": {"enabled": True, "knee": 0.5},
        "mixer": {"red": 1.1},
        "hsl": {"sat_sat": [[0, 0], [1, 1]]},
        "keyframes": {"exposure": {"keyframes": [
            {"frame": 0, "value": 1.0, "interpolation": "linear"}]}},
    }
    result = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id], "payload": payload,
        "domains": ["qualifier", "soft_clip", "mixer", "hsl", "keyframes"]})
    assert result.ok, result.diagnostics
    node = next(n for n in result.graph.nodes if n.node_id == target.node_id)
    assert node.params["qualifier"] == {"enabled": True, "lum_low": 0.2}
    assert (node.legacy_payload.get("soft_clip_params") or {}).get("knee") == 0.5
    assert (node.legacy_payload.get("mixer_params") or {}).get("red") == 1.1
    assert node.params.get("grade_keyframes")
    # 未选 grade 域 → 原样
    assert (node.params.get("grade") or {}) != {"exposure": 9.0}


def test_unregistered_lut_ref_rejected_fail_visible(n12b_graph):
    """跨工程/缺资源：未注册 LUT 引用粘贴即拒（resource_unknown）。"""
    service = _n12b_service()
    graph, target = n12b_graph
    result = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id],
        "payload": {"lut": {"lut_ref": "topos-res://lut_missing"}},
        "domains": ["lut"]})
    assert not result.ok
    assert result.diagnostics[0].code == "resource_unknown"


def test_bake_ref_domain_pastes_and_validates(n12b_graph):
    """bake 域：注册引用粘贴成功；未注册拒绝。"""
    service = _n12b_service()
    graph, target = n12b_graph
    ok = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id],
        "payload": {"bake": {"bake_ref": "topos-res://bake_ok"}},
        "domains": ["bake"]})
    assert ok.ok, ok.diagnostics
    node = next(n for n in ok.graph.nodes if n.node_id == target.node_id)
    assert node.params["resources"]["bake_ref"] == "topos-res://bake_ok"
    rejected = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id],
        "payload": {"bake": {"bake_ref": "topos-res://bake_missing"}},
        "domains": ["bake"]})
    assert not rejected.ok
    assert rejected.diagnostics[0].code == "resource_unknown"


def test_missing_plugin_kept_with_placeholder(n12b_graph):
    """缺插件：实例保留 + missing 占位（不静默丢弃）。"""
    service = _n12b_service()
    graph, target = n12b_graph
    result = service.apply_intent(graph, "paste_selected_domains", {
        "node_ids": [target.node_id],
        "payload": {"effects": [
            {"plugin_id": "known_fx", "params": {"amount": 0.3},
             "enabled": True},
            {"plugin_id": "not_installed", "params": {}, "enabled": True}]},
        "domains": ["effects"]})
    assert result.ok, result.diagnostics
    node = next(n for n in result.graph.nodes if n.node_id == target.node_id)
    plugins = node.legacy_payload["plugins"]
    by_id = {p["plugin_id"]: p for p in plugins}
    assert "missing" not in by_id["known_fx"]
    assert by_id["not_installed"].get("missing") is True
    assert by_id["not_installed"]["plugin_id"] == "not_installed"  # 保留
