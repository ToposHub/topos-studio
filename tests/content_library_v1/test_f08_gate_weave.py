"""F08 gate_weave 测试：oracle↔手算向量、恒等/HDR/边界/seed 有效性、
真实 parser、GLSL 红线、manifest 契约。"""

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

from content_staging.library_v1.references import gate_weave_ref as ref

COMPONENT_DIR = REPO_ROOT / "content_staging" / "library_v1" / "filters" / "gate_weave"
TGLSL_PATH = COMPONENT_DIR / "gate_weave.tglsl"
HAND = json.loads((COMPONENT_DIR / "hand_vectors.json").read_text(encoding="utf-8"))


def ramp_wide(w: int, h: int) -> np.ndarray:
    """水平灰度 ramp：img[y][x] = x/(w-1)，RGB 同值。"""
    x = np.arange(w, dtype=np.float64) / (w - 1)
    img = np.tile(x, (h, 1))
    return np.repeat(img[:, :, None], 3, axis=2).astype(np.float32)


def _fixture(kind: str) -> np.ndarray:
    return ramp_wide(8, 4) if kind == "A" else ramp_wide(64, 32)


class TestHandVectors:
    def test_main_vectors(self) -> None:
        base_params = HAND["params"]
        for vec in HAND["vectors"]:
            params = dict(base_params)
            params.update(vec.get("params", {}))
            img = _fixture(vec["fixture"])
            out = ref.evaluate(img, params)[vec["pos"][1], vec["pos"][0], 0]
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]

    def test_variant_vectors(self) -> None:
        base_params = HAND["params"]
        for vec in HAND["variant_vectors"]:
            params = dict(base_params)
            params.update(vec.get("params", {}))
            if vec["expected"] is None:
                continue
            img = _fixture(vec["fixture"])
            out = ref.evaluate(img, params)[vec["pos"][1], vec["pos"][0], 0]
            assert out == pytest.approx(vec["expected"], abs=1e-6), vec["note"]


class TestSemantics:
    def test_zero_amplitude_zero_rotation_identity(self) -> None:
        img = np.array([[-0.25, 0.0, 4.5], [12.0, 0.18, 1.0]], dtype=np.float32)
        for seed in (0, 7, 100):
            out = ref.evaluate(img, {"amplitude_h": 0.0, "amplitude_v": 0.0,
                                     "rotation": 0.0, "seed": seed})
            np.testing.assert_array_equal(out, img)

    def test_flat_field_invariant_freeze(self) -> None:
        # 常色图上双线性=常值（含旋转/位移），Freeze 模式全图恒等
        flat = np.full((32, 64, 3), 0.37, dtype=np.float32)
        out = ref.evaluate(flat, {"amplitude_h": 0.02, "amplitude_v": 0.02,
                                  "rotation": 0.5, "border_mode": "Freeze"})
        np.testing.assert_allclose(out, flat, atol=1e-7)

    def test_black_border_produces_zeros_freeze_does_not(self) -> None:
        img = ramp_wide(64, 32)
        common = {"amplitude_h": 0.02, "amplitude_v": 0.02, "rotation": 0.0, "seed": 7}
        freeze = ref.evaluate(img, {**common, "border_mode": "Freeze"})
        black = ref.evaluate(img, {**common, "border_mode": "Black"})
        assert (black == 0.0).any(), "Black 模式须有越界黑像素"
        assert not (freeze == 0.0).any(), "Freeze 模式不产生黑像素（ramp 无 0 值端）"
        # 两种模式仅在越界像素上不同
        inside = black != 0.0
        np.testing.assert_array_equal(freeze[inside], black[inside])

    def test_displacement_constant_across_frame(self) -> None:
        # 整帧常量位移：所有像素行进同一 (dx, dy)（rotation=0 时纯平移）
        img = ramp_wide(64, 32)
        out = ref.evaluate(img, {"amplitude_h": 0.02, "amplitude_v": 0.0,
                                 "rotation": 0.0, "seed": 7, "border_mode": "Freeze"})
        # 水平 ramp 上纯水平平移 → 每行输出相同（行间差为 0）
        np.testing.assert_allclose(out, np.repeat(out[0:1], out.shape[0], axis=0),
                                   atol=1e-7)

    def test_seed_changes_output(self) -> None:
        img = ramp_wide(64, 32)
        common = {"amplitude_h": 0.02, "amplitude_v": 0.02, "rotation": 0.1}
        assert not np.allclose(
            ref.evaluate(img, {**common, "seed": 7}),
            ref.evaluate(img, {**common, "seed": 8}))

    def test_same_seed_reproduces_bit_exact(self) -> None:
        img = ramp_wide(64, 32)
        params = {"amplitude_h": 0.02, "amplitude_v": 0.02, "rotation": 0.2, "seed": 9}
        np.testing.assert_array_equal(ref.evaluate(img, params), ref.evaluate(img, params))

    def test_hdr_values_carried_not_clamped(self) -> None:
        # 常色 HDR 场：重采样=常值搬运（Freeze 不引入 clamp；>1 与 <0 双向）
        for value in (12.0, -0.5):
            img = np.full((4, 8, 3), value, dtype=np.float32)
            out = ref.evaluate(img, {"amplitude_h": 0.02, "amplitude_v": 0.02,
                                     "rotation": 0.0, "seed": 7, "border_mode": "Freeze"})
            np.testing.assert_allclose(out, value, atol=1e-6)

    def test_every_param_has_pixel_effect(self) -> None:
        img = ramp_wide(64, 32)
        base = ref.evaluate(img, {"amplitude_h": 0.004, "amplitude_v": 0.002,
                                  "rotation": 0.05, "seed": 7})
        assert not np.allclose(ref.evaluate(img, {"amplitude_h": 0.02, "amplitude_v": 0.002,
                                                  "rotation": 0.05, "seed": 7}), base)
        assert not np.allclose(ref.evaluate(img, {"amplitude_h": 0.004, "amplitude_v": 0.002,
                                                  "rotation": 0.4, "seed": 7}), base)
        assert not np.allclose(ref.evaluate(img, {"amplitude_h": 0.004, "amplitude_v": 0.002,
                                                  "rotation": 0.05, "seed": 42}), base)
        # border 模式在越界像素上有效（幅度需足以越界：0.02 下 x=63 出界）
        assert not np.allclose(ref.evaluate(img, {"amplitude_h": 0.02, "amplitude_v": 0.002,
                                                  "rotation": 0.05, "seed": 7,
                                                  "border_mode": "Black"}), base)
        # amplitude_v：水平 ramp 行间同值验不出竖向位移——用 2D ramp 复验
        y2, x2 = np.meshgrid(np.arange(32), np.arange(64), indexing="ij")
        img2 = np.repeat(((x2 + 64 * y2) / (63 + 64 * 31))[:, :, None], 3, axis=2).astype(np.float32)
        base2 = ref.evaluate(img2, {"amplitude_h": 0.004, "amplitude_v": 0.002,
                                    "rotation": 0.05, "seed": 7})
        assert not np.allclose(ref.evaluate(img2, {"amplitude_h": 0.004, "amplitude_v": 0.02,
                                                   "rotation": 0.05, "seed": 7}), base2)


