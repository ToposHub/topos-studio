"""F02 subtractive_density 测试：oracle↔手算向量、恒等/HDR/亮度补偿、
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

from content_staging.library_v1.references import subtractive_density_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "subtractive_density"
TGLSL_PATH = COMPONENT_DIR / "subtractive_density.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
BASE = {"density": 1.0, "dye_coupling": 0.0, "luma_protection": 0.0, "amount": 1.0}


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
    def test_density_zero_exact_identity_any_other_params(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"density": 0.0, "dye_coupling": 1.0,
                                 "luma_protection": 1.0, "amount": 1.0})
        np.testing.assert_array_equal(out, img)

    def test_amount_zero_exact_identity(self) -> None:
        img = np.array([[12.0, 0.1, 4.5]], dtype=np.float32)
        out = ref.evaluate(img, dict(BASE, amount=0.0))
        np.testing.assert_array_equal(out, img)

    def test_default_params_identity(self) -> None:
        img = np.array([[0.0, 0.18, 4.5], [1.0, 0.0, 0.0]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img), img)

    def test_multiplicative_hdr_extrapolation(self) -> None:
        # 乘性模型：out(c·s) = out(c)·s（τ 与内容无关），含负缩放
        c = np.array([[0.30, 0.20, 0.10]], dtype=np.float32)
        o1 = ref.evaluate(c, BASE)[0]
        o2 = ref.evaluate(c * 20.0, BASE)[0]
        np.testing.assert_allclose(o2, o1 * 20.0, rtol=1e-6, atol=1e-6)

    def test_luma_protection_exact(self) -> None:
        # p=1：dot(out,W) 精确回到 dot(in,W)（dot(W,W) 除数补偿）
        rng_inputs = np.array([[0.30, 0.20, 0.10], [1.0, 0.0, 0.0],
                               [12.0, 0.1, 4.5], [0.18, 0.18, 0.18]], dtype=np.float32)
        out = ref.evaluate(rng_inputs, dict(BASE, dye_coupling=1.0, luma_protection=1.0))
        luma_in = rng_inputs.astype(np.float64) @ LUMA
        luma_out = out.astype(np.float64) @ LUMA
        np.testing.assert_allclose(luma_out, luma_in, rtol=0, atol=2e-7)

    def test_luma_protection_partial_between_extremes(self) -> None:
        # p∈(0,1) 的亮度落在 p=0 与 p=1 之间（补偿单调）
        img = np.array([[0.30, 0.20, 0.10]], dtype=np.float32)
        l0 = float(ref.evaluate(img, BASE)[0] @ LUMA)
        l1 = float(ref.evaluate(img, dict(BASE, luma_protection=1.0))[0] @ LUMA)
        lh = float(ref.evaluate(img, dict(BASE, luma_protection=0.5))[0] @ LUMA)
        assert min(l0, l1) - 1e-6 <= lh <= max(l0, l1) + 1e-6
        assert abs(lh - (l0 + l1) / 2) < 1e-6  # 线性插值

    def test_monotone_in_density(self) -> None:
        # 密度单调加深（luma 随 density 单调不增），负密度提亮
        img = np.array([[0.4, 0.3, 0.2]], dtype=np.float32)
        luma = [float(ref.evaluate(img, dict(BASE, density=d))[0] @ LUMA)
                for d in (-0.5, 0.0, 0.5, 1.0, 2.0)]
        assert all(luma[i] >= luma[i + 1] - 1e-7 for i in range(len(luma) - 1)), luma
        assert luma[0] > float(img[0] @ LUMA)  # 负密度提亮

    def test_crosstalk_redistributes_absorption(self) -> None:
        # k 增大时透射率重分配：D_R=C·δ 行 0 吸收上升（τ_R 降），
        # D_B 行吸收下降（τ_B 升）——非均匀重排，非整体变暗/变亮
        tau_k0 = ref.transmission(1.0, 0.0)
        tau_k1 = ref.transmission(1.0, 1.0)
        assert tau_k1[0] < tau_k0[0]
        assert tau_k1[2] > tau_k0[2]

    def test_coupling_changes_channel_ratios(self) -> None:
        # 串扰作用于密度域：同一彩色输入在 k=0/k=1 的每通道缩放比不同
        # （若只是整体乘性则比值恒定——串扰必须改通道间比例）
        img = np.array([[1.0, 0.5, 0.3]], dtype=np.float32)
        out_k0 = ref.evaluate(img, BASE)[0]
        out_k1 = ref.evaluate(img, dict(BASE, dye_coupling=1.0))[0]
        ratios = out_k1 / out_k0
        assert float(ratios.max() - ratios.min()) > 1e-3
        # 纯原色输入的零通道仍为零（密度域串扰不产生光：0·τ=0）
        red = np.array([[1.0, 0.0, 0.0]], dtype=np.float32)
        out_red = ref.evaluate(red, dict(BASE, dye_coupling=1.0))[0]
        assert out_red[1] == 0.0 and out_red[2] == 0.0

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import color_patches
        base = color_patches()
        base_out = ref.evaluate(base, BASE)

        def changed(params: dict) -> np.ndarray:
            merged = dict(BASE)
            merged.update(params)
            return ref.evaluate(base, merged)

        assert not np.allclose(changed({"density": 0.6}), base_out)
        assert not np.allclose(changed({"dye_coupling": 1.0}), base_out)
        assert not np.allclose(changed({"luma_protection": 1.0}), base_out)
        assert not np.allclose(changed({"amount": 0.5}), base_out)


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "subtractive_density")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "subtractive_density"
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
        # 数字字面量浮点语境（粗检：矩阵/向量常量全带 .0/.88 等小数位）
        assert "1.0 - k + k * 0.88" in body
        assert "/ dot(SD_LUMA_W, SD_LUMA_W)" in body  # 精确补偿分母存在


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
