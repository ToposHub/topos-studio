"""Wipe transition mask generation (shared by preview + export compositors).

A wipe reveals the outgoing (left) clip being progressively covered by the
incoming (right) clip along a spatial boundary that sweeps across the frame
as the transition progresses.

This module is intentionally dependency-light (numpy only) so both the
uint8/cv2 preview path and the float32 numpy export path can reuse it.

Wipe parameters (read from ``TransitionInstance.params``):

- ``direction``: one of ``left`` | ``right`` | ``up`` | ``down`` (default
  ``left`` — the right clip wipes in from the left edge). Also accepts the
  legacy ``start_side`` spelling.
- ``angle``: degrees, used for a diagonal wipe when ``direction`` is absent
  or ``"angle"``. 0° = horizontal sweep, 90° = vertical sweep.
- ``softness``: 0.0..1.0 width of the feathered edge as a fraction of the
  frame's larger dimension (default 0.02).
- ``reverse``: bool — swap which clip is revealed first (default False).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, Mapping

import numpy as np

__all__ = ["get_wipe_params", "compute_wipe_mask"]


_VALID_DIRECTIONS = ("left", "right", "up", "down", "angle")


def _safe_float(value: Any, default: float) -> float:
    """AUD-19210: 外部参数值安全转 float。

    ``params`` 混入来源含用户自定义转场 JSON 与持久化的实例参数
    （``"angle": "45deg"`` / ``"softness": null`` 之类值此前使
    ``float()`` 抛 ValueError/TypeError → ``compute_wipe_mask`` →
    预览/导出中断）。坏值与 NaN/Inf 一律回退默认值——与
    ``transition_effects._float_param`` 同一模式。
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(result):
        return default
    return result


def get_wipe_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """Extract and normalise wipe parameters from a transition's params dict.

    Returns a dict with keys ``direction``, ``angle``, ``softness``,
    ``reverse`` — all clamped to valid ranges and with sensible defaults so
    callers can use them unconditionally.
    """
    p = params or {}
    direction = str(p.get("direction", p.get("start_side", "left"))).lower()
    if direction not in _VALID_DIRECTIONS:
        direction = "left"
    # AUD-19210：angle/softness 经 _safe_float 防护（坏外部参数不再崩渲染）。
    angle = _safe_float(p.get("angle", 0.0), 0.0)
    # Normalise angle to [0, 360)
    angle = angle % 360.0
    softness = _safe_float(p.get("softness", 0.02), 0.02)
    softness = max(0.0, min(0.5, softness))
    reverse = bool(p.get("reverse", False))
    return {"direction": direction, "angle": angle, "softness": softness, "reverse": reverse}


