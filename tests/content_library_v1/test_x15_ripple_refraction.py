"""X15 ripple_refraction 测试：手算向量、端点比特级、幅度 0 溶解退化、
中心零位移、径向振荡（波场非缩放）、对称性、damping 衰减、防御、
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

from content_staging.library_v1.transitions.ripple_refraction import ripple_refraction_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "ripple_refraction"
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

    def test_amplitude_zero_is_exact_dissolve(self) -> None:
        a, b = ramp_pair()
        for p in (0.3, 0.5, 0.7):
            out = np.asarray(core.evaluate(a, b, p, {"amplitude": 0.0}), np.float64)
            np.testing.assert_allclose(out, (1 - p) * a + p * b, atol=1e-6)

    def test_center_pixel_zero_displacement(self) -> None:
        """中心像素（r=0）位移定义 0：该像素输出=溶解值（无位移采样）。"""
        # 8x4：中心取像素中心 (0.5625, 0.625)
        a, b = ramp_pair()
        kw = {"center_x": 0.5625, "center_y": 0.625, "amplitude": 0.25,
              "frequency": 8.0, "damping": 0.3}
        p = 0.5
        out = np.asarray(core.evaluate(a, b, p, kw), np.float64)
        expected = (1 - p) * a[1, 4, 0] + p * b[1, 4, 0]
        assert abs(out[1, 4, 0] - expected) < 1e-6

    def test_radial_oscillation_visible(self) -> None:
        """径向 sin 波场：沿径向行输出非单调（区别于单调缩放/溶解）。"""
        xs = np.arange(32, dtype=np.float32) / 31.0
        a = np.repeat(xs[None, :, None], 8, axis=0).repeat(3, axis=2)
        b = np.zeros_like(a)
        row = np.asarray(core.evaluate(a, b, 0.5, {"amplitude": 0.25, "frequency": 16.0,
                                                   "damping": 0.0, "center_y": 0.5}),
                         np.float64)[4, :, 0]
        diffs = np.diff(row)
        assert (diffs > 0).any() and (diffs < 0).any(), "径向应有振荡"

    def test_symmetric_fixture_symmetric_output(self) -> None:
        """对称源（|x-cx| 图案）+ 中心对称波场 → 输出关于中心列对称。"""
        xs = np.abs(np.arange(16, dtype=np.float32) - 7.5) / 7.5
        a = np.repeat(xs[None, :, None], 4, axis=0).repeat(3, axis=2)
        b = a.copy()
        out = np.asarray(core.evaluate(a, b, 0.5, {"amplitude": 0.25}), np.float64)
        np.testing.assert_allclose(out[:, :, 0], out[:, ::-1, 0], atol=1e-6)

    def test_damping_attenuates_far_field(self) -> None:
        """damping 越大远场位移越小：远列受波影响被 exp(-damping·r) 压低。"""
        xs = np.arange(32, dtype=np.float32) / 31.0
        a = np.repeat(xs[None, :, None], 4, axis=0).repeat(3, axis=2)
        b = np.zeros_like(a)
        kw = {"amplitude": 0.25, "center_x": 0.0, "center_y": 0.5, "damping": 0.3}
        d0 = np.abs(np.asarray(core.evaluate(a, b, 0.5, {**kw, "damping": 0.0}), np.float64)
                    - (0.5 * a))[:, :, 0]
        d1 = np.abs(np.asarray(core.evaluate(a, b, 0.5, {**kw, "damping": 1.0}), np.float64)
                    - (0.5 * a))[:, :, 0]
        assert d1[:, 24:].max() < d0[:, 24:].max()
        assert d1[:, 2:6].max() > d1[:, 24:].max()   # 近场仍受波影响

    def test_center_params_effective(self) -> None:
        a, b = ramp_pair()
        out1 = core.evaluate(a, b, 0.5, {"center_x": 0.2, "center_y": 0.3})
        out2 = core.evaluate(a, b, 0.5, {"center_x": 0.8, "center_y": 0.7})
        assert not np.allclose(out1, out2)

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
            core.evaluate(a, b, 0.5, {"amplitude": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"center_x": float("inf")}),
            core.evaluate(a, b, 0.5), atol=0)
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"frequency": "bad"})))

    def test_size_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        assert m.component_id == "X15" and m.slug == "ripple_refraction"
