"""
Edit Realtime Playback Kernel
专业 NLE 实时播放内核包。

本包按 docs/professional_nle_playback_engine_implementation_spec.md 的目标
架构建立，将 Edit 时间线实时播放从 "同步按帧请求 + CPU 合成 + QImage" 升级为：

    播放 tick
      -> RealtimePlaybackEngine.request_present(frame, deadline)
      -> RealtimePlaybackPolicy.decide_sources(frame)
      -> TimelineDecodeScheduler.get_or_prepare_bundle(frame)
      -> ClipDecodeStream / FrameRingBuffer 返回 ready frames
      -> GpuFramePool 确保 GPU-resident frame
      -> TimelineGpuCompositor 合成
      -> ProgramViewer present

设计原则：
- 实时播放热路径禁止 av.open / 磁盘代理生成 / 同步 master fallback。
- 暂停 / 逐帧 / 导出保持帧准确，允许 master。
- 任何 miss 都必须可观测、可 trace，而不是偷偷 fallback。
"""

from __future__ import annotations

from .types import (
    PlaybackMode,
    SourceKind,
    FrameIdentity,
    FrameHandle,
    LayerFrameBundle,
    DecodeMiss,
    MissReason,
    classify_miss_reason,
    SourceDecision,
)
from .policy import (
    RealtimePlaybackPolicy,
    ProxyStateSnapshot,
    RenderCacheStateSnapshot,
    EmptyProxyState,
    EmptyRenderCacheState,
    get_default_policy,
)
from .telemetry import (
    PlaybackFrameTelemetry,
    TelemetryAggregator,
)
from .frame_ring_buffer import FrameRingBuffer
from .clip_decode_stream import ClipDecodeStream, StreamKey
from .timeline_decode_scheduler import TimelineDecodeScheduler, LayerSpec
from .decode_manager_source import DecodeManagerMediaSource
from .gpu_frame_pool import GpuFramePool
from .timeline_gpu_compositor import (
    TimelineGpuCompositor,
    TimelineGpuRenderTask,
    GpuLayerInput,
    GpuFrameHandle,
)
from .render_cache_adapter import (
    RenderCacheAdapter,
    RenderCacheEntry,
    RenderCacheKey,
    build_render_hash,
    build_effects_signature,
)
from .engine import (
    EngineState,
    RealtimePlaybackEngine,
    get_default_engine,
)
from .adaptive_quality import (
    AdaptiveQualityController,
    AdaptiveQualityEvent,
    QualityPressureLevel,
    level_from_value,
)
from .source_resolver import (
    SourceResolver,
    LayerSourceQuery,
    DiskProxyServiceAdapter,
)
from .audio_ring_buffer import AudioRingBuffer
from .avsync_controller import (
    AVSyncController,
    AVSyncAction,
    AVSyncDecision,
)

__all__ = [
    # types
    "PlaybackMode",
    "SourceKind",
    "FrameIdentity",
    "FrameHandle",
    "LayerFrameBundle",
    "DecodeMiss",
    "MissReason",
    "classify_miss_reason",
    "SourceDecision",
    # policy
    "RealtimePlaybackPolicy",
    "ProxyStateSnapshot",
    "RenderCacheStateSnapshot",
    "EmptyProxyState",
    "EmptyRenderCacheState",
    "get_default_policy",
    # telemetry
    "PlaybackFrameTelemetry",
    "TelemetryAggregator",
    # decode scheduler
    "FrameRingBuffer",
    "ClipDecodeStream",
    "StreamKey",
    "TimelineDecodeScheduler",
    "LayerSpec",
    "DecodeManagerMediaSource",
    # gpu
    "GpuFramePool",
    "TimelineGpuCompositor",
    "TimelineGpuRenderTask",
    "GpuLayerInput",
    "GpuFrameHandle",
    # render cache
    "RenderCacheAdapter",
    "RenderCacheEntry",
    "RenderCacheKey",
    "build_render_hash",
    "build_effects_signature",
    # engine
    "EngineState",
    "RealtimePlaybackEngine",
    "get_default_engine",
    # P1-10 adaptive quality
    "AdaptiveQualityController",
    "AdaptiveQualityEvent",
    "QualityPressureLevel",
    "level_from_value",
    # source resolver
    "SourceResolver",
    "LayerSourceQuery",
    "DiskProxyServiceAdapter",
    # Final-5: audio engine
    "AudioRingBuffer",
    "AVSyncController",
    "AVSyncAction",
    "AVSyncDecision",
]
