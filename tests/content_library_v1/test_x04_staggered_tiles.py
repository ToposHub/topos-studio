"""X04 staggered_tiles 测试：手算向量（独立标量脚本生成）、端点比特级、
错峰时序、块内缩放、Random seed 有效性、硬边、防御、manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.staggered_tiles import staggered_tiles_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "staggered_tiles"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def const_pair(av: float, bv: float, w: int = 4, h: int = 4):
    return (np.full((h, w, 3), av, dtype=np.float32),
            np.full((h, w, 3), bv, dtype=np.float32))


def ramp_pair(w: int = 8, h: int = 4):
    row = np.arange(w, dtype=np.float32) / 7.0
    a = np.repeat(row[None, :, None], h, axis=0).repeat(3, axis=2)
    b = (1.0 - row)[None, :, None].repeat(h, axis=0).repeat(3, axis=2)
    return a, b


class TestHandVectors:
    def test_stagger_timing_const_tiles(self) -> None:
        vec = next(v for v in HAND["vectors"] if "错峰时序" in v["desc"])
        a, b = const_pair(vec["a"], vec["b"])
        out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row0"], atol=1e-6)
        np.testing.assert_allclose(out[3, :, 0], vec["expected_row3"], atol=1e-6)

    def test_tile_inner_window_sampling(self) -> None:
        vec = next(v for v in HAND["vectors"] if "块内 A 窗收缩" in v["desc"])
        a = np.zeros((4, 4, 3), dtype=np.float32)
        b = np.zeros((4, 4, 3), dtype=np.float32)
        for i in range(4):
            a[0, i, 0] = float(i)
            b[0, i, 0] = 10.0 + float(i)
        out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row0_ch0"], atol=1e-6)

    def test_random_order_vector(self) -> None:
        vec = next(v for v in HAND["vectors"] if "Random 档" in v["desc"])
        a, b = ramp_pair()
        out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row0"], atol=1e-6)


class TestSemantics:
    def test_endpoints_bit_exact_float32(self) -> None:
        rng = np.linspace(-0.25, 4.5, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)

    def test_endpoints_bit_exact_uint8(self) -> None:
        rng = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        out0 = core.evaluate(a, b, 0.0)
        out1 = core.evaluate(a, b, 1.0)
        assert out0.dtype == np.uint8 and out1.dtype == np.uint8
        np.testing.assert_array_equal(out0, a)
        np.testing.assert_array_equal(out1, b)

    def test_staggered_corners_exact_at_mid(self) -> None:
        """delay=0.5 grid=2 p=0.5：首块 pi=1（精确 B 常色）、末块 pi=0（精确 A 常色）。"""
        a, b = const_pair(0.1, 0.9)
        out = np.asarray(core.evaluate(a, b, 0.5, {"grid": 2, "delay": 0.5,
                                                   "scale_amt": 0.0}), np.float64)
        np.testing.assert_allclose(out[:2, :2], 0.9, atol=1e-7)   # 左上块 pi=1
        np.testing.assert_allclose(out[2:, 2:], 0.1, atol=1e-7)   # 右下块 pi=0

    def test_delay_zero_scale_zero_is_global_dissolve(self) -> None:
        """delay=0 → pi=p 全块；scale=0 → 窗=1 恒等采样 → 精确全局溶解。"""
        a, b = ramp_pair()
        for p in (0.25, 0.5, 0.75):
            out = np.asarray(core.evaluate(a, b, p, {"delay": 0.0, "scale_amt": 0.0}), np.float64)
            np.testing.assert_allclose(out, (1 - p) * a + p * b, atol=1e-6)

    def test_random_seed_effective_and_reproducible(self) -> None:
        a, b = ramp_pair()
        kw = {"grid": 4, "delay": 0.2, "order": "Random", "scale_amt": 0.25}
        o1 = core.evaluate(a, b, 0.4, {**kw, "seed": 13})
        o2 = core.evaluate(a, b, 0.4, {**kw, "seed": 13})
        np.testing.assert_array_equal(o1, o2)
        assert not np.allclose(o1, core.evaluate(a, b, 0.4, {**kw, "seed": 14}))
        # Row 与 Random 排布不同
        assert not np.allclose(
            core.evaluate(a, b, 0.4, {**kw, "order": "Row"}),
            core.evaluate(a, b, 0.4, {**kw, "order": "Random"}))

    def test_tile_hard_edges_visible(self) -> None:
        """块间硬边（不羽化）：错峰+缩放下块界跳变大于块内最大梯度。"""
        a = np.zeros((4, 8, 3), dtype=np.float32)
        b = np.zeros((4, 8, 3), dtype=np.float32)
        a[:, :4], a[:, 4:] = 0.2, 0.9
        b[:, :4], b[:, 4:] = 0.9, 0.2
        out = np.asarray(core.evaluate(a, b, 0.5, {"grid": 2, "delay": 0.5,
                                                   "scale_amt": 0.5}), np.float64)
        row = out[0, :, 0]
        inner_max = max(np.abs(np.diff(row[:4])).max(), np.abs(np.diff(row[4:])).max())
        seam_gap = abs(row[4] - row[3])
        assert seam_gap > 2.0 * inner_max, (seam_gap, inner_max)

    def test_const_tiles_monotone_no_rollback(self) -> None:
        a, b = const_pair(0.2, 0.8)
        prev = None
        for p in np.linspace(0.02, 0.98, 25):
            out = np.asarray(core.evaluate(a, b, float(p)), np.float64)
            if prev is not None:
                assert np.all(out >= prev - 1e-7), f"回退于 p={p}"
            prev = out

    def test_progress_clamped(self) -> None:
        a, b = const_pair(0.2, 0.8)
        np.testing.assert_array_equal(core.evaluate(a, b, -0.5), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.5), b)

    def test_defense_bad_values_fall_back(self) -> None:
        a, b = const_pair(0.2, 0.8)
        out_nan = core.evaluate(a, b, 0.5, {"delay": float("nan")})
        out_def = core.evaluate(a, b, 0.5)
        np.testing.assert_allclose(out_nan, out_def, atol=0)
        out_inf = core.evaluate(a, b, 0.5, {"scale_amt": float("inf")})
        assert np.all(np.isfinite(out_inf))
        out_bad_grid = core.evaluate(a, b, 0.5, {"grid": "oops"})
        np.testing.assert_allclose(out_bad_grid, core.evaluate(a, b, 0.5), atol=0)

    def test_size_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((5, 4, 3)), 0.5)

    def test_bad_order_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(*const_pair(0.2, 0.8), 0.5, {"order": "Diagonal"})

    def test_uint8_roundtrip_dtype(self) -> None:
        a = np.full((4, 4, 3), 60, dtype=np.uint8)
        b = np.full((4, 4, 3), 200, dtype=np.uint8)
        out = core.evaluate(a, b, 0.5)
        assert out.dtype == np.uint8
        assert 60 <= int(out[0, 0, 0]) <= 200


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert [p.name for p in m.params] == sorted(core.DEFAULTS.keys()) or \
            set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        assert m.component_id == "X04" and m.kind == "transition"
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["order"] == core.ORDERS
        assert m.status == "component_ready"
