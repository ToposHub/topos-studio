"""X01 clock_wipe 试点测试：手算向量（方向/起始边/羽化/aspect）、
端点精确、单调性、中心连续、滑杆有效性。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.clock_wipe import clock_wipe_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "clock_wipe"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(name_snippet: str):
    for vec in HAND["vectors"]:
        if name_snippet in json.dumps(vec, ensure_ascii=False):
            return vec
    raise KeyError(name_snippet)


class TestHandVectors:
    def test_cw_quadrant_reveal(self) -> None:
        vec = _vec("顺时针自北扫")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"])
        np.testing.assert_array_equal(mask, np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_ccw_mirror(self) -> None:
        vec = _vec("逆时针镜像")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"])
        np.testing.assert_array_equal(mask, np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_start_angle_rotation(self) -> None:
        vec = _vec("起始边转 90")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"])
        np.testing.assert_array_equal(mask, np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_softness_feather(self) -> None:
        vec = _vec("羽化向量")
        mask = core.evaluate_mask(4, 4, vec["progress"], vec["params"])
        np.testing.assert_allclose(
            float(mask[vec["pixel"][1], vec["pixel"][0]]), vec["expected_value"], atol=1e-6)

    def test_aspect_correction_pinned(self) -> None:
        vec = _vec("aspect=2 校正")
        mask = core.evaluate_mask(8, 4, vec["progress"], vec["params"])
        assert float(mask[vec["pixel"][1], vec["pixel"][0]]) == vec["expected_value"]


class TestSemantics:
    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0), 1.0)

    def test_monotone_in_progress(self) -> None:
        prev = core.evaluate_mask(24, 12, 0.0)
        for p in np.linspace(0.05, 1.0, 20):
            mask = core.evaluate_mask(24, 12, float(p))
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            assert float(mask.mean()) == pytest.approx(p, abs=0.08)  # 面积近似线性
            prev = mask

    def test_center_revealed_first(self) -> None:
        # 中心像素 atan2(0,0)=0 → rel=0 → 极小 p 即接近全揭示
        mask = core.evaluate_mask(9, 9, 1e-4, {"softness": 0.0})
        assert float(mask[4, 4]) == 1.0

    def test_params_have_effect(self) -> None:
        base = core.evaluate_mask(16, 8, 0.5)
        assert not np.allclose(core.evaluate_mask(16, 8, 0.5, {"center_x": 0.2}), base)
        assert not np.allclose(core.evaluate_mask(16, 8, 0.5, {"center_y": 0.3}), base)
        assert not np.allclose(core.evaluate_mask(16, 8, 0.5, {"start_angle": 90.0}), base)
        assert not np.allclose(core.evaluate_mask(16, 8, 0.5, {"clockwise": False}), base)
        assert not np.allclose(core.evaluate_mask(16, 8, 0.5, {"softness": 0.3}), base)

    def test_mask_range_bounded(self) -> None:
        for p in (0.13, 0.5, 0.87):
            mask = core.evaluate_mask(32, 16, p, {"softness": 0.4})
            assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0

    def test_deterministic_repeat(self) -> None:
        a = core.evaluate_mask(20, 10, 0.37, {"softness": 0.1})
        b = core.evaluate_mask(20, 10, 0.37, {"softness": 0.1})
        np.testing.assert_array_equal(a, b)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
