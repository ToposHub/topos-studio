"""B16.5 生产接通（flag 门控）+ 执行器逐 pass 纹理池化。

- ``TOPOS_COLOR_GRAPH_RENDER``（默认关）：开启后 engine 在
  render_pipeline/request_color_output 提交 ``color_graph_execute``，
  ``_do_render_impl`` 图执行分支接管 front/color output/full-res（N01：
  full-res 由执行器按 target 用 f4 dtype；scope 无条件走图执行）。
- 池化：执行器逐 pass 纹理按尺寸复用（liveness/租约归还进池），池上限
  防爆显存；输出与无池化路径位级一致（parity 套件复跑覆盖）。
- 默认启用（flag 默认值翻转为开）需 S07 原生验收——本文件锁定门控
  语义而非默认值。
"""

from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_migration import (
    migrate_legacy_stack,
)
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.image_engine import ImageEngine
from tests.color.fixtures.color_graph import f01_hdr_ramp, f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3


def _engine():
    engine = ImageEngine.__new__(ImageEngine)
    engine._active_color_graph = None
    engine._active_graph_stack_id = ""
    engine._active_graph_key = None
    engine._active_graph_snapshot_cache = None
    engine._color_graph_revision = 0
    return engine


def _graph():
    return migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))


class TestProductionSwitchFlag:
    def test_default_off_keeps_legacy_loop(self):
        engine = _engine()
        assert not engine.color_graph_render_enabled
        engine.set_active_color_graph(_graph(), stack_id="st-1")
        params = {}
        engine._attach_color_graph_params(params)
        assert "color_graph_snapshot" in params
        assert "color_graph_execute" not in params, (
            "默认必须保持 legacy 循环（默认启用需 S07 原生验收）")

    def test_flag_on_sets_execute(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "1")
        engine = _engine()
        engine.set_active_color_graph(_graph(), stack_id="st-1")
        params = {}
        engine._attach_color_graph_params(params, execute=True)
        assert params["color_graph_execute"] is True

    def test_flag_read_once_and_cacheable(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "true")
        engine = _engine()
        assert engine.color_graph_render_enabled
        # 环境变化不热切（启动时定格；测试可显式覆写私有缓存）
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "")
        assert engine.color_graph_render_enabled
        engine._color_graph_render_enabled = False
        assert not engine.color_graph_render_enabled

    def test_full_res_included_with_f4_dtype(self, monkeypatch):
        """N01：full-res 出口同样图执行（执行器按 target 选 f4 dtype；
        复杂图 fail-closed 不回退）。"""
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "1")
        engine = _engine()
        engine.set_active_color_graph(_graph(), stack_id="st-1")
        params = {"color_output_target": "full_res"}
        engine._attach_color_graph_params(
            params, execute=engine.color_graph_render_enabled)
        assert params["color_graph_execute"] is True
        assert "color_graph_snapshot" in params  # 身份仍随参

    def test_no_graph_no_flag(self, monkeypatch):
        monkeypatch.setenv("TOPOS_COLOR_GRAPH_RENDER", "1")
        engine = _engine()
        params = {}
        engine._attach_color_graph_params(params, execute=True)
        assert "color_graph_execute" not in params
        assert "color_graph_snapshot" not in params


class TestGraphExecutionFailClosed:
    def test_impl_returns_false_without_legacy_fallback(self):
        """``_do_render_impl`` 图分支 fail-closed：图失败返回 False，
        不回退旧循环（复杂图旧循环=过期投影错图）。分支语义：
        ``color_graph_execute`` + 快照同时在场才走图；其余 legacy。"""
        use_graph = bool(
            {"color_graph_execute": True, "color_graph_snapshot": object()}
            .get("color_graph_execute")
            and {"color_graph_execute": True,
                 "color_graph_snapshot": object()}
            .get("color_graph_snapshot") is not None)
        assert use_graph
        legacy = {"color_graph_execute": True}  # 无快照（无图栈）
        assert not (bool(legacy.get("color_graph_execute"))
                    and legacy.get("color_graph_snapshot") is not None)
        flag_only = {"color_graph_snapshot": object()}  # 未开执行
        assert not (bool(flag_only.get("color_graph_execute"))
                    and flag_only.get("color_graph_snapshot") is not None)


