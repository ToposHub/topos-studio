"""
Video Pipeline Fallback Types (P0-4)
视频管线 fallback 原因枚举和日志记录系统。

## 职责

- VideoPipelineFallbackReason: v2/v3 管线 fallback 原因枚举
- FallbackEvent: 每次 fallback 的完整记录
- FallbackLogger: 全局 fallback 日志记录器

## 使用示例

```python
from src.shared.video import VideoPipelineFallbackReason, FallbackEvent, log_fallback

# 记录一个 fallback 事件（log_fallback 接收字段参数并返回创建的
# FallbackEvent。AUD-39006: 此前示例先手工构造事件、再把事件对象直接
# 当位置参数传回 log_fallback，照抄会在日志序列化时抛 AttributeError）
event = log_fallback(
    reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
    codec='prores',
    pixel_format='yuv422p10le',
    backend='ffmpeg',
    source_type='master_only',
    exception=RuntimeError('PyAV context creation failed'),
    user_forced=False,
)

# 检查是否有 pending fallback
events = get_recent_fallback_events(limit=10)
for e in events:
    print(f"Fallback: {e.reason.value} - {e.codec}")
```

## 设计原则

1. 每次 fallback 必须有明确原因（不再是模糊字符串）
2. 记录完整的上下文信息（codec, pixel_format, backend 等）
3. 区分用户强制和技术自动 fallback
4. 可追溯：所有 fallback 事件可导出/统计
5. 线程安全：使用 threading.Lock 保护全局记录
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Any, List, Dict

logger = logging.getLogger(__name__)


# ================================================================
# Fallback Reason 枚举
# ================================================================

class VideoPipelineFallbackReason(Enum):
    """
    视频管线 fallback 原因枚举。

    定义所有从 v3 降级到 v2 或其他降级的明确原因。

    Values:
        NONE: 无 fallback，正常运行
        USER_FORCED_V2: 用户在 Preferences 中显式选择 v2
        V3_INIT_FAILED: v3 PlaybackCoordinator 初始化失败
        COORDINATOR_LOAD_FAILED: coordinator 加载视频失败
        SOURCE_OPEN_FAILED: SourceManager 打开失败
        CODEC_UNSUPPORTED_BY_V3: codec 在 v3 中不支持（如某些高位深格式）
        FIRST_FRAME_TIMEOUT: 首帧获取超时
        FIRST_FRAME_UNAVAILABLE: 首帧不可用（decode 或转换失败）
        FRAME_CONVERSION_FAILED: 帧格式转换失败（如 YUV444P12LE RGB 转换）
        RENDERER_UNAVAILABLE: AsyncRenderer 不可用
        PLAYBACK_START_FAILED: PlaybackCoordinator.start_playback() 失败
        SESSION_INVALID: VideoSession 状态无效
        HARDWARE_ACCEL_FAILED: 硬件解码初始化失败（但不是 codec 不支持）
        PROXY_GENERATION_FAILED: proxy 生成失败，降级到 master
        DEGRADATION_LEVEL_EXCEEDED: 自适应降级超过阈值
        QUEUE_STARVATION: 帧队列持续饥饿（连续多次无法获取帧）
        BACKEND_SWITCH_FAILED: 后端切换失败
        UNKNOWN: 未知原因（需要调查）
    """
    NONE = "none"
    USER_FORCED_V2 = "user_forced_v2"
    V3_INIT_FAILED = "v3_init_failed"
    COORDINATOR_LOAD_FAILED = "coordinator_load_failed"
    SOURCE_OPEN_FAILED = "source_open_failed"
    CODEC_UNSUPPORTED_BY_V3 = "codec_unsupported_by_v3"
    FIRST_FRAME_TIMEOUT = "first_frame_timeout"
    FIRST_FRAME_UNAVAILABLE = "first_frame_unavailable"
    FRAME_CONVERSION_FAILED = "frame_conversion_failed"
    RENDERER_UNAVAILABLE = "renderer_unavailable"
    PLAYBACK_START_FAILED = "playback_start_failed"
    SESSION_INVALID = "session_invalid"
    HARDWARE_ACCEL_FAILED = "hwaccel_failed"
    PROXY_GENERATION_FAILED = "proxy_generation_failed"
    DEGRADATION_LEVEL_EXCEEDED = "degradation_level_exceeded"
    QUEUE_STARVATION = "queue_starvation"
    BACKEND_SWITCH_FAILED = "backend_switch_failed"
    UNKNOWN = "unknown"

    def is_user_forced(self) -> bool:
        """判断是否是用户强制 fallback"""
        return self == VideoPipelineFallbackReason.USER_FORCED_V2

    def is_technical_failure(self) -> bool:
        """判断是否是技术故障导致的 fallback"""
        return self not in (
            VideoPipelineFallbackReason.NONE,
            VideoPipelineFallbackReason.USER_FORCED_V2,
        )

    def requires_investigation(self) -> bool:
        """判断是否需要进一步调查"""
        return self in (
            VideoPipelineFallbackReason.UNKNOWN,
            VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3,
            VideoPipelineFallbackReason.FIRST_FRAME_UNAVAILABLE,
            VideoPipelineFallbackReason.FRAME_CONVERSION_FAILED,
        )


# ================================================================
# Fallback Event 数据类
# ================================================================

@dataclass
class FallbackEvent:
    """
    Fallback 事件完整记录。

    每次发生 fallback 时必须创建此记录，确保所有降级都有明确的追踪信息。

    Attributes:
        reason: Fallback 原因枚举
        timestamp: 事件发生时间（ISO 8601 格式字符串）
        video_path: 视频文件路径（可选）
        codec: 编解码器名称（如 'prores', 'h264', 'hevc'）
        pixel_format: 像素格式（如 'yuv422p10le', 'yuv420p'）
        backend: 当前使用的后端（'ffmpeg', 'gstreamer'）
        source_type: 源类型（'master_only', 'disk_proxy', 'memory_proxy'）
        frame_path: 帧处理路径（'planar_yuv_gpu', 'packed_rgb_u8', 'packed_rgb_f16'）
        exception_str: 异常消息（可选）
        exception_type: 异常类型名称（可选）
        user_forced: 是否用户强制
        effective_pipeline: 最终生效的管线（'v2' 或 'v3_degraded'）
        degradation_level: 降级级别（0-无降级，1-轻微，2-中度，3-重度）
        notes: 附注信息
    """
    reason: VideoPipelineFallbackReason
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    video_path: Optional[str] = None
    codec: Optional[str] = None
    pixel_format: Optional[str] = None
    backend: Optional[str] = None
    source_type: Optional[str] = None
    frame_path: Optional[str] = None
    exception_str: Optional[str] = None
    exception_type: Optional[str] = None
    user_forced: bool = False
    effective_pipeline: str = 'v2'
    degradation_level: int = 0
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        """验证必要字段"""
        if self.user_forced and self.reason != VideoPipelineFallbackReason.USER_FORCED_V2:
            logger.warning(
                f"FallbackEvent.user_forced=True 但 reason={self.reason.value}，"
                f"建议使用 USER_FORCED_V2 原因"
            )

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典用于日志/导出"""
        return {
            'reason': self.reason.value,
            'timestamp': self.timestamp,
            'video_path': self.video_path,
            'codec': self.codec,
            'pixel_format': self.pixel_format,
            'backend': self.backend,
            'source_type': self.source_type,
            'frame_path': self.frame_path,
            'exception_str': self.exception_str,
            'exception_type': self.exception_type,
            'user_forced': self.user_forced,
            'effective_pipeline': self.effective_pipeline,
            'degradation_level': self.degradation_level,
            'notes': self.notes,
        }

    def to_log_string(self) -> str:
        """生成日志字符串"""
        parts = [
            f"reason={self.reason.value}",
            f"pipeline={self.effective_pipeline}",
        ]
        if self.video_path:
            parts.append(f"path={self.video_path}")
        if self.codec:
            parts.append(f"codec={self.codec}")
        if self.pixel_format:
            parts.append(f"pix_fmt={self.pixel_format}")
        if self.backend:
            parts.append(f"backend={self.backend}")
        if self.source_type:
            parts.append(f"source={self.source_type}")
        if self.exception_str:
            parts.append(f"exception={self.exception_str}")
        if self.user_forced:
            parts.append("user_forced=True")
        if self.notes:
            parts.append(f"notes={self.notes}")

        return "VideoPipelineFallback: " + ", ".join(parts)

    def __repr__(self) -> str:
        """字符串表示"""
        return f"FallbackEvent(reason={self.reason.value}, pipeline={self.effective_pipeline})"


