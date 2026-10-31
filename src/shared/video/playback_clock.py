"""
Playback Clock Module
播放时钟和媒体源管理，用于帧调度和双路径解码。

## 职责

- SourceManager: Master/Proxy 源分离管理
- PlaybackClock: 基于 PTS/fps 的帧调度器
- FrameScheduler: 帧预读调度器

## 设计原则

- Master 源保真解码（暂停/逐帧/导出）
- Proxy 源降级解码（实时播放）
- PlaybackClock 精确控制帧节奏
- 允许 drop preview frame，不允许错 master frame
- 完整类型注解

## 使用示例

```python
from src.shared.video.playback_clock import SourceManager, PlaybackClock

# 创建源管理器
manager = SourceManager('/path/to/video.mp4')
manager.open()

# 获取高质量帧（暂停时）
master_frame = manager.get_master_frame(100)

# 获取代理帧（播放时）
proxy_frame = manager.get_proxy_frame(100)

# 创建播放时钟
clock = PlaybackClock(fps=24.0, total_frames=2400)
clock.start()

# 时钟推进
frame_to_display = clock.tick(dt=0.041)  # 24fps 一帧间隔
```

## 架构说明

SourceManager 维护两个独立的媒体源：
1. master_source: 使用 DecodeTarget.MASTER 策略，保留原始质量
2. proxy_source: 使用 DecodeTarget.PREVIEW 策略，降级分辨率和位深

PlaybackClock 基于 wall clock 和 PTS 计算当前应该显示的帧：
- 使用 clock() 获取当前时间
- 根据 fps 计算帧号
- 落后时跳帧追赶
- 记录 dropped_frames 统计
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple, Any, Callable

from src.shared.media.decode_policy import (
    DecodePolicy,
    DecodePolicyManager,
    DecodeTarget,
    HardwareDecodeStatus,
)
from src.shared.media.proxy import (
    ProxyConfig,
    calculate_proxy_size,
    DEFAULT_PROXY_CONFIG,
    DiskProxyInfo,
    ProxyStatus,
    validate_disk_proxy,
    get_disk_proxy_rejection_reason,
    VIEWPORT_THRESHOLD,
)
from src.shared.media.cache import DualFrameCache
from src.shared.media.metadata import Rational
from src.shared.media.frame import DecodedFrame

# P0.A: 导入性能监控器
from src.shared.media.performance_monitor import (
    VideoPerformanceMonitor,
    get_performance_monitor,
)

logger = logging.getLogger(__name__)


# =============================================================================
# SourceManager: Master/Proxy 源分离
# =============================================================================

class SourceManager:
    """
    媒体源管理器。

    管理 master_source 和 proxy_source 的分离：
    - master_source: 保真解码，用于暂停/逐帧/导出
    - proxy_source: 降级解码，用于实时播放（内存降级）
    - disk_proxy_source: 磁盘代理源（专业 NLE 优化，优先使用）

    Attributes:
        _path: 媒体文件路径
        _master_source: 高质量媒体源
        _proxy_source: 代理媒体源（内存降级）
        _disk_proxy_source: 磁盘代理媒体源
        _disk_proxy_info: 磁盘代理信息（状态、路径等）
        _proxy_config: 代理配置
        _cache: 双缓存容器
        _metadata: 视频元数据
    """

    def __init__(
        self,
        path: str,
        proxy_config: Optional[ProxyConfig] = None,
        viewport_size: Optional[Tuple[int, int]] = None,
        source_factory: Optional[Callable[..., Any]] = None,
        enable_planar_proxy: bool = True,  # v3: proxy 源默认启用 planar 输出
        enable_planar_master: bool = True,  # v3.1: master 源 YUV 素材 planar 输出
        disk_proxy_path: Optional[str] = None,  # 磁盘代理文件路径
        disk_proxy_info: Optional[DiskProxyInfo] = None,  # 磁盘代理信息
        preferred_backend: Optional[str] = None,  # P0-2: 偏好的后端（替代自定义工厂）
    ) -> None:
        """
        初始化源管理器。

        Args:
            path: 媒体文件路径
            proxy_config: 代理配置
            viewport_size: 视口大小（用于自动计算代理分辨率）
            source_factory: 媒体源工厂函数（可选，用于自定义源创建）
                            P3.B: 工厂函数应支持 target_size 参数，
                            但为兼容旧代码，使用 Callable[..., Any] 签名。
                            P0-2: 推荐使用 preferred_backend 参数替代自定义工厂，
                            避免参数丢失问题。
            enable_planar_proxy: 是否为 proxy 源启用 planar 输出（避免 CPU RGB 转换）。
                                 v3 默认为 True，让 YUV 素材直接输出 planar DecodedFrame。
            enable_planar_master: 是否为 master 源启用 planar 输出（仅 YUV 素材生效）。
                                 v3.1 默认为 True。4K 10-bit ProRes/DNxHR 的
                                 swscale 转 rgb48le 是拖拽预览瓶颈（实测 4K ProRes
                                 33fps → 105fps），planar 保留 YUV planes 交由
                                 渲染器 YUVGPUUploader 做 GPU 色彩转换，与 proxy
                                 planar 路径一致（无 GPU 支持时渲染线程内 CPU 兜底）。
            disk_proxy_path: 磁盘代理文件路径（可选，专业 NLE 优化）
            disk_proxy_info: 磁盘代理信息（包含状态、分辨率等）
            preferred_backend: 偏好的后端（P0-2）。
                               如果指定，内部工厂会优先使用该后端。
                               支持 'ffmpeg', 'gstreamer'。
                               替代自定义 source_factory，避免参数丢失。
        """
        self._path: str = path
        self._proxy_config: ProxyConfig = proxy_config or DEFAULT_PROXY_CONFIG
        self._viewport_size: Optional[Tuple[int, int]] = viewport_size
        self._enable_planar_proxy: bool = enable_planar_proxy
        self._enable_planar_master: bool = enable_planar_master

        # 媒体源
        self._master_source: Optional[Any] = None  # MediaSource
        self._proxy_source: Optional[Any] = None  # MediaSource（内存降级）
        self._disk_proxy_source: Optional[Any] = None  # MediaSource（磁盘代理）

        # 磁盘代理信息
        self._disk_proxy_path: Optional[str] = disk_proxy_path
        self._disk_proxy_info: Optional[DiskProxyInfo] = disk_proxy_info

        # 解码策略
        self._master_policy: DecodePolicy = DecodePolicyManager.get_policy(
            DecodeTarget.MASTER
        )
        self._proxy_policy: DecodePolicy = DecodePolicyManager.get_policy(
            DecodeTarget.PREVIEW
        )

        # 双缓存
        self._cache: DualFrameCache = DualFrameCache()

        # 元数据
        self._metadata: Optional[Any] = None  # VideoMetadata

        # 状态
        self._is_open: bool = False
        self._proxy_size: Optional[Tuple[int, int]] = None

        # 帧号跟踪
        # P1.A: 使用 -1 表示"还没读过任何帧"，避免第 1 帧触发 seek
        self._master_current_frame: int = -1
        self._proxy_current_frame: int = -1

        # A "no proxy needed" clip deliberately shares one MediaSource between
        # the master and proxy paths.  PyAV/FFmpeg decoder contexts are not
        # safe for concurrent seek/read calls, so every source operation must
        # be serialized even when requests arrive from separate workers.
        self._decode_lock = threading.RLock()        # 源工厂函数
        self._source_factory: Optional[Callable] = source_factory

        # P0-2: 偏好后端（替代自定义工厂）
        self._preferred_backend: Optional[str] = preferred_backend

        # P0.A: 性能监控器
        self._perf_monitor: VideoPerformanceMonitor = get_performance_monitor()

        # P9.A: 硬件解码状态（用于 proxy 源）
        self._hwaccel_status: Optional[HardwareDecodeStatus] = None

    # ========================================
    # AUD-39306: 共享源（无需代理）时的统一游标记账
    #
    # _should_create_proxy_source() 为 False 时 _proxy_source 与
    # _master_source 是同一个 MediaSource 对象（open() 中
    # `self._proxy_source = self._master_source`）。此前 master/proxy
    # 双游标各自记账：master worker 读到第 10 帧只推进
    # _master_current_frame，proxy worker 的顺序播放免 seek 优化仍按
    # _proxy_current_frame 判定"下一帧"，跳过 seek 后 read_next_frame()
    # 实际读到的是解码器当前位置的下一帧（可能是第 11 帧），却被当作
    # 请求的第 15 帧写入 proxy 缓存——读到并缓存错误的帧。共享源期间
    # 两个游标必须同步推进。
    # ========================================

    @property
    def _shares_master_proxy_source(self) -> bool:
        """proxy 路径与 master 路径是否共享同一 MediaSource 对象。"""
        return (
            self._proxy_source is not None
            and self._master_source is not None
            and self._proxy_source is self._master_source
        )

    def _record_master_position(self, frame_index: int) -> None:
        """记录 master 读取位置；共享源时同步 proxy 游标（AUD-39306）。"""
        self._master_current_frame = frame_index
        if self._shares_master_proxy_source:
            self._proxy_current_frame = frame_index

    def _record_proxy_position(self, frame_index: int) -> None:
        """记录 proxy 读取位置；共享源时同步 master 游标（AUD-39306）。"""
        self._proxy_current_frame = frame_index
        if self._shares_master_proxy_source:
            self._master_current_frame = frame_index

    @property
    def path(self) -> str:
        """媒体源路径"""
        return self._path

    @property
    def is_open(self) -> bool:
        """是否已打开"""
        return self._is_open

    @property
    def metadata(self) -> Optional[Any]:
        """视频元数据"""
        return self._metadata

    @property
    def proxy_size(self) -> Optional[Tuple[int, int]]:
        """代理分辨率"""
        return self._proxy_size

    @property
    def original_size(self) -> Optional[Tuple[int, int]]:
        """原始分辨率"""
        if self._metadata:
            return (self._metadata.width, self._metadata.height)
        return None

    @property
    def fps(self) -> Optional[Rational]:
        """帧率"""
        if self._metadata:
            return self._metadata.fps
        return None

    @property
    def total_frames(self) -> Optional[int]:
        """总帧数"""
        if self._metadata:
            return self._metadata.total_frames
        return None

    @property
    def cache(self) -> DualFrameCache:
        """双缓存容器"""
        return self._cache

    def get_hwaccel_status(self) -> Optional[HardwareDecodeStatus]:
        """
        获取硬件解码状态。

        Returns:
            HardwareDecodeStatus 或 None（未打开时）

        Note:
            P9.A: 返回 proxy 源的硬件解码状态，包含是否启用硬解、
            使用哪种解码器、是否发生了 fallback 等信息。
        """
        return self._hwaccel_status

    def open(self) -> bool:
        """
        打开媒体源。

        创建 master_source、disk_proxy_source（优先）、proxy_source（fallback）。
        磁盘代理优先读取（专业 NLE 优化）。

        Returns:
            True 表示成功打开

        Note:
            专业 NLE 磁盘代理优先级：
            1. disk_proxy_source（如果磁盘代理就绪）
            2. proxy_source（内存降级源）
            3. master_source（无代理时）

            P3.A: MediaSourceFactory.create() 现在只创建不 open，
            SourceManager 在创建后显式调用 open()，避免重复 open 问题。
            P3.B: proxy_source 现在正确传递 target_size 进行降分辨率。
            工厂函数使用 Callable[..., Any] 签名，兼容不同的参数形式。
            P3.C: v3 proxy 源默认启用 planar 输出（preserve_planes=True），
            让 YUV 素材直接输出 planar DecodedFrame，避免 CPU RGB 转换。
        """
        if self._is_open:
            logger.warning("SourceManager already open")
            return True

        try:
            # P9.A: 导入工厂类
            from src.shared.media.factory import MediaSourceFactory, MediaBackend

            # P0-2: 判断是否使用内部默认工厂
            # 使用默认工厂的条件：
            # 1. 未提供自定义工厂（source_factory is None）
            # 2. 或者提供了 preferred_backend 参数（表示使用内置工厂的后端偏好）
            use_internal_factory = self._source_factory is None or self._preferred_backend is not None

            # 设置默认工厂（如果需要）
            # P3.B: 默认工厂支持 target_size 和 preserve_planes 参数
            if self._source_factory is None:
                # P0-2: 如果有 preferred_backend，使用它；否则使用 AUTO
                backend_value = self._preferred_backend or MediaBackend.AUTO.value
                # P3.C: 工厂函数现在支持 preserve_planes 参数
                self._source_factory = lambda p, target_size=None, preserve_planes=False: MediaSourceFactory.create(
                    p, backend=backend_value, target_size=target_size, preserve_planes=preserve_planes
                )

            # 创建 master 源（不降级，不启用 planar，不启用硬解）
            # P3.A: create() 只创建不 open，需要显式打开
            # P3.B: master 源不传 target_size，保持原始分辨率
            # P3.C: master 源不启用 planar（保持 RGB packed 输出）
            # P9.A: MASTER 策略不启用硬解（保证质量）
            # 使用 try/except 处理不同签名的 factory
            try:
                self._master_source = self._source_factory(
                    self._path, target_size=None, preserve_planes=False
                )
            except TypeError:
                # 旧签名不支持 target_size/preserve_planes，只用 path
                self._master_source = self._source_factory(self._path)
            self._master_source.open()

            # 探测元数据
            self._metadata = self._master_source.probe()

            # v3.1: master 源对 YUV 素材启用 planar 输出（set_preserve_planes
            # 内部按源像素格式判定，非 YUV 素材保持 packed RGB 不变）。
            if (
                self._enable_planar_master
                and hasattr(self._master_source, 'set_preserve_planes')
            ):
                try:
                    self._master_source.set_preserve_planes(True)
                except Exception as e:
                    logger.warning(f"Failed to enable planar master output: {e}")

            # 计算代理分辨率
            self._calculate_proxy_size()

            # ========================================
            # 磁盘代理优先读取（专业 NLE 优化）
            # ========================================
            # 验证磁盘代理状态
            if self._disk_proxy_info is not None:
                self._disk_proxy_info = validate_disk_proxy(
                    self._disk_proxy_info, self._path
                )

            # 如果磁盘代理就绪，优先打开
            disk_proxy_ready = False
            if self._disk_proxy_info is not None and self._disk_proxy_info.is_ready:
                proxy_path = self._disk_proxy_info.proxy_path
                if proxy_path and os.path.isfile(proxy_path):
                    try:
                        # 打开磁盘代理文件
                        try:
                            self._disk_proxy_source = self._source_factory(
                                proxy_path, target_size=None, preserve_planes=False
                            )
                        except TypeError:
                            self._disk_proxy_source = self._source_factory(proxy_path)
                        self._disk_proxy_source.open()
                        proxy_metadata = self._disk_proxy_source.probe()
                        rejection_reason = get_disk_proxy_rejection_reason(
                            self._metadata,
                            proxy_metadata,
                            self._proxy_size,
                        )
                        if rejection_reason:
                            logger.warning(
                                "Rejecting disk proxy for realtime playback: "
                                f"{rejection_reason}; proxy={proxy_path}"
                            )
                            self._disk_proxy_source.close()
                            self._disk_proxy_source = None
                            self._disk_proxy_info = self._disk_proxy_info.with_status(
                                ProxyStatus.FAILED
                            )
                        else:
                            disk_proxy_ready = True
                            logger.info(
                                f"Disk proxy source opened: {proxy_path}, "
                                f"original={self._path}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to open disk proxy: {proxy_path}, "
                            f"fallback to memory proxy. Error: {e}"
                        )
                        self._disk_proxy_source = None

            # ========================================
            # 创建内存降级源（fallback）
            # ========================================
            # 如果磁盘代理未就绪或打开失败，创建内存降级源
            if not disk_proxy_ready:
                # 创建 proxy 源（如果需要降级）
                # P3.C: v3 proxy 源默认启用 planar 输出
                is_yuv_source = self._metadata.pixel_format.startswith('yuv') or 'yuva' in self._metadata.pixel_format.lower()
                should_enable_planar = self._enable_planar_proxy and is_yuv_source

                if self._should_create_proxy_source():
                    # VideoToolbox's current download path ignores target_size
                    # and emits full-resolution packed frames. A scaled preview
                    # must instead use the regular source, which honors both
                    # target_size and planar output for the GPU YUV uploader.
                    if use_internal_factory:
                        backend_for_hwaccel = self._preferred_backend or MediaBackend.FFMPEG.value
                        if self._proxy_size != (self._metadata.width, self._metadata.height):
                            # 缩放代理走 AUTO 内容路由：Topos 私有编码经
                            # is_topos_movie → topos 后端（尊重 target_size 的
                            # box 缩小）；其余格式 auto 顺序本就解析为 ffmpeg，
                            # 行为不变。此前硬编码 FFMPEG 会让任何大于视口的
                            # Topos 素材在 open() 阶段整体失败。
                            self._proxy_source = MediaSourceFactory.create(
                                self._path,
                                backend=self._preferred_backend or MediaBackend.AUTO.value,
                                target_size=self._proxy_size,
                                preserve_planes=should_enable_planar,
                                auto_open=False,
                            )
                            self._proxy_source.open()
                            self._hwaccel_status = HardwareDecodeStatus(
                                hwaccel_enabled=False,
                                decoder_type='cpu',
                                fallback_occurred=True,
                                fallback_reason='scaled_preview_requires_target_size',
                                decode_path=(
                                    'planar_yuv' if should_enable_planar else 'cpu'
                                ),
                            )
                        else:
                            self._proxy_source, self._hwaccel_status = MediaSourceFactory.create_with_hwaccel(
                                self._path,
                                decode_target=DecodeTarget.PREVIEW,
                                backend=backend_for_hwaccel,
                                target_size=self._proxy_size,
                                preserve_planes=should_enable_planar,
                                auto_open=False,
                            )
                            self._proxy_source.open()
                    else:
                        # 用户提供了自定义工厂（且未指定 preferred_backend），使用它
                        # 注意：自定义工厂可能丢失参数，这是已知限制
                        try:
                            self._proxy_source = self._source_factory(
                                self._path,
                                target_size=self._proxy_size,
                                preserve_planes=should_enable_planar
                            )
                        except TypeError:
                            try:
                                self._proxy_source = self._source_factory(self._path, target_size=self._proxy_size)
                            except TypeError:
                                self._proxy_source = self._source_factory(self._path)
                            if should_enable_planar and hasattr(self._proxy_source, 'set_preserve_planes'):
                                self._proxy_source.set_preserve_planes(True)
                        self._proxy_source.open()
                        # 自定义工厂不提供硬解状态，创建默认 status
                        self._hwaccel_status = HardwareDecodeStatus(
                            hwaccel_enabled=False,
                            decoder_type='cpu',
                            fallback_reason='custom_source_factory'
                        )
                else:
                    # 视频不需要降级，使用同一个源
                    self._proxy_source = self._master_source
                    # 如果需要 planar 且源支持，手动启用
                    if should_enable_planar and hasattr(self._proxy_source, 'set_preserve_planes'):
                        self._proxy_source.set_preserve_planes(True)
                    # P9.A: 不降级时，使用 MASTER 源，硬解状态为 CPU
                    self._hwaccel_status = HardwareDecodeStatus(
                        hwaccel_enabled=False,
                        decoder_type='cpu',
                        fallback_reason='no_proxy_needed'
                    )

            self._is_open = True

            # 记录源类型
            source_type = 'disk_proxy' if disk_proxy_ready else (
                'memory_proxy' if self._proxy_source else 'master_only'
            )
            proxy_planar_mode = getattr(self._proxy_source, 'is_planar_mode', False) if self._proxy_source else False
            master_planar_mode = bool(getattr(self._master_source, 'is_planar_mode', False))
            hwaccel_info = f"hwaccel={self._hwaccel_status.decoder_type}" if self._hwaccel_status else "hwaccel=unknown"

            logger.info(
                f"SourceManager opened: {self._path}, "
                f"original={self.original_size}, "
                f"proxy={self._proxy_size}, "
                f"fps={self.fps}, "
                f"pix_fmt={self._metadata.pixel_format}, "
                f"source_type={source_type}, "
                f"proxy_planar={proxy_planar_mode}, "
                f"master_planar={master_planar_mode}, "
                f"{hwaccel_info}"
            )
            return True

        except Exception as e:
            logger.error(f"SourceManager open failed: {e}")
            self.close()
            return False

    def close(self) -> None:
        """关闭所有源（包括磁盘代理源）"""
        with self._decode_lock:
            master_source = self._master_source
            proxy_source = self._proxy_source
            if master_source is not None:
                try:
                    master_source.close()
                except Exception as e:
                    logger.warning(f"Master source close error: {e}")
                self._master_source = None

            # Compare against the retained object: clearing _master_source
            # first would otherwise close a shared decoder twice.
            if proxy_source is not None and proxy_source is not master_source:
                try:
                    proxy_source.close()
                except Exception as e:
                    logger.warning(f"Proxy source close error: {e}")
            self._proxy_source = None

            if self._disk_proxy_source is not None:
                try:
                    self._disk_proxy_source.close()
                except Exception as e:
                    logger.warning(f"Disk proxy source close error: {e}")
                self._disk_proxy_source = None

        self._cache.clear_all()
        self._is_open = False
        self._metadata = None

        logger.info("SourceManager closed")

    def get_master_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取高质量帧。

        用于暂停、逐帧、导出等需要完整质量的场景。

        Args:
            frame_index: 帧号（从 0 开始）

        Returns:
            DecodedFrame 或 None

        Note:
            此方法不允许出错，必须返回正确的帧
            P1.A: Master 源保持精确，不为性能牺牲准确性
            P2-1: 此方法是同步解码，播放状态下调用是 bug（除非缓存命中）
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        # 使用全局播放状态追踪器检测
        from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
        _assert_no_sync_decode_in_playback(
            operation="SourceManager.get_master_frame",
            frame_index=frame_index,
            source_type="master",
        )

        if not self._is_open or self._master_source is None:
            return None

        # P0.A: 记录开始时间
        start_time = time.perf_counter()

        # 检查缓存
        cached = self._cache.get_master(frame_index)
        if cached is not None:
            # P0.A: 缓存命中，记录
            self._perf_monitor.track_cache_hit(frame_index, True)
            self._perf_monitor.track_decode_time(frame_index, 0.0)  # 缓存命中，解码时间为 0
            # AUD-39306: 共享源时同步 proxy 游标
            self._record_master_position(frame_index)
            return cached

        # P0.A: 缓存未命中
        self._perf_monitor.track_cache_hit(frame_index, False)

        with self._decode_lock:
            try:
                # P1.A: Master 源保持精确，_master_current_frame == -1 时也需要 seek
                seek_start = time.perf_counter()
                if frame_index != self._master_current_frame:
                    self._master_source.seek_frame(frame_index)
                seek_end = time.perf_counter()
                self._perf_monitor.track_seek_time(frame_index, (seek_end - seek_start) * 1000)

                read_start = time.perf_counter()
                frame = self._master_source.read_frame(frame_index)
                read_end = time.perf_counter()
                self._perf_monitor.track_read_time(frame_index, (read_end - read_start) * 1000)
                self._perf_monitor.track_decode_time(frame_index, (time.perf_counter() - start_time) * 1000)

                # AUD-39306: 共享源时同步 proxy 游标
                self._record_master_position(frame_index)
                if frame is not None:
                    self._cache.put_master(frame_index, frame)
                return frame
            except Exception as e:
                logger.error(f"Master frame decode failed at {frame_index}: {e}")
                return None

    def get_proxy_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """
        获取代理帧。

        用于实时播放，允许快速降级解码。
        优先使用磁盘代理源（专业 NLE 优化）。

        Args:
            frame_index: 帧号（从 0 开始）

        Returns:
            DecodedFrame 或 None

        Note:
            专业 NLE 源优先级：
            1. disk_proxy_source（磁盘代理文件）
            2. proxy_source（内存降级源）
            3. master_source（fallback）

            P1.A: 顺序播放优化：
            - 如果 frame_index == _proxy_current_frame + 1，优先调用 read_next_frame()
            - 只有大跳跃、倒退时才 seek_frame()
            - _proxy_current_frame == -1 表示还没读过任何帧

            P2-1: 此方法是同步解码。播放 tick 应从 ReadyFrameQueue 取帧，
            不应直接调用此方法。仅用于 seek/pause/首帧等非播放状态场景。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
        _assert_no_sync_decode_in_playback(
            operation="SourceManager.get_proxy_frame",
            frame_index=frame_index,
            source_type="proxy",
        )

        if not self._is_open:
            return None

        # P0.A: 记录开始时间
        start_time = time.perf_counter()

        # 检查缓存
        cached = self._cache.get_proxy(frame_index)
        if cached is not None:
            # P0.A: 缓存命中，记录
            self._perf_monitor.track_cache_hit(frame_index, True)
            self._perf_monitor.track_decode_time(frame_index, 0.0)  # 缓存命中，解码时间为 0
            # AUD-39306: 共享源时同步 master 游标
            self._record_proxy_position(frame_index)
            return cached

        # P0.A: 缓存未命中
        self._perf_monitor.track_cache_hit(frame_index, False)

        # 选择活跃的代理源（磁盘代理优先）
        active_source = self._disk_proxy_source or self._proxy_source

        if active_source is None:
            # fallback 到 master_source
            active_source = self._master_source

        if active_source is None:
            return None

        with self._decode_lock:
            try:
                # P1.A: 顺序播放优化 - 判断是否为顺序下一帧
                # _proxy_current_frame == -1 时请求第 0 帧也是顺序播放
                # AUD-39306: 共享源（proxy==master）时解码器位置以 master
                # 游标为权威——proxy 自己的游标不知道 master worker 把
                # 解码器推进到了哪里，用它判定"顺序下一帧"会跳过必要的
                # seek，读到并缓存错误的帧。
                if active_source is self._master_source:
                    cursor = self._master_current_frame
                else:
                    cursor = self._proxy_current_frame
                is_sequential = frame_index == cursor + 1

                # P1.A: 记录 seek 时间（顺序播放时不 seek）
                seek_start = time.perf_counter()
                if is_sequential:
                    # 顺序下一帧，不需要 seek
                    seek_ms = 0.0
                else:
                    # 非顺序：倒退、跳跃等，需要 seek
                    active_source.seek_frame(frame_index)
                    seek_end = time.perf_counter()
                    seek_ms = (seek_end - seek_start) * 1000
                self._perf_monitor.track_seek_time(frame_index, seek_ms)

                # P1.A: 顺序播放使用 read_next_frame()，其他情况 seek 后也是 read_next_frame()
                read_start = time.perf_counter()
                frame = active_source.read_next_frame()
                read_end = time.perf_counter()
                self._perf_monitor.track_read_time(frame_index, (read_end - read_start) * 1000)
                self._perf_monitor.track_decode_time(frame_index, (time.perf_counter() - start_time) * 1000)

                if frame is not None:
                    is_from_disk_proxy = active_source == self._disk_proxy_source
                    is_from_memory_proxy = (
                        active_source == self._proxy_source
                        and active_source != self._master_source
                    )
                    object.__setattr__(frame, 'is_proxy', is_from_disk_proxy or is_from_memory_proxy)
                    if is_from_disk_proxy and self._disk_proxy_info is not None and hasattr(frame, 'proxy_resolution'):
                        object.__setattr__(frame, 'proxy_resolution', self._disk_proxy_info.proxy_resolution)

                # AUD-39306: 共享源时同步 master 游标（本帧把解码器推进到了 frame_index）
                self._record_proxy_position(frame_index)
                if frame is not None:
                    self._cache.put_proxy(frame_index, frame)
                return frame

            except Exception as e:
                logger.warning(f"Proxy frame decode failed at {frame_index}: {e}")
                return None

    def update_viewport_size(self, size: Tuple[int, int]) -> None:
        """
        更新视口大小。

        可能触发代理分辨率重新计算。

        Args:
            size: 视口大小 (width, height)
        """
        self._viewport_size = size
        self._calculate_proxy_size()

    def _calculate_proxy_size(self) -> None:
        """计算代理分辨率"""
        if self._metadata is None:
            return

        original_size = (self._metadata.width, self._metadata.height)
        # A missing UI viewport must not silently mean "full-resolution
        # playback". Background/legacy callers commonly initialize before a
        # QWidget has a size; keep their realtime path capped at 1080p.
        viewport_size = self._viewport_size or VIEWPORT_THRESHOLD

        self._proxy_size = calculate_proxy_size(
            original_size=original_size,
            viewport_size=viewport_size,
            strategy=self._proxy_config.strategy,
            custom_max=self._proxy_config.max_resolution,
            min_size=self._proxy_config.min_resolution
        )

        logger.debug(
            f"Proxy size calculated: original={original_size}, "
            f"viewport={viewport_size}, proxy={self._proxy_size}"
        )

    def _should_create_proxy_source(self) -> bool:
        """判断是否需要创建独立的 proxy 源"""
        if self._metadata is None or self._proxy_size is None:
            return False

        original_size = (self._metadata.width, self._metadata.height)

        # 代理分辨率与原始不同，需要独立源
        return self._proxy_size != original_size

    def _determine_decode_path(self) -> str:
        """
        确定实际使用的解码路径。

        Returns:
            decode_path 字符串：
            - 'hwaccel_download': 使用硬件解码
            - 'cpu_planar_yuv': planar 启用且成功（YUV 格式保持 planar）
            - 'cpu_packed_rgb': planar 禁用或失败（转换为 RGB packed）
        """
        # 使用硬解
        if self._hwaccel_status is not None and self._hwaccel_status.hwaccel_enabled:
            return 'hwaccel_download'

        # 检查 proxy 源是否启用了 planar 模式
        if self._proxy_source is not None:
            # 检查源是否实际处于 planar 模式
            if hasattr(self._proxy_source, 'is_planar_mode'):
                if self._proxy_source.is_planar_mode:
                    return 'cpu_planar_yuv'

        # 默认：CPU packed RGB
        return 'cpu_packed_rgb'

    def get_cache_stats(self) -> dict:
        """获取缓存统计"""
        return self._cache.get_combined_stats()

    def get_stats(self) -> dict:
        """
        获取完整统计信息（包含硬解状态）。

        Returns:
            统计字典，包含缓存信息和硬解状态

        Note:
            P9.A: 新增方法，用于 benchmark 输出硬解状态。
        """
        stats = self.get_cache_stats()

        # P9.A: 添加硬件解码状态
        if self._hwaccel_status is not None:
            stats['hwaccel'] = self._hwaccel_status.to_dict()
        else:
            stats['hwaccel'] = {
                'hwaccel_enabled': False,
                'decoder_type': None,
                'fallback_occurred': False,
                'fallback_reason': 'not_opened',
                'codec_supported': True,
            }

        # 添加基本信息
        stats['path'] = self._path
        stats['is_open'] = self._is_open
        if self._metadata:
            stats['codec'] = self._metadata.codec_name
            stats['pixel_format'] = self._metadata.pixel_format
            stats['original_size'] = (self._metadata.width, self._metadata.height)
        stats['proxy_size'] = self._proxy_size

        # 添加 decode_path 字段，记录实际解码路径
        stats['decode_path'] = self._determine_decode_path()

        return stats

    def request_proxy_frame(self, frame_index: int) -> None:
        """
        异步请求代理帧（触发解码并缓存）。

        实现 FrameProviderProtocol 协议。

        Args:
            frame_index: 帧号（0-based）

        Note:
            SourceManager 没有独立的异步解码队列，
            此方法立即解码并存入缓存，供后续 get_proxy_frame 使用。
            如果帧已在缓存中，则跳过解码。
        """
        # 检查缓存，如果已缓存则跳过
        cached = self._cache.get_proxy(frame_index)
        if cached is not None:
            logger.debug(f"request_proxy_frame({frame_index}): already cached")
            return

        # 立即解码并存入缓存
        frame = self.get_proxy_frame(frame_index)
        if frame is not None:
            logger.debug(f"request_proxy_frame({frame_index}): decoded and cached")
        else:
            logger.warning(f"request_proxy_frame({frame_index}): decode failed")

    def request_master_frame(self, frame_index: int) -> None:
        """
        异步请求高质量帧（触发解码并缓存）。

        实现 FrameProviderProtocol 协议。

        Args:
            frame_index: 帧号（0-based）

        Note:
            SourceManager 没有独立的异步解码队列，
            此方法立即解码并存入缓存，供后续 get_master_frame 使用。
            如果帧已在缓存中，则跳过解码。
        """
        # 检查缓存，如果已缓存则跳过
        cached = self._cache.get_master(frame_index)
        if cached is not None:
            logger.debug(f"request_master_frame({frame_index}): already cached")
            return

        # 立即解码并存入缓存
        frame = self.get_master_frame(frame_index)
        if frame is not None:
            logger.debug(f"request_master_frame({frame_index}): decoded and cached")
        else:
            logger.warning(f"request_master_frame({frame_index}): decode failed")


