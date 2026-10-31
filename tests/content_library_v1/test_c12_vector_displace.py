"""C12 lib1_vector_displace 测试：多输入 marker 协议 + oracle↔手算
（独立标量重推位移与采样索引）+ 恒等（vector=neutral / scale=0）+
mask 乘性门控 + 角色串线互斥 + 防御回退 + manifest 一致。"""

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

from content_staging.library_v1.references import vector_displace_ref as ref
from content_staging.library_v1.nodes.vector_displace.lib1_vector_displace_node import (
    EFFECT_KEY, Lib1VectorDisplaceNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "vector_displace" / "component_manifest.json")


def _hand_sample_channel(img: np.ndarray, y: int, x: int,
                         dy: float, dx: float, chan: int) -> float:
    """手算侧独立转录：采样位 (y+dy, x+dx) 的双线性 + clamp（整数位移
    时权重退化为 0/1，直接取整像素）。"""
    h, w = img.shape[0], img.shape[1]
    sy = min(max(y + dy, 0.0), float(h - 1))
    sx = min(max(x + dx, 0.0), float(w - 1))
    y0, x0 = math.floor(sy), math.floor(sx)
    y1, x1 = min(y0 + 1, h - 1), min(x0 + 1, w - 1)
    fy, fx = sy - y0, sx - x0
    return float(img[y0, x0, chan] * (1 - fx) * (1 - fy)
                 + img[y0, x1, chan] * fx * (1 - fy)
                 + img[y1, x0, chan] * (1 - fx) * fy
                 + img[y1, x1, chan] * fx * fy)


