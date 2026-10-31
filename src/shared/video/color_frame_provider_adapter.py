"""
Color Frame Provider Adapter Module
将 VideoFrameProviderService 适配为 Color 播放所需的接口。

## 职责（S4.A）

- ColorFrameProviderAdapter: 适配 VideoFrameProviderService 为 Color 播放接口
- 共享帧服务：同一视频在 Color/Composite 同时打开时共享 source/cache

## 设计原则

- Color 通过 adapter 使用全局 VideoFrameProviderService
- Composite 也使用 VideoFrameProviderService
- 避免同一视频重复打开 source 和 cache
- 接口与 VideoSession 的帧获取方法兼容
- 完整类型注解

## 使用示例

```python
from src.shared.video.color_frame_provider_adapter import ColorFrameProviderAdapter

# 创建 adapter
adapter = ColorFrameProviderAdapter()

# 打开视频
adapter.open('/path/to/video.mp4')

# 请求 proxy 帧（实时播放）
frame = adapter.request_proxy(0)

# 请求 master 帧（暂停、逐帧）
master_frame = adapter.request_master(100, timeout_ms=300)

# 预取帧范围
adapter.prefetch_proxy(0, 10)

# 获取元数据
metadata = adapter.metadata

# 关闭
adapter.close()
```

## 与 VideoSession 的关系

ColorFrameProviderAdapter 替代 VideoSession 的部分帧获取功能：
- VideoSession 继续管理播放状态、时钟、工作线程
- ColorFrameProviderAdapter 专门处理帧请求
- 可以通过 feature flag 切换使用 adapter 或 SourceManager

## frame_index 语义（S4.B）

所有 request 输入都是源视频 frame_index，0-based：
- request_proxy(frame_index): 源视频帧号
- request_master(frame_index): 源视频帧号
- 不做 timeline offset/speed 转换
- Color 直接请求源帧，不走 timeline 转换
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame
    from ..media.gpu_frame import GpuDecodedFrame
    from ..media.metadata import VideoMetadata
    from .frame_provider_service import (
        VideoFrameProviderService,
        ClipInfo,
    )

logger = logging.getLogger(__name__)


# =============================================================================
# ColorFrameProviderAdapter: Color 帧服务适配器
# =============================================================================

class ColorFrameProviderAdapter:
    """
    Color 帧服务适配器。

    实现 S4.A 的设计：
    - 将 VideoFrameProviderService 包成 Color 播放所需接口
    - 共享帧服务：同一视频在 Color/Composite 同时打开时共享 source/cache
    - 接口与 VideoSession 的帧获取方法兼容

    Attributes:
        _service: VideoFrameProviderService 实例（全局）
        _clip_id: 当前打开的剪辑 ID
        _clip_info: 剪辑信息
        _is_open: 是否已打开
        _path: 当前视频路径

    Note:
        frame_index 语义（S4.B）：
        - 所有 request 输入都是源视频 frame_index，0-based
        - 不做 timeline offset/speed 转换
        - Color 直接请求源帧
    """

    def __init__(
        self,
        service: Optional[VideoFrameProviderService] = None,
    ) -> None:
        """
        初始化适配器。

        Args:
            service: VideoFrameProviderService 实例（可选，默认使用全局）
        """
        if service is None:
            from .frame_provider_service import get_frame_provider_service
            service = get_frame_provider_service()

        self._service: VideoFrameProviderService = service
        self._clip_id: Optional[str] = None
        self._clip_info: Optional[ClipInfo] = None
        self._is_open: bool = False
        self._path: Optional[str] = None

        # 统计
        self._total_proxy_requests: int = 0
        self._total_master_requests: int = 0

        logger.debug("ColorFrameProviderAdapter initialized")

    # =============================================================================
    # 属性
    # =============================================================================

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._is_open

    @property
    def path(self) -> Optional[str]:
        """当前视频路径"""
        return self._path

    @property
    def clip_id(self) -> Optional[str]:
        """剪辑 ID"""
        return self._clip_id

    @property
    def metadata(self) -> Optional[VideoMetadata]:
        """
        视频元数据。

        Returns:
            VideoMetadata 或 None

        Note:
            返回的是从 ClipInfo 转换的 VideoMetadata-like 对象
        """
        if self._clip_info is None:
            return None

        # 创建 VideoMetadata-like 对象（简化版）
        return _ClipInfoMetadataAdapter(self._clip_info)

    @property
    def clip_info(self) -> Optional[ClipInfo]:
        """剪辑信息"""
        return self._clip_info

    @property
    def fps(self) -> Optional[float]:
        """帧率"""
        if self._clip_info is not None:
            return self._clip_info.fps
        return None

    @property
    def total_frames(self) -> Optional[int]:
        """总帧数"""
        if self._clip_info is not None:
            return self._clip_info.total_frames
        return None

    @property
    def duration(self) -> Optional[float]:
        """时长（秒）"""
        if self._clip_info is not None:
            return self._clip_info.duration
        return None

    @property
    def width(self) -> Optional[int]:
        """宽度"""
        if self._clip_info is not None:
            return self._clip_info.width
        return None

    @property
    def height(self) -> Optional[int]:
        """高度"""
        if self._clip_info is not None:
            return self._clip_info.height
        return None

    # =============================================================================
    # 打开/关闭
    # =============================================================================

    def open(self, path: str) -> bool:
        """
        打开视频文件。

        Args:
            path: 视频文件路径

        Returns:
            True 表示成功打开

        Note:
            使用全局 VideoFrameProviderService.open_clip()
            如果 Composite 已打开同一文件，共享 clip_id
        """
        if self._is_open:
            logger.warning(f"Already open: {self._path}")
            return False

        try:
            # 使用全局服务打开剪辑
            clip_id = self._service.open_clip(path)

            self._clip_id = clip_id
            self._path = path
            self._clip_info = self._service.get_clip_info(clip_id)

            if self._clip_info is None:
                logger.error(f"Failed to get clip info: {path}")
                self._service.close_clip(clip_id)
                return False

            self._is_open = True

            logger.info(
                f"ColorFrameProviderAdapter opened: {path} -> {clip_id}, "
                f"{self._clip_info.width}x{self._clip_info.height}, "
                f"{self._clip_info.fps:.2f}fps, {self._clip_info.total_frames} frames"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to open video: {e}")
            return False

    def close(self) -> None:
        """
        关闭视频。

        减少 VideoFrameProviderService 的引用计数。
        如果引用计数为 0，释放资源。

        Note:
            多个消费者共享同一剪辑时，只有最后一个关闭才释放资源
        """
        if not self._is_open:
            return

        if self._clip_id is not None:
            self._service.close_clip(self._clip_id)
            logger.info(f"ColorFrameProviderAdapter closed: {self._clip_id}")

        self._clip_id = None
        self._clip_info = None
        self._is_open = False
        self._path = None

        self._total_proxy_requests = 0
        self._total_master_requests = 0

    # =============================================================================
    # 帧请求
    # =============================================================================

    def request_proxy(self, frame_index: int) -> Optional["GpuDecodedFrame"]:
        """
        请求 proxy 帧（低分辨率，用于实时播放）。

        优先使用 GPU 帧（VT zero-copy），fallback 到 CPU DecodedFrame。

        Args:
            frame_index: 帧号（0-based，源视频帧号）

        Returns:
            GpuDecodedFrame, DecodedFrame 或 None

        Note:
            S4.B: frame_index 是源视频帧号，不做 timeline 转换
            Batch G: 优先 request_gpu_frame，充分利用硬件解码
        """
        if not self._is_open or self._clip_id is None:
            return None

        self._total_proxy_requests += 1

        # Try GPU frame first (VT zero-copy / hardware decode)
        frame = self._service.request_gpu_frame(
            clip_id=self._clip_id,
            frame_index=frame_index,
            quality='proxy'
        )

        if frame is None:
            # Fallback to CPU frame
            frame = self._service.request_frame(
                clip_id=self._clip_id,
                frame_index=frame_index,
                quality='proxy'
            )

        if frame is not None:
            logger.debug(
                f"ColorFrameProviderAdapter.request_proxy({frame_index}): "
                f"got frame (size={getattr(frame, 'width', 0)}x{getattr(frame, 'height', 0)}, "
                f"type={type(frame).__name__})"
            )
        else:
            logger.warning(
                f"ColorFrameProviderAdapter.request_proxy({frame_index}): "
                f"failed to get frame"
            )

        return frame

    def request_master(
        self,
        frame_index: int,
        timeout_ms: int = 300
    ) -> Optional[DecodedFrame]:
        """
        请求 master 帧（高质量，用于暂停、逐帧、导出）。

        Args:
            frame_index: 帧号（0-based，源视频帧号）
            timeout_ms: 等待超时（毫秒，默认 300ms）

        Returns:
            DecodedFrame 或 None

        Note:
            S4.B: frame_index 是源视频帧号，不做 timeline 转换
            使用 VideoFrameProviderService.request_frame(quality='master')

            第一版不实现 timeout 等待逻辑，直接请求 master 帧
            后续版本可以添加从 master queue 等待的逻辑
        """
        if not self._is_open or self._clip_id is None:
            return None

        self._total_master_requests += 1

        # GPU-first：会话附带了 VT 零拷贝源时优先取硬件解码的精确帧
        # （延迟 GL 导入，由渲染线程绑定外部纹理）。VT 不可用/解码失败/
        # 越界时 request_gpu_frame 内部自动回退 CPU master。
        frame = self._service.request_gpu_frame(
            clip_id=self._clip_id,
            frame_index=frame_index,
            quality='master'
        )

        if frame is not None:
            logger.debug(
                f"ColorFrameProviderAdapter.request_master({frame_index}): "
                f"got frame (size={frame.width}x{frame.height})"
            )
        else:
            logger.warning(
                f"ColorFrameProviderAdapter.request_master({frame_index}): "
                f"failed to get frame"
            )

        return frame

    def prefetch_proxy(self, start: int, end: int) -> None:
        """
        预取 proxy 帧范围。

        Args:
            start: 开始帧号（0-based）
            end: 结束帧号

        Note:
            S4.B: frame_index 是源视频帧号，不做 timeline 转换
        """
        if not self._is_open or self._clip_id is None:
            return

        self._service.prefetch(
            clip_id=self._clip_id,
            start=start,
            end=end,
            quality='proxy'
        )

        logger.debug(
            f"ColorFrameProviderAdapter.prefetch_proxy({start}-{end})"
        )

    def prefetch_master(self, start: int, end: int) -> None:
        """
        预取 master 帧范围。

        Args:
            start: 开始帧号（0-based）
            end: 结束帧号

        Note:
            S4.B: frame_index 是源视频帧号，不做 timeline 转换
        """
        if not self._is_open or self._clip_id is None:
            return

        self._service.prefetch(
            clip_id=self._clip_id,
            start=start,
            end=end,
            quality='master'
        )

        logger.debug(
            f"ColorFrameProviderAdapter.prefetch_master({start}-{end})"
        )

    # =============================================================================
    # 统计
    # =============================================================================

    def get_stats(self) -> dict[str, int | float | str]:
        """
        获取统计信息。

        Returns:
            统计信息字典
        """
        service_stats = self._service.get_stats() if self._clip_id else {}

        return {
            'is_open': self._is_open,
            'path': self._path or '',
            'clip_id': self._clip_id or '',
            'total_proxy_requests': self._total_proxy_requests,
            'total_master_requests': self._total_master_requests,
            'service_stats': service_stats,
        }

    # =============================================================================
    # FrameProviderProtocol 协议方法
    # =============================================================================

    def get_proxy_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取代理帧（同步，用于实时播放）。

        实现 FrameProviderProtocol 协议。
        与 request_proxy 方法相同，提供协议兼容接口。

        Args:
            frame_index: 帧号（0-based，源视频帧号）

        Returns:
            DecodedFrame 或 None
        """
        return self.request_proxy(frame_index)

    def get_master_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取高质量帧（同步，用于暂停、逐帧、导出）。

        实现 FrameProviderProtocol 协议。
        与 request_master 方法相同，提供协议兼容接口。

        Args:
            frame_index: 帧号（0-based，源视频帧号）

        Returns:
            DecodedFrame 或 None
        """
        return self.request_master(frame_index)

    def request_proxy_frame(self, frame_index: int) -> None:
        """
        异步请求代理帧（触发预读）。

        实现 FrameProviderProtocol 协议。

        Args:
            frame_index: 帧号（0-based）

        Note:
            使用 VideoFrameProviderService.prefetch 触发预读。
            预读范围包括目标帧及其相邻帧。
        """
        if not self._is_open or self._clip_id is None:
            return

        # 预读目标帧和后续几帧
        end_frame = frame_index + 3  # 预读 3 帧
        total = self._clip_info.total_frames if self._clip_info else 0
        if total > 0:
            end_frame = min(end_frame, total - 1)

        self._service.prefetch(
            clip_id=self._clip_id,
            start=frame_index,
            end=end_frame,
            quality='proxy'
        )

        logger.debug(
            f"ColorFrameProviderAdapter.request_proxy_frame({frame_index}): "
            f"prefetch triggered"
        )

    def request_master_frame(self, frame_index: int) -> None:
        """
        异步请求高质量帧（触发预读）。

        实现 FrameProviderProtocol 协议。

        Args:
            frame_index: 帧号（0-based）

        Note:
            使用 VideoFrameProviderService.prefetch 触发预读。
            Master 预读范围较小（只预读目标帧）。
        """
        if not self._is_open or self._clip_id is None:
            return

        # Master 预读只预读目标帧（不预读相邻帧）
        self._service.prefetch(
            clip_id=self._clip_id,
            start=frame_index,
            end=frame_index,
            quality='master'
        )

        logger.debug(
            f"ColorFrameProviderAdapter.request_master_frame({frame_index}): "
            f"prefetch triggered"
        )


# =============================================================================
# ClipInfo 到 VideoMetadata 的适配器
# =============================================================================

class _ClipInfoMetadataAdapter:
    """
    将 ClipInfo 适配为 VideoMetadata-like 对象。

    用于与现有代码兼容（某些地方期望 VideoMetadata 对象）。
    """

    def __init__(self, clip_info: ClipInfo) -> None:
        """初始化适配器"""
        self._clip_info = clip_info

    @property
    def width(self) -> int:
        """宽度"""
        return self._clip_info.width

    @property
    def height(self) -> int:
        """高度"""
        return self._clip_info.height

    @property
    def display_size(self) -> tuple[int, int]:
        """显示尺寸（PlaybackCoordinator.get_video_info 解包此属性）"""
        return (self._clip_info.width, self._clip_info.height)

    @property
    def rotation(self) -> int:
        """旋转角度（度）"""
        return int(getattr(self._clip_info, "rotation", 0) or 0)

    @property
    def codec(self) -> str:
        """编码（get_video_info 的 codec 字段）"""
        return self._clip_info.codec_name

    @property
    def pixel_format(self) -> str:
        """像素格式（get_video_info 的 pixel_format 字段）"""
        return str(getattr(self._clip_info, "pixel_format", "unknown") or "unknown")

    @property
    def fps(self) -> _RationalAdapter:
        """帧率"""
        return _RationalAdapter(self._clip_info.fps)

    @property
    def total_frames(self) -> int:
        """总帧数"""
        return self._clip_info.total_frames

    @property
    def duration(self) -> float:
        """时长（秒）"""
        return self._clip_info.duration

    @property
    def bit_depth(self) -> int:
        """位深度"""
        return self._clip_info.bit_depth

    @property
    def is_hdr(self) -> bool:
        """是否 HDR"""
        return self._clip_info.is_hdr

    @property
    def codec_name(self) -> str:
        """编码名称"""
        return self._clip_info.codec_name

    @property
    def color_primaries(self) -> str:
        """色域"""
        return self._clip_info.color_primaries

    @property
    def color_transfer(self) -> str:
        """传输函数"""
        return self._clip_info.color_transfer

    @property
    def color_matrix(self) -> str:
        """矩阵"""
        return self._clip_info.color_matrix


class _RationalAdapter:
    """
    将 float fps 适配为 Rational-like 对象。
    """

    def __init__(self, fps: float) -> None:
        """初始化适配器"""
        self._fps = fps

    def to_fps(self) -> float:
        """转换为帧率"""
        return self._fps

    def to_float(self) -> float:
        """转换为浮点数"""
        return self._fps