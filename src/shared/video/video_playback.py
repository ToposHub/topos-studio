"""
Video Playback Engine Module
优化的视频播放引擎，独立于图片处理管线，提供流畅的视频回放体验。

## 架构说明

本模块是视频播放的核心控制层，采用双路径渲染架构：

### 组件层次
- **VideoPlaybackEngine**: 基类（UI 信号壳层）
  - v2 GStreamer 解码路径已移除，解码统一走 v3 media source 层
  - 提供播放控制（播放/暂停/跳转/速度/循环）

### 与其他模块的关系

- **AsyncRenderer** (async_renderer.py): 统一渲染器 (v3 默认路径)
  - v3 路径通过 current_front_tex_id 提供显示纹理
  - 支持 RENDER_VIDEO_FRAME 命令和 P1-8 8-bit 直接上传
  - 调色页面主显示路径的唯一渲染器

- **VideoAsyncRenderer** (video_async_renderer.py): 视频专用渲染器 (edit 页面)
  - 完全独立于 AsyncRenderer
  - P2-3: 当 v3 路径激活时不初始化，避免双重渲染器开销
  - 仅供 edit 页面的 timeline preview 使用

- **PlaybackCoordinator** (video/playback_state.py): v3 播放协调器
  - 管理 SourceManager, PlaybackClock, DecodeWorkerManager
  - 提供帧调度、时钟驱动、预读缓存

- **ImageEngine** (image_engine.py): 图像渲染引擎
  - 提供渲染管线和 OpenGL 上下文
  - 视频播放时复用现有的图层和着色器系统

### 管线说明 (P2-4)
- **v3 (唯一路径)**: FFmpeg/AVFoundation 解码 -> DecodedFrame -> AsyncRenderer
  - 支持 YUV 平面直接 GPU 上传，减少带宽
  - P1-8: 8-bit 帧跳过 float16 转换，直接 uint8 上传
- **v2 GStreamer 链路已移除**：解码吞吐与时钟节拍均劣于 v3（实测 4~56 倍差距），
  且 4K 无法维持 25fps；致命错误不再回退 v2，而是报错并保持 placeholder。

### 历史说明
- 本模块替代了旧的 VideoController（已删除）
- 信号接口保持兼容，可直接替换
- 旧版代码已清理，减少维护负担

## 核心优化

1. 异步帧预读：后台线程持续预解码，主线程无阻塞
2. 代理分辨率：播放时自动缩放到 viewport 大小，暂停时切回全分辨率
3. v3 统一渲染路径：使用 AsyncRenderer，避免 VideoAsyncRenderer 双重上下文开销
4. 零拷贝缓冲：复用预分配的 numpy 缓冲区，减少 GC 压力
5. 智能帧丢弃：队列积压时自动跳帧保持节奏
6. Mask 锁定：播放期间不重建 matte 纹理
7. P1-8 8-bit 上传：播放时 uint8 帧直接上传，跳过 float 转换
"""

import logging
import numpy as np
import time
import traceback
from typing import Optional, Tuple, Dict, Any, List, Mapping
from dataclasses import dataclass
from enum import Enum
from src.shared.media.color_decode import ColorConverter
from src.shared.color.types import ColorMatrix, ColorRange
from PyQt6.QtCore import QObject, pyqtSignal, QTimer, QThread, QMutex, Qt
from src.shared.video.render_commands import RenderCommand
from src.shared.media.gpu_frame import GpuDecodedFrame, is_gpu_decoded_frame
from src.shared.media.frame_path import DecodePath, RenderFramePath
from src.shared.diagnostics import flash_diag as _flash_diag

_YUV_GPU_UPLOADER_IMPORT_ERROR: Exception | None = None
try:
    from src.shared.video.yuv_gpu_upload import YUVGPUUploader
except ImportError as exc:
    _YUV_GPU_UPLOADER_IMPORT_ERROR = exc

    class YUVGPUUploader:  # type: ignore[no-redef]
        """Constant-only fallback when ModernGL-backed uploader is unavailable."""
        MATRIX_BT709 = 0
        MATRIX_BT2020 = 1
        MATRIX_BT601 = 2
        RANGE_LIMITED = 0
        RANGE_FULL = 1

_VIDEO_ASYNC_RENDERER_IMPORT_ERROR: Exception | None = None
try:
    from src.shared.video.video_async_renderer import VideoAsyncRenderer, VideoRenderCommand
