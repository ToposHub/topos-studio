"""C13 lib1_stmap 测试：双输入 marker 协议 + oracle↔手算（独立标量
重推 UV→采样索引）+ 恒等锚（归一化位置 map）+ origin/invalid/防御 +
manifest 一致。"""

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

from content_staging.library_v1.references import stmap_ref as ref
from content_staging.library_v1.nodes.stmap.lib1_stmap_node import (
    EFFECT_KEY, Lib1StmapNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "stmap" / "component_manifest.json")


def _identity_map(h: int, w: int, origin: str) -> np.ndarray:
    """恒等 UV map：BottomLeft → v 需翻转；TopLeft → v 直用。"""
    uu, vv = np.meshgrid(np.linspace(0.0, 1.0, w, dtype=np.float32),
                         np.linspace(0.0, 1.0, h, dtype=np.float32))
    if origin == "BottomLeft":
        vv = 1.0 - vv
    return np.stack([uu, vv], axis=-1)


class TestC13NodeMarker:
    def test_multi_input_marker_protocol(self) -> None:
        node = Lib1StmapNode()
        tex = TextureResult(texture_id=81, width=32, height=24)
        mp = TextureResult(texture_id=82, width=32, height=24)
        out = node._do_evaluate({"image": tex, "map": mp},
                                ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_stmap"
        assert out["_effect_input"] is tex          # 角色：被重映射内容
        assert out["_effect_input_map"] is mp       # 角色：绝对 UV 图
        for name in ("channels", "origin", "invalid", "filter"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_inputs_return_empty(self) -> None:
        node = Lib1StmapNode()
        tex = TextureResult(texture_id=81, width=8, height=8)
        mp = TextureResult(texture_id=82, width=8, height=8)
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        assert node._do_evaluate({"image": tex}, ctx=None, fb_manager=None) == {}
        assert node._do_evaluate({"map": mp}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate(
            {"image": invalid, "map": mp}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_full_diagonal_basis(self) -> None:
        node = Lib1StmapNode()
        assert node._tile_padding({}) == 2204   # ceil(hypot(1920,1080))+1


class TestC13OracleHandVectors:
    def test_identity_anchor_normalized_position_map(self) -> None:
        # 手算 1：map = 每像素自身归一化位置 → 精确恒等
        #（Nearest/Linear × BottomLeft/TopLeft 全组合，任意 HDR 内容）
        rng = np.random.RandomState(41)
        img = rng.rand(5, 5, 4).astype(np.float32) * np.float32(3.0)
        for filt in ("Nearest", "Linear"):
            for origin in ("BottomLeft", "TopLeft"):
                out = ref.evaluate(img, _identity_map(5, 5, origin),
                                   {"filter": filt, "origin": origin})
                np.testing.assert_array_equal(out, img)

    def test_constant_uv_center_hand(self) -> None:
        # 手算 2：UV 恒 (0.5, 0.5)，5×5、BottomLeft →
        # px = 0.5·4 = 2、py = (1−0.5)·4 = 2 → 全图 = in[2,2]
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        mp = np.full((5, 5, 2), 0.5, dtype=np.float32)
        out = ref.evaluate(img, mp, {"filter": "Nearest"})
        np.testing.assert_array_equal(out, np.broadcast_to(img[2, 2], (5, 5, 3)))

    def test_origin_flip_hand(self) -> None:
        # 手算 3：v 恒 0.25 → BottomLeft: py = 0.75·4 = 3；TopLeft: py = 1
        #（u 恒 0.5 → 列 2）
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        mp = np.zeros((5, 5, 2), dtype=np.float32)
        mp[..., 0] = 0.5
        mp[..., 1] = 0.25
        out_bl = ref.evaluate(img, mp, {"origin": "BottomLeft", "filter": "Nearest"})
        out_tl = ref.evaluate(img, mp, {"origin": "TopLeft", "filter": "Nearest"})
        np.testing.assert_array_equal(out_bl, np.broadcast_to(img[3, 2], (5, 5, 3)))
        np.testing.assert_array_equal(out_tl, np.broadcast_to(img[1, 2], (5, 5, 3)))

    def test_invalid_clamp_vs_passthrough(self) -> None:
        # 手算 4：u = −0.5（域外）、v 逐行取恒等值 → Clamp: px = −2 →
        # 钳列 0（out[y,x] = in[y,0]）；Passthrough: 原位透传（逐位输入）
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        mp = np.zeros((5, 5, 2), dtype=np.float32)
        mp[..., 0] = -0.5
        for y in range(5):
            mp[y, :, 1] = 1.0 - y / 4.0     # BottomLeft 恒等 v
        out_cl = ref.evaluate(img, mp, {"invalid": "Clamp", "filter": "Nearest"})
        out_pa = ref.evaluate(img, mp, {"invalid": "Passthrough", "filter": "Nearest"})
        col0 = img[:, 0][:, None, :]                     # (5,1,3)：钳列 0
        np.testing.assert_array_equal(out_cl, np.repeat(col0, 5, axis=1))
        np.testing.assert_array_equal(out_pa, img)

    def test_ba_channel_mapping(self) -> None:
        # 手算 5：BA → u=ch2/v=ch3；与把同值放 RG 的结果逐位一致
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        rg = np.zeros((5, 5, 2), dtype=np.float32)
        rg[..., 0] = 0.25
        rg[..., 1] = 0.75
        ba = np.zeros((5, 5, 4), dtype=np.float32)
        ba[..., 2] = 0.25
        ba[..., 3] = 0.75
        out_rg = ref.evaluate(img, rg, {"channels": "RG", "filter": "Nearest"})
        out_ba = ref.evaluate(img, ba, {"channels": "BA", "filter": "Nearest"})
        np.testing.assert_array_equal(out_rg, out_ba)
        # 手算重推：u=0.25 → 列 1；v=0.75、BottomLeft → py = 0.25·4 = 1
        np.testing.assert_array_equal(
            out_ba, np.broadcast_to(img[1, 1], (5, 5, 3)))

    def test_defensive_missing_channels(self) -> None:
        # BA 模式但 map 只有 2 通道 → u=v=0 → BottomLeft 原点角 (py=4, col0)
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        mp = np.zeros((5, 5, 2), dtype=np.float32)
        out = ref.evaluate(img, mp, {"channels": "BA", "filter": "Nearest"})
        np.testing.assert_array_equal(out, np.broadcast_to(img[4, 0], (5, 5, 3)))


class TestC13Semantics:
    def test_absolute_vs_relative_distinction(self) -> None:
        # 绝对坐标口径（验收点）：常量 UV ≠ 常量位移——常量 UV 把全图
        # 塌缩到一个采样点（绝对位置），而相对位移保持结构（C12）。
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        mp = np.zeros((5, 5, 2), dtype=np.float32)
        mp[..., 0] = 0.6
        mp[..., 1] = 0.5
        out = ref.evaluate(img, mp, {"filter": "Nearest"})
        assert np.all(out == out[0, 0]), "常量 UV 必须塌缩到单一采样点"

    def test_alpha_resampled(self) -> None:
        img = np.zeros((5, 5, 4), dtype=np.float32)
        img[..., 3] = np.arange(25, dtype=np.float32).reshape(5, 5) / 25.0
        mp = np.full((5, 5, 2), 0.5, dtype=np.float32)
        out = ref.evaluate(img, mp, {"filter": "Nearest"})
        np.testing.assert_array_equal(out[..., 3], np.float32(img[2, 2, 3]))

    def test_filter_changes_subpixel_result(self) -> None:
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3).astype(np.float32)
        mp = _identity_map(5, 5, "BottomLeft")
        mp[..., 0] = mp[..., 0] * 0.98 + 0.01    # 亚像素扰动
        near = ref.evaluate(img, mp, {"filter": "Nearest"})
        lin = ref.evaluate(img, mp, {"filter": "Linear"})
        assert not np.allclose(near, lin)

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(43)
        img = rng.rand(8, 8, 3).astype(np.float32)
        mp = _identity_map(8, 8, "BottomLeft")
        base = ref.evaluate(img, mp, {})
        shifted = mp.copy()
        shifted[..., 0] = np.clip(shifted[..., 0] + 0.1, 0.0, 1.0)
        assert not np.allclose(ref.evaluate(img, shifted, {}), base)
        assert not np.allclose(ref.evaluate(img, mp, {"origin": "TopLeft"}), base)
        assert not np.allclose(
            ref.evaluate(img, mp, {"channels": "BA"}), base)   # BA 缺失 → 原点角
        assert not np.allclose(ref.evaluate(img, mp * 1.2, {}), base)  # 域外 Clamp


class TestC13Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C13"
        node = Lib1StmapNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name
        port_names_in = [p.name for p in manifest.ports if p.direction == "input"]
        assert port_names_in == ["image", "map"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
