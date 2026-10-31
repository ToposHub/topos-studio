"""Optional Qt editor surfaces for Teaching Animation.

Importing this package does not create Qt objects; widgets are constructed only
when explicitly instantiated (e.g. by ``MotionGraphicEditorWindow``).
"""

from .editor_controller import SceneDiff, SceneEditorController, diff_scenes
from .editor_window_registry import EditorWindowRegistry
from .html_inspector import HtmlInspector
from .html_browser_preview import HtmlBrowserPreview
from .html_devtools_panel import HtmlDevToolsPanel
from .html_source_editor import HtmlSourceEditor
from .layer_tree_model import HTML_LAYER_ROW_ID, LayerRow, build_layer_rows
from .layer_tree_panel import LayerTreePanel
from .motion_graphic_editor_window import MotionGraphicEditorWindow
from .preview_bridge import PreviewBridge, WebEnginePreviewBridge
from .scene_inspector import SceneInspector
from .scene_timeline import SceneTimeline
from .scene_view import SceneView
from .storyboard_panel import StoryboardPanel

__all__ = [
    "EditorWindowRegistry",
    "HTML_LAYER_ROW_ID",
    "HtmlInspector",
    "HtmlBrowserPreview",
    "HtmlDevToolsPanel",
    "LayerRow",
    "LayerTreePanel",
    "MotionGraphicEditorWindow",
    "HtmlSourceEditor",
    "PreviewBridge",
    "SceneDiff",
    "SceneEditorController",
    "SceneInspector",
    "SceneTimeline",
    "SceneView",
    "StoryboardPanel",
    "WebEnginePreviewBridge",
    "build_layer_rows",
    "diff_scenes",
]
