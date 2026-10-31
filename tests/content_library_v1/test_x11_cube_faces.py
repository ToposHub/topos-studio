"""X11 cube_faces 测试：手算向量、端点比特级、共棱单调、无露底、
面阴影语义、Vertical 轴、防御、manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.cube_faces import cube_faces_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "cube_faces"
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
        np.testing.assert_array_equal(core.evaluate(au8, au8[::-1].copy(), 0.0), au8)

    def test_shared_edge_sweeps_b_monotonically(self) -> None:
        """B 面覆盖像素数随 p 单调不减（共棱 x=p 线性右移）。"""
        a, b = const_pair(0.2, 0.8)
        prev = 0
        for p in np.linspace(0.02, 0.98, 25):
            out = np.asarray(core.evaluate(a, b, float(p), {"shadow": 0.0}), np.float64)
            count = int((out[:, :, 0] > 0.5).sum())
            assert count >= prev, f"回退于 p={p}"
            prev = count

    def test_no_background_hole(self) -> None:
        """覆盖完备性：任意 p 每像素都是 A/B 面采样（shadow=0 常色源 → 值只能是
        {0.2, 0.8}，无第三值即无露底）。"""
        a, b = const_pair(0.2, 0.8)
        for p in np.linspace(0.01, 0.99, 33):
            out = np.asarray(core.evaluate(a, b, float(p), {"shadow": 0.0}), np.float64)
            near_a = np.abs(out - 0.2) < 1e-6
            near_b = np.abs(out - 0.8) < 1e-6
            assert np.all(near_a | near_b), f"露底 p={p}"

    def test_face_shadow_semantics(self) -> None:
        """shadow 只压暗对应面：A 面随 p 变暗、B 面随 p 变亮（视宽增长）。"""
        a, b = const_pair(0.8, 0.8)
        vals_a, vals_b = [], []
        for p in (0.2, 0.5, 0.8):
            out = np.asarray(core.evaluate(a, b, p, {"shadow": 1.0, "axis": "Horizontal"}),
                             np.float64)
            vals_a.append(float(out[0, 7, 0]))   # 右缘恒 A 面
            vals_b.append(float(out[0, 0, 0]))   # 左缘恒 B 面
        assert vals_a[0] > vals_a[1] > vals_a[2]         # A 面单调变暗
        assert vals_b[0] < vals_b[1] < vals_b[2]         # B 面单调变亮
        assert vals_a[2] >= 1e-3 and vals_b[2] <= 0.8 + 1e-6

    def test_vertical_axis_differs(self) -> None:
        a = np.zeros((4, 8, 3), dtype=np.float32)
        a[:, :4] = 0.6          # 左半亮
        b = np.full((4, 8, 3), 0.2, dtype=np.float32)
        kw = {"perspective": 0.0, "shadow": 0.0}
        h = np.asarray(core.evaluate(a, b, 0.5, {**kw, "axis": "Horizontal"}), np.float64)
        v = np.asarray(core.evaluate(a, b, 0.5, {**kw, "axis": "Vertical"}), np.float64)
        assert not np.allclose(h, v)
        # 横轴：共棱竖直 x=0.5 → 列 0..2 为 B(0.2)、列 4..7 为 A 面采样
        assert abs(h[0, 0, 0] - 0.2) < 1e-6

    def test_perspective_effective(self) -> None:
        a, b = ramp_pair()
        lin = np.asarray(core.evaluate(a, b, 0.5, {"perspective": 0.0}), np.float64)
        per = np.asarray(core.evaluate(a, b, 0.5, {"perspective": 1.0}), np.float64)
        assert not np.allclose(lin, per)
        # A 面共棱侧（m→0）透视近恒等 → 远端差异更大
        assert abs(per[0, 4, 0] - lin[0, 4, 0]) < abs(per[0, 6, 0] - lin[0, 6, 0]) + 1e-3

    def test_defense_bad_values_fall_back(self) -> None:
        a, b = const_pair()
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"perspective": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"shadow": float("inf")}),
            core.evaluate(a, b, 0.5), atol=0)

    def test_size_mismatch_and_bad_axis_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)
        with pytest.raises(ValueError):
            core.evaluate(*const_pair(), 0.5, {"axis": "Diagonal"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["axis"] == core.AXES
        assert m.image.sampling_border == "in-bounds-by-construction"
