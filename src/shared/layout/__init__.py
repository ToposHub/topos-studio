"""
Layout Module - Layout profile management for Topos.

Manages layout profiles keyed by product_mode + page_id, supporting
save/load from config, panel filtering by enabled features, legacy
page_layouts migration, and cross-product-mode isolation.

This is a new layer on top of the existing DockLayoutMixin layout
save/restore system. It does not replace it.
"""

from src.shared.layout.profile import LayoutProfile
from src.shared.layout.store import LayoutStore

TIMELINE_DOCK_MIN_HEIGHT = 260
TIMELINE_DOCK_DEFAULT_HEIGHT = 420

__all__ = [
    "LayoutProfile",
    "LayoutStore",
    "TIMELINE_DOCK_MIN_HEIGHT",
    "TIMELINE_DOCK_DEFAULT_HEIGHT",
]
