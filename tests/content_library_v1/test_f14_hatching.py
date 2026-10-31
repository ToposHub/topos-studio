"""F14 hatching 测试：oracle↔独立手算向量、恒等/HDR/参数有效性、
平坦灰阶排线（区别 Edge_Ink_Outline）、层数/角度差量、真实 parser、
GLSL 红线（零循环零采样）。"""

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

from content_staging.library_v1.references import hatching_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "hatching"
TGLSL_PATH = COMPONENT_DIR / "hatching.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _uniform(value: float, size: int = 8) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.float32)


def _fixture(name: str) -> np.ndarray:
    if name == "flatwhite_8x8":
        return _uniform(1.0)
    if name == "flatblack_8x8":
        return _uniform(0.0)
    if name == "flathdr12_8x8":
        return _uniform(12.0)
    if name == "flatneg_8x8":
        return _uniform(-0.25)
    if name == "flat018_8x8_s3a0":
        return _uniform(0.18)
    if name == "flat06_16x16":
        return _uniform(0.6, size=16)
    if name == "vgrad16":
        return np.tile((np.arange(16) / 15.0).astype(np.float32)[:, None, None], (1, 16, 3))
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
                continue
            out = ref.evaluate(_fixture(vec["fixture"]), vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['desc']}")


class TestSemantics:
    def test_amount_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        np.testing.assert_array_equal(ref.evaluate(img, {"amount": 0.0}), img)

    def test_flat_gray_produces_hatching(self) -> None:
        # 核心差量钉住：平坦灰阶也产生排线（Edge_Ink_Outline 在平坦输入恒等）
        flat = _uniform(0.5, size=32)
        out = ref.evaluate(flat)
        assert not np.allclose(out, flat), "平坦灰阶必须产生排线"
        # 空间周期性条带：输出沿 x 有方差（非平坦）
        assert float(out[..., 0].std(axis=1).mean()) > 1e-3
        # 墨量有界域：输出在 paper 与 ink 之间
        assert out.min() >= 0.05 - 1e-6
        assert out.max() <= 0.9 + 1e-6

    def test_layer_count_difference(self) -> None:
        # 层数差量：正交第二层在层 1 露白位补充墨量
        img = _fixture("flat06_16x16")
        l1 = ref.evaluate(img, {"angle": 0.0, "layers": "1"})
        l2 = ref.evaluate(img, {"angle": 0.0, "layers": "2"})
        assert not np.allclose(l1, l2)
        # 概率并：两层覆盖 ≥ 单层
        assert np.all(l2[..., 0] <= l1[..., 0] + 1e-7)

    def test_params_have_effect(self) -> None:
        img = _fixture("vgrad16")
        base = ref.evaluate(img)
        for changed in ({"angle": 0.0}, {"line_spacing": 3.0},
                        {"ink_color": (0.6, 0.1, 0.1)}, {"paper": 0.3},
                        {"amount": 0.5}, {"layers": "1"}):
            assert not np.allclose(ref.evaluate(img, changed), base), changed

    def test_darker_means_more_ink(self) -> None:
        # 密度单调：整图越暗，平均输出亮度越低（墨越多）
        outs = [float(ref.evaluate(_uniform(v, size=32)).mean()) for v in (1.0, 0.75, 0.5, 0.25, 0.0)]
        assert all(outs[i] > outs[i + 1] for i in range(len(outs) - 1))

    def test_spacing_changes_band_frequency(self) -> None:
        # spacing 越小条带频率越高：输出沿 x 的过零次数增加
        flat = _uniform(0.5, size=64)
        col = ref.evaluate(flat, {"angle": 0.0, "line_spacing": 24.0})[0, :, 0]
        col_s = ref.evaluate(flat, {"angle": 0.0, "line_spacing": 4.0})[0, :, 0]
        crossings = lambda a: int(np.sum(np.diff(np.sign(np.diff(a))) != 0))
        assert crossings(col_s) > crossings(col)

    def test_finite_on_mixed_hdr(self) -> None:
        img = np.array(
            [[[12.0, -0.25, 4.5], [0.18, 1.0, 0.0], [8.0, 8.0, 8.0]]], dtype=np.float32)
        assert np.all(np.isfinite(ref.evaluate(img)))


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "hatching")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "hatching"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        assert parsed["preview_cost"] == "low"
        layers = {p["var_name"]: p for p in parsed["params"]}["layers"]
        assert layers["options"] == ["1", "2"]
        assert layers["default"] == 1

    def test_params_match_manifest(self, parsed: dict) -> None:
        manifest = json.loads(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        manifest_names = {p["name"] for p in manifest["params"]}
        parsed_names = {p["var_name"] for p in parsed["params"]}
        assert manifest_names == parsed_names

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
        # 程序几何：零循环
        assert not re.search(r"\bfor\s*\(", code_only), "程序条带不应有循环"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