def compute_wipe_mask(
    width: int,
    height: int,
    progress: float,
    params: Mapping[str, Any] | None,
) -> np.ndarray:
    """Compute a per-pixel wipe mask in [0.0, 1.0].

    ``0.0`` means the pixel still shows the **left** (outgoing) clip; ``1.0``
    means it shows the **right** (incoming) clip. The boundary moves from
    fully-left (progress 0) to fully-right (progress 1).

    Args:
        width: frame width in pixels
        height: frame height in pixels
        progress: transition progress in [0.0, 1.0]
        params: wipe parameters (see :func:`get_wipe_params`)

    Returns:
        ``np.ndarray`` of shape ``(height, width)`` dtype ``float32``.
    """
    wp = get_wipe_params(params)
    progress = max(0.0, min(1.0, float(progress)))
    if wp["reverse"]:
        progress = 1.0 - progress

    # At the exact endpoints the transition is fully on one side — return a
    # constant mask so the feathered edge never bleeds into the boundary frame.
    if progress <= 0.0:
        return np.zeros((height, width), dtype=np.float32)
    if progress >= 1.0:
        return np.ones((height, width), dtype=np.float32)

    softness = wp["softness"]
    # Feather width in pixels (relative to the larger dimension).
    feather = max(1.0, softness * float(max(width, height)))

    direction = wp["direction"]
    angle = wp["angle"]

    # Build a normalised coordinate in [0, 1] along the wipe axis.
    # Semantics: mask==1 where the RIGHT (incoming) clip is visible.
    # The right clip sweeps in to cover a fraction `progress` of the frame,
    # measured along the sweep axis from the leading edge.
    if direction == "left":
        # Right clip enters from the left edge: leading edge at x=progress.
        coord = _axis_coord(width, height, horizontal=True)
        boundary = progress
    elif direction == "right":
        # Right clip enters from the right edge.
        coord = 1.0 - _axis_coord(width, height, horizontal=True)
        boundary = progress
    elif direction == "up":
        coord = _axis_coord(width, height, horizontal=False)
        boundary = progress
    elif direction == "down":
        coord = 1.0 - _axis_coord(width, height, horizontal=False)
        boundary = progress
    else:  # "angle" — project pixels onto the sweep direction
        coord = _angle_coord_cached(width, height, angle)
        boundary = progress

    # coord and boundary are in [0, 1]; convert feather to normalised units.
    span = float(max(width, height))
    feather_norm = feather / span

    # mask==1 (right clip visible) where coord <= boundary, with a smooth
    # feathered transition of width feather_norm centred on the boundary.
    # smoothstep maps coord in [boundary - feather, boundary + feather] → [1, 0].
    t = (boundary + feather_norm - coord) / (2.0 * feather_norm)
    t = np.clip(t, 0.0, 1.0)
    # smoothstep for a softer edge
    mask = t * t * (3.0 - 2.0 * t)
    return mask.astype(np.float32)


@lru_cache(maxsize=16)
def _axis_coord(width: int, height: int, horizontal: bool) -> np.ndarray:
    """Per-pixel coordinate in [0, 1] along the requested axis."""
    if horizontal:
        xs = np.linspace(0.0, 1.0, width, dtype=np.float32)
        coord = np.broadcast_to(xs, (height, width)).copy()
    else:
        ys = np.linspace(0.0, 1.0, height, dtype=np.float32)
        coord = np.broadcast_to(ys[:, None], (height, width)).copy()
    coord.flags.writeable = False
    return coord


@lru_cache(maxsize=16)
def _angle_coord(width: int, height: int, angle_deg: float) -> np.ndarray:
    """Per-pixel coordinate in [0, 1] projected onto the sweep direction.

    The projection is normalised so the full extent along the sweep axis
    maps to [0, 1].
    """
    theta = np.deg2rad(angle_deg)
    # Pixel centres in a centred coordinate system.
    xs = (np.arange(width, dtype=np.float32) - (width - 1) / 2.0)
    ys = (np.arange(height, dtype=np.float32) - (height - 1) / 2.0)
    grid_x = np.broadcast_to(xs, (height, width))
    grid_y = np.broadcast_to(ys[:, None], (height, width))
    # Project onto sweep direction vector (cos, sin).
    proj = grid_x * np.cos(theta) + grid_y * np.sin(theta)
    # Normalise to [0, 1].
    pmin = float(proj.min())
    pmax = float(proj.max())
    span = pmax - pmin
    if span < 1e-6:
        coord = np.zeros_like(proj)
    else:
        coord = (proj - pmin) / span
    coord.flags.writeable = False
    return coord


# AUD-19211：连续 float 角度直接作 lru_cache 键——拖动角度参数时每个
# 中间值都是 miss + 一次全分辨率投影数组重建（4K 单次数百万元素），
# 缓存形同虚设。按 0.1° 量化后相邻读数复用同一坐标数组，量化步长
# 对扫掠方向的视觉差异为亚像素级。
_ANGLE_KEY_QUANTUM_DEG = 0.1


@lru_cache(maxsize=16)
def _angle_coord_quantized(width: int, height: int,
                           angle_key: int) -> np.ndarray:
    return _angle_coord(width, height, angle_key * _ANGLE_KEY_QUANTUM_DEG)


def _angle_coord_cached(width: int, height: int, angle_deg: float) -> np.ndarray:
    """Quantised-key entry point for :func:`_angle_coord`."""
    key = int(round(float(angle_deg) / _ANGLE_KEY_QUANTUM_DEG))
    return _angle_coord_quantized(width, height, key)
