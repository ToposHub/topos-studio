"""F06 multiscale_halation 测试：oracle↔独立手算向量、恒等/HDR/滑杆有效性、
多尺度差量（vs 单尺度降级）、真实 parser、GLSL 红线（循环上限常量）。"""

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

from content_staging.library_v1.references import multiscale_halation_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "multiscale_halation"
TGLSL_PATH = COMPONENT_DIR / "multiscale_halation.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def _uniform(value: float, size: int = 8) -> np.ndarray:
    return np.full((size, size, 3), value, dtype=np.float32)


def _fixture(name: str) -> np.ndarray:
    if name == "uniform12_8x8":
        return _uniform(12.0)
    if name == "uniform03_8x8":
        return _uniform(0.3)
    if name == "uniform20_8x8":
        return _uniform(2.0)
    if name == "uniform0725_8x8":
        return _uniform(0.725)
    if name == "point33":
        img = _uniform(0.02, size=33)
        img[16, 16] = 2.0
        return img
    if name == "point129":
        img = _uniform(0.02, size=129)
        img[64, 64] = 2.0
        return img
    if name == "point07_33":
        # 中间调点光源（luma 0.7 落在默认软膝过渡带内）：threshold 有真实影响
        img = _uniform(0.02, size=33)
        img[16, 16] = 0.7
        return img
    if name == "cornerblock16":
        img = _uniform(0.02, size=16)
        img[0:4, 0:4] = 2.0
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
            if vec["params"].get("strength", 1.0) == 0.0:
                continue  # 恒等变体由 TestSemantics 覆盖
            fn = (ref.evaluate_degraded_single_scale
                  if vec.get("entry") == "degraded_single_scale" else ref.evaluate)
            out = fn(_fixture(vec["fixture"]), vec["params"])
            np.testing.assert_allclose(
                _at(out, vec["pos"]), vec["expected"], atol=1e-6,
                err_msg=f"{vec['desc']}")


class TestSemantics:
    def test_strength_zero_identity_including_hdr(self) -> None:
        img = np.array([[[0.0, 0.18, 12.0], [4.5, -0.25, 1.0]]], dtype=np.float32)
        out = ref.evaluate(img, {"strength": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_below_threshold_flat_identity(self) -> None:
        # 阈下均匀场：m=0 → halo=0 → 精确恒等（strength=1 亦然）
        flat = _uniform(0.3, size=8)
        out = ref.evaluate(flat, {"strength": 1.0})
        np.testing.assert_array_equal(out, flat)

    def test_finite_on_mixed_hdr(self) -> None:
        img = np.array(
            [[[12.0, -0.25, 4.5], [0.18, 1.0, 0.0], [8.0, 8.0, 8.0]]], dtype=np.float32)
        out = ref.evaluate(img)
        assert np.all(np.isfinite(out))

    def test_params_have_effect(self) -> None:
        img = _fixture("point33")
        base = ref.evaluate(img)
        for changed in ({"red_scatter": 0.0}, {"strength": 1.0}):
            assert not np.allclose(ref.evaluate(img, changed), base), changed
        # threshold：需要夹具含软膝过渡带内亮度（0.02/2.0 双点夹具上
        # 0.55 与 0.8 的掩码同为 0/1——threshold 不起作用是正确行为）
        img_knee = _fixture("point07_33")
        base_knee = ref.evaluate(img_knee)
        assert not np.allclose(
            ref.evaluate(img_knee, {"threshold": 0.8}), base_knee), "threshold 必须在膝带夹具上起作用"
        # radius：33px 宽夹具落在 2px 半径下限内（映射恒 2px），用 129px
        # 宽点光源跨过下限验证半径滑杆真实像素影响
        img_big = _fixture("point129")
        assert not np.allclose(
            ref.evaluate(img_big, {"radius": 0.1}),
            ref.evaluate(img_big, {"radius": 0.9})), "radius 必须跨过 2px 下限起作用"

    def test_red_scatter_channel_asymmetry(self) -> None:
        img = _fixture("point33")
        out_w = ref.evaluate(img)                      # w=0.5
        out_0 = ref.evaluate(img, {"red_scatter": 0.0})
        d_w = out_w - img
        d_0 = out_0 - img
        # w=0：三通道等增益（能量对称）；w>0：R 增益 1.5、B 增益 0.75
        np.testing.assert_allclose(d_0[..., 0], d_0[..., 1], atol=1e-7)
        np.testing.assert_allclose(d_0[..., 2], d_0[..., 1], atol=1e-7)
        mask = np.abs(d_w[..., 1]) > 1e-6
        assert mask.any()
        np.testing.assert_allclose(
            d_w[..., 0][mask], 1.5 * d_w[..., 1][mask], atol=1e-6)
        np.testing.assert_allclose(
            d_w[..., 2][mask], 0.75 * d_w[..., 1][mask], atol=1e-6)

    def test_two_scales_beyond_single_scale_reach(self) -> None:
        # 尺度差量验收：距点源 3px（小尺度 tap 最大 1.958px 不可达，
        # 大尺度 2R=4px 可达）处，多尺度有能量、单尺度降级版精确无能量
        img = _fixture("point33")
        ms = ref.evaluate(img)
        deg = ref.evaluate_degraded_single_scale(img)
        pos = (16, 19)
        assert np.abs(ms[pos] - img[pos]).max() > 1e-3, "大尺度分量缺失"
        assert np.abs(deg[pos] - img[pos]).max() < 1e-9, \
            "单尺度降级在超出小尺度可达处必须无能量"

    def test_edge_clamp_no_wraparound(self) -> None:
        # 角块对角远端：能量不环绕（clamp-edge 语义钉住）
        img = _fixture("cornerblock16")
        out = ref.evaluate(img)
        np.testing.assert_array_equal(out[12, 12], img[12, 12])


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "multiscale_halation")
        assert result is not None
        return result

    def test_metadata(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "multiscale_halation"
        assert parsed["version"] == "0.1.0"
        assert parsed["capabilities"]["export_support"] == "opengl"
        assert parsed["preview_cost"] == "medium"

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
        for m in re.finditer(r"for\s*\(\s*int\s+(\w+)\s*=\s*(-?\d+)\s*;\s*\1\s*(<=|<)\s*(\w+|\d+)", code_only):
            assert m.group(4).isdigit(), f"非常量循环上限: {m.group(0)}"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