# =============================================================================
# PlaybackClock: 基于时间戳的帧调度
# =============================================================================

@dataclass
class ClockState:
    """
    播放时钟状态。

    Attributes:
        clock_time: 时钟时间（秒）
        current_frame: 当前帧号
        last_frame_time: 上一帧时间戳
        dropped_frames: 累计丢帧数
        total_frames_displayed: 累计显示帧数
        is_playing: 是否正在播放
        speed: 播放速度倍数
    """

    clock_time: float = 0.0
    current_frame: int = 0
    last_frame_time: float = 0.0
    dropped_frames: int = 0
    total_frames_displayed: int = 0
    is_playing: bool = False
    speed: float = 1.0
    # V-P0-02: 速率锚点。ideal_frame = anchor_frame +
    # (clock_time - anchor_clock_time) * fps * speed。set_speed / seek / start
    # 都重置锚点，使"从中段改速"不会让目标帧瞬间跳变。锚点用 clock_time（与
    # tick 的 dt/wall-clock 累计方式一致），避免时间源混用导致漂移。
    anchor_clock_time: float = 0.0
    anchor_frame: float = 0.0


class PlaybackClock:
    """
    播放时钟。

    基于 wall clock、PTS、fps 控制帧调度。
    允许丢帧（preview），不允许错帧（master）。

    核心算法：
    1. 使用 time.time() 或 QElapsedTimer 作为 wall clock
    2. 根据 fps 计算帧号：frame = clock_time * fps
    3. 如果落后超过阈值，跳帧追赶
    4. 记录 dropped_frames 统计

    Attributes:
        _fps: 帧率（浮点数）
        _total_frames: 总帧数
        _state: 时钟状态
        _frame_duration: 一帧时长（秒）
        _drop_threshold: 丢帧阈值（落后多少帧触发跳帧）
        _max_drops_per_tick: 每次 tick 最大跳帧数
    """

    def __init__(
        self,
        fps: float,
        total_frames: int,
        drop_threshold: int = 2,
        max_drops_per_tick: int = 3
    ) -> None:
        """
        初始化播放时钟。

        Args:
            fps: 帧率（浮点数）
            total_frames: 总帧数（0/None 表示未知时长，不做上界 clamp）
            drop_threshold: 丢帧阈值（落后多少帧触发跳帧）
            max_drops_per_tick: 每次 tick 最大跳帧数
        """
        self._fps: float = fps
        # V-P0-09: 未知时长（0/None）不当作有限上界；结束只由明确 EOS 驱动。
        self._total_frames: Optional[int] = (
            int(total_frames) if total_frames and total_frames > 0 else None
        )
        self._state: ClockState = ClockState()
        self._frame_duration: float = 1.0 / fps if fps > 0 else 0.04
        self._drop_threshold: int = drop_threshold
        self._max_drops_per_tick: int = max_drops_per_tick

        # 时钟起点（用于计算 elapsed time）
        self._start_time: Optional[float] = None

    @property
    def fps(self) -> float:
        """帧率"""
        return self._fps

    @property
    def _safe_fps(self) -> float:
        """除法安全的帧率（AUD-39311）。

        fps<=0 时构造器已把 _frame_duration 回退为 0.04s（等效 25fps）；
        start()/seek_to_frame()/get_time_for_frame()/get_remaining_time()
        等处对 _fps 做除法，同样回退 25.0，避免 fps=0 除零崩溃，
        且与 _frame_duration 的隐含口径一致。
        """
        return self._fps if self._fps > 0 else 25.0

    @property
    def total_frames(self) -> int:
        """总帧数（未知时返回 0，保留 int 返回类型以兼容旧调用方）。"""
        return self._total_frames or 0

    @property
    def known_total_frames(self) -> Optional[int]:
        """V-P0-09: 有限总帧数；None 表示未知时长（无上界，EOS 驱动结束）。

        与 ``total_frames``（未知时返回 0）的区别：本属性显式区分
        "总帧数为 0"与"未知"，FrameScheduler 等调度方据此决定是否
        施加预读上界（AUD-39307）。
        """
        return self._total_frames

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        return self._state.current_frame

    @property
    def dropped_frames(self) -> int:
        """累计丢帧数"""
        return self._state.dropped_frames

    @property
    def is_playing(self) -> bool:
        """是否播放中"""
        return self._state.is_playing

    @property
    def speed(self) -> float:
        """播放速度"""
        return self._state.speed

    @property
    def frame_duration(self) -> float:
        """一帧时长（秒）"""
        return self._frame_duration / self._state.speed

    def _reset_rate_anchor(self) -> None:
        """V-P0-02: 以当前帧/时钟为基准重置速率锚点。

        锚点定义了"从这一刻起，ideal = anchor_frame + (clock_time -
        anchor_clock_time) * fps * speed"。set_speed / seek / start 都调用它，
        使改速/seek/resume 不依赖"把 speed 乘到已累计的 clock_time 上"——后者
        会让中段改速瞬间跳到错误帧。
        """
        self._state.anchor_clock_time = self._state.clock_time
        self._state.anchor_frame = float(self._state.current_frame)

    def start(self) -> None:
        """启动时钟"""
        self._start_time = time.time()
        self._state.is_playing = True
        # AUD-39311: fps=0 时回退安全帧率，避免除零
        self._state.clock_time = self._state.current_frame / self._safe_fps
        self._state.last_frame_time = self._start_time
        self._reset_rate_anchor()
        logger.debug(f"PlaybackClock started: fps={self._fps}")

    def stop(self) -> None:
        """停止时钟"""
        self._state.is_playing = False
        logger.debug(f"PlaybackClock stopped: dropped={self._state.dropped_frames}")

    def reset(self) -> None:
        """重置时钟"""
        self._state = ClockState()
        self._start_time = None

    def set_speed(self, speed: float) -> None:
        """
        设置播放速度。

        V-P0-02: 改速时必须重置速率锚点。此前 ideal_frame = clock_time * fps *
        speed 把新 speed 乘到"以旧 speed 累计的 clock_time"上，导致中段改速瞬间
        跳到错误帧（甚至倒退）。锚点重置后，ideal = anchor_frame + elapsed * fps
        * speed，改速只影响"此后"的推进速度，当前位置不变。

        Args:
            speed: 速度倍数（1.0 = 正常，2.0 = 两倍速）
        """
        self._state.speed = max(0.1, min(4.0, speed))
        # 以当前帧为锚点重置，使改速不引起位置跳变。
        self._reset_rate_anchor()

    def seek_to_frame(self, frame: int) -> None:
        """
        Seek 到指定帧。

        更新当前帧号，不改变时钟时间。

        Args:
            frame: 目标帧号
        """
        # V-P0-09: 未知时长（_total_frames is None）时只做下界保护，不 clamp 上界。
        if self._total_frames is not None:
            frame = max(0, min(frame, self._total_frames - 1))
        else:
            frame = max(0, frame)
        self._state.current_frame = frame

        # 重置时钟时间，以当前帧为起点
        # AUD-39311: fps=0 时回退安全帧率，避免除零
        self._state.clock_time = frame / self._safe_fps
        self._state.last_frame_time = time.time()
        # V-P0-02: seek 也重置速率锚点，避免改速/seek 后位置漂移。
        self._reset_rate_anchor()

        logger.debug(f"PlaybackClock seek to frame {frame}")

    def seek_to_time(self, seconds: float) -> None:
        """
        Seek 到指定时间。

        Args:
            seconds: 目标时间（秒）
        """
        frame = int(seconds * self._fps)
        self.seek_to_frame(frame)

    def tick(self, dt: Optional[float] = None) -> int:
        """
        时钟推进，返回应该显示的帧号。

        这是帧调度器的核心方法。

        Args:
            dt: 时间增量（秒），为 None 时使用 wall clock

        Returns:
            应该显示的帧号

        算法：
        1. 计算 elapsed time（dt 或 wall clock）
        2. 根据 fps 计算理想帧号
        3. 检查落后程度
        4. 如果落后超过阈值，跳帧追赶
        5. 返回最终帧号
        """
        if not self._state.is_playing:
            return self._state.current_frame

        # 计算 elapsed time
        if dt is None:
            current_time = time.time()
            elapsed = current_time - self._state.last_frame_time
        else:
            elapsed = dt

        # 更新时钟时间（保留用于诊断/进度，不再直接决定 ideal_frame）
        self._state.clock_time += elapsed
        self._state.last_frame_time = time.time()

        # V-P0-02: 用速率锚点计算理想帧号：
        #   ideal = anchor_frame + (clock_time - anchor_clock_time) * fps * speed
        # 这样改速只影响"此后"的推进，当前位置不跳变（不再把新 speed 乘到
        # 以旧 speed 累计的 clock_time 上）。clock_time 与锚点用同一时间源（dt 或
        # wall-clock 累计），避免混用导致漂移。
        anchor_elapsed = max(0.0, self._state.clock_time - self._state.anchor_clock_time)
        ideal_frame = int(
            self._state.anchor_frame
            + anchor_elapsed * self._fps * self._state.speed
        )
        # V-P0-09: 未知时长不 clamp 上界。
        if self._total_frames is not None:
            ideal_frame = min(ideal_frame, self._total_frames - 1)

        # 检查落后程度
        lag = ideal_frame - self._state.current_frame

        if lag <= 0:
            # 已经超前或刚好，不跳帧
            # 可能是因为 seek 或速度变化
            if ideal_frame > self._state.current_frame:
                self._state.current_frame = ideal_frame
            return self._state.current_frame

        if lag <= 1:
            # 正常情况，前进一帧
            self._state.current_frame += 1
            self._state.total_frames_displayed += 1
            return self._state.current_frame

        # 落后超过 1 帧，判断是否跳帧
        if lag >= self._drop_threshold:
            # 落后超过阈值，跳帧追赶
            drops = min(lag - 1, self._max_drops_per_tick)
            self._state.current_frame += drops + 1
            self._state.dropped_frames += drops
            self._state.total_frames_displayed += 1

            logger.debug(
                f"Frame drop: lag={lag}, dropped={drops}, "
                f"frame={self._state.current_frame}"
            )
            return self._state.current_frame

        # 落后但未达阈值，前进一帧
        self._state.current_frame += 1
        self._state.total_frames_displayed += 1
        return self._state.current_frame

    def get_frame_for_time(self, time_seconds: float) -> int:
        """
        计算指定时间对应的帧号。

        Args:
            time_seconds: 时间（秒）

        Returns:
            帧号
        """
        frame = int(time_seconds * self._fps)
        # V-P0-09: 未知时长不 clamp 上界。
        if self._total_frames is not None:
            return max(0, min(frame, self._total_frames - 1))
        return max(0, frame)

    def get_time_for_frame(self, frame: int) -> float:
        """
        计算指定帧对应的时间。

        Args:
            frame: 帧号

        Returns:
            时间（秒）
        """
        # AUD-39311: fps=0 时回退安全帧率，避免除零
        return frame / self._safe_fps

    def get_state(self) -> ClockState:
        """获取时钟状态"""
        return self._state

    def get_progress(self) -> float:
        """
        获取播放进度百分比。

        Returns:
            0.0 - 1.0
        """
        # V-P0-09: _total_frames is None 表示未知时长，无法计算进度。
        if not self._total_frames or self._total_frames <= 1:
            return 0.0
        return self._state.current_frame / (self._total_frames - 1)

    def get_remaining_time(self) -> float:
        """
        获取剩余时间。

        Returns:
            剩余秒数
        """
        # V-P0-09: 未知时长时无法计算剩余时间，返回 0.0。
        if not self._total_frames:
            return 0.0
        remaining_frames = self._total_frames - self._state.current_frame
        # AUD-39311: fps=0 时回退安全帧率，避免除零
        return max(0.0, remaining_frames) / self._safe_fps


