"""Independent non-modal Motion Graphic editor window."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
import uuid

from PyQt6.QtCore import QEvent, QSize, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QCursor, QDesktopServices
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QAbstractSpinBox, QApplication, QButtonGroup, QComboBox, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QInputDialog, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QStackedWidget,
    QTabWidget, QTextEdit, QToolBar, QToolButton, QVBoxLayout, QWidget, QDialog,
)
from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.i18n.i18n import I18N
from src.shared.assets.icon_manager import IconColors, get_icon, icon_circle, icon_pen, icon_rect, icon_select_tool
from .mg_shortcuts import (
    PLAY_PAUSE_ACTION_ID,
    PLAY_PAUSE_DEFAULT,
    configured_shortcut,
    event_matches,
    register_motion_graphic_shortcuts,
)

from ..core.commands import (
    AddElementCommand,
    AddPageCommand,
    CompositeCommand,
    DetachComponentInstanceCommand,
    InsertSceneFragmentCommand,
    MoveElementsToPageCommand,
    ReorderElementCommand,
    ReorderPageCommand,
    RemoveElementCommand,
    RemovePageCommand,
    SetCanvasSizeCommand,
    SetDurationCommand,
    SetHierarchyCommand,
    SetParentCommand,
    RegisterComponentMasterCommand,
    SetComponentVariantCommand,
    SetSceneMetadataCommand,
    SetSceneBackgroundCommand,
    UpdateElementCommand,
    UpdatePageCommand,
)
from ..core.html_layer_commands import (
    ApplyHtmlPatchCommand, ReplaceHtmlSourceCommand, SetHtmlLayerCommand,
    build_html_layer_from_source,
    SetHtmlPageTimingCommand,
)
from ..core.html_page_deck import safe_background_value, scene_background_css
from ..core.pages import page_frame_ranges
from ..core.html_template import HtmlTemplate, HtmlTemplateRegistry
from ..core.contracts import AnimationFrame, RenderContext, RenderedFrame
from ..core.model import SceneElement, ScenePage, SceneTransform, TeachingSceneDocument
from ..core.components import ComponentMaster, read_component_masters
from ..render.html_layer import apply_html_template, promote_html_layer
from ..render.html_layer.html_patch import HtmlPatch, HtmlPatchOperation
from ..render.scene_renderer import CancellationToken
from ..scene_store import TeachingSceneStore, get_shared_scene_store
from ..presets import (
    PresetError,
    extract_fragment,
    fragment_max_keyframe_frame,
    load_preset_scene,
    prepare_insert,
    retime_fragment_to_page,
    user_preset_root,
)
from .align import ALIGN_MODES, build_alignment_updates
from .asset_library_panel import AssetLibraryPanel, PresetSaveDialog
from .editor_controller import SceneDiff, SceneEditorController
from .html_browser_preview import HtmlBrowserPreview
from .html_devtools_panel import HtmlDevToolsPanel
from .html_inspector import HtmlInspector
from .html_source_editor import HtmlSourceEditor
from .layer_tree_model import HTML_LAYER_ROW_ID, page_display_name
from .layer_tree_panel import LayerTreePanel
from .mg_keyframes_panel import MgKeyframesPanel
from .scene_inspector import SceneInspector
from .scene_timeline import SceneTimeline
from .scene_view import SceneView
from .storyboard_panel import StoryboardPanel
from .page_timing_panel import PageTimingPanel
from .motion_graphic_style import MOTION_GRAPHIC_EDITOR_QSS

logger = logging.getLogger(__name__)

# AUD-30906: closeEvent must not wait forever for a hung browser backend.
_HTML_PREVIEW_STOP_TIMEOUT_MS = 5000


class _HtmlPreviewThread(QThread):
    """Render one immutable HTML scene snapshot away from the Qt UI thread."""

    frameReady = pyqtSignal(int, object)
    renderFailed = pyqtSignal(int, str)

    def __init__(self, scene: TeachingSceneDocument, frame: int, generation: int,
                 *, parent: Any = None) -> None:
        super().__init__(parent)
        self.scene = scene
        self.frame = int(frame)
        self.generation = int(generation)
        self.cancellation = CancellationToken()

    def requestInterruption(self) -> None:  # noqa: N802 — Qt API spelling
        # QThread's flag alone cannot interrupt a synchronous browser call.
        # The linked renderer token also reaches HtmlRendererService, which
        # lets the browser backend tear down the current capture promptly.
        self.cancellation.cancel()
        super().requestInterruption()

    def run(self) -> None:
        renderer: Any | None = None
        try:
            if self.cancellation.is_cancelled or self.isInterruptionRequested():
                return
            from ..render.scene_renderer_router import backend_for_scene, select_scene_renderer

            context = RenderContext(
                fps=self.scene.fps,
                width=self.scene.width,
                height=self.scene.height,
                backend=backend_for_scene(self.scene),
            )
            renderer = select_scene_renderer(self.scene, cancellation=self.cancellation)
            renderer.prepare(self.scene, context)
            frame = renderer.render_frame(AnimationFrame(self.frame, context.fps))
            if not self.cancellation.is_cancelled and not self.isInterruptionRequested():
                self.frameReady.emit(self.generation, frame)
        except Exception as exc:  # noqa: BLE001 — worker boundary reports renderer failures
            if not self.cancellation.is_cancelled and not self.isInterruptionRequested():
                self.renderFailed.emit(self.generation, str(exc))
        finally:
            if renderer is not None:
                try:
                    renderer.cleanup()
                except Exception:
                    # Cleanup is best-effort after a failed browser/process
                    # capture; the service itself owns crash replacement.
                    pass


# AUD-30903: element fields that a canvas-drag preview can change.  A diff
# limited to these fields leaves every panel (layer rows, keyframe tracks,
# storyboard, HTML overlay) invariant, so only the canvas has to re-render.
_TRANSFORM_PREVIEW_FIELDS = frozenset({"transform"})


class MotionGraphicEditorWindow(QMainWindow):
    """Owns one independently editable scene and can be moved across screens."""

    insertRequested = pyqtSignal(object)

    def __init__(self, scene: TeachingSceneDocument, *, parent: Any = None, registry: Any = None,
                 saver: Callable[[TeachingSceneDocument], None] | None = None,
                 scene_store: TeachingSceneStore | None = None,
                 insert_callback: Callable[[TeachingSceneDocument], None] | None = None,
                 render_cache_callback: Callable[[TeachingSceneDocument], str] | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("MotionGraphicEditor")
        self.setStyleSheet(MOTION_GRAPHIC_EDITOR_QSS)
        self.setWindowTitle(f"Motion Graphic — {scene.name}")
        self.setWindowModality(Qt.WindowModality.NonModal)
        # This is an editor owned by the project window, not a peer that may
        # slip behind it when a parameter refresh updates the host.  Qt's Tool
        # flag keeps it above its owner while remaining modeless and usable.
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._space_filter_installed = False
        self._front_restore_pending = False
        # AUD-30907：置前恢复改用父属单发定时器——窗口销毁后回调必不
        # 触发（PyQt6 静态 singleShot 无上下文对象重载）。
        self._reassert_front_timer = QTimer(self)
        self._reassert_front_timer.setSingleShot(True)
        self._reassert_front_timer.setInterval(50)
        self._reassert_front_timer.timeout.connect(
            self._restore_front_after_application_activate)
        self._front_restore_timer = QTimer(self)
        self._front_restore_timer.setSingleShot(True)
        self._front_restore_timer.setInterval(0)
        self._front_restore_timer.timeout.connect(
            self._restore_front_after_refresh)
        self._source_draft_pending = False
        self._devtools_draft_pending = False
        self._close_after_devtools_apply = False
        self._cache_status = ""
        self._registry = registry
        self._saver = saver
        self._insert_callback = insert_callback
        # The editor deliberately knows nothing about EditProject/encoders.
        # Its host supplies this bridge when the scene is opened from a
        # timeline, keeping the UI layer independent while still letting the
        # user bake the currently edited Motion Graphic into video cache.
        self._render_cache_callback = render_cache_callback
        self._scene_store = scene_store or get_shared_scene_store()
        self._scene_store.register(scene)
        self._scene_ids = [item.scene_id for item in self._scene_store.scenes()]
        if scene.scene_id not in self._scene_ids:
            self._scene_ids.append(scene.scene_id)
        self.editor_controller = SceneEditorController(scene)
        self.storyboard = StoryboardPanel(self)
        self.layer_tree = LayerTreePanel(self)
        self.scene_view = SceneView(self)
        self.html_browser_preview = HtmlBrowserPreview(self)
        self.inspector = SceneInspector(self)
        self.html_inspector = HtmlInspector(self)
        self.html_source_editor = HtmlSourceEditor(self)
        self.html_devtools = HtmlDevToolsPanel(self)
        self.asset_library = AssetLibraryPanel(self)
        # Source editing and live Chromium inspection are intentionally
        # independent top-level panels.  Nesting them under HTML hid their
        # purpose and made one click necessary before either was usable.
        self.page_timing = PageTimingPanel(self.html_inspector)
        self.html_inspector.add_persistent_section(self.page_timing)
        # Stacked inspector: structured element (default) vs Freeform HTML layer.
        self.inspector_stack = QStackedWidget(self)
        self.inspector_stack.addWidget(self.inspector)           # index 0
        self.inspector_stack.addWidget(self.html_inspector)      # index 1
        self.timeline = SceneTimeline(self)
        # Keyframes timeline (left element/property tree + right keyframe
        # tracks under one ruler/playhead).  The transport bar above it keeps
        # owning playback, page navigation and markers.
        self.keyframes_panel = MgKeyframesPanel(self)
        # Multi-select (phase-1): a frozenset of selected row ids (elements
        # and/or the synthetic HTML_LAYER_ROW_ID).  The window is the sole
        # selection owner; canvas / layer tree drive it, the inspector shows
        # the "primary" (first) element so its single-element form is reused.
        self._selected_elements: frozenset[str] = frozenset()
        # Drag-moves-all (phase-2): when a drag starts on an element that is
        # part of a multi-selection, the original (x, y) of every selected
        # element is stashed here so each mouse-move can shift them all by the
        # same delta.  Empty outside a drag.
        self._drag_anchors: dict[str, tuple[float, float]] = {}
        self._last_render_error: str | None = None
        # HTML preview is opt-in (the first Render Preview click enables live
        # scrub). Requests are coalesced to the newest immutable scene/frame;
        # stale browser results are ignored before touching Qt.
        self._html_preview_live = False
        self._html_preview_generation = 0
        self._html_preview_thread: _HtmlPreviewThread | None = None
        self._html_preview_pending: tuple[TeachingSceneDocument, int, int] | None = None
        self._html_preview_closing = False
        self._html_browser_active = False
        self._last_vertical_splitter_sizes: list[int] | None = None
        self._syncing_browser_page = False
        # Page the editor canvas is currently showing; derived from the frame
        # (pages partition the timeline) and refreshed by _refresh_scene.
        self._active_page_index = 0
        self._expanded_page_ids: frozenset[str] = frozenset(scene.page_ids)
        self._page_view: TeachingSceneDocument | None = None
        # FH-09: the editor includes the same disk-backed template library as
        # Effects, then adds templates promoted during this editor session.
        # This prevents two competing template pickers with different content.
        self._html_templates = HtmlTemplateRegistry()
        from ..core.motion_graphic_generator_registry import get_motion_graphic_generator_registry
        for template in get_motion_graphic_generator_registry().list_templates():
            self._html_templates.register(template)
        self._build_layout()
        self._connect_signals()
        # Actions before the first refresh: _refresh_scene sets their enabled
        # state from the scene, so they must exist first.
        self._create_actions()
        self._refresh_scene(scene)
        self._setup_customizable_shortcuts()

    def _setup_customizable_shortcuts(self) -> None:
        """Wire this editor's shortcut keys into the ShortcutManager.

        The Motion Graphic editor is a modeless Tool window, so its
        customizable shortcuts must never be registered as window-wide or
        application-wide QShortcuts — those steal keys from other windows
        (see src/shared/ui/shortcut_scope.py).  QAction-bound entries stay
        scoped to this editor's own window; the play/pause key is matched
        inside the event filter / keyPressEvent below.
        """
        from src.shared.ui.shortcut_manager import ShortcutManager
        self._apply_customizable_shortcuts()
        manager = ShortcutManager.instance()
        if manager is not None:
            # Re-register the definitions (idempotent) so the settings
            # dialog lists them even if the module-level registration did
            # not run, e.g. in tests constructing the editor directly.
            register_motion_graphic_shortcuts(manager)
            # PyQt severs the connection automatically when this window
            # (receiver context) is destroyed.
            manager.shortcuts_changed.connect(self._apply_customizable_shortcuts)

    def _apply_customizable_shortcuts(self) -> None:
        """Apply the currently configured sequences to this editor's actions."""
        for action, action_id, default in (
            (self._save_action, "motion_graphic/save_scene", "Ctrl+S"),
            (self._auto_key_action, "motion_graphic/auto_key", "K"),
            (self._undo_action, "motion_graphic/undo", "Ctrl+Z"),
            (self._redo_action, "motion_graphic/redo", "Ctrl+Shift+Z"),
        ):
            action.setShortcut(configured_shortcut(action_id, default))
        self.keyframes_panel.apply_play_shortcut()

    def _build_layout(self) -> None:
        # Compact production layout: one dominant preview on the left, one
        # inspector/DevTools column on the right, and one unified timeline
        # below. Playback controls dock into the keyframes toolbar; the ruler
        # header carries the seconds display beside the element column.
        attributes = QFrame(self)
        attributes.setObjectName("mgPanel")
        attributes_layout = QVBoxLayout(attributes)
        attributes_layout.setContentsMargins(5, 5, 5, 5)
        title = QLabel(I18N.tr("motion_graphic.panels.parameters", "Parameters"), attributes)
        title.setObjectName("mgSectionTitle")
        attributes_layout.addWidget(title)
        self._attributes_splitter = PanelSplitter(Qt.Orientation.Vertical, attributes)
        self._attributes_splitter.setObjectName("mgAttributesSplitter")
        self._attributes_splitter.addWidget(self.inspector_stack)
        self._attributes_splitter.addWidget(self.layer_tree)
        self._attributes_splitter.setStretchFactor(0, 3)
        self._attributes_splitter.setStretchFactor(1, 2)
        self._attributes_splitter.setSizes([440, 280])
        self.layer_tree.setMinimumHeight(150)
        attributes_layout.addWidget(self._attributes_splitter, 1)

        tabs = QTabWidget(self)
        tabs.setObjectName("mgPanelTabs")
        tabs.addTab(attributes, I18N.tr("motion_graphic.tabs.parameters", "Parameters"))
        tabs.addTab(self.storyboard, I18N.tr("motion_graphic.tabs.scenes", "Scenes"))
        tabs.addTab(self.html_source_editor, I18N.tr("motion_graphic.tabs.source", "Source"))
        tabs.addTab(self.html_devtools, I18N.tr("motion_graphic.tabs.developer_tools", "DevTools"))
        tabs.addTab(self.asset_library, I18N.tr("motion_graphic.tabs.library", "Library"))
        # The right side also hosts Chromium DevTools. Keep a useful default
        # width for Elements/Styles/Sources, while leaving the splitter fully
        # user-resizable on large displays (no maximum width clamp).
        tabs.setMinimumWidth(420)
        tabs.setElideMode(Qt.TextElideMode.ElideNone)
        tabs.setUsesScrollButtons(False)
        tabs.tabBar().setExpanding(True)
        self._panel_tabs = tabs

        preview = QFrame(self)
        preview.setObjectName("mgPreview")
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(6, 4, 6, 6)
        preview_header = QHBoxLayout()
        preview_header.setSpacing(2)
        preview_title = QLabel(I18N.tr("motion_graphic.panels.preview", "Preview"), preview)
        preview_title.setObjectName("mgSectionTitle")
        self._preview_info = QLabel(I18N.tr("motion_graphic.panels.waiting_for_render", "Waiting to render"), preview)
        self._preview_info.setObjectName("mgMutedLabel")
        preview_header.addWidget(preview_title)
        preview_header.addWidget(self._build_preview_tool_strip(preview))
        preview_header.addStretch(1)
        preview_header.addWidget(self._preview_info)
        preview_layout.addLayout(preview_header)
        self._preview_stack = QStackedWidget(preview)
        self._preview_stack.addWidget(self.scene_view)
        self._preview_stack.addWidget(self.html_browser_preview)
        preview_layout.addWidget(self._preview_stack, 1)
        self._preview_frame = preview

        horizontal = PanelSplitter(Qt.Orientation.Horizontal, self)
        horizontal.setObjectName("mgMainSplitter")
        horizontal.addWidget(preview)
        horizontal.addWidget(tabs)
        horizontal.setStretchFactor(0, 1)
        horizontal.setStretchFactor(1, 0)
        horizontal.setSizes([920, 520])
        self._main_splitter = horizontal

        timeline_panel = QFrame(self)
        timeline_panel.setObjectName("mgTimeline")
        timeline_layout = QVBoxLayout(timeline_panel)
        timeline_layout.setContentsMargins(0, 0, 0, 0)
        timeline_layout.setSpacing(0)
        # The compact SceneTimeline widget is retained as the playback/state
        # controller and is shown only when the full keyframes panel is hidden.
        timeline_layout.addWidget(self.timeline)
        # The bottom area grows into the keyframes timeline via the vertical
        # splitter handle; its visibility is toggled from the View menu.
        timeline_layout.addWidget(self.keyframes_panel, 1)

        vertical = PanelSplitter(Qt.Orientation.Vertical, self)
        vertical.setObjectName("mgVerticalSplitter")
        vertical.addWidget(horizontal)
        vertical.addWidget(timeline_panel)
        vertical.setStretchFactor(0, 1)
        vertical.setStretchFactor(1, 0)
        vertical.setSizes([640, 260])
        self._vertical_splitter = vertical
        self._timeline_panel = timeline_panel
        self.setCentralWidget(vertical)
        self.resize(1440, 860)

    def _build_preview_tool_strip(self, parent: QWidget) -> QWidget:
        """Create always-visible native drawing tools above the canvas.

        The buttons deliberately follow the main timeline toolbar's compact
        icon-only interaction: one active tool, orange active icon, and a
        tooltip for every command.  Clicking one directly changes the canvas
        tool, rather than opening a second menu.
        """
        strip = QWidget(parent)
        strip.setObjectName("mgPreviewToolStrip")
        layout = QHBoxLayout(strip)
        layout.setContentsMargins(5, 0, 0, 0)
        layout.setSpacing(1)
        self._preview_tool_group = QButtonGroup(strip)
        self._preview_tool_group.setExclusive(True)
        self._preview_tool_buttons: dict[str | None, QToolButton] = {}
        self._preview_tool_icons: dict[str | None, Callable[[str], object]] = {}

        # Keep the tool order familiar: select first, then the most common
        # primitives, with pen last.  Icons come from the shared manager.
        tools: tuple[tuple[str | None, str, str, Callable[[str], object]], ...] = (
            (None, "motion_graphic.layer.select_tool", "Select", icon_select_tool),
            ("rect", "motion_graphic.layer.new_rectangle", "Rectangle", icon_rect),
            ("rounded_rect", "motion_graphic.layer.new_rounded_rectangle", "Rounded Rectangle",
             lambda color: get_icon("fa5s.square-full", color)),
            ("ellipse", "motion_graphic.layer.new_ellipse", "Ellipse", icon_circle),
            ("line", "motion_graphic.layer.new_line", "Line", lambda color: get_icon("fa5s.minus", color)),
            ("arrow", "motion_graphic.layer.new_arrow", "Arrow",
             lambda color: get_icon("fa5s.long-arrow-alt-right", color)),
            ("triangle", "motion_graphic.layer.new_triangle", "Triangle", lambda color: get_icon("fa5s.play", color)),
            ("polygon", "motion_graphic.layer.new_polygon", "Polygon", lambda color: get_icon("fa5s.draw-polygon", color)),
            ("star", "motion_graphic.layer.new_star", "Star", lambda color: get_icon("fa5s.star", color)),
            ("path", "motion_graphic.layer.new_path", "Pen Path", icon_pen),
        )
        for kind, translation_key, fallback, icon_factory in tools:
            button = QToolButton(strip)
            button.setObjectName("mgPreviewToolButton")
            button.setToolTip(I18N.tr(translation_key, fallback))
            button.setFixedSize(26, 24)
            button.setIconSize(QSize(15, 15))
            button.setCheckable(True)
            button.clicked.connect(
                lambda _checked=False, shape_kind=kind: self._set_shape_draw_mode(shape_kind),
            )
            self._preview_tool_group.addButton(button)
            self._preview_tool_buttons[kind] = button
            self._preview_tool_icons[kind] = icon_factory
            layout.addWidget(button)
        self._sync_preview_tool_buttons(None)
        return strip

    def _connect_signals(self) -> None:
        self.editor_controller.subscribe(self._on_scene_notified)
        self.scene_view.elementSelected.connect(self._on_canvas_select)
        self.scene_view.elementSelectionToggled.connect(self._on_canvas_toggle)
        self.scene_view.elementDragged.connect(self._element_dragged)
        self.scene_view.elementDragStarted.connect(self._on_drag_started)
        self.scene_view.elementDragFinished.connect(self._on_drag_finished)
        self.scene_view.marqueeSelectionChanged.connect(self._on_canvas_marquee)
        self.scene_view.shapeDrawn.connect(self._draw_shape_layer)
        self.html_browser_preview.shapeDrawn.connect(self._draw_shape_layer)
        self.html_browser_preview.textEdited.connect(self._browser_text_edited)
        self.scene_view.pathDrawn.connect(self._draw_path_layer)
        self.scene_view.pathAnchorsChanged.connect(self._update_path_anchors)
        self.scene_view.elementEditRequested.connect(self._canvas_edit_requested)
        # Layer tree (UX-10): selection shares the same entry point as the canvas
        # so canvas / layer tree / inspector stay three-way synced.  The tree
        # drives multi-select (ExtendedSelection); the canvas plain-click
        # replaces and shift-click toggles.
        self.layer_tree.selectionChanged.connect(self._on_tree_selection)
        self.layer_tree.visibilityToggled.connect(lambda eid: self._toggle_hierarchy_flag(eid, "hidden"))
        self.layer_tree.lockToggled.connect(lambda eid: self._toggle_hierarchy_flag(eid, "locked"))
        self.layer_tree.soloToggled.connect(lambda eid: self._toggle_hierarchy_flag(eid, "solo"))
        self.layer_tree.removeRequested.connect(self._remove_element)
        self.layer_tree.reorderRequested.connect(self._reorder_element)
        self.layer_tree.reparentRequested.connect(self._reparent_element)
        self.layer_tree.unparentRequested.connect(self._unparent_element)
        self.layer_tree.createLayerRequested.connect(self._create_layer)
        self.layer_tree.pageCreateRequested.connect(self._add_page_requested)
        self.layer_tree.duplicateRequested.connect(self._duplicate_layer)
        self.layer_tree.pageMoveRequested.connect(self._move_elements_to_page)
        self.layer_tree.pageActivated.connect(self._jump_to_page_by_id)
        self.layer_tree.backgroundSelected.connect(self._background_row_selected)
        self.layer_tree.pageRenameRequested.connect(self._rename_page_requested)
        self.layer_tree.pageRemoveRequested.connect(self._remove_page_requested)
        self.layer_tree.pageDurationRequested.connect(self._page_duration_requested)
        self.layer_tree.pageReorderRequested.connect(self._reorder_page_requested)
        self.layer_tree.pageExpansionChanged.connect(
            lambda page_ids: self._set_page_expansion(page_ids, source="layers"),
        )
        self.inspector.elementUpdateRequested.connect(self._element_updated)
        self.inspector.selectionUpdateRequested.connect(self._selection_updated)
        self.inspector.keyframeRequested.connect(self._keyframe_requested)
        self.inspector.canvasSizeRequested.connect(self._canvas_size_requested)
        self.html_inspector.editRequested.connect(self._apply_html_edit)
        self.html_source_editor.sourceEditRequested.connect(self._apply_html_source)
        self.html_devtools.openRequested.connect(self._open_html_devtools)
        self.html_devtools.applyDomRequested.connect(self._apply_live_dom_to_html)
        self.html_devtools.discardDomRequested.connect(self._discard_live_dom_changes)
        self.html_devtools.draftChanged.connect(self._devtools_draft_changed)
        self.html_browser_preview.pageChanged.connect(self._browser_page_changed)
        self.html_browser_preview.elementSelected.connect(self._browser_element_selected)
        self.html_browser_preview.loaded.connect(self._browser_loaded)
        self.html_browser_preview.zoomChanged.connect(self._browser_zoom_changed)
        self.html_browser_preview.domMutated.connect(
            lambda: self.html_devtools.set_has_unapplied_changes(True),
        )
        self.timeline.frameChanged.connect(self._frame_changed)
        self.timeline.durationChangeRequested.connect(self._duration_changed)
        self.timeline.markerRequested.connect(lambda frame: self.editor_controller.add_marker(frame))
        self.timeline.markerJumpRequested.connect(self._page_frame_requested)
        self.timeline.markerRemoveRequested.connect(self.editor_controller.remove_marker)
        self.timeline.markersClearRequested.connect(self._confirm_clear_markers)
        self.timeline.markerRenameRequested.connect(self._rename_marker)
        # Keyframes timeline: scrubbing shares the transport's frame path, so
        # the slider, the ruler and Space playback all move one playhead.
        self.keyframes_panel.frame_changed.connect(self._frame_changed)
        self.keyframes_panel.element_selected.connect(
            lambda element_id: self._set_selection({element_id}, source="keyframes"))
        self.keyframes_panel.tracks_changed.connect(self._apply_keyframe_edits)
        self.keyframes_panel.pageDurationChangeRequested.connect(
            self._page_duration_frames_requested,
        )
        self.keyframes_panel.playbackToggleRequested.connect(self.timeline.toggle_play)
        self.keyframes_panel.pageExpansionChanged.connect(
            lambda page_ids: self._set_page_expansion(page_ids, source="keyframes"),
        )
        self.timeline.pageStepRequested.connect(self._request_relative_page)
        self.page_timing.frameRequested.connect(self._page_frame_requested)
        self.page_timing.settingsWithBackgroundRequested.connect(
            self._apply_html_page_settings_with_backgrounds,
        )
        self.html_source_editor.draftChanged.connect(self._source_draft_changed)
        self.storyboard.sceneSelected.connect(self._scene_selected)
        self.storyboard.createRequested.connect(self._create_scene)
        self.storyboard.duplicateRequested.connect(self._duplicate_scene)
        self.storyboard.deleteRequested.connect(self._delete_scene)
        self.storyboard.reorderRequested.connect(self._reorder_scene)
        # Asset library: panel activation inserts at the canvas centre, canvas
        # drops carry the exact drop point; both funnel into one undoable
        # insert command.
        self.asset_library.presetActivated.connect(self._insert_preset_at_center)
        self.asset_library.templateActivated.connect(self._apply_library_template)
        self.scene_view.presetDropped.connect(self._instantiate_preset_at)
        self.scene_view.templateDropped.connect(
            lambda template_id, _x, _y: self._apply_library_template(template_id))

    def _create_actions(self) -> None:
        # The main application exposes menus as compact toolbar buttons rather
        # than a native menu bar.  Keep the same interaction model here and
        # group editor actions by intent instead of rendering every command as
        # a separate top-level button.
        toolbar = QToolBar("Motion Graphic", self)
        toolbar.setObjectName("motionGraphicToolbar")
        toolbar.setMovable(False)
        self._editor_toolbar = toolbar
        self.addToolBar(toolbar)

        self._save_action = QAction(I18N.tr("motion_graphic.actions.save", "Save"), self)
        self._save_action.setShortcut("Ctrl+S")
        self._save_action.triggered.connect(self.save)

        self._insert_action = QAction(I18N.tr("motion_graphic.actions.add_to_timeline", "Add to Timeline"), self)
        self._insert_action.setToolTip(I18N.tr("motion_graphic.actions.add_to_timeline_tooltip", "Add the current scene to the main timeline"))
        self._insert_action.triggered.connect(self._insert_into_timeline)

        self._import_html_action = QAction(I18N.tr("motion_graphic.actions.import_html", "Import HTML…"), self)
        self._import_html_action.setToolTip(I18N.tr("motion_graphic.actions.import_html_tooltip", "Import a local HTML document into the current Motion Graphic clip"))
        self._import_html_action.triggered.connect(self._import_html_triggered)

        self._export_html_action = QAction(I18N.tr("motion_graphic.actions.export_html", "Export HTML…"), self)
        self._export_html_action.setToolTip(
            I18N.tr("motion_graphic.actions.export_html_tooltip", "Export the current sanitized HTML/CSS Motion Graphic document")
        )
        self._export_html_action.triggered.connect(self._export_html_triggered)

        # Auto-key (UX-14): when on, dragging an element at the current frame
        # writes transform keyframes, turning direct manipulation into animation.
        self._auto_key_action = QAction(I18N.tr("motion_graphic.actions.auto_key", "Auto Keyframe"), self)
        self._auto_key_action.setCheckable(True)
        self._auto_key_action.setChecked(False)
        self._auto_key_action.setShortcut("K")
        self._auto_key_action.setToolTip(I18N.tr("motion_graphic.actions.auto_key_tooltip", "Toggle Auto Keyframe: writes a keyframe at the current frame when a layer is dragged"))
        self._auto_key_action.toggled.connect(self.editor_controller.set_auto_key)

        # FH-09: distil the current HTML layer into a reusable template and
        # re-apply one later.  Disabled until there is something to promote /
        # a template to apply (refreshed in _refresh_scene).
        self._save_template_action = QAction(I18N.tr("motion_graphic.actions.save_template", "Save as Template"), self)
        self._save_template_action.setToolTip(I18N.tr("motion_graphic.actions.save_template_tooltip", "Save the current HTML layer as a reusable template"))
        self._save_template_action.triggered.connect(self._save_as_template_triggered)
        self._apply_template_action = QAction(I18N.tr("motion_graphic.actions.apply_template", "Apply Template…"), self)
        self._apply_template_action.setToolTip(I18N.tr("motion_graphic.actions.apply_template_tooltip", "Apply a saved template to the current scene"))
        self._apply_template_action.triggered.connect(self._apply_template_triggered)
        self._open_template_library_action = QAction(
            I18N.tr("motion_graphic.actions.open_template_library", "Open Template Library Folder"), self,
        )
        self._open_template_library_action.triggered.connect(self._open_template_library)
        self._refresh_template_library_action = QAction(
            I18N.tr("motion_graphic.actions.refresh_template_library", "Refresh Template Library"), self,
        )
        self._refresh_template_library_action.triggered.connect(self._refresh_template_library)

        # Preset library: structured scene fragments with GIF previews, browsed
        # and dragged in from the Library tab.  Saved presets land in the
        # user-writable library root as one folder per preset.
        self._save_preset_action = QAction(
            I18N.tr("motion_graphic.actions.save_preset", "Save as Preset…"), self)
        self._save_preset_action.setToolTip(I18N.tr(
            "motion_graphic.actions.save_preset_tooltip",
            "Save the selected layers (or the whole scene) as a reusable preset with a GIF preview"))
        self._save_preset_action.triggered.connect(self._save_preset_triggered)
        self._open_preset_library_action = QAction(
            I18N.tr("motion_graphic.actions.open_preset_library", "Open Preset Library Folder"), self)
        self._open_preset_library_action.triggered.connect(self._open_preset_library)

        # Multi-select align / distribute (UX-08 phase-3a).  The button is
        # retained as the state owner for selection enablement; the visible
        # commands are grouped into the Edit menu below.
        self._align_button = QToolButton(self)
        self._align_button.setObjectName("mgAlignButton")
        self._align_button.setText(I18N.tr("motion_graphic.actions.align", "Align"))
        self._align_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._align_button.setEnabled(False)
        align_menu = QMenu(self._align_button)
        for mode, label in (
            ("align_left", I18N.tr("motion_graphic.align.left", "Align Left")),
            ("align_center_h", I18N.tr("motion_graphic.align.center_h", "Center Horizontally")),
            ("align_right", I18N.tr("motion_graphic.align.right", "Align Right")),
            ("align_top", I18N.tr("motion_graphic.align.top", "Align Top")),
            ("align_middle_v", I18N.tr("motion_graphic.align.center_v", "Center Vertically")),
            ("align_bottom", I18N.tr("motion_graphic.align.bottom", "Align Bottom")),
            ("distribute_h", I18N.tr("motion_graphic.align.distribute_h", "Distribute Horizontally")),
            ("distribute_v", I18N.tr("motion_graphic.align.distribute_v", "Distribute Vertically")),
        ):
            act = QAction(label, self)
            act.triggered.connect(lambda checked=False, m=mode: self._align_selection(m))
            align_menu.addAction(act)
        self._align_button.setMenu(align_menu)
        # This object is kept as the selection-state owner for existing
        # editor logic/tests; the visible entry point is the grouped Edit menu.
        self._align_button.setVisible(False)

        # UX-13: component master creation and variant switching are explicit
        # command writes.  The scene remains the source of truth; dialogs only
        # collect names/variant ids and never mutate DOM or metadata directly.
        self._create_component_action = QAction(I18N.tr("motion_graphic.actions.create_component", "Create Component…"), self)
        self._create_component_action.triggered.connect(self._create_component_triggered)
        self._component_variant_action = QAction(I18N.tr("motion_graphic.actions.set_component_variant", "Set Component Variant…"), self)
        self._component_variant_action.triggered.connect(self._set_component_variant_triggered)
        self._detach_component_action = QAction(I18N.tr("motion_graphic.actions.detach_component", "Detach Component Instance"), self)
        self._detach_component_action.setToolTip(
            I18N.tr("motion_graphic.actions.detach_component_tooltip",
                    "Turn the selected component instance into plain layers, keeping its current look"))
        self._detach_component_action.triggered.connect(self._detach_component_instance)

        # HTML live preview: the first editor show starts a background capture;
        # this action remains available for an explicit refresh.
        self._render_preview_action = QAction(I18N.tr("motion_graphic.actions.render_preview", "Render Preview"), self)
        self._render_preview_action.setToolTip(
            I18N.tr("motion_graphic.actions.render_preview_tooltip", "Show the current HTML frame and pre-cache the complete Motion Graphic for smooth timeline playback")
        )
        self._render_preview_action.triggered.connect(self._render_preview)

        self._open_devtools_action = QAction(I18N.tr("motion_graphic.actions.developer_tools", "Developer Tools"), self)
        self._open_devtools_action.setToolTip(
            I18N.tr("motion_graphic.actions.developer_tools_tooltip", "Open Chromium DevTools: Elements / Console / Sources / Network")
        )
        self._open_devtools_action.triggered.connect(self._open_html_devtools)
        self._show_html_preview_action = QAction(I18N.tr("motion_graphic.actions.html_preview", "HTML Preview"), self)
        self._show_html_preview_action.triggered.connect(self._show_html_preview)

        self._render_video_cache_action = QAction(I18N.tr("motion_graphic.actions.render_video_cache", "Render Video Cache"), self)
        self._render_video_cache_action.setToolTip(
            I18N.tr("motion_graphic.actions.render_video_cache_tooltip", "Save the current scene and generate a background video cache for its timeline Motion Graphic clip")
        )
        self._render_video_cache_action.triggered.connect(self._render_video_cache)

        self._undo_action = QAction(I18N.tr("motion_graphic.actions.undo", "Undo"), self)
        self._undo_action.setShortcut("Ctrl+Z")
        self._undo_action.triggered.connect(self._undo)
        self._redo_action = QAction(I18N.tr("motion_graphic.actions.redo", "Redo"), self)
        self._redo_action.setShortcut("Ctrl+Shift+Z")
        self._redo_action.triggered.connect(self._redo)
        self.addAction(self._undo_action)
        self.addAction(self._redo_action)

        # Scene actions are routed through the same scene-store methods used by
        # the storyboard panel, so menu and panel interactions stay in sync.
        new_scene_action = QAction(I18N.tr("motion_graphic.storyboard.new", "New Scene"), self)
        new_scene_action.triggered.connect(self._create_scene)
        duplicate_scene_action = QAction(I18N.tr("motion_graphic.storyboard.duplicate", "Duplicate Scene"), self)
        duplicate_scene_action.triggered.connect(
            lambda checked=False: self._duplicate_scene(self.editor_controller.scene.scene_id))
        delete_scene_action = QAction(I18N.tr("motion_graphic.storyboard.delete", "Delete Scene"), self)
        delete_scene_action.triggered.connect(
            lambda checked=False: self._delete_scene(self.editor_controller.scene.scene_id))

        # Build project-style popup menus.
        file_menu = QMenu(I18N.tr("motion_graphic.menu.file", "File"), self)
        file_menu.addAction(self._save_action)
        file_menu.addAction(self._import_html_action)
        file_menu.addAction(self._insert_action)
        file_menu.addSeparator()
        file_menu.addAction(self._save_template_action)
        file_menu.addAction(self._apply_template_action)
        file_menu.addAction(self._open_template_library_action)
        file_menu.addAction(self._refresh_template_library_action)
        file_menu.addSeparator()
        file_menu.addAction(self._save_preset_action)
        file_menu.addAction(self._open_preset_library_action)

        self._copy_style_action = QAction(I18N.tr("motion_graphic.actions.copy_style", "Copy Style"), self)
        self._copy_style_action.triggered.connect(self._copy_style)
        self._paste_style_action = QAction(I18N.tr("motion_graphic.actions.paste_style", "Paste Style"), self)
        self._paste_style_action.triggered.connect(self._paste_style)
        self._style_clipboard: dict | None = None

        edit_menu = QMenu(I18N.tr("motion_graphic.menu.edit", "Edit"), self)
        edit_menu.addAction(self._undo_action)
        edit_menu.addAction(self._redo_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self._copy_style_action)
        edit_menu.addAction(self._paste_style_action)
        edit_menu.addSeparator()
        edit_menu.addAction(self._auto_key_action)
        self._align_menu_action = edit_menu.addMenu(align_menu)
        self._align_menu_action.setEnabled(False)

        scene_menu = QMenu(I18N.tr("motion_graphic.menu.scene", "Scene"), self)
        scene_menu.addAction(new_scene_action)
        scene_menu.addAction(duplicate_scene_action)
        scene_menu.addAction(delete_scene_action)
        scene_menu.addSeparator()
        scene_menu.addAction(self._create_component_action)
        scene_menu.addAction(self._component_variant_action)
        scene_menu.addAction(self._detach_component_action)
        scene_menu.addSeparator()
        self._theme_menu = QMenu(I18N.tr("motion_graphic.menu.theme", "Theme"), self)
        scene_menu.addMenu(self._theme_menu)
        self._theme_menu.aboutToShow.connect(self._rebuild_theme_menu)

        view_menu = QMenu(I18N.tr("motion_graphic.menu.view", "View"), self)
        parameters_action = QAction(I18N.tr("motion_graphic.view.parameters", "Parameters Panel"), self)
        parameters_action.triggered.connect(lambda checked=False: self._show_right_panel_tab(0))
        view_menu.addAction(parameters_action)
        layers_action = QAction(I18N.tr("motion_graphic.view.layers", "Layers Panel"), self)
        layers_action.triggered.connect(lambda checked=False: self._show_layers_panel())
        view_menu.addAction(layers_action)
        # Keyframes timeline toggle: the bottom area swaps between the compact
        # transport bar and the full element/property keyframes panel.
        self._keyframes_panel_action = QAction(
            I18N.tr("motion_graphic.view.keyframes", "Keyframes Timeline"), self)
        self._keyframes_panel_action.setCheckable(True)
        self._keyframes_panel_action.setChecked(True)
        self._keyframes_panel_action.toggled.connect(self._set_keyframes_panel_visible)
        view_menu.addAction(self._keyframes_panel_action)
        for label, index in (
            (I18N.tr("motion_graphic.view.scenes", "Scenes Panel"), 1),
            (I18N.tr("motion_graphic.tabs.source", "Source"), 2),
            (I18N.tr("motion_graphic.tabs.developer_tools", "DevTools"), 3),
            (I18N.tr("motion_graphic.tabs.library", "Library"), 4),
        ):
            action = QAction(label, self)
            action.triggered.connect(
                lambda checked=False, i=index: self._show_right_panel_tab(i))
            view_menu.addAction(action)
        view_menu.addSeparator()
        view_menu.addAction(self._open_devtools_action)
        view_menu.addAction(self._show_html_preview_action)
        view_menu.addSeparator()
        fit_action = QAction(I18N.tr("motion_graphic.actions.fit_preview", "Fit Preview"), self)
        fit_action.triggered.connect(lambda checked=False: self._fit_preview_to_view())
        view_menu.addAction(fit_action)

        self._cache_quality_group = None
        self._cache_quality_menu = QMenu(I18N.tr("motion_graphic.menu.cache_quality", "Cache Quality"), self)
        from ..cache_quality import get_cache_quality
        self._cache_quality_actions: dict[str, QAction] = {}
        selected_quality = get_cache_quality()
        for tier, label in (
            ("proxy", I18N.tr("motion_graphic.cache.proxy", "Proxy (8 Mbps · fast)")),
            ("balanced", I18N.tr("motion_graphic.cache.balanced", "Balanced (20 Mbps)")),
            ("high_quality", I18N.tr("motion_graphic.cache.high_quality", "High Quality (40 Mbps · default)")),
        ):
            action = QAction(label, self._cache_quality_menu)
            action.setCheckable(True)
            action.setChecked(tier == selected_quality)
            action.triggered.connect(
                lambda checked=False, t=tier: self._set_cache_quality(t))
            self._cache_quality_menu.addAction(action)
            self._cache_quality_actions[tier] = action

        preview_menu = QMenu(I18N.tr("motion_graphic.menu.preview", "Preview"), self)
        preview_menu.addMenu(self._cache_quality_menu)
        preview_menu.addAction(self._render_preview_action)
        preview_menu.addAction(self._open_devtools_action)
        preview_menu.addAction(self._show_html_preview_action)
        preview_menu.addAction(self._render_video_cache_action)
        play_action = QAction(I18N.tr("motion_graphic.actions.play_stop", "Play / Stop"), self)
        play_action.triggered.connect(lambda checked=False: self.timeline.toggle_play())
        preview_menu.addAction(play_action)

        export_menu = QMenu(I18N.tr("motion_graphic.menu.export", "Export"), self)
        export_menu.addAction(self._export_html_action)

        menus = (
            (I18N.tr("motion_graphic.menu.file", "File"), file_menu),
            (I18N.tr("motion_graphic.menu.edit", "Edit"), edit_menu),
            (I18N.tr("motion_graphic.menu.scene", "Scene"), scene_menu),
            (I18N.tr("motion_graphic.menu.view", "View"), view_menu),
            (I18N.tr("motion_graphic.menu.preview", "Preview"), preview_menu),
            (I18N.tr("motion_graphic.menu.export", "Export"), export_menu),
        )
        for label, menu in menus:
            button = QToolButton(self)
            button.setObjectName("mgMenuButton")
            button.setText(label)
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            toolbar.addWidget(button)

        # Native vector tools live in the Preview header, where they remain
        # visible beside the canvas instead of being hidden in a menu.

    def _show_right_panel_tab(self, index: int) -> None:
        """Reveal a right-side tab even after the splitter was dragged shut."""
        self._panel_tabs.setCurrentIndex(index)
        self._panel_tabs.show()
        sizes = self._main_splitter.sizes()
        if len(sizes) != 2 or sizes[1] >= 320:
            return
        total = max(sum(sizes), self._main_splitter.width(), 960)
        panel_width = min(520, max(420, total // 3))
        self._main_splitter.setSizes([max(1, total - panel_width), panel_width])

    def _show_layers_panel(self) -> None:
        """Reveal the lower Layers area without separating it from Parameters."""
        self._show_right_panel_tab(0)
        sizes = self._attributes_splitter.sizes()
        if len(sizes) == 2 and sizes[1] < 150:
            total = max(sum(sizes), self._attributes_splitter.height(), 430)
            layer_height = min(max(190, total // 3), max(190, total - 180))
            self._attributes_splitter.setSizes([max(180, total - layer_height), layer_height])
        self.layer_tree.list_widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def _insert_into_timeline(self) -> None:
        scene = self.editor_controller.scene
        self.insertRequested.emit(scene)
        if self._insert_callback is not None:
            self._insert_callback(scene)

    def _on_scene_notified(self, scene: TeachingSceneDocument, diff: SceneDiff) -> None:
        """Route controller notifications by what actually changed (AUD-30903).

        Direct-manipulation previews (canvas drag) fire one notification per
        mouse move.  While a drag is active and the diff only carries element
        transforms, the layer rows, keyframe tracks, storyboard and HTML
        overlay are transform-invariant — rebuilding them per mouse tick is
        wasted O(elements) work, so only the canvas re-renders.  Everything
        else (command commits, undo/redo, duration/page/animation/marker
        edits) gets the full refresh as before.  Note that SceneDiff does not
        track markers/metadata, so an "empty" diff still takes the full path.
        """
        if (
            self._drag_anchors
            and scene.html_layer is None
            and not diff.duration_changed
            and not diff.pages_changed
            and not diff.animation_changed
            and diff.changed_element_ids
            and all(
                eid in diff.changed_fields
                and diff.changed_fields[eid] <= _TRANSFORM_PREVIEW_FIELDS
                for eid in diff.changed_element_ids
            )
        ):
            self._refresh_transform_preview(scene)
            return
        self._refresh_scene(scene)

    def _refresh_transform_preview(self, scene: TeachingSceneDocument) -> None:
        """Light drag refresh: keep the store current and re-render the canvas."""
        self._scene_store.register(scene)
        self._active_page_index = scene.page_index_for_frame(
            self.editor_controller.current_frame)
        self._page_view = (scene.limited_to_page(scene.page_ids[self._active_page_index])
                           if len(scene.pages) > 1 else scene)
        self.scene_view.set_scene(self._page_view)
        try:
            self.scene_view.set_frame(self.editor_controller.render(
                page_id=(scene.page_ids[self._active_page_index]
                         if len(scene.pages) > 1 else None)))
        except Exception as exc:  # noqa: BLE001 — a preview failure must never crash the editor
            self.statusBar().showMessage(f"Preview error: {exc}")

    def _refresh_scene(self, scene: TeachingSceneDocument) -> None:
        self._scene_store.register(scene)
        scene_map = {item.scene_id: item for item in self._scene_store.scenes()}
        ordered_ids = [scene_id for scene_id in self._scene_ids if scene_id in scene_map]
        ordered_ids.extend(scene_id for scene_id in scene_map if scene_id not in ordered_ids)
        self._scene_ids = ordered_ids
        scenes = [scene_map[scene_id] for scene_id in ordered_ids]
        # Page-scoped canvas: the editor shows one artboard at a time.  The
        # scene view receives the page-limited view for rendering and hit
        # testing; every panel below keeps reading the full document.
        self._active_page_index = scene.page_index_for_frame(
            self.editor_controller.current_frame)
        self._page_view = (scene.limited_to_page(scene.page_ids[self._active_page_index])
                           if len(scene.pages) > 1 else scene)
        self._expanded_page_ids = frozenset(scene.page_ids) & self._expanded_page_ids
        self.storyboard.set_scenes(tuple(scenes), selected_scene_id=scene.scene_id)
        self.scene_view.set_scene(self._page_view)
        self.layer_tree.set_scene(
            scene,
            active_page_id=scene.page_ids[self._active_page_index],
            expanded_page_ids=self._expanded_page_ids,
        )
        self.timeline.set_scene(scene)
        self.keyframes_panel.set_scene(
            scene,
            selected_ids=self._selected_elements,
            expanded_page_ids=self._expanded_page_ids,
        )
        self.inspector.set_scene_canvas(scene.width, scene.height, scene.metadata)
        self._sync_timeline_host()
        self.page_timing.set_scene(scene)
        self.html_source_editor.set_layer(scene.html_layer)
        self.html_devtools.set_enabled_for_document(scene.html_layer is not None)
        self._open_devtools_action.setEnabled(scene.html_layer is not None)
        self._show_html_preview_action.setEnabled(scene.html_layer is not None)
        self._panel_tabs.setTabEnabled(2, scene.html_layer is not None)
        self._panel_tabs.setTabEnabled(3, scene.html_layer is not None)
        if scene.html_layer is not None:
            from ..core.design_tokens import read_scene_tokens
            from ..core.html_page_deck import detect_page_count, page_timing_for
            from ..core.hierarchy import read_hierarchy
            from ..render.html_native_overlay import (
        native_animation_css, native_overlay_css_variables, native_overlay_markup)
            page_count = detect_page_count(scene.html_layer.sanitized_html)
            timing = page_timing_for(
                getattr(scene.html_layer, "metadata", {}),
                page_count=page_count,
                fallback_default_seconds=max(0.001, scene.duration_frames / scene.fps) / page_count,
            )
            self.html_browser_preview.set_document(
                scene.html_layer.sanitized_html,
                timing,
                viewport_width=getattr(scene.html_layer, "viewport_width", 0) or scene.width,
                viewport_height=getattr(scene.html_layer, "viewport_height", 0) or scene.height,
                global_background=scene_background_css(scene.background),
                page_backgrounds=tuple(page.background for page in scene.pages),
            )
            tokens = read_scene_tokens(scene.metadata)
            token_css = native_overlay_css_variables(tokens)
            animation_css = native_animation_css(scene)
            markup = native_overlay_markup(
                self._page_view.elements, read_hierarchy(scene.metadata).parents, tokens)
            generated = (
                (f"<style>{token_css}</style>" if token_css else "")
                + (f"<style>{animation_css}</style>" if animation_css else "")
                + markup
            )
            self.html_browser_preview.set_native_overlay(generated)
            self.html_browser_preview.set_native_time(
                self.editor_controller.current_frame / max(0.001, scene.fps))
            # The reloaded deck starts at page 0; mirror the playhead's page so
            # the browser canvas matches the page-scoped editor view.
            if self.html_browser_preview.current_page != self._active_page_index:
                self._syncing_browser_page = True
                try:
                    self.html_browser_preview.set_page(self._active_page_index)
                finally:
                    self._syncing_browser_page = False
            self.html_source_editor.set_generated_output(generated)
            if self._html_browser_active:
                # DevTools lives in the right inspector tabs; the center always
                # remains the interactive HTML canvas while it is open.
                self._preview_stack.setCurrentWidget(self.html_browser_preview)
            else:
                self._preview_stack.setCurrentWidget(self.scene_view)
        else:
            self._html_browser_active = False
            self._preview_stack.setCurrentWidget(self.scene_view)
        duration_seconds = scene.duration_frames / max(scene.fps, 0.001)
        preview_info = f"{scene.width} × {scene.height}  ·  {scene.fps:g} fps  ·  {duration_seconds:g} s"
        self._preview_info.setText(
            f"{preview_info}  ·  {self._cache_status}" if self._cache_status else preview_info,
        )
        # Refresh whichever inspector is active: the HTML inspector always
        # tracks the live layer; the element inspector shows the primary
        # selected element (the HTML-layer row is handled by _set_selection).
        self.html_inspector.set_layer(scene.html_layer)
        # HTML scenes open on the unified Parameters panel.  The HTML-layer
        # inspector and page timing controls are presented together there,
        # instead of forcing users through another tab to make an edit.
        if scene.html_layer is not None and not self._selected_elements:
            self._set_selection({HTML_LAYER_ROW_ID}, source="bootstrap")
            self._show_right_panel_tab(0)
        if HTML_LAYER_ROW_ID not in self._selected_elements:
            from ..core.design_tokens import read_scene_tokens
            selected_elements = tuple(
                element for element_id in self._selected_elements
                if (element := scene.element_by_id(element_id)) is not None
            )
            self.inspector.set_selection(
                selected_elements, tokens=read_scene_tokens(scene.metadata), scene=scene)
        try:
            if scene.html_layer is None:
                # SVG scenes render fast and synchronously on every refresh.
                self.scene_view.set_frame(self.editor_controller.render(
                    page_id=(scene.page_ids[self._active_page_index]
                             if len(scene.pages) > 1 else None)))
            # In a visible desktop editor, HTML uses the interactive browser
            # surface above and never waits for a rendered-video screenshot on
            # a page click.  Headless/offscreen environments use the pinned
            # capture fallback, coalescing the newest frame in the worker.
            elif self._html_preview_live:
                self._request_html_preview(self._page_view, self.editor_controller.current_frame)
            elif self.isVisible():
                # A newly selected HTML scene should behave like the initial
                # editor view: show its current frame immediately instead of
                # waiting for the user to discover a separate preview action.
                self._html_preview_live = True
                self._request_html_preview(self._page_view, self.editor_controller.current_frame)
        except Exception as exc:  # noqa: BLE001 — a preview failure must never crash the editor
            # SVG scenes raise ValueError on unexpected pixel formats; HTML scenes
            # raise HtmlRenderError when the pinned backend is unavailable.  Either
            # way the editor stays usable (the inspector + layer tree + timeline
            # still work) and the status bar explains the missing preview.
            self._last_render_error = str(exc)
            self.statusBar().showMessage(f"Preview error: {exc}")
        self._update_window_title()
        # FH-09 action enablement: promote needs an HTML layer; apply needs a
        # saved template in the window registry.  HTML preview is on-demand
        # (only HTML scenes capture via a browser); SVG scenes refresh inline.
        has_layer = scene.html_layer is not None
        # Native layers now composite over browser-rendered HTML, so drawing
        # stays available for HTML templates such as the particle background.
        for button in self._preview_tool_buttons.values():
            button.setEnabled(True)
        self._render_preview_action.setEnabled(has_layer)
        self._render_video_cache_action.setEnabled(self._render_cache_callback is not None)
        self._export_html_action.setEnabled(has_layer)
        has_templates = bool(self._html_templates.list_templates())
        self._save_template_action.setEnabled(has_layer)
        self._apply_template_action.setEnabled(has_templates)
        has_variants = any(master.variants for master in read_component_masters(scene.metadata))
        self._component_variant_action.setEnabled(has_variants)
        self._refresh_selection_actions()
        self._schedule_front_restore()

    @staticmethod
    def _is_text_editing_focus(widget: QWidget | None) -> bool:
        """Keep Space available for typing when a text/value editor has focus."""
        return isinstance(
            widget,
            (QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QTextEdit),
        )

    def _space_context_active(self) -> bool:
        """Return whether Space should belong to this visible editor window."""
        focus = QApplication.focusWidget()
        focus_in_editor = focus is not None and (
            focus is self
            or self.isAncestorOf(focus)
            or focus.window() is self
        )
        cursor_pos = QCursor.pos()
        cursor_widget = QApplication.widgetAt(cursor_pos)
        cursor_in_editor = cursor_widget is not None and (
            cursor_widget is self or self.isAncestorOf(cursor_widget)
        )
        # ``widgetAt`` can return None for a native WebEngine/Tool child on
        # macOS; the top-level geometry remains a reliable pointer fallback.
        cursor_in_editor = cursor_in_editor or self.frameGeometry().contains(cursor_pos)
        return bool(
            focus_in_editor
            or QApplication.activeWindow() is self
            or self.isActiveWindow()
            or cursor_in_editor
        )

    def _toggle_playback_from_space(self) -> bool:
        if self._is_text_editing_focus(QApplication.focusWidget()):
            return False
        self.timeline.toggle_play()
        return True

    def handle_space_playback_shortcut(self) -> bool:
        """Handle Space when the host application's global filter sees it."""
        if not self.isVisible() or not self._space_context_active():
            return False
        return self._toggle_playback_from_space()

    def _source_draft_changed(self, pending: bool) -> None:
        """Keep an unapplied Source draft visible in the editor's dirty cue."""
        self._source_draft_pending = bool(pending)
        self._update_window_title()

    def _devtools_draft_changed(self, pending: bool) -> None:
        self._devtools_draft_pending = bool(pending)
        self._update_window_title()

    def _update_window_title(self) -> None:
        scene = self.editor_controller.scene
        dirty = (
            self.editor_controller.is_dirty
            or self._source_draft_pending
            or self._devtools_draft_pending
        )
        self.setWindowTitle(f"Motion Graphic — {scene.name}{'*' if dirty else ''}")

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 — Qt API spelling
        """Handle Space before a canvas, slider or browser consumes it."""
        if (
            event.type() == QEvent.Type.ApplicationStateChange
            and self.isVisible()
            and QApplication.applicationState() == Qt.ApplicationState.ApplicationActive
        ):
            # macOS may reactivate the project owner before its modeless child
            # after an app switch.  Reassert this editor after Qt completes
            # that activation turn, without blocking or changing scene state.
            self._reassert_front_timer.start()
        if (
            event.type() == QEvent.Type.KeyPress
            and self.isVisible()
            and event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)
        ):
            # A modeless Tool window can report an owner/native child instead
            # of itself as active on macOS.  Centralize the context check so
            # the host application's global filter uses the exact same rule.
            # The play key itself is user-customizable (motion_graphic/
            # play_pause) and matched here instead of via a cross-window
            # QShortcut, which would swallow the key before any filter runs.
            in_editor = self._space_context_active()
            if in_editor and (event.isAutoRepeat() or self._toggle_playback_from_space()):
                event.accept()
                return True
        if (
            event.type() == QEvent.Type.KeyPress
            and self.isVisible()
            and self.isActiveWindow()
            and event.key() == Qt.Key.Key_Escape
            and event.modifiers() == Qt.KeyboardModifier.NoModifier
            and not event.isAutoRepeat()
            and not self._is_text_editing_focus(QApplication.focusWidget())
            and (self.scene_view.draw_shape_kind is not None
                 or self.html_browser_preview.native_draw_tool is not None)
        ):
            # Escape leaves any draw tool and returns to selection.  Cancelling
            # an in-progress pen gesture first is handled by set_draw_shape_kind
            # clearing the gesture state in both canvases.
            self._set_shape_draw_mode(None)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def _schedule_front_restore(self) -> None:
        """Coalesce refreshes while preserving this tool above its project owner."""
        if not self.isVisible() or self._front_restore_pending:
            return
        self._front_restore_pending = True
        self._front_restore_timer.start()

    def _restore_front_after_refresh(self) -> None:
        self._front_restore_pending = False
        if self.isVisible():
            self.raise_()

    def _restore_front_after_application_activate(self) -> None:
        if self.isVisible() and QApplication.applicationState() == Qt.ApplicationState.ApplicationActive:
            self.raise_()
            self.activateWindow()

    def _page_frame_requested(self, frame: int) -> None:
        """Jump the timeline and preview to a page boundary."""
        self.timeline.set_frame(int(frame))
        self._frame_changed(int(frame))

    def _request_relative_page(self, step: int) -> None:
        """Timeline transport: move directly between page boundaries."""
        scene = self.editor_controller.scene
        if len(scene.pages) < 2 or not step:
            return
        current = scene.page_index_for_frame(self.editor_controller.current_frame)
        target = max(0, min(
            len(scene.pages) - 1,
            current + (1 if step > 0 else -1),
        ))
        if target != current:
            self._page_frame_requested(scene.page_start_frames()[target])

    def _jump_to_page_by_id(self, page_id: str) -> None:
        """Layer-tree page-header click: seek the playhead to the page start."""
        scene = self.editor_controller.scene
        if str(page_id) not in scene.page_ids:
            return
        start = scene.page_start_frames()[scene.page_ids.index(str(page_id))]
        self._page_frame_requested(start)

    def _move_elements_to_page(self, element_id: str, page_id: str) -> None:
        """Layer-tree cross-page drop: one undoable re-page (frames take children)."""
        scene = self.editor_controller.scene
        if str(page_id) not in scene.page_ids or scene.element_by_id(str(element_id)) is None:
            return
        try:
            self.editor_controller.execute(
                MoveElementsToPageCommand((str(element_id),), str(page_id)))
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))

    def _set_page_expansion(self, page_ids: tuple[str, ...], *, source: str) -> None:
        """Keep Layers and Keyframes on one shared page expansion state."""
        scene = self.editor_controller.scene
        self._expanded_page_ids = frozenset(page_ids) & frozenset(scene.page_ids)
        if source != "layers":
            self.layer_tree.set_expanded_pages(self._expanded_page_ids)
        if source != "keyframes":
            self.keyframes_panel.set_expanded_pages(self._expanded_page_ids)

    # ── page management (pages are first-class; every edit is undoable) ─────

    def _add_page_requested(self) -> None:
        scene = self.editor_controller.scene
        index = len(scene.pages)
        page = ScenePage(
            page_id=f"page_{uuid.uuid4().hex[:8]}",
            duration_frames=max(1, round(scene.fps * 3.0)),
        )
        self.editor_controller.execute(AddPageCommand(page))
        self._expanded_page_ids = self._expanded_page_ids | {page.page_id}
        self.layer_tree.set_expanded_pages(self._expanded_page_ids)
        self.keyframes_panel.set_expanded_pages(self._expanded_page_ids)
        starts = self.editor_controller.scene.page_start_frames()
        self._page_frame_requested(starts[min(index, len(starts) - 1)])

    def _remove_page_requested(self, page_id: str) -> None:
        scene = self.editor_controller.scene
        if len(scene.pages) <= 1:
            # The model refuses to drop the last page.  Reject the delete with
            # a visible message instead of letting the model's ValueError
            # escape into a Qt slot — an unhandled slot exception aborts the
            # process.
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.pages.keep_last_page",
                        "A scene must keep at least one page."), 5000)
            return
        try:
            self.editor_controller.execute(RemovePageCommand(str(page_id)))
        except ValueError as exc:
            # Model-level guard raced the UI (page vanished, scene swapped):
            # surface it like _move_elements_to_page instead of aborting.
            self.statusBar().showMessage(str(exc), 5000)
            return
        frame = min(self.editor_controller.current_frame,
                    self.editor_controller.scene.duration_frames - 1)
        self._frame_changed(frame)

    def _rename_page_requested(self, page_id: str) -> None:
        scene = self.editor_controller.scene
        page = scene.page_by_id(str(page_id))
        if page is None:
            return
        name, accepted = QInputDialog.getText(
            self,
            I18N.tr("motion_graphic.pages.rename", "Rename Page…"),
            I18N.tr("motion_graphic.pages.rename_prompt", "Page name:"),
            text=page.name or page_display_name(page, scene.page_ids.index(page_id)),
        )
        if accepted and name.strip():
            self.editor_controller.execute(
                UpdatePageCommand(str(page_id), {"name": name.strip()}))

    def _page_duration_requested(self, page_id: str) -> None:
        scene = self.editor_controller.scene
        page = scene.page_by_id(str(page_id))
        if page is None:
            return
        frames, accepted = QInputDialog.getInt(
            self,
            I18N.tr("motion_graphic.pages.duration", "Set Page Duration…"),
            I18N.tr("motion_graphic.pages.duration_prompt",
                    "Duration in frames ({fps:g} fps):", fps=scene.fps),
            page.duration_frames, 1, 36000,
        )
        if accepted and frames != page.duration_frames:
            self.editor_controller.execute(
                UpdatePageCommand(str(page_id), {"duration_frames": frames}))

    def _page_duration_frames_requested(self, page_id: str, duration_frames: int) -> None:
        """Commit a page-edge drag as one undoable duration edit."""
        scene = self.editor_controller.scene
        page = scene.page_by_id(str(page_id))
        if page is None:
            return
        frames = max(1, int(duration_frames))
        if frames != page.duration_frames:
            self.editor_controller.execute(
                UpdatePageCommand(str(page_id), {"duration_frames": frames}))

    def _reorder_page_requested(self, page_id: str, index: int) -> None:
        scene = self.editor_controller.scene
        if str(page_id) not in scene.page_ids:
            return
        self.editor_controller.execute(
            ReorderPageCommand(str(page_id), max(0, min(int(index), len(scene.pages) - 1))))

    def _confirm_clear_markers(self) -> None:
        """Removing every marker is destructive: confirm, then stay undoable."""
        count = len(self.editor_controller.scene.markers)
        if count == 0:
            return
        confirmed = QMessageBox.question(
            self,
            I18N.tr("motion_graphic.transport.clear_markers", "Remove All Markers"),
            I18N.tr(
                "motion_graphic.transport.clear_markers_confirm",
                "Remove all {count} markers? This can be undone.",
                count=count,
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed == QMessageBox.StandardButton.Yes:
            self.editor_controller.clear_markers()

    def _rename_marker(self, marker_id: str, current_label: str) -> None:
        """Request a concise marker label without exposing model IDs in UI."""
        label, accepted = QInputDialog.getText(
            self,
            I18N.tr("motion_graphic.transport.rename_marker", "Rename Marker"),
            I18N.tr("motion_graphic.transport.marker_name", "Marker name"),
            text=str(current_label),
        )
        if accepted and str(label).strip():
            self.editor_controller.rename_marker(marker_id, str(label).strip())

    def _browser_page_changed(self, index: int) -> None:
        """Mirror browser keyboard navigation into the editor transport."""
        if not self._html_browser_active or self._syncing_browser_page:
            return
        scene = self.editor_controller.scene
        if scene.html_layer is None or not scene.pages:
            return
        page = max(0, min(int(index), len(scene.pages) - 1))
        frame = scene.page_start_frames()[page]
        frame = max(0, min(scene.duration_frames - 1, frame))
        self._syncing_browser_page = True
        try:
            self.editor_controller.set_frame(frame)
            self.timeline.set_frame(frame)
            self.keyframes_panel.set_current_frame(frame)
            self.inspector.set_frame(frame)
        finally:
            self._syncing_browser_page = False

    def _background_row_selected(self, page_id: str) -> None:
        """Open the background controls when a synthetic background row is clicked."""
        scene = self.editor_controller.scene
        if scene.html_layer is not None:
            self.inspector_stack.setCurrentIndex(1)
            self.html_inspector.set_layer(scene.html_layer)
            self._show_right_panel_tab(0)
        if page_id in scene.page_ids:
            index = scene.page_ids.index(page_id)
            self._page_frame_requested(scene.page_start_frames()[index])

    def _browser_element_selected(self, node_id: str) -> None:
        """Select the clicked DOM node through the shared selection chokepoint.

        Native overlay clicks carry the ``SceneElement`` id, so they select the
        same layer as clicking its row in Layers.  Authored editable slots keep
        the HTML-layer row selected and reveal the node in the HTML inspector.
        """
        if not node_id:
            return
        if self.editor_controller.scene.element_by_id(node_id) is not None:
            self._set_selection({node_id}, source="browser")
            return
        self._set_selection({HTML_LAYER_ROW_ID}, source="browser")
        self.html_inspector.select_node(node_id)
        self.html_browser_preview.select_node(node_id)

    def _set_cache_quality(self, tier: str) -> None:
        """Select the Motion Graphic video-cache delivery tier (WP-8)."""
        from ..cache_quality import set_cache_quality
        from src.shared.media.motion_graphic_delivery import describe_motion_graphic_quality
        current = set_cache_quality(tier)
        for key, action in self._cache_quality_actions.items():
            action.setChecked(key == current)
        label = describe_motion_graphic_quality(current)
        self._set_cache_status(f"Cache quality: {label}")
        self.statusBar().showMessage(
            I18N.tr(
                "motion_graphic.cache.quality_set",
                "Motion Graphic cache quality set to {label}",
                label=label,
            ), 3000)

    def _detach_component_instance(self) -> None:
        """Bake instance overrides into plain layers as one undoable edit."""
        from ..core.components import read_component_instances
        instances = read_component_instances(self.editor_controller.scene.metadata)
        selected = self._selected_elements
        commands = tuple(
            DetachComponentInstanceCommand(instance.instance_id)
            for instance in instances
            if any(element_id in instance.element_ids for element_id in selected)
        )
        if commands:
            self.editor_controller.execute(
                CompositeCommand(commands, label="Detach Component Instances"))

    def _selected_native_elements(self) -> tuple[SceneElement, ...]:
        scene = self.editor_controller.scene
        return tuple(
            element for element_id in self._selected_elements
            if (element := scene.element_by_id(element_id)) is not None
        )

    def _copy_style(self) -> None:
        """Copy the primary selection's visual style for later Paste Style."""
        elements = self._selected_native_elements()
        if not elements:
            return
        import copy
        self._style_clipboard = copy.deepcopy(dict(elements[0].style))

    def _paste_style(self) -> None:
        """Apply the copied style to every selected layer as one undo step."""
        clipboard = self._style_clipboard
        if not clipboard:
            return
        elements = self._selected_native_elements()
        commands = tuple(
            UpdateElementCommand(element.element_id, {"style": dict(clipboard)})
            for element in elements
        )
        if commands:
            self.editor_controller.execute(
                CompositeCommand(commands, label="Paste Style"))

    def _rebuild_theme_menu(self) -> None:
        """Rebuild the Theme submenu from the scene's stored brand themes."""
        from ..core.design_tokens import read_active_brand_theme, read_brand_themes
        self._theme_menu.clear()
        scene = self.editor_controller.scene
        themes = read_brand_themes(scene.metadata)
        active = read_active_brand_theme(scene.metadata)

        default_action = QAction(I18N.tr("motion_graphic.menu.theme_default", "Default"), self._theme_menu)
        default_action.setCheckable(True)
        default_action.setChecked(active is None)
        default_action.triggered.connect(lambda checked=False: self._apply_brand_theme(None))
        self._theme_menu.addAction(default_action)
        for theme in themes:
            action = QAction(theme.theme_id, self._theme_menu)
            action.setCheckable(True)
            action.setChecked(active is not None and active.theme_id == theme.theme_id)
            action.triggered.connect(
                lambda checked=False, theme_id=theme.theme_id: self._apply_brand_theme(theme_id))
            self._theme_menu.addAction(action)

    def _apply_brand_theme(self, theme_id: str | None) -> None:
        """Switch the active brand theme as one undoable metadata edit."""
        from ..core.design_tokens import set_active_brand_theme_into
        scene = self.editor_controller.scene
        metadata = set_active_brand_theme_into(scene.metadata, theme_id)
        self.editor_controller.execute(SetSceneMetadataCommand(metadata))

    def _browser_text_edited(self, element_id: str, text: str) -> None:
        """Commit one in-place browser text edit as an undoable scene change."""
        element = self.editor_controller.scene.element_by_id(element_id)
        if element is None:
            return
        content: object = str(text)
        if isinstance(element.content, dict):
            merged = dict(element.content)
            merged["text"] = str(text)
            content = merged
        self.editor_controller.update_element(element_id, {"content": content})

    def _canvas_edit_requested(self, element_id: str) -> None:
        """Reveal the Parameters text field after a canvas double-click."""
        self._set_selection({element_id}, source="canvas")
        self._show_right_panel_tab(0)
        self.inspector.content_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.inspector.content_edit.selectAll()

    def _browser_loaded(self, ok: bool) -> None:
        if ok and self._html_browser_active:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.browser_ready", "HTML live preview ready"), 2000)

    def _browser_zoom_changed(self, value: float) -> None:
        if self._html_browser_active:
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.browser_zoom",
                    "HTML preview zoom: {percent}% (wheel to adjust; View > Fit Preview restores fit)",
                    percent=f"{float(value) * 100:.0f}"),
                1600,
            )

    def _fit_preview_to_view(self) -> None:
        """Fit either the SVG canvas or the interactive HTML page."""
        self.scene_view.fit_canvas()
        self.html_browser_preview.fit_to_view()

    def _export_html_document(self, output: str | Path) -> bool:
        """Export the current HTML layer as a single source document."""
        scene = self.editor_controller.scene
        if scene.html_layer is None:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.no_layer_export", "This scene has no HTML layer to export"))
            return False
        from ..export_service import (
            ExportOptions, TeachingAnimationExportService, TeachingExportError,
        )

        service = TeachingAnimationExportService()
        try:
            result = service.export(
                scene, str(output), options=ExportOptions(format="html_document"),
            )
        except (TeachingExportError, OSError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.html_export_failed", "HTML export failed: {error}", error=exc))
            return False
        finally:
            service.close()
        self.statusBar().showMessage(I18N.tr("motion_graphic.status.html_exported", "HTML exported: {path}", path=result.output))
        return True

    def _render_video_cache(self, checked: bool = False) -> None:
        """Persist then request the host-owned background video-cache job."""
        del checked
        callback = self._render_cache_callback
        if callback is None:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.no_timeline_cache", "This scene is not on a timeline yet; video cache is unavailable"), 4000)
            return
        if self.editor_controller.is_dirty or self.html_source_editor.has_pending_draft:
            self.save()
        from ..cache_quality import get_cache_quality
        from src.shared.media.motion_graphic_delivery import (
            describe_motion_graphic_quality, motion_graphic_bitrate_kbps)
        scene = self.editor_controller.scene
        tier = describe_motion_graphic_quality(get_cache_quality())
        target = motion_graphic_bitrate_kbps(
            scene.width, scene.height, preset=get_cache_quality()) / 1000.0
        self._set_cache_status(
            I18N.tr(
                "motion_graphic.cache.submitted",
                "Cache submitted · {label} · ~{bitrate:g} Mbps",
                label=tier, bitrate=target))
        try:
            message = str(callback(self.editor_controller.scene) or "").strip()
        except Exception as exc:  # noqa: BLE001 - host boundary must not close the editor
            self._set_cache_status("Cache failed")
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.cache_failed", "Failed to start the video cache: {error}", error=exc), 5000)
            return
        self.statusBar().showMessage(
            message or I18N.tr("motion_graphic.status.no_renderable_clip", "No renderable Motion Graphic clip found"), 5000,
        )

    def _set_cache_status(self, status: str) -> None:
        """Show the hand-off state while Timeline owns the disk-cache job."""
        self._cache_status = str(status)
        scene = self.editor_controller.scene
        duration_seconds = scene.duration_frames / max(scene.fps, 0.001)
        self._preview_info.setText(
            f"{scene.width} × {scene.height}  ·  {scene.fps:g} fps  ·  {duration_seconds:g} s  ·  {self._cache_status}",
        )

    def _start_full_preview_cache(self, scene: TeachingSceneDocument) -> None:
        """Start the full-clip cache behind the interactive HTML preview.

        ``Render Preview`` used to capture only one browser frame.  That made the
        timeline indicator stay red and forced playback to open Chromium for
        every missing frame.  When the editor is hosted by the Edit page, use
        the host's disk-cache bridge so the manifest and timeline indicator are
        updated together.  A standalone editor still gets the same in-memory
        scene cache, but only after it has been shown (avoiding background work
        in headless/unit-test construction).
        """
        callback = self._render_cache_callback
        if callback is not None:
            # The host cache is a snapshot of the saved scene.  Persist pending
            # edits before requesting it, exactly like the explicit video-cache
            # action does.
            if self.editor_controller.is_dirty:
                self.save()
            try:
                message = str(callback(self.editor_controller.scene) or "").strip()
            except Exception as exc:  # noqa: BLE001 — host boundary is best effort
                self._set_cache_status("Cache failed")
                self.statusBar().showMessage(I18N.tr("motion_graphic.status.cache_failed", "Failed to start the video cache: {error}", error=exc), 5000)
                return
            self._set_cache_status("Cache submitted to timeline")
            self.statusBar().showMessage(
                message or I18N.tr("motion_graphic.status.caching_background", "Caching the full Motion Graphic in the background…"), 5000,
            )
            return

        # No host means the clip is not on a project timeline yet.  In a real
        # visible editor it is still useful to pre-render the scene so that a
        # later insertion can immediately reuse the frames.  Do not create a
        # worker for hidden/headless windows (notably the HTML preview tests).
        if not self.isVisible():
            return
        try:
            from ..scene_cache import get_shared_scene_cache_coordinator
            coordinator = get_shared_scene_cache_coordinator()
            coordinator.schedule(scene)
        except (ImportError, RuntimeError, TypeError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.precache_failed", "Failed to start pre-caching: {error}", error=exc), 5000)
            return
        self.statusBar().showMessage(
            I18N.tr("motion_graphic.status.precache_background", "Pre-caching in the background; the timeline shows a green bar when done"), 5000,
        )
        self._set_cache_status("Caching in background")

    def _export_html_triggered(self, checked: bool = False) -> None:
        del checked
        scene = self.editor_controller.scene
        if scene.html_layer is None:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.no_layer_export", "This scene has no HTML layer to export"))
            return
        default_name = f"{scene.name or 'motion_graphic'}.html"
        output, selected_filter = QFileDialog.getSaveFileName(
            self, "Export Motion Graphic HTML", default_name,
            "HTML Files (*.html);;All Files (*)",
        )
        if not output:
            return
        path = Path(output)
        if selected_filter.startswith("HTML Files") and path.suffix.lower() != ".html":
            path = path.with_suffix(".html")
        self._export_html_document(path)

    def _set_selection(self, ids: object, *, source: str = "") -> None:
        """Single selection chokepoint (multi-select phase-1).

        ``ids`` may be a single id (str) or any iterable of ids.  The window is
        the sole owner of ``_selected_elements``; ``source`` ("canvas"/"tree")
        prevents echoing the selection back to the panel that originated it.
        The inspector shows the structured form for the "primary" element
        (first selected, HTML layer row taking precedence), so its single-
        element editing surface is reused unchanged.
        """
        if isinstance(ids, str):
            new = frozenset({ids})
        else:
            new = frozenset(str(i) for i in ids)  # type: ignore[arg-type]
        self._selected_elements = new
        self.scene_view.set_selected_elements(new)
        if HTML_LAYER_ROW_ID in new:
            self.inspector_stack.setCurrentIndex(1)
            self.html_inspector.set_layer(self.editor_controller.scene.html_layer)
        else:
            self.inspector_stack.setCurrentIndex(0)
            elements = tuple(
                element for element_id in new
                if (element := self.editor_controller.scene.element_by_id(element_id)) is not None
            )
            # Design tokens ride along so the colour-token combo can bind
            # layers to $palette.* entries of the active brand theme.
            from ..core.design_tokens import read_scene_tokens
            self.inspector.set_selection(
                elements, tokens=read_scene_tokens(self.editor_controller.scene.metadata))
        # Sync the layer-tree highlight unless the change came from the tree.
        if source != "tree":
            self.layer_tree.set_selection(new)
        # Keyframes timeline follows every selection except its own echo.
        if source != "keyframes":
            self.keyframes_panel.set_selection(new)
        # Mirror the selection into the running page so clicking a Layers row
        # and clicking the DOM node highlight the same thing.
        native_ids = new - {HTML_LAYER_ROW_ID}
        self.html_browser_preview.select_node(sorted(native_ids)[0] if len(native_ids) == 1 else "")
        self._refresh_selection_actions()

    def _refresh_selection_actions(self) -> None:
        """Keep selection-dependent Edit/Scene actions usable right away.

        Copy/Paste Style and Detach depend on both the current selection and
        the style clipboard state; freezing their enablement at the last scene
        refresh left Copy Style greyed out after a plain re-select and Paste
        Style dead immediately after the first Copy.
        """
        selected_elements = self._selected_elements - {HTML_LAYER_ROW_ID}
        can_align = len(selected_elements) >= 2
        self._align_button.setEnabled(can_align)
        if hasattr(self, "_align_menu_action"):
            self._align_menu_action.setEnabled(can_align)
        if hasattr(self, "_detach_component_action"):
            from ..core.components import read_component_instances
            instances = read_component_instances(self.editor_controller.scene.metadata)
            self._detach_component_action.setEnabled(any(
                element_id in instance.element_ids
                for instance in instances
                for element_id in selected_elements
            ))
        if hasattr(self, "_copy_style_action"):
            self._copy_style_action.setEnabled(len(selected_elements) == 1)
            self._paste_style_action.setEnabled(
                bool(self._style_clipboard) and len(selected_elements) >= 1)

    def _on_canvas_select(self, element_id: str) -> None:
        # Plain click. If the clicked element is already part of a multi-
        # selection, KEEP the set so a following drag moves all selected
        # (drag-moves-all, phase-2). Otherwise replace with just this element.
        if element_id in self._selected_elements and len(self._selected_elements) > 1:
            return
        self._set_selection({element_id}, source="canvas")

    def _on_canvas_toggle(self, element_id: str) -> None:
        # Shift-click toggles one element in/out of the multi-selection.
        self._set_selection(self._selected_elements ^ {element_id}, source="canvas")

    def _on_tree_selection(self, ids: tuple[str, ...]) -> None:
        # The tree owns multi-row selection; mirror its set exactly.
        self._set_selection(ids, source="tree")

    def _on_canvas_marquee(self, ids: object, additive: bool) -> None:
        """Apply a SceneView rubber-band result through the selection chokepoint."""
        selected = (frozenset(str(item) for item in ids)
                    if isinstance(ids, (tuple, list, set, frozenset)) else frozenset())
        if additive:
            selected = self._selected_elements | selected
        self._set_selection(selected, source="canvas")

    def _on_drag_started(self, element_id: str) -> None:
        # Stash every selected element's origin so a drag moves them all by
        # one delta. If the dragged element isn't in the selection (canvas
        # click selects it first, so this is rare), drag just it.
        targets = (self._selected_elements if element_id in self._selected_elements
                   else frozenset({element_id}))
        scene = self.editor_controller.scene
        self._drag_anchors = {}
        for eid in targets:
            el = scene.element_by_id(eid)
            if el is not None:
                self._drag_anchors[eid] = (el.transform.x, el.transform.y)
                self.editor_controller.begin_preview(eid)

    def _on_drag_finished(self, element_id: str, _x: float, _y: float) -> None:
        # Commit every element that began a preview (the whole selection).
        # Clear the anchors FIRST: the commit notifications must take the
        # full-refresh route in _on_scene_notified (the drag is over), while
        # the in-drag moves above used the light transform preview.
        anchors = self._drag_anchors
        self._drag_anchors = {}
        for eid in anchors:
            self.editor_controller.commit_preview(eid)

    def _element_dragged(self, element_id: str, x: float, y: float) -> None:
        # Drag-moves-all: shift every anchored element by the same delta as the
        # dragged one. The delta is (dragged current pos) − (dragged origin).
        anchors = self._drag_anchors
        if not anchors:
            element = self.editor_controller.scene.element_by_id(element_id)
            if element is not None:
                self.editor_controller.preview_element(
                    element_id, {"transform": replace(element.transform, x=x, y=y)})
            return
        origin = anchors.get(element_id, (x, y))
        dx, dy = x - origin[0], y - origin[1]
        scene = self.editor_controller.scene
        for eid, (sx, sy) in anchors.items():
            el = scene.element_by_id(eid)
            if el is not None:
                self.editor_controller.preview_element(
                    eid, {"transform": replace(el.transform, x=sx + dx, y=sy + dy)})

    def _scene_selected(self, scene_id: str) -> None:
        scene = self._scene_store.get(scene_id)
        if scene is None or scene_id == self.editor_controller.scene.scene_id:
            return
        self._selected_elements = frozenset()
        self.editor_controller = SceneEditorController(scene)
        self.editor_controller.subscribe(self._on_scene_notified)
        self._refresh_scene(scene)

    def _create_scene(self) -> None:
        scene = TeachingSceneDocument.create_default(name=f"Scene {len(self._scene_ids) + 1}")
        self._scene_store.register(scene)
        self._scene_selected(scene.scene_id)

    def _duplicate_scene(self, scene_id: str) -> None:
        source = self._scene_store.get(scene_id)
        if source is None:
            return
        from dataclasses import replace as dc_replace
        import uuid
        duplicate = dc_replace(source, scene_id=f"scene_{uuid.uuid4().hex}", name=f"{source.name} Copy")
        self._scene_store.register(duplicate)
        self._scene_selected(duplicate.scene_id)

    def _delete_scene(self, scene_id: str) -> None:
        if len(self._scene_ids) <= 1:
            return
        self._scene_store.remove(scene_id)
        remaining = self._scene_store.scenes()
        if remaining:
            self._scene_selected(remaining[0].scene_id)

    def _reorder_scene(self, scene_id: str, index: int) -> None:
        if scene_id not in self._scene_ids:
            return
        ids = list(self._scene_ids)
        ids.remove(scene_id)
        ids.insert(max(0, min(index, len(ids))), scene_id)
        self._scene_ids = ids

    def _element_updated(self, element_id: str, updates: object) -> None:
        self.editor_controller.update_element(element_id, updates if isinstance(updates, dict) else {})

    def _selection_updated(self, element_ids: object, updates: object) -> None:
        """Apply one Inspector edit to all selected elements as one undo step."""
        if not isinstance(element_ids, (tuple, list, set, frozenset)) or not isinstance(updates, dict):
            return
        scene = self.editor_controller.scene
        commands: list[UpdateElementCommand] = []
        for raw_id in element_ids:
            element = scene.element_by_id(str(raw_id))
            if element is None:
                continue
            update = dict(updates)
            style_patch = update.pop("style_patch", None)
            transform_patch = update.pop("transform_patch", None)
            if isinstance(style_patch, dict):
                update["style"] = {**dict(element.style), **style_patch}
            if isinstance(transform_patch, dict):
                update["transform"] = replace(element.transform, **transform_patch)
            # AUD-31006: a multi-select Content edit carries a plain string,
            # but elements with structured (dict) content — shapes, charts,
            # paths — must never have their geometry replaced by that string.
            # Merge the text field instead, mirroring the single-select
            # branch in the inspector.
            raw_content = update.get("content")
            if isinstance(raw_content, str) and isinstance(element.content, dict):
                merged = dict(element.content)
                merged["text"] = raw_content
                update["content"] = merged
            if update:
                commands.append(UpdateElementCommand(element.element_id, update))
        if commands:
            self.editor_controller.execute(CompositeCommand(tuple(commands), label="Edit selected elements"))

    def _toggle_hierarchy_flag(self, element_id: str, flag: str) -> None:
        """Invert one element's lock/solo/hide flag via the undoable command.

        ``flag`` is the Hierarchy field name (locked/solo/hidden).  The new value
        is the inverse of the current membership, so the layer-tree icon (which
        already flipped visually) and the scene stay consistent after refresh.
        """
        from ..core.hierarchy import read_hierarchy
        hierarchy = read_hierarchy(self.editor_controller.scene.metadata)
        current = element_id in getattr(hierarchy, flag)
        self.editor_controller.execute(SetHierarchyCommand(element_id, flag, not current))

    def _remove_element(self, element_id: str) -> None:
        if self.editor_controller.scene.element_by_id(element_id) is not None:
            self.editor_controller.execute(RemoveElementCommand(element_id))
            if element_id in self._selected_elements:
                self._set_selection(self._selected_elements - {element_id}, source="canvas")

    def _create_layer(self, element_type: str) -> None:
        """Create a visible MG layer from the Layers panel's add menu."""
        scene = self.editor_controller.scene
        next_index = len(scene.elements) + 1
        z_index = max((item.z_index for item in scene.elements), default=-1) + 1
        page_id = self.editor_controller.current_page_id
        if element_type == "frame":
            element = SceneElement(
                element_id=f"frame_{uuid.uuid4().hex}", type="frame", z_index=z_index,
                transform=SceneTransform(
                    x=scene.width * 0.15, y=scene.height * 0.15,
                    width=scene.width * 0.7, height=scene.height * 0.7,
                ),
                style={"fill": "#1e293b", "corner_radius": 16.0},
                layout={"mode": "flex-column", "gap": 16.0, "padding": 24.0},
                content=f"Frame {next_index}",
                page_id=page_id,
            )
            self.editor_controller.execute(AddElementCommand(element))
            self._set_selection({element.element_id})
            return
        if element_type != "text":
            kind = self._normalized_shape_kind(element_type)
            is_line = kind in {"line", "arrow"}
            element = self._new_shape_element(
                kind, scene.width * 0.35, scene.height * 0.35, scene.width * 0.3,
                24.0 if is_line else scene.height * 0.3, z_index=z_index,
                label_index=next_index, page_id=page_id,
            )
        else:
            element = SceneElement(
                element_id=f"text_{uuid.uuid4().hex}", type="text", z_index=z_index,
                transform=SceneTransform(
                    x=scene.width * 0.25, y=scene.height * 0.45,
                    width=scene.width * 0.5, height=96.0,
                ),
                style={"color": "#f9fafb", "font_size": 48.0},
                content=f"Text {next_index}",
                page_id=page_id,
            )
        self.editor_controller.execute(AddElementCommand(element))
        self._set_selection({element.element_id})

    @staticmethod
    def _normalized_shape_kind(kind: str) -> str:
        return kind if kind in {
            "rect", "rounded_rect", "ellipse", "line", "arrow", "triangle", "polygon", "star",
        } else "rect"

    def _new_shape_element(
        self,
        kind: str,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        z_index: int,
        label_index: int,
        page_id: str = "",
    ) -> SceneElement:
        """Build one portable vector layer from a click-drag canvas gesture."""
        content: dict[str, object] = {
            "kind": kind,
            "label": f"{kind.replace('_', ' ').title()} {label_index}",
        }
        if kind == "rounded_rect":
            content["corner_radius"] = min(24.0, width / 2.0, height / 2.0)
        elif kind == "polygon":
            content["sides"] = 6
        elif kind == "star":
            content["points"] = 5
            content["inner_ratio"] = 0.45
        return SceneElement(
            element_id=f"shape_{uuid.uuid4().hex}", type="shape", z_index=z_index,
            transform=SceneTransform(x=x, y=y, width=max(2.0, width), height=max(2.0, height)),
            style={"fill": "#ff8800", "stroke": "#000000", "stroke_width": 0.0},
            content=content,
            page_id=page_id,
        )

    def _set_shape_draw_mode(self, kind: str | None) -> None:
        scene = self.editor_controller.scene
        if scene.html_layer is not None and self.html_browser_preview.ensure_browser():
            # Keep the actual web page on screen: particles and authored
            # CSS/JS continue running while a transparent SVG draw surface
            # emits an undoable native layer on pointer release.
            self._html_browser_active = True
            self._preview_stack.setCurrentWidget(self.html_browser_preview)
            self.html_browser_preview.set_native_draw_tool(kind)
        self.scene_view.set_draw_shape_kind(kind)
        self._sync_preview_tool_buttons(kind)
        if kind is None:
            self.statusBar().showMessage(I18N.tr("motion_graphic.layer.select_tool", "Select"), 1500)
        else:
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.layer.draw_hint", "Drag on the canvas to draw a shape"), 3000,
            )

    def _sync_preview_tool_buttons(self, active_kind: str | None) -> None:
        """Mirror the main timeline toolbar: active tool uses an orange icon."""
        for kind, button in self._preview_tool_buttons.items():
            is_active = kind == active_kind
            button.setChecked(is_active)
            icon_factory = self._preview_tool_icons[kind]
            button.setIcon(icon_factory(IconColors.ORANGE if is_active else IconColors.DEFAULT))

    def _draw_shape_layer(self, kind: str, x: float, y: float, width: float, height: float) -> None:
        scene = self.editor_controller.scene
        element = self._new_shape_element(
            self._normalized_shape_kind(kind), x, y, width, height,
            z_index=max((item.z_index for item in scene.elements), default=-1) + 1,
            label_index=len(scene.elements) + 1,
            page_id=self.editor_controller.current_page_id,
        )
        self.editor_controller.execute(AddElementCommand(element))
        self._set_selection({element.element_id})

    def _draw_path_layer(self, points: object) -> None:
        """Commit a completed pen gesture as one native, undoable path layer."""
        if not isinstance(points, (tuple, list)):
            return
        from ..core.vector_shapes import path_geometry_from_scene_points
        try:
            raw_points = tuple(
                (float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, (tuple, list)) and len(point) == 2
            )
            closed = len(raw_points) >= 4 and raw_points[0] == raw_points[-1]
            geometry = path_geometry_from_scene_points(
                raw_points[:-1] if closed else raw_points, closed=closed,
            )
        except (TypeError, ValueError):
            return
        x, y, width, height, content = geometry
        scene = self.editor_controller.scene
        element = SceneElement(
            element_id=f"path_{uuid.uuid4().hex}", type="shape",
            z_index=max((item.z_index for item in scene.elements), default=-1) + 1,
            transform=SceneTransform(x=x, y=y, width=width, height=height),
            style={"fill": "#ff8800", "fill_opacity": 0.0, "stroke": "#ff8800", "stroke_width": 3.0},
            content={**content, "label": f"Path {len(scene.elements) + 1}"},
            page_id=self.editor_controller.current_page_id,
        )
        self.editor_controller.execute(AddElementCommand(element))
        self._set_selection({element.element_id})

    def _update_path_anchors(self, element_id: str, points: object) -> None:
        """Normalize a direct-anchor edit and commit it as one MG command."""
        if not isinstance(points, (tuple, list)):
            return
        element = self.editor_controller.scene.element_by_id(element_id)
        if element is None or not isinstance(element.content, dict):
            return
        from ..core.vector_shapes import path_geometry_from_scene_points
        try:
            x, y, width, height, content = path_geometry_from_scene_points(tuple(
                (float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, (tuple, list)) and len(point) == 2
            ), closed=bool(element.content.get("closed", False)))
        except (TypeError, ValueError):
            return
        content["label"] = element.content.get("label", "Path")
        self.editor_controller.execute(UpdateElementCommand(
            element_id,
            {"transform": SceneTransform(x=x, y=y, width=width, height=height,
                                          scale_x=element.transform.scale_x,
                                          scale_y=element.transform.scale_y,
                                          rotation=element.transform.rotation),
             "content": content},
            label="Edit Path Anchors",
        ))

    def _duplicate_layer(self, element_id: str) -> None:
        """Duplicate the active layer above its source as one undoable command."""
        source = self.editor_controller.scene.element_by_id(element_id)
        if source is None:
            return
        duplicate = replace(
            source,
            element_id=f"{source.type}_{uuid.uuid4().hex}",
            z_index=max(item.z_index for item in self.editor_controller.scene.elements) + 1,
            transform=replace(source.transform, x=source.transform.x + 24.0, y=source.transform.y + 24.0),
        )
        self.editor_controller.execute(AddElementCommand(duplicate))
        self._set_selection({duplicate.element_id})

    def _reorder_element(self, element_id: str, new_index: int) -> None:
        if self.editor_controller.scene.element_by_id(element_id) is not None:
            self.editor_controller.execute(ReorderElementCommand(element_id, new_index))

    def _reparent_element(self, element_id: str, parent_id: str) -> None:
        if element_id == parent_id:
            return
        try:
            self.editor_controller.execute(SetParentCommand(element_id, parent_id))
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot nest layer: {exc}")

    def _unparent_element(self, element_id: str) -> None:
        try:
            self.editor_controller.execute(SetParentCommand(element_id, None))
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot unnest layer: {exc}")

    def _align_selection(self, mode: str) -> None:
        """Align/distribute the selected elements as one undoable step (phase-3a).

        Reads the selected *element* ids (the HTML-layer row never participates),
        computes target positions with the pure ``align`` helper, and lands them
        through a ``CompositeCommand`` of per-element ``UpdateElementCommand``\\ s.
        """
        if mode not in ALIGN_MODES:
            return
        scene = self.editor_controller.scene
        elements = [scene.element_by_id(eid)
                    for eid in self._selected_elements
                    if eid != HTML_LAYER_ROW_ID]
        elements = [el for el in elements if el is not None]
        if len(elements) < 2:
            self.statusBar().showMessage("Select at least two elements to align")
            return
        updates = build_alignment_updates(elements, mode)
        if not updates:
            self.statusBar().showMessage(f"{mode}: already aligned")
            return
        commands = tuple(UpdateElementCommand(eid, upd) for eid, upd in updates.items())
        self.editor_controller.execute(CompositeCommand(commands, label=f"Align ({mode})"))

    def _create_component(self, component_id: str, display_name: str) -> bool:
        scene = self.editor_controller.scene
        selected = {item for item in self._selected_elements if item != HTML_LAYER_ROW_ID}
        element_ids = tuple(element.element_id for element in scene.elements if element.element_id in selected)
        if not element_ids or not component_id.strip() or not display_name.strip():
            return False
        try:
            self.editor_controller.execute(RegisterComponentMasterCommand(
                ComponentMaster(component_id.strip(), display_name.strip(), element_ids)))
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot create component: {exc}")
            return False
        self.statusBar().showMessage(f"Component '{display_name.strip()}' created")
        return True

    def _create_component_triggered(self, checked: bool = False) -> None:
        from PyQt6.QtWidgets import QInputDialog
        if not (self._selected_elements - {HTML_LAYER_ROW_ID}):
            self.statusBar().showMessage("Select one or more structured layers first")
            return
        display_name, accepted = QInputDialog.getText(self, "Create Component", "Component name:")
        if not accepted or not display_name.strip():
            return
        component_id = "component_" + uuid.uuid4().hex[:10]
        self._create_component(component_id, display_name)

    def _set_component_variant(self, component_id: str, variant_id: str) -> bool:
        try:
            self.editor_controller.execute(SetComponentVariantCommand(component_id, variant_id))
        except ValueError as exc:
            self.statusBar().showMessage(f"Cannot set component variant: {exc}")
            return False
        return True

    def _set_component_variant_triggered(self, checked: bool = False) -> None:
        from PyQt6.QtWidgets import QInputDialog
        choices = [
            f"{master.component_id}: {variant_id}"
            for master in read_component_masters(self.editor_controller.scene.metadata)
            for variant_id in sorted(master.variants)
        ]
        if not choices:
            return
        choice, accepted = QInputDialog.getItem(self, "Set Component Variant", "Variant:", choices, 0, False)
        if not accepted:
            return
        component_id, variant_id = choice.split(": ", 1)
        self._set_component_variant(component_id, variant_id)

    def _apply_html_edit(self, operation: str, target: str, value: str) -> None:
        """Turn an HTML inspector edit into a validated, re-sanitized, undoable
        patch (FH-06 → FH-10).  The patch is built against the live layer's
        revision + source hash so it validates against current state."""
        layer = self.editor_controller.scene.html_layer
        if layer is None:
            return
        try:
            op = HtmlPatchOperation(operation)
        except ValueError:
            return
        patch = HtmlPatch(op, target, value, layer.revision, layer.source_hash)
        self.editor_controller.execute(ApplyHtmlPatchCommand(patch))

    def _apply_html_source(self, source: str) -> bool:
        """Apply a full manual HTML edit through the layer sanitizer.

        Returns whether the sanitized source actually landed (AUD-30902: the
        caller-driven close flow must be able to tell an applied edit from a
        rejected one instead of reading a stale pending-draft flag).
        """
        if self.editor_controller.scene.html_layer is None:
            return False
        try:
            self.editor_controller.execute(ReplaceHtmlSourceCommand(str(source)))
            self.html_source_editor.discard_pending_draft()
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.source_applied", "HTML source sanitized and applied; undo is available"), 4000)
            return True
        except (TypeError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.source_apply_failed", "HTML source not applied: {error}", error=exc), 5000)
            return False

    def _import_html_triggered(self, checked: bool = False) -> None:
        """Import a local HTML document into an empty or existing clip.

        Import is an undoable ``SetHtmlLayerCommand`` even when the scene was
        created without a layer, so the user can immediately undo/retry a bad
        file without losing the empty Motion Graphic asset.
        """
        del checked
        path, accepted = QFileDialog.getOpenFileName(
            self,
            I18N.tr("motion_graphic.status.import_dialog_title", "Import Motion Graphic HTML"),
            str(Path.home()),
            I18N.tr("motion_graphic.status.import_file_filter", "HTML Files (*.html *.htm);;All Files (*)"),
        )
        if not accepted or not path:
            return
        try:
            source = Path(path).read_text(encoding="utf-8-sig")
            scene = self.editor_controller.scene
            layer = build_html_layer_from_source(
                source,
                layer_id=f"html_{uuid.uuid4().hex}",
                viewport_width=scene.width,
                viewport_height=scene.height,
                duration_frames=scene.duration_frames,
                fps_num=scene.fps_num,
                fps_den=scene.fps_den,
                metadata={
                    "source_file": str(Path(path).resolve()),
                    "source_file_name": Path(path).name,
                },
            )
            self.editor_controller.execute(SetHtmlLayerCommand(layer))
            self._html_browser_active = True
            self._preview_stack.setCurrentWidget(self.html_browser_preview)
            self._show_right_panel_tab(0)
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.html_imported",
                "Imported HTML: {name} (preview, edit and drop it onto the timeline)",
                name=Path(path).name),
                5000,
            )
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.html_import_failed", "HTML import failed: {error}", error=exc), 6000)

    def _open_html_devtools(self) -> None:
        """Attach the real Chromium DevTools frontend to the live preview."""
        if self.editor_controller.scene.html_layer is None:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.no_layer_devtools", "This scene has no HTML layer; Developer Tools are unavailable"), 4000)
            return
        view = self.html_browser_preview.create_devtools_view(self.html_devtools)
        if view is None:
            self.html_devtools.set_devtools_widget(None)
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.devtools_unavailable", "No visible Qt WebEngine in this environment; Developer Tools are unavailable"), 5000,
            )
            return
        self.html_devtools.set_devtools_widget(view)
        self._html_browser_active = True
        self._preview_stack.setCurrentWidget(self.html_browser_preview)
        self._show_right_panel_tab(3)
        self.statusBar().showMessage(
            I18N.tr("motion_graphic.status.devtools_opened", "Chromium DevTools open: inspect and edit live in Elements / Console / Sources"), 4000,
        )

    def _show_html_preview(self) -> None:
        if self.editor_controller.scene.html_layer is None:
            self._preview_stack.setCurrentWidget(self.scene_view)
            return
        self._html_browser_active = True
        self._preview_stack.setCurrentWidget(self.html_browser_preview)

    def _apply_live_dom_to_html(self) -> None:
        """Persist live DevTools Elements/CSS edits through the HTML command."""
        if self.editor_controller.scene.html_layer is None:
            return
        self.statusBar().showMessage(I18N.tr("motion_graphic.status.reading_live_dom", "Reading the live DOM from DevTools…"), 3000)
        # R-07: read BOTH documents.  Authored edits go through the HTML
        # layer command; native-overlay edits become element property
        # commands when every change is on the whitelist.
        self.html_browser_preview.read_authored_document(
            lambda source: self.html_browser_preview.read_live_native_overlay(
                lambda overlay: self._apply_live_dom_source(source, live_native_overlay=overlay)))

    def _build_native_property_command(self, live_native_overlay: str):
        """Translate native-overlay DevTools edits into element commands.

        Returns (CompositeCommand | None, unmappable reasons).  None without
        reasons means "no native edits"; None WITH reasons means the caller
        must fall back to the whole-HTML replacement and say why.
        """
        from ..core.commands import CompositeCommand
        from ..core.devtools_property_mapping import (
            diff_native_dom, edits_to_update_commands)
        expected = self.html_browser_preview.native_markup
        if not expected or not live_native_overlay:
            return None, ()
        result = diff_native_dom(expected, live_native_overlay)
        if not result.edits:
            return None, result.unmappable
        commands, unmappable = edits_to_update_commands(
            self.editor_controller.scene, result.edits)
        if unmappable or not commands:
            return None, tuple(result.unmappable) + tuple(unmappable)
        return CompositeCommand(commands, label="DevTools Property Edits"), ()

    def _apply_live_dom_source(self, source: str, live_native_overlay: str | None = None) -> None:
        from ..core.commands import CompositeCommand
        if not str(source or "").strip():
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.live_dom_unavailable", "Cannot read the live DOM: the HTML browser preview is not ready yet"), 5000,
            )
            # AUD-30902: an aborted read must never leave the deferred-close
            # flag armed, or the next manual Apply would close the editor.
            self._finish_devtools_apply_close(applied=False)
            return
        native_command, unmappable = None, ()
        if live_native_overlay is not None:
            native_command, unmappable = self._build_native_property_command(live_native_overlay)
        try:
            html_command = ReplaceHtmlSourceCommand(str(source))
            if native_command is not None:
                # One undo step for the whole DevTools session: the authored
                # source replacement and the element property edits land (and
                # revert) together.  AUD-30904: the real validation lives in
                # command.apply (empty-source/sanitizer ValueErrors), so the
                # execute call must sit inside the same protection as the
                # command construction.
                self.editor_controller.execute(CompositeCommand(
                    (html_command, native_command), label="DevTools Apply"))
                self.html_source_editor.discard_pending_draft()
                self.html_devtools.set_has_unapplied_changes(False)
                self.statusBar().showMessage(I18N.tr(
                    "motion_graphic.status.devtools_property_applied",
                    "DevTools applied: element property edits + HTML source as one undo step"), 5000)
                self._finish_devtools_apply_close(applied=True)
                return
        except (TypeError, ValueError) as exc:
            if native_command is None:
                self.statusBar().showMessage(I18N.tr(
                    "motion_graphic.status.source_apply_failed",
                    "HTML source not applied: {error}", error=exc), 5000)
                self._finish_devtools_apply_close(applied=False)
                return
            # The authored source is broken but the property edits are sound;
            # apply what mapped and report the authored failure.
            self.editor_controller.execute(native_command)
            self.html_source_editor.discard_pending_draft()
            self.html_devtools.set_has_unapplied_changes(False)
            self.statusBar().showMessage(I18N.tr(
                "motion_graphic.status.devtools_property_applied",
                "Applied DevTools edits to element properties; authored source not applied: {error}",
                error=exc), 6000)
            # AUD-30902: the authored half failed; saving and closing now
            # would silently drop those edits.  Keep the editor open.
            self._finish_devtools_apply_close(applied=False)
            return
        if unmappable:
            self.statusBar().showMessage(I18N.tr(
                "motion_graphic.status.devtools_fallback",
                "Falling back to whole-HTML apply: {reasons}",
                reasons="; ".join(unmappable[:3])), 6000)
        applied = self._apply_html_source(source)
        if applied and not self.html_source_editor.has_pending_draft:
            self.html_devtools.set_has_unapplied_changes(False)
        self.statusBar().showMessage(
            I18N.tr(
                "motion_graphic.status.live_dom_applied" if applied
                else "motion_graphic.status.source_apply_failed",
                "DevTools DOM/CSS applied to the HTML layer; undo is available"
                if applied else "HTML source not applied: the live DOM failed validation"),
            5000,
        )
        self._finish_devtools_apply_close(applied=applied)

    def _finish_devtools_apply_close(self, *, applied: bool) -> None:
        """Resolve a closeEvent-requested save+close after a DevTools apply.

        AUD-30902: every exit path of the DevTools apply must clear the flag.
        When the apply failed the window stays open (and dirty) so nothing is
        silently discarded; a successful apply proceeds with save + close.
        """
        if not self._close_after_devtools_apply:
            return
        self._close_after_devtools_apply = False
        if not applied:
            return
        self.save()
        QTimer.singleShot(0, self.close)

    def _discard_live_dom_changes(self) -> None:
        """Restore the browser from the saved HTML layer and clear its draft cue."""
        self.html_browser_preview.reload_document()
        self.html_devtools.set_has_unapplied_changes(False)
        self.statusBar().showMessage(I18N.tr("motion_graphic.status.devtools_discarded", "Discarded unapplied DevTools changes"), 3000)

    def _apply_html_page_settings(self, mode: str, default_seconds: float,
                                  durations_text: str, transition: str,
                                  transition_seconds: float) -> None:
        """Compatibility entry point for timing-only integrations."""
        scene = self.editor_controller.scene
        self._apply_html_page_settings_with_backgrounds(
            mode,
            default_seconds,
            durations_text,
            transition,
            transition_seconds,
            scene_background_css(scene.background),
            tuple(page.background for page in scene.pages),
        )

    def _apply_html_page_settings_with_backgrounds(
        self,
        mode: str,
        default_seconds: float,
        durations_text: str,
        transition: str,
        transition_seconds: float,
        global_background: str,
        page_backgrounds: object,
    ) -> None:
        """Apply the complete Page section as one undoable scene operation."""
        from ..core.html_page_deck import PageDeckTiming, detect_page_count
        layer = self.editor_controller.scene.html_layer
        if layer is None:
            return
        page_count = detect_page_count(layer.sanitized_html)
        try:
            default = float(default_seconds)
            if mode == "custom":
                durations = tuple(float(item.strip()) for item in durations_text.split(",") if item.strip())
                if len(durations) != page_count:
                    raise ValueError(I18N.tr(
                        "motion_graphic.status.page_count_mismatch",
                        "Expected {expected} page durations, got {actual}",
                        expected=page_count, actual=len(durations)))
                timing = PageDeckTiming(
                    page_count, "custom", default, durations,
                    transition=str(transition or "cut"),
                    transition_seconds=max(0.0, float(transition_seconds)),
                )
            else:
                timing = PageDeckTiming(
                    page_count, "default", default,
                    transition=str(transition or "cut"),
                    transition_seconds=max(0.0, float(transition_seconds)),
                )
            duration_frames = max(1, round(timing.total_seconds * self.editor_controller.scene.fps))
            commands: list[object] = [
                SetHtmlPageTimingCommand(timing),
                SetDurationCommand(duration_frames),
            ]
            commands.extend(self._background_commands(
                self.editor_controller.scene,
                global_background,
                page_backgrounds,
            ))
            self.editor_controller.execute(CompositeCommand(
                tuple(commands),
                label=I18N.tr(
                    "motion_graphic.status.page_settings_label",
                    "Set HTML page timing and backgrounds",
                ),
            ))
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.page_settings_applied",
                "Page settings applied: {pages} pages · {seconds:g}s · {transition}",
                pages=page_count, seconds=timing.total_seconds, transition=timing.transition))
        except (TypeError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.page_settings_invalid", "Invalid page settings: {error}", error=exc))

    def _apply_background_settings(
        self, global_background: str, page_backgrounds: object,
    ) -> None:
        """Apply the global background and page overrides as one undo step."""
        commands = self._background_commands(
            self.editor_controller.scene, global_background, page_backgrounds,
        )
        if commands:
            self.editor_controller.execute(CompositeCommand(
                commands, label="Set Motion Graphic Backgrounds",
            ))

    @staticmethod
    def _background_commands(
        scene: TeachingSceneDocument,
        global_background: str,
        page_backgrounds: object,
    ) -> tuple[object, ...]:
        """Build reversible background writes without mutating the scene."""
        values = tuple(page_backgrounds) if isinstance(page_backgrounds, (list, tuple)) else ()
        current_global = scene_background_css(scene.background)
        normalized_global = safe_background_value(
            global_background, fallback=current_global,
        )
        next_background = dict(scene.background)
        background_key = "css" if "css" in next_background else "color"
        next_background[background_key] = normalized_global
        commands: list[object] = []
        if dict(scene.background) != next_background:
            commands.append(SetSceneBackgroundCommand(next_background))
        for index, page in enumerate(scene.pages):
            value = values[index] if index < len(values) else page.background
            text = str(value or "").strip()
            normalized = "" if not text else safe_background_value(
                text, fallback=page.background,
            )
            if normalized != page.background:
                commands.append(UpdatePageCommand(
                    page.page_id, {"background": normalized},
                ))
        return tuple(commands)

    # Compatibility entry point for code that still applies only timing.
    def _apply_html_page_timing(self, mode: str, default_seconds: float,
                                durations_text: str) -> None:
        layer = self.editor_controller.scene.html_layer
        if layer is None:
            return
        from ..core.html_page_deck import detect_page_count, page_timing_for
        page_count = detect_page_count(layer.sanitized_html)
        current = page_timing_for(
            layer.metadata,
            page_count=page_count,
            fallback_default_seconds=max(0.001, layer.duration_frames / max(0.001, layer.fps)) / page_count,
        )
        self._apply_html_page_settings(
            mode, default_seconds, durations_text,
            current.transition, current.transition_seconds,
        )

    def _apply_html_page_transition(self, transition: str, seconds: float) -> None:
        """Apply a browser-owned page transition without changing page timing."""
        from ..core.html_page_deck import PageDeckTiming, detect_page_count, page_timing_for
        layer = self.editor_controller.scene.html_layer
        if layer is None:
            return
        page_count = detect_page_count(layer.sanitized_html)
        try:
            current = page_timing_for(
                layer.metadata,
                page_count=page_count,
                fallback_default_seconds=(
                    max(0.001, layer.duration_frames / max(0.001, layer.fps))
                    / page_count
                ),
            )
            timing = PageDeckTiming(
                page_count,
                current.mode,
                current.default_seconds,
                current.durations_seconds,
                transition=str(transition or "cut"),
                transition_seconds=max(0.0, float(seconds)),
            )
            self.editor_controller.execute(SetHtmlPageTimingCommand(timing))
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.transition_applied",
                "Browser transition applied: {transition} · {seconds:g}s",
                transition=timing.transition, seconds=timing.transition_seconds))
        except (TypeError, ValueError) as exc:
            self.statusBar().showMessage(I18N.tr("motion_graphic.status.transition_invalid", "Invalid browser transition: {error}", error=exc))

    # ------------------------------------------------------------------ #
    # HTML template promote / apply (FH-09)
    # ------------------------------------------------------------------ #
    #
    # Logic and dialogs are split so the wiring is testable without driving
    # Qt modal dialogs: _save_as_template / _apply_template do the work; the
    # *_triggered variants open the dialog and forward.  Promotion uses the
    # pure engine (render/html_layer.template_promote); re-application lands
    # through the undoable SetHtmlLayerCommand, identical to apply_html_layer.

    def _save_as_template(self, display_name: str, *, template_id: str | None = None) -> HtmlTemplate | None:
        """Freeze the current scene's HTML layer into a reusable template.

        Returns the registered template, or None when the scene has no HTML
        layer to promote (status bar explains why).  The source layer is not
        modified — it stays as the audited fallback.
        """
        layer = self.editor_controller.scene.html_layer
        if layer is None:
            self.statusBar().showMessage("No HTML layer to promote — open or generate an HTML scene first")
            return None
        tid = template_id or f"tpl_{uuid.uuid4().hex[:8]}"
        template = promote_html_layer(layer, template_id=tid, display_name=display_name)
        self._html_templates.register(template)
        # Make verified AI/user-authored templates available in the Edit
        # Effects palette.  The template remains separate from Media Pool
        # assets and is instantiated only when dragged to the timeline.
        from ..core.motion_graphic_generator_registry import (
            register_motion_graphic_generator,
        )
        register_motion_graphic_generator(template)
        n_params = len(template.params_schema)
        self.statusBar().showMessage(
            f"Saved template '{display_name}' ({tid}, {n_params} parameter"
            f"{'s' if n_params != 1 else ''})")
        return template

    def _open_template_library(self, checked: bool = False) -> None:
        """Reveal the user's writable template library (R-05).

        Opening the bundled root would invite users to edit the Python
        package directory; the user library is the supported place to drop
        copied template folders.
        """
        del checked
        from ..core.motion_graphic_template_store import ensure_user_template_root
        try:
            root = ensure_user_template_root()
        except OSError:
            self.statusBar().showMessage("Template library folder is unavailable", 4000)
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _refresh_template_library(self, checked: bool = False) -> None:
        """Reload template folders from every origin without a restart (R-05)."""
        del checked
        from ..core.motion_graphic_generator_registry import get_motion_graphic_generator_registry
        from ..core.motion_graphic_template_store import discover_stored_templates
        registry = get_motion_graphic_generator_registry()
        stored = discover_stored_templates()
        for item in stored:
            self._html_templates.register(item.package.template)
            registry.register(
                item.package.template, preview_path=item.package.preview_path,
                packaged=True)
        self._apply_template_action.setEnabled(bool(self._html_templates.list_templates()))
        self.statusBar().showMessage(
            f"Template library refreshed: {len(stored)} package(s)", 4000)

    # -- preset library (asset library tab) ---------------------------------

    def _insert_preset_at_center(self, preset_id: str) -> None:
        self._instantiate_preset_at(preset_id, None, None)

    def _apply_library_template(self, template_id: str) -> None:
        """Drop/double-click an HTML template package into this scene (R4).

        The unified library drag protocol's template branch: instantiate the
        package with its default params bound to this canvas, then set the
        scene's HTML layer through one undoable command.
        """
        from ..core.html_layer_commands import SetHtmlLayerCommand
        from ..core.mg_package_registry import (
            KIND_HTML_TEMPLATE,
            instantiate_html_template_package,
        )
        catalog = self.asset_library.catalog
        package = catalog.get(template_id, kind=KIND_HTML_TEMPLATE)
        if package is None:
            self.statusBar().showMessage(f"Unknown template: {template_id}")
            return
        scene = self.editor_controller.scene
        try:
            layer = instantiate_html_template_package(
                package, width=scene.width, height=scene.height,
                duration_frames=scene.duration_frames,
                fps_num=scene.fps_num, fps_den=scene.fps_den,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.template_apply_failed",
                        "Template could not be applied: {error}", error=exc), 5000)
            return
        self.editor_controller.execute(SetHtmlLayerCommand(layer))
        self.statusBar().showMessage(
            I18N.tr("motion_graphic.status.template_applied",
                    "HTML template applied: {name} (undo available)",
                    name=package.display_name), 5000)

    def _instantiate_preset_at(self, preset_id: str, scene_x: float | None,
                               scene_y: float | None) -> None:
        """Drop a library preset into the scene as one undoable insert.

        The fragment is re-centred on the drop point (canvas centre when
        inserted via double-click), stacked above existing layers and
        auto-selected.  Timeline alignment: preset keyframes are authored in
        a bare 0-based frame space, so they are shifted to the *active page's*
        global start frame — otherwise the animation would play during page 1
        while the element belongs to page N.  When the fragment's animation
        outruns the destination page, that page (not the whole scene) is
        extended inside the same composite command; ``UpdatePageCommand``
        re-anchors later pages and retimes their tracks.
        """
        record = self.asset_library.library.get(preset_id)
        if record is None:
            self.statusBar().showMessage(f"Unknown preset: {preset_id}")
            return
        try:
            preset_scene = load_preset_scene(record.manifest, record.folder)
        except PresetError as exc:
            self.statusBar().showMessage(I18N.tr(
                "motion_graphic.status.preset_load_failed",
                "Preset failed to load: {error}", error=exc), 6000)
            return
        host = self.editor_controller.scene
        target_x = host.width / 2.0 if scene_x is None else float(scene_x)
        target_y = host.height / 2.0 if scene_y is None else float(scene_y)
        # prepare_insert also converts the preset's keyframe frames to the
        # host fps so wall-clock timing survives the graft.
        elements, parents = prepare_insert(preset_scene, host,
                                           target_x=target_x, target_y=target_y)
        # Preset fragments are page-agnostic; they land on the active page so
        # the canvas (page-scoped) shows them where they were dropped.
        page_id = self.editor_controller.current_page_id
        elements = tuple(
            element if element.page_id == page_id
            else replace(element, page_id=page_id)
            for element in elements
        )
        page = host.page_by_id(page_id)
        ranges = page_frame_ranges(host)
        page_start, page_end = ranges.get(page_id, (0, host.duration_frames))
        commands: list[Any] = []
        needed_end = page_start + fragment_max_keyframe_frame(elements) + 1
        if page is not None and needed_end > page_end:
            commands.append(UpdatePageCommand(page_id, {
                "duration_frames": page.duration_frames + (needed_end - page_end)}))
        elif page is None and needed_end > host.duration_frames:
            # Degenerate host without a resolvable page: fall back to the
            # scene-level duration extension.
            commands.append(SetDurationCommand(needed_end))
        elements = retime_fragment_to_page(elements, page_start,
                                           max(needed_end, page_end))
        commands.append(InsertSceneFragmentCommand(
            elements, parents,
            label=I18N.tr("motion_graphic.commands.insert_preset", "Insert Preset")))
        self.editor_controller.execute(CompositeCommand(
            tuple(commands),
            label=f"Insert Preset: {record.manifest.name}"))
        self._set_selection({element.element_id for element in elements})
        self.statusBar().showMessage(I18N.tr(
            "motion_graphic.status.preset_inserted",
            "Inserted preset '{name}' ({count} layers)",
            name=record.manifest.name, count=len(elements)), 4000)

    def _save_preset_triggered(self, checked: bool = False) -> None:
        """Snapshot the selection (or all structured layers) as a preset."""
        del checked
        host = self.editor_controller.scene
        if not host.elements:
            self.statusBar().showMessage(I18N.tr(
                "motion_graphic.status.preset_nothing_to_save",
                "Nothing to save — the scene has no structured layers (HTML layers use 'Save as Template')"))
            return
        selected = sorted(self._selected_elements - {HTML_LAYER_ROW_ID})
        element_ids = selected if selected else [element.element_id for element in host.elements]
        try:
            scene_data = extract_fragment(host, element_ids)
        except ValueError as exc:
            self.statusBar().showMessage(f"Save as Preset failed: {exc}", 6000)
            return
        dialog = PresetSaveDialog(
            self, categories=list(self.asset_library.library.categories().keys()),
            default_name=host.name)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        payload = dialog.payload()
        if not payload["name"]:
            return
        try:
            record = self.asset_library.library.save_preset(
                name=payload["name"], category=payload["category"],
                scene_data=scene_data, description=payload["description"],
                tags=payload["tags"])
        except (PresetError, OSError) as exc:
            QMessageBox.warning(self, I18N.tr(
                "motion_graphic.library.save_failed", "Save as Preset Failed"), str(exc))
            return
        self.asset_library.refresh()
        self.asset_library.select_preset(record.preset_id)
        self._panel_tabs.setCurrentWidget(self.asset_library)
        self.statusBar().showMessage(I18N.tr(
            "motion_graphic.status.preset_saved",
            "Preset saved to {folder}",
            folder=record.folder), 6000)

    def _open_preset_library(self, checked: bool = False) -> None:
        del checked
        root = user_preset_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(root)))

    def _save_as_template_triggered(self, checked: bool = False) -> None:
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(
            self, "Save as Template", "Template name:", text=self.editor_controller.scene.name)
        if ok and name.strip():
            self._save_as_template(name.strip())

    def _apply_template(self, template_id: str) -> bool:
        """Re-apply a saved template to the scene with default parameters.

        Returns True on success, False if the template id is unknown.  The new
        layer lands through ``SetHtmlLayerCommand`` so the write is undoable.
        """
        template = self._html_templates.get(template_id)
        if template is None:
            self.statusBar().showMessage(f"Unknown template: {template_id}")
            return False
        layer = apply_html_template(template, layer_id=f"html_{uuid.uuid4().hex}")
        self.editor_controller.execute(SetHtmlLayerCommand(layer))
        self.statusBar().showMessage(f"Applied template '{template.display_name}'")
        return True

    def _apply_template_triggered(self, checked: bool = False) -> None:
        templates = self._html_templates.list_templates()
        if not templates:
            self.statusBar().showMessage("No saved templates yet — use 'Save as Template' first")
            return
        from PyQt6.QtWidgets import QDialog, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout
        dialog = QDialog(self)
        dialog.setWindowTitle(I18N.tr("motion_graphic.actions.apply_template_title", "Apply Template"))
        layout = QVBoxLayout(dialog)
        list_widget = QListWidget(dialog)
        for tpl in templates:
            from ..core.content_localization import template_display_name
            item = QListWidgetItem(
                f"{template_display_name(tpl)}  ({tpl.template_id}, {len(tpl.params_schema)} params)")
            item.setData(256, tpl.template_id)
            list_widget.addItem(item)
        layout.addWidget(list_widget)

        def _on_accept() -> None:
            current = list_widget.currentItem()
            if current is not None:
                self._apply_template(str(current.data(256)))
            dialog.accept()

        apply_btn = QPushButton(I18N.tr("motion_graphic.actions.apply_default_params", "Apply (default params)"), dialog)
        apply_btn.clicked.connect(_on_accept)
        layout.addWidget(apply_btn)
        if list_widget.count():
            list_widget.setCurrentRow(0)
        dialog.exec()

    def _keyframe_requested(self, element_id: str, property_name: str, value: float) -> None:
        self.editor_controller.add_keyframe(element_id, property_name, self.editor_controller.current_frame, value)

    def _apply_keyframe_edits(self, edits: list) -> None:
        """Land a keyframes-timeline batch (one drag/paste = one undo step)."""

        self.editor_controller.apply_track_edits(edits)

    def _set_keyframes_panel_visible(self, visible: bool) -> None:
        """Show the keyframes timeline, or collapse back to the compact bar."""

        if visible:
            self.keyframes_panel.setVisible(True)
            self._sync_timeline_host()
            if self._last_vertical_splitter_sizes is not None:
                self._vertical_splitter.setSizes(self._last_vertical_splitter_sizes)
            self.keyframes_panel.fit_time()
        else:
            sizes = self._vertical_splitter.sizes()
            if len(sizes) == 2 and sizes[1] > 64:
                self._last_vertical_splitter_sizes = list(sizes)
            self.keyframes_panel.setVisible(False)
            self._sync_timeline_host()
            total = max(64, sum(sizes) if len(sizes) == 2 else 0)
            self._vertical_splitter.setSizes([max(1, total - 64), 64])

    def _sync_timeline_host(self) -> None:
        """Dock transport controls in the unified timeline or compact fallback."""

        if self._keyframes_panel_action.isChecked():
            self.timeline.set_transport_host(
                self.keyframes_panel.transport_layout, include_timecode=False,
            )
            self.timeline.hide()
            return
        self.timeline.set_transport_host(
            None,
            include_timecode=True,
        )
        self.timeline.show()

    def _frame_changed(self, frame: int) -> None:
        self.editor_controller.set_frame(frame)
        self.inspector.set_frame(frame)
        self.keyframes_panel.set_current_frame(frame)
        # Keep the transport scrubber/timecode in step when the frame came
        # from the keyframes ruler (set_frame is silent, so no echo loop).
        self.timeline.set_frame(frame)
        scene = self.editor_controller.scene
        # Pages partition the timeline: crossing a page boundary switches the
        # artboard the canvas shows (native elements are filtered per page and
        # the browser deck steps to the same page).
        page_index = scene.page_index_for_frame(frame)
        page_switched = page_index != self._active_page_index
        self._active_page_index = page_index
        if self._html_browser_active:
            # Native-layer CSS animations scrub from the timeline playhead;
            # the running web page itself is never reloaded for this.
            self.html_browser_preview.set_native_time(
                frame / max(0.001, scene.fps))
            if page_switched:
                if page_index != self.html_browser_preview.current_page:
                    self._syncing_browser_page = True
                    try:
                        self.html_browser_preview.set_page(page_index)
                    finally:
                        self._syncing_browser_page = False
                self._refresh_native_overlay(scene, page_index)
            return
        self._refresh_scene(scene)

    def _refresh_native_overlay(self, scene: TeachingSceneDocument,
                                page_index: int) -> None:
        """Rebuild the browser's native overlay for one page (page switch)."""
        if scene.html_layer is None or not self._html_browser_active:
            return
        from ..core.design_tokens import read_scene_tokens
        from ..core.hierarchy import read_hierarchy
        from ..render.html_native_overlay import (
            native_animation_css, native_overlay_css_variables, native_overlay_markup)

        page_id = scene.page_ids[page_index]
        tokens = read_scene_tokens(scene.metadata)
        token_css = native_overlay_css_variables(tokens)
        animation_css = native_animation_css(scene)
        markup = native_overlay_markup(
            scene.elements_for_page(page_id),
            read_hierarchy(scene.metadata).parents, tokens)
        generated = (
            (f"<style>{token_css}</style>" if token_css else "")
            + (f"<style>{animation_css}</style>" if animation_css else "")
            + markup
        )
        self.html_browser_preview.set_native_overlay(generated)
        self.html_browser_preview.set_native_time(
            self.editor_controller.current_frame / max(0.001, scene.fps))
        self.html_source_editor.set_generated_output(generated)

    def _render_preview(self, checked: bool = False) -> None:
        """Preview the current frame and pre-cache the complete scene."""
        scene = self.editor_controller.scene
        if scene.html_layer is None:
            return
        self._html_preview_live = True
        # Many page-deck designs use a 0% opacity fade-in.  A literal frame-0
        # capture can therefore be a uniform background even though the next
        # frame already contains the intended artwork.  Show the first
        # meaningful frame for an immediate user preview; timeline/export frame
        # semantics remain unchanged.
        frame = int(self.editor_controller.current_frame)
        if frame == 0 and scene.duration_frames > 1:
            from ..core.html_page_deck import detect_page_count, page_timing_for
            layer = scene.html_layer
            page_count = detect_page_count(layer.sanitized_html) if layer is not None else 1
            timing = page_timing_for(
                getattr(layer, "metadata", {}) if layer is not None else {},
                page_count=page_count,
                fallback_default_seconds=max(0.001, scene.duration_frames / scene.fps)
                / page_count,
            )
            # Stay inside page one when a user chose a short custom interval;
            # otherwise use the familiar ~1 second blank-frame avoidance.
            first_page_frames = max(1, round(timing.durations[0] * scene.fps))
            preview_offset = min(first_page_frames - 1, max(1, round(float(scene.fps))))
            frame = min(scene.duration_frames - 1, preview_offset)
        self._request_html_preview(scene, frame)
        self._start_full_preview_cache(scene)

    def _request_html_preview(self, scene: TeachingSceneDocument, frame: int) -> None:
        """Queue only the newest HTML preview request and cancel older work."""
        if scene.html_layer is None:
            return
        self._html_preview_generation += 1
        request = (scene, int(frame), self._html_preview_generation)
        thread = self._html_preview_thread
        if thread is not None and thread.isRunning():
            self._html_preview_pending = request
            thread.requestInterruption()
            self.statusBar().showMessage(f"Rendering HTML frame {int(frame)}…")
            return
        if thread is not None:
            # The native thread has stopped; let Qt reclaim the wrapper before
            # replacing the Python reference. Its queued signals are harmless
            # because their generation is already stale.
            thread.deleteLater()
            self._html_preview_thread = None
        # AUD-30905: any pending request queued while the old thread was
        # still running is superseded by the request we are about to start —
        # leaving it set would make _on_html_preview_finished restart the
        # stale scene/frame one more time before being generation-rejected.
        self._html_preview_pending = None
        self._start_html_preview(*request)

    def _start_html_preview(self, scene: TeachingSceneDocument, frame: int,
                            generation: int) -> None:
        thread = _HtmlPreviewThread(scene, frame, generation, parent=self)
        thread.frameReady.connect(self._on_html_preview_ready)
        thread.renderFailed.connect(self._on_html_preview_failed)
        thread.finished.connect(lambda thread=thread: self._on_html_preview_finished(thread))
        self._html_preview_thread = thread
        self.statusBar().showMessage(f"Rendering HTML frame {int(frame)}…")
        thread.start(QThread.Priority.LowPriority)

    def _on_html_preview_ready(self, generation: int, frame: object) -> None:
        if generation != self._html_preview_generation:
            return
        if not isinstance(frame, RenderedFrame):
            self._on_html_preview_failed(generation, "renderer returned an invalid frame")
            return
        self._last_render_error = None
        self.scene_view.set_frame(frame)
        self.statusBar().showMessage(
            f"Rendered frame {frame.frame} ({frame.width}×{frame.height})", 3000)

    def _on_html_preview_failed(self, generation: int, message: str) -> None:
        if generation != self._html_preview_generation:
            return
        self._last_render_error = message
        self.statusBar().showMessage(f"HTML preview unavailable: {message}")

    def _on_html_preview_finished(self, thread: _HtmlPreviewThread) -> None:
        if self._html_preview_thread is not thread:
            thread.deleteLater()
            return
        self._html_preview_thread = None
        pending = self._html_preview_pending
        self._html_preview_pending = None
        thread.deleteLater()
        if pending is not None and not self._html_preview_closing:
            self._start_html_preview(*pending)

    def _stop_html_preview(self) -> None:
        thread = self._html_preview_thread
        # Stopping an already-idle preview must not consume a generation.  In
        # particular, WebEngine can emit more than one show event while the
        # editor window is being laid out; invalidating an empty queue there
        # used to make the first visible capture appear as generation 2.
        if thread is None and self._html_preview_pending is None:
            return
        self._html_preview_closing = True
        self._html_preview_generation += 1
        self._html_preview_pending = None
        self._html_preview_thread = None
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            # AUD-30906: bound the wait — the cancellation chain depends on
            # the browser backend tearing down promptly, and a hung backend
            # must not freeze closeEvent forever on the UI thread. The
            # deleteLater below still reclaims the wrapper once Qt delivers
            # deferred deletion.
            if not thread.wait(_HTML_PREVIEW_STOP_TIMEOUT_MS):
                logger.warning(
                    "HTML preview thread still running after %d ms; "
                    "continuing shutdown without it",
                    _HTML_PREVIEW_STOP_TIMEOUT_MS,
                )
        if thread is not None:
            thread.deleteLater()

    def showEvent(self, event: Any) -> None:  # noqa: N802 — Qt API spelling
        """Start the first HTML capture when the editor becomes visible.

        The main timeline already has a rendered frame, but the editor owns a
        separate ``SceneView`` and its browser capture is asynchronous.  The
        old opt-in flow left that view black until the user clicked *Render
        Preview*.  Starting one capture at show time keeps the preview in sync
        without making every construction/test fixture launch Chromium.
        """
        super().showEvent(event)
        if not self._space_filter_installed:
            QApplication.instance().installEventFilter(self)
            self._space_filter_installed = True
        self._schedule_front_restore()
        scene = self.editor_controller.scene
        if scene.html_layer is not None:
            if self._html_preview_live:
                return
            # Set the guard before creating WebEngine.  It may synchronously
            # trigger another layout/show event, and the first capture must be
            # requested exactly once.
            self._html_preview_live = True
            if self.html_browser_preview.ensure_browser():
                self._html_browser_active = True
                self._preview_stack.setCurrentWidget(self.html_browser_preview)
                self._stop_html_preview()
                self._html_preview_closing = False
                self._refresh_scene(scene)
            else:
                self._request_html_preview(scene, self.editor_controller.current_frame)

    def keyPressEvent(self, event: Any) -> None:  # noqa: N802 — Qt API spelling
        """Make page navigation work even when focus is outside the browser."""
        if (
            event_matches(event, PLAY_PAUSE_ACTION_ID, PLAY_PAUSE_DEFAULT)
            and not event.isAutoRepeat()
            and self._toggle_playback_from_space()
        ):
            event.accept()
            return
        if self._html_browser_active:
            focus = QApplication.focusWidget()
            editing_text = self._is_text_editing_focus(focus)
            if not editing_text and event.modifiers() == Qt.KeyboardModifier.NoModifier:
                key = event.key()
                if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown):
                    self.html_browser_preview.step_page(1)
                    event.accept()
                    return
                if key in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp):
                    self.html_browser_preview.step_page(-1)
                    event.accept()
                    return
                if key == Qt.Key.Key_Home:
                    self.html_browser_preview.set_page(0)
                    event.accept()
                    return
                if key == Qt.Key.Key_End:
                    self.html_browser_preview.set_page(10**9)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def _duration_changed(self, duration: int) -> None:
        if duration != self.editor_controller.scene.duration_frames:
            self.editor_controller.execute(SetDurationCommand(duration))

    def _canvas_size_requested(self, width: int, height: int) -> None:
        """R1: 断点 / 快速比例切换 —— 记录基准画布并保持可撤销。"""
        scene = self.editor_controller.scene
        if (width, height) != (scene.width, scene.height):
            self.editor_controller.execute(SetCanvasSizeCommand(int(width), int(height)))

    def _undo(self) -> None:
        self.editor_controller.undo()

    def _redo(self) -> None:
        self.editor_controller.redo()

    def save(self) -> None:
        """Persist the scene without letting I/O failures crash a Qt slot.

        AUD-30901: Ctrl+S / close-time saves must survive a failing host
        saver (disk full, permissions, dropped network drive).  On failure the
        editor stays dirty, the status bar explains the problem, and the
        preview-cache scheduling is skipped (there is nothing new to cache).
        """
        self._scene_store.register(self.editor_controller.scene)
        if self._saver is not None:
            try:
                self.editor_controller.save(self._saver)
            except (OSError, RuntimeError) as exc:
                # editor_controller.save only clears the dirty flag after the
                # saver returns, so the user keeps the chance to retry.
                self.statusBar().showMessage(
                    I18N.tr("motion_graphic.status.save_failed",
                            "Save failed: {error}", error=exc), 6000)
                return
        else:
            self.editor_controller.mark_clean()
        try:
            from ..scene_cache import get_shared_scene_cache_coordinator
            get_shared_scene_cache_coordinator().schedule(self.editor_controller.scene)
        except (ImportError, RuntimeError, TypeError, ValueError) as exc:
            # The cache coordinator is a background preview accelerator only;
            # mirror the guarded usage in _start_full_preview_cache.
            self.statusBar().showMessage(
                I18N.tr("motion_graphic.status.cache_schedule_failed",
                        "Scene saved; preview cache scheduling failed: {error}",
                        error=exc), 4000)
        self._refresh_scene(self.editor_controller.scene)

    def closeEvent(self, event: Any) -> None:
        if (
            self.editor_controller.is_dirty
            or self.html_source_editor.has_pending_draft
            or self.html_devtools.has_unapplied_changes
        ):
            choice = QMessageBox.question(self, "Unsaved scene", "Save changes before closing?", QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel)
            if choice == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if choice == QMessageBox.StandardButton.Save:
                if self.html_source_editor.has_pending_draft:
                    self._apply_html_source(self.html_source_editor.editor.toPlainText())
                    if self.html_source_editor.has_pending_draft:
                        event.ignore()
                        return
                if self.html_devtools.has_unapplied_changes:
                    self._close_after_devtools_apply = True
                    self._apply_live_dom_to_html()
                    event.ignore()
                    return
                self.save()
            elif choice == QMessageBox.StandardButton.Discard:
                self.html_source_editor.discard_pending_draft()
                self._discard_live_dom_changes()
        self._stop_html_preview()
        self.html_browser_preview.close()
        self.timeline.stop()
        if self._space_filter_installed:
            QApplication.instance().removeEventFilter(self)
            self._space_filter_installed = False
        if self._registry is not None:
            self._registry.remember_geometry(self.editor_controller.scene.scene_id, self.saveGeometry().data())
            self._registry.unregister(self.editor_controller.scene.scene_id, self)
        event.accept()
