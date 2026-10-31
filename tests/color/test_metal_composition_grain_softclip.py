"""P7-03 第六批 G2 门禁：film grain / soft clip 的 composition fragment 形态。

P6-06 已为同一 GLSL 公式交付 compute 内核（kernels_color.py）+ numpy
参考实现 + 实测门禁；本批把两效果的 **fragment 形态**接入 composition
MSL emitter（GL main 中的调用位：soft_clip 在 hsl_curve 与 mixer 之间
且带 ``if (u_sc_enabled)`` 守卫；film_grain 在 blend/mix 之后、main 末位）。

门禁按效果类适配：

- **soft clip**：纯数学（pow/sign/mix，无噪声）——标准 GL↔MTL 对拍
  （SDR ≤1/255、HDR ≤2e-3 rel），另有 P6-06 参考实现复验；
- **film grain**：fract-hash 噪声输出（无 sin）——GL 侧噪声相位与 MTL
  不同属预期（同分布），逐像素位级对拍无意义；门禁为
  ①**MTL fragment vs P6-06 numpy 参考** ≤2 fp16 ulp（沿用 kernels_color
  实测约定，即「翻译忠实于 GLSL 权威文本」的最强度量）；
  ②同设备两次渲染**逐位相同**（确定性）；
  ③GL↔MTL 零偏 + 分位数噪声门（两噪声实现同分布的统计佐证）。

输入量化约定（P6-06）：fragment 从 RGBA16F 纹理读入，参考实现必须使用
**先过 fp16 量化的颜色**，否则对比的是量化前后两份输入。
"""

from __future__ import annotations

import numpy as np
import pytest

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用")

from src.features.color.core.composition_uniforms import (  # noqa: E402
    build_composition_uniform_manifest,
    composition_uniform_values,
    upload_composition_uniforms,
)
from src.features.color.core.msl_composition import (  # noqa: E402
    MIGRATED_EFFECTS,
    emit_composition_msl,
)
from src.features.color.core.shader_compiler import ShaderPipeline  # noqa: E402
from src.shared.gpu.metal.kernels_color import film_grain_reference  # noqa: E402

W, H = 2048, 256

# 管线变体（soft_clip 无 manifest 旗标——u_sc_* 恒在基础声明；
# grain_enabled 声明 grain uniform 组）
VARIANTS = {
    "grain": dict(gl=dict(grain_enabled=True),
                  manifest=dict(grain_enabled=True),
                  effects=("grading", "film_grain")),
    "softclip": dict(gl=dict(has_soft_clip=True),
                     manifest=dict(),
                     effects=("grading", "soft_clip")),
    "curves_softclip": dict(gl=dict(has_curves_soft_clip=True),
                            manifest=dict(),
                            effects=("grading", "curves_soft_clip")),
    "both": dict(gl=dict(grain_enabled=True, has_soft_clip=True),
                 manifest=dict(grain_enabled=True),
                 effects=("grading", "soft_clip", "film_grain")),
}


# ---------------------------------------------------------------------------
# 结构层（无 Metal 也运行）
# ---------------------------------------------------------------------------


class TestEmitterStructure:
    def test_new_effects_are_migrated(self):
        for effect in ("soft_clip", "film_grain"):
            assert effect in MIGRATED_EFFECTS

    def test_all_twelve_snippets_migrated(self):
        """composition 12 SNIPPET 全部完成（P7-03 收口 + curves_soft_clip）。"""
        assert set(MIGRATED_EFFECTS) == {
            "grading", "curves", "hsl_curve", "lut3d", "mixer", "warper",
            "blur_sharpen", "blend_modes", "soft_clip", "curves_soft_clip",
            "film_grain"}

    def test_untranslated_effect_still_rejected(self):
        manifest = build_composition_uniform_manifest()
        with pytest.raises(ValueError, match="plugins"):
            emit_composition_msl(manifest, ["grading", "plugins"])

    def test_softclip_call_has_enabled_guard(self):
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, ("grading", "soft_clip"))
        assert "if (params.u_sc_enabled != 0u) { color = msl_apply_soft_clip(" in src

    def test_grain_call_is_after_blend_mix_and_uses_struct_rects(self):
        manifest = build_composition_uniform_manifest(grain_enabled=True)
        src = emit_composition_msl(
            manifest, ("grading", "blend_modes", "film_grain"))
        body = src[src.index("float mask = u_matte_tex"):]
        g = body.index("msl_apply_film_grain")
        m = body.index("mix(tex_color.rgb, blended")
        assert m < g  # grain 在 blend/mix 之后（GL main 末位）
        assert "params.u_grain_monochrome != 0u" in src
        assert "params.u_visible_rect_z, params.u_visible_rect_w" in src


