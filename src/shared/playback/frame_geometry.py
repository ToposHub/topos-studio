"""Shared, dependency-free frame sizing for RGB and YUV decoder paths."""
from __future__ import annotations

from fractions import Fraction
from typing import Protocol


class FrameDimensions(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def fit_aspect_size(frame: FrameDimensions, box_w: int, box_h: int,
                    sar: Fraction | None = None) -> tuple[int, int]:
    """[FR-20] 源帧（含 SAR）等比装入目标框：不拉伸、偶数对齐。

    显示尺寸 disp = (src_w·SAR, src_h)；scale = min(框约束)——等比放大到
    框内（解码尺寸≈显示尺寸，保预览清晰度）。``sar`` 为会话级
    CodecContext.sample_aspect_ratio（Fraction/None）。
    返回方形像素解码尺寸，letterbox 留给呈现层处理。
    """
    src_w, src_h = int(frame.width), int(frame.height)
    if src_w <= 0 or src_h <= 0 or box_w <= 0 or box_h <= 0:
        return max(2, box_w - box_w % 2), max(2, box_h - box_h % 2)
    try:
        sar_num = int(sar.numerator) if sar is not None else 1
        sar_den = int(sar.denominator) if sar is not None else 1
    except (AttributeError, ValueError, TypeError):
        sar_num, sar_den = 1, 1
    if sar_num <= 0 or sar_den <= 0:
        sar_num, sar_den = 1, 1
    disp_w = src_w * sar_num / sar_den
    scale = min(box_w / disp_w, box_h / src_h)
    w = max(2, int(round(disp_w * scale)))
    h = max(2, int(round(src_h * scale)))
    w -= w % 2
    h -= h % 2
    if w < 2 or h < 2:
        w = h = 2
    return w, h
