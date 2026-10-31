"""C05 lib1_mosaic 测试：marker 协议 + oracle↔手算（≥3 独立向量）+
近似恒等（levels=32 界）+ 块一致性/Bayer 相位/HDR 外推 + manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import mosaic_ref as ref
from content_staging.library_v1.nodes.mosaic.lib1_mosaic_node import (
    EFFECT_KEY, Lib1MosaicNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "mosaic" / "component_manifest.json")


class TestC05NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1MosaicNode()
        tex = TextureResult(texture_id=31, width=16, height=8)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_mosaic"
        assert out["_effect_input"] is tex
        assert out["image"].width == 16 and out["image"].height == 8
        assert out["_effect_params"] == {"block_px": 8.0, "levels": 8.0,
                                         "dither": False}

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1MosaicNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_grows_with_block(self) -> None:
        node = Lib1MosaicNode()
        assert node._tile_padding({"block_px": 1.0}) == 1  # ceil(1/2)=1（上界守护）
        assert node._tile_padding({"block_px": 8.0}) == 4
        assert node._tile_padding({"block_px": 9.0}) == 5


class TestC05OracleHandVectors:
    def test_quantization_anchor(self) -> None:
        # 手算 1：block=1、levels=2：c=0.3 → (floor(0.6)+0.5)/2 = 0.25
        img = np.full((1, 1, 3), 0.3, dtype=np.float32)
        out = ref.evaluate(img, {"block_px": 1.0, "levels": 2.0})
        np.testing.assert_allclose(out[0, 0], 0.25, atol=1e-7)

    def test_block_center_sampling(self) -> None:
        # 手算 2：8×1 图 block=8 → 采样 texel floor(0+4)=4，全块取 img[4]
        img = np.arange(8, dtype=np.float32).reshape(1, 8, 1) / 8.0
        img = np.repeat(img, 3, axis=2)
        out = ref.evaluate(img, {"block_px": 8.0, "levels": 32.0})
        expected = (np.floor(img[0, 4] * 32.0) + 0.5) / 32.0
        for x in range(8):
            np.testing.assert_allclose(out[0, x], expected, atol=1e-7, err_msg=x)

    def test_bayer_phase_shifts_quantization(self) -> None:
        # 手算 3（按生产 .tglsl 约定 inner=px−bid·block, ix=int(min(inner,3))）：
        # block=2、levels=4、c=0.5：x=0 → inner.x=0.5 → ix=0（bayer[0][0]=0 →
        # d=−0.46875）→ floor(2−0.46875)=1 → 0.375；x=1 → inner.x=1.5 → ix=1
        # （bayer[0][1]=8 → d=0.03125）→ floor(2.03125)=2 → 0.625。
        # 初版用 block=1：inner 恒 0.5 → ix 恒 0（相位无从偏移）——与生产
        # 约定不符的手算错误，改 block=2 后相位逐像素交替
        img = np.full((1, 2, 3), 0.5, dtype=np.float32)
        out = ref.evaluate(img, {"block_px": 2.0, "levels": 4.0, "dither": True})
        np.testing.assert_allclose(out[0, 0], 0.375, atol=1e-7)
        np.testing.assert_allclose(out[0, 1], 0.625, atol=1e-7)

    def test_hdr_monotone_extrapolation(self) -> None:
        # 手算 4：c=12.0、levels=8 → (floor(96)+0.5)/8 = 12.0625（无界外推）
        img = np.full((1, 1, 3), 12.0, dtype=np.float32)
        out = ref.evaluate(img, {"block_px": 1.0, "levels": 8.0})
        np.testing.assert_allclose(out[0, 0], 12.0625, atol=1e-6)


class TestC05IdentityAndSemantics:
    def test_approx_identity_at_levels_max(self) -> None:
        """近似恒等（显式声明）：block=1+dither off+levels=32 → |out−in|≤1/64；
        levels→∞ 才精确恒等（参数域上限 32）。"""
        rng = np.random.RandomState(3)
        img = rng.rand(4, 6, 3).astype(np.float32)
        out = ref.evaluate(img, {"block_px": 1.0, "levels": 32.0, "dither": False})
        assert float(np.abs(out - img).max()) <= 1.0 / 64.0 + 1e-6
        # 且并非精确恒等（量化确实存在）
        assert not np.array_equal(out, img)

    def test_block_constancy(self) -> None:
        rng = np.random.RandomState(5)
        img = rng.rand(8, 8, 3).astype(np.float32)
        out = ref.evaluate(img, {"block_px": 4.0, "levels": 32.0})
        for by in range(2):
            for bx in range(2):
                block = out[by * 4:(by + 1) * 4, bx * 4:(bx + 1) * 4]
                np.testing.assert_allclose(
                    block, np.broadcast_to(block[0, 0], block.shape),
                    atol=0.0, err_msg=(by, bx))

    def test_quantization_monotone(self) -> None:
        img = np.linspace(0.0, 1.0, 32, dtype=np.float32).reshape(1, 32, 1)
        img = np.repeat(img, 3, axis=2)
        out = ref.evaluate(img, {"block_px": 1.0, "levels": 8.0})
        diffs = np.diff(out.astype(np.float64)[0, :, 0])
        assert (diffs >= -1e-7).all(), "量化必须单调不减"

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(9)
        img = rng.rand(8, 8, 3).astype(np.float32)
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"block_px": 3.0}), base)
        assert not np.allclose(ref.evaluate(img, {"levels": 2.0}), base)
        assert not np.allclose(ref.evaluate(img, {"dither": True}), base)

    def test_alpha_passthrough(self) -> None:
        img = np.zeros((1, 1, 4), dtype=np.float32)
        img[0, 0] = [0.4, 0.4, 0.4, 0.6]
        out = ref.evaluate(img, {"levels": 2.0})
        assert out[0, 0, 3] == np.float32(0.6)


class TestC05Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C05"
        node = Lib1MosaicNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        # 近似恒等必须在 known_limits 显式声明
        assert "近似恒等" in manifest.verification.known_limits


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
