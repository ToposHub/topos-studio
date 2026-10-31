"""Truthful D3D11 timeline-export gate.

Device creation or a WGL interop extension is not enough to enable the
Windows full-GPU exporter.  This gate requires the optional native decode /
render / encode bridge and is the single selector used by Auto and the
diagnostic UI.
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from typing import Mapping

from .d3d11_device import D3D11Device, create_d3d11_device
from .native_bridge import NativeBridgeCapabilities, load_windows_native_bridge
from .reasons import FallbackReason, param

logger = logging.getLogger(__name__)

__all__ = [
    "D3D11ExportCapability",
    "D3D11T0Evidence",
    "probe_d3d11_export_capability",
    "is_d3d11_export_ready",
]


def _release_probe_device(device: D3D11Device | None) -> None:
    """Release a probe-owned D3D11 device (AUD-36001).

    探测自建的 device + immediate context 是 COM 引用——此前函数内全部
    return 路径都不释放，每次探测泄漏一份。释放失败只降级为 warning
    日志，不影响探测结论（fail-closed 到诊断，不让探测自身崩溃）。
    """
    if device is None or device.is_released:
        return
    try:
        device.release()
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("D3D11 export probe device release failed: %s", exc)


@dataclass(frozen=True)
class D3D11T0Evidence:
    """Evidence captured by one real T0 export on one adapter."""

    adapter_luid: str
    frame_count: int
    output_playable: bool
    decode_gpu_ms: float | None
    render_gpu_ms: float | None
    yuv_gpu_ms: float | None
    encode_submit_gpu_ms: float | None
    cpu_upload_bytes: int = 0
    cpu_download_bytes: int = 0
    staging_allocations: int = 0
    map_calls: int = 0
    copy_resource_calls: int = 0

    def validation_reason(self, expected_luid: str) -> str:
        if not self.adapter_luid or self.adapter_luid != expected_luid:
            return FallbackReason.ADAPTER_MISMATCH.value + ":t0_evidence_luid"
        if self.frame_count < 1 or not self.output_playable:
            return "t0_evidence:output_not_playable"
        if self.cpu_upload_bytes != 0:
            return "t0_evidence:cpu_upload_bytes_positive"
        if self.cpu_download_bytes != 0:
            return "t0_evidence:cpu_download_bytes_positive"
        if self.staging_allocations or self.map_calls or self.copy_resource_calls:
            return "t0_evidence:cpu_pixel_path_observed"
        for name, value in (
            ("decode", self.decode_gpu_ms),
            ("render", self.render_gpu_ms),
            ("yuv_conversion", self.yuv_gpu_ms),
            ("encode_submit", self.encode_submit_gpu_ms),
        ):
            if value is None:
                return f"t0_evidence:gpu_timestamp_missing:{name}"
            if value < 0.0:
                return f"t0_evidence:gpu_timestamp_negative:{name}"
        return ""

    def to_dict(self) -> dict[str, object]:
        return {
            "adapter_luid": self.adapter_luid,
            "frame_count": self.frame_count,
            "output_playable": self.output_playable,
            "decode_gpu_ms": self.decode_gpu_ms,
            "render_gpu_ms": self.render_gpu_ms,
            "yuv_gpu_ms": self.yuv_gpu_ms,
            "encode_submit_gpu_ms": self.encode_submit_gpu_ms,
            "cpu_upload_bytes": self.cpu_upload_bytes,
            "cpu_download_bytes": self.cpu_download_bytes,
            "staging_allocations": self.staging_allocations,
            "map_calls": self.map_calls,
            "copy_resource_calls": self.copy_resource_calls,
        }


@dataclass(frozen=True)
class D3D11ExportCapability:
    ready: bool
    reason: str
    adapter_luid: str = ""
    feature_level: str = ""
    bridge_version: str = ""
    decode_surface: bool = False
    renderer: bool = False
    encode_surface: bool = False
    zero_copy_probe: bool = False
    evidence: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "reason": self.reason,
            "adapter_luid": self.adapter_luid,
            "feature_level": self.feature_level,
            "bridge_version": self.bridge_version,
            "decode_surface": self.decode_surface,
            "renderer": self.renderer,
            "encode_surface": self.encode_surface,
            "zero_copy_probe": self.zero_copy_probe,
            "evidence": dict(self.evidence),
        }


def probe_d3d11_export_capability(
    *,
    device: object | None = None,
    bridge_module: object | None = None,
    t0_evidence: D3D11T0Evidence | None = None,
) -> D3D11ExportCapability:
    """Probe the complete gate; no environment variable can force success."""
    if platform.system() != "Windows":
        return D3D11ExportCapability(False, FallbackReason.NOT_WINDOWS.value)
    owned_device: D3D11Device | None = None
    if device is None:
        device, reason = create_d3d11_device()
        if device is None:
            return D3D11ExportCapability(False, f"device:{reason}")
        owned_device = device
    # AUD-36001：探测自建的 device 在所有 return 路径（成功含）都要释放
    try:
        return _probe_d3d11_export_capability_inner(
            device, bridge_module=bridge_module, t0_evidence=t0_evidence,
        )
    finally:
        _release_probe_device(owned_device)


def _probe_d3d11_export_capability_inner(
    device: object,
    *,
    bridge_module: object | None,
    t0_evidence: D3D11T0Evidence | None,
) -> D3D11ExportCapability:
    """Gate evaluation proper; caller owns device lifetime."""
    identity = getattr(device, "identity", None)
    luid = str(getattr(identity, "adapter_luid", "") or "")
    feature_level = hex(int(getattr(identity, "feature_level", 0))) if identity else ""
    if not luid:
        return D3D11ExportCapability(
            False,
            FallbackReason.ADAPTER_MISMATCH.value + ":device_luid_unavailable",
            feature_level=feature_level,
        )
    loaded = load_windows_native_bridge(module=bridge_module)
    if not loaded.available or loaded.capabilities is None:
        return D3D11ExportCapability(
            False,
            loaded.reason,
            adapter_luid=luid,
            feature_level=feature_level,
            evidence={"device": True, "native_bridge": False},
        )
    caps: NativeBridgeCapabilities = loaded.capabilities
    decode = bool(caps.decode_codecs)
    encode = bool(caps.encode_kinds)
    render = bool(caps.d3d11_renderer)
    if not decode:
        reason = param(FallbackReason.CODEC_UNSUPPORTED_BY_HW, "native_decode_surface")
    elif not render:
        reason = FallbackReason.NOT_YET_IMPLEMENTED.value + ":d3d11_renderer"
    elif not encode:
        reason = param(FallbackReason.ENCODER_HW_UNAVAILABLE, "native_surface_submit")
    else:
        # A bridge declaration is necessary but not sufficient.  The first
        # real T0 run must add Direct3D timestamps and zero byte counters.
        reason = FallbackReason.NOT_PROBED.value
        if t0_evidence is not None:
            reason = t0_evidence.validation_reason(luid)
            if not reason:
                return D3D11ExportCapability(
                    ready=True,
                    reason="",
                    adapter_luid=luid,
                    feature_level=feature_level,
                    bridge_version=caps.version,
                    decode_surface=decode,
                    renderer=render,
                    encode_surface=encode,
                    zero_copy_probe=True,
                    evidence={
                        "device": True,
                        "native_bridge": True,
                        "t0_evidence": True,
                        "t0": t0_evidence.to_dict(),
                    },
                )
    return D3D11ExportCapability(
        ready=False,
        reason=reason,
        adapter_luid=luid,
        feature_level=feature_level,
        bridge_version=caps.version,
        decode_surface=decode,
        renderer=render,
        encode_surface=encode,
        zero_copy_probe=False,
        evidence={
            "device": True,
            "native_bridge": True,
            "t0_evidence": t0_evidence is not None,
            **({"t0": t0_evidence.to_dict()} if t0_evidence is not None else {}),
        },
    )


def is_d3d11_export_ready() -> bool:
    return probe_d3d11_export_capability().ready
