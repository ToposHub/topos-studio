"""X06 noise_dissolve 测试：手算向量（Value/Cell 二值场、羽化全场、seed）、
端点精确、单调、seed/scale/mode 视觉区分点、散列可复现、防御、manifest。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.noise_dissolve import noise_dissolve_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "noise_dissolve"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _mask(vec) -> np.ndarray:
    assert vec["fixture"] == "4x4"
    return core.evaluate_mask(4, 4, vec["progress"], vec["params"])


class TestHandVectors:
    def test_value_mode_binary_field(self) -> None:
        vec = _vec("Value rank<0.5 揭示")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_cell_mode_binary_field(self) -> None:
        vec = _vec("Cell：最近格点")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_softness_feather_field(self) -> None:
        vec = _vec("羽化向量")
        np.testing.assert_allclose(_mask(vec), np.array(vec["expected_grid_rows"]), atol=1e-9)

    def test_seed_changes_pattern(self) -> None:
        vec = _vec("seed=1 图案改变")
        mask = _mask(vec)
        np.testing.assert_array_equal(mask, np.array(vec["expected_grid_rows"], dtype=np.float32))
        base = _vec("Value rank<0.5 揭示")
        assert not np.allclose(mask, _mask(base))


class TestSemantics:
    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0), 1.0)

    def test_monotone_in_progress(self) -> None:
        prev = core.evaluate_mask(24, 24, 0.0, {"softness": 0.3})
        for p in np.linspace(0.02, 1.0, 25):
            mask = core.evaluate_mask(24, 24, float(p), {"softness": 0.3})
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            prev = mask

    def test_seed_and_scale_and_mode_distinguish(self) -> None:
        """视觉区分点：seed 改变图案；scale/mode 亦改变图案。"""
        base = core.evaluate_mask(32, 32, 0.5, {"seed": 0, "softness": 0.0})
        assert not np.allclose(core.evaluate_mask(32, 32, 0.5, {"seed": 1, "softness": 0.0}), base)
        assert not np.allclose(core.evaluate_mask(32, 32, 0.5, {"seed": 7, "softness": 0.0}), base)
        assert not np.allclose(
            core.evaluate_mask(32, 32, 0.5, {"scale": 8, "softness": 0.0}), base)
        assert not np.allclose(
            core.evaluate_mask(32, 32, 0.5, {"noise_mode": "Cell", "softness": 0.0}), base)

    def test_seed_static_deterministic(self) -> None:
        """固定 seed 静态：同参数逐次调用逐位一致（无 seed 动画）。"""
        a = core.evaluate_mask(20, 20, 0.4, {"seed": 5})
        b = core.evaluate_mask(20, 20, 0.4, {"seed": 5})
        np.testing.assert_array_equal(a, b)

    def test_hash_u32_reference_value(self) -> None:
        """散列独立钉住：lattice(1,1,0) 与库参考同式（手算脚本同值）。

        标量输入 → 返回 numpy 标量 float64（函数按 ndarray 设计），直接
        float() 取值；0.3380927466787398 为 murmur3 风格雪崩后 /2^32 的
        独立脚本参考值。"""
        h = core.lattice_hash_u32(np.int64(1), np.int64(1), 0)
        assert float(np.asarray(h)) == pytest.approx(0.338092746679, abs=1e-9)

    def test_mask_range_bounded(self) -> None:
        for p in (0.13, 0.5, 0.87):
            mask = core.evaluate_mask(32, 16, p, {"softness": 0.5, "scale": 6})
            assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0

    def test_bad_params_fall_back_no_raise(self) -> None:
        default = core.evaluate_mask(16, 8, 0.5)
        bad = core.evaluate_mask(16, 8, 0.5, {
            "noise_mode": "Perlin", "scale": float("nan"),
            "seed": float("inf"), "softness": None})
        np.testing.assert_array_equal(bad, default)
        for s in (-9, 1000, 3.7):
            mask = core.evaluate_mask(16, 8, 0.5, {"seed": s})
            assert mask.shape == (8, 16) and np.all(np.isfinite(mask))


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "noise_dissolve"
        assert m.time.time_domain == "static"
        assert m.time.seed_policy == "fixed-param-default"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
