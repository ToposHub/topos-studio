"""C02 lib1_rgb_matrix 测试：marker 协议 + oracle↔手算（≥3 独立向量）+
恒等（默认 identity/strength=0，含 HDR）+ manifest↔节点参数一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import rgb_matrix_ref as ref
from content_staging.library_v1.nodes.rgb_matrix.lib1_rgb_matrix_node import (
    EFFECT_KEY, Lib1RgbMatrixNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "rgb_matrix" / "component_manifest.json")


class TestC02NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1RgbMatrixNode()
        tex = TextureResult(texture_id=11, width=6, height=4)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_rgb_matrix"
        assert out["_effect_input"] is tex
        assert out["image"].width == 6 and out["image"].height == 4
        # 参数快照与节点参数一致（11 个效果参数：9 矩阵 + bias + strength）
        assert set(out["_effect_params"]) == {
            "row0_r", "row0_g", "row0_b", "row1_r", "row1_g", "row1_b",
            "row2_r", "row2_g", "row2_b", "bias", "strength"}
        for name, value in out["_effect_params"].items():
            assert value == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1RgbMatrixNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_and_bbox(self) -> None:
        node = Lib1RgbMatrixNode()
        assert node._tile_padding({"row0_r": 2.0}) == 0
        assert node.compute_output_bbox({"image": "bbox"}, {}) == "bbox"


class TestC02OracleHandVectors:
    def test_channel_swap(self) -> None:
        # 手算 1：R/B 交换矩阵 → (0.2, 0.4, 0.6) → (0.6, 0.4, 0.2)
        img = np.array([[[0.2, 0.4, 0.6]]], dtype=np.float32)
        out = ref.evaluate(img, {"row0_r": 0.0, "row0_b": 1.0,
                                 "row2_b": 0.0, "row2_r": 1.0})
        np.testing.assert_allclose(out[0, 0], [0.6, 0.4, 0.2], atol=1e-7)

    def test_row_gain_and_bias(self) -> None:
        # 手算 2：row0_r=2 放大红 + bias (0.1, -0.05, 0.0)
        img = np.array([[[0.3, 0.5, 0.7]]], dtype=np.float32)
        out = ref.evaluate(img, {"row0_r": 2.0, "bias": (0.1, -0.05, 0.0)})
        np.testing.assert_allclose(out[0, 0], [0.7, 0.45, 0.7], atol=1e-7)

    def test_strength_mix(self) -> None:
        # 手算 3：strength=0.5 → 半程混合到 2×红
        img = np.array([[[0.4, 0.2, 0.1]]], dtype=np.float32)
        out = ref.evaluate(img, {"row0_r": 2.0, "strength": 0.5})
        np.testing.assert_allclose(out[0, 0], [0.6, 0.2, 0.1], atol=1e-7)

    def test_cross_talk_hand_computed(self) -> None:
        # 手算 4（交叉串扰）：out.g = 0.3·r + 0.4·g + 0.3·b
        #   = 0.3·0.5 + 0.4·0.2 + 0.3·0.1 = 0.15 + 0.08 + 0.03 = 0.26
        img = np.array([[[0.5, 0.2, 0.1]]], dtype=np.float32)
        out = ref.evaluate(img, {"row1_r": 0.3, "row1_g": 0.4, "row1_b": 0.3})
        np.testing.assert_allclose(out[0, 0], [0.5, 0.26, 0.1], atol=1e-7)


class TestC02Identity:
    def test_default_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.18, 12.0], [4.5, 1.0, -1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img), img)

    def test_strength_zero_identity_any_matrix(self) -> None:
        img = np.array([[[0.3, 0.6, 0.9]]], dtype=np.float32)
        wild = {k: -1.7 for k in
                ("row0_r", "row0_g", "row0_b", "row1_r", "row1_g", "row1_b",
                 "row2_r", "row2_g", "row2_b")}
        wild.update({"bias": (2.0, -2.0, 0.5), "strength": 0.0})
        np.testing.assert_array_equal(ref.evaluate(img, wild), img)

    def test_alpha_passthrough(self) -> None:
        img = np.array([[[0.2, 0.4, 0.6, 0.3]]], dtype=np.float32)
        out = ref.evaluate(img, {"row2_g": 1.5})
        assert out[0, 0, 3] == np.float32(0.3)

    def test_every_param_has_pixel_effect(self) -> None:
        img = np.tile(np.array([0.35, 0.5, 0.65], dtype=np.float32), (4, 4, 1))
        base = ref.evaluate(img)
        for name in ref.DEFAULTS:
            if name == "strength":
                # strength 混合 identity↔M：默认矩阵即 identity，strength 无从
                # 发挥——须配非平凡矩阵再探（否则数学上恒等，非缺陷）
                strong = {"row0_r": 2.0}
                assert not np.allclose(
                    ref.evaluate(img, {**strong, "strength": 0.5}),
                    ref.evaluate(img, strong))
            elif name == "bias":
                assert not np.allclose(
                    ref.evaluate(img, {"bias": (0.1, 0.1, 0.1)}), base)
            else:
                assert not np.allclose(ref.evaluate(img, {name: 0.37}), base), name


class TestC02Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C02"
        assert manifest.slug == "rgb_matrix"
        assert manifest.kind == "node"
        node = Lib1RgbMatrixNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name
        port_names = {(p.name, p.direction) for p in manifest.ports}
        assert port_names == {("image", "input"), ("image", "output")}
        assert set(manifest.capabilities.values()) == {"untested"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
