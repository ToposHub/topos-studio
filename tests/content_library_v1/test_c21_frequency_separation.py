"""C21 lib1_frequency_separation 测试：双输出 marker 协议 + oracle↔手算
（独立标量重推 Box 窗与 Gaussian 权重）+ 恒等锚（radius=0、常数图）+
high 负值不 clamp + 重建精确 + α 契约 + manifest 一致。"""

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

from content_staging.library_v1.references import frequency_separation_ref as ref
from content_staging.library_v1.nodes.frequency_separation.lib1_frequency_separation_node import (
    EFFECT_KEY, Lib1FrequencySeparationNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "frequency_separation" / "component_manifest.json")


def _hand_box_low_1d(row: list[float], k: int) -> list[float]:
    """手算侧独立转录：1D clamp 盒式均值（窗半宽 k，边缘重复计入）。"""
    n = len(row)
    out = []
    for i in range(n):
        acc = 0.0
        for d in range(-k, k + 1):
            acc += row[min(max(i + d, 0), n - 1)]
        out.append(acc / (2 * k + 1))
    return out


def _hand_gaussian_low_1d(row: list[float], sigma: float, k: int) -> list[float]:
    """手算侧独立转录：1D clamp 高斯（全窗归一化权重）。"""
    n = len(row)
    w = [math.exp(-(d * d) / (2.0 * sigma * sigma)) for d in range(-k, k + 1)]
    total = sum(w)
    w = [x / total for x in w]
    out = []
    for i in range(n):
        acc = 0.0
        for j, d in enumerate(range(-k, k + 1)):
            acc += row[min(max(i + d, 0), n - 1)] * w[j]
        out.append(acc)
    return out


class TestC21NodeMarker:
    def test_multi_output_marker_protocol(self) -> None:
        node = Lib1FrequencySeparationNode()
        tex = TextureResult(texture_id=121, width=32, height=24)
        out = node._do_evaluate({"image": tex}, ctx=None, fb_manager=None)
        assert out["_effect_type"] == EFFECT_KEY == "lib1_frequency_separation"
        assert out["_effect_input"] is tex
        # 多输出占位：两个输出端口各一个占位（texture_id=-1）
        assert set(out["low"].__class__.__dataclass_fields__) >= {"texture_id"}
        assert out["low"].texture_id == -1
        assert out["high"].texture_id == -1
        assert out["low"].width == out["high"].width == 32
        assert out["low"].height == out["high"].height == 24
        for name in ("radius", "filter", "mode"):
            assert out["_effect_params"][name] == node.parameters[name], name

    def test_output_ports_declared(self) -> None:
        node = Lib1FrequencySeparationNode()
        assert [p.name for p in node.outputs] == ["low", "high"]
        assert [p.name for p in node.inputs] == ["image", "mask"]  # CP-044 通用 mask

    def test_missing_input_returns_empty(self) -> None:
        node = Lib1FrequencySeparationNode()
        assert node._do_evaluate({}, ctx=None, fb_manager=None) == {}
        invalid = TextureResult(texture_id=-1, width=0, height=0)
        assert node._do_evaluate({"image": invalid}, ctx=None, fb_manager=None) == {}

    def test_tile_padding(self) -> None:
        node = Lib1FrequencySeparationNode()
        assert node._tile_padding({"radius": 4.0}) == 5
        assert node._tile_padding({"radius": 0.0}) == 1


class TestC21OracleHandVectors:
    def test_identity_radius_zero(self) -> None:
        # 手算 1：radius=0 → low=RGB、high=0 精确（任意 filter/mode，HDR）
        rng = np.random.RandomState(71)
        img = rng.rand(5, 6, 4).astype(np.float32) * np.float32(4.0)
        for filt in ("Box", "Gaussian"):
            for mode in ("LowHigh", "Reconstruct"):
                out = ref.evaluate(img, {"radius": 0.0, "filter": filt,
                                         "mode": mode})
                np.testing.assert_array_equal(out["low"], img)
                np.testing.assert_array_equal(out["high"][..., :3], 0.0)
                np.testing.assert_array_equal(out["high"][..., 3], 0.0)

    def test_box_hand_1x5_ramp(self) -> None:
        # 手算 2：1×5 ramp [0,1,2,4,8]、Box k=1（clamp 边缘重复计入）：
        # low = [1/3, 1, 7/3, 14/3, 20/3]；high = in − low（负值出现）
        row = [0.0, 1.0, 2.0, 4.0, 8.0]
        img = np.array(row, dtype=np.float32).reshape(1, 5, 1)
        img = np.repeat(img, 3, axis=2)
        out = ref.evaluate(img, {"radius": 1.0, "filter": "Box"})
        want_low = _hand_box_low_1d(row, 1)
        np.testing.assert_allclose(out["low"][0, :, 0], want_low, atol=1e-6)
        np.testing.assert_allclose(out["high"][0, :, 0],
                                   [row[i] - want_low[i] for i in range(5)],
                                   atol=1e-6)
        assert out["high"][0, :, 0].min() < 0.0      # 负值不 clamp（验收点）

    def test_gaussian_hand_weights(self) -> None:
        # 手算 3：Gaussian radius=3 → σ=1、k=3（ceil(3σ)）；
        # 测试侧独立重推权重 exp(−i²/2)/Σ 对拍 1×7 ramp
        row = [float(i * i) for i in range(7)]
        img = np.array(row, dtype=np.float32).reshape(1, 7, 1).repeat(3, axis=2)
        out = ref.evaluate(img, {"radius": 3.0, "filter": "Gaussian"})
        want = _hand_gaussian_low_1d(row, 1.0, 3)
        np.testing.assert_allclose(out["low"][0, :, 0], want, atol=1e-5)

    def test_constant_image_high_is_zero(self) -> None:
        # 手算 4：常数图 → DC 保持 → high ≈ 0（float64 求和噪声 ~1e-16，
        # 非 0 的部分是两趟分离求和的浮点舍入，非泄漏）
        img = np.full((4, 4, 3), 0.37, dtype=np.float32)
        for filt in ("Box", "Gaussian"):
            out = ref.evaluate(img, {"radius": 5.0, "filter": filt})
            np.testing.assert_allclose(out["high"][..., :3], 0.0, atol=1e-12)
            np.testing.assert_allclose(out["low"][..., :3], 0.37, atol=1e-6)


