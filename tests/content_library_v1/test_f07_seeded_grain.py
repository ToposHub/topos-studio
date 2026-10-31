"""F07 seeded_grain 测试：oracle↔手算向量、恒等/HDR/seed 可复现/滑杆有效性、
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

from content_staging.library_v1.references import seeded_grain_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "seeded_grain"
TGLSL_PATH = COMPONENT_DIR / "seeded_grain.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _flat_8x4(value: float = 0.18) -> np.ndarray:
    return np.full((4, 8, 3), value, dtype=np.float32)


def _at(out: np.ndarray, pos: list[int]) -> np.ndarray:
    return out[pos[1], pos[0], :]


class TestHandVectors:
    def test_main_vectors(self) -> None:
        for vec in HAND["vectors"]:
            base = 4.5 if vec.get("hdr_input") else 0.18
            out = _at(ref.evaluate(_flat_8x4(base)), vec["pos"])
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"pos={vec['pos']} note={vec['note']}")

    def test_variant_vectors(self) -> None:
        for vec in HAND["variant_vectors"]:
            if vec["expected"] is None:
                continue
            out = _at(ref.evaluate(_flat_8x4(), vec["params"]), vec["pos"])
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"params={vec['params']} note={vec['note']}")


class TestSemantics:
    def test_strength_zero_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"strength": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_same_seed_reproduces_bit_exact(self) -> None:
        # 静态版核心语义：同参数两次求值比特级一致（无随机流/无时钟）
        img = _flat_8x4()
        a = ref.evaluate(img)
        b = ref.evaluate(img)
        np.testing.assert_array_equal(a, b)

    def test_seed_changes_output(self) -> None:
        img = _flat_8x4()
        base = ref.evaluate(img, {"seed": 7})
        other = ref.evaluate(img, {"seed": 8})
        assert not np.allclose(base, other), "seed 改变必须改变输出图案"

    def test_chroma_grain_makes_channels_diverge(self) -> None:
        img = _flat_8x4()
        mono = ref.evaluate(img, {"chroma": 0.0})
        color = ref.evaluate(img, {"chroma": 1.0})
        # chroma=0：三通道同偏移（与基色同为常色 → R=G=B）
        np.testing.assert_allclose(mono[..., 0], mono[..., 1], atol=1e-7)
        np.testing.assert_allclose(mono[..., 1], mono[..., 2], atol=1e-7)
        # chroma=1：R/B 相位独立 → 通道分离
        assert np.max(np.abs(color[..., 0] - color[..., 2])) > 1e-4

    def test_additive_grain_never_clamps_hdr(self) -> None:
        img = np.full((4, 8, 3), 12.0, dtype=np.float32)
        out = ref.evaluate(img)
        assert float(out.max()) > 12.0 or float(out.min()) < 12.0  # 有增量
        assert float(out.max()) > 1.0, "HDR 基色不被压回 1"

    def test_every_param_has_pixel_effect(self) -> None:
        img = _flat_8x4()
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"strength": 0.7}), base)
        assert not np.allclose(ref.evaluate(img, {"grain_size": 4.0}), base)
        assert not np.allclose(ref.evaluate(img, {"seed": 13}), base)
        assert not np.allclose(ref.evaluate(img, {"chroma": 1.0}), base)
        assert not np.allclose(ref.evaluate(img, {"shadow_bias": 0.9}), base)
        assert not np.allclose(ref.evaluate(img, {"highlight_bias": 0.9}), base)

    def test_midtone_peak_weighting(self) -> None:
        # 中间调颗粒大于饱和端（乳剂响应）：同位置增量 |out-in|
        mid = ref.evaluate(_flat_8x4(0.18), {"chroma": 0.0})
        dark = ref.evaluate(_flat_8x4(0.0), {"chroma": 0.0})
        d_mid = np.abs(mid - 0.18).mean()
        d_dark = np.abs(dark - 0.0).mean()
        assert d_mid > d_dark, "0.18 中间调权重(0.5904+偏置)须高于纯黑(0.25)"


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "seeded_grain")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "seeded_grain"
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
        assert m.slug == "seeded_grain"
        assert m.time.time_domain == "static"

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", code_only), "本插件应零循环（噪声 4-tap 手工展开）"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)
        assert "sin" not in code_only and "cos" not in code_only, "hash/value-noise 应无三角函数"

    def test_param_names_not_glsl_reserved(self) -> None:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        for p in (json.loads(
                (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))["params"]):
            assert p["name"] not in _GLSL_RESERVED_IDENTIFIERS, p["name"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
