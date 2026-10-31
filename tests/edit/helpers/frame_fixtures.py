"""Deterministic small-size RGBA frame generators (FX-M08).

Replaces the scattered ``np.zeros((8, 8, 3))`` ad-hoc fixtures with a single
shared set of generators. Every generator returns a *plain numpy array* (no
PyQt6 / GL), so the same fixture can feed GPU upload, CPU fallback and
export paths — which is exactly what golden-frame parity needs: identical
input → compare the three outputs.

Conventions:

* Channels are **RGBA**, shape ``(H, W, 4)``, unless noted.
* SDR generators return ``uint8`` in ``[0, 255]``.
* HDR generators return ``float32`` in ``[0, 1]`` but with values that
  *exceed* 1.0 somewhere, so 8-bit quantisation (clamping to 1.0) is
  detectable — this is the banding signal FX-M01 tests rely on.
* Top-left origin is ``(0, 0)``; row index grows downward (image space,
  Y-down). This matches both cv2 and the numpy composite loops.
"""
from __future__ import annotations

import numpy as np


# ── SDR (uint8 0..255) ─────────────────────────────────────────────────────

def solid_rgba_frame(
    width: int, height: int, rgba: tuple[int, int, int, int],
) -> np.ndarray:
    """A flat ``rgba`` fill. Default alpha 255 (opaque)."""
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 0] = rgba[0]
    arr[:, :, 1] = rgba[1]
    arr[:, :, 2] = rgba[2]
    arr[:, :, 3] = rgba[3] if len(rgba) > 3 else 255
    return arr


def checker_frame(
    width: int, height: int, cell: int = 2,
    on: tuple[int, int, int] = (255, 255, 255),
    off: tuple[int, int, int] = (0, 0, 0),
) -> np.ndarray:
    """An RGBA checkerboard; alpha is fully opaque.

    Useful for catching axis swaps (left/right, top/bottom) because the
    pattern is spatially unambiguous.
    """
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 3] = 255
    ys = np.arange(height) // max(cell, 1)
    xs = np.arange(width) // max(cell, 1)
    grid = (ys[:, None] + xs[None, :]) % 2 == 0
    on_arr = np.array([on[0], on[1], on[2], 255], dtype=np.uint8)
    off_arr = np.array([off[0], off[1], off[2], 255], dtype=np.uint8)
    arr[grid] = on_arr
    arr[~grid] = off_arr
    return arr


def vertical_gradient_frame(
    width: int, height: int,
    top: tuple[int, int, int] = (0, 0, 0),
    bottom: tuple[int, int, int] = (255, 255, 255),
) -> np.ndarray:
    """A vertical luma ramp — the classic banding-detection gradient.

    Alpha is fully opaque. Row 0 = ``top``, last row = ``bottom``.
    """
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 3] = 255
    if height <= 1:
        t = np.zeros(height, dtype=np.float32)
    else:
        t = np.linspace(0.0, 1.0, height, dtype=np.float32)
    for ch in range(3):
        col = top[ch] + (bottom[ch] - top[ch]) * t
        arr[:, :, ch] = np.clip(col, 0, 255).astype(np.uint8)[:, None]
    return arr


def horizontal_half_frame(
    width: int, height: int,
    left: tuple[int, int, int] = (255, 0, 0),
    right: tuple[int, int, int] = (0, 255, 0),
) -> np.ndarray:
    """Left half one colour, right half another — a sharp vertical edge.

    The audit's crop fixtures assert left/right/top/bottom crop by checking
    which half survives, so a single sharp edge is the minimal probe.
    """
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 3] = 255
    mid = width // 2
    arr[:, :mid, 0:3] = left
    arr[:, mid:, 0:3] = right
    return arr


def vertical_half_frame(
    width: int, height: int,
    top: tuple[int, int, int] = (0, 0, 255),
    bottom: tuple[int, int, int] = (255, 255, 0),
) -> np.ndarray:
    """Top half one colour, bottom half another — a sharp horizontal edge."""
    arr = np.zeros((height, width, 4), dtype=np.uint8)
    arr[:, :, 3] = 255
    mid = height // 2
    arr[:mid, :, 0:3] = top
    arr[mid:, :, 0:3] = bottom
    return arr


# ── HDR (float32, exceeds 1.0) ─────────────────────────────────────────────

def hdr_gradient_frame(
    width: int, height: int, peak: float = 4.0,
) -> np.ndarray:
    """An HDR RGBA16F-style gradient in float32 that *peaks above 1.0*.

    Values span ``[0, peak]`` so any 8-bit clamp/quantisation shows up as a
    hard cap at 1.0 instead of the smooth ramp. The ramp is diagonal so a
    banding artefact produces visible stripes rather than a single flat
    region.

    Used by FX-M01 surface-precision tests to prove the effect chain keeps
    HDR headroom instead of collapsing to ``f1``.
    """
    yy = np.linspace(0.0, 1.0, height, dtype=np.float32)[:, None]
    xx = np.linspace(0.0, 1.0, width, dtype=np.float32)[None, :]
    ramp = (xx + yy) * 0.5 * peak  # diagonal 0..peak
    arr = np.zeros((height, width, 4), dtype=np.float32)
    arr[:, :, 0] = ramp
    arr[:, :, 1] = ramp * 0.5
    arr[:, :, 2] = ramp * 0.25
    arr[:, :, 3] = 1.0
    return arr


def rgba_to_texture_bytes(arr: np.ndarray) -> bytes:
    """Pack an ``(H, W, 4)`` uint8/float array into the bytes ModernGL expects.

    ModernGL ``texture.write()`` wants a flat buffer in row-major (H, W, C)
    order matching the texture's ``(width, height)``. This helper keeps the
    upload sites tidy and the channel order explicit (RGBA).
    """
    arr = np.ascontiguousarray(arr)
    return arr.tobytes()
