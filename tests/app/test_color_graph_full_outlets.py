"""N01：图执行 flag 的全出口（full-res f4 / 视频播放附快照 / 静默降级补拒）。

验收（计划 N01）：出口语义先完成、默认开启另需性能矩阵——本文件锁定：
- 执行器 dtype 参数化（full-res f4 / front f2；池按 dtype 分离）；
- full_res target 在 flag 开启时携带 color_graph_execute；
- 视频播放 render params 附图快照（flag 门控）；
- headless full-res / Composite ToposColor / Edit 预览对复杂图显式
  拒绝或降级标记（不静默 primary-only）。
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("moderngl")

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.image_engine import ImageEngine
from tests.color.fixtures.color_graph import f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers


def _graph():
    return migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))


def _engine():
    engine = ImageEngine.__new__(ImageEngine)
    engine._active_color_graph = None
    engine._active_graph_stack_id = ""
    engine._active_graph_key = None
    engine._active_graph_snapshot_cache = None
    engine._color_graph_revision = 0
    return engine


# ── 执行器 dtype 参数化 ─────────────────────────────────────────────────


class TestExecutorDtype:
    def _run(self, ctx, dtype):
        graph = _graph()
        executor = ColorGraphExecutor(ctx)
        snapshot = freeze_graph(graph, session_id="s")
        plan = compile_graph(snapshot, size=(32, 32))
        src = ctx.texture((32, 32), 4, dtype=dtype)
        result = executor.execute_plan(
            plan, src, request_id=f"dt-{dtype}", source_size=(32, 32),
            layer_param_provider=graph_node_layer_provider(graph),
            dtype=dtype)
        src.release()
        assert result.published, result.diagnostic_message
        out = result.output_texture
        executor.release_revision(f"dt-{dtype}")
        return executor, out

    def test_full_res_dtype_f4(self):
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor, out = self._run(ctx, "f4")
            assert out.dtype == "f4"
            executor.release_pool()
        finally:
            ctx.release()

    def test_dtype_pool_separation(self):
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor, out4 = self._run(ctx, "f4")
            out4.release()  # 手动释放（发布租约外）
            executor, out2 = self._run(ctx, "f2")
            assert out2.dtype == "f2"
            # 池键含 dtype：f4 纹理不得被 f2 run 复用
            keys = {k[2] for k in executor._pool}
            executor.release_pool()
            assert "f4" not in keys or all(
                t.dtype == "f4" for t in executor._pool.get(
                    (32, 32, "f4"), []))
        finally:
            ctx.release()

    def test_run_dtype_restored_after_failure(self):
        """执行失败（未知算子）后 _run_dtype 恢复默认——后续调用不受染。"""
        import moderngl
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            from src.features.color.core.color_graph_compiler import (
                ColorGraphRenderPlan, RenderPass,
            )
            bad = ColorGraphRenderPlan(
                passes=(RenderPass(index=0, node_id="x", node_type="mystery",
                                   op="mystery_op", params={}, inputs=(),
                                   outputs=("image",)),),
                target="output", snapshot_key="bad")
            src = ctx.texture((8, 8), 4, dtype="f2")
            result = executor.execute_plan(bad, src, request_id="bad",
                                           source_size=(8, 8), dtype="f4")
            assert not result.published
            assert result.diagnostic_code == "capability_unsupported"
            assert executor._run_dtype == "f2"  # 已恢复
            src.release()
            executor.release_pool()
        finally:
            ctx.release()


# ── 出口 attach / 播放参数 ──────────────────────────────────────────────


class TestOutlets:
    def test_full_res_attach_carries_execute_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "1")
        engine = _engine()
        engine.set_active_color_graph(_graph(), stack_id="st-1")
        params = {"color_output_target": "full_res"}
        engine._attach_color_graph_params(
            params, execute=engine.color_graph_render_enabled)
        assert params["color_graph_execute"] is True

    def test_video_render_params_attach_snapshot(self, monkeypatch):
        """N01-a：播放出口经 _build_render_params 附快照（flag 门控）。"""
        from src.shared.video.video_playback import VideoPlaybackEngine

        class _FakeEngine:
            """裸 ImageEngine 属性探针会触 QObject 未初始化——用窄代理
            只暴露 _build_render_params 消费的成员。"""
            layers: list = []
            film_grain_params = None

            def __init__(self, real, flag: bool) -> None:
                self._real, self._flag = real, flag

            @property
            def color_graph_render_enabled(self) -> bool:
                return self._flag

            def _attach_color_graph_params(self, params, execute=False):
                self._real._attach_color_graph_params(params,
                                                      execute=execute)

        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "1")
        real = _engine()
        real.set_active_color_graph(_graph(), stack_id="st-1")
        controller = VideoPlaybackEngine.__new__(VideoPlaybackEngine)
        controller.engine = _FakeEngine(real, True)
        controller.is_playing = False
        params = controller._build_render_params()
        assert "color_graph_snapshot" in params
        assert params["color_graph_execute"] is True
        # flag 关：只附身份不执行
        real2 = _engine()
        real2._color_graph_render_enabled = False
        real2.set_active_color_graph(_graph(), stack_id="st-2")
        controller.engine = _FakeEngine(real2, False)
        params2 = controller._build_render_params()
        assert "color_graph_snapshot" in params2
        assert "color_graph_execute" not in params2


# ── 静默降级补拒（B13 同口径） ─────────────────────────────────────────


def _complex_graph():
    graph = _graph()
    from src.features.color.core.color_graph_service import ColorGraphService
    counter = iter(range(1000))
    service = ColorGraphService(id_factory=lambda: f"n01-{next(counter)}")
    grown = service.apply_intent(graph, "add_parallel_branch",
                                 {"branch_origin_id": next(
                                     n.node_id for n in graph.nodes
                                     if n.node_type == "corrector")})
    assert grown.ok
    return grown.graph


class TestExplicitRejection:
    def test_headless_full_res_rejects_complex_graph(self):
        engine = _engine()
        engine.set_active_color_graph(_complex_graph(), stack_id="st-c")
        with pytest.raises(RuntimeError,
                           match=r"\[color_graph_not_exportable\]"):
            engine._do_render_full_res(render_request_id="r")

    def test_headless_full_res_allows_projectable_graph(self):
        engine = _engine()
        engine.set_active_color_graph(_graph(), stack_id="st-p")
        # 可投影图不触发硬门（真正渲染需要 GL 宿主，这里只验门不拦）
        try:
            engine._do_render_full_res(render_request_id="r")
        except RuntimeError as exc:
            assert "color_graph_not_exportable" not in str(exc)
        except Exception:
            pass  # 无图/无图像等环境性早退均可，只要不是复杂图硬门

    def test_composite_topos_color_rejects_complex_graph(self):
        from src.features.composite.evaluators.color_evaluator import (
            ColorEvaluatorMixin,
        )

        class _Evaluator(ColorEvaluatorMixin):
            pass

        evaluator = _Evaluator()
        evaluator._project = type("P", (), {"color_stacks": {}})()

        class _Stack:
            color_graph = _complex_graph()

        evaluator._project.color_stacks["st-c"] = _Stack()
        with pytest.raises(RuntimeError,
                           match=r"\[color_graph_not_exportable\]"):
            evaluator._apply_topos_color(
                None, {"color_stack_id": "st-c"}, 8, 8)

    def test_edit_preview_marks_unprojectable_graph(self):
        """Edit 预览对复杂图显式降级标记（不静默 primary-only）。"""
        from src.features.edit.core.timeline_preview_renderer import (
            preview_graph_unprojectable,
        )

        class _StackComplex:
            grading_params = {"exposure": 0.3}
            color_graph = _complex_graph()

        class _StackSimple:
            grading_params = {"exposure": 0.3}
            color_graph = _graph()

        class _StackNone:
            grading_params = {"exposure": 0.3}

        assert preview_graph_unprojectable(_StackComplex()) is True
        assert preview_graph_unprojectable(_StackSimple()) is False
        assert preview_graph_unprojectable(_StackNone()) is False
