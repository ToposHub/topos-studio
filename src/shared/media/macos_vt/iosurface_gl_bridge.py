"""
macOS IOSurface -> OpenGL Texture Bridge
将 CVPixelBuffer/IOSurface 桥接为 OpenGL texture planes。

## 职责

- 从 CVPixelBuffer 获取 IOSurface
- 使用 CGLTexImageIOSurface2D 创建 GL texture
- 管理 texture ring buffer
- 返回 GpuFramePlane 列表

## 约束

- 每帧不能创建无上限 texture，必须 ring buffer
- CVPixelBuffer 必须 retain 到 GPU render 完成
- texture target 如果是 rectangle，shader 必须支持
- bridge 失败时返回明确 fallback reason

## 禁止

- Texture.write(video_frame_bytes)
- frame.to_ndarray()
"""

from __future__ import annotations

import logging
import platform
import threading
from dataclasses import dataclass
from typing import Sequence

from src.shared.media.gpu_frame import GpuFramePlane, GpuFrameRelease

logger = logging.getLogger(__name__)

_MACOS_AVAILABLE = platform.system() == 'Darwin'
_IOSURFACE_BRIDGE_IMPLEMENTED = False  # Set to True below if all dependencies loaded

if _MACOS_AVAILABLE:
    try:
        from Quartz.CoreVideo import (
            CVPixelBufferGetIOSurface,
            CVPixelBufferGetWidth,
            CVPixelBufferGetHeight,
            CVPixelBufferGetWidthOfPlane,
            CVPixelBufferGetHeightOfPlane,
            CVPixelBufferGetBytesPerRowOfPlane,
            CVPixelBufferGetPlaneCount,
            CVPixelBufferRetain,
            CVPixelBufferRelease,
        )
        from OpenGL.GL import glGenTextures, glDeleteTextures, glBindTexture

        # ctypes CoreVideo functions for raw pointer access.
        # When CVPixelBuffer comes from ctypes VT callback (raw int pointer),
        # PyObjC's CVPixelBufferGetIOSurface etc. return None/0.
        # We use ctypes versions for raw pointers instead.
        import ctypes as _ctypes
        _cv_lib = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/CoreVideo.framework/CoreVideo'
        )
        _cv_lib.CVPixelBufferGetIOSurface.restype = _ctypes.c_void_p
        _cv_lib.CVPixelBufferGetIOSurface.argtypes = [_ctypes.c_void_p]
        _cv_lib.CVPixelBufferGetWidth.restype = _ctypes.c_int
        _cv_lib.CVPixelBufferGetWidth.argtypes = [_ctypes.c_void_p]
        _cv_lib.CVPixelBufferGetHeight.restype = _ctypes.c_int
        _cv_lib.CVPixelBufferGetHeight.argtypes = [_ctypes.c_void_p]
        _cv_lib.CVPixelBufferGetPlaneCount.restype = _ctypes.c_int
        _cv_lib.CVPixelBufferGetPlaneCount.argtypes = [_ctypes.c_void_p]
        _cv_lib.CVPixelBufferGetWidthOfPlane.restype = _ctypes.c_size_t
        _cv_lib.CVPixelBufferGetWidthOfPlane.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
        ]
        _cv_lib.CVPixelBufferGetHeightOfPlane.restype = _ctypes.c_size_t
        _cv_lib.CVPixelBufferGetHeightOfPlane.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
        ]
        _cv_lib.CVPixelBufferGetBytesPerRowOfPlane.restype = _ctypes.c_size_t
        _cv_lib.CVPixelBufferGetBytesPerRowOfPlane.argtypes = [
            _ctypes.c_void_p,
            _ctypes.c_size_t,
        ]
        _cv_lib.CVPixelBufferRetain.restype = _ctypes.c_void_p
        _cv_lib.CVPixelBufferRetain.argtypes = [_ctypes.c_void_p]
        _cv_lib.CVPixelBufferRelease.restype = None
        _cv_lib.CVPixelBufferRelease.argtypes = [_ctypes.c_void_p]

        def _get_cv_iosurface(pixel_buffer):
            """Get IOSurface from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                ptr = _cv_lib.CVPixelBufferGetIOSurface(_ctypes.c_void_p(pixel_buffer))
                return int(ptr) if ptr else None
            return CVPixelBufferGetIOSurface(pixel_buffer)

        def _get_cv_width(pixel_buffer):
            """Get width from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return _cv_lib.CVPixelBufferGetWidth(_ctypes.c_void_p(pixel_buffer))
            return CVPixelBufferGetWidth(pixel_buffer)

        def _get_cv_height(pixel_buffer):
            """Get height from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return _cv_lib.CVPixelBufferGetHeight(_ctypes.c_void_p(pixel_buffer))
            return CVPixelBufferGetHeight(pixel_buffer)

        def _get_cv_plane_count(pixel_buffer):
            """Get plane count from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return _cv_lib.CVPixelBufferGetPlaneCount(_ctypes.c_void_p(pixel_buffer))
            return CVPixelBufferGetPlaneCount(pixel_buffer)

        def _get_cv_plane_width(pixel_buffer, plane_index: int):
            """Get plane width from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return int(
                    _cv_lib.CVPixelBufferGetWidthOfPlane(
                        _ctypes.c_void_p(pixel_buffer),
                        _ctypes.c_size_t(plane_index),
                    )
                )
            return int(CVPixelBufferGetWidthOfPlane(pixel_buffer, plane_index))

        def _get_cv_plane_height(pixel_buffer, plane_index: int):
            """Get plane height from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return int(
                    _cv_lib.CVPixelBufferGetHeightOfPlane(
                        _ctypes.c_void_p(pixel_buffer),
                        _ctypes.c_size_t(plane_index),
                    )
                )
            return int(CVPixelBufferGetHeightOfPlane(pixel_buffer, plane_index))

        def _get_cv_plane_bytes_per_row(pixel_buffer, plane_index: int):
            """Get plane stride from CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                return int(
                    _cv_lib.CVPixelBufferGetBytesPerRowOfPlane(
                        _ctypes.c_void_p(pixel_buffer),
                        _ctypes.c_size_t(plane_index),
                    )
                )
            return int(CVPixelBufferGetBytesPerRowOfPlane(pixel_buffer, plane_index))

        def _retain_cv_buffer(pixel_buffer) -> None:
            """Retain CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                _cv_lib.CVPixelBufferRetain(_ctypes.c_void_p(pixel_buffer))
            else:
                CVPixelBufferRetain(pixel_buffer)

        def _release_cv_buffer(pixel_buffer) -> None:
            """Release CVPixelBuffer, handling both PyObjC and raw int pointers."""
            if isinstance(pixel_buffer, int):
                _cv_lib.CVPixelBufferRelease(_ctypes.c_void_p(pixel_buffer))
            else:
                CVPixelBufferRelease(pixel_buffer)
        # CGLTexImageIOSurface2D may not be in PyOpenGL's CGL module,
        # load it directly from macOS OpenGL framework via ctypes
        try:
            from OpenGL.CGL import CGLTexImageIOSurface2D as _CGLTexImageIOSurface2D
        except ImportError:
            import ctypes
            _gl_lib = ctypes.cdll.LoadLibrary(
                '/System/Library/Frameworks/OpenGL.framework/OpenGL'
            )
            # Define the ctypes signature for CGLTexImageIOSurface2D
            _gl_lib.CGLTexImageIOSurface2D.restype = ctypes.c_int  # CGLError (0 = kCGLNoError)
            _gl_lib.CGLTexImageIOSurface2D.argtypes = [
                ctypes.c_void_p,  # CGLContextObj
                ctypes.c_int,     # GLenum target
                ctypes.c_int,     # GLint internal_format
                ctypes.c_int,     # GLsizei width
                ctypes.c_int,     # GLsizei height
                ctypes.c_int,     # GLenum format
                ctypes.c_int,     # GLenum type
                ctypes.c_void_p,  # IOSurfaceRef iosurface  (NOT int — comes BEFORE plane)
                ctypes.c_int,     # GLuint plane
            ]
            def _CGLTexImageIOSurface2D(ctx, target, internal_format, width, height,
                                        format, type, iosurface, plane):
                return _gl_lib.CGLTexImageIOSurface2D(
                    ctx, target, internal_format, width, height,
                    format, type, iosurface, plane
                )
        _BRIDGE_AVAILABLE = True
        _IOSURFACE_BRIDGE_IMPLEMENTED = True
        logger.info("IOSurfaceGLBridge: all dependencies loaded, bridge ready")
    except (ImportError, OSError) as e:
        _BRIDGE_AVAILABLE = False
        logger.warning(f"IOSurfaceGLBridge: dependencies not available ({e})")
