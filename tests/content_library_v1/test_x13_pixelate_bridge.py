"""X13 pixelate_bridge 测试：手算向量（块中心采样逐级/switch 换源/Out 镜像/
垂直块）、端点比特级（float32/uint8）、粗化单调、参数有效性、防御、manifest。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.pixelate_bridge import pixelate_bridge_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "pixelate_bridge"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

A_ROW = np.asarray(HAND["fixtures"]["a_row"], dtype=np.float32)
B_ROW = np.asarray(HAND["fixtures"]["b_row"], dtype=np.float32)


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _ab(vec):
    if vec["fixture"] == "8x4-vertical":
        a = np.repeat(np.asarray(HAND["fixtures"]["a_rows_vertical"], dtype=np.float32), 8, axis=1)
    else:
        a = np.tile(A_ROW, (4, 1))
    b = np.tile(B_ROW, (4, 1))
    return a, b


def _eval(vec) -> np.ndarray:
    a, b = _ab(vec)
    return core.evaluate(a, b, vec["progress"], vec["params"])


class TestHandVectors:
    def test_in_first_half_c2(self) -> None:
        vec = _vec("c_eff=4·0.5=2")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_in_c3_block_centers(self) -> None:
        vec = _vec("块中心 src={1,4,7}")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_switch_to_b_at_peak(self) -> None:
        vec = _vec("p=switch")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_in_second_half_refine(self) -> None:
        vec = _vec("B 侧细化")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_out_identity_at_switch(self) -> None:
        vec = _vec("Out p=0.5")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_out_mirrored_profile(self) -> None:
        vec = _vec("Out p=0.25")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_vertical_blocks(self) -> None:
        vec = _vec("垂直块")
        np.testing.assert_array_equal(_eval(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))


class TestSemantics:
    def test_endpoints_bit_exact_float(self) -> None:
        rng = np.linspace(-2.0, 3.0, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)
        np.testing.assert_array_equal(core.evaluate(a, b, -0.5), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.5), b)

    def test_endpoints_bit_exact_uint8(self) -> None:
        a = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
        b = a[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)

    def test_coarsening_monotone_before_switch(self) -> None:
        """In 前半：c_eff 递增 → 唯一值数单调不增（逐级粗化）。"""
        a = np.tile(np.linspace(0, 1, 64, dtype=np.float32), (8, 1))
        b = a.copy()
        uniques = [np.unique(core.evaluate(a, b, p, {"cell": 16})).size
                   for p in (0.05, 0.25, 0.45)]
        assert uniques == sorted(uniques, reverse=True), uniques

    def test_params_have_effect(self) -> None:
        a, b = np.tile(A_ROW, (4, 1)), np.tile(B_ROW, (4, 1))
        base = core.evaluate(a, b, 0.3)
        assert not np.allclose(core.evaluate(a, b, 0.3, {"cell": 8}), base)
        assert not np.allclose(core.evaluate(a, b, 0.3, {"source_switch": 0.3}), base)
        assert not np.allclose(core.evaluate(a, b, 0.3, {"direction": "Out"}), base)

    def test_switch_extremes_no_division_error(self) -> None:
        a, b = np.tile(A_ROW, (4, 1)), np.tile(B_ROW, (4, 1))
        for sw in (0.0, 1.0, -1.0, 2.0):
            out = core.evaluate(a, b, 0.5, {"source_switch": sw})
            assert out.shape == (4, 8) and np.all(np.isfinite(out))

    def test_rgb_channels_blocked_together(self) -> None:
        """通道不串扰：三通道同内容（灰）输入 → 块采样后三通道仍逐位一致。"""
        a = np.zeros((4, 8, 3), dtype=np.float32)
        a[..., :] = np.tile(A_ROW, (4, 1))[..., None]
        b = np.ones((4, 8, 3), dtype=np.float32)
        out = core.evaluate(a, b, 0.25, {"cell": 4})
        assert out.shape == (4, 8, 3)
        np.testing.assert_array_equal(out[..., 0], out[..., 1])
        np.testing.assert_array_equal(out[..., 1], out[..., 2])

    def test_size_mismatch_rejected(self) -> None:
        a = np.zeros((4, 4, 3), dtype=np.float32)
        b = np.zeros((5, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError):
            core.evaluate(a, b, 0.5)

    def test_bad_params_fall_back_no_raise(self) -> None:
        a, b = np.tile(A_ROW, (4, 1)), np.tile(B_ROW, (4, 1))
        default = core.evaluate(a, b, 0.4)
        bad = core.evaluate(a, b, 0.4, {
            "cell": float("nan"), "source_switch": float("inf"),
            "direction": 7})
        np.testing.assert_array_equal(bad, default)


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "pixelate_bridge"
        assert m.time.time_domain == "static"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
