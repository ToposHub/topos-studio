"""GpuEncodeSurface 契约与 D3D11 编码 surface 提供者（计划 §3.3 / 步骤 2）。

计划原文：

    | GpuEncodeSurface | texture、格式、编码器、fence、提交 / 归还所有权 |
    | 硬规则：编码器拿到的必须是同一 device 的 GPU texture。 |

现状（诚实边界）：NVENC / AMF / oneVPL 的 D3D11 surface 提交需要原生桥
（计划步骤 2 的 `topos_hwdecode_windows` 或等价组件）。本模块先把
**提交前的全部校验**做成真实现：编码器身份词表、同 LUID 校验
（跨 adapter 立即失败并给出 `adapter_mismatch`）、格式/尺寸匹配；
`submit_to_encoder` 在原生桥落地前 fail-closed 返回
`not_yet_implemented`，绝不静默走 CPU 转换伪装硬编提交。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple

from .d3d11_device import (
    DXGI_FORMAT_NV12,
    DXGI_FORMAT_P010,
    D3D11Device,
    check_format_support,
)
from .d3d11_frame import D3D11GpuFrame
from .d3d11_format_contract import D3D11FormatContract, validate_d3d11_format
from .d3d11_texture_pool import TextureLease
from .native_bridge import NativeEncodeSubmitResult, WindowsNativeBridge
from .reasons import FallbackReason, codec_unsupported_reason, param

__all__ = [
    "ENCODER_KINDS",
    "GpuEncodeSurface",
    "D3D11EncodeSurfaceProvider",
]

#: 计划 §3.1：三家 vendor 的硬件编码器身份。提交层按此词表路由原生桥。
ENCODER_KINDS = ("nvenc", "amf", "onevpl")

_FormatSupportFn = Callable[[D3D11Device, int], Tuple[bool, int, str]]


@dataclass(frozen=True)
class GpuEncodeSurface:
    """提交给硬编器的 GPU surface（T0 直通链的编码侧载荷）。

    所有权语义：与来源帧共享同一个 lease——handoff 后 lease 的唯一
    责任人变为编码路径（提交 + 归还恰一次）；来源帧对象此后不得再
    单独 release。
    """

    texture_ptr: int
    dxgi_format: int
    width: int
    height: int
    encoder_kind: str
    adapter_luid: str
    lease: TextureLease
    frame_index: int = -1

    def __post_init__(self) -> None:
        if not self.texture_ptr:
            raise ValueError("GpuEncodeSurface 不接受空纹理指针")
        if self.encoder_kind not in ENCODER_KINDS:
            raise ValueError(
                f"encoder_kind 必须在 {ENCODER_KINDS} 词表内，"
                f"得到 {self.encoder_kind!r}"
            )
        if not self.adapter_luid:
            raise ValueError("GpuEncodeSurface 必须携带 adapter LUID")

    def release(self) -> None:
        self.lease.release()

    def release_after(self, fence: object) -> None:
        """Return the underlying lease only after encoder completion."""
        self.lease.release_after(fence)


class D3D11EncodeSurfaceProvider:
    """把 D3D11GpuFrame 校验并包装为可提交硬编的 surface。

    真实实现层：LUID 同卡校验、DXGI 格式支持预检、编码器词表校验。
    原生桥边界：`submit_to_encoder` 返回 `not_yet_implemented`。
    """

    def __init__(
        self,
        device: D3D11Device,
        encoder_kind: str,
        width: int,
        height: int,
        dxgi_format: int = DXGI_FORMAT_NV12,
        format_support_fn: Optional[_FormatSupportFn] = None,
        native_bridge: WindowsNativeBridge | None = None,
    ) -> None:
        if encoder_kind not in ENCODER_KINDS:
            raise ValueError(
                f"encoder_kind 必须在 {ENCODER_KINDS} 词表内，"
                f"得到 {encoder_kind!r}"
            )
        self._device = device
        self._encoder_kind = encoder_kind
        self._width = width
        self._height = height
        self._dxgi_format = dxgi_format
        self._format_support_fn = format_support_fn or check_format_support
        self._prepared = False
        self._prepare_reason = ""
        self._native_bridge = native_bridge
        self._last_completion_fence: object | None = None
        self._last_gpu_submit_ms: float | None = None

    @property
    def prepared(self) -> bool:
        return self._prepared

    @property
    def last_reason(self) -> str:
        return self._prepare_reason

    @property
    def last_completion_fence(self) -> object | None:
        return self._last_completion_fence

    @property
    def last_gpu_submit_ms(self) -> float | None:
        return self._last_gpu_submit_ms

    def prepare(self) -> Tuple[bool, str]:
        """提交前预检：device LUID、格式支持、编码器词表（全真机可验）。"""
        device = self._device
        if device.is_released:
            self._prepared = False
            self._prepare_reason = param_reason_device_released()
            return False, self._prepare_reason
        if not device.adapter_luid:
            self._prepared = False
            self._prepare_reason = FallbackReason.ADAPTER_MISMATCH.value + (
                ":device_luid_unavailable"
            )
            return False, self._prepare_reason
        if self._native_bridge is not None:
            supported = {
                item.lower() for item in self._native_bridge.capabilities().encode_kinds
            }
            if self._encoder_kind not in supported:
                self._prepared = False
                self._prepare_reason = codec_unsupported_reason(
                    "encode", self._encoder_kind
                )
                return False, self._prepare_reason
        # AUD-36206：格式码识别必须引用 d3d11_device 的共享常量而非
        # 散落的字面量——该码值随后经 submit_encode 传过原生桥 ABI 边界，
        # 与 d3d11_device.DXGI_FORMAT_NV12/P010 的取值（历史错值
        # 65=A8_UNORM / 100，真值应为 77/104，根因修复归属该模块）保持
        # 单一事实源，根因修正后此处自动随之正确。
        pixel_format = (
            "p010" if self._dxgi_format == DXGI_FORMAT_P010
            else "nv12" if self._dxgi_format == DXGI_FORMAT_NV12
            else "unknown"
        )
        if pixel_format == "unknown":
            self._prepared = False
            self._prepare_reason = codec_unsupported_reason(
                "encode_surface", pix_fmt=pixel_format,
            )
            return False, self._prepare_reason
        contract_decision = validate_d3d11_format(
            D3D11FormatContract(
                pixel_format, 10 if pixel_format == "p010" else 8,
                surface_owner="native",
            ),
            for_encode=True,
            encoder_kind=self._encoder_kind,
        )
        if not contract_decision.supported:
            self._prepared = False
            self._prepare_reason = contract_decision.reason
            return False, self._prepare_reason
        ok, _bits, why = self._format_support_fn(device, self._dxgi_format)
        # Some AMD drivers reject manual P010 CheckFormatSupport even though
        # their video decoder/encoder can own and exchange P010 surfaces.  A
        # native bridge is the only legitimate bypass; it still has to accept
        # the actual frame and submit it successfully below.
        if not ok and pixel_format == "p010" and self._native_bridge is not None:
            ok, why = True, ""
        if not ok:
            self._prepared = False
            self._prepare_reason = why
            return False, self._prepare_reason
        self._prepared = True
        self._prepare_reason = ""
        return True, ""

    def surface_from_frame(
        self,
        frame: D3D11GpuFrame,
    ) -> Tuple[Optional[GpuEncodeSurface], str]:
        """同卡校验 + 包装。失败返回 (None, reason)，不消耗 lease。"""
        if not self._prepared:
            return None, self._prepare_reason or FallbackReason.NOT_PROBED.value
        # 计划 §3.3 硬规则：跨 adapter 立即失败并给出 reason。
        same = frame.adapter_luid == self._device.adapter_luid
        if not same:
            return None, (
                f"{FallbackReason.ADAPTER_MISMATCH.value}:"
                f"frame={frame.adapter_luid}|device={self._device.adapter_luid}"
            )
        if frame.dxgi_format != self._dxgi_format:
            return None, codec_unsupported_reason(
                "encode_surface", pix_fmt=(
                    "p010" if frame.dxgi_format == DXGI_FORMAT_P010 else "nv12"
                ),
            )
        frame_format = (
            "p010" if frame.dxgi_format == DXGI_FORMAT_P010
            else "nv12" if frame.dxgi_format == DXGI_FORMAT_NV12
            else "unknown"
        )
        frame_decision = validate_d3d11_format(
            D3D11FormatContract(
                frame_format, frame.bit_depth, surface_owner="decoder",
            ),
            for_encode=True,
            encoder_kind=self._encoder_kind,
        )
        if not frame_decision.supported:
            return None, frame_decision.reason
        if (frame.width, frame.height) != (self._width, self._height):
            return None, (
                f"{FallbackReason.CODEC_UNSUPPORTED_BY_HW.value}:"
                f"surface_size_mismatch:{frame.width}x{frame.height}"
                f"!=({self._width}x{self._height})"
            )
        surface = GpuEncodeSurface(
            texture_ptr=frame.texture_ptr,
            dxgi_format=frame.dxgi_format,
            width=frame.width,
            height=frame.height,
            encoder_kind=self._encoder_kind,
            adapter_luid=self._device.adapter_luid,
            lease=frame.lease,
            frame_index=frame.frame_index,
        )
        return surface, ""

    def submit_to_encoder(
        self,
        surface: GpuEncodeSurface,
    ) -> Tuple[bool, str]:
        """原生桥边界（fail-closed）：真硬编提交待 `topos_hwdecode_windows`。

        绝不允许在本方法里做 CPU 转换伪装成功——调用方收到 False 后按
        词表回退已验证的下载路径，并由 ExportGpuTrail 如实记录。
        """
        if not self._prepared:
            return False, self._prepare_reason or FallbackReason.NOT_PROBED.value
        self._last_completion_fence = None
        self._last_gpu_submit_ms = None
        if self._native_bridge is None:
            return False, FallbackReason.NOT_YET_IMPLEMENTED.value
        try:
            raw = self._native_bridge.submit_encode(
                surface.texture_ptr,
                surface.dxgi_format,
                surface.width,
                surface.height,
                surface.encoder_kind,
                surface.adapter_luid,
            )
            result = self._normalize_submit_result(raw)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return False, f"{FallbackReason.ENCODER_HW_UNAVAILABLE.value}:{exc}"
        if not result.accepted:
            return False, result.reason or FallbackReason.ENCODER_HW_UNAVAILABLE.value
        self._last_completion_fence = result.completion_fence
        self._last_gpu_submit_ms = result.gpu_submit_ms
        return True, ""

    @staticmethod
    def _normalize_submit_result(
        raw: NativeEncodeSubmitResult | Mapping[str, object],
    ) -> NativeEncodeSubmitResult:
        if isinstance(raw, NativeEncodeSubmitResult):
            return raw
        if not isinstance(raw, Mapping):
            raise TypeError("native encoder 返回值必须是 NativeEncodeSubmitResult 或 mapping")
        return NativeEncodeSubmitResult(
            accepted=bool(raw.get("accepted", False)),
            reason=str(raw.get("reason", "")),
            gpu_submit_ms=(
                float(raw["gpu_submit_ms"])
                if raw.get("gpu_submit_ms") is not None else None
            ),
            completion_fence=raw.get("completion_fence"),
            packet_count=int(raw.get("packet_count", 0)),
        )


def param_reason_device_released() -> str:
    return param(FallbackReason.GPU_PROBE_FAILED, "device_released")
