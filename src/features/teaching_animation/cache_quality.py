"""Process-wide Motion Graphic cache-quality preference (WP-8).

The MG editor window selects a delivery tier (Proxy / Balanced / High
Quality); the host module applies this preference to the project's render
cache settings before submitting a cache job, so the editor stays decoupled
from the Edit project's settings objects.
"""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_QUALITY = "high_quality"


def get_cache_quality() -> str:
    """Return the selected Motion Graphic cache tier."""
    with _LOCK:
        return _QUALITY


def set_cache_quality(quality: str) -> str:
    """Store a tier name; unknown values keep the previous selection."""
    global _QUALITY
    from src.shared.media.motion_graphic_delivery import MOTION_GRAPHIC_QUALITY_PRESETS
    key = str(quality or "").lower()
    with _LOCK:
        if key in MOTION_GRAPHIC_QUALITY_PRESETS:
            _QUALITY = key
        return _QUALITY


__all__ = ["get_cache_quality", "set_cache_quality"]