else:
    _BRIDGE_AVAILABLE = False


def _current_cgl_context():
    """当前线程的 CGL context 指针；无 current context 时返回 None。

    与 bind_pixel_buffer 内的解析顺序一致：优先 PyOpenGL 的
    OpenGL.CGL，不可用时退回 ctypes 直连 OpenGL.framework。
    """
    try:
        from OpenGL.CGL import CGLGetCurrentContext
    except ImportError:
        _gl_lib = _ctypes.cdll.LoadLibrary(
            '/System/Library/Frameworks/OpenGL.framework/OpenGL'
        )
        _gl_lib.CGLGetCurrentContext.restype = _ctypes.c_void_p
        _gl_lib.CGLGetCurrentContext.argtypes = []
        CGLGetCurrentContext = _gl_lib.CGLGetCurrentContext
    return CGLGetCurrentContext() or None


@dataclass(frozen=True)
class IOSurfaceGLTextureLease:
    """One IOSurface texture-ring slot reserved by a decoded frame."""

    target_key: str
    slot_index: int
    textures: tuple[int, ...]


class IOSurfaceGLTextureRingBuffer:
    """
    IOSurface -> GL texture ring buffer。

    避免每帧创建无上限 texture，使用固定大小的 ring buffer。

    Attributes:
        ring_size: ring buffer 大小
        planes_per_frame: 每帧 plane 数量（如 NV12 = 2, P010 = 2）
    """

    def __init__(
        self,
        ring_size: int = 4,
        planes_per_frame: int = 2,
        max_ring_size: int | None = None,
    ) -> None:
        self._ring_size = ring_size
        self._max_ring_size = max(
            ring_size,
            int(max_ring_size) if max_ring_size is not None else ring_size,
        )
        self._planes_per_frame = planes_per_frame
        self._textures: list[list[int]] = []
        self._textures_by_key: dict[str, list[list[int]]] = {}
        self._current_index_by_key: dict[str, int] = {}
        self._leased_indices_by_key: dict[str, set[int]] = {}
        # Effective GL target family per slot. Once CGLTexImageIOSurface2D
        # specifies IOSurface storage, the texture's target is LOCKED by the
        # driver (macOS): re-binding the same id with a different target
        # raises GL_INVALID_OPERATION. Slots that end up bound under another
        # family are quarantined/replaced so a later acquire never hands the
        # bad name out again.
        self._slot_family_by_key: dict[str, dict[int, str]] = {}
        # Texture names retired after a target/context mismatch. Keep them
        # reachable until destroy() so an error path does not leak GL names.
        self._retired_textures: list[int] = []
        self._current_index: int = 0
        self._initialized: bool = False

    def _allocate_texture_ring(self, count: int | None = None) -> list[list[int]]:
        """Allocate texture slots for one GL target family."""
        # 分配前清空残留 GL 错误标记：moderngl 不消费错误标志，渲染线程
        # 混用 moderngl/raw GL 时陈旧错误会跨调用存活，被 PyOpenGL 归因
        # 到下一个 raw GL 调用——glGenTextures 在 n=1 时自身不可能产生
        # GL_INVALID_VALUE，报 1281 几乎必为遗留错误误报（同 hdri.py
        # _alloc_cube_raw 与 bind 路径的既有处理）。
        # 仅在确有 current context 时清错：无 context 的进程（如 mock GL
        # 的单测）发起真实 GL 调用会段错误，CGLGetCurrentContext 无
        # context 时安全返回 NULL。
        if _current_cgl_context():
            from OpenGL.GL import glGetError, GL_NO_ERROR
            while glGetError() != GL_NO_ERROR:
                pass
        texture_ring: list[list[int]] = []
        for _ in range(count if count is not None else self._ring_size):
            frame_textures: list[int] = []
            for _plane in range(self._planes_per_frame):
                tex_ids = glGenTextures(1)
                if isinstance(tex_ids, (list, tuple)):
                    tex_id = tex_ids[0]
                else:
                    tex_id = int(tex_ids)
                frame_textures.append(tex_id)
            texture_ring.append(frame_textures)
        return texture_ring

    def initialize(self, gl_context: object | None = None) -> bool:
        """
        预分配 texture ring buffer。

        Args:
            gl_context: OpenGL context（可选）

        Returns:
            True 成功，False 失败
        """
        if self._initialized:
            return True

        try:
            self._textures = self._allocate_texture_ring()
            self._textures_by_key = {'2d': self._textures}
            self._current_index_by_key = {'2d': 0}
            self._leased_indices_by_key = {'2d': set()}
            self._slot_family_by_key = {'2d': {}}

            self._initialized = True
            logger.info(
                f"IOSurfaceGLTextureRingBuffer: initialized "
                f"ring_size={self._ring_size}, "
                f"planes_per_frame={self._planes_per_frame}"
            )
            return True
        except Exception as e:
            logger.error(f"IOSurfaceGLTextureRingBuffer: initialize failed: {e}")
            self._textures = []
            self._textures_by_key = {}
            self._current_index_by_key = {}
            self._leased_indices_by_key = {}
            self._slot_family_by_key = {}
            return False

    def get_next_textures(self) -> list[int]:
        """
        获取下一帧的 texture IDs。

        Returns:
            texture ID 列表（每 plane 一个）
        """
        if not self._initialized or not self._textures:
            raise RuntimeError("IOSurfaceGLTextureRingBuffer not initialized")

        textures = self._textures[self._current_index % self._ring_size]
        self._current_index += 1
        return list(textures)

    def _ensure_family(self, key: str) -> list[list[int]]:
        if key not in self._textures_by_key:
            self._textures_by_key[key] = self._allocate_texture_ring()
            self._current_index_by_key[key] = 0
            self._leased_indices_by_key[key] = set()
            self._slot_family_by_key[key] = {}
        return self._textures_by_key[key]

    def migrate_slot_to_family(
        self, lease: "IOSurfaceGLTextureLease", new_family: str
    ) -> "IOSurfaceGLTextureLease":
        """Reject cross-family texture-name migration.

        A GL texture object is permanently associated with the target used by
        its first bind.  Moving the numeric name between the 2D and rectangle
        rings only changes Python bookkeeping and causes the exact
        ``GL_INVALID_OPERATION`` reported by macOS.  The method remains as a
        compatibility guard for old callers; new code must acquire a fresh
        lease from the desired family instead.
        """
        new_family = new_family or '2d'
        if lease.target_key == new_family:
            return lease
        raise ValueError(
            "cannot migrate an OpenGL texture lease across target families; "
            "acquire a fresh lease instead"
        )

    def get_next_textures_for_target(self, target_key: str) -> list[int]:
        """
        获取指定 GL target 家族的下一组 textures。

        OpenGL texture object 的 target 在第一次 glBindTexture 后基本固定。
        因此 GL_TEXTURE_2D 和 GL_TEXTURE_RECTANGLE 不能复用同一批 id，
        否则 fallback profile 会被 target 类型锁死。
        """
        if not self._initialized:
            raise RuntimeError("IOSurfaceGLTextureRingBuffer not initialized")

        key = target_key or '2d'
        if key not in self._textures_by_key:
            self._textures_by_key[key] = self._allocate_texture_ring()
            self._current_index_by_key[key] = 0
            self._leased_indices_by_key[key] = set()
            self._slot_family_by_key[key] = {}

        ring = self._textures_by_key[key]
        index = self._current_index_by_key.get(key, 0)
        textures = ring[index % len(ring)]
        self._current_index_by_key[key] = index + 1
        if key == '2d':
            self._current_index = index + 1
        return list(textures)

    def acquire_textures_for_target(
        self, target_key: str
    ) -> IOSurfaceGLTextureLease | None:
        """Reserve a slot until the source frame's GPU fence releases it."""
        if not self._initialized:
            raise RuntimeError("IOSurfaceGLTextureRingBuffer not initialized")

        key = target_key or '2d'
        ring = self._ensure_family(key)
        leased = self._leased_indices_by_key[key]
        slot_families = self._slot_family_by_key[key]
        next_index = self._current_index_by_key.get(key, 0)
        for offset in range(len(ring)):
            slot_index = (next_index + offset) % len(ring)
            if slot_index in leased:
                continue
            # Skip slots whose textures were locked to another target family
            # (previously bound successfully via a fallback profile); binding
            # them under this family would raise GL_INVALID_OPERATION.
            if not ring[slot_index]:
                continue
            if slot_families.get(slot_index, key) != key:
                continue
            leased.add(slot_index)
            self._current_index_by_key[key] = slot_index + 1
            if key == '2d':
                self._current_index = slot_index + 1
            return IOSurfaceGLTextureLease(
                target_key=key,
                slot_index=slot_index,
                textures=tuple(ring[slot_index]),
            )

        if len(ring) >= self._max_ring_size:
            return None

        ring.extend(self._allocate_texture_ring(count=1))
        slot_index = len(ring) - 1
        leased.add(slot_index)
        slot_families[slot_index] = key
        self._current_index_by_key[key] = slot_index + 1
        if key == '2d':
            self._current_index = slot_index + 1
        logger.warning(
            "IOSurfaceGLTextureRingBuffer: expanded %s ring to %d slots; "
            "source frames are still awaiting GPU release",
            key,
            len(ring),
        )
        return IOSurfaceGLTextureLease(
            target_key=key,
            slot_index=slot_index,
            textures=tuple(ring[slot_index]),
        )

    def release_texture_lease(self, lease: IOSurfaceGLTextureLease) -> None:
        """Make a slot reusable after its source-frame release fence signals."""
        leased = self._leased_indices_by_key.get(lease.target_key)
        if leased is not None:
            leased.discard(lease.slot_index)

    def invalidate_texture_lease(self, lease: IOSurfaceGLTextureLease) -> None:
        """Quarantine a slot whose GL object rejected its target.

        ``GL_INVALID_OPERATION`` from ``glBindTexture`` means the texture name
        is not usable with the target that the ring selected (usually because
        it was first bound under another target in a shared context).  Do not
        hand that name out again.  We intentionally do not call
        ``glDeleteTextures`` here: this method is also used from error paths
        where the current context may be in the middle of recovering.  The
        old names are retained for explicit cleanup, and the slot is replaced
        with fresh names so a transient mismatch does not exhaust the ring.
        """
        ring = self._textures_by_key.get(lease.target_key)
        if ring is not None and 0 <= lease.slot_index < len(ring):
            old_textures = ring[lease.slot_index]
            self._retired_textures.extend(old_textures)
            try:
                ring[lease.slot_index] = self._allocate_texture_ring(count=1)[0]
                self._slot_family_by_key.setdefault(lease.target_key, {})[
                    lease.slot_index
                ] = lease.target_key
            except Exception:
                ring[lease.slot_index] = []
        self._leased_indices_by_key.get(lease.target_key, set()).discard(
            lease.slot_index
        )

    def destroy(self) -> None:
        """释放所有 texture"""
        if not self._initialized:
            return

        seen: set[int] = set()
        for ring in self._textures_by_key.values():
            for frame_textures in ring:
                for tex_id in frame_textures:
                    if tex_id in seen:
                        continue
                    seen.add(tex_id)
                    try:
                        glDeleteTextures([tex_id])
                    except Exception:
                        pass

        for tex_id in self._retired_textures:
            if tex_id in seen:
                continue
            seen.add(tex_id)
            try:
                glDeleteTextures([tex_id])
            except Exception:
                pass

        if not self._textures_by_key:
            for frame_textures in self._textures:
                for tex_id in frame_textures:
                    if tex_id in seen:
                        continue
                    seen.add(tex_id)
                    try:
                        glDeleteTextures([tex_id])
                    except Exception:
                        pass

        self._textures = []
        self._textures_by_key = {}
        self._current_index_by_key = {}
        self._leased_indices_by_key = {}
        self._slot_family_by_key = {}
        self._retired_textures = []
        self._initialized = False
        self._current_index = 0
        logger.debug("IOSurfaceGLTextureRingBuffer: destroyed")

    def __del__(self) -> None:
        # GL objects must be released explicitly while their owning context is
        # current. A Python finalizer may run on an arbitrary thread.
        return

    @property
    def ring_size(self) -> int:
        return self._ring_size

    @property
    def current_index(self) -> int:
        return self._current_index

    @property
    def max_ring_size(self) -> int:
        """Maximum slots allowed per GL target family."""
        return self._max_ring_size

    def leased_slot_count(self, target_key: str = '2d') -> int:
        """Return source frames retaining slots for a target family."""
        return len(self._leased_indices_by_key.get(target_key or '2d', set()))