# ================================================================
# Fallback Logger（全局记录器）
# ================================================================

class FallbackLogger:
    """
    全局 Fallback 事件记录器。

    提供线程安全的 fallback 事件记录和查询功能。

    Features:
    - 线程安全的事件列表
    - 最大容量限制（防止内存泄漏）
    - 按 reason 统计
    - 可导出为 JSON

    Usage:
        # 记录 fallback
        fallback_logger.log(event)

        # 查询最近的事件
        events = fallback_logger.get_recent(limit=10)

        # 统计
        stats = fallback_logger.get_statistics()
    """

    _instance: Optional['FallbackLogger'] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> 'FallbackLogger':
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """初始化"""
        if self._initialized:
            return
        self._initialized = True
        self._events: List[FallbackEvent] = []
        self._events_lock = threading.Lock()
        self._max_events: int = 100  # 最大记录数
        self._session_fallback_count: int = 0  # 当前 session 的 fallback 数

    def log(self, event: FallbackEvent) -> None:
        """
        记录 fallback 事件。

        Args:
            event: Fallback 事件
        """
        with self._events_lock:
            # 添加事件
            self._events.append(event)
            self._session_fallback_count += 1

            # 超过容量时移除旧事件
            if len(self._events) > self._max_events:
                removed_count = len(self._events) - self._max_events
                self._events = self._events[-self._max_events:]
                logger.debug(f"Removed {removed_count} old fallback events")

        # 写入日志
        if event.reason != VideoPipelineFallbackReason.NONE:
            if event.user_forced:
                logger.info(event.to_log_string())
            elif event.reason.requires_investigation():
                logger.warning(event.to_log_string())
            else:
                logger.info(event.to_log_string())

    def get_recent(self, limit: int = 10) -> List[FallbackEvent]:
        """
        获取最近的事件。

        Args:
            limit: 最大数量

        Returns:
            事件列表（按时间正序：最旧在前、最新在末尾；
            tests/video/test_video_pipeline_fallback_types.py 锁定该顺序。
            只取最新一条用 limit=1 后取 events[0]——has_recent_fallback /
            get_last_fallback_reason 即此用法）
        """
        with self._events_lock:
            return list(self._events[-limit:])

    def get_all(self) -> List[FallbackEvent]:
        """获取所有事件"""
        with self._events_lock:
            return list(self._events)

    def get_statistics(self) -> Dict[str, Any]:
        """
        获取统计信息。

        Returns:
            统计字典
        """
        with self._events_lock:
            # 按 reason 统计
            reason_counts: Dict[str, int] = {}
            for event in self._events:
                reason_counts[event.reason.value] = \
                    reason_counts.get(event.reason.value, 0) + 1

            # 按 codec 统计
            codec_counts: Dict[str, int] = {}
            for event in self._events:
                if event.codec:
                    codec_counts[event.codec] = \
                        codec_counts.get(event.codec, 0) + 1

            return {
                'total_events': len(self._events),
                'session_fallback_count': self._session_fallback_count,
                'reason_counts': reason_counts,
                'codec_counts': codec_counts,
                'user_forced_count': sum(1 for e in self._events if e.user_forced),
                'technical_failure_count': sum(
                    1 for e in self._events if e.reason.is_technical_failure()
                ),
            }

    def clear(self) -> None:
        """清除所有记录"""
        with self._events_lock:
            self._events.clear()
            self._session_fallback_count = 0
        logger.info("FallbackLogger cleared")

    def export_to_json(self) -> str:
        """导出为 JSON 字符串"""
        import json
        # 锁内只拷贝事件快照，锁外再组装/序列化：get_statistics() 自身会
        # 加 _events_lock（threading.Lock 非重入），持锁调用会确定性死锁。
        with self._events_lock:
            events_snapshot: List[FallbackEvent] = list(self._events)
        data = {
            'events': [e.to_dict() for e in events_snapshot],
            'statistics': self.get_statistics(),
        }
        return json.dumps(data, indent=2)


