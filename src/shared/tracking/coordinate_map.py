"""
跟踪坐标系统一转换（阶段 0）。

跟踪全链路使用三类坐标：
1. 归一化蒙版坐标（0-1，分辨率无关，与 ``MaskShape`` 一致）。
2. 像素坐标（某一具体帧尺寸下）。
3. 帧序号（source frame / timeline frame / clip local frame）。

本模块只提供纯函数与不可变数据类，不依赖 Qt 与解码器。
Color 页面不直接依赖 Edit 模块（Edit 可物理移除），因此帧号映射
在此独立实现，语义与 ``TimelineClip`` 保持一致（线性速度段）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

# 区域表示：(x, y, width, height)
Region = Tuple[float, float, float, float]
Point = Tuple[float, float]


# ================================================================
# 归一化 <-> 像素
# ================================================================

def norm_point_to_pixel(x: float, y: float, width: int, height: int) -> Point:
    """归一化点 -> 像素点。"""
    return x * float(width), y * float(height)


def pixel_point_to_norm(x: float, y: float, width: int, height: int) -> Point:
    """像素点 -> 归一化点。"""
    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正")
    return x / float(width), y / float(height)


def norm_region_to_pixels(region: Region, width: int, height: int) -> Region:
    """归一化区域 -> 像素区域。"""
    x, y, w, h = region
    return x * float(width), y * float(height), w * float(width), h * float(height)


def pixel_region_to_norm(region: Region, width: int, height: int) -> Region:
    """像素区域 -> 归一化区域。"""
    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正")
    x, y, w, h = region
    return x / float(width), y / float(height), w / float(width), h / float(height)


def clamp_region_to_frame(region: Region, width: int, height: int) -> Region:
    """把像素区域裁剪到帧内（宽高至少为 1 像素）。"""
    x, y, w, h = region
    x0 = max(0.0, min(float(x), float(width)))
    y0 = max(0.0, min(float(y), float(height)))
    x1 = max(0.0, min(float(x + w), float(width)))
    y1 = max(0.0, min(float(y + h), float(height)))
    return x0, y0, max(1.0, x1 - x0), max(1.0, y1 - y0)


def region_contains(outer: Region, inner: Region, tolerance: float = 1e-6) -> bool:
    """outer 是否完全包含 inner（用于 search 必须包住 pattern 的校验）。"""
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return (
        ix >= ox - tolerance
        and iy >= oy - tolerance
        and ix + iw <= ox + ow + tolerance
        and iy + ih <= oy + oh + tolerance
    )


# ================================================================
# source / timeline / clip local 帧号映射
# ================================================================

@dataclass(frozen=True)
class ClipFrameMapper:
    """线性速度片段的帧号映射器。

    语义与 ``TimelineClip.source_to_timeline_frame`` /
    ``ClipInstance.timeline_to_source_frame`` 一致：
    相对帧差按速度换算后取整；timeline 有效区间为闭开区间
    ``[timeline_start, timeline_start + timeline_duration)``
    （``timeline_end`` 属于下一个片段，与 ``TimelineClip`` 的开区间
    上界一致）。

    Attributes:
        source_start: 片段使用的源素材起始帧。
        source_end: 片段使用的源素材结束帧（含）。
        timeline_start: 片段在时间线上的起始帧。
        speed: 播放速度（1.0 为原速）。
    """

    source_start: int
    source_end: int
    timeline_start: int
    speed: float = 1.0

    def __post_init__(self) -> None:
        if self.source_end < self.source_start:
            raise ValueError("source_end 不能小于 source_start")
        if abs(self.speed) < 1e-9:
            raise ValueError("speed 不能为 0")

    @property
    def source_duration(self) -> int:
        return self.source_end - self.source_start + 1

    @property
    def timeline_duration(self) -> int:
        return int(self.source_duration / abs(self.speed))

    def source_to_timeline_frame(self, source_frame: int) -> int:
        """source frame -> timeline frame。"""
        if not (self.source_start <= source_frame <= self.source_end):
            raise ValueError(
                f"Source frame {source_frame} outside clip range "
                f"[{self.source_start}, {self.source_end}]"
            )
        relative = source_frame - self.source_start
        return self.timeline_start + int(relative / self.speed)

    def timeline_to_source_frame(self, timeline_frame: int) -> int:
        """timeline frame -> source frame。

        有效区间为 ``[timeline_start, timeline_start + timeline_duration)``
        （开区间上界，与 ``TimelineClip.timeline_to_source_frame`` 一致）。
        放行 ``timeline_start + timeline_duration`` 会在 speed=1.0 时返回
        ``source_end + 1``（越界 source frame），因此该帧必须拒绝。
        """
        timeline_end = self.timeline_start + self.timeline_duration
        if not (self.timeline_start <= timeline_frame < timeline_end):
            raise ValueError(
                f"Timeline frame {timeline_frame} outside clip range "
                f"[{self.timeline_start}, {timeline_end})"
            )
        relative = timeline_frame - self.timeline_start
        return self.source_start + int(relative * self.speed)

    def source_to_clip_local_frame(self, source_frame: int) -> int:
        """source frame -> clip local frame（0 = 片段第一帧）。"""
        return self.source_to_timeline_frame(source_frame) - self.timeline_start

    def clip_local_to_source_frame(self, clip_local_frame: int) -> int:
        """clip local frame -> source frame（有效区间 [0, timeline_duration)）。"""
        return self.timeline_to_source_frame(self.timeline_start + clip_local_frame)
