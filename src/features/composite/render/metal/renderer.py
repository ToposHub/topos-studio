"""MetalCompositeRenderer — 合成图的 Metal compute 执行器（阶段 5.1/5.3）。

职责与边界：

- 入口是序列化合成图（与 ``CompositeGraphRenderService`` 的 CPU/GL 路径
  同一数据源）；预检不通过 → 返回**显式不可用**的
  ``CompositeGpuFrameHandle``（含节点 ID/类型的回退诊断），整图回退由
  调用方决定，绝不跳过未迁移节点继续产出。
- 全帧与分块共用同一执行模型：节点按扩张区域（`roi.input_roi` + halo）
  dispatch，Output 节点只写 tile 中心（裁回中心）。
- 完成与租约语义（阶段 1 契约）：

  1. GPU 完成前不发布：提交后 ``waitUntilCompleted``，然后才构造
     handle（``fence_ns=0`` 表示生产者已等待，内容完整）。
  2. 输入各释放一次：所有中间纹理在提交时 ``retire``，经
     ``MetalSubmissionScheduler`` 的完成回调在资源线程
     ``finalize_retire``（驱动线程只入队）。
  3. 槽位按租约键确认：纹理池以 ``(池代次, 槽号)`` 记账，
     ``handle.release()`` 幂等归还。

- 本阶段为同步发布（服务 API 语义要求内容完整）；异步发布留给视口
  接入（阶段 2/5.6）再评估 fence 方案。
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, Tuple

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
from .texture_pool import MetalTexturePool, PooledTexture
from .tile_cache import MetalTileCache, composite_cache_key, graph_params_digest

logger = logging.getLogger(__name__)

_RECT_ZERO: Rect = (0, 0, 0, 0)


class MetalCompositeRenderer:
    """以 Metal compute 执行已迁移节点组成的合成图。"""

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

    # ── 属性 ──

    @property
    def pool(self) -> Optional[MetalTexturePool]:
        return self._pool

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

    # ── 生命周期 ──

    def _ensure_context(self) -> MetalComputeContext:
        if self._context is None:
            self._context = MetalComputeContext()
        if self._scheduler is None:
            self._scheduler = MetalSubmissionScheduler(self._context)
        if self._pool is None:
            self._pool = MetalTexturePool(self._context)
        return self._context

    def cleanup(self) -> None:
        """释放资源（关闭/切页）。在途工作先排空再销毁。"""
        if self._scheduler is not None:
            self._scheduler.drain()
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
    ) -> CompositeGpuFrameHandle:
        """渲染一帧并发布 GPU 句柄（不通过预检时返回显式回退句柄）。"""
        support = evaluate_metal_support(graph_data)
        self._last_support = support
        if not support.supported:
            reason = support.fallback_summary or "合成图未通过 Metal 预检"
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

    def render_readback(
        self,
        graph_data: dict,
        timeline_frame: int = 0,
        tile_size: Optional[Tuple[int, int]] = None,
        use_cache: bool = False,
    ) -> Optional[np.ndarray]:
        """渲染并读回 ``(h, w, 4)`` float32（对拍/导出路径）。"""
        handle = self.render(graph_data, timeline_frame=timeline_frame,
                             tile_size=tile_size, use_cache=use_cache)
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

    # ── 内部：整帧编排 ──

    def _render_supported(
        self, graph_data: dict, timeline_frame: int, generation: int,
        tile_size: Optional[Tuple[int, int]], use_cache: bool,
        trace_id: str, started: float,
    ) -> CompositeGpuFrameHandle:
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
        output_entry = self._pool.acquire(canvas_w, canvas_h)
        output_entry.pack = (0, 0, canvas_w, canvas_h)
        self._pending.append(output_entry)
        self._published[output_entry.slot] = output_entry

        tiles: List[Rect] = (
            [(0, 0, canvas_w, canvas_h)] if single_pass
            else list(roi.iter_tile_rects(canvas_w, canvas_h, *tile_size)))
        params_digest = graph_params_digest(graph_data)
        cache_stores: List[Tuple[str, Rect]] = []

        try:
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
                    # 缓存写入必须在提交并完成之后（read_texture 的 blit
                    # 独立提交，乱序会读到未写完的纹理）。
                    cache_stores.append((key, tile))

            # 提交：中间纹理（不含输出纹理——它经 handle 租约归还）在
            # 完成回调里各 finalize 一次（驱动线程只入队，poll 在本线程
            # 执行释放）。
            release_callbacks = self._retire_pending(
                exclude=output_entry)
            submission = self._scheduler.submit(
                release_callbacks=release_callbacks)
            if submission is None:
                # 环满/暂停：未提交的编码属本帧，整组丢弃后显式回退。
                self._context.discard_pending()
                self._force_release_pending()
                return make_unavailable_handle(
                    "Metal 提交环已满（back-pressure），本帧回退",
                    generation=generation, frame=timeline_frame,
                    trace_id=trace_id)
            submission.command_buffer.waitUntilCompleted()
            self._last_gpu_ms = self._context.last_gpu_time_ms
            self._scheduler.poll()
        except Exception:
            # 编码失败：丢弃半编码 buffer，已分配纹理立即归还（未提交，
            # GPU 从未接触）。
            self._context.discard_pending()
            self._force_release_pending()
            raise

        if cache_stores:
            self._store_tile_caches(cache_stores, output_entry)

        handle = CompositeGpuFrameHandle(
            frame=timeline_frame,
            generation=generation,
            texture_id=output_entry.slot,
            width=canvas_w,
            height=canvas_h,
            pixel_format="f2",
            color_space=output_cs,
            range="full",
            alpha_premultiplied=bool(out_meta.alpha_premultiplied),
            # Metal 纹理按图像行序（top-left）读回；GL 消费者需翻转。
            is_gl_oriented=False,
            backend="metal",
            device_identity=context.device_identity,
            fence_ns=0,  # 生产者已 waitUntilCompleted（handle 契约的 finish 兜底）
            release_callback=lambda: self._release_output(output_entry),
            available=True,
            trace_id=trace_id,
        )
        logger.debug(
            "MetalCompositeRenderer: frame=%s tiles=%d halo=%d "
            "elapsed=%.1fms", timeline_frame, len(tiles), halo,
            (time.monotonic() - started) * 1000.0)
        return handle

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
            # 提交时已 waitUntilCompleted：无在途 GPU 工作，直接归还
            # （finalize 为幂等兜底，正常路径在完成回调里不包含输出纹理）。
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
