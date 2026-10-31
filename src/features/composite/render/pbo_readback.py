"""
Async PBO Readback - Non-blocking GPU→CPU pixel transfer.

Uses Pixel Buffer Objects (PBO) to perform asynchronous readback from
FBO textures. This avoids blocking the render thread and allows the
UI thread to receive frames without stalls.

macOS OpenGL 4.1 supports PBO but not compute shaders (4.3).

Architecture:
- 3 PBO ring buffer for pipelined readback
- enqueue(): starts DMA transfer (non-blocking)
- poll_ready(): checks if any transfer completed
- Result includes request_id for UI stale detection

Origin convention:
- Renderer FBO content is OpenGL bottom-left (float32 RGBA).
- After readback + flip, output is CPU top-left BGR float32.
"""

import logging
import numpy as np
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # AUD-8406: 仅为 gl_context 参数注解引入（运行时零依赖）
    import moderngl

logger = logging.getLogger(__name__)

# OpenGL constants for PBO
GL_PIXEL_PACK_BUFFER = 0x88EB
GL_STREAM_READ = 0x88E1
GL_RGBA = 0x1908
GL_FLOAT = 0x1406
GL_UNSIGNED_BYTE = 0x1401
GL_MAP_READ_BIT = 0x0001
GL_READ_ONLY = 0x88B8  # glMapBuffer access (GL_MAP_READ_BIT is for MapBufferRange)
GL_READ_FRAMEBUFFER = 0x8CA8
GL_READ_FRAMEBUFFER_BINDING = 0x8CAA
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_PACK_ALIGNMENT = 0x0D05
GL_ALREADY_SIGNALED = 0x911A
GL_CONDITION_SATISFIED = 0x911B
# T-P1-71：fence 等待的默认有界超时（1s）。GL_TIMEOUT_IGNORED 在 macOS
# 遗留软渲染 GLEngine 上是永久自旋（见 _wait_entry_fence 注释）。
_DEFAULT_FENCE_TIMEOUT_NS = 1_000_000_000

# GL type per moderngl dtype vocabulary
_GL_TYPE_BY_DTYPE = {
    "f4": GL_FLOAT,
    "f2": 0x140B,  # GL_HALF_FLOAT
    "f1": GL_UNSIGNED_BYTE,
}
# numpy dtype per moderngl dtype vocabulary
_NP_DTYPE = {"f4": np.float32, "f2": np.float16, "f1": np.uint8}
# Bytes per channel
_CHANNEL_BYTES = {"f4": 4, "f2": 2, "f1": 1}


@dataclass
class ReadbackResult:
    """Result of an async PBO readback."""
    request_id: int
    data: np.ndarray  # CPU top-left BGR/BGRA float32
    width: int
    height: int