class TestC21Semantics:
    def test_reconstruction_in_declared_domain(self) -> None:
        # 验收点：low+high = 输入（float32 声明域 ~1e-5）；随机 HDR 图
        rng = np.random.RandomState(73)
        img = (rng.rand(6, 8, 4).astype(np.float32) - 0.3) * np.float32(6.0)
        for p in ({"radius": 3.0, "filter": "Box"},
                  {"radius": 3.0, "filter": "Gaussian"},
                  {"radius": 9.0, "filter": "Gaussian"}):
            out = ref.evaluate(img, p)
            recon = out["low"][..., :3] + out["high"][..., :3]
            np.testing.assert_allclose(recon, img[..., :3], atol=1e-5)

    def test_reconstruct_mode_outputs_sum_on_low(self) -> None:
        img = np.arange(24, dtype=np.float32).reshape(2, 4, 3)
        lh = ref.evaluate(img, {"radius": 1.0, "mode": "LowHigh"})
        rc = ref.evaluate(img, {"radius": 1.0, "mode": "Reconstruct"})
        np.testing.assert_allclose(rc["low"], img, atol=1e-5)   # 重建=输入
        np.testing.assert_array_equal(rc["high"], lh["high"])   # high 不变
        assert not np.allclose(lh["low"], img)

    def test_alpha_contract(self) -> None:
        # α 透传 low；high α=0
        rng = np.random.RandomState(77)
        img = rng.rand(4, 5, 4).astype(np.float32)
        out = ref.evaluate(img, {"radius": 2.0})
        np.testing.assert_array_equal(out["low"][..., 3], img[..., 3])
        np.testing.assert_array_equal(out["high"][..., 3], 0.0)

    def test_high_allows_negative_no_clamp(self) -> None:
        # 验收点：边缘/纹理处 high 为负且不被 clamp（HDR 值域亦保留）
        img = np.zeros((1, 7, 3), np.float32)
        img[0, 3, 0] = 10.0                     # 孤立尖峰
        out = ref.evaluate(img, {"radius": 1.0, "filter": "Box"})
        assert out["high"][0, 3, 0] > 0.0
        assert out["high"][0, 2, 0] < 0.0       # 邻域负高频
        assert out["low"][0, 3, 0] < np.float32(10.0)

    def test_high_sign_structure_random(self) -> None:
        # 高频结构：随机图上 high 正负兼有（峰值处正、两侧负）；
        # 注：clamp 边界下 mean(high) 不保证为 0（边缘像素重复计入，
        # 边界带 DC 偏置——README 如实记录，非 Sharpen 分界由重建测试钉住）
        rng = np.random.RandomState(79)
        img = rng.rand(6, 6, 3).astype(np.float32)
        out = ref.evaluate(img, {"radius": 1.0, "filter": "Box"})
        assert out["high"][..., :3].max() > 0.0
        assert out["high"][..., :3].min() < 0.0

    def test_invalid_params_raise(self) -> None:
        img = np.zeros((2, 2, 3), np.float32)
        with pytest.raises(ValueError):
            ref.evaluate(img, {"filter": "Lanczos"})
        with pytest.raises(ValueError):
            ref.evaluate(img, {"mode": "Hybrid"})


class TestC21Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C21"
        node = Lib1FrequencySeparationNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]
        for spec in manifest.params:
            assert spec.default == node.parameters[spec.name], spec.name
        port_names_out = [p.name for p in manifest.ports if p.direction == "output"]
        assert port_names_out == ["low", "high"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
