"""Color 页后台渲染线程（``AsyncRenderer``）。

职责
----
- 在独立 QThread 中持有与 UI 共享的 ModernGL 上下文，执行 Color 页全部
  重量级 GL 操作：图像金字塔上传、视频帧上传（打包 RGB / YUV 平面 /
  GPU 零拷贝外部纹理）、qualifier/mask matte 生成、多层合成 pipeline、
  ColorOutput / 全分辨率导出、GPU scope 分析与视频降噪。
- 通过双缓冲（ping-pong）FBO 发布 front 纹理；UI 侧 Canvas 以
  ``frame_ready`` 携带的 texture id 在自己的共享上下文中完成呈现。

线程边界
--------
- UI（主）线程：``__init__`` 在此线程创建 QOffscreenSurface 与共享
  QOpenGLContext（Windows WGL 的硬性要求）；此后只允许调用
  ``submit`` / ``set_*`` / ``discard_*`` / ``get_*`` /
  ``register|cancel|forget_full_resolution`` / ``stop`` 等线程安全入口，
  绝不直接触 GL。
- 渲染线程（``run``）：唯一创建/释放 GL 资源的地方；所有 ``_do_*`` /
  ``_ensure_*`` / ``_cleanup`` 都在该线程、且持有
  GLContextManager 全局 mutex 时执行。
- 跨线程共享状态：命令队列与 lane 合并槽（``_pending_slots``，
  ``_pending_lock`` 保护）、视频代际 ``_video_generation_id``（过期帧
  丢弃）、scope revision、full-resolution 取消注册表
  （``_full_res_cancel_lock`` 保护）。

命令队列协议
------------
- UI 线程经 ``submit(cmd_type, data)`` 投递 ``(cmd_type, data)`` 二元组；
  可合并的预览类命令进入 ``RenderLane`` 槽位（新替旧，只保留最新），
  同时向队列放入去重后的 ``'_drain_pending'`` 哨兵；关键命令（seek、
  master、导出、上传、bake、编译等）直接入队，绝不丢弃。
- ``run()`` 循环按序消费：哨兵触发 ``_drain_pending_slots``，以固定
  lane 顺序（AUX_TEX → GRADING_REFRESH → VIDEO_PLAYBACK →
  VIEWPORT_REFRESH → COLOR_OUTPUT → SCOPE_SNAPSHOT → GPU_SCOPE）
  执行合并命令；每条命令由 ``_handle_command`` 分发，异常相互隔离，
  mutex 与 ``task_done()`` 保证释放。
- 结果经 pyqtSignal（``frame_ready`` / ``full_resolution_ready`` /
  ``color_output_ready`` / ``gpu_scope_ready`` 等）回到 UI 线程。
"""

import moderngl
import cv2
import numpy as np
import queue
import time
import os
import traceback
import logging
import threading
from contextlib import ExitStack
from enum import Enum
from typing import Dict, Any, Optional, Tuple, List
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext
from src.shared.gpu.gl_backend import GLContextManager, FramebufferManager
from src.shared.gpu.gl_resource_registry import (
    GROUP_DENOISE,
    GROUP_FRAMEBUFFERS,
    GROUP_GRADING,
    GROUP_PRESENT,
    GROUP_SCOPE,
    GROUP_SHADERS,
    GROUP_UPLOAD,
)
from src.features.color.core.opengl_render_backend import OpenGLRenderBackend
from src.features.color.core.curves_soft_clip import curves_soft_clip_is_identity
from src.shared.media.gpu_frame import (
    GpuDecodedFrame,
    GpuFrameRelease,
    is_gpu_decoded_frame,
    safe_release_gpu_frame,
)
from src.shared.media.frame_path import DecodePath, RenderFramePath
from src.shared.diagnostics import flash_diag as _flash_diag

logger = logging.getLogger(__name__)


# Hardware decode backends can expose NV12/P010 as one contiguous ndarray
# instead of a tuple of Y and UV arrays.  These formats must never enter the
# packed RGB uploader: their bytes are YUV samples, not RGB pixels.
_CONTIGUOUS_SEMIPLANAR_420_FORMATS = frozenset({
    'nv12', 'nv21', 'p010', 'p010le', 'p016', 'p016le',
})


# P1-COLOR-08: matte 预览模式 → mask_preview shader u_mode 整数映射
MASK_PREVIEW_MODES = {'highlight': 0, 'black': 1, 'matte': 2}


def matte_cache_hash(qp, ms, size, rect, frame_epoch: int, mask_epoch: int) -> int:
    """P1-COLOR-08: matte 缓存键的单一构造点。

    覆盖所有会导致 matte 内容变化的输入：
    - qp: qualifier 参数（含 denoise/clean/blur/invert）
    - ms: mask_shapes 序列化（含 brush 笔画数）
    - size/rect: 生成分辨率与可见区（viewport 路径）
    - frame_epoch: 视频帧上传代际（播放/seek 时 keyer 输入变化）
    - mask_epoch: CPU 合成 mask 纹理上传代际（AI matte/bitmap 内容变化
      不改 qp/ms 序列化，必须靠 epoch 防止串用旧 matte）
    """
    import json
    payload = {
        'qp': qp,
        'ms': ms,
        'size': tuple(size) if size else None,
        'rect': tuple(rect) if rect else None,
        'epoch': int(frame_epoch),
        'mask_epoch': int(mask_epoch),
    }
    return hash(json.dumps(payload, sort_keys=True))


class RenderLane(Enum):
    """
    P2-3: Render command lane classification for queue merging.

    Each lane follows specific merge strategy:
    - VIDEO_PLAYBACK: RENDER_VIDEO_FRAME preview commands - keep only latest
    - VIEWPORT_REFRESH: zoom/pan/resize - keep only latest, publish last state
    - GRADING_REFRESH: grading parameter changes during playback - keep only latest
    - AUX_TEX: auxiliary texture updates (LUT, curves) - keep only latest
    - COLOR_OUTPUT: Composite page output - keep only latest
    - SCOPE_SNAPSHOT: legacy CPU readback scope (deprecated fallback)
    - GPU_SCOPE: native GPU scope (lowest priority, latest-result-wins)

    Commands NOT in any lane (seek/master/export/upload/bake/etc.) are
    considered CRITICAL and go directly to queue without merging.
    """
    VIDEO_PLAYBACK = "video_playback"
    VIEWPORT_REFRESH = "viewport_refresh"
    GRADING_REFRESH = "grading_refresh"
    AUX_TEX = "aux_tex"
    COLOR_OUTPUT = "color_output"
    SCOPE_SNAPSHOT = "scope_snapshot"
    GPU_SCOPE = "gpu_scope"

from src.shared.video.render_commands import RenderCommand  # noqa: F401 - re-export for backward compatibility

