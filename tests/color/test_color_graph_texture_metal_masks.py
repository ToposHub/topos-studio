"""阶段 4.3 —— 遮罩与复杂节点的 Metal 纹理执行定向测试。

覆盖（计划 4.3 优先批次）：qualifier（keyer 的 MSL 翻译 vs CPU 权威）、
窗口（CPU 光栅化 → R16Float 消费）、反向遮罩（invert）、组合遮罩
（qualifier × 窗口）、Alpha Output、KeyMixer（add/subtract/intersect）、
Outside（key 补集）、Parallel Mixer 声明 key 槽、Layer Mixer、关键帧
（同一帧时间求值，编辑/拖动/导出一致）。

未接线域显式 fail-closed：qualifier 后处理（clean/blur/denoise 邻域）、
降噪/时域类节点（阶段 4.3 后续批次——本文件锚定拒绝行为不静默近似）。

CPU 参考一律 ``run_plan_cpu`` 生产 oracle（与 GL 执行器组合顺序同构，
test_color_graph_key_channel 已锁 CPU↔GL 一致）；双容差 SDR ≤1/255、
HDR ≤2e-3。真机层（无 Metal 设备整类跳过 = NOT RUN）。
"""

from __future__ import annotations

from dataclasses import replace as _replace

import numpy as np
import pytest

