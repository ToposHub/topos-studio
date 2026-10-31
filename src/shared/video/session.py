"""
Video Session Module
定义视频会话，管理播放状态、源管理器、解码工作线程和时钟。

## 职责（P2.B + P5.A）

- VideoSession: 视频会话管理器
- PlaybackState: 播放状态枚举
- SessionStats: 会话统计信息

## 设计原则

- 管理 path、metadata、source_manager、proxy worker、master worker、playback_clock
- 对 Color 和 Composite 暴露同一 FrameProvider
- preview 可丢帧，master 精确帧
- generation_id 管理 seek 后的帧丢弃
- stop/unload 后线程能在 2 秒内退出

## 使用示例

```python
from src.shared.video.session import VideoSession

# 创建视频会话
session = VideoSession()
session.load('/path/to/video.mp4')

# 开始播放
session.start_playback()

# 请求帧
frame = session.get_current_frame()

# seek
session.seek_to_frame(100)

# 停止
session.stop_playback()

# unload
session.unload()
```

## 与 EnhancedVideoPlaybackEngine 的关系

VideoSession 替代 EnhancedVideoPlaybackEngine 的核心管理功能：
- EnhancedVideoPlaybackEngine 变薄，只作为旧 UI 信号兼容层
- UI 调用仍保持：load_video, start_playback, stop_playback, seek_to_frame, next_frame, prev_frame
- MainWindow 不需要知道 worker/cache 细节
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.frame import DecodedFrame
    from ..media.metadata import VideoMetadata
    from .playback_clock import SourceManager, PlaybackClock
    from .frame_queue import ReadyFrameQueue
    from .decode_worker import DecodeWorkerManager
    from .color_frame_provider_adapter import ColorFrameProviderAdapter


logger = logging.getLogger(__name__)


# =============================================================================
# 播放状态
# =============================================================================

class PlaybackState(Enum):
    """
    播放状态枚举。

    Values:
        UNLOADED: 未加载视频
        LOADING: 正在加载
        READY: 已加载，准备播放
        PLAYING: 正在播放
        PAUSED: 暂停
        SEEKING: 正在 seek
        SCRUBBING: 正在 scrub（拖动）
        STOPPING: 正在停止
        ERROR: 错误状态
    """
    UNLOADED = auto()
    LOADING = auto()
    READY = auto()
    PLAYING = auto()
    PAUSED = auto()
    SEEKING = auto()
    SCRUBBING = auto()
    STOPPING = auto()
    ERROR = auto()


# =============================================================================
# 会话统计
# =============================================================================

@dataclass
class SessionStats:
    """
    会话统计信息。

    Attributes:
        total_frames_played: 累计播放帧数
        total_frames_dropped: 累计丢帧数
        total_seek_count: 累计 seek 次数
        total_play_time: 累计播放时间（秒）
        last_frame_decode_ms: 最近帧解码时间
        last_frame_display_ms: 最近帧显示时间
        avg_fps: 平均播放帧率
        current_fps: 当前播放帧率
    """

    total_frames_played: int = 0
    total_frames_dropped: int = 0
    total_seek_count: int = 0
    total_play_time: float = 0.0
    last_frame_decode_ms: float = 0.0
    last_frame_display_ms: float = 0.0
    avg_fps: float = 0.0
    current_fps: float = 0.0

    def update_play(self, display_ms: float) -> None:
        """更新播放统计"""
        self.total_frames_played += 1
        self.last_frame_display_ms = display_ms

    def update_drop(self) -> None:
        """更新丢帧统计"""
        self.total_frames_dropped += 1

    def update_seek(self) -> None:
        """更新 seek 统计"""
        self.total_seek_count += 1

    def update_fps(self, fps: float) -> None:
        """更新帧率"""
        self.current_fps = fps
        if self.total_frames_played > 0:
            self.avg_fps = (
                self.avg_fps * (self.total_frames_played - 1) + fps
            ) / self.total_frames_played

    def to_dict(self) -> dict[str, float | int]:
        """转换为字典"""
        return {
            'total_frames_played': self.total_frames_played,
            'total_frames_dropped': self.total_frames_dropped,
            'total_seek_count': self.total_seek_count,
            'total_play_time': self.total_play_time,
            'last_frame_decode_ms': self.last_frame_decode_ms,
            'last_frame_display_ms': self.last_frame_display_ms,
            'avg_fps': self.avg_fps,
            'current_fps': self.current_fps,
        }


# =============================================================================
# VideoSession: 视频会话管理器
# =============================================================================

class VideoSession:
    """
    视频会话管理器。

    实现 P2.B + P5.A 的 VideoSession 设计：
    - 管理 path、metadata、source_manager、proxy worker、master worker、playback_clock、generation_id
    - 对 Color 和 Composite 暴露同一 FrameProvider
    - preview 可丢帧，master 精确帧
    - EnhancedVideoPlaybackEngine 变薄，只作为旧 UI 信号兼容层

    Attributes:
        _path: 视频文件路径
        _state: 播放状态
        _source_manager: 源管理器
        _playback_clock: 播放时钟
        _frame_queue: 就绪帧队列
        _worker_manager: 解码工作线程管理器
        _generation: 当前 generation_id
        _stats: 会话统计
        _metadata: 视频元数据

    Note:
        stop/unload 后线程能在 2 秒内退出
        MainWindow 不需要知道 worker/cache 细节
    """

    def __init__(
        self,
        viewport_size: Optional[tuple[int, int]] = None,
        prefetch_lookahead: int = 8,
        max_proxy_queue: int = 12,
        max_master_queue: int = 3,
        use_shared_frame_provider: Optional[bool] = None,
    ) -> None:
        """
        初始化视频会话。

        Args:
            viewport_size: 视口大小（用于计算 proxy 分辨率）
            prefetch_lookahead: Proxy 预读帧数
            max_proxy_queue: Proxy 队列最大长度
            max_master_queue: Master 队列最大长度
            use_shared_frame_provider: 是否使用共享帧服务（S4.A）。
                None 表示从配置读取；True 表示强制使用；False 表示禁用。
                使用共享帧服务时，VideoSession 使用 ColorFrameProviderAdapter
                而不是 SourceManager，与 Composite 共享 source/cache。

        Note:
            S4.A: 第一版默认禁用，通过 feature flag 开启
        """
        self._path: Optional[str] = None
        self._state: PlaybackState = PlaybackState.UNLOADED
        self._state_lock: threading.Lock = threading.Lock()

        self._viewport_size: Optional[tuple[int, int]] = viewport_size
        self._prefetch_lookahead: int = prefetch_lookahead
        self._max_proxy_queue: int = max_proxy_queue
        self._max_master_queue: int = max_master_queue

        # P7.A: 共享帧服务配置
        # V-P1-4: 协议测试全绿前默认禁用，通过 Preferences 手动开启
        # Color、Composite、Export 共享同一 VideoFrameProviderService
        # 同一视频只打开一套 MediaSource，帧缓存共享，避免重复解码
        if use_shared_frame_provider is None:
            # V-P0-2 修复：禁止 `value or True`，使用显式 None 判断
            # 原问题：`False or True` 吃掉了 False，导致无法关闭
            from src.shared.config.config_manager import ConfigManager
            value = ConfigManager().get('video_color_use_shared_frame_provider')
            # 显式判断 None，而不是使用 `or`
            use_shared_frame_provider = value if value is not None else False
        self._use_shared_frame_provider: bool = use_shared_frame_provider

        if not self._use_shared_frame_provider:
            logger.info(
                "video_color_use_shared_frame_provider is False. "
                "Protocol parity for ColorFrameProviderAdapter is complete "
                "(open/close/request_proxy/request_master/prefetch_proxy/prefetch_master/"
                "get_proxy_frame/get_master_frame/request_proxy_frame/request_master_frame/"
                "metadata/fps/total_frames/duration). "
                "Consider enabling via config for shared clip sessions."
            )

        # 组件
        self._source_manager: Optional[SourceManager] = None
        # S4.A: 共享帧服务适配器
        self._frame_provider_adapter: Optional[ColorFrameProviderAdapter] = None
        self._playback_clock: Optional[PlaybackClock] = None
        self._frame_queue: Optional[ReadyFrameQueue] = None
        self._worker_manager: Optional[DecodeWorkerManager] = None

        # 状态
        self._generation: int = 0
        self._generation_lock: threading.Lock = threading.Lock()

        # 统计
        self._stats: SessionStats = SessionStats()

        # 元数据
        self._metadata: Optional[VideoMetadata] = None

        # 当前帧
        self._current_frame: int = 0
        self._current_master_frame: Optional[DecodedFrame] = None
        self._current_proxy_frame: Optional[DecodedFrame] = None
        self._last_displayed_proxy_frame_index: int = -1

        # 播放速度
        self._speed: float = 1.0

        # 回调
        self._on_frame_ready: Optional[Callable[[DecodedFrame, bool], None]] = None
        self._on_state_change: Optional[Callable[[PlaybackState], None]] = None

        # 降级状态
        self._degradation_level: int = 0
        self._last_degradation_reason: str = ""

        logger.debug("VideoSession initialized")

    # =============================================================================
    # 属性
    # =============================================================================

    @property
    def path(self) -> Optional[str]:
        """视频文件路径"""
        return self._path

    @property
    def state(self) -> PlaybackState:
        """播放状态"""
        with self._state_lock:
            return self._state

    @property
    def metadata(self) -> Optional[VideoMetadata]:
        """视频元数据"""
        return self._metadata

    @property
    def generation(self) -> int:
        """当前 generation_id"""
        with self._generation_lock:
            return self._generation

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        return self._current_frame

    @property
    def fps(self) -> Optional[float]:
        """帧率"""
        if self._metadata is not None:
            return self._metadata.fps.to_fps()
        return None

    @property
    def total_frames(self) -> Optional[int]:
        """总帧数"""
        if self._metadata is not None:
            return self._metadata.total_frames
        return None

    @property
    def duration(self) -> Optional[float]:
        """时长（秒）"""
        if self._metadata is not None:
            return self._metadata.duration
        return None

    @property
    def speed(self) -> float:
        """播放速度"""
        return self._speed

    @property
    def stats(self) -> SessionStats:
        """会话统计"""
        return self._stats

    @property
    def source_manager(self) -> Optional[SourceManager]:
        """源管理器"""
        return self._source_manager

    @property
    def frame_queue(self) -> Optional[ReadyFrameQueue]:
        """就绪帧队列"""
        return self._frame_queue

    @property
    def playback_clock(self) -> Optional[PlaybackClock]:
        """播放时钟"""
        return self._playback_clock

    @property
    def worker_manager(self) -> Optional[DecodeWorkerManager]:
        """解码工作线程管理器"""
        return self._worker_manager

    @property
    def frame_provider_adapter(self) -> Optional[ColorFrameProviderAdapter]:
        """
        共享帧服务适配器（S4.A）。

        Returns:
            ColorFrameProviderAdapter 或 None
        """
        return self._frame_provider_adapter

    @property
    def use_shared_frame_provider(self) -> bool:
        """是否使用共享帧服务"""
        return self._use_shared_frame_provider

    # =============================================================================
    # 加载/卸载
    # =============================================================================

    def load(self, path: str) -> bool:
        """
        加载视频文件。

        创建 PlaybackClock、ReadyFrameQueue、DecodeWorkerManager。

        Args:
            path: 视频文件路径

        Returns:
            True 表示成功加载

        Note:
            AUD-39412: 默认【不】使用共享帧服务（V-P1-4：协议测试全绿前
            默认禁用，use_shared_frame_provider 为 None 时取配置
            video_color_use_shared_frame_provider，未配置则 False，走独立
            SourceManager 模式）。开启后使用 ColorFrameProviderAdapter
            与 Composite 共享帧服务：同一视频只打开一套 MediaSource，
            帧缓存共享。
        """
        with self._state_lock:
            if self._state not in (PlaybackState.UNLOADED, PlaybackState.ERROR):
                logger.warning(f"Cannot load in state {self._state.name}")
                return False

            self._state = PlaybackState.LOADING

        # V-P0-11: 在 _state_lock 外通知，避免 callback 重入 session 方法时死锁。
        self._notify_state_change()

        self._path = path

        try:
            # P7.A: 根据配置选择帧服务模式
            if self._use_shared_frame_provider:
                # AUD-39412: 共享帧服务模式（默认【关闭】，经配置开启）
                # 使用 ColorFrameProviderAdapter，与 Composite 共享 VideoFrameProviderService
                from .color_frame_provider_adapter import ColorFrameProviderAdapter

                self._frame_provider_adapter = ColorFrameProviderAdapter()
                if not self._frame_provider_adapter.open(path):
                    raise RuntimeError(f"Failed to open adapter: {path}")

                # 获取元数据（从 adapter）
                self._metadata = self._frame_provider_adapter.metadata

                if self._metadata is None:
                    raise RuntimeError("Failed to get metadata from adapter")

                # 不创建独立 SourceManager - 使用共享服务
                # DecodeWorkerManager 使用 ColorFrameProviderAdapter 作为 frame_provider
                # Color 和 Composite 使用同一底层 ClipSession

                logger.info(
                    f"VideoSession loaded with shared frame provider: {path}, "
                    f"clip_id={self._frame_provider_adapter.clip_id}"
                )

            else:
                # 独立 SourceManager 模式（兼容旧路径，禁用共享）
                # 此模式下 Color 和 Composite 可能打开两套 MediaSource
                from .playback_clock import SourceManager
                from src.shared.media.proxy import ProxyConfig, DEFAULT_PROXY_CONFIG

                proxy_config = ProxyConfig(
                    strategy=DEFAULT_PROXY_CONFIG.strategy,
                    max_resolution=DEFAULT_PROXY_CONFIG.max_resolution,
                )

                self._source_manager = SourceManager(
                    path=path,
                    proxy_config=proxy_config,
                    viewport_size=self._viewport_size,
                    enable_planar_proxy=True,
                )

                # 打开源
                if not self._source_manager.open():
                    raise RuntimeError(f"Failed to open source: {path}")

                # 获取元数据
                self._metadata = self._source_manager.metadata

                if self._metadata is None:
                    raise RuntimeError("Failed to get metadata")

            # 创建 PlaybackClock
            from .playback_clock import PlaybackClock

            fps = self._metadata.fps.to_fps()
            total_frames = self._metadata.total_frames or 0

            self._playback_clock = PlaybackClock(
                fps=fps,
                total_frames=total_frames,
            )

            # 创建 ReadyFrameQueue
            from .frame_queue import ReadyFrameQueue

            self._frame_queue = ReadyFrameQueue(
                max_proxy_size=self._max_proxy_queue,
                max_master_size=self._max_master_queue,
            )

            # 创建 DecodeWorkerManager
            from .decode_worker import DecodeWorkerManager

            # P7.A: 根据配置选择 frame_provider
            if self._use_shared_frame_provider and self._frame_provider_adapter is not None:
                # 使用 ColorFrameProviderAdapter 作为 frame_provider（共享模式）
                self._worker_manager = DecodeWorkerManager(
                    frame_provider=self._frame_provider_adapter,
                    frame_queue=self._frame_queue,
                    prefetch_lookahead=self._prefetch_lookahead,
                )
            else:
                # 使用 SourceManager 作为 frame_provider（独立模式）
                self._worker_manager = DecodeWorkerManager(
                    frame_provider=self._source_manager,
                    frame_queue=self._frame_queue,
                    prefetch_lookahead=self._prefetch_lookahead,
                )

            # 创建工作线程
            if not self._worker_manager.create_workers():
                raise RuntimeError("Failed to create decode workers")

            # 重置状态
            self._current_frame = 0
            self._last_displayed_proxy_frame_index = -1
            self._generation = 0
            self._stats = SessionStats()

            with self._state_lock:
                self._state = PlaybackState.READY

            # V-P0-11: 在 _state_lock 外通知，避免 callback 重入死锁。
            self._notify_state_change()

            logger.info(
                f"VideoSession loaded: {path}, "
                f"{self._metadata.width}x{self._metadata.height}, "
                f"{fps:.2f}fps, {total_frames} frames"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to load video: {e}")
            self._cleanup()
            with self._state_lock:
                self._state = PlaybackState.ERROR
            # V-P0-11: 在 _state_lock 外通知。
            self._notify_state_change()
            return False

    def unload(self, timeout: float = 2.0) -> bool:
        """
        卸载视频。

        停止工作线程、释放资源。

        Args:
            timeout: 等待线程停止的超时（秒）

        Returns:
            True 表示成功卸载

        Note:
            设计要求：stop/unload 后线程能在 2 秒内退出
        """
        with self._state_lock:
            if self._state == PlaybackState.UNLOADED:
                return True

            if self._state == PlaybackState.PLAYING:
                self._state = PlaybackState.STOPPING

        # 停止工作线程
        if self._worker_manager is not None:
            self._worker_manager.stop_workers(timeout=timeout)

        # 清理资源
        self._cleanup()

        with self._state_lock:
            self._state = PlaybackState.UNLOADED

        # V-P0-11: 在 _state_lock 外通知。
        self._notify_state_change()

        logger.info("VideoSession unloaded")
        return True

    def _cleanup(self) -> None:
        """清理资源（内部方法）"""
        # S4.A: 关闭共享帧服务适配器
        if self._frame_provider_adapter is not None:
            self._frame_provider_adapter.close()
            self._frame_provider_adapter = None

        # 关闭源管理器
        if self._source_manager is not None:
            self._source_manager.close()
            self._source_manager = None

        # 清理队列
        if self._frame_queue is not None:
            self._frame_queue.clear_all()
            self._frame_queue = None

        # 清理时钟
        self._playback_clock = None

        # 清理工作线程管理器
        self._worker_manager = None

        # 清理元数据
        self._metadata = None
        self._path = None
        self._current_frame = 0
        self._current_master_frame = None
        self._current_proxy_frame = None
        self._last_displayed_proxy_frame_index = -1

    # =============================================================================
    # 播放控制
    # =============================================================================

    def start_playback(self) -> bool:
        """
        开始播放。

        Returns:
            True 表示成功开始
        """
        with self._state_lock:
            if self._state not in (PlaybackState.READY, PlaybackState.PAUSED):
                logger.warning(f"Cannot start playback in state {self._state.name}")
                return False

        # 启动工作线程
        if self._worker_manager is not None:
            self._worker_manager.start_workers()
            self._request_current_proxy_frame_if_missing(self._current_frame)
            self._worker_manager.update_clock_position(self._current_frame)

        # 启动时钟
        if self._playback_clock is not None:
            self._playback_clock.start()

        with self._state_lock:
            self._state = PlaybackState.PLAYING

        # V-P0-11: 在 _state_lock 外通知。
        self._notify_state_change()

        logger.info(f"Playback started at frame {self._current_frame}")
        return True

    def stop_playback(self, timeout: float = 2.0) -> bool:
        """
        停止播放。

        Args:
            timeout: 等待线程停止的超时（秒）

        Returns:
            True 表示成功停止

        Note:
            设计要求：stop/unload 后线程能在 2 秒内退出
        """
        with self._state_lock:
            if self._state not in (PlaybackState.PLAYING, PlaybackState.SEEKING, PlaybackState.SCRUBBING):
                return True

            self._state = PlaybackState.STOPPING

        # 停止时钟
        if self._playback_clock is not None:
            self._playback_clock.stop()

        # V-P0-06: stop_playback 在 pause/seek 中被高频调用。此前每次都
        # stop_workers() 销毁并重建线程，丢失热解码器/请求缓存，造成恢复延迟与
        # 线程抖动。改为只清空待处理请求、保留线程存活；只有 unload() 才彻底
        # 销毁（见 unload() 中的 stop_workers）。
        if self._worker_manager is not None:
            self._worker_manager.pause_workers()

        with self._state_lock:
            self._state = PlaybackState.PAUSED

        # V-P0-11: 在 _state_lock 外通知。
        self._notify_state_change()

        logger.info("Playback stopped")
        return True

    def pause(self) -> bool:
        """
        暂停播放。

        Returns:
            True 表示成功暂停
        """
        with self._state_lock:
            if self._state != PlaybackState.PLAYING:
                logger.warning(f"Cannot pause in state {self._state.name}")
                return False

        # 停止时钟
        if self._playback_clock is not None:
            self._playback_clock.stop()

        # 请求当前帧的 master 版本（暂停时显示高质量）
        self._request_master_frame(self._current_frame, reason='pause')

        with self._state_lock:
            self._state = PlaybackState.PAUSED

        # V-P0-11: 在 _state_lock 外通知。
        self._notify_state_change()

        logger.info(f"Playback paused at frame {self._current_frame}")
        return True

    def resume(self) -> bool:
        """
        恢复播放。

        Returns:
            True 表示成功恢复
        """
        return self.start_playback()

    # =============================================================================
    # 帧控制
    # =============================================================================

    def seek_to_frame(self, frame: int, *, blocking: bool = True, churn_generation: bool = True) -> bool:
        """
        Seek 到指定帧。

        S0.C 实现：使用 blocking 方法获取目标 master 帧。

        Args:
            frame: 目标帧号
            blocking: True 时同步等待 master 帧；False 时只更新时钟/请求队列，
                用于页面切换、播放态 scrub 等不能阻塞 UI 的路径。
            churn_generation: False 时跳过 generation 递增。高频拖拽（每
                8-16ms 一次 seek）若每次翻代数，40-80ms 才完成的 master 解码
                在入队时全部按"过期代数"被丢弃——队列永远为空，画面冻结在
                拖拽前的帧上。拖拽期保持代数稳定，完成的帧才能入队显示；
                松手后的精确 seek（settle）恢复默认翻代数以取得干净状态。

        Returns:
            True 表示成功 seek

        Note:
            AUD-39406: 无状态校验且恒返回 True 的旧行为已修复——
            UNLOADED / LOADING / ERROR 状态下没有可 seek 的媒体，
            必须返回 False 而不是假装成功。

            实现步骤：
            1. 校验状态（UNLOADED/LOADING/ERROR 拒绝）
            2. 停止播放（如果正在播放）
            3. 递增 generation（丢弃旧帧；scrub 热路径可跳过）
            4. Seek 时钟
            5. 可选 blocking 获取目标 master frame
            6. 更新当前帧
            7. 恢复 UI 状态（不自动恢复播放）
        """
        # AUD-39406: 状态校验——未加载/加载中/加载失败的会话没有可
        # seek 的媒体，旧行为无条件 return True 会让调用方以为 seek 成功。
        with self._state_lock:
            state = self._state
        if state in (PlaybackState.UNLOADED, PlaybackState.LOADING,
                     PlaybackState.ERROR):
            logger.warning(
                f"Cannot seek to frame {frame} in state {state.name}")
            return False

        total_frames = self.total_frames
        if total_frames is not None:
            frame = max(0, min(frame, total_frames - 1))

        with self._state_lock:
            old_state = self._state
            self._state = PlaybackState.SEEKING

        # 记录是否正在播放
        was_playing = old_state == PlaybackState.PLAYING

        # 如果正在播放，停止播放时钟
        if was_playing and self._playback_clock is not None:
            self._playback_clock.stop()

        # 递增 generation（丢弃旧帧）。scrub 高频路径跳过：见 churn_generation。
        if churn_generation:
            self._increment_generation()

        # Seek 时钟
        if self._playback_clock is not None:
            self._playback_clock.seek_to_frame(frame)

        master_frame = None
        if blocking:
            # S0.C: 使用 blocking 方法获取目标 master 帧
            master_frame = self.get_master_frame_blocking(frame, timeout_ms=300)
        else:
            self._request_current_proxy_frame_if_missing(frame)
            if self._worker_manager is not None:
                self._worker_manager.update_clock_position(frame)

        # 更新当前帧
        self._current_frame = frame
        self._last_displayed_proxy_frame_index = frame
        self._stats.update_seek()

        # Clear the proxy hold-frame so it cannot resurface after the seek.
        # tick()'s hold-last-frame fallback returns _current_proxy_frame when the
        # proxy queue is temporarily empty (common right after a clip switch
        # while the new clip's first proxy frame is still decoding).  Without
        # this reset the fallback would flash the PREVIOUS clip's last played
        # frame on the first few playback ticks after selecting a new clip.
        self._current_proxy_frame = None

        # 更新 master 帧（用于显示）
        if master_frame is not None:
            self._current_master_frame = master_frame
            logger.info(
                f"Seeked to frame {frame}, got master frame "
                f"(size={master_frame.width}x{master_frame.height})"
            )
        elif blocking:
            logger.warning(
                f"Seeked to frame {frame}, but failed to get master frame"
            )

        # S0.C: 第一版不自动恢复播放，保持 PAUSED/READY 状态
        # 如果之前在播放，变为 PAUSED；否则恢复原状态
        with self._state_lock:
            if was_playing:
                self._state = PlaybackState.PAUSED
            else:
                self._state = old_state

        # V-P0-11: 在 _state_lock 外通知。
        self._notify_state_change()

        return True

    def seek_to_time(self, seconds: float) -> bool:
        """
        Seek 到指定时间。

        Args:
            seconds: 目标时间（秒）

        Returns:
            True 表示成功 seek
        """
        fps = self.fps
        if fps is None:
            return False

        frame = int(seconds * fps)
        return self.seek_to_frame(frame)

    def next_frame(self) -> bool:
        """
        下一帧（逐帧播放）。

        Returns:
            True 表示成功切换
        """
        total_frames = self.total_frames
        if total_frames is None:
            return False

        if self._current_frame >= total_frames - 1:
            return False

        return self.seek_to_frame(self._current_frame + 1)

    def prev_frame(self) -> bool:
        """
        上一帧（逐帧播放）。

        Returns:
            True 表示成功切换
        """
        if self._current_frame <= 0:
            return False

        return self.seek_to_frame(self._current_frame - 1)

    def set_speed(self, speed: float) -> None:
        """
        设置播放速度。

        Args:
            speed: 速度倍数（1.0 = 正常）
        """
        self._speed = max(0.1, min(4.0, speed))

        if self._playback_clock is not None:
            self._playback_clock.set_speed(self._speed)

        logger.debug(f"Speed set to {speed}")

    # =============================================================================
    # 帧获取
    # =============================================================================

    def tick(self) -> Optional[DecodedFrame]:
        """
        时钟 tick，获取当前帧。

        用于播放循环，每次 tick 获取应该显示的帧。

        Returns:
            DecodedFrame 或 None

        Note:
            UI tick 不直接解码，从队列获取 ready frame
            没有 ready frame 就 hold last frame 或 drop
        """
        if self._playback_clock is None or self._frame_queue is None:
            return None

        # 时钟推进
        frame_to_display = self._playback_clock.tick()

        # 更新当前帧号
        self._current_frame = frame_to_display

        # A UI timer may tick more often than the media frame rate.  Until the
        # clock advances there is no new presentation request, so do not touch
        # the decode queue or count a normal front-frame hold as a drop.
        if frame_to_display == self._last_displayed_proxy_frame_index:
            return None

        # 先请求当前应显示帧，再触发未来帧预读。预读只覆盖 current+1..N，
        # 如果不补当前帧，播放 tick 会长期精确取不到帧。
        self._request_current_proxy_frame_if_missing(frame_to_display)

        # 触发预读
        if self._worker_manager is not None:
            self._worker_manager.update_clock_position(frame_to_display)

        # 从队列获取 proxy 帧：优先精确匹配，回退到 latest-before-or-at
        entry = self._frame_queue.dequeue(is_master=False, frame_index=frame_to_display)

        if entry is not None:
            frame = entry.frame
            actual_frame = entry.frame_index
            if actual_frame < self._last_displayed_proxy_frame_index:
                self._stats.update_drop()
                logger.debug(
                    f"tick({frame_to_display}): dropping late exact frame {actual_frame}, "
                    f"last_displayed={self._last_displayed_proxy_frame_index}"
                )
                return self._current_proxy_frame
            self._stats.update_play(entry.queue_wait_ms)
            self._stats.update_fps(1.0 / (entry.queue_wait_ms / 1000.0 if entry.queue_wait_ms > 0 else 0.04))
            self._current_proxy_frame = frame
            self._last_displayed_proxy_frame_index = actual_frame
            return frame

        # 精确匹配失败，获取不超过当前帧号的最新可用帧（出队并丢弃更旧帧）
        fallback = self._frame_queue.dequeue_latest_before_or_at(
            frame_index=frame_to_display,
            is_master=False,
        )
        if fallback.found and fallback.entry is not None:
            entry = fallback.entry
            actual_frame = fallback.actual_frame_index
            if actual_frame < self._last_displayed_proxy_frame_index:
                self._stats.update_drop()
                logger.debug(
                    f"tick({frame_to_display}): dropping late fallback frame {actual_frame}, "
                    f"last_displayed={self._last_displayed_proxy_frame_index}"
                )
                return self._current_proxy_frame
            self._stats.update_drop()  # 记录丢帧（非精确匹配）
            self._stats.update_play(entry.queue_wait_ms)
            self._current_proxy_frame = entry.frame
            self._last_displayed_proxy_frame_index = actual_frame
            logger.debug(
                f"tick({frame_to_display}): using fallback frame {actual_frame}, "
                f"discarded {fallback.older_frames_discarded} older frames"
            )
            return entry.frame

        # 完全没有帧，返回上一帧（hold last frame）。
        # Guard: 拒绝跨片段残留的 hold frame。_current_proxy_frame 可能是 seek
        # 之前旧片段的最后一帧（seek_to_frame 会重置为 None，但 defensive check
        # 防止任何遗漏路径）。帧号明显落后于当前播放位置时返回 None，让上层
        # hold 住画布上已显示的正确帧，而不是闪现旧片段画面。
        self._stats.update_drop()
        hold = self._current_proxy_frame
        if hold is not None:
            hold_idx = getattr(hold, 'frame_index', None)
            if hold_idx is not None and hold_idx < self._last_displayed_proxy_frame_index:
                logger.debug(
                    f"tick({frame_to_display}): hold frame {hold_idx} is stale "
                    f"(last_displayed={self._last_displayed_proxy_frame_index}), "
                    f"returning None to avoid cross-clip flash"
                )
                return None
        return hold

    def get_master_frame_blocking(
        self,
        frame_index: int,
        timeout_ms: int = 300,
    ) -> Optional[DecodedFrame]:
        """
        同步获取高质量 master 帧（用于首帧/seek/暂停）。

        S0.C 实现：确保首帧、seek、暂停能拿到 master 帧。

        Args:
            frame_index: 目标帧号
            timeout_ms: 等待超时（毫秒，默认 300ms）

        Returns:
            DecodedFrame 或 None

        Note:
            此方法仅用于用户主动操作（首帧、seek、暂停），不在播放 tick 热路径调用。

        规则：
            - 优先从 master queue/cache 取
            - 如果 worker 正在运行，发请求后等待最多 timeout_ms
            - 如果 worker 未运行，直接调用 SourceManager.get_master_frame()
            - 必须保证不在播放 tick 热路径调用

        P2-1: 播放状态下调用此方法是 bug！
        播放 tick 只能从 ReadyFrameQueue 取帧，不能阻塞等待 master 解码。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码
        from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
        _assert_no_sync_decode_in_playback(
            operation="VideoSession.get_master_frame_blocking",
            frame_index=frame_index,
            source_type="master_blocking",
        )

        # 优先从队列获取
        if self._frame_queue is not None:
            # 尝试精确帧号
            entry = self._frame_queue.dequeue(is_master=True, frame_index=frame_index)
            if entry is not None:
                self._current_master_frame = entry.frame
                logger.debug(
                    f"get_master_frame_blocking({frame_index}): "
                    f"got from queue (decode_ms={entry.decode_ms:.1f})"
                )
                return entry.frame

            # 尝试缓存的 master 帧
            if self._current_master_frame is not None:
                cached_index = self._current_master_frame.frame_index
                if cached_index == frame_index:
                    logger.debug(
                        f"get_master_frame_blocking({frame_index}): "
                        f"got from cached"
                    )
                    return self._current_master_frame

        # 检查 worker 是否运行
        worker_running = False
        if self._worker_manager is not None:
            master_worker = self._worker_manager.master_worker
            if master_worker is not None and master_worker.is_running():
                worker_running = True

        if worker_running:
            # Worker 运行中：发送请求并等待
            logger.debug(
                f"get_master_frame_blocking({frame_index}): "
                f"requesting from worker, timeout={timeout_ms}ms"
            )

            self._request_master_frame(frame_index, reason='pause')

            # 等待帧就绪
            wait_interval_ms = 10  # 每 10ms 检查一次
            max_attempts = timeout_ms // wait_interval_ms

            for attempt in range(max_attempts):
                if self._frame_queue is not None:
                    entry = self._frame_queue.dequeue(is_master=True, frame_index=frame_index)
                    if entry is not None:
                        self._current_master_frame = entry.frame
                        logger.debug(
                            f"get_master_frame_blocking({frame_index}): "
                            f"got after {(attempt + 1) * wait_interval_ms}ms"
                        )
                        return entry.frame

                time.sleep(wait_interval_ms / 1000.0)

            # 超时，尝试 fallback
            logger.warning(
                f"get_master_frame_blocking({frame_index}): "
                f"timeout after {timeout_ms}ms, falling back to SourceManager"
            )

        # Worker 未运行或超时：直接从 SourceManager 获取
        # 但播放态禁止同步回退，避免阻塞 UI
        if self._state == PlaybackState.PLAYING:
            logger.debug(
                f"get_master_frame_blocking({frame_index}): "
                f"skipping SourceManager fallback during playback"
            )
            return None

        if self._source_manager is not None:
            frame = self._source_manager.get_master_frame(frame_index)
            if frame is not None:
                self._current_master_frame = frame
                logger.debug(
                    f"get_master_frame_blocking({frame_index}): "
                    f"got from SourceManager directly"
                )
                return frame

        logger.warning(
            f"get_master_frame_blocking({frame_index}): "
            f"failed to get master frame"
        )
        return None

    def request_master_frame(self, frame_index: int) -> None:
        """Request a master frame without waiting on the caller's thread."""
        # Static Color previews are requested while paused.  Ensure their
        # dedicated worker exists, but do not start proxy prefetch just to
        # obtain a single original-quality frame.
        if self._worker_manager is not None and self._state != PlaybackState.PLAYING:
            self._worker_manager.start_master_worker()
        self._request_master_frame(int(frame_index), reason='pause')

    def take_ready_master_frame(self, frame_index: int) -> Optional[DecodedFrame]:
        """Return an exact decoded master frame if it is already available."""
        frame_index = int(frame_index)
        if (
            self._current_master_frame is not None
            and self._current_master_frame.frame_index == frame_index
        ):
            return self._current_master_frame

        if self._frame_queue is None:
            return None
        entry = self._frame_queue.dequeue(is_master=True, frame_index=frame_index)
        if entry is None:
            return None
        self._current_master_frame = entry.frame
        return entry.frame

    def take_latest_ready_master_frame(
        self,
        current_target: int,
        *,
        max_frame_distance: int = 96,
    ) -> Optional[DecodedFrame]:
        """Return the newest completed master frame as scrub interim feedback.

        高频拖拽中精确目标总在解码完成前移走（take_ready 永远 miss）。
        取而代之：取队列里最新完成的 master 帧显示——画面以解码速率步进，
        而不是冻结。距离上限防止大跳变后闪现久远位置的旧帧。
        """
        if self._frame_queue is None:
            return None
        newest = None
        while True:
            entry = self._frame_queue.dequeue(is_master=True)
            if entry is None:
                break
            newest = entry
        if newest is None:
            return None
        frame_index = int(getattr(newest.frame, "frame_index", -1))
        if abs(frame_index - int(current_target)) > int(max_frame_distance):
            return None
        self._current_master_frame = newest.frame
        return newest.frame

    def get_current_frame(self, is_master: bool = False) -> Optional[DecodedFrame]:
        """
        获取当前帧。

        Args:
            is_master: 是否获取高质量 master 帧

        Returns:
            DecodedFrame 或 None

        Note:
            S0.C: is_master=True 时使用 blocking 方法，
            确保首帧/暂停能拿到高质量帧。
        """
        if is_master:
            cached = self._current_master_frame
            if cached is not None and getattr(cached, "frame_index", None) == self._current_frame:
                return cached
            # S0.C: 使用 blocking 方法获取 master 帧
            return self.get_master_frame_blocking(self._current_frame, timeout_ms=300)

        else:
            # 返回 proxy 帧
            return self._current_proxy_frame

    def get_frame_at(self, frame_index: int, is_master: bool = False) -> Optional[DecodedFrame]:
        """
        获取指定帧。

        Args:
            frame_index: 目标帧号
            is_master: 是否获取高质量 master 帧

        Returns:
            DecodedFrame 或 None

        P2-1: is_master=True 时会阻塞等待解码，播放状态下调用是 bug。
        播放 tick 只能从 ReadyFrameQueue 取 proxy 帧，不能获取 master 帧。
        """
        # P2-1: 检查播放状态下是否违规调用同步解码（特别是 master 帧获取）
        if is_master:
            from src.shared.video.video_playback import _assert_no_sync_decode_in_playback
            _assert_no_sync_decode_in_playback(
                operation="VideoSession.get_frame_at(master=True)",
                frame_index=frame_index,
                source_type="master",
            )

        if is_master:
            self._request_master_frame(frame_index, reason='composite')

            if self._frame_queue is not None:
                # 等待帧就绪（最多等待 300ms）。AUD-39202 对齐：用
                # dequeue（所有权转移）而不是 peek——peek 返回的是仍在
                # 队列中的快照，队列后续淘汰会释放其 GPU 资源。
                for _ in range(30):
                    entry = self._frame_queue.dequeue(
                        is_master=True, frame_index=frame_index)
                    if entry is not None:
                        self._current_master_frame = entry.frame
                        return entry.frame
                    time.sleep(0.01)

            # AUD-39405: 与 get_master_frame_blocking 对称——播放态
            # 禁止同步回退解码（会把播放热路径拖死在母版解码上）。
            if self._state == PlaybackState.PLAYING:
                logger.debug(
                    f"get_frame_at({frame_index}): skipping synchronous "
                    f"master fallback during playback"
                )
                return None

            # 超时，从源管理器直接获取
            if self._source_manager is not None:
                frame = self._source_manager.get_master_frame(frame_index)
                if frame is not None:
                    self._current_master_frame = frame
                return frame

            # AUD-39405: 共享帧服务模式（无独立 SourceManager）走
            # adapter 的 master 读取（命中共享服务缓存时零解码），
            # 不再无 fallback 直接返回 None。
            if (self._use_shared_frame_provider
                    and self._frame_provider_adapter is not None):
                frame = self._frame_provider_adapter.get_master_frame(
                    frame_index)
                if frame is not None:
                    self._current_master_frame = frame
                return frame

        else:
            # Proxy 帧
            if self._frame_queue is not None:
                entry = self._frame_queue.dequeue(is_master=False, frame_index=frame_index)
                if entry is not None:
                    return entry.frame

            # 从源管理器获取
            if self._source_manager is not None:
                return self._source_manager.get_proxy_frame(frame_index)

        return None

    # =============================================================================
    # 内部方法
    # =============================================================================

    def _increment_generation(self) -> int:
        """
        递增 generation_id。

        Returns:
            新的 generation_id
        """
        with self._generation_lock:
            self._generation += 1

            # 同步到队列和工作线程
            if self._frame_queue is not None:
                self._frame_queue.increment_generation()

            if self._worker_manager is not None:
                self._worker_manager.update_generation(self._generation)

            return self._generation

    def _request_master_frame(
        self,
        frame_index: int,
        reason: str = 'seek',
    ) -> None:
        """
        请求 master 帧。

        Args:
            frame_index: 目标帧号
            reason: 请求原因
        """
        from .frame_request import FrameRequestReason

        reason_enum = {
            'seek': FrameRequestReason.SEEK,
            'pause': FrameRequestReason.SEEK,
            'step': FrameRequestReason.SEEK,
            'scrub': FrameRequestReason.SCRUB,
            'composite': FrameRequestReason.COMPOSITE,
            'export': FrameRequestReason.EXPORT,
        }.get(reason, FrameRequestReason.SEEK)

        if self._worker_manager is not None:
            self._worker_manager.request_master_frame(
                frame_index=frame_index,
                reason=reason_enum,
            )

    def _request_proxy_frame(
        self,
        frame_index: int,
        reason: str = 'playback',
    ) -> None:
        """
        请求 proxy 帧。

        Args:
            frame_index: 目标帧号
            reason: 请求原因
        """
        from .frame_request import FrameRequestReason

        reason_enum = {
            'playback': FrameRequestReason.PLAYBACK,
            'scrub': FrameRequestReason.SCRUB,
        }.get(reason, FrameRequestReason.PLAYBACK)

        if self._worker_manager is not None:
            self._worker_manager.request_proxy_frame(
                frame_index=frame_index,
                reason=reason_enum,
            )

    def _request_current_proxy_frame_if_missing(self, frame_index: int) -> None:
        """
        请求当前播放帧的 proxy 数据。

        Playback tick 从 ready queue 精确取 frame_index；而预读逻辑只请求
        frame_index 之后的帧，因此当前帧必须单独请求。
        """
        if self._worker_manager is None:
            return

        if self._frame_queue is not None:
            try:
                if self._frame_queue.contains(frame_index, is_master=False):
                    return
            except Exception as exc:
                logger.debug(f"Failed to inspect proxy queue for frame {frame_index}: {exc}")

        self._request_proxy_frame(frame_index, reason='playback')

    def _notify_state_change(self) -> None:
        """通知状态变化

        Note: MUST be called OUTSIDE _state_lock（V-P0-11）。回调内部可能
        重入 session 的公共方法（is_playing/fps 等）再次获取 _state_lock，
        持锁触发会在同线程二次获取非重入锁直接自死锁。读取状态用
        self._state 直取而非 self.state 属性，避免无谓的加锁。
        本方法全部调用点（load/unload/start/stop/pause/resume/seek 系列）
        均为锁外调用。
        """
        if self._on_state_change is not None:
            try:
                self._on_state_change(self._state)
            except Exception as e:
                logger.warning(f"State change callback error: {e}")

    # =============================================================================
    # 回调设置
    # =============================================================================

    def set_on_frame_ready(
        self,
        callback: Optional[Callable[[DecodedFrame, bool], None]],
    ) -> None:
        """
        设置帧就绪回调。

        Args:
            callback: 回调函数（frame, is_master）
        """
        self._on_frame_ready = callback

    def set_on_state_change(
        self,
        callback: Optional[Callable[[PlaybackState], None]],
    ) -> None:
        """
        设置状态变化回调。

        Args:
            callback: 回调函数（state）
        """
        self._on_state_change = callback

    # =============================================================================
    # 统计和调试
    # =============================================================================

    def get_stats(self) -> dict[str, dict[str, float | int]]:
        """
        获取完整统计信息。

        Returns:
            统计信息字典
        """
        stats = {
            'session': self._stats.to_dict(),
        }

        if self._worker_manager is not None:
            stats['workers'] = self._worker_manager.get_stats()

        if self._frame_queue is not None:
            stats['queue'] = self._frame_queue.stats

        stats['generation'] = self.generation
        stats['state'] = self.state.name

        return stats

    def apply_playback_degradation(
        self,
        level: int,
        lookahead_reduction: float,
    ) -> dict:
        """
        应用播放降级策略。

        当播放性能不足时，UI 层调用此方法降低解码压力。

        P2.2: 同时考虑代理阶梯的动态调整。当 performance_monitor
        检测到持续的帧耗时超标时，会自动降低代理分辨率；
        当恢复后逐步回升。

        Args:
            level: 降级级别（0=正常，1=轻度，2=中度，3=重度）
            lookahead_reduction: 预读减少比例（0.0-1.0）

        Returns:
            dict: 降级后的有效值
                - degradation_level: int
                - effective_prefetch_lookahead: int
                - effective_proxy_queue_size: int
                - last_degradation_reason: str
                - proxy_long_edge: int (P2.2 新增)

        Note:
            内部调用 worker_manager.set_prefetch_lookahead()
            记录降级状态到 _degradation_level 和 _last_degradation_reason
        """
        # 验证参数
        level = max(0, min(3, level))
        lookahead_reduction = max(0.0, min(1.0, lookahead_reduction))

        # 计算有效的预读帧数
        effective_lookahead = max(
            1,
            int(self._prefetch_lookahead * (1.0 - lookahead_reduction))
        )

        # 生成降级原因描述
        reasons = {
            0: "normal",
            1: "light_degradation",
            2: "medium_degradation",
            3: "heavy_degradation",
        }
        reason = reasons.get(level, "unknown")

        # 更新内部状态
        self._degradation_level = level
        self._last_degradation_reason = reason

        # 应用到 worker_manager
        if self._worker_manager is not None:
            self._worker_manager.set_prefetch_lookahead(effective_lookahead)

        # P2.2: 动态代理阶梯 - 通过 performance_monitor 查询当前阶梯状态
        proxy_long_edge = 0
        perf_monitor = self._get_performance_monitor()
        if perf_monitor is not None:
            ladder_info = perf_monitor.get_proxy_ladder_info()
            proxy_long_edge = ladder_info.get('proxy_long_edge', 0)
            ladder_index = ladder_info.get('ladder_index', -1)

            if proxy_long_edge > 0 and ladder_index >= 0:
                reason += f"+proxy_ladder({ladder_index}:{proxy_long_edge}p)"

        logger.info(
            f"Playback degradation applied: level={level}, "
            f"lookahead={effective_lookahead} (was {self._prefetch_lookahead}), "
            f"reason={reason}"
        )

        return {
            'degradation_level': level,
            'effective_prefetch_lookahead': effective_lookahead,
            'effective_proxy_queue_size': self._max_proxy_queue,
            'last_degradation_reason': reason,
            'proxy_long_edge': proxy_long_edge,
        }

    def _get_performance_monitor(self):
        """
        获取全局性能监控器实例。

        延迟导入避免循环依赖。

        Returns:
            VideoPerformanceMonitor 实例或 None
        """
        try:
            from src.shared.media.performance_monitor import get_performance_monitor
            return get_performance_monitor()
        except Exception:
            return None

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"VideoSession(path={self._path}, "
            f"state={self._state.name}, "
            f"frame={self._current_frame}, "
            f"gen={self._generation})"
        )
