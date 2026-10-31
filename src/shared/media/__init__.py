"""
Media Core Module
统一媒体数据模型，为视频/音频处理提供类型安全的数据结构。

## 模块组成

- metadata: Rational, VideoMetadata, AudioMetadata
- timecode: Timecode, TimecodeType（P9.1）
- timeline: TimelineClip, TimelineTrack, Timeline（时间线数据模型）
- frame: DecodedFrame, PixelFormatInfo, PlaneInfo
- pixel_format: Subsampling, ColorModel, PixelFormatRegistry
- decode_policy: DecodeTarget, DecodePolicy, DecodePolicyManager
- source: MediaSource Protocol, VideoSource, AudioSource
- factory: MediaSourceFactory（后端选择器）
- adapter: VideoDecoderAdapter（兼容旧接口）
- probe: FFmpeg 环境检测
- ffmpeg_source: FFmpegMediaSource（PyAV 后端）
- topos_source: ToposMediaSource（Topos 自研解码后端，阶段 6；auto 按内容探测路由）
- hwaccel: HardwareDecoder, HardwareCapability, FFmpegHwaccelSource（P10.1/P10.2 硬件解码）
- seek: 帧定位辅助函数

## 使用示例

```python
from src.shared.media import Rational, VideoMetadata, DecodedFrame, Timecode, TimecodeType
from src.shared.media import MediaSourceFactory, VideoDecoderAdapter
from src.shared.media import FFmpegMediaSource, frame_to_pts, pts_to_frame

# 创建帧率
fps = Rational(24000, 1001)  # 23.976fps

# 创建视频元数据
metadata = VideoMetadata(
    width=1920,
    height=1080,
    fps=fps,
    total_frames=2400,
    codec_name='h264',
    codec_profile='High',
    pixel_format='yuv420p',
    bit_depth=8,
    color_primaries='bt709',
    color_transfer='bt709',
    color_matrix='bt709',
)

# 时间转换
pts = fps.frame_to_pts(100)  # 第 100 帧的 PTS
seconds = fps.frame_to_seconds(100)  # 第 100 帧的秒数

# 时间码（P9.1）
tc = Timecode.from_string('01:00:00;00', fps)  # 丢帧时间码
frame = tc.to_frame_number()  # 转换为帧号

# 使用 Factory 创建媒体源（自动选择后端）
source = MediaSourceFactory.create('/path/to/video.mp4')
source.open()
frame = source.read_frame(0)

# 使用 FFmpegMediaSource 直接（精确 seek）
with FFmpegMediaSource('/path/to/video.mp4') as src:
    src.seek_frame(100)  # 精确定位到第 100 帧
    frame = src.read_next_frame()

# 使用 Adapter（兼容旧 VideoDecoder 接口）
adapter = VideoDecoderAdapter('/path/to/video.mp4')
frame_bgr8 = adapter.get_frame(0)  # 返回 BGR uint8 数组
```

## 设计原则

1. 不可变数据类：使用 frozen=True 确保线程安全
2. 完整类型注解：所有方法都有类型提示
3. 解耦外部库：不依赖 GStreamer/OpenCV/PyAV 类型
4. Protocol 接口：使用 duck typing，不强制继承
5. 平滑过渡：Adapter 允许旧代码逐步迁移
"""

