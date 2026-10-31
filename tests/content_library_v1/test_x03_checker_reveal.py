"""X03 checker_reveal 测试：手算向量（奇偶两波/羽化/phase/非方格）、端点
精确、单调、棋盘交错参数有效性、防御回退、manifest 契约。

语义修正记录：初版核 trig=(r/rows+c/columns)/2 为反对角波次场（同行单调、
非棋盘），交付自带的交错语义测试当场抓出；修正为 (r+c)%2 奇偶两波，
本文件向量按修正语义重算。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.transitions.checker_reveal import checker_reveal_core as core

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "transitions" / "checker_reveal"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _vec(note_snippet: str):
    for vec in HAND["vectors"]:
        if note_snippet in vec["note"]:
            return vec
    raise KeyError(note_snippet)


def _mask(vec) -> np.ndarray:
    assert vec["fixture"] == "4x4"
    return core.evaluate_mask(4, 4, vec["progress"], vec["params"])


class TestHandVectors:
    def test_parity0_cells_only(self) -> None:
        vec = _vec("parity-0 格")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_parity1_wave_midstate(self) -> None:
        vec = _vec("parity-1 波过半")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_softness_feather_exact_half(self) -> None:
        vec = _vec("羽化向量")
        np.testing.assert_allclose(_mask(vec), np.array(vec["expected_grid_rows"]), atol=1e-9)

    def test_phase_shift(self) -> None:
        vec = _vec("相位可辨")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))

    def test_non_square_grid(self) -> None:
        vec = _vec("行波")
        np.testing.assert_array_equal(_mask(vec), np.array(vec["expected_grid_rows"], dtype=np.float32))


class TestSemantics:
    def test_endpoints_exact(self) -> None:
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 0.0), 0.0)
        np.testing.assert_array_equal(core.evaluate_mask(16, 8, 1.0), 1.0)

    def test_monotone_in_progress(self) -> None:
        prev = core.evaluate_mask(24, 24, 0.0, {"rows": 4, "columns": 4, "softness": 0.2})
        for p in np.linspace(0.02, 1.0, 25):
            mask = core.evaluate_mask(24, 24, float(p), {"rows": 4, "columns": 4, "softness": 0.2})
            assert np.all(mask >= prev - 1e-7), f"p={p} 出现回退"
            prev = mask

    def test_checker_interleave_not_single_wipe(self) -> None:
        """视觉区分点：棋盘交错——同一行内 0/1 交替（单幅 wipe 一行内单调）。"""
        mask = core.evaluate_mask(32, 32, 0.45, {"rows": 4, "columns": 4, "softness": 0.0})
        mid_row = mask[16]
        transitions = int(np.sum(np.abs(np.diff(mid_row)) > 0.5))
        assert transitions >= 2, "行内多段边界 = 二维交错，非单幅 wipe"

    def test_params_have_effect(self) -> None:
        base = core.evaluate_mask(16, 16, 0.4)
        assert not np.allclose(core.evaluate_mask(16, 16, 0.4, {"rows": 8}), base)
        assert not np.allclose(core.evaluate_mask(16, 16, 0.4, {"columns": 8}), base)
        # phase=0.6 在 p=0.4 处交换两波可见性（0.3 只平移仍同可见性，不可辨）
        assert not np.allclose(core.evaluate_mask(16, 16, 0.4, {"phase": 0.6}), base)
        # softness：p=0.2 落在 parity-0 波羽化带内（0.2/0.4=0.5），与硬阈值二值可辨
        assert not np.allclose(core.evaluate_mask(16, 16, 0.2, {"softness": 0.4}),
                               core.evaluate_mask(16, 16, 0.2))

    def test_mask_range_bounded(self) -> None:
        for p in (0.13, 0.5, 0.87):
            mask = core.evaluate_mask(32, 16, p, {"softness": 0.5, "rows": 3, "columns": 5})
            assert float(mask.min()) >= 0.0 and float(mask.max()) <= 1.0

    def test_bad_params_fall_back_no_raise(self) -> None:
        default = core.evaluate_mask(16, 8, 0.5)
        bad = core.evaluate_mask(16, 8, 0.5, {
            "rows": float("nan"), "columns": float("inf"),
            "phase": None, "softness": "x"})
        np.testing.assert_array_equal(bad, default)
        for rc in (0, -3, 999):
            mask = core.evaluate_mask(16, 8, 0.5, {"rows": rc, "columns": rc})
            assert mask.shape == (8, 16) and np.all(np.isfinite(mask))


class TestManifest:
    def test_manifest_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate(staging=True) == []
        assert m.kind == "transition" and m.slug == "checker_reveal"
        assert m.time.time_domain == "static"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