class IOSurfaceGLBridge:
    """
    IOSurface -> OpenGL texture 桥接。

    将 CVPixelBuffer 的 IOSurface planes 桥接为 OpenGL textures，
    不经过 CPU 下载。

    Usage:
        bridge = IOSurfaceGLBridge()
        bridge.initialize()
        planes = bridge.bind_pixel_buffer(pixel_buffer)
        if planes:
            # 使用 planes 按 YUV shader 渲染
            pass
        bridge.release_previous_frame()
        bridge.destroy()
    """

    _PLANE_MAP_NV12 = [
        {'component': 'Y', 'internal_format': 'GL_R8', 'format': 'GL_RED', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_RG8', 'format': 'GL_RG', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 1},
    ]
    _PLANE_MAP_P010 = [
        {'component': 'Y', 'internal_format': 'GL_R16UI', 'format': 'GL_RED_INTEGER', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_RG16UI', 'format': 'GL_RG_INTEGER', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 1},
    ]
    _PLANE_MAP_P010_LDR = [
        {'component': 'Y', 'internal_format': 'GL_R16', 'format': 'GL_RED', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_RG16', 'format': 'GL_RG', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 1},
    ]
    _PLANE_MAP_NV12_UNSIZED = [
        {'component': 'Y', 'internal_format': 'GL_RED', 'format': 'GL_RED', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_RG', 'format': 'GL_RG', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 1},
    ]
    _PLANE_MAP_NV12_LEGACY = [
        {'component': 'Y', 'internal_format': 'GL_LUMINANCE', 'format': 'GL_LUMINANCE', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_LUMINANCE_ALPHA', 'format': 'GL_LUMINANCE_ALPHA', 'type': 'GL_UNSIGNED_BYTE', 'plane_index': 1},
    ]
    _PLANE_MAP_P010_UNSIZED = [
        {'component': 'Y', 'internal_format': 'GL_RED', 'format': 'GL_RED', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 0},
        {'component': 'UV', 'internal_format': 'GL_RG', 'format': 'GL_RG', 'type': 'GL_UNSIGNED_SHORT', 'plane_index': 1},
    ]

    def __init__(self, ring_size: int = 4, max_ring_size: int | None = None) -> None:
        self._ring_buffer: IOSurfaceGLTextureRingBuffer | None = None
        self._ring_size = ring_size
        self._max_ring_size = max_ring_size
        self._initialized: bool = False
        self._bind_count: int = 0
        self._fail_count: int = 0
        self._last_fallback_reason: str | None = None
        # GL names are created in one renderer thread/context.  A bridge is
        # not a cross-thread object even when the underlying contexts share
        # resources; make accidental legacy/UI-thread use fail closed.
        self._owner_thread_id: int | None = None
        # Target family that last produced a successful CGLTexImageIOSurface2D
        # bind. On some macOS/driver combinations only GL_TEXTURE_RECTANGLE
        # accepts IOSurface storage; remembering it lets later frames acquire
        # slots directly from that family instead of burning one '2d' slot per
        # frame via failed 2d profiles (which would exhaust the ring after
        # max_ring_size frames).
        self._preferred_target_family: str | None = None

    def initialize(self, planes_per_frame: int = 2) -> bool:
        """
        初始化 bridge 和 ring buffer。

        Args:
            planes_per_frame: 每帧 plane 数量

        Returns:
            True 成功，False 失败
        """
        if self._initialized:
            if self._owner_thread_id == threading.get_ident():
                return True
            self._last_fallback_reason = "bridge_thread_mismatch"
            logger.warning(
                "IOSurfaceGLBridge: initialize requested from a different "
                "thread than its GL owner"
            )
            return False

        self._owner_thread_id = threading.get_ident()

        self._ring_buffer = IOSurfaceGLTextureRingBuffer(
            ring_size=self._ring_size,
            planes_per_frame=planes_per_frame,
            max_ring_size=self._max_ring_size,
        )

        success = self._ring_buffer.initialize()
        if success:
            self._initialized = True
            logger.info(f"IOSurfaceGLBridge: initialized with ring_size={self._ring_size}")
        else:
            logger.error("IOSurfaceGLBridge: failed to initialize ring buffer")
            self._owner_thread_id = None

        return success

    def bind_pixel_buffer(
        self,
        pixel_buffer: object,
        frame_index: int,
        pixel_format: str = 'nv12',
        bit_depth: int = 8,
        decode_path: str = 'macos_vt_iosurface_zero_copy',
    ) -> tuple[Sequence[GpuFramePlane], GpuFrameRelease] | None:
        """
        将 CVPixelBuffer 桥接为 GpuFramePlane 列表。

        不经过 CPU 下载，直接绑定 IOSurface 到 GL texture。

        Args:
            pixel_buffer: CVPixelBufferRef（PyObjC 对象）
            frame_index: 帧号
            pixel_format: 像素格式（nv12, p010）
            bit_depth: 位深度
            decode_path: 解码路径标识

        Returns:
            (GpuFramePlane list, GpuFrameRelease) 或 None（失败时）
        """
        if not self._initialized or self._ring_buffer is None:
            self._last_fallback_reason = "bridge_not_initialized"
            self._fail_count += 1
            return None

        if self._owner_thread_id != threading.get_ident():
            self._last_fallback_reason = "bridge_thread_mismatch"
            self._fail_count += 1
            logger.warning(
                "IOSurfaceGLBridge: refusing bind from a thread other than "
                "the GL owner thread"
            )
            return None

        try:
            result = self._bind_pixel_buffer_impl(
                pixel_buffer, frame_index, pixel_format, bit_depth, decode_path
            )
            if result is not None:
                self._bind_count += 1
            else:
                self._fail_count += 1
            return result
        except Exception as e:
            logger.error(f"IOSurfaceGLBridge: bind_pixel_buffer failed: {e}")
            self._last_fallback_reason = f"bind_error: {e}"
            self._fail_count += 1
            return None

    def _bind_pixel_buffer_impl(
        self,
        pixel_buffer: object,
        frame_index: int,
        pixel_format: str,
        bit_depth: int,
        decode_path: str,
    ) -> tuple[Sequence[GpuFramePlane], GpuFrameRelease] | None:
        """
        实际绑定实现。

        使用 CGLTexImageIOSurface2D 将 IOSurface planes 绑定为 GL textures。

        步骤：
        1. 从 CVPixelBuffer 获取 IOSurface
        2. 获取 plane 信息（宽度、高度、格式）
        3. 使用 CGLTexImageIOSurface2D 绑定每个 plane
        4. 创建 GpuFramePlane 列表
        5. 创建 GpuFrameRelease 回调

        Args:
            pixel_buffer: CVPixelBufferRef
            frame_index: 帧号
            pixel_format: 像素格式
            bit_depth: 位深度
            decode_path: 解码路径

        Returns:
            (GpuFramePlane list, GpuFrameRelease) 或 None
        """
        active_slot_lease: IOSurfaceGLTextureLease | None = None
        selected_slot_lease: IOSurfaceGLTextureLease | None = None
        if not _BRIDGE_AVAILABLE:
            self._last_fallback_reason = "pyobjc_or_pygl_unavailable"
            return None

        try:
            from OpenGL.GL import (
                GL_TEXTURE_2D,
                GL_TEXTURE_RECTANGLE,
                GL_TEXTURE_MIN_FILTER,
                GL_TEXTURE_MAG_FILTER,
                GL_TEXTURE_WRAP_S,
                GL_TEXTURE_WRAP_T,
                GL_CLAMP_TO_EDGE,
                GL_LINEAR,
                glGetError,
                glTexParameteri,
            )
            # Use module-level resolved CGLTexImageIOSurface2D (PyOpenGL or ctypes fallback).
            # Do NOT re-import from OpenGL.CGL here — it may not be available.
            CGLTexImageIOSurface2D = _CGLTexImageIOSurface2D
            try:
                from OpenGL.CGL import CGLGetCurrentContext
            except ImportError:
                # Fallback: get current CGL context via ctypes
                _gl_lib_local = _ctypes.cdll.LoadLibrary(
                    '/System/Library/Frameworks/OpenGL.framework/OpenGL'
                )
                _gl_lib_local.CGLGetCurrentContext.restype = _ctypes.c_void_p
                _gl_lib_local.CGLGetCurrentContext.argtypes = []
                def CGLGetCurrentContext():
                    return _gl_lib_local.CGLGetCurrentContext()

            # Step 1: 获取 IOSurface
            # Use helper that handles both PyObjC objects and raw int pointers
            io_surface = _get_cv_iosurface(pixel_buffer)
            if io_surface is None:
                self._last_fallback_reason = "no_iosurface_backing"
                logger.warning("IOSurfaceGLBridge: pixel buffer has no IOSurface backing")
                return None

            # Step 2: 获取视频尺寸
            width = _get_cv_width(pixel_buffer)
            height = _get_cv_height(pixel_buffer)

            # Step 3: 获取 plane 数量
            plane_count = _get_cv_plane_count(pixel_buffer)
            if plane_count == 0:
                # 非 planar 格式，当作单 plane 处理
                plane_count = 1

            # Step 4: 以 profile 为单位尝试绑定。macOS/CGL 对 IOSurface
            # target/format 的接受范围依驱动而异，不能只固定一种组合。
            # 同一帧内必须使用同一个 target/profile，避免 renderer 的
            # sampler 类型和坐标体系混用。
            binding_profiles = self._get_binding_profiles(
                pixel_format=pixel_format,
                target_2d=GL_TEXTURE_2D,
                target_rectangle=GL_TEXTURE_RECTANGLE,
            )
            # Put the family that previously succeeded first. Without this,
            # machines where only e.g. 'rectangle' accepts IOSurface storage
            # would burn one '2d' slot per frame (failed 2d profiles then
            # migration) and exhaust the ring.
            preferred = self._preferred_target_family
            if preferred:
                binding_profiles = sorted(
                    binding_profiles,
                    key=lambda p: 0 if p['target_name'] == preferred else 1,
                )
            cgl_ctx = CGLGetCurrentContext()
            if not cgl_ctx:
                self._last_fallback_reason = "no_current_cgl_context"
                logger.warning(
                    "IOSurfaceGLBridge: refusing IOSurface import without a "
                    "current CGL context"
                )
                return None
            bind_errors: list[str] = []
            planes: list[GpuFramePlane] = []
            selected_profile: dict | None = None

            for profile in binding_profiles:
                target_name = profile['target_name']
                texture_target = profile['target']
                plane_configs = profile['planes']
                slot_lease = self._ring_buffer.acquire_textures_for_target(target_name)
                if slot_lease is None:
                    bind_errors.append(
                        f"{profile['name']}:texture_ring_exhausted:"
                        f"max={self._ring_buffer.max_ring_size}"
                    )
                    continue
                active_slot_lease = slot_lease
                textures = slot_lease.textures
                candidate_planes: list[GpuFramePlane] = []
                profile_failed = False

                for plane_idx, tex_id in enumerate(textures):
                    if plane_idx >= plane_count:
                        break

                    config = (
                        plane_configs[plane_idx]
                        if plane_idx < len(plane_configs)
                        else plane_configs[0]
                    )
                    plane_width, plane_height, bytes_per_row = self._get_plane_dimensions(
                        pixel_buffer=pixel_buffer,
                        plane_idx=plane_idx,
                        width=width,
                        height=height,
                    )
                    try:
                        # 清除此前 profile 失败（CGL 2D+IOSurface 被驱动拒绝）
                        # 残留的 GL 错误标记：PyOpenGL 会在下一次调用观察到
                        # 陈旧错误并误判当前调用失败（moderngl 不消费错误，
                        # 混用管线里错误会跨调用存活）。
                        glGetError()
                        glBindTexture(texture_target, tex_id)
                        glTexParameteri(texture_target, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
                        glTexParameteri(texture_target, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
                        glTexParameteri(texture_target, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
                        glTexParameteri(texture_target, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
                    except Exception as e:
                        # PyOpenGL raises immediately for GL_INVALID_OPERATION.
                        # Keep this a recoverable profile failure: the old
                        # behaviour escaped to the outer handler, aborting the
                        # whole GPU task before the remaining target profiles
                        # could be tried.  Quarantine the offending slot so a
                        # later frame cannot repeatedly bind the same bad name.
                        bind_errors.append(
                            f"{profile['name']}:plane{plane_idx}:gl_setup:{e}"
                        )

                        logger.warning(
                            "IOSurfaceGLBridge: GL texture setup rejected "
                            "target=%s texture=%s; quarantining ring slot",
                            profile['target_name'],
                            tex_id,
                        )
                        self._ring_buffer.invalidate_texture_lease(slot_lease)
                        profile_failed = True
                        break

                    try:
                        internal_format = self._get_gl_internal_format(
                            config['internal_format'],
                            bit_depth,
                        )
                        gl_format = self._get_gl_format(config['format'])
                        gl_type = self._get_gl_type(config['type'])

                        success = CGLTexImageIOSurface2D(
                            cgl_ctx,
                            texture_target,
                            internal_format,
                            plane_width,
                            plane_height,
                            gl_format,
                            gl_type,
                            io_surface,
                            plane_idx,
                        )
                    except Exception as e:
                        success = -1
                        bind_errors.append(
                            f"{profile['name']}:plane{plane_idx}:exception:{e}"
                        )

                    if success != 0:
                        glGetError()  # 驱动拒绝 IOSurface 存储时可能置错误标记
                        bind_errors.append(
                            f"{profile['name']}:plane{plane_idx}:err{success}:"
                            f"{plane_width}x{plane_height}:stride{bytes_per_row}:"
                            f"{config['internal_format']}/{config['format']}/{config['type']}"
                        )
                        profile_failed = True
                        break

                    candidate_planes.append(
                        GpuFramePlane(
                            component=config['component'],
                            api='opengl',
                            texture_id=tex_id,
                            target=target_name,
                            width=plane_width,
                            height=plane_height,
                            internal_format=config.get('internal_format', ''),
                            pixel_format=f"{pixel_format}_{config['component']}",
                            pixel_type=config.get('type', ''),
                            texture_unit_hint=plane_idx,
                        )
                    )

                if not profile_failed and candidate_planes:
                    planes = candidate_planes
                    selected_profile = profile
                    self._preferred_target_family = profile['target_name']
                    # The textures are now permanently locked to this
                    # profile's target family. The lease was acquired from
                    # that same family, so keep it there; texture names must
                    # never be copied between 2D and rectangle rings.
                    selected_slot_lease = slot_lease
                    active_slot_lease = None
                    break

                self._ring_buffer.release_texture_lease(slot_lease)
                active_slot_lease = None

            if not planes or selected_profile is None:
                tail = "; ".join(bind_errors[-8:]) if bind_errors else "no_candidates"
                self._last_fallback_reason = f"cgl_tex_image_iosurface_failed:{tail}"
                logger.warning("IOSurfaceGLBridge: all CGL bind profiles failed: %s", tail)
                return None

            # Step 5: 创建 GpuFrameRelease 回调
            # Retain the specific pixel_buffer for this frame, and release
            # exactly this buffer when the frame is done.  This avoids the
            # old bridge-wide _previous_pixel_buffer which could release the
            # wrong buffer when multiple frames are in flight.
            _retain_cv_buffer(pixel_buffer)
            _pb_to_release = pixel_buffer  # capture for closure

            ring_buffer = self._ring_buffer

            def _make_release_cb(pb, lease: IOSurfaceGLTextureLease) -> None:
                def _release_cb() -> None:
                    """渲染完成后释放 this frame's CVPixelBuffer"""
                    try:
                        _release_cv_buffer(pb)
                    finally:
                        ring_buffer.release_texture_lease(lease)
                return _release_cb

            gpu_frame_release = GpuFrameRelease(
                retain_token=pixel_buffer,
                release_callback=_make_release_cb(_pb_to_release, selected_slot_lease),
            )

            logger.debug(
                f"IOSurfaceGLBridge: successfully bound {len(planes)} planes "
                f"for frame {frame_index}, format={pixel_format}, "
                f"profile={selected_profile['name']}"
            )

            return (planes, gpu_frame_release)

        except Exception as e:
            if active_slot_lease is not None and self._ring_buffer is not None:
                self._ring_buffer.release_texture_lease(active_slot_lease)
            if selected_slot_lease is not None and self._ring_buffer is not None:
                self._ring_buffer.release_texture_lease(selected_slot_lease)
            self._last_fallback_reason = f"bind_impl_error: {e}"
            logger.error(f"IOSurfaceGLBridge: _bind_pixel_buffer_impl failed: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _get_binding_profiles(
        self,
        pixel_format: str,
        target_2d: int,
        target_rectangle: int,
    ) -> list[dict]:
        """Return CGLTexImageIOSurface2D binding profiles in preferred order."""
        normalized = (pixel_format or '').lower()
        if normalized == 'p010':
            modern = self._PLANE_MAP_P010_LDR
            unsized = self._PLANE_MAP_P010_UNSIZED
            # P010 has no useful luminance fallback because normalized legacy
            # two-channel 16-bit sampling is not reliable across drivers.
            candidates = [
                ('p010_2d_r16_rg16', target_2d, '2d', modern),
                ('p010_2d_red_rg', target_2d, '2d', unsized),
                ('p010_rect_r16_rg16', target_rectangle, 'rectangle', modern),
                ('p010_rect_red_rg', target_rectangle, 'rectangle', unsized),
            ]
        else:
            candidates = [
                ('nv12_2d_r8_rg8', target_2d, '2d', self._PLANE_MAP_NV12),
                ('nv12_2d_red_rg', target_2d, '2d', self._PLANE_MAP_NV12_UNSIZED),
                ('nv12_rect_r8_rg8', target_rectangle, 'rectangle', self._PLANE_MAP_NV12),
                ('nv12_rect_red_rg', target_rectangle, 'rectangle', self._PLANE_MAP_NV12_UNSIZED),
                ('nv12_rect_luminance', target_rectangle, 'rectangle', self._PLANE_MAP_NV12_LEGACY),
                ('nv12_2d_luminance', target_2d, '2d', self._PLANE_MAP_NV12_LEGACY),
            ]

        return [
            {
                'name': name,
                'target': target,
                'target_name': target_name,
                'planes': planes,
            }
            for name, target, target_name, planes in candidates
        ]

    def _get_plane_dimensions(
        self,
        pixel_buffer: object,
        plane_idx: int,
        width: int,
        height: int,
    ) -> tuple[int, int, int]:
        """Read real CVPixelBuffer plane dimensions, falling back conservatively."""
        try:
            plane_width = _get_cv_plane_width(pixel_buffer, plane_idx)
            plane_height = _get_cv_plane_height(pixel_buffer, plane_idx)
            bytes_per_row = _get_cv_plane_bytes_per_row(pixel_buffer, plane_idx)
        except Exception:
            plane_width = 0
            plane_height = 0
            bytes_per_row = 0

        if plane_width <= 0:
            plane_width = width if plane_idx == 0 else max(1, width // 2)
        if plane_height <= 0:
            plane_height = height if plane_idx == 0 else max(1, height // 2)

        return plane_width, plane_height, bytes_per_row

    def _get_gl_internal_format(self, format_name: str, bit_depth: int) -> int:
        """将格式名称转换为 GL internal format enum"""
        from OpenGL.GL import (
            GL_R8, GL_RG8, GL_RGBA8,
            GL_R16, GL_RG16, GL_RGBA16,
            GL_R16UI, GL_RG16UI, GL_RGBA16UI,
            GL_R32F, GL_RG32F, GL_RGBA32F,
            GL_RED, GL_RG,
            GL_LUMINANCE, GL_LUMINANCE_ALPHA,
        )

        format_map = {
            'GL_R8': GL_R8,
            'GL_RG8': GL_RG8,
            'GL_RGBA8': GL_RGBA8,
            'GL_R16': GL_R16,
            'GL_RG16': GL_RG16,
            'GL_RGBA16': GL_RGBA16,
            'GL_R16UI': GL_R16UI,
            'GL_RG16UI': GL_RG16UI,
            'GL_RGBA16UI': GL_RGBA16UI,
            'GL_R32F': GL_R32F,
            'GL_RG32F': GL_RG32F,
            'GL_RGBA32F': GL_RGBA32F,
            'GL_RED': GL_RED,
            'GL_RG': GL_RG,
            'GL_LUMINANCE': GL_LUMINANCE,
            'GL_LUMINANCE_ALPHA': GL_LUMINANCE_ALPHA,
        }

        return format_map.get(format_name, GL_R8)

    def _get_gl_format(self, format_name: str) -> int:
        """将格式名称转换为 GL format enum"""
        from OpenGL.GL import (
            GL_RED, GL_RG, GL_RGB, GL_RGBA,
            GL_RED_INTEGER, GL_RG_INTEGER, GL_RGB_INTEGER, GL_RGBA_INTEGER,
            GL_LUMINANCE, GL_LUMINANCE_ALPHA,
        )

        format_map = {
            'GL_RED': GL_RED,
            'GL_RG': GL_RG,
            'GL_RGB': GL_RGB,
            'GL_RGBA': GL_RGBA,
            'GL_RED_INTEGER': GL_RED_INTEGER,
            'GL_RG_INTEGER': GL_RG_INTEGER,
            'GL_RGB_INTEGER': GL_RGB_INTEGER,
            'GL_RGBA_INTEGER': GL_RGBA_INTEGER,
            'GL_LUMINANCE': GL_LUMINANCE,
            'GL_LUMINANCE_ALPHA': GL_LUMINANCE_ALPHA,
        }

        return format_map.get(format_name, GL_RED)

    def _get_gl_type(self, type_name: str) -> int:
        """将类型名称转换为 GL type enum"""
        from OpenGL.GL import (
            GL_UNSIGNED_BYTE, GL_BYTE,
            GL_UNSIGNED_SHORT, GL_SHORT,
            GL_UNSIGNED_INT, GL_INT,
            GL_HALF_FLOAT, GL_FLOAT,
        )

        type_map = {
            'GL_UNSIGNED_BYTE': GL_UNSIGNED_BYTE,
            'GL_BYTE': GL_BYTE,
            'GL_UNSIGNED_SHORT': GL_UNSIGNED_SHORT,
            'GL_SHORT': GL_SHORT,
            'GL_UNSIGNED_INT': GL_UNSIGNED_INT,
            'GL_INT': GL_INT,
            'GL_HALF_FLOAT': GL_HALF_FLOAT,
            'GL_FLOAT': GL_FLOAT,
        }

        return type_map.get(type_name, GL_UNSIGNED_BYTE)

    def release_previous_frame(self) -> None:
        """
        No-op kept for API compatibility.

        Per-frame CVPixelBuffer release is now handled by the per-frame
        release callback created in _bind_pixel_buffer_impl().
        """

    def destroy(self) -> bool:
        """销毁 bridge 和 ring buffer。

        AUD-35120：GL 资源释放必须在属主线程（持有 GL 上下文的线程）
        执行——跨线程调用会在无上下文的线程上执行 glDeleteTextures，
        静默失败并泄漏全部纹理，且 bridge 被误标记为已销毁。与
        bind_pixel_buffer 的 fail-closed 语义一致：非属主线程调用时
        拒绝销毁并返回 False（记录 bridge_thread_mismatch），真正的
        销毁必须由属主线程稍后执行。

        Returns:
            True 表示销毁完成；False 表示因跨线程调用被拒绝
        """
        if (
            self._initialized
            and self._owner_thread_id is not None
            and self._owner_thread_id != threading.get_ident()
        ):
            self._last_fallback_reason = "bridge_thread_mismatch"
            logger.warning(
                "IOSurfaceGLBridge: refusing destroy from a thread other "
                "than the GL owner thread (glDeleteTextures on a "
                "context-less thread would silently leak); destroy must be "
                "called on the owner thread"
            )
            return False
        if self._ring_buffer is not None:
            self._ring_buffer.destroy()
            self._ring_buffer = None
        self._initialized = False
        self._owner_thread_id = None
        logger.info(
            f"IOSurfaceGLBridge: destroyed, "
            f"bind_count={self._bind_count}, "
            f"fail_count={self._fail_count}"
        )
        return True

    def __del__(self) -> None:
        # See IOSurfaceGLTextureRingBuffer.__del__: never issue GL calls from
        # a non-deterministic GC thread.
        return

    @property
    def is_initialized(self) -> bool:
        """是否已初始化"""
        return self._initialized

    @property
    def bind_count(self) -> int:
        """成功绑定次数"""
        return self._bind_count

    @property
    def fail_count(self) -> int:
        """失败次数"""
        return self._fail_count

    @property
    def last_fallback_reason(self) -> str | None:
        """最后一次 fallback 原因"""
        return self._last_fallback_reason

    @property
    def pixel_buffer_retain_count(self) -> int:
        """当前 retain 的 pixel buffer 数量（per-frame release，此值始终为 0）"""
        return 0


def is_iosurface_bridge_implemented() -> bool:
    """返回 IOSurface -> GL texture bridge 是否已实现。"""
    return _IOSURFACE_BRIDGE_IMPLEMENTED
