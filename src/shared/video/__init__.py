"""
Video Module
视频播放相关的核心组件。

## 模块组成

- playback_clock: SourceManager, PlaybackClock, FrameScheduler
- frame_request: VideoFrameRequest, FrameRequestReason, RequestPriority
- frame_queue: ReadyFrameQueue, QueueEntry, FrameSource
- decode_worker: ProxyDecodeWorker, MasterDecodeWorker, DecodeWorkerManager (P2.B)
- session: VideoSession, PlaybackState (P2.B + P5.A)
- frame_provider_service: VideoFrameProviderService, ClipSession, FrameQuality (P7.A)

## 使用示例

```python
from src.shared.video import SourceManager, PlaybackClock, FrameScheduler
from src.shared.video import VideoFrameRequest, ReadyFrameQueue
from src.shared.video import VideoSession, PlaybackState

# 使用 VideoSession（推荐）
session = VideoSession()
session.load('/path/to/video.mp4')
session.start_playback()

# 播放循环
while session.state == PlaybackState.PLAYING:
    frame = session.tick()
    if frame is not None:
        # 渲染 frame...

# Seek
session.seek_to_frame(100)

# 暂停（获取高质量 master 帧）
session.pause()
master_frame = session.get_current_frame(is_master=True)

# 停止
session.stop_playback()
session.unload()

# 使用低级组件
manager = SourceManager('/path/to/video.mp4')
manager.open()

clock = PlaybackClock(
    fps=manager.fps.to_float(),
    total_frames=manager.total_frames
)

scheduler = FrameScheduler(clock, manager)

# 播放时
clock.start()
while clock.is_playing:
    frame_num = clock.tick()
    frame = manager.get_proxy_frame(frame_num)
    # 渲染 frame...

# 暂停时
clock.stop()
frame = manager.get_master_frame(100)  # 高质量帧

# 使用帧请求和队列
request = VideoFrameRequest.create_playback(frame_index=50, generation_id=1)
queue = ReadyFrameQueue(max_proxy_size=12)
# 入队解码后的帧...

# 使用 VideoFrameProviderService (P7.A)
from src.shared.video import VideoFrameProviderService, FrameQuality

service = VideoFrameProviderService()
clip_id = service.open_clip('/path/to/video.mp4')

# 请求 proxy 帧（预览）
frame = service.request_frame(clip_id, 0, FrameQuality.PROXY)

# 请求 master 帧（导出）
master_frame = service.request_frame(clip_id, 0, FrameQuality.MASTER)

# 关闭剪辑
service.close_clip(clip_id)
service.shutdown()
```

## 设计原则

1. Master/Proxy 分离：高质量帧和代理帧使用不同解码路径
2. 时钟驱动：基于 wall clock 和 PTS 控制帧调度
3. 智能预读：根据播放状态调度帧预读
4. Generation 控制：seek 后 generation_id 递增，旧帧自动丢弃
5. 线程安全：队列使用 threading.Lock 保护
6. 后台解码：Worker 独立线程运行，不阻塞 UI
7. 完整类型注解
8. 共享服务：多个 VideoFrameNode 共享同一 source/cache (P7.A)
"""

from .playback_clock import (
    SourceManager,
    PlaybackClock,
    ClockState,
    FrameScheduler,
)

from .frame_request import (
    VideoFrameRequest,
    FrameRequestReason,
    RequestPriority,
)

from .frame_queue import (
    ReadyFrameQueue,
    QueueEntry,
    FrameSource,
    LatestFrameResult,
)

from .decode_worker import (
    ProxyDecodeWorker,
    MasterDecodeWorker,
    DecodeWorkerManager,
    DecodeWorkerStats,
    WorkerState,
)

from .session import (
    VideoSession,
    PlaybackState,
    SessionStats,
)

