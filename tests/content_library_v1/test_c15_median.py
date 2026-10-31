"""C15 lib1_median 测试：F19 median_rank 共享核再导出（单一事实源）+
中值稳健性/恒等/manifest 契约。算法语义已由 test_f19 钉住，此处防漂移。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.references import median_ref as ref
from content_staging.library_v1.references import median_rank_ref as f19
from content_staging.library_v1.nodes.median.lib1_median_node import (
    EFFECT_KEY, Lib1MedianNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "median" / "component_manifest.json")


class TestC15SingleSource:
    def test_reexport_is_f19(self) -> None:
        """禁双份漂移：evaluate/DEFAULTS 与 F19 同对象。"""
        assert ref.evaluate is f19.evaluate
        assert ref.DEFAULTS is f19.DEFAULTS
        assert ref.SOURCE_REF == "median_rank_ref"


class TestC15Semantics:
    def test_constant_image_identity(self) -> None:
        img = np.full((6, 6, 3), 0.35, dtype=np.float32)
        np.testing.assert_allclose(ref.evaluate(img), img, atol=1e-7)

    def test_median_suppresses_outlier(self) -> None:
        # 手算 1：5x5 全 0.5，中心 10.0；中心像素 3x3 窗含 8×0.5 + 10 →
        # 排序第 4（0 起）= 0.5（均值 ≈1.55，中值稳健）；远处像素窗全 0.5
        img = np.full((5, 5, 3), 0.5, dtype=np.float32)
        img[2, 2] = 10.0
        out = ref.evaluate(img)
        assert float(out[2, 2, 0]) == pytest.approx(0.5, abs=1e-7)
        assert float(out[0, 0, 0]) == pytest.approx(0.5, abs=1e-7)

    def test_amount_zero_identity(self) -> None:
        rng = np.random.RandomState(21)
        img = rng.rand(5, 5, 3).astype(np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"amount": 0.0}), img)

    def test_percentile_shifts_value(self) -> None:
        # percentile 0.0 → 窗最小值；0.5 → 中值：含离群像素两档可辨
        img = np.full((5, 5, 3), 0.5, dtype=np.float32)
        img[2, 2] = 10.0
        lo = ref.evaluate(img, {"percentile": 0.0})
        mid = ref.evaluate(img, {"percentile": 0.5})
        assert float(lo[2, 2, 0]) == pytest.approx(0.5, abs=1e-7)
        assert float(mid[2, 2, 0]) == pytest.approx(0.5, abs=1e-7)
        # 远处像素两档一致（窗内常量），中心像素一致（10 只占 1/9）
        # —— 可辨点放在 percentile=1.0：
        hi = ref.evaluate(img, {"percentile": 1.0})
        assert float(hi[2, 2, 0]) == pytest.approx(10.0, abs=1e-7)

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(23)
        img = rng.rand(7, 7, 3).astype(np.float32)
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"kernel_size": "5x5"}), base)
        assert not np.allclose(ref.evaluate(img, {"percentile": 0.2}), base)
        assert not np.allclose(ref.evaluate(img, {"amount": 0.5}), base)


class TestC15NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1MedianNode()
        assert node.node_type == EFFECT_KEY == "lib1_median"


class TestC15Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C15"
        node = Lib1MedianNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        assert [p.name for p in manifest.params] == list(ref.DEFAULTS.keys())


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
