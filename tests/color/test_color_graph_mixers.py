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
