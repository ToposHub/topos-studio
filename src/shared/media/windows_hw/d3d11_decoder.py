"""D3D11 GPU-surface decoder boundary.

The actual Media Foundation/NVDEC/AMF surface ownership stays in the optional
``topos_hwdecode_windows`` extension.  This module is the typed Python
adapter: it validates the native result, carries colour/timestamp metadata,
and turns each native handle into a decoder-owned lease.  If the extension is
absent, opening fails with ``not_yet_implemented`` and no CPU decoder is
silently substituted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .d3d11_device import DXGI_FORMAT_NV12, DXGI_FORMAT_P010
from .d3d11_frame import D3D11GpuFrame
from .d3d11_texture_pool import D3D11ExternalTextureLease, TextureLease
from .native_bridge import (
    NativeDecodedTexture,
    WindowsNativeBridge,
    load_windows_native_bridge,
)
from .reasons import FallbackReason, codec_unsupported_reason, param

logger = logging.getLogger(__name__)

__all__ = ["D3D11DecodedTexture", "D3D11Decoder"]


@dataclass(frozen=True)
class D3D11DecodedTexture:
    """GPU-only decoded texture description (never contains CPU planes)."""

    texture_handle: int = 0
    width: int = 0
    height: int = 0
    frame_index: int = -1
    pts: int | None = None
    pixel_format: str = "nv12"
    bit_depth: int = 8
    adapter_luid: str = ""
    color_matrix: str = "bt709"
    color_transfer: str = "bt709"
    color_primaries: str = "bt709"
    video_range: str = "limited"
    duration_us: int | None = None
    source_kind: str = "d3d11va"
    fence: object | None = None
    gpu_decode_ms: float | None = None
    gpu_render_ms: float | None = None
    gpu_yuv_ms: float | None = None

    def __post_init__(self) -> None:
        if self.texture_handle == 0 and self.width == 0 and self.height == 0:
            # Preserve the harmless empty value used by legacy import tests.
            return
        if not self.texture_handle or self.width <= 0 or self.height <= 0:
            raise ValueError("D3D11 解码纹理必须携带有效 handle 和尺寸")
        fmt = self.pixel_format.lower()
        if fmt not in {"nv12", "p010"}:
            raise ValueError(f"D3D11 解码纹理格式不支持: {self.pixel_format}")
        if self.bit_depth != (10 if fmt == "p010" else 8):
            raise ValueError("D3D11 解码纹理格式与 bit depth 不一致")

    @classmethod
    def from_native(cls, value: NativeDecodedTexture | Mapping[str, Any]) -> "D3D11DecodedTexture":
        """Validate a native ABI result without allowing unknown fields."""
        if isinstance(value, NativeDecodedTexture):
            native = value
        elif isinstance(value, Mapping):
            allowed = {
                "texture_handle", "width", "height", "frame_index", "pts_us",
                "pts", "pixel_format", "bit_depth", "adapter_luid",
                "color_matrix", "color_transfer", "color_primaries", "video_range",
                "duration_us", "source_kind", "fence",
                "gpu_decode_ms", "gpu_render_ms", "gpu_yuv_ms",
            }
            unknown = set(value) - allowed
            if unknown:
                raise ValueError(f"native decoder 未知字段: {sorted(unknown)}")
            # AUD-36002：缺必需键必须转成 ValueError——直接下标取值抛出的
            # KeyError 逃出 decode_next 的 except (TypeError, ValueError)
            required = ("texture_handle", "width", "height")
            missing = [key for key in required if key not in value]
            if missing:
                raise ValueError(f"native decoder 缺少必需字段: {sorted(missing)}")
            native = NativeDecodedTexture(
                texture_handle=int(value["texture_handle"]),
                width=int(value["width"]),
                height=int(value["height"]),
                frame_index=int(value.get("frame_index", -1)),
                pts_us=(value.get("pts_us") if "pts_us" in value else value.get("pts")),
                pixel_format=str(value.get("pixel_format", "nv12")),
                bit_depth=int(value.get("bit_depth", 8)),
                adapter_luid=str(value.get("adapter_luid", "")),
                color_matrix=str(value.get("color_matrix", "bt709")),
                color_transfer=str(value.get("color_transfer", "bt709")),
                color_primaries=str(value.get("color_primaries", "bt709")),
                video_range=str(value.get("video_range", "limited")),
                duration_us=value.get("duration_us"),
                source_kind=str(value.get("source_kind", "d3d11va")),
                fence=value.get("fence"),
                gpu_decode_ms=value.get("gpu_decode_ms"),
                gpu_render_ms=value.get("gpu_render_ms"),
                gpu_yuv_ms=value.get("gpu_yuv_ms"),
            )
        else:
            raise TypeError("native decoder 返回值必须是 NativeDecodedTexture 或 mapping")
        return cls(
            texture_handle=native.texture_handle,
            width=native.width,
            height=native.height,
            frame_index=native.frame_index,
            pts=native.pts_us,
            pixel_format=native.pixel_format,
            bit_depth=native.bit_depth,
            adapter_luid=native.adapter_luid,
            color_matrix=native.color_matrix,
            color_transfer=native.color_transfer,
            color_primaries=native.color_primaries,
            video_range=native.video_range,
            duration_us=native.duration_us,
            source_kind=native.source_kind,
            fence=native.fence,
            gpu_decode_ms=native.gpu_decode_ms,
            gpu_render_ms=native.gpu_render_ms,
            gpu_yuv_ms=native.gpu_yuv_ms,
        )


class D3D11Decoder:
    """Typed adapter for a native D3D11 decode-surface session."""

    _SUPPORTED_CODECS = {"h264", "hevc", "h265", "vc1", "vp9", "av1"}

    def __init__(
        self,
        bridge: WindowsNativeBridge | None = None,
        device_ptr: int = 0,
        module_name: str = "topos_hwdecode_windows",
    ) -> None:
        self._bridge = bridge
        self._device_ptr = device_ptr
        self._module_name = module_name
        self._session: object | None = None
        self._is_open = False
        self._width = 0
        self._height = 0
        self._codec = ""
        self._last_fallback_reason = FallbackReason.NOT_PROBED.value
        self._last_native_texture: D3D11DecodedTexture | None = None

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def last_fallback_reason(self) -> str:
        return self._last_fallback_reason

    @property
    def adapter_luid(self) -> str:
        return self._last_native_texture.adapter_luid if self._last_native_texture else ""

    def open(self, path: str, codec: str = "") -> bool:
        """Open a GPU decode session through the optional native bridge."""
        self.close()
        # 每次会话的源尺寸独立：残留上一会话的尺寸会把新源的合法首帧
        # 误判成 decode_size_changed（复用同一实例切换素材的场景）。
        self._width = 0
        self._height = 0
        normalized_codec = codec.strip().lower()
        if normalized_codec and normalized_codec not in self._SUPPORTED_CODECS:
            self._last_fallback_reason = codec_unsupported_reason("decode", normalized_codec)
            return False
        if self._bridge is None:
            loaded = load_windows_native_bridge(self._module_name)
            if not loaded.available:
                # Keep the public decoder reason stable for legacy callers;
                # the detailed loader reason remains available to probes.
                self._last_fallback_reason = (
                    FallbackReason.NOT_YET_IMPLEMENTED.value
                    if loaded.reason.startswith(FallbackReason.NOT_YET_IMPLEMENTED.value)
                    else loaded.reason
                )
                return False
            self._bridge = loaded.bridge
        assert self._bridge is not None
        source = Path(path)
        if not source.is_file():
            self._last_fallback_reason = param(
                FallbackReason.GPU_PROBE_FAILED, "decode_source_missing"
            )
            return False
        capabilities = self._bridge.capabilities()
        if normalized_codec and normalized_codec not in {
            item.lower() for item in capabilities.decode_codecs
        }:
            self._last_fallback_reason = codec_unsupported_reason("decode", normalized_codec)
            return False
        try:
            self._session = self._bridge.open_decoder(
                str(source), normalized_codec, self._device_ptr
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._last_fallback_reason = param(FallbackReason.GPU_PROBE_FAILED, f"decode_open:{exc}")
            return False
        if self._session is None:
            self._last_fallback_reason = FallbackReason.NOT_YET_IMPLEMENTED.value
            return False
        self._codec = normalized_codec
        self._is_open = True
        self._last_fallback_reason = ""
        return True

    def decode_next(self) -> D3D11DecodedTexture | None:
        """Return the next GPU texture, never a PyAV/NumPy frame."""
        if not self._is_open or self._bridge is None or self._session is None:
            return None
        try:
            raw = self._bridge.decode_next(self._session)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._last_fallback_reason = param(FallbackReason.GPU_PROBE_FAILED, f"decode_next:{exc}")
            return None
        if raw is None:
            self._last_fallback_reason = "end_of_stream"
            return None
        try:
            decoded = D3D11DecodedTexture.from_native(raw)
        except (TypeError, ValueError) as exc:
            self._last_fallback_reason = param(FallbackReason.GPU_PROBE_FAILED, f"decode_contract:{exc}")
            return None
        if self._width == 0:
            self._width, self._height = decoded.width, decoded.height
        elif (decoded.width, decoded.height) != (self._width, self._height):
            self._last_fallback_reason = param(FallbackReason.GPU_PROBE_FAILED, "decode_size_changed")
            return None
        self._last_native_texture = decoded
        self._last_fallback_reason = ""
        return decoded

    def decode_next_gpu_frame(
        self,
        release_callback_factory: Callable[[D3D11DecodedTexture], Callable[[], None]],
    ) -> D3D11GpuFrame | None:
        """Wrap one native result in a decoder-owned D3D11 frame lease."""
        decoded = self.decode_next()
        if decoded is None:
            return None
        lease: TextureLease = D3D11ExternalTextureLease(
            decoded.texture_handle, release_callback_factory(decoded)
        )
        dxgi_format = (
            DXGI_FORMAT_P010
            if decoded.pixel_format.lower() == "p010"
            else DXGI_FORMAT_NV12
        )
        return D3D11GpuFrame(
            texture_ptr=decoded.texture_handle,
            dxgi_format=dxgi_format,
            width=decoded.width,
            height=decoded.height,
            lease=lease,
            adapter_luid=decoded.adapter_luid,
            frame_index=decoded.frame_index,
            pts_us=decoded.pts,
            color_matrix=decoded.color_matrix,
            color_transfer=decoded.color_transfer,
            color_primaries=decoded.color_primaries,
            video_range=decoded.video_range,
            bit_depth=decoded.bit_depth,
            gpu_decode_ms=decoded.gpu_decode_ms,
            gpu_render_ms=decoded.gpu_render_ms,
            gpu_yuv_ms=decoded.gpu_yuv_ms,
        )

    def close(self) -> None:
        """Close the native session and reset adapter state.

        AUD-36003：先清理 Python 侧状态再调用 close_decoder，且对原生
        关闭失败做防护——旧实现异常时残留半开状态（_is_open=True、
        _session 悬挂），污染 open() 首行 self.close() 的复用路径。
        关闭失败仅记录 warning（会话句柄可能泄漏给原生层），状态机保持
        一致的"已关闭"，后续 open() 可重建会话。
        """
        session = self._session
        self._session = None
        self._is_open = False
        self._last_native_texture = None
        if session is not None and self._bridge is not None:
            try:
                self._bridge.close_decoder(session)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("close_decoder 失败（原生会话句柄可能泄漏）: %s", exc)