# ---------------------------------------------------------------------------
# 真机层
# ---------------------------------------------------------------------------

try:
    from src.shared.gpu.metal import MetalComputeContext
    from src.shared.gpu.metal.context import MetalComputeContext as _Ctx
    from src.shared.media.macos_vt.cv_metal_bridge import (
        mtl_pixel_format,
        read_texture_rgba16f,
    )
    _HAS_METAL_IMPORTS = True
except Exception:  # noqa: BLE001 — 非 macOS / 无 PyObjC 时只跳过真机层
    _HAS_METAL_IMPORTS = False

requires_metal = pytest.mark.skipif(
    not _HAS_METAL_IMPORTS or not _Ctx.is_supported(),
    reason="无 Metal 设备",
)


def _pattern_sdr() -> np.ndarray:
    img = np.zeros((H, W, 4), dtype=np.float32)
    img[..., :3] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    for row, g in zip(range(8, H, 24), (0.0, 0.018, 0.081, 0.18, 0.5, 0.9,
                                        1.0, 0.25, 0.75)):
        img[row:row + 8, :W // 2, :3] = g
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _pattern_hdr() -> np.ndarray:
    img = np.zeros((H, W, 4), dtype=np.float32)
    img[..., :3] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    neg = np.linspace(-0.2, 0.0, W // 4, dtype=np.float32)[None, :, None]
    hdr = np.linspace(1.0, 4.0, W // 4, dtype=np.float32)[None, :, None]
    img[:H // 4, :W // 4, :3] = neg
    img[:H // 4, W // 2:3 * W // 4, :3] = hdr
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _values_for(layer: dict, grain_params=None) -> dict:
    return composition_uniform_values(
        layer, grain_params=grain_params, width=W, height=H,
        tex_size=(float(W), float(H)))


def _params_from_values(values: dict, dtype) -> np.ndarray:
    arr = np.zeros(1, dtype)
    for name, v in values.items():
        if isinstance(v, (tuple, list)):
            for i, comp in enumerate(v[:4]):
                key = f"{name}_{'xyzw'[i]}"
                if key in dtype.names:
                    arr[key] = comp
        elif name in dtype.names:
            arr[name] = (1 if v else 0) if isinstance(v, bool) else v
    return arr


def _assert_sdr(a: np.ndarray, b: np.ndarray, label: str) -> None:
    qa = np.rint(np.clip(a, 0.0, 1.0) * 255.0)
    qb = np.rint(np.clip(b, 0.0, 1.0) * 255.0)
    diff = np.abs(qa - qb)
    assert diff.max() <= 1.0, (
        f"{label}: {int((diff > 1).sum())} 像素超 G2 SDR 门禁（≤1/255），"
        f"最大 {diff.max():.0f} LSB"
    )


def _assert_hdr(a: np.ndarray, b: np.ndarray, label: str) -> None:
    denom = np.maximum(1.0, np.maximum(np.abs(a), np.abs(b)))
    err = np.abs(a - b) / denom
    assert err.max() <= 2e-3, (
        f"{label}: {int((err > 2e-3).sum())} 像素超 G2 HDR 门禁（≤2e-3 rel），"
        f"最大 {err.max():.4g}"
    )


def _assert_fp16_ulp(out: np.ndarray, ref: np.ndarray, max_ulp: int,
                     label: str) -> None:
    out16 = out.astype(np.float16).astype(np.float32)
    ref16 = ref.astype(np.float16)
    err = np.abs(out16 - ref16)
    up = np.nextafter(ref16, np.float16(np.inf)).astype(np.float32)
    dn = np.nextafter(ref16, np.float16(-np.inf)).astype(np.float32)
    budget = np.maximum(np.abs(up - ref16), np.abs(ref16 - dn))
    bad = err > budget * float(max_ulp) + 1e-12
    assert not bad.any(), (
        f"{label}: {int(bad.sum())} 像素超 {max_ulp} fp16 ulp "
        f"（最大 err={err[bad].max():.4g}）"
    )


# (名称, soft_clip_params)——覆盖 toe 双分支 / shoulder 双分支 / 曝光对比度
SOFTCLIP_SDR_CASES = [
    ("shoulder_toe_active", dict(
        enabled=True, high=1.0, white_point=2.0, shoulder_strength=0.7,
        low=0.0, black_point=-1.0, toe_strength=0.6,
        exposure=1.0, contrast=0.0)),
    ("negative_contrast", dict(
        enabled=True, high=1.2, white_point=4.0, shoulder_strength=0.5,
        low=-0.1, black_point=-2.0, toe_strength=0.3,
        exposure=0.0, contrast=-0.3)),
    ("disabled_passthrough", dict(
        enabled=False, high=0.5, white_point=0.6, shoulder_strength=0.9,
        low=0.5, black_point=0.4, toe_strength=1.0,
        exposure=0.0, contrast=0.5)),
]
SOFTCLIP_HDR_CASES = [
    ("hdr_ratio_preserve", dict(
        enabled=True, high=1.0, white_point=2.0, shoulder_strength=0.5,
        low=0.0, black_point=-1.0, toe_strength=0.6,
        exposure=0.0, contrast=0.0)),
]

# (名称, grain_params)
GRAIN_CASES = [
    ("monochrome_fallback", dict(
        intensity=0.35, size=2.5, monochrome=True, shadows=0.8,
        midtones=1.0, highlights=0.5, seed=0.42)),
    ("color_viewport_rect", dict(
        intensity=0.35, size=2.5, monochrome=False, shadows=0.8,
        midtones=1.0, highlights=0.5, seed=7.0,
        visible_rect=(512.0, 64.0, 1024.0, 128.0))),
]
GRAIN_SDR_PARAMS = {  # GL↔MTL 统计门界（LSB）：振幅 ~ intensity*mask*0.5
    "monochrome_fallback": (2.0, 4.0, 8.0),
    "color_viewport_rect": (2.0, 4.0, 8.0),
}


@pytest.fixture(scope="module")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx
    ctx.finish()


@pytest.fixture(scope="module")
def gl():
    ctx = moderngl.create_standalone_context(require=330)
    yield ctx
    ctx.release()


@pytest.fixture(scope="module")
def pipeline(gl):
    p = ShaderPipeline(gl)
    yield p
    p.release()


@pytest.fixture(scope="module")
def gl_progs(pipeline):
    out = {}
    for name, cfg in VARIANTS.items():
        prog = pipeline.build_composition_shader(**cfg["gl"])
        assert prog is not None, f"GL 编译失败: {name}"
        out[name] = prog
    return out


@pytest.fixture(scope="module")
def psos(metal_ctx):
    import Metal
    dev = metal_ctx.device
    out = {}
    for name, cfg in VARIANTS.items():
        manifest = build_composition_uniform_manifest(**cfg["manifest"])
        src = emit_composition_msl(manifest, cfg["effects"])
        lib, err = dev.newLibraryWithSource_options_error_(src, None, None)
        assert lib is not None, f"MSL 编译失败 ({name}): {err}"
        desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
        desc.setVertexFunction_(lib.newFunctionWithName_("vs_composition"))
        desc.setFragmentFunction_(lib.newFunctionWithName_("fs_composition"))
        desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(
            mtl_pixel_format("rgba16float"))
        pso, perr = dev.newRenderPipelineStateWithDescriptor_error_(desc, None)
        assert pso is not None, f"pipeline 失败 ({name}): {perr}"
        out[name] = (pso,
                     build_composition_uniform_manifest(
                         **cfg["manifest"]).to_numpy_dtype())
    return out


def _gl_render(gl, prog, pattern, values, hdr: bool) -> np.ndarray:
    src = gl.texture((W, H), 4, dtype="f2")
    src.filter = (gl.LINEAR, gl.LINEAR)
    src.repeat_x = False
    src.repeat_y = False
    src.write(pattern.astype(np.float16).tobytes())
    matte = gl.texture((W, H), 4, dtype="f2")
    matte.write(np.ones((H, W, 4), dtype=np.float16).tobytes())
    dst = gl.texture((W, H), 4, dtype="f2" if hdr else "f1")
    fbo = gl.framebuffer(dst)
    quad = gl.buffer(np.array([
        -1.0, -1.0, 0.0, 0.0,
         1.0, -1.0, 1.0, 0.0,
        -1.0,  1.0, 0.0, 1.0,
         1.0,  1.0, 1.0, 1.0,
    ], dtype="f4").tobytes())
    vao = gl.vertex_array(prog, [(quad, "2f 2f", "in_vert", "in_texcoord")])

    upload_composition_uniforms(prog, values)
    if "u_tex_size" in prog:
        prog["u_tex_size"].value = (float(W), float(H))

    fbo.use()
    gl.viewport = (0, 0, W, H)
    src.use(0)
    matte.use(1)
    prog["u_input_tex"].value = 0
    prog["u_matte_tex"].value = 1
    vao.render(moderngl.TRIANGLE_STRIP)
    raw = fbo.read(components=4, dtype="f2" if hdr else "f1")
    out = np.frombuffer(raw, dtype="<f2" if hdr else "uint8").reshape(
        H, W, 4).astype(np.float32)
    if not hdr:
        out /= 255.0
    for res in (src, matte, dst, fbo, quad, vao):
        try:
            res.release()
        except Exception:
            pass
    return out


def _metal_render(metal_ctx, pso, pattern, values, dtype) -> np.ndarray:
    import Metal
    dev = metal_ctx.device
    fmt = mtl_pixel_format("rgba16float")
    rgba = np.empty((H, W, 4), dtype=np.float16)
    rgba[..., :3] = pattern.astype(np.float16)[..., :3]
    rgba[..., 3] = 1.0
    src = metal_ctx.create_texture_from_numpy(rgba, fmt, W, H)
    tdesc = (Metal.MTLTextureDescriptor
             .texture2DDescriptorWithPixelFormat_width_height_mipmapped_(
                 fmt, W, H, False))
    tdesc.setUsage_(Metal.MTLTextureUsageShaderRead
                    | Metal.MTLTextureUsageRenderTarget)
    dst = dev.newTextureWithDescriptor_(tdesc)

    sdesc = Metal.MTLSamplerDescriptor.alloc().init()
    sdesc.setMinFilter_(Metal.MTLSamplerMinMagFilterLinear)
    sdesc.setMagFilter_(Metal.MTLSamplerMinMagFilterLinear)
    sdesc.setSAddressMode_(Metal.MTLSamplerAddressModeClampToEdge)
    sdesc.setTAddressMode_(Metal.MTLSamplerAddressModeClampToEdge)
    sdesc.setRAddressMode_(Metal.MTLSamplerAddressModeClampToEdge)
    smp = dev.newSamplerStateWithDescriptor_(sdesc)

    pbuf = metal_ctx.create_buffer_from_numpy(_params_from_values(values,
                                                                  dtype))

    rpd = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
    ca = rpd.colorAttachments().objectAtIndexedSubscript_(0)
    ca.setTexture_(dst)
    ca.setLoadAction_(Metal.MTLLoadActionDontCare)
    ca.setStoreAction_(Metal.MTLStoreActionStore)
    cb = metal_ctx._queue.commandBuffer()
    enc = cb.renderCommandEncoderWithDescriptor_(rpd)
    enc.setRenderPipelineState_(pso)
    enc.setFragmentTexture_atIndex_(src, 0)
    enc.setFragmentSamplerState_atIndex_(smp, 0)
    enc.setFragmentBuffer_offset_atIndex_(pbuf, 0, 0)
    enc.drawPrimitives_vertexStart_vertexCount_(
        Metal.MTLPrimitiveTypeTriangle, 0, 6)
    enc.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()
    assert cb.error() is None, f"command buffer error: {cb.error()}"
    return read_texture_rgba16f(metal_ctx, dst, W, H)


@requires_metal
class TestSoftClipParity:
    """纯数学路径：标准 GL↔MTL 对拍。"""

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, psos, metal_ctx, pattern, hdr, sc_params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {}, "soft_clip_params": sc_params}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["softclip"], pattern, values, hdr)
        pso, dtype = psos["softclip"]
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,sc", SOFTCLIP_SDR_CASES,
                             ids=[c[0] for c in SOFTCLIP_SDR_CASES])
    def test_sdr(self, gl, gl_progs, psos, metal_ctx, sdr_pattern,
                 name, sc):
        gl_out, mt_out = self._run(gl, gl_progs, psos, metal_ctx,
                                   sdr_pattern, False, sc)
        _assert_sdr(gl_out, mt_out, f"softclip/SDR/{name}")

    @pytest.mark.parametrize("name,sc", SOFTCLIP_HDR_CASES,
                             ids=[c[0] for c in SOFTCLIP_HDR_CASES])
    def test_hdr(self, gl, gl_progs, psos, metal_ctx, hdr_pattern,
                 name, sc):
        gl_out, mt_out = self._run(gl, gl_progs, psos, metal_ctx,
                                   hdr_pattern, True, sc)
        _assert_hdr(gl_out, mt_out, f"softclip/HDR/{name}")


# (名称, curves_soft_clip 状态)——覆盖双膝 / 单膝 / 每通道差异 / 纯 clip 点
def _csc_state(**channels):
    base = {"low_clip": 0.0, "low_soft": 0.0, "high_clip": 1.0, "high_soft": 0.0}
    full = {ch: dict(base) for ch in "RGB"}
    for name, values in channels.items():
        full[name].update(values)
    return {"linked": False, "channels": full}


CURVES_SOFTCLIP_SDR_CASES = [
    ("both_knees", _csc_state(R={"low_clip": -0.1, "low_soft": 0.2,
                                 "high_clip": 0.9, "high_soft": 0.15})),
    ("per_channel", _csc_state(
        R={"high_clip": 0.8, "high_soft": 0.1},
        G={"low_clip": 0.05, "low_soft": 0.1},
        B={"low_clip": -0.2, "low_soft": 0.1,
           "high_clip": 1.2, "high_soft": 0.25})),
    ("clip_points_only", _csc_state(R={"low_clip": -0.05, "high_clip": 0.85})),
]
CURVES_SOFTCLIP_HDR_CASES = [
    ("hdr_knees", _csc_state(R={"low_clip": -0.1, "low_soft": 0.2,
                                "high_clip": 0.95, "high_soft": 0.1})),
]


@requires_metal
class TestCurvesSoftClipParity:
    """曲线面板四参数柔化裁切：GL↔MTL 对拍 + CPU 参考复验。

    与 legacy soft clip 同一 G2 门禁（SDR ≤1/255、HDR ≤2e-3 rel）；CPU
    参考直接复用 advanced_grade_cpu.apply_curves_soft_clip_image（导出
    路径同一实现），输入先过 fp16 量化（P6-06 约定）。
    """

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, psos, metal_ctx, pattern, hdr, state):
        from src.features.color.core.advanced_grade_cpu import (
            apply_curves_soft_clip_image,
        )
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {}, "curves_soft_clip": state}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["curves_softclip"], pattern,
                            values, hdr)
        pso, dtype = psos["curves_softclip"]
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype)
        cpu_ref = apply_curves_soft_clip_image(
            pattern.astype(np.float16).astype(np.float32)[..., :3], state)
        return gl_out, mt_out, cpu_ref

    @pytest.mark.parametrize("name,state", CURVES_SOFTCLIP_SDR_CASES,
                             ids=[c[0] for c in CURVES_SOFTCLIP_SDR_CASES])
    def test_sdr(self, gl, gl_progs, psos, metal_ctx, sdr_pattern,
                 name, state):
        gl_out, mt_out, cpu_ref = self._run(gl, gl_progs, psos, metal_ctx,
                                            sdr_pattern, False, state)
        _assert_sdr(gl_out, mt_out, f"curves_softclip/SDR/{name}")
        _assert_sdr(gl_out[..., :3], cpu_ref, f"curves_softclip/SDR/{name}/cpu")

    @pytest.mark.parametrize("name,state", CURVES_SOFTCLIP_HDR_CASES,
                             ids=[c[0] for c in CURVES_SOFTCLIP_HDR_CASES])
    def test_hdr(self, gl, gl_progs, psos, metal_ctx, hdr_pattern,
                 name, state):
        gl_out, mt_out, cpu_ref = self._run(gl, gl_progs, psos, metal_ctx,
                                            hdr_pattern, True, state)
        _assert_hdr(gl_out, mt_out, f"curves_softclip/HDR/{name}")
        _assert_hdr(gl_out[..., :3], cpu_ref,
                    f"curves_softclip/HDR/{name}/cpu")