# =============================================================================
# FrameScheduler: 帧预读调度
# =============================================================================

class FrameScheduler:
    """
    帧预读调度器。

    根据播放时钟和缓存状态，调度帧预读。

    Attributes:
        _clock: 播放时钟
        _source_manager: 源管理器
        _prefetch_lookahead: 预读前瞻帧数
        _last_scheduled_frame: 上次调度帧号
    """

    def __init__(
        self,
        clock: PlaybackClock,
        source_manager: SourceManager,
        prefetch_lookahead: int = 8
    ) -> None:
        """
        初始化帧调度器。

        Args:
            clock: 播放时钟
            source_manager: 源管理器
            prefetch_lookahead: 预读前瞻帧数
        """
        self._clock = clock
        self._source_manager = source_manager
        self._prefetch_lookahead = prefetch_lookahead
        self._last_scheduled_frame: int = -1

    def get_next_frame(self) -> int:
        """
        获取下一帧号。

        Returns:
            下一帧号
        """
        return self._clock.current_frame + 1

    def get_prefetch_range(self) -> Tuple[int, int]:
        """
        获取预读范围。

        AUD-39307: 未知时长（V-P0-09，known_total_frames is None）时
        不施加上界 clamp——此前 ``min(current + lookahead, total_frames - 1)``
        在 total_frames 属性对未知时长返回 0 时得到 -1，预读被整体禁用。
        """
        current = self._clock.current_frame
        start = current + 1
        end = current + self._prefetch_lookahead
        known_total = self._clock.known_total_frames
        if known_total is not None:
            end = min(end, known_total - 1)
        return (start, end)

    def get_missing_prefetch_frames(self) -> list[int]:
        """
        获取预读范围内缺失的帧号。

        Returns:
            缺失帧号列表
        """
        start, end = self.get_prefetch_range()
        return self._source_manager.cache.proxy_cache.get_missing_frames(start, end)

    def schedule_next_frame(self) -> Optional[int]:
        """
        调度下一帧解码。

        Returns:
            需要解码的帧号，或 None（无需解码）

        AUD-39307: 未知时长时不做 ``next_frame >= total_frames`` 的
        上界拒绝（total_frames 属性对未知时长返回 0，会把预读整体
        禁用）；有限时长行为不变。
        """
        next_frame = self.get_next_frame()

        # 检查是否超出范围（仅已知总帧数时）
        known_total = self._clock.known_total_frames
        if known_total is not None and next_frame >= known_total:
            return None

        # 检查缓存
        if self._source_manager.cache.has_proxy(next_frame):
            return None

        return next_frame

    def update(self) -> None:
        """更新调度状态"""
        self._last_scheduled_frame = self._clock.current_frame