class AsyncPBOReadback:
    """Manages a ring of PBOs for asynchronous GPU→CPU pixel transfer.

    True async transfer (P1-10): ``enqueue`` issues ``glReadPixels`` with a
    PIXEL_PACK_BUFFER bound — the call returns immediately and the DMA runs
    on the GPU — then records a fence. ``poll_ready`` non-blockingly checks
    the fences and maps only the PBOs whose transfer has completed, so the
    consumer takes the latest completed frame without waiting for the
    current one. A full ``read()`` stall happens only if the caller insists
    on a frame whose DMA is still in flight (never inside ``poll_ready``).

    Args:
        gl_context: ModernGL context (any backend with buffer objects).
        ring_size: Number of PBOs in the ring (2 = double buffer).
        dtype: moderngl dtype vocabulary of the source FBO attachment
            ('f4' RGBA32F default; 'f1' for RGBA8/sRGB targets).
        reorder_bgr: Apply the module's RGBA→BGRA reorder after the flip.
            Keep True for raw FBO consumers (matches ``enqueue_sync``'s BGR
            contract). Pass False when the source FBO was produced by the
            ``readback_srgb_bgr`` pass, which already emits BGR order.
    """

    def __init__(self, gl_context: "moderngl.Context", ring_size: int = 3,
                 dtype: str = "f4", reorder_bgr: bool = True) -> None:
        self._ctx = gl_context
        self._ring_size = ring_size
        self._dtype = dtype if dtype in _GL_TYPE_BY_DTYPE else "f4"
        self._reorder_bgr = reorder_bgr
        self._ring: list[_PBOEntry | None] = [None] * ring_size
        self._write_idx = 0
        self._initialized = False
        self._pbos: list[int] = []  # OpenGL buffer IDs

    def initialize(self) -> bool:
        """Create PBO buffer objects. Must be called on the GL thread."""
        if self._initialized:
            return True

        try:
            import ctypes
            try:
                from OpenGL import GL as _gl
            except ImportError:
                logger.warning("[PBO-READBACK] PyOpenGL not available, PBO readback disabled")
                return False

            # PyOpenGL on macOS does not consistently expose glGetCurrentContext.
            # When unavailable, trust the caller: initialize() is invoked from
            # CompositeRenderer after its QOpenGLContext has been made current.
            get_current_context = getattr(_gl, "glGetCurrentContext", None)
            if get_current_context is not None:
                current_ctx = get_current_context()
                if current_ctx == 0 or current_ctx is None:
                    logger.warning("[PBO-READBACK] No GL context current, cannot initialize PBOs")
                    return False

            # Clear any pending GL errors before creating buffers
            while _gl.glGetError() != _gl.GL_NO_ERROR:
                pass

            for i in range(self._ring_size):
                pbo_id = ctypes.c_uint()
                _gl.glGenBuffers(1, ctypes.byref(pbo_id))
                err = _gl.glGetError()
                if err != _gl.GL_NO_ERROR:
                    logger.warning(
                        f"[PBO-READBACK] glGenBuffers failed with GL error {err}, "
                        "PBO readback disabled (GL context may not support buffer objects)"
                    )
                    # Clean up any partially created buffers
                    for pid in self._pbos:
                        try:
                            _gl.glDeleteBuffers(1, ctypes.c_uint(pid))
                        except Exception:
                            pass
                    self._pbos.clear()
                    return False
                self._pbos.append(pbo_id.value)

            self._initialized = True
            logger.info(f"[PBO-READBACK] Initialized with {self._ring_size} PBOs")
            return True
        except Exception as e:
            logger.warning(f"[PBO-READBACK] Initialization failed: {e}")
            return False

    def enqueue(self, fbo, width: int, height: int, request_id: int = 0) -> bool:
        """Start an asynchronous readback from the given FBO.

        Non-blocking: issues ``glReadPixels`` into the ring's next PBO
        (DMA runs on the GPU), records a fence, and returns immediately.
        The FBO must be complete (its draw calls flushed by the driver's
        ordering — no explicit finish needed).

        Args:
            fbo: ModernGL Framebuffer to read from (any dtype this ring
                was constructed for).
            width: Frame width.
            height: Frame height.
            request_id: Request ID for stale detection.

        Returns:
            True if enqueue succeeded.
        """
        if not self._initialized:
            if not self.initialize():
                return False

        try:
            from OpenGL import GL as _gl

            idx = self._write_idx
            # A still-in-flight entry would have its DMA corrupted by
            # reallocating its PBO — wait on it (worst case one frame of
            # GPU work) before recycling the slot.
            prev = self._ring[idx]
            if prev is not None and prev.data is None:
                self._wait_entry_fence(prev)
            # AUD-8403: the entry being overwritten here must not keep its
            # GLsync alive — consumers that stopped collecting (e.g. preview
            # hidden) while the producer keeps enqueueing would otherwise
            # leak one driver fence object per ring rotation. Collected
            # entries already carry fence=None, so this is a no-op for them.
            if prev is not None:
                self._delete_fence(prev.fence)
                prev.fence = None

            gl_type = _GL_TYPE_BY_DTYPE[self._dtype]
            byte_size = width * height * 4 * _CHANNEL_BYTES[self._dtype]

            # Save the read binding so the caller's FBO state is untouched.
            prev_read_fb = _gl.glGetInteger(GL_READ_FRAMEBUFFER_BINDING)

            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, self._pbos[idx])
            _gl.glBufferData(GL_PIXEL_PACK_BUFFER, byte_size, None,
                             GL_STREAM_READ)
            _gl.glPixelStorei(GL_PACK_ALIGNMENT, 1)
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo.glo)
            _gl.glReadBuffer(GL_COLOR_ATTACHMENT0)
            # Last argument 0 = write into the currently bound PACK buffer.
            _gl.glReadPixels(0, 0, width, height, GL_RGBA, gl_type, 0)
            _gl.glBindFramebuffer(GL_READ_FRAMEBUFFER, prev_read_fb)
            _gl.glPixelStorei(GL_PACK_ALIGNMENT, 4)
            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)

            fence = None
            try:
                from OpenGL.GL import GL_SYNC_GPU_COMMANDS_COMPLETE, glFenceSync
                fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0)
            except Exception:
                fence = None

            self._ring[idx] = _PBOEntry(
                request_id=request_id,
                width=width,
                height=height,
                data=None,
                fence=fence,
            )
            self._write_idx = (self._write_idx + 1) % self._ring_size
            return True

        except Exception as e:
            logger.warning(f"[PBO-READBACK] Enqueue failed: {e}")
            return False

    def _wait_entry_fence(self, entry: "_PBOEntry", timeout_ns: int = 0) -> bool:
        """Block (bounded) until the entry's DMA completes.

        T-P1-71（无头 GL 挂死修复）：绝不允许传 GL_TIMEOUT_IGNORED 无限等
        待——macOS 遗留 OpenGL 软渲染器（GLEngine/GLRendererFloat）在
        glClientWaitSync 收到 0xFFFF…FFFF 时会 100% CPU 永久自旋而 fence
        永不判定（采样栈 glClientWaitSync_Exec；同 fence 在 timeout=0 的
        poll 路径正常判定）。默认有界 1 秒；超时返回 False，由调用方决定
        是否把该条目按"未就绪"处理。
        """
        fence = entry.fence
        if fence is None:
            return True
        try:
            from OpenGL.GL import glClientWaitSync
            timeout = _DEFAULT_FENCE_TIMEOUT_NS if timeout_ns <= 0 else timeout_ns
            result = glClientWaitSync(fence, 0, timeout)
            return result in (GL_ALREADY_SIGNALED, GL_CONDITION_SATISFIED)
        except Exception:
            return True

    def _delete_fence(self, fence) -> None:
        if fence is None:
            return
        try:
            from OpenGL.GL import glDeleteSync
            glDeleteSync(fence)
        except Exception:
            pass

    @property
    def pending_count(self) -> int:
        """Entries enqueued whose DMA has not been collected yet."""
        return sum(
            1 for e in self._ring if e is not None and e.data is None)

    def _map_entry(self, entry: "_PBOEntry", pbo_id: int) -> bool:
        """Map a completed PBO into entry.data (CPU top-left BGR)."""
        try:
            import ctypes
            from OpenGL import GL as _gl

            w, h = entry.width, entry.height
            byte_size = w * h * 4 * _CHANNEL_BYTES[self._dtype]
            _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbo_id)
            ptr = _gl.glMapBuffer(GL_PIXEL_PACK_BUFFER, GL_READ_ONLY)
            if not ptr:
                _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)
                return False
            try:
                addr = ctypes.cast(ptr, ctypes.c_void_p).value or 0
                # string_at copies out of the mapped range before unmap.
                raw = np.frombuffer(
                    ctypes.string_at(addr, byte_size), dtype=np.uint8)
            finally:
                _gl.glUnmapBuffer(GL_PIXEL_PACK_BUFFER)
                _gl.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0)

            arr = raw.view(_NP_DTYPE[self._dtype]).reshape(h, w, 4)
            # glReadPixels rows are GL bottom-up; consumers want top-left.
            # RGBA -> BGR(A) reorder matches the sync-path contract (skip
            # when the source already carries BGR order).
            flipped = np.flipud(arr)
            entry.data = flipped[..., [2, 1, 0, 3]].copy() \
                if self._reorder_bgr else flipped.copy()
            return True
        except Exception as e:
            logger.warning(f"[PBO-READBACK] Map failed: {e}")
            return False

    def enqueue_sync(self, fbo, width: int, height: int, request_id: int = 0,
                     preserve_alpha: bool = False) -> bool:
        """Perform a synchronous readback (still on render thread, not UI thread).

        This is the practical implementation: the render thread does the
        readback (which doesn't block the UI thread) and stores the result
        in the ring buffer for the UI thread to pick up via signal.

        Args:
            fbo: ModernGL Framebuffer to read from.
            width: Frame width.
            height: Frame height.
            request_id: Request ID for stale detection.
            preserve_alpha: Keep the readback alpha channel for matte/QC
                consumers.  The default remains BGR for the Viewer path.

        Returns:
            True if readback succeeded.
        """
        try:
            import cv2

            idx = self._write_idx

            # AUD-8404: a synchronous overwrite must not silently drop an
            # in-flight async entry's fence object (conditional risk when a
            # caller mixes enqueue/enqueue_sync on one ring). Delete the
            # superseded entry's fence before reusing the slot; the entry's
            # result is intentionally discarded by this synchronous write.
            prev = self._ring[idx]
            if prev is not None:
                self._delete_fence(prev.fence)
                prev.fence = None

            raw = fbo.read(components=4, dtype='f4')
            arr = np.frombuffer(raw, dtype=np.float32).reshape(height, width, 4)
            # Flip vertically (OpenGL bottom-left origin → CPU top-left)
            # and convert RGB→BGR.  Scope consumers also need the alpha
            # channel, while the Viewer intentionally keeps the historical
            # three-channel contract.
            flipped = np.flipud(arr).copy()
            if preserve_alpha:
                bgr = cv2.cvtColor(flipped, cv2.COLOR_RGBA2BGRA)
            else:
                bgr = cv2.cvtColor(flipped[:, :, :3], cv2.COLOR_RGB2BGR)

            self._ring[idx] = _PBOEntry(
                request_id=request_id,
                width=width,
                height=height,
                data=bgr,  # CPU top-left BGR float32
            )
            self._write_idx = (self._write_idx + 1) % self._ring_size
            return True

        except Exception as e:
            logger.warning(f"[PBO-READBACK] Sync readback failed: {e}")
            return False

    def poll_ready(self) -> list[ReadbackResult]:
        """Return all completed readback results and clear them from the ring.

        Non-blocking: entries whose DMA fence has not signalled stay in the
        ring for a later poll. Entries still pending after a full ring
        rotation are force-waited when their slot must be reused (see
        ``enqueue``).
        """

        results: list[ReadbackResult] = []
        for i in range(self._ring_size):
            entry = self._ring[i]
            if entry is None:
                continue
            if entry.data is not None:
                # Already mapped but not yet collected.
                results.append(ReadbackResult(
                    request_id=entry.request_id,
                    data=entry.data,
                    width=entry.width,
                    height=entry.height,
                ))
                self._ring[i] = None
                continue
            if entry.fence is not None:
                try:
                    from OpenGL.GL import glClientWaitSync
                    status = glClientWaitSync(entry.fence, 0, 0)
                    if status not in (GL_ALREADY_SIGNALED,
                                      GL_CONDITION_SATISFIED):
                        continue  # DMA still in flight — stays pending
                except Exception:
                    pass  # fence unusable — fall through and try the map
            if self._map_entry(entry, self._pbos[i]):
                self._delete_fence(entry.fence)
                entry.fence = None
                results.append(ReadbackResult(
                    request_id=entry.request_id,
                    data=entry.data,
                    width=entry.width,
                    height=entry.height,
                ))
                self._ring[i] = None
        return results

    def get_latest(self, wait_newest: bool = False) -> Optional[ReadbackResult]:
        """Return the most recent completed readback result.

        Discards older results. Only the latest request_id is kept.

        Args:
            wait_newest: When True, block until the NEWEST entry's DMA
                completes (bounded correctness wait for final frames).
                Without it the call never waits — it may return None.
        """
        newest_idx = -1
        for i in range(self._ring_size):
            entry = self._ring[i]
            if entry is None:
                continue
            if newest_idx < 0 or \
                    entry.request_id > self._ring[newest_idx].request_id:
                newest_idx = i
        if newest_idx < 0:
            return None
        newest = self._ring[newest_idx]
        if newest.data is None:
            if wait_newest:
                # T-P1-71 (AUD-8401): a fence timeout must be treated as
                # "not ready" — mapping an in-flight PBO reads unfinished
                # data and can stall legacy drivers. Keep the fence so a
                # later non-blocking poll can still collect this entry.
                if not self._wait_entry_fence(newest):
                    return None
                self._map_entry(newest, self._pbos[newest_idx])
                self._delete_fence(newest.fence)
                newest.fence = None
            else:
                # Never block: only take it if a non-blocking poll maps it.
                # AUD-8402: poll_ready maps every completed entry into its
                # returned list AND clears the ring slots — the sweep below
                # can no longer see them. Deliver the newest of the freshly
                # collected results directly instead of dropping them.
                polled = self.poll_ready()
                if not polled:
                    return None
                return max(polled, key=lambda r: r.request_id)

        best: Optional[ReadbackResult] = None
        for i in range(self._ring_size):
            entry = self._ring[i]
            if entry is not None and entry.data is not None:
                if best is None or entry.request_id > best.request_id:
                    if best is not None:
                        logger.debug(
                            f"[PBO-READBACK] Drop stale readback request_id={best.request_id}"
                        )
                    best = ReadbackResult(
                        request_id=entry.request_id,
                        data=entry.data,
                        width=entry.width,
                        height=entry.height,
                    )
                self._ring[i] = None
        return best

    def cleanup(self) -> None:
        """Release PBO buffer objects and pending fences."""
        for entry in self._ring:
            if entry is not None:
                self._delete_fence(entry.fence)
                entry.fence = None

        if not self._initialized:
            return

        try:
            import ctypes
            from OpenGL import GL as _gl

            for pbo_id in self._pbos:
                _gl.glDeleteBuffers(1, ctypes.c_uint(pbo_id))
            self._pbos.clear()
        except Exception:
            pass

        self._initialized = False
        self._ring = [None] * self._ring_size


@dataclass
class _PBOEntry:
    """Internal ring buffer entry."""
    request_id: int
    width: int
    height: int
    data: np.ndarray | None  # None until readback completes
    fence: Any = None  # GLsync (opaque pointer) guarding the DMA
