"""X09 true_push 试点测试：手算向量、端点比特级、双源特征轨迹
（与 slide 的机器可辨差量）、方向变体、接缝移动、seam_softness。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.true_push import true_push_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "true_push"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _const_a_b(a_val: float, b_val: float, w: int = 4, h: int = 4):
    a = np.full((h, w, 3), a_val, dtype=np.float32)
    b = np.full((h, w, 3), b_val, dtype=np.float32)
    return a, b


def _pattern_img(pattern: list[float]) -> np.ndarray:
    row = np.asarray(pattern, dtype=np.float32)
    img = np.zeros((4, 4, 3), dtype=np.float32)
    img[:, :, 0] = row[None, :]
    return img


class TestHandVectors:
    def test_const_sources_p25(self) -> None:
        vec = next(v for v in HAND["vectors"] if "p=0.25" in v["desc"])
        a, b = _const_a_b(vec["a"], vec["b"])
        out = core.evaluate(a, b, vec["progress"])
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row"], atol=1e-6)
        np.testing.assert_allclose(out[2, :, 0], vec["expected_row"], atol=1e-6)

    def test_const_sources_p5(self) -> None:
        vec = next(v for v in HAND["vectors"] if "p=0.5" in v["desc"])
        a, b = _const_a_b(vec["a"], vec["b"])
        out = core.evaluate(a, b, vec["progress"])
        np.testing.assert_allclose(out[1, :, 0], vec["expected_row"], atol=1e-6)

    def test_a_feature_moves_left(self) -> None:
        vec = next(v for v in HAND["vectors"] if "A 特征左移" in v["desc"])
        out = core.evaluate(_pattern_img(vec["a_pattern"]), _pattern_img(vec["b_pattern"]),
                            vec["progress"])
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row"], atol=1e-6)

    def test_b_feature_moves_left(self) -> None:
        vec = next(v for v in HAND["vectors"] if "B 特征左移" in v["desc"])
        out = core.evaluate(_pattern_img(vec["a_pattern"]), _pattern_img(vec["b_pattern"]),
                            vec["progress"])
        np.testing.assert_allclose(out[0, :, 0], vec["expected_row"], atol=1e-6)


class TestSemantics:
    def test_endpoints_bit_exact(self) -> None:
        rng = np.linspace(-0.25, 4.5, 48, dtype=np.float32).reshape(4, 4, 3)
        a = rng.copy()
        b = rng[::-1].copy()
        np.testing.assert_array_equal(core.evaluate(a, b, 0.0), a)
        np.testing.assert_array_equal(core.evaluate(a, b, 1.0), b)

    def test_both_sources_move_differentiates_slide(self) -> None:
        """与 slide 的机器可辨差量：slide（wipe mask）中 A 原位不动；
        push 中 A 的采样坐标随 p 移动 → 同一 A 列在不同 p 出现在不同视口列。"""
        a = _pattern_img([0, 1, 0, 0])
        b = _pattern_img([0, 0, 0, 0])
        # A 特征在视口的位置随 p 左移：p=0 → i=1；p=0.25 → i=0
        out0 = core.evaluate(a, b, 0.0)
        out25 = core.evaluate(a, b, 0.25)
        assert int(np.argmax(out0[0, :, 0])) == 1
        assert int(np.argmax(out25[0, :, 0])) == 0

    def test_seam_moves_continuously(self) -> None:
        a, b = _const_a_b(0.2, 0.8)
        for p, expected_b_count in ((0.25, 1), (0.5, 2), (0.75, 3)):
            out = core.evaluate(a, b, p)
            assert int((out[0, :, 0] > 0.5).sum()) == expected_b_count, f"p={p}"

    def test_direction_variants(self) -> None:
        a, b = _const_a_b(0.2, 0.8)
        # right：B 自左进入
        out = core.evaluate(a, b, 0.5, {"direction": "right"})
        np.testing.assert_allclose(out[0, :, 0], [0.8, 0.8, 0.2, 0.2], atol=1e-6)
        # up：垂直轴，B 自下进入（顶部先 B? —— up=A 向上推出，B 自底部进入）
        out_up = core.evaluate(a, b, 0.5, {"direction": "up"})
        col = out_up[:, 0, 0]
        np.testing.assert_allclose(col, [0.2, 0.2, 0.8, 0.8], atol=1e-6)
        out_down = core.evaluate(a, b, 0.5, {"direction": "down"})
        np.testing.assert_allclose(out_down[:, 0, 0], [0.8, 0.8, 0.2, 0.2], atol=1e-6)

    def test_seam_softness_blends(self) -> None:
        a, b = _const_a_b(0.2, 0.8)
        hard = core.evaluate(a, b, 0.5)
        soft = core.evaluate(a, b, 0.5, {"seam_softness": 0.2})
        # 硬接缝在 u=0.5（i=1/2 之间）；软化后中间像素出现 (0,1) 开间值
        vals = soft[0, :, 0]
        assert np.all(vals > 0.2 - 1e-6) and np.all(vals < 0.8 + 1e-6)
        assert np.any((vals > 0.21) & (vals < 0.79)), "接缝处应出现混合值"
        # 远离接缝处与硬接缝一致
        np.testing.assert_allclose(vals[0], hard[0, 0, 0], atol=1e-6)
        np.testing.assert_allclose(vals[3], hard[0, 3, 0], atol=1e-6)

    def test_sources_always_in_bounds(self) -> None:
        """边界结论验证：正规 push 采样坐标恒在界内——随机图案无边框伪影。"""
        a = np.repeat(np.arange(8, dtype=np.float32)[None, :, None], 8, axis=0)
        b = np.repeat((np.arange(8, dtype=np.float32) * -0.5)[:, None, None], 8, axis=1)
        for p in np.linspace(0.02, 0.98, 13):
            for d in ("left", "right", "up", "down"):
                out = core.evaluate(a, b, float(p), {"direction": d})
                assert np.all(np.isfinite(out))

    def test_size_mismatch_rejected(self) -> None:
        a = np.zeros((4, 4, 3), dtype=np.float32)
        b = np.zeros((5, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError):
            core.evaluate(a, b, 0.5)

    def test_bad_direction_rejected(self) -> None:
        a = b = np.zeros((4, 4, 3), dtype=np.float32)
        with pytest.raises(ValueError):
            core.evaluate(a, b, 0.5, {"direction": "sideways"})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
