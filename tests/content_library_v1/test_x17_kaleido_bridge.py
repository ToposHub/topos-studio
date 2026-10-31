"""X17 kaleido_bridge 测试：手算向量、端点比特级、两端原画语义、
中段折叠可见、p≤0.5 无 B 掺入、strength=0 溶解退化、折叠点独立重算、
防御、manifest 契约。"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.kaleido_bridge import kaleido_bridge_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "kaleido_bridge"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def ramp_pair():
    row_a = np.arange(8, dtype=np.float32) / 7.0
    row_b = 1.0 - row_a
    a = np.repeat(row_a[None, :, None], 4, axis=0).repeat(3, axis=2)
    b = np.repeat(row_b[None, :, None], 4, axis=0).repeat(3, axis=2)
    return a, b


def const_pair(av=0.2, bv=0.8):
    return (np.full((4, 8, 3), av, dtype=np.float32),
            np.full((4, 8, 3), bv, dtype=np.float32))


def _bilinear1(img: np.ndarray, u: float, v: float) -> float:
    """测试侧独立双线性（像素中心制、clamp）。"""
    h, w = img.shape[0], img.shape[1]
    xf = min(max(u * w - 0.5, 0.0), w - 1.0)
    yf = min(max(v * h - 0.5, 0.0), h - 1.0)
    x0, y0 = int(math.floor(xf)), int(math.floor(yf))
    x1, y1 = min(x0 + 1, w - 1), min(y0 + 1, h - 1)
    fx, fy = xf - x0, yf - y0
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return float(top * (1 - fy) + bot * fy)


class TestHandVectors:
    def test_vectors(self) -> None:
        a, b = ramp_pair()
        for vec in HAND["vectors"]:
            if "expected_rows" not in vec:
                continue
            out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
            np.testing.assert_allclose(out[:, :, 0], vec["expected_rows"], atol=1e-6,
                                       err_msg=vec["desc"])


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(-0.25, 4.5, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)
        au8 = (rng * 40).astype(np.uint8)
        np.testing.assert_array_equal(core.evaluate(au8, au8[::-1].copy(), 0.0), au8)

    def test_fold_point_independent_recompute(self) -> None:
        """strength=1、p=0.5：输出=纯折叠采样 A(u_f)；测试侧按 README 公式独立重算。"""
        a, _ = ramp_pair()
        b = np.full_like(a, 0.5)
        w, h = 8, 4
        kw = {"sectors": 6, "rotation": 30.0, "strength": 1.0}
        out = np.asarray(core.evaluate(a, b, 0.5, kw), np.float64)
        aspect = w / h
        rot = math.radians(30.0)
        sec = 2 * math.pi / 6
        for (j, i) in ((0, 1), (1, 4), (3, 6), (2, 2)):
            u, v = (i + 0.5) / w, (j + 0.5) / h
            rx, ry = (u - 0.5) * aspect, v - 0.5
            r = math.hypot(rx, ry)
            th = math.atan2(ry, rx)
            mm = (th - rot) % (2 * sec)
            af = min(mm, 2 * sec - mm)
            thf = rot + af
            fu = 0.5 + r * math.cos(thf) / aspect
            fv = 0.5 + r * math.sin(thf)
            assert abs(out[j, i, 0] - _bilinear1(a[:, :, 0], fu, fv)) < 1e-6, (j, i)

    def test_no_b_before_half(self) -> None:
        """p≤0.5（w_b=0）：输出与 B 无关（换 B 输出不变）。"""
        a, b1 = ramp_pair()
        b2 = np.full_like(b1, 0.95)
        for p in (0.1, 0.3, 0.5):
            np.testing.assert_array_equal(core.evaluate(a, b1, p),
                                          core.evaluate(a, b2, p))

    def test_fold_visible_mid_progress(self) -> None:
        """中段折叠可见：p∈(0,0.5] 输出≠A（strength>0）且不等于溶解。"""
        a, b = ramp_pair()
        kw = {"strength": 1.0, "sectors": 6}
        for p in (0.25, 0.5):
            out = np.asarray(core.evaluate(a, b, p, kw), np.float64)
            diss = (1 - p) * a + p * b if p > 0.5 else a
            assert not np.allclose(out, diss, atol=1e-3), f"p={p} 折叠不可见"

    def test_strength_zero_is_crossfade(self) -> None:
        a, b = ramp_pair()
        for p in (0.3, 0.6, 0.9):
            out = np.asarray(core.evaluate(a, b, p, {"strength": 0.0}), np.float64)
            w_b = max(0.0, min(1.0, 2 * p - 1))
            np.testing.assert_allclose(out, (1 - w_b) * a + w_b * b, atol=1e-6)

    def test_b_fades_in_only_after_half(self) -> None:
        """w_b=clamp(2p-1,0,1)：p≤0.5 无 B；p=0.75 恰半 B。"""
        a = np.full((4, 8, 3), 0.2, dtype=np.float32)
        b = np.full((4, 8, 3), 0.8, dtype=np.float32)
        out75 = np.asarray(core.evaluate(a, b, 0.75, {"strength": 0.0}), np.float64)
        np.testing.assert_allclose(out75, 0.5 * 0.2 + 0.5 * 0.8, atol=1e-6)
        out25 = np.asarray(core.evaluate(a, b, 0.25, {"strength": 1.0}), np.float64)
        # p=0.25 s_eff>0（折叠改变值域结构）但无 B（值域以 A 常色为主调）
        assert float(out25.max()) < 0.8

    def test_sectors_and_rotation_effective(self) -> None:
        a, b = ramp_pair()
        base = core.evaluate(a, b, 0.5, {"sectors": 6, "rotation": 0.0, "strength": 1.0})
        assert not np.allclose(base, core.evaluate(a, b, 0.5,
                                                   {"sectors": 3, "rotation": 0.0, "strength": 1.0}))
        assert not np.allclose(base, core.evaluate(a, b, 0.5,
                                                   {"sectors": 6, "rotation": 45.0, "strength": 1.0}))

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
            core.evaluate(a, b, 0.5, {"strength": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"rotation": float("inf")}),
            core.evaluate(a, b, 0.5), atol=0)
        # sectors 非法值钳到界内
        assert np.all(np.isfinite(core.evaluate(a, b, 0.5, {"sectors": 99})))

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
        assert m.component_id == "X17" and m.slug == "kaleido_bridge"
