"""S03/S04：scope 图取样契约与真实图输出接入（offscreen 实测）。

S03 契约（``scope_graph_sampling``）：graded 默认输入 = 主 Output 完整
画面；节点选择只导航不隐式改测量输入；单节点/Key 测量是显式独立模式
（目标前缀 + 输入标签）；source/reference 独立语义。

S04 接通：scope 全帧经同一 immutable 图快照与 render plan（N07）执行——
两个非交换 Corrector、分支 Mixer、Compound 与 CPU oracle
（``run_plan_cpu``，同一生产 provider）一致；删除/旁路/调序后输出随
真实 Output 变化；E03 splice 在位（``color_graph_execute`` flag +
fail-closed + 租约语义 + 图命令域参数生效）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.async_renderer import AsyncRenderer
from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_executor import ColorGraphExecutor
from src.features.color.core.color_graph_migration import migrate_legacy_stack
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
    mixer_composite_params,
    node_layer_payload,
)
from src.features.color.core.color_graph_service import ColorGraphService
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.image_engine import ImageEngine
from src.features.color.core.layer import build_curves_lut_from_data
from src.features.color.core.scope_graph_sampling import (
    PANEL_SOURCE_TO_TARGET,
    TARGET_NODE_PREFIX,
    TARGET_OUTPUT,
    TARGET_SOURCE,
    explicit_key_target,
    explicit_node_target,
    parse_target,
    target_label,
)
from src.features.color.core.scope_types import ScopeDemand
from tests.color.fixtures.color_graph import f01_hdr_ramp, f03_layer_stack
from tests.color.test_color_graph_migration import _stack_with_layers

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3


def _dual_ok(cpu: np.ndarray, gl: np.ndarray) -> None:
    diff = np.abs(cpu.astype(np.float64) - gl.astype(np.float64))
    mag = np.maximum(np.abs(gl.astype(np.float64)), 1e-6)
    sdr = np.abs(gl) <= 1.0
    assert diff[sdr].max(initial=0.0) <= SDR_TOL
    rel = diff[~sdr] / mag[~sdr]
    assert rel.max(initial=0.0) <= HDR_REL


def _migrated_graph():
    return migrate_legacy_stack(_stack_with_layers(f03_layer_stack()))


def _run_gl(ctx, executor, graph, source, size):
    plan = compile_graph(freeze_graph(graph, session_id="s04"),
                         size=size)
    h, w = source.shape[:2]
    src_tex = ctx.texture((w, h), components=4, dtype="f2",
                          data=source.astype(np.float16))
    try:
        result = executor.execute_plan(
            plan, src_tex, request_id="s04", source_size=(w, h),
            layer_param_provider=graph_node_layer_provider(graph))
        assert result.published, result.diagnostic_message
        fbo = ctx.framebuffer(color_attachments=[result.output_texture])
        fbo.use()
        out = np.frombuffer(fbo.read(components=4, dtype="f2"),
                            dtype=np.float16).reshape(
            h, w, 4).astype(np.float32)
        executor.release_revision("s04")
        return out, plan
    finally:
        src_tex.release()


# ═══ S03：取样契约 ═══════════════════════════════════════════════════════

class TestScopeSamplingContract:
    def test_graded_default_is_main_output_full_frame(self):
        """S03 §1：graded 默认测量目标 = 主 Output 完整画面。"""
        assert TARGET_OUTPUT == "output"
        identity_default = ScopeDemand(
            revision=1, source_revision=1, mode="waveform",
            display_size=(10, 10)).sample_identity
        assert identity_default is None  # 渐进采用：无身份不影响旧契约

    def test_explicit_node_and_key_targets_are_distinct_modes(self):
        node_t = explicit_node_target("n-1")
        key_t = explicit_key_target("n-1")
        assert node_t.startswith(TARGET_NODE_PREFIX)
        assert parse_target(node_t) == ("node", "n-1")
        assert parse_target(key_t) == ("key", "n-1")
        # 与 graded 默认互斥：目标身份与标签都不同
        assert target_label(node_t) != target_label(TARGET_OUTPUT)
        assert "explicit" in target_label(node_t)

    def test_parse_target_rejects_unknown(self):
        assert parse_target("spin:xyz") == ("unknown", None)
        assert parse_target(None) == ("unknown", None)
        assert parse_target("node:") == ("node", None)

    def test_panel_sources_map_to_independent_targets(self):
        """S03 §5：graded/source/reference 三分语义保留。"""
        assert PANEL_SOURCE_TO_TARGET == {
            "graded": TARGET_OUTPUT,
            "source": TARGET_SOURCE,
            "reference": "reference",
        }

    @pytest.mark.parametrize("source", ["graded", "source", "reference"])
    def test_labels_declare_measurement_domain(self, source):
        label = target_label(PANEL_SOURCE_TO_TARGET[source])
        assert "Output (full frame)" in label or "Source" in label \
            or "Reference" in label


# ═══ S04 前置：生产 provider（图命令域参数必须生效）═════════════════════

class TestProductionProviderMerge:
    def _graph_with_grade_patch(self):
        graph = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        corrector = next(n for n in graph.nodes if n.node_type == "corrector")
        result = svc.op_set_node_option(
            graph, corrector.node_id, "grade_patch", ("exposure", 37.5))
        assert result.ok
        return result.graph, corrector.node_id

    def test_grade_patch_overrides_legacy_grading_params(self):
        graph, node_id = self._graph_with_grade_patch()
        node = next(n for n in graph.nodes if n.node_id == node_id)
        payload = node_layer_payload(node)
        assert payload["grading_params"]["exposure"] == 37.5
        # 基底其余键保留（legacy_payload 未丢）
        assert "contrast" in payload["grading_params"]

    def test_bypass_maps_to_passthrough(self):
        graph = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        corrector = next(n for n in graph.nodes if n.node_type == "corrector")
        result = svc.op_set_bypass(graph, [corrector.node_id], False)
        assert result.ok
        node = next(n for n in result.graph.nodes
                    if n.node_id == corrector.node_id)
        assert node_layer_payload(node)["visible"] is False

    def test_curves_lut_rebuilt_from_control_points(self):
        graph = _migrated_graph()
        curve_node = next(
            n for n in graph.nodes
            if n.node_type == "corrector" and n.legacy_payload
            and dict(n.legacy_payload).get("curves_data"))
        payload = dict(node_layer_payload(curve_node))
        lut = np.asarray(payload["curves_lut"]).reshape(256, 3)
        assert lut.shape == (256, 3)
        # 控制点派生（非恒等）：UI 屏幕坐标 y=0 顶部=最大输出——
        # M 曲线 (0,255) 意味 x=0 映射到最小输出（取反曲线）
        assert lut[0, 0] == pytest.approx(0.0, abs=1e-6)
        assert lut[-1, 0] == pytest.approx(1.0, abs=1e-6)
        expected = build_curves_lut_from_data(dict(payload["curves_data"]))
        np.testing.assert_allclose(
            lut, np.asarray(expected).reshape(256, 3), atol=0.0)

    def test_graph_created_branch_defaults_to_production_layer(self):
        """无 legacy_payload 的新建 Corrector 以生产默认 DTO 为基底。"""
        from src.features.color.core.layer import Layer
        graph = _migrated_graph()
        counter = [0]

        def _ids():
            counter[0] += 1
            return f"new-{counter[0]}"
        result = ColorGraphService(id_factory=_ids).op_add_layer_branch(
            graph)
        assert result.ok
        branch = result.graph.nodes[-1]
        assert branch.node_type == "corrector"
        assert not branch.legacy_payload
        payload = node_layer_payload(branch)
        defaults = Layer("probe").to_dict(None)
        assert set(defaults["grading_params"]) <= set(
            payload["grading_params"])
        assert payload["visible"] is True

    def test_non_corrector_nodes_return_none(self):
        graph = _migrated_graph()
        provider = graph_node_layer_provider(graph)
        output_id = graph.output_node_id
        node = next(n for n in graph.nodes if n.node_id == output_id)
        assert node_layer_payload(node) is None
        assert provider("no-such-node") is None

    def test_mixer_composite_params_both_domains(self):
        assert mixer_composite_params({}) == ("Normal", 1.0)
        assert mixer_composite_params(
            {"blend_mode": "Overlay", "opacity": 0.5}) == ("Overlay", 0.5)
        assert mixer_composite_params(
            {"mixer": {"blend_mode": "Soft Light",
                       "opacity": 0.25}}) == ("Soft Light", 0.25)
        # 顶层优先（B10 构造域）
        assert mixer_composite_params(
            {"blend_mode": "Divide",
             "mixer": {"blend_mode": "Normal"}}) == ("Divide", 1.0)


# ═══ S04 前置：E02 图快照入参 ═══════════════════════════════════════════

class TestEngineGraphStash:
    def _engine(self):
        engine = ImageEngine.__new__(ImageEngine)
        engine._active_color_graph = None
        engine._active_graph_stack_id = ""
        engine._active_graph_key = None
        engine._active_graph_snapshot_cache = None
        engine._color_graph_revision = 0
        return engine

    def test_set_and_clear_bump_revision(self):
        engine = self._engine()
        graph = _migrated_graph()
        engine.set_active_color_graph(graph, stack_id="st-1")
        rev_after_set = engine.color_graph_revision
        assert rev_after_set == 1
        assert engine.active_color_graph_stack_id == "st-1"
        engine.clear_active_color_graph()
        assert engine.color_graph_revision == 2
        assert engine.active_color_graph is None

    def test_snapshot_cached_until_graph_changes(self):
        engine = self._engine()
        graph = _migrated_graph()
        engine.set_active_color_graph(graph, stack_id="st-1")
        snap1 = engine.active_color_graph_snapshot()
        snap2 = engine.active_color_graph_snapshot()
        assert snap1 is snap2  # 播放期间不重复深拷贝
        # 图编辑（新对象）→ 新快照 + revision 递增（plan 缓存失效语义）
        svc = ColorGraphService(id_factory=lambda: "x")
        corrector = next(n for n in graph.nodes
                         if n.node_type == "corrector")
        edited = svc.op_set_node_option(
            graph, corrector.node_id, "grade_patch",
            ("exposure", 11.0)).graph
        engine.set_active_color_graph(edited, stack_id="st-1")
        assert engine.color_graph_revision == 2
        assert engine.active_color_graph_snapshot() is not snap1

    def test_attach_params_only_with_graph(self):
        engine = self._engine()
        params = {}
        engine._attach_color_graph_params(params)
        assert "color_graph_snapshot" not in params
        engine.set_active_color_graph(_migrated_graph(), stack_id="st-1")
        engine._attach_color_graph_params(params)
        assert params["color_graph_snapshot"].session_id == "st-1"
        # 只附加快照，不开启执行（B16.5 前 scope lane 才置 execute）
        assert "color_graph_execute" not in params


# ═══ S04 前置：E07 hydrator 登记 ═════════════════════════════════════════

class TestHydratorGraphStash:
    def test_stack_with_graph_registers_engine_graph(self):
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        stack = _stack_with_layers(f03_layer_stack())
        graph = migrate_legacy_stack(stack)
        stack_with_graph = stack.with_color_graph(graph)
        engine = Mock()
        ColorStackHydrator().apply_stack_to_engine(stack_with_graph, engine)
        engine.set_active_color_graph.assert_called_once_with(
            graph, stack_id=stack.stack_id)

    def test_stack_without_graph_clears_engine_graph(self):
        from src.features.color.adapters.color_stack_hydrator import (
            ColorStackHydrator,
        )
        stack = _stack_with_layers(f03_layer_stack())
        engine = Mock()
        ColorStackHydrator().apply_stack_to_engine(stack, engine)
        engine.set_active_color_graph.assert_not_called()
        engine.clear_active_color_graph.assert_called_once_with()


# ═══ S04：E03 splice（stub，无 GL）══════════════════════════════════════

class TestScopeGraphSplice:
    def _scope_renderer(self, scope_params):
        from tests.color.test_gpu_scope_pipeline import _GpuScopeGuardRenderer
        r = _GpuScopeGuardRenderer()
        r._last_scope_render_params = scope_params
        r._scope_color_output_tex = SimpleNamespace(size=(512, 288))
        r._do_render_color_output = Mock(return_value=True)
        pool = {"wf_display": Mock(glo=71)}
        r._ensure_gpu_scope_pool = lambda *_a: pool
        r._scope_sample_front = Mock()
        r._scope_mask_uv_rect = lambda *_a: (0.0, 0.0, 1.0, 1.0)
        r._scope_accumulate_waveform = lambda *_a: None
        r._scope_display_waveform = lambda *_a: 1.0
        return r

    def test_scope_sets_graph_execute_when_snapshot_present(self):
        r = self._scope_renderer(
            {"layers": [], "color_graph_snapshot": SimpleNamespace()})
        AsyncRenderer._do_render_gpu_scope(
            r, ScopeDemand(revision=5, source_revision=10,
                           mode="waveform", display_size=(10, 10)))
        params = r._do_render_color_output.call_args.args[0]
        assert params["color_graph_execute"] is True
        assert params["color_output_target"] == "scope"

    def test_scope_legacy_path_without_snapshot(self):
        r = self._scope_renderer({"layers": []})
        AsyncRenderer._do_render_gpu_scope(
            r, ScopeDemand(revision=5, source_revision=10,
                           mode="waveform", display_size=(10, 10)))
        params = r._do_render_color_output.call_args.args[0]
        assert "color_graph_execute" not in params

    def test_frame_identity_echoes_demand_with_front_revision(self):
        from src.features.color.core.scope_graph_sampling import (
            ScopeSampleIdentity,
        )
        identity = ScopeSampleIdentity(
            session_id="st-1", graph_revision=3,
            source_content_revision=10, frame=7,
            color_context=("srgb", "bt709"), target=TARGET_OUTPUT)
        r = self._scope_renderer({"layers": []})
        r._scope_source_revision = 12
        AsyncRenderer._do_render_gpu_scope(
            r, ScopeDemand(revision=5, source_revision=10,
                           mode="waveform", display_size=(10, 10),
                           sample_identity=identity))
        frame = r._emitted[0]
        assert frame.sample_identity.session_id == "st-1"
        assert frame.sample_identity.source_content_revision == 12
        assert identity.allows(frame.sample_identity)

    def test_graph_frame_fail_closed_on_deferred_legacy_feature(self):
        import src.features.color.core.scope_diagnostics as diag_mod
        from src.features.color.core.scope_diagnostics import ScopeDiagnostics
        diag = ScopeDiagnostics(enabled=True)
        original = diag_mod.scope_diagnostics
        diag_mod.scope_diagnostics = diag
        try:
            r = AsyncRenderer.__new__(AsyncRenderer)
            r._graph_executor = None
            r._graph_plan_cache = {}
            r._graph_lease = None
            r.ai_plugin_textures = {}
            params = {
                "color_graph_snapshot": SimpleNamespace(),
                "layers": [{"id": "L1", "is_baked": True}],
            }
            assert AsyncRenderer._execute_graph_frame(
                r, params, 64, 64, Mock()) is None
            assert diag.count(
                "execute", "graph_deferred_legacy_feature") == 1
        finally:
            diag_mod.scope_diagnostics = original

    def test_graph_frame_fail_closed_on_compile_error(self):
        import src.features.color.core.scope_diagnostics as diag_mod
        from src.features.color.core.scope_diagnostics import ScopeDiagnostics
        diag = ScopeDiagnostics(enabled=True)
        original = diag_mod.scope_diagnostics
        diag_mod.scope_diagnostics = diag
        try:
            r = AsyncRenderer.__new__(AsyncRenderer)
            r._graph_executor = None
            r._graph_plan_cache = {}
            r._graph_lease = None
            r.ai_plugin_textures = {}
            broken = SimpleNamespace(graph="not-a-graph")
            params = {"color_graph_snapshot": broken, "layers": []}
            assert AsyncRenderer._execute_graph_frame(
                r, params, 64, 64, Mock()) is None
            assert diag.count("execute", "graph_exception") == 1
        finally:
            diag_mod.scope_diagnostics = original

    def test_graph_frame_success_uses_single_lease(self):
        ctx = moderngl.create_standalone_context(require=330)
        try:
            r = AsyncRenderer.__new__(AsyncRenderer)
            r.ctx = ctx
            r.pipeline = None
            r.ai_plugin_textures = {}
            r._graph_executor = None
            r._graph_plan_cache = {}
            r._graph_lease = None
            # 宿主委托的最小桩：租约语义测试不依赖完整渲染器资源表
            r._render_quad = lambda prog: None
            r._bind_aux_textures_single_layer = lambda prog, layer: None
            r._generate_matte = lambda *a, **k: None
            graph = _migrated_graph()
            snapshot = freeze_graph(graph, session_id="s04")
            source = f01_hdr_ramp(32)
            src_tex = ctx.texture((32, 32), components=4, dtype="f2",
                                  data=source.astype(np.float16))
            try:
                for i in range(2):
                    tex = AsyncRenderer._execute_graph_frame(
                        r,
                        {"color_graph_snapshot": snapshot,
                         "render_request_id": f"req-{i}", "layers": []},
                        32, 32, src_tex)
                    assert tex is not None
                    executor, request_id = r._graph_lease
                    assert len(executor._published) == 1
                r._release_graph_lease()
                executor, _ = r._graph_lease or (None, None)
                assert executor is None
                first_executor = r._graph_executor
                assert not first_executor._published
            finally:
                src_tex.release()
        finally:
            ctx.release()


# ═══ S04：真实图输出 == CPU oracle（同一生产 provider）══════════════════

class TestGraphFrameExecutionParity:
    def _compare(self, graph, source):
        size = (source.shape[1], source.shape[0])
        plan = compile_graph(freeze_graph(graph, session_id="s04"),
                             size=size)
        provider = graph_node_layer_provider(graph)
        quantized = source.astype(np.float16).astype(np.float32)
        cpu = run_plan_cpu(plan, quantized,
                           lambda p: provider(p.node_id))
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            h, w = source.shape[:2]
            src_tex = ctx.texture((w, h), components=4, dtype="f2",
                                  data=source.astype(np.float16))
            try:
                result = executor.execute_plan(
                    plan, src_tex, request_id="s04-p", source_size=(w, h),
                    layer_param_provider=provider)
                assert result.published, result.diagnostic_message
                fbo = ctx.framebuffer(
                    color_attachments=[result.output_texture])
                fbo.use()
                gl = np.frombuffer(
                    fbo.read(components=4, dtype="f2"),
                    dtype=np.float16).reshape(h, w, 4).astype(np.float32)
            finally:
                executor.release_revision("s04-p")
                src_tex.release()
        finally:
            ctx.release()
        _dual_ok(cpu.output, gl)
        return gl, cpu.output

    def test_two_non_commutative_correctors_match_cpu(self):
        """S04 验收：两个非交换 Corrector 结果与 CPU oracle 一致。"""
        base = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        correctors = [n for n in base.nodes if n.node_type == "corrector"]
        graph = base
        graph = svc.op_set_node_option(
            graph, correctors[0].node_id, "grade_patch",
            ("exposure", 40.0)).graph
        graph = svc.op_set_node_option(
            graph, correctors[1].node_id, "grade_patch",
            ("contrast", 45.0)).graph
        self._compare(graph, f01_hdr_ramp(48))

    def test_reorder_changes_output_and_still_matches_cpu(self):
        """S04 验收：调序后波形随真实 Output 变化（非交换）。"""
        base = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        correctors = [n for n in base.nodes if n.node_type == "corrector"]
        graph_a = base
        graph_a = svc.op_set_node_option(
            graph_a, correctors[0].node_id, "grade_patch",
            ("exposure", 40.0)).graph
        graph_a = svc.op_set_node_option(
            graph_a, correctors[1].node_id, "grade_patch",
            ("contrast", 45.0)).graph
        # 调序：交换两个 corrector 的 grade 域
        graph_b = base
        graph_b = svc.op_set_node_option(
            graph_b, correctors[0].node_id, "grade_patch",
            ("contrast", 45.0)).graph
        graph_b = svc.op_set_node_option(
            graph_b, correctors[1].node_id, "grade_patch",
            ("exposure", 40.0)).graph
        out_a, _ = self._compare(graph_a, f01_hdr_ramp(48))
        out_b, _ = self._compare(graph_b, f01_hdr_ramp(48))
        assert np.abs(out_a - out_b).max() > 1e-3, (
            "非交换参数调序必须改变输出（scope 波形随真实 Output 变化）")

    def test_bypass_changes_output(self):
        """S04 验收：旁路后输出变化（直通）。"""
        graph = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        correctors = [n for n in graph.nodes if n.node_type == "corrector"]
        out_full, _ = self._compare(graph, f01_hdr_ramp(48))
        bypassed = svc.op_set_bypass(graph, [correctors[0].node_id], False)
        assert bypassed.ok
        out_bypass, _ = self._compare(bypassed.graph, f01_hdr_ramp(48))
        assert np.abs(out_full - out_bypass).max() > 1e-3

    def test_delete_changes_output(self):
        """S04 验收：删除节点后输出变化。"""
        graph = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        correctors = [n for n in graph.nodes if n.node_type == "corrector"]
        out_full, _ = self._compare(graph, f01_hdr_ramp(48))
        deleted = svc.op_delete_nodes(graph, [correctors[0].node_id])
        assert deleted.ok
        out_deleted, _ = self._compare(deleted.graph, f01_hdr_ramp(48))
        assert np.abs(out_full - out_deleted).max() > 1e-3

    def test_branch_layer_mixer_matches_cpu(self):
        """S04 验收：分支 Mixer 结果与 CPU oracle 一致。"""
        graph = _migrated_graph()
        counter = [0]

        def _ids():
            counter[0] += 1
            return f"mix-{counter[0]}"
        svc = ColorGraphService(id_factory=_ids)
        corrector = next(n for n in graph.nodes
                         if n.node_type == "corrector")
        result = svc.op_add_layer_branch(graph, corrector.node_id)
        assert result.ok
        graph = result.graph
        mixer = next(n for n in graph.nodes
                     if n.node_type == "layer_mixer")
        # 图命令域的 blend_mode（set_node_option 领域 schema 键）
        set_mode = svc.op_set_node_option(
            graph, mixer.node_id, "blend_mode", "Overlay")
        assert set_mode.ok
        graph = set_mode.graph
        # 分支 Corrector 参数经 grade_patch 生效（provider 合并语义）
        new_correctors = [n for n in graph.nodes
                          if n.node_type == "corrector"]
        target = new_correctors[-1]
        patched = svc.op_set_node_option(
            graph, target.node_id, "grade_patch", ("saturation", 60.0))
        assert patched.ok
        self._compare(patched.graph, f01_hdr_ramp(48))

    def test_compound_matches_ungrouped(self):
        """S04 验收：Compound 结果与未分组执行一致（位级）。"""
        graph = _migrated_graph()
        svc = ColorGraphService(id_factory=lambda: "x")
        correctors = [n.node_id for n in graph.nodes
                      if n.node_type == "corrector"][:2]
        grouped = svc.op_group_subgraph(graph, correctors)
        assert grouped.ok
        out_flat, _ = self._compare(graph, f01_hdr_ramp(32))
        out_grp, _ = self._compare(grouped.graph, f01_hdr_ramp(32))
        np.testing.assert_array_equal(out_flat, out_grp)
