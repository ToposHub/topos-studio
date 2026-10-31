"""F12 palette_dither 测试：oracle↔手算向量（含像素坐标）、恒等/HDR/显示域策略、
Bayer 周期与翻转、真实 parser 解析（显式暂存路径）、GLSL 红线静态检查。"""

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

from content_staging.library_v1.references import palette_dither_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "palette_dither"
TGLSL_PATH = COMPONENT_DIR / "palette_dither.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

BASE = {"dither_mode": "None", "strength": 1.0}
PALETTE = [tuple(v) for v in
           (ref.DEFAULTS[f"palette_{i}"] for i in range(4))]


def ev(pixels_xy, image, params):
    return ref.evaluate(np.asarray(image, dtype=np.float32),
                        {**BASE, **params}, np.asarray(pixels_xy, dtype=np.float64))


class TestHandVectors:
    def test_main_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["vectors"]:
            px = [vec.get("pixel", [0, 0])]
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32),
                               HAND_VECTORS["params"], np.array(px, dtype=np.float64))[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"input={vec['input']} note={vec['note']}")

    def test_variant_vectors_match_oracle(self) -> None:
        for vec in HAND_VECTORS["variant_vectors"]:
            params = dict(HAND_VECTORS["params"])
            params.update(vec["params"])
            px = [vec.get("pixel", [0, 0])]
            out = ref.evaluate(np.array([vec["input"]], dtype=np.float32),
                               params, np.array(px, dtype=np.float64))[0]
            np.testing.assert_allclose(
                out, np.float32(vec["expected"]), rtol=0, atol=1e-6,
                err_msg=f"params={vec['params']} pixel={px} input={vec['input']}")


class TestSemantics:
    def test_strength_zero_exact_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"strength": 0.0, "dither_mode": "Ordered 4x4"})
        np.testing.assert_array_equal(out, img)

    def test_full_strength_output_is_palette_color(self) -> None:
        # strength=1：输出必为某调色板色（含 HDR/负值输入——显示域查找）
        img = np.array([[4.5, 4.5, 4.5], [12.0, 0.1, 4.5], [-0.25, -0.1, -0.05],
                        [0.5, 0.5, 0.5]], dtype=np.float32)
        out = ref.evaluate(img, BASE)
        for row in out:
            assert any(np.allclose(row, p, atol=1e-7) for p in PALETTE), row

    def test_partial_strength_retains_hdr(self) -> None:
        # 显示域策略只作用查找域：strength=0.5 输出=与原值混合，超白残留
        img = np.array([[12.0, 0.1, 4.5]], dtype=np.float32)
        out = ref.evaluate(img, {"strength": 0.5, "dither_mode": "None"})[0]
        assert out[0] > 1.0 and out[2] > 1.0  # R/B 通道超白残留
        nearest = max(PALETTE, key=lambda p: p[0])
        np.testing.assert_allclose(out, img[0] + (np.float32(nearest) - img[0]) * 0.5, atol=1e-6)

    def test_dither_none_position_independent(self) -> None:
        img = np.array([[0.62, 0.31, 0.16]], dtype=np.float32)
        for x, y in ((0, 0), (3, 7), (100, 55), (7, 3)):
            out = ev([[x, y]], img, {"dither_mode": "None"})
            np.testing.assert_array_equal(out[0], ev([[0, 0]], img, {"dither_mode": "None"})[0])

    def test_bayer_period_and_flips(self) -> None:
        # 周期 4：x 与 x+4 同相位同输出；同输入 4 列内出现 ≥2 个不同调色板色
        img = np.array([[0.62, 0.31, 0.16]] * 8, dtype=np.float32)
        xs = np.arange(8)
        out = ev(np.stack([np.zeros(8, dtype=int), xs], axis=-1), img,
                 {"dither_mode": "Ordered 4x4"})
        for i in range(4):
            np.testing.assert_array_equal(out[i], out[i + 4])
        distinct = {tuple(np.round(v, 6)) for v in out}
        assert len(distinct) >= 2, distinct  # 抖动翻转（手算：px0/2→p1，px1→p2）

    def test_bayer_grid_matches_reference_matrix(self) -> None:
        # 相位查表与 4x4 Bayer 矩阵逐位一致（BAYER4 常数纪律）
        bayer = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
        yy, xx = np.meshgrid(np.arange(4), np.arange(4), indexing="ij")
        phase = ref.bayer_phase(np.stack([yy, xx], axis=-1))
        expect = (np.array(bayer, dtype=np.float64) + 0.5) / 16.0 - 0.5
        np.testing.assert_allclose(phase, expect, atol=0)

    def test_degenerate_palette_no_nan(self) -> None:
        # 调色板全同色：Δ=0，抖动自动失效，输出有限且为该色
        img = np.array([[0.5, 0.5, 0.5]], dtype=np.float32)
        out = ev([[0, 0]], img, {"dither_mode": "Ordered 4x4",
                                 "palette_0": (0.4, 0.4, 0.4),
                                 "palette_1": (0.4, 0.4, 0.4),
                                 "palette_2": (0.4, 0.4, 0.4),
                                 "palette_3": (0.4, 0.4, 0.4)})[0]
        assert np.all(np.isfinite(out))
        np.testing.assert_allclose(out, [0.4, 0.4, 0.4], atol=1e-7)

    def test_tie_breaks_lowest_index(self) -> None:
        # 平局取低索引：probe 恰好在两色中点时选先声明者
        img = np.array([[0.19, 0.075, 0.11]], dtype=np.float32)  # p0/p1 中点
        out = ev([[0, 0]], img, {"dither_mode": "None"})[0]
        np.testing.assert_allclose(out, PALETTE[0], atol=1e-7)

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import color_patches
        # 全 16 块（肤色簇+原色+次色+中性含白块，保证 p3 可被命中）
        base = color_patches()
        base_out = ref.evaluate(base, dict(BASE, dither_mode="Ordered 4x4"))

        def changed(params: dict) -> np.ndarray:
            merged = dict(BASE, dither_mode="Ordered 4x4")
            merged.update(params)
            return ref.evaluate(base, merged)

        for name, params in {
            "palette_0": {"palette_0": (0.0, 0.0, 0.0)},
            "palette_1": {"palette_1": (0.5, 0.0, 0.1)},
            "palette_2": {"palette_2": (0.3, 0.3, 0.3)},
            "palette_3": {"palette_3": (1.0, 1.0, 0.5)},
            "dither_mode": {"dither_mode": "None"},
            "strength": {"strength": 0.5},
        }.items():
            assert not np.allclose(changed(params), base_out), f"{name} 无像素影响"


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "palette_dither")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "palette_dither"
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
                assert pp["default"] == list(mp["options"]).index(mp["default"]), name
            else:
                assert float(pp["default"]) == float(mp["default"]), name
                assert float(pp["min"]) == float(mp["minimum"]), name
                assert float(pp["max"]) == float(mp["maximum"]), name

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        assert "while" not in body, "禁止 while 循环"
        # 最近色 4 路展开：不得有循环
        assert not re.search(r"\bfor\s*\(", body), "4 路展开不应有循环"
        for m in re.finditer(r"\bu_\w+", body):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, \
                f"未声明 uniform {m.group(0)}"
        assert "u_tex_size" in body      # 像素坐标来源（引擎注入）
        assert "clamp(color, 0.0, 1.0)" in body  # 显示域策略显式存在
        # Bayer 矩阵与 Pixel_Blocks 同源（4x4 整数标度）
        assert "int[16](" in body and "(float(PD_BAYER4[iy * 4 + ix]) + 0.5) / 16.0 - 0.5" in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
