"""
Video Performance Monitor Module
视频性能监控模块，用于追踪解码、上传和丢帧统计。

## 职责

- VideoPerformanceMonitor: 性能监控器，记录解码/上传时间、丢帧统计
- 计时上下文管理器：便捷的性能追踪
- 全局监控器实例：单例模式

## 设计原则

- 低开销：记录操作不显著影响性能
- 线程安全：支持多线程环境使用
- 可扩展：支持多种统计维度

## 使用示例

```python
from src.shared.media.performance_monitor import (
    VideoPerformanceMonitor,
    get_performance_monitor,
    track_decode_time,
)

# 使用全局监控器
monitor = get_performance_monitor()
monitor.start_session()

# 方式1：直接记录
monitor.track_decode_time(frame_index=0, decode_ms=15.5)
monitor.track_upload_time(frame_index=0, upload_ms=2.3)
monitor.track_frame_drop(frame_index=5, reason="decoder_overload")

# 方式2：上下文管理器
with track_decode_time(monitor, frame_index=1):
    # 执行解码...
    pass

# 获取统计
stats = monitor.get_stats()
print(f"解码平均: {stats['decode_avg_ms']:.2f}ms")

# 获取报告
print(monitor.get_report())

# 清空数据
monitor.clear()
```
"""

from __future__ import annotations

import time
import threading
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple, Any

# P2.2: 动态代理阶梯配置
# 根据 4K 播放优化计划，播放时 proxy 分辨率阶梯：
# original -> 1920 -> 1280 -> 960 -> 640
PROXY_LADDER_RUNGS = [1920, 1280, 960, 640]

logger = logging.getLogger(__name__)


@dataclass
class FrameDropRecord:
    """
    丢帧记录。

    Attributes:
        frame_index: 丢帧的帧号
        reason: 丢帧原因
        timestamp: 丢帧时间戳
    """
    frame_index: int
    reason: str
    timestamp: float = field(default_factory=time.monotonic)

    def __repr__(self) -> str:
        return f"FrameDropRecord(frame={self.frame_index}, reason='{self.reason}')"


@dataclass
class VideoPlaybackStats:
    """
    视频播放性能统计数据。

    用于记录和聚合播放管线的各阶段耗时。

    S5.B 新增字段：
    - renderer_queue_depth: 渲染队列深度
    - dropped_preview_frames: 渲染器丢弃的预览帧数
    - held_frames: 持有的帧数（等待渲染）

    Attributes:
        tick_ms: UI tick 耗时（毫秒）
        decode_ms: 解码耗时（毫秒）
        process_ms: 处理耗时（毫秒，包括颜色转换等）
        upload_submit_ms: GPU 上传提交耗时（毫秒）
        render_submit_ms: 渲染提交耗时（毫秒）
        queue_depth: 解码队列深度
        dropped_frames: 累计丢帧数
        seek_ms: Seek 耗时（毫秒）
        read_ms: Read 耗时（毫秒）
        cache_hit: 是否命中缓存
        timestamp: 统计记录时间戳
        renderer_queue_depth: 渲染队列深度（S5.B 新增）
        dropped_preview_frames: 渲染器丢弃的预览帧数（S5.B 新增）
        held_frames: 持有的帧数（S5.B 新增）
    """
    tick_ms: float = 0.0
    decode_ms: float = 0.0
    process_ms: float = 0.0
    upload_submit_ms: float = 0.0
    render_submit_ms: float = 0.0
    queue_depth: int = 0
    dropped_frames: int = 0
    seek_ms: float = 0.0
    read_ms: float = 0.0
    cache_hit: bool = False
    frame_index: int = -1
    timestamp: float = field(default_factory=time.monotonic)
    # S5.B 新增
    renderer_queue_depth: int = 0
    dropped_preview_frames: int = 0
    held_frames: int = 0
    # YUV GPU Upload 分段计时
    plane_prepare_ms: float = 0.0
    texture_write_ms: float = 0.0
    render_ms: float = 0.0
    frame_path: str = ''
    # P0-2: Pipeline path diagnostics
    decode_path: str = ''
    render_frame_path: str = ''
    zero_copy: bool = False
    fallback_reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            'tick_ms': self.tick_ms,
            'decode_ms': self.decode_ms,
            'process_ms': self.process_ms,
            'upload_submit_ms': self.upload_submit_ms,
            'render_submit_ms': self.render_submit_ms,
            'queue_depth': self.queue_depth,
            'dropped_frames': self.dropped_frames,
            'seek_ms': self.seek_ms,
            'read_ms': self.read_ms,
            'cache_hit': self.cache_hit,
            'frame_index': self.frame_index,
            'timestamp': self.timestamp,
            # S5.B 新增
            'renderer_queue_depth': self.renderer_queue_depth,
            'dropped_preview_frames': self.dropped_preview_frames,
            'held_frames': self.held_frames,
            # YUV GPU Upload 分段计时
            'plane_prepare_ms': self.plane_prepare_ms,
            'texture_write_ms': self.texture_write_ms,
            'render_ms': self.render_ms,
            'frame_path': self.frame_path,
            # P0-2: Pipeline path diagnostics
            'decode_path': self.decode_path,
            'render_frame_path': self.render_frame_path,
            'zero_copy': self.zero_copy,
            'fallback_reason': self.fallback_reason,
        }

    def __repr__(self) -> str:
        return (
            f"VideoPlaybackStats("
            f"tick={self.tick_ms:.1f}ms, "
            f"decode={self.decode_ms:.1f}ms, "
            f"process={self.process_ms:.1f}ms, "
            f"upload={self.upload_submit_ms:.1f}ms, "
            f"render={self.render_submit_ms:.1f}ms, "
            f"queue={self.queue_depth}, "
            f"drops={self.dropped_frames}, "
            f"renderer_queue={self.renderer_queue_depth}, "
            f"preview_drops={self.dropped_preview_frames})"
        )