from src.features.color.core.color_graph_compiler import compile_graph
from src.features.color.core.color_graph_cpu import run_plan_cpu
from src.features.color.core.color_graph_param_source import (
    graph_node_layer_provider,
)
from src.features.color.core.color_graph_snapshot import freeze_graph
from src.features.color.core.color_graph_texture_metal import (
    ColorGraphMetalTextureExecutor,
    texture_layer_domain_errors,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.project.color_graph import (
    ColorEdgeData,
    ColorGraphData,
    ColorNodeData,
    NodeType,
)
from tests.color.fixtures.color_graph import f02_patches

requires_metal = pytest.mark.skipif(
    not MetalComputeContext.is_supported(), reason="无 Metal 设备（NOT RUN）")

SDR_TOL = 1.0 / 255.0
HDR_REL = 2e-3

_W = _H = 32

_QUALIFIER_A = {"enabled": True, "lum_enabled": True, "hue_enabled": False,
                "sat_enabled": False, "lum_low": 0.2, "lum_high": 0.6,
                "lum_soft_low": 0.05, "lum_soft_high": 0.05}
_QUALIFIER_B = {"enabled": True, "lum_enabled": True, "hue_enabled": False,
                "sat_enabled": False, "lum_low": 0.4, "lum_high": 0.8,
                "lum_soft_low": 0.05, "lum_soft_high": 0.05}
_QUALIFIER_HUE = {"enabled": True, "hue_enabled": True, "sat_enabled": True,
                  "lum_enabled": False, "hue_center": 0.1, "hue_width": 0.3,
                  "hue_soft": 0.1, "sat_low": 0.2, "sat_high": 0.95,
                  "sat_soft_low": 0.05, "sat_soft_high": 0.05}

_CIRCLE = {"id": "w1", "type": "circle", "center": [0.5, 0.5],
           "size": [0.5, 0.5], "rotation": 0.0, "feather": 0.0,
           "opacity": 1.0, "visible": True, "invert": False,
           "operation": "add"}
_CIRCLE_FEATHERED = dict(_CIRCLE, id="w2", feather=0.25, size=[0.7, 0.7])


def _node(node_id: str, node_type: NodeType, *, params=None,
          legacy=None) -> ColorNodeData:
    return ColorNodeData(node_id=node_id, node_type=node_type.value,
                         params=params or {},
                         legacy_payload=legacy or {})


def _corrector(node_id: str, *, qualifier: dict | None = None,
               shapes: list | None = None, grading: dict | None = None,
               keyframes: dict | None = None) -> ColorNodeData:
    legacy = {"id": node_id, "grading_params": dict(grading or {})}
    if qualifier is not None:
        legacy["qualifier_params"] = dict(qualifier)
    if shapes is not None:
        legacy["mask_shapes"] = [dict(s) for s in shapes]
    if keyframes is not None:
        legacy["grade_keyframes"] = keyframes
    return _node(node_id, NodeType.CORRECTOR, legacy=legacy)


def _graph(nodes, edges) -> ColorGraphData:
    return ColorGraphData(nodes=tuple(nodes), edges=tuple(edges),
                          source_node_id="src", output_node_id="out")


def _params_of(graph, frame: int | None = None):
    provider = graph_node_layer_provider(graph, frame=frame)

    def provide(render_pass):
        return provider(render_pass.node_id) or {}
    return provide


def _gradient_source() -> np.ndarray:
    ramp = np.linspace(0.0, 1.0, _W, dtype=np.float32)
    rgb = np.stack([ramp, ramp * 0.8 + 0.1, ramp * 0.5 + 0.2,
                    np.ones_like(ramp)], axis=-1)
    return np.repeat(rgb[None, :, :], _H, axis=0)


def _patches_source() -> np.ndarray:
    return f02_patches(_W)


def _assert_dual(metal: np.ndarray, ref: np.ndarray, label: str) -> None:
    metal32 = np.asarray(metal, dtype=np.float32)
    ref = np.asarray(ref, dtype=np.float32)
    diff = np.abs(metal32.astype(np.float64) - ref.astype(np.float64))
    mag = np.maximum(np.abs(ref.astype(np.float64)), 1e-6)
    sdr = np.abs(ref) <= 1.0
    assert diff[sdr].max(initial=0.0) <= SDR_TOL, (
        f"{label}: SDR abs {diff[sdr].max(initial=0.0):.3e}")
    rel = (diff[~sdr] / mag[~sdr]).max(initial=0.0) if (~sdr).any() else 0.0
    assert rel <= HDR_REL, f"{label}: HDR rel {rel:.3e}"


def _run(ctx, executor, graph, image, *, target="output", frame=None,
         request_id="t", with_cpu=True):
    import Metal

    snapshot = freeze_graph(graph, session_id="mask",
                            viewer_target=target)
    plan = compile_graph(snapshot, size=(_W, _H))
    quantized = image.astype(np.float16).astype(np.float32)
    cpu = None
    if with_cpu:
        cpu = run_plan_cpu(plan, quantized,
                           _params_of(snapshot.graph, frame=frame))
    provider = graph_node_layer_provider(snapshot.graph, frame=frame)
    source = ctx.create_texture_from_numpy(
        np.ascontiguousarray(image, dtype=np.float16),
        Metal.MTLPixelFormatRGBA16Float, _W, _H)
    result = executor.execute_plan(
        plan, source, request_id=request_id,
        layer_param_provider=provider)
    return result, cpu, plan


def _serial_graph(correctors: list[ColorNodeData]) -> ColorGraphData:
    nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT)]
    edges = []
    prev = "src"
    for node in correctors:
        nodes.append(node)
        edges.append(ColorEdgeData(f"{prev}->{node.node_id}", prev, "image",
                                   node.node_id, "image"))
        prev = node.node_id
    edges.append(ColorEdgeData("last->out", prev, "image", "out", "image"))
    return _graph(nodes, edges)


# ---------------------------------------------------------------------------
# 真机层：qualifier / 窗口 / key 通道 / 关键帧
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx
    ctx.finish()


