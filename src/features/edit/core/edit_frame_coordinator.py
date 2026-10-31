# DORMANT(2026-08-19 审计): 已实现且有测试，但暂无生产入口（有意休眠）。
# 接线前请勿在生产依赖；若半年内无接线计划建议删除。
"""
Edit Frame Coordinator Module
编辑页面帧协调器 - 连接 EditPlaybackController 与 VideoFrameProviderService。

## 职责（P10.1）

- EditFrameCoordinator: 协调播放控制与帧获取
- 管理 clip_id 映射（timeline asset_id -> service clip_id）
- 自动 proxy/master 切换
- 性能统计收集

## 设计原则

- EditPlaybackController 只负责帧调度计算
- VideoFrameProviderService 只负责帧解码/缓存
- EditFrameCoordinator 连接两者，处理：
  - clip 打开/关闭生命周期
  - proxy/master 质量切换
  - 预取策略
  - 性能监控

## 使用示例

```python
from src.features.edit.core.edit_frame_coordinator import EditFrameCoordinator

# 创建协调器
coordinator = EditFrameCoordinator()

# 连接播放控制器
coordinator.set_playback_controller(controller)

# 添加视频剪辑
clip_handle = coordinator.add_clip(asset_id, '/path/to/video.mp4')

# 播放时，协调器自动：
# 1. 请求 proxy 帧
# 2. 预取后续帧
# 3. 收集性能统计

# 暂停时，自动请求 master 帧
coordinator.on_pause()

# 获取统计
stats = coordinator.get_stats()
```
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any, TYPE_CHECKING

from PyQt6.QtCore import QObject, pyqtSignal

if TYPE_CHECKING:
    from src.shared.video import VideoFrameProviderService
    from src.shared.video.frame_provider_service import ClipInfo
    from src.features.edit.core.edit_playback_controller import EditPlaybackController

logger = logging.getLogger(__name__)


# =============================================================================
# 剪辑句柄
# =============================================================================

@dataclass
class ClipHandle:
    """
    剪辑句柄 - 管理 timeline asset 与 frame service clip 的映射。

    Attributes:
        asset_id: Timeline 中的资产 ID
        clip_id: VideoFrameProviderService 中的剪辑 ID
        path: 视频文件路径
        clip_info: 剪辑元数据信息
        ref_count: 引用计数（多个 timeline 片段可能引用同一资产）
        last_requested_frame: 最后请求的帧号
        last_quality: 最后请求的质量级别
        last_requested_at: 最后一次请求的单调时钟时间戳（AUD-16419：
            「播放态长时间停在同一帧 → 升级 master」的驻留判断输入）
    """

    asset_id: str
    clip_id: str
    path: str
    clip_info: Optional[ClipInfo] = None
    ref_count: int = 1
    last_requested_frame: int = -1
    last_quality: str = 'proxy'
    last_requested_at: float = 0.0

    def add_ref(self) -> int:
        """增加引用计数"""
        self.ref_count += 1
        return self.ref_count

    def release_ref(self) -> int:
        """减少引用计数"""
        self.ref_count -= 1
        return self.ref_count

    def should_cleanup(self) -> bool:
        """判断是否应该清理"""
        return self.ref_count <= 0


# =============================================================================
# 性能统计
# =============================================================================

@dataclass
class CoordinatorStats:
    """
    协调器性能统计。

    Attributes:
        total_frame_requests: 总帧请求数
        proxy_requests: Proxy 帧请求数
        master_requests: Master 帧请求数
        cache_hits: 缓存命中数
        cache_misses: 缓存未命中数
        total_decode_ms: 总解码时间（毫秒）
        avg_decode_ms: 平均解码时间（毫秒）
        total_wait_ms: 总等待时间（毫秒）
        avg_wait_ms: 平均等待时间（毫秒）
        quality_switches: 质量切换次数
        clips_opened: 打开的剪辑数
        clips_closed: 关闭的剪辑数
        active_clips: 当前活跃剪辑数
    """

    total_frame_requests: int = 0
    proxy_requests: int = 0
    master_requests: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    total_decode_ms: float = 0.0
    avg_decode_ms: float = 0.0
    total_wait_ms: float = 0.0
    avg_wait_ms: float = 0.0
    quality_switches: int = 0
    clips_opened: int = 0
    clips_closed: int = 0
    active_clips: int = 0

    def record_request(self, quality: str, cache_hit: bool, decode_ms: float, wait_ms: float) -> None:
        """
        记录帧请求。

        Args:
            quality: 质量级别 ('proxy' 或 'master')
            cache_hit: 是否缓存命中
            decode_ms: 解码时间（毫秒）
            wait_ms: 等待时间（毫秒）
        """
        self.total_frame_requests += 1

        if quality == 'proxy':
            self.proxy_requests += 1
        else:
            self.master_requests += 1

        if cache_hit:
            self.cache_hits += 1
        else:
            self.cache_misses += 1

        self.total_decode_ms += decode_ms
        self.avg_decode_ms = self.total_decode_ms / self.total_frame_requests

        self.total_wait_ms += wait_ms
        self.avg_wait_ms = self.total_wait_ms / self.total_frame_requests

    def record_quality_switch(self) -> None:
        """记录质量切换"""
        self.quality_switches += 1

    def to_dict(self) -> dict[str, float | int]:
        """转换为字典"""
        return {
            'total_frame_requests': self.total_frame_requests,
            'proxy_requests': self.proxy_requests,
            'master_requests': self.master_requests,
            'cache_hits': self.cache_hits,
            'cache_misses': self.cache_misses,
            'cache_hit_rate': self.cache_hits / max(1, self.total_frame_requests),
            'total_decode_ms': self.total_decode_ms,
            'avg_decode_ms': self.avg_decode_ms,
            'total_wait_ms': self.total_wait_ms,
            'avg_wait_ms': self.avg_wait_ms,
            'quality_switches': self.quality_switches,
            'clips_opened': self.clips_opened,
            'clips_closed': self.clips_closed,
            'active_clips': self.active_clips,
        }


# =============================================================================
# EditFrameCoordinator
# =============================================================================

class EditFrameCoordinator(QObject):
    """
    编辑页面帧协调器。

    连接 EditPlaybackController 与 VideoFrameProviderService，管理：
    - clip 生命周期（打开/关闭）
    - proxy/master 自动切换
    - 预取策略
    - 性能统计

    Signals:
        frame_ready: 帧就绪 (asset_id, frame_index, quality, frame_data)
        clip_loaded: 剪辑加载完成 (asset_id, clip_info)
        clip_error: 剪辑加载失败 (asset_id, error_message)
        stats_updated: 性能统计更新 (stats_dict)

    Attributes:
        _service: VideoFrameProviderService 实例
        _playback_controller: EditPlaybackController 实例
        _asset_to_clip: asset_id -> ClipHandle 映射
        _frame_to_asset: (clip_id, frame_index) -> asset_id 缓存
        _lock: 线程锁
        _stats_lock: 统计专用锁（AUD-16415；锁序：_stats_lock → _lock 单向）
        _stats: 性能统计
        _is_paused: 是否暂停（影响质量选择）
        _current_quality: 当前质量级别
    """

    # Signals
    frame_ready = pyqtSignal(str, int, str, object)  # asset_id, frame_index, quality, frame_data
    clip_loaded = pyqtSignal(str, object)  # asset_id, clip_info
    clip_error = pyqtSignal(str, str)  # asset_id, error_message
    stats_updated = pyqtSignal(dict)  # stats_dict

    def __init__(
        self,
        service: Optional[VideoFrameProviderService] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        """
        初始化帧协调器。

        Args:
            service: VideoFrameProviderService 实例（可选，默认使用全局）
            parent: QObject 父对象
        """
        super().__init__(parent)

        # 帧服务
        if service is None:
            from src.shared.video import get_frame_provider_service
            self._service: VideoFrameProviderService = get_frame_provider_service()
        else:
            self._service = service

        # 播放控制器
        self._playback_controller: Optional[EditPlaybackController] = None

        # 剪辑映射
        self._asset_to_clip: Dict[str, ClipHandle] = {}
        self._lock: threading.Lock = threading.Lock()
        # AUD-16415：统计专用小锁。锁序纪律：绝不与 _lock 嵌套——需要同时
        # 读映射长度时先取 _lock 快照、再进 _stats_lock（即只允许
        # _stats_lock → _lock 的单向嵌套），避免 add/remove 与统计上报互锁。
        self._stats_lock: threading.Lock = threading.Lock()

        # 状态
        self._is_paused: bool = True
        self._current_quality: str = 'master'  # 初始为 master
        self._last_quality: str = 'master'

        # 统计
        self._stats: CoordinatorStats = CoordinatorStats()

        # 预取配置
        self._prefetch_enabled: bool = True
        self._prefetch_lookahead: int = 8  # 预读帧数

        # 统计上报定时器
        self._stats_timer_interval: float = 1.0  # 每秒上报一次
        self._last_stats_report: float = 0.0

        # AUD-16419: 播放态停帧驻留升级阈值（秒）。同一帧连续请求超过该时长
        # 后由 proxy 升级 master（暂停态本就返回 master，不受影响）。
        self._quality_dwell_seconds: float = 0.5

        logger.debug("EditFrameCoordinator initialized")

    # =============================================================================
    # 属性
    # =============================================================================

    @property
    def service(self) -> VideoFrameProviderService:
        """帧服务对象"""
        return self._service

    @property
    def playback_controller(self) -> Optional[EditPlaybackController]:
        """播放控制器"""
        return self._playback_controller

    @property
    def is_paused(self) -> bool:
        """是否暂停（AUD-16422: 与写入方同锁读取）"""
        with self._lock:
            return self._is_paused

    @property
    def current_quality(self) -> str:
        """当前质量级别（AUD-16422: 与写入方同锁读取）"""
        with self._lock:
            return self._current_quality

    @property
    def stats(self) -> CoordinatorStats:
        """性能统计"""
        return self._stats

    @property
    def active_clip_count(self) -> int:
        """活跃剪辑数"""
        with self._lock:
            return len(self._asset_to_clip)

    # =============================================================================
    # 配置
    # =============================================================================

    def set_playback_controller(
        self,
        controller: EditPlaybackController,
    ) -> None:
        """
        设置播放控制器并连接信号。

        Args:
            controller: EditPlaybackController 实例
        """
        self._playback_controller = controller

        # 连接信号
        controller.frame_changed.connect(self._on_frame_changed)
        controller.state_changed.connect(self._on_state_changed)

        logger.info("Playback controller connected")

    def set_prefetch_config(
        self,
        enabled: bool = True,
        lookahead: int = 8,
    ) -> None:
        """
        设置预取配置。

        Args:
            enabled: 是否启用预取
            lookahead: 预读帧数
        """
        self._prefetch_enabled = enabled
        self._prefetch_lookahead = max(1, min(32, lookahead))
        logger.debug(f"Prefetch config: enabled={enabled}, lookahead={lookahead}")

    # =============================================================================
    # 剪辑管理
    # =============================================================================

    def add_clip(
        self,
        asset_id: str,
        path: str,
    ) -> Optional[ClipHandle]:
        """
        添加视频剪辑。

        如果同一文件已打开，复用现有 clip_id。

        AUD-16413/16414：open_clip/get_clip_info 属重 I/O（磁盘探测 +
        解码器初始化可达数百 ms），一律在 ``_lock`` 临界区外执行，媒体打开
        期间不阻塞并发帧请求；clip_error/clip_loaded 信号也在锁外 emit，
        直连槽再进入任意加锁方法不会自锁，且 emit 时句柄已写入映射表。

        Args:
            asset_id: Timeline 中的资产 ID
            path: 视频文件路径

        Returns:
            ClipHandle 或 None（打开失败）
        """
        with self._lock:
            # 检查是否已有该资产的句柄
            existing_handle = self._asset_to_clip.get(asset_id)
            if existing_handle is not None:
                existing_handle.add_ref()
                logger.debug(
                    f"Clip already tracked: asset={asset_id}, "
                    f"ref_count={existing_handle.ref_count}"
                )
                return existing_handle

        # AUD-16414：通过服务打开剪辑（锁外重 I/O）
        clip_id: str
        try:
            clip_id = self._service.open_clip(path)
        except Exception as e:
            logger.error(f"Failed to open clip: path={path}, error={e}")
            # AUD-16413：锁外 emit——持锁 emit 时直连槽调用任意加锁方法
            # 会与非重入 threading.Lock 自锁。
            self.clip_error.emit(asset_id, str(e))
            return None

        # AUD-16414：获取剪辑信息（锁外）
        clip_info: Optional[ClipInfo] = None
        try:
            clip_info = self._service.get_clip_info(clip_id)
        except Exception as e:
            logger.warning(
                f"Failed to get clip info: asset={asset_id}, error={e}"
            )

        handle = ClipHandle(
            asset_id=asset_id,
            clip_id=clip_id,
            path=path,
            clip_info=clip_info,
        )

        loser_clip_id: Optional[str] = None
        with self._lock:
            # 并发双开竞速：保留先入表者；本线程后开的会话在锁外关闭。
            existing_handle = self._asset_to_clip.get(asset_id)
            if existing_handle is not None:
                existing_handle.add_ref()
                loser_clip_id = clip_id
                handle = existing_handle
            else:
                self._asset_to_clip[asset_id] = handle

        if loser_clip_id is not None:
            try:
                self._service.close_clip(loser_clip_id)
            except Exception as e:
                logger.warning(
                    f"Failed to close losing clip: asset={asset_id}, error={e}"
                )
        else:
            active = self.active_clip_count
            with self._stats_lock:
                self._stats.clips_opened += 1
                self._stats.active_clips = active
            logger.info(
                f"Clip added: asset={asset_id}, clip={clip_id}, path={path}"
            )
            # AUD-16413：句柄已入表后才 emit，直连槽观察到一致状态。
            if clip_info is not None:
                self.clip_loaded.emit(asset_id, clip_info)

        return handle

    def remove_clip(
        self,
        asset_id: str,
        *,
        force: bool = False,
    ) -> bool:
        """
        移除视频剪辑。

        减少引用计数，引用计数为 0 时关闭剪辑。

        Args:
            asset_id: 资产 ID
            force: True 时无视引用计数立即关闭（cleanup/shutdown 用，
                AUD-16410——此前 cleanup 对 ref_count>1 的剪辑只减一次，
                解码器会话在「清理」后永不关闭）。

        Returns:
            True 表示成功
        """
        clip_id_to_close: Optional[str] = None
        with self._lock:
            handle = self._asset_to_clip.get(asset_id)
            if handle is None:
                logger.warning(f"Clip not found: asset={asset_id}")
                return False

            if not force:
                # 减少引用计数
                ref_count = handle.release_ref()

                if ref_count > 0:
                    logger.debug(
                        f"Clip ref decreased: asset={asset_id}, "
                        f"ref_count={ref_count}"
                    )
                    return True

            # 引用计数为 0（或 force）：从映射表移除，锁外关闭
            clip_id_to_close = handle.clip_id
            del self._asset_to_clip[asset_id]

        # AUD-16414：close_clip 是重 I/O，移出临界区，不阻塞并发帧请求。
        assert clip_id_to_close is not None
        try:
            self._service.close_clip(clip_id_to_close)
        except Exception as e:
            logger.warning(
                f"Failed to close clip: asset={asset_id}, error={e}"
            )

        active = self.active_clip_count
        with self._stats_lock:
            self._stats.clips_closed += 1
            self._stats.active_clips = active

        logger.info(f"Clip removed and closed: asset={asset_id}")
        return True

    def get_clip_handle(
        self,
        asset_id: str,
    ) -> Optional[ClipHandle]:
        """
        获取剪辑句柄。

        Args:
            asset_id: 资产 ID

        Returns:
            ClipHandle 或 None
        """
        with self._lock:
            return self._asset_to_clip.get(asset_id)

    def has_clip(
        self,
        asset_id: str,
    ) -> bool:
        """
        检查剪辑是否存在。

        Args:
            asset_id: 资产 ID

        Returns:
            True 表示存在
        """
        with self._lock:
            return asset_id in self._asset_to_clip

    # =============================================================================
    # 帧请求
    # =============================================================================

    def request_frame(
        self,
        asset_id: str,
        frame_index: int,
        quality: Optional[str] = None,
    ) -> Optional[Any]:
        """
        请求视频帧。

        根据播放状态自动选择 proxy/master 质量。

        Args:
            asset_id: 资产 ID
            frame_index: 帧号
            quality: 质量级别（可选，自动选择时不传）

        Returns:
            DecodedFrame 或 None
        """
        start_time = time.perf_counter()

        with self._lock:
            handle = self._asset_to_clip.get(asset_id)
            if handle is None:
                logger.warning(f"Clip not found: asset={asset_id}")
                return None

            clip_id = handle.clip_id

            # 自动选择质量
            if quality is None:
                quality = self._select_quality(frame_index, handle)

            paused = self._is_paused

            handle.last_requested_frame = frame_index
            handle.last_quality = quality
            # AUD-16419: 记录本次请求时刻，供 _select_quality 的驻留判断使用。
            handle.last_requested_at = time.monotonic()

        # AUD-16411：播放热路径（proxy 且未暂停）禁止 master 回退——
        # service.request_frame 默认 allow_master_fallback=True，proxy 缓存
        # 未命中时会同步解码 4K 母版拖死播放线程（规格书 §6 / Stage C2）。
        # 暂停/逐帧/显式 master 请求保持回退可用。
        allow_master_fallback = bool(paused) or quality != 'proxy'

        # 请求帧
        frame = self._service.request_frame(
            clip_id, frame_index, quality,
            allow_master_fallback=allow_master_fallback,
        )

        # AUD-16416: frame_ready 是类 docstring 承诺的核心输出信号——成功
        # 取得帧后必须发射（历史实现从未 emit，按文档接线将静默无数据）。
        if frame is not None:
            self.frame_ready.emit(asset_id, frame_index, quality, frame)

        # 记录统计（AUD-16415：全部走 _stats_lock）
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        cache_hit = frame is not None  # 简化判断

        with self._stats_lock:
            # 检测质量切换
            if quality != self._last_quality:
                self._stats.record_quality_switch()
                self._last_quality = quality
            self._stats.record_request(
                quality=quality,
                cache_hit=cache_hit,
                decode_ms=elapsed_ms,
                wait_ms=elapsed_ms,  # 同步请求，等待=解码
            )

        # 触发预取
        if self._prefetch_enabled and frame is not None:
            self._schedule_prefetch(asset_id, frame_index, quality)

        # 上报统计
        self._maybe_report_stats()

        return frame

    def request_frame_sync(
        self,
        asset_id: str,
        frame_index: int,
        quality: str = 'master',
        timeout_ms: float = 300.0,
    ) -> Optional[Any]:
        """
        同步请求帧（带超时）。

        用于精确帧获取（暂停、逐帧、导出）。

        Args:
            asset_id: 资产 ID
            frame_index: 帧号
            quality: 质量级别
            timeout_ms: 超时时间（毫秒）

        Returns:
            DecodedFrame 或 None
        """
        start_time = time.perf_counter()

        with self._lock:
            handle = self._asset_to_clip.get(asset_id)
            if handle is None:
                return None

            clip_id = handle.clip_id

        # 循环尝试获取帧（直到超时）。AUD-16412：帧未就绪时 service 立即
        # 返回 None——无间隔死循环会在超时窗口内 100% 空转一个核心并高频
        # 轰炸解码队列，这里加 3ms 级退避让出 CPU。
        while (time.perf_counter() - start_time) * 1000 < timeout_ms:
            frame = self._service.request_frame(clip_id, frame_index, quality)
            if frame is not None:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                with self._stats_lock:
                    self._stats.record_request(
                        quality=quality,
                        cache_hit=False,
                        decode_ms=elapsed_ms,
                        wait_ms=elapsed_ms,
                    )
                return frame
            time.sleep(0.003)  # AUD-16412：backoff，不空转核心

        logger.warning(
            f"Frame request timeout: asset={asset_id}, "
            f"frame={frame_index}, quality={quality}"
        )
        return None

    # =============================================================================
    # 预取
    # =============================================================================

    def _schedule_prefetch(
        self,
        asset_id: str,
        current_frame: int,
        quality: str,
    ) -> None:
        """
        调度预取任务。

        Args:
            asset_id: 资产 ID
            current_frame: 当前帧号
            quality: 质量级别
        """
        with self._lock:
            handle = self._asset_to_clip.get(asset_id)
            if handle is None:
                return

            clip_id = handle.clip_id
            clip_info = handle.clip_info

        # 计算预取范围
        total_frames = 0
        if clip_info is not None:
            total_frames = clip_info.total_frames

        if total_frames == 0:
            return

        start = current_frame + 1
        end = min(current_frame + self._prefetch_lookahead, total_frames - 1)

        if start > end:
            return

        # 异步预取（不阻塞当前请求）
        try:
            self._service.prefetch(clip_id, start, end, quality)
            logger.debug(
                f"Prefetch scheduled: asset={asset_id}, "
                f"range=[{start}, {end}], quality={quality}"
            )
        except Exception as e:
            logger.warning(f"Prefetch failed: {e}")

    # =============================================================================
    # 播放状态响应
    # =============================================================================

    def _on_frame_changed(
        self,
        frame_index: int,
    ) -> None:
        """
        响应帧变化信号。

        Args:
            frame_index: 新帧号
        """
        # 帧变化由外部处理，协调器只负责质量切换逻辑
        pass

    def _on_state_changed(
        self,
        state: str,
    ) -> None:
        """
        响应播放状态变化。

        Args:
            state: 播放状态 (playing/paused/stopped)
        """
        # AUD-16422: 播放态字段与 request_frame 的锁内读取共用同一把 _lock，
        # 消除「无锁写 vs 锁内读」的锁纪律不一致（GIL 下不崩溃，但质量切换
        # 可能晚一帧生效；同文件两种纪律也易引入真实竞争）。
        with self._lock:
            was_paused = self._is_paused

            if state == 'playing':
                self._is_paused = False
                self._current_quality = 'proxy'
            elif state == 'paused':
                self._is_paused = True
                self._current_quality = 'master'
            else:  # stopped
                self._is_paused = True
                self._current_quality = 'master'

        # 状态变化时记录
        if was_paused != self._is_paused:
            logger.info(
                f"Playback state changed: paused={self._is_paused}, "
                f"quality={self._current_quality}"
            )

    def on_pause(
        self,
        frame_index: Optional[int] = None,
    ) -> None:
        """
        暂停时调用，请求 master 帧。

        Args:
            frame_index: 当前帧号（可选）
        """
        # AUD-16422: 写入与 request_frame 的锁内读取同锁。
        with self._lock:
            self._is_paused = True
            self._current_quality = 'master'

        # 可以在这里触发当前帧的 master 版本请求
        logger.debug(f"Pause: requesting master frame at {frame_index}")

    def on_play(self) -> None:
        """播放时调用，切换回 proxy 质量。"""
        # AUD-16422: 写入与 request_frame 的锁内读取同锁。
        with self._lock:
            self._is_paused = False
            self._current_quality = 'proxy'
        logger.debug("Play: switched to proxy quality")

    # =============================================================================
    # 内部方法
    # =============================================================================

    def _select_quality(
        self,
        frame_index: int,
        handle: ClipHandle,
    ) -> str:
        """
        自动选择质量级别。

        规则（AUD-16419: 逐帧/驻留判断由 frame_index 与
        handle.last_requested_frame/last_requested_at 驱动）：
        - 暂停/停止：master（逐帧移动属于暂停态，天然命中此分支）
        - 播放且目标帧推进中：proxy
        - 播放态但停在同一帧、驻留 >= _quality_dwell_seconds：升级 master

        Args:
            frame_index: 目标帧号
            handle: 剪辑句柄

        Returns:
            质量级别字符串 ('proxy' 或 'master')
        """
        # 暂停/停止时使用 master
        if self._is_paused:
            return 'master'

        # 播放且目标帧与上次请求不同 → 正常推进，proxy
        if frame_index != handle.last_requested_frame:
            return 'proxy'

        # 播放态但停在同一帧：驻留超过阈值后升级 master（首帧请求
        # last_requested_at 已在 request_frame 内更新，不在此处判断）。
        if (
            handle.last_requested_at > 0.0
            and (time.monotonic() - handle.last_requested_at)
            >= self._quality_dwell_seconds
        ):
            return 'master'

        # 播放时使用 proxy
        return 'proxy'

    def _maybe_report_stats(self) -> None:
        """定时上报性能统计"""
        now = time.monotonic()
        if now - self._last_stats_report >= self._stats_timer_interval:
            # AUD-16415：active_clips 经加锁属性读取（旧代码锁外裸读
            # 字典长度），快照在 _stats_lock 内写回并同步 to_dict。
            active = self.active_clip_count
            with self._stats_lock:
                self._last_stats_report = now
                self._stats.active_clips = active
                self.stats_updated.emit(self._stats.to_dict())

    # =============================================================================
    # 统计与诊断
    # =============================================================================

    def get_stats(self) -> dict[str, float | int]:
        """
        获取完整性能统计。

        Returns:
            统计信息字典（包含协调器和帧服务的统计）
        """
        with self._stats_lock:
            coordinator_stats = self._stats.to_dict()
        service_stats = self._service.get_stats()

        return {
            'coordinator': coordinator_stats,
            'service': service_stats,
        }

    def reset_stats(self) -> None:
        """重置统计数据"""
        with self._stats_lock:
            self._stats = CoordinatorStats()
            self._last_stats_report = 0.0
        logger.debug("Stats reset")

    def get_clip_paths(self) -> Dict[str, str]:
        """
        获取所有已打开剪辑的路径映射。

        Returns:
            asset_id -> path 映射
        """
        with self._lock:
            return {
                asset_id: handle.path
                for asset_id, handle in self._asset_to_clip.items()
            }

    # =============================================================================
    # 清理
    # =============================================================================

    def cleanup(self) -> None:
        """
        清理所有资源。

        关闭所有剪辑，重置状态。

        AUD-16410：强制关闭——多个 timeline 片段引用同一资产时 ref_count>1，
        旧实现每资产只 remove_clip 一次（引用计数降不到 0），「清理」后
        解码器会话永不关闭，违背本方法 docstring 的承诺。
        """
        with self._lock:
            asset_ids = list(self._asset_to_clip.keys())

        for asset_id in asset_ids:
            self.remove_clip(asset_id, force=True)

        logger.info("EditFrameCoordinator cleaned up")

    def shutdown(self) -> None:
        """
        关闭协调器。

        清理所有资源并停止服务。
        """
        self.cleanup()

        # 关闭帧服务（如果是全局实例，不关闭）
        # 由应用生命周期管理

        logger.info("EditFrameCoordinator shut down")


# =============================================================================
# 工厂函数
# =============================================================================

def create_edit_frame_coordinator(
    service: Optional[VideoFrameProviderService] = None,
    parent: Optional[QObject] = None,
) -> EditFrameCoordinator:
    """
    创建编辑帧协调器。

    Args:
        service: VideoFrameProviderService 实例（可选）
        parent: QObject 父对象

    Returns:
        EditFrameCoordinator 实例
    """
    return EditFrameCoordinator(service=service, parent=parent)
