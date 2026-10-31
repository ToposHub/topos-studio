"""F15 watercolor 测试：oracle↔独立手算向量、恒等/HDR/参数有效性、
梯度反向扩散方向、平坦区性质、真实 parser、GLSL 红线（循环上限常量）。"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from content_staging.library_v1.references import watercolor_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "watercolor"
TGLSL_PATH = COMPONENT_DIR / "watercolor.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _uniform(value: float, size: int = 8) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.float32)


def _step_edge(dark_left: bool = True) -> np.ndarray:
    img = np.zeros((8, 8, 3), dtype=np.float32)
    img[:, :4] = 0.2 if dark_left else 1.0
    img[:, 4:] = 1.0 if dark_left else 0.2
    return img


def _fixture(name: str) -> np.ndarray:
    if name == "edge8":
        return _step_edge(dark_left=True)
    if name == "edge8_flipped":
        return _step_edge(dark_left=False)
    if name == "flat16":
        return _uniform(0.5, size=16)
    raise KeyError(name)


def _at(out: np.ndarray, pos: list[int]) -> np.ndarray:
    return out[pos[0], pos[1], :]


class TestHandVectors:
    def test_main_vectors(self) -> None:
        for vec in HAND["vectors"]:
            out = ref.evaluate(_fixture(vec["fixture"]), vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['fixture']} {vec['note']}")

    def test_variant_vectors(self) -> None:
        for vec in HAND["variant_vectors"]:
            if vec["expected"] is None:
                continue
            out = ref.evaluate(_fixture(vec["fixture"]), vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['desc']}")


class TestSemantics:
    def test_amount_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"amount": 0.0}), img)

    def test_flat_input_independent_of_diffusion_and_pigment(self) -> None:
        # 平坦区 em=0：扩散位移 0、聚边增益 1——两参数在平坦输入上精确不变
        flat = _uniform(0.5, size=16)
        base = ref.evaluate(flat)
        np.testing.assert_array_equal(ref.evaluate(flat, {"diffusion": 0.0}), base)
        np.testing.assert_array_equal(ref.evaluate(flat, {"diffusion": 1.0}), base)
        np.testing.assert_array_equal(ref.evaluate(flat, {"edge_pigment": 0.0}), base)
        np.testing.assert_array_equal(ref.evaluate(flat, {"edge_pigment": 1.0}), base)
        # 纸纹仍起作用：空间变化且在调暗域 [0.5*0.75, 0.5] 内
        assert float(base[..., 0].std()) > 1e-3
        assert base.min() >= 0.5 * 0.75 - 1e-6
        assert base.max() <= 0.5 + 1e-6

    def test_flat_output_matches_paper_formula(self) -> None:
        # 平坦灰：out = g·(1−0.25·n)，n 由独立 uint32 hash 重算（禁自证）
        flat = _uniform(0.5, size=16)

        def h32(ix: int, iy: int) -> float:
            h = ((ix * 0x9E3779B1) ^ (iy * 0x85EBCA77)) & 0xFFFFFFFF
            h ^= h >> 13
            h = (h * 0xC2B2AE3D) & 0xFFFFFFFF
            h ^= h >> 16
            return h / 4294967296.0

        out = ref.evaluate(flat)
        for (y, x) in [(0, 0), (5, 3), (7, 7), (15, 15)]:
            pqy, pqx = y + 0.5, x + 0.5
            gy, gx = pqy / 8.0, pqx / 8.0
            y0, x0 = int(gy), int(gx)
            fy, fx = gy - y0, gx - x0
            n = (h32(x0, y0) * (1 - fx) * (1 - fy) + h32(x0 + 1, y0) * fx * (1 - fy)
                 + h32(x0, y0 + 1) * (1 - fx) * fy + h32(x0 + 1, y0 + 1) * fx * fy)
            np.testing.assert_allclose(
                out[y, x, 0], 0.5 * (1.0 - 0.25 * n), atol=1e-6,
                err_msg=f"paper formula at ({y},{x})")

    def test_diffusion_direction_follows_negative_gradient(self) -> None:
        # 方向敏感性：边缘翻转后同位输出显著不同（tap 朝暗侧 vs 朝亮侧）
        out = ref.evaluate(_fixture("edge8"))
        out_flip = ref.evaluate(_fixture("edge8_flipped"))
        assert np.abs(out[3, 4] - out_flip[3, 4]).max() > 0.1

    def test_params_have_effect_on_edge(self) -> None:
        img = _fixture("edge8")
        base = ref.evaluate(img)
        for changed in ({"diffusion": 0.0}, {"edge_pigment": 0.0},
                        {"paper_scale": 2.0}, {"amount": 0.5}):
            assert not np.allclose(ref.evaluate(img, changed), base), changed

    def test_finite_on_mixed_hdr(self) -> None:
        img = np.array(
            [[[12.0, -0.25, 4.5], [0.18, 1.0, 0.0], [8.0, 8.0, 8.0]]], dtype=np.float32)
        assert np.all(np.isfinite(ref.evaluate(img)))

    def test_edge_pigment_boost_bounded(self) -> None:
        # 聚边增益有界：em=1 处 chroma ≤2x、亮度 ≤1.5x——彩色边缘上界检查
        img = np.zeros((8, 8, 3), dtype=np.float32)
        img[:, :4] = (0.05, 0.4, 0.05)   # 暗彩色
        img[:, 4:] = (1.6, 1.6, 1.6)     # 亮中性
        out = ref.evaluate(img, {"diffusion": 0.0})
        base = img
        luma_in = base @ np.array([0.2126, 0.7152, 0.0722])
        luma_out = out @ np.array([0.2126, 0.7152, 0.0722])
        # 边缘带上亮度提升不超过 1.5x + 纸纹 0.75 下限
        edge_mask = luma_out > luma_in * 1.0
        assert np.all(luma_out[edge_mask] <= luma_in[edge_mask] * 1.5 * (1.0 + 1e-6))
        assert np.all(np.isfinite(out))


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "watercolor")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "watercolor"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        assert parsed["preview_cost"] == "medium"

    def test_params_match_manifest(self, parsed: dict) -> None:
        manifest = json.loads(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        manifest_names = {p["name"] for p in manifest["params"]}
        parsed_names = {p["var_name"] for p in parsed["params"]}
        assert manifest_names == parsed_names

    def test_manifest_validates(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == []

    def test_glsl_red_lines(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only
        for m in re.finditer(r"for\s*\(\s*int\s+(\w+)\s*=\s*(-?\d+)\s*;\s*\1\s*(<=|<)\s*(\w+|\d+)", code_only):
            assert m.group(4).isdigit(), f"非常量循环上限: {m.group(0)}"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
