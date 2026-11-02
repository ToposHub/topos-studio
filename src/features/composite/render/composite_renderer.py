"""
Composite Renderer - Independent GPU rendering thread for the composite pipeline.

Architecture:
- Own shared OpenGL context (texture IDs valid across all shared contexts)
- Own FramebufferManager and texture pool
- Own command queue
- Evaluates against GraphSnapshot (immutable copy from UI thread)
- Content-addressable cache: same params + same upstream = skip re-evaluation

Supports:
- Passthrough rendering
- Blend/Merge operations
- Blur (separable Gaussian)
- Grade (lift/gamma/gain/offset)
- Transform (translate/scale/rotate)
- Invert (channel inversion)
- Synchronous output readback (for quick export)
"""

import logging
import moderngl
import numpy as np
import cv2
import queue
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)
from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtGui import QOffscreenSurface, QOpenGLContext
from src.shared.gpu.gl_backend import GLContextManager, FramebufferManager
from src.features.composite.core.graph_snapshot import GraphSnapshot
from src.features.composite.nodes.base_node import TextureResult
from src.features.composite.render.cache_manager import CacheManager
from src.features.composite.evaluators.graph_evaluator_mixin import GraphEvaluatorMixin
from .ai_cache_manager import AICacheManager
from .ai_compute_manager import AIComputeManager
from src.features.composite.utils.perf_monitor import PerfMonitor
from src.features.composite.render.render_scheduler import (
    DYNAMIC_SOURCE_NODE_TYPES,
    RenderRequest,
    RenderScheduler,
    RenderMode,
    RequestIdGenerator,
)
from src.features.composite.render.gpu_resource_pool import GPUResourcePool
from src.features.composite.core.image_loader import ImageLoader
from src.features.composite.core.eval_context import CompositeEvalContext
from src.features.composite.core.eval_context import resolve_project_motion_blur_shutter


class CompositeCommand:
    EVALUATE_GRAPH = "evaluate_graph"
    UPLOAD_IMAGE = "upload_image"
    UPLOAD_IMAGE_CPU = "upload_image_cpu"
    IMAGE_DECODE_FAILED = "image_decode_failed"
    CLEANUP = "cleanup"
    CLEANUP_NODE = "cleanup_node"
    READBACK_OUTPUT = "readback_output"
    READBACK_NODE_THUMBNAIL = "readback_node_thumbnail"  # Read node output for thumbnail
    READBACK_PRESENT_ASYNC = "readback_present_async"  # Async readback for preview (non-blocking)
    # One-shot full-resolution export evaluated on this renderer's GL context.
    RENDER_EXPORT = "render_export"
    # Native GPU scope pipeline for the QC Scopes dock (no CPU readback).
    # data: ScopeDemand from src.features.color.core.scope_types. The result
    # is emitted as a ScopeGpuFrame descriptor via gpu_scope_ready.
    RENDER_GPU_SCOPE = "render_gpu_scope"
    AI_RESULT_READY = "ai_result_ready"
    PREFETCH_EVALUATE = "prefetch_evaluate"  # Prefetch predicted parameter values
    CLEAR_PREFETCH_CACHE = "clear_prefetch_cache"  # Clear prefetch cache entries
    # P6-B：相机导航快速路径——绕过完整 eval，只重画 Scene3D（复用缓存的 mesh/材质/IBL）。
    # data: {node_id, cam_params, view_signature, request_id}。渲染线程比对缓存的
    # scene_content_sig，命中则 view-only 复渲；未命中则降级回 EVALUATE_GRAPH。
    RENDER_3D_VIEW = "render_3d_view"
    # P1-25/P2-03：CPU 模型导入完成后由 worker 线程排队唤醒一次 render eval。
    MODEL_IMPORT_READY = "model_import_ready"


