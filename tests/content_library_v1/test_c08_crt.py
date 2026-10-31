"""C08 lib1_crt 测试：marker 协议 + oracle↔手算（≥3 独立向量）+
恒等（双强度 0，含 HDR）+ 荫罩亮度中性/模式开关 + manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import crt_ref as ref
from content_staging.library_v1.nodes.crt.lib1_crt_node import (
    CRT_MODES, EFFECT_KEY, Lib1CrtNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "crt" / "component_manifest.json")


class TestC08NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1CrtNode()
        tex = TextureResult(texture_id=61, width=32, height=16)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_crt"
        assert out["_effect_input"] is tex
        for name in ("mode", "scan_count", "scan_strength", "mask_strength"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1CrtNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_modes_and_padding(self) -> None:
        assert CRT_MODES == ["Scanlines", "RGB Grille", "Full"]
        assert Lib1CrtNode()._tile_padding({}) == 0

    def test_invalid_mode_rejected(self) -> None:
        with pytest.raises(ValueError):
            ref.evaluate(np.zeros((2, 2, 3), dtype=np.float32), {"mode": "bad"})


class TestC08OracleHandVectors:
    def test_scanline_row_gains(self) -> None:
        # 手算 1：H=8、scan_count=2 → period=4；y=0: t=0.125 → tri=0.25→
        # tri²=0.0625 → gain=1−0.6·0.35·0.9375=0.803125；
        # y=1: t=0.375 → tri²=0.5625 → gain=1−0.6·0.35·0.4375=0.908125
        img = np.ones((8, 1, 3), dtype=np.float32)
        out = ref.evaluate(img, {"mode": "Scanlines", "scan_count": 2.0})
        np.testing.assert_allclose(out[0, 0], 0.803125, atol=1e-6)
        np.testing.assert_allclose(out[1, 0], 0.908125, atol=1e-6)

    def test_grille_channel_gains(self) -> None:
        # 手算 2：x=0：s=1/6 → mr=tri(1/6)=1/3、mg=tri(0.5)=1、mb=tri(5/6)=1/3；
        # k=0.5·0.8=0.4 → gain=(1−0.4/6·?,) 精确：1+(1/3−1/2)·0.4=0.9333...、
        # g: 1+(1−1/2)·0.4=1.2、b=0.9333...
        img = np.ones((1, 1, 3), dtype=np.float32)
        out = ref.evaluate(img, {"mode": "RGB Grille"})[0, 0]
        np.testing.assert_allclose(out[0], 1.0 + (1.0 / 3.0 - 0.5) * 0.4, atol=1e-6)
        np.testing.assert_allclose(out[1], 1.2, atol=1e-6)
        np.testing.assert_allclose(out[2], 1.0 + (1.0 / 3.0 - 0.5) * 0.4, atol=1e-6)

    def test_period_guard_min_one(self) -> None:
        # 手算 3：H=2、scan_count=1200 → period 守护=1 → t 恒 0.5 → tri=1 →
        # gain=1（扫描线在守护周期内无衰减）
        img = np.ones((2, 3, 3), dtype=np.float32)
        out = ref.evaluate(img, {"mode": "Scanlines", "scan_count": 1200.0,
                                 "scan_strength": 0.9})
        np.testing.assert_allclose(out, 1.0, atol=1e-6)

    def test_multiplicative_hdr_scaling(self) -> None:
        # 手算 4：乘性增益对 HDR 同式缩放：out = 4.0·gain（不 clamp）
        img = np.full((1, 1, 3), 4.0, dtype=np.float32)
        out = ref.evaluate(img, {"mode": "RGB Grille"})[0, 0]
        np.testing.assert_allclose(out[1], 4.0 * 1.2, atol=1e-5)


class TestC08IdentityAndSemantics:
    def test_dual_zero_strength_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.18, 12.0], [4.5, 1.0, -1.0]]], dtype=np.float32)
        for mode in CRT_MODES:
            np.testing.assert_array_equal(
                ref.evaluate(img, {"mode": mode, "scan_strength": 0.0,
                                   "mask_strength": 0.0}), img)

    def test_grille_discrete_mean_gain_pinned(self) -> None:
        """荫罩增益场：像素中心采样下 3px 周期的 s ∈ {1/6, 1/2, 5/6}（固定
        相位集）→ 离散均值 tri = 5/9，增益均值 = 1 + k/18（连续域才恰 1.0，
        离散口径显式钉住）。"""
        img = np.ones((4, 3, 3), dtype=np.float32)
        out = ref.evaluate(img, {"mode": "RGB Grille", "mask_strength": 0.5})
        np.testing.assert_allclose(out[0].mean(), 1.0 + 0.4 / 18.0, atol=1e-6)

    def test_modes_independent(self) -> None:
        img = np.full((6, 6, 3), 0.5, dtype=np.float32)
        base = ref.evaluate(img)
        scan_only = ref.evaluate(img, {"mode": "Scanlines"})
        grille_only = ref.evaluate(img, {"mode": "RGB Grille"})
        assert not np.allclose(scan_only, grille_only)
        assert not np.allclose(base, scan_only)
        # Full 的增益 = 两场之积（乘性可分解）
        ratio = base / (img + 1e-9)
        ratio_scan = scan_only / (img + 1e-9)
        ratio_grille = grille_only / (img + 1e-9)
        np.testing.assert_allclose(
            ratio, ratio_scan * ratio_grille, rtol=1e-5)

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(11)
        img = rng.rand(8, 8, 3).astype(np.float32)
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"mode": "Scanlines"}), base)
        # period = max(H/scan_count, 1)：8 行图上 480/1200 的 period 都钳 1px，
        # 且 period=1 时每行恰在三角峰（tri≡1）→ scan_strength 亦无效——
        # 两者都须 period>1 才可辨，合并探测
        with_scan = {"scan_count": 2.0}
        assert not np.allclose(ref.evaluate(img, with_scan), base)
        assert not np.allclose(
            ref.evaluate(img, {**with_scan, "scan_strength": 0.7}),
            ref.evaluate(img, with_scan))
        assert not np.allclose(ref.evaluate(img, {"mask_strength": 0.1}), base)

    def test_alpha_passthrough(self) -> None:
        img = np.zeros((1, 1, 4), dtype=np.float32)
        img[0, 0] = [0.5, 0.5, 0.5, 0.42]
        out = ref.evaluate(img)
        assert out[0, 0, 3] == np.float32(0.42)


class TestC08Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C08"
        node = Lib1CrtNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        mode_spec = next(p for p in manifest.params if p.name == "mode")
        assert list(mode_spec.options) == CRT_MODES


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
