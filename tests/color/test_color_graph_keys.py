"""B10 Key 语义测试 —— key mixer / outside 补集 / alpha output（spec §3/§4/§6）。

验收：无重叠/重叠 key、全黑/全白 key、key 有界 0–1、补集准确。
"""

from __future__ import annotations

import numpy as np
import pytest

from src.features.color.core.color_graph_cpu import (
    corrector_oracle,
    key_mix,
    outside_key,
)


def _key(value: float, size=(8, 8)) -> np.ndarray:
    return np.full((size[0], size[1], 4), value, np.float32)


class TestKeyMixerOracle:
    def test_add_union(self):
        a = _key(0.3)
        b = _key(0.7)
        out = key_mix([a, b], "add")
        np.testing.assert_allclose(out[..., 0], 0.7, atol=1e-7)

    def test_subtract(self):
        out = key_mix([_key(0.8), _key(0.5)], "subtract")
        np.testing.assert_allclose(out[..., 0], 0.3, atol=1e-7)

    def test_intersect(self):
        out = key_mix([_key(0.8), _key(0.5)], "intersect")
        np.testing.assert_allclose(out[..., 0], 0.4, atol=1e-7)

    def test_bounded_01(self):
        out = key_mix([_key(2.0), _key(-1.0)], "add")
        assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0

    def test_all_black_and_white(self):
        black = key_mix([_key(0.0), _key(0.0)], "add")
        white = key_mix([_key(1.0), _key(1.0)], "intersect")
        np.testing.assert_allclose(black[..., 0], 0.0)
        np.testing.assert_allclose(white[..., 0], 1.0)


class TestOutsideOracle:
    def test_complement_exact(self):
        k = _key(0.25)
        comp = outside_key(k)
        np.testing.assert_allclose(comp[..., 0], 0.75, atol=1e-7)

    def test_grade_applies_on_complement_only(self):
        image = np.full((2, 2, 4), 0.5, np.float32)
        # 引用 key=0（未选区）→ 补集=1 → outside 全生效
        comp = outside_key(np.zeros((2, 2, 4), np.float32))
        out = corrector_oracle(image, {"exposure": 1.0}, mask=comp)
        np.testing.assert_allclose(out[..., :3], 1.0, atol=1e-6)
        # 引用 key=1（全选区）→ 补集=0 → outside 不生效
        comp = outside_key(np.ones((2, 2, 4), np.float32))
        out = corrector_oracle(image, {"exposure": 1.0}, mask=comp)
        np.testing.assert_allclose(out[..., :3], 0.5, atol=1e-6)

    def test_partial_complement(self):
        image = np.zeros((1, 2, 4), np.float32)
        image[..., :3] = 1.0
        key = np.zeros((1, 2, 4), np.float32)
        key[0, 0] = 1.0            # 左像素被引用 key 选中
        out = corrector_oracle(image, {"exposure": -1.0},
                               mask=outside_key(key))
        # 左像素补集=0（不变 1.0）；右像素补集=1（变 0.5）
        assert out[0, 0, 0] == pytest.approx(1.0)
        assert out[0, 1, 0] == pytest.approx(0.5)


class TestKeyGraphStructure:
    def test_key_mixer_graph_executes_gl(self):
        """key 边图（corrector.key_out → key_mixer）GL 执行成功。"""
        import moderngl
        from src.features.color.core.color_graph_compiler import compile_graph
        from src.features.color.core.color_graph_executor import (
            ColorGraphExecutor,
        )
        from src.features.color.core.color_graph_service import ColorGraphService
        from src.features.color.core.color_graph_snapshot import freeze_graph
        from src.shared.project.color_graph import NodeType
        from tests.color.test_color_graph_schema import _serial_graph

        counter = iter(range(100))
        service = ColorGraphService(id_factory=lambda: f"k{next(counter)}")
        graph = _serial_graph(2)
        cs = [n for n in graph.nodes
              if n.node_type == NodeType.CORRECTOR.value]
        grown = service.apply_intent(graph, "add_key_mixer",
                                     {"key_source_ids": [
                                         cs[0].node_id, cs[1].node_id]})
        assert grown.ok
        ctx = moderngl.create_standalone_context(require=330)
        try:
            executor = ColorGraphExecutor(ctx)
            plan = compile_graph(freeze_graph(grown.graph, session_id="s"),
                                 size=(64, 64))
            src = ctx.texture((64, 64), 4, dtype="f2")
            result = executor.execute_plan(plan, src, request_id="km",
                                           source_size=(64, 64))
            assert result.published, result.diagnostic_message
            executor.release_revision("km")
            src.release()
        finally:
            ctx.release()