from .metadata import (
    Rational,
    VideoMetadata,
    AudioMetadata,
    COMPRESSED_AUDIO_CODECS,
    UNCOMPRESSED_AUDIO_CODECS,
    FPS_23_976,
    FPS_24,
    FPS_25,
    FPS_29_97,
    FPS_30,
    FPS_50,
    FPS_59_94,
    FPS_60,
    COMMON_FRAME_RATES,
)
from .timecode import (
    TimecodeType,
    Timecode,
    DROP_FRAME_FPS_RATIOS,
)
from .timeline import (
    TimelineClip,
    TimelineTrack,
    Timeline,
    MIN_SPEED,
    MAX_SPEED,
)
from .frame import (
    DecodedFrame,
    PixelFormatInfo,
    PlaneInfo,
    PIXEL_FORMATS,
    get_pixel_format,
)
from .pixel_format import (
    Subsampling,
    ColorModel,
    PixelFormatRegistry,
)
from .decode_policy import (
    DecodeTarget,
    DecodePolicy,
    DecodePolicyManager,
    HardwareDecodeStatus,  # P9.A
)
from .frame_path import (
    DecodePath,
    RenderFramePath,
    classify_decode_path,
    classify_render_frame_path,
    is_zero_copy_combination,
    fallback_reason_for_decode_path,
    fallback_reason_for_render_path,
)
from .gpu_frame import (
    GpuFramePlane,
    GpuFrameRelease,
    GpuDecodedFrame,
    is_gpu_decoded_frame,
    is_zero_copy_frame,
    get_frame_decode_path,
    get_frame_render_path,
    safe_release_gpu_frame,
)
from .source import (
    MediaSource,
    VideoSource,
    AudioSource,
    MediaSourceError,
    MediaOpenError,
    MediaSeekError,
    MediaDecodeError,
)
from .errors import (
    MediaError,
    UnsupportedCodecError,
    DecodeError,
    SeekError,
    ColorMetadataWarning,
    FileCorruptedError,
)
from .probe import (
    check_ffmpeg_available,
    get_ffmpeg_version,
    get_supported_codecs,
    check_codec_supported,
    clear_cache,
)
from .ffmpeg_source import (
    FFmpegMediaSource,
    GOP_CODECS,
    INTRA_CODECS,
    _is_gop_codec,
    _is_intra_codec,
    COLOR_PRIMARIES_MAP,
    COLOR_TRANSFER_MAP,
    COLOR_SPACE_MAP,
    COLOR_RANGE_MAP,
    PIXEL_FORMAT_DEPTH_MAP,
)
from .topos_source import ToposMediaSource
from .topos_frame_extract import (
    ToposFrameExtractionResult,
    ToposFrameExtractor,
    extract_topos_frame_to_toos,
)
from .hwaccel import (
    HardwareDecoder,
    HardwareCapability,
    HardwareDecodeConfig,
    FFmpegHwaccelSource,
    HWACCEL_CODEC_SUPPORT,
    HWACCEL_RECOMMENDED_CODECS,
    HWACCEL_NOT_RECOMMENDED_CODECS,
    create_hwaccel_source,
    get_hwaccel_status,
)
from .seek import (
    frame_to_pts,
    pts_to_frame,
    frame_to_seconds,
    seconds_to_frame,
    is_gop_codec,
    is_intra_codec,
    estimate_seek_cost,
    choose_seek_strategy,
    estimate_keyframe_interval,
    SeekInfo,
)
from .factory import (
    MediaBackend,
    MediaSourceFactory,
    configure_media_backend,
    get_current_backend_config,
)
from .adapter import (
    VideoDecoderAdapter,
    VideoDecoderWrapper,
)
from .decode_queue import (
    FrameRequest,
    FramePriority,
    DecodedFrameEntry,
    DecodeQueue,
)
from .frame_cache import (
    FrameCacheEntry,
    FrameCache,
    calculate_frame_memory,
)
from .color_decode import (
    ColorConverter,
    get_color_converter,
)
from .proxy import (
    ProxyStrategy,
    ProxyConfig,
    calculate_proxy_size,
    should_use_proxy,
    get_recommended_strategy,
    is_proxy_frame,
    calculate_scale_ratio,
    DEFAULT_PROXY_CONFIG,
    # P10.3 Disk Proxy
    ProxyFormat,
    ProxyResolution,
    DiskProxyConfig,
    generate_proxy_key,
    get_source_file_info,
    DEFAULT_DISK_PROXY_CONFIG,
    # P1-1 Proxy Status
    ProxyStatus,
    DiskProxyInfo,
    PlaybackProxyState,
    get_proxy_state_display,
)
from .proxy_generator import (
    ProxyGenerator,
    ProxyGenerationResult,
    ProxyProgress,
)
from .proxy_manager import (
    ProxyManager,
    ProxyTask,
    ProxyStats,
)
from .cache import (
    CachePolicy,
    CacheEntry,
    FrameCache as DualPathFrameCache,
    DualFrameCache,
)
from .error_handling import (
    ErrorMessageTemplate,
    UserMessage,
    ErrorSeverity,
    MediaErrorHandler,
    FallbackLevel,
    FallbackResult,
    FallbackStrategy,
    handle_media_error,
    safe_open_media,
    MediaErrorContext,
)
from .macos_vt import (
    MacOSZeroCopyCapability,
    MacOSVtZeroCopySource,
)
from .windows_hw import (
    WindowsZeroCopyCapability,
)

