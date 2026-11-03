"""B10 Mixer 语义测试 —— CPU oracle 规格 + GL 执行对照（spec v1）。

验收：基色 patch、非交换 layer 模式、HDR/负值、层序敏感、节点位置
不改变像素、key 不写 image alpha。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import (
    UnsupportedNode,
    layer_mix,
    parallel_mix,
)
from tests.color.fixtures.color_graph import f02_patches


def _image(seed: int, base: float = 0.4) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.random((8, 8, 4)).astype(np.float32) * base
    img[..., 3] = rng.random((8, 8)).astype(np.float32)
    return img


class TestParallelMixerOracle:
    def test_no_key_full_weight(self):
        base = _image(1)
        branch = _image(2)
        out = parallel_mix(base, [branch], [None])
        np.testing.assert_allclose(out[..., :3], branch[..., :3], atol=1e-7)

    def test_partial_key_lerp(self):
        base = np.zeros((1, 1, 4), np.float32)
        branch = np.ones((1, 1, 4), np.float32)
        key = np.full((1, 1, 4), 0.25, np.float32)
        out = parallel_mix(base, [branch], [key])
        np.testing.assert_allclose(out[..., :3], 0.25, atol=1e-7)
        assert out[..., 3] == base[..., 3]     # alpha 保留基底

    def test_key_does_not_touch_image_alpha(self):
        base = _image(3)
        branch = _image(4)
        key = _image(5)
        out = parallel_mix(base, [branch], [key])
        np.testing.assert_array_equal(out[..., 3], base[..., 3])

    def test_hdr_negative_not_clamped(self):
        base = np.full((1, 1, 4), -0.5, np.float32)
        branch = np.full((1, 1, 4), 16.0, np.float32)
        out = parallel_mix(base, [branch], [None])
        np.testing.assert_allclose(out[..., :3], 16.0, atol=1e-6)


class TestLayerMixerOracle:
    def test_bottom_up_order_matters(self):
        bottom = np.full((1, 1, 4), 0.2, np.float32)
        a = np.full((1, 1, 4), 1.0, np.float32)
        b = np.full((1, 1, 4), 0.0, np.float32)
        one = layer_mix([bottom, a, b], [1.0, 1.0], ["Normal", "Add"])
        two = layer_mix([bottom, b, a], [1.0, 1.0], ["Normal", "Add"])
        # 非交换验证（Subtract）：序 1 = (0.2⊕1.0)⊖0.0 = 1.0；
        # 序 2 = (0.2⊕0.0)⊖1.0 = -1.0（HDR 负值不截断）
        one = layer_mix([bottom, a, b], [1.0, 1.0], ["Normal", "Subtract"])
        two = layer_mix([bottom, b, a], [1.0, 1.0], ["Normal", "Subtract"])
        assert float(one[0, 0, 0]) == pytest.approx(1.0)
        assert float(two[0, 0, 0]) == pytest.approx(-1.0)

    def test_blend_modes_formulas(self):
        a = np.full((1, 1, 4), 0.4, np.float32)
        b = np.full((1, 1, 4), 0.8, np.float32)
        cases = {
            "Normal": 0.4 * 0.25 + 0.8 * 0.75,
            "Add": 0.4 + 0.8 * 0.5,
            "Subtract": 0.4 - 0.8 * 0.5,
            "Multiply": 0.4 * (1 - 0.5 + 0.5 * 0.8),
            "Screen": 1.0 - (1 - 0.4) * (1 - 0.8 * 0.5),
        }
        for mode, expected in cases.items():
            out = layer_mix([a, b], [0.5], [mode]) \
                if mode != "Normal" else layer_mix(
                    [a, b], [0.75], ["Normal"])
            assert float(out[0, 0, 0]) == pytest.approx(expected, abs=1e-6), mode

    def test_unknown_mode_rejected(self):
        a = np.zeros((1, 1, 4), np.float32)
        with pytest.raises(UnsupportedNode):
            layer_mix([a, a], [1.0], ["ColorDodge"])


class TestGraphLevelMixerExecution:
    """GL 执行的分支图 vs CPU oracle（f02 patch 输入）。"""

    def _exec_gl(self, graph, source, params_of):
        import moderngl
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_executor import (
            ColorGraphExecutor,
        )
        from src.features.color.core.color_graph_snapshot import freeze_graph
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(graph, session_id="s"),
                                 size=source.shape[:2][::-1])
            src = ctx.texture(
                (source.shape[1], source.shape[0]), 4, dtype="f2",
                data=np.ascontiguousarray(source).astype(np.float16))
            result = executor.execute_plan(
                plan, src, request_id="m", source_size=(
                    source.shape[1], source.shape[0]),
                layer_param_provider=params_of)
            assert result.published, result.diagnostic_message
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            out = np.frombuffer(
                fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(
                source.shape[0], source.shape[1], 4).astype(np.float32)
            executor.release_revision("m")
            src.release()
            return out
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def _branch_graph(self, kind: str):
        from src.features.color.core.color_graph_service import ColorGraphService
        counter = iter(range(1000))
        service = ColorGraphService(id_factory=lambda: f"g{next(counter)}")
        from tests.color.test_color_graph_schema import _serial_graph
        base = _serial_graph(1)
        grown = service.apply_intent(
            base, "add_parallel_branch" if kind == "parallel"
            else "add_layer_branch", {})
        assert grown.ok
        return grown.graph

    def test_parallel_graph_gl_matches_cpu(self):
        source = f02_patches(64)
        graph = self._branch_graph("parallel")
        # 分支节点 neutral（CPU oracle 与 GL corrector neutral 在容差内）
        from src.features.color.core.color_graph_cpu import run_plan_cpu
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_snapshot import freeze_graph
        plan = compile_graph(freeze_graph(graph, session_id="s"),
                             size=(64, 64))
        cpu = run_plan_cpu(plan, source, lambda p: {})
        gl = self._exec_gl(graph, source, lambda nid: {})
        sdr = np.abs(cpu.output[..., :3]) <= 1.0
        diff = np.abs(gl[..., :3] - cpu.output[..., :3])
        assert float(diff[sdr].max()) <= 2e-3, float(diff[sdr].max())

    def test_layer_graph_gl_executes(self):
        source = f02_patches(64)
        graph = self._branch_graph("layer")
        gl = self._exec_gl(graph, source, lambda nid: {})
        # GL 执行成功即结构语义成立（CPU layer oracle 的 slot 语义在
        # 上方 oracle 组测试锁定）
        assert gl.shape == source.shape


# ===========================================================================
# v1.1（spec §9）：Layer Mixer 全模式（三后端同式对照）
# ===========================================================================

V11_MODES = ("Overlay", "Soft Light", "Hard Light", "Color Dodge",
             "Color Burn", "Darken", "Lighten", "Difference", "Exclusion",
             "Hue", "Saturation", "Color", "Luminosity", "Addition",
             "Subtract (Clamped)", "Divide")


class TestLayerMixerV11Modes:
    def test_v11_modes_hand_computed_values(self):
        """抽样手算锚点（opacity=0.5，b=bottom=0.4，a=top=0.8）。"""
        bottom = np.full((1, 1, 4), 0.4, np.float32)
        top = np.full((1, 1, 4), 0.8, np.float32)
        overlay = 2.0 * 0.4 * 0.8                        # b<0.5 低分支
        dodge = 0.4 / max(1e-3, 1 - 0.8)
        expected = {
            "Overlay": 0.4 * 0.5 + overlay * 0.5,
            "Color Dodge": 0.4 * 0.5 + dodge * 0.5,
            "Darken": 0.4,                     # min(0.4, 0.8)，op=0.5 → 0.4
            "Lighten": 0.4 * 0.5 + 0.8 * 0.5,  # max→0.8 再过 mix
            "Difference": 0.4 * 0.5 + 0.4 * 0.5,   # |0.4-0.8|=0.4 → mix=0.4
            "Exclusion": 0.4 * 0.5 + (0.4 + 0.8 - 0.64) * 0.5,
            "Addition": 0.4 * 0.5 + 1.2 * 0.5,  # b+a=1.2 再过 mix
            "Subtract (Clamped)": 0.4 * 0.5,   # max(0.4-0.8,0)=0 → mix(0.4,0)=0.2
            "Divide": 0.4 * 0.5 + (0.4 / 0.8) * 0.5,
        }
        for mode, want in expected.items():
            out = layer_mix([bottom, top], [0.5], [mode])
            assert float(out[0, 0, 0]) == pytest.approx(want, abs=1e-6), mode

    def test_v11_mix_epilogue_equivalence_with_v1(self):
        """v1.1 mix 形式与 v1 内嵌式代数等价（4 模式 × 多 opacity）。"""
        rng = np.random.default_rng(11)
        a = rng.uniform(-0.2, 1.5, (4, 4, 4)).astype(np.float32)
        b = rng.uniform(-0.2, 1.5, (4, 4, 4)).astype(np.float32)
        for mode, formula in (("Normal", lambda x, y: x * (1 - 0.7) + y * 0.7),
                              ("Add", lambda x, y: x + y * 0.7),
                              ("Multiply", lambda x, y: x * (1 - 0.7 + 0.7 * y)),
                              ("Screen",
                               lambda x, y: 1 - (1 - x) * (1 - y * 0.7))):
            v1 = formula(a[..., :3], b[..., :3])
            from src.features.color.core.color_graph_cpu import (
                _apply_blend_mode_cpu,
            )
            blended = _apply_blend_mode_cpu(
                a[..., :3], b[..., :3],
                {"Normal": "Normal", "Add": "Addition",
                 "Multiply": "Multiply", "Screen": "Screen"}[mode])
            v11 = a[..., :3] * 0.3 + blended * 0.7
            np.testing.assert_allclose(v1, v11, atol=1e-6), mode

    def test_unknown_mode_still_rejected(self):
        a = np.zeros((1, 1, 4), np.float32)
        with pytest.raises(UnsupportedNode):
            layer_mix([a, a], [1.0], ["Mystery"])

    def test_alpha_unchanged_all_modes(self):
        rng = np.random.default_rng(3)
        bottom = rng.random((2, 2, 4)).astype(np.float32)
        top = rng.random((2, 2, 4)).astype(np.float32)
        for mode in V11_MODES:
            out = layer_mix([bottom, top], [0.5], [mode])
            np.testing.assert_array_equal(out[..., 3], bottom[..., 3]), mode

    def test_gl_shader_matches_cpu_all_modes(self):
        """GL layer_mix fragment vs CPU oracle（双容差门）。"""
        moderngl = pytest.importorskip("moderngl",
                                       reason="ModernGL 不可用")
        from src.features.color.shaders.node_ops_gl import (
            LAYER_MIX_FRAG,
            LAYER_MODE_INDEX,
            NODE_OPS_VERT,
        )
        rng = np.random.default_rng(21)
        size = 16
        bottom = rng.uniform(0.0, 1.0, (size, size, 4)).astype(np.float32)
        top = rng.uniform(0.0, 1.0, (size, size, 4)).astype(np.float32)
        qb = bottom.astype(np.float16).astype(np.float32)
        qt = top.astype(np.float16).astype(np.float32)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            vert = """#version 330
            in vec2 in_vert; in vec2 in_texcoord; out vec2 v_uv;
            void main(){ gl_Position=vec4(in_vert,0.,1.); v_uv=in_texcoord; }"""
            prog = ctx.program(vertex_shader=vert,
                               fragment_shader=LAYER_MIX_FRAG)
            import moderngl as mgl
            vao = ctx.vertex_array(prog, [(ctx.buffer(np.array(
                [-1, -1, 0, 0, 1, -1, 1, 0, -1, 1, 0, 1, 1, 1, 1, 1],
                dtype="f4").tobytes()), "2f 2f", "in_vert", "in_texcoord")])
            tex_b = ctx.texture((size, size), components=4, dtype="f2",
                                data=bottom.astype(np.float16))
            tex_t = ctx.texture((size, size), components=4, dtype="f2",
                                data=top.astype(np.float16))
            out = ctx.texture((size, size), components=4, dtype="f2")
            fbo = ctx.framebuffer(color_attachments=[out])
            fbo.use()
            tex_b.use(0)
            prog["u_bottom"].value = 0
            tex_t.use(1)
            prog["u_top"].value = 1
            prog["u_opacity"].value = 0.5
            prog["u_mode"].value = 0
            # 极点模式（Color Dodge/Burn/Divide 在 top→1 附近对输入
            # 量化误差有固有放大——权威公式行为）；用 fp16 精确栅格值
            # （1/8 步长）消除两侧输入差
            qgrid_b = (np.round(qb * 8.0) / 8.0).astype(np.float32)
            qgrid_t = (np.round(qt * 8.0) / 8.0).astype(np.float32)
            grid_b = ctx.texture((size, size), components=4, dtype="f2",
                                 data=qgrid_b.astype(np.float16))
            grid_t = ctx.texture((size, size), components=4, dtype="f2",
                                 data=qgrid_t.astype(np.float16))
            pole_modes = {"Color Dodge", "Color Burn", "Divide"}
            for mode in (*V11_MODES, "Normal", "Add", "Subtract",
                         "Multiply", "Screen"):
                if mode in pole_modes:
                    grid_b.use(0)
                    grid_t.use(1)
                    cpu_in_b, cpu_in_t = qgrid_b, qgrid_t
                else:
                    tex_b.use(0)
                    tex_t.use(1)
                    cpu_in_b, cpu_in_t = qb, qt
                prog["u_mode"].value = LAYER_MODE_INDEX[mode]
                vao.render(mgl.TRIANGLE_STRIP)
                gl_out = np.frombuffer(
                    fbo.read(components=4, dtype="f2"),
                    dtype=np.float16).reshape(size, size, 4).astype(np.float32)
                cpu = layer_mix([cpu_in_b, cpu_in_t], [0.5], [mode])
                diff = np.abs(gl_out.astype(np.float64)
                              - cpu.astype(np.float64))
                # 双容差：SDR abs ≤1/255；HDR（极点模式可放大到数百）
                # rel ≤2e-3（fp16 大值步长固有）
                sdr = np.abs(cpu) <= 1.0
                mag = np.maximum(np.abs(cpu.astype(np.float64)), 1e-6)
                assert diff[sdr].max(initial=0.0) <= 1.0 / 255.0, (
                    f"{mode}: SDR {diff[sdr].max(initial=0.0):.3e}")
                rel = diff[~sdr] / mag[~sdr]
                assert rel.max(initial=0.0) <= 2e-3, (
                    f"{mode}: HDR rel {rel.max(initial=0.0):.3e}")
            for t in (out, fbo, tex_b, tex_t, grid_b, grid_t):
                t.release()
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()
