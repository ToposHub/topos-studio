"""C10 lib1_kaleidoscope 测试：手算向量（折叠镜像/首扇区恒等/rotation）、
amount=0 恒等、扇区数钳制、HDR/alpha、manifest 契约。

手算独立脚本（纯 Python/math，不 import 核）：8x8 ramp img[y,x]=y·8+x，
center (0.5,0.5) → c=(3.5,3.5)，sectors=4 → A=π/2、half=π/4；
folded = a ≤ half ? a : A − a（镜像回 [0, half]），同半径采样。
参数名修正记录：mix 是 GLSL 保留字（AUD-0832），按 F16/F19 先例改 amount。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.references import kaleidoscope_ref as ref
from content_staging.library_v1.nodes.kaleidoscope.lib1_kaleidoscope_node import (
    EFFECT_KEY, Lib1KaleidoscopeNode,
)

MANIFEST_PATH = (REPO_ROOT / "content_staging" / "library_v1" / "nodes"
                 / "kaleidoscope" / "component_manifest.json")


def _ramp() -> np.ndarray:
    return np.arange(64, dtype=np.float32).reshape(8, 8, 1).repeat(3, axis=2)


class TestC10HandVectors:
    def test_mirror_sample_across_sector_edge(self) -> None:
        # 手算 1：(3,5) dx=1.5 dy=−0.5 → theta=−0.3217 → a=1.249 > half
        # → folded=0.3217 → 采样 (y=4, x=5) → img[4,5]=37
        out = ref.evaluate(_ramp(), {"sectors": 4, "amount": 1.0})
        assert float(out[3, 5, 0]) == pytest.approx(37.0, abs=1e-6)

    def test_first_sector_identity(self) -> None:
        # 手算 2：(4,6) dx=2.5 dy=0.5 → theta=0.1974 ≤ half → 采样自身 → 38
        out = ref.evaluate(_ramp(), {"sectors": 4, "amount": 1.0})
        assert float(out[4, 6, 0]) == pytest.approx(38.0, abs=1e-6)

    def test_rotation_reselects_sector(self) -> None:
        # 手算 3：rotation=45 后 (3,6) 折叠相位改变 → 双线性采样
        # (4.9178, 5.6243) ≈ 44.935；不转时同点折叠采样 (4,6)=38。
        # 注：整扇区角（90°@4扇区）旋转是折叠场的不动点（a(θ+A)=a(θ)），
        # 不可辨——手算向量须避开整扇区角（初版误用 90°，两档输出全等）
        out = ref.evaluate(_ramp(), {"sectors": 4, "rotation": 45.0, "amount": 1.0})
        assert float(out[3, 6, 0]) == pytest.approx(44.9350288, abs=1e-5)
        base = ref.evaluate(_ramp(), {"sectors": 4, "amount": 1.0})
        assert float(base[3, 6, 0]) == pytest.approx(38.0, abs=1e-6)


class TestC10IdentityAndSemantics:
    def test_amount_zero_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.18, 12.0], [4.5, 1.0, -1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"amount": 0.0}), img)

    def test_sectors_clamped(self) -> None:
        img = _ramp()
        np.testing.assert_array_equal(
            ref.evaluate(img, {"sectors": 99}),
            ref.evaluate(img, {"sectors": 12}))
        np.testing.assert_array_equal(
            ref.evaluate(img, {"sectors": 1}),
            ref.evaluate(img, {"sectors": 3}))

    def test_output_bounded_by_input_range(self) -> None:
        # 折叠采样是输入域内的凸组合（双线性）→ 输出值域 ⊆ 输入值域
        img = _ramp()
        out = ref.evaluate(img, {"sectors": 6, "amount": 1.0})
        assert float(out.min()) >= float(img.min()) - 1e-5
        assert float(out.max()) <= float(img.max()) + 1e-5

    def test_alpha_passthrough(self) -> None:
        img = np.zeros((2, 2, 4), dtype=np.float32)
        img[..., 3] = 0.42
        out = ref.evaluate(img, {"sectors": 5})
        np.testing.assert_array_equal(out[..., 3], np.float32(0.42))

    def test_every_param_has_pixel_effect(self) -> None:
        img = _ramp()
        base = ref.evaluate(img, {"sectors": 6})
        assert not np.allclose(ref.evaluate(img, {"sectors": 5}), base)
        assert not np.allclose(ref.evaluate(img, {"sectors": 6, "rotation": 30.0}), base)
        assert not np.allclose(ref.evaluate(img, {"sectors": 6, "center": (0.3, 0.7)}), base)
        assert not np.allclose(ref.evaluate(img, {"sectors": 6, "amount": 0.5}), base)


class TestC10NodeMarker:
    def test_marker_protocol(self) -> None:
        from content_staging.library_v1.nodes.kaleidoscope import \
            lib1_kaleidoscope_node as mod
        node = Lib1KaleidoscopeNode()
        assert node.node_type == EFFECT_KEY == "lib1_kaleidoscope"
        assert "amount" in mod._EFFECT_PARAM_NAMES
        assert "mix" not in mod._EFFECT_PARAM_NAMES  # GLSL 保留字不得回归
        assert "mix" not in [p.name for p in node.PARAM_DEFS]


class TestC10Manifest:
    def test_manifest_valid_and_matches_node(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        manifest = ComponentManifest.from_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest.validate() == []
        assert manifest.component_id == "C10"
        node = Lib1KaleidoscopeNode()
        assert [p.name for p in manifest.params] == \
            [p.name for p in node.PARAM_DEFS]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
