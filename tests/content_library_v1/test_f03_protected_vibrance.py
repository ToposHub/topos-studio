"""F03 protected_vibrance 测试：oracle↔手算向量、恒等/HDR/肤色分区差量、
真实 parser 解析（显式暂存路径）、GLSL 红线静态检查。"""

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

from content_staging.library_v1.references import protected_vibrance_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "protected_vibrance"
TGLSL_PATH = COMPONENT_DIR / "protected_vibrance.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
BASE = {"saturation": 0.0, "vibrance": 1.0,
        "skin_hue_center": 20.0, "skin_hue_width": 25.0, "skin_protection": 1.0}

SKIN = np.array([0.65, 0.50, 0.40], dtype=np.float32)          # hue 24°
COMP_BLUE = np.array([0.39934, 0.54934, 0.64934], dtype=np.float32)  # hue 204°，同幅值


class TestHandVectors:
    def test_main_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["vectors"]:
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32), HAND_VECTORS["params"])[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"input={vec['input']} note={vec['note']}")

    def test_variant_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["variant_vectors"]:
            params = dict(HAND_VECTORS["params"])
            params.update(vec["params"])
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32), params)[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"params={vec['params']} input={vec['input']}")


class TestSemantics:
    def test_dual_zero_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"saturation": 0.0, "vibrance": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_default_params_identity(self) -> None:
        img = np.array([[0.0, 0.18, 4.5], [1.0, 0.0, 0.0]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img), img)

    def test_achromatic_bitwise_unchanged(self) -> None:
        gray = np.array([[0.18, 0.18, 0.18], [4.5, 4.5, 4.5]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(gray, BASE), gray)

    def test_luma_preserved_by_construction(self) -> None:
        img = np.array([[0.65, 0.50, 0.40], [12.0, 0.1, 4.5], [1.0, 0.0, 0.0],
                        [-0.25, -0.30, -0.20]], dtype=np.float32)
        out = ref.evaluate(img, BASE)
        np.testing.assert_allclose(out.astype(np.float64) @ LUMA,
                                   img.astype(np.float64) @ LUMA, rtol=0, atol=1e-6)

    def test_skin_vs_same_magnitude_nonskin(self) -> None:
        # 计划 §4 F03 验收点：肤色/非肤色排除——同 luma 同色度幅值、
        # 不同 hue 的两色增益不同（旧 Vibrance_Saturation 的幅值权重
        # 对这两色给出相同增益——机器可辨差量）
        pair = np.stack([SKIN, COMP_BLUE])
        luma_in = pair.astype(np.float64) @ LUMA
        mag = np.linalg.norm(pair.astype(np.float64) - luma_in[:, None], axis=-1)
        np.testing.assert_allclose(mag[0], mag[1], atol=1e-4)  # 前提：幅值相同
        out = ref.evaluate(pair, BASE)
        luma_out = out.astype(np.float64) @ LUMA
        gain = np.linalg.norm(out.astype(np.float64) - luma_out[:, None], axis=-1) / mag
        assert gain[0] == pytest.approx(1.0, abs=1e-6)   # 肤色全保护
        assert gain[1] == pytest.approx(2.0, abs=1e-5)   # 非肤色满增益

    def test_saturation_not_attenuated_by_skin_window(self) -> None:
        out = ref.evaluate(SKIN[None, :], dict(BASE, saturation=0.5, vibrance=0.0))[0]
        luma = float(SKIN.astype(np.float64) @ LUMA)
        np.testing.assert_allclose(out, luma + (SKIN - luma) * 1.5, atol=1e-6)

    def test_hue_wrap_around_zero(self) -> None:
        magenta = np.array([1.0, 0.75, 0.80], dtype=np.float32)  # hue 348°
        narrow = ref.evaluate(magenta[None, :], BASE)[0]
        wide = ref.evaluate(magenta[None, :], dict(BASE, skin_hue_width=60.0))[0]
        gain_n = float(np.max(np.abs(narrow - magenta)))
        gain_w = float(np.max(np.abs(wide - magenta)))
        assert gain_n > 10 * gain_w  # 默认窗不保护，宽窗回绕保护

    def test_protection_window_shape(self) -> None:
        # 窗形：d<=12.5 全保护；12.5..25 单调衰减；>=25 零保护
        for d, expect in ((0.0, 1.0), (12.5, 1.0), (18.0, 0.589568), (25.0, 0.0), (40.0, 0.0)):
            w = ref.protection_weight(np.array([20.0 + d]), 20.0, 25.0)[0]
            assert w == pytest.approx(expect, abs=1e-6), (d, w)
        # 无彩色 → 权重 0
        assert ref.protection_weight(np.array([-1.0]), 20.0, 25.0)[0] == 0.0

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import color_patches
        base = color_patches()
        base_out = ref.evaluate(base, BASE)

        def changed(params: dict) -> np.ndarray:
            merged = dict(BASE)
            merged.update(params)
            return ref.evaluate(base, merged)

        assert not np.allclose(changed({"saturation": 0.4}), base_out)
        assert not np.allclose(changed({"vibrance": 0.6}), base_out)
        assert not np.allclose(changed({"skin_hue_center": 200.0}), base_out)
        assert not np.allclose(changed({"skin_hue_width": 60.0}), base_out)
        assert not np.allclose(changed({"skin_protection": 0.0}), base_out)


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "protected_vibrance")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "protected_vibrance"
        assert parsed["type"] == "dctl"
        assert parsed["version"] == "0.1.0"
        caps = parsed["capabilities"]
        assert caps.get("export_support") == "all"
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

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        assert "while" not in body, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", body), "点处理不应有循环"
        for m in re.finditer(r"\bu_\w+", body):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, \
                f"未声明 uniform {m.group(0)}"
        # 环形距离与软窗存在（浮点语境）
        assert "mod(hue - skin_hue_center + 180.0, 360.0)" in body
        assert "smoothstep(0.5 * w, w, d)" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
