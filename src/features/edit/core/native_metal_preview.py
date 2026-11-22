"""Render eligible Edit preview frames into Metal present-slot textures.

Metal migration plan tasks 2.1 / 2.2.  The existing Metal preview computes a
frame into an IOSurface and the GL path then blits that surface into a GL
present slot.  This producer skips the detour: it writes the same kernel into
a texture that belongs to a native present slot and publishes it as a native
``GpuPresentFrame`` for the Metal viewport.

Threading: the producer belongs to the render thread, like
``MetalTimelinePreview``.  Nothing here waits on the GPU.  A frame is encoded
and committed through a :class:`MetalFrameTransactionPool`; when its command
buffer completes (seen in :meth:`poll`) the slot is marked ready and the
frame is published.  The VT source stays alive until that completion.

Leases flow one way and are each returned once:

- the VT frame and the CVMetal import are inputs of the frame transaction and
  are released when the GPU finished reading them;
- the present slot is released by the viewport through :meth:`ack` once the
  frame was shown and replaced (see ``MetalPresentPort``).

If completions stop arriving the transaction pool pauses this producer; the
renderer then takes the GL path for new frames until the GPU drains.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.shared.gpu.metal.frame_transaction import (
    FrameTransactionState,
    MetalFrameTransaction,
    MetalFrameTransactionPool,
)
from src.shared.gpu.metal.present_port import ORIGIN_TOP_LEFT
from src.shared.gpu.present_frame import (
    GpuPresentFrame,
    NativePresentDescriptor,
    OutputSlotPool,
    PresentColorInfo,
    SlotState,
)
from src.shared.media.gpu_frame import GpuPlaneResource

logger = logging.getLogger(__name__)

MTL_PIXEL_FORMAT_BGRA8_UNORM = 80
RESOURCE_FORMAT = "bgra8unorm"

SUBMITTED = "submitted"   # frame is on its way; poll() will publish it
FALLBACK = "fallback"     # nothing was taken: the GL path may render this frame
DROPPED = "dropped"       # the frame is consumed but will not be published

_RETIRED_POOL_TIMEOUT_S = 2.0


@dataclass
class PublishedFrame:
    """A finished frame: the descriptor to hand to the viewport and its task."""

    present: GpuPresentFrame
    task: Any
    gpu_ms: Optional[float]


@dataclass
class FailedFrame:
    task: Any
    error: Optional[str]


class NativePreviewProducer:
    """Owns the native present slots of one render thread."""

    def __init__(
        self,
        preview: Any,
        *,
        pool_size: int = 4,
        max_in_flight: int = 2,
        completion_timeout_s: float = 2.0,
    ) -> None:
        self._preview = preview
        self._context = preview.metal_context
        self._pool_size = max(2, int(pool_size))
        self._pool: Optional[OutputSlotPool] = None
        self._slot_textures: Dict[int, Any] = {}
        self._size: Optional[Tuple[int, int]] = None
        self._retired: List[Tuple[OutputSlotPool, Dict[int, Any], float]] = []
        self._transactions = MetalFrameTransactionPool(
            self._context, max_in_flight=max_in_flight,
            completion_timeout_s=completion_timeout_s,
        )
        self._pending: Dict[int, Tuple[Any, Any]] = {}
        self.stats: Dict[str, int] = {
            "submitted": 0, "published": 0, "failed": 0, "dropped": 0,
            "fallbacks": 0, "pool_full": 0,
        }

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether the next eligible frame can take the native path."""
        return self._pool is not None and not self._transactions.paused

    @property
    def paused_reason(self) -> Optional[str]:
        return self._transactions.pause_reason

    @property
    def in_flight(self) -> int:
        return self._transactions.in_flight

    @property
    def pool_size(self) -> int:
        return self._pool_size

    def stats_snapshot(self) -> dict:
        """Diagnostics: counters plus slot and transaction states."""
        snapshot = dict(self.stats)
        snapshot["transactions"] = self._transactions.stats()
        if self._pool is not None:
            snapshot["slots"] = self._pool.stats()
        snapshot["retired_pools"] = len(self._retired)
        return snapshot

    # ------------------------------------------------------------------
    # 一帧
    # ------------------------------------------------------------------

    def ensure_size(self, size: Tuple[int, int]) -> None:
        """Create the slot pool for ``size``; retire the previous one."""
        size = (int(size[0]), int(size[1]))
        if size == self._size and self._pool is not None:
            return
        if self._pool is not None:
            self._retired.append((self._pool, self._slot_textures, time.monotonic()))
        self._size = size
        self._pool = OutputSlotPool(size=self._pool_size)
        self._slot_textures = {}
        for slot_id in range(self._pool_size):
            texture = self._context.create_texture_2d(
                MTL_PIXEL_FORMAT_BGRA8_UNORM, size[0], size[1], shader_write=True)
            self._slot_textures[slot_id] = texture
            self._pool.register_slot(
                slot_id, texture_glo=0, width=size[0], height=size[1],
                resource=GpuPlaneResource(
                    api="metal", native_handle=texture,
                    resource_format=RESOURCE_FORMAT, width=size[0], height=size[1],
                    ownership="renderer_owned",
                    device_identity=self._context.device_identity,
                ),
            )

    def try_render(
        self, task: Any, size: Tuple[int, int], source: Any,
        lut: Optional[Tuple[str, float]],
    ) -> str:
        """Encode and submit one frame.  ``SUBMITTED`` / ``FALLBACK`` / ``DROPPED``.

        With ``FALLBACK`` nothing of the task was consumed: the caller's GL
        path may render it.  With ``DROPPED`` the VT frame was released and
        the caller must report the frame as failed.
        """
        self.ensure_size(size)
        if self._transactions.paused:
            self.stats["fallbacks"] += 1
            return FALLBACK
        if self._transactions.in_flight >= self._transactions.max_in_flight:
            # Capacity is checked before anything is imported, so a full ring
            # leaves the frame untouched for the GL path.
            self.stats["pool_full"] += 1
            return FALLBACK
        lease = self._pool.acquire_for_writing(
            generation_id=int(getattr(task, "generation_id", 0)),
            frame_index=int(getattr(task, "frame", -1)),
        )
        if lease is None:
            self.stats["pool_full"] += 1
            return FALLBACK
        imported = self._preview.encode_native(
            source, lease.resource.native_handle, size, lut=lut)
        if imported is None:
            lease.abort_writing()
            self.stats["fallbacks"] += 1
            return FALLBACK
        transaction = self._transactions.begin(
            inputs=(imported.release, source.release), payload=task,
        )
        if transaction is None:
            imported.release()
            lease.abort_writing()
            self.stats["fallbacks"] += 1
            return FALLBACK
        if not self._transactions.submit(transaction):
            # The pool released both inputs exactly once.
            lease.abort_writing()
            self.stats["dropped"] += 1
            return DROPPED
        self._pending[transaction.sequence] = (task, lease)
        self.stats["submitted"] += 1
        return SUBMITTED

    # ------------------------------------------------------------------
    # 完成（渲染线程）
    # ------------------------------------------------------------------

    def poll(self) -> Tuple[List[PublishedFrame], List[FailedFrame]]:
        """Publish the frames whose GPU work finished; report the failed ones."""
        published: List[PublishedFrame] = []
        failed: List[FailedFrame] = []
        for transaction in self._transactions.poll():
            entry = self._pending.pop(transaction.sequence, None)
            if entry is None:
                continue
            task, lease = entry
            if transaction.state is FrameTransactionState.COMPLETED:
                try:
                    lease.mark_ready(0)   # native frames carry no GL fence
                except RuntimeError as error:
                    logger.warning("NativePreviewProducer: slot %s not ready: %s", lease.slot_id, error)
                    failed.append(FailedFrame(task, f"slot_not_ready:{error}"))
                    continue
                published.append(PublishedFrame(
                    present=self._make_present(lease, task),
                    task=task,
                    gpu_ms=self._context.last_gpu_time_ms,
                ))
                self.stats["published"] += 1
            else:
                try:
                    lease.abort_writing()
                except RuntimeError:
                    pass
                self.stats["failed"] += 1
                failed.append(FailedFrame(task, transaction.error))
        self._drain_retired()
        return published, failed

    def _make_present(self, lease: Any, task: Any) -> GpuPresentFrame:
        descriptor = NativePresentDescriptor.from_resource(
            lease.resource, origin=ORIGIN_TOP_LEFT,
            color=PresentColorInfo(
                # The kernel already converted limited YUV to display RGB.
                color_range="full", color_matrix="bt709",
                color_primaries="bt709", color_transfer="bt709",
            ),
        )
        return lease.make_present_frame(
            frame=int(task.frame), generation_id=int(task.generation_id),
            extra={
                "gpu_submit_ms": self._context.last_gpu_time_ms,
                "gpu_time_unavailable": self._context.last_gpu_time_ms is None,
                "gpu_query_deferred": False,
                "render_backend": "metal",
                "native_viewport": True,
                "deadline_ns": int(getattr(task, "deadline_ns", 0) or 0),
                "scrub_token": getattr(task, "scrub_request_token", None),
            },
            native=descriptor,
        )

    def ack(self, present: GpuPresentFrame, reason: str = "") -> None:
        """The viewport is done with a published frame: its slot may be reused."""
        key = getattr(present, "lease_key", None)
        slot_id = int(getattr(present, "slot_id", -1))
        if slot_id < 0:
            return
        pools = [self._pool] + [pool for pool, _tex, _at in self._retired]
        for pool in pools:
            if pool is None:
                continue
            if key is not None and key.pool_generation != pool.generation:
                continue
            if pool.ack(slot_id, key):
                break

    def _drain_retired(self) -> None:
        """Drop retired slot textures once no frame of them is out."""
        if not self._retired:
            return
        now = time.monotonic()
        remaining: List[Tuple[OutputSlotPool, Dict[int, Any], float]] = []
        for pool, textures, retired_at in self._retired:
            states = pool.stats().get("states", {})
            busy = states.get("READY", 0) or states.get("IN_VIEWER", 0) or states.get("WRITING", 0)
            if busy and now - retired_at < _RETIRED_POOL_TIMEOUT_S:
                remaining.append((pool, textures, retired_at))
                continue
            if busy:
                # A frame never came back (viewport gone).  The textures are
                # only referenced by this producer now; the port was shut
                # down first, so nothing on the GPU reads them.
                logger.warning(
                    "NativePreviewProducer: retiring pool %s with %d slot(s) still out",
                    pool.generation, busy,
                )
            pool.release_all()
        self._retired = remaining

    def shutdown(self) -> bool:
        """Stop and give everything back.  ``False``: the GPU may still be busy."""
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
