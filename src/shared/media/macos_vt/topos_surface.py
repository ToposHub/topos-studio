"""macOS shared surfaces for Topos planar decode.

This module owns IOSurface allocation and CPU mapping only.  OpenGL/Metal
texture import remains a renderer-thread operation; the native codec receives
only the temporary mapped plane views through :class:`DecodeSurfaceLease`.
"""

from __future__ import annotations

import ctypes
import logging
import platform
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional

from src.shared.codec import topos_binding
from src.shared.media.frame_path import DecodeSurfaceLease

logger = logging.getLogger(__name__)

MACOS_TOPOS_SURFACE_AVAILABLE = platform.system() == "Darwin"

try:
    if not MACOS_TOPOS_SURFACE_AVAILABLE:
        raise ImportError("not macOS")
    import IOSurface  # type: ignore[import-not-found]
except (ImportError, OSError):  # pragma: no cover - platform dependent
    IOSurface = None  # type: ignore[assignment]
    MACOS_TOPOS_SURFACE_AVAILABLE = False


class ToposSurfaceUnavailable(RuntimeError):
    """Raised when IOSurface is not available on the current platform."""


@dataclass
class _SurfaceSlot:
    surface: Any
    plane_widths: tuple[int, ...]
    plane_heights: tuple[int, ...]
    row_bytes: tuple[int, ...]
    leased: bool = False
    mapped_buffers: list[Any] | None = None