class TestC12NodeMarker:
    def test_multi_input_marker_protocol(self) -> None:
        node = Lib1VectorDisplaceNode()
        tex = TextureResult(texture_id=71, width=32, height=24)
        vec = TextureResult(texture_id=72, width=32, height=24)
        msk = TextureResult(texture_id=73, width=32, height=24)
        out = node._do_evaluate(
            {"image": tex, "vector": vec, "mask": msk},
            ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_vector_displace"
        assert out["_effect_input"] is tex          # 角色：被位移内容
        assert out["_effect_input_map"] is vec      # 角色：位移向量图
        assert out["_effect_mask"] is msk           # 角色：门控
        for name in ("scale", "neutral", "units"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_marker_without_mask_has_no_mask_key(self) -> None:
        node = Lib1VectorDisplaceNode()
        tex = TextureResult(texture_id=71, width=8, height=8)
        vec = TextureResult(texture_id=72, width=8, height=8)
        out = node._do_evaluate({"image": tex, "vector": vec},
                                ctx=None, fb_manager=None)
        assert "_effect_mask" not in out
        assert node._generic_mask_port is False, "语义 mask 端口不吃通用注入"

    def test_missing_inputs_return_empty(self) -> None:
        node = Lib1VectorDisplaceNode()
        tex = TextureResult(texture_id=71, width=8, height=8)
        vec = TextureResult(texture_id=72, width=8, height=8)
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        assert node._do_evaluate({"image": tex}, ctx=None, fb_manager=None) == {}
        assert node._do_evaluate({"vector": vec}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate(
            {"image": invalid, "vector": vec}, ctx=None, fb_manager=None) == {}

    def test_tile_padding(self) -> None:
        node = Lib1VectorDisplaceNode()
        assert node._tile_padding({"scale": 50.0}) == 51
        assert node._tile_padding({"scale": 0.0}) == 1
        assert node._tile_padding({"scale": 0.1, "units": "UV"}) == 193


class TestC12OracleHandVectors:
    def test_identity_when_vector_is_neutral(self) -> None:
        # 手算 1：vector 全图 = neutral → dx=dy=0 → 精确恒等
        #（任意 scale/units/mask，含 HDR）
        rng = np.random.RandomState(31)
        img = rng.rand(6, 8, 4).astype(np.float32)
        img[..., :3] *= np.float32(5.0)          # HDR
        vec = np.full((6, 8, 2), 0.5, dtype=np.float32)
        for p in ({"scale": 50.0}, {"scale": 123.0, "units": "UV"}):
            out = ref.evaluate(img, vec, p)
            np.testing.assert_array_equal(out, img)

    def test_pixels_unit_constant_offset(self) -> None:
        # 手算 2：vx = 0.75（neutral 0.5，float32 精确可表示）→
        # dx = 0.25·8 = +2px 整数位移 → out(x,y) = in(x+2, y)
        #（双线性权重 0/1；右缘钳制）；scale=8
        img = np.arange(40, dtype=np.float32).reshape(5, 8, 1)
        img = np.repeat(img, 3, axis=2)
        vec = np.full((5, 8, 2), 0.5, dtype=np.float32)
        vec[..., 0] = 0.75
        out = ref.evaluate(img, vec, {"scale": 8.0})
        for y in range(5):
            for x in range(8):
                assert out[y, x, 0] == pytest.approx(
                    _hand_sample_channel(img, y, x, 0.0, 2.0, 0), abs=1e-7), (x, y)

    def test_uv_unit_frame_fraction(self) -> None:
        # 手算 3：UV 单位，vy = 0.75 → dy = 0.25·0.25·8 = 0.5px（半像素，
        # float32 精确）→ 采样位 y+0.5 → 双线性行内插；8×8、scale=0.25
        img = np.arange(192, dtype=np.float32).reshape(8, 8, 3)
        vec = np.full((8, 8, 2), 0.5, dtype=np.float32)
        vec[..., 1] = 0.75
        out = ref.evaluate(img, vec, {"scale": 0.25, "units": "UV"})
        for y in range(8):
            for x in range(8):
                for c in range(3):
                    assert out[y, x, c] == pytest.approx(
                        _hand_sample_channel(img, y, x, 0.5, 0.0, c), abs=1e-6), (x, y, c)

    def test_mask_scales_offset(self) -> None:
        # 手算 4：vx = 0.75、scale=16 → dx = 4px；mask=0.5（精确）折半成
        # 2px；mask=0 区精确原位
        img = np.arange(40, dtype=np.float32).reshape(5, 8, 1)
        img = np.repeat(img, 3, axis=2)
        vec = np.full((5, 8, 2), 0.5, dtype=np.float32)
        vec[..., 0] = 0.75                      # dx = 0.25·16 = 4px
        mask = np.zeros((5, 8), dtype=np.float32)
        mask[:, :4] = 0.5
        out = ref.evaluate(img, vec, {"scale": 16.0}, mask=mask)
        for y in range(5):
            for x in range(8):
                want_dx = 2.0 if x < 4 else 0.0
                assert out[y, x, 0] == pytest.approx(
                    _hand_sample_channel(img, y, x, 0.0, want_dx, 0), abs=1e-7), (x, y)


class TestC12Semantics:
    def test_roles_not_swappable(self) -> None:
        # 输入角色不可串线（验收点）：oracle 双源签名按位置绑定
        #（参数 1 = 被位移内容、参数 2 = 位移向量场）——交换实参
        # （把向量图当内容、内容 RG 当位移场）必须得到不同输出。
        rng = np.random.RandomState(37)
        img = rng.rand(8, 8, 3).astype(np.float32)
        vec = np.full((8, 8, 2), 0.5, dtype=np.float32)
        vec[..., 0] = 0.8
        displaced = ref.evaluate(img, vec, {"scale": 20.0})
        content_swapped = np.repeat(vec[..., :1], 3, axis=2)  # 向量图当内容
        disp_swapped = img[..., :2]                           # 内容当位移场
        swapped = ref.evaluate(content_swapped, disp_swapped, {"scale": 20.0})
        assert not np.allclose(displaced, swapped)

    def test_alpha_displaces_with_rgb(self) -> None:
        img = np.zeros((5, 8, 4), dtype=np.float32)
        img[..., 3] = np.arange(40, dtype=np.float32).reshape(5, 8) / 40.0
        vec = np.full((5, 8, 2), 0.5, dtype=np.float32)
        vec[..., 0] = 0.75                      # dx = +2px（采样位 x+2）
        out = ref.evaluate(img, vec, {"scale": 8.0})
        assert out[2, 0, 3] == pytest.approx(float(img[2, 2, 3]), abs=1e-7)
        assert out[2, 4, 3] == pytest.approx(float(img[2, 6, 3]), abs=1e-7)

    def test_defensive_missing_channels(self) -> None:
        # vector 只有 1 通道 → G 按 neutral（零位移）→ 仅 x 位移
        img = np.arange(40, dtype=np.float32).reshape(5, 8, 1).repeat(3, axis=2)
        vec = np.full((5, 8, 1), 0.75, dtype=np.float32)
        out = ref.evaluate(img, vec, {"scale": 8.0})
        for x in range(8):
            assert out[2, x, 0] == pytest.approx(
                _hand_sample_channel(img, 2, x, 0.0, 2.0, 0), abs=1e-7), x

    def test_resolution_mismatch_clamped_read(self) -> None:
        # vector/mask 分辨率小于 image → 同索引钳制读取（不崩溃、行为可推）
        img = np.arange(40, dtype=np.float32).reshape(5, 8, 1).repeat(3, axis=2)
        vec = np.full((3, 4, 2), 0.5, dtype=np.float32)
        vec[..., 0] = 0.75
        out = ref.evaluate(img, vec, {"scale": 8.0})
        for y in range(5):
            for x in range(8):
                want_dx = 2.0
                assert out[y, x, 0] == pytest.approx(
                    _hand_sample_channel(img, y, x, 0.0, want_dx, 0), abs=1e-7), (x, y)


class TestC12Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C12"
        node = Lib1VectorDisplaceNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name
        port_names_in = [p.name for p in manifest.ports if p.direction == "input"]
        assert port_names_in == ["image", "vector", "mask"]
        assert [p for p in manifest.ports
                if p.name == "mask" and p.direction == "input"][0].required is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