except ImportError as exc:
    _VIDEO_ASYNC_RENDERER_IMPORT_ERROR = exc

    class VideoAsyncRenderer:  # type: ignore[no-redef]
        """Placeholder used when the optional legacy video renderer is unavailable."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"VideoAsyncRenderer unavailable: {_VIDEO_ASYNC_RENDERER_IMPORT_ERROR}")

    class VideoRenderCommand(Enum):  # type: ignore[no-redef]
        """Placeholder enum for type compatibility when legacy renderer is unavailable."""
        NOOP = "noop"

# P0.A: 导入性能监控器
from src.shared.media.performance_monitor import (
    VideoPerformanceMonitor,
    get_performance_monitor,
)

logger = logging.getLogger(__name__)


# =============================================================================
# P0-3/P0-4: Fallback 枚举和 FirstFrameStatus 类
# =============================================================================

from enum import Enum, auto

# P0-4: 从统一的 fallback_types 模块导入枚举和辅助函数
from src.shared.video.fallback_types import (
    VideoPipelineFallbackReason,
    log_fallback,
    get_last_fallback_reason,
)

# V-P1-1: 从 pipeline_decision 模块导入决策类型和函数
# （VideoPipelineFallbackReason 与 fallback_types 是同一个类，已在上方导入）
from src.shared.video.pipeline_decision import (
    VideoPipelineMode,
    VideoPipelineEffectiveMode,
    VideoPipelineDecision,
    resolve_video_pipeline_mode,
    fallback_to_v2_or_fail,
    should_prevent_fallback_for_codec_format,
    is_professional_codec,
)

# P3-4: 导入效果预览质量系统（播放时重型效果降级）
from src.shared.effects.preview_quality import (
    get_effect_preview_quality_manager,
    get_degraded_effect_params,
)


class FirstFrameStatus(Enum):
    """
    首帧获取状态枚举（P0-3）。

    用于记录首帧获取的三级处理结果。

    Values:
        NOT_READY: 首帧未就绪（加载后初始状态）
        MASTER_OK: 使用 master 帧成功显示
        PROXY_OK: master 失败后使用 proxy 帧成功
        PLACEHOLDER: master/proxy 都失败，显示占位符
        TIMEOUT_MASTER: master 获取超时（等待 fallback proxy）
        TIMEOUT_PROXY: proxy 获取超时（显示占位符）
        FRAME_PATH_DEGRADED: frame path 降级（planar -> packed_u16 -> packed_u8）
        ERROR: 获取失败（非超时，处理错误）
    """
    NOT_READY = auto()
    MASTER_OK = auto()
    PROXY_OK = auto()
    PLACEHOLDER = auto()
    TIMEOUT_MASTER = auto()
    TIMEOUT_PROXY = auto()
    FRAME_PATH_DEGRADED = auto()
    ERROR = auto()


@dataclass
class FirstFrameResult:
    """
    首帧获取结果（P0-3）。

    Attributes:
        status: 首帧状态
        frame: 获取的帧数据（DecodedFrame 或 numpy.ndarray）
        frame_index: 帧号（通常为 0）
        used_proxy: 是否使用了 proxy 帧（而非 master）
        frame_path: 帧路径类型（planar_yuv, packed_rgb_u8, packed_rgb_f16 等）
        decode_ms: 解码耗时（毫秒）
        process_ms: 处理耗时（毫秒）
        fallback_reason: 如果需要 fallback v2，记录原因
        error_message: 错误信息（如果有）
        degraded_from: 进入 Level-3（placeholder）前的中间状态
            （TIMEOUT_MASTER / TIMEOUT_PROXY / FRAME_PATH_DEGRADED）。
            BUG-C2-04：此前 Level-3 无条件把中间状态覆盖为 PLACEHOLDER，
            调用方永远看不到进入占位符路径的原因。
    """
    status: FirstFrameStatus
    frame: Optional[Any] = None
    frame_index: int = 0
    used_proxy: bool = False
    frame_path: str = "unknown"
    decode_ms: float = 0.0
    process_ms: float = 0.0
    fallback_reason: Optional[VideoPipelineFallbackReason] = None
    error_message: Optional[str] = None
    degraded_from: Optional["FirstFrameStatus"] = None

    @property
    def is_displayable(self) -> bool:
        """是否有可显示的帧"""
        return self.status in (
            FirstFrameStatus.MASTER_OK,
            FirstFrameStatus.PROXY_OK,
            FirstFrameStatus.FRAME_PATH_DEGRADED,
        )

    @property
    def needs_placeholder(self) -> bool:
        """是否需要显示占位符"""
        return self.status in (
            FirstFrameStatus.NOT_READY,
            FirstFrameStatus.PLACEHOLDER,
            FirstFrameStatus.TIMEOUT_PROXY,
        )

    @property
    def should_fallback_v2(self) -> bool:
        """是否应该 fallback 到 v2"""
        return self.fallback_reason is not None and self.fallback_reason != VideoPipelineFallbackReason.NONE


# =============================================================================
# P2-1: 播放状态追踪与同步解码断言
# =============================================================================

import threading

# 全局播放状态追踪器（用于检测播放状态下的同步解码）
_global_playback_state_lock = threading.Lock()
_global_playback_engines: dict[int, bool] = {}  # {engine_id: is_playing}


def _register_playback_engine(engine_id: int, is_playing: bool) -> None:
    """
    注册播放引擎的播放状态。

    P2-1: 用于全局追踪播放状态，在同步解码入口点检测是否违规。

    Args:
        engine_id: 播放引擎的唯一标识（使用 hash 或 id）
        is_playing: 当前是否在播放状态
    """
    with _global_playback_state_lock:
        _global_playback_engines[engine_id] = is_playing


def _unregister_playback_engine(engine_id: int) -> None:
    """
    注销播放引擎。

    Args:
        engine_id: 播放引擎的唯一标识
    """
    with _global_playback_state_lock:
        _global_playback_engines.pop(engine_id, None)


def _is_any_engine_playing() -> bool:
    """
    检查是否有任何播放引擎处于播放状态。

    Returns:
        True 如果至少有一个引擎正在播放
    """
    with _global_playback_state_lock:
        return any(_global_playback_engines.values())


def _assert_no_sync_decode_in_playback(
    operation: str,
    frame_index: Optional[int] = None,
    source_type: str = "unknown",
    engine_id: Optional[int] = None,
) -> None:
    """
    P2-1: 断言播放状态下不触发同步解码。

    根据文档 P2-1 的硬性规则：
    _on_play_tick() 只能：
    1. 读取 clock 目标帧
    2. 从 ReadyFrameQueue 取 ready frame
    3. 没帧就 hold last 或 drop
    4. 提交当前 latest frame 到 renderer

    禁止：
    - tick 中 source.get_frame()
    - tick 中 seek_frame()
    - tick 中读取 master
    - tick 中等待 decode
    - tick 中 engine.set_image()

    Args:
        operation: 操作名称（如 "get_master_frame", "seek_frame", "get_proxy_frame"）
        frame_index: 目标帧号（可选）
        source_type: 源类型（master, proxy, disk_proxy）
        engine_id: 特定引擎 ID（可选，如果指定只检查该引擎）

    Raises:
        AssertionError: 如果在播放状态下触发同步解码（DEBUG 模式）
    """
    # 检查播放状态
    is_playing = False
    if engine_id is not None:
        with _global_playback_state_lock:
            is_playing = _global_playback_engines.get(engine_id, False)
    else:
        is_playing = _is_any_engine_playing()

    # P2-1 的禁令针对 UI tick 热路径。后台 DecodeWorker 在播放期间
    # 调用 SourceManager/MediaSource 做预解码是预期行为，不应按每帧 ERROR
    # 记录；否则日志 I/O 本身会成为播放卡顿源。
    if is_playing and threading.current_thread() is threading.main_thread():
        # P2-1: 播放状态下触发同步解码是 bug
        msg = (
            f"P2-1 VIOLATION: Sync decode '{operation}' called during playback! "
            f"frame_index={frame_index}, source_type={source_type}. "
            f"This is prohibited per P2-1 rules - UI tick should only consume ready frames."
        )
        logger.error(msg)
        # 在 DEBUG 模式下触发断言，帮助开发者发现问题
        # 生产环境只记录日志，不中断运行
        if __debug__:
            # 使用 warning 而不是 assert，避免崩溃，但让问题可见
            logger.warning(f"DEBUG_ASSERT: {msg}")
            # 也可以选择在严格模式下抛出异常
            # raise AssertionError(msg)


# =============================================================================
# P1-1: 安全统计读取辅助函数
# =============================================================================


def _safe_stat_int(stats: Mapping[str, Any], key: str, default: int = 0) -> int:
    """
    安全地从 stats 字典读取整数值。

    P1-1: 防止测试中 MagicMock 与 int 比较导致的 TypeError。

    Args:
        stats: 统计字典（可能包含 MagicMock 等非整数值）
        key: 要读取的键名
        default: 默认值（当值不是 int 时返回）

    Returns:
        整数值，如果原始值不是 int 则返回 default
    """
    value = stats.get(key, default)
    return value if isinstance(value, int) else default


def _validated_playback_speed(speed: Any) -> Optional[float]:
    """校验外部输入的播放速度（AUD-39602）。

    speed=0 曾触发 ZeroDivisionError（1000/(fps*0)），负值把定时器
    interval 钳到 8ms 导致 125Hz 高频空转，NaN/非数值同样非法。
    合法值必须为有限正数。

    Returns:
        可用的正速度；非法输入返回 None（调用方拒绝并保持原速）
    """
    try:
        value = float(speed)
    except (TypeError, ValueError):
        return None
    if not (value > 0.0) or value != value:  # 排除 0/负/NaN
        return None
    return value


class ProxyPrefetchWorker(QThread):
    """
    后台 Proxy 帧预读线程 (P1.B 实现)。

    用于 EnhancedVideoPlaybackEngine 的 dual path 模式，
    在后台持续从 SourceManager.get_proxy_frame() 预读帧到 ready_queue，
    确保 UI tick 不直接解码，移除同步阻塞。

    核心特性：
    1. 独立线程运行，不阻塞 UI
    2. 根据 PlaybackClock 当前帧号预读未来帧
    3. 支持 seek 后清空队列并重新预读
    4. 队列满时丢弃旧帧，保留最新帧
    5. Generation ID 机制：seek 后旧帧自动失效

    使用方式：
    - EnhancedVideoPlaybackEngine.start_playback() 时启动
    - EnhancedVideoPlaybackEngine._on_play_tick() 从 ready_queue 非阻塞取帧
    - EnhancedVideoPlaybackEngine.stop_playback() 时停止
    """
    frame_ready = pyqtSignal(int)  # 帧号就绪信号
    error_occurred = pyqtSignal(str)

    def __init__(
        self,
        source_manager: Any,  # SourceManager
        playback_clock: Any,  # PlaybackClock
        max_queue_size: int = 8,
        prefetch_lookahead: int = 4
    ) -> None:
        """
        初始化 Proxy 预读线程。

        Args:
            source_manager: SourceManager 实例，提供 get_proxy_frame()
            playback_clock: PlaybackClock 实例，提供当前帧号
            max_queue_size: 就绪队列最大容量（默认 8 帧）
            prefetch_lookahead: 预读前瞻帧数（默认 4 帧）
        """
        super().__init__()
        self.setObjectName("ProxyPrefetchWorker")

        self._source_manager = source_manager
        self._playback_clock = playback_clock
        self._max_queue_size = max_queue_size
        self._prefetch_lookahead = prefetch_lookahead

        # 就绪帧队列：(frame_index, frame_array, generation_id)
        # 使用 list 便于按帧号查找和丢弃旧帧
        self._ready_frames: List[Tuple[int, np.ndarray, int]] = []
        self._ready_frames_mutex = QMutex()

        # 控制标志
        self._stop_flag: bool = False
        self._seek_request: Optional[int] = None
        self._seek_mutex = QMutex()

        # Generation ID（每次 seek 递增，用于丢弃旧帧）
        self._generation_id: int = 0
        self._generation_mutex = QMutex()

        # 当前预读帧号
        self._prefetch_frame: int = 0

        # 性能监控
        self._perf_monitor: VideoPerformanceMonitor = get_performance_monitor()

    def run(self) -> None:
        """线程主循环：后台预读 proxy 帧"""
        logger.debug(f"ProxyPrefetchWorker started, lookahead={self._prefetch_lookahead}")

        while not self._stop_flag:
            try:
                # 检查 seek 请求
                self._check_seek_request()

                # 获取当前时钟帧号和 generation
                current_frame = self._playback_clock.current_frame
                generation = self._get_generation()

                # 计算预读范围
                next_frame = self._prefetch_frame
                target_end = min(
                    current_frame + self._prefetch_lookahead,
                    (self._source_manager.total_frames or 0) - 1
                )

                # 如果预读帧落后当前时钟，跳到当前 + 1
                if next_frame < current_frame:
                    next_frame = current_frame + 1

                # 如果已经预读到目标范围，等待
                if next_frame > target_end:
                    time.sleep(0.01)
                    continue

                # 检查队列是否已满（丢弃旧帧）
                self._maybe_drop_old_frames(current_frame)

                # 检查是否已有该帧
                if self._has_frame(next_frame):
                    self._prefetch_frame = next_frame + 1
                    continue

                # 从 SourceManager 获取 proxy 帧（后台线程，不阻塞 UI）
                frame = self._source_manager.get_proxy_frame(next_frame)

                if frame is not None:
                    # 处理 DecodedFrame（转换为数组）
                    frame_array = self._process_frame(frame)
                    if frame_array is not None:
                        # 加入就绪队列
                        self._add_frame(next_frame, frame_array, generation)
                        self.frame_ready.emit(next_frame)
                        logger.debug(f"Prefetched proxy frame {next_frame}")
                    self._prefetch_frame = next_frame + 1
                else:
                    # 获取失败，等待一下
                    time.sleep(0.02)

            except Exception as e:
                logger.error(f"ProxyPrefetchWorker error: {e}")
                self.error_occurred.emit(str(e))
                time.sleep(0.1)

        logger.debug("ProxyPrefetchWorker stopped")

    def get_ready_frame(
        self,
        target_frame: int,
        timeout: float = 0.0
    ) -> Optional[Tuple[np.ndarray, int]]:
        """
        非阻塞获取就绪帧。

        P1.B 核心方法：UI tick 调用此方法，timeout 必须为 0。

        Args:
            target_frame: 目标帧号
            timeout: 超时时间（秒），必须为 0 或极短

        Returns:
            (frame_array, actual_frame) 或 None
        """
        if timeout > 0.005:
            logger.warning(f"get_ready_frame timeout={timeout}s too long, forcing 0")
            timeout = 0.0

        self._ready_frames_mutex.lock()
        try:
            # 查找目标帧或最接近的帧
            best_match: Optional[Tuple[int, np.ndarray, int]] = None
            current_gen = self._get_generation()

            for frame_idx, frame_array, gen in self._ready_frames:
                # 跳过旧 generation 的帧
                if gen != current_gen:
                    continue

                if frame_idx == target_frame:
                    # 精确匹配
                    best_match = (frame_idx, frame_array)
                    break
                elif frame_idx < target_frame:
                    # 过去的帧，跳过
                    continue
                elif best_match is None or frame_idx < best_match[0]:
                    # 找最接近的帧（未来帧）
                    best_match = (frame_idx, frame_array)

            if best_match is not None:
                # 移除已消费的帧
                self._remove_frame(best_match[0])
                return (best_match[1], best_match[0])

            return None

        finally:
            self._ready_frames_mutex.unlock()

    def request_seek(self, frame_number: int) -> None:
        """
        请求 seek 到指定帧。

        递增 generation ID，清空旧帧。

        Args:
            frame_number: 目标帧号
        """
        # AUD-39610: 所有 lock/unlock 均改 try/finally，锁内异常不再
        # 永久持有 QMutex 导致队列死锁。
        self._seek_mutex.lock()
        try:
            self._seek_request = frame_number
        finally:
            self._seek_mutex.unlock()

        # 递增 generation ID
        self._generation_mutex.lock()
        try:
            self._generation_id += 1
        finally:
            self._generation_mutex.unlock()

        # 清空队列
        self._ready_frames_mutex.lock()
        try:
            self._ready_frames.clear()
            self._prefetch_frame = frame_number
        finally:
            self._ready_frames_mutex.unlock()

        logger.debug(f"Seek requested to frame {frame_number}, gen={self._generation_id}")

    def stop(self) -> None:
        """停止预读线程"""
        self._stop_flag = True
        self._ready_frames_mutex.lock()
        try:
            self._ready_frames.clear()
        finally:
            self._ready_frames_mutex.unlock()

    def get_queue_depth(self) -> int:
        """获取当前队列深度"""
        # AUD-39610: try/finally 保证解锁
        self._ready_frames_mutex.lock()
        try:
            return len(self._ready_frames)
        finally:
            self._ready_frames_mutex.unlock()

    # === 内部方法 ===

    def _check_seek_request(self) -> None:
        """检查并处理 seek 请求"""
        # AUD-39610: try/finally 保证解锁
        self._seek_mutex.lock()
        try:
            seek_target = self._seek_request
            self._seek_request = None
        finally:
            self._seek_mutex.unlock()

        if seek_target is not None:
            self._prefetch_frame = seek_target

    def _get_generation(self) -> int:
        """获取当前 generation ID"""
        # AUD-39610: try/finally 保证解锁
        self._generation_mutex.lock()
        try:
            return self._generation_id
        finally:
            self._generation_mutex.unlock()

    def _has_frame(self, frame_index: int) -> bool:
        """检查是否已有该帧"""
        # AUD-39610: 单一 unlock 路径（try/finally），不再依赖循环内
        # 手写提前 unlock + return。
        self._ready_frames_mutex.lock()
        try:
            current_gen = self._get_generation()
            for idx, _, gen in self._ready_frames:
                if idx == frame_index and gen == current_gen:
                    return True
            return False
        finally:
            self._ready_frames_mutex.unlock()

    def _add_frame(
        self,
        frame_index: int,
        frame_array: np.ndarray,
        generation: int
    ) -> None:
        """添加帧到就绪队列"""
        # AUD-39610: try/finally 保证解锁（锁内 min() 对畸形元组抛异常
        # 时不再永久持有 QMutex）
        self._ready_frames_mutex.lock()
        try:
            # 如果队列满，丢弃最旧的帧
            if len(self._ready_frames) >= self._max_queue_size:
                # 找最旧的帧（帧号最小）
                min_idx = min(self._ready_frames, key=lambda x: x[0])[0]
                self._remove_frame(min_idx)
                logger.debug(f"Dropped frame {min_idx} from queue")

            self._ready_frames.append((frame_index, frame_array, generation))
        finally:
            self._ready_frames_mutex.unlock()

    def _remove_frame(self, frame_index: int) -> None:
        """从队列移除帧"""
        # AUD-39610: try/finally 保证解锁
        self._ready_frames_mutex.lock()
        try:
            self._ready_frames = [
                (idx, arr, gen) for idx, arr, gen in self._ready_frames
                if idx != frame_index
            ]
        finally:
            self._ready_frames_mutex.unlock()

    def _maybe_drop_old_frames(self, current_frame: int) -> None:
        """丢弃过时的帧（落后当前时钟太多）"""
        # AUD-39610: try/finally 保证解锁
        self._ready_frames_mutex.lock()
        try:
            current_gen = self._get_generation()
            # 保留当前帧和未来帧，丢弃旧 generation 和落后太多的帧
            keep_threshold = current_frame - 2  # 允许保留最多 2 帧之前的
            self._ready_frames = [
                (idx, arr, gen) for idx, arr, gen in self._ready_frames
                if gen == current_gen and idx >= keep_threshold
            ]
        finally:
            self._ready_frames_mutex.unlock()

    def _process_frame(self, frame: Any) -> Optional[np.ndarray]:
        """
        处理 DecodedFrame 转换为 numpy 数组。

        复用 EnhancedVideoPlaybackEngine._process_decoded_frame 的逻辑。
        """
        try:
            # 打包格式：有 data 属性
            if hasattr(frame, 'data') and frame.data is not None:
                data = frame.data
                pixel_format = getattr(frame, 'pixel_format', 'bgra')

                if pixel_format in ('bgr24', 'bgra', 'bgra64'):
                    if data.dtype == np.uint8:
                        return data[..., :3] if data.shape[-1] == 4 else data
                    elif data.dtype == np.uint16:
                        return (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    return data[..., :3] if data.ndim == 3 and data.shape[-1] >= 3 else data

                elif pixel_format in ('rgb24', 'rgba', 'rgba64', 'rgba64le',
                                      'rgb48le', 'rgb48be', 'rgb48',
                                      'gbrp', 'gbrp10le', 'gbrp12le', 'gbrp16le'):
                    if data.dtype == np.uint16:
                        rgb8 = (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    elif data.dtype == np.uint8:
                        rgb8 = data[..., :3]
                    else:
                        rgb8 = np.clip(data[..., :3] * 255.0, 0, 255).astype(np.uint8)
                    return np.flip(rgb8, axis=-1)  # RGB → BGR

                else:
                    if data.dtype == np.uint16:
                        return (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    return data[..., :3] if data.ndim == 3 and data.shape[-1] >= 3 else data

            # 平面格式：需要 YUV → RGB 转换（简化处理）
            elif hasattr(frame, 'planes') and frame.planes:
                # 这里简化处理，完整转换在 EnhancedVideoPlaybackEngine 中
                # 对于预读线程，可以暂时返回 None，让 UI tick 触发高质量处理
                logger.warning(f"Planar format in prefetch worker, skipping: {getattr(frame, 'pixel_format', 'unknown')}")
                return None

            return None

        except Exception as e:
            logger.error(f"Process frame error: {e}")
            return None


class VideoPlaybackEngine(QObject):
    """
    优化的视频播放引擎。
    
    替代 VideoController 的角色，但使用完全独立的优化路径：
    - 播放时：异步帧预读 + 代理分辨率 + 轻量 GPU 上传
    - 暂停/逐帧时：回退到原始 engine.set_image() 路径（确保完整质量）
    
    信号接口与 VideoController 完全兼容，可直接替换。
    """
    
    # === 兼容 VideoController 的信号接口 ===
    frame_changed = pyqtSignal(int)           # 当前帧变化
    frame_ready = pyqtSignal()                # 帧已准备好渲染（通知 MainWindow 更新 Canvas）
    total_frames_changed = pyqtSignal(int)    # 总帧数变化
    video_loaded = pyqtSignal(str)            # 视频加载完成
    play_state_changed = pyqtSignal(bool)     # 播放状态变化

    def __init__(self, engine) -> None:
        """
        Args:
            engine: ImageEngine 实例，用于渲染管线和获取 renderer
        """
        super().__init__()
        self.engine = engine

        # 视频状态（v2 GStreamer decoder 已移除，解码统一走 v3 coordinator）
        self.current_frame: int = 0
        self.is_playing: bool = False
        self.current_path: Optional[str] = None

        # AUD-39601: 基类公共方法（seek_to_time/last_frame/set_playback_speed/
        # get_current_time/get_time_string/_check_proxy_ladder）引用的组件
        # 必须在基类初始化——此前只在子类 EnhancedVideoPlaybackEngine 赋值，
        # 直接实例化基类（或子类漏初始化）即 AttributeError。
        self._playback_clock: Optional[Any] = None
        self._proxy_prefetch_worker: Optional[Any] = None
        
        # 视频专用渲染器（独立于图片渲染管线）
        self._video_renderer: Optional[VideoAsyncRenderer] = None
        self._use_video_renderer: bool = True  # 是否使用独立的视频渲染器

        # 播放定时器（驱动帧消费）。
        # PreciseTimer：视频帧消费对定时间隔敏感，CoarseTimer 在 macOS 上
        # 可能有 ~5% 的间隔抖动，会放大丢帧；与剪辑页 EditPlaybackController
        # 的定时器策略保持一致。
        self._play_timer = QTimer()
        self._play_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._play_timer.setSingleShot(False)
        self._play_timer.timeout.connect(self._on_play_tick)

        # 日志节流：4K packed RGB 警告首次打印后静默；PlaybackClock
        # 重启警告每 5s 最多 1 次，避免按帧率刷屏
        self._warned_4k_packed: bool = False
        self._clock_restart_warn_ts: float = 0.0

        # 循环播放
        self.loop_playback: bool = False

        # In/Out 播放范围（帧号）。None 表示未设置；生效范围 = [in_frame, out_frame]。
        # 播放循环到 out_frame 后自动跳回 in_frame（循环播放该区间）。
        # seek_to_frame 不受此限制（手动 scrub 仍可越界），仅在 _on_play_tick 内做循环判断。
        self.in_frame: int = 0
        self.out_frame: Optional[int] = None  # None = total_frames - 1

        # 代理分辨率相关
        self._proxy_size: Optional[Tuple[int, int]] = None  # (width, height)
        self._use_proxy: bool = True  # 是否使用代理分辨率播放
        self._display_scene_size: Optional[Tuple[int, int]] = None
        # Presentation geometry is distinct from decoded texture geometry.
        # Non-square-pixel media can have a 2.59:1 raster while its display
        # aspect is 16:9; ProgramViewer already honours this metadata.
        self._media_display_size: Optional[Tuple[int, int]] = None

        # 缓存的渲染参数（播放时复用，避免每帧重建）
        self._cached_render_params: Optional[Dict[str, Any]] = None
        # Monotonic revision used to re-grade a held frame exactly once after
        # a user changes a Color parameter during playback.
        self._render_params_generation: int = 0

        # 预分配的 float32 缓冲区
        self._float_buffer: Optional[np.ndarray] = None
        self._buffer_size: Tuple[int, int] = (0, 0)  # (height, width)

        # ── Video Pipeline Feature Flags (P0.B) ─────────────────────
        # 从配置读取视频管线设置
        # Default is v3; matches ConfigManager defaults.
        self._video_pipeline_version: str = "v3"
        self._video_pipeline_v3_enabled: bool = True
        self._video_preview_drop_frames: bool = True
        self._video_preview_max_queue: int = 3
        self._video_use_yuv_gpu_upload: bool = True
        self._load_video_pipeline_config()

        # P0.A: 性能监控器
        self._perf_monitor: VideoPerformanceMonitor = get_performance_monitor()

        # P4.B: Generation ID for stale frame detection (incremented on seek)
        self._generation_id: int = 0

        # 初始化视频渲染器
        self._init_video_renderer()

    def set_display_scene_size(self, size: Optional[Tuple[int, int]]) -> None:
        """Set the viewer coordinate space used for playback display."""
        if size is None:
            had_display_size = self._display_scene_size is not None
            self._display_scene_size = None
            # Do not leave the previous timeline-sized override behind on the
            # engine.  Once cleared, ``_get_playback_scene_size`` must fall
            # back to the decoded source frame's display dimensions.
            if self.engine is not None:
                self.engine.scene_size = None
            if had_display_size:
                self.invalidate_cached_render_params()
            return

        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            had_display_size = self._display_scene_size is not None
            self._display_scene_size = None
            if self.engine is not None:
                self.engine.scene_size = None
            if had_display_size:
                self.invalidate_cached_render_params()
            return

        new_size = (width, height)
        if self._display_scene_size == new_size:
            if self.engine is not None:
                self.engine.scene_size = (float(width), float(height))
            return

        self._display_scene_size = new_size
        if self.engine is not None:
            self.engine.scene_size = (float(width), float(height))
        self.invalidate_cached_render_params()

    def set_media_display_size(self, size: Optional[Tuple[int, int]]) -> None:
        """Use the media's metadata-derived display size for Color preview."""
        if size is None:
            self._media_display_size = None
            return
        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            self._media_display_size = None
            return
        self._media_display_size = (width, height)
        self.set_display_scene_size(self._media_display_size)

    def get_media_display_size(self) -> Optional[Tuple[int, int]]:
        """Return the source presentation size, including pixel-aspect correction."""
        return self._media_display_size

    def _presentation_size_for_frame(
        self,
        fallback_width: int,
        fallback_height: int,
    ) -> Tuple[int, int]:
        """Return the stable display geometry for every Color video frame.

        Decoder/FBO sizes are deliberately *not* used as the presentation
        contract: both can change while a dock is being resized.  The media
        display size is fixed for the lifetime of the clip and is the same
        geometry ProgramViewer uses (including pixel-aspect correction).
        """
        size = self._media_display_size or self._display_scene_size
        if size is not None:
            width, height = int(size[0]), int(size[1])
            if width > 0 and height > 0:
                return width, height
        return max(1, int(fallback_width)), max(1, int(fallback_height))

    @staticmethod
    def _fit_render_target_to_presentation(
        viewport_size: Tuple[int, int],
        presentation_size: Tuple[int, int],
    ) -> Tuple[int, int]:
        """Fit an FBO inside a viewport while preserving fixed media aspect."""
        viewport_w = max(1, int(viewport_size[0]))
        viewport_h = max(1, int(viewport_size[1]))
        display_w = max(1, int(presentation_size[0]))
        display_h = max(1, int(presentation_size[1]))
        scale = min(viewport_w / display_w, viewport_h / display_h)
        return (
            max(1, int(round(display_w * scale))),
            max(1, int(round(display_h * scale))),
        )

    def _set_media_display_size_from_metadata(self, metadata: Any) -> None:
        """Adopt VideoMetadata.display_size without changing decoded pixels."""
        if metadata is None:
            return
        try:
            width, height = metadata.display_size
            # The texture has already been oriented by the decoder, but the
            # metadata presentation dimensions still need a 90°/270° swap.
            if int(getattr(metadata, 'rotation', 0) or 0) in (90, 270):
                width, height = height, width
            self.set_media_display_size((int(width), int(height)))
        except (AttributeError, TypeError, ValueError):
            logger.debug("Unable to apply video display metadata", exc_info=True)

    def clear_display_scene_size(self) -> None:
        self.set_display_scene_size(None)

    def _get_static_preview_render_params(self, frame_width: int, frame_height: int) -> Dict[str, Any]:
        """Return render params for a static preview frame.

        ``viewport_size`` reuses ``engine.viewport_size`` (the widget pixel
        size), matching the playback/seek path, so the GPU renders into a
        widget-sized FBO rather than a thumbnail-sized one — this keeps the
        render target from being downgraded to the placeholder's low
        resolution.  ``frame_width``/``frame_height`` only build the matching
        ``visible_rect`` so the source frame keeps its own aspect ratio in
        texture space.
        """
        if self._cached_render_params is None:
            self._cached_render_params = self._build_render_params()

        viewport_size = getattr(self.engine, 'viewport_size', None)
        if not viewport_size or viewport_size[0] <= 0 or viewport_size[1] <= 0:
            viewport_size = (max(1, int(frame_width)), max(1, int(frame_height)))

        width = max(1, int(frame_width))
        height = max(1, int(frame_height))
        params = self._cached_render_params.copy()
        params['viewport_size'] = tuple(int(v) for v in viewport_size)
        params['visible_rect'] = (0.0, 0.0, float(width), float(height))
        params['scene_visible_rect'] = (0.0, 0.0, float(width), float(height))
        params['grain_visible_rect'] = params['visible_rect']
        params['show_mask'] = bool(getattr(self.engine, 'show_mask_mode', False))
        params['active_layer_index'] = int(getattr(self.engine, 'active_layer_index', 0))
        params['render_reason'] = 'static_preview'
        return params

    def _letterbox_static_preview_frame(self, frame: np.ndarray) -> np.ndarray:
        """Fit a static preview frame into the current viewport without distortion.

        .. deprecated::
            No longer called by :meth:`submit_static_preview_array`.  That
            path now uploads the placeholder frame at its native aspect ratio
            and lets the GPU scene_size + fit_to_window handle proportion,
            because letterboxing here double-degraded the ≤720p thumbnail and
            mismatched the scene aspect on clip switch (blurry + stretched).
            Retained for reference / potential future use; do not wire it
            back into the submit path without reconsidering the aspect handling.
        """
        if frame is None or not isinstance(frame, np.ndarray) or frame.ndim < 2:
            return frame

        viewport_size = getattr(self.engine, 'viewport_size', None)
        if not viewport_size or len(viewport_size) != 2:
            return frame
        target_w, target_h = int(viewport_size[0]), int(viewport_size[1])
        if target_w <= 0 or target_h <= 0:
            return frame

        src_h, src_w = frame.shape[:2]
        if src_w <= 0 or src_h <= 0:
            return frame
        if src_w == target_w and src_h == target_h:
            return frame if frame.flags['C_CONTIGUOUS'] else np.ascontiguousarray(frame)

        scale = min(float(target_w) / float(src_w), float(target_h) / float(src_h))
        draw_w = max(1, int(round(src_w * scale)))
        draw_h = max(1, int(round(src_h * scale)))

        try:
            import cv2

            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            fitted = cv2.resize(frame, (draw_w, draw_h), interpolation=interpolation)
        except Exception:
            return frame if frame.flags['C_CONTIGUOUS'] else np.ascontiguousarray(frame)

        background_value = 26 if np.issubdtype(frame.dtype, np.integer) else 0.1
        if frame.ndim == 2:
            canvas = np.full((target_h, target_w), background_value, dtype=frame.dtype)
        else:
            canvas = np.full(
                (target_h, target_w, frame.shape[2]), background_value, dtype=frame.dtype
            )
        x0 = max(0, (target_w - draw_w) // 2)
        y0 = max(0, (target_h - draw_h) // 2)
        canvas[y0:y0 + draw_h, x0:x0 + draw_w] = fitted
        return np.ascontiguousarray(canvas)

    def _update_engine_video_frame_reference(
        self,
        frame: np.ndarray,
        *,
        preserve_display_reference: bool = False,
    ) -> None:
        """Keep the paused source frame accurate without proxy-size churn in playback."""
        # A timeline display scene (for example 1920x1080) is not the source
        # frame.  The old unconditional guard left ImageEngine pointing at the
        # previous clip whenever Color was paused, so its viewport geometry and
        # the newly uploaded video texture described different images.  Only
        # protect the reference during actual proxy playback; a paused seek
        # must replace it with the current clip's original frame.
        if (
            preserve_display_reference
            and self._display_scene_size is not None
            and self.engine.img_full_original is not None
        ):
            return
        self.engine.img_full_original = frame
        self.engine.img_source_full = frame

    def load_video(self, path: str) -> bool:
        """
        加载视频文件（基类桩）。

        v2 GStreamer 解码链路已移除：基类不再自行解码。
        实际加载由 EnhancedVideoPlaybackEngine.load_video 通过
        PlaybackCoordinator（v3 media source 层）完成。
        """
        logger.error(
            "VideoPlaybackEngine.load_video: v2 GStreamer pipeline removed; "
            "use EnhancedVideoPlaybackEngine (v3) instead"
        )
        return False

    def unload_video(self) -> None:
        """卸载当前视频，释放所有资源"""
        self.stop_playback()

        self.current_frame = 0
        self.current_path = None
        self._media_display_size = None
        self._cached_render_params = None
        self._float_buffer = None
        self._video_path_logged = False

    def is_video_loaded(self) -> bool:
        """是否有视频已加载（基类：按 current_path 判断，v2 decoder 已移除）"""
        return self.current_path is not None
    
    def _init_video_renderer(self) -> None:
        """初始化独立的视频渲染器

        P2-3: 当 v3 (AsyncRenderer) 路径激活时，跳过 VideoAsyncRenderer 初始化。
        v3 路径通过 AsyncRenderer.current_front_tex_id 提供显示纹理，
        无需 VideoAsyncRenderer 的独立 OpenGL 共享上下文，避免双重渲染器的
        线程/上下文开销。VideoAsyncRenderer 仍可供 edit 页面等其他功能使用。
        """
        # P2-3: v3 路径使用 AsyncRenderer，不需要 VideoAsyncRenderer
        if self.is_v3_pipeline_enabled():
            logger.debug("P2-3: Skipping VideoAsyncRenderer init — v3 AsyncRenderer path is active")
            self._use_video_renderer = False
            return

        if self._use_video_renderer and self._video_renderer is None:
            try:
                # 获取主 OpenGL 上下文
                if hasattr(self.engine, 'renderer') and self.engine.renderer:
                    main_context = self.engine.renderer.main_context
                    self._video_renderer = VideoAsyncRenderer(main_context)
                    self._video_renderer.frame_ready.connect(self._on_video_frame_ready)
                    self._video_renderer.error_occurred.connect(self._on_video_renderer_error)
                    self._video_renderer.start()
                else:
                    logger.warning("WARNING: Cannot access main OpenGL context, falling back to AsyncRenderer")
                    self._use_video_renderer = False
            except Exception as e:
                logger.error(f"Failed to initialize VideoAsyncRenderer: {e}")
                traceback.print_exc()
                self._use_video_renderer = False
    
    def _on_video_frame_ready(self, tex_id: int) -> None:
        """视频帧渲染完成回调"""
        # 通知 MainWindow 更新 Canvas
        self.frame_ready.emit()
    
    def _on_video_renderer_error(self, error_msg: str) -> None:
        """视频渲染器错误回调"""
        logger.error(f"VideoRenderer error: {error_msg}")
        # 回退到使用 AsyncRenderer
        self._use_video_renderer = False

    def _update_proxy_size_from_info(self, vid_w: int, vid_h: int) -> None:
        """
        根据视频尺寸和配置计算代理分辨率（不依赖 decoder）。

        Args:
            vid_w: 视频宽度
            vid_h: 视频高度
        """
        # 从配置获取最大长边尺寸（默认 1920）
        max_long_edge = self._get_max_video_resolution()

        # 计算缩放比例，保持宽高比
        long_edge = max(vid_w, vid_h)
        if max_long_edge > 0 and long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            proxy_w = max(64, int(vid_w * scale))
            proxy_h = max(64, int(vid_h * scale))
        else:
            # 视频小于配置的最大尺寸，或设置为原始分辨率，不缩放
            proxy_w, proxy_h = vid_w, vid_h

        self._proxy_size = (proxy_w, proxy_h)

    def _update_proxy_size(self) -> None:
        """根据用户配置和视频尺寸计算代理分辨率（长边不超过配置值）"""
        if not self._media_display_size:
            return

        vid_w, vid_h = self._media_display_size

        # 从配置获取最大长边尺寸（默认 1920）
        max_long_edge = self._get_max_video_resolution()

        # 计算缩放比例，保持宽高比
        long_edge = max(vid_w, vid_h)
        if max_long_edge > 0 and long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            proxy_w = max(64, int(vid_w * scale))
            proxy_h = max(64, int(vid_h * scale))
        else:
            # 视频小于配置的最大尺寸，或设置为原始分辨率，不缩放
            proxy_w, proxy_h = vid_w, vid_h

        self._proxy_size = (proxy_w, proxy_h)

    def _get_max_video_resolution(self) -> int:
        """从配置获取视频播放最大分辨率（长边像素数）。

        Returns:
            最大长边像素数，0 表示使用原始分辨率
        """
        # 通过 engine 访问 config_manager
        if hasattr(self.engine, 'window') and hasattr(self.engine.window, 'config_manager'):
            return self.engine.window.config_manager.config.get('video_playback_resolution', 1920)
        return 1920  # 默认 1920 (2K)

    def _load_video_pipeline_config(self) -> None:
        """
        从配置加载视频管线 Feature Flags (P0.B, P0-1)。

        P0-1: 使用新的单一枚举配置项 video_pipeline_mode 和 video_pipeline_allow_v2_fallback。
        旧的 video_pipeline_version 和 video_pipeline_v3_enabled 已废弃，仅在迁移时使用。

        配置项包括：
        - video_pipeline_mode: "auto" | "v3" | "v2" (默认 "auto")
        - video_pipeline_allow_v2_fallback: v3 失败时是否允许自动回退 (默认 True)
        - video_preview_drop_frames: 预览队列满时是否丢帧
        - video_preview_max_queue: 预览队列深度
        - video_use_yuv_gpu_upload: 是否使用 GPU 进行 YUV 转换
        """
        # 通过 engine 访问 config_manager
        if hasattr(self.engine, 'window') and hasattr(self.engine.window, 'config_manager'):
            config_manager = self.engine.window.config_manager
            config = config_manager.config

            # P0-1: 使用 resolve_video_pipeline_mode() 决策函数
            self._pipeline_decision = resolve_video_pipeline_mode(config_manager)

            # 其他视频管线选项
            self._video_preview_drop_frames = config.get('video_preview_drop_frames', True)
            self._video_preview_max_queue = config.get('video_preview_max_queue', 3)
            self._video_use_yuv_gpu_upload = config.get('video_use_yuv_gpu_upload', True)
        else:
            # 无 window/config_manager（独立引擎/测试环境）也要有决策对象：
            # load_video 的异常兜底路径无条件引用 self._pipeline_decision，
            # 缺失会把真实加载异常掩盖成 AttributeError。
            try:
                from src.shared.config.config_manager import ConfigManager

                fallback_config_manager = ConfigManager()
                self._pipeline_decision = resolve_video_pipeline_mode(fallback_config_manager)
                config = fallback_config_manager.config
            except Exception as exc:
                logger.debug(f"pipeline config fallback failed: {exc}")
                self._pipeline_decision = VideoPipelineDecision(
                    requested_mode=VideoPipelineMode.AUTO,
                    effective_mode=VideoPipelineEffectiveMode.V3,
                    allow_fallback=True,
                    reason="default_auto",
                    fallback_reason=VideoPipelineFallbackReason.NONE
                    if hasattr(VideoPipelineFallbackReason, "NONE")
                    else None,
                    message="Video pipeline: v3 (default, config unavailable)",
                )
                config = {}
            self._video_preview_drop_frames = True
            self._video_preview_max_queue = 3
            self._video_use_yuv_gpu_upload = True

        # 记录日志，显示实际路径
        logger.info(f"Video pipeline config loaded: {self._pipeline_decision}")
        logger.debug(f"Video pipeline options: drop_frames={self._video_preview_drop_frames}, "
                    f"max_queue={self._video_preview_max_queue}, "
                    f"yuv_gpu_upload={self._video_use_yuv_gpu_upload}")

        # 保留旧的属性名以兼容现有代码（但使用决策结果）
        self._video_pipeline_version = self._pipeline_decision.effective_mode.value
        self._video_pipeline_v3_enabled = self._pipeline_decision.is_v3_active()
        self._video_pipeline_allow_v2_fallback = self._pipeline_decision.allow_fallback

    def is_v3_pipeline_enabled(self) -> bool:
        """
        检查是否启用了 v3 视频管线架构。

        P0-1: 使用 VideoPipelineDecision.is_v3_active() 判断。
        """
        if hasattr(self, '_pipeline_decision'):
            return self._pipeline_decision.is_v3_active()
        # 兼容旧代码路径（如果 _load_video_pipeline_config 未调用）
        return self._video_pipeline_version == 'v3' and self._video_pipeline_v3_enabled

    def get_pipeline_decision(self) -> Optional[VideoPipelineDecision]:
        """
        P0-1: 获取完整的管线决策结果。

        用于 UI 显示、日志记录和调试。
        """
        return getattr(self, '_pipeline_decision', None)

    def get_pipeline_mode_string(self) -> str:
        """
        P0-1: 获取用于 UI 显示的管线模式字符串。
        """
        if hasattr(self, '_pipeline_decision'):
            return str(self._pipeline_decision)
        return "Video pipeline: unknown"

    def should_drop_preview_frames(self) -> bool:
        """检查预览时是否允许丢帧"""
        return self._video_preview_drop_frames

    def get_preview_queue_depth(self) -> int:
        """获取预览队列深度"""
        return self._video_preview_max_queue

    def should_use_yuv_gpu_upload(self) -> bool:
        """检查是否使用 GPU 进行 YUV 转换

        macOS 必须通过 GPU YUV self-test 才能启用，否则 CPU fallback。
        其他平台只要配置开启就直接启用。
        """
        import sys
        if not self._video_use_yuv_gpu_upload:
            return False
        if sys.platform == 'darwin':
            # macOS: 必须 self-test 通过才启用
            try:
                engine = getattr(self, 'engine', None)
            except RuntimeError:
                engine = None
            renderer = getattr(engine, 'renderer', None)
            supported = bool(getattr(renderer, '_yuv_gpu_supported', False))
            if not supported:
                result = getattr(renderer, '_yuv_gpu_self_test_result', None)
                reason = result.reason if result else 'renderer_not_initialized'
                logger.info(
                    f"macOS YUV GPU disabled: reason={reason}, fallback=renderer_cpu_yuv"
                )
            return supported
        return True

    def _get_float_buffer(self, height: int, width: int) -> np.ndarray:
        """获取或创建预分配的 float32 缓冲区"""
        if (self._float_buffer is None or 
            self._buffer_size[0] != height or 
            self._buffer_size[1] != width):
            self._float_buffer = np.empty((height, width, 3), dtype=np.float32)
            self._buffer_size = (height, width)
        return self._float_buffer

    # ================================================================
    # 高质量路径（暂停/逐帧/seek 时使用）
    # ================================================================

    def _show_frame_hq(self, frame_num: int, is_new_file: bool = False) -> None:
        """
        高质量显示指定帧 — v2 桩方法。

        v2 GStreamer 解码链路已移除；高质量首帧/seek 显示由
        EnhancedVideoPlaybackEngine 的 coordinator 路径负责。
        """
        logger.debug(
            "_show_frame_hq: no-op (v2 GStreamer pipeline removed), frame=%s",
            frame_num,
        )

    # ================================================================
    # 轻量播放路径（播放时使用）
    # ================================================================

    def _submit_frame_playback(
        self,
        frame: np.ndarray,
        frame_num: int,
        *,
        quality: str = "proxy",
        reason: str = "playback",
        drop_if_stale: bool = True,
        render_params: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        轻量级帧提交（播放专用）。

        P4.B: 使用 RENDER_VIDEO_FRAME 命令，合并上传和渲染为单次操作。
        支持 generation_id 机制，减少队列积压。

        UI tick stays lightweight: submit the raw BGR frame to the render
        thread and let AsyncRenderer handle BGR->RGBA conversion/upload.

        Args:
            frame: BGR uint8 帧（已缩放到代理分辨率）
            frame_num: 帧号
            quality: Frame quality label for renderer coalescing decisions.
            reason: Render reason. ``seek`` frames are retained.
            drop_if_stale: Whether renderer may replace this frame with newer
                pending playback frames.
            render_params: Optional precomputed render params. Static preview
                paths use this to avoid inheriting canvas pan/zoom visible_rect.
        """
        # P3-2: Track last pipeline path for get_pipeline_report()
        self._last_decode_path = (
            getattr(self, '_last_decode_path', None)
            or DecodePath.CPU_PACKED_RGB.value
        )
        self._last_render_frame_path = RenderFramePath.PACKED_RGB_CPU_UPLOAD.value
        self._last_zero_copy = False

        # P0.A: 记录处理开始时间
        process_start = time.perf_counter()

        # P4.B: 使用合并的 RENDER_VIDEO_FRAME 命令
        renderer = self.engine.renderer
        if renderer is None:
            return

        # The external monitor must not treat a retained seek/static texture
        # as the first frame of a newly started playback session.
        self._last_submitted_video_reason = reason
        self._last_submitted_video_frame_index = int(frame_num)

        h, w = frame.shape[:2]
        frame_payload = frame if frame.flags['C_CONTIGUOUS'] else np.ascontiguousarray(frame)

        # P0.A: 记录处理结束时间，上传开始时间
        process_end = time.perf_counter()
        process_ms = (process_end - process_start) * 1000
        self._perf_monitor.track_process_time(frame_num, process_ms)

        upload_start = time.perf_counter()

        # 2. 更新 engine 的显示图像引用（用于 Canvas 坐标系）
        self._update_engine_video_frame_reference(
            frame,
            preserve_display_reference=reason == "playback",
        )

        # 3. 图层参数可缓存，但视口/缩放/遮罩预览必须每帧刷新
        if render_params is None:
            render_params = self._get_playback_render_params(
                w,
                h,
                render_at_source_resolution=True,
            )

        # P0.A: 记录上传提交结束时间
        upload_end = time.perf_counter()
        upload_ms = (upload_end - upload_start) * 1000
        self._perf_monitor.track_upload_time(frame_num, upload_ms)

        render_start = time.perf_counter()

        # 4. P4.B: 提交合并的 RENDER_VIDEO_FRAME 命令（上传 + 渲染）
        # S5.A: 增加 quality 和 reason 字段用于队列合并逻辑
        renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, {
            'frame': frame_payload,
            'frame_index': frame_num,
            'render_params': render_params,
            'generation_id': self._generation_id,
            'drop_if_stale': drop_if_stale,
            'quality': quality,
            'reason': reason,
        })

        # P0.A: 记录渲染提交结束时间
        render_end = time.perf_counter()
        render_ms = (render_end - render_start) * 1000
        self._perf_monitor.track_render_submit_time(frame_num, render_ms)

        self.current_frame = frame_num
        self.frame_changed.emit(frame_num)
        if reason != "playback" or not drop_if_stale:
            self._last_frame_ready_direct_render = True
        self.frame_ready.emit()

    # S2.A: DecodedFrame 提交入口
    def _submit_decoded_frame_playback(
        self,
        frame: Any,
        frame_num: int,
        *,
        quality: str = "proxy",
        reason: str = "playback",
        drop_if_stale: bool = True,
        render_at_source_resolution: Optional[bool] = None,
    ) -> None:
        """
        S2.A: 直接提交 DecodedFrame 到渲染器，避免 UI 线程 BGR/RGBA 转换。

        根据 frame 类型分流：
        - GpuDecodedFrame: GPU 零拷贝路径，提交 RENDER_VIDEO_GPU_FRAME
        - packed (RGB/RGBA/BGR/BGRA): 直接提交 RENDER_VIDEO_FRAME，payload 中传 decoded_frame
        - planar (YUV/YUVA): 如果 should_use_yuv_gpu_upload() 为 True，
          使用 GPU 上传；否则 CPU fallback 转换为 numpy

        Args:
            frame: DecodedFrame 或 GpuDecodedFrame 对象
            frame_num: 帧号
            quality: Frame quality label for renderer coalescing decisions.
            reason: Render reason. ``seek`` frames are never merged/dropped.
            drop_if_stale: Whether renderer may replace this frame with newer
                pending playback frames.
            render_at_source_resolution: Override the render-target resolution
                policy for precision previews. ``None`` follows ``quality``.
        """
        # P0.A: 记录处理开始时间
        process_start = time.perf_counter()

        renderer = self.engine.renderer
        if renderer is None:
            return
        _flash_diag.event(
            'submit', frame_num=int(frame_num), reason=str(reason),
            gen=int(getattr(self, '_generation_id', 0) or 0),
            params_gen=int(getattr(self, '_render_params_generation', 0) or 0),
            gpu=bool(is_gpu_decoded_frame(frame)),
        )
        if render_at_source_resolution is None:
            # Keep the render target at the decoded frame's native size. A
            # proxy setting controls how a frame is decoded, but must not
            # additionally downscale an already-native 1080p frame to the
            # viewer widget before color processing.
            render_at_source_resolution = True

        # Keep the presentation intent alongside the submitted frame.  The
        # DeckLink path uses this to reject a delayed static frame after Play.
        self._last_submitted_video_reason = reason
        self._last_submitted_video_frame_index = int(frame_num)

        # Phase D: GPU 零拷贝路径 — GpuDecodedFrame 直接绑定外部纹理
        if is_gpu_decoded_frame(frame):
            gpu_frame: GpuDecodedFrame = frame
            source_size = (gpu_frame.width, gpu_frame.height)

            # P3-2: Track last pipeline path for get_pipeline_report()
            self._last_decode_path = gpu_frame.decode_path
            # Determine render path from actual plane components (not hardcoded)
            plane_api = gpu_frame.planes[0].api if gpu_frame.planes else "opengl"
            is_rgb = any(p.component == "RGBA" for p in gpu_frame.planes)
            try:
                self._last_render_frame_path = RenderFramePath.qualify_external(
                    plane_api, is_rgb).value
            except ValueError:
                self._last_render_frame_path = (
                    RenderFramePath.GPU_DECODED_EXTERNAL_RGB.value if is_rgb
                    else RenderFramePath.GPU_DECODED_EXTERNAL_YUV.value)
            self._last_zero_copy = True

            render_params = self._get_playback_render_params(
                gpu_frame.width,
                gpu_frame.height,
                source_width=source_size[0],
                source_height=source_size[1],
                render_at_source_resolution=render_at_source_resolution,
            )

            render_path = self._last_render_frame_path

            renderer.submit(RenderCommand.RENDER_VIDEO_GPU_FRAME, {
                'gpu_decoded_frame': gpu_frame,
                'frame_index': frame_num,
                'render_params': render_params,
                'generation_id': self._generation_id,
                'drop_if_stale': drop_if_stale,
                'decode_path': gpu_frame.decode_path,
                'frame_path': render_path,
                'yuv_matrix': gpu_frame.color_matrix,
                'color_range': gpu_frame.color_range,
                'quality': quality,
                'reason': reason,
            })

            process_ms = (time.perf_counter() - process_start) * 1000.0
            self._perf_monitor.track_process_time(frame_num, process_ms)

            self.current_frame = frame_num
            self.frame_changed.emit(frame_num)
            self._last_frame_ready_direct_render = True
            self.frame_ready.emit()
            self._last_displayed_frame_index = frame_num
            return

        # 判断 frame 类型
        if not hasattr(frame, 'is_planar'):
            # 不是 DecodedFrame，fallback 到旧方法
            if isinstance(frame, np.ndarray):
                self._submit_frame_playback(
                    frame,
                    frame_num,
                    quality=quality,
                    reason=reason,
                    drop_if_stale=drop_if_stale,
                )
                return
            else:
                logger.warning(f"_submit_decoded_frame_playback: Unknown frame type {type(frame)}")
                return

        # S2.A: 根据 is_planar 分流
        if frame.is_planar:
            # 平面格式 (YUV/YUVA)：播放 tick 只能提交 DecodedFrame。
            # 绝不能在这里做 CPU YUV->RGB，否则 ProRes/MOV 会把 UI tick
            # 拉到 100ms+，播放、缩放和拖动都会卡顿。
            process_ms = 0.0
            self._perf_monitor.track_process_time(frame_num, process_ms)

            upload_start = time.perf_counter()

            # 图层参数可缓存，但视口/缩放/遮罩预览必须每帧刷新
            source_size = self._get_decoded_frame_source_size(frame)
            render_params = self._get_playback_render_params(
                frame.width,
                frame.height,
                source_width=source_size[0],
                source_height=source_size[1],
                render_at_source_resolution=render_at_source_resolution,
            )

            upload_end = time.perf_counter()
            upload_ms = (upload_end - upload_start) * 1000
            self._perf_monitor.track_upload_time(frame_num, upload_ms)

            render_start = time.perf_counter()

            # Log video path decision on first frame
            if frame_num == 0 or not hasattr(self, '_video_path_logged') or not self._video_path_logged:
                self._video_path_logged = True
                renderer_obj = self.engine.renderer if self.engine else None
                if renderer_obj and hasattr(renderer_obj, 'log_video_path_decision'):
                    try:
                        renderer_obj.log_video_path_decision(
                            pixel_format=getattr(frame, 'pixel_format', 'unknown'),
                            proxy_size=(frame.width, frame.height),
                            preserve_planes=True,
                            yuv_gpu_config=self._video_use_yuv_gpu_upload,
                        )
                    except Exception:
                        pass

            # 提交 RENDER_VIDEO_FRAME，payload 中传 decoded_frame。
            # renderer 负责 YUV planes 上传/转换；如需 CPU fallback，也必须在
            # renderer 线程内完成，不能回到 UI tick。
            renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, {
                'decoded_frame': frame,
                'frame_index': frame_num,
                'render_params': render_params,
                'generation_id': self._generation_id,
                'drop_if_stale': drop_if_stale,
                'yuv_matrix': self._get_yuv_matrix(frame),
                'color_range': self._get_color_range(frame),
                'use_yuv_gpu_upload': self.should_use_yuv_gpu_upload(),
                'quality': quality,
                'reason': reason,
            })

            render_end = time.perf_counter()
            render_ms = (render_end - render_start) * 1000
            self._perf_monitor.track_render_submit_time(frame_num, render_ms)

            self.current_frame = frame_num
            self.frame_changed.emit(frame_num)
            self._last_frame_ready_direct_render = True
            self.frame_ready.emit()
            self._last_displayed_decoded_frame = frame
            self._last_displayed_frame_index = frame_num

        else:
            # 打包格式 (RGB/RGBA/BGR/BGRA)
            # 直接提交 DecodedFrame，renderer 会处理
            # P2-4: Warn when packed RGB CPU upload is used for 4K playback,
            # as this path moves large buffers through CPU memory.  Planar
            # decode with GPU YUV upload is preferred for 4K YUV sources.
            if (frame.width >= 3840 or frame.height >= 2160) and not self._warned_4k_packed:
                self._warned_4k_packed = True
                logger.warning(
                    f"4K playback using packed_rgb_cpu_upload path, "
                    f"consider using proxy or planar decode. "
                    f"size={frame.width}x{frame.height}"
                )

            process_ms = 0.0  # packed DecodedFrame submit，无 CPU 转换
            self._perf_monitor.track_process_time(frame_num, process_ms)

            upload_start = time.perf_counter()

            # 更新 engine 的显示图像引用
            if frame.data is not None:
                self._update_engine_video_frame_reference(
                    frame.data,
                    preserve_display_reference=reason == "playback",
                )

            # 图层参数可缓存，但视口/缩放/遮罩预览必须每帧刷新
            source_size = self._get_decoded_frame_source_size(frame)
            render_params = self._get_playback_render_params(
                frame.width,
                frame.height,
                source_width=source_size[0],
                source_height=source_size[1],
                render_at_source_resolution=render_at_source_resolution,
            )

            upload_end = time.perf_counter()
            upload_ms = (upload_end - upload_start) * 1000
            self._perf_monitor.track_upload_time(frame_num, upload_ms)

            render_start = time.perf_counter()

            # 提交 RENDER_VIDEO_FRAME，payload 中传 decoded_frame
            # S5.A: 增加 quality 和 reason 字段用于队列合并逻辑
            renderer.submit(RenderCommand.RENDER_VIDEO_FRAME, {
                'decoded_frame': frame,
                'frame_index': frame_num,
                'render_params': render_params,
                'generation_id': self._generation_id,
                'drop_if_stale': drop_if_stale,
                'quality': quality,
                'reason': reason,
            })

            render_end = time.perf_counter()
            render_ms = (render_end - render_start) * 1000
            self._perf_monitor.track_render_submit_time(frame_num, render_ms)

            self.current_frame = frame_num
            self.frame_changed.emit(frame_num)
            self._last_frame_ready_direct_render = True
            self.frame_ready.emit()
            self._last_displayed_decoded_frame = frame
            self._last_displayed_frame_index = frame_num

    def refresh_current_frame_view(self) -> bool:
        """
        轻量视口刷新：用已有的 GPU 纹理重新渲染，不重新上传帧数据。

        滚轮缩放、窗口尺寸变化等操作只改变视口参数，不应该触发同步解码
        或纹理重传；这里只重建 render_params（含最新 visible_rect）并提交
        一次 RENDER_FRAME，复用渲染器上已有的视频纹理。
        """
        renderer = getattr(self.engine, 'renderer', None)
        if renderer is None:
            return False

        # During playback only RENDER_VIDEO_FRAME may publish the front
        # texture.  Submitting a RENDER_FRAME here would redraw a retained
        # source frame between two playback frames and visibly flash between
        # pictures.  The next playback tick reads the current viewport state
        # and presents it through the video lane.
        if self.is_playing:
            return True

        # 获取已有视频纹理的尺寸
        width = height = 0
        current_video_texture = getattr(renderer, 'current_video_texture', None)
        if current_video_texture is not None:
            width, height = current_video_texture.size
        else:
            planes = getattr(renderer, 'current_video_planes', None)
            if planes:
                width, height = planes[0].size

        if width <= 0 or height <= 0:
            # v3 playback may have a DecodedFrame ready before the first GPU
            # video texture exists. Re-submit only as a bootstrap fallback.
            last_decoded = getattr(self, '_last_displayed_decoded_frame', None)
            last_frame_index = getattr(self, '_last_displayed_frame_index', None)
            if last_decoded is not None and last_frame_index is not None:
                self._submit_decoded_frame_playback(last_decoded, last_frame_index)
                return True
            return False

        last_decoded = getattr(self, '_last_displayed_decoded_frame', None)
        source_size = self._get_decoded_frame_source_size(last_decoded)
        render_params = self._get_playback_render_params(
            width,
            height,
            source_width=source_size[0],
            source_height=source_size[1],
        )
        render_params['render_reason'] = 'viewport_refresh'

        renderer.submit(
            RenderCommand.RENDER_FRAME,
            render_params,
        )
        return True

    def _get_yuv_matrix(self, frame: Any) -> int:
        """
        从 DecodedFrame.metadata 获取 YUV 矩阵类型。

        Args:
            frame: DecodedFrame 对象

        Returns:
            YUV 矩阵类型码，使用 YUVGPUUploader 常量：
            - YUVGPUUploader.MATRIX_BT709 (0): BT.709 (HD video)
            - YUVGPUUploader.MATRIX_BT2020 (1): BT.2020 (HDR)
            - YUVGPUUploader.MATRIX_BT601 (2): BT.601 (SD video)
        """
        if not hasattr(frame, 'metadata') or frame.metadata is None:
            return YUVGPUUploader.MATRIX_BT709  # 默认 BT709

        matrix_str = getattr(frame.metadata, 'color_matrix', 'bt709')
        matrix_map = {
            'bt709': YUVGPUUploader.MATRIX_BT709,
            'smpte170m': YUVGPUUploader.MATRIX_BT601,
            'bt470bg': YUVGPUUploader.MATRIX_BT601,
            'bt2020nc': YUVGPUUploader.MATRIX_BT2020,
            'bt2020': YUVGPUUploader.MATRIX_BT2020,
        }
        return matrix_map.get(matrix_str.lower(), YUVGPUUploader.MATRIX_BT709)

    def _get_color_range(self, frame: Any) -> int:
        """
        从 DecodedFrame.metadata 获取颜色范围。

        Args:
            frame: DecodedFrame 对象

        Returns:
            颜色范围码，使用 YUVGPUUploader 常量：
            - YUVGPUUploader.RANGE_LIMITED (0): TV legal range (16-235)
            - YUVGPUUploader.RANGE_FULL (1): PC full range (0-255)
        """
        if not hasattr(frame, 'metadata') or frame.metadata is None:
            return YUVGPUUploader.RANGE_LIMITED  # 默认 LIMITED

        range_str = getattr(frame.metadata, 'color_range', 'limited')
        range_map = {
            'limited': YUVGPUUploader.RANGE_LIMITED,
            'tv': YUVGPUUploader.RANGE_LIMITED,
            'full': YUVGPUUploader.RANGE_FULL,
            'pc': YUVGPUUploader.RANGE_FULL,
        }
        return range_map.get(range_str.lower(), YUVGPUUploader.RANGE_LIMITED)

    def _identity_video_raw_params(self) -> Dict[str, Any]:
        """Return a no-op raw preview block for normal video playback."""
        return {
            'exposure': 0.0,
            'gains': (1.0, 1.0, 1.0),
            'color_space': -1,
            'gamma': 0,
            'is_srgb_source': False,
            'shadows': 0.0,
            'highlights': 0.0,
            'contrast': 1.0,
            'saturation': 1.0,
            'lift': 0.0,
            'gain': 1.0,
            'color_boost': 0.0,
            'mid_detail': 0.0,
            'sharpness': 0.0,
        }

    def _build_video_raw_params(self) -> Dict[str, Any]:
        """Build raw preview params for video, bypassing the RAW panel unless enabled."""
        curr = getattr(self.engine, 'current_raw_params', {}) or {}
        if not curr.get('enabled', False):
            return self._identity_video_raw_params()

        saturation = float(curr.get('saturation', 100.0))
        if saturation > 2.0:
            saturation /= 100.0

        return {
            'exposure': float(curr.get('exposure', 0.0)),
            'gains': (1.0, 1.0, 1.0),
            'color_space': -1,
            'gamma': 0,
            'is_srgb_source': False,
            'shadows': float(curr.get('shadows', 0.0)),
            'highlights': float(curr.get('highlights', 0.0)),
            'contrast': float(curr.get('contrast', 1.0)),
            'saturation': saturation,
            'lift': float(curr.get('lift', 0.0)),
            'gain': float(curr.get('gain', 1.0)),
            'color_boost': float(curr.get('color_boost', 0.0)),
            'mid_detail': float(curr.get('mid_detail', 0.0)),
            'sharpness': float(curr.get('sharpness', 0.0)),
        }

    def _get_playback_render_params(
        self,
        frame_width: int = 0,
        frame_height: int = 0,
        source_width: int = 0,
        source_height: int = 0,
        render_at_source_resolution: bool = False,
    ) -> Dict[str, Any]:
        """Return render params with cached layer data and live viewport state."""
        if self._cached_render_params is None:
            self._cached_render_params = self._build_render_params()

        params = self._cached_render_params.copy()

        viewport_size = getattr(self.engine, 'viewport_size', (0, 0))
        if not viewport_size or viewport_size[0] <= 0 or viewport_size[1] <= 0:
            viewport_size = (
                int(frame_width) if frame_width > 0 else 1,
                int(frame_height) if frame_height > 0 else 1,
            )
        if render_at_source_resolution and frame_width > 0 and frame_height > 0:
            # Keep the decoded master frame at its native resolution *level*,
            # while matching the display/timeline aspect ratio.  The decoded
            # dimensions are not always the presentation dimensions (proxy
            # metadata, pixel-aspect corrections, and timeline overrides can
            # all differ), so using them verbatim creates an incorrectly
            # shaped intermediate FBO.  The Edit viewer follows the same
            # policy: render at a source-sized target, then fit once at the
            # viewer boundary.
            scene_size = self._get_playback_scene_size(source_width, source_height)
            if scene_size and scene_size[0] > 0 and scene_size[1] > 0:
                scene_w, scene_h = scene_size
                display_aspect = float(scene_w) / float(scene_h)
                frame_aspect = float(frame_width) / float(frame_height)
                if abs(frame_aspect - display_aspect) > 1e-4:
                    if frame_aspect > display_aspect:
                        target_h = int(frame_height)
                        target_w = max(1, int(round(target_h * display_aspect)))
                    else:
                        target_w = int(frame_width)
                        target_h = max(1, int(round(target_w / display_aspect)))
                    viewport_size = (target_w, target_h)
                else:
                    viewport_size = (int(frame_width), int(frame_height))
            else:
                viewport_size = (int(frame_width), int(frame_height))

        scene_visible_rect = getattr(self.engine, 'visible_rect', (0, 0, 0, 0))
        if (
            not scene_visible_rect
            or len(scene_visible_rect) != 4
            or scene_visible_rect[2] <= 0
            or scene_visible_rect[3] <= 0
        ):
            fallback_scene_size = self._get_playback_scene_size(source_width, source_height)
            fallback_w = float(fallback_scene_size[0]) if fallback_scene_size else float(frame_width)
            fallback_h = float(fallback_scene_size[1]) if fallback_scene_size else float(frame_height)
            scene_visible_rect = (
                0.0,
                0.0,
                fallback_w if fallback_w > 0 else float(viewport_size[0]),
                fallback_h if fallback_h > 0 else float(viewport_size[1]),
            )

        visible_rect = tuple(float(v) for v in scene_visible_rect)
        scene_size = self._get_playback_scene_size(source_width, source_height)
        if (
            scene_size
            and len(scene_size) == 2
            and frame_width > 0
            and frame_height > 0
            and scene_size[0] > 0
            and scene_size[1] > 0
        ):
            scene_w, scene_h = float(scene_size[0]), float(scene_size[1])
            if abs(scene_w - frame_width) > 0.5 or abs(scene_h - frame_height) > 0.5:
                scale_x = float(frame_width) / scene_w
                scale_y = float(frame_height) / scene_h
                vx, vy, vw, vh = visible_rect
                visible_rect = (
                    vx * scale_x,
                    vy * scale_y,
                    vw * scale_x,
                    vh * scale_y,
                )

        params['viewport_size'] = tuple(int(v) for v in viewport_size)
        params['visible_rect'] = tuple(float(v) for v in visible_rect)
        params['scene_visible_rect'] = tuple(float(v) for v in scene_visible_rect)
        params['grain_visible_rect'] = params['visible_rect']
        params['show_mask'] = bool(getattr(self.engine, 'show_mask_mode', False))
        params['active_layer_index'] = int(getattr(self.engine, 'active_layer_index', 0))
        params['render_reason'] = 'playback'
        return params

    @staticmethod
    def _get_decoded_frame_source_size(frame: Any) -> tuple[int, int]:
        """Return the full source size for a decoded frame when metadata carries it."""
        if frame is None:
            return (0, 0)

        metadata = getattr(frame, 'metadata', None)
        width = int(getattr(metadata, 'width', 0) or 0)
        height = int(getattr(metadata, 'height', 0) or 0)
        if width > 0 and height > 0:
            return (width, height)

        extra = getattr(frame, 'extra', None) or {}
        for width_key, height_key in (
            ('original_width', 'original_height'),
            ('source_width', 'source_height'),
        ):
            width = int(extra.get(width_key, 0) or 0)
            height = int(extra.get(height_key, 0) or 0)
            if width > 0 and height > 0:
                return (width, height)

        return (0, 0)

    def _get_playback_scene_size(
        self,
        source_width: int = 0,
        source_height: int = 0,
    ) -> Optional[tuple[float, float]]:
        """
        Pick the coordinate space used by engine.visible_rect.

        During playback the displayed frame is often a proxy, while the canvas
        scene can be the timeline resolution.  The visible_rect comes from the
        canvas, so prefer an explicit display scene or engine.scene_size whenever
        valid.  Decoded metadata is only a fallback because shared proxy sources
        may report the proxy size rather than the original clip size.
        """
        if self._display_scene_size is not None:
            return (float(self._display_scene_size[0]), float(self._display_scene_size[1]))

        scene_size = getattr(self.engine, 'scene_size', None)
        if (
            scene_size
            and len(scene_size) == 2
            and scene_size[0] > 0
            and scene_size[1] > 0
        ):
            return (float(scene_size[0]), float(scene_size[1]))

        if source_width > 0 and source_height > 0:
            return (float(source_width), float(source_height))

        return None

    def _build_render_params(self) -> Dict[str, Any]:
        """
        构建渲染参数（播放时缓存复用）。

        从 engine 获取当前调色参数，避免每帧重建。

        P3-4: 播放时根据效果预览质量管理器降级重型效果。

        Returns:
            渲染参数字典，包含 layers, viewport_size, raw_preview 等
        """
        params = {
            'viewport_size': (0, 0),  # 由 renderer 使用视频尺寸填充
            'video_stabilization': getattr(self.engine, 'video_stabilization', {}),
            'layers': [],
            'raw_preview': self._identity_video_raw_params(),
            'film_grain': {},
            'visible_rect': (0, 0, 0, 0),  # 每帧由 _get_playback_render_params 刷新
            'grain_visible_rect': (0, 0, 0, 0),
            'show_mask': False,
            'active_layer_index': 0,
        }

        # P3-4: 获取效果预览质量管理器
        quality_manager = get_effect_preview_quality_manager()
        is_playing = self.is_playing

        # 从 engine 获取 layers 数据
        if hasattr(self.engine, 'layers') and self.engine.layers:
            layers_data = []
            for L in self.engine.layers:
                layer_plugins = []
                if hasattr(L, 'plugins') and L.plugins:
                    for plugin_inst in L.plugins:
                        plugin_id = plugin_inst.get('plugin_id')
                        plugin_enabled = plugin_inst.get('enabled', True)

                        # P3-4: 播放时检查插件是否需要跳过
                        # 如果插件标记为 SKIP preview_quality，播放时禁用
                        if is_playing and quality_manager.should_skip_effect(f"plugin:{plugin_id}"):
                            plugin_enabled = False

                        layer_plugins.append({
                            'plugin_id': plugin_id,
                            'enabled': plugin_enabled,
                            'params': plugin_inst.get('params', {}).copy()
                        })

                # P3-4: AI Matte 播放时锁定
                ai_matte_enabled = L.ai_matte_enabled if hasattr(L, 'ai_matte_enabled') else False
                if is_playing and ai_matte_enabled and quality_manager.should_skip_effect("ai_matte"):
                    ai_matte_enabled = False

                # P3-4: 高半径 blur 检查
                effects_params = L.effects_params.copy() if hasattr(L, 'effects_params') and L.effects_params else None
                if is_playing and effects_params:
                    blur_radius = effects_params.get('blur_radius', 0.0)
                    if blur_radius > 20.0 and quality_manager.is_effect_degraded("blur_high_radius"):
                        # 限制 blur 半径
                        effects_params['blur_radius'] = 20.0
                        effects_params['blur_iterations'] = 1

                layers_data.append({
                    'id': id(L),
                    'visible': L.visible,
                    'opacity': L.opacity,
                    'blend_mode': L.blend_mode,
                    'grading_params': L.grading_params.copy() if L.grading_params else None,
                    'active_lut_path': L.active_lut_path,
                    'lut_intensity': L.lut_intensity,
                    'soft_clip_params': L.soft_clip_params.copy() if L.soft_clip_params else None,
                    'qualifier_params': L.qualifier_params.copy() if L.qualifier_params else None,
                    'curves_lut': L.curves_lut,
                    'hsl_curve_lut': L.hsl_curve_lut,
                    'warper_params': L.warper_params,
                    'mixer_params': L.mixer_params.copy() if L.mixer_params else None,
                    'effects_params': effects_params,
                    'mask_shapes': [s.to_dict() for s in L.mask_shapes],
                    'has_mask': any(s.visible for s in L.mask_shapes) or (L.ai_matte is not None and ai_matte_enabled),
                    # P3-4: 使用修改后的 ai_matte_enabled
                    'ai_matte_enabled': ai_matte_enabled,
                    'plugins': layer_plugins
                })
            params['layers'] = layers_data

            # 找到活跃图层
            for i, L in enumerate(self.engine.layers):
                if hasattr(self.engine, '_active_layer_index'):
                    params['active_layer_index'] = self.engine._active_layer_index

        params['raw_preview'] = self._build_video_raw_params()

        # film_grain 参数
        # P3-4: 播放时降级 film_grain（由 shader_compiler.py 的 update_uniforms 处理）
        if hasattr(self.engine, 'film_grain_params'):
            grain_params = self.engine.film_grain_params.copy() if self.engine.film_grain_params else {}
            # 使用降级后的参数（如果正在播放）
            params['film_grain'] = get_degraded_effect_params("film_grain", grain_params)

        # N01：视频播放出口附图快照（flag 开启时图执行接管播放调色；
        # 快照身份含 graph_revision——图命令提交后缓存经
        # invalidate_cached_render_params 失效重建）
        attach = getattr(self.engine, '_attach_color_graph_params', None)
        if callable(attach):
            attach(params,
                   execute=bool(getattr(self.engine,
                                        'color_graph_render_enabled', False)))
        return params

    def invalidate_cached_render_params(self) -> None:
        """
        清除缓存渲染参数，调色参数变化时调用。
        """
        self._cached_render_params = None
        self._render_params_generation += 1
        _flash_diag.event(
            'params_invalidate', gen=self._render_params_generation,
            playing=bool(getattr(self, 'is_playing', False)),
        )
        renderer = getattr(self.engine, 'renderer', None)
        discard = getattr(renderer, 'discard_pending_grading_refresh', None)
        if callable(discard):
            discard()

    # ================================================================
    # 异步帧预读控制
    # ================================================================

    # ================================================================
    # 播放控制（兼容 VideoController 接口）
    # ================================================================

    def _set_renderer_playback_active(self, active: bool) -> None:
        """Tell the Color renderer which command lane owns presentation."""
        renderer = getattr(self.engine, 'renderer', None)
        if renderer is not None:
            _flash_diag.event(
                'play_state', active=bool(active),
                frame=int(getattr(self, 'current_frame', -1) or -1),
            )
            setattr(renderer, 'video_playback_active', bool(active))
            if active:
                reset_history = getattr(renderer, 'reset_video_denoise_history', None)
                if callable(reset_history):
                    reset_history()
                discard = getattr(renderer, 'discard_pending_preview_refreshes', None)
                if callable(discard):
                    discard()

    def start_playback(self):
        """开始流畅播放 — v2 桩方法（GStreamer 预读链路已移除）。"""
        if self.is_playing:
            return
        logger.error(
            "VideoPlaybackEngine.start_playback: v2 GStreamer pipeline removed; "
            "use EnhancedVideoPlaybackEngine (v3) instead"
        )

    def stop_playback(self):
        """停止播放"""
        self._set_renderer_playback_active(False)
        if not self.is_playing:
            return

        self.is_playing = False
        self._play_timer.stop()
        self.play_state_changed.emit(False)

        # P2-1: 从全局追踪器注销播放状态
        _register_playback_engine(id(self), False)

        # P3-4: 恢复效果预览质量管理器状态（恢复完整质量）
        get_effect_preview_quality_manager().set_playback_state(False)

        # P0.A: 结束性能监控会话
        self._perf_monitor.end_session()

    def toggle_play(self):
        """切换播放/暂停"""
        if self.is_playing:
            self.stop_playback()
        else:
            self.start_playback()

    # ── In/Out 播放范围（循环播放该区间）──────────────────────────────────
    # 设置后，播放到 out_frame 自动跳回 in_frame 循环；不影响手动 seek/scrub。
    def set_in_out_range(self, in_frame: int, out_frame: Optional[int]) -> None:
        """设置 In/Out 播放范围（帧号）。

        Args:
            in_frame: 入点（含）。out_frame: 出点（含），None 表示到素材末尾。
            in_frame > out_frame 时忽略本次设置（非法区间）。
        """
        # v2 decoder 已移除；基类无法得知总帧数（Enhanced 子类会覆写此方法）
        total_frames = 0
        total = max(0, total_frames - 1)
        if total_frames <= 0:
            effective_out = int(out_frame) if out_frame is not None else max(0, int(in_frame))
        else:
            effective_out = total if out_frame is None else min(int(out_frame), total)
        if in_frame > effective_out:
            logger.warning(
                f"Ignore invalid I/O range: in={in_frame} > out={effective_out}")
            return
        self.in_frame = max(0, int(in_frame))
        self.out_frame = effective_out

    def clear_in_out_range(self) -> None:
        """清除 In/Out 播放范围（恢复全片播放）。"""
        self.in_frame = 0
        self.out_frame = None

    def _in_out_range_active(self) -> bool:
        """In/Out 范围是否已设置（用户标记过）。"""
        return self.out_frame is not None

    def _effective_out_frame(self) -> Optional[int]:
        """实际生效的出点帧号：out_frame（已设）否则 total_frames - 1。

        V-P0-09: 未知 total_frames 时返回 ``None``，表示没有已知播放终点，
        结束只能由明确 EOS 驱动，避免未知时长素材在首帧即停。
        （v2 decoder 已移除，基类恒为未知时长；Enhanced 子类会覆写。）
        """
        total = None
        if self._in_out_range_active() and self.out_frame is not None:
            return self.out_frame if total is None else min(self.out_frame, total)
        return total

    def _on_play_tick(self) -> None:
        """播放定时器回调 — v2 桩方法。

        v2 预读队列消费逻辑已随 GStreamer 链路移除；
        v3 模式由 EnhancedVideoPlaybackEngine._on_play_tick 驱动。
        """
        self.stop_playback()

    def _check_proxy_ladder(self) -> None:
        """
        P2.2: 检查代理阶梯，根据帧总耗时动态调整 proxy 分辨率。

        降级条件：连续 60 帧（约 2 秒）total_p95 > frame_budget * 1.25
        升级条件：连续 150 帧（约 5 秒）total_p95 < frame_budget * 0.65

        当阶梯变化时，更新代理分辨率（v2 decoder 已移除，原始尺寸取
        media display size；播放中的解码分辨率由 v3 源管理）。
        """
        if not self._use_proxy or not self._proxy_size:
            return

        proxy_long_edge, reason = self._perf_monitor.check_proxy_ladder()

        if reason == "stable" or reason == "no_data" or reason == "insufficient_data" or reason == "no_video":
            return

        if not self._media_display_size:
            return

        vid_w, vid_h = self._media_display_size
        original_long_edge = max(vid_w, vid_h)

        # 如果 proxy_long_edge >= original_long_edge，恢复原始分辨率
        if proxy_long_edge >= original_long_edge:
            new_proxy_w, new_proxy_h = vid_w, vid_h
        else:
            scale = proxy_long_edge / original_long_edge
            new_proxy_w = max(64, int(vid_w * scale))
            new_proxy_h = max(64, int(vid_h * scale))

        old_proxy_size = self._proxy_size
        new_proxy_size = (new_proxy_w, new_proxy_h)

        if new_proxy_size == old_proxy_size:
            return

        logger.info(
            f"ProxyLadder: adjusting proxy from {old_proxy_size} to {new_proxy_size}, "
            f"reason={reason}"
        )

        self._proxy_size = new_proxy_size

        # 更新代理预取工作线程（旧架构）
        if self._proxy_prefetch_worker and hasattr(self._proxy_prefetch_worker, 'update_target_size'):
            self._proxy_prefetch_worker.update_target_size(new_proxy_size)

    def seek_to_frame(self, frame_number: int):
        """
        跳转到指定帧（高质量路径）。
        
        Args:
            frame_number: 目标帧号
        """
        if not self.current_path:
            return

        self.stop_playback()

        # P4.B: Increment generation ID on seek to drop stale frames
        self._generation_id += 1
        logger.debug(f"Seek to frame {frame_number}, generation_id={self._generation_id}")

        self._show_frame_hq(frame_number)

    def seek_to_time(self, time_seconds: float):
        """跳转到指定时间"""
        if not self.current_path:
            return
        fps = self._playback_clock.fps if self._playback_clock else 30.0
        frame_number = int(time_seconds * fps)
        self.seek_to_frame(frame_number)

    def next_frame(self):
        """下一帧（v2 桩：由子类 seek_to_frame 路径接管）"""
        if not self.current_path:
            return
        self.stop_playback()
        self._show_frame_hq(self.current_frame + 1)

    def prev_frame(self):
        """上一帧（v2 桩：由子类 seek_to_frame 路径接管）"""
        if not self.current_path:
            return
        self.stop_playback()
        self._show_frame_hq(self.current_frame - 1)

    def first_frame(self):
        """跳到第一帧"""
        self.seek_to_frame(0)

    def last_frame(self):
        """跳到最后一帧（v2 桩：帧数未知时 seek 大帧号由子类 clamp）"""
        if self._playback_clock and self._playback_clock.total_frames > 0:
            self.seek_to_frame(self._playback_clock.total_frames - 1)
        else:
            self.seek_to_frame(max(0, self.current_frame))

    def set_playback_speed(self, speed: float) -> None:
        """
        设置播放速度。

        Args:
            speed: 播放速度倍数（1.0 为正常速度，必须为正数）

        AUD-39602: speed 是外部输入，必须验证——传 0 触发
        ZeroDivisionError（1000/(fps*0)），负值把 interval 钳到 8ms
        使播放定时器以 125Hz 空转。非法值拒绝并保持原速。
        """
        speed_value = _validated_playback_speed(speed)
        if speed_value is None:
            logger.warning(f"Ignoring invalid playback speed: {speed!r}")
            return

        if self.is_playing:
            fps = self._playback_clock.fps if self._playback_clock else 30.0
            if fps <= 0:
                return
            interval = int(1000 / (fps * speed_value))
            interval = max(8, int(interval * 0.8))
            self._play_timer.setInterval(interval)

    # ================================================================
    # 代理分辨率控制
    # ================================================================

    def update_viewport_size(self, width: int, height: int) -> None:
        """
        更新 viewport 尺寸（影响代理分辨率计算）。
        
        应在 viewport resize 时调用。
        
        Args:
            width: viewport 宽度
            height: viewport 高度
        """
        self.engine.viewport_size = (width, height)
        self._update_proxy_size()

    # ================================================================
    # 信息查询（兼容 VideoController 接口）
    # ================================================================

    def get_current_time(self) -> float:
        """获取当前播放时间（秒）"""
        fps = self._playback_clock.fps if self._playback_clock else 0.0
        if fps > 0:
            return self.current_frame / fps
        return 0.0

    def get_time_string(self) -> str:
        """获取格式化的时间字符串 HH:MM:SS / HH:MM:SS"""
        def format_time(seconds: float) -> str:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        current = self.get_current_time()
        fps = self._playback_clock.fps if self._playback_clock else 0.0
        total = (self._playback_clock.total_frames / fps) if fps > 0 else 0.0
        return f"{format_time(current)} / {format_time(total)}"

    def get_video_info(self) -> Optional[Dict[str, Any]]:
        """获取当前视频信息 — v2 桩方法（decoder 已移除，子类覆写）。"""
        if not self.current_path:
            return None
        return {
            'path': self.current_path,
            'current_frame': self.current_frame,
        }

# =============================================================================
# EnhancedVideoPlaybackEngine: 集成 SourceManager 和 PlaybackClock
# =============================================================================

class EnhancedVideoPlaybackEngine(VideoPlaybackEngine):
    """
    增强的视频播放引擎（P5.A 改造版）。

    **P5.A 改造说明**：
    
    本类已变薄为 UI 信号兼容层，实际播放管理委托给 PlaybackCoordinator：
    - 保留原有的 PyQt 信号（frame_changed, frame_ready, play_state_changed 等）
    - 保持 UI 接口不变（load_video, start_playback, stop_playback, seek_to_frame, next_frame, prev_frame）
    - 内部使用 PlaybackCoordinator 进行播放调度
    - MainWindow 不需要知道 worker/cache 细节
    - 旧控制栏信号不变

    **架构层次**：
    
    ```
    MainWindow / Canvas
        ↓ 信号连接
    EnhancedVideoPlaybackEngine（UI 信号兼容层）
        ↓ 委托
    PlaybackCoordinator（播放协调器）
        ↓ 管理
    VideoSession（视频会话）
        ↓ 组合
    SourceManager + PlaybackClock + DecodeWorkerManager + ReadyFrameQueue
    ```

    **保留特性**（向后兼容）：
    1. Master/Proxy 源分离：暂停时使用高质量 master，播放时使用代理 proxy
    2. 时钟驱动调度：基于 wall clock 的精确帧调度
    3. 智能预读缓存：根据播放状态动态预读
    4. 丢帧统计：记录播放过程中的丢帧数

    Attributes:
        _coordinator: PlaybackCoordinator 实例（P5.A 新增）
        _source_manager: 源管理器（向后兼容属性）
        _playback_clock: 播放时钟（向后兼容属性）
        _frame_scheduler: 帧调度器（向后兼容属性）
        _use_dual_path: 是否启用双路径架构
    """

    def __init__(self, engine) -> None:
        """
        初始化增强的播放引擎。

        Args:
            engine: ImageEngine 实例
        """
        super().__init__(engine)

        # P5.A: PlaybackCoordinator（播放协调器）
        self._coordinator: Optional[Any] = None  # PlaybackCoordinator

        # 新架构组件（向后兼容属性，实际从 coordinator 获取）
        self._source_manager: Optional[Any] = None  # SourceManager
        self._playback_clock: Optional[Any] = None  # PlaybackClock
        self._frame_scheduler: Optional[Any] = None  # FrameScheduler

        # P1.B: Proxy 预读 Worker（后台线程，不阻塞 UI）
        self._proxy_prefetch_worker: Optional[ProxyPrefetchWorker] = None

        # 是否启用双路径架构
        self._use_dual_path: bool = False

        # 代理配置
        self._proxy_config: Optional[Any] = None  # ProxyConfig

        # P1.B: 最后显示的帧（用于 hold last frame）
        # P0-1: 保持 numpy 数组引用用于向后兼容（如 seek/第一帧显示等非热路径）
        self._last_displayed_frame: Optional[np.ndarray] = None

        # P0-1: 轻量状态 - 用于 UI tick hold last frame，避免大数组复制
        # 只保存 DecodedFrame 引用和帧号，不复制 numpy 数组
        self._last_displayed_decoded_frame: Optional[Any] = None  # DecodedFrame 引用
        self._last_displayed_frame_index: Optional[int] = None  # 帧号
        self._last_submitted_render_params_generation: int = -1

    def _range_total_frames(self) -> int:
        """Return the active source frame count across v3 and legacy paths."""
        if self._coordinator is not None:
            try:
                total = int(self._coordinator.total_frames or 0)
                if total > 0:
                    return total
            except (TypeError, ValueError):
                pass
        if self._source_manager is not None:
            try:
                total = int(self._source_manager.total_frames or 0)
                if total > 0:
                    return total
            except (TypeError, ValueError):
                pass
        return 0

    def set_in_out_range(self, in_frame: int, out_frame: Optional[int]) -> None:
        """Set playback range for both v3 coordinator and legacy decoder paths."""
        total_frames = self._range_total_frames()
        total = max(0, total_frames - 1)
        if total_frames <= 0:
            effective_out = int(out_frame) if out_frame is not None else max(0, int(in_frame))
        else:
            effective_out = total if out_frame is None else min(int(out_frame), total)
        in_frame = max(0, int(in_frame))
        if in_frame > effective_out:
            logger.warning(
                f"Ignore invalid I/O range: in={in_frame} > out={effective_out}"
            )
            return
        self.in_frame = in_frame
        self.out_frame = effective_out

    def _effective_out_frame(self) -> Optional[int]:
        """Actual inclusive out frame for v3 coordinator and legacy paths.

        V-P0-09: 当素材时长未知（total_frames 为 0 / None / 无法解析）时返回
        ``None``，表示"没有已知的播放终点"。播放结束必须由明确的 EOS
        （解码器上报 / coordinator EOF）驱动，而不是由一个塌缩成 0 的上界驱动——
        否则任何 ``current_frame`` 都会被误判为越界并提前停止/循环，导致未知时长
        素材在首帧即停。只有"已知总帧数"或"用户显式设置了 Out 点"时才返回有限值。
        """
        known_total = self._range_total_frames()
        total = max(0, known_total - 1) if known_total > 0 else None
        if self._in_out_range_active() and self.out_frame is not None:
            user_out = int(self.out_frame)
            if total is None:
                return user_out
            return min(user_out, total)
        return total

    def enable_dual_path(
        self,
        enabled: bool = True,
        proxy_config: Optional[Any] = None
    ) -> None:
        """
        启用/禁用双路径架构（P5.A 改造版）。

        Args:
            enabled: 是否启用
            proxy_config: 代理配置（ProxyConfig）

        Note:
            P5.A: 现在使用 PlaybackCoordinator 进行播放管理，
            此方法初始化 PlaybackCoordinator 并设置回调。

        S0.A: 如果 v3 初始化失败，自动 fallback 到旧路径并记录 warning。
        """
        self._use_dual_path = enabled
        self._proxy_config = proxy_config

        if enabled:
            # P5.A: 初始化 PlaybackCoordinator
            init_success = self._init_playback_coordinator()

            # S0.A: 如果初始化失败，自动 fallback 到旧路径
            if not init_success:
                # P0-4: 记录详细的 fallback 事件
                log_fallback(
                    reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                    video_path=self.current_path,
                    user_forced=False,
                    effective_pipeline='v2',
                    notes="PlaybackCoordinator initialization failed",
                )
                logger.warning(
                    "v3 PlaybackCoordinator 初始化失败（v2 GStreamer 链路已移除，"
                    "后续 load_video 将直接报错返回）"
                )
                self._use_dual_path = False
                self._coordinator = None
                return

            if self.current_path:
                # 当前有视频，重新加载以使用新架构
                self._init_dual_path_components()

    def _init_playback_coordinator(self) -> bool:
        """
        初始化 PlaybackCoordinator（P5.A 新增）。

        创建 PlaybackCoordinator 并设置回调，用于播放管理和调度。

        S0.A: 返回初始化是否成功，失败时不影响 load_video()。

        Returns:
            True 表示成功初始化，False 表示失败需要 fallback
        """
        if self._coordinator is not None:
            return True

        try:
            from src.shared.video.playback_state import PlaybackCoordinator

            # 获取视口大小
            viewport_size = getattr(self.engine, 'viewport_size', None)

            # 创建 PlaybackCoordinator
            self._coordinator = PlaybackCoordinator(
                viewport_size=viewport_size,
                prefetch_lookahead=8,
                max_proxy_queue=12,
                max_master_queue=3,
            )

            # 设置回调
            self._coordinator.set_on_frame_ready(self._on_coordinator_frame_ready)
            self._coordinator.set_on_state_change(self._on_coordinator_state_change)

            logger.info("PlaybackCoordinator initialized")
            return True

        except Exception as e:
            logger.error(f"Failed to init PlaybackCoordinator: {e}")
            traceback.print_exc()
            self._coordinator = None
            return False

    def _on_coordinator_frame_ready(self, frame: Any, frame_index: int) -> None:
        """
        PlaybackCoordinator 帧就绪回调（P5.A 新增，S2.A 修改）。

        当 PlaybackCoordinator 有新帧就绪时触发。

        S2.A: 直接提交 DecodedFrame，不再先调用 _process_coordinator_frame。

        Args:
            frame: DecodedFrame 或 numpy 数组
            frame_index: 帧号
        """
        # S2.A: 直接提交 DecodedFrame
        if frame is not None:
            self._submit_decoded_frame_playback(frame, frame_index)

    def _on_coordinator_state_change(self, state_name: str) -> None:
        """
        PlaybackCoordinator 状态变化回调（P5.A 新增）。

        Args:
            state_name: 状态名称（PLAYING, PAUSED, READY 等）
        """
        # 映射到播放状态信号
        is_playing = state_name == 'PLAYING'
        self.play_state_changed.emit(is_playing)

    def _begin_static_preview_generation(self) -> int:
        """Advance generation so queued playback frames cannot repaint over a seek preview."""
        # ``EnhancedVideoPlaybackEngine.__new__`` is used by lightweight
        # controller tests; QObject raises on a missing attribute before its
        # superclass constructor runs, so read the instance dictionary first.
        self._generation_id = int(self.__dict__.get('_generation_id', 0) or 0) + 1
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None)
        if renderer is not None:
            try:
                renderer._video_generation_id = max(
                    int(getattr(renderer, "_video_generation_id", 0) or 0),
                    self._generation_id,
                )
            except Exception:
                pass
        return self._generation_id

    def _set_static_preview_frame_hold(self, active: bool) -> None:
        """Retain the last completed viewport frame during an exact static seek."""
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        if engine is not None:
            setattr(engine, "_hold_viewport_frame", bool(active))

    def begin_clip_switch_preview_guard(self) -> None:
        """Arm flash protection before a Color clip-switch transaction starts.

        The switch transaction runs flush/fit/hydration steps before
        ``request_static_preview_frame`` is reached.  Those steps submit
        grading and viewport RENDER_FRAME commands; executed while the
        departing clip's video planes are still bound (and before the
        pending/generation guards are armed) they publish the previous
        shot for one frame.  The flash becomes visible when the user
        starts dragging a grading control right after selecting the clip:
        the first drag's render lands inside that unguarded window.
        Arming here closes the window for the whole transaction.
        """
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is not None:
            # Drop queued grading/viewport refreshes that still reference
            # the departing clip's source texture.
            discard = getattr(renderer, "discard_pending_preview_refreshes", None)
            if callable(discard):
                try:
                    discard()
                except Exception:
                    pass
        # Bump the generation so any RENDER_FRAME already past the lane
        # slots is rejected by _do_render's stale-generation gate, then
        # arm the pending gate and withdraw the front texture.
        self._begin_static_preview_generation()
        self._mark_renderer_video_texture_pending()

    def withdraw_static_preview_pending(self) -> None:
        """Disarm the clip-switch guard when no replacement frame will come.

        Used when the static-preview path aborts (video not loaded) or the
        requested frame is already displayed, so the withdrawn front
        texture does not leave the viewport permanently blank.
        """
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is not None:
            renderer.video_texture_pending = False
        # Republish the held source so the withdrawn front texture is
        # replaced instead of leaving the canvas blank.
        if engine is not None:
            try:
                engine.render_pipeline(render_reason='viewport_refresh')
            except (AttributeError, RuntimeError) as e:
                # AUD-39605: 收窄为 Qt 包装对象失效类异常（引擎/管线已
                # 销毁），其余领域错误不得静默吞掉
                logger.debug(
                    "withdraw_static_preview_pending: render_pipeline "
                    "unavailable: %s", e)
        canvas = getattr(engine, "canvas_ref", None) if engine else None
        if canvas is not None:
            try:
                canvas.viewport().update()
            except (AttributeError, RuntimeError):
                pass

    def static_preview_shows_frame(self, frame_number: int) -> bool:
        """Return True when the paused viewer already presents this frame."""
        if bool(getattr(self, "is_playing", False)):
            return False
        if str(getattr(self, "current_path", "") or "") == "":
            return False
        try:
            if int(getattr(self, "current_frame", -1) or -1) != int(frame_number):
                return False
        except (TypeError, ValueError):
            return False
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is None:
            return False
        return (
            getattr(renderer, "current_video_planes", None) is not None
            or getattr(renderer, "current_video_texture", None) is not None
        )

    def _mark_renderer_video_texture_pending(self) -> None:
        """Flag the renderer's video texture as stale during a clip switch.

        Set before an async seek so any RENDER_FRAME (grading/viewport_refresh)
        queued before the new frame's RENDER_VIDEO_FRAME is processed will be
        skipped by _do_render — otherwise it would repaint the departing clip's
        texture on the Canvas and DeckLink.  Cleared automatically when the new
        RENDER_VIDEO_FRAME uploads a fresh texture.
        """
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is not None:
            renderer.video_texture_pending = True
            # ``current_front_tex_id`` is the texture Canvas actually blits.
            # Merely marking the source texture pending still leaves that
            # previous front texture drawable until Canvas happens to repaint,
            # which made a selected clip appear to keep the preceding clip's
            # tail frame (and let it flash once when playback started).  The
            # texture remains allocated in the renderer's double-buffer pair;
            # only its presentation handle is withdrawn until the requested
            # frame publishes a new completed front texture.
            renderer.current_front_tex_id = None
            renderer.current_front_size = (0, 0)

        # Request a viewport paint after withdrawing the front texture.  The
        # GL viewport clears its background, so the old pixels cannot persist
        # while the exact current-clip frame is decoding asynchronously.
        canvas = getattr(engine, "canvas_ref", None) if engine else None
        if canvas is not None:
            try:
                canvas.viewport().update()
            except (AttributeError, RuntimeError):
                pass

    def is_video_loaded(self) -> bool:
        """Return loaded state for both v3 coordinator and legacy decoder paths."""
        if self._use_dual_path and self._coordinator is not None:
            try:
                if bool(getattr(self._coordinator, "is_loaded", False)):
                    return True
            except Exception:
                pass
            return bool(self.current_path)
        return super().is_video_loaded()

    def _process_coordinator_frame(self, frame: Any) -> Optional[np.ndarray]:
        """
        处理 PlaybackCoordinator 返回的帧（P5.A 新增）。

        Args:
            frame: DecodedFrame 或 numpy 数组

        Returns:
            处理后的 numpy 数组（BGR uint8）
        """
        # 如果已经是 numpy 数组，直接返回
        if isinstance(frame, np.ndarray):
            return frame

        # 如果是 DecodedFrame，使用现有处理逻辑
        return self._process_decoded_frame(frame)

    def _init_dual_path_components(self) -> None:
        """
        初始化双路径组件（P5.A 改造版）。

        P5.A: 现在委托给 PlaybackCoordinator，此方法作为向后兼容层。
        实际初始化在 PlaybackCoordinator.load() 中完成。
        """
        if not self._use_dual_path or not self.current_path:
            return

        # P5.A: 如果有 coordinator，同步向后兼容属性
        if self._coordinator is not None:
            self._source_manager = self._coordinator.source_manager
            self._playback_clock = self._coordinator.session.playback_clock if self._coordinator.session else None
            # FrameScheduler 可以从 coordinator 获取
            self._frame_scheduler = None  # P5.A: 保留向后兼容，但实际不使用

        try:
            from src.shared.video import SourceManager, PlaybackClock, FrameScheduler
            from src.shared.media.factory import MediaBackend
            from src.shared.media.proxy import DEFAULT_PROXY_CONFIG

            # 选择 backend（专业格式强制 FFmpeg）
            backend = self._select_backend_for_path(self.current_path)

            # 创建源管理器
            viewport_size = getattr(self.engine, 'viewport_size', None)
            config = self._proxy_config or DEFAULT_PROXY_CONFIG

            # P0-2: v3 默认不使用简化的自定义 source_factory
            # SourceManager 内部会使用完整的工厂函数，支持 target_size、preserve_planes、hwaccel 参数
            # 如果需要强制 backend，通过 preferred_backend 参数传递，而不是自定义工厂
            preferred_backend = None
            if backend == 'ffmpeg':
                preferred_backend = MediaBackend.FFMPEG.value

            self._source_manager = SourceManager(
                path=self.current_path,
                proxy_config=config,
                viewport_size=viewport_size,
                preferred_backend=preferred_backend  # P0-2: 显式传递 backend 而不是自定义工厂
            )

            # 打开源管理器
            if not self._source_manager.open():
                logger.warning("Failed to open SourceManager, falling back to single path")
                self._use_dual_path = False
                self._source_manager = None
                return

            # 创建播放时钟
            fps = self._source_manager.fps
            if fps:
                self._playback_clock = PlaybackClock(
                    fps=fps.to_float(),
                    total_frames=self._source_manager.total_frames or 0
                )
            else:
                logger.warning("SourceManager 未提供 fps，跳过 PlaybackClock 创建")

            # 创建帧调度器
            if self._source_manager and self._playback_clock:
                self._frame_scheduler = FrameScheduler(
                    clock=self._playback_clock,
                    source_manager=self._source_manager
                )

            logger.info(
                f"Dual path enabled: backend={backend}, "
                f"proxy={self._source_manager.proxy_size}, "
                f"fps={self._playback_clock.fps}"
            )

        except Exception as e:
            logger.error(f"Failed to init dual path: {e}")
            traceback.print_exc()
            self._use_dual_path = False
            # AUD-39603: SourceManager.open() 成功后若后续组件创建抛异常，
            # 已打开的解码器/文件句柄不得直接置 None 丢弃——先显式 close
            # 再清理引用，否则底层 C 资源依赖 GC 时机累积泄漏。
            source_manager = self._source_manager
            self._source_manager = None
            self._playback_clock = None
            if source_manager is not None:
                try:
                    source_manager.close()
                except Exception as close_error:
                    logger.warning(
                        f"Failed to close SourceManager after dual-path "
                        f"init failure: {close_error}")
            self._frame_scheduler = None

    def load_video(self, path: str) -> bool:
        """
        加载视频文件（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 进行播放管理，
        保持 UI 信号接口不变。

        Args:
            path: 视频文件路径

        Returns:
            是否加载成功
        """
        # 卸载当前视频
        self.unload_video()

        # 检查是否需要强制使用 FFmpeg backend（专业格式）
        backend = self._select_backend_for_path(path)

        if self._use_dual_path:
            # P5.A: 使用 PlaybackCoordinator 新架构
            try:
                # 确保 PlaybackCoordinator 已初始化
                if self._coordinator is None:
                    self._init_playback_coordinator()

                if self._coordinator is None:
                    # V-P0-3: 统一 fallback 处理
                    fallback_result = fallback_to_v2_or_fail(
                        reason=VideoPipelineFallbackReason.V3_INIT_FAILED,
                        path=path,
                        notes="PlaybackCoordinator 初始化失败",
                        decision=self._pipeline_decision,
                    )
                    # v2 GStreamer 链路已移除：不再回退，直接报错
                    logger.error(fallback_result.message)
                    return False

                # 使用 PlaybackCoordinator 加载视频
                if not self._coordinator.load(path):
                    # V-P0-3: 统一 fallback 处理
                    fallback_result = fallback_to_v2_or_fail(
                        reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED,
                        path=path,
                        notes="PlaybackCoordinator 加载失败",
                        decision=self._pipeline_decision,
                    )
                    # v2 GStreamer 链路已移除：清理并报错，不再回退
                    logger.error(fallback_result.message)
                    if self._coordinator:
                        try:
                            self._coordinator.unload()
                        except Exception:
                            pass
                    self._coordinator = None
                    return False

                # 同步向后兼容属性
                self._source_manager = self._coordinator.source_manager
                self._playback_clock = self._coordinator.session.playback_clock if self._coordinator.session else None

                # 设置基本属性（兼容父类接口）
                self.current_frame = 0
                self.current_path = path

                # 获取视频信息
                fps = self._coordinator.fps or 30.0
                total_frames = self._coordinator.total_frames or 0

                # P2.2: 从 coordinator 获取视频分辨率并初始化代理阶梯
                video_info = self._coordinator.get_video_info() if self._coordinator else None
                if video_info and isinstance(video_info, dict):
                    display_w = video_info.get('display_width', 0)
                    display_h = video_info.get('display_height', 0)
                    if isinstance(display_w, (int, float)) and isinstance(display_h, (int, float)):
                        if display_w > 0 and display_h > 0:
                            self.set_media_display_size((int(display_w), int(display_h)))
                    vid_w = video_info.get('width', 0)
                    vid_h = video_info.get('height', 0)
                    if isinstance(vid_w, (int, float)) and isinstance(vid_h, (int, float)) and vid_w > 0 and vid_h > 0:
                        self._perf_monitor.set_original_resolution(int(vid_w), int(vid_h))

                        # P4-1: ProRes proxy policy log
                        codec_name = video_info.get('codec', '') or ''
                        if 'prores' in codec_name.lower() and (int(vid_w) >= 3840 or int(vid_h) >= 2160):
                            logger.info(
                                f"ProRes 4K direct decode (no proxy). "
                                f"Consider generating proxy for smoother playback. "
                                f"codec={codec_name}, size={int(vid_w)}x{int(vid_h)}"
                            )

                # 发送信号
                self.total_frames_changed.emit(total_frames)
                self.video_loaded.emit(path)
                self.frame_changed.emit(0)

                # P0-3: 显示第一帧（使用三级处理）
                # 只有明确 codec/backend 不支持时才 fallback v2
                # ProRes 第一帧慢、高位深转换失败不应触发 v2 fallback
                result = self._show_first_frame_via_coordinator()

                if result.should_fallback_v2:
                    # V-P0-3/V-P0-5: 统一 fallback 处理
                    # V-P0-5: 只有 fatal reason 才允许 fallback
                    # ProRes/高位深/首帧超时应该只 placeholder，不应 fallback
                    video_info = self._coordinator.get_video_info() if self._coordinator else None
                    codec = video_info.get('codec', 'unknown') if video_info else 'unknown'
                    pixel_format = video_info.get('pixel_format', 'unknown') if video_info else 'unknown'

                    # V-P0-5: ProRes/高位深 格式不 fallback
                    if should_prevent_fallback_for_codec_format(codec, pixel_format):
                        logger.info(
                            f"首帧问题（{result.fallback_reason}）但 codec={codec}, "
                            f"pixel_format={pixel_format} 为专业格式/高位深，"
                            f"保持 v3 状态，显示 placeholder"
                        )
                        return True  # 不 fallback

                    fallback_result = fallback_to_v2_or_fail(
                        reason=result.fallback_reason,
                        path=path,
                        notes=result.error_message,
                        decision=self._pipeline_decision,
                        codec=codec,
                        pixel_format=pixel_format,
                    )
                    # v2 GStreamer 链路已移除：首帧问题不回退，保持 v3 状态
                    logger.info(
                        f"首帧问题（{result.fallback_reason}）不 fallback: "
                        f"{fallback_result.message}"
                    )
                    return True

                elif result.needs_placeholder:
                    # P0-3: 保持 v3 loaded 状态，不 fallback v2
                    # 等待后台解码产生帧后通过 tick 显示
                    logger.info(
                        f"首帧暂未就绪（{result.status}），"
                        f"保持 v3 loaded 状态，后台继续解码"
                    )
                    # 不清理 coordinator，保持 v3 状态
                    # 返回 True 表示加载成功，只是首帧暂时未显示
                    return True

                else:
                    # 首帧显示成功（MASTER_OK 或 PROXY_OK 或 FRAME_PATH_DEGRADED）
                    logger.info(
                        f"首帧显示成功（{result.status}）: "
                        f"frame_path={result.frame_path}, "
                        f"decode={result.decode_ms:.1f}ms, "
                        f"process={result.process_ms:.1f}ms, "
                        f"used_proxy={result.used_proxy}"
                    )
                    return True

                logger.info(
                    f"EnhancedVideoPlaybackEngine (P5.A) 加载成功: "
                    f"path={path}, backend={backend}, "
                    f"fps={fps}, total_frames={total_frames}"
                )
                return True

            except Exception as e:
                logger.error(f"PlaybackCoordinator 加载失败: {e}")
                traceback.print_exc()
                # V-P0-3: 统一 fallback 处理
                fallback_result = fallback_to_v2_or_fail(
                    reason=VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED,
                    path=path,
                    notes=f"Exception: {str(e)}",
                    decision=self._pipeline_decision,
                    exception=e,
                )
                # v2 GStreamer 链路已移除：清理并报错，不再回退
                logger.error(fallback_result.message)
                if self._coordinator:
                    try:
                        self._coordinator.unload()
                    except Exception:
                        pass
                self._coordinator = None
                return False
        else:
            # v2 GStreamer 链路已移除：双路径未启用时无法加载
            logger.error(
                "load_video: dual path 未启用（v2 GStreamer 链路已移除），加载失败"
            )
            return False

    def _show_first_frame_via_coordinator(self) -> FirstFrameResult:
        """
        使用 PlaybackCoordinator 显示第一帧（P0-3 三级处理）。

        P0-3 改进：不再直接 fallback v2，而是实现三级处理：
        1. 先尝试 master 当前帧，限时 500ms
        2. master 失败时尝试 proxy 当前帧，限时 300ms
        3. proxy 也失败时，保持 v3 loaded 状态并显示占位符

        禁止：
        - 因为 ProRes 第一帧慢就 fallback v2
        - 因为高位深 RGB 转换失败就整条 v3 退出
        - 应针对 frame path 降级

        Returns:
            FirstFrameResult: 包含首帧获取状态和是否需要 fallback v2

        Note:
            返回 FirstFrameResult 而非 bool，让调用者能够判断：
            - 是否需要显示占位符（保持 v3 loaded）
            - 是否需要 fallback v2（仅在明确 codec/backend 不支持时）
        """
        result = FirstFrameResult(status=FirstFrameStatus.NOT_READY)

        if not self._coordinator or not self._coordinator.is_loaded:
            result.fallback_reason = VideoPipelineFallbackReason.COORDINATOR_LOAD_FAILED
            result.error_message = "PlaybackCoordinator 未加载"
            return result

        # 获取视频信息用于日志
        video_info = self._coordinator.get_video_info()
        codec = video_info.get('codec', 'unknown') if video_info else 'unknown'
        pixel_format = video_info.get('pixel_format', 'unknown') if video_info else 'unknown'

        # P2-3: First-frame decode strategy
        # Professional codecs (ProRes, DNxHR, etc.) use intra-frame compression
        # so the first frame requires full decode without any reference frames.
        # This makes the first decode slower than typical H.264/H.265 playback.
        # Strategy: allow more time for professional codecs, avoid v2 fallback,
        # and log clearly so users/developers understand the wait is expected.
        logger.info(f"First frame decode for {codec}: starting...")

        is_pro_codec = is_professional_codec(codec)

        # ============================================================================
        # 第一级：尝试获取 master 帧
        # P2-3: Professional codecs get 1200ms (vs 500ms) because their first
        # intra frame decode is inherently slower.  Do NOT fall back to v2 on
        # timeout -- just show a placeholder and let background decode continue.
        # ============================================================================
        master_timeout_ms = 1200 if is_pro_codec else 500
        logger.info(
            f"首帧获取 Level 1: 尝试 master 帧, codec={codec}, pixel_format={pixel_format}, "
            f"timeout={master_timeout_ms}ms{' (professional codec)' if is_pro_codec else ''}"
        )

        master_start = time.perf_counter()

        # 使用 blocking 方法获取 master 帧
        master_frame = self._get_first_frame_blocking(is_master=True, timeout_ms=master_timeout_ms)
        master_elapsed_ms = (time.perf_counter() - master_start) * 1000

        if master_frame is not None:
            # Master 帧获取成功，尝试处理
            process_start = time.perf_counter()
            frame_array = self._process_coordinator_frame(master_frame)
            process_elapsed_ms = (time.perf_counter() - process_start) * 1000

            if frame_array is not None:
                # 处理成功，渲染并返回
                render_success = self._render_first_frame(frame_array, master_frame)
                if render_success:
                    result.status = FirstFrameStatus.MASTER_OK
                    result.frame = master_frame
                    result.decode_ms = master_elapsed_ms
                    result.process_ms = process_elapsed_ms
                    result.frame_path = self._get_frame_path(master_frame)
                    logger.info(
                        f"首帧获取成功 (master): "
                        f"decode={master_elapsed_ms:.1f}ms, process={process_elapsed_ms:.1f}ms, "
                        f"frame_path={result.frame_path}"
                    )
                    return result
                else:
                    # 渲染失败（可能是 renderer 问题）
                    logger.warning("首帧渲染失败，尝试 proxy fallback")
                    # 不直接 fallback v2，继续尝试 proxy
            else:
                # 处理失败（可能是高位深转换问题），记录并尝试 frame path 降级
                logger.warning(
                    f"首帧处理失败 (master, codec={codec}, pixel_format={pixel_format}), "
                    f"尝试 proxy fallback"
                )
                result.status = FirstFrameStatus.FRAME_PATH_DEGRADED

        else:
            # Master 获取超时
            logger.warning(
                f"首帧获取超时 (master, timeout={master_timeout_ms}ms, elapsed={master_elapsed_ms:.1f}ms), "
                f"尝试 proxy fallback"
            )
            result.status = FirstFrameStatus.TIMEOUT_MASTER

        # ============================================================================
        # 第二级：尝试获取 proxy 帧
        # P2-3: Professional codecs also get a longer proxy timeout (600ms vs 300ms).
        # ============================================================================
        proxy_timeout_ms = 600 if is_pro_codec else 300
        logger.info(f"首帧获取 Level 2: 尝试 proxy 帧, timeout={proxy_timeout_ms}ms")

        proxy_start = time.perf_counter()

        proxy_frame = self._get_first_frame_blocking(is_master=False, timeout_ms=proxy_timeout_ms)
        proxy_elapsed_ms = (time.perf_counter() - proxy_start) * 1000

        if proxy_frame is not None:
            # Proxy 帧获取成功
            process_start = time.perf_counter()
            frame_array = self._process_coordinator_frame(proxy_frame)
            process_elapsed_ms = (time.perf_counter() - process_start) * 1000

            if frame_array is not None:
                # 处理成功，渲染并返回
                render_success = self._render_first_frame(frame_array, proxy_frame)
                if render_success:
                    result.status = FirstFrameStatus.PROXY_OK
                    result.frame = proxy_frame
                    result.used_proxy = True
                    result.decode_ms = proxy_elapsed_ms
                    result.process_ms = process_elapsed_ms
                    result.frame_path = self._get_frame_path(proxy_frame)
                    logger.info(
                        f"首帧获取成功 (proxy fallback): "
                        f"decode={proxy_elapsed_ms:.1f}ms, process={process_elapsed_ms:.1f}ms, "
                        f"frame_path={result.frame_path}"
                    )
                    return result
                else:
                    logger.warning("首帧渲染失败 (proxy), 保持 v3 状态")
            else:
                logger.warning(
                    f"首帧处理失败 (proxy, codec={codec}, pixel_format={pixel_format}), "
                    f"保持 v3 状态并显示占位符"
                )

        else:
            # Proxy 获取超时
            logger.warning(
                f"首帧获取超时 (proxy, timeout={proxy_timeout_ms}ms, elapsed={proxy_elapsed_ms:.1f}ms), "
                f"保持 v3 状态并显示占位符"
            )
            result.status = FirstFrameStatus.TIMEOUT_PROXY

        # ============================================================================
        # 第三级：保持 v3 loaded 状态，显示占位符，后台继续解码
        # ============================================================================
        # P0-3: 只有明确 codec/backend 不支持时才 fallback v2
        # ProRes 第一帧慢、高位深转换失败不应触发 v2 fallback

        # v2 GStreamer 链路已移除：永远不回退 v2，保持 v3 状态
        should_fallback = False

        if should_fallback:
            result.fallback_reason = VideoPipelineFallbackReason.CODEC_UNSUPPORTED_BY_V3
            result.error_message = f"Codec/Pixel format 可能不被 v3 支持: codec={codec}, pixel_format={pixel_format}"
            logger.warning(f"首帧获取失败（致命错误）: {result.error_message}, fallback v2")
        else:
            # 保持 v3 状态，显示占位符。
            # BUG-C2-04：Level-1/2 写入的中间状态（TIMEOUT_MASTER /
            # TIMEOUT_PROXY / FRAME_PATH_DEGRADED）此前被无条件覆盖，
            # 调用方永远无法观测进入占位符路径的原因。这里先把中间
            # 状态保留到 degraded_from，再收敛为终态 PLACEHOLDER
            # （needs_placeholder / is_displayable 语义保持不变）。
            if result.status != FirstFrameStatus.NOT_READY:
                result.degraded_from = result.status
            result.status = FirstFrameStatus.PLACEHOLDER
            result.fallback_reason = None  # 不 fallback
            logger.info(
                f"首帧暂未就绪，保持 v3 loaded 状态，后台继续解码 "
                f"(codec={codec}, pixel_format={pixel_format}, "
                f"degraded_from={result.degraded_from.name if result.degraded_from else None})"
            )
            # 显示占位符（可选）
            self._show_first_frame_placeholder()

        return result

    def _get_first_frame_blocking(
        self,
        is_master: bool,
        timeout_ms: int,
    ) -> Optional[Any]:
        """
        同步获取第一帧（带超时）。

        P0-3 新增：用于首帧三级处理。

        Args:
            is_master: 是否获取 master 帧
            timeout_ms: 超时时间（毫秒）

        Returns:
            DecodedFrame 或 None

        Note:
            使用 VideoSession.get_master_frame_blocking() / get_proxy_frame_blocking()
            实现 blocking 获取，不阻塞 UI 线程太久。
        """
        if not self._coordinator or not self._coordinator.session:
            return None

        session = self._coordinator.session

        if is_master:
            # 使用 session 的 blocking 方法获取 master 帧
            return session.get_master_frame_blocking(0, timeout_ms=timeout_ms)
        else:
            # 获取 proxy 帧（blocking）
            return self._get_proxy_frame_blocking(session, 0, timeout_ms=timeout_ms)

    def _get_proxy_frame_blocking(
        self,
        session: Any,
        frame_index: int,
        timeout_ms: int,
    ) -> Optional[Any]:
        """
        同步获取 proxy 帧（带超时）。

        P0-3 新增：session 没有 proxy blocking 方法，这里实现。

        Args:
            session: VideoSession 实例
            frame_index: 目标帧号
            timeout_ms: 超时时间（毫秒）

        Returns:
            DecodedFrame 或 None
        """
        # 优先从队列获取
        if session._frame_queue is not None:
            entry = session._frame_queue.dequeue(is_master=False, frame_index=frame_index)
            if entry is not None:
                return entry.frame

        # 队列没有，直接从 SourceManager 获取（如果有）
        if session._source_manager is not None:
            wait_interval_ms = 10
            max_attempts = timeout_ms // wait_interval_ms

            for attempt in range(max_attempts):
                frame = session._source_manager.get_proxy_frame(frame_index)
                if frame is not None:
                    return frame
                time.sleep(wait_interval_ms / 1000.0)

        return None

    def _render_first_frame(
        self,
        frame_array: np.ndarray,
        decoded_frame: Any,
    ) -> bool:
        """
        渲染首帧到 canvas。

        P0-3 新增：独立渲染方法，避免代码重复。

        Args:
            frame_array: 处理后的 numpy 数组
            decoded_frame: 原始 DecodedFrame

        Returns:
            True 表示渲染成功
        """
        try:
            self.engine.set_image(
                frame_array, frame_array, frame_array,
                is_new_file=True,
                is_video=True,
                video_path=self.current_path
            )
            self._last_displayed_frame = frame_array.copy()
            self._last_displayed_decoded_frame = decoded_frame
            self._last_displayed_frame_index = 0
            self.frame_ready.emit()
            return True
        except Exception as e:
            logger.error(f"首帧渲染失败: {e}")
            return False

    def _get_frame_path(self, frame: Any) -> str:
        """
        获取帧路径类型。

        P0-3 新增：用于日志和监控。
        返回值与 RenderFramePath 枚举对应。

        Args:
            frame: DecodedFrame

        Returns:
            frame_path 字符串（与 RenderFramePath 枚举值对应）
        """
        if frame is None:
            return "unknown"

        # 检查是否有 planes（planar 格式）
        if hasattr(frame, 'planes') and frame.planes:
            pixel_format = getattr(frame, 'pixel_format', 'unknown')
            dtype = getattr(frame, 'dtype', 'unknown')
            bit_depth = getattr(frame, 'bit_depth', 8)

            if 'nv12' in pixel_format.lower() or 'nv21' in pixel_format.lower():
                return "nv12_r8_rg8_upload"
            elif 'p010' in pixel_format.lower():
                return "p010_r16ui_upload"
            elif dtype == np.uint16 or str(dtype).startswith('uint16') or bit_depth > 8:
                return "planar_yuv_r16ui_upload"
            elif dtype == np.float16 or str(dtype).startswith('float16'):
                return "planar_yuv_r16ui_upload"
            else:
                return "planar_yuv_r8_upload"

        # 检查是否有 data（packed 格式）
        if hasattr(frame, 'data') and frame.data is not None:
            return "packed_rgb_cpu_upload"

        return "unknown"

    def _show_first_frame_placeholder(self) -> None:
        """
        显示首帧占位符。

        P0-3 新增：当首帧暂未就绪时，显示占位符并保持 v3 状态。

        Note:
            显示占位符的方式：
            1. 如果有之前的帧（如加载其他视频后），显示灰色占位符
            2. 或者什么都不显示，等待 decode worker 产生帧后通过 tick 显示

            这不会触发 fallback v2，v3 保持 loaded 状态。
        """
        # 当前实现：不主动显示占位符，等待 decode worker 产生帧
        # 后续可以通过 UI 显示 "首帧加载中..." 状态
        logger.info("首帧暂未就绪，等待后台解码...")

    def _select_backend_for_path(self, path: str) -> str:
        """
        根据文件路径选择合适的媒体后端。

        ProRes/DNxHR/MXF/MOV 专业格式默认使用 FFmpeg backend。

        Args:
            path: 视频文件路径

        Returns:
            后端名称 ('ffmpeg' 或 'auto')
        """
        import os

        # 专业格式的扩展名和编解码器
        professional_extensions = ('.mxf', '.mov', '.prores', '.dnxhd')
        professional_codecs = ('prores', 'dnxhr', 'dnxhd', 'ffv1', 'rawvideo')

        # 检查扩展名
        ext = os.path.splitext(path)[1].lower()
        if ext in professional_extensions:
            logger.info(f"专业格式文件 ({ext})，强制使用 FFmpeg backend: {path}")
            return 'ffmpeg'

        # 尝试探测编解码器名称
        try:
            from src.shared.media.factory import MediaSourceFactory
            metadata = MediaSourceFactory.probe_video_metadata(path)
            if metadata and metadata.codec_name:
                codec_lower = metadata.codec_name.lower()
                if any(c in codec_lower for c in professional_codecs):
                    logger.info(
                        f"专业编解码器 ({metadata.codec_name})，"
                        f"强制使用 FFmpeg backend: {path}"
                    )
                    return 'ffmpeg'
        except (OSError, ValueError, KeyError, RuntimeError) as e:
            # AUD-39605: 收窄异常面并留痕——探测失败静默吞掉会让专业
            # 格式（ProRes/DNxHR）走错 backend（硬件解码路径不支持）
            logger.debug(f"编解码器探测失败，回退 auto backend: {path} ({e})")

        # 默认自动选择
        return 'auto'

    def _process_decoded_frame(self, frame: Any) -> Optional[np.ndarray]:
        """
        处理 DecodedFrame 数据，转换为引擎可用的 RGB 数组。

        根据 pixel_format/dtype/metadata 选择正确的处理路径：
        - 打包格式 (RGB/BGR): 直接使用或转换
        - 平面格式 (YUV): 使用 ColorConverter 转为 RGB

        Args:
            frame: DecodedFrame 对象

        Returns:
            RGB/BGR uint8 数组，或 None 处理失败
        """
        try:
            # 打包格式：有 data 属性
            if hasattr(frame, 'data') and frame.data is not None:
                data = frame.data

                # 根据 pixel_format 处理
                pixel_format = getattr(frame, 'pixel_format', 'bgra')

                if pixel_format in ('bgr24', 'bgra', 'bgra64'):
                    # BGR 格式，直接使用
                    if data.dtype == np.uint8:
                        return data[..., :3] if data.shape[-1] == 4 else data
                    elif data.dtype == np.uint16:
                        # AUD-39600 高位深（bgra64 等 16-bit 打包，full range
                        # 0-65535）转 uint8。旧实现除以 (1 << bit_depth) 后
                        # 值域落在 [0,1)，astype 截断后全为 0（16-bit BGR 帧
                        # 显示为全黑）。与相邻 RGB 分支及 _process_frame 的
                        # 转换惯例保持一致：/65535*255。
                        return (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    else:
                        return data[..., :3] if data.shape[-1] == 4 else data

                elif pixel_format in ('rgb24', 'rgba', 'rgba64', 'rgba64le',
                                      'rgb48le', 'rgb48be', 'rgb48', 'bgr48le', 'bgr48be',
                                      'gbrp', 'gbrp10le', 'gbrp12le', 'gbrp16le',
                                      'gbrap', 'gbrap10le', 'gbrap12le', 'gbrap16le'):
                    # RGB/GBR 格式，转为 BGR uint8（引擎期望 BGR）
                    if data.dtype == np.uint16:
                        # 16-bit packed RGB: full range 0-65535
                        rgb8 = (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    elif data.dtype == np.uint8:
                        rgb8 = data[..., :3]
                    else:
                        rgb8 = np.clip(data[..., :3] * 255.0, 0, 255).astype(np.uint8)

                    # GBR formats already in RGB order from PyAV reformat
                    # RGB → BGR flip for engine
                    return np.flip(rgb8, axis=-1)

                else:
                    # 其他打包格式，假设为 BGR（保持向后兼容）
                    if data.dtype == np.uint16:
                        return (data[..., :3] / 65535.0 * 255.0).astype(np.uint8)
                    return data[..., :3] if data.ndim == 3 and data.shape[-1] >= 3 else data

            # 平面格式：有 planes 属性
            elif hasattr(frame, 'planes') and frame.planes:
                return self._convert_planar_to_rgb(frame)

            else:
                # 无有效数据
                logger.warning(f"DecodedFrame 无有效数据: {frame}")
                return None

        except Exception as e:
            logger.error(f"处理 DecodedFrame 失败: {e}")
            traceback.print_exc()
            return None

    def unload_video(self) -> None:
        """
        卸载当前视频（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 进行卸载。

        AUD-39604: coordinator.unload() / source_manager.close() 各自
        异常隔离——解码器关闭失败很常见，一旦中途抛出会跳过后续纹理
        清空与标志复位，触发"按播放后闪一下上一片段画面"的缺陷。
        AUD-1100: 卸载即离开播放态，恢复效果预览完整质量。
        """
        # AUD-1100: 播放降级状态随卸载退出（与 stop_playback 对称）
        get_effect_preview_quality_manager().set_playback_state(False)

        # P1.B: 停止预读 worker
        self._stop_proxy_prefetch_worker()

        # P5.A: 使用 PlaybackCoordinator 卸载
        # AUD-39604: 单独异常保护，保证后续清理必然执行
        coordinator = self._coordinator
        self._coordinator = None
        if coordinator is not None:
            try:
                coordinator.unload()
            except Exception as e:
                logger.warning(f"coordinator.unload() failed: {e}", exc_info=True)

        # 关闭双路径组件（向后兼容）
        # AUD-39604: 同样隔离 close 异常，引用先转移再清理
        source_manager = self._source_manager
        self._source_manager = None
        self._playback_clock = None
        self._frame_scheduler = None
        if source_manager is not None:
            try:
                source_manager.close()
            except Exception as e:
                logger.warning(
                    f"source_manager.close() failed: {e}", exc_info=True)

        # P1.B: 清除 last displayed frame
        self._last_displayed_frame = None
        # P0-1: 清除轻量状态
        self._last_displayed_decoded_frame = None
        self._last_displayed_frame_index = None

        # 清空渲染器持有的视频纹理状态。RENDER_FRAME（grading/viewport
        # refresh/color_stack hydration）命令没有 generation 检查，会在命令
        # 执行时刻读取 current_video_texture/current_video_planes 作为渲染源。
        # 若不清空，片段切换期间残留在队列里的 RENDER_FRAME 会用上一片段的
        # 纹理渲染一帧并发布到屏幕，表现为"按播放后闪一下上一片段的画面"。
        # 清空后，残留的 RENDER_FRAME 会回退到 pyramid_textures（新片段首帧）
        # 或因无源纹理直接 return，不再闪旧片段。
        self._clear_renderer_video_texture_state()

        # 重置跨片段残留的惰性决策标志。这些标志从不在 unload 中清理，
        # 残留的旧值会误导新片段的帧提交 / 监视器输出 / Canvas 更新路径。
        self._last_submitted_video_reason = None
        self._last_submitted_video_frame_index = -1
        self._last_frame_ready_direct_render = False
        self._static_preview_request_key = None
        self._last_displayed_static_preview_is_thumbnail = False
        # 解除 paintGL hold，防止上一个片段 seek 路径残留的 hold 导致
        # 切换期间 paintGL 不清屏、保留旧 framebuffer。
        self._set_static_preview_frame_hold(False)

        # 调用父类方法
        super().unload_video()

    def _clear_renderer_video_texture_state(self) -> None:
        """清空主渲染器的视频输入引用（UI 线程调用安全，AUD-39606）。

        背景：unload 需要立刻清掉 renderer.current_video_texture /
        current_video_planes，防止残留 RENDER_FRAME 闪现旧片段画面。

        AUD-39606: 这两个属性可能持有 ModernGL 纹理——若置空发生在
        UI 线程且恰是最后引用，glDelete 会在 UI 线程执行（违反
        「GL 仅渲染线程」铁律）。处理方式：
        1. 引用清空本身安全：current_video_texture 恒来自渲染线程的
           _video_texture_pool（池持有强引用），current_video_planes
           恒来自 _yuv_gpu_uploader 池（同样被持有）或非占有的
           ExternalGLTexture 描述符——UI 侧置空不会触发 GL 释放；
        2. 为让该不变量"由构造保证"而非"恰好成立"，同时向渲染线程
           提交 RELEASE_VIDEO_TEXTURE_POOL：旧片段的池内纹理在渲染
           线程统一释放，绝不依赖 UI 线程的引用计数归零。
        """
        try:
            engine = getattr(self, "engine", None)
        except RuntimeError:
            engine = None
        renderer = getattr(engine, "renderer", None) if engine else None
        if renderer is None:
            return
        try:
            renderer.current_video_texture = None
            renderer.current_video_planes = None
            from src.shared.video.render_commands import RenderCommand
            submit = getattr(renderer, 'submit', None)
            if callable(submit):
                submit(RenderCommand.RELEASE_VIDEO_TEXTURE_POOL, None)
        except (AttributeError, RuntimeError) as e:
            # 渲染器已被销毁/线程已退出时静默跳过（Qt 包装 C++ 对象
            # 删除后属性访问抛 RuntimeError）
            logger.debug(f"clear renderer video texture state skipped: {e}")

    def start_playback(self) -> None:
        """
        开始播放（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 进行播放控制。

        FIX: 增加健壮性检查，确保 coordinator 和 session 状态正确。

        P2-1: 注册播放状态到全局追踪器，用于检测同步解码违规。
        V-P0-4: 只有实际成功启动后才注册 True，避免失败路径留下误报状态。
        """
        # V-P0-4: 不在入口处过早注册，等实际成功后再注册

        if self._use_dual_path and self._coordinator:
            # FIX: 检查 coordinator 是否真正可用
            if not self._coordinator.is_loaded:
                # V-P0-3: 统一 fallback 处理
                fallback_result = fallback_to_v2_or_fail(
                    reason=VideoPipelineFallbackReason.SESSION_INVALID,
                    path=self.current_path,
                    notes="Coordinator not loaded when starting playback",
                    decision=self._pipeline_decision,
                )
                # v2 GStreamer 链路已移除：不再回退，直接报错
                logger.error(fallback_result.message)
                return

            # A retained seek/static preview may still be waiting in the GL
            # command queue. Starting playback defines a new presentation
            # epoch so an old clip cannot repaint after a proxy frame arrives.
            self._begin_static_preview_generation()
            # Withdraw the previous clip's front texture so the Canvas clears
            # its background while the first playback frame decodes.  Without
            # this, a stale frame submitted by the first tick (the coordinator
            # can still hold the departing clip's tail frame mid-seek) flashes
            # through the still-valid front texture for one frame.
            self._mark_renderer_video_texture_pending()
            self._last_submitted_video_reason = "playback_pending"

            # P5.A: 使用 PlaybackCoordinator
            if self._in_out_range_active():
                effective_out = self._effective_out_frame()
                # V-P0-09: 仅在确实存在已知出点时做越界回退；未知时长不在此约束内。
                if (
                    effective_out is not None
                    and (self.current_frame < self.in_frame
                         or self.current_frame > effective_out)
                ):
                    self._coordinator.seek_to_frame(self.in_frame, blocking=False)
                    self.current_frame = self.in_frame
                    self.frame_changed.emit(self.in_frame)
            start_result = self._coordinator.start_playback()
            if not start_result:
                # V-P0-3: 统一 fallback 处理
                fallback_result = fallback_to_v2_or_fail(
                    reason=VideoPipelineFallbackReason.PLAYBACK_START_FAILED,
                    path=self.current_path,
                    notes="PlaybackCoordinator.start_playback() returned False",
                    decision=self._pipeline_decision,
                )
                # v2 GStreamer 链路已移除：不再回退，直接报错
                logger.error(fallback_result.message)
                return

            self.is_playing = True
            self._set_renderer_playback_active(True)
            self._set_static_preview_frame_hold(False)
            # Playback moves the presentation away from the delivered static
            # preview; the clip-switch fast-complete path must not reuse it.
            self._last_static_preview_delivered = False
            self.play_state_changed.emit(True)

            # V-P0-4: coordinator 成功启动后才注册播放状态
            _register_playback_engine(id(self), True)

            # AUD-1100: 播放降级系统接线——实际成功启动后激活重型效果
            # 的播放降级（暂停/停止/卸载路径对称恢复完整质量）。此前
            # 全库无任何 set_playback_state(True) 调用，降级系统从未激活。
            get_effect_preview_quality_manager().set_playback_state(True)

            # P0.A: 开始性能监控会话
            self._perf_monitor.start_session()

            # FIX: 预热 DecodeWorker - 请求当前帧和后续帧
            # 避免 tick() 前几秒返回 None 导致画面不动
            if self._coordinator.session is not None:
                session = self._coordinator.session
                if hasattr(session, '_request_current_proxy_frame_if_missing'):
                    session._request_current_proxy_frame_if_missing(self.current_frame)
                if hasattr(session, '_worker_manager') and session._worker_manager is not None:
                    session._worker_manager.update_clock_position(self.current_frame)

            # 定时器驱动帧消费
            fps = self._coordinator.fps or 30.0
            interval = int(1000 / fps) if fps > 0 else 33
            interval = max(8, int(interval * 0.8))
            self._play_timer.start(interval)

        elif self._use_dual_path and self._playback_clock:
            # 向后兼容：使用旧架构（无 coordinator）
            self._playback_clock.start()
            self.is_playing = True
            self._set_renderer_playback_active(True)
            self.play_state_changed.emit(True)

            # V-P0-4: playback_clock 成功启动后才注册播放状态
            _register_playback_engine(id(self), True)

            # AUD-1100: 播放降级接线（向后兼容旧架构路径，与 v3 对称）
            get_effect_preview_quality_manager().set_playback_state(True)

            # P0.A: 开始性能监控会话
            self._perf_monitor.start_session()

            # P1.B: 启动 ProxyPrefetchWorker 后台预读
            if self._source_manager and self._playback_clock:
                self._start_proxy_prefetch_worker()

            # 定时器驱动帧消费
            fps = self._playback_clock.fps if self._playback_clock else 30.0
            interval = int(1000 / fps) if fps > 0 else 33
            interval = max(8, int(interval * 0.8))
            self._play_timer.start(interval)

        else:
            # v2 GStreamer 链路已移除：无可用播放路径
            logger.error(
                "start_playback: 无可用播放路径（v2 GStreamer 链路已移除）"
            )

    def _start_proxy_prefetch_worker(self) -> None:
        """
        启动 ProxyPrefetchWorker 后台预读线程 (P1.B)。

        只在 dual path 模式下启动，使用 SourceManager.get_proxy_frame() 预读帧。
        """
        # 停止现有的 worker
        self._stop_proxy_prefetch_worker()

        if not self._source_manager or not self._playback_clock:
            logger.warning("Cannot start ProxyPrefetchWorker: missing source_manager or playback_clock")
            return

        # 获取预读队列深度配置
        max_queue = self.get_preview_queue_depth() if hasattr(self, 'get_preview_queue_depth') else 8
        lookahead = max(4, max_queue // 2)  # 预读前瞻帧数

        # 创建并启动 worker
        self._proxy_prefetch_worker = ProxyPrefetchWorker(
            source_manager=self._source_manager,
            playback_clock=self._playback_clock,
            max_queue_size=max_queue,
            prefetch_lookahead=lookahead
        )

        # 连接信号
        self._proxy_prefetch_worker.frame_ready.connect(self._on_prefetch_frame_ready)
        self._proxy_prefetch_worker.error_occurred.connect(self._on_prefetch_error)

        # 启动线程
        self._proxy_prefetch_worker.start()
        logger.info(f"ProxyPrefetchWorker started: queue={max_queue}, lookahead={lookahead}")

    def _stop_proxy_prefetch_worker(self) -> None:
        """停止 ProxyPrefetchWorker 后台预读线程 (P1.B)"""
        if self._proxy_prefetch_worker is not None:
            try:
                self._proxy_prefetch_worker.stop()
                if self._proxy_prefetch_worker.isRunning():
                    self._proxy_prefetch_worker.wait(2000)  # 等待最多 2 秒
                self._proxy_prefetch_worker = None
                logger.info("ProxyPrefetchWorker stopped")
            except Exception as e:
                logger.error(f"Error stopping ProxyPrefetchWorker: {e}")

    def _on_prefetch_frame_ready(self, frame_index: int) -> None:
        """预读帧就绪回调 (P1.B)"""
        logger.debug(f"Prefetch frame ready: {frame_index}")
        # 可用于通知 UI 或触发其他操作

    def _on_prefetch_error(self, error_msg: str) -> None:
        """预读错误回调 (P1.B)"""
        logger.error(f"Prefetch error: {error_msg}")

    def stop_playback(self) -> None:
        """
        停止播放（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 进行播放控制。

        P2-1: 从全局追踪器注销播放状态。
        """
        self._set_renderer_playback_active(False)

        # P2-1: 从全局追踪器注销播放状态（无论哪个路径）
        _register_playback_engine(id(self), False)

        if self._use_dual_path and self._coordinator:
            # P5.A: 使用 PlaybackCoordinator
            self._coordinator.stop_playback()
            self.is_playing = False

            # AUD-1100: 播放降级接线——v3 分支不经过 super().stop_playback()
            # （基类 2123 处的 set_playback_state(False) 到不了这里），
            # 必须就地恢复效果预览完整质量。
            get_effect_preview_quality_manager().set_playback_state(False)

            # 停止播放定时器，防止停止后仍有 tick 回调消耗 CPU/GPU
            if hasattr(self, '_play_timer') and self._play_timer is not None:
                self._play_timer.stop()

            # Reset the playback dedup index so a clip switch / stop+replay does
            # not leave the previous clip's last frame index behind.  Otherwise
            # the first tick after switching to a new clip sees
            # ``new_start != old_last`` and re-submits the start frame that the
            # static preview already displayed — making the first frame appear
            # to play twice.
            self._last_displayed_frame_index = None

            self.play_state_changed.emit(False)

            # P0.A: 结束性能监控会话
            self._perf_monitor.end_session()

        elif self._playback_clock:
            # 向后兼容：使用旧架构
            self._playback_clock.stop()

            # P1.B: 停止预读 worker
            self._stop_proxy_prefetch_worker()

            # P1.B: 清除 last displayed frame
            self._last_displayed_frame = None
            # P0-1: 清除轻量状态
            self._last_displayed_decoded_frame = None
            self._last_displayed_frame_index = None

            super().stop_playback()

        else:
            # 使用原有架构
            super().stop_playback()

    def _convert_planar_to_rgb(self, frame) -> Optional[np.ndarray]:
        """
        将 YUV planar 帧转换为引擎 set_image() 需要的 BGR packed 格式。

        使用 ColorConverter 进行 CPU YUV→RGB 转换。

        Args:
            frame: DecodedFrame 对象，包含 planes 属性

        Returns:
            BGR uint8 数组 (H, W, 3)，或 None 转换失败
        """
        try:
            # 提取 Y/U/V 平面
            if len(frame.planes) < 3:
                logger.warning(f"Not enough planes: {len(frame.planes)}")
                return None

            y_plane = frame.planes[0]
            u_plane = frame.planes[1]
            v_plane = frame.planes[2]

            # 获取色彩元数据
            color_matrix = ColorMatrix.BT709  # 默认值
            color_range = ColorRange.LIMITED  # 默认值
            bit_depth = 8  # 默认值

            if hasattr(frame, 'metadata') and frame.metadata:
                # 从 metadata 解析 color_matrix
                matrix_str = frame.metadata.color_matrix
                if matrix_str:
                    matrix_map = {
                        'bt709': ColorMatrix.BT709,
                        'bt2020nc': ColorMatrix.BT2020_NC,
                        'bt2020': ColorMatrix.BT2020_NC,
                        'smpte170m': ColorMatrix.BT601,
                        'bt470bg': ColorMatrix.BT601_PAL,
                    }
                    color_matrix = matrix_map.get(matrix_str.lower(), ColorMatrix.BT709)

                # 从 metadata 解析 color_range
                range_str = frame.metadata.color_range
                if range_str:
                    range_map = {
                        'limited': ColorRange.LIMITED,
                        'tv': ColorRange.LIMITED,
                        'full': ColorRange.FULL,
                        'pc': ColorRange.FULL,
                    }
                    color_range = range_map.get(range_str.lower(), ColorRange.LIMITED)

                # 获取位深度
                if hasattr(frame.metadata, 'bit_depth'):
                    bit_depth = frame.metadata.bit_depth

            # 使用 ColorConverter 进行转换
            converter = ColorConverter()
            rgb = converter.yuv_to_rgb(
                y=y_plane,
                u=u_plane,
                v=v_plane,
                matrix=color_matrix,
                range_=color_range,
                bit_depth=bit_depth
            )

            # ColorConverter 输出 RGB；seek/HQ 路径会调用 ImageEngine.set_image()，
            # 而 ImageEngine/AsyncRenderer 的图片上传路径约定输入为 BGR。
            # 如果这里直接返回 RGB，后续会再按 BGR->RGB 转换一次，红蓝通道互换。
            rgb_uint8 = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
            bgr_uint8 = np.ascontiguousarray(np.flip(rgb_uint8, axis=-1))

            logger.debug(
                f"Converted planar frame {getattr(frame, 'pixel_format', 'unknown')} "
                f"to BGR: {bgr_uint8.shape}, matrix={color_matrix.value}, range={color_range.value}"
            )

            return bgr_uint8

        except Exception as e:
            logger.error(f"Failed to convert planar frame: {e}", exc_info=True)
            return None

    def seek_to_frame(
        self,
        frame_number: int,
        *,
        force_display: bool = False,
        blocking: Optional[bool] = None,
        allow_proxy_fallback: bool = True,
        fast_scrub: bool = False,
    ) -> None:
        """
        跳转到指定帧（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 进行 seek。

        Args:
            frame_number: 目标帧号
            force_display: True 时把 seek 视为用户主动切换画面，立即同步获取并
                显示目标 master 帧。用于 Color Timeline Clips 等非播放热路径。
            blocking: V-P0-01 显式控制是否在 UI 线程同步取 master 帧。
                - None（默认）：保持旧行为，按播放态推断（force_display or
                  未在播放）。播放态快照在 stop_playback 之前采样，避免
                  “播放中发起的 seek”被误判为暂停 seek 而阻塞 UI 线程。
                - False：强制非阻塞——只发异步 seek，不调用 get_current_frame_data
                  同步等 master 解码。用于 scrub 等高频交互热路径，避免 ~300ms 卡顿。
                  目标帧由后续静态预览轮询 / renderer 异步交付。
                - True：强制阻塞同步取帧（仅离线/非 UI 线程使用）。
            allow_proxy_fallback: 非阻塞 seek 在目标 master 帧尚未就绪时，是否
                允许用当前 proxy 帧提供即时反馈。Color 调色页拖拽预览传 False，
                避免把低分辨率 proxy 放大到预览窗口；其他实时预览保留默认行为。
            fast_scrub: 高频拖拽模式（需配合 blocking=False）：seek 不翻
                generation，且允许显示"最新完成的 master 帧"作为过渡反馈。
                逐代数丢弃会让所有慢于请求节奏的解码在入队时被丢——
                快拖时画面完全冻结（真实 App 实测 249 次 seek 仅 1 帧显示）。
        """
        if self._use_dual_path and self._coordinator:
            # P5.A: 使用 PlaybackCoordinator
            # 在停止播放前先快照播放态：seek_to_frame 自身会 stop_playback，
            # 之后再看 _is_any_engine_playing() 必然为 False，会把“播放中
            # 发起的 seek”（如同源片段跨边界推进）误判为暂停 seek，在 UI 线程
            # 同步解码 master 帧，造成可见卡顿。
            was_playing = bool(self.is_playing) or _is_any_engine_playing()
            # 停止播放
            self.stop_playback()
            # V-P0-01: 显式 blocking 优先；None 时维持原“按播放态推断”逻辑。
            if blocking is False:
                blocking_seek = False
            elif blocking is True:
                blocking_seek = True
            else:
                blocking_seek = force_display or not was_playing
            if blocking_seek:
                self._set_static_preview_frame_hold(True)
                # A paused/explicit seek supersedes every retained static
                # preview that may still be queued on the renderer thread.
                self._begin_static_preview_generation()
                self._mark_renderer_video_texture_pending()

            # 使用 coordinator seek（scrub 高频路径不翻 generation，否则慢于
            # 请求节奏的 master 解码在入队时全部被丢为过期帧——快拖冻结）。
            self._coordinator.seek_to_frame(
                frame_number,
                blocking=blocking_seek,
                churn_generation=not (fast_scrub and not blocking_seek),
            )

            frame = None
            if blocking_seek:
                # 获取高质量帧并显示。页面切换/播放态 seek 不走这里，
                # 避免 P2-1 禁止的 UI 线程同步解码。
                frame = self._coordinator.get_current_frame_data(is_master=True)
                if (
                    frame is not None
                    and getattr(frame, "frame_index", frame_number) != frame_number
                ):
                    logger.warning(
                        "Rejecting mismatched static seek frame: requested=%s actual=%s",
                        frame_number,
                        getattr(frame, "frame_index", None),
                    )
                    frame = None
                if frame is None and force_display:
                    candidate = self._coordinator.get_frame_at(frame_number, is_master=True)
                    if getattr(candidate, "frame_index", None) == frame_number:
                        frame = candidate
            else:
                # V-P0-01 非阻塞 seek（scrub 等高频路径）：绝不同步等 master 解码，
                # 但必须给用户即时视觉反馈——否则拖动过程中画面静止，只有松手才变。
                # 1) 异步请求目标 master 帧（松手后/refine 时可用）。
                request_master = getattr(self._coordinator, 'request_master_frame', None)
                if callable(request_master):
                    request_master(frame_number)
                # 2) 非阻塞取"已就绪"的帧：优先精确 master（take_ready）。
                take_master = getattr(self._coordinator, 'take_ready_master_frame', None)
                if callable(take_master):
                    candidate = take_master(frame_number)
                    if getattr(candidate, "frame_index", None) == frame_number:
                        frame = candidate
                # 3) fast_scrub：取最新完成的 master 帧作为过渡显示。高频拖拽
                #    中精确目标总在解码完成前移走，take_ready 永远 miss；没有
                #    proxy 轨道时 proxy 兜底也是 None——两者皆空时画面完全
                #    冻结。以解码速率步进显示最新完成帧（全分辨率）。
                if frame is None and fast_scrub:
                    take_latest = getattr(
                        self._coordinator, 'take_latest_ready_master_frame', None
                    )
                    if callable(take_latest):
                        frame = take_latest(frame_number)
                # 4) 其余路径：当前 proxy/master（get_current_frame_data，不等待）。
                if frame is None and allow_proxy_fallback:
                    current = self._coordinator.get_current_frame_data(is_master=False)
                    if current is not None:
                        frame = current

            if frame is not None:
                self._submit_decoded_frame_playback(
                    frame,
                    frame_number,
                    quality=(
                        "master"
                        if blocking_seek or not allow_proxy_fallback
                        else "proxy"
                    ),
                    reason="seek" if blocking_seek else "playback",
                    drop_if_stale=True,
                )
                if blocking_seek:
                    self._set_static_preview_frame_hold(False)
                    # An exact blocking seek presents the requested frame, so
                    # the clip-switch fast-complete path may treat it as the
                    # delivered static preview.
                    self._last_static_preview_delivered = True
            elif blocking_seek:
                logger.warning(
                    f"Seek to frame {frame_number} did not produce an exact master frame"
                )
                self.current_frame = frame_number
                self.frame_changed.emit(frame_number)
            else:
                self.current_frame = frame_number
                self.frame_changed.emit(frame_number)

        elif self._use_dual_path and self._source_manager:
            # 向后兼容：使用旧架构
            was_playing = self.is_playing
            self.stop_playback()

            frame = self._source_manager.get_master_frame(frame_number)

            if frame is not None:
                frame_array = self._process_decoded_frame(frame)

                if frame_array is not None:
                    try:
                        self.engine.set_image(
                            frame_array, frame_array, frame_array,
                            is_new_file=False,
                            is_video=True,
                            video_path=self.current_path
                        )
                        self._last_displayed_frame_index = frame_number
                    except Exception as e:
                        logger.error(f"HQ render failed: {e}")

            if self._playback_clock:
                self._playback_clock.seek_to_frame(frame_number)

            if self._proxy_prefetch_worker:
                self._proxy_prefetch_worker.request_seek(frame_number + 1)

            self.current_frame = frame_number
            self.frame_changed.emit(frame_number)
            self.frame_ready.emit()

        else:
            # 使用原有架构
            super().seek_to_frame(frame_number)

    def present_ready_scrub_frame(self, frame_number: int) -> bool:
        """Present a just-decoded target frame during an interactive scrub.

        ``seek_to_frame(..., blocking=False)`` deliberately checks the ready
        queue only once, so a frame that finishes decoding a few milliseconds
        later otherwise remains invisible until the user releases the scrub
        bar.  The UI calls this method from a lightweight timer while it is
        still dragging.  It only dequeues an exact, already-ready frame and
        never performs synchronous decode work.
        """
        if not (self._use_dual_path and self._coordinator):
            return False

        frame_number = int(frame_number)
        if getattr(self, "_last_displayed_frame_index", None) == frame_number:
            return True

        take_master = getattr(self._coordinator, "take_ready_master_frame", None)
        if not callable(take_master):
            return False
        try:
            frame = take_master(frame_number)
        except (AttributeError, RuntimeError, TypeError) as exc:
            logger.debug("Unable to consume ready scrub frame %s: %s", frame_number, exc)
            return False

        if getattr(frame, "frame_index", None) != frame_number:
            return False

        # Keep this in the coalescing playback lane: scrubbing can produce many
        # frames per second, and only the latest visual preview matters.  The
        # release path still submits an exact retained seek frame.
        self._force_source_resolution_submit = True
        try:
            self._submit_decoded_frame_playback(
                frame,
                frame_number,
                quality="proxy",
                reason="playback",
                drop_if_stale=True,
            )
        finally:
            self._force_source_resolution_submit = False
        return True

    def present_latest_ready_scrub_frame(self, target_frame: int) -> bool:
        """Present the newest completed master frame while scrubbing.

        精确帧等待（present_ready_scrub_frame）在拖拽事件快于解码时永远
        miss：目标在解码完成前就被下一个事件作废，显示被锁步在
        "解码 + 事件间隔 + 轮询粒度" 周期（实测 4K ProRes ~29ups，而解码
        本身 ~105fps）。这里改取"最新已完成"的 master 帧直接上屏（帧距
        上限沿用 96 防止大跳变闪旧帧），画面以解码完成率步进。松手后的
        精确落点仍由 settle 路径的 exact 帧负责。
        """
        if not (self._use_dual_path and self._coordinator):
            return False

        take_latest = getattr(self._coordinator, "take_latest_ready_master_frame", None)
        if not callable(take_latest):
            return False

        try:
            frame = take_latest(int(target_frame))
        except (AttributeError, RuntimeError, TypeError) as exc:
            logger.debug("Unable to consume latest ready scrub frame: %s", exc)
            return False

        frame_index = getattr(frame, "frame_index", None)
        if frame_index is None:
            return False
        if getattr(self, "_last_displayed_frame_index", None) == int(frame_index):
            return True

        self._force_source_resolution_submit = True
        try:
            self._submit_decoded_frame_playback(
                frame,
                int(frame_index),
                quality="proxy",
                reason="playback",
                drop_if_stale=True,
            )
        finally:
            self._force_source_resolution_submit = False
        return True

    def request_static_preview_frame(self, frame_number: int) -> bool:
        """Submit a decoded master frame for the paused Color viewer.

        Color grading is normally performed while paused, so this path never
        promotes a compressed thumbnail or proxy frame to the default grading
        image.  It requests the target master frame asynchronously and returns
        False until that exact frame is ready for submission.
        """
        if self._use_dual_path and self._coordinator:
            frame_number = int(frame_number)
            request_key = (str(getattr(self, "current_path", "") or ""), frame_number)
            if request_key != self.__dict__.get("_static_preview_request_key"):
                # The page polls this method while a master frame is decoding.
                # A seek/generation bump on every poll invalidates the exact
                # frame already being decoded, so only issue it for a new
                # source-frame request.
                self.stop_playback()
                self._set_static_preview_frame_hold(True)
                self._begin_static_preview_generation()
                self._mark_renderer_video_texture_pending()
                try:
                    self._coordinator.seek_to_frame(frame_number, blocking=False)
                except TypeError:
                    self._coordinator.seek_to_frame(frame_number)

                request_master = getattr(self._coordinator, 'request_master_frame', None)
                if callable(request_master):
                    request_master(frame_number)
                self._static_preview_request_key = request_key
                # A new request supersedes any previously delivered static
                # preview; the fast-complete path must not treat the old
                # frame as current.
                self._last_static_preview_delivered = False

            frame = None
            try:
                take_master = getattr(self._coordinator, 'take_ready_master_frame', None)
                candidate = take_master(frame_number) if callable(take_master) else None
                if getattr(candidate, "frame_index", None) == frame_number:
                    frame = candidate
            except (AttributeError, TypeError, ValueError, RuntimeError) as e:
                # AUD-39605: 收窄异常面——就绪帧获取失败按"未就绪"处理，
                # 其余领域错误正常传播
                logger.debug(
                    "request_static_preview_frame: take_ready_master_frame "
                    "failed for frame %s: %s", frame_number, e)
                frame = None

            if frame is None:
                logger.debug(
                    "[COLOR_CLIP_PREVIEW] pending frame=%s path=%s",
                    frame_number,
                    self.current_path,
                )
                self.current_frame = frame_number
                self.frame_changed.emit(frame_number)
                return False

            logger.debug(
                "[COLOR_CLIP_PREVIEW] submit frame=%s actual=%s path=%s",
                frame_number,
                getattr(frame, "frame_index", None),
                self.current_path,
            )
            self._submit_decoded_frame_playback(
                frame,
                frame_number,
                quality="master",
                reason="seek",
                drop_if_stale=True,
            )
            self._set_static_preview_frame_hold(False)
            self._last_displayed_static_preview_is_thumbnail = False
            self._last_static_preview_delivered = True
            return True

        self.seek_to_frame(frame_number)
        return True

    def render_static_preview_frame(self, frame_number: int) -> bool:
        """Synchronously render a paused preview frame through ImageEngine.

        This is the most deterministic Color-page static preview path: it
        updates ImageEngine's current video source frame and then lets the
        normal Color render pipeline produce the viewport texture.  It is used
        for Timeline Clip card selection where correctness matters more than the
        playback-optimized command path.
        """
        frame_number = int(frame_number)
        try:
            self.stop_playback()
        except (AttributeError, RuntimeError) as e:
            # AUD-39605: 收窄为组件失效类异常；停止播放的领域错误不吞
            logger.warning(
                "render_static_preview_frame: stop_playback failed: %s", e,
                exc_info=True)
        self._begin_static_preview_generation()
        self._mark_renderer_video_texture_pending()
        logger.debug(
            "[COLOR_CLIP_DEBUG] video.hard_request requested=%s current_path=%s "
            "use_dual=%s has_coord=%s has_source_mgr=%s has_decoder=%s",
            frame_number,
            self.current_path,
            self._use_dual_path,
            self._coordinator is not None,
            self._source_manager is not None,
        )

        frame = None
        frame_source = "none"
        if self._use_dual_path and self._coordinator:
            try:
                self._coordinator.seek_to_frame(frame_number, blocking=False)
            except TypeError:
                self._coordinator.seek_to_frame(frame_number)
            except (AttributeError, RuntimeError) as e:
                # AUD-39605: 收窄异常面（旧签名 TypeError 兼容保留）
                logger.warning(
                    "render_static_preview_frame: seek failed: %s", e,
                    exc_info=True)

            try:
                candidate = self._coordinator.get_current_frame_data(is_master=False)
            except (AttributeError, TypeError, ValueError, RuntimeError) as e:
                logger.debug(
                    "render_static_preview_frame: get_current_frame_data "
                    "failed: %s", e)
                candidate = None
            if (
                candidate is not None
                and getattr(candidate, "frame_index", frame_number) == frame_number
            ):
                frame = candidate
                frame_source = "coordinator_ready_proxy"

        if frame is None and self._source_manager is not None:
            for getter_name in ("get_proxy_frame", "get_master_frame"):
                getter = getattr(self._source_manager, getter_name, None)
                if not callable(getter):
                    continue
                try:
                    candidate = getter(frame_number)
                except Exception:
                    candidate = None
                if candidate is not None:
                    frame = candidate
                    frame_source = f"source_manager.{getter_name}"
                    break

        if frame is None:
            logger.debug(
                "[COLOR_CLIP_PREVIEW] hard-path no frame frame=%s path=%s",
                frame_number,
                self.current_path,
            )
            return False
        logger.debug(
            "[COLOR_CLIP_DEBUG] video.hard_candidate requested=%s actual=%s "
            "source=%s type=%s",
            frame_number,
            getattr(frame, "frame_index", frame_number if isinstance(frame, np.ndarray) else None),
            frame_source,
            type(frame).__name__,
        )

        if isinstance(frame, np.ndarray):
            frame_array = frame
            decoded_frame = None
        else:
            frame_array = self._process_decoded_frame(frame)
            decoded_frame = frame

        if frame_array is None:
            logger.debug(
                "[COLOR_CLIP_PREVIEW] hard-path conversion failed frame=%s path=%s",
                frame_number,
                self.current_path,
            )
            return False

        try:
            self.engine.set_image(
                frame_array,
                frame_array,
                frame_array,
                is_new_file=False,
                is_video=True,
                video_path=self.current_path,
            )
        except Exception as exc:
            logger.debug(
                "[COLOR_CLIP_PREVIEW] hard-path render failed frame=%s path=%s: %s",
                frame_number,
                self.current_path,
                exc,
            )
            return False

        renderer = getattr(self.engine, "renderer", None)
        if renderer is not None:
            setattr(renderer, "debug_last_render_command", "IMAGEENGINE_SET_IMAGE_STATIC_PREVIEW")
            setattr(renderer, "debug_last_rendered_video_frame_index", frame_number)
            setattr(renderer, "debug_last_render_reason", "color_clip_static_preview")

        self.current_frame = frame_number
        self._last_displayed_frame = frame_array.copy()
        self._last_displayed_decoded_frame = decoded_frame
        self._last_displayed_frame_index = frame_number
        self.frame_changed.emit(frame_number)
        self._last_frame_ready_direct_render = True
        self._last_static_preview_delivered = True
        self.frame_ready.emit()
        logger.debug(
            "[COLOR_CLIP_PREVIEW] hard-path rendered frame=%s path=%s",
            frame_number,
            self.current_path,
        )
        logger.debug(
            "[COLOR_CLIP_DEBUG] video.hard_set_image requested=%s frame_shape=%s "
            "vc_current_frame=%s last_displayed=%s",
            frame_number,
            getattr(frame_array, "shape", None),
            self.current_frame,
            self._last_displayed_frame_index,
        )
        return True

    def submit_static_preview_array(self, frame: np.ndarray, frame_number: int) -> bool:
        """Submit an already-decoded BGR/RGB preview frame as a retained seek frame.

        Used by UI paths that already have a clip thumbnail/preview image.  It
        gives the viewer an immediate visual response while the normal decoder
        catches up with the exact playback frame.

        The frame is uploaded at its native size and aspect ratio (no
        letterbox/resize to the widget).  The GPU's scene_size + fit_to_window
        path keeps the display proportion correct, matching the playback/seek
        path.  Earlier code letterboxed the frame to ``engine.viewport_size``
        here, which double-degraded a ≤720p thumbnail (resized again) and —
        because the letterboxed aspect rarely matched ``scene_size`` — caused
        the picture to look stretched and blurry on clip switch.
        """
        if frame is None or not isinstance(frame, np.ndarray):
            return False
        if getattr(self.engine, "renderer", None) is None:
            return False
        try:
            frame_number = int(frame_number)
            self._begin_static_preview_generation()
            preview_frame = frame if frame.flags['C_CONTIGUOUS'] else np.ascontiguousarray(frame)
            preview_h, preview_w = preview_frame.shape[:2]
            self._submit_frame_playback(
                preview_frame,
                frame_number,
                quality="proxy",
                reason="seek",
                drop_if_stale=True,
                render_params=self._get_static_preview_render_params(
                    preview_w,
                    preview_h,
                ),
            )
            self.current_frame = frame_number
            self._last_displayed_frame = preview_frame
            self._last_displayed_frame_index = frame_number
            # This canvas-aligned image is only an on-screen placeholder.  It
            # must never be treated as a full-frame source by external output.
            self._last_displayed_static_preview_is_thumbnail = True
            self._last_static_preview_delivered = True
            return True
        except (AttributeError, TypeError, ValueError, RuntimeError) as exc:
            # AUD-39605: 收窄异常面并升级为 warning——缩略图提交失败仅
            # debug 记录会让调色页黑屏类问题无从定位
            logger.warning(
                "submit_static_preview_array failed: %s", exc, exc_info=True)
            return False

    def _on_play_tick(self) -> None:
        """
        播放定时器回调（P5.A 改造版）。

        P5.A: 双路径模式下使用 PlaybackCoordinator 获取帧。

        P1.B 核心要求：
        1. UI tick 不直接解码，移除同步阻塞
        2. 只做 clock.tick() + 非阻塞取帧
        3. 没有 ready frame 就 hold last frame 或 drop
        4. timeout 必须是 0 或极短，不得阻塞 UI

        验收指标：
        - UI tick 平均 < 2ms
        - worker 解码慢时 UI 仍可拖动、暂停、切面板
        - get_proxy_frame() 不在 UI tick 热路径内直接执行
        """
        # 防御性保护：如果已停止播放，立即返回，避免残留 tick 执行无效工作
        if not self.is_playing:
            return

        # P0.A: 记录 tick 开始时间
        tick_start = time.perf_counter()

        if self._use_dual_path and self._coordinator:
            # P5.A: 使用 PlaybackCoordinator
            # V-P0-14: 默认每 tick 都推进时钟并提交；仅渲染拥塞时抑制 submit。
            skip_render_submit = False
            # S5.B: 先检查渲染队列深度，超过阈值时跳过提交（backpressure）
            # P1-1: 使用 _safe_stat_int 防止 MagicMock TypeError
            renderer = self.engine.renderer
            if renderer:
                renderer_stats = renderer.get_renderer_stats()
                renderer_queue_depth = _safe_stat_int(renderer_stats, 'queue_depth', 0)
                dropped_preview = _safe_stat_int(renderer_stats, 'dropped_preview_frames', 0)
                coalesced_commands = _safe_stat_int(renderer_stats, 'coalesced_video_commands', 0)

                # S5.B: 追踪渲染队列深度
                self._perf_monitor.track_renderer_queue_depth(renderer_queue_depth)
                if dropped_preview > 0:
                    self._perf_monitor.track_dropped_preview_frame(dropped_preview)
                if coalesced_commands > 0:
                    self._perf_monitor.track_coalesced_video_commands(coalesced_commands)

                # VIDEO_PLAYBACK is a latest-frame merge lane in AsyncRenderer,
                # but the lane shares its worker with critical color commands.
                # Under a long grading refresh, submitting on every UI tick
                # starves that worker and can freeze both Color and Edit.
                #
                # V-P0-14: 即便渲染队列拥塞，仍必须推进 clock / playhead / decode，
                # 否则恢复后会去追旧帧、变成 5Hz 卡顿。这里只在拥塞时"合并/抑制
                # render submit"——每 ~200ms 探测一次最新帧并提交，其余 tick 仍
                # 调用 coordinator.tick() 推进时钟与解码，只跳过提交。
                skip_render_submit = False
                if renderer_queue_depth >= self._video_preview_max_queue:
                    now = time.monotonic()
                    last_probe = float(
                        getattr(self, '_renderer_backpressure_last_probe_at', 0.0)
                    )
                    if now - last_probe < 0.2:
                        # 拥塞中：本 tick 不提交，但仍要推进时钟/解码（见下方）。
                        skip_render_submit = True
                    else:
                        self._renderer_backpressure_last_probe_at = now
                        logger.debug(
                            "v3 playback queue busy (depth=%s); probing latest "
                            "frame through the coalescing lane",
                            renderer_queue_depth,
                        )
                else:
                    self._renderer_backpressure_last_probe_at = 0.0

            # S2.A: coordinator.tick() 返回 DecodedFrame，直接提交
            frame = self._coordinator.tick()

            if frame is not None:
                frame_num = getattr(frame, 'frame_index', self._coordinator.current_frame)
                effective_out = self._effective_out_frame()
                # Drop a frame left over in the decode pipeline from BEFORE the
                # current clip's in-point.  After a clip switch + play, the
                # coordinator can still return the previous clip's tail frame
                # (decoded while the seek to the new in-point was in flight).
                # Because _submit stamps the current generation id at submit
                # time, the renderer's stale-frame guard cannot reject it, so a
                # 1-frame flash of the previous clip appeared at playback start.
                # An in/out-range frame is always >= in_frame; anything below it
                # here belongs to the departing clip and must not be rendered.
                if (
                    self._in_out_range_active()
                    and frame_num < self.in_frame
                ):
                    logger.debug(
                        "v3 tick: dropping stale pre-seek frame %s "
                        "(in_frame=%s)",
                        frame_num, self.in_frame,
                    )
                    return
                # A stale frame decoded for the DEPARTING clip can also sit
                # ABOVE the new out-point (e.g. same-source clips whose source
                # ranges are not ordered: the wrap seek's target frame arrives
                # after the context already rebinds to the next clip).  The
                # range check below would treat it as a genuine range crossing
                # and wrap back to in_frame, flashing one old-clip frame.
                # Wrap decisions must come from the playback clock, not from a
                # leftover decoded frame: if the clock has not crossed the
                # out-point yet, any out-of-range frame is pipeline residue.
                if (
                    self._in_out_range_active()
                    and effective_out is not None
                    and frame_num > effective_out
                ):
                    session = self._coordinator.session
                    clock = getattr(session, "playback_clock", None) if session else None
                    clock_frame = getattr(clock, "current_frame", None) if clock else None
                    if (
                        isinstance(clock_frame, int)
                        and clock_frame <= effective_out
                    ):
                        logger.debug(
                            "v3 tick: dropping stale post-out frame %s "
                            "(clock=%s, out=%s)",
                            frame_num, clock_frame, effective_out,
                        )
                        return
                # V-P0-09: 未知时长时 effective_out 为 None，表示没有已知终点，
                # 不能据此提前结束播放；结束只能由明确的 EOS 驱动。
                if effective_out is not None and frame_num >= effective_out + 1:
                    if self._in_out_range_active() and self.in_frame <= effective_out:
                        self._coordinator.seek_to_frame(self.in_frame, blocking=False)
                        self.current_frame = self.in_frame
                        self.frame_changed.emit(self.in_frame)
                        return
                    if self.loop_playback:
                        self._coordinator.seek_to_frame(0, blocking=False)
                        self.current_frame = 0
                        self.frame_changed.emit(0)
                        return
                    self.stop_playback()
                    return
                # VideoSession returns the current proxy frame while a newer
                # request is still decoding.  That is a display hold, not a new
                # frame: submitting it again would make Color re-upload and
                # re-grade the same image on every timer tick, starving the
                # renderer exactly when decode is under pressure.
                needs_grade_refresh = (
                    self._last_submitted_render_params_generation
                    != self._render_params_generation
                )
                # V-P0-14: 渲染拥塞时跳过 submit（但时钟/解码已由 coordinator.tick()
                # 推进），恢复后直接追最新帧，不补播旧帧。
                if skip_render_submit:
                    self._last_displayed_decoded_frame = frame
                    self._last_displayed_frame_index = frame_num
                    logger.debug(
                        "v3 tick: renderer congested, advancing clock to %s "
                        "without render submit",
                        frame_num,
                    )
                elif frame_num != self._last_displayed_frame_index or needs_grade_refresh:
                    _flash_diag.event(
                        'tick_submit', frame_num=int(frame_num),
                        grade_refresh=bool(needs_grade_refresh),
                        same_frame=bool(frame_num == self._last_displayed_frame_index),
                    )
                    self._submit_decoded_frame_playback(frame, frame_num)
                    # P0-1: 只保存 DecodedFrame 引用和帧号，避免 UI tick 大数组复制
                    # 无论是 packed 还是 planar 格式，都只保存轻量引用
                    self._last_displayed_decoded_frame = frame
                    self._last_displayed_frame_index = frame_num
                    self._last_submitted_render_params_generation = (
                        self._render_params_generation
                    )
                    # 注意：不再设置 _last_displayed_frame（numpy 数组）
                    # v3 路径使用 _last_displayed_decoded_frame 进行 hold last frame
                else:
                    logger.debug(
                        "v3 tick: holding displayed frame %s without re-render",
                        frame_num,
                    )
            else:
                # FIX: 诊断日志 - coordinator.tick() 返回 None
                # 可能原因：DecodeWorker 还没产出帧、PlaybackClock 未启动、队列为空
                session = self._coordinator.session if self._coordinator else None
                if session is not None:
                    clock = session.playback_clock
                    worker_mgr = session.worker_manager
                    clock_playing = clock.is_playing if clock else False
                    clock_frame = clock.current_frame if clock else -1
                    workers_running = worker_mgr.is_running() if worker_mgr else False
                    logger.debug(
                        f"v3 tick: frame=None, clock_playing={clock_playing}, "
                        f"clock_frame={clock_frame}, workers_running={workers_running}, "
                        f"session_state={session.state.name}"
                    )
                    # FIX: 如果 PlaybackClock 未启动，尝试重新启动
                    if clock is not None and not clock_playing:
                        now = time.monotonic()
                        if now - self._clock_restart_warn_ts >= 5.0:
                            self._clock_restart_warn_ts = now
                            logger.warning("v3 tick: PlaybackClock not playing, restarting...")
                        clock.start()

            # P0.A: 记录 tick 结束时间
            tick_end = time.perf_counter()
            tick_ms = (tick_end - tick_start) * 1000
            self._perf_monitor.track_tick_time(tick_ms)

            # P2.2: 记录帧总耗时用于代理阶梯判定
            fps = 30.0
            if self._coordinator and self._coordinator.session and self._coordinator.session.fps:
                fps = self._coordinator.session.fps
            self._perf_monitor.track_frame_total_time(tick_ms, fps)

            # S5.B: 检查 tick 性能并应用降级策略
            degradation_level = self._perf_monitor.check_tick_performance_and_degrade()
            # P1-1: 类型防御，防止测试中 mock 返回非 int
            if not isinstance(degradation_level, int):
                degradation_level = 0
            if degradation_level > 0:
                # 应用降级策略：降低预读前瞻
                lookahead_reduction = self._perf_monitor.get_prefetch_lookahead_reduction()
                # P1-1: 优先通过 session API 应用降级
                if self._coordinator and self._coordinator.session:
                    session = self._coordinator.session
                    if hasattr(session, 'apply_playback_degradation'):
                        # 调用 session 公开 API，不直接摸私有字段
                        result = session.apply_playback_degradation(degradation_level, lookahead_reduction)
                        logger.debug(
                            f"v3 degradation via session API: level={degradation_level}, "
                            f"lookahead_reduction={lookahead_reduction:.0%}, "
                            f"result={result}"
                        )
                    else:
                        # 回退：直接调整组件（兼容旧架构，session API 未实现）
                        if hasattr(session, 'decode_worker_manager'):
                            worker_manager = session.decode_worker_manager
                            if worker_manager and hasattr(worker_manager, 'prefetch_lookahead'):
                                original_lookahead = getattr(worker_manager, '_original_prefetch_lookahead', 8)
                                new_lookahead = int(original_lookahead * (1.0 - lookahead_reduction))
                                worker_manager.prefetch_lookahead = max(1, new_lookahead)
                                logger.debug(
                                    f"v3 degradation fallback: level={degradation_level}, "
                                    f"lookahead_reduction={lookahead_reduction:.0%}, "
                                    f"new_lookahead={new_lookahead}"
                                )
                        if hasattr(session, 'ready_frame_queue'):
                            queue = session.ready_frame_queue
                            if queue and hasattr(queue, 'max_size'):
                                original_max_size = getattr(queue, '_original_max_size', self._video_preview_max_queue)
                                new_max_size = int(original_max_size * (1.0 - lookahead_reduction))
                                queue.max_size = max(4, new_max_size)

            # P2.2: 检查代理阶梯升降级
            self._check_proxy_ladder()

        elif self._use_dual_path and self._playback_clock and self._source_manager:
            # DEPRECATED: 旧架构播放路径 — 所有新会话应使用 PlaybackCoordinator (v3)。
            # 此分支仅在 v2 fallback 场景激活，计划在确认所有路径迁移后移除。
            logger.warning("Using deprecated v2 playback path - consider migrating to PlaybackCoordinator")
            # P1.B: 使用旧架构 - UI tick 不直接解码
            # 1. 时钟推进
            frame_num = self._playback_clock.tick()

            # 检查是否到达结尾（或越过 In/Out 范围的 Out 点）
            effective_out = self._effective_out_frame()
            # V-P0-09: 未知时长（effective_out 为 None）时不做越界判定。
            if effective_out is not None and frame_num >= effective_out + 1:
                if self._in_out_range_active() and self.in_frame <= effective_out:
                    # I/O 范围循环：跳回 in_frame
                    self._playback_clock.seek_to_frame(self.in_frame)
                    if self._proxy_prefetch_worker:
                        self._proxy_prefetch_worker.request_seek(self.in_frame)
                    return
                elif self.loop_playback:
                    self._playback_clock.seek_to_frame(0)
                    if self._proxy_prefetch_worker:
                        self._proxy_prefetch_worker.request_seek(0)
                    return
                else:
                    self.stop_playback()
                    return

            # P1.B: 2. 从 ready queue/cache 非阻塞取帧
            # 绝不调用 get_proxy_frame() 阻塞 UI
            frame_array: Optional[np.ndarray] = None
            actual_frame: int = frame_num

            # 优先从 P1.B 的 ProxyPrefetchWorker 取帧（非阻塞，timeout=0）
            if self._proxy_prefetch_worker:
                result = self._proxy_prefetch_worker.get_ready_frame(frame_num, timeout=0.0)
                if result is not None:
                    frame_array, actual_frame = result
                    logger.debug(f"Got frame {actual_frame} from prefetch worker")
                else:
                    # 检查队列深度，如果为空记录一次警告
                    queue_depth = self._proxy_prefetch_worker.get_queue_depth()
                    if queue_depth == 0:
                        logger.debug(f"Prefetch queue empty at frame {frame_num}")

            # P1.B: 3. 没有 ready frame 就 hold last frame 或 drop
            if frame_array is None:
                # Hold last frame（保持上一帧显示）
                if self._last_displayed_frame_index is not None:
                    # 不提交新渲染，保持当前显示
                    logger.debug(f"Hold last frame at tick for frame {frame_num}")
                    pass
                else:
                    # 没有 last frame，跳过这次 tick
                    logger.debug(f"Dropped tick at frame {frame_num}")

                # P0.A: 记录 tick 结束时间（即使没有帧）
                tick_end = time.perf_counter()
                tick_ms = (tick_end - tick_start) * 1000
                self._perf_monitor.track_tick_time(tick_ms)
                return

            # 有帧，提交渲染
            self._submit_frame_playback(frame_array, actual_frame)

            # P1.B: 保存轻量状态，避免 UI tick 复制大数组
            self._last_displayed_frame_index = actual_frame

            # P0.A: 记录 tick 结束时间
            tick_end = time.perf_counter()
            tick_ms = (tick_end - tick_start) * 1000
            self._perf_monitor.track_tick_time(tick_ms)

        else:
            # v2 GStreamer 链路已移除：无可用播放路径，停止播放并报错
            logger.error(
                "_on_play_tick: 无可用播放路径（v2 GStreamer 链路已移除），停止播放"
            )
            self.stop_playback()

            # P0.A: 记录 tick 时间
            tick_end = time.perf_counter()
            tick_ms = (tick_end - tick_start) * 1000
            self._perf_monitor.track_tick_time(tick_ms)

    def update_viewport_size(self, width: int, height: int) -> None:
        """
        更新视口大小。

        同步更新 SourceManager 的视口配置。
        """
        super().update_viewport_size(width, height)

        if self._source_manager:
            self._source_manager.update_viewport_size((width, height))

    def get_dropped_frames(self) -> int:
        """
        获取累计丢帧数。

        Returns:
            丢帧数（仅双路径架构有效）
        """
        if self._playback_clock:
            return self._playback_clock.dropped_frames
        return 0

    def get_cache_stats(self) -> Optional[Dict[str, Any]]:
        """
        获取缓存统计信息。

        Returns:
            统计信息（仅双路径架构有效）
        """
        if self._source_manager:
            return self._source_manager.get_cache_stats()
        return None

    def get_clock_state(self) -> Optional[Any]:
        """
        获取时钟状态。

        Returns:
            ClockState（仅双路径架构有效）
        """
        if self._playback_clock:
            return self._playback_clock.get_state()
        return None

    def set_playback_speed(self, speed: float) -> None:
        """
        设置播放速度。

        同步更新 PlaybackClock 的速度。

        AUD-39602: 非法 speed（0/负/NaN/非数值）在基类入口被拒绝，
        且不得透传给 PlaybackClock——其 set_speed 对非正值的帧推进
        行为未定义。
        """
        speed_value = _validated_playback_speed(speed)
        if speed_value is None:
            logger.warning(f"Ignoring invalid playback speed: {speed!r}")
            return

        super().set_playback_speed(speed_value)

        if self._playback_clock:
            self._playback_clock.set_speed(speed_value)

    def get_source_manager(self) -> Optional[Any]:
        """获取源管理器"""
        # P5.A: 优先从 coordinator 获取
        if self._coordinator:
            return self._coordinator.source_manager
        return self._source_manager

    def get_playback_clock(self) -> Optional[Any]:
        """获取播放时钟"""
        # P5.A: 优先从 coordinator 获取
        if self._coordinator and self._coordinator.session:
            return self._coordinator.session.playback_clock
        return self._playback_clock

    def get_coordinator(self) -> Optional[Any]:
        """
        获取 PlaybackCoordinator（P5.A 新增）。

        Returns:
            PlaybackCoordinator 实例
        """
        return self._coordinator

    def get_video_info(self) -> Optional[Dict[str, Any]]:
        """
        获取当前视频信息（P5.A 改造版）。

        Returns:
            视频信息字典
        """
        # P5.A: 优先从 coordinator 获取
        if self._coordinator:
            return self._coordinator.get_video_info()

        # 向后兼容：从父类获取
        return super().get_video_info()

    # P0.A: 性能统计接口
    def get_perf_stats(self) -> Dict[str, Any]:
        """
        获取性能统计数据。

        返回聚合的播放性能指标，用于性能监控和分析。

        Returns:
            性能统计字典，包含：
            - tick_avg_ms: 平均 UI tick 耗时
            - tick_max_ms: 最大 UI tick 耗时
            - decode_avg_ms: 平均解码耗时
            - process_avg_ms: 平均处理耗时
            - upload_avg_ms: 平均上传提交耗时
            - render_avg_ms: 平均渲染提交耗时
            - queue_avg_depth: 平均队列深度
            - dropped_frames: 累计丢帧数
            - cache_hit_rate: 缓存命中率
            - frames_processed: 处理的总帧数
        """
        return self._perf_monitor.get_aggregate_stats()

    def get_perf_monitor(self) -> VideoPerformanceMonitor:
        """
        获取性能监控器实例。

        用于直接访问监控器的高级功能。

        Returns:
            VideoPerformanceMonitor 实例
        """
        return self._perf_monitor

    def reset_perf_stats(self) -> None:
        """
        重置性能统计数据。

        清空所有统计计数器。
        """
        self._perf_monitor.clear()

    def get_pipeline_report(self) -> Dict[str, Any]:
        """
        返回当前播放管线的运行时报告。

        聚合已有的管线状态数据，用于诊断和调试。
        不添加新的追踪逻辑，仅聚合现有数据。

        Returns:
            字典包含 pipeline_mode, source_backend, codec/pixel_format/bit_depth,
            decode_path, render_frame_path, zero_copy, fallback_reason,
            renderer_queue_depth, finish/readback counters 等。
        """
        report: Dict[str, Any] = {}

        # 1. pipeline_mode (v2/v3)
        if hasattr(self, '_pipeline_decision'):
            report['pipeline_mode'] = self._pipeline_decision.effective_mode.value
        else:
            report['pipeline_mode'] = self._video_pipeline_version

        # 2. source_backend
        if self._coordinator:
            sm = self._coordinator.source_manager
            if sm and hasattr(sm, 'backend_name'):
                report['source_backend'] = sm.backend_name
            elif sm and hasattr(sm, 'backend'):
                report['source_backend'] = str(sm.backend)
        if 'source_backend' not in report and self._source_manager:
            for attr in ('backend_name', 'backend'):
                val = getattr(self._source_manager, attr, None)
                if val is not None:
                    report['source_backend'] = str(val)
                    break
        report.setdefault('source_backend', 'unknown')

        # 3. codec / pixel_format / bit_depth from video_info
        video_info = self.get_video_info() or {}
        report['codec'] = video_info.get('codec', 'unknown')
        report['pixel_format'] = video_info.get('pixel_format', 'unknown')

        # bit_depth from source manager metadata
        bit_depth = None
        src = (self._coordinator.source_manager
               if self._coordinator else self._source_manager)
        if src:
            for attr in ('bit_depth', 'metadata'):
                val = getattr(src, attr, None)
                if val is not None:
                    if attr == 'bit_depth':
                        bit_depth = val
                    elif isinstance(val, dict):
                        bit_depth = val.get('bit_depth')
                    break
        report['bit_depth'] = bit_depth

        # 4. decode_path / render_frame_path / zero_copy from last GPU frame
        report['decode_path'] = getattr(
            self, '_last_decode_path', None)
        report['render_frame_path'] = getattr(
            self, '_last_render_frame_path', None)
        report['zero_copy'] = getattr(
            self, '_last_zero_copy', False)

        # 5. fallback_reason
        report['fallback_reason'] = get_last_fallback_reason()

        # 6. renderer_queue_depth from perf monitor
        perf_stats = self._perf_monitor.get_aggregate_stats()
        report['renderer_queue_depth'] = perf_stats.get(
            'renderer_queue_depth', 0)

        # 7. finish/readback counters from perf stats
        report['finish_count'] = perf_stats.get('finish_count', 0)
        report['readback_count'] = perf_stats.get('readback_count', 0)

        return report
