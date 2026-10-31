"""F01 wb_adaptation 测试：oracle↔手算向量、恒等/HDR/白点对拍、
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

from content_staging.library_v1.references import wb_adaptation_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "wb_adaptation"
TGLSL_PATH = COMPONENT_DIR / "wb_adaptation.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

WARM = {"source_white": (1.0, 0.85, 0.65), "target_white": (1.0, 1.0, 1.0),
        "adapt_model": "Bradford", "strength": 1.0}


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
    def test_equal_whites_exact_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"source_white": (1.0, 0.9, 0.75),
                                 "target_white": (1.0, 0.9, 0.75),
                                 "strength": 1.0})
        np.testing.assert_array_equal(out, img)

    def test_strength_zero_exact_identity(self) -> None:
        img = np.array([[12.0, 0.1, 4.5]], dtype=np.float32)
        out = ref.evaluate(img, dict(WARM, strength=0.0))
        np.testing.assert_array_equal(out, img)

    def test_default_params_identity(self) -> None:
        img = np.array([[0.0, 0.18, 4.5], [1.0, 0.0, 0.0]], dtype=np.float32)
        out = ref.evaluate(img)
        np.testing.assert_array_equal(out, img)

    def test_white_maps_to_white(self) -> None:
        # 计划 §4 F01 验收点：中性灰/参考变换对拍——白点输入精确映射到
        # target 白点色度（XYZ 比例相等，两模型皆成立）
        for model in ("Bradford", "XYZ Scaling"):
            params = dict(WARM, adapt_model=model)
            out = ref.evaluate(np.array([[1.0, 0.85, 0.65]], dtype=np.float32), params)[0]
            out = out.astype(np.float64)
            oxyz = ref.RGB2XYZ @ out
            wt = ref.white_xyz((1.0, 1.0, 1.0))
            np.testing.assert_allclose(oxyz / oxyz[1], wt / wt[1], atol=1e-6)

    def test_warm_source_gray_compensates_cool(self) -> None:
        # 暖白光源下的中灰 → 补偿偏冷（B>R），方向性锚点
        out = ref.evaluate(np.array([[0.18, 0.18, 0.18]], dtype=np.float32), WARM)[0]
        assert out[2] > out[0]
        # 方向互换：D65→暖白，中灰变暖
        swap = {"source_white": (1.0, 1.0, 1.0), "target_white": (1.0, 0.85, 0.65),
                "adapt_model": "Bradford", "strength": 1.0}
        out2 = ref.evaluate(np.array([[0.18, 0.18, 0.18]], dtype=np.float32), swap)[0]
        assert out2[0] > out2[2]

    def test_hdr_linear_extrapolation(self) -> None:
        # 齐次性：同一变换下 out(4.5·g) == 4.5·out(g)（线性、无 clamp）
        gray = np.array([[0.18, 0.18, 0.18]], dtype=np.float32)
        big = gray * 25.0
        o1 = ref.evaluate(gray, WARM)[0]
        o2 = ref.evaluate(big, WARM)[0]
        np.testing.assert_allclose(o2, o1 * 25.0, rtol=1e-6, atol=1e-6)

    def test_models_distinguishable(self) -> None:
        img = np.array([[12.0, 0.1, 4.5]], dtype=np.float32)
        b = ref.evaluate(img, WARM)
        x = ref.evaluate(img, dict(WARM, adapt_model="XYZ Scaling"))
        assert not np.allclose(b, x, atol=1e-4)

    def test_nonphysical_white_falls_back_d65(self) -> None:
        # 黑/负和白点（ΣXYZ<=0）走同一 D65 回退：两种非物理输入输出逐位一致；
        # 回退 D65 与 target=(1,1,1) 的推导 xy 仅差固定矩阵常数舍入（<1e-3）
        img = np.array([[0.18, 0.18, 0.18]], dtype=np.float32)
        black = ref.evaluate(img, dict(WARM, source_white=(0.0, 0.0, 0.0)))[0]
        negblack = ref.evaluate(img, dict(WARM, source_white=(-1.0, -1.0, -1.0)))[0]
        np.testing.assert_array_equal(black, negblack)
        assert float(np.max(np.abs(black - img[0]))) < 1e-3

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import color_patches
        base = color_patches()
        base_out = ref.evaluate(base, WARM)

        def changed(params: dict) -> np.ndarray:
            merged = dict(WARM)
            merged.update(params)
            return ref.evaluate(base, merged)

        assert not np.allclose(changed({"source_white": (0.9, 0.9, 0.9)}), base_out)
        assert not np.allclose(changed({"target_white": (1.0, 0.9, 0.8)}), base_out)
        assert not np.allclose(changed({"adapt_model": "XYZ Scaling"}), base_out)
        assert not np.allclose(changed({"strength": 0.5}), base_out)


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "wb_adaptation")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "wb_adaptation"
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
                # .tglsl ABI 层 select 默认值是 int 索引；manifest 采用 label 口径
                assert pp["default"] == list(mp["options"]).index(mp["default"]), name
            else:
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
        # 参数名不得命中 GLSL 内建/保留（AUD-0832 黑名单类）
        for bad in ("mix ", "max ", "min ", "step "):
            assert f"@param slider {bad.strip()} " not in src
        assert "rt.x / rs.x" in body  # 对角缩放为浮点除


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
