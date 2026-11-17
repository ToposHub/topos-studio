"""Shared, dependency-free frame sizing for RGB and YUV decoder paths."""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Protocol

import numpy as np


class FrameDimensions(Protocol):
    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def fit_aspect_size(frame: FrameDimensions, box_w: int, box_h: int,
                    sar: Fraction | None = None,
                    rotation_quarters: int = 0) -> tuple[int, int]:
    """[FR-20] 源帧（含 SAR）等比装入目标框：不拉伸、偶数对齐。

    显示尺寸 disp = (src_w·SAR, src_h)；scale = min(框约束)——等比放大到
    框内（解码尺寸≈显示尺寸，保预览清晰度）。``sar`` 为会话级
    CodecContext.sample_aspect_ratio（Fraction/None）。
    返回方形像素解码尺寸，letterbox 留给呈现层处理。

    [E4] ``rotation_quarters``（90° 象限，顺时针显示旋转）：90/270 时
    显示宽高互换——按旋转后的显示比例装框等价于先把框转置再拟合，
    解码后由调用方 ``rotate_rgb_quarters`` 旋转像素得到框内直立画面。
    """
    if int(rotation_quarters) % 2:
        box_w, box_h = box_h, box_w
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


def display_rotation_quarters(matrix) -> int:
    """[E4] FFmpeg DISPLAYMATRIX（9×int32 16.16 定点，行优先）→ 显示旋转
    的 90° 象限数（0-3，顺时针）。

    实测口径（ffmpeg ``-display_rotation W`` 写入 → 首帧 side data 读回）：
    W=90 → [[0,-1],[1,0]]；180 → [[-1,0],[0,-1]]；270 → [[0,1],[-1,0]]。
    公式 ``atan2(-b, a)`` 归一化到 [0,360)。**只支持 90° 倍数**：任意角、
    镜像（det<0）返回 0 并由调用方告警——不静默错误呈现。
    """
    try:
        values = [int(v) for v in list(matrix)[:9]]
    except (TypeError, ValueError):
        return 0
    if len(values) < 9:
        return 0
    a, b = values[0] / 65536.0, values[1] / 65536.0
    d, e = values[3] / 65536.0, values[4] / 65536.0
    if a * e - b * d < 0:
        return 0  # 镜像翻转：预览路径不支持，如实按未旋转处理并告警
    deg = math.degrees(math.atan2(-b, a)) % 360.0
    quarter = round(deg / 90.0) % 4
    if abs(deg - quarter * 90.0) > 2.0:
        return 0  # 任意角度：不支持（不为凑比例假转 90°）
    return quarter


def rotate_rgb_quarters(rgb: np.ndarray, quarters: int) -> np.ndarray:
    """[E4] RGB(H,W,3) 顺时针旋转 quarters×90°；0 象限原样返回。"""
    quarter = int(quarters) % 4
    if quarter == 0:
        return rgb
    return np.ascontiguousarray(np.rot90(rgb, k=-quarter))