def _aligned_row_bytes(width: int, bytes_per_element: int = 2) -> int:
    raw = int(width) * int(bytes_per_element)
    return ((raw + 63) // 64) * 64


class ToposIOSurfacePool:
    """Bounded IOSurface ring for Topos YUV422/444/GBR planes.

    The pool allocates one IOSurface per in-flight frame.  Each surface has
    explicit planes with uint16 storage and can be mapped to the native C ABI
    without making a NumPy array or a full-resolution CPU copy.  A slot is not
    reusable until its :class:`DecodeSurfaceLease` has been unmapped and fenced.

    ``pixel_format`` follows Topos values: ``0`` is 4:2:2, non-zero is full
    width chroma/GBR.  ``plane_count`` is 3 or 4.
    """

    def __init__(
        self,
        width: int,
        height: int,
        pixel_format: int = 0,
        plane_count: int = 3,
        ring_size: int = 3,
        fence_callback: Optional[Callable[[Any], None]] = None,
    ) -> None:
        if not MACOS_TOPOS_SURFACE_AVAILABLE or IOSurface is None:
            raise ToposSurfaceUnavailable("IOSurface unavailable")
        if width <= 0 or height <= 0:
            raise ValueError("surface geometry must be positive")
        if pixel_format not in (0, 1, 2):
            raise ValueError(f"unsupported Topos pixel_format: {pixel_format}")
        if plane_count not in (3, 4):
            raise ValueError("Topos surface plane_count must be 3 or 4")
        if ring_size < 1:
            raise ValueError("ring_size must be positive")

        self.width = int(width)
        self.height = int(height)
        self.pixel_format = int(pixel_format)
        self.plane_count = int(plane_count)
        self.ring_size = int(ring_size)
        self._fence_callback = fence_callback
        self._lock = threading.Lock()
        self._slots: list[_SurfaceSlot] = []

        chroma_width = self.width if self.pixel_format != 0 else (self.width + 1) // 2
        plane_widths = (self.width, chroma_width, chroma_width)
        if self.plane_count == 4:
            plane_widths += (self.width,)
        plane_heights = (self.height,) * self.plane_count
        requested_row_bytes = tuple(_aligned_row_bytes(w) for w in plane_widths)
        for _ in range(self.ring_size):
            plane_info = [
                {
                    IOSurface.kIOSurfacePlaneWidth: w,
                    IOSurface.kIOSurfacePlaneHeight: h,
                    IOSurface.kIOSurfacePlaneBytesPerElement: 2,
                    IOSurface.kIOSurfacePlaneBytesPerRow: stride,
                }
                for w, h, stride in zip(plane_widths, plane_heights, requested_row_bytes)
            ]
            # AUD-35200：绝不创建 kIOSurfaceIsGlobal 表面。全局表面会向
            # 全系统广播 IOSurface ID（任意进程可 IOSurfaceLookup 命中），
            # 本池只在进程内按对象引用传递/映射，无需全局可见性。
            surface = IOSurface.IOSurfaceCreate({
                IOSurface.kIOSurfaceWidth: self.width,
                IOSurface.kIOSurfaceHeight: self.height,
                IOSurface.kIOSurfacePlaneInfo: plane_info,
            })
            if surface is None:
                self.close()
                raise ToposSurfaceUnavailable("IOSurfaceCreate returned NULL")
            # AUD-35201：请求的 BytesPerRow 只是建议，IOSurface 可能按自身
            # 对齐规则分配更大的行距。必须回查每平面实际值，用实际值建立
            # CPU 映射视图，否则 stride 偏小导致逐行错位/越界。
            actual_row_bytes: list[int] = []
            for plane in range(self.plane_count):
                try:
                    row_bytes = int(
                        IOSurface.IOSurfaceGetBytesPerRowOfPlane(surface, plane)
                    )
                except (TypeError, ValueError) as exc:
                    self.close()
                    raise ToposSurfaceUnavailable(
                        f"IOSurfaceGetBytesPerRowOfPlane({plane}) failed: {exc}"
                    ) from exc
                if row_bytes < requested_row_bytes[plane]:
                    self.close()
                    raise ToposSurfaceUnavailable(
                        "IOSurface plane row bytes below requested stride: "
                        f"plane={plane}, actual={row_bytes}, "
                        f"requested={requested_row_bytes[plane]}"
                    )
                actual_row_bytes.append(row_bytes)
            self._slots.append(_SurfaceSlot(
                surface=surface,
                plane_widths=tuple(plane_widths),
                plane_heights=tuple(plane_heights),
                row_bytes=tuple(actual_row_bytes),
            ))

    def acquire(self) -> Optional[DecodeSurfaceLease]:
        """Reserve one ring slot, or return ``None`` when all are in flight."""
        with self._lock:
            slot = next((item for item in self._slots if not item.leased), None)
            if slot is None:
                return None
            slot.leased = True

        def map_surface() -> Any:
            result = IOSurface.IOSurfaceLock(slot.surface, 0, None)
            status = int(result[0] if isinstance(result, tuple) else result)
            if status != 0:
                raise ToposSurfaceUnavailable(f"IOSurfaceLock failed: {status}")
            try:
                views = (topos_binding._CPlaneView * topos_binding.TC_FRAME_MAX_PLANES)()
                buffers: list[Any] = []
                for plane in range(self.plane_count):
                    height = slot.plane_heights[plane]
                    row_bytes = slot.row_bytes[plane]
                    base = IOSurface.IOSurfaceGetBaseAddressOfPlane(slot.surface, plane)
                    mapped = base.as_buffer(row_bytes * height)
                    address = ctypes.addressof(ctypes.c_char.from_buffer(mapped))
                    views[plane].struct_size = ctypes.sizeof(topos_binding._CPlaneView)
                    views[plane].abi_version = topos_binding.TOPOS_CODEC_ABI_VERSION
                    views[plane].pixels = address
                    views[plane].stride = row_bytes // ctypes.sizeof(ctypes.c_uint16)
                    buffers.append(mapped)
                slot.mapped_buffers = buffers
                return views
            except Exception:
                IOSurface.IOSurfaceUnlock(slot.surface, 0, None)
                raise

        def unmap_surface() -> None:
            # AUD-35204：仅解锁成功时清 mapped_buffers——失败时保留映射
            # 视图与状态一致（memoryview 保活即 CPU 锁仍在），lease 停留
            # MAPPED 可重试 unmap；不再出现"缓冲引用已丢 + 状态机卡死"
            # 的不一致导致槽位与 CPU 锁永久泄漏
            status = IOSurface.IOSurfaceUnlock(slot.surface, 0, None)
            if isinstance(status, tuple):
                status = status[0]
            if int(status) != 0:
                raise ToposSurfaceUnavailable(f"IOSurfaceUnlock failed: {status}")
            slot.mapped_buffers = None

        def release_surface() -> None:
            with self._lock:
                if slot.mapped_buffers is not None:
                    logger.error("ToposIOSurfacePool: release while mapped")
                    return
                slot.leased = False

        def fence_surface() -> None:
            if self._fence_callback is not None:
                self._fence_callback(slot.surface)

        return DecodeSurfaceLease(
            backend="macos_iosurface_topos_planar",
            memory_type=topos_binding.TC_DECODE_MEMORY_HOST_VISIBLE_GPU,
            resource=slot.surface,
            map_callback=map_surface,
            unmap_callback=unmap_surface,
            # A renderer supplies GL/Metal fence insertion at pool creation;
            # a missing callback still leaves the explicit fenced state but
            # does not manufacture an external synchronization primitive.
            fence_callback=fence_surface,
            release_callback=release_surface,
            zero_copy=True,
        )

    def close(self) -> None:
        """Drop pool references; in-flight leases remain caller-owned."""
        with self._lock:
            if any(slot.leased for slot in self._slots):
                raise RuntimeError("cannot close ToposIOSurfacePool with leased slots")
            self._slots.clear()

    def __enter__(self) -> "ToposIOSurfacePool":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