class CompositeRenderer(GraphEvaluatorMixin, QThread):
    """
    Independent GPU rendering thread for composite operations.

    Evaluates the node graph against an immutable GraphSnapshot,
    never touching the UI thread's NodeGraph directly.
    """

    # frame_ready now includes request_id for UI stale detection
    frame_ready = pyqtSignal(int, int, int, int)  # request_id, texture_id, width, height
    metadata_ready = pyqtSignal(dict)        # {"format": {...}|None, "bbox": {...}|None}
    error_occurred = pyqtSignal(str)
    # Signal for node thumbnail data (node_id, width, height, numpy array as bytes)
    node_thumbnail_ready = pyqtSignal(str, int, int, bytes)  # node_id, width, height, data
    # Async preview image: request_id, BGR float32 numpy array, width, height
    preview_image_ready = pyqtSignal(int, object, int, int)
    # Native GPU scope frame descriptor (ScopeGpuFrame; texture ids only,
    # never pixel arrays).  Mirrors AsyncRenderer.gpu_scope_ready so the
    # Composite QC scopes run the exact same full-GPU pipeline as Color.
    gpu_scope_ready = pyqtSignal(object)
    # M08.1c: Bake staleness badge (audit_bake_staleness entry list; empty = clear badge).
    # Emitted only when the state changes (per-frame audit, signal deduplicated).
    bake_staleness_changed = pyqtSignal(list)

    def __init__(self, main_context, ai_cache_manager=None,
                 ai_compute_manager=None) -> None:
        super().__init__()
        self.main_context = main_context
        self.command_queue = queue.Queue()

        # B6: the AI compute manager is a QObject whose worker slots are
        # delivered via QUEUED connections — they require a thread with a
        # running event loop. This render thread uses a manual queue loop,
        # so a manager created HERE never receives worker completions and
        # the async AI path silently dies. The page (main thread, event
        # loop) owns the manager; both share ONE disk-cache instance.
        self._external_ai_cache_manager = ai_cache_manager
        self._external_ai_compute_manager = ai_compute_manager

        # OpenGL context
        self.ctx = None
        self.surface = None
        self.shared_context = None

        # Triple buffer ring for output
        self.fbo_ring = [None, None, None]
        self.tex_ring = [None, None, None]
        self.write_idx = 0

        # State
        self.is_running = True
        self.setObjectName("CompositeRenderThread")
        self._context_current = False
        self._cleanup_started = False
        self._cleanup_lock = threading.Lock()
        self._cleanup_complete = threading.Event()

        # Evaluation state — snapshot-driven
        self.fb_manager = None
        self.utility_shaders = {}
        self._initialized = False

        # Current snapshot (immutable, owned by render thread)
        self._snapshot: Optional[GraphSnapshot] = None
        self._last_eval_generation: int = -1
        self._last_color_revision: int = -1
        # B1: active user viewer ROI (None = full frame)
        self._active_viewer_roi = None

        # Request signature for skip condition (P1-2 fix)
        # Must include all factors that affect output beyond generation/revision
        self._last_render_mode: str = ""
        self._last_viewer_roi: Optional[tuple] = None  # (x, y, w, h) or None
        self._last_proxy_scale: float = 1.0  # 预览代理分辨率(LOD 强制档)
        self._last_quality_profile: str = ""
        self._last_output_name: str = "display"
        self._last_output_node_id: Optional[str] = None
        self._last_color_texture_id: int = 0
        self._last_color_width: int = 0
        self._last_color_height: int = 0
        # view_signature: view_mode + preview target. A change forces re-eval even
        # when topology is unchanged (Tab toggle / preview switch bake different
        # show_grid/bg_opaque into Scene3D output). None ⇒ never matched first time.
        self._last_view_signature: Optional[str] = None

        # Per-node render instances for GPU-stateful nodes
        self._render_nodes: Dict[str, object] = {}
        # 最近一次完整渲染请求，用于后台模型导入完成后的同快照刷新。
        self._last_render_request: Optional[RenderRequest] = None

        # 阶段七双轨制：scene 数据旁路（key=node_id, value={port: ScenePayload}）。
        # Camera3D/Light3D/Geo3D/Model3D 源节点写入；Scene3D 聚合器读取。
        # 在 _evaluate_snapshot 每帧开始时清空。
        self._scene_payloads: Dict[str, Dict[str, object]] = {}

        # Cache manager and resource pool — initialized in run()
        self._cache_manager: Optional[CacheManager] = None
        self._resource_pool: Optional[GPUResourcePool] = None

        # PBO readback for async preview
        self._pbo_readback = None
        # Output textures are shared with the viewer context.  Keep retired
        # FBO/texture pairs alive until a render-thread fence completes rather
        # than deleting a texture immediately during resize/teardown.
        self._deferred_output_releases: list[tuple[object, list[tuple[object, object]]]] = []

        # Shared GPU scope engine (same module the Color renderer uses).
        # Created lazily on the render thread once the context is current.
        self._gpu_scope_engine: object = None
        self._gpu_scope_failed: bool = False

        # Render scheduler for request tracking
        self._scheduler = RenderScheduler()
        # 自驱播放代际：每次 stop_playback_drive/play 启动递增，驱动循环
        # 据此退出。UI 线程递增、渲染线程只读——int 赋值原子，无需锁。
        self._drive_generation: int = 0
        self._request_id_gen = RequestIdGenerator()

        # Create offscreen surface (must be on GUI thread for Windows)
        self.surface = QOffscreenSurface()
        self.surface.setFormat(main_context.format())
        self.surface.create()

        # Create shared context
        current_surface = self.main_context.surface()
        if current_surface:
            self.main_context.doneCurrent()

        self.shared_context = QOpenGLContext()
        self.shared_context.setFormat(self.main_context.format())
        self.shared_context.setShareContext(self.main_context)

        if not self.shared_context.create():
            logger.error("Failed to create shared context!")

        self.shared_context.moveToThread(self)

        # Restore main context
        if current_surface:
            self.main_context.makeCurrent(current_surface)

    def submit(self, cmd_type: str, data: dict = None):
        """Submit a command to the render thread.

        For EVALUATE_GRAPH commands, drains any pending EVALUATE_GRAPH
        from the queue first to avoid processing stale snapshots during
        rapid parameter changes (e.g. slider drag).

        Enhanced queue merging:
        - Compare request signatures (request_id, graph_generation, color_revision)
        - Only keep the most recent request, discard all stale ones
        - Preserves non-EVALUATE commands (UPLOAD_IMAGE, CLEANUP, etc.)
        """
        if cmd_type == CompositeCommand.EVALUATE_GRAPH:
            # Get the new request signature for comparison
            request = data.get('request')
            new_request_id = 0
            new_generation = 0
            new_color_rev = 0

            if request is not None:
                new_request_id = request.request_id
                new_generation = request.graph_generation
                new_color_rev = request.color_revision
                # Interactive graph edits are latest-wins and may cancel an
                # in-flight render. Timeline playback is different: cancelling
                # every frame that is marginally slower than the source FPS
                # produces a permanently frozen viewer. Keep the current
                # timeline render alive, while still coalescing queued frames
                # below to the newest request.
                if request.cancel_on_supersede:
                    self._scheduler.advance_request_id(new_request_id)

            # Drain queue and filter stale EVALUATE_GRAPH commands
            preserved = []
            best_eval_request = None  # Track best EVALUATE_GRAPH to keep

            while True:
                try:
                    item = self.command_queue.get_nowait()
                    item_type, item_data = item

                    if item_type == CompositeCommand.EVALUATE_GRAPH:
                        # Compare request signatures
                        old_request = item_data.get('request')
                        if old_request is not None:
                            old_request_id = old_request.request_id
                            old_generation = old_request.graph_generation
                            old_color_rev = old_request.color_revision

                            # Keep only if newer than new request
                            # "Newer" means: higher request_id OR higher generation OR higher color_rev
                            is_stale = (
                                old_request_id < new_request_id or
                                old_generation < new_generation or
                                old_color_rev < new_color_rev
                            )

                            if not is_stale and best_eval_request is None:
                                # Old request is newer or same, keep it
                                best_eval_request = item
                        # Discard all EVALUATE_GRAPH without request (legacy format)
                        # These are considered stale
                    else:
                        # Preserve non-EVALUATE commands
                        preserved.append(item)
                except queue.Empty:
                    break

            # Put back preserved commands
            for item in preserved:
                self.command_queue.put(item)

            # Put the best EVALUATE_GRAPH (either new or kept old)
            if best_eval_request is not None:
                self.command_queue.put(best_eval_request)
            else:
                self.command_queue.put((cmd_type, data or {}))
        elif cmd_type == CompositeCommand.RENDER_3D_VIEW:
            # View navigation can generate commands at pointer rate. Coalesce
            # only this command type while preserving graph evaluations and
            # resource commands in their original queue order.
            request = (data or {}).get('request')
            new_request_id = getattr(request, "request_id", 0)
            if request is not None:
                # A view-only request must participate in the same
                # latest-wins/stale-result tracking as a full evaluation.
                self._scheduler.advance_request_id(new_request_id)
            preserved = []
            best_item = (cmd_type, data or {})
            best_request_id = new_request_id
            while True:
                try:
                    item = self.command_queue.get_nowait()
                except queue.Empty:
                    break
                item_type, item_data = item
                if item_type != CompositeCommand.RENDER_3D_VIEW:
                    preserved.append(item)
                    continue
                old_request = (item_data or {}).get('request')
                old_request_id = getattr(old_request, "request_id", 0)
                if old_request_id > best_request_id:
                    best_item = item
                    best_request_id = old_request_id
            for item in preserved:
                self.command_queue.put(item)
            self.command_queue.put(best_item)
        elif cmd_type == CompositeCommand.RENDER_GPU_SCOPE:
            # Scope analysis is latest-wins: a queued but unexecuted scope
            # demand is replaced by the newer one so a burst of presented
            # frames never stacks up scope passes in front of graph
            # evaluations in the FIFO queue.
            new_rev = getattr(data, 'revision', 0)
            preserved = []
            best_item = (cmd_type, data)
            best_rev = new_rev
            while True:
                try:
                    item = self.command_queue.get_nowait()
                except queue.Empty:
                    break
                item_type, item_data = item
                if item_type != CompositeCommand.RENDER_GPU_SCOPE:
                    preserved.append(item)
                    continue
                old_rev = getattr(item_data, 'revision', 0)
                if old_rev > best_rev:
                    best_item = item
                    best_rev = old_rev
            for item in preserved:
                self.command_queue.put(item)
            self.command_queue.put(best_item)
        else:
            # Non-EVALUATE commands: just enqueue
            self.command_queue.put((cmd_type, data or {}))

    def stop_thread(self, timeout_ms: int = 3000) -> bool:
        """Stop the render thread and wait for bounded cleanup completion.

        The queue wake-up avoids waiting for the polling timeout, while the
        bounded wait prevents page teardown from hanging forever on a driver
        or a long-running GPU command. A timeout is reported to the caller so
        it can keep the renderer reference alive instead of freeing objects
        while the render thread may still use them.
        """
        self._drive_generation += 1
        self.is_running = False
        if not self.isRunning():
            return True
        if QThread.currentThread() == self:
            logger.warning("CompositeRenderer.stop_thread() called on render thread")
            return False
        # Wake the manual queue loop immediately. The loop condition is
        # checked after this command and its finally block owns GL cleanup.
        self.command_queue.put((CompositeCommand.CLEANUP, {}))
        stopped = self.wait(max(0, int(timeout_ms)))
        if not stopped:
            logger.error(
                "Composite render thread did not stop within %d ms; "
                "resources remain owned by the live thread", timeout_ms)
        return bool(stopped)

    def readback_output(self, timeout=10.0) -> Optional[np.ndarray]:
        """Synchronous readback of the current output texture.

        Called from the main thread. Submits a readback command and blocks
        until the result is ready or timeout expires.

        Returns:
            BGR float32 numpy array, or None on failure/timeout.
        """
        if not self.is_running or not self.isRunning():
            return None

        result_holder = {'data': None}
        done_event = threading.Event()

        self.command_queue.put((CompositeCommand.READBACK_OUTPUT, {
            'result_holder': result_holder,
            'done_event': done_event,
        }))

        if done_event.wait(timeout=timeout):
            return result_holder['data']
        return None

    def render_export(
        self,
        snapshot: GraphSnapshot,
        color_image: np.ndarray,
        *,
        config_manager: object | None = None,
        render_pass: str = "beauty",
        interactive_scheduler: object | None = None,
        timeout: float = 120.0,
    ):
        """Evaluate a full-resolution export on the composite render thread.

        ``color_image`` is CPU data only.  The worker uploads it into its own
        shared context, evaluates an isolated ``CompositeExportRenderer`` and
        returns its structured result after readback.  The blocking wait keeps
        legacy delivery APIs compatible; no OpenGL call occurs on the caller.
        """
        if not self.is_running or not self.isRunning():
            return None
        holder = {'data': None}
        done_event = threading.Event()
        self.command_queue.put((CompositeCommand.RENDER_EXPORT, {
            'snapshot': snapshot,
            'color_image': color_image,
            'config_manager': config_manager,
            'render_pass': render_pass,
            'interactive_scheduler': interactive_scheduler,
            'result_holder': holder,
            'done_event': done_event,
        }))
        if done_event.wait(timeout=max(0.0, float(timeout))):
            return holder['data']
        return None

    def run(self) -> None:
        """Main render thread loop with exception-safe GL teardown."""
        mutex = GLContextManager.instance().mutex
        try:
            if not self.shared_context.makeCurrent(self.surface):
                logger.warning("Failed to make context current")
                self.error_occurred.emit("Failed to make OpenGL context current")
                return
            self._context_current = True

            try:
                self.ctx = moderngl.create_context()
                self.fb_manager = FramebufferManager(self.ctx)
                self._resource_pool = GPUResourcePool(self.fb_manager, max_memory_mb=1024)
                self._cache_manager = CacheManager(self._resource_pool, max_entries=128)
                self._load_shaders()

                # Initialize AI cache and compute managers. Prefer the
                # page-owned instances (main thread, see __init__ note);
                # constructing the manager here is a compatibility fallback
                # for direct/test constructions without an event loop.
                self._ai_cache_manager = (
                    self._external_ai_cache_manager or AICacheManager())
                self._ai_compute_manager = (
                    self._external_ai_compute_manager
                    or AIComputeManager(self._ai_cache_manager))
                self._perf_monitor = PerfMonitor()

                # Initialize async image loader
                # Use direct callbacks to command_queue instead of Qt signals,
                # since this thread uses a manual queue loop, not a Qt event loop.
                self._image_loader = ImageLoader(
                    on_decoded=lambda nid, decoded: self._on_image_decoded(nid, decoded),
                    on_failed=lambda nid, fpath, err: self._on_image_decode_failed(nid, fpath, err),
                )

                self._initialized = True
                logger.info("ModernGL initialized")

                # Initialize PBO readback for async preview
                try:
                    from src.features.composite.render.pbo_readback import AsyncPBOReadback
                    self._pbo_readback = AsyncPBOReadback(self.ctx)
                    if not self._pbo_readback.initialize():
                        logger.info("[COMPOSITE-PREVIEW] PBO readback init failed, using direct readback")
                        self._pbo_readback = None
                except Exception as e:
                    logger.debug(f"[COMPOSITE-PREVIEW] PBO readback unavailable: {e}")
                    self._pbo_readback = None
            except Exception as e:
                logger.error(f"Failed to initialize: {e}")
                logger.debug("Stack trace:", exc_info=True)
                self.error_occurred.emit(str(e))
                return

            while self.is_running:
                try:
                    item = self.command_queue.get(timeout=0.01)
                    cmd_type, data = item

                    mutex.lock()
                    try:
                        self._process_command(cmd_type, data)
                    finally:
                        mutex.unlock()

                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"Error in render loop: {e}")
                    logger.debug("Stack trace:", exc_info=True)
                    self.error_occurred.emit(str(e))
        finally:
            self.is_running = False
            self._cleanup()
            if self._context_current and self.shared_context is not None:
                try:
                    self.shared_context.doneCurrent()
                except Exception as exc:
                    logger.debug("Shared OpenGL context release failed: %s", exc)
                self._context_current = False

    def _process_command(self, cmd_type: str, data: dict):
        """Process a single command."""
        self._drain_deferred_output_releases()
        if cmd_type == CompositeCommand.EVALUATE_GRAPH:
            self._handle_evaluate_graph(data)
        elif cmd_type == CompositeCommand.UPLOAD_IMAGE:
            self._handle_upload_image(data)
        elif cmd_type == CompositeCommand.UPLOAD_IMAGE_CPU:
            self._handle_upload_image_cpu(data)
        elif cmd_type == CompositeCommand.IMAGE_DECODE_FAILED:
            self._handle_image_decode_failed(data)
        elif cmd_type == CompositeCommand.CLEANUP:
            self.is_running = False
        elif cmd_type == CompositeCommand.CLEANUP_NODE:
            self._handle_cleanup_node(data)
        elif cmd_type == CompositeCommand.READBACK_OUTPUT:
            self._handle_readback(data)
        elif cmd_type == CompositeCommand.READBACK_NODE_THUMBNAIL:
            self._handle_readback_node_thumbnail(data)
        elif cmd_type == CompositeCommand.READBACK_PRESENT_ASYNC:
            self._handle_readback_present_async(data)
        elif cmd_type == CompositeCommand.RENDER_EXPORT:
            self._handle_render_export(data)
        elif cmd_type == CompositeCommand.RENDER_GPU_SCOPE:
            self._handle_render_gpu_scope(data)
        elif cmd_type == CompositeCommand.AI_RESULT_READY:
            self._handle_ai_result_ready(data)
        elif cmd_type == CompositeCommand.PREFETCH_EVALUATE:
            self._handle_prefetch_evaluate(data)
        elif cmd_type == CompositeCommand.CLEAR_PREFETCH_CACHE:
            self._handle_clear_prefetch_cache(data)
        elif cmd_type == CompositeCommand.RENDER_3D_VIEW:
            self._handle_render_3d_view(data)
        elif cmd_type == CompositeCommand.MODEL_IMPORT_READY:
            self._handle_model_import_ready(data)

    def _handle_render_3d_view(self, data: dict):
        """P6-B：相机导航快速路径——绕过完整 eval，复用缓存重画 Scene3D。

        只有同一图代际、同一颜色输入、同一预览目标的请求才允许进入
        ``Scene3DNode.redraw_with_view``。任何缓存缺失、拓扑/颜色变化、
        外接相机或非预览请求都回退到完整图求值。
        """
        node_id = data.get("node_id", "")
        request = data.get("request")
        request_id = getattr(request, "request_id", data.get("request_id", 0))
        if request is None:
            self._handle_evaluate_graph(data)
            return

        cached_request = self._last_render_request
        node = self._render_nodes.get(node_id) if node_id else None
        request_quality_profile = getattr(request, "quality_profile", "")
        cached_quality_profile = getattr(
            cached_request, "quality_profile", "") if cached_request else ""
        request_output_name = getattr(request, "output_name", "display")
        cached_output_name = getattr(
            cached_request, "output_name", "display") if cached_request else "display"
        request_output_node_id = getattr(request, "output_node_id", None)
        cached_output_node_id = getattr(
            cached_request, "output_node_id", None) if cached_request else None
        cache_compatible = (
            cached_request is not None
            and request.render_mode == RenderMode.PREVIEW
            and request.viewer_node_id == node_id
            and cached_request.viewer_node_id == node_id
            and request.graph_generation == cached_request.graph_generation
            and request.color_revision == cached_request.color_revision
            and request.snapshot.generation == cached_request.snapshot.generation
            and request.snapshot.color_texture_id == cached_request.snapshot.color_texture_id
            and request.snapshot.color_width == cached_request.snapshot.color_width
            and request.snapshot.color_height == cached_request.snapshot.color_height
            and request_quality_profile == cached_quality_profile
            and request_output_name == cached_output_name
            and request_output_node_id == cached_output_node_id
            and not request.force_refresh
            and getattr(self, "_eval_ctx", None) is not None
            and self._snapshot is not None
            and request.snapshot.generation == self._snapshot.generation
        )
        if (
            not cache_compatible
            or node is None
            or getattr(node, "node_type", None) != "scene3d"
            or not hasattr(node, "_last_scene_content_sig")
            or not callable(getattr(node, "redraw_with_view", None))
        ):
            # 无缓存实例、图已变化或非 Scene3D → 降级完整 eval。
            self._handle_evaluate_graph(data)
            return

        # Keep the render-thread context in sync with the request. The cached
        # scene payload dictionary is intentionally preserved; source nodes are
        # not evaluated again on this path.
        eval_ctx = self._eval_ctx
        eval_ctx.interaction_active = bool(request.interaction_active)
        eval_ctx.quality_profile = request_quality_profile
        eval_ctx.quad_view = bool(request.quad_view)
        eval_ctx.single_ortho_view = request.single_ortho_view
        eval_ctx.quad_active_view = request.quad_active_view
        eval_ctx.selected_3d_node_id = request.selected_3d_node_id
        eval_ctx.isolate_3d_node_id = request.isolate_3d_node_id
        eval_ctx.view_mode_overrides = request.view_mode_overrides
        eval_ctx.global_3d_scene_settings = request.global_3d_scene_settings
        eval_ctx.ortho_view_textures = None
        eval_ctx.id_pick_info = None

        result = node.redraw_with_view(
            data.get("cam_params", {}), eval_ctx, self.fb_manager,
            resource_pool=self._resource_pool,
            render_mode=request.render_mode,
        )
        if result is None or not result.is_valid:
            self._handle_evaluate_graph(data)
            return
        if self._scheduler.is_stale(request_id):
            # Scene3D owns its output texture. A stale view-only result must not
            # be returned to the pool; just suppress presentation.
            return

        self._last_render_request = request
        # AUD-8201: same discipline as the other render-to-output call
        # sites — release the transient textures gathered by the evaluation
        # once the frame has been presented.
        try:
            self._render_to_output(result, request_id=request_id)
        finally:
            self._release_transient_textures()
        snapshot = request.snapshot
        self._last_eval_generation = snapshot.generation
        self._last_color_revision = snapshot.color_revision
        self._last_render_mode = request.render_mode
        viewer_roi = request.viewer_roi
        self._last_viewer_roi = (
            (viewer_roi.x, viewer_roi.y, viewer_roi.width, viewer_roi.height)
            if viewer_roi is not None and not viewer_roi.is_empty else None
        )
        self._last_proxy_scale = request.proxy_scale
        self._last_quality_profile = request_quality_profile
        self._last_output_name = request_output_name
        self._last_output_node_id = request_output_node_id
        self._last_color_texture_id = snapshot.color_texture_id
        self._last_color_width = snapshot.color_width
        self._last_color_height = snapshot.color_height
        self._last_view_signature = request.view_signature

    def _sync_render_nodes(self, snapshot: GraphSnapshot):
        """同步 render nodes，并为 Model3D 绑定后台导入完成唤醒。"""
        super()._sync_render_nodes(snapshot)
        for node_id, node_snapshot in snapshot.nodes.items():
            if node_snapshot.node_type != "model3d":
                continue
            node = self._render_nodes.get(node_id)
            if node is not None and hasattr(node, "set_model_import_ready_callback"):
                node.set_model_import_ready_callback(
                    lambda nid=node_id: self._on_model_import_ready(nid))

    def _on_model_import_ready(self, node_id: str) -> None:
        """worker 回调入口：只投递命令，不触碰 Qt/GL 状态。"""
        if not self.is_running:
            return
        self.command_queue.put((CompositeCommand.MODEL_IMPORT_READY,
                                {"node_id": node_id}))

    def _handle_model_import_ready(self, data: dict) -> None:
        """在 render thread 消费已完成的 CPU SceneGraph 并强制刷新一次。"""
        node_id = str(data.get("node_id", ""))
        node = self._render_nodes.get(node_id)
        if node is None or self._snapshot is None:
            return
        request = self._last_render_request
        if request is None:
            return

        # Model3D scene payload 不产生 TextureResult；清理评估缓存才能让
        # Scene3D 重新收集新 payload，并在本线程创建对应 VAO/VBO。
        self._clear_eval_cache()
        if hasattr(self, "_node_cache_keys"):
            self._node_cache_keys.clear()
        if hasattr(self._scheduler, "reset_dirty_tracking"):
            self._scheduler.reset_dirty_tracking()
        self._last_eval_generation = -1
        self._last_view_signature = None

        refresh = replace(
            request,
            request_id=self._request_id_gen.next_id(),
            timestamp=time.monotonic(),
            force_refresh=True,
            playback_drive=None,
        )
        self._handle_evaluate_graph({"request": refresh})

    # ── Command Handlers ──

    def _handle_evaluate_graph(self, data: dict):
        """Evaluate the node graph from a render request or raw snapshot."""
        if not self._initialized:
            return

        # Support both RenderRequest and legacy raw snapshot
        request: Optional[RenderRequest] = data.get('request')
        if request is not None:
            snapshot = request.snapshot
            request_id = request.request_id
            render_mode = request.render_mode
            viewer_roi = request.viewer_roi
        else:
            snapshot = data.get('snapshot')
            request_id = 0
            render_mode = RenderMode.PREVIEW
            viewer_roi = None
            # Create a minimal request for tracking
            request = RenderRequest(
                snapshot=snapshot,
                request_id=0,
                timestamp=time.monotonic(),
                render_mode=render_mode,
                viewer_roi=viewer_roi,
                graph_generation=snapshot.generation if snapshot else 0,
                color_revision=snapshot.color_revision if snapshot else 0,
            )
        if snapshot is None:
            return
        self._last_render_request = request
        # B1: keep the active ROI so _render_to_output can recomposite a
        # cropped result back into a full frame (also used by the
        # AI_RESULT_READY re-render path which has no request at hand).
        # Preview-only: in FULL the chain is never ROI-cropped, and a
        # naturally sub-format result (Transform down-scale …) must not be
        # misread as an ROI sub-image and shifted into place.  Likewise a
        # forced-LOD (proxy quality) preview evaluates at reduced
        # resolution, where the full-res ROI rect would crop the wrong area.
        self._active_viewer_roi = (
            viewer_roi
            if (viewer_roi is not None
                and not viewer_roi.is_empty
                and render_mode == RenderMode.PREVIEW
                and float(getattr(request, 'proxy_scale', 1.0) or 1.0) >= 1.0)
            else None)

        # Build current request signature for skip comparison
        current_render_mode = render_mode
        current_viewer_roi = None
        if viewer_roi is not None and not viewer_roi.is_empty:
            current_viewer_roi = (viewer_roi.x, viewer_roi.y, viewer_roi.width, viewer_roi.height)
        current_color_texture_id = snapshot.color_texture_id
        current_color_width = snapshot.color_width
        current_color_height = snapshot.color_height
        # proxy_scale 影响渲染分辨率(LOD 强制档),必须纳入 skip signature,
        # 否则切换预览质量会被误判为"无变化"而跳过重渲染。
        current_proxy_scale = getattr(request, 'proxy_scale', 1.0)
        current_quality_profile = getattr(request, 'quality_profile', '')
        current_output_name = getattr(request, 'output_name', 'display')
        current_output_node_id = getattr(request, 'output_node_id', None)

        # Skip only if ALL signature components match
        # P1-2 fix: include render_mode, viewer_roi, color texture state
        gen_same = snapshot.generation == self._last_eval_generation
        rev_same = snapshot.color_revision == self._last_color_revision
        mode_same = current_render_mode == self._last_render_mode
        roi_same = current_viewer_roi == self._last_viewer_roi
        color_tex_same = current_color_texture_id == self._last_color_texture_id
        color_size_same = (current_color_width == self._last_color_width and
                          current_color_height == self._last_color_height)
        proxy_same = current_proxy_scale == self._last_proxy_scale
        quality_same = current_quality_profile == self._last_quality_profile
        output_same = (
            current_output_name == self._last_output_name
            and current_output_node_id == self._last_output_node_id
        )
        # view_signature captures view-dependent parameter overrides (view_mode +
        # preview target) that bake into node outputs but don't change topology
        # (generation). Without it, a Tab toggle would be masked by the skip
        # check and the stale (e.g. grid-on) result would be shown.
        current_view_signature = getattr(request, 'view_signature', '')
        force_refresh = bool(getattr(request, 'force_refresh', False))
        if force_refresh:
            if self._cache_manager:
                self._clear_eval_cache()
            if hasattr(self, '_node_cache_keys'):
                self._node_cache_keys.clear()
            if hasattr(self._scheduler, 'reset_dirty_tracking'):
                self._scheduler.reset_dirty_tracking()
            self._last_eval_generation = -1
            self._last_view_signature = None
        view_same = current_view_signature == self._last_view_signature
        has_runtime_dynamic_source = any(
            ns.node_type in DYNAMIC_SOURCE_NODE_TYPES
            for ns in snapshot.nodes.values()
        )

        if (not force_refresh and not has_runtime_dynamic_source and gen_same and rev_same and mode_same
                and roi_same and color_tex_same and color_size_same
                and view_same and proxy_same and quality_same and output_same):
            return

        # Track current request for diagnostics and stale detection
        self._scheduler.mark_current(request)

        # Wire AI compute manager stale tracking
        if hasattr(self, '_ai_compute_manager') and self._ai_compute_manager:
            self._ai_compute_manager.set_latest_request_id(request_id)

        self._snapshot = snapshot
        self._sync_render_nodes(snapshot)
        self._sync_image_input_nodes(snapshot)
        self._runtime_context = getattr(request, 'runtime_context', None)
        provider = getattr(request, 'timeline_clip_gpu_provider', None)
        self._project = getattr(request, 'edit_project', None) or getattr(provider, '_project', None)

        # Build CompositeEvalContext from request for source nodes that need
        # business-level context (e.g., TimelineClipInputNode).
        self._eval_ctx = CompositeEvalContext(
            gl=self.ctx,
            runtime_context=self._runtime_context,
            timeline_clip_gpu_provider=provider,
            resource_pool=self._resource_pool,
            framebuffer_manager=self.fb_manager,
            scene_payloads=self._scene_payloads,
            global_3d_scene_settings=getattr(
                request, 'global_3d_scene_settings', None),
            view_mode_overrides=getattr(
                request, 'view_mode_overrides', None),
            quality_profile=current_quality_profile,
            motion_blur_shutter=resolve_project_motion_blur_shutter(
                getattr(request, 'snapshot', None)),
        )

        result = self._evaluate_snapshot(request_id=request_id,
                                         render_mode=render_mode,
                                         viewer_roi=viewer_roi,
                                         request_timestamp=request.timestamp if request else 0.0,
                                         interaction_active=getattr(request, 'interaction_active', False),
                                         viewer_node_id=getattr(request, 'viewer_node_id', None),
                                         viewer_node_port=getattr(request, 'viewer_node_port', None),
                                         quad_view=getattr(request, 'quad_view', False),
                                         single_ortho_view=getattr(request, 'single_ortho_view', None),
                                         quad_active_view=getattr(request, 'quad_active_view', None),
                                         selected_3d_node_id=getattr(request, 'selected_3d_node_id', None),
                                         isolate_3d_node_id=getattr(request, 'isolate_3d_node_id', None),
                                         proxy_scale=current_proxy_scale,
                                         quality_profile=current_quality_profile,
                                         output_name=current_output_name,
                                         output_node_id=current_output_node_id)

        # Check if result is stale before emitting
        if self._scheduler.is_stale(request_id):
            # Request was superseded, discard result and release resources
            if self._perf_monitor:
                self._perf_monitor.record_dropped()
            # ``_evaluate_snapshot`` stores valid node outputs in CacheManager
            # before returning.  Releasing ``result.texture_id`` here therefore
            # puts a texture still referenced by an Output/Transform cache entry
            # back into the pool.  A later cache hit can then bind a recycled GL
            # name, which manifests as intermittent green frames or a driver
            # crash while reconnecting/dragging nodes.  Cache eviction owns this
            # lifecycle; a stale request only suppresses presentation.
            self._release_transient_textures()
            return

        if result and result.is_valid:
            try:
                self._render_to_output(result, request_id=request_id)
            finally:
                self._release_transient_textures()
            # Update ALL signature components on valid result
            # This ensures re-evaluation on next request if any factor changes
            self._last_eval_generation = snapshot.generation
            self._last_color_revision = snapshot.color_revision
            self._last_render_mode = current_render_mode
            self._last_viewer_roi = current_viewer_roi
            self._last_proxy_scale = current_proxy_scale
            self._last_quality_profile = current_quality_profile
            self._last_output_name = current_output_name
            self._last_output_node_id = current_output_node_id
            self._last_color_texture_id = current_color_texture_id
            self._last_color_width = current_color_width
            self._last_color_height = current_color_height
            self._last_view_signature = current_view_signature
        else:
            # P2 fix: Distinguish waiting_for_color vs graph_invalid
            # waiting_for_color: ColorOutput not ready yet (keep old frame)
            # graph_invalid: User disconnected output or graph broken (clear viewer)
            timeline_backed_request = (
                self._runtime_context is not None
                or has_runtime_dynamic_source
            )
            waiting_for_color = (
                not timeline_backed_request
                and (
                    current_color_texture_id <= 0 or
                    current_color_width <= 0 or
                    current_color_height <= 0
                )
            )
            if waiting_for_color:
                # ColorOutput not ready - keep old frame, wait for update_color_output()
                pass
            else:
                # Graph invalid (Output disconnected, upstream deleted) - clear viewer
                logger.warning(
                    "[COMPOSITE-RENDER] graph produced no valid output; "
                    f"request_id={request_id} timeline_backed={timeline_backed_request} "
                    f"dynamic_source={has_runtime_dynamic_source} "
                    f"nodes={len(snapshot.nodes)} connections={len(snapshot.connections)} "
                    f"color_tex={current_color_texture_id} "
                    f"color_size={current_color_width}x{current_color_height}"
                )
                self.frame_ready.emit(request_id, 0, 0, 0)
            self._release_transient_textures()

        # ── 渲染线程自驱播放 ──
        # 独立 comp clip 播放（无时间线、无关键帧动画）时，本请求携带
        # playback_drive：渲染线程接管帧率驱动权，按 wall-clock 自行推进
        # 帧号连续渲染上屏，UI 不再逐帧提交（消灭解码→notify→UI 快照→
        # 重提交的双 UI 往返）。暂停/seek/参数修改由 UI 提交新请求
        # （supersede → is_stale）或 stop_playback_drive() 令循环退出。
        drive = getattr(request, 'playback_drive', None) if request else None
        if drive:
            # 首帧求值可能因视频刚打开而 invalid（解码未就绪）——驱动循环
            # 自己轮询等待解码，不依赖首帧成功。
            self._playback_drive_loop(request)

    # ───────────────────────────────────────────────────────────────────────
    # Render-thread self-driven playback
    # ───────────────────────────────────────────────────────────────────────

    def _dynamic_source_frame_arrived(self) -> bool:
        """worker 解码产物是否已到达（驱动循环的轻量轮询判定，O(1)）。

        video 节点的解码 worker 把 VT 帧写入单槽 ``_vt_pending``、CPU 帧
        写入 ``_frame_cache``；任一非空即有新内容可消费。等待期间用本
        判定轮询而不是反复跑全图求值——持帧求值本身也要几毫秒，重复
        求值曾把 4K 驱动的每帧成本放大 3 倍以上。
        """
        for nid in getattr(self, '_dynamic_cache_bypass_nodes', ()) or ():
            node = self._render_nodes.get(nid)
            if node is None:
                continue
            if getattr(node, '_vt_pending', None) is not None:
                return True
            try:
                with node._cache_lock:
                    if node._frame_cache:
                        return True
            except AttributeError:
                continue
        return False

    def stop_playback_drive(self) -> None:
        """UI 请求退出自驱循环（线程安全：仅递增代际计数）。"""
        self._drive_generation += 1

    def _drive_stopped(self, gen: int, request_id: int) -> bool:
        """自驱循环退出判定：代际变化 / 请求被 supersede / 渲染器关闭。"""
        if self._drive_generation != gen:
            return True
        if self._scheduler.is_stale(request_id):
            return True
        if getattr(self, '_stop_flag', False) or not getattr(self, '_initialized', True):
            return True
        return False

    def _playback_drive_loop(self, request) -> None:
        """自驱播放主循环（渲染线程，命令处理内调用，已持全局 GL 锁）。

        每帧：按 wall-clock 算目标帧号 → 改 snapshot.frame_number → 求值
        （解码 miss 时放锁短睡重试同帧——自驱模式下 UI 不再提交请求，
        没有 supersede 风险，等待是安全的，这是与旧"等待自驱"方案的
        本质区别）→ 渲染上屏 → 就地消费挂起的 scope demand（命令循环
        被本循环独占，不就地执行则 QC 示波器整个播放期间静止）→ 睡到
        下一帧时刻（放锁，其他 GL 线程可穿插）。loop 边界在循环内自治重锚。
        """
        import time as _time
        from src.shared.gpu.gl_backend import GLContextManager

        drive = dict(request.playback_drive)
        gen = self._drive_generation
        request_id = request.request_id
        fps = max(1.0, float(drive.get('fps', 24.0)))
        frame_period = 1.0 / fps
        out_frame = int(drive.get('out_frame', 0))
        in_frame = int(drive.get('in_frame', 0))
        loop = bool(drive.get('loop', True))
        gl_mutex = GLContextManager.instance().mutex

        # 动态源 revision（用于检测"持旧帧"：解码未就绪）
        def _dyn_revs():
            revs = {}
            for nid in getattr(self, '_dynamic_cache_bypass_nodes', ()) or ():
                node = self._render_nodes.get(nid)
                if node is not None:
                    revs[nid] = getattr(node, '_revision', 0)
            return revs

        drive_stats = {"eval": 0.0, "wait": 0.0, "render": 0.0,
                       "sleep": 0.0, "scope": 0.0, "frames": 0, "retries": 0}
        try:
            while not self._drive_stopped(gen, request_id):
                # 目标帧号（wall-clock 驱动，渲染跟不上时自然跳帧）
                _f0 = _time.perf_counter()
                elapsed = _f0 - drive['start_time']
                frame = int(drive['start_frame'] + elapsed * fps)

                if out_frame > 0 and frame > out_frame:
                    if loop and out_frame > in_frame:
                        drive['start_time'] = _time.perf_counter()
                        drive['start_frame'] = in_frame
                        continue
                    break  # one-shot 到尾：退出驱动（UI tick 负责收尾显示）

                # 解码等待重试（持帧 = revision 未变）：放锁短睡，无
                # supersede 风险；上限 3 个帧周期后放弃本帧继续推进。
                # 解码等待：先轻量轮询 worker 产物（O(1)，不跑全图求值——
                # 持帧求值本身要几毫秒，反复重试曾把每帧成本放大 3 倍+），
                # 产物到达后再求值一次。放锁短睡无 supersede 风险；上限
                # 3 个帧周期后放弃等待（推进到下一目标帧）。
                _w0 = _time.perf_counter()
                retry_deadline = _time.perf_counter() + frame_period * 3
                while not self._dynamic_source_frame_arrived():
                    if self._drive_stopped(gen, request_id):
                        break
                    if _time.perf_counter() >= retry_deadline:
                        break
                    drive_stats["retries"] += 1
                    gl_mutex.unlock()
                    _time.sleep(0.003)
                    gl_mutex.lock()
                drive_stats["wait"] += _time.perf_counter() - _w0

                if self._drive_stopped(gen, request_id):
                    break

                _e0 = _time.perf_counter()
                # AUD-48006：GraphSnapshot 已冻结——动画驱动按帧推进改用
                # replace 生成替换快照（字段容器引用复用，开销可忽略），
                # 不再原地重绑定 frame_number。
                self._snapshot = replace(self._snapshot, frame_number=frame)
                result = self._evaluate_snapshot(
                    request_id=request_id,
                    render_mode=request.render_mode,
                    viewer_roi=getattr(request, 'viewer_roi', None),
                    request_timestamp=_time.perf_counter(),
                    interaction_active=getattr(request, 'interaction_active', False),
                    viewer_node_id=getattr(request, 'viewer_node_id', None),
                    viewer_node_port=getattr(request, 'viewer_node_port', None),
                    quad_view=getattr(request, 'quad_view', False),
                    single_ortho_view=getattr(request, 'single_ortho_view', None),
                    quad_active_view=getattr(request, 'quad_active_view', None),
                    proxy_scale=getattr(request, 'proxy_scale', 1.0),
                    quality_profile=getattr(request, 'quality_profile', ''),
                    output_name=getattr(request, 'output_name', 'display'),
                    output_node_id=getattr(request, 'output_node_id', None),
                )
                drive_stats["eval"] += _time.perf_counter() - _e0

                if result is not None and result.is_valid:
                    _r0 = _time.perf_counter()
                    try:
                        self._render_to_output(result, request_id=request_id)
                    finally:
                        self._release_transient_textures()
                    drive_stats["render"] += _time.perf_counter() - _r0
                    drive_stats["frames"] += 1

                # 自驱独占命令循环：UI 提交的 scope demand 不会被 run() 消费，
                # 每帧上屏后就地执行挂起的那条（latest-wins，通常 ≤15Hz）。
                _c0 = _time.perf_counter()
                self._consume_pending_scope_demand()
                drive_stats["scope"] += _time.perf_counter() - _c0

                # 睡到下一帧时刻（放锁让其他 GL 线程穿插）
                now = _time.perf_counter()
                next_at = drive['start_time'] + (int(elapsed * fps) + 1) / fps
                if next_at > now:
                    _s0 = _time.perf_counter()
                    gl_mutex.unlock()
                    _time.sleep(min(next_at - now, frame_period))
                    gl_mutex.lock()
                    drive_stats["sleep"] += _time.perf_counter() - _s0
        except Exception as e:
            logger.warning(f"playback drive loop aborted: {e}")
        finally:
            n = max(drive_stats["frames"], 1)
            print(
                "[DRIVE] frames=%d eval=%.1fms wait=%.1fms render=%.1fms "
                "sleep=%.1fms scope=%.1fms retries=%d (per-frame avg: "
                "eval=%.1f wait=%.1f render=%.1f sleep=%.1f scope=%.1f)" % (
                    drive_stats["frames"],
                    drive_stats["eval"] * 1000, drive_stats["wait"] * 1000,
                    drive_stats["render"] * 1000, drive_stats["sleep"] * 1000,
                    drive_stats["scope"] * 1000,
                    drive_stats["retries"],
                    drive_stats["eval"] / n * 1000, drive_stats["wait"] / n * 1000,
                    drive_stats["render"] / n * 1000, drive_stats["sleep"] / n * 1000,
                    drive_stats["scope"] / n * 1000,
                )
            )

    def _consume_pending_scope_demand(self) -> None:
        """Execute at most one queued RENDER_GPU_SCOPE demand in-place.

        自驱播放独占渲染线程：run() 的命令循环在播放结束前不会消费队列，
        UI 以 ~15Hz 提交的 scope demand 会一路积压到 playback 结束（表现为
        示波器静止、暂停瞬间跳帧刷新）。在每帧上屏后调用本方法，取出队列
        中的 scope demand（latest-wins，submit() 保证最多一条）在当前线程、
        当前 GL 上下文上直接执行——分析的正是刚写入的前台纹理。其余命令
        按原顺序回队。仅在持有全局 GL 锁的驱动循环内调用。
        """
        pending = None
        preserved = []
        while True:
            try:
                item = self.command_queue.get_nowait()
            except queue.Empty:
                break
            if item[0] == CompositeCommand.RENDER_GPU_SCOPE:
                pending = item[1]
            else:
                preserved.append(item)
        for item in preserved:
            self.command_queue.put(item)
        if pending is not None:
            self._handle_render_gpu_scope(pending)

    def _sync_image_input_nodes(self, snapshot: GraphSnapshot):
        """Check image_input nodes for file_path changes and trigger async decode.

        Detects when a file_path has changed or file has been modified on disk
        (by mtime), and submits async decode requests via ImageLoader.
        """
        image_loader = getattr(self, '_image_loader', None)
        if image_loader is None:
            return

        for nid, ns in snapshot.nodes.items():
            if ns.node_type != "image_input":
                continue

            node = self._render_nodes.get(nid)
            if node is None:
                continue

            file_path = ns.parameters.get("file_path", "")

            # Check if node already has valid GPU texture for this path
            # (also true when its exr_part/extra-channel selection changed)
            if node.is_ready and not node.needs_file_mtime_check(file_path):
                continue

            # Path changed, file modified or EXR slice changed: submit the
            # async decode WITH the requested EXR settings (A6).
            node.set_load_loading()
            part_raw = str(ns.parameters.get("exr_part", "") or "")
            extra_raw = str(ns.parameters.get("exr_extra_channels", "") or "")
            extra_list = ([c.strip() for c in extra_raw.split(",")
                           if c.strip()] if extra_raw else None)
            image_loader.request_decode(nid, file_path,
                                        exr_part=part_raw,
                                        extra_channels=extra_list)

    def _handle_upload_image(self, data: dict):
        """Upload an image to a node's GPU texture (synchronous path)."""
        node_id = data.get('node_id')
        node = self._render_nodes.get(node_id)
        if node and hasattr(node, 'upload_to_gpu'):
            node.upload_to_gpu(self.ctx)

    def _handle_upload_image_cpu(self, data: dict):
        """Upload pre-decoded CPU image data to GPU texture.

        Called on the render thread after async ImageLoader completes decode.
        Receives the decoded data via queue and uploads to GPU.
        """
        node_id = data.get('node_id')
        node = self._render_nodes.get(node_id)
        if node is None:
            return

        # Apply decoded data from the worker thread
        decoded = data.get('_decoded')
        if decoded is not None and hasattr(node, 'set_decoded_image'):
            node.set_decoded_image(decoded)

        if hasattr(node, 'needs_upload') and node.needs_upload():
            node.upload_from_cpu(self.ctx)
            # The ImageInput texture changed without a graph/snapshot revision
            # bump.  Downstream cache keys from the placeholder pass still point
            # at the previous 64x64 checker result, so force the next evaluation
            # to rebuild the whole chain from the uploaded source.
            if self._cache_manager:
                self._cache_manager.clear()
            if hasattr(self, '_node_cache_keys'):
                self._node_cache_keys.clear()
            if hasattr(self._scheduler, 'reset_dirty_tracking'):
                self._scheduler.reset_dirty_tracking()
            self._last_eval_generation = -1
            self._last_view_signature = None  # force re-eval on next request

    def _handle_image_decode_failed(self, data: dict):
        """Apply an async image decode failure on the render thread."""
        node_id = data.get('node_id')
        file_path = data.get('file_path', '')
        error = data.get('error', '')
        node = self._render_nodes.get(node_id)
        if node is not None:
            node.set_load_failed(error)
        logger.warning(f"Image decode failed for {file_path}: {error}")

    # ── Async Image Loader Callbacks ──

    def _on_image_decoded(self, node_id: str, decoded):
        """Handle completed async image decode.

        Called from the decode worker thread via direct callback.
        Submits commands to the render thread queue for GPU upload
        and graph re-evaluation. No direct access to render state.
        """
        # Submit upload command to render thread queue
        self.command_queue.put((CompositeCommand.UPLOAD_IMAGE_CPU, {
            'node_id': node_id,
            '_decoded': decoded,
        }))

        # Trigger re-evaluation with current snapshot
        if self._snapshot is not None:
            self.command_queue.put((CompositeCommand.EVALUATE_GRAPH, {
                'request': RenderRequest(
                    snapshot=self._snapshot,
                    request_id=self._request_id_gen.next_id(),
                    timestamp=time.monotonic(),
                    render_mode=RenderMode.PREVIEW,
                    graph_generation=self._snapshot.generation,
                    color_revision=self._snapshot.color_revision,
                    runtime_context=self._runtime_context,
                    timeline_clip_gpu_provider=getattr(self._eval_ctx, "timeline_clip_gpu_provider", None),
                    edit_project=self._project,
                ),
            }))

    def _on_image_decode_failed(self, node_id: str, file_path: str, error: str):
        """Handle failed async image decode."""
        self.command_queue.put((CompositeCommand.IMAGE_DECODE_FAILED, {
            'node_id': node_id,
            'file_path': file_path,
            'error': error,
        }))

    def _handle_cleanup_node(self, data: dict):
        """Release GPU resources for a node on the render thread.

        Looks up the render-side node instance by node_id and cleans it up,
        then removes it from _render_nodes and invalidates related cache entries.
        """
        node_id = data.get('node_id')
        if node_id and node_id in self._render_nodes:
            node = self._render_nodes.pop(node_id)
            if hasattr(node, 'cleanup'):
                node.cleanup()
            self._invalidate_cache_for_node(node_id)

    def _handle_readback(self, data: dict):
        """Readback the current front buffer to CPU.

        Origin convention (GPU -> CPU boundary):
            FBO readback returns OpenGL bottom-left float32 RGBA.
            np.flipud converts from GL bottom-left to CPU top-left.
            cv2.cvtColor converts from RGB to BGR for CPU consumers.

        Output convention:
            result_holder['data']  = CPU top-left BGR float32 array
            result_holder['data_origin']      = "cpu_top_left"
            result_holder['data_color_order'] = "bgr"
        """
        result_holder = data['result_holder']
        done_event = data['done_event']

        try:
            front_idx = (self.write_idx - 1) % 3  # Most recently completed
            tex = self.tex_ring[front_idx]
            if tex is None:
                done_event.set()
                return

            w, h = tex.size
            fbo = self.fbo_ring[front_idx]
            if fbo is None:
                done_event.set()
                return

            fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            raw = fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
            # GL bottom-left -> CPU top-left: flip once at readback boundary,
            # then convert RGB -> BGR for CPU consumers
            bgr = cv2.cvtColor(np.flipud(arr[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)
            result_holder['data'] = bgr
            result_holder['data_origin'] = "cpu_top_left"
            result_holder['data_color_order'] = "bgr"
        except Exception as e:
            logger.error(f"Readback error: {e}")
            logger.debug("Stack trace:", exc_info=True)
        finally:
            done_event.set()

    def _handle_render_export(self, data: dict) -> None:
        """Run the isolated one-shot export evaluator on this render thread."""
        holder = data.get('result_holder')
        if holder is None:
            holder = {}
        done_event = data.get('done_event')
        color_tex = None
        try:
            from src.features.composite.render.composite_export_renderer import (
                CompositeExportRenderer,
            )

            color_image = np.asarray(data.get('color_image'))
            if color_image.ndim != 3 or color_image.shape[2] < 3:
                raise ValueError("composite export source must be HxWx3 CPU image")
            height, width = color_image.shape[:2]
            rgb = cv2.cvtColor(color_image[..., :3], cv2.COLOR_BGR2RGB)
            if rgb.dtype == np.uint8:
                rgb = rgb.astype(np.float32) / 255.0
            elif rgb.dtype == np.uint16:
                rgb = rgb.astype(np.float32) / 65535.0
            else:
                rgb = rgb.astype(np.float32, copy=False)
            rgb = np.ascontiguousarray(np.flipud(rgb), dtype=np.float16)
            color_tex = self.ctx.texture(
                (int(width), int(height)), 3, data=rgb.tobytes(), dtype='f2'
            )
            color_tex.filter = (moderngl.LINEAR, moderngl.LINEAR)

            original_snapshot = data['snapshot']
            snapshot = replace(
                original_snapshot,
                color_texture_id=int(color_tex.glo),
                color_width=int(width),
                color_height=int(height),
                color_revision=int(getattr(original_snapshot, 'color_revision', 0)) + 1,
            )
            renderer = CompositeExportRenderer(
                interactive_scheduler=data.get('interactive_scheduler'),
                context=self.ctx,
            )
            renderer.set_config_manager(data.get('config_manager'))
            holder['data'] = renderer.render_and_readback(
                snapshot, render_pass=data.get('render_pass', 'beauty')
            )
        except Exception:
            logger.exception("Composite render-thread export failed")
            holder['data'] = None
        finally:
            if color_tex is not None:
                try:
                    color_tex.release()
                except Exception:
                    logger.debug(
                        "Failed to release composite export source texture",
                        exc_info=True,
                    )
            if done_event is not None:
                done_event.set()

    def _handle_readback_present_async(self, data: dict):
        """Async readback for preview display — non-blocking for UI.

        Performs readback on the render thread (not UI thread) and emits
        preview_image_ready signal with the result. The UI thread receives
        the signal and uploads to the viewer-local texture.

        Uses PBO readback when available for pipelined transfer;
        falls back to direct fbo.read() otherwise.

        Args:
            data: dict with keys:
                - 'request_id': Request ID for stale detection
        """
        self._handle_async_readback(data, self.preview_image_ready, "[COMPOSITE-PRESENT]")

    def _ensure_gpu_scope_engine(self):
        """Create + load the shared scope engine on the render thread."""
        if self._gpu_scope_engine is None and not self._gpu_scope_failed:
            from src.shared.gpu.gpu_scope_engine import GpuScopeEngine
            engine = GpuScopeEngine(self.ctx)
            if not engine.load_shaders():
                logger.warning(
                    "Composite GPU scope shaders failed to compile; "
                    "the scope lane stays disabled"
                )
                self._gpu_scope_failed = True
                return None
            self._gpu_scope_engine = engine
        return self._gpu_scope_engine

    def _handle_render_gpu_scope(self, demand) -> None:
        """Run the native GPU scope pipeline over the latest output texture.

        Mirrors AsyncRenderer._do_render_gpu_scope: the analysis texture,
        every density pass and the tone-mapped display textures stay on the
        GPU; only the ``ScopeGpuFrame`` descriptor (texture ids) crosses
        back to the UI thread.
        """
        from src.features.color.core.scope_types import (
            ScopeDemand, ScopeGpuFrame,
            DEFAULT_HISTOGRAM_DISPLAY_SIZE, DEFAULT_CIE_SIZE,
            DEFAULT_FALSECOLOR_SIZE,
        )

        if not isinstance(demand, ScopeDemand):
            return
        engine = self._ensure_gpu_scope_engine()
        if engine is None:
            return
        front_idx = (self.write_idx - 1) % 3
        tex = self.tex_ring[front_idx]
        if tex is None:
            return
        w, h = tex.size
        if w <= 0 or h <= 0:
            return

        t0 = time.monotonic()
        try:
            outputs = engine.render_demand(demand, tex)
            # Cross-context visibility for the UI-thread presenter blit.
            self.ctx.finish()
            frame = ScopeGpuFrame(
                revision=demand.revision,
                source_revision=demand.source_revision,
                waveform_texture_id=wf[0] if (wf := outputs.get('waveform')) else None,
                waveform_size=wf[1] if wf else (0, 0),
                vectorscope_texture_id=vs[0] if (vs := outputs.get('vectorscope')) else None,
                vectorscope_size=vs[1] if vs else (0, 0),
                parade_texture_id=par[0] if (par := outputs.get('parade')) else None,
                parade_size=par[1] if par else (0, 0),
                luma_texture_id=lu[0] if (lu := outputs.get('luma')) else None,
                luma_size=lu[1] if lu else (0, 0),
                histogram_texture_id=hi[0] if (hi := outputs.get('histogram')) else None,
                histogram_size=hi[1] if hi else DEFAULT_HISTOGRAM_DISPLAY_SIZE,
                cie_texture_id=cie[0] if (cie := outputs.get('cie')) else None,
                cie_size=cie[1] if cie else DEFAULT_CIE_SIZE,
                falsecolor_texture_id=fc[0] if (fc := outputs.get('falsecolor')) else None,
                falsecolor_size=fc[1] if fc else DEFAULT_FALSECOLOR_SIZE,
                analysis_ms=(time.monotonic() - t0) * 1000.0,
            )
            self.gpu_scope_ready.emit(frame)
        except Exception as exc:
            logger.warning(
                "[COMPOSITE-SCOPE] GPU scope pass failed: %s: %r",
                type(exc).__name__, exc, exc_info=True,
            )

    def _handle_async_readback(self, data: dict, signal, log_prefix: str,
                               preserve_alpha: bool = False) -> None:
        """Read the current output on the render thread and emit ``signal``."""
        request_id = data.get('request_id', 0)
        try:
            front_idx = (self.write_idx - 1) % 3
            tex = self.tex_ring[front_idx]
            if tex is None:
                return

            w, h = tex.size
            fbo = self.fbo_ring[front_idx]
            if fbo is None:
                return

            fbo.use()
            self.ctx.viewport = (0, 0, w, h)

            # Use PBO readback for pipelined transfer
            if self._pbo_readback is not None:
                if self._pbo_readback.enqueue_sync(
                    fbo, w, h, request_id, preserve_alpha=preserve_alpha
                ):
                    result = self._pbo_readback.get_latest()
                    if result is not None:
                        signal.emit(
                            result.request_id, result.data, result.width, result.height
                        )
                        return

            # Fallback: direct readback (still on render thread, not UI thread)
            raw = fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)
            # GL bottom-left -> CPU top-left: flip once at readback boundary,
            # then convert RGB -> BGR/BGRA for CPU consumers.
            flipped = np.flipud(arr).copy()
            if preserve_alpha:
                bgr = cv2.cvtColor(flipped, cv2.COLOR_RGBA2BGRA)
            else:
                bgr = cv2.cvtColor(flipped[:, :, :3], cv2.COLOR_RGB2BGR)
            signal.emit(request_id, bgr, w, h)
        except Exception as e:
            logger.debug(f"{log_prefix} async readback failed: {e}")

    def _handle_readback_node_thumbnail(self, data: dict):
        """Readback a node's output texture as a thumbnail for UI display.

        This method is called after graph evaluation completes. It reads
        the node's output texture from the cache, downsamples to 64x64,
        and sends the result to the UI thread via node_thumbnail_ready signal.

        Args:
            data: dict with keys:
                - 'node_id': The node ID to read
                - 'output_port': Output port name (default 'result' or 'display')
                - 'thumbnail_size': Target size (default 64)
        """
        node_id = data.get('node_id')
        output_port = data.get('output_port', 'result')
        thumbnail_size = data.get('thumbnail_size', 64)

        if not self._initialized or self._snapshot is None:
            return

        # Get node snapshot to check for output port names
        ns = self._snapshot.nodes.get(node_id)
        if ns is None:
            return

        # Determine the correct output port name
        if ns.node_type == "output":
            output_port = "display"
        elif ns.output_ports:
            # Use first output port
            output_port = ns.output_ports[0][0]
        else:
            output_port = "result"

        # Try to get the node's cached output from the evaluation cache
        # The cache stores TextureResult for each node output
        cache_key = self._node_cache_keys.get(node_id) if hasattr(self, '_node_cache_keys') else None

        if cache_key is None:
            # Try direct cache lookup (fallback)
            # Re-evaluate the node to get its output (expensive, but necessary for thumbnails)
            return

        # Get cached output from CacheManager
        entry = self._cache_manager.get(cache_key)
        if entry is None:
            return

        tex_result = entry.outputs.get(output_port)
        if tex_result is None or not tex_result.is_valid:
            return

        try:
            # Calculate thumbnail dimensions preserving source aspect ratio
            src_w = tex_result.width or 1
            src_h = tex_result.height or 1
            scale = thumbnail_size / max(src_w, src_h)
            thumb_w = max(1, int(src_w * scale))
            thumb_h = max(1, int(src_h * scale))

            # Readback the texture at thumbnail resolution
            thumb_data = self._readback_texture_as_thumbnail(
                tex_result, thumb_w, thumb_h
            )
            if thumb_data is not None:
                # Convert to bytes for thread-safe signal emission
                thumb_bytes = thumb_data.tobytes()
                self.node_thumbnail_ready.emit(
                    node_id,
                    thumb_w,
                    thumb_h,
                    thumb_bytes
                )
        except Exception as e:
            logger.warning(f"Thumbnail readback error for {node_id}: {e}")
            logger.debug("Stack trace:", exc_info=True)

    def _readback_texture_as_thumbnail(self, tex_result: TextureResult,
                                        thumb_w: int = 64, thumb_h: int = 64) -> Optional[np.ndarray]:
        """Readback a texture and downsample to thumbnail size.

        Args:
            tex_result: TextureResult with texture_id, width, height
            thumb_w: Target thumbnail width
            thumb_h: Target thumbnail height

        Returns:
            BGR float32 numpy array of size (thumb_h, thumb_w, 3)
            or None on failure.
        """
        if tex_result is None or not tex_result.is_valid:
            return None

        tex_id = tex_result.texture_id

        # Create temporary FBO for readback
        # For efficiency, we read at a reduced resolution using mipmaps or direct read
        # ModernGL doesn't have built-in mipmap read, so we'll use a simple approach

        thumb_tex = None
        try:
            # Use utility shader to render to a small thumbnail FBO
            prog = self.utility_shaders.get('passthrough')
            if prog is None:
                return None

            # Create thumbnail texture and FBO
            thumb_tex = self._resource_pool.acquire_texture(thumb_w, thumb_h)
            thumb_fbo = self.fb_manager.get_fbo(thumb_tex)

            thumb_fbo.use()
            self.ctx.viewport = (0, 0, thumb_w, thumb_h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            # Bind source texture and render
            self._bind_external_texture(tex_id)
            prog['u_texture'].value = 0
            self._render_quad(prog)

            # Readback thumbnail
            raw = thumb_fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(thumb_h, thumb_w, 4)

            # Flip and convert to BGR
            bgr = cv2.cvtColor(np.flipud(arr[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)

            return bgr

        except Exception as e:
            logger.warning(f"Texture thumbnail readback error: {e}")
            return None
        finally:
            # AUD-8203: the pooled texture must be returned on EVERY exit —
            # the old code released it only on the success path, so any
            # exception between acquire and release leaked a pool texture
            # on every failing thumbnail readback.
            if thumb_tex is not None:
                try:
                    self._resource_pool.release_texture(thumb_tex)
                except Exception as release_exc:
                    logger.warning(
                        "Thumbnail texture release failed: %s", release_exc)

    def _handle_ai_result_ready(self, data: dict):
        """Handle AI computation completion — re-evaluate with current snapshot."""
        if not self._initialized or self._snapshot is None:
            return
        # Clear eval cache to force re-evaluation of AI nodes
        self._clear_eval_cache()
        # Use current request_id for tracking (AI triggers don't have their own request)
        result = self._evaluate_snapshot()
        # AUD-8201: align with _handle_evaluate_graph / the playback drive
        # loop — transient textures must be returned to the pool right after
        # presenting (or suppressing) the result, or repeated AI_RESULT_READY
        # deliveries hold pool textures indefinitely.
        # AUD-8206: read the request id through the scheduler's locked
        # accessor instead of its private _latest_request_id.
        latest_request_id = self._scheduler.latest_request_id
        try:
            if result and result.is_valid:
                self._render_to_output(result, request_id=latest_request_id)
            else:
                self.frame_ready.emit(latest_request_id, 0, 0, 0)
        finally:
            self._release_transient_textures()

    # ── AI Manager Access ──

    @property
    def ai_compute_manager(self):
        """Access the AI compute manager for signal connections."""
        return getattr(self, '_ai_compute_manager', None)

    @property
    def ai_cache_manager(self):
        """Access the AI cache manager."""
        return getattr(self, '_ai_cache_manager', None)

    @property
    def perf_monitor(self):
        """Access the performance monitor for UI thread diagnostics."""
        return getattr(self, '_perf_monitor', None)

    # ── Output Rendering ──

    @staticmethod
    def _build_aov_metadata(result: TextureResult) -> dict:
        """Describe auxiliary render passes for the UI without sharing ownership.

        The renderer remains the owner of every AOV texture.  The viewer uses
        these descriptors to copy the pass into a viewer-local texture before
        displaying it, so a pass can be inspected without creating a second
        GL-resource ownership path.
        """
        aovs = {}
        for name, texture_id in sorted(result.aov_texture_ids.items()):
            texture_id = int(texture_id)
            if texture_id <= 0:
                continue
            width, height = result.aov_dimensions.get(
                name, (result.width, result.height))
            pass_name = str(name)
            if pass_name == "depth":
                storage = "r32f"
            elif pass_name in ("object_id", "material_id"):
                storage = "rgba8"
            else:
                storage = "rgba16f"
            aovs[pass_name] = {
                "texture_id": texture_id,
                "width": int(width),
                "height": int(height),
                "storage": storage,
            }
        return aovs

    def _render_to_output(self, result: TextureResult, request_id: int = 0):
        """Render a texture result to the output buffer.

        B1: when a user viewer ROI is active and the evaluated result is a
        ROI-sized sub-image (cropped evaluation chain), the sub-image is
        composited back into a FULL-FRAME output buffer at its offset — the
        viewer display path, pixel inspector and overlays stay unchanged.

        Args:
            result: TextureResult from graph evaluation.
            request_id: Request ID for UI stale detection.
        """
        w = result.width or 1
        h = result.height or 1

        # B1: full-frame recomposite when the chain was ROI-cropped.
        roi = self._active_viewer_roi
        fmt = result.format
        out_w, out_h = w, h
        if (roi is not None and fmt is not None
                and (w < fmt.width or h < fmt.height)):
            out_w = int(fmt.width)
            out_h = int(fmt.height)
            if hasattr(roi, "clamped"):
                # clamped() takes the ImageFormat, not two ints.
                roi = roi.clamped(fmt)
            # The chain may have cropped its sources to an EXPANDED roi
            # (spatial-effect padding) — the result's data window is the
            # authoritative blit rect then; falling back to the requested
            # roi would squash the larger texture into a smaller viewport.
            bbox = getattr(result, "bbox", None)
            if (bbox is not None and not bbox.is_empty
                    and (bbox.width != int(roi.width)
                         or bbox.height != int(roi.height))):
                roi = bbox
        else:
            roi = None

        self._ensure_output_buffers(out_w, out_h)

        fbo = self.fbo_ring[self.write_idx]
        fbo.use()
        self.ctx.viewport = (0, 0, out_w, out_h)
        self.ctx.clear(0.0, 0.0, 0.0, 0.0)

        prog = self.utility_shaders.get('passthrough')
        if prog is None:
            return

        if roi is not None:
            # Blit the ROI sub-image into place (GL viewport origin is
            # bottom-left, matching BBox's y-up convention).
            self.ctx.viewport = (int(roi.x), int(roi.y),
                                 int(roi.width), int(roi.height))

        self._bind_external_texture(result.texture_id)
        prog['u_texture'].value = 0
        self._render_quad(prog)

        # Ensure GPU finishes writing to output texture before the viewer's
        # GL context (shared) reads it.  On macOS cross-context texture
        # sharing is NOT implicitly synchronised — without a finish/flush
        # the viewer can segfault accessing an incomplete texture.
        self.ctx.finish()

        # Insert a fence so the resource pool knows when GPU is done with
        # textures released during this frame (prevents reuse-while-in-flight).
        if self._resource_pool:
            self._resource_pool.insert_fence()

        front_idx = self.write_idx
        self.write_idx = (self.write_idx + 1) % 3
        out_tex_id = self.tex_ring[front_idx].glo
        self.frame_ready.emit(request_id, out_tex_id, out_w, out_h)

        # Emit metadata for viewport overlays
        fmt_data = None
        if result.format is not None:
            fmt_data = {
                "width": result.format.width,
                "height": result.format.height,
                "pixel_aspect": result.format.pixel_aspect,
                "name": result.format.name,
                "origin_x": result.format.origin_x,
                "origin_y": result.format.origin_y,
            }
        bbox_data = None
        if result.bbox is not None:
            bbox_data = {
                "x": result.bbox.x, "y": result.bbox.y,
                "width": result.bbox.width, "height": result.bbox.height,
            }
        # M2: 附带四视图正交纹理 glo（Scene3D 渲染后写入 eval_ctx 旁路）。
        # viewport 据此 blit 到 quad 各象限；None 表示本帧无正交纹理。
        ortho_views = None
        # P7：附带 ID picking 信息（ID texture glo + pick_map）。
        id_pick = None
        eval_ctx_ref = getattr(self, '_eval_ctx', None)
        if eval_ctx_ref is not None:
            ortho_views = getattr(eval_ctx_ref, 'ortho_view_textures', None)
            id_pick = getattr(eval_ctx_ref, 'id_pick_info', None)
        # P0 / Milestone A：附带 render_diagnostics（含 perf_stats）给 viewer debug overlay。
        render_diagnostics = getattr(result, "render_diagnostics", None)
        self.metadata_ready.emit({
            "format": fmt_data, "bbox": bbox_data,
            "ortho_views": ortho_views,
            "id_pick": id_pick,
            "aov": self._build_aov_metadata(result),
            "render_diagnostics": render_diagnostics,
        })

    def _release_output_buffers(self) -> None:
        """Retire the output ring without releasing in-flight GL objects."""
        self._drain_deferred_output_releases()
        pairs: list[tuple[object, object]] = []
        # ModernGL framebuffers retain their attachment references.  Release the
        # FBO before its texture; the reverse order can leave a framebuffer
        # referring to a deleted texture during a Transform-driven resize.
        for i in range(3):
            fbo = self.fbo_ring[i]
            tex = self.tex_ring[i]
            self.fbo_ring[i] = None
            self.tex_ring[i] = None
            if fbo is not None or tex is not None:
                pairs.append((fbo, tex))

        if pairs:
            fence = self._create_output_release_fence()
            if fence is None:
                for fbo, tex in pairs:
                    self._release_output_pair(fbo, tex)
            else:
                self._deferred_output_releases.append((fence, pairs))
        self.write_idx = 0

    @staticmethod
    def _release_output_pair(fbo, tex) -> None:
        """Release an output FBO before its attached texture."""
        if fbo is not None:
            try:
                fbo.release()
            except Exception as exc:
                logger.debug("Output FBO release failed: %s", exc)
        if tex is not None:
            try:
                tex.release()
            except Exception as exc:
                logger.debug("Output texture release failed: %s", exc)

    def _create_output_release_fence(self):
        """Create a producer fence before retiring shared output textures.

        The method is called only on the composite render thread.  If sync
        objects are unavailable, ``finish`` is the safe fallback and the
        caller may release immediately.
        """
        if self.__dict__.get("ctx") is None:
            return None
        try:
            from OpenGL.GL import (
                GL_SYNC_GPU_COMMANDS_COMPLETE,
                glFenceSync,
                glFlush,
            )
            fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            glFlush()
            if fence:
                return fence
        except Exception as exc:
            logger.debug("Output release fence creation failed: %s", exc)

        finish = getattr(self.__dict__.get("ctx"), "finish", None)
        if callable(finish):
            try:
                finish()
            except Exception as exc:
                logger.debug("Output release finish fallback failed: %s", exc)
        return None

    def _drain_deferred_output_releases(self, block: bool = False) -> int:
        """Release retired output pairs after their render fence completes."""
        # ``__new__``-constructed renderer doubles are used by no-GL tests;
        # QObject's attribute machinery raises before QThread.__init__.
        pending = self.__dict__.get("_deferred_output_releases", [])
        if not pending:
            return 0

        try:
            from OpenGL.GL import (
                GL_ALREADY_SIGNALED,
                GL_CONDITION_SATISFIED,
                glClientWaitSync,
                glDeleteSync,
            )
        except Exception:
            if not block:
                return 0
            finish = getattr(getattr(self, "ctx", None), "finish", None)
            if callable(finish):
                try:
                    finish()
                except Exception as exc:
                    logger.debug("Output release drain finish failed: %s", exc)
            for _, pairs in pending:
                for fbo, tex in pairs:
                    self._release_output_pair(fbo, tex)
            self._deferred_output_releases = []
            return sum(len(pairs) for _, pairs in pending)

        remaining = []
        released = 0
        for fence, pairs in pending:
            ready = False
            try:
                timeout_ns = 50_000_000 if block else 0
                status = glClientWaitSync(fence, 0, timeout_ns)
                ready = status in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED)
            except Exception as exc:
                logger.debug("Output release fence wait failed: %s", exc)

            if not ready and block:
                finish = getattr(getattr(self, "ctx", None), "finish", None)
                if callable(finish):
                    try:
                        finish()
                    except Exception as exc:
                        logger.debug("Output release drain finish failed: %s", exc)
                ready = True

            if not ready:
                remaining.append((fence, pairs))
                continue

            try:
                glDeleteSync(fence)
            except Exception as exc:
                logger.debug("Output release fence delete failed: %s", exc)
            for fbo, tex in pairs:
                self._release_output_pair(fbo, tex)
                released += 1

        self._deferred_output_releases = remaining
        return released

    def _ensure_output_buffers(self, w: int, h: int):
        """Create or resize output FBOs if needed."""
        needs_create = False
        if self.tex_ring[0] is None:
            needs_create = True
        elif self.tex_ring[0].size != (w, h):
            self._release_output_buffers()
            needs_create = True

        if needs_create:
            try:
                for i in range(3):
                    tex = self.ctx.texture((w, h), 4, dtype='f2')
                    tex.filter = (moderngl.LINEAR, moderngl.LINEAR)
                    tex.repeat_x = False
                    tex.repeat_y = False
                    fbo = self.ctx.framebuffer(color_attachments=[tex])
                    self.tex_ring[i] = tex
                    self.fbo_ring[i] = fbo
            except Exception:
                # Leave the ring in a known-empty state if allocation only
                # succeeds part way through.  The next request can safely retry.
                self._release_output_buffers()
                raise

    def _cleanup(self) -> None:
        """Release all GPU resources exactly once on the render thread."""
        with self._cleanup_lock:
            if self._cleanup_started:
                return
            self._cleanup_started = True

        def safe_call(label: str, callback: Callable[[], None]) -> None:
            try:
                callback()
            except Exception as exc:
                logger.warning("Composite cleanup failed for %s: %s", label, exc)

        try:
            stabilization_runner = self.__dict__.pop('_stabilization_runner', None)
            if stabilization_runner is not None:
                safe_call('stabilization runner', stabilization_runner.release)
            # Cancel workers before releasing the GL objects they may use.
            ai_manager = getattr(self, '_ai_compute_manager', None)
            if ai_manager:
                safe_call("AI workers", ai_manager.cancel_all)

            image_loader = getattr(self, '_image_loader', None)
            if image_loader:
                safe_call("image-loader workers", image_loader.cancel_all)

            pbo_readback = getattr(self, '_pbo_readback', None)
            if pbo_readback:
                safe_call("PBO readback", pbo_readback.cleanup)
                self._pbo_readback = None

            # Shared GPU scope engine owns every scope texture/FBO/program.
            # Hold the global GL mutex while releasing: the UI-thread
            # presenter may be mid-blit of one of these texture ids, and
            # glIsTexture passing just before deletion would otherwise race.
            scope_engine = getattr(self, '_gpu_scope_engine', None)
            if scope_engine is not None:
                scope_mutex = GLContextManager.instance().mutex
                scope_mutex.lock()
                try:
                    safe_call("GPU scope engine", scope_engine.release)
                finally:
                    scope_mutex.unlock()
                self._gpu_scope_engine = None

            safe_call("output buffers", self._release_output_buffers)
            quad_vbo = getattr(self, '_quad_vbo', None)
            if quad_vbo is not None:
                safe_call("quad VBO", quad_vbo.release)
                self._quad_vbo = None
            vao_cache = getattr(self, '_vao_cache', None)
            if vao_cache is not None:
                for vao in list(vao_cache.values()):
                    safe_call("cached VAO", vao.release)
                vao_cache.clear()

            # Clear cache (releases owned textures) before the resource pool.
            if self._cache_manager:
                safe_call("cache", self._cache_manager.clear)
            if self._resource_pool:
                safe_call("resource pool", self._resource_pool.cleanup)
            elif self.fb_manager:
                safe_call("framebuffer manager", self.fb_manager.cleanup)

            for node in list(self._render_nodes.values()):
                if hasattr(node, 'cleanup'):
                    safe_call("render node", node.cleanup)
            self._render_nodes.clear()

            # MeshVBOPool is process-global but context-scoped. Node cleanup
            # releases normal references; this final sweep also covers LOD or
            # partially-built VAO paths that never made it into a node's live
            # list. It must run while the render context is still current.
            render_ctx = getattr(self, "ctx", None)
            if render_ctx is not None:
                def release_mesh_vbo_pool() -> None:
                    from ..render3d.mesh_vbo_pool import MeshVBOPool
                    MeshVBOPool.release_context(render_ctx)

                safe_call("mesh VBO pool", release_mesh_vbo_pool)

            shader_lib = getattr(self, '_shader_lib', None)
            if shader_lib:
                safe_call("shader library", shader_lib.release)
            compute_manager = getattr(self, '_compute_shader_manager', None)
            if compute_manager:
                safe_call("compute shader manager", compute_manager.release)
            safe_call("deferred output releases",
                      lambda: self._drain_deferred_output_releases(block=True))
        finally:
            self._initialized = False
            logger.info("Cleaned up")
            self._cleanup_complete.set()

    # ── Prefetch Handlers ──

    def _handle_prefetch_evaluate(self, data: dict):
        """Handle prefetch evaluation request.

        Evaluates the graph against a snapshot carrying the PREDICTED
        parameter value and stores the resulting node outputs under the
        PREFETCH cache category (short-lived, aggressively evicted,
        promoted to EFFECT on the real hit — CacheManager's designed
        prefetch lifecycle). When the user settles on the predicted value,
        the real request computes the same content-addressed key and hits
        the warmed entry instead of re-rendering (AUD-8202: this handler
        used to build modified snapshots and then evaluate nothing, so
        prefetch silently never worked).

        Prefetch never presents a frame: it only warms the cache on the
        render thread during the UI debounce window. Any failure is
        best-effort — logged and dropped, never propagated to the caller.
        """
        from dataclasses import replace as _dc_replace
        from .cache_manager import CacheCategory
        from .render_scheduler import PrefetchRequest  # noqa: F401 — type only

        prefetch_request: Optional[PrefetchRequest] = data.get('prefetch_request')
        if prefetch_request is None or not self._initialized:
            return

        snapshot = prefetch_request.snapshot
        if snapshot is None or self._snapshot is None:
            return
        cache_mgr = getattr(self, '_cache_manager', None)
        if cache_mgr is None:
            return

        target_node_id = prefetch_request.node_id
        param_name = prefetch_request.param_name
        node_snapshot = snapshot.nodes.get(target_node_id)
        if node_snapshot is None or param_name not in node_snapshot.parameters:
            # Unknown node/parameter (graph rebuilt mid-debounce): nothing
            # sensible to predict — a wrong-key entry would be dead weight.
            return

        # 1. Build the predicted snapshot: one node's parameter replaced,
        #    everything else shared (dataclass replace keeps it cheap).
        modified_params = dict(node_snapshot.parameters)
        modified_params[param_name] = prefetch_request.predicted_value
        predicted_snapshot = _dc_replace(
            snapshot,
            nodes={**snapshot.nodes,
                   target_node_id: _dc_replace(node_snapshot,
                                               parameters=modified_params)},
        )

        # 2. Evaluate the predicted snapshot with cache writes tagged
        #    PREFETCH. The tagger wraps the manager's public put() for the
        #    evaluation window only; source-node entries keep their
        #    protected auto category (their keys are parameter-independent
        #    and demoting them would lose eviction protection).
        source_node_types = getattr(type(cache_mgr), '_SOURCE_NODE_TYPES',
                                    frozenset())
        original_put = cache_mgr.put

        def _prefetch_tagging_put(key, entry, *args, **kwargs):
            if kwargs.get('category') is None:
                if key.split(':')[0] not in source_node_types:
                    kwargs['category'] = CacheCategory.PREFETCH
            return original_put(key, entry, *args, **kwargs)

        original_snapshot = self._snapshot
        try:
            self._snapshot = predicted_snapshot
            cache_mgr.put = _prefetch_tagging_put
            self._prefetch_mode = True
            self._prefetch_node_id = target_node_id
            self._prefetch_param_name = param_name
            self._prefetch_predicted_value = prefetch_request.predicted_value
            self._evaluate_snapshot(
                request_id=prefetch_request.request_id,
                render_mode="preview",
                request_timestamp=prefetch_request.timestamp,
            )
        except Exception as exc:  # noqa: BLE001 — prefetch 是尽力而为，不影响主渲染路径
            logger.debug("Prefetch evaluation failed for node %s (%s=%r): %s",
                         target_node_id, param_name,
                         prefetch_request.predicted_value, exc, exc_info=True)
        finally:
            cache_mgr.put = original_put
            self._snapshot = original_snapshot
            self._prefetch_mode = False
            self._prefetch_node_id = None
            self._prefetch_param_name = None
            self._prefetch_predicted_value = None
            # Same discipline as every other evaluation site: hand the
            # scratch textures gathered by this evaluation back to the pool.
            self._release_transient_textures()

    def _handle_clear_prefetch_cache(self, data: dict):
        """Clear prefetch cache entries when prediction was incorrect."""
        if self._cache_manager:
            self._cache_manager.clear_prefetch_cache()
