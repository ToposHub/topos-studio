"""F17 solarize 测试：oracle↔手算向量、恒等/HDR 折返/连续性/两档差异、
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

from content_staging.library_v1.references import solarize_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "solarize"
TGLSL_PATH = COMPONENT_DIR / "solarize.tglsl"
HAND_VECTORS = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
BASE = {"threshold": 1.0, "softness": 0.0, "channel_mode": "Luma", "amount": 1.0}


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
    def test_amount_zero_exact_identity_including_hdr(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        out = ref.evaluate(img, {"amount": 0.0})
        np.testing.assert_array_equal(out, img)

    def test_fold_anchor_values(self) -> None:
        # 折返锚点（thr=1, softness=0）：非单调回落
        for v, expect in ((0.3, 0.3), (1.0, 1.0), (1.5, 0.5), (2.6, -0.6), (12.0, -10.0)):
            got = ref.fold(np.float64(v), 1.0, 1e-4)
            assert got == pytest.approx(expect, abs=1e-6), (v, got)
        # 负值不动
        assert ref.fold(np.float64(-0.25), 1.0, 1e-4) == pytest.approx(-0.25, abs=1e-9)

    def test_luma_mode_preserves_chroma_vector(self) -> None:
        # Luma 档：out − in 为常量平移（色度向量逐位保留）。
        # 两像素 luma 均须 >thr（fold 后亮度差=原亮度差保持镜像）
        img = np.array([[2.0, 1.0, 0.5], [2.5, 1.5, 0.8]], dtype=np.float32)
        assert np.all(img.astype(np.float64) @ LUMA > 1.0)
        out = ref.evaluate(img, BASE)
        shift = (out - img).astype(np.float64)
        np.testing.assert_allclose(shift[0], shift[0][0], atol=1e-6)
        np.testing.assert_allclose(shift[1], shift[1][0], atol=1e-6)
        luma_in = img.astype(np.float64) @ LUMA
        luma_out = out.astype(np.float64) @ LUMA
        np.testing.assert_allclose(luma_out[0] - luma_out[1],
                                   (2.0 - luma_in[0]) - (2.0 - luma_in[1]), atol=1e-6)

    def test_rgb_mode_per_channel(self) -> None:
        img = np.array([[2.0, 1.0, 0.5]], dtype=np.float32)
        out = ref.evaluate(img, dict(BASE, channel_mode="RGB"))[0]
        np.testing.assert_allclose(out, [0.0, 1.0, 0.5], atol=1e-6)

    def test_modes_distinguishable(self) -> None:
        img = np.array([[2.0, 1.0, 0.5]], dtype=np.float32)
        luma_out = ref.evaluate(img, BASE)
        rgb_out = ref.evaluate(img, dict(BASE, channel_mode="RGB"))
        assert not np.allclose(luma_out, rgb_out, atol=1e-3)

    def test_continuity_at_threshold(self) -> None:
        # v=thr 处 fold(v)=v：连续无跳变（含软过渡）
        vs = np.array([[1.0 - 1e-3, 1.0 - 1e-3, 1.0 - 1e-3],
                       [1.0, 1.0, 1.0],
                       [1.0 + 1e-3, 1.0 + 1e-3, 1.0 + 1e-3]], dtype=np.float32)
        for params in (BASE, dict(BASE, softness=0.5)):
            out = ref.evaluate(vs, params)
            assert float(np.abs(np.diff(out.astype(np.float64), axis=0)).max()) < 1e-2

    def test_softness_monotone_partial_fold(self) -> None:
        # 软过渡：v 在过渡带内时输出介于原值与硬折返之间，且随 softness 增大更接近原值
        img = np.array([[1.2, 1.2, 1.2]], dtype=np.float32)
        hard = ref.evaluate(img, BASE)[0]                       # thr=1: fold=0.8
        soft = ref.evaluate(img, dict(BASE, softness=0.5))[0]
        original = img[0]
        assert np.all(soft > hard) and np.all(soft < original)

    def test_hdr_linear_foldback_declared(self) -> None:
        # 显式声明的行为：HDR 折出负值、负值输入不动（非 clamp、非裁剪）
        out = ref.evaluate(np.array([[12.0, 0.1, 4.5]], dtype=np.float32),
                           dict(BASE, channel_mode="RGB"))[0]
        np.testing.assert_allclose(out, [-10.0, 0.1, -2.5], atol=1e-5)

    def test_every_param_has_pixel_effect(self) -> None:
        from content_staging.library_v1.fixtures.generate_fixtures import hdr_probes
        base = hdr_probes()
        base_out = ref.evaluate(base, BASE)

        def changed(params: dict) -> np.ndarray:
            merged = dict(BASE)
            merged.update(params)
            return ref.evaluate(base, merged)

        assert not np.allclose(changed({"threshold": 2.0}), base_out)
        assert not np.allclose(changed({"softness": 0.5}), base_out)
        assert not np.allclose(changed({"channel_mode": "RGB"}), base_out)
        assert not np.allclose(changed({"amount": 0.5}), base_out)


class TestRealParserCompat:
    """经真实 PluginManager 解析（显式暂存路径，不触全局 registry/生产目录）。"""

    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "solarize")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "solarize"
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
            if mp["ptype"] == "option":
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
        assert not re.search(r"\bfor\s*\(", body), "点处理不应有循环"
        for m in re.finditer(r"\bu_\w+", body):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, \
                f"未声明 uniform {m.group(0)}"
        # 折返与软过渡存在（浮点语境）；无 clamp 内容色
        assert "2.0 * thr - v" in body
        assert "smoothstep(thr - h, thr + h, v)" in body
        assert "clamp(" not in body


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
