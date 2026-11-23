"""Metal texture readback on request, explicit and playback counted apart (plan 4.6).

A screenshot, a still grab or an explicit pixel comparison must not wait
behind -- or be dropped like -- the readbacks the playing image makes for
scopes and thumbnails.  This queue keeps the two apart:

* ``purpose="explicit"``: a user's screenshot, a grab, a pixel comparison.
  Rare, and the result has to arrive.  When the queue is full an explicit
  request is refused (``request`` returns ``False``) so the caller can
  retry; it never displaces an older request.
* ``purpose="playback"``: scope displays, thumbnails -- readbacks made
  while the image plays.  Frequent, and stale the moment a newer one is
  asked for: when the queue is full the oldest playback request is dropped.

The counters ``explicit_readbacks`` and ``playback_readbacks`` (and the
``dropped_*`` ones) tell the two apart, which is what plan 4.6 asks to be
able to count.

Nothing waits on the GPU.  ``drain()`` copies every queued texture into a
shared buffer with a small compute kernel, each on a command buffer of the
queue's own command queue, and registers a completion handler; the handler
(a driver thread) only notes the completion, and ``poll()`` on the render
thread reads the buffer and delivers.  The queue never touches the command
buffer the owner is encoding into, so a readback cannot commit half a frame.

Threading: ``request`` and ``cancel_all`` from any thread (they only queue);
``drain`` and ``poll`` on the thread that owns the Metal context.  No Qt.

The texture must hold its final content when ``drain()`` encodes the copy:
ask for a frame after its own GPU work completed (the producer's completion
event), as Metal does not order work across command queues.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.shared.gpu.metal.context import MetalComputeContext, MetalKernel

logger = logging.getLogger(__name__)

PURPOSE_EXPLICIT = "explicit"
PURPOSE_PLAYBACK = "playback"
PURPOSES = (PURPOSE_EXPLICIT, PURPOSE_PLAYBACK)

#: One RGBA texel per pixel, read as float and stored as the request's dtype.
_READBACK_MSL = """
#include <metal_stdlib>
using namespace metal;

kernel void topos_readback_f16(
    texture2d<float> source [[texture(0)]],
    device half4 *out [[buffer(0)]],
    uint2 p [[thread_position_in_grid]]) {
    if (p.x >= source.get_width() || p.y >= source.get_height()) return;
    out[p.y * source.get_width() + p.x] = half4(source.read(p));
}

