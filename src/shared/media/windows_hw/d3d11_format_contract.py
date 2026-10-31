"""D3D11 surface format and colour-metadata contract.

This module is deliberately a policy layer.  It does not create a texture or
claim that a driver can allocate P010: native decoder/encoder-owned surfaces
remain the required escape hatch when manual P010 allocation is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .d3d11_device import DXGI_FORMAT_NV12, DXGI_FORMAT_P010

__all__ = [
    "DXGI_FORMAT_R16G16B16A16_FLOAT",
    "D3D11FormatContract",
    "D3D11FormatDecision",
    "validate_d3d11_format",
    "same_d3d11_adapter",
]

# DXGI_FORMAT_R16G16B16A16_FLOAT from dxgiformat.h.  Kept local because the
# device module only exposes the two video-surface formats it creates.
DXGI_FORMAT_R16G16B16A16_FLOAT = 10

_FORMAT_BY_NAME = {
    "nv12": DXGI_FORMAT_NV12,
    "p010": DXGI_FORMAT_P010,
    "rgba16f": DXGI_FORMAT_R16G16B16A16_FLOAT,
}
_HDR_TRANSFERS = {"smpte2084", "pq", "arib-std-b67", "hlg"}
_SURFACE_OWNERS = {"decoder", "encoder", "native", "pool"}


@dataclass(frozen=True)
class D3D11FormatContract:
    """One GPU surface's format, colour metadata and ownership provenance."""

    pixel_format: str
    bit_depth: int
    color_matrix: str = "bt709"
    color_transfer: str = "bt709"
    color_primaries: str = "bt709"
    video_range: str = "limited"
    has_alpha: bool = False
    surface_owner: str = "native"

    def __post_init__(self) -> None:
        fmt = self.pixel_format.strip().lower()
        if fmt not in _FORMAT_BY_NAME:
            raise ValueError(f"不支持的 D3D11 surface 格式: {self.pixel_format}")
        expected = {"nv12": 8, "p010": 10, "rgba16f": 16}[fmt]
        if self.bit_depth != expected:
            raise ValueError(
                f"D3D11 格式 {fmt} 的 bit_depth 必须是 {expected}"
            )
        if self.video_range.strip().lower() not in {"limited", "full"}:
            raise ValueError(f"不支持的视频 range: {self.video_range}")
        if self.surface_owner.strip().lower() not in _SURFACE_OWNERS:
            raise ValueError(f"不支持的 surface owner: {self.surface_owner}")

    @property
    def normalized_format(self) -> str:
        return self.pixel_format.strip().lower()

    @property
    def dxgi_format(self) -> int:
        return _FORMAT_BY_NAME[self.normalized_format]

    @property
    def is_hdr(self) -> bool:
        return self.color_transfer.strip().lower() in _HDR_TRANSFERS

    @property
    def is_video_surface(self) -> bool:
        return self.normalized_format in {"nv12", "p010"}

    def to_dict(self) -> dict[str, object]:
        return {
            "pixel_format": self.normalized_format,
            "dxgi_format": self.dxgi_format,
            "bit_depth": self.bit_depth,
            "color_matrix": self.color_matrix,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "video_range": self.video_range,
            "has_alpha": self.has_alpha,
            "surface_owner": self.surface_owner,
            "is_hdr": self.is_hdr,
        }


@dataclass(frozen=True)
class D3D11FormatDecision:
    supported: bool
    reason: str = ""
    format_name: str = ""
    requires_native_owned_surface: bool = False


def validate_d3d11_format(
    contract: D3D11FormatContract,
    *,
    for_encode: bool = False,
    encoder_kind: Optional[str] = None,
) -> D3D11FormatDecision:
    """Validate a surface without probing or allocating driver resources."""
    fmt = contract.normalized_format
    if contract.has_alpha and contract.is_video_surface:
        return D3D11FormatDecision(
            False, "unsupported_format:alpha_on_yuv_surface", fmt
        )
    if contract.is_hdr and (
        fmt != "p010"
        or contract.color_primaries.strip().lower() != "bt2020"
        or contract.color_matrix.strip().lower() != "bt2020nc"
    ):
        return D3D11FormatDecision(False, "unsupported_format:hdr_requires_p010_bt2020", fmt)
    if for_encode and not contract.is_video_surface:
        return D3D11FormatDecision(False, "unsupported_format:encoder_requires_nv12_or_p010", fmt)
    if for_encode and fmt == "p010" and contract.surface_owner not in {"decoder", "encoder", "native"}:
        return D3D11FormatDecision(
            False,
            "unsupported_format:p010_requires_native_owned_surface",
            fmt,
            requires_native_owned_surface=True,
        )
    if encoder_kind is not None and encoder_kind.strip().lower() not in {"nvenc", "amf", "onevpl"}:
        return D3D11FormatDecision(False, f"unsupported_encoder:{encoder_kind}", fmt)
    return D3D11FormatDecision(True, "", fmt)


def same_d3d11_adapter(*adapter_luids: str) -> Tuple[bool, str]:
    """Return whether all provided non-empty LUIDs refer to one adapter."""
    normalized = tuple(value.strip() for value in adapter_luids)
    if not normalized or any(not value for value in normalized):
        return False, "adapter_mismatch:luid_unavailable"
    if len(set(normalized)) != 1:
        return False, "adapter_mismatch:multiple_luids"
    return True, ""
