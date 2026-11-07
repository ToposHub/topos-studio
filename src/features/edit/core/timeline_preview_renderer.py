"""
Timeline Preview Renderer Module - Preview rendering for Edit timeline.

时间线预览渲染器模块，实现 Edit 页面的预览渲染管线。

## 聧责

- TimelinePreviewRenderer: 预览渲染控制器
- PreviewRenderWorker: 后台渲染线程
- PreviewFrameCache: 帧缓存（LRU）

## 设计原则

- 异步渲染：不阻塞 UI 线程
- 复用现有组件：VideoFrameProviderService, VideoAsyncRenderer
- 信号驱动：渲染完成通过信号通知 Viewer
- 性能优化：帧缓存、代理分辨率

## 架构

```text
┌─────────────────────────────────────────────────────────────────────┐
│ TimelinePanel / Viewer                                               │
│ - 监听 frame_ready 信号                                               │
│ - 移动播放头触发 request_render(frame)                                │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ request_render(frame)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ TimelinePreviewRenderer                                              │
│ - 管理 PreviewRenderWorker                                           │
│ - 管理 PreviewFrameCache                                             │
│ - 持有 VideoFrameProviderService                                     │
│ - 持有 AssetResolver                                                 │
│ - 持有 RenderPlanBuilder                                             │
└───────────────────────────────┬─────────────────────────────────────┘
                                │ enqueue(frame, render_plan)
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│ PreviewRenderWorker (后台线程)                                        │
│ - 执行 RenderPlan                                                    │
│ - 使用 VideoFrameProviderService 获取帧                              │
│ - 使用 VideoAsyncRenderer 上传并渲染                                  │
│ - 发射 frame_rendered 信号                                            │
└─────────────────────────────────────────────────────────────────────┘
```

## 渲染流程

1. 播放头移动 -> TimelinePanel 调用 request_render(frame)
2. TimelinePreviewRenderer:
   - 检查缓存 -> 如果有缓存帧，直接发射 frame_ready
   - 否则 -> 使用 RenderPlanBuilder 计算 RenderPlan
   - 将任务加入渲染队列
3. PreviewRenderWorker:
   - 从队列获取任务
   - 根据 RenderPlan.items 遍历 clip
   - 通过 AssetResolver 获取素材路径
   - 通过 VideoFrameProviderService 获取帧
   - 合成（第一版：取最上层）
   - 使用 VideoAsyncRenderer 上传并渲染
4. 发射 frame_ready 信号 -> Viewer 更新显示
"""

from __future__ import annotations

import logging
import math
import numpy as np
import queue
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from typing import Optional, Dict, Any, Tuple, List, Callable, TYPE_CHECKING
from PyQt6.QtCore import QObject, pyqtSignal, QThread, QMutex, QTimer
from PyQt6.QtGui import QImage, QOpenGLContext, QOffscreenSurface

from src.features.color.core.curves_soft_clip import (
    curves_edit_is_default,
    curves_soft_clip_is_identity,
)
from src.features.edit.core.timeline_render_plan import (
    TimelineRenderPlan,
    FrameRenderPlan,
    ClipLayerInfo,
    TransitionBlendInfo,
)
# FX-C02 渲染操作计划（AUD-16808 生产消费点）：GPU 任务装车循环的分类权威。
from src.features.edit.core.effect_render_operations import (
    ApplyAdjustment,
    build_render_operations,
)
from src.features.edit.core.transition_mask import compute_transition_mask
from src.features.edit.core.transition_catalog import (
    FULL_FRAME_TRANSITION_TYPES,
    RESAMPLE_TRANSITION_TYPES,
    TRANSITION_EFFECT_TYPES,
    is_composite_transition,
    is_dip_transition,
)
from src.features.edit.core.transition_effects import (
    apply_full_frame_transition,
    apply_transition_effect,
)
from src.features.edit.core.timeline_v2 import TimelineSequence
from src.features.edit.core.project import EditProject
from src.features.edit.core.playback_debug import playback_trace
from src.features.edit.core.crop_cpu import apply_crop_cpu, crop_is_identity
from src.shared.gpu.gl_backend import GLContextManager

if TYPE_CHECKING:
    # 仅供类型注解使用；运行时由 MG 分支按需延迟导入，避免跨 feature
    # 的模块级依赖。
    from src.features.teaching_animation.core.contracts import RenderContext
    from src.features.edit.core.realtime_playback.task_priority import FrameTaskPriority


# Program 的“流畅”预览目标：1920 长边对应 16:9 的 1920×1080。
# 仅作为高分辨率素材的下限，不会放大低于 1080p 的素材。
_SMOOTH_PREVIEW_EDGE = 1920


# ================================================================
# AssetResolver - 素材路径解析器
# ================================================================

class AssetResolver:
    """
    素材路径解析器。

    从 EditProject 获取素材路径。

    使用方式：
    ```python
    resolver = AssetResolver(edit_project)
    path = resolver.get_asset_path(asset_id)
    ```
    """

    def __init__(self, project: Any) -> None:  # EditProject
        """
        初始化解析器。

        Args:
            project: EditProject 实例
        """
        self._project = project

    def get_asset_path(self, asset_id: str) -> Optional[str]:
        """
        获取素材路径。

        Args:
            asset_id: 素材 ID

        Returns:
            素材文件路径，或 None（素材不存在）
        """
        from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
        if motion_graphic_scene_id(asset_id) is not None:
            # Motion Graphic clips reference the shared TeachingSceneStore,
            # not a file in EditProject.assets.
            return None
        asset = self._project.get_asset_by_id(asset_id)
        if asset is None:
            logger.warning(f"Asset not found: {asset_id}")
            return None
        return asset.path

    def get_asset_metadata(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """
        获取素材元数据。

        Args:
            asset_id: 素材 ID

        Returns:
            元数据字典，或 None
        """
        asset = self._project.get_asset_by_id(asset_id)
        if asset is None:
            return None
        return asset.metadata

    def get_asset_type(self, asset_id: str) -> Optional[str]:
        """
        获取素材类型。

        Args:
            asset_id: 素材 ID

        Returns:
            媒体类型（'image', 'video', 'audio', 'image_sequence',
            'comp_clip', 'nested_sequence', 'adjustment', 'solid'），或 None
        """
        from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
        if motion_graphic_scene_id(asset_id) is not None:
            return "teaching_animation"
        asset = self._project.get_asset_by_id(asset_id)
        if asset is None:
            return None
        return asset.media_type

    def get_asset(self, asset_id: str) -> Optional[Any]:
        """获取完整 MediaAsset（用于读取 generator 专属字段如 solid_color）。"""
        return self._project.get_asset_by_id(asset_id)

# Type hints for external modules (avoid circular imports)
if False:
    pass


logger = logging.getLogger(__name__)

# TL-C01: sentinel asset_path marking a comp_clip GPU-direct decode job
# (the layer carries a CompositeGpuFrameHandle instead of a decoded frame).
COMP_GPU_JOB_SENTINEL = "__comp_gpu__"


_DECODE_TELEMETRY_DUMP_EVERY: Optional[int] = None
_DECODE_TELEMETRY_RESOLVED = False


def _decode_telemetry_dump_every() -> int:
    """``TOPOS_PLAYBACK_DECODE_TELEMETRY=N``：每 N 帧导出一次解码波遥测。

    W3 zero-copy 立项的取数入口（K-W1 触发信号）：decode_ms 按
    decode_layers 分桶随轨数恶化 + 超预算率 → 多流解码供给饱和证据。
    默认 0 = 关闭（每帧零额外开销）；>0 时 dump 到系统临时目录并落
    INFO 日志。env 解析一次后缓存（进程内不重复读）。
    """
    global _DECODE_TELEMETRY_DUMP_EVERY, _DECODE_TELEMETRY_RESOLVED
    if not _DECODE_TELEMETRY_RESOLVED:
        _DECODE_TELEMETRY_RESOLVED = True
        try:
            _DECODE_TELEMETRY_DUMP_EVERY = max(
                0,
                int(os.environ.get("TOPOS_PLAYBACK_DECODE_TELEMETRY", "0") or 0),
            )
        except (TypeError, ValueError):
            _DECODE_TELEMETRY_DUMP_EVERY = 0
    return _DECODE_TELEMETRY_DUMP_EVERY or 0


_REALTIME_HOLD_ON_GPU_FALLBACK_REASONS = frozenset({
    "decode_failed_or_cpu_packed",
    "gpu_renderer_queue_full",
    "no_valid_layers_for_gpu",
})


# ================================================================
# PreviewFrameCache - 帧缓存（LRU）
# ================================================================

class PreviewFrameCache:
    """
    预览帧缓存（字节预算 LRU）。

    缓存最近渲染的帧，减少重复渲染。淘汰策略双保险：
    - max_size 条数上限（防巨量小帧）
    - max_bytes 字节预算（QImage/ndarray 按实际字节数计价；4K 帧约 33MB，
      帧数上限不能反映真实内存占用）

    Attributes:
        max_size: 最大缓存帧数
        max_bytes: 最大缓存字节数
        frames: OrderedDict (frame -> cached_data)
        mutex: 线程锁
    """

    _UNKNOWN_ENTRY_BYTES = 8 * 1024 * 1024  # 未知类型按 8MB 估价

    def __init__(self, max_size: int = 256, max_bytes: Optional[int] = None) -> None:
        """
        初始化帧缓存。

        Args:
            max_size: 最大缓存帧数（默认 256；字节预算是主要限制）
            max_bytes: 最大缓存字节数。None 时读环境变量
                TOPOS_PREVIEW_FRAME_CACHE_MB（默认 512MB）
        """
        self.max_size = max(1, int(max_size))
        if max_bytes is None:
            try:
                env_mb = float(os.environ.get("TOPOS_PREVIEW_FRAME_CACHE_MB", "512"))
            except (TypeError, ValueError):
                env_mb = 512.0
            max_bytes = int(max(64.0, env_mb) * 1024 * 1024)
        self.max_bytes = int(max_bytes)
        self._frames: OrderedDict[int, Any] = OrderedDict()
        self._entry_bytes: Dict[int, int] = {}
        self._bytes_used: int = 0
        self._mutex = QMutex()

        # 缓存统计
        self._total_hits = 0
        self._total_misses = 0

    @classmethod
    def _estimate_entry_bytes(cls, data: Any) -> int:
        """估算单条缓存条目的内存占用（字节）。"""
        size_fn = getattr(data, "sizeInBytes", None)
        if callable(size_fn):
            try:
                return max(1, int(size_fn()))
            except Exception:
                pass
        nbytes = getattr(data, "nbytes", None)
        if isinstance(nbytes, int) and nbytes > 0:
            return nbytes
        # dict 载荷（GPU present descriptor 等）：按 width*height*4 估价
        width = getattr(data, "width", None)
        height = getattr(data, "height", None)
        if callable(width) and callable(height):
            try:
                return max(1, int(width()) * int(height()) * 4)
            except Exception:
                pass
        return cls._UNKNOWN_ENTRY_BYTES

    def get(self, frame: int) -> Optional[Any]:
        """
        获取缓存的帧。

        Args:
            frame: 帧号

        Returns:
            缓存数据或 None
        """
        self._mutex.lock()
        try:
            if frame in self._frames:
                # 移到末尾（最近使用）
                self._frames.move_to_end(frame)
                self._total_hits += 1
                return self._frames[frame]
            self._total_misses += 1
            return None
        finally:
            self._mutex.unlock()

    def put(self, frame: int, data: Any) -> None:
        """
        添加帧到缓存。

        Args:
            frame: 帧号
            data: 缓存数据
        """
        self._mutex.lock()
        try:
            # 如果已存在，先回收旧条目的字节配额
            if frame in self._frames:
                del self._frames[frame]
                self._bytes_used -= self._entry_bytes.pop(frame, 0)

            entry_bytes = self._estimate_entry_bytes(data)
            # 单帧超预算时不缓存（否则会挤掉整缓存）
            if entry_bytes > self.max_bytes:
                logger.debug(
                    f"PreviewFrameCache: frame {frame} too large "
                    f"({entry_bytes} > {self.max_bytes}), skip caching"
                )
                return

            # 添加新的
            self._frames[frame] = data
            self._entry_bytes[frame] = entry_bytes
            self._bytes_used += entry_bytes

            # 淘汰最旧的，直到同时满足条数与字节预算
            while len(self._frames) > self.max_size or self._bytes_used > self.max_bytes:
                oldest_frame, _ = next(iter(self._frames.items()))
                del self._frames[oldest_frame]
                self._bytes_used -= self._entry_bytes.pop(oldest_frame, 0)
                logger.debug(f"Cache evicted frame {oldest_frame}")
                if not self._frames:
                    break
        finally:
            self._mutex.unlock()

    def clear(self) -> None:
        """清空缓存"""
        self._mutex.lock()
        self._frames.clear()
        self._entry_bytes.clear()
        self._bytes_used = 0
        self._mutex.unlock()

    def invalidate(self, start_frame: int, end_frame: int) -> None:
        """
        使帧范围失效（需要重新渲染）。

        Args:
            start_frame: 开始帧号
            end_frame: 结束帧号
        """
        self._mutex.lock()
        try:
            for frame in range(start_frame, end_frame + 1):
                if frame in self._frames:
                    del self._frames[frame]
                    self._bytes_used -= self._entry_bytes.pop(frame, 0)
        finally:
            self._mutex.unlock()

    def get_hit_rate(self) -> float:
        """获取缓存命中率"""
        total = self._total_hits + self._total_misses
        if total == 0:
            return 0.0
        return self._total_hits / total

    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        self._mutex.lock()
        size = len(self._frames)
        bytes_used = self._bytes_used
        self._mutex.unlock()
        return {
            "size": size,
            "max_size": self.max_size,
            "bytes_used": bytes_used,
            "max_bytes": self.max_bytes,
            "hits": self._total_hits,
            "misses": self._total_misses,
            "hit_rate": self.get_hit_rate(),
        }


# ================================================================
# PreviewRenderWorker - 后台渲染线程
# ================================================================

class _GuardedPreviewSource:
    """直连预览源的线程安全句柄：close 延迟到在飞 read_frame 结束。

    worker 在锁外调用 source.read_frame()（PyAV C 层长解码）；UI 线程的
    时间线重建会并发 _clear_direct_preview_sources() 直接 close 源。
    PyAV 容器在解码中被释放 → avcodec 段错误（8 轨场景 ~1/3 复现）。
    句柄用 inflight 计数：close 只标记，真正释放发生在最后一个读者
    release() 时。

    AUD-18900：closed 判定与 inflight 计数必须原子（_lock 保护
    check-then-act）。否则 retire 可在 worker 通过 closed 检查之后、
    inflight += 1 之前看到 inflight==0 并立即 close 源，随后 worker
    对已释放的 PyAV 容器 read_frame——正是本类要防的段错误窗口。
    source.close() 在锁外执行（close 可能触碰 C 层，避免阻塞 acquire），
    由 _source_closed 幂等标记保证只 close 一次。
    """

    __slots__ = ("source", "inflight", "closed", "_lock", "_source_closed")

    def __init__(self, source: Any) -> None:
        self.source = source
        self.inflight = 0
        self.closed = False
        self._lock = threading.Lock()
        self._source_closed = False

    def acquire(self) -> Any:
        """登记一个在飞读者，返回底层源（仅未关闭时）。

        closed 检查与 inflight 递增持锁原子完成：一旦返回源，retire
        必然能看到 inflight>0 而推迟 close。
        """
        with self._lock:
            if self.closed:
                return None
            self.inflight += 1
            return self.source

    def release(self) -> None:
        """读者结束；若已标记关闭且无其他读者，真正关闭源。"""
        close_now = False
        with self._lock:
            self.inflight -= 1
            if self.closed and self.inflight <= 0 and not self._source_closed:
                self._source_closed = True
                close_now = True
        if close_now:
            self._close_source()

    def retire(self) -> None:
        """标记关闭；空闲则立即释放（否则推迟到最后一个读者 release）。"""
        close_now = False
        with self._lock:
            self.closed = True
            if self.inflight <= 0 and not self._source_closed:
                self._source_closed = True
                close_now = True
        if close_now:
            self._close_source()

    def _close_source(self) -> None:
        """真正释放底层源（幂等性由 _source_closed 保证，锁外调用）。"""
        try:
            self.source.close()
        except Exception:
            pass


class PreviewRenderWorker(QThread):
    """
    后台预览渲染线程。

    执行 RenderPlan，使用 VideoFrameProviderService 获取帧，
    并通过 VideoAsyncRenderer 上传渲染。

    信号:
        frame_rendered: 帧渲染完成 (frame, texture_id 或 QImage)
        render_error: 渲染错误 (frame, error_message)
    """

    frame_rendered = pyqtSignal(int, object)  # frame, result (texture_id or QImage)
    render_error = pyqtSignal(int, str)  # frame, error_message
    offline_media = pyqtSignal(int, object)  # frame, [{asset_id, name, path}]
    gpu_submitted = pyqtSignal(int, int)  # frame, generation_id
    # CPU 后台缓存回填（idle prebuffer）：只进帧缓存，不上屏。
    background_frame_ready = pyqtSignal(int, int, object)  # frame, generation, QImage

    def __init__(
        self,
        frame_provider: Any,  # VideoFrameProviderService
        asset_resolver: AssetResolver,
        video_renderer: Any,  # VideoAsyncRenderer (optional)
        timeline_sequence: TimelineSequence | None = None,
        viewport_size: Tuple[int, int] = (1920, 1080),
        max_preview_edge: int = 3840,
        gpu_decode_manager: Any = None,  # EditGpuDecodeManager (optional)
        gpu_renderer: Any = None,  # EditGpuPreviewRenderer (optional)
    ) -> None:
        """
        初始化渲染线程。

        Args:
            frame_provider: VideoFrameProviderService 实例
            asset_resolver: AssetResolver 实例
            video_renderer: VideoAsyncRenderer 实例（可选，用于 GPU 渲染）
            viewport_size: 视口尺寸 (width, height)
            max_preview_edge: 编辑预览解码最长边上限；默认 4K，只有 6K/8K
                等更大时间线会降到 4K 级别
            gpu_decode_manager: EditGpuDecodeManager 实例（可选，GPU 解码）
            gpu_renderer: EditGpuPreviewRenderer 实例（可选，GPU 渲染）
        """
        super().__init__()
        self.setObjectName("PreviewRenderWorker")

        self._frame_provider = frame_provider
        self._asset_resolver = asset_resolver
        self._video_renderer = video_renderer
        self._timeline_sequence = timeline_sequence
        self._viewport_size = viewport_size
        # 渲染完成回调（frame, decode_ms, total_ms, composite_ms）——
        # 由 renderer 注入，用于回填引擎遥测的真实管线耗时。
        self._render_trace_hook = None
        self._max_preview_edge = max(64, int(max_preview_edge))
        self._preview_quality: float = 0.0
        # 预览跳过输出去块（用户偏好 video_preview_no_deblock）：只作用于预览
        # 解码 request，不动交付/导出路径（见 ToposMediaSource.no_deblock）。
        self._no_deblock: bool = False
        # 嵌套序列支持：解析子序列的回调（由上层注入），以及递归渲染防循环栈。
        # 默认 None 时，_render_nested_sequence_frame 回退到仅返回当前序列。
        self._sequence_resolver: Optional[Callable[[str], Optional[TimelineSequence]]] = None
        self._nested_render_stack: set[str] = set()
        # A render task may own a temporary decode size.  Scrub can use the
        # playback target for GPU paths while CPU-only preview keeps a smaller
        # independent target.
        self._active_decode_target_size: Optional[Tuple[int, int]] = None
        # 当前执行的任务是否为 idle-prebuffer 后台缓存回填任务：结果只进
        # 帧缓存（background_frame_ready / GPU snapshot），不驱动 viewer 与
        # 交互单飞状态机。
        self._current_task_background: bool = False
        self._gpu_decode_manager = gpu_decode_manager
        # AUD-18902：共享解码资源只销毁一次（stop 超时路径经 finished
        # 信号延迟销毁时防止与直接调用双跑）。
        self._decode_resources_torn_down: bool = False
        self._gpu_renderer = gpu_renderer
        self._gpu_compositor: Any = None
        # Optional bounded scheduler handoff.  The worker remains the owner of
        # the final EditRenderTask, so rich effects/transitions keep their
        # existing metadata path while ready scheduler handles can replace
        # only the decode step.
        self._timeline_decode_scheduler: Any = None
        self._gpu_submitted_callback: Optional[Callable[[int, int], None]] = None
        self._interactive_scrub_lock = threading.Lock()
        self._interactive_scrub_active: bool = False
        self._interactive_scrub_token: int = 0
        self._interactive_scrub_frame: int = -1
        # Scrub is latest-only: once the mouse moves, an old random seek must
        # release the worker quickly so the newest target can be decoded.
        self._interactive_scrub_decode_deadline_ms: float = 160.0
        self._interactive_scrub_stale_grace_ms: float = 16.0
        # In-flight scrub decodes are abandoned only when the playhead moved
        # farther than this many frames from their target. Being superseded by
        # a nearby newer request must not abort a running decode — that kills
        # nearly-finished GOP walks and freezes the viewer during fast drags.
        self._SCRUB_ABANDON_DISTANCE_FRAMES: int = 120
        # 并发层解码：4 轨 4K 一波解完（18.3→23.4fps），8 轨时间线也保持
        # 单波（idle 线程零成本）。VT 和 PyAV 解码释放 GIL，线程真实扩展。
        # 注意与核显硬解实例的平衡：只有可见层会提交解码任务，8 线程只在
        # ≥5 可见层时才真正并发。
        self._decode_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="EditPreviewDecode",
        )

        # CompositeFrameRenderer for comp_clip assets (lazy-initialized)
        self._comp_frame_renderer = None

        # GL context for comp_clip rendering（后注入：由 _init_gpu_pipeline 经
        # TimelinePreviewRenderer.set_comp_gl_context 在 GL 就绪后调用 setup）。
        # worker.run() 本身不碰 GL；仅 _render_comp_clip_frame 渲染前后
        # makeCurrent/doneCurrent 这个共享 context。
        self._comp_gl_context: Optional[QOpenGLContext] = None
        self._comp_gl_surface: Optional[QOffscreenSurface] = None
        self._comp_gl_ready: bool = False
        # True only while _try_gpu_render holds the worker-owned shared
        # context for a batch of Comp Clip producers.  It prevents nested
        # producer calls from repeatedly locking/making-current the same
        # context and makes the ownership boundary explicit.
        self._comp_gl_context_current: bool = False

        # CompClip CPU fallback frame cache: (graph_id, source_frame,
        # caption_signature, timeline_frame, output_width, output_height)
        # -> BGR uint8 numpy.  The count-only limit was
        # unsafe for 4K frames (60 frames can exceed 1.5 GiB), so eviction is
        # now a true byte-budgeted LRU with a count ceiling as a second guard.
        self._comp_clip_cache: OrderedDict[
            Tuple[Any, ...], np.ndarray
        ] = OrderedDict()
        self._comp_clip_cache_max: int = 60
        self._comp_clip_cache_max_bytes: int = 256 * 1024 * 1024
        self._comp_clip_cache_bytes: int = 0
        self._comp_clip_cache_lock = threading.RLock()
        # Set by invalidate_composite_cache() (UI thread); consumed on the
        # worker thread while the shared GL context is current, where the
        # CompositeFrameRenderer's persistent node cache can be safely freed.
        self._comp_graph_dirty: bool = False

        # R-8: Last composite render error (set per-frame, cleared before each composite)
        self._last_comp_error: str | None = None

        # TL-C01: set when the GPU compositor declares comp-gpu support and
        # the comp renderer has been promoted to the GPU-direct producer.
        self._comp_gpu_direct_enabled: bool = False

        # 渲染队列：预览只需要最新请求，播放时丢弃旧帧避免队列堆积。
        self._queue: queue.Queue = queue.Queue(maxsize=2)
        self._stop_flag = False
        self._generation_id = 0

        # Clip ID -> clip_id 映射（由 frame_provider 管理）
        self._clip_id_map: Dict[str, str] = {}  # asset_id -> clip_id (frame_provider)

        # Clip 会话管理.  The key includes the timeline clip identity so
        # repeated use of the same asset on multiple tracks does not share a
        # decoder cursor and seek back/forth during playback.
        self._open_clips: Dict[str, str] = {}  # session_key -> clip_session_id
        self._prebuffer_clips: Dict[str, str] = {}  # session_key -> prebuffer clip_session_id
        # 每个直连源持有一个打开的 FFmpeg 解码器（文件句柄 + 解码上下文），
        # 长会话浏览大量 clip 会无限累积。LRU 上限：超出后退休最旧源
        # （_GuardedPreviewSource.retire 在飞行读取结束后才真正 close）。
        self._direct_preview_sources: OrderedDict[
            tuple[str, str, Tuple[int, int]], Any
        ] = OrderedDict()
        self._DIRECT_PREVIEW_SOURCES_MAX: int = 6
        # A completed Motion Graphic render-cache file is also a playback
        # source.  Keeping one sequential FFmpeg decoder per cache path makes
        # a reopened project smooth even when the in-memory HTML scene cache
        # has been evicted or was never populated in this process.
        self._render_cache_sources: OrderedDict[
            tuple[str, Tuple[int, int]], Any
        ] = OrderedDict()
        self._RENDER_CACHE_SOURCES_MAX: int = 4
        self._render_cache_last_frames: Dict[
            tuple[str, Tuple[int, int]], tuple[int, np.ndarray]
        ] = {}
        # The direct FFmpeg preview source advances its cursor after returning
        # a frame.  A transition deliberately holds one side on its last
        # source frame for several timeline frames; asking read_frame(99)
        # again after the cursor has advanced to 100 would otherwise perform
        # a backwards GOP seek on every render.  Keep the most recent packed
        # frame per source so held transition frames are true cache hits.
        self._direct_preview_last_frames: Dict[
            tuple[str, str, Tuple[int, int]], tuple[int, np.ndarray]
        ] = {}
        # Motion Graphic static-frame fallback for playback: while a clip's
        # disk render cache is not VALID yet, playback must never block this
        # worker on a browser capture.  ``_playback_active`` mirrors
        # notify_playback_state from the UI thread (simple bool, GIL-safe like
        # the decode manager's set_playback_active); the memo holds converted
        # BGR poster frames keyed by the live nearest-frame lookup result so a
        # hit does not re-convert the cached RGBA on every rendered frame.
        self._playback_active: bool = False
        self._mg_static_frames: Dict[Tuple[str, int, Tuple[int, int]], np.ndarray] = {}
        self._mg_static_frames_max: int = 32
        self._open_clips_lock = threading.Lock()
        self._direct_preview_lock = threading.Lock()
        # direct preview 快路创建失败过的 key（负缓存）：Topos 等私有编码
        # 无法产出廉价 packed RGB 源，曾以每帧 1-2 次的频率重试失败的
        # av.open。记一次失败即跳过，素材回落 GPU planar/FrameProvider 主路。
        self._direct_preview_failed: set = set()

        # 线程锁
        self._mutex = QMutex()

    def set_render_trace_hook(self, hook) -> None:
        """注册渲染耗时回填回调（renderer → 引擎遥测）。"""
        self._render_trace_hook = hook

    def set_playback_active(self, active: bool) -> None:
        """UI 线程通知播放状态；决定 MG 层走同步渲染还是静帧回退。"""
        self._playback_active = bool(active)
        if not active:
            # 播放结束后清理静帧 memo：拖擦/暂停路径会用新的场景内容哈希
            # 重新渲染真实帧，旧静帧不应在场景被编辑后继续命中。
            self._mg_static_frames.clear()

    def set_realtime_decode_scheduler(self, scheduler: Any) -> None:
        """Attach the renderer-owned scheduler used by realtime GPU renders."""
        self._timeline_decode_scheduler = scheduler

    def set_gpu_compositor(self, compositor: Any) -> None:
        """Attach the single GPU submission boundary for Edit preview."""
        self._gpu_compositor = compositor

    def _record_frame_trace(self, trace: Any) -> None:
        """记录帧 trace 并把真实耗时回填给引擎遥测。"""
        from src.shared.video.playback_frame_trace import get_trace_collector

        collector = get_trace_collector()
        collector.record(trace)
        every = _decode_telemetry_dump_every()
        if every and collector.trace_count and collector.trace_count % every == 0:
            self._dump_decode_telemetry(collector)
        hook = self._render_trace_hook
        if hook is not None:
            try:
                hook(
                    trace.frame_index,
                    float(getattr(trace, "decode_ms", 0.0) or 0.0),
                    float(getattr(trace, "total_ms", 0.0) or 0.0),
                    float(getattr(trace, "render_ms", 0.0) or 0.0),
                    int(getattr(trace, "trace_id", 0) or 0),
                )
            except Exception:
                pass

    def _dump_decode_telemetry(self, collector: Any) -> None:
        """导出解码波饱和遥测（TOPOS_PLAYBACK_DECODE_TELEMETRY=N 门控）。

        多流解码供给饱和的取数入口：按 decode_layers 分桶的 decode_ms
        分布 + 超预算率进 INFO 日志，全量 ring buffer 落临时目录 JSON。
        """
        import json as _json
        import tempfile as _tempfile
        from pathlib import Path as _Path

        try:
            summary = collector.get_summary()
            saturation = summary.get("decode_saturation") or {}
            logger.info(
                "decode-telemetry: frames=%s saturation=%s",
                summary.get("total_frames"),
                _json.dumps(saturation, ensure_ascii=False),
            )
            path = (
                _Path(_tempfile.gettempdir())
                / "topos_playback_decode_telemetry.json"
            )
            path.write_text(collector.export_json(), encoding="utf-8")
            logger.info("decode-telemetry: full dump -> %s", path)
        except Exception:  # noqa: BLE001  遥测失败绝不影响播放
            logger.debug("decode-telemetry dump failed", exc_info=True)

    def run(self) -> None:
        """线程主循环"""
        logger.info(f"{self.objectName()} started")

        while not self._stop_flag:
            try:
                # 等待任务（短超时以支持退出检查）
                task = self._queue.get(timeout=0.05)

                if task is None:
                    continue

                skip_cpu_fallback_on_gpu_miss = False
                source_decisions_by_clip: Dict[str, Any] = {}
                decode_target_size: Optional[Tuple[int, int]] = None
                task_meta: Dict[str, Any] = {}
                if len(task) >= 7:
                    (
                        frame, render_plan, generation_id,
                        skip_cpu_fallback_on_gpu_miss, source_decisions_by_clip,
                        decode_target_size, task_meta,
                    ) = task[:7]
                elif len(task) >= 6:
                    (
                        frame, render_plan, generation_id,
                        skip_cpu_fallback_on_gpu_miss, source_decisions_by_clip,
                        decode_target_size,
                    ) = task[:6]
                elif len(task) >= 5:
                    # Final-2: 5-tuple with source decisions map.
                    (
                        frame, render_plan, generation_id,
                        skip_cpu_fallback_on_gpu_miss, source_decisions_by_clip,
                    ) = task[:5]
                elif len(task) == 4:
                    frame, render_plan, generation_id, skip_cpu_fallback_on_gpu_miss = task
                elif len(task) == 3:
                    frame, render_plan, generation_id = task
                else:
                    frame, render_plan = task
                    generation_id = self._generation_id

                if generation_id != self._generation_id:
                    playback_trace(
                        "worker.drop_stale_generation_before_execute",
                        frame=frame,
                        task_generation=generation_id,
                        worker_generation=self._generation_id,
                    )
                    self._queue.task_done()
                    continue

                if self._is_abandoned_interactive_scrub_task(frame, task_meta):
                    self._emit_interactive_scrub_drop(
                        frame,
                        generation_id,
                        task_meta,
                        "abandoned_far_before_execute",
                    )
                    self._queue.task_done()
                    continue

                # 执行渲染
                request_started_at = task_meta.get("request_started_at")
                wait_ms = (
                    (time.perf_counter() - float(request_started_at)) * 1000.0
                    if isinstance(request_started_at, (int, float)) else 0.0
                )
                playback_trace(
                    "worker.dequeue_execute",
                    frame=frame,
                    generation=generation_id,
                    request_kind=task_meta.get("request_kind", ""),
                    scrub_token=task_meta.get("scrub_request_token", ""),
                    queue_wait_ms=wait_ms,
                )
                self._active_decode_target_size = decode_target_size
                self._current_task_background = bool(task_meta.get("background"))
                try:
                    self._execute_render(
                        frame,
                        render_plan,
                        generation_id,
                        skip_cpu_fallback_on_gpu_miss=skip_cpu_fallback_on_gpu_miss,
                        source_decisions_by_clip=source_decisions_by_clip,
                        task_meta=task_meta,
                    )
                finally:
                    self._active_decode_target_size = None
                    self._current_task_background = False

                self._queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Render worker error: {e}")
                self.render_error.emit(-1, str(e))

        logger.info(f"{self.objectName()} stopped")

    def _pending_head_priority(self) -> Optional["FrameTaskPriority"]:
        """窥视队头 pending 任务的协议优先级（P1-02 满队列裁决用）。

        持 queue.mutex 做纯读（一次获取，不嵌套调用任何 Queue 方法，
        不会触发 mutex 重入死锁），不改变 unfinished_tasks / 不消费
        元素、不需要 not_empty 通知——避免 pop+回插方案在并发消费下的
        会计竞态。空队列或异常返回 None（调用方退回无条件替换）。
        """
        from src.features.edit.core.realtime_playback.task_priority import (
            resolve_request_priority,
        )
        try:
            with self._queue.mutex:
                if not self._queue.queue:
                    return None
                head = self._queue.queue[0]
            head_meta = head[6] if isinstance(head, tuple) and len(head) >= 7 else {}
            return resolve_request_priority(
                head_meta.get("request_kind", ""),
                background=bool(head_meta.get("background")),
            )
        except Exception as peek_error:
            logger.debug(f"priority peek failed: {peek_error}")
            return None

    def enqueue(
        self,
        frame: int,
        render_plan: FrameRenderPlan,
        generation_id: Optional[int] = None,
        *,
        skip_cpu_fallback_on_gpu_miss: bool = False,
        source_decisions_by_clip: Optional[Dict[str, Any]] = None,
        decode_target_size: Optional[Tuple[int, int]] = None,
        request_kind: str = "",
        scrub_request_token: Optional[int] = None,
        request_started_at: Optional[float] = None,
        scrub_settled: bool = False,
        scrub_coarse: bool = False,
        trace_id: int = 0,
        deadline_ns: int = 0,
        dispatch_channel: str = "",
    ) -> bool:
        """
        加入渲染任务。

        Args:
            frame: 帧号
            render_plan: 帧渲染计划
            generation_id: TimelinePreviewRenderer generation ID
            skip_cpu_fallback_on_gpu_miss: GPU miss 时是否禁止 CPU 回退（realtime）
            source_decisions_by_clip: Final-2 engine source decisions，
                {clip_id -> SourceDecision}。worker._try_gpu_render 会把它传给
                EditGpuDecodeManager.get_frame_with_policy()，拒绝 realtime master。
            decode_target_size: Optional per-request decode size. Scrub uses
                720p while ordinary edit playback stays capped at 1080p.
            request_kind: Optional task kind for interactive telemetry.
            scrub_request_token: Latest-only scrub token, when request_kind=scrub.
            request_started_at: Monotonic input time for latency tracing.
            scrub_settled: True for a delayed repair pass after the playhead stops.

        Returns:
            True if task was queued, False otherwise.
        """
        if generation_id is None:
            generation_id = self._generation_id

        # Final-2: 把 source decisions 装进 task tuple（即使是空 dict）。
        # 这样 _try_gpu_render 一定能拿到一个 dict（None 兼容为空）。
        decisions_map: Dict[str, Any] = source_decisions_by_clip or {}
        task_meta: Dict[str, Any] = {
            "request_kind": str(request_kind or ""),
            "frame": int(frame),
            # P1-01: 后台回填的通道标注（优先级已在 priority 覆盖）。
            **({"dispatch_channel": "background"}
               if str(request_kind or "") == "idle_prebuffer" else {}),
        }
        if trace_id:
            # P0-01: engine 分配的统一 trace id 随 task_meta 传给 worker。
            task_meta["trace_id"] = int(trace_id)
        if deadline_ns:
            # P0-02/P1-06: 单帧 deadline 随 task_meta 传入帧 trace（分段
            # 预算归因在 collector.record 统一评估）。
            task_meta["deadline_ns"] = int(deadline_ns)
        if dispatch_channel:
            # P1-01: 路由通道正字段（engine/scrub/background/direct/refresh）。
            task_meta["dispatch_channel"] = str(dispatch_channel)
        if scrub_request_token is not None:
            task_meta["scrub_request_token"] = int(scrub_request_token)
        if request_started_at is not None:
            task_meta["request_started_at"] = float(request_started_at)
        if scrub_settled:
            task_meta["scrub_settled"] = True
        if scrub_coarse:
            task_meta["scrub_coarse"] = True

        # P1-02: 显式任务优先级协议（scrub_latest > playhead > prefetch >
        # cache_fill > thumbnail > export）。优先级随 task_meta 全链路携带
        # （trace/HUD 可见），满队列时按协议裁决而不是无条件替换。
        from src.features.edit.core.realtime_playback.task_priority import (
            eviction_decision,
            priority_name,
            resolve_request_priority,
        )
        incoming_priority = resolve_request_priority(
            task_meta.get("request_kind", ""),
            background=bool(task_meta.get("background")),
        )
        task_meta["priority"] = incoming_priority.name.lower()

        # 预览播放以最新帧为准。队列里旧任务还没开始就直接丢弃，
        # 避免播放时堆满队列并显示过期画面。
        try:
            dropped = 0
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    dropped += 1
                except queue.Empty:
                    break

            if dropped:
                logger.debug(f"Coalesced {dropped} stale render request(s)")

            self._queue.put_nowait((
                frame,
                render_plan,
                generation_id,
                skip_cpu_fallback_on_gpu_miss,
                decisions_map,
                decode_target_size,
                task_meta,
            ))
            playback_trace(
                "worker.enqueue",
                frame=frame,
                generation=generation_id,
                dropped=dropped,
                queue_size=self._queue.qsize(),
                skip_cpu_fallback=skip_cpu_fallback_on_gpu_miss,
                request_kind=task_meta.get("request_kind", ""),
                scrub_token=task_meta.get("scrub_request_token", ""),
                scrub_settled=task_meta.get("scrub_settled", False),
                scrub_coarse=task_meta.get("scrub_coarse", False),
            )
            return True
        except queue.Full:
            # P1-02: 满队列按优先级协议裁决。队头即最新 pending（交互入队
            # 与本裁决同在主线程串行），窥视其优先级：
            # - incoming 更优先或同级 → latest wins，替换；
            # - incoming 更不优先 → 丢弃 incoming，绝不挤掉高优先级工作。
            pending_priority = self._pending_head_priority()

            if pending_priority is not None:
                verdict = eviction_decision(incoming_priority, pending_priority)
                if verdict.action == "drop_incoming":
                    playback_trace(
                        "worker.enqueue_drop_low_priority",
                        frame=frame,
                        generation=generation_id,
                        priority=task_meta.get("priority", ""),
                        pending_priority=priority_name(pending_priority),
                        reason=verdict.reason,
                    )
                    return False
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                self._queue.put_nowait((
                    frame,
                    render_plan,
                    generation_id,
                    skip_cpu_fallback_on_gpu_miss,
                    decisions_map,
                    decode_target_size,
                    task_meta,
                ))
                playback_trace(
                    "worker.enqueue_replace_full",
                    frame=frame,
                    generation=generation_id,
                    queue_size=self._queue.qsize(),
                    skip_cpu_fallback=skip_cpu_fallback_on_gpu_miss,
                    request_kind=task_meta.get("request_kind", ""),
                    scrub_token=task_meta.get("scrub_request_token", ""),
                    scrub_settled=task_meta.get("scrub_settled", False),
                    priority=task_meta.get("priority", ""),
                    pending_priority=(
                        priority_name(pending_priority)
                        if pending_priority is not None else ""
                    ),
                )
                logger.debug(f"Replaced stale render request with frame {frame}")
                return True
            except queue.Empty:
                playback_trace("worker.enqueue_full_empty_race", frame=frame, generation=generation_id)
                logger.debug(f"Render queue full while empty check raced, dropping frame {frame}")
            except queue.Full:
                playback_trace("worker.enqueue_drop_full", frame=frame, generation=generation_id)
                logger.debug(f"Render worker busy, dropping stale preview frame {frame}")
            return False

    def enqueue_background_if_idle(
        self,
        frame: int,
        render_plan: FrameRenderPlan,
        generation_id: int,
        *,
        decode_target_size: Optional[Tuple[int, int]] = None,
    ) -> bool:
        """Queue an idle-prebuffer cache-fill task only when nothing waits.

        后台任务只在 worker 空闲时入队，永远不可能挤掉一个已排队的交互
        任务。反过来，交互 enqueue 的 latest-only 丢弃规则会自动淘汰仍在
        排队的后台任务。
        """
        if self._stop_flag:
            return False
        task_meta: Dict[str, Any] = {
            "request_kind": "idle_prebuffer",
            "frame": int(frame),
            "background": True,
            # P1-02: 后台缓存回填 = cache_fill 优先级（可被任何交互任务淘汰）。
            "priority": "cache_fill",
        }
        task = (
            frame,
            render_plan,
            generation_id,
            False,
            {},
            decode_target_size,
            task_meta,
        )
        # 调用方只有 Qt 主线程（idle-prebuffer tick 与交互 enqueue 都在
        # 主线程串行执行），因此 empty()→put_nowait() 之间不存在竞争；
        # 不能持有 queue.mutex 做原子判断——Queue.mutex 是普通 Lock，
        # empty()/put_nowait() 内部重入获取会死锁（测试复现）。
        try:
            if not self._queue.empty():
                return False
            self._queue.put_nowait(task)
            playback_trace(
                "worker.enqueue_background",
                frame=frame,
                generation=generation_id,
            )
            return True
        except queue.Full:
            return False

    def clear_pending_requests(self) -> int:
        """Drop queued render requests that have not started yet."""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                dropped += 1
            except queue.Empty:
                break
        if dropped:
            playback_trace("worker.clear_pending_requests", dropped=dropped)
        return dropped

    def set_interactive_scrub_state(self, token: int, frame: int, active: bool) -> None:
        """Publish the latest scrub request so stale worker tasks can self-drop."""
        with self._interactive_scrub_lock:
            self._interactive_scrub_active = bool(active)
            self._interactive_scrub_token = int(token)
            self._interactive_scrub_frame = int(frame)

    def _is_stale_interactive_scrub_task(
        self,
        task_meta: Optional[Dict[str, Any]],
        *,
        min_age_ms: float = 0.0,
    ) -> bool:
        """Return whether a scrub task has already been superseded by the UI."""
        if not task_meta or task_meta.get("request_kind") != "scrub":
            return False
        token = task_meta.get("scrub_request_token")
        if token is None:
            return False
        request_started_at = task_meta.get("request_started_at")
        if min_age_ms > 0.0 and isinstance(request_started_at, (int, float)):
            age_ms = (time.perf_counter() - float(request_started_at)) * 1000.0
            if age_ms < float(min_age_ms):
                return False
        with self._interactive_scrub_lock:
            return (
                self._interactive_scrub_active
                and int(token) != int(self._interactive_scrub_token)
            )

    def _is_abandoned_interactive_scrub_task(
        self,
        frame: int,
        task_meta: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Return whether a scrub render target is far from the latest playhead.

        Scrub decodes must NOT be aborted just because a newer scrub request
        arrived: a mostly-completed decode is still the freshest possible
        viewer update, and killing it mid-GOP-walk throws away nearly all the
        work (observed as a frozen viewer during fast drags). Abandon a task
        only when the playhead has moved far past its target, where finishing
        the decode would be wasted effort on a position nobody is looking at.
        """
        if not task_meta or task_meta.get("request_kind") != "scrub":
            return False
        with self._interactive_scrub_lock:
            if not self._interactive_scrub_active:
                return False
            latest = int(self._interactive_scrub_frame)
        return abs(int(frame) - latest) > self._SCRUB_ABANDON_DISTANCE_FRAMES

    def _interactive_scrub_deadline_at(
        self,
        *,
        asset_path: str,
        source_frame: int,
        target_size: Optional[Tuple[int, int]],
        stream_key: Optional[str],
        task_meta: Optional[Dict[str, Any]],
        timeline_frame: Optional[int] = None,
    ) -> float:
        """Return an absolute deadline for the current latest scrub decode."""
        deadline_ms = float(self._interactive_scrub_decode_deadline_ms)
        estimate_deadline = getattr(
            self._gpu_decode_manager,
            "estimate_interactive_decode_deadline_ms",
            None,
        )
        if callable(estimate_deadline):
            try:
                estimated = estimate_deadline(
                    asset_path,
                    source_frame,
                    target_size=target_size,
                    stream_key=stream_key,
                )
                deadline_ms = float(estimated)
            except Exception as e:
                playback_trace(
                    "worker.scrub_deadline_estimate_failed",
                    frame=source_frame,
                    stream_key=stream_key,
                    error=e,
                )

        now = time.perf_counter()
        request_started_at = (task_meta or {}).get("request_started_at")
        queue_elapsed_ms = None
        if isinstance(request_started_at, (int, float)):
            queue_elapsed_ms = max(0.0, (now - float(request_started_at)) * 1000.0)
        scrub_settled = bool((task_meta or {}).get("scrub_settled"))
        if scrub_settled:
            deadline_ms = min(220.0, max(120.0, deadline_ms * 1.75))
        else:
            # Only shrink the budget when the playhead is far past this
            # target. A merely-superseded task still deserves its full
            # deadline so its nearly-complete decode can be displayed.
            abandoned_frame = (
                int(timeline_frame)
                if timeline_frame is not None
                else (task_meta or {}).get("frame")
            )
            if abandoned_frame is not None and self._is_abandoned_interactive_scrub_task(
                int(abandoned_frame), task_meta
            ):
                deadline_ms = max(1.0, deadline_ms - float(queue_elapsed_ms or 0.0))
        playback_trace(
            "worker.scrub_interactive_deadline",
            frame=source_frame,
            stream_key=stream_key,
            deadline_ms=deadline_ms,
            queue_elapsed_ms=queue_elapsed_ms,
            scrub_settled=scrub_settled,
            stale_grace_ms=self._interactive_scrub_stale_grace_ms,
        )
        return now + max(1.0, deadline_ms) / 1000.0

    def _emit_interactive_scrub_drop(
        self,
        frame: int,
        generation_id: int,
        task_meta: Optional[Dict[str, Any]],
        reason: str,
    ) -> None:
        """Notify the renderer that an obsolete scrub task should unblock latest."""
        playback_trace(
            "worker.scrub_stale_drop",
            frame=frame,
            generation=generation_id,
            reason=reason,
            task_token=(task_meta or {}).get("scrub_request_token", ""),
        )
        if generation_id == self._generation_id:
            self.frame_rendered.emit(
                int(frame),
                {
                    "scrub_stale_drop": True,
                    "reason": reason,
                    "scrub_request_token": (task_meta or {}).get("scrub_request_token"),
                },
            )

    def set_gpu_submitted_callback(
        self,
        callback: Optional[Callable[[int, int], None]],
    ) -> None:
        """Set a lightweight callback invoked when scrub work reaches the GPU queue."""
        self._gpu_submitted_callback = callback

    def _notify_gpu_submitted_direct(self, frame: int, generation_id: int) -> None:
        """Release scrub decode pressure before the queued Qt signal reaches UI."""
        callback = self._gpu_submitted_callback
        if callback is None:
            return
        try:
            callback(int(frame), int(generation_id))
        except Exception as e:
            playback_trace(
                "worker.gpu_submitted_callback_error",
                frame=frame,
                generation=generation_id,
                error=str(e),
            )

    def _teardown_comp_gl_resources(self) -> None:
        """AUD-18909：销毁 comp_clip 共享 GL context 与 offscreen surface。

        此前 setup_comp_gl_context 创建的 QOpenGLContext/QOffscreenSurface
        没有任何销毁路径——每次 renderer 生命周期（工程打开/页面重建）
        泄漏一个共享 GL context + offscreen surface（驱动侧 GL 对象），
        长会话多工程切换下累积。

        线程契约（与 stop() 的解码资源销毁相同）：
        - 必须在渲染线程退出**之后**调用（context 已无 current，不会再
          被 makeCurrent）；
        - QOffscreenSurface 在 GUI 线程创建，按 Qt 约定回 GUI 线程
          destroy()——stop()/本方法都由 UI 线程调用，满足要求；
        - 幂等：重复调用无副作用。
        """
        context = getattr(self, "_comp_gl_context", None)
        if context is not None:
            if getattr(self, "_comp_gl_context_current", False):
                try:
                    context.doneCurrent()
                except Exception as e:
                    logger.debug(f"PreviewRenderWorker: comp GL doneCurrent failed: {e}")
                self._comp_gl_context_current = False
            self._comp_gl_context = None
        surface = getattr(self, "_comp_gl_surface", None)
        if surface is not None:
            try:
                surface.destroy()
            except Exception as e:
                logger.debug(f"PreviewRenderWorker: comp GL surface destroy failed: {e}")
            self._comp_gl_surface = None
        self._comp_gl_ready = False
        self._comp_gpu_direct_enabled = False

    def _teardown_decode_resources(self) -> None:
        """销毁共享解码资源（幂等；仅在线程退出后调用，见 stop）。"""
        if getattr(self, "_decode_resources_torn_down", False):
            return
        self._decode_resources_torn_down = True
        # 清理 GPU 解码管理器
        if self._gpu_decode_manager is not None:
            try:
                self._gpu_decode_manager.close_all()
            except Exception as e:
                logger.warning(f"Failed to close GPU decode manager: {e}")
        self._clear_direct_preview_sources()
        # 清理 CompositeFrameRenderer
        if self._comp_frame_renderer is not None:
            try:
                self._comp_frame_renderer.cleanup()
            except Exception as e:
                logger.warning(f"Failed to cleanup comp frame renderer: {e}")
            self._comp_frame_renderer = None
        # AUD-18909：comp_clip 共享 GL context/surface 同样只在渲染线程
        # 退出后销毁——复用本方法作为统一的"线程退出后资源销毁"钩子。
        self._teardown_comp_gl_resources()

    def stop(self) -> None:
        """停止渲染线程"""
        self._gpu_submitted_callback = None
        self._stop_flag = True
        # 清空队列
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        # 等待渲染线程退出后再销毁解码资源：在飞的 _execute_render 可能
        # 正持有 VT 会话在 C 层解码，不等线程退出就 close_all() 会销毁
        # 正在使用的会话 → SIGSEGV（真实复现：暂停 seek 的后台 refine
        # 解码与 shutdown 并发）。_GuardedPreviewSource 的延迟 close 同理
        # 只保护直连源，解码管理器的 VT 会话必须先等线程。
        timed_out = False
        if self.isRunning():
            if not self.wait(5000):
                timed_out = True
                logger.warning(
                    "PreviewRenderWorker.stop: render thread did not exit "
                    "within 5s; deferring shared decode teardown until the "
                    "thread finishes (AUD-18902)"
                )
        # 层解码线程池先于解码管理器关闭。shutdown 必须等待在飞 future：
        # wait=False 会放任解码线程继续持有 VT 源，与随后的 close_all()
        # 并发（SIGSEGV 窗口）。cancel_futures 已取消排队未开始的任务。
        try:
            self._decode_executor.shutdown(wait=True, cancel_futures=True)
        except Exception as e:
            logger.warning(f"Failed to stop preview decode pool: {e}")
        if timed_out:
            # AUD-18902：线程尚未退出，绝不能现在销毁共享解码资源
            # （解码管理器的 VT 会话可能正被在飞渲染使用）。把销毁推迟到
            # 线程真正退出之后——finished 以本 worker（QObject，UI 线程）
            # 为接收者，queued 到 UI 事件循环执行；线程在 stop 与 connect
            # 之间退出时 finished 已发过，直接补一次（幂等保护防双跑）。
            try:
                self.finished.connect(self._teardown_decode_resources)
            except (RuntimeError, TypeError) as e:
                logger.warning(
                    f"PreviewRenderWorker.stop: defer connect failed: {e}"
                )
            if not self.isRunning():
                self._teardown_decode_resources()
            return
        self._teardown_decode_resources()

    def set_viewport_size(self, size: Tuple[int, int]) -> None:
        """设置视口尺寸"""
        self._viewport_size = size
        self._clear_direct_preview_sources()

    def set_preview_quality(self, scale: float) -> None:
        """设置预览代理分辨率缩放比例"""
        scale = float(scale)
        self._preview_quality = 0.0 if scale <= 0.0 else max(0.125, min(1.0, scale))
        self._clear_direct_preview_sources()

    def set_no_deblock(self, enabled: bool) -> None:
        """预览跳过输出去块：透传解码管理器（并使其 source 失效）。"""
        self._no_deblock = bool(enabled)
        manager = getattr(self, '_gpu_decode_manager', None)
        apply_fn = getattr(manager, 'set_no_deblock', None)
        if callable(apply_fn):
            try:
                apply_fn(bool(enabled))
            except Exception:
                logger.debug("no_deblock apply failed", exc_info=True)
        self._clear_direct_preview_sources()

    def _get_sequence_resolution(self) -> Tuple[int, int]:
        """Get sequence native resolution; fallback to viewport if unknown."""
        seq = self._timeline_sequence
        seq_w = int(getattr(seq, "width", 0))
        seq_h = int(getattr(seq, "height", 0))
        if seq_w > 0 and seq_h > 0:
            return (seq_w, seq_h)
        return self._viewport_size

    @property
    def _decode_target_size(self) -> Tuple[int, int]:
        """Decode target size based on sequence resolution, scaled by preview quality."""
        active = self._active_decode_target_size
        if active is not None:
            return active
        base_w, base_h = self._get_sequence_resolution()
        s = self._effective_preview_quality_scale(base_w, base_h)
        if s >= 1.0:
            return (base_w, base_h)
        dw = max(64, int(round(base_w * s)))
        dh = max(64, int(round(base_h * s)))
        if dw % 2:
            dw += 1
        if dh % 2:
            dh += 1
        return (dw, dh)

    def _effective_preview_quality_scale(self, base_w: int, base_h: int) -> float:
        explicit = float(getattr(self, "_preview_quality", 0.0))
        longest = max(1, int(base_w), int(base_h))
        max_edge = max(64, int(getattr(self, "_max_preview_edge", 3840)))
        if explicit > 0.0:
            # Explicit choices are relative to the sequence resolution.
            # Full quality is capped at the 4K preview ceiling only for 6K/8K
            # timelines; it must always be at least as large as Smooth.
            return max(0.125, min(1.0, explicit, max_edge / longest))

        # Smooth mode targets 1920x1080 for high-resolution material while
        # preserving the native size of low-resolution material.
        smooth_edge = min(max_edge, _SMOOTH_PREVIEW_EDGE)
        return max(0.125, min(1.0, smooth_edge / longest))

    def set_generation_id(self, gen_id: int) -> None:
        """设置 generation ID（用于判断任务是否过期）"""
        self._generation_id = gen_id

    # ================================================================
    # 内部方法
    # ================================================================

    def _execute_render(
        self,
        frame: int,
        render_plan: FrameRenderPlan,
        generation_id: int,
        *,
        skip_cpu_fallback_on_gpu_miss: bool = False,
        source_decisions_by_clip: Optional[Dict[str, Any]] = None,
        task_meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        执行渲染任务。

        GPU 优先路径：尝试通过 EditGpuDecodeManager 获取 GpuDecodedFrame，
        提交到 EditGpuPreviewRenderer 进行 GPU 合成和显示。
        如果 GPU 路径不可用或失败，回退到 CPU 路径。

        Args:
            frame: 帧号
            render_plan: 帧渲染计划
            generation_id: Renderer generation for this task
            skip_cpu_fallback_on_gpu_miss: GPU miss 时是否禁止 CPU 回退
            source_decisions_by_clip: Final-2 engine source decisions
                {clip_id -> SourceDecision}，传给 _try_gpu_render。
        """
        from src.shared.video.playback_frame_trace import (
            EditPlaybackFrameTrace,
        )

        # Stage C2: 标记当前 render 是否处于实时播放（用于禁止 master fallback）。
        # skip_cpu_fallback_on_gpu_miss 由上层根据 playback controller 是否 playing
        # 计算，等价于"实时播放热路径"，复用它避免新增独立状态通道。
        self._current_render_realtime = bool(skip_cpu_fallback_on_gpu_miss)

        trace = EditPlaybackFrameTrace(
            frame_index=frame,
            page='edit_timeline',
            generation_id=generation_id,
            # P0-PREV-GPU-T 基线分桶：转场帧/非转场帧分开统计
            transition_layers=len(getattr(render_plan, 'transitions', ()) or ()),
        )
        task_meta = task_meta or {}
        if task_meta.get("request_kind"):
            trace.extra["request_kind"] = task_meta.get("request_kind")
        if task_meta.get("scrub_request_token") is not None:
            trace.request_id = int(task_meta.get("scrub_request_token") or 0)
        # P0-01: engine 入口分配的统一 trace id（0 = 非 engine 派发的旁路帧）。
        if task_meta.get("trace_id"):
            trace.trace_id = int(task_meta.get("trace_id") or 0)
        # P0-02/P1-06: 单帧 deadline（collector.record 统一做分段越界归因）。
        if task_meta.get("deadline_ns"):
            trace.deadline_ns = int(task_meta.get("deadline_ns") or 0)
        # P1-01: 路由通道正字段（engine/scrub/background/direct/refresh）。
        if task_meta.get("dispatch_channel"):
            trace.dispatch_channel = str(task_meta["dispatch_channel"])
        # P1-02: 优先级协议词表随帧 trace 落盘（enqueue 已计算并携带）。
        if task_meta.get("priority"):
            trace.priority = str(task_meta["priority"])
        # P0-02: backend 词表（GPU 合成器在场即 opengl，CPU 回退在
        # _render_frame 前的路径上覆盖为 cpu）。
        trace.backend = (
            "opengl"
            if (self._gpu_renderer is not None) else "cpu"
        )
        t0 = time.perf_counter()
        playback_trace(
            "worker.execute_start",
            frame=frame,
            generation=generation_id,
            has_content=render_plan.has_content,
            request_kind=task_meta.get("request_kind", ""),
            scrub_token=task_meta.get("scrub_request_token", ""),
        )

        # 检查是否有内容
        if not render_plan.has_content:
            if generation_id == self._generation_id:
                playback_trace("worker.execute_no_content_emit", frame=frame, generation=generation_id)
                if not self._current_task_background:
                    self.frame_rendered.emit(frame, None)
            return

        try:
            layers = render_plan.layers
            transitions = render_plan.transitions
            original_layer_count = len(layers)
            layers = self._prune_occluded_layers_for_preview(layers, transitions, int(frame))
            if len(layers) != original_layer_count:
                playback_trace(
                    "worker.preview_occluded_layers_pruned",
                    frame=frame,
                    original_layers=original_layer_count,
                    layers=len(layers),
                )

            if not layers:
                if generation_id == self._generation_id:
                    playback_trace("worker.execute_no_layers_emit", frame=frame, generation=generation_id)
                    if not self._current_task_background:
                        self.frame_rendered.emit(frame, None)
                return

            # Detect missing source files before either the GPU or CPU path.
            # This keeps the diagnosis stable even when decoding fails deep in
            # a backend and lets the UI show a dedicated Media Offline state.
            offline_media = self._offline_media_for_layers(layers)
            if (
                offline_media
                and generation_id == self._generation_id
                and not self._current_task_background
            ):
                self.offline_media.emit(frame, offline_media)

            # ---- GPU 路径 ----
            if self._gpu_decode_manager is not None and self._gpu_renderer is not None:
                playback_trace(
                    "worker.gpu_try",
                    frame=frame,
                    generation=generation_id,
                    layers=len(layers),
                )
                gpu_result, gpu_trace_info = self._try_gpu_render(
                    frame, layers, transitions, generation_id,
                    source_decisions_by_clip=source_decisions_by_clip,
                    task_meta=task_meta,
                )
                if gpu_result:
                    trace.decode_path = gpu_trace_info.get('decode_path', 'gpu')
                    trace.render_path = 'gpu_composite'
                    # A0.1：这里不再把 last_ms（RING_DEPTH-1 帧前的旧 query）
                    # 写进当前帧——旧实现属于"旧 query 冒充当前帧"。本帧的
                    # GPU 时间由 GL 线程 drain_completed_gpu_queries 完成后
                    # 按发起帧 trace_id 回填（见 _on_gpu_frame_ready）；
                    # worker 侧保持 gpu_time_unavailable=True（fail-closed）。
                    trace.total_ms = (time.perf_counter() - t0) * 1000.0
                    trace.decode_ms = float(gpu_trace_info.get('decode_ms', 0.0) or 0.0)
                    trace.decode_layers = int(
                        gpu_trace_info.get('decode_layers', 0) or 0
                    )
                    trace.render_ms = max(
                        0.0, trace.total_ms - trace.decode_ms
                    )
                    self._record_frame_trace(trace)
                    playback_trace(
                        "worker.gpu_submitted_return",
                        frame=frame,
                        generation=generation_id,
                        elapsed_ms=trace.total_ms,
                    )
                    if self._current_task_background:
                        # 后台缓存回填任务：GPU 提交即结束，不驱动交互
                        # 单飞状态机（_notify_gpu_submitted_direct 会释放
                        # pending/in-flight 记账，只有交互任务能调）。
                        return  # GPU 渲染成功，直接返回
                    if task_meta.get("request_kind") == "scrub":
                        self._notify_gpu_submitted_direct(int(frame), int(generation_id))
                        self.gpu_submitted.emit(int(frame), int(generation_id))
                    else:
                        # 播放/暂停步进：GL 任务入队即放行单飞准入，让下一帧
                        # 解码波与本帧 GL 合成重叠（流水线化）。此前要等
                        # GL 完成 + UI 发射才清 _render_in_flight，帧间全串行，
                        # 16 线程 CPU 与核显大部分时间在互相等待。
                        self._notify_gpu_submitted_direct(int(frame), int(generation_id))
                    return  # GPU 渲染成功，直接返回
                else:
                    trace.fallback_reason = gpu_trace_info.get('fallback_reason', 'gpu_render_failed')
                    # 解码波在回退前已经跑完——波次流数与耗时同样进 trace：
                    # 饱和分桶不能恰好丢掉回退帧（队列满 hold_previous 正是
                    # 解码供给超过 GL 消费的 W3 触发信号）。CPU 回退路径的
                    # 重解码耗时则继续由 total_ms 承载。
                    trace.decode_layers = int(
                        gpu_trace_info.get('decode_layers', 0) or 0
                    )
                    trace.decode_ms = float(
                        gpu_trace_info.get('decode_ms', 0.0) or 0.0
                    )
                    if trace.fallback_reason == 'interactive_scrub_stale':
                        trace.render_path = 'scrub_stale_drop'
                        trace.total_ms = (time.perf_counter() - t0) * 1000
                        self._record_frame_trace(trace)
                        self._emit_interactive_scrub_drop(
                            frame,
                            generation_id,
                            task_meta,
                            "stale_after_gpu_prepare",
                        )
                        return
                    playback_trace(
                        "worker.gpu_fallback",
                        frame=frame,
                        generation=generation_id,
                        reason=trace.fallback_reason,
                    )
                    if self._should_hold_previous_on_gpu_fallback(
                        skip_cpu_fallback_on_gpu_miss,
                        trace.fallback_reason,
                    ):
                        trace.decode_path = gpu_trace_info.get('decode_path', 'gpu')
                        trace.render_path = 'hold_previous_frame'
                        trace.total_ms = (time.perf_counter() - t0) * 1000
                        self._record_frame_trace(trace)
                        playback_trace(
                            "worker.realtime_hold_previous_on_gpu_fallback",
                            frame=frame,
                            generation=generation_id,
                            reason=trace.fallback_reason,
                            elapsed_ms=trace.total_ms,
                        )
                        if generation_id == self._generation_id and not self._current_task_background:
                            self.frame_rendered.emit(frame, None)
                        return

            if self._is_abandoned_interactive_scrub_task(frame, task_meta):
                trace.fallback_reason = 'interactive_scrub_stale'
                trace.render_path = 'scrub_stale_drop'
                trace.total_ms = (time.perf_counter() - t0) * 1000
                self._record_frame_trace(trace)
                self._emit_interactive_scrub_drop(
                    frame,
                    generation_id,
                    task_meta,
                    "abandoned_far_before_cpu_fallback",
                )
                return

            # ---- CPU 回退路径 ----
            # Batch 10 (缺陷计划 RP-P0-10): realtime CPU fallback 必须可观测。
            # 记录 realtime 模式下进入 CPU 合成的次数（telemetry gate，
            # 验收 realtime cpu_qimage_frames == 0）。
            if self._current_render_realtime and getattr(self, '_playback_session_emitted', False):
                self._realtime_cpu_fallback_count = (
                    getattr(self, '_realtime_cpu_fallback_count', 0) + 1
                )
                playback_trace(
                    "worker.realtime_cpu_fallback",
                    frame=frame,
                    generation=generation_id,
                    fallback_reason=trace.fallback_reason,
                    realtime_cpu_count=self._realtime_cpu_fallback_count,
                )
            if not trace.fallback_reason:
                trace.fallback_reason = 'no_gpu_pipeline'
            trace.decode_path = trace.decode_path or 'cpu_packed_rgb'
            trace.render_path = 'cpu_qimage_upload'

            # Clear per-frame composite error state before compositing
            self._last_comp_error = None

            direct_qimage = self._try_render_single_direct_preview_qimage(
                layers,
                transitions,
                frame,
            )
            if direct_qimage is not None:
                if generation_id != self._generation_id:
                    playback_trace(
                        "worker.direct_single_layer_drop_generation",
                        frame=frame,
                        task_generation=generation_id,
                        worker_generation=self._generation_id,
                    )
                    return

                trace.total_ms = (time.perf_counter() - t0) * 1000
                trace.extra["fast_path"] = "direct_single_layer"
                self._record_frame_trace(trace)
                playback_trace(
                    "worker.direct_single_layer_qimage_emit",
                    frame=frame,
                    generation=generation_id,
                    elapsed_ms=trace.total_ms,
                )
                self._render_frame_qimage(frame, direct_qimage, generation_id)
                return

            composite_frame = self._composite_layers(layers, transitions, frame)

            if generation_id != self._generation_id:
                playback_trace(
                    "worker.cpu_drop_generation_after_composite",
                    frame=frame,
                    task_generation=generation_id,
                    worker_generation=self._generation_id,
                )
                return

            if composite_frame is None:
                # R-8: If a composite clip failed, emit a structured error
                # with the failure policy context so the viewer can show the
                # appropriate overlay instead of a generic message.
                if self._last_comp_error:
                    from src.features.edit.core.composite_failure_policy import CompositeFailurePolicy
                    policy = CompositeFailurePolicy.for_edit_preview(self._last_comp_error)
                    if generation_id == self._generation_id and not self._current_task_background:
                        self.render_error.emit(
                            frame,
                            f"composite_error:{policy['message']}:{policy['show_ghost']}",
                        )
                elif generation_id == self._generation_id and not self._current_task_background:
                    self.render_error.emit(frame, "Failed to composite layers")
                trace.fallback_reason = 'composite_failed'
                trace.total_ms = (time.perf_counter() - t0) * 1000
                self._record_frame_trace(trace)
                playback_trace(
                    "worker.cpu_composite_failed",
                    frame=frame,
                    generation=generation_id,
                    elapsed_ms=trace.total_ms,
                )
                return

            trace.cpu_to_ndarray = True
            trace.qimage_copy = True
            trace.gl_tex_image_upload = True
            # A0.1：CPU 回退合成归类为独立回读分类（READBACK_KINDS 词表；
            # 普通预览 GPU 路径不应出现该值）。
            trace.readback_kind = "cpu_fallback"
            # P0-02: 实际走了 CPU 回退合成，backend 覆盖为 cpu。
            trace.backend = "cpu"
            # P0-04: CPU 回退路径的整帧 CPU→GPU 上传字节（本帧归因 + 累计审计）。
            try:
                _fallback_bytes = int(
                    composite_frame.sizeInBytes()
                    if hasattr(composite_frame, 'sizeInBytes')
                    else composite_frame.nbytes
                )
                trace.cpu_upload_bytes = _fallback_bytes
                trace.texture_write_bytes = _fallback_bytes
                from src.shared.video.pixel_copy_counters import (
                    CopyKind,
                    add_pixel_copy,
                )
                add_pixel_copy(CopyKind.CPU_UPLOAD, _fallback_bytes)
                add_pixel_copy(CopyKind.TEXTURE_WRITE, _fallback_bytes)
            except Exception:
                pass

            # 渲染帧
            self._render_frame(frame, composite_frame, generation_id)

            trace.total_ms = (time.perf_counter() - t0) * 1000
            self._record_frame_trace(trace)
            playback_trace(
                "worker.cpu_render_done",
                frame=frame,
                generation=generation_id,
                elapsed_ms=trace.total_ms,
            )

        except Exception as e:
            logger.error(f"Render execution error for frame {frame}: {e}")
            playback_trace("worker.execute_error", frame=frame, generation=generation_id, error=e)
            trace.fallback_reason = f'render_error: {e}'
            trace.total_ms = (time.perf_counter() - t0) * 1000
            self._record_frame_trace(trace)
            if generation_id == self._generation_id and not self._current_task_background:
                self.render_error.emit(frame, str(e))

    def _offline_media_for_layers(
        self,
        layers: Tuple[ClipLayerInfo, ...],
    ) -> list[dict[str, str]]:
        """Return visible project assets whose source path is unavailable."""
        project = getattr(self._asset_resolver, "_project", None)
        if project is None:
            return []

        offline: dict[str, dict[str, str]] = {}
        for layer in layers:
            asset_id = str(getattr(layer, "asset_id", "") or "")
            if not asset_id or asset_id in offline:
                continue
            asset = project.get_asset_by_id(asset_id)
            if asset is None or getattr(asset, "is_virtual", False):
                continue
            if getattr(asset, "is_comp_clip", False):
                continue
            path = str(getattr(asset, "path", "") or "")
            # The path is the source of truth here.  ``is_offline`` is a
            # project-level snapshot that may lag behind a file being
            # restored/relinked while the project remains open.
            if not path or os.path.exists(path):
                continue
            offline[asset_id] = {
                "asset_id": asset_id,
                "name": str(getattr(asset, "display_name", "") or path),
                "path": path,
            }
        return list(offline.values())

    def _should_hold_previous_on_gpu_fallback(
        self,
        enabled: bool,
        fallback_reason: Optional[str],
    ) -> bool:
        """Return whether realtime playback should avoid the slow CPU fallback."""
        if not enabled:
            return False
        if fallback_reason is None:
            return False
        return fallback_reason in _REALTIME_HOLD_ON_GPU_FALLBACK_REASONS

    @staticmethod
    def _build_gpu_transition_jobs(
        transitions: Tuple[TransitionBlendInfo, ...],
        viewport_size: Tuple[int, int],
    ) -> Optional[Tuple[tuple, ...]]:
        """为转场帧构造 GPU 任务；存在不支持的转场时返回 None（整帧回退）。

        所有内置转场都只携带 pixel-free ``GpuTransitionMaskSpec`` 控制块：
        几何 mask、运动模糊采样和 full-frame dissolve 全部由 GPU shader
        按像素计算。``dip_to_color`` 仍由合成域颜色 pass 处理，
        ``cross_dissolve`` 只需 opacity 重写。
        """
        try:
            from src.features.edit.core.transition_mask_gpu import (
                build_gpu_transition_mask_spec,
            )
        except ImportError:
            return None

        jobs: list[tuple] = []
        for trans in transitions:
            transition_type = trans.transition_type
            if transition_type == "cross_dissolve":
                continue
            if transition_type == "dip_to_color":
                jobs.append((trans, None))
                continue
            # AUD-19202 注：base 为 dip_to_color 的用户自定义转场走下面的
            # build_gpu_transition_mask_spec → 解析到 dip base 返回 None →
            # 整帧回退 CPU——CPU 路径已正确渲染 dip（颜色 pass 继承 base）。
            # GPU 直渲需 GPU 合成器的 dip_jobs 过滤器同样解析用户 base
            # （edit_gpu_preview_renderer.py，不在本批白名单）。
            try:
                spec = build_gpu_transition_mask_spec(
                    transition_type,
                    float(trans.blend_factor),
                    getattr(trans.transition, "params", None),
                )
            except Exception:
                return None
            if spec is None:
                return None
            jobs.append((trans, spec))
        return tuple(jobs)

    def _retime_interp_requires_cpu(
        self, layers: Tuple[ClipLayerInfo, ...], frame: int,
    ) -> Optional[str]:
        """F2.8：变速插值 GPU 门控——返回 fallback_reason 或 None。

        - frame_blend 且本帧混合权重 > 0：GPU 合成器不实现帧混合，整帧
          回退 CPU（此前静默按 nearest 显示，违反"预览/成片一致"验收）。
        - optical_flow：全渲染端未实现（模型枚举而已），显式留警告后按
          nearest 继续——不得静默降级（计划 F2.8 原文）。
        - 冻结帧无混合，恒速 nearest 恒通过。
        """
        from src.features.edit.core.clip_params import SpeedParams
        for layer in layers:
            clip = layer.clip
            if getattr(clip, "freeze_source_frame", None) is not None:
                continue  # 冻结帧无混合
            speed = getattr(clip, "speed", None)
            interp = speed.interpolation if isinstance(speed, SpeedParams) \
                else "nearest"
            if interp == "frame_blend":
                try:
                    _a, _b, weight = clip.timeline_to_source_frame_pair(frame)
                except (ValueError, ZeroDivisionError):
                    weight = 0.0
                if weight and weight > 0.0:
                    return "frame_blend_retime"
            elif interp == "optical_flow":
                warned = getattr(self, "_optical_flow_warned", None)
                if warned is None:
                    warned = set()
                    self._optical_flow_warned = warned
                if clip.clip_id not in warned:
                    warned.add(clip.clip_id)
                    logger.warning(
                        "clip %s: optical_flow 插值未实现，按 nearest 渲染"
                        "（F2.8：不得静默降级，此为显式告知）", clip.clip_id)
        return None

    def _try_gpu_render(
        self,
        frame: int,
        layers: Tuple[ClipLayerInfo, ...],
        transitions: Tuple[TransitionBlendInfo, ...],
        generation_id: int,
        *,
        source_decisions_by_clip: Optional[Dict[str, Any]] = None,
        task_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """
        尝试 GPU 渲染路径。

        为每个 layer 获取解码帧（GPU 零拷贝或 planar YUV），
        如果全部成功则提交到 GPU 渲染器。

        EditGpuDecodeManager.get_frame() 返回 (frame, frame_type):
        - 'vt_pixel_buffer': VT/CVPixelBuffer frame; renderer-thread GL import
        - 'gpu': GpuDecodedFrame (legacy already-imported VT 零拷贝)
        - 'planar': DecodedFrame with planar YUV (ProRes/DNxHR/VP8); CPU
          entropy decode followed by GPU texture upload and YUV conversion
        - 'cpu': DecodedFrame with packed RGB (回退)

        Final-2 (FINAL-P0-02): 当 engine 提供 source_decisions_by_clip 时，
        走 EditGpuDecodeManager.get_frame_with_policy() 路径，让 SourceDecision
        真正约束解码（realtime 禁止 master 解码）。当 SourceDecision
        source_kind=PLACEHOLDER 且 allow_master_fallback=False 时，get_frame_with_policy
        会返回 (None, 'placeholder')，本函数随后返回 False 让 engine 走 hold/rebuffer。

        Returns:
            (success, trace_info) tuple.
        """
        from src.features.edit.core.edit_gpu_preview_renderer import EditRenderTask
        from src.shared.media.gpu_frame import safe_release_gpu_frame

        # F2.8：变速插值 GPU 门控（与导出 GPU 路径同语义，预览/成片一致）
        requires_cpu = self._retime_interp_requires_cpu(layers, frame)
        if requires_cpu:
            return False, {"fallback_reason": requires_cpu}
        for layer in layers:
            graph = self._clip_color_graph(layer)
            if graph is not None:
                from src.features.color.core.color_graph_texture import graph_texture_fallback_reason
                reason = graph_texture_fallback_reason(graph, int(getattr(layer, "source_frame", 0)))
                if reason:
                    return False, {"fallback_reason": reason}

        # P0-PREV-GPU-T：转场帧按能力分流：
        # - cross_dissolve：仅 opacity 重写（合成器原生支持）；
        # - dip_to_color：合成域内全屏颜色 pass；
        # - 空间转场、运动模糊和 full-frame dissolve：GPU procedural pass；
        # - dip_to_color：GPU 合成域颜色 pass；
        # - cross_dissolve：仅 opacity 重写。
        # 任一转场不被 GPU 支持时整帧回退，绝不静默变成普通层混合。
        transition_jobs: tuple = ()
        if transitions:
            transition_jobs = self._build_gpu_transition_jobs(
                transitions, self._viewport_size,
            )
            if transition_jobs is None:
                return False, {
                    "fallback_reason": "native_transition_cpu_compositor",
                }
            transition_supported = getattr(
                self._gpu_renderer, "has_transition_support", None,
            )
            if callable(transition_supported) and not transition_supported():
                return False, {
                    "fallback_reason": "native_transition_cpu_compositor",
                }
            full_frame_jobs = tuple(
                trans for trans, spec in transition_jobs
                if spec is not None and int(getattr(spec, "kind", -1)) == 4
            )
            full_frame_supported = getattr(
                self._gpu_renderer, "has_full_frame_transition_support", None,
            )
            if full_frame_jobs and callable(full_frame_supported):
                if not full_frame_supported():
                    return False, {
                        "fallback_reason": "native_transition_cpu_compositor",
                    }

        # P3.2 复合缓存命中：该帧的全分辨率合成结果已在 GPU 缓存，跳过
        # 整条解码+合成链，提交纯 blit 任务（实时播放不参与）。`is True`
        # 严格比较：Mock 后端（测试替身）返回 truthy Mock 而非 bool，必须
        # 落回真实解码路径。
        has_cached = getattr(self._gpu_renderer, "has_cached_composite", None)
        try:
            realtime_now = bool(self._current_render_realtime)
        except (AttributeError, RuntimeError):
            realtime_now = False
        if (
            callable(has_cached)
            and not realtime_now
            and has_cached(int(frame), int(generation_id), self._viewport_size) is True
        ):
            cache_task = EditRenderTask(
                frame=frame,
                generation_id=generation_id,
                layers=[],
                viewport_size=self._viewport_size,
                output_fbo_size=self._viewport_size,
                coordinate_size=self._get_sequence_resolution(),
                request_kind=str(task_meta.get("request_kind", "") or ""),
                scrub_request_token=(
                    int(task_meta["scrub_request_token"])
                    if task_meta.get("scrub_request_token") is not None
                    else None
                ),
                deadline_ns=int(task_meta.get("deadline_ns", 0) or 0),
                cached_composite=True,
            )
            if self._gpu_renderer.submit(cache_task):
                playback_trace(
                    "worker.composite_cache_submit",
                    frame=frame,
                    generation=generation_id,
                    request_kind=cache_task.request_kind,
                )
                return True, {"decode_path": "composite_cache_blit"}

        def release_decoded_resource(resource: Any) -> None:
            if resource is None:
                return
            release = getattr(resource, "release", None)
            if callable(release):
                try:
                    release()
                    return
                except Exception:
                    pass
            safe_release_gpu_frame(resource)

        def release_layer_data(layer_data: Dict[str, Any]) -> None:
            """Release every GPU resource a decoded layer may hold.

            TL-C01: comp_gpu_handle layers lease a CompositeGpuFrameHandle —
            dropping one without release() leaks the pooled texture forever
            (P0-5 lease contract). Video layers carry gpu_frame /
            vt_pixel_buffer / decoded_frame instead.
            """
            handle = layer_data.get('gpu_handle')
            if handle is not None:
                try:
                    handle.release()
                except Exception:
                    pass
            release_decoded_resource(
                layer_data.get('gpu_frame')
                or layer_data.get('vt_pixel_buffer')
                or layer_data.get('cuda_resident')
                or layer_data.get('decoded_frame')
            )

        trace_info: Dict[str, Any] = {}
        task_meta = task_meta or {}
        layer_jobs: list[tuple[ClipLayerInfo, str, int, Optional[Dict[str, Any]]]] = []
        # Adjustment Clip layers collected separately — their effect chains and
        # sampled Transform/Opacity are applied at their z-position by the
        # scoped compositor. Each entry (T-P1-22 三元组):
        # (ClipLayerInfo, effects_tuple, preview_grade)；grade 为 ColorStack
        # 参数快照或 None，与消费方 edit_gpu_preview_renderer 的
        # _build_scoped_composite_work 契约一致。
        adjustment_layers: list[tuple[ClipLayerInfo, tuple, Optional[Dict[str, Any]]]] = []
        # Final-2: source decisions dict（{clip_id: SourceDecision}），用于 policy-aware decode。
        decisions_map = source_decisions_by_clip or {}
        # P0-PREV-GPU-T：转场帧有效 opacity 映射（与 CPU _composite_layers
        # 2292-2299 同源）：cross_dissolve 的 lerp、dip 的单侧可见性、空间
        # 转场的 base opacity 都由 render_plan 预计算在 TransitionBlendInfo。
        transition_map_by_clip: Dict[str, TransitionBlendInfo] = {}
        for trans in transitions:
            if trans.left_clip_id:
                transition_map_by_clip[trans.left_clip_id] = trans
            if trans.right_clip_id:
                transition_map_by_clip[trans.right_clip_id] = trans

        if self._is_abandoned_interactive_scrub_task(frame, task_meta):
            trace_info['fallback_reason'] = 'interactive_scrub_stale'
            playback_trace(
                "worker.gpu_prepare_scrub_stale",
                frame=frame,
                generation=generation_id,
                scrub_token=task_meta.get("scrub_request_token", ""),
            )
            return False, trace_info

        # FX-C02 / AUD-16808：渲染操作计划（纯数据，effect_render_operations）
        # 是调整剪辑 / solid / 普通层的单一分类权威——本循环按计划输出遍历，
        # effects/grade 快照也由计划构建时经回调一次性采集；GPU 侧
        # _build_scoped_composite_work 与这里的分类不再各持一份判定逻辑。
        # 计划必须基于遮挡裁剪后的 layers（与下方解码/装车一致），因此组装
        # 一个仅承载本循环所需字段的 shim FrameRenderPlan。无效果的恒等
        # 调整剪辑会被计划跳过（GPU 侧对该输入本就是像素级 no-op）。
        # 空 layers 时跳过计划组装（与旧直遍 layers 语义一致）：组装参数
        # 需要 _asset_resolver 等仅完整构造后才存在的属性，部分构造的
        # worker（转场门控单测用 __new__）不应触碰。
        render_ops = (
            build_render_operations(
                FrameRenderPlan(
                    frame=int(frame),
                    layers=layers,
                    transitions=transitions,
                    has_content=True,
                    solo_active=False,
                ),
                self._asset_resolver,
                effects_snapshot=self._get_clip_effects_snapshot,
                # N07-B：调整剪辑 grade 快照豁免 timeline 段（下方 clip
                # 已各自应用过，防 timeline look 双重应用）。
                grade_snapshot=lambda lyr: self._get_preview_color_grade(
                    lyr, exclude_timeline=True),
            )
            if layers
            else ()
        )
        for op in render_ops:
            layer = op.layer
            asset_id = layer.asset_id
            if not asset_id:
                trace_info['fallback_reason'] = 'missing_asset_id'
                playback_trace(
                    "worker.gpu_prepare_failed",
                    frame=frame,
                    generation=generation_id,
                    reason=trace_info['fallback_reason'],
                    gpu_layers=0,
                )
                return False, trace_info

            # Resolve media_type up front — generators (adjustment / solid) are
            # handled specially: adjustment clips never decode (they post-
            # process the composited frame), solid clips skip decoding and are
            # painted procedurally by the GPU renderer.
            asset_type = self._asset_resolver.get_asset_type(asset_id)

            # A normal video clip with a composite binding still needs the
            # legacy bridge until its graph is represented as a comp_clip
            # asset. A real comp_clip must be allowed to reach the
            # capability-gated GPU producer below; ``composite_id`` is how
            # the render plan identifies its graph.
            if getattr(layer, "composite_id", None) and asset_type not in (
                'comp_clip', 'nested_sequence',
            ):
                trace_info['fallback_reason'] = 'composite_bound_clip'
                playback_trace(
                    "worker.gpu_prepare_failed",
                    frame=frame,
                    generation=generation_id,
                    reason=trace_info['fallback_reason'],
                    gpu_layers=0,
                )
                return False, trace_info

            if asset_type == "teaching_animation":
                # The CPU layer path renders the referenced scene from the
                # shared TeachingSceneStore. It is not a decodable media file
                # and must never enter the generic asset-path lookup.
                trace_info['fallback_reason'] = 'teaching_animation_cpu_bridge'
                playback_trace(
                    "worker.gpu_prepare_failed",
                    frame=frame,
                    generation=generation_id,
                    reason=trace_info['fallback_reason'],
                    gpu_layers=0,
                )
                return False, trace_info

            # Adjustment Clip: collect its effect chain for scoped
            # post-composite processing; do NOT add to layer_jobs (it has no
            # source frame). Transform/Opacity are carried by ``layer``.
            # FX-C02 计划（ApplyAdjustment）已携带构建时采集的 effects 与
            # T-P1-22 ColorStack grade 快照。
            if isinstance(op, ApplyAdjustment):
                adjustment_layers.append((layer, op.effects, op.grade))
                continue

            # A ColorStack belongs to the timeline clip, not to the source
            # decoder.  Carry a small immutable snapshot with this render
            # task so the edit GPU compositor can grade the decoded texture
            # before multi-track blending.  Previously the zero-copy path
            # bypassed the stack completely and showed the raw source.
            from src.features.shared.effects.video_stabilization import clip_stabilization_state
            from src.shared.tracking.stabilization_state import stabilization_matrix
            state = clip_stabilization_state(getattr(self._asset_resolver, '_project', None),
                                             getattr(layer, 'clip', None))
            if stabilization_matrix(state, int(layer.source_frame)) is not None:
                # The current timeline GL/Metal geometry path transforms clips
                # before grading. Use the source-space CPU compositor until
                # that path can represent the same full projective ordering.
                trace_info["fallback_reason"] = "video_stabilization_source_space"
                return False, trace_info
            preview_grade = self._get_preview_color_grade(layer)
            # Same pattern for the clip's video filters (blur / sharpen / ...):
            # snapshot the effect chain on the UI thread so the GPU renderer
            # can apply each effect on the per-track texture without touching
            # the live ClipInstance.
            clip_effects = self._get_clip_effects_snapshot(layer)

            # Solid Color: skip decoding — the GPU renderer paints the track
            # FBO with the asset's color. Carry the color + a sentinel path.
            # （is_solid 由 FX-C02 计划分类，与 asset_resolver 判定一致。）
            if op.is_solid:
                solid_asset = self._asset_resolver.get_asset(asset_id)
                solid_rgb = (0.0, 0.6, 0.8)
                gen_params = None
                # AUD-18901：颜色读取与判空必须在同一缩进块内。此前
                # `if sc is not None` 与 `if solid_asset is not None` 同级，
                # 素材解析失败时 sc 未定义（NameError→整帧 render_error），
                # 或沿用上一层 solid 泄漏的旧颜色（第二个 solid 层渲染错色）。
                if solid_asset is not None:
                    sc = getattr(solid_asset, "solid_color", None)
                    if sc is not None:
                        solid_rgb = sc
                    gen_params = getattr(solid_asset, "generator_params", None)
                # Solid 无源帧，关键帧按帧 0 求值（端点保持外推 = 首帧值），
                # 与 CPU/导出路径对 solid 的 source_frame=0 语义一致。
                preview_grade = self._evaluate_preview_grade_at(preview_grade, 0)
                layer_jobs.append((layer, "__solid__", 0, preview_grade, clip_effects, solid_rgb, gen_params))
                continue

            if asset_type in ('comp_clip', 'nested_sequence'):
                # TL-C01: capability-gated comp_clip GPU direct. The comp
                # producer (TL-P1-01) hands out CompositeGpuFrameHandles;
                # when the GPU compositor *declares* comp-gpu support (a
                # strict ``is True`` so test doubles never enable the path)
                # the layer is submitted as a texture layer (fence + lease)
                # instead of dropping the whole frame to the CPU bridge.
                # Without the declared capability the historical CPU
                # fallback holds.
                if asset_type == 'comp_clip' \
                        and self._comp_clip_gpu_direct_supported():
                    layer_jobs.append(
                        (layer, COMP_GPU_JOB_SENTINEL, 0, preview_grade,
                         clip_effects))
                    continue
                trace_info['fallback_reason'] = f'{asset_type}_requires_cpu_bridge'
                playback_trace(
                    "worker.gpu_prepare_failed",
                    frame=frame,
                    generation=generation_id,
                    reason=trace_info['fallback_reason'],
                    gpu_layers=0,
                )
                return False, trace_info
            else:
                # Normal asset path
                asset_path = self._asset_resolver.get_asset_path(asset_id)
                if not asset_path:
                    trace_info['fallback_reason'] = 'missing_asset_path'
                    playback_trace(
                        "worker.gpu_prepare_failed",
                        frame=frame,
                        generation=generation_id,
                        reason=trace_info['fallback_reason'],
                        gpu_layers=0,
                    )
                    return False, trace_info

            # Calculate source frame index
            source_frame = self._get_source_frame_index(layer, frame)
            # P1-COLOR-03: GPU 路径装车前按本帧源帧号求值关键帧（CPU/导出
            # 路径在合成时各自求值），保证三路帧值一致。
            preview_grade = self._evaluate_preview_grade_at(preview_grade, source_frame)
            layer_jobs.append((layer, asset_path, source_frame, preview_grade, clip_effects))

        # Stage E3 handoff: consume a complete scheduler bundle only when it
        # exactly describes the same ordinary video layers as this render
        # task.  A partial/occluded/special-asset plan stays on the established
        # decode path; this preserves effect/transition semantics and avoids
        # consuming a ring frame that cannot be submitted.
        scheduler_handles_by_clip: Dict[str, Any] = {}
        try:
            scheduler = self._timeline_decode_scheduler
        except (AttributeError, RuntimeError):
            # Some lightweight gate tests construct the QThread subclass via
            # __new__ to exercise pure GPU task assembly.  Treat that as the
            # scheduler being absent, just like a headless renderer.
            scheduler = None
        if scheduler is not None and realtime_now and task_meta.get("request_kind") != "scrub":
            try:
                scheduler_clip_ids = set(scheduler.layer_clip_ids)
                ordinary_jobs = [
                    job for job in layer_jobs
                    if job[1] not in ("__solid__", COMP_GPU_JOB_SENTINEL)
                    and self._asset_resolver.get_asset_type(
                        getattr(job[0], "asset_id", "")
                    ) == "video"
                ]
                ordinary_clip_ids = {getattr(job[0], "clip_id", "") for job in ordinary_jobs}
                if ordinary_clip_ids and ordinary_clip_ids == scheduler_clip_ids:
                    from src.features.edit.core.realtime_playback.types import PlaybackMode

                    source_frames = {
                        str(getattr(job[0], "clip_id", "")): int(job[2])
                        for job in ordinary_jobs
                    }
                    bundle = scheduler.request_bundle(
                        timeline_frame=int(frame),
                        generation_id=int(generation_id),
                        deadline_ns=int(task_meta.get("deadline_ns", 0) or 0),
                        mode=PlaybackMode.REALTIME,
                        layer_source_frames=source_frames,
                        consume=True,
                    )
                    if bundle is not None and bundle.complete:
                        for handle in bundle.layers:
                            if handle.payload_type in ("gpu_texture", "planar_yuv"):
                                scheduler_handles_by_clip[handle.identity.clip_id] = handle
                            else:
                                release_decoded_resource(handle.payload)
                        if len(scheduler_handles_by_clip) == len(ordinary_clip_ids):
                            trace_info["scheduler_bundle_hit"] = True
                            trace_info["scheduler_bundle_layers"] = len(
                                scheduler_handles_by_clip
                            )
                        else:
                            for handle in scheduler_handles_by_clip.values():
                                release_decoded_resource(handle.payload)
                            scheduler_handles_by_clip.clear()
            except Exception:
                # Scheduler is an optimization layer.  A missing capability or
                # stale test double must never block the proven GPU path.
                logger.debug("worker scheduler bundle consume failed", exc_info=True)
                scheduler_handles_by_clip.clear()

        def decode_layer(job: tuple) -> Dict[str, Any]:
            # Normal jobs are 5-tuples; solid jobs carry a 6th element (rgb)
            # and optionally a 7th (generator_params dict).
            if len(job) >= 6:
                layer, asset_path, source_frame, preview_grade, clip_effects, solid_rgb = job[:6]
                gen_params = job[6] if len(job) > 6 else None
            else:
                layer, asset_path, source_frame, preview_grade, clip_effects = job
                solid_rgb = None
                gen_params = None

            # TL-C01: comp_clip GPU-direct layer — produced as a
            # CompositeGpuFrameHandle (no CPU frame, no readback).
            if asset_path == COMP_GPU_JOB_SENTINEL:
                result = self._decode_comp_clip_gpu_layer(layer, frame)
                if result.get("ok"):
                    # A comp graph is the clip's source, but the timeline clip
                    # may still own a ColorStack, filters, crop and transform.
                    # Keep those semantics on the Edit task exactly like a
                    # decoded video layer; only the source pixels bypass CPU.
                    result.setdefault("track_info", {}).update({
                        "preview_grade": preview_grade,
                        "clip_effects": clip_effects,
                        "source_frame": source_frame,
                    })
                return result

            # Solid Color generator: no source to decode. Return a synthetic
            # layer_data the GPU renderer paints with a fill-color shader.
            if asset_path == "__solid__" and solid_rgb is not None:
                track_info = {
                    'opacity': getattr(layer, 'opacity', 1.0),
                    'blend_mode': getattr(getattr(layer, 'clip', None), 'blend_mode', None),
                    'track_index': getattr(layer, 'track_index', 0),
                    'clip_id': getattr(layer, 'clip_id', ''),
                    'preview_grade': preview_grade,
                    'clip_effects': clip_effects,
                    'source_frame': source_frame,
                    # Generator flags consumed by EditGpuPreviewRenderer:
                    'is_solid': True,
                    'solid_color': (float(solid_rgb[0]), float(solid_rgb[1]), float(solid_rgb[2])),
                    'generator_params': gen_params,
                    # FX-M06: crop applies to solid layers too (a solid can be
                    # cropped like any other clip). See video-layer track_info.
                    'crop': getattr(layer, 'crop', None),
                }
                return {
                    'ok': True,
                    'frame_type': 'solid',
                    'track_info': track_info,
                }
            # Final-2 (FINAL-P0-02): policy-aware decode。
            # 当 engine 提供了该 layer 的 SourceDecision 时，走
            # get_frame_with_policy() 让 SourceDecision 真正约束解码
            # （realtime PLACEHOLDER/NONE 拒绝 master，返回 'placeholder'）。
            # 否则回退到既有 get_frame() 行为（兼容无 engine 的直接渲染）。
            clip_id = getattr(layer, "clip_id", None)
            decode_stream_key = getattr(layer, "decoder_stream_key", None) or clip_id
            decision = decisions_map.get(clip_id) if clip_id else None
            is_interactive_scrub = task_meta.get("request_kind") == "scrub"
            scheduler_handle = scheduler_handles_by_clip.pop(str(clip_id), None)
            if scheduler_handle is not None:
                decoded_frame = scheduler_handle.payload
                frame_type = (
                    "gpu" if scheduler_handle.payload_type == "gpu_texture"
                    else "planar"
                )
            elif is_interactive_scrub and hasattr(self._gpu_decode_manager, "get_frame_interactive"):
                deadline_at = self._interactive_scrub_deadline_at(
                    asset_path=asset_path,
                    source_frame=source_frame,
                    target_size=self._decode_target_size,
                    stream_key=decode_stream_key,
                    task_meta=task_meta,
                    timeline_frame=frame,
                )
                decoded_frame, frame_type = self._gpu_decode_manager.get_frame_interactive(
                    asset_path,
                    source_frame,
                    target_size=self._decode_target_size,
                    stream_key=decode_stream_key,
                    is_stale=lambda: (
                        generation_id != self._generation_id
                        or self._stop_flag
                        # Abandon only when the playhead moved far past this
                        # target. A newer-but-nearby request must not kill a
                        # nearly-complete GOP walk — that is what froze the
                        # viewer during fast drags.
                        or self._is_abandoned_interactive_scrub_task(frame, task_meta)
                    ),
                    deadline_at=deadline_at,
                    coarse=bool(task_meta.get("scrub_coarse", False)),
                )
            elif decision is not None and hasattr(self._gpu_decode_manager, "get_frame_with_policy"):
                decoded_frame, frame_type = self._gpu_decode_manager.get_frame_with_policy(
                    asset_path,
                    source_frame,
                    target_size=self._decode_target_size,
                    stream_key=decode_stream_key,
                    source_decision=decision,
                )
            else:
                decoded_frame, frame_type = self._gpu_decode_manager.get_frame(
                    asset_path,
                    source_frame,
                    target_size=self._decode_target_size,
                    stream_key=decode_stream_key,
                )

            if decoded_frame is None:
                if frame_type == 'interactive_stale':
                    playback_trace(
                        "worker.decode_interactive_scrub_stale",
                        frame=frame,
                        generation=generation_id,
                        clip_id=clip_id,
                        source_frame=source_frame,
                    )
                    return {
                        'ok': False,
                        'fallback_reason': 'interactive_scrub_stale',
                        'layer': layer,
                    }
                # Final-2: 'placeholder' 来自 SourceDecision 拒绝 master 解码，
                # engine 已在 request_present() 层处理 hold/rebuffer，这里标记原因。
                if frame_type == 'placeholder':
                    playback_trace(
                        "worker.decode_realtime_placeholder",
                        frame=frame,
                        generation=generation_id,
                        clip_id=clip_id,
                        source_frame=source_frame,
                    )
                    return {
                        'ok': False,
                        'fallback_reason': 'realtime_master_denied_placeholder',
                        'layer': layer,
                    }
                return {
                    'ok': False,
                    'fallback_reason': 'decode_failed_or_cpu_packed',
                    'layer': layer,
                }

            if frame_type == 'cpu':
                # CPU packed frame — can't use GPU path, release and fallback
                safe_release_gpu_frame(decoded_frame)
                return {
                    'ok': False,
                    'fallback_reason': 'decode_failed_or_cpu_packed',
                    'layer': layer,
                }

            # Calculate track info (opacity, etc.)
            track_info = {
                'opacity': getattr(layer, 'opacity', 1.0),
                'blend_mode': getattr(getattr(layer, 'clip', None), 'blend_mode', None),
                'track_index': getattr(layer, 'track_index', 0),
                'clip_id': getattr(layer, 'clip_id', ''),
                'preview_grade': preview_grade,
                # Frozen effect chain snapshot (tuple of EffectInstance).
                # The GPU renderer applies each entry in `order` on the
                # per-track texture after the primary grade. Empty tuple =
                # no work, renderer skips the effect pass entirely.
                'clip_effects': clip_effects,
                # source_frame is needed by the renderer to sample animated
                # effect params at the correct frame (keyframe interpolation).
                'source_frame': source_frame,
                # clip TransformParams 7-tuple (pos_x, pos_y, scale_x, scale_y,
                # rotation, anchor_x, anchor_y). GPU renderer applies it as a
                # mat3 in NDC space on the track-FBO quad (see
                # edit_gpu_preview_renderer._transform_to_ndc_mat3_bytes).
                'transform': getattr(layer, 'transform', None),
                # TRAW（批 2）：非破坏 RAW 参数（RawParams | None）——
                # debayer shader 的 WB/EI/OOTF uniform 源；None = 单位默认。
                'raw_params': getattr(getattr(layer, 'clip', None), 'raw', None),
                # FX-M06: CropParams 5-tuple (left, right, top, bottom, feather).
                # GPU renderer applies it as a matte pass after transform, before
                # grade. Without this key the crop silently no-ops in preview.
                'crop': getattr(layer, 'crop', None),
            }

            # P0-PREV-GPU-T：转场帧的有效 opacity 覆写（CPU 同语义）。
            # GPU 合成器按 u_opacity 混合，cross dissolve 的 lerp、dip 的
            # 单侧隐藏由此生效。
            _trans_info = transition_map_by_clip.get(clip_id)
            if _trans_info is not None:
                if clip_id == _trans_info.left_clip_id:
                    track_info['opacity'] = float(_trans_info.left_opacity)
                elif clip_id == _trans_info.right_clip_id:
                    track_info['opacity'] = float(_trans_info.right_opacity)

            if frame_type == 'gpu':
                return {
                    'ok': True,
                    'gpu_frame': decoded_frame,
                    'frame_type': 'gpu',
                    'track_info': track_info,
                }
            if frame_type == 'cuda_resident':
                # B2.1：cuda 驻留帧（设备指针描述，无 GL 资源）——renderer
                # GL 线程导入后按零拷贝通路消费；release 幂等。
                # W03：asset_path 随层传递，GL 导入失败时按素材上报（解码
                # 管理器停该素材驻留路由并重建下载/CPU 源）。
                return {
                    'ok': True,
                    'cuda_resident': decoded_frame,
                    'frame_type': 'cuda_resident',
                    'asset_path': asset_path,
                    'track_info': track_info,
                }
            if frame_type == 'vt_pixel_buffer':
                return {
                    'ok': True,
                    'vt_pixel_buffer': decoded_frame,
                    'frame_type': 'vt_pixel_buffer',
                    'track_info': track_info,
                }
            if frame_type in ('planar', 'vt_planar'):
                return {
                    'ok': True,
                    'decoded_frame': decoded_frame,
                    'frame_type': frame_type,
                    'track_info': track_info,
                }
            return {
                'ok': False,
                'fallback_reason': 'decode_failed_or_cpu_packed',
                'layer': layer,
            }

        gpu_layers: list[Dict[str, Any]] = []
        all_ok = True
        fallback_reason = 'decode_failed_or_cpu_packed'
        t_decode0 = time.perf_counter()

        if len(layer_jobs) > 1:
            playback_trace(
                "worker.gpu_decode_parallel_start",
                frame=frame,
                generation=generation_id,
                layers=len(layer_jobs),
            )
            # A Comp Clip GPU producer is not a CPU decode job: it owns
            # the worker's shared QOpenGLContext and must call
            # makeCurrent/render/release on that same thread.  Submitting
            # it to the generic decode pool can move GL calls to an
            # executor thread, where the context is not thread-affine and
            # pooled textures/fences belong to the wrong lane.  Keep only
            # ordinary media decode jobs in the pool; run Comp Clip
            # producers on this worker thread while those CPU/VT decodes
            # remain parallel.
            try:
                results = [None] * len(layer_jobs)
                futures = []
                comp_gl_mutex = None
                comp_gl_acquired = False
                try:
                    has_comp_jobs = any(
                        job[1] == COMP_GPU_JOB_SENTINEL
                        for job in layer_jobs
                    )
                    if (
                        has_comp_jobs
                        and self._comp_gl_ready
                        and not self._comp_gl_context_current
                    ):
                        comp_gl_mutex = GLContextManager.instance().mutex
                        comp_gl_mutex.lock()
                        if self._comp_gl_context.makeCurrent(
                                self._comp_gl_surface):
                            self._comp_gl_context_current = True
                            comp_gl_acquired = True
                        else:
                            comp_gl_mutex.unlock()
                            comp_gl_mutex = None

                    for index, job in enumerate(layer_jobs):
                        if job[1] == COMP_GPU_JOB_SENTINEL:
                            results[index] = decode_layer(job)
                        else:
                            futures.append((
                                index,
                                self._decode_executor.submit(
                                    decode_layer, job),
                            ))
                finally:
                    if comp_gl_acquired:
                        self._comp_gl_context.doneCurrent()
                        self._comp_gl_context_current = False
                    if comp_gl_mutex is not None:
                        comp_gl_mutex.unlock()

                for index, future in futures:
                    results[index] = future.result()
            except RuntimeError:
                # executor submit/result 的 RuntimeError（典型：shutdown 后
                # 再提交）会落到这里。第一轮已产出的结果持有 comp GPU
                # handle / VT 像素缓冲租约：直接整表重解会泄漏它们并把
                # comp 图解码两次。ok=True 的结果才持资源（失败路径在
                # decode_layer 内部已自行释放），逐个归还后串行重解。
                for produced in results:
                    if produced is not None and produced.get('ok'):
                        release_layer_data(produced)
                results = [decode_layer(job) for job in layer_jobs]
        else:
            results = [decode_layer(job) for job in layer_jobs]

        for result in results:
            if not result.get('ok'):
                all_ok = False
                fallback_reason = result.get('fallback_reason', fallback_reason)
                # Keep scanning (no break): decode jobs after the failed one
                # already ran and hold GPU frames / handle leases — skipping
                # them here would abandon those resources unreleased.
                continue
            gpu_layers.append(result)

        # Per-frame decode budget (all layers, including parallel waves) for
        # the frame trace; everything after this until frame_ready is the
        # submit/composite/emit side.
        trace_info['decode_ms'] = (time.perf_counter() - t_decode0) * 1000.0
        # 多流解码波饱和遥测维度：本帧并行解码流数（W3 立项 K-W1 触发
        # 信号——decode_ms 按 decode_layers 分桶随轨数恶化 = CPU 解码
        # 核饱和；见 FrameTraceCollector._summarize_decode_saturation）
        trace_info['decode_layers'] = len(layer_jobs)

        if not all_ok or not gpu_layers:
            # Release any frames we obtained
            for layer_data in gpu_layers:
                release_layer_data(layer_data)
            if not gpu_layers and fallback_reason == 'decode_failed_or_cpu_packed':
                trace_info['fallback_reason'] = 'no_valid_layers_for_gpu'
            else:
                trace_info['fallback_reason'] = fallback_reason
            playback_trace(
                "worker.gpu_prepare_failed",
                frame=frame,
                generation=generation_id,
                reason=trace_info.get('fallback_reason', 'unknown'),
                gpu_layers=len(gpu_layers),
            )
            return False, trace_info

        # A renderer object can exist before its shared GL thread has finished
        # initialization, or after that thread has failed.  Treat that as an
        # unavailable pipeline, not a temporary full queue: realtime playback
        # may use the CPU fallback until the GPU renderer is genuinely ready.
        renderer_ready = getattr(self._gpu_renderer, "is_initialized", True)
        if callable(renderer_ready):
            renderer_ready = renderer_ready()
        if not renderer_ready:
            for layer_data in gpu_layers:
                release_layer_data(layer_data)
            trace_info['fallback_reason'] = 'gpu_renderer_not_ready'
            playback_trace(
                "worker.gpu_submit_skipped_not_ready",
                frame=frame,
                generation=generation_id,
            )
            return False, trace_info

        # Submit to GPU renderer
        task = EditRenderTask(
            frame=frame,
            generation_id=generation_id,
            layers=gpu_layers,
            viewport_size=self._viewport_size,
            output_fbo_size=self._viewport_size,
            coordinate_size=self._get_sequence_resolution(),
            request_kind=str(task_meta.get("request_kind", "") or ""),
            scrub_request_token=(
                int(task_meta["scrub_request_token"])
                if task_meta.get("scrub_request_token") is not None
                else None
            ),
            deadline_ns=int(task_meta.get("deadline_ns", 0) or 0),
            background=bool(task_meta.get("background")),
            # P3.2：非实时任务合成后把结果存入 GPU 复合纹理缓存。
            cacheable=not realtime_now,
            # Adjustment Clip operations — effects/Transform are applied at
            # their z-position and Opacity is mixed into the lower result by
            # the GPU renderer. Carried as (ClipLayerInfo, effects_tuple).
            adjustment_layers=adjustment_layers,
            # P0-PREV-GPU-T：转场任务（spatial mask / dip 颜色 pass）。
            transition_jobs=transition_jobs,
        )

        playback_trace(
            "worker.gpu_submit",
            frame=frame,
            generation=generation_id,
            layers=len(gpu_layers),
            viewport=self._viewport_size,
            request_kind=task.request_kind,
            scrub_token=task.scrub_request_token,
        )
        try:
            compositor = self._gpu_compositor
        except (AttributeError, RuntimeError):
            compositor = None
        compositor_submit = getattr(compositor, "submit_backend_task", None)
        if callable(compositor_submit):
            # Keep the complete EditRenderTask intact: effect chains,
            # adjustment scopes and procedural transition jobs must survive
            # the compositor migration unchanged.
            submitted = bool(compositor_submit(task))
        else:
            submitted = bool(self._gpu_renderer.submit(task))
        if not submitted:
            # submit() can also fail when the render thread exits between the
            # readiness check and this call.  Only an initialized renderer may
            # report the transient queue-full condition that is safe to hold.
            for layer_data in gpu_layers:
                release_layer_data(layer_data)
            if task.background:
                # 后台缓存回填任务遇 GPU 队列满：静默丢弃这一帧，绝不能
                # 落到慢速 CPU 合成去占用空闲窗口。
                playback_trace(
                    "worker.gpu_submit_failed_background_drop",
                    frame=frame,
                    generation=generation_id,
                )
                return True, trace_info
            renderer_ready = getattr(self._gpu_renderer, "is_initialized", True)
            if callable(renderer_ready):
                renderer_ready = renderer_ready()
            trace_info['fallback_reason'] = (
                'gpu_renderer_queue_full'
                if renderer_ready else 'gpu_renderer_not_ready'
            )
            playback_trace("worker.gpu_submit_failed", frame=frame, generation=generation_id)
            return False, trace_info

        # Record decode path info
        decode_paths = set()
        for layer_data in gpu_layers:
            ft = layer_data.get('frame_type', '')
            if ft == 'comp_gpu_handle':
                # TL-C01: comp clip consumed as a GPU texture handle —
                # no CPU readback happened for this layer.
                decode_paths.add('comp_gpu_direct')
            elif ft in ('gpu', 'vt_pixel_buffer'):
                decode_paths.add('macos_vt_zero_copy')
            elif ft == 'cuda_resident':
                # B2.1：Windows cuda 驻留帧（设备内拷贝入常驻 GL 纹理）。
                decode_paths.add('win_cuda_resident_zero_copy')
            elif ft == 'vt_planar':
                decode_paths.add('videotoolbox_planar')
            elif ft == 'planar':
                # The decoder itself is FFmpeg/CPU for VP8 (VideoToolbox does
                # not expose a usable VP8 IOSurface path).  The expensive
                # colour conversion, scaling and compositing remain on GPU.
                decode_paths.add('ffmpeg_planar_gpu_upload')
        trace_info['decode_path'] = ','.join(sorted(decode_paths)) if decode_paths else 'gpu'

        return True, trace_info

    def _get_source_frame_index(self, layer: ClipLayerInfo, timeline_frame: int) -> int:
        """Get the source frame index for a clip layer."""
        # ClipLayerInfo already computes source_frame
        return getattr(layer, 'source_frame', 0) or 0

    def _composite_layers(
        self,
        layers: Tuple[ClipLayerInfo, ...],
        transitions: Tuple[TransitionBlendInfo, ...],
        frame: int
    ) -> Optional[np.ndarray]:
        """
        合成多个层。

        多轨叠加规则：
        - 按 track_index 从小到大（从下到上）合成
        - opacity < 1.0 时 alpha blend
        - 转场中的 clip 按转场规则混合

        Args:
            layers: ClipLayerInfo 列表（按 track_index 排序）
            transitions: TransitionBlendInfo 列表
            frame: 帧号

        Returns:
            合成后的帧数据（numpy.ndarray BGR uint8），或 None
        """
        if not layers:
            return None

        # Keep the authoritative timeline frame available while decoding all
        # layers, including the pre-pass used by spatial/wipe transitions.
        # Motion Graphic cache files are indexed by timeline-relative frame
        # after a duration retime.
        previous_timeline_frame = getattr(self, "_current_render_timeline_frame", None)
        self._current_render_timeline_frame = frame
        try:
            # 按 track_index 排序（从小到大，从下到上）
            sorted_layers = sorted(layers, key=lambda l: l.track_index)

            # CPU fallback/composite should render at preview resolution, not the
            # widget size. The viewer can scale the resulting QImage cheaply; doing
            # full-size CPU resize/blend here is the main reason edit playback lags.
            viewport_w, viewport_h = self._decode_target_size

            # 创建合成缓冲区（uint8 用于高效混合）
            composite_buffer: Optional[np.ndarray] = None

            # 处理转场映射：clip_id -> transition_info
            transition_map: Dict[str, TransitionBlendInfo] = {}
            for trans in transitions:
                if trans.left_clip_id:
                    transition_map[trans.left_clip_id] = trans
                if trans.right_clip_id:
                    transition_map[trans.right_clip_id] = trans

            # 两源转场先预合成成单张图，再作为普通层注入合成流（同时移除
            # 两个独立层）。形状/方向类走 mask；Blur/Film/Smooth Cut 走全画面
            # 双源合成，不会出现几何边界。
            wipe_composited: Dict[str, np.ndarray] = {}
            wipe_skip_ids: set[str] = set()
            for trans in transitions:
                # AUD-19202：改用集中判定（统一解析用户自定义转场的 base）。
                # 内置类型行为不变（COMPOSITE_TRANSITION_TYPES == 内置集合）；
                # 用户 spatial base（wipe/slide/iris）由此进入几何/全画面双源
                # 预合成，不再退化为普通溶解。dip 类（含用户 dip base）不经
                # 此循环——由下方合成域颜色 pass 处理（与内置 dip 同路径）。
                if not is_composite_transition(trans.transition_type):
                    continue
                left_layer = next((l for l in sorted_layers if l.clip_id == trans.left_clip_id), None)
                right_layer = next((l for l in sorted_layers if l.clip_id == trans.right_clip_id), None)
                if left_layer is None and right_layer is None:
                    continue
                left_frame = self._get_layer_frame(left_layer) if left_layer is not None else None
                right_frame = self._get_layer_frame(right_layer) if right_layer is not None else None
                if left_frame is None and right_frame is None:
                    continue
                if left_frame is not None and left_layer is not None:
                    left_frame = self._apply_preview_color_grade(left_frame, left_layer)
                    left_frame = self._resize_frame_to_viewport(left_frame, viewport_w, viewport_h)
                if right_frame is not None and right_layer is not None:
                    right_frame = self._apply_preview_color_grade(right_frame, right_layer)
                    right_frame = self._resize_frame_to_viewport(right_frame, viewport_w, viewport_h)
                # A one-sided wipe uses black as its missing source.
                template = left_frame if left_frame is not None else right_frame
                assert template is not None
                if left_frame is None:
                    left_frame = np.zeros_like(template)
                if right_frame is None:
                    right_frame = np.zeros_like(template)
                left_frame, right_frame = self._match_frames(left_frame, right_frame)
                # LIB1/X09（L03/L05）：双源重采样合成；user_* 转场按解析后
                # base 判定（否则 user 配方静默退化为普通溶解）。
                from src.features.edit.core.transition_catalog import (
                    resolve_transition_base_type,
                )
                if resolve_transition_base_type(trans.transition_type) \
                        in RESAMPLE_TRANSITION_TYPES:
                    # LIB1 双源复合家族（X09 + L14/L15）：统一分派器按
                    # base 类型路由（未知类型 ValueError 精确拒绝）。
                    from src.features.edit.transition_packs.library_v1.resample_cores import (
                        composite_lib1_transition,
                    )
                    wipe_composited[trans.transition_id] = composite_lib1_transition(
                        resolve_transition_base_type(trans.transition_type),
                        left_frame,
                        right_frame,
                        trans.blend_factor,
                        dict(trans.transition.params or {}),
                    )
                elif trans.transition_type in FULL_FRAME_TRANSITION_TYPES:
                    wipe_composited[trans.transition_id] = apply_full_frame_transition(
                        left_frame,
                        right_frame,
                        trans.blend_factor,
                        trans.transition_type,
                        trans.transition.params,
                        first_opacity=trans.left_opacity,
                        second_opacity=trans.right_opacity,
                    )
                else:
                    if trans.transition_type in TRANSITION_EFFECT_TYPES:
                        left_frame = apply_transition_effect(
                            left_frame,
                            trans.blend_factor,
                            trans.transition_type,
                            trans.transition.params,
                        )
                        right_frame = apply_transition_effect(
                            right_frame,
                            trans.blend_factor,
                            trans.transition_type,
                            trans.transition.params,
                        )
                    mask = compute_transition_mask(
                        left_frame.shape[1], left_frame.shape[0],
                        trans.blend_factor, trans.transition_type,
                        trans.transition.params,
                    )
                    # Apply each source opacity before the spatial wipe. Multiplying
                    # the two opacities would attenuate the result twice and would
                    # make a partially transparent clip unexpectedly darker.
                    left_opacity = max(0.0, min(1.0, trans.left_opacity))
                    right_opacity = max(0.0, min(1.0, trans.right_opacity))
                    if (
                        left_opacity >= 1.0
                        and right_opacity >= 1.0
                        and left_frame.dtype == np.uint8
                        and right_frame.dtype == np.uint8
                        and left_frame.ndim == 3
                        and right_frame.ndim == 3
                    ):
                        # The normal Wipe case has two opaque uint8 BGR sources.
                        # OpenCV performs this per-pixel blend in C++ without the
                        # several full-frame float32 temporaries used by NumPy.
                        import cv2
                        wipe_composited[trans.transition_id] = cv2.blendLinear(
                            left_frame,
                            right_frame,
                            1.0 - mask,
                            mask,
                        )
                    else:
                        mask_c = mask[:, :, None] if left_frame.ndim == 3 else mask
                        blended = (
                            left_frame.astype(np.float32)
                            * (1.0 - mask_c)
                            * left_opacity
                            + right_frame.astype(np.float32)
                            * mask_c
                            * right_opacity
                        )
                        wipe_composited[trans.transition_id] = np.clip(
                            blended, 0, 255
                        ).astype(np.uint8)
                if trans.left_clip_id:
                    wipe_skip_ids.add(trans.left_clip_id)
                if trans.right_clip_id:
                    wipe_skip_ids.add(trans.right_clip_id)

            # 标记每个 wipe transition 在 left 层注入（用 left 的 track_index/opacity）
            wipe_inject: Dict[str, str] = {}  # available clip id -> transition id
            for trans in transitions:
                if (
                    is_composite_transition(trans.transition_type)
                    and trans.transition_id in wipe_composited
                ):
                    inject_id = trans.left_clip_id or trans.right_clip_id
                    if inject_id:
                        wipe_inject[inject_id] = trans.transition_id

            # 遍历所有层进行合成
            for layer in sorted_layers:
                # Wipe 的两个原始层已预合成，跳过；在 left 层位置注入预合成结果
                if layer.clip_id in wipe_skip_ids:
                    if layer.clip_id in wipe_inject:
                        tid = wipe_inject[layer.clip_id]
                        composite_buffer = self._composite_frame_into_buffer(
                            composite_buffer, wipe_composited[tid], 1.0, viewport_w, viewport_h
                        )
                    continue

                # Resolve transition opacity before decoding. At the exact start
                # or end of a transition one side is fully transparent; decoding
                # that side only wastes a frame seek/decode on the playback path.
                effective_opacity = layer.opacity
                if layer.clip_id in transition_map:
                    trans_info = transition_map[layer.clip_id]
                    if layer.clip_id == trans_info.left_clip_id:
                        effective_opacity = trans_info.left_opacity
                    elif layer.clip_id == trans_info.right_clip_id:
                        effective_opacity = trans_info.right_opacity
                if effective_opacity <= 0.0:
                    continue

                # 获取该层的帧数据
                layer_frame = self._get_layer_frame(layer)
                # A3.2: frame_blend 双源帧加权（慢速/斜坡消频闪）
                if layer_frame is not None:
                    from src.features.edit.core.timeline_frame_mapper import (
                        _apply_frame_blend_layer,
                    )
                    layer_frame = _apply_frame_blend_layer(
                        layer, layer_frame, frame, self._get_layer_frame)

                if layer_frame is None:
                    # FX-C03: Adjustment Clips have no source frame, but unlike
                    # the old behaviour they must NOT be silently dropped. Their
                    # effect chain applies to the accumulated composite_buffer
                    # (everything composited below them so far) — the CPU mirror
                    # of the GPU post-composite pass. This keeps preview parity
                    # when the GPU pipeline is unavailable (init failure / forced
                    # CPU). Scope is correct because the loop processes layers
                    # bottom→top, so only lower layers are in the buffer here.
                    _atype = self._asset_resolver.get_asset_type(layer.asset_id)
                    if _atype == 'adjustment':
                        if composite_buffer is not None:
                            # T-P1-22：ColorStack 先于效果链（与导出路径一致）。
                            # N07-B：调整剪辑豁免 timeline 段（下方 clip 已
                            # 各自应用过，防 timeline look 双重应用）。
                            adjusted_buffer = self._apply_preview_color_grade(
                                composite_buffer, layer, exclude_timeline=True
                            )
                            adjusted_buffer = self._apply_clip_effects_cpu(
                                adjusted_buffer, layer, frame
                            )
                            from src.features.edit.core.adjustment_composite import (
                                apply_transform_cpu,
                                blend_adjustment_cpu,
                            )
                            adjusted_buffer = apply_transform_cpu(
                                adjusted_buffer,
                                getattr(layer, "transform", None),
                                viewport_w,
                                viewport_h,
                                *self._get_sequence_resolution(),
                            )
                            composite_buffer = blend_adjustment_cpu(
                                composite_buffer,
                                adjusted_buffer,
                                getattr(layer, "transform", None),
                                getattr(layer, "opacity", 1.0),
                                *self._get_sequence_resolution(),
                            )
                        continue
                    logger.warning(f"Failed to get frame for layer {layer.clip_id}")
                    continue

                # CPU is the fallback path only.  It must still honor the same
                # basic ColorStack grade as the GPU path, otherwise a decoder
                # fallback silently changes the visible edit result.
                layer_frame = self._apply_preview_color_grade(layer_frame, layer)

                # FX-M06: Apply edge crop (after grade, before effects)
                crop = tuple(getattr(layer, "crop", ()) or ())
                if not crop_is_identity(crop):
                    layer_frame = apply_crop_cpu(layer_frame, crop)

                # CPU fallback must also honor clip video filters (blur/sharpen/...)
                # — the GPU path applies them via EffectPassRunner, but without
                # this CPU equivalent any clip effect is silently invisible when
                # the GPU pipeline is unavailable (v3 disabled or init failure).
                layer_frame = self._apply_clip_effects_cpu(layer_frame, layer, frame)

                # 调整帧尺寸以匹配视口
                layer_frame = self._resize_frame_to_viewport(layer_frame, viewport_w, viewport_h)

                # Apply clip transform (position/scale/rotation/anchor) in viewport
                # pixel space. Skipped for identity transforms (the common case).
                # Must run AFTER resize (so warp operates on viewport-sized frame)
                # and BEFORE composite (so the transformed frame is alpha-blended).
                layer_frame = self._apply_clip_transform_cpu(
                    layer_frame, getattr(layer, 'transform', None),
                    viewport_w, viewport_h,
                    *self._get_sequence_resolution(),
                )

                # FX-M06: apply the clip's crop matte AFTER transform, in viewport
                # space — matching the GPU order (transform → crop → grade/effects
                # happen on the track FBO; here grade/effects ran in source space
                # but they are non-geometric, so the geometric crop/transform order
                # is what matters for parity). Prior to FX-M06 the CPU path ignored
                # crop entirely. Crop geometry is normalised [0,1], so it adapts to
                # the viewport-sized frame here.
                layer_frame = self._apply_crop_cpu(layer_frame, layer)

                # 合成到缓冲区（A1: 携带片段图层混合模式）
                composite_buffer = self._composite_frame_into_buffer(
                    composite_buffer, layer_frame, effective_opacity, viewport_w, viewport_h,
                    blend_mode=getattr(layer.clip, "blend_mode", None),
                )

            # 处理转场的颜色混合（Dip To Color）
            for trans in transitions:
                if composite_buffer is None:
                    continue
                # AUD-19202：dip 判定继承用户自定义转场的 base（base 为
                # dip_to_color 的 user_* 类型不再退化为普通溶解）。
                if not is_dip_transition(trans.transition_type):
                    continue
                if trans.transition_type == "dip_to_color":
                    color_factor = trans.get_color_factor()
                    blend_color = trans.blend_color
                else:
                    # render plan 不为自定义类型计算 dip 专属字段，按内置
                    # dip 的同一三阶段公式与颜色参数解析规则在此补齐。
                    color_factor = (
                        trans.blend_factor * 2.0
                        if trans.blend_factor <= 0.5
                        else (1.0 - trans.blend_factor) * 2.0
                    )
                    blend_color = self._resolve_user_dip_color(trans)
                if color_factor > 0.0:
                    composite_buffer = self._blend_with_color(
                        composite_buffer,
                        blend_color,
                        color_factor
                    )

        finally:
            # AUD-18905：合成循环中途抛异常（会被 _execute_render 兜底捕获）
            # 时同样恢复/删除帧号——否则本帧的 timeline_frame 残留，下一
            # 帧渲染前 _get_motion_graphic_render_cache_frame 与 comp 缓存
            # 键会用陈旧帧号索引，可能读到错误缓存帧（嵌套序列递归同样受影响）。
            if previous_timeline_frame is None:
                try:
                    delattr(self, "_current_render_timeline_frame")
                except AttributeError:
                    pass
            else:
                self._current_render_timeline_frame = previous_timeline_frame

        if composite_buffer is None:
            return None

        # composite_buffer 已经是 uint8 BGR
        return composite_buffer

    def _composite_frame_into_buffer(
        self,
        composite_buffer: Optional[np.ndarray],
        layer_frame: np.ndarray,
        effective_opacity: float,
        viewport_w: int,
        viewport_h: int,
        blend_mode=None,
    ) -> Optional[np.ndarray]:
        """Composite a single (already-sized) layer frame into the buffer.

        Centralises the init/blend logic used by both the normal layer loop
        and the wipe pre-composite injection path.

        A1: ``blend_mode`` 非 None 且非 normal 时按图层混合模式混合
        （与 GPU blend.glsl / blend_modes.py 同一公式），normal 走原
        alpha over 路径不变。
        """
        if layer_frame.ndim == 3 and layer_frame.shape[2] == 4:
            # Straight BGRA from transparent scene/caption renderers.
            bgr = layer_frame[..., :3].astype(np.float32)
            alpha = (layer_frame[..., 3:4].astype(np.float32) / 255.0) * max(0.0, min(1.0, effective_opacity))
            if composite_buffer is None:
                return np.clip(bgr * alpha, 0.0, 255.0).astype(np.uint8)
            if composite_buffer.shape[:2] != layer_frame.shape[:2]:
                import cv2
                composite_buffer = cv2.resize(composite_buffer, (layer_frame.shape[1], layer_frame.shape[0]), interpolation=cv2.INTER_LINEAR)
            base = composite_buffer[..., :3].astype(np.float32)
            # A1: 4 通道层（字幕/场景/RGBA 解码）同样应用图层混合模式，
            # 与 CPU 导出（per-pixel alpha）和 GPU 预览一致——此前该分支
            # 在 mode 分派之前 early return，混合模式对字幕层完全无效。
            mode_code = getattr(blend_mode, "code", 0) if blend_mode is not None else 0
            if mode_code != 0:
                from src.features.edit.core.blend_modes import blend_rgb
                base_f = base / 255.0
                over_f = bgr / 255.0
                # blend_rgb 的 HSL 系公式按 RGB 加权；输入输出均为 BGR，
                # 翻转后混合再翻回（与 blend_frames_u8 同一约定）。
                blended = blend_rgb(
                    base_f[..., ::-1], over_f[..., ::-1], mode_code,
                )[..., ::-1]
                out = blended * alpha + base_f * (1.0 - alpha)
                return np.clip(out * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
            return np.clip(bgr * alpha + base * (1.0 - alpha), 0.0, 255.0).astype(np.uint8)

        if composite_buffer is None:
            # 第一层：直接初始化缓冲区
            buf = self._init_composite_buffer(layer_frame, viewport_w, viewport_h)
            # 应用 opacity（与黑色背景混合）
            if effective_opacity < 1.0:
                import cv2
                black = np.zeros_like(buf)
                buf = cv2.addWeighted(buf, effective_opacity, black, 0, 0)
            return buf
        # 后续层：A1 图层混合模式（非 normal）→ 共享 NumPy 公式
        mode_code = getattr(blend_mode, "code", 0) if blend_mode is not None else 0
        if mode_code != 0:
            from src.features.edit.core.blend_modes import blend_frames_u8
            if layer_frame.shape[:2] != composite_buffer.shape[:2]:
                import cv2
                layer_frame = cv2.resize(
                    layer_frame, (composite_buffer.shape[1], composite_buffer.shape[0]),
                    interpolation=cv2.INTER_LINEAR)
            return blend_frames_u8(
                composite_buffer, layer_frame, mode_code, effective_opacity)
        # normal：alpha blend
        return self._alpha_blend(composite_buffer, layer_frame, effective_opacity)

    @staticmethod
    def _match_frames(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Align two frames to the same shape and channel count.

        Used by the wipe pre-composite so the left/right clips (which may have
        different decode sizes or gray/colour channels) can be blended.
        """
        import cv2
        # 统一通道数到 3（BGR）
        def to_bgr(img: np.ndarray) -> np.ndarray:
            if img.ndim == 2:
                return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            if img.ndim == 3 and img.shape[2] == 4:
                return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
            if img.ndim == 3 and img.shape[2] == 1:
                return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            return img
        a = to_bgr(a)
        b = to_bgr(b)
        # 统一尺寸（以 a 为准）
        h, w = a.shape[:2]
        if b.shape[:2] != (h, w):
            b = cv2.resize(b, (w, h), interpolation=cv2.INTER_AREA)
        return a, b

    def _get_comp_frame_renderer(self):
        """Lazy-initialize and return the CompositeFrameRenderer.

        The renderer is created on first use and reused across frames.
        Thread-safe: called only on the worker thread.
        """
        if self._comp_frame_renderer is None:
            from src.features.composite.render.composite_frame_renderer import CompositeFrameRenderer
            self._comp_frame_renderer = CompositeFrameRenderer()
        return self._comp_frame_renderer

    def setup_comp_gl_context(self, main_context: QOpenGLContext) -> bool:
        """在 GUI 线程创建共享 GL context + offscreen surface,转交本 worker 线程。

        复用 CompositeRenderer / EditGpuPreviewRenderer 的成熟模式：
        QOffscreenSurface + setShareContext(main_context) + moveToThread(self)。
        必须在 GUI 线程调用（QOffscreenSurface / context.create() 的平台要求）。

        Returns:
            True 表示成功，worker 后续渲染 comp_clip 时可 makeCurrent。
        """
        if main_context is None:
            return False
        try:
            # QOffscreenSurface 必须在 GUI 线程创建（Windows WGL 要求）
            self._comp_gl_surface = QOffscreenSurface()
            self._comp_gl_surface.setFormat(main_context.format())
            self._comp_gl_surface.create()

            # 暂存主 context 当前 surface，临时释放以便创建共享 context
            current_surface = main_context.surface()
            if current_surface:
                main_context.doneCurrent()

            self._comp_gl_context = QOpenGLContext()
            self._comp_gl_context.setFormat(main_context.format())
            self._comp_gl_context.setShareContext(main_context)
            if not self._comp_gl_context.create():
                logger.error("PreviewRenderWorker: failed to create shared GL context for comp_clip")
                self._comp_gl_context = None
                self._comp_gl_surface = None
                self._comp_gpu_direct_enabled = False
                if current_surface:
                    main_context.makeCurrent(current_surface)
                return False

            # 所有权转交 worker 线程（后续 makeCurrent 在 worker 线程调用）
            self._comp_gl_context.moveToThread(self)
            self._comp_gl_ready = True
            # TL-C01 producer lane armed: the actual gate stays the GPU
            # compositor's declared capability (has_comp_clip_gpu_support),
            # so the direct path only lights up once the joint-lane consumer
            # lands. Without this wiring the flag could never turn True in
            # production and the capability gate would be dead code.
            self._comp_gpu_direct_enabled = True

            # 恢复主 context
            if current_surface:
                main_context.makeCurrent(current_surface)

            logger.info("PreviewRenderWorker: shared GL context ready for comp_clip rendering")
            return True
        except Exception as e:
            logger.error(f"PreviewRenderWorker: setup_comp_gl_context failed: {e}", exc_info=True)
            self._comp_gl_context = None
            self._comp_gl_surface = None
            self._comp_gl_ready = False
            self._comp_gpu_direct_enabled = False
            return False

    def _comp_clip_gpu_direct_supported(self) -> bool:
        """TL-C01: whether the GPU compositor declared comp-gpu support.

        Strict ``is True`` comparison — a Mock test double returns a truthy
        Mock, never True, so legacy behaviour is preserved in tests unless
        they explicitly opt in.
        """
        if not self._comp_gpu_direct_enabled:
            return False
        supported = getattr(
            self._gpu_renderer, "has_comp_clip_gpu_support", None)
        return bool(callable(supported) and supported() is True)

    def _resolve_comp_graph(self, layer: ClipLayerInfo):
        """Resolve a comp_clip layer to (graph_id, graph_data) or (None, None)."""
        graph_id = getattr(layer, "composite_id", None) or ""
        if not graph_id:
            asset = self._asset_resolver._project.get_asset_by_id(
                layer.asset_id)
            if asset is None or not asset.is_comp_clip:
                return None, None
            graph_id = asset.metadata.get('graph_id', '')
        if not graph_id:
            return None, None
        graph_data = self._asset_resolver._project.compositions.get(graph_id)
        if graph_data is None:
            return None, None
        return graph_id, graph_data

    def _decode_comp_clip_gpu_layer(self, layer: ClipLayerInfo,
                                    frame: int) -> Dict[str, Any]:
        """TL-C01/TL-P1-01: produce a comp_clip layer as a GPU handle.

        Runs the comp graph through ``render_comp_clip_gpu`` (no CPU
        readback, no QImage/NumPy full frame) and returns a
        ``comp_gpu_handle`` layer for the GPU compositor. Failures return
        an explicit ``ok: False`` so the frame falls back with a
        diagnosable reason instead of silently degrading.
        """
        track_info = {
            'opacity': getattr(layer, 'opacity', 1.0),
            'blend_mode': getattr(getattr(layer, 'clip', None), 'blend_mode', None),
            'track_index': getattr(layer, 'track_index', 0),
            'clip_id': getattr(layer, 'clip_id', ''),
            'source_frame': 0,
            'transform': getattr(layer, 'transform', None),
            'crop': getattr(layer, 'crop', None),
        }
        graph_id, graph_data = self._resolve_comp_graph(layer)
        if graph_data is None:
            return {
                'ok': False,
                'fallback_reason': 'comp_gpu_graph_unavailable',
                'layer': layer,
            }

        renderer = self._get_comp_frame_renderer()
        timeline_frame = frame
        context_already_current = bool(
            getattr(self, "_comp_gl_context_current", False))
        gl_mutex = (
            GLContextManager.instance().mutex
            if self._comp_gl_ready and not context_already_current
            else None
        )
        if gl_mutex is not None:
            gl_mutex.lock()
            if not self._comp_gl_context.makeCurrent(self._comp_gl_surface):
                gl_mutex.unlock()
                return {
                    'ok': False,
                    'fallback_reason': 'comp_gpu_makecurrent_failed',
                    'layer': layer,
                }
            self._comp_gl_context_current = True
        try:
            if self._comp_graph_dirty:
                renderer.invalidate_graph_cache()
                self._comp_graph_dirty = False
            handle = renderer.render_comp_clip_gpu(
                graph_data=graph_data,
                timeline_id=graph_data.get('timeline_id'),
                clip_id=graph_data.get('clip_id')
                or graph_data.get('source_clip_id'),
                asset_id=(
                    graph_data.get('source_asset_id')
                    or (graph_data.get('source_refs') or {}).get(
                        'original_asset_id')
                    or layer.asset_id
                ),
                edit_project=self._asset_resolver._project,
                timeline_sequences={
                    getattr(self._timeline_sequence, 'timeline_id', ''): self._timeline_sequence
                } if self._timeline_sequence is not None else None,
                timeline_frame=int(timeline_frame),
                source_frame=getattr(layer, 'source_frame', 0),
                generation=int(getattr(self, '_generation_id', 0) or 0),
            )
            if handle is None or not getattr(handle, "available", False):
                reason = getattr(handle, "fallback_reason", "") \
                    if handle is not None else "renderer returned None"
                # Release any lease the producer may have attached — inside
                # the GL mutex so the pool bookkeeping stays serialized with
                # concurrent renders on the other executor threads.
                if handle is not None:
                    try:
                        handle.release()
                    except Exception:
                        pass
                handle = None
                fallback_reason = f'comp_gpu_unavailable: {reason}'
        finally:
            if gl_mutex is not None:
                self._comp_gl_context.doneCurrent()
                self._comp_gl_context_current = False
                gl_mutex.unlock()

        if handle is None:
            return {
                'ok': False,
                'fallback_reason': fallback_reason,
                'layer': layer,
            }
        return {
            'ok': True,
            'frame_type': 'comp_gpu_handle',
            'gpu_handle': handle,
            'track_info': track_info,
        }

    def _render_comp_clip_frame(self, layer: ClipLayerInfo) -> Optional[np.ndarray]:
        """Render a comp_clip layer through the Composite graph.

        Uses an in-memory LRU cache to avoid re-rendering the same frame
        during playback. Cache is keyed by (graph_id, source_frame).

        Args:
            layer: ClipLayerInfo for the comp_clip layer

        Returns:
            BGR uint8 numpy array, or None if graph data unavailable
        """
        try:
            asset = self._asset_resolver._project.get_asset_by_id(layer.asset_id)

            graph_id = getattr(layer, "composite_id", None) or ""
            if not graph_id:
                if asset is None or not asset.is_comp_clip:
                    return None
                graph_id = asset.metadata.get('graph_id', '')
            if not graph_id:
                logger.debug(f"Comp clip '{layer.asset_id}' has no graph_id")
                return None

            # Cache identity must include both source and timeline time. A
            # comp graph may animate by timeline frame (keyframes, captions,
            # temporal effects), so source_frame alone can replay a wrong
            # image after a move, retime, or seek.
            source_frame = layer.source_frame
            timeline_frame = getattr(self, "_current_render_timeline_frame", None)
            if timeline_frame is None:
                try:
                    timeline_frame = layer.clip.source_to_timeline_frame(source_frame)
                except (ValueError, AttributeError):
                    timeline_frame = source_frame
            caption_input = None
            sequence = getattr(self, "_timeline_sequence", None)
            if sequence is not None and hasattr(sequence, "get_caption_graphic_binding_by_clip"):
                binding = sequence.get_caption_graphic_binding_by_clip(layer.clip_id)
                if binding is not None and binding.composite_id:
                    try:
                        from src.features.composite.adapters.caption_input import (
                            CaptionCompositeInputAdapter,
                        )
                        caption_input = CaptionCompositeInputAdapter.resolve(
                            sequence, binding.caption_cue_id)
                    except (ValueError, TypeError) as exc:
                        logger.warning("Caption Composite input unavailable: %s", exc)
            caption_sig = repr(caption_input) if caption_input is not None else ""
            graph_data = self._asset_resolver._project.compositions.get(graph_id)
            if graph_data is None:
                logger.debug(f"Graph data not found for graph_id='{graph_id}'")
                return None
            cache_key = (
                graph_id,
                int(source_frame),
                caption_sig,
                int(timeline_frame),
                int(graph_data.get("width", 0) or 0),
                int(graph_data.get("height", 0) or 0),
            )
            cached = self._get_comp_clip_cached_frame(cache_key)
            if cached is not None:
                logger.debug(f"Comp clip cache hit: graph={graph_id}, frame={source_frame}")
                return cached

            # Render through CompositeFrameRenderer
            renderer = self._get_comp_frame_renderer()

            # GL context: 仅在共享 context 就绪时 makeCurrent，渲染后立即 doneCurrent。
            # 用全局 mutex 串行化 GL 访问（与 CompositeRenderer / EditGpuPreviewRenderer 一致），
            # 避免 comp_clip 离线渲染与其他后台 GL 线程竞争共享 context。
            gl_mutex = GLContextManager.instance().mutex if self._comp_gl_ready else None
            if gl_mutex is not None:
                gl_mutex.lock()
                if not self._comp_gl_context.makeCurrent(self._comp_gl_surface):
                    gl_mutex.unlock()
                    logger.warning(
                        f"Comp clip makeCurrent failed: graph={graph_id}, frame={layer.source_frame}"
                    )
                    return None
            try:
                if self._comp_graph_dirty:
                    renderer.invalidate_graph_cache()
                    self._comp_graph_dirty = False
                result = renderer.render_comp_clip_frame(
                    graph_data=graph_data,
                    timeline_id=graph_data.get('timeline_id'),
                    clip_id=graph_data.get('clip_id') or graph_data.get('source_clip_id'),
                    asset_id=(
                        graph_data.get('source_asset_id')
                        or (graph_data.get('source_refs') or {}).get('original_asset_id')
                        or layer.asset_id
                    ),
                    edit_project=self._asset_resolver._project,
                    timeline_sequences={
                        getattr(self._timeline_sequence, 'timeline_id', ''): self._timeline_sequence
                    } if self._timeline_sequence is not None else None,
                    timeline_frame=int(timeline_frame),
                    source_frame=layer.source_frame,
                    quality="preview",
                    caption_input=caption_input,
                )
            finally:
                if gl_mutex is not None:
                    self._comp_gl_context.doneCurrent()
                    gl_mutex.unlock()

            if result is not None:
                if result.dtype == np.uint8:
                    # Preview 路径：readback shader 已在 GPU 完成 sRGB OETF +
                    # BGR 重排 + 翻转（CompositeExportRenderer srgb_u8 模式），
                    # 直接就是预览合成器期望的格式，跳过下面的 numpy 链。
                    frame_uint8 = result
                else:
                    # result is BGR float32 [0-1] in SCENE-LINEAR space.
                    # Edit 预览合成器期望 sRGB 编码的 uint8（与解码后的视频帧一致），
                    # 因此必须先做线性→sRGB OETF（IEC 61966-2-1），再 *255。
                    # 交互式合成页面在 viewer 的 display shader 里做这一步；
                    # comp_clip 走 CPU readback 路径，必须在这里补上。
                    # F5.1：OETF 收口到共享单点（真值=provider，GPU 回读
                    # shader 的等价性由 display_encode_contract 测试冻结）。
                    from src.shared.color.display_encode import (
                        linear_bgr_to_srgb_display_u8,
                    )
                    frame_uint8 = linear_bgr_to_srgb_display_u8(result)

                self._store_comp_clip_frame(cache_key, frame_uint8)

                return frame_uint8

            return None

        except Exception as e:
            logger.warning(f"Comp clip render failed for layer {layer.clip_id}: {e}")
            # R-8: Store the failure reason so the composite_layers path
            # can emit a structured composite_error indicator instead of
            # silently falling back to the original source.
            self._last_comp_error = str(e)
            return None

    def _store_comp_clip_frame(
        self,
        cache_key: Tuple[Any, ...],
        frame: np.ndarray,
    ) -> None:
        """Insert one CPU fallback frame into the byte-budgeted LRU."""
        # A 4K uint8 BGR frame is about 25 MiB; a frame-count-only cache could
        # therefore retain multiple gigabytes and stall playback via memory
        # pressure. The explicit byte counter avoids scanning every array on
        # each insertion.
        with self._comp_clip_cache_lock:
            if not isinstance(self._comp_clip_cache, OrderedDict):
                self._comp_clip_cache = OrderedDict(self._comp_clip_cache)
                self._comp_clip_cache_bytes = sum(
                    int(item.nbytes) for item in self._comp_clip_cache.values()
                )
            old_frame = self._comp_clip_cache.pop(cache_key, None)
            if old_frame is not None:
                self._comp_clip_cache_bytes -= int(old_frame.nbytes)
            frame_bytes = int(frame.nbytes)
            if frame_bytes > self._comp_clip_cache_max_bytes:
                return
            self._comp_clip_cache[cache_key] = frame
            self._comp_clip_cache_bytes += frame_bytes
            while (
                len(self._comp_clip_cache) > self._comp_clip_cache_max
                or self._comp_clip_cache_bytes > self._comp_clip_cache_max_bytes
            ):
                _oldest_key, oldest_frame = self._comp_clip_cache.popitem(last=False)
                self._comp_clip_cache_bytes -= int(oldest_frame.nbytes)

    def _get_comp_clip_cached_frame(
        self,
        cache_key: Tuple[Any, ...],
    ) -> np.ndarray | None:
        """Return a CPU fallback frame and refresh its LRU position."""
        with self._comp_clip_cache_lock:
            cached = self._comp_clip_cache.get(cache_key)
            if cached is not None and hasattr(self._comp_clip_cache, "move_to_end"):
                self._comp_clip_cache.move_to_end(cache_key)
            return cached

    def _render_nested_sequence_frame(self, layer: ClipLayerInfo) -> Optional[np.ndarray]:
        """递归求值嵌套序列，返回子序列在 source_frame 处的合成帧。

        通过子序列的 TimelineRenderPlan 计算 frame plan，再用本 worker 的
        _composite_layers 合成。维护 _nested_render_stack 防止循环嵌套。
        """
        try:
            asset = self._asset_resolver._project.get_asset_by_id(layer.asset_id)
            if asset is None or not getattr(asset, 'is_nested_sequence', False):
                return None
            child_tid = asset.nested_timeline_id
            if not child_tid:
                return None

            # 防循环：子序列正在渲染栈中则中止
            if child_tid in self._nested_render_stack:
                logger.warning(f"Circular nested sequence detected: {child_tid}")
                return None

            # 解析子序列
            child_seq = None
            if self._sequence_resolver is not None:
                child_seq = self._sequence_resolver(child_tid)
            if child_seq is None and self._timeline_sequence is not None:
                if self._timeline_sequence.timeline_id == child_tid:
                    child_seq = self._timeline_sequence
            if child_seq is None:
                logger.debug(f"Nested sequence '{child_tid}' not resolvable")
                return None

            # 限制 source_frame 到子序列范围内
            source_frame = layer.source_frame
            child_duration = int(getattr(child_seq, 'duration_frames', 0) or 0)
            if child_duration > 0:
                source_frame = max(0, min(source_frame, child_duration - 1))
            else:
                source_frame = max(0, source_frame)

            # 递归求值：建子 plan 并合成
            self._nested_render_stack.add(child_tid)
            try:
                plan = TimelineRenderPlan(child_seq)
                frame_plan = plan.get_frame_plan(source_frame)
                # O2 复查对齐：嵌套序列与导出路径（_composite_layers 内部分析）
                # 同语义——子序列的层也做保守遮挡裁剪；record=False，权威
                # telemetry 记录在外层帧的 _execute_render。
                child_layers = self._prune_occluded_layers_for_preview(
                    frame_plan.layers, frame_plan.transitions,
                    source_frame, record=False,
                )
                composite = self._composite_layers(
                    child_layers, frame_plan.transitions, source_frame
                )
            finally:
                self._nested_render_stack.discard(child_tid)

            return composite
        except Exception as e:
            logger.warning(f"Nested sequence render failed for layer {layer.clip_id}: {e}")
            return None

    def invalidate_composite_cache(self, composite_id: str | None = None) -> None:
        """Drop rendered Comp Clip frames after an edit to a graph.

        TimelinePreviewRenderer calls this on the UI thread while this worker
        may be producing a frame, so cache access is explicitly synchronized.
        """
        # The frame renderer's persistent GPU cache must be cleared on the
        # worker thread (GL context current) — just flag it here.
        self._comp_graph_dirty = True
        with self._comp_clip_cache_lock:
            # Older integrations may replace the mapping directly with a
            # plain dict. Rebuild the accounting before subtracting an
            # evicted frame so the byte budget cannot drift or under-count
            # retained frames.
            if not isinstance(self._comp_clip_cache, OrderedDict):
                self._comp_clip_cache = OrderedDict(self._comp_clip_cache)
                self._comp_clip_cache_bytes = sum(
                    int(frame.nbytes) for frame in self._comp_clip_cache.values()
                )
            if composite_id is None:
                self._comp_clip_cache.clear()
                self._comp_clip_cache_bytes = 0
                return
            stale_keys = [
                key for key in self._comp_clip_cache
                if key[0] == composite_id
            ]
            for key in stale_keys:
                stale_frame = self._comp_clip_cache.pop(key)
                self._comp_clip_cache_bytes -= int(stale_frame.nbytes)

    def _get_layer_frame(self, layer: ClipLayerInfo) -> Optional[np.ndarray]:
        """
        获取单个层的帧数据。

        Args:
            layer: ClipLayerInfo

        Returns:
            帧数据（numpy.ndarray BGR uint8），或 None
        """
        # Motion Graphic clips reference a shared scene, not a Media Pool file.
        from src.features.teaching_animation.host_adapters import motion_graphic_scene_id
        scene_id = motion_graphic_scene_id(layer.asset_id)
        if scene_id is not None:
            cached = self._get_motion_graphic_render_cache_frame(layer)
            if cached is not None:
                return cached
            if self._playback_active:
                # 播放中磁盘缓存未就绪时绝不同步打开浏览器捕获（最长可
                # 阻塞本 worker 10 秒并拖垮所有素材的帧率）。改用静帧：
                # 命中内存场景缓存最近帧则显示之，否则在后台渲染一张，
                # 就绪前该层暂时缺席（与渲染失败跳过层的表现一致）。
                return self._get_motion_graphic_static_frame(layer, scene_id)
            from src.features.teaching_animation.host_adapters import render_teaching_scene_frame
            from src.features.teaching_animation.scene_store import get_shared_scene_store
            scene = get_shared_scene_store().get(scene_id)
            try:
                rgba = render_teaching_scene_frame(
                    scene_id,
                    int(layer.source_frame),
                    context=self._motion_graphic_render_context(scene),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Teaching Animation preview frame unavailable for %s: %s", scene_id, exc)
                return None
            return self._convert_rgba_to_preview_frame(rgba)

        # B7: 占位符片段走专用静态帧（彩条+斜纹），绝不触发解码器。
        # 三路（CPU 预览 / CPU 导出 / GPU 预览经 CPU 帧路径）共用同一帧。
        try:
            from src.features.edit.core.placeholder_frame import (
                is_placeholder_asset,
                render_placeholder_frame_for_asset,
            )
            asset_for_ph = (
                self._project.get_asset_by_id(layer.asset_id)
                if getattr(self, "_project", None) is not None
                else None
            )
            if asset_for_ph is not None and is_placeholder_asset(asset_for_ph):
                return render_placeholder_frame_for_asset(asset_for_ph, 1920, 1080)
        except (ImportError, AttributeError):
            pass

        # Caption graphic clips are ordinary video layers with a synthetic
        # asset id. Resolve them from the source cue before consulting the
        # media resolver so preview never attempts to decode a fake file.
        sequence = getattr(self, "_sequence", None)
        binding = (
            sequence.get_caption_graphic_binding_by_clip(layer.clip_id)
            if sequence is not None and hasattr(sequence, "get_caption_graphic_binding_by_clip")
            else None
        )
        if binding is not None and isinstance(getattr(binding, "caption_cue_id", None), str) and binding.caption_cue_id:
            if binding.composite_id:
                composite_frame = self._render_comp_clip_frame(layer)
                if composite_frame is not None:
                    return composite_frame
                logger.warning(
                    "Caption graphic Composite render failed; falling back to template "
                    "binding=%s composite_id=%s",
                    binding.binding_id, binding.composite_id,
                )
            cue = sequence.get_subtitle_clip_by_id(binding.caption_cue_id)
            if cue is not None:
                from src.features.edit.core.captions.graphics import (
                    render_caption_graphic_rgba,
                    render_snapshot_for_binding,
                    template_parameters,
                )
                template = sequence.get_caption_graphic_template_registry().get(
                    binding.template_id, binding.template_version
                )
                if template is None:
                    logger.error(
                        "Caption graphic template missing: %s@%s",
                        binding.template_id, binding.template_version,
                    )
                    return None
                track = sequence.get_track_by_id(cue.track_id)
                rgba = render_caption_graphic_rgba(
                    cue, track, template,
                    template_parameters(template, binding.parameters),
                    self._decode_target_size[0], self._decode_target_size[1],
                    layer.source_frame,
                    render_snapshot_for_binding(cue, binding),
                )
                if rgba is None:
                    return None
                rgb = np.clip(rgba[..., :3], 0.0, 1.0)
                alpha = np.clip(rgba[..., 3:4], 0.0, 1.0)
                # Caption graphics are transparent layers even when the final
                # preview is opaque.  Returning pre-multiplied 3-channel BGR
                # made the CPU compositor treat the transparent canvas as an
                # opaque black rectangle.  Keep straight BGRA until the shared
                # layer compositor consumes alpha.
                return np.rint(np.concatenate(
                    [rgb[..., ::-1], alpha], axis=-1) * 255.0
                ).astype(np.uint8)

        # Generator handling (CPU fallback path).
        asset_type = self._asset_resolver.get_asset_type(layer.asset_id)
        if asset_type == 'adjustment':
            # Adjustment clips have no source frame; their effect chain is
            # applied as a post-composite pass in _composite_layers. Returning
            # None here keeps them out of the regular layer blend.
            return None
        if asset_type == 'solid':
            # Solid Color generator: paint a flat colour frame at preview
            # resolution so it composites like any other video layer.
            asset = self._asset_resolver.get_asset(layer.asset_id)
            rgb = (0.0, 0.6, 0.8)
            if asset is not None:
                sc = getattr(asset, "solid_color", None)
                if sc is not None:
                    rgb = sc
            vw, vh = self._decode_target_size
            bgr = (
                int(round(rgb[2] * 255.0)),
                int(round(rgb[1] * 255.0)),
                int(round(rgb[0] * 255.0)),
            )
            return np.full((vh, vw, 3), bgr, dtype=np.uint8)

        if getattr(layer, "composite_id", None):
            # R-1: Check composite bypass - if bypassed, skip composite rendering
            if getattr(layer, "composite_bypassed", False):
                logger.debug(f"Composite bypassed for clip={layer.clip_id}, using source")
            else:
                # Composite-bound clips render through their graph even if their
                # media asset has not been promoted/replaced as a comp_clip yet.
                frame_data = self._render_comp_clip_frame(layer)
                if frame_data is not None:
                    return frame_data
                # Composite render failed — do NOT silently fall back to original source.
                # Error overlay will be shown by the calling method.
                logger.warning(
                    "Composite-bound clip render failed; NOT falling back to original source "
                    f"clip={layer.clip_id}, composite_id={layer.composite_id}"
                )
                return None

        # 获取素材路径
        asset_path = self._asset_resolver.get_asset_path(layer.asset_id)
        if asset_path is None:
            return None

        # 获取素材类型
        asset_type = self._asset_resolver.get_asset_type(layer.asset_id)

        frame_data: Optional[np.ndarray] = None

        if asset_type == "nested_sequence":
            # 嵌套序列：递归求值子序列的对应帧（CPU 路径，与 comp_clip 一致）
            frame_data = self._render_nested_sequence_frame(layer)
            return frame_data

        if asset_type == "comp_clip":
            # Comp Clip: try to render through the Composite graph first.
            frame_data = self._render_comp_clip_frame(layer)
            if frame_data is not None:
                return frame_data

            # Composite graph render failed.
            # Only fall back to original source if user explicitly bypassed composite.
            # Otherwise, return None to trigger error overlay display.
            bypassed = getattr(layer, 'composite_bypassed', False)
            if not bypassed:
                # Error already emitted by _composite_layers / _render_comp_clip_frame
                return None

            # Bypass mode: show original source material
            comp_clip_asset = self._asset_resolver._project.get_asset_by_id(layer.asset_id)
            if comp_clip_asset and comp_clip_asset.is_comp_clip:
                orig_asset_id = comp_clip_asset.metadata.get('original_asset_id', '')
                if orig_asset_id:
                    orig_path = self._asset_resolver.get_asset_path(orig_asset_id)
                    orig_type = self._asset_resolver.get_asset_type(orig_asset_id)
                    if orig_path and orig_type == "video":
                        frame_data = self._get_video_frame(
                            orig_path,
                            layer.source_frame,
                            orig_asset_id,
                            layer.decoder_stream_key,
                        )
                    elif orig_path and orig_type == "image":
                        frame_data = self._get_image_frame(orig_path)
            # If no original source (empty comp clip), return None
            if frame_data is None:
                return None

        elif asset_type == "video":
            frame_data = self._get_video_frame(
                asset_path,
                layer.source_frame,
                layer.asset_id,
                layer.decoder_stream_key,
                raw_params=getattr(getattr(layer, 'clip', None), 'raw', None),
            )
        elif asset_type == "image":
            frame_data = self._get_image_frame(asset_path)
        else:
            logger.warning(f"Unsupported asset type: {asset_type}")
            return None

        if frame_data is None:
            return None

        # 确保返回 uint8 格式
        if frame_data.dtype != np.uint8:
            if np.issubdtype(frame_data.dtype, np.integer):
                max_val = float(np.iinfo(frame_data.dtype).max)
                frame_data = (frame_data.astype(np.float32) * (255.0 / max_val)).astype(np.uint8)
            else:
                frame_data = np.clip(frame_data * 255.0, 0, 255).astype(np.uint8)

        return frame_data

    def _init_composite_buffer(
        self,
        frame_data: np.ndarray,
        viewport_w: int,
        viewport_h: int
    ) -> np.ndarray:
        """
        初始化合成缓冲区。

        Args:
            frame_data: 第一帧数据（uint8 BGR）
            viewport_w: 视口宽度
            viewport_h: 视口高度

        Returns:
            合成缓冲区（uint8 BGR）
        """
        # 确保尺寸匹配
        if frame_data.shape[0] != viewport_h or frame_data.shape[1] != viewport_w:
            frame_data = self._resize_frame_to_viewport(frame_data, viewport_w, viewport_h)

        return frame_data.copy()

    def _resize_frame_to_viewport(
        self,
        frame_data: np.ndarray,
        viewport_w: int,
        viewport_h: int
    ) -> np.ndarray:
        """
        调整帧尺寸以匹配视口，同时保持原始宽高比。

        使用 letterboxing：先按宽高比缩放到视口内最大尺寸，
        然后居中放置在黑色画布上。

        Args:
            frame_data: 帧数据（uint8 或 float32）
            viewport_w: 视口宽度
            viewport_h: 视口高度

        Returns:
            调整后的帧数据（viewport_w x viewport_h），视频居中并带有 letterboxing
        """
        h, w = frame_data.shape[:2]

        if h == viewport_h and w == viewport_w:
            return frame_data

        import cv2

        # 计算保持宽高比的最大缩放因子
        scale = min(viewport_w / w, viewport_h / h)
        new_w = int(w * scale)
        new_h = int(h * scale)

        # 保持宽高比缩放
        if frame_data.dtype == np.uint8:
            resized = cv2.resize(frame_data, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        else:
            resized = self._resize_frame_float(frame_data, new_w, new_h)

        # 如果缩放后恰好匹配视口，直接返回
        if new_w == viewport_w and new_h == viewport_h:
            return resized

        # Letterboxing：创建黑色画布，居中放置缩放后的帧
        channels = frame_data.shape[2] if frame_data.ndim == 3 else 1
        canvas = np.zeros((viewport_h, viewport_w, channels), dtype=frame_data.dtype)

        x_offset = (viewport_w - new_w) // 2
        y_offset = (viewport_h - new_h) // 2
        canvas[y_offset:y_offset + new_h, x_offset:x_offset + new_w] = resized

        return canvas

    def _apply_clip_transform_cpu(
        self,
        frame_data: np.ndarray,
        transform_tuple: Any,
        viewport_w: int,
        viewport_h: int,
        coordinate_w: Optional[int] = None,
        coordinate_h: Optional[int] = None,
    ) -> np.ndarray:
        """Apply a clip's pixel-space TransformParams (8-tuple) via warpAffine.

        Mirrors the GPU path (``_transform_to_ndc_mat3_bytes``) but operates in
        pixel space with ``cv2.warpAffine``. translate/pivot are already in
        pixels (Y-down); composition (image space, Y-down):
            M = T(translate) · T(pivot) · R(θ) · S(scale) · T(−pivot)
        pivot_mode="center" resolves to the viewport center.

        Identity tuples skip the warp entirely (no-op fast path). The output
        keeps the same viewport size with transparent/black borders so it
        composites correctly on top of the lower layers.

        Args:
            frame_data: viewport-sized BGR/BGRA frame (uint8 or float32).
            transform_tuple: 8-tuple (translate_x, translate_y, scale_x,
                scale_y, rotation, pivot_mode, pivot_x, pivot_y), or None.
            viewport_w, viewport_h: viewport dimensions in pixels.

        Returns:
            Transformed frame (same dtype/shape as input).
        """
        if not transform_tuple:
            return frame_data
        try:
            if len(transform_tuple) != 8:
                return frame_data
            tx = float(transform_tuple[0])
            ty = float(transform_tuple[1])
            sx = float(transform_tuple[2])
            sy = float(transform_tuple[3])
            rot_deg = float(transform_tuple[4])
            pivot_mode = str(transform_tuple[5])
            piv_x = float(transform_tuple[6])
            piv_y = float(transform_tuple[7])
        except (TypeError, ValueError):
            return frame_data

        # Identity fast path.
        if (abs(tx) < 1e-4 and abs(ty) < 1e-4
                and abs(sx - 1.0) < 1e-4 and abs(sy - 1.0) < 1e-4
                and abs(rot_deg) < 1e-4 and pivot_mode == "center"):
            return frame_data

        import cv2

        # TransformParams are stored in native sequence pixels, while the CPU
        # frame is already resized to the preview canvas. Convert the pivot
        # and translation into output pixels before building the affine. This
        # mirrors the GPU matrix's separate native coordinate size.
        coordinate_w = max(1, int(coordinate_w or viewport_w))
        coordinate_h = max(1, int(coordinate_h or viewport_h))
        output_scale_x = viewport_w / float(coordinate_w)
        output_scale_y = viewport_h / float(coordinate_h)

        # Resolve pivot in native pixel space (Y-down), then map to output.
        if pivot_mode == "custom":
            anchor_px_x = piv_x * output_scale_x
            anchor_px_y = piv_y * output_scale_y
        else:  # "center"
            anchor_px_x, anchor_px_y = viewport_w / 2.0, viewport_h / 2.0

        # Build the 2×3 affine: rotate+scale around pivot, then translate.
        rad = math.radians(rot_deg)
        cos_r = math.cos(rad)
        sin_r = math.sin(rad)
        m00 = cos_r * sx
        m01 = -sin_r * sy
        m10 = sin_r * sx
        m11 = cos_r * sy
        m02 = anchor_px_x - m00 * anchor_px_x - m01 * anchor_px_y + tx * output_scale_x
        m12 = anchor_px_y - m10 * anchor_px_x - m11 * anchor_px_y + ty * output_scale_y

        affine = np.array([[m00, m01, m02], [m10, m11, m12]], dtype=np.float32)

        return cv2.warpAffine(
            frame_data, affine, (viewport_w, viewport_h),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=(0, 0, 0, 0) if frame_data.shape[-1] == 4 else (0, 0, 0),
        )

    def _apply_crop_cpu(
        self,
        frame_data: np.ndarray,
        layer: "ClipLayerInfo",
    ) -> np.ndarray:
        """Apply a clip's CropParams matte to a CPU-rendered frame (FX-M06).

        Mirrors the GPU ``_apply_crop`` pass but operates on the CPU fallback's
        numpy frames (BGR/BGRA uint8). Uses the shared ``crop_render`` math so
        the CPU output matches the GPU shader within tolerance. Identity crops
        (no crop tuple, or all-zero margins) return the frame unchanged.

        Runs *before* grading/effects to match the GPU order (the crop is a
        geometric stage applied to the source, not a post-grade filter).
        """
        crop_tuple = getattr(layer, "crop", None)
        if not crop_tuple:
            return frame_data
        from src.features.edit.core.clip_params import CropParams
        from src.features.edit.core import crop_render
        try:
            crop = CropParams(
                left=float(crop_tuple[0]), right=float(crop_tuple[1]),
                top=float(crop_tuple[2]), bottom=float(crop_tuple[3]),
                feather=float(crop_tuple[4]),
            )
        except (TypeError, ValueError, IndexError):
            return frame_data
        if crop_render.crop_is_identity(crop):
            return frame_data
        # crop_render.apply_crop_bgr handles opaque BGR uint8. For BGRA (4-ch)
        # frames we apply the premultiplied RGBA path instead.
        if frame_data.ndim == 3 and frame_data.shape[-1] == 4:
            rect = crop_render.crop_rect_for_size(
                crop, frame_data.shape[1], frame_data.shape[0])
            out = crop_render.apply_crop_premultiplied(
                frame_data.astype(np.float32) / 255.0, rect)
            return np.clip(out * 255.0, 0, 255).astype(np.uint8)
        return crop_render.apply_crop_bgr(frame_data, crop)

    def _resize_frame_float(
        self,
        frame_data: np.ndarray,
        target_w: int,
        target_h: int
    ) -> np.ndarray:
        """
        缩放 float32 帧数据。

        Args:
            frame_data: 帧数据（float32）
            target_w: 目标宽度
            target_h: 目标高度

        Returns:
            缩放后的帧数据
        """
        import cv2
        return cv2.resize(frame_data, (target_w, target_h), interpolation=cv2.INTER_LINEAR)

    def _alpha_blend(
        self,
        base: np.ndarray,
        overlay: np.ndarray,
        opacity: float
    ) -> np.ndarray:
        """
        Alpha 混合两层（uint8 路径）。

        使用 cv2.addWeighted 实现高效的 uint8 alpha 混合，
        避免昂贵的 float32 转换。

        Args:
            base: 底层（uint8 BGR）
            overlay: 上层（uint8 BGR）
            opacity: 上层透明度 [0-1]

        Returns:
            混合结果（uint8 BGR）
        """
        import cv2

        # 确保 overlay 尺寸与 base 匹配
        if overlay.shape[:2] != base.shape[:2]:
            overlay = cv2.resize(overlay, (base.shape[1], base.shape[0]),
                                interpolation=cv2.INTER_LINEAR)

        # cv2.addWeighted 在 uint8 中高效工作
        return cv2.addWeighted(overlay, opacity, base, 1.0 - opacity, 0)

    @staticmethod
    def _resolve_user_dip_color(trans: "TransitionBlendInfo") -> Tuple[float, float, float]:
        """AUD-19202：解析用户自定义 dip 转场的颜色参数。

        render plan 仅为内置 ``dip_to_color`` 提取 blend_color；base 为 dip
        的 user_* 类型在此按同一规则解析（dict / list / "#RRGGBB"），
        缺省回退黑色。
        """
        params: Optional[Dict[str, Any]] = getattr(trans.transition, "params", None)
        try:
            from src.features.edit.core.custom_transitions import (
                merge_user_transition_params,
            )
            resolved = merge_user_transition_params(
                trans.transition_type, params)
            if resolved is not None:
                params = resolved[1]
        except ImportError:
            pass
        color_params: Any = (params or {}).get(
            "color", {"r": 0.0, "g": 0.0, "b": 0.0})
        if isinstance(color_params, dict):
            return (
                float(color_params.get("r", 0.0)),
                float(color_params.get("g", 0.0)),
                float(color_params.get("b", 0.0)),
            )
        if isinstance(color_params, (list, tuple)) and len(color_params) >= 3:
            return (
                float(color_params[0]),
                float(color_params[1]),
                float(color_params[2]),
            )
        if isinstance(color_params, str):
            value = color_params.strip().lstrip("#")
            if len(value) == 6:
                try:
                    return (
                        int(value[0:2], 16) / 255.0,
                        int(value[2:4], 16) / 255.0,
                        int(value[4:6], 16) / 255.0,
                    )
                except ValueError as e:
                    logger.debug(
                        f"Invalid dip color hex {color_params!r}, "
                        f"falling back to black: {e}"
                    )
        return (0.0, 0.0, 0.0)

    def _blend_with_color(
        self,
        frame: np.ndarray,
        color: Tuple[float, float, float],
        factor: float
    ) -> np.ndarray:
        """
        将帧与颜色混合（Dip To Color）。

        Args:
            frame: 帧数据（uint8 BGR）
            color: 颜色值 (R, G, B) [0-1]
            factor: 混合比例 [0-1]

        Returns:
            混合结果（uint8 BGR）
        """
        import cv2

        # color 是 RGB，需要转换为 BGR uint8
        bgr_color = np.array([color[2] * 255, color[1] * 255, color[0] * 255], dtype=np.uint8)

        # 创建颜色层
        color_layer = np.full_like(frame, bgr_color, dtype=np.uint8)

        # Alpha blend using addWeighted
        return cv2.addWeighted(color_layer, factor, frame, 1.0 - factor, 0)

    def _clear_direct_preview_sources(self) -> None:
        """Close lightweight direct and render-cache preview sources.

        在飞的读取（worker 持句柄解码中）只标记关闭，由其 release()
        时真正释放，避免 PyAV 容器在解码中被释放导致段错误。
        """
        with self._direct_preview_lock:
            handles = list(self._direct_preview_sources.values()) + list(
                self._render_cache_sources.values()
            )
            self._direct_preview_sources.clear()
            self._direct_preview_last_frames.clear()
            self._render_cache_sources.clear()
            self._render_cache_last_frames.clear()
        for handle in handles:
            if isinstance(handle, _GuardedPreviewSource):
                handle.retire()
            else:
                try:
                    handle.close()
                except Exception:
                    pass

    def _motion_graphic_render_context(self, scene: Any) -> "RenderContext":
        """Build the RenderContext shared by the sync preview and poster paths.

        HTML/CSS is authored in scene coordinates.  A proxy preview must not
        become the browser's CSS viewport: rendering a 1920x1080 design
        directly in a 960x540 browser changes/crops layout before the
        compositor can scale it.  Render at the authored viewport, then
        downsample the finished RGBA frame to the preview target.
        """
        from src.features.teaching_animation.core.contracts import RenderContext
        target_w, target_h = self._decode_target_size
        render_w = max(1, int(getattr(scene, "width", 0) or target_w))
        render_h = max(1, int(getattr(scene, "height", 0) or target_h))
        return RenderContext(
            fps=24.0,
            width=render_w,
            height=render_h,
            transparent=True,
            # Motion Graphic HTML scenes pass the creation/edit renderability
            # gate before they enter the timeline.  Playback must preserve an
            # authored solid-color page (including intentional black/transparent
            # slides), while the strict gate remains available to callers that
            # explicitly leave this flag disabled.
            allow_uniform_frames=True,
        )

    def _convert_rgba_to_preview_frame(self, rgba: Any) -> np.ndarray:
        """Convert a teaching-animation RGBA frame to BGR at the decode target."""
        target_w, target_h = self._decode_target_size
        rgba_array = np.frombuffer(rgba.data, dtype=np.uint8).reshape((rgba.height, rgba.width, 4))
        frame = rgba_array[..., [2, 1, 0, 3]].copy()
        if frame.shape[1] != target_w or frame.shape[0] != target_h:
            frame = self._resize_frame_to_viewport(frame, target_w, target_h)
        return frame

    def _get_motion_graphic_static_frame(
        self, layer: ClipLayerInfo, scene_id: str,
    ) -> Optional[np.ndarray]:
        """Frozen-frame fallback while an MG clip's disk cache is unavailable.

        Returns the memory scene-cache frame nearest the requested source
        frame (converted once and memoised), or ``None`` after scheduling a
        background single-frame render so the poster appears on later frames
        without ever blocking this worker.
        """
        from src.features.teaching_animation.host_adapters import (
            reset_teaching_scene_frame_schedule,
        )
        from src.features.teaching_animation.scene_cache import (
            get_shared_scene_cache_coordinator,
        )
        nearest = get_shared_scene_cache_coordinator().cache.nearest_frame_for_scene(
            scene_id, int(layer.source_frame),
        )
        if nearest is None:
            self._schedule_motion_graphic_static_frame(layer, scene_id)
            return None
        frame_no, rendered = nearest
        target_w, target_h = self._decode_target_size
        memo_key = (scene_id, frame_no, (target_w, target_h))
        frame = self._mg_static_frames.get(memo_key)
        if frame is None:
            from src.features.teaching_animation.render.frame_adapter import FrameAdapter
            try:
                rgba = FrameAdapter.from_rendered_frame(rendered)
            except (KeyError, TypeError, ValueError):
                return None
            frame = self._convert_rgba_to_preview_frame(rgba)
            if len(self._mg_static_frames) >= self._mg_static_frames_max:
                # AUD-18906：UI 线程（set_playback_active → 播放停止）可能
                # 恰好在 len 判定之后、取最旧键之前 clear() 整表——
                # next(iter(空 dict)) 抛 StopIteration，沿 _get_layer_frame
                # 上抛到 _execute_render 兜底 → 该帧 render_error（MG 层
                # 播放→暂停切换瞬间丢一帧）。防御空表即可，淘汰语义不变。
                try:
                    oldest_key = next(iter(self._mg_static_frames))
                except StopIteration:
                    oldest_key = None
                if oldest_key is not None:
                    self._mg_static_frames.pop(oldest_key, None)
            self._mg_static_frames[memo_key] = frame
        else:
            # 成功命中后重置该场景的后台调度/重试状态，播放再次进入时
            # 不会带着失败计数。
            reset_teaching_scene_frame_schedule(scene_id)
        return frame

    def _schedule_motion_graphic_static_frame(
        self, layer: ClipLayerInfo, scene_id: str,
    ) -> None:
        """Render one poster frame for ``scene_id`` off-thread (deduped)."""
        from src.features.teaching_animation.host_adapters import (
            schedule_teaching_scene_frame,
        )
        from src.features.teaching_animation.scene_store import get_shared_scene_store
        scene = get_shared_scene_store().get(scene_id)
        if scene is None:
            return
        schedule_teaching_scene_frame(
            scene_id,
            int(layer.source_frame),
            context=self._motion_graphic_render_context(scene),
        )

    def _get_motion_graphic_render_cache_frame(
        self, layer: ClipLayerInfo,
    ) -> Optional[np.ndarray]:
        """Read one frame from a VALID Motion Graphic disk cache, if present.

        The cache file is indexed in timeline order while ``ClipLayerInfo``
        carries a source-relative frame.  Mapping through the clip's timeline
        start keeps trimmed clips and legacy manifests aligned.  A decode
        failure simply falls through to the deterministic scene-cache/browser
        path; a bad cache must never make the clip disappear.
        """
        project = getattr(self._asset_resolver, "_project", None)
        sequence = getattr(self, "_timeline_sequence", None) or getattr(self, "_sequence", None)
        getter = getattr(project, "get_render_cache_for_clip", None)
        if project is None or sequence is None or not callable(getter):
            return None
        timeline_id = str(getattr(sequence, "timeline_id", "") or "")
        if not timeline_id:
            return None
        try:
            from src.features.edit.core.render_cache import (
                RenderCacheManifest, RenderCacheStatus,
                build_render_cache_fingerprint,
            )
            current_fingerprint = build_render_cache_fingerprint(
                project, timeline_id, layer.clip,
            )
            try:
                manifest = getter(
                    timeline_id,
                    str(layer.clip_id),
                    render_fingerprint=current_fingerprint,
                )
            except TypeError:
                # Keep lightweight project adapters compatible with the
                # pre-fingerprint two-argument getter.
                manifest = None
            if manifest is None:
                manifest = getter(timeline_id, str(layer.clip_id))
            if isinstance(manifest, dict):
                manifest = RenderCacheManifest.from_dict(manifest)
            # Timeline duplication intentionally creates a new ClipInstance
            # while keeping the same Motion Graphic asset/scene.  Such a
            # duplicate has no own manifest yet; reuse the newest valid scene
            # recording instead of falling back to the slower HTML renderer.
            manifest_is_shared = False
            if manifest is None:
                asset = (getattr(project, "assets", {}) or {}).get(
                    str(getattr(layer, "asset_id", ""))
                )
                scene_id = str(
                    ((getattr(asset, "metadata", {}) or {}).get("scene_id", ""))
                    or ""
                )
                if scene_id:
                    for raw in reversed(
                        tuple((getattr(project, "render_cache_manifests", {}) or {}).values())
                    ):
                        candidate = (
                            RenderCacheManifest.from_dict(raw)
                            if isinstance(raw, dict) else raw
                        )
                        if (
                            isinstance(candidate, RenderCacheManifest)
                            and candidate.timeline_id == timeline_id
                            and candidate.render_kind == "motion_graphic"
                            and candidate.scene_id == scene_id
                            and candidate.status == RenderCacheStatus.VALID
                            and candidate.path
                            and os.path.isfile(os.path.abspath(str(candidate.path)))
                        ):
                            manifest = candidate
                            manifest_is_shared = True
                            break
            if not isinstance(manifest, RenderCacheManifest):
                return None
            if manifest.render_kind != "motion_graphic":
                return None
            if manifest.status != RenderCacheStatus.VALID or not manifest.path:
                return None
            # 指纹豁免与 render_cache_status 一致：共享 manifest 录制自
            # 同场景的另一个 ClipInstance（复制片段），其指纹携带原 clip
            # 的 clip_id/timeline_start，与本 clip 必然不等——若不豁免，
            # 上面的共享回退对新式 manifest 永远失效，复制 MG 片段退回
            # 较慢的 HTML 渲染路径。
            if (
                manifest.render_fingerprint
                and not manifest_is_shared
                and manifest.render_fingerprint != current_fingerprint
            ):
                return None
            cache_path = os.path.abspath(str(manifest.path))
            if not os.path.isfile(cache_path):
                return None

            # A cache recording is indexed by its own timeline-relative
            # frames.  For an authored-duration clip this is identical to the
            # scene/source frame, which preserves the existing moved-clip
            # behaviour.  A Shift-retimed clip, however, has a different
            # duration and the cache worker normalises scene frames into that
            # shorter/longer recording; indexing by ``source_frame`` would
            # immediately run past a shortened cache or play it too early.
            clip = layer.clip
            scene_duration = max(1, int(getattr(clip, "source_duration", 1) or 1))
            if int(getattr(clip, "timeline_duration", scene_duration) or scene_duration) != scene_duration:
                timeline_frame = getattr(self, "_current_render_timeline_frame", None)
                if timeline_frame is None:
                    cache_frame = int(layer.source_frame)
                else:
                    cache_frame = int(timeline_frame) - int(getattr(clip, "timeline_start", 0) or 0)
            else:
                cache_frame = int(layer.source_frame)
            duration = int(manifest.duration_frames or 0)
            if cache_frame < 0 or (duration > 0 and cache_frame >= duration):
                return None

            target_size = self._decode_target_size
            key = (cache_path, target_size)
            with self._direct_preview_lock:
                handle = self._render_cache_sources.get(key)
                previous = self._render_cache_last_frames.get(key)
                if previous is not None and previous[0] == cache_frame:
                    return previous[1]
            if handle is None or handle.closed:
                from src.shared.media.ffmpeg_source import FFmpegMediaSource
                source = FFmpegMediaSource(
                    path=cache_path,
                    target_size=target_size,
                    preserve_planes=False,
                    output_mode="packed_rgb",
                    output_format="rgb24",
                )
                source.open()
                new_handle = _GuardedPreviewSource(source)
                with self._direct_preview_lock:
                    existing = self._render_cache_sources.get(key)
                    if existing is not None and not existing.closed:
                        new_handle.retire()
                        handle = existing
                        self._render_cache_sources.move_to_end(key)
                    else:
                        self._render_cache_sources[key] = new_handle
                        handle = new_handle
                        self._retire_oldest_source_if_over(
                            self._render_cache_sources,
                            self._render_cache_last_frames,
                            self._RENDER_CACHE_SOURCES_MAX,
                        )
            else:
                with self._direct_preview_lock:
                    if key in self._render_cache_sources:
                        self._render_cache_sources.move_to_end(key)

            source = handle.acquire()
            if source is None:
                return None
            try:
                decoded = source.read_frame(cache_frame)
            finally:
                handle.release()
            frame_data = self._extract_frame_data(decoded)
            if frame_data is None:
                return None
            with self._direct_preview_lock:
                self._render_cache_last_frames[key] = (cache_frame, frame_data)
            return frame_data
        except Exception as exc:  # noqa: BLE001 — fallback is intentional
            logger.debug(
                "Motion Graphic render-cache playback failed for %s: %s",
                getattr(layer, "clip_id", ""), exc,
            )
            # Do not keep a decoder that failed mid-stream; the next frame can
            # retry the scene-cache path without repeatedly throwing here.
            try:
                if "key" in locals():
                    with self._direct_preview_lock:
                        failed = self._render_cache_sources.pop(key, None)
                        self._render_cache_last_frames.pop(key, None)
                    if failed is not None:
                        if isinstance(failed, _GuardedPreviewSource):
                            failed.retire()
                        else:
                            failed.close()
            except Exception:
                pass
            return None

    def _direct_preview_source_key(
        self,
        asset_path: str,
        asset_id: str,
        clip_key: Optional[str],
    ) -> tuple[str, str, Tuple[int, int]]:
        stream_key = f"{clip_key}:{asset_id}" if clip_key else asset_id
        return (stream_key, asset_path, self._decode_target_size)

    def _retire_oldest_source_if_over(
        self,
        sources: "OrderedDict[tuple, Any]",
        last_frames: Optional[Dict[tuple, Any]],
        cap: int,
    ) -> None:
        """超出上限时退休最旧的直连源（调用方需持有 _direct_preview_lock）。"""
        while len(sources) > cap:
            oldest_key, oldest = sources.popitem(last=False)
            if last_frames is not None:
                last_frames.pop(oldest_key, None)
            if isinstance(oldest, _GuardedPreviewSource):
                oldest.retire()
            elif oldest is not None:
                try:
                    oldest.close()
                except Exception:
                    pass
            playback_trace("worker.direct_preview_source_lru_evicted", key=str(oldest_key))

    def _get_or_create_direct_preview_source(
        self,
        asset_path: str,
        asset_id: str,
        clip_key: Optional[str],
    ) -> tuple[tuple[str, str, Tuple[int, int]], Any] | tuple[None, None]:
        """
        Return a low-cost FFmpeg source for edit preview playback.

        This intentionally bypasses VideoFrameProviderService for plain media
        preview.  Editing needs responsive 8-bit display frames more than
        grading/compositing fidelity; effects/render-cache paths can still use
        the full pipeline when needed.
        """
        if not asset_path or not os.path.isfile(asset_path):
            return None, None

        key = self._direct_preview_source_key(asset_path, asset_id, clip_key)
        with self._direct_preview_lock:
            existing = self._direct_preview_sources.get(key)
            if existing is not None and not existing.closed:
                self._direct_preview_sources.move_to_end(key)
                return key, existing
            if key in self._direct_preview_failed:
                # 负缓存：此 key 曾创建失败（如 Topos 私有编码无法产出
                # packed RGB 源），不再逐帧重试 av.open，直接回落主渲染路。
                return None, None

        # The plain-media fast path below is intentionally FFmpeg-only.
        # Topos MOV is a TPIC stream and must fall through to the main decode
        # route, whose factory selects ToposMediaSource. Mark it failed here
        # before constructing FFmpegMediaSource so project-open never logs a
        # misleading decoder error for a supported asset.
        try:
            from src.shared.media.topos_source import is_topos_movie
            if is_topos_movie(asset_path):
                with self._direct_preview_lock:
                    self._direct_preview_failed.add(key)
                playback_trace(
                    "worker.direct_preview_source_skipped",
                    asset=asset_path,
                    reason="topos_native_decoder",
                )
                return None, None
        except ImportError:
            # A build without the optional Topos decoder retains the normal
            # FFmpeg attempt, which reports its unsupported-codec error.
            pass

        try:
            from src.shared.media.ffmpeg_source import FFmpegMediaSource

            source = FFmpegMediaSource(
                path=asset_path,
                target_size=key[2],
                preserve_planes=False,
                output_mode="packed_rgb",
                # Edit playback is an 8-bit display path.  Without this
                # override a 10-bit H.265/ProRes source produces rgb48le
                # (uint16), and _get_video_frame converts the full 1080p
                # buffer to uint8 again on every request—even when the
                # direct-frame cache already contains the same frame.
                output_format="rgb24",
            )
            source.open()
        except Exception as e:
            logger.debug(f"Direct edit preview source failed for {asset_path}: {e}")
            with self._direct_preview_lock:
                self._direct_preview_failed.add(key)
            return None, None

        handle = _GuardedPreviewSource(source)
        with self._direct_preview_lock:
            existing = self._direct_preview_sources.get(key)
            if existing is not None and not existing.closed:
                handle.retire()
                self._direct_preview_sources.move_to_end(key)
                return key, existing
            self._direct_preview_sources[key] = handle
            self._retire_oldest_source_if_over(
                self._direct_preview_sources,
                self._direct_preview_last_frames,
                self._DIRECT_PREVIEW_SOURCES_MAX,
            )

        playback_trace(
            "worker.direct_preview_source_opened",
            asset=asset_path,
            target_size=key[2],
        )
        return key, handle

    def _discard_direct_preview_source(
        self,
        key: tuple[str, str, Tuple[int, int]] | None,
    ) -> None:
        if key is None:
            return
        with self._direct_preview_lock:
            handle = self._direct_preview_sources.pop(key, None)
            self._direct_preview_last_frames.pop(key, None)
        if isinstance(handle, _GuardedPreviewSource):
            handle.retire()
        elif handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def _get_direct_preview_frame(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        clip_key: Optional[str],
    ) -> Optional[np.ndarray]:
        key, handle = self._get_or_create_direct_preview_source(
            asset_path,
            asset_id,
            clip_key,
        )
        if handle is None:
            return None

        source = handle.acquire() if isinstance(handle, _GuardedPreviewSource) else handle
        if source is None:
            # 句柄刚被标记关闭（时间线重建）：本帧放弃，下一帧会重建源
            return None

        try:
            requested_frame = max(0, int(source_frame))
            with self._direct_preview_lock:
                last_frame = self._direct_preview_last_frames.get(key)
            if last_frame is not None and last_frame[0] == requested_frame:
                playback_trace(
                    "worker.direct_preview_frame_cache_hit",
                    asset=asset_path,
                    frame=requested_frame,
                    target_size=key[2] if key else None,
                )
                return last_frame[1]

            decoded_frame = source.read_frame(requested_frame)
            decoded_frame = self._validate_source_frame(
                decoded_frame,
                requested_frame,
                asset_id,
                clip_key,
                "direct_preview",
            )
            if decoded_frame is None:
                return None
            frame_data = self._extract_frame_data(decoded_frame)
            if frame_data is not None:
                # Render stages treat decoded source arrays as immutable.  Do
                # not copy the 1080p buffer here: the cache is specifically
                # intended to avoid both the backwards seek and a second
                # full-frame allocation while one transition side is held.
                with self._direct_preview_lock:
                    self._direct_preview_last_frames[key] = (
                        requested_frame,
                        frame_data,
                    )
                playback_trace(
                    "worker.direct_preview_frame",
                    asset=asset_path,
                    frame=requested_frame,
                    target_size=key[2] if key else None,
                )
            return frame_data
        except Exception as e:
            self._discard_direct_preview_source(key)
            logger.debug(
                "Direct edit preview frame failed: "
                f"asset={asset_path}, frame={source_frame}: {e}"
            )
            return None
        finally:
            if isinstance(handle, _GuardedPreviewSource):
                handle.release()

    def _get_preview_color_grade(
        self,
        layer: ClipLayerInfo,
        *,
        exclude_timeline: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """Return the active clip ColorStack in a renderer-safe form.

        Raw edit playback remains a direct decode path when a clip has no
        visible grade.  Once a stack changes pixels, both CPU and GPU preview
        paths receive the same frozen parameter snapshot for this render task.
        """
        clip = getattr(layer, "clip", None)
        stack_id = getattr(clip, "color_stack_id", None)
        if not stack_id:
            return None

        project = getattr(self._asset_resolver, "_project", None)
        stacks = getattr(project, "color_stacks", {}) if project is not None else {}
        stack = stacks.get(stack_id)
        if stack is None:
            return None

        grading_params = dict(getattr(stack, "grading_params", {}) or {})
        wheel_defaults = {
            "lift": (0.0, 0.0, 0.0),
            "gamma": (0.0, 0.0, 0.0),
            "gain": (0.0, 0.0, 0.0),
        }
        scalar_defaults = {
            "exposure": 0.0,
            "temp": 0.0,
            "tint": 0.0,
            "contrast": 0.0,
            "shadow": 0.0,
            "highlights": 0.0,
            "saturation": 0.0,
            "vibrance": 0.0,
            "mid_detail": 0.0,
            "sharpness": 0.0,
            "hue_shift": 0.0,
        }

        def changed(value: Any, default: Any) -> bool:
            try:
                if isinstance(default, tuple):
                    values = tuple(float(item) for item in value)
                    return len(values) != len(default) or any(
                        abs(item - expected) > 1e-6
                        for item, expected in zip(values, default)
                    )
                return abs(float(value) - float(default)) > 1e-6
            except (TypeError, ValueError):
                return value != default

        basic_active = any(
            changed(grading_params.get(name, default), default)
            for name, default in {**wheel_defaults, **scalar_defaults}.items()
        )
        advanced_active = bool(
            getattr(stack, "curves_data", None)
            or getattr(stack, "hsl_curve_data", None)
            or getattr(stack, "mixer_params", None)
            or getattr(stack, "lut_path", None)
            or bool((getattr(stack, "soft_clip_params", {}) or {}).get("enabled", False))
            or not curves_soft_clip_is_identity(
                getattr(stack, "curves_soft_clip", None))
            or not curves_edit_is_default(getattr(stack, "curves_edit", None))
        )

        # P1-COLOR-03: 提取调色关键帧轨道（活动层或首个含轨道的层），
        # 由 _apply_preview_color_grade 按 layer.source_frame 求值。
        grade_keyframes = None
        layers_snapshot = list(getattr(stack, "layers", None) or ())
        active_index = getattr(stack, "active_layer_index", None)
        ordered_layers = []
        if isinstance(active_index, int) and 0 <= active_index < len(layers_snapshot):
            ordered_layers.append(layers_snapshot[active_index])
        ordered_layers.extend(
            layer_dict for i, layer_dict in enumerate(layers_snapshot)
            if i != active_index
        )
        for layer_dict in ordered_layers:
            if isinstance(layer_dict, dict) and layer_dict.get("grade_keyframes"):
                grade_keyframes = layer_dict["grade_keyframes"]
                break

        # M06（S11）：qualifier 纳入 advanced 检测 + 参数透传（与
        # timeline_export._get_clip_color_grade 同口径；只开 qualifier
        # 的栈此前返回 None 静默无效果）
        qualifier_params = None
        for layer_dict in ordered_layers:
            if not isinstance(layer_dict, dict):
                continue
            qp = layer_dict.get("qualifier_params") or None
            if qp and qp.get("enabled"):
                qualifier_params = dict(qp)
                advanced_active = True
                break

        # F4.1: 调色链段（组前/微调/组后/timeline）——解析单点 core/color_chain。
        # N07-B：此前只读 `asset_resolver._current_timeline`（全仓无写入方，
        # 恒为 ""）→ timeline 段在 Edit 预览/调整剪辑/磁盘缓存全部出口被
        # 跳过而导出应用——预览≠成片。以 worker 自持的 `_timeline_sequence`
        # 兜底（resolver 属性保留为优先源，防既有注入方依赖）。
        from src.features.edit.core.color_chain import (
            chain_extra_params, extras_active,
        )
        current_timeline = getattr(
            self._asset_resolver, "_current_timeline", None) \
            if self._asset_resolver is not None else None
        if current_timeline is None:
            current_timeline = getattr(self, "_timeline_sequence", None)
        timeline_id = str(
            getattr(current_timeline, "timeline_id", "") or "")
        extras = chain_extra_params(project, timeline_id, clip,
                                    exclude_timeline=exclude_timeline)
        extras_on = extras_active(extras)

        graph = getattr(stack, "color_graph", None)
        if graph is not None:
            return {"stack_id": str(stack_id), "color_graph": graph,
                    "source_frame": int(getattr(layer, "source_frame", 0)),
                    "chain_extra_params": extras,
                    "has_advanced_operations": True}

        if not basic_active and not advanced_active and not grade_keyframes \
                and not extras_on:
            return None

        # N01：栈带不可投影的复杂图时显式降级标记（Edit 实时预览只承
        # 载 primary 域——复杂图在此出口不得静默压扁为 primary-only）。
        graph_unprojectable = preview_graph_unprojectable(stack)
        if graph_unprojectable:
            advanced_active = True
            logger.warning(
                "Edit preview: stack %s 携带不可投影复杂图——实时"
                "预览只应用 primary 域（完整图渲染归 Color 页/导出；"
                "不静默当作完整调色）", stack_id)

        return {
            "stack_id": str(stack_id),
            "grading_params": grading_params,
            # Advanced operations deliberately remain visible as a marker.
            # The GPU pass applies the realtime primary grade; cache/render
            # paths own the heavier curves, LUT and compositing stages.
            "has_advanced_operations": advanced_active,
            "graph_not_projectable": graph_unprojectable,
            "grade_keyframes": grade_keyframes,
            # F4.1: 链段参数（顺序 pre→trim→post；含高级标记时由应用端
            # 降级处理，不静默丢弃）
            "chain_extra_params": extras,
            # M06（S11）：qualifier 参数随 grade 透传（求值与告警同
            # 导出侧口径）
            "qualifier_params": qualifier_params,
        }

    @staticmethod
    def _evaluate_preview_grade_at(
        grade: Optional[Dict[str, Any]],
        source_frame: int,
    ) -> Optional[Dict[str, Any]]:
        """P1-COLOR-03: GPU 预览路径按源帧求值调色关键帧。

        CPU 合成（``_apply_preview_color_grade``）与导出各自按
        ``layer.source_frame`` 求值；GPU 零拷贝路径只读
        ``grading_params``，必须在快照装车前就地求值，否则 Edit 页
        预览显示静态 base grade，与导出结果不一致。
        """
        if not grade:
            return grade
        if grade.get("color_graph") is not None:
            # Task assembly may resolve a different source index (e.g. a
            # composite or generated source). Never retain the initial index.
            return {**grade, "source_frame": int(source_frame)}
        tracks_payload = grade.get("grade_keyframes")
        base_params = grade.get("grading_params")
        if not tracks_payload or not isinstance(base_params, dict):
            return grade
        from src.features.color.core.grade_keyframes import (
            GradeKeyframeTrack,
            evaluate_grade_keyframes,
        )
        tracks = {
            name: (
                track if isinstance(track, GradeKeyframeTrack)
                else GradeKeyframeTrack.from_dict(track)
            )
            for name, track in tracks_payload.items()
            if isinstance(track, (dict, GradeKeyframeTrack))
        }
        if not tracks:
            return grade
        evaluated = dict(grade)
        evaluated["grading_params"] = evaluate_grade_keyframes(
            tracks, int(source_frame), base_params)
        return evaluated

    def _get_clip_effects_snapshot(
        self,
        layer: ClipLayerInfo,
    ) -> tuple[Any, ...]:
        """Return the clip's effect chain in a renderer-safe form.

        Mirrors :meth:`_get_preview_color_grade`: pulls data off the
        ``ClipInstance`` at task-build time so the render thread receives a
        stable snapshot and never touches the live timeline object.

        Returns the clip's ``effects`` tuple as-is when non-empty, else an
        empty tuple. ``EffectInstance`` is a frozen dataclass, which makes
        sharing the reference across threads safe for read-only rendering
        (the same "gentleman's agreement" the rest of this module uses for
        the grade dict). The render thread reads parameters via
        ``EffectInstance.get_param_at_frame(name, frame)`` and never
        mutates them.
        """
        clip = getattr(layer, "clip", None)
        if clip is None:
            return ()
        effects = getattr(clip, "effects", ()) or ()
        # Filter to known specs early so the renderer doesn't have to handle
        # unknown legacy effect_types on the hot path.
        try:
            from src.features.shared.effects import get_effect_catalog
            catalog = get_effect_catalog()
            known = tuple(
                e for e in effects if catalog.is_known(getattr(e, "effect_type", ""))
            )
        except Exception:  # noqa: BLE001 — never block playback on catalog errors
            known = effects if effects else ()
        return known

    def _apply_clip_effects_cpu(
        self,
        frame_data: np.ndarray,
        layer: ClipLayerInfo,
        frame: int,
    ) -> np.ndarray:
        """Apply a clip's video-filter chain on the CPU composite path.

        Counterpart of the GPU path's ``EffectPassRunner``: takes the same
        effect snapshot (see :meth:`_get_clip_effects_snapshot`) and applies
        each effect in ``order``. Only ``blur``/``sharpen`` have a CPU
        implementation here; other effect types (denoise/glow/...) currently
        require the GPU path and are passed through untouched with a
        one-shot warning, so a missing CPU impl never crashes playback.
        """
        effects = self._get_clip_effects_snapshot(layer)
        if not effects:
            return frame_data

        # Sort by processing order (0 = first) to match the GPU renderer.
        ordered = sorted(effects, key=lambda e: getattr(e, "order", 0))

        result = frame_data
        for effect in ordered:
            effect_type = getattr(effect, "effect_type", "")
            try:
                params = {
                    name: effect.get_param_at_frame(name, frame)
                    for name in (getattr(effect, "params", {}) or {}).keys()
                }
            except Exception:  # noqa: BLE001 — defensive: never break render
                logger.exception("Failed to read params for effect %r", effect_type)
                continue

            if effect_type in ("blur", "gaussian_blur"):
                result = self._apply_blur_cpu(result, params)
            elif effect_type == "sharpen":
                result = self._apply_sharpen_cpu(result, params)
            else:
                # CPU implementations for denoise/glow/defocus/... are not
                # available; warn once per type so the user understands why
                # the effect is invisible without flooding the log.
                self._warn_cpu_effect_unsupported(effect_type)
        return result

    def _warn_cpu_effect_unsupported(self, effect_type: str) -> None:
        """Emit a single warning per unsupported CPU effect type."""
        warned = getattr(self, "_cpu_effect_warned", None)
        if warned is None:
            warned = set()
            self._cpu_effect_warned = warned
        if effect_type in warned:
            return
        warned.add(effect_type)
        logger.warning(
            "Effect %r has no CPU implementation; it only renders on the GPU "
            "path. Enable GPU pipeline (v3) to preview it.",
            effect_type,
        )

    def _apply_blur_cpu(
        self,
        frame_data: np.ndarray,
        params: Dict[str, Any],
    ) -> np.ndarray:
        """CPU blur: box or gaussian, per-channel mask, mix with original.

        Mirrors the semantics of ``effect_pass_runner._pack_blur_params`` /
        the H/V shader: radius drives kernel size, ``mix`` blends the blurred
        result back over the source, ``channel_*`` toggles select which
        channels are blurred (alpha left untouched when off).
        """
        # Keep software preview/export on one implementation so the five Blur
        # variants do not drift between renderers.
        from src.features.edit.core.timeline_export import _cpu_blur
        return _cpu_blur(frame_data, params)

    def _apply_sharpen_cpu(
        self,
        frame_data: np.ndarray,
        params: Dict[str, Any],
    ) -> np.ndarray:
        """CPU sharpen via unsharp mask.

        Mirrors ``effect_pass_runner._pack_sharpen_params``: amount scales
        the high-frequency add, radius drives the blur kernel, threshold
        skips low-contrast pixels, and ``channels`` chooses all / luma /
        chroma. Precision is float32 internally; result clipped to uint8.
        """
        import cv2
        from src.features.shared.effects.effect_pass_runner import _pack_sharpen_params

        if frame_data is None or frame_data.ndim != 3 or frame_data.shape[2] < 3:
            return frame_data

        try:
            uniforms = _pack_sharpen_params(params)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to pack sharpen params; skipping effect")
            return frame_data

        amount = float(uniforms.get("u_amount", 1.0))
        radius = float(uniforms.get("u_radius", 1.0))
        threshold = float(uniforms.get("u_threshold", 0.0))
        # u_channels: 0 = all, 1 = luma, 2 = chroma
        channels_idx = int(uniforms.get("u_channels", 0))

        if amount <= 0.001 or radius < 0.5:
            return frame_data

        src = frame_data
        has_alpha = src.shape[2] == 4
        bgr = src[:, :, :3] if has_alpha else src

        ksize = int(min(511, max(1, round(radius) * 2 + 1)))
        bgr_f = bgr.astype(np.float32)

        if channels_idx == 0:
            # Sharpen all channels directly in BGR.
            base = bgr_f
            low = cv2.GaussianBlur(base, (ksize, ksize), 0)
            high = base - low
            if threshold > 0.0:
                # Only boost pixels whose local contrast exceeds threshold.
                mask = (np.abs(high) > threshold).astype(np.float32)
                high = high * mask
            sharp = base + high * amount
            sharp = np.clip(sharp, 0, 255).astype(np.uint8)
        else:
            # Convert to YCrCb, sharpen Y (luma) or Cr/Cb (chroma) only.
            ycc = cv2.cvtColor(bgr, cv2.COLOR_BGR2YCrCb).astype(np.float32)
            if channels_idx == 1:  # luma
                y = ycc[:, :, 0]
                low = cv2.GaussianBlur(y, (ksize, ksize), 0)
                high = y - low
                if threshold > 0.0:
                    mask = (np.abs(high) > threshold).astype(np.float32)
                    high = high * mask
                ycc[:, :, 0] = np.clip(y + high * amount, 0, 255)
            else:  # chroma — sharpen Cr and Cb
                for c in (1, 2):
                    ch = ycc[:, :, c]
                    low = cv2.GaussianBlur(ch, (ksize, ksize), 0)
                    high = ch - low
                    if threshold > 0.0:
                        m = (np.abs(high) > threshold).astype(np.float32)
                        high = high * m
                    ycc[:, :, c] = np.clip(ch + high * amount, 0, 255)
            sharp = cv2.cvtColor(
                ycc.astype(np.uint8), cv2.COLOR_YCrCb2BGR
            )

        if has_alpha:
            sharp = np.dstack([sharp, src[:, :, 3]])
        return sharp

    def _apply_preview_color_grade(self, frame_data: np.ndarray, layer: ClipLayerInfo,
                                   *, exclude_timeline: bool = False) -> np.ndarray:
        from src.features.shared.effects.video_stabilization import (
            clip_stabilization_state, apply_stabilization_cpu)
        graded = self._apply_preview_color_grade_unstabilized(
            frame_data, layer, exclude_timeline=exclude_timeline)
        state = clip_stabilization_state(getattr(self._asset_resolver, '_project', None),
                                         getattr(layer, 'clip', None))
        return apply_stabilization_cpu(graded, state, int(getattr(layer, "source_frame", 0)))

    def _clip_color_graph(self, layer: ClipLayerInfo) -> Optional[Any]:
        """clip 调色栈携带的权威图（无图栈返回 None）。"""
        clip = getattr(layer, "clip", None)
        stack_id = getattr(clip, "color_stack_id", None)
        if not stack_id:
            return None
        project = getattr(self._asset_resolver, "_project", None) \
            if self._asset_resolver is not None else None
        stacks = getattr(project, "color_stacks", {}) if project is not None else {}
        stack = stacks.get(stack_id)
        return getattr(stack, "color_graph", None) if stack is not None else None

    def _apply_preview_color_graph_cpu(
        self,
        frame_data: np.ndarray,
        layer: ClipLayerInfo,
        graph: Any,
        *,
        exclude_timeline: bool = False,
    ) -> Optional[np.ndarray]:
        """R5：Edit CPU 预览回退按权威图逐节点合成。

        与 Deliver 侧 ``run_graph_frame_cpu`` 同一入口/同一生产 oracle
        （预览与导出回退路径一致）。能力不足或资源失败上报错误，
        由现有预览任务错误通道保留上一帧，避免显示缺少效果的成片预览。
        """
        from src.features.color.core.color_graph_compiler import (
            GraphCompileError,
        )
        from src.features.color.core.color_graph_cpu import (
            UnsupportedNode, run_graph_frame_cpu,
        )
        from src.features.edit.core.color_chain import chain_extra_params
        project = (getattr(self._asset_resolver, "_project", None)
                   if self._asset_resolver is not None else None)
        current_timeline = getattr(
            self._asset_resolver, "_current_timeline", None) \
            if self._asset_resolver is not None else None
        if current_timeline is None:
            current_timeline = getattr(self, "_timeline_sequence", None)
        timeline_id = str(getattr(current_timeline, "timeline_id", "") or "")
        extras = [extra for extra in (
            chain_extra_params(project, timeline_id,
                               getattr(layer, "clip", None),
                               exclude_timeline=exclude_timeline) or ())
            if isinstance(extra, dict)]
        for extra in extras:
            if extra.get("_unsupported_advanced", False):
                logger.warning(
                    "Edit preview color chain: clip %s 链段含 curves/LUT 等"
                    "高级操作，基础链段求值不能完整执行该段",
                    layer.clip_id)
        pre = [e for e in extras if e.get("_chain_segment") == "pre"]
        after = [e for e in extras if e.get("_chain_segment") != "pre"]
        try:
            return run_graph_frame_cpu(
                graph, frame_data,
                int(getattr(layer, "source_frame", 0) or 0),
                pre_chain=pre, post_chain=after)
        except (UnsupportedNode, GraphCompileError) as exc:
            logger.warning(
                "Edit preview color grade: clip %s 图 CPU 逐节点合成不支持"
                "（%s）——未发布不完整画面",
                layer.clip_id, exc)
            raise RuntimeError(f"Color graph preview failed for clip {layer.clip_id}: {exc}") from exc
        except Exception:  # noqa: BLE001 — log and rethrow at render-task boundary
            logger.warning(
                "Edit preview color graph cpu failed for clip %s; no partial grade published",
                layer.clip_id, exc_info=True)
            raise

    def _apply_preview_color_grade_unstabilized(
        self,
        frame_data: np.ndarray,
        layer: ClipLayerInfo,
        *,
        exclude_timeline: bool = False,
    ) -> np.ndarray:
        """Apply the realtime ColorStack primary grade for a CPU fallback."""
        graph = self._clip_color_graph(layer)
        if graph is not None:
            graded = self._apply_preview_color_graph_cpu(
                frame_data, layer, graph, exclude_timeline=exclude_timeline)
            if graded is not None:
                return graded
            raise RuntimeError(f"Color graph preview is unsupported for clip {layer.clip_id}; full grade was not rendered")
        grade = self._get_preview_color_grade(
            layer, exclude_timeline=exclude_timeline)
        if grade is None:
            return frame_data

        # P1-COLOR-03: 有关键帧轨道时按当前帧求值（与导出同一函数）
        grading_params = grade["grading_params"]
        if grade.get("grade_keyframes"):
            try:
                from src.features.color.core.grade_keyframes import (
                    GradeKeyframeTrack, evaluate_grade_keyframes,
                )
                tracks = {
                    str(name): GradeKeyframeTrack.from_dict(track)
                    for name, track in grade["grade_keyframes"].items()
                }
                grading_params = evaluate_grade_keyframes(
                    tracks, int(layer.source_frame), grading_params)
            except Exception as error:
                logger.warning(
                    "Edit preview grade keyframe eval failed for clip %s: %s",
                    layer.clip_id, error,
                )

        try:
            from src.features.color.core.the_decompiler import GradingMath

            if frame_data.ndim != 3 or frame_data.shape[2] < 3:
                return frame_data
            integer_input = np.issubdtype(frame_data.dtype, np.integer)
            max_value = float(np.iinfo(frame_data.dtype).max) if integer_input else 1.0
            rgb = frame_data[..., :3][..., ::-1].astype(np.float32) / max_value
            # M05（S08）：链段按 _chain_segment 分侧——pre 段在主栈之前，
            # trim/post 段在主栈之后（pre→main→trim→post；未标记载荷按
            # 主栈之后处理，与旧行为一致）。
            extras = [
                e for e in (grade.get("chain_extra_params") or ())
                if isinstance(e, dict)]

            def _apply_chain_entries(buf, entries):
                for extra in entries:
                    if extra.get("_unsupported_advanced", False):
                        logger.warning(
                            "Edit preview color chain: clip %s 链段含 curves/"
                            "LUT 等高级操作，primary 预览跳过该段（成片渲染"
                            "路径不受影响）", layer.clip_id)
                        continue
                    if any(abs(float(extra.get(name, 0.0) or 0.0)) > 1e-6
                           for name in ("exposure", "temp", "tint", "contrast",
                                        "shadow", "highlights", "saturation",
                                        "vibrance", "mid_detail", "sharpness",
                                        "hue_shift")) or any(
                            any(abs(float(v)) > 1e-6 for v in extra.get(w, ()))
                            for w in ("lift", "gamma", "gain")):
                        buf = GradingMath.apply_grading(
                            buf.reshape(-1, 3), extra).reshape(rgb.shape)
                return buf

            graded = _apply_chain_entries(
                rgb, [e for e in extras if e.get("_chain_segment") == "pre"])
            graded = GradingMath.apply_grading(
                graded.reshape(-1, 3),
                grading_params,
            ).reshape(rgb.shape)
            # F4.1: 调色链段（pre_group → trim → post_group）顺序应用。
            # 非线性参数无代数合并，顺序执行即精确组合；高级标记段
            # （curves/LUT）在 primary 域不可表示——跳过并告警（与
            # has_advanced_operations 的降级语义一致，不静默丢段）。
            graded = _apply_chain_entries(
                graded, [e for e in extras
                         if e.get("_chain_segment") != "pre"])
            result = frame_data.copy()
            if integer_input:
                result[..., :3] = np.rint(
                    np.clip(graded[..., ::-1], 0.0, 1.0) * max_value
                ).astype(frame_data.dtype)
            else:
                result[..., :3] = graded[..., ::-1]
            return result
        except Exception as error:
            logger.warning(
                "Edit CPU color-stack preview fallback failed for clip %s: %s",
                layer.clip_id,
                error,
            )
            return frame_data

    def _is_identity_layer(self, layer: ClipLayerInfo) -> bool:
        """Return True when a layer needs no CPU timeline compositing work."""
        if self._get_preview_color_grade(layer) is not None:
            return False
        if getattr(layer, "is_in_transition", False):
            return False
        if getattr(layer, "composite_id", None):
            return False
        if abs(float(getattr(layer, "opacity", 1.0)) - 1.0) > 0.001:
            return False

        transform = tuple(getattr(layer, "transform", ()) or ())
        # 8-tuple pixel model: (tx, ty, sx, sy, rot, pivot_mode, piv_x, piv_y).
        # pivot_mode (index 5) is a string, so use the GPU renderer's identity
        # checker which handles the mixed-type comparison safely.
        from src.features.edit.core.edit_gpu_preview_renderer import (
            _transform_tuple_is_identity as _tt_is_identity,
        )
        if not _tt_is_identity(transform):
            return False

        crop = tuple(getattr(layer, "crop", ()) or ())
        identity_crop = (0.0, 0.0, 0.0, 0.0, 0.0)
        if len(crop) != len(identity_crop):
            return False
        if any(abs(float(a) - b) > 0.001 for a, b in zip(crop, identity_crop)):
            return False

        # Effects (blur/sharpen/...) require per-pixel work — never treat a
        # clip with effects as an identity layer, or the single-clip fast
        # path (_try_render_single_direct_preview_qimage) would bypass them
        # and the user would see no change when tweaking effect parameters.
        if self._get_clip_effects_snapshot(layer):
            return False
        return True

    def _occlusion_facts_for_layer(self, layer: ClipLayerInfo):
        """收集单层遮挡分析所需的静态事实（O2 共享模块契约）。

        任何失败都返回 None（fail-closed：该层不参与遮挡证据）。
        """
        try:
            from src.features.edit.core.timeline_occlusion import (
                LayerOcclusionFacts,
                effects_background_independent,
                normalize_blend_mode,
                source_alpha_known_opaque,
            )
            asset_id = getattr(layer, "asset_id", "") or ""
            metadata = self._asset_resolver.get_asset_metadata(asset_id) or {}
            clip = getattr(layer, "clip", None)
            effects = self._get_clip_effects_snapshot(layer)
            return LayerOcclusionFacts(
                asset_kind=str(self._asset_resolver.get_asset_type(asset_id) or ""),
                source_alpha_known_opaque=source_alpha_known_opaque(
                    metadata.get("pixel_format")
                ),
                blend_mode=normalize_blend_mode(
                    getattr(clip, "blend_mode", "normal")
                ),
                has_effects=bool(effects),
                # O7：能力声明仅作为事实记录，第一阶段不改变裁剪行为。
                background_independent=effects_background_independent(effects),
            )
        except Exception:  # noqa: BLE001 — 事实收集失败按未知层处理
            return None

    def _prune_occluded_layers_for_preview(
        self,
        layers: Tuple[ClipLayerInfo, ...],
        transitions: Tuple[TransitionBlendInfo, ...],
        frame: int = -1,
        record: bool = True,
    ) -> Tuple[ClipLayerInfo, ...]:
        """
        Drop lower video layers hidden by a full-frame opaque upper layer.

        This is the common edit-preview case after dragging a clip to V2/V3:
        a normal video clip on the upper track completely covers lower tracks.
        Decoding and compositing the hidden layers doubles playback cost with
        no visual benefit.

        O2：判定逻辑统一收敛到共享纯分析模块 ``timeline_occlusion``，
        与导出/GPU 路径共用同一份裁剪语义（源 Alpha、混合模式、效果链
        均按 fail-closed 处理）。

        ``record=False`` 供实时调度器配置等派生调用使用：telemetry 的
        权威记录在 worker 渲染侧（_execute_render），派生调用不双计。
        """
        from src.features.edit.core.timeline_occlusion import (
            analyze_visible_layers,
        )

        if len(layers) <= 1:
            return tuple(layers)
        decision = analyze_visible_layers(
            tuple(layers),
            tuple(transitions),
            int(frame),
            self._decode_target_size,
            mode="preview",
            facts_of=self._occlusion_facts_for_layer,
            coordinate_size=self._get_sequence_resolution(),
            record=record,
        )
        if decision.culled:
            return decision.render_layers
        return tuple(layers)

    def _try_render_single_direct_preview_qimage(
        self,
        layers: Tuple[ClipLayerInfo, ...],
        transitions: Tuple[TransitionBlendInfo, ...],
        frame: int,
    ) -> Optional[QImage]:
        """Fast path for the common edit case: one plain source clip, no effects."""
        if transitions or len(layers) != 1:
            return None

        layer = layers[0]
        if not self._is_identity_layer(layer):
            return None
        asset_type = self._asset_resolver.get_asset_type(layer.asset_id)
        if asset_type != "video":
            return None
        asset_path = self._asset_resolver.get_asset_path(layer.asset_id)
        if not asset_path:
            return None

        frame_data = self._get_direct_preview_frame(
            asset_path,
            int(getattr(layer, "source_frame", frame)),
            layer.asset_id,
            layer.decoder_stream_key,
        )
        if frame_data is None:
            return None

        return self._numpy_to_qimage(frame_data)

    def _get_video_frame(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        clip_key: Optional[str] = None,
        raw_params: Any = None,
    ) -> Optional[np.ndarray]:
        """
        获取视频帧。

        Plain source media first uses a lightweight direct FFmpeg preview source
        (packed BGR at preview size). This keeps the edit page close to a media
        player for raw素材 playback. If that path is unavailable, fall back to
        VideoFrameProviderService.

        Args:
            asset_path: 视频文件路径
            source_frame: 源帧号
            asset_id: 素材 ID
            clip_key: Timeline clip identity.  When present, a separate
                frame-provider session is kept for each timeline clip even if
                multiple clips reference the same file.
            raw_params: clip RawParams（CFA debayer 用户参数，CPU 回退与
                GPU 预览同优先级；非 CFA 格式忽略）

        Returns:
            帧数据（numpy.ndarray）或 None
        """
        direct_frame = self._get_direct_preview_frame(
            asset_path,
            source_frame,
            asset_id,
            clip_key,
        )
        if direct_frame is not None:
            return direct_frame

        clip_id = self._get_or_open_clip_id(asset_path, asset_id, clip_key)
        if clip_id is None:
            return None

        # 播放模式判定：realtime 禁止 master fallback。
        is_realtime = self._is_realtime_playback_active()

        try:
            decoded_frame = None

            if is_realtime:
                # 实时播放热路径：只请求 proxy，禁止 master fallback。
                # proxy miss 时返回 None，上层 hold/placeholder/rebuffer。
                decoded_frame = self._frame_provider.request_frame(
                    clip_id,
                    source_frame,
                    quality="proxy",
                    allow_master_fallback=False,
                )
                decoded_frame = self._validate_source_frame(
                    decoded_frame, source_frame, asset_id, clip_key, "proxy"
                )
            else:
                # 非实时（paused/step/export/静态预览）：保留 proxy→master fallback
                # 以保证帧准确。
                for quality in ("proxy", "master"):
                    decoded_frame = self._frame_provider.request_frame(
                        clip_id,
                        source_frame,
                        quality=quality
                    )
                    decoded_frame = self._validate_source_frame(
                        decoded_frame, source_frame, asset_id, clip_key, quality
                    )
                    if decoded_frame is not None:
                        if quality == "master":
                            logger.debug(
                                f"Proxy frame unavailable, used master fallback: "
                                f"asset={asset_id}, frame={source_frame}"
                            )
                        break

            if decoded_frame is None:
                logger.warning(f"Failed to get frame {source_frame} from {asset_path}")
                return None

            # 从 DecodedFrame 提取数据
            return self._extract_frame_data(decoded_frame,
                                            raw_params=raw_params)

        except Exception as e:
            logger.error(f"Failed to request frame {source_frame}: {e}")
            return None

    def _is_realtime_playback_active(self) -> bool:
        """
        Return whether realtime playback master-fallback prohibition should apply.

        规格书 Stage C2：实时播放热路径禁止 master fallback。
        Worker 通过 _execute_render 入口的 skip_cpu_fallback_on_gpu_miss 标记
        获知当前 render 是否来自实时播放（该标记由 TimelinePreviewRenderer 根据
        playback controller 是否 playing 计算）。非实时 render（暂停/逐帧/静态
        预览/导出）保留 master fallback 以保证帧准确。
        """
        return bool(getattr(self, "_current_render_realtime", False))

    def _get_decoded_frame_for_cache_passthrough(
        self,
        asset_path: str,
        source_frame: int,
        asset_id: str,
        clip_key: Optional[str] = None,
    ) -> Optional[Any]:
        """T-03 后半段：渲染缓存产出端的免转换解码。

        与 :meth:`_get_video_frame` 的非实时分支同语义（proxy→master 兜底
        保证帧准确），但**不做** packed BGR 提取——平面 DecodedFrame 原样
        返回，让 RenderCacheWorker 的 planar 直通路径把它一路送进 Topos
        编码器，省去 planar→BGR（10bit→8bit 量化）的整帧转换。刻意不走
        direct preview 源（今天它只产 packed BGR，对平面直通没有意义）。
        """
        clip_id = self._get_or_open_clip_id(asset_path, asset_id, clip_key)
        if clip_id is None:
            return None
        try:
            decoded_frame = self._frame_provider.request_frame(
                clip_id, source_frame, quality="proxy"
            )
            decoded_frame = self._validate_source_frame(
                decoded_frame, source_frame, asset_id, clip_key, "proxy"
            )
            if decoded_frame is None:
                decoded_frame = self._frame_provider.request_frame(
                    clip_id, source_frame, quality="master"
                )
                decoded_frame = self._validate_source_frame(
                    decoded_frame, source_frame, asset_id, clip_key, "master"
                )
            return decoded_frame
        except Exception as e:
            logger.error(
                f"Cache passthrough decode failed frame {source_frame}: {e}"
            )
            return None

    def _validate_source_frame(
        self,
        decoded_frame: Any,
        source_frame: int,
        asset_id: str,
        clip_key: Optional[str],
        quality: str,
    ) -> Any:
        """
        校验返回的 source frame 与请求一致。

        错误帧（mismatch > 1）必须拒绝，避免展示错帧（规格书 §10.3 / §25）。
        adjacent frame（mismatch <= 1）仅在非实时预览时接受。
        """
        if decoded_frame is None:
            return None
        actual_frame = getattr(decoded_frame, "frame_index", None)
        if isinstance(actual_frame, int) and actual_frame != source_frame:
            mismatch = abs(actual_frame - source_frame)
            if mismatch > 1:
                logger.warning(
                    "Decoded wrong source frame; rejecting result: "
                    f"asset={asset_id}, clip={clip_key}, requested={source_frame}, "
                    f"actual={actual_frame}, quality={quality}"
                )
                return None
            logger.debug(
                "Accepting adjacent decoded frame for preview: "
                f"asset={asset_id}, clip={clip_key}, requested={source_frame}, "
                f"actual={actual_frame}, quality={quality}"
            )
        return decoded_frame

    def _get_or_open_clip_id(
        self,
        asset_path: str,
        asset_id: str,
        clip_key: Optional[str] = None,
        *,
        prebuffer: bool = False,
    ) -> Optional[str]:
        """Return the shared frame-provider clip id for a timeline clip."""
        session_key = f"{clip_key}:{asset_id}" if clip_key else asset_id
        session_map = self._prebuffer_clips if prebuffer else self._open_clips
        with self._open_clips_lock:
            clip_id = session_map.get(session_key)

        if clip_id is not None:
            return clip_id

        try:
            clip_id = self._frame_provider.open_clip(
                asset_path,
                share_by_path=False,
                defer_memory_proxy=True,
            )
            with self._open_clips_lock:
                existing = session_map.get(session_key)
                if existing is not None:
                    try:
                        self._frame_provider.close_clip(clip_id)
                    except Exception:
                        pass
                    return existing
                session_map[session_key] = clip_id
            logger.debug(f"Opened clip: {asset_path} -> {clip_id}")
            return clip_id
        except Exception as e:
            logger.error(f"Failed to open clip {asset_path}: {e}")
            return None

    def prefetch_video_frame_range(
        self,
        asset_path: str,
        asset_id: str,
        clip_key: Optional[str],
        source_frames: List[int],
        quality: str = "proxy",
    ) -> bool:
        """Prefetch source frames without touching the display decode session."""
        if not source_frames:
            return False

        # Keep prebuffer decoding on its own ClipSession.  Reusing the display
        # session lets background prefetch move the playback decoder cursor,
        # which shows up as multi-track jitter and wrong-frame flashes.
        clip_id = self._get_or_open_clip_id(
            asset_path,
            asset_id,
            clip_key,
            prebuffer=True,
        )
        if clip_id is None:
            return False

        try:
            frames = sorted({max(0, int(frame)) for frame in source_frames})
            start = end = frames[0]
            for frame in frames[1:]:
                if frame == end + 1:
                    end = frame
                    continue
                self._frame_provider.prefetch(
                    clip_id,
                    start,
                    end,
                    quality=quality
                )
                start = end = frame
            self._frame_provider.prefetch(clip_id, start, end, quality=quality)
            return True
        except Exception as e:
            logger.debug(
                "Timeline prebuffer failed: "
                f"asset={asset_id}, clip={clip_key}, frames={source_frames}: {e}"
            )
            return False

    def _get_image_frame(self, asset_path: str) -> Optional[np.ndarray]:
        """
        获取图片帧。

        对于静态图片，直接加载整个文件。

        Args:
            asset_path: 图片文件路径

        Returns:
            帧数据（numpy.ndarray）或 None
        """
        try:
            # 使用 FileIO 加载图片
            from src.shared.io.file_io import FileIO

            # 获取预览分辨率（proxy）
            img_data = FileIO.get_image_preview(asset_path, max_size=self._decode_target_size)

            if img_data is None:
                return None

            # 确保是 BGR uint8 格式
            if img_data.dtype != np.uint8:
                img_data = (img_data * 255).astype(np.uint8)

            return img_data

        except Exception as e:
            logger.error(f"Failed to load image {asset_path}: {e}")
            return None

    def _extract_frame_data(self, decoded_frame: Any,
                            raw_params: Any = None) -> Optional[np.ndarray]:
        """
        从 DecodedFrame 提取 numpy 数组。

        Args:
            decoded_frame: DecodedFrame 实例
            raw_params: clip RawParams（CFA debayer 用户参数；None = 走
                帧 TRWM as-shot 基线）

        Returns:
            numpy.ndarray (BGR uint8/uint16)
        """
        try:
            # 检查是否有 data 属性
            if hasattr(decoded_frame, 'data') and decoded_frame.data is not None:
                data = decoded_frame.data

                # 处理不同格式
                pixel_format = getattr(decoded_frame, 'pixel_format', 'bgra')

                if pixel_format in ('bgr24', 'bgra', 'bgr48le', 'bgra64le'):
                    # BGR 格式，直接返回（去掉 alpha 如果存在）
                    return data[..., :3] if data.shape[-1] == 4 else data

                elif pixel_format in ('rgb24', 'rgba', 'rgb48le', 'rgb64le'):
                    # RGB 格式，转换为 BGR
                    return np.flip(data[..., :3], axis=-1)

                elif pixel_format in ('gbrp', 'gbrp10le', 'gbrp12le'):
                    # 平面格式，需要转换
                    # 这里简化处理，实际需要完整的 YUV/RGB 转换
                    return self._convert_planar_to_bgr(
                        decoded_frame, raw_params=raw_params)

                else:
                    # 默认处理
                    return data[..., :3] if data.ndim == 3 and data.shape[-1] >= 3 else data

            # 平面格式处理
            elif hasattr(decoded_frame, 'planes') and decoded_frame.planes:
                return self._convert_planar_to_bgr(
                    decoded_frame, raw_params=raw_params)

            return None

        except Exception as e:
            logger.error(f"Failed to extract frame data: {e}")
            return None

    def _plane_to_uint8(self, plane: Any, bit_depth: int) -> np.ndarray:
        """
        Normalize a decoded planar channel to uint8.

        Args:
            plane: Source plane array-like object
            bit_depth: Source bit depth

        Returns:
            uint8 numpy array
        """
        arr = np.asarray(plane)
        if arr.dtype == np.uint8:
            return arr

        if np.issubdtype(arr.dtype, np.integer):
            if bit_depth > 8:
                shift = max(0, bit_depth - 8)
                return np.clip(arr >> shift, 0, 255).astype(np.uint8)
            return np.clip(arr, 0, 255).astype(np.uint8)

        # 浮点平面（'gbrph16le' 等，M1-F6）：线性值域 → sRGB 显示编码后
        # 量化。直接 *255 会把线性当显示编码（整体偏暗），>1 高光硬剪。
        # F5.1：OETF 收口到共享单点（真值=provider）。
        from src.shared.color.display_encode import linear_bgr_to_srgb_display_u8

        return linear_bgr_to_srgb_display_u8(arr)

    def _convert_planar_to_bgr(self, decoded_frame: Any,
                               raw_params: Any = None) -> Optional[np.ndarray]:
        """
        转换平面格式为 BGR。

        支持 GBRP (G, B, R 平面) 和 YUV420P 格式。

        Args:
            decoded_frame: DecodedFrame 实例（平面格式）
            raw_params: clip RawParams（CFA debayer 用户参数；None = 走
                帧 TRWM as-shot 基线——2026-09-21 复查：CPU 回退与 GPU
                预览同优先级，不再只吃 TRWM）

        Returns:
            numpy.ndarray (BGR uint8)
        """
        try:
            import cv2

            # 获取 planes
            planes = getattr(decoded_frame, 'planes', None)
            if planes is None:
                logger.warning(f"Invalid planes: {planes}")
                return None

            # 获取 pixel_format
            pixel_format = getattr(decoded_frame, 'pixel_format', 'gbrp')
            bit_depth = int(getattr(decoded_frame, 'bit_depth', 8) or 8)

            # W4：TRAW CFA 相位平面——CPU debayer 回退（GPU 链不可用时
            # 的显示路径；LOG0 逆曲线/16-bit linear/双线性与 GPU shader
            # 同式）。CFA 帧绝不走下方 YUV 分支乱渲。
            # RC3/RC6：RAW 参数经 resolve_debayer_params 三链同源解析
            # （用户 clip RawParams > TRWM as-shot > 单位默认）。
            if pixel_format.startswith('topos_cfa'):
                from src.shared.video.camera_gamut_matrices import (
                    resolve_debayer_params)
                from src.shared.video.cfa_cpu_debayer import debayer_cfa_planes
                infos = getattr(decoded_frame, 'plane_infos', None) or ()
                cfa_bd = (int(getattr(infos[0], 'bit_depth', 0) or 0)
                          if len(infos) else 0) \
                    or (16 if 'cfa16' in pixel_format else 12)
                fw = decoded_frame.width
                fh = decoded_frame.height
                trwm = (getattr(decoded_frame, 'extra', None) or {}).get(
                    'topos_trwm') or {}
                rp = resolve_debayer_params(raw_params, trwm)
                rgb = debayer_cfa_planes(
                    planes, fw, fh, bit_depth=cfa_bd,
                    wb_gains=rp['wb_gains'],
                    exposure_stops=rp['exposure_stops'],
                    black_level=rp['black_level'],
                    gamma_mode=rp['gamma_mode'],
                    gamut_matrix=rp['matrix'])
                bgr = np.ascontiguousarray(rgb[..., ::-1])
                return (np.clip(bgr, 0.0, 1.0) * 255.0).astype(np.uint8)

            # NV12/P010: Y plane + interleaved UV plane.
            if pixel_format.startswith('nv12') or pixel_format.startswith('p010'):
                if len(planes) < 2:
                    logger.warning(f"Invalid {pixel_format} planes: {planes}")
                    return None

                y_plane = self._plane_to_uint8(planes[0], bit_depth)
                uv_plane = self._plane_to_uint8(planes[1], bit_depth)
                h, w = y_plane.shape

                if uv_plane.ndim == 3 and uv_plane.shape[2] >= 2:
                    u_plane = uv_plane[..., 0]
                    v_plane = uv_plane[..., 1]
                else:
                    u_plane = uv_plane[:, 0::2]
                    v_plane = uv_plane[:, 1::2]

                u_full = cv2.resize(u_plane, (w, h), interpolation=cv2.INTER_LINEAR)
                v_full = cv2.resize(v_plane, (w, h), interpolation=cv2.INTER_LINEAR)
                yuv = np.stack([y_plane, u_full, v_full], axis=2)
                return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

            if len(planes) < 3:
                logger.warning(f"Invalid {pixel_format} planes: {planes}")
                return None

            # GBRP 格式 (G, B, R 平面) - 常见于某些解码器
            if pixel_format.startswith('gbrp'):
                g_plane = self._plane_to_uint8(planes[0], bit_depth)
                b_plane = self._plane_to_uint8(planes[1], bit_depth)
                r_plane = self._plane_to_uint8(planes[2], bit_depth)

                # Stack planes to interleaved BGR (OpenCV expects BGR)
                # planes 是 (G, B, R)，我们需要 (B, G, R)
                return np.stack([b_plane, g_plane, r_plane], axis=2)

            # YUV420P 格式 (Y, U, V 平面) - 最常见
            elif pixel_format.startswith('yuv420p') or pixel_format == 'yuvj420p':
                y_plane = self._plane_to_uint8(planes[0], bit_depth)
                u_plane = self._plane_to_uint8(planes[1], bit_depth)
                v_plane = self._plane_to_uint8(planes[2], bit_depth)

                # YUV420P: U 和 V 是 Y 的一半尺寸
                h, w = y_plane.shape

                # Upscale U and V to full size
                u_full = cv2.resize(u_plane, (w, h), interpolation=cv2.INTER_LINEAR)
                v_full = cv2.resize(v_plane, (w, h), interpolation=cv2.INTER_LINEAR)

                # Stack to YUV
                yuv = np.stack([y_plane, u_full, v_full], axis=2)

                # Convert YUV to BGR
                # 注意: yuvj420p 是 full range (0-255), yuv420p 是 limited range (16-235)
                if pixel_format == 'yuvj420p':
                    # Full range JPEG YUV
                    bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)
                else:
                    # AUD-18903：limited range（广播 H.264/HEVC 常见的
                    # yuv420p）不能直接用 full-range JPEG 矩阵转换——黑位 16/
                    # 白位 235 会被当作 0/255，对比度压扁、黑不黑白不白，
                    # 与 GPU 解码路径肉眼可见不一致。先做 studio→full 摆幅
                    # 拉伸（Y: 16-235，UV: 16-240 以 128 为中心）再转换。
                    expanded = yuv.astype(np.float32)
                    expanded[..., 0] = np.clip(
                        (expanded[..., 0] - 16.0) * (255.0 / 219.0), 0.0, 255.0)
                    expanded[..., 1:] = np.clip(
                        (expanded[..., 1:] - 128.0) * (255.0 / 224.0) + 128.0,
                        0.0, 255.0)
                    bgr = cv2.cvtColor(
                        expanded.astype(np.uint8), cv2.COLOR_YUV2BGR)

                return bgr

            # YUV422P 格式
            elif pixel_format.startswith('yuv422p'):
                y_plane = self._plane_to_uint8(planes[0], bit_depth)
                u_plane = self._plane_to_uint8(planes[1], bit_depth)
                v_plane = self._plane_to_uint8(planes[2], bit_depth)

                h, w = y_plane.shape
                # YUV422P: U 和 V 是 Y 的一半宽度
                u_full = cv2.resize(u_plane, (w, h), interpolation=cv2.INTER_LINEAR)
                v_full = cv2.resize(v_plane, (w, h), interpolation=cv2.INTER_LINEAR)

                yuv = np.stack([y_plane, u_full, v_full], axis=2)
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

                return bgr

            # YUV444P 格式
            elif pixel_format.startswith('yuv444p'):
                y_plane = self._plane_to_uint8(planes[0], bit_depth)
                u_plane = self._plane_to_uint8(planes[1], bit_depth)
                v_plane = self._plane_to_uint8(planes[2], bit_depth)

                yuv = np.stack([y_plane, u_plane, v_plane], axis=2)
                return cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR)

            else:
                logger.warning(f"Unsupported planar format: {pixel_format}")
                return None

        except ImportError:
            logger.error("cv2 not available for planar conversion")
            return None
        except Exception as e:
            logger.error(f"Planar conversion error: {e}")
            return None

    def _render_frame(
        self,
        frame: int,
        frame_data: np.ndarray,
        generation_id: int,
    ) -> None:
        """
        CPU 回退路径：将 numpy 帧数据转换为 QImage 并发射。

        此方法仅在 GPU 路径不可用或失败时调用（composite clip、comp_clip 资产等）。
        GPU 路径通过 _try_gpu_render() → EditGpuPreviewRenderer 单独处理，
        最终通过 _on_gpu_frame_ready() 发射 GPU 纹理到 ProgramViewer。

        Args:
            frame: 帧号
            frame_data: 帧数据（numpy.ndarray BGR uint8）
            generation_id: Renderer generation for this task
        """
        try:
            qimage = self._numpy_to_qimage(frame_data)
            self._render_frame_qimage(frame, qimage, generation_id)
        except Exception as e:
            logger.error(f"QImage render error: {e}")
            playback_trace("worker.cpu_qimage_error", frame=frame, generation=generation_id, error=e)
            if generation_id == self._generation_id and not self._current_task_background:
                self.render_error.emit(frame, str(e))

    def _render_frame_qimage(self, frame: int, qimage: QImage, generation_id: int) -> None:
        """Emit an already-built QImage if the render generation is still current."""
        if generation_id == self._generation_id:
            if self._current_task_background:
                # idle-prebuffer：全分辨率 CPU 结果只回填帧缓存，不上屏。
                playback_trace(
                    "worker.background_qimage_emit",
                    frame=frame,
                    generation=generation_id,
                    width=qimage.width(),
                    height=qimage.height(),
                )
                self.background_frame_ready.emit(frame, generation_id, qimage)
                return
            playback_trace(
                "worker.cpu_qimage_emit",
                frame=frame,
                generation=generation_id,
                width=qimage.width(),
                height=qimage.height(),
            )
            self.frame_rendered.emit(frame, qimage)

    def _numpy_to_qimage(self, frame_data: np.ndarray) -> QImage:
        """
        将 numpy 数组转换为 QImage。

        使用 Format_BGR888 直接传递 BGR 数据，跳过 BGR→RGB 翻转，
        显著减少 CPU 开销和内存拷贝。

        Args:
            frame_data: numpy.ndarray (BGR uint8)

        Returns:
            QImage
        """
        h, w = frame_data.shape[:2]

        # 确保连续内存
        bgr_data = np.ascontiguousarray(frame_data)

        # 使用 BGR888 格式直接创建 QImage，无需 BGR→RGB 翻转
        qimage = QImage(
            bgr_data.data,
            w,
            h,
            w * 3,
            QImage.Format.Format_BGR888
        )

        # 保持 numpy 数据引用，防止 GC 回收。
        # ProgramViewer.set_qimage() 会做保护性 copy，无需在此重复。
        qimage._bgr_data_ref = bgr_data
        return qimage

    def cleanup_clips(self) -> None:
        """清理所有打开的 clip"""
        with self._open_clips_lock:
            open_clips = list(self._open_clips.items())
            self._open_clips.clear()
            prebuffer_clips = list(self._prebuffer_clips.items())
            self._prebuffer_clips.clear()

        for session_key, clip_id in open_clips + prebuffer_clips:
            try:
                self._frame_provider.close_clip(clip_id)
            except Exception as e:
                logger.warning(f"Failed to close clip {clip_id}: {e}")

        # Cleanup CompositeFrameRenderer
        if self._comp_frame_renderer is not None:
            try:
                self._comp_frame_renderer.cleanup()
            except Exception as e:
                logger.warning(f"Failed to cleanup comp frame renderer: {e}")
            self._comp_frame_renderer = None

        teaching_provider = getattr(self, "_teaching_scene_provider", None)
        if teaching_provider is not None:
            try:
                teaching_provider.close()
            except Exception as e:
                logger.warning(f"Failed to cleanup Teaching Animation provider: {e}")
            self._teaching_scene_provider = None

        # Clear comp clip cache
        self.invalidate_composite_cache()


# ================================================================
# TimelinePreviewRenderer - 预览渲染控制器
# ================================================================

def preview_graph_unprojectable(stack: Any) -> bool:
    """N01：栈是否携带不可投影为 legacy layers 的复杂图。

    Edit 实时预览只承载 primary 域——复杂图在此出口必须显式降级标记
    （``graph_not_projectable`` + advanced），不得静默 primary-only。
    """
    color_graph = getattr(stack, "color_graph", None)
    if color_graph is None:
        return False
    from src.features.color.core.color_graph_migration import (
        can_project_to_legacy_layers,
    )
    return not can_project_to_legacy_layers(color_graph)


class TimelinePreviewRenderer(QObject):
    """
    时间线预览渲染控制器。

    管理 Edit 页面的预览渲染管线。

    信号:
        frame_ready: 帧渲染完成 (frame, result)
        render_error: 渲染错误 (frame, error_message)
        cache_stats_changed: 缓存统计变化

    使用方式:
    ```python
    renderer = TimelinePreviewRenderer(
        project=edit_project,
        sequence=timeline_sequence,
        frame_provider=video_frame_provider,
        video_renderer=video_async_renderer,
        main_context=gl_context,
    )

    # 连接信号
    renderer.frame_ready.connect(on_frame_ready)

    # 请求渲染
    renderer.request_render(frame=100)

    # 移动播放头
    renderer.set_playhead(frame=50)
    ```
    """

    frame_ready = pyqtSignal(int, object)  # frame, result (QImage or texture_id)
    render_error = pyqtSignal(int, str)
    offline_media = pyqtSignal(int, object)
    cache_stats_changed = pyqtSignal(dict)
    # Final-4: 通知下游 Viewer 当前 realtime 状态（让 set_frame 在 REALTIME
    # 下拒绝裸 tuple，必须走 GpuPresentFrame fence/slot 协议）。
    playback_mode_changed = pyqtSignal(bool)

    def __init__(
        self,
        project: EditProject,
        sequence: TimelineSequence,
        frame_provider: Any,  # VideoFrameProviderService
        video_renderer: Any = None,  # VideoAsyncRenderer
        main_context: Any = None,  # QOpenGLContext
        viewport_size: Tuple[int, int] = (1920, 1080),
        cache_size: int = 256,
        max_preview_edge: int = 3840,
        scrub_preview_edge: int = 3840,
    ) -> None:
        """
        初始化预览渲染器。

        Args:
            project: EditProject 实例
            sequence: TimelineSequence 实例
            frame_provider: VideoFrameProviderService 实例
            video_renderer: VideoAsyncRenderer 实例（可选）
            main_context: 主 OpenGL 上下文（可选，用于 GPU 渲染）
            viewport_size: 视口尺寸
            cache_size: 帧缓存大小
            max_preview_edge: 编辑播放解码最长边上限（默认 4K）
            scrub_preview_edge: 拖拽播放头解码最长边上限（默认 4K，避免
                拖拽时退化到 720p 模糊帧）
        """
        super().__init__()

        self._project = project
        self._sequence = sequence
        self._frame_provider = frame_provider
        self._video_renderer = video_renderer
        self._main_context = main_context
        self._sequence_render_signature = self._build_sequence_render_signature(sequence)
        self._max_preview_edge = max(1, int(max_preview_edge))
        self._scrub_preview_edge = max(64, int(scrub_preview_edge))
        self._raw_viewport_size = viewport_size
        self._viewport_size = self._fit_viewport_to_sequence_aspect(viewport_size)
        # Edit-page preview defaults to an 8-bit, 4K-capped playback path.
        # 4K timelines stay native; only 6K/8K timelines are reduced to a
        # still-detailed 4K preview. The viewer itself may render smaller than
        # this texture, but it should not force a low-resolution decode.
        self._preview_quality: float = 0.0
        # 预览跳过输出去块（用户偏好 video_preview_no_deblock）：只作用于预览
        # 解码 request，交付/导出保持滤波。
        self._no_deblock: bool = False
        # Dynamic quality policy: realtime playback and scrub stay on the
        # bounded AUTO_2K target; after a short pause a separate FULL request
        # may replace the current frame. The stage is generation-gated so a
        # late FULL result cannot overwrite a newly resumed AUTO_2K frame.
        self._decode_quality_stage: str = "auto2k"
        self._quality_transition_token: int = 0
        self._full_quality_refresh_token: int = 0
        self._full_quality_delay_ms: int = 200
        self._full_quality_refresh_pending: bool = False
        self._full_quality_refresh_timer = QTimer(self)
        self._full_quality_refresh_timer.setSingleShot(True)
        self._full_quality_refresh_timer.timeout.connect(
            self._on_full_quality_refresh_timeout
        )
        # P1-10: 播放期自适应质量系数（engine quality_pressure_changed 驱动）。
        # 仅在 auto/smooth 档且 engine 播放态生效；用户显式分辨率选择或
        # FULL 暂停刷新不受影响（"用户偏好允许下"的降质与自动回退）。
        self._adaptive_playback_scale: float = 1.0

        # 帧缓存
        self._cache = PreviewFrameCache(max_size=cache_size)
        # 快照帧标记：哪些缓存条目来自 GPU 合成快照（1/4 分辨率临时帧）。
        # 命中快照帧时先立即显示、再触发全分辨率精确重渲染（refine）。
        # 有界集合，防长会话增长；generation 变更时整体失效。
        self._snapshot_frame_keys: set[int] = set()
        # ---- P3.1 空闲主动预缓冲（idle prebuffer）----
        # 暂停/静默时后台预解码播放头 ±N 帧进帧缓存：暂停态的邻域步进、
        # 小范围来回拖拽直接命中缓存（DaVinci 的 pause-neighborhood 行为）。
        # 失效机制基于"安静时间"：任何渲染请求都会刷新 _last_render_request_ts，
        # 轮询在静默满 delay_ms 后才开跑；渲染完成链里的同帧重入请求只会
        # 推迟预缓冲，不会孤立已排定的回调（epoch 方案的缺陷）。
        self._idle_prebuffer_armed: bool = False
        self._idle_prebuffer_polling: bool = False
        self._idle_prebuffer_dispatching: bool = False
        self._idle_prebuffer_anchor: int = 0
        self._idle_prebuffer_anchor_gen: int = -1
        self._idle_prebuffer_pending: set[int] = set()
        self._idle_prebuffer_targets: list[int] = []
        self._idle_prebuffer_retry: int = 0
        self._idle_prebuffer_enabled_flag: bool = (
            os.getenv("TOPOS_IDLE_PREBUFFER", "1").lower()
            not in ("0", "false", "off")
        )
        self._idle_prebuffer_radius: int = max(
            0, int(os.getenv("TOPOS_IDLE_PREBUFFER_FRAMES", "12") or 12)
        )
        # 静默判定：最后一次渲染请求后等多少 ms 才开始预缓冲。
        self._idle_prebuffer_delay_ms: int = 320
        # 两个预缓冲任务之间的间隔：让 worker 队列绝大多数时间保持空闲，
        # 交互请求一到即抢占（latest-only 丢弃 + enqueue_background_if_idle）。
        self._idle_prebuffer_gap_ms: int = 30
        self._SNAPSHOT_KEYS_MAX: int = 1024

        # GPU 渲染器（在 GL 上下文初始化后通过 set_gpu_renderer 设置）
        self._gpu_renderer = None

        # 素材路径解析器
        self._asset_resolver = AssetResolver(project)

        # 渲染计划计算器（使用现有的 TimelineRenderPlan）
        self._render_plan: Optional[TimelineRenderPlan] = None
        self._render_plan_mutex = QMutex()

        # 后台渲染线程
        self._worker: Optional[PreviewRenderWorker] = None

        # 当前播放头位置
        self._playhead_frame: int = 0
        self._playhead_mutex = QMutex()

        # Generation ID（用于判断计划是否过期）
        self._generation_id: int = 0
        # Snapshot of _generation_id that was last sent to the worker.
        # Used by _on_frame_rendered / _on_render_error to detect whether
        # set_sequence() / set_project() bumped the generation while a render
        # task was still in-flight or queued on the event loop.
        self._dispatched_generation_id: int = 0
        # Worker reports this before the corresponding frame/error signal.
        # Keep it on the controller so the UI can apply the offline overlay
        # after presenting a valid fallback frame as well.
        self._offline_media_by_frame: dict[int, tuple[int, tuple[dict[str, str], ...]]] = {}

        # Batch 3 (缺陷计划 RP-P0-05 / §3.2): RealtimePlaybackEngine 作为
        # Edit 播放的唯一热路径入口。所有播放 tick 通过 engine.request_present
        # 统一决策（mode、source policy、generation、miss handling）。
        # engine 内部委托回本类的 _do_render（实际执行），形成单一入口 +
        # policy 守卫，避免旧旁路绕过。
        from src.features.edit.core.realtime_playback import (
            RealtimePlaybackEngine,
        )
        # Final-1 (FINAL-P0-03): SourceResolver 成为 engine 的 source 决策唯一入口。
        # engine 在每帧 request_present() 内部调用 resolve_layers()，
        # source_kind 写入 telemetry，并驱动后台 proxy 生成。
        from src.features.edit.core.realtime_playback.source_resolver import (
            DiskProxyServiceAdapter,
            SourceResolver,
        )
        # 把全局 DiskProxyService 适配成 SourceResolver 期望的 proxy_manager 接口，
        # 这样 SourceResolver 能查询 proxy ready 状态并在缺失时触发后台生成，
        # 不需要修改既有 AssetProxyManager 代码。
        self._source_resolver = SourceResolver(
            proxy_manager=DiskProxyServiceAdapter(),
        )
        self._realtime_engine = RealtimePlaybackEngine()
        self._realtime_engine.configure(
            renderer=self,
            delegate_render=self._engine_delegate_render,
            delegate_scrub_render=self._engine_delegate_scrub_render,
            source_resolver=self._source_resolver,
            layer_query_provider=self._build_layer_source_queries,
        )
        self._engine_playback_active: bool = False
        # 最近一次预览渲染请求（播放 tick 或拖擦）的单调时间戳，供
        # is_render_idle 判断编辑会话是否空闲（用于 MG 空闲缓存调度）。
        self._last_render_request_ts: float = 0.0
        # 防递归标志：_submit_render_internal 调 request_render 时置 True，
        # 让 request_render 跳过 engine 路由（engine 已在外层决策）。
        self._render_from_engine: bool = False

        # Final-2 (FINAL-P0-02): 暂存 engine 的 SourceDecision 列表（按 timeline_frame）。
        # worker._try_gpu_render 取出对应 layer 的 decision，传给
        # EditGpuDecodeManager.get_frame_with_policy() 拒绝 realtime master 解码。
        self._pending_source_decisions: Dict[int, list] = {}

        # P0-01: 暂存 engine 为本帧分配的统一 trace id（按 timeline_frame）。
        # 有界字典（≤8 项，enqueue 时 pop；未被消费的旧项被淘汰），
        # 避免无界容器（P6-04）。
        # P0-02: 值改为 (trace_id, deadline_ns) 元组。
        self._pending_frame_trace_ids: Dict[int, tuple] = {}

        # P0-02: 在飞帧身份（frame → (trace_id, deadline_ns)）。enqueue 消费
        # pending 后转移到这里，供 _on_gpu_frame_ready 的 guard-drop trace
        # 归因（drop 帧恰恰是最需要与 engine telemetry 对拍的）。同样有界。
        self._in_flight_frame_ids: Dict[int, tuple] = {}

        # Final-3 (FINAL-P0-05): GPU 合成器与 frame pool 作为唯一 GPU 合成入口。
        # 在 set_gpu_renderer（GL context 就绪）时实例化并 attach backend。
        self._gpu_compositor: Any = None
        self._gpu_frame_pool: Any = None
        # Stage E3: bounded realtime decode scheduler.  It is created only
        # after the GPU decode manager is available and is shut down with this
        # renderer; the manager itself remains the single decoder/source owner.
        self._realtime_decode_scheduler: Any = None

        # 渲染请求去重
        self._pending_frame: Optional[int] = None
        self._queued_frame: Optional[int] = None
        self._pending_mutex = QMutex()
        self._last_placeholder_key: Optional[tuple[int, int]] = None
        self._pending_placeholder_requests: Dict[tuple[str, int], tuple[int, int, str]] = {}
        self._placeholder_provider_connected: bool = False
        self._placeholder_provider: Any = None
        self._pending_layer_count: int = 0
        self._scrub_release_lock = threading.RLock()
        self._prebuffer_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="TimelineProxyPrebuffer",
        )
        self._prebuffer_lock = threading.Lock()
        self._prebuffer_active_key: tuple[int, int, int] | None = None
        self._prebuffer_completed_keys: set[tuple[int, int, int]] = set()
        # The prefetch window is intentionally bounded. The effective depth
        # is selected between 2 and 4 from frame cadence and AUTO_2K plane
        # bytes, so first-frame work is never hidden behind a large batch.
        self._prebuffer_lookahead_frames: int = 4
        self._prebuffer_stats: Dict[str, int] = {
            "cache_hits": 0,
            "cache_fills": 0,
            "dropped": 0,
            "submitted": 0,
            "last_depth": 0,
        }
        self._prebuffer_submit_delay_ms: int = 1
        # Cut-boundary stream prepositioning: scan the lookahead window once
        # every few presented frames and advance upcoming clip decode streams
        # to their entry frame so boundary frames decode sequentially.
        self._preposition_scan_interval_frames: int = 4
        self._preposition_scan_counter: int = 0
        self._preposition_scheduled_keys: set[tuple[int, str, str, int, bool]] = set()

        # Scrub preview mode. A drag uses a separate 720p decode target and a
        # coalescing single-flight queue. New positions replace only pending
        # work; they do not repeatedly invalidate the decode that is already
        # close to finishing.
        self._scrub_active: bool = False
        self._scrub_latest_frame: int = -1
        self._scrub_exact_frame: int | None = None
        self._scrub_exact_debounce_ms: int = 0
        # 实时 scrub 派发节奏：越短，上一帧渲染完成后新输入越早被派发。
        self._scrub_live_interval_ms: int = 4
        self._scrub_exact_request_token: int = 0
        self._scrub_exact_pending_frame: int | None = None
        self._scrub_exact_in_flight_frame: int | None = None
        self._scrub_exact_timer_pending: bool = False
        self._scrub_settled_timer_token: int = 0
        # 播放头停顿后的精确补帧延迟：越短“松手后画面追上播放头”越快。
        self._scrub_settled_delay_ms: int = 48
        self._scrub_last_dispatch_at: float = 0.0
        # 看门狗：singleShot 回调 / 单飞释放链丢失时的自愈窗口。真实 App
        # 中主线程拥堵会让 exact 派发一次性回调永不执行，闩锁卡死 = 拖拽
        # 画面永久冻结；超窗即强制复位重调度。
        self._SCRUB_TIMER_WATCHDOG_S: float = 0.05
        self._SCRUB_RELEASE_WATCHDOG_S: float = 1.0
        self._scrub_exact_scheduled_at: float = 0.0
        self._scrub_last_real_frame: int | None = None
        self._scrub_last_input_frame: int = -1
        self._scrub_direction: int = 0
        self._scrub_preposition_horizon_frames: int = 4
        self._scrub_request_serial: int = 0
        self._scrub_latest_request_started_at: float = 0.0
        self._scrub_request_started_by_frame: Dict[tuple[int, int], float] = {}
        self._scrub_finish_exact_frame: int | None = None
        # Fast-drag detection (with hysteresis): when scrub inputs arrive
        # faster than ~24ms apart, exact GOP-walking decodes can never keep
        # up, so dispatches switch to coarse keyframe-anchored decodes until
        # the cadence slows back down.
        self._scrub_input_fast: bool = False
        self._scrub_last_input_at: float | None = None
        # Adaptive display stride (1x/2x/4x/8x/16x): fast drags decode a
        # stride-quantized target so completed frames stay displayable
        # instead of finishing as stale drops. Slow drags keep stride=1.
        self._scrub_input_velocity_fps: float = 0.0
        self._scrub_velocity_samples: int = 0
        self._scrub_latest_display_frame: int | None = None
        # EMA of the completed decode-cycle period; 1/period is the display
        # rate the pipeline can actually sustain (per codec / machine).
        self._scrub_cycle_period_ema_s: float | None = None
        self._scrub_last_cycle_release_at: float | None = None

        # 单调帧守卫：防止异步渲染乱序导致画面前后帧来回跳（jitter）。
        # 记录已发射到 viewer 的最大帧号；比该值更旧的渲染结果直接丢弃。
        # 在 set_sequence() / set_project() / 大跳变（seek）时重置为 -1。
        self._last_emitted_frame: int = -1

        # 渲染节流：同一时间只允许一个预览渲染执行。
        # 播放 tick 在渲染中到来时合并为 _queued_frame，完成后只消费
        # 排好的下一帧，避免并发竞争和追墙钟式跳帧。
        self._render_in_flight: bool = False

        # 播放控制器引用（由 file_controller 通过 set_playback_controller 注入）。
        # 用于判断方向；播放状态由 realtime engine 统一从 controller
        # state_changed 派生，避免 panel/renderer 双重通知。
        self._playback_controller: Any = None
        self._playback_activity_signal: Any = None

        # 初始化渲染线程
        self._init_worker()
        self._prewarm_zero_copy_capability()

        logger.info("TimelinePreviewRenderer initialized")

    def _fit_viewport_to_sequence_aspect(self, size: Tuple[int, int]) -> Tuple[int, int]:
        """
        Convert a raw widget viewport into an internal preview render size.

        ProgramViewer can be any shape, but preview textures must keep the
        timeline frame aspect ratio. If the widget size is used directly as the
        FBO size, the GPU path stretches frames before the viewer can fit them.

        Note: The display size is capped only by the viewport widget itself.
        Decode resolution is controlled separately by _decode_target_size
        which applies the preview quality scale.
        """
        raw_w, raw_h = size
        raw_w = max(1, int(raw_w))
        raw_h = max(1, int(raw_h))

        seq_w = int(getattr(self._sequence, "width", 0) or raw_w)
        seq_h = int(getattr(self._sequence, "height", 0) or raw_h)
        if seq_w <= 0 or seq_h <= 0:
            return raw_w, raw_h

        scale = min(raw_w / seq_w, raw_h / seq_h, 1.0)
        fitted_w = max(1, int(round(seq_w * scale)))
        fitted_h = max(1, int(round(seq_h * scale)))
        return fitted_w, fitted_h

    def _get_sequence_resolution(self) -> Tuple[int, int]:
        """Get sequence native resolution; fallback to viewport if unknown."""
        seq_w = int(getattr(self._sequence, "width", 0))
        seq_h = int(getattr(self._sequence, "height", 0))
        if seq_w > 0 and seq_h > 0:
            return (seq_w, seq_h)
        return self._viewport_size

    @property
    def _decode_target_size(self) -> Tuple[int, int]:
        """Return the target for the current dynamic quality stage."""
        if (
            getattr(self, "_decode_quality_stage", "auto2k") == "full"
            and float(getattr(self, "_preview_quality", 0.0)) <= 0.0
        ):
            full_target = getattr(self, "_full_decode_target_size", None)
            if callable(full_target):
                return full_target()
            return self._get_sequence_resolution()
        auto2k_target = getattr(self, "_auto2k_decode_target_size", None)
        if callable(auto2k_target):
            return auto2k_target()
        # Keep lightweight shell/test doubles compatible with the property
        # contract that predates the helper split above.
        base_w, base_h = self._get_sequence_resolution()
        s = self._effective_preview_quality_scale(base_w, base_h)
        if s >= 1.0:
            return (base_w, base_h)
        dw = max(64, int(round(base_w * s)))
        dh = max(64, int(round(base_h * s)))
        if dw % 2:
            dw += 1
        if dh % 2:
            dh += 1
        return (dw, dh)

    def _full_decode_target_size(self) -> Tuple[int, int]:
        """Return the exact sequence geometry for a paused FULL refresh."""
        return self._get_sequence_resolution()

    def _apply_adaptive_playback_scale(self, scale: float) -> float:
        """P1-10: 在 auto/smooth 档播放态应用自适应缩放系数。

        生效条件（任一不满足即原样返回，保护用户显式选择与交互路径）：
        - engine 处于播放态（`_engine_playback_active`）；
        - 非 scrub（scrub 有自己的 latest-only 交互语义）；
        - 预览质量为 auto/smooth（`_preview_quality <= 0`，显式档视为
          用户手动覆盖，自适应不介入）。
        """
        # __dict__.get 兼容 shell/未初始化 QObject 实例（getattr 回落 Qt 元
        # 对象会抛 RuntimeError）。
        adaptive = self.__dict__.get("_adaptive_playback_scale", 1.0)
        if (
            float(adaptive) >= 1.0
            or not self.__dict__.get("_engine_playback_active", False)
            or self.__dict__.get("_scrub_active", False)
            or float(self.__dict__.get("_preview_quality", 0.0)) > 0.0
        ):
            return scale
        return max(0.125, float(scale) * float(adaptive))

    def _auto2k_decode_target_size(self) -> Tuple[int, int]:
        """Return the bounded target used by playback and scrub."""
        base_w, base_h = self._get_sequence_resolution()
        s = self._effective_preview_quality_scale(base_w, base_h)
        s = self._apply_adaptive_playback_scale(s)
        if s >= 1.0:
            return (base_w, base_h)
        dw = max(64, int(round(base_w * s)))
        dh = max(64, int(round(base_h * s)))
        # Ensure YUV dimensions are even.
        if dw % 2:
            dw += 1
        if dh % 2:
            dh += 1
        return (dw, dh)

    @property
    def _scrub_decode_target_size(self) -> Tuple[int, int]:
        """Return the independent 720p-class target used while dragging."""
        base_w, base_h = self._get_sequence_resolution()
        longest = max(1, int(base_w), int(base_h))
        scale = min(1.0, self._scrub_preview_edge / longest)
        if scale >= 1.0:
            return (base_w, base_h)
        dw = max(64, int(round(base_w * scale)))
        dh = max(64, int(round(base_h * scale)))
        if dw % 2:
            dw += 1
        if dh % 2:
            dh += 1
        return (dw, dh)

    def _scrub_render_decode_target_size(self) -> Tuple[int, int]:
        """
        Return the decode target for the exact frame rendered during scrub.

        With the GPU preview pipeline attached, H264/H265 sources should keep
        using the playback-sized native hardware decode source and let the GPU
        scale into the viewer. This prevents a fast drag from warming a 720p
        source, then making play start or track switch open a second source at
        the normal playback size.
        """
        # Scrub is an interactive realtime path too. Keep it on the same
        # bounded target as playback so a 12K source cannot trigger a full
        # decode merely because the GPU preview backend is unavailable.
        auto2k_target = getattr(self, "_auto2k_decode_target_size", None)
        if callable(auto2k_target):
            return auto2k_target()
        return self._decode_target_size

    def _allow_scrub_placeholder_frames(self) -> bool:
        """
        Return whether scrub may display non-exact placeholder imagery.

        The GPU preview path should chase true frames. Nearby CPU cache hits and
        media-pool thumbnails make fast playhead drags look blurry or stale, so
        keep them only for CPU-only preview mode.
        """
        return not self._has_gpu_preview_pipeline()

    def _should_bypass_scrub_cpu_cache(self) -> bool:
        """
        Return whether scrub should avoid CPU QImage cache presentation.

        With the GPU preview pipeline attached, even an exact CPU cache hit can
        be a lower-resolution still-preview frame from an older path. Fast
        drag responsiveness should come from the latest hardware-decoded GPU
        frame, not from showing cached stills that then need repair.
        """
        return self._has_gpu_preview_pipeline()

    def _publish_interactive_scrub_state(self, frame: int, *, active: bool = True) -> None:
        """Publish latest scrub identity to the worker for latest-only dropping."""
        worker = getattr(self, "_worker", None)
        publish = getattr(worker, "set_interactive_scrub_state", None)
        if callable(publish):
            try:
                publish(self._scrub_request_serial, int(frame), bool(active))
            except Exception as error:
                logger.debug(f"TimelinePreviewRenderer: scrub state publish failed: {error}")

    def _begin_scrub_request_latency(self, frame: int) -> float:
        """Record a new scrub input event and return its monotonic start time."""
        self._scrub_request_serial += 1
        self._scrub_latest_request_started_at = time.perf_counter()
        self._publish_interactive_scrub_state(frame, active=True)
        playback_trace(
            "renderer.scrub_input",
            frame=frame,
            scrub_token=self._scrub_request_serial,
        )
        return self._scrub_latest_request_started_at

    def _trace_scrub_frame_ready_latency(self, frame: int, result_type: str) -> None:
        """Trace input-to-ready latency for a real scrub frame."""
        key = (self._generation_id, int(frame))
        started_at = self._scrub_request_started_by_frame.pop(key, None)
        if started_at is None:
            return
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        playback_trace(
            "renderer.scrub_frame_ready_latency",
            frame=frame,
            scrub_token=self._scrub_request_serial,
            result_type=result_type,
            elapsed_ms=elapsed_ms,
        )

    def _trace_scrub_stage_latency(self, frame: int, stage: str, **fields: Any) -> None:
        """Trace a non-terminal scrub stage without consuming the request timer."""
        key = (self._generation_id, int(frame))
        started_at = self._scrub_request_started_by_frame.get(key)
        if started_at is None:
            return
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        playback_trace(
            stage,
            frame=frame,
            scrub_token=self._scrub_request_serial,
            elapsed_ms=elapsed_ms,
            **fields,
        )

    def _effective_preview_quality_scale(self, base_w: int, base_h: int) -> float:
        """
        Resolve preview quality to an actual scale.

        Smooth mode is a 1920-long-edge target (1920×1080 for 16:9), while
        explicit settings are relative to the timeline's native resolution.
        Full/explicit output remains bounded by ``_max_preview_edge`` so a
        deliberate choice cannot accidentally decode an 8K master in the
        live Program viewer.
        """
        explicit = float(getattr(self, "_preview_quality", 0.0))
        longest = max(1, int(base_w), int(base_h))
        max_edge = max(64, int(getattr(self, "_max_preview_edge", 3840)))
        if explicit > 0.0:
            return max(0.125, min(1.0, explicit, max_edge / longest))

        smooth_edge = min(max_edge, _SMOOTH_PREVIEW_EDGE)
        return max(0.125, min(1.0, smooth_edge / longest))

    def _invalidate_render_generation_for_quality(self, reason: str) -> None:
        """Invalidate queued/in-flight work when decode quality changes."""
        self._generation_id += 1
        self._clear_offline_media_diagnostics()
        self._dispatched_generation_id = self._generation_id - 1
        self._sync_gpu_pipeline_generation()

        worker = getattr(self, "_worker", None)
        if worker is not None:
            try:
                worker.set_generation_id(self._generation_id)
                clear_pending = getattr(worker, "clear_pending_requests", None)
                if callable(clear_pending):
                    clear_pending()
            except Exception as error:
                logger.debug("quality generation sync failed: %s", error)

        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        self._render_in_flight = False
        self._pending_source_decisions.clear()
        self._clear_placeholder_requests_for_frame()
        self._clear_prebuffer_state()
        self._clear_stale_pending_present_frames()
        self._reset_emitted_frame_guard()
        playback_trace(
            "renderer.quality_generation_invalidated",
            generation=self._generation_id,
            quality_stage=getattr(self, "_decode_quality_stage", "auto2k"),
            reason=reason,
        )

    def _set_decode_quality_stage(self, stage: str, reason: str) -> None:
        """Switch between bounded AUTO_2K and paused FULL decode stages."""
        stage = "full" if str(stage).lower() == "full" else "auto2k"
        if stage == getattr(self, "_decode_quality_stage", "auto2k"):
            return
        self._decode_quality_stage = stage
        self._invalidate_preview_frame_caches()
        self._invalidate_render_generation_for_quality(reason)

    def _set_adaptive_playback_scale(self, scale: float, reason: str) -> None:
        """P1-10: 设置播放期自适应缩放系数（档位变化的渲染端应用）。

        变更走与质量档切换相同的失效链（缓存 + generation + 在途清理），
        保证后续帧按新目标尺寸解码；换目标尺寸会命中解码管理器的
        64px 桶缓存键（新会话），因此 engine 侧控制器带迟滞与最小驻留。
        """
        scale = max(0.25, min(1.0, float(scale)))
        if abs(scale - float(self.__dict__.get("_adaptive_playback_scale", 1.0))) < 1e-6:
            return
        self._adaptive_playback_scale = scale
        self._invalidate_preview_frame_caches()
        self._invalidate_render_generation_for_quality(reason)
        playback_trace(
            "renderer.adaptive_quality_scale",
            scale=scale,
            reason=reason,
        )

    def _on_engine_quality_pressure_changed(self, level: str, detail: dict) -> None:
        """engine 自适应质量档位变化的渲染端映射（P1-10）。"""
        from src.features.edit.core.realtime_playback.adaptive_quality import (
            level_from_value,
        )
        level_enum = level_from_value(level)
        if level_enum is None:
            return
        self._set_adaptive_playback_scale(
            level_enum.scale,
            reason=f"engine_pressure:{level_enum.value}",
        )

    def set_adaptive_playback_quality_enabled(self, enabled: bool) -> None:
        """P1-10: 用户偏好开关（透传给 engine 的自适应质量控制器）。"""
        engine = getattr(self, "_realtime_engine", None)
        configure = getattr(engine, "configure_adaptive_quality", None)
        if callable(configure):
            try:
                configure(enabled=bool(enabled))
            except Exception:
                logger.debug("adaptive quality preference apply failed", exc_info=True)

    def _cancel_full_quality_refresh(self) -> None:
        """Cancel a delayed FULL refresh using a monotonic timer token."""
        self._quality_transition_token += 1
        self._full_quality_refresh_pending = False
        timer = getattr(self, "_full_quality_refresh_timer", None)
        if timer is not None:
            timer.stop()

    def _schedule_full_quality_refresh(self) -> None:
        """Schedule a bounded-delay FULL replacement after pause settles."""
        if getattr(self, "_scrub_active", False):
            return
        self._quality_transition_token += 1
        token = self._quality_transition_token
        self._full_quality_refresh_token = token
        self._full_quality_refresh_pending = True
        delay_ms = max(150, min(250, int(self._full_quality_delay_ms)))
        playback_trace("renderer.full_quality_schedule", delay_ms=delay_ms, token=token)
        timer = getattr(self, "_full_quality_refresh_timer", None)
        if timer is not None:
            timer.start(delay_ms)

    def _on_full_quality_refresh_timeout(self) -> None:
        """Dispatch the pending refresh from the renderer-owned timer."""
        if not self._full_quality_refresh_pending:
            return
        self._submit_full_quality_refresh(self._full_quality_refresh_token)

    def _submit_full_quality_refresh(self, token: int) -> None:
        """Decode the current paused frame at FULL quality if still relevant."""
        if (
            token != self._quality_transition_token
            or not self._full_quality_refresh_pending
        ):
            return
        self._full_quality_refresh_pending = False
        if (
            getattr(self, "_scrub_active", False)
            or getattr(self, "_engine_playback_active", False)
            or self._is_controller_playing()
        ):
            playback_trace("renderer.full_quality_drop_not_paused", token=token)
            return
        self._set_decode_quality_stage("full", "pause_settled")
        self._playhead_mutex.lock()
        frame = int(self._playhead_frame)
        self._playhead_mutex.unlock()
        playback_trace("renderer.full_quality_dispatch", frame=frame, token=token)
        self.request_render(frame)

    def set_preview_quality(self, scale: float) -> None:
        """
        Set preview proxy resolution scale.

        Args:
            scale: 0.0 = smooth (1920×1080 target for high-resolution
                material), 1.0 = full timeline resolution up to the 4K
                preview ceiling, 0.5 = half, 0.333 = third, 0.25 = quarter
        """
        scale = float(scale)
        if scale <= 0.0:
            scale = 0.0
        else:
            scale = max(0.125, min(1.0, scale))
        if abs(scale - self._preview_quality) < 0.01:
            return
        self._preview_quality = scale
        self._cancel_full_quality_refresh()
        self._set_decode_quality_stage("auto2k", "manual_quality_change")
        if scale <= 0.0:
            logger.info(
                "TimelinePreviewRenderer: preview quality set to auto "
                f"(max edge {self._max_preview_edge}px)"
            )
        else:
            logger.info(f"TimelinePreviewRenderer: preview quality set to {scale:.2f}")

        # Clear cache — old frames are at a different resolution
        self._invalidate_preview_frame_caches()
        self._clear_prebuffer_state()

        # Propagate quality to worker
        if self._worker:
            self._worker.set_preview_quality(scale)

        # Invalidate decode sources — they cache the target_size
        if self._worker and hasattr(self._worker, '_gpu_decode_manager') and self._worker._gpu_decode_manager:
            self._worker._gpu_decode_manager.invalidate_all_sources()

        # Reset monotonic guard so the first frame at new quality is accepted
        self._reset_emitted_frame_guard()
        self._render_in_flight = False

        # Request current frame re-render
        self._schedule_current_frame_warmup()

    def set_no_deblock(self, enabled: bool) -> None:
        """预览关闭输出去块（P6 v2.1 后续，2026-09-22）。

        弱机提速开关：滤波占解码时间 1.19–2.02×（低档最大，见
        docs/codec/topos_speed_matrix_2k_4k_2026-09-22.md），代价是 8×8 块界
        台阶回到未滤波水平（低档 ~1.4–3.2 LSB @10-bit，预览里可见为小色块）。
        只作用于预览解码 request；导出/交付路径不经过这里，保持滤波。

        携带旧 flag 的 source 必须失效（request 在 open() 时冻结），并重渲
        当前帧——与 set_preview_quality 同一套路。
        """
        enabled = bool(enabled)
        if enabled == self._no_deblock:
            return
        self._no_deblock = enabled
        logger.info(
            "TimelinePreviewRenderer: preview no-deblock = %s（预览跳过输出"
            "去块；交付路径不受影响）", enabled,
        )
        self._invalidate_preview_frame_caches()
        self._clear_prebuffer_state()
        if self._worker:
            self._worker.set_no_deblock(enabled)
        self._reset_emitted_frame_guard()
        self._render_in_flight = False
        self._schedule_current_frame_warmup()

    def _init_worker(self) -> None:
        """初始化后台渲染线程"""
        self._worker = PreviewRenderWorker(
            frame_provider=self._frame_provider,
            asset_resolver=self._asset_resolver,
            video_renderer=self._video_renderer,
            timeline_sequence=self._sequence,
            viewport_size=self._viewport_size,
            max_preview_edge=self._max_preview_edge,
        )
        # P1-02：worker 重建时转发已注入的子序列解析器
        if getattr(self, "_sequence_resolver", None) is not None:
            self._worker._sequence_resolver = self._sequence_resolver

        # 连接信号
        self._worker.frame_rendered.connect(self._on_frame_rendered)
        self._worker.render_error.connect(self._on_render_error)
        self._worker.offline_media.connect(self._on_worker_offline_media)
        self._worker.background_frame_ready.connect(self._on_worker_background_frame_ready)
        self._worker.set_gpu_submitted_callback(self._on_worker_gpu_submitted_direct)
        self._worker.gpu_submitted.connect(self._on_worker_gpu_submitted)
        # 渲染耗时回填引擎遥测（decode/total/effective_fps 失真修复）
        self._worker.set_render_trace_hook(self._on_worker_render_trace)

        # 启动线程
        self._worker.start()

    def _on_worker_offline_media(self, frame: int, items: object) -> None:
        """Cache the worker's offline diagnosis for the matching UI result."""
        if not isinstance(items, (list, tuple)):
            return
        normalized = tuple(
            dict(item) for item in items if isinstance(item, dict)
        )
        if not normalized:
            return
        self._offline_media_by_frame[int(frame)] = (
            self._generation_id,
            normalized,
        )
        # Offline diagnostics are transient frame metadata, not a second
        # unbounded frame cache.
        if len(self._offline_media_by_frame) > 64:
            oldest = next(iter(self._offline_media_by_frame))
            self._offline_media_by_frame.pop(oldest, None)

    def offline_media_for_frame(self, frame: int) -> tuple[dict[str, str], ...]:
        """Consume offline diagnostics associated with one rendered frame."""
        entry = self._offline_media_by_frame.pop(int(frame), None)
        if entry is None or entry[0] != self._generation_id:
            return ()
        return entry[1]

    def _clear_offline_media_diagnostics(self) -> None:
        """Drop diagnostics that belong to a previous render generation."""
        self._offline_media_by_frame.clear()

    def _on_worker_render_trace(
        self,
        frame: int,
        decode_ms: float,
        total_ms: float,
        composite_ms: float,
        trace_id: int = 0,
    ) -> None:
        """worker 渲染完成：把真实管线耗时回填到该帧的遥测记录。

        P0-01: trace_id 非零时按 trace_id 精确回填（消除同帧多次
        重渲染/跳帧时的匹配歧义）。
        """
        engine = getattr(self, "_realtime_engine", None)
        telemetry = getattr(engine, "telemetry", None)
        enrich = getattr(telemetry, "enrich_last", None)
        if callable(enrich):
            try:
                enrich(int(frame), decode_ms, total_ms, composite_ms,
                       trace_id=int(trace_id or 0))
            except Exception:
                pass

    def _prewarm_zero_copy_capability(self) -> None:
        """Start the global macOS VT zero-copy capability probe before playback."""
        try:
            from src.shared.media.macos_vt.capability import prewarm_zero_copy_capability
            prewarm_zero_copy_capability(blocking=False)
        except Exception as e:
            logger.debug(f"TimelinePreviewRenderer: zero-copy capability prewarm failed: {e}")

    def invalidate_comp_clip_cache(self, composite_id: str | None = None) -> None:
        """清除 comp_clip 帧缓存。

        合成图变化（节点增删改、参数调整等）时必须调用，否则循环播放会命中
        旧的缓存帧，导致内容/色彩跳变（尤其色彩转换修正后旧帧是线性空间的）。

        Args:
            composite_id: 指定图 ID 则只清该图的缓存；None 清全部。
        """
        if self._worker is not None:
            self._worker.invalidate_comp_clip_cache(composite_id)

    def set_gpu_renderer(
        self,
        gpu_decode_manager: Any,
        gpu_renderer: Any,
    ) -> None:
        """
        设置 GPU 渲染器（在 GL 上下文初始化后调用）。

        Args:
            gpu_decode_manager: EditGpuDecodeManager 实例
            gpu_renderer: EditGpuPreviewRenderer 实例
        """
        if self._worker is None:
            logger.warning("Cannot set GPU renderer: worker not initialized")
            return

        self._gpu_renderer = gpu_renderer
        self._worker._gpu_decode_manager = gpu_decode_manager
        self._worker._gpu_renderer = gpu_renderer

        # Stage E3: wire the bounded scheduler to the same decode manager used
        # by the worker.  The scheduler prefetches selected proxy/cache frames
        # in its own single-flight executor; the worker consumes a complete
        # ready bundle and still submits the existing rich EditRenderTask.
        try:
            from src.features.edit.core.realtime_playback import TimelineDecodeScheduler

            old_scheduler = getattr(self, "_realtime_decode_scheduler", None)
            if old_scheduler is not None:
                old_scheduler.shutdown()
            scheduler = TimelineDecodeScheduler(default_capacity=24)
            self._realtime_decode_scheduler = scheduler
            self._realtime_engine.configure_decode_scheduler(
                scheduler,
                radius_before=2,
                radius_after=8,
                direction_provider=self._playback_prebuffer_step,
                rate_provider=self._playback_rate_multiplier,
                config_provider=self._build_realtime_scheduler_config,
            )
            self._realtime_engine.sync_generation_id(self._generation_id)
            self._worker.set_realtime_decode_scheduler(scheduler)
            logger.debug("TimelinePreviewRenderer: realtime decode scheduler attached")
        except Exception as e:
            self._realtime_decode_scheduler = None
            logger.debug(f"TimelinePreviewRenderer: scheduler attach failed: {e}")

        # Final-3: TimelineGpuCompositor is the single GPU submission and
        # signal boundary.  The worker still builds the complete native
        # EditRenderTask so effect/adjustment/transition metadata is lossless;
        # the compositor owns the final backend handoff and present forwarding.
        try:
            from src.features.edit.core.realtime_playback import TimelineGpuCompositor

            old_compositor = getattr(self, "_gpu_compositor", None)
            if old_compositor is not None:
                old_compositor.shutdown()
            compositor = TimelineGpuCompositor()
            compositor.attach_backend(gpu_renderer)
            if not bool(getattr(compositor, "_connected", False)):
                compositor.shutdown()
                raise RuntimeError("compositor backend signals unavailable")
            compositor.reset_generation(self._generation_id)
            self._gpu_compositor = compositor
            self._worker.set_gpu_compositor(compositor)
        except Exception as e:
            logger.debug(f"TimelineGpuCompositor attach failed: {e}")
            self._gpu_compositor = None
            self._worker.set_gpu_compositor(None)

        # GpuFramePool manages optional upload/resident-frame accounting.  The
        # scheduler's external VT/planar payloads remain zero-copy inputs to
        # EditGpuPreviewRenderer and are not uploaded from the decode thread.
        try:
            from src.features.edit.core.realtime_playback.gpu_frame_pool import GpuFramePool
            self._gpu_frame_pool = GpuFramePool(max_resident=64)
            logger.debug(
                "TimelinePreviewRenderer: GpuFramePool and TimelineGpuCompositor attached"
            )
        except Exception as e:
            logger.debug(f"GpuFramePool attach failed: {e}")
            self._gpu_frame_pool = None

        # Connect the compositor's forwarded frame_ready so all GPU output
        # follows the same cache + generation pipeline as CPU results.  A
        # direct backend connection is retained only if compositor setup was
        # unavailable (headless/test fallback).
        signal_source = (
            self._gpu_compositor
            if self._gpu_compositor is not None
            and bool(getattr(self._gpu_compositor, "_connected", False))
            else gpu_renderer
        )
        signal_source.frame_ready.connect(self._on_gpu_frame_ready)

        # 合成快照回填：暂停态 seek/scrub-settle 的 GPU 帧以 1/4 分辨率
        # QImage 进入 CPU 帧缓存，让 seek 回看/邻域先显有数据源
        # （GPU present 帧本身不能进 CPU 缓存——纹理归 present pool 管）。
        if hasattr(gpu_renderer, "snapshot_ready"):
            gpu_renderer.snapshot_ready.connect(self._on_gpu_snapshot_ready)

        # Batch 1 (缺陷计划 RP-P0-01/02): present frame descriptor + ack 协议。
        # present_frame_ready 携带 GpuPresentFrame（fence + slot_id），
        # 转发到 ProgramViewer 的 set_present_frame，让 Viewer 在 blit 前
        # glWaitSync、blit 后 ack slot。
        if hasattr(signal_source, 'present_frame_ready'):
            signal_source.present_frame_ready.connect(self._on_present_frame_ready)
        # slot_acked 由 Viewer ack 时触发（via ack_present_slot），用于 telemetry。
        if hasattr(gpu_renderer, 'slot_acked'):
            gpu_renderer.slot_acked.connect(self._on_present_slot_acked)
        # P3.2：GL 后端错误（如复合缓存竞态 miss）→ 重新请求该帧。
        signal_source.render_error.connect(self._on_gpu_backend_render_error)
        # W03（计划 W-I02）：cuda 驻留导入失败按素材上报 → 解码管理器停该
        # 素材驻留路由、租约归零后关旧源，下一帧经下载 planar/CPU 重建
        # （单素材故障隔离，不影响其它轨）。方法借用方（shell 模式）可能
        # 未带此槽——缺槽时跳过连接（该 shell 无驻留恢复链，行为同旧版）。
        _resident_failed_slot = getattr(
            self, "_on_cuda_resident_import_failed", None)
        if (hasattr(gpu_renderer, 'cuda_resident_import_failed')
                and callable(_resident_failed_slot)):
            gpu_renderer.cuda_resident_import_failed.connect(
                _resident_failed_slot)

        prewarm_capability = getattr(self, "_prewarm_zero_copy_capability", None)
        if callable(prewarm_capability):
            prewarm_capability()
        prewarm_sequence = getattr(self, "_prewarm_sequence_decode_sources", None)
        if callable(prewarm_sequence):
            prewarm_sequence(self._sequence)
        schedule_warmup = getattr(self, "_schedule_current_frame_warmup", None)
        if callable(schedule_warmup):
            schedule_warmup()

        # P0-05: 把本页 GPU 资源池注册进全局资源快照（1s 周期采样 + 峰值）。
        try:
            from src.shared.diagnostics.resource_snapshot import get_resource_registry
            registry = get_resource_registry()
            if hasattr(gpu_renderer, "present_pool_stats"):
                registry.register(
                    "playback", "edit_present_pool", gpu_renderer.present_pool_stats
                )
            if hasattr(self, "gpu_pipeline_stats"):
                registry.register(
                    "playback", "edit_gpu_pipeline", self.gpu_pipeline_stats
                )
            if self._gpu_frame_pool is not None and hasattr(self._gpu_frame_pool, "stats"):
                registry.register(
                    "playback", "edit_gpu_frame_pool", self._gpu_frame_pool.stats
                )
        except Exception as e:
            logger.debug(f"resource snapshot registration failed: {e}")

        logger.info("TimelinePreviewRenderer: GPU renderer configured")

    def set_comp_gl_context(self, main_context: Any) -> None:
        """注入主 GL context，供 worker 渲染 comp_clip（在 GL 就绪后调用）。

        在 GUI 线程为 worker 创建共享 context + offscreen surface，
        转交 worker 线程所有权。后续 _render_comp_clip_frame 会 makeCurrent 它。
        必须在 GUI 线程调用（QOffscreenSurface / context.create() 的平台要求）。
        """
        if self._worker is None or main_context is None:
            return
        self._main_context = main_context
        try:
            self._worker.setup_comp_gl_context(main_context)
        except Exception as e:
            logger.error(f"TimelinePreviewRenderer: set_comp_gl_context failed: {e}", exc_info=True)

    def _on_present_frame_ready(self, present_frame: object) -> None:
        """
        Batch 1: 收到 GpuPresentFrame，转发到 ProgramViewer。

        通过 generation/monotonic 守卫后，调用 viewer.set_present_frame，
        把 fence + slot_id + ack callback 一并传过去。
        """
        try:
            slot_id = getattr(present_frame, 'slot_id', -1)
            generation_id = getattr(present_frame, 'generation_id', 0)
            frame = getattr(present_frame, 'frame', -1)
        except Exception:
            return

        # Generation / monotonic 守卫：丢弃 stale present frame。
        if generation_id != self._dispatched_generation_id:
            playback_trace(
                "renderer.present_drop_generation",
                frame=frame, generation=generation_id,
                dispatched=self._dispatched_generation_id,
                slot_id=slot_id,
            )
            # A0.1：显式丢弃事件（present_status='dropped'）。
            self._annotate_present_drop(frame, slot_id, "generation_mismatch")
            # 仍然要 ack 这个 slot，避免泄漏（stale frame 的 slot 不再展示）
            self._ack_present_slot_safe(slot_id)
            return

        if self._is_playback_result_stale(frame):
            playback_trace(
                "renderer.present_drop_playback_stale",
                frame=frame, slot_id=slot_id,
            )
            self._annotate_present_drop(frame, slot_id, "playback_stale")
            self._ack_present_slot_safe(slot_id)
            return

        self._stash_pending_present_frame(frame, generation_id, present_frame)
        # A0.1：descriptor 已发出（fence 就绪）= submitted；同时登记
        # slot→frame 映射供 ack 侧 annotate slot_released。
        self._annotate_present_status(frame, "submitted", slot_id=int(slot_id))
        self._note_slot_frame(int(slot_id), int(frame))
        self._note_telemetry_present(frame, "submitted")

        # ``present_frame_ready`` and the legacy completion signal cross the
        # GPU and UI threads independently.  Under a busy GL queue the legacy
        # signal can arrive first; realtime mode then rejects its naked texture
        # and clears the in-flight marker.  Consume the late, fenced descriptor
        # here instead of leaving the Program Viewer frozen on the old frame.
        late_key = getattr(self, "_late_realtime_present_key", None)
        if late_key == (int(frame), int(generation_id)):
            self._late_realtime_present_key = None
            accepted = self._pop_pending_present_frame(frame, generation_id)
            if accepted is not None:
                gpu_renderer = getattr(self, "_gpu_renderer", None)
                ack_cb = (
                    gpu_renderer.ack_present_slot
                    if gpu_renderer is not None and hasattr(gpu_renderer, "ack_present_slot")
                    else None
                )
                if self._should_emit_frame(frame):
                    self._mark_frame_emitted(frame)
                    playback_trace(
                        "renderer.present_frame_late_emit",
                        frame=frame,
                        slot_id=getattr(accepted, "slot_id", -1),
                        generation=generation_id,
                    )
                    self.frame_ready.emit(frame, {
                        "present_frame": accepted,
                        "ack_cb": ack_cb,
                        # A0.1：Viewer 侧呈现状态 annotate 的身份。
                        "trace_id": self._lookup_frame_trace_id(int(frame)),
                    })
                else:
                    self._annotate_present_drop(
                        frame, getattr(accepted, "slot_id", -1),
                        "late_emit_monotonic_rejected")
                    self._ack_present_slot_safe(getattr(accepted, "slot_id", -1))
            return
        playback_trace(
            "renderer.present_frame_stashed",
            frame=frame, slot_id=slot_id, generation=generation_id,
        )

    def _on_present_slot_acked(self, slot_id: int) -> None:
        """Viewer 完成 blit 后的 telemetry 钩子。"""
        playback_trace("renderer.present_slot_acked", slot_id=slot_id)
        # A0.1：slot 回收 = 资源协议终点。仅当该帧未被 TPR 侧标记为
        # dropped/superseded 时 annotate slot_released（丢弃帧的终态
        # 保留 dropped，不被 slot_released 覆盖）。
        self._annotate_present_status_for_slot(slot_id, "slot_released")

    def _annotate_present_status(
        self, frame: int, status: str, *,
        slot_id: int | None = None, reason: str | None = None,
        record_state: bool = True,
    ) -> None:
        """A0.1：呈现状态事件写入发起帧 trace（迟到 annotate 通道）。

        submitted 事件附带 present_submitted_mono（displayed 时计算
        present_ms）。trace 滚出 ring / 旁路帧由 annotate 失败计数兜底。
        """
        try:
            trace_id = self._lookup_frame_trace_id(int(frame))
            if not trace_id:
                return
            from src.shared.video.playback_frame_trace import get_trace_collector
            fields: dict = {"present_status": status}
            if slot_id is not None:
                fields["present_slot_id"] = int(slot_id)
            if status == "submitted":
                fields["present_submitted_mono"] = time.monotonic()
            if reason:
                fields["extra"] = {"present_drop_reason": reason}
            get_trace_collector().annotate(trace_id, **fields)
        except Exception:
            pass
        if record_state:
            self._record_frame_present_state(int(frame), status)

    def _annotate_present_status_for_slot(self, slot_id: int, status: str) -> None:
        """A0.1：按 slot_id 反查发起帧后 annotate（slot→frame 映射有界 64）。"""
        if slot_id < 0:
            return
        slot_map = getattr(self, "_present_slot_frames", None)
        if not slot_map:
            return
        entry = slot_map.pop(int(slot_id), None)
        if entry is None:
            return
        frame = int(entry)
        state_map = getattr(self, "_frame_present_state", None) or {}
        if state_map.get(frame) in ("dropped", "superseded"):
            return
        self._annotate_present_status(frame, status, slot_id=int(slot_id))

    def _record_frame_present_state(self, frame: int, status: str) -> None:
        state = getattr(self, "_frame_present_state", None)
        if state is None:
            state = {}
            self._frame_present_state = state
        state[int(frame)] = status
        while len(state) > 256:
            oldest = next(iter(state))
            state.pop(oldest, None)

    def _note_slot_frame(self, slot_id: int, frame: int) -> None:
        if slot_id < 0:
            return
        slot_map = getattr(self, "_present_slot_frames", None)
        if slot_map is None:
            slot_map = {}
            self._present_slot_frames = slot_map
        slot_map[int(slot_id)] = int(frame)
        while len(slot_map) > 64:
            oldest = next(iter(slot_map))
            slot_map.pop(oldest, None)

    def _note_telemetry_present(self, frame: int, outcome: str) -> None:
        """A0.1：TPR 可观测的呈现结果同步进 engine telemetry（尽力而为）。"""
        try:
            engine = getattr(self, "_realtime_engine", None)
            telemetry = getattr(engine, "telemetry", None)
            note = getattr(telemetry, "note_present_outcome", None)
            if callable(note):
                note(self._lookup_frame_trace_id(int(frame)), int(frame), outcome)
        except Exception:
            pass

    def _annotate_present_drop(
        self, frame: int, slot_id: int, reason: str,
    ) -> None:
        """A0.1：present descriptor 在显示前被丢弃（切代/stale/守卫拒绝）。"""
        self._annotate_present_status(
            frame, "dropped", slot_id=int(slot_id), reason=reason)
        self._note_telemetry_present(frame, "dropped")

    def _stash_pending_present_frame(
        self, frame: int, generation_id: int, present_frame: object
    ) -> None:
        """Store a GpuPresentFrame until the legacy frame_ready gate accepts it."""
        pending = getattr(self, "_pending_present_frames", None)
        if pending is None:
            pending = {}
            self._pending_present_frames = pending
        key = (int(frame), int(generation_id))
        previous = pending.get(key)
        if previous is not None and previous is not present_frame:
            # A0.1：同帧旧 descriptor 被新 descriptor 顶掉 = superseded
            # （未显示即回收；此前无事件无计数）。
            self._annotate_present_status(
                frame, "superseded",
                slot_id=int(getattr(previous, "slot_id", -1)),
                reason="stash_overwritten")
            self._note_telemetry_present(frame, "superseded")
            self._ack_present_slot_safe(getattr(previous, "slot_id", -1))
        pending[key] = present_frame

    def _pop_pending_present_frame(self, frame: int, generation_id: int) -> object | None:
        """Take the GpuPresentFrame for a frame/generation if renderer emitted one."""
        pending = getattr(self, "_pending_present_frames", None)
        if not pending:
            return None
        return pending.pop((int(frame), int(generation_id)), None)

    def _ack_present_slot_safe(self, slot_id: int) -> None:
        """安全 ack slot（避免 stale frame 泄漏 slot）。"""
        if slot_id < 0:
            return
        gpu_renderer = getattr(self, '_gpu_renderer', None)
        if gpu_renderer is not None and hasattr(gpu_renderer, 'ack_present_slot'):
            try:
                gpu_renderer.ack_present_slot(slot_id)
            except Exception:
                pass

    def _clear_stale_pending_present_frames(self) -> None:
        """清空所有暂存的 present frame，并 ack 它们的 slot。

        在 seek / set_sequence / set_project / generation 切换时调用，
        防止上一代 generation 的 GpuPresentFrame 在 _on_gpu_frame_ready
        里被 pop 出来当成新帧显示（stale frame 泄漏 → 闪现旧画面）。
        """
        pending = getattr(self, "_pending_present_frames", None)
        if not pending:
            return
        for present in pending.values():
            slot_id = getattr(present, "slot_id", -1)
            # A0.1：切代清空 = 该帧在被显示前丢弃（dropped）。
            self._annotate_present_drop(
                int(getattr(present, "frame", -1)), slot_id, "generation_switch")
            if slot_id >= 0:
                self._ack_present_slot_safe(slot_id)
        pending.clear()

    def shutdown(self) -> None:
        """关闭渲染器"""
        self._clear_offline_media_diagnostics()
        # P1-01: engine 与 renderer 同生命周期——不主动 shutdown 会留下
        # 采样/信号连接（泄漏审计 P6-03：关闭工程后归零）。
        engine = getattr(self, "_realtime_engine", None)
        if engine is not None:
            try:
                engine.shutdown()
            except Exception as e:
                logger.debug(f"engine shutdown failed: {e}")
        scheduler = getattr(self, "_realtime_decode_scheduler", None)
        if scheduler is not None:
            try:
                scheduler.shutdown()
            except Exception as e:
                logger.debug(f"realtime scheduler shutdown failed: {e}")
            self._realtime_decode_scheduler = None
        # Invalidate pending delayed callbacks before workers or the QObject
        # are torn down. The child timer is also destroyed with this renderer.
        self._cancel_full_quality_refresh()
        self._prebuffer_executor.shutdown(wait=True, cancel_futures=True)

        # 停止渲染线程
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)  # QThread.wait(ms) 不接受 timeout 关键字参数
            self._worker.cleanup_clips()
            # AUD-18909：线程已退出但 finished 延迟销毁尚未派发时，补一次
            # comp GL context/surface 销毁（幂等；线程仍在运行则绝不销毁）。
            if not self._worker.isRunning():
                try:
                    self._worker._teardown_comp_gl_resources()
                except Exception as e:
                    logger.debug(f"comp GL resource teardown failed: {e}")
            # 捕获解码管理器引用（worker 置 None 前），线程停止后再关闭
            # VT 源——若留给解释器拆除阶段 GC，VT 会话/IOSurface 与已销毁
            # 的 GL context 的销毁顺序不可控，退出时段错误。
            gpu_decode_manager = getattr(self._worker, "_gpu_decode_manager", None)
            self._worker = None
        else:
            gpu_decode_manager = None

        compositor = getattr(self, "_gpu_compositor", None)
        if compositor is not None:
            try:
                compositor.shutdown()
            except Exception as e:
                logger.debug(f"TimelineGpuCompositor shutdown failed: {e}")
            self._gpu_compositor = None

        # 停止 GPU 渲染线程
        if hasattr(self, '_gpu_renderer') and self._gpu_renderer is not None:
            self._gpu_renderer.stop()
            self._gpu_renderer.wait(3000)
            self._gpu_renderer = None

        # 线程全部停止后关闭解码源（VT 会话）
        if gpu_decode_manager is not None:
            try:
                gpu_decode_manager.close_all()
            except Exception as e:
                logger.warning(f"Failed to close GPU decode manager sources: {e}")

        self._disconnect_placeholder_provider()
        self._clear_placeholder_requests_for_frame()
        self._clear_prebuffer_state()

        # 清理缓存
        self._invalidate_preview_frame_caches()

        if self._video_renderer:
            try:
                self._video_renderer.stop_thread()
            except Exception as e:
                logger.warning(f"Failed to stop timeline GPU preview renderer: {e}")
            self._video_renderer = None

        logger.info("TimelinePreviewRenderer shutdown")

    # ================================================================
    # 公共接口
    # ================================================================

    # ==================== 单调帧守卫 ====================

    _SEEK_JUMP_THRESHOLD: int = 10  # 帧号跳变超过此值视为 seek
    _PLAYBACK_STALE_TOLERANCE_FRAMES: int = 6
    # A timer callback may arrive a frame late even when decode and present are
    # healthy. Do not turn that ordinary event-loop jitter into a controller
    # re-anchor; only hold the clock after a real multi-frame backlog exists.
    _PLAYBACK_DEFER_LAG_FRAMES: int = 3
    # A completed scrub seek can be a little behind the mouse, but it must be
    # close enough to act as a live frame rather than a misleading old image.
    _SCRUB_RESULT_MAX_LAG_FRAMES: int = 6

    def _reset_emitted_frame_guard(self) -> None:
        """
        重置单调帧守卫（seek / 时间线变化时调用）。

        递增 epoch 以确保旧 epoch 的渲染结果被丢弃。
        """
        self._last_emitted_frame = -1
        self._frame_epoch = getattr(self, '_frame_epoch', 0) + 1
        self._playback_direction = 1  # 默认正向

    def _set_playback_direction(self, forward: bool) -> None:
        """
        设置当前播放方向，用于帧序守卫判定。

        Args:
            forward: True 为正向播放，False 为反向播放
        """
        direction = 1 if forward else -1
        if getattr(self, '_playback_direction', 1) != direction:
            self._playback_direction = direction
            # 方向切换时重置守卫
            self._last_emitted_frame = -1

    def _should_emit_frame(self, frame: int) -> bool:
        """
        判断是否应该发射此帧到 viewer。

        根据播放方向和 epoch 判定：
        - 非播放状态（暂停/逐帧步进）：总是允许（守卫仅用于播放乱序防护）
        - 首次帧（guard == -1）：总是允许
        - 正向播放：帧号 >= 已发射帧号才允许
        - 反向播放：帧号 <= 已发射帧号才允许
        - 帧号跳变超过阈值：视为 seek，重置并允许

        Args:
            frame: 渲染完成的帧号

        Returns:
            True 表示应该发射此帧
        """
        # BUG FIX: 非播放状态（暂停、逐帧步进）时绕过单调守卫。
        # 单调守卫是为防止播放期间异步渲染乱序导致画面抖动，
        # 但暂停/逐帧时每次只请求一帧，不存在乱序问题。
        # 不绕过会导致向后逐帧被永久阻断（frame < last_emitted → 永远丢弃）。
        controller = self._playback_controller
        if controller is not None:
            try:
                state = controller.state
                state_value = getattr(state, "value", state)
                if state_value != "playing":
                    return True
            except Exception:
                pass

        if self._last_emitted_frame < 0:
            return True

        direction = getattr(self, '_playback_direction', 1)

        if direction >= 0:
            # 正向播放：允许 >= last_emitted 的帧
            if frame >= self._last_emitted_frame:
                return True

            # 帧号落后 — 可能是 seek 或方向切换
            backward_delta = self._last_emitted_frame - frame
            if backward_delta > self._SEEK_JUMP_THRESHOLD:
                # 大幅回退：视为 seek 或反向播放开始，重置守卫
                self._last_emitted_frame = -1
                return True

            # 小幅回退：异步渲染乱序，丢弃旧帧
            return False
        else:
            # 反向播放：允许 <= last_emitted 的帧
            if frame <= self._last_emitted_frame:
                return True

            # 帧号超前 — 可能是 seek 或方向切换
            forward_delta = frame - self._last_emitted_frame
            if forward_delta > self._SEEK_JUMP_THRESHOLD:
                self._last_emitted_frame = -1
                return True

            return False

    def _mark_frame_emitted(self, frame: int) -> None:
        """记录已发射帧号（更新守卫）"""
        direction = getattr(self, '_playback_direction', 1)
        if direction >= 0:
            if frame > self._last_emitted_frame:
                self._last_emitted_frame = frame
        else:
            if self._last_emitted_frame < 0 or frame < self._last_emitted_frame:
                self._last_emitted_frame = frame
        # 标记本播放会话已成功发射过帧
        self._playback_session_emitted = True

    def _advance_last_emitted_to_dropped_frame(self, dropped_frame: int) -> None:
        """
        单调守卫丢弃帧后，推进 _last_emitted_frame 到被丢弃帧号。

        目的：防止 _advance_to_next_frame 重新请求同一帧导致死循环。
        _advance_to_next_frame 的非播放路径用
        ``next_frame == _last_emitted_frame`` 判断是否跳过，
        如果不更新，被丢弃帧永远不等于 _last_emitted_frame，循环永不终止。

        不设置 _playback_session_emitted：丢弃帧并未实际显示。

        Args:
            dropped_frame: 被守卫丢弃的帧号
        """
        direction = getattr(self, '_playback_direction', 1)
        if direction >= 0:
            if dropped_frame > self._last_emitted_frame:
                self._last_emitted_frame = dropped_frame
        else:
            if self._last_emitted_frame < 0 or dropped_frame < self._last_emitted_frame:
                self._last_emitted_frame = dropped_frame

    def _is_playback_result_stale(self, frame: int) -> bool:
        """
        Return whether a completed render is too far behind the live playhead.

        During active playback the renderer is paced by the timer/panel
        backpressure loop: if rendering is late, the controller is re-anchored
        to the visible frame before it can run far ahead.  Dropping a completed
        playback frame because it is behind the wall-clock target reintroduces
        the old jitter pattern: render N, controller reaches N+k, drop N, then
        jump-request N+k.  Keep playback frame-accurate here and let the
        monotonic guard reject only true out-of-order results.

        Also check when PAUSED: in-flight renders from the previous playback
        session may complete long after the playhead has moved. Showing those
        stale results (e.g. frame 97 when playhead is at 199) causes visible
        jump-back before the next play session starts.
        """
        controller = self._playback_controller
        if controller is None:
            return False

        try:
            state = controller.state
            state_value = getattr(state, "value", state)
            current_frame = int(controller.current_frame)
        except Exception:
            return False

        if self._last_emitted_frame < 0:
            return False

        # BUG FIX: 播放刚开始、本播放会话尚未成功显示任何帧时，
        # 接受第一个完整 GPU 帧结果，避免蓝屏/冻帧。
        # _last_emitted_frame 可能由播放前的预览渲染设置（非 -1），
        # 所以不能仅靠 _last_emitted_frame < 0 判断。
        if not getattr(self, '_playback_session_emitted', False):
            return False

        tolerance = self._PLAYBACK_STALE_TOLERANCE_FRAMES

        if state_value == "playing":
            return False

        # PAUSED：丢弃远离 playhead 的旧渲染结果。
        # 暂停后，之前播放会话的进行中渲染可能在很晚才完成，
        # 显示这些旧帧会导致画面跳回到旧位置。
        if state_value == "paused":
            return abs(frame - current_frame) > tolerance

        return False

    def _is_controller_playing(self) -> bool:
        """Return whether the attached playback controller is actively playing."""
        controller = self._playback_controller
        if controller is None:
            return False
        try:
            state = controller.state
            return getattr(state, "value", state) == "playing"
        except Exception:
            return False

    def _has_gpu_preview_pipeline(self) -> bool:
        """
        Return whether the edit preview has a GPU decode/render pipeline attached.

        The objects are attached before the shared-GL render thread finishes
        initialising.  That intermediate state is *not* a usable pipeline:
        ``PreviewRenderWorker._try_gpu_render`` deliberately reports
        ``gpu_renderer_not_ready`` there.  Treating mere object attachment as
        a live pipeline would then suppress the CPU/cache fallback, leaving a
        one-clip timeline with no frame at all when the shared context times
        out (the exact failure reported by the app log).

        Realtime playback must not silently fall back to CPU QImage when a
        *ready* GPU pipeline is present.  A media player can hold/rebuffer
        until that hardware path produces a presentable frame; while it is
        still initialising, however, playback must remain usable through the
        normal cached/CPU path.
        """
        worker = getattr(self, "_worker", None)
        if worker is None:
            return False
        gpu_decode_manager = getattr(worker, "_gpu_decode_manager", None)
        gpu_renderer = getattr(worker, "_gpu_renderer", None)
        if gpu_decode_manager is None or gpu_renderer is None:
            return False

        # EditGpuPreviewRenderer exposes ``is_initialized`` as a property.
        # Keep the defensive callable form for test doubles and older
        # backends, where it may still be implemented as a method.  A backend
        # without this readiness flag retains the historical attached-pipeline
        # behaviour; only an explicit False disables GPU-only playback.
        try:
            renderer_ready = getattr(gpu_renderer, "is_initialized", True)
            if callable(renderer_ready):
                renderer_ready = renderer_ready()
        except Exception:
            renderer_ready = False
        return bool(renderer_ready)

    def _should_use_gpu_only_realtime_preview(self) -> bool:
        """Return True when realtime playback should not display CPU preview frames."""
        return (
            not getattr(self, "_scrub_active", False)
            and self._is_controller_playing()
            and self._has_gpu_preview_pipeline()
        )

    def _should_skip_cpu_fallback_on_gpu_miss(self) -> bool:
        """
        Return whether realtime playback should hold the last frame on GPU misses.

        Once the GPU pipeline is attached, playback should behave like a media
        player: hold/rebuffer on GPU/decode misses instead of synchronously
        decoding RGB/QImage on the clock. When no GPU pipeline exists (tests,
        headless mode, old GL init failure), keep the CPU fallback available.
        """
        if getattr(self, "_scrub_active", False):
            return False
        if not self._is_controller_playing():
            return False
        # A previous frame may already have been emitted in this playback
        # session, but that must not turn a failed/uninitialised GPU backend
        # into a permanent hold.  In particular, when the shared GL context
        # times out, the renderer remains attached with ``is_initialized``
        # false; the CPU/cache path is the only viable producer and must stay
        # enabled for every subsequent frame, not just the first one.
        if not self._has_gpu_preview_pipeline():
            return False
        return self._should_use_gpu_only_realtime_preview()

    def should_defer_playback_tick(self, requested_frame: int | None = None) -> bool:
        """
        Return whether TimelinePanel should hold a playback timer tick.

        The renderer is intentionally single-flight, but a normal Qt timer can
        arrive one or two frames after the most recent request. Treating that
        tiny scheduling jitter as back-pressure repeatedly re-anchors the
        controller and makes otherwise realtime 25/30fps clips visibly stall.
        Hold only after the playback clock is several frames ahead of the
        active render window.

        Args:
            requested_frame: Frame produced by the playback timer for this tick.

        Returns:
            True when the panel should re-anchor the controller to the current
            visible playhead instead of emitting another render request.
        """
        if not self._is_controller_playing():
            return False
        if not self._render_in_flight:
            return False

        # 播放单飞看门狗：in_flight 超时未释放（完成信号丢失）时强制
        # 复位并放行本 tick。最坏情况损失一帧，而不是播放头永久冻结。
        # （scrub 路径的 _SCRUB_RELEASE_WATCHDOG_S 同款自愈。）
        last_dispatch = float(getattr(self, "_playback_dispatch_at", 0.0) or 0.0)
        if last_dispatch > 0.0:
            watchdog_s = float(getattr(self, "_PLAYBACK_RELEASE_WATCHDOG_S", 2.0))
            stuck_for = time.monotonic() - last_dispatch
            if stuck_for > watchdog_s:
                playback_trace(
                    "renderer.playback_release_watchdog_recover",
                    stuck_for_ms=stuck_for * 1000.0,
                )
                self._render_in_flight = False
                return False

        self._pending_mutex.lock()
        try:
            pending_frame = self._pending_frame
            queued_frame = self._queued_frame
            pending_layer_count = self._pending_layer_count
        finally:
            self._pending_mutex.unlock()

        if pending_frame is None:
            return False

        if requested_frame is None:
            return False

        try:
            direction = getattr(self._playback_controller, "playback_direction", None)
            direction_value = getattr(direction, "value", direction)
        except Exception:
            direction_value = "forward"

        step = -1 if direction_value == "backward" else 1
        requested = int(requested_frame)
        lag_frames = (requested - int(pending_frame)) * step

        # Accept a small timer/render overlap. request_render() already keeps
        # one adjacent follow-up frame, so only a sustained backlog needs a
        # controller re-anchor.
        defer_lag = int(getattr(
            self,
            "_PLAYBACK_DEFER_LAG_FRAMES",
            TimelinePreviewRenderer._PLAYBACK_DEFER_LAG_FRAMES,
        ))
        if lag_frames > defer_lag:
            playback_trace(
                "renderer.defer_tick_backlog",
                requested_frame=requested,
                pending=pending_frame,
                queued=queued_frame,
                lag_frames=lag_frames,
                layers=pending_layer_count,
            )
            return True

        return False

    def _coalesce_playback_frame(self, requested_frame: int, pending_frame: int | None) -> int:
        """
        Decide which frame to queue while a render is in flight.

        Playback must be frame-accurate and renderer-paced.  While a frame is
        in flight, queue only the adjacent frame in the current playback
        direction.  Jumping straight to the timer's far-ahead wall-clock frame
        makes slow sources alternate between stale drops and large seeks, which
        is the visible "previous/next frame shaking" users reported.
        """
        if pending_frame is None or not self._is_controller_playing():
            return requested_frame

        try:
            direction = getattr(self._playback_controller, "playback_direction", None)
            direction_value = getattr(direction, "value", direction)
        except Exception:
            direction_value = "forward"

        step = -1 if direction_value == "backward" else 1
        return int(pending_frame) + step

    def set_playback_controller(self, controller: Any) -> None:
        """
        注入播放控制器引用（由 file_controller 调用）。

        用于判断播放状态、方向，以及配合 panel 的回锚背压。

        Args:
            controller: EditPlaybackController 实例
        """
        old_signal = getattr(self, "_playback_activity_signal", None)
        if old_signal is not None:
            try:
                old_signal.disconnect(self._on_engine_playback_active_changed)
            except (TypeError, RuntimeError):
                pass
        self._playback_activity_signal = None
        # P1-10: 自适应质量档位信号同样走 engine 单一桥接，重绑定时断开。
        # __dict__.get：shell/未初始化 QObject 实例上 getattr 回落 Qt 元对象
        # 会抛 "super-class __init__() never called"。
        old_quality_signal = self.__dict__.get("_quality_pressure_signal")
        if old_quality_signal is not None:
            try:
                old_quality_signal.disconnect(self._on_engine_quality_pressure_changed)
            except (TypeError, RuntimeError):
                pass
        self._quality_pressure_signal = None
        self._playback_controller = controller

        # The engine is the sole bridge from EditPlaybackController to the
        # renderer.  Connecting before bind is deliberate: bind immediately
        # synchronizes a controller that may already be playing.
        engine = getattr(self, "_realtime_engine", None)
        active_signal = getattr(engine, "playback_active_changed", None)
        if active_signal is not None and hasattr(active_signal, "connect"):
            try:
                active_signal.connect(self._on_engine_playback_active_changed)
                self._playback_activity_signal = active_signal
            except (TypeError, RuntimeError):
                logger.debug("TimelinePreviewRenderer playback bridge connect failed", exc_info=True)
        quality_signal = getattr(engine, "quality_pressure_changed", None)
        if quality_signal is not None and hasattr(quality_signal, "connect"):
            try:
                quality_signal.connect(self._on_engine_quality_pressure_changed)
                self._quality_pressure_signal = quality_signal
            except (TypeError, RuntimeError):
                logger.debug("TimelinePreviewRenderer quality bridge connect failed", exc_info=True)
        bind_controller = getattr(engine, "bind_playback_controller", None)
        if callable(bind_controller):
            bind_controller(controller)
        logger.debug("TimelinePreviewRenderer: playback controller attached")

    def _on_engine_playback_active_changed(self, is_playing: bool) -> None:
        """Apply renderer side effects for the engine-owned transport state."""
        self.notify_playback_state(bool(is_playing), _sync_engine=False)

    # ================================================================
    # Batch 3: RealtimePlaybackEngine 唯一热路径入口
    # ================================================================

    @property
    def realtime_engine(self) -> Any:
        """暴露 RealtimePlaybackEngine（供 telemetry / UI 状态查询）。"""
        return self._realtime_engine

    @property
    def source_resolver(self) -> Any:
        """Final-1: 暴露 SourceResolver（供 proxy manager / cache adapter 注入）。"""
        return getattr(self, "_source_resolver", None)

    @property
    def gpu_compositor(self) -> Any:
        """Final-3: 暴露 TimelineGpuCompositor（唯一 GPU 合成入口）。"""
        return getattr(self, "_gpu_compositor", None)

    @property
    def gpu_frame_pool(self) -> Any:
        """Final-3: 暴露 GpuFramePool（GPU-resident frame 生命周期管理）。"""
        return getattr(self, "_gpu_frame_pool", None)

    def gpu_pipeline_stats(self) -> Dict[str, Any]:
        """Final-3: 聚合 compositor + frame pool 的统计（供 telemetry / UI）。"""
        stats: Dict[str, Any] = {}
        compositor = getattr(self, "_gpu_compositor", None)
        pool = getattr(self, "_gpu_frame_pool", None)
        if compositor is not None:
            try:
                stats["compositor"] = compositor.stats()
            except Exception:
                pass
        if pool is not None:
            try:
                stats["frame_pool"] = pool.stats()
            except Exception:
                pass
        return stats

    def _sync_gpu_pipeline_generation(self) -> None:
        """同步 GPU 资源池与 realtime scheduler 的 renderer generation。

        在 set_sequence / set_project 等 generation bump 时调用，
        让 frame pool 释放旧 generation 的 resident frame（避免 seek 后显示旧纹理）。
        compositor、scheduler 和 frame pool 必须同时切代，避免序列/项目
        更新后消费旧任务或错误素材帧。
        """
        compositor = getattr(self, "_gpu_compositor", None)
        if compositor is not None:
            try:
                compositor.reset_generation(self._generation_id)
            except Exception as e:
                logger.debug(f"compositor.reset_generation failed: {e}")
        engine = getattr(self, "_realtime_engine", None)
        if engine is not None:
            try:
                engine.sync_generation_id(self._generation_id)
            except Exception as e:
                logger.debug(f"engine generation sync failed: {e}")
        pool = getattr(self, "_gpu_frame_pool", None)
        if pool is not None:
            try:
                pool.release_generation(self._generation_id - 1)
            except Exception as e:
                logger.debug(f"pool.release_generation failed: {e}")

    def set_proxy_manager_for_source_resolver(self, proxy_manager: Any) -> None:
        """
        Final-1 (FINAL-P0-03): 把 AssetProxyManager 注入 SourceResolver。

        SourceResolver 需要 proxy_manager 才能查询每帧的 proxy ready 状态，
        并在 proxy 缺失时后台触发 ensure_proxy_async。file_controller 在
        timeline 初始化后调用一次。
        """
        resolver = getattr(self, "_source_resolver", None)
        if resolver is None:
            return
        try:
            resolver.configure(proxy_manager=proxy_manager)
            logger.debug(
                "TimelinePreviewRenderer: SourceResolver proxy_manager injected"
            )
        except Exception as e:
            logger.debug(f"SourceResolver proxy injection failed: {e}")

    def _build_layer_source_queries(self, timeline_frame: int) -> list:
        """
        Final-1 (FINAL-P0-03): 为 engine 构造当前帧的 LayerSourceQuery 列表。

        从 render plan 提取所有可见 clip layer，把 clip_id / asset_id /
        source_path / source_frame 等信息填入 LayerSourceQuery。engine 调
        SourceResolver.resolve_layers() 后得到 SourceDecision，写入 telemetry
        并驱动后台 proxy 生成（如有缺失）。

        这里只构造查询，不解码、不阻塞。
        """
        from src.features.edit.core.realtime_playback.source_resolver import (
            LayerSourceQuery,
        )

        try:
            plan = self._compute_frame_plan(timeline_frame)
        except Exception as e:
            logger.debug(f"_build_layer_source_queries plan failed: {e}")
            return []

        layers = getattr(plan, "layers", ()) or ()
        queries: list = []
        for layer in layers:
            clip = getattr(layer, "clip", None)
            if clip is None:
                continue
            # composite-bound / comp_clip layers 走 render_cache / CPU 桥接，
            # 不参与 realtime source policy（GPU 路径会回退 CPU）。
            composite_id = getattr(layer, "composite_id", None)
            asset_id = getattr(layer, "asset_id", "") or ""
            if not asset_id:
                continue
            asset_type = self._asset_resolver.get_asset_type(asset_id)
            if composite_id or asset_type == "comp_clip":
                # comp clip 需要 render cache，标记 requires_effect_cache
                source_path = self._asset_resolver.get_asset_path(asset_id) or ""
                queries.append(
                    LayerSourceQuery(
                        clip_id=getattr(layer, "clip_id", "") or "",
                        asset_id=asset_id,
                        source_path=source_path,
                        source_frame=int(getattr(layer, "source_frame", 0) or 0),
                        timeline_frame=int(timeline_frame),
                        requires_effect_cache=True,
                    )
                )
                continue

            source_path = self._asset_resolver.get_asset_path(asset_id) or ""
            queries.append(
                LayerSourceQuery(
                    clip_id=getattr(layer, "clip_id", "") or "",
                    asset_id=asset_id,
                    source_path=source_path,
                    source_frame=int(getattr(layer, "source_frame", 0) or 0),
                    timeline_frame=int(timeline_frame),
                    requires_effect_cache=False,
                )
            )
        return queries

    def _realtime_scheduler_source_frames(self, timeline_frame: int) -> Dict[str, int]:
        """Return exact source-frame mapping for scheduler prefetch."""
        try:
            plan = self._compute_frame_plan(int(timeline_frame))
        except Exception:
            return {}
        mapping: Dict[str, int] = {}
        for layer in getattr(plan, "layers", ()) or ():
            asset_id = getattr(layer, "asset_id", "") or ""
            if not asset_id or getattr(layer, "composite_id", None):
                continue
            if self._asset_resolver.get_asset_type(asset_id) != "video":
                continue
            clip_id = str(getattr(layer, "clip_id", "") or "")
            if clip_id:
                mapping[clip_id] = int(getattr(layer, "source_frame", 0) or 0)
        return mapping

    def _build_realtime_scheduler_config(
        self,
        timeline_frame: int,
        decisions_by_clip: list,
    ) -> Optional[Dict[str, Any]]:
        """Build scheduler config for ordinary video layers only.

        SourceResolver decisions are authoritative here: a scheduler stream
        is created only for a realtime-ready disk proxy or render cache.  This
        prevents the prefetch worker from accidentally warming a master file
        while the engine is enforcing realtime source policy.
        """
        scheduler = getattr(self, "_realtime_decode_scheduler", None)
        worker = getattr(self, "_worker", None)
        gpu_manager = getattr(worker, "_gpu_decode_manager", None)
        if scheduler is None or gpu_manager is None:
            return None

        try:
            plan = self._compute_frame_plan(int(timeline_frame))
            layers = tuple(getattr(plan, "layers", ()) or ())
            # Keep the configuration aligned with the worker's ordinary GPU
            # plan.  If the preview prunes an occluded layer, the worker will
            # still conservatively skip consumption for that frame.
            #
            # O4 复查修复（存量缺陷）：本方法属于 TimelinePreviewRenderer，
            # 而 _prune_occluded_layers_for_preview 定义在 PreviewRenderWorker
            # 上。此前 ``self._prune_...`` 必然抛 AttributeError 并被下面的
            # except 静默吞掉——实时调度器配置从未真正构建（引擎侧拿到
            # None 后跳过重配置，解码预取调度静默降级）。改为委托 worker
            # 的共享分析，与 worker 实际渲染计划保持同一裁剪决策；
            # record=False 避免 telemetry 双计（权威记录在 worker 渲染侧）。
            prune_on_worker = getattr(
                worker, "_prune_occluded_layers_for_preview", None,
            )
            if callable(prune_on_worker):
                visible_layers = tuple(prune_on_worker(
                    layers,
                    tuple(getattr(plan, "transitions", ()) or ()),
                    int(timeline_frame),
                    record=False,
                ))
            else:
                visible_layers = layers
        except Exception:
            return None

        decision_map = {
            str(clip_id): decision
            for clip_id, decision in decisions_by_clip
            if clip_id
        }
        from src.features.edit.core.realtime_playback import LayerSpec, SourceKind
        from src.features.edit.core.realtime_playback.decode_manager_source import (
            DecodeManagerMediaSource,
        )

        timeline_id = str(
            getattr(self._sequence, "timeline_id", "")
            or getattr(self._sequence, "sequence_id", "")
            or "edit"
        )
        target_width, target_height = self._auto2k_decode_target_size()
        specs: list[LayerSpec] = []
        signature: list[tuple] = []
        for layer in visible_layers:
            asset_id = str(getattr(layer, "asset_id", "") or "")
            clip_id = str(getattr(layer, "clip_id", "") or "")
            if not asset_id or not clip_id:
                continue
            if getattr(layer, "composite_id", None):
                continue
            if self._asset_resolver.get_asset_type(asset_id) != "video":
                continue
            decision = decision_map.get(clip_id)
            if decision is None:
                return None
            source_kind = getattr(decision, "source_kind", None)
            if not isinstance(source_kind, SourceKind) or not source_kind.is_realtime_ready:
                return None
            source_path = str(getattr(decision, "source_path", "") or "")
            if not source_path:
                return None
            spec = LayerSpec(
                clip_id=clip_id,
                asset_id=asset_id,
                timeline_id=timeline_id,
                source_path=source_path,
                source_kind=source_kind,
                target_width=int(target_width),
                target_height=int(target_height),
                decoder_stream_key=(
                    str(getattr(layer, "decoder_stream_key", "") or clip_id)
                ),
            )
            specs.append(spec)
            signature.append(
                (
                    clip_id,
                    asset_id,
                    source_path,
                    source_kind.value,
                    int(target_width),
                    int(target_height),
                    str(getattr(layer, "decoder_stream_key", "") or clip_id),
                )
            )

        if not specs:
            return None

        def source_factory(key: Any) -> DecodeManagerMediaSource:
            return DecodeManagerMediaSource(gpu_manager, key)

        return {
            "signature": tuple(signature),
            "layers": specs,
            "capacity_per_stream": 24,
            "media_source_factory": source_factory,
            "source_frame_provider": self._realtime_scheduler_source_frames,
        }

    def _engine_delegate_render(
        self,
        timeline_frame: int,
        generation_id: int,
        deadline_ns: int,
        source_decisions: Optional[list] = None,
        trace_id: int = 0,
    ) -> bool:
        """
        RealtimePlaybackEngine 的渲染委托：执行实际的 request_render。

        engine 在调用前已完成 mode/source policy/generation 决策，
        source_decisions 是 SourceResolver.resolve_layers() 的结果。
        这里只负责把帧提交到渲染队列。返回 True 表示已提交（present 异步反馈）。

        source_decisions 当前由 engine 写入 telemetry，渲染委托本身仍走既有
        worker pipeline（Final-2 之后才由 TimelineDecodeScheduler 接管解码）。

        Final-2 (FINAL-P0-02): 把 source_decisions 暂存到 _pending_source_decisions，
        worker 在 _try_gpu_render 内会取出对应 layer 的 decision 并传给
        EditGpuDecodeManager.get_frame_with_policy() —— 这是 scheduler/source
        decisions 真正接管 decode 热路径的连接点（拒绝 realtime master 解码）。

        P0-01: trace_id 暂存到 _pending_frame_trace_ids，worker enqueue 时
        取出并随 task_meta 传入帧 trace，实现 request → present 全链路可关联。
        """
        # 暂存 source decisions 供 worker 取用（按 timeline_frame 索引）。
        # worker 取到后会按 clip_id 匹配 layer 的 decision。
        if source_decisions:
            try:
                self._pending_source_decisions = {
                    int(timeline_frame): list(source_decisions),
                }
            except Exception:
                pass
        # P0-01/P0-02: 暂存 trace id + deadline（有界：保留最近 8 项，
        # 未被消费的旧帧淘汰）。pending 供 enqueue 消费；in-flight 供
        # guard-drop 归因——drop 帧恰恰是最需要与 engine 对拍的帧。
        if trace_id:
            try:
                entry = (int(trace_id), int(deadline_ns or 0))
                pending_ids = self._pending_frame_trace_ids
                pending_ids[int(timeline_frame)] = entry
                while len(pending_ids) > 8:
                    oldest = next(iter(pending_ids))
                    pending_ids.pop(oldest, None)
                in_flight = self._in_flight_frame_ids
                in_flight[int(timeline_frame)] = entry
                while len(in_flight) > 16:
                    oldest = next(iter(in_flight))
                    in_flight.pop(oldest, None)
            except Exception:
                pass
        try:
            # 调用内部的渲染提交逻辑（不经过 engine 避免递归）
            self._submit_render_internal(timeline_frame)
            return True
        except Exception as e:
            logger.error(f"_engine_delegate_render failed: {e}")
            return False

    def _submit_render_internal(self, frame: int) -> None:
        """内部渲染提交（绕过 request_render 的 engine 路由，防递归）。"""
        # 直接调用原始 request_render 的核心逻辑。
        # 为避免大改 request_render，这里复用它——engine 已在外层做决策。
        # request_render 内部不会再次调用 engine（engine 只在 panel tick 入口
        # 被调用），所以无递归风险。
        # 用一个 flag 标记"来自 engine"，让 request_render 跳过 engine 路由。
        self._render_from_engine = True
        try:
            self.request_render(frame)
        finally:
            self._render_from_engine = False

    def request_present_via_engine(self, frame: int, deadline_ns: int = 0) -> bool:
        """
        Batch 3: 经 RealtimePlaybackEngine 的 present 入口（播放热路径唯一入口）。

        TimelinePanel 的播放 tick 应调用此方法（而非直接 request_render），
        让 engine 统一做 mode/source policy/generation/miss 决策。
        非播放路径（手动 seek、scrub）仍可直接 request_render。

        Returns:
            True 表示已提交渲染，False 表示 miss/hold。
        """
        return self._realtime_engine.request_present(frame, deadline_ns)

    def _pop_source_decisions_for_frame(self, frame: int) -> Dict[str, Any]:
        """
        Final-2 (FINAL-P0-02): 取出 engine 为本帧暂存的 SourceDecision 列表，
        转成 {clip_id -> SourceDecision} map 供 worker._try_gpu_render 使用。

        暂存由 _engine_delegate_render 写入（engine 调 delegate 时附带 decisions）。
        非 engine 路径（直接 request_render，如 seek/scrub）没有 decisions，
        返回空 dict —— worker 会走既有 get_frame() 行为（兼容）。

        SourceDecision.source_kind / allow_master_fallback 控制解码门禁：
        realtime PLACEHOLDER/NONE 时 worker 不会 master-decode（get_frame_with_policy
        返回 'placeholder'，让 _try_gpu_render 失败并触发 engine hold/rebuffer）。
        """
        decisions_map: Dict[str, Any] = {}
        pending = getattr(self, "_pending_source_decisions", {}) or {}
        # _engine_delegate_render 用 timeline_frame 作为 key 存储本帧 decisions。
        decisions_list = pending.pop(int(frame), None)
        if not decisions_list:
            return decisions_map
        # SourceResolver 没有把 clip_id 直接附在 SourceDecision 上（decision 来自
        # policy，clip_id 在 LayerSourceQuery 里）。这里通过 index 对齐 queries
        # 暂存不实际可行（engine 内部已经消化 queries），所以更稳健的做法是：
        # 用 SourceDecision 的 reason 字段里携带的 source_path 反查。
        # 但当前 engine 已经把 queries->decisions 配对消费，这里改用更直接的策略：
        # _engine_delegate_render 存的是 list[(clip_id, decision)] 元组列表。
        for item in decisions_list:
            if isinstance(item, tuple) and len(item) == 2:
                cid, dec = item
                decisions_map[str(cid)] = dec
            elif hasattr(item, "source_kind"):
                # 没有 clip_id 时无法精确对齐，跳过（兼容旧调用）。
                pass
        return decisions_map

    def _take_in_flight_frame_ids(self, frame: int) -> tuple:
        """取出并移除本帧的在飞 (trace_id, deadline_ns)（P0-02 归因用）。

        dict 单操作在 GIL 下原子，跨线程（worker/GL/UI）安全；缺失返回 (0, 0)。
        A0.1：取走的同时登记进 _frame_trace_id_history（有界 256）——
        GPU timer query 的迟到回填（RING_DEPTH-1 帧后）与呈现状态
        annotate 都发生在该帧的 in-flight 条目被 pop 之后，靠历史映射
        找回 trace_id。
        """
        try:
            entry = tuple(self._in_flight_frame_ids.pop(int(frame), (0, 0)))
        except (AttributeError, TypeError, ValueError):
            return (0, 0)
        trace_id = int(entry[0] or 0) if entry else 0
        if trace_id:
            history = getattr(self, "_frame_trace_id_history", None)
            if history is None:
                history = {}
                self._frame_trace_id_history = history
            history[int(frame)] = trace_id
            while len(history) > 256:
                oldest = next(iter(history))
                history.pop(oldest, None)
        return entry

    def _lookup_frame_trace_id(self, frame: int) -> int:
        """A0.1：查本帧最近的 trace_id（含已 pop 的历史帧；0 = 未知）。"""
        history = getattr(self, "_frame_trace_id_history", None)
        if not history:
            return 0
        return int(history.get(int(frame), 0) or 0)

    def _is_seeking_from_non_playing(self, frame: int) -> bool:
        """
        Final-1: 判断当前 request_render 是否是“非播放态的 seek”（scrub/手动跳转）。

        这些场景必须直接走核心 request_render 逻辑（清缓存、重置单调守卫、
        中断 in-flight 渲染），不能交给 engine 的 realtime present 通道，
        因为 engine 在 REALTIME 模式下严格禁止 master fallback，会错过 paused/
        scrub 期望的 exact master frame。seek 由 panel 的 _on_seek_requested
        触发，此时 playback controller 通常不是 playing 态。

        真正的“播放中 seek”（engine seek）由 engine.seek() 单独处理，不经过这里。
        """
        # 当 playback controller 处于非 playing 态时，任何 frame 跳变都视为 seek/scrub
        is_playing_fn = getattr(self, "_is_controller_playing", None)
        try:
            is_playing = bool(is_playing_fn()) if callable(is_playing_fn) else False
        except Exception:
            is_playing = False
        if not is_playing:
            return True
        # playing 态下大跳变（panel reanchor）也视为 seek，走核心路径
        self._pending_mutex.lock()
        last_pending = self._pending_frame
        self._pending_mutex.unlock()
        if last_pending is not None and abs(frame - last_pending) > self._SEEK_JUMP_THRESHOLD:
            return True
        return False

    def is_render_idle(self, quiet_seconds: float = 2.5) -> bool:
        """编辑会话是否空闲：最近一次播放/拖擦渲染请求已静默足够久。

        MG 空闲缓存调度在 UI 线程调用本方法，避免 Chromium 录制与用户
        正在进行的拖擦/播放争抢资源。请求时间戳由 request_render /
        request_scrub_preview 内联写入（shell 测试以非继承方式调用这些
        方法，不能依赖额外的辅助方法存在）。
        """
        return (time.monotonic() - self._last_render_request_ts) >= float(quiet_seconds)

    def notify_playback_state(
        self,
        is_playing: bool,
        *,
        _sync_engine: bool = True,
    ) -> None:
        """
        通知 renderer/engine 播放状态变化（panel play/pause 时调用）。

        Args:
            is_playing: True=进入播放，False=暂停/停止
        """
        self._engine_playback_active = bool(is_playing)
        if is_playing:
            # Any paused FULL request becomes stale as soon as playback starts.
            self._cancel_full_quality_refresh()
            self._set_decode_quality_stage("auto2k", "playback_started")
        elif getattr(self, "_scrub_active", False):
            # Scrub is interactive even though the transport is paused.
            self._cancel_full_quality_refresh()
            self._set_decode_quality_stage("auto2k", "scrub_started")
        else:
            # Keep the current frame responsive immediately; the delayed
            # callback will switch to FULL only after the pause settles.
            self._schedule_full_quality_refresh()
        # 播放中延迟交互解码池预热（VT 会话创建会阻塞播放解码流）。
        gpu_dm = getattr(self._worker, "_gpu_decode_manager", None) if self._worker else None
        if gpu_dm is not None and hasattr(gpu_dm, "set_playback_active"):
            try:
                gpu_dm.set_playback_active(bool(is_playing))
            except Exception:
                pass
        # MG 静帧回退标志：播放中未缓存 MG 层不再同步打开浏览器捕获。
        worker = self._worker
        if worker is not None and hasattr(worker, "set_playback_active"):
            try:
                worker.set_playback_active(bool(is_playing))
            except Exception:
                pass
        if not is_playing:
            # 播放结束：把交互解码池游标重新对齐到当前播放头。播放期间
            # 池克隆停在旧位置（播放不用池）、主源游标停在停止点；不对齐
            # 的话，停止后的第一次拖拽会反复选中远端游标做长 GOP 反走
            # （真实复现：播放后拖拽 fps 下降、同帧重复 ~100ms 解码）。
            # singleShot(0) 让暂停链路（audio/engine 状态切换）先落地。
            QTimer.singleShot(0, self._realign_interactive_pool_after_playback)
            # P3.1：播放结束静默后，预缓冲停止点邻域进帧缓存。
            self._schedule_idle_prebuffer()
        # FX-M04: the GPU effect policy must follow the real playback state.
        # Previously set_preview_degraded() had no production caller, so the
        # policy was permanently disabled despite its passing unit tests.
        gpu_renderer = getattr(self, "_gpu_renderer", None)
        set_preview_degraded = getattr(gpu_renderer, "set_preview_degraded", None)
        if callable(set_preview_degraded):
            set_preview_degraded(bool(is_playing))
        from src.features.edit.core.realtime_playback import PlaybackMode
        if _sync_engine:
            if is_playing:
                self._realtime_engine.set_mode(PlaybackMode.REALTIME)
            else:
                self._realtime_engine.set_mode(PlaybackMode.PAUSED)
        if is_playing:
            prewarm_playback = getattr(self, "_prewarm_current_playback_window", None)
            if callable(prewarm_playback):
                prewarm_playback()
        # When the engine already applied the controller transition, playback
        # prewarm still belongs to this renderer side-effect stage.
        # Final-4 (FINAL-P0-09): 通知下游 Viewer 当前 realtime 状态，
        # 让 ProgramViewerWidget.set_frame 在 REALTIME 下拒绝裸 tuple。
        try:
            self.playback_mode_changed.emit(bool(is_playing))
        except Exception:
            pass

    def _prewarm_current_playback_window(self) -> None:
        """Warm decode sources and near-future proxy reads when playback starts."""
        try:
            controller = self._playback_controller
            if controller is not None:
                frame = int(getattr(controller, "current_frame", self._playhead_frame))
            else:
                frame = int(self._playhead_frame)
            plan = self._compute_frame_plan(frame)
        except Exception as e:
            logger.debug(f"TimelinePreviewRenderer: playback prewarm plan failed: {e}")
            return

        self._prewarm_zero_copy_capability()
        self._prime_current_frame_prefetch(plan)
        self._warmup_gpu_decode_sources(
            plan, for_playback=True, warm_interactive_pool=False,
        )
        self._schedule_playback_prebuffer(frame, plan)

    def _advance_to_next_frame(self) -> None:
        """
        渲染完成后的追帧逻辑。

        播放中只消费 timer tick 合并出的最新 queued frame。没有 queued
        frame 时不主动请求 controller.current_frame，避免 renderer 形成
        第二套播放节拍并和 EditPlaybackController/PlaybackControllerTimer
        互相抢帧。
        """
        try:
            self._pending_mutex.lock()
            next_queued_frame = self._queued_frame
            self._queued_frame = None
            self._pending_mutex.unlock()

            if self._playback_controller is None:
                playback_trace("renderer.advance_no_controller")
                return

            state = self._playback_controller.state
            is_playing = getattr(state, "value", state) == "playing"

            if next_queued_frame is not None:
                # 暂停态 seek 后残留的 queued follow-up 已远离播放头：重新渲染
                # 它只会把画面拉回旧位置（还会抢占空闲预缓冲锚点）。播放态的
                # queued 来自 tick 合并，天然贴近播放头，不参与此判定（
                # renderer._playhead_frame 在播放中不一定随 tick 同步）。
                if not is_playing:
                    try:
                        playhead_now = int(self.get_playhead())
                    except Exception:
                        playhead_now = int(next_queued_frame)
                    if abs(int(next_queued_frame) - playhead_now) > self._SEEK_JUMP_THRESHOLD:
                        playback_trace(
                            "renderer.advance_drop_stale_queued",
                            frame=next_queued_frame,
                            playhead=playhead_now,
                        )
                        return
                playback_trace("renderer.advance_consume_queued", frame=next_queued_frame)
                self.request_render(next_queued_frame)
                return

            if is_playing:
                playback_trace("renderer.advance_playing_no_self_pace")
                return

            # BUG FIX: 非播放状态（暂停、逐帧步进）下不自步调。
            # 渲染完成后再请求 controller.current_frame 会形成无限循环：
            # advance → request(N) → render → advance → request(N) → ...
            # 因为暂停时 controller.current_frame 不变，每轮请求相同帧。
            # 外部信号链（playhead_changed → request_render）已负责驱动
            # 新的帧请求（如用户按方向键逐帧步进），不需要自步调。
            playback_trace("renderer.advance_paused_no_self_pace")
            return
        except Exception:
            playback_trace("renderer.advance_error")
            return

    def _schedule_current_frame_warmup(self) -> None:
        """Warm the current preview frame after timeline/project changes."""
        def _warmup() -> None:
            try:
                controller = self._playback_controller
                if controller is not None:
                    state = getattr(controller, "state", None)
                    if getattr(state, "value", state) == "playing":
                        return
                    frame = int(getattr(controller, "current_frame", self._playhead_frame))
                else:
                    frame = int(self._playhead_frame)
            except Exception:
                frame = 0

            playback_trace("renderer.warmup_current_frame", frame=frame)
            self.request_render(frame)

        QTimer.singleShot(0, _warmup)

    # ==================== 渲染请求 ====================

    def _invalidate_render_generation_for_scrub(self, frame: int, reason: str) -> None:
        """
        Invalidate in-flight exact renders when entering/leaving/moving scrub mode.

        A running FFmpeg seek cannot be force-killed safely, but its generation
        can be made stale and all not-yet-started work can be removed. Scrub
        preview then stays responsive because queued old random seeks are removed
        and the next exact render always represents the newest playhead frame.
        """
        self._generation_id += 1
        self._clear_offline_media_diagnostics()
        self._dispatched_generation_id = self._generation_id - 1
        self._sync_gpu_pipeline_generation()

        worker = self._worker
        if worker is not None:
            try:
                worker.set_generation_id(self._generation_id)
                clear_pending = getattr(worker, "clear_pending_requests", None)
                if callable(clear_pending):
                    clear_pending()
            except Exception as e:
                logger.debug(f"Scrub generation sync failed: {e}")

        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()

        self._render_in_flight = False
        self._scrub_exact_pending_frame = None
        self._scrub_exact_in_flight_frame = None
        self._pending_source_decisions.clear()
        self._clear_placeholder_requests_for_frame()
        self._clear_prebuffer_state()
        self._clear_stale_pending_present_frames()
        self._reset_emitted_frame_guard()
        playback_trace(
            "renderer.scrub_generation_invalidated",
            frame=frame,
            generation=self._generation_id,
            reason=reason,
        )

    def _invalidate_stale_scrub_exact_for_latest(self, frame: int) -> None:
        """Coalesce an older scrub seek without aborting the active decoder."""
        frame = int(frame)
        self._pending_mutex.lock()
        pending_frame = self._pending_frame
        queued_frame = self._queued_frame
        self._pending_mutex.unlock()
        in_flight_frame = self._scrub_exact_in_flight_frame

        stale_exact = any(
            candidate is not None and int(candidate) != frame
            for candidate in (pending_frame, queued_frame, in_flight_frame)
        )
        if not stale_exact:
            return

        # Repeated generation invalidation made every fast drag cancel its own
        # exact seek. Let the single in-flight decode finish, then immediately
        # consume only this latest pending frame.
        self._scrub_exact_pending_frame = frame
        playback_trace(
            "renderer.scrub_stale_exact_coalesced",
            frame=frame,
            pending=pending_frame,
            queued=queued_frame,
            in_flight=in_flight_frame,
            generation=self._generation_id,
        )

    def begin_scrub(self, frame: int) -> None:
        """Enter lightweight scrub mode and invalidate old exact renders."""
        self._scrub_active = True
        self._scrub_latest_frame = int(frame)
        self._scrub_start_frame = int(frame)
        self._scrub_finish_exact_frame = None
        self._scrub_exact_frame = None
        self._scrub_exact_request_token += 1
        self._scrub_exact_pending_frame = None
        self._scrub_exact_in_flight_frame = None
        self._scrub_exact_timer_pending = False
        self._scrub_settled_timer_token += 1
        self._scrub_last_dispatch_at = 0.0
        self._scrub_exact_scheduled_at = 0.0
        # 回收上一轮遗留的 present 槽：stash 的 GpuPresentFrame 若从未被
        # legacy gate 消费，其 slot（4K IOSurface ~33MB）会一直被占住，
        # present 池满后新帧全部丢弃——长时间会话内存膨胀与拖拽冻屏的
        # 共同来源之一。每次新拖拽开始时统一 ack 释放。
        self._clear_stale_pending_present_frames()
        self._scrub_last_real_frame = None
        self._scrub_last_real_frame_at = None
        self._scrub_last_input_frame = int(frame)
        self._scrub_direction = 0
        self._scrub_input_fast = False
        # None so the first request after begin can never measure a bogus
        # ~0ms interval and spuriously enter fast mode.
        self._scrub_last_input_at = None
        self._scrub_input_velocity_fps = 0.0
        self._scrub_velocity_samples = 0
        self._scrub_latest_display_frame = int(frame)
        self._scrub_last_cycle_release_at = None
        self._begin_scrub_request_latency(int(frame))
        self.notify_playback_state(False)
        self._invalidate_render_generation_for_scrub(int(frame), "begin")
        # 播放后拖拽变慢的根治点：播放把全部解码游标聚到停止点，拖拽
        # 起点离游标团远时前期全是关键帧锚定解码（实测首段解码 p90 劣化
        # 2-3 倍）。拖拽开始时同步发起"起点预热"（后台线程执行解码）：
        # sticky 预置位不被后续请求级预置位的 token 递增打断，deadline
        # 放宽到 400ms（默认 48ms 走不完远端锚定路）——此前每次新输入
        # 都会作废上一个预置位，克隆永远走不到拖拽起点，这正是
        # "播放后第一次拖拽慢、第二次快"的直接原因。
        self._prewarm_scrub_origin_cursors()
        playback_trace("renderer.scrub_started", frame=frame)

    def _prewarm_scrub_origin_cursors(self) -> None:
        """拖拽开始时把池克隆预热到拖拽起点（sticky + 宽 deadline）。"""
        if self._worker is None:
            return
        frame = int(
            getattr(self, "_scrub_latest_display_frame", None)
            or getattr(self, "_scrub_start_frame", 0)
            or 0
        )
        try:
            plan = self._compute_frame_plan(frame)
        except Exception as e:
            logger.debug(f"scrub origin prewarm plan failed at {frame}: {e}")
            return
        layers = getattr(plan, "layers", None)
        if not layers:
            return
        # P3.4 多层限制：预置位 = 每层 2 个后台解码。N 层时间线上它与首帧
        # 的 N 路精确解码波叠加（实测 4 层时 = 12 个并发 4K VT 会话），硬件
        # 解码器被打饱和后每路慢 4-5 倍、首帧 680ms。多层时只预热最上层
        # （叠层对齐时最上层即当前视觉主体），把并发压回硬件容量内。
        layer_count = len(layers)
        if layer_count > 2:
            from dataclasses import replace as _dc_replace
            try:
                plan = _dc_replace(plan, layers=layers[-1:])
            except Exception:
                pass  # 非 dataclass 场景保持原计划（预热多层的代价可接受）
            playback_trace(
                "renderer.scrub_origin_prewarm_top_layer_only",
                frame=frame,
                layers=layer_count,
            )
        target_size = self._scrub_render_decode_target_size()
        # 两个克隆分别压在起点与前向 horizon；sticky 保证它们在请求级
        # 预置位持续 token 递增下仍能走完。
        self._preposition_interactive_scrub_sources(
            plan, timeline_frame=frame, direction=0, target_size=target_size,
            deadline_ms=400.0, sticky=True,
        )
        self._preposition_interactive_scrub_sources(
            plan, timeline_frame=frame, direction=1, target_size=target_size,
            deadline_ms=400.0, sticky=True,
        )
        playback_trace("renderer.scrub_origin_prewarm", frame=frame)

    def _update_scrub_direction(self, frame: int) -> int:
        """Update and return the current drag direction for predictive prepositioning."""
        frame = int(frame)
        previous = int(getattr(self, "_scrub_last_input_frame", frame))
        if frame > previous:
            self._scrub_direction = 1
        elif frame < previous:
            self._scrub_direction = -1
        self._scrub_last_input_frame = frame
        return int(getattr(self, "_scrub_direction", 0))

    def _update_scrub_input_cadence(self, frame: int | None = None, previous_frame: int | None = None) -> None:
        """Track how fast scrub inputs arrive; toggle fast-drag mode.

        Fast mode switches scrub dispatches to coarse keyframe-anchored
        decoding so the viewer keeps moving; slow mode returns to exact
        decoding. Also smooths the input velocity (frames/sec) that drives
        the adaptive display stride.

        快拖判定不只看输入间隔（<24ms）：页面往返/主线程拥堵会把真实快拖
        的输入间隔拉到 24-40ms 死区，导致快模式永不开启——步长失效且每次
        请求都派发后台预置位解码，与显示解码争流锁，画面冻结（真实 App
        复现：快拖 870 次请求 680 次解码失败、~4ups）。因此步幅维度并行
        判定：单次输入跨越 ≥4 帧即为快拖，与间隔无关；退出快模式需同时
        满足间隔 >40ms 且步幅 ≤2（迟滞）。
        """
        now = time.perf_counter()
        last = self._scrub_last_input_at
        if last is not None:
            interval_ms = (now - last) * 1000.0
            delta_frames = (
                abs(int(frame) - int(previous_frame))
                if frame is not None and previous_frame is not None
                else None
            )
            if not self._scrub_input_fast:
                if interval_ms < 24.0:
                    self._scrub_input_fast = True
                    playback_trace("renderer.scrub_fast_cadence_on", interval_ms=round(interval_ms, 2))
                elif delta_frames is not None and delta_frames >= 4:
                    self._scrub_input_fast = True
                    playback_trace(
                        "renderer.scrub_fast_cadence_on_by_stride",
                        interval_ms=round(interval_ms, 2),
                        delta_frames=delta_frames,
                    )
            elif interval_ms > 40.0 and (delta_frames is None or delta_frames <= 2):
                self._scrub_input_fast = False
                playback_trace(
                    "renderer.scrub_fast_cadence_off",
                    interval_ms=round(interval_ms, 2),
                    delta_frames=delta_frames,
                )
            if frame is not None and previous_frame is not None:
                from src.shared.video.scrub_stride import update_velocity

                self._scrub_input_velocity_fps = update_velocity(
                    self._scrub_input_velocity_fps,
                    int(frame) - int(previous_frame),
                    now - last,
                )
                self._scrub_velocity_samples += 1
        self._scrub_last_input_at = now

    def _scrub_sustainable_display_rate(self) -> float:
        """Display rate the current codec/machine can sustain while scrubbing."""
        period = self._scrub_cycle_period_ema_s
        if period is None or period <= 0.0:
            from src.shared.video.scrub_stride import DEFAULT_DISPLAY_RATE_CAP

            return DEFAULT_DISPLAY_RATE_CAP
        return min(60.0, max(12.0, 1.0 / period))

    def _scrub_display_stride(self) -> int:
        """Power-of-two frame stride for the current drag velocity.

        Engages only after a few sustained fast inputs (~30ms of dragging)
        so a single seek-click never gets stride-quantized.
        """
        if not self._scrub_input_fast or self._scrub_velocity_samples < 3:
            return 1
        from src.shared.video.scrub_stride import display_stride

        return display_stride(
            self._scrub_input_velocity_fps,
            self._scrub_sustainable_display_rate(),
        )

    def _quantize_scrub_display_frame(self, frame: int) -> int:
        """Floor-quantize the playhead onto the stride grid (never ahead)."""
        from src.shared.video.scrub_stride import quantize_frame

        return quantize_frame(frame, self._scrub_display_stride())

    def request_scrub_preview(self, frame: int) -> None:
        """
        Show an immediate cached placeholder when available and continuously
        request exact frames at the active Program preview target.

        Priority:
        1. Exact CPU preview cache.
        2. Nearby cached frame (recent available frame).
        3. Cached/async media thumbnail placeholder.
        """
        # 内联写入（而非辅助方法）：shell 测试以非继承方式调用本方法。
        # 该时间戳同时驱动空闲预缓冲的静默判定（写入即推迟预缓冲）。
        self._last_render_request_ts = time.monotonic()
        frame = int(frame)
        if not self._scrub_active:
            self.begin_scrub(frame)
        previous_input_frame = int(getattr(self, "_scrub_last_input_frame", frame))
        scrub_direction = self._update_scrub_direction(frame)
        self._update_scrub_input_cadence(frame, previous_input_frame)
        self._scrub_latest_frame = frame
        # Adaptive stride: fast drags chase a quantized target (1x/2x/4x/8x/
        # 16x grid) so completed decodes stay displayable; slow drags decode
        # every frame exactly as before. Release/settle always land exact.
        display_frame = self._quantize_scrub_display_frame(frame)
        self._scrub_latest_display_frame = display_frame
        self._begin_scrub_request_latency(display_frame)

        self._playhead_mutex.lock()
        self._playhead_frame = frame
        self._playhead_mutex.unlock()

        self._invalidate_stale_scrub_exact_for_latest(display_frame)
        playback_trace(
            "renderer.scrub_preview_request",
            frame=frame,
            display_frame=display_frame,
            stride=self._scrub_display_stride(),
        )

        has_gpu_preview = self._has_gpu_preview_pipeline()
        cached = self._cache.get(display_frame)
        if cached is not None:
            playback_trace("renderer.scrub_preview_cache_exact", frame=display_frame)
            self.frame_ready.emit(display_frame, cached)
            if not has_gpu_preview:
                return
            playback_trace("renderer.scrub_preview_cache_exact_provisional_gpu", frame=display_frame)

        allow_placeholders = self._allow_scrub_placeholder_frames()
        if allow_placeholders:
            # A nearby still is only an interim image. Keep the radius tight
            # so a fast drag never appears to show a frame from a different
            # moment in the clip while the exact GPU request is in flight.
            nearest = self._get_nearest_cached_frame(display_frame, radius=2)
            if nearest is not None:
                playback_trace("renderer.scrub_preview_cache_nearest", frame=display_frame)
                self.frame_ready.emit(display_frame, nearest)
                self._schedule_scrub_exact_render(display_frame)
                self._schedule_scrub_settled_exact(frame)
                return

        try:
            plan = self._compute_frame_plan(display_frame)
        except Exception as e:
            logger.debug(f"Scrub preview plan failed for frame={display_frame}: {e}")
            return

        if allow_placeholders:
            self._emit_cached_placeholder_frame(
                display_frame,
                plan,
                allow_async_request=True,
                replace_pending_async=True,
                allow_generic_thumbnail=False,
            )
        if not self._scrub_input_fast:
            # Preposition a spare VT cursor toward the predicted next frame.
            # Skipped during fast drags: the prediction horizon (a few frames)
            # is meaningless when the playhead moves tens of frames per
            # cycle, and the background preposition decode contends for the
            # per-stream decode lock with the display decode.
            self._preposition_interactive_scrub_sources(
                plan,
                timeline_frame=display_frame,
                direction=scrub_direction,
                target_size=self._scrub_render_decode_target_size(),
            )
        self._schedule_scrub_exact_render(display_frame)
        self._schedule_scrub_settled_exact(frame)

    def _schedule_scrub_exact_render(self, frame: int, delay_ms: int | None = None) -> None:
        """Coalesce scrub work into one live real-frame stream."""
        if not self._scrub_active:
            return
        frame = int(frame)
        self._scrub_exact_pending_frame = frame
        now = time.monotonic()
        if self._render_in_flight:
            # 自愈 1：单飞闩锁依赖 worker→主线程的释放链（GPU submit 信号）。
            # 释放信号丢失（线程异常/事件被吞）会让 in_flight 永久卡死，
            # 之后所有输入只 defer 不派发。超过看门狗窗口仍未释放则强制
            # 复位——最坏情况损失一帧，而不是拖拽永久冻结。
            last_dispatch = float(self._scrub_last_dispatch_at or 0.0)
            if (
                last_dispatch > 0.0
                and (now - last_dispatch) > self._SCRUB_RELEASE_WATCHDOG_S
            ):
                playback_trace(
                    "renderer.scrub_release_watchdog_recover",
                    frame=frame,
                    stuck_for_ms=(now - last_dispatch) * 1000.0,
                )
                self._render_in_flight = False
                self._scrub_exact_in_flight_frame = None
            else:
                playback_trace("renderer.scrub_exact_defer_busy", frame=frame)
                return
        if self._scrub_exact_timer_pending:
            # 自愈 2：exact 派发是一次性 QTimer.singleShot 回调。回调丢失
            # （主线程拥堵/异常被 Qt 吞掉）会让 timer_pending 闩锁永久卡死，
            # 表现为"拖拽画面完全不动"（真实 App 中复现：2227 次调度、
            # 0 次 submit）。超过看门狗窗口仍无 submit 到达则重新调度。
            last_schedule = float(getattr(self, "_scrub_exact_scheduled_at", 0.0) or 0.0)
            if (
                last_schedule > 0.0
                and (now - last_schedule) > self._SCRUB_TIMER_WATCHDOG_S
            ):
                playback_trace(
                    "renderer.scrub_exact_timer_watchdog_recover",
                    frame=frame,
                    lost_for_ms=(now - last_schedule) * 1000.0,
                )
                self._scrub_exact_timer_pending = False
            else:
                return

        token = self._scrub_exact_request_token
        elapsed_ms = (now - self._scrub_last_dispatch_at) * 1000.0
        cadence_delay = max(0, int(self._scrub_live_interval_ms - elapsed_ms))
        delay = cadence_delay if delay_ms is None else max(0, int(delay_ms))
        self._scrub_exact_timer_pending = True
        self._scrub_exact_scheduled_at = now
        playback_trace(
            "renderer.scrub_exact_coalesced_schedule",
            frame=frame,
            token=token,
            delay_ms=delay,
        )
        QTimer.singleShot(
            delay,
            lambda frame=frame, token=token: self._submit_scrub_exact_render(frame, token),
        )

    def _submit_scrub_exact_render(self, frame: int, token: int) -> None:
        """Submit the newest coalesced scrub frame without waiting for mouse-up."""
        frame = int(frame)
        self._scrub_exact_timer_pending = False
        if not self._scrub_active:
            playback_trace("renderer.scrub_exact_drop_inactive", frame=frame)
            return
        if token != self._scrub_exact_request_token:
            playback_trace("renderer.scrub_exact_drop_token", frame=frame, token=token)
            return

        # The timer captures an old frame by design. Always consume the newest
        # requested position when it fires (quantized display target).
        frame = int(
            self._scrub_exact_pending_frame
            or (self._scrub_latest_display_frame
                if self._scrub_latest_display_frame is not None
                else self._scrub_latest_frame)
        )
        self._scrub_exact_pending_frame = None

        bypass_cpu_cache = self._should_bypass_scrub_cpu_cache()
        cached = None if bypass_cpu_cache else self._cache.get(frame)
        if bypass_cpu_cache:
            playback_trace("renderer.scrub_exact_cache_bypass_gpu", frame=frame)
        if cached is not None:
            playback_trace("renderer.scrub_exact_cache_hit_emit", frame=frame)
            self.frame_ready.emit(frame, cached)
            return

        if self._render_in_flight:
            self._scrub_exact_pending_frame = frame
            playback_trace("renderer.scrub_exact_defer_busy", frame=frame)
            return

        self._dispatch_scrub_exact_render(frame)

    def _schedule_scrub_settled_exact(self, frame: int) -> None:
        """After a short pause, retry the latest scrub frame with an exact budget."""
        if not self._scrub_active:
            return
        frame = int(frame)
        self._scrub_settled_timer_token += 1
        token = self._scrub_settled_timer_token
        playback_trace(
            "renderer.scrub_settled_exact_schedule",
            frame=frame,
            token=token,
            delay_ms=self._scrub_settled_delay_ms,
        )
        QTimer.singleShot(
            self._scrub_settled_delay_ms,
            lambda frame=frame, token=token: self._submit_scrub_settled_exact(frame, token),
        )

    def _submit_scrub_settled_exact(self, frame: int, token: int) -> None:
        """Submit an exact repair frame if the scrub playhead has stopped moving."""
        frame = int(frame)
        if not self._scrub_active:
            return
        if token != self._scrub_settled_timer_token:
            playback_trace("renderer.scrub_settled_exact_drop_token", frame=frame, token=token)
            return
        latest = int(self._scrub_latest_frame)
        if frame != latest:
            playback_trace(
                "renderer.scrub_settled_exact_drop_moved",
                frame=frame,
                latest=latest,
                token=token,
            )
            return
        if self._render_in_flight:
            playback_trace("renderer.scrub_settled_exact_defer_busy", frame=frame, token=token)
            QTimer.singleShot(
                16,
                lambda frame=frame, token=token: self._submit_scrub_settled_exact(frame, token),
            )
            return
        self._dispatch_scrub_exact_render(frame, settled=True)

    def _dispatch_scrub_exact_render(self, frame: int, *, settled: bool = False) -> bool:
        """Queue one exact render at the active preview target."""
        frame = int(frame)
        if not self._scrub_active:
            return False
        target = int(
            self._scrub_latest_display_frame
            if self._scrub_latest_display_frame is not None
            else self._scrub_latest_frame
        )
        # Live dispatches chase the stride-quantized display target; the
        # settled repair after the drag stops always targets the true frame.
        if frame != target and not (settled and frame == int(self._scrub_latest_frame)):
            return False

        try:
            plan = self._compute_frame_plan(frame)
        except Exception as e:
            logger.debug(f"Scrub exact plan failed for frame={frame}: {e}")
            return False

        layer_count = len(getattr(plan, "layers", ()) or ())
        decode_target_size = self._scrub_render_decode_target_size()
        scrub_request_token = int(self._scrub_request_serial)
        request_started_at = float(self._scrub_latest_request_started_at or time.perf_counter())
        started = self._scrub_request_started_by_frame
        started[(self._generation_id, frame)] = request_started_at
        # 只有成功 emit 的帧才会 pop；被丢弃的帧（stale/gpu/cpu drop、入队
        # 被拒）的条目永远不会弹出。按插入序保留最近 N 条，防止随整个
        # 编辑会话单调增长。
        while len(started) > 32:
            started.pop(next(iter(started)))
        self._publish_interactive_scrub_state(frame, active=True)
        self._warmup_gpu_decode_sources(plan, target_size=decode_target_size)
        self._prime_current_frame_prefetch(plan, target_size=decode_target_size)
        # 快照布防只在松手收尾（settled）时触发：实时拖拽流每帧都做
        # 同步 readback 会拖住渲染线程（实测拖拽 fps 下降）；settle 帧
        # 是用户要"停在那里看"的帧，回填缓存价值最大。
        if settled:
            self._arm_snapshot_for_next_gpu_frame()

        with self._scrub_release_lock:
            self._pending_mutex.lock()
            self._pending_frame = frame
            self._queued_frame = None
            self._pending_layer_count = layer_count
            self._pending_mutex.unlock()
            self._render_in_flight = True
            self._scrub_exact_in_flight_frame = frame
        self._scrub_last_dispatch_at = time.monotonic()

        if self._worker is None:
            with self._scrub_release_lock:
                self._render_in_flight = False
                self._scrub_exact_in_flight_frame = None
            return False

        # P1-01: scrub 派发经 engine 外壳入口（trace id 分配 + telemetry），
        # renderer 的单飞/token 状态机不动；engine 未就绪时退回直连。
        engine = getattr(self, "_realtime_engine", None)
        payload = {
            "plan": plan,
            "skip_cpu_fallback_on_gpu_miss": (
                self._should_skip_cpu_fallback_on_gpu_miss()
            ),
            "decode_target_size": decode_target_size,
            "scrub_request_token": scrub_request_token,
            "scrub_settled": bool(settled),
            "scrub_coarse": bool(self._scrub_input_fast) and not settled,
            "request_started_at": request_started_at,
        }
        use_engine = (
            engine is not None and hasattr(engine, "request_scrub_present")
        )
        if use_engine:
            queued = engine.request_scrub_present(frame, payload)
        else:
            queued = self._enqueue_scrub_exact_worker_task(
                frame, payload, trace_id=0,
            )
        if queued:
            self._dispatched_generation_id = self._generation_id
            playback_trace(
                "renderer.scrub_exact_worker_queued",
                frame=frame,
                generation=self._generation_id,
                layers=layer_count,
                scrub_token=scrub_request_token,
                settled=bool(settled),
            )
            return True

        with self._scrub_release_lock:
            self._pending_mutex.lock()
            if self._pending_frame == frame:
                self._pending_frame = None
                self._pending_layer_count = 0
            self._pending_mutex.unlock()
            self._render_in_flight = False
            self._scrub_exact_in_flight_frame = None
        playback_trace("renderer.scrub_exact_worker_rejected", frame=frame)
        return False

    def _engine_delegate_scrub_render(
        self, frame: int, deadline_ns: int, trace_id: int, payload: dict,
    ) -> bool:
        """RealtimePlaybackEngine 的 scrub 委托（P1-01）。

        engine 在 request_scrub_present 里分配 trace_id 后回调到这里；
        renderer 用自己的 generation/token 状态机完成实际入队。
        """
        return self._enqueue_scrub_exact_worker_task(
            int(frame), payload or {}, trace_id=int(trace_id or 0),
        )

    def _enqueue_scrub_exact_worker_task(
        self, frame: int, payload: dict, trace_id: int = 0,
    ) -> bool:
        """把 scrub 精确帧任务写入 worker 队列（P1-01 起接受 engine trace_id）。"""
        plan = payload.get("plan")
        if plan is None:
            return False
        queued = self._worker.enqueue(
            frame,
            plan,
            self._generation_id,
            skip_cpu_fallback_on_gpu_miss=bool(
                payload.get("skip_cpu_fallback_on_gpu_miss")
            ),
            decode_target_size=payload.get("decode_target_size"),
            request_kind="scrub",
            scrub_request_token=payload.get("scrub_request_token"),
            request_started_at=payload.get("request_started_at"),
            scrub_settled=bool(payload.get("scrub_settled")),
            scrub_coarse=bool(payload.get("scrub_coarse")),
            trace_id=int(trace_id or 0),
            dispatch_channel="scrub",
        )
        return queued

    def _dispatch_pending_scrub_exact_if_needed(self) -> bool:
        """Dispatch the latest deferred exact scrub frame after a render completes."""
        if not self._scrub_active or self._render_in_flight:
            return False
        frame = self._scrub_exact_pending_frame
        self._scrub_exact_pending_frame = None
        if frame is None:
            return False
        latest = int(
            self._scrub_latest_display_frame
            if self._scrub_latest_display_frame is not None
            else self._scrub_latest_frame
        )
        if int(frame) != latest:
            frame = latest
        return self._dispatch_scrub_exact_render(int(frame))

    def finish_scrub(self, frame: int) -> None:
        """Leave scrub mode and request one exact frame for the release point."""
        frame = int(frame)
        self._scrub_active = False
        self._scrub_latest_frame = frame
        # Release always lands exact: drop any stride quantization so the
        # final frame under the pointer is decoded, not the grid position.
        self._scrub_latest_display_frame = frame
        self._scrub_input_velocity_fps = 0.0
        self._scrub_velocity_samples = 0
        self._scrub_exact_frame = frame
        self._scrub_finish_exact_frame = frame
        self._scrub_exact_request_token += 1
        self._scrub_settled_timer_token += 1
        self._scrub_exact_pending_frame = None
        self._scrub_exact_in_flight_frame = None
        self._scrub_exact_timer_pending = False
        self._publish_interactive_scrub_state(frame, active=False)
        self._invalidate_render_generation_for_scrub(frame, "finish")
        playback_trace("renderer.scrub_finished_exact_request", frame=frame)
        self.request_render(frame)
        self._schedule_full_quality_refresh()

    def _is_scrub_finish_exact_request(self, frame: int) -> bool:
        """Return whether this frame is the no-placeholder exact pass after mouse-up."""
        return getattr(self, "_scrub_finish_exact_frame", None) == int(frame)

    def _clear_scrub_finish_exact_if_matches(self, frame: int) -> None:
        """Clear the no-placeholder mouse-up marker after the exact pass resolves."""
        if self._is_scrub_finish_exact_request(frame):
            self._scrub_finish_exact_frame = None

    def _connect_placeholder_provider(self, provider: Any) -> None:
        """Connect thumbnail callbacks lazily for first-frame placeholders."""
        if self._placeholder_provider_connected and self._placeholder_provider is provider:
            return
        signal = getattr(provider, "frame_thumbnail_ready", None)
        connect = getattr(signal, "connect", None)
        if not callable(connect):
            return
        try:
            connect(self._on_placeholder_thumbnail_ready)
            self._placeholder_provider_connected = True
            self._placeholder_provider = provider
        except (TypeError, RuntimeError):
            pass

    def _disconnect_placeholder_provider(self) -> None:
        """Disconnect the thumbnail placeholder callback during shutdown."""
        provider = self._placeholder_provider
        if provider is None:
            return
        signal = getattr(provider, "frame_thumbnail_ready", None)
        disconnect = getattr(signal, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect(self._on_placeholder_thumbnail_ready)
            except (TypeError, RuntimeError):
                pass
        self._placeholder_provider = None
        self._placeholder_provider_connected = False

    def _get_asset_total_frames(self, asset_id: str, fallback_frame: int) -> int:
        """Return a best-effort total frame count for thumbnail extraction."""
        total_frames = fallback_frame + 1
        try:
            asset = self._project.get_asset_by_id(asset_id)
            if asset is None:
                return max(1, total_frames)
            for value in (
                getattr(asset, "total_frames", None),
                getattr(asset, "frame_count", None),
                getattr(asset, "metadata", {}).get("total_frames", None),
                getattr(asset, "metadata", {}).get("frame_count", None),
            ):
                if value is None:
                    continue
                total_frames = max(total_frames, int(float(value)))
                break
        except Exception:
            pass
        return max(1, total_frames)

    def _request_async_placeholder_frame(
        self,
        provider: Any,
        asset_id: str,
        asset_path: str,
        source_frame: int,
        timeline_frame: int,
        *,
        replace_pending: bool = False,
    ) -> None:
        """Request an async source-frame thumbnail for the current preview frame."""
        self._connect_placeholder_provider(provider)
        key = (str(asset_path), int(source_frame))
        if replace_pending:
            stale_keys = [
                pending_key
                for pending_key in self._pending_placeholder_requests
                if pending_key != key
            ]
            for pending_key in stale_keys:
                self._pending_placeholder_requests.pop(pending_key, None)
        if key in self._pending_placeholder_requests:
            return

        self._pending_placeholder_requests[key] = (
            int(timeline_frame),
            int(self._generation_id),
            str(asset_id),
        )
        self._placeholder_accepting_frame = int(timeline_frame)
        try:
            total_frames = self._get_asset_total_frames(asset_id, source_frame)
            provider.request_frame_pixmap_async(
                asset_id,
                asset_path,
                source_frame,
                total_frames,
                target_height=256,
            )
            playback_trace(
                "renderer.placeholder_frame_request_async",
                frame=timeline_frame,
                asset_id=asset_id,
                source_frame=source_frame,
                total_frames=total_frames,
            )
        except Exception as e:
            self._pending_placeholder_requests.pop(key, None)
            logger.debug(f"Failed to request async placeholder frame: {e}")

    def _on_placeholder_thumbnail_ready(self, file_path: str, frame_index: int, pixmap: Any) -> None:
        """Emit an async thumbnail placeholder if the real frame is still pending."""
        key = (str(file_path), int(frame_index))
        pending = self._pending_placeholder_requests.pop(key, None)
        if pending is None:
            return

        frame, generation, asset_id = pending
        if generation != self._generation_id:
            return
        if self._scrub_active:
            display = int(
                self._scrub_latest_display_frame
                if self._scrub_latest_display_frame is not None
                else self._scrub_latest_frame
            )
            if frame not in (int(self._scrub_latest_frame), display):
                return
        if (
            not getattr(self, "_scrub_active", False)
            and self._is_controller_playing()
            and getattr(self, "_playback_session_emitted", False)
        ):
            return
        if pixmap is None or pixmap.isNull():
            return

        self._pending_mutex.lock()
        pending_frame = self._pending_frame
        queued_frame = self._queued_frame
        self._pending_mutex.unlock()
        if pending_frame != frame and queued_frame != frame:
            if self._render_in_flight or getattr(self, "_placeholder_accepting_frame", None) != frame:
                return

        try:
            image = pixmap.toImage()
            if image is None or image.isNull():
                return
            self._last_placeholder_key = (generation, frame)
            playback_trace(
                "renderer.placeholder_frame_emit_async",
                frame=frame,
                asset_id=asset_id,
                source_frame=frame_index,
                width=image.width(),
                height=image.height(),
            )
            self.frame_ready.emit(frame, image.copy())
        except Exception as e:
            logger.debug(f"Failed to emit async placeholder frame: {e}")

    def _clear_placeholder_requests_for_frame(self, frame: int | None = None) -> None:
        """Remove pending placeholder requests once real render state changes."""
        if frame is None:
            self._pending_placeholder_requests.clear()
            self._placeholder_accepting_frame = None
            return
        stale_keys = [
            key
            for key, (pending_frame, _, _) in self._pending_placeholder_requests.items()
            if pending_frame == frame
        ]
        for key in stale_keys:
            self._pending_placeholder_requests.pop(key, None)
        if not self._pending_placeholder_requests:
            self._placeholder_accepting_frame = None

    def _emit_cached_placeholder_frame(
        self,
        frame: int,
        plan: Any,
        *,
        allow_async_request: bool = True,
        replace_pending_async: bool = False,
        allow_generic_thumbnail: bool = True,
    ) -> None:
        """
        Emit a cached thumbnail while the real preview frame renders.

        Timeline edits invalidate the render cache and the first true frame may
        need to open FFmpeg/VT sources. A cached media-pool or timeline thumbnail
        keeps ProgramViewer from showing the empty GL surface during that warmup.
        """
        if self._should_use_gpu_only_realtime_preview():
            playback_trace("renderer.placeholder_skip_gpu_only_realtime", frame=frame)
            return
        if (
            not getattr(self, "_scrub_active", False)
            and self._is_controller_playing()
            and getattr(self, "_playback_session_emitted", False)
        ):
            return
        if not getattr(plan, "has_content", False):
            return

        placeholder_key = (self._generation_id, int(frame))
        if self._last_placeholder_key == placeholder_key:
            return

        layers = tuple(getattr(plan, "layers", ()) or ())
        if not layers:
            return

        layer = layers[-1]
        asset_id = getattr(layer, "asset_id", None)
        if not asset_id:
            return

        asset_path = self._asset_resolver.get_asset_path(asset_id)
        if not asset_path:
            return

        try:
            from src.shared.media.thumbnail_provider import ThumbnailProvider

            provider = ThumbnailProvider.instance()
            source_frame = int(getattr(layer, "source_frame", 0) or 0)
            pixmap = None

            for height in (256, 128, 80, 64, 48):
                pixmap = provider.get_cached_frame_pixmap(
                    asset_id,
                    source_frame,
                    height,
                    path=asset_path,
                )
                if pixmap is not None and not pixmap.isNull():
                    break

            if allow_generic_thumbnail and (pixmap is None or pixmap.isNull()):
                for size in (256, 128, 120, 80, 64):
                    pixmap = provider.get_cached_thumbnail(asset_path, size)
                    if pixmap is not None and not pixmap.isNull():
                        break
            elif pixmap is None or pixmap.isNull():
                playback_trace(
                    "renderer.placeholder_skip_generic_thumbnail",
                    frame=frame,
                    asset_id=asset_id,
                    source_frame=source_frame,
                )

            if pixmap is None or pixmap.isNull():
                if not allow_async_request:
                    playback_trace("renderer.placeholder_frame_skip_async_during_scrub", frame=frame)
                    return
                self._request_async_placeholder_frame(
                    provider,
                    asset_id,
                    asset_path,
                    source_frame,
                    frame,
                    replace_pending=replace_pending_async,
                )
                return

            image = pixmap.toImage()
            if image is None or image.isNull():
                return

            self._last_placeholder_key = placeholder_key
            playback_trace(
                "renderer.placeholder_frame_emit",
                frame=frame,
                asset_id=asset_id,
                source_frame=source_frame,
                width=image.width(),
                height=image.height(),
            )
            self.frame_ready.emit(frame, image.copy())
        except Exception as e:
            logger.debug(f"Failed to emit cached placeholder frame: {e}")

    def request_render(self, frame: int, request_kind: str = "") -> None:
        """
        请求渲染指定帧。

        渲染请求合并：
        - 当一个渲染正在执行时（_render_in_flight），timer 触发的新请求
          会合并为播放方向上的相邻帧，避免并发渲染和大跨度跳帧。
        - Seek（帧号大幅跳变）会中断当前渲染，立即请求新帧。
        - 播放中渲染完成后只消费已合并的 follow-up frame，不额外自步调推进。

        Final-1 (FINAL-P0-01): 当 engine 处于 PLAYING 状态时，把播放 tick
        路由到 engine.request_present()，让 engine 统一做 mode/source
        policy/generation/miss 决策。engine 内部委托回 _submit_render_internal
        → request_render（通过 _render_from_engine flag 防递归），所以原有
        seek/缓存/单飞/合并逻辑完整保留。非播放路径（seek、scrub、pause）仍
        直接走下面的核心逻辑。

        Args:
            frame: 帧号
        """
        # Keep the renderer's paused-frame guard aligned with every normal
        # preview request, including refreshes caused by an in-place edit.
        # Previously only the explicit ``set_playhead``/scrub paths updated
        # ``_playhead_frame``.  A Transform drag therefore rendered the new
        # pixels at the correct frame, but the GPU completion path could still
        # compare them with the old playhead and discard them while paused;
        # starting playback then made the same frame visible by accident.
        frame = max(0, int(frame))
        playhead_mutex = getattr(self, "_playhead_mutex", None)
        if playhead_mutex is not None:
            playhead_mutex.lock()
            self._playhead_frame = frame
            playhead_mutex.unlock()
        else:
            # Lightweight shell objects are used by the renderer hot-path
            # tests and intentionally omit Qt synchronization primitives.
            self._playhead_frame = frame

        # 内联写入（而非辅助方法）：shell 测试以非继承方式调用本方法。
        # 该时间戳同时驱动空闲预缓冲的静默判定（写入即推迟预缓冲）。
        self._last_render_request_ts = time.monotonic()
        if (
            getattr(self, "_scrub_active", False)
            and not getattr(self, "_render_from_engine", False)
        ):
            self.request_scrub_preview(frame)
            return

        # Final-1: 播放态经 engine 热路径入口（request_present_via_engine）。
        # _render_from_engine 标记来自 engine delegate 的递归调用，直接执行核心逻辑。
        if (
            getattr(self, "_engine_playback_active", False)
            and not getattr(self, "_render_from_engine", False)
            and not self._is_seeking_from_non_playing(frame)
        ):
            try:
                presented = self.request_present_via_engine(frame)
                if presented:
                    return
                # engine 返回 miss（back-pressure）：engine 内部已处理 hold/rebuffer。
                # 这里仍需清除 pending，避免下一次 request_render 被守卫挡住。
                playback_trace("renderer.request_engine_miss", frame=frame)
                return
            except Exception as e:
                logger.error(f"request_present_via_engine failed: {e}, fallback to direct render")
                # P1-01: engine 路由异常必须可见（唯一入口审计）。
                playback_trace(
                    "renderer.request_engine_fallback",
                    frame=frame,
                    error=str(e),
                )

        # 检测播放状态转换：非播放→播放时重置会话标记
        is_currently_playing = getattr(self, "_is_controller_playing", lambda: False)()
        if is_currently_playing:
            if not getattr(self, '_playback_session_started', False):
                self._playback_session_started = True
                self._playback_session_emitted = False
                # BUG FIX: 恢复播放时重置单调帧守卫。
                # 上一次播放会话可能有旧渲染结果在暂停期间被丢弃或显示，
                # 导致 _last_emitted_frame 指向旧帧。如果不重置，
                # 新会话的第一帧会被单调守卫误判或产生大幅跳跃。
                self._reset_emitted_frame_guard()
                playback_trace("renderer.playback_resume_reset_guard")
        else:
            self._playback_session_started = False

        # 检测 seek（帧号大幅跳变），重置单调帧守卫并中断当前渲染
        self._pending_mutex.lock()
        last_pending = self._pending_frame
        queued = self._queued_frame
        self._pending_mutex.unlock()
        playback_trace(
            "renderer.request",
            frame=frame,
            pending=last_pending,
            queued=queued,
            in_flight=self._render_in_flight,
            last_emitted=self._last_emitted_frame,
        )

        is_controller_playing = getattr(self, "_is_controller_playing", lambda: False)
        is_seek = (
            not is_controller_playing()
            and last_pending is not None
            and abs(frame - last_pending) > self._SEEK_JUMP_THRESHOLD
        )
        if is_seek:
            self._reset_emitted_frame_guard()
            self._render_in_flight = False  # 中断当前渲染链，允许新帧
            # 拖拽时间轴 = 反复 seek。清掉上一代/上一帧暂存的 present frame，
            # 防止它们在 _on_gpu_frame_ready 里被 pop 出来当成当前帧显示
            # （闪现旧画面 / stale frame 泄漏）。
            self._clear_stale_pending_present_frames()
            # seek 前合并出的 queued follow-up 已过期：不清掉会在新帧渲染
            # 完成后被 advance 链重新渲染回旧位置（远离播放头的僵尸请求，
            # 反复刷新请求时间戳并抢占空闲预缓冲锚点）。
            self._pending_mutex.lock()
            self._queued_frame = None
            self._pending_mutex.unlock()
            playback_trace("renderer.request_seek_reset", frame=frame, last_pending=last_pending)

        # 去重：如果已有相同帧的请求在处理中，跳过
        self._pending_mutex.lock()
        if self._pending_frame == frame:
            self._pending_mutex.unlock()
            playback_trace("renderer.request_skip_same_pending", frame=frame)
            return
        self._pending_mutex.unlock()

        # P3.1 暂停态精确缓存命中提前发射：上一帧的全分辨率 refine 可能仍在
        # 渲染（占用单飞），但缓存里已有本帧——立即显示，不等 refine。迟到
        # 的 refine 结果由 _on_gpu_frame_ready 的暂停态播放头比对丢弃，不会
        # 回闪旧画面。后续单飞守卫照常把本帧的 refine 排进队列。
        early_emitted = False
        if (
            not is_currently_playing
            and not getattr(self, "_should_use_gpu_only_realtime_preview", lambda: False)()
            and not getattr(self, "_is_scrub_finish_exact_request", lambda _f: False)(frame)
            and getattr(self, "_cache", None) is not None
            and self._cache.get(frame) is not None
            and getattr(self, "_should_emit_frame", lambda _f: True)(frame)
        ):
            early_emitted = True
            clear_placeholders = getattr(self, "_clear_placeholder_requests_for_frame", None)
            if callable(clear_placeholders):
                clear_placeholders(frame)
            mark_emitted = getattr(self, "_mark_frame_emitted", None)
            if callable(mark_emitted):
                mark_emitted(frame)
            playback_trace(
                "renderer.request_cache_hit_emit_inflight",
                frame=frame,
                snapshot=frame in getattr(self, "_snapshot_frame_keys", ()),
                in_flight=self._render_in_flight,
            )
            frame_ready_signal = getattr(self, "frame_ready", None)
            if frame_ready_signal is not None:
                frame_ready_signal.emit(frame, self._cache.get(frame))
            schedule_prebuffer = getattr(self, "_schedule_playback_prebuffer", None)
            if callable(schedule_prebuffer):
                schedule_prebuffer(frame)

        # 单飞守卫：如果有渲染正在执行，合并此次请求。
        # 这包括缓存命中路径——在异步渲染进行中时，发射缓存帧会导致
        # 画面闪烁（显示旧缓存帧后立即显示新的异步渲染帧）。
        # Seek 已在上方重置 _render_in_flight，不受此守卫影响。
        if self._render_in_flight:
            coalesce_frame = getattr(
                self,
                "_coalesce_playback_frame",
                lambda requested, pending: requested,
            )
            queued_frame = coalesce_frame(frame, last_pending)
            self._pending_mutex.lock()
            self._queued_frame = queued_frame
            self._pending_mutex.unlock()
            playback_trace(
                "renderer.request_queue_latest",
                frame=queued_frame,
                requested_frame=frame,
                pending=last_pending,
            )
            logger.debug(
                f"request_render: queued follow-up frame {queued_frame} "
                f"(render in flight, pending={last_pending})"
            )
            return

        # 检查缓存（仅在无异步渲染时执行，避免缓存帧与异步帧竞争）。
        # Realtime + GPU pipeline must not display CPU QImage cache entries:
        # that would mix old still-preview frames into the hardware playback path.
        bypass_cpu_cache = self._should_use_gpu_only_realtime_preview()
        suppress_scrub_finish_placeholder = self._is_scrub_finish_exact_request(frame)
        cached = None if (bypass_cpu_cache or suppress_scrub_finish_placeholder) else self._cache.get(frame)
        if bypass_cpu_cache:
            playback_trace("renderer.request_cache_bypass_gpu_only_realtime", frame=frame)
        if suppress_scrub_finish_placeholder:
            playback_trace("renderer.request_cache_bypass_scrub_finish_exact", frame=frame)
        if cached is not None:
            # 快照命中：缓存条目是 1/4 分辨率的 GPU 合成快照。先立即显示
            # 获得瞬间出画，随后继续走精确渲染路径提升到全分辨率。
            snapshot_hit = frame in self._snapshot_frame_keys
            # 单调帧守卫：缓存命中也需要检查
            if not early_emitted:
                if self._should_emit_frame(frame):
                    self._clear_placeholder_requests_for_frame(frame)
                    self._mark_frame_emitted(frame)
                    playback_trace(
                        "renderer.request_cache_hit_emit",
                        frame=frame,
                        snapshot=snapshot_hit,
                    )
                    self.frame_ready.emit(frame, cached)
                    self._schedule_playback_prebuffer(frame)
                    # 缓存命中也需要消费已经排队的 follow-up 帧。
                    # 使用 QTimer.singleShot 延迟到下一事件循环迭代，
                    # 避免 缓存命中→emit→_advance→request_render→缓存命中
                    # 的同步递归导致 RecursionError（#crash-fix）。
                    QTimer.singleShot(0, self._advance_to_next_frame)
                else:
                    playback_trace("renderer.request_cache_hit_drop", frame=frame, last_emitted=self._last_emitted_frame)
                    logger.debug(
                        f"request_render: dropping stale cached frame {frame} "
                        f"(last_emitted={self._last_emitted_frame})"
                    )
                    return
            if not snapshot_hit:
                return
            playback_trace("renderer.request_cache_snapshot_refine", frame=frame)

        # 暂停态单击 seek 快速路径：精确缓存 miss 时，若附近（±2 帧）有
        # 最近渲染的缓存帧，先立即显示它作为临时画面，再异步渲染精确帧。
        # 这与拖拽 scrub 的 nearest-still 策略（request_scrub_preview）一致：
        # - 播放态（bypass_cpu_cache）不参与，避免 CPU 静帧混入硬件播放路径
        # - scrub settle 精确请求不参与：松手后的精确帧必须保持拖拽画面
        #   直到解码完成，闪一个可能过期的缓存静帧是回退（见
        #   test_finish_scrub_does_not_flash_cached_placeholder）
        if (
            cached is None
            and not is_currently_playing
            and not bypass_cpu_cache
            and not suppress_scrub_finish_placeholder
        ):
            nearest = self._get_nearest_cached_frame(frame, radius=2)
            if nearest is not None:
                playback_trace("renderer.request_seek_nearest_frame_emit", frame=frame)
                self.frame_ready.emit(frame, nearest)

        # 计算渲染计划
        plan = self._compute_frame_plan(frame)
        layer_count = len(getattr(plan, "layers", ()) or ())
        playback_trace(
            "renderer.request_plan",
            frame=frame,
            has_content=getattr(plan, "has_content", None),
            layers=layer_count,
        )
        if suppress_scrub_finish_placeholder:
            playback_trace("renderer.placeholder_skip_scrub_finish_exact", frame=frame)
        else:
            # A media-pool thumbnail represents the asset, not the requested
            # source frame.  It is often the first/cover frame, so showing it
            # for a paused seek makes every left/right frame step flash that
            # unrelated still before the exact decoder result arrives.  While
            # paused, retain only source-frame-specific thumbnails (or the
            # previous real viewer frame) until the exact render completes.
            # Playback's first frame keeps the generic fallback to avoid an
            # empty monitor while a decoder session is warming up.
            self._emit_cached_placeholder_frame(
                frame,
                plan,
                allow_generic_thumbnail=is_currently_playing,
            )

        # Capture the quality at dispatch time. A later play/pause transition
        # increments generation and drops this task, so an in-flight FULL
        # request cannot be relabeled as AUTO_2K (or vice versa).
        decode_target_size = self._decode_target_size

        # Pre-warm GPU decode sources in background (non-blocking).
        # This starts av.open() + codec probe early so the worker thread
        # finds the source already cached when it processes the render task.
        self._prime_current_frame_prefetch(plan, target_size=decode_target_size)
        # 播放中不预热交互池：每条 clip 的 2 个额外 VT 会话会长期持有，
        # 多轨时挤占核显有限的硬解实例（scrub 首次使用时本就会懒加载）。
        # 暂停态保持原行为，为接下来的拖拽准备好池。
        self._warmup_gpu_decode_sources(
            plan,
            target_size=decode_target_size,
            for_playback=is_currently_playing,
            warm_interactive_pool=not is_currently_playing,
        )
        # Keep the initial playback window resident while the timeline is
        # paused. This makes pressing Play consume the same exact-frame ring
        # instead of racing a new VT session against the first render. Scrub
        # mode owns its own latest-frame path and intentionally opts out.
        if not self._scrub_active:
            self._schedule_playback_prebuffer(frame, plan, allow_idle=True)
        # 暂停态布防合成快照：本帧渲染完成后以 1/4 分辨率回填 CPU 帧缓存，
        # 供后续 seek 的"最近帧先显/精确命中"使用。
        if not is_currently_playing:
            self._arm_snapshot_for_next_gpu_frame()
            # P3.1：暂停态静默后预缓冲播放头邻域（±N 帧进缓存）。
            if not self._scrub_active:
                self._schedule_idle_prebuffer(frame)

        # 标记 pending 帧和渲染中状态
        self._pending_mutex.lock()
        self._pending_frame = frame
        self._pending_layer_count = layer_count
        if self._queued_frame == frame:
            self._queued_frame = None
        self._pending_mutex.unlock()
        self._render_in_flight = True
        # 播放单飞看门狗的时间基准：单飞闩锁依赖 worker→主线程的完成
        # 信号链释放，信号丢失（线程异常/事件被吞）会让 in_flight 永久
        # 卡死，should_defer_playback_tick 从此只 defer 不放行，播放头
        # 永久冻结（scrub 路径已有同款看门狗，播放路径此前缺失）。
        self._playback_dispatch_at = time.monotonic()

        # 加入渲染队列
        if self._worker:
            skip_cpu_fallback_on_gpu_miss = self._should_skip_cpu_fallback_on_gpu_miss()
            # Final-2: 取出 engine 为本帧暂存的 SourceDecision 列表（按 clip_id 索引）。
            decisions_map = self._pop_source_decisions_for_frame(frame)
            # P0-01/P0-02: 取出 engine 为本帧暂存的 (trace_id, deadline_ns)
            # （非 engine 路径为 (0, 0)）；取出后转移到在飞字典供 drop 归因。
            trace_id = 0
            frame_deadline_ns = 0
            try:
                entry = self._pending_frame_trace_ids.pop(int(frame), None)
                if entry:
                    trace_id = int(entry[0] or 0)
                    frame_deadline_ns = int(entry[1] or 0)
            except (AttributeError, TypeError, ValueError):
                trace_id, frame_deadline_ns = 0, 0
            # P1-01: 路由通道正字段——trace_id 非零即 engine 派发；
            # 显式 request_kind（refresh 等）优先作为通道与 request_kind 双标注。
            channel = str(request_kind or "")
            if trace_id and not channel:
                channel = "engine"
            queued = self._worker.enqueue(
                frame,
                plan,
                self._generation_id,
                skip_cpu_fallback_on_gpu_miss=skip_cpu_fallback_on_gpu_miss,
                source_decisions_by_clip=decisions_map,
                decode_target_size=decode_target_size,
                trace_id=trace_id,
                deadline_ns=frame_deadline_ns,
                request_kind=request_kind,
                dispatch_channel=channel or "direct",
            )
            if queued:
                # Record which generation was dispatched so that
                # _on_frame_rendered / _on_render_error can detect stale results.
                self._dispatched_generation_id = self._generation_id
                playback_trace(
                    "renderer.request_worker_queued",
                    frame=frame,
                    generation=self._generation_id,
                    skip_cpu_fallback=skip_cpu_fallback_on_gpu_miss,
                )
            else:
                self._pending_mutex.lock()
                if self._pending_frame == frame:
                    self._pending_frame = None
                    self._pending_layer_count = 0
                self._pending_mutex.unlock()
                self._render_in_flight = False
                self._clear_placeholder_requests_for_frame(frame)
                playback_trace(
                    "renderer.request_worker_rejected",
                    frame=frame,
                    generation=self._generation_id,
                )

    def set_playhead(self, frame: int) -> None:
        """
        设置播放头位置。

        Args:
            frame: 帧号
        """
        self._playhead_mutex.lock()
        self._playhead_frame = frame
        self._playhead_mutex.unlock()

        # 自动请求渲染
        self.request_render(frame)

    def get_playhead(self) -> int:
        """获取播放头位置"""
        self._playhead_mutex.lock()
        frame = self._playhead_frame
        self._playhead_mutex.unlock()
        return frame

    def _warmup_gpu_decode_sources(
        self,
        plan: Any,
        *,
        target_size: Optional[Tuple[int, int]] = None,
        for_playback: bool = False,
        warm_interactive_pool: bool = True,
    ) -> None:
        """
        Pre-warm GPU decode sources for all layers in a render plan.

        Starts background threads that call av.open() + codec probe for each
        asset, so the worker thread finds sources already cached when it
        processes the render task.  Non-blocking: returns immediately.

        ``for_playback`` resolves with the playback VT routing so the warmed
        session matches the one the playback hot path uses.  Playback start
        also passes ``warm_interactive_pool=False``: the scrub pool's extra
        VT sessions compete with the first frames for hardware decoder
        instances, and the pool warmups lazily on the first scrub anyway.
        """
        gpu_dm = getattr(self._worker, '_gpu_decode_manager', None) if self._worker else None
        if gpu_dm is None:
            return
        resolver = getattr(self._worker, '_asset_resolver', None)
        if resolver is None:
            return

        decode_target_size = target_size or self._decode_target_size
        for layer in getattr(plan, 'layers', ()) or ():
            asset_id = getattr(layer, 'asset_id', None)
            if not asset_id:
                continue
            # Skip composite clips (they use CPU bridge, not GPU decode)
            if getattr(layer, "composite_id", None):
                continue
            asset_type = resolver.get_asset_type(asset_id)
            if asset_type == 'comp_clip':
                continue
            asset_path = resolver.get_asset_path(asset_id)
            if asset_path:
                gpu_dm.warmup_source(
                    asset_path,
                    target_size=decode_target_size,
                    stream_key=(
                        getattr(layer, 'decoder_stream_key', None)
                        or getattr(layer, 'clip_id', None)
                    ),
                    for_playback=for_playback,
                    warm_interactive_pool=warm_interactive_pool,
                )

    def _prime_current_frame_prefetch(
        self,
        plan: Any,
        *,
        target_size: Optional[Tuple[int, int]] = None,
    ) -> None:
        """Register visible exact frames before the renderer worker starts."""
        gpu_dm = getattr(self._worker, '_gpu_decode_manager', None) if self._worker else None
        resolver = getattr(self._worker, '_asset_resolver', None) if self._worker else None
        prefetch = getattr(gpu_dm, 'prefetch_frames', None)
        if not callable(prefetch) or resolver is None:
            return

        decode_target_size = target_size or self._decode_target_size
        for layer in getattr(plan, 'layers', ()) or ():
            asset_id = getattr(layer, 'asset_id', None)
            if not asset_id or getattr(layer, 'composite_id', None):
                continue
            if resolver.get_asset_type(asset_id) == 'comp_clip':
                continue
            asset_path = resolver.get_asset_path(asset_id)
            if not asset_path:
                continue
            try:
                prefetch(
                    asset_path,
                    [int(getattr(layer, 'source_frame', 0) or 0)],
                    target_size=decode_target_size,
                    stream_key=(
                        getattr(layer, 'decoder_stream_key', None)
                        or getattr(layer, 'clip_id', None)
                    ),
                )
            except Exception as error:
                logger.debug(f"TimelinePreviewRenderer: current-frame prefetch failed: {error}")

    def _realign_interactive_pool_after_playback(self) -> None:
        """播放结束后把交互池游标对齐到播放头，为随后的拖拽预热。

        每次调用保留/移动一个空闲克隆；发两个方向请求（播放头本身 +
        前方 horizon 帧），模拟一次"从播放头开始拖拽"的预热效果。
        池不存在时会触发后台池预热（VT 会话创建在后台线程，不在暂停
        主路径上）。scrub 进行中跳过（拖拽有自己的预置位流）。
        """
        if getattr(self, "_scrub_active", False) or self._worker is None:
            return
        self._playhead_mutex.lock()
        frame = int(self._playhead_frame)
        self._playhead_mutex.unlock()
        try:
            plan = self._compute_frame_plan(frame)
        except Exception as e:
            logger.debug(f"pool realign plan failed at frame {frame}: {e}")
            return
        if not getattr(plan, "layers", None):
            return
        target_size = self._scrub_render_decode_target_size()
        self._preposition_interactive_scrub_sources(
            plan, timeline_frame=frame, direction=0, target_size=target_size,
        )
        self._preposition_interactive_scrub_sources(
            plan, timeline_frame=frame, direction=1, target_size=target_size,
        )
        playback_trace("renderer.playback_end_pool_realign", frame=frame)

    def _preposition_interactive_scrub_sources(
        self,
        plan: Any,
        *,
        timeline_frame: Optional[int] = None,
        direction: int = 0,
        target_size: Optional[Tuple[int, int]] = None,
        deadline_ms: float = 48.0,
        sticky: bool = False,
    ) -> None:
        """Move spare VT cursors toward the newest scrub source frames."""
        gpu_dm = getattr(self._worker, '_gpu_decode_manager', None) if self._worker else None
        resolver = getattr(self._worker, '_asset_resolver', None) if self._worker else None
        preposition = getattr(gpu_dm, 'preposition_interactive_frame', None)
        if not callable(preposition) or resolver is None:
            return

        decode_target_size = target_size or self._scrub_render_decode_target_size()
        preposition_frame = self._scrub_preposition_timeline_frame(
            timeline_frame,
            direction,
        )
        preposition_plan = plan
        if preposition_frame is not None and timeline_frame is not None:
            current_frame = int(timeline_frame)
            if int(preposition_frame) != current_frame:
                try:
                    candidate_plan = self._compute_frame_plan(int(preposition_frame))
                    if getattr(candidate_plan, "layers", None):
                        preposition_plan = candidate_plan
                except Exception as error:
                    logger.debug(
                        "TimelinePreviewRenderer: scrub preposition plan failed: %s",
                        error,
                    )
                    preposition_frame = current_frame

        for layer in getattr(preposition_plan, 'layers', ()) or ():
            asset_id = getattr(layer, 'asset_id', None)
            if not asset_id or getattr(layer, 'composite_id', None):
                continue
            if resolver.get_asset_type(asset_id) == 'comp_clip':
                continue
            asset_path = resolver.get_asset_path(asset_id)
            if not asset_path:
                continue
            try:
                # sticky 仅在 True 时传参：保持既有调用形态（测试断言
                # deadline_ms=48.0 的默认路径）不被新 kwarg 打扰
                preposition_kwargs = dict(
                    target_size=decode_target_size,
                    stream_key=(
                        getattr(layer, 'decoder_stream_key', None)
                        or getattr(layer, 'clip_id', None)
                    ),
                    deadline_ms=deadline_ms,
                )
                if sticky:
                    preposition_kwargs["sticky"] = True
                preposition(
                    asset_path,
                    int(getattr(layer, 'source_frame', 0) or 0),
                    **preposition_kwargs,
                )
                playback_trace(
                    "renderer.scrub_preposition",
                    frame=timeline_frame,
                    target_frame=preposition_frame,
                    direction=direction,
                    asset_id=asset_id,
                    source_frame=int(getattr(layer, 'source_frame', 0) or 0),
                )
            except Exception as error:
                logger.debug(
                    "TimelinePreviewRenderer: interactive scrub preposition failed: %s",
                    error,
                )

    def _scrub_preposition_timeline_frame(
        self,
        timeline_frame: Optional[int],
        direction: int,
    ) -> Optional[int]:
        """Return the timeline frame whose source cursor should be warmed for scrub."""
        if timeline_frame is None or int(direction) == 0:
            return int(timeline_frame) if timeline_frame is not None else None
        horizon = max(1, int(getattr(self, "_scrub_preposition_horizon_frames", 4)))
        target = int(timeline_frame) + (1 if direction > 0 else -1) * horizon
        duration = int(getattr(self._sequence, "duration_frames", 0) or 0)
        if duration > 0:
            target = max(0, min(target, duration - 1))
        return target

    def _clear_prebuffer_state(self) -> None:
        """Reset playback prebuffer bookkeeping after timeline/project changes."""
        with self._prebuffer_lock:
            self._prebuffer_active_key = None
            self._prebuffer_completed_keys.clear()
            self._preposition_scheduled_keys.clear()
            self._preposition_scan_counter = 0
        # 时间线/工程变更：作废空闲预缓冲调度与其中间状态。
        self._idle_prebuffer_armed = False
        self._idle_prebuffer_dispatching = False
        self._idle_prebuffer_targets = []
        self._idle_prebuffer_pending.clear()

    def _playback_prebuffer_step(self) -> int:
        """Return +1/-1 according to the active playback direction."""
        controller = self._playback_controller
        if controller is None:
            return 1
        try:
            direction = getattr(controller, "playback_direction", None)
            direction_value = getattr(direction, "value", direction)
            return -1 if direction_value == "backward" else 1
        except Exception:
            return 1

    def _playback_rate_multiplier(self) -> float:
        """P1-07: 当前播放速度倍数（J/K/L 变速），缺省 1.0。"""
        controller = self.__dict__.get("_playback_controller")
        if controller is None:
            return 1.0
        try:
            return float(getattr(controller, "playback_speed", 1.0) or 1.0)
        except Exception:
            return 1.0

    def _playback_prebuffer_duration(self) -> int:
        """Return a best-effort playable timeline duration."""
        controller = self._playback_controller
        if controller is not None:
            try:
                duration = int(getattr(controller, "timeline_duration", 0) or 0)
                if duration > 0:
                    return duration
            except Exception:
                pass
        try:
            return int(getattr(self._sequence, "duration_frames", 0) or 0)
        except Exception:
            return 0

    def _adaptive_playback_prefetch_depth(self) -> int:
        """Choose a bounded 2–4 frame lookahead for the current playback.

        The estimate is deliberately cheap and conservative: Topos planar
        uint16 4:2:2 is treated as roughly six bytes per luma pixel. Higher
        cadence or larger AUTO_2K frames get the smaller window, preserving
        first-frame latency and memory headroom.
        """
        configured = max(2, min(4, int(self._prebuffer_lookahead_frames)))
        width, height = self._auto2k_decode_target_size()
        frame_bytes = int(width) * int(height) * 6
        try:
            fps_value = getattr(self._sequence, "fps", 0.0)
            fps = float(fps_value.to_fps() if hasattr(fps_value, "to_fps") else fps_value)
        except (TypeError, ValueError, AttributeError):
            fps = 30.0

        if fps >= 50.0 or frame_bytes >= (24 << 20):
            pressure_depth = 2
        elif fps >= 30.0 or frame_bytes >= (11 << 20):
            pressure_depth = 3
        else:
            pressure_depth = 4
        depth = max(2, min(configured, pressure_depth))
        stats = self.__dict__.setdefault("_prebuffer_stats", {"last_depth": 0})
        stats["last_depth"] = depth
        return depth

    def playback_prefetch_diagnostics(self) -> Dict[str, int]:
        """Expose bounded prefetch/cache/drop counters for playback HUDs."""
        stats = dict(self.__dict__.get("_prebuffer_stats", {}))
        worker = self.__dict__.get("_worker")
        if worker is not None:
            try:
                stats["queue_depth"] = int(worker._queue.qsize())
            except Exception:
                stats["queue_depth"] = 0
        stats["adaptive_depth"] = self._adaptive_playback_prefetch_depth()
        return stats

    def _collect_playback_prebuffer_jobs(
        self,
        anchor_frame: int,
        step: int,
        generation_id: int,
        anchor_plan: Any | None = None,
    ) -> Dict[tuple[str, str, str], List[int]]:
        """
        Build source-frame prefetch jobs for the upcoming playback window.

        Jobs are keyed by ``(asset_path, asset_id, decoder_stream_key)`` so
        truly contiguous blade splits share one cursor, while unrelated uses
        of the same file retain independent decoder sessions.
        """
        if generation_id != self._generation_id:
            return {}

        duration = self._playback_prebuffer_duration()
        jobs: Dict[tuple[str, str, str], List[int]] = {}
        lookahead = self._adaptive_playback_prefetch_depth()

        for offset in range(0, lookahead + 1):
            timeline_frame = int(anchor_frame) + step * offset
            if timeline_frame < 0:
                break
            if duration > 0 and timeline_frame >= duration:
                break

            try:
                plan = anchor_plan if offset == 0 and anchor_plan is not None else self._compute_frame_plan(timeline_frame)
            except Exception:
                continue
            if not getattr(plan, "has_content", False):
                continue

            for layer in getattr(plan, "layers", ()) or ():
                asset_id = getattr(layer, "asset_id", None)
                if not asset_id:
                    continue
                if getattr(layer, "composite_id", None):
                    continue

                asset_type = self._asset_resolver.get_asset_type(asset_id)
                if asset_type not in ("video", "comp_clip"):
                    continue
                if asset_type == "comp_clip":
                    # Composite clips render through graph state; source-frame
                    # prefetch cannot safely represent the final image yet.
                    continue

                asset_path = self._asset_resolver.get_asset_path(asset_id)
                if not asset_path:
                    continue

                decode_stream_key = str(
                    getattr(layer, "decoder_stream_key", "")
                    or getattr(layer, "clip_id", "")
                    or ""
                )
                source_frame = int(getattr(layer, "source_frame", 0) or 0)
                jobs.setdefault(
                    (asset_path, str(asset_id), decode_stream_key),
                    [],
                ).append(source_frame)

        return jobs

    def _schedule_playback_prebuffer(
        self,
        frame: int,
        plan: Any | None = None,
        *,
        allow_idle: bool = False,
    ) -> None:
        """
        Schedule cut-boundary stream prepositioning for upcoming playback.

        The render worker decodes only the active clip stream.  When the
        playhead approaches a cut, the next clip's stream would otherwise
        cold-start (source creation + seek + decode-forward) on the display
        path, which costs several frame periods of stutter at the cut.

        This schedules a background task that advances each upcoming clip's
        OWN display stream toward its entry frame, so the boundary frame
        decodes sequentially.  No second decoder session is created for a
        clip (the former isolated-decoder design); per-stream locks in the
        decode manager serialize prepositioning against display decode of
        the same stream, and the wait is productive because the background
        decode-forward is exactly the work the display read needs.
        """
        # Throttle window scans: one scan every few presented frames keeps
        # the 16-frame lookahead covered without recomputing frame plans on
        # every single presented frame.  The first call of a session scans
        # immediately.
        self._preposition_scan_counter += 1
        if (self._preposition_scan_counter - 1) % self._preposition_scan_interval_frames:
            return
        self._submit_playback_prebuffer(frame, plan, allow_idle=allow_idle)

    def _submit_playback_prebuffer(
        self,
        frame: int,
        plan: Any | None = None,
        *,
        allow_idle: bool = False,
    ) -> None:
        """Submit the next exact-frame window for playback or idle prewarm."""
        if not allow_idle and not self._is_controller_playing():
            return
        if allow_idle and self._scrub_active:
            return
        if self._worker is None:
            return

        step = self._playback_prebuffer_step()
        generation_id = int(self._generation_id)
        key = (generation_id, int(frame), step)

        with self._prebuffer_lock:
            if self._prebuffer_active_key is not None:
                return
            if key in self._prebuffer_completed_keys:
                return
            self._prebuffer_active_key = key

        try:
            self._prebuffer_executor.submit(
                self._run_playback_prebuffer,
                int(frame),
                step,
                generation_id,
                plan,
                key,
            )
        except RuntimeError:
            self._prebuffer_stats["dropped"] += 1
            with self._prebuffer_lock:
                if self._prebuffer_active_key == key:
                    self._prebuffer_active_key = None

    def _active_playback_stream_keys(
        self,
        frame: int,
        plan: Any | None,
    ) -> set[tuple[str, str]]:
        """Return ``(asset_path, decoder_stream_key)`` active at the anchor."""
        streams: set[tuple[str, str]] = set()
        try:
            active_plan = plan if plan is not None else self._compute_frame_plan(int(frame))
        except Exception:
            return streams
        if active_plan is None or not getattr(active_plan, "has_content", False):
            return streams
        for layer in getattr(active_plan, "layers", ()) or ():
            asset_id = getattr(layer, "asset_id", None)
            if not asset_id:
                continue
            try:
                asset_path = self._asset_resolver.get_asset_path(asset_id)
            except Exception:
                continue
            if not asset_path:
                continue
            decode_stream_key = str(
                getattr(layer, "decoder_stream_key", "")
                or getattr(layer, "clip_id", "")
                or ""
            )
            streams.add((asset_path, decode_stream_key))
        return streams

    def _run_playback_prebuffer(
        self,
        frame: int,
        step: int,
        generation_id: int,
        plan: Any | None,
        key: tuple[int, int, int],
    ) -> None:
        """Background task that prepositions upcoming clip decode streams."""
        try:
            if generation_id != self._generation_id or self._worker is None:
                self._prebuffer_stats["dropped"] += 1
                return
            gpu_dm = getattr(self._worker, '_gpu_decode_manager', None)
            if gpu_dm is None or not hasattr(gpu_dm, 'preposition_stream_frame'):
                return

            jobs = self._collect_playback_prebuffer_jobs(
                frame,
                step,
                generation_id,
                anchor_plan=plan,
            )
            if not jobs:
                return

            # Streams the display path is already driving track the playhead;
            # prepositioning them would fight the live cursor for nothing.
            active_streams = self._active_playback_stream_keys(frame, plan)

            def _stale() -> bool:
                return generation_id != self._generation_id or self._worker is None

            prepositioned = 0
            for (asset_path, asset_id, decode_stream_key), source_frames in jobs.items():
                if generation_id != self._generation_id or self._worker is None:
                    return
                if not source_frames:
                    continue
                if (asset_path, decode_stream_key) in active_streams:
                    continue
                # Forward playback enters a clip at its lowest upcoming source
                # frame; backward playback at the highest one.
                entry = min(source_frames) if step >= 0 else max(source_frames)
                schedule_key = (
                    generation_id,
                    asset_path,
                    decode_stream_key,
                    int(entry),
                    step >= 0,
                )
                with self._prebuffer_lock:
                    if schedule_key in self._preposition_scheduled_keys:
                        continue
                    self._preposition_scheduled_keys.add(schedule_key)
                    if len(self._preposition_scheduled_keys) > 512:
                        self._preposition_scheduled_keys = set(
                            list(self._preposition_scheduled_keys)[-256:]
                        )
                try:
                    if gpu_dm.preposition_stream_frame(
                        asset_path,
                        int(entry),
                        target_size=self._auto2k_decode_target_size(),
                        stream_key=decode_stream_key,
                        is_stale=_stale,
                    ):
                        prepositioned += 1
                except Exception as e:
                    logger.debug(
                        f"TimelinePreviewRenderer: playback stream preposition "
                        f"failed for stream {decode_stream_key}: {e}"
                    )

            playback_trace(
                "renderer.playback_prebuffer_done",
                frame=frame,
                step=step,
                jobs=len(jobs),
                frames=prepositioned,
                generation=generation_id,
            )
            self._prebuffer_stats["submitted"] += prepositioned
        finally:
            with self._prebuffer_lock:
                if self._prebuffer_active_key == key:
                    self._prebuffer_active_key = None
                self._prebuffer_completed_keys.add(key)
                if len(self._prebuffer_completed_keys) > 128:
                    self._prebuffer_completed_keys = set(
                        list(self._prebuffer_completed_keys)[-64:]
                    )

    def _prewarm_sequence_decode_sources(self, sequence: Any) -> None:
        """
        Lightly pre-warm GPU decode sources for timeline assets.

        set_sequence() is also called for structural edits such as split and
        drag-to-new-track. Warming every clip_id after those edits can start a
        decode thread storm, especially after one source has been split into
        many timeline clips. Keep this pass asset-path based and capped; the
        current-frame render plan still warms visible clip streams precisely.

        Non-blocking: warmup_source starts background work and deduplicates
        requests that share the same source key.
        """
        gpu_dm = getattr(self._worker, '_gpu_decode_manager', None) if self._worker else None
        if gpu_dm is None:
            return
        resolver = getattr(self._worker, '_asset_resolver', None)
        if resolver is None:
            return

        max_sources = int(getattr(self, "_sequence_prewarm_source_limit", 4) or 4)
        max_sources = max(0, max_sources)
        if max_sources <= 0:
            return

        # stream key 必须与渲染热路径一致（ClipLayerInfo.decoder_stream_key，
        # 由 TimelineRenderPlan._decode_stream_key_for_clip 计算：同轨内
        # 前向连续的切片共享左邻 clip 的流，其余按 clip_id）。此前用
        # asset_path 预热，播放时 worker 按该流 key 取流，key 不匹配 →
        # 预热的 VT 会话闲置，播放启动现场重建会话（4K HEVC ~0.5s，
        # 期间 VideoToolbox 阻塞其他解码流），呈现出现启动尖峰丢帧。
        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan

        seen_keys: set[tuple[str, str]] = set()
        warmed = 0
        for track in getattr(sequence, 'video_tracks', ()) or ():
            clips = sorted(
                (c for c in getattr(track, 'clips', ()) or ()
                 if getattr(c, 'asset_id', None)),
                key=lambda c: c.timeline_start,
            )
            previous_clip = None
            shared_key = None
            for clip in clips:
                asset_id = clip.asset_id
                # Skip comp clips (they use CPU bridge, not GPU decode)
                if getattr(clip, 'composite_id', None):
                    previous_clip = None
                    shared_key = None
                    continue
                asset_type = resolver.get_asset_type(asset_id)
                if asset_type in ('comp_clip', 'audio', None):
                    previous_clip = None
                    shared_key = None
                    continue
                asset_path = resolver.get_asset_path(asset_id)
                if not asset_path:
                    previous_clip = None
                    shared_key = None
                    continue
                if (
                    previous_clip is not None
                    and shared_key is not None
                    and TimelineRenderPlan._clips_share_forward_decode_cursor(
                        previous_clip, clip)
                ):
                    stream_key = shared_key
                else:
                    stream_key = clip.clip_id
                    shared_key = stream_key
                previous_clip = clip
                warm_key = (asset_path, stream_key)
                if warm_key in seen_keys:
                    continue
                seen_keys.add(warm_key)
                gpu_dm.warmup_source(
                    asset_path,
                    target_size=self._auto2k_decode_target_size(),
                    stream_key=stream_key,
                )
                warmed += 1
                if warmed >= max_sources:
                    playback_trace(
                        "renderer.sequence_prewarm_capped",
                        warmed=warmed,
                        unique_assets=len({p for p, _k in seen_keys}),
                    )
                    return

    def prewarm_imported_asset(self, asset_id: str, metadata: Optional[dict] = None) -> None:
        """
        Warm decode capability/metadata for a newly imported media-pool asset.

        Import-time warmup must not keep a clip-specific VT session alive because
        the asset may never be placed on the timeline. Sequence warmup handles
        real per-clip sources once a timeline clip exists.
        """
        self._prewarm_zero_copy_capability()
        gpu_dm = getattr(self._worker, '_gpu_decode_manager', None) if self._worker else None
        if gpu_dm is None:
            return
        resolver = getattr(self._worker, '_asset_resolver', None)
        if resolver is None:
            return

        try:
            asset_type = resolver.get_asset_type(asset_id)
        except Exception:
            asset_type = (metadata or {}).get("type")
        if asset_type not in ("video", "movie", "clip"):
            return

        try:
            asset_path = resolver.get_asset_path(asset_id)
        except Exception:
            asset_path = (metadata or {}).get("path") or (metadata or {}).get("file_path")
        if not asset_path:
            return

        warmup_asset = getattr(gpu_dm, "warmup_asset_capability", None)
        if callable(warmup_asset):
            warmup_asset(asset_path)

    def set_sequence_resolver(self, resolver) -> None:
        """注入子序列解析器（P1-02 嵌套序列生产接线）。

        ``resolver(timeline_id) -> TimelineSequence | None``，由控制器提供
        （通常读 ``_current_timeline_sequences``）。渲染嵌套片段时按
        timeline_id 解析子序列实时求值——子序列被修改后父序列画面在下一次
        渲染即反映（无需重建父序列）。线程安全：worker 只在渲染线程读取，
        本方法在 UI 线程于 worker 启动前/空闲时调用。
        """
        self._sequence_resolver = resolver
        if self._worker is not None:
            self._worker._sequence_resolver = resolver

    def set_sequence(self, sequence: TimelineSequence) -> None:
        """
        设置时间线序列。

        Args:
            sequence: TimelineSequence 实例
        """
        old_signature = getattr(self, "_sequence_render_signature", None)
        new_signature = self._build_sequence_render_signature(sequence)
        if old_signature == new_signature:
            self._sequence = sequence
            self._viewport_size = self._fit_viewport_to_sequence_aspect(self._raw_viewport_size)
            self._sequence_render_signature = new_signature

            try:
                timeline_fps = float(sequence.fps)
                if timeline_fps > 0.0:
                    self._realtime_engine.budget.set_target_fps(timeline_fps)
            except (TypeError, ValueError):
                pass

            self._render_plan = None
            if self._worker:
                self._worker._timeline_sequence = sequence
                self._worker.set_viewport_size(self._viewport_size)
            playback_trace("renderer.set_sequence_lightweight", generation=self._generation_id)
            return

        self._sequence = sequence
        self._sequence_render_signature = new_signature
        self._viewport_size = self._fit_viewport_to_sequence_aspect(self._raw_viewport_size)

        # The render budget is a timeline property, not a global 25fps
        # default. A 24/25/29.97/30fps sequence must judge backlog against
        # its own cadence or the controller can make incorrect hold/drop
        # decisions even when source-frame mapping is correct.
        try:
            timeline_fps = float(sequence.fps)
            if timeline_fps > 0.0:
                self._realtime_engine.budget.set_target_fps(timeline_fps)
        except (TypeError, ValueError):
            pass

        # 递增 generation ID
        self._generation_id += 1
        self._clear_offline_media_diagnostics()

        # Invalidate dispatched generation so any in-flight results from the
        # previous generation are dropped by _on_frame_rendered / _on_render_error.
        self._dispatched_generation_id = self._generation_id - 1

        # Final-3: 同步 GPU compositor / frame pool generation（丢弃旧 GPU task
        # 与 resident frame，避免 set_sequence 后显示旧纹理）。
        self._sync_gpu_pipeline_generation()

        # 清理缓存
        self._invalidate_preview_frame_caches()

        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        self._clear_placeholder_requests_for_frame()
        self._clear_prebuffer_state()
        self._clear_stale_pending_present_frames()

        # 重置单调帧守卫（时间线变化后帧号体系完全改变）
        self._reset_emitted_frame_guard()

        # 重置渲染链状态（旧渲染已无效，允许新请求）
        self._render_in_flight = False

        # 更新渲染计划
        self._render_plan = None

        if self._worker:
            self._worker.set_generation_id(self._generation_id)
            self._worker._timeline_sequence = sequence
            self._worker.set_viewport_size(self._viewport_size)

        # Pre-warm GPU decode sources for ALL clips in the sequence.
        # This starts av.open() + codec probe in background threads so that
        # the first playback/seek to any clip position is fast (~70ms instead
        # of ~2-3s).  Non-blocking: each warmup runs on a DecodeWarmup thread.
        self._prewarm_sequence_decode_sources(sequence)

        self._schedule_current_frame_warmup()

    def update_sequence_silent(self, sequence: Any) -> None:
        """Hot-path in-place sequence update for interactive editing (transform drag).

        Mirrors :meth:`update_color_stacks`: swap the sequence and fully
        invalidate the CPU frame cache + in-flight render state so the next
        ``request_render`` actually re-renders with the new transform instead
        of re-emitting the stale cached frame.

        We deliberately do NOT call :meth:`set_sequence` here — that path also
        tears down prebuffer / decode-warmup state, turning every 60Hz drag
        tick into a cold start. This method only touches what a transform
        change affects: pixels of the current frame.

        Safe only for edits that change per-clip mutable state (transform /
        opacity / effects) without altering the sequence structure (track /
        clip add/remove) — structural edits must go through ``set_sequence``.
        """
        self._sequence = sequence
        self._render_plan = None
        # Invalidate the CPU frame cache — without this, request_render hits
        # the cached pre-drag frame and returns early, so the transform edit
        # is never re-rendered (the preview appears frozen).
        self._invalidate_preview_frame_caches()
        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        # Allow the very next request_render to dispatch immediately instead
        # of queueing behind a now-stale in-flight render.
        self._render_in_flight = False
        if self._worker:
            self._worker._timeline_sequence = sequence

    def _build_sequence_render_signature(self, sequence: Any) -> tuple[Any, ...]:
        """
        Return the subset of sequence state that can change video preview pixels.

        Pure structural timeline edits, such as adding an empty video track,
        should not flush the preview cache or restart decode generations.
        """
        if sequence is None:
            return ()

        def _float(value: Any, default: float = 0.0) -> float:
            try:
                return round(float(value), 6)
            except (TypeError, ValueError):
                return default

        try:
            fps = _float(getattr(sequence, "fps", 0.0))
        except Exception:
            fps = 0.0

        tracks: list[tuple[Any, ...]] = []
        for track in getattr(sequence, "video_tracks", ()) or ():
            clips = tuple(getattr(track, "clips", ()) or ())
            transitions = tuple(getattr(track, "transitions", ()) or ())
            if not clips and not transitions:
                continue

            clip_entries: list[tuple[Any, ...]] = []
            for clip in clips:
                transform = getattr(clip, "transform", None)
                crop = getattr(clip, "crop", None)
                clip_entries.append((
                    getattr(clip, "clip_id", ""),
                    getattr(clip, "asset_id", ""),
                    int(getattr(clip, "timeline_start", 0) or 0),
                    int(getattr(clip, "timeline_duration", 0) or 0),
                    int(getattr(clip, "source_start", 0) or 0),
                    int(getattr(clip, "source_duration", 0) or 0),
                    _float(getattr(clip, "opacity", 1.0), 1.0),
                    _float(getattr(clip, "speed", 1.0), 1.0),
                    bool(getattr(clip, "is_enabled", True)),
                    int(getattr(clip, "fade_in_duration", 0) or 0),
                    int(getattr(clip, "fade_out_duration", 0) or 0),
                    getattr(clip, "color_stack_id", None),
                    getattr(clip, "composite_id", None),
                    bool(getattr(clip, "composite_bypassed", False)),
                    tuple(getattr(clip, "effects", ()) or ()),
                    (
                        _float(getattr(transform, "translate_x", 0.0)),
                        _float(getattr(transform, "translate_y", 0.0)),
                        _float(getattr(transform, "scale_x", 1.0), 1.0),
                        _float(getattr(transform, "scale_y", 1.0), 1.0),
                        _float(getattr(transform, "rotation", 0.0)),
                        str(getattr(transform, "pivot_mode", "center")),
                        _float(getattr(transform, "pivot_x", 0.0)),
                        _float(getattr(transform, "pivot_y", 0.0)),
                    ),
                    (
                        _float(getattr(crop, "left", 0.0)),
                        _float(getattr(crop, "right", 0.0)),
                        _float(getattr(crop, "top", 0.0)),
                        _float(getattr(crop, "bottom", 0.0)),
                        _float(getattr(crop, "feather", 0.0)),
                    ),
                ))

            transition_entries: list[tuple[Any, ...]] = []
            for transition in transitions:
                transition_entries.append((
                    getattr(transition, "transition_id", ""),
                    getattr(transition, "transition_type", ""),
                    getattr(transition, "left_clip_id", ""),
                    getattr(transition, "right_clip_id", ""),
                    int(getattr(transition, "center_frame", 0) or 0),
                    int(getattr(transition, "duration", 0) or 0),
                    repr(getattr(transition, "params", {}) or {}),
                ))

            tracks.append((
                bool(getattr(track, "is_enabled", True)),
                bool(getattr(track, "is_visible", True)),
                bool(getattr(track, "is_solo", False)),
                tuple(clip_entries),
                tuple(transition_entries),
            ))

        return (
            int(getattr(sequence, "width", 0) or 0),
            int(getattr(sequence, "height", 0) or 0),
            fps,
            tuple(tracks),
        )

    def set_project(self, project: EditProject) -> None:
        """
        更新 EditProject 引用（热更新）。

        当媒体池导入素材后，project 会更新，需要同步更新 renderer 的引用。

        Args:
            project: 新的 EditProject 实例
        """
        self._project = project

        # 更新素材路径解析器
        self._asset_resolver = AssetResolver(project)

        # 递增 generation ID
        self._generation_id += 1
        self._clear_offline_media_diagnostics()

        # Invalidate dispatched generation so any in-flight results from the
        # previous generation are dropped by _on_frame_rendered / _on_render_error.
        self._dispatched_generation_id = self._generation_id - 1

        # Final-3: 同步 GPU compositor / frame pool generation。
        self._sync_gpu_pipeline_generation()

        # 清空缓存
        self._invalidate_preview_frame_caches()
        if self._worker:
            self._worker.invalidate_composite_cache()

        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        self._clear_placeholder_requests_for_frame()
        self._clear_prebuffer_state()
        self._clear_stale_pending_present_frames()

        # 重置单调帧守卫（项目变化后帧号可能完全改变）
        self._reset_emitted_frame_guard()

        # 重置渲染链状态
        self._render_in_flight = False

        # 更新渲染计划
        self._render_plan = None

        if self._worker:
            # 更新 worker 的 asset_resolver
            self._worker._asset_resolver = self._asset_resolver
            self._worker._timeline_sequence = self._sequence
            self._worker.set_generation_id(self._generation_id)

        self._prewarm_zero_copy_capability()
        self._prewarm_sequence_decode_sources(self._sequence)
        self._schedule_current_frame_warmup()

        logger.info(f"TimelinePreviewRenderer project updated: {project.project_id}")

    def update_project_metadata(self, project: EditProject) -> None:
        """Publish nonvisual project metadata without disturbing warm frames.

        The controller uses this only when source, timeline and color content
        are unchanged (for example a named grade version was renamed).
        """
        self._project = project
        self._asset_resolver = AssetResolver(project)
        if self._worker:
            self._worker._asset_resolver = self._asset_resolver

    def update_color_stacks(self, project: EditProject) -> None:
        """Publish ColorStack edits without tearing down warm decode state.

        Color controls can emit many updates during one drag.  They change
        pixels but never change media paths, decode capability or the timeline
        structure, so calling ``set_project`` here would unnecessarily discard
        prebuffer state and make the next frame look like a cold start.
        """
        # The project-change broadcast and the Color UI host may both publish
        # this immutable snapshot. One generation invalidation is sufficient.
        if self._project is project:
            return
        self._project = project
        self._asset_resolver = AssetResolver(project)
        self._generation_id += 1
        self._clear_offline_media_diagnostics()
        self._dispatched_generation_id = self._generation_id - 1
        self._sync_gpu_pipeline_generation()
        self._invalidate_preview_frame_caches()
        self._pending_mutex.lock()
        self._pending_frame = None
        self._queued_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        self._clear_placeholder_requests_for_frame()
        self._clear_stale_pending_present_frames()
        self._reset_emitted_frame_guard()
        # Old work now belongs to the previous grade generation.  Do not let
        # it keep the current-color request behind an in-flight gate.
        self._render_in_flight = False

        if self._worker:
            self._worker._asset_resolver = self._asset_resolver
            self._worker._timeline_sequence = self._sequence
            self._worker.set_generation_id(self._generation_id)

        logger.debug("TimelinePreviewRenderer color stacks updated")

    def set_viewport_size(self, size: Tuple[int, int]) -> None:
        """
        设置视口尺寸。

        Args:
            size: (width, height)
        """
        self._raw_viewport_size = size
        self._viewport_size = self._fit_viewport_to_sequence_aspect(size)

        if self._worker:
            self._worker.set_viewport_size(self._viewport_size)

    def invalidate_cache(self, start_frame: int, end_frame: int) -> None:
        """
        使帧范围缓存失效。

        当时间线编辑导致帧内容变化时调用。

        Args:
            start_frame: 开始帧号
            end_frame: 结束帧号
        """
        self._cache.invalidate(start_frame, end_frame)
        self._clear_prebuffer_state()

    def clear_cache(self) -> None:
        """清空缓存"""
        self._invalidate_preview_frame_caches()
        self._clear_prebuffer_state()

    def invalidate_composite_cache(self, composite_id: str | None = None) -> None:
        """Drop preview frames produced by a changed Composite graph."""
        self._invalidate_preview_frame_caches()
        if self._worker:
            self._worker.invalidate_composite_cache(composite_id)

    def prefetch(self, start_frame: int, end_frame: int) -> None:
        """
        预取帧范围。

        Args:
            start_frame: 开始帧号
            end_frame: 结束帧号
        """
        # 逐帧请求渲染（不阻塞）
        for frame in range(start_frame, end_frame + 1):
            self.request_render(frame)

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        return self._cache.get_stats()

    # ================================================================
    # 内部方法
    # ================================================================

    def _compute_frame_plan(self, frame: int) -> Any:  # FrameRenderPlan
        """
        计算帧渲染计划。

        Args:
            frame: 帧号

        Returns:
            FrameRenderPlan
        """
        # 使用现有的 TimelineRenderPlan 计算
        from src.features.edit.core.timeline_render_plan import TimelineRenderPlan

        self._render_plan_mutex.lock()
        try:
            # 如果没有渲染计划实例，创建一个
            if self._render_plan is None:
                self._render_plan = TimelineRenderPlan(self._sequence)

            # 获取帧计划
            return self._render_plan.get_frame_plan(frame)
        finally:
            self._render_plan_mutex.unlock()

    def _frame_has_content(self, frame: int) -> bool:
        """Return whether the current render plan contains visible content."""
        try:
            plan = self._compute_frame_plan(frame)
            return bool(getattr(plan, "has_content", False))
        except Exception:
            return False

    def _make_no_content_frame(self) -> QImage:
        """Create an explicit black frame for an intentional timeline gap.

        A ``None`` render result is also used for transient decode misses, so
        the viewer historically kept its previous texture.  That is correct
        for a failed render but wrong when the render plan genuinely contains
        no clip: a gap between two cuts must not hold the preceding frame.
        """
        width, height = self._decode_target_size
        image = QImage(max(1, int(width)), max(1, int(height)), QImage.Format.Format_RGB888)
        image.fill(0)
        return image

    def _get_nearest_cached_frame(self, frame: int, radius: int = 6) -> Any:
        """Find a nearby cached CPU preview frame for transient decode misses."""
        cached = self._cache.get(frame)
        if cached is not None:
            return cached

        for offset in range(1, max(1, radius) + 1):
            previous = self._cache.get(frame - offset)
            if previous is not None:
                return previous
            following = self._cache.get(frame + offset)
            if following is not None:
                return following
        return None

    def _drop_scrub_render_result(self, frame: int, reason: str) -> None:
        """Drop an old scrub render and unblock the latest exact scrub request."""
        frame = int(frame)
        self._clear_placeholder_requests_for_frame(frame)
        self._pending_mutex.lock()
        pending_matches = self._pending_frame == frame
        if pending_matches:
            self._pending_frame = None
            self._pending_layer_count = 0
        self._pending_mutex.unlock()

        in_flight_matches = self._scrub_exact_in_flight_frame == frame
        if pending_matches or in_flight_matches:
            self._render_in_flight = False
        if in_flight_matches:
            self._scrub_exact_in_flight_frame = None

        playback_trace(
            "renderer.scrub_result_drop",
            frame=frame,
            reason=reason,
            latest=self._scrub_latest_frame,
        )
        self._dispatch_pending_scrub_exact_if_needed()

    def _release_scrub_gpu_submitted(
        self,
        frame: int,
        generation: int,
        *,
        dispatch_pending: bool,
        trace_stage: str,
    ) -> bool:
        """Release single-flight state after the frame is accepted by GPU.

        scrub：交互拖拽的 exact 单飞在 GL 入队时放行，让解码追最新帧。
        非 scrub（播放/暂停步进）：GL 任务入队即放行单飞准入，使下一帧
        解码波与本帧 GL 合成重叠（流水线化）。两种情况都只在完成帧
        与当前 pending 匹配时才释放，避免误放更新的在飞请求。
        """
        frame = int(frame)
        generation = int(generation)
        if generation != self._generation_id:
            return False

        with self._scrub_release_lock:
            self._pending_mutex.lock()
            pending_matches = self._pending_frame == frame
            if pending_matches:
                self._pending_frame = None
                self._pending_layer_count = 0
            self._pending_mutex.unlock()

            if self._scrub_active:
                in_flight_matches = self._scrub_exact_in_flight_frame == frame
                released = pending_matches or in_flight_matches
                if in_flight_matches:
                    self._scrub_exact_in_flight_frame = None
            else:
                released = pending_matches
            if released:
                self._render_in_flight = False
                # Track the completed decode-cycle period: its inverse is the
                # display rate the pipeline sustains, which drives the
                # adaptive scrub stride (slow codecs step harder).
                now = time.perf_counter()
                last_release = self._scrub_last_cycle_release_at
                if last_release is not None:
                    cycle_s = now - last_release
                    if 0.0 < cycle_s < 2.0:
                        previous = self._scrub_cycle_period_ema_s
                        self._scrub_cycle_period_ema_s = (
                            cycle_s if previous is None else previous * 0.7 + cycle_s * 0.3
                        )
                self._scrub_last_cycle_release_at = now

        if not self._scrub_active:
            trace_stage = "renderer.gpu_submitted_playback_release"
        playback_trace(
            trace_stage,
            frame=frame,
            generation=generation,
            latest=self._scrub_latest_frame,
            released=released,
            dispatch_pending=dispatch_pending,
        )
        if dispatch_pending:
            self._dispatch_pending_scrub_exact_if_needed()
        return released

    def _on_worker_gpu_submitted_direct(self, frame: int, generation: int) -> None:
        """Release worker pressure immediately without touching UI-thread dispatch."""
        self._release_scrub_gpu_submitted(
            frame,
            generation,
            dispatch_pending=False,
            trace_stage="renderer.scrub_gpu_submitted_direct_release",
        )

    def _on_worker_gpu_submitted(self, frame: int, generation: int) -> None:
        """Let scrub decode chase the latest frame once the current frame is queued on GPU."""
        self._release_scrub_gpu_submitted(
            frame,
            generation,
            dispatch_pending=True,
            trace_stage="renderer.scrub_gpu_submitted_release",
        )

    def _can_emit_scrub_render_result(self, frame: int) -> bool:
        """Return whether a completed real scrub frame is useful to display.

        Continuous-feedback policy (DaVinci-style scrub): prefer showing a
        completed decode over freezing the viewer. The direction window
        prevents bouncing back to positions the playhead already left, but
        when nothing has been displayed for a while (fast drag, coarse
        keyframe results trailing the playhead) any fresh completion is
        better than a still image.
        """
        if not self._scrub_active:
            return True

        frame = int(frame)
        latest = int(self._scrub_latest_frame)
        if frame == latest:
            return True
        display = int(
            self._scrub_latest_display_frame
            if self._scrub_latest_display_frame is not None
            else latest
        )
        if frame == display:
            return True

        # Never-freeze escape hatch: coarse/fast drags legitimately complete
        # frames outside the window (keyframe-anchored results trail the
        # playhead by up to a GOP). A still viewer reads as "stuck"; show it.
        last_shown_at = getattr(self, "_scrub_last_real_frame_at", None)
        if last_shown_at is None or (time.perf_counter() - last_shown_at) > 0.15:
            return True

        previous = self._scrub_last_real_frame
        if previous is None:
            start = int(getattr(self, "_scrub_start_frame", latest))
            if latest >= start:
                return start <= frame <= latest
            return latest <= frame <= start

        # Only let provisional real frames move toward the current playhead.
        # That retains motion during a drag without letting an async completion
        # bounce the viewer back in the opposite direction.
        if latest > previous:
            return previous <= frame <= latest
        if latest < previous:
            return latest <= frame <= previous
        if abs(frame - latest) > self._SCRUB_RESULT_MAX_LAG_FRAMES:
            return False
        return frame == latest

    def _on_frame_rendered(self, frame: int, result: Any) -> None:
        """
        帧渲染完成回调。

        Drops results from stale generations (e.g. after set_sequence / set_project
        was called while the worker was still processing an older request).

        Args:
            frame: 帧号
            result: 渲染结果（QImage 或 texture_id）
        """
        if isinstance(result, dict) and result.get("scrub_stale_drop"):
            self._drop_scrub_render_result(
                frame,
                str(result.get("reason") or "worker_scrub_stale"),
            )
            return

        scrub_exact_frame = getattr(self, "_scrub_exact_frame", None)
        if scrub_exact_frame is not None and int(frame) != int(scrub_exact_frame):
            self._drop_scrub_render_result(frame, "finish_non_final")
            return

        if self._scrub_active and not self._can_emit_scrub_render_result(frame):
            self._drop_scrub_render_result(frame, "active_too_far_behind")
            return

        # Generation gate: the worker already checked generation before emitting,
        # but set_sequence() / set_project() may have bumped the generation on the
        # main thread while the signal was in the event queue.  Discard stale results
        # to avoid flashing back to old frames or showing a no-clip screen after a
        # rapid clip add/replace.
        if self._generation_id != self._dispatched_generation_id:
            playback_trace(
                "renderer.cpu_result_drop_generation",
                frame=frame,
                current_generation=self._generation_id,
                dispatched_generation=self._dispatched_generation_id,
            )
            logger.debug(
                f"Dropping stale frame {frame} result (gen mismatch: "
                f"current={self._generation_id}, dispatched={self._dispatched_generation_id})"
            )
            # 重置渲染链状态并尝试推进到下一帧
            self._render_in_flight = False
            self._pending_mutex.lock()
            self._pending_frame = None
            self._pending_mutex.unlock()
            self._clear_placeholder_requests_for_frame(frame)
            if self._scrub_exact_in_flight_frame == frame:
                self._scrub_exact_in_flight_frame = None
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        self._clear_placeholder_requests_for_frame(frame)

        has_content = self._frame_has_content(frame) if result is None else True
        if result is None and not has_content:
            # Empty timeline ranges are a valid render result, not a decode
            # failure.  Emit black so ProgramViewer replaces the previous
            # texture instead of leaving a frozen still over the gap.
            result = self._make_no_content_frame()
            playback_trace(
                "renderer.cpu_result_no_content_black_frame",
                frame=frame,
                width=result.width(),
                height=result.height(),
            )

        if result is None and has_content:
            bypass_none_fallback_cache = (
                (
                    self._scrub_active
                    and self._should_bypass_scrub_cpu_cache()
                )
                or self._is_scrub_finish_exact_request(frame)
            )
            fallback = None if bypass_none_fallback_cache else self._get_nearest_cached_frame(frame)
            if bypass_none_fallback_cache:
                playback_trace("renderer.cpu_result_none_skip_cache_fallback_gpu_scrub", frame=frame)
            if fallback is not None:
                playback_trace("renderer.cpu_result_none_use_cached_fallback", frame=frame)
                result = fallback
            else:
                playback_trace("renderer.cpu_result_none_preserve_previous", frame=frame)
                self._pending_mutex.lock()
                self._pending_frame = None
                self._pending_layer_count = 0
                self._pending_mutex.unlock()
                self._render_in_flight = False
                if self._scrub_exact_in_flight_frame == frame:
                    self._scrub_exact_in_flight_frame = None
                if self._dispatch_pending_scrub_exact_if_needed():
                    return
                self._advance_to_next_frame()
                return

        # 缓存结果（即使最终不发射也要缓存，后续 seek 回来可能命中）
        if result is not None:
            self._cache.put(frame, result)

        # 清除 pending 标记和渲染中标记（流水线化：仅当本帧仍是当前
        # pending 或没有更新请求接管时才释放，避免误清下一帧在飞状态）
        self._pending_mutex.lock()
        queued = self._queued_frame
        cpu_pending_matches = self._pending_frame in (None, frame)
        if cpu_pending_matches:
            self._pending_frame = None
        self._pending_mutex.unlock()
        if cpu_pending_matches:
            self._render_in_flight = False
        if self._scrub_exact_in_flight_frame == frame:
            self._scrub_exact_in_flight_frame = None
        playback_trace(
            "renderer.cpu_result_ready",
            frame=frame,
            queued=queued,
            result_type=type(result).__name__,
        )
        if self._scrub_active:
            self._trace_scrub_stage_latency(
                frame,
                "renderer.scrub_cpu_ready_latency",
                result_type=type(result).__name__,
            )

        # 单调帧守卫：丢弃比已显示帧更旧的乱序渲染结果，防止画面抖动
        if self._is_playback_result_stale(frame):
            playback_trace(
                "renderer.cpu_result_drop_playback_stale",
                frame=frame,
                controller_frame=getattr(self._playback_controller, "current_frame", None),
            )
            logger.debug(
                f"Dropping stale playback frame {frame} "
                f"(controller_current={self._playback_controller.current_frame})"
            )
            self._pending_layer_count = 0
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        # 单调帧守卫：丢弃比已显示帧更旧的乱序渲染结果，防止画面抖动
        if not self._should_emit_frame(frame):
            playback_trace(
                "renderer.cpu_result_drop_monotonic",
                frame=frame,
                last_emitted=self._last_emitted_frame,
            )
            logger.debug(
                f"Dropping stale frame {frame} (last_emitted={self._last_emitted_frame}): "
                f"async render arrived out of order"
            )
            # BUG FIX: 推进 _last_emitted_frame 防止 _advance_to_next_frame 重入死循环
            self._advance_last_emitted_to_dropped_frame(frame)
            # 即使丢弃也要推进渲染链，否则会卡死
            self._pending_layer_count = 0
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        self._mark_frame_emitted(frame)
        if self._scrub_active:
            self._scrub_last_real_frame = int(frame)
            self._scrub_last_real_frame_at = time.perf_counter()
        if scrub_exact_frame is not None and int(frame) == int(scrub_exact_frame):
            self._scrub_exact_frame = None
        self._clear_scrub_finish_exact_if_matches(frame)

        # 发射信号
        playback_trace("renderer.cpu_frame_ready_emit", frame=frame)
        if self._scrub_active:
            self._trace_scrub_frame_ready_latency(frame, type(result).__name__)
        self.frame_ready.emit(frame, result)
        self._schedule_playback_prebuffer(frame)

        # 更新缓存统计
        self.cache_stats_changed.emit(self._cache.get_stats())

        # 渲染完成后只消费 timer/panel 合并出的下一帧，不主动追墙钟。
        self._pending_layer_count = 0
        if self._dispatch_pending_scrub_exact_if_needed():
            return
        self._advance_to_next_frame()

    def _on_render_error(self, frame: int, error_msg: str) -> None:
        """
        渲染错误回调。

        Drops errors from stale generations so that an old renderer or old
        sequence cannot overwrite the viewer after a new sequence has been set.

        Args:
            frame: 帧号
            error_msg: 错误信息
        """
        # Generation gate (same rationale as _on_frame_rendered)
        if self._generation_id != self._dispatched_generation_id:
            logger.debug(
                f"Dropping stale render error for frame {frame} "
                f"(gen mismatch: current={self._generation_id}, "
                f"dispatched={self._dispatched_generation_id})"
            )
            self._render_in_flight = False
            self._pending_mutex.lock()
            self._pending_frame = None
            self._pending_layer_count = 0
            self._pending_mutex.unlock()
            if self._scrub_exact_in_flight_frame == frame:
                self._scrub_exact_in_flight_frame = None
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        logger.error(f"Render error for frame {frame}: {error_msg}")

        # 清除 pending 标记和渲染中标记
        self._pending_mutex.lock()
        self._pending_frame = None
        self._pending_layer_count = 0
        self._pending_mutex.unlock()
        self._render_in_flight = False
        if self._scrub_exact_in_flight_frame == frame:
            self._scrub_exact_in_flight_frame = None
        self._clear_placeholder_requests_for_frame(frame)
        self._clear_scrub_finish_exact_if_matches(frame)

        if error_msg == "composite_cache_miss":
            # P3.2 竞态兜底（防御位：正常不会到达——GL 渲染器的错误经
            # _on_gpu_backend_render_error 处理）：重新请求该帧。
            playback_trace("renderer.composite_cache_miss_retry", frame=frame)
            self.request_render(frame)
            return

        # 发射错误信号
        self.render_error.emit(frame, error_msg)

        # 即使出错也要推进渲染链
        if self._dispatch_pending_scrub_exact_if_needed():
            return
        self._advance_to_next_frame()

    def _arm_snapshot_for_next_gpu_frame(self) -> None:
        """布防下一个 GPU 合成帧的快照捕获（无 GPU 管线时 no-op）。"""
        renderer = getattr(self, "_gpu_renderer", None)
        if renderer is None or not hasattr(renderer, "arm_frame_snapshot"):
            return
        try:
            renderer.arm_frame_snapshot()
        except Exception:
            pass

    def _invalidate_preview_frame_caches(self) -> None:
        """统一清空预览帧缓存：CPU LRU + 快照键 + GPU 复合纹理缓存。

        时间线/工程/特效参数变化时调用（generation 失效契约的落点）。
        """
        self._cache.clear()
        self._snapshot_frame_keys.clear()
        gpu_renderer = getattr(self, "_gpu_renderer", None)
        invalidate = getattr(gpu_renderer, "invalidate_composite_cache", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception:
                pass
        # 空闲预缓冲的中间状态随之作废。
        self._idle_prebuffer_pending.clear()

    def _on_cuda_resident_import_failed(self, asset_path: str, reason: str,
                                        generation: int) -> None:
        """W03（计划 W-I02）：某素材的 cuda 驻留帧 GL 导入失败。

        真实恢复链入口：转交解码管理器按素材降级（停新驻留请求 → 在飞
        租约归零后关闭旧驻留源 → 下一次 get_frame 重建下载 planar/CPU
        源，当前片段下一帧自动恢复）。这里不做代际过滤——降级针对的是
        "该素材的驻留源本身已不可用"这一事实，跨代仍然成立；重建只发生在
        后续 get_frame（换工程后无该素材请求即无迟到提交）。
        """
        asset_path = str(asset_path or '')
        playback_trace(
            "renderer.cuda_resident_import_failed",
            asset=asset_path,
            reason=str(reason),
            generation=int(generation),
        )
        logger.warning(
            "TimelinePreviewRenderer: cuda resident import failed for "
            "%s (%s), demoting to download/CPU rebuild",
            asset_path, reason,
        )
        manager = getattr(self, "_gpu_decode_manager", None)
        if manager is not None and asset_path:
            try:
                manager.note_resident_import_failure(asset_path, str(reason))
            except Exception as exc:  # noqa: BLE001 —— 恢复链失败留痕不外抛
                logger.warning(
                    "TimelinePreviewRenderer: note_resident_import_failure "
                    "failed for %s: %s", asset_path, exc,
                )

    def _on_gpu_backend_render_error(self, frame: int, error_msg: str) -> None:
        """GL 渲染后端错误：复合缓存竞态 miss 时重试，其余仅记录。

        worker 在解码前确认过复合缓存命中，但 GL 线程 lookup 时条目可能
        刚被 LRU 淘汰（极小概率竞态）。此时清 pending 并重新请求该帧——
        缓存已无条目，必然走完整解码路径，viewer 不弹错误。
        """
        if error_msg == "composite_cache_miss":
            playback_trace("renderer.composite_cache_miss_retry", frame=frame)
            self._pending_mutex.lock()
            self._pending_frame = None
            self._pending_layer_count = 0
            self._pending_mutex.unlock()
            self._render_in_flight = False
            self.request_render(frame)
            return
        logger.warning(f"GPU backend render error frame={frame}: {error_msg}")
        # P1-05: 播放中 GL 渲染失败=本帧未呈现，归因上报给 engine。
        self._note_engine_playback_miss(
            f"gpu_result_drop_render_error:{error_msg}",
            frame=frame,
        )

    def _on_gpu_snapshot_ready(self, frame: int, generation: int, image: Any) -> None:
        """GPU 合成快照到达：回填 CPU 帧缓存（seek 快速路径数据源）。

        快照在帧已 present 之后才到达，这里不发射 viewer 信号——它只服务
        后续 seek 的"先显最近帧/精确命中"。若该帧已有更高质量的条目
        （CPU 全分辨率渲染结果），保留原条目不覆盖。
        """
        if generation != self._generation_id:
            return
        if image is None or image.isNull():
            return
        frame = int(frame)
        if self._cache.get(frame) is not None and frame not in self._snapshot_frame_keys:
            return  # 已有全分辨率条目
        self._cache.put(frame, image)
        self._snapshot_frame_keys.add(frame)
        while len(self._snapshot_frame_keys) > self._SNAPSHOT_KEYS_MAX:
            self._snapshot_frame_keys.pop()
        self._idle_prebuffer_pending.discard(frame)
        playback_trace("renderer.gpu_snapshot_cache_fill", frame=frame)

    # ================================================================
    # P3.1 空闲主动预缓冲（idle prebuffer）
    # ================================================================

    def _idle_prebuffer_anchor_frame(self) -> int:
        """Best-effort current playhead frame for prebuffer anchoring."""
        pending = getattr(self, "_pending_frame", None)
        if pending is not None:
            return int(pending)
        emitted = getattr(self, "_last_emitted_frame", None)
        if emitted is not None and emitted >= 0:
            return int(emitted)
        scrub_latest = getattr(self, "_scrub_latest_frame", None)
        if scrub_latest is not None and scrub_latest >= 0:
            return int(scrub_latest)
        return 0

    def _schedule_idle_prebuffer(self, frame: Optional[int] = None) -> None:
        """布防空闲预缓冲：静默满 delay_ms 后在播放头邻域后台填缓存。

        只布防 + 启动一条低频轮询链；任何渲染请求都会刷新
        _last_render_request_ts，轮询自然推迟。重复调用安全（锚点覆盖）。
        """
        if not getattr(self, "_idle_prebuffer_enabled_flag", False):
            return
        if getattr(self, "_scrub_active", False):
            return
        if self._worker is None:
            return
        if self._idle_prebuffer_radius <= 0:
            return
        # 播放态复核推迟到轮询回调（notify_playback_state(False) 时
        # controller 状态可能尚未完全翻转，这里早退会错失暂停后的调度）。
        self._idle_prebuffer_armed = True
        self._idle_prebuffer_anchor = (
            int(frame) if frame is not None else self._idle_prebuffer_anchor_frame()
        )
        if not self._idle_prebuffer_polling:
            self._idle_prebuffer_polling = True
            playback_trace(
                "renderer.idle_prebuffer_schedule",
                frame=self._idle_prebuffer_anchor,
            )
            QTimer.singleShot(60, self._idle_prebuffer_poll)

    def _idle_prebuffer_poll(self) -> None:
        """低频轮询：静默满 delay_ms 且仍未进入播放/拖拽时启动窗口填充。"""
        self._idle_prebuffer_polling = False
        if not self._idle_prebuffer_armed:
            return
        if self._idle_prebuffer_dispatching:
            return
        if self._worker is None:
            self._idle_prebuffer_armed = False
            return
        if getattr(self, "_scrub_active", False):
            self._idle_prebuffer_armed = False
            return
        if getattr(self, "_is_controller_playing", lambda: False)():
            self._idle_prebuffer_armed = False
            return
        if getattr(self, "_engine_playback_active", False):
            self._idle_prebuffer_armed = False
            return
        quiet_s = time.monotonic() - getattr(self, "_last_render_request_ts", 0.0)
        if quiet_s < self._idle_prebuffer_delay_ms / 1000.0:
            # 尚未静默：继续轮询（渲染完成链的同帧重入只会推迟，不会孤立）。
            self._idle_prebuffer_polling = True
            QTimer.singleShot(60, self._idle_prebuffer_poll)
            return
        self._start_idle_prebuffer(int(self._idle_prebuffer_anchor))

    def _start_idle_prebuffer(self, anchor: int) -> None:
        """构建预缓冲目标窗口并启动逐帧派发链。"""
        self._idle_prebuffer_armed = False
        if self._worker is None:
            return
        if getattr(self, "_scrub_active", False):
            return
        if getattr(self, "_is_controller_playing", lambda: False)():
            return
        if getattr(self, "_engine_playback_active", False):
            return
        generation_id = int(self._generation_id)
        radius = int(self._idle_prebuffer_radius)
        try:
            duration = int(self._playback_prebuffer_duration() or 0)
        except Exception:
            duration = 0
        targets: list[int] = []
        # 正向全部优先：顺序解码 ~10-20ms/帧，先把前进方向铺满；反向是
        # keyframe 反走（数百 ms/帧），用剩余空闲时间慢慢补。
        for candidate in (anchor + offset for offset in range(1, radius + 1)):
            if 0 <= candidate and (duration <= 0 or candidate < duration):
                targets.append(int(candidate))
        for candidate in (anchor - offset for offset in range(1, radius + 1)):
            if 0 <= candidate and (duration <= 0 or candidate < duration):
                targets.append(int(candidate))
        # 缓存已命中的目标无需重渲染；已 pending 的也不会重复入队。
        self._idle_prebuffer_targets = [
            f for f in targets if self._cache.get(f) is None
        ]
        self._prebuffer_stats["cache_hits"] += len(targets) - len(self._idle_prebuffer_targets)
        self._idle_prebuffer_pending.clear()
        self._idle_prebuffer_anchor_gen = generation_id
        self._idle_prebuffer_dispatching = True
        if not self._idle_prebuffer_targets:
            self._idle_prebuffer_dispatching = False
            return
        playback_trace(
            "renderer.idle_prebuffer_start",
            frame=anchor,
            targets=len(self._idle_prebuffer_targets),
        )
        self._tick_idle_prebuffer()

    def _tick_idle_prebuffer(self) -> None:
        """逐帧派发一个后台缓存回填任务；任何渲染活动立即停链。"""
        if not self._idle_prebuffer_dispatching:
            return
        if self._idle_prebuffer_anchor_gen != self._generation_id:
            self._idle_prebuffer_dispatching = False
            return
        if self._worker is None:
            self._idle_prebuffer_dispatching = False
            return
        if getattr(self, "_scrub_active", False):
            self._idle_prebuffer_dispatching = False
            return
        if getattr(self, "_is_controller_playing", lambda: False)():
            self._idle_prebuffer_dispatching = False
            return
        if getattr(self, "_engine_playback_active", False):
            self._idle_prebuffer_dispatching = False
            return
        # 派发期间出现新的渲染请求（seek/步进/播放）→ 立即让位。
        quiet_s = time.monotonic() - getattr(self, "_last_render_request_ts", 0.0)
        if quiet_s < self._idle_prebuffer_delay_ms / 1000.0:
            self._idle_prebuffer_dispatching = False
            self._prebuffer_stats["dropped"] += 1
            playback_trace("renderer.idle_prebuffer_yield")
            return

        # 丢弃已完成/已在缓存的目标。
        targets = self._idle_prebuffer_targets
        while targets:
            frame = targets[0]
            if (
                frame in self._idle_prebuffer_pending
                or self._cache.get(frame) is not None
            ):
                targets.pop(0)
                continue
            break
        else:
            frame = None

        if frame is not None:
            try:
                plan = self._compute_frame_plan(frame)
            except Exception as e:
                logger.debug(f"idle prebuffer plan failed at frame={frame}: {e}")
                plan = None
            has_content = bool(getattr(plan, "has_content", False)) if plan is not None else False
            if plan is None or not has_content:
                # 无内容/计划失败：永久跳过该帧（防循环）。
                targets.pop(0)
            else:
                queued = False
                try:
                    queued = self._worker.enqueue_background_if_idle(
                        frame,
                        plan,
                        self._generation_id,
                        decode_target_size=self._auto2k_decode_target_size(),
                    )
                except Exception as e:
                    logger.debug(f"idle prebuffer enqueue failed at frame={frame}: {e}")
                if queued:
                    self._idle_prebuffer_retry = 0
                    self._idle_prebuffer_pending.add(frame)
                    targets.pop(0)
                    playback_trace(
                        "renderer.idle_prebuffer_queued",
                        frame=frame,
                        remaining=len(targets),
                    )
                else:
                    # worker 正在执行上一帧（队非空）：保留队头，下个 tick 重试。
                    # 上限重试后放弃该帧，避免死循环占住调度链。
                    self._idle_prebuffer_retry = getattr(self, "_idle_prebuffer_retry", 0) + 1
                    if self._idle_prebuffer_retry > 40:
                        self._idle_prebuffer_retry = 0
                        targets.pop(0)
                        self._prebuffer_stats["dropped"] += 1
                        playback_trace(
                            "renderer.idle_prebuffer_skip_busy",
                            frame=frame,
                        )

        if not self._idle_prebuffer_targets:
            self._idle_prebuffer_dispatching = False
            playback_trace("renderer.idle_prebuffer_done")
            return
        gap_ms = int(self._idle_prebuffer_gap_ms)
        QTimer.singleShot(gap_ms, self._tick_idle_prebuffer)

    def _on_worker_background_frame_ready(self, frame: int, generation: int, image: Any) -> None:
        """CPU 后台缓存回填结果到达：只写帧缓存，不发射 viewer 信号。"""
        if generation != self._generation_id:
            return
        frame = int(frame)
        self._idle_prebuffer_pending.discard(frame)
        if image is None or image.isNull():
            return
        if self._cache.get(frame) is not None and frame not in self._snapshot_frame_keys:
            return  # 已有不低于本条目质量的缓存
        self._cache.put(frame, image)
        self._snapshot_frame_keys.discard(frame)
        self._prebuffer_stats["cache_fills"] += 1
        playback_trace("renderer.idle_prebuffer_cache_fill", frame=frame)

    def _note_engine_playback_miss(
        self,
        reason: str,
        *,
        frame: int = -1,
        generation_id: int | None = None,
    ) -> None:
        """P1-05: 把异步呈现失败的真实归因上报给 RealtimePlaybackEngine。

        engine 的 delegate 契约只返回 bool（提交成功≠呈现成功）；GPU 结果
        drop 点在这里上报分类（gpu busy / stale / 协议违规），engine 的
        下一次 miss 消费该归因决定 hold/rebuffer。engine 缺失（headless
        测试/未配置）时静默跳过；generation 不匹配的上报由 engine 丢弃。
        """
        engine = getattr(self, "_realtime_engine", None)
        if engine is None:
            return
        try:
            engine.note_playback_miss(
                reason,
                generation_id=generation_id,
                timeline_frame=int(frame),
            )
        except Exception:
            logger.debug("engine miss note failed", exc_info=True)

    def _on_gpu_frame_ready(
        self,
        frame: int,
        generation: int,
        tex_id: int,
        width: int,
        height: int,
    ) -> None:
        """
        GPU 帧渲染完成回调。

        由 EditGpuPreviewRenderer.frame_ready 信号触发，
        携带 frame/generation/tex_id/width/height。

        通过 generation 验证、写入缓存、清除 pending 标记，
        确保 GPU 结果与 CPU 结果走同一条 pipeline。

        Args:
            frame: 帧号
            generation: 渲染任务携带的 generation ID
            tex_id: OpenGL 纹理 ID（共享上下文）
            width: 纹理宽度
            height: 纹理高度
        """
        from src.shared.video.playback_frame_trace import (
            EditPlaybackFrameTrace,
            get_trace_collector,
        )
        # P0-02: 消费本帧在飞身份 (trace_id, deadline_ns)，供下方 guard-drop
        # trace 归因；本调用是每个 GPU 结果的终点（成功或某种 drop），
        # 无论走哪条分支都取走，避免在飞字典滞留旧帧。
        frame_trace_id, frame_deadline_ns = self._take_in_flight_frame_ids(frame)
        late_key = getattr(self, "_late_realtime_present_key", None)
        if late_key is not None and late_key != (int(frame), int(generation)):
            # A newer GPU result supersedes any unmatched descriptor from the
            # previous frame; it must never be presented late.
            self._late_realtime_present_key = None

        # Generation gate: reject stale GPU results
        if generation != self._generation_id or generation != self._dispatched_generation_id:
            stale_present = self._pop_pending_present_frame(frame, generation)
            if stale_present is not None:
                self._ack_present_slot_safe(getattr(stale_present, "slot_id", -1))
            playback_trace(
                "renderer.gpu_result_drop_generation",
                frame=frame,
                generation=generation,
                current_generation=self._generation_id,
                dispatched_generation=self._dispatched_generation_id,
                tex_id=tex_id,
            )
            logger.debug(
                f"Dropping stale GPU frame {frame} (gen mismatch: "
                f"generation={generation}, dispatched={self._dispatched_generation_id})"
            )
            # Record stale frame drop
            trace = EditPlaybackFrameTrace(
                frame_index=frame,
                page='edit_timeline',
                decode_path='gpu',
                render_path='gpu_composite',
                fallback_reason='stale_generation_dropped',
                stale_frame_emitted=True,
                generation_id=generation,
                gpu_texture_slot=tex_id,
                trace_id=frame_trace_id,
                deadline_ns=frame_deadline_ns,
            )
            get_trace_collector().record(trace)
            self._render_in_flight = False
            self._pending_mutex.lock()
            self._pending_frame = None
            self._pending_mutex.unlock()
            self._clear_placeholder_requests_for_frame(frame)
            if self._scrub_exact_in_flight_frame == frame:
                self._scrub_exact_in_flight_frame = None
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        scrub_exact_frame = getattr(self, "_scrub_exact_frame", None)
        if scrub_exact_frame is not None and int(frame) != int(scrub_exact_frame):
            stale_present = self._pop_pending_present_frame(frame, generation)
            if stale_present is not None:
                self._ack_present_slot_safe(getattr(stale_present, "slot_id", -1))
            playback_trace(
                "renderer.gpu_result_drop_scrub_non_final",
                frame=frame,
                scrub_exact_frame=scrub_exact_frame,
                generation=generation,
                tex_id=tex_id,
            )
            self._drop_scrub_render_result(frame, "gpu_finish_non_final")
            return

        if self._scrub_active and not self._can_emit_scrub_render_result(frame):
            stale_present = self._pop_pending_present_frame(frame, generation)
            if stale_present is not None:
                self._ack_present_slot_safe(getattr(stale_present, "slot_id", -1))
            playback_trace(
                "renderer.gpu_result_drop_active_scrub_too_far_behind",
                frame=frame,
                latest=self._scrub_latest_frame,
                generation=generation,
                tex_id=tex_id,
            )
            self._drop_scrub_render_result(frame, "gpu_active_too_far_behind")
            return

        # Prefer the Batch 1 GpuPresentFrame descriptor. The renderer still
        # emits the legacy frame_ready signal to drive the existing pending /
        # monotonic / advance logic, but the actual Viewer payload must keep
        # fence + slot ack metadata. Falling back to a naked texture tuple is
        # only for old renderers/tests that do not emit present_frame_ready.
        present_frame = self._pop_pending_present_frame(frame, generation)
        if present_frame is not None:
            gpu_renderer = getattr(self, '_gpu_renderer', None)
            ack_cb = (
                gpu_renderer.ack_present_slot
                if gpu_renderer is not None and hasattr(gpu_renderer, 'ack_present_slot')
                else None
            )
            result = {
                "present_frame": present_frame,
                "ack_cb": ack_cb,
                # A0.1：Viewer 侧呈现状态 annotate（viewer_copy/displayed/
                # superseded）的身份；旁路帧为 0（annotate 明确拒收并计数）。
                "trace_id": int(frame_trace_id or 0)
                or self._lookup_frame_trace_id(int(frame)),
            }
        elif getattr(self, "_engine_playback_active", False):
            # Final-4 (FINAL-P1-10 / FINAL-P0-09): REALTIME 热路径禁止裸 tuple。
            # 没有 GpuPresentFrame（fence + slot）的纹理在 REALTIME 下视为非法：
            # tuple 会绕过 fence/ack 协议，可能导致混帧/半帧。这里丢弃并 hold。
            # 注意：_is_realtime_playback_active() 是 PreviewRenderWorker 的方法，
            # TimelinePreviewRenderer 实例上要用 _engine_playback_active 字段判断
            # 是否处于播放态（由 notify_playback_state 设置）。
            playback_trace(
                "renderer.gpu_result_drop_realtime_no_present",
                frame=frame,
                generation=generation,
                tex_id=tex_id,
            )
            self._note_engine_playback_miss(
                "gpu_result_drop_realtime_no_present",
                frame=frame,
                generation_id=int(generation),
            )
            from src.shared.video.playback_frame_trace import (
                EditPlaybackFrameTrace,
                get_trace_collector,
            )
            get_trace_collector().record(EditPlaybackFrameTrace(
                frame_index=frame,
                page='edit_timeline',
                decode_path='gpu',
                render_path='gpu_composite',
                fallback_reason='realtime_legacy_tuple_rejected',
                stale_frame_emitted=True,
                generation_id=generation,
                gpu_texture_slot=tex_id,
                trace_id=frame_trace_id,
                deadline_ns=frame_deadline_ns,
            ))
            # Keep a narrowly-scoped rendezvous key for a valid fenced
            # descriptor that may be delivered after this completion signal.
            self._late_realtime_present_key = (int(frame), int(generation))
            self._render_in_flight = False
            self._pending_mutex.lock()
            self._pending_frame = None
            self._pending_mutex.unlock()
            self._pending_layer_count = 0
            self._clear_placeholder_requests_for_frame(frame)
            if self._scrub_exact_in_flight_frame == frame:
                self._scrub_exact_in_flight_frame = None
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return
        else:
            # 非 realtime（paused/step/scrub/测试）：保留 tuple 兼容路径。
            result = (tex_id, width, height)

        # NOTE: Do NOT cache GPU texture results. GPU textures are managed
        # by the renderer's triple-buffer pool and can be recycled after a
        # few frames. Caching a tex_id that gets reused would cause stale
        # or wrong frame content to appear on cache hits (frame jitter).
        # Only QImage (CPU) results are safe to cache because they hold
        # independent pixel data.

        # Clear pending frame marker and render-in-flight
        self._pending_mutex.lock()
        queued = self._queued_frame
        pending_matches = self._pending_frame == frame
        if pending_matches:
            self._pending_frame = None
            self._pending_layer_count = 0
        pending_taken_over = self._pending_frame is not None
        self._pending_mutex.unlock()
        in_flight_matches = self._scrub_exact_in_flight_frame == frame
        if self._scrub_active:
            if pending_matches or in_flight_matches:
                self._render_in_flight = False
        elif not pending_taken_over:
            # 流水线化：本帧 GL 提交时已放行单飞，pending 可能已属于
            # 更新的在飞帧——只有本帧仍是当前 pending（或没有新请求
            # 接管）时才释放，避免误清下一帧的在飞状态。
            self._render_in_flight = False
        if in_flight_matches:
            self._scrub_exact_in_flight_frame = None
        self._clear_placeholder_requests_for_frame(frame)
        playback_trace(
            "renderer.gpu_result_ready",
            frame=frame,
            generation=generation,
            tex_id=tex_id,
            width=width,
            height=height,
            queued=queued,
        )
        if self._scrub_active:
            self._trace_scrub_stage_latency(
                frame,
                "renderer.scrub_gpu_ready_latency",
                tex_id=tex_id,
                width=width,
                height=height,
            )

        # 单调帧守卫：丢弃比已显示帧更旧的乱序 GPU 渲染结果，防止画面抖动
        if self._is_playback_result_stale(frame):
            playback_trace(
                "renderer.gpu_result_drop_playback_stale",
                frame=frame,
                controller_frame=getattr(self._playback_controller, "current_frame", None),
                tex_id=tex_id,
            )
            self._note_engine_playback_miss(
                "gpu_result_drop_playback_stale",
                frame=frame,
                generation_id=int(generation),
            )
            logger.debug(
                f"Dropping stale playback GPU frame {frame} "
                f"(controller_current={self._playback_controller.current_frame})"
            )
            trace = EditPlaybackFrameTrace(
                frame_index=frame,
                page='edit_timeline',
                decode_path='gpu',
                render_path='gpu_composite',
                fallback_reason='playback_current_frame_guard_dropped',
                stale_frame_emitted=True,
                generation_id=generation,
                gpu_texture_slot=tex_id,
                trace_id=frame_trace_id,
                deadline_ns=frame_deadline_ns,
            )
            get_trace_collector().record(trace)
            if present_frame is not None:
                self._ack_present_slot_safe(getattr(present_frame, "slot_id", -1))
            self._pending_layer_count = 0
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        # P3.1 暂停态播放头比对：缓存命中提前发射后，前一帧的 refine 结果
        # 可能迟到——它已不是用户当前停留的帧，上屏会回闪旧画面。暂停态
        # 只显示播放头帧（scrub 有自己的 latest-only 逻辑，播放态走上面的
        # playback-stale 守卫）。
        if (
            not self._scrub_active
            and not getattr(self, "_is_controller_playing", lambda: False)()
            and int(frame) != int(self.get_playhead())
        ):
            playback_trace(
                "renderer.gpu_result_drop_paused_playhead",
                frame=frame,
                playhead=self.get_playhead(),
                tex_id=tex_id,
            )
            trace = EditPlaybackFrameTrace(
                frame_index=frame,
                page='edit_timeline',
                decode_path='gpu',
                render_path='gpu_composite',
                fallback_reason='paused_playhead_guard_dropped',
                stale_frame_emitted=True,
                generation_id=generation,
                gpu_texture_slot=tex_id,
                trace_id=frame_trace_id,
                deadline_ns=frame_deadline_ns,
            )
            get_trace_collector().record(trace)
            if present_frame is not None:
                self._ack_present_slot_safe(getattr(present_frame, "slot_id", -1))
            self._pending_layer_count = 0
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        # 单调帧守卫：丢弃比已显示帧更旧的乱序 GPU 渲染结果，防止画面抖动
        if not self._should_emit_frame(frame):
            playback_trace(
                "renderer.gpu_result_drop_monotonic",
                frame=frame,
                last_emitted=self._last_emitted_frame,
                tex_id=tex_id,
            )
            logger.debug(
                f"Dropping stale GPU frame {frame} (last_emitted={self._last_emitted_frame}): "
                f"async GPU render arrived out of order"
            )
            # Record stale frame drop
            trace = EditPlaybackFrameTrace(
                frame_index=frame,
                page='edit_timeline',
                decode_path='gpu',
                render_path='gpu_composite',
                fallback_reason='monotonic_guard_dropped',
                stale_frame_emitted=True,
                generation_id=generation,
                gpu_texture_slot=tex_id,
                trace_id=frame_trace_id,
                deadline_ns=frame_deadline_ns,
            )
            get_trace_collector().record(trace)
            # BUG FIX: 推进 _last_emitted_frame 到被丢弃帧，防止无限循环。
            # 不推进时，_advance_to_next_frame 的非播放路径判断
            # next_frame(=controller.current_frame) != _last_emitted_frame → 重新请求
            # → 又渲染同一帧 → 又被丢弃 → 死循环刷屏。
            # 推进后 next_frame == _last_emitted_frame → skip → 循环终止。
            self._advance_last_emitted_to_dropped_frame(frame)
            if present_frame is not None:
                self._ack_present_slot_safe(getattr(present_frame, "slot_id", -1))
            self._pending_layer_count = 0
            if self._dispatch_pending_scrub_exact_if_needed():
                return
            self._advance_to_next_frame()
            return

        self._mark_frame_emitted(frame)
        if self._scrub_active:
            self._scrub_last_real_frame = int(frame)
            self._scrub_last_real_frame_at = time.perf_counter()
        if scrub_exact_frame is not None and int(frame) == int(scrub_exact_frame):
            self._scrub_exact_frame = None
        self._clear_scrub_finish_exact_if_matches(frame)

        # T-03/P0-04: planar 上传证据迟到合并——worker 记录 trace 时 GL
        # 上传尚未发生（异步 submit），GL 完成后把本帧 planar 轨的
        # 上传字节 / 模式 / import_upload 段耗时按 trace_id 补进成功 trace。
        # A0.1：零拷贝帧（mode=iosurface_zero_copy 且 bytes==0）标
        # import_upload_status='not_applicable'（"无需该段"与实测 0 区分）；
        # planar 帧标 'measured'（0.0ms 也是有效实测）。upload 段耗时同步
        # 回填 engine telemetry。快照回读证据（consume_readback_stats）
        # 与 GPU timer query 发起帧回填（drain_completed_gpu_queries）
        # 在同一完成回调消费。
        _consume_upload = getattr(
            self._gpu_renderer, "consume_planar_upload_stats", None
        ) if self._gpu_renderer is not None else None
        if callable(_consume_upload):
            _upload_stats = _consume_upload(int(frame))
            if _upload_stats and frame_trace_id:
                _mode = str(_upload_stats.get("mode", ""))
                _zero_copy_only = (
                    _mode == "iosurface_zero_copy"
                    and int(_upload_stats.get("bytes", 0)) == 0
                )
                try:
                    if _zero_copy_only:
                        get_trace_collector().annotate(
                            int(frame_trace_id),
                            import_upload_status="not_applicable",
                            extra={"zero_copy_import": True},
                        )
                    else:
                        get_trace_collector().annotate(
                            int(frame_trace_id),
                            cpu_upload_bytes=int(_upload_stats.get("bytes", 0)),
                            texture_write_bytes=int(_upload_stats.get("bytes", 0)),
                            import_upload_ms=float(_upload_stats.get("ms", 0.0)),
                            import_upload_status="measured",
                            extra={
                                "planar_upload": {
                                    "mode": _mode,
                                    "pixel_format": _upload_stats.get("pixel_format", ""),
                                    "gbr_mode": bool(_upload_stats.get("gbr", False)),
                                    "tracks": int(_upload_stats.get("tracks", 0)),
                                }
                            },
                        )
                except Exception:
                    pass
                try:
                    engine = getattr(self, "_realtime_engine", None)
                    telemetry = getattr(engine, "telemetry", None)
                    enrich = getattr(telemetry, "enrich_last", None)
                    if callable(enrich):
                        if _zero_copy_only:
                            enrich(int(frame), trace_id=int(frame_trace_id),
                                   upload_status="not_applicable")
                        else:
                            enrich(int(frame), trace_id=int(frame_trace_id),
                                   upload_ms=float(_upload_stats.get("ms", 0.0)))
                except Exception:
                    pass

        # A0.1：快照回读证据（GL 线程暂存）按发起帧 annotate。
        _consume_readback = getattr(
            self._gpu_renderer, "consume_readback_stats", None
        ) if self._gpu_renderer is not None else None
        if callable(_consume_readback):
            _readback = _consume_readback(int(frame))
            if _readback:
                _rb_trace = int(frame_trace_id) or self._lookup_frame_trace_id(int(frame))
                _kinds = [str(k) for k in (_readback.get("kinds") or []) if k]
                if _rb_trace and _kinds:
                    _annotate_fields: dict = {
                        "cpu_download_bytes": int(_readback.get("bytes", 0)),
                        "readback_kind": _kinds[0],
                    }
                    if len(_kinds) > 1:
                        _annotate_fields["extra"] = {"readback_kinds": _kinds}
                    try:
                        get_trace_collector().annotate(_rb_trace, **_annotate_fields)
                    except Exception:
                        pass

        # A0.1：GPU timer query 发起帧回填——drain 出的结果属于
        # RING_DEPTH-1 帧前的发起帧（tag），按其 trace_id annotate；
        # 读回失败（ms=None）保持 gpu_time_unavailable=True（fail-closed）。
        _drain_queries = getattr(
            self._gpu_renderer, "drain_completed_gpu_queries", None
        ) if self._gpu_renderer is not None else None
        if callable(_drain_queries):
            try:
                for _query_result in _drain_queries():
                    _ms = _query_result.get("ms")
                    if _ms is None:
                        continue
                    _origin_frame = int(_query_result.get("tag", -1) or -1)
                    _origin_trace = self._lookup_frame_trace_id(_origin_frame)
                    if not _origin_trace:
                        continue  # 旁路帧/滚出历史：annotate 计数不可用，跳过
                    get_trace_collector().annotate(
                        _origin_trace,
                        gpu_submit_ms=round(float(_ms), 3),
                        gpu_time_unavailable=False,
                    )
            except Exception:
                pass

        # Emit to ProgramViewer (same path as CPU results)
        playback_trace("renderer.gpu_frame_ready_emit", frame=frame, tex_id=tex_id, width=width, height=height)
        if self._scrub_active:
            self._trace_scrub_frame_ready_latency(frame, "gpu")
        self.frame_ready.emit(frame, result)
        self._schedule_playback_prebuffer(frame)

        logger.debug(
            f"GPU frame ready: frame={frame}, tex={tex_id}, "
            f"size={width}x{height}, gen={generation}"
        )

        # 渲染完成后只消费 timer/panel 合并出的下一帧，不主动追墙钟。
        self._pending_layer_count = 0
        if self._dispatch_pending_scrub_exact_if_needed():
            return
        self._advance_to_next_frame()


# ================================================================
# 辅助函数
# ================================================================

def create_timeline_preview_renderer(
    project: EditProject,
    sequence: TimelineSequence,
    main_context: Any,
    config: Optional[Dict[str, Any]] = None,
) -> TimelinePreviewRenderer:
    """
    创建 TimelinePreviewRenderer 实例。

    工厂函数，简化创建流程。

    Args:
        project: EditProject 实例
        sequence: TimelineSequence 实例
        main_context: 主 OpenGL 上下文
        config: 配置参数（可选）

    Returns:
        TimelinePreviewRenderer 实例
    """
    # 默认配置
    if config is None:
        config = {}

    # 创建 VideoFrameProviderService
    from src.shared.video.frame_provider_service import VideoFrameProviderService

    frame_provider = VideoFrameProviderService(
        max_proxy_cache=config.get("cache_size", 16),
        auto_generate_disk_proxies=False,
    )

    # 创建 VideoAsyncRenderer（如果提供 main_context）
    video_renderer = None
    if main_context is not None:
        from src.shared.video.video_async_renderer import VideoAsyncRenderer

        video_renderer = VideoAsyncRenderer(main_context)
        video_renderer.start()

    # 创建预览渲染器
    renderer = TimelinePreviewRenderer(
        project=project,
        sequence=sequence,
        frame_provider=frame_provider,
        video_renderer=video_renderer,
        main_context=main_context,
        viewport_size=config.get("viewport_size", (1920, 1080)),
        cache_size=config.get("cache_size", 256),
        max_preview_edge=config.get("max_preview_edge", 3840),
        scrub_preview_edge=config.get("scrub_preview_edge", 3840),
    )

    return renderer
