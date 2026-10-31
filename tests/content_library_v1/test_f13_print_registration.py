"""F13 print_registration 测试：oracle↔独立手算向量、恒等/HDR/参数有效性、
套印偏移与分离模式差量、真实 parser、GLSL 红线（零循环）。"""

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

from content_staging.library_v1.references import print_registration_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "print_registration"
TGLSL_PATH = COMPONENT_DIR / "print_registration.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _uniform(rgb: tuple[float, float, float], size: int = 8) -> np.ndarray:
    return np.tile(np.array(rgb, dtype=np.float32), (size, size, 1))


def _fixture(name: str) -> np.ndarray:
    if name == "flatblack_8x8":
        return _uniform((0.0, 0.0, 0.0))
    if name == "flatred_8x8":
        return _uniform((1.0, 0.0, 0.0))
    if name == "flatwhite_16x16":
        return _uniform((1.0, 1.0, 1.0), size=16)
    if name == "gradient16":
        return np.tile((np.arange(16) / 15.0).astype(np.float32)[None, :, None], (16, 1, 3))
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
    def test_ink_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"ink_amount": 0.0}), img)
        # 恒等与 paper_base 取值无关（纸底只在有墨时参与）
        np.testing.assert_array_equal(
            ref.evaluate(img, {"ink_amount": 0.0, "paper_base": 1.0}), img)

    def test_flat_black_position_independent(self) -> None:
        flat = _uniform((0.0, 0.0, 0.0), size=16)
        out = ref.evaluate(flat)
        np.testing.assert_allclose(out, 0.144, atol=1e-7)

    def test_separation_modes_differ_on_color(self) -> None:
        # 分离模式差量：彩色输入上两档输出不同（CMY=通道互补驱动 vs Luma=同源驱动）
        img = _uniform((1.0, 0.0, 0.0), size=8)
        cmy = ref.evaluate(img)
        luma = ref.evaluate(img, {"cmyk_separation": "Luma"})
        assert not np.allclose(cmy, luma)
        # 灰输入两档等价（源场相同——Luma 档同源语义的钉住）
        gray = _uniform((0.5, 0.5, 0.5), size=8)
        np.testing.assert_allclose(
            ref.evaluate(gray), ref.evaluate(gray, {"cmyk_separation": "Luma"}), atol=1e-7)

    def test_reg_offset_effect(self) -> None:
        # 套印偏移差量：梯度上偏移改变 Y 版采样源 → 网点变化
        img = _fixture("gradient16")
        base = ref.evaluate(img)
        aligned = ref.evaluate(img, {"reg_offset_x": 0.0, "reg_offset_y": 0.0})
        shifted = ref.evaluate(img, {"reg_offset_x": 2.0, "reg_offset_y": 0.5})
        assert not np.allclose(base, aligned)
        assert not np.allclose(base, shifted)
        # 平坦场对偏移不敏感
        flat = _uniform((0.5, 0.5, 0.5), size=8)
        np.testing.assert_array_equal(
            ref.evaluate(flat), ref.evaluate(flat, {"reg_offset_x": 4.0, "reg_offset_y": -4.0}))

    def test_paper_and_ink_have_effect(self) -> None:
        flat = _uniform((0.0, 0.0, 0.0), size=8)
        assert not np.allclose(
            ref.evaluate(flat, {"paper_base": 0.5}), ref.evaluate(flat))
        assert not np.allclose(
            ref.evaluate(flat, {"ink_amount": 0.4}), ref.evaluate(flat))

    def test_finite_on_mixed_hdr(self) -> None:
        img = np.array(
            [[[12.0, -0.25, 4.5], [0.18, 1.0, 0.0], [8.0, 8.0, 8.0]]], dtype=np.float32)
        out = ref.evaluate(img)
        assert np.all(np.isfinite(out))
        # HDR 线性衰减混入纸底：out ≤ color*(1-pb) + pb（pb = paper_base*ink
        # = 0.72），无内容钳制（SDR 输入下输出 ≤1，HDR 按 0.28 系数透传）
        pb = 0.9 * 0.8
        bound = img * (1.0 - pb) + pb
        assert np.all(out <= bound + 1e-6)

    def test_flat_white_sparse_dots(self) -> None:
        # 平坦白：r=0 仅网屏胞心 0.06 邻域出小网点 → 大部分像素精确 1.0
        out = ref.evaluate(_uniform((1.0, 1.0, 1.0), size=16))
        frac_paper = float((out >= 1.0 - 1e-6).all(axis=-1).mean())
        assert frac_paper > 0.9


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "print_registration")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "print_registration"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        assert parsed["preview_cost"] == "low"

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
        # 三版全展开：本插件零循环
        assert not re.search(r"\bfor\s*\(", code_only), "3 版展开不应有循环"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