class VideoPerformanceMonitor:
    """
    视频性能监控器。

    追踪视频管线的关键性能指标：
    - 解码时间统计
    - GPU 上传时间统计
    - 丢帧计数和原因分析
    - 会话持续时间

    Features:
    - 线程安全：使用锁保护数据
    - 低开销：记录操作仅存储数据，计算延迟到 get_stats()
    - 详细报告：生成人类可读的性能报告

    Usage:
        monitor = VideoPerformanceMonitor(name='video_pipeline')
        monitor.start_session()
        monitor.track_decode_time(0, 15.5)
        stats = monitor.get_stats()
        report = monitor.get_report()
    """

    # AUD-35404：会话内时间序列统计的保留窗口。此前各 track_* 列表
    # 无界增长，长播放会话（小时级）内存持续膨胀；600 条 ≈ 10 秒
    # @60fps 的滚动窗口，对 p50/p95 实时监控语义足够。
    _MAX_TRACKED_SAMPLES: int = 600

    def __init__(self, name: str = 'video_pipeline') -> None:
        """
        初始化性能监控器。

        Args:
            name: 监控器名称，用于标识和日志
        """
        self._name = name
        self._lock = threading.RLock()

        # 解码时间记录 (frame_index -> ms)
        self._decode_times: Dict[int, float] = {}
        # 上传时间记录 (frame_index -> ms)
        self._upload_times: Dict[int, float] = {}
        # 丢帧记录
        self._frame_drops: List[FrameDropRecord] = []
        # 会话开始时间
        self._start_time: float = 0.0
        # 会话结束时间
        self._end_time: float = 0.0
        # 会话是否活动
        self._session_active: bool = False
        # 总帧数（用于计算丢帧率）
        self._total_frames: int = 0

        # P0.A 新增: 播放性能统计数据
        self._playback_stats: List[VideoPlaybackStats] = []
        # UI tick 时间记录
        self._tick_times: List[float] = []
        # 处理时间记录
        self._process_times: List[float] = []
        # 渲染提交时间记录
        self._render_submit_times: List[float] = []
        # 队列深度记录
        self._queue_depths: List[int] = []
        # seek 时间记录
        self._seek_times: List[float] = []
        # read 时间记录
        self._read_times: List[float] = []
        # 缓存命中记录
        self._cache_hits: List[bool] = []

        # 杨红枚理: 渲染队列深度记录
        self._renderer_queue_depths: List[int] = []
        self._dropped_preview_frames: int = 0
        self._held_frames: List[int] = []

        # P2.2: 合并视频命令数记录
        self._coalesced_video_commands: List[int] = []

        # YUV GPU Upload 分段计时
        self._plane_prepare_times: List[float] = []
        self._texture_write_times: List[float] = []
        self._yuv_render_times: List[float] = []
        self._frame_paths: List[str] = []

        # 零拷贝追踪
        self._zero_copy_frames: int = 0
        self._cpu_download_frames: int = 0
        self._cpu_upload_frames: int = 0
        self._external_texture_frames: int = 0
        self._texture_write_video_frames: int = 0
        self._decode_path_counts: Dict[str, int] = {}
        self._render_frame_path_counts: Dict[str, int] = {}
        self._last_fallback_reason: Optional[str] = None

        # S5.B: 自动降级状态
        self._degradation_level: int = 0  # 0=正常, 1=轻度, 2=中度, 3=重度
        self._last_tick_p95_high_time: float = 0.0  # 上次 tick p95 超阈值的时间
        self._consecutive_slow_ticks: int = 0  # 连续慢 tick 计数
        self._consecutive_normal_ticks: int = 0  # S5.B: 连续正常 tick 计数（用于恢复策略）
        self._prefetch_lookahead_reduction: float = 0.0  # 预读前瞻降低比例
        # 2ms 对真实 PyQt 播放 tick 过于激进，会把 3ms 左右的正常 tick
        # 误判为过载并持续削减预读，最终导致播放队列被饿空。
        self._tick_degradation_threshold_ms: float = 8.0

        # P2.2: 动态代理阶梯状态
        # 当前 proxy 阶梯索引，-1 表示原始分辨率（不降级）
        self._proxy_ladder_index: int = -1
        # 当前 proxy 分辨率（long edge），0 表示未激活
        self._current_proxy_long_edge: int = 0
        # 原始视频 long edge（在 start_session 时设置）
        self._original_long_edge: int = 0
        # 总分段耗时 p95 采样（用于 proxy 阶梯决策）
        self._total_frame_times: List[float] = []
        # 连续帧超时计数（用于降级判定）
        self._consecutive_slow_frames: int = 0
        # 连续帧正常计数（用于升级判定）
        self._consecutive_fast_frames: int = 0

        # 聚合日志输出相关
        self._last_aggregate_time: float = 0.0
        self._aggregate_interval: float = 1.0  # 1秒聚合一次
        self._aggregate_log_enabled: bool = True

        logger.debug(f"VideoPerformanceMonitor: 创建监控器 '{name}'")

    def start_session(self) -> None:
        """
        开始监控会话。

        清空之前的统计数据并开始新的记录周期。
        """
        with self._lock:
            self._clear_data_locked()
            self._start_time = time.monotonic()
            self._end_time = 0.0
            self._session_active = True
            self._last_aggregate_time = self._start_time

            logger.info(f"VideoPerformanceMonitor: 会话开始 '{self._name}'")

    def end_session(self) -> None:
        """
        结束监控会话。

        标记会话结束，停止记录新数据。
        """
        with self._lock:
            if self._session_active:
                self._end_time = time.monotonic()
                self._session_active = False
                logger.info(f"VideoPerformanceMonitor: 会话结束 '{self._name}'")

    def track_decode_time(self, frame_index: int, decode_ms: float) -> None:
        """
        记录解码时间。

        Args:
            frame_index: 帧号
            decode_ms: 解码耗时（毫秒）
        """
        with self._lock:
            self._decode_times[frame_index] = decode_ms
            self._total_frames += 1

            # 记录详细日志（仅对慢解码）
            if decode_ms > 50.0:  # 超过 50ms 视为慢解码
                logger.debug(
                    f"VideoPerformanceMonitor: 慢解码 "
                    f"frame={frame_index}, decode_ms={decode_ms:.1f}"
                )

    def track_upload_time(self, frame_index: int, upload_ms: float) -> None:
        """
        记录 GPU 上传时间。

        Args:
            frame_index: 帧号
            upload_ms: 上传耗时（毫秒）
        """
        with self._lock:
            self._upload_times[frame_index] = upload_ms

            # 记录详细日志（仅对慢上传）
            if upload_ms > 20.0:  # 超过 20ms 视为慢上传
                logger.debug(
                    f"VideoPerformanceMonitor: 慢上传 "
                    f"frame={frame_index}, upload_ms={upload_ms:.1f}"
                )

    def track_frame_drop(self, frame_index: int, reason: str) -> None:
        """
        记录丢帧。

        Args:
            frame_index: 丢帧的帧号
            reason: 丢帧原因
                - 'decoder_overload': 解码器过载
                - 'upload_lag': GPU 上传延迟
                - 'seek': Seek 操作
                - 'cache_evict': 缓存驱逐
                - 'playback_reset': 播放重置
                - 'unknown': 未知原因
        """
        with self._lock:
            record = FrameDropRecord(frame_index=frame_index, reason=reason)
            self._frame_drops.append(record)

            logger.debug(
                f"VideoPerformanceMonitor: 丢帧 "
                f"frame={frame_index}, reason='{reason}'"
            )

    # ================================================================
    # P0.A 新增: 播放性能追踪方法
    # ================================================================

    def track_tick_time(self, tick_ms: float) -> None:
        """
        记录 UI tick 耗时。

        Args:
            tick_ms: UI tick 耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._tick_times, tick_ms)
        # AUD-35404：聚合检查移出锁外。每秒一次的聚合日志要对全部统计
        # 序列排序（见 get_aggregate_stats），此前在 RLock 内执行，长会话
        # 下与所有 track_* 记录路径争锁。
        self._check_aggregate_log()

    def track_process_time(self, frame_index: int, process_ms: float) -> None:
        """
        记录帧处理耗时（包括颜色转换、格式转换等）。

        Args:
            frame_index: 帧号
            process_ms: 处理耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._process_times, process_ms)

            if process_ms > 30.0:  # 超过 30ms 视为慢处理
                logger.debug(
                    f"VideoPerformanceMonitor: 慢处理 "
                    f"frame={frame_index}, process_ms={process_ms:.1f}"
                )

    def track_render_submit_time(self, frame_index: int, render_ms: float) -> None:
        """
        记录渲染提交耗时。

        Args:
            frame_index: 帧号
            render_ms: 渲染提交耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._render_submit_times, render_ms)

            if render_ms > 20.0:  # 超过 20ms 视为慢渲染
                logger.debug(
                    f"VideoPerformanceMonitor: 慢渲染提交 "
                    f"frame={frame_index}, render_ms={render_ms:.1f}"
                )

    def track_queue_depth(self, depth: int) -> None:
        """
        记录队列深度。

        Args:
            depth: 当前队列深度（帧数）
        """
        with self._lock:
            self._append_bounded(self._queue_depths, depth)

    def track_seek_time(self, frame_index: int, seek_ms: float) -> None:
        """
        记录 Seek 耗时。

        Args:
            frame_index: 目标帧号
            seek_ms: Seek 耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._seek_times, seek_ms)

            if seek_ms > 100.0:  # 超过 100ms 视为慢 seek
                logger.debug(
                    f"VideoPerformanceMonitor: 慢 seek "
                    f"frame={frame_index}, seek_ms={seek_ms:.1f}"
                )

    def track_read_time(self, frame_index: int, read_ms: float) -> None:
        """
        记录 Read（解码）耗时。

        Args:
            frame_index: 帧号
            read_ms: Read 耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._read_times, read_ms)

    def track_cache_hit(self, frame_index: int, hit: bool) -> None:
        """
        记录缓存命中情况。

        Args:
            frame_index: 帧号
            hit: 是否命中缓存
        """
        with self._lock:
            self._append_bounded(self._cache_hits, hit)

    # ================================================================
    # S5.B 新增: 渲染队列和预览丢帧追踪方法
    # ================================================================

    def track_renderer_queue_depth(self, depth: int) -> None:
        """
        S5.B: 记录渲染队列深度。

        Args:
            depth: 渲染队列当前深度（命令数）
        """
        with self._lock:
            self._append_bounded(self._renderer_queue_depths, depth)

            if depth > 10:  # 渲染队列超过 10 个命令视为积压
                logger.debug(
                    f"VideoPerformanceMonitor: 渲染队列积压 "
                    f"depth={depth}"
                )

    def track_dropped_preview_frame(self, count: int = 1) -> None:
        """
        S5.B: 记录渲染器丢弃的预览帧数。

        Args:
            count: 本次丢弃的帧数（默认 1）
        """
        with self._lock:
            self._dropped_preview_frames += count

    def track_held_frames(self, count: int) -> None:
        """
        S5.B: 记录持有的帧数（等待渲染或处理）。

        Args:
            count: 当前持有的帧数
        """
        with self._lock:
            self._append_bounded(self._held_frames, count)

    def track_coalesced_video_commands(self, count: int) -> None:
        """
        S5.B: 记录合并的视频渲染命令数。

        用于追踪渲染器队列中合并丢弃的命令数量，
        反映 backpressure 机制的效果。

        Args:
            count: 本次合并的命令数
        """
        with self._lock:
            self._append_bounded(self._coalesced_video_commands, count)

    # ================================================================
    # YUV GPU Upload 分段计时
    # ================================================================

    def track_plane_prepare_time(self, prepare_ms: float) -> None:
        """
        记录 YUV plane 归一化/准备耗时。

        Args:
            prepare_ms: Numpy 归一化、astype、buffer 准备耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._plane_prepare_times, prepare_ms)

    def track_texture_write_time(self, write_ms: float) -> None:
        """
        记录 Texture.write() 三平面上传耗时。

        Args:
            write_ms: GPU texture write 耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._texture_write_times, write_ms)

    def track_yuv_render_time(self, render_ms: float) -> None:
        """
        记录 YUV shader 转 RGB + FBO render 耗时。

        Args:
            render_ms: GPU render 耗时（毫秒）
        """
        with self._lock:
            self._append_bounded(self._yuv_render_times, render_ms)

    def track_frame_path(self, frame_path: str) -> None:
        """
        记录帧处理路径。

        Args:
            frame_path: 路径标识 (planar_yuv, packed_rgb_u8, packed_rgb_f16 等)
        """
        with self._lock:
            self._append_bounded(self._frame_paths, frame_path)

    def track_zero_copy_frame(
        self,
        decode_path: str,
        render_frame_path: str,
        is_zero_copy: bool,
        cpu_download: bool = False,
        cpu_upload: bool = False,
        external_texture: bool = False,
        texture_write: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> None:
        """
        记录零拷贝相关帧统计。

        每帧渲染后调用一次，明确标记该帧的数据流路径。

        Args:
            decode_path: 解码路径标识（如 DecodePath 枚举值）
            render_frame_path: 渲染路径标识（如 RenderFramePath 枚举值）
            is_zero_copy: 是否为零拷贝路径
            cpu_download: 是否有 CPU 下载（frame.to_ndarray() / frame.transfer()）
            cpu_upload: 是否有 CPU 上传（Texture.write() 写视频帧）
            external_texture: 是否直接绑定 external GPU texture
            texture_write: 是否使用 Texture.write() 写视频帧
            fallback_reason: fallback 原因（零拷贝失败时必须提供）
        """
        with self._lock:
            if is_zero_copy:
                self._zero_copy_frames += 1
            if cpu_download:
                self._cpu_download_frames += 1
            if cpu_upload:
                self._cpu_upload_frames += 1
            if external_texture:
                self._external_texture_frames += 1
            if texture_write:
                self._texture_write_video_frames += 1

            self._decode_path_counts[decode_path] = (
                self._decode_path_counts.get(decode_path, 0) + 1
            )
            self._render_frame_path_counts[render_frame_path] = (
                self._render_frame_path_counts.get(render_frame_path, 0) + 1
            )

            if fallback_reason:
                self._last_fallback_reason = fallback_reason
            elif not is_zero_copy:
                self._last_fallback_reason = fallback_reason or "non_zero_copy_path"

    def track_cpu_download_frame(self) -> None:
        """
        记录一次 CPU 下载帧（frame.to_ndarray() / frame.transfer()）。

        用于标记硬解后数据回到 CPU 的情况。
        """
        with self._lock:
            self._cpu_download_frames += 1

    def track_cpu_upload_frame(self) -> None:
        """
        记录一次 CPU 上传帧（Texture.write() 写视频帧到 GPU）。
        """
        with self._lock:
            self._cpu_upload_frames += 1

    def track_external_texture_frame(self) -> None:
        """
        记录一次 external GPU texture 直接绑定帧（零拷贝路径）。
        """
        with self._lock:
            self._external_texture_frames += 1

    def check_tick_performance_and_degrade(self) -> int:
        """
        S5.B: 检查 tick 性能并返回降级建议。

        如果连续 1 秒 tick p95 高于播放降级阈值，建议增加降级策略。
        连续 3 秒 tick p95 正常后，逐级恢复降级级别。

        Returns:
            降级级别：
            - 0: 正常，无需降级
            - 1: 轻度降级，降低预读前瞻 25%
            - 2: 中度降级，降低预读前瞻 50%
            - 3: 重度降级，降低预读前瞻 75%，增加丢帧策略
        """
        with self._lock:
            # 计算最近 1 秒的 tick p95

            # 需要有足够的数据（至少 10 个 tick）
            if len(self._tick_times) < 10:
                # AUD-35405：样本不足时不做升降级判定，但已生效的降级
                # 级别对调用方仍然成立——返回当前级别而非硬编码 0，
                # 避免与 get_degradation_level() 口径不一致。
                return self._degradation_level

            # 计算最近的 tick p95
            recent_ticks = self._tick_times[-100:]  # 最近 100 个 tick
            sorted_ticks = sorted(recent_ticks)
            p95_index = int(len(sorted_ticks) * 0.95)
            tick_p95 = sorted_ticks[p95_index]

            # 判断是否超阈值。真实 UI tick 只要低于一帧预算的一小部分
            # 就不应触发预读降级；否则会在正常播放时误伤解码队列。
            if tick_p95 > self._tick_degradation_threshold_ms:
                self._consecutive_slow_ticks += 1
                # 重置连续正常计数
                self._consecutive_normal_ticks = 0

                # 检查是否连续超过阈值超过一定次数（约 1 秒）
                # 假设 tick 间隔约 16ms (60fps)，60 次 ≈ 1 秒
                if self._consecutive_slow_ticks >= 30:
                    # 升级降级级别
                    if self._degradation_level < 3:
                        self._degradation_level += 1
                        self._update_lookahead_reduction()
                        logger.warning(
                            f"VideoPerformanceMonitor: UI tick 性能过载，升级降级级别 "
                            f"level={self._degradation_level}, "
                            f"tick_p95={tick_p95:.2f}ms, "
                            f"lookahead_reduction={self._prefetch_lookahead_reduction:.0%}"
                        )

                    self._consecutive_slow_ticks = 0  # 重置计数
            else:
                # tick 正常
                # S5.B: 连续 3 秒（约 180 ticks）正常后，逐级恢复
                self._consecutive_normal_ticks += 1

                # 重置慢 tick 计数
                if self._consecutive_slow_ticks > 0:
                    self._consecutive_slow_ticks = 0

                # S5.B: 连续 3 秒正常后降低降级级别（约 180 ticks @ 60fps）
                if self._consecutive_normal_ticks >= 180 and self._degradation_level > 0:
                    self._degradation_level -= 1
                    self._update_lookahead_reduction()
                    self._consecutive_normal_ticks = 0  # 重置计数，等待下一个 3 秒周期
                    logger.info(
                        f"VideoPerformanceMonitor: UI tick 性能恢复（连续3秒），降低降级级别 "
                        f"level={self._degradation_level}"
                    )

            return self._degradation_level

    def _update_lookahead_reduction(self) -> None:
        """S5.B: 根据降级级别更新预读前瞻降低比例"""
        # 确保级别在有效范围内
        level = max(0, min(3, self._degradation_level))
        reduction_map = {
            0: 0.0,
            1: 0.25,
            2: 0.50,
            3: 0.75,
        }
        self._prefetch_lookahead_reduction = reduction_map.get(level, 0.75)

    def get_prefetch_lookahead_reduction(self) -> float:
        """
        S5.B: 获取预读前瞻降低比例。

        Returns:
            降低比例（0.0 - 0.75）
        """
        with self._lock:
            return self._prefetch_lookahead_reduction

    def get_degradation_level(self) -> int:
        """
        S5.B: 获取当前降级级别。

        Returns:
            降级级别（0-3）
        """
        with self._lock:
            return self._degradation_level

    # ================================================================
    # P2.2: 动态代理阶梯
    # ================================================================

    def set_original_resolution(self, width: int, height: int) -> None:
        """
        P2.2: 设置原始视频分辨率。

        在开始播放时调用，用于计算代理阶梯的各级分辨率。

        Args:
            width: 原始视频宽度
            height: 原始视频高度
        """
        with self._lock:
            self._original_long_edge = max(width, height)
            # 初始状态：不降级（-1 表示原始分辨率）
            self._proxy_ladder_index = -1
            self._current_proxy_long_edge = self._original_long_edge
            self._consecutive_slow_frames = 0
            self._consecutive_fast_frames = 0
            logger.info(
                f"ProxyLadder: set original resolution {width}x{height}, "
                f"long_edge={self._original_long_edge}"
            )

    def track_frame_total_time(self, total_ms: float, fps: float = 30.0) -> None:
        """
        P2.2: 记录每帧总耗时（decode + prepare + write + render）。

        用于动态代理阶梯的帧预算判定。

        Args:
            total_ms: 每帧总耗时（毫秒）
            fps: 当前视频帧率（用于计算帧预算）
        """
        with self._lock:
            self._total_frame_times.append(total_ms)
            # 只保留最近 120 帧的数据
            if len(self._total_frame_times) > 120:
                self._total_frame_times = self._total_frame_times[-120:]

            if self._original_long_edge <= 0:
                return

            frame_budget_ms = 1000.0 / max(fps, 1.0)

            if total_ms > frame_budget_ms * 1.25:
                self._consecutive_slow_frames += 1
                self._consecutive_fast_frames = 0
            else:
                self._consecutive_fast_frames += 1
                self._consecutive_slow_frames = 0

    def check_proxy_ladder(self) -> Tuple[int, str]:
        """
        P2.2: 检查是否需要调整代理阶梯。

        根据最近帧的连续超时/达标计数（由 track_frame_total_time 逐帧
        按该帧的 frame_budget * 1.25 / * 0.65 判定累计），决定是否
        升降代理分辨率等级；total_p95 仅进入 reason 字符串用于日志。

        降级条件（满足任一）：
        - 逐帧 total_ms > frame_budget * 1.25 连续累计 60 帧
        - late_frames > 5 连续 60 帧

        升级条件：
        - 逐帧 total_ms < frame_budget * 0.65 连续累计 150 帧

        Returns:
            (proxy_long_edge, reason): 新的代理分辨率长边和变更原因字符串
        """
        with self._lock:
            if self._original_long_edge <= 0:
                return (self._original_long_edge, "no_video")

            if not self._total_frame_times:
                return (self._current_proxy_long_edge, "no_data")

            # 计算 recent 帧的 total p95（最近 60 帧）
            recent = self._total_frame_times[-60:]
            if len(recent) < 10:
                return (self._current_proxy_long_edge, "insufficient_data")

            sorted_times = sorted(recent)
            p95_index = int(len(sorted_times) * 0.95)
            total_p95 = sorted_times[min(p95_index, len(sorted_times) - 1)]

            current_index = self._proxy_ladder_index
            new_index = current_index
            reason = "stable"

            # 降级判定：逐帧超预算计数连续 60（AUD-35407：原 elif
            # current_index == -1 分支永不可达——current_index == -1 时
            # 首条件 -1 < len(RUNGS)-1 恒真且已置 new_index = 0，删除；
            # 帧 Budget 常量同理从未参与本函数判定，删除）
            if self._consecutive_slow_frames >= 60:
                # 需要降一级（current_index == -1 表示原始分辨率，降至第一级）
                if current_index < len(PROXY_LADDER_RUNGS) - 1:
                    new_index = current_index + 1 if current_index >= 0 else 0
                    reason = f"slow_frames={self._consecutive_slow_frames},total_p95={total_p95:.1f}ms"

            # 升级判定：total_p95 连续低于帧预算 0.65 倍
            elif self._consecutive_fast_frames >= 150:
                if current_index > 0:
                    new_index = current_index - 1
                    reason = f"upgrade,fast_frames={self._consecutive_fast_frames},total_p95={total_p95:.1f}ms"
                elif current_index == 0:
                    # 从第一级阶梯升回原始分辨率
                    new_index = -1
                    reason = f"upgrade_to_original,fast_frames={self._consecutive_fast_frames}"

            if new_index != current_index:
                self._proxy_ladder_index = new_index
                if new_index == -1:
                    self._current_proxy_long_edge = self._original_long_edge
                else:
                    self._current_proxy_long_edge = PROXY_LADDER_RUNGS[new_index]

                logger.info(
                    f"ProxyLadder: {current_index} -> {new_index}, "
                    f"proxy_long_edge={self._current_proxy_long_edge}, "
                    f"reason={reason}"
                )

                # 重置计数器
                self._consecutive_slow_frames = 0
                self._consecutive_fast_frames = 0

            return (self._current_proxy_long_edge, reason)

    def get_proxy_ladder_info(self) -> Dict[str, Any]:
        """
        P2.2: 获取当前代理阶梯状态信息。

        Returns:
            包含 ladder_index, proxy_long_edge, original_long_edge 的字典
        """
        with self._lock:
            return {
                'ladder_index': self._proxy_ladder_index,
                'proxy_long_edge': self._current_proxy_long_edge,
                'original_long_edge': self._original_long_edge,
                'consecutive_slow_frames': self._consecutive_slow_frames,
                'consecutive_fast_frames': self._consecutive_fast_frames,
                'ladder_rungs': list(PROXY_LADDER_RUNGS),
            }

    def record_playback_stats(self, stats: VideoPlaybackStats) -> None:
        """
        记录完整的播放统计数据。

        Args:
            stats: VideoPlaybackStats 实例
        """
        with self._lock:
            self._playback_stats.append(stats)
            self._total_frames += 1
        # AUD-35404：聚合检查移出锁外（与 track_tick_time 同理）。
        self._check_aggregate_log()

    def get_playback_stats(self) -> List[VideoPlaybackStats]:
        """
        获取所有播放统计数据。

        Returns:
            VideoPlaybackStats 列表
        """
        with self._lock:
            return list(self._playback_stats)

    def get_aggregate_stats(self) -> Dict[str, Any]:
        """
        获取聚合的播放性能统计。

        用于 get_perf_stats() 接口输出。

        S5.B 新增字段：
        - renderer_queue_depth: 渲染队列深度
        - dropped_preview_frames: 渲染器丢弃的预览帧数
        - held_frames: 持有的帧数
        - degradation_level: 当前降级级别
        - prefetch_lookahead_reduction: 预读前瞻降低比例

        Returns:
            聚合统计数据，包含：
            - tick_avg_ms: 平均 UI tick 耗时
            - tick_max_ms: 最大 UI tick 耗时
            - tick_p95_ms: UI tick P95 耗时
            - decode_avg_ms: 平均解码耗时
            - process_avg_ms: 平均处理耗时
            - upload_avg_ms: 平均上传提交耗时
            - render_avg_ms: 平均渲染提交耗时
            - queue_avg_depth: 平均队列深度
            - dropped_frames: 累计丢帧数
            - seek_avg_ms: 平均 seek 耗时
            - read_avg_ms: 平均 read 耗时
            - cache_hit_rate: 缓存命中率
        """
        with self._lock:
            # tick 统计
            tick_stats = self._calculate_stats(self._tick_times)

            # 处理统计
            process_stats = self._calculate_stats(self._process_times)

            # 渲染提交统计
            render_stats = self._calculate_stats(self._render_submit_times)

            # seek 统计
            seek_stats = self._calculate_stats(self._seek_times)

            # read 统计
            read_stats = self._calculate_stats(self._read_times)

            # AUD-35409：每类数据只拷贝/排序一次，避免同一数据在字典推导
            # 中被 _calculate_stats 重复执行 3-4 次（该函数内含 sorted +
            # 两次遍历，且每秒在锁内运行）。
            decode_stats = self._calculate_stats(list(self._decode_times.values()))
            upload_stats = self._calculate_stats(list(self._upload_times.values()))
            plane_prepare_stats = self._calculate_stats(self._plane_prepare_times)
            texture_write_stats = self._calculate_stats(self._texture_write_times)
            yuv_render_stats = self._calculate_stats(self._yuv_render_times)
            queue_depth_stats = self._calculate_int_stats(self._queue_depths)
            renderer_queue_depth_stats = self._calculate_int_stats(self._renderer_queue_depths)
            held_frames_stats = self._calculate_int_stats(self._held_frames)

            # 队列深度统计
            queue_avg = sum(self._queue_depths) / len(self._queue_depths) if self._queue_depths else 0.0
            queue_max = max(self._queue_depths) if self._queue_depths else 0

            # S5.B: 渲染队列深度统计
            renderer_queue_avg = sum(self._renderer_queue_depths) / len(self._renderer_queue_depths) if self._renderer_queue_depths else 0.0
            renderer_queue_max = max(self._renderer_queue_depths) if self._renderer_queue_depths else 0

            # S5.B: 持有帧数统计
            held_frames_avg = sum(self._held_frames) / len(self._held_frames) if self._held_frames else 0.0
            held_frames_max = max(self._held_frames) if self._held_frames else 0

            # 缓存命中率
            cache_hits = sum(1 for h in self._cache_hits if h)
            cache_hit_rate = cache_hits / len(self._cache_hits) if self._cache_hits else 0.0

            # 累计丢帧数
            dropped_frames = len(self._frame_drops)

            return {
                # P5-1: 完整的百分位统计
                'tick_avg_ms': tick_stats['avg'],
                'tick_min_ms': tick_stats['min'],
                'tick_max_ms': tick_stats['max'],
                'tick_p50_ms': tick_stats['p50'],
                'tick_p95_ms': tick_stats['p95'],
                'tick_count': tick_stats['count'],

                'decode_avg_ms': decode_stats['avg'],
                'decode_p50_ms': decode_stats['p50'],
                'decode_p95_ms': decode_stats['p95'],
                'decode_max_ms': decode_stats['max'],

                'process_avg_ms': process_stats['avg'],
                'process_p50_ms': process_stats['p50'],
                'process_p95_ms': process_stats['p95'],
                'process_max_ms': process_stats['max'],

                'upload_avg_ms': upload_stats['avg'],
                'upload_p50_ms': upload_stats['p50'],
                'upload_p95_ms': upload_stats['p95'],
                'upload_max_ms': upload_stats['max'],

                'render_avg_ms': render_stats['avg'],
                'render_p50_ms': render_stats['p50'],
                'render_p95_ms': render_stats['p95'],
                'render_max_ms': render_stats['max'],

                'queue_avg_depth': queue_avg,
                'queue_max_depth': queue_max,
                'queue_p50_depth': queue_depth_stats['p50'],
                'queue_p95_depth': queue_depth_stats['p95'],

                'dropped_frames': dropped_frames,

                # S5.B 新增
                'renderer_queue_depth': renderer_queue_avg,
                'renderer_queue_max_depth': renderer_queue_max,
                'renderer_queue_p50_depth': renderer_queue_depth_stats['p50'],
                'renderer_queue_p95_depth': renderer_queue_depth_stats['p95'],
                'dropped_preview_frames': self._dropped_preview_frames,
                'held_frames': held_frames_avg,
                'held_frames_max': held_frames_max,
                'held_frames_p50': held_frames_stats['p50'],
                'held_frames_p95': held_frames_stats['p95'],
                'degradation_level': self._degradation_level,
                'prefetch_lookahead_reduction': self._prefetch_lookahead_reduction,

                # YUV GPU Upload 分段计时
                'plane_prepare_avg_ms': plane_prepare_stats['avg'],
                'plane_prepare_p50_ms': plane_prepare_stats['p50'],
                'plane_prepare_p95_ms': plane_prepare_stats['p95'],
                'plane_prepare_max_ms': plane_prepare_stats['max'],

                'texture_write_avg_ms': texture_write_stats['avg'],
                'texture_write_p50_ms': texture_write_stats['p50'],
                'texture_write_p95_ms': texture_write_stats['p95'],
                'texture_write_max_ms': texture_write_stats['max'],

                'yuv_render_avg_ms': yuv_render_stats['avg'],
                'yuv_render_p50_ms': yuv_render_stats['p50'],
                'yuv_render_p95_ms': yuv_render_stats['p95'],

                'frame_path_counts': self._count_frame_paths(),

                # 零拷贝追踪
                'zero_copy_frames': self._zero_copy_frames,
                'cpu_download_frames': self._cpu_download_frames,
                'cpu_upload_frames': self._cpu_upload_frames,
                'external_texture_frames': self._external_texture_frames,
                'texture_write_video_frames': self._texture_write_video_frames,
                'decode_path_counts': dict(self._decode_path_counts),
                'render_frame_path_counts': dict(self._render_frame_path_counts),
                'last_fallback_reason': self._last_fallback_reason,

                # P2.2: 代理阶梯状态
                'proxy_ladder_index': self._proxy_ladder_index,
                'proxy_long_edge': self._current_proxy_long_edge,
                'original_long_edge': self._original_long_edge,
                'consecutive_slow_frames': self._consecutive_slow_frames,
                'consecutive_fast_frames': self._consecutive_fast_frames,
                'coalesced_video_commands_total': sum(self._coalesced_video_commands) if self._coalesced_video_commands else 0,
                'coalesced_video_commands_count': len(self._coalesced_video_commands),

                'seek_avg_ms': seek_stats['avg'],
                'seek_p50_ms': seek_stats['p50'],
                'seek_p95_ms': seek_stats['p95'],
                'seek_max_ms': seek_stats['max'],

                'read_avg_ms': read_stats['avg'],
                'read_p50_ms': read_stats['p50'],
                'read_p95_ms': read_stats['p95'],

                'cache_hit_rate': cache_hit_rate,
                'cache_hit_count': cache_hits,
                'cache_total_requests': len(self._cache_hits),

                'frames_processed': self._total_frames,
            }

    # ================================================================
    # 聚合日志输出
    # ================================================================

    def _check_aggregate_log(self) -> None:
        """
        检查是否需要输出聚合日志。

        每 1 秒输出一次聚合的性能统计，避免每帧打印拖慢播放。
        AUD-35404：调用方不得持有 self._lock——聚合输出要对全部统计
        序列排序，此前在 RLock 内执行，长会话下阻塞所有 track_* 记录。
        时间戳判定本身在锁内完成（原子推进 _last_aggregate_time），
        重活 _output_aggregate_log 在锁外做（其内部经 get_aggregate_stats
        短暂加锁取快照）。
        """
        if not self._aggregate_log_enabled:
            return

        current_time = time.monotonic()
        with self._lock:
            elapsed = current_time - self._last_aggregate_time
            if elapsed < self._aggregate_interval:
                return
            self._last_aggregate_time = current_time

        self._output_aggregate_log()

    def _output_aggregate_log(self) -> None:
        """
        输出聚合的性能日志。

        格式简洁，包含关键指标的 1 秒平均值。
        """
        stats = self.get_aggregate_stats()

        # 只在有实际数据时输出
        if stats['frames_processed'] == 0 and stats['dropped_frames'] == 0:
            return

        # 构建 YUV 上传分段日志
        yuv_parts = ""
        if stats.get('plane_prepare_avg_ms', 0) > 0:
            yuv_parts = (f", yuv_prepare={stats['plane_prepare_avg_ms']:.1f}ms"
                        f", yuv_write={stats['texture_write_avg_ms']:.1f}ms"
                        f", yuv_render={stats['yuv_render_avg_ms']:.1f}ms")

        # 帧路径分布
        path_counts = stats.get('frame_path_counts', {})
        path_str = ", ".join(f"{k}={v}" for k, v in path_counts.items()) if path_counts else ""

        # 零拷贝状态
        zero_copy_frames = stats.get('zero_copy_frames', 0)
        cpu_download_frames = stats.get('cpu_download_frames', 0)
        cpu_upload_frames = stats.get('cpu_upload_frames', 0)
        external_texture_frames = stats.get('external_texture_frames', 0)
        decode_path_counts = stats.get('decode_path_counts', {})
        render_path_counts = stats.get('render_frame_path_counts', {})
        last_fallback = stats.get('last_fallback_reason', None)

        zero_copy_str = f", zero_copy={zero_copy_frames}"
        if cpu_download_frames > 0:
            zero_copy_str += f", cpu_download={cpu_download_frames}"
        if cpu_upload_frames > 0:
            zero_copy_str += f", cpu_upload={cpu_upload_frames}"
        if external_texture_frames > 0:
            zero_copy_str += f", ext_tex={external_texture_frames}"
        if last_fallback:
            zero_copy_str += f", fallback={last_fallback}"

        # P0-2: Pipeline path in aggregate log（AUD-35404：改从 stats 快照
        # 读取，不再无锁直读 self._decode_path_counts）
        if decode_path_counts:
            last_decode = list(decode_path_counts.keys())[-1]
            last_render = list(render_path_counts.keys())[-1] if render_path_counts else ""
            if last_decode:
                zero_copy_str += f", last_decode={last_decode}"
            if last_render:
                zero_copy_str += f", last_render={last_render}"

        decode_str = ", ".join(f"{k}={v}" for k, v in decode_path_counts.items()) if decode_path_counts else ""
        render_str = ", ".join(f"{k}={v}" for k, v in render_path_counts.items()) if render_path_counts else ""

        # P2.2: 代理阶梯信息
        ladder_index = stats.get('proxy_ladder_index', -1)
        proxy_long_edge = stats.get('proxy_long_edge', 0)
        original_long_edge = stats.get('original_long_edge', 0)
        ladder_str = ""
        if original_long_edge > 0 and proxy_long_edge != original_long_edge:
            ladder_str = f", proxy={proxy_long_edge}p(ladder={ladder_index})"

        logger.info(
            f"[播放性能] "
            f"tick={stats['tick_avg_ms']:.1f}ms "
            f"(max={stats['tick_max_ms']:.1f}), "
            f"decode={stats['decode_avg_ms']:.1f}ms, "
            f"process={stats['process_avg_ms']:.1f}ms, "
            f"upload={stats['upload_avg_ms']:.1f}ms, "
            f"render={stats['render_avg_ms']:.1f}ms{yuv_parts}, "
            f"queue={stats['queue_avg_depth']:.0f} "
            f"(max={stats['queue_max_depth']}), "
            f"drop={stats['dropped_frames']}, "
            f"cache_hit={stats['cache_hit_rate']:.1%}"
            f"{', path=[' + path_str + ']' if path_str else ''}"
            f"{zero_copy_str}"
            f"{', decode=[' + decode_str + ']' if decode_str else ''}"
            f"{', render=[' + render_str + ']' if render_str else ''}"
            f"{ladder_str}"
        )

    def set_aggregate_log_interval(self, interval_seconds: float) -> None:
        """
        设置聚合日志输出间隔。

        Args:
            interval_seconds: 输出间隔（秒），默认 1.0
        """
        with self._lock:
            self._aggregate_interval = max(0.5, min(60.0, interval_seconds))

    def set_aggregate_log_enabled(self, enabled: bool) -> None:
        """
        启用/禁用聚合日志输出。

        Args:
            enabled: True 启用，False 禁用
        """
        with self._lock:
            self._aggregate_log_enabled = enabled

    def get_stats(self) -> Dict[str, Any]:
        """
        获取统计信息。

        Returns:
            统计数据字典，包含：
            - decode_avg_ms: 平均解码时间（毫秒）
            - decode_max_ms: 最大解码时间（毫秒）
            - decode_min_ms: 最小解码时间（毫秒）
            - decode_p95_ms: 解码时间 P95（毫秒）
            - upload_avg_ms: 平均上传时间（毫秒）
            - upload_max_ms: 最大上传时间（毫秒）
            - upload_min_ms: 最小上传时间（毫秒）
            - upload_p95_ms: 上传时间 P95（毫秒）
            - total_drops: 总丢帧数
            - drop_rate: 丢帧率（0.0 - 1.0）
            - drop_reasons: 按原因分类的丢帧统计
            - session_duration: 会话持续时间（秒）
            - frames_processed: 处理的总帧数
            - effective_fps: 有效处理帧率
        """
        with self._lock:
            # 解码统计
            decode_times = list(self._decode_times.values())
            decode_stats = self._calculate_stats(decode_times)

            # 上传统计
            upload_times = list(self._upload_times.values())
            upload_stats = self._calculate_stats(upload_times)

            # 丢帧统计
            total_drops = len(self._frame_drops)
            drop_rate = total_drops / self._total_frames if self._total_frames > 0 else 0.0

            # 按原因分类丢帧
            drop_reasons: Dict[str, int] = {}
            for drop in self._frame_drops:
                drop_reasons[drop.reason] = drop_reasons.get(drop.reason, 0) + 1

            # 会话持续时间
            if self._session_active:
                duration = time.monotonic() - self._start_time
            elif self._end_time > 0:
                duration = self._end_time - self._start_time
            else:
                duration = 0.0

            # 有效帧率
            effective_fps = (
                self._total_frames / duration
                if duration > 0 else 0.0
            )

            return {
                # P5-1: 解码统计（完整百分位）
                'decode_avg_ms': decode_stats['avg'],
                'decode_max_ms': decode_stats['max'],
                'decode_min_ms': decode_stats['min'],
                'decode_p50_ms': decode_stats['p50'],
                'decode_p95_ms': decode_stats['p95'],
                'decode_std_ms': decode_stats['std'],
                'decode_count': decode_stats['count'],

                # P5-1: 上传统计（完整百分位）
                'upload_avg_ms': upload_stats['avg'],
                'upload_max_ms': upload_stats['max'],
                'upload_min_ms': upload_stats['min'],
                'upload_p50_ms': upload_stats['p50'],
                'upload_p95_ms': upload_stats['p95'],
                'upload_std_ms': upload_stats['std'],
                'upload_count': upload_stats['count'],

                # 丢帧统计
                'total_drops': total_drops,
                'drop_rate': drop_rate,
                'drop_reasons': drop_reasons,

                # 会话统计
                'session_duration': duration,
                'frames_processed': self._total_frames,
                'effective_fps': effective_fps,

                # 元数据
                'name': self._name,
                'session_active': self._session_active,
            }

    def get_report(self) -> str:
        """
        获取文本格式的性能报告。

        Returns:
            格式化的性能报告字符串
        """
        stats = self.get_stats()

        lines = [
            f"=== 视频性能报告 [{self._name}] ===",
            "",
            "--- 解码性能 ---",
            f"  平均时间: {stats['decode_avg_ms']:.2f} ms",
            f"  最大时间: {stats['decode_max_ms']:.2f} ms",
            f"  最小时间: {stats['decode_min_ms']:.2f} ms",
            f"  P50 时间: {stats['decode_p50_ms']:.2f} ms",
            f"  P95 时间: {stats['decode_p95_ms']:.2f} ms",
            f"  标准差:   {stats['decode_std_ms']:.2f} ms",
            f"  样本数:   {stats['decode_count']}",
            "",
            "--- 上传性能 ---",
            f"  平均时间: {stats['upload_avg_ms']:.2f} ms",
            f"  最大时间: {stats['upload_max_ms']:.2f} ms",
            f"  最小时间: {stats['upload_min_ms']:.2f} ms",
            f"  P50 时间: {stats['upload_p50_ms']:.2f} ms",
            f"  P95 时间: {stats['upload_p95_ms']:.2f} ms",
            f"  标准差:   {stats['upload_std_ms']:.2f} ms",
            f"  样本数:   {stats['upload_count']}",
            "",
            "--- 丢帧统计 ---",
            f"  总丢帧:   {stats['total_drops']}",
            f"  丢帧率:   {stats['drop_rate']:.2%}",
        ]

        # 丢帧原因详情
        if stats['drop_reasons']:
            lines.append("  丢帧原因:")
            for reason, count in sorted(
                stats['drop_reasons'].items(),
                key=lambda x: x[1],
                reverse=True
            ):
                lines.append(f"    - {reason}: {count}")

        lines.extend([
            "",
            "--- 会话统计 ---",
            f"  持续时间: {stats['session_duration']:.2f} s",
            f"  处理帧数: {stats['frames_processed']}",
            f"  有效帧率: {stats['effective_fps']:.2f} fps",
            "",
            "=" * 40,
        ])

        return "\n".join(lines)

    def clear(self) -> None:
        """
        清空所有统计数据。

        保持会话状态不变，仅清空数据（含降级状态与全部计数器，
        与 start_session 的清理口径一致，见 _clear_data_locked）。
        """
        with self._lock:
            self._clear_data_locked()
            self._last_aggregate_time = time.monotonic()

            logger.debug(f"VideoPerformanceMonitor: 清空数据 '{self._name}'")

    def reset(self) -> None:
        """
        完全重置监控器。

        清空数据并重置会话状态。
        """
        with self._lock:
            self._clear_data_locked()
            self._start_time = 0.0
            self._end_time = 0.0
            self._session_active = False
            self._last_aggregate_time = 0.0

            logger.debug(f"VideoPerformanceMonitor: 重置 '{self._name}'")

    @property
    def name(self) -> str:
        """获取监控器名称"""
        return self._name

    @property
    def is_active(self) -> bool:
        """检查会话是否活动"""
        with self._lock:
            return self._session_active

    @property
    def frames_processed(self) -> int:
        """获取已处理的帧数"""
        with self._lock:
            return self._total_frames

    # ================================================================
    # 内部方法
    # ================================================================

    def _clear_data_locked(self) -> None:
        """
        清空全部统计数据、计数器与降级状态（调用方必须已持有 self._lock）。

        AUD-35405：此前 clear()/reset() 只清空了 start_session 字段的
        子集——S5.B 降级状态（_degradation_level/_consecutive_*_ticks/
        _prefetch_lookahead_reduction）、零拷贝计数、渲染队列/YUV 分段
        计时、代理阶梯状态等滞留，清空后聚合统计与 get_degradation_level()
        返回陈旧数据。start_session/clear/reset 统一走本方法，保证三者
        清理口径一致。
        """
        # 基础统计
        self._decode_times.clear()
        self._upload_times.clear()
        self._frame_drops.clear()
        self._total_frames = 0

        # P0.A: 播放性能统计数据
        self._playback_stats.clear()
        self._tick_times.clear()
        self._process_times.clear()
        self._render_submit_times.clear()
        self._queue_depths.clear()
        self._seek_times.clear()
        self._read_times.clear()
        self._cache_hits.clear()

        # S5.B: 渲染队列 / 预览丢帧
        self._renderer_queue_depths.clear()
        self._dropped_preview_frames = 0
        self._held_frames.clear()
        self._plane_prepare_times.clear()
        self._texture_write_times.clear()
        self._yuv_render_times.clear()
        self._frame_paths.clear()

        # 零拷贝追踪
        self._zero_copy_frames = 0
        self._cpu_download_frames = 0
        self._cpu_upload_frames = 0
        self._external_texture_frames = 0
        self._texture_write_video_frames = 0
        self._decode_path_counts.clear()
        self._render_frame_path_counts.clear()
        self._last_fallback_reason = None

        # S5.B: 自动降级状态
        self._degradation_level = 0
        self._last_tick_p95_high_time = 0.0
        self._consecutive_slow_ticks = 0
        self._consecutive_normal_ticks = 0
        self._prefetch_lookahead_reduction = 0.0

        # P2.2: 动态代理阶梯状态
        self._total_frame_times.clear()
        self._coalesced_video_commands.clear()
        self._proxy_ladder_index = -1
        self._current_proxy_long_edge = 0
        self._original_long_edge = 0
        self._consecutive_slow_frames = 0
        self._consecutive_fast_frames = 0

    def _append_bounded(self, values: List[Any], value: Any) -> None:
        """
        追加样本并保持列表有界（AUD-35404）。

        摊还 O(1)：仅当长度超过保留窗口两倍时一次性裁剪到最近
        _MAX_TRACKED_SAMPLES 条，避免每次追加都搬移整段内存。此后聚合
        统计（avg/p50/p95）基于最近 ~10 秒滚动窗口而非全会话累积。

        Args:
            values: 目标列表（调用方必须已持有 self._lock）
            value: 追加的样本
        """
        values.append(value)
        limit = self._MAX_TRACKED_SAMPLES
        if len(values) > limit * 2:
            del values[: len(values) - limit]

    def _count_frame_paths(self) -> Dict[str, int]:
        """统计帧路径分布"""
        counts: Dict[str, int] = {}
        for p in self._frame_paths:
            counts[p] = counts.get(p, 0) + 1
        return counts

    def _calculate_stats(self, values: List[float]) -> Dict[str, float]:
        """
        计算统计值。

        Args:
            values: 数值列表

        Returns:
            包含 avg, max, min, p50, p95, std 的字典

        Note:
            P5-1: 新增 p50（中位数）统计，用于更准确的性能分析。
            p50 代表典型性能，p95 代表接近最差性能。
        """
        if not values:
            return {
                'avg': 0.0,
                'max': 0.0,
                'min': 0.0,
                'p50': 0.0,
                'p95': 0.0,
                'std': 0.0,
                'count': 0,
            }

        sorted_values = sorted(values)
        n = len(sorted_values)

        # 平均值
        avg = sum(values) / n

        # 最大最小值
        max_val = sorted_values[-1]
        min_val = sorted_values[0]

        # P50（中位数）
        p50_index = int(n * 0.50)
        p50_index = min(p50_index, n - 1)  # 边界保护
        p50 = sorted_values[p50_index]

        # P95
        p95_index = int(n * 0.95)
        p95_index = min(p95_index, n - 1)  # 边界保护
        p95 = sorted_values[p95_index]

        # 标准差
        if n > 1:
            variance = sum((x - avg) ** 2 for x in values) / n
            std = variance ** 0.5
        else:
            std = 0.0

        return {
            'avg': avg,
            'max': max_val,
            'min': min_val,
            'p50': p50,
            'p95': p95,
            'std': std,
            'count': n,
        }

    def _calculate_int_stats(self, values: List[int]) -> Dict[str, float]:
        """
        计算整数列表的统计值。

        P5-1: 专门用于队列深度等整数统计。

        Args:
            values: 整数列表

        Returns:
            包含 avg, max, min, p50, p95 的字典
        """
        float_values = [float(v) for v in values]
        return self._calculate_stats(float_values)

    def __repr__(self) -> str:
        """字符串表示"""
        with self._lock:
            status = "active" if self._session_active else "inactive"
            return (
                f"VideoPerformanceMonitor("
                f"name='{self._name}', "
                f"frames={self._total_frames}, "
                f"drops={len(self._frame_drops)}, "
                f"status={status})"
            )


# ================================================================
# 上下文管理器
# ================================================================

@contextmanager
def track_decode_time(monitor: VideoPerformanceMonitor, frame_index: int):
    """
    解码时间追踪上下文管理器。

    自动测量代码块的执行时间并记录到监控器。

    Args:
        monitor: 性能监控器实例
        frame_index: 帧号

    Usage:
        with track_decode_time(monitor, frame_index=0):
            frame = decode_frame(...)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_decode_time(frame_index, elapsed)


