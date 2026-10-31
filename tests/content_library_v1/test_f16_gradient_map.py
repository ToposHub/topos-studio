"""F16 gradient_map 试点测试：oracle↔手算向量、恒等/HDR/滑杆有效性、
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

from content_staging.library_v1.references import gradient_map_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "gradient_map"
TGLSL_PATH = COMPONENT_DIR / "gradient_map.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


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
    def test_mix_zero_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"amount": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_hdr_endstop_saturation(self) -> None:
        # 显式策略：超白/负值映射端点停靠色，不是 0-1 裁剪也不是外推
        out = ref.evaluate(np.array([[12.0, 12.0, 12.0]], dtype=np.float32))[0]
        np.testing.assert_allclose(out, np.float32([1.00, 0.98, 0.90]), atol=1e-7)
        out_neg = ref.evaluate(np.array([[-0.25] * 3], dtype=np.float32))[0]
        np.testing.assert_allclose(out_neg, np.float32([0.02, 0.02, 0.05]), atol=1e-7)

    def test_monotone_in_luma(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        out = ref.evaluate(gray_ramp_linear())
        luma_out = out @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
        assert np.all(np.diff(luma_out[16]) > -1e-9)  # 中间行沿 x 单调不减

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        base = gray_ramp_linear()
        base_out = ref.evaluate(base)

        def changed(params: dict) -> np.ndarray:
            return ref.evaluate(base, params)

        assert not np.allclose(changed({"pos_1": 0.2}), base_out)
        assert not np.allclose(changed({"pos_2": 0.5}), base_out)
        assert not np.allclose(changed({"interp_mode": "Smooth"}), base_out)
        assert not np.allclose(changed({"amount": 0.5}), base_out)
        assert not np.allclose(changed({"stop_1": (0.5, 0.0, 0.0)}), base_out)
        assert not np.allclose(changed({"stop_2": (0.0, 0.5, 0.0)}), base_out)
        # 端点停靠在 [0,1] 内也有影响（端点色即 0/1 亮度输出）
        assert not np.allclose(changed({"stop_0": (0.0, 0.0, 0.0)}), base_out)
        assert not np.allclose(changed({"stop_3": (1.0, 1.0, 1.0)}), base_out)

    def test_equal_positions_guarded(self) -> None:
        # 等位置不除零，输出有限
        out = ref.evaluate(
            np.array([[0.5, 0.5, 0.5]], dtype=np.float32),
            {"pos_1": 0.5, "pos_2": 0.5})
        assert np.all(np.isfinite(out))


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "gradient_map")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "gradient_map"
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
            if mp["ptype"] == "color":
                got = tuple(round(float(v), 4) for v in pp["default"])
                assert got == tuple(round(float(v), 4) for v in mp["default"]), name
            elif mp["ptype"] == "option":
                assert pp["options"] == list(mp["options"]), name
                # .tglsl ABI 层 select 默认值是 int 索引；catalog 转 ParamDef 时
                # 才投影为 label 字符串（manifest 采用 label 口径）
                assert pp["default"] == list(mp["options"]).index(mp["default"]), name
            else:
                assert float(pp["default"]) == float(mp["default"]), name
                assert float(pp["min"]) == float(mp["minimum"]), name
                assert float(pp["max"]) == float(mp["maximum"]), name

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        assert "while" not in body, "禁止 while 循环"
        # 循环上限常量红线：本插件零循环，出现 for 即违规
        assert not re.search(r"\bfor\s*\(", body), "零采样点处理不应有循环"
        # 未声明 u_* 红线：仅引擎三注入白名单
        for m in re.finditer(r"\bu_\w+", body):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, \
                f"未声明 uniform {m.group(0)}"
        # 数字字面量浮点语境（粗检：禁 int 字面量乘除 vec/float —— 由真实编译门禁兜底）
        assert "3.0 - 2.0" in body  # smoothstep 展开为浮点


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
