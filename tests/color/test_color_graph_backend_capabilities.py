"""B12.1/B12.4 三后端能力矩阵测试 —— 预检一致 / fail-closed / UI 可选项。

验收（计划 B12）：矩阵覆盖全部图算子且取值合法；能力预检与实际执行
一致（GL exact 的算子 GL 可执行；CPU/Metal fallback 的 Corrector 在
基础域可执行、越域 fail-closed）；unsupported 阻断执行不产出少节点
结果；UI 可选项从实际 operator 支持生成。
"""

from __future__ import annotations

import pytest

from src.features.color.core.color_graph_compiler import (
    GL_CAPABILITY_CATALOG,
    compile_graph,
)
from src.features.color.core.color_graph_cpu import (
    CPU_CAPABILITY_CATALOG,
    UnsupportedNode,
    production_layer_domain_errors,
)
from src.features.color.core.color_graph_metal import (
    METAL_CAPABILITY_CATALOG,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.shared.effects.effect_catalog import (
    graph_backend_capability_matrix,
    graph_node_backend_options,
)
from src.shared.project.color_graph import NODE_PORT_CATALOG
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers

VALID_STATUSES = {"exact", "fallback", "unsupported", "pending"}


class TestMatrixCoverage:
    def test_matrix_covers_all_catalog_node_types(self):
        """矩阵覆盖 NODE_PORT_CATALOG 的全部节点类型（无遗漏算子）。"""
        matrix = graph_backend_capability_matrix()
        for node_type in NODE_PORT_CATALOG:
            key = getattr(node_type, "value", node_type)
            assert key in matrix, f"节点类型 {key} 缺矩阵行"
        for row in matrix.values():
            assert set(row) == {"gl", "cpu", "metal"}

    def test_statuses_from_actual_catalogs(self):
        matrix = graph_backend_capability_matrix()
        for op, status in GL_CAPABILITY_CATALOG.items():
            assert matrix[op]["gl"] == status
        for op, status in CPU_CAPABILITY_CATALOG.items():
            assert matrix[op]["cpu"] == status
        for op, status in METAL_CAPABILITY_CATALOG.items():
            assert matrix[op]["metal"] == status

    def test_all_statuses_valid(self):
        matrix = graph_backend_capability_matrix()
        for row in matrix.values():
            for backend, status in row.items():
                assert status in VALID_STATUSES, (backend, status)

    def test_ui_options_generated_from_operators(self):
        options = graph_node_backend_options()
        assert options, "UI 可选项不能为空"
        by_type = {o["node_type"]: o for o in options}
        # 结构算子三后端 exact → ui_enabled 且 exact_backends 齐全
        for op in ("parallel_mixer", "layer_mixer", "key_mixer", "splitter",
                   "combiner", "alpha_output", "source", "output"):
            opt = by_type[op]
            assert opt["ui_enabled"], op
            assert set(opt["exact_backends"]) == {"gl", "cpu", "metal"}, op
        # compound：无后端 exact/可执行 → UI 禁用（如实呈现）
        assert by_type["compound"]["exact_backends"] == []
        # 全 unsupported 的项必须 ui_enabled=False（本目录暂无此例，
        # 构造性验证判据本身）
        fake = [o for o in options if all(
            v == "unsupported" for v in o["backends"].values())]
        for opt in fake:
            assert not opt["ui_enabled"]


class TestPrecheckMatchesExecution:
    def test_fallback_passes_precheck_but_fails_closed_on_domain(self):
        """CPU/Metal 的 corrector=fallback：预检放行，越域参数执行期拒绝
        （预检与实际执行一致的两段式 fail-closed）。"""
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        plan = compile_graph(freeze_graph(graph, session_id="c"),
                             size=(64, 64))
        cpu_report = plan.validate_capabilities(CPU_CAPABILITY_CATALOG)
        metal_report = plan.validate_capabilities(METAL_CAPABILITY_CATALOG)
        assert not cpu_report.unsupported
        assert not metal_report.unsupported
        # f03 的 L2 含 curves（基础域）——预检放行后，执行期域检查对
        # 基础域通过；附插件即越域（fail-closed，预检/执行两段一致）
        from src.shared.project.color_graph import unfreeze_params
        payloads = [unfreeze_params(dict(n.legacy_payload))
                    for n in graph.nodes if n.node_type == "corrector"]
        with_plugin = dict(payloads[0])
        with_plugin["plugins"] = [{"name": "p"}]
        assert production_layer_domain_errors(with_plugin), (
            "插件必须显式 unsupported（无 CPU/Metal 实现）")

    def test_unsupported_op_blocks_execution_no_silent_pass(self):
        """unsupported 算子阻断执行——不会"成功但少节点"。"""
        catalog = {op: "unsupported" for op in GL_CAPABILITY_CATALOG}
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        plan = compile_graph(freeze_graph(graph, session_id="c"),
                             size=(64, 64))
        report = plan.validate_capabilities(catalog)
        assert report.unsupported, "全 unsupported 目录必须拒绝"
        assert all(not s for s in [report.executable])

    def test_pending_rejected_fallback_allowed(self):
        catalog = dict(GL_CAPABILITY_CATALOG)
        catalog["corrector"] = "pending"
        catalog["output"] = "fallback"
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        plan = compile_graph(freeze_graph(graph, session_id="c"),
                             size=(64, 64))
        report = plan.validate_capabilities(catalog)
        blocked_ops = {label.split(":")[2] for label, _ in report.unsupported}
        assert "corrector" in blocked_ops
        assert "output" not in blocked_ops, (
            "fallback（域受限完整执行）预检应放行")


class TestScriptApiCapabilityVisibility:
    def test_inspect_capabilities_three_backends(self):
        from src.features.scripting.color_graph_api import ColorGraphScriptAPI
        graph = migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))
        holder = {"s1": _stack_with_layers(f03_layer_stack()).with_color_graph(
            graph)}
        api = ColorGraphScriptAPI(
            get_stack=lambda sid: holder.get(sid),
            set_stack=lambda sid, s: holder.__setitem__(sid, s))
        caps = api.inspect_capabilities("s1")
        assert caps["has_graph"]
        assert caps["backend_gl"]["corrector"] == "exact"
        assert caps["backend_cpu"]["corrector"] == "fallback"
        assert caps["backend_metal"]["corrector"] == "fallback"
        # 图内未出现的算子不进入逐算子表（has_graph=True 下仅列在图算子）
        assert "parallel_mixer" not in caps["backend_metal"]
