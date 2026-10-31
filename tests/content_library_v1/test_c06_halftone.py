"""C06 lib1_halftone 测试（复用 F13）：marker 协议 + 参数透传至 F13 共享核
（单一事实源）+ 借用手算语义锚点（≥3 独立数值断言）+ manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import halftone_ref, print_registration_ref
from content_staging.library_v1.nodes.halftone.lib1_halftone_node import (
    EFFECT_KEY, SEPARATION_MODES, Lib1HalftoneNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "halftone" / "component_manifest.json")


class TestC06NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1HalftoneNode()
        tex = TextureResult(texture_id=41, width=32, height=16)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_halftone"
        assert out["_effect_input"] is tex
        for name in ("cmyk_separation", "reg_offset_x", "reg_offset_y",
                     "paper_base", "ink_amount"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1HalftoneNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_param_defs_match_f13_defaults(self) -> None:
        node = Lib1HalftoneNode()
        assert {p.name for p in node.PARAM_DEFS} == set(halftone_ref.DEFAULTS)
        for name, default in halftone_ref.DEFAULTS.items():
            assert node.parameters[name] == default, name
        assert SEPARATION_MODES == ["Luma", "CMY approx"]
        assert node._tile_padding({}) == 2  # max(|1.0|, 0)+1

    def test_reexport_single_source(self) -> None:
        assert halftone_ref.SOURCE_REF == "print_registration_ref"
        assert halftone_ref.evaluate is print_registration_ref.evaluate


class TestC06PassthroughToF13:
    def test_marker_params_drive_f13_core(self) -> None:
        node = Lib1HalftoneNode()
        node.parameters["ink_amount"] = 0.4
        node.parameters["reg_offset_x"] = -2.0
        out = node._do_evaluate(
            {"image": TextureResult(texture_id=1, width=4, height=4)},
            ctx=None, fb_manager=None)
        p = dict(out["_effect_params"])
        img = np.full((4, 4, 3), 0.3, dtype=np.float32)
        np.testing.assert_allclose(
            halftone_ref.evaluate(img, p),
            halftone_ref.evaluate(img, {"ink_amount": 0.4, "reg_offset_x": -2.0}),
            atol=1e-7)


class TestC06BorrowedHandVectors:
    def test_ink_amount_zero_identity(self) -> None:
        # 手算 1：ink_amount=0 精确恒等（任意 paper_base）
        img = np.array([[[0.1, 0.5, 0.9], [1.2, 0.0, -0.3]]], dtype=np.float32)
        for paper in (0.0, 0.9, 1.0):
            np.testing.assert_array_equal(
                halftone_ref.evaluate(img, {"ink_amount": 0.0, "paper_base": paper}),
                img)

    def test_white_paper_stays_white(self) -> None:
        # 手算 2：全 1.0 输入 → cov=0（r=sqrt(1−1)=0 无网点）→ 输出=1.0
        img = np.ones((4, 4, 3), dtype=np.float32)
        out = halftone_ref.evaluate(img)
        np.testing.assert_allclose(out, 1.0, atol=1e-6)

    def test_black_field_full_ink_coverage(self) -> None:
        # 手算 3：黑场（0,0,0）→ r=sqrt(1−0)=1 → cov=1（全覆盖）；
        # paper = 0·(1−0.72)+0.72 = 0.72；out = 0.72·(1−0.8) = 0.144
        img = np.zeros((4, 4, 3), dtype=np.float32)
        out = halftone_ref.evaluate(img)
        np.testing.assert_allclose(out, 0.144, atol=1e-6)


class TestC06Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C06"
        node = Lib1HalftoneNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        assert "print_registration" in manifest.verification.oracle


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
