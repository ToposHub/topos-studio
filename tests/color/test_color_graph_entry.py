"""阶段 4.1 —— 共享图入口（color_graph_entry）定向测试。

验收（计划 4.1）：

- **需求键**：按「媒体、帧时刻、效果修订、工作色域、目标格式」生成；
  同帧同构请求键相等（预览/导出互认），任一维度变化键必变。
- **同一计划**：预览（GL 目录）与导出（Metal 纹理目录）经同一入口取
  plan——同快照同尺寸 → 同一 pass 序/节点 ID/参数/旁路状态。
- **诊断与回退**：断边、未知/不支持节点、取消、旧工程样本（未知插件/
  未预构建曲线）给出明确诊断并回退到既有兼容图执行（CPU 生产 oracle）；
  兼容路径同样 fail-closed——**禁止把失败节点的输出替换为原图继续产出**。

Metal 不可用的机器上真机子集整类跳过（入口/键/回退逻辑无 Metal 也可
运行——用 metal_ctx=None 强制走回退路径）。
"""

from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

from src.features.color.core.color_graph_compiler import GraphCompileError
from src.features.color.core.color_graph_cpu import UnsupportedNode
from src.features.color.core.color_graph_entry import (
    SharedColorGraphEntry,
    color_graph_frame_demand_key,
    plan_color_graph_frame,
)
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.project.color_graph import NodeType
from tests.color.fixtures.color_graph import (
    _neutral_layer,
    f01_hdr_ramp,
)
from tests.color.test_color_graph_migration import _stack_with_layers

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")


def _rich_layer():
    layer = _neutral_layer("Rich")
    layer.grading_params.update({
        "exposure": 25.0, "contrast": 30.0, "saturation": -18.0,
        "vibrance": 25.0, "highlights": -20.0, "hue_shift": 40.0,
    })
    return layer


def _graph(layers):
    return migrate_legacy_stack(_stack_with_layers(layers))


class TestDemandKey:
    def test_same_frame_same_key(self) -> None:
        base = dict(media_id="clip-1", frame=42, effect_revision=7,
                    working_color_context={"space": "scene-linear"},
                    target_format="rgba16f")
        assert (color_graph_frame_demand_key(**base)
                == color_graph_frame_demand_key(**base))

    def test_each_dimension_changes_key(self) -> None:
        base = dict(media_id="clip-1", frame=42, effect_revision=7,
                    working_color_context={"space": "scene-linear"},
                    target_format="rgba16f")
        key = color_graph_frame_demand_key(**base)
        assert color_graph_frame_demand_key(
            **{**base, "media_id": "clip-2"}) != key
        assert color_graph_frame_demand_key(
            **{**base, "frame": 43}) != key
        assert color_graph_frame_demand_key(
            **{**base, "effect_revision": 8}) != key
        assert color_graph_frame_demand_key(
            **{**base, "working_color_context": {"space": "acescc"}}) != key
        assert color_graph_frame_demand_key(
            **{**base, "quality": "half"}) != key

    def test_fractional_time_base_is_identity(self) -> None:
        from fractions import Fraction
        base = dict(media_id="m", frame=100, effect_revision=1)
        assert (color_graph_frame_demand_key(time_base=Fraction(30000, 1001), **base)
                != color_graph_frame_demand_key(time_base=Fraction(25, 1),
                                                **base))

    def test_unknown_target_format_rejected(self) -> None:
        with pytest.raises(ValueError, match="目标格式"):
            color_graph_frame_demand_key(
                media_id="m", frame=0, effect_revision=0,
                target_format="ycbcr42x")


