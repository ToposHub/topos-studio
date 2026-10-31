"""P7-03 第五批 G2 门禁：blend modes 的 MSL vs GL 对拍。

继承既有 G2 方法学（SDR ≤1/255、HDR ≤2e-3 rel）。blend modes 无 GL
编译旗标——composition main 恒定以 ``apply_blend_mode(原色, 处理结果,
mode)`` 收尾并 ``mix(mask * opacity)``；本批迁移后 MSL emitter 补齐
main 尾部语义（matte 采样 texture(5) + blend epilog），对拍因此可以
覆盖**真实混合路径**（不再依赖 Normal+白 matte+opacity 1 的等价基准）：

- 19 种混合模式（Layer.BLEND_MODES 0~18）× SDR 全覆盖；
- 部分透明 + 渐变 matte（mix 权重非平凡）；
- HDR 域选取 Screen/Color Dodge/Hue/Addition（除法与 HSV 路径）。

mode 0（Normal）为直通分支：base = blend_layer 返回处理结果——既有
套件的等价基准即此特例。
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
from src.features.color.core.layer import Layer  # noqa: E402
from src.features.color.core.msl_composition import (  # noqa: E402
    MIGRATED_EFFECTS,
    emit_composition_msl,
)
from src.features.color.core.shader_compiler import ShaderPipeline  # noqa: E402

W, H = 2048, 256

EFFECTS = ("grading", "blend_modes")
BLEND_NAMES = list(Layer.BLEND_MODES)
assert len(BLEND_NAMES) == 19


# ---------------------------------------------------------------------------
# 结构层（无 Metal 也运行）
# ---------------------------------------------------------------------------


class TestEmitterStructure:
    def test_blend_modes_is_migrated(self):
        assert "blend_modes" in MIGRATED_EFFECTS

    def test_untranslated_effect_still_rejected(self):
        manifest = build_composition_uniform_manifest()
        with pytest.raises(ValueError, match="qualifier"):
            emit_composition_msl(manifest, ["blend_modes", "qualifier"])

    def test_emitted_source_contains_blend_epilog(self):
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, EFFECTS)
        assert "texture2d<float> u_matte_tex [[texture(5)]]" in src
        assert "float mask = u_matte_tex.sample(smp, r.uv).r;" in src
        assert ("msl_apply_blend_mode(tex_color.rgb, color, "
                "params.u_blend_mode);") in src
        assert "mix(tex_color.rgb, blended, mask * params.u_opacity);" in src

    def test_no_blend_epilog_without_effect(self):
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, ["grading"])
        assert "msl_apply_blend_mode" not in src
        assert "[[texture(5)]]" not in src


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


def _matte_white() -> np.ndarray:
    m = np.ones((H, W, 4), dtype=np.float16)
    return np.ascontiguousarray(m)


def _matte_gradient() -> np.ndarray:
    m = np.zeros((H, W, 4), dtype=np.float16)
    m[..., 0] = np.linspace(0.15, 0.95, W, dtype=np.float32)[None, :]
    m[..., 3] = 1.0
    return np.ascontiguousarray(m)


def _values_for(layer: dict) -> dict:
    return composition_uniform_values(
        layer, width=W, height=H, tex_size=(float(W), float(H)))


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


# (模式名, opacity, 用渐变 matte)
SDR_CASES = [(name, 1.0, False) for name in BLEND_NAMES] + [
    ("Overlay+渐变matte+opacity0.7", 0.7, True),
]
HDR_MODE_NAMES = ["Screen", "Color Dodge", "Hue", "Addition"]
HDR_CASES = [(name, 1.0, False) for name in HDR_MODE_NAMES]


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
def gl_prog(pipeline):
    # blend modes 无编译旗标（main 尾部恒定存在）
    prog = pipeline.build_composition_shader()
    assert prog is not None
    return prog


@pytest.fixture(scope="module")
def msl_setup(metal_ctx):
    import Metal
    dev = metal_ctx.device
    manifest = build_composition_uniform_manifest()
    src = emit_composition_msl(manifest, EFFECTS)
    lib, err = dev.newLibraryWithSource_options_error_(src, None, None)
    assert lib is not None, f"MSL 编译失败: {err}"
    desc = Metal.MTLRenderPipelineDescriptor.alloc().init()
    desc.setVertexFunction_(lib.newFunctionWithName_("vs_composition"))
    desc.setFragmentFunction_(lib.newFunctionWithName_("fs_composition"))
    desc.colorAttachments().objectAtIndexedSubscript_(0).setPixelFormat_(
        mtl_pixel_format("rgba16float"))
    pso, perr = dev.newRenderPipelineStateWithDescriptor_error_(desc, None)
    assert pso is not None, f"pipeline 失败: {perr}"
    return pso, manifest.to_numpy_dtype()


def _gl_render(gl, prog, pattern, matte, values, hdr: bool = False) -> np.ndarray:
    src = gl.texture((W, H), 4, dtype="f2")
    src.filter = (gl.LINEAR, gl.LINEAR)
    src.repeat_x = False
    src.repeat_y = False
    src.write(pattern.astype(np.float16).tobytes())
    matte_tex = gl.texture((W, H), 4, dtype="f2")
    matte_tex.filter = (gl.LINEAR, gl.LINEAR)
    matte_tex.repeat_x = False
    matte_tex.repeat_y = False
    matte_tex.write(matte.tobytes())
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
    matte_tex.use(1)
    prog["u_input_tex"].value = 0
    prog["u_matte_tex"].value = 1
    vao.render(moderngl.TRIANGLE_STRIP)
    raw = fbo.read(components=4, dtype="f2" if hdr else "f1")
    out = np.frombuffer(raw, dtype="<f2" if hdr else "uint8").reshape(
        H, W, 4).astype(np.float32)
    if not hdr:
        out /= 255.0
    for res in (src, matte_tex, dst, fbo, quad, vao):
        try:
            res.release()
        except Exception:
            pass
    return out


def _metal_render(metal_ctx, pso, pattern, matte, values, dtype) -> np.ndarray:
    import Metal
    dev = metal_ctx.device
    fmt = mtl_pixel_format("rgba16float")
    rgba = np.empty((H, W, 4), dtype=np.float16)
    rgba[..., :3] = pattern.astype(np.float16)[..., :3]
    rgba[..., 3] = 1.0
    src = metal_ctx.create_texture_from_numpy(rgba, fmt, W, H)
    matte_tex = metal_ctx.create_texture_from_numpy(matte, fmt, W, H)
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
    enc.setFragmentTexture_atIndex_(matte_tex, 5)
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
class TestBlendModesParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_prog, msl_setup, metal_ctx, pattern, matte,
             blend_name, opacity, hdr: bool = False,
             grading_params=None):
        layer = {"opacity": opacity, "blend_mode": blend_name,
                 "grading_params": grading_params or {}}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_prog, pattern, matte, values, hdr)
        pso, dtype = msl_setup
        mt_out = _metal_render(metal_ctx, pso, pattern, matte, values, dtype)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,opacity,grad_matte", SDR_CASES,
                             ids=[c[0] for c in SDR_CASES])
    def test_sdr(self, gl, gl_prog, msl_setup, metal_ctx, sdr_pattern,
                 name, opacity, grad_matte):
        matte = _matte_gradient() if grad_matte else _matte_white()
        gl_out, mt_out = self._run(gl, gl_prog, msl_setup, metal_ctx,
                                   sdr_pattern, matte, name, opacity)
        _assert_sdr(gl_out, mt_out, f"blend/SDR/{name}")

    @pytest.mark.parametrize("name,opacity,grad_matte", HDR_CASES,
                             ids=[c[0] for c in HDR_CASES])
    def test_hdr(self, gl, gl_prog, msl_setup, metal_ctx, hdr_pattern,
                 name, opacity, grad_matte):
        matte = _matte_white()
        gl_out, mt_out = self._run(gl, gl_prog, msl_setup, metal_ctx,
                                   hdr_pattern, matte, name, opacity, hdr=True)
        _assert_hdr(gl_out, mt_out, f"blend/HDR/{name}")
