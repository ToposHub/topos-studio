"""
Composite Export Renderer - One-shot full-resolution composite evaluation.

Evaluates the composite graph at full resolution using a ModernGL context
owned by the caller. Application exports pass the existing composite render
thread context; standalone CLI callers may still create a fresh context.

Supports two rendering paths:
- Full-frame: single-pass evaluation (for images <= 2048px).
- Tiled: tile-by-tile evaluation (for images > 2048px), with automatic
  padding for spatial effects (blur, transform) to ensure seamless boundaries.

Architecture:
- Shares RenderScheduler semantics with CompositeRenderer.
- Accepts an optional interactive_scheduler to seed dirty tracking,
  so export plan generation can detect unchanged nodes.
- Returns structured ExportResult with image data, error info, and diagnostics.
- Cache isolation: export creates its own CacheManager/GPUResourcePool
  by design, never touching the interactive renderer's cache.

Lifecycle:
1. Created on the owning render thread during export
2. render_and_readback() called with a GraphSnapshot
3. Evaluates the graph, reads back the result
4. cleanup() releases all GPU resources
5. Instance discarded after use
"""

import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import cv2
import moderngl
import numpy as np

from src.shared.gpu.gl_backend import FramebufferManager
from .cache_manager import CacheManager
from ..core.format import ImageFormat
from src.features.composite.evaluators.graph_evaluator_mixin import GraphEvaluatorMixin, TileContext
from ..core.graph_snapshot import GraphSnapshot
from ..core.node_capabilities import is_output_node_type
from .gpu_resource_pool import GPUResourcePool
from ..nodes.base_node import TextureResult
from .render_scheduler import RenderMode, RenderScheduler, RenderPlan

if TYPE_CHECKING:  # pragma: no cover - annotation-only import
    from src.shared.gpu.composite_gpu_frame_handle import CompositeGpuFrameHandle


logger = logging.getLogger(__name__)

# ── Export Result ──

@dataclass
class ExportResult:
    """Structured result from export rendering.

    Attributes:
        image: BGR float32 numpy array (h, w, 3) in [0, 1] range, or None on failure.
        error: Error message if export failed, None on success.
        used_tiling: Whether tile rendering was used.
        tile_count: Number of tiles rendered (0 for full-frame).
        render_time_ms: Total wall-clock render time in milliseconds.
    """
    image: Optional[np.ndarray] = None
    error: Optional[str] = None
    used_tiling: bool = False
    tile_count: int = 0
    render_time_ms: float = 0.0
    # T-P1-60 步骤 6: honest stage attribution. None = not measured on
    # this path (never reported as 0) — mirrors the frame-trace rule.
    eval_ms: Optional[float] = None
    readback_ms: Optional[float] = None
    # P0-1: which large-frame preview strategy was chosen ("full_frame",
    # "tiled_gpu_stitch", "tiled_cpu_fallback") and why. None for frames
    # below the tiling threshold.
    preview_strategy: Optional[str] = None
    preview_strategy_reason: Optional[str] = None
    # Optional multi-pass payload. ``image`` remains the backwards-compatible
    # beauty result; ``passes`` is populated by render_and_readback_passes().
    passes: Dict[str, np.ndarray] = field(default_factory=dict)
    # P0-4: per-frame trace dict (trace_id, strategy, timings, transfer
    # bytes, fallback reason). Attached to every result for programmatic
    # consumers; emitted through frame_trace.set_trace_sink when enabled.
    frame_trace: Optional[Dict[str, Any]] = None
    # T-P1-60: node-level structured diagnostics from the evaluation
    # (e.g. {"code": "MODEL_UNAVAILABLE", "node_id", "node_type"} for
    # AI placeholder nodes without a model pack). Empty when clean.
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)


