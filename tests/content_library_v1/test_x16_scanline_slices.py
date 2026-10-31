"""X16 scanline_slices 测试：手算向量（含带向表对拍）、端点比特级、
单色通道同移（非 RGB split 钉住）、行带平移、带间硬边、Ease 差量、
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

from content_staging.library_v1.transitions.scanline_slices import scanline_slices_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "scanline_slices"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _h01(ix: int, iy: int, seed: int) -> float:
    """测试侧纯 Python 整数重算（禁自证惯例）。"""
    m = 0xFFFFFFFF
    x, y, s = ix & m, iy & m, seed & m
    h = ((x * 0x9E3779B1) ^ (y * 0x85EBCA77) ^ (s * 0xC2B2AE3D)) & m
    h ^= h >> 15
    h = (h * 0x2545F491) & m
    h ^= h >> 13
    h = (h * 0x9E3779B1) & m
    h ^= h >> 16
    return h / 4294967296.0


def ramp_pair():
    """与 hand_vectors 生成夹具同构：A=i/7、B=10+i（非镜像——镜像 B 与 A 位移
    在 p=0.5 相消成常色，掩盖行带位移语义）。"""
    row_a = np.arange(8, dtype=np.float32) / 7.0
    row_b = 10.0 + np.arange(8, dtype=np.float32)
    a = np.repeat(row_a[None, :, None], 4, axis=0).repeat(3, axis=2)
    b = np.repeat(row_b[None, :, None], 4, axis=0).repeat(3, axis=2)
    return a, b


def const_pair(av=0.2, bv=0.8):
    return (np.full((4, 8, 3), av, dtype=np.float32),
            np.full((4, 8, 3), bv, dtype=np.float32))


class TestHandVectors:
    def test_vectors_and_band_dirs(self) -> None:
        a, b = ramp_pair()
        for vec in HAND["vectors"]:
            if "expected_rows" not in vec:
                continue
            out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
            np.testing.assert_allclose(out[:, :, 0], vec["expected_rows"], atol=1e-6,
                                       err_msg=vec["desc"])
            for k, d in vec.get("band_dirs", {}).items():
                expected_dir = 1 if _h01(int(k), 53, vec["params"]["seed"]) >= 0.5 else -1
                assert d == expected_dir


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(-0.25, 4.5, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)

    def test_mono_pattern_channels_move_together(self) -> None:
        """RGB split 钉住差量：单色图案行带平移 → 三通道同移（无彩色边）。"""
        xs = np.arange(16, dtype=np.float32) / 15.0
        a = np.repeat(xs[None, :, None], 8, axis=0).repeat(3, axis=2)
        b = a.copy()
        out = core.evaluate(a, b, 0.5, {"band_px": 2, "amount": 0.25, "seed": 1})
        np.testing.assert_array_equal(out[..., 0], out[..., 1])
        np.testing.assert_array_equal(out[..., 1], out[..., 2])

    def test_rows_shifted_not_static(self) -> None:
        """中段行带 UV 位移真实发生：A=B 同源时输出≠源（位移混合可见）。"""
        xs = np.arange(16, dtype=np.float32) / 15.0
        a = np.repeat(xs[None, :, None], 8, axis=0).repeat(3, axis=2)
        out = core.evaluate(a, a.copy(), 0.5, {"band_px": 2, "amount": 0.25, "seed": 1})
        assert not np.allclose(out, a, atol=1e-4)

    def test_band_shift_constant_within_band(self) -> None:
        """同带位移常量：带内行与行之间输出相同（v 原位 + 同 d_k）。"""
        xs = np.arange(16, dtype=np.float32) / 15.0
        a = np.repeat(xs[None, :, None], 8, axis=0).repeat(3, axis=2)
        b = a.copy()
        out = np.asarray(core.evaluate(a, b, 0.5, {"band_px": 4, "amount": 0.25, "seed": 1}),
                         np.float64)
        np.testing.assert_allclose(out[0], out[1], atol=1e-7)   # 带 0（行 0..3）
        np.testing.assert_allclose(out[4], out[5], atol=1e-7)   # 带 1
        # 带 0 与带 1 方向不同（seed=1：-1/+1）→ 行组间不同
        assert not np.allclose(out[0], out[4], atol=1e-4)

    def test_band_hard_edges(self) -> None:
        """带间硬边：带界行的输出跳变大于带内行间差。"""
        xs = np.arange(32, dtype=np.float32) / 31.0
        a = np.repeat(xs[None, :, None], 8, axis=0).repeat(3, axis=2)
        b = a.copy()
        out = np.asarray(core.evaluate(a, b, 0.5, {"band_px": 2, "amount": 0.25, "seed": 1}),
                         np.float64)[:, :, 0]
        inner_gap = np.abs(out[1] - out[0]).max()    # 带 0 内（行 0|1 同向）
        seam_gap = np.abs(out[2] - out[1]).max()     # 带界（行 1|2，dir -1→+1）
        assert seam_gap > inner_gap

    def test_curve_ease_differs_from_linear(self) -> None:
        """Ease=三角窗²：过渡段（非峰值处）与 Linear 不同；峰值 p=0.5 同值。"""
        a, b = ramp_pair()
        kw = {"band_px": 2, "amount": 0.25, "seed": 0}
        lin25 = core.evaluate(a, b, 0.25, {**kw, "curve": "Linear"})
        ease25 = core.evaluate(a, b, 0.25, {**kw, "curve": "Ease"})
        assert not np.allclose(lin25, ease25)
        np.testing.assert_allclose(core.evaluate(a, b, 0.5, {**kw, "curve": "Linear"}),
                                   core.evaluate(a, b, 0.5, {**kw, "curve": "Ease"}),
                                   atol=1e-7)

    def test_amount_zero_is_exact_dissolve(self) -> None:
        a, b = ramp_pair()
        for p in (0.3, 0.5, 0.7):
            out = np.asarray(core.evaluate(a, b, p, {"amount": 0.0}), np.float64)
            np.testing.assert_allclose(out, (1 - p) * a + p * b, atol=1e-6)

    def test_window_vanishes_near_endpoints(self) -> None:
        """w=1-|2p-1|：靠近端点位移趋 0（输出趋溶解）。"""
        a, b = ramp_pair()
        kw = {"band_px": 2, "amount": 0.25, "seed": 0}
        for p in (0.02, 0.98):
            out = np.asarray(core.evaluate(a, b, p, kw), np.float64)
            diss = (1 - p) * a + p * b
            # w(0.02)=0.04 → 位移 ≤0.25·0.04=0.01（0.08px）·B 梯度 1/px
            assert np.max(np.abs(out - diss)) < 0.1
        for p in (0.0, 1.0):
            out = np.asarray(core.evaluate(a, b, p, kw), np.float64)
            diss = (1 - p) * a + p * b
            assert np.max(np.abs(out - diss)) == 0.0

    def test_seed_effective_and_reproducible(self) -> None:
        a, b = ramp_pair()
        kw = {"band_px": 2, "amount": 0.25}
        np.testing.assert_array_equal(core.evaluate(a, b, 0.5, {**kw, "seed": 0}),
                                      core.evaluate(a, b, 0.5, {**kw, "seed": 0}))
        assert not np.allclose(core.evaluate(a, b, 0.5, {**kw, "seed": 0}),
                               core.evaluate(a, b, 0.5, {**kw, "seed": 2}))

    def test_const_sources_monotone_no_rollback(self) -> None:
        a, b = const_pair(0.2, 0.8)
        prev = None
        for p in np.linspace(0.02, 0.98, 25):
            out = np.asarray(core.evaluate(a, b, float(p)), np.float64)
            if prev is not None:
                assert np.all(out >= prev - 1e-7)
            prev = out

    def test_defense_bad_values_fall_back(self) -> None:
        a, b = const_pair()
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"amount": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"band_px": float("inf")})))

    def test_size_mismatch_and_bad_curve_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)
        with pytest.raises(ValueError):
            core.evaluate(*const_pair(), 0.5, {"curve": "Smooth"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["curve"] == core.CURVES
