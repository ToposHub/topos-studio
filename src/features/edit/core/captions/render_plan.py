"""SUB-P1-10: canonical subtitle transform — single source of truth.

Preview (QPainter) and export (CPU warp) previously re-derived the subtitle
transform math independently, risking drift in pivot handling and transform
order. This module defines the transform ONCE:

* :func:`pivot_texture_local` — canvas pivot → texture/bbox-local pivot
  (the conversion that was duplicated in four places).
* :func:`build_affine_ydown` — the canonical 2×3 affine (scale → rotate about
  pivot → translate) in **Y-down image space**, returned as a flat
  ``(m00, m01, m02, m10, m11, m12)`` tuple matching ``QTransform`` layout.

Both the export warp and the preview painter consume the same affine, so a
custom-pivot cue lands on identical pixels in preview and burned output. The
color/alpha contract (premultiplied vs straight) is the caller's
responsibility; this module is pure geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Tuple


@dataclass(frozen=True)
class SubtitleTransform:
    """A resolved subtitle transform in Y-down image space.

    All fields are in texture-local pixels unless noted. ``m`` is the flat
    2×3 affine (row-major: m00 m01 m02 / m10 m11 m12) mapping a texture-local
    source point to its canvas-space destination.
    """

    pivot_x: float
    pivot_y: float
    # 2x3 affine, row-major, flat tuple of 6 floats.
    m: Tuple[float, float, float, float, float, float]


def pivot_texture_local(
    clip: Any,
    bbox_x: float,
    bbox_y: float,
    tex_w: float,
    tex_h: float,
    style: Mapping[str, Any] | None = None,
) -> Tuple[float, float]:
    """Resolve a clip's pivot to texture-local (bbox-relative) pixels.

    ``center`` mode → texture centre; ``custom`` mode → ``clip.pivot_x/y``
    shifted by the bbox origin. This is the single conversion every consumer
    (renderer warp, preview painter, handles) must use.
    """
    def _value(name: str, default: Any) -> Any:
        if style is not None and style.get(name) is not None:
            return style[name]
        return getattr(clip, name, default)

    mode = _value("pivot_mode", "center")
    if mode == "custom":
        return float(_value("pivot_x", 0.0)) - float(bbox_x), \
               float(_value("pivot_y", 0.0)) - float(bbox_y)
    return tex_w / 2.0, tex_h / 2.0


def build_affine_ydown(
    translate_x: float,
    translate_y: float,
    scale_x: float,
    scale_y: float,
    rotation_deg: float,
    pivot_x: float,
    pivot_y: float,
) -> Tuple[float, float, float, float, float, float]:
    """Build the canonical scale-then-rotate-about-pivot Y-down affine.

    Maps a texture-local source point ``(lx, ly)`` to its canvas-space
    destination:
        dst_x = sx*lx*cos - sy*ly*sin + (pivot_x*(1-sx*cos) + pivot_y*sy*sin + tx)
        dst_y = sx*lx*sin + sy*ly*cos + (pivot_y*(1-sy*cos) - pivot_x*sx*sin + ty)

    The translation terms fold the pivot anchor + user translate into m02/m12.
    Returns ``(m00, m01, m02, m10, m11, m12)`` row-major, matching QTransform's
    ``(m11, m12, m13=0, m21, m22, m23=0, m31, m32, m33=1)`` first two rows.
    """
    rad = math.radians(float(rotation_deg))
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    sx = float(scale_x)
    sy = float(scale_y)
    px = float(pivot_x)
    py = float(pivot_y)
    tx = float(translate_x)
    ty = float(translate_y)

    m00 = sx * cos_r
    m01 = -sy * sin_r
    m10 = sx * sin_r
    m11 = sy * cos_r
    # Anchor term: keep pivot fixed under scale+rotate, then add user translate.
    m02 = px - m00 * px - m01 * py + tx
    m12 = py - m10 * px - m11 * py + ty
    return (m00, m01, m02, m10, m11, m12)


def from_clip(
    clip: Any,
    bbox_x: float,
    bbox_y: float,
    tex_w: float,
    tex_h: float,
    style: Mapping[str, Any] | None = None,
) -> SubtitleTransform:
    """Resolve a SubtitleClip into a canonical Y-down transform.

    Convenience combining pivot resolution + affine build. The resulting affine
    maps texture-local source pixels to canvas-space destinations.
    """
    def _value(name: str, default: Any) -> Any:
        if style is not None and style.get(name) is not None:
            return style[name]
        return getattr(clip, name, default)

    piv_x, piv_y = pivot_texture_local(
        clip, bbox_x, bbox_y, tex_w, tex_h, style=style
    )
    m = build_affine_ydown(
        translate_x=float(_value("translate_x", 0.0)),
        translate_y=float(_value("translate_y", 0.0)),
        scale_x=float(_value("scale_x", 1.0)),
        scale_y=float(_value("scale_y", 1.0)),
        rotation_deg=float(_value("rotation", 0.0)),
        pivot_x=piv_x,
        pivot_y=piv_y,
    )
    return SubtitleTransform(pivot_x=piv_x, pivot_y=piv_y, m=m)


def apply_affine(
    m: Tuple[float, float, float, float, float, float],
    lx: float,
    ly: float,
) -> Tuple[float, float]:
    """Apply a Y-down 2×3 affine to a texture-local point. Pure helper."""
    m00, m01, m02, m10, m11, m12 = m
    return (m00 * lx + m01 * ly + m02, m10 * lx + m11 * ly + m12)
