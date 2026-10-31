"""X05 luma_reveal 测试：手算向量（前沿/暗先亮后/invert 镜像/偏置/源 B）、
缺省回退、端点精确（含常量亮度）、单调、Rec.709 亮度、防御、manifest。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.luma_reveal import luma_reveal_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "luma_reveal"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _cols_img(cols: list[float]) -> np.ndarray:
    return np.tile(np.asarray(cols, dtype=np.float32), (4, 1))


class TestHandVectors:
    def test_dark_first_front_center(self) -> None:
        vec = _vec("暗部先揭示")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"],
                                  image_a=_cols_img(HAND["fixtures"]["image_a_gray_cols"]))
        np.testing.assert_allclose(mask, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_early_progress(self) -> None:
        vec = _vec("thr_eff=0.15")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"],
                                  image_a=_cols_img(HAND["fixtures"]["image_a_gray_cols"]))
        np.testing.assert_allclose(mask, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_invert_mirrors_ranking(self) -> None:
        vec = _vec("反相对象是亮度排序")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"],
                                  image_a=_cols_img(HAND["fixtures"]["image_a_gray_cols"]))
        np.testing.assert_allclose(mask, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_threshold_bias(self) -> None:
        vec = _vec("亮度偏置")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"],
                                  image_a=_cols_img(HAND["fixtures"]["image_a_gray_cols"]))
        np.testing.assert_allclose(mask, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_source_b(self) -> None:
        vec = _vec("source=B")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"],
                                  image_b=_cols_img(HAND["fixtures"]["image_b_gray_cols"]))
        np.testing.assert_allclose(mask, np.array(vec["expected_grid_rows"]), atol=1e-6)


class TestSemantics:
    def test_missing_image_linear_dissolve(self) -> None:
        vec = _vec("图像缺省回退")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"])
        np.testing.assert_allclose(mask, vec["expected_all"], atol=1e-7)
        # 源图缺省（source=B 但未给 B）同样回退
        mask_b = core.evaluate_mask(4, 4, 0.3, {"source": "B"},
                                    image_a=_cols_img(HAND["fixtures"]["image_a_gray_cols"]))
        np.testing.assert_allclose(mask_b, 0.3, atol=1e-7)

    def test_shape_mismatch_falls_back(self) -> None:
        mask = core.evaluate_mask(4, 4, 0.42, {}, image_a=np.zeros((5, 5), dtype=np.float32))
        np.testing.assert_allclose(mask, 0.42, atol=1e-7)

    def test_endpoints_exact_constant_luma(self) -> None:
        """常量亮度输入端点确定（台账差量验收点）。"""
        img = np.full((8, 8), 0.3, dtype=np.float32)
        np.testing.assert_array_equal(
            core.evaluate_mask(8, 8, 0.0, {}, image_a=img), 0.0)
        np.testing.assert_array_equal(
            core.evaluate_mask(8, 8, 1.0, {}, image_a=img), 1.0)

    def test_monotone_in_progress(self) -> None:
        img = _cols_img(HAND["fixtures"]["image_a_gray_cols"])
        prev = core.evaluate_mask(4, 4, 0.0, {}, image_a=img)
        for p in np.linspace(0.02, 1.0, 25):
            mask = core.evaluate_mask(4, 4, float(p), {}, image_a=img)
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            prev = mask

    def test_rec709_luma_weights(self) -> None:
        """RGB 亮度 = Rec.709 加权：纯红与灰 0.2126 揭示一致。"""
        red = np.zeros((4, 4, 3), dtype=np.float32)
        red[..., 0] = 1.0
        gray = np.full((4, 4), 0.2126, dtype=np.float32)
        m_red = core.evaluate_mask(4, 4, 0.5, {}, image_a=red)
        m_gray = core.evaluate_mask(4, 4, 0.5, {}, image_a=gray)
        np.testing.assert_allclose(m_red, m_gray, atol=2e-3)

    def test_content_driven_not_geometric(self) -> None:
        """内容驱动：同一 p 下不同图案的 mask 不同（几何核做不到）。"""
        dark = np.zeros((8, 8), dtype=np.float32)
        grad = np.tile(np.linspace(0, 1, 8, dtype=np.float32), (8, 1))
        m0 = core.evaluate_mask(8, 8, 0.5, {}, image_a=dark)
        m1 = core.evaluate_mask(8, 8, 0.5, {}, image_a=grad)
        assert not np.allclose(m0, m1)

    def test_bad_params_fall_back_no_raise(self) -> None:
        img = _cols_img(HAND["fixtures"]["image_a_gray_cols"])
        default = core.evaluate_mask(4, 4, 0.5, {}, image_a=img)
        bad = core.evaluate_mask(4, 4, 0.5, {
            "source": "C", "threshold": float("nan"),
            "span": float("inf"), "invert": False}, image_a=img)
        np.testing.assert_allclose(bad, default, atol=1e-6)


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "luma_reveal"
        assert m.time.time_domain == "static"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
