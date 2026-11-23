"""阶段 4.4/4.5 —— 调色帧的 Metal 原生发布（present slot + 帧事务）。

把「工作空间纹理 → 展示码值 → 可呈现描述符」收口为一个渲染线程生产
者（参照 Edit 域 ``native_metal_preview.NativePreviewProducer`` 的租约
纪律，本类为 Color 域实现，不复用其 VT 专属编码路径）：

- **槽位**：RGBA16Float 纹理池（``OutputSlotPool`` + ``GpuPlaneResource``）
  ——float 容器是 HDR/EDR 的前提（BGRA8 会把 >1 的高光剪掉）；
- **展示变换施加点唯一**：``render_frame(encode=...)`` 把槽位纹理交给
  ``MetalColorIO.encode_for_display(target=slot)``——输入边界解码与图
  执行都在工作空间，展示编码只发生在写 present slot 这一步；
- **帧事务**：``MetalFrameTransactionPool``（begin → encode → submit），
  GPU 完成经 ``poll()`` 在渲染线程观察到后才 ``mark_ready`` 并发布
  ``GpuPresentFrame``（native 描述符带 ``PresentColorInfo``——由帧描述
  的 ``display_output_for`` 决定，不另造色彩词表）；
- **租约单向**：槽位由视口经 ``ack`` 归还（present port 的 on_released
  回调）；工作纹理的释放回调作为事务 input 挂账。

线程边界：全部方法只在拥有 Metal 设备/上下文的渲染线程调用（``ack``
除外——present port 的 on_released 在渲染线程 poll 中触发）。无 Qt。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

from src.shared.gpu.metal.frame_transaction import (
    FrameTransactionState,
    MetalFrameTransactionPool,
)
from src.shared.gpu.metal.present_port import ORIGIN_TOP_LEFT
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    OutputSlotPool,
    PresentColorInfo,
)
from src.shared.media.gpu_frame import GpuPlaneResource

logger = logging.getLogger(__name__)

#: RGBA16Float（compute 输出常用；EDR/HDR present 需要 float 容器）
MTL_PIXEL_FORMAT_RGBA16_FLOAT = 115
RESOURCE_FORMAT = "rgba16float"

SUBMITTED = "submitted"     # 已编码提交；poll() 将发布
FALLBACK = "fallback"       # 什么都没消费：调用方走 GL/CPU 路径
DROPPED = "dropped"         # 已消费但不会发布（按失败上报）

_RETIRED_POOL_TIMEOUT_S = 2.0


@dataclass
class PublishedColorFrame:
    """一帧完成的发布结果：present 描述符 + 承载它的事务。"""

    present: GpuPresentFrame
    transaction: Any
    gpu_ms: Optional[float] = None


class ColorFrameMetalPublisher:
    """Color 页 Metal 原生帧生产者（渲染线程独占）。"""

    def __init__(
        self,
        ctx: Any,
        *,
        pool_size: int = 3,
        max_in_flight: int = 2,
        completion_timeout_s: float = 2.0,
    ) -> None:
        self._ctx = ctx
        self._pool_size = max(2, int(pool_size))
        self._pool: Optional[OutputSlotPool] = None
        self._slot_textures: dict = {}
        self._size: Optional[Tuple[int, int]] = None
        self._retired: List[Tuple[OutputSlotPool, dict, float]] = []
        self._transactions = MetalFrameTransactionPool(
            ctx, max_in_flight=max_in_flight,
            completion_timeout_s=completion_timeout_s)
        self._pending: dict = {}
        self.stats: dict = {
            "submitted": 0, "published": 0, "failed": 0, "dropped": 0,
            "fallbacks": 0, "pool_full": 0,
        }

    # ── 状态 ──

    @property
    def enabled(self) -> bool:
        return not self._transactions.paused

    @property
    def paused_reason(self) -> Optional[str]:
        return self._transactions.pause_reason

    def stats_snapshot(self) -> dict:
        snapshot = dict(self.stats)
        snapshot["transactions"] = self._transactions.stats()
        if self._pool is not None:
            snapshot["slots"] = self._pool.stats()
        snapshot["retired_pools"] = len(self._retired)
        return snapshot

    # ── 槽位 ──

    def ensure_size(self, size: Tuple[int, int]) -> None:
        """按尺寸建槽位池；旧池退役（等在外帧全部归还后释放）。"""
        size = (int(size[0]), int(size[1]))
        if size == self._size and self._pool is not None:
            return
        if self._pool is not None:
            self._retired.append(
                (self._pool, self._slot_textures, time.monotonic()))
        self._size = size
        self._pool = OutputSlotPool(size=self._pool_size)
        self._slot_textures = {}
        for slot_id in range(self._pool_size):
            texture = self._ctx.create_texture_2d(
                MTL_PIXEL_FORMAT_RGBA16_FLOAT, size[0], size[1],
                shader_write=True)
            self._slot_textures[slot_id] = texture
            self._pool.register_slot(
                slot_id, texture_glo=0, width=size[0], height=size[1],
                resource=GpuPlaneResource(
                    api="metal", native_handle=texture,
                    resource_format=RESOURCE_FORMAT,
                    width=size[0], height=size[1],
                    ownership="renderer_owned",
                    device_identity=self._ctx.device_identity,
                ))

    # ── 一帧 ──

    def render_frame(
        self,
        size: Tuple[int, int],
        encode: Callable[[Any], None],
        *,
        frame: int,
        generation_id: int,
        color_info: PresentColorInfo,
        working_release: Optional[Callable[[], None]] = None,
        extra: Optional[dict] = None,
    ) -> str:
        """编码并提交一帧：``encode(slot_texture)`` 写展示码值。

        ``encode`` 必须**只编码进当前 command buffer**（dispatch），不做
        提交/等待——提交由帧事务统一完成。返回
        ``SUBMITTED``/``FALLBACK``/``DROPPED``（语义同
        ``NativePreviewProducer._try_submit``）。
        """
        self.ensure_size(size)
        if self._transactions.paused:
            self.stats["fallbacks"] += 1
            return FALLBACK
        if self._transactions.in_flight >= self._transactions.max_in_flight:
            self.stats["pool_full"] += 1
            return FALLBACK
        lease = self._pool.acquire_for_writing(
            generation_id=int(generation_id), frame_index=int(frame))
        if lease is None:
            self.stats["pool_full"] += 1
            return FALLBACK
        transaction = self._transactions.begin(
            inputs=(working_release,) if working_release is not None else ())
        if transaction is None:
            lease.abort_writing()
            self.stats["fallbacks"] += 1
            return FALLBACK
        try:
            encode(lease.resource.native_handle)
        except Exception as exc:  # noqa: BLE001 —— 编码失败整体回退
            logger.warning("ColorFrameMetalPublisher: encode failed: %s", exc)
            self._transactions.abort(transaction, "encode_failed")
            lease.abort_writing()
            self.stats["fallbacks"] += 1
            return FALLBACK
        if not self._transactions.submit(transaction):
            lease.abort_writing()
            self.stats["dropped"] += 1
            return DROPPED
        self._pending[transaction.sequence] = (
            lease, int(frame), int(generation_id), color_info,
            dict(extra or {}))
        self.stats["submitted"] += 1
        return SUBMITTED

    # ── 完成（渲染线程） ──

    def poll(self) -> List[PublishedColorFrame]:
        """发布 GPU 完成的帧（失败帧计 dropped，不发布半帧）。"""
        published: List[PublishedColorFrame] = []
        for transaction in self._transactions.poll():
            entry = self._pending.pop(transaction.sequence, None)
            if entry is None:
                continue
            lease, frame, generation_id, color_info, extra = entry
            if transaction.state is not FrameTransactionState.COMPLETED:
                try:
                    lease.abort_writing()
                except RuntimeError:
                    pass
                self.stats["failed"] += 1
                continue
            try:
                lease.mark_ready(0)     # 原生帧无 GL fence
            except RuntimeError as error:
                logger.warning(
                    "ColorFrameMetalPublisher: slot %s not ready: %s",
                    lease.slot_id, error)
                self.stats["failed"] += 1
                continue
            descriptor = NativePresentDescriptor.from_resource(
                lease.resource, origin=ORIGIN_TOP_LEFT, color=color_info)
            present = lease.make_present_frame(
                frame=frame, generation_id=generation_id,
                extra={**extra, "render_backend": "metal",
                       "native_viewport": True},
                native=descriptor)
            published.append(PublishedColorFrame(
                present=present, transaction=transaction,
                gpu_ms=self._ctx.last_gpu_time_ms))
            self.stats["published"] += 1
        self._drain_retired()
        return published

    def note_display_done(self, transaction: Any) -> None:
        """视口呈现完成（present port on_presented）——事务输出可释放。

        ``transaction`` 即 :class:`PublishedColorFrame`.transaction——发布
        方（视口 presenter）持有 ``PublishedColorFrame``，经闭包回传，不
        把事务对象塞进 frozen 描述符。
        """
        self._transactions.note_display_done(transaction)

    def ack(self, present: GpuPresentFrame, reason: str = "") -> None:
        """视口释放帧（on_released）：槽位归还（新池/退役池都查）。"""
        key = getattr(present, "lease_key", None)
        slot_id = int(getattr(present, "slot_id", -1))
        if slot_id < 0:
            return
        pools = [self._pool] + [pool for pool, _t, _at in self._retired]
        for pool in pools:
            if pool is None:
                continue
            if key is not None and key.pool_generation != pool.generation:
                continue
            if pool.ack(slot_id, key):
                break

    def _drain_retired(self) -> None:
        if not self._retired:
            return
        now = time.monotonic()
        remaining: List[Tuple[OutputSlotPool, dict, float]] = []
        for pool, _textures, retired_at in self._retired:
            states = pool.stats().get("states", {})
            busy = (states.get("READY", 0) or states.get("IN_VIEWER", 0)
                    or states.get("WRITING", 0))
            if busy and now - retired_at < _RETIRED_POOL_TIMEOUT_S:
                remaining.append((pool, _textures, retired_at))
                continue
            if busy:
                logger.warning(
                    "ColorFrameMetalPublisher: retiring pool %s with %d "
                    "slot(s) still out", pool.generation, busy)
            pool.release_all()
        self._retired = remaining

    def shutdown(self) -> bool:
        """排空并归还全部资源；``False``＝GPU 仍忙（稍后重试）。"""
        drained = self._transactions.drain(timeout_s=2.0)
        self.poll()
        if not drained:
            return False
        for pool, _textures, _at in self._retired:
            pool.release_all()
        self._retired = []
        if self._pool is not None:
            self._pool.release_all()
        self._slot_textures = {}
        return True


__all__ = [
    "ColorFrameMetalPublisher",
    "PublishedColorFrame",
    "RESOURCE_FORMAT",
    "SUBMITTED",
    "FALLBACK",
    "DROPPED",
]
