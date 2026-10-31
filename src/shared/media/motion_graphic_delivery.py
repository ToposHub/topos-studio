"""Delivery contract for Motion Graphic render caches (WP-8).

These constants and pure helpers define how a Teaching Animation scene is
encoded into an interactive/final cache: VP8-in-WebM, tiered bitrate
presets, and the 2K-class preview surface.  The contract is intentionally
shared infrastructure — the Edit feature submits cache jobs through it and
the Teaching Animation feature selects tiers/exports through it, so neither
feature imports the other to obtain these values.
"""
from __future__ import annotations

from typing import Any


# Motion Graphic caches are browser recordings rather than ordinary source
# media.  Keep one explicit delivery contract for them so a legacy ProRes or
# low-bitrate cache cannot silently be reused by the timeline.
MOTION_GRAPHIC_CACHE_CODEC = "vp8_high_quality"
MOTION_GRAPHIC_CACHE_CONTAINER = "webm"
# The interactive cache is deliberately a 1080p/2K-class proxy.  The final
# recorder may request a larger surface and gets a resolution-scaled rate.
MOTION_GRAPHIC_PREVIEW_WIDTH = 1920
MOTION_GRAPHIC_PREVIEW_HEIGHT = 1080
MOTION_GRAPHIC_CACHE_BITRATE_KBPS = 40_000
MOTION_GRAPHIC_CACHE_MIN_BITRATE_KBPS = 20_000
MOTION_GRAPHIC_CACHE_PATH_TAG = "vp8_40m"

# WP-8 delivery presets for Motion Graphic caches.  Each tier names the
# target/minimum VP8 rate at the 1080p reference surface; larger render
# surfaces scale both values by pixel area.
MOTION_GRAPHIC_QUALITY_PRESETS: dict[str, tuple[int, int]] = {
    "proxy": (8_000, 4_000),
    "balanced": (20_000, 10_000),
    "high_quality": (MOTION_GRAPHIC_CACHE_BITRATE_KBPS, MOTION_GRAPHIC_CACHE_MIN_BITRATE_KBPS),
}
MOTION_GRAPHIC_DEFAULT_QUALITY = "high_quality"


def motion_graphic_quality_preset(name: Any) -> tuple[int, int]:
    """Normalize a quality tier to (target_kbps, min_kbps) at 1080p."""
    key = str(name or MOTION_GRAPHIC_DEFAULT_QUALITY).lower()
    return MOTION_GRAPHIC_QUALITY_PRESETS.get(
        key, MOTION_GRAPHIC_QUALITY_PRESETS[MOTION_GRAPHIC_DEFAULT_QUALITY])


def describe_motion_graphic_quality(name: Any) -> str:
    """User-facing tier label used by cache status surfaces."""
    key = str(name or MOTION_GRAPHIC_DEFAULT_QUALITY).lower()
    if key not in MOTION_GRAPHIC_QUALITY_PRESETS:
        key = MOTION_GRAPHIC_DEFAULT_QUALITY
    return {
        "proxy": "Proxy",
        "balanced": "Balanced",
        "high_quality": "High Quality",
    }[key]


def motion_graphic_preview_size(width: int, height: int) -> tuple[int, int]:
    """Fit an interactive Motion Graphic cache into the 2K preview surface."""
    source_width = max(1, int(width or MOTION_GRAPHIC_PREVIEW_WIDTH))
    source_height = max(1, int(height or MOTION_GRAPHIC_PREVIEW_HEIGHT))
    scale = min(
        1.0,
        MOTION_GRAPHIC_PREVIEW_WIDTH / source_width,
        MOTION_GRAPHIC_PREVIEW_HEIGHT / source_height,
    )
    target_width = max(2, int(round(source_width * scale)))
    target_height = max(2, int(round(source_height * scale)))
    # Video encoders require even dimensions for yuv420p.
    return target_width - target_width % 2, target_height - target_height % 2


def motion_graphic_bitrate_kbps(
    width: int,
    height: int,
    *,
    requested_kbps: int | None = None,
    preset: Any = MOTION_GRAPHIC_DEFAULT_QUALITY,
) -> int:
    """Return the VP8 target rate for a render surface.

    The preset tier fixes the 1920x1080 reference rate (High Quality keeps
    the user-facing 40 Mbps default); larger final-output surfaces scale by
    pixel area (3840x2160 therefore targets 4x the reference), while an
    explicit lower request is never allowed to undercut the preset target.
    """
    reference_kbps = motion_graphic_quality_preset(preset)[0]
    surface_area = max(1, int(width or 1)) * max(1, int(height or 1))
    reference_area = MOTION_GRAPHIC_PREVIEW_WIDTH * MOTION_GRAPHIC_PREVIEW_HEIGHT
    scaled = reference_kbps * surface_area / reference_area
    requested = int(requested_kbps or 0)
    return max(reference_kbps, requested, int(round(scaled / 1000.0)) * 1000)


def motion_graphic_min_bitrate_kbps(
    width: int,
    height: int,
    *,
    preset: Any = MOTION_GRAPHIC_DEFAULT_QUALITY,
) -> int:
    """Return the minimum effective average bitrate for a VP8 recording."""
    reference_kbps = motion_graphic_quality_preset(preset)[1]
    surface_area = max(1, int(width or 1)) * max(1, int(height or 1))
    reference_area = MOTION_GRAPHIC_PREVIEW_WIDTH * MOTION_GRAPHIC_PREVIEW_HEIGHT
    scaled = reference_kbps * surface_area / reference_area
    return max(reference_kbps, int(round(scaled / 1000.0)) * 1000)


__all__ = [
    "MOTION_GRAPHIC_CACHE_BITRATE_KBPS",
    "MOTION_GRAPHIC_CACHE_CODEC",
    "MOTION_GRAPHIC_CACHE_CONTAINER",
    "MOTION_GRAPHIC_CACHE_MIN_BITRATE_KBPS",
    "MOTION_GRAPHIC_CACHE_PATH_TAG",
    "MOTION_GRAPHIC_DEFAULT_QUALITY",
    "MOTION_GRAPHIC_PREVIEW_HEIGHT",
    "MOTION_GRAPHIC_PREVIEW_WIDTH",
    "MOTION_GRAPHIC_QUALITY_PRESETS",
    "describe_motion_graphic_quality",
    "motion_graphic_bitrate_kbps",
    "motion_graphic_min_bitrate_kbps",
    "motion_graphic_preview_size",
    "motion_graphic_quality_preset",
]
