"""F04 zoned_tint 测试：oracle↔手算向量、恒等/HDR/分区连续、
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

from content_staging.library_v1.references import zoned_tint_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "zoned_tint"
TGLSL_PATH = COMPONENT_DIR / "zoned_tint.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
BASE = {"shadow_color": (0.38, 0.46, 0.60), "midtone_color": (0.5, 0.5, 0.5),
        "highlight_color": (0.60, 0.53, 0.42),
        "shadow_strength": 1.0, "midtone_strength": 0.0, "highlight_strength": 1.0,
        "shadow_pivot": 0.25, "highlight_pivot": 0.75,
        "shadow_width": 0.30, "highlight_width": 0.30}


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
    def test_neutral_tints_exact_identity_including_hdr(self) -> None:
        # tint 全 0.5 灰（默认）：0.5*2.0=1.0 无舍入，乘法路径精确恒等
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img), img)

    def test_zero_strengths_exact_identity(self) -> None:
        img = np.array([[4.5, -0.25, 12.0]], dtype=np.float32)
        out = ref.evaluate(img, dict(BASE, shadow_strength=0.0,
                                     midtone_strength=0.0, highlight_strength=0.0))
        np.testing.assert_array_equal(out, img)

    def test_partition_of_unity_at_default_pivots(self) -> None:
        # 默认设置下三区权重和恒 1（两过渡带不重叠）
        t = np.linspace(0.0, 1.0, 1001)
        w_s, w_m, w_h = ref.zone_weights(t, 0.25, 0.75, 0.30, 0.30)
        np.testing.assert_allclose(w_s + w_m + w_h, 1.0, rtol=0, atol=1e-12)
        # 单调性：阴影权重随 t 降，高光升
        assert np.all(np.diff(w_s) <= 1e-12)
        assert np.all(np.diff(w_h) >= -1e-12)

    def test_zone_separation_shadow_vs_highlight(self) -> None:
        # 深灰只吃 shadow 冷 tint，超白只吃 highlight 暖 tint（分区有效）
        deep = ref.evaluate(np.array([[0.02, 0.02, 0.02]], dtype=np.float32), BASE)[0]
        bright = ref.evaluate(np.array([[8.0, 8.0, 8.0]], dtype=np.float32), BASE)[0]
        assert deep[2] > deep[0]   # 阴影蓝 tint
        assert bright[0] > bright[2]  # 高光暖 tint

    def test_continuity_no_hard_edges(self) -> None:
        # 计划 §4 F04 验收点：分区连续——灰梯度相邻输出无跳变。
        # 阈值口径：ramp 步长 1/63≈0.0159（线性），叠 smoothstep 最大斜率
        # (1.5/width)·|tint·2-1|·strength 的增益变化 → 相邻差上界 ≈0.021；
        # 真硬边（width→0 的阶跃）会在单步跳 ≈0.1-0.2 量级
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        ramp = gray_ramp_linear()
        out = ref.evaluate(ramp, dict(BASE, midtone_strength=0.6,
                                      midtone_color=(0.46, 0.56, 0.50)))
        diff = np.abs(np.diff(out.astype(np.float64), axis=1))
        assert float(diff.max()) < 0.03, f"相邻跳变过大: {diff.max()}"

    def test_pivots_and_widths_move_the_zones(self) -> None:
        # 分界/交叉宽度滑杆真实改变分区（验收：不能仅复制固定三段）
        gray = np.array([[0.18, 0.18, 0.18]], dtype=np.float32)   # t=0.5
        out_default = ref.evaluate(gray, BASE)[0]
        out_sp_hi = ref.evaluate(gray, dict(BASE, shadow_pivot=0.40))[0]
        assert not np.allclose(out_default, out_sp_hi, atol=1e-4)
        dark = np.array([[0.03, 0.03, 0.03]], dtype=np.float32)   # t≈0.1429
        out_w0 = ref.evaluate(dark, dict(BASE, shadow_width=0.0))[0]
        out_w5 = ref.evaluate(dark, dict(BASE, shadow_width=0.5))[0]
        assert not np.allclose(out_w0, out_w5, atol=1e-4)

    def test_negative_luma_full_shadow(self) -> None:
        # 负 luma 按纯阴影分区（t=0），颜色本身不被 clamp
        neg = np.array([[-0.6, -0.5, -0.4]], dtype=np.float32)
        out = ref.evaluate(neg, BASE)[0]
        expect_gain = 1.0 + (np.array([0.38, 0.46, 0.60]) * 2.0 - 1.0) * 1.0
        np.testing.assert_allclose(out, neg[0] * expect_gain, atol=1e-6)

    def test_hdr_saturated_zone_gain_and_multiplicativity(self) -> None:
        # HDR 策略：t=l/(l+0.18) 随亮度饱和——luma>=1.62（t>=0.9）进入
        # 纯高光区，增益收敛到 highlight-only 并在该区内精确乘性
        g = np.array([[8.0, 8.0, 8.0]], dtype=np.float32)
        out = ref.evaluate(g, BASE)[0]
        np.testing.assert_allclose(out, g[0] * np.array([1.2, 1.06, 0.84]), atol=1e-6)
        o_scaled = ref.evaluate(g * 10.0, BASE)[0]
        np.testing.assert_allclose(o_scaled, out * 10.0, rtol=1e-6, atol=1e-6)

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import gray_ramp_linear
        base = gray_ramp_linear()
        param_base = dict(BASE, midtone_strength=0.6, midtone_color=(0.46, 0.56, 0.50))
        base_out = ref.evaluate(base, param_base)

        def changed(params: dict) -> np.ndarray:
            merged = dict(param_base)
            merged.update(params)
            return ref.evaluate(base, merged)

        for name, params in {
            "shadow_color": {"shadow_color": (0.2, 0.3, 0.7)},
            "midtone_color": {"midtone_color": (0.7, 0.6, 0.3)},
            "highlight_color": {"highlight_color": (0.7, 0.5, 0.3)},
            "shadow_strength": {"shadow_strength": 0.4},
            "midtone_strength": {"midtone_strength": 0.2},
            "highlight_strength": {"highlight_strength": 0.4},
            "shadow_pivot": {"shadow_pivot": 0.15},
            "highlight_pivot": {"highlight_pivot": 0.65},
            "shadow_width": {"shadow_width": 0.1},
            "highlight_width": {"highlight_width": 0.45},
        }.items():
            assert not np.allclose(changed(params), base_out), f"{name} 无像素影响"


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "zoned_tint")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "zoned_tint"
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
        # 可动分界/宽度与早退存在（浮点语境）
        assert "smoothstep(shadow_pivot - 0.5 * sw, shadow_pivot + 0.5 * sw, t)" in body
        assert "max(shadow_width, 1e-4)" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
