"""P0-COLOR-05 三方 golden vector：OCIO / Builtin / GPU shader 数值一致。

计划表验收标准：「同一组 RGB/灰阶/高光样本误差在定义容差内」——本文件
对 cs_transform GPU shader、BuiltinColorProvider、OCIOColorProvider 三条
引擎路径施加同一组样本（灰阶 + 三原色 + 混合色 + HDR 高光），要求输出
在容差内一致。GPU 路径用 ModernGL standalone context 真实执行
shaders.py 的 _CS_TRANSFORM_FRAGMENT（非 mock）。

样本覆盖：
  - 灰阶：0 / 0.018 / 0.081（OETF 膝点）/ 0.5 / 0.9 / 1.0
  - 三原色与混合色（gamut 矩阵的非对角项参与）
  - HDR 高光 0.99（PQ 高段，nits 域）

容差：
  - SDR 传输函数（sRGB/BT.709）：绝对 1e-3（GPU float32 + 纹理往返）
  - PQ（nits 域 0..10000）：相对 2e-3（绝对误差无意义）
  - HLG：相对 2e-3
"""

import numpy as np
import pytest

from src.shared.color.color_transform_provider import (
    BuiltinColorProvider,
    OCIOColorProvider,
    reset_provider_cache,
)
from src.shared.color.color_space_descriptor import (
    ColorSpaceDescriptor, LINEAR_SCENE,
)
from src.shared.color.types import ColorPrimaries, ColorTransfer, ColorRange

# ── 样本集（灰阶 + 三原色 + 混合色 + HDR 高光）─────────────────────────

