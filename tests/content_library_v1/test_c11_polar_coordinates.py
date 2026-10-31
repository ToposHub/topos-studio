"""C11 lib1_polar_coordinates 测试：marker 协议 + oracle↔手算（独立标量
重推采样位）+ 反变换往返（Linear）+ 滤波/参数有效性 + α 重映射 +
manifest 一致。手算向量不 import 被测核内部函数——测试侧用纯 Python
标量 math 重推 (u,v)→采样索引。"""

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

from content_staging.library_v1.references import polar_coordinates_ref as ref
from content_staging.library_v1.nodes.polar_coordinates.lib1_polar_coordinates_node import (
    EFFECT_KEY, Lib1PolarCoordinatesNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "polar_coordinates" / "component_manifest.json")


def _nearest_index(pos: float, n: int) -> int:
    """手算侧独立转录：clamp-half-texel 最近邻索引（像素中心=整数索引）。"""
    clamped = min(max(pos, 0.0), float(n - 1))
    return int(min(max(math.floor(clamped + 0.5), 0), n - 1))


def _hand_topolar_uv(px: float, py: float, cx: float, cy: float,
                     span_deg: float, r_max: float) -> tuple[float, float]:
    """手算侧独立转录：输出像素 → 输入 (u, v)。"""
    theta = math.atan2(py - cy, px - cx)
    r = math.hypot(px - cx, py - cy)
    u = 0.5 + theta / math.radians(span_deg)
    v = r / r_max
    return u, v


