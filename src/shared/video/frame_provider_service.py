"""
Video Frame Provider Service Module
提供统一的视频帧服务，Color、Composite、Export 共用同一帧源。

## 职责（P7.A）

- VideoFrameProviderService: 视频帧提供服务
- ClipSession: 单个视频剪辑的会话管理
- FrameQuality: 帧质量级别枚举

## 设计原则

- 同一视频文件共享 MediaSource，避免重复打开
- proxy/master 分离：预览用 proxy，导出用 master
- 多个 VideoFrameNode 引用同一文件共享 source/cache
- Color 页面和 Composite 页面请求同一帧时 frame_index 一致
- 完整类型注解

## 使用示例

```python
from src.shared.video import VideoFrameProviderService, FrameQuality

# 创建服务实例（全局或应用级）
service = VideoFrameProviderService()

# 打开视频剪辑
clip_id = service.open_clip('/path/to/video.mp4')

# 请求帧（proxy quality 用于预览）
frame = service.request_frame(clip_id, 0, FrameQuality.PROXY)

# 请求高质量帧（用于导出）
master_frame = service.request_frame(clip_id, 0, FrameQuality.MASTER)

# 预取帧范围
service.prefetch(clip_id, 0, 10, FrameQuality.PROXY)

# 关闭剪辑
service.close_clip(clip_id)

# 关闭所有剪辑（应用退出时）
service.shutdown()
```

## 与 VideoSession 的关系

VideoFrameProviderService 是 VideoSession 的子集功能对外暴露：
- VideoSession 用于 Color 页面的完整播放控制
- VideoFrameProviderService 用于 Composite 节点的帧获取
- 两者可以共享底层 SourceManager 和帧缓存

## 架构位置

```text
┌────────────────────────────────────────────────────────────────────┐
│ Composite / Color / Export                                          │
│ - 通过 VideoFrameProviderService 获取帧                              │
│ - 不直接创建 MediaSourceFactory                                      │
└───────────────────────────────┬────────────────────────────────────┘
                                │ request_frame / prefetch
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│ VideoFrameProviderService                                           │
│ - clip_id -> ClipSession 映射                                       │
│ - 同路径共享 MediaSource                                             │
│ - proxy/master 分离                                                  │
│ - 帧缓存管理                                                         │
└───────────────┬────────────────────────────┬───────────────────────┘
                │                            │
                ▼                            ▼
┌─────────────────────────────┐   ┌──────────────────────────────────┐
│ ClipSession (per-clip)       │   │ Shared SourceManager             │
│ - 独立帧缓存                  │   │ - master_source                  │
│ - proxy/master 请求          │   │ - proxy_source                   │
│ - 解码策略                    │   │ - 帧请求队列                      │
└───────────────────────────────┘   └──────────────────────────────────┘
```

Note:
    VideoFrameNode 不直接创建 MediaSourceFactory，而是通过 service 拿帧。
    Composite 预览用 proxy quality，最终导出用 master quality。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Dict, List, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame
    from ..media.metadata import VideoMetadata
    from ..media.source import MediaSource
    from ..media.proxy import PlaybackProxyState, ProxyConfig, DiskProxyInfo
    from ..media.proxy_generator import ProxyProgress

logger = logging.getLogger(__name__)


# =============================================================================
# 帧质量级别
# =============================================================================

class FrameQuality(Enum):
    """
    帧质量级别枚举。

    Values:
        PROXY: 低分辨率代理帧（预览、实时播放）
        MASTER: 高质量原始帧（暂停、逐帧、导出）
        AUTO: 自动选择（根据 render_mode 决定）
    """
    PROXY = auto()
    MASTER = auto()
    AUTO = auto()

    @staticmethod
    def from_string(quality_str: str) -> FrameQuality:
        """
        从字符串转换。

        Args:
            quality_str: 质量字符串 ('proxy', 'master', 'auto')

        Returns:
            FrameQuality 枚举值
        """
        mapping = {
            'proxy': FrameQuality.PROXY,
            'master': FrameQuality.MASTER,
            'auto': FrameQuality.AUTO,
        }
        return mapping.get(quality_str.lower(), FrameQuality.AUTO)

    def to_string(self) -> str:
        """转换为字符串"""
        return self.name.lower()


# =============================================================================
# 剪辑会话
# =============================================================================

@dataclass
class ClipSession:
    """
    单个视频剪辑的会话管理。

    Attributes:
        clip_id: 唯一剪辑标识
        path: 视频文件路径
        metadata: 视频元数据
        master_source: 高质量媒体源
        proxy_source: 代理媒体源（可选）
        disk_proxy_source: 磁盘代理媒体源（专业NLE优化）
        proxy_config: 代理配置
        disk_proxy_info: 磁盘代理信息（状态、路径等）
        frame_cache_proxy: Proxy 帧缓存 {frame_index: DecodedFrame}
        frame_cache_master: Master 帧缓存 {frame_index: DecodedFrame}
        ref_count: 引用计数（多个节点引用同一剪辑）
        created_at: 创建时间
        last_accessed: 最后访问时间
        is_open: 是否已打开
        open_error: 打开失败的错误信息
        stats: 剪辑级性能统计
        playback_mode: 播放模式 ('preview', 'paused', 'export')
    """

    clip_id: str
    path: str
    metadata: Optional[VideoMetadata] = None
    master_source: Optional[MediaSource] = None
    proxy_source: Optional[MediaSource] = None  # 内存降级源
    disk_proxy_source: Optional[MediaSource] = None  # 磁盘代理源
    proxy_config: Optional[ProxyConfig] = None
    disk_proxy_info: Optional[DiskProxyInfo] = None  # 磁盘代理状态
    frame_cache_proxy: Dict[int, DecodedFrame] = field(default_factory=dict)
    frame_cache_master: Dict[int, DecodedFrame] = field(default_factory=dict)
    ref_count: int = 1
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    is_open: bool = False
    open_error: Optional[str] = None
    playback_mode: str = 'preview'  # 'preview', 'paused', 'export'
    defer_memory_proxy: bool = False
    memory_proxy_pending: bool = False
    # macOS VT 零拷贝源（可选）。解码线程产出"延迟 GL 导入"的 GpuDecodedFrame，
    # 由渲染线程绑定外部纹理；不可用时保持 None，全部走 CPU 源。
    vt_source: Optional[Any] = None
    # VT 源与 CPU 源各自的操作锁：master/proxy worker 与合成渲染线程可能
    # 并发请求同一会话，PyAV 解码器不支持并发 seek/read，必须串行化。
    vt_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Re-entrant because request_frame holds this lock while rechecking the
    # cache and _decode_frame also protects direct callers.  That lets the
    # service coalesce concurrent same-frame misses without changing the
    # private decoder method's call signature.
    decode_lock: threading.Lock = field(default_factory=threading.RLock, repr=False)
    # 前台 decode_lock 等待者计数：request_frame 进入锁等待前 +1、离开后
    # -1。后台 prefetch 每帧解码前检查，>0 时立即让路终止本轮扫描，避免
    # 前台请求（尤其缓存命中快路径之外的 miss）被连续的后台解码排队。
    decode_waiters: int = field(default=0, repr=False)
    decode_waiters_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # AUD-39103：打开完成事件。首个 open_clip 在锁外打开源；并发共享
    # 调用方锁外等待该事件，打开结束后才裁决共享（成功 → 加引用）或
    # 自建（失败 → 原会话已被清理，绝不返回死 clip_id）。
    open_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # AUD-39106：_close_clip_sources 置位。此后会话不再接受新源赋值，
    # 防止向已关闭/已摘除的会话赋新源造成"永不释放"泄漏。
    sources_closed: bool = field(default=False, repr=False)

    # 缓存配置
    max_proxy_cache: int = 16
    max_master_cache: int = 4

    # Per-clip 统计
    _stats_frames_requested: int = field(default=0)
    _stats_cache_hits: int = field(default=0)
    _stats_cache_misses: int = field(default=0)
    _stats_proxy_requests: int = field(default=0)
    _stats_master_requests: int = field(default=0)
    _stats_total_decode_ms: float = field(default=0.0)

    def touch(self) -> None:
        """更新最后访问时间"""
        self.last_accessed = time.time()

    def add_ref(self) -> int:
        """增加引用计数"""
        self.ref_count += 1
        return self.ref_count

    def release_ref(self) -> int:
        """减少引用计数"""
        self.ref_count -= 1
        return self.ref_count

    def should_cleanup(self) -> bool:
        """判断是否应该清理（引用计数为 0）"""
        return self.ref_count <= 0

    def get_active_proxy_source(self) -> Optional[MediaSource]:
        """
        获取活跃的代理媒体源。

        根据磁盘代理状态和播放模式，选择最优的代理源：
        - disk_proxy_source: 磁盘代理文件（优先）
        - proxy_source: 内存降级源（fallback）

        Returns:
            MediaSource 或 None

        Note:
            专业 NLE 优先级：
            1. disk_proxy_source（如果就绪）
            2. proxy_source（内存降级）
            3. None（使用 master）
        """
        # 检查磁盘代理是否就绪
        if self.disk_proxy_info is not None and self.disk_proxy_info.is_ready:
            if self.disk_proxy_source is not None:
                return self.disk_proxy_source

        # fallback 到内存代理源
        return self.proxy_source

    def get_source_for_quality(
        self,
        quality: FrameQuality,
    ) -> Optional[MediaSource]:
        """
        根据质量级别获取合适的媒体源。

        Args:
            quality: 帧质量级别

        Returns:
            MediaSource 或 None

        Note:
            - MASTER: 总是使用 master_source
            - PROXY: 使用 get_active_proxy_source()
            - AUTO: 根据 playback_mode 决定
        """
        if quality == FrameQuality.MASTER:
            return self.master_source

        if quality == FrameQuality.PROXY:
            return self.get_active_proxy_source()

        # AUTO: 根据 playback_mode 决定
        if self.playback_mode in ('paused', 'export'):
            return self.master_source
        else:
            return self.get_active_proxy_source() or self.master_source

    def get_proxy_state(self) -> 'PlaybackProxyState':
        """
        获取播放代理状态。

        用于 UI 显示当前使用的帧源状态。

        Returns:
            PlaybackProxyState 枚举值

        Note:
            P1-1: 专业 NLE 状态显示。
            - USING_DISK_PROXY: 使用磁盘代理（最高优先级）
            - USING_MEMORY_PROXY: 使用内存降级代理
            - GENERATING_PROXY: 正在生成代理
            - USING_ORIGINAL: 使用原始素材（无代理或暂停/导出模式）
        """
        from ..media.proxy import PlaybackProxyState

        return PlaybackProxyState.from_disk_proxy_info(
            disk_proxy_info=self.disk_proxy_info,
            has_disk_proxy_source=self.disk_proxy_source is not None,
            has_memory_proxy_source=self.proxy_source is not None,
            playback_mode=self.playback_mode,
        )

    def get_proxy_generation_progress(self) -> float:
        """
        获取代理生成进度。

        Returns:
            进度百分比（0.0-100.0）
        """
        if self.disk_proxy_info is not None:
            return self.disk_proxy_info.generation_progress
        return 0.0

    def set_playback_mode(self, mode: str) -> None:
        """
        设置播放模式。

        Args:
            mode: 播放模式 ('preview', 'paused', 'export')
        """
        if mode in ('preview', 'paused', 'export'):
            self.playback_mode = mode

    def evict_old_frames(self, center_frame: int, quality: FrameQuality) -> None:
        """
        清理旧缓存帧。

        Args:
            center_frame: 当前帧位置
            quality: 质量级别
        """
        if quality == FrameQuality.PROXY:
            cache = self.frame_cache_proxy
            max_size = self.max_proxy_cache
        else:
            cache = self.frame_cache_master
            max_size = self.max_master_cache

        if len(cache) <= max_size:
            return

        # 按距离排序，保留最近的帧
        sorted_frames = sorted(
            cache.keys(),
            key=lambda f: abs(f - center_frame)
        )

        # 移除最远的帧
        to_remove = sorted_frames[max_size:]
        for frame_idx in to_remove:
            cache.pop(frame_idx, None)

    def resolve_cache_quality(self, quality: FrameQuality) -> FrameQuality:
        """AUD-39101：把请求质量解析为实际缓存桶质量。

        AUTO 请求在 preview 模式下由代理源（磁盘/内存）服务，但
        get_cached_frame/cache_frame 按 AUTO != PROXY 落进 master 桶：
        proxy 分辨率帧被写进 master 缓存，后续 MASTER 请求直接命中
        低分辨率帧（缓存桶错位）。这里按 get_source_for_quality 实际
        选择的源决定桶：master 源 → MASTER；代理源（或无源）→ PROXY。
        """
        if quality != FrameQuality.AUTO:
            return quality
        source = self.get_source_for_quality(quality)
        if source is not None and source is self.master_source:
            return FrameQuality.MASTER
        return FrameQuality.PROXY

    def get_cached_frame(
        self,
        frame_index: int,
        quality: FrameQuality
    ) -> Optional[DecodedFrame]:
        """
        从缓存获取帧。

        Args:
            frame_index: 帧号
            quality: 质量级别

        Returns:
            DecodedFrame 或 None
        """
        self.touch()

        if quality == FrameQuality.PROXY:
            return self.frame_cache_proxy.get(frame_index)
        else:
            return self.frame_cache_master.get(frame_index)

    def cache_frame(
        self,
        frame_index: int,
        frame: DecodedFrame,
        quality: FrameQuality
    ) -> None:
        """
        缓存帧。

        Args:
            frame_index: 帧号
            frame: DecodedFrame 实例
            quality: 质量级别
        """
        if quality == FrameQuality.PROXY:
            self.frame_cache_proxy[frame_index] = frame
        else:
            self.frame_cache_master[frame_index] = frame

    def record_request(
        self,
        quality: FrameQuality,
        cache_hit: bool,
        decode_ms: float,
    ) -> None:
        """
        记录帧请求统计。

        Args:
            quality: 质量级别
            cache_hit: 是否缓存命中
            decode_ms: 解码时间（毫秒）
        """
        self._stats_frames_requested += 1

        if quality == FrameQuality.PROXY:
            self._stats_proxy_requests += 1
        else:
            self._stats_master_requests += 1

        if cache_hit:
            self._stats_cache_hits += 1
        else:
            self._stats_cache_misses += 1

        self._stats_total_decode_ms += decode_ms

    def get_stats(self) -> Dict[str, float | int]:
        """
        获取剪辑级统计。

        Returns:
            统计信息字典
        """
        total_requests = self._stats_frames_requested
        cache_hit_rate = (
            self._stats_cache_hits / total_requests
            if total_requests > 0
            else 0.0
        )
        avg_decode_ms = (
            self._stats_total_decode_ms / total_requests
            if total_requests > 0
            else 0.0
        )

        return {
            'clip_id': self.clip_id,
            'path': self.path,
            'ref_count': self.ref_count,
            'is_open': self.is_open,
            'frames_requested': self._stats_frames_requested,
            'proxy_requests': self._stats_proxy_requests,
            'master_requests': self._stats_master_requests,
            'cache_hits': self._stats_cache_hits,
            'cache_misses': self._stats_cache_misses,
            'cache_hit_rate': cache_hit_rate,
            'total_decode_ms': self._stats_total_decode_ms,
            'avg_decode_ms': avg_decode_ms,
            'proxy_cache_size': len(self.frame_cache_proxy),
            'master_cache_size': len(self.frame_cache_master),
            'max_proxy_cache': self.max_proxy_cache,
            'max_master_cache': self.max_master_cache,
        }


# =============================================================================
# 剪辑信息
# =============================================================================

@dataclass(frozen=True)
class ClipInfo:
    """
    剪辑信息（不包含敏感资源）。

    用于返回给调用方，不暴露 MediaSource。

    Attributes:
        clip_id: 剪辑标识
        path: 文件路径
        width: 视频宽度
        height: 视频高度
        fps: 帧率
        total_frames: 总帧数
        duration: 时长（秒）
        bit_depth: 位深度
        is_hdr: 是否 HDR
        codec_name: 编码名称
        color_primaries: 色域
        color_transfer: 传输函数
        color_matrix: 矩阵
        color_range: 信号范围（limited/full）
        proxy_state: P1-1 播放代理状态
        proxy_generation_progress: P1-1 代理生成进度（0.0-100.0）
    """

    clip_id: str
    path: str
    width: int = 0
    height: int = 0
    fps: float = 0.0
    total_frames: int = 0
    duration: float = 0.0
    bit_depth: int = 8
    is_hdr: bool = False
    codec_name: str = ""
    color_primaries: str = ""
    color_transfer: str = ""
    color_matrix: str = ""
    color_range: str = ""
    # P1-1: Proxy 状态
    proxy_state: str = "Using original"  # UI 显示字符串
    proxy_generation_progress: float = 0.0

    @classmethod
    def from_metadata(
        cls,
        clip_id: str,
        path: str,
        metadata: Optional[VideoMetadata],
        proxy_state: str = "Using original",
        proxy_progress: float = 0.0,
    ) -> ClipInfo:
        """
        从 VideoMetadata 创建 ClipInfo。

        Args:
            clip_id: 剪辑标识
            path: 文件路径
            metadata: 视频元数据
            proxy_state: P1-1 代理状态字符串
            proxy_progress: P1-1 代理生成进度

        Returns:
            ClipInfo 实例
        """
        if metadata is None:
            return cls(
                clip_id=clip_id,
                path=path,
                proxy_state=proxy_state,
                proxy_generation_progress=proxy_progress,
            )

        duration = getattr(metadata, 'duration', None)
        if duration is None:
            duration = getattr(metadata, 'duration_seconds', None)

        return cls(
            clip_id=clip_id,
            path=path,
            width=int(getattr(metadata, 'width', 0) or 0),
            height=int(getattr(metadata, 'height', 0) or 0),
            fps=metadata.fps.to_fps() if getattr(metadata, 'fps', None) is not None else 0.0,
            total_frames=int(getattr(metadata, 'total_frames', 0) or 0),
            duration=float(duration or 0.0),
            bit_depth=int(getattr(metadata, 'bit_depth', 8) or 8),
            is_hdr=bool(getattr(metadata, 'is_hdr', False)),
            codec_name=getattr(metadata, 'codec_name', None) or "",
            color_primaries=getattr(metadata, 'color_primaries', None) or "",
            color_transfer=getattr(metadata, 'color_transfer', None) or "",
            color_matrix=getattr(metadata, 'color_matrix', None) or "",
            color_range=getattr(metadata, 'color_range', None) or "",
            proxy_state=proxy_state,
            proxy_generation_progress=proxy_progress,
        )

    @classmethod
    def from_session(cls, session: ClipSession) -> ClipInfo:
        """
        从 ClipSession 创建 ClipInfo。

        P1-1: 包含代理状态信息。

        Args:
            session: 剪辑会话

        Returns:
            ClipInfo 实例
        """
        from ..media.proxy import get_proxy_state_display

        proxy_state = session.get_proxy_state()
        proxy_progress = session.get_proxy_generation_progress()

        # 获取显示字符串
        proxy_state_display = get_proxy_state_display(proxy_state, proxy_progress)

        return cls.from_metadata(
            clip_id=session.clip_id,
            path=session.path,
            metadata=session.metadata,
            proxy_state=proxy_state_display,
            proxy_progress=proxy_progress,
        )


# =============================================================================
# VideoFrameProviderService: 视频帧提供服务
# =============================================================================

class VideoFrameProviderService:
    """
    视频帧提供服务。

    实现 P7.A 的 VideoFrameProviderService 设计：
    - 定义服务接口：open_clip, request_frame, prefetch, close_clip
    - VideoFrameNode 不直接创建 MediaSourceFactory，而是通过 service 拿帧
    - Composite 预览用 proxy quality，最终导出用 master quality
    - 多个节点引用同一个视频文件时共享 source/cache

    Attributes:
        _clips: clip_id -> ClipSession 映射
        _path_to_clips: path -> [clip_ids] 映射（用于共享检测）
        _lock: 线程锁
        _proxy_config: 默认代理配置
        _backend: 媒体后端偏好

    Note:
        两个 VideoFrameNode 指向同一文件，不重复打开两个 master source
        Color 页面和 Composite 页面请求同一帧时 frame_index 一致
    """

    def __init__(
        self,
        proxy_config: Optional[ProxyConfig] = None,
        backend: str = 'auto',
        max_proxy_cache: int = 16,
        max_master_cache: int = 4,
        auto_generate_disk_proxies: bool = False,
    ) -> None:
        """
        初始化帧提供服务。

        Args:
            proxy_config: 代理配置（默认使用 1/2 分辨率）
            backend: 媒体后端偏好 ('auto', 'ffmpeg', 'gstreamer')
            max_proxy_cache: Proxy 帧缓存最大数量
            max_master_cache: Master 帧缓存最大数量
            auto_generate_disk_proxies: 是否允许打开素材时启动后台转码。
                编辑播放必须保持 False，代理只能由显式的用户/缓存任务生成。
        """
        self._clips: Dict[str, ClipSession] = {}
        self._path_to_clips: Dict[str, List[str]] = {}
        self._lock: threading.Lock = threading.Lock()

        # 配置
        if proxy_config is None:
            from ..media.proxy import DEFAULT_PROXY_CONFIG
            proxy_config = DEFAULT_PROXY_CONFIG

        self._proxy_config = proxy_config
        self._backend = backend
        self._max_proxy_cache = max_proxy_cache
        self._max_master_cache = max_master_cache
        self._auto_generate_disk_proxies = bool(auto_generate_disk_proxies)

        # 统计
        # AUD-39108: 播放/导出线程并发请求帧时这些计数会被并发自增，
        # += 读-改-写需要锁保护，否则高并发下丢失更新、命中率失真。
        self._stats_lock: threading.Lock = threading.Lock()
        self._total_clips_opened: int = 0
        self._total_clips_closed: int = 0
        self._total_frames_served: int = 0
        self._total_cache_hits: int = 0
        self._total_cache_misses: int = 0

        logger.debug("VideoFrameProviderService initialized")

    # =============================================================================
    # 属性
    # =============================================================================

    @property
    def total_clips_opened(self) -> int:
        """累计打开的剪辑数"""
        return self._total_clips_opened

    @property
    def total_clips_closed(self) -> int:
        """累计关闭的剪辑数"""
        return self._total_clips_closed

    @property
    def total_frames_served(self) -> int:
        """累计服务的帧数"""
        # AUD-39108: 与自增互斥读取
        with self._stats_lock:
            return self._total_frames_served

    @property
    def cache_hit_rate(self) -> float:
        """缓存命中率"""
        # AUD-39108: 命中/未命中两个值必须同一把锁下读取，否则可能读到
        # 不一致的配对。
        with self._stats_lock:
            total_requests = self._total_cache_hits + self._total_cache_misses
            hits = self._total_cache_hits
        if total_requests == 0:
            return 0.0
        return hits / total_requests

    @property
    def active_clip_count(self) -> int:
        """当前活跃的剪辑数"""
        with self._lock:
            return len(self._clips)

    # =============================================================================
    # 剪辑管理
    # =============================================================================

    def open_clip(
        self,
        path: str,
        share_by_path: bool = True,
        defer_memory_proxy: bool = False,
    ) -> str:
        """
        打开视频剪辑。

        查询 DiskProxyService，优先使用已就绪的磁盘代理。
        未就绪时默认直接打开原始素材；不得在播放热路径启动转码。

        如果同一文件已被打开，返回现有 clip_id 并增加引用计数。
        否则创建新的 ClipSession。

        Args:
            path: 视频文件路径
            share_by_path: True 时同一路径复用已打开会话；False 时强制
                创建独立会话。时间线多轨预览中，同一素材的不同 clip
                可能同时请求不同 source frame，必须使用独立解码游标。
            defer_memory_proxy: True 时首帧只同步打开 master/磁盘代理，
                内存降级 proxy 在后台打开，避免时间线首帧被 proxy 初始化阻塞。

        Returns:
            clip_id: 剪辑唯一标识

        Raises:
            MediaOpenError: 无法打开文件

        Note:
            专业 NLE 优先级：
            1. disk_proxy_source（如果磁盘代理就绪）
            2. proxy_source（内存降级源）
            3. master_source（无代理时）

            代理生成是显式的媒体池/缓存任务，不在打开视频时执行。
        """
        clip_id = self._generate_clip_id(path)
        while True:
            pending_open_event: Optional[threading.Event] = None
            with self._lock:
                # 检查是否已有剪辑
                if share_by_path and path in self._path_to_clips:
                    existing_clip_ids = self._path_to_clips[path]
                    if existing_clip_ids:
                        shared_clip_id = existing_clip_ids[0]
                        existing_session = self._clips.get(shared_clip_id)
                        if existing_session is not None:
                            if not existing_session.open_event.is_set():
                                # AUD-39103：首个打开者仍在打开中——
                                # 锁外等它完成后再重新裁决，绝不拿着
                                # "可能因打开失败而被移除"的 clip_id
                                # 提前返回。
                                pending_open_event = existing_session.open_event
                            elif existing_session.open_error is None:
                                # 使用现有剪辑，增加引用计数
                                existing_session.add_ref()
                                existing_session.touch()
                                logger.info(
                                    f"Clip already open, adding ref: {path} -> "
                                    f"{shared_clip_id}, "
                                    f"ref_count={existing_session.ref_count}"
                                )
                                return shared_clip_id
                        # 会话不存在（失败清理中/已完成）或 open_error 有值
                        # （打开已失败）→ 落到新建
            if pending_open_event is None:
                break
            pending_open_event.wait()

        session = ClipSession(
            clip_id=clip_id,
            path=path,
            max_proxy_cache=self._max_proxy_cache,
            max_master_cache=self._max_master_cache,
            defer_memory_proxy=defer_memory_proxy,
        )

        # 注册会话（此时 open_event 未置位：并发共享调用方会等待打开完成）
        with self._lock:
            self._clips[clip_id] = session

            # 记录路径映射
            if path not in self._path_to_clips:
                self._path_to_clips[path] = []
            self._path_to_clips[path].append(clip_id)

        # P1-1: 自动查询 DiskProxyService
        disk_proxy_info = None
        try:
            from ..media.disk_proxy_service import get_disk_proxy_service

            proxy_service = get_disk_proxy_service()
            disk_proxy_info = proxy_service.get_disk_proxy_info(path)

            # 记录代理状态
            logger.info(
                f"Disk proxy status for {path}: {disk_proxy_info.status.name}"
            )

            # 只有显式开启时才允许打开素材触发后台转码。编辑时间线
            # 使用默认 False，避免首播与编码任务争用硬解、CPU 和磁盘带宽。
            if self._auto_generate_disk_proxies and disk_proxy_info.needs_generation:
                # 生成 asset_id（基于 clip_id）
                asset_id = f"asset-{clip_id}"

                # 触发后台生成（不阻塞）
                proxy_service.generate_proxy(
                    source_path=path,
                    asset_id=asset_id,
                    on_complete=lambda src, info: self._on_proxy_complete(src, info),
                    on_progress=lambda src, prog: self._on_proxy_progress(src, prog),
                )

                logger.info(
                    f"Triggered proxy generation for {path}, "
                    f"status={disk_proxy_info.status.name}"
                )

        except Exception as e:
            logger.warning(
                f"Failed to query DiskProxyService for {path}: {e}, "
                f"proceeding without disk proxy"
            )

        # 在锁外打开媒体源（避免长时间阻塞）
        try:
            # P1-1: 传入 disk_proxy_info
            if disk_proxy_info is not None:
                session.disk_proxy_info = disk_proxy_info

            self._open_clip_sources(session)
            # AUD-39108: 并发自增持锁，防丢更新
            with self._stats_lock:
                self._total_clips_opened += 1
            # AUD-39103：打开成功，唤醒等待共享的调用方（此后共享直接加引用）
            session.open_event.set()

            # P1-1: 记录代理状态
            proxy_state = session.get_proxy_state()
            source_type = 'disk_proxy' if session.disk_proxy_source else (
                'memory_proxy' if session.proxy_source else 'master_only'
            )

            logger.info(
                f"Clip opened: {path} -> {clip_id}, "
                f"{session.metadata.width if session.metadata else 0}x"
                f"{session.metadata.height if session.metadata else 0}, "
                f"{session.metadata.fps.to_fps() if session.metadata else 0:.2f}fps, "
                f"source_type={source_type}, "
                f"proxy_state={proxy_state.display_name}"
            )
            return clip_id

        except Exception as e:
            # AUD-39103：先记录失败并唤醒等待共享的调用方（它们会自建
            # 新会话并得到同样的失败结果），再做清理——共享方绝不返回
            # 死 clip_id、引用计数绝不被静默吞掉。
            session.open_error = str(e)
            session.open_event.set()
            # AUD-39102：打开失败必须释放已打开的源（此前只移除映射，
            # master/proxy/VT 半开状态源泄漏）。
            self._close_clip_sources(session)

            # 打开失败，清理
            with self._lock:
                self._clips.pop(clip_id, None)
                if path in self._path_to_clips:
                    self._path_to_clips[path] = [
                        cid for cid in self._path_to_clips[path]
                        if cid != clip_id
                    ]
                    if not self._path_to_clips[path]:
                        self._path_to_clips.pop(path)

            from ..media.source import MediaOpenError
            raise MediaOpenError(
                f"Failed to open clip: {path}",
                source_path=path
            ) from e

    def open_clip_with_disk_proxy(
        self,
        path: str,
        disk_proxy_info: Optional[DiskProxyInfo] = None,
    ) -> str:
        """
        打开视频剪辑（带磁盘代理信息）。

        专业 NLE 优化：如果磁盘代理就绪，优先打开磁盘代理文件。

        Args:
            path: 视频文件路径
            disk_proxy_info: 磁盘代理信息（可选）

        Returns:
            clip_id: 剪辑唯一标识

        Raises:
            MediaOpenError: 无法打开文件

        Note:
            - 如果 disk_proxy_info.is_ready，优先打开磁盘代理
            - 否则 fallback 到内存降级源
            - master_source 总是从原始文件创建
        """
        clip_id = self._generate_clip_id(path)
        while True:
            pending_open_event: Optional[threading.Event] = None
            with self._lock:
                # 检查是否已有剪辑
                if path in self._path_to_clips:
                    existing_clip_ids = self._path_to_clips[path]
                    if existing_clip_ids:
                        shared_clip_id = existing_clip_ids[0]
                        existing_session = self._clips.get(shared_clip_id)
                        if existing_session is not None:
                            if not existing_session.open_event.is_set():
                                # AUD-39103：与 open_clip 同语义——等待
                                # 在途打开完成后再裁决共享/自建
                                pending_open_event = existing_session.open_event
                            elif existing_session.open_error is None:
                                # 使用现有剪辑，增加引用计数
                                existing_session.add_ref()
                                existing_session.touch()

                                # 更新磁盘代理信息（如果提供）
                                if disk_proxy_info is not None:
                                    existing_session.disk_proxy_info = disk_proxy_info

                                logger.info(
                                    f"Clip already open with disk proxy, adding ref: "
                                    f"{path} -> {shared_clip_id}, "
                                    f"ref_count={existing_session.ref_count}, "
                                    f"proxy_status={disk_proxy_info.status if disk_proxy_info else 'none'}"
                                )
                                return shared_clip_id
            if pending_open_event is None:
                break
            pending_open_event.wait()

        session = ClipSession(
            clip_id=clip_id,
            path=path,
            disk_proxy_info=disk_proxy_info,
            max_proxy_cache=self._max_proxy_cache,
            max_master_cache=self._max_master_cache,
        )

        # 注册会话（open_event 未置位：并发共享调用方等待打开完成）
        with self._lock:
            self._clips[clip_id] = session

            # 记录路径映射
            if path not in self._path_to_clips:
                self._path_to_clips[path] = []
            self._path_to_clips[path].append(clip_id)

        # 在锁外打开媒体源（避免长时间阻塞）
        try:
            self._open_clip_sources(session)
            # AUD-39108: 并发自增持锁，防丢更新
            with self._stats_lock:
                self._total_clips_opened += 1
            # AUD-39103：打开成功，唤醒等待共享的调用方
            session.open_event.set()

            # 获取源类型
            source_type = 'disk_proxy' if session.disk_proxy_source else (
                'memory_proxy' if session.proxy_source else 'master_only'
            )

            logger.info(
                f"Clip opened with disk proxy: {path} -> {clip_id}, "
                f"{session.metadata.width if session.metadata else 0}x"
                f"{session.metadata.height if session.metadata else 0}, "
                f"{session.metadata.fps.to_fps() if session.metadata else 0:.2f}fps, "
                f"source_type={source_type}, "
                f"proxy_status={disk_proxy_info.status if disk_proxy_info else 'none'}"
            )
            return clip_id

        except Exception as e:
            # AUD-39103：记录失败并唤醒等待共享的调用方
            session.open_error = str(e)
            session.open_event.set()
            # AUD-39102：释放已打开的源（此前只移除映射 → 源泄漏）
            self._close_clip_sources(session)

            # 打开失败，清理
            with self._lock:
                self._clips.pop(clip_id, None)
                if path in self._path_to_clips:
                    self._path_to_clips[path] = [
                        cid for cid in self._path_to_clips[path]
                        if cid != clip_id
                    ]
                    if not self._path_to_clips[path]:
                        self._path_to_clips.pop(path)

            from ..media.source import MediaOpenError
            raise MediaOpenError(
                f"Failed to open clip: {path}",
                source_path=path
            ) from e

    def update_disk_proxy_info(
        self,
        clip_id: str,
        disk_proxy_info: DiskProxyInfo,
    ) -> bool:
        """
        更新剪辑的磁盘代理信息。

        用于代理生成完成后更新状态，可能触发重新打开磁盘代理源。

        Args:
            clip_id: 剪辑标识
            disk_proxy_info: 新的磁盘代理信息

        Returns:
            True 表示成功更新

        Note:
            - 如果代理状态变为 ready 且当前没有 disk_proxy_source，
              会尝试打开磁盘代理文件
            - 如果代理状态变为 stale/missing，会关闭磁盘代理源
        """
        with self._lock:
            session = self._clips.get(clip_id)
            if session is None:
                logger.warning(f"Clip not found: {clip_id}")
                return False

            session.disk_proxy_info = disk_proxy_info

        # 处理状态变化
        from ..media.proxy import ProxyStatus

        # 如果代理变为 ready，尝试打开磁盘代理源
        if disk_proxy_info.is_ready and session.disk_proxy_source is None:
            if disk_proxy_info.proxy_path and os.path.isfile(disk_proxy_info.proxy_path):
                disk_proxy_source = None
                try:
                    from ..media.factory import MediaSourceFactory
                    from ..media.proxy import get_disk_proxy_rejection_reason

                    disk_proxy_source = MediaSourceFactory.create(
                        path=disk_proxy_info.proxy_path,
                        backend=self._backend,
                        target_size=None,
                        auto_open=False
                    )
                    MediaSourceFactory.open_source(disk_proxy_source)

                    # AUD-39107（与 _open_clip_sources 同一验收）：磁盘
                    # 代理必须过元数据 probe + 拒绝原因裁决，不合格代理
                    # 不得挂载（此前本路径无校验，坏代理直接上线）。
                    rejection_reason: Optional[str] = None
                    if session.metadata is not None:
                        proxy_metadata = disk_proxy_source.probe()
                        proxy_size = self._calculate_proxy_size_for_metadata(
                            session.metadata)
                        rejection_reason = get_disk_proxy_rejection_reason(
                            session.metadata,
                            proxy_metadata,
                            proxy_size,
                        )
                    if rejection_reason:
                        logger.warning(
                            "Rejecting disk proxy after update: "
                            f"{rejection_reason}; clip={clip_id}"
                        )
                        # 源尚未发布，直接关闭即可（无在途解码）
                        disk_proxy_source.close()
                        with self._lock:
                            session.disk_proxy_info = disk_proxy_info.with_status(
                                ProxyStatus.FAILED
                            )
                        return True

                    old_proxy_source: Optional[MediaSource] = None
                    session_stale = False
                    with self._lock:
                        registered = self._clips.get(clip_id)
                        # AUD-39106：会话已被关闭/摘除 → 不再赋源，锁外
                        # 释放新源（否则新源挂在死会话上永不释放）。
                        if registered is not session or session.sources_closed:
                            session_stale = True
                        else:
                            session.disk_proxy_source = disk_proxy_source
                            # 清理内存代理源（可选）：锁内只摘引用，
                            # close 必须持 decode_lock 且不得在 self._lock
                            # 内执行（与 _decode_frame 的 read_frame 串行化）。
                            if session.proxy_source is not None:
                                old_proxy_source = session.proxy_source
                                session.proxy_source = None

                            # 清除旧的 proxy 帧缓存，确保从新源解码
                            session.frame_cache_proxy.clear()

                    if session_stale:
                        with session.decode_lock:
                            try:
                                disk_proxy_source.close()
                            except Exception as e:
                                logger.warning(
                                    f"Failed to close stale disk proxy source: {e}"
                                )
                        logger.info(
                            f"Discarded disk proxy source for closed clip: {clip_id}"
                        )
                        return True

                    if old_proxy_source is not None:
                        with session.decode_lock:
                            try:
                                old_proxy_source.close()
                            except Exception as e:
                                logger.warning(
                                    f"Failed to close memory proxy source: {e}"
                                )

                    logger.info(
                        f"Disk proxy source opened after update: "
                        f"{disk_proxy_info.proxy_path}, clip={clip_id}, "
                        f"cache cleared"
                    )

                except Exception as e:
                    logger.warning(
                        f"Failed to open disk proxy after update: "
                        f"{disk_proxy_info.proxy_path}, error={e}"
                    )
                    if disk_proxy_source is not None:
                        try:
                            disk_proxy_source.close()
                        except Exception as close_error:
                            logger.warning(
                                f"Failed to close unusable disk proxy source: "
                                f"{close_error}"
                            )
                    with self._lock:
                        session.disk_proxy_info = disk_proxy_info.with_status(
                            ProxyStatus.FAILED
                        )

        # 如果代理变为 stale/missing/failed，关闭磁盘代理源
        elif disk_proxy_info.status in (ProxyStatus.STALE, ProxyStatus.MISSING, ProxyStatus.FAILED):
            disk_proxy_source = session.disk_proxy_source
            if disk_proxy_source is not None:
                # 先在 self._lock 内摘引用（新的解码不再选中该源），再在
                # 锁外持 decode_lock 关闭：与在途 read_frame 串行化，
                # 避免 use-after-close 竞态。
                with self._lock:
                    session.disk_proxy_source = None
                    # 清除旧的 proxy 帧缓存
                    session.frame_cache_proxy.clear()

                with session.decode_lock:
                    try:
                        disk_proxy_source.close()
                    except Exception as e:
                        logger.warning(f"Failed to close disk proxy source: {e}")

                # 如果没有内存代理源，重新创建
                if session.proxy_source is None:
                    self._create_memory_proxy_source(session)

                logger.info(
                    f"Disk proxy source closed, fallback to memory proxy: "
                    f"clip={clip_id}, status={disk_proxy_info.status}, "
                    f"cache cleared"
                )

        return True

    def _create_memory_proxy_source(self, session: ClipSession) -> None:
        """
        创建内存降级代理源。

        Args:
            session: 剪辑会话
        """
        from ..media.factory import MediaSourceFactory

        if session.metadata is None:
            return

        proxy_config = self._proxy_config
        if proxy_config is None:
            return

        proxy_size = self._calculate_proxy_size_for_metadata(session.metadata)

        if proxy_size != (session.metadata.width, session.metadata.height):
            try:
                proxy_source = MediaSourceFactory.create(
                    path=session.path,
                    backend=self._backend,
                    target_size=proxy_size,
                    preserve_planes=self._should_preserve_proxy_planes(session.metadata),
                    auto_open=False
                )
                MediaSourceFactory.open_source(proxy_source)

                session_stale = False
                with self._lock:
                    registered = self._clips.get(session.clip_id)
                    # AUD-39106：会话已被关闭/摘除 → 不得赋源；锁外
                    # 释放新源（否则挂在死会话上永不释放）。
                    if session.sources_closed or (
                        registered is not None and registered is not session
                    ):
                        session_stale = True
                    else:
                        session.proxy_source = proxy_source
                        session.proxy_config = proxy_config
                        proxy_source = None

                if session_stale:
                    with session.decode_lock:
                        try:
                            proxy_source.close()
                        except Exception as e:
                            logger.warning(
                                f"Failed to close stale memory proxy source: {e}"
                            )
                    logger.info(
                        f"Discarded memory proxy source for closed clip: "
                        f"{session.clip_id}"
                    )
                    return

                logger.debug(
                    f"Memory proxy source recreated: {session.path}, "
                    f"size={proxy_size[0]}x{proxy_size[1]}"
                )

            except Exception as e:
                logger.warning(
                    f"Failed to create memory proxy source: {e}"
                )

    def _schedule_deferred_memory_proxy_source(self, clip_id: str) -> None:
        """Open the in-memory proxy source on a background thread."""
        with self._lock:
            session = self._clips.get(clip_id)
            if session is None:
                return
            if session.proxy_source is not None or session.memory_proxy_pending:
                return
            session.memory_proxy_pending = True

        def _worker() -> None:
            proxy_source = None
            try:
                with self._lock:
                    active_session = self._clips.get(clip_id)
                    if active_session is None or active_session.metadata is None:
                        return
                    path = active_session.path
                    metadata = active_session.metadata
                    proxy_config = self._proxy_config

                if proxy_config is None:
                    return

                proxy_size = self._calculate_proxy_size_for_metadata(metadata)
                if proxy_size == (metadata.width, metadata.height):
                    return

                from ..media.factory import MediaSourceFactory

                proxy_source = MediaSourceFactory.create(
                    path=path,
                    backend=self._backend,
                    target_size=proxy_size,
                    preserve_planes=self._should_preserve_proxy_planes(metadata),
                    auto_open=False,
                )
                MediaSourceFactory.open_source(proxy_source)

                with self._lock:
                    active_session = self._clips.get(clip_id)
                    if (
                        active_session is not None
                        and not active_session.sources_closed  # AUD-39106
                        and active_session.proxy_source is None
                        and active_session.master_source is not None
                    ):
                        active_session.proxy_source = proxy_source
                        active_session.proxy_config = proxy_config
                        active_session.frame_cache_proxy.clear()
                        active_session.memory_proxy_pending = False
                        proxy_source = None
                        logger.debug(
                            f"Deferred memory proxy source opened: {path}, "
                            f"size={proxy_size[0]}x{proxy_size[1]}"
                        )
            except Exception as e:
                logger.warning(f"Deferred memory proxy source failed for {clip_id}: {e}")
            finally:
                if proxy_source is not None:
                    try:
                        proxy_source.close()
                    except Exception as e:
                        logger.warning(f"Failed to close unused deferred proxy source: {e}")
                with self._lock:
                    active_session = self._clips.get(clip_id)
                    if active_session is not None:
                        active_session.memory_proxy_pending = False

        threading.Thread(
            target=_worker,
            name="FrameProviderProxyWarmup",
            daemon=True,
        ).start()

    def close_clip(self, clip_id: str) -> bool:
        """
        关闭视频剪辑。

        减少引用计数。当引用计数为 0 时，释放所有资源。

        Args:
            clip_id: 剪辑标识

        Returns:
            True 表示成功关闭

        Note:
            多个节点引用同一剪辑时，只有最后一个节点关闭才释放资源
        """
        with self._lock:
            session = self._clips.get(clip_id)
            if session is None:
                logger.warning(f"Clip not found: {clip_id}")
                return False

            # 减少引用计数
            ref_count = session.release_ref()

            if ref_count > 0:
                logger.info(
                    f"Clip ref decreased: {clip_id}, ref_count={ref_count}"
                )
                return True

            # 引用计数为 0，清理资源
            path = session.path
            self._clips.pop(clip_id)

            # 清理路径映射
            if path in self._path_to_clips:
                self._path_to_clips[path] = [
                    cid for cid in self._path_to_clips[path]
                    if cid != clip_id
                ]
                if not self._path_to_clips[path]:
                    self._path_to_clips.pop(path)

        # 在锁外关闭媒体源
        self._close_clip_sources(session)
        # AUD-39108: 并发自增持锁，防丢更新
        with self._stats_lock:
            self._total_clips_closed += 1

        logger.info(f"Clip closed and resources released: {clip_id}")
        return True

    def get_clip_info(self, clip_id: str) -> Optional[ClipInfo]:
        """
        获取剪辑信息。

        P1-1: 包含代理状态信息供 UI 显示。

        Args:
            clip_id: 剪辑标识

        Returns:
            ClipInfo 或 None（剪辑不存在）
        """
        with self._lock:
            session = self._clips.get(clip_id)
            if session is None:
                return None

        # P1-1: 使用 from_session() 包含代理状态
        return ClipInfo.from_session(session)

    def has_clip(self, clip_id: str) -> bool:
        """
        检查剪辑是否存在。

        Args:
            clip_id: 剪辑标识

        Returns:
            True 表示存在
        """
        with self._lock:
            return clip_id in self._clips

    # =============================================================================
    # 帧请求
    # =============================================================================

    def request_frame(
        self,
        clip_id: str,
        frame_index: int,
        quality: str = 'proxy',
        *,
        allow_master_fallback: bool = True,
    ) -> Optional[DecodedFrame]:
        """
        请求视频帧。

        从缓存或解码获取帧。支持 proxy 和 master 质量。

        Args:
            clip_id: 剪辑标识
            frame_index: 帧号（源视频帧号，0-based）
            quality: 质量级别 ('proxy', 'master', 'auto')
            allow_master_fallback: proxy 解码失败时是否允许自动回退到 master。
                实时播放热路径必须传 False（规格书 §6 / Stage C2），
                避免 proxy miss 时同步解母版拖死播放线程。paused/step/export
                可保持 True。

        Returns:
            DecodedFrame 或 None

        Note:
            S4.B: frame_index 是源视频帧号，不做 timeline offset/speed 转换。
            - 所有 request 输入都是源视频 frame_index，0-based
            - timeline frame 到 source frame 的转换只发生在 timeline/context 层
            - Color 直接请求源 frame，不走 timeline 转换

            Composite 预览用 proxy quality，最终导出用 master quality
            Color 页面和 Composite 页面请求同一帧时 frame_index 一致
        """
        start_time = time.perf_counter()
        quality_enum = FrameQuality.from_string(quality)

        with self._lock:
            session = self._clips.get(clip_id)
            if session is None:
                logger.warning(f"Clip not found: {clip_id}")
                return None

            if not session.is_open:
                logger.warning(f"Clip not open: {clip_id}")
                return None

            session.touch()

        # AUD-39101：AUTO 请求按实际服务源选缓存桶（代理源 → proxy 桶），
        # 防止 proxy 分辨率帧写入 master 缓存被后续 MASTER 请求命中。
        cache_quality = session.resolve_cache_quality(quality_enum)

        # 前台快路径：缓存命中不得排在后台预取解码之后。把命中检查收进
        # decode_lock 解决了并发同帧重复解码，但也让"本可立即命中"的
        # 前台请求被持锁中的预取解码阻塞（最坏等一整次 master 解码）。
        # get_cached_frame 是纯 dict.get（GIL 下原子），无锁读取安全；
        # 未命中仍进入下方持锁路径做合并解码。
        fast_cached = session.get_cached_frame(frame_index, cache_quality)
        if fast_cached is not None:
            # AUD-39108: 并发自增持锁，防丢更新
            with self._stats_lock:
                self._total_cache_hits += 1
                self._total_frames_served += 1
            session.record_request(
                quality_enum, cache_hit=True,
                decode_ms=(time.perf_counter() - start_time) * 1000)
            logger.debug(
                f"Frame cache hit: clip={clip_id}, frame={frame_index}, "
                f"quality={quality}"
            )
            return fast_cached

        with session.decode_waiters_lock:
            session.decode_waiters += 1
        try:
            # Serialize cache check + decode + cache insert for one clip.  The
            # source decoder already has to be serialized, but checking the cache
            # outside that lock allowed two consumers (for example Color and
            # Composite) to both miss the same frame and perform the same seek.
            # Rechecking while holding the decode lock coalesces those misses into
            # one decode without changing the public request semantics.
            with session.decode_lock:
                cached_frame = session.get_cached_frame(frame_index, cache_quality)
                if cached_frame is not None:
                    cache_hit = True
                    frame = cached_frame
                else:
                    cache_hit = False
                    # AUD-39108: 并发自增持锁，防丢更新
                    with self._stats_lock:
                        self._total_cache_misses += 1
                    frame = self._decode_frame(session, frame_index, quality_enum)

                    # Proxy 源不可用时（如 2K 素材无独立 proxy），自动 fallback 到 master。
                    # 实时播放热路径通过 allow_master_fallback=False 禁止此行为（规格书 Stage C2），
                    # 让上层进入 hold/placeholder/rebuffer，而不是同步解母版拖死播放。
                    if (
                        frame is None
                        and quality_enum == FrameQuality.PROXY
                        and allow_master_fallback
                    ):
                        frame = self._decode_frame(
                            session, frame_index, FrameQuality.MASTER)

                    if frame is not None:
                        # AUD-39101：按解析后的桶缓存。PROXY miss 回退
                        # master 解码的帧仍留在 proxy 桶（旧行为，避免
                        # 重复解码）；AUTO 才是本修复的桶错位来源。
                        session.cache_frame(frame_index, frame, cache_quality)
                        session.evict_old_frames(frame_index, cache_quality)
        finally:
            with session.decode_waiters_lock:
                session.decode_waiters -= 1

        if cache_hit:
            # AUD-39108: 并发自增持锁，防丢更新
            with self._stats_lock:
                self._total_cache_hits += 1
        if frame is not None:
            with self._stats_lock:
                self._total_frames_served += 1

            # 记录 per-clip 统计
            decode_ms = (time.perf_counter() - start_time) * 1000
            session.record_request(
                quality_enum, cache_hit=cache_hit, decode_ms=decode_ms)

            logger.debug(
                f"Frame {'cache hit' if cache_hit else 'decoded and cached'}: "
                f"clip={clip_id}, frame={frame_index}, quality={quality}"
            )

        return frame

    def has_vt_source(self, clip_id: str) -> bool:
        """该片段是否挂载了 macOS VT 硬解源。"""
        session = self._clips.get(clip_id)
        return session is not None and session.vt_source is not None

    def request_gpu_frame(
        self,
        clip_id: str,
        frame_index: int,
        quality: str = "proxy",
        *,
        interactive: bool = False,
        deadline_s: Optional[float] = None,
        is_stale: Optional[callable] = None,
        coarse: bool = False,
        allow_master_fallback: bool = True,
    ):
        """
        Request a GPU-resident frame when available.

        Priority:
        1. macOS VT IOSurface zero-copy (if the session attached a VT source)
        2. CPU decode + GPU upload fallback

        Args:
            interactive: True 时走 VT 交互式读取（拖拽/快梭）。coarse=True
                为关键帧快梭（回拖/大跳：seek 到目标前关键帧只解 1 帧）；
                False 为 deadline 顺序走（前向短跳：nearest±2 兜底）。
            deadline_s: 交互式解码预算（秒）。
            is_stale: 被更新目标取代时返回 True，中止当前交互式解码。
            allow_master_fallback: CPU 回退解码 proxy miss 时是否允许自动
                回退 master（AUD-39104：此前该开关不转发、恒为 True，
                播放热路径可被同步母版解码拖死）。实时播放热路径应显式
                传 False；paused/step 可保持默认 True。

        Returns:
            GpuDecodedFrame（VT 路径为"延迟 GL 导入"包装，渲染线程绑定外部纹理）
            或 DecodedFrame（CPU fallback）或 None

        Note:
            解码 worker 线程没有 GL 上下文，因此 VT 路径返回的是保留
            CVPixelBuffer 的延迟导入帧（planes 为空），由 AsyncRenderer /
            EditGpuPreviewRenderer 在渲染线程完成 IOSurface 绑定。
        """
        session = self._clips.get(clip_id)
        if session is None:
            return None

        vt_source = session.vt_source
        if vt_source is not None:
            with session.vt_lock:
                try:
                    if interactive:
                        import time as _time
                        deadline_at = (
                            _time.perf_counter() + deadline_s
                        ) if deadline_s else None
                        vt_frame = vt_source.read_frame_pixel_buffer_interactive(
                            frame_index,
                            is_stale=is_stale,
                            deadline_at=deadline_at,
                            coarse=coarse,
                        )
                        gpu_frame = (
                            vt_source.wrap_pixel_buffer_deferred(vt_frame)
                            if vt_frame is not None else None
                        )
                    else:
                        gpu_frame = vt_source.read_frame_gpu_deferred(frame_index)
                except Exception as e:
                    logger.warning(
                        f"[GPU-FRAME] VT read failed clip={clip_id} "
                        f"frame={frame_index}: {e}"
                    )
                    gpu_frame = None
            if gpu_frame is not None:
                logger.debug(
                    f"[GPU-FRAME] zero-copy clip={clip_id} frame={frame_index} "
                    f"path=macos_vt_iosurface_zero_copy(deferred)"
                )
                return gpu_frame

        # CPU fallback
        # AUD-39104：转发 allow_master_fallback。仅在显式 False 时传递
        # （True 即 request_frame 默认值），保持既有调用的参数形状不变。
        fallback_kwargs: Dict[str, Any] = {}
        if not allow_master_fallback:
            fallback_kwargs['allow_master_fallback'] = False
        frame = self.request_frame(
            clip_id=clip_id,
            frame_index=frame_index,
            quality=quality,
            **fallback_kwargs,
        )
        if frame is not None:
            logger.debug(
                f"[GPU-FRAME] cpu-fallback clip={clip_id} frame={frame_index} "
                f"path=cpu"
            )
        return frame

    @staticmethod
    def _vt_source_downsamples_chroma(session: 'ClipSession') -> bool:
        """AUD-39105：VT 源对 ProRes（4:2:2）素材输出 p010（4:2:0）。

        _try_open_vt_source 为 ProRes 预览挂 VT 硬解时，色度从 4:2:2
        下采样到 4:2:0——预览可接受、导出不可接受（"export 恒走 CPU
        全质量"）。request_metal_frame 是导出路径，遇此素材必须绕开
        VT，落到全质量 CPU/原生 surface 路径。metadata 缺失时无法
        判定，维持既有 VT 优先行为。
        """
        metadata = session.metadata
        if metadata is None:
            return False
        codec = str(getattr(metadata, 'codec_name', '') or '').lower()
        pixel_format = str(getattr(metadata, 'pixel_format', '') or '').lower()
        if 'prores' in codec:
            return True
        return '422' in pixel_format

    def request_metal_frame(
        self,
        clip_id: str,
        frame_index: int,
        quality: str = "master",
    ):
        """Return a Metal-resident VideoToolbox or Topos frame when supported.

        This export-oriented path imports NV12/P010 CVPixelBuffer planes or
        Topos 4:2:2 IOSurface planes as MTLTextures. It deliberately returns
        the ordinary CPU DecodedFrame when the native decoder or its
        CoreVideo/Metal bridge is unavailable, so callers can preserve the
        established export fallback.

        AUD-39105: ProRes（4:2:2）素材的 VT 源输出 p010（4:2:0，色度
        下采样），不得进入导出——"export 恒走 CPU 全质量"（见
        _try_open_vt_source 的质量取舍注释）。此时绕开 VT，走 Topos
        原生 surface / CPU 全质量路径；4:2:0 素材（VT 无重采样）与
        metadata 缺失的会话维持 VT 优先。
        """
        session = self._clips.get(clip_id)
        if session is None:
            return None
        vt_source = session.vt_source
        if vt_source is not None and self._vt_source_downsamples_chroma(session):
            logger.info(
                "[GPU-FRAME] export Metal path skips chroma-downsampling VT "
                "source clip=%s frame=%s",
                clip_id, frame_index,
            )
            vt_source = None
        if vt_source is not None:
            with session.vt_lock:
                try:
                    gpu_frame = vt_source.read_frame_metal(int(frame_index))
                except Exception as error:
                    logger.info(
                        "Metal VideoToolbox frame unavailable clip=%s frame=%s: %s",
                        clip_id, frame_index, error,
                    )
                    gpu_frame = None
            if gpu_frame is not None:
                return gpu_frame

        # Topos has a native caller-owned surface ABI.  On macOS the source
        # can decode directly into IOSurface-backed R16Uint Y/U/V textures,
        # avoiding the ordinary NumPy planar staging upload used by
        # ``request_frame``.  Keep this export-oriented route behind the
        # source capability so unsupported streams fall back transparently.
        try:
            quality_enum = FrameQuality.from_string(quality)
            source = session.get_source_for_quality(quality_enum)
            read_metal = getattr(source, "read_frame_metal", None)
            if callable(read_metal):
                with session.decode_lock:
                    gpu_frame = read_metal(int(frame_index))
                if gpu_frame is not None:
                    logger.debug(
                        "[GPU-FRAME] Topos IOSurface/Metal frame clip=%s frame=%s",
                        clip_id, frame_index,
                    )
                    return gpu_frame
        except Exception as error:
            logger.info(
                "Topos Metal surface frame unavailable clip=%s frame=%s: %s",
                clip_id, frame_index, error,
            )
        return self.request_frame(
            clip_id=clip_id,
            frame_index=frame_index,
            quality=quality,
        )

    def prefetch_vt_sequential(self, clip_id: str, frame_index: int) -> bool:
        """VT 源顺序预取下一帧进 reorder 缓冲（播放前瞻流水线）。

        仅当 frame_index 恰好是 VT 游标的下一顺序帧时才解码（绝不 seek，
        绝不超前多于 1 帧），其它情况直接放弃。让下一次 request_gpu_frame
        的目标命中缓冲/推进游标，把播放中的间隙补齐解码摊到后台。

        Returns:
            是否成功入缓冲（无 VT 源/非顺序目标返回 False）。
        """
        with self._lock:
            session = self._clips.get(clip_id)
        if session is None or not session.is_open:
            return False
        vt_source = session.vt_source
        if vt_source is None:
            return False
        with session.vt_lock:
            try:
                return bool(vt_source.prefetch_sequential_frame(frame_index))
            except Exception as e:
                logger.debug(f"[GPU-FRAME] VT sequential prefetch failed: {e}")
                return False

    def prefetch(
        self,
        clip_id: str,
        start: int,
        end: int,
        quality: str = 'proxy'
    ) -> None:
        """
        预取帧范围。

        AUD-39111: 本方法在【调用线程同步】逐帧解码（非后台异步）——
        调用方必须自行安排在工作线程执行；从 UI/渲染线程直接调用会
        冻结该线程（最坏等整个范围的解码时间）。

        缓存增长控制：每解出一帧即在会话缓存插入后按当前帧滚动淘汰
        （evict_old_frames），预取期间缓存增量被 max_*cache 上界约束，
        不再等到整个范围结束后才一次性淘汰。

        Args:
            clip_id: 剪辑标识
            start: 开始帧号
            end: 结束帧号
            quality: 质量级别 ('proxy', 'master')
        """
        quality_enum = FrameQuality.from_string(quality)

        with self._lock:
            session = self._clips.get(clip_id)
            if session is None or not session.is_open:
                return

            session.touch()

        # AUD-39101：AUTO 预取同样按实际服务源选缓存桶
        cache_quality = session.resolve_cache_quality(quality_enum)

        # 确定需要预取的帧
        frames_to_prefetch = []
        for frame_idx in range(start, end + 1):
            if session.get_cached_frame(frame_idx, cache_quality) is None:
                frames_to_prefetch.append(frame_idx)

        if not frames_to_prefetch:
            return

        logger.debug(
            f"Prefetching {len(frames_to_prefetch)} frames: "
            f"clip={clip_id}, range=[{start}, {end}], quality={quality}"
        )

        # 顺序解码（避免 seek 开销）。每一帧在真正解码前再次持有会话
        # 锁并检查缓存：前台请求可能在预取扫描之后已经填入该帧，不能
        # 因为旧的 frames_to_prefetch 列表再次 seek/decode。
        for frame_idx in frames_to_prefetch:
            # 前台让路：有前台消费者正在等 decode_lock 时终止本轮扫描。
            # 进行中的那一次解码无法中断（有界：一次解码），但后续帧
            # 不再排队，前台最坏只等当前这一帧。
            with session.decode_waiters_lock:
                if session.decode_waiters > 0:
                    logger.debug(
                        "Prefetch yielding to foreground: clip=%s, at frame %d",
                        clip_id, frame_idx,
                    )
                    break
            with session.decode_lock:
                if session.get_cached_frame(frame_idx, cache_quality) is not None:
                    continue
                frame = self._decode_frame(session, frame_idx, quality_enum)
                if frame is not None:
                    session.cache_frame(frame_idx, frame, cache_quality)
                    # AUD-39111: 淘汰移入循环内（同一 decode_lock 下与
                    # cache_frame 互斥），按当前帧滚动淘汰——大范围预取
                    # 期间缓存不再无界增长到整个范围的大小。
                    session.evict_old_frames(frame_idx, cache_quality)

    # ------------------------------------------------------------------
    # M2: 视频降噪邻帧窗口接口
    # ------------------------------------------------------------------

    def request_frame_window(
        self,
        clip_id: str,
        center_frame: int,
        before: int,
        after: int,
        quality: str = 'master',
        generation_id: int = 0,
        *,
        allow_master_fallback: bool = True,
    ):
        """请求以 center_frame 为中心的邻帧窗口（时域降噪用）。

        逐帧走 request_frame（命中 ClipSession 缓存的帧零解码成本），
        失败/越界的帧记入 missing_frames 而不是中断——时域降噪在缺帧时
        退化为空域降噪，绝不能阻塞播放。

        Args:
            clip_id: 剪辑标识
            center_frame: 中心帧号（源视频 0-based）
            before / after: 前后邻帧数量
            quality: 'proxy' / 'master'
            generation_id: 调用方世代号（seek 后变化），原样带回 FrameWindow
        Returns:
            FrameWindow（见 src/shared/video/denoise/frame_types.py）
        """
        from src.shared.video.denoise.frame_types import FrameWindow

        before = max(0, int(before))
        after = max(0, int(after))

        window = FrameWindow(
            clip_id=clip_id,
            center_frame=center_frame,
            quality=quality,
            generation_id=generation_id,
        )

        with self._lock:
            session = self._clips.get(clip_id)
            total_frames = (
                session.metadata.total_frames
                if session is not None and session.metadata else 0
            )

        if session is None:
            window.missing_frames = [
                center_frame + d
                for d in range(-before, after + 1)
            ]
            return window

        # 前向（past）邻帧：按离中心由近到远
        for dist in range(1, before + 1):
            idx = center_frame - dist
            if idx < 0 or (total_frames > 0 and idx >= total_frames):
                window.missing_frames.append(idx)
                continue
            frame = self.request_frame(
                clip_id=clip_id,
                frame_index=idx,
                quality=quality,
                allow_master_fallback=allow_master_fallback,
            )
            if frame is not None:
                window.previous_frames.append(frame)
            else:
                window.missing_frames.append(idx)

        # 中心帧
        center = self.request_frame(
            clip_id=clip_id,
            frame_index=center_frame,
            quality=quality,
            allow_master_fallback=allow_master_fallback,
        )
        window.center_frame_data = center
        if center is None:
            window.missing_frames.append(center_frame)

        # 后向（future）邻帧
        for dist in range(1, after + 1):
            idx = center_frame + dist
            if idx < 0 or (total_frames > 0 and idx >= total_frames):
                window.missing_frames.append(idx)
                continue
            frame = self.request_frame(
                clip_id=clip_id,
                frame_index=idx,
                quality=quality,
                allow_master_fallback=allow_master_fallback,
            )
            if frame is not None:
                window.next_frames.append(frame)
            else:
                window.missing_frames.append(idx)

        return window

    # =============================================================================
    # 内部方法
    # =============================================================================

    def _generate_clip_id(self, path: str) -> str:
        """
        生成剪辑 ID。

        使用路径 hash + UUID 确保唯一性。

        Args:
            path: 文件路径

        Returns:
            剪辑 ID 字符串
        """
        path_hash = hashlib.md5(path.encode()).hexdigest()[:8]
        unique_id = uuid.uuid4().hex[:8]
        return f"clip_{path_hash}_{unique_id}"

    def _open_clip_sources(self, session: ClipSession) -> None:
        """
        打开剪辑的媒体源。

        创建 master_source 和 proxy_source。
        支持磁盘代理优先读取（专业 NLE 优化）。

        Args:
            session: 剪辑会话

        Raises:
            MediaOpenError: 打开失败

        Note:
            磁盘代理优先级：
            1. 如果 session.disk_proxy_info.is_ready，打开磁盘代理作为 proxy_source
            2. 否则，创建内存降级源作为 proxy_source
            3. master_source 总是从原始文件创建
        """
        from ..media.factory import MediaSourceFactory
        from ..media.proxy import (
            ProxyStatus,
            validate_disk_proxy,
            get_disk_proxy_rejection_reason,
        )

        path = session.path

        # 验证文件
        if not path or not os.path.isfile(path):
            from ..media.source import MediaOpenError
            session.open_error = f"File not found: {path}"
            raise MediaOpenError(session.open_error, source_path=path)

        # 创建 master 源
        master_source = MediaSourceFactory.create(
            path=path,
            backend=self._backend,
            target_size=None,  # master 不缩放
            auto_open=False
        )
        MediaSourceFactory.open_source(master_source)

        # 获取元数据
        metadata = master_source.probe()
        if metadata is None:
            master_source.close()
            from ..media.source import MediaOpenError
            session.open_error = "Failed to probe metadata"
            raise MediaOpenError(session.open_error, source_path=path)

        session.master_source = master_source
        session.metadata = metadata

        # ========================================
        # 磁盘代理优先读取（专业 NLE 优化）
        # ========================================
        disk_proxy_info = session.disk_proxy_info
        if disk_proxy_info is not None:
            # 验证磁盘代理状态
            disk_proxy_info = validate_disk_proxy(disk_proxy_info, path)
            session.disk_proxy_info = disk_proxy_info

            # 如果磁盘代理就绪，优先打开
            if disk_proxy_info.is_ready and disk_proxy_info.proxy_path is not None:
                proxy_path = disk_proxy_info.proxy_path
                if os.path.isfile(proxy_path):
                    try:
                        disk_proxy_source = MediaSourceFactory.create(
                            path=proxy_path,
                            backend=self._backend,
                            target_size=None,  # 磁盘代理已是降级分辨率
                            auto_open=False
                        )
                        MediaSourceFactory.open_source(disk_proxy_source)
                        proxy_metadata = disk_proxy_source.probe()
                        proxy_size = self._calculate_proxy_size_for_metadata(metadata)
                        rejection_reason = get_disk_proxy_rejection_reason(
                            metadata,
                            proxy_metadata,
                            proxy_size,
                        )
                        if rejection_reason:
                            logger.warning(
                                "Rejecting disk proxy for realtime playback: "
                                f"{rejection_reason}; clip={session.clip_id}"
                            )
                            disk_proxy_source.close()
                            session.disk_proxy_info = disk_proxy_info.with_status(
                                ProxyStatus.FAILED
                            )
                        else:
                            session.disk_proxy_source = disk_proxy_source
                            logger.info(
                                f"Disk proxy source opened: {proxy_path}, "
                                f"clip={session.clip_id}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to open disk proxy: {proxy_path}, "
                            f"fallback to memory proxy. Error: {e}"
                        )
                        # 打开失败，状态标记为失败
                        session.disk_proxy_info = disk_proxy_info.with_status(
                            ProxyStatus.FAILED
                        )

        # ========================================
        # 创建内存降级源（fallback）
        # ========================================
        # 如果磁盘代理未就绪或打开失败，创建内存降级源
        if session.disk_proxy_source is None:
            proxy_config = self._proxy_config
            if proxy_config is not None:
                proxy_size = self._calculate_proxy_size_for_metadata(metadata)

                if proxy_size != (metadata.width, metadata.height):
                    if session.defer_memory_proxy:
                        session.proxy_config = proxy_config
                        self._schedule_deferred_memory_proxy_source(session.clip_id)
                    else:
                        # proxy 源使用目标尺寸
                        proxy_source = MediaSourceFactory.create(
                            path=path,
                            backend=self._backend,
                            target_size=proxy_size,
                            preserve_planes=self._should_preserve_proxy_planes(metadata),
                            auto_open=False
                        )
                        MediaSourceFactory.open_source(proxy_source)
                        session.proxy_source = proxy_source
                        session.proxy_config = proxy_config

                        logger.debug(
                            f"Memory proxy source created: {path}, "
                            f"size={proxy_size[0]}x{proxy_size[1]}"
                        )

        session.is_open = True
        session.open_error = None

        # 附加 VT 零拷贝源（按硬件能力策略，失败静默回退 CPU）
        session.vt_source = self._try_open_vt_source(path, metadata)

        # 记录打开信息
        source_type = 'disk_proxy' if session.disk_proxy_source else (
            'memory_proxy' if session.proxy_source else 'master_only'
        )
        vt_attached = 'yes' if session.vt_source is not None else 'no'
        logger.info(
            f"Clip opened: {path} -> {session.clip_id}, "
            f"{metadata.width}x{metadata.height}, "
            f"{metadata.fps.to_fps():.2f}fps, "
            f"source_type={source_type}, vt_zero_copy={vt_attached}"
        )

    def _try_open_vt_source(self, path: str, metadata: Any) -> Optional[Any]:
        """按硬件能力策略创建 macOS VT 零拷贝源（失败返回 None，回退 CPU）。

        4:2:0 的 HEVC/H.264 走 VT：HEVC 实测 scrub 1.9-2.2x、顺序 1.7x；
        H.264 此前按"FFmpeg 软解已够快"保持 CPU，但共享此服务的合成页
        播放管线实测 4K H264 CPU planar 只有 ~15-19fps，而 VT 零拷贝
        顺序解码 175fps+（与剪辑页 EditGpuDecodeManager 同源策略）——
        8-bit H.264 在所有 Mac 上都有硬解，10-bit 仍遵循
        playback_should_prefer_ffmpeg 的硬件判定（无硬解的机器上 VT 软解
        远慢于 FFmpeg 帧线程，必须回退）。ProRes 4:2:2 等其它子采样/编码
        保持 CPU 母版，避免调色母版被色度重采样。
        """
        import sys
        if sys.platform != 'darwin':
            return None

        try:
            from src.shared.media.macos_vt.hardware_capability import (
                playback_should_prefer_ffmpeg,
            )
            from src.shared.media.ffmpeg_source import _infer_bit_depth
        except Exception:
            return None

        codec = str(getattr(metadata, 'codec_name', '') or '').lower()
        pix_fmt = str(getattr(metadata, 'pixel_format', '') or '').lower()
        is_prores = 'prores' in codec
        if not any(c in codec for c in ('h265', 'hevc', 'h264', 'avc')) and not is_prores:
            return None

        # ProRes 预览挂 VT 硬解（VT 会话原生支持 apcn/apch/ap4h 等 tag）：
        # 4K ProRes 422 HQ 实测 VT 18.3ms/帧（54.7fps）vs PyAV CPU 路径
        # 33-49ms/帧——播放从 ~15/s 提到满帧。质量取舍：VT 输出 p010
        # （4:2:0 10bit），预览色度从 4:2:2 下采样；export 恒走 CPU 4:2:2
        # planar 全质量路径（VideoFrameNode is_export 不请求 VT），导出
        # 零妥协。4:2:2 零拷贝（v216+packed422 桥接）为后续工作。
        if is_prores:
            if pix_fmt not in ('yuv422p10le', 'yuv422p', 'p422itlg', 'yuv422p16le'):
                return None
        elif pix_fmt not in ('yuv420p', 'nv12', 'yuv420p10le', 'p010le', 'p010'):
            return None

        width = int(getattr(metadata, 'width', 0) or 0)
        height = int(getattr(metadata, 'height', 0) or 0)
        bit_depth = int(getattr(metadata, 'bit_depth', 0) or 0) or _infer_bit_depth(pix_fmt)
        if playback_should_prefer_ffmpeg(codec, pix_fmt, path, width=width, height=height):
            logger.info(
                f"VT zero-copy skipped by hardware policy: {path}, "
                f"codec={codec}, pix_fmt={pix_fmt}, bit_depth={bit_depth}"
            )
            return None

        try:
            from src.shared.media.macos_vt.source import MacOSVtZeroCopySource

            vt_source = MacOSVtZeroCopySource()
            if vt_source.open(path):
                logger.info(
                    f"VT zero-copy source attached to clip session: {path}, "
                    f"codec={codec}, {width}x{height}, bit_depth={bit_depth}"
                )
                return vt_source
            reason = vt_source.fallback_reason or 'unknown'
            logger.info(f"VT zero-copy unavailable for {path}: {reason}, using CPU sources")
            vt_source.close()
        except Exception as e:
            logger.warning(f"VT zero-copy source open failed for {path}: {e}")
        return None

    def _calculate_proxy_size_for_metadata(self, metadata: Any) -> tuple[int, int]:
        """Calculate the in-memory proxy size from VideoMetadata and ProxyConfig."""
        from ..media.proxy import VIEWPORT_THRESHOLD, calculate_proxy_size

        proxy_config = self._proxy_config
        original_size = (int(metadata.width), int(metadata.height))
        viewport_size = proxy_config.max_resolution or VIEWPORT_THRESHOLD

        return calculate_proxy_size(
            original_size=original_size,
            viewport_size=viewport_size,
            strategy=proxy_config.strategy,
            custom_max=proxy_config.max_resolution,
            min_size=proxy_config.min_resolution,
        )

    def _should_preserve_proxy_planes(self, metadata: Any) -> bool:
        """Return True when proxy playback should keep YUV planes for GPU conversion."""
        pixel_format = (getattr(metadata, 'pixel_format', '') or '').lower()
        if not pixel_format:
            return False

        yuv_prefixes = ('yuv', 'yuva')
        yuv_formats = ('nv12', 'p010', 'p016', 'p210', 'p216')
        return pixel_format.startswith(yuv_prefixes) or pixel_format in yuv_formats

    def _close_clip_sources(self, session: ClipSession) -> None:
        """
        关闭剪辑的媒体源。

        释放所有资源（包括磁盘代理源）。

        Args:
            session: 剪辑会话

        Note:
            master/proxy/disk_proxy 源的 close 必须持 session.decode_lock，
            与 _decode_frame 的 read_frame 共享同一把锁协议：PyAV 解码器/
            容器不支持并发 close + read，锁外 close 会与在途解码构成
            use-after-close 竞态（C 层 use-after-free）。decode_lock 为
            RLock，与 request_frame/prefetch 的持锁调用路径重入安全。
        """
        # 关闭 CPU 源（master/proxy/disk_proxy）：持 decode_lock 与在途
        # 解码串行化；缓存的清理同样在锁内，与持锁的 cache_frame/
        # evict_old_frames 互斥。
        with session.decode_lock:
            # 关闭 master 源
            if session.master_source is not None:
                try:
                    session.master_source.close()
                except Exception as e:
                    logger.warning(f"Failed to close master source: {e}")
                session.master_source = None

            # 关闭 proxy 源（内存降级源）
            if session.proxy_source is not None:
                try:
                    session.proxy_source.close()
                except Exception as e:
                    logger.warning(f"Failed to close proxy source: {e}")
                session.proxy_source = None

            # 关闭磁盘代理源
            if session.disk_proxy_source is not None:
                try:
                    session.disk_proxy_source.close()
                except Exception as e:
                    logger.warning(f"Failed to close disk proxy source: {e}")
                session.disk_proxy_source = None

            # 清理缓存
            session.frame_cache_proxy.clear()
            session.frame_cache_master.clear()

        # 关闭 VT 零拷贝源（持 vt_lock，与 VT 读路径互斥）
        if session.vt_source is not None:
            try:
                with session.vt_lock:
                    session.vt_source.close()
            except Exception as e:
                logger.warning(f"Failed to close VT zero-copy source: {e}")
            session.vt_source = None

        session.is_open = False
        session.metadata = None
        # AUD-39106：标记会话已关闭——此后任何延迟打开的源（磁盘代理/
        # 内存代理回调）都不得再赋给该会话，只能立即释放。
        session.sources_closed = True

    def _decode_frame(
        self,
        session: ClipSession,
        frame_index: int,
        quality: FrameQuality
    ) -> Optional[DecodedFrame]:
        """
        解码帧。

        从 MediaSource 获取帧。
        使用 ClipSession.get_source_for_quality 选择合适的源。

        Args:
            session: 剪辑会话
            frame_index: 帧号
            quality: 质量级别

        Returns:
            DecodedFrame 或 None

        Note:
            专业 NLE 源优先级：
            - MASTER: master_source（高质量）
            - PROXY: disk_proxy_source（优先）> proxy_source > master_source
        """
        # Clamp 帧号
        total_frames = session.metadata.total_frames if session.metadata else 0
        if total_frames > 0:
            frame_index = max(0, min(frame_index, total_frames - 1))

        try:
            # PyAV/FFmpeg 解码器不支持并发 seek/read；master worker、proxy
            # worker 与合成渲染线程可能同时请求同一会话，必须串行化。
            # 源的选择也必须在锁内完成：close 路径（_close_clip_sources、
            # update_disk_proxy_info）持同一把锁摘引用并释放源，锁外取
            # 引用会在 close 之后 read 已释放的解码器（use-after-close）。
            with session.decode_lock:
                source = session.get_source_for_quality(quality)
                if source is None:
                    return None
                frame = source.read_frame(frame_index)
            # 标记帧是否为 proxy
            if frame is not None:
                # 判断是否使用了代理源
                is_from_disk_proxy = (source == session.disk_proxy_source)
                is_from_memory_proxy = (source == session.proxy_source)
                frame.is_proxy = is_from_disk_proxy or is_from_memory_proxy

                # 如果来自磁盘代理，标记分辨率信息
                if is_from_disk_proxy and session.disk_proxy_info is not None:
                    # 磁盘代理帧的分辨率信息
                    if hasattr(frame, 'proxy_resolution'):
                        object.__setattr__(
                            frame, 'proxy_resolution',
                            session.disk_proxy_info.proxy_resolution
                        )

            return frame

        except Exception as e:
            logger.warning(
                f"Frame decode error: clip={session.clip_id}, "
                f"frame={frame_index}, quality={quality.name}, error={e}"
            )
            return None

    # =============================================================================
    # 统计与诊断
    # =============================================================================

    def get_stats(self) -> Dict[str, int | float]:
        """
        获取服务统计信息。

        Returns:
            统计信息字典（包含全局统计和 per-clip 统计）
        """
        with self._lock:
            active_clips = len(self._clips)
            total_refs = sum(s.ref_count for s in self._clips.values())

            # 收集 per-clip 统计
            clip_stats = []
            for session in self._clips.values():
                clip_stats.append(session.get_stats())

        # AUD-39108: 全局计数值同一把锁下快照（cache_hit_rate 内部会再取
        # _stats_lock，与这里错开避免语义混乱——先取计数快照再算比率）。
        with self._stats_lock:
            snap_opened = self._total_clips_opened
            snap_closed = self._total_clips_closed
            snap_served = self._total_frames_served
            snap_hits = self._total_cache_hits
            snap_misses = self._total_cache_misses

        global_stats = {
            'active_clip_count': active_clips,
            'total_reference_count': total_refs,
            'total_clips_opened': snap_opened,
            'total_clips_closed': snap_closed,
            'total_frames_served': snap_served,
            'total_cache_hits': snap_hits,
            'total_cache_misses': snap_misses,
            'cache_hit_rate': (
                snap_hits / (snap_hits + snap_misses)
                if (snap_hits + snap_misses) else 0.0
            ),
        }

        return {
            'global': global_stats,
            'clips': clip_stats,
        }

    def get_shared_clips_info(self) -> Dict[str, List[str]]:
        """
        获取共享剪辑信息。

        Returns:
            path -> [clip_ids] 映射，显示哪些路径被多个 clip_id 引用
        """
        with self._lock:
            return dict(self._path_to_clips)

    def get_clip_session(self, clip_id: str) -> Optional[ClipSession]:
        """
        获取剪辑会话（用于诊断）。

        Args:
            clip_id: 剪辑标识

        Returns:
            ClipSession 或 None
        """
        with self._lock:
            return self._clips.get(clip_id)

    # =============================================================================
    # P1-1: Disk Proxy 回调处理
    # =============================================================================

    def _on_proxy_complete(
        self,
        source_path: str,
        disk_proxy_info: DiskProxyInfo,
    ) -> None:
        """
        处理代理生成完成回调。

        当 DiskProxyService 完成代理生成时，更新相关剪辑的状态。

        Args:
            source_path: 源文件路径
            disk_proxy_info: 新的代理信息

        Note:
            - AUD-39107：此前本回调在持有 self._lock 的情况下执行
              MediaSourceFactory.create/open（阻塞 I/O），会卡住所有
              并发的 open_clip/request_frame；且跳过磁盘代理校验、
              不关闭被替换的内存代理源。现在按剪辑委托给
              update_disk_proxy_info——那里已把打开/关闭等重活放在
              self._lock 外，并带 isfile/元数据校验、内存代理释放与
              AUD-39106 的死会话防护。锁内只做路径 → clip_ids 快照。
            - 触发状态变化通知（供 UI 更新）
        """
        with self._lock:
            # 查找所有与此源文件相关的剪辑（快照后立即放锁）
            clip_ids = list(self._path_to_clips.get(source_path, []))
        if not clip_ids:
            logger.debug(
                f"No open clips for proxy completion: {source_path}"
            )
            return

        for clip_id in clip_ids:
            self.update_disk_proxy_info(clip_id, disk_proxy_info)
            with self._lock:
                session = self._clips.get(clip_id)
                if session is None:
                    continue
                # 记录新状态
                proxy_state = session.get_proxy_state()
            logger.info(
                f"Proxy state updated for {clip_id}: "
                f"{proxy_state.display_name}"
            )

    def _on_proxy_progress(
        self,
        source_path: str,
        progress: 'ProxyProgress',
    ) -> None:
        """
        处理代理生成进度回调。

        更新相关剪辑的生成进度（供 UI 显示）。

        Args:
            source_path: 源文件路径
            progress: 进度信息
        """

        with self._lock:
            # 查找所有与此源文件相关的剪辑
            clip_ids = self._path_to_clips.get(source_path, [])
            if not clip_ids:
                return

            for clip_id in clip_ids:
                session = self._clips.get(clip_id)
                if session is None:
                    continue

                # 更新 disk_proxy_info 的进度
                if session.disk_proxy_info is not None:
                    session.disk_proxy_info = session.disk_proxy_info.with_progress(
                        progress.percent
                    )

                    logger.debug(
                        f"Proxy progress for {clip_id}: {progress.percent:.1f}%"
                    )

    def shutdown(self) -> None:
        """
        关闭所有剪辑（应用退出时）。

        强制释放所有资源，忽略引用计数。
        """
        with self._lock:
            clips_to_close = list(self._clips.values())
            self._clips.clear()
            self._path_to_clips.clear()

        for session in clips_to_close:
            self._close_clip_sources(session)

        logger.info(
            f"Service shutdown: {len(clips_to_close)} clips closed, "
            f"{self._total_frames_served} frames served total"
        )


# =============================================================================
# 全局服务实例
# =============================================================================

# 全局服务实例（单例模式）
_GLOBAL_SERVICE: Optional[VideoFrameProviderService] = None
_SERVICE_LOCK: threading.Lock = threading.Lock()


def get_frame_provider_service() -> VideoFrameProviderService:
    """
    获取全局帧提供服务实例。

    Returns:
        VideoFrameProviderService 实例
    """
    global _GLOBAL_SERVICE

    with _SERVICE_LOCK:
        if _GLOBAL_SERVICE is None:
            _GLOBAL_SERVICE = VideoFrameProviderService()
        return _GLOBAL_SERVICE


def reset_frame_provider_service() -> None:
    """
    重置全局帧提供服务。

    用于测试或应用重置。
    """
    global _GLOBAL_SERVICE

    with _SERVICE_LOCK:
        if _GLOBAL_SERVICE is not None:
            _GLOBAL_SERVICE.shutdown()
            _GLOBAL_SERVICE = None
