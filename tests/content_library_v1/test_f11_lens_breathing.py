"""F11 lens_breathing 测试：oracle↔手算向量、恒等/HDR/pivot 锚定/边界、
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

from content_staging.library_v1.references import lens_breathing_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "lens_breathing"
TGLSL_PATH = COMPONENT_DIR / "lens_breathing.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def ramp_8x4() -> np.ndarray:
    """img[y][x] = (x + 8y)/31，RGB 同值。"""
    y, x = np.meshgrid(np.arange(4), np.arange(8), indexing="ij")
    v = (x + 8 * y) / 31.0
    return np.repeat(v[:, :, None], 3, axis=2).astype(np.float32)


def _at(out: np.ndarray, pos: list[int]) -> float:
    return float(out[pos[1], pos[0], 0])


class TestHandVectors:
    def test_main_vectors(self) -> None:
        base_params = HAND["params"]
        for vec in HAND["vectors"]:
            params = dict(base_params)
            params.update(vec.get("params", {}))
            out = _at(ref.evaluate(ramp_8x4(), params), vec["pos"])
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]

    def test_variant_vectors(self) -> None:
        base_params = HAND["params"]
        for vec in HAND["variant_vectors"]:
            if vec["expected"] is None:
                continue
            params = dict(base_params)
            params.update(vec.get("params", {}))
            out = _at(ref.evaluate(ramp_8x4(), params), vec["pos"])
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]


class TestSemantics:
    def test_amount_zero_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"amount": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_scale_one_identity_including_hdr(self) -> None:
        img = np.array([[[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"scale": 1.0})
        np.testing.assert_array_equal(out, img)

    def test_pivot_pixel_is_fixed_point(self) -> None:
        # pivot 恰为某像素中心时该像素为不动点（任意 scale 精确拷贝）
        img = ramp_8x4()
        for scale in (0.8, 1.2):
            out = _at(ref.evaluate(img, {"scale": scale, "pivot_x": 0.5625,
                                         "pivot_y": 0.625}), [4, 2])
            assert out == pytest.approx((4 + 16) / 31.0, abs=1e-7)

    def test_flat_field_invariant_freeze(self) -> None:
        flat = np.full((16, 32, 3), 0.73, dtype=np.float32)
        out = ref.evaluate(flat, {"scale": 0.8, "border_mode": "Freeze"})
        np.testing.assert_allclose(out, flat, atol=1e-7)

    def test_black_vs_freeze_border(self) -> None:
        img = ramp_8x4()
        common = {"scale": 0.8, "amount": 1.0}
        freeze = ref.evaluate(img, {**common, "border_mode": "Freeze"})
        black = ref.evaluate(img, {**common, "border_mode": "Black"})
        assert (black == 0.0).any(), "zoom-in 出界像素 Black 须为 0"
        inside = black != 0.0
        np.testing.assert_array_equal(freeze[inside], black[inside])

    def test_zoom_out_pulls_frame_edges_inward(self) -> None:
        # scale>1（zoom-out，视野扩大）：边缘像素采样自更内侧 → 左缘值升高
        img = ramp_8x4()
        out = ref.evaluate(img, {"scale": 1.2})
        assert _at(out, [0, 0]) > _at(img, [0, 0]), "zoom-out 左缘应采样更内侧（值更高）"

    def test_amount_interpolates_scale(self) -> None:
        img = ramp_8x4()
        identity = ref.evaluate(img, {"amount": 0.0})
        full = ref.evaluate(img, {"amount": 1.0, "scale": 0.8})
        half = ref.evaluate(img, {"amount": 0.5, "scale": 0.8})
        # amount=0.5 严格介于两端之间（线性插值 s）
        assert not np.allclose(half, identity)
        assert not np.allclose(half, full)

    def test_every_param_has_pixel_effect(self) -> None:
        img = ramp_8x4()
        base = ref.evaluate(img, {"amount": 1.0, "pivot_x": 0.5, "pivot_y": 0.5,
                                  "scale": 1.2, "border_mode": "Freeze"})
        assert not np.allclose(ref.evaluate(img, {"amount": 0.7, "pivot_x": 0.5,
                                                  "pivot_y": 0.5, "scale": 1.2}), base)
        assert not np.allclose(ref.evaluate(img, {"amount": 1.0, "pivot_x": 0.3,
                                                  "pivot_y": 0.5, "scale": 1.2}), base)
        assert not np.allclose(ref.evaluate(img, {"amount": 1.0, "pivot_x": 0.5,
                                                  "pivot_y": 0.3, "scale": 1.2}), base)
        assert not np.allclose(ref.evaluate(img, {"amount": 1.0, "pivot_x": 0.5,
                                                  "pivot_y": 0.5, "scale": 0.9}), base)
        assert not np.allclose(ref.evaluate(img, {"amount": 1.0, "pivot_x": 0.5,
                                                  "pivot_y": 0.5, "scale": 0.8,
                                                  "border_mode": "Black"}), base)


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "lens_breathing")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "lens_breathing"
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
            if mp["ptype"] == "option":
                assert pp["options"] == list(mp["options"]), name
                assert pp["default"] == list(mp["options"]).index(mp["default"]), name
            else:
                assert float(pp["default"]) == float(mp["default"]), name
                assert float(pp["min"]) == float(mp["minimum"]), name
                assert float(pp["max"]) == float(mp["maximum"]), name

    def test_manifest_roundtrip_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == [], m.validate()
        assert m.slug == "lens_breathing"
        assert m.time.time_domain == "static"
        assert m.image.sampling_border == "clamp-half-texel"

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", code_only), "单次重采样应零循环"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)
        assert re.search(r"clamp\s*\(\s*suv\s*,\s*mn\s*,\s*mx\s*\)", code_only), \
            "采样坐标须钳半纹素边界"
        assert re.search(r"vec2 mn = vec2\(0\.5\) / ts", code_only)

    def test_param_names_not_glsl_reserved(self) -> None:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        for p in (json.loads(
                (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))["params"]):
            assert p["name"] not in _GLSL_RESERVED_IDENTIFIERS, p["name"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
