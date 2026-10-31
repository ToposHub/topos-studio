"""C01 lib1_gradient_map / C17 lib1_edge_extend 节点试点测试。

C01：节点 marker 协议（与现有单输入效果同构）+ 与 F16 共享核的参数一致性；
C17：CPU oracle 手算向量 + 语义（原 alpha 保留/premult/radius 增长/阈值）+
节点 marker 协议。真实 dispatch/registry 接线属 L04（未验，见提案）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import edge_extend_ref, gradient_map_ref
from content_staging.library_v1.nodes.gradient_map.lib1_gradient_map_node import (
    INTERP_MODES, Lib1GradientMapNode,
)
from content_staging.library_v1.nodes.edge_extend.lib1_edge_extend_node import (
    OUTPUT_MODES, Lib1EdgeExtendNode,
)

C = np.float32([0.6, 0.5, 0.4])


def _rgba(pixels: list[list[float]]) -> np.ndarray:
    return np.asarray(pixels, dtype=np.float32)


# ────────────────────────────────────────────── C01

class TestC01NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1GradientMapNode()
        tex = TextureResult(texture_id=7, width=4, height=4)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == "lib1_gradient_map"
        assert out["_effect_input"] is tex
        assert out["image"].width == 4 and out["image"].height == 4
        # 参数快照与节点参数一致（F16 同名参数）
        for name in ("stop_0", "pos_1", "pos_2", "interp_mode", "amount"):
            assert out["_effect_params"][name] == node.parameters[name]

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1GradientMapNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_param_defs_match_f16_core(self) -> None:
        for name in gradient_map_ref.DEFAULTS:
            assert any(pd.name == name for pd in Lib1GradientMapNode.PARAM_DEFS), name
        assert INTERP_MODES == ["Linear", "Smooth"]
        assert gradient_map_ref.DEFAULTS["interp_mode"] in INTERP_MODES

    def test_marker_params_drive_f16_core(self) -> None:
        """节点参数快照直接喂 F16 共享核 oracle 有效（同核语义）。"""
        node = Lib1GradientMapNode()
        node.parameters["amount"] = 0.5
        out = node._do_evaluate(
            {"image": TextureResult(texture_id=1, width=2, height=1)},
            ctx=None, fb_manager=None)
        p = dict(out["_effect_params"])
        img = np.full((1, 2, 3), 0.18, dtype=np.float32)
        np.testing.assert_allclose(
            gradient_map_ref.evaluate(img, p), gradient_map_ref.evaluate(img, {"amount": 0.5}),
            atol=1e-7)


# ────────────────────────────────────────────── C17 oracle

class TestC17HandVectors:
    def test_linear_extension(self) -> None:
        img = np.zeros((1, 7, 4), dtype=np.float32)
        img[0, :3, :3] = C
        img[0, :3, 3] = 1.0
        out = edge_extend_ref.evaluate(img, {"radius": 3})
        for col in (3, 4, 5):
            np.testing.assert_allclose(out[0, col, :3], C, atol=1e-6, err_msg=f"col={col}")
        np.testing.assert_array_equal(out[0, 6, :3], np.float32(0.0))  # 距离 4 > 3
        np.testing.assert_array_equal(out[0, :, 3], np.float32([1, 1, 1, 0, 0, 0, 0]))

    def test_premultiplied_mode(self) -> None:
        img = np.zeros((1, 4, 4), dtype=np.float32)
        img[0, :2, :3] = C
        img[0, :2, 3] = 1.0
        img[0, 2, 3] = 0.3
        out = edge_extend_ref.evaluate(
            img, {"radius": 2, "output_mode": "premultiplied"})
        np.testing.assert_allclose(out[0, 2, :3], np.float32([0.18, 0.15, 0.12]), atol=1e-6)
        np.testing.assert_array_equal(out[0, 3, :3], np.float32(0.0))  # alpha=0 → premult 0

    def test_diamond_growth(self) -> None:
        img = np.zeros((5, 5, 4), dtype=np.float32)
        img[2, 2, :3] = C
        img[2, 2, 3] = 1.0
        out = edge_extend_ref.evaluate(img, {"radius": 2})
        for (y, x) in [(1, 2), (3, 2), (2, 1), (2, 3),           # 距离1
                       (0, 2), (4, 2), (2, 0), (2, 4),           # 距离2
                       (1, 1), (3, 1), (1, 3), (3, 3)]:
            np.testing.assert_allclose(out[y, x, :3], C, atol=1e-6, err_msg=f"{(y, x)}")
        for (y, x) in [(0, 0), (0, 1), (0, 3), (1, 0), (4, 4)]:  # 距离 3+
            np.testing.assert_array_equal(out[y, x, :3], np.float32(0.0))

    def test_threshold_source_semantics(self) -> None:
        img = np.zeros((1, 2, 4), dtype=np.float32)
        img[0, 0, :3] = C
        img[0, 0, 3] = 0.6
        img[0, 1, :3] = (0.1, 0.9, 0.2)
        img[0, 1, 3] = 0.4
        out = edge_extend_ref.evaluate(img, {"radius": 1, "threshold": 0.6})
        np.testing.assert_allclose(out[0, 1, :3], C, atol=1e-6)  # 0.4<0.6 非源，从列0 延伸
        # 提高 radius 后列1 自身不是源——即使它有 RGB
        out2 = edge_extend_ref.evaluate(img, {"radius": 1, "threshold": 0.7})
        np.testing.assert_allclose(out2[0, 0, :3], C, atol=1e-6)  # 列0 也非源(0.6<0.7)：全不动

    def test_radius_zero_identity(self) -> None:
        img = _rgba([[[0.2, 0.3, 0.4, 1.0], [-0.5, 0.0, 4.0, 0.2]]])
        out = edge_extend_ref.evaluate(img, {"radius": 0})
        np.testing.assert_array_equal(out, img)

    def test_all_solid_identity(self) -> None:
        img = np.zeros((3, 3, 4), dtype=np.float32)
        img[..., :3] = 0.3
        img[..., 3] = 1.0
        out = edge_extend_ref.evaluate(img, {"radius": 8})
        np.testing.assert_array_equal(out, img)

    def test_fixture_fringe_extended(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import alpha_fringe_hair
        fringe = alpha_fringe_hair()
        out = edge_extend_ref.evaluate(fringe, {"radius": 3})
        # 阈值 0.5 下实区到列 34（彩边列 35-37 的 alpha < 0.5）；
        # 延伸 3px 覆盖列 35-37，列 38+（距离 ≥4）仍为 0
        for col in (35, 36, 37):
            got = out[10, col, :3]
            assert float(np.abs(got - np.float32([0.6, 0.5, 0.4])).max()) < 1e-5, col
        np.testing.assert_array_equal(out[10, 40, :3], np.float32(0.0))
        # 原 alpha 保留
        np.testing.assert_array_equal(out[..., 3], fringe[..., 3])


class TestC17NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1EdgeExtendNode()
        tex = TextureResult(texture_id=9, width=8, height=8)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == "lib1_edge_extend"
        assert out["_effect_input"] is tex
        assert out["_effect_params"]["radius"] == 4.0
        assert out["_effect_params"]["output_mode"] == "color_only"
        assert node._tile_padding({"radius": 6.0}) == 6  # tile padding = radius

    def test_output_modes_valid(self) -> None:
        assert OUTPUT_MODES == ["color_only", "premultiplied"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
