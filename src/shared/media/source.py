"""
Media Source Interface Module
定义媒体源的统一接口（Protocol）。

## 职责

- MediaSource: 媒体源接口协议
- MediaSourceError: 媒体源错误类型

## 设计原则

- 使用 Protocol 定义接口，不强制继承
- 与具体实现解耦（GStreamer/OpenCV/PyAV）
- 支持视频和音频（预留）
- 完整类型注解
"""

from __future__ import annotations

from typing import Protocol, Optional, runtime_checkable
import numpy as np

from .metadata import VideoMetadata, AudioMetadata
from .frame import DecodedFrame


class MediaSourceError(Exception):
    """
    媒体源错误基类。

    用于表示媒体源操作中的各种错误情况。
    """

    def __init__(self, message: str, source_path: Optional[str] = None) -> None:
        """
        初始化错误。

        Args:
            message: 错误消息
            source_path: 媒体源路径（可选）
        """
        super().__init__(message)
        self.source_path = source_path


class MediaOpenError(MediaSourceError):
    """媒体打开错误"""
    pass


class MediaSeekError(MediaSourceError):
    """媒体定位错误"""
    pass


class MediaDecodeError(MediaSourceError):
    """媒体解码错误"""
    pass


@runtime_checkable
class MediaSource(Protocol):
    """
    媒体源接口协议。

    定义所有媒体源（视频、音频）必须实现的基本接口。
    使用 Protocol 实现静态 duck typing，允许任何类实现此接口而不需要显式继承。

    方法说明：
        open(): 打开媒体源，初始化解码器
        close(): 关闭媒体源，释放资源
        probe(): 探测媒体元数据
        read_frame(): 读取指定帧
        seek_frame(): 定位到指定帧
        read_audio_samples(): 读取音频采样（预留）
    """

    def open(self) -> None:
        """
        打开媒体源。

        初始化解码器和相关资源。

        Raises:
            MediaOpenError: 打开失败
        """
        ...

    def close(self) -> None:
        """
        关闭媒体源。

        释放所有解码器和资源。
        """
        ...

    def probe(self) -> VideoMetadata:
        """
        探测视频元数据。

        返回视频的技术参数（分辨率、帧率、色彩信息等）。

        Returns:
            VideoMetadata 实例

        Raises:
            MediaSourceError: 探测失败
        """
        ...

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """
        读取指定帧。

        Args:
            frame_index: 帧号（从 0 开始）

        Returns:
            DecodedFrame 实例

        Raises:
            MediaDecodeError: 解码失败
            ValueError: 帧号超出范围
        """
        ...

    def seek_frame(self, frame_index: int) -> None:
        """
        定位到指定帧。

        Args:
            frame_index: 目标帧号（从 0 开始）

        Raises:
            MediaSeekError: 定位失败
            ValueError: 帧号超出范围
        """
        ...

    def read_audio_samples(
        self,
        start_pts: int,
        count: int
    ) -> Optional[np.ndarray]:
        """
        读取音频采样（预留接口）。

        Args:
            start_pts: 起始时间戳
            count: 采样数量

        Returns:
            音频采样数组 (samples, channels)，无音频返回 None
        """
        ...

    @property
    def is_open(self) -> bool:
        """
        媒体源是否已打开。

        Returns:
            已打开返回 True
        """
        ...

    @property
    def current_frame(self) -> int:
        """
        当前帧号。

        Returns:
            当前帧号（从 0 开始）
        """
        ...

    @property
    def path(self) -> str:
        """
        媒体源路径。

        Returns:
            文件路径
        """
        ...


@runtime_checkable
class VideoSource(Protocol):
    """
    视频源接口协议。

    扩展 MediaSource，添加视频特定方法。
    """

    def open(self) -> None:
        """打开视频源"""
        ...

    def close(self) -> None:
        """关闭视频源"""
        ...

    def probe(self) -> VideoMetadata:
        """探测视频元数据"""
        ...

    def read_frame(self, frame_index: int) -> DecodedFrame:
        """读取指定帧"""
        ...

    def seek_frame(self, frame_index: int) -> None:
        """定位到指定帧"""
        ...

    def get_frame_at_time(self, time_seconds: float) -> DecodedFrame:
        """
        读取指定时间点的帧。

        Args:
            time_seconds: 时间（秒）

        Returns:
            DecodedFrame 实例
        """
        ...

    def get_thumbnail(
        self,
        frame_index: int = 0,
        max_size: int = 320
    ) -> Optional[np.ndarray]:
        """
        获取缩略图。

        Args:
            frame_index: 帧号
            max_size: 最大尺寸

        Returns:
            缩略图数组
        """
        ...

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        ...

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        ...

    @property
    def path(self) -> str:
        """视频路径"""
        ...

    @property
    def total_frames(self) -> int:
        """总帧数"""
        ...

    @property
    def fps(self) -> float:
        """帧率"""
        ...

    @property
    def width(self) -> int:
        """宽度"""
        ...

    @property
    def height(self) -> int:
        """高度"""
        ...

    @property
    def duration(self) -> float:
        """时长（秒）"""
        ...


@runtime_checkable
class AudioSource(Protocol):
    """
    音频源接口协议（预留）。

    定义音频源的基本接口。
    """

    def open(self) -> None:
        """打开音频源"""
        ...

    def close(self) -> None:
        """关闭音频源"""
        ...

    def probe(self) -> AudioMetadata:
        """探测音频元数据"""
        ...

    def read_samples(
        self,
        start_sample: int,
        count: int
    ) -> Optional[np.ndarray]:
        """
        读取音频采样。

        Args:
            start_sample: 起始采样索引
            count: 采样数量

        Returns:
            音频采样数组
        """
        ...

    def seek_sample(self, sample_index: int) -> None:
        """
        定位到指定采样。

        Args:
            sample_index: 目标采样索引
        """
        ...

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        ...

    @property
    def path(self) -> str:
        """音频路径"""
        ...

    @property
    def sample_rate(self) -> int:
        """采样率"""
        ...

    @property
    def channels(self) -> int:
        """声道数"""
        ...

    @property
    def total_samples(self) -> int:
        """总采样数"""
        ...

    @property
    def duration(self) -> float:
        """时长（秒）"""
        ...