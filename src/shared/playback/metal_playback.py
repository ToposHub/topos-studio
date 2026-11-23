"""Metal playback opt-in, capability verdicts and kernel parameters (plan 7.1).

This module is pure logic: no Qt widget, no Metal object and no GL context is
created here, so every decision can be unit-tested on any machine.

Three questions are answered:

- *May this process show playback frames on Metal?*  Only when the user asked
  for it (``TOPOS_PLAYBACK_METAL=1``), on the Cocoa Qt platform of macOS, and
  while ``TOPOS_METAL_DISABLE`` does not switch the native viewport off.
  Without the opt-in nothing in the playback module changes.
- *May the Metal path show this frame?*  :func:`frame_refusal` accepts exactly
  the ``YuvFrame`` shapes the shared players produce today (8/10/12/16-bit,
  4:2:0 / 4:2:2 / 4:4:4 planar, GBR planes) and refuses everything else; a
  refused frame is shown by the existing GL worker or the GUI direct path.
- *Which numbers does the kernel need?*  :func:`build_yuv_params` reproduces
  the arithmetic of ``YUVGPUUploader``'s GLSL (same matrix coefficients, same
  range constants computed in float32, same chroma phase offset) and
  :func:`letterbox_rect` reproduces the viewport rectangle of
  ``_RenderThread._render_frame`` / ``GLVideoCanvas._paint_yuv``.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Mapping, Optional

import numpy as np

from .yuv_frame import YuvFrame

logger = logging.getLogger(__name__)

#: Set to ``1`` to show shared-player frames through Metal instead of OpenGL.
ENV_PLAYBACK_METAL = "TOPOS_PLAYBACK_METAL"

_TRUTHY = frozenset({"1", "true", "on", "yes"})

# Bit depths and subsamplings of the planar formats the players hand over
# (libav: yuv420p / p10 / p12 / p16 families and gbrp*; Topos: 422/444 10- and
# 12-bit).  Anything else is refused and takes the GL path.
SUPPORTED_BIT_DEPTHS = frozenset({8, 10, 12, 16})
SUPPORTED_SUBSAMPLING = frozenset({"420", "422", "444"})
# H.273 chroma_sample_loc_type values ``topos_chroma_sampling_params`` accepts.
SUPPORTED_CHROMA_SITING = frozenset({0, 1, 2})
# Matrix coefficients with a defined outcome in the GL shader: 9 is BT.2020,
# 5/6 are BT.601, the rest of this set is BT.709.  Others are refused rather
# than silently shown with a matrix the GL path never defines for them.
SUPPORTED_MATRICES = frozenset({0, 1, 2, 5, 6, 9, 10})

# Every Metal device on macOS accepts 16384; a smaller device limit is read at
# run time and passed into the verdicts.
DEFAULT_MAX_TEXTURE = 16384

#: Kernel for 8-bit planes (``R8Unorm``, linear filtered like the GL ``R8`` pool).
KERNEL_U8 = "playback_yuv_u8"
#: Kernel for 10/12/16-bit planes (``R16Uint``, nearest like the GL ``R16UI`` pool).
KERNEL_U16 = "playback_yuv_u16"

# Same coefficient tuples as the GLSL ``mat3`` constants of
# ``YUVGPUUploader``: (cr_to_r, cb_to_g, cr_to_g, cb_to_b) in the column order
# of the matrix multiplied with vec3(y, u, v).
_MATRIX_COEFFS: dict[int, tuple[float, float, float, float]] = {
    0: (1.5748, -0.1873, -0.4681, 1.8556),       # BT.709
    1: (1.4746, -0.1646, -0.5714, 1.8814),       # BT.2020
    2: (1.4020, -0.3441, -0.7141, 1.7720),       # BT.601
}

# ``PlaybackYuvParams`` of the MSL source in ``metal_yuv_renderer``.  The
# 16-byte vectors come first so that the packed numpy layout and the MSL
# layout (whose largest member forces a 16-byte alignment) coincide: 112 bytes.
YUV_PARAMS_DTYPE = np.dtype([
    ("rect", "<f4", (4,)),                 # left, top, width, height (pixels)
    ("coeffs", "<f4", (4,)),               # cr_to_r, cb_to_g, cr_to_g, cb_to_b
    ("luma_texel_scale", "<f4", (2,)),     # luma texels per output pixel
    ("chroma_texel_scale", "<f4", (2,)),   # chroma texels per output pixel
    ("chroma_offset_texels", "<f4", (2,)), # chroma phase in chroma texels
    ("chroma_scale", "<f4", (2,)),         # normalised, as the GL uniform
    ("chroma_offset", "<f4", (2,)),        # normalised, as the GL uniform
    ("pad", "<f4", (2,)),
    ("max_val", "<f4"),
    ("y_black", "<f4"),
    ("y_range_inv", "<f4"),
    ("c_neutral", "<f4"),
    ("c_range_inv", "<f4"),
    ("color_range", "<i4"),
    ("gbr_mode", "<i4"),
    ("pad_i", "<i4"),
])
assert YUV_PARAMS_DTYPE.itemsize == 112


def env_flag(value: Optional[str]) -> bool:
    """Whether an environment variable value means "on"."""
    return str(value or "").strip().lower() in _TRUTHY


def playback_metal_requested(environ: Optional[Mapping[str, str]] = None) -> bool:
    """Whether the user asked for Metal playback (``TOPOS_PLAYBACK_METAL=1``)."""
    source = os.environ if environ is None else environ
    return env_flag(source.get(ENV_PLAYBACK_METAL))


def session_refusal(
    *,
    requested: bool,
    platform: str,
    qt_platform: str,
    native_viewport_disabled: bool,
) -> str:
    """Why Metal playback must not start for a canvas; ``""`` when it may.

    ``platform`` is ``sys.platform`` and ``qt_platform`` the Qt platform name
    (``cocoa`` is the only one whose ``winId()`` is an AppKit view).
    """
    if not requested:
        return "not_requested"
    if platform != "darwin":
        return f"platform_unsupported:{platform or 'unknown'}"
    if qt_platform != "cocoa":
        return f"qt_platform_unsupported:{qt_platform or 'none'}"
    if native_viewport_disabled:
        return "disabled_by_switch:native_viewport"
    return ""


def frame_refusal(frame: YuvFrame, *, max_texture: int = DEFAULT_MAX_TEXTURE) -> str:
    """Why the Metal path cannot show ``frame``; ``""`` when it can.

    Checked on the GUI thread before a frame is submitted, from the frame's
    metadata and array headers only (no pixel data is read).  A refused frame
    is routed to the existing GL worker, so a format Metal does not handle is
    shown exactly as it is today.
    """
    bit_depth = int(frame.bit_depth)
    if bit_depth not in SUPPORTED_BIT_DEPTHS:
        return f"bit_depth_unsupported:{bit_depth}"
    subsampling = str(frame.subsampling)
    if subsampling not in SUPPORTED_SUBSAMPLING:
        return f"subsampling_unsupported:{subsampling or 'none'}"
    if "gbr" in str(frame.pixel_format or "") and subsampling != "444":
        return f"gbr_requires_444:{subsampling}"
    if int(frame.color_range) not in (0, 1):
        return f"color_range_unsupported:{frame.color_range}"
    if int(frame.matrix) not in SUPPORTED_MATRICES:
        return f"matrix_unsupported:{frame.matrix}"
    if (subsampling == "422"
            and int(frame.chroma_siting) not in SUPPORTED_CHROMA_SITING):
        return f"chroma_siting_unsupported:{frame.chroma_siting}"
    expected_dtype = np.dtype(np.uint8 if bit_depth == 8 else np.uint16)
    limit = int(max_texture)
    for name, plane in (("y", frame.y), ("u", frame.u), ("v", frame.v)):
        shape = getattr(plane, "shape", None)
        if shape is None or len(shape) != 2:
            return f"plane_shape_unsupported:{name}"
        if int(shape[0]) <= 0 or int(shape[1]) <= 0:
            return f"plane_empty:{name}"
        if int(shape[0]) > limit or int(shape[1]) > limit:
            return f"plane_too_large:{name}"
        dtype = getattr(plane, "dtype", None)
        if dtype is None or np.dtype(dtype) != expected_dtype:
            return f"plane_dtype_unsupported:{name}:{np.dtype(dtype) if dtype is not None else 'none'}"
    if not frame.is_valid():
        return "plane_geometry_mismatch"
    return ""


def device_max_texture(device: Any) -> int:
    """Largest 2D texture edge ``device`` accepts.

    pyobjc-framework-Metal does not expose ``maxTextureDimension2D`` (12.2);
    every Metal device on macOS accepts 16384, which is the documented
    minimum of the macOS GPU families, so that value is used unless the
    binding does provide the property.
    """
    reader = getattr(device, "maxTextureDimension2D", None)
    if callable(reader):
        try:
            return int(reader())
        except (TypeError, ValueError):
            logger.debug("Metal device reported no usable texture limit", exc_info=True)
    return DEFAULT_MAX_TEXTURE


def display_size_refusal(width: int, height: int, *, max_texture: int = DEFAULT_MAX_TEXTURE) -> str:
    """Why a drawable of this pixel size cannot hold a Metal picture; ``""`` if it can."""
    if int(width) <= 0 or int(height) <= 0:
        return f"display_size_unusable:{width}x{height}"
    limit = int(max_texture)
    if int(width) > limit or int(height) > limit:
        return f"display_size_unsupported:{width}x{height}:limit={limit}"
    return ""


def letterbox_rect(frame_w: int, frame_h: int, out_w: int, out_h: int) -> tuple[int, int, int, int]:
    """``(left, top, width, height)`` of the picture inside a top-left-origin output.

    Same rectangle as the GL path: the picture keeps its aspect ratio, is
    centred, and an odd left-over row stays at the top (the GL viewport is
    measured from the bottom, where ``(out_h - vh) // 2`` rounds down).
    """
    scale = min(int(out_w) / int(frame_w), int(out_h) / int(frame_h))
    view_w = max(1, int(int(frame_w) * scale))
    view_h = max(1, int(int(frame_h) * scale))
    left = (int(out_w) - view_w) // 2
    bottom = (int(out_h) - view_h) // 2
    return left, int(out_h) - bottom - view_h, view_w, view_h


def uploader_matrix(matrix: int) -> int:
    """H.273 / FFmpeg matrix coefficient → ``YUVGPUUploader`` matrix constant.

    The same decision as ``gl_video_canvas._h273_matrix_to_uploader``:
    9 is BT.2020, 5 and 6 are BT.601, everything else is BT.709.
    """
    if int(matrix) == 9:
        return 1
    if int(matrix) in (5, 6):
        return 2
    return 0


def chroma_sampling(frame: YuvFrame) -> tuple[tuple[float, float], tuple[float, float]]:
    """``(chroma_uv_scale, chroma_uv_offset)`` exactly as the GL worker passes them.

    Only 4:2:2 carries a horizontal chroma phase; it is read from the one
    function the GL path uses, so the two paths cannot drift apart.
    """
    if str(frame.subsampling) != "422":
        return (1.0, 1.0), (0.0, 0.0)
    from src.shared.video.yuv_gpu_upload import topos_chroma_sampling_params

    return topos_chroma_sampling_params(
        "422", int(frame.source_width or frame.width),
        int(frame.source_height or frame.height), int(frame.chroma_siting))


def range_constants(bit_depth: int) -> tuple[float, float, float, float, float]:
    """``(max_val, y_black, y_range_inv, c_neutral, c_range_inv)`` in float32.

    Evaluated with float32 operations, as the GLSL shader does on the GPU, so
    the constants handed to Metal are the same values the GL shader computes.
    """
    depth = int(bit_depth)
    if depth not in SUPPORTED_BIT_DEPTHS:
        raise ValueError(f"unsupported bit depth for YUV conversion: {bit_depth}")
    f32 = np.float32
    shift = f32(depth - 8)
    nominal_max = f32(2.0) ** shift * f32(256.0) - f32(1.0)   # pow(2.0, depth) - 1.0
    y_black = f32(16.0) * f32(2.0) ** shift / nominal_max
    y_range_inv = nominal_max / (f32(219.0) * f32(2.0) ** shift)
    c_neutral = f32(128.0) * f32(2.0) ** shift / nominal_max
    c_range_inv = nominal_max / (f32(224.0) * f32(2.0) ** shift)
    return (float(nominal_max), float(y_black), float(y_range_inv),
            float(c_neutral), float(c_range_inv))


def _f32(value: float) -> float:
    """``value`` rounded to float32, as the GPU will see it."""
    return float(np.float32(value))


def build_yuv_params(
    frame: YuvFrame,
    rect: tuple[int, int, int, int],
    chroma_size: Optional[tuple[int, int]] = None,
) -> np.ndarray:
    """Pack the kernel parameters for ``frame`` drawn into ``rect``.

    ``rect`` is :func:`letterbox_rect` of the output texture and
    ``chroma_size`` the ``(width, height)`` of the U/V planes (taken from the
    frame when omitted).  The result is a one-element array of
    :data:`YUV_PARAMS_DTYPE`, ready for ``MetalComputeContext.create_buffer_from_numpy``.

    The sampling position is handed over as *texels per output pixel* plus a
    phase in texels, evaluated in float64 and rounded once.  For the usual
    ratios (1, 1/2, 2 …) that product is exact, so a sample point that falls
    exactly on a texel boundary stays on it — the outcome the GL shader has
    when its coordinate is an integer.  Deriving the position from a
    normalised coordinate instead loses those ties to float32 rounding.
    """
    bit_depth = int(frame.bit_depth)
    refusal = frame_refusal(frame)
    if refusal:
        # The caller checks frame_refusal first; a frame that slipped through
        # must not reach the kernel with parameters that were never defined.
        raise ValueError(f"frame refused by the Metal path: {refusal}")
    max_val, y_black, y_range_inv, c_neutral, c_range_inv = range_constants(bit_depth)
    scale, offset = chroma_sampling(frame)
    c_w, c_h = chroma_size if chroma_size is not None else (int(frame.u.shape[1]), int(frame.u.shape[0]))
    left, _top, view_w, view_h = (int(value) for value in rect)
    values = np.zeros(1, dtype=YUV_PARAMS_DTYPE)
    values["rect"] = np.asarray(rect, dtype=np.float32)
    values["coeffs"] = np.asarray(_MATRIX_COEFFS[uploader_matrix(int(frame.matrix))], dtype=np.float32)
    values["luma_texel_scale"] = np.asarray(
        (_f32(frame.width / view_w), _f32(frame.height / view_h)), dtype=np.float32)
    values["chroma_texel_scale"] = np.asarray(
        (_f32(c_w / view_w), _f32(c_h / view_h)), dtype=np.float32)
    values["chroma_offset_texels"] = np.asarray(
        (_f32(offset[0] * c_w), _f32(offset[1] * c_h)), dtype=np.float32)
    values["chroma_scale"] = np.asarray(scale, dtype=np.float32)
    values["chroma_offset"] = np.asarray(offset, dtype=np.float32)
    values["max_val"] = max_val
    values["y_black"] = y_black
    values["y_range_inv"] = y_range_inv
    values["c_neutral"] = c_neutral
    values["c_range_inv"] = c_range_inv
    values["color_range"] = 1 if int(frame.color_range) else 0
    values["gbr_mode"] = 1 if "gbr" in str(frame.pixel_format or "") else 0
    return values


def params_key(frame: YuvFrame, rect: tuple[int, int, int, int]) -> tuple[Any, ...]:
    """Identity of a parameter set, for caching the small constant buffers."""
    return (
        tuple(int(value) for value in rect),
        frame.width, frame.height, int(frame.u.shape[1]), int(frame.u.shape[0]),
        int(frame.bit_depth), str(frame.subsampling), str(frame.pixel_format or ""),
        int(frame.matrix), int(frame.color_range), int(frame.chroma_siting),
        int(frame.source_width or frame.width), int(frame.source_height or frame.height),
    )


def current_session_refusal(qt_platform: str) -> str:
    """:func:`session_refusal` for this process (opt-in, platform, kill switch)."""
    from src.shared.gpu.metal.feature_switches import MetalFeature, switch_refusal

    refusal = session_refusal(
        requested=playback_metal_requested(),
        platform=sys.platform,
        qt_platform=qt_platform,
        native_viewport_disabled=switch_refusal(MetalFeature.NATIVE_VIEWPORT) is not None,
    )
    return refusal


__all__ = [
    "DEFAULT_MAX_TEXTURE",
    "ENV_PLAYBACK_METAL",
    "KERNEL_U8",
    "KERNEL_U16",
    "SUPPORTED_BIT_DEPTHS",
    "SUPPORTED_CHROMA_SITING",
    "SUPPORTED_MATRICES",
    "SUPPORTED_SUBSAMPLING",
    "YUV_PARAMS_DTYPE",
    "build_yuv_params",
    "chroma_sampling",
    "current_session_refusal",
    "device_max_texture",
    "display_size_refusal",
    "env_flag",
    "frame_refusal",
    "letterbox_rect",
    "params_key",
    "playback_metal_requested",
    "range_constants",
    "session_refusal",
    "uploader_matrix",
]