from .frame_provider_service import (
    VideoFrameProviderService,
    ClipSession,
    ClipInfo,
    FrameQuality,
    get_frame_provider_service,
    reset_frame_provider_service,
)

# S4.A: Color 帧服务适配器
from .color_frame_provider_adapter import (
    ColorFrameProviderAdapter,
)

# Public protocol for cross-feature frame access
from .frame_provider_protocol import FrameProvider

# P0-4: Fallback Reason 枚举和日志系统
from .fallback_types import (
    VideoPipelineFallbackReason,
    FallbackEvent,
    FallbackLogger,
    fallback_logger,
    log_fallback,
    get_recent_fallback_events,
    get_fallback_statistics,
    clear_fallback_events,
    has_recent_fallback,
    get_last_fallback_reason,
    extract_codec_info,
    extract_pixel_format_info,
    extract_backend_info,
    create_fallback_event_from_context,
)

# V-P1-1: Pipeline Decision 模块
from .pipeline_decision import (
    VideoPipelineMode,
    VideoPipelineEffectiveMode,
    VideoPipelineDecision,
    FallbackDecision,
    resolve_video_pipeline_mode,
    fallback_to_v2_or_fail,
    is_fatal_fallback_reason,
    should_allow_fallback_for_reason,
    is_professional_codec,
    is_high_bitdepth_format,
    should_prevent_fallback_for_codec_format,
    FATAL_FALLBACK_REASONS,
    NON_FATAL_REASONS,
)

# Playback Frame Trace (Batch A)
from .playback_frame_trace import (
    EditPlaybackFrameTrace,
    FrameTraceCollector,
    get_trace_collector,
    reset_trace_collector,
)

__all__ = [
    # playback_clock
    'SourceManager',
    'PlaybackClock',
    'ClockState',
    'FrameScheduler',
    # frame_request
    'VideoFrameRequest',
    'FrameRequestReason',
    'RequestPriority',
    # frame_queue
    'ReadyFrameQueue',
    'QueueEntry',
    'FrameSource',
    'LatestFrameResult',
    # decode_worker (P2.B)
    'ProxyDecodeWorker',
    'MasterDecodeWorker',
    'DecodeWorkerManager',
    'DecodeWorkerStats',
    'WorkerState',
    # session (P2.B + P5.A)
    'VideoSession',
    'PlaybackState',
    'SessionStats',
    # frame_provider_service (P7.A)
    'VideoFrameProviderService',
    'ClipSession',
    'ClipInfo',
    'FrameQuality',
    'get_frame_provider_service',
    'reset_frame_provider_service',
    # color_frame_provider_adapter (S4.A)
    'ColorFrameProviderAdapter',
    # frame_provider_protocol (cross-feature)
    'FrameProvider',
    # fallback_types (P0-4)
    'VideoPipelineFallbackReason',
    'FallbackEvent',
    'FallbackLogger',
    'fallback_logger',
    'log_fallback',
    'get_recent_fallback_events',
    'get_fallback_statistics',
    'clear_fallback_events',
    'has_recent_fallback',
    'get_last_fallback_reason',
    'extract_codec_info',
    'extract_pixel_format_info',
    'extract_backend_info',
    'create_fallback_event_from_context',
    # pipeline_decision (V-P1-1)
    'VideoPipelineMode',
    'VideoPipelineEffectiveMode',
    'VideoPipelineDecision',
    'FallbackDecision',
    'resolve_video_pipeline_mode',
    'fallback_to_v2_or_fail',
    'is_fatal_fallback_reason',
    'should_allow_fallback_for_reason',
    'is_professional_codec',
    'is_high_bitdepth_format',
    'should_prevent_fallback_for_codec_format',
    'FATAL_FALLBACK_REASONS',
    'NON_FATAL_REASONS',
    # playback_frame_trace (Batch A)
    'EditPlaybackFrameTrace',
    'FrameTraceCollector',
    'get_trace_collector',
    'reset_trace_collector',
]