@requires_metal
class TestFilmGrainFragment:
    """fract-hash 噪声路径：MTL vs P6-06 参考门 + 确定性 + GL↔MTL 统计。"""

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    def _grain_layer(self, grain_params):
        gp = dict(grain_params)
        visible_rect = gp.pop("visible_rect", (0.0, 0.0, 0.0, 0.0))
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {}}
        return layer, gp, visible_rect

    def _run(self, gl, gl_progs, psos, metal_ctx, pattern, grain_params):
        layer, gp, visible_rect = self._grain_layer(grain_params)
        values = _values_for(layer, grain_params=gp)
        values["u_visible_rect"] = tuple(float(v) for v in visible_rect) \
            if sum(visible_rect) > 0 else (0.0, 0.0, 0.0, 0.0)
        gl_out = _gl_render(gl, gl_progs["grain"], pattern, values, True)
        pso, dtype = psos["grain"]
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype)
        return gl_out, mt_out, gp, visible_rect

    @pytest.mark.parametrize("name,gp", GRAIN_CASES,
                             ids=[c[0] for c in GRAIN_CASES])
    def test_mtl_vs_p606_reference_within_2_fp16_ulp(
            self, gl, gl_progs, psos, metal_ctx, sdr_pattern, name, gp):
        _, mt_out, gp2, visible_rect = self._run(
            gl, gl_progs, psos, metal_ctx, sdr_pattern, gp)
        # P6-06 参考约定：输入先过 fp16 量化（fragment 从 RGBA16F 读入）
        color_in = sdr_pattern.astype(np.float16)[..., :3].astype(np.float32)
        ref = film_grain_reference(
            color_in, intensity=gp2["intensity"], size=gp2["size"],
            monochrome=gp2["monochrome"], shadows=gp2["shadows"],
            midtones=gp2["midtones"], highlights=gp2["highlights"],
            seed=gp2["seed"], visible_rect=tuple(visible_rect),
            tex_size=(float(W), float(H)))
        _assert_fp16_ulp(mt_out[..., :3], ref, 2, f"grain/{name}")

    @pytest.mark.parametrize("name,gp", GRAIN_CASES[:1],
                             ids=[c[0] for c in GRAIN_CASES[:1]])
    def test_mtl_determinism_bitwise(self, gl, gl_progs, psos, metal_ctx,
                                     sdr_pattern, name, gp):
        _, mt1, _, _ = self._run(gl, gl_progs, psos, metal_ctx,
                                 sdr_pattern, gp)
        _, mt2, _, _ = self._run(gl, gl_progs, psos, metal_ctx,
                                 sdr_pattern, gp)
        np.testing.assert_array_equal(
            mt1.astype(np.float16).view(np.uint16),
            mt2.astype(np.float16).view(np.uint16))

    @pytest.mark.parametrize("name,gp", GRAIN_CASES,
                             ids=[c[0] for c in GRAIN_CASES])
    def test_gl_mtl_same_distribution(self, gl, gl_progs, psos, metal_ctx,
                                      sdr_pattern, name, gp):
        """GL 与 MTL 噪声相位不同属预期（不同 hash 实现），但输出须
        同分布：零偏 + 分位数尾界（振幅 ~ intensity×mask×0.5）。"""
        gl_out, mt_out, _, _ = self._run(gl, gl_progs, psos, metal_ctx,
                                         sdr_pattern, gp)
        qa = np.rint(np.clip(gl_out[..., :3], 0.0, 1.0) * 255.0)
        qb = np.rint(np.clip(mt_out[..., :3], 0.0, 1.0) * 255.0)
        signed = (qa - qb).ravel()
        assert abs(signed.mean()) <= 0.3, (
            f"GL↔MTL 符号偏差 {signed.mean():+.3f} LSB ≠ 0"
        )
        ad = np.abs(signed)
        p99, p999 = np.percentile(ad, 99), np.percentile(ad, 99.9)
        bounds = GRAIN_SDR_PARAMS[name]
        assert p99 <= bounds[0] and p999 <= bounds[1], (
            f"GL↔MTL p99={p99:.1f}/p99.9={p999:.1f} 超噪声门 {bounds}"
        )


@requires_metal
class TestCombinedGrainSoftClip:
    def test_softclip_then_grain_pipeline_order(self, gl, gl_progs, psos,
                                                metal_ctx):
        pattern = _pattern_sdr()
        sc = SOFTCLIP_SDR_CASES[0][1]
        gp = dict(GRAIN_CASES[0][1])
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {}, "soft_clip_params": sc}
        values = _values_for(layer, grain_params=gp)
        gl_out = _gl_render(gl, gl_progs["both"], pattern, values, True)
        pso, dtype = psos["both"]
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype)
        # 组合路径含噪声 → 统计门（同 TestFilmGrainFragment）
        qa = np.rint(np.clip(gl_out[..., :3], 0.0, 1.0) * 255.0)
        qb = np.rint(np.clip(mt_out[..., :3], 0.0, 1.0) * 255.0)
        signed = (qa - qb).ravel()
        assert abs(signed.mean()) <= 0.3
        ad = np.abs(signed)
        assert np.percentile(ad, 99.9) <= 5.0
