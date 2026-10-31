"""Backend-neutral GPU encode-surface ownership contract.

The existing Windows D3D11 surface provider is intentionally kept separate
because it owns DXGI-specific validation. This module is the shared seam for
Metal/VideoToolbox/Topos: the renderer hands over GPU resources and a lease;
an encoder may submit them only after validating the same device identity.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional, Sequence

from src.shared.media.gpu_frame import (
    GpuDecodedFrame,
    GpuFrameRelease,
    GpuFramePlane,
)

GpuEncodeBackend = Literal["metal", "videotoolbox", "topos_metal"]


@dataclass(frozen=True)
class GpuEncodeSurfaceDescriptor:
    """Immutable metadata passed across the renderer/encoder boundary."""

    backend: GpuEncodeBackend
    pixel_format: str
    width: int
    height: int
    bit_depth: int
    device_identity: str
    planes: Sequence[GpuFramePlane]
    frame_index: int = -1
    pts: int | None = None
    duration_seconds: float | None = None
    color_range: str = "limited"
    color_matrix: str = "bt709"
    color_primaries: str | None = None
    color_transfer: str | None = None
    # Native encoder handle, such as a CVPixelBuffer.  The renderer and
    # contract validator treat it as opaque; only the backend bridge may
    # consume it.  Keeping it beside the Metal planes prevents callers from
    # reconstructing a CPU frame just to recover the encoder object.
    native_handle: Any = None

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("GPU encode surface dimensions must be positive")
        if self.bit_depth not in (8, 10, 12, 16):
            raise ValueError(f"unsupported GPU encode bit depth: {self.bit_depth}")
        if not self.device_identity:
            raise ValueError("GPU encode surface requires device_identity")
        if not self.planes:
            raise ValueError("GPU encode surface requires at least one plane")


class GpuEncodeSurface:
    """A single-use handoff from Metal renderer to a GPU encoder.

    ``submit_callback`` must either enqueue the surface to a native encoder or
    return ``False`` with a diagnostic. It must not read a plane into NumPy.
    The callback owns completion timing; ``release`` is safe and idempotent for
    cancellation/error paths. A raise or a falsy return from the callback
    releases the lease automatically (AUD-33208) — a caller never has to
    release on the rejection path.
    """

    def __init__(
        self,
        descriptor: GpuEncodeSurfaceDescriptor,
        lease: GpuFrameRelease,
        submit_callback: Callable[["GpuEncodeSurface"], Any],
        *,
        source_frame: Optional[GpuDecodedFrame] = None,
    ) -> None:
        self.descriptor = descriptor
        self.lease = lease
        self._submit_callback = submit_callback
        self._source_frame = source_frame
        self._lock = threading.Lock()
        self._submitted = False
        self._released = False

    @property
    def submitted(self) -> bool:
        with self._lock:
            return self._submitted

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def native_handle(self) -> Any:
        """Opaque backend object owned by the surface provider."""
        return self.descriptor.native_handle

    def validate(self, *, backend: str, device_identity: str) -> str:
        if self.descriptor.backend != backend:
            return (
                f"encode_backend_mismatch:{self.descriptor.backend}"
                f"!={backend}"
            )
        if self.descriptor.device_identity != device_identity:
            return (
                f"device_mismatch:surface={self.descriptor.device_identity}"
                f"|encoder={device_identity}"
            )
        for plane in self.descriptor.planes:
            if plane.api != "metal":
                return f"unsupported_surface_plane_api:{plane.api}"
        return ""

    def submit(self) -> Any:
        with self._lock:
            if self._released:
                raise RuntimeError("cannot submit a released GPU encode surface")
            if self._submitted:
                raise RuntimeError("GPU encode surface submitted more than once")
            self._submitted = True
        try:
            result = self._submit_callback(self)
        except Exception:
            # The encoder did not accept ownership. The caller can still retry
            # a documented fallback, but this surface must not leak.
            self.release()
            raise
        if not result:
            # AUD-33208: the callback contract documents a falsy return as
            # "did not accept ownership, with diagnostic" — the lease must not
            # depend on caller discipline. Release here (release is safe and
            # idempotent, so a caller that also releases stays correct) and
            # return the callback's value unchanged.
            self.release()
        return result

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self.lease.release()

    @classmethod
    def from_decoded_frame(
        cls,
        frame: GpuDecodedFrame,
        *,
        backend: GpuEncodeBackend,
        pixel_format: str,
        device_identity: str,
        submit_callback: Callable[["GpuEncodeSurface"], Any],
    ) -> tuple[Optional["GpuEncodeSurface"], str]:
        reason = frame.validate_for_device(
            api="metal", device_identity=device_identity)
        if reason:
            return None, reason
        if not frame.device_identity:
            # The consumer identity is the only known identity at this seam;
            # it is still recorded in the surface descriptor and never copied.
            frame_device = device_identity
        else:
            frame_device = frame.device_identity
        descriptor = GpuEncodeSurfaceDescriptor(
            backend=backend,
            pixel_format=pixel_format,
            width=frame.width,
            height=frame.height,
            bit_depth=frame.bit_depth,
            device_identity=frame_device,
            planes=tuple(frame.planes),
            frame_index=frame.frame_index,
            pts=frame.pts,
            duration_seconds=frame.duration_seconds,
            color_range=frame.color_range,
            color_matrix=frame.color_matrix,
            color_primaries=frame.color_primaries,
            color_transfer=frame.color_transfer,
        )
        return cls(descriptor, frame.release, submit_callback, source_frame=frame), ""