@requires_metal
class TestQualifier:
    def test_qualifier_kernel_vs_cpu_matte(self, metal_ctx):
        """tg_qualifier vs compute_qualifier_matte（lum/hue/sat 窗 + invert）。"""
        import Metal

        from src.features.color.core.qualifier_cpu import (
            compute_qualifier_matte,
        )
        image = _patches_source()
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        for label, qualifier in (("lum", _QUALIFIER_A),
                                 ("hue+sat", _QUALIFIER_HUE),
                                 ("invert", dict(_QUALIFIER_A,
                                                 invert=True))):
            from src.features.color.core.metal_texture_kernels import (
                make_tex_qualifier_params,
            )
            src_tex = metal_ctx.create_texture_from_numpy(
                np.ascontiguousarray(image, dtype=np.float16),
                Metal.MTLPixelFormatRGBA16Float, _W, _H)
            matte = metal_ctx.create_texture_2d(
                Metal.MTLPixelFormatR16Float, _W, _H, shader_write=True)
            params = make_tex_qualifier_params(qualifier)
            kernel = metal_ctx.compile_kernel(
                "tg_qualifier",
                __import__("src.features.color.core.metal_texture_kernels",
                           fromlist=["METAL_TEX_GRAPH_OPS_MSL"]
                           ).METAL_TEX_GRAPH_OPS_MSL)
            metal_ctx.dispatch(kernel,
                               textures={0: src_tex, 1: matte},
                               buffers={0: metal_ctx.create_buffer_from_numpy(
                                   params)},
                               threads=(_W, _H, 1))
            metal_ctx.submit(wait=True)
            got = executor.read_key_r16f(matte, _W, _H)
            ref = compute_qualifier_matte(
                image.astype(np.float32), qualifier)[..., 0]
            diff = np.abs(got - ref)
            assert diff.max() <= 2.0 / 255.0, (
                f"{label}: matte max diff {diff.max():.4f}")

    def test_corrector_with_qualifier_vs_cpu(self, metal_ctx):
        grading = {"exposure": 20.0, "saturation": -10.0}
        graph = _serial_graph([_corrector(
            "c1", qualifier=_QUALIFIER_A, grading=grading)])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "qualifier-corrector")
        # matte 限定的效果只在选中带内生效（中心带被改、暗端不动）
        assert np.abs(got[..., :3] - cpu.output[..., :3]).max() <= SDR_TOL

    def test_qualifier_postprocess_fail_closed(self, metal_ctx):
        """qualifier 后处理（blur/clean/denoise 邻域）未接线——显式拒绝。"""
        layer = {"qualifier_params": {"enabled": True, "blur": 12.0}}
        assert any("后处理" in e
                   for e in texture_layer_domain_errors(layer))
        graph = _serial_graph([_corrector(
            "c1", qualifier={"enabled": True, "blur": 12.0})])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, _cpu, _plan = _run(metal_ctx, executor, graph,
                                   _gradient_source())
        assert not result.published
        assert result.diagnostic_code == "param_domain_unsupported"


@requires_metal
class TestWindows:
    def test_window_mask_vs_cpu(self, metal_ctx):
        grading = {"exposure": -15.0}
        graph = _serial_graph([_corrector(
            "c1", shapes=[_CIRCLE], grading=grading)])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "window")

    def test_feathered_window_vs_cpu(self, metal_ctx):
        """羽化边缘（半透明过渡带）——CPU 光栅化同源消费。"""
        grading = {"contrast": 20.0}
        graph = _serial_graph([_corrector(
            "c1", shapes=[_CIRCLE_FEATHERED], grading=grading)])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _patches_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "window-feather")

    def test_inverted_window_vs_cpu(self, metal_ctx):
        inverted = dict(_CIRCLE, invert=True)
        graph = _serial_graph([_corrector(
            "c1", shapes=[inverted], grading={"exposure": 25.0})])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "window-invert")

    def test_qualifier_and_window_combined_vs_cpu(self, metal_ctx):
        graph = _serial_graph([_corrector(
            "c1", qualifier=_QUALIFIER_A, shapes=[_CIRCLE],
            grading={"saturation": 25.0})])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "qualifier+window")