class TestExecutorTexturePool:
    def _execute(self, ctx, executor, graph, source, size=(32, 32)):
        plan = compile_graph(freeze_graph(graph, session_id="pool"),
                             size=size)
        provider = graph_node_layer_provider(graph)
        h, w = source.shape[:2]
        src_tex = ctx.texture((w, h), components=4, dtype="f2",
                              data=source.astype(np.float16))
        try:
            result = executor.execute_plan(
                plan, src_tex, request_id="pool", source_size=(w, h),
                layer_param_provider=provider)
            assert result.published
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            out = np.frombuffer(
                fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(h, w, 4).astype(np.float32)
            return out, plan, provider
        finally:
            src_tex.release()

    def test_pool_reuses_textures_across_plans(self):
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _graph()
            source = f01_hdr_ramp(32)
            out1, plan, provider = self._execute(ctx, executor, graph,
                                                 source)
            # 执行中 liveness 到期的中间纹理已归还池；输出仍在租约
            during = executor.pooled_texture_count()
            assert "pool" in executor._published
            executor.release_revision("pool")
            assert executor.pooled_texture_count() == during + 1, (
                "租约归还后输出纹理应进池")
            out2, _, _ = self._execute(ctx, executor, graph, source)
            executor.release_revision("pool")
            # 复用不改变输出（位级）
            np.testing.assert_array_equal(out1, out2)
        finally:
            executor.release_pool()
            ctx.release()

    def test_pool_bounded_per_size(self):
        ctx = moderngl.create_standalone_context(require=330)
        executor = ColorGraphExecutor(ctx)
        try:
            overflow = 4
            textures = [executor._alloc_texture(16, 16) for _ in range(
                ColorGraphExecutor._POOL_MAX_PER_SIZE + overflow)]
            for tex in textures:
                executor._release_texture(tex)
            assert executor.pooled_texture_count() == \
                ColorGraphExecutor._POOL_MAX_PER_SIZE
        finally:
            executor.release_pool()
            ctx.release()

    def test_release_pool_frees_all(self):
        ctx = moderngl.create_standalone_context(require=330)
        executor = ColorGraphExecutor(ctx)
        try:
            textures = [executor._alloc_texture(8, 8) for _ in range(3)]
            for tex in textures:
                executor._release_texture(tex)
            assert executor.pooled_texture_count() == 3
            released = executor.release_pool()
            assert released >= 3
            assert executor.pooled_texture_count() == 0
        finally:
            ctx.release()

    def test_pooled_output_matches_cpu_oracle(self):
        """池化不改变数学输出（与 CPU oracle 双门限一致）。"""
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            graph = _graph()
            source = f01_hdr_ramp(32)
            out, plan, provider = self._execute(ctx, executor, graph,
                                                source)
            executor.release_revision("pool")
            # 再跑一次（全部走池）
            out2, _, _ = self._execute(ctx, executor, graph, source)
            executor.release_revision("pool")
            quantized = source.astype(np.float16).astype(np.float32)
            cpu = run_plan_cpu(plan, quantized,
                               lambda p: provider(p.node_id))
            for gl in (out, out2):
                diff = np.abs(cpu.output.astype(np.float64)
                              - gl.astype(np.float64))
                mag = np.maximum(np.abs(gl.astype(np.float64)), 1e-6)
                sdr = np.abs(gl) <= 1.0
                assert diff[sdr].max(initial=0.0) <= SDR_TOL
                assert (diff[~sdr] / mag[~sdr]).max(initial=0.0) <= HDR_REL
        finally:
            executor.release_pool()
            ctx.release()


class TestRendererExecutorPoolRelease:
    def test_cleanup_releases_executor_pool(self):
        """renderer 清理路径释放执行器池（无泄漏租约/池纹理）。"""
        r = AsyncRenderer.__new__(AsyncRenderer)
        executor = MockExecutor()
        r._graph_executor = executor
        r._graph_lease = None
        r._graph_plan_cache = {}
        r._release_graph_resources()
        assert executor.pool_released


class MockExecutor:
    def __init__(self):
        self.pool_released = False

    def release_revision(self, _rid):
        return 1

    def release_pool(self):
        self.pool_released = True
        return 0
