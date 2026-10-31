"""F19 median_rank 试点测试：oracle↔手算、恒等/flat/滑杆有效性、
真实 parser、GLSL 红线（循环上限常量）。"""

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

from content_staging.library_v1.references import median_rank_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "median_rank"
TGLSL_PATH = COMPONENT_DIR / "median_rank.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _gradient_5x5() -> np.ndarray:
    g = np.arange(25, dtype=np.float64).reshape(5, 5) / 24.0
    return np.repeat(g[:, :, None], 3, axis=1).reshape(5, 5, 3).astype(np.float32)


def _at(out: np.ndarray, pos: list[int]) -> float:
    return float(out[pos[1], pos[0], 0])


class TestHandVectors:
    def test_main_vectors(self) -> None:
        img = _gradient_5x5()
        for vec in HAND["vectors"]:
            out = ref.evaluate(img, vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['note']}")

    def test_luma_mode_vector(self) -> None:
        # 3x3 灰 patch（行主序 0.1..0.9，中心像素置 0.1）
        vals = np.array([[0.1, 0.2, 0.3], [0.4, 0.1, 0.6], [0.7, 0.8, 0.9]])
        img = np.repeat(vals[..., None], 3, axis=2).astype(np.float32)
        out = ref.evaluate(img, {"channel_mode": "Luma"})
        # 中心窗口=全图，luma 排序 [0.1,0.1,0.2,0.3,0.4,0.6,0.7,0.8,0.9]
        #（0.1 出现两次）→ med_l=0.4；中心 luma=0.1 → out=0.1*(0.4/0.1)=0.4
        np.testing.assert_allclose(out[1, 1, 0], 0.4, atol=1e-6)
        # RGB 三通道同值 → Luma 档输出仍为灰
        np.testing.assert_allclose(out[1, 1, 0], out[1, 1, 1], atol=1e-7)


class TestSemantics:
    def test_mix_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"amount": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_flat_field_identity_all_modes(self) -> None:
        flat = np.full((6, 6, 3), 0.37, dtype=np.float32)
        for mode in ("RGB", "Luma"):
            for kern in ("3x3", "5x5"):
                out = ref.evaluate(flat, {"channel_mode": mode, "kernel_size": kern})
                np.testing.assert_allclose(out, flat, atol=1e-7)

    def test_salt_pepper_removal(self) -> None:
        # 中值滤波经典语义：孤立脉冲被邻域中值替换（bilateral/blur 会残留扩散）
        img = np.full((5, 5, 3), 0.2, dtype=np.float32)
        img[2, 2] = 8.0  # HDR 脉冲
        out = ref.evaluate(img)
        assert float(out[2, 2, 0]) == pytest.approx(0.2, abs=1e-6)
        # 周围像素窗口含脉冲：9 样本排序 [0.2×8, 8.0] 中值仍 0.2
        np.testing.assert_allclose(out, 0.2, atol=1e-6)

    def test_percentile_extremes(self) -> None:
        img = _gradient_5x5()
        p0 = ref.evaluate(img, {"percentile": 0.0})
        p1 = ref.evaluate(img, {"percentile": 1.0})
        assert _at(p0, [2, 2]) < _at(ref.evaluate(img), [2, 2]) < _at(p1, [2, 2])

    def test_kernel_and_mode_params_have_effect(self) -> None:
        img = _gradient_5x5()
        base = ref.evaluate(img)
        assert not np.allclose(ref.evaluate(img, {"kernel_size": "5x5"}), base)
        assert not np.allclose(ref.evaluate(img, {"percentile": 0.3}), base)
        # Luma 档在灰图上与 RGB 等价（灰输入色度比例=1）——用彩色输入验证差量
        color_img = np.zeros((5, 5, 3), dtype=np.float32)
        color_img[..., 0] = _gradient_5x5()[:, :, 0]
        color_img[..., 1] = _gradient_5x5()[:, :, 0] * 0.5
        base_c = ref.evaluate(color_img)
        luma_c = ref.evaluate(color_img, {"channel_mode": "Luma"})
        assert not np.allclose(base_c, luma_c), "彩色输入上 Luma 档必须区别于 RGB 档"


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "median_rank")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "median_rank"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        # preview_cost 是 parser 顶层字段（不入 capabilities dict）
        assert parsed["preview_cost"] == "medium"

    def test_params_match_manifest(self, parsed: dict) -> None:
        manifest = json.loads(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        manifest_names = {p["name"] for p in manifest["params"]}
        parsed_names = {p["var_name"] for p in parsed["params"]}
        assert manifest_names == parsed_names

    def test_glsl_red_lines(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only
        # 循环上限必须编译期常量：允许的 for 形态仅 dy/dx 有界常量循环与秩计数循环
        for m in re.finditer(r"for\s*\(\s*int\s+(\w+)\s*=\s*(-?\d+)\s*;\s*\1\s*(<=|<)\s*(\w+|\d+)", code_only):
            bound = m.group(4)
            assert bound.isdigit(), f"非常量循环上限: {m.group(0)}"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
