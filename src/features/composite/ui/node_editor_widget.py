"""
Node Editor Widget - QGraphicsView-based node graph canvas.

Supports:
- Pan (middle-click drag or Space+left drag)
- Zoom (scroll wheel)
- Rubber band selection
- Node selection and multi-selection
- Node dragging with optional grid snap
- Connection drawing (drag from output to input port)
- Context menu (right click) to add nodes
- Dot grid background
- Keyboard shortcuts (Del, F, M, Ctrl+D, Ctrl+Z, Ctrl+Shift+Z, G, H, Ctrl+A)
"""

from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsItem,
    QMenu, QDockWidget,
    QInputDialog, )
from PyQt6.QtCore import Qt, QPointF, QRectF, pyqtSignal, QTimer, QEvent
from PyQt6.QtGui import (
    QPainter, QPen, QColor,     QWheelEvent, QMouseEvent, QTransform, QKeySequence, QAction,
)
from typing import TYPE_CHECKING, Any, Dict, Optional, List, Callable
from collections import deque
import logging
import os
import threading
import uuid
import time

if TYPE_CHECKING:  # pragma: no cover - annotation-only imports
    from PyQt6.QtWidgets import QDialog, QListWidget

logger = logging.getLogger(__name__)

from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_manager import ShortcutManager
from src.shared.ui.style_sheet import PRESET_DIALOG_STYLE, MANAGE_PRESET_DIALOG_STYLE
from .node_item import NodeItem
from .backdrop_item import BackdropItem
from .sticky_note_item import StickyNoteItem
from .port_item import PortItem
from .connection_item import ConnectionItem
from .preview_connection_item import PreviewConnectionItem
from .node_search_popup import NodeSearchPopup
from .graph_node_search_popup import GraphNodeSearchPopup
from .node_editor_actions import NodeEditorActions
from .graph_layout import GraphLayout
from .graph_navigation import GraphNavigationState
from src.features.composite.core.node_engine import NodeGraph, NODE_CATEGORIES, get_categories_for_mode
from src.features.composite.nodes.base_node import Node


class ShakeDetector:
    """Detects horizontal shake gesture during node drag.

    Tracks recent X positions and timestamps, counts direction reversals
    (sign changes in deltaX), and triggers when threshold is reached within
    a time window.

    The trigger strictness (reversal threshold + minimum move distance) is
    derived from the user-configurable sensitivity; see
    ``_shake_params_from_sensitivity``.
    """

    REVERSAL_THRESHOLD = 4       # Default number of direction changes to trigger
    TIME_WINDOW_MS = 800         # Time window in milliseconds
    MIN_DELTA_X = 3.0            # Default minimum deltaX to count as a meaningful move

    def __init__(self, reversal_threshold: Optional[int] = None,
                 min_delta_x: Optional[float] = None) -> None:
        self._samples: list[tuple[float, float]] = []  # [(timestamp_ms, x_pos), ...]
        self._prev_dx: float = 0.0                      # Previous deltaX sign (-1, 0, +1)
        self._reversal_count: int = 0
        self._triggered: bool = False
        self._glow_progress: float = 0.0                # 0 to 1, for visual feedback
        # Instance-level strictness (falls back to class defaults) so the
        # sensitivity preference can tune detection without touching constants.
        self._reversal_threshold = (reversal_threshold
                                    if reversal_threshold is not None
                                    else self.REVERSAL_THRESHOLD)
        self._min_delta_x = (min_delta_x if min_delta_x is not None
                             else self.MIN_DELTA_X)

    def add_sample(self, timestamp_ms: float, x_pos: float) -> bool:
        """Add a mouse move sample. Returns True if shake is triggered."""
        if self._triggered:
            return True

        # Prune samples outside time window
        cutoff = timestamp_ms - self.TIME_WINDOW_MS
        self._samples = [(t, x) for t, x in self._samples if t >= cutoff]

        # Add new sample
        self._samples.append((timestamp_ms, x_pos))

        # Check for direction reversal
        if len(self._samples) >= 2:
            _, prev_x = self._samples[-2]
            dx = x_pos - prev_x

            if abs(dx) >= self._min_delta_x:
                sign = 1 if dx > 0 else -1
                if self._prev_dx != 0 and sign != self._prev_dx:
                    self._reversal_count += 1
                self._prev_dx = sign

        # Update glow progress (proportional to reversal count)
        self._glow_progress = min(
            self._reversal_count / self._reversal_threshold, 1.0
        )

        # Check threshold
        if self._reversal_count >= self._reversal_threshold:
            self._triggered = True
            return True

        return False

    @property
    def glow_progress(self) -> float:
        return self._glow_progress

    @property
    def triggered(self) -> bool:
        return self._triggered

    def reset(self) -> None:
        """Reset all shake detection state."""
        self._samples.clear()
        self._prev_dx = 0.0
        self._reversal_count = 0
        self._triggered = False
        self._glow_progress = 0.0


def _shake_params_from_sensitivity(sensitivity) -> tuple[int, float]:
    """Map a 0-100 sensitivity value to detector parameters.

    Returns ``(reversal_threshold, min_delta_x)``.

    Higher sensitivity → easier to trigger (fewer direction reversals needed,
    smaller minimum move). Lower sensitivity → harder to trigger and ignores
    slow, small movements (the previous "slow move disconnects" complaint).

    Mapping (clamped):
      reversal_threshold: 10 at s=0  → 3 at s=100
      min_delta_x:        8.0 at s=0 → 2.0 at s=100
    """
    s = max(0.0, min(100.0, float(sensitivity)))
    reversal_threshold = max(2, round(10 - s * 0.07))
    min_delta_x = max(1.5, 8.0 - s * 0.06)
    return reversal_threshold, min_delta_x


# MIME type for Media Pool drag and drop (P7.2)
MIME_TYPE_MEDIA_POOL_ASSETS = "application/x-topos-media-pool-assets"

# ── 3D 场景节点级自动匹配表（节点级拖拽连接）──
# 当连接线释放在节点主体（而非具体端口）上时，按源节点类型自动选择目标端口。
# 仅作用于 scene 类型轨道的 Scene3D 输入端口，2D image 连接保持精确到端口。
# v8：Scene3D 只有单一 `in` 端口，所有 3D 源类型都连到它（Scene3D 按 payload 类型分派）。
SCENE_AUTO_MATCH = {
    "camera3d": "in",
    "light3d":  "in",
    "geo3d":    "in",
    "model3d":  "in",
}
# 反向：从 Scene3D 的 `in` 端口拉出时，悬停的 3D 源节点类型集合 → 都可吸附。
SCENE_AUTO_MATCH_REVERSE = {
    "in": {"camera3d", "light3d", "geo3d", "model3d"},
}

_VIEWER3D_OBJECT_TYPES = frozenset({
    "camera3d", "light3d", "geo3d", "model3d",
})

# Supported file extensions for drag-and-drop — 统一引用 format_registry，
# 避免与媒体池/注册表的扩展名集合漂移（阶段五新增 3D 模型支持）。
from src.shared.media.format_registry import (
    IMAGE_EXTENSIONS as _IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS as _VIDEO_EXTENSIONS,
    MODEL_3D_EXTENSIONS as _MODEL_3D_EXTENSIONS,
    is_media as _is_media,
)


