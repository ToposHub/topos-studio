"""P7-03 第二批 G2 门禁：curves / hsl_curve / lut3d 的 MSL vs GL 对拍。

继承 :mod:`tests.color.test_metal_composition_grading` 的方法学（同负载、
同输入纹理 fp16 字节、逐像素比对；SDR ≤1/255、HDR ≤2e-3 rel）。本批
新增的纹理 fixture 与生产绑定形态一致：

- curves： ``(256, 1)`` 3 通道 float32（async_renderer._create_defaults
  的 default_curves_tex 同布局），LINEAR + clamp；
- hsl_curve： ``(6, 256)`` 1 通道 float32（default_hsl_curve_tex 同布局，
  数据 (256, 6) 转置存放），LINEAR + clamp——注意 GL 侧必须 clamp：
  生产默认 REPEAT 在 t=1.0 处会混入 row 0（Metal 端 ClampToEdge 无此
  语义），wrap 策略属于绑定期对齐项（P6-02 消费时核对），不属于
  翻译契约；
- lut3d： 16³ sampler3D（生产 texture3d f4 同布局），LINEAR + clamp。

用例覆盖：区间采样、线性外推（x<0 / x>1）、暗部/高光软性保护、
RGB 通道平衡校正、HSL 六曲线（色相环绕 / 饱亮度乘子）、LUT 三线性
采样 + intensity 混合 + HDR 比例缩放 + 负值加法偏移，以及四效果
串联的管线顺序（grading → curves → hsl_curve → lut3d）。

Metal 纹理槽位契约（msl_composition._EFFECT_TEXTURE_ARGS）：
texture(0)=input / texture(1)=curves / texture(2)=hsl / texture(3)=lut3d。
32 位 float 纹理在 Metal 侧用 RGBA32Float（无 RGB32Float）/ R32Float，
GL 侧保持生产 3/1 通道布局——采样只取 .rgb/.r，两侧 texel 值相同。
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

W, H = 2048, 256

# 管线变体：manifest 旗标（GL 侧）与 active_effects（Metal 侧）必须成对。
# GL composition main 恒定先调 apply_grading（fx 关闭直通的 blur_sharpen
# 之后的第一个效果位）——因此 Metal 侧每个变体也必须包含 "grading"
# （中性参数时恒等，已在 grading 套件证明）。
VARIANTS = {
    "curves": dict(flags=dict(has_curves=True),
                   effects=("grading", "curves")),
    "hsl": dict(flags=dict(has_hsl_curve=True),
                effects=("grading", "hsl_curve")),
    "lut": dict(flags=dict(has_lut3d=True),
                effects=("grading", "lut3d")),
    "all": dict(flags=dict(has_curves=True, has_hsl_curve=True,
                           has_lut3d=True),
                effects=("grading", "curves", "hsl_curve", "lut3d")),
}
# 变体 → 该管线绑定的辅助纹理（Metal 槽位见 msl_composition 契约）
_VARIANT_AUX = {
    "curves": ("u_curves_tex",),
    "hsl": ("u_hsl_curve_tex",),
    "lut": ("u_lut_tex",),
    "all": ("u_curves_tex", "u_hsl_curve_tex", "u_lut_tex"),
}
_METAL_SLOT = {"u_curves_tex": 1, "u_hsl_curve_tex": 2, "u_lut_tex": 3}


# ---------------------------------------------------------------------------
# 结构层（无 Metal 也运行）
# ---------------------------------------------------------------------------


class TestEmitterStructure:
    def test_new_effects_are_migrated(self):
        for effect in ("curves", "hsl_curve", "lut3d"):
            assert effect in MIGRATED_EFFECTS

    def test_untranslated_effect_still_rejected(self):
        manifest = build_composition_uniform_manifest()
        # qualifier 尚无 composition MSL 翻译（matte 生成侧职责，归属待裁决）；
        # blur_sharpen/blend_modes 已迁移
        with pytest.raises(ValueError, match="qualifier"):
            emit_composition_msl(manifest, ["curves", "qualifier"])

    def test_fragment_signature_has_texture_slots(self):
        manifest = build_composition_uniform_manifest(
            has_curves=True, has_hsl_curve=True, has_lut3d=True)
        src = emit_composition_msl(
            manifest, ("grading", "curves", "hsl_curve", "lut3d"))
        assert "texture2d<float> u_curves_tex [[texture(1)]]" in src
        assert "texture2d<float> u_hsl_curve_tex [[texture(2)]]" in src
        assert "texture3d<float> u_lut_tex [[texture(3)]]" in src
        assert "msl_apply_curves(color, u_curves_tex, smp);" in src
        assert "msl_apply_hsl_curve(color, u_hsl_curve_tex, smp);" in src
        assert ("msl_apply_3dlut(color, u_lut_tex, smp, "
                "params.u_lut_intensity);") in src

    def test_grading_only_emission_unchanged(self):
        """grading 单独发射仍无多余纹理参数（既有批次回归钉子）。"""
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, ["grading"])
        assert "[[texture(1)]]" not in src
        assert "texture3d" not in src


# ---------------------------------------------------------------------------
# 纹理 fixture 数据（与生产布局一致）
# ---------------------------------------------------------------------------


def _curves_lut_data() -> np.ndarray:
    """(1, 256, 3)：r 通道黑端陡升（触发暗部外推/guard）、g 通道白端
    陡升（触发高光 guard）、b 通道轻微抬升——SDR 域内光滑，外推分支
    只由 HDR 输入激活。"""
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    r = np.interp(x, [0.0, 2.0 / 255.0, 1.0], [0.45, 0.85, 1.0])
    g = np.interp(x, [0.0, 253.0 / 255.0, 1.0], [0.0, 0.4, 1.0])
    b = 0.04 + 0.96 * x
    lut = np.stack([r, g, b], axis=-1)[None, ...]
    return np.ascontiguousarray(lut, dtype=np.float32)


def _hsl_lut_data() -> np.ndarray:
    """(6, 256) 曲线值，存储为 (256, 6)（生产 default_hsl_curve_tex 的
    转置布局）。光滑非恒等：亮度→饱和乘子、色相偏移、色相/饱和→饱和、
    色相/饱和→亮度全启用。"""
    hsl = np.ones((6, 256), dtype=np.float32)
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    hsl[0] = 1.0 + 0.35 * np.sin(2.0 * np.pi * x)
    hsl[1] = 0.08 * np.sin(2.0 * np.pi * x + 0.7)
    hsl[2] = 1.0 + 0.25 * np.sin(4.0 * np.pi * x)
    hsl[3] = 1.0 - 0.15 * np.sin(2.0 * np.pi * x)
    hsl[4] = 1.0 + 0.3 * (x - 0.5)
    hsl[5] = 1.0 - 0.1 * (x - 0.5)
    return np.ascontiguousarray(hsl.T, dtype=np.float32)


_LUT3D_N = 16


def _lut3d_data() -> np.ndarray:
    """(16, 16, 16, 3)：数据轴序 (depth=b, height=g, width=r)——采样坐标
    (r, g, b) → (x=宽, y=高, z=深)，与 GL texture3d / Metal texture3d
    的内存布局一致。内容：通道串扰 + 温和 S 曲线的非恒等变换。"""
    g = np.linspace(0.0, 1.0, _LUT3D_N, dtype=np.float32)
    B, G, R = np.meshgrid(g, g, g, indexing="ij")

    def _scurve(v: np.ndarray) -> np.ndarray:
        return v + 0.12 * (v * v * (3.0 - 2.0 * v) - v)

    rr = R + 0.10 * (G - 0.5)
    gg = G + 0.06 * (B - 0.5)
    bb = B + 0.08 * (R - 0.5)
    lut = np.stack([_scurve(rr), _scurve(gg), _scurve(bb)], axis=-1)
    return np.ascontiguousarray(np.clip(lut, 0.0, 1.0), dtype=np.float32)


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


# (名称, grading_params, lut_intensity)
CURVES_SDR_CASES = [
    ("film_curve", {}, 1.0),
    ("with_grading", {"exposure": 15.0, "contrast": 12.0,
                      "saturation": 10.0}, 1.0),
]
CURVES_HDR_CASES = [
    ("extrapolation_guards", {}, 1.0),
    ("guards_with_grading", {"lift": (12.0, 0.0, -8.0),
                             "gain": (1.08, 1.0, 0.94)}, 1.0),
]
HSL_SDR_CASES = [
    ("six_curves", {}, 1.0),
    ("with_grading", {"hue_shift": 20.0, "saturation": -10.0}, 1.0),
]
HSL_HDR_CASES = [("hdr_input", {}, 1.0)]
LUT_SDR_CASES = [
    ("intensity_070", {}, 0.7),
    ("intensity_100", {}, 1.0),
]
LUT_HDR_CASES = [("ratio_preserve", {}, 1.0)]
ALL_SDR_CASES = [
    ("kitchen_sink", {"exposure": 18.0, "contrast": 14.0,
                      "lift": (8.0, 0.1, 0.05), "gamma": (1.05, 0.95, 1.0),
                      "gain": (1.02, 1.0, 0.98), "shadow": 15.0,
                      "highlights": -18.0, "vibrance": 12.0,
                      "mid_detail": 15.0, "sharpness": 20.0,
                      "hue_shift": 10.0, "saturation": 12.0}, 0.85),
]
ALL_HDR_CASES = [
    ("kitchen_sink_hdr", {"exposure": -12.0, "contrast": 20.0,
                          "shadow": 25.0, "highlights": -30.0,
                          "vibrance": -20.0, "hue_shift": 45.0,
                          "saturation": 18.0}, 1.0),
]


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
    """四个管线变体的 GL program（与 Metal PSO 一一对应）。"""
    out = {}
    for name, cfg in VARIANTS.items():
        prog = pipeline.build_composition_shader(**cfg["flags"])
        assert prog is not None, f"GL 编译失败: {name}"
        out[name] = prog
    return out


@pytest.fixture(scope="module")
def gl_aux(gl):
    curves = gl.texture((256, 1), 3, data=_curves_lut_data().tobytes(),
                        dtype="f4")
    curves.filter = (gl.LINEAR, gl.LINEAR)
    curves.repeat_x = curves.repeat_y = False
    hsl = gl.texture((6, 256), 1, data=_hsl_lut_data().tobytes(), dtype="f4")
    hsl.filter = (gl.LINEAR, gl.LINEAR)
    hsl.repeat_x = hsl.repeat_y = False
    lut = gl.texture3d((_LUT3D_N,) * 3, 3, data=_lut3d_data().tobytes(),
                       dtype="f4")
    lut.filter = (gl.LINEAR, gl.LINEAR)
    lut.repeat_x = lut.repeat_y = lut.repeat_z = False
    yield {"u_curves_tex": curves, "u_hsl_curve_tex": hsl,
           "u_lut_tex": lut}
    for tex in (curves, hsl, lut):
        tex.release()


@pytest.fixture(scope="module")
def psos(metal_ctx):
    """四个管线变体的 Metal render pipeline state。"""
    import Metal
    dev = metal_ctx.device
    out = {}
    for name, cfg in VARIANTS.items():
        manifest = build_composition_uniform_manifest(**cfg["flags"])
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
        out[name] = pso
    return out


def _create_texture3d(dev, metal_ctx, pixel_format, data: np.ndarray, n: int):
    metal = metal_ctx._metal
    desc = metal.MTLTextureDescriptor.alloc().init()
    desc.setTextureType_(metal.MTLTextureType3D)
    desc.setPixelFormat_(pixel_format)
    desc.setWidth_(n)
    desc.setHeight_(n)
    desc.setDepth_(n)
    desc.setUsage_(metal.MTLTextureUsageShaderRead)
    if bool(dev.hasUnifiedMemory()):
        desc.setStorageMode_(metal.MTLStorageModeShared)
    else:
        desc.setStorageMode_(metal.MTLStorageModeManaged)
    tex = dev.newTextureWithDescriptor_(desc)
    assert tex is not None, "texture3D 创建失败"
    bytes_per_row = n * 16  # RGBA32Float
    tex.replaceRegion_mipmapLevel_slice_withBytes_bytesPerRow_bytesPerImage_(
        metal.MTLRegionMake3D(0, 0, 0, n, n, n), 0, 0,
        data.tobytes(), bytes_per_row, bytes_per_row * n)
    return tex


@pytest.fixture(scope="module")
def metal_aux(metal_ctx):
    """Metal 侧辅助纹理（槽位映射见 _METAL_SLOT）。32 位 float 在 Metal
    没有 RGB32Float——用 RGBA32Float/R32Float，rgb 分量与 GL 侧数据一致。"""
    import Metal
    dev = metal_ctx.device
    fmt_rgba32 = Metal.MTLPixelFormatRGBA32Float
    fmt_r32 = Metal.MTLPixelFormatR32Float

    curves = np.zeros((1, 256, 4), dtype=np.float32)
    curves[..., :3] = _curves_lut_data()
    curves_tex = metal_ctx.create_texture_from_numpy(
        np.ascontiguousarray(curves), fmt_rgba32, 256, 1)

    hsl = _hsl_lut_data()[..., None]
    hsl_tex = metal_ctx.create_texture_from_numpy(
        np.ascontiguousarray(hsl), fmt_r32, 6, 256)

    lut = np.zeros((_LUT3D_N,) * 3 + (4,), dtype=np.float32)
    lut[..., :3] = _lut3d_data()
    lut_tex = _create_texture3d(dev, metal_ctx, fmt_rgba32, lut, _LUT3D_N)

    yield {"u_curves_tex": curves_tex, "u_hsl_curve_tex": hsl_tex,
           "u_lut_tex": lut_tex}


def _gl_render(gl, prog, aux_names, aux, pattern, values, hdr: bool) -> np.ndarray:
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
    for name in aux_names:
        aux[name].use(_AUX_UNITS[name])
        prog[name].value = _AUX_UNITS[name]
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


_AUX_UNITS = {"u_curves_tex": 2, "u_hsl_curve_tex": 3, "u_lut_tex": 4}


def _metal_render(metal_ctx, pso, pattern, values, dtype, aux_names,
                  metal_aux) -> np.ndarray:
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
    for name in aux_names:
        enc.setFragmentTexture_atIndex_(metal_aux[name], _METAL_SLOT[name])
    enc.setFragmentBuffer_offset_atIndex_(pbuf, 0, 0)
    enc.drawPrimitives_vertexStart_vertexCount_(
        Metal.MTLPrimitiveTypeTriangle, 0, 6)
    enc.endEncoding()
    cb.commit()
    cb.waitUntilCompleted()
    assert cb.error() is None, f"command buffer error: {cb.error()}"
    return read_texture_rgba16f(metal_ctx, dst, W, H)


@requires_metal
class TestCurvesParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
             pattern, hdr, params, lut_intensity):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params, "lut_intensity": lut_intensity}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["curves"], _VARIANT_AUX["curves"],
                            gl_aux, pattern, values, hdr)
        cfg = VARIANTS["curves"]
        dtype = build_composition_uniform_manifest(**cfg["flags"]) \
            .to_numpy_dtype()
        mt_out = _metal_render(metal_ctx, psos["curves"], pattern, values,
                               dtype, _VARIANT_AUX["curves"], metal_aux)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,params,lut_i", CURVES_SDR_CASES,
                             ids=[c[0] for c in CURVES_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 sdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, sdr_pattern, False, params,
                                   lut_i)
        _assert_sdr(gl_out, mt_out, f"curves/SDR/{name}")

    @pytest.mark.parametrize("name,params,lut_i", CURVES_HDR_CASES,
                             ids=[c[0] for c in CURVES_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 hdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, hdr_pattern, True, params,
                                   lut_i)
        _assert_hdr(gl_out, mt_out, f"curves/HDR/{name}")


@requires_metal
class TestHslCurveParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
             pattern, hdr, params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params, "lut_intensity": 1.0}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["hsl"], _VARIANT_AUX["hsl"],
                            gl_aux, pattern, values, hdr)
        cfg = VARIANTS["hsl"]
        dtype = build_composition_uniform_manifest(**cfg["flags"]) \
            .to_numpy_dtype()
        mt_out = _metal_render(metal_ctx, psos["hsl"], pattern, values,
                               dtype, _VARIANT_AUX["hsl"], metal_aux)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,params,lut_i", HSL_SDR_CASES,
                             ids=[c[0] for c in HSL_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 sdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, sdr_pattern, False, params)
        _assert_sdr(gl_out, mt_out, f"hsl/SDR/{name}")

    @pytest.mark.parametrize("name,params,lut_i", HSL_HDR_CASES,
                             ids=[c[0] for c in HSL_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 hdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, hdr_pattern, True, params)
        _assert_hdr(gl_out, mt_out, f"hsl/HDR/{name}")


@requires_metal
class TestLut3dParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
             pattern, hdr, params, lut_intensity):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params, "lut_intensity": lut_intensity}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["lut"], _VARIANT_AUX["lut"],
                            gl_aux, pattern, values, hdr)
        cfg = VARIANTS["lut"]
        dtype = build_composition_uniform_manifest(**cfg["flags"]) \
            .to_numpy_dtype()
        mt_out = _metal_render(metal_ctx, psos["lut"], pattern, values,
                               dtype, _VARIANT_AUX["lut"], metal_aux)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,params,lut_i", LUT_SDR_CASES,
                             ids=[c[0] for c in LUT_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 sdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, sdr_pattern, False, params,
                                   lut_i)
        _assert_sdr(gl_out, mt_out, f"lut3d/SDR/{name}")

    @pytest.mark.parametrize("name,params,lut_i", LUT_HDR_CASES,
                             ids=[c[0] for c in LUT_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 hdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, hdr_pattern, True, params,
                                   lut_i)
        _assert_hdr(gl_out, mt_out, f"lut3d/HDR/{name}")


@requires_metal
class TestCombinedParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
             pattern, hdr, params, lut_intensity):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": params, "lut_intensity": lut_intensity}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_progs["all"], _VARIANT_AUX["all"],
                            gl_aux, pattern, values, hdr)
        cfg = VARIANTS["all"]
        dtype = build_composition_uniform_manifest(**cfg["flags"]) \
            .to_numpy_dtype()
        mt_out = _metal_render(metal_ctx, psos["all"], pattern, values,
                               dtype, _VARIANT_AUX["all"], metal_aux)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,params,lut_i", ALL_SDR_CASES,
                             ids=[c[0] for c in ALL_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 sdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, sdr_pattern, False, params,
                                   lut_i)
        _assert_sdr(gl_out, mt_out, f"all/SDR/{name}")

    @pytest.mark.parametrize("name,params,lut_i", ALL_HDR_CASES,
                             ids=[c[0] for c in ALL_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_aux, metal_ctx, psos, metal_aux,
                 hdr_pattern, name, params, lut_i):
        gl_out, mt_out = self._run(gl, gl_progs, gl_aux, metal_ctx, psos,
                                   metal_aux, hdr_pattern, True, params,
                                   lut_i)
        _assert_hdr(gl_out, mt_out, f"all/HDR/{name}")
