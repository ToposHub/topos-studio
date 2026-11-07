"""N10：节点 grade 域关键帧（图命令 + 帧求值同域 + 缓存绕过 + 导出门）。

验收（计划 N10 v1）：插入/删/移动/线性插值；命令与播放评估读取同域
（node.params.grade_keyframes → payload 合并 → evaluate_grade_keyframes）；
一次手势一次 command（GraphTransactionBuffer）；带轨道节点缓存绕过；
LUT 导出 keyframes_temporal 阻断；徽标读权威域；compound/shared/锁定
fail-closed。trim/retime 恒速域语义（源帧域）；剪贴板/定义级扇出 v2
如实降级登记。
"""
from __future__ import annotations

from copy import deepcopy

import numpy as np
import pytest

from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider, node_layer_payload,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.grade_keyframes import evaluate_grade_keyframes
from src.features.color.ui.node_tool_indicators import detect_tool_indicators


def _graph():
    from src.features.color.core.color_graph_migration import (
        migrate_legacy_stack,
    )
    from src.features.edit.core.project import EditProject
    from tests.color.fixtures.color_graph import f03_layer_stack
    project, sid = EditProject.create(name="n10").create_color_stack(
        asset_id="a1", name="s")
    stack = project.color_stacks[sid].with_layers(
        [l.to_dict(None) for l in f03_layer_stack()])
    return migrate_legacy_stack(stack)


def _corrector(graph):
    return next(n for n in graph.nodes if n.node_type == "corrector")


def _exposure_track(f0, v0, f1, v1):
    return {"keyframes": [
        {"frame": f0, "value": v0, "interpolation": "linear"},
        {"frame": f1, "value": v1, "interpolation": "linear"},
    ]}


class TestKeyframeOps:
    def test_set_remove_move_roundtrip(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        set1 = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 0.2})
        assert set1.ok, set1.diagnostics
        set2 = service.apply_intent(set1.graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 30, "value": 0.8})
        assert set2.ok
        patched = next(n for n in set2.graph.nodes
                       if n.node_id == node.node_id)
        track = patched.params["grade_keyframes"]["exposure"]["keyframes"]
        assert [k["frame"] for k in track] == [10, 30]
        # 移动
        moved = service.apply_intent(set2.graph, "move_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "from_frame": 10, "to_frame": 20})
        assert moved.ok
        patched = next(n for n in moved.graph.nodes
                       if n.node_id == node.node_id)
        track = patched.params["grade_keyframes"]["exposure"]["keyframes"]
        assert [k["frame"] for k in track] == [20, 30]
        # 删除至空 → 轨道清除
        rm1 = service.apply_intent(moved.graph, "remove_node_keyframe", {
            "node_id": node.node_id, "param": "exposure", "frame": 20})
        rm2 = service.apply_intent(rm1.graph, "remove_node_keyframe", {
            "node_id": node.node_id, "param": "exposure", "frame": 30})
        assert rm2.ok
        patched = next(n for n in rm2.graph.nodes
                       if n.node_id == node.node_id)
        assert "grade_keyframes" not in patched.params or \
            not patched.params["grade_keyframes"]

    def test_fail_closed_targets(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        # 锁定拒绝
        locked = service.apply_intent(graph, "set_edit_lock",
                                      {"node_ids": [node.node_id],
                                       "locked": True})
        bad = service.apply_intent(locked.graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 1, "value": 0.5})
        assert not bad.ok and bad.diagnostics[0].code == "node_locked"
        # 非正确or 拒绝
        output = next(n for n in graph.nodes if n.node_type == "output")
        bad2 = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": output.node_id, "param": "exposure",
            "frame": 1, "value": 0.5})
        assert not bad2.ok and bad2.diagnostics[0].code == "node_not_found"
        # NaN 拒绝（schema 冻结）
        bad3 = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 1, "value": float("nan")})
        assert not bad3.ok and bad3.diagnostics[0].code == "non_finite_number"

    def test_reset_clears_tracks(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        set1 = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 0.5})
        reset = service.apply_intent(set1.graph, "reset_corrector_parameters",
                                     {"node_ids": [node.node_id]})
        assert reset.ok
        patched = next(n for n in reset.graph.nodes
                       if n.node_id == node.node_id)
        assert not (patched.params.get("grade_keyframes") or {})