@contextmanager
def track_upload_time(monitor: VideoPerformanceMonitor, frame_index: int):
    """
    上传时间追踪上下文管理器。

    自动测量代码块的执行时间并记录到监控器。

    Args:
        monitor: 性能监控器实例
        frame_index: 帧号

    Usage:
        with track_upload_time(monitor, frame_index=0):
            texture = upload_to_gpu(frame_data)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_upload_time(frame_index, elapsed)


# ================================================================
# P0.A 新增: 播放性能追踪上下文管理器
# ================================================================

@contextmanager
def track_playback_tick(monitor: VideoPerformanceMonitor):
    """
    UI Tick 时间追踪上下文管理器。

    自动测量 _on_play_tick() 执行时间。

    Args:
        monitor: 性能监控器实例

    Usage:
        with track_playback_tick(monitor):
            # _on_play_tick 逻辑
            pass
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_tick_time(elapsed)


@contextmanager
def track_frame_process(monitor: VideoPerformanceMonitor, frame_index: int):
    """
    帧处理时间追踪上下文管理器。

    自动测量帧处理（颜色转换、格式转换等）时间。

    Args:
        monitor: 性能监控器实例
        frame_index: 帧号

    Usage:
        with track_frame_process(monitor, frame_index):
            # 处理帧数据
            processed_frame = process_frame(raw_frame)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_process_time(frame_index, elapsed)


@contextmanager
def track_frame_seek(monitor: VideoPerformanceMonitor, frame_index: int):
    """
    Seek 时间追踪上下文管理器。

    自动测量 seek 操作时间。

    Args:
        monitor: 性能监控器实例
        frame_index: 目标帧号

    Usage:
        with track_frame_seek(monitor, frame_index):
            source.seek_frame(frame_index)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_seek_time(frame_index, elapsed)


