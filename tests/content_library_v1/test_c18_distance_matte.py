"""C18 lib1_distance_matte 测试：marker 协议 + oracle↔手算（独立标量
重推距离与符号）+ 三 metric 分辨 + 三种输出模式 + 防御 + 全帧声明 +
manifest 一致。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import distance_matte_ref as ref
from content_staging.library_v1.nodes.distance_matte.lib1_distance_matte_node import (
    EFFECT_KEY, Lib1DistanceMatteNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "distance_matte" / "component_manifest.json")


def _rgba(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    return np.concatenate([rgb, alpha[..., None]], axis=-1).astype(np.float32)


class TestC18NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1DistanceMatteNode()
        tex = TextureResult(texture_id=101, width=32, height=24)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_distance_matte"
        assert out["_effect_input"] is tex
        for name in ("metric", "radius", "threshold", "output_mode"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1DistanceMatteNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_full_frame_declaration(self) -> None:
        # 全帧声明：padding = 对角线上界 2204 @1920×1080（有限但
        # 不宣称 tile-safe——README/manifest 声明）
        node = Lib1DistanceMatteNode()
        assert node._tile_padding({"radius": 64.0}) == 2204


class TestC18OracleHandVectors:
    def test_1d_row_all_output_modes(self) -> None:
        # 手算 1：1×4 α=[1,1,0,0]、thr 0.5、Euclid、radius=2 →
        # s = [+2, +1, −1, −2] → dn = [1, 0.5, −0.5, −1]
        # DistanceField=dn；Matte=0.5+0.5dn=[1,0.75,0.25,0]；
        # EdgeBand=1−|dn|=[0,0.5,0.5,0]
        rgb = np.arange(12, dtype=np.float32).reshape(1, 4, 3)
        alpha = np.array([[1.0, 1.0, 0.0, 0.0]], dtype=np.float32)
        img = _rgba(rgb, alpha)
        p = {"radius": 2.0}
        df = ref.evaluate(img, dict(p, output_mode="DistanceField"))
        np.testing.assert_allclose(df[0, :, 3], [1.0, 0.5, -0.5, -1.0], atol=1e-7)
        mt = ref.evaluate(img, dict(p, output_mode="Matte"))
        np.testing.assert_allclose(mt[0, :, 3], [1.0, 0.75, 0.25, 0.0], atol=1e-7)
        eb = ref.evaluate(img, dict(p, output_mode="EdgeBand"))
        np.testing.assert_allclose(eb[0, :, 3], [0.0, 0.5, 0.5, 0.0], atol=1e-7)
        for out in (df, mt, eb):
            np.testing.assert_array_equal(out[..., :3], rgb)   # RGB 透传

    def test_metrics_distinguish_3x3(self) -> None:
        # 手算 2：3×3 中心 FG、radius=2、DistanceField：
        # Euclid：角 d=√2 → −√2/2；边中 d=1 → −0.5；中心 +0.5
        # Chebyshev：角 = 边中 = −0.5；Manhattan：角 d=2 → −1
        alpha = np.zeros((3, 3), dtype=np.float32)
        alpha[1, 1] = 1.0
        img = _rgba(np.zeros((3, 3, 3), np.float32), alpha)
        eu = ref.evaluate(img, {"metric": "Euclid", "radius": 2.0})
        assert eu[1, 1, 3] == pytest.approx(0.5, abs=1e-7)
        assert eu[0, 1, 3] == pytest.approx(-0.5, abs=1e-7)
        assert eu[0, 0, 3] == pytest.approx(-math.sqrt(2) / 2, abs=1e-7)
        ch = ref.evaluate(img, {"metric": "Chebyshev", "radius": 2.0})
        assert ch[0, 0, 3] == pytest.approx(-0.5, abs=1e-7)
        assert ch[0, 1, 3] == pytest.approx(-0.5, abs=1e-7)
        ma = ref.evaluate(img, {"metric": "Manhattan", "radius": 2.0})
        assert ma[0, 0, 3] == pytest.approx(-1.0, abs=1e-7)
        assert ma[0, 1, 3] == pytest.approx(-0.5, abs=1e-7)
        # 三 metric 互异（角像素：√2/2 ≈ 0.7071 ≠ 0.5 ≠ 1）
        assert not np.allclose(eu[0, 0, 3], ch[0, 0, 3])
        assert not np.allclose(eu[0, 0, 3], ma[0, 0, 3])

    def test_threshold_boundary(self) -> None:
        # 手算 3：1×3 α=[0.3, 0.6, 0.9]、radius=2：
        # thr 0.5 → FG={1,2}: s=[−1,+1,+2] → dn=[−0.5,0.5,1]
        # thr 0.95 → FG={2}: s=[−2,−1,+1] → dn=[−1,−0.5,0.5]
        rgb = np.zeros((1, 3, 3), np.float32)
        alpha = np.array([[0.3, 0.6, 0.9]], dtype=np.float32)
        img = _rgba(rgb, alpha)
        out = ref.evaluate(img, {"radius": 2.0, "threshold": 0.5})
        np.testing.assert_allclose(out[0, :, 3], [-0.5, 0.5, 1.0], atol=1e-7)
        # thr 0.85 → FG={x2}（α=0.9）：s=[−2,−1,+1] → dn=[−1,−0.5,0.5]
        out2 = ref.evaluate(img, {"radius": 2.0, "threshold": 0.85})
        np.testing.assert_allclose(out2[0, :, 3], [-1.0, -0.5, 0.5], atol=1e-7)
        # thr 0.95 > max α=0.9 → FG 空 → 无对类饱和 dn=−1（防御口径）
        out3 = ref.evaluate(img, {"radius": 2.0, "threshold": 0.95})
        np.testing.assert_allclose(out3[0, :, 3], [-1.0, -1.0, -1.0], atol=1e-7)


class TestC18Semantics:
    def test_hdr_alpha_and_no_class_saturation(self) -> None:
        # HDR：α=5.0 ≥ 0.5 即 FG；全 BG → dn=−1（Matte→0）；
        # 全 FG → dn=+1（Matte→1）
        rgb = np.zeros((2, 2, 3), np.float32)
        img_bg = _rgba(rgb, np.zeros((2, 2), np.float32))
        out = ref.evaluate(img_bg, {"output_mode": "Matte"})
        np.testing.assert_allclose(out[..., 3], 0.0, atol=1e-7)
        img_hdr = _rgba(rgb, np.full((2, 2), 5.0, np.float32))
        out2 = ref.evaluate(img_hdr, {"output_mode": "Matte"})
        np.testing.assert_allclose(out2[..., 3], 1.0, atol=1e-7)
        df = ref.evaluate(img_hdr, {"output_mode": "DistanceField"})
        np.testing.assert_allclose(df[..., 3], 1.0, atol=1e-7)

    def test_distance_not_erode_dilate(self) -> None:
        # 不等同 erode/dilate（验收点）：EdgeBand 在边界两侧都有非零带
        # （腐蚀/膨胀只动单侧）；FG 内部 1px 深处与 BG 外部 1px 处
        # EdgeBand 值相同（对称带）
        alpha = np.zeros((1, 4), np.float32)
        alpha[0, 1] = 1.0     # 单像素 FG
        img = _rgba(np.zeros((1, 4, 3), np.float32), alpha)
        eb = ref.evaluate(img, {"radius": 3.0, "output_mode": "EdgeBand"})
        assert eb[0, 1, 3] == pytest.approx(1 - 1 / 3, abs=1e-7)   # FG 侧
        assert eb[0, 2, 3] == pytest.approx(1 - 1 / 3, abs=1e-7)   # BG 侧对称
        assert eb[0, 0, 3] == pytest.approx(1 - 1 / 3, abs=1e-7)   # x=0 距 FG=1
        assert eb[0, 3, 3] == pytest.approx(1 - 2 / 3, abs=1e-7)   # x=3 距 FG=2

    def test_invalid_metric_raises(self) -> None:
        img = _rgba(np.zeros((1, 2, 3), np.float32), np.ones((1, 2), np.float32))
        with pytest.raises(ValueError):
            ref.evaluate(img, {"metric": "Hausdorff"})
        with pytest.raises(ValueError):
            ref.evaluate(np.zeros((2, 2, 3), np.float32), {})   # 缺 α 显式报错


class TestC18Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C18"
        node = Lib1DistanceMatteNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
