"""Color 页面蒙版跟踪器（DaVinci 风格）— A1 迁移后的兼容层。

纯算法实现已迁移到 ``src.shared.tracking``（两页共用）：
types / coordinate_map / homography_stability / feature_detector /
track_data / frame_sampler / point_manager / corner_pin / tracker_2d /
merge / tracking_cache / shape_mask。

本包保留 Color 页专属的 Qt/会话耦合部分：
- tracking_worker / tracking_service：QThread 异步任务
- tracking_store：与 color image_engine 绑定的持久化
- tracking_evaluator：结果应用到蒙版

旧 import 路径经同名垫片模块继续可用。
"""

from .corner_pin import (
    build_corner_pin_export,
    validate_corner_pin,
    write_corner_pin_export,
)

__all__ = [
    "build_corner_pin_export",
    "validate_corner_pin",
    "write_corner_pin_export",
]
