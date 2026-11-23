"""The native Metal viewport host moved to ``src/shared/ui/widgets``.

The playback and colour domains reuse it as well, so it no longer lives in
the Edit feature.  This module keeps the old import path working.
"""

from __future__ import annotations

from src.shared.ui.widgets.metal_viewport_host import (  # noqa: F401
    METAL_VIEWPORT_PIXEL_FORMAT,
    MetalViewportHost,
)

__all__ = ["METAL_VIEWPORT_PIXEL_FORMAT", "MetalViewportHost"]
