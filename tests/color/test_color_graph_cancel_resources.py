"""B09.2–B09.4 取消 / 资源 / 失败路径测试 —— N07 执行器生命周期。

验收：中途取消不发布半帧（front 未受影响）；每 pass 边界取消生效；
失败统一退栈释放 scratch；成功后仅保留发布输出（release_revision 归还）；
缓存资源归 owner 不随 scratch 归还。
"""

from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import (
    ColorGraphExecutor,
    GraphExecutionCancelled,
)
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_service import ColorGraphService
from tests.color.fixtures.color_graph import f01_hdr_ramp
from tests.color.test_color_graph_gl_execution import (
    _layer_provider,
    _migrated_graph,
)


def _plan_and_provider(n_extra: int = 0):
    graph = _migrated_graph()
    service = ColorGraphService(id_factory=lambda: f"x{np.random.randint(1<<30)}")
    for _ in range(n_extra):
        grown = service.apply_intent(graph, "insert_corrector",
                                     {"active_node_id": None, "before": False})
        assert grown.ok
        graph = grown.graph
    return graph


class TestCancellation:
    def test_cancel_before_execution_raises(self):
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2")
            with pytest.raises(GraphExecutionCancelled):
                executor.execute_plan(
                    plan, src, request_id="pre",
                    source_size=(64, 64),
                    cancel_check=lambda rid: rid == "pre")
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_mid_execution_cancel_releases_and_does_not_publish(self):
        graph = _plan_and_provider(n_extra=2)   # 6 correctors，足够长
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=f01_hdr_ramp(64).astype(np.float16))
            # pass 2 之后取消
            result = executor.execute_plan(
                plan, src, request_id="mid", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph),
                cancel_check=lambda rid, _state={"n": 0}: (
                    _state.__setitem__("n", _state["n"] + 1)
                    or _state["n"] > 3))
            assert result.published is False
            assert result.diagnostic_code == "cancelled"
            # 已执行的 pass scratch 全部退栈（部分中间纹理可能已按
            # liveness 归还——退栈数 ≥1 且无发布）
            assert result.released_textures >= 1
            assert "mid" not in executor._published  # 不发布半帧
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_cancel_registry_flags(self):
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            executor.cancel_request("r1")
            assert executor.is_cancelled("r1")
            assert not executor.is_cancelled("r2")
            executor.clear_cancel("r1")
            assert not executor.is_cancelled("r1")
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()


class TestResourceLifecycle:
    def test_success_publishes_only_output(self):
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=f01_hdr_ramp(64).astype(np.float16))
            result = executor.execute_plan(
                plan, src, request_id="ok", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert result.published
            assert set(executor._published) == {"ok"}
            # release_revision 后发布纹理归还、登记清空
            assert executor.release_revision("ok") == 1
            assert "ok" not in executor._published
            assert executor.release_revision("ok") == 0   # 幂等
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_failure_releases_all_scratch(self):
        """layer provider 抛异常 → execution_error，scratch 全退。"""
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2")

            def boom(node_id):
                raise RuntimeError("provider boom")

            result = executor.execute_plan(
                plan, src, request_id="fail", source_size=(64, 64),
                layer_param_provider=boom)
            assert result.published is False
            assert result.diagnostic_code == "execution_error"
            assert result.released_textures >= 1
            assert "fail" not in executor._published
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_liveness_release_frees_intermediates(self):
        """release_after 命中即归还：成功路径 released == passes-1（只留
        发布输出）。"""
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=f01_hdr_ramp(64).astype(np.float16))
            result = executor.execute_plan(
                plan, src, request_id="live", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert result.published
            assert result.released_textures == len(plan.passes) - 1
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_cached_aux_textures_survive_request_release(self):
        """curves identity/缓存纹理是 owner 资源：请求失败不释放缓存。"""
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=f01_hdr_ramp(64).astype(np.float16))
            ok = executor.execute_plan(
                plan, src, request_id="a", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert ok.published
            curves_cache = getattr(executor, "_curves_tex_cache", {})
            executor.release_revision("a")
            assert curves_cache, "curves 缓存应存在"
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()


class TestRequestIdentityMatching:
    def test_published_result_carries_request_id(self):
        graph = _migrated_graph()
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="sess-9"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=f01_hdr_ramp(64).astype(np.float16))
            result = executor.execute_plan(
                plan, src, request_id="req-42", source_size=(64, 64),
                layer_param_provider=_layer_provider(graph))
            assert result.request_id == "req-42"
            assert result.output_size == (64, 64)
            executor.release_revision("req-42")
            src.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()
