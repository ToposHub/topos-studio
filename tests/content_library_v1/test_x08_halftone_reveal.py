"""X08 halftone_reveal 测试：手算向量（Dot/Square 闭式距离、羽化、间距/角
区分）、端点精确（含最大 softness）、单调、参数有效性、防御、manifest。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.halftone_reveal import halftone_reveal_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "halftone_reveal"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _mask(vec) -> np.ndarray:
    assert vec["fixture"] == "8x4"
    return core.evaluate_mask(8, 4, vec["progress"], vec["params"])


class TestHandVectors:
    def test_dot_expansion(self) -> None:
        vec = _vec("Dot p=0.3")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_square_chebyshev(self) -> None:
        vec = _vec("Square 切比雪夫距")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_softness_feather(self) -> None:
        vec = _vec("羽化向量")
        np.testing.assert_allclose(_mask(vec), np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_spacing_changes_frequency(self) -> None:
        vec = _vec("spacing=4")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_angle_rotates_lattice(self) -> None:
        vec = _vec("angle=45")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))


class TestSemantics:
    def test_endpoints_exact_even_max_softness(self) -> None:
        """r_max=√2 ≥ 角距 √2/2 + max soft 0.5：最大羽化下端点也全覆盖。"""
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0, {"softness": 0.5}), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0, {"softness": 0.5}), 1.0)
        # 无早退中段逼近：p=0.999 已接近全覆盖
        mask = core.evaluate_mask(16, 16, 0.999, {"softness": 0.5, "spacing": 8})
        assert float(mask.min()) > 0.9

    def test_monotone_in_progress(self) -> None:
        prev = core.evaluate_mask(24, 24, 0.0, {"softness": 0.2, "angle": 30.0})
        for p in np.linspace(0.02, 1.0, 25):
            mask = core.evaluate_mask(24, 24, float(p), {"softness": 0.2, "angle": 30.0})
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            prev = mask

    def test_params_have_effect(self) -> None:
        base = core.evaluate_mask(32, 16, 0.5)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"spacing": 8}), base)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"angle": 0.0}), base)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"shape": "Square"}), base)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"softness": 0.4}), base)

    def test_dot_vs_square_machine_distinct(self) -> None:
        """形状区分：对角位置 Square 纳入、Dot 排除（切比雪夫 < 欧氏）。"""
        dot = core.evaluate_mask(32, 32, 0.3, {"spacing": 16, "angle": 0.0, "softness": 0.0})
        square = core.evaluate_mask(32, 32, 0.3, {"spacing": 16, "angle": 0.0,
                                                 "shape": "Square", "softness": 0.0})
        assert float(square.mean()) > float(dot.mean())
        assert not np.allclose(dot, square)

    def test_mask_range_bounded(self) -> None:
        for p in (0.13, 0.5, 0.87):
            mask = core.evaluate_mask(32, 16, p, {"softness": 0.5, "angle": -60.0})
            assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0

    def test_bad_params_fall_back_no_raise(self) -> None:
        default = core.evaluate_mask(16, 8, 0.5)
        bad = core.evaluate_mask(16, 8, 0.5, {
            "spacing": float("nan"), "angle": float("inf"),
            "shape": "Triangle", "softness": "x"})
        np.testing.assert_array_equal(bad, default)
        for sp in (0, -8, 10000):
            mask = core.evaluate_mask(16, 8, 0.5, {"spacing": sp})
            assert mask.shape == (8, 16) and np.all(np.isfinite(mask))


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "halftone_reveal"
        assert m.time.time_domain == "static"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
