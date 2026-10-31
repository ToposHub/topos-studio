# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Unified GPU frame handle for zero-copy / GPU-resident frame transport.

Used by Composite, Edit, and Color features to pass frame data
through the GPU pipeline without unnecessary CPU round-trips.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class TimelineFrameGPUHandle:
    """Handle for a GPU-resident frame that can be passed between subsystems."""

    texture_id: int
    width: int
    height: int
    color_space: str = "srgb"
    bit_depth: int = 8
    origin: str = "gl_bottom_left"
    zero_copy: bool = False
    decode_path: str = "unknown"
    owner: str = "unknown"
    release_callback: Optional[Callable] = None
