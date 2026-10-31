"""X18 burn_reveal 测试：手算向量（Field/emission 两态/seed/Edge-Field 分叉/
RGB 橙→白）、端点比特级还原 A/B、焦边存在性（与 X06 的差量证明）、防御、
manifest。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.burn_reveal import burn_reveal_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "burn_reveal"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _const_ab(a_val: float, b_val: float, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    a = np.full((h, w), a_val, dtype=np.float32)
    b = np.full((h, w), b_val, dtype=np.float32)
    return a, b


class TestHandVectors:
    def test_field_baseline(self) -> None:
        vec = _vec("Field 基线")
        a, b = _const_ab(0.1, 0.9, 4, 4)
        out = core.evaluate(a, b, vec["progress"], vec["params"])
        np.testing.assert_allclose(out, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_emission_zero_pure_mix(self) -> None:
        vec = _vec("emission=0")
        a, b = _const_ab(0.1, 0.9, 4, 4)
        out = core.evaluate(a, b, vec["progress"], vec["params"])
        np.testing.assert_allclose(out, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_seed_changes_pattern(self) -> None:
        vec = _vec("seed=1 图案改变")
        a, b = _const_ab(0.1, 0.9, 4, 4)
        out = core.evaluate(a, b, vec["progress"], vec["params"])
        np.testing.assert_allclose(out, np.array(vec["expected_grid_rows"]), atol=1e-6)

    def test_field_vs_edge_on_6x4(self) -> None:
        """pattern 分叉：6x4 上 Field（双线性）与 Edge（最近格点）图案不同。"""
        a, b = _const_ab(0.1, 0.9, 6, 4)
        field = core.evaluate(a, b, 0.5, {"pattern": "Field", "char_width": 0.1,
                                          "emission": 0.5, "seed": 0})
        edge = core.evaluate(a, b, 0.5, {"pattern": "Edge", "char_width": 0.1,
                                         "emission": 0.5, "seed": 0})
        vec_field = _vec("Field=双线性 value rank")
        vec_edge = _vec("Edge=最近格点 rank")
        np.testing.assert_allclose(field, np.array(vec_field["expected_grid_rows"]), atol=1e-6)
        np.testing.assert_allclose(edge, np.array(vec_edge["expected_grid_rows"]), atol=1e-6)
        assert not np.allclose(field, edge)

    def test_rgb_orange_to_white_heat(self) -> None:
        vec = _vec("RGB 焦边橙→白")
        a = np.full((4, 4, 3), 0.1, dtype=np.float32)
        b = np.full((4, 4, 3), 0.9, dtype=np.float32)
        out = core.evaluate(a, b, vec["progress"], vec["params"])
        px = out[vec["pixel"][1], vec["pixel"][0]]
        np.testing.assert_allclose(px, vec["expected_value_rgb"], atol=1e-6)
        assert px[0] > px[1] > px[2], "白热前沿 R≥G≥B 的橙→白梯度"


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(0.05, 0.95, 96, dtype=np.float32).reshape(4, 8, 3)
        a, b = rng.copy(), rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)
        np.testing.assert_array_equal(core.evaluate(a, b, -0.2), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.3), b)

    def test_endpoints_bit_exact_uint8(self) -> None:
        a = np.arange(96, dtype=np.uint8).reshape(4, 8, 3)
        b = a[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)

    def test_charred_edge_differentiates_x06(self) -> None:
        """与 X06 的差量由焦边证明：emission>0 时前沿带出现 >base 的加性热色。"""
        a, b = _const_ab(0.1, 0.9, 32, 32)
        glow = core.evaluate(a, b, 0.5, {"emission": 1.0, "char_width": 0.1})
        pure = core.evaluate(a, b, 0.5, {"emission": 0.0, "char_width": 0.1})
        assert not np.allclose(glow, pure)
        # 前沿带：glow 超出 mix 的 [0.1, 0.9] 基线上界（加性发光）
        assert float(glow.max()) > 0.9 + 1e-6
        # emission=0 时严格退化为 base 区间
        assert float(pure.min()) >= 0.1 - 1e-6 and float(pure.max()) <= 0.9 + 1e-6

    def test_params_have_effect(self) -> None:
        a, b = _const_ab(0.1, 0.9, 16, 16)
        base = core.evaluate(a, b, 0.5)
        assert not np.allclose(core.evaluate(a, b, 0.5, {"pattern": "Edge"}), base)
        assert not np.allclose(core.evaluate(a, b, 0.5, {"char_width": 0.3}), base)
        assert not np.allclose(core.evaluate(a, b, 0.5, {"emission": 0.0}), base)
        assert not np.allclose(core.evaluate(a, b, 0.5, {"seed": 3}), base)

    def test_monotone_reveal_without_heat(self) -> None:
        """emission=0 时退化为 rank 揭示 mix：逐像素随 p 单调不减。

        图像须满足 a ≤ b：out = a·(1−mask)+b·mask 随 mask 增大从 a 走向 b，
        a > b 的像素随揭示**变暗**（mask 单调 ≠ 输出值单调）——初版用
        b=1−a 在 a>0.5 像素上误断输出单调，属测试侧错误，已修正。"""
        a = np.tile(np.linspace(0, 0.5, 16, dtype=np.float32), (16, 1))
        b = np.tile(np.linspace(0.5, 1.0, 16, dtype=np.float32), (16, 1))
        prev = core.evaluate(a, b, 0.0, {"emission": 0.0, "char_width": 0.15})
        for p in np.linspace(0.05, 1.0, 20):
            out = core.evaluate(a, b, float(p), {"emission": 0.0, "char_width": 0.15})
            assert np.all(out >= prev - 1e-6), f"p={p} 出现回退"
            prev = out

    def test_size_mismatch_rejected(self) -> None:
        a = np.zeros((4, 4, 3), dtype=np.float32)
        b = np.zeros((5, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError):
            core.evaluate(a, b, 0.5)

    def test_bad_params_fall_back_no_raise(self) -> None:
        a, b = _const_ab(0.1, 0.9, 8, 8)
        default = core.evaluate(a, b, 0.5)
        bad = core.evaluate(a, b, 0.5, {
            "pattern": "Plasma", "char_width": float("nan"),
            "emission": float("inf"), "seed": "x"})
        np.testing.assert_array_equal(bad, default)
        for s in (-3, 500, 2.6):
            out = core.evaluate(a, b, 0.5, {"seed": s})
            assert out.shape == (8, 8) and np.all(np.isfinite(out))


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "burn_reveal"
        assert m.time.time_domain == "static"
        assert m.time.seed_policy == "fixed-param-default"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
