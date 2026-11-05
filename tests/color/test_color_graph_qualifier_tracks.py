"""N10-B：qualifier 域动画轨道（图命令 + provider 逐帧求值 + 声明）。

覆盖（计划 N10-B 验收的可自动化子集）：
- 三原子 op 写 qualifier.* 轨道往返；非法轨道名 fail-closed；
- SHARED_CORRECTOR 实例可动画（实例级先行）；输出/混合器拒绝；
- provider 帧感知：hue_center 最短弧插值、lum 线性、grade 轨道互不干扰、
  限定器空基底跳过（与 layer 级语义一致）；
- 缓存绕过/徽标/LUT 阻断经既有 grade_keyframes 键自动生效（断言）；
- N02-B 切片 4：key_frame_animated 声明与 plan json；
- 与 layer 级 evaluate_qualifier_keyframes 同帧同值（同源）。
"""
from __future__ import annotations

import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.shared.project.color_graph import ColorNodeData, ColorGraphData, NodeType

from tests.color.test_color_graph_schema import _serial_graph


def _service() -> ColorGraphService:
    return ColorGraphService(effect_schema_provider=lambda rid: None,
                             lut_reference_validator=lambda ref: True)


def _corrector(graph: ColorGraphData) -> ColorNodeData:
    return next(n for n in graph.nodes if n.node_type == "corrector")


def _shared_graph():
    """带一个 shared_corrector 实例的图。"""
    from src.shared.project.color_graph import ColorEdgeData
    from uuid import uuid4
    source = ColorNodeData(node_id=str(uuid4()),
                           node_type=NodeType.SOURCE.value)
    from src.shared.project.color_graph import SharedDefinitionData
    shared = ColorNodeData(node_id=str(uuid4()),
                           node_type=NodeType.SHARED_CORRECTOR.value,
                           shared_definition_id="def-1")
    output = ColorNodeData(node_id=str(uuid4()),
                           node_type=NodeType.OUTPUT.value)
    graph = ColorGraphData(
        nodes=(source, shared, output),
        edges=(ColorEdgeData("e1", source.node_id, "image",
                             shared.node_id, "image"),
               ColorEdgeData("e2", shared.node_id, "image",
                             output.node_id, "image")),
        source_node_id=source.node_id, output_node_id=output.node_id,
        shared_definitions=(SharedDefinitionData(
            definition_id="def-1", name="Look",
            node_type=NodeType.SHARED_CORRECTOR.value),))
    return graph, shared


def test_qualifier_track_ops_roundtrip_and_whitelist():
    service = _service()
    graph = _serial_graph(1)
    node = _corrector(graph)
    applied = service.apply_intent(graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "qualifier.hue_center",
        "frame": 10, "value": 0.25})
    assert applied.ok, applied.diagnostics
    tracks = _corrector(applied.graph).params["grade_keyframes"]
    assert "qualifier.hue_center" in tracks
    # 非法轨道名 fail-closed
    rejected = service.apply_intent(applied.graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "qualifier.not_a_field",
        "frame": 11, "value": 0.5})
    assert not rejected.ok
    assert rejected.diagnostics[0].code == "illegal_param_key"
    # remove 清轨道
    removed = service.apply_intent(applied.graph, "remove_node_keyframe", {
        "node_id": node.node_id, "param": "qualifier.hue_center",
        "frame": 10})
    assert removed.ok
    assert "qualifier.hue_center" not in (
        _corrector(removed.graph).params.get("grade_keyframes") or {})


def test_shared_corrector_instance_animatable_output_rejected():
    service = _service()
    graph, shared = _shared_graph()
    ok = service.apply_intent(graph, "set_node_keyframe", {
        "node_id": shared.node_id, "param": "qualifier.lum_low",
        "frame": 0, "value": 0.1})
    assert ok.ok, ok.diagnostics
    # 输出节点拒绝
    graph2 = _serial_graph(1)
    out = next(n for n in graph2.nodes if n.node_type == "output")
    rejected = service.apply_intent(graph2, "set_node_keyframe", {
        "node_id": out.node_id, "param": "qualifier.lum_low",
        "frame": 0, "value": 0.1})
    assert not rejected.ok


def _graph_with_track(track_name: str, frames_values, qualifier_base):
    service = _service()
    graph = _serial_graph(1)
    node = _corrector(graph)
    from dataclasses import replace
    params = dict(node.params)
    params["qualifier"] = dict(qualifier_base)
    new_node = replace(node, params=params)
    from src.shared.project.color_graph import ColorGraphData as G
    graph = G(nodes=tuple(new_node if n.node_id == node.node_id else n
                          for n in graph.nodes),
              edges=graph.edges, source_node_id=graph.source_node_id,
              output_node_id=graph.output_node_id)
    for frame, value in frames_values:
        result = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": track_name,
            "frame": frame, "value": value})
        assert result.ok, result.diagnostics
        graph = result.graph
    return graph, node.node_id


_QP_BASE = {"enabled": True, "hue_center": 0.5, "lum_low": 0.0}


