"""视频时域 + 空域降噪共享模块。

纯 CPU 侧的参数与帧窗口类型；GPU 实现见
src/features/color/core/video_denoise_*（依赖渲染线程 ModernGL 上下文）。
"""

from .params import (
    MOTION_QUALITIES,
    QUALITY_MODES,
    SPATIAL_MODES,
    SpatialDenoiseParams,
    TemporalDenoiseParams,
    VideoDenoiseParams,
)
from .frame_types import FrameWindow, TemporalFrameWindowCache

__all__ = [
    "MOTION_QUALITIES",
    "QUALITY_MODES",
    "SPATIAL_MODES",
    "SpatialDenoiseParams",
    "TemporalDenoiseParams",
    "VideoDenoiseParams",
    "FrameWindow",
    "TemporalFrameWindowCache",
]
