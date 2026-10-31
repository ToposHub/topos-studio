"""F10 starburst 测试：oracle↔独立手算向量、恒等/HDR/滑杆有效性、
多叶方向差量、真实 parser、GLSL 红线（循环上限常量）。"""

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

from content_staging.library_v1.references import starburst_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "starburst"
TGLSL_PATH = COMPONENT_DIR / "starburst.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _uniform(value: float, size: int = 8) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.float32)


def _fixture(name: str) -> np.ndarray:
    if name == "uniform20_8x8":
        return _uniform(2.0)
    if name == "uniform02_8x8":
        return _uniform(0.2)
    if name == "uniform12_8x8":
        return _uniform(12.0)
    if name == "point33":
        img = _uniform(0.02, size=33)
        img[16, 16] = 2.0
        return img
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
                continue  # 恒等变体由 TestSemantics 覆盖
            out = ref.evaluate(_fixture(vec["fixture"]), vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['desc']}")


class TestSemantics:
    def test_intensity_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"intensity": 0.0}), img)
        np.testing.assert_array_equal(ref.evaluate(img, {"ray_length": 0.0}), img)

    def test_below_threshold_flat_identity(self) -> None:
        flat = _uniform(0.2, size=8)
        np.testing.assert_array_equal(ref.evaluate(flat, {"intensity": 1.0}), flat)

    def test_finite_on_mixed_hdr(self) -> None:
        img = np.array(
            [[[12.0, -0.25, 4.5], [0.18, 1.0, 0.0], [8.0, 8.0, 8.0]]], dtype=np.float32)
        assert np.all(np.isfinite(ref.evaluate(img)))

    def test_blades_change_output(self) -> None:
        # 方向数差量验收：同一夹具上 2/4/6 叶输出两两不同
        img = _fixture("point33")
        outs = {b: ref.evaluate(img, {"blades": b}) for b in ("2", "4", "6")}
        assert not np.allclose(outs["2"], outs["4"])
        assert not np.allclose(outs["4"], outs["6"])
        assert not np.allclose(outs["2"], outs["6"])

    def test_blade_direction_count_semantics(self) -> None:
        # 钉住：45° 对角像素在 4 叶方向集内有能量、2 叶内精确为零
        img = _fixture("point33")
        out4 = ref.evaluate(img, {"blades": "4"})
        out2 = ref.evaluate(img, {"blades": "2"})
        assert np.abs(out4[19, 19] - img[19, 19]).max() > 1e-6
        np.testing.assert_array_equal(out2[19, 19], img[19, 19])

    def test_angle_rotates_rays(self) -> None:
        img = _fixture("point33")
        base = ref.evaluate(img)
        rot = ref.evaluate(img, {"angle": 30.0})
        assert not np.allclose(rot, base)
        # +x 轴像素：angle=0 有轴向射线能量，angle=30 射线离开 → 精确基色
        np.testing.assert_array_equal(rot[16, 20], img[16, 20])
        assert np.abs(base[16, 20] - img[16, 20]).max() > 1e-6

    def test_params_have_effect(self) -> None:
        img = _fixture("point33")
        base = ref.evaluate(img)
        for changed in ({"ray_length": 0.2}, {"threshold": 0.9},
                        {"intensity": 1.0}, {"angle": -30.0}):
            assert not np.allclose(ref.evaluate(img, changed), base), changed

    def test_energy_bounded_linear(self) -> None:
        # 均匀 HDR：star = s·(luma−thr) 线性有界（无平方失控）
        out = ref.evaluate(_uniform(12.0), {"intensity": 1.0})
        np.testing.assert_allclose(out[0, 0], 12.0 + 12.0 * (12.0 - 0.55), atol=1e-4)

    def test_on_axis_vs_off_axis(self) -> None:
        # 方向性语义：轴向像素（射线穿过，距源 4px）有能量；
        # 对角像素（距源 5.66px，最近 tap 距源 1.83px 超出足迹）精确为零
        img = _fixture("point33")
        out = ref.evaluate(img, {"blades": "4"})
        d_on = np.abs(out[16, 20] - img[16, 20]).max()
        d_off = np.abs(out[20, 20] - img[20, 20]).max()
        assert d_on > 0
        assert d_on > d_off


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "starburst")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "starburst"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        assert parsed["preview_cost"] == "high"
        # select 档解析为字符串选项表
        blades = {p["var_name"]: p for p in parsed["params"]}["blades"]
        assert blades["options"] == ["2", "4", "6"]
        assert blades["default"] == 1

    def test_params_match_manifest(self, parsed: dict) -> None:
        manifest = json.loads(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        manifest_names = {p["name"] for p in manifest["params"]}
        parsed_names = {p["var_name"] for p in parsed["params"]}
        assert manifest_names == parsed_names
        assert "ray_length" in manifest_names, "length 为 GLSL 保留字，必须命名 ray_length"

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
        # 行程参数不得使用保留字 length（AUD-0832）
        assert not re.search(r"@param\s+slider\s+length\b", src)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