class CompositeExportRenderer(GraphEvaluatorMixin):
    """One-shot renderer for exporting composite graphs at full resolution.

    Runs on the owning render thread using its current GL context. Created for
    a single export operation and discarded after use.

    For images larger than TILE_THRESHOLD, uses tile-based rendering to
    bound GPU memory usage. Each tile is evaluated independently with
    source textures cropped to the tile region (plus padding).

    Args:
        interactive_scheduler: Optional RenderScheduler from the interactive
            renderer. If provided, dirty tracking state is seeded from it
            so the export can detect unchanged nodes. If None (standalone),
            all nodes are evaluated (no incremental optimization).
    """

    # Images with max dimension > this use tile rendering
    TILE_THRESHOLD = 2048
    DEFAULT_TILE_SIZE = 1024

    def __init__(
        self,
        interactive_scheduler: Optional[RenderScheduler] = None,
        context: Optional[moderngl.Context] = None,
    ) -> None:
        self.ctx: Optional[moderngl.Context] = context
        self._wrapped_context = context is not None
        self.fb_manager: Optional[FramebufferManager] = None
        self.utility_shaders: Dict[str, moderngl.Program] = {}
        self._render_nodes: Dict[str, object] = {}
        self._cache_manager: Optional[CacheManager] = None
        self._resource_pool: Optional[GPUResourcePool] = None
        self._snapshot: Optional[GraphSnapshot] = None
        self._scheduler = RenderScheduler()
        self._quad_vbo = None
        self._vao_cache = {}
        self._tile_context = None
        self._runtime_context = None
        self._timeline_clip_gpu_provider = None
        # Persistent mode: keep GL resources (shaders, resource pool, render
        # nodes) alive across frames instead of rebuilding them per render.
        # Used by the Edit comp-clip preview path, which drives this renderer
        # frame-by-frame; the one-shot export path keeps the default
        # build-render-teardown behaviour.
        self._persistent = False
        # ConfigManager for colour-engine selection (builtin vs OCIO), used by
        # nodes that do their own colour conversion (FileOutputNode). None →
        # OCIO default. Set via set_config_manager() before rendering.
        self._config_manager = None
        # P0-5: GL fences handed out via CompositeGpuFrameHandle. Consumers
        # call release() on their own thread, usually with no GL context
        # current — glDeleteSync there fails silently and leaks one sync
        # object per frame. Instead, release callbacks only QUEUE the fence
        # here; the next producer-side GL operation (this renderer, with its
        # context current) drains the queue. deque.append is thread-safe.
        self._deferred_fence_deletes: "deque[int]" = deque()

        # Seed dirty tracking from interactive scheduler if available
        if interactive_scheduler is not None:
            self._scheduler.prepare_for_export(interactive_scheduler)

    def set_persistent_mode(self, enabled: bool) -> None:
        """Enable/disable reuse of GL resources across frames.

        When enabled, the renderer must be called repeatedly with the same GL
        context current (e.g. the preview worker's shared context). Shaders,
        resource pool and render nodes are created once and reused; cleanup()
        is only performed on explicit teardown, not after every frame.
        """
        self._persistent = bool(enabled)

    def set_timeline_clip_context(
        self,
        runtime_context: object | None = None,
        timeline_clip_gpu_provider: object | None = None,
    ) -> None:
        """Provide timeline source context for TimelineClipInput nodes."""
        self._runtime_context = runtime_context
        self._timeline_clip_gpu_provider = timeline_clip_gpu_provider

    def set_config_manager(self, config_manager: object | None) -> None:
        """Provide the ConfigManager so nodes honour composite.color_engine.

        FileOutputNode and any node that runs its own colour conversion read
        this (via CompositeEvalContext) to select builtin vs OCIO instead of
        always defaulting to OCIO.
        """
        self._config_manager = config_manager

    def _install_eval_context(self) -> None:
        """Install CompositeEvalContext after GL resources are initialized."""
        from src.features.composite.core.eval_context import CompositeEvalContext
        self._eval_ctx = CompositeEvalContext(
            gl=self.ctx,
            runtime_context=self._runtime_context,
            caption_input=getattr(self._runtime_context, "caption_input", None),
            timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
            resource_pool=self._resource_pool,
            framebuffer_manager=self.fb_manager,
            config_manager=self._config_manager,
        )
        # R-13：_ensure_gpu_resources 会在渲染中途重建 eval context——
        # 新 ctx 必须继承本次渲染已解析的项目运动快门，否则
        # use_project_shutter 的 VectorBlur 在导出路径回落节点自身值
        # （预览/导出快门不一致）。multi-pass/tiled 入口同样经由这里
        # 获得 pending 值。
        pending_shutter = getattr(self, "_pending_project_shutter", None)
        if pending_shutter is not None:
            self._eval_ctx.motion_blur_shutter = pending_shutter
        # R-14：全局 3D 场景成品设置同一模式继承——预览由页面注入
        # ctx.global_3d_scene_settings，导出路径此前不安装，Scene3D
        # 回落节点自身旧默认，相机/环境/背景/IBL/输出开关与预览不一致。
        pending_3d = getattr(self, "_pending_global_3d_settings", None)
        if pending_3d is not None:
            self._eval_ctx.global_3d_scene_settings = pending_3d

    def _apply_project_motion_blur_shutter(self, snapshot: GraphSnapshot) -> None:
        """Resolve + apply the composition-level shutter for this render (R-13).

        先存 pending（供渲染途中 _install_eval_context 重建 ctx 时继承），
        再立即应用到现存 ctx。
        """
        from src.features.composite.core.eval_context import (
            resolve_project_motion_blur_shutter,
        )
        self._pending_project_shutter = resolve_project_motion_blur_shutter(
            snapshot)
        eval_ctx = getattr(self, "_eval_ctx", None)
        if eval_ctx is not None:
            eval_ctx.motion_blur_shutter = self._pending_project_shutter

    def _apply_global_3d_scene_settings(self, snapshot: GraphSnapshot) -> None:
        """Install the persisted global 3D scene settings for this render (R-14).

        与预览同一来源（viewer_state.default_3d_scene）与同一 merge 合同；
        与快门相同先存 pending——渲染中途重建的 eval context 经
        _install_eval_context 继承。
        """
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings,
        )
        raw = getattr(snapshot, "global_3d_scene_settings", None) or {}
        try:
            merged = merged_viewer3d_scene_settings(dict(raw))
        except Exception:
            merged = dict(raw)
        self._pending_global_3d_settings = merged
        eval_ctx = getattr(self, "_eval_ctx", None)
        if eval_ctx is not None:
            eval_ctx.global_3d_scene_settings = merged

    def render_and_readback(self, snapshot: GraphSnapshot,
                            readback_output: str = "linear_f32",
                            render_pass: str = "beauty",
                            trace_id: str = "") -> ExportResult:
        """Evaluate composite graph and readback result as BGR numpy array.

        Automatically selects between tiled and full-frame rendering based
        on output image dimensions and GPU memory budget.

        Uses RenderPlan's tiling analysis to determine the optimal tile size
        and padding for seamless boundaries.

        Must be called with a current OpenGL context on the calling thread.

        Args:
            snapshot: Immutable graph snapshot containing nodes, connections,
                      and ColorOutput texture state.
            readback_output: "linear_f32" (export default, scene-linear
                      float32) or "srgb_u8" (preview: sRGB uint8 produced
                      entirely on the GPU; oversized previews choose a
                      full-frame or GPU-stitched tile path via
                      _decide_preview_render_strategy — see P0-1).
            render_pass: Semantic pass to read back. ``beauty`` is the
                         default; auxiliary passes are forced through the
                         full-frame path because their dimensions/format may
                         differ from the beauty tile contract.
            trace_id: P0-4 correlation id supplied by the caller (Edit
                      timeline / encoder). Empty → a process-unique id is
                      generated.

        Returns:
            ExportResult with image data on success, or error message on failure.
        """
        # CP-021/R-13: 每次导出按当前图刷新合成级快门（Output 节点参数）。
        # 经 _apply_project_motion_blur_shutter 存 pending——渲染中途
        # _ensure_gpu_resources 重建 ctx 也会继承该值。
        self._apply_project_motion_blur_shutter(snapshot)
        # R-14：全局 3D 场景设置同合同安装。
        self._apply_global_3d_scene_settings(snapshot)
        self._readback_output = readback_output
        # Auxiliary AOVs are numeric data, never display-referred colour.
        # Force the linear readback path even when the caller is rendering a
        # preview frame (otherwise the sRGB readback shader would alter depth,
        # normal, ID, or motion-vector values).
        if str(render_pass or "beauty") != "beauty":
            self._readback_output = "linear_f32"
        # P0-4: per-frame trace — created here, enriched by the inner
        # renderer, emitted/attached at every exit path.
        from .frame_trace import CompositeFrameTrace
        trace = CompositeFrameTrace(
            frame=int(getattr(snapshot, "frame_number", 0) or 0),
            generation=int(getattr(snapshot, "generation", 0) or 0),
            quality="export" if self._readback_output == "linear_f32"
            else "preview",
            producer="readback",
            readback_format=self._readback_output,
            node_count=len(getattr(snapshot, "nodes", {}) or {}),
        )
        if trace_id:
            trace.trace_id = str(trace_id)
        self._active_trace = trace
        t0 = time.monotonic()
        # Analysis-only plan: must NOT consume dirty tracking. The
        # evaluator's own plan() call inside _render_full_frame is the one
        # that advances the dirty state — advancing it here made every
        # later plan() compare equal against the just-stored state and
        # skip ALL nodes with cached outputs, so parameter edits never
        # re-rendered on the readback path until a full cache clear (P1-8).
        plan: RenderPlan = self._scheduler.plan(
            snapshot, RenderMode.EXPORT, update_tracking=False)

        output_w, output_h = plan.output_dimensions or self._get_output_dimensions(snapshot)
        trace.target_w = int(output_w)
        trace.target_h = int(output_h)

        # Use RenderPlan's tiling decision
        if plan.needs_tiling and str(render_pass or "beauty") == "beauty":
            if self._readback_output == "srgb_u8":
                # P0-1: preview frames must never take the per-tile
                # linear_f32 readback path (4 x 2048² f4 sync readbacks +
                # numpy stitch ≈ 500ms/frame at 4K while the GPU evaluation
                # itself costs ~5ms). Pick the cheapest GPU path the memory
                # budget allows instead.
                return self._finish_trace(self._render_preview_large_frame(
                    snapshot, output_w, output_h, plan), trace, t0)
            # Tile readback path is linear-only (per-tile CPU stitch).
            self._readback_output = "linear_f32"
            return self._finish_trace(self._render_tiled_with_plan(
                snapshot, output_w, output_h, plan), trace, t0)
        else:
            return self._finish_trace(
                self._render_full_frame(snapshot, render_pass=render_pass),
                trace, t0)

    def _finish_trace(self, result: ExportResult,
                      trace, t0: float) -> ExportResult:
        """Fill the remaining trace fields from the finished result."""
        from .frame_trace import emit_trace
        # T-P1-60：attach node-level diagnostics collected during this
        # evaluation (MODEL_UNAVAILABLE etc.) to the structured result.
        node_dx = self.consume_node_diagnostics()
        if node_dx:
            result.diagnostics = [*node_dx, *result.diagnostics]
        # T-P1-60 步骤 6：stage attribution 进 trace（None = 该路径未测）。
        if result.eval_ms is not None:
            trace.eval_ms = result.eval_ms
        if result.readback_ms is not None:
            trace.readback_ms = result.readback_ms
        trace.total_ms = (time.monotonic() - t0) * 1000.0
        trace.strategy = result.preview_strategy or (
            "tiled_cpu_fallback" if result.used_tiling else "full_frame")
        trace.strategy_reason = result.preview_strategy_reason or ""
        trace.tile_count = result.tile_count
        if result.image is not None:
            trace.cpu_download_bytes = int(result.image.nbytes)
        if result.error:
            trace.fallback_reason = result.error
        info = getattr(self.ctx, "info", {}) or {}
        trace.device_identity = str(info.get("GL_RENDERER", ""))
        trace.gl_version = str(info.get("GL_VERSION", ""))
        result.frame_trace = emit_trace(trace)
        self._active_trace = None
        return result

    def render_and_readback_passes(
        self,
        snapshot: GraphSnapshot,
        render_passes: Optional[Tuple[str, ...] | list[str]] = None,
        readback_output: str = "linear_f32",
    ) -> ExportResult:
        """Evaluate once and read back several beauty/AOV passes.

        This is the batch boundary for multi-pass delivery. It deliberately
        shares one graph evaluation and the producing node's live AOV textures;
        callers receive independent CPU arrays and never take ownership of GL
        resources. Auxiliary passes always use linear data readback, even when
        the caller requests the GPU sRGB preview path for beauty.

        Args:
            snapshot: Immutable graph snapshot.
            render_passes: Pass names to read. ``None`` reads beauty plus all
                AOVs available on the evaluated result.
            readback_output: ``linear_f32`` or ``srgb_u8`` for beauty. Mixed
                beauty/AOV reads are forced to ``linear_f32``.

        Returns:
            ``ExportResult.passes`` keyed by semantic pass name. ``image`` is
            also set when beauty was requested.
        """
        t_start = time.monotonic()
        if render_passes is None:
            requested_names: Optional[tuple[str, ...]] = None
        elif isinstance(render_passes, str):
            requested_names = (str(render_passes),)
        else:
            requested_names = tuple(
                dict.fromkeys(str(name) for name in render_passes if str(name))
            )
        if requested_names == ():
            return ExportResult(error="No render passes requested")

        self._readback_output = (
            readback_output if requested_names in (None, ("beauty",))
            else "linear_f32"
        )
        # R-13：multi-pass 入口同样解析并应用项目运动快门（此前完全没有
        # 这段赋值，导出多 pass 时 VectorBlur 一律回落节点自身值）。
        self._apply_project_motion_blur_shutter(snapshot)
        # R-14：multi-pass 入口同样安装全局 3D 场景设置。
        self._apply_global_3d_scene_settings(snapshot)
        try:
            self._ensure_gpu_resources()
            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)
            result = self._evaluate_snapshot(render_mode=RenderMode.EXPORT)
            if result is None or not result.is_valid:
                return ExportResult(
                    error="Graph evaluation produced no output",
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                )

            if requested_names is None:
                requested_names = (
                    "beauty",
                    *sorted(str(name) for name in result.aov_texture_ids),
                )
                # AUD-80003: render_passes=None expands to beauty plus
                # every AOV on the evaluated result. When any AOV is
                # present this is a mixed beauty/AOV read, so a caller's
                # srgb_u8 request must not stand — AOVs (depth/object_id/
                # motion) are numeric data and the sRGB OETF readback
                # would corrupt them. The docstring contract previously
                # only held for explicit pass lists.
                if (len(requested_names) > 1
                        and self._readback_output == "srgb_u8"):
                    self._readback_output = "linear_f32"
            available = {"beauty", *result.aov_texture_ids.keys()}
            missing = [name for name in requested_names if name not in available]
            if missing:
                return ExportResult(
                    error=("Requested render passes unavailable: "
                           + ", ".join(missing)),
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                )

            images: Dict[str, np.ndarray] = {}
            for pass_name in requested_names:
                selected = self._select_render_pass(result, pass_name)
                if selected is None:
                    return ExportResult(
                        error=f"Requested render pass unavailable: {pass_name}",
                        render_time_ms=(time.monotonic() - t_start) * 1000.0,
                    )
                image = self._readback(selected)
                if image is None:
                    return ExportResult(
                        error=f"Readback failed for render pass: {pass_name}",
                        render_time_ms=(time.monotonic() - t_start) * 1000.0,
                    )
                images[pass_name] = image

            return ExportResult(
                image=images.get("beauty"),
                passes=images,
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        except Exception as exc:
            return ExportResult(
                error=str(exc),
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        finally:
            self._tile_context = None
            if not self._persistent:
                self.cleanup()

    # ── Output Dimensions ──

    @staticmethod
    def _get_output_dimensions(snapshot: GraphSnapshot) -> Tuple[int, int]:
        """Determine the output image dimensions from the graph.

        Checks OutputNode parameters:
        - format_mode "custom" / "project": uses width/height parameters.
        - format_mode "input" (or legacy "auto"): inherits from color input.
        - Legacy resolution_mode "custom" is also supported for backward compat.

        Returns:
            (width, height) tuple in pixels.
        """
        for nid, ns in snapshot.nodes.items():
            if is_output_node_type(ns.node_type):
                # Prefer format_mode, fall back to legacy resolution_mode
                mode = ns.parameters.get("format_mode",
                                         ns.parameters.get("resolution_mode", "auto"))
                if mode in ("custom", "project"):
                    w = max(1, int(ns.parameters.get("width", 1920)))
                    h = max(1, int(ns.parameters.get("height", 1080)))
                    return w, h
                else:
                    # Auto / input: use color input dimensions
                    w = snapshot.color_width or 1920
                    h = snapshot.color_height or 1080
                    return max(1, w), max(1, h)
        return 1920, 1080

    # ── Full-Frame Rendering ──

    def _ensure_gpu_resources(self) -> None:
        """Lazily create GL resources (once) and refresh the eval context.

        The eval context is rebuilt every call so runtime_context / provider
        updates from set_timeline_clip_context() are always picked up, while
        the expensive parts (context wrap, framebuffer manager, resource
        pool, shader compilation) only run on first use.

        TOPOS_HEADLESS_GL=1 selects a standalone (offscreen) context. On
        macOS hosts whose window-server-backed context creation is unhealthy,
        ``moderngl.create_context()`` dies with SIGSEGV inside native code
        (uncatchable); the identical pipeline runs fine on a standalone
        context. The CLI renderer sets this flag — see scripts/topos_render.py.
        """
        if self.ctx is None:
            if os.environ.get("TOPOS_HEADLESS_GL") == "1":
                self.ctx = self._create_headless_context()
            else:
                self.ctx = moderngl.create_context()
        if self.fb_manager is None:
            self.fb_manager = FramebufferManager(self.ctx)
            self._resource_pool = GPUResourcePool(self.fb_manager, max_memory_mb=2048)
            self._cache_manager = CacheManager(self._resource_pool, max_entries=256)
            # Load shaders (imports source from composite_renderer)
            self._load_shaders()
        # Our GL context is current on this thread — retire fences whose
        # handles were released by consumers since the last producer call.
        self._drain_deferred_fence_deletes()
        self._install_eval_context()

    def _drain_deferred_fence_deletes(self) -> None:
        """glDeleteSync the fences queued by released GPU frame handles.

        Must run with this renderer's GL context current. Sync objects are
        context-local: deleting on a consumer thread (no context, or a
        different one) either errors out or targets the wrong context's
        handle, so release callbacks defer to here instead.
        """
        queue = getattr(self, "_deferred_fence_deletes", None)
        if not queue:
            return
        from src.shared.gpu.composite_gpu_frame_handle import delete_fence
        while queue:
            delete_fence(queue.popleft())

    def _create_headless_context(self) -> "moderngl.Context":
        """Create the headless standalone context with capability negotiation (P0-2).

        ``create_standalone_context()`` without ``require`` negotiates GL 3.3,
        which silently disables every compute-shader path: large-radius
        effects (blur/defocus/denoise/sharpen) then run the slower fragment
        fallback even on GPUs exposing GL 4.6 (measured ~2x on this class of
        nodes). Windows/Linux therefore request a compute-capable context
        (GL 4.3, the minimum for GLSL compute) first; macOS caps at GL 4.1
        and keeps the fragment fallback. ``TOPOS_HEADLESS_GL_REQUIRE``
        overrides the requested version explicitly.

        Negotiation never makes headless export unavailable: a failed request
        falls back to the plain standalone context, and the outcome is logged
        as ``compute_unavailable`` with driver/version so downstream nodes
        (ComputeShaderManager probes the real dispatch path) can report an
        explicit fallback reason.
        """
        candidates: list = []
        env_require = os.environ.get("TOPOS_HEADLESS_GL_REQUIRE", "").strip()
        if env_require:
            try:
                candidates.append(int(env_require))
            except ValueError:
                logger.warning(
                    "Invalid TOPOS_HEADLESS_GL_REQUIRE=%r ignored", env_require)
        if not candidates and sys.platform != "darwin":
            candidates.append(430)
        for require in candidates:
            try:
                ctx = moderngl.create_standalone_context(require=require)
            except Exception as exc:
                logger.warning(
                    "compute_unavailable: standalone GL %d request failed (%s); "
                    "falling back to the default standalone context",
                    require, exc)
                continue
            info = ctx.info
            logger.info(
                "Headless GL context: GL %s on %s (requested %d)",
                info.get("GL_VERSION"), info.get("GL_RENDERER"), require)
            return ctx
        ctx = moderngl.create_standalone_context()
        info = ctx.info
        logger.warning(
            "compute_unavailable: headless context is GL %s (%s); "
            "compute-shader effects will use the fragment fallback",
            info.get("GL_VERSION"), info.get("GL_RENDERER"))
        return ctx

    # ── P0-5 / TL-P1-01: GPU frame handle producer ──

    def render_gpu_handle(self,
                          snapshot: GraphSnapshot,
                          render_pass: str = "beauty",
                          frame_number: int = 0,
                          generation: int = 0,
                          render_mode: "RenderMode" = None,
                          trace_id: str = "") -> "CompositeGpuFrameHandle":
        """Evaluate the graph and return the result as a GPU frame handle.

        TL-C02/TL-P1-01: the comp-clip preview path must not read back.
        This producer copies the evaluated beauty texture ONCE into a
        pooled texture owned by the returned handle (single GPU copy, no
        CPU transfer, no QImage/NumPy). The handle carries the P0-5
        contract: device identity, pixel format, colour/alpha semantics,
        producer fence and an idempotent lease release that returns the
        texture to the pool. CPU arrays stay reserved for export,
        thumbnails and diagnostics.

        Must be called with a current GL context on the calling thread.

        Args:
            snapshot: Immutable graph snapshot.
            render_pass: Semantic pass to export (beauty by default).
            frame_number: Timeline frame for the trace/lease record.
            generation: Timeline generation for stale-present detection.
            render_mode: Evaluation mode; defaults to EXPORT quality.

        Returns:
            CompositeGpuFrameHandle — never raises; failures return an
            unavailable handle with an explicit fallback_reason.
        """
        from src.shared.gpu.composite_gpu_frame_handle import (
            CompositeGpuFrameHandle, make_unavailable_handle,
        )
        from .frame_trace import CompositeFrameTrace, emit_trace

        trace = CompositeFrameTrace(
            frame=int(frame_number), generation=int(generation),
            quality="preview", producer="gpu_handle", node_count=0)
        if trace_id:
            trace.trace_id = str(trace_id)
        t0 = time.monotonic()

        try:
            self._ensure_gpu_resources()
            if render_mode is None:
                render_mode = RenderMode.EXPORT
            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)
            trace.node_count = len(getattr(snapshot, "nodes", {}) or {})
            result = self._evaluate_snapshot(render_mode=render_mode)
            selected = (self._select_render_pass(result, render_pass)
                        if result is not None and result.is_valid else None)
            if selected is None or not selected.is_valid:
                trace.fallback_reason = "graph evaluation produced no output"
                trace.total_ms = (time.monotonic() - t0) * 1000.0
                return make_unavailable_handle(
                    trace.fallback_reason, generation=generation,
                    frame=frame_number, trace_id=trace.trace_id)

            w = max(1, int(selected.width or 1))
            h = max(1, int(selected.height or 1))
            trace.target_w, trace.target_h = w, h
            # Node outputs live in the pool's default half-float format.
            out_tex = self._resource_pool.acquire_texture(
                w, h, components=4, dtype="f2")
            fbo = self.fb_manager.get_fbo(out_tex)
            prog = self.utility_shaders.get("passthrough")
            if prog is None:
                self._resource_pool.release_texture(out_tex)
                trace.fallback_reason = "passthrough shader unavailable"
                trace.total_ms = (time.monotonic() - t0) * 1000.0
                return make_unavailable_handle(
                    trace.fallback_reason, generation=generation,
                    frame=frame_number, trace_id=trace.trace_id)
            fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)
            self._bind_external_texture(selected.texture_id)
            prog["u_texture"].value = 0
            self._render_quad(prog)
            self._unbind_texture_unit(0)

            # Producer fence: consumers in another (shared) context wait on
            # it before sampling. finish() is the correctness fallback when
            # sync objects are unavailable.
            from src.shared.gpu.composite_gpu_frame_handle import fence_to_int
            fence_ns = 0
            try:
                from OpenGL.GL import (
                    GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync, glFlush,
                )
                fence_ns = fence_to_int(
                    glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0))
                glFlush()
            except Exception:
                fence_ns = 0
            if not fence_ns:
                self.ctx.finish()

            def _release(tex=out_tex, fence=fence_ns) -> None:
                # Called on the CONSUMER's thread: no GL context is current
                # there (and a foreign context would target the wrong sync
                # object), so the fence is queued for producer-side deletion
                # — see _drain_deferred_fence_deletes. Returning the texture
                # to the pool is pure bookkeeping and is safe from any thread.
                if fence:
                    try:
                        self._deferred_fence_deletes.append(fence)
                    except Exception:
                        pass
                try:
                    self._resource_pool.release_texture(tex)
                except Exception as e:
                    logger.debug(f"Handle texture release error: {e}")

            info = self.ctx.info
            trace.device_identity = str(info.get("GL_RENDERER", ""))
            trace.gl_version = str(info.get("GL_VERSION", ""))
            trace.total_ms = (time.monotonic() - t0) * 1000.0
            trace.cpu_download_bytes = 0  # GPU→GPU: provably zero transfer
            trace.cpu_upload_bytes = 0
            emit_trace(trace)
            return CompositeGpuFrameHandle(
                frame=int(frame_number),
                generation=int(generation),
                timestamp_ns=time.monotonic_ns(),
                texture_id=int(out_tex.glo),
                width=w,
                height=h,
                pixel_format="f2",
                color_space=selected.color_space or "linear",
                range="full",
                alpha_premultiplied=bool(selected.alpha_premultiplied),
                is_gl_oriented=True,
                backend="opengl",
                device_identity=str(info.get("GL_RENDERER", "")),
                fence_ns=int(fence_ns) if fence_ns else 0,
                release_callback=_release,
                trace_id=trace.trace_id,
            )
        except Exception as exc:
            logger.error("render_gpu_handle failed: %s", exc)
            logger.debug("Stack trace:", exc_info=True)
            trace.fallback_reason = str(exc)
            trace.total_ms = (time.monotonic() - t0) * 1000.0
            emit_trace(trace)
            return make_unavailable_handle(
                str(exc), generation=generation, frame=frame_number,
                trace_id=trace.trace_id)
        finally:
            # AUD-80002: the one-shot teardown discipline of every other
            # public entry point (render_and_readback / _readback_passes /
            # _render_full_frame / the tile paths). A non-persistent caller
            # of this API — the documented build-render-teardown export
            # path — otherwise leaked the fb manager, resource pool, cache
            # manager, shader library and every render node built here.
            if not self._persistent:
                self.cleanup()

    def draw_srgb_into_fbo(self, texture_id: int, w: int, h: int, fbo) -> bool:
        """Draw an f2 texture through the sRGB/BGR/flip pass into ``fbo``.

        Shared by the async PBO preview lane (P1-10): the caller owns the
        target FBO (a pooled RGBA8 attachment kept alive across the async
        DMA) and consumes it via the PBO ring instead of a sync ``read()``.
        Must be called with this renderer's GL context current.
        """
        try:
            prog = self.utility_shaders.get("readback_srgb_bgr")
            if prog is None:
                return False
            fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self._bind_external_texture(texture_id)
            prog["u_texture"].value = 0
            self._render_quad(prog)
            self._unbind_texture_unit(0)
            return True
        except Exception as e:
            logger.error(f"draw_srgb_into_fbo failed: {e}")
            return False

    def _render_full_frame(self, snapshot: GraphSnapshot,
                           render_pass: str = "beauty") -> ExportResult:
        """Non-tiled rendering: evaluate entire graph at full resolution.

        This is the original single-pass export path, used for images
        smaller than TILE_THRESHOLD.
        """
        t_start = time.monotonic()
        try:
            # 1. GL resources: created once in persistent mode, per render otherwise
            self._ensure_gpu_resources()

            # 2. Store snapshot and sync nodes
            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)

            # 3. Evaluate the full graph in EXPORT mode
            _eval_t0 = time.monotonic()
            result = self._evaluate_snapshot(render_mode=RenderMode.EXPORT)
            _eval_ms = (time.monotonic() - _eval_t0) * 1000.0

            if result is None or not result.is_valid:
                return ExportResult(
                    error="Graph evaluation produced no output",
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                    eval_ms=_eval_ms,
                )

            # 4. Readback the selected semantic pass. Auxiliary textures are
            # still owned by their producing node; this method only samples
            # them into a temporary CPU readback target.
            selected = self._select_render_pass(result, render_pass)
            if selected is None:
                return ExportResult(
                    error=f"Requested render pass unavailable: {render_pass}",
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                    eval_ms=_eval_ms,
                )
            _rb_t0 = time.monotonic()
            image = self._readback(selected)
            _readback_ms = (time.monotonic() - _rb_t0) * 1000.0
            if image is None:
                return ExportResult(
                    error="Readback failed",
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                    eval_ms=_eval_ms, readback_ms=_readback_ms,
                )

            return ExportResult(
                image=image,
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
                eval_ms=_eval_ms, readback_ms=_readback_ms,
            )

        except Exception as e:
            return ExportResult(
                error=str(e),
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        finally:
            self._tile_context = None
            if not self._persistent:
                self.cleanup()

    @staticmethod
    def _select_render_pass(result: TextureResult,
                            render_pass: str) -> Optional[TextureResult]:
        """Return a readback-safe view of a beauty/AOV result."""
        requested = str(render_pass or "beauty")
        if requested == "beauty":
            return result
        texture_id = int(result.aov_texture_ids.get(requested, 0))
        if texture_id <= 0:
            return None
        width, height = result.aov_dimensions.get(
            requested, (result.width, result.height))
        return result.with_updates(
            texture_id=texture_id,
            width=int(width),
            height=int(height),
            color_space="data",
            alpha_premultiplied=False,
            aov_semantic=requested,
            depth_texture_id=None,
            has_deep_data=False,
            aov_texture_ids={},
            aov_dimensions={},
        )

    # ── Tiled Rendering ──

    # Transient full-frame f4 textures a full-frame preview evaluation may
    # hold at once (input copies, ping-pong pairs, node outputs in flight,
    # readback staging). The pool budget must cover this multiplier before
    # a >TILE_THRESHOLD preview goes full-frame (P0-1 item 1: count input,
    # output, ping-pong, caches and co-resident Edit work into the budget).
    FULL_FRAME_TRANSIENT_FACTOR = 6

    def _decide_preview_render_strategy(self, output_w: int,
                                        output_h: int) -> Tuple[str, str]:
        """Pick the render strategy for preview frames above TILE_THRESHOLD.

        Returns:
            (strategy, reason) where strategy is one of:
            - "full_frame": single-pass GPU evaluation + one sRGB u8
              readback (3 bytes/px, ~15ms at 4K).
            - "tiled_gpu_stitch": tile evaluation with GPU-side stitch
              into a full-frame linear canvas; the only CPU transfer is
              one full-frame sRGB u8 readback.
            - "tiled_cpu_fallback": legacy per-tile linear_f32 readback +
              numpy stitch (slow, but memory-bounded for extreme sizes).
        """
        try:
            self._ensure_gpu_resources()
        except Exception as exc:
            return "tiled_cpu_fallback", f"gpu_resource_init_failed: {exc}"

        frame_f4_bytes = output_w * output_h * 4 * 4
        max_tex = int(self.ctx.info.get("MAX_TEXTURE_SIZE", 0) or 0)
        if max_tex and max(output_w, output_h) > max_tex:
            return ("tiled_gpu_stitch",
                    f"frame {output_w}x{output_h} exceeds MAX_TEXTURE_SIZE "
                    f"{max_tex}")
        budget_bytes = int(
            getattr(self._resource_pool, "max_memory_bytes", 0) or 0)
        if not budget_bytes:
            budget_bytes = int(
                getattr(self._resource_pool, "max_memory_mb", 0) or 0) \
                * 1024 * 1024
        if not budget_bytes:
            return "full_frame", "no pool budget configured; full-frame assumed safe"
        if frame_f4_bytes * self.FULL_FRAME_TRANSIENT_FACTOR <= budget_bytes:
            return ("full_frame",
                    f"frame f4 {frame_f4_bytes}B x"
                    f"{self.FULL_FRAME_TRANSIENT_FACTOR} transients fit pool "
                    f"budget {budget_bytes}B")
        if frame_f4_bytes * 2 <= budget_bytes:
            return ("tiled_gpu_stitch",
                    f"frame f4 {frame_f4_bytes}B transients exceed pool budget "
                    f"{budget_bytes}B; GPU stitch canvas fits")
        return ("tiled_cpu_fallback",
                f"frame f4 {frame_f4_bytes}B exceeds pool budget "
                f"{budget_bytes}B")

    def _render_preview_large_frame(self, snapshot: GraphSnapshot,
                                    output_w: int, output_h: int,
                                    plan: RenderPlan) -> ExportResult:
        """Dispatch an oversized preview frame to the best GPU path (P0-1).

        Records the chosen strategy and reason on the ExportResult so the
        "preview looks fast but quietly degraded" failure mode stays
        diagnosable.
        """
        strategy, reason = self._decide_preview_render_strategy(
            output_w, output_h)
        logger.info("Comp preview %dx%d strategy=%s (%s)",
                    output_w, output_h, strategy, reason)
        if strategy == "full_frame":
            result = self._render_full_frame(snapshot, render_pass="beauty")
        elif strategy == "tiled_gpu_stitch":
            result = self._render_tiled_gpu_stitch(
                snapshot, output_w, output_h, plan)
        else:
            # Extreme sizes: keep the memory-bounded legacy path. The
            # preview consumer accepts linear f32 (numpy sRGB chain).
            self._readback_output = "linear_f32"
            result = self._render_tiled_with_plan(
                snapshot, output_w, output_h, plan)
        # Inner fallbacks (reformat / missing shaders) record the path they
        # actually took — the decision above must not overwrite that.
        if result.preview_strategy is None:
            result.preview_strategy = strategy
            result.preview_strategy_reason = reason
        return result

    def _render_tiled_gpu_stitch(self, snapshot: GraphSnapshot,
                                 output_w: int, output_h: int,
                                 plan: RenderPlan) -> ExportResult:
        """Tile evaluation with GPU-side stitch and a single u8 readback.

        Each tile is evaluated at tile resolution, then blitted into a
        full-frame linear f4 canvas through sub-rect UV sampling (no CPU
        crop). After the last tile the canvas goes through the existing
        sRGB BGR flip shader into an RGBA8 readback FBO and is read back
        once as (h, w, 3) uint8 — 3 bytes/px cross the bus in total,
        versus ~268MB of per-tile f4 readbacks at 4K.
        """
        from ..core.tile_scheduler import TileScheduler

        t_start = time.monotonic()
        # Reformat graphs are tile-incompatible (same rule as the export
        # tiled path) — their output is usually smaller anyway.
        if any(ns.node_type == "reformat"
               for ns in snapshot.nodes.values()):
            result = self._render_full_frame(snapshot, render_pass="beauty")
            # P0-1 item 4: record the path actually taken, not the decision.
            result.preview_strategy = "full_frame"
            result.preview_strategy_reason = (
                "reformat node present; tile-incompatible, full-frame")
            return result
        try:
            self._ensure_gpu_resources()
            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)

            prog_tile = self.utility_shaders.get("passthrough_sub")
            prog_flip = self.utility_shaders.get("readback_srgb_bgr")
            if prog_tile is None or prog_flip is None:
                logger.warning(
                    "GPU stitch shaders unavailable; falling back to "
                    "full-frame preview")
                result = self._render_full_frame(
                    snapshot, render_pass="beauty")
                result.preview_strategy = "full_frame"
                result.preview_strategy_reason = (
                    "GPU stitch shaders unavailable; full-frame fallback")
                return result

            output_format = ImageFormat(width=output_w, height=output_h)
            tiles = TileScheduler.generate_tiles(
                output_format, plan.estimated_padding, plan.tile_size)

            # P0-1/P1-6: take canvas + readback from the resource pool so
            # their bytes count toward the eviction budget. Raw ctx textures
            # (invisible to the pool) tipped budget checks during tile
            # evaluation and triggered glActiveTexture GL 1282 inside
            # effect nodes.
            canvas_tex = self._resource_pool.acquire_texture(
                output_w, output_h, components=4, dtype="f4")
            canvas_fbo = self.fb_manager.get_fbo(canvas_tex)
            read_tex = None
            read_fbo = None
            rendered_tiles = 0
            try:
                for tile in tiles:
                    # AUD-80001: evict tile-scoped SOURCE entries BEFORE
                    # evaluating so this tile cannot hit entries cropped
                    # for a previous tile (or a previous pass) under
                    # identical tile-agnostic keys.
                    self._clear_tile_scoped_cache()
                    tile_ctx = TileContext(
                        padded_roi=tile.padded_roi,
                        output_format=output_format,
                        tile_id=tile.tile_id,
                        output_roi=tile.output_roi,
                        padding=tile.padding,
                    )
                    result = self._evaluate_snapshot(
                        render_mode=RenderMode.EXPORT,
                        tile_context=tile_ctx,
                    )
                    if result is None or not result.is_valid:
                        # P1-6 semantics: a missing tile is an error, not a
                        # silent black region in the preview.
                        raise RuntimeError(
                            f"Tile {tile.tile_id} ({tile.output_roi}) "
                            f"produced no output")
                    pad_w = max(1, int(result.width
                                       or tile.padded_roi.width))
                    pad_h = max(1, int(result.height
                                       or tile.padded_roi.height))
                    out = tile.output_roi
                    # AUD-80001 follow-up: per-tile re-evaluation makes the
                    # output node render each tile at its GLOBAL position
                    # into an output-format-sized texture (result covers the
                    # whole frame, tile content at out.x/out.y, black
                    # elsewhere).  The legacy math sampled relative to the
                    # tile's padded_roi, which only matches tile-sized
                    # results — for full-frame results sample the exact
                    # global output sub-rect instead.
                    if (pad_w >= output_w and pad_h >= output_h):
                        u_offset = out.x / pad_w
                        v_offset = out.y / pad_h
                    else:
                        u_offset = (out.x - tile.padded_roi.x) / pad_w
                        v_offset = (out.y - tile.padded_roi.y) / pad_h
                    canvas_fbo.use()
                    self.ctx.viewport = (out.x, out.y, out.width, out.height)
                    self._bind_external_texture(result.texture_id)
                    prog_tile["u_texture"].value = 0
                    prog_tile["u_uv_scale"].value = (
                        out.width / pad_w, out.height / pad_h)
                    prog_tile["u_uv_offset"].value = (u_offset, v_offset)
                    self._render_quad(prog_tile)
                    # P1-6: release-before-unbind leaves a deleted texture
                    # bound on the unit — the next evaluation then dies with
                    # glActiveTexture GL 1282 ("invalid operation").
                    self._unbind_texture_unit(0)
                    rendered_tiles += 1

                # Single GPU flip + sRGB encode + readback (top-down
                # uint8 BGR, matching the full-frame srgb_u8 path).
                read_tex = self._resource_pool.acquire_texture(
                    output_w, output_h, components=4, dtype="f1")
                read_fbo = self.fb_manager.get_fbo(read_tex)
                read_fbo.use()
                self.ctx.viewport = (0, 0, output_w, output_h)
                canvas_tex.use(0)
                prog_flip["u_texture"].value = 0
                self._render_quad(prog_flip)
                # Unbind the canvas before releasing it (P1-6: the canvas
                # texture must not stay bound across its release).
                self._unbind_texture_unit(0)

                raw = read_fbo.read(components=3, dtype="f1", alignment=1)
                image = np.frombuffer(raw, dtype=np.uint8).reshape(
                    output_h, output_w, 3)
                return ExportResult(
                    image=image,
                    used_tiling=True,
                    tile_count=rendered_tiles,
                    render_time_ms=(time.monotonic() - t_start) * 1000.0,
                )
            finally:
                # FBOs stay in fb_manager's cache (keyed by texture) — they
                # are reused when the pooled texture is re-acquired and are
                # released by cleanup(). Manually releasing here leaves a
                # dead InvalidObject in the cache (AttributeError on next
                # frame's get_fbo().use()).
                if read_tex is not None:
                    try:
                        self._resource_pool.release_texture(read_tex)
                    except Exception as e:
                        logger.debug(f"Read texture release error: {e}")
                try:
                    self._resource_pool.release_texture(canvas_tex)
                except Exception as e:
                    logger.debug(f"Canvas texture release error: {e}")

        except Exception as e:
            logger.error(f"GPU-stitch preview failed: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return ExportResult(
                error=str(e),
                used_tiling=True,
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        finally:
            # AUD-80001: the last tile's cropped SOURCE entries must not
            # survive into later full-frame EXPORT evaluations on a
            # persistent renderer.  EXPORT entries are dropped post-pass
            # only (see _clear_tile_scoped_cache docstring).
            self._clear_tile_scoped_cache()
            if self._cache_manager:
                self._cache_manager.clear_export_cache()
            self._tile_context = None
            if not self._persistent:
                self.cleanup()

    def _render_tiled_with_plan(self, snapshot: GraphSnapshot,
                                output_w: int, output_h: int,
                                plan: RenderPlan) -> ExportResult:
        """Tile-based rendering using RenderPlan's tiling configuration.

        Uses the pre-computed tile size and padding from the RenderPlan,
        ensuring optimal memory usage and seamless boundaries.

        Args:
            snapshot: Graph snapshot to evaluate.
            output_w: Output image width.
            output_h: Output image height.
            plan: RenderPlan with tiling configuration.

        Returns:
            ExportResult with stitched image data.
        """
        from ..core.tile_scheduler import TileScheduler

        t_start = time.monotonic()

        # Use plan's configuration
        tile_size = plan.tile_size
        total_padding = plan.estimated_padding

        # 1. Check for Reformat nodes — explicit fallback
        has_reformat = any(
            ns.node_type == "reformat" for ns in snapshot.nodes.values()
        )
        if has_reformat:
            logger.info("Reformat node detected, "
                        "falling back to full-frame (tile-incompatible)")
            return self._render_full_frame(snapshot)

        # 2. Generate tiles using plan's configuration
        output_format = ImageFormat(width=output_w, height=output_h)
        tiles = TileScheduler.generate_tiles(
            output_format, total_padding, tile_size
        )

        logger.debug(f"Tiled with plan: {output_w}x{output_h}, "
                           f"padding={total_padding}px, tiles={len(tiles)}, "
                           f"tile_size={tile_size}, "
                           f"estimated_memory={plan.estimated_memory_mb:.1f}MB")

        # 3. Initialize GPU resources
        try:
            self._ensure_gpu_resources()

            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)

            # 4. Allocate CPU output buffer
            output_array = np.zeros((output_h, output_w, 3), dtype=np.float32)

            # 5. Evaluate each tile
            for tile in tiles:
                # AUD-80001: evict tile-scoped SOURCE entries BEFORE
                # evaluating so this tile cannot hit entries cropped
                # for a previous tile (or a previous pass) under
                # identical tile-agnostic keys.
                self._clear_tile_scoped_cache()
                try:
                    tile_ctx = TileContext(
                        padded_roi=tile.padded_roi,
                        output_format=output_format,
                        tile_id=tile.tile_id,
                        output_roi=tile.output_roi,
                        padding=tile.padding,
                    )
                    result = self._evaluate_snapshot(
                        render_mode=RenderMode.EXPORT,
                        tile_context=tile_ctx,
                    )

                    if result is None or not result.is_valid:
                        # P1-6: a skipped tile silently corrupts the export
                        # (black/cropped region) — fail the render instead.
                        raise RuntimeError(
                            f"Tile {tile.tile_id} ({tile.output_roi}) "
                            f"produced no output")

                    tile_data = self._readback_tile(result, tile,
                                                    output_w, output_h)
                    if tile_data is None:
                        # P1-6: a readback failure must not silently drop
                        # the tile (black region in the stitched output).
                        raise RuntimeError(
                            f"Tile {tile.tile_id} ({tile.output_roi}) "
                            f"readback failed")
                    self._stitch_tile(output_array, tile_data, tile, output_h)

                except Exception as e:
                    logger.error(f"Tile {tile.tile_id} error: {e}")
                    logger.debug("Stack trace:", exc_info=True)
                    raise

            return ExportResult(
                image=output_array,
                used_tiling=True,
                tile_count=len(tiles),
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )

        except Exception as e:
            return ExportResult(
                error=str(e),
                used_tiling=True,
                tile_count=len(tiles) if 'tiles' in dir() else 0,
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        finally:
            # AUD-80001: the last tile's cropped SOURCE entries must not
            # survive into later full-frame EXPORT evaluations on a
            # persistent renderer.  EXPORT entries are dropped post-pass
            # only (see _clear_tile_scoped_cache docstring).
            self._clear_tile_scoped_cache()
            if self._cache_manager:
                self._cache_manager.clear_export_cache()
            self._tile_context = None
            if not self._persistent:
                self.cleanup()

    def _render_tiled(self, snapshot: GraphSnapshot,
                      output_w: int, output_h: int) -> ExportResult:
        """Tile-based rendering for large images.

        Divides the output into tiles, evaluates each tile independently,
        and stitches the results into a single output array.

        Memory benefit: intermediate textures are tile-sized, not full-frame.
        For a 4K image with 1024px tiles, intermediate textures are ~4MB
        instead of ~64MB (16x reduction per texture).
        """
        from ..core.tile_padding_computer import TilePaddingComputer
        from ..core.tile_scheduler import TileScheduler

        t_start = time.monotonic()

        # 1. Check for Reformat nodes — explicit fallback
        has_reformat = any(
            ns.node_type == "reformat" for ns in snapshot.nodes.values()
        )
        if has_reformat:
            logger.info("Reformat node detected, "
                        "falling back to full-frame (tile-incompatible)")
            return self._render_full_frame(snapshot)

        # 2. Compute padding needed for spatial effects
        padding_info = TilePaddingComputer.compute(snapshot)
        total_padding = padding_info.total_padding

        # 3. Generate tiles
        output_format = ImageFormat(width=output_w, height=output_h)
        tiles = TileScheduler.generate_tiles(
            output_format, total_padding, self.DEFAULT_TILE_SIZE
        )

        logger.debug(f"Tiled: {output_w}x{output_h}, "
                           f"padding={total_padding}px, tiles={len(tiles)}, "
                           f"tile_size={self.DEFAULT_TILE_SIZE}")

        # 4. Initialize GPU resources
        try:
            self._ensure_gpu_resources()

            self._snapshot = snapshot
            self._sync_render_nodes(snapshot)

            # 5. Allocate CPU output buffer
            output_array = np.zeros((output_h, output_w, 3), dtype=np.float32)

            # 6. Evaluate each tile
            for tile in tiles:
                # AUD-80001: evict tile-scoped SOURCE entries BEFORE
                # evaluating so this tile cannot hit entries cropped
                # for a previous tile (or a previous pass) under
                # identical tile-agnostic keys.
                self._clear_tile_scoped_cache()
                try:
                    tile_ctx = TileContext(
                        padded_roi=tile.padded_roi,
                        output_format=output_format,
                    )
                    result = self._evaluate_snapshot(
                        render_mode=RenderMode.EXPORT,
                        tile_context=tile_ctx,
                    )

                    if result is None or not result.is_valid:
                        # P1-6: a skipped tile silently corrupts the export
                        # (black/cropped region) — fail the render instead.
                        raise RuntimeError(
                            f"Tile {tile.tile_id} ({tile.output_roi}) "
                            f"produced no output")

                    tile_data = self._readback_tile(result, tile,
                                                    output_w, output_h)
                    if tile_data is None:
                        # P1-6: a readback failure must not silently drop
                        # the tile (black region in the stitched output).
                        raise RuntimeError(
                            f"Tile {tile.tile_id} ({tile.output_roi}) "
                            f"readback failed")
                    self._stitch_tile(output_array, tile_data, tile, output_h)

                except Exception as e:
                    logger.error(f"Tile {tile.tile_id} error: {e}")
                    logger.debug("Stack trace:", exc_info=True)
                    raise

            return ExportResult(
                image=output_array,
                used_tiling=True,
                tile_count=len(tiles),
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )

        except Exception as e:
            return ExportResult(
                error=str(e),
                used_tiling=True,
                tile_count=len(tiles) if 'tiles' in dir() else 0,
                render_time_ms=(time.monotonic() - t_start) * 1000.0,
            )
        finally:
            # AUD-80001: the last tile's cropped SOURCE entries must not
            # survive into later full-frame EXPORT evaluations on a
            # persistent renderer.  EXPORT entries are dropped post-pass
            # only (see _clear_tile_scoped_cache docstring).
            self._clear_tile_scoped_cache()
            if self._cache_manager:
                self._cache_manager.clear_export_cache()
            self._tile_context = None
            if not self._persistent:
                self.cleanup()

    def _clear_tile_scoped_cache(self) -> None:
        """Reset tile-scoped evaluation state between tiles (AUD-80001).

        Two stale-state channels serve tile 1's (or a pre-loop full
        frame's) content to later tiles because eval-cache keys carry no
        tile geometry (the ROI suffix exists only for the PREVIEW
        viewer-ROI path):

        1. SOURCE/AI cache entries.  Source nodes have empty input
           metadata, so their recomputed key is identical for every tile —
           ``clear_export_cache()`` never evicts them (protected category)
           and tiles 2+ hit tile 1's cropped texture verbatim.  Evict them
           via invalidate_node().
        2. The evaluator's per-node key memo (``_node_cache_keys``).  The
           dirty-skip path serves entries by memoized key without
           recomputing them, so a tile-agnostic EXPORT entry (e.g. the
           output node's full-frame passthrough) leaks into every tile
           even after its inputs changed.  Clearing the memo forces each
           tile to recompute keys from its actual (cropped) inputs, which
           miss.

        EXPORT entries themselves stay cached during the pass — a full
        ``CacheManager.clear()`` was tried and rejected: releasing the
        EXPORT output textures back into the pool mid-pass (on top of the
        SOURCE eviction) measurably corrupts subsequent tiles on the
        GPU-stitch path (recycled textures re-acquired by the next tile's
        evaluation render ~1/3 linear value).  EXPORT entries are cleared
        post-pass in the finally blocks instead.
        """
        cm = self._cache_manager
        snapshot = self._snapshot
        if cm is None or snapshot is None:
            return
        tainted = (CacheManager._SOURCE_NODE_TYPES
                   | CacheManager._AI_NODE_TYPES)
        for ns in snapshot.nodes.values():
            if ns.node_type in tainted:
                cm.invalidate_node(ns.node_id)
        memo = getattr(self, "_node_cache_keys", None)
        if isinstance(memo, dict):
            memo.clear()

    def _readback_tile(self, result: TextureResult, tile,
                       output_w: int = 0, output_h: int = 0) -> Optional[np.ndarray]:
        """Read back a tile-sized GPU result and crop to the output ROI.

        The GPU result covers the padded region. We read back the full
        padded texture, then crop to just the output ROI (strip padding).

        Args:
            result: TextureResult from tile evaluation.
            tile: Tile with output_roi and padded_roi.
            output_w: Full output width (detects full-frame results).
            output_h: Full output height (detects full-frame results).

        Returns:
            BGR float32 numpy array for the output region only, or None.

        AUD-80001 follow-up: per-tile re-evaluation makes the output node
        render each tile at its GLOBAL position into an output-format-sized
        texture; for such results the crop rect is the output ROI's global
        position, not its position relative to the tile's padded_roi.
        """
        w = result.width or 1
        h = result.height or 1

        readback_tex = None

        try:
            readback_tex, readback_fbo = self._acquire_readback_target(
                w, h, 'f4')
            readback_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            prog = self.utility_shaders.get('passthrough')
            if prog is None:
                return None

            self._bind_external_texture(result.texture_id)
            prog['u_texture'].value = 0
            self._render_quad(prog)
            # P1-6: the result texture must not stay bound on unit 0 — the
            # caller releases evaluation caches right after this readback,
            # and a released-but-bound texture poisons later binds (GL 1282).
            self._unbind_texture_unit(0)
            # No ctx.finish() (P2-10): read() below syncs implicitly.

            raw = readback_fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

            # Flip vertically (OpenGL bottom-left origin -> image top-left)
            arr = np.flipud(arr)

            # Crop to output ROI (strip padding)
            padded = tile.padded_roi
            output = tile.output_roi
            if output_w and w >= output_w and h >= output_h:
                # Full-frame positioned result (see docstring)
                x0 = output.x
                y0 = output.y
            else:
                x0 = output.x - padded.x
                y0 = output.y - padded.y
            x1 = x0 + output.width
            y1 = y0 + output.height

            # Safety clamp
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(w, x1), min(h, y1)

            cropped = arr[y0:y1, x0:x1, :3].copy()
            return cv2.cvtColor(cropped, cv2.COLOR_RGB2BGR)

        except Exception as e:
            logger.error(f"Tile readback error: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return None

        finally:
            self._release_readback_target(readback_tex)

    @staticmethod
    def _stitch_tile(output_array: np.ndarray, tile_data: np.ndarray,
                     tile, total_h: int):
        """Copy tile data into the correct position in the output array.

        Handles the OpenGL-to-image Y coordinate flip:
        - BBox y=0 is at the bottom (OpenGL convention)
        - numpy row 0 is at the top (image convention)

        Args:
            output_array: Full output image buffer (h, w, 3), float32.
            tile_data: Cropped tile data matching output_roi size.
            tile: Tile with output_roi specifying position.
            total_h: Total output image height.
        """
        roi = tile.output_roi
        # Convert OpenGL y-coords to image row indices
        img_row_start = total_h - (roi.y + roi.height)
        img_row_end = total_h - roi.y
        img_col_start = roi.x
        img_col_end = roi.x + roi.width

        # Safety bounds check
        img_row_start = max(0, img_row_start)
        img_row_end = min(total_h, img_row_end)

        output_array[img_row_start:img_row_end,
                     img_col_start:img_col_end, :] = tile_data

    # ── Full-Frame Readback ──

    def _acquire_readback_target(self, w: int, h: int, dtype: str):
        """Acquire a pooled readback texture + its cached FBO (P2-10).

        The previous per-frame ``ctx.texture`` + ``ctx.framebuffer`` pair
        cost a driver allocation every frame on the export/preview hot
        path. Textures come from the resource pool; FBOs stay in
        FramebufferManager's cache (never released here — see the stitch
        path note on InvalidObject).
        """
        tex = self._resource_pool.acquire_texture(w, h, components=4,
                                                 dtype=dtype)
        fbo = self.fb_manager.get_fbo(tex)
        return tex, fbo

    def _release_readback_target(self, tex) -> None:
        if tex is None:
            return
        try:
            self._resource_pool.release_texture(tex)
        except Exception as e:
            logger.debug(f"Readback texture release error: {e}")

    def _readback(self, result: TextureResult) -> Optional[np.ndarray]:
        """Readback a GPU texture result to CPU.

        Output format depends on ``self._readback_output``:

        - ``"linear_f32"`` (default, export): BGR float32 (h, w, 3) in
          [0, 1], scene-linear — full-fidelity readback with CPU flip +
          channel swap.
        - ``"srgb_u8"`` (preview): sRGB-encoded BGR uint8 (h, w, 3).
          OETF + BGR reorder + vertical flip all run in the readback
          shader into an RGBA8 target; only 3 bytes/pixel cross the bus.
          This replaces the previous "f32 readback (33MB/frame at 1080p)
          + numpy np.power sRGB + flipud + cvtColor" CPU chain, which
          cost 200ms+/frame on the timeline comp-clip preview path.

        Used by the full-frame (non-tiled) export path.

        Args:
            result: TextureResult with valid texture_id, width, height.

        Returns:
            BGR numpy array (see above), or None on failure.
        """
        w = result.width or 1
        h = result.height or 1
        srgb_u8 = getattr(self, "_readback_output", "linear_f32") == "srgb_u8"

        readback_tex = None

        try:
            if srgb_u8:
                prog = self.utility_shaders.get('readback_srgb_bgr')
                if prog is None:
                    srgb_u8 = False  # shader missing → linear fallback
                    prog = self.utility_shaders.get('passthrough')
            else:
                prog = self.utility_shaders.get('passthrough')
            if prog is None:
                return None

            # P2-10: pooled readback target. The sRGB preview shader writes
            # a floating ``vec4``; it therefore needs a *normalized* RGBA8
            # attachment (``f1``), not an unsigned-integer RGBA8UI
            # attachment (``u1``). On macOS GL 4.1 the latter accepts the
            # draw with undefined results, which surfaced as black/corrupted
            # frames when a Composite clip played on the Edit timeline.
            readback_tex, readback_fbo = self._acquire_readback_target(
                w, h, 'f1' if srgb_u8 else 'f4')
            readback_fbo.use()
            self.ctx.viewport = (0, 0, w, h)
            self.ctx.clear(0.0, 0.0, 0.0, 0.0)

            # Render the result texture to the readback FBO
            self._bind_external_texture(result.texture_id)
            prog['u_texture'].value = 0
            self._render_quad(prog)
            # P1-6: unbind before the caller's cache clear can release the
            # still-bound result texture (same 1282 hazard as the stitch path).
            self._unbind_texture_unit(0)
            # No ctx.finish() (P2-10): fbo.read() is an implicit sync point
            # for the pixels it reads; a full pipeline stall is redundant.

            if srgb_u8:
                # Shader already applied sRGB OETF + BGR order + Y flip.
                raw = readback_fbo.read(components=3, dtype='f1', alignment=1)
                return np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 3)

            # Read pixels from FBO
            raw = readback_fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(h, w, 4)

            # Flip vertically (OpenGL bottom-left origin → image top-left)
            # Strip alpha, convert RGB → BGR
            bgr = cv2.cvtColor(np.flipud(arr[:, :, :3]).copy(), cv2.COLOR_RGB2BGR)

            return bgr

        except Exception as e:
            logger.error(f"Readback error: {e}")
            logger.debug("Stack trace:", exc_info=True)
            return None

        finally:
            self._release_readback_target(readback_tex)

    def cleanup(self):
        """Release all temporary GPU resources."""
        # Retire queued handle fences while the context can still service
        # glDeleteSync (the pool cleanup below may destroy it).
        try:
            self._drain_deferred_fence_deletes()
        except Exception as e:
            logger.debug(f"Fence drain error in cleanup: {e}")
        if hasattr(self, '_quad_vbo') and self._quad_vbo is not None:
            try:
                self._quad_vbo.release()
            except Exception as e:
                logger.debug(f"VBO release error in cleanup: {e}")
            self._quad_vbo = None
        if hasattr(self, '_vao_cache'):
            for vao in self._vao_cache.values():
                try:
                    vao.release()
                except Exception as e:
                    logger.debug(f"VAO release error in cleanup: {e}")
            self._vao_cache.clear()
        # Clear cache (releases owned textures) then cleanup resource pool
        if self._cache_manager:
            self._cache_manager.clear()
        if self._resource_pool:
            self._resource_pool.cleanup()
        elif self.fb_manager:
            self.fb_manager.cleanup()
        # Cleanup render nodes
        for node in self._render_nodes.values():
            if hasattr(node, 'cleanup'):
                try:
                    node.cleanup()
                except Exception as e:
                    logger.debug(f"Node cleanup error: {e}")
        self._render_nodes.clear()
        # Release shader library (centralized program cache)
        if hasattr(self, '_shader_lib'):
            self._shader_lib.release()
        # Release compute shader manager
        if hasattr(self, '_compute_shader_manager') and self._compute_shader_manager:
            self._compute_shader_manager.release()
        # Reset handles so _ensure_gpu_resources() re-initializes cleanly
        # after an explicit cleanup (e.g. leaving the preview path).
        self.ctx = None
        self.fb_manager = None
        self._resource_pool = None
        self._cache_manager = None
        self.utility_shaders = {}
        self._snapshot = None
        # Note: we do NOT release the wrapped moderngl context here because
        # it's the shared main context (moderngl.create_context() wraps the
        # current GL context, it doesn't own it). Releasing it would destroy
        # the app's GL context.