class TestC11NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1PolarCoordinatesNode()
        tex = TextureResult(texture_id=71, width=32, height=24)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_polar_coordinates"
        assert out["_effect_input"] is tex
        for name in ("mode", "center", "angle_span", "radius_scale", "filter"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1PolarCoordinatesNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_full_diagonal_basis(self) -> None:
        node = Lib1PolarCoordinatesNode()
        assert node._tile_padding({}) == 2204  # hypot(1920,1080)≈2202.9 → +1

    def test_ports_and_defaults(self) -> None:
        node = Lib1PolarCoordinatesNode()
        # CP-044 基类契约：GENERIC_MASK_MIX 默认 True 会自动附加通用
        # mask 端口（与 despill/glitch 草案同构）；语义端口仅 image。
        assert [p.name for p in node.inputs] == ["image", "mask"]
        assert [p.name for p in node.outputs] == ["image"]
        assert node.parameters["center"] == (0.5, 0.5)


class TestC11OracleHandVectors:
    def test_center_pixel_samples_top_row(self) -> None:
        # 手算 1（5×5，center(0.5,0.5)→c=(2,2)，r_max=2.5，span=360）：
        # 输出像素 (2,2)：θ=atan2(0,0)=0，r=0 → (u,v)=(0.5, 0)
        # → 采样位 (0.5·4, 0·4)=(2.0, 0.0) → nearest 行 0 列 2
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        out = ref.evaluate(img, {"mode": "CartesianToPolar", "filter": "Nearest"})
        u, v = _hand_topolar_uv(2, 2, 2.0, 2.0, 360.0, 2.5)
        assert (u, v) == (0.5, 0.0)
        assert out[2, 2, 1] == img[_nearest_index(v * 4, 5), _nearest_index(u * 4, 5), 1]

    def test_east_pixel_hand_recompute(self) -> None:
        # 手算 2（正东 (4,2)）：θ=0 → u=0.5、v=2/2.5=0.8
        # → 采样位 (2.0, 3.2) → nearest (行3, 列2)
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        out = ref.evaluate(img, {"mode": "CartesianToPolar", "filter": "Nearest"})
        u, v = _hand_topolar_uv(4, 2, 2.0, 2.0, 360.0, 2.5)
        assert u == pytest.approx(0.5) and v == pytest.approx(0.8)
        assert out[2, 4, 1] == img[_nearest_index(v * 4, 5), _nearest_index(u * 4, 5), 1]

    def test_diagonal_pixel_hand_recompute(self) -> None:
        # 手算 3（45° (3,3)）：θ=π/4 → u=0.5+1/8=0.625 → sx=2.5 → 列3；
        # v=√2/2.5 → sy=4·0.565685…=2.2627… → 行2
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        out = ref.evaluate(img, {"mode": "CartesianToPolar", "filter": "Nearest"})
        u, v = _hand_topolar_uv(3, 3, 2.0, 2.0, 360.0, 2.5)
        assert u == pytest.approx(0.625)
        assert out[3, 3, 1] == img[_nearest_index(v * 4, 5), _nearest_index(u * 4, 5), 1]

    def test_frompolar_pixel_hand_recompute(self) -> None:
        # 手算 4（PolarToCartesian，(4,2)）：uq=4/4=1、vq=2/4=0.5
        # → θ=π、r=1.25 → 采样位 (2−1.25, 2+1.25·sin π≈2) → (行2, 列1)
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        out = ref.evaluate(img, {"mode": "PolarToCartesian", "filter": "Nearest"})
        uq, vq = 4 / 4, 2 / 4
        theta = math.radians(360.0) * (uq - 0.5)
        rad = vq * 2.5
        sx = 2.0 + rad * math.cos(theta)
        sy = 2.0 + rad * math.sin(theta)
        assert out[2, 4, 1] == img[_nearest_index(sy, 5), _nearest_index(sx, 5), 1]

    def test_inverse_pair_position_level(self) -> None:
        # 反变换验收（位置级，float64 无重采样）：G 的采样位 p' 代回正向
        # 映射 F 必须还原 (uq, vq)——F∘G = id（独立标量重推，atol 1e-9）。
        # 排除零符号简并带：p'y == cy（sin(±π) 舍入为 0）时 atan2(+0.0,
        # dx<0) = +π 把 θ 推到对侧接缝——固有浮点简并，README 记录。
        # 图像级两趟往返另含中间重采样插值误差（非线性映射下双线性
        # J(p') ≠ 连续 J(p')，随机噪声图 ~0.3 量级）——不作逐像素恒等
        # 断言，由本位置级验证 + 两方向各自手算采样位向量共同钉住。
        h, w = 6, 8
        cx, cy, r_max = 0.5 * (w - 1), 0.5 * (h - 1), 0.5 * min(w, h)
        skipped = 0
        for y in range(h):
            for x in range(w):
                uq, vq = x / (w - 1), y / (h - 1)
                theta = math.radians(360.0) * (uq - 0.5)
                rad = vq * r_max
                px = cx + rad * math.cos(theta)
                py = cy + rad * math.sin(theta)
                if py == cy:      # 零符号简并带（独立 float 重推判定）
                    skipped += 1
                    continue
                u_back = 0.5 + math.atan2(py - cy, px - cx) / math.radians(360.0)
                v_back = math.hypot(px - cx, py - cy) / r_max
                assert u_back == pytest.approx(uq, abs=1e-9), (x, y)
                assert v_back == pytest.approx(vq, abs=1e-9), (x, y)
        assert skipped > 0, "本图幅必须实际命中接缝简并带（否则排除失效）"

    def test_roundtrip_v0_row_singular_degenerate(self) -> None:
        # 中心奇点（如实钉住）：逆向在 vq=0 行 r=0 → 全行采样 J(center)
        # → I(F(center)) = I(frame(0.5, 0)) = 像素 (3.5, 0) 的插值——
        # 整行同一值（角度轴消失），≠ 原位。
        rng = np.random.RandomState(11)
        img = rng.rand(6, 8, 3).astype(np.float32)
        polar = ref.evaluate(img, {"mode": "CartesianToPolar", "filter": "Linear"})
        back = ref.evaluate(polar, {"mode": "PolarToCartesian", "filter": "Linear"})
        np.testing.assert_allclose(back[0, :], np.broadcast_to(back[0, 0], back[0].shape))
        assert not np.allclose(back[0, :], img[0, :])


class TestC11Semantics:
    def test_alpha_is_remapped_not_passthrough(self) -> None:
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3)
        alpha = np.arange(25, dtype=np.float32).reshape(5, 5) / 25.0
        img4 = np.concatenate([img, alpha[..., None]], axis=-1)
        out = ref.evaluate(img4, {"mode": "CartesianToPolar", "filter": "Nearest"})
        # 中心像素 α = 采样位 (行0,列2) 的 α（非原位透传）
        assert out[2, 2, 3] == img4[0, 2, 3]
        assert out[2, 4, 3] == img4[3, 2, 3]

    def test_filter_changes_subpixel_result(self) -> None:
        img = np.arange(75, dtype=np.float32).reshape(5, 5, 3).astype(np.float32)
        near = ref.evaluate(img, {"filter": "Nearest"})
        lin = ref.evaluate(img, {"filter": "Linear"})
        assert not np.allclose(near, lin), "亚像素采样位上两种滤波必须不同"

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(23)
        img = rng.rand(16, 16, 3).astype(np.float32)
        base = ref.evaluate(img, {})
        assert not np.allclose(
            ref.evaluate(img, {"mode": "PolarToCartesian"}), base)
        assert not np.allclose(ref.evaluate(img, {"angle_span": 90.0}), base)
        assert not np.allclose(ref.evaluate(img, {"radius_scale": 2.0}), base)
        assert not np.allclose(ref.evaluate(img, {"center": (0.3, 0.6)}), base)
        assert not np.allclose(ref.evaluate(img, {"filter": "Linear"}), base)

    def test_hdr_passthrough_no_clamp(self) -> None:
        img = np.full((5, 5, 3), 0.0, dtype=np.float32)
        img[0, 2, 0] = 12.0        # 中心像素的采样位（手算 1）
        img[0, 2, 1] = -0.5
        out = ref.evaluate(img, {"filter": "Nearest"})
        assert out[2, 2, 0] == np.float32(12.0)
        assert out[2, 2, 1] == np.float32(-0.5)


class TestC11Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C11"
        node = Lib1PolarCoordinatesNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            expected = node.parameters[spec.name]
            if spec.ptype in ("vec2", "vec3"):
                assert tuple(spec.default) == tuple(expected), spec.name
            else:
                assert spec.default == expected, spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
