"""
Playback Coordinator Module
播放协调器，整合播放调度，作为 UI 和底层播放管线的中间层。

## 职责（P5.A）

- PlaybackCoordinator: 播放协调器，整合 VideoSession 和 UI 交互
- 播放控制代理：将 UI 命令转发给 VideoSession
- 信号适配：将 VideoSession 状态变化转换为 Qt 信号
- 性能监控：聚合播放统计数据

## 设计原则

- EnhancedVideoPlaybackEngine 变薄，只作为旧 UI 信号兼容层
- PlaybackCoordinator 管理 VideoSession 和播放调度
- UI 调用接口保持不变
- MainWindow 不需要知道 worker/cache 细节
- 旧控制栏信号不变

## 与 EnhancedVideoPlaybackEngine 的关系

PlaybackCoordinator 是实际播放管理的核心：
- 管理 VideoSession、SourceManager、PlaybackClock、DecodeWorkerManager
- 处理播放控制命令（播放/暂停/跳转）
- 协调解码工作线程和帧队列

EnhancedVideoPlaybackEngine 变薄为 UI 信号兼容层：
- 保留原有的 PyQt 信号（frame_changed, frame_ready, play_state_changed）
- 内部委托给 PlaybackCoordinator
- 保持 UI 接口不变（load_video, start_playback, stop_playback, seek_to_frame）
- 负责与 Canvas/MainWindow 的 Qt 信号通信

## 使用示例

```python
from src.shared.video.playback_state import PlaybackCoordinator

# 创建协调器
coordinator = PlaybackCoordinator()

# 加载视频
coordinator.load('/path/to/video.mp4')

# 开始播放
coordinator.start_playback()

# 帧控制
coordinator.seek_to_frame(100)
coordinator.next_frame()

# 获取统计
stats = coordinator.get_stats()
```
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Callable, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from ..media.metadata import VideoMetadata
    from .session import VideoSession
    from .playback_clock import SourceManager
    from .frame_queue import ReadyFrameQueue
    from .decode_worker import DecodeWorkerManager


logger = logging.getLogger(__name__)


# =============================================================================
# 播放命令类型
# =============================================================================

class PlaybackCommand(Enum):
    """
    播放命令类型。

    用于 UI 到 PlaybackCoordinator 的命令传递。

    Values:
        LOAD: 加载视频
        UNLOAD: 卸载视频
        START: 开始播放
        STOP: 停止播放
        PAUSE: 暂停播放
        RESUME: 恢复播放
        SEEK: 跳转到帧
        NEXT_FRAME: 下一帧
        PREV_FRAME: 上一帧
        FIRST_FRAME: 第一帧
        LAST_FRAME: 最后一帧
        SET_SPEED: 设置播放速度
    """
    LOAD = auto()
    UNLOAD = auto()
    START = auto()
    STOP = auto()
    PAUSE = auto()
    RESUME = auto()
    SEEK = auto()
    NEXT_FRAME = auto()
    PREV_FRAME = auto()
    FIRST_FRAME = auto()
    LAST_FRAME = auto()
    SET_SPEED = auto()


# =============================================================================
# 播放命令数据
# =============================================================================

@dataclass
class PlaybackCommandData:
    """
    播放命令数据。

    封装命令及其参数。

    Attributes:
        command: 命令类型
        path: 视频路径（LOAD 命令）
        frame_index: 目标帧号（SEEK 命令）
        speed: 播放速度（SET_SPEED 命令）
        timestamp: 命令时间戳
    """

    command: PlaybackCommand
    path: Optional[str] = None
    frame_index: Optional[int] = None
    speed: Optional[float] = None
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# 播放协调器统计
# =============================================================================

@dataclass
class CoordinatorStats:
    """
    播放协调器统计信息。

    Attributes:
        command_count: 命令处理次数
        error_count: 错误次数
        avg_command_latency_ms: 平均命令延迟
        last_command_latency_ms: 最近命令延迟
    """

    command_count: int = 0
    error_count: int = 0
    avg_command_latency_ms: float = 0.0
    last_command_latency_ms: float = 0.0

    def update_command(self, latency_ms: float) -> None:
        """
        更新命令统计。

        Args:
            latency_ms: 命令处理延迟（毫秒）
        """
        self.command_count += 1
        self.last_command_latency_ms = latency_ms
        if self.command_count > 0:
            self.avg_command_latency_ms = (
                self.avg_command_latency_ms * (self.command_count - 1) + latency_ms
            ) / self.command_count

    def update_error(self) -> None:
        """更新错误统计"""
        self.error_count += 1

    def to_dict(self) -> dict[str, float | int]:
        """转换为字典"""
        return {
            'command_count': self.command_count,
            'error_count': self.error_count,
            'avg_command_latency_ms': self.avg_command_latency_ms,
            'last_command_latency_ms': self.last_command_latency_ms,
        }


# =============================================================================
# PlaybackCoordinator: 播放协调器
# =============================================================================

class PlaybackCoordinator:
    """
    播放协调器。

    实现 P5.A 的 PlaybackCoordinator 设计：
    - 整合 VideoSession 进行播放管理
    - 提供播放控制命令接口
    - 协调解码工作线程和帧队列
    - 聚合播放统计数据

    作为 EnhancedVideoPlaybackEngine 和 VideoSession 之间的桥梁：
    - EnhancedVideoPlaybackEngine 保留 UI 信号兼容层职责
    - PlaybackCoordinator 实际执行播放控制
    - VideoSession 管理底层播放管线

    Attributes:
        _session: VideoSession 实例
        _stats: 协调器统计
        _command_history: 最近命令历史（用于调试）
        _on_frame_ready: 帧就绪回调
        _on_state_change: 状态变化回调

    Note:
        MainWindow 不需要知道 worker/cache 细节
        旧控制栏信号通过 EnhancedVideoPlaybackEngine 保持
    """

    def __init__(
        self,
        viewport_size: Optional[tuple[int, int]] = None,
        prefetch_lookahead: int = 8,
        max_proxy_queue: int = 12,
        max_master_queue: int = 3,
    ) -> None:
        """
        初始化播放协调器。

        Args:
            viewport_size: 视口大小（用于计算 proxy 分辨率）
            prefetch_lookahead: Proxy 预读帧数
            max_proxy_queue: Proxy 队列最大长度
            max_master_queue: Master 队列最大长度
        """
        self._session: Optional[VideoSession] = None
        self._session_lock: threading.Lock = threading.Lock()

        self._viewport_size: Optional[tuple[int, int]] = viewport_size
        self._prefetch_lookahead: int = prefetch_lookahead
        self._max_proxy_queue: int = max_proxy_queue
        self._max_master_queue: int = max_master_queue

        # 统计
        self._stats: CoordinatorStats = CoordinatorStats()

        # 命令历史（最近 10 条）
        self._command_history: list[PlaybackCommandData] = []
        self._command_history_lock: threading.Lock = threading.Lock()

        # 回调
        # AUD-39403: 回调字段的写入/清空/读取统一经 _callback_lock。
        # （原 _tick_callback/_tick_lock 为 S0.B 遗留死代码，从未被读取，
        # 已删除。）
        self._callback_lock: threading.Lock = threading.Lock()
        self._on_frame_ready: Optional[Callable[[Any, int], None]] = None
        self._on_state_change: Optional[Callable[[str], None]] = None

        logger.debug("PlaybackCoordinator initialized")

    # =============================================================================
    # 属性
    # =============================================================================

    @property
    def session(self) -> Optional[VideoSession]:
        """VideoSession 实例"""
        with self._session_lock:
            return self._session

    @property
    def stats(self) -> CoordinatorStats:
        """协调器统计"""
        return self._stats

    @property
    def is_loaded(self) -> bool:
        """是否已加载视频

        AUD-39401: ERROR 状态必须排除——session.load() 失败后会话停留在
        ERROR，此前此属性仍返回 True，UI 按已加载逻辑继续操作（seek/
        取帧全部空转）。LOADING 属于瞬时中间态，load() 是同步方法，
        返回时已离开 LOADING，无需特判。
        """
        with self._session_lock:
            if self._session is not None:
                from .session import PlaybackState
                return self._session.state not in (
                    PlaybackState.UNLOADED, PlaybackState.ERROR)
            return False

    @property
    def is_playing(self) -> bool:
        """是否正在播放"""
        with self._session_lock:
            if self._session is not None:
                from .session import PlaybackState
                return self._session.state == PlaybackState.PLAYING
            return False

    @property
    def current_frame(self) -> int:
        """当前帧号"""
        with self._session_lock:
            if self._session is not None:
                return self._session.current_frame
            return 0

    @property
    def total_frames(self) -> Optional[int]:
        """总帧数"""
        with self._session_lock:
            if self._session is not None:
                return self._session.total_frames
            return None

    @property
    def fps(self) -> Optional[float]:
        """帧率"""
        with self._session_lock:
            if self._session is not None:
                return self._session.fps
            return None

    @property
    def duration(self) -> Optional[float]:
        """时长（秒）"""
        with self._session_lock:
            if self._session is not None:
                return self._session.duration
            return None

    @property
    def path(self) -> Optional[str]:
        """当前视频路径"""
        with self._session_lock:
            if self._session is not None:
                return self._session.path
            return None

    @property
    def metadata(self) -> Optional[VideoMetadata]:
        """视频元数据"""
        with self._session_lock:
            if self._session is not None:
                return self._session.metadata
            return None

    @property
    def generation(self) -> int:
        """当前 generation_id"""
        with self._session_lock:
            if self._session is not None:
                return self._session.generation
            return 0

    @property
    def source_manager(self) -> Optional[SourceManager]:
        """源管理器"""
        with self._session_lock:
            if self._session is not None:
                return self._session.source_manager
            return None

    @property
    def frame_queue(self) -> Optional[ReadyFrameQueue]:
        """就绪帧队列"""
        with self._session_lock:
            if self._session is not None:
                return self._session.frame_queue
            return None

    @property
    def worker_manager(self) -> Optional[DecodeWorkerManager]:
        """解码工作线程管理器"""
        with self._session_lock:
            if self._session is not None:
                return self._session.worker_manager
            return None

    # =============================================================================
    # 加载/卸载
    # =============================================================================

    def _detach_session(self) -> Optional[VideoSession]:
        """取下并返回当前 session（持锁；AUD-39400 辅助）。"""
        with self._session_lock:
            session = self._session
            self._session = None
            return session

    def _peek_session(self) -> Optional[VideoSession]:
        """获取当前 session 引用（持锁读取，不持有锁返回；AUD-39400 辅助）。"""
        with self._session_lock:
            return self._session

    def load(self, path: str) -> bool:
        """
        加载视频文件。

        创建 VideoSession 并初始化播放管线。

        Args:
            path: 视频文件路径

        Returns:
            True 表示成功加载

        Note:
            AUD-39400: session.load() 会在 LOADING/READY/ERROR 等状态切换时
            同步触发 ``_on_session_state_change`` 回调。此前整个调用都
            持有 ``_session_lock``——Qt 直连槽一旦重入协调器属性
            （is_loaded / total_frames 等）就会在同线程二次获取非重入锁，
            直接自死锁。现在锁内只做 session 指针切换，重活（旧会话
            unload、新会话 load）全部在锁外执行。
        """
        start_time = time.perf_counter()

        # AUD-39400: 锁内取下旧 session、挂上新 session（纯指针操作，
        # 不触发任何回调），重活放锁外。
        old_session = self._detach_session()

        from .session import VideoSession

        new_session = VideoSession(
            viewport_size=self._viewport_size,
            prefetch_lookahead=self._prefetch_lookahead,
            max_proxy_queue=self._max_proxy_queue,
            max_master_queue=self._max_master_queue,
        )
        new_session.set_on_state_change(self._on_session_state_change)
        with self._session_lock:
            self._session = new_session

        # 锁外：旧会话卸载（其回调可能重入协调器）
        if old_session is not None:
            old_session.unload()

        # 锁外：新会话加载（LOADING→READY/ERROR 两次回调都在无锁状态触发）
        result = new_session.load(path)

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.LOAD,
            path=path,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        if result:
            logger.info(f"PlaybackCoordinator loaded: {path}")
        else:
            logger.error(f"PlaybackCoordinator load failed: {path}")
            self._stats.update_error()

        return result

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
            AUD-39400: session.unload() 的状态回调在 _session_lock 外触发。
        """
        start_time = time.perf_counter()

        session = self._detach_session()
        if session is None:
            return True

        result = session.unload(timeout=timeout)

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.UNLOAD,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        logger.info("PlaybackCoordinator unloaded")
        return result

    # =============================================================================
    # 播放控制
    # =============================================================================

    def start_playback(self) -> bool:
        """
        开始播放。

        Returns:
            True 表示成功开始

        Note:
            AUD-39400: session.start_playback() 的状态回调在 _session_lock
            外触发（同批修复 pause/resume/stop/seek 系列方法）。
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            logger.warning("Cannot start playback: no session")
            return False

        result = session.start_playback()

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.START,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def stop_playback(self, timeout: float = 2.0) -> bool:
        """
        停止播放。

        Args:
            timeout: 等待线程停止的超时（秒）

        Returns:
            True 表示成功停止

        Note:
            设计要求：stop/unload 后线程能在 2 秒内退出
            AUD-39400: 状态回调在 _session_lock 外触发。
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return True

        result = session.stop_playback(timeout=timeout)

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.STOP,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def pause(self) -> bool:
        """
        暂停播放。

        Returns:
            True 表示成功暂停

        Note:
            AUD-39400: 状态回调在 _session_lock 外触发。
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return False

        result = session.pause()

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.PAUSE,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def resume(self) -> bool:
        """
        恢复播放。

        Returns:
            True 表示成功恢复

        Note:
            AUD-39400: 状态回调在 _session_lock 外触发。
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return False

        result = session.resume()

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.RESUME,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def toggle_play(self) -> bool:
        """
        切换播放/暂停状态。

        Returns:
            True 表示成功切换
        """
        if self.is_playing:
            return self.pause()
        else:
            return self.start_playback()

    # =============================================================================
    # 帧控制
    # =============================================================================

    def seek_to_frame(self, frame: int, *, blocking: bool = True, churn_generation: bool = True) -> bool:
        """
        Seek 到指定帧。

        Args:
            frame: 目标帧号
            blocking: True 时同步等待 master 帧；False 时只更新播放时钟和
                worker 请求，用于播放态/页面切换热路径。
            churn_generation: False 时 seek 不递增 generation（scrub 高频
                路径：翻代数会让慢于请求节奏的 master 解码在入队时全部
                被丢为过期帧，画面冻结）。

        Returns:
            True 表示成功 seek
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return False

        result = session.seek_to_frame(
            frame, blocking=blocking, churn_generation=churn_generation
        )

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.SEEK,
            frame_index=frame,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

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
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return False

        result = session.next_frame()

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.NEXT_FRAME,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def prev_frame(self) -> bool:
        """
        上一帧（逐帧播放）。

        Returns:
            True 表示成功切换
        """
        start_time = time.perf_counter()

        session = self._peek_session()
        if session is None:
            return False

        result = session.prev_frame()

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.PREV_FRAME,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

        return result

    def first_frame(self) -> bool:
        """
        第一帧。

        Returns:
            True 表示成功跳转
        """
        return self.seek_to_frame(0)

    def last_frame(self) -> bool:
        """
        最后一帧。

        Returns:
            True 表示成功跳转
        """
        total_frames = self.total_frames
        if total_frames is None:
            return False

        return self.seek_to_frame(total_frames - 1)

    def set_speed(self, speed: float) -> None:
        """
        设置播放速度。

        Args:
            speed: 速度倍数（1.0 = 正常）
        """
        start_time = time.perf_counter()

        with self._session_lock:
            if self._session is not None:
                self._session.set_speed(speed)

        # 记录命令
        self._record_command(PlaybackCommandData(
            command=PlaybackCommand.SET_SPEED,
            speed=speed,
        ))

        latency_ms = (time.perf_counter() - start_time) * 1000
        self._stats.update_command(latency_ms)

    # =============================================================================
    # 帧获取
    # =============================================================================

    def tick(self) -> Optional[Any]:
        """
        时钟 tick，获取当前帧。

        用于播放循环，每次 tick 获取应该显示的帧。

        Returns:
            DecodedFrame 或 None

        Note:
            UI tick 不直接解码，从队列获取 ready frame

        S0.B: 移除 _on_frame_ready 回调调用，由 _on_play_tick() 统一提交渲染。
        set_on_frame_ready() 保留给未来 worker 主动通知，但不在 UI tick 同步触发。
        """
        with self._session_lock:
            if self._session is None:
                return None

            frame = self._session.tick()

            # S0.B: 不在 tick 内调用 _on_frame_ready 回调
            # 由 EnhancedVideoPlaybackEngine._on_play_tick() 统一处理渲染提交
            # _on_frame_ready 保留给未来 worker 主动通知场景

            return frame

    def get_current_frame_data(self, is_master: bool = False) -> Optional[Any]:
        """
        获取当前帧数据。

        Args:
            is_master: 是否获取高质量 master 帧

        Returns:
            DecodedFrame 或 None
        """
        with self._session_lock:
            if self._session is None:
                return None

            return self._session.get_current_frame(is_master=is_master)

    def get_frame_at(self, frame_index: int, is_master: bool = False) -> Optional[Any]:
        """
        获取指定帧数据。

        Args:
            frame_index: 目标帧号
            is_master: 是否获取高质量 master 帧

        Returns:
            DecodedFrame 或 None
        """
        with self._session_lock:
            if self._session is None:
                return None

            return self._session.get_frame_at(frame_index, is_master=is_master)

    def request_master_frame(self, frame_index: int) -> None:
        """Schedule a high-quality frame without blocking the UI thread."""
        with self._session_lock:
            if self._session is not None:
                self._session.request_master_frame(int(frame_index))

    def take_ready_master_frame(self, frame_index: int) -> Optional[Any]:
        """Return an exact master frame only when its async decode has completed."""
        with self._session_lock:
            if self._session is None:
                return None
            return self._session.take_ready_master_frame(int(frame_index))

    def take_latest_ready_master_frame(
        self, current_target: int, *, max_frame_distance: int = 96
    ) -> Optional[Any]:
        """Return the newest completed master frame as scrub interim feedback."""
        with self._session_lock:
            if self._session is None:
                return None
            take = getattr(self._session, "take_latest_ready_master_frame", None)
            if not callable(take):
                return None
            return take(int(current_target), max_frame_distance=int(max_frame_distance))

    # =============================================================================
    # 统计与监控
    # =============================================================================

    def get_stats(self) -> dict[str, Any]:
        """
        获取完整统计信息。

        Returns:
            统计信息字典
        """
        stats = {
            'coordinator': self._stats.to_dict(),
            'session': None,
            'workers': None,
            'queue': None,
        }

        with self._session_lock:
            if self._session is not None:
                stats['session'] = self._session.stats.to_dict()

                if self._session.worker_manager is not None:
                    stats['workers'] = self._session.worker_manager.get_stats()

                if self._session.frame_queue is not None:
                    stats['queue'] = self._session.frame_queue.stats

        return stats

    def get_video_info(self) -> dict[str, Any]:
        """
        获取视频信息。

        Returns:
            视频信息字典（用于 UI 显示）
        """
        with self._session_lock:
            if self._session is None or self._session.metadata is None:
                return {}

            metadata = self._session.metadata
            display_width, display_height = metadata.display_size
            if int(getattr(metadata, 'rotation', 0) or 0) in (90, 270):
                display_width, display_height = display_height, display_width

            # Access session attributes directly — do NOT use self.fps,
            # self.total_frames etc. properties as they try to re-acquire
            # _session_lock, causing a self-deadlock with threading.Lock.
            return {
                'path': self._session.path,
                'width': metadata.width,
                'height': metadata.height,
                'display_width': int(display_width),
                'display_height': int(display_height),
                'fps': self._session.fps,
                'total_frames': self._session.total_frames,
                'duration': getattr(self._session, 'duration', None),
                'codec': getattr(metadata, 'codec', 'unknown'),
                'pixel_format': getattr(metadata, 'pixel_format', 'unknown'),
            }

    # =============================================================================
    # 回调设置
    # =============================================================================

    def set_on_frame_ready(self, callback: Optional[Callable[[Any, int], None]]) -> None:
        """
        设置帧就绪回调。

        Args:
            callback: 回调函数 (frame, frame_index)
        """
        # AUD-39403: 回调字段写入持锁
        with self._callback_lock:
            self._on_frame_ready = callback

    def set_on_state_change(self, callback: Optional[Callable[[str], None]]) -> None:
        """
        设置状态变化回调。

        Args:
            callback: 回调函数 (state_name)
        """
        # AUD-39403: 回调字段写入持锁
        with self._callback_lock:
            self._on_state_change = callback

    # =============================================================================
    # 内部方法
    # =============================================================================

    def _record_command(self, command_data: PlaybackCommandData) -> None:
        """
        记录命令到历史。

        Args:
            command_data: 命令数据
        """
        with self._command_history_lock:
            self._command_history.append(command_data)

            # 保持最近 10 条
            if len(self._command_history) > 10:
                self._command_history = self._command_history[-10:]

    def _on_session_state_change(self, state: Any) -> None:
        """
        VideoSession 状态变化回调。

        Args:
            state: PlaybackState 枚举值
        """
        state_name = state.name if hasattr(state, 'name') else str(state)

        # AUD-39403: 回调字段读取持锁（快照引用后锁外调用，回调内部
        # 可能重入 set_on_state_change）
        with self._callback_lock:
            callback = self._on_state_change
        if callback is not None:
            try:
                callback(state_name)
            except Exception as e:
                logger.warning(f"State change callback error: {e}")

    # =============================================================================
    # 视口更新
    # =============================================================================

    def update_viewport_size(self, size: tuple[int, int]) -> None:
        """
        更新视口大小。

        Args:
            size: 视口大小 (width, height)
        """
        self._viewport_size = size

        with self._session_lock:
            if self._session is not None:
                # 触发 proxy 重新计算
                if self._session.source_manager is not None:
                    self._session.source_manager.update_viewport_size(size)

    # =============================================================================
    # 资源清理
    # =============================================================================

    def cleanup(self) -> None:
        """
        清理所有资源。

        用于应用程序退出时释放所有资源。
        """
        self.unload()
        # AUD-39403: 回调清空持锁（与 setter/getter 同一把锁）
        with self._callback_lock:
            self._on_frame_ready = None
            self._on_state_change = None

        logger.info("PlaybackCoordinator cleanup complete")

    def __repr__(self) -> str:
        """字符串表示"""
        return (
            f"PlaybackCoordinator("
            f"loaded={self.is_loaded}, "
            f"playing={self.is_playing}, "
            f"frame={self.current_frame}, "
            f"path={self.path})"
        )
