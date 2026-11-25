"""Control geometry shared by the Color Warper canvas and renderer."""
from __future__ import annotations

from functools import lru_cache

import numpy as np


def _tent(position: np.ndarray, anchor: np.ndarray) -> np.ndarray:
    """Piecewise linear support between the anchor and the outer boundaries."""
    lower = np.divide(position, anchor, out=np.ones(np.broadcast_shapes(position.shape, anchor.shape)),
                      where=anchor > 0.0)
    upper = np.divide(1.0 - position, 1.0 - anchor, out=np.ones_like(lower), where=anchor < 1.0)
    return np.clip(np.minimum(lower, upper), 0.0, 1.0)


def _chroma_basis(height: int, width: int, mask: bytes) -> tuple[np.ndarray, np.ndarray, tuple[np.ndarray, np.ndarray]]:
    from scipy.linalg import lu_factor

    rows, cols = np.nonzero(np.frombuffer(mask, dtype=np.bool_).reshape(height, width))
    x, y = np.linspace(0.0, 1.0, width), np.linspace(0.0, 1.0, height)
    horizontal, vertical = _tent(x[:, None], x[cols]), _tent(y[:, None], y[rows])
    factorization = lu_factor(horizontal[cols] * vertical[rows], check_finite=False)
    return horizontal, vertical, factorization


# An ordinary drag moves the same authored anchors. Cache the expensive
# factorization by topology while the positions continue to change.
_cached_chroma_basis = lru_cache(maxsize=3)(_chroma_basis)


def chroma_luma_mesh(grid: np.ndarray) -> np.ndarray | None:
    """Convert moved and pinned vertices into a dense bilinear mesh."""
    from scipy.linalg import lu_solve

    values = np.asarray(grid, dtype=np.float64)
    if values.ndim != 3 or values.shape[2] != 3 or min(values.shape[:2]) < 2:
        raise ValueError("Chroma–Luma expects at least a 2×2 grid of dc, dl and pin values")
    if not np.isfinite(values).all():
        raise ValueError("Chroma–Luma grid values must be finite")
    height, width = values.shape[:2]
    x = np.linspace(0.0, 1.0, width)
    y = np.linspace(0.0, 1.0, height)
    offsets = np.empty_like(values[..., :2])
    offsets[..., 0] = np.clip(values[..., 0], -x, 1.0 - x)
    offsets[..., 1] = np.clip(values[..., 1], -y[:, None], 1.0 - y[:, None])
    if not offsets.any():
        return None
    mask = np.any(offsets != 0.0, axis=-1) | (values[..., 2] != 0.0)
    rows, cols = np.nonzero(mask)
    if len(rows) == height * width:
        return offsets
    # Cardinal interpolation fixes every edited or pinned position while an
    # isolated edit retains the measured tent across all unpinned vertices.
    basis = _cached_chroma_basis if len(rows) <= 1600 else _chroma_basis
    horizontal, vertical, factorization = basis(height, width, mask.tobytes())
    coefficients = lu_solve(factorization, offsets[rows, cols], check_finite=False)
    field = np.einsum("ri,ci,ik->rck", vertical, horizontal, coefficients, optimize=True)
    field[rows, cols] = offsets[rows, cols]
    return field
