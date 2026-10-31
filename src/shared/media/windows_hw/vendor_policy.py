"""Vendor and multi-adapter policy gates for the Windows D3D11 path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence, Tuple

from .reasons import FallbackReason, param

__all__ = [
    "VENDOR_SPECIALIZATIONS",
    "VendorABDecision",
    "MultiGpuDecision",
    "evaluate_vendor_ab",
    "select_single_adapter",
]

VENDOR_SPECIALIZATIONS = {
    "nvidia": ("cuda_ai", "nvenc"),
    "amd": ("smartaccess_video", "amf"),
    "intel": ("onevpl",),
}


@dataclass(frozen=True)
class VendorABDecision:
    vendor: str
    feature: str
    enabled: bool
    gain_ratio: float
    reason: str


@dataclass(frozen=True)
class MultiGpuDecision:
    selected_luid: str
    enabled: bool
    reason: str
    adapter_luids: Tuple[str, ...]


def evaluate_vendor_ab(
    vendor: str,
    feature: str,
    baseline_fps: float,
    candidate_fps: float,
    *,
    generic_d3d11_gate: bool,
    has_staging_copy: bool = False,
    has_cross_adapter_copy: bool = False,
) -> VendorABDecision:
    """Enable a vendor-specific optimization only after a fair A/B run."""
    normalized_vendor = vendor.strip().lower()
    normalized_feature = feature.strip().lower()
    allowed = normalized_feature in VENDOR_SPECIALIZATIONS.get(normalized_vendor, ())
    if not allowed:
        return VendorABDecision(
            normalized_vendor, normalized_feature, False, 0.0,
            param(FallbackReason.CODEC_UNSUPPORTED_BY_HW, "vendor_feature_not_declared"),
        )
    if not generic_d3d11_gate:
        return VendorABDecision(
            normalized_vendor, normalized_feature, False, 0.0,
            FallbackReason.NOT_PROBED.value + ":generic_d3d11_gate",
        )
    if has_staging_copy or has_cross_adapter_copy:
        return VendorABDecision(
            normalized_vendor, normalized_feature, False, 0.0,
            "zero_copy_violation",
        )
    if baseline_fps <= 0.0 or candidate_fps < 0.0:
        raise ValueError("A/B FPS 必须是非负且 baseline 大于 0")
    gain = candidate_fps / baseline_fps - 1.0
    if gain + 1e-9 < 0.15:
        return VendorABDecision(
            normalized_vendor, normalized_feature, False, gain,
            "ab_gain_below_15_percent",
        )
    return VendorABDecision(normalized_vendor, normalized_feature, True, gain, "")


def select_single_adapter(
    adapter_luids: Sequence[str],
    requested_luid: str = "",
    *,
    independent_partition: bool = False,
    measured_gain_ratio: float = 0.0,
    has_cross_adapter_copy: bool = False,
) -> MultiGpuDecision:
    """Bind one export to one LUID unless a safe, measured partition exists."""
    unique = tuple(dict.fromkeys(str(item) for item in adapter_luids if str(item)))
    if not unique:
        return MultiGpuDecision("", False, "adapter_luid_unavailable", unique)
    if requested_luid and requested_luid not in unique:
        return MultiGpuDecision(
            unique[0], False,
            f"{FallbackReason.ADAPTER_MISMATCH.value}:requested={requested_luid}",
            unique,
        )
    selected = requested_luid or unique[0]
    if len(unique) == 1:
        return MultiGpuDecision(selected, False, "single_adapter", unique)
    if not independent_partition or has_cross_adapter_copy or measured_gain_ratio < 0.15:
        return MultiGpuDecision(selected, False, "multi_gpu_gate_not_met", unique)
    return MultiGpuDecision(selected, True, "", unique)
