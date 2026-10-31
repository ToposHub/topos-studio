"""VideoToolbox-compatible GPU encode surface allocation.

This module owns the CVPixelBuffer/CVMetalTextureCache half of the M5 seam.
It deliberately does not pretend to be a VideoToolbox compression session:
the caller must inject a native encoder submitter.  Without that submitter,
``submit`` fails closed and the caller can record a mixed/CPU fallback.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from src.shared.export.gpu_encode_surface import (
    GpuEncodeSurface,
    GpuEncodeSurfaceDescriptor,
)
from src.shared.gpu.metal.context import MetalComputeContext
from src.shared.media.macos_vt.cv_metal_bridge import CVMetalVideoBridge

logger = logging.getLogger(__name__)


class VideoToolboxSurfaceError(RuntimeError):
    """A CVPixelBuffer-backed Metal encode surface cannot be allocated."""


def metal_device_identity(device: Any) -> str:
    """Return a process-stable identity for one PyObjC MTLDevice proxy."""
    try:
        import objc

        pointer = int(objc.pyobjc_id(device))
    except Exception:
        pointer = id(device)
    name = str(device.name()) if callable(getattr(device, "name", None)) else type(device).__name__
    return f"metal:{name}:{pointer:x}"


class VideoToolboxGpuSurfaceProvider:
    """Allocate NV12/P010 IOSurface-backed Metal planes for a VT encoder.

    ``submitter`` receives the fully populated :class:`GpuEncodeSurface` and
    is responsible for the native ``VTCompressionSessionEncodeFrame`` call.
    The surface's ``native_handle`` is the CVPixelBuffer to pass to that ABI.
    """

    def __init__(
        self,
        metal_context: MetalComputeContext,
        submitter: Optional[Callable[[GpuEncodeSurface], Any]] = None,
        *,
        # T-P0-31：默认 8。导出流水线（生产者队列 3 + 渲染中 1 + VT
        # 编码在途若干）在 ring=4 时会间歇性 track 满员 → acquire 失败
        # → CPU 帧回落 → bridge 拒收整次导出。
        ring_size: int = 8,
    ) -> None:
        self._metal_context = metal_context
        self._submitter = submitter
        self._bridge = CVMetalVideoBridge(metal_context, ring_size=ring_size)
        self._device_identity = str(
            getattr(metal_context, "device_identity", "")
            or metal_device_identity(metal_context.device)
        )

    @property
    def device_identity(self) -> str:
        return self._device_identity

    @property
    def bridge(self) -> CVMetalVideoBridge:
        """Expose the bridge for diagnostics and completion-ring integration."""
        return self._bridge

    def acquire(
        self,
        *,
        width: int,
        height: int,
        pixel_format: str = "nv12",
        frame_index: int = -1,
        pts: int | None = None,
        duration_seconds: float | None = None,
        color_range: str = "limited",
        color_matrix: str = "bt709",
    ) -> GpuEncodeSurface:
        """Create one IOSurface-backed surface without CPU pixel staging."""
        import Quartz

        width = int(width)
        height = int(height)
        fmt = str(pixel_format).strip().lower()
        if width <= 0 or height <= 0 or width % 2 or height % 2:
            raise VideoToolboxSurfaceError("VT GPU surface dimensions must be even and positive")
        if fmt not in {"nv12", "p010"}:
            raise VideoToolboxSurfaceError(f"unsupported_videotoolbox_surface_format:{fmt}")

        if fmt == "nv12":
            pixel_type = Quartz.kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
            bit_depth = 8
        else:
            pixel_type = Quartz.kCVPixelFormatType_420YpCbCr10BiPlanarVideoRange
            bit_depth = 10

        status, pixel_buffer = Quartz.CVPixelBufferCreate(
            None,
            width,
            height,
            pixel_type,
            {Quartz.kCVPixelBufferIOSurfacePropertiesKey: {}},
            None,
        )
        if int(status) != 0 or pixel_buffer is None:
            raise VideoToolboxSurfaceError(
                f"cvpixelbuffer_create_failed:{int(status)}"
            )

        imported = self._bridge.import_pixel_buffer(
            pixel_buffer,
            frame_index=frame_index,
            pixel_format=fmt,
            bit_depth=bit_depth,
        )
        if imported is None:
            reason = self._bridge.last_fallback_reason() or "cvmetal_import_failed"
            raise VideoToolboxSurfaceError(reason)
        planes, release = imported
        descriptor = GpuEncodeSurfaceDescriptor(
            backend="videotoolbox",
            pixel_format=fmt,
            width=width,
            height=height,
            bit_depth=bit_depth,
            device_identity=self._device_identity,
            planes=tuple(planes),
            frame_index=frame_index,
            pts=pts,
            duration_seconds=duration_seconds,
            color_range=str(color_range),
            color_matrix=str(color_matrix),
            native_handle=pixel_buffer,
        )

        def submit(surface: GpuEncodeSurface) -> Any:
            if self._submitter is None:
                raise VideoToolboxSurfaceError(
                    "videotoolbox_encoder_submitter_missing"
                )
            return self._submitter(surface)

        return GpuEncodeSurface(descriptor, release, submit)

    def close(self) -> None:
        self._bridge.destroy()
