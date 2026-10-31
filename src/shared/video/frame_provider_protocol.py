"""
Frame Provider Protocol Module
定义帧提供者协议，用于 Color、Composite、Export 共享帧源的抽象接口。

## 职责

- FrameProviderProtocol: 帧提供者协议
- 定义 SourceManager 和 ColorFrameProviderAdapter 的统一接口

## 设计原则

- DecodeWorkerManager 依赖协议，不依赖具体 SourceManager
- 同一 path 在 Color + Composite 下底层 source/session 可观测为共享
- 关闭其中一个页面不会影响另一个页面（引用计数）
- 完整类型注解

## 使用示例

```python
from typing import Protocol
from src.shared.video.frame_provider_protocol import FrameProviderProtocol

# DecodeWorkerManager 接收任意实现协议的对象
def create_worker_manager(provider: FrameProviderProtocol) -> DecodeWorkerManager:
    return DecodeWorkerManager(frame_provider=provider, ...)
```

## 协议定义

协议包含以下属性和方法：
- metadata: VideoMetadata（视频元数据）
- total_frames: int（总帧数）
- get_proxy_frame(frame_index): 获取代理帧
- get_master_frame(frame_index): 获取高质量帧
- request_proxy_frame(frame_index): 异步请求代理帧
- request_master_frame(frame_index): 异步请求高质量帧
- close(): 关闭资源

Note:
    Protocol 使用 typing.Protocol，不强制继承，只需实现方法即可。
    SourceManager 和 ColorFrameProviderAdapter 都实现此协议。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame
    from ..media.metadata import VideoMetadata


@runtime_checkable
class FrameProviderProtocol(Protocol):
    """
    帧提供者协议。

    定义 SourceManager 和 ColorFrameProviderAdapter 的统一接口。
    DecodeWorkerManager 依赖此协议，不依赖具体实现。

    Attributes（协议要求）:
        metadata: 视频元数据
        total_frames: 总帧数

    Methods（协议要求）:
        get_proxy_frame(frame_index): 获取代理帧（同步，用于播放）
        get_master_frame(frame_index): 获取高质量帧（同步，用于暂停/导出）
        request_proxy_frame(frame_index): 异步请求代理帧（触发解码）
        request_master_frame(frame_index): 异步请求高质量帧（触发解码）

    Note:
        使用 @runtime_checkable 允许 isinstance(provider, FrameProviderProtocol) 检查。
        Protocol 不强制继承，只需实现方法即可。
    """

    @property
    def metadata(self) -> Optional[VideoMetadata]:
        """
        视频元数据。

        Returns:
            VideoMetadata 或 None（未打开时）
        """
        ...

    @property
    def total_frames(self) -> Optional[int]:
        """
        总帧数。

        Returns:
            总帧数或 None（未打开时）
        """
        ...

    @property
    def is_open(self) -> bool:
        """
        是否已打开。

        Returns:
            True 表示已打开
        """
        ...

    def get_proxy_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取代理帧（同步，用于实时播放）。

        Args:
            frame_index: 帧号（0-based）

        Returns:
            DecodedFrame 或 None

        Note:
            允许丢帧、跳帧以保持实时性。
            顺序播放时优化（避免重复 seek）。
        """
        ...

    def get_master_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取高质量帧（同步，用于暂停、逐帧、导出）。

        Args:
            frame_index: 帧号（0-based）

        Returns:
            DecodedFrame 或 None

        Note:
            不允许错误帧，保持精确。
            保留高位深和 alpha。
        """
        ...

    def request_proxy_frame(self, frame_index: int) -> None:
        """
        异步请求代理帧（触发解码）。

        Args:
            frame_index: 帧号（0-based）

        Note:
            用于预读和异步解码。
            SourceManager 实现：添加到请求队列，由 worker 处理。
            ColorFrameProviderAdapter 实现：调用 prefetch。
        """
        ...

    def request_master_frame(self, frame_index: int) -> None:
        """
        异步请求高质量帧（触发解码）。

        Args:
            frame_index: 帧号（0-based）

        Note:
            用于 seek、暂停、逐帧后的高质量帧预读。
            SourceManager 实现：添加到请求队列，由 worker 处理。
            ColorFrameProviderAdapter 实现：调用 prefetch(master)。
        """
        ...

    def close(self) -> None:
        """
        关闭资源。

        Note:
            引用计数管理：
            - SourceManager: 直接关闭
            - ColorFrameProviderAdapter: 减少引用计数，为 0 时才释放
        """
        ...


class FrameProvider(Protocol):
    """Protocol for requesting frames from any feature module.

    A simplified, cross-feature protocol that defines the minimal interface
    for accessing video frames. Any feature module that needs frame data can
    depend on this protocol rather than a concrete implementation.

    This is intentionally simpler than FrameProviderProtocol, which includes
    proxy/master frame distinction and async request methods. FrameProvider
    focuses on basic frame access for cross-feature communication.
    """

    def get_frame(self, frame_index: int) -> object:
        """Get a frame at the given index.

        Args:
            frame_index: 0-based frame index.

        Returns:
            Image data (numpy array or similar).
        """
        ...

    def get_frame_count(self) -> int:
        """Return total number of available frames.

        Returns:
            Total frame count.
        """
        ...

    def is_available(self) -> bool:
        """Return True if this provider can currently provide frames.

        Returns:
            True if frames are available, False otherwise.
        """
        ...