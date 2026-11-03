"""B09.1/B09.5 GL 执行测试 —— N07 串行 plan 执行与新旧路径等价。

核心断言：迁移图的串行 plan 经 N07 执行的输出与 B00 基线（旧
layers ping-pong 同数学路径）**位级一致**——图执行链路首次拿到与
legacy 等价的像素证据。
"""

from __future__ import annotations

import os

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import (
    ColorGraphExecutor,
    GraphExecutionResult,
)
from src.features.color.core.color_graph_migration import (
    migrate_legacy_stack,
    project_legacy_layers,
)
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.corrector_render_service import run_serial_passes
from src.features.color.core.shader_compiler import ShaderPipeline
from src.shared.project.color_graph import NodeType
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers

BASELINE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "docs", "validation", "color_nodes", "baselines")


def _migrated_graph():
    return migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))


def _layer_provider(graph):
    """node_id → 可执行 Layer DTO：legacy_payload + 曲线 LUT 从
    curves_data 重建（save/load 语义：控制点持久化，LUT 是缓存）。"""
    from src.features.color.core.layer import build_curves_lut_from_data
    from src.shared.project.color_graph import unfreeze_params
    payloads = {n.node_id: unfreeze_params(dict(n.legacy_payload))
                for n in graph.nodes}

    def provide(node_id):
        payload = dict(payloads.get(node_id) or {})
        node = next((n for n in graph.nodes if n.node_id == node_id), None)
        if node is not None:
            payload["visible"] = node.enabled
        if payload.get("curves_data"):
            payload["curves_lut"] = build_curves_lut_from_data(
                dict(payload["curves_data"]))
        if payload.get("name") == "Tone Curve":
            # 基线 L2 携带插件 stub（非注册表插件）——显式注入 binding
            from tests.color.fixtures.color_graph import (
                f03_plugin_stub_binding,
            )
            payload["_plugin_bindings"] = [f03_plugin_stub_binding()]
        return payload
    return provide


class TestSerialGraphExecutionParity:
    def test_serial_plan_matches_legacy_baseline(self):
        """串行图 plan 执行 == B00 f03_serial 基线（新旧路径等价）。"""
        graph = _migrated_graph()
        source = __import__("tests.color.fixtures.color_graph",
                            fromlist=["f01_hdr_ramp"]).f01_hdr_ramp(64)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            snapshot = freeze_graph(graph, session_id="s1")
            plan = compile_graph(snapshot, size=(64, 64))

            src_tex = ctx.texture((64, 64), components=4, dtype="f2",
                                  data=source.astype(np.float16))
            result = executor.execute_plan(
                plan, src_tex, request_id="req-1", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert result.published
            assert result.executed_passes == len(plan.passes)

            out_fbo = ctx.framebuffer(
                color_attachments=[result.output_texture])
            out_fbo.use()
            gl_out = np.frombuffer(
                out_fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(64, 64, 4).astype(np.float32)

            ref = np.load(os.path.join(BASELINE_DIR, "f03_serial.npy"))
            np.testing.assert_array_equal(gl_out, ref, err_msg=(
                "串行图执行与旧 layers 路径必须位级一致（B09.5 对照）"))
            executor.release_revision("req-1")
            src_tex.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_disabled_node_respected_in_execution(self):
        """图侧 enabled=False 的节点在执行中旁路（与 legacy visible 同义）。"""
        graph = _migrated_graph()
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        from dataclasses import replace as _replace
        disabled = _replace(cs[1], enabled=False)
        graph = _replace(
            graph, nodes=tuple(
                disabled if n.node_id == cs[1].node_id else n
                for n in graph.nodes))
        source = __import__("tests.color.fixtures.color_graph",
                            fromlist=["f01_hdr_ramp"]).f01_hdr_ramp(64)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(
                freeze_graph(graph, session_id="s"), size=(64, 64))
            src_tex = ctx.texture((64, 64), components=4, dtype="f2",
                                  data=source.astype(np.float16))
            result = executor.execute_plan(
                plan, src_tex, request_id="r", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert result.published
            out_fbo = ctx.framebuffer(
                color_attachments=[result.output_texture])
            out_fbo.use()
            gl_out = np.frombuffer(
                out_fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(64, 64, 4).astype(np.float32)
            ref = np.load(os.path.join(
                BASELINE_DIR, "f03_l1_bypassed.npy"))
            np.testing.assert_array_equal(gl_out, ref)
            executor.release_revision("r")
            src_tex.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()


class TestCapabilityFailClosed:
    def test_unknown_op_refuses_execution(self):
        """目录外算子：fail-closed 拒绝，不发布（B10 mixer 已翻 exact）。"""
        from src.features.color.core.color_graph_service import ColorGraphService
        counter = iter(range(100))
        service = ColorGraphService(id_factory=lambda: f"m{next(counter)}")
        grown = service.apply_intent(_migrated_graph(),
                                     "add_parallel_branch", {})
        assert grown.ok
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(
                ctx, backend_catalog={"source": "exact", "corrector": "exact",
                                      "output": "exact", "parallel_mixer":
                                      "pending"})
            plan = compile_graph(freeze_graph(grown.graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), components=4, dtype="f2")
            result = executor.execute_plan(plan, src, request_id="r2")
            assert result.published is False
            assert result.diagnostic_code == "capability_unsupported"
            assert "fail-closed" in result.diagnostic_message
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()
