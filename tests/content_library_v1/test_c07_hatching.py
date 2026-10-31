"""C07 lib1_hatching 测试（复用 F14）：marker 协议 + 参数透传至 F14 共享核
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

from content_staging.library_v1.references import hatching_ref
from content_staging.library_v1.nodes.hatching.lib1_hatching_node import (
    EFFECT_KEY, HATCHING_LAYERS, Lib1HatchingNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "hatching" / "component_manifest.json")


class TestC07NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1HatchingNode()
        tex = TextureResult(texture_id=51, width=24, height=12)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_hatching"
        assert out["_effect_input"] is tex
        for name in ("line_spacing", "angle", "layers", "ink_color",
                     "paper", "amount"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1HatchingNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_param_defs_match_f14_defaults(self) -> None:
        node = Lib1HatchingNode()
        assert {p.name for p in node.PARAM_DEFS} == set(hatching_ref.DEFAULTS)
        for name, default in hatching_ref.DEFAULTS.items():
            assert node.parameters[name] == default, name
        assert HATCHING_LAYERS == ["1", "2"]
        assert node._tile_padding({}) == 0


class TestC07PassthroughToF14:
    def test_marker_params_drive_f14_core(self) -> None:
        node = Lib1HatchingNode()
        node.parameters["amount"] = 0.35
        node.parameters["angle"] = 30.0
        node.parameters["layers"] = "1"
        out = node._do_evaluate(
            {"image": TextureResult(texture_id=1, width=8, height=8)},
            ctx=None, fb_manager=None)
        p = dict(out["_effect_params"])
        img = np.full((8, 8, 3), 0.4, dtype=np.float32)
        np.testing.assert_allclose(
            hatching_ref.evaluate(img, p),
            hatching_ref.evaluate(img, {"amount": 0.35, "angle": 30.0,
                                        "layers": "1"}),
            atol=1e-7)


class TestC07BorrowedHandVectors:
    def test_amount_zero_identity(self) -> None:
        # 手算 1：amount=0 精确恒等（含 HDR）
        img = np.array([[[0.1, 0.5, 0.9], [2.0, -0.3, 1.5]]], dtype=np.float32)
        np.testing.assert_array_equal(
            hatching_ref.evaluate(img, {"amount": 0.0}), img)

    def test_full_black_field_full_ink(self) -> None:
        # 手算 2：黑场（luma=0 → D=1）→ h=0.5+2AA−AA=0.5+AA → 条带满幅：
        # cov=1 → out = ink_color（amount=1）。numpy 2.5 assert_allclose
        # 拒绝可广播的不同形状 → 显式 broadcast_to
        img = np.zeros((8, 8, 3), dtype=np.float32)
        out = hatching_ref.evaluate(img)
        ink = np.broadcast_to(np.asarray(hatching_ref.DEFAULTS["ink_color"],
                                         dtype=np.float32), out.shape)
        np.testing.assert_allclose(out, ink, atol=1e-6)

    def test_full_white_field_bare_paper(self) -> None:
        # 手算 3：白场（luma=1 → D=0）→ h=−AA → cov=0 → out = paper = 0.9
        img = np.ones((8, 8, 3), dtype=np.float32)
        out = hatching_ref.evaluate(img)
        np.testing.assert_allclose(out, 0.9, atol=1e-6)


class TestC07Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C07"
        node = Lib1HatchingNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        assert "hatching_ref" in manifest.verification.oracle  # F14 单一事实源


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