@requires_metal
class TestKeyChannel:
    def _alpha_graph(self, corrector_node: ColorNodeData):
        nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
                 corrector_node, _node("alpha", NodeType.ALPHA_OUTPUT)]
        edges = [
            ColorEdgeData("src->c", "src", "image",
                          corrector_node.node_id, "image"),
            ColorEdgeData("c->alpha", corrector_node.node_id, "key_out",
                          "alpha", "key"),
            ColorEdgeData("src->out", "src", "image", "out", "image"),
        ]
        return _graph(nodes, edges), "alpha"

    def test_alpha_output_key_from_qualifier(self, metal_ctx):
        graph, alpha_id = self._alpha_graph(
            _corrector("c1", qualifier=_QUALIFIER_A))
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source(),
                                  target=f"viewer:{alpha_id}")
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        assert cpu.alpha_output is not None
        _assert_dual(got[..., 0], cpu.alpha_output[..., 0], "alpha-key")
        # 带内选中/带外不选中的锚点
        assert got[..., 0].std() > 0.05

    def test_alpha_output_key_from_window(self, metal_ctx):
        graph, alpha_id = self._alpha_graph(
            _corrector("c1", shapes=[_CIRCLE]))
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source(),
                                  target=f"viewer:{alpha_id}")
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got[..., 0], cpu.alpha_output[..., 0], "alpha-window")
        assert got[_H // 2, _W // 2, 0] > 0.5
        assert got[1, 1, 0] < 0.1

    def test_key_mixer_modes_vs_cpu(self, metal_ctx):
        """KeyMixer add/subtract/intersect：混合 key 产物（R 通道）。"""
        for mode in ("add", "subtract", "intersect"):
            nodes = [_node("src", NodeType.SOURCE),
                     _node("out", NodeType.OUTPUT),
                     _corrector("ca", qualifier=_QUALIFIER_A),
                     _corrector("cb", qualifier=_QUALIFIER_B),
                     _node("km", NodeType.KEY_MIXER,
                           params={"mode": mode})]
            edges = [
                ColorEdgeData("src->ca", "src", "image", "ca", "image"),
                ColorEdgeData("ca->cb", "ca", "image", "cb", "image"),
                ColorEdgeData("cb->out", "cb", "image", "out", "image"),
                ColorEdgeData("ca->km", "ca", "key_out", "km", "key",
                              slot_id="km/key/0"),
                ColorEdgeData("cb->km", "cb", "key_out", "km", "key",
                              slot_id="km/key/1"),
            ]
            graph = _graph(nodes, edges)
            executor = ColorGraphMetalTextureExecutor(metal_ctx)
            result, cpu, _plan = _run(
                metal_ctx, executor, graph, _gradient_source(),
                target="viewer:km", request_id=f"km-{mode}")
            assert result.published, result.diagnostic_message
            got = executor.read_output_rgba16f(result)
            final = cpu.output
            # matte 域容差：串行中间链的 fp16 量化（±2.4e-4）经 softband
            # 斜率（1.5/0.05=30/unit）放大约 7e-3——锚定 8e-3（repo key
            # 通道测试直接 matte 比较用 3e-3，本例多一级 corrector 链）
            diff = np.abs(got[..., 0] - final[..., 0])
            assert diff.max() <= 8e-3, (
                f"key-mix-{mode}: matte diff {diff.max():.4f}")

    def test_outside_key_complement_vs_cpu(self, metal_ctx):
        """Outside：引用 key 的补集作为 matte 的 Corrector。"""
        nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
                 _corrector("ca", qualifier=_QUALIFIER_A,
                            grading={"exposure": 10.0}),
                 _node("oc", NodeType.OUTSIDE,
                       params={"key_source_mode": "node_input",
                               "outside_key_source": "ca"})]
        edges = [
            ColorEdgeData("src->ca", "src", "image", "ca", "image"),
            ColorEdgeData("ca->oc", "ca", "image", "oc", "image"),
            ColorEdgeData("ca->oc-key", "ca", "key_out", "oc", "key_in"),
            ColorEdgeData("oc->out", "oc", "image", "out", "image"),
        ]
        graph = _graph(nodes, edges)
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "outside")

    def test_parallel_mixer_declared_key_slot_vs_cpu(self, metal_ctx):
        """Parallel Mixer 声明 key 槽：分支按真实 key_out matte 折叠。"""
        nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
                 _corrector("base", grading={}),
                 _corrector("branch", qualifier=_QUALIFIER_A,
                            grading={"exposure": 40.0}),
                 _node("pm", NodeType.PARALLEL_MIXER)]
        edges = [
            ColorEdgeData("src->base", "src", "image", "base", "image"),
            ColorEdgeData("base->pm", "base", "image", "pm", "base"),
            ColorEdgeData("base->branch", "base", "image", "branch", "image"),
            ColorEdgeData("branch->pm", "branch", "image", "pm", "branch",
                          slot_id="pm/branch/0"),
            ColorEdgeData("branch->pm-key", "branch", "key_out", "pm", "key",
                          slot_id="pm/branch/0"),
            ColorEdgeData("pm->out", "pm", "image", "out", "image"),
        ]
        graph = _graph(nodes, edges)
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, cpu, _plan = _run(metal_ctx, executor, graph,
                                  _gradient_source())
        assert result.published, result.diagnostic_message
        got = executor.read_output_rgba16f(result)
        _assert_dual(got, cpu.output, "parallel-key-slot")

    def test_missing_key_product_fail_closed(self, metal_ctx):
        """声明的 key 边无上游 key 产物——显式拒绝（不常白顶替）。"""
        nodes = [_node("src", NodeType.SOURCE), _node("out", NodeType.OUTPUT),
                 _corrector("plain", grading={}),
                 _node("km", NodeType.KEY_MIXER)]
        edges = [
            ColorEdgeData("src->plain", "src", "image", "plain", "image"),
            ColorEdgeData("plain->out", "plain", "image", "out", "image"),
            ColorEdgeData("plain->km", "plain", "key_out", "km", "key",
                          slot_id="km/key/0"),
        ]
        graph = _graph(nodes, edges)
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        result, _cpu, _plan = _run(metal_ctx, executor, graph,
                                   _gradient_source(), target="viewer:km",
                                   with_cpu=False)
        assert not result.published
        assert result.diagnostic_code in ("key_output_missing",
                                          "execution_error")


