"""CompositeGpuFrameHandle — P0-5 ownership contract for composite frames.

A handle is NOT a bare GL texture name. It is the full transfer-of-
ownership + synchronisation record that lets another context (Edit
timeline GPU compositor, encoder surface chain) consume a composite
frame safely:

- identity    : backend/device, generation, frame, timestamp
- pixels      : texture id + dimensions + pixel format/bit depth
- colour      : scene/display colour space, range, alpha semantics
- sync        : producer fence (opaque GLsync int, 0 = finish-fallback)
- ownership   : lease with a release callback; producer MUST NOT recycle
                the texture before the consumer acknowledges (lease==0)

Rules enforced by contract (plan §六 P0-5):

- A handle whose generation no longer matches the consumer's timeline
  generation must never be presented (``is_presentable``).
- A handle produced after a context rebuild / device loss carries
  ``available=False`` plus an explicit ``fallback_reason``; consumers go
  through the copy or CPU fallback instead of presenting a dead texture
  id.
- ``release()`` is idempotent and thread-safe; the underlying producer
  resource returns to its pool exactly once.

This module does not depend on Qt or moderngl — the fence is an opaque
int (GLsync pointer) so tests can drive the state machine with mocks.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CompositeGpuFrameHandle:
    """Ownership + sync record for one composite GPU frame.

    Lifecycle / release responsibility (plan §六 P0-5):

    1. The **producer** (composite render thread) fills the identity /
       pixels / colour / fence fields and hands the handle to a consumer;
       from that moment it MUST NOT recycle or delete the backing texture
       until the lease is acknowledged.
    2. The **consumer** (timeline compositor, encoder surface chain, tests)
       is responsible for calling :meth:`release` exactly once when it is
       done presenting or copying — dropping the last reference without
       ``release()`` leaks the producer's slot.
    3. ``release()`` invokes ``release_callback`` at most once, from
       whichever thread calls it first (any thread is allowed), outside
       the internal lock; callback exceptions are swallowed so a broken
       consumer cannot take down the producer.  The underlying resource
       returns to the producer pool exactly once (idempotent).
    """

    # ── identity ──
    frame: int = 0
    generation: int = 0
    timestamp_ns: int = 0

    # ── pixels ──
    texture_id: int = 0
    width: int = 0
    height: int = 0
    # Pipeline texture format key ('f2' = RGBA16F, 'f4' = RGBA32F,
    # 'f1' = RGBA8) — matches moderngl dtype vocabulary.
    pixel_format: str = "f2"

    # ── colour / alpha semantics ──
    color_space: str = "linear"       # scene-linear working space
    range: str = "full"
    alpha_premultiplied: bool = True
    is_gl_oriented: bool = True       # GL FBO orientation (y=0 bottom)

    # ── backend / device identity ──
    backend: str = "opengl"
    device_identity: str = ""         # GL renderer / adapter LUID

    # ── synchronisation ──
    # Opaque GLsync handle (int). 0 means "producer used finish()" — the
    # consumer may treat the content as complete but must record that the
    # fast path was unavailable.
    fence_ns: int = 0

    # ── ownership ──
    release_callback: Optional[Callable[[], None]] = field(
        default=None, repr=False, compare=False)

    # ── availability / diagnostics ──
    available: bool = True
    fallback_reason: str = ""
    # P0-4: correlation id shared with the producer's frame trace and the
    # consumer's present trace. Empty when produced without tracing.
    trace_id: str = ""

    _released: bool = field(default=False, repr=False, compare=False)
    _lock: threading.Lock = field(default_factory=threading.Lock,
                                  repr=False, compare=False)

    # ── contract helpers ──

    def is_presentable(self, consumer_generation: Optional[int] = None) -> bool:
        """True when the consumer may present this handle.

        A stale generation, an unavailable handle or a zero texture id all
        disqualify presentation.
        """
        if not self.available or self.texture_id <= 0:
            return False
        if consumer_generation is not None \
                and self.generation != consumer_generation:
            return False
        return True

    def has_fence(self) -> bool:
        """True when a real GL fence (not the finish-fallback) is attached."""
        return self.fence_ns != 0

    def release(self) -> None:
        """Return the leased texture to the producer pool (idempotent).

        Consumer-side duty (see class docstring): call once when done.
        Thread-safe — the ``_released`` flip happens under the handle
        lock, and the callback runs outside it exactly once.
        """
        with self._lock:
            if self._released:
                return
            self._released = True
            callback = self.release_callback
        if callback is not None:
            try:
                callback()
            except Exception:  # pragma: no cover - defensive
                pass

    @property
    def released(self) -> bool:
        """True after :meth:`release` has run (lease acknowledged)."""
        return self._released

    def as_dict(self) -> dict:
        """Trace-friendly snapshot (no fence value, no callbacks)."""
        return {
            "frame": self.frame,
            "generation": self.generation,
            "timestamp_ns": self.timestamp_ns,
            "trace_id": self.trace_id,
            "texture_id": self.texture_id,
            "width": self.width,
            "height": self.height,
            "pixel_format": self.pixel_format,
            "color_space": self.color_space,
            "range": self.range,
            "alpha_premultiplied": self.alpha_premultiplied,
            "is_gl_oriented": self.is_gl_oriented,
            "backend": self.backend,
            "device_identity": self.device_identity,
            "has_fence": self.has_fence(),
            "available": self.available,
            "fallback_reason": self.fallback_reason,
            "released": self._released,
        }


def make_unavailable_handle(reason: str,
                            generation: int = 0,
                            frame: int = 0,
                            trace_id: str = "") -> CompositeGpuFrameHandle:
    """Explicit fallback handle: never presentable, always diagnosed."""
    return CompositeGpuFrameHandle(
        frame=frame,
        generation=generation,
        available=False,
        fallback_reason=reason,
        trace_id=trace_id,
    )


# ── Fence helpers ──
# PyOpenGL returns glFenceSync's GLsync pointer as an opaque ctypes pointer
# (int/bytes on some builds). The handle contract stores an opaque int (the
# pointer address); these helpers normalise both directions.

_FENCE_SENTINELS = (None, 0, b"", "")


def fence_to_int(fence) -> int:
    """Normalise a glFenceSync result to the opaque int contract."""
    if fence in _FENCE_SENTINELS:
        return 0
    import ctypes
    if isinstance(fence, ctypes._Pointer):
        return ctypes.cast(fence, ctypes.c_void_p).value or 0
    if isinstance(fence, (bytes, bytearray)):
        return int.from_bytes(fence, "little")
    try:
        return int(fence)
    except (TypeError, ValueError):
        return 0


def _int_to_sync(fence_ns: int):
    """Rebuild the GLsync argument PyOpenGL expects from the int form."""
    import ctypes
    if not fence_ns:
        return 0
    try:
        from OpenGL._opaque import GLsync_pointer
        return ctypes.cast(ctypes.c_void_p(fence_ns), GLsync_pointer)
    except Exception:
        return ctypes.c_void_p(fence_ns)


def wait_fence(fence_ns: int, timeout_ns: int = 0) -> bool:
    """Consumer-side: block until the producer's fence signals.

    ``timeout_ns == 0`` (default) uses ``glWaitSync`` with
    ``GL_TIMEOUT_IGNORED`` — a server-side wait with no CPU stall beyond
    the GPU work itself.  The GL spec *requires* ``glWaitSync``'s timeout
    argument to be ``GL_TIMEOUT_IGNORED``; passing any other value
    generates ``GL_INVALID_VALUE`` and the wait is still unbounded
    (AUD-1207), so a caller asking for a bounded wait must not reach the
    ``glWaitSync`` path.

    ``timeout_ns > 0`` requests a bounded wait: the call switches to
    ``glClientWaitSync``, the only fence wait that honours a timeout.

    Returns False when the fence handle is invalid (0).  With a bounded
    wait the function returns True whether the fence signalled or the
    timeout expired — the return value only reports handle validity
    (unchanged contract).
    """
    if not fence_ns:
        return False
    sync = _int_to_sync(fence_ns)
    if timeout_ns > 0:
        from OpenGL.GL import glClientWaitSync
        glClientWaitSync(sync, 0, int(timeout_ns))
        return True
    from OpenGL.GL import GL_TIMEOUT_IGNORED, glWaitSync
    glWaitSync(sync, 0, GL_TIMEOUT_IGNORED)
    return True


def delete_fence(fence_ns: int) -> None:
    """Producer-side: free the fence once no consumer can wait on it.

    AUD-1209: 删除失败不再被无痕吞掉——``glDeleteSync`` 失败意味着
    GLsync 泄漏，必须留诊断（fence id + 异常）供泄漏排查。失败仍不向
    调用方传播（生产者回收路径不应因诊断性删除失败而中断），但按
    .clinerules 要求只捕具体异常类型并留痕。
    """
    if not fence_ns:
        return
    try:
        # PyOpenGL 异常基类随绑定一起延迟解析——本模块不硬依赖 PyOpenGL
        # （测试以 mock 驱动状态机），Error 覆盖 GLError/NullFunctionError。
        from OpenGL.GL import glDeleteSync
        from OpenGL.error import Error as _OpenGLError
    except ImportError as exc:
        logger.warning(
            "delete_fence(0x%x): OpenGL GL bindings unavailable (%s); "
            "fence not deleted (GLsync leak)", fence_ns, exc)
        return
    try:
        glDeleteSync(_int_to_sync(fence_ns))
    except (_OpenGLError, ctypes.ArgumentError) as exc:
        # _OpenGLError: PyOpenGL 侧失败（GLError / NullFunctionError）；
        # ctypes.ArgumentError: GLsync 指针重建产物非法。
        # 两者都意味着 fence 未被释放（泄漏），必须留痕。
        logger.warning(
            "delete_fence(0x%x): glDeleteSync failed (%r); GLsync leaked",
            fence_ns, exc)
