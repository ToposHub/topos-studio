"""Pointwise Rec.709 Gamma 2.4 responses for the two Chroma Warper palettes.

The measurements use full-range RGB input and output. Extended-range values
stay in the renderer until the final output transform. The fit parameter sets
describe the HSP Chroma–Luma mesh and the Chroma Warp plot respectively.
"""
from __future__ import annotations

import numpy as np

from src.shared.color.warper_mesh import chroma_luma_mesh


__all__ = [
    "apply_chroma_luma_709", "apply_chroma_warp_709",
    "chroma_warp_position", "chroma_warp_color",
]

_CL_POWER = 2.45753646
_CL_LUMA_POWER = 3.75667569
_CL_WEIGHTS = np.array((0.22397788, 0.47992433, 0.29609779), dtype=np.float32)
_CL_AXES = np.array(((-0.04683667, -0.07817342, 0.28029710),
                     (0.20009512, -0.11323463, 0.01882541)), dtype=np.float32)
_CL_DIRECTION = np.array(((-0.66266926, -1.19713930, 4.13676913),
                          (5.42280301, -2.18619562, 0.54220069)), dtype=np.float32)
_CL_DIRECTION_SLOPE = np.array([
    [[0.43201332, 0.45234450, -0.38314720],
     [1.96193471, -0.10041713, -1.04219430],
     [0.32077624, -0.32574086, 1.85096508]],
    [[2.38611038, 0.51368426, -1.07686886],
     [0.64691750, 0.29826522, 0.13507348],
     [-0.36695460, 0.88090642, 0.41818304]],
], dtype=np.float32)
_CL_DIRECTION_LUMA_POWER = 1.36595878

_CW_GAMMA = 2.4
# Default-exposure captures preserve this luminance on every measured hue.
_CW_LUMA = np.array((0.20116587, 0.62251235, 0.17632178), dtype=np.float32)
_CW_PLOT_POWER = 2.19389422
_CW_PLOT_WEIGHTS = np.array((0.21131004, 0.43787194, 0.35081802), dtype=np.float32)
_CW_VERTICES = np.array(((0.27797970, 0.09583576),
                         (0.22359754, 0.71202325),
                         (0.75480842, 0.56694796)), dtype=np.float32)
_CW_PLOT_INVERSE = np.linalg.inv(np.vstack((_CW_VERTICES.T, np.ones(3))))
_CW_WIDTH = 0.04802809
_CW_RANGE = 0.04
_CW_EXPONENT = 2.51797926
_CW_FORWARD_ROLLOFF = 1.55147850
_CW_GAIN = 1.07972034


def _cl_sample(field: np.ndarray, chroma: np.ndarray, luma: np.ndarray) -> np.ndarray:
    height, width = field.shape[:2]
    x = np.clip(chroma, 0.0, 1.0) * (width - 1)
    y = np.clip(luma, 0.0, 1.0) * (height - 1)
    col = np.minimum(x.astype(np.intp), width - 2)
    row = np.minimum(y.astype(np.intp), height - 2)
    tx = (x - col.astype(x.dtype))[:, None]
    ty = (y - row.astype(y.dtype))[:, None]
    lower = field[row, col] * (1.0 - tx) + field[row, col + 1] * tx
    upper = field[row + 1, col] * (1.0 - tx) + field[row + 1, col + 1] * tx
    delta = lower * (1.0 - ty) + upper * ty
    inside = (chroma >= 0.0) & (chroma <= 1.0) & (luma <= 1.0)
    return np.where(inside[:, None], delta, 0.0)


