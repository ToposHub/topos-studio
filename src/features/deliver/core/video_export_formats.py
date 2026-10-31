"""Canonical video container/codec compatibility data.

The Deliver UI and export preflight must agree on which encoder choices are
valid for a container.  Keeping the matrix here prevents an invalid option
from being selectable in the UI while the exporter separately rejects it.
"""

from __future__ import annotations

from typing import FrozenSet, List, Optional

__all__ = [
    "CONTAINER_ORDER",
    "CODEC_CONTAINER_TABLE",
    "SEQUENCE_CODECS",
    "allowed_codecs_for_container",
    "allowed_containers_for_codec",
    "codec_container_error",
    "is_codec_container_compatible",
]


CONTAINER_ORDER: tuple[str, ...] = ("mp4", "mov", "mkv", "mxf", "avi", "webm", "gif")

# Keep this table deliberately strict.  The UI is allowed to expose only
# combinations that the exporter and common playback tools can represent.
CODEC_CONTAINER_TABLE: dict[str, FrozenSet[str]] = {
    "h264": frozenset({"mp4", "mov", "mkv", "avi", "webm"}),
    "hevc": frozenset({"mp4", "mov", "mkv", "webm"}),
    "gif": frozenset({"gif"}),
    "prores_proxy": frozenset({"mov", "mkv", "mxf"}),
    "prores_lt": frozenset({"mov", "mkv", "mxf"}),
    "prores_422": frozenset({"mov", "mkv", "mxf"}),
    "prores_422hq": frozenset({"mov", "mkv", "mxf"}),
    "prores_4444": frozenset({"mov", "mkv", "mxf"}),
    "prores_4444xq": frozenset({"mov", "mkv", "mxf"}),
    "dnxhr_lb": frozenset({"mov", "mkv", "mxf"}),
    "dnxhr_sq": frozenset({"mov", "mkv", "mxf"}),
    "dnxhr_hq": frozenset({"mov", "mkv", "mxf"}),
    "dnxhr_hqx": frozenset({"mov", "mkv", "mxf"}),
    "dnxhr_444": frozenset({"mov", "mkv", "mxf"}),
    # Topos Video Codec（自研，阶段 8）：自研 MOV 容器（FourCC TPIC，
    # container_spec_v1）——只有 mov，无 mkv/mxf 写出路径
    "topos_proxy": frozenset({"mov"}),
    "topos_lt": frozenset({"mov"}),
    "topos": frozenset({"mov"}),
    "topos_hq": frozenset({"mov"}),
    "topos_4444": frozenset({"mov"}),   # R4.6：自研 MOV only
    "topos_4444xq": frozenset({"mov"}),
    "topos_lp": frozenset({"mov"}),     # v1.8（ADR-C056）：帧间档，自研 MOV only
    "topos_netproxy": frozenset({"mov"}),   # v1.9（P2）：网络代理档，自研 MOV only
    "topos_raw": frozenset({"mov"}),    # M4-R8（ADR-C058）：RAW 直出，自研 MOV only
    "png_sequence": frozenset({""}),
    "tiff_sequence": frozenset({""}),
    "topos_image_sequence": frozenset({""}),
}

SEQUENCE_CODECS: frozenset[str] = frozenset({
    "png_sequence", "tiff_sequence", "topos_image_sequence",
})


def allowed_codecs_for_container(container: str) -> List[str]:
    """Return codecs valid for *container*, in catalogue order."""
    normalized = container.lower().strip()
    return [
        codec
        for codec, containers in CODEC_CONTAINER_TABLE.items()
        if normalized in containers
    ]


def allowed_containers_for_codec(codec: str) -> FrozenSet[str]:
    """Return the valid containers for *codec*, or an empty set if unknown."""
    return CODEC_CONTAINER_TABLE.get(codec, frozenset())


def is_codec_container_compatible(codec: str, container: str) -> bool:
    """Return whether a codec can be wrapped in a container."""
    return container.lower().strip() in allowed_containers_for_codec(codec)


def codec_container_error(codec: str, container: str) -> Optional[str]:
    """Return a user-facing compatibility error, or ``None`` when valid."""
    allowed = CODEC_CONTAINER_TABLE.get(codec)
    normalized = container.lower().strip()
    if allowed is None:
        return f"Unknown codec “{codec}”."
    if normalized not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        return (
            f"Codec “{codec}” cannot be wrapped in “{normalized}”. "
            f"Allowed: {allowed_text}."
        )
    return None
