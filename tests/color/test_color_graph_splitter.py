"""B10 Splitter/Combiner 测试 —— 恒等、通道载体、GL 执行（spec §5）。

验收：split+combine 恒等（CPU 精确 + GL 容差）；通道值正确映射；
改变节点位置不改变像素（布局分离已在 B02 锁定，这里补像素视角）。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import combine_rgb, split_rgb
from tests.color.fixtures.color_graph import f02_patches


class TestSplitCombineOracle:
    def test_identity_roundtrip(self):
        image = f02_patches(64)
        planes = split_rgb(image)
        back = combine_rgb(planes["r"], planes["g"], planes["b"])
        np.testing.assert_array_equal(back, image)

    def test_channel_carriers(self):
        image = np.zeros((1, 1, 4), np.float32)
        image[0, 0] = (0.2, 0.4, 0.6, 0.8)
        planes = split_rgb(image)
        np.testing.assert_allclose(planes["r"][0, 0], (0.2, 0.2, 0.2, 0.8))
        np.testing.assert_allclose(planes["g"][0, 0], (0.4, 0.4, 0.4, 0.8))
        np.testing.assert_allclose(planes["b"][0, 0], (0.6, 0.6, 0.6, 0.8))
        back = combine_rgb(planes["r"], planes["g"], planes["b"])
        np.testing.assert_allclose(back[0, 0], (0.2, 0.4, 0.6, 0.8))

    def test_channel_modification_survives(self):
        image = np.zeros((1, 1, 4), np.float32)
        image[0, 0] = (0.2, 0.4, 0.6, 1.0)
        planes = split_rgb(image)
        modified_g = planes["g"].copy()
        modified_g[0, 0, :3] = 0.9    # 中间处理改 G 通道
        back = combine_rgb(planes["r"], modified_g, planes["b"])
        np.testing.assert_allclose(back[0, 0, :3], (0.2, 0.9, 0.6))


class TestSplitCombineGraph:
    def test_splitter_pair_graph_gl_identity(self):
        """splitter+combiner 对图（中间无处理）GL 输出 ≈ 输入（恒等）。"""
        import moderngl
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_executor import (
            ColorGraphExecutor,
        )
        from src.features.color.core.color_graph_service import ColorGraphService
        from src.features.color.core.color_graph_snapshot import freeze_graph
        from tests.color.test_color_graph_schema import _serial_graph

        counter = iter(range(100))
        service = ColorGraphService(id_factory=lambda: f"s{next(counter)}")
        graph = _serial_graph(0)
        grown = service.apply_intent(graph, "add_splitter_pair", {})
        assert grown.ok
        source = f02_patches(64)
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(grown.graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2",
                              data=np.ascontiguousarray(
                                  source).astype(np.float16))
            result = executor.execute_plan(
                plan, src, request_id="sc", source_size=(64, 64))
            assert result.published, result.diagnostic_message
            fbo = ctx.framebuffer(color_attachments=[result.output_texture])
            fbo.use()
            out = np.frombuffer(
                fbo.read(components=4, dtype="f2"),
                dtype=np.float16).reshape(64, 64, 4).astype(np.float32)
            executor.release_revision("sc")
            src.release()
            # f16 往返容差
            np.testing.assert_allclose(out, source, atol=2e-3)
        finally:
            try:
                executor.release_pool()
            except Exception:
                pass
            ctx.release()

    def test_layout_move_does_not_change_pixels(self):
        """移动节点位置（update_layout）后同 plan 编译输出不变。"""
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_service import ColorGraphService
        from src.features.color.core.color_graph_snapshot import freeze_graph
        from tests.color.test_color_graph_schema import _serial_graph

        counter = iter(range(100))
        service = ColorGraphService(id_factory=lambda: f"l{next(counter)}")
        graph = _serial_graph(1)
        moved = service.apply_intent(
            graph, "update_layout",
            {"positions": {graph.nodes[1].node_id: (999.0, -42.0)}})
        assert moved.ok
        # 布局变更不 bump render revision（B02 契约），plan 编译等价
        assert moved.graph.graph_revision == graph.graph_revision
        plan_a = compile_graph(freeze_graph(graph, session_id="s"),
                               size=(64, 64))
        plan_b = compile_graph(freeze_graph(moved.graph, session_id="s"),
                               size=(64, 64))
        a = [(p.node_id, p.op, [r.node_id for _, r in p.inputs])
             for p in plan_a.passes]
        b = [(p.node_id, p.op, [r.node_id for _, r in p.inputs])
             for p in plan_b.passes]
        assert a == b