def _cl_coordinates(rgb: np.ndarray, color_space: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    magnitude = np.maximum(rgb, 0.0)
    linear = magnitude ** _CL_POWER
    if color_space == "hsv":
        luma = linear.max(axis=1)
    elif color_space == "hsl":
        luma = (linear.max(axis=1) + linear.min(axis=1)) * 0.5
    elif color_space == "hsy":
        luma = linear @ np.array((0.2126, 0.7152, 0.0722), dtype=linear.dtype)
    else:
        luma = (magnitude ** _CL_LUMA_POWER @ _CL_WEIGHTS) ** (_CL_POWER / _CL_LUMA_POWER)
    ratio = linear / np.maximum(luma[:, None], 1e-12) - 1.0
    return linear, luma, ratio


def chroma_luma_position(rgb: np.ndarray, angle: float, color_space: str) -> np.ndarray:
    """Return normalized positions in each opponent grid, with screen Y down."""
    _, luma, ratio = _cl_coordinates(np.asarray(rgb).reshape(-1, 3), color_space)
    cosine, sine = np.cos(np.deg2rad(angle)), np.sin(np.deg2rad(angle))
    axes = np.array(((cosine, -sine), (sine, cosine)), dtype=ratio.dtype)
    chroma = ratio @ (axes @ _CL_AXES).T
    return np.stack((chroma + 0.5, np.broadcast_to(1.0 - luma[:, None], chroma.shape)), axis=-1)


def _cl_one(rgb: np.ndarray, field: np.ndarray, axis: np.ndarray, color_space: str) -> np.ndarray:
    linear, luma, ratio = _cl_coordinates(rgb, color_space)
    chroma = ratio @ (axis @ _CL_AXES)
    delta = _cl_sample(field, chroma + 0.5, luma)
    direction = axis @ _CL_DIRECTION + ratio @ np.einsum("i,ijk->jk", axis, _CL_DIRECTION_SLOPE)
    warped = linear * np.maximum(1.0 + delta[:, 1] / np.maximum(luma, 1e-12), 0.0)[:, None]
    warped += direction * (delta[:, 0] * luma ** _CL_DIRECTION_LUMA_POWER)[:, None]
    adjusted = np.maximum(warped, 0.0) ** (1.0 / _CL_POWER) + np.minimum(rgb, 0.0)
    return np.where(np.any(delta != 0.0, axis=-1)[:, None], adjusted, rgb)


def apply_chroma_luma_709(
    rgb: np.ndarray, grids: tuple[np.ndarray, np.ndarray], angle: float,
    *, color_space: str = "hsp",
) -> np.ndarray:
    """Apply the two opponent grids, with dc/dl/pin per vertex and bottom row first."""
    source = np.asarray(rgb)
    colors = source.reshape(-1, 3).astype(np.result_type(source.dtype, np.float32), copy=False)
    radians = np.deg2rad(float(angle))
    cosine, sine = np.cos(radians), np.sin(radians)
    axes = np.array(((cosine, -sine), (sine, cosine)), dtype=colors.dtype)
    for grid, axis in zip(grids, axes, strict=True):
        field = chroma_luma_mesh(grid)
        if field is not None:
            colors = _cl_one(colors, field.astype(colors.dtype, copy=False), axis, color_space)
    return colors.reshape(source.shape).astype(source.dtype, copy=False)


def chroma_warp_position(rgb: np.ndarray) -> np.ndarray:
    """Project encoded RGB onto the plot where black and gray share the white point."""
    source = np.asarray(rgb)
    colors = np.maximum(source.astype(np.result_type(source.dtype, np.float32), copy=False), 0.0)
    peak = np.max(colors, axis=-1, keepdims=True)
    colors = np.divide(colors, peak, out=np.zeros_like(colors), where=peak > 0.0)
    values = colors ** _CW_PLOT_POWER * _CW_PLOT_WEIGHTS
    total = values.sum(axis=-1, keepdims=True)
    white = _CW_PLOT_WEIGHTS @ _CW_VERTICES
    return np.where(total > 1e-12, values @ _CW_VERTICES / np.maximum(total, 1e-12), white)


def _cw_plot_to_linear(position: np.ndarray) -> np.ndarray:
    inverse = _CW_PLOT_INVERSE.astype(position.dtype, copy=False)
    barycentric = position @ inverse[:, :2].T + inverse[:, 2]
    ratios = (np.maximum(barycentric, 0.0) / _CW_PLOT_WEIGHTS) ** (_CW_GAMMA / _CW_PLOT_POWER)
    return ratios / np.maximum((ratios @ _CW_LUMA)[..., None], 1e-12)


def chroma_warp_color(position: np.ndarray) -> np.ndarray:
    """Reconstruct a unit-luminance encoded RGB anchor from the plot."""
    return _cw_plot_to_linear(np.asarray(position, dtype=np.float64)) ** (1.0 / _CW_GAMMA)


def _smoothstep(unit: np.ndarray) -> np.ndarray:
    limited = np.clip(unit, 0.0, 1.0)
    return limited * limited * (3.0 - 2.0 * limited)


def _cw_tonal(luma: np.ndarray, low: float, high: float, pivot: float) -> np.ndarray | float:
    # Resolve's 1/1 defaults are exposure-independent. Narrower ranges fade
    # into the selected dark or bright side of the pivot.
    if low == 1.0 and high == 1.0:
        return 1.0
    shadow = _smoothstep(luma / max(pivot, 1e-6))
    highlight = _smoothstep((1.0 - luma) / max(1.0 - pivot, 1e-6))
    return (low + (1.0 - low) * shadow) * (high + (1.0 - high) * highlight)


def _cw_kernel(offset: np.ndarray, chroma_range: float) -> np.ndarray:
    width = _CW_WIDTH * max(chroma_range, 1e-6) / _CW_RANGE
    distance_squared = np.einsum("ij,ij->i", offset, offset)
    return np.exp(-((distance_squared / (width * width)) ** (_CW_EXPONENT * 0.5)))


def _cw_constraints(data: np.ndarray) -> np.ndarray:
    result = np.asarray(data, dtype=np.float64)
    if result.size == 0:
        return result.reshape(0, 4, 3)
    if result.ndim != 3 or result.shape[1:] != (4, 3) or not np.isfinite(result).all():
        raise ValueError("Chroma Warp expects finite N×4×3 stroke or pin constraints")
    return result


def chroma_warp_inputs(colors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Project fixed input colors once when repeatedly warping the same cube."""
    return np.maximum(colors, 0.0) ** _CW_GAMMA @ _CW_LUMA, chroma_warp_position(colors)


def apply_chroma_warp_709(
    rgb: np.ndarray, strokes: np.ndarray, pins: np.ndarray, *,
    inputs: tuple[np.ndarray, np.ndarray] | None = None,
) -> np.ndarray:
    """Apply plot constraints recorded as four triples per stroke or pin.

    The rows store (sx, sy, tx), (ty, range, exposure), (low, high, pivot)
    and (mode, 0, 0). Pins use coincident start and target positions.
    """
    source = np.asarray(rgb)
    active = _cw_constraints(strokes)
    if not active.size:
        return source.copy()
    protected = _cw_constraints(pins)
    colors = source.reshape(-1, 3).astype(np.result_type(source.dtype, np.float32), copy=False)
    active = active.astype(colors.dtype, copy=False)
    protected = protected.astype(colors.dtype, copy=False)
    luma, plot = inputs if inputs is not None else chroma_warp_inputs(colors)
    tonal_luma = np.minimum(luma ** (1.0 / _CW_GAMMA), 1.0)
    shift = np.zeros_like(plot)
    exposure_shift = np.zeros_like(luma)
    weight_total = np.zeros_like(luma)
    for (sx, sy, tx), (ty, chroma_range, exposure), (low, high, pivot), (mode, _, _) in active:
        start = np.array((sx, sy), dtype=plot.dtype)
        target = np.array((tx, ty), dtype=plot.dtype)
        direction = target - start
        difference = plot - start
        travel = np.clip((difference @ direction) / max(direction @ direction, 1e-12),
                         0.0, 1.0 if mode < 0.5 else 0.0)
        tonal = _cw_tonal(tonal_luma, low, high, pivot)
        weight = _cw_kernel(difference - travel[:, None] * direction, chroma_range) * tonal
        weight *= (1.0 - 0.5 * travel) ** (_CW_FORWARD_ROLLOFF - 1.0)
        shift += weight[:, None] * (_CW_GAIN * direction)
        weight_total += weight
        if exposure != 0.0:
            exposure_shift += exposure * tonal * _cw_kernel(plot - target, chroma_range)
    protection = np.ones_like(luma)
    for (sx, sy, _), (_, chroma_range, _), (low, high, pivot), _ in protected:
        center = np.array((sx, sy), dtype=plot.dtype)
        weight = _cw_kernel(plot - center, chroma_range) * _cw_tonal(tonal_luma, low, high, pivot)
        protection *= np.maximum(1.0 - weight, 0.0)
    influence = protection / np.maximum(weight_total, 1.0)
    shift *= influence[:, None]
    exposure_shift *= influence
    changed = np.any(np.abs(shift) > 1e-12, axis=-1) | (np.abs(exposure_shift) > 1e-12)
    result = colors.copy()
    adjusted_luma = luma[changed] * np.exp2(exposure_shift[changed])
    linear = _cw_plot_to_linear(plot[changed] + shift[changed]) * adjusted_luma[:, None]
    result[changed] = linear ** (1.0 / _CW_GAMMA) + np.minimum(colors[changed], 0.0)
    return result.reshape(source.shape).astype(source.dtype, copy=False)
