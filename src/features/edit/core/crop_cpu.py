"""
FX-M06: CPU crop/matte implementation.

Applies edge crop with optional feather as an alpha matte on a BGR/BGRA numpy
frame. Used by the CPU preview fallback and timeline export paths.

AUD-16315: the matte geometry is NOT reimplemented here — it is delegated to
``crop_render.build_crop_mask`` (the single geometry source shared with the
GPU crop shader), so the same CropParams produces the same visible region on
every render path: boundary alpha = 1, feather ramp fading outward from the
crop rect boundary (see crop_render.CROP_FRAGMENT_SHADER).

- Crop edges are normalized fractions (0.0–0.5 per side).
- Feather is in pixels, clamped to half the smaller kept dimension.
- Output: per-pixel alpha modulated by the crop matte (straight alpha).
- For BGR (3-channel) frames, an alpha channel is added when crop is active.

Coordinate contract (matching GPU shader):
- Image space: row 0 = top, col 0 = left.
- CropParams.top crops from row 0 side.
- CropParams.bottom crops from the last row side.
"""

from __future__ import annotations

import numpy as np
from typing import Tuple

from src.features.edit.core.crop_render import CropRect, build_crop_mask


def crop_is_identity(crop_tuple: Tuple[float, ...] | None) -> bool:
    """Return True when crop has no visible effect."""
    if not crop_tuple or len(crop_tuple) < 4:
        return True
    left, right, top, bottom = (float(v) for v in crop_tuple[:4])
    return left <= 0.0 and right <= 0.0 and top <= 0.0 and bottom <= 0.0


def _crop_rect_from_tuple(
    crop_tuple: Tuple[float, ...],
    width: int,
    height: int,
) -> CropRect:
    """Build the shared crop geometry (crop_render contract) from a 5-tuple.

    AUD-16315：几何（矩形 + 羽化钳制）必须与 ``crop_render.crop_rect_for_size``
    完全一致——羽化钳到保留区短边的一半，防止超大 feather 让中心透明。
    不直接构造 ``CropParams`` 是因为本函数输入是历史 5 元组，可能不满足
    CropParams 的归一化校验；这里显式做同样的钳制。

    AUD-16316: 长度契约与 ``crop_is_identity`` 统一——4 元组（历史形态，
    缺 feather）合法且 feather 默认 0.0，与 identity 检查（len >= 4 即参与
    判定）一致；长度 < 4 在 identity 检查处已按无裁剪返回，不会到达这里。
    """
    values = [float(v) for v in crop_tuple[:5]]
    if len(values) < 5:
        values.append(0.0)
    left, right, top, bottom, feather = values
    x0 = left * width
    x1 = (1.0 - right) * width
    y0 = top * height       # row 0 = image top
    y1 = (1.0 - bottom) * height
    kept = min(x1 - x0, y1 - y0)
    feather_px = max(0.0, min(max(feather, 0.0), kept * 0.5))
    return CropRect(
        x0=x0, y0=y0, x1=x1, y1=y1,
        feather_px=feather_px, width=int(width), height=int(height),
    )


def apply_crop_cpu(
    frame: np.ndarray,
    crop_tuple: Tuple[float, ...],
) -> np.ndarray:
    """Apply edge crop with optional feather to a numpy frame.

    Args:
        frame: BGR uint8 (H, W, 3) or BGRA uint8 (H, W, 4) frame.
        crop_tuple: (left, right, top, bottom, feather) where left/right/
            top/bottom are normalized 0.0–0.5 and feather is in pixels.
            AUD-16316: a legacy 4-tuple (missing feather) is accepted and
            treated as feather=0.0 — the length contract now matches
            ``crop_is_identity`` instead of crashing at unpack.

    Returns:
        Frame with alpha channel modulated by crop matte. If the input is
        3-channel, a 4th alpha channel is added. If crop is identity,
        returns the input unchanged (no copy).

    AUD-16315：羽化 matte 一律来自 ``crop_render.build_crop_mask``（单一
    几何源）。旧的本地 smoothstep 把渐变放在矩形内侧、边界处 alpha=0，
    与 GPU 着色器/导出参考（边界 alpha=1、向矩形外渐隐）方向相反——同一
    CropParams 在 CPU 与 GPU 路径产生不同可见区域。
    """
    if crop_is_identity(crop_tuple):
        return frame

    if frame is None or frame.ndim != 3:
        return frame

    h, w = frame.shape[:2]
    if h == 0 or w == 0:
        return frame

    rect = _crop_rect_from_tuple(crop_tuple, w, h)
    matte = build_crop_mask(rect)  # (H, W) float32 in [0,1]

    # Preserve the source working depth. Timeline high-depth export uses
    # uint16 BGR buffers; creating a uint8 alpha channel here would both
    # downcast that buffer and make an opaque alpha equal only 255/65535.
    if np.issubdtype(frame.dtype, np.integer):
        max_value = float(np.iinfo(frame.dtype).max)
    else:
        max_value = 1.0

    # Ensure frame has alpha channel
    if frame.shape[2] == 3:
        alpha_ch = np.full((h, w, 1), max_value, dtype=frame.dtype)
        frame = np.concatenate([frame, alpha_ch], axis=2)
    else:
        frame = frame.copy()  # don't mutate caller's array

    # Modulate existing alpha by crop matte
    existing_alpha = frame[:, :, 3].astype(np.float32) / max_value
    new_alpha = existing_alpha * matte
    frame[:, :, 3] = np.rint(
        np.clip(new_alpha, 0.0, 1.0) * max_value
    ).astype(frame.dtype)

    return frame
