"""C09 lib1_glitch 测试：marker 协议 + oracle↔手算（≥3 独立向量，含
hash 内联转录对拍）+ 恒等（corruption=0 且 rgb_split=0）+ seed 确定性
+ 条带一致性 + manifest 一致。"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.features.composite.nodes.base_node import TextureResult

from content_staging.library_v1.references import glitch_ref as ref
from content_staging.library_v1.nodes.glitch.lib1_glitch_node import (
    EFFECT_KEY, Lib1GlitchNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "glitch" / "component_manifest.json")


def _hash12_literal(x: float, y: float) -> float:
    """Dave Hoskins hash12 的独立字面转录（测试侧手算，不 import oracle）。"""
    a = (x * 0.1031) % 1.0
    b = (y * 0.1031) % 1.0
    c = a
    d = a * (b + 33.33) + b * (c + 33.33) + c * (a + 33.33)
    a2, b2, c2 = a + d, b + d, c + d
    return ((a2 + b2) * c2) % 1.0


class TestC09NodeMarker:
    def test_marker_protocol(self) -> None:
        node = Lib1GlitchNode()
        tex = TextureResult(texture_id=71, width=32, height=32)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_glitch"
        assert out["_effect_input"] is tex
        for name in ("band_height", "shift_amount", "rgb_split",
                     "corruption", "seed"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1GlitchNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding_bounds(self) -> None:
        node = Lib1GlitchNode()
        assert node._tile_padding({"shift_amount": 0.0}) == 13   # 12 split + 1
        assert node._tile_padding({"shift_amount": 1.0}) == 12 + 240 + 1


class TestC09OracleHandVectors:
    def test_identity_when_no_corruption_no_split(self) -> None:
        # 手算 1：corruption=0 → hot=0（n1<1 恒成立）→ x_shift=0；
        # rgb_split=0 → 三通道采样位重合 → 精确恒等（任意 shift/seed，含 HDR）
        img = np.array([[[0.1, 0.5, 0.9], [12.0, -0.3, 4.0]]], dtype=np.float32)
        for shift in (0.0, 1.0):
            for seed in (0.0, 100.0):
                np.testing.assert_array_equal(
                    ref.evaluate(img, {"corruption": 0.0, "rgb_split": 0.0,
                                       "shift_amount": shift, "seed": seed}), img)

    def test_split_only_hand_computed_shift(self) -> None:
        # 手算 2：corruption=0（hot=0）→ shift=0；rgb_split=0.25 →
        # split_px = 0.25·12·0.25 = 0.75；水平 ramp 上：
        # r 采 floor(x+0.5+0.75)=x+1、g 采 x、b 采 floor(x+0.5−0.75)=x−1
        # （x=0 的 b 钳半纹素 → texel 0）
        img = np.arange(6, dtype=np.float32).reshape(1, 6, 1) / 6.0
        img = np.repeat(img, 3, axis=2)
        out = ref.evaluate(img, {"corruption": 0.0, "rgb_split": 0.25,
                                 "shift_amount": 0.9})
        for x in range(6):
            assert out[0, x, 0] == pytest.approx(img[0, min(x + 1, 5), 0], abs=1e-7), x
            assert out[0, x, 1] == pytest.approx(img[0, x, 1], abs=1e-7), x
            assert out[0, x, 2] == pytest.approx(img[0, max(x - 1, 0), 2], abs=1e-7), x

    def test_hash_literal_transcription_matches_output(self) -> None:
        # 手算 3：hash 独立字面转录 → 推导 band 0/seed 0 的 n1/n2 →
        # corruption=1（hot=1 全出错）下 x_shift_px = (n2−0.5)·0.35·0.25·W
        w = 8
        img = np.zeros((2, w, 3), dtype=np.float32)
        img[0, :, 0] = 1.0            # 红通道条纹：位移可读
        img[1, 3, :] = 0.5
        n1 = _hash12_literal(0.0, 7.0)   # band = floor(0.5/24) = 0
        n2 = _hash12_literal(0.0, 23.0)
        assert n1 >= 0.0 and n1 < 1.0
        shift_px = (n2 - 0.5) * 0.35 * 0.25 * w
        out = ref.evaluate(img, {"corruption": 1.0})
        # 采样位 = x+0.5+shift_px+split_px（split = 0.3·12·1.0 = 3.6）
        for x in range(w):
            pos = min(max(x + 0.5 + shift_px + 3.6, 0.5), w - 0.5)
            assert out[0, x, 0] == pytest.approx(
                img[0, int(np.floor(pos)), 0], abs=1e-7), (x, shift_px)

    def test_seed_changes_hash_coordinates(self) -> None:
        # 手算 4：seed 进 hash（条带坐标平移 seed·97）——不同 seed 输出不同
        rng = np.random.RandomState(13)
        img = rng.rand(24, 32, 3).astype(np.float32)
        p = {"corruption": 0.5, "rgb_split": 0.2, "shift_amount": 0.5}
        a = ref.evaluate(img, dict(p, seed=0.0))
        b = ref.evaluate(img, dict(p, seed=5.0))
        assert not np.allclose(a, b), "seed 必须改变条带激活图案"
        # 同参数同 seed → 逐位复现（静态确定性）
        np.testing.assert_array_equal(a, ref.evaluate(img, dict(p, seed=0.0)))


class TestC09Semantics:
    def test_uniform_image_identity_any_params(self) -> None:
        rng = np.random.RandomState(2)
        img = np.full((12, 10, 3), 0.37, dtype=np.float32)
        for p in ({"corruption": 1.0, "shift_amount": 1.0, "rgb_split": 1.0},
                  {"corruption": 0.5, "seed": 42.0}):
            np.testing.assert_array_equal(ref.evaluate(img, p), img)

    def test_band_coherence(self) -> None:
        # 同一条带内所有行共享同一 shift/split → 相同输入行 → 相同输出行
        row = np.arange(16, dtype=np.float32).reshape(1, 16, 1) / 16.0
        img = np.repeat(row, 12, axis=0)  # band_height=4 → 3 条带 × 4 行
        img = np.repeat(img, 3, axis=2)
        out = ref.evaluate(img, {"band_height": 4.0, "corruption": 1.0,
                                 "shift_amount": 0.8, "rgb_split": 0.5})
        for band in range(3):
            rows = out[band * 4:(band + 1) * 4]
            # numpy 2.5 assert_allclose 拒绝可广播的不同形状 → 显式 broadcast_to
            np.testing.assert_allclose(
                rows, np.broadcast_to(rows[0], rows.shape), atol=0.0,
                err_msg=band)

    def test_every_param_has_pixel_effect(self) -> None:
        rng = np.random.RandomState(17)
        img = rng.rand(48, 24, 3).astype(np.float32)  # band_height=4 → 12 条带
        base = ref.evaluate(img, {"corruption": 0.5, "band_height": 4.0})
        assert not np.allclose(
            ref.evaluate(img, {"corruption": 0.5, "band_height": 8.0}), base)
        assert not np.allclose(
            ref.evaluate(img, {"corruption": 0.5, "shift_amount": 0.9,
                               "band_height": 4.0}), base)
        assert not np.allclose(
            ref.evaluate(img, {"corruption": 0.5, "rgb_split": 0.1,
                               "band_height": 4.0}), base)
        # corruption 改变 hot 判定门限 n1 ≥ 1−c：须多条带才有 band 跨门限
        # （单带图上 n1 固定，0.5→0.9 可能同为 hot/cold——初版 24×24 单带图
        # 两档输出全等，属探测图像构造错误非核缺陷）
        assert not np.allclose(
            ref.evaluate(img, {"corruption": 0.9, "band_height": 4.0}), base)
        assert not np.allclose(
            ref.evaluate(img, {"corruption": 0.5, "seed": 7.0,
                               "band_height": 4.0}), base)

    def test_alpha_passthrough(self) -> None:
        img = np.zeros((4, 8, 4), dtype=np.float32)
        img[..., 3] = 0.66
        out = ref.evaluate(img, {"corruption": 1.0, "rgb_split": 0.5})
        np.testing.assert_array_equal(out[..., 3], np.float32(0.66))


class TestC09Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C09"
        node = Lib1GlitchNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
