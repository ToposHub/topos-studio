"""Spatial Midtone Detail fit for Rec.709 primary calibration v2."""

from __future__ import annotations

import numpy as np


# Bilinear samples fitted to the measured 1080p spatial response. Each point
# contributes at four quarter turns. Scale offsets with the source width.
_TAPS = (
    (1.428, 14.407, 0.062844),
    (3.533, 17.469, 0.047035),
    (5.552, 21.471, 0.042703),
    (7.579, 25.447, 0.038805),
    (9.592, 30.507, 0.033888),
    (11.500, 41.403, 0.024725),
)
_LUMA = np.array((0.2126, 0.7152, 0.0722), dtype=np.float32)


def detail_shader_taps(vector_type: str) -> str:
    return ", ".join(f"{vector_type}({x:.3f}, {y:.3f}, {weight:.6f})" for x, y, weight in _TAPS)


DETAIL_SHADER_GAIN = """
    if (center_luma <= 0.0) { return color; }
    float code = pow(center_luma, 1.0 / 2.4);
    float blurred = pow(max(blur_luma, 0.0), 1.0 / 2.4);
    float delta = code - blurred;
    float tone = min(code, 1.0);
    float strength = tone * (1.362 + tone * (0.107 - 0.199 * tone));
    float contrast_limit = delta / 0.264;
    float edge = 1.0 + contrast_limit * contrast_limit + max(variance, 0.0) / (0.162 * 0.162);
    float adjusted = max(0.0, code + delta * strength * (amount / 100.0) / edge);
    return color * pow(adjusted / code, 2.4);
"""


def apply_calibrated_detail_cpu(color: np.ndarray, source: np.ndarray, amount: float) -> np.ndarray:
    """Modulate luminance by local code-domain detail, protecting strong edges."""
    import cv2

    source = np.ascontiguousarray(source, dtype=np.float32)
    height, width = source.shape[:2]
    mean = np.zeros_like(source, dtype=np.float32)
    second_moment = np.zeros((height, width), dtype=np.float32)
    tmp = np.empty_like(mean)
    scale = width / 1920.0
    for x, y, weight in _TAPS:
        for dx, dy in ((x, y), (-y, x), (-x, -y), (y, -x)):
            translation = np.array(((1.0, 0.0, -dx * scale), (0.0, 1.0, -dy * scale)), dtype=np.float32)
            cv2.warpAffine(source, translation, (width, height), dst=tmp, flags=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE)
            mean += tmp * np.float32(weight)
            second_moment += ((tmp * tmp) @ _LUMA) * np.float32(weight)
    center = source @ _LUMA
    code = np.maximum(center, 0.0) ** np.float32(1.0 / 2.4)
    blurred = np.maximum(mean @ _LUMA, 0.0) ** np.float32(1.0 / 2.4)
    variance = np.maximum(second_moment - (mean * mean) @ _LUMA, 0.0)
    delta = code - blurred
    tone = np.minimum(code, 1.0)
    strength = tone * (1.362 + tone * (0.107 - 0.199 * tone))
    edges = 1.0 + (delta / 0.264) ** 2 + variance / (0.162 * 0.162)
    adjusted = np.maximum(0.0, code + delta * strength * (amount / 100.0) / edges)
    gain = np.divide(adjusted, code, out=np.ones_like(code), where=code > 0.0) ** np.float32(2.4)
    return color * gain[..., None]
