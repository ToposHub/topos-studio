"""P7-03 G2 门禁：composition MSL emitter vs GL 同负载对拍（base grading）。

ADR 0004 冻结的 G2 判定标准：
- SDR 8-bit 输出 ≤1/255 per channel（两侧均渲染到 RGBA16F，统一量化
  到 uint8 后逐通道比对，避免两种 GPU 量化器的 ±0.5LSB 相位噪声）；
- HDR ≤2e-3 rel（相对容差带 1.0 下限，与 triple golden 约定一致）。

等价性基准：GL 侧走生产 ``build_composition_shader``（fx 全关 →
apply_blur_sharpen 直通；Normal blend（mode 0）直通 graded；白 matte ×
opacity 1 → mix 因子 1）——其输出数学上 ≡ apply_grading 直出，与 MSL
``fs_composition``（采样 → grading → 输出）同构。

对拍方法学（ADR 0003）：同负载、同输入纹理（RGBA16F、同一份 fp16
字节）、逐像素比对。mid_detail/sharpness 用 2048 宽画布使
scale_factor = u_tex_size.x/2048 = 1.0（邻域采样落在整数 texel 上，
排除双线性权重实现差异）。GL 侧 wrap 设 CLAMP_TO_EDGE 对齐 Metal
sampler（默认 REPEAT 会在边缘 texel 上分叉）。

真机层无 Metal 设备整类跳过；结构层（emitter 覆盖率契约）总是运行。
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

W, H = 2048, 256  # scale_factor = 1.0 → mid/sharp 偏移为整数 texel


@pytest.mark.skipif(not MIGRATED_EFFECTS, reason="无已迁移效果")
class TestEmitterStructure:
    """结构层（无 Metal 也运行）：覆盖率契约与显式失败语义（G4）。"""

    def test_grading_is_migrated(self):
        assert "grading" in MIGRATED_EFFECTS

    def test_untranslated_effect_rejected(self):
        manifest = build_composition_uniform_manifest()
        # qualifier 尚无 composition MSL 翻译（matte 生成侧职责，归属待裁决）；
        # blur_sharpen/blend_modes 已迁移
        with pytest.raises(ValueError, match="qualifier"):
            emit_composition_msl(manifest, ["grading", "qualifier"])

    def test_emitted_source_contains_contract_pieces(self):
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, ["grading"])
        assert "struct CompositionParams" in src
        assert "float u_lift_x;" in src          # vec3 逐分量展开
        assert "int u_blend_mode;" in src        # int uniform → MSL int（bool 才是 uint）
        assert "vertex Raster vs_composition" in src
        assert "fragment float4 fs_composition" in src
        assert "msl_apply_grading(color, r.uv, params, u_input_tex, smp);" in src
        assert "return float4(color, tex_color.a);" in src

    def test_struct_dtype_layout(self):
        manifest = build_composition_uniform_manifest()
        dtype = manifest.to_numpy_dtype()
        # 4 字节紧排：snippet-owned vec2+vec4 打头，随后 u_opacity
        assert dtype.names[0] == "u_tex_size_x"
        assert dtype.names[2] == "u_visible_rect_x"
        assert dtype.names[6] == "u_opacity"
        assert dtype.fields["u_opacity"][1] == 24
        # int uniform 在 struct 中为 int 基型（<i4）
        assert dtype.fields["u_blend_mode"][0].base == np.int32
        assert dtype.fields["u_fx_enabled"][0].base == np.uint32  # bool → uint


# ---------------------------------------------------------------------------
# 真机层：G2 对拍
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
    """0..1 亮度斜坡 + 灰阶条 + 饱和色块（SDR 域）。"""
    img = np.zeros((H, W, 4), dtype=np.float32)
    img[..., :3] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    for row, g in zip(range(8, H, 24), (0.0, 0.018, 0.081, 0.18, 0.5, 0.9,
                                        1.0, 0.25, 0.75)):
        img[row:row + 8, :W // 2, :3] = g
    for col, c in zip(range(W // 2, W, W // 16),
                      ((0.859, 0.729, 0.604), (0.180, 0.412, 0.667),
                       (0.639, 0.200, 0.184), (0.204, 0.529, 0.290),
                       (0.878, 0.804, 0.067), (0.769, 0.584, 0.486),
                       (0.055, 0.055, 0.055), (0.945, 0.945, 0.945))):
        img[:, col:col + W // 32, :3] = c
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _pattern_hdr() -> np.ndarray:
    """斜坡 + 负值条 (-0.2..0) + HDR 条 (1.0..4.0) + 灰阶（HDR 域）。"""
    img = np.zeros((H, W, 4), dtype=np.float32)
    img[..., :3] = np.linspace(0.0, 1.0, W, dtype=np.float32)[None, :, None]
    neg = np.linspace(-0.2, 0.0, W // 4, dtype=np.float32)[None, :, None]
    hdr = np.linspace(1.0, 4.0, W // 4, dtype=np.float32)[None, :, None]
    img[:H // 4, :W // 4, :3] = neg
    img[:H // 4, W // 2:3 * W // 4, :3] = hdr
    img[H // 2:H // 2 + 16, :, :3] = 1.2
    img[H // 2 + 16:H // 2 + 32, :, :3] = -0.15
    img[..., 3] = 1.0
    return np.ascontiguousarray(img)


def _values_for(layer: dict) -> dict:
    return composition_uniform_values(
        layer, width=W, height=H, tex_size=(float(W), float(H)))


def _params_from_values(values: dict) -> np.ndarray:
    """manifest dtype 填充：vec → 逐分量（u_lift → u_lift_x/y/z）、
    bool → 0/1。注意 dtype 字段是展开后的名字，基名不在其中。"""
    dtype = build_composition_uniform_manifest().to_numpy_dtype()
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


# (名称, grading_params)——覆盖 apply_grading 全部分支
GL_CASES = [
    ("neutral", {}),
    ("exposure_temp_tint", {"exposure": 35.0, "temp": 18.0, "tint": -12.0}),
    ("contrast_pos", {"contrast": 30.0}),
    ("contrast_neg", {"contrast": -25.0}),
    ("lift_gamma_gain", {"lift": (25.0, 0.4, 0.15),
                         "gamma": (0.75, 1.35, 1.0),
                         "gain": (0.9, 1.15, 1.05)}),
    ("shadows_pos", {"shadow": 35.0}),
    ("shadows_negative_factor", {"shadow": -150.0}),  # factor<0 分支
    ("highlights_compress", {"highlights": -45.0}),
    ("highlights_boost", {"highlights": 35.0}),
    ("vibrance_saturation", {"vibrance": 45.0, "saturation": -55.0}),
    ("mid_detail_sharpness", {"mid_detail": 40.0, "sharpness": 50.0}),
    ("hue_shift", {"hue_shift": 30.0}),
    ("hue_shift_wrap", {"hue_shift": -160.0}),
    ("kitchen_sink", {"exposure": 20.0, "temp": -10.0, "contrast": 15.0,
                      "lift": (10.0, 0.2, 0.1), "gamma": (1.1, 0.9, 1.0),
                      "gain": (1.05, 1.0, 0.95), "shadow": 20.0,
                      "highlights": -25.0, "vibrance": 20.0,
                      "mid_detail": 25.0, "sharpness": 30.0,
                      "hue_shift": 12.0, "saturation": 15.0}),
]
HDR_CASES = {"neutral", "contrast_pos", "lift_gamma_gain",
             "mid_detail_sharpness", "kitchen_sink"}


@pytest.fixture(scope="class")
def metal_ctx():
    ctx = MetalComputeContext()
    yield ctx
    ctx.finish()


@pytest.fixture(scope="class")
def msl_pso(metal_ctx):
    """编译 emitter 输出并缓存 render pipeline state。"""
    import Metal
    dev = metal_ctx.device
    src = emit_composition_msl(
        build_composition_uniform_manifest(), ["grading"])
    lib, err = dev.newLibraryWithSource_options_error_(src, None, None)
    assert lib is not None, f"MSL 编译失败: {err}"
    desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
    desc.setVertexFunction_(lib.newFunctionWithName_("vs_composition"))
    desc.setFragmentFunction_(lib.newFunctionWithName_("fs_composition"))
    desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(
        mtl_pixel_format("rgba16float"))
    pso, perr = dev.newRenderPipelineStateWithDescriptor_error_(desc, None)
    assert pso is not None, f"pipeline 失败: {perr}"
    return pso


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


def _gl_render(gl, pipeline, pattern, values, hdr: bool) -> np.ndarray:
    prog = pipeline.build_composition_shader()
    assert prog is not None
    src = gl.texture((W, H), 4, dtype="f2")
    src.filter = (gl.LINEAR, gl.LINEAR)
    src.repeat_x = False  # False = CLAMP_TO_EDGE（对齐 Metal sampler）
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
        out /= 255.0  # RGBA8 读回是 0..255 整数，归一到 0..1
    for res in (src, matte, dst, fbo, quad, vao):
        try:
            res.release()
        except Exception:
            pass
    return out


def _metal_render(metal_ctx, msl_pso, pattern, values) -> np.ndarray:
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
    smp = dev.newSamplerStateWithDescriptor_(sdesc)

    pbuf = metal_ctx.create_buffer_from_numpy(_params_from_values(values))

    rpd = Metal.MTLRenderPassDescriptor.renderPassDescriptor()
    ca = rpd.colorAttachments().objectAtIndexedSubscript_(0)
    ca.setTexture_(dst)
    ca.setLoadAction_(Metal.MTLLoadActionDontCare)
    ca.setStoreAction_(Metal.MTLStoreActionStore)
    cb = metal_ctx._queue.commandBuffer()
    enc = cb.renderCommandEncoderWithDescriptor_(rpd)
    enc.setRenderPipelineState_(msl_pso)
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


@pytest.mark.skipif(
    not _HAS_METAL_IMPORTS or not _Ctx.is_supported(), reason="无 Metal 设备")
class TestGradingParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    @pytest.mark.parametrize("name,params", GL_CASES, ids=[c[0] for c in GL_CASES])
    def test_sdr_within_1_lsb(self, gl, pipeline, metal_ctx, msl_pso,
                              sdr_pattern, name, params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params}
        values = _values_for(layer)
        gl_out = _gl_render(gl, pipeline, sdr_pattern, values, hdr=False)
        mt_out = _metal_render(metal_ctx, msl_pso, sdr_pattern, values)
        _assert_sdr(gl_out, mt_out, f"SDR/{name}")

    @pytest.mark.parametrize("name,params",
                             [c for c in GL_CASES if c[0] in HDR_CASES],
                             ids=[c[0] for c in GL_CASES if c[0] in HDR_CASES])
    def test_hdr_within_2e3_rel(self, gl, pipeline, metal_ctx, msl_pso,
                                hdr_pattern, name, params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params}
        values = _values_for(layer)
        gl_out = _gl_render(gl, pipeline, hdr_pattern, values, hdr=True)
        mt_out = _metal_render(metal_ctx, msl_pso, hdr_pattern, values)
        _assert_hdr(gl_out, mt_out, f"HDR/{name}")
