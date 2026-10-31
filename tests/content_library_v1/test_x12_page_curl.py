"""X12 page_curl 测试：手算向量、端点比特级、三区语义（平铺/卷区/B）、
镜像方向、back_color/shadow 有效性、防御、manifest 契约。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.page_curl import page_curl_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "page_curl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

BACK_RED = [1.0, 0.0, 0.0]


def ramp_pair():
    row_a = np.arange(8, dtype=np.float32) / 7.0
    row_b = 10.0 + np.arange(8, dtype=np.float32)
    a = np.repeat(row_a[None, :, None], 4, axis=0).repeat(3, axis=2)
    b = np.repeat(row_b[None, :, None], 4, axis=0).repeat(3, axis=2)
    return a, b


class TestHandVectors:
    def test_vectors(self) -> None:
        a, b = ramp_pair()
        for vec in HAND["vectors"]:
            if "expected_row0_ch0" not in vec:
                continue
            out = np.asarray(core.evaluate(a, b, vec["progress"], vec["params"]), np.float64)
            np.testing.assert_allclose(out[0, :, 0], vec["expected_row0_ch0"], atol=1e-6,
                                       err_msg=vec["desc"])


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(-0.25, 4.5, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)
        au8 = (rng * 40).astype(np.uint8)
        np.testing.assert_array_equal(core.evaluate(au8, au8[::-1].copy(), 1.0), au8[::-1])

    def test_zone_semantics(self) -> None:
        """p=0.5 r=0.25 Left（x_c=0.375）：i=0 平铺 A 精确、i≥3（u≥0.4375）B 精确、
        i∈{1,2}（u∈[0.1875,0.3125]）卷区混 back_color（R 向 1 拉、G 向 0 拉）。"""
        a, b = ramp_pair()
        out = np.asarray(core.evaluate(a, b, 0.5, {"direction": "Left", "radius": 0.25,
                                                   "shadow": 0.0, "back_color": BACK_RED}),
                         np.float64)
        row_r, row_g = out[0, :, 0], out[0, :, 1]
        assert abs(row_r[0] - a[0, 0, 0]) < 1e-6           # 平铺区 A 原位
        for i in (3, 4, 5, 6, 7):
            assert abs(row_r[i] - b[0, i, 0]) < 1e-6       # B 区原位
        for i in (1, 2):
            assert row_r[i] > a[0, i, 0] + 1e-3            # R 向 back(1) 拉
            assert 0.0 < row_g[i] < a[0, i, 1] - 1e-3      # G 向 back(0) 拉

    def test_direction_mirror_is_exact(self) -> None:
        """Right 输出 = 镜像源在 Left 下的输出镜像（偶宽像素中心对称）。"""
        a, b = ramp_pair()
        kw = {"radius": 0.25, "shadow": 0.5, "back_color": BACK_RED}
        left = core.evaluate(a, b, 0.5, {**kw, "direction": "Left"})
        right = core.evaluate(a[:, ::-1], b[:, ::-1], 0.5, {**kw, "direction": "Right"})
        np.testing.assert_allclose(right[:, ::-1], left, atol=1e-6)

    def test_curl_zone_sampling_mirrored_texture(self) -> None:
        """卷区源坐标 x_src=x_c+s 随 t 减小而增大：非对称特征在卷区顺序翻转。"""
        a = np.zeros((1, 8, 3), dtype=np.float32)
        a[0, 5, 0] = 1.0          # 单一亮列（源 u=0.6875）
        b = np.zeros((1, 8, 3), dtype=np.float32)
        # p=0.5 r=0.5 → x_c=0.25，卷区 [−0.25,0.25]∩[0,1]=[0,0.25]（i=0）
        out = np.asarray(core.evaluate(a, b, 0.5, {"direction": "Left", "radius": 0.5,
                                                   "shadow": 0.0, "back_color": [0.0] * 3}),
                         np.float64)
        # i=0（t=0.0625）：φ=atan… 由 (0.25-0.0625)/0.5·90°=33.75°，s≈0.1181，
        # x_src≈0.3681≠0.6875 → 该亮列不出现在卷区 i=0（back=0 时 G/R≈A(0.368)≈0）
        assert out[0, 0, 0] < 1e-6

    def test_back_color_effective(self) -> None:
        a, b = ramp_pair()
        kw = {"direction": "Left", "radius": 0.25, "shadow": 0.0}
        r1 = np.asarray(core.evaluate(a, b, 0.5, {**kw, "back_color": BACK_RED}), np.float64)
        g1 = np.asarray(core.evaluate(a, b, 0.5, {**kw, "back_color": [0.0, 1.0, 0.0]}), np.float64)
        assert not np.allclose(r1[0, 1:4], g1[0, 1:4])      # 卷区颜色随 back_color 变

    def test_shadow_darkens_curl_only(self) -> None:
        a, b = ramp_pair()
        kw = {"direction": "Left", "radius": 0.25, "back_color": BACK_RED}
        s0 = np.asarray(core.evaluate(a, b, 0.5, {**kw, "shadow": 0.0}), np.float64)
        s1 = np.asarray(core.evaluate(a, b, 0.5, {**kw, "shadow": 1.0}), np.float64)
        assert (s1[0, 1:3, 1] < s0[0, 1:3, 1] - 1e-3).all()  # 卷区（i=1,2）G 变暗
        np.testing.assert_allclose(s1[0, 3:, 1], s0[0, 3:, 1], atol=1e-7)  # B 区不受
        np.testing.assert_allclose(s1[0, 0, 1], s0[0, 0, 1], atol=1e-7)    # 平铺区不受

    def test_curl_line_sweeps(self) -> None:
        """B 区（t>x_c）面积随 p 单调不减。"""
        a, b = ramp_pair()
        prev = 0
        for p in np.linspace(0.05, 0.95, 19):
            out = np.asarray(core.evaluate(a, b, float(p), {"radius": 0.25, "shadow": 0.0}),
                             np.float64)
            count = int((np.abs(out[:, :, 0] - b[:, :, 0]) < 1e-6).sum())
            assert count >= prev, f"回退于 p={p}"
            prev = count

    def test_defense_bad_values_fall_back(self) -> None:
        a, b = ramp_pair()
        np.testing.assert_allclose(
            core.evaluate(a, b, 0.5, {"radius": float("nan")}),
            core.evaluate(a, b, 0.5), atol=0)
        # back_color 坏值（2 元/NaN）回退默认，不崩溃
        out2 = core.evaluate(a, b, 0.5, {"back_color": [0.5, 0.5]})
        out_nan = core.evaluate(a, b, 0.5, {"back_color": [float("nan"), 0.1, 0.1]})
        assert np.all(np.isfinite(out2)) and np.all(np.isfinite(out_nan))

    def test_size_mismatch_and_bad_direction_rejected(self) -> None:
        with pytest.raises(ValueError):
            core.evaluate(np.zeros((4, 4, 3)), np.zeros((4, 5, 3)), 0.5)
        with pytest.raises(ValueError):
            core.evaluate(*ramp_pair(), 0.5, {"direction": "Up"})


class TestManifest:
    def test_manifest_valid_and_params_match_core(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []
        assert set(p.name for p in m.params) == set(core.DEFAULTS.keys())
        back = next(p for p in m.params if p.name == "back_color")
        assert back.ptype == "color" and tuple(back.default) == tuple(core.DEFAULTS["back_color"])
        opt_params = {p.name: p.options for p in m.params if p.ptype == "option"}
        assert opt_params["direction"] == core.DIRECTIONS