class NodeEditorWidget(QGraphicsView):
    """
    Interactive node graph editor.

    Manages the visual representation of a NodeGraph,
    handling all user interactions (pan, zoom, drag, connect, select).
    """

    # Signals
    node_added = pyqtSignal(str, str)  # node_id, node_type
    node_about_to_be_removed = pyqtSignal(str)  # node_id, before visual teardown
    nodes_about_to_be_removed = pyqtSignal(list)  # node_ids, before visual teardown
    node_removed = pyqtSignal(str)     # node_id
    node_connected = pyqtSignal(str, str, str, str)  # src_node, src_port, dst_node, dst_port
    node_disconnected = pyqtSignal(str, str, str, str)
    node_double_clicked = pyqtSignal(str)  # node_id
    # B3-1: user asked to enter the group containing this node (double-
    # click / context menu). The page owns the undo record and calls
    # enter_group_view() after applying the command.
    group_enter_requested = pyqtSignal(str)  # group_id
    # U-16：粘贴/预设装配开始（节点已建、内部连线将逐条提交）。页面
    # 在此期间抑制逐边 undo，等 nodes_pasted 统一成组压栈。
    nodes_pasting = pyqtSignal(list)
    group_view_changed = pyqtSignal(object)  # current group_id or None
    node_bypass_toggled = pyqtSignal(str)  # node_id (when bypass is toggled)
    # U-07：多选旁路一次性上报 {node_id: new_disabled}，页面压入单条
    # group undo（逐节点信号会拆成 N 条历史）。
    nodes_bypass_batch_toggled = pyqtSignal(dict)
    node_lock_changed = pyqtSignal(str, bool)  # node_id, canvas read-only state
    node_rename_requested = pyqtSignal(str, str)  # node_id, new user_label (B4; "" clears)
    # C5: version binning on the selected Read/Write node
    node_version_step_requested = pyqtSignal(str, int)  # node_id, delta
    set_all_reads_latest_requested = pyqtSignal()  # graph-level version up
    node_selected = pyqtSignal(str)   # node_id (when a single node is clicked/selected)
    nodes_selected = pyqtSignal(list)  # list of node_ids (when multiple nodes are selected)
    selection_cleared = pyqtSignal()  # when no node is selected
    graph_changed = pyqtSignal()
    nodes_moved = pyqtSignal(list, list)  # node_ids, old_positions (for undo)
    undo_requested = pyqtSignal()  # Ctrl+Z pressed
    redo_requested = pyqtSignal()  # Ctrl+Shift+Z pressed
    project_settings_requested = pyqtSignal()  # "S" pressed — toggle Project Settings
    preview_requested = pyqtSignal()  # "1" pressed — toggle node-preview of selected node
    preview_exit_requested = pyqtSignal()  # "Esc" pressed — leave node-preview (back to Output)
    zoom_changed = pyqtSignal(float)  # zoom level (0.1-5.0)
    viewport_moved = pyqtSignal()  # viewport panned/scrolled
    nodes_pasted = pyqtSignal(list)  # list of serialized node dicts (for undo)
    nodes_duplicated = pyqtSignal(list)  # list of new node IDs (for grouped undo)
    nodes_batch_removed = pyqtSignal(list)  # list of removed node IDs (for grouped undo)
    placeholder_node_added = pyqtSignal(str)  # node_title (when a placeholder node is added)

    # P7.2: Asset drop signal for creating asset-backed nodes
    assets_dropped = pyqtSignal(list, tuple)  # asset_ids, drop_position (x, y)

    # AUD-10502: video metadata probed off the UI thread for a dropped file.
    # file_path, {fps, total_frames, video_width, video_height}
    video_metadata_probed = pyqtSignal(str, dict)

    # Backdrop signals (for undo - backdrops don't trigger render dirty)
    backdrop_created = pyqtSignal(str, dict)  # backdrop_id, backdrop_data
    backdrop_deleted = pyqtSignal(str, dict)  # backdrop_id, backdrop_data (for undo restore)
    backdrops_deleted = pyqtSignal(list, dict)  # list of backdrop_ids, {id: data} dict
    backdrop_moved = pyqtSignal(str, tuple, tuple)  # backdrop_id, old_position, new_position
    backdrop_resized = pyqtSignal(str, tuple, tuple)  # backdrop_id, old_size, new_size
    backdrop_modified = pyqtSignal(str, dict, dict, list)  # backdrop_id, old_data, new_data, node_moves

    # Sticky note signals (for undo - sticky notes don't trigger render dirty)
    sticky_note_created = pyqtSignal(str, dict)  # note_id, note_data
    sticky_note_deleted = pyqtSignal(str, dict)  # note_id, note_data (for undo restore)
    sticky_notes_deleted = pyqtSignal(list, dict)  # list of note_ids, {id: data} dict
    sticky_note_modified = pyqtSignal(str, dict, dict)  # note_id, old_data, new_data

    # AUD-9920: group membership lifecycle → undo. Emitted AFTER the group
    # commands have been applied to the graph; payload is a human-readable
    # description plus the applied command objects. CompositePage owns the
    # undo record (same contract as backdrop_created / sticky_note_created).
    group_commands_applied = pyqtSignal(str, list)  # description, [commands]

    GRID_SIZE = 20
    GRID_COLOR_MINOR = QColor(50, 50, 55)
    GRID_COLOR_MAJOR = QColor(60, 60, 65)
    BG_COLOR = QColor(35, 35, 38)

    MIN_ZOOM = 0.1
    MAX_ZOOM = 5.0
    PORT_SNAP_DISTANCE = 30.0

    # Node thumbnail preview display modes (set from the context menu).
    # The editor owns this value and pushes per-node visibility via
    # NodeItem.set_thumbnail_visible(); NodeItem is decoupled from selection.
    PREVIEW_NONE = "none"                     # No node shows a thumbnail
    PREVIEW_SELECTED_SINGLE = "selected_single"  # Only the sole selected node (DEFAULT)
    PREVIEW_ALL = "all"                       # Every node with a thumbnail
    PREVIEW_MODES = (PREVIEW_NONE, PREVIEW_SELECTED_SINGLE, PREVIEW_ALL)
    PREVIEW_CONFIG_KEY = "composite.node_preview_mode"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # Scene
        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(-5000, -5000, 10000, 10000)
        # Performance: Use BspTreeIndex for faster item lookup in large scenes
        self._scene.setItemIndexMethod(QGraphicsScene.ItemIndexMethod.BspTreeIndex)
        self.setScene(self._scene)

        # B4: F2 (node rename) is also a window-level QAction shortcut
        # ("Rename Layer") — Qt fires window shortcuts BEFORE delivering
        # key events to child widgets. Accepting ShortcutOverride while the
        # canvas holds focus suppresses the global shortcut and lets F2
        # reach our ShortcutManager; other widgets keep the global action.
        self.viewport().installEventFilter(self)

        # Node items
        self._node_items: Dict[str, NodeItem] = {}

        # Node-preview pipeline: dashed connection from the previewed node to the
        # Output node. None when no node is being previewed. Toggled via
        # set_preview_node(); cleared on graph rebuild / node removal.
        self._preview_connection: Optional[PreviewConnectionItem] = None

        # Node thumbnail preview mode (resolved lazily from config on first use;
        # self.window() is not available yet during __init__).
        self._preview_mode = self.PREVIEW_SELECTED_SINGLE
        self._preview_mode_resolved = False
        # How the current selection was made. In SELECTED_SINGLE mode a preview
        # only appears when the sole selection came from a direct node CLICK;
        # box/rubber-band selection ("rubber") never triggers one.
        self._preview_selection_source = "none"
        # Recompute per-node preview visibility whenever the scene selection changes
        # (covers click, Ctrl/Shift, rubber-band, Ctrl-A, upstream/downstream, etc.).
        self._scene.selectionChanged.connect(self._on_selection_changed_for_preview)

        # Backdrop annotation items
        self._backdrop_items: Dict[str, BackdropItem] = {}

        # Sticky note annotation items
        self._sticky_note_items: Dict[str, StickyNoteItem] = {}

        # Connection items
        self._connection_items: list = []

        # Node graph data model reference
        self._node_graph: Optional[NodeGraph] = None
        # B3-1: group editing view. Stack of entered group ids (breadcrumb
        # hierarchy); _group_filter_ids is the visible node subset while
        # inside a group (None = show everything).
        self._group_view_stack: list = []
        self._group_filter_ids: Optional[set] = None

        # Actions (initialized when graph is set)
        self._actions: Optional[NodeEditorActions] = None

        # Layout manager (initialized when graph is set)
        self._layout: Optional[GraphLayout] = None

        # Interaction state
        self._dragging_connection = False
        self._drag_src_port: Optional[PortItem] = None
        self._drag_dst_port: Optional[PortItem] = None  # For reverse drag
        self._drag_direction: Optional[str] = None  # "forward" or "reverse"
        self._drag_src_node_id: Optional[str] = None
        self._temp_connection: Optional[ConnectionItem] = None
        self._snap_port: Optional[PortItem] = None  # Current snap target during drag

        # Rubber band selection state
        self._rubber_band_active = False
        self._rubber_band_start: Optional[QPointF] = None
        self._rubber_band_current_pos: Optional[QPointF] = None  # Current mouse pos for HiDPI accuracy

        # Grid snap
        self._snap_to_grid = False

        # Pan state
        self._space_held = False
        self._mid_button_panning = False
        self._pan_start: Optional[QPointF] = None
        self._pan_start_center: Optional[QPointF] = None

        # Zoom state
        self._current_zoom = 1.0

        # Clipboard for cut/copy/paste
        self._clipboard: Optional[dict] = None

        # Node move tracking (for undo)
        self._moving_nodes = False
        self._move_start_positions: Dict[str, QPointF] = {}

        # Shake-to-disconnect state
        self._shake_detector: Optional[ShakeDetector] = None
        self._shake_disconnecting: bool = False   # Re-entrancy guard
        self._shake_triggered: bool = False       # Stops proximity detection after shake
        self._last_shake_glow_value: float = -1.0 # Cache to avoid redundant repaints

        # Drop-on-connection state
        self._drop_insert_connection: Optional[tuple] = None
        self._drop_insert_nearest_point: Optional[QPointF] = None
        self._proximity_frame_counter: int = 0    # Throttle counter
        self._proximity_last_check_pos: Optional[QPointF] = None  # Cache for early exit
        self._proximity_last_time: float = 0.0    # Time-based throttle

        # Selected node tracking
        self._selected_node_id: Optional[str] = None

        # P1-12: persistent view bookmarks plus session-only back/forward
        # history.  Locked IDs live in the same project UI metadata as the
        # viewport, but do not alter render evaluation or undo state.
        self._navigation = GraphNavigationState()
        self._locked_node_ids: set[str] = set()
        self._navigation_restore_active = False
        # P1-12: graph-only focus state.  These are intentionally session
        # state: they change canvas readability, never graph evaluation or
        # project semantics.
        self._branch_isolation_ids: Optional[set[str]] = None
        self._dependency_highlight_ids: set[str] = set()

        # Cmd/Ctrl modifier state for connection insertion dots
        self._ctrl_insertion_active = False
        self._hovered_insertion_connection: Optional[ConnectionItem] = None

        # Node search popup (lazy-created)
        self._node_search_popup = None

        # Graph node search popup (lazy-created, for searching existing nodes)
        self._graph_node_search_popup = None

        # Highlighted nodes (for search results)
        self._highlighted_node_ids: List[str] = []

        # Shortcut management
        self._shortcuts_enabled = True
        self._shortcut_callbacks: Dict[str, Callable] = {}
        self._shortcut_keys_cache: Dict[str, QKeySequence] = {}
        self._setup_shortcuts()

        # View settings
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        # Performance: Use SmartViewportUpdate for better performance with partial updates
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)

        # Performance: LOD (Level of Detail) settings
        self._simplified_draw_threshold = 0.3  # Zoom below this uses simplified drawing
        self._batch_mode = False               # Suppress graph_changed during batch ops
        self._batch_update_active = False
        self._batch_update_timer = QTimer(self)
        self._batch_update_timer.setSingleShot(True)
        self._batch_update_timer.timeout.connect(self._flush_batch_updates)
        self._pending_updates = set()

        # Initial zoom
        self.scale(1.0, 1.0)

        # Node editor needs focus for key events
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Accept file drops from Finder/Explorer
        self.setAcceptDrops(True)

        # AUD-10502: backfill slot for background video-metadata probes.
        self.video_metadata_probed.connect(self._on_video_metadata_probed)

    # ── Node Management ──

    def editor_actions(self) -> Optional[NodeEditorActions]:
        """Return the programmatic node-operation entry, or None pre-bind.

        The actions object exists once a graph is bound (``build_from_graph``);
        the page uses it for node creation that must emit ``node_added`` /
        ``graph_changed`` (e.g. the AUD-10503 media-pool canvas drop consumer).
        """
        return self._actions

    def add_node_visual(self, node: Node) -> None:
        """Add a visual node item for the given node data."""
        # Check if this is a placeholder node (not fully implemented)
        is_placeholder = getattr(node, 'IS_PLACEHOLDER', False)

        # Check if this is a dot node (minimal circle rendering)
        is_dot = getattr(node, 'node_type', '') == 'dot'

        # Use "(N/A)" suffix for placeholder nodes in title
        node_title = node.display_title
        if is_placeholder:
            node_title = f"{node.display_title} (N/A)"

        item = NodeItem(
            node.id,
            node_title=node_title,
            header_color=node.header_color,
            is_dot=is_dot,
            node_type=node.node_type,
        )

        # Add ports
        for port in node.inputs:
            p = item.add_input_port(port.name, port.data_type,
                                    max_connections=port.max_connections)
            if is_dot:
                p.set_compact(True)
        for port in node.outputs:
            p = item.add_output_port(port.name, port.data_type)
            if is_dot:
                p.set_compact(True)

        # Set position (snap if enabled)
        x, y = node.position
        if self._snap_to_grid:
            x = round(x / self.GRID_SIZE) * self.GRID_SIZE
            y = round(y / self.GRID_SIZE) * self.GRID_SIZE
        # Dot nodes are positioned by center, not top-left
        if is_dot:
            x -= NodeItem.DOT_SIZE / 2
            y -= NodeItem.DOT_SIZE / 2
        item.setPos(x, y)

        # Set disabled state
        if hasattr(node, 'disabled') and node.disabled:
            item.set_disabled(True)

        # Set placeholder state for visual rendering (yellow hatch overlay)
        if is_placeholder:
            item.set_state("placeholder")

        if node.id in self._locked_node_ids:
            item.set_locked(True)

        # Double-click handler
        item.set_double_click_handler(self._on_node_double_clicked)

        self._scene.addItem(item)
        self._node_items[node.id] = item
        # Nodes added while a group view is active stay hidden unless they
        # belong to the filtered subset (batch builds re-filter once at
        # build_from_graph's end instead).
        if not self._batch_mode and self._group_filter_ids is not None:
            item.setVisible(node.id in self._group_filter_ids)
        # Defer backdrop refresh during batch operations (e.g., rapid node adds)
        if not self._batch_mode:
            self._refresh_all_backdrop_containment()
            # Apply current preview-mode visibility to the new node (and recheck
            # others). Skipped during batch build; applied once at build's end.
            self._apply_preview_mode()
        
        # Emit signal for placeholder notification
        if is_placeholder:
            self.placeholder_node_added.emit(node.node_title)

    def remove_node_visual(self, node_id: str) -> None:
        """Remove a visual node item."""
        self._locked_node_ids.discard(node_id)
        item = self._node_items.pop(node_id, None)
        if item:
            # Remove all connections
            for conn in list(item.connections):
                self._remove_connection_item(conn)
            # If this node was an endpoint of the preview connection, drop it —
            # its QGraphicsItem is about to be removed from the scene.
            if self._preview_connection is not None and node_id in (
                self._preview_node_id_for(self._preview_connection._src_item),
                self._preview_node_id_for(self._preview_connection._dst_item),
            ):
                self._clear_preview_connection()
            self._scene.removeItem(item)
            self._refresh_all_backdrop_containment()

    @staticmethod
    def _preview_node_id_for(item) -> Optional[str]:
        """Reverse-lookup a NodeItem's node_id (best-effort)."""
        # NodeItem stores node_id as an attribute; guard for non-NodeItem items.
        return getattr(item, "node_id", None)

    def add_backdrop_visual(self, data: dict) -> Optional[BackdropItem]:
        """Add a visual backdrop item from serialized UI metadata."""
        if "id" not in data:
            return None

        item = BackdropItem.from_dict(data)
        if not item.backdrop_id:
            return None

        self._wire_backdrop(item)
        self._scene.addItem(item)
        self._backdrop_items[item.backdrop_id] = item
        self._sync_backdrop_to_graph(item)
        return item

    def remove_backdrop_visual(self, backdrop_id: str, emit_signal: bool = True) -> None:
        """Remove a backdrop visual and its UI metadata.

        When ``emit_signal`` is True (default), emits ``backdrop_deleted`` for
        undo integration. Undo/redo callers pass False to avoid recursion.
        """
        item = self._backdrop_items.pop(backdrop_id, None)
        backdrop_data = None
        if item:
            backdrop_data = item.to_dict()
            self._scene.removeItem(item)
        if self._node_graph is not None:
            self._node_graph.remove_backdrop(backdrop_id)
        # Emit signal for undo (after removal, so data is captured)
        if emit_signal and backdrop_data:
            self.backdrop_deleted.emit(backdrop_id, backdrop_data)

    # ── Sticky Note Management ──

    def create_sticky_note(self, x: float, y: float, text: str = "",
                           color: str = "#FFE082") -> Optional[StickyNoteItem]:
        """Create a new sticky note at the given position.

        Args:
            x: Scene X coordinate
            y: Scene Y coordinate
            text: Initial text content
            color: Background color (hex string)

        Returns:
            The created StickyNoteItem or None if failed
        """
        note_id = str(uuid.uuid4())
        item = StickyNoteItem(
            note_id=note_id,
            text=text,
            color=color,
            position=QPointF(x, y),
        )
        self._wire_sticky_note(item)
        self._scene.addItem(item)
        self._sticky_note_items[note_id] = item
        self._sync_sticky_note_to_graph(item)
        # Emit for undo integration (graph_changed keeps project dirty/save working).
        self.sticky_note_created.emit(note_id, item.to_dict())
        self.graph_changed.emit()
        # Select the new note for immediate interaction (mirrors _add_new_backdrop).
        self._scene.clearSelection()
        item.setSelected(True)
        return item

    def add_sticky_note_visual(self, data: dict) -> Optional[StickyNoteItem]:
        """Add a visual sticky note item from serialized UI metadata."""
        if "id" not in data:
            return None

        item = StickyNoteItem.from_dict(data)
        if not item.note_id():
            return None

        self._wire_sticky_note(item)
        self._scene.addItem(item)
        self._sticky_note_items[item.note_id()] = item
        self._sync_sticky_note_to_graph(item)
        return item

    def remove_sticky_note_visual(self, note_id: str, emit_signal: bool = True) -> None:
        """Remove a sticky note visual and its UI metadata.

        When ``emit_signal`` is True (default), emits ``sticky_note_deleted`` for
        undo integration. Undo/redo callers pass False to avoid recursion.
        """
        item = self._sticky_note_items.pop(note_id, None)
        note_data = None
        if item:
            note_data = item.to_dict()
            self._scene.removeItem(item)
        if self._node_graph is not None:
            self._node_graph.remove_sticky_note(note_id)
        # Emit signal for undo (after removal, so data is captured).
        if emit_signal and note_data:
            self.sticky_note_deleted.emit(note_id, note_data)

    def _wire_sticky_note(self, item: StickyNoteItem) -> None:
        """Attach change/modify handlers to a sticky note."""
        item.set_changed_handler(self._on_sticky_note_changed)
        item.set_modified_handler(self._on_sticky_note_modified)

    def _on_sticky_note_modified(self, item: StickyNoteItem, old_data: dict, new_data: dict) -> None:
        """Forward a single sticky note edit (move/resize/edit/color/z) for undo."""
        self.sticky_note_modified.emit(item.note_id(), old_data, new_data)

    def _on_sticky_note_changed(self, item: StickyNoteItem) -> None:
        """Handle sticky note change (move, resize, edit, delete)."""
        # Check if the item has been removed (delete via context menu)
        if item.scene() is None:
            note_data = item.to_dict()
            if self._node_graph is not None:
                self._node_graph.remove_sticky_note(item.note_id())
            self._sticky_note_items.pop(item.note_id(), None)
            self.sticky_note_deleted.emit(item.note_id(), note_data)
        else:
            self._sync_sticky_note_to_graph(item)
        self.graph_changed.emit()

    def _sync_sticky_note_to_graph(self, item: StickyNoteItem) -> None:
        """Persist sticky note metadata to the graph."""
        if self._node_graph is None:
            return
        self._node_graph.add_sticky_note(item.note_id(), item.to_dict())

    def _find_sticky_note_from_item(self, item: Optional[QGraphicsItem]) -> Optional[StickyNoteItem]:
        """Return the sticky note item for a scene item or one of its parents."""
        current = item
        while current is not None:
            if isinstance(current, StickyNoteItem):
                return current
            current = current.parentItem()
        return None

    def add_connection_visual(self, src_node_id: str, src_port_name: str,
                              dst_node_id: str, dst_port_name: str,
                              data_type: str = "image") -> None:
        """Add a visual connection between two ports."""
        src_item = self._node_items.get(src_node_id)
        dst_item = self._node_items.get(dst_node_id)
        if not src_item or not dst_item:
            return

        src_port = src_item.output_ports.get(src_port_name)
        dst_port = dst_item.input_ports.get(dst_port_name)
        if not src_port or not dst_port:
            return

        conn = ConnectionItem(src_port, dst_port, data_type)
        self._scene.addItem(conn)

        src_item.connections.append(conn)
        dst_item.connections.append(conn)
        src_port.increment_connection()
        dst_port.increment_connection()

        self._connection_items.append((conn, src_node_id, src_port_name, dst_node_id, dst_port_name))
        conn.update_path()

        # Connect signals for proper data model updates and undo support
        conn.signals().delete_requested.connect(self._on_connection_delete_requested)
        conn.signals().reconnect_requested.connect(self._on_connection_reconnect_requested)
        conn.signals().disconnect_requested.connect(self._on_connection_disconnect_requested)

        # Start flow animation
        conn.start_flow_animation()

    def _remove_connection_item(self, conn: ConnectionItem) -> None:
        """Remove a connection visual item and reset port states."""
        conn.stop_flow_animation()
        # 端口连接计数：用 decrement_connection 让计数器自动管理视觉状态，
        # 无需全表扫描判断「是否还有其它连接」（多输入端口也能正确归零）。
        src_port = conn.src_port
        dst_port = conn.dst_port

        if src_port:
            src_port.decrement_connection()

        if dst_port:
            dst_port.decrement_connection()

        self._scene.removeItem(conn)
        # Remove from node connection lists
        for node_id, item in self._node_items.items():
            if conn in item.connections:
                item.connections.remove(conn)
        # Remove from tracking
        self._connection_items = [c for c in self._connection_items if c[0] is not conn]

    def remove_connection_visual_by_dst(self, dst_node_id: str, dst_port_name: str) -> None:
        """Remove the visual connection going into a specific input port."""
        for conn_data in list(self._connection_items):
            conn_item, src_nid, src_pname, dst_nid, dst_pname = conn_data
            if dst_nid == dst_node_id and dst_pname == dst_port_name:
                self._remove_connection_item(conn_item)
                return

    def remove_connection_visual_exact(self, src_node_id: str, src_port_name: str,
                                       dst_node_id: str, dst_port_name: str) -> bool:
        """Remove only the visual connection matching the full quad (U-15).

        ``remove_connection_visual_by_dst`` 只按目标端口删第一条：多输入
        端口（Scene3D.in 等）上会误删其他来源的视觉线，产生幽灵线/丢正常
        线。所有断线与 undo/redo 恢复必须用本方法按 (src,src_port,dst,
        dst_port) 精确删除。
        """
        for conn_data in list(self._connection_items):
            conn_item, sn, sp, dn, dp = conn_data
            if (sn == src_node_id and sp == src_port_name
                    and dn == dst_node_id and dp == dst_port_name):
                self._remove_connection_item(conn_item)
                return True
        return False

    # ── Connection Signal Handlers ──

    def _on_connection_delete_requested(self, conn: ConnectionItem) -> None:
        """Handle connection delete request from ConnectionItem.

        Emits node_disconnected signal for CompositePage to handle
        with proper undo/redo support and data model updates.
        CompositePage is responsible for visual cleanup.
        """
        # Find connection data in tracking list
        conn_data = None
        for c in self._connection_items:
            if c[0] is conn:
                conn_data = c
                break

        if conn_data:
            _, src_nid, src_pname, dst_nid, dst_pname = conn_data
            if (self.is_node_locked(src_nid)
                    or self.is_node_locked(dst_nid)):
                return
            # Emit signal for CompositePage to handle with undo support
            # CompositePage handles both data model update and visual cleanup
            self.node_disconnected.emit(src_nid, src_pname, dst_nid, dst_pname)

    def _on_connection_disconnect_requested(self, conn: ConnectionItem) -> None:
        """Handle connection disconnect request (Alt+drag without valid target).

        This is similar to delete, but specifically triggered when user
        Alt+drags a connection endpoint to an invalid location.
        """
        self._on_connection_delete_requested(conn)

    def _on_connection_reconnect_requested(self, conn: ConnectionItem,
                                            target_port: PortItem,
                                            dragging_src: bool) -> None:
        """Handle connection reconnect request from ConnectionItem.

        Emits appropriate disconnect/connect signals for CompositePage
        to handle with proper undo/redo support and data model updates.

        Args:
            conn: The connection being modified
            target_port: The new port to connect to
            dragging_src: True if dragging the source end, False for destination
        """

        # Find current connection data
        conn_data = None
        for c in self._connection_items:
            if c[0] is conn:
                conn_data = c
                break

        if not conn_data:
            return

        _, old_src_nid, old_src_pname, old_dst_nid, old_dst_pname = conn_data

        # Reconnecting mutates both the existing edge and one of its
        # endpoints.  A locked endpoint is read-only even when the drag was
        # started from the connection curve rather than its port.
        if (self.is_node_locked(old_src_nid)
                or self.is_node_locked(old_dst_nid)):
            return

        # Get target node and port info
        target_node_item = target_port.parentItem()
        if not target_node_item:
            return

        # Find target node ID from node items
        target_node_id = None
        for nid, item in self._node_items.items():
            if item is target_node_item:
                target_node_id = nid
                break

        if not target_node_id:
            return

        if self.is_node_locked(target_node_id):
            return

        target_port_name = target_port.port_name

        # Determine which connection changes to make
        if dragging_src:
            # Reconnecting source: old_src -> target as new src
            # First disconnect old connection (CompositePage handles visual cleanup)
            self.node_disconnected.emit(old_src_nid, old_src_pname, old_dst_nid, old_dst_pname)
            # Then connect with new source (CompositePage handles visual creation)
            self.node_connected.emit(target_node_id, target_port_name, old_dst_nid, old_dst_pname)
        else:
            # Reconnecting destination: target becomes new dst
            # First disconnect old connection (CompositePage handles visual cleanup)
            self.node_disconnected.emit(old_src_nid, old_src_pname, old_dst_nid, old_dst_pname)
            # Then connect with new destination (CompositePage handles visual creation)
            self.node_connected.emit(old_src_nid, old_src_pname, target_node_id, target_port_name)

    def clear_all(self) -> None:
        """Remove all nodes and connections."""
        # Stop flow animations before clearing scene to prevent
        # timer callbacks on deleted C++ objects
        for conn_data in self._connection_items:
            conn_data[0].stop_flow_animation()
        self._branch_isolation_ids = None
        self._dependency_highlight_ids.clear()
        # Drop Python references BEFORE scene.clear(): removing selected items
        # fires selectionChanged, which re-enters _apply_preview_mode and would
        # dereference already-deleted C++ objects via _node_items.
        self._node_items.clear()
        self._backdrop_items.clear()
        self._sticky_note_items.clear()
        self._connection_items.clear()
        # Properly detach the preview connection (from the flow-animation driver
        # AND the scene) before scene.clear() destroys its endpoints — otherwise
        # the driver would tick a deleted C++ object on its next timer fire.
        self._clear_preview_connection()
        self._scene.clear()

    def reset_session_state(self) -> None:
        """Reset cross-graph transient state before loading another graph.

        ``clear_all`` 有意保留分组过滤/锁/书签——同图 undo 重建依赖它们
        维持视图语境。但跨图 load 必须清空这些会话态（U-09/U-13）：
        锁集合、分组视图栈与过滤、隔离/高亮、导航历史都属于上一张图
        的 UI schema，残留会让新合成节点全部隐藏或不可编辑。
        """
        self._locked_node_ids = set()
        self._group_view_stack = []
        had_filter = self._group_filter_ids is not None
        self._group_filter_ids = None
        self._branch_isolation_ids = None
        self._dependency_highlight_ids.clear()
        self._navigation = GraphNavigationState()
        self._selected_node_id = None
        self._viewing_node_id = None
        self._highlighted_node_ids = []
        if had_filter:
            # 同步面包屑/分组导航 UI 回到 root。
            self.group_view_changed.emit(None)

    def build_from_graph(self, graph: NodeGraph) -> None:
        """Build visuals from a NodeGraph."""
        self._node_graph = graph
        self._actions = NodeEditorActions(self, graph)
        self._layout = GraphLayout(graph)
        viewport_state = getattr(graph, "_viewport_state", {}) or {}
        raw_locked = viewport_state.get("locked_node_ids", [])
        self._locked_node_ids = {
            str(node_id) for node_id in raw_locked
            if str(node_id) in graph.nodes
        } if isinstance(raw_locked, (list, tuple, set)) else set()
        self._navigation = GraphNavigationState.from_dict(viewport_state)
        self._navigation.clear_history()
        self.clear_all()
        for backdrop_data in graph.backdrops.values():
            self.add_backdrop_visual(backdrop_data)
        for sticky_note_data in graph.get_sticky_notes().values():
            self.add_sticky_note_visual(sticky_note_data)
        # Batch the node-add loop so add_node_visual defers its per-add
        # preview-mode and backdrop work (each applied once, below).
        self._batch_mode = True
        try:
            for node in graph.nodes.values():
                self.add_node_visual(node)
        finally:
            self._batch_mode = False
        for conn in graph.connections:
            self.add_connection_visual(
                conn.src_node_id, conn.src_port_name,
                conn.dst_node_id, conn.dst_port_name,
            )
        self._refresh_all_backdrop_containment()
        # Restore UI state (viewport and viewer) after building visuals.
        # If no saved viewport state exists, center on all nodes.
        has_saved_viewport = bool(graph._viewport_state)
        self._navigation_restore_active = True
        try:
            self.restore_ui_state()
        finally:
            self._navigation_restore_active = False
        if not has_saved_viewport and self._node_items:
            # Defer fit-all until the widget has a valid size;
            # otherwise fitInView uses zero geometry.
            QTimer.singleShot(0, self.fit_all_nodes)
        # Apply preview-mode visibility once after the full build (covers any
        # selection restored by restore_ui_state()).
        self._apply_preview_mode()
        # A canvas rebuild (undo/restore of external edits, script ops)
        # recreates every item VISIBLE — re-apply the group filter or the
        # view silently jumps out of the group while the breadcrumb still
        # claims we are inside it.
        if self._group_filter_ids is not None:
            self._apply_group_filter()
        self._record_navigation_point()

    # ── Interaction ──

    # ── File Drag & Drop ──

    @staticmethod
    def _media_pool_asset_ids(mime_data: Any) -> List[str]:
        """Parse asset ids from a media-pool drag; [] for any other drag.

        AUD-10503: the media pool drags a custom MIME payload
        (``application/x-topos-media-pool-assets``, JSON list of asset ids).
        The custom MIME must be checked BEFORE falling back to URL handling,
        otherwise pool assets with local paths degrade to plain file imports
        (asset linkage lost) and pool assets without paths are rejected.
        """
        if mime_data is None or not mime_data.hasFormat(MIME_TYPE_MEDIA_POOL_ASSETS):
            return []
        from src.features.composite.core.media_drop_payload import (
            parse_media_pool_mime,
        )
        try:
            return parse_media_pool_mime(mime_data)
        except Exception:
            logger.exception("Failed to parse media-pool drag payload")
            return []

    def dragEnterEvent(self, event) -> None:
        """Accept media-pool asset drags and file drops（image/video/3D model）。"""
        # AUD-10503: pool drags first (see _media_pool_asset_ids).
        if self._media_pool_asset_ids(event.mimeData()):
            event.acceptProposedAction()
            return
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile():
                    # 用统一的 is_media() 判定（含 image/video/model）
                    if _is_media(url.toLocalFile()):
                        event.acceptProposedAction()
                        return
        event.ignore()

    def dragMoveEvent(self, event) -> None:
        """Keep accepting during drag move."""
        if (self._media_pool_asset_ids(event.mimeData())
                or event.mimeData().hasUrls()):
            event.acceptProposedAction()
        else:
            event.ignore()

    @staticmethod
    def _probe_video_metadata(path: str) -> Optional[Dict[str, Any]]:
        """Probe lightweight video metadata for a dropped file.

        Returns the video_frame node parameter keys (fps/total_frames/
        video_width/video_height) or None when probing fails — the drop
        still creates the node, just without an instant media length.
        """
        try:
            from src.shared.media.factory import MediaSourceFactory
            meta = MediaSourceFactory.probe_video_metadata(path)
            if meta is None:
                return None
            try:
                fps = float(meta.fps.to_fps())
            except (AttributeError, TypeError, ValueError):
                fps = float(getattr(meta, "fps", 0.0) or 0.0)
            return {
                "fps": fps,
                "total_frames": int(getattr(meta, "total_frames", 0) or 0),
                "video_width": int(getattr(meta, "width", 0) or 0),
                "video_height": int(getattr(meta, "height", 0) or 0),
            }
        except Exception as e:
            logging.getLogger(__name__).debug(
                f"Video metadata probe failed for {path}: {e}")
            return None

    def dropEvent(self, event) -> None:
        """Handle dropped files / media-pool assets.

        P2：3D 模型拖拽改为建分裂图（Scene3D + Model3D），避免新项目绕过
        分裂架构导致 gizmo/路由错乱。Camera/Light 使用 Scene3D fallback；
        普通模型不自动创建默认 Geo3D。

        AUD-10503：媒体池拖拽（自定义 MIME）→ 发射 assets_dropped，由页面
        创建 asset-backed 节点；未命中再走 URL 文件导入。
        AUD-10502：视频元数据探测移出 UI 线程（见 _schedule_video_metadata_probe）。
        """
        # Qt6/PyQt6: QDropEvent.pos() is gone — position() is the successor
        # (QPointF); QGraphicsView.mapToScene takes a QPoint. (Calling
        # .pos() here raised AttributeError, so the whole drop path was
        # dead under PyQt6.)
        scene_pos = self.mapToScene(event.position().toPoint())
        asset_ids = self._media_pool_asset_ids(event.mimeData())
        if asset_ids:
            self.assets_dropped.emit(asset_ids, (scene_pos.x(), scene_pos.y()))
            event.acceptProposedAction()
            return

        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            ext = os.path.splitext(path)[1].lower()

            # 按扩展名路由到对应节点类型
            if ext in _IMAGE_EXTENSIONS:
                node_type = "image_input"
                params = {"file_path": path}
            elif ext in _VIDEO_EXTENSIONS:
                node_type = "video_frame"
                params = {"file_path": path}
                # Probe real media metadata in the background so the
                # Keyframes panel can show the full source length soon after
                # the drop (the render-thread node instance also discovers
                # it later, on first open). AUD-10502: probing synchronously
                # here opened the demuxer (PyAV/ffmpeg) on the UI thread and
                # froze the canvas for multi-file/network/corrupt drops.
                self._schedule_video_metadata_probe(path)
            elif ext in _MODEL_3D_EXTENSIONS:
                # 3D 模型 → 分裂图（Scene3D + 子节点），与迁移路径一致
                if self._actions:
                    self._actions.create_3d_scene_with_children(
                        scene_pos.x(), scene_pos.y(), model_path=path)
                scene_pos = QPointF(scene_pos.x() + 220, scene_pos.y())
                continue
            else:
                continue

            if self._actions:
                self._actions.add_node(node_type, scene_pos.x(), scene_pos.y(),
                                       parameters=params)
                scene_pos = QPointF(scene_pos.x() + 220, scene_pos.y())
        event.acceptProposedAction()

    def _schedule_video_metadata_probe(self, path: str) -> None:
        """Probe dropped-video metadata off the UI thread (AUD-10502).

        The node is created immediately with just ``file_path``; the
        discovered fps/length/size are back-filled through
        :attr:`video_metadata_probed` (queued to the UI thread) once the
        probe finishes. Daemon threads keep a hung probe (network/corrupt
        file) from blocking application shutdown.
        """
        path = str(path)

        def _work() -> None:
            meta = self._probe_video_metadata(path)
            if meta is None:
                return
            try:
                # Cross-thread emit is delivered as a queued connection.
                self.video_metadata_probed.emit(path, meta)
            except RuntimeError:
                # Widget already destroyed (project closed during probe).
                pass

        try:
            threading.Thread(
                target=_work, name="node-video-probe", daemon=True,
            ).start()
        except RuntimeError:
            # Interpreter shutting down — nothing left to backfill.
            pass

    def _on_video_metadata_probed(self, path: str, meta: Dict[str, Any]) -> None:
        """Backfill probed metadata onto the video_frame node for ``path``."""
        if self._node_graph is None or not meta:
            return
        for node in list(self._node_graph.nodes.values()):
            if (getattr(node, "node_type", "") != "video_frame"
                    or node.parameters.get("file_path") != path):
                continue
            if node.parameters.get("total_frames"):
                # Already discovered elsewhere (e.g. render thread) — keep it.
                return
            try:
                for key, value in meta.items():
                    node.set_parameter(key, value)
            except Exception:
                logger.exception(
                    "Failed to backfill video metadata for %r", path)
                return
            self.graph_changed.emit()
            return

    def scrollContentsBy(self, dx, dy) -> None:
        """Track viewport changes for minimap."""
        super().scrollContentsBy(dx, dy)
        self._record_navigation_point()
        self.viewport_moved.emit()

    def wheelEvent(self, event: QWheelEvent) -> None:
        """Zoom with scroll wheel (with limits)."""
        factor = 1.15
        if event.angleDelta().y() > 0:
            new_zoom = self._current_zoom * factor
        else:
            new_zoom = self._current_zoom / factor

        # Clamp to zoom limits
        new_zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, new_zoom))

        if abs(new_zoom - self._current_zoom) < 0.001:
            return

        self._current_zoom = new_zoom
        self.setTransform(QTransform().scale(new_zoom, new_zoom))
        self.zoom_changed.emit(new_zoom)
        self._record_navigation_point()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Handle mouse press."""
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.pos())

            # Check for insertion dot click first (Cmd/Ctrl + click on connection midpoint)
            if self._ctrl_insertion_active and self._handle_insertion_dot_click(scene_pos):
                return

            # Check if we clicked on a port
            port_item = self._find_port_at(scene_pos)

            if port_item:
                parent_node = port_item.parentItem()
                if isinstance(parent_node, NodeItem) and parent_node.is_locked:
                    # A locked node remains inspectable/selectable, but its
                    # connection endpoints are read-only as well.
                    return
                if port_item.direction == "output":
                    self._start_connection_drag(port_item, scene_pos)
                    return
                elif port_item.direction == "input" and port_item._connected:
                    # 点击已连输入端口 → 断开重拖（恢复基线行为）。
                    # 多输入端口的「追加连接」改由从源节点 output 拖到 input 实现
                    # （点击 input 是断开语义，从 output 拖是追加语义，两者互不冲突）。
                    self._disconnect_and_drag(port_item, scene_pos)
                    return
                elif port_item.direction == "input" and not port_item._connected:
                    # Reverse drag: drag from empty input port to find an output
                    self._start_reverse_connection_drag(port_item, scene_pos)
                    return

            # Check if we clicked on a node
            clicked_item = self.itemAt(event.pos())
            node_item = None
            if isinstance(clicked_item, NodeItem):
                node_item = clicked_item
            elif clicked_item and isinstance(clicked_item.parentItem(), NodeItem):
                node_item = clicked_item.parentItem()

            backdrop_item = self._find_backdrop_from_item(clicked_item)
            sticky_item = self._find_sticky_note_from_item(clicked_item)
            if node_item:
                # Dragging a node must not co-drag a selected backdrop (Qt moves
                # all selected movable items together); node/backdrop selection
                # are mutually exclusive.
                self._clear_backdrop_selection()
                self._clear_sticky_selection()
                if self._handle_node_click(node_item, event):
                    # AUD-10504: the manual Ctrl/Shift selection consumed the
                    # press — do not also hand it to the scene, whose own
                    # (version-dependent) Ctrl/Shift item-selection semantics
                    # would re-apply on the already-toggled state.
                    return
            elif backdrop_item:
                for item in self._node_items.values():
                    item.set_selected(False)
                self._clear_sticky_selection()
                self._selected_node_id = None
                self.selection_cleared.emit()
                super().mousePressEvent(event)
                return
            elif sticky_item:
                # Let the sticky note handle its own press (move via drag, resize
                # via the corner handle) instead of starting a rubber-band box,
                # which would otherwise swallow the move events.
                for item in self._node_items.values():
                    item.set_selected(False)
                self._clear_backdrop_selection()
                self._selected_node_id = None
                self.selection_cleared.emit()
                super().mousePressEvent(event)
                return
            elif not self._space_held:
                # Start rubber band selection
                self._rubber_band_active = True
                # Box-select must not trigger node previews.
                self._preview_selection_source = "rubber"
                self._rubber_band_start = scene_pos
                # Deselect all
                for item in self._node_items.values():
                    item.set_selected(False)
                self._clear_backdrop_selection()
                self._clear_sticky_selection()
                self._selected_node_id = None
                self.selection_cleared.emit()

            # Track move start positions for undo
            if node_item:
                self._move_start_positions = {
                    nid: item.pos()
                    for nid, item in self._node_items.items()
                    if item.isSelected()
                }
                self._moving_nodes = True
                # Initialize shake detector for single-node drag only
                if len(self._move_start_positions) == 1:
                    sens = self._get_shake_sensitivity()
                    threshold, min_delta = _shake_params_from_sensitivity(sens)
                    self._shake_detector = ShakeDetector(
                        reversal_threshold=threshold, min_delta_x=min_delta
                    )
                # Pause flow animation during drag to reduce repaint overhead
                from .connection_item import _get_flow_driver
                _get_flow_driver().set_slow_mode(True)

        if event.button() == Qt.MouseButton.MiddleButton:
            self._start_pan(event)
            return

        if self._space_held and event.button() == Qt.MouseButton.LeftButton:
            self._start_pan(event)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """Handle mouse move for connection dragging, port snapping, and rubber band."""
        if self._dragging_connection and self._temp_connection:
            scene_pos = self.mapToScene(event.pos())

            # Find nearest valid port for snapping
            snap_target = self._find_nearest_valid_port(scene_pos)

            # Clear previous snap highlight (port)
            if self._snap_port and self._snap_port is not snap_target:
                self._snap_port.set_snap_highlight(False)

            if self._drag_direction == "forward" and self._drag_src_port:
                src_pos = self._drag_src_port.get_center_scene_pos()
                if snap_target:
                    # Snap to port center
                    self._snap_port = snap_target
                    snap_target.set_snap_highlight(True, True)
                    self._temp_connection.set_drag_validity(True)
                    self._temp_connection.set_temp_endpoints(src_pos, snap_target.get_center_scene_pos())
                else:
                    self._snap_port = None
                    self._temp_connection.set_drag_validity(True)
                    self._temp_connection.set_temp_endpoints(src_pos, scene_pos)

            elif self._drag_direction == "reverse" and self._drag_dst_port:
                dst_pos = self._drag_dst_port.get_center_scene_pos()
                if snap_target:
                    self._snap_port = snap_target
                    snap_target.set_snap_highlight(True, True)
                    self._temp_connection.set_drag_validity(True)
                    self._temp_connection.set_temp_endpoints(snap_target.get_center_scene_pos(), dst_pos)
                else:
                    self._snap_port = None
                    self._temp_connection.set_drag_validity(True)
                    self._temp_connection.set_temp_endpoints(scene_pos, dst_pos)

            return

        if self._rubber_band_active and self._rubber_band_start is not None:
            scene_pos = self.mapToScene(event.pos())
            self._rubber_band_current_pos = scene_pos  # Track for HiDPI accuracy in drawForeground
            self._update_rubber_band_selection(scene_pos)
            return

        if self._mid_button_panning and self._pan_start is not None:
            # Direct pan using centerOn delta
            current_pos = event.position()
            delta_view = current_pos - self._pan_start
            # Scale delta by current zoom
            delta_scene = QPointF(delta_view.x() / self._current_zoom,
                                   delta_view.y() / self._current_zoom)
            self.centerOn(self._pan_start_center - delta_scene)
            return

        # Update insertion dot hover when Cmd/Ctrl is held
        if self._ctrl_insertion_active and not self._dragging_connection and not self._moving_nodes:
            scene_pos = self.mapToScene(event.pos())
            self._update_insertion_dot_hover(scene_pos)

        # Shake-to-disconnect detection during node drag
        if self._moving_nodes and self._shake_detector and not self._shake_disconnecting:
            scene_pos = self.mapToScene(event.pos())
            current_time = time.time() * 1000  # Convert to milliseconds
            if self._shake_detector.add_sample(current_time, scene_pos.x()):
                self._execute_shake_disconnect()
            elif not self._shake_triggered:
                # Only update glow if shake hasn't triggered yet
                self._update_shake_glow()

        # Drop-on-connection proximity detection during node drag (time-throttled)
        if self._moving_nodes and not self._shake_triggered and not self._shake_disconnecting:
            now = time.time() * 1000
            if now - self._proximity_last_time >= 80:  # Max ~12 checks/sec
                self._proximity_last_time = now
                self._detect_connection_proximity()

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Handle mouse release."""
        if event.button() == Qt.MouseButton.LeftButton and self._dragging_connection:
            self._finish_connection_drag(event)
            return

        if event.button() == Qt.MouseButton.LeftButton and self._rubber_band_active:
            self._rubber_band_active = False
            self._rubber_band_start = None
            self._rubber_band_current_pos = None  # Clear tracked position
            # Finalize selection using the unified signal emission method
            self._emit_selection_signal()
            self.viewport().update()  # Force repaint to clear rubber band overlay
            return

        if event.button() == Qt.MouseButton.LeftButton and self._moving_nodes:
            # Check for drop-on-connection insertion before finalizing move
            if self._drop_insert_connection:
                self._execute_connection_insertion()

            # Emit move info for undo
            moved_ids = []
            old_positions = []
            for nid, old_pos in self._move_start_positions.items():
                item = self._node_items.get(nid)
                if item and item.pos() != old_pos:
                    # Snap to grid if enabled
                    if self._snap_to_grid:
                        pos = item.pos()
                        snapped_x = round(pos.x() / self.GRID_SIZE) * self.GRID_SIZE
                        snapped_y = round(pos.y() / self.GRID_SIZE) * self.GRID_SIZE
                        item.setPos(snapped_x, snapped_y)
                    moved_ids.append(nid)
                    old_positions.append((old_pos.x(), old_pos.y()))
            if moved_ids:
                self.nodes_moved.emit(moved_ids, old_positions)
            self._moving_nodes = False
            self._move_start_positions.clear()
            self._update_graph_positions()

            # Cleanup shake state and clear glow on any affected node
            self._shake_detector = None
            self._shake_triggered = False
            self._shake_disconnecting = False
            self._proximity_frame_counter = 0
            self._proximity_last_check_pos = None
            self._proximity_last_time = 0.0
            self._last_shake_glow_value = -1.0
            for item in self._node_items.values():
                if getattr(item, '_shake_glow', 0) > 0:
                    item._shake_glow = 0.0
                    item.update()

            # Clear connection highlight
            self._clear_connection_highlight()

            # Resume flow animation after drag ends
            from .connection_item import _get_flow_driver
            _get_flow_driver().set_slow_mode(False)

        if event.button() == Qt.MouseButton.MiddleButton and self._mid_button_panning:
            self._mid_button_panning = False
            self._pan_start = None
            self._pan_start_center = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        if self._space_held and event.button() == Qt.MouseButton.LeftButton and self._mid_button_panning:
            self._mid_button_panning = False
            self._pan_start = None
            self._pan_start_center = None
            return

        super().mouseReleaseEvent(event)

    def _handle_node_click(self, node_item: NodeItem,
                           event: QMouseEvent) -> bool:
        """Handle clicking on a node. Returns True if the event is consumed.

        AUD-10504: the Ctrl/Shift multi-select path is applied manually AND
        consumes the press — forwarding the same press to the scene afterwards
        let Qt apply its own (version-dependent) Ctrl/Shift item-selection
        semantics on top of the already-toggled state, which can bounce the
        visual selection back while the emitted signal reported the manual
        state. Consuming keeps exactly one application and one signal, both
        derived from the same state. (Qt 6's own default handler ignores
        Ctrl presses on items entirely, so nothing is lost by not
        forwarding.)
        """
        # A direct node click is the only selection source that may show a
        # thumbnail preview in SELECTED_SINGLE mode.
        self._preview_selection_source = "click"
        if event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                | Qt.KeyboardModifier.ShiftModifier):
            if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Toggle selection with Ctrl
                node_item.set_selected(not node_item.isSelected())
            else:
                # Add to selection with Shift
                node_item.set_selected(True)
            self._emit_selection_signal()
            self._apply_preview_mode()
            return True
        elif node_item.isSelected():
            # Clicking an already-selected node: keep multi-selection for batch drag
            self._selected_node_id = node_item.node_id
            # 如果有多个选中节点，发送 nodes_selected 信号
            selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
            if len(selected_ids) > 1:
                self.nodes_selected.emit(selected_ids)
            else:
                self.node_selected.emit(node_item.node_id)
        else:
            # Clicking an unselected node: single select
            self._selected_node_id = node_item.node_id
            for nid, item in self._node_items.items():
                item.set_selected(nid == node_item.node_id)
            self.node_selected.emit(node_item.node_id)

        # Re-evaluate preview for the new "click" source. Needed in particular
        # when clicking an already-selected node (no selectionChanged fires).
        self._apply_preview_mode()
        return False

    def _emit_selection_signal(self) -> None:
        """根据当前选中状态发送适当的信号。"""
        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if len(selected_ids) == 0:
            self._selected_node_id = None
            self.selection_cleared.emit()
        elif len(selected_ids) == 1:
            self._selected_node_id = selected_ids[0]
            self.node_selected.emit(selected_ids[0])
        else:
            self._selected_node_id = selected_ids[0]  # 主选中节点
            self.nodes_selected.emit(selected_ids)

    def _update_rubber_band_selection(self, scene_pos: QPointF) -> None:
        """Update rubber band selection rectangle."""
        # Keep marking the source as rubber-band so the live selection changes
        # during the drag never light up a node preview.
        self._preview_selection_source = "rubber"
        if self._rubber_band_start is None:
            return

        rect = QRectF(
            min(self._rubber_band_start.x(), scene_pos.x()),
            min(self._rubber_band_start.y(), scene_pos.y()),
            abs(scene_pos.x() - self._rubber_band_start.x()),
            abs(scene_pos.y() - self._rubber_band_start.y()),
        )

        # Select nodes that intersect the rubber band
        for nid, item in self._node_items.items():
            item_rect = item.sceneBoundingRect()
            item.set_selected(rect.intersects(item_rect))

        # Update the viewport to show the selection rectangle
        self.viewport().update()

    def _start_pan(self, event: QMouseEvent) -> None:
        """Start panning with direct centerOn approach."""
        self._mid_button_panning = True
        self._pan_start = event.position()
        self._pan_start_center = self.mapToScene(self.viewport().rect().center())
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    # ── Node Preview Mode ──

    def _resolve_preview_mode(self) -> str:
        """Lazily read the persisted preview mode from shared config.

        ``self.window()`` (and thus the ConfigManager) is not available during
        ``__init__``, so the persisted value is read on first use. Falls back to
        ``PREVIEW_SELECTED_SINGLE`` if config is unavailable or holds an unknown
        value. The result is cached for subsequent calls.
        """
        if self._preview_mode_resolved:
            return self._preview_mode
        self._preview_mode_resolved = True
        try:
            cm = getattr(self.window(), "config_manager", None)
            if cm is not None:
                val = cm.get(self.PREVIEW_CONFIG_KEY)
                if val in self.PREVIEW_MODES:
                    self._preview_mode = val
        except Exception:
            pass
        return self._preview_mode

    def _set_node_preview_mode(self, mode: str) -> None:
        """Change the node thumbnail preview mode and persist it.

        Repaints only when the mode actually changes.
        """
        if mode not in self.PREVIEW_MODES:
            return
        changed = (mode != self._preview_mode)
        self._preview_mode = mode
        self._preview_mode_resolved = True
        # Persist (mirrors the shake-sensitivity config pattern).
        try:
            cm = getattr(self.window(), "config_manager", None)
            if cm is not None:
                cm.set(self.PREVIEW_CONFIG_KEY, mode)
        except Exception as e:
            logger.debug("Failed to persist preview mode: %s", e)
        if changed:
            self._apply_preview_mode()

    def _apply_preview_mode(self) -> None:
        """Push per-node thumbnail visibility for the current preview mode.

        - PREVIEW_NONE            → no node shows a thumbnail.
        - PREVIEW_SELECTED_SINGLE → only the sole selected node shows one
          (box/multi-select shows none).
        - PREVIEW_ALL             → every node shows one (when it has data).

        ``NodeItem.set_thumbnail_visible`` is a no-op when unchanged, so only
        nodes whose visibility actually flips get re-laid-out.
        """
        mode = self._resolve_preview_mode()
        selected = [nid for nid, it in self._node_items.items() if it.isSelected()]
        single_id = selected[0] if len(selected) == 1 else None

        for nid, item in self._node_items.items():
            if mode == self.PREVIEW_NONE:
                target = False
            elif mode == self.PREVIEW_ALL:
                target = True
            else:  # PREVIEW_SELECTED_SINGLE
                # Only a direct node click lights up a preview; box-select
                # (even when it catches exactly one node) never does.
                target = (nid == single_id) and self._preview_selection_source == "click"
            item.set_thumbnail_visible(target)

    def _on_selection_changed_for_preview(self) -> None:
        """Recompute preview visibility when the scene selection changes.

        Covers every selection source (click, Ctrl/Shift-click, rubber-band,
        Ctrl-A, upstream/downstream, keyboard nav, undo/redo restore).
        """
        self._apply_preview_mode()

    # ── Node-Preview Pipeline (viewer shows a specific node's output) ───────

    def set_preview_node(self, node_id: Optional[str]) -> None:
        """Show/clear the dashed preview connection from a node to the Output.

        Called by CompositePage when node-preview is toggled (the "1" shortcut).
        ``node_id`` is the node being previewed, or None to return to Output.
        Redraws the dashed curve; a no-op if the target node or Output node
        isn't currently on the canvas (graceful — the page retries on the next
        rebuild).
        """
        # Tear down any existing preview connection first.
        self._clear_preview_connection()

        if not node_id:
            return

        src_item = self._node_items.get(node_id)
        if src_item is None:
            return
        dst_item = self._find_output_node_item()
        if dst_item is None or dst_item is src_item:
            return

        conn = PreviewConnectionItem(src_item, dst_item)
        self._scene.addItem(conn)
        self._preview_connection = conn
        # Register on both endpoints so itemChange() follows either move.
        src_item._preview_connections.append(conn)
        dst_item._preview_connections.append(conn)

    def _clear_preview_connection(self) -> None:
        """Remove the current preview connection, if any."""
        conn = self._preview_connection
        if conn is None:
            return
        # Detach from endpoint NodeItems.
        for endpoint in (conn._src_item, conn._dst_item):
            if endpoint is not None:
                lst = getattr(endpoint, "_preview_connections", None)
                if lst is not None and conn in lst:
                    lst.remove(conn)
        conn.remove_from_scene()
        self._preview_connection = None

    def _find_output_node_item(self) -> Optional[NodeItem]:
        """Return the NodeItem for the (single) Output node, or None.

        AUD-10500: identity is ``node_type`` ("output") from the graph (or
        the item itself), never the display title — the title changes when
        the user renames the node (F2), which used to silently disable the
        preview pipeline. The title is consulted only in graph-less
        contexts (manually registered items, tests), where renaming is
        impossible anyway.
        """
        for nid, item in self._node_items.items():
            if self._is_protected_node_type(nid, "output"):
                return item
        for item in self._node_items.values():
            if getattr(item, "node_type", "") == "output":
                return item
        if self._node_graph is None:
            for item in self._node_items.values():
                if getattr(item, "node_title", "") == "Output":
                    return item
        return None

    def _is_protected_node_type(self, node_id: str, *node_types: str) -> bool:
        """Return True if the graph node's node_type matches one of ``node_types``.

        AUD-10500/AUD-10408: COLOR/Output protection and lookup must be
        judged by ``node.node_type`` from the graph; the user-editable
        display title (``node_title``) is display-only and a rename used to
        bypass every title-based guard.
        """
        if not node_types:
            node_types = ("color_input", "output")
        if self._node_graph is None:
            return False
        node = self._node_graph.get_node(str(node_id))
        return (node is not None
                and getattr(node, "node_type", "") in node_types)

    def _build_preview_submenu(self, menu: QMenu) -> dict:
        """Build the 'Preview' submenu; returns {mode: QAction}.

        Actions are checkable (radio-like, exactly one checked = current mode).
        Labels are localized via i18n (``composite.node_preview.*``); the inline
        English default is a last-resort fallback if a key is missing.
        """
        from src.shared.i18n.i18n import I18N

        def tr(key: str, default: str) -> str:
            return I18N.tr(f"composite.node_preview.{key}", default)

        mode = self._resolve_preview_mode()
        submenu = menu.addMenu(tr("title", "Preview"))
        labels = [
            (self.PREVIEW_NONE, tr("none", "None")),
            (self.PREVIEW_SELECTED_SINGLE, tr("selected_single", "Selected (Single)")),
            (self.PREVIEW_ALL, tr("all", "All")),
        ]
        actions: dict = {}
        for m, label in labels:
            act = submenu.addAction(label)
            act.setCheckable(True)
            act.setChecked(m == mode)
            actions[m] = act
        return actions

    # ── Context Menu Helpers ──

    def _build_node_category_menu(self, parent_menu: QMenu) -> Dict[str, QAction]:
        """Build categorized node type submenus.

        Returns:
            Dict mapping node_type to QAction for action handling.
        """
        actions = {}
        seen_types = set()

        for category, type_list in NODE_CATEGORIES.items():
            submenu = parent_menu.addMenu(category)

            for node_type, node_cls in type_list:
                if node_type in seen_types:
                    continue
                if node_type == "color_input":
                    continue  # COLOR node is auto-created
                if node_type == "blend":
                    continue  # Legacy alias (shown as "merge")
                if node_cls is None:
                    continue  # Placeholder not yet implemented
                seen_types.add(node_type)

                title = node_cls.node_title
                # Mark placeholder nodes with warning icon and suffix
                is_placeholder = getattr(node_cls, 'IS_PLACEHOLDER', False)
                if is_placeholder:
                    title += " (N/A)"
                    action = submenu.addAction(title)
                    # Add warning icon for placeholder nodes
                    from src.shared.assets.icon_manager import get_icon
                    action.setIcon(get_icon('fa5s.exclamation-triangle', color='#c8b428'))
                else:
                    action = submenu.addAction(title)
                actions[node_type] = action

        return actions

    def _build_preset_submenu(self, parent_menu: QMenu, selected_count: int) -> dict:
        """Build preset submenu with save/load/import options.

        Returns:
            Dict with keys: save, import, manage, preset_actions
        """
        preset_submenu = parent_menu.addMenu("Presets")

        # Save as Preset (requires selection)
        action_save_preset = preset_submenu.addAction(
            I18N.tr("node_editor.save_as_preset", "Save as Preset..."))
        action_save_preset.setEnabled(selected_count >= 1)

        preset_submenu.addSeparator()

        # Load Preset submenu
        load_preset_submenu = preset_submenu.addMenu("Load Preset")

        preset_actions = {}
        action_manage_presets = None
        if self._actions:
            preset_names = self._actions.list_presets()
            if preset_names:
                for preset_name in preset_names:
                    action = load_preset_submenu.addAction(preset_name)
                    preset_actions[preset_name] = action
                load_preset_submenu.addSeparator()
                action_manage_presets = load_preset_submenu.addAction(
                    I18N.tr("node_editor.manage_presets", "Manage Presets..."))
            else:
                action_no_presets = load_preset_submenu.addAction(
                    I18N.tr("node_editor.no_presets_saved", "(No presets saved)"))
                action_no_presets.setEnabled(False)
        else:
            action_no_presets = load_preset_submenu.addAction(
                I18N.tr("node_editor.no_presets", "(No presets)"))
            action_no_presets.setEnabled(False)

        preset_submenu.addSeparator()
        action_import_preset = preset_submenu.addAction(
            I18N.tr("node_editor.import_preset", "Import Preset..."))

        return {
            'save': action_save_preset,
            'import': action_import_preset,
            'manage': action_manage_presets,
            'preset_actions': preset_actions,
        }

    def _build_arrange_submenu(self, parent_menu: QMenu, selected_count: int) -> Optional[dict]:
        """Build arrange submenu for node alignment and distribution.

        Returns None if fewer than 2 nodes selected.
        """
        if selected_count < 2:
            return None

        arrange_submenu = parent_menu.addMenu("Arrange")

        action_auto_layout = arrange_submenu.addAction(
            I18N.tr("node_editor.auto_layout", "Auto Layout"))
        arrange_submenu.addSeparator()

        # Alignment
        align_submenu = arrange_submenu.addMenu("Align")
        action_align_left = align_submenu.addAction(
            I18N.tr("node_editor.align_left", "Left"))
        action_align_right = align_submenu.addAction(
            I18N.tr("node_editor.align_right", "Right"))
        action_align_top = align_submenu.addAction(
            I18N.tr("node_editor.align_top", "Top"))
        action_align_bottom = align_submenu.addAction(
            I18N.tr("node_editor.align_bottom", "Bottom"))
        action_align_center_h = align_submenu.addAction(
            I18N.tr("node_editor.align_center_h", "Center Horizontal"))
        action_align_center_v = align_submenu.addAction(
            I18N.tr("node_editor.align_center_v", "Center Vertical"))

        # Distribution
        distribute_submenu = arrange_submenu.addMenu("Distribute")
        action_distribute_h = distribute_submenu.addAction(
            I18N.tr("node_editor.distribute_h", "Horizontal"))
        action_distribute_v = distribute_submenu.addAction(
            I18N.tr("node_editor.distribute_v", "Vertical"))

        arrange_submenu.addSeparator()
        action_cleanup_overlaps = arrange_submenu.addAction(
            I18N.tr("node_editor.cleanup_overlaps", "Cleanup Overlaps"))

        return {
            'auto_layout': action_auto_layout,
            'align_left': action_align_left,
            'align_right': action_align_right,
            'align_top': action_align_top,
            'align_bottom': action_align_bottom,
            'align_center_h': action_align_center_h,
            'align_center_v': action_align_center_v,
            'distribute_h': action_distribute_h,
            'distribute_v': action_distribute_v,
            'cleanup_overlaps': action_cleanup_overlaps,
        }

    def _handle_context_menu_action(
        self,
        action: QAction,
        scene_pos: QPointF,
        conn_to_delete: Optional[ConnectionItem],
        clicked_backdrop: Optional[BackdropItem],
        node_actions: Dict[str, QAction],
        preset_data: dict,
        arrange_data: Optional[dict],
        action_delete_conn: Optional[QAction],
        action_add_backdrop: QAction,
        action_add_sticky_note: QAction,
        action_edit_backdrop: Optional[QAction],
        action_color_backdrop: Optional[QAction],
        action_backdrop_to_back: Optional[QAction],
        action_delete_backdrop: Optional[QAction],
        action_delete_node: Optional[QAction],
        action_disconnect_node: Optional[QAction],
        selected_node_id: Optional[str],
        action_lock_node: Optional[QAction],
        action_float: Optional[QAction],
        dock: Optional[QDockWidget],
    ) -> bool:
        """Handle a context menu action.

        Returns True if action was handled.
        """
        # Connection delete
        if action == action_delete_conn and conn_to_delete is not None:
            if self._connection_touches_locked_node(conn_to_delete):
                return True
            if self._actions:
                for conn_data in self._connection_items:
                    if conn_data[0] is conn_to_delete:
                        _, src_nid, src_pname, dst_nid, dst_pname = conn_data
                        self._actions.disconnect(src_nid, src_pname, dst_nid, dst_pname)
                        break
            return True

        # Node type actions — auto-connect after selected node when right-clicking on a node
        if action in node_actions.values():
            node_type = [k for k, v in node_actions.items() if v == action][0]
            self._add_new_node(node_type, scene_pos, source_node_id=selected_node_id)
            return True

        # Add backdrop/sticky note
        if action == action_add_backdrop:
            self._add_new_backdrop(scene_pos)
            return True
        if action == action_add_sticky_note:
            self.create_sticky_note(scene_pos.x(), scene_pos.y())
            return True

        if action == action_lock_node and selected_node_id is not None:
            self.toggle_node_lock(selected_node_id)
            return True

        if action == action_delete_node and selected_node_id is not None \
                and self.is_node_locked(selected_node_id):
            return True

        # Backdrop actions
        if action == action_edit_backdrop and clicked_backdrop is not None:
            self._edit_backdrop(clicked_backdrop)
            return True
        if action == action_color_backdrop and clicked_backdrop is not None:
            self._change_backdrop_color(clicked_backdrop)
            return True
        if action == action_backdrop_to_back and clicked_backdrop is not None:
            min_z = min(
                (item.zValue() for item in self._backdrop_items.values()),
                default=BackdropItem.DEFAULT_Z_ORDER,
            )
            clicked_backdrop.send_to_back(min_z - 1.0)
            self._sync_backdrop_to_graph(clicked_backdrop)
            self.graph_changed.emit()
            return True
        if action == action_delete_backdrop and clicked_backdrop is not None:
            self.remove_backdrop_visual(clicked_backdrop.backdrop_id)
            self.graph_changed.emit()
            return True

        # Node delete
        if action == action_delete_node and selected_node_id is not None:
            if self._actions:
                self._actions.delete_node(selected_node_id)
            return True

        # Node disconnect (extract from chain, reconnect upstream->downstream)
        if action == action_disconnect_node and selected_node_id is not None:
            if self.is_node_locked(selected_node_id):
                return True
            self._extract_node_from_chain(selected_node_id)
            return True

        # Float/Dock toggle
        if action == action_float and dock:
            dock.setFloating(not dock.isFloating())
            return True

        # Preset actions
        if action == preset_data.get('save'):
            self._show_save_preset_dialog()
            return True
        if action == preset_data.get('import'):
            self._show_import_preset_dialog()
            return True
        if action == preset_data.get('manage'):
            self._show_manage_presets_dialog()
            return True
        preset_actions = preset_data.get('preset_actions', {})
        if action in preset_actions.values():
            preset_name = [name for name, act in preset_actions.items() if act == action][0]
            self._load_preset_at_position(preset_name, scene_pos)
            return True

        # Arrange actions
        if arrange_data:
            if action == arrange_data.get('auto_layout'):
                self._auto_layout_selected_nodes()
                return True
            if action == arrange_data.get('align_left'):
                self._align_selected_nodes("left")
                return True
            if action == arrange_data.get('align_right'):
                self._align_selected_nodes("right")
                return True
            if action == arrange_data.get('align_top'):
                self._align_selected_nodes("top")
                return True
            if action == arrange_data.get('align_bottom'):
                self._align_selected_nodes("bottom")
                return True
            if action == arrange_data.get('align_center_h'):
                self._align_selected_nodes("center_horizontal")
                return True
            if action == arrange_data.get('align_center_v'):
                self._align_selected_nodes("center_vertical")
                return True
            if action == arrange_data.get('distribute_h'):
                self._distribute_selected_nodes("horizontal")
                return True
            if action == arrange_data.get('distribute_v'):
                self._distribute_selected_nodes("vertical")
                return True
            if action == arrange_data.get('cleanup_overlaps'):
                self._cleanup_overlaps_selected_nodes()
                return True

        return False

    def contextMenuEvent(self, event) -> None:
        """Show context menu for adding nodes and deleting connections.

        Refactored into helper methods for maintainability.
        """
        # No per-menu stylesheet: popups inherit the app-wide DARK_THEME QMenu
        # styling — the same translucent rgba(39,39,43,240) background + orange
        # selection as the Composite viewport menu. Setting a local stylesheet
        # here re-engages Qt's styled render path on every submenu and was the
        # main cause of scroll/hover jank on long category menus.
        menu = QMenu(self)
        scene_pos = self.mapToScene(event.pos())

        # Detect clicked item type
        clicked_item = self.itemAt(event.pos())
        conn_to_delete = None
        if isinstance(clicked_item, ConnectionItem):
            conn_to_delete = clicked_item
        elif clicked_item:
            parent = clicked_item.parentItem()
            if isinstance(parent, ConnectionItem):
                conn_to_delete = parent
        clicked_backdrop = self._find_backdrop_from_item(clicked_item)

        # Connection delete action
        action_delete_conn = None
        if conn_to_delete:
            action_delete_conn = menu.addAction(
                I18N.tr("node_editor.delete_connection", "Delete Connection"))
            action_delete_conn.setEnabled(
                not self._connection_touches_locked_node(conn_to_delete))
            menu.addSeparator()

        # Backdrop actions
        action_edit_backdrop = None
        action_color_backdrop = None
        action_backdrop_to_back = None
        action_delete_backdrop = None
        if clicked_backdrop is not None and conn_to_delete is None:
            action_edit_backdrop = menu.addAction(
                I18N.tr("node_editor.edit_backdrop", "Edit Backdrop"))
            action_color_backdrop = menu.addAction(
                I18N.tr("node_editor.change_backdrop_color", "Change Backdrop Color"))
            action_backdrop_to_back = menu.addAction(
                I18N.tr("node_editor.send_backdrop_to_back", "Send Backdrop to Back"))
            action_delete_backdrop = menu.addAction(
                I18N.tr("node_editor.delete_backdrop", "Delete Backdrop"))
            menu.addSeparator()

        # Node category menu
        node_actions = self._build_node_category_menu(menu)

        menu.addSeparator()
        action_add_backdrop = menu.addAction(
            I18N.tr("node_editor.add_backdrop", "Add Backdrop"))
        action_add_sticky_note = menu.addAction(
            I18N.tr("node_editor.add_sticky_note", "Add Sticky Note"))
        # C5: batch version-up for every Read node with a newer version on disk
        action_set_all_reads_latest = menu.addAction(
            I18N.tr("node_editor.set_reads_latest", "Set All Reads to Latest Version"))

        # Preset menu
        selected_nodes = [nid for nid, item in self._node_items.items() if item.isSelected()]
        preset_data = self._build_preset_submenu(menu, len(selected_nodes))

        # Arrange menu
        arrange_data = self._build_arrange_submenu(menu, len(selected_nodes))

        # Node preview display mode (None / Selected-Single / All)
        preview_actions = self._build_preview_submenu(menu)
        navigation_data = self._build_navigation_submenu(menu)

        # Float/Dock toggle
        dock = self._find_parent_dock()
        action_float = None
        if dock:
            action_float = menu.addAction("Dock" if dock.isFloating() else "Float")

        menu.addSeparator()
        action_delete_node = None
        action_disconnect_node = None
        action_lock_node = None
        action_rename_node = None
        selected_node_id = None
        item = self.itemAt(event.pos())
        if isinstance(item, NodeItem) or (item and isinstance(item.parentItem(), NodeItem)):
            target = item if isinstance(item, NodeItem) else item.parentItem()
            # B3-1: enter the owning group's edit view (Nuke-style).
            enter_gid = None
            if self._node_graph is not None and hasattr(self._node_graph, "get_group_for_node"):
                try:
                    g = self._node_graph.get_group_for_node(target.node_id)
                    enter_gid = g.get("id") if isinstance(g, dict) else None
                except Exception:
                    enter_gid = None
            if enter_gid and enter_gid not in self._group_view_stack:
                action_enter_group = menu.addAction(
                    f"Enter Group ({enter_gid[:8]}…)")
                action_enter_group.triggered.connect(
                    lambda _checked=False, gid=enter_gid:
                    self.group_enter_requested.emit(gid))

            action_delete_node = menu.addAction(f"Delete {target.node_title}")
            selected_node_id = target.node_id
            action_rename_node = menu.addAction(
                I18N.tr("node_editor.rename_node", "Rename Node"))
            action_rename_node.setEnabled(not target.is_locked)
            action_lock_node = menu.addAction(
                "Unlock Node" if target.is_locked else "Lock Node")
            action_delete_node.setEnabled(not target.is_locked)
            # Show disconnect option only if the node has connections
            has_connections = any(
                c[1] == selected_node_id or c[3] == selected_node_id
                for c in self._connection_items
            )
            if has_connections:
                action_disconnect_node = menu.addAction(
                    I18N.tr("node_editor.disconnect_node", "Disconnect Node"))
                action_disconnect_node.setEnabled(not target.is_locked)

        focus_data = self._build_focus_submenu(
            menu, bool(selected_nodes or selected_node_id))

        # Execute and handle
        action = menu.exec(event.globalPos())

        # Preview mode is handled inline (no scene state needed), keeping it out
        # of the long _handle_context_menu_action signature.
        if action in preview_actions.values():
            mode = [m for m, a in preview_actions.items() if a is action][0]
            self._set_node_preview_mode(mode)
            return

        # B4: rename is handled inline for the same reason as preview.
        if action_rename_node is not None and action == action_rename_node:
            self.begin_node_rename(selected_node_id)
            return

        # C5: batch version-up handled inline (delegates to the page).
        if action == action_set_all_reads_latest:
            self.set_all_reads_latest_requested.emit()
            return

        if action == navigation_data["add"]:
            self._prompt_add_view_bookmark()
            return
        if action == navigation_data["back"]:
            self.navigate_back()
            return
        if action == navigation_data["forward"]:
            self.navigate_forward()
            return
        bookmark_actions = navigation_data["bookmarks"]
        if action in bookmark_actions.values():
            name = next(name for name, candidate in bookmark_actions.items()
                        if candidate is action)
            self.navigate_to_bookmark(name)
            return
        remove_actions = navigation_data["remove"]
        if action in remove_actions.values():
            name = next(name for name, candidate in remove_actions.items()
                        if candidate is action)
            self.remove_view_bookmark(name)
            return

        if action == focus_data["isolate"]:
            self.isolate_selected_branch(selected_node_id)
            return
        if action == focus_data["dependencies"]:
            self.highlight_selected_dependencies(selected_node_id)
            return
        if action == focus_data["clear"]:
            self.clear_graph_focus()
            return

        self._handle_context_menu_action(
            action, scene_pos, conn_to_delete, clicked_backdrop,
            node_actions, preset_data, arrange_data,
            action_delete_conn, action_add_backdrop, action_add_sticky_note,
            action_edit_backdrop, action_color_backdrop, action_backdrop_to_back,
            action_delete_backdrop, action_delete_node, action_disconnect_node,
            selected_node_id, action_lock_node, action_float, dock,
        )

    # ── Keyboard Shortcuts ──

    def _setup_shortcuts(self) -> None:
        """Register all keyboard shortcuts with the ShortcutManager.
        
        This creates a centralized registry for node editor shortcuts,
        enabling user customization and conflict detection.
        """
        sm = ShortcutManager.instance()
        if sm is None:
            return
        
        # Helper to register and store callback
        def reg(action_id: str, label: str, default_shortcut: str, callback: Callable) -> None:
            sm.register(action_id, label, "Node Editor", default_shortcut)
            self._shortcut_callbacks[action_id] = callback
            # Cache the QKeySequence for matching
            seq = QKeySequence(sm.get_shortcut(action_id))
            if not seq.isEmpty():
                self._shortcut_keys_cache[action_id] = seq
        
        # Navigation shortcuts
        reg("node_editor/search", "Search Nodes", "Tab", self._show_node_search_popup)
        reg("node_editor/search_graph", "Search in Graph", "Ctrl+F", self._show_graph_node_search_popup)
        reg("node_editor/jump_to_node", "Jump to Node", "Ctrl+L", self._show_jump_to_node_dialog)
        reg("node_editor/fit_all", "Fit All Nodes", "F", self.fit_all_nodes)
        reg("node_editor/fit_selected", "Fit Selected Nodes", "Shift+F", self.fit_selected_nodes)
        reg("node_editor/navigation_back", "Previous View", "Alt+Left",
            self.navigate_back)
        reg("node_editor/navigation_forward", "Next View", "Alt+Right",
            self.navigate_forward)
        
        # Selection shortcuts
        reg("node_editor/select_all", "Select All", "Ctrl+A", self._select_all_nodes)
        reg("node_editor/select_upstream", "Select Upstream", "Ctrl+Up", self._select_upstream_nodes)
        reg("node_editor/select_downstream", "Select Downstream", "Ctrl+Down", self._select_downstream_nodes)
        
        # Edit shortcuts
        reg("node_editor/delete", "Delete Selected", "Delete", self._delete_selected)
        reg("node_editor/copy", "Copy Nodes", "Ctrl+C", self._copy_selected_nodes)
        reg("node_editor/cut", "Cut Nodes", "Ctrl+X", self._cut_selected_nodes)
        reg("node_editor/paste", "Paste Nodes", "Ctrl+V", self._paste_nodes)
        reg("node_editor/duplicate", "Duplicate Nodes", "Ctrl+D", self._duplicate_selected_nodes)
        reg("node_editor/undo", "Undo", "Ctrl+Z", lambda: self.undo_requested.emit())
        reg("node_editor/redo", "Redo", "Ctrl+Shift+Z", lambda: self.redo_requested.emit())
        
        # Node creation shortcuts — auto-connect after selected node when one is selected
        reg("node_editor/add_merge", "Add Merge Node", "M", lambda: self._add_new_node("merge", self.mapToScene(self.viewport().rect().center()), source_node_id=self._selected_node_id))
        # Note: Dot node is now added via Cmd/Ctrl + click on connection midpoint (no D shortcut)
        # Add Backdrop: wraps the current selection (box-select → Alt+G) or sits at the
        # viewport center when nothing is selected. Alt+G is free app-wide; G is grid-snap
        # and Shift+G is ungroup, so Alt+G avoids clobbering either.
        reg("node_editor/add_backdrop", "Add Backdrop", "Alt+G", self._add_new_backdrop)
        reg("node_editor/add_blur", "Add Blur Node", "B", lambda: self._add_new_node("blur", self.mapToScene(self.viewport().rect().center()), source_node_id=self._selected_node_id))
        reg("node_editor/add_sticky_note", "Add Sticky Note", "N", lambda: self.create_sticky_note(self.mapToScene(self.viewport().rect().center()).x(), self.mapToScene(self.viewport().rect().center()).y()))
        
        # Group/Ungroup
        reg("node_editor/group", "Group Nodes", "Ctrl+G", self._group_selected_nodes)
        reg("node_editor/ungroup", "Ungroup Nodes", "Shift+G", self._ungroup_selected_nodes)

        # B4: rename selected node (F2 was unbound in the composite editor)
        reg("node_editor/rename_node", "Rename Node", "F2", self._rename_selected_node)

        # C5: version binning for the selected Read/Write node
        reg("node_editor/version_up", "Version Up (Read/Write)", "]",
            lambda: self._version_step_selected(1))
        reg("node_editor/version_down", "Version Down (Read/Write)", "[",
            lambda: self._version_step_selected(-1))

        # Layout shortcuts
        reg("node_editor/auto_layout", "Auto Layout", "L", self._auto_layout_selected_nodes)

        # Toggle shortcuts
        reg("node_editor/toggle_grid_snap", "Toggle Grid Snap", "G", self._toggle_grid_snap)
        reg("node_editor/toggle_bypass", "Toggle Bypass", "H", self._toggle_bypass_selected)

        # Project Settings — toggle the properties panel's Project Settings view
        reg("node_editor/project_settings", "Project Settings", "S",
            lambda: self.project_settings_requested.emit())

        # Node-Preview — preview the selected node in the viewer (Nuke-style,
        # single viewer slot). Press again (or Esc) to return to Output.
        reg("node_editor/toggle_preview", "Preview Selected Node", "1",
            lambda: self.preview_requested.emit())
        # Esc exits node-preview (if active) and returns the viewer to Output.
        reg("node_editor/exit_preview", "Exit Node Preview", "Esc",
            lambda: self.preview_exit_requested.emit())

        # Space for pan mode is handled separately (not registered)

    def _select_all_nodes(self) -> None:
        """Select all nodes in the graph."""
        for item in self._node_items.values():
            item.set_selected(True)

    def _delete_selected(self) -> None:
        """Delete selected connections, backdrops, sticky notes, or nodes."""
        # Check the node selection before touching selected edges.  Otherwise
        # Delete could still remove a connection while a locked node was part
        # of the same selection.
        if self._selection_contains_locked_node():
            return

        # First try to delete selected connections
        if self._actions:
            deleted = self._actions.delete_selected_connections()
            if deleted:
                return

        # Then delete selected backdrops (batch delete for undo)
        selected_backdrop_ids = [
            backdrop_id for backdrop_id, item in self._backdrop_items.items()
            if item.isSelected()
        ]
        if selected_backdrop_ids:
            # Collect backdrop data before deletion for undo
            deleted_data = {}
            for backdrop_id in selected_backdrop_ids:
                item = self._backdrop_items.get(backdrop_id)
                if item:
                    deleted_data[backdrop_id] = item.to_dict()
            # Delete visuals (suppress per-item signal; the batch signal below
            # carries all of them for a single undo step)
            for backdrop_id in selected_backdrop_ids:
                self.remove_backdrop_visual(backdrop_id, emit_signal=False)
            # Emit batch delete signal for undo
            self.backdrops_deleted.emit(selected_backdrop_ids, deleted_data)
            # AUD-10501: remove_backdrop_visual already mutated the graph
            # model (_node_graph.remove_backdrop); without graph_changed the
            # project dirty/save state misses this deletion (the sticky-note
            # and single-delete paths both emit it).
            self.graph_changed.emit()
            return

        # Then delete selected sticky notes (batch delete for undo)
        selected_sticky_note_ids = [
            note_id for note_id, item in self._sticky_note_items.items()
            if item.isSelected()
        ]
        if selected_sticky_note_ids:
            # Collect note data before deletion for undo
            deleted_data = {}
            for note_id in selected_sticky_note_ids:
                item = self._sticky_note_items.get(note_id)
                if item:
                    deleted_data[note_id] = item.to_dict()
            # Delete visuals (suppress per-item signal; the batch signal below
            # carries all of them for a single undo step)
            for note_id in selected_sticky_note_ids:
                self.remove_sticky_note_visual(note_id, emit_signal=False)
            # Emit batch delete signal for undo
            self.sticky_notes_deleted.emit(selected_sticky_note_ids, deleted_data)
            self.graph_changed.emit()
            return

        # Then delete selected nodes (multi-select delete).
        # AUD-10500: filter by node_type — a renamed Output node used to
        # slip through this title check and get deleted, breaking the
        # render chain (the actions-level guard is type-based too).
        selected_ids = [nid for nid, item in self._node_items.items()
                        if item.isSelected()
                        and not self._is_protected_node_type(nid)]
        if selected_ids:
            if self._actions:
                self._actions.delete_selected_nodes()

    def _toggle_grid_snap(self) -> None:
        """Toggle grid snap mode."""
        self._snap_to_grid = not self._snap_to_grid

    def _match_key_to_shortcut(self, event) -> Optional[str]:
        """Check if a key event matches a registered shortcut.
        
        Args:
            event: QKeyEvent to check
            
        Returns:
            action_id if matched, None otherwise
        """
        if not self._shortcuts_enabled:
            return None
        
        # Build key sequence from event
        key = event.key()
        mods = event.modifiers()
        
        # Handle special keys (Delete/Backspace)
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            key_seq = QKeySequence(Qt.Key.Key_Delete)
        else:
            # Convert KeyboardModifier to int for bitwise OR
            key_seq = QKeySequence(mods.value | key)
        
        # Check against all registered shortcuts
        for action_id, cached_seq in self._shortcut_keys_cache.items():
            if cached_seq.matches(key_seq) == QKeySequence.SequenceMatch.ExactMatch:
                return action_id
        
        return None

    def set_shortcuts_enabled(self, enabled: bool) -> None:
        """Enable or disable keyboard shortcuts.
        
        Args:
            enabled: True to enable shortcuts, False to disable
        """
        self._shortcuts_enabled = enabled

    def shortcuts_enabled(self) -> bool:
        """Check if keyboard shortcuts are enabled."""
        return self._shortcuts_enabled

    def _refresh_shortcuts_cache(self) -> None:
        """Refresh the cached key sequences after customization changes."""
        sm = ShortcutManager.instance()
        if sm is None:
            return
        
        self._shortcut_keys_cache.clear()
        for action_id in self._shortcut_callbacks.keys():
            seq = QKeySequence(sm.get_shortcut(action_id))
            if not seq.isEmpty():
                self._shortcut_keys_cache[action_id] = seq

    def _get_view_center_scene_pos(self) -> QPointF:
        """Get the current viewport center in scene coordinates.

        This method is called at runtime (not cached in a closure),
        ensuring the position is recalculated each time the popup is used.
        """
        return self.mapToScene(self.viewport().rect().center())

    def _show_node_search_popup(self) -> None:
        """Show node search popup.

        Note: The on_node_added callback uses _get_view_center_scene_pos()
        instead of directly calculating position in the lambda. This ensures
        the position is recalculated each time, not cached when the popup
        is first created.
        """
        if self._node_search_popup is None:
            self._node_search_popup = NodeSearchPopup(
                NODE_CATEGORIES,
                parent=self,
                on_node_added=lambda node_type: self._add_new_node(
                    node_type,
                    self._get_view_center_scene_pos(),
                    source_node_id=self._selected_node_id,
                ),
            )
        # Refresh the category map each time so the Add Node menu reflects the
        # active work mode (image mode hides time-driven nodes).
        try:
            from src.shared.feature_flags import get_work_mode
            cm = getattr(self.window(), "config_manager", None)
            image_mode = get_work_mode(cm) == "image"
            self._node_search_popup.set_node_categories(
                get_categories_for_mode(image_mode)
            )
        except Exception:
            pass

        self._node_search_popup.exec()

    def _show_graph_node_search_popup(self) -> None:
        """Show graph node search popup for searching existing nodes.

        This popup allows searching and navigating to nodes already in the graph,
        unlike the Tab-based NodeSearchPopup which adds new nodes.
        """
        if not self._node_graph or not self._node_items:
            return

        # Lazy-create the popup
        if self._graph_node_search_popup is None:
            self._graph_node_search_popup = GraphNodeSearchPopup(
                self._node_items,
                self._node_graph,
                parent=self,
                on_navigate=self._navigate_to_node,
            )
            # Connect signal for highlighting
            self._graph_node_search_popup.search_results_changed.connect(
                self._on_search_results_changed
            )

        self._graph_node_search_popup.exec()

    def _show_jump_to_node_dialog(self) -> None:
        """Show a quick dialog to jump to a specific node by ID or name."""
        if not self._node_graph or not self._node_items:
            return

        # Build a list of node names for the combo box
        node_names = []
        for nid, item in self._node_items.items():
            node_names.append(f"{item.node_title} ({nid[:8]}...)")

        # Show input dialog
        from PyQt6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getItem(
            self,
            "Jump to Node",
            "Select node to jump to:",
            node_names,
            editable=True,
        )

        if ok and text:
            # Parse the node ID from the selected text
            # Format: "NodeTitle (nodeId...)"
            if "(" in text:
                # Extract the node ID prefix
                start = text.rfind("(") + 1
                end = text.rfind("...")
                if start < end:
                    id_prefix = text[start:end].strip()
                    # Find matching node
                    for nid, item in self._node_items.items():
                        if nid.startswith(id_prefix):
                            self._navigate_to_node(nid)
                            return
            # Or try matching by full title
            for nid, item in self._node_items.items():
                if item.node_title == text:
                    self._navigate_to_node(nid)
                    return
            # Or try matching by node ID directly
            for nid in self._node_items:
                if nid == text:
                    self._navigate_to_node(nid)
                    return

    def _navigate_to_node(self, node_id: str) -> None:
        """Navigate to a specific node, centering it in the viewport.

        Args:
            node_id: The node ID to navigate to
        """
        item = self._node_items.get(node_id)
        if not item:
            return

        # Select the node
        self._selected_node_id = node_id
        for nid, nitem in self._node_items.items():
            nitem.set_selected(nid == node_id)
        self.node_selected.emit(node_id)

        # Center the node in the viewport
        node_rect = item.sceneBoundingRect()
        # Add padding around the node for better visibility

        # Smooth zoom to fit the node
        target_zoom = 1.0
        if self._current_zoom < 0.5:
            # If zoomed out too much, zoom in to 1.0x
            self.setTransform(QTransform().scale(target_zoom, target_zoom))
            self._current_zoom = target_zoom
            self.zoom_changed.emit(target_zoom)

        # Center on the node
        self.centerOn(node_rect.center())

        # Briefly highlight the node with a flash effect
        self._flash_node_highlight(node_id)

    def _flash_node_highlight(self, node_id: str) -> None:
        """Briefly flash highlight a node for visual feedback.

        Args:
            node_id: The node ID to highlight
        """
        item = self._node_items.get(node_id)
        if not item:
            return

        # Store original state
        original_selected = item.isSelected()

        # Create a timer to flash the highlight
        flash_timer = QTimer(self)
        flash_timer.setSingleShot(True)
        flash_count = 0
        max_flashes = 3

        def do_flash() -> None:
            nonlocal flash_count
            flash_count += 1
            if flash_count <= max_flashes:
                # Toggle selection state for visual flash
                if flash_count % 2 == 1:
                    # Highlight
                    item.set_selected(True)
                    item.update()
                else:
                    # Restore
                    item.set_selected(original_selected)
                    item.update()

                if flash_count < max_flashes:
                    flash_timer.start(150)  # Flash every 150ms
                else:
                    # Final: restore original state
                    item.set_selected(True)  # Keep selected for navigation
                    item.update()

        flash_timer.timeout.connect(do_flash)
        flash_timer.start(50)  # Initial delay before first flash

    def _on_search_results_changed(self, matched_node_ids: List[str]) -> None:
        """Handle search results change, highlighting matched nodes.

        Args:
            matched_node_ids: List of node IDs that match the search
        """
        # Clear previous highlights
        self._clear_node_highlights()

        # Highlight new matches
        if matched_node_ids:
            self._highlight_nodes(matched_node_ids)

    def _highlight_nodes(self, node_ids: List[str]) -> None:
        """Highlight multiple nodes with a distinctive visual effect.

        Args:
            node_ids: List of node IDs to highlight
        """
        self._highlighted_node_ids = node_ids

        for nid in node_ids:
            item = self._node_items.get(nid)
            if item:
                # Use a distinctive border color for search highlight
                # The NodeItem's paint method already handles selection glow
                # We just need to ensure it's selected for visual feedback
                item.update()

        # Force viewport update
        self.viewport().update()

    def _clear_node_highlights(self) -> None:
        """Clear all node search highlights."""
        for nid in self._highlighted_node_ids:
            item = self._node_items.get(nid)
            if item:
                item.update()

        self._highlighted_node_ids.clear()
        self.viewport().update()

    def eventFilter(self, obj, event):
        if (obj is self.viewport()
                and event.type() == QEvent.Type.ShortcutOverride
                and event.key() == Qt.Key.Key_F2
                and event.modifiers() == Qt.KeyboardModifier.NoModifier):
            event.accept()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:
        """Handle keyboard shortcuts via ShortcutManager."""
        # Check if shortcuts are enabled
        if not self._shortcuts_enabled:
            super().keyPressEvent(event)
            return

        key = event.key()
        mods = event.modifiers()

        # Cmd/Ctrl key: show insertion dots on all connections
        if key in (Qt.Key.Key_Control, Qt.Key.Key_Meta) and not self._ctrl_insertion_active:
            self._ctrl_insertion_active = True
            self._show_connection_insertion_dots(True)

        # Space key for pan mode - handled separately (not registered)
        # Check for IME (Input Method Editor) state to prevent accidental
        # activation when composing text (e.g., Chinese/Japanese input)
        if key == Qt.Key.Key_Space and not mods:
            # Skip if IME is active (composing text)
            from PyQt6.QtWidgets import QApplication
            input_method = QApplication.inputMethod()
            if input_method and input_method.isVisible():
                # IME is active, let the system handle this for text input
                super().keyPressEvent(event)
                return
            # Also check if the event is from IME composition
            # Non-spontaneous events are synthetic (e.g., from IME)
            if not event.spontaneous():
                super().keyPressEvent(event)
                return

            self._space_held = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            return
        
        # Try to match via ShortcutManager
        action_id = self._match_key_to_shortcut(event)
        if action_id:
            callback = self._shortcut_callbacks.get(action_id)
            if callback:
                callback()
            return
        
        # Fallback: pass to parent for any unhandled keys
        super().keyPressEvent(event)


    def keyReleaseEvent(self, event) -> None:
        """Handle key release."""
        if event.key() == Qt.Key.Key_Space:
            self._space_held = False
            if not self._mid_button_panning:
                self.setCursor(Qt.CursorShape.ArrowCursor)

        # Cmd/Ctrl released: hide insertion dots
        if event.key() in (Qt.Key.Key_Control, Qt.Key.Key_Meta):
            self._ctrl_insertion_active = False
            self._show_connection_insertion_dots(False)

        super().keyReleaseEvent(event)

    # ── View Controls ──

    def fit_all_nodes(self) -> None:
        """Center and fit all nodes in the viewport.

        Uses a moderate zoom cap (1.5x) so small graphs don't appear
        uncomfortably large.
        """
        if not self._node_items:
            return
        rect = QRectF()
        for item in self._node_items.values():
            rect = rect.united(item.sceneBoundingRect())
        if rect.isValid():
            self.fitInView(rect.adjusted(-60, -40, 60, 40), Qt.AspectRatioMode.KeepAspectRatio)
            # Sync zoom state after fitInView changes the transform
            transform = self.transform()
            new_zoom = transform.m11()
            # Cap zoom so small graphs (e.g. 3 default nodes) aren't oversized
            if new_zoom > 1.5:
                new_zoom = 1.5
                self.setTransform(QTransform().scale(new_zoom, new_zoom))
            self._current_zoom = new_zoom
            self.zoom_changed.emit(self._current_zoom)
            self._record_navigation_point()

    def center_on_nodes(self) -> None:
        """Center the viewport on all nodes without changing zoom level.

        Used when switching to the Composite page to ensure nodes are
        visible, while preserving the user's zoom preference.
        """
        if not self._node_items:
            return
        rect = QRectF()
        for item in self._node_items.values():
            rect = rect.united(item.sceneBoundingRect())
        if rect.isValid():
            self.centerOn(rect.center())
            self._record_navigation_point()
            self.viewport_moved.emit()

    # ── Background Grid ──

    def drawBackground(self, painter, rect) -> None:
        """Draw grid background."""
        painter.fillRect(rect, self.BG_COLOR)

        # Draw grid lines
        pen_minor = QPen(self.GRID_COLOR_MINOR, 0.5)
        pen_major = QPen(self.GRID_COLOR_MAJOR, 1.0)

        left = int(rect.left()) - (int(rect.left()) % self.GRID_SIZE)
        top = int(rect.top()) - (int(rect.top()) % self.GRID_SIZE)

        # Minor grid
        painter.setPen(pen_minor)
        x = left
        while x < rect.right():
            painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))
            x += self.GRID_SIZE
        y = top
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), y, int(rect.right()), y)
            y += self.GRID_SIZE

        # Major grid (every 5 cells)
        painter.setPen(pen_major)
        x = left - (left % (self.GRID_SIZE * 5))
        while x < rect.right():
            painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))
            x += self.GRID_SIZE * 5
        y = top - (top % (self.GRID_SIZE * 5))
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), y, int(rect.right()), y)
            y += self.GRID_SIZE * 5

    # ── Rubber Band Overlay ──

    def drawForeground(self, painter, rect) -> None:
        """Draw rubber band selection overlay and zoom indicator."""
        if self._rubber_band_active and self._rubber_band_start is not None:
            # Use tracked position for HiDPI accuracy (set in mouseMoveEvent)
            # instead of QCursor.pos() which can be inaccurate on HiDPI displays
            scene_pos = self._rubber_band_current_pos or self._rubber_band_start
            sel_rect = QRectF(
                self._rubber_band_start,
                scene_pos,
            ).normalized()

            # Semi-transparent fill
            painter.setBrush(QColor(255, 180, 60, 20))
            painter.setPen(QPen(QColor(255, 180, 60, 120), 1))
            painter.drawRect(sel_rect)

    # ── Helpers ──

    def _find_port_at(self, scene_pos: QPointF) -> Optional[PortItem]:
        """Find a port item at the given scene position.

        Performance: Uses QGraphicsScene.items() for O(1) spatial lookup
        instead of O(n) iteration over all nodes.
        """
        # Use Qt's spatial indexing for fast lookup
        items_at_pos = self._scene.items(scene_pos)
        for item in items_at_pos:
            if isinstance(item, PortItem):
                return item
            # Port might be a child item, check parent
            parent = item.parentItem()
            if isinstance(parent, PortItem):
                return parent
        return None

    def _find_node_at(self, scene_pos: QPointF) -> Optional[NodeItem]:
        """Find a node item at the given scene position.

        Performance: Uses QGraphicsScene.items() for O(1) spatial lookup
        instead of O(n) iteration over all nodes.
        """
        # Use Qt's spatial indexing for fast lookup
        items_at_pos = self._scene.items(scene_pos)
        for item in items_at_pos:
            if isinstance(item, NodeItem):
                return item
            # Check if item is a child of a node (e.g., port, header)
            parent = item.parentItem()
            if isinstance(parent, NodeItem):
                return parent
        return None

    def _find_backdrop_from_item(self, item: Optional[QGraphicsItem]) -> Optional[BackdropItem]:
        """Return the backdrop item for a scene item or one of its parents."""
        current = item
        while current is not None:
            if isinstance(current, BackdropItem):
                return current
            current = current.parentItem()
        return None

    def _clear_backdrop_selection(self) -> None:
        """Deselect all backdrop items.

        Node and backdrop selection are kept mutually exclusive: Qt's default
        scene drag moves every selected movable item together, so a backdrop
        left selected would be co-dragged when the user drags a node inside it.
        """
        for item in self._backdrop_items.values():
            if item.isSelected():
                item.setSelected(False)

    def _clear_sticky_selection(self) -> None:
        """Deselect all sticky note items.

        Kept mutually exclusive with node/backdrop selection for the same
        reason as :meth:`_clear_backdrop_selection`: a sticky left selected
        would be co-dragged when the user drags a node nearby.
        """
        for item in self._sticky_note_items.values():
            if item.isSelected():
                item.setSelected(False)

    def _wire_backdrop(self, item: BackdropItem) -> None:
        """Attach change/modify handlers and the node-items provider to a backdrop."""
        item.set_changed_handler(self._on_backdrop_changed)
        item.set_modified_handler(self._on_backdrop_modified)
        item.set_node_items_provider(lambda: self._node_items)

    def _on_backdrop_modified(self, item: BackdropItem, old_data: dict, new_data: dict) -> None:
        """Forward a single backdrop edit (move/resize/edit/color/z) for undo.

        Carries any node positions that moved along with a backdrop drag so the
        undo step can restore them as one group.
        """
        node_moves = item.take_node_moves() or []
        self.backdrop_modified.emit(item.backdrop_id, old_data, new_data, node_moves)
        if node_moves:
            # Nodes were translated by the backdrop drag — persist their new pos.
            self._update_graph_positions()

    def _on_backdrop_changed(self, item: BackdropItem) -> None:
        """Synchronize backdrop UI metadata after move, resize, or edit.
        
        Note: Backdrop changes don't trigger render dirty.
        Emits appropriate signals for undo integration.
        """
        # Check if backdrop was deleted (item removed from scene)
        if item.scene() is None:
            if self._node_graph is not None:
                self._node_graph.remove_backdrop(item.backdrop_id)
            self._backdrop_items.pop(item.backdrop_id, None)
            return
        
        # Sync to graph (no render dirty)
        self._sync_backdrop_to_graph(item)
        # Emit graph_changed for UI state saving (but not render)
        self.graph_changed.emit()

    def _sync_backdrop_to_graph(self, item: BackdropItem) -> None:
        """Refresh containment and write backdrop metadata to the graph."""
        if self._node_graph is None:
            return
        item.update_contained_nodes(self._node_items)
        self._node_graph.set_backdrop(item.backdrop_id, item.to_dict())

    def _refresh_backdrop_containment(self, item: BackdropItem) -> None:
        """Refresh one backdrop's covered node IDs and persist the result."""
        changed = item.update_contained_nodes(self._node_items)
        if changed:
            self._sync_backdrop_to_graph(item)

    def _refresh_all_backdrop_containment(self) -> None:
        """Refresh covered node IDs for every backdrop."""
        for item in self._backdrop_items.values():
            self._refresh_backdrop_containment(item)

    def _start_connection_drag(self, port: PortItem, scene_pos: QPointF) -> None:
        """Start dragging a connection from an output port (forward drag)."""
        self._dragging_connection = True
        self._drag_src_port = port
        self._drag_dst_port = None
        self._drag_direction = "forward"
        self._snap_port = None

        parent = port.parentItem()
        if isinstance(parent, NodeItem):
            self._drag_src_node_id = parent.node_id

        self._temp_connection = ConnectionItem(data_type=port.data_type)
        self._scene.addItem(self._temp_connection)

    def _start_reverse_connection_drag(self, port: PortItem, scene_pos: QPointF) -> None:
        """Start dragging a connection from an empty input port (reverse drag).

        The user drags from an input port to find an output port to connect from.
        """
        self._dragging_connection = True
        self._drag_src_port = None
        self._drag_dst_port = port
        self._drag_direction = "reverse"
        self._snap_port = None
        self._drag_src_node_id = None

        self._temp_connection = ConnectionItem(data_type=port.data_type)
        self._scene.addItem(self._temp_connection)

    def _disconnect_and_drag(self, port: PortItem, scene_pos: QPointF) -> None:
        """Disconnect an input port and start dragging from the source."""
        parent = port.parentItem()
        if not isinstance(parent, NodeItem):
            return

        dst_node_id = parent.node_id
        dst_port_name = port.port_name

        # Use actions to disconnect, but keep the drag logic
        if self._actions:
            src_info = self._actions.disconnect_by_dst(dst_node_id, dst_port_name)
            if src_info:
                src_nid, src_pname = src_info
                src_item = self._node_items.get(src_nid)
                if src_item:
                    src_port = src_item.output_ports.get(src_pname)
                    if src_port:
                        self._start_connection_drag(src_port, scene_pos)
                return

    def _finish_connection_drag(self, event: QMouseEvent) -> None:
        """Finish the connection drag (forward or reverse).

        Handles two cases:
        1. Normal port connection
        2. Cancel (no valid target)
        """
        # Save snap targets before clearing
        snap_target_port = self._snap_port

        # Clear port snap highlight
        if self._snap_port:
            self._snap_port.set_snap_highlight(False)
            self._snap_port = None

        if self._temp_connection:
            self._scene.removeItem(self._temp_connection)
            self._temp_connection = None

        # Handle normal port connection
        if self._drag_direction == "forward" and self._drag_src_port and self._drag_src_node_id:
            # Prefer the snapped port (what the user saw highlighted) over mouse position
            target_port = snap_target_port
            if not target_port:
                scene_pos = self.mapToScene(event.pos())
                target_port = self._find_port_at(scene_pos)

            # 类型校验：源端口 data_type 必须与目标匹配，否则拒绝（即使精准命中）。
            # snap 流程已校验，但 _find_port_at 精确命中路径无校验 → 这里兜底。
            src_data_type = self._drag_src_port.data_type
            if (target_port and target_port.direction == "input"
                    and target_port.data_type == src_data_type):
                dst_item = target_port.parentItem()
                src_item = self._drag_src_port.parentItem()
                if (isinstance(dst_item, NodeItem)
                        and isinstance(src_item, NodeItem)
                        and not dst_item.is_locked
                        and not src_item.is_locked):
                    self.node_connected.emit(
                        self._drag_src_node_id, self._drag_src_port.port_name,
                        dst_item.node_id, target_port.port_name,
                    )

        elif self._drag_direction == "reverse" and self._drag_dst_port:
            # Prefer the snapped port over mouse position
            target_port = snap_target_port
            if not target_port:
                scene_pos = self.mapToScene(event.pos())
                target_port = self._find_port_at(scene_pos)

            # 类型校验：反向拖拽时，目标 output 的 data_type 必须与拉出的 input 匹配。
            dst_data_type = self._drag_dst_port.data_type
            if (target_port and target_port.direction == "output"
                    and target_port.data_type == dst_data_type):
                src_item = target_port.parentItem()
                if isinstance(src_item, NodeItem):
                    dst_item = self._drag_dst_port.parentItem()
                    if (isinstance(dst_item, NodeItem)
                            and not src_item.is_locked
                            and not dst_item.is_locked):
                        self.node_connected.emit(
                            src_item.node_id, target_port.port_name,
                            dst_item.node_id, self._drag_dst_port.port_name,
                        )

        self._dragging_connection = False
        self._drag_src_port = None
        self._drag_dst_port = None
        self._drag_direction = None
        self._drag_src_node_id = None

    def _find_non_overlapping_position(self, x: float, y: float) -> tuple:
        """Adjust (x, y) so the new node doesn't overlap existing nodes.

        Scans downward from the desired position until a free slot is found.
        Returns the adjusted (x, y) tuple.
        """
        from .node_item import NodeItem as _NI
        node_w = _NI.NODE_WIDTH
        offset_y = 40  # Vertical offset per overlap attempt

        for attempt in range(20):
            test_rect = QRectF(x, y, node_w, 60)  # Approximate min height
            overlaps = False
            for item in self._node_items.values():
                if item.sceneBoundingRect().intersects(test_rect):
                    overlaps = True
                    break
            if not overlaps:
                return (x, y)
            y += offset_y
        return (x, y)

    def _add_new_node(self, node_type: str, scene_pos: QPointF,
                      source_node_id: Optional[str] = None) -> None:
        """Add a new node at the given scene position.

        If source_node_id is provided, the new node will be automatically
        inserted after the source node in the connection chain:

            Before: source -> downstream
            After:  source -> new_node -> downstream

        The new node is positioned to the right of the source node, and
        any downstream connections from the source are re-routed through
        the new node.

        Args:
            node_type: Type of node to create (e.g., "blur", "grade")
            scene_pos: Fallback position if no source_node_id
            source_node_id: Optional node ID to auto-connect after
        """
        if self._actions is None:
            return

        # If source node specified, position new node to the right of it
        if source_node_id:
            source_item = self._node_items.get(source_node_id)
            if source_item:
                new_x = source_item.pos().x() + source_item.rect().width() + 80
                new_y = source_item.pos().y()
                scene_pos = QPointF(new_x, new_y)

        # Avoid overlapping existing nodes
        pos_x, pos_y = self._find_non_overlapping_position(
            scene_pos.x(), scene_pos.y()
        )

        # Use batch mode when auto-connecting to suppress intermediate signals.
        # AUD-10505: save/restore (not blind set/clear) and try/finally — an
        # exception in add_node/_auto_connect_after used to leave _batch_mode
        # stuck on, permanently suppressing containment refresh and
        # graph_changed for every later operation.
        needs_batch = source_node_id is not None and not self._batch_mode
        if needs_batch:
            self._batch_mode = True

        try:
            # 菜单/搜索建 Scene3D → 只建一个 Scene3D（用其内置默认相机/灯光/cube，
            # 开箱即用）。不再自动 spawn Camera3D/Light3D/Geo3D 子节点——用户需要时
            # 可手动添加并连到 scene3d 的 camera_in/lights_in/geometry_in。
            # 拖拽 3D 模型文件仍走 create_3d_scene_with_children（dropEvent 路径），
            # 保留模型导入 + 材质纹理端口能力；v6→v7 迁移路径不受影响。
            new_node = self._actions.add_node(node_type, pos_x, pos_y)

            # Auto-connect after source node if specified. 3D helper/source nodes
            # are scene objects in the global 3D Viewer; creating one should not
            # splice it into the selected 2D/Scene chain implicitly.
            if (new_node and source_node_id
                    and getattr(new_node, "node_type", None) not in _VIEWER3D_OBJECT_TYPES):
                self._auto_connect_after(source_node_id, new_node)
        finally:
            if needs_batch:
                self._batch_mode = False
                self._refresh_all_backdrop_containment()
                self.graph_changed.emit()

    def _auto_connect_after(self, source_node_id: str, new_node: Node) -> None:
        """Insert new_node after source_node in the connection chain.

        Finds compatible ports between source and new_node, then:
        1. Disconnects source's downstream connections (matching output port)
        2. Reconnects them through new_node's output
        3. Connects source output → new_node input

        Args:
            source_node_id: The node to insert after
            new_node: The newly created node to insert
        """
        if not self._actions or not self._node_graph:
            return

        source_node = self._node_graph.get_node(source_node_id)
        if not source_node:
            return

        # Find compatible ports between source output and new_node input
        source_output_port = None
        new_input_port = None

        for src_port in source_node.outputs:
            for dst_port in new_node.inputs:
                if src_port.data_type == dst_port.data_type:
                    source_output_port = src_port
                    new_input_port = dst_port
                    break
            if source_output_port:
                break

        if not source_output_port or not new_input_port:
            return

        # Find downstream connections from the matched source output port
        downstream_conns = [
            c for c in self._node_graph.connections
            if c.src_node_id == source_node_id
            and c.src_port_name == source_output_port.name
        ]

        # Find compatible output port on new_node for downstream reconnection
        new_output_port = None
        for port in new_node.outputs:
            if port.data_type == source_output_port.data_type:
                new_output_port = port
                break

        # Reconnect downstream nodes through the new node
        if new_output_port and downstream_conns:
            for conn in downstream_conns:
                # Disconnect: source -> downstream
                self._actions.disconnect(
                    conn.src_node_id, conn.src_port_name,
                    conn.dst_node_id, conn.dst_port_name
                )
                # Reconnect: new_node -> downstream
                self._actions.connect(
                    new_node.id, new_output_port.name,
                    conn.dst_node_id, conn.dst_port_name
                )

        # Connect: source -> new_node
        self._actions.connect(
            source_node_id, source_output_port.name,
            new_node.id, new_input_port.name
        )

    # ------------------------------------------------------------------
    # Node extraction / disconnect helpers (used by shake & context menu)
    # ------------------------------------------------------------------

    def _extract_node_from_chain(self, node_id: str) -> None:
        """Extract a node from its connection chain.

        Before: upstream -> node -> downstream
        After:  upstream -> downstream   (node is disconnected)

        For each input connection (upstream -> node), finds a matching
        output connection (node -> downstream) by data_type and
        reconnects upstream directly to downstream.
        """
        if not self._actions or not self._node_graph:
            return

        # Collect input connections: (upstream_nid, upstream_port, shaken_input_port_name, data_type)
        input_conns = []
        # Collect output connections: (shaken_output_port_name, downstream_nid, downstream_port, data_type)
        output_conns = []

        for c in self._connection_items:
            _, src_nid, src_pname, dst_nid, dst_pname = c
            if dst_nid == node_id:
                src_node = self._node_graph.get_node(src_nid)
                if src_node:
                    for p in src_node.outputs:
                        if p.name == src_pname:
                            input_conns.append((src_nid, src_pname, dst_pname, p.data_type))
                            break
            elif src_nid == node_id:
                dst_node = self._node_graph.get_node(dst_nid)
                if dst_node:
                    for p in dst_node.inputs:
                        if p.name == dst_pname:
                            output_conns.append((src_pname, dst_nid, dst_pname, p.data_type))
                            break

        # Reconnect: for each input, find a matching output by data_type
        reconnected_outputs = set()
        for up_nid, up_pname, _, in_dtype in input_conns:
            for i, (_, dn_nid, dn_pname, out_dtype) in enumerate(output_conns):
                if i not in reconnected_outputs and out_dtype == in_dtype:
                    self._actions.connect(up_nid, up_pname, dn_nid, dn_pname)
                    reconnected_outputs.add(i)
                    break

        # Disconnect all connections involving the node
        connections_to_remove = [
            (c[1], c[2], c[3], c[4])
            for c in self._connection_items
            if c[1] == node_id or c[3] == node_id
        ]

        for src_nid, src_pname, dst_nid, dst_pname in connections_to_remove:
            self._actions.disconnect(src_nid, src_pname, dst_nid, dst_pname)

    def _execute_shake_disconnect(self) -> None:
        """Extract the shaken node from its connection chain via shake gesture.

        Uses batch mode to suppress intermediate graph_changed signals and
        disables visual updates to prevent UI freezing during the operation.
        """
        # Re-entrancy guard
        if self._shake_disconnecting:
            return
        if not self._shake_detector or not self._actions or not self._node_graph:
            return
        if not self._shake_detector.triggered:
            return

        if len(self._move_start_positions) != 1:
            return

        shaken_node_id = next(iter(self._move_start_positions))

        # Set guards immediately
        self._shake_disconnecting = True
        self._shake_triggered = True
        self._shake_detector = None

        # Clear visual state before batch operation
        self._clear_connection_highlight()
        for item in self._node_items.values():
            if getattr(item, '_shake_glow', 0) > 0:
                item._shake_glow = 0.0
        self._last_shake_glow_value = -1.0

        # Disable visual updates during batch operation to prevent freezing
        self.setUpdatesEnabled(False)
        self._batch_mode = True

        try:
            self._extract_node_from_chain(shaken_node_id)
        finally:
            # Always restore state even if an error occurs
            self._batch_mode = False
            self.setUpdatesEnabled(True)
            # Emit a single graph_changed for the entire batch
            self.graph_changed.emit()

        self._shake_disconnecting = False

    def _get_shake_sensitivity(self) -> float:
        """Read the shake-to-disconnect sensitivity (0-100) from shared config.

        Reads the live value from the main window's shared ``ConfigManager``
        (the same instance Preferences writes to) so changes take effect on the
        very next node drag. Falls back to the default (50) if config is
        unavailable, so detection still works standalone.
        """
        default = 50
        try:
            cm = getattr(self.window(), "config_manager", None)
            if cm is not None:
                val = cm.get("composite.shake_disconnect_sensitivity")
                if val is not None:
                    return float(val)
        except Exception:
            pass
        return default

    def _update_shake_glow(self) -> None:
        """Update the visual shake glow on the dragged node.

        Only triggers repaint when the glow value has actually changed
        significantly (≥0.05 delta) to avoid redundant repaints.
        """
        if not self._shake_detector:
            return

        if len(self._move_start_positions) != 1:
            return

        new_glow = self._shake_detector.glow_progress
        if abs(new_glow - self._last_shake_glow_value) < 0.05:
            return  # Skip if change is too small to be visible

        self._last_shake_glow_value = new_glow
        shaken_node_id = next(iter(self._move_start_positions))
        item = self._node_items.get(shaken_node_id)
        if item:
            item._shake_glow = new_glow
            item.update()

    # ------------------------------------------------------------------
    # Connection insertion dot helpers (Cmd/Ctrl + click)
    # ------------------------------------------------------------------

    def _show_connection_insertion_dots(self, show: bool) -> None:
        """Show or hide insertion dots on all connections."""
        for conn_data in self._connection_items:
            conn_item = conn_data[0]
            conn_item.set_show_insertion_dot(show)
        if not show:
            self._clear_insertion_dot_hover()

    def _update_insertion_dot_hover(self, scene_pos: QPointF) -> None:
        """Update hover state on insertion dots based on mouse position."""
        if not self._ctrl_insertion_active:
            return

        new_hovered = None
        for conn_data in self._connection_items:
            conn_item = conn_data[0]
            if conn_item.hit_test_insertion_dot(scene_pos):
                new_hovered = conn_item
                break

        # Clear previous hover
        if self._hovered_insertion_connection and self._hovered_insertion_connection is not new_hovered:
            self._hovered_insertion_connection._insertion_dot_hovered = False
            self._hovered_insertion_connection.update()

        # Set new hover
        if new_hovered:
            new_hovered._insertion_dot_hovered = True
            new_hovered.update()

        self._hovered_insertion_connection = new_hovered

    def _clear_insertion_dot_hover(self) -> None:
        """Clear any hovered insertion dot state."""
        if self._hovered_insertion_connection:
            self._hovered_insertion_connection._insertion_dot_hovered = False
            self._hovered_insertion_connection.update()
            self._hovered_insertion_connection = None

    def _handle_insertion_dot_click(self, scene_pos: QPointF) -> bool:
        """Handle click on an insertion dot to insert a Dot node.

        Returns True if a dot was inserted (caller should skip further processing).
        """
        if not self._ctrl_insertion_active:
            return False

        for conn_data in self._connection_items:
            conn_item = conn_data[0]
            if conn_item.hit_test_insertion_dot(scene_pos):
                # Found a hit — insert Dot node
                _, src_nid, src_pname, dst_nid, dst_pname = conn_data
                mid = conn_item.get_midpoint_scene()
                if self._actions:
                    self._actions.insert_dot_on_connection(
                        src_nid, src_pname, dst_nid, dst_pname,
                        insert_x=mid.x(), insert_y=mid.y(),
                    )
                return True

        return False

    # ------------------------------------------------------------------
    # Drop-on-connection helpers
    # ------------------------------------------------------------------

    def _detect_connection_proximity(self) -> None:
        """During node drag, detect if the node is near a connection line.

        Highlights the nearest compatible connection and stores it for
        potential auto-insert on release.

        Performance optimizations:
        - Skip scan if node hasn't moved enough since last check
        - Bounding box pre-filter before expensive Bezier subdivision
        - Only update highlight when the best connection actually changes
        """
        if not self._move_start_positions or not self._actions:
            return

        # Only single-node drag supports drop-on-connection
        if len(self._move_start_positions) != 1:
            self._clear_connection_highlight()
            return

        dragged_node_id = next(iter(self._move_start_positions))

        # Get the dragged node's center position
        dragged_item = self._node_items.get(dragged_node_id)
        if not dragged_item:
            return

        node_center = dragged_item.sceneBoundingRect().center()

        # Early exit: skip scan if node hasn't moved enough since last check
        if self._proximity_last_check_pos is not None:
            dx = node_center.x() - self._proximity_last_check_pos.x()
            dy = node_center.y() - self._proximity_last_check_pos.y()
            if dx * dx + dy * dy < 64:  # Less than 8px movement
                return
        self._proximity_last_check_pos = QPointF(node_center)

        # Find nearest compatible connection
        best_conn_data = None
        best_dist = float('inf')
        best_point = None

        # Wider tolerance than port snapping for node dropping
        node_snap_distance = ConnectionItem.SNAP_DISTANCE * 2.5  # ~37.5 pixels

        for conn_data in self._connection_items:
            conn_item = conn_data[0]
            _, conn_src_nid, _, conn_dst_nid, _ = conn_data

            # Skip connections that involve the dragged node (avoid self-insertion)
            if conn_src_nid == dragged_node_id or conn_dst_nid == dragged_node_id:
                continue

            # Cheap bounding box pre-filter: skip if node center is far from
            # the connection's scene bounding rect (expanded by snap distance)
            conn_rect = conn_item.sceneBoundingRect()
            expanded = conn_rect.adjusted(
                -node_snap_distance, -node_snap_distance,
                node_snap_distance, node_snap_distance
            )
            if not expanded.contains(node_center):
                continue

            # Expensive: get distance from node center to connection path
            nearest_point, dist = conn_item.get_nearest_point_on_path(node_center)

            if dist < node_snap_distance and dist < best_dist:
                # Check port type compatibility
                if self._can_insert_into_connection(dragged_node_id, conn_data):
                    best_dist = dist
                    best_conn_data = conn_data
                    best_point = nearest_point

        # Only update highlight when the best connection actually changed
        prev_conn = self._drop_insert_connection
        same_as_before = (
            prev_conn is not None and best_conn_data is not None
            and prev_conn[1] == best_conn_data[1]  # same src node
            and prev_conn[2] == best_conn_data[2]  # same src port
            and prev_conn[3] == best_conn_data[3]  # same dst node
            and prev_conn[4] == best_conn_data[4]  # same dst port
        )

        if same_as_before:
            # Same connection highlighted, just update nearest point
            self._drop_insert_nearest_point = best_point
            return

        # Connection changed — clear old highlight and apply new one
        self._clear_connection_highlight()

        if best_conn_data:
            best_conn_data[0].set_snap_highlight(True, True)
            self._drop_insert_connection = best_conn_data
            self._drop_insert_nearest_point = best_point

    def _can_insert_into_connection(self, node_id: str, conn_data: tuple) -> bool:
        """Check if a node can be inserted into a connection chain.

        The node must have at least one input port and one output port
        whose data types match the connection's data type.
        """
        if not self._node_graph:
            return False

        conn_item = conn_data[0]
        conn_data_type = conn_item.data_type

        node = self._node_graph.get_node(node_id)
        if not node:
            return False

        # Check if node has a compatible input port
        has_compatible_input = any(
            port.data_type == conn_data_type
            for port in node.inputs
        )

        # Check if node has a compatible output port
        has_compatible_output = any(
            port.data_type == conn_data_type
            for port in node.outputs
        )

        return has_compatible_input and has_compatible_output

    def _clear_connection_highlight(self) -> None:
        """Clear any active connection drop highlight."""
        if self._drop_insert_connection:
            conn_item = self._drop_insert_connection[0]
            if conn_item.scene():  # Check if still in scene
                conn_item.set_snap_highlight(False)
            self._drop_insert_connection = None
            self._drop_insert_nearest_point = None

    def _execute_connection_insertion(self) -> None:
        """Insert the dragged node into the highlighted connection chain.

        Before: original_source --[conn]--> original_destination
        After:  original_source --> dragged_node --> original_destination

        Uses batch mode to suppress intermediate graph_changed signals.

        Robustness: this runs inside ``mouseReleaseEvent``. If a
        disconnect/connect step raised (bad topology, cycle, occupied port),
        the old code let the exception propagate out of the release handler —
        skipping the highlight clear and leaving ``_moving_nodes=True``, which
        stuck the drag and presented as a frozen UI. Now any mutation failure
        restores the original connection, and the highlight + re-entrancy guard
        are always cleared in ``finally``, so a bad drop can never freeze the app.
        """
        if not self._drop_insert_connection or not self._actions or not self._node_graph:
            self._clear_connection_highlight()
            return

        # Re-entrancy guard — should never nest, but never let it corrupt state.
        if getattr(self, '_inserting_node', False):
            self._clear_connection_highlight()
            return
        self._inserting_node = True

        try:
            conn_data = self._drop_insert_connection
            conn_item, old_src_nid, old_src_pname, old_dst_nid, old_dst_pname = conn_data
            conn_data_type = conn_item.data_type

            # Find the dragged node (single selection)
            if len(self._move_start_positions) != 1:
                return

            dragged_node_id = next(iter(self._move_start_positions))

            # Don't insert if the node is already part of this connection
            if dragged_node_id == old_src_nid or dragged_node_id == old_dst_nid:
                return

            dragged_node = self._node_graph.get_node(dragged_node_id)
            if not dragged_node:
                return

            # Find compatible input port on dragged node
            new_input_port = None
            for port in dragged_node.inputs:
                if port.data_type == conn_data_type:
                    new_input_port = port
                    break

            # Find compatible output port on dragged node
            new_output_port = None
            for port in dragged_node.outputs:
                if port.data_type == conn_data_type:
                    new_output_port = port
                    break

            if not new_input_port or not new_output_port:
                return

            # Batch mode: suppress intermediate graph_changed signals.
            # setUpdatesEnabled(False) is paired inside try/finally so the view
            # can never be left with updates disabled after an error.
            self.setUpdatesEnabled(False)
            self._batch_mode = True
            disconnected = False

            try:
                # Disconnect the original connection
                self._actions.disconnect(old_src_nid, old_src_pname, old_dst_nid, old_dst_pname)
                disconnected = True

                # Connect: original_source --> dragged_node.input
                self._actions.connect(old_src_nid, old_src_pname, dragged_node_id, new_input_port.name)

                # Connect: dragged_node.output --> original_destination
                self._actions.connect(dragged_node_id, new_output_port.name, old_dst_nid, old_dst_pname)

                # Snap the node onto the connection on success only — a failed
                # insertion leaves the node where the user dropped it.
                if self._drop_insert_nearest_point:
                    dragged_item = self._node_items.get(dragged_node_id)
                    if dragged_item:
                        insert_pos = self._drop_insert_nearest_point
                        half_w = dragged_item.rect().width() / 2
                        half_h = dragged_item.rect().height() / 2
                        dragged_item.setPos(insert_pos.x() - half_w, insert_pos.y() - half_h)
            except Exception:
                # A mutation failed mid-splice. Restore the original connection
                # (if we had disconnected it) so the graph is never left
                # half-spliced, then swallow — never freeze the drag.
                logger.exception(
                    "Drop-on-connection insertion failed for node %s into "
                    "%s.%s -> %s.%s; restoring original connection.",
                    dragged_node_id, old_src_nid, old_src_pname, old_dst_nid, old_dst_pname,
                )
                if disconnected:
                    try:
                        self._actions.connect(
                            old_src_nid, old_src_pname, old_dst_nid, old_dst_pname
                        )
                    except Exception:
                        logger.exception(
                            "Failed to restore original connection %s.%s -> %s.%s "
                            "after aborted insertion.",
                            old_src_nid, old_src_pname, old_dst_nid, old_dst_pname,
                        )
            finally:
                self._batch_mode = False
                self.setUpdatesEnabled(True)
                self.graph_changed.emit()
        finally:
            self._inserting_node = False
            # Always clear the drop highlight + state on every path, including
            # validation aborts and mutation failures.
            self._clear_connection_highlight()

    def _on_node_double_clicked(self, node_id: str) -> None:
        """Handle node double-click.

        B3-1: double-clicking a node that belongs to a UI group requests
        entering that group's edit view; other node types keep emitting
        the generic signal (color-page switch etc.).
        """
        group_id = None
        if self._node_graph is not None and hasattr(self._node_graph, "get_group_for_node"):
            try:
                group = self._node_graph.get_group_for_node(node_id)
                group_id = group.get("id") if isinstance(group, dict) else None
            except Exception:
                group_id = None
        if group_id:
            self.group_enter_requested.emit(str(group_id))
            return
        self.node_double_clicked.emit(node_id)

    # ── B3-1: group edit view (enter/exit/filter) ─────────────────────────

    def is_in_group_view(self) -> bool:
        return bool(self._group_view_stack)

    @property
    def group_view_stack(self) -> list:
        return list(self._group_view_stack)

    def enter_group_view(self, group_id: str) -> bool:
        """Switch the canvas to the group's member subset.

        Applies EnterGroupCommand (is_expanded=True), hides non-member
        nodes and their connections, and records the breadcrumb level.
        The caller (page) owns the undo push. Returns False when the
        group does not exist.
        """
        if self._node_graph is None:
            return False
        from src.features.composite.ui.group_commands import EnterGroupCommand
        cmd = EnterGroupCommand(graph=self._node_graph, group_id=str(group_id))
        if not cmd.redo():
            return False
        members = self._group_member_ids(group_id)
        self._group_view_stack.append(str(group_id))
        self._group_filter_ids = set(members)
        self._apply_group_filter()
        self.group_view_changed.emit(str(group_id))
        return True

    def exit_group_view(self) -> bool:
        """Leave the deepest group level (breadcrumb back). Applies
        ExitGroupCommand; returns False when not inside a group view."""
        if not self._group_view_stack:
            return False
        group_id = self._group_view_stack.pop()
        if self._node_graph is not None:
            from src.features.composite.ui.group_commands import ExitGroupCommand
            ExitGroupCommand(graph=self._node_graph,
                             group_id=group_id).redo()
        if self._group_view_stack:
            # Returning to a parent group view: refilter to its members.
            parent = self._group_view_stack[-1]
            self._group_filter_ids = set(self._group_member_ids(parent))
        else:
            self._group_filter_ids = None
        self._apply_group_filter()
        self.group_view_changed.emit(
            self._group_view_stack[-1] if self._group_view_stack else None)
        return True

    def _group_member_ids(self, group_id: str) -> list:
        if self._node_graph is None:
            return []
        try:
            from src.features.composite.ui.group_commands import (
                get_nodes_in_group,
            )
            return list(get_nodes_in_group(self._node_graph, group_id))
        except Exception:
            return []

    def _apply_group_filter(self) -> None:
        """Show only the filtered subset (or everything when None)."""
        visible = self._group_filter_ids
        for node_id, item in self._node_items.items():
            item.setVisible(visible is None or node_id in visible)
        for entry in self._connection_items:
            # Entries are (ConnectionItem, src_id, src_port, dst_id, dst_port).
            conn_item, src_id, _sp, dst_id, _dp = entry
            conn_item.setVisible(
                visible is None or (src_id in visible and dst_id in visible))
        self.scene().update()

    def _selection_contains_locked_node(self) -> bool:
        """Return whether the current selection contains a locked node."""
        return any(
            item.isSelected() and item.is_locked
            for item in self._node_items.values()
        )

    def _connection_touches_locked_node(self,
                                        connection: ConnectionItem) -> bool:
        """Return whether a connection has a locked source or destination."""
        for conn_data in self._connection_items:
            if conn_data[0] is connection:
                return (self.is_node_locked(conn_data[1])
                        or self.is_node_locked(conn_data[3]))
        return False

    def _find_parent_dock(self):
        """Walk up the widget hierarchy to find the parent QDockWidget."""
        widget = self.parent()
        while widget:
            if isinstance(widget, QDockWidget):
                return widget
            widget = widget.parent()
        return None

    def _toggle_bypass_selected(self) -> None:
        """Toggle bypass/disable on selected nodes."""
        if self._selection_contains_locked_node():
            return
        if self._actions:
            self._actions.toggle_bypass_selected()

    def _duplicate_selected_nodes(self) -> None:
        """Duplicate all selected nodes, preserving parameters."""
        if self._actions:
            self._actions.duplicate_selected_nodes()

    def _copy_selected_nodes(self) -> None:
        """Copy selected nodes to clipboard with relative positions."""
        if self._actions:
            self._actions.copy_selected_nodes()

    def _cut_selected_nodes(self) -> None:
        """Cut (copy + delete) selected nodes."""
        if self._actions:
            self._actions.cut_selected_nodes()

    def _paste_nodes(self) -> None:
        """Paste nodes from clipboard."""
        if self._actions:
            self._actions.paste_nodes()

    def _add_new_backdrop(self, scene_pos: Optional[QPointF] = None) -> None:
        """Create a backdrop, auto-sizing to the current selection if any.

        Behavior:
        - Selected nodes → wrap their bounding box (auto-sized).
        - No selection + click position → center a default backdrop on it.
        - No selection + no position (e.g. "Alt+G" shortcut) → center on viewport.

        The new backdrop is selected for immediate move/resize/edit.
        """
        from .backdrop_item import BackdropItem

        selected = [nid for nid, it in self._node_items.items() if it.isSelected()]
        if selected:
            # Use sceneBoundingRect() (not it.x()/it.width()): NodeItem is a
            # QGraphicsRectItem with no .width()/.height(), and sceneBoundingRect
            # matches how BackdropItem.update_contained_nodes defines containment.
            items = [self._node_items[n] for n in selected if n in self._node_items]
            rects = [it.sceneBoundingRect() for it in items]
            x = min(r.left() for r in rects) - 20
            y = min(r.top() for r in rects) - 40
            w = max(r.right() for r in rects) - x + 40
            h = max(r.bottom() for r in rects) - y + 60
        else:
            if scene_pos is None:
                scene_pos = self.mapToScene(self.viewport().rect().center())
            w, h = 320.0, 220.0
            x = scene_pos.x() - w / 2
            y = scene_pos.y() - h / 2

        backdrop_id = str(uuid.uuid4())
        backdrop = BackdropItem(backdrop_id, "Backdrop")
        backdrop.setPos(x, y)
        backdrop.setRect(0, 0, w, h)
        self._scene.addItem(backdrop)
        self._wire_backdrop(backdrop)
        self._backdrop_items[backdrop_id] = backdrop

        # Refresh containment, persist to graph, and emit for undo integration.
        backdrop.update_contained_nodes(self._node_items)
        backdrop_data = backdrop.to_dict()
        if self._node_graph is not None:
            self._node_graph.set_backdrop(backdrop_id, backdrop_data)
        self.backdrop_created.emit(backdrop_id, backdrop_data)

        # Select the new backdrop for immediate interaction.
        self._scene.clearSelection()
        backdrop.setSelected(True)

    def _edit_backdrop(self, item: BackdropItem) -> None:
        """Open the edit dialog for a backdrop's title/note.

        The dialog applies changes via ``setTitle``/``setNote``, each of which
        fires ``_on_backdrop_changed`` (→ graph sync). No-op if cancelled.
        """
        item._open_edit_dialog()

    def _change_backdrop_color(self, item: BackdropItem) -> None:
        """Open the color picker for a backdrop.

        ``setColor`` is only called for a valid (accepted) color and fires
        ``_on_backdrop_changed`` to sync the graph. No-op if cancelled.
        """
        item._choose_color()

    def _group_selected_nodes(self) -> None:
        """Group selected nodes using group_commands."""
        if self._selection_contains_locked_node():
            return
        if self._actions:
            self._actions.group_selected_nodes()

    def _ungroup_selected_nodes(self) -> None:
        """Ungroup selected nodes."""
        if self._selection_contains_locked_node():
            return
        if self._actions:
            self._actions.ungroup_selected()

    def _select_upstream_nodes(self) -> None:
        """Select all upstream connected nodes."""
        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if not selected_ids:
            return
        
        upstream = set()
        for nid in selected_ids:
            upstream.update(self._get_upstream_nodes(nid))
        
        for nid in self._node_items:
            self._node_items[nid].set_selected(nid in upstream)

    def _select_downstream_nodes(self) -> None:
        """Select all downstream connected nodes."""
        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if not selected_ids:
            return
        
        downstream = set()
        for nid in selected_ids:
            downstream.update(self._get_downstream_nodes(nid))
        
        for nid in self._node_items:
            self._node_items[nid].set_selected(nid in downstream)

    def _get_upstream_nodes(self, node_id: str) -> set:
        """Get all nodes that feed into the given node (transitive closure).

        U-10：旧实现在递归前预标记邻居，递归入口发现“已访问”立即返回，
        闭包永远只到一跳。改为迭代 BFS，由每层入口统一标记；仅返回仍在
        图中的节点（坏边不产生虚假选择），深图不受递归上限约束。
        """
        result: set = set()
        if self._node_graph.get_node(node_id) is None:
            return result
        visited = {node_id}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            for conn in self._node_graph.get_input_connections(current):
                src_id = getattr(conn, "src_node_id", None)
                if not src_id or src_id in visited:
                    continue
                if self._node_graph.get_node(src_id) is None:
                    continue
                visited.add(src_id)
                result.add(src_id)
                queue.append(src_id)
        return result

    def _get_downstream_nodes(self, node_id: str) -> set:
        """Get all nodes that the given node feeds into (transitive closure).

        与 _get_upstream_nodes 同一 BFS 契约（U-10）。
        """
        result: set = set()
        if self._node_graph.get_node(node_id) is None:
            return result
        visited = {node_id}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            for conn in self._node_graph.get_output_connections(current):
                dst_id = getattr(conn, "dst_node_id", None)
                if not dst_id or dst_id in visited:
                    continue
                if self._node_graph.get_node(dst_id) is None:
                    continue
                visited.add(dst_id)
                result.add(dst_id)
                queue.append(dst_id)
        return result

    def _update_graph_positions(self) -> None:
        """Update node positions in the data model after dragging."""
        if self._node_graph is None:
            return
        for nid, item in self._node_items.items():
            node = self._node_graph.get_node(nid)
            if node:
                pos = item.pos()
                node.position = (pos.x(), pos.y())

    # ── Preset Operations ──

    def _show_save_preset_dialog(self) -> None:
        """显示保存预设对话框。"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QLabel, QLineEdit, QPushButton, QTextEdit, QMessageBox

        if not self._actions:
            return

        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if not selected_ids:
            QMessageBox.warning(self, "Save Preset", "Please select at least one node.")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(I18N.tr("node_editor.save_preset_title", "Save as Preset"))
        dialog.setMinimumWidth(400)
        dialog.setStyleSheet(PRESET_DIALOG_STYLE)

        layout = QVBoxLayout(dialog)

        # Name
        name_label = QLabel(I18N.tr("node_editor.preset_name_label", "Preset Name:"))
        layout.addWidget(name_label)
        name_edit = QLineEdit()
        name_edit.setPlaceholderText(
            I18N.tr("node_editor.preset_name_placeholder", "Enter preset name..."))
        layout.addWidget(name_edit)

        # Description
        desc_label = QLabel(
            I18N.tr("node_editor.preset_desc_label", "Description (optional):"))
        layout.addWidget(desc_label)
        desc_edit = QTextEdit()
        desc_edit.setMaximumHeight(100)
        desc_edit.setPlaceholderText(
            I18N.tr("node_editor.preset_desc_placeholder", "Describe this preset..."))
        layout.addWidget(desc_edit)

        # Info
        info_label = QLabel(f"Selected: {len(selected_ids)} nodes")
        layout.addWidget(info_label)

        # Buttons
        btn_layout = QVBoxLayout()
        save_btn = QPushButton(I18N.tr("node_editor.save", "Save"))
        save_btn.clicked.connect(dialog.accept)
        cancel_btn = QPushButton(I18N.tr("node_editor.cancel", "Cancel"))
        cancel_btn.clicked.connect(dialog.reject)
        btn_layout.addWidget(save_btn)
        btn_layout.addWidget(cancel_btn)
        layout.addLayout(btn_layout)

        if dialog.exec() == QDialog.DialogCode.Accepted:
            name = name_edit.text().strip()
            if not name:
                QMessageBox.warning(self, "Save Preset", "Please enter a preset name.")
                return

            description = desc_edit.toPlainText().strip()

            # 检查是否已存在
            preset_manager = self._actions.get_preset_manager()
            if preset_manager.preset_exists(name):
                reply = QMessageBox.question(
                    self, "Save Preset",
                    f"Preset '{name}' already exists. Overwrite?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

            success = self._actions.save_selected_as_preset(name, description)
            if success:
                QMessageBox.information(self, "Save Preset", f"Preset '{name}' saved successfully.")
            else:
                QMessageBox.warning(self, "Save Preset", "Failed to save preset.")

    def _show_import_preset_dialog(self) -> None:
        """显示导入预设对话框。"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        if not self._actions:
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Preset",
            "",
            "JSON Files (*.json);;All Files (*)"
        )

        if not file_path:
            return

        preset_name = self._actions.import_preset(file_path)
        if preset_name:
            QMessageBox.information(self, "Import Preset", f"Preset '{preset_name}' imported successfully.")
        else:
            QMessageBox.warning(self, "Import Preset", "Failed to import preset. The file may be invalid.")

    def _show_manage_presets_dialog(self) -> None:
        """显示管理预设对话框。"""
        from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton, QLabel

        if not self._actions:
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(
            I18N.tr("node_editor.manage_presets_title", "Manage Presets"))
        dialog.setMinimumSize(500, 400)
        dialog.setStyleSheet(MANAGE_PRESET_DIALOG_STYLE)

        layout = QVBoxLayout(dialog)

        # List
        list_label = QLabel(I18N.tr("node_editor.saved_presets_label", "Saved Presets:"))
        layout.addWidget(list_label)

        preset_list = QListWidget()
        layout.addWidget(preset_list)

        # Populate list
        preset_manager = self._actions.get_preset_manager()
        for preset_name in preset_manager.list_presets():
            info = preset_manager.get_preset_info(preset_name)
            if info:
                display_text = f"{preset_name} ({info['node_count']} nodes)"
                preset_list.addItem(display_text)
            else:
                preset_list.addItem(preset_name)

        # Buttons
        btn_layout = QHBoxLayout()

        load_btn = QPushButton(I18N.tr("node_editor.load", "Load"))
        load_btn.clicked.connect(lambda: self._load_selected_preset(dialog, preset_list))
        btn_layout.addWidget(load_btn)

        rename_btn = QPushButton(I18N.tr("node_editor.rename", "Rename"))
        rename_btn.clicked.connect(lambda: self._rename_selected_preset(preset_list))
        btn_layout.addWidget(rename_btn)

        delete_btn = QPushButton(I18N.tr("node_editor.delete", "Delete"))
        delete_btn.clicked.connect(lambda: self._delete_selected_preset(preset_list))
        btn_layout.addWidget(delete_btn)

        export_btn = QPushButton(I18N.tr("node_editor.export", "Export"))
        export_btn.clicked.connect(lambda: self._export_selected_preset(preset_list))
        btn_layout.addWidget(export_btn)

        close_btn = QPushButton(I18N.tr("node_editor.close", "Close"))
        close_btn.clicked.connect(dialog.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

        dialog.exec()

    def _load_selected_preset(self, dialog: "QDialog", preset_list: "QListWidget") -> None:
        """加载列表中选中的预设。"""
        if not self._actions:
            return

        current_item = preset_list.currentItem()
        if not current_item:
            return

        # 从显示文本中提取名称（去掉节点数信息）
        display_text = current_item.text()
        preset_name = display_text.split(" (")[0]

        # 在视图中心加载
        center = self.mapToScene(self.viewport().rect().center())
        self._actions.load_preset(preset_name, (center.x(), center.y()))

        dialog.accept()

    def _rename_selected_preset(self, preset_list: "QListWidget") -> None:
        """重命名列表中选中的预设。"""
        from PyQt6.QtWidgets import QInputDialog, QMessageBox

        if not self._actions:
            return

        current_item = preset_list.currentItem()
        if not current_item:
            return

        display_text = current_item.text()
        old_name = display_text.split(" (")[0]

        new_name, ok = QInputDialog.getText(
            self, "Rename Preset", "New name:",
            text=old_name
        )

        if ok and new_name.strip():
            preset_manager = self._actions.get_preset_manager()
            success = preset_manager.rename_preset(old_name, new_name.strip())
            if success:
                # 更新列表显示
                info = preset_manager.get_preset_info(new_name.strip())
                if info:
                    current_item.setText(f"{new_name.strip()} ({info['node_count']} nodes)")
                else:
                    current_item.setText(new_name.strip())
            else:
                QMessageBox.warning(self, "Rename Preset", "Failed to rename preset.")

    def _delete_selected_preset(self, preset_list: "QListWidget") -> None:
        """删除列表中选中的预设。"""
        from PyQt6.QtWidgets import QMessageBox

        if not self._actions:
            return

        current_item = preset_list.currentItem()
        if not current_item:
            return

        display_text = current_item.text()
        preset_name = display_text.split(" (")[0]

        reply = QMessageBox.question(
            self, "Delete Preset",
            f"Delete preset '{preset_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            success = self._actions.delete_preset(preset_name)
            if success:
                preset_list.takeItem(preset_list.row(current_item))
            else:
                QMessageBox.warning(self, "Delete Preset", "Failed to delete preset.")

    def _export_selected_preset(self, preset_list: "QListWidget") -> None:
        """导出列表中选中的预设。"""
        from PyQt6.QtWidgets import QFileDialog, QMessageBox

        if not self._actions:
            return

        current_item = preset_list.currentItem()
        if not current_item:
            return

        display_text = current_item.text()
        preset_name = display_text.split(" (")[0]

        file_path, _ = QFileDialog.getSaveFileName(
            self, "Export Preset",
            f"{preset_name}.json",
            "JSON Files (*.json)"
        )

        if file_path:
            success = self._actions.export_preset(preset_name, file_path)
            if success:
                QMessageBox.information(self, "Export Preset", f"Preset exported to '{file_path}'.")
            else:
                QMessageBox.warning(self, "Export Preset", "Failed to export preset.")

    def _load_preset_at_position(self, preset_name: str, scene_pos: QPointF) -> None:
        """在指定位置加载预设。"""
        if self._actions:
            self._actions.load_preset(preset_name, (scene_pos.x(), scene_pos.y()))

    # ── Layout Operations ──

    def _auto_layout_selected_nodes(self) -> None:
        """自动排布选中的节点。

        使用拓扑排序算法，将输入节点放左侧，输出节点放右侧。
        """
        if (not self._layout or not self._node_graph
                or self._selection_contains_locked_node()):
            return

        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if len(selected_ids) < 2:
            return

        nodes = [self._node_graph.get_node(nid) for nid in selected_ids]
        nodes = [n for n in nodes if n is not None]

        new_positions = self._layout.auto_layout(nodes)

        if not new_positions:
            return

        # 记录旧位置用于 undo
        old_positions = []
        moved_ids = []

        for nid, (x, y) in new_positions.items():
            item = self._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                old_positions.append(old_pos)
                moved_ids.append(nid)
                item.setPos(x, y)

        # 更新数据模型
        self._update_graph_positions()

        # 更新连接路径
        for conn_data in self._connection_items:
            conn_data[0].update_path()

        # 发送信号用于 undo
        if moved_ids:
            self.nodes_moved.emit(moved_ids, old_positions)
            self.graph_changed.emit()

    def _align_selected_nodes(self, direction: str) -> None:
        """对齐选中的节点。

        Args:
            direction: 对齐方向 ("left", "right", "top", "bottom",
                       "center_horizontal", "center_vertical")
        """
        if (not self._layout or not self._node_graph
                or self._selection_contains_locked_node()):
            return

        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if len(selected_ids) < 2:
            return

        nodes = [self._node_graph.get_node(nid) for nid in selected_ids]
        nodes = [n for n in nodes if n is not None]

        # 获取对齐结果
        if direction == "left":
            new_coords = self._layout.align_left(nodes)
            coord_key = "x"
        elif direction == "right":
            new_coords = self._layout.align_right(nodes)
            coord_key = "x"
        elif direction == "top":
            new_coords = self._layout.align_top(nodes)
            coord_key = "y"
        elif direction == "bottom":
            new_coords = self._layout.align_bottom(nodes)
            coord_key = "y"
        elif direction == "center_horizontal":
            new_coords = self._layout.align_center_horizontal(nodes)
            coord_key = "y"
        elif direction == "center_vertical":
            new_coords = self._layout.align_center_vertical(nodes)
            coord_key = "x"
        else:
            return

        if not new_coords:
            return

        # 记录旧位置用于 undo
        old_positions = []
        moved_ids = []

        for nid, new_val in new_coords.items():
            item = self._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                old_positions.append(old_pos)
                moved_ids.append(nid)

                if coord_key == "x":
                    item.setPos(new_val, item.pos().y())
                else:
                    item.setPos(item.pos().x(), new_val)

        # 更新数据模型
        self._update_graph_positions()

        # 更新连接路径
        for conn_data in self._connection_items:
            conn_data[0].update_path()

        # 发送信号用于 undo
        if moved_ids:
            self.nodes_moved.emit(moved_ids, old_positions)
            self.graph_changed.emit()

    def _distribute_selected_nodes(self, direction: str) -> None:
        """均匀分布选中的节点。

        Args:
            direction: 分布方向 ("horizontal", "vertical")
        """
        if (not self._layout or not self._node_graph
                or self._selection_contains_locked_node()):
            return

        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if len(selected_ids) < 2:
            return

        nodes = [self._node_graph.get_node(nid) for nid in selected_ids]
        nodes = [n for n in nodes if n is not None]

        # 获取分布结果
        if direction == "horizontal":
            new_coords = self._layout.distribute_horizontal(nodes)
            coord_key = "x"
        elif direction == "vertical":
            new_coords = self._layout.distribute_vertical(nodes)
            coord_key = "y"
        else:
            return

        if not new_coords:
            return

        # 记录旧位置用于 undo
        old_positions = []
        moved_ids = []

        for nid, new_val in new_coords.items():
            item = self._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                old_positions.append(old_pos)
                moved_ids.append(nid)

                if coord_key == "x":
                    item.setPos(new_val, item.pos().y())
                else:
                    item.setPos(item.pos().x(), new_val)

        # 更新数据模型
        self._update_graph_positions()

        # 更新连接路径
        for conn_data in self._connection_items:
            conn_data[0].update_path()

        # 发送信号用于 undo
        if moved_ids:
            self.nodes_moved.emit(moved_ids, old_positions)
            self.graph_changed.emit()

    def _cleanup_overlaps_selected_nodes(self) -> None:
        """清理选中节点的重叠。

        微调位置避免节点重叠。
        """
        if (not self._layout or not self._node_graph
                or self._selection_contains_locked_node()):
            return

        selected_ids = [nid for nid, item in self._node_items.items() if item.isSelected()]
        if len(selected_ids) < 2:
            return

        nodes = [self._node_graph.get_node(nid) for nid in selected_ids]
        nodes = [n for n in nodes if n is not None]

        new_positions = self._layout.cleanup_overlaps(nodes)

        if not new_positions:
            return

        # 记录旧位置用于 undo
        old_positions = []
        moved_ids = []

        for nid, (x, y) in new_positions.items():
            item = self._node_items.get(nid)
            if item:
                old_pos = (item.pos().x(), item.pos().y())
                old_positions.append(old_pos)
                moved_ids.append(nid)
                item.setPos(x, y)

        # 更新数据模型
        self._update_graph_positions()

        # 更新连接路径
        for conn_data in self._connection_items:
            conn_data[0].update_path()

        # 发送信号用于 undo
        if moved_ids:
            self.nodes_moved.emit(moved_ids, old_positions)
            self.graph_changed.emit()

    def get_snap_state(self) -> bool:
        return self._snap_to_grid

    def set_snap_state(self, enabled: bool) -> None:
        self._snap_to_grid = enabled

    def _find_nearest_valid_port(self, scene_pos: QPointF) -> Optional[PortItem]:
        """Find the nearest valid port for connection snapping.

        Performance: First tries Qt's spatial index for fast lookup at the
        mouse position, then falls back to distance calculation if needed.

        Returns None if no port is within PORT_SNAP_DISTANCE.
        """
        if not self._dragging_connection:
            return None

        # Determine what direction of port we're looking for
        if self._drag_direction == "forward":
            target_direction = "input"
            source_data_type = self._drag_src_port.data_type if self._drag_src_port else None
            source_node_id = self._drag_src_node_id
        elif self._drag_direction == "reverse":
            target_direction = "output"
            source_data_type = self._drag_dst_port.data_type if self._drag_dst_port else None
            source_node_id = None
            # Get the dst node id for cycle checking
            dst_parent = self._drag_dst_port.parentItem() if self._drag_dst_port else None
            source_node_id = dst_parent.node_id if isinstance(dst_parent, NodeItem) else None
        else:
            return None

        # Performance: First try fast lookup at exact position using Qt's spatial index
        items_at_pos = self._scene.items(scene_pos)
        for item in items_at_pos:
            port = None
            if isinstance(item, PortItem):
                port = item
            elif isinstance(item.parentItem(), PortItem):
                port = item.parentItem()

            if port and port.direction == target_direction:
                # Check validity
                parent = port.parentItem()
                if isinstance(parent, NodeItem):
                    # Skip self-connection
                    if parent.node_id == source_node_id:
                        continue
                    # Check data type
                    if source_data_type and port.data_type != source_data_type:
                        continue
                    # 多输入：输入端口满载（达 max_connections）才跳过，
                    # 否则仍可继续连接（如 Scene3D 的 scene 端口支持多输入）。
                    if target_direction == "input" and port.is_at_capacity():
                        continue
                    return port

        # Fallback: distance-based search for ports near but not at the position
        # Use squared distance to avoid sqrt in comparisons
        best_port = None
        best_dist_sq = self.PORT_SNAP_DISTANCE ** 2

        # Performance: Filter to viewport only to avoid O(n) on large node graphs
        viewport_rect = self.mapToScene(self.viewport().rect()).boundingRect()

        for nid, node_item in self._node_items.items():
            # Skip off-screen nodes for performance
            if not viewport_rect.intersects(node_item.sceneBoundingRect()):
                continue
            # Skip the node we're dragging from (for forward drag)
            if self._drag_direction == "forward" and nid == self._drag_src_node_id:
                continue
            # Skip the destination node (for reverse drag)
            if self._drag_direction == "reverse" and nid == source_node_id:
                continue

            ports = (node_item.input_ports if target_direction == "input"
                     else node_item.output_ports)

            for pname, port in ports.items():
                if port.direction != target_direction:
                    continue

                # Check data type compatibility
                if source_data_type and port.data_type != source_data_type:
                    continue

                # For input ports in forward drag, skip only if at capacity (multi-input aware)
                if target_direction == "input" and port.is_at_capacity():
                    continue

                port_pos = port.get_center_scene_pos()
                # Use squared distance for comparison (avoids sqrt)
                dist_sq = (scene_pos.x() - port_pos.x()) ** 2 + \
                          (scene_pos.y() - port_pos.y()) ** 2

                if dist_sq < best_dist_sq:
                    best_dist_sq = dist_sq
                    best_port = port

        # ── 节点级自动匹配（仅 3D 场景节点）──
        # 端口级吸附失败（best_port 仍为 None）时，若鼠标落在节点主体上，
        # 按源/目标节点类型自动选择匹配端口。复用现有 snap 高亮机制——
        # 拖 Camera3D 掠过 Scene3D 主体时，camera_in 端口会自动亮绿环。
        if best_port is None:
            best_port = self._scene_auto_match_port(scene_pos, source_data_type)

        return best_port

    def _scene_auto_match_port(self, scene_pos: QPointF,
                               source_data_type) -> Optional[PortItem]:
        """节点级自动匹配：连接线落在节点主体（非端口）上时按类型选端口。

        仅作用于 scene 类型轨道 + Scene3D 节点（2D image 保持精确到端口）。
        - 正向（output→input）：源是 3D 源节点，目标节点是 Scene3D →
          按 SCENE_AUTO_MATCH[src.node_type] 选 camera_in/lights_in/geometry_in。
        - 反向（input→output）：从 Scene3D 输入端口拉出，悬停 3D 源节点主体 →
          按拉出端口名反查期望的源类型，吸附其 display 输出端口。

        端口级精确瞄准优先（_find_nearest_valid_port 先做端口搜索），此为回退。
        """
        node_item = self._find_node_at(scene_pos)
        if node_item is None:
            return None

        if self._drag_direction == "forward":
            # 正向：源是 3D 源节点 → Scene3D 输入端口
            src_item = self._node_items.get(self._drag_src_node_id) if self._drag_src_node_id else None
            if src_item is None:
                return None
            if node_item.node_id == self._drag_src_node_id:
                return None
            if node_item.node_type != "scene3d":
                return None
            target_name = SCENE_AUTO_MATCH.get(src_item.node_type)
            if not target_name:
                return None
            port = node_item.input_ports.get(target_name)
            if port is None:
                return None
            if source_data_type and port.data_type != source_data_type:
                return None
            if port.is_at_capacity():
                return None
            return port

        elif self._drag_direction == "reverse":
            # 反向：从 Scene3D 输入端口拉出 → 3D 源节点的 display 输出
            if self._drag_dst_port is None:
                return None
            src_port_name = self._drag_dst_port.port_name
            expected_types = SCENE_AUTO_MATCH_REVERSE.get(src_port_name)
            if not expected_types:
                return None
            if node_item.node_type not in expected_types:
                return None
            dst_parent = self._drag_dst_port.parentItem()
            if dst_parent is node_item:
                return None
            port = node_item.output_ports.get("display")
            if port is None or port.direction != "output":
                return None
            if source_data_type and port.data_type != source_data_type:
                return None
            return port

        return None

    def _find_nearest_valid_connection(self, scene_pos: QPointF) -> Optional[ConnectionItem]:
        """Find the nearest connection for insertion snap.

        A connection is a valid insertion target if:
        1. It has the same data type as the dragged connection
        2. The source/destination nodes are not the same as the drag source
           (to avoid creating cycles or self-connections)

        Returns None if no connection is within SNAP_DISTANCE.
        """
        if not self._dragging_connection:
            return None

        # Get source data type from the drag
        source_data_type = None
        if self._drag_direction == "forward" and self._drag_src_port:
            source_data_type = self._drag_src_port.data_type
        elif self._drag_direction == "reverse" and self._drag_dst_port:
            source_data_type = self._drag_dst_port.data_type

        best_conn = None
        best_dist = float('inf')
        snap_dist = ConnectionItem.SNAP_DISTANCE

        # Check all connections
        for conn_data in self._connection_items:
            conn = conn_data[0]

            # Skip if data type mismatch
            if source_data_type and conn.data_type != source_data_type:
                continue

            # Skip self-connection scenarios
            # Get connection source and destination node IDs
            _, conn_src_nid, _, conn_dst_nid, _ = conn_data

            # For forward drag: new source -> insert point
            # The new source should not be the same as the connection's destination
            if self._drag_direction == "forward" and self._drag_src_node_id:
                if self._drag_src_node_id == conn_dst_nid:
                    continue  # Would create direct cycle

            # For reverse drag: insert point -> new destination
            # The new destination should not be the same as the connection's source
            if self._drag_direction == "reverse":
                dst_parent = self._drag_dst_port.parentItem() if self._drag_dst_port else None
                dst_nid = dst_parent.node_id if isinstance(dst_parent, NodeItem) else None
                if dst_nid and dst_nid == conn_src_nid:
                    continue  # Would create direct cycle

            # Get nearest point and distance
            nearest_point, dist = conn.get_nearest_point_on_path(scene_pos)

            if dist < snap_dist and dist < best_dist:
                best_dist = dist
                best_conn = conn

        return best_conn

    def _handle_connection_insertion(
        self, target_conn: ConnectionItem, insert_point: QPointF
    ) -> None:
        """Handle inserting a Dot node into an existing connection.

        This is triggered when a new connection is dropped onto an existing connection.
        The operation:
        1. Creates a Dot node at the insertion point
        2. Splits the target connection, inserting the Dot
        3. Connects the new connection to the appropriate side of the Dot

        Behavior:
        - Forward drag (output -> connection): new source replaces original source
          Result: new_source -> Dot -> original_destination
        - Reverse drag (connection -> input): new destination replaces original destination
          Result: original_source -> Dot -> new_destination

        Args:
            target_conn: The connection to insert into
            insert_point: The scene position where the Dot node should be placed
        """
        if not self._actions:
            return

        # Find target connection data
        conn_data = None
        for c in self._connection_items:
            if c[0] is target_conn:
                conn_data = c
                break

        if not conn_data:
            return

        _, old_src_nid, old_src_pname, old_dst_nid, old_dst_pname = conn_data

        # Determine replacement based on drag direction
        new_src_nid = None
        new_src_pname = None
        new_dst_nid = None
        new_dst_pname = None

        if self._drag_direction == "forward" and self._drag_src_port and self._drag_src_node_id:
            # Forward drag: new source replaces original source
            new_src_nid = self._drag_src_node_id
            new_src_pname = self._drag_src_port.port_name

        elif self._drag_direction == "reverse" and self._drag_dst_port:
            # Reverse drag: new destination replaces original destination
            dst_parent = self._drag_dst_port.parentItem()
            if isinstance(dst_parent, NodeItem):
                new_dst_nid = dst_parent.node_id
                new_dst_pname = self._drag_dst_port.port_name

        # Use the unified insert_dot_on_connection method
        self._actions.insert_dot_on_connection(
            old_src_node=old_src_nid,
            old_src_port=old_src_pname,
            old_dst_node=old_dst_nid,
            old_dst_port=old_dst_pname,
            new_src_node=new_src_nid,
            new_src_port=new_src_pname,
            new_dst_node=new_dst_nid,
            new_dst_port=new_dst_pname,
            insert_x=insert_point.x(),
            insert_y=insert_point.y(),
        )

    def fit_selected_nodes(self) -> None:
        """Fit only the selected nodes in the viewport."""
        selected_rects = [item.sceneBoundingRect()
                          for nid, item in self._node_items.items()
                          if item.isSelected()]
        if not selected_rects:
            return
        rect = QRectF()
        for r in selected_rects:
            rect = rect.united(r)
        if rect.isValid():
            self.fitInView(rect.adjusted(-60, -40, 60, 40),
                           Qt.AspectRatioMode.KeepAspectRatio)
            # Sync zoom state after fitInView changes the transform
            transform = self.transform()
            self._current_zoom = transform.m11()
            self.zoom_changed.emit(self._current_zoom)
            self._record_navigation_point()

    def get_zoom_level(self) -> float:
        """Return the current zoom level."""
        return self._current_zoom

    # ── UI State Serialization ──

    def _navigation_viewport(self) -> dict[str, float]:
        """Return the compact viewport payload used by session history."""
        center = self.mapToScene(self.viewport().rect().center())
        return {
            "zoom": float(self._current_zoom),
            "center_x": float(center.x()),
            "center_y": float(center.y()),
        }

    def _record_navigation_point(self) -> None:
        """Record a changed view without persisting history in the project."""
        if self._navigation_restore_active or not self._node_items:
            return
        self._navigation.record_view(
            self._navigation_viewport(), self._selected_node_id)

    def _apply_navigation_entry(self, entry: dict) -> bool:
        """Apply a bookmark/history entry and restore its optional selection."""
        viewport = entry.get("viewport", entry)
        if not isinstance(viewport, dict):
            return False
        self._navigation_restore_active = True
        try:
            self.set_viewport_state(viewport)
        finally:
            self._navigation_restore_active = False

        node_id = entry.get("selected_node_id")
        if node_id and node_id in self._node_items:
            for nid, item in self._node_items.items():
                item.set_selected(nid == node_id)
            self._selected_node_id = str(node_id)
            self.node_selected.emit(str(node_id))
            self._apply_preview_mode()
        return True

    def add_view_bookmark(self, name: str) -> bool:
        """Create or replace a named bookmark at the current view."""
        try:
            self._navigation.add_bookmark(
                name, self._navigation_viewport(), self._selected_node_id)
        except ValueError:
            return False
        self.save_ui_state()
        self.graph_changed.emit()
        return True

    def remove_view_bookmark(self, name: str) -> bool:
        """Remove a named bookmark and persist the UI metadata change."""
        removed = self._navigation.remove_bookmark(name)
        if removed:
            self.save_ui_state()
            self.graph_changed.emit()
        return removed

    def navigate_to_bookmark(self, name: str) -> bool:
        """Jump to a saved bookmark, if it still exists."""
        bookmark = self._navigation.get_bookmark(name)
        return bookmark is not None and self._apply_navigation_entry(
            bookmark.to_dict())

    def navigate_back(self) -> bool:
        """Navigate to the previous session view."""
        entry = self._navigation.back()
        return entry is not None and self._apply_navigation_entry(entry)

    def navigate_forward(self) -> bool:
        """Navigate to the next session view."""
        entry = self._navigation.forward()
        return entry is not None and self._apply_navigation_entry(entry)

    def _focus_seed_ids(self, preferred_node_id: Optional[str] = None) -> set[str]:
        """Return selected nodes, falling back to a context-menu target."""
        selected = {
            nid for nid, item in self._node_items.items() if item.isSelected()
        }
        if selected:
            return selected
        if preferred_node_id and preferred_node_id in self._node_items:
            return {preferred_node_id}
        return set()

    def _dependency_ids_for(self, seed_ids: set[str]) -> set[str]:
        """Return the connected dependency closure around graph seeds."""
        related = set(seed_ids)
        for node_id in tuple(seed_ids):
            related.update(self._get_upstream_nodes(node_id))
            related.update(self._get_downstream_nodes(node_id))
        return related

    def _apply_graph_focus(self) -> None:
        """Apply canvas-only opacity and border state to nodes and connections."""
        focus_ids = self._branch_isolation_ids
        if focus_ids is None and self._dependency_highlight_ids:
            focus_ids = self._dependency_highlight_ids

        for node_id, item in self._node_items.items():
            item.set_dependency_highlighted(
                node_id in self._dependency_highlight_ids)
            if focus_ids is None:
                item.setOpacity(1.0)
            elif node_id in focus_ids:
                item.setOpacity(1.0)
            else:
                # Keep unrelated nodes clickable and visible, but make the
                # selected dependency branch visually dominant.
                item.setOpacity(0.12 if self._branch_isolation_ids is not None
                                else 0.30)

        for conn, src_node_id, _src_port, dst_node_id, _dst_port in self._connection_items:
            if focus_ids is None:
                conn.setOpacity(1.0)
            elif src_node_id in focus_ids and dst_node_id in focus_ids:
                conn.setOpacity(1.0)
            else:
                conn.setOpacity(0.10 if self._branch_isolation_ids is not None
                                 else 0.25)
        self.viewport().update()

    def highlight_selected_dependencies(
            self, preferred_node_id: Optional[str] = None) -> set[str]:
        """Highlight the upstream/downstream closure without changing selection."""
        seed_ids = self._focus_seed_ids(preferred_node_id)
        if not seed_ids:
            return set()
        self._branch_isolation_ids = None
        self._dependency_highlight_ids = self._dependency_ids_for(seed_ids)
        self._apply_graph_focus()
        return set(self._dependency_highlight_ids)

    def isolate_selected_branch(
            self, preferred_node_id: Optional[str] = None) -> set[str]:
        """Dim unrelated nodes and connections around the current branch."""
        seed_ids = self._focus_seed_ids(preferred_node_id)
        if not seed_ids:
            return set()
        self._branch_isolation_ids = self._dependency_ids_for(seed_ids)
        self._dependency_highlight_ids = set(self._branch_isolation_ids)
        self._apply_graph_focus()
        return set(self._branch_isolation_ids)

    def clear_graph_focus(self) -> None:
        """Clear dependency highlighting and branch isolation."""
        self._branch_isolation_ids = None
        self._dependency_highlight_ids.clear()
        self._apply_graph_focus()

    @property
    def graph_focus_ids(self) -> Optional[set[str]]:
        """Return the active branch focus, or ``None`` when canvas is normal."""
        if self._branch_isolation_ids is not None:
            return set(self._branch_isolation_ids)
        if self._dependency_highlight_ids:
            return set(self._dependency_highlight_ids)
        return None

    def set_node_diagnostics(self, diagnostics: object) -> None:
        """Render grouped preflight issues as non-destructive node badges."""
        grouped: Dict[str, list[tuple[str, str, str]]] = {}
        raw_issues = getattr(diagnostics, "issues", diagnostics)
        if isinstance(raw_issues, dict):
            iterable = []
            for node_id, issue in raw_issues.items():
                if isinstance(issue, (list, tuple)):
                    iterable.extend((node_id, candidate) for candidate in issue)
                else:
                    iterable.append((node_id, issue))
        else:
            try:
                iterable = list(raw_issues or [])
            except TypeError:
                iterable = []

        for entry in iterable:
            explicit_node_id = None
            issue = entry
            if isinstance(entry, tuple) and len(entry) == 2:
                explicit_node_id, issue = entry
            node_id = str(getattr(issue, "node_id", "") or explicit_node_id or "")
            if not node_id or node_id not in self._node_items:
                continue
            level = str(getattr(issue, "severity", "info") or "info").lower()
            code = str(getattr(issue, "code", "") or "diagnostic")
            message = str(getattr(issue, "message", issue) or "")
            suggestion = str(getattr(issue, "suggestion", "") or "")
            detail = f"[{code}] {message}"
            if suggestion:
                detail += f" — {suggestion}"
            grouped.setdefault(node_id, []).append((level, detail, code))

        priority = {"info": 0, "warning": 1, "error": 2}
        for item in self._node_items.values():
            item.clear_diagnostic()
        for node_id, entries in grouped.items():
            level = max((entry[0] for entry in entries),
                        key=lambda value: priority.get(value, 0))
            message = "\n".join(entry[1] for entry in entries)
            self._node_items[node_id].set_diagnostic(level, message)

    def clear_node_diagnostics(self) -> None:
        """Clear all preflight badges from the canvas."""
        for item in self._node_items.values():
            item.clear_diagnostic()

    def set_node_locked(self, node_id: str, locked: bool) -> bool:
        """Set a node's canvas read-only state and persist the UI metadata."""
        item = self._node_items.get(str(node_id))
        if item is None:
            return False
        node_id = str(node_id)
        locked = bool(locked)
        if locked:
            self._locked_node_ids.add(node_id)
        else:
            self._locked_node_ids.discard(node_id)
        item.set_locked(locked)
        self.save_ui_state()
        self.node_lock_changed.emit(node_id, locked)
        self.graph_changed.emit()
        return True

    def toggle_node_lock(self, node_id: str) -> bool:
        """Toggle one node's canvas read-only state."""
        item = self._node_items.get(str(node_id))
        return (item is not None
                and self.set_node_locked(str(node_id), not item.is_locked))

    def is_node_locked(self, node_id: str) -> bool:
        """Return whether a node is currently locked in the editor."""
        return str(node_id) in self._locked_node_ids

    # ── B4: in-canvas node rename ──

    def begin_node_rename(self, node_id: str) -> None:
        """Start inline rename editing on a node's canvas title.

        Fires :attr:`node_rename_requested`(node_id, new_label) when the
        user commits; the page owns the model change and the undo step.
        Empty committed text clears the custom label (falls back to the
        auto number).

        AUD-10500: COLOR/Output nodes cannot be renamed — their canonical
        titles are load-bearing for identity in the project pipeline, and a
        rename used to bypass every downstream title-based protection.
        """
        item = self._node_items.get(str(node_id))
        if item is None or item.is_locked or item.is_renaming:
            return
        if self._is_protected_node_type(node_id):
            return

        def _commit(text: str) -> None:
            self.node_rename_requested.emit(str(node_id), text)

        item.begin_inline_rename(_commit)

    def _rename_selected_node(self) -> None:
        """F2 handler: rename the primary selected node."""
        if self._selected_node_id:
            self.begin_node_rename(self._selected_node_id)

    # ── C5: Read/Write version binning ──

    _VERSIONABLE_NODE_TYPES = frozenset({"image_input", "file_output"})

    def _version_step_selected(self, delta: int) -> None:
        """[ / ] handler: step the selected Read/Write node's version."""
        node_id = self._selected_node_id
        if not node_id:
            return
        node = (self._node_graph.get_node(node_id)
                if self._node_graph is not None else None)
        if node is None or node.node_type not in self._VERSIONABLE_NODE_TYPES:
            return
        if self.is_node_locked(node_id):
            return
        self.node_version_step_requested.emit(str(node_id), int(delta))

    def set_node_user_label(self, node_id: str, label: Optional[str]) -> bool:
        """Apply a user label to the node model and refresh the canvas title.

        Called by the page on both the initial rename and every undo/redo
        replay so the visual title always matches ``node.user_label``.
        """
        node = (self._node_graph.get_node(str(node_id))
                if self._node_graph is not None else None)
        item = self._node_items.get(str(node_id))
        if item is None:
            return False
        if node is not None:
            node.user_label = (str(label).strip() or None) if label else None
            item.set_title(node.display_title)
        else:
            item.set_title(str(label) if label else item.node_title)
        return True

    def _prompt_add_view_bookmark(self) -> None:
        """Ask for a bookmark name and save the current graph view."""
        default_name = f"View {len(self._navigation.bookmarks()) + 1}"
        name, accepted = QInputDialog.getText(
            self, "Add View Bookmark", "Bookmark name:", text=default_name)
        if accepted:
            self.add_view_bookmark(name)

    def _build_navigation_submenu(self, parent_menu: QMenu) -> dict:
        """Build the graph view navigation menu and return its actions."""
        submenu = parent_menu.addMenu("View Navigation")
        action_back = submenu.addAction(I18N.tr("node_editor.nav_back", "Back"))
        action_back.setEnabled(self._navigation.can_go_back)
        action_forward = submenu.addAction(I18N.tr("node_editor.nav_forward", "Forward"))
        action_forward.setEnabled(self._navigation.can_go_forward)
        submenu.addSeparator()
        action_add = submenu.addAction(
            I18N.tr("node_editor.add_view_bookmark", "Add View Bookmark..."))

        bookmarks_menu = submenu.addMenu("Bookmarks")
        bookmark_actions: Dict[str, QAction] = {}
        for bookmark in self._navigation.bookmarks():
            action = bookmarks_menu.addAction(bookmark.name)
            bookmark_actions[bookmark.name] = action
        if not bookmark_actions:
            empty = bookmarks_menu.addAction(
                I18N.tr("node_editor.no_bookmarks", "(No bookmarks)"))
            empty.setEnabled(False)

        remove_menu = submenu.addMenu("Remove Bookmark")
        remove_actions: Dict[str, QAction] = {}
        for bookmark in self._navigation.bookmarks():
            action = remove_menu.addAction(bookmark.name)
            remove_actions[bookmark.name] = action
        if not remove_actions:
            empty = remove_menu.addAction(
                I18N.tr("node_editor.no_bookmarks", "(No bookmarks)"))
            empty.setEnabled(False)

        return {
            "back": action_back,
            "forward": action_forward,
            "add": action_add,
            "bookmarks": bookmark_actions,
            "remove": remove_actions,
        }

    def _build_focus_submenu(self, parent_menu: QMenu, enabled: bool) -> dict:
        """Build the non-destructive branch focus menu for the node canvas."""
        submenu = parent_menu.addMenu("Graph Focus")
        action_isolate = submenu.addAction(
            I18N.tr("node_editor.isolate_branch", "Isolate Selected Branch"))
        action_dependencies = submenu.addAction(
            I18N.tr("node_editor.highlight_dependencies", "Highlight Dependencies"))
        action_clear = submenu.addAction(
            I18N.tr("node_editor.clear_graph_focus", "Clear Graph Focus"))
        action_isolate.setEnabled(enabled)
        action_dependencies.setEnabled(enabled)
        action_clear.setEnabled(self.graph_focus_ids is not None)
        return {
            "isolate": action_isolate,
            "dependencies": action_dependencies,
            "clear": action_clear,
        }

    def get_viewport_state(self) -> dict:
        """Get current viewport state (zoom and center position).

        Returns:
            dict with keys: zoom, center_x, center_y
        """
        center = self.mapToScene(self.viewport().rect().center())
        return {
            "zoom": self._current_zoom,
            "center_x": center.x(),
            "center_y": center.y(),
            "bookmarks": self._navigation.to_dict(),
            "locked_node_ids": sorted(self._locked_node_ids),
        }

    def set_viewport_state(self, state: dict) -> None:
        """Restore viewport state from saved dict.

        Args:
            state: dict with keys: zoom, center_x, center_y
        """
        if not state:
            return

        if "bookmarks" in state:
            self._navigation = GraphNavigationState.from_dict(state)
        if "locked_node_ids" in state:
            raw_locked = state.get("locked_node_ids")
            self._locked_node_ids = {
                str(node_id) for node_id in raw_locked
                if str(node_id) in self._node_items
            } if isinstance(raw_locked, (list, tuple, set)) else set()
            for node_id, item in self._node_items.items():
                item.set_locked(node_id in self._locked_node_ids)

        # AUD-10506: ``state`` comes from persisted project JSON — a null or
        # string value for an existing key bypasses the .get() default and
        # used to raise TypeError inside build_from_graph, aborting the
        # whole canvas build. Validate and fall back to the defaults.
        zoom = self._coerce_viewport_number(state.get("zoom"), 1.0)
        center_x = self._coerce_viewport_number(state.get("center_x"), 0.0)
        center_y = self._coerce_viewport_number(state.get("center_y"), 0.0)

        # Clamp zoom to valid range
        zoom = max(self.MIN_ZOOM, min(self.MAX_ZOOM, zoom))

        # Apply zoom transform
        self._current_zoom = zoom
        self.setTransform(QTransform().scale(zoom, zoom))
        self.zoom_changed.emit(zoom)

        # Center on saved position
        self.centerOn(QPointF(center_x, center_y))
        self.viewport_moved.emit()

    @staticmethod
    def _coerce_viewport_number(value: Any, default: float) -> float:
        """Validate a viewport number deserialized from a project file.

        Bools are ints in Python but meaningless as zoom/center values, so
        they are rejected along with every other non-numeric type.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return float(value)

    def get_viewer_state(self) -> dict:
        """Get current viewer state (selected/viewing node).

        Returns:
            dict with keys: viewing_node_id, selected_node_id
        """
        return {
            "viewing_node_id": getattr(self, '_viewing_node_id', None),
            "selected_node_id": self._selected_node_id,
        }

    def set_viewer_state(self, state: dict) -> None:
        """Restore viewer state from saved dict.

        Args:
            state: dict with keys: viewing_node_id, selected_node_id
        """
        if not state:
            return

        viewing_node_id = state.get("viewing_node_id")
        selected_node_id = state.get("selected_node_id")

        # Restore selection
        if selected_node_id and selected_node_id in self._node_items:
            # Select only this node
            for nid, it in self._node_items.items():
                it.set_selected(nid == selected_node_id)
            self._selected_node_id = selected_node_id
            self.node_selected.emit(selected_node_id)

        # Store viewing node ID for later use by composite viewer
        if viewing_node_id:
            self._viewing_node_id = viewing_node_id

    def save_ui_state(self) -> None:
        """Save current UI state to the node graph."""
        if self._node_graph is None:
            return

        self._node_graph._viewport_state = self.get_viewport_state()
        viewer_state = dict(getattr(self._node_graph, "_viewer_state", {}) or {})
        viewer_state.update(self.get_viewer_state())
        self._node_graph._viewer_state = viewer_state

    def restore_ui_state(self) -> None:
        """Restore UI state from the node graph."""
        if self._node_graph is None:
            return

        # Restore viewport state
        if self._node_graph._viewport_state:
            self.set_viewport_state(self._node_graph._viewport_state)

        # Restore viewer state
        if self._node_graph._viewer_state:
            self.set_viewer_state(self._node_graph._viewer_state)

    # ── Performance Optimization Methods ──

    def _update_lod_mode(self) -> None:
        """Update LOD (Level of Detail) mode based on current zoom level."""
        is_simplified = self._current_zoom < self._simplified_draw_threshold
        
        # Update all node items
        for node_item in self._node_items.values():
            node_item.set_simplified_draw(is_simplified)
        
        # Update connection animations
        for conn_data in self._connection_items:
            conn = conn_data[0]
            if is_simplified:
                conn.slowdown_animation()
            else:
                conn.resume_animation()

    def start_batch_update(self) -> None:
        """Start a batch update to reduce redraws during multiple changes."""
        self._batch_update_active = True
        self._pending_updates.clear()

    def end_batch_update(self) -> None:
        """End batch update and flush all pending updates."""
        self._batch_update_active = False
        self._flush_batch_updates()

    def _flush_batch_updates(self) -> None:
        """Flush all pending batch updates."""
        if not self._pending_updates:
            return
        
        # Update only the items that changed
        for item in self._pending_updates:
            if isinstance(item, NodeItem):
                item.update()
            elif isinstance(item, ConnectionItem):
                item.update_path()
                item.update()
        
        self._pending_updates.clear()
        self.viewport().update()

    def request_update(self, item) -> None:
        """Request an update for an item, batching if active."""
        if self._batch_update_active:
            self._pending_updates.add(item)
            # Use a short timer to auto-flush
            self._batch_update_timer.start(50)  # 50ms debounce
        else:
            if isinstance(item, NodeItem):
                item.update()
            elif isinstance(item, ConnectionItem):
                item.update_path()
                item.update()

    # ── Viewport Node Detection (for Thumbnail System) ──

    def get_nodes_in_viewport(self) -> List[str]:
        """Get node IDs that are currently visible in the viewport.

        Used by thumbnail system to limit thumbnail generation to
        visible nodes only (performance optimization).

        Returns:
            List of node_ids for nodes whose bounding rect intersects
            the current viewport.
        """
        viewport_rect = self.mapToScene(self.viewport().rect()).boundingRect()
        visible_ids: List[str] = []

        for node_id, node_item in self._node_items.items():
            if viewport_rect.intersects(node_item.sceneBoundingRect()):
                visible_ids.append(node_id)

        return visible_ids

    def get_selected_node_id(self) -> Optional[str]:
        """Get the currently selected node ID.

        Returns:
            The selected node ID, or None if no node is selected.
        """
        return self._selected_node_id

    def select_node(self, node_id: str, emit: bool = True) -> bool:
        """Select one node programmatically (for cross-panel synchronization).

        The regular mouse-selection path remains unchanged.  This small public
        entry point lets panels such as the 3D Outliner select the owning graph
        node without reaching into ``_node_items`` from outside the editor.
        """
        item = self._node_items.get(str(node_id))
        if item is None:
            return False
        selected_id = str(node_id)
        for nid, other in self._node_items.items():
            other.set_selected(nid == selected_id)
        self._selected_node_id = selected_id
        if emit:
            self.node_selected.emit(selected_id)
        return True

    def get_node_item(self, node_id: str) -> Optional[NodeItem]:
        """Get the NodeItem for a specific node ID.

        Args:
            node_id: The node ID to look up.

        Returns:
            NodeItem instance or None if not found.
        """
        return self._node_items.get(node_id)
