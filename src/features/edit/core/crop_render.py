"""FX-M06: the crop render-stage contract — single source of truth for crop
geometry shared by GPU preview, CPU fallback and timeline export.

Prior to FX-M06, ``CropParams`` lived on every clip (``ClipInstance.crop``),
was editable in the Inspector, participated in the render-cache hash, and
was then **silently dropped by all three renderers** — the GPU path wrote
``track_info["crop"]`` but never read it, and the CPU/export paths never
referenced it at all. The net effect was a "ghost parameter": changing it
invalidated the cache (so a re-render happened) but the re-render was
pixel-identical to before.

This module closes that gap by defining crop's geometry **once**, in pure
data, so the three renderers cannot drift:

* :func:`crop_rect_for_size` — normalised ``CropParams`` → pixel rectangle
  + feather radius in output space (image coordinates, row 0 = top).
* :func:`build_crop_mask` — a numpy alpha mask consumed by the CPU and
  export paths (and used as the reference the GPU shader must match).
* :func:`crop_is_identity` — fast no-op guard so an empty crop adds no pass.
* ``CROP_FRAGMENT_SHADER`` — the GPU fragment shader, kept here (not inline
  in the renderer) so the math it implements is reviewable next to the
  numpy reference.

Coordinate conventions (these are the contract — do not reinterpret per
backend):

* ``CropParams.top`` crops the **top of the image** (row 0), i.e. the
  viewer's top edge. This is the intuitive meaning a user expects from the
  Inspector.
* Image space is Y-down: row index 0 is the top row, row ``h-1`` the
  bottom. The CPU/export numpy paths work in image space directly.
* GPU texture space: the renderer's track FBOs use the GL-default
  (non-flipped) orientation, which stores the image **upside down** in the
  texture — the image's visual top (row 0) maps to ``v_texcoord.y ≈ 0``
  (low) and the visual bottom to ``v_texcoord.y ≈ 1``. This is the same
  orientation the transform/grade/composite passes operate in (they all use
  the non-flipped ``_blend_quad_vbo``); only the final present blit flips
  once for display. ``crop_gl_uniforms`` encodes this mapping
  (``fbo_y_flipped=False`` by default), so crop ``top`` removes low
  ``v_texcoord.y``. The flip lives ONLY in that helper + the shader's UV
  interpretation; the numpy path never sees it.

The crop is a **premultiplied-alpha matte**: outside the crop rect the
output is fully transparent (alpha 0), and RGB is multiplied by alpha so
no dirty edge colour leaks into later compositing. ``feather`` widens the
rect's boundary with a ``smoothstep`` alpha ramp for a soft edge.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np

if TYPE_CHECKING:
    from src.features.edit.core.clip_params import CropParams


# Normalised epsilon: crop amounts below this are treated as zero so a
# default CropParams() never allocates a mask / shader pass.
_CROP_EPS = 1e-6


@dataclass(frozen=True)
class CropRect:
    """Pixel-space crop rectangle in output coordinates (image space, Y-down).

    Attributes:
        x0, y0: inclusive top-left corner of the kept region, in pixels.
        x1, y1: exclusive bottom-right corner of the kept region.
            Pixels with ``x0 <= x < x1`` and ``y0 <= y < y1`` are inside.
        feather_px: soft-edge width in pixels (0 = hard edge). The feather
            ramp straddles each boundary symmetrically.
        width, height: the output size the rect was computed for (so the
            shader can be given normalised bounds without re-deriving).
    """

    x0: float
    y0: float
    x1: float
    y1: float
    feather_px: float
    width: int
    height: int


def crop_is_identity(crop: Optional["CropParams"]) -> bool:
    """True when ``crop`` removes nothing (no pass needed).

    A ``None`` crop or one with all four edges and feather at ~0 is an
    identity — the renderer must skip the crop pass entirely so an
    un-cropped clip pays zero cost (audit FX-M06: "empty crop adds no
    unnecessary pass, output equivalent to input").
    """
    if crop is None:
        return True
    if (abs(crop.left) < _CROP_EPS and abs(crop.right) < _CROP_EPS
            and abs(crop.top) < _CROP_EPS and abs(crop.bottom) < _CROP_EPS
            and abs(crop.feather) < _CROP_EPS):
        return True
    return False


def crop_rect_for_size(
    crop: "CropParams", width: int, height: int,
) -> CropRect:
    """Convert normalised ``CropParams`` to a pixel ``CropRect``.

    ``left/right/top/bottom`` are fractions of the corresponding output
    dimension in ``[0, 0.5]`` (validated by ``CropParams``). ``feather`` is
    interpreted in output pixels; because crop works in normalised output
    space it is also expressed as a fraction of the shorter side when fed
    to the GPU (see :data:`CROP_FRAGMENT_SHADER`).

    The kept rectangle is the central region left after removing the four
    margins. With the ``CropParams`` invariant (each side ≤ 0.5, opposing
    sides sum ≤ 0.9) the rectangle is always non-empty.
    """
    x0 = crop.left * float(width)
    x1 = (1.0 - crop.right) * float(width)
    y0 = crop.top * float(height)
    y1 = (1.0 - crop.bottom) * float(height)
    # Feather in pixels; clamp to half the smaller kept dimension so the
    # ramp can never invert (a feather larger than the rect would make the
    # smoothstep cross zero inside the kept region → all-transparent).
    kept = min(x1 - x0, y1 - y0)
    feather = max(0.0, min(float(crop.feather), kept * 0.5))
    return CropRect(
        x0=x0, y0=y0, x1=x1, y1=y1,
        feather_px=feather, width=int(width), height=int(height),
    )


def build_crop_mask(rect: CropRect) -> np.ndarray:
    """Build a ``[0,1]`` float32 alpha mask for the CPU/export paths.

    Returns an ``(height, width)`` array. Inside the rect the value is 1.0;
    outside it is 0.0; across the feather band it ramps via ``smoothstep``.
    This is the numerical reference the GPU shader must match — the
    ``test_crop_render_stage`` parity suite compares the shader output to
    this mask within SDR tolerance.
    """
    w, h = rect.width, rect.height
    ys = (np.arange(h, dtype=np.float32) + 0.5)[:, None]  # (h, 1) pixel centres
    xs = (np.arange(w, dtype=np.float32) + 0.5)[None, :]  # (1, w)

    def _edge_distance(coord: np.ndarray, lo: float, hi: float) -> np.ndarray:
        """Signed distance from the kept band [lo, hi]; <0 inside the feather
        ramp near a boundary, very negative well inside, positive outside."""
        d_lo = coord - lo
        d_hi = hi - coord
        return np.minimum(d_lo, d_hi)

    dx = _edge_distance(xs, rect.x0, rect.x1)  # (1, w)
    dy = _edge_distance(ys, rect.y0, rect.y1)  # (h, 1)
    # min distance to any of the four edges; <0 means outside the hard rect.
    dist = np.minimum(dx, dy)  # broadcast → (h, w)

    if rect.feather_px < _CROP_EPS:
        # Hard edge: alpha 1 inside (dist >= 0), 0 outside.
        return (dist >= 0.0).astype(np.float32)

    f = rect.feather_px
    # smoothstep over [-f, 0]: -f → 0, 0 → 1. Outside (< -f) stays 0, inside
    # (>= 0) stays 1. This gives a symmetric soft edge straddling the rect.
    t = np.clip(dist / f, -1.0, 0.0) + 1.0  # map [-f,0] → [0,1]
    sm = t * t * (3.0 - 2.0 * t)
    return sm.astype(np.float32)


def crop_gl_uniforms(
    crop: "CropParams", width: int, height: int,
    *, fbo_y_flipped: bool = False,
) -> tuple[tuple[float, float, float, float], tuple[float, float]]:
    """Compute the GL-space ``(u_crop_rect, u_feather)`` uniforms for the shader.

    The numpy path works in image space (Y-down: row 0 = top). The fragment
    shader works in GL texture space. The mapping between the two depends on
    the FBO's orientation, which this helper takes as ``fbo_y_flipped``:

    * ``fbo_y_flipped=False`` (GL default, used by Edit's track FBOs): the
      texture stores image content **upside down** — image top (row 0) maps
      to ``v_texcoord.y = 0`` (low), image bottom to ``v_texcoord.y = 1``.
      So crop ``top`` removes low ``v_texcoord.y``, ``bottom`` removes high.
      This matches the renderer's non-flipped ``_blend_quad_vbo`` track FBOs
      (see ``_transform_to_ndc_mat3_bytes(track_fbo_y_flipped=True)``).
    * ``fbo_y_flipped=True`` (image already upright in texture space): image
      top maps to ``v_texcoord.y = 1`` (high), so crop ``top`` removes high
      ``v_texcoord.y``, ``bottom`` removes low.

    Args:
        crop: the normalised crop params.
        width, height: output size in pixels.
        fbo_y_flipped: orientation of the FBO the shader will read/write —
            see above. The Edit GPU preview path passes False (its track FBOs
            are non-flipped / GL-default).

    Returns:
        ((x0, y0, x1, y1), (feather_x_uv, feather_y_uv)) — crop rect in GL
        UV units [0,1], and feather width in UV units for each axis.
    """
    rect = crop_rect_for_size(crop, width, height)
    nx0 = rect.x0 / float(width)
    nx1 = rect.x1 / float(width)
    ny0_img = rect.y0 / float(height)  # image-space top
    ny1_img = rect.y1 / float(height)  # image-space bottom
    if fbo_y_flipped:
        # Image upright in texture space: image top (ny0_img) → high GL v.
        gl_rect = (nx0, 1.0 - ny1_img, nx1, 1.0 - ny0_img)
    else:
        # GL default (track FBO): image top (ny0_img) → low GL v.
        gl_rect = (nx0, ny0_img, nx1, ny1_img)
    # The shader measures distance in normalised UV coordinates. Convert the
    # requested pixel feather per axis; using the shorter side for both axes
    # makes horizontal feathering too wide on 16:9 canvases.
    feather_uv = (
        rect.feather_px / float(width),
        rect.feather_px / float(height),
    )
    return gl_rect, feather_uv


def apply_crop_premultiplied(
    frame_rgba: np.ndarray, rect: CropRect,
) -> np.ndarray:
    """Apply the crop mask to an RGBA frame using premultiplied alpha.

    Used by the CPU fallback and export paths (which operate on numpy).
    ``frame_rgba`` is ``(H, W, 4)`` float in [0, 1] (or any range — only the
    alpha channel is modified multiplicatively). Returns a new array; the
    input is not mutated. Outside the crop rect the result is fully
    transparent with RGB zeroed (premultiplied), so a later alpha-blend
    contributes nothing — no dirty edge colour leaks through.
    """
    mask = build_crop_mask(rect)  # (H, W) in [0,1]
    # AUD-16317: astype 默认 copy=True 已返回全新数组，再 .copy() 每帧多一次
    # 全帧分配（4K RGBA float32 ≈ 132MB，预览路径逐帧调用）。
    out = frame_rgba.astype(np.float32)
    out[..., 0:3] *= mask[..., None]
    out[..., 3] *= mask
    return out


def apply_crop_straight_alpha(
    frame_rgba: np.ndarray, rect: CropRect,
) -> np.ndarray:
    """Crop an RGBA frame keeping **straight** (non-premultiplied) alpha.

    复验 P1-01：导出 CPU 合成按 straight alpha 消费——遮罩只缩放 alpha
    （coverage），RGB 不动；预乘式 RGB 缩放会把羽化边缘的颜色压暗，
    且结果被下游当 straight 合成时语义错误。输入任意数值范围
    （uint8/uint16/float），返回与输入同 dtype 的新数组。
    """
    mask = build_crop_mask(rect)  # (H, W) in [0,1]
    out = frame_rgba.copy()
    if np.issubdtype(out.dtype, np.integer):
        scale = float(np.iinfo(out.dtype).max)
        a = np.rint(
            out[..., 3].astype(np.float32) * mask
        ).clip(0.0, scale).astype(out.dtype)
    else:
        a = (out[..., 3].astype(np.float32) * mask).astype(out.dtype)
    out[..., 3] = a
    return out


def apply_crop_bgr(
    frame_bgr_u8: np.ndarray, crop: "CropParams",
) -> np.ndarray:
    """Apply the crop mask to an opaque BGR uint8 frame (CPU/export paths).

    The CPU fallback and export pipelines composite in **BGR uint8 without a
    per-pixel alpha channel** (they carry only a uniform clip opacity and use
    ``cv2.addWeighted``), so there is no real Porter-Duff alpha to clear.
    We approximate the GPU's premultiplied matte by **zeroing the RGB in the
    cropped region**: the cropped pixels contribute black, which on a single
    cropped layer over a black/transparent background matches the GPU output
    exactly (both are zero there). On a multi-layer stack the CPU's
    ``addWeighted`` blend is itself an approximation of true alpha compositing,
    so this stays consistent with the existing CPU composite contract rather
    than inventing a per-pixel-alpha path the rest of the pipeline doesn't
    support.

    Identity crops (no crop, or all-zero margins) return the frame unchanged
    — no allocation, no copy.

    Args:
        frame_bgr_u8: ``(H, W, 3)`` uint8 BGR frame (OpenCV convention).
        crop: normalised crop params.

    Returns:
        A new ``(H, W, 3)`` uint8 BGR frame (or the input array unchanged for
        identity crops).
    """
    if crop_is_identity(crop):
        return frame_bgr_u8
    h, w = frame_bgr_u8.shape[:2]
    rect = crop_rect_for_size(crop, w, h)
    mask = build_crop_mask(rect)  # (H, W) in [0,1]
    # Premultiplied: RGB *= mask. Scale mask to uint8 domain [0,255] and blend
    # via multiply (the cropped region goes to 0, kept region unchanged).
    mask_u8 = np.clip(mask * 255.0 + 0.5, 0, 255).astype(np.uint8)
    # Element-wise scale: out = (frame * mask) / 255 in integer domain.
    out = (frame_bgr_u8.astype(np.uint16) * mask_u8[..., None] // 255).astype(np.uint8)
    return out


# ── GPU fragment shader ──────────────────────────────────────────────────────
#
# Consumed by EditGpuPreviewRenderer._apply_crop. Kept in this module (not
# inline in the renderer) so the geometry it implements sits next to the
# numpy reference. The renderer pairs this fragment shader with its standard
# vertex shader (``in_vert``/``in_texcoord``/``u_transform`` → ``v_texcoord``)
# and binds the source track texture to unit 0.
#
# Convention notes:
# * The renderer's canonical vertex shader emits ``v_texcoord`` (NOT
#   ``v_uv``), so this fragment shader reads ``v_texcoord`` to match — the
#   renderer compiles it with its own vert source, no adapter needed.
# * GL texture space follows the GL convention: ``v_texcoord`` (0,0) is the
#   **bottom-left**, (1,1) the top-right. The renderer's track FBOs use the
#   **GL-default (non-flipped) orientation**, which stores the image upside
#   down: the image's visual top (row 0) maps to ``v_texcoord.y ≈ 0`` (low)
#   and the visual bottom to ``v_texcoord.y ≈ 1``. ``crop_gl_uniforms`` with
#   ``fbo_y_flipped=False`` (the default) produces the matching UV rect, so
#   crop ``top`` removes low ``v_texcoord.y`` and ``bottom`` removes high —
#   see the module docstring for the full convention.
#
# Uniforms:
#   u_texture     – sampler2D, the source track texture (unit 0)
#   u_crop_rect   – vec4 (x0, y0, x1, y1) in GL UV space [0,1], the kept rect
#   u_feather     – vec2, soft-edge width in GL UV units for X/Y. 0.0 → hard
#                   edge.

CROP_FRAGMENT_SHADER = """
#version 330
in vec2 v_texcoord;
out vec4 f_color;

uniform sampler2D u_texture;
uniform vec4 u_crop_rect;   // (x0, y0, x1, y1) in GL UV space
uniform vec2 u_feather;     // soft-edge in UV units (X/Y), 0 = hard

// Signed distance from the kept rect, in UV units. >=0 inside the hard rect,
// <0 inside the feather band, very negative well outside.
float crop_distance(vec2 uv) {
    float dx = min(uv.x - u_crop_rect.x, u_crop_rect.z - uv.x);
    float dy = min(uv.y - u_crop_rect.y, u_crop_rect.w - uv.y);
    return min(dx, dy);
}

void main() {
    vec4 src = texture(u_texture, v_texcoord);
    float d = crop_distance(v_texcoord);

    float alpha;
    if (u_feather.x <= 0.0 || u_feather.y <= 0.0) {
        // Hard edge.
        alpha = step(0.0, d);
    } else {
        // Normalise the signed distance per axis before taking the minimum.
        // This keeps feather in pixel units on non-square textures.
        float dx = min(v_texcoord.x - u_crop_rect.x,
                       u_crop_rect.z - v_texcoord.x) / u_feather.x;
        float dy = min(v_texcoord.y - u_crop_rect.y,
                       u_crop_rect.w - v_texcoord.y) / u_feather.y;
        float t = clamp(min(dx, dy), -1.0, 0.0) + 1.0;
        alpha = t * t * (3.0 - 2.0 * t);
    }

    // Premultiplied output: RGB scaled by alpha so no edge colour leaks.
    f_color = vec4(src.rgb * alpha, src.a * alpha);
}
"""
