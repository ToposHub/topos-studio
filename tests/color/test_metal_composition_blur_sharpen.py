"""P7-03 第四批 G2 门禁：blur/sharpen 的 MSL vs GL 对拍（抖动噪声门）。

翻译正确性与既有批次同等严格，但**门禁形态按效果类适配**（P6-06 先例：
numpy 参考 + 实测容差）：``blur_rand`` 是 sin-hash 抖动，公式逐字翻译，
但 sin 大参数的 ulp 差异被 hash 放大——实测两侧 hash 图 18.6% 像素完
全分歧（max 0.988），抖动旋转角在两种后端上**本质不同**（采样相位不
同）。抖动只影响采样相位不影响均值语义，因此：

1. **直通路径**（fx 关闭）仍按位级门禁——既有套件 neutral 用例
   ≤1 LSB 已覆盖（直通分支无采样相位问题）。
2. **抖动路径**采用双层门禁：
   a. GL↔MTL 分位数门（零中值 + 冻结尾界，按实测 ×1.5 头寸冻结）；
   b. **相位 0 numpy 参考实现交叉验证**——两侧各自与确定性 oracle
      （抖动旋转=0）比对，断言 Metal 的偏差分布不劣于 GL（同噪声族、
      零偏）。翻译结构性错误会表现为偏移/系统性劣化，在此被拦截。

实测（真机 AMD 6800，SDR 8-bit LSB）：全部用例 p50=0（半数像素位级
一致）、符号偏差 ≈0.000；尾差集中于色块边缘（tap 相位跨越边缘所致，
如 blur_fast 40px 半径、硬边缘高度 1.0 → 单 tap 翻转 ≈21 LSB）。

管线顺序：blur_sharpen 恒为 composition main 第一位（fx 关闭时直通）。
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

EFFECTS = ("blur_sharpen", "grading")


# ---------------------------------------------------------------------------
# 结构层（无 Metal 也运行）
# ---------------------------------------------------------------------------


class TestEmitterStructure:
    def test_blur_sharpen_is_migrated(self):
        assert "blur_sharpen" in MIGRATED_EFFECTS

    def test_untranslated_effect_still_rejected(self):
        manifest = build_composition_uniform_manifest()
        with pytest.raises(ValueError, match="qualifier"):
            emit_composition_msl(manifest, ["blur_sharpen", "qualifier"])

    def test_call_is_pipeline_first_and_has_bool_conversions(self):
        manifest = build_composition_uniform_manifest()
        src = emit_composition_msl(manifest, EFFECTS)
        body = src[src.index("fs_composition"):]
        assert body.index("msl_apply_blur_sharpen") < body.index(
            "msl_apply_grading")
        assert "params.u_fx_enabled != 0u" in src
        assert "params.u_blur_enabled != 0u" in src
        assert "float2(params.u_tex_size_x, params.u_tex_size_y)" in src

    def test_radians_expanded(self):
        """MSL 无 radians 内建——翻译为显式 π/180 乘法。"""
        from src.features.color.core.shader_nodes_msl import MSL_BLUR_SHARPEN
        assert "= radians(" not in MSL_BLUR_SHARPEN
        assert "0.017453292519943295" in MSL_BLUR_SHARPEN


# ---------------------------------------------------------------------------
# 相位 0 numpy 参考实现（与 GLSL 逐条对齐，抖动旋转=0 的确定性 oracle）
# ---------------------------------------------------------------------------


def _bilinear(img: np.ndarray, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    """clamp-to-edge 双线性采样；img (H, W, 3)，px/py texel 坐标。"""
    h, w = img.shape[:2]
    px = np.clip(px, 0.0, w - 1.0)
    py = np.clip(py, 0.0, h - 1.0)
    x0 = np.floor(px).astype(np.int64)
    y0 = np.floor(py).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (px - x0)[..., None]
    fy = (py - y0)[..., None]
    a = img[y0, x0] * (1.0 - fx) + img[y0, x1] * fx
    b = img[y1, x0] * (1.0 - fx) + img[y1, x1] * fx
    return a * (1.0 - fy) + b * fy


class _RefGrid:
    """下采样像素网格的 uv/texel 坐标（stride 4 控制成本）。"""

    def __init__(self, stride: int = 4):
        self.rows = np.arange(0, H, stride)
        self.cols = np.arange(0, W, stride)
        cc, rr = np.meshgrid(self.cols, self.rows)
        self.px = cc.astype(np.float64).ravel()
        self.py = rr.astype(np.float64).ravel()
        self.u = (self.px + 0.5) / W
        self.v = (self.py + 0.5) / H

    def sample(self, img: np.ndarray, off_px: np.ndarray) -> np.ndarray:
        return _bilinear(img, self.px + off_px[..., 0],
                         self.py + off_px[..., 1])


def _ref_fast_blur(img: np.ndarray, grid: _RefGrid, intensity: float,
                   radius: float) -> np.ndarray:
    scale = W / 2048.0
    r = (intensity * radius * scale) / 50.0
    if r <= 0.0:
        return grid.sample(img, np.zeros((len(grid.px), 2)))
    idx = np.arange(12)
    theta = idx * 2.39996  # noise = 0（相位 0 oracle）
    dist = np.sqrt(idx / 12.0) * r
    acc = np.zeros((len(grid.px), 3))
    for k in range(12):
        off = np.stack([np.cos(theta[k]), np.sin(theta[k])]) * dist[k]
        acc += grid.sample(img, np.broadcast_to(off, (len(grid.px), 2)))
    return acc / 12.0


def _ref_bilateral(img: np.ndarray, grid: _RefGrid, intensity: float,
                   detail_preserve: float) -> np.ndarray:
    scale = W / 2048.0
    sigma_s = max(intensity * scale / 10.0, 0.1)
    sigma_r = max(1.0 - detail_preserve / 100.0, 0.01) * 0.5
    center = grid.sample(img, np.zeros((len(grid.px), 2)))
    acc = np.zeros_like(center)
    total = 0.0
    for i in range(-2, 3):
        for j in range(-2, 3):
            # 相位 0：rot = mat2(1,0,0,1)（rnd=0 → cos=1, sin=0）
            off = np.array([float(i), float(j)]) * (sigma_s / 2.0)
            sample_c = grid.sample(img, np.broadcast_to(off, (len(grid.px), 2)))
            spatial_w = np.exp(-(i * i + j * j) / 4.0)
            range_d = np.linalg.norm(center - sample_c, axis=-1)
            range_w = np.exp(-range_d * range_d / (2.0 * sigma_r * sigma_r))
            acc += sample_c * range_w[..., None] * spatial_w
            total += range_w * spatial_w
    out = acc / np.maximum(total, 1e-30)[..., None]
    return np.where((total > 0.0)[..., None], out, center)


def _ref_bokeh(img: np.ndarray, grid: _RefGrid, radius: float,
               bokeh: float) -> np.ndarray:
    scale = W / 2048.0
    r = radius * scale / 2.0
    boost = 1.0 + (bokeh / 100.0) * 2.0
    acc = np.zeros((len(grid.px), 3))
    for k in range(28):
        theta = k * 2.39996  # noise = 0
        dist = np.sqrt(k / 28.0) * r
        off = np.stack([np.cos(theta), np.sin(theta)]) * dist
        c = grid.sample(img, np.broadcast_to(off, (len(grid.px), 2)))
        lum = c @ np.array([0.299, 0.587, 0.114])
        acc += c * (1.0 + np.maximum(lum, 0.001) ** 2 * boost)[..., None]
    return acc / 28.0


def _ref_motion(img: np.ndarray, grid: _RefGrid, intensity: float,
                angle_deg: float) -> np.ndarray:
    scale = W / 2048.0
    ang = angle_deg * 0.017453292519943295
    direction = np.array([np.cos(ang), np.sin(ang)])
    length = intensity * scale * 1.5
    jitter = -0.5  # 相位 0：blur_rand=0 → jitter=-0.5
    acc = np.zeros((len(grid.px), 3))
    for k in range(32):
        t = (k + jitter) / 31.0
        off = direction * ((t - 0.5) * length)
        acc += grid.sample(img, np.broadcast_to(off, (len(grid.px), 2)))
    return acc / 32.0


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


# (p99, p99.9, max)，8-bit LSB——实测（真机 AMD 6800）×1.5 头寸后冻结。
# 相位噪声门：零中值抖动的采样相位差，非位级语义差（直通路径仍位级，
# 见既有套件 neutral 用例；结构性错误由相位 0 参考实现交叉验证拦截）。
_SDR_NOISE_BOUNDS = {
    "usm_mode1_iters": (3.0, 14.0, 30.0),
    "usm_mode0_texture": (2.0, 3.0, 6.0),
    "halo_suppress": (2.0, 3.0, 14.0),
    "blur_bilateral": (2.0, 3.0, 5.0),
    "blur_bokeh": (6.0, 15.0, 27.0),
    "blur_motion": (2.0, 3.0, 5.0),
    "blur_fast": (23.0, 44.0, 75.0),
    "sharpen_plus_blur": (2.0, 3.0, 15.0),
}
_HDR_NOISE_BOUNDS = {  # 相对容差（带 1.0 下限）
    "hdr_sharpen": (1.1e-3, 3.6e-2, 0.15),
}


def _assert_noise_gate(a: np.ndarray, b: np.ndarray, label: str,
                       bounds: tuple) -> None:
    """抖动噪声门：零中值（|bias|≤0.25 LSB）+ 冻结分位数尾界。"""
    qa = np.rint(np.clip(a[..., :3], 0.0, 1.0) * 255.0)
    qb = np.rint(np.clip(b[..., :3], 0.0, 1.0) * 255.0)
    signed = (qa - qb).ravel()
    assert abs(signed.mean()) <= 0.25, (
        f"{label}: 符号偏差 {signed.mean():+.3f} LSB ≠ 0（疑似系统性错误，"
        "非抖动噪声）"
    )
    ad = np.abs(signed)
    p99, p999, mx = (np.percentile(ad, 99), np.percentile(ad, 99.9),
                     ad.max())
    assert p99 <= bounds[0] and p999 <= bounds[1] and mx <= bounds[2], (
        f"{label}: p99={p99:.1f}/p99.9={p999:.1f}/max={mx:.0f} LSB 超噪声门 "
        f"{bounds}（结构性偏差疑似，需人工复核）"
    )


def _assert_hdr_noise_gate(a: np.ndarray, b: np.ndarray, label: str,
                           bounds: tuple) -> None:
    denom = np.maximum(1.0, np.maximum(np.abs(a[..., :3]), np.abs(b[..., :3])))
    signed = (a[..., :3] - b[..., :3]) / denom
    assert abs(signed.mean()) <= 2e-5, (
        f"{label}: 符号相对偏差 {signed.mean():.2e} ≠ 0（疑似系统性错误）"
    )
    err = np.abs(signed)
    p99, p999, mx = (np.percentile(err, 99), np.percentile(err, 99.9),
                     err.max())
    assert p99 <= bounds[0] and p999 <= bounds[1] and mx <= bounds[2], (
        f"{label}: p99={p99:.2e}/p99.9={p999:.2e}/max={mx:.2e} rel 超噪声门 "
        f"{bounds}"
    )


# (名称, effects_params 内的 fx 组, blur 组)
FX_SDR_CASES = [
    ("usm_mode1_iters", {"enabled": True, "sharpen": 40.0, "mode": 1,
                         "radius": 2.0, "iterations": 2}, {}),
    ("usm_mode0_texture", {"enabled": True, "texture": 30.0}, {}),
    ("halo_suppress", {"enabled": True, "sharpen": 40.0, "halo": 50.0,
                       "mode": 0}, {}),
    ("blur_bilateral", {}, {"blur_enabled": True, "blur_mode": 0,
                            "blur_intensity": 30.0, "blur_detail": 50.0}),
    ("blur_bokeh", {}, {"blur_enabled": True, "blur_mode": 1,
                        "blur_radius": 25.0, "blur_bokeh": 40.0}),
    ("blur_motion", {}, {"blur_enabled": True, "blur_mode": 2,
                         "blur_intensity": 35.0, "blur_angle": 30.0}),
    ("blur_fast", {}, {"blur_enabled": True, "blur_mode": 3,
                       "blur_intensity": 40.0, "blur_radius": 50.0}),
    ("sharpen_plus_blur", {"enabled": True, "sharpen": 35.0, "mode": 1,
                           "radius": 1.5, "iterations": 1},
     {"blur_enabled": True, "blur_mode": 0, "blur_intensity": 25.0,
      "blur_detail": 60.0}),
]
FX_HDR_CASES = [
    ("hdr_sharpen", {"enabled": True, "sharpen": 30.0, "mode": 1,
                     "radius": 2.0, "iterations": 1}, {}),
]
# 参考实现交叉验证覆盖的纯模糊用例（ref_fn(img16, grid)）。
# nonlinear=True：核函数含逐 tap 非线性（bokeh 的 lum² highlight boost）
# ——相位均值 ≠ 相位 0 特解，oracle 有固有偏，改判「两后端相对 oracle
# 等偏」（结构性分歧仍被拦截，非线性零偏不可得）。
_REF_CASES = {
    "blur_bilateral": (False, lambda img16, grid: _ref_bilateral(
        img16, grid, 30.0, 50.0)),
    "blur_bokeh": (True, lambda img16, grid: _ref_bokeh(
        img16, grid, 25.0, 40.0)),
    "blur_motion": (False, lambda img16, grid: _ref_motion(
        img16, grid, 35.0, 30.0)),
    "blur_fast": (False, lambda img16, grid: _ref_fast_blur(
        img16, grid, 40.0, 50.0)),
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
def gl_prog(pipeline):
    # blur_sharpen 无编译旗标（fx 关闭时直通，调用位恒在）
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
class TestBlurSharpenParity:
    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()

    @pytest.fixture(scope="class")
    @classmethod
    def hdr_pattern(cls):
        return _pattern_hdr()

    def _run(self, gl, gl_prog, msl_setup, metal_ctx, pattern, hdr,
             fx_params, blur_params):
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {},
                 "effects_params": {**fx_params, **blur_params}}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_prog, pattern, values, hdr)
        pso, dtype = msl_setup
        mt_out = _metal_render(metal_ctx, pso, pattern, values, dtype)
        return gl_out, mt_out

    @pytest.mark.parametrize("name,fx,blur", FX_SDR_CASES,
                             ids=[c[0] for c in FX_SDR_CASES])
    def test_sdr_noise_gate(self, gl, gl_prog, msl_setup, metal_ctx,
                            sdr_pattern, name, fx, blur):
        gl_out, mt_out = self._run(gl, gl_prog, msl_setup, metal_ctx,
                                   sdr_pattern, False, fx, blur)
        _assert_noise_gate(gl_out, mt_out, f"fx/SDR/{name}",
                           _SDR_NOISE_BOUNDS[name])

    @pytest.mark.parametrize("name,fx,blur", FX_HDR_CASES,
                             ids=[c[0] for c in FX_HDR_CASES])
    def test_hdr_noise_gate(self, gl, gl_prog, msl_setup, metal_ctx,
                            hdr_pattern, name, fx, blur):
        gl_out, mt_out = self._run(gl, gl_prog, msl_setup, metal_ctx,
                                   hdr_pattern, True, fx, blur)
        _assert_hdr_noise_gate(gl_out, mt_out, f"fx/HDR/{name}",
                               _HDR_NOISE_BOUNDS[name])


@requires_metal
class TestDitherPhaseReference:
    """相位 0 numpy 参考交叉验证：两侧偏差同噪声族、零偏——

    翻译结构性错误（公式/坐标/权重错）会使 Metal-vs-ref 显著劣于
    GL-vs-ref 或产生符号偏差；纯相位噪声则二者统计同族。
    """

    @pytest.mark.parametrize("name", sorted(_REF_CASES))
    def test_backend_bias_vs_phase0_reference(self, gl, gl_prog, msl_setup,
                                              metal_ctx, sdr_pattern, name):
        nonlinear, ref_fn = _REF_CASES[name]
        img16 = sdr_pattern.astype(np.float16)[..., :3].astype(np.float64)
        grid = _RefGrid(stride=4)
        ref = ref_fn(img16, grid)

        fx, blur = next(c for c in FX_SDR_CASES if c[0] == name)[1:]
        layer = {"opacity": 1.0, "blend_mode": "Normal",
                 "grading_params": {},
                 "effects_params": {**fx, **blur}}
        values = _values_for(layer)
        gl_out = _gl_render(gl, gl_prog, sdr_pattern, values, False)
        pso, dtype = msl_setup
        mt_out = _metal_render(metal_ctx, pso, sdr_pattern, values, dtype)

        rows, cols = grid.rows, grid.cols
        gl_s = np.rint(np.clip(gl_out[rows][:, cols, :3], 0, 1) * 255.0)
        mt_s = np.rint(np.clip(mt_out[rows][:, cols, :3], 0, 1) * 255.0)
        ref_s = np.clip(ref.reshape(len(rows), len(cols), 3), 0, 1) * 255.0

        e_gl = (gl_s - ref_s).ravel()
        e_mt = (mt_s - ref_s).ravel()
        if nonlinear:
            # 逐 tap 非线性核：oracle 固有偏（相位均值 ≠ 特解），改判
            # 「两后端相对 oracle 等偏」——结构性分歧仍被拦截。
            assert abs(e_gl.mean() - e_mt.mean()) <= 0.6, (
                f"两后端相对 oracle 偏差不等：GL {e_gl.mean():+.3f} vs "
                f"MT {e_mt.mean():+.3f}")
        else:
            # 零偏：相位噪声的符号均值 ≈ 0（阈值 0.6 LSB，N≈32k 采样下
            # 零均值噪声的均值标准误 <0.05 LSB）
            assert abs(e_gl.mean()) <= 0.6, (
                f"GL-vs-ref 偏差 {e_gl.mean():+.3f}")
            assert abs(e_mt.mean()) <= 0.6, (
                f"MT-vs-ref 偏差 {e_mt.mean():+.3f}")
        # 同噪声族：Metal 的离散度不劣于 GL 的 2 倍（实测二者同量级）
        assert e_mt.std() <= 2.0 * e_gl.std() + 0.5, (
            f"MT-vs-ref std {e_mt.std():.2f} vs GL-vs-ref std "
            f"{e_gl.std():.2f}——疑似结构性劣化"
        )

    @pytest.fixture(scope="class")
    @classmethod
    def sdr_pattern(cls):
        return _pattern_sdr()
