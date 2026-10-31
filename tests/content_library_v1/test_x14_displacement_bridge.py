"""X14 displacement_bridge 测试：手算向量、端点比特级、scale=0 溶解退化、
Wave/Cell 差量、seed 有效性、中段位移可见（与 dissolve 差量）、防御、
manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.displacement_bridge import displacement_bridge_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "displacement_bridge"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def ramp_pair():
    """与 hand_vectors 生成夹具同构：A=i/7、B=10+i（非镜像——镜像 B 在 p=0.5
    会与 A 位移相消成常色，掩盖位移语义）。"""
    row_a = np.arange(8, dtype=np.float32) / 7.0
    row_b = 10.0 + np.arange(8, dtype=np.float32)
    a = np.repeat(row_a[None, :, None], 4, axis=0).repeat(3, axis=2)
    b = np.repeat(row_b[None, :, None], 4, axis=0).repeat(3, axis=2)
    return a, b


def const_pair(av=0.2, bv=0.8):
    return (np.full((4, 8, 3), av, dtype=np.float32),
            np.full((4, 8, 3), bv, dtype=np.float32))


class TestHandVectors:
    def test_vectors(self) -> None:
        a, b = ramp_pair()
        for vec in HAND["vectors"]:
            if "expected_row0" not in vec:
                continue
            out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
            np.testing.assert_allclose(out[0, :, 0], vec["expected_row0"], atol=1e-6,
                                       err_msg=vec["desc"])


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(-0.25, 4.5, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)
        au8 = (rng * 40).astype(np.uint8)
        np.testing.assert_array_equal(core.evaluate(au8, au8[::-1].copy(), 0.0), au8)

    def test_scale_zero_is_exact_dissolve(self) -> None:
        a, b = ramp_pair()
        for p in (0.3, 0.5, 0.7):
            out = np.asarray(core.evaluate(a, b, p, {"scale": 0.0}), np.float64)
            np.testing.assert_allclose(out, (1 - p) * a + p * b, atol=1e-6)

    def test_mid_displacement_differs_from_dissolve(self) -> None:
        """中段 UV 真位移（与 dissolve 的机器可辨差量）：特征移动。"""
        a, b = ramp_pair()
        for p in (0.25, 0.5, 0.75):
            out = np.asarray(core.evaluate(a, b, p, {"scale": 0.25}), np.float64)
            diss = (1 - p) * a + p * b
            assert not np.allclose(out, diss, atol=1e-4), f"p={p} 位移不可见"

    def test_displacement_peaks_at_mid_vanishes_to_endpoints(self) -> None:
        """三角窗：A=B 同源（溶解=源本身）时偏离幅度 ∝ 窗 w(p)，
        在 p=0.5 峰值、两侧对称递减、端点精确 0。"""
        row = np.arange(16, dtype=np.float32) / 15.0
        a = np.repeat(row[None, :, None], 8, axis=0).repeat(3, axis=2)
        b = a.copy()
        dev = lambda p: float(np.max(np.abs(np.asarray(
            core.evaluate(a, b, p, {"scale": 0.25}), np.float64) - a)))
        assert dev(0.5) > dev(0.35) > dev(0.2) > 0.0
        assert dev(0.5) > dev(0.65) > dev(0.8) > 0.0
        assert abs(dev(0.35) - dev(0.65)) < 1e-6   # 窗对称

    def test_wave_vs_cell_differ(self) -> None:
        a, b = ramp_pair()
        kw = {"scale": 0.25, "frequency": 4.0, "seed": 0}
        w = core.evaluate(a, b, 0.5, {**kw, "mode": "Wave"})
        c = core.evaluate(a, b, 0.5, {**kw, "mode": "Cell"})
        assert not np.allclose(w, c)

    def test_cell_mode_constant_within_interior_cells(self) -> None:
        """Cell 模式：格内位移常量 → 内部格（远离边 clamp）上 out-A 为常量
        （线性 ramp 上常量位移=常量值偏移）；Wave 模式无此性质。"""
        a = np.repeat(np.arange(16, dtype=np.float32)[None, :], 16,
                      axis=0)[:, :, None].repeat(3, axis=2)
        b = a.copy()
        kw = {"frequency": 4.0, "scale": 0.1, "seed": 0}
        cell = np.asarray(core.evaluate(a, b, 0.5, {**kw, "mode": "Cell"}), np.float64)
        wave = np.asarray(core.evaluate(a, b, 0.5, {**kw, "mode": "Wave"}), np.float64)
        ref = a[:, :, 0].astype(np.float64)
        off_cell = cell[:, :, 0] - ref
        off_wave = wave[:, :, 0] - ref
        interior = (slice(4, 8), slice(4, 8))   # 格 (1,1)（4px 格，离帧边 2 格）
        assert float(np.ptp(off_cell[interior])) < 1e-6, "Cell 格内应常量偏移"
        assert float(np.ptp(off_wave[interior])) > 1e-3, "Wave 格内应连续变化"

    def test_seed_effective_and_reproducible(self) -> None:
        a, b = ramp_pair()
        kw = {"scale": 0.25, "frequency": 4.0}
        np.testing.assert_array_equal(
            core.evaluate(a, b, 0.5, {**kw, "seed": 3}),
            core.evaluate(a, b, 0.5, {**kw, "seed": 3}))
        assert not np.allclose(core.evaluate(a, b, 0.5, {**kw, "seed": 3}),
                               core.evaluate(a, b, 0.5, {**kw, "seed": 4}))

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
            core.evaluate(a, b, 0.5, {"scale": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"frequency": float("inf")})))
        # frequency 越界钳制（16 上限）不崩溃且有限
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"frequency": 99.0})))

    def test_size_mismatch_and_bad_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)
        with pytest.raises(ValueError):
            core.evaluate(*const_pair(), 0.5, {"mode": "Turbulence"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["mode"] == core.MODES
        # 外部 map 不在本批（资源子门）声明
        assert "外部 map" in m.host_min_contract
