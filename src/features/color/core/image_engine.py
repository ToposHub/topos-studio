"""ImageEngine：Topos Color 调色页的图像引擎（状态与编排层）。

职责
----
- 持有调色会话的全部状态：源图像多级缓存（full/high/low）、图层栈
  （Layer 列表 + active_layer_index）、RAW / 胶片颗粒 / split-view
  等参数、历史（HistoryManager）、蒙版跟踪（TrackingStore）。
- 作为 UI 线程与渲染线程之间的唯一编排入口：把 Layer / 蒙版 / 参数
  快照为不可变 DTO（见 ``_layer_render_data``），经 ``RenderCommand``
  提交给 AsyncRenderer，结果通过 Qt 信号转发回 UI。
- 负责蒙版栅格化（CPU 侧 QPainter / cv2 生成 mask 纹理并上传）、
  AI matte（BiRefNet / SAM / ONNX 插件）工作线程调度，以及
  bake / unbake（栅格化冻结）生命周期管理。

与 async_renderer 的边界
------------------------
- 本模块运行在 UI 线程，只做状态维护、DTO 构造与命令提交，不直接
  执行渲染线程的 GL 工作（``_do_render_full_res`` 等本地同步路径是
  无 AsyncRenderer 的 headless / legacy 兼容场景专用）。
- AsyncRenderer 运行在独立线程 + 共享 GL context 上，拥有纹理的
  创建、执行、同步与回读；ImageEngine 通过 ``renderer.submit()``
  单向驱动，通过 ``frame_ready`` / ``color_output_ready`` 等信号
  单向接收结果。

状态机（核心不变量）
--------------------
- 图层：``layers`` 自底向上逐层合成，``active_layer_index`` 指向参数
  编辑目标；图层变更后由调用方触发 ``render_pipeline()``。
- 蒙版：``layer.mask_dirty`` 标记栅格化过期；``_update_mask_textures``
  在非 transient 渲染前重栅格化并上传（交互中 512 基准宽，静止
  1024）。形状/群组蒙版与 AI matte 按 ``ai_matte_operation`` 布尔合成。
- bake：baked 纹理的语义是「到该层为止的完整栈前缀」。bake 时记录
  前缀内容摘要（``_bake_prefix_digests``），渲染入口
  ``_validate_baked_prefixes`` 校验；下层任何变更（含源图 epoch 递增）
  都会使摘要失配并自动解冻（P0-COL-04）。
"""

import cv2
import numpy as np
import copy
import moderngl
import time  # noqa: F401 — 回归守卫 patch 锚点（tests/color/test_quality_control 断言种子不使用 wall-clock）
import os
import logging
import threading
from typing import Optional, List, Dict, Any
from PyQt6.QtGui import QOpenGLContext, QPainter, QPainterPath, QBrush, QImage
from PyQt6.QtCore import (
    QObject, pyqtSignal, pyqtSlot, Qt, QPointF,
    QRectF, QThread, QEventLoop, QTimer,
)
from src.features.color.core.history import HistoryManager
from src.features.color.core.layer import Layer, MaskShape, MaskGroup
from src.shared.gpu.gl_backend import GLContextManager, FramebufferManager
from src.features.color.shaders.shaders import PASSTHROUGH_VERTEX, PASSTHROUGH_FRAGMENT, SPLIT_VIEW_FRAGMENT, ROTATION_PREVIEW_VERTEX, ROTATION_PREVIEW_FRAGMENT
from src.features.color.core.shader_compiler import ShaderPipeline
from src.features.color.core.async_renderer import AsyncRenderer, RenderCommand
from src.features.color.core.color_output import ColorOutput
from src.shared.ai_models.ai_mask_engine import AIMaskWorker

logger = logging.getLogger(__name__)

# P1-THREAD-01：terminate 后仍存活的线程对象在此「泄漏」到进程退出，
# 防止 GC 在线程存活期间析构 QThread 触发 Abort。
_TERMINATED_THREADS: list = []

# P1-COLOR-13: grain 动画步长 = 黄金比例小数部分，帧间散布均匀且确定
GRAIN_ANIMATION_STEP = 0.6180339887498949


def effective_grain_seed(grain_params: Dict[str, Any], frame_index: int) -> float:
    """P1-COLOR-13: 计算 grain 的有效种子（确定性）。

    - 基础 seed 完全由参数决定（不再取墙钟时间）；
    - ``animated`` 模式按帧号以黄金比例小数步长推进：同一帧重复渲染
      结果一致，回放/重渲/导出完全可复现，且逐帧颗粒图案不重复。
    """
    seed = float(grain_params.get("seed", 0.0) or 0.0)
    if grain_params.get("animated"):
        frame = int(frame_index)
        if frame >= 0:
            seed = (seed + frame * GRAIN_ANIMATION_STEP) % 1.0
    return seed


def _copy_array_like(value: Any) -> Any:
    """AUD-0609：ndarray/list 形态的层字段按值拷贝，其余原样返回。

    渲染线程 DTO 必须不可变——curves_lut/hsl_curve_lut/warper_params/
    ai_matte 此前按引用传递，UI 线程对同一 ndarray 的任何原地写会在
    渲染线程 .tobytes()/上传时产生撕裂数据。
    """
    if isinstance(value, np.ndarray):
        return value.copy()
    if isinstance(value, list):
        return list(value)
    return value


def _layer_render_data(layer: Layer) -> Dict[str, Any]:
    """Build the immutable layer DTO consumed by the render thread.

    Keep this conversion in one place.  The preview and ColorOutput paths used
    to have near-identical hand-written versions which silently dropped both
    bake state and AI-plugin identity fields.
    """
    plugins = []
    for plugin in getattr(layer, "plugins", ()) or ():
        if not isinstance(plugin, dict):
            continue
        plugins.append({
            "instance_id": plugin.get("instance_id"),
            "plugin_id": plugin.get("plugin_id"),
            "type": plugin.get("type", "dctl"),
            "enabled": plugin.get("enabled", True),
            "params": (plugin.get("params") or {}).copy(),
        })

    mask_shapes = [shape.to_dict() for shape in layer.mask_shapes]
    return {
        "id": layer.id,
        "visible": layer.visible,
        "opacity": layer.opacity,
        "blend_mode": layer.blend_mode,
        "grading_params": layer.grading_params.copy() if layer.grading_params else None,
        "active_lut_path": layer.active_lut_path,
        "lut_intensity": layer.lut_intensity,
        "soft_clip_params": layer.soft_clip_params.copy() if layer.soft_clip_params else None,
        "qualifier_params": layer.qualifier_params.copy() if layer.qualifier_params else None,
        "curves_lut": _copy_array_like(layer.curves_lut),
        "hsl_curve_lut": _copy_array_like(layer.hsl_curve_lut),
        "warper_params": _copy_array_like(layer.warper_params),
        "mixer_params": layer.mixer_params.copy() if layer.mixer_params else None,
        "effects_params": layer.effects_params.copy() if layer.effects_params else None,
        "mask_shapes": mask_shapes,
        "has_mask": any(shape.visible for shape in layer.mask_shapes)
        or (layer.ai_matte is not None and layer.ai_matte_enabled),
        "plugins": plugins,
        "is_baked": bool(getattr(layer, "is_baked", False)),
    }


class Tile:
    """Legacy 瓦片渲染的数据单元（像素坐标 + 尺寸 + 金字塔层级）。

    保留给旧的 tile 管线接口；当前渲染路径整图提交，不再使用。
    """

    def __init__(self, x: int, y: int, w: int, h: int, level: int) -> None:
        """Initialize tile position, size (pixels) and pyramid level."""
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.level = level

class RawUpdateWorker(QObject):
    """Background worker for heavy RAW decoding tasks."""
    finished = pyqtSignal(object, object, object, dict) # full, high, low, applied_params
    error = pyqtSignal(str)

    def __init__(self):
        """Initialize busy flag and the cooperative cancellation event."""
        super().__init__()
        self._is_busy = False
        self._cancel_event = threading.Event()

    def request_cancel(self) -> None:
        """Stop delivery of a decode result after the current native call."""
        self._cancel_event.set()

    @pyqtSlot(str, dict)
    def decode(self, path, params):
        """Decode a RAW file on the worker thread and emit the result.

        Emits ``finished`` with (full, high, low, applied_params) or
        ``error``; after :meth:`request_cancel` the pending result is
        dropped instead of emitted.
        """
        self._is_busy = True
        try:
            if self._cancel_event.is_set():
                return
            from src.shared.io.file_io import FileIO
            full, high, low, applied_params = FileIO.update_raw_decoding(path, params, preview_only=False)
            if self._cancel_event.is_set():
                return
            self.finished.emit(full, high, low, applied_params)
        except Exception as e:
            if not self._cancel_event.is_set():
                self.error.emit(str(e))
        finally:
            self._is_busy = False