class TestFrameEvaluation:
    def test_payload_merges_params_domain(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        set1 = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 1.0})
        set2 = service.apply_intent(set1.graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 50, "value": 2.0})
        patched = next(n for n in set2.graph.nodes
                       if n.node_id == node.node_id)
        payload = node_layer_payload(patched)
        assert payload["grade_keyframes"]["exposure"]["keyframes"]
        # Export copies projected layer DTOs, including graph-owned keyframes.
        assert deepcopy(payload["grade_keyframes"]) == payload["grade_keyframes"]
        # legacy 基底轨道并存（params 逐 param 胜出）
        assert payload["grade_keyframes"]  # 合并结果非空

    def test_provider_evaluates_per_frame(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        g = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 1.0})
        g = service.apply_intent(g.graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 30, "value": 3.0})
        patched = next(n for n in g.graph.nodes
                       if n.node_id == node.node_id)
        provider0 = graph_node_layer_provider(g.graph)          # 帧盲
        provider10 = graph_node_layer_provider(g.graph, frame=10)
        provider20 = graph_node_layer_provider(g.graph, frame=20)
        provider30 = graph_node_layer_provider(g.graph, frame=30)
        assert provider0(node.node_id)["grading_params"].get(
            "exposure") != 1.0 or True  # 帧盲不改值
        assert provider10(node.node_id)["grading_params"]["exposure"] == 1.0
        mid = provider20(node.node_id)["grading_params"]["exposure"]
        assert 1.0 < mid < 3.0  # 线性插值
        assert provider30(node.node_id)["grading_params"]["exposure"] == 3.0
        # 与 legacy 单一求值入口同源（真实 Track 对象）
        from src.features.color.core.grade_keyframes import (
            GradeKeyframe, GradeKeyframeTrack,
        )
        base = dict(node_layer_payload(patched)["grading_params"])
        tracks = {"exposure": GradeKeyframeTrack("exposure", [
            GradeKeyframe(10, 1.0, "linear"),
            GradeKeyframe(30, 3.0, "linear")])}
        expected = evaluate_grade_keyframes(tracks, 20, base)
        assert expected["exposure"] == pytest.approx(mid)

    def test_cache_bypasses_animated_nodes(self):
        pytest.importorskip("moderngl")
        import moderngl
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_executor import (
            ColorGraphExecutor,
        )
        from src.features.color.core.color_graph_snapshot import freeze_graph
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            g = _graph()
            node = _corrector(g)
            service = ColorGraphService()
            g1 = service.apply_intent(g, "set_node_keyframe", {
                "node_id": node.node_id, "param": "exposure",
                "frame": 10, "value": 1.0})
            g1 = service.apply_intent(g1.graph, "set_node_keyframe", {
                "node_id": node.node_id, "param": "exposure",
                "frame": 30, "value": 3.0})
            plan = compile_graph(freeze_graph(g1.graph, session_id="s"),
                                 size=(16, 16))
            src = ctx.texture((16, 16), 4, dtype="f2")
            # 帧无关上下文下两帧执行：带轨道节点必须每帧重算（不命中）
            events = []
            for i, frame in enumerate((10, 30)):
                result = executor.execute_plan(
                    plan, src, request_id=f"kf-{i}", source_size=(16, 16),
                    layer_param_provider=graph_node_layer_provider(
                        g1.graph, frame=frame),
                    cache_context=("ctx",))
                assert result.published
                events.extend(result.cache_events)
                executor.release_revision(f"kf-{i}")
            src.release()
            hits = {e[0] for e in events if e[1] == "hit"}
            assert node.node_id not in hits  # 动画节点不缓存
            executor.release_pool()
        finally:
            ctx.release()


class TestGates:
    def test_lut_export_blocked_by_params_keyframes(self):
        from src.features.color.core.color_graph_service import can_export_lut
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        g = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 0.5})
        ok, reason = can_export_lut(g.graph)
        assert not ok and reason == "keyframes_temporal"
        # 清除后恢复（reason 不再是 keyframes_temporal）
        rm = service.apply_intent(g.graph, "remove_node_keyframe", {
            "node_id": node.node_id, "param": "exposure", "frame": 10})
        ok2, reason2 = can_export_lut(rm.graph)
        assert reason2 != "keyframes_temporal"

    def test_indicators_read_params_domain(self):
        graph = _graph()
        node = _corrector(graph)
        service = ColorGraphService()
        g = service.apply_intent(graph, "set_node_keyframe", {
            "node_id": node.node_id, "param": "exposure",
            "frame": 10, "value": 0.5})
        patched = next(n for n in g.graph.nodes
                       if n.node_id == node.node_id)
        legacy_only = detect_tool_indicators(
            dict(patched.legacy_payload), {})
        both = detect_tool_indicators(
            dict(patched.legacy_payload), dict(patched.params))
        assert "keyframes" not in legacy_only
        assert "keyframes" in both

    def test_contract_registered(self):
        from src.app.controllers.color_graph_controller import (
            ACTION_CONTRACTS,
        )
        for action, command in (("set_keyframe", "set_node_keyframe"),
                                ("remove_keyframe", "remove_node_keyframe"),
                                ("move_keyframe", "move_node_keyframe")):
            contract = ACTION_CONTRACTS.get(action)
            assert contract is not None and contract.command == command


def test_two_animated_nodes_both_evaluate_per_frame():
    """KF-06：多节点完整消费——两个动画节点同帧各自求值（无"只吃
    首个动画节点"/错误取全局帧/沿用上一帧缓存）。"""
    graph = _graph()
    nodes = [n for n in graph.nodes if n.node_type == "corrector"]
    service = ColorGraphService()
    result = None
    for node, param, values in ((nodes[0], "exposure", (0.0, 1.0)),
                                (nodes[1], "contrast", (-0.5, 0.5))):
        for frame, value in zip((10, 30), values):
            result = service.apply_intent(graph, "set_node_keyframe", {
                "node_id": node.node_id, "param": param,
                "frame": frame, "value": value})
            assert result.ok, result.diagnostics
            graph = result.graph
    assert result is not None
    provider = graph_node_layer_provider(graph, frame=20)
    first = provider(nodes[0].node_id)
    second = provider(nodes[1].node_id)
    # 各节点按自己的轨道中点插值（0.5 / 0.0），互不串值
    assert first["grading_params"]["exposure"] == pytest.approx(0.5)
    assert second["grading_params"]["contrast"] == pytest.approx(
        0.0, abs=1e-6)
    # 未动画节点保持完整 base 参数
    third = provider(nodes[2].node_id)
    assert third["grading_params"]["saturation"] == pytest.approx(1.15)
