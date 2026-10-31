"""C19 lib1_fill_holes 测试：marker 协议 + oracle↔手算（独立标量重推
连通域/面积/触边）+ 恒等锚（max_area=0）+ 对角连通差分 + Holes 模式 +
防御 + manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import fill_holes_ref as ref
from content_staging.library_v1.nodes.fill_holes.lib1_fill_holes_node import (
    EFFECT_KEY, Lib1FillHolesNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "fill_holes" / "component_manifest.json")


def _rgba(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return np.concatenate([rgb, alpha[..., None]], axis=-1).astype(np.float32)


class TestC19NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1FillHolesNode()
        tex = TextureResult(texture_id=111, width=32, height=24)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_fill_holes"
        assert out["_effect_input"] is tex
        for name in ("connectivity", "max_area", "threshold", "output_mode"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1FillHolesNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_full_frame_declaration(self) -> None:
        node = Lib1FillHolesNode()
        assert node._tile_padding({}) == 2204   # 对角线上界（全帧声明）


class TestC19OracleHandVectors:
    def test_center_hole_filled_and_area_gate(self) -> None:
        # 手算 1：4×4 FG 边框环 + 中心 2×2 α=0 孔（area=4）：
        # max_area=0.5（限 8 ≥ 4）→ 填（α→1）；max_area=0.1（限 1.6 < 4）
        # → 不填（α 原样 0）
        alpha = np.ones((4, 4), np.float32)
        alpha[1:3, 1:3] = 0.0
        rgb = np.zeros((4, 4, 3), np.float32)
        img = _rgba(rgb, alpha)
        filled = ref.evaluate(img, {"max_area": 0.5})
        np.testing.assert_allclose(filled[1:3, 1:3, 3], 1.0, atol=1e-7)
        np.testing.assert_array_equal(filled[0, :], img[0, :])   # 边框原样
        not_filled = ref.evaluate(img, {"max_area": 0.1})
        np.testing.assert_array_equal(not_filled, img)

    def test_border_background_never_filled(self) -> None:
        # 手算 2（验收点）：仅 (0,0) 一个 FG 像素——其余 15 个 BG 像素
        # 触边 → 真背景，永不误填（即使 max_area=1.0）
        alpha = np.zeros((4, 4), np.float32)
        alpha[0, 0] = 1.0
        img = _rgba(np.zeros((4, 4, 3), np.float32), alpha)
        out = ref.evaluate(img, {"max_area": 1.0})
        np.testing.assert_array_equal(out[..., 3], alpha)
        # FG 十字：两臂触边，BG 分四角且全部触边 → 不填
        alpha2 = np.zeros((3, 3), np.float32)
        alpha2[1, :] = 1.0
        alpha2[:, 1] = 1.0
        out2 = ref.evaluate(_rgba(np.zeros((3, 3, 3), np.float32), alpha2),
                            {"max_area": 1.0})
        np.testing.assert_array_equal(out2[..., 3], alpha2)

    def test_connectivity_diagonal_discrimination(self) -> None:
        # 手算 3：3×3 FG 全满，除 (1,1)、(2,2) α=0：
        # (2,2) 触边 → 真背景；(1,1) 仅与 (2,2) 对角相邻——
        # 4-conn：不连通 → 封闭孔 area=1 ≤ 0.2·9=1.8 → 填；
        # 8-conn：与 (2,2) 连通 → 经触边 → 真背景 → 不填
        alpha = np.ones((3, 3), np.float32)
        alpha[1, 1] = 0.0
        alpha[2, 2] = 0.0
        img = _rgba(np.zeros((3, 3, 3), np.float32), alpha)
        out4 = ref.evaluate(img, {"connectivity": "4", "max_area": 0.2})
        assert out4[1, 1, 3] == 1.0
        assert out4[2, 2, 3] == 0.0                     # 触边不误填
        out8 = ref.evaluate(img, {"connectivity": "8", "max_area": 0.2})
        assert out8[1, 1, 3] == 0.0                     # 对角连通 → 真背景
        assert out8[2, 2, 3] == 0.0

    def test_holes_output_mode(self) -> None:
        # 手算 4：Holes → 孔 mask 本身（1 孔 / 0 其余，含 FG 区）
        alpha = np.ones((4, 4), np.float32)
        alpha[1:3, 1:3] = 0.0
        alpha[0, 0] = 0.0                               # 触边 BG
        img = _rgba(np.zeros((4, 4, 3), np.float32), alpha)
        out = ref.evaluate(img, {"max_area": 0.5, "output_mode": "Holes"})
        expect = np.zeros((4, 4), np.float32)
        expect[1:3, 1:3] = 1.0
        np.testing.assert_allclose(out[..., 3], expect, atol=1e-7)


class TestC19Semantics:
    def test_identity_anchor_max_area_zero(self) -> None:
        # max_area=0 → 不填 → α 原样透传（任意输入，含 HDR α）
        rng = np.random.RandomState(61)
        img = _rgba(rng.rand(6, 6, 3).astype(np.float32),
                    (rng.rand(6, 6) * 6.0).astype(np.float32))
        out = ref.evaluate(img, {"max_area": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_hdr_alpha_fill_sets_one(self) -> None:
        # Fill 把孔 α 置 1.0（非 HDR 放大）；FG HDR α=5.0 原样保留
        alpha = np.full((3, 3), 5.0, np.float32)
        alpha[1, 1] = 0.0
        img = _rgba(np.zeros((3, 3, 3), np.float32), alpha)
        out = ref.evaluate(img, {"max_area": 0.5})
        assert out[1, 1, 3] == np.float32(1.0)
        assert out[0, 0, 3] == np.float32(5.0)

    def test_threshold_gates_fg(self) -> None:
        # threshold 分界：α=[0.3,0.6,0.9] 1×3（两端触边）——
        # thr 0.5 → FG={x1,x2}，x0 触边真背景；无孔 → 不填
        alpha = np.array([[0.3, 0.6, 0.9]], np.float32)
        img = _rgba(np.zeros((1, 3, 3), np.float32), alpha)
        out = ref.evaluate(img, {"max_area": 1.0, "threshold": 0.5})
        np.testing.assert_array_equal(out[..., 3], alpha)
        # thr 0.8 → FG={x2}；x0、x1 均 BG 且都触边（1×3 两端）→ 不填
        out2 = ref.evaluate(img, {"max_area": 1.0, "threshold": 0.8})
        np.testing.assert_array_equal(out2[..., 3], alpha)

    def test_invalid_params_raise(self) -> None:
        img = _rgba(np.zeros((2, 2, 3), np.float32), np.ones((2, 2), np.float32))
        with pytest.raises(ValueError):
            ref.evaluate(img, {"connectivity": "6"})
        with pytest.raises(ValueError):
            ref.evaluate(np.zeros((2, 2, 3), np.float32), {})   # 缺 α 显式报错

    def test_every_param_has_pixel_effect(self) -> None:
        # 分级 α：0.7=FG、(2,2)=0.3 封闭孔、(0,0)=0.2 触边 BG
        alpha = np.full((5, 5), 0.7, np.float32)
        alpha[2, 2] = 0.3
        alpha[0, 0] = 0.2
        img = _rgba(np.zeros((5, 5, 3), np.float32), alpha)
        base = ref.evaluate(img, {"max_area": 0.2})
        assert base[2, 2, 3] == 1.0
        assert base[0, 0, 3] == np.float32(0.2)     # 触边不填
        assert not np.allclose(
            ref.evaluate(img, {"max_area": 0.01}), base)          # 面积门控
        # thr 0.99 → 无 FG → 整幅单连通 BG 触边 → 全不填（α 原样）
        assert not np.allclose(
            ref.evaluate(img, {"threshold": 0.99, "max_area": 0.2}), base)
        assert not np.allclose(
            ref.evaluate(img, {"max_area": 0.2, "output_mode": "Holes"}), base)
        # connectivity：对角链 (1,1)-(2,2)-(3,3)-(4,4)——
        # 4-conn：(1,1)/(2,2)/(3,3) 为孤立封闭孔 → 填；(4,4) 触角边 →
        # 真背景不填；8-conn：整链单连通域且触边 → 全不填
        alpha2 = np.ones((5, 5), np.float32)
        for k in range(1, 5):
            alpha2[k, k] = 0.0
        img2 = _rgba(np.zeros((5, 5, 3), np.float32), alpha2)
        out4 = ref.evaluate(img2, {"connectivity": "4", "max_area": 0.2})
        out8 = ref.evaluate(img2, {"connectivity": "8", "max_area": 0.2})
        expect4 = alpha2.copy()
        for k in range(1, 4):
            expect4[k, k] = 1.0
        np.testing.assert_allclose(out4[..., 3], expect4, atol=1e-7)
        np.testing.assert_array_equal(out8[..., 3], alpha2)
        assert not np.allclose(out4, out8)


class TestC19Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C19"
        node = Lib1FillHolesNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
