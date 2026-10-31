"""C03 lib1_despill 测试：marker 协议 + oracle↔手算（≥3 独立向量）+
恒等（strength=0，含 HDR）+ 两模式等价区/差异区 + manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import despill_ref as ref
from content_staging.library_v1.nodes.despill.lib1_despill_node import (
    DESPILL_METHODS, EFFECT_KEY, Lib1DespillNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "despill" / "component_manifest.json")

SPILL_IMG = np.array([[[0.2, 0.5, 0.3]]], dtype=np.float32)


class TestC03NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1DespillNode()
        tex = TextureResult(texture_id=13, width=8, height=2)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_despill"
        assert out["_effect_input"] is tex
        assert out["image"].width == 8 and out["image"].height == 2
        assert out["_effect_params"] == {"method": "Subtract",
                                         "limit": 1.0, "strength": 1.0}
        for name, value in out["_effect_params"].items():
            assert value == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1DespillNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_method_labels_and_padding(self) -> None:
        assert DESPILL_METHODS == ["Subtract", "Scale"]
        assert Lib1DespillNode()._tile_padding({}) == 0

    def test_invalid_method_rejected(self) -> None:
        with pytest.raises(ValueError):
            ref.evaluate(SPILL_IMG, {"method": "nonesuch"})


class TestC03OracleHandVectors:
    def test_subtract_basic(self) -> None:
        # 手算 1：m=max(0.2,0.3)=0.3，Δ=0.2 → g'=0.5−1.0·0.2=0.3
        out = ref.evaluate(SPILL_IMG, {"method": "Subtract"})
        np.testing.assert_allclose(out[0, 0], [0.2, 0.3, 0.3], atol=1e-7)

    def test_subtract_limit_cap(self) -> None:
        # 手算 2：Δ=0.5、limit=0.1 → 仅削 0.1 → g'=0.4
        img = np.array([[[0.0, 0.5, 0.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"method": "Subtract", "limit": 0.1})
        np.testing.assert_allclose(out[0, 0], [0.0, 0.4, 0.0], atol=1e-7)

    def test_scale_green_deficient_pulls_up(self) -> None:
        # 手算 3：g<max(r,b)（Δ=−0.4）：Scale 乘性抬升，但 ratio=clip(−0.4/0.2,
        # −1, 1)=−1 → g'=2g=0.4（守护封顶：深绿不足最多翻倍，不精确到 m=0.6）；
        # Subtract 不动（g_spill=0）。初版期望 g'=0.6 与 ±1 守护设计矛盾——测试侧错误
        img = np.array([[[0.6, 0.2, 0.4]]], dtype=np.float32)
        sub = ref.evaluate(img, {"method": "Subtract"})
        sca = ref.evaluate(img, {"method": "Scale"})
        np.testing.assert_allclose(sub[0, 0], [0.6, 0.2, 0.4], atol=1e-7)
        np.testing.assert_allclose(sca[0, 0], [0.6, 0.4, 0.4], atol=1e-7)

    def test_scale_strength_partial(self) -> None:
        # 手算 4：Scale 半强度：g' = 0.5·(1−0.5·(0.2/0.5)) = 0.5·0.8 = 0.4
        out = ref.evaluate(SPILL_IMG, {"method": "Scale", "strength": 0.5})
        np.testing.assert_allclose(out[0, 0], [0.2, 0.4, 0.3], atol=1e-7)

    def test_modes_coincide_in_limit_region(self) -> None:
        # 钉住：0 ≤ Δ ≤ limit 区两模式代数同值（README 声明的等价区）——
        # g 构造在 [m, m+0.45] ⊂ [m, m+limit]；同时钉住 Δ<0 差异区两模式
        # 必须不同（Subtract 不动 / Scale 抬升）。初版把"绿不足区"当等价区
        # 断言同值，与 README 差异区声明直接矛盾——测试侧错误
        rng = np.random.RandomState(7)
        img = rng.rand(6, 8, 3).astype(np.float32) * 0.5  # SDR，m ≤ 0.5
        surplus = img.copy()
        m = np.maximum(surplus[..., 0], surplus[..., 2])
        surplus[..., 1] = m + rng.rand(6, 8).astype(np.float32) * 0.45  # Δ∈[0,0.45]⊂[0,limit]
        np.testing.assert_allclose(
            ref.evaluate(surplus, {"method": "Subtract"}),
            ref.evaluate(surplus, {"method": "Scale"}), atol=1e-6)
        deficient = img.copy()
        deficient[..., 1] = np.minimum(deficient[..., 1],
                                       np.maximum(deficient[..., 0], deficient[..., 2]))
        assert not np.allclose(
            ref.evaluate(deficient, {"method": "Subtract"}),
            ref.evaluate(deficient, {"method": "Scale"}))

    def test_hdr_surplus_capped_by_limit(self) -> None:
        img = np.array([[[0.5, 8.0, 0.5]]], dtype=np.float32)
        out = ref.evaluate(img, {"limit": 1.0})
        np.testing.assert_allclose(out[0, 0], [0.5, 7.0, 0.5], atol=1e-6)

    def test_green_zero_guard(self) -> None:
        # g=0：Scale 的 ratio 守护（clip ±1）不产生 NaN/发散
        img = np.array([[[0.9, 0.0, 0.1]]], dtype=np.float32)
        out = ref.evaluate(img, {"method": "Scale"})
        assert np.isfinite(out).all()
        assert out[0, 0, 1] == pytest.approx(0.0, abs=1e-7)


class TestC03Identity:
    def test_strength_zero_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 3.0, 0.4], [12.0, 0.0, 8.0]]], dtype=np.float32)
        for method in DESPILL_METHODS:
            np.testing.assert_array_equal(
                ref.evaluate(img, {"method": method, "strength": 0.0}), img)

    def test_no_spill_pixels_unchanged_subtract(self) -> None:
        # g ≤ max(r,b)：Subtract 恒等（g_spill=0）
        img = np.array([[[0.9, 0.1, 0.5], [0.3, 0.3, 0.3]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"method": "Subtract"}), img)

    def test_alpha_passthrough(self) -> None:
        img = np.array([[[0.2, 0.5, 0.3, 0.7]]], dtype=np.float32)
        out = ref.evaluate(img)
        assert out[0, 0, 3] == np.float32(0.7)

    def test_every_param_has_pixel_effect(self) -> None:
        # 参数效果依赖 Δ 区域（README 声明的差异区）——各参数在其能发挥的
        # 图像上探测：绿不足区 method 可辨（盈余区内两模式代数同值）、
        # 盈余区 limit/strength 可辨（绿不足区 Subtract 系参数恒不动）
        surplus = np.tile(np.array([0.2, 0.5, 0.3], dtype=np.float32), (3, 3, 1))
        deficient = np.tile(np.array([0.6, 0.2, 0.4], dtype=np.float32), (3, 3, 1))
        assert not np.allclose(ref.evaluate(deficient, {"method": "Scale"}),
                               ref.evaluate(deficient))
        assert not np.allclose(ref.evaluate(surplus, {"limit": 0.05}),
                               ref.evaluate(surplus))
        assert not np.allclose(ref.evaluate(surplus, {"strength": 0.5}),
                               ref.evaluate(surplus))


class TestC03Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C03"
        node = Lib1DespillNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        method_spec = next(p for p in manifest.params if p.name == "method")
        assert list(method_spec.options) == DESPILL_METHODS


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
