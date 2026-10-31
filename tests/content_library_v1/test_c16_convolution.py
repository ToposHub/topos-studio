"""C16 lib1_convolution 测试：手算向量（平移核/归一化盒核/零核防御/偏置
次序/HDR 线性）、恒等、alpha 透传、manifest 契约。

手算独立脚本（纯 Python，不 import 核）：out[y,x] = Σ K[i,j]·src[y+i−1,
x+j−1]（edge 钳制），normalize 除 Σ|K|（<1e-6 回退 1），bias 后加。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.references import convolution_ref as ref
from content_staging.library_v1.nodes.convolution.lib1_convolution_node import (
    EFFECT_KEY, Lib1ConvolutionNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "convolution" / "component_manifest.json")


def _ramp() -> np.ndarray:
    return np.arange(64, dtype=np.float32).reshape(8, 8, 1).repeat(3, axis=2)


class TestC16HandVectors:
    def test_center_tap_identity(self) -> None:
        # 恒等核是中心 tap k11=1（k00=1 是 (−1,−1) 平移核，非恒等）
        img = np.array([[[-0.25, 0.18, 12.0], [4.5, 1.0, -1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img), img)

    def test_k00_shift_kernel(self) -> None:
        # 手算 1：k00=1 → out[y,x]=src[y−1,x−1]：ramp (3,2)→17=img[2,1]；
        # 边界钳制 (0,0)→img[0,0]=0；(0,5)→img[0,4]=4
        out = ref.evaluate(_ramp(), {"k00": 1.0, "k11": 0.0})
        assert float(out[3, 2, 0]) == pytest.approx(17.0, abs=1e-6)
        assert float(out[0, 0, 0]) == pytest.approx(0.0, abs=1e-6)
        assert float(out[0, 5, 0]) == pytest.approx(4.0, abs=1e-6)

    def test_box_kernel_normalize(self) -> None:
        # 手算 2：全 1 核 + normalize → 3x3 均值；均匀 0.5 图恒 0.5
        img = np.full((6, 6, 3), 0.5, dtype=np.float32)
        box = {f"k{r}{c}": 1.0 for r in range(3) for c in range(3)}
        box["k11"] = 0.0  # 去掉默认中心 tap
        out = ref.evaluate(img, {**box, "normalize": True})
        np.testing.assert_allclose(out, img, atol=1e-6)

    def test_zero_kernel_defense_and_bias(self) -> None:
        # 手算 3：全零核 D=0 < 1e-6 → D=1 防御回退，out = 0 + bias = 0.25
        img = _ramp()
        out = ref.evaluate(img, {"k11": 0.0, "bias": 0.25})
        np.testing.assert_allclose(out, np.full_like(img, 0.25), atol=1e-7)

    def test_bias_after_normalize(self) -> None:
        # 手算 4：盒核归一化 0.5 + bias 0.1 → 0.6（bias 在归一化之后）
        img = np.full((6, 6, 3), 0.5, dtype=np.float32)
        box = {f"k{r}{c}": 1.0 for r in range(3) for c in range(3)}
        box["k11"] = 0.0
        out = ref.evaluate(img, {**box, "normalize": True, "bias": 0.1})
        np.testing.assert_allclose(out, np.full_like(img, 0.6), atol=1e-6)

    def test_laplacian_hdr_linear_no_clamp(self) -> None:
        # 手算 5：Laplacian 核（负权重）+ normalize（Σ|K|=8）：均匀 12.0 →
        # (12·4 − 12·4)/8 = 0（线性运算，无 clamp）
        img = np.full((6, 6, 3), 12.0, dtype=np.float32)
        lap = {"k01": 1.0, "k10": 1.0, "k12": 1.0, "k21": 1.0,
               "k11": -4.0, "normalize": True}
        out = ref.evaluate(img, lap)
        np.testing.assert_allclose(out, np.zeros_like(img), atol=1e-6)


class TestC16Semantics:
    def test_strength_of_identity_vs_kernel(self) -> None:
        img = _ramp()
        assert not np.allclose(
            ref.evaluate(img, {"k00": 1.0, "k11": 0.0}), img)

    def test_alpha_passthrough(self) -> None:
        img = np.zeros((2, 2, 4), dtype=np.float32)
        img[..., 3] = 0.77
        out = ref.evaluate(img, {"k00": 0.5, "k11": 0.5})
        np.testing.assert_array_equal(out[..., 3], np.float32(0.77))

    def test_every_param_has_pixel_effect(self) -> None:
        img = _ramp()
        base = ref.evaluate(img)
        for name in ("k00", "k01", "k02", "k10", "k12", "k20", "k21", "k22"):
            assert not np.allclose(ref.evaluate(img, {name: 0.5}), base), name
        assert not np.allclose(ref.evaluate(img, {"k11": 2.0}), base)
        # normalize 在恒等核（Σ|K|=1）上是数学恒等——配盒核（Σ|K|=9）才可辨
        box = {f"k{r}{c}": 1.0 for r in range(3) for c in range(3)}
        box["k11"] = 0.0
        assert not np.allclose(ref.evaluate(img, {**box, "normalize": True}),
                               ref.evaluate(img, box))
        assert not np.allclose(ref.evaluate(img, {"bias": 0.3}), base)


class TestC16NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1ConvolutionNode()
        assert node.node_type == EFFECT_KEY == "lib1_convolution"


class TestC16Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C16"
        node = Lib1ConvolutionNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        assert [p.name for p in manifest.params] == list(ref.DEFAULTS.keys())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