kernel void topos_readback_u8(
    texture2d<float> source [[texture(0)]],
    device uchar4 *out [[buffer(0)]],
    uint2 p [[thread_position_in_grid]]) {
    if (p.x >= source.get_width() || p.y >= source.get_height()) return;
    out[p.y * source.get_width() + p.x] = uchar4(round(saturate(source.read(p)) * 255.0));
}
"""

#: numpy dtype -> the name of the kernel that stores a texel that way
_KERNELS = {
    np.dtype(np.float16): "topos_readback_f16",
    np.dtype(np.uint8): "topos_readback_u8",
}


def is_metal_texture(value: Any) -> bool:
    """Whether ``value`` can be handed to Metal as a texture.

    Checked before anything is queued: Metal does not report an object that
    is not a texture, the process crashes.
    """
    import objc

    conforms = getattr(value, "conformsToProtocol_", None)
    if not callable(conforms):
        return False
    return bool(conforms(objc.protocolNamed("MTLTexture")))


@dataclass
class ReadbackRequest:
    """One texture on its way to the CPU.

    ``token`` is the caller's own identity for the request (frame number,
    revision, ...); it is handed back unchanged with the pixels so a late
    result can be checked against the state it was asked for.
    """

    texture: Any = field(repr=False)
    width: int
    height: int
    purpose: str
    dtype: Any = np.float16
    token: Any = None
    on_ready: Optional[Callable[[Any, np.ndarray], None]] = field(default=None, repr=False)
    submitted: bool = False
    done: bool = False


class MetalReadbackQueue:
    """Metal texture -> CPU, on request; explicit and playback counted apart."""

    def __init__(self, ctx: MetalComputeContext, *, max_queued: int = 8) -> None:
        self._ctx = ctx
        self._lock = threading.Lock()
        self._queue: List[ReadbackRequest] = []
        self._in_flight: List[Tuple[ReadbackRequest, Any]] = []
        self._completed: List[Tuple[ReadbackRequest, Any, bool]] = []
        self._max_queued = max(int(max_queued), 1)
        self._kernels: Dict[str, MetalKernel] = {}
        self._command_queue: Any = None
        self.stats: Dict[str, int] = {
            "requested_explicit": 0,
            "requested_playback": 0,
            "explicit_readbacks": 0,       # explicit requests delivered
            "playback_readbacks": 0,       # playback requests delivered
            "dropped_playback": 0,         # displaced by a newer one, or cancelled
            "dropped_explicit": 0,         # cancelled
            "failed": 0,
        }

    # -- queueing (any thread; nothing is encoded here) --------------------

    def request(
        self,
        texture: Any,
        size: Tuple[int, int],
        *,
        purpose: str,
        token: Any = None,
        dtype: Any = np.float16,
        on_ready: Optional[Callable[[Any, np.ndarray], None]] = None,
    ) -> bool:
        """Register one readback; ``False`` when the queue is full.

        ``dtype`` is what the pixels come back as: ``float16`` (the default;
        the exact texels of a float texture) or ``uint8`` (display values).
        """
        if purpose not in PURPOSES:
            raise ValueError(
                f"unknown readback purpose {purpose!r} (one of {', '.join(PURPOSES)})")
        dtype = np.dtype(dtype)
        if dtype not in _KERNELS:
            raise ValueError(f"unsupported readback dtype {dtype!r} (float16 or uint8)")
        width, height = int(size[0]), int(size[1])
        if width <= 0 or height <= 0:
            raise ValueError(f"invalid readback size {size!r}")
        if not is_metal_texture(texture):
            raise TypeError(f"not a Metal texture: {type(texture).__name__}")
        entry = ReadbackRequest(
            texture=texture, width=width, height=height, purpose=purpose,
            dtype=dtype, token=token, on_ready=on_ready)
        with self._lock:
            if len(self._queue) >= self._max_queued:
                if purpose == PURPOSE_PLAYBACK:
                    # The oldest playback readback is stale by definition.
                    for index, pending in enumerate(self._queue):
                        if pending.purpose == PURPOSE_PLAYBACK:
                            self._queue.pop(index)
                            self.stats["dropped_playback"] += 1
                            break
                    else:
                        return False            # only explicit requests wait
                else:
                    return False                 # a screenshot is never displaced
            self._queue.append(entry)
            self.stats[f"requested_{purpose}"] += 1
        return True

    def cancel_all(self, reason: str = "") -> Dict[str, int]:
        """Drop every request not yet delivered (page switch, cache clear,
        undo, another LUT).

        A copy the GPU has already started cannot be recalled; when its
        completion arrives the result is dropped rather than delivered, so
        pixels of the previous state never reach the new one.  Returns how
        many requests of each purpose were dropped.
        """
        dropped = {PURPOSE_EXPLICIT: 0, PURPOSE_PLAYBACK: 0}
        with self._lock:
            for pending in self._queue:
                dropped[pending.purpose] += 1
            self._queue.clear()
            # An entry stays listed until poll() takes it, so count each once.
            for entry, _buffer in self._in_flight:
                if not entry.done:
                    entry.done = True
                    dropped[entry.purpose] += 1
            for entry, _buffer, _failed in self._completed:
                if not entry.done:
                    entry.done = True
                    dropped[entry.purpose] += 1
            self.stats["dropped_explicit"] += dropped[PURPOSE_EXPLICIT]
            self.stats["dropped_playback"] += dropped[PURPOSE_PLAYBACK]
        if reason and (dropped[PURPOSE_EXPLICIT] or dropped[PURPOSE_PLAYBACK]):
            logger.debug("MetalReadbackQueue: cancelled %s (%s)", dropped, reason)
        return dropped

    # -- encoding and completion (the thread that owns the context) --------

    def _prepare(self, dtype: Any) -> MetalKernel:
        """The copy kernel for ``dtype``; the command queue is made once."""
        name = _KERNELS[np.dtype(dtype)]
        kernel = self._kernels.get(name)
        if kernel is None:
            kernel = self._ctx.compile_kernel(name, _READBACK_MSL)
            self._kernels[name] = kernel
        if self._command_queue is None:
            self._command_queue = self._ctx.device.newCommandQueue()
            if self._command_queue is None:
                raise RuntimeError("MetalReadbackQueue: the device gave no command queue")
        return kernel

    def drain(self) -> int:
        """Encode and commit every queued readback without waiting.

        Returns how many were committed.  An entry that cannot be encoded is
        counted as failed and dropped; the others are not affected.
        """
        with self._lock:
            batch = self._queue
            self._queue = []
        committed = 0
        for entry in batch:
            if entry.done:
                continue
            try:
                self._commit(entry)
                committed += 1
            except Exception as error:  # noqa: BLE001 - one bad request must not stop the rest
                with self._lock:
                    self.stats["failed"] += 1
                logger.warning("MetalReadbackQueue: readback of %dx%d not submitted: %s",
                               entry.width, entry.height, error)
        return committed

    def _commit(self, entry: ReadbackRequest) -> None:
        import Metal

        kernel = self._prepare(entry.dtype)
        texel = 2 if np.dtype(entry.dtype) == np.float16 else 1
        buffer = self._ctx.create_buffer(entry.width * entry.height * 4 * texel)
        command_buffer = self._command_queue.commandBuffer()
        encoder = command_buffer.computeCommandEncoder()
        encoder.setComputePipelineState_(kernel.pipeline_state)
        encoder.setTexture_atIndex_(entry.texture, 0)
        encoder.setBuffer_offset_atIndex_(buffer, 0, 0)
        # The group size MetalComputeContext.dispatch uses.
        limit = kernel.max_threads_per_group
        gx = max(1, min(16, limit // 16))
        group = (gx, max(1, limit // gx), 1)
        encoder.dispatchThreads_threadsPerThreadgroup_(
            Metal.MTLSizeMake(entry.width, entry.height, 1), Metal.MTLSizeMake(*group))
        encoder.endEncoding()
        command_buffer.addCompletedHandler_(self._completion(entry, buffer))
        command_buffer.commit()
        entry.submitted = True
        with self._lock:
            self._in_flight.append((entry, buffer))

    def _completion(self, entry: ReadbackRequest, buffer: Any) -> Callable[[Any], None]:
        def completed(command_buffer: Any) -> None:
            # A driver thread: note the completion and touch nothing else.
            try:
                failed = command_buffer.error() is not None
            except Exception:  # noqa: BLE001 - report rather than raise on a driver thread
                failed = True
            with self._lock:
                self._completed.append((entry, buffer, failed))
        return completed

    def poll(self) -> List[Tuple[Any, np.ndarray]]:
        """Deliver the readbacks whose copy completed: ``[(token, pixels)]``.

        Pixels are ``(height, width, 4)`` of the request's dtype.  A request
        cancelled before its completion arrived is dropped here.
        """
        with self._lock:
            finished = self._completed
            self._completed = []
            for entry, _buffer, _failed in finished:
                self._in_flight = [(e, b) for e, b in self._in_flight if e is not entry]
        delivered: List[Tuple[Any, np.ndarray]] = []
        for entry, buffer, failed in finished:
            if failed:
                with self._lock:
                    self.stats["failed"] += 1
                continue
            if entry.done:
                continue                    # cancelled while the GPU copied it
            raw = self._ctx.read_buffer(
                buffer, entry.width * entry.height * 4 * np.dtype(entry.dtype).itemsize)
            pixels = np.frombuffer(raw, dtype=np.dtype(entry.dtype)).reshape(
                entry.height, entry.width, 4).copy()
            with self._lock:
                self.stats[f"{entry.purpose}_readbacks"] += 1
            if entry.on_ready is not None:
                try:
                    entry.on_ready(entry.token, pixels)
                except Exception:  # noqa: BLE001 - a caller's callback must not stop delivery
                    logger.exception("MetalReadbackQueue: on_ready callback failed")
            delivered.append((entry.token, pixels))
        return delivered

    # -- queries ------------------------------------------------------------

    @property
    def pending(self) -> int:
        """Requests queued, copying, or copied but not yet polled."""
        with self._lock:
            return len(self._queue) + len(self._in_flight)

    def stats_snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self.stats)

    def wait_all(self, timeout_s: float = 5.0) -> Optional[List[Tuple[Any, np.ndarray]]]:
        """Drain, then poll until nothing is pending: what was delivered.

        For verification and shutdown; the playing image never waits here.
        ``None`` when something is still pending after ``timeout_s``.
        """
        deadline = time.monotonic() + float(timeout_s)
        delivered: List[Tuple[Any, np.ndarray]] = []
        while True:
            self.drain()
            delivered.extend(self.poll())
            if self.pending == 0:
                return delivered
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.002)


__all__ = [
    "MetalReadbackQueue",
    "PURPOSE_EXPLICIT",
    "is_metal_texture",
    "PURPOSE_PLAYBACK",
    "PURPOSES",
    "ReadbackRequest",
]
