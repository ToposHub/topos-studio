"""The Metal blend kernels compute the shared blend formulas (plan task 2.6).

Frame-level parity with GL (``test_metal_edit_compositor.py``) leaves two
possible sources of difference: the formulas, and the inputs — a converted
NV12 pixel may round one level differently on Metal than on GL.  Here both
sides read the *same* 8-bit textures, so any difference is the formula.  The
reference is ``blend_modes.blend_rgb``, the NumPy twin of
``shaders/blend.glsl`` that the CPU fallback and CPU export use.
"""

from __future__ import annotations

import sys

import numpy as np
import pytest

from src.features.edit.core.blend_modes import blend_rgb
from src.features.edit.core.clip_params import ClipBlendMode


def _metal() -> object:
    pytest.importorskip("Metal")
    from src.shared.gpu.metal.context import MetalComputeContext

    if not MetalComputeContext.is_supported():
        pytest.skip("Metal is unavailable")
    return MetalComputeContext()


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Metal")
@pytest.mark.parametrize("mode", [member.code for member in ClipBlendMode],
                         ids=[member.value for member in ClipBlendMode])
def test_blend_kernel_matches_the_numpy_formula_on_identical_inputs(mode: int) -> None:
    import Metal

    from src.features.edit.core.metal_composite_msl import EDIT_COMPOSITE_MSL

    context = _metal()
    kernel = context.compile_kernel("edit_composite_blend", EDIT_COMPOSITE_MSL)
    width, height = 128, 96
    rng = np.random.default_rng(11)
    base_u8 = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    overlay_u8 = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    overlay_u8[..., 3] = rng.integers(0, 256, (height, width), dtype=np.uint8)
    # Pixels with two or three equal channels exercise the HSL ranking ties.
    overlay_u8[::7, ::5, 1] = overlay_u8[::7, ::5, 0]
    base_u8[::4, ::3, 2] = base_u8[::4, ::3, 0]
    base_u8[::11, ::9, 1] = base_u8[::11, ::9, 0]
    grey = rng.integers(0, 256, (8, width, 1), dtype=np.uint8)
    overlay_u8[-8:, :, :3] = grey
    base = context.create_texture_from_numpy(
        base_u8, Metal.MTLPixelFormatRGBA8Unorm, width, height)
    overlay = context.create_texture_from_numpy(
        overlay_u8, Metal.MTLPixelFormatRGBA8Unorm, width, height)
    out = context.create_texture_2d(
        Metal.MTLPixelFormatRGBA8Unorm, width, height, shader_write=True)
    opacity = 0.6
    context.dispatch(
        kernel, buffers={0: context.create_buffer_from_numpy(np.array([mode], np.int32)),
                         1: context.create_buffer_from_numpy(np.array([opacity], np.float32))},
        textures={0: base, 1: overlay, 2: out}, threads=(width, height, 1))
    context.submit(wait=True)
    got = context.read_texture(
        out, width * 4, height, np.uint8, region_width=width,
    ).reshape(height, width, 4).astype(np.int16)

    base_f = base_u8[..., :3].astype(np.float32) / 255.0
    overlay_f = overlay_u8[..., :3].astype(np.float32) / 255.0
    mixed = blend_rgb(base_f, overlay_f, mode)
    weight = opacity * overlay_u8[..., 3:4].astype(np.float32) / 255.0
    expected = np.rint((base_f * (1.0 - weight) + mixed * weight) * 255.0)

    error = np.abs(got[..., :3] - expected)
    # Same inputs, same formula: float32 evaluation order and the store's
    # rounding leave at most one level.
    assert error.max() <= 1, (mode, int(error.max()), float(error.mean()))
    assert error.mean() < 0.1, (mode, float(error.mean()))
    assert (got[..., 3] == 255).all()            # the composite is opaque


@pytest.mark.skipif(sys.platform != "darwin", reason="requires Metal")
def test_the_bottom_layer_is_drawn_over_black_at_its_own_opacity() -> None:
    import Metal

    from src.features.edit.core.metal_composite_msl import EDIT_COMPOSITE_MSL

    context = _metal()
    kernel = context.compile_kernel("edit_composite_first", EDIT_COMPOSITE_MSL)
    width, height = 64, 8
    rng = np.random.default_rng(3)
    layer_u8 = rng.integers(0, 256, (height, width, 4), dtype=np.uint8)
    layer_u8[..., 3] = np.linspace(0, 255, width, dtype=np.uint8)
    layer = context.create_texture_from_numpy(
        layer_u8, Metal.MTLPixelFormatRGBA8Unorm, width, height)
    out = context.create_texture_2d(
        Metal.MTLPixelFormatRGBA8Unorm, width, height, shader_write=True)
    context.dispatch(
        kernel, buffers={0: context.create_buffer_from_numpy(np.array([0.8], np.float32))},
        textures={0: layer, 1: out}, threads=(width, height, 1))
    context.submit(wait=True)
    got = context.read_texture(
        out, width * 4, height, np.uint8, region_width=width).reshape(height, width, 4)
    weight = 0.8 * layer_u8[..., 3:4].astype(np.float32) / 255.0
    expected = np.rint(layer_u8[..., :3].astype(np.float32) * weight)
    assert np.abs(got[..., :3].astype(np.int16) - expected).max() <= 1
    assert (got[..., 3] == 255).all()