# 全局单例
fallback_logger: FallbackLogger = FallbackLogger()


# ================================================================
# 便捷函数
# ================================================================

def log_fallback(
    reason: VideoPipelineFallbackReason,
    video_path: Optional[str] = None,
    codec: Optional[str] = None,
    pixel_format: Optional[str] = None,
    backend: Optional[str] = None,
    source_type: Optional[str] = None,
    frame_path: Optional[str] = None,
    exception: Optional[Exception] = None,
    user_forced: bool = False,
    effective_pipeline: str = 'v2',
    degradation_level: int = 0,
    notes: Optional[str] = None,
) -> FallbackEvent:
    """
    记录 fallback 事件的便捷函数。

    Args:
        reason: Fallback 原因
        video_path: 视频路径
        codec: 编解码器
        pixel_format: 像素格式
        backend: 后端
        source_type: 源类型
        frame_path: 帧路径
        exception: 异常对象
        user_forced: 用户强制
        effective_pipeline: 生效管线
        degradation_level: 降级级别
        notes: 附注

    Returns:
        创建的 FallbackEvent
    """
    exception_str: Optional[str] = None
    exception_type: Optional[str] = None
    if exception is not None:
        exception_str = str(exception)
        exception_type = type(exception).__name__

    event = FallbackEvent(
        reason=reason,
        video_path=video_path,
        codec=codec,
        pixel_format=pixel_format,
        backend=backend,
        source_type=source_type,
        frame_path=frame_path,
        exception_str=exception_str,
        exception_type=exception_type,
        user_forced=user_forced,
        effective_pipeline=effective_pipeline,
        degradation_level=degradation_level,
        notes=notes,
    )

    fallback_logger.log(event)
    return event


