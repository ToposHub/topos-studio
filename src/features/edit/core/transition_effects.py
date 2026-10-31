"""CPU effects shared by native motion-blur transitions and GIF previews."""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np

from src.features.edit.core.transition_catalog import (
    FULL_FRAME_TRANSITION_TYPES,
    MOTION_BLUR_TRANSITION_TYPES,
    TRANSITION_EFFECT_TYPES,
)

__all__ = [
    "apply_full_frame_transition",
    "apply_transition_effect",
    "apply_transition_motion_blur",
]


_DIRECTION_ANGLES = {
    "left": 0.0,
    "right": 0.0,
    "up": 90.0,
    "down": 90.0,
}


def _transition_direction(transition_type: str) -> str:
    return transition_type.rsplit("_", 1)[-1]


def _float_param(
    params: Mapping[str, Any],
    key: str,
    default: float,
) -> float:
    """Read a numeric transition parameter without breaking a render."""
    try:
        value = float(params.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _apply_directional_motion_blur(
    frame: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Apply a progress-shaped directional streak to one transition source.

    The native transition compositor calls this for both sides immediately
    before its spatial mask.  The same helper is used by the preview GIF
    generator so the Library thumbnail and timeline rendering share the same
    look.  ``frame`` may be BGR/RGB or BGRA/RGBA and may be uint8 or float32;
    alpha is kept unchanged.
    """
    if transition_type not in MOTION_BLUR_TRANSITION_TYPES:
        return frame
    if frame is None or getattr(frame, "ndim", 0) != 3 or frame.shape[2] < 3:
        return frame

    p = params or {}
    try:
        progress = max(0.0, min(1.0, float(progress)))
    except (TypeError, ValueError):
        progress = 0.0
    pulse = math.sin(math.pi * progress)
    is_whip = transition_type.startswith("whip_pan_")
    default_radius = 34.0 if is_whip else 22.0
    radius = max(0.0, _float_param(p, "blur_radius", default_radius))
    shutter = max(0.0, min(2.0, _float_param(p, "shutter", 1.0)))
    mix = max(0.0, min(1.0, _float_param(
        p, "blur_mix", 0.92 if is_whip else 0.82)))
    # The pulse peaks around the cut and avoids a visible hard discontinuity at
    # either endpoint. Whip Pan intentionally has a longer, denser streak.
    radius *= pulse ** (0.72 if is_whip else 0.9) * shutter
    mix *= pulse ** 0.35
    if radius < 0.5 or mix <= 0.001:
        return frame

    import cv2

    size = max(3, min(127, int(round(radius)) * 2 + 1))
    kernel = np.zeros((size, size), dtype=np.float32)
    center = (size - 1) / 2.0
    angle = math.radians(_float_param(
        p, "angle", _DIRECTION_ANGLES[_transition_direction(transition_type)]))
    dx = math.cos(angle) * center
    dy = math.sin(angle) * center
    cv2.line(
        kernel,
        (int(round(center - dx)), int(round(center - dy))),
        (int(round(center + dx)), int(round(center + dy))),
        1.0,
        1,
    )
    kernel /= max(float(kernel.sum()), 1.0)

    source = np.asarray(frame)
    color = source[..., :3]
    filtered = cv2.filter2D(
        color,
        -1,
        kernel,
        borderType=cv2.BORDER_REPLICATE,
    )
    if is_whip:
        # A Whip Pan is more than a longer blur: the blurred image also
        # travels a few percent of the frame and settles back by the endpoint.
        # BORDER_REPLICATE keeps the source opaque while the spatial mask is
        # doing the actual reveal.
        travel = 0.06 * float(frame.shape[1]) * pulse ** 0.65
        if _transition_direction(transition_type) == "left":
            travel = -travel
        filtered = cv2.warpAffine(
            filtered,
            np.asarray(((1.0, 0.0, travel), (0.0, 1.0, 0.0)), dtype=np.float32),
            (int(frame.shape[1]), int(frame.shape[0])),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
    if np.issubdtype(source.dtype, np.integer):
        info = np.iinfo(source.dtype)
        output_color = np.rint(
            np.clip(
                color.astype(np.float32)
                + (filtered.astype(np.float32) - color.astype(np.float32)) * mix,
                info.min,
                info.max,
            )
        ).astype(source.dtype)
    else:
        output_color = (
            color.astype(np.float32)
            + (filtered.astype(np.float32) - color.astype(np.float32)) * mix
        ).astype(source.dtype, copy=False)

    output = source.copy()
    output[..., :3] = output_color
    return output


def _mix_effect(source: np.ndarray, effect: np.ndarray, mix: float) -> np.ndarray:
    """Mix an effect's RGB channels while preserving any source alpha."""
    source_color = source[..., :3].astype(np.float32)
    effect_color = effect[..., :3].astype(np.float32)
    output_color = source_color + (effect_color - source_color) * mix
    if np.issubdtype(source.dtype, np.integer):
        info = np.iinfo(source.dtype)
        output_color = np.rint(np.clip(output_color, info.min, info.max))
    output = source.copy()
    output[..., :3] = output_color.astype(source.dtype)
    return output


def _apply_source_opacity(frame: np.ndarray, opacity: float) -> np.ndarray:
    """Apply a source opacity before it is folded into a composite layer."""
    clamped_opacity = max(0.0, min(1.0, float(opacity)))
    if clamped_opacity >= 1.0:
        return frame
    output = frame.copy()
    color = output[..., :3].astype(np.float32) * clamped_opacity
    if np.issubdtype(output.dtype, np.integer):
        info = np.iinfo(output.dtype)
        color = np.rint(np.clip(color, info.min, info.max))
    output[..., :3] = color.astype(output.dtype)
    if output.ndim == 3 and output.shape[2] >= 4:
        alpha = output[..., 3].astype(np.float32) * clamped_opacity
        if np.issubdtype(output.dtype, np.integer):
            alpha = np.rint(np.clip(alpha, 0, np.iinfo(output.dtype).max))
        output[..., 3] = alpha.astype(output.dtype)
    return output


def _blend_transition_frames(
    first: np.ndarray,
    second: np.ndarray,
    progress: float,
    *,
    linear_light: bool = False,
) -> np.ndarray:
    """Blend two matched frames while preserving dtype and any alpha channel."""
    if first.shape != second.shape:
        raise ValueError("full-frame transition inputs must have equal shapes")
    first_color = first[..., :3]
    second_color = second[..., :3]
    if linear_light:
        # Video frames entering this helper are normally uint8, while the
        # export pre-composite path may already have normalised float frames.
        # Decode and re-encode in a display-linear approximation so Film
        # Dissolve does not produce the milky midpoint of a gamma blend.
        scale = (
            float(np.iinfo(first.dtype).max)
            if np.issubdtype(first.dtype, np.integer)
            else 1.0
        )
        first_work = np.power(
            np.maximum(first_color.astype(np.float32) / scale, 0.0), 2.2
        )
        second_work = np.power(
            np.maximum(second_color.astype(np.float32) / scale, 0.0), 2.2
        )
        color = np.power(
            np.maximum(
                first_work * (1.0 - progress) + second_work * progress,
                0.0,
            ),
            1.0 / 2.2,
        ) * scale
    else:
        color = (
            first_color.astype(np.float32) * (1.0 - progress)
            + second_color.astype(np.float32) * progress
        )

    output = first.copy()
    if np.issubdtype(first.dtype, np.integer):
        info = np.iinfo(first.dtype)
        color = np.rint(np.clip(color, info.min, info.max))
    output[..., :3] = color.astype(first.dtype)
    if first.shape[2] >= 4:
        alpha = (
            first[..., 3].astype(np.float32) * (1.0 - progress)
            + second[..., 3].astype(np.float32) * progress
        )
        if np.issubdtype(first.dtype, np.integer):
            alpha = np.rint(np.clip(alpha, 0, np.iinfo(first.dtype).max))
        output[..., 3] = alpha.astype(first.dtype)
    return output


def _gaussian_blur_frame(frame: np.ndarray, sigma: float) -> np.ndarray:
    """Blur only RGB channels and leave alpha untouched."""
    if sigma <= 0.01:
        return frame.copy()
    import cv2

    source = np.asarray(frame)
    color = source[..., :3]
    kernel_side = max(3, min(127, int(math.ceil(sigma * 6.0)) | 1))
    blurred = cv2.GaussianBlur(
        color,
        (kernel_side, kernel_side),
        sigmaX=sigma,
        sigmaY=sigma,
        borderType=cv2.BORDER_REPLICATE,
    )
    output = source.copy()
    output[..., :3] = blurred
    return output


def _apply_smooth_cut(
    first: np.ndarray,
    second: np.ndarray,
    progress: float,
    params: Mapping[str, Any],
) -> np.ndarray:
    """Warp both sources toward an optical-flow midpoint before blending.

    This is intentionally a CPU path.  Analysis is performed on a bounded
    thumbnail and the resulting flow is upscaled, keeping the transition
    usable on large frames while avoiding a geometric wipe boundary.  It is
    best suited to similar framing, talking heads, and small jump cuts; very
    different scenes should use Blur Dissolve instead.
    """
    import cv2

    source_a = np.asarray(first)
    source_b = np.asarray(second)
    if source_a.ndim != 3 or source_b.ndim != 3:
        return _blend_transition_frames(source_a, source_b, progress)
    height, width = source_a.shape[:2]
    analysis_width = int(max(64.0, min(320.0, _float_param(
        params, "flow_width", 320.0))))
    analysis_width = min(width, analysis_width)
    analysis_height = max(32, int(round(height * analysis_width / max(width, 1))))
    small_a = cv2.resize(
        source_a[..., :3], (analysis_width, analysis_height),
        interpolation=cv2.INTER_AREA,
    )
    small_b = cv2.resize(
        source_b[..., :3], (analysis_width, analysis_height),
        interpolation=cv2.INTER_AREA,
    )
    gray_a = cv2.cvtColor(small_a, cv2.COLOR_BGR2GRAY)
    gray_b = cv2.cvtColor(small_b, cv2.COLOR_BGR2GRAY)
    flow_ab = cv2.calcOpticalFlowFarneback(
        gray_a, gray_b, None, 0.5, 3, 15, 2, 5, 1.2, 0
    )
    flow_ba = cv2.calcOpticalFlowFarneback(
        gray_b, gray_a, None, 0.5, 3, 15, 2, 5, 1.2, 0
    )
    flow_ab = cv2.resize(flow_ab, (width, height), interpolation=cv2.INTER_LINEAR)
    flow_ba = cv2.resize(flow_ba, (width, height), interpolation=cv2.INTER_LINEAR)
    flow_ab[..., 0] *= width / max(float(analysis_width), 1.0)
    flow_ab[..., 1] *= height / max(float(analysis_height), 1.0)
    flow_ba[..., 0] *= width / max(float(analysis_width), 1.0)
    flow_ba[..., 1] *= height / max(float(analysis_height), 1.0)
    flow_scale = max(0.0, min(1.0, _float_param(params, "flow_strength", 1.0)))
    flow_ab = np.nan_to_num(flow_ab, nan=0.0, posinf=0.0, neginf=0.0)
    flow_ba = np.nan_to_num(flow_ba, nan=0.0, posinf=0.0, neginf=0.0)
    flow_ab *= flow_scale
    flow_ba *= flow_scale

    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    map_a = (
        xx - flow_ab[..., 0] * progress,
        yy - flow_ab[..., 1] * progress,
    )
    map_b = (
        xx - flow_ba[..., 0] * (1.0 - progress),
        yy - flow_ba[..., 1] * (1.0 - progress),
    )
    warped_a = source_a.copy()
    warped_b = source_b.copy()
    warped_a[..., :3] = cv2.remap(
        source_a[..., :3], map_a[0], map_a[1], cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    warped_b[..., :3] = cv2.remap(
        source_b[..., :3], map_b[0], map_b[1], cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_REPLICATE,
    )
    return _blend_transition_frames(warped_a, warped_b, progress)


def apply_full_frame_transition(
    first: np.ndarray,
    second: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any] | None = None,
    *,
    first_opacity: float = 1.0,
    second_opacity: float = 1.0,
) -> np.ndarray:
    """Render a no-mask, full-frame transition between two source frames.

    Unlike the stylized motion batch, these transitions never reveal the
    incoming source through a shape or directional wipe.  The entire frame is
    blended together, with defocus or flow carrying the edit point through the
    least legible moment.
    """
    if transition_type not in FULL_FRAME_TRANSITION_TYPES:
        return first.copy()
    try:
        normalised_progress = max(0.0, min(1.0, float(progress)))
    except (TypeError, ValueError):
        normalised_progress = 0.0
    first = _apply_source_opacity(first, first_opacity)
    second = _apply_source_opacity(second, second_opacity)
    if normalised_progress <= 0.0:
        return first.copy()
    if normalised_progress >= 1.0:
        return second.copy()
    resolved = params or {}

    if transition_type == "film_dissolve":
        return _blend_transition_frames(
            first, second, normalised_progress, linear_light=True
        )
    if transition_type == "smooth_cut":
        try:
            import cv2
        except ImportError:
            return _blend_transition_frames(first, second, normalised_progress)
        try:
            return _apply_smooth_cut(
                first, second, normalised_progress, resolved
            )
        except (cv2.error, ValueError):
            # A flow failure should still produce a valid, neutral transition;
            # it must never turn an otherwise renderable edit into a crash.
            return _blend_transition_frames(first, second, normalised_progress)

    pulse = math.sin(math.pi * normalised_progress)
    default_ratio = 0.075 if transition_type == "defocus_dissolve" else 0.045
    blur_ratio = max(0.0, _float_param(resolved, "blur_ratio", default_ratio))
    sigma = min(
        96.0,
        blur_ratio * min(first.shape[0], first.shape[1]) * pulse ** 0.72,
    )
    blur_mix = pulse ** 0.65
    blurred_first = _gaussian_blur_frame(first, sigma)
    blurred_second = _gaussian_blur_frame(second, sigma)
    softened_first = _mix_effect(first, blurred_first, blur_mix)
    softened_second = _mix_effect(second, blurred_second, blur_mix)
    return _blend_transition_frames(
        softened_first,
        softened_second,
        normalised_progress * normalised_progress
        * (3.0 - 2.0 * normalised_progress),
    )


def _apply_radial_blur(
    frame: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any],
) -> np.ndarray:
    """Create a zoom or rotational blur from a small set of centred samples."""
    import cv2

    source = np.asarray(frame)
    color = source[..., :3]
    pulse = math.sin(math.pi * progress)
    if pulse <= 1e-6:
        return source
    is_zoom = transition_type.startswith("zoom_blur_")
    is_increasing = transition_type.endswith("_in") or transition_type.endswith("_cw")
    sample_count = int(max(3.0, min(9.0, _float_param(params, "samples", 7.0))))
    mix = max(0.0, min(1.0, _float_param(params, "blur_mix", 0.88)))
    mix *= pulse ** 0.35

    height, width = color.shape[:2]
    center = (width * 0.5, height * 0.5)
    if is_zoom:
        strength = max(0.0, _float_param(params, "blur_strength", 0.18))
        strength *= pulse ** 0.72
        sign = 1.0 if is_increasing else -1.0
        values = np.linspace(0.0, sign * strength, sample_count)
        accum = np.zeros_like(color, dtype=np.float32)
        for amount in values:
            scale = 1.0 + float(amount)
            matrix = np.asarray(
                ((scale, 0.0, (1.0 - scale) * center[0]),
                 (0.0, scale, (1.0 - scale) * center[1])),
                dtype=np.float32,
            )
            sample = cv2.warpAffine(
                color, matrix, (width, height), flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            accum += sample.astype(np.float32)
    else:
        strength = max(0.0, _float_param(params, "spin_angle", 18.0))
        strength *= pulse ** 0.72
        sign = 1.0 if is_increasing else -1.0
        values = np.linspace(0.0, sign * strength, sample_count)
        accum = np.zeros_like(color, dtype=np.float32)
        for angle in values:
            matrix = cv2.getRotationMatrix2D(center, float(angle), 1.0)
            sample = cv2.warpAffine(
                color, matrix, (width, height), flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            accum += sample.astype(np.float32)

    blurred = np.clip(accum / float(sample_count), 0.0, None)
    effect = source.copy()
    effect[..., :3] = blurred.astype(source.dtype)
    return _mix_effect(source, effect, mix)


def _apply_rgb_split(
    frame: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any],
) -> np.ndarray:
    """Offset the outer colour channels in opposite directions at the cut."""
    import cv2

    source = np.asarray(frame)
    color = source[..., :3]
    pulse = math.sin(math.pi * progress)
    amount = max(0.0, _float_param(params, "split_amount", 0.045))
    amount *= pulse ** 0.7 * float(color.shape[1])
    if amount < 0.25:
        return source
    sign = -1.0 if transition_type.endswith("_left") else 1.0
    effect = source.copy()
    width, height = int(color.shape[1]), int(color.shape[0])
    for channel, offset in ((0, sign * amount), (2, -sign * amount)):
        matrix = np.asarray(((1.0, 0.0, offset), (0.0, 1.0, 0.0)), dtype=np.float32)
        effect[..., channel] = cv2.warpAffine(
            color[..., channel], matrix, (width, height),
            flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE,
        )
    mix = max(0.0, min(1.0, _float_param(params, "split_mix", 0.96)))
    return _mix_effect(source, effect, mix * pulse ** 0.25)


def apply_transition_effect(
    frame: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Apply the source-side effect for any native stylized transition."""
    if transition_type not in TRANSITION_EFFECT_TYPES:
        return frame
    try:
        normalised_progress = max(0.0, min(1.0, float(progress)))
    except (TypeError, ValueError):
        normalised_progress = 0.0
    resolved = params or {}
    if transition_type in MOTION_BLUR_TRANSITION_TYPES:
        return _apply_directional_motion_blur(
            frame, normalised_progress, transition_type, resolved,
        )
    if transition_type.startswith(("zoom_blur_", "spin_blur_")):
        return _apply_radial_blur(
            frame, normalised_progress, transition_type, resolved,
        )
    return _apply_rgb_split(
        frame, normalised_progress, transition_type, resolved,
    )


def apply_transition_motion_blur(
    frame: np.ndarray,
    progress: float,
    transition_type: str,
    params: Mapping[str, Any] | None = None,
) -> np.ndarray:
    """Backward-compatible name for callers using the first effect batch."""
    return apply_transition_effect(frame, progress, transition_type, params)
