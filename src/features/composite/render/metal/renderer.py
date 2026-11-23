"""MetalCompositeRenderer — 合成图的 Metal compute 执行器（阶段 5.1–5.6）。

职责与边界：

- 入口是序列化合成图（与 ``CompositeGraphRenderService`` 的 CPU/GL 路径
  同一数据源）；预检不通过 → 返回**显式不可用**的
  ``CompositeGpuFrameHandle``（含节点 ID/类型的回退诊断），整图回退由
  调用方决定，绝不跳过未迁移节点继续产出。
- 全帧与分块共用同一执行模型：节点按扩张区域（`roi.input_roi` + halo）
  dispatch，Output 节点只写 tile 中心（裁回中心）。
- 完成与租约语义（阶段 1 契约 / 阶段 5.6 有界异步发布）：

  1. **编码前预留环容量**：``MetalSubmissionScheduler.try_reserve``；
     环满（back-pressure）时先 ``poll_completions()`` 回收一次再试，
     仍满则显式回退本帧，不发布半成品。
  2. **GPU 完成前不发布**：``render_async`` 返回
     :class:`MetalFramePending`；完成回调（驱动线程只入队）在拥有者线程
     的 :meth:`poll_completions` 里执行——中间纹理 finalize、tile 缓存
     写入、**然后**构造 ``CompositeGpuFrameHandle``（``fence_ns=0`` 表示
     内容完整：完成事件已经到达）。
  3. 输入各释放一次：所有中间纹理在提交时 ``retire``，完成回调
     ``finalize_retire``；输出纹理经 handle 租约归还，幂等。
  4. 等待超时/设备暂停：``pending.wait(timeout)`` 超时显式回退并**放弃**
     该帧（迟到的完成在回调里直接回收租约，不发布）；调度器的
     ``pending_timeouts`` 会暂停该设备的新提交，排空后自动恢复。

- 同步入口 :meth:`render` 保留（对拍/诊断/单帧调用方）：内部同样走异步
  提交，只是 ``wait()`` 驱动 poll 直到完成——不再 ``waitUntilCompleted``。
- 阶段 5.4：请求携带 ``segment_id``；``TemporalHistoryTracker`` 记录
  正放/倒放/seek/跨剪切的边界，时域图在重置边界给出含节点 ID/类型与
  所需历史的拒绝诊断（当前所有时域节点在预检即整图回退）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from src.shared.gpu.composite_gpu_frame_handle import (
    CompositeGpuFrameHandle,
    make_unavailable_handle,
)
from src.shared.gpu.metal import MetalComputeContext, MetalComputeError
from src.shared.gpu.metal.submission_scheduler import MetalSubmissionScheduler

from . import graph_plan as gp
from . import roi
from .cpu_reference import _consumed_mask_port
from .graph_plan import ExecutionPlan, FrameMeta, PlannedNode, Rect
from .kernels import (
    ALPHA_PARAMS_DT,
    CONSTANT_PARAMS_DT,
    COPY_PARAMS_DT,
    CROP_PARAMS_DT,
    MASK_PARAMS_DT,
    MERGE_PARAMS_DT,
    METAL_COMPOSITE_KERNEL_SOURCES,
    OUTPUT_PARAMS_DT,
    TRANSFORM_PARAMS_DT,
    region_dict,
)
from .node_support import MetalSupportReport, evaluate_metal_support
from .texture_pool import MetalPoolExhausted, MetalTexturePool, PooledTexture
from .time_contract import (
    TemporalHistoryTracker,
    evaluate_temporal_requirements,
    temporal_fallback_reason,
)
from .tile_cache import MetalTileCache, composite_cache_key, graph_params_digest

logger = logging.getLogger(__name__)

_RECT_ZERO: Rect = (0, 0, 0, 0)


class MetalFramePending:
    """``render_async`` 的在途帧：GPU 完成后才发布 handle。

    生命周期：``SUBMITTED``（GPU 可能仍在执行）→ 完成（``poll`` 里发布
    handle 或超时被放弃）。发布后 ``wait``/``try_get`` 返回内容完整的
    ``CompositeGpuFrameHandle``；超时返回显式回退句柄，迟到的完成在
    回调里直接回收租约（绝不发布半成品/孤儿句柄给第二个消费者）。
    """

    def __init__(self, renderer: "MetalCompositeRenderer", sequence: int,
                 frame: int, generation: int, trace_id: str) -> None:
        self._renderer = renderer
        self._sequence = int(sequence)
        self.frame = int(frame)
        self.generation = int(generation)
        self.trace_id = str(trace_id)
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._handle: Optional[CompositeGpuFrameHandle] = None
        self._settled = False
        self._abandoned = False

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def _settle(self, handle: CompositeGpuFrameHandle) -> None:
        """完成回调（拥有者线程 poll 内）调用：发布或回收。"""
        with self._lock:
            if self._settled:
                # 防御：不应发生（完成回调恰好一次）。
                handle.release()
                return
            if self._abandoned:
                # 等待方已超时回退：本 handle 无人消费，直接归还租约。
                self._settled = True
                self._handle = None
                abandoned = True
            else:
                self._settled = True
                self._handle = handle
                abandoned = False
        if abandoned:
            handle.release()
        else:
            self._event.set()

    def wait(self, timeout_s: float = 10.0) -> CompositeGpuFrameHandle:
        """等待 GPU 完成并返回已发布的 handle（超时显式回退）。"""
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while not self._event.wait(0.005):
            self._renderer.poll_completions()
            if self._event.is_set():
                break
            if time.monotonic() >= deadline:
                with self._lock:
                    if self._settled and self._handle is not None:
                        break
                    self._abandoned = True
                self._renderer._forget_pending(self)
                return make_unavailable_handle(
                    f"等待 Metal GPU 完成超时（>{timeout_s:.1f}s），本帧回退",
                    generation=self.generation, frame=self.frame,
                    trace_id=self.trace_id)
        with self._lock:
            handle = self._handle
        self._renderer._forget_pending(self)
        if handle is None:  # pragma: no cover - _settle 保证与 event 配对
            return make_unavailable_handle(
                "Metal 帧完成状态异常", generation=self.generation,
                frame=self.frame, trace_id=self.trace_id)
        return handle

    def try_get(self) -> Optional[CompositeGpuFrameHandle]:
        """非阻塞：完成返回 handle，未完成返回 None（并顺带 poll 一次）。"""
        self._renderer.poll_completions()
        if not self._event.is_set():
            return None
        with self._lock:
            handle = self._handle
        if handle is not None:
            self._renderer._forget_pending(self)
        return handle


class MetalCompositeRenderer:
    """以 Metal compute 执行已迁移节点组成的合成图。"""

    # 完成事件超时（秒）：超时的提交暂停该设备的新提交（调度器契约）。
    COMPLETION_TIMEOUT_S = 10.0

    def __init__(
        self,
        context: Optional[MetalComputeContext] = None,
        scheduler: Optional[MetalSubmissionScheduler] = None,
        pool: Optional[MetalTexturePool] = None,
        cache: Optional[MetalTileCache] = None,
    ) -> None:
        self._context = context
        self._scheduler = scheduler
        self._pool = pool
        self._cache = cache if cache is not None else MetalTileCache()
        self._last_support: Optional[MetalSupportReport] = None
        self._last_plan: Optional[ExecutionPlan] = None
        self._last_gpu_ms: Optional[float] = None
        # 本帧分配的中间纹理（提交后 retire，完成回调里 finalize）。
        self._pending: List[PooledTexture] = []
        # 已发布句柄的槽位 → 纹理记账（readback/release 用）。
        self._published: Dict[int, PooledTexture] = {}
        # 阶段 5.4：历史边界跟踪（正放/倒放/seek/跨剪切/代际）。
        self._history = TemporalHistoryTracker()
        self.last_history_reset_reason: str = ""
        # 提交序号 → 在途帧。
        self._pending_frames: Dict[int, MetalFramePending] = {}

    # ── 属性 ──

    @property
    def pool(self) -> Optional[MetalTexturePool]:
        return self._pool

    @property
    def scheduler(self) -> Optional[MetalSubmissionScheduler]:
        return self._scheduler

    @property
    def cache(self) -> MetalTileCache:
        return self._cache

    @property
    def last_support(self) -> Optional[MetalSupportReport]:
        return self._last_support

    @property
    def last_plan(self) -> Optional[ExecutionPlan]:
        return self._last_plan

    @property
    def last_gpu_time_ms(self) -> Optional[float]:
        return self._last_gpu_ms

    @property
    def in_flight_frames(self) -> int:
        """在途（已提交未发布）帧数（环占用诊断）。"""
        return sum(1 for pending in self._pending_frames.values()
                   if not pending.done)

    def _forget_pending(self, pending: "MetalFramePending") -> None:
        """句柄已被消费/放弃：从注册表移除（幂等）。"""
        self._pending_frames.pop(pending.sequence, None)

    # ── 生命周期 ──

    def _ensure_context(self) -> MetalComputeContext:
        if self._context is None:
            self._context = MetalComputeContext()
        if self._scheduler is None:
            self._scheduler = MetalSubmissionScheduler(
                self._context,
                completion_timeout_s=self.COMPLETION_TIMEOUT_S)
        if self._pool is None:
            self._pool = MetalTexturePool(self._context)
        return self._context

    def cleanup(self) -> None:
        """释放资源（关闭/切页）。在途工作先排空再销毁。"""
        if self._scheduler is not None:
            self._scheduler.drain()
            # drain 已让完成回调落地：发布的 handle 由消费者持有租约；
            # 无人消费的已发布句柄与未结算的 pending 在此归还/拒绝。
            for pending in list(self._pending_frames.values()):
                if pending.done:
                    handle = pending.try_get()
                    if handle is not None and handle.available:
                        handle.release()
                else:
                    pending._settle(make_unavailable_handle(
                        "渲染器已清理，本帧不发布", frame=pending.frame,
                        generation=pending.generation,
                        trace_id=pending.trace_id))
            self._pending_frames.clear()
        if self._pool is not None:
            self._pool.destroy()
        self._cache.clear()
        self._pending.clear()
        self._published.clear()
        self._context = None
        self._scheduler = None
        self._pool = None

    # ── 渲染入口 ──

    def render(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        generation: int = 0,
        tile_size: Optional[Tuple[int, int]] = None,
        use_cache: bool = True,
        trace_id: str = "",
        segment_id: str = "",
        params_resolved: bool = False,
    ) -> CompositeGpuFrameHandle:
        """渲染一帧并发布 GPU 句柄（同步包装：完成事件驱动，无
        ``waitUntilCompleted``）。不通过预检时返回显式回退句柄。"""
        outcome = self.render_async(
            graph_data, timeline_frame=timeline_frame, generation=generation,
            tile_size=tile_size, use_cache=use_cache, trace_id=trace_id,
            segment_id=segment_id, params_resolved=params_resolved)
        if isinstance(outcome, CompositeGpuFrameHandle):
            return outcome
        return outcome.wait(timeout_s=self.COMPLETION_TIMEOUT_S)

    def render_async(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        generation: int = 0,
        tile_size: Optional[Tuple[int, int]] = None,
        use_cache: bool = True,
        trace_id: str = "",
        segment_id: str = "",
        params_resolved: bool = False,
    ) -> Union[MetalFramePending, CompositeGpuFrameHandle]:
        """渲染一帧：有界异步提交，GPU 完成后才发布句柄。

        Returns:
            :class:`MetalFramePending`（完成经 :meth:`poll_completions`
            发布）；预检/预留不通过时直接返回显式不可用句柄（整图回退
            由调用方决定）。
        """
        support = evaluate_metal_support(graph_data,
                                         params_resolved=params_resolved)
        self._last_support = support
        if not support.supported:
            reason = support.fallback_summary or "合成图未通过 Metal 预检"
            logger.info("MetalCompositeRenderer: %s", reason)
            return make_unavailable_handle(
                reason, generation=generation, frame=timeline_frame,
                trace_id=trace_id)
        # 阶段 5.4：历史边界（时域节点当前在预检即回退；此处为已迁移
        # 时域节点与后续接线保留可执行的边界逻辑）。
        decision = self._history.note_request(
            timeline_frame, generation=generation, segment_id=segment_id)
        self.last_history_reset_reason = (
            decision.reason if decision.reset else "")
        temporal = evaluate_temporal_requirements(graph_data)
        if temporal and decision.reset:
            reason = temporal_fallback_reason(temporal, decision)
            logger.info("MetalCompositeRenderer: %s", reason)
            return make_unavailable_handle(
                reason, generation=generation, frame=timeline_frame,
                trace_id=trace_id)
        try:
            started = time.monotonic()
            return self._render_supported(
                graph_data, timeline_frame, generation, tile_size, use_cache,
                trace_id, started)
        except MetalComputeError as exc:
            logger.error("MetalCompositeRenderer 执行失败: %s", exc)
            return make_unavailable_handle(
                f"Metal 执行失败: {exc}", generation=generation,
                frame=timeline_frame, trace_id=trace_id)
        except (gp.PlanError, ValueError) as exc:
            logger.error("MetalCompositeRenderer 计划失败: %s", exc)
            return make_unavailable_handle(
                f"Metal 计划失败: {exc}", generation=generation,
                frame=timeline_frame, trace_id=trace_id)
        except MetalPoolExhausted as exc:
            logger.error("MetalCompositeRenderer 纹理池耗尽: %s", exc)
            return make_unavailable_handle(
                f"Metal 纹理池耗尽: {exc}", generation=generation,
                frame=timeline_frame, trace_id=trace_id)

    def render_readback(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        tile_size: Optional[Tuple[int, int]] = None,
        use_cache: bool = False,
        segment_id: str = "",
        params_resolved: bool = False,
    ) -> Optional[np.ndarray]:
        """渲染并读回 ``(h, w, 4)`` float32（对拍/导出路径）。"""
        pending = self.render_async(
            graph_data, timeline_frame=timeline_frame, tile_size=tile_size,
            use_cache=use_cache, segment_id=segment_id,
            params_resolved=params_resolved)
        if isinstance(pending, CompositeGpuFrameHandle):
            if not pending.available:
                return None
            handle = pending
        else:
            handle = pending.wait(timeout_s=self.COMPLETION_TIMEOUT_S)
            if not handle.available:
                return None
        try:
            return self.readback(handle)
        finally:
            handle.release()

    def readback(self, handle: CompositeGpuFrameHandle) -> np.ndarray:
        """把已发布句柄的纹理读回 numpy（诊断/导出/对拍用）。"""
        if not handle.available or self._pool is None or self._context is None:
            raise MetalComputeError("handle 不可用或渲染器已清理")
        entry = self._published.get(handle.texture_id)
        if entry is None:
            raise MetalComputeError(
                f"句柄纹理槽 {handle.texture_id} 不属于本渲染器")
        raw = self._context.read_texture(
            entry.texture, handle.width * 4, handle.height,
            dtype=np.float16, region_width=handle.width)
        return raw.reshape(handle.height, handle.width, 4).astype(np.float32)

    def poll_completions(self) -> Tuple[int, ...]:
        """拥有者线程：回收完成提交、发布句柄、处理完成超时。"""
        if self._scheduler is None:
            return ()
        timed_out = self._scheduler.pending_timeouts()
        if timed_out and not self._scheduler.paused:
            self._scheduler.pause(
                f"completion_timeout:{list(timed_out)}")
        completed = self._scheduler.poll()
        if self._scheduler.paused and self._scheduler.resume_if_idle():
            logger.info(
                "MetalCompositeRenderer: GPU 已排空，恢复接受新提交")
        return completed

    # ── 内部：整帧编排 ──

    def _render_supported(
        self, graph_data: dict, timeline_frame: int, generation: int,
        tile_size: Optional[Tuple[int, int]], use_cache: bool,
        trace_id: str, started: float,
    ) -> Union[MetalFramePending, CompositeGpuFrameHandle]:
        context = self._ensure_context()
        plan = gp.build_execution_plan(graph_data, timeline_frame)
        self._last_plan = plan
        index = plan.node_index()
        out_node = index[plan.output_node_id]
        out_meta = out_node.meta
        assert out_meta is not None
        canvas_w, canvas_h = plan.output_canvas

        halo = self._graph_halo(graph_data, plan)
        single_pass = tile_size is None
        output_cs = _output_color_space(out_node)

        assert self._pool is not None and self._scheduler is not None
        # 有界调度：编码前预留环容量；环满先回收一次，仍满显式回退。
        sequence = self._scheduler.try_reserve()
        if sequence is None:
            self.poll_completions()
            sequence = self._scheduler.try_reserve()
        if sequence is None:
            return make_unavailable_handle(
                "Metal 提交环已满（back-pressure），本帧回退",
                generation=generation, frame=timeline_frame,
                trace_id=trace_id)

        try:
            output_entry = self._pool.acquire(canvas_w, canvas_h)
            output_entry.pack = (0, 0, canvas_w, canvas_h)
            self._pending.append(output_entry)
            self._published[output_entry.slot] = output_entry

            tiles: List[Rect] = (
                [(0, 0, canvas_w, canvas_h)] if single_pass
                else list(roi.iter_tile_rects(canvas_w, canvas_h, *tile_size)))
            params_digest = graph_params_digest(graph_data)
            cache_stores: List[Tuple[str, Rect]] = []

            for tile in tiles:
                expanded, _crop = roi.input_roi(
                    tile, halo, canvas_w, canvas_h)
                key = composite_cache_key(
                    graph_revision=plan.graph_revision,
                    frame=timeline_frame,
                    node_id=plan.output_node_id, port="display",
                    width=canvas_w, height=canvas_h, tile_rect=tile,
                    color_space=output_cs,
                    alpha_premultiplied=bool(out_meta.alpha_premultiplied),
                    encode_srgb=_output_encodes_srgb(out_node),
                    params_digest=params_digest)
                cached = self._cache.get(key) if use_cache else None
                if cached is not None:
                    self._blit_cached_tile(cached, tile, output_entry)
                    continue
                self._execute_tile_pass(plan, expanded, tile, output_entry,
                                        is_tile_pass=not single_pass)
                if use_cache:
                    cache_stores.append((key, tile))
        except Exception:
            # 编码失败：丢弃半编码 buffer，取消预留，已分配纹理立即归还
            # （未提交，GPU 从未接触）。
            self._context.discard_pending()
            self._scheduler.cancel_reservation(sequence)
            self._force_release_pending()
            raise

        pending = MetalFramePending(self, sequence, timeline_frame,
                                    generation, trace_id)
        release_callbacks = self._retire_pending(exclude=output_entry)

        def on_complete(submission, error) -> None:
            self._on_frame_completed(
                pending, submission, error, output_entry, cache_stores,
                timeline_frame, generation, out_meta, output_cs, trace_id,
                len(tiles), halo, started)

        submission = self._scheduler.submit_reserved(
            sequence, release_callbacks=release_callbacks,
            on_complete=on_complete)
        if submission is None:
            # 环预留失效或上下文无已编码工作：整组丢弃并显式回退。
            self._context.discard_pending()
            self._force_release_pending()
            return make_unavailable_handle(
                "Metal 提交失败（无已编码工作或预留失效），本帧回退",
                generation=generation, frame=timeline_frame,
                trace_id=trace_id)
        self._pending_frames[sequence] = pending
        return pending

    def _on_frame_completed(
        self,
        pending: MetalFramePending,
        submission,
        error: Optional[str],
        output_entry: PooledTexture,
        cache_stores: List[Tuple[str, Rect]],
        timeline_frame: int,
        generation: int,
        out_meta: FrameMeta,
        output_cs: str,
        trace_id: str,
        tile_count: int,
        halo: int,
        started: float,
    ) -> None:
        """完成回调（拥有者线程 poll 内）：发布句柄或显式失败。"""
        assert self._pool is not None
        self._last_gpu_ms = _measure_gpu_time(submission.command_buffer)
        if error is not None:
            # GPU 报告错误：不发布半成品；完成已到达，无在途工作，
            # 直接归还输出纹理租约。
            self._release_output(output_entry)
            pending._settle(make_unavailable_handle(
                f"Metal command buffer 执行失败: {error}",
                generation=generation, frame=timeline_frame,
                trace_id=trace_id))
            return
        if cache_stores:
            try:
                self._store_tile_caches(cache_stores, output_entry)
            except MetalComputeError as exc:
                # 缓存写入（读回）失败只降级为本帧不缓存，帧本身有效。
                logger.warning(
                    "MetalCompositeRenderer: tile 缓存写入失败（忽略）: %s",
                    exc)
        handle = CompositeGpuFrameHandle(
            frame=timeline_frame,
            generation=generation,
            texture_id=output_entry.slot,
            width=output_entry.width,
            height=output_entry.height,
            pixel_format="f2",
            color_space=output_cs,
            range="full",
            alpha_premultiplied=bool(out_meta.alpha_premultiplied),
            # Metal 纹理按图像行序（top-left）读回；GL 消费者需翻转。
            is_gl_oriented=False,
            backend="metal",
            device_identity=(self._context.device_identity
                             if self._context is not None else ""),
            fence_ns=0,  # 完成事件已到达：内容完整（handle 契约的 finish 兜底）
            release_callback=lambda: self._release_output(output_entry),
            available=True,
            trace_id=trace_id,
        )
        logger.debug(
            "MetalCompositeRenderer: frame=%s tiles=%d halo=%d "
            "elapsed=%.1fms", timeline_frame, tile_count, halo,
            (time.monotonic() - started) * 1000.0)
        pending._settle(handle)

    def _retire_pending(self, exclude: Optional[PooledTexture] = None) -> Tuple:
        """把本帧中间纹理置为待回收，返回完成回调（各释放一次）。"""
        pool = self._pool
        assert pool is not None
        callbacks = []
        for entry in self._pending:
            if entry is exclude:
                continue
            if pool.retire(entry):
                callbacks.append(_finalize_once(pool, entry))
        self._pending = []
        return tuple(callbacks)

    def _force_release_pending(self) -> None:
        """失败路径：未提交的纹理直接归还（GPU 从未接触）。"""
        pool = self._pool
        if pool is None:
            return
        for entry in self._pending:
            pool.release_direct(entry)
        self._pending = []

    def _release_output(self, entry: PooledTexture) -> None:
        if self._pool is not None:
            # 发布仅在完成回调之后：无在途 GPU 工作，直接归还
            # （finalize 为幂等兜底，正常路径输出纹理不在 retire 队列）。
            self._pool.finalize_retire(entry)
            self._pool.release_direct(entry)
        self._published.pop(entry.slot, None)

    def _graph_halo(self, graph_data: dict, plan: ExecutionPlan) -> int:
        propagation = roi.propagate_input_halos(
            graph_data.get("nodes") or [],
            graph_data.get("connections") or [],
            [plan.output_node_id])
        return propagation.node_input_halo.get(plan.output_node_id, 0)

    # ── 内部：单 tile pass ──

    def _execute_tile_pass(
        self, plan: ExecutionPlan, expanded: Rect, tile: Rect,
        output_entry: PooledTexture, is_tile_pass: bool,
    ) -> None:
        gp.assign_execution_regions(plan, expanded, is_tile_pass)
        index = plan.node_index()
        entries: Dict[str, Optional[PooledTexture]] = {}

        for node in plan.nodes:
            meta = node.meta
            assert meta is not None
            inputs = {port: entries[src] for port, src in node.inputs.items()
                      if src in entries}
            region = node.region
            if region[2] <= region[0] or region[3] <= region[1]:
                entries[node.node_id] = None
                continue
            if meta.passthrough:
                entries[node.node_id] = next(iter(inputs.values()), None)
                continue

            if node.node_type == "output":
                # 输出节点：dispatch tile 中心（裁回中心），写入整帧纹理。
                self._dispatch_output_node(node, inputs.get("image"), tile,
                                           output_entry)
                entries[node.node_id] = output_entry
                continue

            width = region[2] - region[0]
            height = region[3] - region[1]
            assert self._pool is not None
            out_entry = self._pool.acquire(width, height)
            out_entry.pack = region
            entries[node.node_id] = out_entry
            self._pending.append(out_entry)

            self._dispatch_node(node, inputs, region, out_entry, index, plan)

    def _dispatch_node(
        self, node: PlannedNode, inputs: Dict[str, Optional[PooledTexture]],
        region: Rect, out_entry: PooledTexture,
        index: Dict[str, PlannedNode], plan: ExecutionPlan,
    ) -> None:
        t = node.node_type
        origin = region_dict(region[0], region[1],
                             region[2] - region[0], region[3] - region[1])
        ctx = self._context
        if t == "constant":
            params = {**origin, **gp.constant_params(node)}
            buffer = ctx.create_buffer_from_numpy(
                _pack(CONSTANT_PARAMS_DT, params))
            ctx.dispatch(self._kernel("comp_constant"),
                         buffers={0: buffer}, textures={0: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        if t == "transform":
            src = inputs.get("image")
            src_meta = self._meta_of(node, "image")
            assert src_meta is not None
            params = {**origin, **gp.transform_params(node, src_meta)}
            gp.fill_input_pack(params, src.pack if src else _RECT_ZERO)
            buffer = ctx.create_buffer_from_numpy(
                _pack(TRANSFORM_PARAMS_DT, params))
            src_tex = src.texture if src is not None else out_entry.texture
            ctx.dispatch(self._kernel("comp_transform"),
                         buffers={0: buffer},
                         textures={0: src_tex, 1: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        if t == "merge":
            frame_a = inputs.get("A")
            frame_b = inputs.get("B")
            frame_m = inputs.get("mask")
            params = {**origin, **gp.merge_params(
                node, self._meta_of(node, "A"), self._meta_of(node, "B"),
                self._meta_of(node, "mask"))}
            params["pack_a"] = frame_a.pack if frame_a else _RECT_ZERO
            params["pack_b"] = frame_b.pack if frame_b else _RECT_ZERO
            params["pack_mask"] = frame_m.pack if frame_m else _RECT_ZERO
            buffer = ctx.create_buffer_from_numpy(_pack(MERGE_PARAMS_DT, params))
            fallback = (frame_a or frame_b or out_entry).texture
            tex_a = frame_a.texture if frame_a is not None else fallback
            tex_b = frame_b.texture if frame_b is not None else tex_a
            tex_m = frame_m.texture if frame_m is not None else tex_a
            ctx.dispatch(self._kernel("comp_merge"),
                         buffers={0: buffer},
                         textures={0: tex_a, 1: tex_b, 2: tex_m,
                                   3: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        if t in ("premult", "unpremult"):
            src = inputs.get("image")
            params = {**origin, **gp.alpha_op_params(
                1 if t == "unpremult" else 0)}
            buffer = ctx.create_buffer_from_numpy(
                _pack(ALPHA_PARAMS_DT, params))
            src_tex = src.texture if src is not None else out_entry.texture
            ctx.dispatch(self._kernel("comp_alpha_op"),
                         buffers={0: buffer},
                         textures={0: src_tex, 1: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        if t == "crop":
            src = inputs.get("image")
            src_meta = self._meta_of(node, "image")
            assert src_meta is not None
            params = {**origin, **gp.crop_params(node, src_meta)}
            gp.fill_crop_pack(params, src.pack if src else _RECT_ZERO, region)
            buffer = ctx.create_buffer_from_numpy(_pack(CROP_PARAMS_DT, params))
            src_tex = src.texture if src is not None else out_entry.texture
            ctx.dispatch(self._kernel("comp_crop"),
                         buffers={0: buffer},
                         textures={0: src_tex, 1: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        if t == "mask":
            src = inputs.get("image")
            port = _consumed_mask_port(plan, index, node)
            params = {**origin, **gp.mask_params(node, src is not None, port)}
            buffer = ctx.create_buffer_from_numpy(_pack(MASK_PARAMS_DT, params))
            src_tex = src.texture if src is not None else out_entry.texture
            ctx.dispatch(self._kernel("comp_mask_rect"),
                         buffers={0: buffer},
                         textures={0: src_tex, 1: out_entry.texture},
                         threads=(params["size_x"], params["size_y"], 1))
            return
        raise gp.PlanError(f"未支持的节点类型 {t!r}")

    def _dispatch_output_node(
        self, node: PlannedNode, src_entry: Optional[PooledTexture],
        tile: Rect, output_entry: PooledTexture,
    ) -> None:
        src_meta = self._meta_of(node, "image")
        params = gp.output_params(node, src_meta)
        tw, th = tile[2] - tile[0], tile[3] - tile[1]
        region = region_dict(tile[0], tile[1], tw, th,
                             write_x=tile[0], write_y=tile[1])
        gp.fill_output_pack(params, src_entry.pack if src_entry else _RECT_ZERO)
        merged = {**region, **params}
        buffer = self._context.create_buffer_from_numpy(
            _pack(OUTPUT_PARAMS_DT, merged))
        src_tex = src_entry.texture if src_entry is not None \
            else output_entry.texture
        self._context.dispatch(
            self._kernel("comp_output"),
            buffers={0: buffer},
            textures={0: src_tex, 1: output_entry.texture},
            threads=(tw, th, 1))

    def _meta_of(self, node: PlannedNode, port: str) -> Optional[FrameMeta]:
        """取节点某输入的元数据（从计划反查）。"""
        src_id = node.inputs.get(port)
        if src_id is None or self._last_plan is None:
            return None
        src_node = self._last_plan.node_index().get(src_id)
        return src_node.meta if src_node is not None else None

    # ── 内部：缓存 ──

    def _blit_cached_tile(self, cached: np.ndarray, tile: Rect,
                          output_entry: PooledTexture) -> None:
        assert self._pool is not None
        tw, th = tile[2] - tile[0], tile[3] - tile[1]
        upload = self._context.create_texture_from_numpy(
            np.ascontiguousarray(cached, dtype=np.float16),
            self._pool.pixel_format, tw, th)
        params = {
            **region_dict(tile[0], tile[1], tw, th,
                          write_x=tile[0], write_y=tile[1]),
            "src_x": 0, "src_y": 0,
        }
        buffer = self._context.create_buffer_from_numpy(
            _pack(COPY_PARAMS_DT, params))
        self._context.dispatch(
            self._kernel("comp_copy_region"),
            buffers={0: buffer},
            textures={0: upload, 1: output_entry.texture},
            threads=(tw, th, 1))
        # 上传纹理只被本条命令引用；GPU 完成后由引用计数回收，不进池。

    def _store_tile_caches(self, stores: List[Tuple[str, Rect]],
                           output_entry: PooledTexture) -> None:
        """提交完成后一次性读回并按 tile 切片入缓存。"""
        raw = self._context.read_texture(
            output_entry.texture, output_entry.width * 4,
            output_entry.height, dtype=np.float16,
            region_width=output_entry.width)
        frame = raw.reshape(output_entry.height, output_entry.width, 4)
        for key, tile in stores:
            self._cache.put(key, frame[tile[1]:tile[3], tile[0]:tile[2]])

    # ── 内部：kernel 编译缓存 ──

    def _kernel(self, name: str):
        return self._context.compile_kernel(
            name, METAL_COMPOSITE_KERNEL_SOURCES[name])


class _finalize_once:
    """ finalize_retire 的幂等回调包装（完成回调在 poll 时执行）。"""

    def __init__(self, pool: MetalTexturePool, entry: PooledTexture) -> None:
        self._pool = pool
        self._entry = entry
        self._done = False

    def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        self._pool.finalize_retire(self._entry)


def _measure_gpu_time(command_buffer) -> Optional[float]:
    """完成回调里读 Metal 的 GPU 区间（与 context 的测量同式）。"""
    start_fn = getattr(command_buffer, "gpuStartTime", None)
    end_fn = getattr(command_buffer, "gpuEndTime", None)
    if not callable(start_fn) or not callable(end_fn):
        return None
    try:
        start = float(start_fn())
        end = float(end_fn())
    except (TypeError, ValueError, RuntimeError):
        return None
    if start <= 0.0 or end < start:
        return None
    return (end - start) * 1000.0


def _output_color_space(out_node: PlannedNode) -> str:
    value = str(out_node.params.get("output_color_space", "same"))
    if value in ("same", "display", "", "linear"):
        return "linear"
    return value


def _output_encodes_srgb(out_node: PlannedNode) -> bool:
    return str(out_node.params.get("output_color_space", "same")) == "srgb"


def _pack(dtype: np.dtype, values: dict) -> np.ndarray:
    """把参数 dict 打包成与 MSL struct 对齐的标量数组。"""
    array = np.zeros(1, dtype=dtype)
    for name in dtype.names or ():
        array[name][0] = values.get(name, 0)
    return array
