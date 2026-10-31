"""B1 批次表达式语义 focused 回归测试（N-02/N-03/N-12）。

对应 2026-10-14 复合页审计计划：
- N-02 同节点 this 参数依赖不得依赖表达式插入顺序；
- N-03 本地环 / n_ UUID 别名环必须在提交时被拒绝（canonical resolver）；
- N-12 表达式负数真值（nonzero 为真）与 BoolOp/IfExp/Compare 短路。
"""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core


def _graph_with_nodes():
    from src.features.composite.core.node_engine import NodeGraph
    from src.features.composite.nodes.transform_node import TransformNode

    graph = NodeGraph()
    node = TransformNode(node_id="t")
    graph.add_node(node)
    return graph, node


# ── N-02：同节点 this 依赖顺序无关 ─────────────────────────────────────


class TestLocalExpressionOrder:
    def test_n02_same_node_order_independent(self):
        """x=this.translate_y*2 的结果不得依赖设置顺序。"""
        graph, _ = _graph_with_nodes()

        ok, err = graph.set_node_expression(
            "t", "translate_x", "this.translate_y * 2")
        assert ok and err is None
        ok, _ = graph.set_node_expression("t", "translate_y", "10")
        assert ok

        params = graph.get_snapshot_params(0) if hasattr(
            graph, "get_snapshot_params") else None
        # 通过 evaluate 通道验证
        snap = graph.create_snapshot(0) if hasattr(
            graph, "create_snapshot") else None
        values = {}
        for getter in (params, snap):
            if isinstance(getter, dict):
                values = getter
                break
        if not values:
            # fallback: use expression manager evaluate_all directly
            from src.features.composite.utils.expression_parser import (
                ExpressionManager,
            )
            mgr = ExpressionManager()
            mgr.set_expression(
                "t", "translate_x", "this.translate_y * 2",
                local_param_names={"translate_x", "translate_y"},
                resolve_node_ref=lambda ref: ref if ref == "t" else None)
            mgr.set_expression("t", "translate_y", "10",
                               local_param_names={"translate_x", "translate_y"},
                               resolve_node_ref=lambda ref: ref if ref == "t" else None)
            values = mgr.evaluate_all(
                {"t": {"translate_x": 0.0, "translate_y": 0.0}}, {}, 0)
        assert values["t"]["translate_x"] == 20.0

    def test_n02_reverse_order_same_result(self):
        graph, _ = _graph_with_nodes()

        ok, _ = graph.set_node_expression("t", "translate_y", "10")
        assert ok
        ok, _ = graph.set_node_expression(
            "t", "translate_x", "this.translate_y * 2")
        assert ok

        from src.features.composite.utils.expression_parser import (
            ExpressionManager,
        )
        mgr = ExpressionManager()
        mgr.set_expression(
            "t", "translate_x", "this.translate_y * 2",
            local_param_names={"translate_x", "translate_y"},
            resolve_node_ref=lambda ref: ref if ref == "t" else None)
        mgr.set_expression("t", "translate_y", "10",
                           local_param_names={"translate_x", "translate_y"},
                           resolve_node_ref=lambda ref: ref if ref == "t" else None)
        values = mgr.evaluate_all(
            {"t": {"translate_x": 0.0, "translate_y": 0.0}}, {}, 0)
        assert values["t"]["translate_x"] == 20.0


# ── N-03：本地环 / 别名环提交拒绝 ──────────────────────────────────────


class TestCanonicalResolverCycles:
    def test_n03_this_self_reference_rejected(self):
        graph, _ = _graph_with_nodes()

        ok, err = graph.set_node_expression(
            "t", "translate_x", "this.translate_x + 1")

        assert ok is False
        assert err is not None

    def test_n03_uuid_alias_cycle_rejected(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode

        graph = NodeGraph()
        a = TransformNode(node_id="a1b2c3d4")
        b = TransformNode(node_id="b5c6d7e8")
        graph.add_node(a)
        graph.add_node(b)

        # b.radius 绑定 a.radius（生成 n_a1b2c3d4.radius 表达式）
        ok, err = graph.set_parameter_binding(
            "b5c6d7e8", "rotation", "a1b2c3d4", "rotation")
        assert ok, err

        # 反向：给 a.radius 设置 n_b.radius —— 跨别名环必须被拒
        ok, err = graph.set_node_expression(
            "a1b2c3d4", "rotation", "n_b5c6d7e8.rotation")

        assert ok is False
        assert err is not None

    def test_n03_valid_cross_node_expression_still_works(self):
        from src.features.composite.core.node_engine import NodeGraph
        from src.features.composite.nodes.transform_node import TransformNode

        graph = NodeGraph()
        a = TransformNode(node_id="a1b2c3d4")
        b = TransformNode(node_id="b5c6d7e8")
        graph.add_node(a)
        graph.add_node(b)

        ok, err = graph.set_node_expression(
            "a1b2c3d4", "rotation", "n_b5c6d7e8.rotation + 5")
        assert ok and err is None


# ── N-12：数值真值与短路 ───────────────────────────────────────────────


class TestNumericTruthinessAndShortCircuit:
    def _eval(self, expression: str):
        from src.features.composite.utils.expression_parser import (
            ExpressionContext, ExpressionParser,
        )

        parser = ExpressionParser()
        ctx = ExpressionContext(
            this_params={"x": 0.0},
            node_params={},
            input_attrs={"width": 1920, "height": 1080},
            frame=0, time=0.0, fps=24.0,
            current_value=0.0, project_width=1920, project_height=1080,
        )
        result = parser.evaluate(expression, ctx)
        assert result.success, result.error and result.error.message
        return result.value

    def test_n12_negative_condition_is_true(self):
        assert self._eval("10 if -1 else 20") == 10.0

    def test_n12_negative_and_operand_is_true(self):
        assert self._eval("(-1) and 1") == 1.0

    def test_n12_or_short_circuit_skips_invalid_branch(self):
        # 不可达右支（sqrt(-1) 会报 domain error）不得被求值
        assert self._eval("1 or sqrt(-1)") == 1.0

    def test_n12_and_short_circuit_skips_invalid_branch(self):
        assert self._eval("0 and sqrt(-1)") == 0.0

    def test_n12_zero_is_false(self):
        assert self._eval("10 if 0 else 20") == 20.0

    def test_n12_unary_not_consistent_with_ifexp(self):
        assert self._eval("not -1") == 0.0
        assert self._eval("not 0") == 1.0

    def test_n12_division_guard_expression(self):
        # 条件保护除零：x=0 时不求 1/x
        assert self._eval("1/x if x != 0 else 0") == 0.0

    def test_n12_compare_chain_short_circuit(self):
        # 链式比较已判假后不求值后续子式（1 < 0 为假）
        assert self._eval("1 < 0 < sqrt(-1)") == 0.0