# 公共导出
__all__ = [
    # Metadata
    'Rational',
    'VideoMetadata',
    'AudioMetadata',
    'COMPRESSED_AUDIO_CODECS',
    'UNCOMPRESSED_AUDIO_CODECS',
    'FPS_23_976',
    'FPS_24',
    'FPS_25',
    'FPS_29_97',
    'FPS_30',
    'FPS_50',
    'FPS_59_94',
    'FPS_60',
    'COMMON_FRAME_RATES',
    # Timecode (P9.1)
    'TimecodeType',
    'Timecode',
    'DROP_FRAME_FPS_RATIOS',
    # Timeline
    'TimelineClip',
    'TimelineTrack',
    'Timeline',
    'MIN_SPEED',
    'MAX_SPEED',
    # Frame
    'DecodedFrame',
    'PixelFormatInfo',
    'PlaneInfo',
    'PIXEL_FORMATS',
    'get_pixel_format',
    # Pixel Format Registry
    'Subsampling',
    'ColorModel',
    'PixelFormatRegistry',
    # Decode Policy
    'DecodeTarget',
    'DecodePolicy',
    'DecodePolicyManager',
    'HardwareDecodeStatus',  # P9.A
    # Frame Path (Zero-Copy)
    'DecodePath',
    'RenderFramePath',
    'classify_decode_path',
    'classify_render_frame_path',
    'is_zero_copy_combination',
    'fallback_reason_for_decode_path',
    'fallback_reason_for_render_path',
    # GPU Frame (Zero-Copy)
    'GpuFramePlane',
    'GpuFrameRelease',
    'GpuDecodedFrame',
    'is_gpu_decoded_frame',
    'is_zero_copy_frame',
    'get_frame_decode_path',
    'get_frame_render_path',
    'safe_release_gpu_frame',
    # Source
    'MediaSource',
    'VideoSource',
    'AudioSource',
    'MediaSourceError',
    'MediaOpenError',
    'MediaSeekError',
    'MediaDecodeError',
    # Errors
    'MediaError',
    'UnsupportedCodecError',
    'DecodeError',
    'SeekError',
    'ColorMetadataWarning',
    'FileCorruptedError',
    # Probe
    'check_ffmpeg_available',
    'get_ffmpeg_version',
    'get_supported_codecs',
    'check_codec_supported',
    'clear_cache',
    # FFmpeg Source
    'FFmpegMediaSource',
    # Topos Source（阶段 6）
    'ToposMediaSource',
    'ToposFrameExtractionResult',
    'ToposFrameExtractor',
    'extract_topos_frame_to_toos',
    'GOP_CODECS',
    'INTRA_CODECS',
    '_is_gop_codec',
    '_is_intra_codec',
    'COLOR_PRIMARIES_MAP',
    'COLOR_TRANSFER_MAP',
    'COLOR_SPACE_MAP',
    'COLOR_RANGE_MAP',
    'PIXEL_FORMAT_DEPTH_MAP',
    # Hardware Acceleration (P10.1/P10.2)
    'HardwareDecoder',
    'HardwareCapability',
    'HardwareDecodeConfig',
    'FFmpegHwaccelSource',
    'HWACCEL_CODEC_SUPPORT',
    'HWACCEL_RECOMMENDED_CODECS',
    'HWACCEL_NOT_RECOMMENDED_CODECS',
    'create_hwaccel_source',
    'get_hwaccel_status',
    # Seek helpers
    'frame_to_pts',
    'pts_to_frame',
    'frame_to_seconds',
    'seconds_to_frame',
    'is_gop_codec',
    'is_intra_codec',
    'estimate_seek_cost',
    'choose_seek_strategy',
    'estimate_keyframe_interval',
    'SeekInfo',
    # Factory
    'MediaBackend',
    'MediaSourceFactory',
    'configure_media_backend',
    'get_current_backend_config',
    # Adapter
    'VideoDecoderAdapter',
    'VideoDecoderWrapper',
    # Decode Queue
    'FrameRequest',
    'FramePriority',
    'DecodedFrameEntry',
    'DecodeQueue',
    # Frame Cache
    'FrameCacheEntry',
    'FrameCache',
    'calculate_frame_memory',
    # Color Decode (P7.2)
    'ColorConverter',
    'get_color_converter',
    # Proxy (P5.2)
    'ProxyStrategy',
    'ProxyConfig',
    'calculate_proxy_size',
    'should_use_proxy',
    'get_recommended_strategy',
    'is_proxy_frame',
    'calculate_scale_ratio',
    'DEFAULT_PROXY_CONFIG',
    # Disk Proxy (P10.3)
    'ProxyFormat',
    'ProxyResolution',
    'DiskProxyConfig',
    'generate_proxy_key',
    'get_source_file_info',
    'DEFAULT_DISK_PROXY_CONFIG',
    # P1-1 Proxy Status
    'ProxyStatus',
    'DiskProxyInfo',
    'PlaybackProxyState',
    'get_proxy_state_display',
    'ProxyGenerator',
    'ProxyGenerationResult',
    'ProxyProgress',
    'ProxyManager',
    'ProxyTask',
    'ProxyStats',
    # Dual Path Cache (P5.1)
    'CachePolicy',
    'CacheEntry',
    'DualPathFrameCache',
    'DualFrameCache',
    # Error Handling (P11.4)
    'ErrorMessageTemplate',
    'UserMessage',
    'ErrorSeverity',
    'MediaErrorHandler',
    'FallbackLevel',
    'FallbackResult',
    'FallbackStrategy',
    'handle_media_error',
    'safe_open_media',
    'MediaErrorContext',
    # macOS VT Zero-Copy (Phase C)
    'MacOSZeroCopyCapability',
    'MacOSVtZeroCopySource',
    # Windows HW Zero-Copy (Phase E)
    'WindowsZeroCopyCapability',
]

# N20（2026-10-25）：PyAV 日志回调 GIL 死锁防护（生产侧）。
# tests/edit 全组单进程长跑实测挂起，sample 栈定位：libavcodec 帧线程
# av_log → PyAV Cython 日志回调 → PyGILState_Ensure 永等 GIL，而主线程
# 持 GIL 等待编码线程收尾（close/join）——经典死锁。PyAV 默认把 av_log
# 路由进 Python logging，令 FFmpeg 工作线程依赖 GIL。恢复 FFmpeg 默认
# C 回调（stderr 直写）后工作线程不再进 Python；av 诊断仍可在 stderr
# 看到（需要结构化 av 日志时由调用方在**持有 GIL 的线程**主动查询，
# 不经回调）。
try:  # av 缺席的环境（纯逻辑包）不受影响
    import av.logging as _av_logging
    _av_logging.restore_default_callback()
except Exception:  # noqa: BLE001
    pass