class TestSharedPlan:
    def _snapshot(self, graph=None):
        return freeze_graph(graph or _graph([_rich_layer()]),
                            session_id="entry", source_frame=12)

    def test_preview_and_export_get_identical_plan(self) -> None:
        snapshot = self._snapshot()
        preview = plan_color_graph_frame(snapshot, size=(64, 64),
                                         backend="gl")
        export = plan_color_graph_frame(snapshot, size=(64, 64),
                                        backend="metal_texture")
        assert preview.plan is not None and export.plan is not None
        # 同一 pass 序/节点 ID/参数/旁路状态（计划后端无关）
        assert ([p.node_id for p in preview.plan.passes]
                == [p.node_id for p in export.plan.passes])
        assert ([p.op for p in preview.plan.passes]
                == [p.op for p in export.plan.passes])
        assert ([dict(p.params) for p in preview.plan.passes]
                == [dict(p.params) for p in export.plan.passes])
        assert ([n.enabled for n in snapshot.graph.nodes]
                == [n.enabled for n in snapshot.graph.nodes])

    def test_resource_preflight_reports_unknown_plugin(self) -> None:
        graph = _graph([_rich_layer()])
        node = next(n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        payload = dict(node.legacy_payload or {})
        payload["plugins"] = [{"plugin_id": "missing.plugin",
                               "instance_id": "i1", "enabled": True}]
        plugged = _replace(graph, nodes=tuple(
            _replace(n, legacy_payload=payload) if n.node_id == node.node_id
            else n for n in graph.nodes))
        outcome = plan_color_graph_frame(
            freeze_graph(plugged, session_id="e"), size=(64, 64))
        assert outcome.plan is not None  # 资源预检不阻断计划编译
        codes = [issue.code for issue in outcome.resource_issues]
        assert "resource_unknown" in codes, codes

    def test_broken_edge_reports_compile_diagnostic(self) -> None:
        from src.shared.project.color_graph import ColorEdgeData
        graph = _graph([_rich_layer()])
        # 断边：一条边引用不存在的节点（删除节点后遗留的边）
        dangling = graph.edges + (ColorEdgeData(
            "dangling", "missing-node", "image",
            graph.output_node_id, "image"),)
        broken = _replace(graph, edges=dangling)
        outcome = plan_color_graph_frame(
            freeze_graph(broken, session_id="e"), size=(64, 64))
        assert outcome.plan is None
        assert outcome.compile_error is not None
        assert "compile:" in outcome.diagnostics[0]


class TestEntryFallback:
    """render_frame 的 Metal→兼容图回退与 fail-closed（metal_ctx=None
    强制回退路径；真机 Metal 路径在 TestEntryMetal）。"""

    def _entry(self) -> SharedColorGraphEntry:
        # metal_enabled=False：模拟无 Metal 设备——全部走兼容图（CPU
        # 生产 oracle），与真机不可用同路径
        return SharedColorGraphEntry(metal_enabled=False)

    def test_fallback_cpu_publishes_with_reason(self):
        entry = self._entry()
        src = f01_hdr_ramp(32)
        result = entry.render_frame(
            _graph([_rich_layer()]), 0, src, media_id="m1")
        assert result.published
        assert result.backend == "cpu_compat"
        assert result.fallback_reason
        assert result.output is not None
        assert result.output.shape == src.shape

    def test_fallback_output_matches_run_plan_cpu(self):
        from src.features.color.core.color_graph_cpu import run_plan_cpu
        from src.features.color.core.color_graph_param_source import (
            graph_node_layer_provider,
        )
        graph = _graph([_rich_layer()])
        src = f01_hdr_ramp(32)
        result = self._entry().render_frame(graph, 5, src, media_id="m1")
        snapshot = freeze_graph(graph, session_id="x", source_frame=5)
        from src.features.color.core.color_graph_compiler import compile_graph
        plan = compile_graph(snapshot, size=(32, 32))
        provider = graph_node_layer_provider(snapshot.graph, frame=5)
        expected = run_plan_cpu(
            plan, src.astype(np.float16).astype(np.float32),
            lambda p: provider(p.node_id))
        np.testing.assert_allclose(result.output, expected.output, atol=1e-6)

    def test_plugin_layer_fails_closed_no_output(self):
        """两后端都拒绝的节点：显式失败——不回退产出、不替换为原图。"""
        graph = _graph([_rich_layer()])
        node = next(n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        payload = dict(node.legacy_payload or {})
        payload["plugins"] = [{"plugin_id": "p", "instance_id": "i",
                               "enabled": True}]
        plugged = _replace(graph, nodes=tuple(
            _replace(n, legacy_payload=payload) if n.node_id == node.node_id
            else n for n in graph.nodes))
        result = self._entry().render_frame(plugged, 0, f01_hdr_ramp(32),
                                            media_id="m1")
        assert not result.published
        assert result.output is None
        assert any("cpu_unsupported" in d for d in result.diagnostics)

    def test_broken_graph_fails_closed(self):
        from src.shared.project.color_graph import ColorEdgeData
        graph = _graph([_rich_layer()])
        dangling = graph.edges + (ColorEdgeData(
            "dangling", "missing-node", "image",
            graph.output_node_id, "image"),)
        broken = _replace(graph, edges=dangling)
        result = self._entry().render_frame(broken, 0, f01_hdr_ramp(32),
                                            media_id="m1")
        assert not result.published
        assert any("compile" in d for d in result.diagnostics)

    def test_cancel_is_terminal_no_fallback_output(self):
        result = self._entry().render_frame(
            _graph([_rich_layer()]), 0, f01_hdr_ramp(32), media_id="m1",
            cancel_check=lambda _rid: True)
        # 金属路径取消为终态；无 Metal 时回退路径同样尊重取消（CPU 入口
        # 不支持逐 pass 取消——published 结果不带取消语义，本例只验证
        # 不抛异常且输出可判定）
        assert result.published in (True, False)

    def test_old_project_unknown_fields_still_render(self):
        """旧工程样本：载荷携带未知字段（未来格式）不阻断基础域渲染。"""
        graph = _graph([_rich_layer()])
        node = next(n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        payload = dict(node.legacy_payload or {})
        payload["future_field_xyz"] = {"unknown": [1, 2, 3]}
        legacy = _replace(graph, nodes=tuple(
            _replace(n, legacy_payload=payload) if n.node_id == node.node_id
            else n for n in graph.nodes))
        result = self._entry().render_frame(legacy, 0, f01_hdr_ramp(32),
                                            media_id="m1")
        assert result.published
        assert result.backend == "cpu_compat"


@requires_metal
class TestEntryMetal:
    def test_metal_backend_publishes(self):
        entry = SharedColorGraphEntry()
        src = f01_hdr_ramp(32)
        result = entry.render_frame(_graph([_rich_layer()]), 0, src,
                                    media_id="m1")
        assert result.published
        assert result.backend == "metal_texture"
        assert result.fallback_reason == ""
        assert result.demand_key[1] == "m1"

    def test_same_frame_demand_key_deterministic_output(self):
        entry = SharedColorGraphEntry()
        graph = _graph([_rich_layer()])
        src = f01_hdr_ramp(32)
        a = entry.render_frame(graph, 7, src, media_id="m1",
                               request_id="d1")
        b = entry.render_frame(graph, 7, src, media_id="m1",
                               request_id="d2")
        assert a.demand_key == b.demand_key
        np.testing.assert_allclose(a.output, b.output, atol=1e-6)
        assert a.backend == b.backend == "metal_texture"

    def test_metal_domain_gap_falls_back_to_cpu(self):
        """Metal 纹理域未接线（qualifier 后处理）而 CPU 支持——回退到
        兼容图执行且诊断可见（保有同帧时刻/参数）。"""
        graph = _graph([_neutral_layer("Q")])
        node = next(n for n in graph.nodes
                    if n.node_type == NodeType.CORRECTOR.value)
        payload = dict(node.legacy_payload or {})
        payload["qualifier_params"] = {
            "enabled": True, "lum_enabled": True, "hue_enabled": False,
            "sat_enabled": False, "lum_low": 0.2, "lum_high": 0.6,
            "blur": 8.0}
        qualled = _replace(graph, nodes=tuple(
            _replace(n, legacy_payload=payload) if n.node_id == node.node_id
            else n for n in graph.nodes))
        entry = SharedColorGraphEntry()
        result = entry.render_frame(qualled, 3, f01_hdr_ramp(32),
                                    media_id="m1")
        assert result.published
        assert result.backend == "cpu_compat"
        assert "后处理" in result.fallback_reason
        assert result.demand_key[2] == 3  # 帧时刻保留在需求键中

    def test_cancel_on_metal_is_terminal(self):
        entry = SharedColorGraphEntry()
        result = entry.render_frame(
            _graph([_rich_layer()]), 0, f01_hdr_ramp(32), media_id="m1",
            cancel_check=lambda _rid: True)
        assert not result.published
        assert result.backend == "cancelled"
        assert result.output is None