@contextmanager
def track_frame_read(monitor: VideoPerformanceMonitor, frame_index: int):
    """
    Read 时间追踪上下文管理器。

    自动测量 read_frame 操作时间。

    Args:
        monitor: 性能监控器实例
        frame_index: 帧号

    Usage:
        with track_frame_read(monitor, frame_index):
            frame = source.read_frame(frame_index)
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = (time.perf_counter() - start) * 1000  # 转换为毫秒
        monitor.track_read_time(frame_index, elapsed)


# ================================================================
# 全局监控器实例
# ================================================================

_global_monitor: Optional[VideoPerformanceMonitor] = None
_global_lock = threading.Lock()


def get_performance_monitor() -> VideoPerformanceMonitor:
    """
    获取全局性能监控器实例。

    首次调用时创建实例，后续调用返回同一实例。

    Returns:
        VideoPerformanceMonitor 实例
    """
    global _global_monitor

    if _global_monitor is None:
        with _global_lock:
            # 双重检查锁定
            if _global_monitor is None:
                _global_monitor = VideoPerformanceMonitor(name='global_video_pipeline')

    return _global_monitor


def reset_global_monitor() -> None:
    """
    重置全局监控器。

    用于测试或重新初始化。
    """
    global _global_monitor

    with _global_lock:
        if _global_monitor is not None:
            _global_monitor.reset()
            _global_monitor = None
