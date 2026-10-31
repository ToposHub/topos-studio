"""
Edit Page Module - Timeline-based editing interface.

This module contains the Edit page and its associated panels:
- EditPage: Main container and signal hub
- MediaPoolPanel: Project-level media asset management
- ProgramViewerWidget: Program viewer (节目监视器) for playhead preview
- SourceViewerWidget: Source viewer for previewing media assets (P3.1)
- TimelinePanel: Timeline view and editing tools
- EditInfoPanel: Detailed read-only metadata for the selected media
- EditPropertiesPanel: Properties for selected clips/transitions
- EditEffectsPanel: Effects library and application
- SceneCutDialog: Scene cut detection review dialog (P4)
- SceneCutPreviewWidget: Three-panel preview for scene cuts (P4)
- SceneCutGraphWidget: Detection score visualization graph (P4)
- SceneCutResultModel: QAbstractTableModel for scene cut results (P4)

Note: Heavy modules (MediaPoolPanel, TimelinePanel, etc.) use lazy imports
to avoid pulling in cv2/pillow_heif/etc. when only lightweight components
are needed (e.g. scene_cut_result_model tests).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# ── Lightweight core imports (no heavy dependencies) ─────────────────────
from .edit_page import EditPage
from . import edit_style

# ── Deferred heavy imports ─────────────────────────────────────────────────
# These modules pull in cv2, pillow_heif, and other heavy dependencies.
# They are loaded on first attribute access via __getattr__.

_HEAVY_MODULES = {
    "MediaPoolPanel": (".media_pool_panel", "MediaPoolPanel"),
    "ProgramViewerWidget": (".program_viewer", "ProgramViewerWidget"),
    "ProgramViewer": (".program_viewer", "ProgramViewer"),
    "ProgramViewerBar": (".program_viewer", "ProgramViewerBar"),
    "TransportControlBar": (".program_viewer", "TransportControlBar"),
    "SourceViewerWidget": (".source_viewer", "SourceViewerWidget"),
    "SourceViewer": (".source_viewer", "SourceViewer"),
    "SourceViewerBar": (".source_viewer", "SourceViewerBar"),
    "SourceSlider": (".source_viewer", "SourceSlider"),
    "TimelinePanel": (".timeline_panel", "TimelinePanel"),
    "EditInfoPanel": (".edit_info_panel", "EditInfoPanel"),
    "EditPropertiesPanel": (".edit_properties_panel", "EditPropertiesPanel"),
    "EditEffectsPanel": (".edit_effects_panel", "EditEffectsPanel"),
    "timeline_items": (".timeline_items", None),  # submodule
    "QuickProjectDialog": (".quick_project_dialog", "QuickProjectDialog"),
    # Scene Cut Detection UI components (P4)
    "SceneCutDialog": (".scene_cut_dialog", "SceneCutDialog"),
    "TransportBar": (".scene_cut_dialog", "TransportBar"),
    "SceneListWidget": (".scene_cut_dialog", "SceneListWidget"),
    "BottomActionBar": (".scene_cut_dialog", "BottomActionBar"),
    "create_scene_cut_dialog": (".scene_cut_dialog", "create_scene_cut_dialog"),
    "show_scene_cut_dialog": (".scene_cut_dialog", "show_scene_cut_dialog"),
    "SceneCutPreviewWidget": (".scene_cut_preview_widget", "SceneCutPreviewWidget"),
    "SinglePreviewPanel": (".scene_cut_preview_widget", "SinglePreviewPanel"),
    "PreviewImageWidget": (".scene_cut_preview_widget", "PreviewImageWidget"),
    "ThumbnailLoader": (".scene_cut_preview_widget", "ThumbnailLoader"),
    "PreviewPanelType": (".scene_cut_preview_widget", "PreviewPanelType"),
    "SceneCutGraphWidget": (".scene_cut_graph_widget", "SceneCutGraphWidget"),
    "SceneCutGraphArea": (".scene_cut_graph_widget", "SceneCutGraphArea"),
    "MiniScrubber": (".scene_cut_graph_widget", "MiniScrubber"),
    "ThresholdSlider": (".scene_cut_graph_widget", "ThresholdSlider"),
    "GraphScale": (".scene_cut_graph_widget", "GraphScale"),
    "SceneCutResultModel": (".scene_cut_result_model", "SceneCutResultModel"),
    "SceneCutFilterProxy": (".scene_cut_result_model", "SceneCutFilterProxy"),
    "Column": (".scene_cut_result_model", "Column"),
}

# Cache for already-loaded attributes
_loaded: dict = {}


def __getattr__(name: str):
    """Lazy-load heavy modules on first access."""
    if name in _loaded:
        return _loaded[name]

    if name in _HEAVY_MODULES:
        relative_module, attr_name = _HEAVY_MODULES[name]
        mod = importlib.import_module(relative_module, __package__)
        if attr_name is None:
            # Submodule import
            _loaded[name] = mod
            return mod
        value = getattr(mod, attr_name)
        _loaded[name] = value
        return value

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Type-checking support for IDEs
if TYPE_CHECKING:
    from .media_pool_panel import MediaPoolPanel
    from .program_viewer import ProgramViewerWidget, ProgramViewer, ProgramViewerBar
    from .source_viewer import SourceViewerWidget, SourceViewer, SourceViewerBar, SourceSlider
    from .timeline_panel import TimelinePanel
    from .edit_info_panel import EditInfoPanel
    from .edit_properties_panel import EditPropertiesPanel
    from .edit_effects_panel import EditEffectsPanel
    from . import timeline_items
    from .quick_project_dialog import QuickProjectDialog
    from .scene_cut_dialog import (
        SceneCutDialog, TransportBar, SceneListWidget, BottomActionBar,
        create_scene_cut_dialog, show_scene_cut_dialog,
    )
    from .scene_cut_preview_widget import (
        SceneCutPreviewWidget, SinglePreviewPanel, PreviewImageWidget,
        ThumbnailLoader, PreviewPanelType,
    )
    from .scene_cut_graph_widget import (
        SceneCutGraphWidget, SceneCutGraphArea, MiniScrubber,
        ThresholdSlider, GraphScale,
    )
    from .scene_cut_result_model import (
        SceneCutResultModel, SceneCutFilterProxy, Column,
    )


__all__ = [
    "EditPage",
    "MediaPoolPanel",
    "ProgramViewerWidget",
    "ProgramViewer",
    "ProgramViewerBar",
    "TransportControlBar",
    "SourceViewerWidget",
    "SourceViewer",
    "SourceViewerBar",
    "SourceSlider",
    "TimelinePanel",
    "EditInfoPanel",
    "EditPropertiesPanel",
    "EditEffectsPanel",
    "edit_style",
    "timeline_items",
    "QuickProjectDialog",
    # Scene Cut Detection UI
    "SceneCutDialog",
    "TransportBar",
    "SceneListWidget",
    "BottomActionBar",
    "create_scene_cut_dialog",
    "show_scene_cut_dialog",
    "SceneCutPreviewWidget",
    "SinglePreviewPanel",
    "PreviewImageWidget",
    "ThumbnailLoader",
    "PreviewPanelType",
    "SceneCutGraphWidget",
    "SceneCutGraphArea",
    "MiniScrubber",
    "ThresholdSlider",
    "GraphScale",
    "SceneCutResultModel",
    "SceneCutFilterProxy",
    "Column",
]