class TestRealParserCompat:
    @pytest.fixture()
    def parsed(self) -> dict:
        from src.features.color.core.plugin_manager import PluginManager
        result = PluginManager()._parse_plugin_file(str(TGLSL_PATH), "gate_weave")
        assert result is not None, "真实 parser 解析失败"
        return result

    def test_metadata_fields(self, parsed: dict) -> None:
        assert parsed["plugin_id"] == "gate_weave"
        assert parsed["version"] == "0.1.0"
        caps = parsed["capabilities"]
        assert caps.get("export_support") == "opengl"
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

    def test_manifest_roundtrip_valid(self) -> None:
        from content_staging.library_v1.specs.manifest import ComponentManifest
        m = ComponentManifest.from_json(
            (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))
        assert m.validate() == [], m.validate()
        assert m.slug == "gate_weave"
        assert m.time.time_domain == "static"
        assert m.image.sampling_border == "clamp-half-texel"

    def test_glsl_red_lines_static(self) -> None:
        src = TGLSL_PATH.read_text(encoding="utf-8")
        body = re.sub(r"// --- Topos Plugin Metadata ---.*?// -{5,}", "", src, flags=re.DOTALL)
        code_only = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())
        assert "while" not in code_only, "禁止 while 循环"
        assert not re.search(r"\bfor\s*\(", code_only), "单次重采样应零循环"
        for m in re.finditer(r"\bu_\w+", code_only):
            assert m.group(0) in {"u_input_tex", "u_tex_size", "u_aspect_ratio"}, m.group(0)
        # 半纹素安全：Freeze 采样必须钳到 [0.5/ts, 1-0.5/ts]
        assert re.search(r"clamp\s*\(\s*suv\s*,\s*mn\s*,\s*mx\s*\)", code_only), \
            "采样坐标须钳半纹素边界"
        assert re.search(r"vec2 mn = vec2\(0\.5\) / ts", code_only)

    def test_param_names_not_glsl_reserved(self) -> None:
        from src.features.color.core.shader_compiler import _GLSL_RESERVED_IDENTIFIERS
        for p in (json.loads(
                (COMPONENT_DIR / "component_manifest.json").read_text(encoding="utf-8"))["params"]):
            assert p["name"] not in _GLSL_RESERVED_IDENTIFIERS, p["name"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
