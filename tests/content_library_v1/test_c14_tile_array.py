"""C14 lib1_tile_array 测试：marker 协议 + oracle↔手算（独立标量重推
格胞/内容盒/环绕采样）+ 恒等（1×1+0+0）+ 空隙 alpha=0 + offset 环绕 +
count 钳制 + manifest 一致。"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import tile_array_ref as ref
from content_staging.library_v1.nodes.tile_array.lib1_tile_array_node import (
    EFFECT_KEY, Lib1TileArrayNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "tile_array" / "component_manifest.json")


def _hand_nearest(pos: float, n: int) -> int:
    """手算侧独立转录：texel-block 最近邻 floor(pos) 后钳 [0, n−1]
    （纹素 i 覆盖 [i/n, (i+1)/n)；1×1+0 恒等在此口径精确）。"""
    return int(min(max(math.floor(pos), 0), n - 1))


def _hand_frac(v: float) -> float:
    """手算侧独立转录：frac 正模（环绕）。"""
    return v - math.floor(v)


class TestC14NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1TileArrayNode()
        tex = TextureResult(texture_id=91, width=32, height=24)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_tile_array"
        assert out["_effect_input"] is tex
        for name in ("count_x", "count_y", "spacing", "offset_x", "offset_y"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1TileArrayNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_zero_wraps_in_frame(self) -> None:
        node = Lib1TileArrayNode()
        assert node._tile_padding({"offset_x": 1.0, "offset_y": -1.0}) == 0

    def test_output_bbox_passthrough(self) -> None:
        node = Lib1TileArrayNode()
        bbox = (0, 0, 100, 50)
        assert node.compute_output_bbox({"image": bbox}, {}) is bbox


class TestC14OracleHandVectors:
    def test_identity_single_tile(self) -> None:
        # 手算 1：count 1×1 + spacing=0 + offset=0 → 精确恒等（含 HDR/α）
        rng = np.random.RandomState(51)
        img = rng.rand(4, 4, 4).astype(np.float32) * np.float32(6.0)
        out = ref.evaluate(img, {"count_x": 1, "count_y": 1,
                                 "spacing": 0.0, "offset_x": 0.0,
                                 "offset_y": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_array_2x1_hand_recompute(self) -> None:
        # 手算 2：4×1 图、count 2×1、spacing=0 → tw=2；
        # x=0: u_c=0→xi=0；x=1: u_c=0.5→u_s=0.5→xi=round(0.5·3)=2；
        # x=2: i=1, u_c=0→xi=0；x=3: u_c=0.5→xi=2 → [in0, in2, in0, in2]
        img = np.arange(4, dtype=np.float32).reshape(1, 4, 1).repeat(3, axis=2)
        out = ref.evaluate(img, {"count_x": 2, "count_y": 1, "spacing": 0.0})
        for x, want in ((0, 0), (1, 2), (2, 0), (3, 2)):
            assert out[0, x, 0] == img[0, want, 0], x

    def test_spacing_gap_alpha_zero(self) -> None:
        # 手算 3：4×4 图、count 2×1、spacing=0.5（x/y 双向生效）→
        # tw=2/cw=1/margin_x=0.5、th=4/ch=2/margin_y=1：
        # 行 y=1: v_c=(1−1)/2=0 → vi=floor(0)=0（源行 0）；
        # 列 x=1: u_c=(1−0.5)/1=0.5 → xi=floor(0.5·4)=2 → img[0,2]；
        # 列 x=0: u_c=−0.5 空隙、x=2: (2−2−0.5)<0 空隙、x=3: 0.5→xi=2
        # → 行 1 = [空隙, img[0,2], 空隙, img[0,2]]
        img = np.arange(64, dtype=np.float32).reshape(4, 4, 4)
        out = ref.evaluate(img, {"count_x": 2, "count_y": 1, "spacing": 0.5})
        assert out[1, 0, 0] == 0.0 and out[1, 0, 3] == 0.0    # 空隙 RGB/α=0
        assert out[1, 2, 0] == 0.0 and out[1, 2, 3] == 0.0
        assert out[1, 1, 0] == img[0, 2, 0]
        assert out[1, 1, 3] == img[0, 2, 3]                   # 瓦片内 α=源 α
        assert out[1, 3, 0] == img[0, 2, 0]
        # 行 y=0（v_c=−0.5）与行 y=3（v_c=1.0）整行空隙（y 向同样生效）
        np.testing.assert_array_equal(out[0], np.zeros((4, 4), np.float32))
        np.testing.assert_array_equal(out[3], np.zeros((4, 4), np.float32))

    def test_offset_wrap_phase(self) -> None:
        # 手算 4：count 1×1、offset_x=0.5 → u_s = frac(u_c+0.5)，
        # xi = floor(u_s·4)：x=0→0.5·4=2；x=1→3；x=2→frac(1)=0；x=3→1
        img = np.arange(4, dtype=np.float32).reshape(1, 4, 1).repeat(3, axis=2)
        out = ref.evaluate(img, {"count_x": 1, "count_y": 1, "spacing": 0.0,
                                 "offset_x": 0.5})
        for x in range(4):
            u_s = _hand_frac(x / 4.0 + 0.5)
            assert out[0, x, 0] == img[0, _hand_nearest(u_s * 4, 4), 0], x
        # 负 offset：−0.25 正模环绕
        out_n = ref.evaluate(img, {"count_x": 1, "count_y": 1, "spacing": 0.0,
                                   "offset_x": -0.25})
        for x in range(4):
            u_s = _hand_frac(x / 4.0 - 0.25)
            assert out_n[0, x, 0] == img[0, _hand_nearest(u_s * 4, 4), 0], x

    def test_array_2x2_hand_recompute(self) -> None:
        # 手算 5：4×4 图、count 2×2、spacing=0（tw=th=2、margin 0）→
        # (0,0): u_c=v_c=0→(0,0)；(1,1): 0.5→floor(0.5·4)=2 → in[2,2]；
        # (2,2): i=j=1、u_c=v_c=0→(0,0)；(3,3): 0.5→2 → in[2,2]；
        # (0,3): u_c=0.5→xi=2、v_c=0→vi=0 → in[0,2]；(3,0): in[2,0]
        img = np.arange(48, dtype=np.float32).reshape(4, 4, 3)
        out = ref.evaluate(img, {"count_x": 2, "count_y": 2, "spacing": 0.0})
        assert out[0, 0, 1] == img[0, 0, 1]
        assert out[1, 1, 1] == img[2, 2, 1]
        assert out[2, 2, 1] == img[0, 0, 1]
        assert out[3, 3, 1] == img[2, 2, 1]
        assert out[0, 3, 1] == img[0, 2, 1]
        assert out[3, 0, 1] == img[2, 0, 1]


class TestC14Semantics:
    def test_gap_is_transparent_not_black_repeat(self) -> None:
        # 验收点：空隙 α=0（透明）而非黑 RGB repeat——4 通道含非零 α；
        # 2×8 图、count 2×1、spacing=0.5 → 内容列 {1,2,5,6}、空隙 {0,3,4,7}
        img = np.ones((2, 8, 4), dtype=np.float32)
        img[..., 3] = 0.7
        out = ref.evaluate(img, {"count_x": 2, "count_y": 1, "spacing": 0.5})
        for x in (0, 3, 4, 7):
            assert out[1, x, 3] == 0.0, x     # 空隙 α=0
            assert out[1, x, 0] == 0.0, x     # RGB 同为 0（straight）
        for x in (1, 2, 5, 6):
            assert out[1, x, 3] == np.float32(0.7), x   # 瓦片内 α 透传

    def test_hdr_passthrough_inside_tiles(self) -> None:
        img = np.full((1, 4, 3), 0.0, dtype=np.float32)
        img[0, 2, 0] = 12.0
        img[0, 2, 1] = -0.5
        out = ref.evaluate(img, {"count_x": 2, "count_y": 1, "spacing": 0.0})
        assert out[0, 1, 0] == np.float32(12.0)     # xi=2（手算 2）
        assert out[0, 1, 1] == np.float32(-0.5)

    def test_count_clamped_defensive(self) -> None:
        img = np.arange(48, dtype=np.float32).reshape(4, 4, 3)
        for p in ({"count_x": 0, "count_y": 0}, {"count_x": 99, "count_y": -3}):
            out = ref.evaluate(img, dict(p, spacing=0.0))   # 不崩溃
            assert out.shape == img.shape

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(53)
        img = rng.rand(16, 16, 3).astype(np.float32)
        base = ref.evaluate(img, {})
        assert not np.allclose(ref.evaluate(img, {"count_x": 4}), base)
        assert not np.allclose(ref.evaluate(img, {"count_y": 4}), base)
        assert not np.allclose(ref.evaluate(img, {"spacing": 0.4}), base)
        assert not np.allclose(ref.evaluate(img, {"offset_x": 0.3}), base)
        assert not np.allclose(ref.evaluate(img, {"offset_y": 0.3}), base)


class TestC14Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C14"
        node = Lib1TileArrayNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
