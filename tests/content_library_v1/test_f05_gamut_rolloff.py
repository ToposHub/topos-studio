"""F05 gamut_rolloff 测试：oracle↔手算向量、恒等/HDR/滑杆有效性、
真实 parser 解析（显式暂存路径）、GLSL 红线静态检查、manifest 契约。"""

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

from content_staging.library_v1.references import gamut_rolloff_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "gamut_rolloff"
TGLSL_PATH = COMPONENT_DIR / "gamut_rolloff.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))
LUMA_W = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


class TestHandVectors:
    def test_main_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["vectors"]:
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32))[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"input={vec['input']} note={vec['note']}")

    def test_variant_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["variant_vectors"]:
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32), vec["params"])[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"params={vec['params']} input={vec['input']}")


class TestSemantics:
    def test_strength_zero_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0], [-4.0, 0.0, 0.0]], dtype=np.float32)
        out = ref.evaluate(img, {"strength": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_gray_and_below_threshold_identity(self) -> None:
        # 灰梯度全图 m=0 恒等；SDR 常规色 m<thr 恒等
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        ramp = gray_ramp_linear()
        np.testing.assert_array_equal(ref.evaluate(ramp), ramp)
        below = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 1.0], [0.9, 0.4, 0.1]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(below), below)

    def test_above_threshold_chroma_magnitude_decreases(self) -> None:
        img = np.array([[4.0, 0.0, 0.0], [12.0, 0.1, 4.5]], dtype=np.float32)
        out = ref.evaluate(img)
        for row_in, row_out in zip(img, out):
            m_in = np.linalg.norm(row_in.astype(np.float64) - float(row_in @ LUMA_W))
            m_out = np.linalg.norm(row_out.astype(np.float64) - float(row_out @ LUMA_W))
            assert m_out < m_in, "超阈值像素色度幅值必须下降"

    def test_luma_and_hue_direction_preserved(self) -> None:
        rng_vals = np.array([[4.0, 0.0, 0.0], [12.0, 0.1, 4.5], [-4.0, 1.0, 0.5]], dtype=np.float32)
        out = ref.evaluate(rng_vals)
        luma_in = rng_vals.astype(np.float64) @ LUMA_W
        luma_out = out.astype(np.float64) @ LUMA_W
        np.testing.assert_allclose(luma_out, luma_in, rtol=0, atol=1e-6)
        # 色度向量同向（叉积≈0 → 比例系数一致）
        for row_in, row_out in zip(rng_vals, out):
            c_in = row_in.astype(np.float64) - float(row_in @ LUMA_W)
            c_out = row_out.astype(np.float64) - float(row_out @ LUMA_W)
            cross = np.cross(c_in, c_out)
            np.testing.assert_allclose(cross, 0.0, atol=1e-6)

    def test_output_not_clamped_hdr_negative(self) -> None:
        out = ref.evaluate(np.array([[-4.0, 0.0, 0.0], [12.0, 0.1, 4.5]], dtype=np.float32))
        assert float(out[0, 0]) < 0.0, "负值不被 clamp 到 0"
        assert float(out[1, 0]) > 1.0, "HDR 不被 clamp 到 1"

    def test_soft_knee_less_compression_than_hard(self) -> None:
        img = np.array([[1.6, 0.2, 0.2]], dtype=np.float32)  # ex≈0.18 < knee=0.2
        soft = ref.evaluate(img)[0]
        hard = ref.evaluate(img, {"knee": 0.0})[0]
        m_soft = float(np.linalg.norm(soft.astype(np.float64) - float(soft @ LUMA_W)))
        m_hard = float(np.linalg.norm(hard.astype(np.float64) - float(hard @ LUMA_W)))
        assert m_soft > m_hard, "软膝过渡区压缩斜率更缓（m' 更大）"

    def test_full_strength_hard_knee_hits_threshold_exactly(self) -> None:
        out = ref.evaluate(np.array([[4.0, 0.0, 0.0]], dtype=np.float32),
                           {"strength": 1.0, "knee": 0.0})[0]
        m = float(np.linalg.norm(out.astype(np.float64) - float(out @ LUMA_W)))
        assert m == pytest.approx(1.0, abs=1e-6)

    def test_every_param_has_pixel_effect(self) -> None:
        img = np.array([[4.0, 0.0, 0.0], [12.0, 0.1, 4.5], [1.6, 0.2, 0.2]], dtype=np.float32)
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"threshold": 1.5}), base)
        assert not np.allclose(ref.evaluate(img, {"knee": 0.0}), base)
        assert not np.allclose(ref.evaluate(img, {"strength": 0.4}), base)


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "gamut_rolloff")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "gamut_rolloff"
        assert parsed["type"] == "dctl"
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
        assert m.slug == "gamut_rolloff"

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        assert "while" not in body, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", body), "点处理不应有循环"
        for m in re.finditer(r"\bu_\w+", body):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, \
                f"未声明 uniform {m.group(0)}"
        assert "3.0 - 2.0" in body  # smoothstep 展开为浮点

    def test_param_names_not_glsl_reserved(self) -> None:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        for p in (json.loads(
                (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))["params"]):
            assert p["name"] not in _GLSL_RESERVED_IDENTIFIERS, p["name"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
