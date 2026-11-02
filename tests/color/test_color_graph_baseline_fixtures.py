"""B00.2 fixture 生成器回归 —— 确定性 / 覆盖域 / neutral 恒等。

生成器本体在 ``tests/color/fixtures/color_graph``；float reference 归档在
``docs/validation/color_nodes/baselines``（由 scripts/generate_color_graph_
baselines.py 产出，manifest 带 GPU 指纹）。本文件：

1. 纯 CPU：生成确定性（两次调用字节一致）与计划 §7.1 要求的覆盖域；
2. GL（ModernGL 可用时）：neutral Layer 经生产 composition shader 后与
   输入逐像素一致（float16 往返容差）——B07 提取算子的恒等锚点；
3. 归档对照：仅当本机 GPU 指纹与归档 manifest 一致时逐像素比对参考
   .npy（机器无关部分恒等 + 调色参考位级一致），指纹不符则 skip
   （NOT RUN，不产生伪 golden）。
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest

from tests.color.fixtures.color_graph import (
    F01_ANCHORS,
    f01_hdr_ramp,
    f02_patches,
    f03_layer_stack,
    fixture_manifest,
)


class TestGeneratorDeterminism:
    def test_f01_byte_identical_across_calls(self):
        a = f01_hdr_ramp(64)
        b = f01_hdr_ramp(64)
        assert a.tobytes() == b.tobytes()

    def test_f02_byte_identical_across_calls(self):
        assert f02_patches(64).tobytes() == f02_patches(64).tobytes()

    def test_f03_stack_params_stable_except_ids(self):
        s1, s2 = f03_layer_stack(), f03_layer_stack()
        for a, b in zip(s1, s2):
            assert a.grading_params == b.grading_params
            assert a.name == b.name
            assert a.id != b.id  # 身份每次新建（UUID），参数确定性


class TestCoverageContract:
    def test_f01_covers_required_anchor_values(self):
        img = f01_hdr_ramp(64)
        vals = {round(float(v), 4) for v in img[..., :3].ravel()}
        for anchor in F01_ANCHORS:
            assert round(float(anchor), 4) in vals, f"anchor {anchor} missing"

    def test_f01_alpha_all_opaque(self):
        assert np.all(f01_hdr_ramp(64)[..., 3] == 1.0)

    def test_f02_transparent_border_and_alpha_gradient(self):
        img = f02_patches(64)
        assert np.all(img[:4, :, 3] == 0.0)
        assert np.all(img[-4:, :, 3] == 0.0)
        assert np.all(img[:, :4, 3] == 0.0)
        assert np.all(img[:, -4:, 3] == 0.0)
        # 内部 alpha 梯度：左内沿 < 右内沿，不全等
        inner = img[10, 4:-4, 3]
        assert inner[0] < inner[-1]

    def test_manifest_hashes_match_generated_arrays(self):
        import hashlib
        m = fixture_manifest()
        digest = hashlib.sha256(f01_hdr_ramp(64).tobytes()).hexdigest()
        assert m["f01"]["sha256_64"] == digest

    def test_f03_layers_have_non_commutative_domains(self):
        l0, l1, l2 = f03_layer_stack()
        assert l0.grading_params["exposure"] != 0      # 乘性
        assert any(l1.grading_params["lift"])           # 加性
        assert l2.curves_lut is not None                # 曲线
        assert l2.grading_params["saturation"] != 1.0


# ── GL 恒等锚点（B07 算子提取的 neutral 基线） ──────────────────────────

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")


def _render_neutral(img: np.ndarray) -> np.ndarray:
    from src.features.color.core.layer import Layer
    from src.features.color.core.shader_compiler import ShaderPipeline
    from src.features.color.core.composition_uniforms import (
        composition_uniform_values,
        upload_composition_uniforms,
    )

    h, w = img.shape[:2]
    ctx = moderngl.create_standalone_context(require=330)
    try:
        pipeline = ShaderPipeline(ctx)
        prog = pipeline.build_composition_shader()
        assert prog is not None
        src_tex = ctx.texture((w, h), components=4, dtype="f2",
                              data=np.ascontiguousarray(img).astype(np.float16))
        out_tex = ctx.texture((w, h), components=4, dtype="f2")
        matte = np.ones((h, w, 4), dtype=np.float16)
        matte_tex = ctx.texture((w, h), components=4, dtype="f2", data=matte)
        fbo = ctx.framebuffer(color_attachments=[out_tex])
        prog["u_input_tex"].value = 0
        prog["u_matte_tex"].value = 1
        src_tex.use(0)
        matte_tex.use(1)
        upload_composition_uniforms(
            prog, composition_uniform_values(Layer("neutral"), width=w, height=h))
        fbo.use()
        vao = ctx.vertex_array(prog, [(ctx.buffer(np.array([
            -1.0, -1.0, 0.0, 0.0, 1.0, -1.0, 1.0, 0.0,
            -1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0],
            dtype="f4").tobytes()), "2f 2f", "in_vert", "in_texcoord")])
        vao.render(moderngl.TRIANGLE_STRIP)
        out = np.frombuffer(fbo.read(components=4, dtype="f2"),
                            dtype=np.float16).reshape(h, w, 4).astype(np.float32)
        for t in (src_tex, out_tex, matte_tex):
            t.release()
        fbo.release()
        return out
    finally:
        ctx.release()


def _assert_dual_tolerance(out: np.ndarray, ref: np.ndarray,
                           abs_tol: float = 2e-3, rel_tol: float = 2e-3) -> None:
    """计划 §7.1 误差规则：|ref|<=1 用绝对容差，HDR 用相对容差。

    B00 实测：生产 composition shader 在 neutral 参数下并非位级恒等
    （HDR 段最大绝对漂移 ~2.9e-3、相对 ~2.3e-4），属 float16 中转 +
    grading 数学链的正常数值噪声，在相对容差内。
    """
    diff = np.abs(out[..., :3] - ref[..., :3])
    sdr = np.abs(ref[..., :3]) <= 1.0
    if sdr.any():
        assert float(diff[sdr].max()) <= abs_tol, (
            f"SDR abs violation {float(diff[sdr].max()):.2e} > {abs_tol:.0e}")
    hdr = ~sdr
    if hdr.any():
        denom = np.maximum(np.abs(ref[..., :3][hdr]), 1e-6)
        assert float((diff[hdr] / denom).max()) <= rel_tol, (
            f"HDR rel violation {float((diff[hdr] / denom).max()):.2e} > {rel_tol:.0e}")


class TestNeutralIdentityGl:
    def test_neutral_pass_is_identity_on_hdr_ramp(self):
        img = f01_hdr_ramp(64)
        out = _render_neutral(img)
        # RGBA16F 往返：HDR/负值不得截断（计划 F01 断言，双容差规则）
        _assert_dual_tolerance(out, img)

    def test_neutral_pass_preserves_alpha(self):
        img = f02_patches(64)
        out = _render_neutral(img)
        np.testing.assert_allclose(out[..., 3], img[..., 3], atol=2e-3)


# ── 归档 reference 对照（仅同 GPU 指纹时执行） ──────────────────────────

_BASELINE_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..",
    "docs", "validation", "color_nodes", "baselines")


class TestArchivedBaselineMatch:
    def _manifest(self):
        path = os.path.join(_BASELINE_DIR, "manifest.json")
        if not os.path.exists(path):
            pytest.skip("基线未归档（先运行 scripts/generate_color_graph_baselines.py）")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _fingerprint(self):
        ctx = moderngl.create_standalone_context(require=330)
        try:
            info = ctx.info
            return (info.get("GL_RENDERER", ""), info.get("GL_VERSION", ""))
        finally:
            ctx.release()

    def test_neutral_reference_bitwise_match_on_same_gpu(self):
        m = self._manifest()
        gpu = m.get("gpu", {})
        renderer, _ = self._fingerprint()
        if gpu.get("renderer") != renderer:
            pytest.skip(
                f"GPU 指纹不符（归档 {gpu.get('renderer')!r} vs 本机 {renderer!r}）；"
                "换机后需重新归档基线再比对")
        ref = np.load(os.path.join(_BASELINE_DIR, "f01_neutral_64.npy"))
        out = _render_neutral(f01_hdr_ramp(64))
        assert out.shape == ref.shape
        np.testing.assert_array_equal(out, ref)
