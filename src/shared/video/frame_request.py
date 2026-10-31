"""
Video Frame Request Module

定义视频帧请求的数据结构，用于播放调度和解码任务管理。

## 职责

- VideoFrameRequest: 帧请求的完整描述
- FrameRequestReason: 请求原因枚举
- RequestPriority: 优先级计算策略

## 设计原则

- frozen=True 确保请求不可变，防止意外修改
- generation_id 用于 seek 后丢弃旧 generation 帧
- priority 用于解码调度排序
- 完整类型注解

## 使用场景

- playback: 顺序播放请求
- seek: 用户跳转到特定帧
- scrub: 拖动时间条时的快速预览
- composite: Composite 节点请求帧
- export: 导出时的高质量帧请求
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class FrameRequestReason(Enum):
    """
    帧请求原因。

    不同原因有不同的优先级和丢弃策略：
    - playback: 顺序播放，可丢帧
    - seek: 精确跳转，需要高质量帧
    - scrub: 拖动预览，可丢帧，优先速度
    - composite: 合成节点，需要精确帧
    - export: 导出，不能丢帧，最高质量
    """

    PLAYBACK = "playback"
    SEEK = "seek"
    SCRUB = "scrub"
    COMPOSITE = "composite"
    EXPORT = "export"


# 优先级常量（数值越高优先级越高）
class RequestPriority:
    """
    请求优先级常量。

    优先级用于解码调度器排序请求：
    - EXPORT 最高，不能丢帧
    - SEEK/COMPOSITE 高，需要精确帧
    - SCRUB 中等，快速预览
    - PLAYBACK 低，可丢帧
    """

    PLAYBACK: Final[int] = 10
    SCRUB: Final[int] = 30
    SEEK: Final[int] = 50
    COMPOSITE: Final[int] = 70
    EXPORT: Final[int] = 100

    # 默认优先级映射
    DEFAULT_PRIORITY_MAP: Final[dict[FrameRequestReason, int]] = {
        FrameRequestReason.PLAYBACK: PLAYBACK,
        FrameRequestReason.SCRUB: SCRUB,
        FrameRequestReason.SEEK: SEEK,
        FrameRequestReason.COMPOSITE: COMPOSITE,
        FrameRequestReason.EXPORT: EXPORT,
    }

    @staticmethod
    def get_default(reason: FrameRequestReason) -> int:
        """
        获取默认优先级。

        Args:
            reason: 请求原因

        Returns:
            默认优先级值
        """
        return RequestPriority.DEFAULT_PRIORITY_MAP.get(reason, RequestPriority.PLAYBACK)


@dataclass(frozen=True)
class VideoFrameRequest:
    """
    视频帧请求。

    frozen=True 确保请求不可变，防止调度过程中意外修改。
    generation_id 用于 seek 后丢弃旧 generation 帧。

    Attributes:
        frame_index: 目标帧号（从 0 开始）
        is_master: 是否请求高质量 master 帧（False = proxy）
        generation_id: 生成版本号，seek 后递增
        priority: 优先级（数值越高越优先）
        reason: 请求原因（playback, seek, scrub, composite, export）

    Examples:
        >>> # 播放请求 proxy 帧
        >>> req = VideoFrameRequest(
        ...     frame_index=100,
        ...     is_master=False,
        ...     generation_id=1,
        ...     priority=RequestPriority.PLAYBACK,
        ...     reason=FrameRequestReason.PLAYBACK,
        ... )
        >>> # seek 请求 master 帧
        >>> seek_req = VideoFrameRequest(
        ...     frame_index=50,
        ...     is_master=True,
        ...     generation_id=2,  # seek 后 generation 递增
        ...     priority=RequestPriority.SEEK,
        ...     reason=FrameRequestReason.SEEK,
        ... )
    """

    frame_index: int
    is_master: bool
    generation_id: int
    priority: int
    reason: FrameRequestReason

    def __post_init__(self) -> None:
        """验证参数有效性"""
        if self.frame_index < 0:
            raise ValueError(f"frame_index must be >= 0, got {self.frame_index}")
        if self.generation_id < 0:
            raise ValueError(f"generation_id must be >= 0, got {self.generation_id}")

    @classmethod
    def create_playback(
        cls,
        frame_index: int,
        generation_id: int,
        is_master: bool = False,
    ) -> VideoFrameRequest:
        """
        创建播放请求的便捷方法。

        Args:
            frame_index: 目标帧号
            generation_id: 当前 generation
            is_master: 是否请求 master 帧

        Returns:
            VideoFrameRequest 实例
        """
        return cls(
            frame_index=frame_index,
            is_master=is_master,
            generation_id=generation_id,
            priority=RequestPriority.PLAYBACK,
            reason=FrameRequestReason.PLAYBACK,
        )

    @classmethod
    def create_seek(
        cls,
        frame_index: int,
        generation_id: int,
        is_master: bool = True,
    ) -> VideoFrameRequest:
        """
        创建 seek 请求的便捷方法。

        Args:
            frame_index: 目标帧号
            generation_id: seek 后的新 generation
            is_master: 是否请求 master 帧（默认 True）

        Returns:
            VideoFrameRequest 实例
        """
        return cls(
            frame_index=frame_index,
            is_master=is_master,
            generation_id=generation_id,
            priority=RequestPriority.SEEK,
            reason=FrameRequestReason.SEEK,
        )

    @classmethod
    def create_scrub(
        cls,
        frame_index: int,
        generation_id: int,
        is_master: bool = False,
    ) -> VideoFrameRequest:
        """
        创建 scrub（拖动）请求的便捷方法。

        Args:
            frame_index: 目标帧号
            generation_id: 当前 generation
            is_master: 是否请求 master 帧

        Returns:
            VideoFrameRequest 实例
        """
        return cls(
            frame_index=frame_index,
            is_master=is_master,
            generation_id=generation_id,
            priority=RequestPriority.SCRUB,
            reason=FrameRequestReason.SCRUB,
        )

    @classmethod
    def create_export(
        cls,
        frame_index: int,
        generation_id: int,
    ) -> VideoFrameRequest:
        """
        创建导出请求的便捷方法。

        Args:
            frame_index: 目标帧号
            generation_id: 当前 generation

        Returns:
            VideoFrameRequest 实例（最高优先级，master 帧）
        """
        return cls(
            frame_index=frame_index,
            is_master=True,
            generation_id=generation_id,
            priority=RequestPriority.EXPORT,
            reason=FrameRequestReason.EXPORT,
        )

    @property
    def can_drop(self) -> bool:
        """
        是否可丢弃该请求的帧。

        Export 和 Composite 不能丢帧。
        playback/scrub 可以丢帧。

        Returns:
            可丢弃返回 True
        """
        return self.reason in (
            FrameRequestReason.PLAYBACK,
            FrameRequestReason.SCRUB,
        )

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"VideoFrameRequest(frame={self.frame_index}, "
            f"master={self.is_master}, "
            f"gen={self.generation_id}, "
            f"pri={self.priority}, "
            f"reason={self.reason.value})"
        )