class ImageEngine(QObject):
    """调色页核心引擎：图层栈 / 蒙版 / RAW 与调色参数的编排和渲染调度。

    完整职责、与 AsyncRenderer 的边界、图层/蒙版/bake 状态机见模块级
    docstring。典型生命周期：``__init__``（UI 线程）→
    ``initialize_async_renderer(main_context)``（共享 GL context 就绪
    后创建渲染线程）→ ``set_image(...)`` 装载源图 → 各 ``set_*`` /
    ``apply_*`` 入口驱动 ``render_pipeline()`` →
    ``shutdown_worker_threads()`` 收尾。
    """

    TILE_SIZE = 512
    MAX_PYRAMID_LEVELS = 6
    RAW_EXTENSIONS = ['.arw', '.cr2', '.cr3', '.nef', '.dng', '.orf', '.raf', '.sr2', '.srf', '.rw2', '.pef', '.x3f']

    @staticmethod
    def _path_transfer_is_linear(path: str) -> bool:
        """M1-F5：源文件传输特性是否为线性（scene/display-linear）。

        仅 .toos 走媒体层探测（ToposImageSource 元数据，color_transfer
        字符串）；其他格式不在此判定（EXR 等 linear 源统一口径见 W3）。
        任何异常按 False（非线性）返回——不阻塞图像加载。
        """
        try:
            if not path.lower().endswith('.toos'):
                return False
            from src.shared.media.topos_image_source import ToposImageSource
            src = ToposImageSource(path)
            try:
                src.open()
                meta = src.probe()
                return str(getattr(meta, 'color_transfer', '') or '') == 'linear'
            finally:
                src.close()
        except Exception:
            return False
    
    frame_ready = pyqtSignal(int)
    scope_snapshot_ready = pyqtSignal(object)
    gpu_scope_ready = pyqtSignal(object)  # ScopeGpuFrame descriptor (no CPU readback)
    error_occurred = pyqtSignal(str)
    mask_shapes_changed = pyqtSignal()
    sam_encode_finished = pyqtSignal()
    ai_process_finished = pyqtSignal()
    color_output_changed = pyqtSignal(int, int, int)  # texture_id, width, height
    monitor_color_output_changed = pyqtSignal(int, int, int)  # texture_id, width, height
    full_resolution_ready = pyqtSignal(str, object)  # request_id, BGR ndarray
    full_resolution_failed = pyqtSignal(str, str)  # request_id, reason
    full_resolution_progress = pyqtSignal(str, int, str)  # request_id, percent, phase

    # AI Baking Signals
    layer_baking_started = pyqtSignal(int)  # layer_index
    layer_baking_finished = pyqtSignal(int)  # layer_index

    # AI Process Signals (Robust Cross-Thread communication)
    sig_process_birefnet = pyqtSignal(np.ndarray)
    sig_process_sam_encode = pyqtSignal(np.ndarray)
    sig_process_sam_decode = pyqtSignal(list, list)

    def __init__(self):
        """Initialize engine state, background workers, and per-session stores.

        创建 RAW / AI Mask 两个后台 QThread（关闭时必须调用
        shutdown_worker_threads()）、AI 插件推理引擎、历史管理器与蒙版
        跟踪存储。AsyncRenderer 在此尚未创建——须等共享 GL context
        就绪后由 initialize_async_renderer() 注入。
        """
        super().__init__()
        self.gl_manager = GLContextManager.instance()

        # P4-2 SAFETY: Reference to Canvas for paint/render cycle detection
        # Set by MainWindow after canvas.engine = self.engine
        self.canvas_ref = None

        self.img_full_original = None # BGR uint8 (Phase 3.5 Optimization)
        self.img_source_full = None   # Original source image for non-destructive crop

        # Source-level AI bake (denoise/deblur result, persisted in .topos project)
        self.source_bake_file_name = None   # e.g. "source_bake_<uuid>.exr"
        self.source_bake_type = None        # "denoise" | "deblur" | None

        # M5: 视频降噪参数（时域+空域，非破坏）。独立于图片降噪 source_bake，
        # 持久化到项目顶层 "video_denoise" 字段。
        self.video_denoise_params = None    # VideoDenoiseParams | None

        # Flip state tracking (for Edit menu flip operations)
        self._flip_h = False
        self._flip_v = False

        self.crop_params = {
            "rect": (0.0, 0.0, 1.0, 1.0), # Normalized (x, y, w, h)
            "rotation": 0.0,              # Degrees
        }
        
        # RAW Background Worker
        self.raw_worker_thread = QThread()
        # 命名：QThread destroyed-while-running 崩溃报告可直指责任线程
        self.raw_worker_thread.setObjectName("ImageEngineRawWorker")
        self.raw_worker = RawUpdateWorker()
        self.raw_worker.moveToThread(self.raw_worker_thread)
        self.raw_worker.finished.connect(self._on_raw_update_finished)
        self.raw_worker_thread.start()

        # AI Mask Worker
        self.ai_worker_thread = QThread()
        self.ai_worker_thread.setObjectName("ImageEngineAIWorker")
        self.ai_worker = AIMaskWorker()
        self.ai_worker.moveToThread(self.ai_worker_thread)
        self.ai_worker.mask_ready.connect(self.on_ai_mask_ready)
        self.ai_worker.error.connect(self.error_occurred.emit)
        self.ai_worker.sam_encode_finished.connect(self.sam_encode_finished.emit)
        
        # Connect AI Signals to Worker slots
        self.sig_process_birefnet.connect(self.ai_worker.process_birefnet)
        self.sig_process_sam_encode.connect(self.ai_worker.process_sam_encode)
        self.sig_process_sam_decode.connect(self.ai_worker.process_sam_decode)
        
        self.ai_worker_thread.start()

        # AI Plugin Engine (ONNX inference for AI plugins)
        from src.shared.ai_models.ai_plugin_engine import AIPluginEngine
        self.ai_plugin_engine = AIPluginEngine()
        self.ai_plugin_engine.processing_finished.connect(self._on_ai_plugin_finished)
        self.ai_plugin_engine.progress_updated.connect(self._on_ai_plugin_progress)
        self.ai_plugin_engine.error_occurred.connect(self._on_ai_plugin_error)
        self.ai_plugin_result_cache = {}  # {(layer_id, instance_id): numpy_array} CPU-side cache for persistence
        # P0-COL-04：bake 前缀摘要台账。baked 纹理的语义是「到该层为止的
        # 完整栈前缀」，下层任何变更都会使其过期；bake 文件名只含 layer.id
        # 无法表达版本，因此在 bake 时记录前缀内容摘要，渲染入口校验，
        # 不匹配即自动解冻（见 _validate_baked_prefixes）。
        self._bake_prefix_digests = {}  # {layer_id: digest_str}
        self._pending_bake_digests = {}  # bake 进行中的摘要（完成时转正）
        # AUD-0608：bake 代数台账。bake 的 EXR 落盘是异步的，unbake 发生
        # 在"渲染完成→保存中→保存完成信号"窗口内时，迟到的完成信号不得
        # 复活已被解冻的层（且无 digest 记录的前缀校验会永久失效）。
        # 每次 bake 提交递增代数；unbake/新 bake 使旧代数失配，完成信号
        # 按代数守卫丢弃。
        self._bake_generation = {}  # {layer_id: int}
        self._source_epoch = 0  # 每次替换源图递增，参与前缀摘要

        self._clipboard_layer = None  # Instance-level clipboard for layer copy/paste
        self.viewport_size = (2048, 2048)
        self._render_request_counter = 0
        self._full_res_render_active = False
        
        # Async Renderer (Phase 4)
        self.renderer = None
        # Realtime scopes are GPU-only. Keep the legacy snapshot API disabled
        # unless an explicit diagnostic caller opts into CPU readback.
        self.scope_snapshots_enabled: bool = False

        # ColorOutput: cached Color pipeline result for Composite page
        self._color_output = ColorOutput()


        # CPU copies for analysis
        self.img_high = None # float32
        self.img_low = None  # float32 (or float16 if supported)

        self.temp_image = None
        
        self.history = HistoryManager()
        self.layers = [Layer("Layer 1")]
        self.active_layer_index = 0

        # 蒙版跟踪绑定存储（阶段 1）：随调色栈 trackings 键持久化，
        # 仅 UI 线程读写；跟踪工作线程只生产不可变结果。
        from src.features.color.tracking.tracking_store import TrackingStore
        from src.features.color.tracking.tracking_evaluator import TrackingEvaluator
        self.tracking_store = TrackingStore()
        # 渲染链通过求值器读取「基准几何 + 跟踪变换」，不直接访问 store。
        self.tracking_evaluator = TrackingEvaluator(self.tracking_store)
        # 由 UI 层注入：返回 (clip_id, source_frame)。未设置时渲染使用原始几何。
        self.tracking_context_provider = None
        self.current_lod = 0
        self.visible_rect = (0, 0, 1, 1) # x, y, w, h in scene coords
        
        # UI rendering resources (Main Thread)
        self.quad_vao = None
        self.quad_vbo = None
        self.passthrough_prog = None
        self.pipeline = None
        self.cached_thumbnail = None
        self.is_interacting = False
        self.interaction_type = "general"
        self.show_mask_mode = False
        # P1-COLOR-08: matte 预览模式 — highlight(图+灰底)/black(图+黑底)/matte(纯灰度)
        self.mask_preview_style = 'highlight'
        # P1-COLOR-03: 当前视频源帧号（grade keyframe 求值用）
        self.current_grade_frame = None
        
        # Split View / Wipe Comparison (Phase 6)
        self.split_view_active = False
        self.compare_mode = "split"  # F4.3
        # P1-COLOR-05: reference still 状态（split 左侧来源）与 A/B 全屏态
        self.reference_is_still = False
        self._ab_showing_reference = False
        # P1-COLOR-07: 参考 still 的 CPU 副本（scope 输入源用），BGR float 0..1
        self._reference_still_cpu = None
        self.split_position = 0.5 # 0.0 to 1.0 (UV Space)
        self.split_prog = None

        # RAW Parameters (Phase RAW)
        self.current_raw_params = {"enabled": False}
        self._last_applied_raw_params = {"enabled": False, "exposure": 0.0, "user_wb": [1.0, 1.0, 1.0, 1.0]}
        self.current_raw_metadata = {"camera_wb": [2.0, 1.0, 1.5, 1.0]}
        self.current_file_path = None
        self.is_current_image_srgb = False

        # Film Grain Parameters (Phase 7)
        self.film_grain_params = {
            "enabled": False,
            "intensity": 0.0,
            "size": 2.0, # Default size 2.0 (UI 2.0)
            "monochrome": True,
            "shadows": 0.0,
            "midtones": 1.0,
            "highlights": 0.0,
            "seed": 0.0,
            # P1-COLOR-13: animated=True 时 seed 随帧号确定推进（视频颗粒）
            "animated": False,
        }

        # SAM Session Tracking - Track the active SAM mask shape for continuous updates
        self._active_sam_shape_id: Optional[str] = None

        # Offline Media Handling - When original image is missing but baked cache exists
        self.is_offline_mode = False  # True when operating in offline cache mode
        self._offline_canvas_size = None  # (height, width) from baked EXR cache

        # Video Mode - When displaying video frames (skips Mipmap generation for faster playback)
        self._is_video_mode = False
        self._current_video_path = None

    def shutdown_worker_threads(self, timeout_ms: int = 3000) -> None:
        """停止后台工作线程（RAW / AI Mask），可安全重复调用。

        先向 worker 发出协作取消，再退出事件循环；正在执行的 native
        解码/推理会在返回后抑制过期结果。若 provider 仍未在超时内退出，
        保留 terminate() 兜底与进程级引用，避免 QThread 析构触发 Abort。
        窗口关闭与测试清理都必须走本方法。
        """
        import logging
        logger = logging.getLogger(__name__)
        for name in ("raw_worker_thread", "ai_worker_thread"):
            thread: Optional[QThread] = getattr(self, name, None)
            if thread is None:
                continue
            try:
                if not thread.isRunning():
                    continue
                worker_name = name.removesuffix("_thread")
                worker = getattr(self, worker_name, None)
                request_cancel = getattr(worker, "request_cancel", None)
                if callable(request_cancel):
                    # This method only flips a lock/event and is safe to call
                    # from the UI thread; the active worker checks it at its
                    # next native-operation boundary before emitting results.
                    request_cancel()
                thread.quit()
                if thread.wait(timeout_ms):
                    continue
                logger.warning(
                    "%s 未在 %dms 内退出，强制终止", thread.objectName(), timeout_ms
                )
                thread.terminate()
                if not thread.wait(timeout_ms):
                    # P1-THREAD-01：terminate 后仍存活——保留引用推迟销毁。
                    # 线程被 GC 析构时若仍在运行会直接 Abort 进程；把对象
                    # 挂到进程级引用，销毁责任转交给进程退出，状态已上报。
                    _TERMINATED_THREADS.append(thread)
                    logger.error(
                        "%s 强制终止后仍在运行，已推迟销毁（进程退出时回收）",
                        thread.objectName(),
                    )
            except RuntimeError:
                # 线程已销毁/未启动等 Qt 状态异常
                logger.debug("关闭 %s 时线程状态异常", name, exc_info=True)

    @property
    def color_output(self):
        """The ColorOutput instance for the Composite page."""
        return self._color_output

    def request_color_output(
        self, max_proxy_dim=None, update_masks=True, target="composite",
        quality_mode=None,
    ):
        """Request a ColorOutput render at proxy resolution.

        Renders the full Color pipeline to an offscreen texture, independent
        of Canvas viewport zoom/pan. Used by the Composite page to get a
        correct, full-image Color result.

        The result is delivered asynchronously via the renderer's
        color_output_ready signal.
        """
        if not self.renderer or self.img_full_original is None:
            return

        # P0-COL-04：与 render_pipeline 相同的前缀失效校验
        self._validate_baked_prefixes()

        if update_masks:
            self._update_mask_textures()

        # Build render params (same as render_pipeline)
        v_rect = (0, 0, 1.0, 1.0)
        if self.img_full_original is not None:
            h, w = self.img_full_original.shape[:2]
            v_rect = (0, 0, float(w), float(h))

        eff_visible_rect = v_rect
        if self.img_full_original is not None and self.img_high is not None:
            h_full, w_full = self.img_full_original.shape[:2]
            h_gpu, w_gpu = self.img_high.shape[:2]
            if w_full != w_gpu or h_full != h_gpu:
                scale_x = w_gpu / w_full
                scale_y = h_gpu / h_full
                vx, vy, vw, vh = v_rect
                eff_visible_rect = (vx * scale_x, vy * scale_y, vw * scale_x, vh * scale_y)

        layers_data = [_layer_render_data(layer) for layer in self.layers]

        # Build raw params (same logic as render_pipeline)
        curr = self.current_raw_params
        last_decode = self._last_applied_raw_params
        baseline_wb = self.current_raw_metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
        last_wb = last_decode.get('user_wb', baseline_wb)
        curr_wb = curr.get('user_wb', last_wb)

        gains = (
            float(curr_wb[0] / max(1e-4, last_wb[0])),
            float(curr_wb[1] / max(1e-4, last_wb[1])),
            float(curr_wb[2] / max(1e-4, last_wb[2]))
        )

        cs_map = {
            "sRGB": 0, "Rec.709": 1, "Rec.2020": 2, "Adobe RGB": 3, "DCI-P3": 4, "Display P3": 5,
            "ACEScg": 6, "ARRI Wide Gamut 3": 7, "Sony S-Gamut3": 8, "Panasonic V-Gamut": 9,
            "DaVinci Wide Gamut": 10, "REDWideGamutRGB": 11, "Canon Cinema Gamut": 12,
            "Fujifilm F-Gamut": 13, "BMD Film Gen5": 14, "CMYK Simulation": 15
        }
        gamma_map = {
            "Linear": 0, "sRGB": 1, "Rec.709": 2, "Gamma 2.2": 3, "Gamma 2.4": 4, "Gamma 2.6": 5,
            "Rec.2100 HLG": 6, "sLog3": 7, "ARRI LogC3": 8, "ACEScct": 9, "Panasonic V-Log": 10,
            "DaVinci Intermediate": 11, "RED Log3G10": 12, "Canon Log 2": 13, "Canon Log 3": 14,
            "Fujifilm F-Log": 15, "Fujifilm F-Log2": 16, "Apple Log": 17
        }

        is_raw_path = self.current_file_path and os.path.splitext(self.current_file_path)[1].lower() in self.RAW_EXTENSIONS
        is_effectively_srgb = self.is_current_image_srgb or (not is_raw_path)

        if is_effectively_srgb and not curr.get("enabled", False):
            raw_params = {
                'exposure': 0.0, 'gains': (1.0, 1.0, 1.0), 'color_space': -1,
                'gamma': 0, 'is_srgb_source': False, 'shadows': 0.0, 'highlights': 0.0,
                'contrast': 1.0, 'saturation': 1.0, 'lift': 0.0, 'gain': 1.0,
                'color_boost': 0.0, 'mid_detail': 0.0, 'sharpness': 0.0,
            }
        else:
            raw_params = {
                'exposure': float(curr.get('exposure', 0.0)),
                'gains': gains,
                'color_space': cs_map.get(curr.get('color_space', "sRGB"), 0),
                'gamma': gamma_map.get(curr.get('gamma', "Linear"), 0),
                'is_srgb_source': self.is_current_image_srgb,
                'shadows': float(curr.get('shadows', 0.0)),
                'highlights': float(curr.get('highlights', 0.0)),
                'contrast': float(curr.get('contrast', 1.0)),
                'saturation': float(curr.get('saturation', 100.0) / 100.0),
                'lift': float(curr.get('lift', 0.0)),
                'gain': float(curr.get('gain', 1.0)),
                'color_boost': float(curr.get('color_boost', 0.0)),
                'mid_detail': float(curr.get('mid_detail', 0.0)),
                'sharpness': float(curr.get('sharpness', 0.0)),
            }

        render_params = {
            'render_request_id': self._next_render_request_id(),
            'visible_rect': eff_visible_rect,
            'grain_visible_rect': v_rect,
            'current_lod': 0,
            'layers': layers_data,
                'show_mask': False,
                'mask_preview_style': self.mask_preview_style,
                'active_layer_index': self.active_layer_index,
                'film_grain': self._effective_film_grain_params(),
                'raw_preview': raw_params,
            }
        if max_proxy_dim is not None:
            render_params['max_proxy_dim'] = int(max_proxy_dim)
        render_params['color_output_target'] = str(target or "composite")
        if quality_mode in ('preview', 'paused', 'export'):
            render_params['video_denoise_quality_mode'] = quality_mode

        self.renderer.submit(RenderCommand.RENDER_COLOR_OUTPUT, render_params)

    def enter_offline_mode(self, layers: List['Layer']) -> bool:
        """
        Enter offline mode when original image is missing but baked cache exists.

        This creates a black placeholder canvas using the dimensions from the
        bottom-most baked layer's EXR cache, allowing the user to continue
        working with cached data.

        Args:
            layers: List of Layer objects from the loaded project

        Returns:
            True if offline mode was successfully entered, False otherwise
        """
        from src.shared.io.file_io import FileIO

        # Find the bottom-most layer with a baked cache (lowest index in model)
        baked_layer = None
        for layer in layers:
            if getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None):
                baked_layer = layer
                break  # Bottom-most (first in list)

        if baked_layer is None:
            return False

        # Load the baked EXR to get dimensions
        baked_array = FileIO.load_baked_layer(baked_layer.baked_file_name)
        if baked_array is None:
            logger.error(f": Failed to load baked layer '{baked_layer.baked_file_name}'")
            return False

        # Get dimensions from the baked EXR
        height, width = baked_array.shape[:2]
        self._offline_canvas_size = (height, width)

        # Create a pure black placeholder canvas (BGR uint8 format)
        black_canvas = np.zeros((height, width, 3), dtype=np.uint8)

        # Set the placeholder as the "original" image for rendering pipeline
        self.img_full_original = black_canvas
        self.img_source_full = black_canvas.copy()

        # Create preview resolutions
        low_img = FileIO._resize_to_fit(black_canvas, FileIO.LOW_RES_SIZE)
        high_img = FileIO._resize_to_fit(black_canvas, FileIO.HIGH_RES_SIZE)

        self.img_high = high_img.astype(np.float32) / 255.0
        self.img_low = low_img.astype(np.float32) / 255.0

        # Mark as offline mode
        self.is_offline_mode = True
        self.is_current_image_srgb = True
        self.current_file_path = None  # No valid source path

        # Reset crop and RAW params
        self.crop_params = {
            "rect": (0.0, 0.0, 1.0, 1.0),
            "rotation": 0.0,
        }
        self._flip_h = False
        self._flip_v = False
        self.current_raw_params = {"enabled": False}
        self._last_applied_raw_params = {"enabled": False}

        # Upload black canvas to GPU
        img_for_upload = np.ascontiguousarray(self.img_high, dtype=np.float32)
        if self.renderer:
            self.renderer.submit(RenderCommand.UPLOAD_IMAGE, {
                'image': img_for_upload,
                'original_size': (width, height)
            })

        # Upload baked textures for all baked layers
        for layer in layers:
            if getattr(layer, 'is_baked', False) and getattr(layer, 'baked_file_name', None):
                baked_data = FileIO.load_baked_layer(layer.baked_file_name)
                if baked_data is not None and self.renderer:
                    self.renderer.submit(RenderCommand.UPLOAD_BAKED_TEXTURE, {
                        'layer_id': layer.id,
                        'image_array': baked_data
                    })

        logger.info(f"offline mode with canvas size: {width}x{height}")
        return True

    def exit_offline_mode(self):
        """
        Exit offline mode when a valid source image is loaded.

        Resets offline mode flags and clears the placeholder canvas.
        """
        self.is_offline_mode = False
        self._offline_canvas_size = None

    def initialize_async_renderer(self, main_context):
        """Initializes the background render thread with a shared context."""
        if self.renderer is None:
            self.renderer = AsyncRenderer(main_context)
            self.renderer.frame_ready.connect(self.frame_ready.emit)
            self.renderer.scope_snapshot_ready.connect(self.scope_snapshot_ready.emit)
            # GPU scope: forward the descriptor frame only; no pixel data.
            if hasattr(self.renderer, 'gpu_scope_ready'):
                self.renderer.gpu_scope_ready.connect(self.gpu_scope_ready.emit)
            self.renderer.layer_baked_ready.connect(self._on_layer_baked)
            if hasattr(self.renderer, 'full_resolution_ready'):
                self.renderer.full_resolution_ready.connect(self._on_full_resolution_ready)
            if hasattr(self.renderer, 'full_resolution_failed'):
                self.renderer.full_resolution_failed.connect(self._on_full_resolution_failed)
            if hasattr(self.renderer, 'full_resolution_progress'):
                self.renderer.full_resolution_progress.connect(
                    self._on_full_resolution_progress
                )
            self.renderer.color_output_ready.connect(self._on_color_output_ready)
            if hasattr(self.renderer, 'monitor_color_output_ready'):
                self.renderer.monitor_color_output_ready.connect(self._on_monitor_color_output_ready)
            # Connect async EXR save completion signal
            self._baked_save_finished.connect(self._on_baked_save_finished)
            self.renderer.start()

    def request_scope_snapshot(self, max_dim: int = 512) -> None:
        """Queue a reduced front-texture readback on the render thread.

        .. deprecated::
            Legacy CPU-readback scope path (``fbo.read()``).  The realtime
            scope is now :meth:`request_gpu_scope`; this method is kept only
            as a paused fallback / reference oracle and is gated off during
            playback by the controller.  Do not call from playback paths.
        """
        if not self.scope_snapshots_enabled or self.renderer is None:
            return
        self.renderer.submit(RenderCommand.RENDER_SCOPE, {'max_dim': max(1, int(max_dim))})

    def request_gpu_scope(self, demand) -> None:
        """Queue a native GPU scope pass for ``ScopeDemand``.

        The renderer analyses the latest published front texture entirely on
        the GPU and emits ``gpu_scope_ready`` with a ``ScopeGpuFrame``
        descriptor.  No pixels are read back to the CPU.  Safe to call during
        playback — the GPU_SCOPE lane is the lowest priority and never blocks
        the video lane.
        """
        if self.renderer is None:
            return
        self.renderer.submit(RenderCommand.RENDER_GPU_SCOPE, demand)

    @property
    def scope_source_revision(self) -> int:
        """The renderer-side front-texture revision last seen by the engine.

        Used by the controller to stamp ``ScopeDemand.source_revision`` so
        stale scope frames can be rejected after seek / clip switch.
        """
        if self.renderer is None:
            return 0
        return int(getattr(self.renderer, '_scope_source_revision', 0))

    def discard_pending_gpu_scope(self) -> None:
        """Drop any queued GPU scope demand (seek / clip switch / hide)."""
        if self.renderer is not None and hasattr(self.renderer, 'discard_pending_gpu_scope'):
            self.renderer.discard_pending_gpu_scope()

    def set_image(self, full, high, low, is_new_file=True, path=None, is_video=False, video_path=None):
        """Sets the source images. Uploads to GPU asynchronously.

        Args:
            full: Full resolution image
            high: High resolution preview
            low: Low resolution preview
            is_new_file: Whether this is a new file (resets params)
            path: File path
            is_video: Whether this is a video frame (skips Mipmap generation)
            video_path: Original video file path
        """
        if full is None: return

        # P0-COL-04：源图替换使所有 bake 前缀过期（参与前缀摘要）
        self._source_epoch += 1

        # Store video mode flag
        self._is_video_mode = is_video
        if is_video and video_path:
            self._current_video_path = video_path
            self.current_file_path = video_path

        # 对于视频帧，始终更新 img_full_original（即使 is_new_file=False）
        # 因为视频帧切换时需要更新当前显示的帧
        if is_video:
            self.img_full_original = full.copy()
            self.img_source_full = full.copy()

        if is_new_file:
            self.img_source_full = full.copy()
            self.img_full_original = full.copy()
            self._flip_h = False
            self._flip_v = False
            self.current_file_path = video_path if is_video and video_path else path
            self._color_output.mark_dirty()  # New image — ColorOutput stale
            # Clear AI plugin result cache from previous image to free memory
            self.ai_plugin_result_cache.clear()
            self.crop_params = {
                "rect": (0.0, 0.0, 1.0, 1.0),
                "rotation": 0.0,
            }

            # Detect if it's a RAW file
            is_raw = path and os.path.splitext(path)[1].lower() in self.RAW_EXTENSIONS
            # M1-F5：线性浮点静图（.toos HALF，transfer=linear）不是显示域
            # sRGB——不再 sRGB 直通 bypass（直通把线性值当显示编码值直接
            # 上屏 → 偏暗），按 linear 输入参与 RAW 变换并以 sRGB 输出编码。
            # 探测失败按旧口径（sRGB）处理，不阻塞加载。
            is_linear_source = (path and not is_raw
                                and self._path_transfer_is_linear(str(path)))
            self.is_current_image_srgb = (not is_raw) and (not is_linear_source)

            # Reset RAW parameters to defaults.
            # For sRGB images, we default gamma to "Linear" (0) to avoid double-gamma brightening.
            # 线性源与 RAW 同口径：输出经 sRGB 再编码（linear → 显示域）。
            default_gamma = "sRGB" if (is_raw or is_linear_source) else "Linear"
            
            self.current_raw_params = {
                "enabled": False,
                "color_space": "sRGB",
                "gamma": default_gamma
            }
            self._last_applied_raw_params = {
                "enabled": False, 
                "exposure": 0.0, 
                "user_wb": [1.0, 1.0, 1.0, 1.0],
                "color_space": "sRGB",
                "gamma": default_gamma
            }
            self.current_raw_metadata = {"camera_wb": [2.0, 1.0, 1.5, 1.0]}
            
            # Extract RAW metadata for lock-free preview calculation
            if is_raw:
                from src.shared.io.file_io import FileIO
                with FileIO._raw_lock:
                    if FileIO._current_raw_path == path and FileIO._current_raw_obj:
                        self.current_raw_metadata["camera_wb"] = FileIO._get_normalized_camera_wb(FileIO._current_raw_obj)
                    else:
                        try:
                            import rawpy
                            with rawpy.imread(path) as raw:
                                self.current_raw_metadata["camera_wb"] = FileIO._get_normalized_camera_wb(raw)
                        except Exception:
                            self.current_raw_metadata["camera_wb"] = [2.0, 1.0, 1.5, 1.0]

                # ===== 正确的修改方式 =====
                # 同步底层的初次解码状态。将真实的相机白平衡作为基准线。
                # 加 .copy() 或 list() 防止引用污染
                self._last_applied_raw_params['user_wb'] = list(self.current_raw_metadata["camera_wb"])
            else:
                # For non-RAW images, use neutral white balance as baseline
                self.current_raw_metadata["camera_wb"] = [1.0, 1.0, 1.0, 1.0]
                self._last_applied_raw_params['user_wb'] = [1.0, 1.0, 1.0, 1.0]
        
        # When image resolution or content changes, invalidate all mask textures
        for layer in self.layers:
            layer.mask_dirty = True

        # Helper methods for robust normalization and type conversion
        def normalize_to_float32(img):
            if img.dtype == np.uint16:
                return img.astype(np.float32) / 65535.0
            elif img.dtype == np.uint8:
                return img.astype(np.float32) / 255.0
            return img.astype(np.float32) # Assume already 0-1 or HDR

        def convert_to_uint8(img):
            if img.dtype == np.uint8: return img
            if img.dtype == np.uint16: return (img / 256).astype(np.uint8)
            return (np.clip(img, 0, 1) * 255).astype(np.uint8)

        self.img_high = normalize_to_float32(high)
        self.img_low = normalize_to_float32(low)
        
        # 统一使用 img_high 推送给 GPU，避免 FBO 与纹理尺寸错位
        img_for_upload = np.ascontiguousarray(self.img_high, dtype=np.float32)

        if self.renderer:
            # Pass original full resolution size to ensure consistent effect scaling
            orig_h, orig_w = self.img_full_original.shape[:2]
            self.renderer.submit(RenderCommand.UPLOAD_IMAGE, {
                'image': img_for_upload,
                'original_size': (orig_w, orig_h),
                'is_video': is_video,  # FIX: 传递 is_video 标志，防止 _do_upload 清除视频纹理状态
            })
        else:
            self._upload_to_gpu(img_for_upload, high)
            self._generate_pyramid(img_for_upload)

        if is_new_file:
            # 参考图也必须与 GPU 主纹理尺寸保持一致
            self.set_reference_image(self.img_high)

        self.temp_image = None
        self.render_pipeline()

    def set_interaction_mode(self, is_active: bool, itype: str = "general") -> None:
        """进入/退出交互模式（滑杆拖动、裁剪、蒙版绘制等）。

        退出交互时把所有层蒙版置脏，随后的渲染按静止基准（1024）重
        栅格化。交互中的渲染走轻量 merge lane：general 调色用
        'grading'，其余类型用 'full'。
        """
        if self.is_interacting and not is_active:
            for layer in self.layers:
                layer.mask_dirty = True
                
        self.is_interacting = is_active
        self.interaction_type = itype
        # General adjustment controls already submit an interactive preview on
        # their first value change.  Keep this state transition in the same
        # merge lane so starting a wheel/slider drag cannot enqueue an
        # unmergeable full render ahead of the live updates.
        render_reason = 'grading' if is_active and itype == 'general' else 'full'
        self.render_pipeline(render_reason=render_reason)

    def get_display_image(self) -> Optional[np.ndarray]:
        """Returns the full resolution image to anchor the UI coordinate system."""
        # If we are in crop mode (interacting), we might want to show the source image
        if self.is_interacting and self.interaction_type == "crop":
            return self.img_source_full
        return self.img_full_original

    def get_source_bake_state(self) -> Optional[Dict[str, Any]]:
        """Return source bake metadata for project serialization."""
        if self.source_bake_file_name:
            return {
                "source_bake_file_name": self.source_bake_file_name,
                "source_bake_type": self.source_bake_type
            }
        return None

    def set_source_bake_state(self, state: Optional[Dict[str, Any]]) -> None:
        """Restore source bake metadata from project data."""
        if state and state.get("source_bake_file_name"):
            self.source_bake_file_name = state["source_bake_file_name"]
            self.source_bake_type = state.get("source_bake_type")
        else:
            self.source_bake_file_name = None
            self.source_bake_type = None

    def get_video_denoise_state(self) -> Optional[dict]:
        """Return video denoise params for project serialization (None if off)."""
        params = getattr(self, 'video_denoise_params', None)
        if params is not None and params.is_active():
            return params.to_dict()
        return None

    def set_video_denoise_state(self, state: Optional[dict]) -> None:
        """Restore video denoise params from project data and push to renderer."""
        from src.shared.video.denoise.params import VideoDenoiseParams

        params = VideoDenoiseParams.from_dict(state) if state else None
        self.video_denoise_params = params
        renderer = getattr(self, 'renderer', None)
        if params is not None and renderer is not None and hasattr(
            renderer, 'set_video_denoise_params'
        ):
            try:
                renderer.set_video_denoise_params(params)
            except Exception:
                logger.debug('set_video_denoise_params failed', exc_info=True)

    def get_render_result(self) -> Optional[np.ndarray]:
        """Return the latest renderable image as BGR uint8.

        优先级：cached_thumbnail（scope 回读缓存）→ temp_image（上一
        次全分辨率/管线结果，float32 转 uint8）→ 显示锚点图像。
        """
        if self.cached_thumbnail is not None:
            return self.cached_thumbnail
        if self.temp_image is not None:
            if self.temp_image.dtype == np.float32:
                return (np.clip(self.temp_image, 0, 1) * 255).astype(np.uint8)
            return self.temp_image
        return self.get_display_image()

    def get_current_image(self, force_full_res: bool = False) -> Optional[np.ndarray]:
        """Return the current working image (temp_image if rendered, else full).

        ``force_full_res=True`` 时改为触发并返回一次全分辨率渲染。
        """
        if force_full_res:
            return self.render_full_resolution()
        return self.temp_image if self.temp_image is not None else self.img_full_original

    def request_full_resolution(
        self, request_id: Optional[str] = None
    ) -> Optional[str]:
        """Submit a cancellable full-resolution render and return its request id.

        This is the asynchronous counterpart to :meth:`render_full_resolution`.
        The existing synchronous export/apply API remains available, while UI
        callers that can stay asynchronous may subscribe to
        ``full_resolution_progress`` and cancel with
        :meth:`cancel_full_resolution`.
        """
        if self.img_full_original is None:
            return None
        if bool(self.__dict__.get('_full_res_render_active', False)):
            logger.warning("Ignoring re-entrant full-resolution render request")
            return None
        renderer = self.__dict__.get('renderer')
        if renderer is None:
            logger.warning(
                "Cannot submit asynchronous full-resolution render without renderer"
            )
            return None

        request_id = str(request_id or self._next_render_request_id())
        self._full_res_render_active = True
        self._full_res_pending_request_id = request_id
        self.temp_image = None
        try:
            submitted_id = self.render_pipeline(
                is_full_res=True,
                render_request_id=request_id,
            )
        except Exception:
            self._full_res_render_active = False
            self._full_res_pending_request_id = None
            raise
        if not submitted_id:
            self._full_res_render_active = False
            self._full_res_pending_request_id = None
            return None
        # A custom render_pipeline implementation may return a different id;
        # keep the returned value authoritative for callers and signal matching.
        if submitted_id != request_id:
            self._full_res_pending_request_id = str(submitted_id)
        return str(submitted_id)

    def cancel_full_resolution(self, request_id: Optional[str] = None) -> bool:
        """Request cooperative cancellation of one full-resolution render."""
        renderer = self.__dict__.get('renderer')
        rid = str(
            request_id
            or self.__dict__.get('_full_res_pending_request_id')
            or ''
        ).strip()
        if renderer is None or not rid:
            return False
        cancel = getattr(renderer, 'cancel_full_resolution', None)
        if not callable(cancel):
            return False
        return bool(cancel(rid))

    def render_full_resolution(self) -> Optional[np.ndarray]:
        """同步渲染全分辨率结果并返回 BGR ndarray（失败/超时为 None）。

        生产路径：提交 RENDER_FULL_RESOLUTION 给渲染线程，在嵌套
        QEventLoop 中等待 full_resolution_ready / failed（120s 超时
        兜底）——保留导出/脚本用的同步 API 语义，同时不在 UI 线程执行
        任何全分辨率 GL 调用。无 AsyncRenderer 的 headless 场景退化为
        本地 legacy 渲染路径。失败时绝不返回上一次的过期导出结果。
        """
        if self.img_full_original is None:
            return None
        if bool(self.__dict__.get("_full_res_render_active", False)):
            logger.warning("Ignoring re-entrant full-resolution render request")
            return None
        # A failed full-resolution pass must never expose the previous export.
        # Clear the last result before submitting the render so callers cannot
        # mistake stale pixels for a successful render.
        try:
            # Production path: submit a critical command and wait in a nested
            # Qt event loop for the renderer-thread readback.  This preserves
            # the historical synchronous API used by export/apply/scripting,
            # while ensuring no full-resolution OpenGL call runs on the UI
            # thread.  The timeout is a safety net for context/device loss.
            # Lightweight state tests may construct ImageEngine via __new__;
            # read through __dict__ so QObject's uninitialised property access
            # cannot turn a harmless no-renderer case into a RuntimeError.
            renderer = self.__dict__.get('renderer')
            if renderer is not None:
                # Submit through the request API so timeout/caller cancellation
                # uses the same request registry as asynchronous consumers.
                request_id = self._next_render_request_id()
                result = {'image': None, 'reason': 'timeout'}
                loop = QEventLoop()
                timeout = QTimer()
                timeout.setSingleShot(True)

                def on_ready(rid, image):
                    if rid != request_id:
                        return
                    result['image'] = image
                    result['reason'] = None
                    loop.quit()

                def on_failed(rid, reason):
                    if rid != request_id:
                        return
                    result['reason'] = reason
                    loop.quit()

                self.full_resolution_ready.connect(on_ready)
                self.full_resolution_failed.connect(on_failed)
                try:
                    # Connect before submission.  Small renders can complete
                    # quickly enough that attaching listeners afterwards would
                    # otherwise leave the nested loop waiting for a signal it
                    # already missed.
                    submitted_id = self.request_full_resolution(request_id)
                    if not submitted_id:
                        return None
                    request_id = submitted_id
                    timeout.timeout.connect(loop.quit)
                    timeout.start(120000)
                    loop.exec()
                finally:
                    if result['image'] is None and result['reason'] == 'timeout':
                        self.cancel_full_resolution(request_id)
                    timeout.stop()
                    try:
                        self.full_resolution_ready.disconnect(on_ready)
                    except (TypeError, RuntimeError):
                        pass
                    try:
                        self.full_resolution_failed.disconnect(on_failed)
                    except (TypeError, RuntimeError):
                        pass
                if result['image'] is not None:
                    self.temp_image = result['image']
                elif result['reason']:
                    logger.warning(
                        "Full-resolution render did not produce output "
                        "(request_id=%s, reason=%s)", request_id, result['reason']
                    )
                return self.temp_image.copy() if self.temp_image is not None else None

            # Compatibility fallback for headless/legacy callers that have not
            # created an AsyncRenderer yet.  This path is not used by the
            # application once the shared context is initialized.
            self._full_res_render_active = True
            self.temp_image = None
            self.render_pipeline(is_full_res=True)
            return self.temp_image.copy() if self.temp_image is not None else None
        finally:
            self._full_res_render_active = False

    def apply_changes(self) -> None:
        """把当前调色结果烘焙进源图（历史压栈 + 全分辨率渲染 + 重置缓存）。"""
        full_res_result = self.render_full_resolution()
        if full_res_result is not None:
            # 保存完整状态（图像 + 图层）
            self.history.push_state(self.img_full_original, self.layers, self.active_layer_index)
            from src.shared.io.file_io import FileIO
            u8 = full_res_result if full_res_result.dtype == np.uint8 else np.clip(full_res_result * 255, 0, 255).astype(np.uint8)
            low = FileIO._resize_to_fit(u8, FileIO.LOW_RES_SIZE)
            high = FileIO._resize_to_fit(u8, FileIO.HIGH_RES_SIZE)
            self._commit_baked_source(u8, high, low)

    def _commit_baked_source(self, full: np.ndarray, high: np.ndarray, low: np.ndarray) -> None:
        """AUD-0610：把烘焙结果提交为当前会话源图的独立内部路径。

        旧实现借道 ``set_image(is_new_file=True)``（新文件语义），连带
        触发 ``current_file_path=None``、清空 ``ai_plugin_result_cache``、
        重置 RAW 参数与 sRGB 探测口径等一揽子副作用——烘焙是会话内操
        作，不是加载新文件。几何基准（img_source_full / _flip_* /
        crop_params）与旧口径一致地回到恒等：烘焙像素已含当前翻转与
        裁剪。
        """
        self.img_full_original = full.copy()
        self.img_source_full = full.copy()
        self._flip_h = False
        self._flip_v = False
        self.crop_params = {"rect": (0.0, 0.0, 1.0, 1.0), "rotation": 0.0}
        self._color_output.mark_dirty()  # 源像素已替换——ColorOutput 过期
        # is_new_file=False：保留文件引用 / AI 缓存 / RAW 与色彩管理口径，
        # 只走源图替换公共路径（_source_epoch+1、蒙版置脏、上传 GPU、
        # 重渲染——见 set_image）。
        self.set_image(full, high, low, is_new_file=False)
        # 参考图随源更新（split wipe 两侧同源），与旧"新文件"口径一致
        self.set_reference_image(self.img_high)

    def discard_changes(self) -> None:
        """丢弃活动图层的全部调色参数并清除其残留 GPU 辅助纹理。

        reset_params() 只重置模型；渲染线程的 layer_textures 仍持有旧
        纹理会继续显示过期调色（C-P0-05），故同时提交 CLEAR_AUX_TEX。
        """
        layer = self.get_active_layer()
        if layer:
            layer.reset_params()
            # C-P0-05: reset_params() clears curves/LUT/warper/qualifier/mask in
            # the model, but the renderer's layer_textures[layer.id] still held
            # the old GL textures and the bind path kept using them. Drop the
            # stale GPU slot so the next render binds the identity defaults,
            # matching the reset state — rather than leaking/showing stale grade.
            if self.renderer:
                self.renderer.submit(RenderCommand.CLEAR_AUX_TEX, {
                    'layer_id': layer.id
                })
        self.render_pipeline()

    def reset_all_layers(self) -> None:
        """丢弃所有图层的调色参数并逐层清除残留 GPU 辅助纹理（C-P0-05）。"""
        for layer in self.layers:
            layer.reset_params()
            # C-P0-05: see discard_changes() — clear every layer's stale AUX slot.
            if self.renderer:
                self.renderer.submit(RenderCommand.CLEAR_AUX_TEX, {
                    'layer_id': layer.id
                })
        self.render_pipeline()

    def get_active_layer(self) -> Optional[Layer]:
        """Return the active Layer, or None when the index is out of range."""
        if 0 <= self.active_layer_index < len(self.layers):
            return self.layers[self.active_layer_index]
        return None

    def add_layer(self) -> Layer:
        """Append a new Layer on top, make it active, and trigger a full render."""
        new_layer = Layer(f"Layer {len(self.layers) + 1}")
        self.layers.append(new_layer)
        self.active_layer_index = len(self.layers) - 1
        self.render_pipeline()
        return new_layer

    def remove_layer(self, index: int) -> None:
        """Remove a layer (never the last one) and drop its baked/GPU resources.

        删除该层的 baked EXR 缓存文件（磁盘 GC），并通知渲染线程释放
        以 layer.id 为键的全部纹理槽位。
        """
        if len(self.layers) > 1:
            layer = self.layers[index]
            layer_id = layer.id  # Use UUID string (matches keys in baked_textures/ai_plugin_textures/layer_textures/matte_cache)

            # Disk Garbage Collection: Delete baked EXR file if exists
            if layer.baked_file_name:
                from src.shared.io.file_io import FileIO
                try:
                    FileIO.delete_baked_layer(layer.baked_file_name)
                except OSError as e:
                    # File might be in use or already deleted - log but don't crash
                    logger.error(f": Failed to delete baked layer file '{layer.baked_file_name}': {e}")

            self.layers.pop(index)
            self.active_layer_index = max(0, min(self.active_layer_index, len(self.layers) - 1))
            if self.renderer:
                self.renderer.submit(RenderCommand.REMOVE_LAYER, layer_id)
            self.render_pipeline()

    def set_active_layer(self, index: int) -> None:
        """Set the active layer index when in range (no re-render)."""
        if 0 <= index < len(self.layers):
            self.active_layer_index = index

    # Layer Copy/Paste Support

    def copy_layer(self, index: int):
        """复制指定图层的调色参数到剪贴板。
        
        Args:
            index: 要复制的图层索引
        """
        if 0 <= index < len(self.layers):
            source_layer = self.layers[index]
            # 创建一个新的 Layer 对象作为剪贴板，只复制参数
            clipboard_layer = Layer(f"Clipboard_{source_layer.name}")
            clipboard_layer.copy_params_from(source_layer)
            # 同时复制基础属性（名称、可见性、透明度、混合模式）
            clipboard_layer.name = source_layer.name
            clipboard_layer.visible = source_layer.visible
            clipboard_layer.opacity = source_layer.opacity
            clipboard_layer.blend_mode = source_layer.blend_mode
            self._clipboard_layer = clipboard_layer
            logger.info(f"layer '{source_layer.name}' parameters to clipboard")
        else:
            logger.info(f"copy layer: invalid index {index}")

    def paste_layer(self, target_index: int):
        """将剪贴板中的图层参数粘贴到指定图层。
        
        Args:
            target_index: 要粘贴到的目标图层索引
            
        Returns:
            bool: 是否成功粘贴
        """
        if self._clipboard_layer is None:
            logger.info("paste: clipboard is empty")
            return False

        if not (0 <= target_index < len(self.layers)):
            logger.info(f"paste: invalid target index {target_index}")
            return False

        target_layer = self.layers[target_index]
        target_layer.copy_params_from(self._clipboard_layer)

        # 复制基础属性
        target_layer.opacity = self._clipboard_layer.opacity
        target_layer.blend_mode = self._clipboard_layer.blend_mode
        
        logger.info(f"layer parameters to '{target_layer.name}'")
        self.render_pipeline()
        return True

    def duplicate_layer(self, index: int):
        """复制并创建一个新的图层（完全复制，包括名称加后缀）。
        
        Args:
            index: 要复制的图层索引
            
        Returns:
            Layer: 新创建的图层，或 None 如果失败
        """
        if 0 <= index < len(self.layers):
            source_layer = self.layers[index]
            new_layer = Layer(f"{source_layer.name} Copy")
            new_layer.copy_params_from(source_layer)
            # 复制基础属性
            new_layer.visible = source_layer.visible
            new_layer.opacity = source_layer.opacity
            new_layer.blend_mode = source_layer.blend_mode
            self.layers.append(new_layer)
            self.active_layer_index = len(self.layers) - 1
            logger.info(f"layer '{source_layer.name}' as '{new_layer.name}'")
            self.render_pipeline()
            return new_layer
        else:
            logger.info(f"duplicate layer: invalid index {index}")
            return None

    def apply_lut(self, path: Optional[str]) -> bool:
        """Attach a 3D LUT file to the active layer, upload it, and re-render.

        Returns:
            True when a layer accepted the LUT, False when there is no
            active layer.
        """
        layer = self.get_active_layer()
        if layer:
            layer.active_lut_path = path
            if self.renderer:
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'lut3d', 'path': path
                })
            self.render_pipeline()
            return True
        return False

    def set_lut_intensity(self, intensity: float) -> None:
        """Set the active layer's 3D LUT mix intensity and re-render."""
        layer = self.get_active_layer()
        if layer:
            layer.lut_intensity = intensity
            self.render_pipeline()

    def set_layer_blend_mode(self, mode: str) -> None:
        """Set the active layer's blend mode and re-render."""
        layer = self.get_active_layer()
        if layer:
            layer.blend_mode = mode
            self.render_pipeline()

    def set_layer_opacity(self, opacity: float) -> None:
        """Set the active layer's opacity (0-1) and re-render."""
        layer = self.get_active_layer()
        if layer:
            layer.opacity = opacity
            self.render_pipeline()

    def set_mask_preview_mode(self, enabled: bool, style: Optional[str] = None) -> None:
        """开启/关闭 matte 预览，可同时切换预览模式。

        style: 'highlight' | 'black' | 'matte'；None 表示保持当前模式。
        """
        self.show_mask_mode = enabled
        if style is not None:
            self.mask_preview_style = str(style)
        self.render_pipeline()

    def set_split_view_mode(self, enabled: bool) -> None:
        """Toggle split-view (wipe) comparison and force a viewport refresh."""
        self.split_view_active = enabled
        self.viewport_size = (0, 0) # Force viewport update
        self.render_pipeline()

    def set_compare_mode(self, mode: str) -> None:
        """F4.3: 对比模式——"split"（wipe，默认）/ "difference"（全屏
        |current - reference|，黑=一致）。差异模式需要 wipe 参考存在。"""
        mode = "difference" if mode == "difference" else "split"
        if mode == "difference" and not self.split_view_active:
            self.split_view_active = True  # 差异模式复用参考纹理通路
        self.compare_mode = mode
        self.viewport_size = (0, 0)
        self.render_pipeline()

    def set_split_position(self, pos: float) -> None:
        """Set the wipe line position in UV space [0, 1].

        拖回中间位置即退出 A/B 全屏态，回到 wipe 对比。
        """
        self.split_position = max(0.0, min(1.0, pos))
        # 用户拖回中间分割位置 = 回到 wipe 对比，退出 A/B 全屏态
        if 0.0 < self.split_position < 1.0:
            self._ab_showing_reference = False
        self.render_pipeline()

    def set_reference_image(self, image: Optional[np.ndarray]) -> None:
        """Normalize an image to float32 BGR and upload it as the wipe reference.

        参考纹理尺寸必须与 GPU 主纹理（img_high）一致。
        """
        if image is None or self.renderer is None:
            return

        if image.dtype == np.uint8:
            img_for_upload = image.astype(np.float32) / 255.0
        elif image.dtype == np.uint16:
            img_for_upload = image.astype(np.float32) / 65535.0
        else:
            img_for_upload = image.astype(np.float32)
            
        img_for_upload = np.ascontiguousarray(img_for_upload, dtype=np.float32)
        self.renderer.submit(RenderCommand.UPLOAD_REFERENCE, img_for_upload)

    # ------------------------------------------------------------------
    # P1-COLOR-05: A/B compare & reference wipe monitoring
    # ------------------------------------------------------------------
    def capture_reference_still(self) -> bool:
        """抓取当前调色结果为参考图，并自动开启 wipe 对比。

        参考 wipe 的左侧从此显示该静帧（get_original_texture_id 优先
        返回 reference 纹理）。纯显示层操作，不改工程状态。
        """
        img = self.grab_viewport_image()
        if img is None:
            return False
        return self.load_reference_still(img)

    def load_reference_still(self, image) -> bool:
        """把外部图像（如 Gallery 静帧存档）加载为参考 still。

        与 :meth:`capture_reference_still` 共享记账：上传 reference
        纹理、维护 CPU 副本（scope reference 输入源用）、自动开 wipe。
        纯显示层操作，不改工程状态。
        """
        if image is None or getattr(image, "size", 0) == 0:
            return False
        self.set_reference_image(image)
        # CPU 副本供 scope reference 输入源使用
        if image.dtype == np.uint8:
            self._reference_still_cpu = (image.astype(np.float32) / 255.0)
        elif image.dtype == np.uint16:
            self._reference_still_cpu = (image.astype(np.float32) / 65535.0)
        else:
            self._reference_still_cpu = image.astype(np.float32)
        self.reference_is_still = True
        self._ab_showing_reference = False
        if not self.split_view_active:
            self.set_split_view_mode(True)
        else:
            self.render_pipeline()
        return True

    def clear_reference_still(self) -> None:
        """清除参考静帧；split 左侧回退为未调色原图。"""
        if self.renderer:
            self.renderer.submit(RenderCommand.CLEAR_REFERENCE, None)
        self.reference_is_still = False
        self._ab_showing_reference = False
        self._reference_still_cpu = None
        self.render_pipeline()

    def get_reference_still_cpu(self):
        """参考 still 的 CPU 副本（BGR float 0..1），无参考时 None。"""
        return self._reference_still_cpu

    def toggle_ab_view(self) -> bool:
        """A/B 全屏切换：参考(或原图) ↔ 当前调色结果。

        实现 = split 位置在 1.0（全显示 B 侧）与 0.0（全显示当前）
        之间翻转；拖动分割线到中间即回到 wipe。返回翻转后是否显示 B。
        """
        if not self.split_view_active:
            self.set_split_view_mode(True)
        self._ab_showing_reference = not self._ab_showing_reference
        # 直接写 split_position，避免 set_split_position 重置 A/B 状态
        self.split_position = 1.0 if self._ab_showing_reference else 0.0
        self.render_pipeline()
        return self._ab_showing_reference

    def reset_lut(self) -> None:
        """Detach the 3D LUT from the active layer and re-render."""
        layer = self.get_active_layer()
        if layer:
            layer.active_lut_path = None
            self.render_pipeline()

    def decompile_lut(self, path: str) -> Optional[dict]:
        """Uses The Decompiler to reverse engineer a .cube file into GradeState."""
        from src.features.color.core.the_decompiler import Decompiler
        layer = self.get_active_layer()
        if not layer: return None
        
        decompiler = Decompiler()
        try:
            results = decompiler.decompile(path)
            # results contains: 'grading', 'curves', 'warper'
            
            # Apply to layer
            layer.grading_params.update(results['grading'])
            layer.curves_lut = results['curves']
            layer.warper_params = results['warper']
            layer.active_lut_path = None # Disable the 3D LUT as it's now decompiled
            
            # Sync with GPU
            self.sync_all_resources()
            self.render_pipeline()
            return results
        except Exception as e:
            logger.error(f"Error: {e}")
            return None

    def sync_all_resources(self) -> None:
        """Re-upload every layer's curves / HSL curves / 3D LUT / warper textures to the GPU.

        项目加载或参数整体替换后调用，恢复渲染线程侧的辅助纹理状态。
        """
        if not self.renderer: return
        for layer in self.layers:
            if layer.curves_lut is not None:
                lut = layer.curves_lut
                if lut.dtype != np.float32:
                    lut = lut.astype(np.float32)
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'curves', 'data': lut.tobytes()
                })
            if layer.hsl_curve_lut is not None:
                # 工程重载后 HSL 曲线纹理必须同样恢复：缺了这一步
                # shader 会绑定恒等默认纹理，保存的 HSL 曲线整体 no-op
                lut = layer.hsl_curve_lut
                if lut.dtype != np.float32:
                    lut = lut.astype(np.float32)
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'hsl_curve', 'data': lut.tobytes()
                })
            if layer.active_lut_path:
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'lut3d', 'path': layer.active_lut_path
                })
            if layer.warper_params is not None:
                h, w, c = layer.warper_params.shape
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id,
                    'type': 'warper',
                    'data': layer.warper_params.astype(np.float32).tobytes(),
                    'size': (w, h)
                })

    def apply_curves(self, curves_lut: np.ndarray) -> None:
        """Attach an RGB curves LUT (256x3) to the active layer and preview it.

        LUT 归一为 float32 后以 UPDATE_AUX_TEX('curves') 上传，再以
        'grading' 通道触发交互预览渲染。
        """
        layer = self.get_active_layer()
        if layer:
            layer.curves_lut = curves_lut
            if self.renderer:
                lut = curves_lut
                if lut.dtype != np.float32:
                    lut = lut.astype(np.float32)
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'curves', 'data': lut.tobytes()
                })
            self.render_pipeline(render_reason='grading')

    def apply_hsl_curve(self, hsl_curve_lut: np.ndarray) -> None:
        """Apply HSL Curve (Lum vs Sat mapping) to active layer."""
        layer = self.get_active_layer()
        if layer:
            layer.hsl_curve_lut = hsl_curve_lut
            if self.renderer:
                lut = hsl_curve_lut
                if lut.dtype != np.float32:
                    lut = lut.astype(np.float32)
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'hsl_curve', 'data': lut.tobytes()
                })
            self.render_pipeline(render_reason='grading')

    def grade_image(self, lift, gamma, gain, exp, temp, tint, contrast, shadow, sat, vibrance=0, mid_detail=0, hue_shift=0, highlights: float | None = None) -> None:
        """仅更新 grading 参数，不触发渲染。由 UI 层统一控制渲染时机。

        LGG 面板的参数是权威全量；不在其管理范围内的扩展键
        （色调区间饱和度 lift/gamma/gain_saturation 等）保留原值，
        避免拖动色轮时抹掉其它面板写入的状态。
        """
        layer = self.get_active_layer()
        if layer:
            preserved = {
                k: v for k, v in layer.grading_params.items()
                if k not in ("lift", "gamma", "gain", "exposure", "temp",
                             "tint", "contrast", "shadow", "saturation",
                             "vibrance", "mid_detail", "hue_shift")
            }
            layer.grading_params = {
                "lift": lift, "gamma": gamma, "gain": gain,
                "exposure": exp, "temp": temp, "tint": tint,
                "contrast": contrast, "shadow": shadow, "saturation": sat,
                "vibrance": vibrance, "mid_detail": mid_detail, "hue_shift": hue_shift,
                **preserved
            }
            # Older integrations still send the original twelve parameters.
            # Preserve their existing highlights until explicitly supplied.
            if highlights is not None:
                layer.grading_params["highlights"] = highlights

    def set_tonal_saturation(self, lift_sat, gamma_sat, gain_sat) -> None:
        """仅更新三个色调区间饱和度（merge），不触发渲染。

        由 TonalSaturationPanel 驱动；值域 -100..100，与移动端
        tonal_saturation_adjust_g 一致。
        """
        layer = self.get_active_layer()
        if layer:
            layer.grading_params["lift_saturation"] = lift_sat
            layer.grading_params["gamma_saturation"] = gamma_sat
            layer.grading_params["gain_saturation"] = gain_sat

    def set_mixer_params(self, params):
        """仅更新 mixer 参数，不触发渲染。由 UI 层统一控制渲染时机。"""
        layer = self.get_active_layer()
        if layer:
            layer.mixer_params = params

    def set_effects_params(self, params):
        """仅更新 effects 参数，不触发渲染。由 UI 层统一控制渲染时机。"""
        layer = self.get_active_layer()
        if layer:
            layer.effects_params = params

    def set_soft_clip_params(self, params: Dict[str, Any]) -> None:
        """Merge soft-clip params into the active layer and submit a grading render."""
        layer = self.get_active_layer()
        if layer:
            layer.soft_clip_params.update(params)
            self.render_pipeline(render_reason='grading')

    def set_qualifier_params(self, params: Dict[str, Any]) -> None:
        """Merge qualifier (keyer) params into the active layer and submit a grading render."""
        layer = self.get_active_layer()
        if layer:
            layer.qualifier_params.update(params)
            self.render_pipeline(render_reason='grading')

    # ------------------------------------------------------------------
    # P1-COLOR-03: 调色关键帧
    # ------------------------------------------------------------------

    def set_grade_frame(self, frame):
        """记录当前视频源帧号；图层带有关键帧轨道时按帧求值并触发渲染。

        由 Color 页统一的帧回调（播放/seek）驱动；无轨道时只记录帧号，
        不触发任何渲染，保证零开销。
        """
        frame = int(frame) if frame is not None else None
        self.current_grade_frame = frame
        if frame is None:
            return
        if not self.has_grade_keyframes():
            return
        self.apply_grade_keyframes(frame)

    def has_grade_keyframes(self) -> bool:
        """Return True when any layer carries grade keyframe tracks."""
        return any(
            getattr(layer, 'grade_keyframes', None)
            for layer in getattr(self, 'layers', []) or []
        )

    def apply_grade_keyframes(self, frame: int, render: bool = True):
        """按帧求值所有带轨道图层的 grading_params（被轨道化的键打补丁）。

        ``render=False`` 供 undo/redo 恢复路径复用：只求值参数，由调用方
        随后的 render_pipeline 统一触发渲染，避免重复渲染。
        """
        from src.features.color.core.grade_keyframes import (
            evaluate_grade_keyframes, evaluate_mask_keyframes,
            evaluate_qualifier_keyframes,
        )
        changed = False
        mask_changed = False
        for layer in self.layers or []:
            tracks = getattr(layer, 'grade_keyframes', None)
            if not tracks:
                continue
            layer.grading_params = evaluate_grade_keyframes(
                tracks, int(frame), layer.grading_params)
            layer.is_dirty = True
            changed = True
            # P1-COLOR-09: mask.<shape_id>.<field> 轨道求值——照
            # refresh_tracking_masks 模式置 mask_dirty 并立即重栅格化
            # 上传，播放渲染管线吃掉 dirty，暂停帧随后的渲染取新纹理。
            if evaluate_mask_keyframes(layer, int(frame)):
                layer.mask_dirty = True
                mask_changed = True
            # F4.5：qualifier.<field> 轨道（亮度区间）求值——就地写回
            # qualifier_params，渲染管线经既有 qualifier 阶段消费（预览
            # 与成片导出同帧求值语义由导出侧 evaluate 同函数保证）。
            if evaluate_qualifier_keyframes(layer, int(frame)):
                layer.is_dirty = True
        if mask_changed:
            try:
                self._update_mask_textures()
            except Exception:
                logger.debug('_update_mask_textures failed', exc_info=True)
        if changed and render:
            self.render_pipeline(render_reason='grading')
        return changed

    def active_grade_keyframe_frames(self):
        """活动图层关键帧帧号并集（ruler 标记用）。"""
        from src.features.color.core.grade_keyframes import all_keyframe_frames
        layer = self.get_active_layer()
        tracks = getattr(layer, 'grade_keyframes', None) if layer else None
        return all_keyframe_frames(tracks or {})

    def record_grade_keyframe(self, frame: int, param_name: str, value,
                              interpolation: str = 'linear') -> bool:
        """在活动图层上为参数写入/覆盖当前帧关键帧。"""
        from src.features.color.core.grade_keyframes import (
            GRADE_KEYFRAME_PARAMS, GradeKeyframeTrack,
        )
        if param_name not in GRADE_KEYFRAME_PARAMS:
            return False
        layer = self.get_active_layer()
        if layer is None:
            return False
        track = layer.grade_keyframes.get(param_name)
        if track is None:
            track = GradeKeyframeTrack(param_name=param_name)
            layer.grade_keyframes[param_name] = track
        track.set_keyframe(int(frame), value, interpolation)
        return True

    def remove_grade_keyframe(self, frame: int) -> bool:
        """删除活动图层所有轨道在当前帧的关键帧。"""
        layer = self.get_active_layer()
        if layer is None:
            return False
        removed = False
        for track in list(layer.grade_keyframes.values()):
            if track.remove_keyframe(int(frame)):
                removed = True
        # 清掉空轨道
        layer.grade_keyframes = {
            name: track for name, track in layer.grade_keyframes.items()
            if track.keyframes
        }
        if removed and self.current_grade_frame == int(frame):
            self.apply_grade_keyframes(int(frame))
        return removed

    def set_film_grain_params(self, params: Dict[str, Any]) -> None:
        """Merge film-grain params and submit a grading render.

        seed 只由参数决定（缺省时补 0.5），保证重渲/导出可复现
        （P1-COLOR-13）。
        """

        self.film_grain_params.update(params)

        # P1-COLOR-13: seed 只由参数决定，保证重渲可复现
        # （旧实现从关到开时取墙钟时间，跨会话不可复现）
        if "seed" not in params:
            if not self.film_grain_params.get("seed"):
                self.film_grain_params["seed"] = 0.5

        self.render_pipeline(render_reason='grading')

    def reseed_film_grain(self) -> float:
        """P1-COLOR-13: 显式换一颗种子（确定性步进，用户主动操作）。"""
        current = float(self.film_grain_params.get("seed", 0.0) or 0.0)
        new_seed = (current + (1.0 - GRAIN_ANIMATION_STEP)) % 1.0
        self.set_film_grain_params({"seed": new_seed})
        return new_seed

    def _effective_film_grain_params(self) -> Dict[str, Any]:
        """构建送入渲染管线的 grain 参数（animated 模式换算帧种子）。"""
        params = dict(self.film_grain_params)
        if params.get("animated"):
            renderer = getattr(self, "renderer", None)
            frame = int(getattr(renderer, "current_video_frame_index", -1) or -1)
            params["seed"] = effective_grain_seed(params, frame)
        return params

    @pyqtSlot(np.ndarray)
    def on_ai_mask_ready(self, matte: np.ndarray) -> None:
        """Slot to receive generated matte from AI worker.
        
        This method handles both SAM continuous sessions and BiRefNet one-shot masks:
        - If an active SAM shape ID exists and is found in the layer's mask_shapes, 
          update that shape's bitmap_data directly (SAM continuous session).
        - Otherwise, create a new BITMAP shape and track it if this is a SAM session.
        
        Args:
            matte: The generated mask as a numpy array (float32, 0-1 range).
        """
        layer = self.get_active_layer()
        if not layer:
            return
        
        # Try to find existing SAM shape for update
        existing_shape = None
        if self._active_sam_shape_id is not None:
            for s in layer.mask_shapes:
                if s.id == self._active_sam_shape_id:
                    existing_shape = s
                    break
        
        if existing_shape is not None:
            # Update existing shape (SAM continuous session)
            existing_shape.bitmap_data = matte
            layer.mask_dirty = True
            layer.is_dirty = True
            self.render_pipeline()
            logger.info("Mask Updated (continuous session)")
        else:
            # Create new shape (BiRefNet or first SAM click)
            shape = layer.add_shape(MaskShape.BITMAP)
            shape.name = "AI Mask"
            shape.bitmap_data = matte
            shape.feather = 0  # AI masks default to sharp edges
            
            # Track this shape if it's a SAM session (has points)
            if hasattr(self, '_sam_points') and self._sam_points:
                self._active_sam_shape_id = shape.id
                logger.info(f"SAM Mask created and tracked: {shape.id}")
            else:
                logger.info("Mask created (one-shot)")
            
            layer.mask_dirty = True
            self.mask_shapes_changed.emit()
            self.render_pipeline()
        
        # Notify UI that AI processing has finished
        self.ai_process_finished.emit()

    # --- AI Integration Methods ---
    def _prepare_image_for_ai(self, img):
        """Prepares image for AI models (converts BGR to RGB, and uint16/float to uint8)"""
        if img is None: return None
        
        # 1. Handle Depth (AI models expect 8-bit uint8)
        if img.dtype == np.uint16:
            img_u8 = (img / 256).astype(np.uint8)
        elif img.dtype == np.float32 or img.dtype == np.float64:
            img_u8 = (np.clip(img, 0.0, 1.0) * 255).astype(np.uint8)
        else:
            img_u8 = img.copy()
            
        # 2. Handle Color Space (Convert OpenCV BGR to standard RGB)
        img_rgb = cv2.cvtColor(img_u8, cv2.COLOR_BGR2RGB)
        return np.ascontiguousarray(img_rgb)

    def process_ai_plugin(self, layer_index, instance_id):
        """Trigger ONNX inference for an AI plugin on a specific layer.

        Args:
            layer_index: Index of the layer in self.layers
            instance_id: Plugin instance ID
        """
        if not (0 <= layer_index < len(self.layers)):
            return
        layer = self.layers[layer_index]
        plugin_inst = layer.get_plugin_instance(instance_id)
        if not plugin_inst or plugin_inst.get('type') != 'ai':
            return

        from src.features.color.core.plugin_manager import get_plugin_manager
        plugin_data = get_plugin_manager().get_plugin(plugin_inst['plugin_id'])
        if not plugin_data:
            return

        # Get source image as float32 RGB [0, 1]
        if self.img_full_original is None:
            return

        source_bgr = self.img_full_original.astype(np.float32) / 255.0
        source_rgb = cv2.cvtColor(source_bgr, cv2.COLOR_BGR2RGB)

        logger.info(f"Starting AI plugin: {plugin_data.get('name', plugin_inst['plugin_id'])}")

        # Start async inference
        self.ai_plugin_engine.process_plugin(
            layer.id, instance_id, source_rgb, plugin_data,
            plugin_inst.get('params', {})
        )

    def _on_ai_plugin_finished(self, layer_id, instance_id, result_array):
        """Handle AI plugin inference completion - upload to GPU texture."""
        logger.info(f"AI plugin finished for layer {layer_id[:8]}")

        # CPU 缓存/EXR 落盘契约是 BGR（FileIO.save/load_ai_plugin_result
        # 双向 BGR，历史项目数据即此格式）；GPU 上传契约是 RGB——
        # _do_upload_ai_plugin_result 按声明原样写入纹理、不做通道转换。
        # 此前同一个 BGR 数组既进缓存又进上传，AI 结果在 GPU 端红蓝互换
        # （P0-COL-03）。
        if result_array.shape[2] == 3:
            result_bgr = cv2.cvtColor(result_array, cv2.COLOR_RGB2BGR)
        else:
            result_bgr = result_array

        # Store result in CPU cache for project save persistence
        self.ai_plugin_result_cache[(layer_id, instance_id)] = result_bgr.copy()

        # Upload result to GPU via render thread (RGB contract)
        if self.renderer:
            self.renderer.submit(RenderCommand.UPLOAD_AI_PLUGIN_RESULT, {
                'layer_id': layer_id,
                'instance_id': instance_id,
                'image_array': result_array
            })

        # Mark layer as dirty and trigger re-render
        for layer in self.layers:
            if layer.id == layer_id:
                layer.is_dirty = True
                break
        # P0-COL-03：此前调用不存在的 self.request_render()，槽函数在提交
        # 重渲染前抛 AttributeError——AI 完成后预览不刷新、
        # ai_process_finished 信号不发射（UI 进度条卡死）。
        self.render_pipeline()
        self.ai_process_finished.emit()

    def _on_ai_plugin_progress(self, layer_id, instance_id, pct):
        """Forward AI plugin progress."""
        logger.info(f"AI plugin progress: {pct}%")

    def _on_ai_plugin_error(self, layer_id, instance_id, msg):
        """Handle AI plugin error."""
        logger.error(f"AI plugin error: {msg}")
        self.error_occurred.emit(f"AI Plugin Error: {msg}")

    def _get_layer_ai_texture(self, layer):
        """返回该层最后一个启用 AI 插件的 GPU 结果纹理（无则 None）。

        通道契约见 _on_ai_plugin_finished：GPU 纹理为 RGB（P0-COL-03）。
        预览与全分辨率路径共用此解析，保证两路消费同一纹理。
        """
        renderer = self.renderer
        if renderer is None:
            return None
        ai_plugins = [p for p in (getattr(layer, 'plugins', None) or [])
                      if isinstance(p, dict) and p.get('type') == 'ai'
                      and p.get('enabled', True)]
        if not ai_plugins:
            return None
        last = ai_plugins[-1]
        return renderer.ai_plugin_textures.get(
            (layer.id, last.get('instance_id')))

    def remove_plugin(self, layer_index, instance_id):
        """Remove a plugin instance and clean up all associated resources.

        Args:
            layer_index: Index of the layer containing the plugin
            instance_id: The plugin instance ID to remove
        """
        if layer_index < 0 or layer_index >= len(self.layers):
            return
        layer = self.layers[layer_index]

        # 1. Remove from layer's plugin list
        layer.remove_plugin(instance_id)

        # 2. Clear CPU cache
        cache_key = (layer.id, instance_id)
        self.ai_plugin_result_cache.pop(cache_key, None)

        # 3. Release GPU texture
        if self.renderer:
            self.renderer.submit(RenderCommand.REMOVE_AI_PLUGIN_TEXTURE, {
                'layer_id': layer.id,
                'instance_id': instance_id
            })

        # 4. Mark dirty and re-render（P0-COL-03：request_render 未定义，
        # 移除 AI 插件后同样从不重渲染——改用标准入口）
        layer.is_dirty = True
        self.render_pipeline()

    def request_birefnet(self):
        """Triggers BiRefNet background removal on the current image."""
        if self.img_full_original is not None:
            logger.info("sig_process_birefnet (with cleaned RGB data)")
            img_ready = self._prepare_image_for_ai(self.img_full_original)
            self.sig_process_birefnet.emit(img_ready)
        else:
            logger.error("failed: img_full_original is None")

    def request_sam_encode(self):
        """Triggers SAM encoding on the current image."""
        if self.img_full_original is not None:
            logger.info("sig_process_sam_encode (with cleaned RGB data)")
            img_ready = self._prepare_image_for_ai(self.img_full_original)
            self.sig_process_sam_encode.emit(img_ready)
        else:
            logger.error("failed: img_full_original is None")

    def request_sam_decode(self, x: float, y: float, label: int) -> None:
        """Triggers SAM decoding with a new point."""
        if not hasattr(self, '_sam_points'):
            self._sam_points = []
            self._sam_labels = []

        self._sam_points.append((x, y))
        self._sam_labels.append(label)

        logger.info(f"sig_process_sam_decode with {len(self._sam_points)} points")
        # AUD-0606：跨线程信号必须发射快照副本（与 undo_sam_point/
        # remove_sam_point 一致）——按引用发射时 AI 工作线程遍历期间，
        # UI 线程下一次点击继续 append 同一列表，产生数据竞争。
        self.sig_process_sam_decode.emit(
            self._sam_points.copy(), self._sam_labels.copy())

    def reset_sam_points(self):
        """Clear all SAM points and labels, and reset the active SAM shape tracking."""
        self._sam_points = []
        self._sam_labels = []
        self._active_sam_shape_id = None

    def undo_sam_point(self) -> bool:
        """Remove the last SAM point and refresh the mask if points remain.
        
        Returns:
            bool: True if a point was removed, False if no points exist.
        """
        if not hasattr(self, '_sam_points') or not self._sam_points:
            return False
        
        self._sam_points.pop()
        self._sam_labels.pop()
        
        if self._sam_points:
            # Re-request decode with remaining points to refresh the mask
            self.sig_process_sam_decode.emit(self._sam_points.copy(), self._sam_labels.copy())
        # If no points left, the mask will remain as-is until user adds new points
        # or clears it via the mask panel reset button
        
        return True

    def remove_sam_point(self, index: int) -> bool:
        """Remove a specific SAM point by index and refresh the mask if points remain.
        
        This method is called when the user right-clicks on an existing SAM point
        marker to delete it individually.
        
        Args:
            index: The index of the point to remove in _sam_points/_sam_labels lists.
            
        Returns:
            bool: True if a point was removed, False if index is invalid or no points exist.
        """
        if not hasattr(self, '_sam_points') or not self._sam_points:
            return False
        
        if index < 0 or index >= len(self._sam_points):
            return False
        
        # Remove the point at the specified index
        self._sam_points.pop(index)
        self._sam_labels.pop(index)
        
        logger.info(f"point {index} removed. Remaining points: {len(self._sam_points)}")
        
        # Clear the active SAM shape tracking if no points remain
        if not self._sam_points:
            # 先暂存目标 ID，然后再清理状态
            target_shape_id = self._active_sam_shape_id
            self._active_sam_shape_id = None
            
            layer = self.get_active_layer()
            if layer:
                if target_shape_id:
                    for shape in layer.mask_shapes:
                        if shape.id == target_shape_id:
                            layer.mask_shapes.remove(shape)
                            break
                layer.mask_dirty = True
                layer.is_dirty = True
                self.mask_shapes_changed.emit()
                self.render_pipeline()
        else:
            # Re-request decode with remaining points to refresh the mask
            self.sig_process_sam_decode.emit(self._sam_points.copy(), self._sam_labels.copy())
        
        return True

    @pyqtSlot(dict)
    def on_raw_params_changed(self, params: dict) -> None:
        """Handles RAW parameter changes from RawPanel.

        AUD-0612: 入口先浅拷贝调用方 dict——本方法会写入 ``user_wb``、
        把 ``current_raw_params`` 挂到 engine，并在重解码路径经 Q_ARG 把
        参数引用跨线程交给 RAW worker；就地变异调用方持有的对象会让
        工作线程读到中途状态、engine 与调用方共享可变结构。
        """
        params = dict(params)  # AUD-0612: 与调用方对象解耦
        if self._last_applied_raw_params == params:
            return
            
        self.current_raw_params = params
        if not self.current_file_path:
            return

        # GPU Speedup: All Raw Tone/Exposure parameters are now handled in Shader
        # This provides real-time 60FPS feedback and avoids re-decoding lag.
        
        # 1. White Balance Calculation - LOCK FREE
        from src.shared.io.file_io import FileIO
        baseline_wb = self.current_raw_metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
        
        wb_type = params.get('wb_type', 'camera')
        if wb_type == 'camera' or wb_type == 'as_shot':
            curr_wb = baseline_wb
        elif wb_type == 'auto':
            # Auto WB requires re-decoding to get LibRaw's result
            # For real-time, we stay with camera WB until user stops dragging
            curr_wb = baseline_wb 
        else: # custom
            curr_wb = FileIO.calculate_wb_gains(baseline_wb, params.get('temp', 6500), params.get('tint', 0))

        # Store computed user_wb in params
        # AUD-0612: list(...) 防引用污染——custom WB 时 curr_wb 可能是
        # current_raw_metadata 内部 list 的引用，不得随 params 外泄/跨线程。
        params['user_wb'] = list(curr_wb)
        
        # Gains are relative to identity (1.0, 1.0, 1.0) because Raw is decoded with bright=1.0 and camera_wb
        # Wait, if we decode with camera_wb, then shader gains should be relative to camera_wb.
        # But for unified pipeline, we decode with unity gains and apply everything in shader?
        # No, FileIO still uses some WB. Let's make it relative to the decoded state.
        last_decode_wb = self._last_applied_raw_params.get('user_wb', baseline_wb)
        gains = (
            float(curr_wb[0] / max(1e-4, last_decode_wb[0])),
            float(curr_wb[1] / max(1e-4, last_decode_wb[1])),
            float(curr_wb[2] / max(1e-4, last_decode_wb[2]))
        )

        # 2. Mapping Color Space/Gamma strings to int IDs
        cs_map = {
            "sRGB": 0, "Rec.709": 1, "Rec.2020": 2, "Adobe RGB": 3, "DCI-P3": 4, "Display P3": 5,
            "ACEScg": 6, "ARRI Wide Gamut 3": 7, "Sony S-Gamut3": 8, "Panasonic V-Gamut": 9, "DaVinci Wide Gamut": 10, 
            "REDWideGamutRGB": 11, "Canon Cinema Gamut": 12, "Fujifilm F-Gamut": 13, "BMD Film Gen5": 14, "CMYK Simulation": 15
        }
        gamma_map = {
            "Linear": 0, "sRGB": 1, "Rec.709": 2, "Gamma 2.2": 3, "Gamma 2.4": 4, "Gamma 2.6": 5, 
            "Rec.2100 HLG": 6, "sLog3": 7, "ARRI LogC3": 8, "ACEScct": 9, "Panasonic V-Log": 10, "DaVinci Intermediate": 11,
            "RED Log3G10": 12, "Canon Log 2": 13, "Canon Log 3": 14, "Fujifilm F-Log": 15, "Fujifilm F-Log2": 16, "Apple Log": 17
        }

        raw_preview_data = {
            'exposure': params.get('exposure', 0.0), # Absolute exposure now
            'gains': gains,
            'color_space': cs_map.get(params.get('color_space', "sRGB"), 0),
            'gamma': gamma_map.get(params.get('gamma', "Linear"), 0),
            'shadows': params.get('shadows', 0.0),
            'highlights': params.get('highlights', 0.0),
            'contrast': params.get('contrast', 1.0),
            'saturation': params.get('saturation', 100.0) / 100.0,
            'lift': params.get('lift', 0.0),
            'gain': params.get('gain', 1.0),
            'color_boost': params.get('color_boost', 0.0),
            'mid_detail': params.get('mid_detail', 0.0),
            'sharpness': params.get('sharpness', 0.0),
        }

        # Submit to renderer as preview uniforms
        if self.renderer:
            self.renderer.submit(RenderCommand.UPDATE_UNIFORMS, {
                'raw_preview': raw_preview_data
            })
            
            input_lut = params.get('input_lut')
            if input_lut != self._last_applied_raw_params.get('input_lut'):
                 self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'type': 'raw_input_lut', 
                    'path': input_lut
                })
        
        self.render_pipeline(render_reason='grading')

        # Only trigger heavy re-decoding for non-interacting changes or WB 'auto'
        # Also ensure we only call decode for actual RAW files
        is_raw = self.current_file_path and os.path.splitext(self.current_file_path)[1].lower() in self.RAW_EXTENSIONS

        if is_raw and (not self.is_interacting or wb_type == 'auto'):
            from PyQt6.QtCore import QMetaObject, Q_ARG
            # AUD-0612: 传给 worker 前再浅拷贝一次——queued 连接只传递
            # Python 对象引用，拷贝后 worker 与 engine 各持独立 dict。
            QMetaObject.invokeMethod(self.raw_worker, "decode",
                                   Qt.ConnectionType.QueuedConnection,
                                   Q_ARG(str, self.current_file_path),
                                   Q_ARG(dict, dict(params)))
        elif not is_raw:
            # For non-RAW files, we don't call the worker, so we must update 
            # the last applied params immediately to keep the state in sync.
            self._last_applied_raw_params = params.copy()

    def _on_raw_update_finished(self, full, high, low, applied_params):
        """Consume a finished background RAW decode (RawUpdateWorker.finished).

        重解码把 RAW 参数烘焙进了新图：重置 shader 侧 RAW 预览 uniforms、
        同步 _last_applied_raw_params 基线、重置所有层蒙版置脏，并把新
        预览图上传渲染线程后触发全量渲染。
        """
        if full is None: return
        
        # Reset Shader RAW Previews since they are now baked into the decoded image
        # Note: Some parameters might still need to be applied in shader if not baked by raw decoder
        # For this implementation, we assume raw decoder bakes everything, so we reset previews.
        # EXCEPT: If we want to keep "live" editing feel, we might want to keep parameters in shader
        # and only use raw decoder for de-bayering quality updates.
        # But typically "Apply" means baking.
        if self.renderer:
            self.renderer.submit(RenderCommand.UPDATE_UNIFORMS, {
                'raw_preview': {
                    'exposure': 0.0, 
                    'gains': (1.0, 1.0, 1.0),
                    'shadows': 0.0,
                    'highlights': 0.0,
                    'contrast': 1.0,
                    'saturation': 1.0,
                    'lift': 0.0,
                    'gain': 1.0
                }
            })

        self._last_applied_raw_params = applied_params.copy()
        for layer in self.layers:
            layer.mask_dirty = True
        # AUD-0611：重解码直接替换了源图像内容（绕过 set_image），必须
        # 与 set_image（P0-COL-04）及 undo/redo 恢复路径同口径递增
        # _source_epoch——bake 前缀摘要以 src_epoch 为盐，漏掉该路径
        # 会让"半新半旧"的 baked 层通过失效校验。
        self._source_epoch += 1
        self.img_full_original = full
        
        def normalize_to_f32(img):
            if img.dtype == np.uint16: return img.astype(np.float32) / 65535.0
            if img.dtype == np.uint8: return img.astype(np.float32) / 255.0
            return img.astype(np.float32)

        self.img_high = normalize_to_f32(high)
        self.img_low = normalize_to_f32(low)
        
        img_for_upload = np.ascontiguousarray(self.img_high, dtype=np.float32)
        if self.renderer:
            # Pass original full resolution size for consistency
            orig_h, orig_w = self.img_full_original.shape[:2]
            self.renderer.submit(RenderCommand.UPLOAD_IMAGE, {
                'image': img_for_upload,
                'original_size': (orig_w, orig_h)
            })
        
        self.set_reference_image(self.img_high)
        self.render_pipeline()

    def set_warper_params(self, grid_data: Optional[np.ndarray]) -> None:
        """Set the warper mesh (HxWx2 位移网格) on the active layer and preview.

        None 表示清除 warper；非 None 时归一为 float32 网格纹理上传。
        """
        layer = self.get_active_layer()
        if layer:
            layer.warper_params = grid_data
            if self.renderer and grid_data is not None:
                h, w, c = grid_data.shape
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id,
                    'type': 'warper',
                    'data': grid_data.astype(np.float32).tobytes(),
                    'size': (w, h)
                })
            self.render_pipeline(render_reason='grading')

    def _render_brush_mask_to_canvas(self, shape, target_w: int, target_h: int, img_w: int) -> np.ndarray:
        """渲染笔刷蒙版到指定尺寸的 canvas。
        
        将笔刷绘制逻辑提取为独立方法，供预览渲染和全分辨率导出共同调用，
        确保两者使用完全相同的算法。
        
        Args:
            shape: MaskShape 对象（类型必须为 MaskShape.BRUSH）
            target_w: 目标宽度（像素）
            target_h: 目标高度（像素）
            img_w: 原始图像宽度（用于计算笔刷半径缩放比例）
        
        Returns:
            shape_arr: float32 numpy 数组，范围 0.0-1.0，尺寸为 (target_h, target_w)
        """
        shape_arr = np.zeros((target_h, target_w), dtype=np.float32)
        
        # Get brush parameters
        brush_size = getattr(shape, 'brush_size', 50)
        brush_feather = getattr(shape, 'brush_feather', 0)
        brush_opacity = getattr(shape, 'brush_opacity', 1.0)
        brush_strokes = getattr(shape, 'brush_strokes', [])
        
        # Render each stroke
        for stroke in brush_strokes:
            points = stroke.get('points', [])
            stroke_size = stroke.get('size', brush_size)
            stroke_opacity = stroke.get('opacity', brush_opacity)
            stroke_feather = stroke.get('feather', brush_feather)
            stroke_flow = stroke.get('flow', 1.0)
            is_eraser = stroke.get('is_eraser', False)  # 橡皮擦模式
            
            if not points or len(points) < 2:
                continue
            
            # Convert normalized coordinates to pixel coordinates
            px_points = [(int(p[0] * target_w), int(p[1] * target_h)) for p in points]
            
            # Calculate brush radius in mask space (scale by image width)
            radius = max(2, int(stroke_size * target_w / img_w))
            feather_radius = max(0, int(stroke_feather * radius / 100.0))
            
            # Draw connected line segments with anti-aliasing
            # Use cv2.polylines for smooth connected strokes
            pts_array = np.array(px_points, dtype=np.int32).reshape((-1, 1, 2))
            
            # Create a temporary canvas for this stroke
            stroke_canvas = np.zeros((target_h, target_w), dtype=np.float32)
            
            # Draw thick lines connecting all points
            line_thickness = max(1, radius * 2)
            cv2.polylines(stroke_canvas, [pts_array], False, 1.0, thickness=line_thickness, lineType=cv2.LINE_AA)
            
            # Draw circles at each point to ensure rounded ends and fill gaps
            for px, py in px_points:
                cv2.circle(stroke_canvas, (px, py), radius, 1.0, -1)
            
            # Apply feather (Gaussian blur) for soft edges
            if feather_radius > 0:
                blur_ksize = max(3, int(feather_radius * 2 + 1))
                if blur_ksize % 2 == 0:
                    blur_ksize += 1
                stroke_canvas = self._fast_feather_blur(stroke_canvas, blur_ksize)
                # Renormalize after blur to maintain opacity
                max_val = stroke_canvas.max()
                if max_val > 0:
                    stroke_canvas = stroke_canvas / max_val
            
            # Apply opacity and flow
            stroke_canvas = stroke_canvas * stroke_opacity * stroke_flow
            
            # 橡皮擦模式：减法混合 / 画笔模式：加法混合
            if is_eraser:
                # Eraser mode: subtract from mask
                shape_arr = np.clip(shape_arr - stroke_canvas, 0.0, 1.0)
            else:
                # Brush mode: additive blending (accumulate strokes)
                shape_arr = np.clip(shape_arr + stroke_canvas, 0.0, 1.0)
        
        return shape_arr

    def _render_single_shape(self, shape: MaskShape, target_w: int, target_h: int, img_w: int) -> np.ndarray:
        """渲染单个 MaskShape 到指定尺寸的蒙版数组。

        Performance Optimization:
        - 分离形状生成、旋转和位移：
          * 形状缓存不含旋转和位置（circle/rect）
          * 旋转变化时只需 cv2.warpAffine（~0.5ms）
          * 位置变化时同样只需 warpAffine 平移（~0.5ms）
        - 形状生成（QPainter）只在形状/尺寸/羽化变化时执行

        Args:
            shape: MaskShape 对象
            target_w: 目标宽度（像素）
            target_h: 目标高度（像素）
            img_w: 原始图像宽度（用于笔刷缩放）

        Returns:
            float32 numpy 数组，范围 0.0-1.0
        """
        # Check full render cache first (exact match including rotation + position)
        full_key = shape._get_render_cache_key(target_w, target_h, img_w)
        if shape._render_cache is not None and shape._render_cache_key == full_key:
            return shape._render_cache.copy()

        has_rotation = shape.type not in (MaskShape.BITMAP, MaskShape.BRUSH) and abs(shape.rotation) > 0.01
        is_translatable = shape.type in ('circle', 'rect')

        if has_rotation:
            rot_pad = max(target_w, target_h) * 3 // 4 + 4

            # Shape cache excludes rotation AND position (for circle/rect)
            padded_key = ('padded', rot_pad) + shape._get_shape_cache_key(target_w, target_h, img_w)
            if shape._shape_cache is not None and shape._shape_cache_key == padded_key:
                shape_arr = shape._shape_cache.copy()
            else:
                shape_arr = self._generate_shape_texture(shape, target_w, target_h, img_w, pad=rot_pad)
                shape._shape_cache = shape_arr.copy()
                shape._shape_cache_key = padded_key

            # Rotate around canonical center
            pw = target_w + 2 * rot_pad
            ph = target_h + 2 * rot_pad
            ccx = 0.5 * target_w + rot_pad
            ccy = 0.5 * target_h + rot_pad
            M = cv2.getRotationMatrix2D((ccx, ccy), -shape.rotation, 1.0)
            rotated = cv2.warpAffine(shape_arr, M, (pw, ph),
                                       flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_CONSTANT,
                                       borderValue=0.0)
            shape_arr = rotated[rot_pad:rot_pad + target_h, rot_pad:rot_pad + target_w]

            # For circle/rect: translate from canonical center to actual position
            if is_translatable:
                dx = (shape.center[0] - 0.5) * target_w
                dy = (shape.center[1] - 0.5) * target_h
                if abs(dx) > 0.5 or abs(dy) > 0.5:
                    M_trans = np.float32([[1, 0, dx], [0, 1, dy]])
                    shape_arr = cv2.warpAffine(shape_arr, M_trans, (target_w, target_h),
                                               flags=cv2.INTER_LINEAR,
                                               borderMode=cv2.BORDER_CONSTANT,
                                               borderValue=0.0)
        else:
            # No rotation: use standard (unpadded) shape cache
            # Blur is applied AFTER translation on the final canvas,
            # so no feather padding needed — same as rendering at actual position.
            shape_key = shape._get_shape_cache_key(target_w, target_h, img_w)
            if shape._shape_cache is not None and shape._shape_cache_key == shape_key:
                shape_arr = shape._shape_cache.copy()
            else:
                shape_arr = self._generate_shape_texture(shape, target_w, target_h, img_w)
                shape._shape_cache = shape_arr.copy()
                shape._shape_cache_key = shape_key

            # For circle/rect: translate from canonical center to actual position
            if is_translatable:
                dx = (shape.center[0] - 0.5) * target_w
                dy = (shape.center[1] - 0.5) * target_h
                if abs(dx) > 0.5 or abs(dy) > 0.5:
                    M = np.float32([[1, 0, dx], [0, 1, dy]])
                    shape_arr = cv2.warpAffine(shape_arr, M, (target_w, target_h),
                                               flags=cv2.INTER_LINEAR,
                                               borderMode=cv2.BORDER_CONSTANT,
                                               borderValue=0.0)

        # Apply feather/blur/clean AFTER all transformations (rotation, translation, crop).
        # This ensures blur operates on the final target_w × target_h canvas with
        # BORDER_REFLECT_101, preventing any edge clipping — identical to v4 behavior.
        # Performance: _fast_feather_blur is O(1) per pixel, ~5ms for 512×512.

        # Apply feather (ONLY for vector masks - NOT for BITMAP/AI masks)
        if shape.type == MaskShape.CURVE and shape.has_per_point_feather:
            shape_arr = self._render_curve_feather_sdf(shape, shape_arr, target_w, target_h)
        elif shape.feather > 0 and shape.type != MaskShape.BITMAP:
            blur_radius = int((shape.feather / 100.0) * 4.0 * (target_w / 5.0))
            if blur_radius > 0:
                if blur_radius % 2 == 0: blur_radius += 1
                shape_arr = self._fast_feather_blur(shape_arr, blur_radius)

        # Apply blur (works for ALL mask types including BITMAP)
        if hasattr(shape, 'blur') and shape.blur > 0:
            blur_radius = int((shape.blur / 100.0) * (target_w / 5.0))
            if blur_radius > 0:
                if blur_radius % 2 == 0: blur_radius += 1
                shape_arr = self._fast_feather_blur(shape_arr, blur_radius)

        # Apply clean_black
        if hasattr(shape, 'clean_black') and shape.clean_black > 0:
            kernel_size = max(3, int(shape.clean_black * 3.0))
            if kernel_size % 2 == 0: kernel_size += 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            shape_arr = cv2.erode(shape_arr, kernel, iterations=1)

        # Apply clean_white
        if hasattr(shape, 'clean_white') and shape.clean_white > 0:
            kernel_size = max(3, int(shape.clean_white * 3.0))
            if kernel_size % 2 == 0: kernel_size += 1
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))
            shape_arr = cv2.dilate(shape_arr, kernel, iterations=1)

        # Apply shape-level opacity and invert
        shape_arr *= shape.opacity
        if shape.invert:
            shape_arr = 1.0 - shape_arr

        # Store full render cache
        shape._render_cache = shape_arr
        shape._render_cache_key = full_key

        return shape_arr

    def _render_curve_feather_sdf(self, shape: MaskShape, sharp_mask: np.ndarray,
                                   target_w: int, target_h: int) -> np.ndarray:
        """使用有符号距离场 (SDF) 渲染 Curve 的逐点内外羽化。

        算法：
        1. 计算 signed distance field（正值=内部，负值=外部）
        2. 密采样 bezier 曲线，为每个采样点插值 fi/fo 值
        3. 用 distanceTransformWithLabels 找到每个像素最近的曲线采样点
        4. 用 smoothstep 距离 / 局部羽化值 计算 alpha

        性能：512×512 约 4-6ms（distanceTransform ~1ms + 查表 ~3ms）
        """
        h, w = sharp_mask.shape
        mask_uint8 = (sharp_mask > 0.5).astype(np.uint8)

        # 1. 计算 signed distance field
        dist_inside = cv2.distanceTransform(mask_uint8, cv2.DIST_L2, 5)
        dist_outside = cv2.distanceTransform(1 - mask_uint8, cv2.DIST_L2, 5)
        sdf = dist_inside.astype(np.float32) - dist_outside.astype(np.float32)

        # 2. 羽化缩放因子（与 _fast_feather_blur 一致）
        feather_scale = 4.0 * (target_w / 5.0) / 100.0  # pixels per feather unit

        # 3. 构建曲线采样点和羽化查找
        # 从 QPainterPath 采样不够方便，直接从控制点采样 bezier
        pts = shape.points
        n_ctrl = len(pts)
        if n_ctrl < 2:
            return sharp_mask

        # 每个 bezier 段细分为 sub_div 个采样点
        sub_div = 30
        samples = []  # [(pixel_x, pixel_y, fi_pixels, fo_pixels), ...]

        for i in range(n_ctrl):
            p1 = pts[i]
            p2 = pts[(i + 1) % n_ctrl]

            # 获取端点的 fi/fo 值（None 则用全局 feather 的一半）
            global_fi = shape.feather / 2.0
            global_fo = shape.feather / 2.0
            fi1 = p1.get('fi') if p1.get('fi') is not None else global_fi
            fo1 = p1.get('fo') if p1.get('fo') is not None else global_fo
            fi2 = p2.get('fi') if p2.get('fi') is not None else global_fi
            fo2 = p2.get('fo') if p2.get('fo') is not None else global_fo

            # Bezier 段: pos1 -> c2_1 -> c1_2 -> pos2 (cubic)
            x0, y0 = p1['pos'][0] * w, p1['pos'][1] * h
            x1, y1 = p1['c2'][0] * w, p1['c2'][1] * h
            x2, y2 = p2['c1'][0] * w, p2['c1'][1] * h
            x3, y3 = p2['pos'][0] * w, p2['pos'][1] * h

            for s in range(sub_div):
                t = s / sub_div
                # Cubic bezier
                u = 1 - t
                px = u**3 * x0 + 3 * u**2 * t * x1 + 3 * u * t**2 * x2 + t**3 * x3
                py = u**3 * y0 + 3 * u**2 * t * y1 + 3 * u * t**2 * y2 + t**3 * y3
                # 线性插值 fi/fo
                fi = fi1 + (fi2 - fi1) * t
                fo = fo1 + (fo2 - fo1) * t
                samples.append((px, py, fi * feather_scale, fo * feather_scale))

        if not samples:
            return sharp_mask

        # 4. 构建曲线标记图用于 distanceTransformWithLabels
        # 在曲线上画标记像素（值为0），其余为255
        curve_marker = np.full((h, w), 255, dtype=np.uint8)
        # 同时创建一个坐标到采样索引的映射
        sample_map = {}  # (y, x) -> sample_index

        for idx, (sx, sy, fi_px, fo_px) in enumerate(samples):
            ix, iy = int(round(sx)), int(round(sy))
            if 0 <= ix < w and 0 <= iy < h:
                curve_marker[iy, ix] = 0
                # 多个采样可能映射到同一像素，保留第一个（距离最近的覆盖）
                if (iy, ix) not in sample_map:
                    sample_map[(iy, ix)] = idx

        # 5. 用 distanceTransform 找到每个像素最近的曲线采样点
        dist_result, labels = cv2.distanceTransformWithLabels(
            curve_marker, cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL
        )

        sample_arr = np.array(samples, dtype=np.float32)  # (N, 4): px, py, fi_px, fo_px
        default_f = shape.feather / 2.0 * feather_scale

        # P0-COL-05：DIST_LABEL_PIXEL 的 label 是「零像素按行主序扫描的
        # 序号（从 1 起）」，不是线性坐标 (y*w+x)。旧实现按坐标解码，
        # label 几乎永远匹配不到采样点坐标，逐点 fi/fo 退化成全局
        # feather/2。这里改为：零像素表（np.nonzero，行主序，与 OpenCV
        # 编号一致）建立 label→采样点 的 LUT，整图一次向量化查表——
        # 同时替换掉旧实现 O(像素×采样点) 的逐点布尔掩码循环。
        fi_map = np.full((h, w), default_f, dtype=np.float32)
        fo_map = np.full((h, w), default_f, dtype=np.float32)

        zero_ys, zero_xs = np.nonzero(curve_marker == 0)
        max_label = int(labels.max()) if labels.size else 0
        lut_size = max(max_label, len(zero_ys)) + 1
        lut_fi = np.full(lut_size, default_f, dtype=np.float32)
        lut_fo = np.full(lut_size, default_f, dtype=np.float32)
        for k in range(len(zero_ys)):
            sidx = sample_map.get((int(zero_ys[k]), int(zero_xs[k])))
            if sidx is not None:
                lut_fi[k + 1] = sample_arr[sidx, 2]  # label 序号从 1 起
                lut_fo[k + 1] = sample_arr[sidx, 3]

        on_curve = labels >= 1
        fi_map[on_curve] = lut_fi[labels[on_curve]]
        fo_map[on_curve] = lut_fo[labels[on_curve]]
        # 曲线像素自身（label 0）直接取所在坐标的采样点值
        for (iy, ix), sidx in sample_map.items():
            fi_map[iy, ix] = sample_arr[sidx, 2]
            fo_map[iy, ix] = sample_arr[sidx, 3]

        # 7. 用 smoothstep 计算 alpha
        total = fi_map + fo_map
        total = np.maximum(total, 0.001)  # 避免除零
        t = np.clip((sdf + fo_map) / total, 0.0, 1.0)
        alpha = t * t * (3.0 - 2.0 * t)  # Hermite smoothstep

        return alpha

    def _fast_feather_blur(self, arr: np.ndarray, blur_radius: int) -> np.ndarray:
        """Fast feather blur using 3-pass box blur to approximate Gaussian.

        Uses integral-image based box blur (O(1) per pixel per pass) to approximate
        a Gaussian blur. This approach:
        - Has NO edge clipping artifacts (unlike downscale-blur-upscale)
        - Is fast regardless of kernel size (constant time per pixel)
        - Produces results visually identical to true Gaussian blur (<0.5% error)
        - Reference: "Fast Almost-Gaussian Filtering" by Peter Kovesi
        """
        if blur_radius <= 31:
            return cv2.GaussianBlur(arr, (blur_radius, blur_radius), 0)

        # Calculate Gaussian sigma from kernel size (OpenCV's formula)
        sigma = 0.3 * ((blur_radius - 1) * 0.5 - 1) + 0.8

        # Calculate box sizes for 3-pass approximation
        n = 3
        w_ideal = np.sqrt(12.0 * sigma * sigma / n + 1.0)
        wl = int(np.floor(w_ideal))
        if wl % 2 == 0: wl -= 1
        wu = wl + 2
        m = int(round((12.0 * sigma * sigma - n * wl * wl - 4 * n * wl - 3 * n) / (-4.0 * wl - 4.0)))
        m = max(0, min(m, n))
        box_sizes = [wl] * m + [wu] * (n - m)

        result = arr
        for box_size in box_sizes:
            if box_size >= 3:
                result = cv2.blur(result, (box_size, box_size))

        return result

    def _generate_shape_texture(self, shape: MaskShape, target_w: int, target_h: int, img_w: int, pad: int = 0) -> np.ndarray:
        """生成不带旋转的形状纹理（不含opacity/invert/rotation）。

        Args:
            pad: 当需要旋转时，在画布四周添加的额外像素。
                 形状会在更大的画布上绘制（位置偏移pad），这样旋转后
                 形状的羽化边缘不会被画布边界截断。
        """
        canvas_w = target_w + 2 * pad
        canvas_h = target_h + 2 * pad

        # Check for Bitmap type first - it doesn't need QPainter
        if shape.type == MaskShape.BITMAP and shape.bitmap_data is not None:
            shape_arr = cv2.resize(shape.bitmap_data, (canvas_w, canvas_h), interpolation=cv2.INTER_LINEAR)
        elif shape.type == MaskShape.BRUSH:
            shape_arr = self._render_brush_mask_to_canvas(shape, target_w, target_h, img_w)
            if pad > 0:
                tmp = np.zeros((canvas_h, canvas_w), dtype=np.float32)
                tmp[pad:pad+target_h, pad:pad+target_w] = shape_arr
                shape_arr = tmp
        else:
            # For vector shapes, use QPainter (WITHOUT rotation)
            shape_img = QImage(canvas_w, canvas_h, QImage.Format.Format_Grayscale8)
            shape_img.fill(Qt.GlobalColor.black)
            painter = QPainter(shape_img)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()

            # Shape position: original coordinates + padding offset
            # Shape size: based on original target_w/target_h (NOT padded canvas)
            if shape.type == "circle":
                rx = shape.size[0] * target_w
                ry = shape.size[1] * target_h
                # Render at canonical center (0.5, 0.5) for cache reuse across positions
                cx = 0.5 * target_w + pad
                cy = 0.5 * target_h + pad
                path.addEllipse(QPointF(cx, cy), rx, ry)
            elif shape.type == "rect":
                sw, sh = shape.size[0] * target_w, shape.size[1] * target_h
                # Render at canonical center (0.5, 0.5) for cache reuse across positions
                cx = 0.5 * target_w + pad
                cy = 0.5 * target_h + pad
                path.addRect(QRectF(cx - sw/2, cy - sh/2, sw, sh))
            elif shape.type == "curve" and shape.points:
                pts = shape.points
                path.moveTo(pts[0]['pos'][0] * target_w + pad, pts[0]['pos'][1] * target_h + pad)
                for i in range(len(pts)):
                    p1 = pts[i]
                    p2 = pts[(i + 1) % len(pts)]
                    path.cubicTo(
                        p1['c2'][0] * target_w + pad, p1['c2'][1] * target_h + pad,
                        p2['c1'][0] * target_w + pad, p2['c1'][1] * target_h + pad,
                        p2['pos'][0] * target_w + pad, p2['pos'][1] * target_h + pad
                    )
            painter.setBrush(QBrush(Qt.GlobalColor.white))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawPath(path)
            painter.end()

            ptr = shape_img.bits()
            ptr.setsize(canvas_w * canvas_h)
            shape_arr = np.frombuffer(ptr, np.uint8).reshape((canvas_h, canvas_w)).astype(np.float32) / 255.0

        # Gradient type
        if shape.type == "gradient" and hasattr(shape, 'grad_points'):
            p1 = np.array([shape.grad_points[0][0] * target_w + pad, shape.grad_points[0][1] * target_h + pad])
            p2 = np.array([shape.grad_points[1][0] * target_w + pad, shape.grad_points[1][1] * target_h + pad])
            yy, xx = np.mgrid[:canvas_h, :canvas_w]
            pixels = np.c_[xx.ravel(), yy.ravel()]
            vec = p2 - p1
            vec_len_sq = np.sum(vec**2)
            if vec_len_sq > 1e-6:
                t = np.sum((pixels - p1) * vec, axis=1) / vec_len_sq
                shape_arr = np.clip(t, 0, 1).reshape((canvas_h, canvas_w))
            else:
                shape_arr = np.zeros((canvas_h, canvas_w), dtype=np.float32)

        # NOTE: feather/blur/clean are applied AFTER rotation/translation/crop
        # in _render_single_shape to avoid edge clipping artifacts.
        # This function returns the sharp shape only.
        return shape_arr

    # ------------------------------------------------------------------
    # 跟踪结果应用（阶段 6 集成）：渲染时按当前帧求值蒙版几何
    # ------------------------------------------------------------------

    def _tracking_frame_context(self) -> tuple:
        """返回 (clip_id, source_frame)；无上下文时 clip_id 为空串。"""
        provider = getattr(self, "tracking_context_provider", None)
        if provider is None:
            return "", 0
        try:
            context = provider() or ("", 0)
        except Exception:  # noqa: BLE001 - 渲染路径不允许因求值失败中断
            logger.warning("tracking_context_provider 失败，回退原始几何",
                           exc_info=True)
            return "", 0
        clip_id = str(context[0] or "")
        try:
            frame = int(context[1] or 0)
        except (TypeError, ValueError):
            frame = 0
        return clip_id, frame

    def get_tracked_geometry(self, shape) -> Optional[dict]:
        """按当前帧求值形状的跟踪几何；无有效跟踪时返回 None。"""
        evaluator = getattr(self, "tracking_evaluator", None)
        if evaluator is None or self.img_full_original is None:
            return None
        clip_id, frame = self._tracking_frame_context()
        if not clip_id:
            return None
        h, w = self.img_full_original.shape[:2]
        return evaluator.evaluate_geometry(
            clip_id, shape.id, frame, (int(w), int(h)))

    def _tracked_shape_view(self, shape):
        """返回应用了当前帧跟踪变换的形状视图（不修改原始形状）。

        浅拷贝代理：几何属性被替换，其余参数（羽化/透明度等）共享；
        渲染缓存字段清空，避免污染原始形状的缓存。
        """
        geometry = self.get_tracked_geometry(shape)
        if geometry is None:
            return shape
        import copy as _copy
        proxy = _copy.copy(shape)
        proxy.center = list(geometry.get("center", shape.center))
        proxy.size = list(geometry.get("size", shape.size))
        proxy.rotation = float(geometry.get("rotation", shape.rotation) or 0.0)
        if "points" in geometry:
            proxy.points = geometry["points"]
        proxy._render_cache = None
        proxy._render_cache_key = None
        proxy._shape_cache = None
        proxy._shape_cache_key = None
        return proxy

    def _layer_contains_shape(self, layer, shape_id: str) -> bool:
        """递归判断层内（含群组嵌套）是否包含指定形状。"""
        def _walk(items) -> bool:
            for item in items:
                if item.id == shape_id:
                    return True
                if isinstance(item, MaskGroup) and _walk(item.children):
                    return True
            return False

        return _walk(layer.mask_shapes)

    def refresh_tracking_masks(self, source_frame=None) -> bool:
        """视频帧切换后重建受跟踪绑定影响的蒙版纹理。

        返回 True 表示至少一层纹理被更新（调用方负责触发视口重绘）。
        """
        if self.tracking_store.is_empty() or self.img_full_original is None:
            return False
        clip_id, current = self._tracking_frame_context()
        if not clip_id:
            return False
        frame = int(source_frame) if source_frame is not None else current
        changed = False
        for binding in self.tracking_store.bindings_for_clip(clip_id):
            # 无任何结果的绑定不参与渲染求值；范围内无结果帧的绑定
            # 也要置脏（当前帧需从变换位置回退到基准几何）
            if binding.frame_range_with_results() is None:
                continue
            if not binding.contains_frame(frame):
                continue
            for layer in self.layers:
                if self._layer_contains_shape(layer, binding.shape_id):
                    layer.mask_dirty = True
                    changed = True
                    break
        if changed:
            self._update_mask_textures()
        return changed

    def _render_mask_items_recursive(
        self, 
        items: List, 
        target_w: int, 
        target_h: int,
        img_w: int
    ) -> np.ndarray:
        """递归渲染遮罩元素列表，支持 MaskShape 和 MaskGroup。
        
        对于 MaskGroup，递归渲染其 children，然后应用群组级的 opacity、invert 和布尔运算。
        
        Args:
            items: 遮罩元素列表 (可包含 MaskShape 或 MaskGroup)
            target_w: 目标宽度（像素）
            target_h: 目标高度（像素）
            img_w: 原始图像宽度（用于笔刷缩放）
            
        Returns:
            float32 numpy 数组，范围 0.0-1.0，为合成后的蒙版
        """
        final_mask = np.zeros((target_h, target_w), dtype=np.float32)
        first_visible = True
        
        for item in items:
            # BUG-A2-04：未知类型过滤前移。原实现先解引用 item.visible 再做
            # isinstance 判定——杂项对象（旧序列化数据/编程错误）没有 visible
            # 属性时在到达"跳过未知类型"分支前就抛 AttributeError，防御分支
            # 不可达；先过滤未知类型再读 visible，MaskShape/MaskGroup 行为不变。
            if not isinstance(item, (MaskGroup, MaskShape)):
                continue
            if not item.visible:
                continue

            if isinstance(item, MaskGroup):
                # === 递归渲染群组 ===
                group_mask = self._render_mask_items_recursive(item.children, target_w, target_h, img_w)

                # 应用群组级 opacity
                group_mask = group_mask * item.opacity

                # 应用群组级 invert
                if item.invert:
                    group_mask = 1.0 - group_mask

                shape_arr = group_mask
                operation = item.operation

            else:
                # 循环开头的类型守卫已排除未知条目，此处必为 MaskShape。
                # === 渲染单个形状 ===
                # 跟踪集成：有跟踪绑定时按当前帧求值几何渲染（原始形状不被修改）
                shape_arr = self._render_single_shape(
                    self._tracked_shape_view(item), target_w, target_h, img_w)
                operation = item.operation
            
            # === 布尔运算混合 ===
            if first_visible:
                final_mask = shape_arr
                first_visible = False
            else:
                if operation == 'add':
                    final_mask = np.maximum(final_mask, shape_arr)
                elif operation == 'subtract':
                    final_mask = np.clip(final_mask - shape_arr, 0.0, 1.0)
                elif operation == 'intersect':
                    final_mask = final_mask * shape_arr
        
        return final_mask
    
    def _has_visible_mask_items(self, items: List) -> bool:
        """递归检查是否有可见的遮罩元素 (包括群组内的子元素)。
        
        Args:
            items: 遮罩元素列表
            
        Returns:
            bool: 是否存在可见元素
        """
        for item in items:
            if isinstance(item, MaskGroup):
                if item.visible and self._has_visible_mask_items(item.children):
                    return True
            elif isinstance(item, MaskShape):
                if item.visible:
                    return True
        return False

    def _update_mask_textures(self) -> None:
        """重栅格化所有 mask_dirty 层的蒙版并上传为单通道纹理。

        分辨率策略：交互中 512 基准宽（保实时性），静止 1024。CPU 侧
        递归合成形状 / MaskGroup / 跟踪视图 / AI matte 后，以
        UPDATE_AUX_TEX('mask') 提交 uint8 蒙版；无可见形状且无 AI
        matte 的层上传全零蒙版。蒙版宽高按源图纵横比换算。
        """
        if not self.renderer or self.img_full_original is None: return
        h, w = self.img_full_original.shape[:2]
        if self.is_interacting:
            base_size = 512
        else:
            base_size = 1024
            
        mask_w, mask_h = base_size, int(base_size * h / w)
        
        for layer in self.layers:
            if not layer.mask_dirty:
                continue
                
            has_ai_matte = layer.ai_matte is not None and layer.ai_matte_enabled
            # 检查是否有可见的遮罩元素 (递归检查，包括群组内的子元素)
            has_visible_items = self._has_visible_mask_items(layer.mask_shapes)
            if not has_visible_items and not has_ai_matte: 
                self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                    'layer_id': layer.id, 'type': 'mask', 'data': bytes(mask_w * mask_h), 'size': (mask_w, mask_h)
                })
                layer.mask_dirty = False
                continue
            
            # 使用递归渲染函数处理遮罩列表 (支持 MaskGroup)
            img_w = self.img_full_original.shape[1] if self.img_full_original is not None else 1000
            final_mask = self._render_mask_items_recursive(layer.mask_shapes, mask_w, mask_h, img_w)
            
            # AI Matte Integration
            if layer.ai_matte is not None and layer.ai_matte_enabled:
                ai_matte_resized = cv2.resize(layer.ai_matte, (mask_w, mask_h), interpolation=cv2.INTER_LINEAR)
                
                # 检查是否有任何可见元素
                has_visible_content = np.any(final_mask > 0)
                if not has_visible_content:
                    final_mask = ai_matte_resized
                else:
                    op = layer.ai_matte_operation
                    if op == 'add': final_mask = np.maximum(final_mask, ai_matte_resized)
                    elif op == 'subtract': final_mask = np.clip(final_mask - ai_matte_resized, 0.0, 1.0)
                    elif op == 'intersect': final_mask = final_mask * ai_matte_resized

            final_mask_u8 = (np.clip(final_mask, 0, 1) * 255).astype(np.uint8)
            self.renderer.submit(RenderCommand.UPDATE_AUX_TEX, {
                'layer_id': layer.id, 'type': 'mask', 'data': np.ascontiguousarray(final_mask_u8).tobytes(), 'size': (mask_w, mask_h)
            })
            layer.mask_dirty = False

    def _next_render_request_id(self) -> str:
        """Return a process-local, path-free ID for one submitted render."""
        self._render_request_counter = int(
            getattr(self, "_render_request_counter", 0) or 0
        ) + 1
        return f"render-{self._render_request_counter:08d}"

    def render_pipeline(
        self,
        is_full_res=False,
        is_preview=False,
        render_reason='full',
        render_request_id: Optional[str] = None,
    ):
        """Run the GPU render pipeline.

        Args:
            is_full_res: If True, submit a critical full-resolution render to
                ``AsyncRenderer`` (e.g. for export/apply).  The renderer owns
                texture creation, execution, synchronization, and readback.
            is_preview: If True, skip viewport-side rendering.
            render_reason: Why this render was triggered.  Accepted values:
                'full'            – default; updates mask textures and marks ColorOutput
                                    dirty.  Used for grading changes, layer changes,
                                    LUT edits, etc.
                'viewport_refresh' – lightweight refresh caused by zoom / pan / resize.
                                     Skips mask texture updates and ColorOutput dirty
                                     marking so transient viewport changes do not
                                     pollute the Composite page output state.
                'playback'         – video playback frame refresh.  Same lightweight
                                     behaviour as viewport_refresh.
                'grading'          – synonymous with 'full'; kept for readability at
                                     call-sites to indicate a user-driven grading
                                     parameter change.
        """
        # P0-COL-04：渲染前校验 baked 前缀摘要，下层已变更的冻结层自动解冻，
        # 保证「bake = 到该层为止的完整栈前缀」语义始终成立。
        self._validate_baked_prefixes()
        # P4-2 SAFETY: Detect paint/render cycle bug
        # If Canvas.drawBackground() is currently executing, calling render_pipeline
        # would cause: paint -> submit render -> frame_ready -> repaint -> paint loop
        # This is the root cause of video stuttering and jittering.
        if self.canvas_ref and getattr(self.canvas_ref, '_in_draw_background', False):
            logger.error(
                "P4-2 BUG DETECTED: render_pipeline() called during Canvas.drawBackground()! "
                "This causes paint-render-repaint cycle. "
                "Call stack should be checked for the bug source."
            )
            # In debug mode, raise an exception to break the cycle
            # In production, just log and return to prevent the cycle
            return

        mutex = self.gl_manager.mutex
        # Transient renders (viewport refresh, playback, grading) should skip
        # heavy mask updates and ColorOutput dirty marking. Grading changes
        # (curves, wheels, sliders) only affect the Color pipeline, not masks.
        _transient = render_reason in ('viewport_refresh', 'playback', 'grading')
        if not is_preview:
            if is_full_res or not _transient:
                self._update_mask_textures()
            # Grading previews intentionally skip expensive mask work and use a
            # merge lane, but they still change pixels.  Publish a new color
            # revision so Composite/monitor consumers cannot retain a stale
            # grade after a slider, wheel, or reset gesture.
            if not is_full_res and render_reason not in ('viewport_refresh', 'playback'):
                self._color_output.mark_dirty()

        if self.renderer and not is_preview:
            if is_full_res and self.img_full_original is not None:
                full_h, full_w = self.img_full_original.shape[:2]
                v_rect = (0.0, 0.0, float(full_w), float(full_h))
            else:
                v_rect = self.visible_rect
            if v_rect[2] <= 1.0 and self.img_full_original is not None:
                scene_size = self._get_video_scene_size() if self._is_video_mode else None
                if scene_size is not None:
                    w, h = scene_size
                else:
                    h, w = self.img_full_original.shape[:2]
                v_rect = (0, 0, float(w), float(h))
                self.visible_rect = v_rect

            eff_visible_rect = v_rect
            grain_visible_rect = v_rect  # Film Grain 使用原始图像坐标
            video_source_size = None if is_full_res else self._get_active_video_render_size()
            if video_source_size is not None:
                scene_size = self._get_video_scene_size()
                if scene_size is not None:
                    scene_w, scene_h = scene_size
                    video_w, video_h = video_source_size
                    if abs(scene_w - video_w) > 0.5 or abs(scene_h - video_h) > 0.5:
                        scale_x = video_w / scene_w
                        scale_y = video_h / scene_h
                        vx, vy, vw, vh = v_rect
                        eff_visible_rect = (
                            vx * scale_x,
                            vy * scale_y,
                            vw * scale_x,
                            vh * scale_y,
                        )
            elif self.img_full_original is not None and self.img_high is not None:
                h_full, w_full = self.img_full_original.shape[:2]
                h_gpu, w_gpu = self.img_high.shape[:2]
                
                if w_full != w_gpu or h_full != h_gpu:
                    scale_x = w_gpu / w_full
                    scale_y = h_gpu / h_full
                    vx, vy, vw, vh = v_rect
                    eff_visible_rect = (vx * scale_x, vy * scale_y, vw * scale_x, vh * scale_y)
                    # grain_visible_rect 保持原始坐标不变，让着色器使用一致的坐标系统

            layers_data = [_layer_render_data(layer) for layer in self.layers]
            
            # GPU 硬件 Mipmap 优化：LOD 始终为 0
            # GPU 会通过 LINEAR_MIPMAP_LINEAR 过滤器自动根据 Screen-space derivatives
            # 选择最佳 Mipmap 层级，Python 端无需手动计算 LOD
            active_lod = 0

            # During playback, only the VIDEO_PLAYBACK lane may publish the
            # current frame. A grading RENDER_FRAME followed by the next
            # playback tick would otherwise alternate between the new grade
            # and the cached old grade, which appears as a two-frame flash.
            # Invalidate the playback snapshot and let the next tick render it
            # once with the new parameters.
            if not is_full_res and self._is_video_mode and render_reason in ('grading', 'full'):
                video_controller = None
                if self.canvas_ref is not None:
                    try:
                        window = self.canvas_ref.window()
                        video_controller = getattr(window, 'video_controller', None)
                    except Exception:
                        video_controller = None
                if bool(getattr(video_controller, 'is_playing', False)):
                    invalidate = getattr(video_controller, 'invalidate_cached_render_params', None)
                    if callable(invalidate):
                        invalidate()
                    return

            # 统一提取 Raw 参数
            curr = self.current_raw_params
            last_decode = self._last_applied_raw_params
            
            baseline_wb = self.current_raw_metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
            last_wb = last_decode.get('user_wb', baseline_wb)
            curr_wb = curr.get('user_wb', last_wb)
            
            gains = (
                float(curr_wb[0] / max(1e-4, last_wb[0])),
                float(curr_wb[1] / max(1e-4, last_wb[1])),
                float(curr_wb[2] / max(1e-4, last_wb[2]))
            )
            
            cs_map = {
                "sRGB": 0, "Rec.709": 1, "Rec.2020": 2, "Adobe RGB": 3, "DCI-P3": 4, "Display P3": 5,
                "ACEScg": 6, "ARRI Wide Gamut 3": 7, "Sony S-Gamut3": 8, "Panasonic V-Gamut": 9, "DaVinci Wide Gamut": 10, 
                "REDWideGamutRGB": 11, "Canon Cinema Gamut": 12, "Fujifilm F-Gamut": 13, "BMD Film Gen5": 14, "CMYK Simulation": 15
            }
            gamma_map = {
                "Linear": 0, "sRGB": 1, "Rec.709": 2, "Gamma 2.2": 3, "Gamma 2.4": 4, "Gamma 2.6": 5, 
                "Rec.2100 HLG": 6, "sLog3": 7, "ARRI LogC3": 8, "ACEScct": 9, "Panasonic V-Log": 10, "DaVinci Intermediate": 11,
                "RED Log3G10": 12, "Canon Log 2": 13, "Canon Log 3": 14, "Fujifilm F-Log": 15, "Fujifilm F-Log2": 16, "Apple Log": 17
            }

            # Fix for black/magenta screen on non-RAW images:
            # Bypass color space conversions if RAW processing is not explicitly enabled for sRGB sources.
            eff_is_srgb = self.is_current_image_srgb
            eff_color_space = cs_map.get(curr.get('color_space', "sRGB"), 0)
            
            # Robustly determine if we should bypass gamma/color management for sRGB images
            is_raw_path = self.current_file_path and os.path.splitext(self.current_file_path)[1].lower() in self.RAW_EXTENSIONS
            is_effectively_srgb = self.is_current_image_srgb or (not is_raw_path)
            
            # If it's effectively an sRGB image and RAW module is disabled, force bypass
            if is_effectively_srgb and not curr.get("enabled", False):
                eff_is_srgb = False  # Skip Input -> ProPhoto
                eff_color_space = -1 # Skip ProPhoto -> Output
                eff_gamma = 0        # Skip Gamma Encoding (Pass-through)
                gains = (1.0, 1.0, 1.0) # Force identity gains to prevent tint bugs
                bypass_raw = True
            else:
                # Normal path (RAW file, or sRGB with Raw module enabled)
                bypass_raw = False
                eff_gamma = gamma_map.get(curr.get('gamma', "Linear"), 0)
                
                # Special case: If we are an sRGB image but Raw module is enabled,
                # the default 'gamma' from current_raw_params might have been 'sRGB' (1) 
                # if the logic in set_image failed or was bypassed.
                # But our new set_image fix sets it to 'Linear' (0) by default for sRGB.

            if bypass_raw:
                raw_params = {
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
            else:
                raw_params = {
                    'exposure': float(curr.get('exposure', 0.0)),
                    'gains': gains,
                    'color_space': eff_color_space,
                    'gamma': eff_gamma,
                    'is_srgb_source': eff_is_srgb,
                    'shadows': float(curr.get('shadows', 0.0)),
                    'highlights': float(curr.get('highlights', 0.0)),
                    'contrast': float(curr.get('contrast', 1.0)),
                    'saturation': float(curr.get('saturation', 100.0) / 100.0),
                    'lift': float(curr.get('lift', 0.0)),
                    'gain': float(curr.get('gain', 1.0)),
                    'color_boost': float(curr.get('color_boost', 0.0)),
                    'mid_detail': float(curr.get('mid_detail', 0.0)),
                    'sharpness': float(curr.get('sharpness', 0.0)),
                }

            render_params = {
                'render_request_id': (
                    str(render_request_id)
                    if render_request_id
                    else self._next_render_request_id()
                ),
                'viewport_size': (
                    (int(self.img_full_original.shape[1]), int(self.img_full_original.shape[0]))
                    if is_full_res and self.img_full_original is not None
                    else self.viewport_size
                ),
                'visible_rect': eff_visible_rect,
                'scene_visible_rect': v_rect,  # Original scene coords for UV adjustment
                'grain_visible_rect': grain_visible_rect,  # Film Grain 专用：原始图像坐标
                'current_lod': active_lod,
                'layers': layers_data,
                'show_mask': self.show_mask_mode,
                'mask_preview_style': self.mask_preview_style,
                'active_layer_index': self.active_layer_index,
                'film_grain': self._effective_film_grain_params(),
                'raw_preview': raw_params, # 始终传递参数，由 Shader 统一处理
                'render_reason': render_reason,
                # P1-COLOR-13: 全分辨率渲染（导出/应用更改）走降噪最高档
                'video_denoise_quality_mode': 'export' if is_full_res else None,
                # Snapshot the renderer's video generation so _do_render can drop
                # a stale grading/refresh command that crosses a clip switch.
                # RENDER_FRAME has no built-in generation gate (unlike
                # RENDER_VIDEO_FRAME); without this, a queued grading refresh
                # submitted before a clip switch can repaint the departing
                # clip's video texture after UPLOAD_IMAGE has already replaced
                # it, briefly flashing the previous shot on Play.
                'video_generation': int(getattr(self.renderer, '_video_generation_id', 0) or 0),
            }
            if is_full_res:
                # CPU-side colour conversion is deliberately limited to
                # preparing immutable data.  All GL work and the readback are
                # performed by AsyncRenderer on its own context/thread.
                source = self.img_full_original
                if source is None:
                    return None
                if source.ndim == 2:
                    source = cv2.cvtColor(source, cv2.COLOR_GRAY2RGB)
                else:
                    source = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
                if source.dtype == np.uint8:
                    source = source.astype(np.float32) / 255.0
                elif source.dtype == np.uint16:
                    source = source.astype(np.float32) / 65535.0
                else:
                    source = source.astype(np.float32, copy=False)
                render_params.update({
                    'source_image': np.ascontiguousarray(source, dtype=np.float32),
                    'color_output_mode': True,
                    'color_output_target': 'full_res',
                    'full_resolution_request_id': render_params['render_request_id'],
                    'render_reason': 'full_res',
                })
                register = getattr(self.renderer, 'register_full_resolution', None)
                if callable(register) and not register(render_params['render_request_id']):
                    logger.warning(
                        "Full-resolution request already registered: %s",
                        render_params['render_request_id'],
                    )
                    return None
                try:
                    self.renderer.submit(RenderCommand.RENDER_FULL_RESOLUTION, render_params)
                except Exception:
                    forget = getattr(self.renderer, 'forget_full_resolution', None)
                    if callable(forget):
                        forget(render_params['render_request_id'])
                    raise
            else:
                self.renderer.submit(RenderCommand.RENDER_FRAME, render_params)
            return render_params['render_request_id']

        if not self.gl_manager.initialized: return
        mutex.lock()
        try:
            self._init_gl_resources()
            if is_full_res:
                self._do_render_full_res(
                    render_request_id=(
                        str(render_request_id)
                        if render_request_id
                        else self._next_render_request_id()
                    )
                )
        finally:
            mutex.unlock()

    def _get_active_video_render_size(self) -> Optional[tuple[float, float]]:
        """Return the currently active video texture size, if the renderer is showing video."""
        if not self._is_video_mode or self.renderer is None:
            return None

        texture = getattr(self.renderer, 'current_video_texture', None)
        if texture is not None:
            width, height = getattr(texture, 'size', (0, 0))
            if width > 0 and height > 0:
                return (float(width), float(height))

        planes = getattr(self.renderer, 'current_video_planes', None)
        if planes:
            first_plane = planes[0]
            width, height = getattr(first_plane, 'size', (0, 0))
            if width > 0 and height > 0:
                return (float(width), float(height))

        video_size = getattr(self.renderer, 'video_source_size', (0, 0))
        if video_size and video_size[0] > 0 and video_size[1] > 0:
            return (float(video_size[0]), float(video_size[1]))

        if self.img_full_original is not None:
            height, width = self.img_full_original.shape[:2]
            if width > 0 and height > 0:
                return (float(width), float(height))

        return None

    def _get_video_scene_size(self) -> Optional[tuple[float, float]]:
        """Return the coordinate space used by visible_rect for the current video."""
        scene_size = getattr(self, 'scene_size', None)
        if (
            scene_size
            and len(scene_size) == 2
            and scene_size[0] > 0
            and scene_size[1] > 0
        ):
            return (float(scene_size[0]), float(scene_size[1]))

        if self.img_full_original is not None:
            height, width = self.img_full_original.shape[:2]
            if width > 0 and height > 0:
                return (float(width), float(height))

        return None

    def _do_render_full_res(self, *, render_request_id: str | None = None):
        """
        Full-resolution rendering using Ping-Pong framebuffers.
        This ensures that blur and other effects are applied correctly,
        with each layer's output feeding into the next layer's input.
        """
        texture_garbage = []
        # AUD-0607：本方法内即时创建（非 pipeline comp_cache 托管）的
        # program 统一登记，finally 释放——此前 prog_raw/prog_pass 任何
        # 路径都不释放，反复导出持续累积 GL program。
        program_garbage = []
        raw_dev_fbo = None
        ping_fbo = None
        pong_fbo = None
        try:
            if self.img_full_original is None: return
            h, w = self.img_full_original.shape[:2]
            ctx = self.gl_manager.ctx
            aspect_ratio = w / h if h != 0 else 1.0

            # 设置 viewport 为全分辨率，避免与显示 viewport 尺寸不匹配导致扭曲
            ctx.viewport = (0, 0, w, h)

            # 重置 quad_vbo 为标准全屏四边形，防止显示管线修改的 UV 坐标污染导出渲染
            standard_vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
            self.quad_vbo.write(standard_vertices)

            # --- Step 1: Upload and Raw Development ---
            rgb_full = cv2.cvtColor(self.img_full_original, cv2.COLOR_BGR2RGB)
            if rgb_full.dtype == np.uint8:
                input_data = rgb_full.astype(np.float32) / 255.0
            elif rgb_full.dtype == np.uint16:
                input_data = rgb_full.astype(np.float32) / 65535.0
            else:
                input_data = rgb_full.astype(np.float32)
            
            input_data = np.ascontiguousarray(input_data, dtype='f4')
            # AUD-0607：GPU 资源创建后立即登记 deferred-release 清单——
            # 此前 input_tex/raw_dev_tex 直到 Step 2 才入清单，期间任何
            # 异常（着色器编译失败/GL 错误）都会泄漏纹理。
            input_tex = ctx.texture((w, h), 3, data=input_data.tobytes(), dtype='f4')
            input_tex.repeat_x = input_tex.repeat_y = False
            texture_garbage.append(input_tex)

            # Raw development pass
            raw_dev_tex = ctx.texture((w, h), 4, dtype='f4')
            texture_garbage.append(raw_dev_tex)
            raw_dev_fbo = ctx.framebuffer(color_attachments=[raw_dev_tex])
            raw_dev_fbo.use()
            ctx.clear(0, 0, 0, 1)
            
            from src.features.color.shaders.shaders import PASSTHROUGH_VERTEX, RAW_VIEW_FRAGMENT
            prog_raw = ctx.program(vertex_shader=PASSTHROUGH_VERTEX, fragment_shader=RAW_VIEW_FRAGMENT)
            # AUD-0607：本方法内即时创建的 program（非 pipeline 托管缓存）
            # 统一登记，finally 一并释放
            program_garbage.append(prog_raw)
            
            input_tex.use(0)
            prog_raw['u_texture'].value = 0
            
            curr = self.current_raw_params
            last_decode = self._last_applied_raw_params
            baseline_wb = self.current_raw_metadata.get("camera_wb", [2.0, 1.0, 1.5, 1.0])
            last_wb = last_decode.get('user_wb', baseline_wb)
            curr_wb = curr.get('user_wb', last_wb)
            
            gains = (
                float(curr_wb[0] / max(1e-4, last_wb[0])),
                float(curr_wb[1] / max(1e-4, last_wb[1])),
                float(curr_wb[2] / max(1e-4, last_wb[2]))
            )
            
            cs_map = {
                "sRGB": 0, "Rec.709": 1, "Rec.2020": 2, "Adobe RGB": 3, "DCI-P3": 4, "Display P3": 5,
                "ACEScg": 6, "ARRI Wide Gamut 3": 7, "Sony S-Gamut3": 8, "Panasonic V-Gamut": 9, "DaVinci Wide Gamut": 10, 
                "REDWideGamutRGB": 11, "Canon Cinema Gamut": 12, "Fujifilm F-Gamut": 13, "BMD Film Gen5": 14, "CMYK Simulation": 15
            }
            gamma_map = {
                "Linear": 0, "sRGB": 1, "Rec.709": 2, "Gamma 2.2": 3, "Gamma 2.4": 4, "Gamma 2.6": 5, 
                "Rec.2100 HLG": 6, "sLog3": 7, "ARRI LogC3": 8, "ACEScct": 9, "Panasonic V-Log": 10, "DaVinci Intermediate": 11,
                "RED Log3G10": 12, "Canon Log 2": 13, "Canon Log 3": 14, "Fujifilm F-Log": 15, "Fujifilm F-Log2": 16, "Apple Log": 17
            }
            
            is_raw_path = self.current_file_path and os.path.splitext(self.current_file_path)[1].lower() in self.RAW_EXTENSIONS
            is_effectively_srgb = self.is_current_image_srgb or (not is_raw_path)

            if is_effectively_srgb and not curr.get("enabled", False):
                prog_raw['u_raw_prev_exposure'].value = 0.0
                prog_raw['u_raw_prev_gains'].value = (1.0, 1.0, 1.0)
                prog_raw['u_raw_lift'].value = 0.0
                prog_raw['u_raw_gain'].value = 1.0
                prog_raw['u_raw_contrast'].value = 1.0
                prog_raw['u_raw_saturation'].value = 1.0
                prog_raw['u_raw_shadows'].value = 0.0
                prog_raw['u_raw_highlights'].value = 0.0
                prog_raw['u_raw_color_boost'].value = 0.0
                prog_raw['u_raw_mid_detail'].value = 0.0
                prog_raw['u_raw_sharpness'].value = 0.0
                prog_raw['u_raw_color_space'].value = -1
                prog_raw['u_raw_gamma'].value = 0
                prog_raw['u_is_srgb_source'].value = False
            else:
                prog_raw['u_raw_prev_exposure'].value = float(curr.get('exposure', 0.0))
                prog_raw['u_raw_prev_gains'].value = gains
                prog_raw['u_raw_lift'].value = float(curr.get('lift', 0.0))
                prog_raw['u_raw_gain'].value = float(curr.get('gain', 1.0))
                prog_raw['u_raw_contrast'].value = float(curr.get('contrast', 1.0))
                prog_raw['u_raw_saturation'].value = float(curr.get('saturation', 100.0) / 100.0)
                prog_raw['u_raw_shadows'].value = float(curr.get('shadows', 0.0))
                prog_raw['u_raw_highlights'].value = float(curr.get('highlights', 0.0))
                prog_raw['u_raw_color_boost'].value = float(curr.get('color_boost', 0.0))
                prog_raw['u_raw_mid_detail'].value = float(curr.get('mid_detail', 0.0))
                prog_raw['u_raw_sharpness'].value = float(curr.get('sharpness', 0.0))
                prog_raw['u_raw_color_space'].value = cs_map.get(curr.get('color_space', "sRGB"), 0)
                prog_raw['u_raw_gamma'].value = gamma_map.get(curr.get('gamma', "Linear"), 0)
                prog_raw['u_is_srgb_source'].value = is_effectively_srgb

            input_lut_path = curr.get('input_lut')
            raw_lut_tex_full = None
            if input_lut_path and os.path.exists(input_lut_path):
                raw_lut_tex_full = self._load_lut_sync(ctx, input_lut_path)
                # AUD-0607：加载的 LUT 纹理入清单，异常路径同样释放
                if raw_lut_tex_full is not None:
                    texture_garbage.append(raw_lut_tex_full)

            if raw_lut_tex_full:
                raw_lut_tex_full.use(1)
                prog_raw['u_lut_tex'].value = 1
                prog_raw['u_lut_enabled'].value = True
            else:
                data = np.array([0,0,0, 1,0,0, 0,1,0, 1,1,0, 0,0,1, 1,0,1, 0,1,1, 1,1,1], dtype='f4').tobytes()
                raw_lut_tex_full = ctx.texture3d((2, 2, 2), 3, data=data, dtype='f4')
                raw_lut_tex_full.filter = (moderngl.LINEAR, moderngl.LINEAR)
                texture_garbage.append(raw_lut_tex_full)
                raw_lut_tex_full.use(1)
                prog_raw['u_lut_tex'].value = 1
                prog_raw['u_lut_enabled'].value = False

            # AUD-0607：raw pass 的 VBO/VAO 用 try/finally 释放，
            # 渲染抛异常时不脱离 GL 对象管理
            vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
            vbo_raw = ctx.buffer(vertices)
            vao_raw = None
            try:
                vao_raw = ctx.vertex_array(prog_raw, [(vbo_raw, '2f 2f', 'in_vert', 'in_texcoord')])
                vao_raw.render(moderngl.TRIANGLE_STRIP)
            finally:
                if vao_raw is not None:
                    vao_raw.release()
                vbo_raw.release()
            
            # --- Step 2: Ping-Pong Layer Rendering ---
            # Create ping-pong framebuffers
            ping_tex = ctx.texture((w, h), 4, dtype='f4')
            pong_tex = ctx.texture((w, h), 4, dtype='f4')
            # AUD-0607：创建即登记（此前延迟到本行下方统一登记，中间的
            # FBO/program 创建异常会泄漏两张全分辨率纹理）
            texture_garbage.append(ping_tex)
            texture_garbage.append(pong_tex)
            ping_fbo = ctx.framebuffer(color_attachments=[ping_tex])
            pong_fbo = ctx.framebuffer(color_attachments=[pong_tex])
            
            # Initialize ping buffer with raw development result
            ping_fbo.use()
            ctx.clear(0, 0, 0, 1)
            raw_dev_tex.use(0)
            prog_pass = self.gl_manager.ctx.program(
                vertex_shader=PASSTHROUGH_VERTEX,
                fragment_shader="""
                #version 330
                uniform sampler2D u_texture;
                in vec2 v_texcoord;
                out vec4 f_color;
                void main() {
                    vec3 rgb = texture(u_texture, v_texcoord).rgb;
                    f_color = vec4(rgb, 1.0);
                }
                """
            )
            program_garbage.append(prog_pass)
            temp_vao = ctx.vertex_array(prog_pass, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
            temp_vao.render(moderngl.TRIANGLE_STRIP)
            temp_vao.release()

            # Render each layer sequentially
            visible_count = 0
            for i, layer in enumerate(self.layers):
                if not layer.visible:
                    continue

                # Determine source and target for this layer (ping-pong)
                # 使用可见图层计数器而非图层索引，避免不可见图层打断交替顺序
                if visible_count % 2 == 0:
                    source_tex = ping_tex
                    target_fbo = pong_fbo
                else:
                    source_tex = pong_tex
                    target_fbo = ping_fbo
                
                target_fbo.use()
                ctx.clear(0, 0, 0, 1)

                # P0-COL-03：AI 结果纹理是该层的新 source——全分辨率路径与
                # 预览同语义消费（此前此路径完全不读 ai_plugin_textures，
                # 导出/全分辨率输出丢失全部 AI 效果）。
                ai_tex = self._get_layer_ai_texture(layer)

                # Generate full-resolution matte for this layer (including qualifier)
                # Phase 2: Use _generate_full_res_matte which includes qualifier + mask_shapes + ai_matte + blur/clean
                # Qualifier 从该层的实际 source 采样颜色：有 AI 纹理时与预览
                # 一致地以 AI 结果为采样源，否则用 raw_dev_tex。
                mask_tex = self._generate_full_res_matte(
                    layer,
                    ai_tex if ai_tex is not None else raw_dev_tex,
                    w, h)
                
                # AUD-0607：蒙版纹理来自 fb_manager 临时纹理池，创建后
                # 任何异常都必须归还池管理（此前仅正常路径/编译失败
                # 路径释放，uniform 上传或 aux 纹理创建抛异常时脱池）。
                try:
                    # Build composition shader for this layer
                    has_curves = self.pipeline.has_active_curves_lut(layer.curves_lut)
                    has_warper = layer.warper_params is not None
                    has_lut3d = layer.active_lut_path is not None
                    scp = layer.soft_clip_params or {}
                    has_soft_clip = scp.get('enabled', False)
                
                    # Phase 2: Matte already includes qualifier, so disable shader-side qualifier
                    has_qualifier = False
                
                    has_hsl_curve = self.pipeline.has_active_hsl_curve_lut(layer.hsl_curve_lut)
                    has_mixer = self.pipeline.has_active_mixer_params(layer.mixer_params)

                    # Get plugins for this layer（P7-01：解析为权威 PluginBinding，
                    # index 保留原始占位——加载失败的插件不影响后续插件前缀）
                    from src.features.color.core.shader_compiler import (
                        plugin_bindings_from_instances,
                    )
                    layer_plugins = plugin_bindings_from_instances(
                        layer.plugins) if getattr(layer, 'plugins', None) else []

                    prog_comp = self.pipeline.build_composition_shader(
                        has_curves=has_curves,
                        has_warper=has_warper,
                        has_lut3d=has_lut3d,
                        has_soft_clip=has_soft_clip,
                        grain_enabled=False,
                        has_qualifier=has_qualifier,
                        has_hsl_curve=has_hsl_curve,
                        has_mixer=has_mixer,
                        plugins=layer_plugins,
                        request_id=render_request_id,
                        layer_id=layer.id,
                    )

                    if not prog_comp:
                        # Full-resolution output is an export artifact.  A missing
                        # layer must fail closed instead of producing a seemingly
                        # successful image with silently dropped effects.
                        logger.error(
                            "Full-resolution render aborted: composition shader "
                            "failed for layer_id=%s",
                            layer.id,
                        )
                        return
                
                    # Bind source texture（有 AI 结果时以 AI 纹理为本层输入，
                    # pass 输出进入 ping-pong 供后续层消费）
                    (ai_tex if ai_tex is not None else source_tex).use(0)
                    prog_comp['u_input_tex'].value = 0
                
                    # Bind mask texture
                    mask_tex.use(1)
                    prog_comp['u_matte_tex'].value = 1
                
                    # Update uniforms（P7-02：值提取与上传统一由
                    # composition_uniforms 承载——layer_plugins 已是 P7-01 的
                    # 权威 PluginBinding 列表，uniform 名/插件索引与编译侧
                    # manifest 天然对齐）
                    from src.features.color.core.composition_uniforms import (
                        composition_uniform_values,
                        upload_composition_uniforms,
                    )
                    values = composition_uniform_values(
                        layer,
                        width=w, height=h,
                        aspect_ratio=aspect_ratio,
                        tex_size=(float(w), float(h)),
                        bindings=layer_plugins,
                    )
                    upload_composition_uniforms(prog_comp, values)

                    # Bind auxiliary textures
                    tex_unit = 2

                    # Curves
                    if has_curves:
                        lut_data = np.array(layer.curves_lut)
                        if lut_data.ndim == 2:
                            lut_data = lut_data.reshape(256, 1, 3)
                        lut_f32 = lut_data.astype(np.float32)
                        c_tex = ctx.texture((256, 1), 3, data=lut_f32.tobytes(), dtype='f4')
                        c_tex.repeat_x = c_tex.repeat_y = False
                        c_tex.use(tex_unit)
                        prog_comp['u_curves_tex'].value = tex_unit
                        texture_garbage.append(c_tex)
                        tex_unit += 1

                    # HSL Curve (6 curves, 256 samples each)
                    if has_hsl_curve:
                        hsl_lut_data = np.array(layer.hsl_curve_lut, dtype=np.float32)
                        # HSL curve LUT is (6, 256): 6 curves, each with 256 samples
                        # Transpose to (256, 6) so each column is a curve for texture sampling
                        # Texture size: width=6 (curves), height=256 (samples)
                        hsl_lut_f32 = hsl_lut_data.T.astype(np.float32)
                        hsl_tex = ctx.texture((6, 256), 1, data=hsl_lut_f32.tobytes(), dtype='f4')
                        hsl_tex.repeat_x = hsl_tex.repeat_y = False
                        hsl_tex.use(tex_unit)
                        prog_comp['u_hsl_curve_tex'].value = tex_unit
                        texture_garbage.append(hsl_tex)
                        tex_unit += 1

                    # Warper
                    if has_warper:
                        wp = np.array(layer.warper_params)
                        wh, ww = wp.shape[:2]
                        w_tex = ctx.texture((ww, wh), 2, data=wp.astype(np.float32).tobytes(), dtype='f4')
                        w_tex.repeat_x = w_tex.repeat_y = False
                        w_tex.use(tex_unit)
                        prog_comp['u_warper_tex'].value = tex_unit
                        prog_comp['u_warper_grid_size'].value = float(wh)
                        texture_garbage.append(w_tex)
                        tex_unit += 1
                
                    # 3D LUT
                    if has_lut3d:
                        lut_tex = self._load_lut_sync(ctx, layer.active_lut_path)
                        if lut_tex:
                            lut_tex.use(tex_unit)
                            prog_comp['u_lut_tex'].value = tex_unit
                            prog_comp['u_lut_intensity'].value = float(layer.lut_intensity)
                            texture_garbage.append(lut_tex)
                            tex_unit += 1
                
                    # Render the layer
                    temp_vao = ctx.vertex_array(prog_comp, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
                    temp_vao.render(moderngl.TRIANGLE_STRIP)
                    temp_vao.release()
                

                    visible_count += 1
                finally:
                    if mask_tex is not self.white_tex:
                        self.fb_manager.release_temp_texture(mask_tex)

            # --- Step 3: Global Effects (Film Grain) ---
            # 使用可见图层计数确定最终结果所在的 FBO
            current_fbo = pong_fbo if visible_count % 2 == 1 else ping_fbo
            
            if self.film_grain_params.get('enabled', False):
                # Switch to the other FBO for grain pass
                source_tex = ping_tex if current_fbo == ping_fbo else pong_tex
                target_fbo = pong_fbo if current_fbo == ping_fbo else ping_fbo
                
                target_fbo.use()
                ctx.clear(0, 0, 0, 1)
                
                # Reuse composition shader for grain
                prog_grain = self.pipeline.build_composition_shader(
                    grain_enabled=True,
                    request_id=render_request_id,
                )
                
                source_tex.use(0)
                prog_grain['u_input_tex'].value = 0
                
                # Dummy matte
                white_data = np.array([1.0], dtype='f4').tobytes()
                dummy_matte = ctx.texture((1, 1), 1, data=white_data, dtype='f4')
                # AUD-0607：dummy matte 入 deferred-release 清单，渲染异常
                # 时同样释放（此前仅成功路径 release）
                texture_garbage.append(dummy_matte)
                dummy_matte.use(1)
                prog_grain['u_matte_tex'].value = 1
                
                # Set opacity to 0 to only apply global effects (grain)
                prog_grain['u_opacity'].value = 0.0
                prog_grain['u_blend_mode'].value = 0
                
                # Set Grain Uniforms
                g = self._effective_film_grain_params()
                prog_grain['u_grain_intensity'].value = float(g.get('intensity', 0.0))
                prog_grain['u_grain_size'].value = float(g.get('size', 1.0))
                prog_grain['u_grain_monochrome'].value = bool(g.get('monochrome', True))
                prog_grain['u_grain_shadows'].value = float(g.get('shadows', 0.0))
                prog_grain['u_grain_midtones'].value = float(g.get('midtones', 1.0))
                prog_grain['u_grain_highlights'].value = float(g.get('highlights', 0.0))
                prog_grain['u_grain_seed'].value = float(g.get('seed', 0.0))
                prog_grain['u_grain_aspect_ratio'].value = aspect_ratio
                
                if 'u_tex_size' in prog_grain:
                    prog_grain['u_tex_size'].value = (float(w), float(h))
                
                # Film Grain Image-space 绑定：全分辨率导出时，视口即全图
                if 'u_visible_rect' in prog_grain:
                    prog_grain['u_visible_rect'].value = (0.0, 0.0, float(w), float(h))
                
                temp_vao = ctx.vertex_array(prog_grain, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
                temp_vao.render(moderngl.TRIANGLE_STRIP)
                temp_vao.release()
                current_fbo = target_fbo

            # --- Step 4: Read back final result ---
            current_fbo.use()
            data = current_fbo.read(components=4, alignment=1, dtype='f4')
            img_rgba = np.frombuffer(data, dtype=np.float32).reshape(h, w, 4)
            img_rgb = img_rgba[:, :, :3].copy()
            self.temp_image = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)

        except Exception as e:
            logger.error(f"-Res Render Error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Cleanup GPU resources regardless of success or failure
            # （AUD-0607：except 收窄为 Exception，禁止裸 except 吞掉
            # SystemExit/KeyboardInterrupt；program 清单一并释放）
            for t in texture_garbage:
                try:
                    t.release()
                except Exception:
                    logger.debug("Failed to release texture in texture_garbage cleanup")
            for prog in program_garbage:
                try:
                    prog.release()
                except Exception:
                    logger.debug("Failed to release program in program_garbage cleanup")
            if raw_dev_fbo is not None:
                try: raw_dev_fbo.release()
                except Exception: logger.debug("Failed to release raw_dev_fbo in cleanup")
            if ping_fbo is not None:
                try: ping_fbo.release()
                except Exception: logger.debug("Failed to release ping_fbo in cleanup")
            if pong_fbo is not None:
                try: pong_fbo.release()
                except Exception: logger.debug("Failed to release pong_fbo in cleanup")

    def _generate_full_res_matte(self, layer, input_tex, w, h):
        """Generate full-resolution matte including qualifier, mask_shapes, and postprocess.
        
        Phase 2: This is the unified matte generation path, matching AsyncRenderer._generate_source_matte().
        Steps:
        1. Keyer pass (qualifier on source texture)
        2. Combine with mask_shapes and ai_matte
        3. Postprocess (clean_black, clean_white, blur)
        
        Returns a single-channel f2 texture at (w, h).
        """
        ctx = self.gl_manager.ctx
        qp = layer.qualifier_params or {}
        ms = layer.mask_shapes or []
        
        has_qualifier = qp.get('enabled', False)
        has_mask_shapes = bool(ms)
        has_ai_matte = layer.ai_matte is not None and layer.ai_matte_enabled
        
        if not has_qualifier and not has_mask_shapes and not has_ai_matte:
            return self.white_tex
        
        matte_tex_A = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_A = self.fb_manager.get_fbo(matte_tex_A)
        fbo_A.use()
        ctx.viewport = (0, 0, w, h)
        
        # Step 1: Qualifier / Keyer
        if has_qualifier:
            ctx.clear(0.0, 0.0, 0.0, 1.0)
            prog_keyer = self.utility_shaders['keyer']
            input_tex.use(0)
            prog_keyer['u_input_tex'].value = 0
            prog_keyer['u_h_enabled'].value = bool(qp.get('hue_enabled', True))
            prog_keyer['u_h_center'].value = float(qp.get('hue_center', 0.5))
            prog_keyer['u_h_width'].value = float(qp.get('hue_width', 1.0))
            prog_keyer['u_h_soft'].value = float(qp.get('hue_soft', 0.1))
            prog_keyer['u_s_enabled'].value = bool(qp.get('sat_enabled', True))
            prog_keyer['u_s_low'].value = float(qp.get('sat_low', 0.0))
            prog_keyer['u_s_high'].value = float(qp.get('sat_high', 1.0))
            prog_keyer['u_s_s_low'].value = float(qp.get('sat_soft_low', 0.0))
            prog_keyer['u_s_s_high'].value = float(qp.get('sat_soft_high', 0.0))
            prog_keyer['u_l_enabled'].value = bool(qp.get('lum_enabled', True))
            prog_keyer['u_l_low'].value = float(qp.get('lum_low', 0.0))
            prog_keyer['u_l_high'].value = float(qp.get('lum_high', 1.0))
            prog_keyer['u_l_s_low'].value = float(qp.get('lum_soft_low', 0.0))
            prog_keyer['u_l_s_high'].value = float(qp.get('lum_soft_high', 0.0))
            prog_keyer['u_invert'].value = bool(qp.get('invert', False))
            self.quad_vao.render(moderngl.TRIANGLE_STRIP)
        else:
            ctx.clear(1.0, 1.0, 1.0, 1.0)
        
        # Step 2: Combine with mask_shapes and ai_matte (CPU)
        if has_mask_shapes or has_ai_matte:
            matte_tex_B = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
            fbo_B = self.fb_manager.get_fbo(matte_tex_B)
            fbo_B.use()
            ctx.viewport = (0, 0, w, h)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            
            # Read qualifier matte from GPU
            matte_data = np.frombuffer(matte_tex_A.read(), dtype='f2').reshape(h, w)
            
            # Combine with mask_shapes (CPU)
            if has_mask_shapes:
                img_w = self.img_full_original.shape[1] if self.img_full_original is not None else w
                mask_shape_matte = self._render_mask_items_recursive(ms, w, h, img_w)
                matte_data = matte_data * mask_shape_matte
            
            # Combine with ai_matte (CPU)
            if has_ai_matte:
                ai_matte_resized = cv2.resize(layer.ai_matte, (w, h), interpolation=cv2.INTER_LINEAR)
                op = layer.ai_matte_operation
                if op == 'add':
                    matte_data = np.maximum(matte_data, ai_matte_resized)
                elif op == 'subtract':
                    matte_data = np.clip(matte_data - ai_matte_resized, 0.0, 1.0)
                elif op == 'intersect':
                    matte_data = matte_data * ai_matte_resized
            
            # Upload combined matte to GPU
            matte_tex_B.write(matte_data.astype('f2').tobytes())
            self.fb_manager.release_temp_texture(matte_tex_A)
            matte_tex_A = matte_tex_B
        
        # Step 3: Postprocess (clean_black, clean_white, blur)
        matte_tex_A = self._apply_matte_postprocess(matte_tex_A, w, h, qp, ms)
        
        return matte_tex_A

    def _apply_matte_postprocess(self, matte_tex, w, h, qp, ms):
        """Apply clean_black (erode), clean_white (dilate), and blur to matte."""
        ctx = self.gl_manager.ctx
        
        max_clean_black = 0.0
        max_clean_white = 0.0
        max_blur = 0.0
        
        for shape in ms:
            if isinstance(shape, dict):
                shape_type = shape.get('type', '')
                blur = shape.get('blur', 0.0)
                clean_black = shape.get('clean_black', 0.0)
                clean_white = shape.get('clean_white', 0.0)
            else:
                shape_type = getattr(shape, 'type', '')
                blur = getattr(shape, 'blur', 0.0)
                clean_black = getattr(shape, 'clean_black', 0.0)
                clean_white = getattr(shape, 'clean_white', 0.0)
            
            if shape_type == 'bitmap':
                max_blur = max(max_blur, blur)
                max_clean_black = max(max_clean_black, clean_black)
                max_clean_white = max(max_clean_white, clean_white)
        
        clean_black = max(max_clean_black, qp.get('clean_black', 0.0))
        clean_white = max(max_clean_white, qp.get('clean_white', 0.0))
        blur_radius = max(max_blur, qp.get('blur', 0.0))
        
        matte_tex_A = matte_tex

        def _morph_pass(src_tex, radius, operation):
            """Separable morphology: horizontal + vertical sweeps (matches
            async_renderer._morphology_pass; box min/max factorises exactly)."""
            prog_morph = self.utility_shaders['morphology']
            prog_morph['u_texel_size'].value = (1.0 / w, 1.0 / h)
            prog_morph['u_radius'].value = float(radius)
            prog_morph['u_operation'].value = operation

            tex_h = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
            fbo_h = self.fb_manager.get_fbo(tex_h)
            fbo_h.use()
            ctx.viewport = (0, 0, w, h)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            src_tex.use(0)
            prog_morph['u_tex'].value = 0
            prog_morph['u_direction'].value = 0
            self.quad_vao.render(moderngl.TRIANGLE_STRIP)

            tex_v = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
            fbo_v = self.fb_manager.get_fbo(tex_v)
            fbo_v.use()
            ctx.viewport = (0, 0, w, h)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            tex_h.use(0)
            prog_morph['u_tex'].value = 0
            prog_morph['u_direction'].value = 1
            self.quad_vao.render(moderngl.TRIANGLE_STRIP)

            self.fb_manager.release_temp_texture(src_tex)
            self.fb_manager.release_temp_texture(tex_h)
            return tex_v

        # Apply clean_black (erosion)
        if clean_black > 0.01:
            radius = max(1.0, clean_black * 3.0)
            matte_tex_A = _morph_pass(matte_tex_A, radius, 0)  # Erode

        # Apply clean_white (dilation)
        if clean_white > 0.01:
            radius = max(1.0, clean_white * 3.0)
            matte_tex_A = _morph_pass(matte_tex_A, radius, 1)  # Dilate
        
        # Apply blur (blur.glsl is separable: horizontal + vertical passes)
        if blur_radius > 0.01:
            blur_pixel_radius = max(1.0, blur_radius * 0.5)
            matte_tex_A = self._blur_matte_pass(matte_tex_A, w, h, blur_pixel_radius)

        # Apply qualifier denoise (small blur + smoothstep contrast restore)
        denoise = qp.get('denoise', 0.0)
        if denoise > 0.5:
            denoise_radius = max(1.0, denoise * 0.04)
            matte_tex_A = self._blur_matte_pass(matte_tex_A, w, h, denoise_radius)
            prog_dn = self.utility_shaders['matte_denoise']
            matte_tex_B = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
            fbo_B = self.fb_manager.get_fbo(matte_tex_B)
            fbo_B.use()
            ctx.viewport = (0, 0, w, h)
            ctx.clear(0.0, 0.0, 0.0, 0.0)
            matte_tex_A.use(0)
            prog_dn['u_tex'].value = 0
            prog_dn['u_strength'].value = float(min(denoise / 100.0, 1.0))
            self.quad_vao.render(moderngl.TRIANGLE_STRIP)
            self.fb_manager.release_temp_texture(matte_tex_A)
            matte_tex_A = matte_tex_B

        return matte_tex_A

    def _blur_matte_pass(self, src_tex, w, h, radius):
        """Separable gaussian blur on a matte texture (H then V), matching
        async_renderer's postprocess. Returns the new texture; src released."""
        ctx = self.gl_manager.ctx
        prog_blur = self.utility_shaders['blur']
        tex_h = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_h = self.fb_manager.get_fbo(tex_h)
        fbo_h.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        src_tex.use(0)
        prog_blur['u_tex'].value = 0
        prog_blur['u_radius'].value = float(radius)
        prog_blur['u_direction'].value = (1.0 / w, 0.0)
        self.quad_vao.render(moderngl.TRIANGLE_STRIP)

        tex_v = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_v = self.fb_manager.get_fbo(tex_v)
        fbo_v.use()
        ctx.viewport = (0, 0, w, h)
        ctx.clear(0.0, 0.0, 0.0, 0.0)
        tex_h.use(0)
        prog_blur['u_tex'].value = 0
        prog_blur['u_radius'].value = float(radius)
        prog_blur['u_direction'].value = (0.0, 1.0 / h)
        self.quad_vao.render(moderngl.TRIANGLE_STRIP)

        self.fb_manager.release_temp_texture(src_tex)
        self.fb_manager.release_temp_texture(tex_h)
        return tex_v

    def _load_lut_sync(self, ctx, path):
        """P1-COLOR-11 复查：主管线 LUT 上传统一走 lut_loader。

        此前这里是 .cube 私有解析器——不支持 .3dl、不校验行数、忽略
        metadata。统一后 .cube/.3dl 共用一套解析（含 TITLE/DOMAIN 等
        metadata），GPU 纹理字节序与原实现一致（red 最快）。
        非单位 DOMAIN 的表先重采样到 [0,1]，与 CPU 导出采样同口径。
        """
        try:
            from src.shared.color.lut_loader import load_lut, resample_to_unit_domain
            loaded = resample_to_unit_domain(load_lut(path))
            data = np.ascontiguousarray(loaded.table, dtype='f4').tobytes()
            tex = ctx.texture3d((loaded.size,) * 3, 3, data=data, dtype='f4')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = tex.repeat_z = False
            return tex
        except Exception:
            logger.debug(f"Failed to load LUT from {path}")
        return None

    @staticmethod
    def _wait_for_front_texture_fence(renderer, tex_id) -> bool:
        """Order the UI blit after the renderer completed this video texture.

        The Color worker and Canvas use different shared OpenGL contexts.
        glWaitSync is a server-side wait: it preserves GPU ordering without
        blocking the GUI thread like a per-frame ctx.finish would.

        Returns:
            当前纹理与其同步对象仍匹配、可以安全绘制时为 True。
        """
        if tex_id != getattr(renderer, 'current_front_tex_id', None):
            return False
        fence = getattr(renderer, 'current_front_fence', None)
        if not fence:
            return True
        try:
            from OpenGL.GL import GL_TIMEOUT_IGNORED, glWaitSync

            # glWaitSync permits only zero flags. The producer has already
            # flushed the fence before publishing the front texture.
            glWaitSync(fence, 0, GL_TIMEOUT_IGNORED)
            return True
        except Exception:
            # A few drivers reject cross-context GLsync waits even when the
            # textures themselves are shareable. Never turn that capability
            # failure into a black viewport: keep presenting the current front
            # texture, matching the legacy behavior, while retaining the
            # mutex/latest-slot protection above.
            logger.debug(
                'Color front-texture fence wait failed; presenting current texture',
                exc_info=True,
            )
            return True

    def _draw_background_tex_id(self, tex_id, viewport_size, src_rect=None, rotation_angle=0.0, letterbox=False, presentation_size=None):
        """Draw background texture with optional rotation for crop preview.

        Args:
            tex_id: OpenGL texture ID to render
            viewport_size: Tuple of (width, height) for the viewport
            src_rect: Optional source rectangle for UV coordinates
            rotation_angle: Rotation angle in degrees for crop preview (default 0.0)
            letterbox: If True, fit texture maintaining aspect ratio with black bars
            presentation_size: Fixed media display size used for video fitting.
        """
        mutex = self.gl_manager.mutex
        # Never stall the GUI event loop behind a long render-thread pass.
        # Canvas will retain the previously presented frame and paint again on
        # the next frame_ready signal once the renderer releases this mutex.
        if not mutex.tryLock():
            return False
        try:
            # Canvas reads tex_id before taking the shared GL mutex. During
            # continuous playback the renderer can publish a newer front slot
            # before Canvas obtains the lock. Drawing the captured id would be
            # unsafe, but dropping it every time freezes the viewport while
            # the transport continues. Snapshot the newest texture and its
            # fence together inside the mutex, then present that current slot.
            current_tex_id = getattr(self.renderer, 'current_front_tex_id', None)
            if current_tex_id is None or current_tex_id <= 0:
                return False
            if not self._wait_for_front_texture_fence(self.renderer, current_tex_id):
                return False
            self._do_draw_background_tex_id(
                current_tex_id,
                viewport_size,
                src_rect,
                rotation_angle,
                letterbox,
                presentation_size,
            )
            return True
        finally:
            mutex.unlock()

    def _do_draw_background_tex_id(self, tex_id, viewport_size, src_rect=None, rotation_angle=0.0, letterbox=False, presentation_size=None) -> None:
        """Blit a GL texture to the current context's default framebuffer.

        调用方必须已持有共享 GL mutex（见 _draw_background_tex_id）。
        依据状态选择着色器：split 开启时用 split_prog 做 wipe 对比；
        非零 rotation_angle 用旋转预览着色器（裁剪预览）；否则走
        passthrough。letterbox 分支按 presentation_size/纹理实际尺寸
        做宽高比适配并清屏为 #141414（ProgramViewer 对齐）。
        """
        context = QOpenGLContext.currentContext()
        if not context:
            logger.warning("_do_draw_background_tex_id: No current OpenGL context")
            return
        # PyQt6 只提供 QOpenGLFunctions_4_1_Core (OpenGL 4.1 是 3.3 的超集)
        from PyQt6.QtOpenGL import QOpenGLFunctions_4_1_Core
        gl = QOpenGLFunctions_4_1_Core()
        if not gl.initializeOpenGLFunctions():
            # Fallback: 尝试使用 QOpenGLFunctions (OpenGL 2.0 / ES 2.0 级别)
            # 这种情况下 glActiveTexture 等基本函数仍然可用
            logger.error("QOpenGLFunctions_4_1_Core init failed, context version:", 
                  context.format().version())
            from PyQt6.QtOpenGL import QOpenGLFunctions
            gl = QOpenGLFunctions(context)
            if not gl.initializeOpenGLFunctions():
                logger.error("Even QOpenGLFunctions failed to initialize!")
                return
        
        # Disable SRGB conversion for the default framebuffer to avoid double-gamma
        # when displaying images that are already sRGB-encoded.
        gl.glDisable(0x8DB9) # GL_FRAMEBUFFER_SRGB

        if not self.passthrough_prog: self._init_gl_resources()
        self.gl_manager.ctx.viewport = (0, 0, viewport_size[0], viewport_size[1])

        # ``letterbox`` draws a smaller native OpenGL quad.  QGraphicsView's
        # brush does not reliably cover the unused default-framebuffer area,
        # so it was left black while Edit's ProgramViewer cleared it to its
        # dark-gray viewer colour.  Clear the final presentation framebuffer
        # here, immediately before the quad is drawn, for every Color source.
        gl.glClearColor(0.08, 0.08, 0.08, 1.0)  # #141414, ProgramViewer parity
        gl.glClear(0x00004000)  # GL_COLOR_BUFFER_BIT

        if letterbox:
            # CompositeViewport's stable presentation rule: fit the media's
            # fixed display geometry, not the transient FBO texture size.
            # The latter changes whenever a dock is resized and was the direct
            # cause of Color video being stretched with the panel.
            tw = th = 0
            if presentation_size and len(presentation_size) == 2:
                try:
                    tw, th = int(presentation_size[0]), int(presentation_size[1])
                except (TypeError, ValueError):
                    tw = th = 0
            if tw <= 0 or th <= 0:
                gl.glBindTexture(0x0DE1, tex_id)
                tw = gl.glGetTexLevelParameteriv(0x0DE1, 0, 0x1000)  # GL_TEXTURE_WIDTH
                th = gl.glGetTexLevelParameteriv(0x0DE1, 0, 0x1001)  # GL_TEXTURE_HEIGHT
                gl.glBindTexture(0x0DE1, 0)
            if tw > 0 and th > 0:
                tex_aspect = tw / th
                vp_aspect = viewport_size[0] / viewport_size[1]
                if tex_aspect > vp_aspect:
                    # Texture wider — fit width, letterbox top/bottom
                    sy = vp_aspect / tex_aspect
                    x0, x1 = -1.0, 1.0
                    y0, y1 = -sy, sy
                else:
                    # Texture taller — fit height, letterbox left/right
                    sx = tex_aspect / vp_aspect
                    x0, x1 = -sx, sx
                    y0, y1 = -1.0, 1.0
                vertices = np.array([x0, y1, 0.0, 1.0, x0, y0, 0.0, 0.0,
                                     x1, y1, 1.0, 1.0, x1, y0, 1.0, 0.0], dtype='f4')
            else:
                vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0,
                                     1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
            self.quad_vbo.write(vertices)
        elif src_rect:
            u0, v0, u1, v1 = src_rect
            vertices = np.array([-1.0, 1.0, u0, v1, -1.0, -1.0, u0, v0, 1.0, 1.0, u1, v1, 1.0, -1.0, u1, v0], dtype='f4')
            self.quad_vbo.write(vertices)
        else:
            vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
            self.quad_vbo.write(vertices)

        if self.split_view_active and self.renderer:
            original_tex_id = self.renderer.get_original_texture_id(self.current_lod)
            if original_tex_id is not None:
                gl.glActiveTexture(0x84C0); gl.glBindTexture(0x0DE1, tex_id)
                gl.glActiveTexture(0x84C1); gl.glBindTexture(0x0DE1, original_tex_id)
                self.split_prog['u_tex_current'].value = 0
                self.split_prog['u_tex_original'].value = 1
                self.split_prog['u_split_pos'].value = self.split_position
                # F4.3: 对比模式（0=split wipe / 1=difference）
                try:
                    self.split_prog['u_compare_mode'].value = (
                        1 if getattr(self, "compare_mode", "split")
                        == "difference" else 0)
                except KeyError:
                    pass  # 旧 shader 无该 uniform
                if self.img_full_original is not None:
                    h, w = self.img_full_original.shape[:2]
                    vx, vy, vw, vh = self.visible_rect
                    if vw <= 1.0 or vh <= 1.0: vx, vy, vw, vh = 0.0, 0.0, float(w), float(h)
                    uv_rect = (vx / w, (vy + vh) / h, vw / w, -vh / h)
                    self.split_prog['u_uv_rect'].value = uv_rect
                else: self.split_prog['u_uv_rect'].value = (0.0, 1.0, 1.0, -1.0)
                self.split_vao.render(moderngl.TRIANGLE_STRIP)
                gl.glActiveTexture(0x84C1); gl.glBindTexture(0x0DE1, 0)
                gl.glActiveTexture(0x84C0); gl.glBindTexture(0x0DE1, 0)
                return
        
        gl.glActiveTexture(0x84C0); gl.glBindTexture(0x0DE1, tex_id)
        
        # Use rotation shader for crop preview when rotation_angle is non-zero
        if abs(rotation_angle) > 0.01:
            self.rotation_prog['u_texture'].value = 0
            self.rotation_prog['u_rotation_angle'].value = float(rotation_angle)
            
            # Calculate viewport aspect ratio for correct physical space rotation
            # NDC is square [-1, 1] but viewport is rectangular, so we must
            # transform to physical space before rotating to avoid distortion
            vp_w, vp_h = viewport_size
            viewport_aspect = float(vp_w) / float(vp_h) if vp_h > 0 else 1.0
            
            self.rotation_prog['u_viewport_aspect'].value = float(viewport_aspect)
            self.rotation_vao.render(moderngl.TRIANGLE_STRIP)
        else:
            self.passthrough_prog['u_texture'].value = 0
            self.quad_vao.render(moderngl.TRIANGLE_STRIP)
        
        gl.glBindTexture(0x0DE1, 0)

    def render_scope_view(self, max_dim: int = 512) -> Optional[np.ndarray]:
        """Read back the visible viewport region for legacy CPU scopes.

        渲染到内部 _scope_fbo 后回读，结果为 BGR float32 0..1，并同时
        刷新 cached_thumbnail。失败或无渲染器时返回 None。
        """
        if self.renderer and self.renderer.current_front_tex_id is not None:
            try:
                if not QOpenGLContext.currentContext(): return None
                vw, vh = self.viewport_size
                vx, vy, v_width, v_height = self.visible_rect
                if self.img_full_original is not None:
                    img_h, img_w = self.img_full_original.shape[:2]
                    u0 = max(0.0, min(1.0, (0 - vx) / v_width))
                    u1 = max(0.0, min(1.0, (img_w - vx) / v_width))
                    v0 = max(0.0, min(1.0, 1.0 - (img_h - vy) / v_height))
                    v1 = max(0.0, min(1.0, 1.0 - (0 - vy) / v_height))
                    src_rect = (u0, v0, u1, v1)
                else: src_rect = None
                scale = max_dim / max(vw, vh)
                rw, rh = int(vw * scale), int(vh * scale)
                if not hasattr(self, '_scope_fbo') or self._scope_fbo.size != (rw, rh):
                    if hasattr(self, '_scope_fbo'): self._scope_fbo.release(); self._scope_tex.release()
                    self._scope_tex = self.gl_manager.ctx.texture((rw, rh), 3, dtype='f4')
                    self._scope_fbo = self.gl_manager.ctx.framebuffer(color_attachments=[self._scope_tex])
                self._scope_fbo.use(); self.gl_manager.ctx.scissor = None; self._scope_fbo.clear(0, 0, 0, 1)
                self._draw_background_tex_id(self.renderer.current_front_tex_id, (rw, rh), src_rect=src_rect)
                data = self._scope_fbo.read(components=3, alignment=1, dtype='f4')
                img = np.frombuffer(data, dtype=np.float32).reshape(rh, rw, 3)
                img = np.flipud(img); res_bgr_float = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                self.cached_thumbnail = (np.clip(res_bgr_float, 0, 1) * 255).astype(np.uint8) 
                return res_bgr_float
            except Exception as e: logger.error(f"Readback Error: {e}")
        return None

    def render_full_image_for_scope(self, max_dim: int = 512) -> Optional[np.ndarray]:
        """
        Render the entire image for scope display (not just visible area).

        This is used when the user wants the scope to show the full image's
        color information regardless of viewport zoom level.

        Args:
            max_dim: Maximum dimension for the rendered image

        Returns:
            numpy.ndarray: BGR float32 image (0.0-1.0 range), or None if failed
        """
        if self.renderer and self.renderer.current_front_tex_id is not None:
            try:
                if not QOpenGLContext.currentContext():
                    return None

                if self.img_full_original is None:
                    return None

                img_h, img_w = self.img_full_original.shape[:2]

                # Calculate render size (scale down if needed)
                scale = min(1.0, max_dim / max(img_w, img_h))
                rw, rh = int(img_w * scale), int(img_h * scale)

                # Create/use FBO for full image rendering
                if not hasattr(self, '_full_scope_fbo') or self._full_scope_fbo.size != (rw, rh):
                    if hasattr(self, '_full_scope_fbo'):
                        self._full_scope_fbo.release()
                        self._full_scope_tex.release()
                    self._full_scope_tex = self.gl_manager.ctx.texture((rw, rh), 3, dtype='f4')
                    self._full_scope_fbo = self.gl_manager.ctx.framebuffer(color_attachments=[self._full_scope_tex])

                self._full_scope_fbo.use()
                self.gl_manager.ctx.scissor = None
                self._full_scope_fbo.clear(0, 0, 0, 1)

                # Draw the entire image (src_rect = None means full texture)
                self._draw_background_tex_id(self.renderer.current_front_tex_id, (rw, rh), src_rect=None)

                # Read back the image data
                data = self._full_scope_fbo.read(components=3, alignment=1, dtype='f4')
                img = np.frombuffer(data, dtype=np.float32).reshape(rh, rw, 3)
                img = np.flipud(img)
                res_bgr_float = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

                return res_bgr_float
            except Exception as e:
                logger.error(f"Image Scope Readback Error: {e}")
        return None

    def render_cached_image(self, target_size: Optional[tuple] = None) -> Optional[np.ndarray]:
        """
        使用缓存渲染整个图像（快速导出）
        
        使用 img_high 缓存进行全图像渲染，不受视口缩放影响。
        速度比全分辨率渲染快很多，最大支持 4000 像素。

        Args:
            target_size: 目标尺寸 (width, height)，如果为 None 则使用缓存原始尺寸

        Returns:
            numpy.ndarray: BGR 格式的图像数据，如果失败返回 None
        """
        try:
            if not QOpenGLContext.currentContext():
                return None

            if self.img_high is None:
                return None

            # 保存原始图像引用
            orig_full = self.img_full_original

            # AUD-0603：临时替换必须 try/finally 恢复——render_full_resolution
            # 内部经嵌套事件循环（loop.exec）处理排队事件，期间任意读取者
            # （scope/另一次渲染）会看到代理图；渲染抛异常时若不恢复，
            # img_full_original 将永久滞留为低分辨率 8 位代理。
            try:
                # 临时使用 img_high 作为全分辨率图像进行渲染
                # 这样可以利用现有的全分辨率渲染管线
                # AUD-0604：float 图先 clip 到 [0,1] 再量化（与
                # convert_to_uint8 同口径）——HDR 值直接 astype(uint8)
                # 会按模 256 回绕，导出出现亮度反转伪影。
                if self.img_high.dtype == np.float32:
                    self.img_full_original = (np.clip(self.img_high, 0.0, 1.0) * 255).astype(np.uint8)
                else:
                    self.img_full_original = self.img_high

                # 调用全分辨率渲染
                result = self.render_full_resolution()
            finally:
                # 恢复原始图像引用
                self.img_full_original = orig_full

            if result is None:
                return None
            
            # 如果需要调整尺寸
            if target_size:
                orig_h, orig_w = self.img_full_original.shape[:2] if self.img_full_original is not None else result.shape[:2]
                orig_ratio = orig_w / max(1, orig_h)
                tw, th = target_size
                target_ratio = tw / max(1, th)
                
                # 保持宽高比
                if abs(orig_ratio - target_ratio) > 0.01:
                    if tw / max(1, orig_ratio) <= th:
                        th = int(tw / orig_ratio)
                    else:
                        tw = int(th * orig_ratio)
                
                # 限制最大为缓存尺寸
                cache_h, cache_w = self.img_high.shape[:2]
                if tw > cache_w or th > cache_h:
                    scale = min(cache_w / max(1, tw), cache_h / max(1, th))
                    tw = int(tw * scale)
                    th = int(th * scale)
                
                result = cv2.resize(result, (tw, th), interpolation=cv2.INTER_LANCZOS4)
            
            return result
            
        except Exception as e:
            logger.error(f"Export Render Error: {e}")
            return None

    def grab_viewport_image(self, target_size: Optional[tuple] = None) -> Optional[np.ndarray]:
        """
        从当前视图抓取图像（已弃用，保留兼容性）
        现在调用 render_cached_image 来渲染整个图像

        Args:
            target_size: 目标尺寸 (width, height)，如果为 None 则使用缓存原始尺寸

        Returns:
            numpy.ndarray: BGR 格式的图像数据，如果失败返回 None
        """
        return self.render_cached_image(target_size)

    def _init_gl_resources(self) -> None:
        """Create the UI-thread GL resources (idempotent).

        passthrough / split / rotation 预览着色器、共享 quad VBO+VAO、
        蒙版生成用 utility shaders（keyer/blur/morphology/denoise/
        mask_combine）、FramebufferManager 与 1x1 white_tex。
        """
        if self.passthrough_prog: return
        self.pipeline = ShaderPipeline(self.gl_manager.ctx)
        self.passthrough_prog = self.gl_manager.ctx.program(vertex_shader=PASSTHROUGH_VERTEX, fragment_shader=PASSTHROUGH_FRAGMENT)
        self.split_prog = self.gl_manager.ctx.program(vertex_shader=PASSTHROUGH_VERTEX, fragment_shader=SPLIT_VIEW_FRAGMENT)
        # Rotation preview shader for crop tool - uses vertex-space rotation with viewport aspect ratio correction
        self.rotation_prog = self.gl_manager.ctx.program(vertex_shader=ROTATION_PREVIEW_VERTEX, fragment_shader=ROTATION_PREVIEW_FRAGMENT)
        vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
        self.quad_vbo = self.gl_manager.ctx.buffer(vertices)
        self.quad_vao = self.gl_manager.ctx.vertex_array(self.passthrough_prog, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
        self.split_vao = self.gl_manager.ctx.vertex_array(self.split_prog, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
        self.rotation_vao = self.gl_manager.ctx.vertex_array(self.rotation_prog, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
        
        # Phase 2: Utility shaders for matte generation (qualifier, blur, morphology)
        self.utility_shaders = {}
        self.fb_manager = FramebufferManager(self.gl_manager.ctx)
        self._load_utility_shaders()
        self.white_tex = self.gl_manager.ctx.texture((1, 1), 1, data=np.array([1.0], dtype='f4').tobytes(), dtype='f4')
        self.white_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

    def _load_utility_shaders(self):
        """Load utility shaders for matte generation (keyer, blur, morphology, mask_combine)."""
        shader_dir = os.path.join(os.path.dirname(__file__), "..", "shaders")
        ctx = self.gl_manager.ctx
        
        def load_shader(name, path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    frag = f.read()
                self.utility_shaders[name] = ctx.program(
                    vertex_shader=PASSTHROUGH_VERTEX,
                    fragment_shader=frag
                )
            except Exception as e:
                logger.error(f"Failed to load utility shader {name}: {e}")
        
        load_shader("keyer", os.path.join(shader_dir, "keyer.glsl"))
        load_shader("blur", os.path.join(shader_dir, "blur.glsl"))
        load_shader("morphology", os.path.join(shader_dir, "morphology.glsl"))
        load_shader("matte_denoise", os.path.join(shader_dir, "matte_denoise.glsl"))

        mask_combine_fs = """
        #version 330
        uniform sampler2D u_matte_a;
        uniform sampler2D u_matte_b;
        uniform vec4 u_mask_uv_rect;
        in vec2 v_texcoord;
        out float f_result;
        void main() {
            vec2 mask_uv = u_mask_uv_rect.xy + v_texcoord * u_mask_uv_rect.zw;
            f_result = texture(u_matte_a, v_texcoord).r * texture(u_matte_b, mask_uv).r;
        }
        """
        self.utility_shaders["mask_combine"] = ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=mask_combine_fs
        )

    def _render_quad(self, prog):
        """Render a full-screen quad with the given program."""
        prog['in_vert'].value = self.quad_vbo.bind('2f')
        prog['in_texcoord'].value = self.quad_vbo.bind('2f', offset=8)
        self.quad_vao.render(moderngl.TRIANGLE_STRIP)

    def _upload_to_gpu(self, full, high) -> None:
        """Legacy no-op：源图上传统一走 AsyncRenderer.UPLOAD_IMAGE."""
        pass

    def _generate_pyramid(self, img) -> None:
        """Legacy no-op：mipmap 由 GPU（LINEAR_MIPMAP_LINEAR）自动生成."""
        pass

    def undo(self) -> bool:
        """撤销到上一个状态，恢复图像和图层状态。返回 True 表示成功。"""
        state = self.history.undo(self.img_full_original, self.layers, self.active_layer_index,
                                  current_flip_h=self._flip_h, current_flip_v=self._flip_v,
                                  current_crop_params=self.crop_params)
        if state is None:
            return False
        # 恢复图像和图层
        self._restore_state(state)
        return True

    def redo(self) -> bool:
        """重做到下一个状态，恢复图像和图层状态。返回 True 表示成功。"""
        state = self.history.redo(self.img_full_original, self.layers, self.active_layer_index,
                                  current_flip_h=self._flip_h, current_flip_v=self._flip_v,
                                  current_crop_params=self.crop_params)
        if state is None:
            return False
        # 恢复图像和图层
        self._restore_state(state)
        return True

    def _restore_state(self, state: dict):
        """从状态字典恢复图像和图层状态。"""
        from src.features.color.core.history import HistoryManager

        # 恢复图像
        image = state.get('image')
        if image is not None:
            self.img_full_original = image.copy()
            # AUD-0602：快照记录了裁剪参数（crop/flip 等改变几何基准的
            # 操作压栈时携带）时一并恢复——flip 重建会重放 crop_params，
            # 撤销裁剪后不恢复该参数会让随后的翻转用最新裁剪重建出错误
            # 画面；未记录（None）时保持现行参数。
            crop_params = state.get('crop_params')
            if crop_params is not None:
                self.crop_params = copy.deepcopy(crop_params)
            from src.shared.io.file_io import FileIO
            low = FileIO._resize_to_fit(image, FileIO.LOW_RES_SIZE)
            high = FileIO._resize_to_fit(image, FileIO.HIGH_RES_SIZE)
            # AUD-0600：与 set_image 同口径归一化（img_high/img_low 保持
            # float32 不变量），并把恢复后的像素重新上传 GPU——渲染线程
            # 只消费此前 UPLOAD_IMAGE 的纹理，不重传则视口继续显示撤销
            # 前的旧图/旧尺寸。
            def normalize_to_float32(img):
                if img.dtype == np.uint16:
                    return img.astype(np.float32) / 65535.0
                elif img.dtype == np.uint8:
                    return img.astype(np.float32) / 255.0
                return img.astype(np.float32)  # Assume already 0-1 or HDR

            self.img_high = normalize_to_float32(high)
            self.img_low = normalize_to_float32(low)
            # 源像素已变化（crop/resize/rotate/flip 的撤销/重做）：所有
            # bake 前缀过期（src_epoch 参与前缀摘要，同 set_image）
            self._source_epoch += 1
            img_for_upload = np.ascontiguousarray(self.img_high, dtype=np.float32)
            if self.renderer:
                orig_h, orig_w = self.img_full_original.shape[:2]
                self.renderer.submit(RenderCommand.UPLOAD_IMAGE, {
                    'image': img_for_upload,
                    'original_size': (orig_w, orig_h),
                    'is_video': self._is_video_mode,
                })
            else:
                self._upload_to_gpu(img_for_upload, high)
                self._generate_pyramid(img_for_upload)
            # split 参考纹理必须与 GPU 主纹理（img_high）同尺寸同内容，
            # 否则撤销后 wipe 参考侧仍是旧图
            self.set_reference_image(self.img_high)

        # 恢复图层
        layers_data = state.get('layers', [])
        if layers_data:
            self.layers = HistoryManager.deserialize_layers(layers_data)

        # AUD-0600：图像内容/尺寸已恢复，全部层的蒙版必须重栅格化——
        # _update_mask_textures 按 mask_dirty 跳过未置脏的层，不重置则
        # 蒙版纹理停留在撤销前的旧内容（deserialize 新建的 Layer 默认
        # 置脏，此循环同时覆盖图层列表未被替换的路径）
        for layer in self.layers:
            layer.mask_dirty = True

        # 恢复活动图层索引
        self.active_layer_index = state.get('active_layer_index', 0)
        if self.active_layer_index >= len(self.layers):
            self.active_layer_index = max(0, len(self.layers) - 1)

        # BUG-A2-01：翻转标志随快照恢复。flip_image 压栈时显式记录了
        # flip_h/flip_v，undo/redo 换回该快照时必须同步回写 _flip_h/_flip_v，
        # 否则 get_flip_state() 报告错误翻转态（项目保存写入错误值）、后续
        # _apply_flip_state() 以错误标志为基准产生双重翻转。快照未记录
        # （None，crop/resize 等不改变翻转态的操作）时保持现行标志——
        # 恢复的像素本来就是带现行翻转的，重置反而制造不一致。
        flip_h = state.get('flip_h')
        if flip_h is not None:
            self._flip_h = bool(flip_h)
        flip_v = state.get('flip_v')
        if flip_v is not None:
            self._flip_v = bool(flip_v)

        # P1-COLOR-03: undo/redo 恢复的 grading_params 是压栈时刻（当时帧）
        # 的求值结果；带关键帧轨道时按当前帧重新求值再渲染，否则撤销到
        # 别的帧会显示旧帧的调色值。
        if self.current_grade_frame is not None and self.has_grade_keyframes():
            self.apply_grade_keyframes(int(self.current_grade_frame), render=False)

        # 重新渲染
        self.render_pipeline()

    def can_undo(self) -> bool:
        """Return True when an undo state is available."""
        return self.history.can_undo()

    def can_redo(self) -> bool:
        """Return True when a redo state is available."""
        return self.history.can_redo()

    def resize_image(self, width: int, height: int) -> None:
        """Resize the working image (history push) and refresh all preview caches."""
        if self.img_full_original is None: return
        self.history.push_state(self.img_full_original, self.layers, self.active_layer_index)
        res = cv2.resize(self.img_full_original, (width, height), interpolation=cv2.INTER_LANCZOS4)
        from src.shared.io.file_io import FileIO
        low = FileIO._resize_to_fit(res, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(res, FileIO.HIGH_RES_SIZE)
        # AUD-0601：set_image(is_new_file=False) 不回写 img_full_original
        # （仅 is_new_file/is_video 分支写），resize 必须像 rotate/flip 一样
        # 先同步基准图，否则全分辨率导出与坐标基准滞留旧尺寸。
        self.img_full_original = res.copy()
        self.set_image(res, high, low, is_new_file=False)

    def rotate_image(self, clockwise: bool = True) -> None:
        """Rotate the working image 90° (history push) and refresh preview caches."""
        if self.img_full_original is None: return
        self.history.push_state(self.img_full_original, self.layers, self.active_layer_index)
        code = cv2.ROTATE_90_CLOCKWISE if clockwise else cv2.ROTATE_90_COUNTERCLOCKWISE
        res = cv2.rotate(self.img_full_original, code)
        from src.shared.io.file_io import FileIO
        low = FileIO._resize_to_fit(res, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(res, FileIO.HIGH_RES_SIZE)
        self.img_full_original = res.copy()
        self.set_image(res, high, low, is_new_file=False)

    def flip_image(self, horizontal: bool = True) -> None:
        """Toggle cumulative H/V flip (history push) and rebuild the working image.

        翻转状态非破坏：img_source_full 始终保留原始像素，
        _apply_flip_state() 按当前 _flip_h/_flip_v（叠加 crop_params
        重放）重建显示图。
        """
        if self.img_full_original is None: return
        self.history.push_state(self.img_full_original, self.layers, self.active_layer_index,
                                flip_h=self._flip_h, flip_v=self._flip_v,
                                crop_params=self.crop_params)
        # Toggle the cumulative flip state
        if horizontal:
            self._flip_h = not self._flip_h
        else:
            self._flip_v = not self._flip_v
        self._apply_flip_state()

    def _apply_flip_state(self):
        """Recompute img_full_original from img_source_full using current flip state.

        AUD-0602：img_source_full 是未裁剪原图（裁剪工具的几何锚点），
        重建必须先重放当前 crop_params 再叠加翻转——否则裁剪后的
        flip/reset_flip/set_flip_state 会从未裁剪原图重建，裁剪结果
        被静默丢弃。
        """
        if self.img_source_full is None: return
        res = self._apply_crop_warp(self.img_source_full, self.crop_params or {})
        if self._flip_h and self._flip_v:
            res = cv2.flip(res, -1)
        elif self._flip_h:
            res = cv2.flip(res, 1)
        elif self._flip_v:
            res = cv2.flip(res, 0)
        from src.shared.io.file_io import FileIO
        low = FileIO._resize_to_fit(res, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(res, FileIO.HIGH_RES_SIZE)
        self.img_full_original = res.copy()
        self.set_image(res, high, low, is_new_file=False)

    def get_flip_state(self) -> Dict[str, bool]:
        """Return {"flip_h", "flip_v"} for project serialization."""
        return {"flip_h": self._flip_h, "flip_v": self._flip_v}

    def set_flip_state(self, state: Optional[Dict[str, Any]]) -> None:
        """Restore flip flags from project data and rebuild the working image."""
        if state and self.img_source_full is not None:
            self._flip_h = state.get("flip_h", False)
            self._flip_v = state.get("flip_v", False)
            self._apply_flip_state()

    def reset_flip(self):
        """Reset flip state and restore original image."""
        self._flip_h = False
        self._flip_v = False
        if self.img_source_full is not None:
            self._apply_flip_state()

    def apply_crop(self, params):
        """Apply crop with rotation support using unified Transformation Matrix.

        params: {
            'rect': (nx, ny, nw, nh),      # Legacy/Fallback - normalized rect
            'rotation': float,              # Degrees (clockwise from UI)
            'center': (norm_cx, norm_cy),   # Normalized center position
            'size': (norm_w, norm_h),       # Normalized dimensions
        }

        AUD-0602：img_source_full 永远保持未裁剪原图（裁剪工具交互期间
        展示的就是它，连续裁剪按"替换"语义相对原图定义——见
        get_display_image 的 crop interacting 分支）。裁剪结果由
        crop_params 表达，_apply_flip_state 从原图重建时重放该参数，
        裁剪不再被随后的 flip/reset_flip 静默丢弃。
        """
        if self.img_source_full is None:
            return

        self.history.push_state(self.img_full_original, self.layers, self.active_layer_index,
                                crop_params=self.crop_params)
        self.crop_params = params

        src = self.img_source_full
        final = self._apply_crop_warp(src, params)

        # 5. Update engine and cache
        from src.shared.io.file_io import FileIO
        low = FileIO._resize_to_fit(final, FileIO.LOW_RES_SIZE)
        high = FileIO._resize_to_fit(final, FileIO.HIGH_RES_SIZE)

        if self.current_file_path:
            FileIO.update_cache(self.current_file_path, low, "low")
            FileIO.update_cache(self.current_file_path, high, "high")

        # Update the full resolution working image
        self.img_full_original = final

        # Update high/low previews and GPU textures
        self.set_image(final, high, low, is_new_file=False)

        # Notify status (handled by MainWindow but we set it here for completeness)
        if hasattr(self, 'status_bar_message'):
            self.status_bar_message = f"Crop Applied: {final.shape[1]}x{final.shape[0]}"

    def _apply_crop_warp(self, src: np.ndarray, params: Dict[str, Any]) -> np.ndarray:
        """按裁剪参数对 src 执行统一 warp（裁剪 + 旋转）。

        apply_crop 与 _apply_flip_state 共用的裁剪实现（AUD-0602）：
        支持 center/size 与 legacy rect 两种参数形态；全幅无旋转的
        恒等裁剪原样返回，避免无意义重采样。
        """
        h_src, w_src = src.shape[:2]

        # 1. Extract and convert parameters
        if 'center' in params and 'size' in params:
            norm_cx, norm_cy = params['center']
            norm_w, norm_h = params['size']
        elif 'rect' in params:
            rx, ry, rw, rh = params['rect']
            norm_cx, norm_cy = rx + rw / 2.0, ry + rh / 2.0
            norm_w, norm_h = rw, rh
        else:
            norm_cx, norm_cy = params.get('center', (0.5, 0.5))
            norm_w, norm_h = params.get('size', (1.0, 1.0))
        rotation = params.get('rotation', 0.0)

        # Identity crop (full frame, no rotation) — skip resampling
        if norm_w >= 1.0 and norm_h >= 1.0 and rotation == 0.0:
            return src

        px_cx = norm_cx * w_src
        px_cy = norm_cy * h_src
        target_w = max(1, int(round(norm_w * w_src)))
        target_h = max(1, int(round(norm_h * h_src)))

        # 2. Build Transformation Matrix
        # Note: UI rotation is clockwise. OpenCV getRotationMatrix2D angle is counter-clockwise.
        # To straighten a clockwise-rotated crop box, we must rotate the image counter-clockwise.
        # Thus, angle = rotation.
        angle = rotation
        M = cv2.getRotationMatrix2D((px_cx, px_cy), angle, 1.0)

        # Shift translation component to map (px_cx, px_cy) to (target_w/2, target_h/2)
        M[0, 2] += (target_w / 2.0) - px_cx
        M[1, 2] += (target_h / 2.0) - px_cy

        # 3. Perform unified Warp (Crop + Rotate in one step)
        # We use BORDER_CONSTANT with zero/black to avoid artifacts outside image bounds
        # Lanczos4 provides high quality for rotation/resizing
        return cv2.warpAffine(src, M, (target_w, target_h),
                              flags=cv2.INTER_LANCZOS4,
                              borderMode=cv2.BORDER_CONSTANT)

    # ========================================
    # AI Baking (栅格化) Methods
    # ========================================

    def bake_layer(self, layer_index: int):
        """
        Bake a layer to cache heavy AI computations.

        This renders the layer with all its effects (blur/sharpen, plugins, etc.)
        at full resolution and saves the result to an EXR file. Subsequent renders
        will use the cached texture instead of recomputing heavy effects.

        Args:
            layer_index: Index of the layer to bake
        """
        if not self.renderer:
            return

        if not (0 <= layer_index < len(self.layers)):
            return

        layer = self.layers[layer_index]

        # Don't bake if already baked
        if layer.is_baked:
            return

        # Emit signal for UI feedback
        self.layer_baking_started.emit(layer_index)

        # Create a snapshot of the layer for the render thread
        layer_snapshot = self._create_layer_snapshot(layer)

        # CRITICAL: Prepare full-resolution image data for baking
        # pyramid_textures[0] is only preview resolution (max 4000px)
        # We must pass full-size image to ensure high-quality baked result
        full_res_image = None
        if self.img_full_original is not None:
            # AUD-0605：按 dtype 归一化（与 _do_render_full_res Step 1 同
            # 口径）。img_full_original 可为 uint16（RAW 重解码路径）或
            # float（EXR/HDR 源），一律 /255 会使 uint16 过曝 ~257 倍、
            # float HDR 缩小 255 倍。
            full_rgb = cv2.cvtColor(self.img_full_original, cv2.COLOR_BGR2RGB)
            full_rgba = cv2.cvtColor(full_rgb, cv2.COLOR_RGB2RGBA)
            if full_rgba.dtype == np.uint16:
                normalized = full_rgba.astype(np.float32) / 65535.0
            elif full_rgba.dtype == np.uint8:
                normalized = full_rgba.astype(np.float32) / 255.0
            else:
                normalized = full_rgba.astype(np.float32)
            full_res_image = np.ascontiguousarray(normalized)

        # Submit bake command to render thread with full-resolution image
        # P0-COL-04：bake 必须捕获「到该层为止的完整栈前缀」——预览循环把
        # baked 纹理当作前缀替换语义消费，若只烘单层，冻结后下方图层效果
        # 全部丢失。stack_prefix 携带 0..layer_index 的完整 DTO（与渲染
        # 线程消费的格式一致），prefix_digest 供渲染入口做失效校验。
        stack_prefix = [_layer_render_data(l)
                        for l in self.layers[:layer_index + 1]]
        prefix_digest = self._compute_prefix_digest(stack_prefix)
        self._pending_bake_digests[layer.id] = prefix_digest
        # AUD-0608：bake 代数递增——保存窗口内的 unbake/再 bake 会使该
        # 代数失配，迟到的完成信号被丢弃
        bake_gen = self._bake_generation.get(layer.id, 0) + 1
        self._bake_generation[layer.id] = bake_gen
        self.renderer.submit(RenderCommand.BAKE_LAYER, {
            'layer': layer_snapshot,
            'layer_id': layer.id,
            'stack_prefix': stack_prefix,
            'prefix_digest': prefix_digest,
            'source_size': self.img_full_original.shape[:2][::-1] if self.img_full_original is not None else (0, 0),
            'full_res_image': full_res_image  # Full-resolution image data for GPU upload
        })

    def unbake_layer(self, layer_index: int) -> bool:
        """
        Unbake a layer, clearing the cached texture.

        This removes the baked EXR file and GPU texture, forcing subsequent
        renders to recompute all effects.

        In offline mode, unbaking is blocked to prevent destroying the only
        surviving copy of the image data.

        Args:
            layer_index: Index of the layer to unbake

        Returns:
            True if unbake was successful, False if blocked or failed
        """
        # Offline Mode Protection: Block unbaking to preserve the only surviving cache
        if self.is_offline_mode:
            logger.info("unbake in offline mode! Original media is unavailable.")
            return False

        if not (0 <= layer_index < len(self.layers)):
            return False

        # P0-COL-04：解冻走共用实现（删缓存文件 + 清 GPU 纹理 + 清台账）
        self._unbake_layer_internal(self.layers[layer_index], layer_index)

        # Trigger re-render
        self.render_pipeline()

        return True

    # ── P0-COL-04：bake 前缀摘要与失效校验 ────────────────────────────

    @staticmethod
    def _canonical_digest_part(obj) -> str:
        """把任意层 DTO 字段规约为稳定的摘要输入。

        ndarray 按字节摘要；dict/list 递归；标量用 repr。numpy 的 repr
        会截断显示，不可直接作为指纹。
        """
        import hashlib
        h = hashlib.md5()
        if isinstance(obj, dict):
            for key in sorted(obj.keys(), key=repr):
                h.update(repr(key).encode('utf-8', 'surrogatepass'))
                h.update(ImageEngine._canonical_digest_part(obj[key])
                         .encode('ascii'))
        elif isinstance(obj, (list, tuple)):
            for item in obj:
                h.update(ImageEngine._canonical_digest_part(item)
                         .encode('ascii'))
        elif isinstance(obj, np.ndarray):
            h.update(obj.tobytes())
            h.update(f"|{obj.dtype}|{obj.shape}".encode('ascii'))
        else:
            h.update(repr(obj).encode('utf-8', 'surrogatepass'))
        return h.hexdigest()

    def _compute_prefix_digest(self, prefix_dtos) -> str:
        """计算「到某层为止」的栈前缀摘要（含源图 epoch）。

        末位 DTO（bake 目标自身）的 ``is_baked`` 不参与摘要：bake 完成
        后该标志从 False→True，与「前缀内容是否过期」无关，否则每次
        渲染都会误判失配并解冻刚建好的 bake。
        """
        import hashlib
        h = hashlib.md5()
        h.update(f"src_epoch={self._source_epoch};".encode('ascii'))
        for k, dto in enumerate(prefix_dtos):
            if k == len(prefix_dtos) - 1 and isinstance(dto, dict) \
                    and 'is_baked' in dto:
                dto = {**dto, 'is_baked': False}
            h.update(self._canonical_digest_part(dto).encode('ascii'))
            h.update(b'|')
        return h.hexdigest()

    def _validate_baked_prefixes(self) -> None:
        """渲染入口校验：任一 baked 层的栈前缀与 bake 时不一致即解冻。

        覆盖所有下层变更路径（调色、插件、遮罩、AI 完成、undo/redo、
        源图替换……），无需在每个变更点挂失效调用。解冻走与手动 unbake
        相同的清理（删缓存文件 + 清 GPU 纹理），但不触发重渲染——
        当前渲染随后会以解冻后的状态进行。
        """
        if not self._bake_prefix_digests:
            return
        stale_ids = []
        for j, layer in enumerate(self.layers):
            if not getattr(layer, 'is_baked', False):
                continue
            recorded = self._bake_prefix_digests.get(layer.id)
            if recorded is None:
                # 没有台账的项目（旧版本 bake / 加载自磁盘）：按无摘要
                # 处理，不误伤——下次 bake 会建立台账。
                continue
            current = self._compute_prefix_digest(
                [_layer_render_data(l) for l in self.layers[:j + 1]])
            if current != recorded:
                stale_ids.append(layer.id)
        for layer_id in stale_ids:
            for j, layer in enumerate(self.layers):
                if layer.id != layer_id:
                    continue
                logger.warning(
                    "Baked prefix stale (lower layer changed); unbaking "
                    "layer '%s' to keep bake = stack-prefix semantics",
                    layer.name)
                self._unbake_layer_internal(layer, j)
                break

    def _unbake_layer_internal(self, layer, layer_index: int) -> None:
        """解冻单个层（不触发重渲染）；unbake_layer 的共用实现。"""
        layer.is_baked = False
        self._bake_prefix_digests.pop(layer.id, None)
        # AUD-0608：同时取消进行中的 bake——丢弃 pending 摘要并作废代数，
        # 异步 EXR 保存完成信号到达时按代数守卫丢弃，不再"复活"刚解冻
        # 的层（复活层无 digest 记录，前缀失效校验对其永久失效）。
        self._pending_bake_digests.pop(layer.id, None)
        self._bake_generation.pop(layer.id, None)
        if layer.baked_file_name:
            from src.shared.io.file_io import FileIO
            try:
                FileIO.delete_baked_layer(layer.baked_file_name)
            except OSError as e:
                logger.error(f": Failed to delete baked layer file "
                             f"'{layer.baked_file_name}': {e}")
            layer.baked_file_name = None
        layer.baked_texture_id = None
        if self.renderer:
            self.renderer.submit(RenderCommand.CLEAR_BAKED_TEXTURE, layer.id)
        layer.is_dirty = True

    # Signal for async EXR save completion
    # AUD-0608：追加 bake 代数（int）——保存窗口内发生 unbake/再 bake 时
    # 完成信号按代数失配丢弃
    _baked_save_finished = pyqtSignal(str, str, object, int)  # layer_id, file_name, baked_array, bake_generation

    @pyqtSlot(str, object)
    def _on_full_resolution_ready(self, request_id: str, image: object) -> None:
        """Forward renderer-thread full-resolution readback to UI callers."""
        self._clear_full_resolution_request_state(request_id)
        self.full_resolution_ready.emit(request_id, image)

    @pyqtSlot(str, str)
    def _on_full_resolution_failed(self, request_id: str, reason: str) -> None:
        """Forward a failed/cancelled full-resolution request."""
        self._clear_full_resolution_request_state(request_id)
        self.full_resolution_failed.emit(request_id, reason)

    @pyqtSlot(str, int, str)
    def _on_full_resolution_progress(
        self, request_id: str, percent: int, phase: str
    ) -> None:
        """Forward request progress without exposing renderer internals."""
        self.full_resolution_progress.emit(request_id, percent, phase)

    def _clear_full_resolution_request_state(self, request_id: str) -> None:
        """Clear request ownership after terminal renderer feedback."""
        pending = self.__dict__.get('_full_res_pending_request_id')
        if pending is None or str(pending) == str(request_id):
            self._full_res_pending_request_id = None
            self._full_res_render_active = False

    @pyqtSlot(int, int, int)
    def _on_color_output_ready(self, texture_id: int, width: int, height: int):
        """Handle ColorOutput render completion from AsyncRenderer."""
        self._color_output.update(texture_id, width, height)
        self.color_output_changed.emit(texture_id, width, height)

    @pyqtSlot(int, int, int)
    def _on_monitor_color_output_ready(self, texture_id: int, width: int, height: int):
        """Handle monitor-only ColorOutput without notifying Composite/UI consumers."""
        self.monitor_color_output_changed.emit(texture_id, width, height)

    @pyqtSlot(str, object)
    def _on_layer_baked(self, layer_id: str, baked_array: np.ndarray):
        """
        Slot called when layer baking is complete.

        This saves the baked result to an EXR file asynchronously and uploads it to GPU.

        Args:
            layer_id: ID of the baked layer
            baked_array: Numpy array containing the baked result (float32, RGB)
        """
        # Find the layer by ID
        layer_index = -1
        for i, layer in enumerate(self.layers):
            if layer.id == layer_id:
                layer_index = i
                break

        if layer_index < 0:
            return

        # AUD-0608：渲染期间已发生 unbake（代数被作废）的 bake 直接丢弃，
        # 不再启动 EXR 落盘
        bake_gen = self._bake_generation.get(layer_id)
        if bake_gen is None:
            logger.info("Bake of layer '%s' was cancelled before save; discarding",
                        layer_id)
            return

        layer = self.layers[layer_index]

        # Generate unique filename for baked layer
        baked_file_name = f"baked_{layer.id}.exr"

        # Convert RGB to BGR for OpenCV EXR saving
        # fbo.read() returns RGB format, but cv2.imencode expects BGR
        # CRITICAL: Make a contiguous copy to ensure thread-safe memory access
        baked_array_bgr = np.ascontiguousarray(cv2.cvtColor(baked_array, cv2.COLOR_RGB2BGR))

        # CRITICAL: Use threading to save EXR asynchronously
        # Large EXR files can take 100ms+ to write, which would block UI/render thread
        import threading
        from src.shared.io.file_io import FileIO

        def _async_save_exr():
            try:
                success = FileIO.save_baked_layer(baked_file_name, baked_array_bgr)
                if success:
                    # Notify main thread via signal (Qt signal is thread-safe)
                    self._baked_save_finished.emit(layer_id, baked_file_name,
                                                   baked_array, bake_gen)
                else:
                    self.error_occurred.emit(f"Failed to save baked layer: {baked_file_name}")
            except Exception as e:
                self.error_occurred.emit(f"Error saving baked layer: {e}")

        # Start background thread for disk I/O
        threading.Thread(target=_async_save_exr, daemon=True).start()

    @pyqtSlot(str, str, object, int)
    def _on_baked_save_finished(self, layer_id: str, baked_file_name: str,
                                baked_array: np.ndarray, bake_generation: int):
        """
        Slot called when async EXR save is complete.

        This updates layer state and uploads the baked texture to GPU.

        Args:
            layer_id: ID of the layer that was baked
            baked_file_name: Name of the saved EXR file
            baked_array: Original numpy array (float32, RGB) for GPU upload
            bake_generation: bake 提交时的代数（AUD-0608 守卫入参）
        """
        # Find the layer by ID
        layer_index = -1
        for i, layer in enumerate(self.layers):
            if layer.id == layer_id:
                layer_index = i
                break

        if layer_index < 0:
            return

        layer = self.layers[layer_index]

        # AUD-0608：代数守卫。EXR 异步保存窗口内发生 unbake（代数被作废）
        # 或再次 bake（代数已递增）时，本完成信号作废——不复活层状态、
        # 不上传纹理；孤儿 EXR 文件在层未持有同名有效 bake 时清理。
        if self._bake_generation.get(layer_id) != bake_generation:
            logger.info(
                "Bake of layer '%s' finished after it was cancelled or "
                "superseded (gen %d != current %s); discarding result",
                layer_id, bake_generation,
                self._bake_generation.get(layer_id))
            if not (getattr(layer, 'is_baked', False)
                    and layer.baked_file_name == baked_file_name):
                from src.shared.io.file_io import FileIO
                try:
                    FileIO.delete_baked_layer(baked_file_name)
                except OSError as e:
                    logger.error("Failed to delete orphaned baked layer "
                                 f"file '{baked_file_name}': {e}")
            self.layer_baking_finished.emit(layer_index)
            return

        # Update layer state
        layer.is_baked = True
        layer.baked_file_name = baked_file_name
        # P0-COL-04：登记 bake 时的栈前缀摘要，供渲染入口失效校验
        digest = self._pending_bake_digests.pop(layer.id, None)
        self._bake_generation.pop(layer.id, None)
        if digest is not None:
            self._bake_prefix_digests[layer.id] = digest

        # Upload baked texture to GPU
        if self.renderer:
            self.renderer.submit(RenderCommand.UPLOAD_BAKED_TEXTURE, {
                'layer_id': layer.id,
                'image_array': baked_array
            })

        # Mark layer as clean (baked result is now cached)
        layer.is_dirty = False

        # Trigger re-render to use baked texture
        self.render_pipeline()

        # Emit signal for UI feedback
        self.layer_baking_finished.emit(layer_index)

        logger.info(f"'{layer.name}' baked successfully: {baked_file_name}")

    def _create_layer_snapshot(self, layer: Layer) -> dict:
        """
        Create a serializable snapshot of a layer for the render thread.

        The render thread needs layer data as a dict (not a Layer object)
        to avoid threading issues.

        Args:
            layer: Layer object to snapshot

        Returns:
            Dict containing layer data
        """
        snapshot = {
            'id': layer.id,
            'name': layer.name,
            'visible': layer.visible,
            'opacity': layer.opacity,
            'blend_mode': layer.blend_mode,
            'grading_params': layer.grading_params.copy(),
            'curves_lut': _copy_array_like(layer.curves_lut),
            'curves_data': layer.curves_data.copy() if layer.curves_data else {},
            'hsl_curve_lut': _copy_array_like(layer.hsl_curve_lut),
            'hsl_curve_data': layer.hsl_curve_data.copy() if layer.hsl_curve_data else {},
            'mixer_params': layer.mixer_params.copy() if layer.mixer_params else {},
            'warper_params': _copy_array_like(layer.warper_params),
            'effects_params': layer.effects_params.copy(),
            'active_lut_path': layer.active_lut_path,
            'lut_intensity': layer.lut_intensity,
            'soft_clip_params': layer.soft_clip_params.copy(),
            'qualifier_params': layer.qualifier_params.copy(),
            'mask_shapes': [s.to_dict() for s in layer.mask_shapes],
            'ai_matte': _copy_array_like(layer.ai_matte),
            'ai_matte_operation': layer.ai_matte_operation,
            'ai_matte_enabled': layer.ai_matte_enabled,
            'plugins': layer.plugins.copy() if layer.plugins else [],
            'is_baked': False,  # Always False for baking - we're creating the baked result
        }
        return snapshot