class AsyncRenderer(QThread):
    """
    Background rendering thread that handles all heavy OpenGL operations.
    Uses double-buffering to ensure smooth UI display.

    P3-01（计划 §3.1）调度层边界：本类是 Color 页渲染的 orchestrator。
    命令队列与 lane 合并（submit/_drain_pending_slots）、视频 generation
    与 scope revision 的过期帧丢弃、色彩 metadata 传递、统一性能追踪
    （_renderer_stats/performance_monitor）全部收敛在此；所有 GL 资源的
    创建与释放只发生在渲染线程（run()/_do_*/_cleanup），UI 线程仅允许
    submit/set_*/discard_*/get_* 等线程安全入口。GraphicsBackend 契约的
    对外视图见同目录 OpenGLRenderBackend（P3-02 facade）：它是本类的只读
    包装与资源台账，不引入平行渲染路径，render_pass 直接委托 submit。
    """
    frame_ready = pyqtSignal(int) # texture_id of the front buffer
    layer_baked_ready = pyqtSignal(str, object)  # AI Baking: (layer_id, numpy_array)
    full_resolution_ready = pyqtSignal(str, object)  # (render_request_id, BGR ndarray)
    full_resolution_failed = pyqtSignal(str, str)  # (render_request_id, reason)
    full_resolution_progress = pyqtSignal(str, int, str)  # (request_id, percent, phase)
    color_output_ready = pyqtSignal(int, int, int)  # ColorOutput: (texture_id, width, height)
    monitor_color_output_ready = pyqtSignal(int, int, int)  # DeckLink monitor output only
    scope_snapshot_ready = pyqtSignal(object)  # BGR float32 scope image (CPU fallback)
    gpu_scope_ready = pyqtSignal(object)  # ScopeGpuFrame descriptor (no CPU readback)
    
    def __init__(self, main_context: QOpenGLContext) -> None:
        """构造渲染线程并准备跨线程共享的 GL 上下文（必须在 UI 线程调用）。

        Windows WGL 要求 QOffscreenSurface 在 GUI 线程创建；这里还会短暂
        ``doneCurrent`` 主上下文、创建以它为 share 源的共享
        QOpenGLContext，并 ``moveToThread(self)`` 移交所有权，使 ``run()``
        能在渲染线程合法 ``makeCurrent``。真正的 GL 资源（ModernGL ctx、
        工具 shader、纹理池等）全部延迟到 ``run()`` 中创建。

        Args:
            main_context: UI 侧已创建并 current 过的 QOpenGLContext。
        """
        super().__init__()
        self.main_context = main_context
        self.command_queue = queue.Queue()
        self.ctx = None
        self.surface = None
        self.shared_pyqt_context = None

        # P3-02: GL backend facade —— GraphicsBackend 契约的 'opengl' 实现。
        # 纯 Python 对象（无 GL 调用），GUI 线程构造安全；真实 GL 资源仍由
        # 渲染线程按原有时机创建/释放，facade 只做契约包装与资源台账。
        self._gpu_backend = OpenGLRenderBackend(self)

        # Double Buffering (Ping-Pong)
        self.fbo_pair = [None, None]
        self.tex_pair = [None, None]
        self.write_idx = 0 # Currently rendering to this buffer
        self._scope_snapshot_tex = None
        self._scope_snapshot_fbo = None
        self._scope_snapshot_size = (0, 0)

        # ---- GPU scope state (native pipeline, no CPU readback) ----
        # source_revision increments on every published front texture.
        # demand_revision mirrors the latest ScopeDemand the UI submitted;
        # the renderer rejects any pass whose demand revision is stale.
        self._scope_source_revision: int = 0
        self._scope_demand_revision: int = 0
        # Scope content is independent from the canvas viewport.  The normal
        # front texture is viewport-sized and may be cropped by pan/zoom, so
        # the GPU scope lane maintains a small full-frame ColorOutput cache
        # keyed by content changes (grade/video frame) and analysis quality,
        # never by viewport refreshes.
        self._scope_content_revision: int = 0
        self._scope_color_output_revision: int = -1
        self._scope_color_output_max_dim: int = 0
        self._last_scope_render_params: Optional[dict] = None
        # gpu_scope_enabled: master rollback switch. When False the renderer
        # never runs the GPU scope lane; the UI may show the CPU fallback
        # only while paused. See plan §10.
        self.gpu_scope_enabled: bool = True
        # GPU scope resource pool — owned by the shared GpuScopeEngine
        # (src/shared/gpu/gpu_scope_engine.py) which also serves the
        # Composite renderer. Allocated lazily on the render thread.
        self._gpu_scope_engine: Optional[object] = None
        # Backward-compatible handle: the engine's active pool dict.
        self._gpu_scope_pool: Optional[dict] = None
        # Keep one pool per quality tier. Switching play/paused quality then
        # reuses existing textures/FBOs instead of releasing and reallocating
        # the entire scope graph on every state transition.
        self._gpu_scope_pool_cache: Dict[tuple, dict] = {}

        self.is_running = True
        self._stop_event = threading.Event()
        # Full-resolution requests are critical and must never be merged, but
        # callers still need a request-scoped cancellation boundary.  The
        # registry is protected because cancellation originates on the UI
        # thread while the request is consumed by this render thread.
        self._full_res_cancel_lock = threading.Lock()
        self._full_res_active_requests: set[str] = set()
        self._full_res_cancelled_requests: set[str] = set()
        self.setObjectName("RenderThread")

        # CRITICAL (Windows): QOffscreenSurface must be created on the GUI thread.
        # On macOS this works from any thread, but Windows WGL requires it.
        self.surface = QOffscreenSurface()
        self.surface.setFormat(main_context.format())
        self.surface.create()

        # ==========================================================
        # ⚠️ WINDOWS WGL 黑屏核心修复 (必须在主线程执行)
        # ==========================================================
        # 在 Windows 下，wglShareLists 要求被共享的源上下文不能在任何线程处于 Current 状态
        # 否则会报 0x502 (请求的资源在使用中) 错误，导致纹理无法跨线程共享 (最终黑屏)

        current_surface = self.main_context.surface()
        if current_surface:
            self.main_context.doneCurrent()  # 暂时释放主线程对上下文的占用

        # 此时安全创建共享上下文
        self.shared_pyqt_context = QOpenGLContext()
        self.shared_pyqt_context.setFormat(self.main_context.format())
        self.shared_pyqt_context.setShareContext(self.main_context)

        if self.shared_pyqt_context.create():
            pass  # Context created successfully
        else:
            logger.error("ERROR: Failed to create shared context!")

        # 核心：将这个刚创建的上下文所有权，移交给当前 QThread 对象！
        # 这样一会才能在子线程的 run() 函数里面合法调用 makeCurrent
        self.shared_pyqt_context.moveToThread(self)

        # 恢复主线程的上下文绑定
        if current_surface:
            self.main_context.makeCurrent(current_surface)
        # ==========================================================

        # Internal state
        self.pipeline = None
        self.layers = []
        self.view_params = None
        self.canvas_size = (0, 0)
        self.raw_preview_params = {'exposure': 0.0, 'gains': (1.0, 1.0, 1.0)}
        self.source_image_size = (0, 0)  # 原始图像尺寸，用于锐化等效果的一致性
        
        self.pyramid_textures = []
        # Video generation at the time the current pyramid texture was
        # uploaded.  When the video generation has since advanced (clip
        # switch / playback epoch), the pyramid holds a previous clip's
        # still frame and must not be used as a render fallback.
        self._pyramid_epoch_generation = 0
        self.reference_pyramid_textures = []
        self.tiles = []
        self.layer_textures = {} # {id(layer): {'curves': tex, 'warper': tex, 'lut3d': tex}}
        self.baked_textures = {}  # AI Baking: {layer_id: texture} - cached baked layer textures
        self.ai_plugin_textures = {}  # AI Plugins: {(layer_id, instance_id): texture}
        self.identity_lut = None
        self.current_front_tex_id = None
        self.current_front_size = (0, 0)
        # The Color canvas presents this texture from a different shared GL
        # context. A raw texture id is not enough during realtime rendering:
        # the UI can otherwise sample a double-buffer slot before the render
        # context has finished writing it. This fence is replaced on every
        # unfinished front publish and waited by ImageEngine before the UI blit.
        # Keep the PyOpenGL GLsync object itself. On macOS it is an opaque
        # ctypes pointer and converting it with int() raises, which would
        # silently force every presentation through the glFinish fallback.
        self.current_front_fence: Optional[Any] = None
        # Fixed source presentation geometry.  Unlike ``current_front_size``,
        # this never follows FBO/widget resizing and is safe for final display.
        self.current_video_display_size: tuple[int, int] = (0, 0)
        self.rendered_visible_rect = None  # visible_rect of the current front texture
        self.fb_manager = None

        # ColorOutput: persistent offscreen texture for Composite page
        self._color_output_tex = None
        self._color_output_fbo = None
        # Private full-resolution export target. Keeping it separate prevents
        # an export resize/readback from invalidating the live ColorOutput
        # texture currently shared with Composite/monitor consumers.
        self._full_res_color_output_tex = None
        self._full_res_color_output_fbo = None
        # Optional GPU→CPU PBO ring for full-resolution exports.  The
        # dependency is loaded lazily on the render thread; if the driver or
        # PyOpenGL cannot provide pixel-pack buffers, the request falls back to
        # the existing synchronous worker-thread readback.
        self._full_res_pbo_readback = None
        self._full_res_pbo_disabled = False
        self._full_res_pending_readbacks: Dict[str, Tuple[int, int]] = {}
        # Renderer-internal full-frame source for GPU scopes. Keep it separate
        # from externally published Composite/monitor textures because scope
        # quality tiers use smaller sizes and may reallocate independently.
        self._scope_color_output_tex = None
        self._scope_color_output_fbo = None
        self.utility_shaders = {}
        self.white_tex = None
        self.raw_lut_tex = None
        self.garbage = [] # List of (timestamp, fbo, tex)
        self.matte_cache = {} # {layer_id: {'matte': tex, 'params_hash': hash}}
        self._matte_viewport_textures = {}  # P2-1: per-layer viewport-res matte for cleanup
        # Bumped on every uploaded video frame. Qualifier mattes key on it so
        # playback re-keys the matte per frame instead of reusing frame 0's.
        self._video_matte_epoch = 0
        # P1-COLOR-08: per-layer CPU mask texture upload epoch. AI matte /
        # bitmap repaints re-upload the mask texture without changing the
        # serialized qp/ms, so the matte cache must key on this too.
        self._mask_epoch = {}
        self._use_cpu_transfer = False  # Windows QWebEngine 兼容性标记

        # P4.2: Video frame input state (avoid rebuilding pyramid per frame)
        self.current_video_texture: Optional[moderngl.Texture] = None
        self.current_video_frame_index: int = -1
        self.video_source_size: tuple[int, int] = (0, 0)  # (width, height)
        self.debug_last_rendered_video_frame_index: Optional[int] = None
        self.debug_last_render_command: Optional[str] = None
        self.debug_last_render_reason: Optional[str] = None
        self.debug_last_front_tex_id: Optional[int] = None
        # While true, playback commands exclusively own front-texture
        # presentation; ordinary full/grading renders are dropped.
        self.video_playback_active: bool = False

        # Set True by the playback engine when a clip switch seeks a new frame
        # that has not yet been uploaded (RENDER_VIDEO_FRAME).  While pending,
        # a RENDER_FRAME (grading/viewport_refresh/fit-to-window) would reuse
        # the departing clip's video texture; _do_render skips such renders so
        # the Canvas and DeckLink keep showing the last valid frame until the
        # new frame arrives.  Cleared when a RENDER_VIDEO_FRAME uploads a fresh
        # texture.
        self.video_texture_pending: bool = False
        # Set on the first playback RENDER_VIDEO_FRAME after a clip switch so
        # _do_render forces a glFinish() before publishing that frame's texture.
        # The double-buffer slot written on that frame has not been touched
        # since the previous clip; without a finish the UI can blit its stale
        # contents for one frame (the cross-clip flash).
        self._force_finish_next_publish: bool = False
        # P4.A: Video planes state (for YUV/NV12/P010 formats)
        self.current_video_planes: Optional[tuple] = None  # (y_tex, u_tex, v_tex) or (y_tex, uv_tex)
        self.current_video_pixel_format: Optional[str] = None  # 'yuv420p', 'nv12', 'p010', etc.

        # P0-3: Video YUV metadata state (from actual frame, not raw_preview)
        self.current_video_yuv_matrix: int = 0  # YUVGPUUploader.MATRIX_BT709
        self.current_video_color_range: int = 0  # YUVGPUUploader.RANGE_LIMITED
        self.current_video_bit_depth: int = 8  # Bit depth for YUV range constants
        self.current_video_chroma_siting: int = 0  # Topos: left/center/top-left

        # Debug: frame path tracking for diagnostics
        self._debug_frame_path: str = 'unknown'
        self._debug_decode_path: str = 'unknown'

        # Phase D: GPU zero-copy frame tracking
        self._previous_gpu_frame: GpuDecodedFrame | None = None
        self._current_gpu_frame_planes = None

        # P4.B: Video generation ID for stale frame detection
        # Incremented on seek, old generation frames are dropped
        self._video_generation_id: int = 0

        # M5: 视频降噪（时域+空域）。pipeline 在渲染线程 run() 中创建；
        # 参数经 SET_VIDEO_DENOISE 命令投递（GL mutex 下更新，线程安全）。
        self.video_denoise_pipeline = None
        self.video_denoise_params = None  # VideoDenoiseParams | None
        self.video_denoise_quality_mode = 'preview'
        # P1-COLOR-13: 最近一次实际使用的降噪档位（render status 探测用）
        self.last_video_denoise_quality: Optional[str] = None

        # S5.A: Statistics for dropped preview frames
        self._dropped_preview_frames: int = 0
        # S5.B: Statistics for coalesced video commands
        self._coalesced_video_commands: int = 0
        self._renderer_stats: Dict[str, Any] = {
            'dropped_preview_frames': 0,
            'queue_depth': 0,
            'processed_frames': 0,
            'coalesced_video_commands': 0,  # S5.B 新增
            'coalesced_lane_commands': 0,   # P1-1: lane-based merge count
        }

        # P2-3: Lane-based command merging using RenderLane enum
        # Each lane holds at most one command; newer submissions replace older ones.
        # Lane execution order (see _drain_pending_slots):
        #   AUX_TEX (per-resource sub-slots) -> GRADING_REFRESH -> VIDEO_PLAYBACK
        #   -> VIEWPORT_REFRESH -> COLOR_OUTPUT -> SCOPE_SNAPSHOT
        # VIEWPORT_REFRESH executes last to ensure latest viewport state is published.
        # Commands NOT in any lane (seek, master, export, compile, upload, bake, etc.)
        # go directly to the queue and are never discarded (critical commands).
        # AUX_TEX 槽位键是 (RenderLane.AUX_TEX, layer_id, tex_type) 元组：
        # 同一资源的重复更新仍可合并，不同资源互不覆盖（单槽会在项目
        # 加载/多层 mask 上传时静默丢弃先到的更新）。
        self._pending_slots: Dict[object, Optional[Tuple[str, Any]]] = {
            lane: None for lane in RenderLane
        }
        self._pending_aux_keys: list = []  # 已占用 AUX 子槽键（按提交顺序）
        self._pending_lock = threading.Lock()
        self._coalesced_lane_commands: int = 0  # P1-1: stats for lane-merged commands

        # P1-1 fix: drain sentinel deduplication
        # Without this, rapid submissions pile up (_drain_pending, None) sentinels
        # in the queue, causing worker spin and inaccurate queue_depth stats.
        self._drain_pending_signaled: bool = False
        self._drain_pending_signal_lock = threading.Lock()

        # ==========================================================
        # GPU Pass 优化：减少每帧 temp texture 分配和同步阻塞
        # ==========================================================
        # P1: 可配置的同步模式
        # - "finish": 强制 GPU 完成（最安全，高延迟）
        # - "fence": 使用 GL fence 同步（推荐，中等延迟）
        # - "none": 不同步（最快，可能帧撕裂）
        self.video_render_sync_mode: str = "none"  # 播放时不每帧阻塞 GPU

        # P2: 统计计数器（用于测试验证）
        self._temp_texture_alloc_count: int = 0  # 每帧临时纹理分配计数
        self._finish_call_count: int = 0  # ctx.finish() 调用计数

        # P3: YUV 转 RGB output texture ring buffer（按尺寸缓存）
        # 格式: {(width, height): [tex1, tex2, tex3], ...}
        self._yuv_rgb_output_cache: Dict[Tuple[int, int], List[moderngl.Texture]] = {}
        self._yuv_rgb_output_ring_idx: Dict[Tuple[int, int], int] = {}
        self._yuv_rgb_output_ring_size: int = 3  # 每尺寸缓存 3 个纹理
        # AUD-0202: 尺寸键的 LRU 顺序（最近使用的尺寸在尾部）。每个尺寸的
        # ring 是 3 张视口级 float16 纹理（4K 视口约 192MB），窗口连续缩放
        # 时若不淘汰旧尺寸，GPU 内存会单调增长。
        self._yuv_rgb_output_size_order: List[Tuple[int, int]] = []
        self._yuv_rgb_output_max_sizes: int = 2  # 最多同时保留 2 个尺寸的 ring

        # P1-5: Cached FBO for YUV-to-RGB conversion (keyed by texture glo)
        # Avoids creating a new framebuffer every frame during video playback.
        self._yuv_conv_fbo: Optional[moderngl.Framebuffer] = None
        self._yuv_conv_fbo_tex_glo: int = 0  # glo of the texture the cached FBO wraps

        # P1-6: Cached quad VAOs per program (keyed by program glo)
        # Avoids creating a new VAO on every _render_quad() call.
        self._quad_vao_cache: Dict[int, moderngl.VertexArray] = {}

        # P4: 持久化的 raw_view/ping/pong 纹理（避免每帧从 pool 查找）
        self._persistent_raw_view_tex: Optional[moderngl.Texture] = None
        self._persistent_raw_view_fbo: Optional[moderngl.Framebuffer] = None
        self._persistent_ping_tex: Optional[moderngl.Texture] = None
        self._persistent_ping_fbo: Optional[moderngl.Framebuffer] = None
        self._persistent_pong_tex: Optional[moderngl.Texture] = None
        self._persistent_pong_fbo: Optional[moderngl.Framebuffer] = None
        self._persistent_tex_size: Tuple[int, int] = (0, 0)  # 当前缓存的纹理尺寸

        # P1-7: Persistent tile VBO/VAO (avoid per-tile allocation)
        self._tile_vbo = None
        self._tile_vao = None
        self._tile_prog_id = None  # track which program the VAO was built for

    def run(self) -> None:
        """渲染线程主入口：激活共享上下文、初始化 GL 资源并进入命令循环。

        在本线程 ``makeCurrent`` 共享上下文后创建 ModernGL ctx /
        FramebufferManager / 工具 shader / 视频纹理池 / YUV uploader /
        降噪管线，并执行 GPU YUV 自检；随后循环取命令
        （``'_drain_pending'`` 哨兵或关键命令），在 GLContextManager
        全局 mutex 下交给 ``_handle_command``，单条命令抛错被隔离并记录，
        不会中断线程。``is_running`` 置 False 退出循环后执行 ``_cleanup``
        释放全部 GL 资源。
        """
        logger.info("Thread Started")
        mutex = GLContextManager.instance().mutex

        # 1. 在渲染子线程激活已经在主线程创建好的共享上下文
        # 由于上下文已经在 __init__ 中通过 moveToThread(self) 移交给了本线程，
        # 这里可以直接 makeCurrent
        if not self.shared_pyqt_context.makeCurrent(self.surface):
            logger.error("Failed to make context current on RenderThread")
            # AUD-0211: 早退路径也要释放 surface/上下文引用并复位运行态，
            # 否则 is_running 悬挂为 True、离屏 surface 泄漏。
            self.is_running = False
            self._cleanup()
            return

        # AUD-0211: create_context / 工具 shader 编译等初始化步骤抛错时，
        # 异常冲出 run() 会跳过 _cleanup（泄漏离屏 surface 与共享上下文）。
        # 统一包入 try/except：失败时记录异常、清理后退出线程。
        try:
            # 2. Initialize ModernGL on this thread
            self.ctx = moderngl.create_context()
            self.fb_manager = FramebufferManager(self.ctx)
            self._load_utility_shaders()
            self._create_identity_lut()
            self._create_defaults()

            # P4.1: Initialize VideoTexturePool for video frame uploads
            from src.shared.video.video_texture_pool import VideoTexturePool
            self._video_texture_pool = VideoTexturePool(self.ctx, pool_size=5)

            # P4.A: Initialize YUVGPUUploader for YUV/NV12/P010 plane uploads
            from src.shared.video.yuv_gpu_upload import YUVGPUUploader
            self._yuv_gpu_uploader = YUVGPUUploader(self.ctx, pool_size=5)

            # P1: Run GPU YUV self-test to determine if macOS GPU YUV path is safe
            self._yuv_gpu_self_test_result = None
            self._yuv_gpu_supported = False
            self._run_yuv_gpu_self_test()

            # M5: 视频降噪管线（时域+空域）。初始化失败只禁用降噪，不阻塞渲染。
            try:
                from src.features.color.core.video_denoise_pipeline import (
                    VideoDenoisePipeline,
                )
                self.video_denoise_pipeline = VideoDenoisePipeline(self.ctx)
            except Exception:
                logger.exception("VideoDenoisePipeline init failed; denoise disabled")
                self.video_denoise_pipeline = None

            # P3-02/03: 初始化阶段创建的资源登记到 backend 台账 + 确认 READY。
            # 只记账，不改变任何资源的创建时机与方式（P3-01 行为不变）。
            # __dict__.get 守卫：测试可经 __new__ 构造跳过 __init__ 直接跑 run()
            # （见 test_async_renderer_lane_merge 的 loop runner）。
            gpu_backend = self.__dict__.get('_gpu_backend', None)
            if gpu_backend is not None:
                self._register_core_gpu_resources()
                gpu_backend.initialize()
        except Exception:
            logger.exception(
                "RenderThread initialization failed; running _cleanup() before exit")
            self.is_running = False
            self._cleanup()
            return

        logger.info(f"initialized on {self.objectName()}")

        # 3. Task Loop
        while self.is_running:
            try:
                # Full-res PBO DMA is polled on the render thread while the
                # queue is serviced.  This keeps the UI independent from the
                # GPU→CPU transfer and lets cancellation be observed while a
                # fence is still in flight.
                self._poll_full_resolution_readbacks()
                # Use a small timeout to allow checking self.is_running
                item = self.command_queue.get(timeout=0.01)
                cmd_type, data = item

                # P1-1: Drain sentinel - process all accumulated lane commands
                if cmd_type == '_drain_pending':
                    self.command_queue.task_done()
                    # Reset the signaled flag so next lane submission can enqueue a new sentinel
                    with self._drain_pending_signal_lock:
                        self._drain_pending_signaled = False
                    pending_commands = self._drain_pending_slots()
                    if not pending_commands:
                        continue
                    # Execute each drained lane command.
                    # G-P0-11: 每条 lane 命令独立异常隔离——某条命令的 handler
                    # 抛错时，必须继续处理后续已 drain 的命令，并保证锁一定释放，
                    # 否则一次坏命令会让整批 lane 命令静默丢失。
                    for lane_cmd_type, lane_data in pending_commands:
                        mutex.lock()
                        try:
                            self._handle_command(lane_cmd_type, lane_data)
                        except Exception:
                            traceback.print_exc()
                        finally:
                            mutex.unlock()
                        self._renderer_stats['processed_frames'] += 1
                    continue

                # Non-lane (important) commands: process directly, no merging.
                # G-P0-11: task_done() 必须在 finally 中执行；此前它位于 try 之外，
                # 一旦 _handle_command 抛错就永远跳过，导致 command_queue.join() 永久阻塞。
                try:
                    mutex.lock()
                    try:
                        self._handle_command(cmd_type, data)
                    finally:
                        mutex.unlock()
                    self._renderer_stats['processed_frames'] += 1
                except Exception:
                    traceback.print_exc()
                finally:
                    self.command_queue.task_done()
            except queue.Empty:
                continue
            except Exception:
                traceback.print_exc()

        # Cleanup
        self._cleanup()

    def submit(self, cmd_type: str, data: Any = None) -> None:
        """
        Thread-safe submission of render commands with lane-based merging.

        Preview/playback/view-refresh commands are placed into merge lanes
        where newer commands replace older ones (only the latest is kept).
        Important commands (seek, master, export, compile, upload, bake, etc.)
        bypass the lanes and are enqueued directly so they are never lost.

        A sentinel value is placed in the actual queue so the worker thread
        knows to drain the pending slots.
        """
        # Determine which lane (if any) this command belongs to
        lane = self._get_lane_for_command(cmd_type, data)

        if lane is not None:
            # Preview/mergeable command: update the lane slot
            with self._pending_lock:
                old = self._pending_slots.get(lane)
                if old is None and isinstance(lane, tuple):
                    # 首次占用的 AUX 子槽登记顺序，drain 时按序消费
                    self._pending_aux_keys.append(lane)
                self._pending_slots[lane] = (cmd_type, data)
                if old is not None:
                    self._coalesced_lane_commands += 1
                    # Phase D: release GpuDecodedFrame if old command held one
                    if old[0] == RenderCommand.RENDER_VIDEO_GPU_FRAME:
                        old_data = old[1] or {}
                        old_gpu_frame = old_data.get('gpu_decoded_frame')
                        if old_gpu_frame is not None:
                            safe_release_gpu_frame(old_gpu_frame)
                # Track the latest GPU scope demand revision so stale
                # passes can be rejected even while mid-flight.
                if cmd_type == RenderCommand.RENDER_GPU_SCOPE and data is not None:
                    try:
                        self._scope_demand_revision = max(
                            self._scope_demand_revision,
                            getattr(data, 'revision', 0),
                        )
                    except Exception:
                        logger.debug('scope demand revision unavailable', exc_info=True)
            # Signal the worker to drain pending slots (deduplicated)
            with self._drain_pending_signal_lock:
                if not self._drain_pending_signaled:
                    self._drain_pending_signaled = True
                    self.command_queue.put(('_drain_pending', None))
        else:
            # Important command: enqueue directly, never discard
            self.command_queue.put((cmd_type, data))

    def register_full_resolution(self, request_id: str) -> bool:
        """Register a full-resolution request before queueing its command.

        Registration closes the race where the UI cancels a request after
        submission but before the worker has dequeued it.  The method is
        intentionally thread-safe and does not touch any GL state.
        """
        rid = str(request_id or '').strip()
        if not rid:
            return False
        lock = self.__dict__.get('_full_res_cancel_lock')
        active = self.__dict__.get('_full_res_active_requests')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is None:
            lock = threading.Lock()
            self._full_res_cancel_lock = lock
        if active is None:
            active = set()
            self._full_res_active_requests = active
        if cancelled is None:
            cancelled = set()
            self._full_res_cancelled_requests = cancelled
        with lock:
            if rid in active:
                return False
            active.add(rid)
            cancelled.discard(rid)
        return True

    def forget_full_resolution(self, request_id: str) -> None:
        """Remove a request that failed before its command reached the queue."""
        rid = str(request_id or '').strip()
        if not rid:
            return
        lock = self.__dict__.get('_full_res_cancel_lock')
        active = self.__dict__.get('_full_res_active_requests')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is None:
            return
        with lock:
            if active is not None:
                active.discard(rid)
            if cancelled is not None:
                cancelled.discard(rid)

    def cancel_full_resolution(self, request_id: str) -> bool:
        """Cooperatively cancel one queued or running full-resolution request."""
        rid = str(request_id or '').strip()
        if not rid:
            return False
        lock = self.__dict__.get('_full_res_cancel_lock')
        active = self.__dict__.get('_full_res_active_requests')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is None or active is None or cancelled is None:
            return False
        with lock:
            if rid not in active:
                return False
            cancelled.add(rid)
        return True

    def _full_resolution_cancelled(self, request_id: str | None = None) -> bool:
        """Return global-stop or request-scoped cancellation state."""
        if self._cancel_requested():
            return True
        rid = str(request_id or '').strip()
        if not rid:
            return False
        lock = self.__dict__.get('_full_res_cancel_lock')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is None or cancelled is None:
            return False
        with lock:
            return rid in cancelled

    def _render_cancelled(self, request_id: str | None = None) -> bool:
        """Cancellation adapter for lightweight renderer test doubles."""
        checker = getattr(self, '_full_resolution_cancelled', None)
        if callable(checker):
            return bool(checker(request_id))
        fallback = getattr(self, '_cancel_requested', None)
        return bool(fallback and fallback())

    def _emit_full_resolution_progress(
        self, request_id: str, percent: int, phase: str
    ) -> None:
        """Emit bounded progress without allowing telemetry to break rendering."""
        try:
            self.full_resolution_progress.emit(
                str(request_id), max(0, min(100, int(percent))), str(phase)
            )
        except Exception:
            logger.debug('full-resolution progress emission failed', exc_info=True)

    def discard_pending_grading_refresh(self) -> None:
        """Drop a queued grading refresh before the playback lane publishes.

        Playback owns presentation while running.  Clearing only the cached
        playback parameters is not enough if an older grading command is
        already waiting in the merge slot; that command could still publish a
        competing front texture once during the next tick.
        """
        with self._pending_lock:
            self._pending_slots[RenderLane.GRADING_REFRESH] = None

    def discard_pending_preview_refreshes(self) -> None:
        """Discard UI refreshes before the playback lane takes ownership.

        Both grading and viewport refreshes are safe while paused, but neither
        may publish a retained frame once continuous video playback starts.
        """
        with self._pending_lock:
            self._pending_slots[RenderLane.GRADING_REFRESH] = None
            self._pending_slots[RenderLane.VIEWPORT_REFRESH] = None

    def discard_pending_gpu_scope(self) -> None:
        """Drop any queued GPU scope demand.

        Called on seek / clip switch / playback start / panel hide so a
        stale analysis never publishes over a fresher source.  A pass
        already mid-flight is not forcibly interrupted; it re-validates
        the generation before emitting (see ``_do_render_gpu_scope``).
        """
        with self._pending_lock:
            self._pending_slots[RenderLane.GPU_SCOPE] = None

    def _get_lane_for_command(self, cmd_type: str, data: Any) -> Optional[object]:
        """
        P2-3: Determine which merge lane a command belongs to.

        Returns None for commands that must never be discarded (critical commands:
        seek, master, export, compile, upload, bake, etc.).

        Returns a RenderLane enum for mergeable preview/playback/refresh commands.

        RENDER_FRAME is split into different lanes based on render_reason:
        - 'viewport_refresh' -> RenderLane.VIEWPORT_REFRESH (zoom/pan/resize)
        - 'grading' -> RenderLane.GRADING_REFRESH (grading param changes)
        - 'full'/'playback'/other -> None (critical, not mergeable)
        """
        if cmd_type == RenderCommand.RENDER_VIDEO_FRAME:
            # Only proxy+playback video frames are mergeable
            d = data or {}
            quality = d.get('quality', 'proxy')
            reason = d.get('reason', 'playback')
            drop_if_stale = d.get('drop_if_stale', True)
            if drop_if_stale and quality == 'proxy' and reason == 'playback':
                return RenderLane.VIDEO_PLAYBACK
            # seek / master / export RENDER_VIDEO_FRAME: not mergeable
            return None
        elif cmd_type == RenderCommand.RENDER_VIDEO_GPU_FRAME:
            # Phase D: GPU zero-copy video frames use same merge lane as RENDER_VIDEO_FRAME
            d = data or {}
            quality = d.get('quality', 'proxy')
            reason = d.get('reason', 'playback')
            drop_if_stale = d.get('drop_if_stale', True)
            if drop_if_stale and quality == 'proxy' and reason == 'playback':
                return RenderLane.VIDEO_PLAYBACK
            return None
        elif cmd_type == RenderCommand.RENDER_FRAME:
            # P2-3: Split RENDER_FRAME based on render_reason
            d = data or {}
            render_reason = d.get('render_reason', 'full')
            if render_reason == 'viewport_refresh':
                return RenderLane.VIEWPORT_REFRESH
            elif render_reason == 'grading':
                return RenderLane.GRADING_REFRESH
            # 'full', 'playback', or other reasons -> not mergeable (critical)
            return None
        elif cmd_type == RenderCommand.UPDATE_AUX_TEX:
            # AUX 纹理携带 per-layer/per-type 状态（curves/hsl_curve/lut3d/
            # warper/mask/raw_input_lut），合并粒度必须按资源细分：
            # 单槽会在 sync_all_resources / 多层 mask 上传的紧循环里把
            # 不同资源的更新互相覆盖掉，导致加载后曲线/LUT/蒙版静默失效。
            d = data or {}
            return (RenderLane.AUX_TEX,
                    str(d.get('layer_id')), str(d.get('type')))
        elif cmd_type == RenderCommand.RENDER_COLOR_OUTPUT:
            return RenderLane.COLOR_OUTPUT
        elif cmd_type == RenderCommand.RENDER_SCOPE:
            return RenderLane.SCOPE_SNAPSHOT
        elif cmd_type == RenderCommand.RENDER_GPU_SCOPE:
            # GPU scope is the lowest-priority, latest-result-wins lane.
            # It runs after everything else so it always analyses the
            # newest published front texture without contending with the
            # video lane.  See docs/gpu_scope_implementation_plan_2026-08-05.md §5.1.
            return RenderLane.GPU_SCOPE
        # Everything else (UPLOAD_*, COMPILE_*, BAKE_*, REMOVE_*, RELEASE_*, etc.)
        # is NOT mergeable and must be processed in order.
        return None

    def _drain_pending_slots(self) -> List[Tuple[str, Any]]:
        """
        P2-3: Atomically drain all pending slot commands.

        Called by the worker thread when it encounters a _drain_pending sentinel.
        Returns a list of (cmd_type, data) tuples in a stable order.
        After draining, all slots are reset to None.

        Execution order (ensures correct state publication):
        1. AUX_TEX - update auxiliary textures first (dependencies)
        2. GRADING_REFRESH - update grading parameters
        3. VIDEO_PLAYBACK - upload/render video frame
        4. VIEWPORT_REFRESH - render with latest viewport (publishes last)
        5. COLOR_OUTPUT - render composite output
        6. SCOPE_SNAPSHOT - legacy CPU readback (deprecated fallback)
        7. GPU_SCOPE - native GPU scope (lowest priority, must read the
           freshest front texture and never block video frames)

        VIEWPORT_REFRESH executes after VIDEO_PLAYBACK to ensure:
        - If both pending, video frame is uploaded first
        - Final output uses latest viewport parameters
        - Prevents stale visible_rect from overriding fresh zoom/pan state
        """
        commands = []
        with self._pending_lock:
            # 1. AUX 子槽（依赖项先行），按提交顺序消费。
            # __dict__.get 而非 getattr：Mock 测试替身会自动创建任意属性，
            # 只有真实渲染器（__init__）或显式赋值的替身才在 __dict__ 里。
            aux_keys = self.__dict__.get('_pending_aux_keys') or ()
            for key in aux_keys:
                entry = self._pending_slots.pop(key, None)
                if entry is not None:
                    commands.append(entry)
            if isinstance(aux_keys, list):
                aux_keys.clear()
            # 2. 常规 lane 按固定顺序（grading → video → viewport → 输出/scope）
            for lane in (
                RenderLane.GRADING_REFRESH,
                RenderLane.VIDEO_PLAYBACK,
                RenderLane.VIEWPORT_REFRESH,
                RenderLane.COLOR_OUTPUT,
                RenderLane.SCOPE_SNAPSHOT,
                RenderLane.GPU_SCOPE,
            ):
                entry = self._pending_slots.get(lane)
                if entry is not None:
                    commands.append(entry)
                    self._pending_slots[lane] = None
        return commands

    def _load_utility_shaders(self):
        """Loads and compiles fixed-function shaders from GLSL files."""
        shader_dir = os.path.join(os.path.dirname(__file__), "..", "shaders")
        from src.features.color.shaders.shaders import PASSTHROUGH_VERTEX, PASSTHROUGH_FRAGMENT, RAW_VIEW_FRAGMENT
        
        def load_prog(name, path):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    frag = f.read()
                self.utility_shaders[name] = self.ctx.program(
                    vertex_shader=PASSTHROUGH_VERTEX,
                    fragment_shader=frag
                )
            except Exception as e:
                # AUD-0206: 编译失败必须留下完整可诊断日志（含名字与路径），
                # 消费方经 ``_get_utility_shader`` 拿到 None 后显式降级，
                # 而不是每帧 KeyError 被 run() 的宽泛 except 吞掉。
                logger.error("Failed to load utility shader %s (%s): %s", name, path, e)

        load_prog("keyer", os.path.join(shader_dir, "keyer.glsl"))
        load_prog("blur", os.path.join(shader_dir, "blur.glsl"))
        load_prog("morphology", os.path.join(shader_dir, "morphology.glsl"))
        load_prog("matte_denoise", os.path.join(shader_dir, "matte_denoise.glsl"))
        
        self.utility_shaders["passthrough"] = self.ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=PASSTHROUGH_FRAGMENT
        )

        self.utility_shaders["raw_view"] = self.ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=RAW_VIEW_FRAGMENT
        )

        mask_combine_fs = """
        #version 330
        uniform sampler2D u_matte_a;
        uniform sampler2D u_matte_b;
        uniform vec4 u_mask_uv_rect; // [offset_x, offset_y, scale_x, scale_y]
        in vec2 v_texcoord;
        out float f_result;
        void main() {
            // Map viewport UV to Image UV for sampling the full mask texture
            vec2 mask_uv = u_mask_uv_rect.xy + v_texcoord * u_mask_uv_rect.zw;
            f_result = texture(u_matte_a, v_texcoord).r * texture(u_matte_b, mask_uv).r;
        }
        """
        self.utility_shaders["mask_combine"] = self.ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=mask_combine_fs
        )

        mask_preview_fs = """
        #version 330
        uniform sampler2D u_tex;
        uniform sampler2D u_mask;
        uniform int u_mode;  // P1-COLOR-08: 0=highlight, 1=black, 2=matte(grayscale)
        in vec2 v_texcoord;
        out vec4 f_color;
        void main() {
            vec4 color = texture(u_tex, v_texcoord);
            float mask = texture(u_mask, v_texcoord).r;
            if (u_mode == 2) {
                // Grayscale matte: white selected, black unselected
                float g = clamp(mask, 0.0, 1.0);
                f_color = vec4(vec3(g), 1.0);
            } else if (u_mode == 1) {
                // Highlight over pure black
                f_color = vec4(color.rgb * mask, color.a);
            } else {
                // Highlight over mid-gray (default)
                vec3 background = vec3(0.2, 0.2, 0.2);
                f_color = vec4(mix(background, color.rgb, mask), color.a);
            }
        }
        """
        self.utility_shaders["mask_preview"] = self.ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=mask_preview_fs
        )

        # P2-1: Matte sampling shader — resamples a source-resolution matte into viewport space.
        # visible_rect [vx, vy, vw, vh] is in image-pixel coords (top-down origin).
        # v_texcoord is viewport UV (bottom-up, v=1 at top).
        # We map: mask_uv = offset + v_texcoord * scale
        #   u: offset_x = vx / img_w,  scale_x = vw / img_w
        #   v: viewport v=0 (bottom) -> image row vy+vh (top-down) -> (vy+vh)/img_h
        #       viewport v=1 (top)    -> image row vy      (top-down) -> vy/img_h
        #       => offset_y = (vy+vh)/img_h,  scale_y = -vh / img_h
        matte_sample_fs = """
        #version 330
        uniform sampler2D u_tex;
        uniform vec4 u_uv_rect; // [offset_x, offset_y, scale_x, scale_y]
        in vec2 v_texcoord;
        out float f_result;
        void main() {
            vec2 mask_uv = u_uv_rect.xy + v_texcoord * u_uv_rect.zw;
            f_result = texture(u_tex, mask_uv).r;
        }
        """
        self.utility_shaders["matte_sample"] = self.ctx.program(
            vertex_shader=PASSTHROUGH_VERTEX,
            fragment_shader=matte_sample_fs
        )

        # GPU scope shaders (native pipeline, no CPU readback).
        self._load_gpu_scope_shaders()

    def _get_utility_shader(self, name: str):
        """AUD-0206: 安全读取工具 shader；缺失（启动编译失败）时返回 None。

        仅对首次缺失记录 error 级降级日志，避免每帧刷屏。返回 None 时
        调用方必须显式降级对应功能路径；禁止用无守卫下标访问
        ``utility_shaders[name]``——那会让缺失退化为每帧 KeyError 被
        ``run()`` 的宽泛 except 静默吞掉。
        """
        prog = self.utility_shaders.get(name)
        if prog is None:
            warned = self.__dict__.setdefault('_missing_utility_shader_warned', set())
            if name not in warned:
                warned.add(name)
                logger.error(
                    "Utility shader '%s' unavailable (startup compile failed); "
                    "degrading the dependent feature path", name)
        return prog

    def _handle_command(self, cmd_type: str, data: Any) -> None:
        """渲染线程命令分发：把 RenderCommand 常量映射到对应的 ``_do_*``。

        由 ``run()`` 的命令循环调用（GL mutex 已持有）。未知命令被静默
        忽略；抛错的命令由外层循环的异常隔离兜底，不会中断线程。
        """
        if cmd_type == RenderCommand.RENDER_FRAME:
            self._do_render(data)
        elif cmd_type == RenderCommand.RENDER_FULL_RESOLUTION:
            self._do_render_full_resolution(data)
        elif cmd_type == RenderCommand.RENDER_COLOR_OUTPUT:
            self._do_render_color_output(data)
        elif cmd_type == RenderCommand.RENDER_SCOPE:
            self._do_render_scope_snapshot(data)
        elif cmd_type == RenderCommand.RENDER_GPU_SCOPE:
            self._do_render_gpu_scope(data)
        elif cmd_type == RenderCommand.UPLOAD_IMAGE:
            if isinstance(data, dict):
                # FIX: 传递 is_video 标志，防止视频模式下清除视频纹理状态
                self._do_upload(data['image'], is_reference=False, original_size=data.get('original_size'), is_video=data.get('is_video', False))
            else:
                self._do_upload(data, is_reference=False)
        elif cmd_type == RenderCommand.UPLOAD_REFERENCE:
            if isinstance(data, dict):
                self._do_upload(data['image'], is_reference=True, original_size=data.get('original_size'))
            else:
                self._do_upload(data, is_reference=True)
        elif cmd_type == RenderCommand.CLEAR_REFERENCE:
            self._do_clear_reference()
        elif cmd_type == RenderCommand.UPLOAD_VIDEO_FRAME:
            self._do_upload_video_frame(data)
        elif cmd_type == RenderCommand.UPLOAD_DECODED_VIDEO_FRAME:
            # P4.A: Upload packed RGB/RGBA from DecodedFrame
            self._do_upload_decoded_video_frame(data)
        elif cmd_type == RenderCommand.UPLOAD_VIDEO_PLANES:
            # P4.A: Upload YUV/NV12/P010 planes from DecodedFrame
            self._do_upload_video_planes(data)
        elif cmd_type == RenderCommand.UPDATE_UNIFORMS:
            self._do_update_uniforms(data)
        elif cmd_type == RenderCommand.UPDATE_AUX_TEX:
            self._do_update_aux_tex(data)
        elif cmd_type == RenderCommand.CLEAR_AUX_TEX:
            self._do_clear_aux_tex(data)
        elif cmd_type == RenderCommand.REMOVE_LAYER:
            self._do_remove_layer(data)
        elif cmd_type == RenderCommand.BAKE_LAYER:
            self._do_bake_layer(data)
        elif cmd_type == RenderCommand.UPLOAD_BAKED_TEXTURE:
            self._do_upload_baked_texture(data)
        elif cmd_type == RenderCommand.CLEAR_BAKED_TEXTURE:
            self._do_clear_baked_texture(data)
        elif cmd_type == RenderCommand.UPLOAD_AI_PLUGIN_RESULT:
            self._do_upload_ai_plugin_result(data)
        elif cmd_type == RenderCommand.REMOVE_AI_PLUGIN_TEXTURE:
            self._do_remove_ai_plugin_texture(data)
        elif cmd_type == RenderCommand.RELEASE_VIDEO_TEXTURE_POOL:
            self._do_release_video_texture_pool()
        elif cmd_type == RenderCommand.RENDER_VIDEO_FRAME:
            # P4.B: Combined upload + render for video playback
            self._do_render_video_frame(data)
        elif cmd_type == RenderCommand.RENDER_VIDEO_GPU_FRAME:
            # Phase D: GPU zero-copy render (GpuDecodedFrame)
            self._do_render_video_gpu_frame(data)
        elif cmd_type == RenderCommand.SET_VIDEO_DENOISE:
            self._do_set_video_denoise(data)
        elif cmd_type == RenderCommand.SET_VIDEO_DENOISE_WINDOW:
            self._do_set_video_denoise_window(data)
        elif cmd_type == RenderCommand.RESET_VIDEO_DENOISE_HISTORY:
            # BUG-A1-02：__dict__.get 守卫（与 run()/_do_render 等处惯例一致）。
            # 未跑 __init__ 的 __new__ 测试桩上缺失属性时 PyQt6 的
            # getattr(self, x, default) 抛 RuntimeError，默认值兜不住。
            pipeline = self.__dict__.get('video_denoise_pipeline', None)
            if pipeline is not None:
                pipeline.reset_temporal_history()

    def _do_set_video_denoise(self, data):
        """M5: 更新视频降噪参数（UI 线程经 submit 投递，GL mutex 下执行）。"""
        if not isinstance(data, dict):
            return
        from src.shared.video.denoise.params import VideoDenoiseParams

        params = VideoDenoiseParams.from_dict(data.get('params', {}))
        self.__dict__['video_denoise_params'] = params
        quality_mode = data.get('quality_mode', 'preview')
        if quality_mode in ('preview', 'paused', 'export'):
            self.__dict__['video_denoise_quality_mode'] = quality_mode
        pipeline = getattr(self, 'video_denoise_pipeline', None)
        if pipeline is not None:
            pipeline.set_params(params, quality_mode)
            if 'debug_overlay' in data:
                pipeline.set_debug_overlay(bool(data['debug_overlay']))

    def set_video_denoise_params(self, params, quality_mode: str = 'preview',
                                 debug_overlay: Optional[bool] = None) -> None:
        """UI 线程入口：线程安全投递降噪参数（合并策略：直接入队不合并）。"""
        if params is None:
            return
        try:
            payload = {'params': params.to_dict(), 'quality_mode': quality_mode}
            if debug_overlay is not None:
                payload['debug_overlay'] = bool(debug_overlay)
            self.submit(RenderCommand.SET_VIDEO_DENOISE, payload)
        except Exception:
            logger.exception("set_video_denoise_params submit failed")

    def reset_video_denoise_history(self) -> None:
        """UI 线程入口：请求清空时域历史（seek 后 UI 侧显式重置）。"""
        try:
            self.submit(RenderCommand.RESET_VIDEO_DENOISE_HISTORY, None)
        except Exception:
            logger.debug('video denoise history reset submit failed', exc_info=True)

    # ------------------------------------------------------------------
    # M4.2: 暂停态邻帧窗口（Paused 质量模式）
    # ------------------------------------------------------------------

    def _do_set_video_denoise_window(self, data):
        """接收/清除暂停态邻帧窗口（UI 后台线程取帧转换后投递）。

        payload: {'path', 'center_frame', 'neighbors': [(frame_index,
        rgba_f16 ndarray), ...] 最旧→最新不含当前帧}；空 dict 清除。
        只存数据不建 GL 对象——纹理在首次匹配当前帧时懒上传，
        避免 seek 后窗口未被消费就浪费上传带宽。
        """
        self._release_video_denoise_window_textures()
        if not isinstance(data, dict) or not data.get('neighbors'):
            self.__dict__.pop('_video_denoise_window', None)
            return
        self.__dict__['_video_denoise_window'] = {
            'path': data.get('path'),
            'center_frame': int(data.get('center_frame', -1)),
            'neighbors': list(data['neighbors']),
            'textures': None,
        }

    def _release_video_denoise_window_textures(self) -> None:
        """释放暂停态邻帧窗口已懒上传的纹理（渲染线程）。

        只清 ``textures`` 列表；窗口的 CPU 数据（neighbors）保留，
        下次匹配当前帧时按需重新懒上传。
        """
        window = self.__dict__.get('_video_denoise_window')
        if window is None:
            return
        for tex in (window.get('textures') or []):
            try:
                tex.release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
        window['textures'] = None

    def _get_video_denoise_window_textures(self, window):
        """懒上传邻帧纹理（RGBA16F，原始数据尺寸），已上传则直接复用。"""
        if window.get('textures'):
            return window['textures']
        textures = []
        try:
            for _frame_index, rgba in window['neighbors']:
                h, w = rgba.shape[:2]
                tex = self.ctx.texture((int(w), int(h)), components=4, dtype='f2')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                tex.repeat_x = False
                tex.repeat_y = False
                tex.write(rgba)
                textures.append(tex)
        except Exception:
            logger.exception("_get_video_denoise_window_textures failed")
            for tex in textures:
                try:
                    tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            window['textures'] = []
            return []
        window['textures'] = textures
        return textures

    def set_video_denoise_window(self, payload) -> None:
        """UI 线程入口：投递暂停态邻帧窗口（空 dict 清除）。"""
        try:
            self.submit(RenderCommand.SET_VIDEO_DENOISE_WINDOW, payload or {})
        except Exception:
            logger.debug('video denoise window submit failed', exc_info=True)


    def _do_clear_aux_tex(self, data):
        """C-P0-05: Release a layer's stale AUX textures after a grade reset.

        ``Layer.reset_params()`` clears curves/LUT/warper/qualifier/mask in the
        model, but previously nothing told the GPU side — so the renderer kept
        binding the old ``layer_textures[id(layer)]`` entries (the bind path
        falls back to stale textures when present). Drop the layer's AUX slot
        so subsequent renders bind the identity defaults, matching the reset
        model state, and free the GL objects. Only the named layer is affected;
        ``REMOVE_LAYER`` should still be used for full layer teardown.
        """
        layer_id = data.get('layer_id') if isinstance(data, dict) else data
        if layer_id is None:
            return
        # Release and drop the layer's AUX textures (curves/hsl_curve/lut3d/warper/mask).
        tex_dict = self.layer_textures.pop(layer_id, None)
        if tex_dict:
            for tex in tex_dict.values():
                try:
                    tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
        # Invalidate the qualifier matte cache for this layer so reset
        # (e.g. qualifier cleared) does not keep showing the old matte.
        matte = self.matte_cache.pop(layer_id, None)
        if matte:
            try:
                matte['matte'].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
        vp_tex = self._matte_viewport_textures.pop(layer_id, None)
        if vp_tex is not None and self.fb_manager:
            try:
                self.fb_manager.release_temp_texture(vp_tex)
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)

    def _do_remove_layer(self, layer_id):
        """Release all GPU resources associated with a removed layer."""
        if layer_id in self.layer_textures:
            for tex in self.layer_textures[layer_id].values():
                try:
                    tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            del self.layer_textures[layer_id]
        if layer_id in self.matte_cache:
            try:
                self.matte_cache[layer_id]['matte'].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
            del self.matte_cache[layer_id]
        # P2-1: Clean up viewport-res matte texture
        vp_tex = self._matte_viewport_textures.pop(layer_id, None)
        if vp_tex is not None:
            self.fb_manager.release_temp_texture(vp_tex)
        # P1-COLOR-08: drop the per-layer mask upload epoch with its cache
        self._mask_epoch.pop(layer_id, None)
        # AI Baking: Clean up baked texture
        if layer_id in self.baked_textures:
            try:
                self.baked_textures[layer_id].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
            del self.baked_textures[layer_id]

        # AI Plugins: Clean up AI plugin textures
        keys_to_remove = [k for k in self.ai_plugin_textures if k[0] == layer_id]
        for key in keys_to_remove:
            try:
                self.ai_plugin_textures[key].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
            del self.ai_plugin_textures[key]

    def _do_update_uniforms(self, data: Dict[str, Any]) -> None:
        """UPDATE_UNIFORMS：缓存 UI 投递的渲染参数快照（渲染线程）。

        仅更新 ``self.layers`` / ``raw_preview_params`` / ``view_params``
        等 CPU 侧缓存，不触 GL；后续 RENDER_FRAME 命令以 ``view_params``
        为默认参数集合成。
        """
        if not data: return
        if 'layers' in data:
            self.layers = data['layers']
        if 'raw_preview' in data:
            self.raw_preview_params = data['raw_preview']
        self.view_params = data

    def _ensure_persistent_render_textures(
        self,
        width: int,
        height: int
    ) -> Tuple[moderngl.Texture, moderngl.Framebuffer, moderngl.Texture, moderngl.Framebuffer, moderngl.Texture, moderngl.Framebuffer]:
        """
        P4: 获取或创建持久化的渲染纹理（raw_view/ping/pong）。

        避免每帧从 pool 查找和重新绑定纹理，减少 GPU pass 开销。
        当尺寸变化时，重新创建纹理。

        Args:
            width: 纹理宽度
            height: 纹理高度

        Returns:
            (raw_view_tex, raw_view_fbo, ping_tex, ping_fbo, pong_tex, pong_fbo)
        """
        current_size = (width, height)

        # 尺寸匹配，直接返回缓存的纹理
        if self._persistent_tex_size == current_size and self._persistent_raw_view_tex is not None:
            return (
                self._persistent_raw_view_tex, self._persistent_raw_view_fbo,
                self._persistent_ping_tex, self._persistent_ping_fbo,
                self._persistent_pong_tex, self._persistent_pong_fbo
            )

        # 尺寸变化，释放旧纹理并创建新的
        for tex in [self._persistent_raw_view_tex, self._persistent_ping_tex, self._persistent_pong_tex]:
            if tex:
                try: tex.release()
                except Exception: logger.debug("GPU resource release failed during cleanup", exc_info=True)
        for fbo in [self._persistent_raw_view_fbo, self._persistent_ping_fbo, self._persistent_pong_fbo]:
            if fbo:
                try: fbo.release()
                except Exception: logger.debug("GPU resource release failed during cleanup", exc_info=True)

        # 创建新纹理
        self._persistent_raw_view_tex = self.ctx.texture((width, height), 4, dtype='f2')
        self._persistent_raw_view_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._persistent_raw_view_tex.repeat_x = False
        self._persistent_raw_view_tex.repeat_y = False
        self._persistent_raw_view_fbo = self.ctx.framebuffer(color_attachments=[self._persistent_raw_view_tex])

        self._persistent_ping_tex = self.ctx.texture((width, height), 4, dtype='f2')
        self._persistent_ping_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._persistent_ping_tex.repeat_x = False
        self._persistent_ping_tex.repeat_y = False
        self._persistent_ping_fbo = self.ctx.framebuffer(color_attachments=[self._persistent_ping_tex])

        self._persistent_pong_tex = self.ctx.texture((width, height), 4, dtype='f2')
        self._persistent_pong_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self._persistent_pong_tex.repeat_x = False
        self._persistent_pong_tex.repeat_y = False
        self._persistent_pong_fbo = self.ctx.framebuffer(color_attachments=[self._persistent_pong_tex])

        self._persistent_tex_size = current_size

        # 统计：新纹理分配
        self._temp_texture_alloc_count += 3

        logger.debug(f"_ensure_persistent_render_textures: Created new textures at {width}x{height}")

        return (
            self._persistent_raw_view_tex, self._persistent_raw_view_fbo,
            self._persistent_ping_tex, self._persistent_ping_fbo,
            self._persistent_pong_tex, self._persistent_pong_fbo
        )

    def _get_yuv_rgb_output_texture(
        self,
        width: int,
        height: int
    ) -> moderngl.Texture:
        """
        P3: 从 ring buffer 获取 YUV 转 RGB 输出纹理。

        按尺寸缓存纹理，使用 ring buffer 策略轮换，
        避免每帧创建新纹理。

        Args:
            width: 纹理宽度
            height: 纹理高度

        Returns:
            RGBA float16 纹理
        """
        size_key = (width, height)

        # AUD-0202: 记录尺寸访问顺序（LRU，最近使用的在尾部）。
        size_order = self._yuv_rgb_output_size_order
        if size_key in size_order:
            size_order.remove(size_key)
        size_order.append(size_key)

        # 检查缓存是否存在
        if size_key not in self._yuv_rgb_output_cache:
            self._yuv_rgb_output_cache[size_key] = []
            self._yuv_rgb_output_ring_idx[size_key] = 0
            self._evict_stale_yuv_rgb_output_sizes()

        cache_list = self._yuv_rgb_output_cache[size_key]

        # 如果缓存不足，创建新纹理
        if len(cache_list) < self._yuv_rgb_output_ring_size:
            tex = self.ctx.texture((width, height), 4, dtype='f2')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = False
            tex.repeat_y = False
            cache_list.append(tex)
            self._temp_texture_alloc_count += 1
            logger.debug(f"_get_yuv_rgb_output_texture: Created new texture at {width}x{height}")
            return tex

        # 使用 ring buffer 策略轮换
        idx = self._yuv_rgb_output_ring_idx[size_key]
        tex = cache_list[idx]
        self._yuv_rgb_output_ring_idx[size_key] = (idx + 1) % len(cache_list)

        return tex

    def _evict_stale_yuv_rgb_output_sizes(self) -> None:
        """淘汰最久未使用视口尺寸的 YUV→RGB ring（渲染线程）。

        AUD-0202：``_yuv_rgb_output_cache`` 此前按尺寸无限累积，窗口多次
        缩放后 GPU 内存单调增长（每尺寸 3 张视口级 float16 纹理）。本方法
        在新增尺寸键时把最旧的尺寸 ring 整组释放，仅保留最近
        ``_yuv_rgb_output_max_sizes`` 个尺寸。刚请求的尺寸总是位于 LRU 尾
        部，不会被本方法淘汰；这些纹理只在渲染线程同上下文内使用，不跨
        上下文消费，释放安全。若缓存的 YUV 转换 FBO 恰好包着被淘汰的纹
        理，一并丢弃，避免后续帧绑定已释放的纹理名。
        """
        while len(self._yuv_rgb_output_size_order) > self._yuv_rgb_output_max_sizes:
            evict_key = self._yuv_rgb_output_size_order.pop(0)
            textures = self._yuv_rgb_output_cache.pop(evict_key, [])
            self._yuv_rgb_output_ring_idx.pop(evict_key, None)
            evicted_glos: set = set()
            for tex in textures:
                try:
                    evicted_glos.add(int(tex.glo))
                    tex.release()
                except Exception:
                    logger.debug(
                        "Failed to release YUV RGB output texture for size %s",
                        evict_key,
                        exc_info=True,
                    )
            if (
                self._yuv_conv_fbo is not None
                and self._yuv_conv_fbo_tex_glo in evicted_glos
            ):
                try:
                    self._yuv_conv_fbo.release()
                except Exception:
                    logger.debug(
                        "Failed to release stale YUV conversion FBO",
                        exc_info=True,
                    )
                self._yuv_conv_fbo = None
                self._yuv_conv_fbo_tex_glo = 0
            logger.debug(
                "Evicted YUV RGB output ring for size %s (%d textures)",
                evict_key,
                len(textures),
            )

    def _sync_gpu(self) -> bool:
        """
        P1: 根据 video_render_sync_mode 执行 GPU 同步。

        Returns:
            当前渲染命令是否已由阻塞式同步完整执行。

        Modes:
        - "finish": 强制 GPU 完成（最安全，高延迟）
        - "fence": 使用 GL fence 同步（推荐，中等延迟）
        - "none": 不同步（最快，可能帧撕裂）

        默认为 "none"，视频播放时不每帧 glFinish()，避免把播放线程拖慢。
        黑屏问题由渲染 FBO 绑定顺序修复，不能依赖每帧强制同步掩盖。
        """
        mode = self.video_render_sync_mode

        if mode == "finish":
            self.ctx.finish()
            self._finish_call_count += 1
            return True
        elif mode == "fence":
            # ModernGL 不直接支持 fence，这里用 finish() 模拟
            # 未来可考虑使用 moderngl.Context.finish() 或 OpenGL fence 扩展
            self.ctx.finish()
            self._finish_call_count += 1
            return True
        # "none" 模式：不调用任何同步方法
        return False

    def _retire_front_present_fence(self) -> None:
        """Delete the superseded Color-canvas presentation fence.

        This method is called only from the render thread while the shared GL
        mutex is held. A UI-side glWaitSync that was already queued keeps its
        server-side reference valid after deletion; a UI repaint that has not
        started always reads the newer fence instead.
        """
        fence = getattr(self, 'current_front_fence', None)
        self.current_front_fence = None
        if not fence:
            return
        try:
            from OpenGL.GL import glDeleteSync

            glDeleteSync(fence)
        except Exception:
            logger.debug('Failed to retire Color front-texture fence', exc_info=True)

    def _publish_front_present_fence(self) -> Optional[Any]:
        """Publish a non-blocking cross-context completion fence for video.

        ctx.finish() makes the render worker wait every frame. A fence lets the
        UI context place the wait immediately before its own blit, so the GPU
        preserves frame ordering without stalling decode or grading. When sync
        objects are unavailable, finish is used as a correctness fallback.
        """
        self._retire_front_present_fence()
        fence = None
        try:
            from OpenGL.GL import (
                GL_SYNC_GPU_COMMANDS_COMPLETE,
                glFenceSync,
                glFlush,
            )

            fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            # The consumer lives in the GUI's shared context. The fence must
            # enter the command stream before its glWaitSync can observe it.
            glFlush()
            if fence:
                self.current_front_fence = fence
                return fence
        except Exception:
            logger.debug('Failed to create Color front-texture fence', exc_info=True)

        # A driver without sync-object support is uncommon, but presenting an
        # unfinished double-buffer slot produces old/new frame alternation.
        try:
            self.ctx.finish()
            self._finish_call_count += 1
        except Exception:
            logger.debug('Color front-texture finish fallback failed', exc_info=True)
        if fence:
            try:
                from OpenGL.GL import glDeleteSync

                glDeleteSync(fence)
            except Exception:
                logger.debug(
                    'Failed to retire unpublished Color front-texture fence',
                    exc_info=True,
                )
        return None

    def _update_front_present_sync(self, *, render_complete: bool) -> None:
        """Publish the synchronization state for the current front texture.

        ``allow_during_playback`` controls render ownership, not whether GPU
        writes are visible in the Canvas shared context. Any front texture
        that has not completed on the producer must therefore carry a fence,
        including a paused grading render.
        """
        if render_complete:
            self._retire_front_present_fence()
        else:
            self._publish_front_present_fence()

    def get_render_stats(self) -> Dict[str, Any]:
        """
        获取渲染统计信息（用于测试验证）。

        Returns:
            Dict 包含：
            - temp_texture_alloc_count: 临时纹理分配次数
            - finish_call_count: ctx.finish() 调用次数
            - persistent_tex_size: 当前持久化纹理尺寸
            - yuv_rgb_cache_sizes: YUV RGB 缓存中的纹理数量
        """
        return {
            'temp_texture_alloc_count': self._temp_texture_alloc_count,
            'finish_call_count': self._finish_call_count,
            'persistent_tex_size': self._persistent_tex_size,
            'yuv_rgb_cache_sizes': {str(k): len(v) for k, v in self._yuv_rgb_output_cache.items()},
            'video_render_sync_mode': self.video_render_sync_mode,
        }

    def reset_render_stats(self) -> None:
        """重置渲染统计计数器（每帧开始时调用）"""
        self._temp_texture_alloc_count = 0
        self._finish_call_count = 0

    def _do_render_scope_snapshot(self, data: Dict[str, Any] | None) -> None:
        """Read a reduced copy of the published front texture on the render thread.

        .. deprecated::
            This is the legacy CPU-readback scope path. It performs an
            ``fbo.read()`` (a synchronous GPU stall) and ships an ndarray to
            the UI, which is why it is gated off during playback (see
            ``LayerOpsMixin.on_frame_ready``).  The native GPU scope pipeline
            (``_do_render_gpu_scope``) replaces it for realtime use.  This
            method is intentionally retained as an explicit, paused-only
            fallback / reference oracle and MUST NOT be re-enabled during
            playback.  See docs/gpu_scope_implementation_plan_2026-08-05.md §6/§8.
        """
        if not self.ctx or self.current_front_tex_id is None:
            return

        max_dim = int((data or {}).get('max_dim', 512) or 512)
        max_dim = max(1, max_dim)
        front_texture = next(
            (
                texture for texture in self.tex_pair
                if texture is not None and texture.glo == self.current_front_tex_id
            ),
            None,
        )
        if front_texture is None:
            return

        source_w, source_h = self.current_front_size
        if source_w <= 0 or source_h <= 0:
            return
        scale = min(1.0, max_dim / max(source_w, source_h))
        target_size = (
            max(1, int(round(source_w * scale))),
            max(1, int(round(source_h * scale))),
        )

        try:
            if self._scope_snapshot_size != target_size:
                if self._scope_snapshot_fbo is not None:
                    self._scope_snapshot_fbo.release()
                if self._scope_snapshot_tex is not None:
                    self._scope_snapshot_tex.release()
                self._scope_snapshot_tex = self.ctx.texture(target_size, 3, dtype='f4')
                self._scope_snapshot_fbo = self.ctx.framebuffer(
                    color_attachments=[self._scope_snapshot_tex]
                )
                self._scope_snapshot_size = target_size

            self._scope_snapshot_fbo.use()
            self.ctx.viewport = (0, 0, *target_size)
            self.ctx.scissor = None
            self._scope_snapshot_fbo.clear(0.0, 0.0, 0.0, 1.0)
            program = self.utility_shaders.get('passthrough')
            front_texture.use(0)
            program['u_texture'].value = 0
            self._render_quad(program)
            pixels = self._scope_snapshot_fbo.read(
                components=3,
                alignment=1,
                dtype='f4',
            )
            image = np.frombuffer(pixels, dtype=np.float32).reshape(
                target_size[1], target_size[0], 3,
            )
            image_bgr = cv2.cvtColor(np.flipud(image), cv2.COLOR_RGB2BGR)
            self.scope_snapshot_ready.emit(image_bgr)
        except Exception as exc:
            logger.debug("Scope snapshot readback failed: %s", exc)

    # ------------------------------------------------------------------
    # GPU scope (native pipeline — see scope_types.py and
    # docs/gpu_scope_implementation_plan_2026-08-05.md)
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # GPU scope passes (delegated to the shared GpuScopeEngine)
    # ------------------------------------------------------------------
    def _ensure_scope_engine(self):
        """Return the context-bound scope engine, creating it lazily.

        The engine must be constructed on the render thread with this
        renderer's context current (its GL objects follow that context).
        """
        engine = getattr(self, '_gpu_scope_engine', None)
        if engine is None or getattr(engine, 'ctx', None) is not self.ctx:
            from src.shared.gpu.gpu_scope_engine import GpuScopeEngine
            engine = GpuScopeEngine(self.ctx)
            self._gpu_scope_engine = engine
            self._gpu_scope_pool_cache = engine._pool_cache
            # P3-03: scope 资源组登记（懒创建点）。
            backend = getattr(self, '_gpu_backend', None)
            if backend is not None:
                backend.registry.register(GROUP_SCOPE, "scope_engine", engine)
        return engine

    def _load_gpu_scope_shaders(self) -> None:
        """Compile the GPU scope programs from shaders/gpu_scope.glsl.

        All scope stages (sample / waveform / vectorscope / parade / luma /
        histogram / CIE / false color) live in the shared engine; failures
        are logged there and surface via the returned flag.
        """
        self._gpu_scope_shaders_loaded = (
            self._ensure_scope_engine().load_shaders()
        )

    def _ensure_gpu_scope_pool(self, analysis_size, waveform_size, vectorscope_size):
        """Allocate/reuse the GPU scope resource pool (via the engine).

        Resources are keyed by their intended size and only reallocated when
        the size changes (plan §9.1 "no per-frame allocation").
        """
        pool = self._ensure_scope_engine().ensure_pool(
            analysis_size, waveform_size, vectorscope_size
        )
        self._gpu_scope_pool = pool
        return pool

    def _release_gpu_scope_pool(self) -> None:
        """Release every GPU scope resource. Called from cleanup()."""
        engine = getattr(self, '_gpu_scope_engine', None)
        if engine is not None:
            engine.release()
        self._gpu_scope_pool = None
        if isinstance(getattr(self, '_gpu_scope_pool_cache', None), dict):
            self._gpu_scope_pool_cache.clear()

    def _scope_sample_front(
        self,
        front_texture,
        pool,
        mask_texture=None,
        mask_uv_rect=(0.0, 0.0, 1.0, 1.0),
    ) -> None:
        """Downscale the front texture, optionally applying a GPU mask."""
        self._ensure_scope_engine().sample(
            front_texture, pool,
            mask_texture=mask_texture, mask_uv_rect=mask_uv_rect,
        )

    def _scope_mask_uv_rect(self, mask_texture):
        """Map front-texture UVs to the renderer-owned image mask texture."""
        visible_rect = getattr(self, 'rendered_visible_rect', None)
        if mask_texture is None or not visible_rect:
            return (0.0, 0.0, 1.0, 1.0)
        try:
            source_w, source_h = getattr(self, 'source_image_size', (0, 0))
            if source_w <= 0 or source_h <= 0:
                source_w, source_h = getattr(self, 'video_source_size', (0, 0))
            if source_w <= 0 or source_h <= 0:
                return (0.0, 0.0, 1.0, 1.0)
            vx, vy, vw, vh = (float(v) for v in visible_rect)
            if vw <= 0.0 or vh <= 0.0:
                return (0.0, 0.0, 1.0, 1.0)
            # Mask textures use top-down image coordinates while OpenGL UVs
            # are bottom-up, matching the existing mask-combine mapping.
            return (
                vx / float(source_w),
                (vy + vh) / float(source_h),
                vw / float(source_w),
                -vh / float(source_h),
            )
        except (TypeError, ValueError):
            return (0.0, 0.0, 1.0, 1.0)

    # ------------------------------------------------------------------
    # Scope pass wrappers (thin delegation to the shared GpuScopeEngine).
    #
    # All GL work for every scope mode lives in
    # src/shared/gpu/gpu_scope_engine.py so the Color renderer and the
    # Composite renderer execute byte-identical passes.  These wrappers
    # keep the historical call sites (and test seams) intact.
    # ------------------------------------------------------------------
    def _scope_accumulate_waveform(self, pool) -> None:
        """RGB waveform density (three additive POINTS passes, R/G/B)."""
        self._ensure_scope_engine().accumulate_waveform(pool)

    def _scope_accumulate_luma(self, pool) -> None:
        """Luma waveform density (BT.709 Y, white trace via channel 3)."""
        self._ensure_scope_engine().accumulate_waveform(pool, channel=3)

    def _scope_display_luma(self, pool: dict, *, target: str = 'wf_display') -> None:
        """Tone-map luma density (waveform display renders the white trace)."""
        self._ensure_scope_engine().display_waveform(pool, single_channel=True,
                                                   target=target)

    def _scope_display_waveform(self, pool) -> float:
        """Tone-map waveform/luma density -> RGBA8 with graticule."""
        self._ensure_scope_engine().display_waveform(pool)
        return 1.0

    def _scope_accumulate_parade(self, pool) -> None:
        """RGB parade: waveform accumulation viewport-partitioned in thirds."""
        self._ensure_scope_engine().accumulate_parade(pool)

    def _scope_display_parade(self, pool: dict, *, target: str = 'wf_display') -> None:
        """Tone-map parade density (waveform display over channel thirds)."""
        self._ensure_scope_engine().display_parade(pool, target=target)

    def _scope_accumulate_vectorscope(self, pool, zoom: float) -> None:
        """Accumulate density into the R16F vectorscope texture."""
        self._ensure_scope_engine().accumulate_vectorscope(pool, zoom)

    def _scope_display_vectorscope(self, pool, zoom: float) -> None:
        """Tone-map vectorscope density -> RGBA8 display texture."""
        self._ensure_scope_engine().display_vectorscope(pool, zoom)

    def _scope_accumulate_histogram(self, pool: dict, *, include_luma: bool = False,
                                    curve_mode: str = 'Custom') -> None:
        """Per-channel code-value point splat into the histogram density."""
        self._ensure_scope_engine().accumulate_histogram(pool, include_luma=include_luma, curve_mode=curve_mode)

    def _scope_reduce_histogram(self, pool) -> None:
        """Column-sum the scattered density into log1p totals (512x1)."""
        self._ensure_scope_engine().reduce_histogram(pool)

    def _scope_display_histogram(self, pool) -> None:
        """Draw the RGB histogram curves into the display texture."""
        self._ensure_scope_engine().display_histogram(pool)

    def _scope_display_curves_histogram(
        self, pool: dict, display_size: tuple[int, int], line_width: float,
        curve_mode: str = 'Custom',
    ) -> None:
        """Draw the dedicated YRGB underlay using the shared histogram counts."""
        self._ensure_scope_engine().display_curves_histogram(pool, display_size, line_width, curve_mode)

    def _scope_accumulate_cie(self, pool, demand) -> None:
        """CIE xy chromaticity density (transfer/gamut aware)."""
        self._ensure_scope_engine().accumulate_cie(
            pool,
            getattr(demand, 'scope_transfer', 'srgb'),
            getattr(demand, 'scope_primaries', 'bt709'),
        )

    def _scope_display_cie(self, pool, demand) -> None:
        """Heat-map display pass; plot chrome is presenter-side."""
        self._ensure_scope_engine().display_cie(pool)

    def _scope_display_falsecolor(self, pool, source_texture, demand) -> None:
        """Single-pass false color / zebra over the untouched source."""
        self._ensure_scope_engine().display_falsecolor(
            source_texture, pool,
            mode=getattr(demand, 'falsecolor_mode', 'sdr'),
            transfer=getattr(demand, 'scope_transfer', 'srgb'),
            zebra_threshold=getattr(demand, 'zebra_threshold', 1.0),
        )

    def _release_scope_points_vao_cache(self) -> None:
        """Release the VAO/index-buffer pairs used by GPU scope passes."""
        engine = getattr(self, '_gpu_scope_engine', None)
        if engine is not None:
            for cached in engine._points_vao_cache.values():
                resources = cached if isinstance(cached, tuple) else (cached,)
                for resource in resources:
                    try:
                        resource.release()
                    except Exception:
                        logger.debug('GPU resource release failed during cleanup', exc_info=True)
            engine._points_vao_cache.clear()

    def _do_render_gpu_scope(self, demand) -> None:
        """Run the native GPU scope pipeline for a ``ScopeDemand``.

        Pipeline (all on the render thread, no CPU pixel readback):
          1. validate + generation guard
          2. ensure resource pool (cached by size)
          3. render/reuse full-frame ColorOutput, then sample it (+ optional
             GPU mask) -> analysis RGBA16F
          4. run the passes requested by ``demand.mode`` (waveform /
             vectorscope / split / parade / luma / histogram / cie /
             falsecolor) via the shared GpuScopeEngine
          5. emit ScopeGpuFrame descriptor (texture ids only)
        """
        from src.features.color.core.scope_types import (
            ScopeDemand, ScopeGpuFrame,
            mode_needs_waveform, mode_needs_vectorscope,
            mode_needs_parade, mode_needs_luma, mode_needs_histogram,
            mode_needs_cie, mode_needs_falsecolor,
            DEFAULT_WAVEFORM_DENSITY_SIZE, DEFAULT_VECTORSCOPE_DENSITY_SIZE,
            DEFAULT_HISTOGRAM_DISPLAY_SIZE, DEFAULT_CIE_SIZE,
            DEFAULT_FALSECOLOR_SIZE,
        )

        if not self.gpu_scope_enabled or not self.ctx:
            return
        if not isinstance(demand, ScopeDemand):
            return
        if self.current_front_tex_id is None:
            return
        if not getattr(self, '_gpu_scope_shaders_loaded', False):
            # Shader compile failed at startup — disable the lane. The normal
            # UI path is GPU-only, so it must remain blank/last-frame rather
            # than silently re-enabling CPU readback.
            self.gpu_scope_enabled = False
            with self._pending_lock:
                self._pending_slots[RenderLane.GPU_SCOPE] = None
            return

        # Generation guard: only a newer UI demand may invalidate this pass.
        # ``source_revision`` is a lower-bound freshness marker, not an exact
        # texture identity: GPU_SCOPE intentionally drains after video, so a
        # queued demand often reaches this lane after a newer front texture has
        # already been published. Analysing that newer front is valid (and is
        # accepted by ``is_frame_valid_for_demand``); rejecting it starves the
        # scope for the entire duration of playback.
        if demand.revision < self._scope_demand_revision:
            return

        t0 = time.monotonic()
        try:
            analysis_size = (
                max(1, int(demand.analysis_size[0])),
                max(1, int(demand.analysis_size[1])),
            )
            waveform_size = DEFAULT_WAVEFORM_DENSITY_SIZE
            vectorscope_size = DEFAULT_VECTORSCOPE_DENSITY_SIZE

            pool = self._ensure_gpu_scope_pool(
                analysis_size, waveform_size, vectorscope_size
            )

            # ``current_front_tex_id`` is the canvas presentation buffer and
            # therefore follows visible_rect (pan/zoom).  Render a compact
            # full-frame ColorOutput on content changes and use that texture
            # for scope sampling during both playback and paused grading.  This
            # keeps scope geometry stable and excludes the canvas background
            # from the signal.  Viewport-only refreshes reuse the cached output.
            # The scope-specific ColorOutput path skips its intermediate finish;
            # the single finish below publishes the completed scope textures.
            scope_source = None
            scope_source_is_full_frame = False
            scope_params = getattr(self, '_last_scope_render_params', None)
            if not scope_params:
                return
            content_revision = int(getattr(self, '_scope_content_revision', 0))
            output_revision = int(
                getattr(self, '_scope_color_output_revision', -1)
            )
            output_max_dim = int(
                getattr(self, '_scope_color_output_max_dim', 0)
            )
            requested_max_dim = max(analysis_size)
            if (
                getattr(self, '_scope_color_output_tex', None) is None
                or output_revision != content_revision
                or output_max_dim != requested_max_dim
            ):
                output_params = dict(scope_params)
                # The scope analysis size is the quality budget for this pass;
                # never render an invisible full-resolution duplicate.
                output_params['max_proxy_dim'] = requested_max_dim
                output_params['color_output_target'] = 'scope'
                if not self._do_render_color_output(output_params):
                    return
                self._scope_color_output_revision = content_revision
                self._scope_color_output_max_dim = requested_max_dim
            candidate = getattr(self, '_scope_color_output_tex', None)
            candidate_size = getattr(candidate, 'size', (0, 0))
            if (
                candidate is None
                or candidate_size[0] <= 0
                or candidate_size[1] <= 0
            ):
                self._scope_color_output_revision = -1
                return
            scope_source = candidate
            scope_source_is_full_frame = True

            # 1. Downscale front texture to analysis resolution. The mask is
            # resolved from the renderer-owned GPU texture table and applied
            # before any density pass; no mask ndarray is created here.
            # (False color samples the untouched source directly instead.)
            mask_texture = None
            if demand.mask_enabled and demand.mask_layer_id:
                mask_texture = self.layer_textures.get(
                    demand.mask_layer_id, {}
                ).get('mask')
            if demand.mode != 'falsecolor' or bool(
                getattr(demand, 'histogram_overlay', False)
            ):
                self._scope_sample_front(
                    scope_source,
                    pool,
                    mask_texture=mask_texture,
                    mask_uv_rect=(0.0, 0.0, 1.0, 1.0)
                    if scope_source_is_full_frame
                    else self._scope_mask_uv_rect(mask_texture),
                )

            wf_id = None
            vs_id = None
            parade_id = None
            luma_id = None
            hist_id = None
            curves_hist_id = None
            cie_id = None
            fc_id = None
            need_wf = mode_needs_waveform(demand.mode)
            need_vs = mode_needs_vectorscope(demand.mode)

            if need_wf:
                self._scope_accumulate_waveform(pool)
                self._scope_display_waveform(pool)
                wf_id = pool['wf_display'].glo

            if need_vs:
                self._scope_accumulate_vectorscope(pool, demand.vectorscope_zoom)
                self._scope_display_vectorscope(pool, demand.vectorscope_zoom)
                vs_id = pool['vs_display'].glo

            if mode_needs_parade(demand.mode):
                self._scope_accumulate_parade(pool)
                if demand.mode in ('quad', 'all'):
                    self._scope_display_parade(pool, target='parade_display')
                    parade_id = pool['parade_display'].glo
                else:
                    self._scope_display_parade(pool)
                    parade_id = pool['wf_display'].glo

            if mode_needs_luma(demand.mode):
                self._scope_accumulate_luma(pool)
                if demand.mode == 'all':
                    self._scope_display_luma(pool, target='luma_display')
                    luma_id = pool['luma_display'].glo
                else:
                    self._scope_display_luma(pool)
                    luma_id = pool['wf_display'].glo

            if mode_needs_histogram(demand.mode) or bool(
                getattr(demand, 'histogram_overlay', False)
            ):
                if bool(getattr(demand, 'histogram_overlay', False)):
                    self._scope_accumulate_histogram(pool, include_luma=True, curve_mode=demand.curves_mode)
                else:
                    self._scope_accumulate_histogram(pool)
                self._scope_reduce_histogram(pool)
                self._scope_display_histogram(pool)
                hist_id = pool['hist_display'].glo
                if bool(getattr(demand, 'histogram_overlay', False)):
                    self._scope_display_curves_histogram(pool, demand.curves_display_size,
                                                        demand.curves_line_width, demand.curves_mode)
                    curves_hist_id = pool['curves_hist_display'].glo

            if mode_needs_cie(demand.mode):
                self._scope_accumulate_cie(pool, demand)
                self._scope_display_cie(pool, demand)
                cie_id = pool['cie_display'].glo

            if mode_needs_falsecolor(demand.mode):
                self._scope_display_falsecolor(pool, scope_source, demand)
                fc_id = pool['fc_display'].glo

            # Cross-context visibility: the presenter (UI thread / its own
            # context) reads these textures via glBlitFramebuffer. Insert an
            # explicit finish so the writes are visible before we publish the
            # texture ids. This is a full-pipeline sync, but it runs only on
            # the rate-limited, low-priority GPU_SCOPE lane rather than on
            # every video render. Cheaper than a CPU readback.
            self.ctx.finish()

            analysis_ms = (time.monotonic() - t0) * 1000.0
            frame = ScopeGpuFrame(
                revision=demand.revision,
                source_revision=self._scope_source_revision,
                waveform_texture_id=wf_id,
                waveform_size=waveform_size if need_wf else (0, 0),
                vectorscope_texture_id=vs_id,
                vectorscope_size=vectorscope_size if need_vs else (0, 0),
                parade_texture_id=parade_id,
                parade_size=waveform_size if parade_id is not None else (0, 0),
                luma_texture_id=luma_id,
                luma_size=waveform_size if luma_id is not None else (0, 0),
                histogram_texture_id=hist_id,
                curves_histogram_texture_id=curves_hist_id,
                curves_mode=demand.curves_mode,
                curves_histogram_size=(
                    pool['curves_hist_display_size']
                    if curves_hist_id is not None else (0, 0)
                ),
                histogram_size=(
                    DEFAULT_HISTOGRAM_DISPLAY_SIZE
                    if hist_id is not None else (0, 0)
                ),
                cie_texture_id=cie_id,
                cie_size=DEFAULT_CIE_SIZE if cie_id is not None else (0, 0),
                falsecolor_texture_id=fc_id,
                falsecolor_size=(
                    DEFAULT_FALSECOLOR_SIZE if fc_id is not None else (0, 0)
                ),
                analysis_ms=analysis_ms,
            )
            self.gpu_scope_ready.emit(frame)
        except Exception as exc:
            # A single failed pass should not permanently disable the scope
            # (e.g. transient GL error, empty-VAO driver quirk). Log once per
            # distinct error to avoid spamming, and keep the lane alive.
            #
            # NOTE: moderngl.Error frequently carries an *empty* message (the
            # underlying GL error code has no mapped text on some drivers).
            # Deduping on str(exc) collapses every distinct failure into the
            # same "" key and hides the real cause.  Dedupe on the exception
            # type + the innermost source line instead, and emit a full
            # traceback the first time so the failing stage is identifiable.
            tb_text = traceback.format_exc()
            tb_lines = [ln.strip() for ln in tb_text.splitlines() if ln.strip()]
            err_key = "{}@{}".format(
                type(exc).__name__, tb_lines[-1] if tb_lines else "?"
            )[:200]
            seen = getattr(self, '_gpu_scope_errors', None)
            if seen is None:
                seen = {}
                self._gpu_scope_errors = seen  # type: ignore[attr-defined]
            count = seen.get(err_key, 0)
            if count < 3:
                logger.warning(
                    "GPU scope pass failed: %s: %r", type(exc).__name__, exc
                )
                if count == 0:
                    logger.warning(
                        "GPU scope first-failure traceback:\n%s", tb_text
                    )
            seen[err_key] = count + 1

    def _do_render(
        self,
        params: Dict[str, Any],
        *,
        allow_during_playback: bool = False,
    ) -> bool:
        """渲染入口：以 ExitStack 包住 ``_do_render_impl``，保证临时纹理归还。

        ColorOutput 模式从 FramebufferManager 租借的临时纹理登记到
        ``render_temp_textures``，任何一层合成 pass 抛错也会在退出时归还
        池中，而不是泄漏或带着陈旧内容被下一帧复用。

        Args:
            params: 渲染参数（透传给 ``_do_render_impl``）。
            allow_during_playback: 播放激活时是否仍允许发布 front。

        Returns:
            ``_do_render_impl`` 的结果：True 已发布，False 被丢弃/取消。
        """
        with ExitStack() as render_temp_textures:
            return AsyncRenderer._do_render_impl(
                self,
                params,
                allow_during_playback=allow_during_playback,
                render_temp_textures=render_temp_textures,
            )

    def _finish_full_resolution_request(self, request_id: str) -> None:
        """Release request bookkeeping after a terminal result/failure."""
        rid = str(request_id or '').strip()
        if not rid:
            return
        lock = self.__dict__.get('_full_res_cancel_lock')
        active = self.__dict__.get('_full_res_active_requests')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is None:
            return
        with lock:
            if active is not None:
                active.discard(rid)
            if cancelled is not None:
                cancelled.discard(rid)

    def _ensure_full_res_pbo_readback(self):
        """Create the optional PBO readback ring on the current GL thread."""
        if self.__dict__.get('_full_res_pbo_disabled', False):
            return None
        existing = self.__dict__.get('_full_res_pbo_readback')
        if existing is not None:
            return existing
        ctx = self.__dict__.get('ctx')
        if ctx is None:
            return None
        try:
            from src.features.composite.render.pbo_readback import AsyncPBOReadback

            pbo = AsyncPBOReadback(
                ctx,
                ring_size=3,
                dtype='f4',
                reorder_bgr=True,
            )
            if not pbo.initialize():
                self._full_res_pbo_disabled = True
                return None
            self._full_res_pbo_readback = pbo
            logger.info('Full-resolution PBO readback enabled')
            return pbo
        except Exception:
            # PBO support is an optimization, never a correctness boundary.
            self._full_res_pbo_disabled = True
            logger.info(
                'Full-resolution PBO readback unavailable; using synchronous fallback',
                exc_info=True,
            )
            return None

    def _enqueue_full_resolution_readback(
        self, request_id: str, output_fbo, width: int, height: int
    ) -> bool:
        """Queue a GPU→CPU transfer and defer result emission until its fence signals."""
        pbo = self._ensure_full_res_pbo_readback()
        if pbo is None:
            return False
        try:
            if not pbo.enqueue(
                output_fbo,
                int(width),
                int(height),
                request_id=str(request_id),
            ):
                return False
            self._full_res_pending_readbacks[str(request_id)] = (
                int(width), int(height)
            )
            return True
        except Exception:
            logger.info(
                'Full-resolution PBO enqueue failed; using synchronous fallback',
                exc_info=True,
            )
            return False

    def _poll_full_resolution_readbacks(self) -> None:
        """Publish completed PBO results without blocking the render loop."""
        pbo = self.__dict__.get('_full_res_pbo_readback')
        pending = self.__dict__.get('_full_res_pending_readbacks')
        if pbo is None or not pending:
            return
        try:
            results = pbo.poll_ready()
        except Exception:
            logger.exception('Full-resolution PBO polling failed')
            for request_id in list(pending):
                pending.pop(request_id, None)
                self._emit_full_resolution_progress(request_id, 100, 'failed')
                self.full_resolution_failed.emit(request_id, 'readback_failed')
                self._finish_full_resolution_request(request_id)
            self._full_res_pbo_disabled = True
            return

        for result in results:
            request_id = str(getattr(result, 'request_id', '') or '')
            expected_size = pending.pop(request_id, None)
            if expected_size is None:
                continue
            try:
                data = np.asarray(result.data)
                width, height = expected_size
                if (
                    data.ndim != 3
                    or data.shape[0] != height
                    or data.shape[1] != width
                    or data.shape[2] < 3
                ):
                    raise ValueError(
                        f'unexpected PBO result shape {data.shape!r}, '
                        f'expected ({height}, {width}, 3+)'
                    )
                if self._full_resolution_cancelled(request_id):
                    self._emit_full_resolution_progress(request_id, 100, 'cancelled')
                    self.full_resolution_failed.emit(request_id, 'cancelled')
                else:
                    bgr = np.ascontiguousarray(data[..., :3], dtype=np.float32)
                    self._emit_full_resolution_progress(request_id, 100, 'done')
                    self.full_resolution_ready.emit(request_id, bgr)
            except Exception as exc:
                logger.exception(
                    'Full-resolution PBO result conversion failed (request_id=%s)',
                    request_id,
                )
                self._emit_full_resolution_progress(request_id, 100, 'failed')
                self.full_resolution_failed.emit(request_id, str(exc))
            finally:
                self._finish_full_resolution_request(request_id)

    def _synchronize_full_res_pbo_for_resize(self, width: int, height: int) -> None:
        """Drain PBO work before replacing a differently-sized export FBO.

        ``glReadPixels`` keeps the source attachment live until the DMA has
        completed.  A subsequent batch item may use a different source size,
        which would otherwise release that attachment while its PBO transfer
        is still in flight.  The common same-size path remains fully
        asynchronous; this bounded fence drain only runs at a size boundary.
        """
        pending = self.__dict__.get('_full_res_pending_readbacks')
        if not pending:
            return
        requested_size = (int(width), int(height))
        if all(size == requested_size for size in pending.values()):
            return

        ctx = self.__dict__.get('ctx')
        if ctx is None:
            return
        try:
            # ModernGL finish is issued on the render thread/current context;
            # it makes all prior readback DMA safe before FBO replacement.
            ctx.finish()
        except Exception:
            logger.exception('Failed to synchronize full-resolution PBO resize')

        self._poll_full_resolution_readbacks()
        if not pending:
            return

        # A broken fence must not leave a request hanging forever or allow a
        # live attachment to be released under an in-flight DMA operation.
        logger.error(
            'Full-resolution PBO readback remained pending across resize; '
            'failing %d request(s) and disabling PBO readback',
            len(pending),
        )
        for request_id in list(pending):
            pending.pop(request_id, None)
            self._emit_full_resolution_progress(request_id, 100, 'failed')
            self.full_resolution_failed.emit(request_id, 'readback_resize')
            self._finish_full_resolution_request(request_id)
        pbo = self.__dict__.get('_full_res_pbo_readback')
        if pbo is not None:
            try:
                pbo.cleanup()
            except Exception:
                logger.debug(
                    'Failed to cleanup disabled full-resolution PBO',
                    exc_info=True,
                )
        self._full_res_pbo_readback = None
        self._full_res_pbo_disabled = True

    def _drain_full_resolution_readbacks_for_shutdown(self) -> None:
        """Terminate pending PBO requests before releasing their source FBO."""
        pending = self.__dict__.get('_full_res_pending_readbacks')
        if not pending:
            return

        pbo = self.__dict__.get('_full_res_pbo_readback')
        ctx = self.__dict__.get('ctx')
        if pbo is not None and ctx is not None:
            try:
                # Cleanup runs on the render thread while its context is still
                # current.  Finish first so the source attachment is safe to
                # release, then give the normal poll path a final chance to
                # publish successful results.
                ctx.finish()
            except Exception:
                logger.exception(
                    'Failed to finish full-resolution PBOs during shutdown'
                )
            try:
                self._poll_full_resolution_readbacks()
            except Exception:
                logger.exception(
                    'Failed to poll full-resolution PBOs during shutdown'
                )

        # If the context/fence is already unusable, never leave a UI request
        # waiting for its normal timeout.  Emit an explicit terminal failure.
        for request_id in list(pending):
            pending.pop(request_id, None)
            self._emit_full_resolution_progress(request_id, 100, 'failed')
            self.full_resolution_failed.emit(request_id, 'renderer_stopped')
            self._finish_full_resolution_request(request_id)

    def _do_render_full_resolution(self, params: Dict[str, Any] | None) -> None:
        """Render one full-resolution request and read it back on this thread.

        The caller only submits an immutable CPU source payload.  Texture
        creation, shader execution, synchronization, and readback all happen
        after the render thread has made its ModernGL context current.
        """
        params = params or {}
        request_id = str(params.get('render_request_id') or '')
        if not request_id:
            logger.warning("full-resolution render missing request id")
            return
        lock = self.__dict__.get('_full_res_cancel_lock')
        active = self.__dict__.get('_full_res_active_requests')
        cancelled = self.__dict__.get('_full_res_cancelled_requests')
        if lock is not None and active is not None and cancelled is not None:
            with lock:
                # Direct callers/tests may bypass register_full_resolution;
                # make the worker-side handler safe for those paths too.
                active.add(request_id)
        deferred_readback = False
        self._emit_full_resolution_progress(request_id, 0, 'queued')
        try:
            if self._full_resolution_cancelled(request_id):
                self._emit_full_resolution_progress(request_id, 100, 'cancelled')
                self.full_resolution_failed.emit(request_id, 'cancelled')
                return
            self._emit_full_resolution_progress(request_id, 5, 'prepare')
            width, height = params.get('viewport_size', (0, 0))
            self._synchronize_full_res_pbo_for_resize(int(width), int(height))
            rendered = self._do_render(params, allow_during_playback=True)
            if not rendered:
                reason = (
                    'cancelled'
                    if self._full_resolution_cancelled(request_id)
                    else 'render_failed'
                )
                self._emit_full_resolution_progress(request_id, 100, reason)
                self.full_resolution_failed.emit(request_id, reason)
                return

            output_fbo = getattr(self, '_full_res_color_output_fbo', None)
            if output_fbo is None or width <= 0 or height <= 0:
                self._emit_full_resolution_progress(request_id, 100, 'missing_output')
                self.full_resolution_failed.emit(request_id, 'missing_output')
                return

            if self._full_resolution_cancelled(request_id):
                self._emit_full_resolution_progress(request_id, 100, 'cancelled')
                self.full_resolution_failed.emit(request_id, 'cancelled')
                return
            self._emit_full_resolution_progress(request_id, 85, 'readback')

            # Prefer a fenced pixel-pack transfer.  The command returns to the
            # render loop immediately; _poll_full_resolution_readbacks emits
            # the terminal signal once the DMA is ready.  If PBOs are not
            # available, retain the proven synchronous worker-thread path.
            if self._enqueue_full_resolution_readback(
                request_id, output_fbo, int(width), int(height)
            ):
                deferred_readback = True
                return

            # ModernGL readback is bottom-up.  Convert the pipeline's RGB
            # output to the application's established BGR ndarray contract.
            raw = output_fbo.read(components=4, alignment=1, dtype='f4')
            rgba = np.frombuffer(raw, dtype=np.float32).reshape(
                int(height), int(width), 4
            )
            rgb = np.flipud(rgba[..., :3]).copy()
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            if self._full_resolution_cancelled(request_id):
                self._emit_full_resolution_progress(request_id, 100, 'cancelled')
                self.full_resolution_failed.emit(request_id, 'cancelled')
                return
            self._emit_full_resolution_progress(request_id, 100, 'done')
            self.full_resolution_ready.emit(request_id, bgr)
        except Exception as exc:
            logger.exception("full-resolution render failed (request_id=%s)", request_id)
            self._emit_full_resolution_progress(request_id, 100, 'failed')
            self.full_resolution_failed.emit(request_id, str(exc))
        finally:
            if not deferred_readback:
                self._finish_full_resolution_request(request_id)

    def _do_render_impl(
        self,
        params: Dict[str, Any],
        *,
        allow_during_playback: bool = False,
        render_temp_textures: ExitStack,
    ) -> bool:
        """``_do_render`` 的核心实现：raw → 多层合成 → 发布的完整 pass。

        渲染线程专用（GL mutex 持有中）。流程：
        1. 丢弃守卫：取消、播放占用 front、换剪辑 texture pending、
           ``video_generation`` 过期、陈旧 pyramid 回退。
        2. 依输入类型选源纹理——CPU source override（全分辨率导出）、
           YUV 平面（先转 RGB）、打包视频纹理或图像金字塔——并可叠加
           视频降噪。
        3. raw_view pass 应用 RAW 预览调整，随后逐层 ping-pong 合成
           （matte 生成 → composition shader → aux 纹理绑定）。
        4. 输出阶段按模式发布：ColorOutput 离屏纹理（composite / monitor /
           scope / full_res 目标），或视口双缓冲 front 纹理 + fence/finish
           同步，最后 emit ``frame_ready`` / ``color_output_ready``。

        Args:
            params: 渲染参数（viewport_size / layers / visible_rect /
                render_reason / color_output_mode /
                full_resolution_request_id / video_generation 等）。
            allow_during_playback: 播放激活时是否仍允许发布 front
                （视频帧命令内部传 True，普通 RENDER_FRAME 默认 False）。
            render_temp_textures: ExitStack（由 ``_do_render`` 提供），
                ColorOutput 临时纹理在其中租借，异常路径也能归还池。

        Returns:
            True 表示完成渲染并发布；False 表示被守卫丢弃或输入缺失。
        """
        full_res_request_id = params.get('full_resolution_request_id')
        cancel_adapter = getattr(self, '_render_cancelled', None)
        if callable(cancel_adapter):
            is_cancelled = lambda: bool(cancel_adapter(full_res_request_id))
        else:
            legacy_cancel = getattr(self, '_cancel_requested', None)
            is_cancelled = lambda: bool(legacy_cancel and legacy_cancel())
        if is_cancelled():
            return False
        if not self.ctx or not params:
            return False
        w, h = params.get('viewport_size', (1, 1))
        if w <= 0 or h <= 0:
            return False

        color_output_mode = params.get('color_output_mode', False)
        color_output_target = (
            params.get('color_output_target') if color_output_mode else None
        )
        color_output_tex = None
        color_output_fbo = None
        source_image = params.get('source_image')
        has_source_override = source_image is not None

        # While playback is active, the video lane is the sole owner of the
        # front texture.  A RENDER_FRAME can reuse a held source texture and
        # publish it between two RENDER_VIDEO_FRAME commands.  That makes the
        # viewer alternate between two pictures while a colour control is
        # adjusted (or while a resize submits viewport_refresh).  The next
        # video command already carries the latest cached display parameters,
        # so it is the only safe publisher during playback.  Video commands
        # call this method internally after uploading/binding their next frame;
        # they explicitly retain presentation permission.
        render_reason = params.get('render_reason', 'full')
        if (
            bool(getattr(self, 'video_playback_active', False))
            and not allow_during_playback
        ):
            _flash_diag.event(
                'drop', why='playback_active', reason=render_reason,
                vgen=int(getattr(self, '_video_generation_id', 0) or 0),
            )
            return False

        # A paused Timeline Clip switch has a small interval between advancing
        # the video generation and uploading the requested master frame. The
        # old video texture remains bound during that interval. Hydrating the
        # target grade and fitting the Color viewport can submit RENDER_FRAME
        # commands in the same interval; without this gate they publish that
        # retained texture and the viewer alternates between the two clips.
        # Only the new RENDER_VIDEO_FRAME / RENDER_VIDEO_GPU_FRAME is allowed
        # to present while the source texture is pending.
        if (
            bool(getattr(self, 'video_texture_pending', False))
            and not allow_during_playback
            and (
                self.current_video_texture is not None
                or self.current_video_planes is not None
            )
        ):
            logger.debug(
                "_do_render: dropped RENDER_FRAME while the replacement "
                "video texture is pending"
            )
            _flash_diag.event(
                'drop', why='texture_pending', reason=render_reason,
                vgen=int(getattr(self, '_video_generation_id', 0) or 0),
            )
            return False

        # P2: 重置统计计数器（每帧开始）
        self.reset_render_stats()
        render_request_id = params.get('render_request_id')

        # Drop a stale RENDER_FRAME (grading/viewport_refresh/hydration) that
        # crosses a clip switch.  RENDER_FRAME has no generation gate of its
        # own; render_pipeline stamps the submit-time _video_generation_id into
        # params['video_generation'].  If the generation has since advanced
        # (start_playback / seek / static-preview bumped it) AND this render
        # would read a video texture, the command was queued against the
        # previous clip and would flash its last frame.  Image-only renders
        # (no video texture) are always allowed through.
        cmd_video_gen = params.get('video_generation')
        if cmd_video_gen is not None:
            try:
                cmd_video_gen = int(cmd_video_gen)
            except (TypeError, ValueError):
                cmd_video_gen = None
        if cmd_video_gen is not None:
            current_gen = int(getattr(self, '_video_generation_id', 0) or 0)
            if cmd_video_gen < current_gen:
                # Generation advanced since submit: a new clip epoch began.
                # Only suppress when the source would be a (stale) video
                # texture — a pure image-pyramid render is still correct.
                if self.current_video_texture is not None or self.current_video_planes is not None:
                    logger.debug(
                        "_do_render: dropped stale RENDER_FRAME gen=%s "
                        "current=%s (clip switch, video texture held)",
                        cmd_video_gen, current_gen,
                    )
                    _flash_diag.event(
                        'drop', why='stale_generation', reason=render_reason,
                        cmd_gen=cmd_video_gen, cur_gen=current_gen,
                    )
                    return False

        # Render the full graded source before stabilization. The final warp
        # then samples the requested viewport ROI, so zoom/pan cannot reveal
        # missing offscreen pixels or move grading masks separately.
        stabilization_state = params.get('video_stabilization') or {}
        stabilization_output_rect = None
        stabilization_frame = int(params.get('stabilization_source_frame', 0))
        if stabilization_state:
            from src.shared.tracking.stabilization_state import stabilization_matrix
            if stabilization_matrix(stabilization_state, stabilization_frame) is not None:
                native_w = float(stabilization_state['source_width'])
                native_h = float(stabilization_state['source_height'])
                input_w, input_h = self.video_source_size if (
                    self.current_video_texture is not None or self.current_video_planes is not None
                ) else self.source_image_size
                if has_source_override:
                    input_h, input_w = source_image.shape[:2]
                input_w, input_h = float(input_w or native_w), float(input_h or native_h)
                scene_rect = params.get('scene_visible_rect')
                if scene_rect:
                    stabilization_output_rect = tuple(scene_rect)
                else:
                    vx, vy, vw, vh = params.get('visible_rect') or (0., 0., input_w, input_h)
                    stabilization_output_rect = (vx * native_w / input_w, vy * native_h / input_h,
                                                 vw * native_w / input_w, vh * native_h / input_h)
                params = dict(params)
                full_rect = (0., 0., input_w, input_h)
                params['visible_rect'] = full_rect
                params['grain_visible_rect'] = full_rect

        self.ctx.viewport = (0, 0, w, h)
        layers = params.get('layers', [])

        # GPU 硬件 Mipmap 优化：LOD 始终为 0
        # GPU 会通过 LINEAR_MIPMAP_LINEAR 过滤器自动根据 Screen-space derivatives
        # 选择最佳 Mipmap 层级，Python 端无需手动计算 LOD
        lod = 0

        # P4.2 / S3.A: Check for any available source texture (video planes, video texture, or image)
        has_video_planes = self.current_video_planes is not None
        has_video_texture = self.current_video_texture is not None
        has_image = bool(self.pyramid_textures)

        presentation_size = params.get('presentation_size')
        if presentation_size and len(presentation_size) == 2:
            try:
                presentation_w, presentation_h = (
                    int(presentation_size[0]), int(presentation_size[1])
                )
                if presentation_w > 0 and presentation_h > 0:
                    self.current_video_display_size = (presentation_w, presentation_h)
            except (TypeError, ValueError):
                pass
        else:
            source_w, source_h = getattr(self, 'video_source_size', (0, 0))
            if (has_video_planes or has_video_texture) and source_w > 0 and source_h > 0:
                self.current_video_display_size = (source_w, source_h)

        if (
            not has_image
            and not has_video_texture
            and not has_video_planes
            and not has_source_override
        ):
            return False

        if color_output_mode:
            color_output_tex, color_output_fbo = self._ensure_color_output_fbo(
                w, h, color_output_target
            )
        else:
            self._ensure_fbos(w, h)
        if not self.pipeline:
            from src.features.color.core.shader_compiler import ShaderPipeline
            self.pipeline = ShaderPipeline(self.ctx)

        # P4: 使用持久化的渲染纹理（避免每帧从 pool 查找）
        # 对于 ColorOutput 模式，仍使用临时纹理以确保正确性
        if color_output_mode:
            # ColorOutput 模式：使用临时纹理（原有逻辑）
            output_dtype = 'f4' if color_output_target == 'full_res' else 'f2'
            raw_view_tex = self._acquire_color_output_temp_texture(
                render_temp_textures, w, h, dtype=output_dtype
            )
            raw_view_fbo = self.fb_manager.get_fbo(raw_view_tex)
            self._temp_texture_alloc_count += 1
        else:
            # 视频播放/预览模式：使用持久化纹理
            raw_view_tex, raw_view_fbo, ping_tex, ping_fbo, pong_tex, pong_fbo = \
                self._ensure_persistent_render_textures(w, h)
        
        raw_view_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0) # Image content on transparent background
        
        # P4.2 / S3.A: Select source texture based on input type
        # Video planes: convert YUV/NV12 planes to RGB texture first
        # Video texture: use current_video_texture (no mipmap, fast upload)
        # Image input: use pyramid_textures (mipmap for smooth zoom)
        # P1-1: Track if source_tex is temporary (needs release) or shared (must not release)
        source_tex = None
        source_tex_is_temp = False
        source_tex_is_viewport_aligned = False

        if has_source_override:
            # Full-resolution export/apply passes carry a CPU RGB image in the
            # command payload.  Creating and releasing this texture here keeps
            # every OpenGL operation on the renderer thread while preserving
            # the existing raw/composition pipeline.
            try:
                source_array = np.asarray(source_image)
                if source_array.ndim == 2:
                    source_array = np.repeat(source_array[..., None], 3, axis=2)
                if source_array.ndim != 3 or source_array.shape[2] not in (3, 4):
                    logger.warning("_do_render: invalid full-resolution source shape %s", source_array.shape)
                    return False
                if source_array.shape[2] == 3:
                    alpha = np.ones(source_array.shape[:2] + (1,), dtype=source_array.dtype)
                    source_array = np.concatenate((source_array, alpha), axis=2)
                source_array = np.ascontiguousarray(source_array, dtype=np.float32)
                source_tex = self.ctx.texture(
                    (int(source_array.shape[1]), int(source_array.shape[0])),
                    4,
                    data=source_array.tobytes(),
                    dtype='f4',
                    alignment=4,
                )
                source_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                source_tex.repeat_x = source_tex.repeat_y = False
                source_tex_is_temp = True
                source_tex_is_viewport_aligned = True
            except Exception:
                logger.exception("_do_render: failed to upload full-resolution source")
                if source_tex is not None:
                    try:
                        source_tex.release()
                    except Exception:
                        logger.debug(
                            "failed to release partially-created full-resolution source",
                            exc_info=True,
                        )
                return False
        elif has_video_planes:
            # S3.A: Render YUV planes to a temporary RGB texture
            # P3: YUV texture 从 ring buffer 获取，不需要释放
            source_tex = self._render_yuv_planes_to_rgb_texture(w, h, params)
            if source_tex is None:
                logger.warning("_do_render: Failed to convert YUV planes to RGB")
                return False
            source_tex_is_temp = False  # P3: Ring buffer texture 不需要释放
            source_tex_is_viewport_aligned = True
        elif has_video_texture:
            source_tex = self.current_video_texture
            source_tex_is_viewport_aligned = True  # Packed video texture fills viewport
        elif has_image:
            # Video-mode fallback guard: the pyramid texture is a still frame
            # uploaded at clip open/seek time.  Once the video generation has
            # advanced past that upload (clip switch, playback restart, new
            # seek), the pyramid belongs to the previous clip epoch.  A queued
            # grading/viewport RENDER_FRAME that reaches this branch would
            # otherwise publish that stale still and flash a different clip's
            # picture for one frame while the user adjusts colour parameters.
            _cur_gen = 0
            _pyr_epoch = 0
            _is_video_source = False
            try:
                _cur_gen = int(self.__dict__.get('_video_generation_id', 0) or 0)
                _pyr_epoch = int(
                    self.__dict__.get('_pyramid_epoch_generation', 0) or 0
                )
                _src_size = self.__dict__.get('video_source_size', (0, 0)) or (0, 0)
                _is_video_source = bool(_src_size[0] > 0)
            except RuntimeError:
                # QObject base never initialised (lightweight test stubs):
                # keep the legacy permissive fallback behaviour.
                _cur_gen = 0
                _pyr_epoch = 0
                _is_video_source = False
            if _is_video_source and _cur_gen > _pyr_epoch:
                logger.debug(
                    "_do_render: dropped %s render, pyramid fallback is stale "
                    "(vgen=%s pyramid_epoch=%s)",
                    render_reason, _cur_gen, _pyr_epoch,
                )
                _flash_diag.event(
                    'drop', why='stale_pyramid', reason=render_reason,
                    vgen=_cur_gen, pyramid_epoch=_pyr_epoch,
                )
                return False
            source_tex = self.pyramid_textures[lod]
        else:
            return False  # No source texture available

        # M5: 视频降噪（时域+空域）——视频纹理准备完成后、调色节点执行前。
        # CPU 上传路径与 GPU 外部纹理路径在此汇合（source_tex 已是 RGBA 纹理），
        # 保证两条路径参数语义一致。任何失败回退原始帧，不阻塞渲染。
        if (has_video_planes or has_video_texture) and source_tex is not None:
            denoised = self._apply_video_denoise(source_tex, params)
            if denoised is not None and denoised is not source_tex:
                source_tex = denoised
                source_tex_is_viewport_aligned = True

        # YUV conversion binds its own output FBO. Restore the raw-view FBO before
        # applying preview/grading, otherwise playback draws into the wrong target
        # and the UI-visible texture remains transparent.
        raw_view_fbo.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Use specialized raw_view shader to apply preview adjustments once
        # P1-1: Wrap raw view pass in try/finally to ensure YUV temp texture is released on exception
        try:
            prog_raw = self.utility_shaders['raw_view']
            source_tex.use(0)
            prog_raw['u_texture'].value = 0

            rp = params.get('raw_preview', self.raw_preview_params) or {'exposure': 0.0, 'gains': (1.0, 1.0, 1.0)}
            prog_raw['u_raw_prev_exposure'].value = float(rp.get('exposure', 0.0))
            prog_raw['u_raw_prev_gains'].value = tuple(rp.get('gains', (1.0, 1.0, 1.0)))
            prog_raw['u_raw_lift'].value = float(rp.get('lift', 0.0))
            prog_raw['u_raw_gain'].value = float(rp.get('gain', 1.0))
            prog_raw['u_raw_contrast'].value = float(rp.get('contrast', 1.0))
            prog_raw['u_raw_saturation'].value = float(rp.get('saturation', 1.0))
            prog_raw['u_raw_shadows'].value = float(rp.get('shadows', 0.0))
            prog_raw['u_raw_highlights'].value = float(rp.get('highlights', 0.0))
            prog_raw['u_raw_color_boost'].value = float(rp.get('color_boost', 0.0))
            prog_raw['u_raw_mid_detail'].value = float(rp.get('mid_detail', 0.0))
            prog_raw['u_raw_sharpness'].value = float(rp.get('sharpness', 0.0))
            prog_raw['u_raw_color_space'].value = int(rp.get('color_space', 0))
            prog_raw['u_raw_gamma'].value = int(rp.get('gamma', 0))
            prog_raw['u_is_srgb_source'].value = bool(rp.get('is_srgb_source', False))
            # P3-1: GPU端 BGR->RGB swizzle（视频帧是 BGR/BGRA 时设置）
            # Full-resolution source overrides are already RGB.  Do not carry
            # a stale video BGR/BGRA swizzle flag across a clip/export switch.
            prog_raw['u_bgr_swizzle'].value = (
                False if has_source_override
                else bool(getattr(self, '_video_needs_swizzle', False))
            )

            # Bind Global Raw LUT if enabled
            lut_tex = self.raw_lut_tex if self.raw_lut_tex else self.identity_lut
            if lut_tex:
                lut_tex.use(1)
                prog_raw['u_lut_tex'].value = 1
                prog_raw['u_lut_enabled'].value = bool(self.raw_lut_tex)
            else:
                prog_raw['u_lut_enabled'].value = False

            if source_tex_is_viewport_aligned:
                self._render_video_quad(prog_raw)
            else:
                self._render_tiles_to_current_fbo(source_tex, prog_raw, params, lod)
        finally:
            # YUV source textures come from the ring buffer and remain owned by
            # the renderer.  Full-resolution source overrides are per-request
            # textures and must be released after the raw pass, including on
            # shader/driver exceptions.
            if source_tex_is_temp and source_tex is not None:
                try:
                    source_tex.release()
                except Exception:
                    logger.debug("failed to release full-resolution source texture", exc_info=True)
            source_tex = None  # Prevent accidental reuse

        # 2. Setup Comp Ping-Pong
        # P4: 对于 ColorOutput 模式，使用临时纹理；否则使用持久化纹理
        if color_output_mode:
            # ColorOutput 模式：使用临时纹理（原有逻辑）
            output_dtype = 'f4' if color_output_target == 'full_res' else 'f2'
            ping_tex = self._acquire_color_output_temp_texture(
                render_temp_textures, w, h, dtype=output_dtype
            )
            pong_tex = self._acquire_color_output_temp_texture(
                render_temp_textures, w, h, dtype=output_dtype
            )
            ping_fbo = self.fb_manager.get_fbo(ping_tex)
            pong_fbo = self.fb_manager.get_fbo(pong_tex)
            self._temp_texture_alloc_count += 2
        else:
            # 视频播放/预览模式：使用已获取的持久化纹理
            # (ping_tex, pong_tex, ping_fbo, pong_fbo 已在前面通过 _ensure_persistent_render_textures 获取)
            pass

        # Initial source for the first layer is the raw view
        current_source_tex = raw_view_tex
        current_target_fbo = ping_fbo
        current_target_tex = ping_tex

        grain_params = params.get('film_grain', {})
        visible_rect = params.get('visible_rect', (0,0,w,h))
        grain_visible_rect = params.get('grain_visible_rect', visible_rect)  # Film Grain 专用
        show_mask = params.get('show_mask', False)
        active_idx = params.get('active_layer_index', 0)
        raw_preview = params.get('raw_preview', self.raw_preview_params)
        active_matte = None

        total_layers = max(1, len(layers))
        for i, layer in enumerate(layers):
            if is_cancelled():
                logger.info("Render request cancelled before layer %s", i)
                return False
            is_active = (i == active_idx)
            is_visible = layer.get('visible', True)
            
            # Skip hidden layers unless we need to preview their mask
            if not is_visible and not (is_active and show_mask):
                continue

            if full_res_request_id:
                self._emit_full_resolution_progress(
                    str(full_res_request_id),
                    10 + int(70 * (i + 1) / total_layers),
                    'render',
                )

            # CRITICAL FIX: Use raw_view_tex so qualifier operates on the gamma-corrected, color-managed image.
            # This ensures the HSL math perfectly matches the colors the user actually sees and picks on screen.
            source_tex = raw_view_tex

            # AI Plugin: Check if this layer has a processed AI plugin texture
            # If so, use it as the source texture instead of the raw view
            l_id = layer.get('id')
            ai_tex_used = None
            layer_ai_plugins = [p for p in layer.get('plugins', [])
                                if p.get('type') == 'ai' and p.get('enabled', True)]
            if layer_ai_plugins and l_id:
                # Use the last active AI plugin's result texture
                last_ai = layer_ai_plugins[-1]
                ai_key = (l_id, last_ai.get('instance_id'))
                ai_tex = self.ai_plugin_textures.get(ai_key)
                if ai_tex:
                    source_tex = ai_tex
                    ai_tex_used = ai_tex
            matte_tex = self._generate_matte(layer, source_tex, w, h, visible_rect)
            
            if is_active:
                active_matte = matte_tex
            
            # If layer is hidden, don't perform composition
            if not is_visible:
                continue

            # A layer bake is a complete, composited image at the point it was
            # frozen (rather than only a LUT/effect delta).  It must therefore
            # replace the current stack input, not be composited a second time
            # as a grade.  The prior implementation also lost ``is_baked``
            # before this point, making saved/offline bakes render as black.
            baked_tex = self._get_baked_layer_texture(layer)
            if baked_tex is not None:
                current_target_fbo.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                prog_pass = self.utility_shaders['passthrough']
                baked_tex.use(0)
                prog_pass['u_texture'].value = 0
                self._render_tiles_to_current_fbo(
                    baked_tex,
                    prog_pass,
                    params,
                    lod,
                    coordinate_size=self._baked_layer_coordinate_size(
                        baked_tex.size, lod
                    ),
                )
                current_source_tex = current_target_tex
                if current_target_tex == pong_tex:
                    current_target_tex = ping_tex
                    current_target_fbo = ping_fbo
                else:
                    current_target_tex = pong_tex
                    current_target_fbo = pong_fbo
                continue

            has_curves = self.pipeline.has_active_curves_lut(layer.get('curves_lut'))
            has_warper = layer.get('warper_params') is not None
            has_lut3d = layer.get('active_lut_path') is not None
            scp = layer.get('soft_clip_params') or {}
            has_soft_clip = scp.get('enabled', False)
            # 曲线面板四参数柔化裁切：非恒等才编译进管线（默认恒等直通）
            has_curves_soft_clip = not curves_soft_clip_is_identity(
                layer.get('curves_soft_clip'))
            has_hsl_curve = self.pipeline.has_active_hsl_curve_lut(layer.get('hsl_curve_lut'))
            has_mixer = self.pipeline.has_active_mixer_params(layer.get('mixer_params'))

            # Get plugins for this layer（P7-01：解析为权威 PluginBinding）
            from src.features.color.core.shader_compiler import (
                plugin_bindings_from_instances,
            )
            plugin_bindings = plugin_bindings_from_instances(
                layer.get('plugins', []))

            prog_comp = self.pipeline.build_composition_shader(
                has_curves=has_curves, has_warper=has_warper,
                has_lut3d=has_lut3d, has_soft_clip=has_soft_clip,
                grain_enabled=False, has_hsl_curve=has_hsl_curve,
                has_mixer=has_mixer, has_curves_soft_clip=has_curves_soft_clip,
                plugins=plugin_bindings,
                request_id=render_request_id,
                layer_id=l_id,
            )
            if not prog_comp:
                # P0-COL-01/P1-OBS-01：编译失败不允许静默丢层。编译器内部
                # 已 record_compile_failure（含插件标识）；这里把「该层在本
                # pass 被跳过」本身落到日志，让“导出成功但少了某层效果”
                # 可以从日志/诊断定位到具体层。
                logger.warning(
                    "Composition shader build failed; layer skipped in this "
                    "pass (layer_id=%s, plugins=%s) — see render diagnostics",
                    l_id,
                    [p.get('plugin_id') or p.get('type')
                     for p in layer.get('plugins', [])],
                )
                continue

            current_target_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0) # Ensure target is clean for alpha blending if any
            # P0-COL-03：AI 结果纹理是该层的新 source，合成 pass 必须消费
            # 它（matte 已在上面消费同一纹理）。此前 ai_tex 只进 matte，
            # 这里仍绑定栈前缀结果，AI 像素从不进入最终画面。
            pass_input_tex = (ai_tex_used if ai_tex_used is not None
                              else current_source_tex)
            pass_input_tex.use(0)
            matte_tex.use(1)
            prog_comp['u_input_tex'].value = 0
            prog_comp['u_matte_tex'].value = 1
            
            self._update_composition_uniforms(prog_comp, layer, grain_params, w, h, raw_preview, grain_visible_rect, bindings=plugin_bindings)
            self._bind_aux_textures_single_layer(prog_comp, layer)
            self._render_quad(prog_comp)
            
            # NOTE: We do NOT release matte_tex here because it's now owned by the cache
            # if matte_tex != self.white_tex:
            #    self.fb_manager.release_temp_texture(matte_tex)

            current_source_tex = current_target_tex
            if current_target_tex == pong_tex:
                current_target_tex = ping_tex
                current_target_fbo = ping_fbo
            else:
                current_target_tex = pong_tex
                current_target_fbo = pong_fbo

        # ── 全局胶片颗粒（独立 pass，与 legacy 引擎的 Step 3 对齐）──
        # 此前颗粒折叠在"原始列表最后一层"的合成 pass 里，但隐藏层、
        # baked 层、编译失败层都会 continue 跳过合成 pass——最后一层
        # 不可见/已 bake 时，开启的颗粒会整帧静默消失。
        if grain_params.get('enabled', False):
            prog_grain = self.pipeline.build_composition_shader(
                grain_enabled=True,
                request_id=render_request_id,
            )
            if prog_grain:
                from src.features.color.core.composition_uniforms import (
                    composition_uniform_values,
                    upload_composition_uniforms,
                )
                src_w, src_h = self.source_image_size
                values = composition_uniform_values(
                    {},
                    grain_params=grain_params,
                    width=w, height=h,
                    tex_size=(float(src_w), float(src_h))
                    if src_w > 0 and src_h > 0 else None,
                    visible_rect=grain_visible_rect,
                )
                current_target_fbo.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                current_source_tex.use(0)
                prog_grain['u_input_tex'].value = 0
                if self.white_tex:
                    self.white_tex.use(1)
                    prog_grain['u_matte_tex'].value = 1
                upload_composition_uniforms(prog_grain, values)
                # u_opacity=0：mix 丢弃"层调色"结果，只保留输入 + 全局颗粒
                prog_grain['u_opacity'].value = 0.0
                prog_grain['u_blend_mode'].value = 0
                self._render_quad(prog_grain)
                current_source_tex = current_target_tex
                if current_target_tex == pong_tex:
                    current_target_tex = ping_tex
                    current_target_fbo = ping_fbo
                else:
                    current_target_tex = pong_tex
                    current_target_fbo = pong_fbo

        if stabilization_output_rect is not None:
            from src.features.shared.effects.effect_pass_runner import EffectPassRunner
            from src.features.shared.effects.video_stabilization import apply_stabilization_gpu
            runner = self.__dict__.get('_stabilization_runner')
            if runner is None:
                runner = EffectPassRunner(self.ctx)
                self._stabilization_runner = runner
            apply_stabilization_gpu(runner, current_source_tex.glo, stabilization_state,
                                    stabilization_frame, w, h,
                                    dtype='f4' if color_output_target == 'full_res' else 'f2',
                                    output_rect=stabilization_output_rect)
            current_source_tex = runner.output_texture()
            if current_source_tex is None:
                raise RuntimeError('Stabilization produced no render surface')

        # ── Output stage ──
        if color_output_mode:
            # Scope uses a private target so its quality-tier resize cannot
            # invalidate an externally published Composite/monitor texture.
            color_output_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            prog_pass = self.utility_shaders['passthrough']
            current_source_tex.use(0)
            prog_pass['u_texture'].value = 0
            self._render_quad(prog_pass)
            # Composite/monitor consumers read this texture outside the current
            # render pass and therefore require immediate completion.  The
            # scope target is consumed next in the same GL context; command
            # ordering is sufficient, and _do_render_gpu_scope performs the
            # one cross-context finish after its display textures are complete.
            if self._color_output_requires_immediate_finish(color_output_target):
                self.ctx.finish()
                self._finish_call_count += 1

            if color_output_target == 'monitor':
                self.monitor_color_output_ready.emit(color_output_tex.glo, w, h)
            elif color_output_target not in ('scope', 'full_res'):
                self.color_output_ready.emit(color_output_tex.glo, w, h)
        else:
            # Output to screen FBO (normal viewport rendering)
            fbo_screen = self.fbo_pair[self.write_idx]
            fbo_screen.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.1, 0.1, 0.1, 1.0) # Viewport background remains stable

            self.ctx.enable(moderngl.BLEND)
            self.ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

            if show_mask and active_matte:
                prog_preview = self.utility_shaders['mask_preview']
                current_source_tex.use(0)
                active_matte.use(1)
                prog_preview['u_tex'].value = 0
                prog_preview['u_mask'].value = 1
                prog_preview['u_mode'].value = MASK_PREVIEW_MODES.get(
                    params.get('mask_preview_style', 'highlight'), 0)
                self._render_quad(prog_preview)
            else:
                prog_pass = self.utility_shaders['passthrough']
                current_source_tex.use(0)
                prog_pass['u_texture'].value = 0
                self._render_quad(prog_pass)

            self.ctx.disable(moderngl.BLEND)

            # P4: 持久化纹理不释放（下一帧可复用）
            # 只有持久化纹理才不释放，临时纹理仍需释放
            # 当前非 ColorOutput 模式使用持久化纹理，无需释放

            front_idx = self.write_idx
            self.write_idx = 1 - self.write_idx
            # Static image preview should publish only completed frames, matching
            # the older Color branch behavior. Video playback keeps the non-blocking
            # sync mode for throughput.
            is_paused_video_preview = (
                has_image
                and not has_video_texture
                and not has_video_planes
                and self.video_source_size[0] > 0
                and self.video_source_size[1] > 0
                and render_reason == 'grading'
            )
            is_exact_static_video_preview = (
                (has_video_texture or has_video_planes)
                and render_reason == 'static_preview'
            )
            force_finish = bool(getattr(self, "_force_finish_next_publish", False))
            front_render_complete = False
            if (
                (
                    has_image
                    and not has_video_texture
                    and not has_video_planes
                    and not is_paused_video_preview
                )
                or render_reason == 'viewport_refresh'
                or is_exact_static_video_preview
                or force_finish
            ):
                self.ctx.finish()
                self._finish_call_count += 1
                front_render_complete = True
                if force_finish:
                    # Consumed: only the first playback frame after a clip
                    # switch needs the finish; afterwards the double-buffer
                    # pipeline is warm again.
                    self._force_finish_next_publish = False
            else:
                # Video playback and paused-video grading use a double-buffered
                # FBO and publish without glFinish().  Waiting here turns every
                # color-wheel sample into a GPU stall and makes the control lag
                # behind the pointer.
                front_render_complete = self._sync_gpu()

            self.current_front_tex_id = self.tex_pair[front_idx].glo
            self.current_front_size = (w, h)
            self.debug_last_front_tex_id = self.current_front_tex_id
            self.debug_last_render_reason = render_reason
            self._update_front_present_sync(
                render_complete=front_render_complete,
            )
            # Flash diagnostics: content fingerprint + held source frame.
            # A cross-clip flash publishes the wrong pixels, so metadata alone
            # (reason/src/tex id) cannot prove what the viewer actually gets.
            _fp = None
            try:
                if _flash_diag.enabled():
                    _fbo_fp = self.fbo_pair[front_idx]
                    _fw = max(1, min(16, w - 16 if w > 32 else 0))
                    _fh = max(1, min(16, h - 16 if h > 32 else 0))
                    if _fw >= 1 and _fh >= 1:
                        _raw = _fbo_fp.read(
                            components=3, dtype='f1',
                            viewport=(w // 2, h // 2, _fw, _fh),
                        )
                        _arr = np.frombuffer(_raw, dtype=np.uint8)
                        _fp = [
                            int(round(float(v)))
                            for v in _arr.astype(np.float64).reshape(-1, 3).mean(axis=0)
                        ]
            except Exception:
                _fp = None
            _flash_diag.event(
                'publish', reason=render_reason,
                front_tex=self.current_front_tex_id,
                src=('planes' if has_video_planes
                     else ('vtex:%s' % getattr(self.current_video_texture, 'glo', None)
                           if has_video_texture else 'pyramid')),
                playback=bool(getattr(self, 'video_playback_active', False)),
                allow_playback=bool(allow_during_playback),
                finish=bool(front_render_complete),
                fence=bool(getattr(self, 'current_front_fence', None)),
                vgen=int(getattr(self, '_video_generation_id', 0) or 0),
                vfi=int(getattr(self, 'current_video_frame_index', -1) or -1),
                pending=bool(self.__dict__.get('video_texture_pending', False)),
                pyr_epoch=int(self.__dict__.get('_pyramid_epoch_generation', 0) or 0),
                hold=bool(getattr(self.engine, '_hold_viewport_frame', False)) if getattr(self, 'engine', None) else False,
                fp=_fp,
            )
            # Store the scene-visible rect (original image coords) for UV adjustment during zoom
            self.rendered_visible_rect = params.get('scene_visible_rect', stabilization_output_rect or params.get('visible_rect'))
            # Cache only parameters that reached a successful front publish.
            # A dropped playback/clip-generation render must never replace the
            # scope recipe with stale layers or grading state.
            # AUD-0204: deep-freeze the payload — the scope lane reuses this
            # snapshot on the render thread much later, so it must not alias
            # UI-owned mutable layer DTOs.
            self._last_scope_render_params = self._snapshot_scope_params(params)
            # GPU scope source revision: every successfully published front
            # texture advances the revision so stale scope frames can be
            # rejected after seek / clip switch.  See scope_types.ScopeGpuFrame.
            self._scope_source_revision = int(getattr(self, '_scope_source_revision', 0)) + 1
            # A viewport refresh changes only the crop/scale, not the image
            # content.  Scope's full-frame ColorOutput cache must therefore
            # survive viewport pan/zoom and invalidate only for a new frame,
            # grade, seek, or other content render.
            if render_reason != 'viewport_refresh':
                self._scope_content_revision = int(
                    getattr(self, '_scope_content_revision', 0)
                ) + 1
                self._scope_color_output_revision = -1
            self.frame_ready.emit(self.current_front_tex_id)

        return True

    def _render_yuv_planes_to_rgb_texture(
        self,
        viewport_w: int,
        viewport_h: int,
        params: Dict[str, Any]
    ) -> Optional[moderngl.Texture]:
        """
        S3.A: Render YUV/NV12/P010 planes to a temporary RGB texture.

        P3: 使用 ring buffer 缓存输出纹理，减少每帧纹理创建。

        Must be called from render thread only.

        Args:
            viewport_w: Viewport width
            viewport_h: Viewport height
            params: Render parameters (for image raw_preview, not video YUV metadata)

        Returns:
            RGBA texture with converted RGB content (从 ring buffer 获取，复用)
        """
        if not self.ctx or self.current_video_planes is None:
            return None

        planes = self.current_video_planes
        pixel_format = self.current_video_pixel_format or 'yuv420p'
        video_w, video_h = self.video_source_size

        # P0-3: Use YUV metadata from video frame state (not from raw_preview)
        # raw_preview's color_space/gamma are for image raw processing, not video YUV
        yuv_matrix = self.current_video_yuv_matrix
        color_range = self.current_video_color_range

        # P3: 从 ring buffer 获取输出纹理（复用而非每帧创建）
        output_tex = self._get_yuv_rgb_output_texture(viewport_w, viewport_h)

        # P1-5: Reuse cached FBO for YUV conversion instead of creating one per frame.
        # Only rebuild when the underlying texture changes (different glo).
        if self._yuv_conv_fbo is not None and self._yuv_conv_fbo_tex_glo == output_tex.glo:
            output_fbo = self._yuv_conv_fbo
        else:
            if self._yuv_conv_fbo is not None:
                try:
                    self._yuv_conv_fbo.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            output_fbo = self.ctx.framebuffer(color_attachments=[output_tex])
            self._yuv_conv_fbo = output_fbo
            self._yuv_conv_fbo_tex_glo = output_tex.glo

        output_fbo.use()
        self.ctx.viewport = (0, 0, viewport_w, viewport_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        # Phase 1: 渲染计时
        _render_start = time.perf_counter()

        # Determine format type and render
        try:
            if 'nv12' in pixel_format or 'p010' in pixel_format:
                # NV12 / P010: interleaved UV (planes = (y_tex, uv_tex))
                y_tex, uv_tex = planes
                self._render_nv12_to_rgb(
                    y_tex, uv_tex, output_fbo,
                    video_w, video_h, viewport_w, viewport_h,
                    yuv_matrix, color_range, params.get('visible_rect')
                )
            else:
                # YUV420P / YUV422P / YUV444P: separate planes
                # (y_tex, u_tex, v_tex) 或 YUVA 四元组 (…, a_tex)
                y_tex, u_tex, v_tex = planes[0], planes[1], planes[2]
                a_tex = planes[3] if len(planes) >= 4 else None
                self._render_yuv420_to_rgb(
                    y_tex, u_tex, v_tex, output_fbo,
                    video_w, video_h, viewport_w, viewport_h,
                    yuv_matrix, color_range, pixel_format, params.get('visible_rect'),
                    a_tex=a_tex
                )

            # P1-1: Removed ctx.finish() here - synchronization is handled at final output stage
            # P3: 注意：output_tex 是从 ring buffer 获取的，不需要释放
            # 下一帧会自动轮换使用，异常情况下纹理仍在缓存中

            # Phase 1: 记录渲染耗时
            _render_ms = (time.perf_counter() - _render_start) * 1000.0
            try:
                from src.shared.media.performance_monitor import get_performance_monitor
                perf = get_performance_monitor()
                perf.track_yuv_render_time(_render_ms)
                perf.track_frame_path(self._debug_frame_path)
                # _debug_frame_path 是自由格式诊断串（'unknown'、
                # '*_gpu_failed' 等）——遥测必须用安全解析，未知值按
                # 非零拷贝统计，不得因解析异常跳过 track_*。
                frame_path = RenderFramePath.try_from_serialized(
                    self._debug_frame_path
                )
                is_external_gpu_frame = (
                    frame_path.is_zero_copy if frame_path is not None else False
                )
                perf.track_zero_copy_frame(
                    decode_path=getattr(self, '_debug_decode_path', 'unknown'),
                    render_frame_path=self._debug_frame_path,
                    is_zero_copy=is_external_gpu_frame,
                    cpu_upload=not is_external_gpu_frame,
                    texture_write=not is_external_gpu_frame,
                    external_texture=is_external_gpu_frame,
                )
            except Exception:
                logger.debug('external GPU frame attach failed', exc_info=True)

            return output_tex

        except Exception as e:
            logger.error(f"_render_yuv_planes_to_rgb_texture: {e}")
            # P3: 不释放 ring buffer 中的纹理，它会被下一帧复用
            return None

    def _render_yuv420_to_rgb(
        self,
        y_tex: moderngl.Texture,
        u_tex: moderngl.Texture,
        v_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        video_w: int,
        video_h: int,
        viewport_w: int,
        viewport_h: int,
        yuv_matrix: int,
        color_range: int,
        pixel_format: str,
        visible_rect: Optional[tuple] = None,
        a_tex: Optional[moderngl.Texture] = None
    ) -> None:
        """Render YUV420/422/444 planes to RGB FBO.

        注意：对于独立 Y/U/V 纹理，UV 纹理坐标已归一化到 [0, 1]，
        无需额外的 subsampling 缩放。直接使用 v_texcoord 即可。

        a_tex: 可选 Alpha 纹理（YUVA 第四平面，全分辨率，与 Y 同 UV）。
        """
        source_uv_scale, source_uv_offset = self._video_source_uv_transform(
            visible_rect, video_w, video_h
        )

        alpha_bit_depth = int(getattr(self, 'current_video_alpha_bit_depth', 10))
        alpha_premult = bool(getattr(self, 'current_video_alpha_premultiplied', False))
        from src.shared.video.yuv_gpu_upload import topos_chroma_sampling_params
        chroma_uv_scale, chroma_uv_offset = topos_chroma_sampling_params(
            pixel_format,
            video_w,
            video_h,
            int(getattr(self, 'current_video_chroma_siting', 0)),
        )
        gbr_mode = 'gbr' in str(pixel_format).lower()

        upload_mode = self._yuv_gpu_uploader.get_last_upload_timing().get('mode', 'float16')
        if upload_mode == 'r16ui_direct':
            self._yuv_gpu_uploader.set_yuv_matrix(yuv_matrix)
            self._yuv_gpu_uploader.set_color_range(color_range)
            self._yuv_gpu_uploader.render_yuv_r16ui_to_rgb(
                y_tex,
                u_tex,
                v_tex,
                output_fbo,
                chroma_uv_scale=chroma_uv_scale,
                chroma_uv_offset=chroma_uv_offset,
                bit_depth=int(getattr(self, 'current_video_bit_depth', 8)),
                source_uv_scale=source_uv_scale,
                source_uv_offset=source_uv_offset,
                a_tex=a_tex,
                alpha_bit_depth=alpha_bit_depth,
                alpha_premultiplied=alpha_premult,
                gbr_mode=gbr_mode,
            )
            return

        # Get YUV shader from uploader
        prog = self._yuv_gpu_uploader.get_yuv_shader()

        # Bind textures（unit3 无 Alpha 时绑 Y 作占位）
        y_tex.use(0)
        u_tex.use(1)
        v_tex.use(2)
        (a_tex if a_tex is not None else y_tex).use(3)

        # Set uniforms
        prog['y_plane'].value = 0
        prog['u_plane'].value = 1
        prog['v_plane'].value = 2
        prog['yuv_matrix'].value = yuv_matrix
        prog['color_range'].value = color_range
        prog['bit_depth'].value = int(getattr(self, 'current_video_bit_depth', 8))
        prog['has_alpha'].value = 1 if a_tex is not None else 0
        prog['alpha_premultiplied'].value = 1 if alpha_premult else 0
        prog['source_uv_scale'].value = source_uv_scale
        prog['source_uv_offset'].value = source_uv_offset

        prog['gbr_mode'].value = 1 if gbr_mode else 0
        prog['chroma_uv_scale'].value = chroma_uv_scale
        prog['chroma_uv_offset'].value = chroma_uv_offset

        # The target FBO already has the Color timeline presentation aspect.
        # Do not add a source-aspect matte inside it: Canvas owns the final
        # FBO-to-widget letterbox.
        self._render_quad(prog)

    def _render_nv12_to_rgb(
        self,
        y_tex: moderngl.Texture,
        uv_tex: moderngl.Texture,
        output_fbo: moderngl.Framebuffer,
        video_w: int,
        video_h: int,
        viewport_w: int,
        viewport_h: int,
        yuv_matrix: int,
        color_range: int,
        visible_rect: Optional[tuple] = None
    ) -> None:
        """Render NV12/P010 planes to RGB FBO.

        注意：对于独立 NV12 UV 纹理，纹理坐标已归一化到 [0, 1]，
        无需额外的 subsampling 缩放。直接使用 v_texcoord 即可。
        """
        # Get NV12 shader from uploader
        prog = self._yuv_gpu_uploader.get_nv12_shader()

        # Bind textures
        y_tex.use(0)
        uv_tex.use(1)

        # Set uniforms
        prog['y_plane'].value = 0
        prog['uv_plane'].value = 1
        prog['yuv_matrix'].value = yuv_matrix
        prog['color_range'].value = color_range
        prog['bit_depth'].value = int(getattr(self, 'current_video_bit_depth', 8))
        source_uv_scale, source_uv_offset = self._video_source_uv_transform(
            visible_rect, video_w, video_h
        )
        prog['source_uv_scale'].value = source_uv_scale
        prog['source_uv_offset'].value = source_uv_offset

        # 独立纹理的 UV 坐标已归一化，使用默认参数
        prog['chroma_uv_scale'].value = (1.0, 1.0)
        prog['chroma_uv_offset'].value = (0.0, 0.0)

        # The target FBO already has the Color timeline presentation aspect.
        # Do not add a second source-aspect letterbox inside it.
        self._render_quad(prog)

    def _video_source_uv_transform(
        self,
        visible_rect: Optional[tuple],
        video_w: int,
        video_h: int
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Map viewport UV to the visible rectangle in the source video."""
        if video_w <= 0 or video_h <= 0:
            return (1.0, 1.0), (0.0, 0.0)

        if not visible_rect or len(visible_rect) != 4:
            return (1.0, 1.0), (0.0, 0.0)

        vx, vy, vw, vh = (float(v) for v in visible_rect)
        if vw <= 0.0 or vh <= 0.0:
            return (1.0, 1.0), (0.0, 0.0)

        sx = vw / float(video_w)
        sy = vh / float(video_h)
        ox = vx / float(video_w)
        oy = vy / float(video_h)
        return (sx, sy), (ox, oy)

    def _get_source_matte_resolution(self, layer):
        """Determine the source-resolution dimensions for matte generation.
        Returns (src_w, src_h) at which the matte should be generated.
        For images, this is the full image size. For video, this is the video source size.
        P2-1: Matte is generated at source resolution so the cache is viewport-independent.
        """
        if self.pyramid_textures:
            return self.pyramid_textures[0].size
        if self.video_source_size[0] > 0:
            # For video we may not have a source-resolution texture for the keyer.
            # In that case the keyer will fall back to input_tex (viewport-res),
            # but we still cache at the declared video source size for consistency.
            return self.video_source_size
        # Fallback: no source info available
        return (0, 0)

    def _get_source_tex_for_qualifier(self, layer):
        """Get the best source texture for the keyer/qualifier pass at source resolution.
        P2-1: Uses pyramid_textures[0] or AI plugin texture — not raw_view_tex (viewport-res).
        Returns None when no source-resolution texture is available (e.g. YUV video planes).
        """
        l_id = layer.get('id')
        # Check for AI plugin texture first (same priority as _do_render)
        layer_ai_plugins = [p for p in layer.get('plugins', [])
                            if p.get('type') == 'ai' and p.get('enabled', True)]
        if layer_ai_plugins and l_id:
            last_ai = layer_ai_plugins[-1]
            ai_key = (l_id, last_ai.get('instance_id'))
            ai_tex = self.ai_plugin_textures.get(ai_key)
            if ai_tex:
                return ai_tex
        # Fall back to pyramid (image) or video source texture
        if self.pyramid_textures:
            return self.pyramid_textures[0]
        if self.current_video_texture:
            return self.current_video_texture
        # YUV video planes: no combined source-resolution RGB texture available
        return None

    def _generate_matte(self, layer, input_tex, w, h, visible_rect):
        """Generate matte for a layer, decoupled from viewport position/zoom.

        P2-1: When a source-resolution texture is available (image input or
        direct video frame), the matte is generated at source-image resolution
        and cached independently of visible_rect.  On cache hit we only resample
        the cached source-resolution matte into the current viewport using
        visible_rect.

        When no source-resolution texture is available (e.g. YUV video planes),
        the original viewport-resolution path is used (visible_rect stays in
        cache key since mask_combine needs it at generation time).
        """
        if not layer: return self.white_tex
        l_id = layer.get('id')
        qp = layer.get('qualifier_params') or {}
        ms = layer.get('mask_shapes') or []

        # --- determine if source-resolution path is possible ---------------------
        src_w, src_h = self._get_source_matte_resolution(layer)
        has_source_tex = self._get_source_tex_for_qualifier(layer) is not None
        
        # CRITICAL: When qualifier is enabled, use viewport-resolution path.
        # The source-resolution path uses pyramid_textures[0] (raw image without RAW development),
        # but canvas color picker uses RAW-developed colors. This mismatch causes incorrect matte.
        # Viewport-resolution path uses input_tex (raw_view_tex with RAW development), matching canvas.
        has_qualifier = qp.get('enabled', False)
        
        if not has_source_tex or src_w <= 0 or src_h <= 0 or has_qualifier:
            # Fallback: viewport-resolution path (YUV video planes, or qualifier enabled)
            # Use the original logic with visible_rect in cache key.
            return self._generate_viewport_matte(
                layer, input_tex, w, h, visible_rect,
                frame_epoch=getattr(self, '_video_matte_epoch', 0))

        # --- early return: no qualifier + no mask = full white matte -------------
        # Avoids unnecessary GPU texture alloc, FBO creation, clear, and resample
        # for the common case of a plain color grade with no masking.
        if not qp.get('enabled') and not ms:
            return self.white_tex

        # --- source-resolution path (P2-1: no visible_rect in cache key) --------
        params_hash = matte_cache_hash(
            qp, ms, (src_w, src_h), None,
            frame_epoch=0,
            mask_epoch=self._mask_epoch.get(l_id, 0))

        cached = self.matte_cache.get(l_id)
        src_matte_tex = None

        if cached and cached['params_hash'] == params_hash:
            src_matte_tex = cached['matte']
        else:
            # --- regenerate source-resolution matte ------------------------------
            # AUD-0203: 先生成新 matte、成功后才归还旧条目。若生成抛异常，
            # 缓存仍持有自己独占的旧纹理（自洽可命中），而不是一张已归还
            # 池、可能被再分配为其他 pass 目标的纹理。
            src_matte_tex = self._generate_source_matte(
                layer, input_tex, src_w, src_h
            )
            if cached:
                self.fb_manager.release_temp_texture(cached['matte'])
            self.matte_cache[l_id] = {'matte': src_matte_tex, 'params_hash': params_hash}

        # --- resample source matte to viewport resolution ------------------------
        # Release the previous frame's viewport-res texture for this layer (if any)
        prev_vp_tex = self._matte_viewport_textures.pop(l_id, None)
        if prev_vp_tex is not None:
            self.fb_manager.release_temp_texture(prev_vp_tex)

        vp_matte = self._sample_matte_to_viewport(src_matte_tex, src_w, src_h, w, h, visible_rect)

        # Track the viewport-res texture so we can release it next frame.
        # Only track it if it's a different texture from the cached source texture.
        if vp_matte is not src_matte_tex:
            self._matte_viewport_textures[l_id] = vp_matte

        return vp_matte

    def _generate_source_matte(self, layer, input_tex, src_w, src_h):
        """Generate the matte at source-image resolution (viewport-independent).

        Steps:
        1. Keyer pass on source texture (qualifier).
        2. Combine with mask shapes (both in source-space, no UV remap).
        3. Post-processing (morphology / blur).
        Returns a single-channel f2 texture at (src_w, src_h).
        """
        l_id = layer.get('id')
        qp = layer.get('qualifier_params') or {}
        ms = layer.get('mask_shapes') or []

        matte_tex_A = self.fb_manager.get_temp_texture(src_w, src_h, components=1, dtype='f2')
        fbo_A = self.fb_manager.get_fbo(matte_tex_A)
        fbo_A.use()
        self.ctx.viewport = (0, 0, src_w, src_h)

        # --- Step 1: Keyer / qualifier -------------------------------------------
        if qp.get('enabled'):
            prog_keyer = self._get_utility_shader('keyer')
            if prog_keyer is None:
                # AUD-0206: keyer 缺失 → 降级为"qualifier 关闭"（全白 matte）
                self.ctx.clear(1.0, 1.0, 1.0, 1.0)
            else:
                self.ctx.clear(0.0, 0.0, 0.0, 1.0)
                # Use the source-resolution texture for mask-only matte generation
                # Note: This path is only used when qualifier is disabled (see _generate_matte)
                source_for_keyer = self._get_source_tex_for_qualifier(layer)
                if source_for_keyer is None:
                    source_for_keyer = input_tex
                source_for_keyer.use(0)
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
                self._render_quad(prog_keyer)
        else:
            self.ctx.clear(1.0, 1.0, 1.0, 1.0)

        # --- Step 2: Combine with mask shapes ------------------------------------
        if ms:
            mask_tex = self.layer_textures.get(l_id, {}).get('mask')
            if mask_tex:
                matte_tex_B = self.fb_manager.get_temp_texture(src_w, src_h, components=1, dtype='f2')
                fbo_B = self.fb_manager.get_fbo(matte_tex_B)
                fbo_B.use()
                self.ctx.viewport = (0, 0, src_w, src_h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                prog_combine = self.utility_shaders['mask_combine']

                # P2-1: Both qualifier output and mask texture are in source-space,
                # so the UV mapping is identity (no visible_rect remap needed).
                prog_combine['u_mask_uv_rect'].value = (0.0, 0.0, 1.0, 1.0)

                matte_tex_A.use(0)
                mask_tex.use(1)
                prog_combine['u_matte_a'].value = 0
                prog_combine['u_matte_b'].value = 1
                self._render_quad(prog_combine)
                self.fb_manager.release_temp_texture(matte_tex_A)
                matte_tex_A = matte_tex_B
                fbo_A = fbo_B

        # --- Step 3: Post-processing (morphology / blur) -------------------------
        matte_tex_A = self._apply_matte_postprocess(matte_tex_A, src_w, src_h, qp, ms)

        return matte_tex_A

    def _generate_viewport_matte(self, layer, input_tex, w, h, visible_rect, frame_epoch: int = 0):
        """Viewport-resolution matte generation (fallback for YUV video planes).

        P2-1: This path is used when no source-resolution texture is available.
        It retains visible_rect in the cache key because the mask_combine step
        bakes visible_rect UV remapping into the generated texture.

        Qualifier mattes additionally key on frame_epoch (bumped per uploaded
        video frame): during playback the keyed content changes every frame,
        so a stale matte must not be reused. Mask-only mattes stay cached —
        the mask texture itself is static between aux uploads.
        """
        l_id = layer.get('id')
        qp = layer.get('qualifier_params') or {}
        ms = layer.get('mask_shapes') or []

        # Early return: no qualifier + no mask = full white matte
        if not qp.get('enabled') and not ms:
            return self.white_tex

        params_hash = matte_cache_hash(
            qp, ms, (w, h), visible_rect,
            frame_epoch=int(frame_epoch) if qp.get('enabled') else 0,
            mask_epoch=self._mask_epoch.get(l_id, 0))

        cached = self.matte_cache.get(l_id)
        if cached and cached['params_hash'] == params_hash:
            return cached['matte']

        # AUD-0203: 旧条目在生成成功后才归还池（见 _generate_matte 源分辨
        # 率路径注释）——生成中途抛异常时缓存仍自洽，不会"命中"一张已被
        # 池再分配的纹理。
        matte_tex_A = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_A = self.fb_manager.get_fbo(matte_tex_A)
        fbo_A.use()
        self.ctx.viewport = (0, 0, w, h)

        # Keyer pass on viewport-resolution input_tex
        if qp.get('enabled'):
            prog_keyer = self._get_utility_shader('keyer')
            if prog_keyer is None:
                # AUD-0206: keyer 缺失 → 降级为"qualifier 关闭"（全白 matte）
                self.ctx.clear(1.0, 1.0, 1.0, 1.0)
            else:
                self.ctx.clear(0.0, 0.0, 0.0, 1.0)
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
                self._render_quad(prog_keyer)
        else:
            self.ctx.clear(1.0, 1.0, 1.0, 1.0)

        # Combine with mask shapes (needs visible_rect UV remap)
        if ms:
            mask_tex = self.layer_textures.get(l_id, {}).get('mask')
            if mask_tex:
                matte_tex_B = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
                fbo_B = self.fb_manager.get_fbo(matte_tex_B)
                fbo_B.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                prog_combine = self.utility_shaders['mask_combine']

                # Calculate UV mapping from viewport to full image
                if self.pyramid_textures:
                    img_w, img_h = self.pyramid_textures[0].size
                    vx, vy, vw, vh = visible_rect
                    u_offset = vx / img_w
                    v_offset = (vy + vh) / img_h
                    u_scale = vw / img_w
                    v_scale = -vh / img_h
                    prog_combine['u_mask_uv_rect'].value = (u_offset, v_offset, u_scale, v_scale)
                elif self.video_source_size[0] > 0:
                    img_w, img_h = self.video_source_size
                    vx, vy, vw, vh = visible_rect
                    u_offset = vx / float(img_w)
                    v_offset = (vy + vh) / float(img_h)
                    u_scale = vw / float(img_w)
                    v_scale = -vh / float(img_h)
                    prog_combine['u_mask_uv_rect'].value = (u_offset, v_offset, u_scale, v_scale)
                else:
                    prog_combine['u_mask_uv_rect'].value = (0, 0, 1, 1)

                matte_tex_A.use(0)
                mask_tex.use(1)
                prog_combine['u_matte_a'].value = 0
                prog_combine['u_matte_b'].value = 1
                self._render_quad(prog_combine)
                self.fb_manager.release_temp_texture(matte_tex_A)
                matte_tex_A = matte_tex_B
                fbo_A = fbo_B

        # Post-processing
        matte_tex_A = self._apply_matte_postprocess(matte_tex_A, w, h, qp, ms)

        if cached:
            self.fb_manager.release_temp_texture(cached['matte'])
        self.matte_cache[l_id] = {'matte': matte_tex_A, 'params_hash': params_hash}
        return matte_tex_A

    def _morphology_pass(self, src_tex, w, h, radius, operation):
        """One separable morphology sweep (horizontal + vertical).

        Box min/max factorises exactly, so two 1-D passes reproduce the old
        (2r+1)^2 square-window scan at a fraction of the texture fetches.
        Returns the new texture; src_tex is released.
        """
        prog_morph = self._get_utility_shader('morphology')
        if prog_morph is None:
            # AUD-0206: 形态学 shader 缺失 → 跳过该 pass，原样返回输入
            # （不释放 src_tex，所有权随返回值交给调用方）
            return src_tex
        prog_morph['u_texel_size'].value = (1.0 / w, 1.0 / h)
        prog_morph['u_radius'].value = float(radius)
        prog_morph['u_operation'].value = operation

        tex_h = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_h = self.fb_manager.get_fbo(tex_h)
        fbo_h.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        src_tex.use(0)
        prog_morph['u_tex'].value = 0
        prog_morph['u_direction'].value = 0
        self._render_quad(prog_morph)

        tex_v = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_v = self.fb_manager.get_fbo(tex_v)
        fbo_v.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        tex_h.use(0)
        prog_morph['u_tex'].value = 0
        prog_morph['u_direction'].value = 1
        self._render_quad(prog_morph)

        self.fb_manager.release_temp_texture(src_tex)
        self.fb_manager.release_temp_texture(tex_h)
        return tex_v

    def _apply_matte_postprocess(self, matte_tex_A, w, h, qp, ms):
        """Apply clean_black (erode), clean_white (dilate), and blur to matte."""

        # Collect blur, clean_black, clean_white from mask_shapes
        # IMPORTANT: For BITMAP (AI masks), we apply blur in GPU for per-mask control
        # For basic shapes (circle, rect, curve, gradient), blur is already applied in CPU layer
        max_blur = 0.0
        max_clean_black = 0.0
        max_clean_white = 0.0

        for shape in ms:
            # Handle both dict (serialized) and object (MaskShape class) formats
            if isinstance(shape, dict):
                blur = shape.get('blur', 0.0)
                clean_black = shape.get('clean_black', 0.0)
                clean_white = shape.get('clean_white', 0.0)
                shape_type = shape.get('type', '')
            else:
                # MaskShape object
                blur = getattr(shape, 'blur', 0.0)
                clean_black = getattr(shape, 'clean_black', 0.0)
                clean_white = getattr(shape, 'clean_white', 0.0)
                shape_type = getattr(shape, 'type', '')

            # Only collect blur for BITMAP (AI masks) - basic shapes are already processed in CPU
            if shape_type == 'bitmap':
                max_blur = max(max_blur, blur)
                max_clean_black = max(max_clean_black, clean_black)
                max_clean_white = max(max_clean_white, clean_white)

        # Also check qualifier_params for fallback (for backward compatibility)
        clean_black = max(max_clean_black, qp.get('clean_black', 0.0))
        clean_white = max(max_clean_white, qp.get('clean_white', 0.0))
        blur_radius = max(max_blur, qp.get('blur', 0.0))

        # Debug: Print collected mask parameters
        if clean_black > 0.01 or clean_white > 0.01 or blur_radius > 0.01:
            logger.debug(f"Debug] blur={blur_radius:.2f}, clean_black={clean_black:.2f}, clean_white={clean_white:.2f}")

        # Apply clean_black (erosion / black level cleanup)
        if clean_black > 0.01:
            # Convert 0-20 slider to radius (scale by image size for visible effect)
            radius = max(1.0, clean_black * 3.0)
            matte_tex_A = self._morphology_pass(matte_tex_A, w, h, radius, 0)  # Erode (min)
            logger.debug(f"Debug] Applied clean_black (erode) with radius={radius:.1f}")

        # Apply clean_white (dilation / white level cleanup)
        if clean_white > 0.01:
            # Convert 0-20 slider to radius (scale by image size for visible effect)
            radius = max(1.0, clean_white * 3.0)
            matte_tex_A = self._morphology_pass(matte_tex_A, w, h, radius, 1)  # Dilate (max)
            logger.debug(f"Debug] Applied clean_white (dilate) with radius={radius:.1f}")

        # Apply blur (Gaussian blur for smooth edges)
        if blur_radius > 0.01:
            prog_blur = self._get_utility_shader('blur')
            if prog_blur is None:
                # AUD-0206: blur shader 缺失 → 跳过模糊 pass
                logger.debug("Skip matte blur: blur shader unavailable")
            else:
                matte_tex_B = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
                fbo_B = self.fb_manager.get_fbo(matte_tex_B)

                # Convert 0-100 slider to blur radius
                # Increased scale factor for more visible blur effect
                radius = max(1.0, blur_radius * 1.0)

                fbo_B.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                matte_tex_A.use(0)
                prog_blur['u_tex'].value = 0
                prog_blur['u_radius'].value = float(radius)
                prog_blur['u_direction'].value = (1.0 / w, 0.0)
                self._render_quad(prog_blur)

                # Second pass for vertical blur
                fbo_A = self.fb_manager.get_fbo(matte_tex_A)
                fbo_A.use()
                self.ctx.viewport = (0, 0, w, h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)
                matte_tex_B.use(0)
                prog_blur['u_tex'].value = 0
                prog_blur['u_radius'].value = float(radius)
                prog_blur['u_direction'].value = (0.0, 1.0 / h)
                self._render_quad(prog_blur)

                self.fb_manager.release_temp_texture(matte_tex_B)
                logger.debug(f"Debug] Applied blur with radius={radius:.1f}")

        # P1-COLOR-08: qualifier denoise — small blur + smoothstep contrast
        # restore. Speckle noise blurs toward grey and collapses to 0/1 while
        # true edges regain steepness.
        denoise = qp.get('denoise', 0.0)
        prog_dn = self._get_utility_shader('matte_denoise') if denoise > 0.5 else None
        if denoise > 0.5 and prog_dn is not None:
            dn_radius = max(1.0, denoise * 0.04)
            matte_tex_A = self._blur_matte_separable(matte_tex_A, w, h, dn_radius)
            matte_tex_B = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
            fbo_B = self.fb_manager.get_fbo(matte_tex_B)
            fbo_B.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            matte_tex_A.use(0)
            prog_dn['u_tex'].value = 0
            prog_dn['u_strength'].value = float(min(denoise / 100.0, 1.0))
            self._render_quad(prog_dn)
            self.fb_manager.release_temp_texture(matte_tex_A)
            matte_tex_A = matte_tex_B

        return matte_tex_A

    def _blur_matte_separable(self, src_tex, w, h, radius):
        """Separable gaussian blur on a matte texture (H then V).

        Used by the qualifier denoise postprocess; releases src_tex and
        returns a new texture (matches image_engine._blur_matte_pass).
        """
        prog_blur = self._get_utility_shader('blur')
        if prog_blur is None:
            # AUD-0206: blur shader 缺失 → 跳过该 pass，原样返回输入
            return src_tex
        tex_h = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_h = self.fb_manager.get_fbo(tex_h)
        fbo_h.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        src_tex.use(0)
        prog_blur['u_tex'].value = 0
        prog_blur['u_radius'].value = float(radius)
        prog_blur['u_direction'].value = (1.0 / w, 0.0)
        self._render_quad(prog_blur)

        tex_v = self.fb_manager.get_temp_texture(w, h, components=1, dtype='f2')
        fbo_v = self.fb_manager.get_fbo(tex_v)
        fbo_v.use()
        self.ctx.viewport = (0, 0, w, h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)
        tex_h.use(0)
        prog_blur['u_tex'].value = 0
        prog_blur['u_radius'].value = float(radius)
        prog_blur['u_direction'].value = (0.0, 1.0 / h)
        self._render_quad(prog_blur)

        self.fb_manager.release_temp_texture(src_tex)
        self.fb_manager.release_temp_texture(tex_h)
        return tex_v

    def _sample_matte_to_viewport(self, src_matte_tex, src_w, src_h, vp_w, vp_h, visible_rect):
        """Resample a source-resolution matte into viewport space.

        P2-1: This is the only step that depends on visible_rect.
        It is cheap (single texture sample) and runs every frame on cache hit.
        """
        if src_matte_tex is None or src_matte_tex == self.white_tex:
            return self.white_tex

        # If source and viewport are the same size (e.g. video), skip resampling
        if src_w == vp_w and src_h == vp_h:
            return src_matte_tex

        # Allocate viewport-resolution output
        vp_matte = self.fb_manager.get_temp_texture(vp_w, vp_h, components=1, dtype='f2')
        vp_fbo = self.fb_manager.get_fbo(vp_matte)
        vp_fbo.use()
        self.ctx.viewport = (0, 0, vp_w, vp_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        prog = self.utility_shaders['matte_sample']
        src_matte_tex.use(0)
        prog['u_tex'].value = 0

        # Calculate UV mapping from viewport to full image
        # visible_rect is [vx, vy, vw, vh] in image pixels (top-down)
        if visible_rect and len(visible_rect) == 4:
            vx, vy, vw, vh = visible_rect
            u_offset = vx / float(src_w)
            v_offset = (vy + vh) / float(src_h)
            u_scale = vw / float(src_w)
            v_scale = -vh / float(src_h)
        else:
            u_offset, v_offset, u_scale, v_scale = 0.0, 0.0, 1.0, 1.0

        prog['u_uv_rect'].value = (u_offset, v_offset, u_scale, v_scale)
        self._render_quad(prog)

        return vp_matte

    def _render_quad(self, prog) -> None:
        """用缓存的 VAO 画一个全屏 TRIANGLE_STRIP quad（渲染线程）。

        VAO 按 program glo 缓存（P1-6），避免每次调用重新分配；``prog``
        为 None 时直接返回。此 quad 的 V=1 在屏幕上方（图像/翻转路径）；
        未做 CPU 垂直翻转的打包视频路径用 ``_render_video_quad``。
        """
        if prog is None: return
        # P1-6: Cache VAO per program to avoid per-call allocation overhead.
        prog_glo = prog.glo
        quad_vao_cache = self.__dict__.setdefault('_quad_vao_cache', {})
        vao = quad_vao_cache.get(prog_glo)
        if vao is None:
            vao = self.ctx.vertex_array(prog, [(self.quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
            quad_vao_cache[prog_glo] = vao
        vao.render(moderngl.TRIANGLE_STRIP)

    def _render_video_quad(self, prog) -> None:
        """Render a packed video quad using the native video orientation.

        Packed decoded frames are uploaded directly without a vertical CPU
        flip, so the top of the screen maps to texture V=0.
        """
        if prog is None:
            return
        prog_glo = prog.glo
        video_quad_vao_cache = self.__dict__.setdefault('_video_quad_vao_cache', {})
        video_quad_vbo = self.__dict__.get('video_quad_vbo')
        if video_quad_vbo is None:
            fallback_render_quad = self.__dict__.get('_render_quad')
            if callable(fallback_render_quad):
                fallback_render_quad(prog)
                return
            video_quad_vbo = self.__dict__.get('quad_vbo')
        if video_quad_vbo is None:
            logger.warning("_render_video_quad: video quad VBO is not initialized")
            return
        vao = video_quad_vao_cache.get(prog_glo)
        if vao is None:
            vao = self.ctx.vertex_array(prog, [(video_quad_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
            video_quad_vao_cache[prog_glo] = vao
        vao.render(moderngl.TRIANGLE_STRIP)

    def _release_quad_vao_cache(self):
        """P1-6: Release all cached quad VAOs (called on pipeline recompile and cleanup)."""
        for vao in self._quad_vao_cache.values():
            try:
                vao.release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
        self._quad_vao_cache.clear()
        for vao in self._video_quad_vao_cache.values():
            try:
                vao.release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
        self._video_quad_vao_cache.clear()

    def _render_tiles_to_current_fbo(
        self,
        source_tex,
        prog,
        params,
        lod,
        coordinate_size: Optional[Tuple[int, int]] = None,
    ):
        """把可见区内的 level-``lod`` 图像瓦片逐个画到当前绑定的 FBO。

        只绘制与 ``visible_rect`` 相交的 tile；顶点坐标按可见区归一到
        clip space，UV 按 ``coordinate_size``（默认为源纹理尺寸）归一，
        使全分辨率 bake 与预览金字塔共享同一 UV 空间。
        """
        texture_w, texture_h = source_tex.size
        coordinate_w, coordinate_h = coordinate_size or (texture_w, texture_h)
        vx, vy, v_width, v_height = params.get(
            'visible_rect', (0, 0, coordinate_w, coordinate_h)
        )
        scale_factor = 1.0 / (2**lod)
        for tile in self.tiles:
            if tile.level != lod: continue
            tx, ty = tile.x / scale_factor, tile.y / scale_factor
            tw, th = tile.w / scale_factor, tile.h / scale_factor
            if (tx + tw < vx or tx > vx + v_width or ty + th < vy or ty > vy + v_height):
                continue
            x0 = ((tx - vx) / v_width) * 2.0 - 1.0
            y0 = 1.0 - ((ty - vy) / v_height) * 2.0
            x1 = (((tx + tw) - vx) / v_width) * 2.0 - 1.0
            y1 = 1.0 - (((ty + th) - vy) / v_height) * 2.0
            u0 = tile.x / coordinate_w
            v0 = tile.y / coordinate_h
            u1 = (tile.x + tile.w) / coordinate_w
            v1 = (tile.y + tile.h) / coordinate_h
            self._render_tile_raw_uv(prog, x0, y0, x1, y1, u0, v0, u1, v1)

    def _get_baked_layer_texture(self, layer: Dict[str, Any]):
        """Return an uploaded bake only when the render DTO explicitly uses it."""
        if not layer.get('is_baked', False):
            return None
        layer_id = layer.get('id')
        texture = getattr(self, 'baked_textures', {}).get(layer_id)
        if texture is None:
            logger.debug("Baked layer %s has no uploaded texture; rendering live layer", layer_id)
        return texture

    def _baked_layer_coordinate_size(
        self,
        baked_size: Tuple[int, int],
        lod: int,
    ) -> Tuple[int, int]:
        """Return the coordinate space shared by preview tiles and a bake.

        A full-resolution bake may be larger than the preview pyramid.  Its UV
        coordinates still represent the same normalized source image, so tiles
        and ``visible_rect`` must keep the preview coordinate space instead of
        being scaled independently.
        """
        source_textures = getattr(self, 'pyramid_textures', ())
        if not source_textures or lod >= len(source_textures):
            return baked_size
        return tuple(source_textures[lod].size)

    def _update_composition_uniforms(self, prog, layer, grain_params, w, h, raw_preview=None, visible_rect=None, bindings=None):
        """P7-02：值提取与上传统一由 composition_uniforms 承载。

        ``raw_preview`` 历史参数已无消费者，保留签名兼容；
        ``bindings`` 为编译时使用的同一批 PluginBinding（缺省时从 layer
        重建，保证插件 uniform 名/索引与编译侧 manifest 对齐）。
        """
        if not prog or not layer:
            return
        from src.features.color.core.composition_uniforms import (
            composition_uniform_values,
            upload_composition_uniforms,
        )
        if bindings is None:
            from src.features.color.core.shader_compiler import (
                plugin_bindings_from_instances,
            )
            bindings = plugin_bindings_from_instances(
                layer.get('plugins', []) if isinstance(layer, dict)
                else getattr(layer, 'plugins', []))
        src_w, src_h = self.source_image_size
        values = composition_uniform_values(
            layer,
            grain_params=grain_params if grain_params is not None else {},
            width=w, height=h,
            tex_size=(float(src_w), float(src_h))
            if src_w > 0 and src_h > 0 else None,
            visible_rect=visible_rect,
            bindings=bindings,
        )
        upload_composition_uniforms(prog, values)

    def _bind_aux_textures_single_layer(self, prog, layer) -> None:
        """为单层 composition shader 绑定 aux 纹理并分配采样单元。

        curves / hsl_curve / warper 依次从 ``layer_textures`` 取，缺失时
        绑定对应默认（identity）纹理；仅当 program 声明了对应 uniform
        才占用纹理单元。lut3d 只在该层 ``active_lut_path`` 存在时绑定。
        """
        if not layer: return
        l_id = layer.get('id')
        tex_unit = 2
        tex = self.layer_textures.get(l_id, {}).get('curves') or self.default_curves_tex
        tex.use(tex_unit)
        if 'u_curves_tex' in prog:
            prog['u_curves_tex'].value = tex_unit
            tex_unit += 1
        
        # HSL Curve texture - bind default if not available
        hsl_tex = self.layer_textures.get(l_id, {}).get('hsl_curve') or self.default_hsl_curve_tex
        hsl_tex.use(tex_unit)
        if 'u_hsl_curve_tex' in prog:
            prog['u_hsl_curve_tex'].value = tex_unit
            tex_unit += 1
        
        tex = self.layer_textures.get(l_id, {}).get('warper') or self.default_warper_tex
        tex.use(tex_unit)
        if 'u_warper_tex' in prog:
            prog['u_warper_tex'].value = tex_unit
            tex_unit += 1
        if layer.get('active_lut_path'):
            tex = self.layer_textures.get(l_id, {}).get('lut3d') or self.identity_lut
            tex.use(tex_unit)
            if 'u_lut_tex' in prog:
                prog['u_lut_tex'].value = tex_unit
                tex_unit += 1

    def _ensure_tile_vao(self, prog):
        """P1-7: Lazily create persistent tile VBO/VAO. Returns (vbo, vao) or (None, None)."""
        if self._tile_vbo is None:
            try:
                # 16 floats = 4 vertices * (2 pos + 2 uv), allocated with DYNAMIC_DRAW
                self._tile_vbo = self.ctx.buffer(reserve=16 * 4, dynamic=True)
                self._tile_vao = self.ctx.vertex_array(prog, [
                    (self._tile_vbo, '2f 2f', 'in_vert', 'in_texcoord'),
                ])
                self._tile_prog_id = prog
            except Exception as e:
                logger.error(f"Failed to create tile VBO/VAO: {e}")
                return None, None
        # If program changed, rebuild VAO with new prog (VBO is reused)
        if self._tile_prog_id is not prog:
            try:
                self._tile_vao.release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
            try:
                self._tile_vao = self.ctx.vertex_array(prog, [
                    (self._tile_vbo, '2f 2f', 'in_vert', 'in_texcoord'),
                ])
                self._tile_prog_id = prog
            except Exception as e:
                logger.error(f"Failed to rebuild tile VAO: {e}")
                return None, None
        return self._tile_vbo, self._tile_vao

    def _render_tile_raw_uv(self, prog, x0, y0, x1, y1, u0, v0, u1, v1) -> None:
        """上传一个 tile 的 4 顶点（clip 坐标 + UV）并渲染（渲染线程）。

        优先复用持久化 VBO/VAO（P1-7），仅在创建失败时回退到一次性
        buffer；两条路径的异常都只记日志，不中断整帧渲染。
        """
        # P1-7: Reuse persistent VBO/VAO, only update vertex data per tile
        vbo, vao = self._ensure_tile_vao(prog)
        if vbo is None or vao is None:
            # Fallback to old per-tile allocation
            vertices = np.array([x0, y0, u0, v0, x0, y1, u0, v1, x1, y0, u1, v0, x1, y1, u1, v1], dtype='f4')
            fallback_vbo = self.ctx.buffer(vertices)
            try:
                fallback_vao = self.ctx.vertex_array(prog, [(fallback_vbo, '2f 2f', 'in_vert', 'in_texcoord')])
                fallback_vao.render(moderngl.TRIANGLE_STRIP)
                fallback_vao.release()
            except Exception as e:
                logger.error(f"Tile Error: {e}")
            finally:
                fallback_vbo.release()
            return
        vertices = np.array([x0, y0, u0, v0, x0, y1, u0, v1, x1, y0, u1, v0, x1, y1, u1, v1], dtype='f4')
        try:
            vbo.write(vertices)
            vao.render(moderngl.TRIANGLE_STRIP)
        except Exception as e:
            logger.error(f"Tile Error: {e}")

    def _bind_aux_textures(self, prog, layers) -> None:
        """按层索引绑定 aux 纹理（``L{i}_curves/warper/mask/lut/baked``）。

        uniform 名带层索引（legacy 多层变体）；shader 未声明的采样器跳过
        并记录 debug 日志。当前合成循环使用
        ``_bind_aux_textures_single_layer``，此方法保留给旧调用点/测试。
        """
        tex_unit_counter = 1
        for i, layer in enumerate(layers):
            if not layer: continue
            l_id = layer.get('id')
            for key in ['curves', 'warper', 'mask']:
                tex = self.layer_textures.get(l_id, {}).get(key)
                if not tex:
                    if key == 'curves': tex = self.default_curves_tex
                    elif key == 'warper': tex = self.default_warper_tex
                    else: continue
                if tex:
                    tex.use(tex_unit_counter)
                    try:
                        prog[f"L{i}_{key}_tex"].value = tex_unit_counter
                        tex_unit_counter += 1
                    except KeyError: logger.debug(f"Uniform L{i}_{key}_tex not found in shader")
            alp = layer.get('active_lut_path')
            if alp:
                tex = self.layer_textures.get(l_id, {}).get('lut3d') or self.identity_lut
                if tex:
                    tex.use(tex_unit_counter)
                    try:
                        prog[f"L{i}_lut_tex"].value = tex_unit_counter
                        tex_unit_counter += 1
                    except KeyError: logger.debug(f"Uniform L{i}_lut_tex not found in shader")

            # AI Baking: Bind baked texture if available
            is_baked = layer.get('is_baked', False)
            if is_baked and l_id in self.baked_textures:
                baked_tex = self.baked_textures[l_id]
                baked_tex.use(tex_unit_counter)
                try:
                    prog[f"L{i}_baked_tex"].value = tex_unit_counter
                    tex_unit_counter += 1
                except KeyError: logger.debug(f"Uniform L{i}_baked_tex not found in shader")

    def _create_identity_lut(self) -> None:
        """创建 2x2x2 恒等 3D LUT 纹理（渲染线程，run() 初始化阶段）。

        作为未激活 LUT3D 层的占位采样器。
        """
        if not self.ctx: return
        data = np.array([0,0,0, 1,0,0, 0,1,0, 1,1,0, 0,0,1, 1,0,1, 0,1,1, 1,1,1], dtype='f4').tobytes()
        self.identity_lut = self.ctx.texture3d((2, 2, 2), 3, data=data, dtype='f4')
        self.identity_lut.filter = (moderngl.LINEAR, moderngl.LINEAR)
        self.identity_lut.repeat_x = self.identity_lut.repeat_y = self.identity_lut.repeat_z = False

    def _create_defaults(self) -> None:
        """创建默认占位纹理与共享 quad VBO（渲染线程，run() 初始化阶段）。

        包含：线性 curves 纹理、6 行默认 HSL 曲线、2x2 warper、可重复
        采样的 1x1 ``white_tex``，以及图像 quad VBO（V=1 在上）与视频
        quad VBO（V=0 在上，匹配未做 CPU 垂直翻转的打包视频上传）。
        """
        if not self.ctx: return
        ramp = np.linspace(0, 1, 256, dtype=np.float32)
        ramp_rgb = np.stack([ramp]*3, axis=-1)
        data_c = ramp_rgb.astype(np.float32).tobytes()
        self.default_curves_tex = self.ctx.texture((256, 1), 3, data=data_c, dtype='f4')
        self.default_curves_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        
        # Default HSL curves: 6 curves, each 256 entries
        # Row 0: LumSat (default 1.0 multiplier)
        # Row 1: HueHue (default 0.0 shift)
        # Row 2: HueSat (default 1.0 multiplier)
        # Row 3: HueLum (default 1.0 multiplier)
        # Row 4: SatSat (default 1.0 multiplier)
        # Row 5: SatLum (default 1.0 multiplier)
        # HSL Curves: Texture layout is (256, 6) where:
        # - Each row (y=0..255) is one sample point
        # - Each column (x=0..5) is one curve type
        # So we need to transpose (6, 256) data to (256, 6) for correct memory layout
        hsl_defaults = np.zeros((6, 256), dtype=np.float32)
        # Multiplier curves default to 1.0
        hsl_defaults[0] = 1.0  # LumSat
        hsl_defaults[2] = 1.0  # HueSat
        hsl_defaults[3] = 1.0  # HueLum
        hsl_defaults[4] = 1.0  # SatSat
        hsl_defaults[5] = 1.0  # SatLum
        # HueHue defaults to 0.0 (no shift) - already zeros
        # Transpose: (6, 256) -> (256, 6) so each texture row has all 6 curve values at that sample point
        data_hsl = hsl_defaults.T.astype(np.float32).tobytes()
        self.default_hsl_curve_tex = self.ctx.texture((6, 256), 1, data=data_hsl, dtype='f4')
        self.default_hsl_curve_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        
        data_w = np.zeros((2, 2, 2), dtype=np.float32).tobytes()
        self.default_warper_tex = self.ctx.texture((2, 2), 2, data=data_w, dtype='f4')
        self.default_warper_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
        # CRITICAL: white_tex must be repeating so that 1x1 sampling returns 1.0 at any UV.
        # Using float16 (f2) to match the rest of the pipeline and ensure stable sampling.
        white_data = np.array([1.0, 1.0, 1.0, 1.0], dtype='f2').tobytes()
        self.white_tex = self.ctx.texture((1, 1), 4, data=white_data, dtype='f2')
        self.white_tex.filter = (moderngl.NEAREST, moderngl.NEAREST)
        self.white_tex.repeat_x = True
        self.white_tex.repeat_y = True
        vertices = np.array([-1.0, 1.0, 0.0, 1.0, -1.0, -1.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, -1.0, 1.0, 0.0], dtype='f4')
        self.quad_vbo = self.ctx.buffer(vertices)
        # Packed video data is uploaded without a CPU vertical flip; map the
        # top of the screen to V=0, matching the source row order.
        video_vertices = np.array([-1.0, 1.0, 0.0, 0.0, -1.0, -1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, -1.0, 1.0, 1.0], dtype='f4')
        self.video_quad_vbo = self.ctx.buffer(video_vertices)
        self._video_quad_vao_cache = {}

    def _do_update_aux_tex(self, data: Dict[str, Any]) -> None:
        """UPDATE_AUX_TEX：更新层辅助纹理（渲染线程）。

        按 ``data['type']`` 分派：``raw_input_lut``（全局 RAW LUT，path 为
        空表示清除）、``curves``、``hsl_curve``、``lut3d``、``warper``、
        ``mask``。已有纹理尺寸/dtype 匹配时原地 ``write`` 复用，否则释放
        重建；mask 上传会 bump ``_mask_epoch`` 使 matte 缓存失效。
        """
        tex_type = data['type']

        if tex_type == 'raw_input_lut':
            path = data.get('path')
            if self.raw_lut_tex:
                self.raw_lut_tex.release()
                self.raw_lut_tex = None
            if path:
                self.raw_lut_tex = self._upload_3dlut(path)
            return

        l_id = data.get('layer_id')
        if l_id is None: return

        if l_id not in self.layer_textures: self.layer_textures[l_id] = {}
        if tex_type == 'curves':
            tex_data = data['data']
            # Fallback for old uint8 data if any
            if len(tex_data) == 768:
                arr_u8 = np.frombuffer(tex_data, dtype=np.uint8)
                tex_data = (arr_u8.astype(np.float32) / 255.0).tobytes()

            tex = self.layer_textures[l_id].get('curves')
            if not tex:
                tex = self.ctx.texture((256, 1), 3, data=tex_data, dtype='f4')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                tex.repeat_x = tex.repeat_y = False
                self.layer_textures[l_id]['curves'] = tex
            else:
                # If texture exists but might have been created with different dtype (unlikely but safe)
                if tex.dtype != 'f4':
                    tex.release()
                    tex = self.ctx.texture((256, 1), 3, data=tex_data, dtype='f4')
                    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    tex.repeat_x = tex.repeat_y = False
                    self.layer_textures[l_id]['curves'] = tex
                else:
                    tex.write(tex_data)
        elif tex_type == 'hsl_curve':
            # HSL Curves: 2D R32F texture (6, 256) with 6 curves
            # Texture layout: width=6 (curve types), height=256 (sample points)
            # Data comes as (6, 256) array, needs transpose for correct memory layout
            lut_array = np.frombuffer(data['data'], dtype=np.float32).reshape(6, 256)
            tex_data = lut_array.T.tobytes()  # Transpose to (256, 6) memory layout
            tex = self.layer_textures[l_id].get('hsl_curve')
            if not tex:
                tex = self.ctx.texture((6, 256), 1, data=tex_data, dtype='f4')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                tex.repeat_x = tex.repeat_y = False
                self.layer_textures[l_id]['hsl_curve'] = tex
            else:
                if tex.dtype != 'f4' or tex.size != (6, 256):
                    tex.release()
                    tex = self.ctx.texture((6, 256), 1, data=tex_data, dtype='f4')
                    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    tex.repeat_x = tex.repeat_y = False
                    self.layer_textures[l_id]['hsl_curve'] = tex
                else:
                    tex.write(tex_data)
        elif tex_type == 'lut3d':
            path = data['path']
            tex = self._upload_3dlut(path)
            old_tex = self.layer_textures[l_id].pop('lut3d', None)
            if old_tex:
                try: old_tex.release()
                except Exception: logger.debug("Failed to release old lut3d texture")
            if tex:
                self.layer_textures[l_id]['lut3d'] = tex
            else:
                # 解析失败：清掉旧纹理（active_lut_path 已指向新 LUT，
                # 保留旧纹理会静默渲染错误的调色）；绑定路径回退 identity LUT
                logger.warning(
                    "3D LUT upload failed for %s; falling back to identity LUT",
                    path)
        elif tex_type == 'warper':
            w, h = data['size']
            tex_data = data['data']
            tex = self.layer_textures[l_id].get('warper')
            if not tex or tex.size != (w, h):
                if tex: tex.release()
                tex = self.ctx.texture((w, h), 2, data=tex_data, dtype='f4')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                tex.repeat_x = tex.repeat_y = False
                self.layer_textures[l_id]['warper'] = tex
            else: tex.write(tex_data)
        elif tex_type == 'mask':
            w, h = data['size']
            tex_data = data['data']
            tex = self.layer_textures[l_id].get('mask')
            if not tex or tex.size != (w, h):
                if tex: tex.release()
                tex = self.ctx.texture((w, h), 1, data=tex_data, dtype='f1')
                tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                tex.repeat_x = tex.repeat_y = False
                self.layer_textures[l_id]['mask'] = tex
            else: tex.write(tex_data)
            # P1-COLOR-08: mask 内容（AI matte/bitmap/brush 合成结果）变化不
            # 反映在 qp/ms 序列化里，bump epoch 让 matte cache 失效。
            self._mask_epoch[l_id] = self._mask_epoch.get(l_id, 0) + 1

    def _upload_3dlut(self, path: str) -> Optional[moderngl.Texture]:
        """解析 3D LUT（.cube/.3dl）并创建 3D 纹理（渲染线程）。

        P1-COLOR-11 收尾：统一走 lut_loader——支持 .3dl（Lustre）、行数
        校验、TITLE/DOMAIN metadata。非单位 DOMAIN 的表先重采样到
        [0,1] 域，与 CPU 导出采样（_apply_domain）同口径。解析失败时
        返回 None，由调用方清空旧纹理并回退 identity LUT。
        """
        try:
            from src.shared.color.lut_loader import load_lut, resample_to_unit_domain
            loaded = resample_to_unit_domain(load_lut(path))
            if loaded.shaper_1d is not None:
                logger.warning(
                    "LUT %s carries a 1D shaper; GPU path ignores it", path)
            data = np.ascontiguousarray(loaded.table, dtype='f4').tobytes()
            tex = self.ctx.texture3d((loaded.size,) * 3, 3, data=data, dtype='f4')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.repeat_x = tex.repeat_y = tex.repeat_z = False
            return tex
        except Exception as e:
            logger.warning(f"Error uploading 3D LUT {path}: {e}")
        return None

    def _do_upload(self, img_u8, is_reference=False, original_size=None, is_video=False):
        """
        GPU 硬件级 Mipmap 上传 - 彻底摒弃 CPU 端软件金字塔生成
        
        优化说明：
        - 旧方案：CPU 端循环 6 次 cv2.pyrDown() 生成金字塔，每层单独上传
        - 新方案：只上传原图 (Level 0) 一次，调用 tex.build_mipmaps() 让 GPU 硬件自动生成所有层级
        - 收益：减少 ~50% 图像上传时间，释放 CPU 算力，GPU 硬件 Mipmap 质量更优
        """
        if not self.ctx: return
        
        # 清理旧纹理
        target_list = self.reference_pyramid_textures if is_reference else self.pyramid_textures
        for tex in target_list:
            try: tex.release()
            except Exception: logger.debug("Failed to release texture during _do_upload cleanup")
        
        if is_reference:
            self.reference_pyramid_textures = []
        else:
            self.pyramid_textures = []
            self.tiles = []
            # UPLOAD_IMAGE becomes the active source for image mode and for
            # video HQ/first-frame renders. Clear stale video inputs so _do_render
            # does not prefer old YUV planes or an old playback texture over the
            # freshly uploaded pyramid texture.
            self.current_video_texture = None
            self.current_video_planes = None
            self.current_video_pixel_format = None
            self.current_video_chroma_siting = 0
            self.current_video_frame_index = -1
            # Stamp the pyramid with the current video epoch so _do_render can
            # refuse to fall back onto it once a newer clip epoch begins.
            # Read via the instance dictionary: lightweight controller tests
            # build the renderer with __new__ and never run QObject.__init__,
            # so plain attribute access can raise RuntimeError.
            try:
                self._pyramid_epoch_generation = int(
                    self.__dict__.get('_video_generation_id', 0) or 0
                )
            except RuntimeError:
                pass
            # 记录原始图像尺寸用于锐化效果的一致性
            if original_size:
                self.source_image_size = original_size
            else:
                h, w = img_u8.shape[:2]
                self.source_image_size = (w, h)
            if is_video:
                self.video_source_size = self.source_image_size
            else:
                self.video_source_size = (0, 0)
        
        # 只上传原图 (Level 0) 一次
        h, w = img_u8.shape[:2]
        rgb = cv2.cvtColor(img_u8, cv2.COLOR_BGR2RGB)
        rgba = cv2.cvtColor(rgb, cv2.COLOR_RGB2RGBA)
        
        tex = self.ctx.texture((w, h), 4, data=rgba.astype(np.float16).tobytes(), dtype='f2', alignment=4)
        # 启用三线性过滤 + Mipmap，GPU 会根据 Screen-space derivatives 自动选择最佳层级
        tex.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        tex.repeat_x = tex.repeat_y = False
        # GPU 硬件一次性自动生成所有 Mipmap 层级，实现达芬奇/Lightroom 级别的平滑缩放
        tex.build_mipmaps()
        
        (self.reference_pyramid_textures if is_reference else self.pyramid_textures).append(tex)
        
        # 生成分块信息（只针对 Level 0 原图）
        if not is_reference:
            self._ensure_source_tiles(w, h, force=True)

    def _ensure_source_tiles(self, width: int, height: int, force: bool = False) -> None:
        """Ensure level-0 source tiles match the currently active source size."""
        if width <= 0 or height <= 0:
            return

        needs_rebuild = force or not self.tiles
        if not needs_rebuild:
            level0_tiles = [tile for tile in self.tiles if getattr(tile, 'level', None) == 0]
            if not level0_tiles:
                needs_rebuild = True
            else:
                max_x = max(tile.x + tile.w for tile in level0_tiles)
                max_y = max(tile.y + tile.h for tile in level0_tiles)
                needs_rebuild = max_x != width or max_y != height

        if not needs_rebuild:
            return

        from src.features.color.core.image_engine import Tile
        tile_size = 512
        self.tiles = []
        for y in range(0, height, tile_size):
            for x in range(0, width, tile_size):
                tw = min(tile_size, width - x)
                th = min(tile_size, height - y)
                self.tiles.append(Tile(x, y, tw, th, level=0))

    def _do_upload_video_frame(self, data: Dict[str, Any]) -> Optional[moderngl.Texture]:
        """
        P4.2: Upload video frame without mipmap generation.
        
        与 UPLOAD_IMAGE 的区别：
        - 不调用 build_mipmaps()（视频帧不需要 mipmaps）
        - 使用 VideoTexturePool 纹理池（避免每帧创建/释放）
        - 更轻量，适合每帧上传
        
        Args:
            data: 包含以下键的字典：
                - 'frame_data': numpy 数组（float32, 归一化到 [0,1]）
                - 'frame_index': 帧号（用于纹理池追踪）
                - 'width': 帧宽度
                - 'height': 帧高度
                - 'components': 通道数（可选，默认 4）
                - 'dtype': 数据类型（可选，默认 'f2'）
                
        Returns:
            上传的 Texture 对象，或 None（失败时）
        """
        if not self.ctx or not data:
            return None
            
        frame_data = data.get('frame_data')
        frame_index = data.get('frame_index', -1)
        width = data.get('width', 0)
        height = data.get('height', 0)
        components = data.get('components', 4)
        dtype = data.get('dtype', 'f2')
        
        if frame_data is None or width <= 0 or height <= 0:
            logger.warning("Video frame upload: Invalid data or dimensions")
            return None
        
        # 从纹理池获取纹理槽位
        texture = self._video_texture_pool.acquire(
            width=width,
            height=height,
            frame_index=frame_index,
            components=components,
            dtype=dtype
        )
        
        if texture is None:
            logger.error("Video frame upload: Failed to acquire texture from pool")
            return None
        
        # 上传帧数据（不调用 build_mipmaps）
        try:
            texture.write(frame_data.tobytes())
        except Exception as e:
            logger.error(f"Video frame upload: Failed to write data: {e}")
            return None
        # P4.2: Update renderer state for video input
        # This makes UPLOAD_VIDEO_FRAME the actual display input
        self.current_video_texture = texture
        self.current_video_planes = None
        self.current_video_pixel_format = None
        self.current_video_chroma_siting = 0
        self.current_video_frame_index = frame_index
        self.video_source_size = (width, height)

        # Also update source_image_size for effect scaling consistency
        self.source_image_size = (width, height)
        self._video_needs_swizzle = False
        self._ensure_source_tiles(width, height)

        logger.debug(f"Video frame uploaded: frame_index={frame_index}, "
                    f"size={width}x{height}, pool_stats={self._video_texture_pool.get_stats()}")
        
        return texture

    def _do_upload_decoded_video_frame(self, data: Dict[str, Any]) -> Optional[moderngl.Texture]:
        """
        P4.A: Upload packed RGB/RGBA frame from DecodedFrame.

        P1-8 + P3-1 optimized:
        - 8-bit formats upload as normalized 8-bit (f1) directly, avoiding CPU float16 conversion
        - BGR/BGRA swizzle handled in GPU shader (u_bgr_swizzle uniform), avoiding CPU memcpy
        - High bit-depth (10-bit+) uses float16 path with same GPU swizzle

        CPU operations eliminated:
        - No float16 conversion for 8-bit frames
        - No numpy index swap for BGR/BGRA channels
        - Only minimal alpha channel concatenation for 3-channel formats

        Args:
            data: 包含以下键的字典：
                - 'decoded_frame': DecodedFrame 对象
                - 'frame_index': 帧号（可选，从 DecodedFrame 获取）

        Returns:
            上传的 Texture 对象，或 None（失败时）
        """
        if not self.ctx or not data:
            return None

        from src.shared.media.frame import DecodedFrame, get_pixel_format

        decoded_frame: Optional[DecodedFrame] = data.get('decoded_frame')
        if decoded_frame is None or decoded_frame.data is None:
            logger.warning("UPLOAD_DECODED_VIDEO_FRAME: Invalid DecodedFrame or no packed data")
            return None

        frame_index = data.get('frame_index', decoded_frame.frame_index)
        pixel_format = decoded_frame.pixel_format
        width = decoded_frame.width
        height = decoded_frame.height

        if width <= 0 or height <= 0:
            logger.warning("UPLOAD_DECODED_VIDEO_FRAME: Invalid dimensions")
            return None

        format_info = get_pixel_format(pixel_format)
        if format_info is None or format_info.is_planar:
            logger.warning(f"UPLOAD_DECODED_VIDEO_FRAME: {pixel_format} is planar, use UPLOAD_VIDEO_PLANES")
            return None

        components = 4 if format_info.has_alpha else 3
        needs_swizzle = pixel_format.lower().startswith('bgr')
        frame_data = decoded_frame.data

        # P1-8: 8-bit fast path - upload as normalized bytes directly
        is_8bit = format_info.bit_depth <= 8 and frame_data.dtype == np.uint8

        if is_8bit:
            # P3-1: Upload uint8 directly as normalized bytes, no CPU swizzle
            # BGR/BGRA swizzle happens in GPU shader (u_bgr_swizzle uniform)
            dtype = 'f1'

            # Ensure data is contiguous
            if not frame_data.flags['C_CONTIGUOUS']:
                frame_data = np.ascontiguousarray(frame_data)

            # For 3-channel (rgb24/bgr24), add alpha channel as uint8 255
            # since the texture pool expects 4-component for video textures
            if frame_data.ndim == 3 and frame_data.shape[2] == 3:
                alpha = np.full((height, width, 1), 255, dtype=np.uint8)
                frame_data = np.concatenate([frame_data, alpha], axis=2)
                components = 4
        else:
            # High bit-depth path (10-bit, 16-bit, etc.) - use float16
            # P3-1: No CPU swizzle, handled by GPU shader
            if format_info.bit_depth > 8:
                max_val = (1 << format_info.bit_depth) - 1
                frame_data = frame_data.astype(np.float32) / max_val

            frame_data = frame_data.astype(np.float16)
            dtype = 'f2'

            # 添加 alpha 通道 (high bit-depth path)
            if components == 4 and frame_data.ndim == 3 and frame_data.shape[2] == 3:
                alpha = np.ones((height, width, 1), dtype=np.float16)
                frame_data = np.concatenate([frame_data, alpha], axis=2)

        texture = self._video_texture_pool.acquire(width, height, frame_index, components, dtype)
        if texture is None:
            logger.error("UPLOAD_DECODED_VIDEO_FRAME: Failed to acquire texture")
            return None

        try:
            texture.write(frame_data.tobytes())
        except Exception as e:
            logger.error(f"UPLOAD_DECODED_VIDEO_FRAME: Write failed: {e}")
            return None

        # P4.A: Update state
        self.current_video_texture = texture
        self.current_video_planes = None
        self.current_video_pixel_format = pixel_format
        self.current_video_frame_index = frame_index
        self.video_source_size = (width, height)
        self.source_image_size = (width, height)
        self._video_needs_swizzle = needs_swizzle
        self._ensure_source_tiles(width, height)

        logger.debug(f"Decoded video frame uploaded: frame={frame_index}, fmt={pixel_format}, "
                    f"size={width}x{height}, swizzle={needs_swizzle}")
        return texture

    def _do_upload_video_planes(self, data: Dict[str, Any]) -> Optional[tuple]:
        """
        P4.A: Upload YUV/NV12/P010 planes from DecodedFrame.

        Args:
            data: 包含以下键的字典：
                - 'decoded_frame': DecodedFrame 对象
                - 'yuv_matrix': YUV 矩阵（可选）
                - 'color_range': 颜色范围（可选）

        Returns:
            (y_tex, u_tex, v_tex) 或 (y_tex, uv_tex) 元组
        """
        if not self.ctx or not data:
            return None

        frame_index = data.get('frame_index', -1)

        # P1-4: Back off after repeated failures, but periodically retry so a
        # transient GL/upload error does not disable the GPU path forever.
        if getattr(self, '_planar_upload_disabled', False):
            retry_interval = 120
            if frame_index < 0 or frame_index % retry_interval != 0:
                return None
            self._planar_upload_disabled = False

        from src.shared.media.frame import DecodedFrame, get_pixel_format

        decoded_frame: Optional[DecodedFrame] = data.get('decoded_frame')
        normalized_frame = self._materialize_contiguous_semiplanar_frame(decoded_frame)
        if normalized_frame is not None:
            decoded_frame = normalized_frame
        if decoded_frame is None or not decoded_frame.is_planar or not decoded_frame.planes:
            logger.warning("UPLOAD_VIDEO_PLANES: Invalid DecodedFrame or no planar data")
            return None

        frame_index = data.get('frame_index', decoded_frame.frame_index)
        pixel_format = decoded_frame.pixel_format
        width = decoded_frame.width
        height = decoded_frame.height

        if width <= 0 or height <= 0:
            logger.warning("UPLOAD_VIDEO_PLANES: Invalid dimensions")
            return None

        format_info = get_pixel_format(pixel_format)
        if format_info is None or not format_info.is_planar:
            logger.warning(f"UPLOAD_VIDEO_PLANES: {pixel_format} is not planar")
            return None

        yuv_matrix = data.get('yuv_matrix', 0)
        color_range = data.get('color_range', 0)
        frame_extra = getattr(decoded_frame, 'extra', None) or {}
        chroma_siting = int(data.get(
            'chroma_siting', frame_extra.get('topos_chroma_siting', 0)
        ) or 0)

        self._yuv_gpu_uploader.set_yuv_matrix(yuv_matrix)
        self._yuv_gpu_uploader.set_color_range(color_range)

        planes = decoded_frame.planes
        bit_depth = format_info.bit_depth

        # YUVA 第四平面（Topos a8/a10/a12/a16 / FFmpeg yuva*）：Alpha 位深
        # 独立于 luma（format_info.bit_depth 是 luma 的），必须从 plane_infos[3] 取。
        alpha_plane = None
        alpha_bit_depth = bit_depth
        alpha_premult = False
        if len(planes) >= 4:
            candidate = planes[3]
            if (
                candidate.ndim == 2
                and candidate.shape[:2] == (height, width)
            ):
                plane_infos = getattr(decoded_frame, 'plane_infos', None)
                if plane_infos and len(plane_infos) >= 4:
                    alpha_bit_depth = int(
                        getattr(plane_infos[3], 'bit_depth', bit_depth) or bit_depth)
                alpha_plane = candidate
                alpha_premult = bool(
                    (getattr(decoded_frame, 'extra', None) or {}).get(
                        'topos_alpha_premultiplied', False))
            else:
                logger.debug(
                    f"UPLOAD_VIDEO_PLANES: alpha plane shape {candidate.shape[:2]} "
                    f"!= frame {(height, width)}, dropping alpha")

        # R16UI: only enable when all conditions are met:
        # 1. bit_depth > 8
        # 2. is_r16_available() (probe + self-test both passed)
        # 3. Planar YUV format (not NV12/P010)
        # 4. plane dtype is uint16
        from src.features.edit.core.edit_gpu_preview_renderer import _classify_planar_format
        upload_mode = _classify_planar_format(pixel_format, len(planes), bit_depth)
        is_2plane = upload_mode in ('nv12', 'p010', 'p016')

        use_r16ui = (
            bit_depth > 8
            and self._yuv_gpu_uploader.is_r16_available()
            and not is_2plane
            and len(planes) >= 3
            and planes[0].dtype == np.uint16
        )

        # R16UI shader 的 a_plane 是 usampler2D：Alpha 平面 dtype 必须与主平面
        # 一致（uint16）。混合 dtype（理论罕见）时丢弃 Alpha 而不是整帧降级。
        if alpha_plane is not None and use_r16ui and alpha_plane.dtype != np.uint16:
            logger.debug(
                f"UPLOAD_VIDEO_PLANES: alpha dtype {alpha_plane.dtype} != uint16 "
                f"on r16ui path, dropping alpha")
            alpha_plane = None

        try:
            if upload_mode == 'nv12':
                y_tex, uv_tex = self._yuv_gpu_uploader.upload_nv12(
                    planes[0], planes[1], frame_index, bit_depth)
                result_planes = (y_tex, uv_tex)
            elif upload_mode == 'p010':
                y_tex, uv_tex = self._yuv_gpu_uploader.upload_p010(
                    planes[0], planes[1], frame_index)
                result_planes = (y_tex, uv_tex)
            elif upload_mode == 'p016':
                y_tex, uv_tex = self._yuv_gpu_uploader.upload_p010(
                    planes[0], planes[1], frame_index)
                result_planes = (y_tex, uv_tex)
            elif upload_mode in ('yuv420', 'yuv411'):
                if use_r16ui:
                    result_planes = self._yuv_gpu_uploader.upload_yuv420_r16ui(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                else:
                    result_planes = self._yuv_gpu_uploader.upload_yuv420(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
            elif upload_mode in ('yuv422',):
                if use_r16ui:
                    result_planes = self._yuv_gpu_uploader.upload_yuv422_r16ui(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                else:
                    result_planes = self._yuv_gpu_uploader.upload_yuv422(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
            elif upload_mode in ('yuv444', 'yuv440', 'rgb_planar'):
                if use_r16ui:
                    result_planes = self._yuv_gpu_uploader.upload_yuv444_r16ui(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
                else:
                    result_planes = self._yuv_gpu_uploader.upload_yuv444(
                        planes[0], planes[1], planes[2], frame_index, bit_depth,
                        a=alpha_plane, alpha_bit_depth=alpha_bit_depth)
            else:
                logger.warning(f"Unknown YUV format {pixel_format}, treating as YUV420")
                result_planes = self._yuv_gpu_uploader.upload_yuv420(
                    planes[0], planes[1], planes[2], frame_index, bit_depth,
                    a=alpha_plane, alpha_bit_depth=alpha_bit_depth)

            # Reset failure counter on success — transient failures should not
            # permanently disable the GPU upload path.
            if getattr(self, '_planar_upload_failures', 0) > 0:
                self._planar_upload_failures = 0

            # 记录分段计时到性能监控器
            timing = self._yuv_gpu_uploader.get_last_upload_timing()
            if timing:
                from src.shared.media.performance_monitor import get_performance_monitor
                perf = get_performance_monitor()
                perf.track_plane_prepare_time(timing.get('prepare_ms', 0))
                perf.track_texture_write_time(timing.get('write_ms', 0))
                mode = timing.get('mode', 'float16')
                logger.debug(f"Video planes uploaded: mode={mode}, "
                           f"prepare={timing.get('prepare_ms', 0):.2f}ms, "
                           f"write={timing.get('write_ms', 0):.2f}ms")
        except Exception as e:
            # Track consecutive failures to avoid per-frame retry spam
            self._planar_upload_failures = getattr(self, '_planar_upload_failures', 0) + 1
            if self._planar_upload_failures >= 3:
                # Disable until the next low-frequency retry.
                self._planar_upload_disabled = True
                logger.error(
                    f"Planar YUV GPU upload failed {self._planar_upload_failures} times, "
                    f"temporarily disabled until retry. Last error: {e}"
                )
            else:
                logger.warning(f"Planar YUV GPU upload failed, falling back to CPU conversion: {e}")
            return None

        # P4.A: Update state
        self.current_video_texture = None
        self.current_video_planes = result_planes
        self.current_video_pixel_format = pixel_format
        self.current_video_frame_index = frame_index
        self.video_source_size = (width, height)
        self.source_image_size = (width, height)
        self._video_needs_swizzle = False

        # P0-3: Save YUV metadata for rendering (from actual frame, not raw_preview)
        self.current_video_yuv_matrix = int(yuv_matrix)
        self.current_video_color_range = int(color_range)
        self.current_video_bit_depth = int(bit_depth)
        self.current_video_chroma_siting = chroma_siting
        # Alpha 状态：渲染时 render_yuv_*_to_rgb 需要（位深独立 + 预乘标志）
        self.current_video_has_alpha = len(result_planes) >= 4
        self.current_video_alpha_bit_depth = int(alpha_bit_depth)
        self.current_video_alpha_premultiplied = bool(alpha_premult)

        logger.debug(f"Video planes uploaded: frame={frame_index}, fmt={pixel_format}, "
                    f"size={width}x{height}, yuv_matrix={yuv_matrix}, color_range={color_range}, "
                    f"alpha={self.current_video_has_alpha}")
        return result_planes

    def _do_upload_planar_video_frame_cpu_fallback(
        self,
        decoded_frame: Any,
        frame_index: int,
        yuv_matrix: int = 0,
        color_range: int = 0,
    ) -> Optional[moderngl.Texture]:
        """
        Convert planar YUV to RGBA on the renderer thread and upload it.

        This is intentionally a renderer-thread fallback. Doing this conversion
        from the UI playback tick causes 100ms+ stalls on ProRes/MOV footage.
        """
        try:
            if decoded_frame is None or len(decoded_frame.planes) < 3:
                return None

            from src.features.color.core.color_manager import ColorMatrix, ColorRange
            from src.shared.media.color_decode import get_color_converter

            matrix_map = {
                0: ColorMatrix.BT709,
                1: ColorMatrix.BT2020_NC,
                2: ColorMatrix.BT601,
            }
            range_map = {
                0: ColorRange.LIMITED,
                1: ColorRange.FULL,
            }

            converter = get_color_converter()
            rgb = converter.yuv_to_rgb(
                y=decoded_frame.planes[0],
                u=decoded_frame.planes[1],
                v=decoded_frame.planes[2],
                matrix=matrix_map.get(int(yuv_matrix), ColorMatrix.BT709),
                range_=range_map.get(int(color_range), ColorRange.LIMITED),
                bit_depth=getattr(decoded_frame, 'bit_depth', 8),
            )

            # Alpha 归一化必须用 alpha 自己的位深（plane_infos[3]），
            # 不能用格式级 bit_depth——Topos a8/a10/a12/a16 与 luma 的 10 不同。
            alpha_plane = None
            alpha_bit_depth = 8
            if len(decoded_frame.planes) >= 4:
                candidate = decoded_frame.planes[3]
                if candidate.shape[:2] == (decoded_frame.height, decoded_frame.width):
                    alpha_plane = candidate
                    plane_infos = getattr(decoded_frame, 'plane_infos', None)
                    if plane_infos and len(plane_infos) >= 4:
                        alpha_bit_depth = int(
                            getattr(plane_infos[3], 'bit_depth', 8) or 8)

            if alpha_plane is not None:
                # 带 Alpha 的帧走 RGBA16F：保留 10/12/16-bit Alpha 不降到 8-bit
                # （P7-Alpha：修复此前 /255 错误归一化 + f1 量化双重降级）。
                a_max = float((1 << alpha_bit_depth) - 1)
                alpha_f = np.clip(
                    alpha_plane.astype(np.float32) / a_max, 0.0, 1.0)[..., None]
                rgba = np.ascontiguousarray(
                    np.concatenate([rgb, alpha_f], axis=2)).astype(np.float16)
                out_dtype = 'f2'
            else:
                rgb8 = (np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)
                alpha8 = np.full(
                    (decoded_frame.height, decoded_frame.width, 1),
                    255, dtype=np.uint8)
                rgba = np.ascontiguousarray(
                    np.concatenate([rgb8, alpha8], axis=2))
                out_dtype = 'f1'

            return self._do_upload_video_frame({
                'frame_data': rgba,
                'frame_index': frame_index,
                'width': decoded_frame.width,
                'height': decoded_frame.height,
                'components': 4,
                'dtype': out_dtype,
            })

        except Exception as e:
            logger.error(f"Planar CPU fallback upload failed: {e}", exc_info=True)
            return None

    # ================================================================
    # GPU YUV Self-Test
    # ================================================================

    def _run_yuv_gpu_self_test(self) -> None:
        """
        P1: Run GPU YUV self-test on the render thread.

        Tests YUV444 -> RGB conversion using synthetic data.
        Sets _yuv_gpu_supported based on result.
        Must be called after ctx and _yuv_gpu_uploader are initialized.
        """
        import sys

        # Only run self-test on macOS; other platforms assume GPU YUV works
        if sys.platform != 'darwin':
            self._yuv_gpu_supported = True
            self._yuv_gpu_self_test_result = None
            logger.info("YUV GPU self-test: skipped (non-macOS, assumed supported)")
            return

        try:
            from src.shared.video.yuv_gpu_self_test import run_yuv_gpu_self_test
            result = run_yuv_gpu_self_test(self.ctx, self._yuv_gpu_uploader)
            self._yuv_gpu_self_test_result = result
            self._yuv_gpu_supported = result.passed

            if result.passed:
                logger.info(f"YUV GPU self-test PASSED: {result}")
            else:
                logger.warning(f"YUV GPU self-test FAILED: {result}. "
                             f"GPU YUV path disabled, will use CPU fallback.")
        except Exception as e:
            self._yuv_gpu_supported = False
            logger.error(f"YUV GPU self-test exception: {e}", exc_info=True)

        # Run R16UI self-test (12-bit integer texture path)
        try:
            from src.shared.video.yuv_gpu_self_test import run_r16ui_gpu_self_test
            r16ui_result = run_r16ui_gpu_self_test(self.ctx, self._yuv_gpu_uploader)
            if r16ui_result.passed:
                logger.info(f"R16UI YUV self-test PASSED: {r16ui_result}")
            else:
                logger.warning(f"R16UI YUV self-test FAILED: {r16ui_result}. "
                             f"R16UI path disabled, will use float16 fallback.")
        except Exception as e:
            logger.warning(f"R16UI YUV self-test exception: {e}")

    def get_yuv_gpu_status(self) -> Dict[str, Any]:
        """
        Get current YUV GPU status for UI/logging.

        Returns dict with:
            - supported: bool
            - self_test_result: str or None
            - reason: str
        """
        result = self._yuv_gpu_self_test_result
        return {
            'supported': self._yuv_gpu_supported,
            'self_test_result': str(result) if result else 'not_run',
            'reason': result.reason if result else 'not_tested',
        }

    def log_video_path_decision(
        self,
        pixel_format: str,
        proxy_size: tuple,
        preserve_planes: bool,
        yuv_gpu_config: bool,
    ) -> None:
        """
        Log the video path decision once when opening a video.

        Call from playback engine after determining the frame path.
        """
        import sys
        self_test_status = 'passed' if self._yuv_gpu_supported else 'failed'
        logger.info(
            f"Video path decision:\n"
            f"  platform={sys.platform}\n"
            f"  pixel_format={pixel_format}\n"
            f"  proxy_size={proxy_size[0]}x{proxy_size[1]}\n"
            f"  preserve_planes={preserve_planes}\n"
            f"  yuv_gpu_config={yuv_gpu_config}\n"
            f"  yuv_gpu_self_test={self_test_status}\n"
            f"  playback_frame_path={self._debug_frame_path}"
        )

    def _do_release_video_texture_pool(self) -> None:
        """
        P4.2: Release video texture pool resources.
        
        用于视频播放结束或尺寸变化时清理纹理池。
        """
        if self._video_texture_pool:
            self._video_texture_pool.release_all()
            logger.debug("Video texture pool released")

    @staticmethod
    def _materialize_contiguous_semiplanar_frame(decoded_frame: Any) -> Optional[Any]:
        """Split a contiguous hardware-decoded NV12/P010 buffer into Y and UV.

        PyAV and VideoToolbox may return semi-planar frames as a single
        ``(height * 3 / 2, width)`` ndarray.  The byte layout is still Y + UV,
        but ``DecodedFrame.is_planar`` is false because no explicit ``planes``
        tuple was constructed.  Normalize that representation at the renderer
        boundary so every upload entry point takes the same GPU YUV path.
        """
        from src.shared.media.frame import DecodedFrame, get_pixel_format

        if not isinstance(decoded_frame, DecodedFrame) or decoded_frame.is_planar:
            return decoded_frame

        pixel_format = str(decoded_frame.pixel_format or '').lower().strip()
        format_info = get_pixel_format(pixel_format)
        if (
            pixel_format not in _CONTIGUOUS_SEMIPLANAR_420_FORMATS
            or format_info is None
            or not format_info.is_planar
            or decoded_frame.data is None
        ):
            return None

        width = int(decoded_frame.width)
        height = int(decoded_frame.height)
        if width <= 0 or height <= 0 or width % 2 != 0 or height % 2 != 0:
            logger.warning(
                "RENDER_VIDEO_FRAME: Invalid contiguous %s dimensions %sx%s",
                pixel_format,
                width,
                height,
            )
            return None

        raw_data = np.asarray(decoded_frame.data)
        chroma_height = height // 2
        required_rows = height + chroma_height
        if raw_data.ndim == 1:
            required_values = required_rows * width
            if raw_data.size < required_values:
                logger.warning(
                    "RENDER_VIDEO_FRAME: Contiguous %s buffer is too small (%s < %s)",
                    pixel_format,
                    raw_data.size,
                    required_values,
                )
                return None
            packed = raw_data[:required_values].reshape(required_rows, width)
        elif raw_data.ndim == 2:
            if raw_data.shape[0] < required_rows or raw_data.shape[1] < width:
                logger.warning(
                    "RENDER_VIDEO_FRAME: Contiguous %s shape %s cannot hold %sx%s",
                    pixel_format,
                    raw_data.shape,
                    width,
                    required_rows,
                )
                return None
            packed = raw_data[:required_rows, :width]
        else:
            logger.warning(
                "RENDER_VIDEO_FRAME: Unsupported contiguous %s data shape %s",
                pixel_format,
                raw_data.shape,
            )
            return None

        y_plane = np.ascontiguousarray(packed[:height, :width])
        uv_plane = np.ascontiguousarray(packed[height:required_rows, :width])
        return DecodedFrame(
            frame_index=decoded_frame.frame_index,
            pts=decoded_frame.pts,
            time_seconds=decoded_frame.time_seconds,
            data=None,
            planes=(y_plane, uv_plane),
            pixel_format=pixel_format,
            dtype=raw_data.dtype,
            width=width,
            height=height,
            metadata=decoded_frame.metadata,
            is_proxy=decoded_frame.is_proxy,
            plane_infos=decoded_frame.plane_infos,
            extra=decoded_frame.extra,
        )

    def _apply_video_denoise(self, source_tex, params: Dict[str, Any]):
        """M5: 对视频源纹理执行时域+空域降噪（渲染线程）。

        质量模式映射：播放中=preview（快），暂停/静态预览=paused（较好），
        显式 export=export。帧号不连续/seek/换素材由 pipeline 内部状态机重置。
        """
        # __new__ 构造的轻量测试桩可能没有该属性（与文件内既有惯例一致）
        pipeline = getattr(self, 'video_denoise_pipeline', None)
        if pipeline is None or not pipeline.is_active():
            return source_tex
        try:
            frame_index = int(getattr(self, 'current_video_frame_index', -1))
        except (TypeError, ValueError, RuntimeError):
            frame_index = -1
        try:
            generation = int(self.__dict__.get('_video_generation_id', 0) or 0)
        except RuntimeError:
            generation = 0
        requested_quality = params.get('video_denoise_quality_mode')
        if requested_quality in ('preview', 'paused', 'export'):
            quality = requested_quality
        elif getattr(self, 'video_denoise_quality_mode', 'preview') == 'export':
            quality = 'export'
        elif bool(getattr(self, 'video_playback_active', False)):
            quality = 'preview'
        else:
            quality = 'paused'
        # P1-COLOR-13: 记录实际档位供 UI 状态显示
        self.last_video_denoise_quality = quality

        # M4.2: 暂停态优先使用邻帧窗口重放（前后帧双向累积，画质优于
        # 因果历史）。窗口由 UI 后台取帧投递，center 帧号必须与当前帧
        # 一致才有效；缺失/不匹配时退化为因果历史（Preview 同款）。
        if quality == 'paused' and frame_index >= 0:
            window = self.__dict__.get('_video_denoise_window')
            if (
                window is not None
                and window.get('center_frame') == frame_index
            ):
                try:
                    textures = self._get_video_denoise_window_textures(window)
                except Exception:
                    logger.exception("video denoise window texture upload failed")
                    textures = []
                if textures:
                    try:
                        result = pipeline.process_window(
                            source_tex, textures, quality_mode='paused',
                        )
                        if result is not None:
                            return result
                    except Exception:
                        logger.exception(
                            "video denoise window replay failed; fallback causal"
                        )

        try:
            return pipeline.process(
                source_tex, frame_index, generation, 'color_video',
                quality_mode=quality,
            )
        except Exception:
            logger.exception("_apply_video_denoise failed; passthrough")
            return source_tex

    def _do_render_video_frame(self, data: Dict[str, Any]) -> None:
        """
        P4.B: Combined upload + render for video playback (S2.A 修改).

        将帧上传和渲染合并为单次操作，减少队列积压。
        支持 generation_id 机制，丢弃过期帧。

        S2.A: 支持 decoded_frame payload，直接上传 DecodedFrame，
        避免 UI 线程 BGR/RGBA 转换。

        Args:
            data: 包含以下键的字典：
                - 'decoded_frame': DecodedFrame 对象（S2.A 新增）
                - 'frame': numpy 数组（BGR uint8）- legacy fallback；
                  float 输入按线性 HDR 原值上传，0..255 尺度的 float
                  生产方必须显式传 'frame_scale'（AUD-0205）
                - 'frame_data': 预处理的 numpy 数组（RGBA float16）- legacy fallback
                - 'frame_index': 帧号
                - 'render_params': 渲染参数（layers, viewport_size, etc.）
                - 'generation_id': seek 后递增的 ID，用于丢弃旧帧
                - 'drop_if_stale': 是否丢弃过期帧（默认 True）
                - 'yuv_matrix': YUV 矩阵类型（用于 planar 格式）
                - 'color_range': 颜色范围（用于 planar 格式）
        """
        if not self.ctx or not data:
            return

        generation_id = data.get('generation_id', 0)
        drop_if_stale = data.get('drop_if_stale', True)
        frame_index = data.get('frame_index', -1)

        # P4.B: Generation ID check - drop stale frames after seek
        if drop_if_stale and generation_id < self._video_generation_id:
            logger.debug(f"Dropped stale video frame: gen={generation_id}, "
                        f"current_gen={self._video_generation_id}, frame={frame_index}")
            return

        # Update generation ID if newer
        if generation_id > self._video_generation_id:
            self._video_generation_id = generation_id

        # 1. Upload frame to GPU texture
        # S2.A: 支持 decoded_frame payload
        decoded_frame = data.get('decoded_frame')

        # P5.3: Track GPU YUV timing
        _yuv_upload_start = time.perf_counter()

        texture = None
        width = 0
        height = 0

        if decoded_frame is not None:
            # S2.A: DecodedFrame 提交路径
            from src.shared.media.frame import DecodedFrame
            if not isinstance(decoded_frame, DecodedFrame):
                logger.warning("RENDER_VIDEO_FRAME: decoded_frame is not DecodedFrame")
                return

            normalized_frame = self._materialize_contiguous_semiplanar_frame(decoded_frame)
            if normalized_frame is not None:
                decoded_frame = normalized_frame

            frame_index = decoded_frame.frame_index
            width = decoded_frame.width
            height = decoded_frame.height

            if width <= 0 or height <= 0:
                logger.warning("RENDER_VIDEO_FRAME: Invalid DecodedFrame dimensions")
                return

            if decoded_frame.is_planar:
                # 平面格式：优先 GPU planes 上传。CPU fallback 只能在
                # renderer 线程内发生，不能退回 UI tick 做 YUV->RGB。
                use_yuv_gpu_upload = bool(data.get('use_yuv_gpu_upload', True))

                # P1-5: Compute the actual upload mode for accurate diagnostics.
                # Must match the conditions in _do_upload_video_planes exactly.
                _actual_use_r16ui = False
                if use_yuv_gpu_upload and decoded_frame.planes:
                    from src.shared.media.frame import get_pixel_format
                    _fmt_info = get_pixel_format(decoded_frame.pixel_format)
                    if _fmt_info is not None:
                        _actual_use_r16ui = (
                            _fmt_info.bit_depth > 8
                            and self._yuv_gpu_uploader.is_r16_available()
                            and 'nv12' not in decoded_frame.pixel_format
                            and 'p010' not in decoded_frame.pixel_format
                            and len(decoded_frame.planes) >= 3
                            and decoded_frame.planes[0].dtype == np.uint16
                        )
                self._debug_frame_path = 'planar_yuv_r16ui_upload' if _actual_use_r16ui else 'planar_yuv_r8_upload'

                if use_yuv_gpu_upload:
                    planes_data = {
                        'decoded_frame': decoded_frame,
                        'frame_index': frame_index,
                        'yuv_matrix': data.get('yuv_matrix', 0),
                        'color_range': data.get('color_range', 0),
                    }
                    planes_result = self._do_upload_video_planes(planes_data)
                    if planes_result is None:
                        logger.warning(
                            "RENDER_VIDEO_FRAME: Failed to upload video planes; "
                            "using renderer-thread CPU fallback"
                        )
                        self._debug_frame_path = 'planar_yuv_r8_upload_gpu_failed'

                        # P1-4: Track fallback in performance monitor
                        from src.shared.media.performance_monitor import get_performance_monitor
                        _perf = get_performance_monitor()
                        _perf.track_zero_copy_frame(
                            decode_path=getattr(self, '_debug_decode_path', 'unknown'),
                            render_frame_path='packed_rgb_cpu_upload',
                            is_zero_copy=False,
                            cpu_upload=True,
                            fallback_reason='gpu_plane_upload_failed',
                        )

                        texture = self._do_upload_planar_video_frame_cpu_fallback(
                            decoded_frame=decoded_frame,
                            frame_index=frame_index,
                            yuv_matrix=int(data.get('yuv_matrix', 0)),
                            color_range=int(data.get('color_range', 0)),
                        )
                        if texture is None:
                            logger.warning("RENDER_VIDEO_FRAME: CPU planar fallback failed")
                            return
                else:
                    texture = self._do_upload_planar_video_frame_cpu_fallback(
                        decoded_frame=decoded_frame,
                        frame_index=frame_index,
                        yuv_matrix=int(data.get('yuv_matrix', 0)),
                        color_range=int(data.get('color_range', 0)),
                    )
                    if texture is None:
                        logger.warning("RENDER_VIDEO_FRAME: CPU planar fallback failed")
                        return
                # GPU path uses current_video_planes; CPU fallback uses texture.
                # Both states are updated by their upload helpers.
                _yuv_upload_ms = (time.perf_counter() - _yuv_upload_start) * 1000.0
                # P5.3: Log timing periodically (every 100 frames)
                if frame_index % 100 == 0:
                    logger.info(f"Video frame upload: frame={frame_index}, "
                              f"path={self._debug_frame_path}, "
                              f"upload_time={_yuv_upload_ms:.1f}ms")
            else:
                # 打包格式：使用 _do_upload_decoded_video_frame
                upload_data = {
                    'decoded_frame': decoded_frame,
                    'frame_index': frame_index,
                }
                texture = self._do_upload_decoded_video_frame(upload_data)
                if texture is None:
                    logger.warning("RENDER_VIDEO_FRAME: Failed to upload decoded video frame")
                    return

        else:
            # Legacy path: frame_data 或 frame numpy
            frame_data = data.get('frame_data')
            width = data.get('width', 0)
            height = data.get('height', 0)

            # If preprocessed frame_data provided, use it directly
            if frame_data is not None and width > 0 and height > 0:
                upload_data = {
                    'frame_data': frame_data,
                    'frame_index': frame_index,
                    'width': width,
                    'height': height,
                    'components': data.get('components', 4),
                    'dtype': data.get('dtype', 'f2')
                }
                texture = self._do_upload_video_frame(upload_data)
            else:
                # Raw frame needs preprocessing (fallback for legacy calls)
                frame = data.get('frame')
                if frame is None:
                    logger.warning("RENDER_VIDEO_FRAME: No frame data provided")
                    return

                # P1-8: Process raw frame with 8-bit fast path
                import cv2
                if frame.ndim == 2:
                    rgba = cv2.cvtColor(frame, cv2.COLOR_GRAY2RGBA)
                elif frame.shape[-1] == 3:
                    rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
                elif frame.shape[-1] == 4:
                    rgba = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGBA)
                else:
                    rgba = frame

                h, w = rgba.shape[:2]

                # P1-8: 8-bit fast path
                if rgba.dtype == np.uint8:
                    frame_data = np.ascontiguousarray(rgba)
                    legacy_dtype = 'f1'
                elif rgba.dtype == np.uint16:
                    frame_data = np.ascontiguousarray((rgba.astype(np.float32) / 65535.0).astype(np.float16))
                    legacy_dtype = 'f2'
                else:
                    # AUD-0205: float 帧承载的是线性 HDR 场景值，旧的
                    # ``max() > 1.0`` 启发式会把合法高光整帧除以 255（画面
                    # 近黑、HDR 数据被无声破坏）。float 输入按原值上传；
                    # 旧的 0..255 float 生产方必须通过 ``frame_scale`` 显式
                    # 声明尺度（如 255.0），整数输入已有精确分支。
                    frame_data = rgba.astype(np.float32)
                    frame_scale = data.get('frame_scale')
                    if frame_scale is not None:
                        frame_data = frame_data / float(frame_scale)
                    frame_data = np.ascontiguousarray(frame_data.astype(np.float16))
                    legacy_dtype = 'f2'

                upload_data = {
                    'frame_data': frame_data,
                    'frame_index': frame_index,
                    'width': w,
                    'height': h,
                    'components': 4,
                    'dtype': legacy_dtype
                }
                texture = self._do_upload_video_frame(upload_data)
                width, height = w, h

            if texture is None:
                logger.warning("RENDER_VIDEO_FRAME: Failed to upload texture")
                return

        # 2. Render using cached render_params
        # AUD-0212: 浅拷贝——``data['render_params']`` 是 UI 线程构造并被调用方
        # 持有的 dict，渲染线程不得就地改写（下方 viewport_size 修正会写入该
        # dict）；拷贝后再修正，避免跨线程写共享对象。
        render_params = dict(data.get('render_params', {}))
        if not render_params:
            # Use default render params if not provided
            render_params = {
                'viewport_size': self.canvas_size if self.canvas_size else (width, height),
                'layers': [],
                'raw_preview': self.raw_preview_params,
                'film_grain': {},
                'visible_rect': (0, 0, float(width), float(height)),
                'show_mask': False,
                'active_layer_index': 0,
            }

        # Ensure viewport size matches video if not specified
        viewport_size = render_params.get('viewport_size', (width, height))
        if viewport_size[0] <= 0 or viewport_size[1] <= 0:
            render_params['viewport_size'] = (width, height)

        # Execute render
        self.debug_last_render_command = "RENDER_VIDEO_FRAME"
        # New frame content: qualifier mattes keyed on this epoch must regenerate
        # (__new__-constructed test stubs skip __init__; __dict__ access avoids
        # the QObject RuntimeError raised by plain attribute reads)
        self._video_matte_epoch = int(self.__dict__.get('_video_matte_epoch', 0) or 0) + 1
        try:
            _diag_vgen = int(self.__dict__.get('_video_generation_id', 0) or 0)
        except RuntimeError:
            _diag_vgen = 0
        _flash_diag.event(
            'video_cmd', cmd='RENDER_VIDEO_FRAME', frame_index=int(frame_index),
            reason=str(data.get('reason')), quality=str(data.get('quality')),
            gen=int(generation_id) if 'generation_id' in data else None,
            vgen=_diag_vgen,
        )
        if data.get('reason') == 'seek':
            # A selected Color timeline clip is a paused, exact preview. The
            # front texture must be complete before it is shared with Canvas;
            # otherwise the UI context can sample its previous FBO contents.
            render_params = dict(render_params)
            render_params['render_reason'] = 'static_preview'
        # A fresh texture has been uploaded; clear the clip-switch pending flag
        # so subsequent RENDER_FRAME commands may reuse this texture again.
        # If this is the first playback frame after a clip switch, the
        # double-buffer slot being written has not been touched since the
        # previous clip, so its GPU contents are stale.  The playback path
        # normally publishes without glFinish() for throughput, but that would
        # let drawBackground blit the still-incomplete (old-clip) texture for
        # one frame.  Force a finish on this one frame so the new pixels land
        # before the front texture is published.
        # Read via __dict__: lightweight tests construct the renderer with
        # __new__ and plain attribute access raises RuntimeError.
        if bool(self.__dict__.get("video_texture_pending", False)):
            self._force_finish_next_publish = True
        self.video_texture_pending = False
        render_params = dict(render_params)
        render_params['stabilization_source_frame'] = int(frame_index)
        self._do_render(render_params, allow_during_playback=True)
        self.debug_last_rendered_video_frame_index = int(frame_index)
        if data.get('reason') == 'seek' or not drop_if_stale:
            logger.debug(
                "[COLOR_CLIP_DEBUG] renderer.video_frame_done frame=%s reason=%s "
                "quality=%s drop_if_stale=%s front_tex=%s front_size=%s "
                "current_video=%s",
                frame_index,
                data.get('reason'),
                data.get('quality'),
                drop_if_stale,
                self.current_front_tex_id,
                self.current_front_size,
                self.current_video_frame_index,
            )

        logger.debug(f"RENDER_VIDEO_FRAME completed: frame={frame_index}, gen={generation_id}")

    def _do_render_video_gpu_frame(self, data: Dict[str, Any]) -> None:
        """
        Phase D: GPU 零拷贝渲染 GpuDecodedFrame。

        直接绑定 external GPU texture planes，不做 Texture.write，
        不做 to_ndarray / transfer，不做 CPU upload/download。

        热路径禁止：
        - Texture.write(video_frame_bytes)
        - frame.to_ndarray() / frame.transfer()
        - ctx.finish() / glFinish()

        Args:
            data: 包含 gpu_decoded_frame 的渲染数据
        """
        gpu_frame: GpuDecodedFrame | None = data.get('gpu_decoded_frame')
        if gpu_frame is None or not is_gpu_decoded_frame(gpu_frame):
            logger.warning("RENDER_VIDEO_GPU_FRAME: no GpuDecodedFrame provided")
            return

        frame_index = data.get('frame_index', gpu_frame.frame_index)
        generation_id = data.get('generation_id', 0)
        render_params = data.get('render_params', {})
        drop_if_stale = data.get('drop_if_stale', True)
        decode_path = data.get('decode_path', gpu_frame.decode_path)
        yuv_matrix = self._coerce_video_yuv_matrix(gpu_frame.color_matrix)
        color_range = self._coerce_video_color_range(gpu_frame.color_range)

        # Generation check: drop stale frames
        if drop_if_stale and generation_id < self._video_generation_id:
            logger.debug(
                f"RENDER_VIDEO_GPU_FRAME: dropping stale frame {frame_index}, "
                f"gen={generation_id} < current={self._video_generation_id}"
            )
            safe_release_gpu_frame(gpu_frame)
            return

        # Update generation if newer
        if generation_id > self._video_generation_id:
            self._video_generation_id = generation_id

        # Track frame path
        self._debug_frame_path = data.get(
            'frame_path',
            RenderFramePath.OPENGL_EXTERNAL_YUV.value  # P3-04 限定值（旧值经别名仍可读）
        )
        self._debug_decode_path = decode_path

        # G-P0-05: don't release the previous GPU frame's backing until the new
        # frame is successfully wrapped and bound. current_video_planes may still
        # reference the previous frame's IOSurface-backed external textures; if we
        # release the old frame first and the new wrap then fails/returns early,
        # the renderer would sample a freed surface. Pin the new frame now and
        # defer the old release to after a successful bind (or restore on failure).
        previous_gpu_frame = getattr(self, '_previous_gpu_frame', None)
        self._previous_gpu_frame = gpu_frame

        # Bind external GPU planes directly
        # External textures from IOSurface/D3D11 are already on GPU
        # No CPU upload/download needed
        _bind_start = time.perf_counter()

        planes = gpu_frame.planes
        if not planes and getattr(gpu_frame, 'deferred_import', '') == 'macos_vt_pixel_buffer':
            # 共享帧服务的解码线程没有 GL 上下文，VT 帧以保留 CVPixelBuffer
            # 的延迟形式送达；在渲染线程（当前线程，上下文已 current）完成
            # IOSurface → 外部纹理绑定。失败则按未绑定处理。
            if not self._import_deferred_vt_frame(gpu_frame):
                logger.warning(
                    f"RENDER_VIDEO_GPU_FRAME: deferred VT import failed "
                    f"frame={frame_index}, falling back"
                )
                if gpu_frame is not previous_gpu_frame:
                    safe_release_gpu_frame(gpu_frame)
                self._previous_gpu_frame = previous_gpu_frame
                return
            planes = gpu_frame.planes
        if not planes or len(planes) == 0:
            logger.warning(
                f"RENDER_VIDEO_GPU_FRAME: no planes in GpuDecodedFrame "
                f"frame={frame_index}"
            )
            # G-P0-05: 没绑定成功，恢复保留前一帧的 backing，避免悬空采样。
            if gpu_frame is not previous_gpu_frame:
                safe_release_gpu_frame(gpu_frame)
            self._previous_gpu_frame = previous_gpu_frame
            return

        # Store external texture info for YUV render
        self._current_gpu_frame_planes = planes
        self._current_gpu_frame_width = gpu_frame.width
        self._current_gpu_frame_height = gpu_frame.height
        self._current_gpu_frame_color_range = gpu_frame.color_range
        self._current_gpu_frame_color_matrix = gpu_frame.color_matrix
        self._current_gpu_frame_bit_depth = gpu_frame.bit_depth
        self._current_gpu_frame_pixel_format = gpu_frame.pixel_format

        # Bind external GPU textures as renderable planes.
        # Wrap external GL texture IDs into ModernGL ExternalTexture objects
        # so the existing YUV→RGB render path can use them directly.
        try:
            wrapped_planes = self._wrap_external_gpu_planes(planes, gpu_frame)
            if wrapped_planes is not None:
                self.current_video_planes = wrapped_planes
                self.current_video_pixel_format = gpu_frame.pixel_format
                self.current_video_yuv_matrix = yuv_matrix
                self.current_video_color_range = color_range
                self.current_video_bit_depth = gpu_frame.bit_depth
                self.current_video_chroma_siting = int(
                    getattr(gpu_frame, 'chroma_siting', 0) or 0
                )
                self.video_source_size = (gpu_frame.width, gpu_frame.height)
                # M5: GPU 路径同步帧号（时域降噪状态机的连续性检查依赖它；
                # 此前只有 CPU 上传路径更新，零拷贝路径会一直 -1 导致历史
                # 永远重置）。
                try:
                    self.current_video_frame_index = int(frame_index)
                except (TypeError, ValueError):
                    pass
                # G-P0-05: 新帧已成功绑定，现在安全释放前一帧的 backing。
                if (
                    previous_gpu_frame is not None
                    and previous_gpu_frame is not gpu_frame
                ):
                    safe_release_gpu_frame(previous_gpu_frame)
            else:
                logger.warning(
                    f"RENDER_VIDEO_GPU_FRAME: failed to wrap external planes "
                    f"frame={frame_index}, falling back"
                )
                # G-P0-05: 绑定失败，恢复保留前一帧 backing。
                if gpu_frame is not previous_gpu_frame:
                    safe_release_gpu_frame(gpu_frame)
                self._previous_gpu_frame = previous_gpu_frame
                return
        except Exception as e:
            logger.error(f"RENDER_VIDEO_GPU_FRAME: error wrapping external planes: {e}")
            # G-P0-05: 异常，恢复保留前一帧 backing。
            if gpu_frame is not previous_gpu_frame:
                safe_release_gpu_frame(gpu_frame)
            self._previous_gpu_frame = previous_gpu_frame
            return

        _bind_ms = (time.perf_counter() - _bind_start) * 1000.0

        logger.debug(
            f"RENDER_VIDEO_GPU_FRAME: bound external planes "
            f"frame={frame_index}, planes={len(planes)}, "
            f"bind_ms={_bind_ms:.2f}, "
            f"path={self._debug_frame_path}"
        )

        # Execute render using the external GPU texture planes
        self.debug_last_render_command = "RENDER_VIDEO_GPU_FRAME"
        # New frame content: qualifier mattes keyed on this epoch must regenerate
        # (__new__-constructed test stubs skip __init__; __dict__ access avoids
        # the QObject RuntimeError raised by plain attribute reads)
        self._video_matte_epoch = int(self.__dict__.get('_video_matte_epoch', 0) or 0) + 1
        try:
            _diag_vgen_gpu = int(self.__dict__.get('_video_generation_id', 0) or 0)
        except RuntimeError:
            _diag_vgen_gpu = 0
        _flash_diag.event(
            'video_cmd', cmd='RENDER_VIDEO_GPU_FRAME', frame_index=int(frame_index),
            reason=str(data.get('reason')), quality=str(data.get('quality')),
            gen=int(generation_id),
            vgen=_diag_vgen_gpu,
        )
        # Mirror _do_render_video_frame: the first playback frame after a clip
        # switch must finish before publish, otherwise the double-buffer slot
        # (untouched since the previous clip) is blitted with stale contents.
        # Read via __dict__: lightweight tests construct the renderer with
        # __new__ and plain attribute access raises RuntimeError.
        if bool(self.__dict__.get("video_texture_pending", False)):
            self._force_finish_next_publish = True
        self.video_texture_pending = False
        render_params = dict(render_params)
        render_params['stabilization_source_frame'] = int(frame_index)
        self._do_render(render_params, allow_during_playback=True)
        self.debug_last_rendered_video_frame_index = int(frame_index)
        if data.get('reason') == 'seek' or not drop_if_stale:
            logger.debug(
                "[COLOR_CLIP_DEBUG] renderer.gpu_frame_done frame=%s reason=%s "
                "quality=%s drop_if_stale=%s front_tex=%s front_size=%s "
                "current_video=%s",
                frame_index,
                data.get('reason'),
                data.get('quality'),
                drop_if_stale,
                self.current_front_tex_id,
                self.current_front_size,
                self.current_video_frame_index,
            )

        logger.debug(
            f"RENDER_VIDEO_GPU_FRAME completed: frame={frame_index}, "
            f"gen={generation_id}, path={self._debug_frame_path}"
        )

    def _import_deferred_vt_frame(self, gpu_frame: GpuDecodedFrame) -> bool:
        """在渲染线程把"延迟导入"的 VT pixel buffer 绑定为外部纹理。

        输入帧由 MacOSVtZeroCopySource.read_frame_gpu_deferred 产出：
        planes 为空、deferred_vt_frame 保留 CVPixelBuffer。绑定成功后
        覆写 planes 与 release（先释放桥接纹理租约，再释放像素缓冲），
        与剪辑页 EditGpuPreviewRenderer 的导入方式一致。
        """
        vt_frame = getattr(gpu_frame, 'deferred_vt_frame', None)
        pixel_buffer = getattr(vt_frame, 'pixel_buffer', None)
        if pixel_buffer is None:
            logger.warning("_import_deferred_vt_frame: missing pixel buffer")
            return False

        if self.__dict__.get('_video_vt_bridge_broken', False):
            return False

        bridge = self.__dict__.get('_video_vt_bridge', None)
        if bridge is None:
            try:
                from src.shared.media.macos_vt.iosurface_gl_bridge import IOSurfaceGLBridge

                bridge = IOSurfaceGLBridge(ring_size=4)
                if not bridge.initialize(planes_per_frame=2):
                    self.__dict__['_video_vt_bridge_broken'] = True
                    logger.warning("_import_deferred_vt_frame: bridge init failed")
                    return False
                self.__dict__['_video_vt_bridge'] = bridge
            except Exception as e:
                self.__dict__['_video_vt_bridge_broken'] = True
                logger.warning(f"_import_deferred_vt_frame: bridge unavailable: {e}")
                return False

        try:
            result = bridge.bind_pixel_buffer(
                pixel_buffer=pixel_buffer,
                frame_index=vt_frame.frame_index,
                pixel_format=vt_frame.pixel_format,
                bit_depth=vt_frame.bit_depth,
                decode_path=DecodePath.MACOS_VT_IOSURFACE_ZERO_COPY.value,
            )
        except Exception as e:
            logger.warning(f"_import_deferred_vt_frame: bind failed: {e}")
            return False

        if result is None:
            return False

        gpu_planes, gpu_release = result
        original_release = gpu_frame.release

        def _release() -> None:
            try:
                gpu_release.release()
            finally:
                original_release.release()

        gpu_frame.planes = gpu_planes
        gpu_frame.release = GpuFrameRelease(release_callback=_release, retain_token=vt_frame)
        return True

    @staticmethod
    def _coerce_video_yuv_matrix(value: Any) -> int:
        """Convert video color matrix metadata to YUVGPUUploader uniform codes."""
        if isinstance(value, int):
            return value if value in (0, 1, 2) else 0
        if hasattr(value, 'value'):
            value = value.value
        name = str(value or 'bt709').lower().strip()
        matrix_map = {
            'bt709': 0,
            'bt.709': 0,
            'smpte170m': 2,
            'bt470bg': 2,
            'bt601': 2,
            'bt.601': 2,
            'bt2020nc': 1,
            'bt2020': 1,
            'bt.2020': 1,
        }
        return matrix_map.get(name, 0)

    @staticmethod
    def _coerce_video_color_range(value: Any) -> int:
        """Convert video range metadata to YUVGPUUploader uniform codes."""
        if isinstance(value, int):
            return value if value in (0, 1) else 0
        if hasattr(value, 'value'):
            value = value.value
        name = str(value or 'limited').lower().strip()
        range_map = {
            'limited': 0,
            'tv': 0,
            'mpeg': 0,
            'full': 1,
            'pc': 1,
            'jpeg': 1,
        }
        return range_map.get(name, 0)

    def _wrap_external_gpu_planes(
        self,
        planes: list,
        gpu_frame: 'GpuDecodedFrame',
    ) -> Optional[tuple]:
        """
        Phase D: Wrap external GPU texture IDs into ExternalGLTexture
        objects so the existing YUV→RGB render path can use them.

        The external textures come from IOSurface/D3D11 interop and are
        already resident on the GPU. We wrap their GL texture IDs into
        ExternalGLTexture objects that provide .glo, .size, .use().

        Args:
            planes: List of GpuFramePlane from GpuDecodedFrame
            gpu_frame: The GpuDecodedFrame containing metadata

        Returns:
            Tuple of ExternalGLTexture objects (y, u, v) or (y, uv),
            or None on failure.
        """
        from src.shared.media.gpu_frame import GpuFramePlane
        from src.shared.media.macos_vt.external_texture import ExternalGLTexture

        from OpenGL.GL import GL_TEXTURE_2D, GL_TEXTURE_RECTANGLE

        wrapped = []
        for plane in planes:
            if not isinstance(plane, GpuFramePlane):
                logger.warning(f"_wrap_external_gpu_planes: unexpected plane type {type(plane)}")
                return None

            if plane.texture_id == 0:
                logger.warning("_wrap_external_gpu_planes: plane has texture_id=0")
                return None

            target = GL_TEXTURE_2D
            if plane.target == 'rectangle':
                target = GL_TEXTURE_RECTANGLE
            elif plane.target == 'external':
                target = 36197  # GL_TEXTURE_EXTERNAL_OES

            wrapped.append(ExternalGLTexture(
                texture_id=plane.texture_id,
                target=target,
                size=(plane.width, plane.height),
            ))

        if len(wrapped) == 2:
            return (wrapped[0], wrapped[1])
        elif len(wrapped) == 3:
            return (wrapped[0], wrapped[1], wrapped[2])
        elif len(wrapped) >= 4:
            # YUVA 外部 GPU 帧：保留第四平面（Alpha），渲染层按 4 元组消费
            return (wrapped[0], wrapped[1], wrapped[2], wrapped[3])
        else:
            logger.warning(f"_wrap_external_gpu_planes: unexpected plane count {len(wrapped)}")
            return None

    def get_video_texture_pool_stats(self) -> Dict[str, Any]:
        """
        P4.2: Get video texture pool statistics.
        
        用于验证 GPU 内存稳定性。
        
        Returns:
            包含池统计信息的字典
        """
        if self._video_texture_pool:
            return self._video_texture_pool.get_stats()
        return {'pool_size': 0, 'max_size': 0, 'active_frames': [], 'sizes': []}

    def get_renderer_stats(self) -> Dict[str, Any]:
        """
        S5.A: Get renderer statistics including dropped preview frames.

        S5.B: 新增 coalesced_video_commands 计数.
        P1-1: 新增 coalesced_lane_commands 计数.

        用于监控渲染队列状态和丢帧统计。

        Returns:
            包含渲染器统计信息的字典
        """
        stats = self._renderer_stats.copy()
        # P1-1 fix: with sentinel deduplication in submit(), _drain_pending sentinels
        # no longer pile up in the queue. Queue depth now reflects real pending commands.
        stats['queue_depth'] = self.command_queue.qsize()
        # S5.B: 同步 coalesced_video_commands 到 stats
        stats['coalesced_video_commands'] = self._coalesced_video_commands
        # P1-1: lane-based merge stats
        stats['coalesced_lane_commands'] = self._coalesced_lane_commands
        return stats

    def reset_renderer_stats(self) -> None:
        """
        S5.B: Reset renderer statistics counters.
        P1-1: Also resets lane-based merge stats.

        在性能监控 session 开始时调用，确保统计数据准确。
        """
        self._dropped_preview_frames = 0
        self._coalesced_video_commands = 0
        self._coalesced_lane_commands = 0
        self._renderer_stats['dropped_preview_frames'] = 0
        self._renderer_stats['processed_frames'] = 0
        self._renderer_stats['coalesced_video_commands'] = 0
        self._renderer_stats['coalesced_lane_commands'] = 0

    def get_lane_states(self) -> Dict[str, bool]:
        """
        P2-3: Get current pending state for each lane.

        Returns a dictionary mapping lane name to whether it has a pending command.
        Useful for monitoring and debugging queue merging behavior.

        Returns:
            Dict with keys: 'video_playback', 'viewport_refresh', 'grading_refresh',
                           'aux_tex', 'color_output', each with True/False value.
        """
        with self._pending_lock:
            return {
                lane.value: self._pending_slots.get(lane) is not None
                for lane in RenderLane
            }

    def _ensure_fbos(self, w: int, h: int) -> None:
        """确保视口双缓冲 FBO 存在且为 (w, h)，否则整体重建（渲染线程）。

        旧 FBO/纹理进入 garbage 队列延迟释放（约 2 秒宽限，供 UI 侧消费
        完旧帧）；新纹理保持 LINEAR 过滤——它们是动态渲染的 1:1 视口
        大小，使用 mipmap 过滤而不 ``build_mipmaps()`` 会采样出黑色。
        """
        if self.fbo_pair[0] and self.fbo_pair[0].size == (w, h): return
        for i in range(2):
            if self.fbo_pair[i]: self.garbage.append((time.time(), self.fbo_pair[i], self.tex_pair[i]))
            tex = self.ctx.texture((w, h), 4, dtype='f2')
            # FBO 纹理保持 LINEAR 过滤，因为它们是动态渲染的 1:1 视口大小
            # 使用 MIPMAP 过滤而没有 build_mipmaps() 会导致采样返回黑色
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            self.fbo_pair[i] = self.ctx.framebuffer(color_attachments=[tex])
            self.tex_pair[i] = tex
        # P3-03: present 双缓冲登记（懒创建点；重建（尺寸变化）时覆盖登记）。
        backend = getattr(self, '_gpu_backend', None)
        if backend is not None:
            backend.registry.register(GROUP_PRESENT, "fbo_pair", self.fbo_pair)
        self._process_garbage()

    def _ensure_color_output_fbo(
        self,
        w: int,
        h: int,
        output_target: Optional[str] = None,
    ) -> Tuple[Any, Any]:
        """Create or resize the persistent target and return ``(tex, fbo)``."""
        is_scope = output_target == 'scope'
        is_full_res = output_target == 'full_res'
        if is_scope:
            tex_attr = '_scope_color_output_tex'
            fbo_attr = '_scope_color_output_fbo'
            dtype_attr = '_scope_color_output_dtype'
        elif is_full_res:
            # Full-resolution export must not resize/release the live
            # ColorOutput texture consumed by Composite/monitor consumers.
            tex_attr = '_full_res_color_output_tex'
            fbo_attr = '_full_res_color_output_fbo'
            dtype_attr = '_full_res_color_output_dtype'
        else:
            tex_attr = '_color_output_tex'
            fbo_attr = '_color_output_fbo'
            dtype_attr = '_color_output_dtype'
        requested_dtype = 'f4' if output_target == 'full_res' else 'f2'
        tex = getattr(self, tex_attr, None)
        fbo = getattr(self, fbo_attr, None)
        if (
            tex is not None
            and fbo is not None
            and tex.size == (w, h)
            and getattr(self, dtype_attr, 'f2') == requested_dtype
        ):
            return tex, fbo

        # Build the replacement completely before touching the live pair.  A
        # driver allocation failure must leave the previous external/scope
        # target valid, and a half-created texture must not leak.
        new_tex = None
        new_fbo = None
        try:
            new_tex = self.ctx.texture((w, h), 4, dtype=requested_dtype)
            new_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            new_fbo = self.ctx.framebuffer(color_attachments=[new_tex])
        except Exception:
            if new_fbo is not None:
                try:
                    new_fbo.release()
                except Exception:
                    logger.debug(
                        "Failed to release partially-created %s ColorOutput FBO",
                        output_target,
                    )
            if new_tex is not None:
                try:
                    new_tex.release()
                except Exception:
                    logger.debug(
                        "Failed to release partially-created %s ColorOutput texture",
                        output_target,
                    )
            raise

        setattr(self, tex_attr, new_tex)
        setattr(self, fbo_attr, new_fbo)
        setattr(self, dtype_attr, requested_dtype)
        if is_scope:
            # A resized scope target cannot satisfy its previous content or
            # quality-tier cache key, even if the following render returns early.
            self._scope_color_output_revision = -1
            self._scope_color_output_max_dim = 0

        # AUD-0201: the superseded ColorOutput texture may still be sampled
        # by a cross-context consumer (DeckLink monitor / Composite) that
        # holds the published glo from ``color_output_ready`` /
        # ``monitor_color_output_ready``.  Releasing it synchronously here
        # can delete the texture name underneath an in-flight consumer blit.
        # Route the old pair through the same deferred-release garbage queue
        # (2s grace) as the front-buffer path in ``_ensure_fbos``.  The scope
        # target is consumed in this context immediately and the full-res
        # path drains its PBO fences before a size change (see
        # ``_synchronize_full_res_pbo_for_resize``), so the grace period is a
        # strict safety margin for them.
        if fbo is not None and tex is not None:
            self.garbage.append((time.time(), fbo, tex))
        return new_tex, new_fbo

    def _acquire_color_output_temp_texture(
        self,
        cleanup: ExitStack,
        w: int,
        h: int,
        dtype: str = 'f2',
    ) -> Any:
        """Lease a ColorOutput temp and return it on every exit path."""
        tex = self.fb_manager.get_temp_texture(
            w, h, components=4, dtype=dtype
        )
        cleanup.callback(self.fb_manager.release_temp_texture, tex)
        return tex

    @staticmethod
    def _color_output_requires_immediate_finish(
        output_target: Optional[str],
    ) -> bool:
        """Whether a ColorOutput consumer needs completion before return.

        Scope analysis consumes its ColorOutput immediately in the same GL
        context and performs one final finish before publishing its textures.
        Full-resolution exports enqueue a PBO readback/fence on this context;
        forcing a finish here would erase the overlap that PBOs provide. The
        synchronous fallback still waits safely when PBOs are unavailable.
        Composite and monitor consumers cross a render-pass/context boundary,
        so their existing immediate completion contract remains unchanged.
        """
        return output_target not in ('scope', 'full_res')

    def _do_render_color_output(self, params: Dict[str, Any]) -> bool:
        """Render the full Color pipeline to the ColorOutput texture.

        Called when entering the Composite page or when the ColorOutput
        is dirty. Renders at proxy resolution with full-image visible_rect,
        independent of Canvas viewport zoom/pan state.
        """
        # P4.2 / S3.A: Check for any available source texture
        has_video_planes = self.current_video_planes is not None
        has_video_texture = self.current_video_texture is not None
        has_image = bool(self.pyramid_textures)

        if not has_image and not has_video_texture and not has_video_planes:
            return False

        from src.features.color.core.color_output import ColorOutput

        # S3.A / P4.2: Get dimensions from appropriate source
        if has_video_planes:
            img_w, img_h = self.video_source_size
        elif has_video_texture:
            img_w, img_h = self.current_video_texture.size
        else:
            img_w, img_h = self.pyramid_textures[0].size
        if img_w <= 0 or img_h <= 0:
            return False

        render_params = dict(params)
        max_dim = render_params.get('max_proxy_dim', ColorOutput.PROXY_MAX_DIM)
        target_w, target_h = ColorOutput.compute_proxy_size(img_w, img_h, max_dim)

        # Override params for ColorOutput mode
        render_params['viewport_size'] = (target_w, target_h)
        render_params['visible_rect'] = (0, 0, img_w, img_h)
        render_params['grain_visible_rect'] = (0, 0, img_w, img_h)
        render_params['show_mask'] = False
        render_params['color_output_mode'] = True

        return self._do_render(render_params, allow_during_playback=True)

    @staticmethod
    def _snapshot_scope_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-freeze a front-publish params payload for later scope reuse.

        AUD-0204: ``_last_scope_render_params`` previously held a shallow
        ``dict(params)`` whose ``layers`` list and nested grade DTOs were
        still shared with the UI-side payload.  The GPU scope lane is low
        priority and may execute ``_do_render_color_output`` long after the
        publishing frame; if the UI thread mutates those nested structures
        in place (parameter drag), the render thread would read them without
        any isolation.  This snapshot recursively copies the mutable
        containers (dict / list / tuple / set / small ndarray such as
        curves LUTs and warper grids); immutable scalars, strings, bytes,
        and opaque objects pass through untouched.
        """
        def _copy(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: _copy(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_copy(v) for v in value]
            if isinstance(value, tuple):
                return tuple(_copy(v) for v in value)
            if isinstance(value, set):
                return {_copy(v) for v in value}
            if isinstance(value, np.ndarray):
                return value.copy()
            return value

        return {k: _copy(v) for k, v in params.items()}

    def _process_garbage(self, force: bool = False) -> None:
        """回收延迟释放队列中超过宽限期（或 force 时全部）的 FBO/纹理。

        宽限期（约 2 秒）保证刚被替换的 front 纹理仍可被 UI 共享上下文
        采样完；单个资源释放失败只记 debug 日志。
        """
        now, remaining = time.time(), []
        for ts, fbo, tex in self.garbage:
            if force or (now - ts > 2.0):
                try: fbo.release(); tex.release()
                except Exception: logger.debug("Failed to release FBO/texture in garbage collection")
            else: remaining.append((ts, fbo, tex))
        self.garbage = remaining

    def _release_queued_gpu_frames(self) -> None:
        """Release GPU frames abandoned while this render thread is stopping.

        ``GpuDecodedFrame`` owns a retained external decode surface.  Normal
        lane coalescing releases a replaced frame, but a stop can bypass the
        command loop with one frame still in a lane and others still in the
        direct queue.  This method runs from ``_cleanup`` on the render thread,
        clears those unexecuted commands, balances ``Queue.task_done()``, and
        releases each retained surface exactly once.
        """
        queued_frames: list[GpuDecodedFrame] = []

        pending_lock = getattr(self, '_pending_lock', None)
        pending_slots = getattr(self, '_pending_slots', None)
        if pending_lock is not None and pending_slots is not None:
            with pending_lock:
                for lane, entry in pending_slots.items():
                    pending_slots[lane] = None
                    if entry is None:
                        continue
                    cmd_type, data = entry
                    if cmd_type == RenderCommand.RENDER_VIDEO_GPU_FRAME:
                        frame = (data or {}).get('gpu_decoded_frame')
                        if is_gpu_decoded_frame(frame):
                            queued_frames.append(frame)

        drain_lock = getattr(self, '_drain_pending_signal_lock', None)
        if drain_lock is not None:
            with drain_lock:
                self._drain_pending_signaled = False

        command_queue = getattr(self, 'command_queue', None)
        if command_queue is None:
            return
        while True:
            try:
                cmd_type, data = command_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if cmd_type == RenderCommand.RENDER_VIDEO_GPU_FRAME:
                    frame = (data or {}).get('gpu_decoded_frame')
                    if is_gpu_decoded_frame(frame):
                        queued_frames.append(frame)
            finally:
                command_queue.task_done()

        for frame in queued_frames:
            safe_release_gpu_frame(frame)

    def _register_core_gpu_resources(self) -> None:
        """P3-03: 把 run() 初始化阶段创建的 GPU 资源登记到 backend 台账。

        纯台账模式（release_fn=None）：释放仍由 ``_cleanup`` 的既有分段
        路径执行，登记只服务于诊断报告与 Phase 4 backend 切换的 quiesce
        前置视图（计划 §4.2）。scope/present 等懒创建资源在各自的
        ``_ensure_*`` 点登记。
        """
        registry = self._gpu_backend.registry
        registry.register(GROUP_FRAMEBUFFERS, "fb_manager", self.fb_manager)
        registry.register(GROUP_SHADERS, "utility_shaders", self.utility_shaders)
        registry.register(GROUP_UPLOAD, "video_texture_pool", self._video_texture_pool)
        registry.register(GROUP_UPLOAD, "yuv_gpu_uploader", self._yuv_gpu_uploader)
        pipeline = self.__dict__.get('video_denoise_pipeline', None)
        if pipeline is not None:
            registry.register(GROUP_DENOISE, "video_denoise_pipeline", pipeline)
        # 调色默认纹理（identity LUT / raw LUT / white / curves 等）。
        for tex_name in (
            "identity_lut",
            "raw_lut_tex",
            "default_curves_tex",
            "default_hsl_curve_tex",
            "default_warper_tex",
            "white_tex",
        ):
            tex = self.__dict__.get(tex_name, None)
            if tex is not None:
                registry.register(GROUP_GRADING, tex_name, tex)

    def get_gpu_backend_report(self) -> Dict[str, Any]:
        """P3-02: GL backend 台账诊断（backend 状态 + 组 → 资源名）。"""
        backend = self.__dict__.get('_gpu_backend', None)
        if backend is None:
            return {
                "backend_id": "opengl",
                "graphics_api": "opengl",
                "state": "uninitialized",
                "owner": "async_renderer",
                "groups": {},
            }
        return backend.resource_report()

    def _cleanup(self) -> None:
        """渲染线程退出路径：释放全部 GL 资源并解除上下文绑定。

        仅由 ``run()`` 在命令循环结束后调用（本线程仍持有上下文）。
        顺序上先释放队列中残留的 GPU 零拷贝帧与 VT 桥接环，再按依赖序
        分段释放：matte / 层纹理 / 池与 uploader / composition pipeline
        与工具 shader / 持久化纹理 / scope / ColorOutput（含 PBO）/ 双
        缓冲与金字塔，最后 ``doneCurrent`` 并清空 backend 台账。每段
        释放单独吞异常，保证后续段照常执行。
        """
        # Run this before the GL teardown while the render thread still owns
        # the context.  A queued zero-copy frame has no other release path once
        # ``run`` exits its command loop.
        self._release_queued_gpu_frames()
        if self.ctx:
            runner = self.__dict__.pop('_stabilization_runner', None)
            if runner is not None:
                runner.release()
            # 释放 VT 零拷贝桥接的 GL 纹理环（本线程持有上下文）
            vt_bridge = self.__dict__.get('_video_vt_bridge', None)
            if vt_bridge is not None:
                try:
                    vt_bridge.destroy()
                except Exception:
                    logger.debug("Failed to destroy VT bridge in cleanup")
                self.__dict__.pop('_video_vt_bridge', None)
            self._retire_front_present_fence()
            # M5: 释放视频降噪管线（着色器/表面缓存/quad）与暂停态邻帧窗口纹理
            pipeline = self.__dict__.get('video_denoise_pipeline', None)
            if pipeline is not None:
                try:
                    pipeline.release()
                except Exception:
                    logger.debug("Failed to release video denoise pipeline")
                self.video_denoise_pipeline = None
            try:
                self._release_video_denoise_window_textures()
            except Exception:
                logger.debug("Failed to release video denoise window textures")
            self.__dict__.pop('_video_denoise_window', None)
            for item in self.matte_cache.values():
                try: item['matte'].release()
                except Exception: logger.debug("Failed to release matte texture in cleanup")
            self.matte_cache.clear()
            self._matte_viewport_textures.clear()

            # Release layer auxiliary textures (curves, warper, lut3d, mask, hsl_curve)
            for tex_dict in self.layer_textures.values():
                for tex in tex_dict.values():
                    try: tex.release()
                    except Exception: logger.debug("Failed to release layer texture in cleanup")
            self.layer_textures.clear()

            # Release baked layer textures
            for tex in self.baked_textures.values():
                try: tex.release()
                except Exception: logger.debug("Failed to release baked texture in cleanup")
            self.baked_textures.clear()

            # Release AI plugin result textures
            for tex in self.ai_plugin_textures.values():
                try: tex.release()
                except Exception: logger.debug("Failed to release AI plugin texture in cleanup")
            self.ai_plugin_textures.clear()

            # P3: Release YUV RGB output cache textures
            for size_key, tex_list in self._yuv_rgb_output_cache.items():
                for tex in tex_list:
                    try: tex.release()
                    except Exception: logger.debug("Failed to release YUV RGB cache texture in cleanup")
            self._yuv_rgb_output_cache.clear()
            self._yuv_rgb_output_ring_idx.clear()
            self._yuv_rgb_output_size_order.clear()

            # P1-5: Release cached YUV conversion FBO
            if self._yuv_conv_fbo is not None:
                try:
                    self._yuv_conv_fbo.release()
                except Exception:
                    logger.debug("Failed to release YUV conv FBO in cleanup")
                self._yuv_conv_fbo = None
                self._yuv_conv_fbo_tex_glo = 0

            # P1-6: Release all cached quad VAOs
            self._release_quad_vao_cache()

            # Pools and uploader own their own textures, PBOs, shaders and
            # VAOs.  They outlive individual video frames, so they must be
            # released at renderer shutdown as well as at clip changes.
            video_texture_pool = getattr(self, '_video_texture_pool', None)
            if video_texture_pool is not None:
                try:
                    video_texture_pool.release_all()
                except Exception:
                    logger.debug("Failed to release video texture pool in cleanup")
                self._video_texture_pool = None
            yuv_gpu_uploader = getattr(self, '_yuv_gpu_uploader', None)
            if yuv_gpu_uploader is not None:
                try:
                    yuv_gpu_uploader.release()
                except Exception:
                    logger.debug("Failed to release YUV GPU uploader in cleanup")
                self._yuv_gpu_uploader = None
            self.current_video_texture = None
            self.current_video_planes = None

            # The composition cache owns one GL program per structural layer
            # configuration.  Release it after renderer VAOs that reference
            # those programs have been retired.
            pipeline = getattr(self, 'pipeline', None)
            if pipeline is not None:
                try:
                    pipeline.release()
                except Exception:
                    logger.debug("Failed to release shader pipeline in cleanup")
                self.pipeline = None

            # Fixed-function programs and default textures are created once in
            # ``run`` and otherwise never enter a framebuffer/pool owner.
            for program in self.utility_shaders.values():
                try:
                    program.release()
                except Exception:
                    logger.debug("Failed to release utility shader in cleanup")
            self.utility_shaders.clear()
            for attr_name in (
                'raw_lut_tex',
                'identity_lut',
                'default_curves_tex',
                'default_hsl_curve_tex',
                'default_warper_tex',
                'white_tex',
                'quad_vbo',
                'video_quad_vbo',
            ):
                resource = getattr(self, attr_name, None)
                if resource is None:
                    continue
                try:
                    resource.release()
                except Exception:
                    logger.debug("Failed to release %s in cleanup", attr_name)
                setattr(self, attr_name, None)

            # P1-7: Release persistent tile VBO/VAO
            if self._tile_vao:
                try: self._tile_vao.release()
                except Exception: logger.debug("Failed to release tile VAO in cleanup")
            if self._tile_vbo:
                try: self._tile_vbo.release()
                except Exception: logger.debug("Failed to release tile VBO in cleanup")
            self._tile_vao = None
            self._tile_vbo = None
            self._tile_prog_id = None

            # P4: Release persistent raw_view/ping/pong textures
            for tex in [self._persistent_raw_view_tex, self._persistent_ping_tex, self._persistent_pong_tex]:
                if tex:
                    try: tex.release()
                    except Exception: logger.debug("Failed to release persistent texture in cleanup")
            for fbo in [self._persistent_raw_view_fbo, self._persistent_ping_fbo, self._persistent_pong_fbo]:
                if fbo:
                    try: fbo.release()
                    except Exception: logger.debug("Failed to release persistent FBO in cleanup")
            self._persistent_raw_view_tex = None
            self._persistent_raw_view_fbo = None
            self._persistent_ping_tex = None
            self._persistent_ping_fbo = None
            self._persistent_pong_tex = None
            self._persistent_pong_fbo = None
            self._persistent_tex_size = (0, 0)

            if self._scope_snapshot_fbo is not None:
                try:
                    self._scope_snapshot_fbo.release()
                except Exception:
                    logger.debug("Failed to release scope snapshot FBO in cleanup")
            if self._scope_snapshot_tex is not None:
                try:
                    self._scope_snapshot_tex.release()
                except Exception:
                    logger.debug("Failed to release scope snapshot texture in cleanup")
            self._scope_snapshot_fbo = None
            self._scope_snapshot_tex = None
            self._scope_snapshot_size = (0, 0)

            # Release native GPU scope resource pool.
            try:
                self._release_scope_points_vao_cache()
                self._release_gpu_scope_pool()
            except Exception:
                logger.debug("Failed to release GPU scope pool in cleanup")

            if self.fb_manager: self.fb_manager.cleanup()
            self._process_garbage(force=True)

            # Phase D: Release previous GPU zero-copy frame
            if hasattr(self, '_previous_gpu_frame') and self._previous_gpu_frame is not None:
                safe_release_gpu_frame(self._previous_gpu_frame)
                self._previous_gpu_frame = None
            # Release ColorOutput texture
            if self._color_output_tex:
                try: self._color_output_tex.release()
                except Exception: logger.debug("Failed to release ColorOutput texture in cleanup")
            if self._color_output_fbo:
                try: self._color_output_fbo.release()
                except Exception: logger.debug("Failed to release ColorOutput FBO in cleanup")
            self._drain_full_resolution_readbacks_for_shutdown()
            if self._full_res_color_output_fbo:
                try: self._full_res_color_output_fbo.release()
                except Exception:
                    logger.debug("Failed to release full-resolution ColorOutput FBO in cleanup")
            if self._full_res_color_output_tex:
                try: self._full_res_color_output_tex.release()
                except Exception:
                    logger.debug("Failed to release full-resolution ColorOutput texture in cleanup")
            if self._full_res_pbo_readback is not None:
                try:
                    self._full_res_pbo_readback.cleanup()
                except Exception:
                    logger.debug("Failed to release full-resolution PBO readback", exc_info=True)
            self._full_res_pbo_readback = None
            self._full_res_pending_readbacks.clear()
            if self._scope_color_output_fbo:
                try:
                    self._scope_color_output_fbo.release()
                except Exception:
                    logger.debug("Failed to release scope ColorOutput FBO in cleanup")
            if self._scope_color_output_tex:
                try:
                    self._scope_color_output_tex.release()
                except Exception:
                    logger.debug("Failed to release scope ColorOutput texture in cleanup")
            self._scope_color_output_fbo = None
            self._scope_color_output_tex = None
            self._full_res_color_output_fbo = None
            self._full_res_color_output_tex = None
            self._scope_color_output_revision = -1
            self._scope_color_output_max_dim = 0
            for fbo in self.fbo_pair:
                if fbo: fbo.release()
            for tex in self.tex_pair:
                if tex: tex.release()
            for tex in self.pyramid_textures:
                if tex: tex.release()
            for tex in self.reference_pyramid_textures:
                if tex: tex.release()
        if self.shared_pyqt_context: self.shared_pyqt_context.doneCurrent()
        # P3-02: 真实资源已由上方分段路径释放，facade 只清台账并标记
        # RELEASED（台账为纯记录模式，release_all 不触发任何 GL 调用）。
        backend = self.__dict__.get('_gpu_backend', None)
        if backend is not None:
            backend.release()

    def _do_clear_reference(self):
        """Release the reference pyramid; split view falls back to the
        ungraded original texture (P1-COLOR-05 clear reference)."""
        if not self.ctx:
            return
        for tex in self.reference_pyramid_textures:
            try:
                tex.release()
            except Exception:
                logger.debug("Failed to release reference texture")
        self.reference_pyramid_textures = []

    def get_original_texture_id(self, lod: int = 0) -> Optional[int]:
        """取原始（未调色）金字塔纹理的 GL 名称（UI 线程可调）。

        优先返回参考（wipe 对照）金字塔，无参考时回退当前图像金字塔；
        ``lod`` 越界时钳制到有效范围。

        Args:
            lod: 金字塔层级。

        Returns:
            纹理 glo（int）；无任何已上传纹理时为 None。返回的是裸纹理
            id，调用方需保证在共享上下文中使用时纹理仍存活。
        """
        target_list = self.reference_pyramid_textures if self.reference_pyramid_textures else self.pyramid_textures
        if not target_list: return None
        return target_list[max(0, min(lod, len(target_list) - 1))].glo

    def _lookup_layer_ai_texture(self, layer):
        """解析层最后一个启用 AI 插件的 GPU 结果纹理（无则 None）。

        预览合成与 bake 前缀渲染共用此语义（P0-COL-03：AI 输出是该层的
        真实 source，而不仅是 matte 采样源）。
        """
        is_dict = isinstance(layer, dict)
        l_id = layer.get('id') if is_dict else getattr(layer, 'id', None)
        plugins = (layer.get('plugins') if is_dict
                   else getattr(layer, 'plugins', None)) or []
        ai_plugins = [p for p in plugins if isinstance(p, dict)
                      and p.get('type') == 'ai' and p.get('enabled', True)]
        if not (ai_plugins and l_id):
            return None
        return self.ai_plugin_textures.get(
            (l_id, ai_plugins[-1].get('instance_id')))

    def _do_bake_layer(self, data):
        """
        Bake the full layer-stack prefix to a high-precision FBO and read back.

        P0-COL-04: the preview loop consumes a baked texture as a *stack
        prefix replacement* (everything up to and including that layer), so
        the bake must render the whole prefix ``layers[0..n]`` — not just the
        single layer. Layers below the target would otherwise silently drop
        out of the image once the layer is frozen.

        Args:
            data: dict with keys:
                - 'layer': layer dict or object (the bake target)
                - 'layer_id': unique layer identifier (str)
                - 'stack_prefix': full DTO list layers[0..n] (optional for
                  backward compatibility; falls back to [layer])
                - 'source_size': (width, height) of source image
        """
        if not self.ctx or not data:
            return
        if self._cancel_requested():
            return

        layer = data.get('layer')
        layer_id = data.get('layer_id')
        source_size = data.get('source_size', (0, 0))
        full_res_image = data.get('full_res_image')  # Full-resolution image data
        # AUD-0200：诊断关联 ID（仅用于 build_composition_shader 的日志上下文，
        # 不参与缓存键）。BAKE_LAYER 负载当前不携带该键 → None，与
        # _do_render_impl 的提取口径一致，修复引用未定义变量的 NameError。
        render_request_id = data.get('render_request_id')
        # P0-COL-04：完整栈前缀；旧格式（无 stack_prefix）回退单层
        prefix_layers = data.get('stack_prefix') or [layer]

        if not layer or not layer_id:
            return

        # CRITICAL: Get source image dimensions from original_size parameter
        # DO NOT use pyramid_textures[0].size - that's the preview resolution (max 4000px)
        # We must bake at FULL resolution to preserve quality
        if source_size and source_size[0] > 0 and source_size[1] > 0:
            img_w, img_h = source_size
        else:
            # Fallback: use pyramid texture size (not ideal, but prevents crash)
            if self.pyramid_textures:
                img_w, img_h = self.pyramid_textures[0].size
            else:
                logger.warning(": No source size available for baking")
                return

        # Determine source texture for rendering
        # CRITICAL: If full_res_image is provided, create a temporary full-size texture
        # Otherwise, fall back to pyramid_textures[0] (preview resolution - lower quality)
        source_tex = None
        temp_full_tex = None  # Track temporary texture for cleanup

        if full_res_image is not None:
            # Create temporary full-resolution texture for high-quality baking
            h, w = full_res_image.shape[:2]
            try:
                temp_full_tex = self.ctx.texture((w, h), 4, data=full_res_image.astype(np.float16).tobytes(), dtype='f2')
                temp_full_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                temp_full_tex.repeat_x = temp_full_tex.repeat_y = False
                source_tex = temp_full_tex
                logger.info(f"at full resolution: {w}x{h}")
            except Exception as e:
                logger.error(f": Failed to create full-res texture, using preview: {e}")
                temp_full_tex = None

        # Fallback to preview texture if full-res texture creation failed
        if source_tex is None and self.pyramid_textures:
            source_tex = self.pyramid_textures[0]
            tex_w, tex_h = source_tex.size
            if (tex_w, tex_h) != (img_w, img_h):
                logger.warning(f": Baking at preview resolution {tex_w}x{tex_h} instead of full {img_w}x{img_h}")

        if source_tex is None:
            logger.warning(": No source texture available for baking")
            return

        # Create a high-precision FBO for baking (float32)
        # CRITICAL: Use try...finally to ensure VRAM resources are always released
        bake_tex = None
        bake_fbo = None
        scratch_tex = None
        scratch_fbo = None
        result_array = None

        try:
            bake_tex = self.ctx.texture((img_w, img_h), 4, dtype='f4')
            bake_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            bake_fbo = self.ctx.framebuffer(color_attachments=[bake_tex])
            # P0-COL-04：前缀逐层合成需要第二个等尺寸高精度目标做 ping-pong
            scratch_tex = self.ctx.texture((img_w, img_h), 4, dtype='f4')
            scratch_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            scratch_fbo = self.ctx.framebuffer(color_attachments=[scratch_tex])

            dst_choices = [(bake_tex, bake_fbo), (scratch_tex, scratch_fbo)]
            dst_idx = 0
            current_src_tex = source_tex
            current_src_fbo = None  # (source_tex 是外部纹理，无配对 FBO)

            from src.features.color.core.shader_compiler import (
                plugin_bindings_from_instances,
            )

            # P0-COL-04：逐层渲染完整栈前缀（可见层），语义与预览循环一致：
            # 普通层走 composition pass；前缀内已冻结层按「前缀替换」直通；
            # 有 AI 结果纹理的层以 AI 输出为本层输入（P0-COL-03）。
            for prefix_layer in prefix_layers:
                if self._cancel_requested():
                    logger.info("Bake request cancelled before prefix layer")
                    return
                pl_is_dict = isinstance(prefix_layer, dict)
                pl_visible = (prefix_layer.get('visible', True) if pl_is_dict
                              else getattr(prefix_layer, 'visible', True))
                if not pl_visible:
                    continue

                dst_tex, dst_fbo = dst_choices[dst_idx]
                dst_fbo.use()
                self.ctx.viewport = (0, 0, img_w, img_h)
                self.ctx.clear(0.0, 0.0, 0.0, 0.0)

                pl_id = (prefix_layer.get('id') if pl_is_dict
                         else getattr(prefix_layer, 'id', None))
                pl_baked = (prefix_layer.get('is_baked', False) if pl_is_dict
                            else getattr(prefix_layer, 'is_baked', False))
                baked_tex = (self.baked_textures.get(pl_id)
                             if (pl_baked and pl_id) else None)

                if baked_tex is not None:
                    # 已冻结的下层：直通替换当前栈结果（与预览一致）
                    prog_pass = self.utility_shaders['passthrough']
                    baked_tex.use(0)
                    prog_pass['u_texture'].value = 0
                    self._render_quad(prog_pass)
                else:
                    # P0-COL-03：有 AI 结果时以 AI 纹理为本层输入（matte
                    # 与合成消费同一纹理，与预览路径同语义）
                    input_tex = (self._lookup_layer_ai_texture(prefix_layer)
                                 or current_src_tex)
                    curves_lut = (prefix_layer.get('curves_lut') if pl_is_dict
                                  else getattr(prefix_layer, 'curves_lut', None))
                    has_curves = self.pipeline.has_active_curves_lut(curves_lut)
                    warper = (prefix_layer.get('warper_params') if pl_is_dict
                              else getattr(prefix_layer, 'warper_params', None))
                    has_warper = warper is not None
                    lut_path = (prefix_layer.get('active_lut_path') if pl_is_dict
                                else getattr(prefix_layer, 'active_lut_path', None))
                    has_lut3d = lut_path is not None
                    scp = (prefix_layer.get('soft_clip_params') if pl_is_dict
                           else getattr(prefix_layer, 'soft_clip_params', {}))
                    has_soft_clip = scp.get('enabled', False) if scp else False
                    # 曲线面板四参数柔化裁切：非恒等才编译进管线
                    csc = (prefix_layer.get('curves_soft_clip') if pl_is_dict
                           else getattr(prefix_layer, 'curves_soft_clip', None))
                    has_curves_soft_clip = not curves_soft_clip_is_identity(csc)
                    hsl_lut = (prefix_layer.get('hsl_curve_lut') if pl_is_dict
                               else getattr(prefix_layer, 'hsl_curve_lut', None))
                    has_hsl_curve = self.pipeline.has_active_hsl_curve_lut(hsl_lut)
                    mixer = (prefix_layer.get('mixer_params') if pl_is_dict
                             else getattr(prefix_layer, 'mixer_params', None))
                    has_mixer = self.pipeline.has_active_mixer_params(mixer)
                    # P7-01：解析为权威 PluginBinding（index 保留原始占位）
                    pl_plugins_instances = (
                        prefix_layer.get('plugins') if pl_is_dict
                        else getattr(prefix_layer, 'plugins', []))
                    layer_plugins = plugin_bindings_from_instances(
                        pl_plugins_instances)

                    prog = self.pipeline.build_composition_shader(
                        has_curves=has_curves,
                        has_warper=has_warper,
                        has_lut3d=has_lut3d,
                        has_soft_clip=has_soft_clip,
                        grain_enabled=False,
                        has_qualifier=False,
                        has_hsl_curve=has_hsl_curve,
                        has_mixer=has_mixer,
                        has_curves_soft_clip=has_curves_soft_clip,
                        plugins=layer_plugins,
                        request_id=render_request_id,
                        layer_id=pl_id,
                    )
                    if not prog:
                        # P0-COL-01/P1-OBS-01：不允许静默降级——bake 产物
                        # 会缺层，直接放弃本次 bake 并留下可检索日志。
                        logger.warning(
                            "Bake aborted: composition shader build failed "
                            "for prefix layer (layer_id=%s)",
                            pl_id)
                        return

                    matte_tex = self._generate_matte(
                        prefix_layer, current_src_tex,
                        img_w, img_h, (0, 0, img_w, img_h))

                    input_tex.use(0)
                    matte_tex.use(1)
                    prog['u_input_tex'].value = 0
                    prog['u_matte_tex'].value = 1
                    prog['u_tex_size'].value = (float(img_w), float(img_h))

                    self._update_composition_uniforms(
                        prog, prefix_layer, {}, img_w, img_h, {},
                        (0, 0, img_w, img_h), bindings=layer_plugins)
                    self._bind_aux_textures_single_layer(prog, prefix_layer)

                    self._render_quad(prog)

                # 本层输出成为下一层输入
                current_src_tex, current_src_fbo = dst_tex, dst_fbo
                dst_idx ^= 1

            # Read back pixels as float32 numpy array
            # fbo.read() returns bytes, must convert to numpy array immediately
            read_fbo = current_src_fbo if current_src_fbo is not None else bake_fbo
            raw_bytes = read_fbo.read(components=3, dtype='f4')
            result_array = np.frombuffer(raw_bytes, dtype=np.float32).reshape((img_h, img_w, 3))

            # Flip vertically (OpenGL origin is bottom-left, EXR uses top-left origin)
            result_array = np.flipud(result_array)

        finally:
            # CRITICAL: Always release VRAM resources to prevent memory leak
            # This must happen even if rendering fails mid-process
            if bake_fbo is not None:
                try:
                    bake_fbo.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            if bake_tex is not None:
                try:
                    bake_tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            if scratch_fbo is not None:
                try:
                    scratch_fbo.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            if scratch_tex is not None:
                try:
                    scratch_tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)
            # CRITICAL: Release temporary full-resolution texture
            if temp_full_tex is not None:
                try:
                    temp_full_tex.release()
                except Exception:
                    logger.debug('GPU resource release failed during cleanup', exc_info=True)

        # Emit signal with result (only if successful)
        if result_array is not None:
            self.layer_baked_ready.emit(layer_id, result_array)

    def _do_upload_baked_texture(self, data):
        """
        Upload a baked layer result (from EXR file) to GPU texture.

        Args:
            data: dict with keys:
                - 'layer_id': unique layer identifier (str)
                - 'image_array': numpy array (float32, BGR format)
        """
        if not self.ctx or not data:
            return

        layer_id = data.get('layer_id')
        image_array = data.get('image_array')

        if not layer_id or image_array is None:
            return

        # AUD-0208: 形状校验（在释放旧纹理之前）——baked 路径只接受 HxWx3；
        # 2 维/4 通道等异常形状会让 tex.write 抛错并泄漏已创建的纹理。
        if image_array.ndim != 3 or image_array.shape[2] != 3:
            logger.error(
                "Baked texture upload rejected: expected HxWx3 float array, "
                "got dtype=%s shape=%s for layer %s",
                getattr(image_array, 'dtype', None), getattr(image_array, 'shape', None),
                layer_id)
            return

        # Release existing baked texture if any
        if layer_id in self.baked_textures:
            try:
                self.baked_textures[layer_id].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)

        # Ensure correct format
        if image_array.dtype != np.float32:
            image_array = image_array.astype(np.float32)

        h, w = image_array.shape[:2]

        # Flip vertically for OpenGL (origin is bottom-left)
        # EXR file is stored with normal orientation (top-left origin)
        # OpenGL textures use bottom-left origin, so we need to flip
        # CRITICAL: np.flipud creates a non-contiguous view, which can crash tex.write()
        # Must wrap with np.ascontiguousarray to ensure safe memory layout
        image_array = np.ascontiguousarray(np.flipud(image_array))

        # AUD-0208: 创建/写入包 try/except——write 抛错时释放已创建的纹理，
        # 不把泄漏对象留在显存里。
        tex = None
        try:
            tex = self.ctx.texture((w, h), 3, dtype='f4')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.write(image_array.tobytes())
        except Exception:
            logger.exception("Failed to upload baked texture for layer %s", layer_id)
            if tex is not None:
                try:
                    tex.release()
                except Exception:
                    logger.debug("Failed to release failed baked texture", exc_info=True)
            return

        self.baked_textures[layer_id] = tex

    def _do_clear_baked_texture(self, layer_id: str) -> None:
        """Release a layer bake without disturbing its other GPU resources."""
        texture = self.baked_textures.pop(layer_id, None)
        if texture is None:
            return
        try:
            texture.release()
        except Exception:
            logger.debug("Failed to release baked texture", exc_info=True)

    def get_baked_texture(self, layer_id):
        """Get the baked texture for a layer, or None if not baked."""
        return self.baked_textures.get(layer_id)

    def _do_upload_ai_plugin_result(self, data):
        """
        Upload an AI plugin inference result to GPU texture.

        Args:
            data: dict with keys:
                - 'layer_id': unique layer identifier (str)
                - 'instance_id': plugin instance identifier (str)
                - 'image_array': numpy array (float32, RGB format)
        """
        if not self.ctx or not data:
            return

        layer_id = data.get('layer_id')
        instance_id = data.get('instance_id')
        image_array = data.get('image_array')

        if not layer_id or not instance_id or image_array is None:
            return

        key = (layer_id, instance_id)

        # Ensure correct format
        if image_array.dtype != np.float32:
            image_array = image_array.astype(np.float32)

        h, w = image_array.shape[:2]

        # Handle grayscale (mask-like) or 3-channel images
        if image_array.ndim == 2:
            image_array = np.stack([image_array] * 3, axis=-1)
        elif image_array.ndim == 3 and image_array.shape[2] == 1:
            image_array = np.repeat(image_array, 3, axis=2)
        elif image_array.ndim == 3 and image_array.shape[2] == 4:
            image_array = image_array[:, :, :3]

        # AUD-0208: 归一化后仍非 HxWx3（如 1 维/4 维数组或 2 通道）→ 拒绝上传。
        # 校验放在释放旧纹理之前，被拒绝的帧不影响既有纹理。
        if image_array.ndim != 3 or image_array.shape[2] != 3:
            logger.error(
                "AI plugin result rejected: expected HxWx3 (or gray/RGBA) array, "
                "got dtype=%s shape=%s for layer %s",
                getattr(image_array, 'dtype', None), getattr(image_array, 'shape', None),
                layer_id)
            return

        # Release existing texture if any
        if key in self.ai_plugin_textures:
            try:
                self.ai_plugin_textures[key].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)

        # For viewport display, downscale if the image is much larger than needed
        # (e.g., 4x upscale produces a huge texture; we display at original resolution)
        # Check if there is a pyramid texture to get original size
        max_viewport_size = 4096
        if max(h, w) > max_viewport_size and self.pyramid_textures:
            orig_w, orig_h = self.pyramid_textures[0].size
            # Downscale to viewport-friendly size while preserving aspect ratio
            scale = min(max_viewport_size / max(h, w), 1.0)
            new_w, new_h = int(w * scale), int(h * scale)
            image_array = cv2.resize(image_array, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
            h, w = new_h, new_w

        # Flip vertically for OpenGL
        image_array = np.ascontiguousarray(np.flipud(image_array))

        # AUD-0208: 创建/写入包 try/except——write 抛错时释放已创建的纹理。
        tex = None
        try:
            tex = self.ctx.texture((w, h), 3, dtype='f4')
            tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
            tex.write(image_array.tobytes())
        except Exception:
            logger.exception(
                "Failed to upload AI plugin result for layer %s", layer_id)
            if tex is not None:
                try:
                    tex.release()
                except Exception:
                    logger.debug("Failed to release failed AI texture", exc_info=True)
            return

        self.ai_plugin_textures[key] = tex
        logger.info(f"AI plugin texture uploaded: {w}x{h} for layer {layer_id[:8]}")

    def _do_remove_ai_plugin_texture(self, data):
        """Release GPU texture for a removed AI plugin instance."""
        if not self.ctx or not data:
            return
        layer_id = data.get('layer_id')
        instance_id = data.get('instance_id')
        if not layer_id or not instance_id:
            return
        key = (layer_id, instance_id)
        if key in self.ai_plugin_textures:
            try:
                self.ai_plugin_textures[key].release()
            except Exception:
                logger.debug('GPU resource release failed during cleanup', exc_info=True)
            del self.ai_plugin_textures[key]
            logger.info(f"AI plugin texture released for layer {layer_id[:8]}, plugin {instance_id[:8]}")

    def _cancel_requested(self) -> bool:
        """Return whether the render thread has received a cooperative stop."""
        event = self.__dict__.get("_stop_event")
        return bool(event is not None and event.is_set())

    def stop(self, timeout_ms: int = 10000) -> bool:
        """停止渲染线程并等待退出。

        无超时的 wait() 在 GL 清理挂起或长命令（AI bake/视频渲染）执行中
        会永久阻塞调用方（窗口关闭/测试清理）。超时后记录警告并返回 False，
        由调用方决定后续处置；线程仍在运行时销毁本对象会 Abort 进程，
        因此超时路径必须可见、可诊断。
        """
        stop_event = self.__dict__.get("_stop_event")
        if stop_event is not None:
            stop_event.set()
        self.is_running = False
        if self.wait(timeout_ms):
            return True
        logger.error(
            "AsyncRenderer 未在 %dms 内退出（可能仍在执行长命令或 GL 清理挂起）",
            timeout_ms,
        )
        return False
