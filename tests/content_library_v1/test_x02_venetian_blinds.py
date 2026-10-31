"""X02 venetian_blinds 测试：手算向量（同时扫掠/count 带宽/羽化/angle/phase）、
端点精确、单调、条带数等参数有效性（视觉区分点）、防御回退、manifest 契约。

语义修正记录：初版核公式 r=mod(s·count+phase, count) 退化为带角度线性 wipe
（条带不错峰），独立重推改为带内相位 t=mod(s·count+phase, 1) 的标准百叶
（全带同时扫掠）；本文件向量按修正语义重算。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.venetian_blinds import venetian_blinds_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "venetian_blinds"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _mask(vec) -> np.ndarray:
    w, h = (8, 4) if vec["fixture"] == "8x4" else (4, 4)
    return core.evaluate_mask(w, h, vec["progress"], vec["params"])


class TestHandVectors:
    def test_simultaneous_sweep(self) -> None:
        vec = _vec("同时扫掠")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_count2_band_width(self) -> None:
        vec = _vec("count=2")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_softness_feather_exact_half(self) -> None:
        vec = _vec("羽化向量")
        np.testing.assert_allclose(_mask(vec), np.array(vec["expected_grid_rows"]), atol=1e-9)

    def test_angle_90_horizontal_blinds(self) -> None:
        vec = _vec("angle=90")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_phase_shift_periodic(self) -> None:
        vec = _vec("phase=0.5")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))


class TestSemantics:
    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0), 1.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, -0.3), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.7), 1.0)

    def test_monotone_in_progress(self) -> None:
        prev = core.evaluate_mask(24, 12, 0.0, {"count": 5, "softness": 0.2})
        for p in np.linspace(0.02, 1.0, 25):
            mask = core.evaluate_mask(24, 12, float(p), {"count": 5, "softness": 0.2})
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            prev = mask

    def test_count_changes_output(self) -> None:
        """视觉区分点：条带数改变输出（4 带 vs 8 带图案不同）。"""
        base = core.evaluate_mask(32, 16, 0.5, {"count": 4, "softness": 0.0})
        other = core.evaluate_mask(32, 16, 0.5, {"count": 8, "softness": 0.0})
        assert not np.allclose(base, other)
        assert abs(float(base.mean()) - float(other.mean())) < 0.2  # 揭示量级相近、图案不同

    def test_params_have_effect(self) -> None:
        base = core.evaluate_mask(32, 16, 0.5, {"count": 4})
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"count": 4, "angle": 30.0}), base)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"count": 4, "phase": 0.5}), base)
        assert not np.allclose(core.evaluate_mask(32, 16, 0.5, {"count": 4, "softness": 0.4}), base)

    def test_mask_range_bounded(self) -> None:
        for p in (0.13, 0.5, 0.87):
            mask = core.evaluate_mask(32, 16, p, {"softness": 0.5, "count": 3, "angle": -45.0})
            assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0

    def test_bad_params_fall_back_no_raise(self) -> None:
        default = core.evaluate_mask(16, 8, 0.5)
        bad = core.evaluate_mask(16, 8, 0.5, {
            "count": float("nan"), "angle": float("inf"),
            "phase": None, "softness": "x"})
        np.testing.assert_array_equal(bad, default)
        # 极值条数不除零：count=1/9999 clamp 到 [2,32]
        for c in (0, 1, 9999, -5):
            mask = core.evaluate_mask(16, 8, 0.5, {"count": c})
            assert mask.shape == (8, 16) and np.all(np.isfinite(mask))


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "venetian_blinds"
        assert m.time.time_domain == "static"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