_GRAYSCALE = [0.0, 0.018, 0.081, 0.18, 0.5, 0.9, 0.99, 1.0]
_COLORS = [
    [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
    [1.0, 1.0, 0.0], [0.9, 0.1, 0.2], [0.2, 0.9, 0.3], [0.1, 0.2, 0.9],
]


def _sample_pixels() -> np.ndarray:
    """(N, 4) float32 像素集：灰阶在前，彩色在后。"""
    rows = [[v, v, v, 1.0] for v in _GRAYSCALE] + [c + [1.0] for c in _COLORS]
    return np.array(rows, dtype=np.float32)


# ── GPU 路径：ModernGL standalone 真跑 _CS_TRANSFORM_FRAGMENT ──────────

moderngl = pytest.importorskip("moderngl", reason="ModernGL 不可用，跳过 GPU 侧")

# shader uniform 约定（shaders.py _CS_TRANSFORM_FRAGMENT）
_TRANSFER_CODE = {
    ColorTransfer.SRGB: 0,
    ColorTransfer.BT709: 1,
    ColorTransfer.LINEAR: 2,
    ColorTransfer.PQ: 3,
    ColorTransfer.HLG: 4,
}
_PRIMARIES_CODE = {
    ColorPrimaries.BT709: 0,
    ColorPrimaries.BT2020: 1,
    ColorPrimaries.P3_DCI: 2,   # shader 的 P3 分支是 P3-D65 矩阵
    ColorPrimaries.P3_DISPLAY: 2,
}


def _gpu_decode(pixels: np.ndarray, transfer: ColorTransfer,
                primaries: ColorPrimaries) -> np.ndarray:
    """在 standalone GL context 上执行 GPU cs_transform decode 并读回。

    布局：(1, N) RGBA32F 纹理（h=1 避免 v 翻转歧义），全屏 quad 采样。
    """
    from src.features.composite.core import shaders

    ctx = moderngl.create_standalone_context(require=330)
    try:
        n = pixels.shape[0]
        prog = ctx.program(
            vertex_shader=shaders._PASSTHROUGH_VERTEX,
            fragment_shader=shaders._CS_TRANSFORM_FRAGMENT,
        )
        prog["u_texture"].value = 0
        prog["u_direction"].value = 0  # decode
        prog["u_transfer"].value = _TRANSFER_CODE[transfer]
        prog["u_primaries"].value = _PRIMARIES_CODE[primaries]

        tex = ctx.texture((n, 1), components=4, dtype="f4",
                          data=np.ascontiguousarray(pixels.reshape(1, n, 4)))
        tex.use(0)
        fbo = ctx.framebuffer(color_attachments=[tex])
        fbo.use()

        quad = ctx.vertex_array(
            prog,
            ctx.buffer(np.array([
                -1.0, -1.0, 0.0, 0.0,
                1.0, -1.0, 1.0, 0.0,
                -1.0, 1.0, 0.0, 1.0,
                1.0, 1.0, 1.0, 1.0,
            ], dtype="f4")),
            *["in_vert", "in_uv"],
        )
        quad.render(moderngl.TRIANGLE_STRIP)

        out = np.frombuffer(fbo.read(components=4, dtype="f4"),
                            dtype=np.float32).reshape(1, n, 4)
        return out.reshape(n, 4)
    finally:
        ctx.release()


# ── 参数化：转换组合矩阵 ────────────────────────────────────────────────
#
# GPU shader 不处理 range（full/limited 由 CPU 侧 numpy 完成），因此 GPU
# 对比全部用 FULL-range 描述符——测试的是三方共享的 transfer+gamut 核心。

_CASES = [
    # (名称, 传输, 原色域, 容差类型)
    ("srgb_bt709", ColorTransfer.SRGB, ColorPrimaries.BT709, "abs"),
    ("bt709_bt709", ColorTransfer.BT709, ColorPrimaries.BT709, "abs"),
    ("pq_bt2020", ColorTransfer.PQ, ColorPrimaries.BT2020, "rel"),
    ("hlg_bt2020", ColorTransfer.HLG, ColorPrimaries.BT2020, "rel"),
    ("pq_p3", ColorTransfer.PQ, ColorPrimaries.P3_DCI, "rel"),
    ("srgb_p3", ColorTransfer.SRGB, ColorPrimaries.P3_DISPLAY, "abs"),
]

_TOL_ABS = 1e-3
_TOL_REL = 2e-3


def _max_err(a: np.ndarray, b: np.ndarray, mode: str) -> float:
    diff = np.abs(a[..., :3] - b[..., :3])
    if mode == "abs":
        return float(np.max(diff))
    denom = np.maximum(np.abs(b[..., :3]), 1e-6)
    return float(np.max(diff / denom))


@pytest.fixture(scope="module")
def providers():
    reset_provider_cache()
    ocio = OCIOColorProvider()
    if not ocio.is_available():
        pytest.skip("OCIO not available in this environment")
    return ocio, BuiltinColorProvider()


@pytest.mark.parametrize("name, transfer, primaries, mode", _CASES)
def test_triple_golden_decode(providers, name, transfer, primaries, mode):
    """三方引擎对同一样本集的 decode（encoded → scene-linear）一致。"""
    ocio, builtin = providers
    pixels = _sample_pixels()
    desc = ColorSpaceDescriptor(primaries=primaries, transfer=transfer,
                                range=ColorRange.FULL)

    oc = ocio.transform(pixels.copy(), desc, LINEAR_SCENE)
    bi = builtin.transform(pixels.copy(), desc, LINEAR_SCENE)
    gl = _gpu_decode(pixels, transfer, primaries)

    tol = _TOL_ABS if mode == "abs" else _TOL_REL
    e_ob = _max_err(oc, bi, mode)
    e_og = _max_err(oc, gl, mode)
    e_bg = _max_err(bi, gl, mode)
    assert e_ob < tol, f"{name}: OCIO vs builtin err {e_ob:.2e} >= {tol:.0e}"
    assert e_og < tol, f"{name}: OCIO vs GPU err {e_og:.2e} >= {tol:.0e}"
    assert e_bg < tol, f"{name}: builtin vs GPU err {e_bg:.2e} >= {tol:.0e}"


@pytest.mark.parametrize("name, transfer, primaries, mode", [
    ("srgb_bt709", ColorTransfer.SRGB, ColorPrimaries.BT709, "abs"),
    ("bt709_bt709", ColorTransfer.BT709, ColorPrimaries.BT709, "abs"),
])
def test_triple_golden_encode(providers, name, transfer, primaries, mode):
    """三方引擎的 encode（scene-linear → encoded）一致（SDR 往返契约）。"""
    ocio, builtin = providers
    # 线性域样本：SDR 灰阶 + 低亮彩色（encode 输入是 linear [0,1]）
    lin = np.array(
        [[v, v, v, 1.0] for v in _GRAYSCALE]
        + [[0.5 * c for c in rgb] + [1.0] for rgb in _COLORS],
        dtype=np.float32,
    )
    desc = ColorSpaceDescriptor(primaries=primaries, transfer=transfer,
                                range=ColorRange.FULL)

    oc = ocio.transform(lin.copy(), LINEAR_SCENE, desc)
    bi = builtin.transform(lin.copy(), LINEAR_SCENE, desc)

    # GPU encode 路径
    from src.features.composite.core import shaders
    ctx = moderngl.create_standalone_context(require=330)
    try:
        n = lin.shape[0]
        prog = ctx.program(
            vertex_shader=shaders._PASSTHROUGH_VERTEX,
            fragment_shader=shaders._CS_TRANSFORM_FRAGMENT,
        )
        prog["u_texture"].value = 0
        prog["u_direction"].value = 1  # encode
        prog["u_transfer"].value = _TRANSFER_CODE[transfer]
        prog["u_primaries"].value = _PRIMARIES_CODE[primaries]
        tex = ctx.texture((n, 1), components=4, dtype="f4",
                          data=np.ascontiguousarray(lin.reshape(1, n, 4)))
        tex.use(0)
        fbo = ctx.framebuffer(color_attachments=[tex])
        fbo.use()
        quad = ctx.vertex_array(
            prog,
            ctx.buffer(np.array([
                -1.0, -1.0, 0.0, 0.0,
                1.0, -1.0, 1.0, 0.0,
                -1.0, 1.0, 0.0, 1.0,
                1.0, 1.0, 1.0, 1.0,
            ], dtype="f4")),
            *["in_vert", "in_uv"],
        )
        quad.render(moderngl.TRIANGLE_STRIP)
        gl = np.frombuffer(fbo.read(components=4, dtype="f4"),
                           dtype=np.float32).reshape(n, 4)
    finally:
        ctx.release()

    tol = _TOL_ABS if mode == "abs" else _TOL_REL
    assert _max_err(oc, bi, mode) < tol, f"{name} encode: OCIO vs builtin 超差"
    assert _max_err(oc, gl, mode) < tol, f"{name} encode: OCIO vs GPU 超差"
    assert _max_err(bi, gl, mode) < tol, f"{name} encode: builtin vs GPU 超差"


def test_triple_golden_canonical_anchor(providers):
    """锚点值：code 0.5 的解码结果三方必须落在 BT.709/sRGB 契约值上。"""
    ocio, builtin = providers
    img = np.array([[0.5, 0.5, 0.5, 1.0]], dtype=np.float32)
    for transfer, expected in [
        (ColorTransfer.SRGB, 0.2140),   # ((0.5+0.055)/1.055)^2.4
        (ColorTransfer.BT709, 0.2596),  # ((0.5+0.099)/1.099)^(1/0.45)
    ]:
        desc = ColorSpaceDescriptor(primaries=ColorPrimaries.BT709,
                                    transfer=transfer, range=ColorRange.FULL)
        oc = float(ocio.transform(img.copy(), desc, LINEAR_SCENE)[0, 0])
        bi = float(builtin.transform(img.copy(), desc, LINEAR_SCENE)[0, 0])
        gl = float(_gpu_decode(img, transfer, ColorPrimaries.BT709)[0, 0])
        for label, v in [("ocio", oc), ("builtin", bi), ("gpu", gl)]:
            assert abs(v - expected) < 2e-3, (
                f"{transfer.value} code0.5 {label}={v} != {expected}"
            )
