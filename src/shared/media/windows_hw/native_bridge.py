"""Optional native Windows GPU bridge contracts.

The Python side deliberately owns the policy and evidence rules while the
native side owns Media Foundation/NVDEC/AMF/oneVPL handles.  Keeping the
boundary as a small, validated protocol lets the application remain
importable on non-Windows hosts and, importantly, prevents a missing native
module from being mistaken for a CPU-backed "GPU" path.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol, Tuple, runtime_checkable

from .reasons import FallbackReason, param

__all__ = [
    "NATIVE_BRIDGE_API_VERSION",
    "NativeDecodedTexture",
    "NativeEncodeSubmitResult",
    "NativeBridgeCapabilities",
    "WindowsNativeBridge",
    "NativeBridgeLoadResult",
    "load_windows_native_bridge",
]

NATIVE_BRIDGE_API_VERSION = 1


@dataclass(frozen=True)
class NativeDecodedTexture:
    """A native decoder's GPU-only frame description.

    ``texture_handle`` is an ``ID3D11Texture2D*`` represented as an integer
    only at the ctypes boundary.  No CPU plane or staging pointer is allowed
    in this object.
    """

    texture_handle: int
    width: int
    height: int
    frame_index: int
    pts_us: Optional[int]
    pixel_format: str
    bit_depth: int
    adapter_luid: str
    color_matrix: str = "bt709"
    color_transfer: str = "bt709"
    color_primaries: str = "bt709"
    video_range: str = "limited"
    duration_us: Optional[int] = None
    source_kind: str = "d3d11va"
    fence: object | None = None
    # Optional Direct3D timestamp-query results.  Missing values are kept as
    # None so the export gate cannot mistake an unmeasured stage for zero.
    gpu_decode_ms: Optional[float] = None
    gpu_render_ms: Optional[float] = None
    gpu_yuv_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.texture_handle:
            raise ValueError("native decode texture handle 不能为空")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("native decode texture 尺寸必须为正")
        if self.pixel_format.lower() not in {"nv12", "p010"}:
            raise ValueError(f"native decode 不支持像素格式: {self.pixel_format}")
        expected_depth = 10 if self.pixel_format.lower() == "p010" else 8
        if self.bit_depth != expected_depth:
            raise ValueError("native decode pixel format 与 bit depth 不一致")
        if not self.adapter_luid:
            raise ValueError("native decode texture 缺少 adapter LUID")
        for name in ("gpu_decode_ms", "gpu_render_ms", "gpu_yuv_ms"):
            value = getattr(self, name)
            if value is not None and value < 0.0:
                raise ValueError(f"{name} 不可为负")


@dataclass(frozen=True)
class NativeEncodeSubmitResult:
    """Native encoder submission result.

    An accepted asynchronous submission must carry a completion fence.  The
    Python lease cannot be returned to the fixed pool before that fence is
    signalled, otherwise the next frame could overwrite an encoder input.
    """

    accepted: bool
    reason: str = ""
    gpu_submit_ms: Optional[float] = None
    completion_fence: object | None = None
    packet_count: int = 0

    def __post_init__(self) -> None:
        if self.accepted and self.completion_fence is None:
            raise ValueError("异步硬编提交缺少 completion_fence")
        if self.gpu_submit_ms is not None and self.gpu_submit_ms < 0.0:
            raise ValueError("gpu_submit_ms 不可为负")


@dataclass(frozen=True)
class NativeBridgeCapabilities:
    """Capabilities declared by a loaded native bridge."""

    api_version: int
    name: str
    version: str
    decode_codecs: Tuple[str, ...]
    encode_kinds: Tuple[str, ...]
    d3d11_renderer: bool

    @property
    def valid(self) -> bool:
        return (
            self.api_version == NATIVE_BRIDGE_API_VERSION
            and bool(self.name)
            and bool(self.decode_codecs)
            and bool(self.encode_kinds)
        )


@runtime_checkable
class WindowsNativeBridge(Protocol):
    """ABI-neutral surface expected from ``topos_hwdecode_windows``.

    Implementations may be a Python test double or a compiled extension.  A
    real extension must keep decoder sessions and encoder completion fences
    native; Python must never request a pixel download to satisfy this API.
    """

    def capabilities(self) -> NativeBridgeCapabilities:
        ...

    def open_decoder(self, path: str, codec: str, device_ptr: int) -> object:
        ...

    def decode_next(self, session: object) -> NativeDecodedTexture | Mapping[str, Any] | None:
        ...

    def close_decoder(self, session: object) -> None:
        ...

    def submit_encode(
        self, texture_handle: int, dxgi_format: int, width: int, height: int,
        encoder_kind: str, adapter_luid: str,
    ) -> NativeEncodeSubmitResult | Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class NativeBridgeLoadResult:
    """Result of optional bridge loading, with an explicit fallback reason."""

    bridge: Optional[WindowsNativeBridge]
    capabilities: Optional[NativeBridgeCapabilities]
    reason: str

    @property
    def available(self) -> bool:
        return self.bridge is not None and self.capabilities is not None


def load_windows_native_bridge(
    module_name: str = "topos_hwdecode_windows",
    module: object | None = None,
) -> NativeBridgeLoadResult:
    """Load and validate the optional native bridge.

    ``module`` is injectable for contract tests.  Only import/load errors are
    converted to a fallback result; malformed APIs are reported as probe
    failures instead of being swallowed.

    AUD-36208：``factory()`` 与 ``capabilities()`` 属于加载边界——原生
    扩展在初始化阶段抛错（ctypes/加载器异常）同样必须收敛为显式的
    probe-failure 结果，绝不让异常逃逸使调用方（zero-copy probe）崩溃，
    与本 docstring 的承诺保持一致。
    """
    if module is None:
        try:
            module = importlib.import_module(module_name)
        except (ImportError, OSError) as exc:
            return NativeBridgeLoadResult(
                None, None,
                param(FallbackReason.NOT_YET_IMPLEMENTED, f"native_bridge:{exc}"),
            )

    factory = getattr(module, "create_bridge", None)
    if not callable(factory):
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, "native_bridge_missing_create_bridge"),
        )
    try:
        bridge = factory()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, f"native_bridge_factory:{exc}"),
        )
    if not isinstance(bridge, WindowsNativeBridge):
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, "native_bridge_protocol_mismatch"),
        )
    try:
        capabilities = bridge.capabilities()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, f"native_bridge_capabilities:{exc}"),
        )
    if not isinstance(capabilities, NativeBridgeCapabilities) or not capabilities.valid:
        return NativeBridgeLoadResult(
            None, None,
            param(FallbackReason.GPU_PROBE_FAILED, "native_bridge_capabilities_invalid"),
        )
    return NativeBridgeLoadResult(bridge, capabilities, "")
