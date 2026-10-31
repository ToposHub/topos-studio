"""
CompositeFrameRenderer - Headless composite graph frame rendering for Edit timeline.

Renders composite graph frames for Edit timeline preview and export.
Independent of CompositePage/UI -- works headlessly by delegating to
CompositeExportRenderer, which uses the same GraphEvaluator pipeline.

Architecture:
  CompositeGraphData dict
      |
      v
  NodeGraph.from_dict()  -->  GraphSnapshot
      |
      v
  CompositeExportRenderer (delegation)
      |
      v
  BGR float32 numpy array (CPU)

Lifecycle:
  1. Created with lightweight init (no GPU resources)
  2. render_comp_clip_frame() called per frame
  3. Returns numpy array (BGR float32 [0-1]) for CPU pipeline consumption
  4. cleanup() releases all GPU resources
  5. Can be reused across frames

Thread safety:
  - NOT thread-safe. Each thread needs its own instance.
  - For preview: called on PreviewRenderWorker thread
  - For export: called on the export thread
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np

from src.features.composite.core.graph_snapshot import GraphSnapshot
from src.features.composite.core.node_engine import NodeGraph

logger = logging.getLogger(__name__)

# P2-17：复合图渲染**实现**的身份标识。graph JSON 内容哈希只覆盖图数据
# 本身；渲染器实现（节点求值、shader、采样、混合语义）变化而图不变时，
# 同键会命中旧实现渲染的缓存帧。任何改变像素输出的渲染器实现变更都
# 必须递增此版本（导出帧级缓存 SegmentRenderKey.renderer_version 消费）。
COMPOSITE_GRAPH_RENDERER_VERSION = "comp-graph-render-v1"


def _preview_render_mode():
    """Preview-quality RenderMode for the GPU handle producer."""
    try:
        from src.features.composite.render.render_scheduler import RenderMode
        return RenderMode.PREVIEW
    except Exception:
        return None


class CompositeFrameRenderer:
    """Renders composite graph frames for Edit timeline preview and export.

    Independent of CompositePage/UI -- works headlessly by delegating to
    CompositeExportRenderer which uses the same GraphEvaluator pipeline.

    Usage:
        renderer = CompositeFrameRenderer()

        # Render a frame from a composite graph
        frame_data = renderer.render_comp_clip_frame(
            graph_data=composite_graph_data_dict,
            timeline_frame=42,
            quality="preview",
        )

        # frame_data is a BGR float32 [0-1] numpy array, or None on failure
        renderer.cleanup()
    """

    def __init__(self, config_manager: Any = None) -> None:
        """Initialize the renderer (no GPU resources allocated until first render).

        Args:
            config_manager: ConfigManager for colour-engine selection (builtin
                vs OCIO), forwarded to the internal CompositeExportRenderer so
                nodes that do their own colour conversion (FileOutputNode)
                honour composite.color_engine. None → OCIO default.
        """
        # Internal CompositeExportRenderer (lazy-initialized)
        self._export_renderer: Any = None
        self._config_manager = config_manager
        # T-P1-60：最近一次 render_comp_clip_frame 的节点级结构化诊断。
        self.last_diagnostics: list = []
        # T-P1-60 步骤 6：最近一帧的 performance attribution（frame trace
        # dict：eval_ms/readback_ms/total_ms/transfer 字节/cache_hit/strategy）。
        self.last_frame_trace: dict | None = None
        # 反序列化缓存：播放中 graph_data 是同一 dict 对象（编辑时整体
        # 替换），按 id 缓存 NodeGraph，跳过每帧 NodeGraph.from_dict 的
        # 全图反序列化（时间线预览每帧 ~5-15ms 纯 Python 开销）。
        # (cache_key, graph_data_ref, NodeGraph). The key includes a cheap
        # persisted revision token when available, so an in-place graph
        # update cannot silently reuse a deserialized graph. Older payloads
        # retain the identity fast path and explicit invalidation contract.
        self._snapshot_graph_cache: tuple[tuple[int, Any], Any, Any] | None = None
        # Reused TimelineClipFrameProvider: recreating it per frame rebuilds
        # decode sessions and discards the source texture cache every time,
        # which dominates comp-clip preview cost. Kept across frames and
        # re-keyed when the clip/graph changes.
        self._provider: Any = None
        self._provider_key: tuple | None = None
        # P1-10: async PBO readback lane (opt-in, persistent mode only).
        # 2×PBO ring + one alive RGBA8 target per slot — a slot's texture
        # must survive until its DMA completes, so slots are never returned
        # to the pool between frames.
        self._async_pbo: Any = None
        self._async_slots: list[dict] = []
        self._async_slot_idx: int = 0
        self._async_request_id: int = 0
        self._async_download_bytes: int = 0

    # ================================================================
    # Public API
    # ================================================================

    def render_comp_clip_frame(
        self,
        graph_data: dict,
        timeline_id: str | None = None,
        clip_id: str | None = None,
        asset_id: str | None = None,
        edit_project: Any | None = None,
        timeline_sequences: dict[str, Any] | None = None,
        timeline_frame: int = 0,
        source_frame: int | None = None,
        quality: str = "preview",
        caption_input: object | None = None,
        render_pass: str = "beauty",
        trace_id: str | None = None,
    ) -> Optional[np.ndarray]:
        """Render a single frame from a composite graph.

        Args:
            graph_data: CompositeGraphData dict (from EditProject.compositions).
                        Must contain 'nodes', 'connections', 'composite_id'.
            timeline_id: Optional timeline ID for context.
            clip_id: Optional clip ID for context.
            timeline_frame: Frame number for animation parameter interpolation.
            quality: "preview" or "export" quality mode.
            render_pass: Semantic output pass to read back (``beauty`` or an
                available auxiliary AOV such as ``depth``/``object_id``).
            trace_id: P0-4 correlation id; when supplied it is echoed back
                on the produced trace so Edit/encoder events link up.

        Returns:
            BGR float32 numpy array in [0, 1] range, or None on failure.
        """
        # Validate input
        if not graph_data or 'nodes' not in graph_data:
            logger.warning("CompositeFrameRenderer: empty or invalid graph_data")
            self.last_diagnostics = []
            # AUD-80011: failure paths must not leak the previous frame's
            # performance attribution to T-P1-60 step-6 readers.
            self.last_frame_trace = None
            return None

        graph_id = graph_data.get('composite_id', '')

        try:
            # Build a GraphSnapshot from the graph data
            snapshot = self._build_snapshot(
                graph_data, timeline_frame,
                preview_sync_vt=(quality == "preview"))
            if snapshot is None:
                logger.warning("CompositeFrameRenderer: failed to build snapshot")
                self.last_diagnostics = []
                # AUD-80011: same stale-attribution leak as above.
                self.last_frame_trace = None
                return None

            # Delegate to CompositeExportRenderer
            renderer = self._get_export_renderer()
            runtime_context, provider = self._build_timeline_source_context(
                graph_data=graph_data,
                timeline_id=timeline_id,
                clip_id=clip_id,
                asset_id=asset_id,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                timeline_frame=timeline_frame,
                source_frame=source_frame,
                quality=quality,
                caption_input=caption_input,
            )
            renderer.set_timeline_clip_context(runtime_context, provider)
            # Preview: sRGB uint8 readback produced entirely on the GPU
            # (OETF + BGR + flip in the readback shader) — skips the old
            # f32 full-frame readback + numpy sRGB chain (200ms+/frame at
            # 1080p on the timeline comp-clip path). Export stays linear.
            result = renderer.render_and_readback(
                snapshot,
                readback_output=("srgb_u8" if quality == "preview"
                                 else "linear_f32"),
                render_pass=render_pass,
                trace_id=trace_id)

            # T-P1-60：节点级结构化诊断（MODEL_UNAVAILABLE 等）——即使本帧
            # 出图成功（占位节点直通）也要可查；调用方读 last_diagnostics。
            self.last_diagnostics = list(result.diagnostics or [])
            # T-P1-60 步骤 6：performance attribution（P0-4 frame trace）。
            self.last_frame_trace = result.frame_trace

            if result.error:
                logger.warning(
                    f"CompositeFrameRenderer: render error "
                    f"(graph={graph_id}, frame={timeline_frame}): {result.error}"
                )
                return None
            if result.image is None:
                logger.warning(
                    f"CompositeFrameRenderer: render returned no image "
                    f"(graph={graph_id}, frame={timeline_frame})"
                )
                return None

            if quality == "preview":
                # 逐帧时间线预览（TL-C03）：不再整帧清空求值缓存。静态分支
                # （grade/LUT/mask/文本等 key 不含帧号的节点，P0-2）跨帧保留
                # 后直接命中，不再每帧重渲染；帧相关条目（key 含 :fn）与被
                # 新参数版本取代的旧条目在本帧未被触碰（last_used_frame 落后
                # 于当前帧），由 retain_frame 统一归还资源池——显存上限不变，
                # 仍受 put 时 max_entries / max_memory_mb 预算约束。
                cache_manager = getattr(renderer, "_cache_manager", None)
                if cache_manager is not None:
                    try:
                        cache_manager.retain_frame()
                    except Exception as e:
                        logger.debug(f"preview cache retain failed: {e}")
                        try:
                            cache_manager.clear()
                        except Exception:
                            pass

            return result.image

        except Exception as e:
            logger.error(
                f"CompositeFrameRenderer render error (graph={graph_id}, "
                f"frame={timeline_frame}): {e}"
            )
            # AUD-80011: an exception left both diagnostics and the frame
            # trace holding the PREVIOUS frame's values — a failed frame
            # must not carry stale attribution.
            self.last_diagnostics = []
            self.last_frame_trace = None
            return None

    def render_comp_clip_gpu(
        self,
        graph_data: dict,
        timeline_id: str | None = None,
        clip_id: str | None = None,
        asset_id: str | None = None,
        edit_project: Any | None = None,
        timeline_sequences: dict[str, Any] | None = None,
        timeline_frame: int = 0,
        source_frame: int | None = None,
        caption_input: object | None = None,
        generation: int = 0,
        trace_id: str | None = None,
    ) -> "Any":
        """Render a comp-clip frame WITHOUT readback and return a GPU handle.

        TL-P1-01: normal playback must never produce a QImage or NumPy
        full frame. This API evaluates the composite graph on the GL
        worker's (current) context and wraps the result in a
        ``CompositeGpuFrameHandle`` (P0-5 contract: device identity,
        pixel format, colour/alpha semantics, producer fence, idempotent
        lease). The Edit timeline GPU compositor consumes the handle's
        texture after waiting on its fence and calls ``release()`` when
        its copy is done.

        Must be called on the thread holding the comp-clip GL context.

        Args:
            trace_id: P0-4 correlation id echoed onto the handle's trace.

        Returns:
            CompositeGpuFrameHandle (never None, never raises; failures
            carry ``available=False`` + ``fallback_reason``).
        """
        from src.shared.gpu.composite_gpu_frame_handle import (
            make_unavailable_handle,
        )

        if not graph_data or 'nodes' not in graph_data:
            return make_unavailable_handle("empty or invalid graph_data")
        try:
            snapshot = self._build_snapshot(
                graph_data, timeline_frame, preview_sync_vt=True)
            if snapshot is None:
                return make_unavailable_handle("failed to build snapshot")
            renderer = self._get_export_renderer()
            runtime_context, provider = self._build_timeline_source_context(
                graph_data=graph_data,
                timeline_id=timeline_id,
                clip_id=clip_id,
                asset_id=asset_id,
                edit_project=edit_project,
                timeline_sequences=timeline_sequences,
                timeline_frame=timeline_frame,
                source_frame=source_frame,
                quality="preview",
                caption_input=caption_input,
            )
            renderer.set_timeline_clip_context(runtime_context, provider)
            return renderer.render_gpu_handle(
                snapshot,
                render_pass="beauty",
                frame_number=int(timeline_frame),
                generation=int(generation),
                render_mode=_preview_render_mode(),
                trace_id=trace_id or "",
            )
        except Exception as exc:
            logger.error("render_comp_clip_gpu failed: %s", exc)
            return make_unavailable_handle(str(exc))

    def _build_timeline_source_context(
        self,
        graph_data: dict,
        timeline_id: str | None,
        clip_id: str | None,
        asset_id: str | None,
        edit_project: Any | None,
        timeline_sequences: dict[str, Any] | None,
        timeline_frame: int,
        source_frame: int | None,
        quality: str,
        caption_input: object | None = None,
    ) -> tuple[Any | None, Any | None]:
        """Create runtime/provider pair for TimelineClipInput during headless preview."""
        if edit_project is None:
            return None, None

        timeline_id = (
            timeline_id
            or graph_data.get("timeline_id")
            or (graph_data.get("source_refs") or {}).get("original_timeline_id")
            or getattr(edit_project, "active_timeline_id", None)
            or ""
        )
        clip_id = (
            clip_id
            or graph_data.get("clip_id")
            or graph_data.get("source_clip_id")
            or (graph_data.get("source_refs") or {}).get("original_clip_id")
            or ""
        )
        asset_id = (
            asset_id
            or graph_data.get("source_asset_id")
            or (graph_data.get("source_refs") or {}).get("original_asset_id")
            or ""
        )
        if not clip_id:
            return None, None

        width = int(graph_data.get("width") or 1920)
        height = int(graph_data.get("height") or 1080)
        fps = float(graph_data.get("fps") or 24.0)
        actual_timeline_frame = int(timeline_frame)
        actual_source_frame = int(source_frame if source_frame is not None else timeline_frame)
        clip_local_frame = 0

        timeline = None
        if timeline_sequences:
            timeline = timeline_sequences.get(timeline_id) if timeline_id else None
            if timeline is None:
                timeline = next(iter(timeline_sequences.values()), None)
        clip = None
        if timeline is not None and clip_id:
            try:
                clip = timeline.get_clip_by_id(clip_id)
            except AttributeError:
                clip = None
        if clip is not None:
            clip_local_frame = max(0, actual_timeline_frame - int(getattr(clip, "timeline_start", 0) or 0))
            if source_frame is None:
                try:
                    actual_source_frame = clip.timeline_to_source_frame(actual_timeline_frame)
                except (ValueError, AttributeError):
                    actual_source_frame = int(getattr(clip, "source_start", 0) or 0) + clip_local_frame

        try:
            from src.features.composite.core.runtime_context import CompositeRuntimeContext
        except Exception as e:
            logger.debug(f"CompositeFrameRenderer context imports failed: {e}")
            return None, None

        runtime = CompositeRuntimeContext.for_timeline_clip(
            project_id=getattr(edit_project, "project_id", ""),
            graph_id=graph_data.get("composite_id", ""),
            timeline_id=timeline_id,
            clip_id=clip_id,
            timeline_frame=actual_timeline_frame,
            clip_local_frame=clip_local_frame,
            source_frame=actual_source_frame,
            asset_id=asset_id or None,
            fps=fps,
            width=width,
            height=height,
            quality=quality,
            comp_clip_id=graph_data.get("comp_clip_id") or graph_data.get("composite_id"),
            input_stage="source_raw",
            caption_input=caption_input,
        )
        # Reuse the provider across frames for the same project/graph/clip so
        # decode sessions and the source texture cache stay warm.
        provider_key = (
            getattr(edit_project, "project_id", ""),
            graph_data.get("composite_id", ""),
            clip_id,
            asset_id or "",
        )
        provider = self._get_or_create_provider(
            provider_key, edit_project, timeline_sequences)
        return runtime, provider

    # ================================================================
    # P1-10: async PBO preview lane
    # ================================================================

    def submit_preview_frame_async(self, graph_data: dict,
                                   timeline_id: str | None = None,
                                   clip_id: str | None = None,
                                   asset_id: str | None = None,
                                   edit_project: Any | None = None,
                                   timeline_sequences: dict[str, Any] | None = None,
                                   timeline_frame: int = 0,
                                   source_frame: int | None = None,
                                   caption_input: object | None = None,
                                   trace_id: str | None = None,
                                   ) -> int:
        """Render a preview frame and START its readback without waiting.

        P1-10 producer side: evaluates the graph once (via the GPU-handle
        producer), draws it through the sRGB/BGR/flip pass into a
        ring-owned RGBA8 target and issues ``glReadPixels`` into a PBO.
        Returns immediately after the enqueue — the DMA overlaps whatever
        the caller does next (decode, UI, next frame's evaluate).

        The consumer collects with ``collect_async_preview()`` — normally
        one frame behind, latest-completed semantics, never blocking on
        the current frame's DMA.

        Must be called on the thread holding the comp-clip GL context.

        Args:
            trace_id: P0-4 correlation id carried into the producer trace.

        Returns:
            request_id for stale detection, or 0 on failure.
        """
        handle = self.render_comp_clip_gpu(
            graph_data=graph_data,
            timeline_id=timeline_id,
            clip_id=clip_id,
            asset_id=asset_id,
            edit_project=edit_project,
            timeline_sequences=timeline_sequences,
            timeline_frame=timeline_frame,
            source_frame=source_frame,
            caption_input=caption_input,
            trace_id=trace_id,
        )
        try:
            if handle is None or not getattr(handle, "available", False):
                return 0
            renderer = self._get_export_renderer()
            if renderer.ctx is None:
                return 0
            w, h = int(handle.width), int(handle.height)
            slot = self._get_async_slot(renderer, self._async_slot_idx, w, h)
            if slot is None:
                return 0
            from src.features.composite.render.pbo_readback import (
                AsyncPBOReadback,
            )
            if self._async_pbo is None:
                # reorder_bgr=False: the sRGB flip pass already emits BGR
                # order — a second reorder would undo it.
                self._async_pbo = AsyncPBOReadback(
                    renderer.ctx, ring_size=2, dtype="f1", reorder_bgr=False)
            if not renderer.draw_srgb_into_fbo(
                    handle.texture_id, w, h, slot["fbo"]):
                return 0
            self._async_request_id += 1
            if not self._async_pbo.enqueue(
                    slot["fbo"], w, h,
                    request_id=self._async_request_id):
                return 0
            # Slots alternate in lockstep with the PBO ring (both start at
            # 0, both advance only on success): frame N's DMA must never
            # see its target texture redrawn by frame N+1.
            self._async_slot_idx = (self._async_slot_idx + 1) % 2
            return self._async_request_id
        finally:
            # The f2 copy is consumed: the flip draw was queued after it on
            # the same context, so returning it to the pool here is safe.
            if handle is not None:
                try:
                    handle.release()
                except Exception:
                    pass

    def collect_async_preview(
            self, wait: bool = False) -> "tuple[int, np.ndarray] | None":
        """Collect the newest COMPLETED async preview frame.

        Args:
            wait: False (default) → never blocks; returns the previous
                frame's pixels or None. True → blocks until the newest
                enqueued frame's DMA completes (final-frame correctness).

        Returns:
            (request_id, BGR uint8 (h, w, 3)) or None.
        """
        if self._async_pbo is None:
            return None
        result = self._async_pbo.get_latest(wait_newest=wait)
        if result is None:
            return None
        self._async_download_bytes += int(result.data.nbytes)
        return result.request_id, result.data[..., :3]

    @property
    def async_download_bytes(self) -> int:
        """CPU download bytes moved through the async lane (P0-4 metric)."""
        return self._async_download_bytes

    def _get_async_slot(self, renderer: Any, idx: int,
                        w: int, h: int) -> Optional[dict]:
        """Return ring slot ``idx``'s alive RGBA8 target, (re)sized.

        Slots alternate in step with the PBO ring (see the caller), so a
        slot's texture is only redrawn after its PBO went around the ring
        (enqueue itself force-waits a still-in-flight slot).
        """

        def _make_slot() -> dict:
            tex = renderer._resource_pool.acquire_texture(
                w, h, components=4, dtype="f1")
            return {
                "tex": tex, "fbo": renderer.fb_manager.get_fbo(tex),
                "w": w, "h": h,
            }

        while len(self._async_slots) <= idx:
            try:
                self._async_slots.append(_make_slot())
            except Exception as e:
                logger.warning("async preview slot alloc failed: %s", e)
                return None
        slot = self._async_slots[idx]
        if slot["w"] != w or slot["h"] != h:
            # Quality/size change: recycle through the pool and reallocate.
            try:
                renderer._resource_pool.release_texture(slot["tex"])
            except Exception:
                pass
            try:
                new_slot = _make_slot()
            except Exception as e:
                logger.warning("async preview slot realloc failed: %s", e)
                return None
            slot.update(new_slot)
        return slot

    def cleanup(self) -> None:
        """Release all GPU resources. Call when the renderer is no longer needed."""
        if self._async_pbo is not None:
            try:
                self._async_pbo.cleanup()
            except Exception as e:
                logger.debug(f"async pbo cleanup error: {e}")
            self._async_pbo = None
        if self._export_renderer is not None and self._async_slots:
            for slot in self._async_slots:
                try:
                    self._export_renderer._resource_pool.release_texture(
                        slot["tex"])
                except Exception:
                    pass
        self._async_slots = []
        if self._export_renderer is not None:
            try:
                self._export_renderer.cleanup()
            except Exception as e:
                logger.debug(f"CompositeFrameRenderer cleanup error: {e}")
            self._export_renderer = None
        self._snapshot_graph_cache = None
        self._release_provider()
        logger.debug("CompositeFrameRenderer cleaned up")

    def invalidate_graph_cache(self) -> None:
        """Drop cached node results after the composite graph was edited.

        In persistent preview mode the export renderer's CacheManager keeps
        node outputs across frames; a param change must not reuse them.
        """
        renderer = self._export_renderer
        cache_manager = getattr(renderer, "_cache_manager", None) if renderer else None
        if cache_manager is not None:
            try:
                cache_manager.clear()
            except Exception as e:
                logger.debug(f"CompositeFrameRenderer cache invalidation error: {e}")
        # 图已编辑：反序列化缓存一并作废（graph_data dict 虽会被整体替换
        # 使 id 失效，但原地 merge 的路径不能依赖这一点）。
        self._snapshot_graph_cache = None

    # ================================================================
    # Internal
    # ================================================================

    def _get_or_create_provider(
        self,
        key: tuple,
        edit_project: Any,
        timeline_sequences: dict[str, Any] | None,
    ) -> Any:
        """Return the cached TimelineClipFrameProvider, recreating on key change.

        The provider owns decode-session references and a source texture
        cache; reusing it across frames avoids per-frame session rebuilds
        and keeps the texture cache warm. Cheap setters are re-applied every
        frame so project/sequence updates are always reflected.
        """
        from src.features.edit.core.timeline_clip_frame_provider import (
            TimelineClipFrameProvider,
        )

        if self._provider is None or self._provider_key != key:
            self._release_provider()
            self._provider = TimelineClipFrameProvider()
            # The provider imports deferred VT/CVPixelBuffer frames on this
            # renderer's current GL thread.  It does not reuse texture names
            # from the Edit context, so the old blanket disable is no longer
            # needed; unsupported platforms/formats still capability-fallback
            # to the bounded CPU/planar path inside the provider.
            self._provider.set_allow_external_gpu_frames(True)
            self._provider_key = key
        self._provider.set_project(edit_project)
        if timeline_sequences:
            self._provider.set_timeline_sequences(timeline_sequences)
        return self._provider

    def _release_provider(self) -> None:
        """Release the cached provider's decode sessions and texture cache."""
        provider = self._provider
        self._provider = None
        self._provider_key = None
        if provider is None:
            return
        try:
            provider.clear_source_cache()
        except Exception as e:
            logger.debug(f"Provider source cache cleanup error: {e}")
        try:
            provider.release_video_sessions()
        except Exception as e:
            logger.debug(f"Provider video session cleanup error: {e}")

    def _get_export_renderer(self) -> Any:
        """Lazy-initialize and return the internal CompositeExportRenderer.

        The renderer is created on first use and reused across frames.
        CompositeExportRenderer handles GPU context creation and evaluation.
        """
        if self._export_renderer is None:
            from src.features.composite.render.composite_export_renderer import (
                CompositeExportRenderer,
            )
            self._export_renderer = CompositeExportRenderer()
            # Preview drives this renderer frame-by-frame on the worker's
            # shared GL context: keep shaders/resource pool alive across
            # frames instead of rebuilding them per frame.
            self._export_renderer.set_persistent_mode(True)
            # Forward config_manager so FileOutputNode etc. honour the user's
            # composite.color_engine (builtin vs OCIO) instead of OCIO default.
            if self._config_manager is not None:
                self._export_renderer.set_config_manager(self._config_manager)
        return self._export_renderer

    def warm_up(self) -> None:
        """Eagerly create the GL context on the CALLING thread.

        A3 parallel export: the per-worker factory runs inside its own
        worker thread, so calling this there fails fast (→ serial fallback)
        instead of the first frame dying mid-export. Also guarantees the
        context is thread-affine to the thread that will render.
        """
        renderer = self._get_export_renderer()
        ensure = getattr(renderer, "_ensure_gpu_resources", None)
        if callable(ensure):
            ensure()

    def _build_snapshot(
        self, graph_data: dict, frame_number: int = 0,
        preview_sync_vt: bool = False,
    ) -> Optional[GraphSnapshot]:
        """Build a GraphSnapshot from a CompositeGraphData dict.

        Uses NodeGraph.from_dict() to deserialize, then takes a snapshot.

        Args:
            graph_data: CompositeGraphData dict with 'nodes', 'connections', etc.
            frame_number: Frame number for animation interpolation.

        Returns:
            GraphSnapshot, or None on failure.
        """
        try:
            # 反序列化缓存：graph_data 在播放中是同一对象（编辑时被整体
            # 替换，id 随之改变），命中时跳过全图 from_dict。
            cache_key = (id(graph_data), self._graph_revision_token(graph_data))
            cache = self._snapshot_graph_cache
            if cache is not None and cache[0] == cache_key:
                node_graph = cache[2]
            else:
                node_graph = NodeGraph.from_dict(graph_data)
                # Hold a strong reference to graph_data: a live object's
                # id() can never be reused, so a freed-and-reallocated dict
                # can no longer collide with the cached identity and serve
                # a stale deserialization.
                self._snapshot_graph_cache = (cache_key, graph_data, node_graph)

            # Take a snapshot from the deserialized graph.
            # color_texture_id=0 because we don't have a ColorOutput texture
            # (comp_clip graphs use their own source nodes, not the Color page output).
            # 时间线预览（非导出）：视频帧用 VT 硬解同步解码（参数注入，
            # video_frame 节点读到此标志后在 export 同步兜底分支优先
            # VT 精确读取，miss 才回退 CPU 软解——1080p HEVC 实测软解
            # ~92ms/帧，VT 同步读取 <20ms）。与缓存图共用同一对象，
            # 注入/移除必须对称，避免污染 export 语义。
            for node in node_graph.nodes.values():
                if getattr(node, "node_type", "") == "video_frame":
                    if preview_sync_vt:
                        node.parameters["timeline_sync_vt"] = True
                    else:
                        node.parameters.pop("timeline_sync_vt", None)

            snapshot = node_graph.snapshot(
                color_texture_id=0,
                color_width=0,
                color_height=0,
                color_revision=0,
                frame_number=frame_number,
                fps=float(graph_data.get("fps", 24.0) or 24.0),
            )

            return snapshot

        except Exception as e:
            logger.error(f"CompositeFrameRenderer snapshot build error: {e}")
            return None

    @staticmethod
    def _graph_revision_token(graph_data: dict) -> Any:
        """Return a cheap persisted graph revision, if the producer exposes one.

        ``NodeGraph.to_dict()`` writes ``graph_revision``. Older
        ``CompositeGraphData`` payloads expose ``modified_at`` instead, so it
        is a useful compatibility fallback. The returned value is deliberately
        not derived from nodes/connections: deep hashing would add work to
        every realtime frame and still would not replace explicit invalidation
        for legacy payloads.
        """
        if not isinstance(graph_data, dict):
            return None
        for key in ("graph_revision", "revision", "modified_at"):
            value = graph_data.get(key)
            if value is not None:
                return (key, value)
        return None
