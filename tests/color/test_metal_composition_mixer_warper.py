"""P7-03 第三批 G2 门禁：mixer / warper 的 MSL vs GL 对拍。

继承既有 G2 方法学（同负载、同输入纹理 fp16 字节、逐像素比对；
SDR ≤1/255、HDR ≤2e-3 rel）。要点：

- mixer：纯 uniform 效果（8 通道 vec3 = struct 平铺分量，调用点重建
  float3）；色相环 8 中心 mask 相互重叠（30° 作用半径），SDR 图案的
  连续色相斜坡天然覆盖重叠区；HDR 用例钉住 ``sign(color)`` 保持负值
  色相的语义（abs(color) 进 HSV、sign 还原）。
- warper：纹理 fixture 与生产布局一致——(w, h) 2 通道 float32
  （delta_hue, delta_sat），LINEAR + clamp；``warper_params`` 传真实
  numpy 数组（extractor 读 ``shape[0]`` 作 grid_size）。饱和度
  <0.02 早退分支由图案中的灰阶条覆盖。

管线顺序（GL composition main）：… → mixer → warper → lut3d——
"both" 变体按此顺序串联两个效果。
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

# has_mixer 不是 manifest 旗标（8 通道 vec3 恒在基础声明中）——它只门
# GL main 的调用位；Metal 侧对应物是 active_effects。因此 GL 构建旗标
# （gl）与 manifest 旗标（manifest）必须分开指定。
VARIANTS = {
    "mixer": dict(gl=dict(has_mixer=True), manifest=dict(),
                  effects=("grading", "mixer")),
    "warper": dict(gl=dict(has_warper=True), manifest=dict(has_warper=True),
                   effects=("grading", "warper")),
    "both": dict(gl=dict(has_mixer=True, has_warper=True),
                 manifest=dict(has_warper=True),
                 effects=("grading", "mixer", "warper")),
}
_AUX_NAMES = {
    "mixer": (),
    "warper": ("u_warper_tex",),
    "both": ("u_warper_tex",),
}
_GL_UNIT = {"u_warper_tex": 2}
_METAL_SLOT = {"u_warper_tex": 4}


# ---------------------------------------------------------------------------
# 结构层（无 Metal 也运行）
# ---------------------------------------------------------------------------


class TestEmitterStructure:
    def test_new_effects_are_migrated(self):
        for effect in ("mixer", "warper"):
            assert effect in MIGRATED_EFFECTS

    def test_untranslated_effect_still_rejected(self):
        manifest = build_composition_uniform_manifest()
        # qualifier 尚无 composition MSL 翻译（matte 生成侧职责，归属待裁决）；
        # blur_sharpen/blend_modes 已迁移
        with pytest.raises(ValueError, match="qualifier"):
            emit_composition_msl(manifest, ["mixer", "qualifier"])

    def test_emitted_source_contains_contract_pieces(self):
        manifest = build_composition_uniform_manifest(has_warper=True)
        src = emit_composition_msl(manifest, ("grading", "mixer", "warper"))
        assert "texture2d<float> u_warper_tex [[texture(4)]]" in src
        assert "msl_apply_mixer(color, float3(params.u_red_p_x," in src
        assert ("msl_apply_warper(color, u_warper_tex, smp, "
                "params.u_warper_grid_size);") in src

    def test_mixer_call_rebuilds_all_8_channels(self):
        # mixer 无 manifest 旗标（8 通道 vec3 恒在基础声明中）
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, ("mixer",))
        for field in ("u_red_p", "u_orange_p", "u_yellow_p", "u_green_p",
                      "u_aqua_p", "u_blue_p", "u_purple_p", "u_magenta_p"):
            assert f"float3(params.{field}_x, params.{field}_y, params.{field}_z)" in src


# ---------------------------------------------------------------------------
# 纹理 fixture（与生产布局一致：(w, h) 2 通道 f4）
# ---------------------------------------------------------------------------

_WARPER_N = 16


def _warper_lut_data() -> np.ndarray:
    """(16, 16, 2)：[j, i] 采样坐标 (u=i/15, v=j/15)——x=宽度轴存
    delta_hue、y=高度轴对应 hsv.y。光滑非零，驱动色相/饱和双向偏移。"""
    u = np.linspace(0.0, 1.0, _WARPER_N, dtype=np.float32)
    uu, vv = np.meshgrid(u, u)  # shape (h, w)：行=j(v)，列=i(u)
    delta_hue = 0.06 * np.sin(2.0 * np.pi * uu) * np.cos(np.pi * vv)
    delta_sat = 0.18 * (vv - 0.5)
    data = np.stack([delta_hue, delta_sat], axis=-1)
    return np.ascontiguousarray(data, dtype=np.float32)


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


# (名称, mixer_params, warper_enabled, grading_params)
MIXER_SDR_CASES = [
    ("channel_select", {"red": (30.0, 0.0, 0.0), "yellow": (0.0, 20.0, 0.0),
                        "blue": (0.0, 0.0, -15.0)}, False, {}),
    ("all_channels", {"red": (12.0, 8.0, 0.0), "orange": (-10.0, 0.0, 5.0),
                      "yellow": (0.0, 15.0, -6.0), "green": (8.0, -12.0, 0.0),
                      "aqua": (0.0, 10.0, 8.0), "blue": (-15.0, 0.0, -8.0),
                      "purple": (10.0, 12.0, 0.0),
                      "magenta": (0.0, -10.0, 12.0)}, False, {}),
    ("with_grading", {"red": (20.0, 10.0, 0.0), "green": (0.0, -15.0, 10.0)},
     False, {"hue_shift": 25.0, "saturation": 10.0}),
]
MIXER_HDR_CASES = [
    ("hdr_sign_preserve", {"red": (25.0, 0.0, 10.0),
                           "blue": (0.0, 15.0, -10.0)}, False, {}),
]
WARPER_SDR_CASES = [
    ("warp_saturated", {}, True, {}),
    ("with_grading", {}, True, {"hue_shift": 30.0, "vibrance": 15.0}),
]
WARPER_HDR_CASES = [
    ("hdr_input", {}, True, {}),
]
BOTH_SDR_CASES = [
    ("mixer_then_warper", {"red": (18.0, 10.0, 0.0), "aqua": (0.0, -12.0, 8.0)},
     True, {"saturation": 8.0}),
]
BOTH_HDR_CASES = [
    ("both_hdr", {"red": (15.0, 10.0, 5.0)}, True, {"contrast": 12.0}),
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
    out = {}
    for name, cfg in VARIANTS.items():
        prog = pipeline.build_composition_shader(**cfg["gl"])
        assert prog is not None, f"GL 编译失败: {name}"
        out[name] = prog
    return out


@pytest.fixture(scope="module")
def gl_warper_tex(gl):
    tex = gl.texture((_WARPER_N, _WARPER_N), 2,
                     data=_warper_lut_data().tobytes(), dtype="f4")
    tex.filter = (gl.LINEAR, gl.LINEAR)
    tex.repeat_x = tex.repeat_y = False
    yield tex
    tex.release()


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


@pytest.fixture(scope="module")
def metal_warper_tex(metal_ctx):
    import Metal
    # Metal 无 RG32Float 之外的 2 通道等价物问题：RG32Float 存在且可过滤
    tex = metal_ctx.create_texture_from_numpy(
        _warper_lut_data(), Metal.MTLPixelFormatRG32Float,
        _WARPER_N, _WARPER_N)
    return tex


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
        aux[name].use(_GL_UNIT[name])
        prog[name].value = _GL_UNIT[name]
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
class _ParityBase:
    """共享驱动：variant 名由类属性给出。"""

    VARIANT = "mixer"

    def _run(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
             metal_warper_tex, pattern, hdr, mixer_params, use_warper,
             grading_params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": grading_params,
                 "mixer_params": mixer_params,
                 "warper_params": (_warper_lut_data() if use_warper else None)}
        values = _values_for(layer)
        gl_aux = {"u_warper_tex": gl_warper_tex}
        gl_out = _gl_render(gl, gl_progs[self.VARIANT],
                            _AUX_NAMES[self.VARIANT], gl_aux, pattern,
                            values, hdr)
        pso, dtype = psos[self.VARIANT]
        metal_aux = {"u_warper_tex": metal_warper_tex}
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype,
                               _AUX_NAMES[self.VARIANT], metal_aux)
        return gl_out, mt_out


@requires_metal
class TestMixerParity(_ParityBase):
    VARIANT = "mixer"

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    @pytest.mark.parametrize("name,mixer,warper,grading", MIXER_SDR_CASES,
                             ids=[c[0] for c in MIXER_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, sdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, sdr_pattern,
                                   False, mixer, warper, grading)
        _assert_sdr(gl_out, mt_out, f"mixer/SDR/{name}")

    @pytest.mark.parametrize("name,mixer,warper,grading", MIXER_HDR_CASES,
                             ids=[c[0] for c in MIXER_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, hdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, hdr_pattern,
                                   True, mixer, warper, grading)
        _assert_hdr(gl_out, mt_out, f"mixer/HDR/{name}")


@requires_metal
class TestWarperParity(_ParityBase):
    VARIANT = "warper"

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    @pytest.mark.parametrize("name,mixer,warper,grading", WARPER_SDR_CASES,
                             ids=[c[0] for c in WARPER_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, sdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, sdr_pattern,
                                   False, mixer, warper, grading)
        _assert_sdr(gl_out, mt_out, f"warper/SDR/{name}")

    @pytest.mark.parametrize("name,mixer,warper,grading", WARPER_HDR_CASES,
                             ids=[c[0] for c in WARPER_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, hdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, hdr_pattern,
                                   True, mixer, warper, grading)
        _assert_hdr(gl_out, mt_out, f"warper/HDR/{name}")


@requires_metal
class TestBothParity(_ParityBase):
    VARIANT = "both"

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    @pytest.mark.parametrize("name,mixer,warper,grading", BOTH_SDR_CASES,
                             ids=[c[0] for c in BOTH_SDR_CASES])
    def test_sdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, sdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, sdr_pattern,
                                   False, mixer, warper, grading)
        _assert_sdr(gl_out, mt_out, f"both/SDR/{name}")

    @pytest.mark.parametrize("name,mixer,warper,grading", BOTH_HDR_CASES,
                             ids=[c[0] for c in BOTH_HDR_CASES])
    def test_hdr(self, gl, gl_progs, gl_warper_tex, metal_ctx, psos,
                 metal_warper_tex, hdr_pattern, name, mixer, warper, grading):
        gl_out, mt_out = self._run(gl, gl_progs, gl_warper_tex, metal_ctx,
                                   psos, metal_warper_tex, hdr_pattern,
                                   True, mixer, warper, grading)
        _assert_hdr(gl_out, mt_out, f"both/HDR/{name}")
