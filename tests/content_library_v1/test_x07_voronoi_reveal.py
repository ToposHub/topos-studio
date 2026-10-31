"""X07 voronoi_reveal 测试：手算向量（独立标量脚本生成 + 种子表）、端点、
逐像素单调、胞元中心先于边界（钉住语义）、edge_width 带宽、seed 有效性、
防御、manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.voronoi_reveal import voronoi_reveal_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "voronoi_reveal"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


class TestHandVectors:
    def test_full_mask_vector(self) -> None:
        vec = next(v for v in HAND["vectors"] if "edge=0.1" in v["desc"])
        out = np.asarray(core.evaluate_mask(8, 8, vec["progress"], vec["params"]), np.float64)
        np.testing.assert_allclose(out, np.asarray(vec["expected_rows"]), atol=1e-6)

    def test_wide_band_vector(self) -> None:
        vec = next(v for v in HAND["vectors"] if "edge_width=0.3" in v["desc"])
        out = np.asarray(core.evaluate_mask(8, 8, vec["progress"], vec["params"]), np.float64)
        np.testing.assert_allclose(out, np.asarray(vec["expected_rows"]), atol=1e-6)

    def test_seed_table_matches_independent_hash(self) -> None:
        """种子表（手算向量附带）与测试侧纯 Python 整数重算 hash 对拍。"""
        vec = next(v for v in HAND["vectors"] if "seeds" in v)
        seeds = vec["seeds"]
        m = seeds["n"] * seeds["n"]
        M = 0xFFFFFFFF
        def h01(ix, iy, seed):
            x, y, s = ix & M, iy & M, seed & M
            h = ((x * 0x9E3779B1) ^ (y * 0x85EBCA77) ^ (s * 0xC2B2AE3D)) & M
            h ^= h >> 15; h = (h * 0x2545F491) & M
            h ^= h >> 13; h = (h * 0x9E3779B1) & M
            h ^= h >> 16
            return h / 4294967296.0
        sx, sy = [], []
        for jy in range(seeds["n"]):
            for jx in range(seeds["n"]):
                sx.append((jx + h01(jx, jy, 7)) / seeds["n"])
                sy.append((jy + h01(jx, jy, 7 + 1001)) / seeds["n"])
        np.testing.assert_allclose(sx, seeds["sx"], atol=1e-6)
        np.testing.assert_allclose(sy, seeds["sy"], atol=1e-6)
        assert m == len(sx)


class TestSemantics:
    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0), np.zeros((8, 16), np.float32))
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0), np.ones((8, 16), np.float32))
        # p=0/1 精确常量（含非默认参数）
        np.testing.assert_array_equal(
            core.evaluate_mask(16, 8, 0.0, {"edge_width": 0.5, "cell_count": "32"}),
            np.zeros((8, 16), np.float32))

    def test_monotone_no_rollback_per_pixel(self) -> None:
        prev = core.evaluate_mask(32, 24, 0.0)
        for p in np.linspace(0.03, 0.97, 23):
            cur = core.evaluate_mask(32, 24, float(p))
            assert np.all(cur >= prev - 1e-7), f"回退于 p={p}"
            prev = cur

    def test_cell_center_reveals_before_cell_boundary(self) -> None:
        """晶胞语义钉住：同一胞元（同最近种子）内 d 小（中心）先于 d 大（胞界）。"""
        vec = next(v for v in HAND["vectors"] if "seeds" in v)
        seeds = vec["seeds"]
        w = h = 32
        aspect = w / h
        diag = np.hypot(aspect, 1.0)
        us = (np.arange(w) + 0.5) / w
        vs = (np.arange(h) + 0.5) / h
        uu, vv = np.meshgrid(us, vs)
        sx = np.asarray(seeds["sx"])
        sy = np.asarray(seeds["sy"])
        d2 = ((uu[..., None] - sx) * aspect) ** 2 + (vv[..., None] - sy) ** 2
        own = np.argmin(d2, axis=-1)
        dmin = np.sqrt(np.min(d2, axis=-1))
        for p in (0.3, 0.5, 0.7):
            mask = np.asarray(core.evaluate_mask(w, h, p, {"cell_count": "8", "seed": 7}), np.float64)
            for i in range(len(sx)):
                sel = own == i
                if sel.sum() < 2:
                    continue
                d_own = np.where(sel, dmin, np.inf)
                center = np.unravel_index(np.argmin(d_own), d_own.shape)
                d_own_max = np.where(sel, dmin, -np.inf)
                boundary = np.unravel_index(np.argmax(d_own_max), d_own_max.shape)
                assert mask[center] >= mask[boundary] - 1e-7, (p, i)

    def test_edge_width_widens_band(self) -> None:
        """edge_width 增大 → 揭示带（部分值像素）变宽（峰值部分像素数更多）。"""
        def peak_partial(ew: float) -> int:
            best = 0
            for p in np.linspace(0.05, 0.95, 37):
                m = np.asarray(core.evaluate_mask(48, 32, float(p),
                                                  {"edge_width": ew}), np.float64)
                best = max(best, int(((m > 1e-4) & (m < 1 - 1e-4)).sum()))
            return best
        assert peak_partial(0.3) > peak_partial(0.05) > 0

    def test_seed_effective(self) -> None:
        m1 = np.asarray(core.evaluate_mask(32, 32, 0.5, {"seed": 7}), np.float64)
        m2 = np.asarray(core.evaluate_mask(32, 32, 0.5, {"seed": 8}), np.float64)
        assert not np.allclose(m1, m2)
        np.testing.assert_array_equal(
            core.evaluate_mask(32, 32, 0.5, {"seed": 7}),
            core.evaluate_mask(32, 32, 0.5, {"seed": 7}))

    def test_cell_count_tiers_differ(self) -> None:
        outs = [np.asarray(core.evaluate_mask(48, 48, 0.5, {"cell_count": c}), np.float64)
                for c in ("8", "16", "32")]
        assert not np.allclose(outs[0], outs[1])
        assert not np.allclose(outs[1], outs[2])

    def test_values_bounded(self) -> None:
        for p in np.linspace(0.02, 0.98, 13):
            m = core.evaluate_mask(24, 16, float(p))
            assert m.dtype == np.float32
            assert float(m.min()) >= 0.0 and float(m.max()) <= 1.0

    def test_defense_bad_values_fall_back(self) -> None:
        out = core.evaluate_mask(16, 8, 0.5, {"edge_width": float("nan")})
        np.testing.assert_array_equal(out, core.evaluate_mask(16, 8, 0.5))
        out_seed = core.evaluate_mask(16, 8, 0.5, {"seed": float("inf")})
        assert np.all(np.isfinite(out_seed)) and out_seed.dtype == np.float32

    def test_bad_cell_count_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate_mask(8, 8, 0.5, {"cell_count": "12"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["cell_count"] == core.CELL_COUNTS
        # mask 类端口例外：仅 mask 输出口
        assert [port.name for port in m.ports] == ["mask"]
        assert m.component_id == "X07"
