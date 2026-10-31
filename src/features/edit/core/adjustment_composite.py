"""CPU helpers for applying Adjustment Clip geometry and opacity.

Adjustment Clips operate on the already-composited image below their z-order.
These helpers are shared by the realtime CPU fallback and the export path so
their Transform/Opacity semantics stay identical.
"""

from __future__ import annotations

from typing import Any

import numpy as np


def transform_is_identity(transform: Any) -> bool:
    """Return whether a TransformParams-style tuple is the identity."""
    if not transform or len(transform) != 8:
        return False
    try:
        if str(transform[5]) != "center":
            return False
        values = [float(transform[index]) for index in (0, 1, 2, 3, 4, 6, 7)]
    except (TypeError, ValueError, IndexError):
        return False
    identity = (0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    return all(abs(value - expected) <= 1e-4 for value, expected in zip(values, identity))


def apply_transform_cpu(
    frame_data: np.ndarray,
    transform: Any,
    viewport_w: int,
    viewport_h: int,
    coordinate_w: int | None = None,
    coordinate_h: int | None = None,
) -> np.ndarray:
    """Apply a pixel-space TransformParams tuple to a viewport-sized frame.

    ``coordinate_w``/``coordinate_h`` are the native sequence canvas size;
    they may differ from the proxy-sized output frame.
    """
    if not transform or transform_is_identity(transform):
        return frame_data
    try:
        if len(transform) != 8:
            return frame_data
        tx, ty, sx, sy, rotation = (float(transform[index]) for index in range(5))
        pivot_mode = str(transform[5])
        pivot_x, pivot_y = float(transform[6]), float(transform[7])
    except (TypeError, ValueError, IndexError):
        return frame_data

    import cv2

    coordinate_w = max(1, int(coordinate_w or viewport_w))
    coordinate_h = max(1, int(coordinate_h or viewport_h))
    output_scale_x = viewport_w / float(coordinate_w)
    output_scale_y = viewport_h / float(coordinate_h)

    if pivot_mode == "custom":
        anchor_x, anchor_y = pivot_x * output_scale_x, pivot_y * output_scale_y
    else:
        anchor_x, anchor_y = viewport_w / 2.0, viewport_h / 2.0

    radians = np.deg2rad(rotation)
    cos_r = float(np.cos(radians))
    sin_r = float(np.sin(radians))
    m00 = cos_r * sx
    m01 = -sin_r * sy
    m10 = sin_r * sx
    m11 = cos_r * sy
    m02 = anchor_x - m00 * anchor_x - m01 * anchor_y + tx * output_scale_x
    m12 = anchor_y - m10 * anchor_x - m11 * anchor_y + ty * output_scale_y
    affine = np.array([[m00, m01, m02], [m10, m11, m12]], dtype=np.float32)

    border_value: Any
    if frame_data.ndim == 3 and frame_data.shape[-1] == 4:
        border_value = (0, 0, 0, 0)
    elif frame_data.ndim == 3:
        border_value = (0, 0, 0)
    else:
        border_value = 0
    return cv2.warpAffine(
        frame_data,
        affine,
        (viewport_w, viewport_h),
        flags=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border_value,
    )


def blend_adjustment_cpu(
    base: np.ndarray,
    adjusted: np.ndarray,
    transform: Any,
    opacity: float,
    coordinate_w: int | None = None,
    coordinate_h: int | None = None,
) -> np.ndarray:
    """Blend an adjusted result over its original lower-layer composite.

    For a non-identity transform at 100% opacity, the transformed image
    replaces the lower-layer composite; this is what prevents a scaled
    Adjustment Clip from appearing as a small image over the original full
    image.  At lower opacity, the transformed result is blended with the
    original using a coverage mask.
    """
    if base.shape != adjusted.shape:
        import cv2
        adjusted = cv2.resize(adjusted, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_LINEAR)

    alpha = max(0.0, min(1.0, float(opacity)))
    has_transform = bool(transform and not transform_is_identity(transform))
    if has_transform and alpha >= 1.0 - 1e-4:
        return adjusted.astype(base.dtype, copy=False)

    if has_transform:
        coverage_source = np.ones_like(adjusted, dtype=np.float32)
        coverage = apply_transform_cpu(
            coverage_source, transform, base.shape[1], base.shape[0],
            coordinate_w, coordinate_h,
        )
        coverage = np.clip(coverage[..., :1], 0.0, 1.0)
        weight = coverage * alpha
    else:
        weight = alpha

    result = base.astype(np.float32) * (1.0 - weight) + adjusted.astype(np.float32) * weight
    return result.astype(base.dtype, copy=False)