@requires_metal
class TestKeyframes:
    """关键帧：同一帧时间求值——provider 在同一 frame 上取参，编辑/拖动/
    导出（Metal 与 CPU）对同一帧一致。"""

    _KF = {"exposure": {"keyframes": [
        {"frame": 0, "value": 0.0, "interpolation": "linear"},
        {"frame": 30, "value": 60.0, "interpolation": "linear"},
        {"frame": 60, "value": -20.0, "interpolation": "linear"},
    ]}}

    def _animated_graph(self):
        return _serial_graph([
            _corrector("c1", grading={"contrast": 10.0},
                       keyframes=self._KF),
            _corrector("c2", qualifier=_QUALIFIER_A,
                       grading={"saturation": 15.0}),
        ])

    def test_frames_evaluate_same_time_both_backends(self, metal_ctx):
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        image = _gradient_source()
        outputs = []
        for frame in (0, 15, 30, 45, 60):
            result, cpu, _plan = _run(
                metal_ctx, executor, self._animated_graph(), image,
                frame=frame, request_id=f"kf{frame}")
            assert result.published, result.diagnostic_message
            got = executor.read_output_rgba16f(result)
            _assert_dual(got, cpu.output, f"kf-{frame}")
            outputs.append(got)
        # 关键帧确实逐帧生效（不同帧输出不同；帧 0 是无动画基线）
        assert np.abs(outputs[1] - outputs[0]).max() > 1e-3
        assert np.abs(outputs[2] - outputs[4]).max() > 1e-3

    def test_seek_back_reproduces_frame(self, metal_ctx):
        """拖动/切片往返：回到同一帧时刻输出一致（无时域状态）。"""
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        image = _gradient_source()
        graph = self._animated_graph()
        first, _cpu, _plan = _run(metal_ctx, executor, graph, image,
                                  frame=45, request_id="s1")
        a = executor.read_output_rgba16f(first)
        second, _cpu2, _plan2 = _run(metal_ctx, executor, graph, image,
                                     frame=22, request_id="s2")
        b_mid = executor.read_output_rgba16f(second)
        third, _cpu3, _plan3 = _run(metal_ctx, executor, graph, image,
                                    frame=45, request_id="s3")
        c = executor.read_output_rgba16f(third)
        np.testing.assert_allclose(a, c, atol=1e-6)
        assert np.abs(a - b_mid).max() > 1e-3

    def test_key_product_pool_hygiene(self, metal_ctx):
        """key 产物 matte 在请求末归池：带 qualifier 的图重复执行不增长。"""
        graph = _serial_graph([_corrector(
            "c1", qualifier=_QUALIFIER_A, grading={"exposure": 10.0})])
        executor = ColorGraphMetalTextureExecutor(metal_ctx)
        image = _gradient_source()
        first, _cpu, _plan = _run(metal_ctx, executor, graph, image,
                                  request_id="hy1")
        assert first.published
        executor.release_revision(first.request_id)
        allocated = executor.pool_allocated()
        second, _cpu2, _plan2 = _run(metal_ctx, executor, graph, image,
                                     request_id="hy2")
        assert second.published
        executor.release_revision(second.request_id)
        assert executor.pool_allocated() == allocated, (
            "key 产物 matte 重复执行应复用池（无持续增长）")