def get_recent_fallback_events(limit: int = 10) -> List[FallbackEvent]:
    """获取最近的 fallback 事件"""
    return fallback_logger.get_recent(limit)


def get_fallback_statistics() -> Dict[str, Any]:
    """获取 fallback 统计信息"""
    return fallback_logger.get_statistics()


def clear_fallback_events() -> None:
    """清除 fallback 事件记录"""
    fallback_logger.clear()


def has_recent_fallback() -> bool:
    """检查是否有最近的 fallback（非 NONE）"""
    events = fallback_logger.get_recent(limit=1)
    if events:
        return events[0].reason != VideoPipelineFallbackReason.NONE
    return False


def get_last_fallback_reason() -> Optional[VideoPipelineFallbackReason]:
    """获取最后一次 fallback 的原因"""
    events = fallback_logger.get_recent(limit=1)
    if events:
        return events[0].reason
    return None


# ================================================================
# 上下文信息提取辅助函数
# ================================================================

def extract_codec_info(source: Any) -> Optional[str]:
    """
    从 source 对象提取 codec 信息。

    Args:
        source: MediaSource 或 VideoDecoder 对象

    Returns:
        codec 名称字符串
    """
    if source is None:
        return None

    # 尝试从 metadata 提取
    if hasattr(source, 'metadata') and source.metadata:
        return getattr(source.metadata, 'codec_name', None)

    # 尝试从 codec_name 属性提取
    if hasattr(source, 'codec_name'):
        return source.codec_name

    # 尝试从 _codec 属性提取
    if hasattr(source, '_codec'):
        return source._codec

    return None


def extract_pixel_format_info(source: Any) -> Optional[str]:
    """
    从 source 对象提取 pixel format 信息。

    Args:
        source: MediaSource 或 VideoDecoder 对象

    Returns:
        pixel format 字符串
    """
    if source is None:
        return None

    # 尝试从 metadata 提取
    if hasattr(source, 'metadata') and source.metadata:
        return getattr(source.metadata, 'pixel_format', None)

    # 尝试从 pixel_format 属性提取
    if hasattr(source, 'pixel_format'):
        return source.pixel_format

    return None


def extract_backend_info(source: Any) -> Optional[str]:
    """
    从 source 对象提取 backend 信息。

    Args:
        source: MediaSource 或 VideoDecoder 对象

    Returns:
        backend 名称字符串
    """
    if source is None:
        return None

    # 检查类型名称
    type_name = type(source).__name__
    if 'FFmpeg' in type_name or 'PyAV' in type_name:
        return 'ffmpeg'
    if 'GStreamer' in type_name:
        return 'gstreamer'

    # 尝试从 backend 属性提取
    if hasattr(source, 'backend'):
        return str(source.backend)

    return None


def create_fallback_event_from_context(
    reason: VideoPipelineFallbackReason,
    video_path: Optional[str] = None,
    source: Optional[Any] = None,
    coordinator: Optional[Any] = None,
    exception: Optional[Exception] = None,
    user_forced: bool = False,
    effective_pipeline: str = 'v2',
    notes: Optional[str] = None,
) -> FallbackEvent:
    """
    从上下文对象创建 FallbackEvent。

    Args:
        reason: Fallback 原因
        video_path: 视频路径
        source: MediaSource 对象
        coordinator: PlaybackCoordinator 对象
        exception: 异常对象
        user_forced: 用户强制
        effective_pipeline: 生效管线
        notes: 附注

    Returns:
        FallbackEvent
    """
    codec = None
    pixel_format = None
    backend = None
    source_type = None
    frame_path = None
    degradation_level = 0

    # 从 source 提取信息
    if source:
        codec = extract_codec_info(source)
        pixel_format = extract_pixel_format_info(source)
        backend = extract_backend_info(source)

    # 从 coordinator 提取信息
    if coordinator:
        # 尝试获取 session
        session = getattr(coordinator, 'session', None)
        if session:
            source_manager = getattr(session, 'source_manager', None)
            if source_manager:
                codec = codec or extract_codec_info(source_manager)
                pixel_format = pixel_format or extract_pixel_format_info(source_manager)

            # 获取 degradation level
            if hasattr(session, '_degradation_level'):
                degradation_level = session._degradation_level

        # 尝试获取 source_type
        if hasattr(coordinator, '_source_type'):
            source_type = coordinator._source_type

    return log_fallback(
        reason=reason,
        video_path=video_path,
        codec=codec,
        pixel_format=pixel_format,
        backend=backend,
        source_type=source_type,
        frame_path=frame_path,
        exception=exception,
        user_forced=user_forced,
        effective_pipeline=effective_pipeline,
        degradation_level=degradation_level,
        notes=notes,
    )
