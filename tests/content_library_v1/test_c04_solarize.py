"""C04 lib1_solarize 测试（复用 F17）：marker 协议 + 参数透传至 F17 共享核
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

from content_staging.library_v1.references import solarize_ref
from content_staging.library_v1.nodes.solarize.lib1_solarize_node import (
    EFFECT_KEY, SOLARIZE_CHANNEL_MODES, Lib1SolarizeNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "solarize" / "component_manifest.json")


class TestC04NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1SolarizeNode()
        tex = TextureResult(texture_id=21, width=5, height=3)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_solarize"
        assert out["_effect_input"] is tex
        assert out["image"].width == 5 and out["image"].height == 3
        for name in ("threshold", "softness", "channel_mode", "amount"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1SolarizeNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_param_defs_match_f17_defaults(self) -> None:
        node = Lib1SolarizeNode()
        assert {p.name for p in node.PARAM_DEFS} == set(solarize_ref.DEFAULTS)
        for name, default in solarize_ref.DEFAULTS.items():
            assert node.parameters[name] == default, name
        assert SOLARIZE_CHANNEL_MODES == ["Luma", "RGB"]
        assert node._tile_padding({}) == 0


class TestC04PassthroughToF17:
    def test_marker_params_drive_f17_core(self) -> None:
        """节点参数快照直接喂 F17 共享核 oracle 有效（同核语义，禁双份漂移）。"""
        node = Lib1SolarizeNode()
        node.parameters["amount"] = 0.5
        node.parameters["channel_mode"] = "RGB"
        out = node._do_evaluate(
            {"image": TextureResult(texture_id=1, width=2, height=1)},
            ctx=None, fb_manager=None)
        p = dict(out["_effect_params"])
        img = np.full((1, 2, 3), 0.18, dtype=np.float32)
        np.testing.assert_allclose(
            solarize_ref.evaluate(img, p),
            solarize_ref.evaluate(img, {"amount": 0.5, "channel_mode": "RGB"}),
            atol=1e-7)


class TestC04BorrowedHandVectors:
    """借 F17 语义的手算锚点（F17 全量见 test_f17，此处只钉透传语义）。"""

    def test_rgb_mode_fold_anchor(self) -> None:
        # 手算 1：RGB 档 thr=1：(2,1,0.5) → (0,1,0.5)（折返 2thr−v）
        img = np.array([[[2.0, 1.0, 0.5]]], dtype=np.float32)
        out = solarize_ref.evaluate(img, {"channel_mode": "RGB"})
        np.testing.assert_allclose(out[0, 0], [0.0, 1.0, 0.5], atol=1e-6)

    def test_amount_zero_identity(self) -> None:
        # 手算 2：amount=0 精确恒等（含 HDR）
        img = np.array([[[-0.25, 0.18, 12.0]]], dtype=np.float32)
        np.testing.assert_array_equal(
            solarize_ref.evaluate(img, {"amount": 0.0}), img)

    def test_threshold_fold_anchor(self) -> None:
        # 手算 3：Luma 档 thr=1 softness=0：v<thr 不动、v=2.6 折回 −0.6
        for v, expect in ((0.3, 0.3), (1.0, 1.0), (2.6, -0.6)):
            got = float(solarize_ref.fold(np.float64(v), 1.0, 1e-4))
            assert got == pytest.approx(expect, abs=1e-6), v


class TestC04Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C04"
        node = Lib1SolarizeNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name
        assert "solarize_ref" in manifest.verification.oracle  # F17 单一事实源


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
