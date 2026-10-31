"""X10 hinged_doors 测试：手算向量、端点比特级、门边单调、B 露出单调、
shadow 只作用门区、透视差量、防御、manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.hinged_doors import hinged_doors_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "hinged_doors"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def ramp_pair():
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
        bu8 = au8[::-1].copy()
        out1 = core.evaluate(au8, bu8, 1.0)
        assert out1.dtype == np.uint8
        np.testing.assert_array_equal(out1, bu8)

    def test_door_reveals_b_monotonically(self) -> None:
        """B 露出像素数随 p 单调不减（门边 W=1-0.999p 单调左移）。"""
        a, b = const_pair(0.2, 0.8)
        prev = 0
        for p in np.linspace(0.02, 0.98, 25):
            out = np.asarray(core.evaluate(a, b, float(p), {"shadow": 0.0}), np.float64)
            count = int((out[:, :, 0] > 0.5).sum())
            assert count >= prev, f"回退于 p={p}"
            prev = count

    def test_shadow_only_darkens_door(self) -> None:
        a, b = const_pair(0.4, 0.8)
        kw = {"hinge": "Left", "perspective": 0.0}
        no_shade = np.asarray(core.evaluate(a, b, 0.5, {**kw, "shadow": 0.0}), np.float64)
        shaded = np.asarray(core.evaluate(a, b, 0.5, {**kw, "shadow": 1.0}), np.float64)
        door = no_shade[:, :, 0] <= 0.4 + 1e-7
        behind = ~door
        assert (shaded[:, :, 0][door] < no_shade[:, :, 0][door] - 0.1).all()
        np.testing.assert_allclose(shaded[:, :, 0][behind], no_shade[:, :, 0][behind], atol=1e-7)

    def test_perspective_changes_door_sampling(self) -> None:
        """persp>0：远端（自由边）采样更压缩（近铰链近恒等）。"""
        a, b = ramp_pair()
        lin = np.asarray(core.evaluate(a, b, 0.5, {"hinge": "Left", "perspective": 0.0}), np.float64)
        per = np.asarray(core.evaluate(a, b, 0.5, {"hinge": "Left", "perspective": 1.0}), np.float64)
        assert not np.allclose(lin, per)
        # 铰链侧列（i=0，m 小）受透视影响最小；自由边侧差异大
        d0 = abs(per[0, 0, 0] - lin[0, 0, 0])
        d3 = abs(per[0, 3, 0] - lin[0, 3, 0])
        assert d3 > d0

    def test_both_hinge_shows_a_halves(self) -> None:
        """Both：p 小时左右门分别显示 A 的左右半幅（中缝 B）。"""
        a, b = const_pair(0.3, 0.9)
        out = np.asarray(core.evaluate(a, b, 0.5, {"hinge": "Both", "perspective": 0.0,
                                                   "shadow": 0.0}), np.float64)
        row = out[0, :, 0]
        assert (row[0] < 0.31) and (row[7] < 0.31)         # 门区 A（值域 ≤0.3）
        assert abs(row[3] - 0.9) < 1e-6 and abs(row[4] - 0.9) < 1e-6  # 中缝 B 精确

    def test_const_door_values_in_range(self) -> None:
        """shadow=1 p=0.5：门值 = 0.2·(1-0.5)=0.1、B 区 = 0.8——无出界值。"""
        a, b = const_pair(0.2, 0.8)
        out = np.asarray(core.evaluate(a, b, 0.5, {"shadow": 1.0}), np.float64)
        assert np.all(out >= 0.1 - 1e-7) and np.all(out <= 0.8 + 1e-7)

    def test_defense_bad_values_fall_back(self) -> None:
        a, b = const_pair()
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"perspective": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"shadow": float("inf")}),
            core.evaluate(a, b, 0.5), atol=0)
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"perspective": "x"})))

    def test_size_mismatch_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)

    def test_bad_hinge_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(*const_pair(), 0.5, {"hinge": "Top"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["hinge"] == core.HINGES
        assert m.image.sampling_border == "in-bounds-by-construction"
