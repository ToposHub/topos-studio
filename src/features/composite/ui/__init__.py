"""
Composite UI package (new location).

Public surface (AUD-9409: the previous docstring advertised three viewer /
inspector widgets that this package never imported or exported — the viewer
lives on CompositePage, not here. It now lists only real exports):

- ViewerToolMode: Tool modes for viewer interaction
- ViewerInteractionState: State tracking for tool interactions
- HandleType / hit_test_handle and the NDC coordinate helpers shared by the
  viewer overlay stack
"""

from src.features.composite.ui.viewer_tool_system import (
    ViewerToolMode,
    ViewerInteractionState,
    HandleType,
    TOOL_MODE_LABELS,
    TOOL_MODE_SHORTCUTS,
    view_pos_to_ndc,
    ndc_to_view_pos,
    ndc_to_image_pos,
    image_pos_to_ndc,
    compute_image_ndc_rect,
    hit_test_handle,
)

__all__ = [
    "ViewerToolMode",
    "ViewerInteractionState",
    "HandleType",
    "TOOL_MODE_LABELS",
    "TOOL_MODE_SHORTCUTS",
    "view_pos_to_ndc",
    "ndc_to_view_pos",
    "ndc_to_image_pos",
    "image_pos_to_ndc",
    "compute_image_ndc_rect",
    "hit_test_handle",
]