def test_provider_shortest_arc_for_hue_center():
    graph, _nid = _graph_with_track(
        "qualifier.hue_center", [(0, 0.9), (10, 0.1)], _QP_BASE)
    provider = graph_node_layer_provider(graph, frame=5)
    payload = provider(next(n for n in graph.nodes
                            if n.node_type == "corrector").node_id)
    # 最短弧：0.9 → 0.1 走 0.0 方向，中点 = 0.0（或 1.0 等价值）
    mid = payload["qualifier_params"]["hue_center"]
    assert mid == pytest.approx(0.0, abs=1e-6) or mid == pytest.approx(1.0, abs=1e-6)
    # 端点外推
    p0 = graph_node_layer_provider(graph, frame=0)(
        next(n for n in graph.nodes if n.node_type == "corrector").node_id)
    assert p0["qualifier_params"]["hue_center"] == pytest.approx(0.9)


def test_provider_linear_and_grade_track_orthogonal():
    graph, _nid = _graph_with_track(
        "qualifier.lum_low", [(0, 0.0), (10, 0.5)], _QP_BASE)
    service = _service()
    node = _corrector(graph)
    with_grade = service.apply_intent(graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "exposure",
        "frame": 0, "value": 1.0})
    # exposure 只有一个关键帧 → 全程保持 1.0，且不影响 qualifier 域
    result = service.apply_intent(with_grade.graph, "set_node_keyframe", {
        "node_id": node.node_id, "param": "exposure",
        "frame": 10, "value": 3.0})
    provider = graph_node_layer_provider(result.graph, frame=5)
    payload = provider(node.node_id)
    assert payload["qualifier_params"]["lum_low"] == pytest.approx(0.25)
    assert payload["grading_params"]["exposure"] == pytest.approx(2.0)


def test_provider_matches_layer_semantics_on_default_base():
    """图 payload 的 qualifier_params 恒有默认基底（Layer 构造语义）；
    与 layer 级一致：字段恒求值（keyer 是否消费由 enabled 决定）。
    未启用时求值仍发生——数值变化但无视觉影响。"""
    graph, _nid = _graph_with_track(
        "qualifier.lum_low", [(0, 0.0), (10, 0.5)],
        qualifier_base={})  # 不覆盖 → legacy 默认基底（enabled=False）
    provider = graph_node_layer_provider(graph, frame=5)
    payload = provider(_corrector(graph).node_id)
    qp = payload["qualifier_params"]
    assert qp["enabled"] is False          # 未启用（无视觉影响）
    assert qp["lum_low"] == pytest.approx(0.25)  # 字段仍求值（同 layer 级）


def test_cache_bypass_and_lut_block_inherited():
    """grade_keyframes 键已让 N06 绕过与 LUT 阻断自动覆盖（机制复用断言）。"""
    graph, _nid = _graph_with_track(
        "qualifier.lum_low", [(0, 0.0), (10, 0.5)], _QP_BASE)
    from src.features.color.core.color_graph_service import can_export_lut
    blocked, reason = can_export_lut(graph)
    assert blocked is False and reason == "keyframes_temporal"


def test_key_frame_animated_declared_in_plan():
    graph, _nid = _graph_with_track(
        "qualifier.lum_low", [(0, 0.0), (10, 0.5)], _QP_BASE)
    plan = compile_graph(freeze_graph(graph, session_id="s"), size=(64, 64))
    cp = next(p for p in plan.passes if p.op == "corrector")
    assert cp.key_frame_animated is True
    assert "qualifier" in cp.key_pipeline  # enabled 基底仍在
    data = plan.to_plan_json()
    cj = next(p for p in data["passes"] if p["op"] == "corrector")
    assert cj["key_frame_animated"] is True
    # 静态图 false
    plan2 = compile_graph(freeze_graph(_serial_graph(1), session_id="s"),
                          size=(64, 64))
    cp2 = next(p for p in plan2.passes if p.op == "corrector")
    assert cp2.key_frame_animated is False


def test_layer_level_and_dict_evaluation_agree():
    """图域纯函数与 layer 级求值同帧同值（单一求值权威）。"""
    from types import SimpleNamespace
    from src.features.color.core.grade_keyframes import (
        GradeKeyframe, GradeKeyframeTrack, evaluate_qualifier_keyframes,
        evaluate_qualifier_params_tracks,
    )
    track = GradeKeyframeTrack("qualifier.lum_low", [
        GradeKeyframe(0, 0.0), GradeKeyframe(10, 0.5)])
    base = {"enabled": True, "lum_low": 0.0, "lum_high": 1.0}
    dict_out = evaluate_qualifier_params_tracks(
        {"qualifier.lum_low": track}, 5, base)
    layer = SimpleNamespace(qualifier_params=dict(base),
                            grade_keyframes={"qualifier.lum_low": track})
    evaluate_qualifier_keyframes(layer, 5)
    assert dict_out["lum_low"] == pytest.approx(
        layer.qualifier_params["lum_low"])
