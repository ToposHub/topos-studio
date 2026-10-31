"""F09 dust_scratches 测试：oracle↔手算向量、恒等/HDR 乘性/单调性/滑杆有效性、
真实 parser、GLSL 红线、manifest 契约。"""

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

from content_staging.library_v1.references import dust_scratches_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "dust_scratches"
TGLSL_PATH = COMPONENT_DIR / "dust_scratches.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def white_16() -> np.ndarray:
    return np.ones((16, 16, 3), dtype=np.float32)


def _at(out: np.ndarray, pos: list[int]) -> float:
    return float(out[pos[1], pos[0], 0])


class TestHandVectors:
    def test_main_vectors(self) -> None:
        for vec in HAND["vectors"]:
            out = _at(ref.evaluate(white_16()), vec["pos"])
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]

    def test_variant_vectors(self) -> None:
        for vec in HAND["variant_vectors"]:
            if vec["expected"] is None:
                continue
            out = _at(ref.evaluate(white_16(), vec["params"]), vec["pos"])
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]


class TestSemantics:
    def test_density_zero_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"density": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_opacity_zero_mathematical_identity(self) -> None:
        img = white_16()
        out = ref.evaluate(img, {"opacity": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_multiplicative_darkening_never_brightens(self) -> None:
        out = ref.evaluate(white_16())
        assert float(out.min()) >= 1.0 - 1e-7 - 0.9, "衰减上限 0.9"
        assert float(out.max()) <= 1.0 + 1e-7, "只加暗不加亮"

    def test_hdr_scales_through_multiplicatively(self) -> None:
        white = white_16()
        hdr = white * 12.0
        out_w = ref.evaluate(white)
        out_h = ref.evaluate(hdr)
        # 乘性：HDR 输出 = 白图输出 × 12（逐像素同衰减系数）
        np.testing.assert_allclose(out_h, out_w * 12.0, rtol=1e-6, atol=1e-5)
        assert float(out_h.max()) > 1.0, "HDR 不被 clamp"

    def test_density_monotone_darkening_same_seed(self) -> None:
        # 同 seed 下密度门只增不减：逐像素输出随 density 单调不减（变暗）
        d_lo = ref.evaluate(white_16(), {"density": 0.2})
        d_hi = ref.evaluate(white_16(), {"density": 0.8})
        assert np.all(d_hi <= d_lo + 1e-7)

    def test_seed_changes_output(self) -> None:
        base = ref.evaluate(white_16(), {"seed": 7})
        other = ref.evaluate(white_16(), {"seed": 8})
        assert not np.allclose(base, other), "seed 改变必须改变图案"

    def test_same_seed_reproduces_bit_exact(self) -> None:
        a = ref.evaluate(white_16())
        b = ref.evaluate(white_16())
        np.testing.assert_array_equal(a, b)

    def test_scratch_is_vertical_line(self) -> None:
        # density=1.0/seed=5：列 2 被选中（像素 x∈[6,8]）；x=8 处 fx=0.8333
        # 距线位 0.7432 为 0.0901 < w=0.1603 → prof=0.4072；
        # 中段（行 5..12，远离端部 0.03 羽化）每行同衰减 0.0855177（尘点只增）
        img = white_16()
        out = ref.evaluate(img, {"density": 1.0, "seed": 5})
        col_atten = 1.0 - out[:, :, 0]
        np.testing.assert_allclose(col_atten[5:12, 8], 0.0855177, rtol=0, atol=2e-6)
        # 线外像素（x=6，fx=0.1667 距线位 0.5765 > w）划痕衰减为 0：
        # 只允许尘点贡献（<1e-6 的行不少见；上限钉住划痕缺席）
        assert float(col_atten[5:12, 6].max()) < 0.36, "线外无划痕衰减（仅尘点）"

    def test_every_param_has_pixel_effect(self) -> None:
        base = ref.evaluate(white_16(), {"density": 1.0, "seed": 5})
        assert not np.allclose(ref.evaluate(white_16(), {"density": 1.0, "seed": 5,
                                                         "scratch_length": 1.0}), base)
        assert not np.allclose(ref.evaluate(white_16(), {"density": 1.0, "seed": 5,
                                                         "opacity": 0.3}), base)
        assert not np.allclose(ref.evaluate(white_16(), {"density": 0.5, "seed": 5}), base)
        assert not np.allclose(ref.evaluate(white_16(), {"density": 1.0, "seed": 9}), base)
        assert not np.allclose(ref.evaluate(white_16(), {"density": 1.0, "seed": 5,
                                                         "size": 6.0}), base)


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "dust_scratches")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "dust_scratches"
        assert parsed["version"] == "0.1.0"
        caps = parsed["capabilities"]
        assert caps.get("export_support") == "opengl"
        assert caps.get("input_space") == "scene-linear"

    def test_param_schema_matches_manifest(self, parsed: dict) -> None:
        manifest = json.loads(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        manifest_params = {p["name"]: p for p in manifest["params"]}
        parsed_params = {p["var_name"]: p for p in parsed["params"]}
        assert set(manifest_params) == set(parsed_params), "manifest 与 @param 集合不一致"
        for name, mp in manifest_params.items():
            pp = parsed_params[name]
            assert float(pp["default"]) == float(mp["default"]), name
            assert float(pp["min"]) == float(mp["minimum"]), name
            assert float(pp["max"]) == float(mp["maximum"]), name

    def test_manifest_roundtrip_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == [], m.validate()
        assert m.slug == "dust_scratches"
        assert m.time.time_domain == "static"

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", code_only), "程序版应零循环零邻域采样"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)
        # textureSize 存在但无 texture() 采样（程序版声明）
        assert "textureSize" in code_only
        assert not re.search(r"(?<!texture)\btexture\s*\(", code_only), "程序版不应采样纹理"

    def test_param_names_not_glsl_reserved(self) -> None:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        for p in (json.loads(
                (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))["params"]):
            assert p["name"] not in _GLSL_RESERVED_IDENTIFIERS, p["name"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
