"""
Composite Page - Node editor panel for the Composite page.

The viewport display is handled by an independent CompositeViewer
that replaces the Canvas in the viewport dock when the Composite
page is active.

Manages:
- The CompositeRenderer (independent GPU thread)
- The node editor (QGraphicsView based)
- Node graph state
- Undo/Redo support
- Properties panel integration
- Thumbnail preview system
"""

import copy
import logging
import time
from typing import Any, Optional, List, Tuple

import numpy as np

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QToolButton, QMenu, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QActionGroup, QKeySequence, QShortcut

from src.features.composite.render.composite_renderer import CompositeRenderer, CompositeCommand
from src.features.composite.render.thumbnail_policy import CompositeThumbnailPolicy, ThumbnailThrottleState, compute_thumbnail_requests

logger = logging.getLogger(__name__)
from src.features.composite.render.render_scheduler import RenderRequest, RequestIdGenerator, RenderMode, PrefetchRequest
from src.features.composite.render.prefetch_scheduler import PrefetchScheduler, PrefetchPrediction
from src.features.composite.nodes.node_state import NodeState
from src.features.composite.core.node_engine import NodeGraph, NODE_REGISTRY
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.media_drop_payload import (
    resolve_drop_payloads, resolve_asset_ids, MEDIA_TYPE_MODEL3D,
    MEDIA_TYPE_VIDEO,
)
from src.features.composite.nodes.color_node import COLORNode  # Deprecated - kept for isinstance checks
from src.features.composite.nodes.topos_color_node import ToposColorNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.image_input_node import ImageInputNode
from src.features.composite.nodes.blend_node import MergeNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.edit.core.composite_graph_data import (
    PROJECT_BACKGROUND_PARAM,
    CONSTANT_COLOR_SPACE_PARAM,
)
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.grade_node import GradeNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.invert_node import InvertNode
from src.features.composite.nodes.glow_node import GlowNode
from src.features.composite.nodes.denoise_node import DenoiseNode
from src.features.composite.nodes.mask_node import MaskNode
from src.features.composite.nodes.premult_node import PremultNode
from src.features.composite.nodes.unpremult_node import UnpremultNode
from src.features.composite.nodes.shuffle_node import ShuffleNode
from src.features.composite.nodes.copy_channel_node import CopyChannelNode
from src.features.composite.nodes.cs_transform_node import CSTransformNode
from src.features.composite.nodes.reformat_node import ReformatNode
from src.features.composite.nodes.crop_node import CropNode
from src.features.composite.nodes.group_node import GroupNode
from src.features.composite.core.undo_stack import (
    UndoStack, UndoAction,
    create_param_change_action,
    create_move_nodes_action,
    create_add_node_action,
    create_remove_node_action,
    create_connect_action,
    create_disconnect_action,
)
from .node_editor_widget import NodeEditorWidget
from .minimap_widget import MinimapWidget
from .node_thumbnail import NodeThumbnailManager, linear_rgb_to_display
from .viewer_tool_system import ViewerToolMode, NodeOutputRegion
from src.shared.ui.style_sheet import (
    C,
    COMPOSITE_TOOLBAR_STYLE,
    COMPOSITE_TOOLBAR_TITLE_STYLE,
    COMPOSITE_ADD_NODE_BTN_STYLE,
    COMPOSITE_SETTINGS_BTN_STYLE,
    ZOOM_LABEL_STYLE,
)
from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_scope import shortcut_owner_window_active


def _modifiers_have_shift(modifiers) -> bool:
    """True when the modifier mask contains Shift.

    PyQt6 strict enums reject ``&`` between int and enum in both operand
    orders, so normalize both sides through ``.value`` before masking.
    """
    shift = int(getattr(Qt.KeyboardModifier.ShiftModifier, "value",
                        Qt.KeyboardModifier.ShiftModifier))
    value = int(getattr(modifiers, "value", modifiers))
    return bool(shift & value)


def _page_node_is_locked(owner: object, node_id: object) -> bool:
    """Read the editor lock state while tolerating lightweight test doubles."""
    editor = getattr(owner, "_node_editor", None)
    is_locked = getattr(editor, "is_node_locked", None)
    if not callable(is_locked) or node_id is None:
        return False
    # The real editor returns bool.  Requiring that exact type keeps a
    # lightweight MagicMock used by handler tests from becoming truthy and
    # accidentally blocking an otherwise unrelated parameter edit.
    return is_locked(str(node_id)) is True


class _FrameReadyTrampoline:
    """VideoContext 单例帧就绪回调的弱引用蹦床（T-P1-71）。

    直接注册 ``signal.emit``（C 函数槽，无接收者上下文）会让全局单例在
    页面析构 + 包装回收后持有悬空回调——解码 worker 线程调用即原生段
    错误。蹦床持页面弱引用：页面已回收 → no-op；包装仍在而 C++ 已亡 →
    emit 抛 RuntimeError，由 VideoContext.notify_frame_ready 的 except
    分支降级为调试日志（其文档化的预期路径）。
    """

    __slots__ = ("_ref",)

    def __init__(self, page: "CompositePage") -> None:
        import weakref

        self._ref = weakref.ref(page)

    def __call__(self) -> None:
        page = self._ref()
        if page is not None:
            page._emit_video_node_frame_ready()


class CompositePage(QWidget):
    """
    The Composite page panel. Contains the node graph editor.

    The viewport display is handled by an independent CompositeViewer
    in the viewport dock, managed by MainWindow.
    """

    # Signal: request switching to Color page
    switch_to_color_requested = pyqtSignal()

    # Signal: composite frame ready (request_id for UI stale detection)
    composite_frame_ready = pyqtSignal(int, int, int, int)  # request_id, tex_id, width, height
    # Signal: overlay metadata ready (format/bbox for viewport display)
    composite_metadata_ready = pyqtSignal(dict)
    # Signal: status message for UI feedback
    status_message = pyqtSignal(str)  # message text
    # Signal: async preview image ready (non-blocking readback result)
    composite_preview_image_ready = pyqtSignal(int, object, int, int)  # request_id, BGR ndarray, w, h
    # Signal: native GPU scope frame descriptor (ScopeGpuFrame, texture ids
    # only). Same payload contract as the Color page's gpu_scope_ready.
    composite_gpu_scope_ready = pyqtSignal(object)

    # Signal: fires once per composite session when user makes a real edit
    # (add/remove/connect nodes, paste, duplicate, parameter change).
    # Used by EditController to trigger Comp Clip promotion.
    # Argument: composite_id (str) of the current composite session.
    composite_user_edit_started = pyqtSignal(str)
    composite_graph_changed = pyqtSignal(str, dict)

    # Signal: the composite playhead frame changed (comp-clip playback).
    # Argument: new frame number. The transport bar listens to refresh its
    # time/frame display as the comp-clip timer advances the playhead.
    composite_frame_changed = pyqtSignal(int)

    # Signal: Output node "Render Output" button clicked.
    # Argument: node_id of the Output node.
    render_output_requested = pyqtSignal(str)

    # Signal: create a new persistent Comp Clip from the empty-state CTA.
    # The MainWindow handles the existing settings dialog/project workflow.
    create_empty_comp_clip_requested = pyqtSignal()

    # Signal: 某个视频节点的当前帧已在后台解码完成（持帧预览路径）。
    # 工作线程经 VideoContext.notify_frame_ready → 本信号 emit（自动队列
    # 连接回 UI 线程）→ 重求值把就绪帧上屏。
    _video_node_frame_ready = pyqtSignal()

    _VIEWER3D_VIRTUAL_NODE_ID = "__viewer3d_scene__"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._video_node_frame_ready.connect(self._on_video_node_frame_ready)
        try:
            from src.features.composite.core.video_context import VideoContext
            # T-P1-71（composite 套件第二处段错误根因）：此前直接注册
            # ``self._video_node_frame_ready.emit``——``.emit`` 的 C 函数槽
            # 无接收者上下文，页面析构 + 包装回收后全局单例仍持悬空回调，
            # 解码 worker 线程调用即原生段错误（try/except 挡不住；
            # deflicker 真实视频套件崩溃的根因）。改用弱引用蹦床：页面
            # 已回收 → no-op；包装仍在而 C++ 已亡 → RuntimeError 由
            # VideoContext.notify_frame_ready 的 except 降级（其文档化
            # 的预期路径）。注意 destroyed 信号在 PyQt 中不能连 Python
            # 槽（析构时连接已被丢弃），不能用作注销时机。
            self._frame_ready_handler_ref = _FrameReadyTrampoline(self)
            VideoContext.get_instance().set_frame_ready_handler(
                self._frame_ready_handler_ref
            )
        except Exception:
            logger.debug("CompositePage: video frame-ready handler not registered")

        self._renderer = None
        self._main_gl_context = None
        self._is_active = False
        self._last_texture_id = None
        self._last_texture_width = 0
        self._last_texture_height = 0
        self._last_metadata = {}  # format/bbox overlay data
        # Model3D diagnostics are received as plain metadata from the render
        # thread. Keep the latest summary so selecting a Model3D node before
        # its next frame still populates the optional report/outliner docks.
        self._last_model_animation_clips: list[dict] = []
        self._last_model_import_report: Optional[dict] = None
        self._last_model_import_status: str = ""
        self._last_model_import_phase: str = ""
        self._last_model_import_progress: float = 0.0
        self._last_model_import_diagnostics: tuple[str, ...] = ()
        self._last_model_import_resource_estimate: Optional[dict] = None
        self._last_model_import_probe: Optional[dict] = None
        self._last_model_runtime_diagnostics: tuple[dict, ...] = ()
        self._last_model_source_path: str = ""
        self._last_model_node_id: str = ""
        self._last_model_hierarchy: Optional[dict] = None
        self._last_model_hidden_mesh_ids: tuple[str, ...] = ()
        self._last_model_locked_mesh_ids: tuple[str, ...] = ()
        self._last_model_solo_mesh_id: str = ""
        self._last_model_materials: list[dict] = []
        # P1-14: project context used by the read-only asset report and the
        # page-level collect/relink operations.  It is deliberately plain
        # path data; no asset operation touches the render thread.
        self._asset_project_dir: Optional[str] = None
        self._asset_search_roots: tuple[str, ...] = ()

        # Track ColorOutput state for snapshot submission
        self._last_color_texture_id = 0
        self._last_color_width = 0
        self._last_color_height = 0
        self._last_color_revision = 0
        self._force_next_view_refresh = False
        # Tracks the image size last synced into the Output node, so the
        # composition canvas follows the source image until the user picks a
        # custom resolution. None = not yet synced (or user overrode it).
        self._synced_output_image_size: Optional[tuple] = None
        # Full-resolution source dimensions (from engine.img_full_original),
        # injected by the adapter. The Composite preview always renders at the
        # 2048-px proxy for interactivity, but Project Settings and the Output
        # node should reflect the *real* source size so exports default to
        # full res. None = unknown; fall back to the proxy dimensions.
        self._source_image_size: Optional[tuple] = None
        # Set by _apply_project_settings: the user has manually chosen a
        # composition resolution, so stop auto-following the source image.
        self._output_resolution_user_locked: bool = False

        # Stale frame detection: track last submitted request_id
        self._last_submitted_request_id = 0
        # Presentation must be monotonic, but it must not require a completed
        # frame to be the request most recently *submitted*. During playback
        # the completed frame normally trails the playhead by one or more ticks.
        self._last_presented_frame_request_id = 0
        self._last_presented_preview_request_id = 0

        # Runtime playhead updates are not graph edits. Keep the most recent
        # graph payload so normal playback does not persist an identical graph
        # for every decoded frame.
        self._last_published_graph_data: Optional[dict] = None
        # AUD-9508: mutation-counter gate so the hot request path skips the
        # full-graph to_dict() + deep compare when nothing changed.
        # -1 = "never published / force publish on next evaluation".
        self._last_published_graph_generation: int = -1

        # Dirty flag: set when Color page changes while Composite is inactive
        self._color_dirty = False

        # Renderer init retry timer (for deferred GL context availability)
        self._renderer_retry_timer = QTimer(self)
        self._renderer_retry_timer.setSingleShot(True)
        self._renderer_retry_timer.setInterval(300)
        # GL context 初始化状态：用于事件驱动 + 限流，避免在 GL context 尚未
        # ready 时无限轮询刷屏。canvas 的 gl_viewport 完成 initializeGL() 后会
        # 发射 gl_initialized 信号 —— 那才是 renderer 初始化的正确同步点，而非
        # 定时器盲轮询。
        self._gl_retry_count = 0                # 盲轮询重试计数（封顶用）
        self._gl_init_warned = False            # "No GL context" 是否已 warning 过
        self._gl_signal_connected = False       # gl_initialized 信号是否已连接
        self._gl_viewport_ref: Optional[Any] = None  # 缓存引用，供信号槽使用
        self._renderer_retry_timer.timeout.connect(self._retry_renderer_init)

        # AI pending computations: cache_key -> node_id
        self._ai_pending: dict = {}

        # B6: AI compute manager must live on a thread with a Qt event
        # loop (worker slots are queued connections). The page's thread
        # runs one; the render thread's manual queue loop does not.
        # Created here, shared with the renderer (single disk-cache
        # instance so the evaluator sees new cache entries immediately).
        from src.features.composite.render.ai_cache_manager import AICacheManager
        from src.features.composite.render.ai_compute_manager import AIComputeManager
        self._ai_cache_manager = AICacheManager()
        self._ai_compute_manager = AIComputeManager(self._ai_cache_manager)

        # Node graph (data model)
        self._node_graph = NodeGraph()

        # Thumbnail manager for node previews
        self._thumbnail_manager: Optional[NodeThumbnailManager] = None

        # Page-level Tab fallback: Qt may route Tab through focus traversal
        # before the viewport sees it when focus is in the node graph or panel.
        self._view_mode_tab_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Tab), self)
        self._view_mode_tab_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._view_mode_tab_shortcut.activated.connect(self._toggle_view_mode_from_shortcut)

        # Undo stack
        self._undo_stack = UndoStack(self)
        # AE/Nuke hybrid controller. It shares the page's NodeGraph and undo
        # stack so layer edits are atomic with the rest of the composition.
        self._layer_stack_controller = LayerStackController(
            self._node_graph, self._undo_stack, parent=self)

        # Parameter change debounce timer
        self._param_debounce_timer = QTimer(self)
        self._param_debounce_timer.setSingleShot(True)
        self._param_debounce_timer.setInterval(80)  # ms
        self._param_debounce_timer.timeout.connect(self._flush_param_changes)
        self._pending_param_change = None  # (node_id, param_name, old_value, new_value)

        # Batch parameter change debounce timer (for multi-selection)
        self._batch_param_debounce_timer = QTimer(self)
        self._batch_param_debounce_timer.setSingleShot(True)
        self._batch_param_debounce_timer.setInterval(80)  # ms
        self._batch_param_debounce_timer.timeout.connect(self._flush_batch_param_changes)
        self._pending_batch_param_change = None  # (node_ids, param_name, old_values, new_value)

        # U-16：粘贴/预设装配的静默连线事务标志（nodes_pasting→nodes_pasted）
        self._compound_paste_active = False

        # Graph evaluation debounce timer — coalesces rapid _evaluate_graph() calls
        # (e.g., from add_node + auto_connect triggering multiple signal handlers)
        self._eval_debounce_timer = QTimer(self)
        self._eval_debounce_timer.setSingleShot(True)
        self._eval_debounce_timer.setInterval(16)  # ~1 frame at 60fps
        self._eval_debounce_timer.timeout.connect(self._do_evaluate_graph)

        # Minimap update debounce timer — coalesces rapid graph_changed signals
        self._minimap_timer = QTimer(self)
        self._minimap_timer.setSingleShot(True)
        self._minimap_timer.setInterval(50)  # ms
        # T-P1-71（composite 套件间歇性 SIGSEGV 根因）：必须连接绑定方法，
        # 不能连 lambda——lambda 槽不带接收者上下文，页面 deleteLater 与
        # Python 包装生命周期错位时，已武装的 50ms 单发 timer 会把信号发进
        # 已释放的槽 PyObject（PyQtSlot::invoke 内段错误，.ips 栈定位）。
        # 绑定方法让 PyQt 注册本页为接收者，析构时 Qt 自动断开连接。
        self._minimap_timer.timeout.connect(self._update_minimap_deferred)

        # Comp Clip promotion tracking: fire composite_user_edit_started only once
        self._user_edit_fired = False
        self._current_composite_id = ""  # set by load_composite_data / on_enter
        # True only while a real composition is open (or an intentional
        # standalone-image composition is active).  A bare Composite page is
        # still navigable, but its node workspace stays read-only until this
        # becomes True.
        self._composition_active = False
        # Composite-local Shift+Z. Edit's TimelineView owns its own Shift+Z
        # meaning, so this shortcut is enabled only while this page is active
        # and an actual composition is open.
        self._fit_composition_shortcut = QShortcut(
            QKeySequence("Shift+Z"), self)
        self._fit_composition_shortcut.setContext(
            Qt.ShortcutContext.WindowShortcut)
        self._fit_composition_shortcut.activated.connect(
            self._fit_keyframes_to_composition)
        self._fit_composition_shortcut.setEnabled(False)
        # AUD-9504: host-bound panel. The host assigns this via the
        # ``properties_panel`` setter / ``set_properties_panel`` after
        # construction, but slots such as ``on_enter`` and
        # ``_on_project_settings_requested`` read the attribute directly.
        # Initialize it here so those entry points see a defined ``None``
        # instead of raising AttributeError out of a Qt slot when called
        # before binding (test doubles, plugin-style integration, host
        # binding-order changes).
        self._properties_panel: Optional[Any] = None
        self._node_toolbar = None
        self._node_toolbar_controls: list[QWidget] = []
        self._empty_state_overlay = None
        self._empty_state_create_button = None

        # Prefetch system: scheduler and timer
        self._prefetch_scheduler = PrefetchScheduler()
        self._prefetch_scheduler.set_prefetch_callback(self._trigger_prefetch_request)
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(40)  # ms (fires during debounce period)
        self._prefetch_timer.timeout.connect(self._on_prefetch_timeout)
        self._active_prefetch_prediction: Optional[PrefetchPrediction] = None
        self._prefetch_enabled = True  # Can be disabled for performance

        # Render request ID generator (thread-safe, used from UI thread)
        self._request_id_gen = RequestIdGenerator()

        # Current render mode for interactive evaluation
        self._current_render_mode = RenderMode.PREVIEW

        # 预览代理分辨率（1.0=全分辨率，0.5=半分辨率，0.25=1/4）。
        # 仅影响预览渲染（经 LOD 强制降采样），导出恒走原始分辨率。
        self._preview_quality: float = 1.0
        # B2-1: "auto" resolution tier — downgrade to half while the user
        # interacts with the view, restore the base tier when idle.
        self._resolution_auto: bool = False
        self._auto_proxy_saved_quality = None
        # P2-05: selected terminal output target. Legacy projects default to
        # the first/last-compatible ``display`` output when no name exists.
        self._output_target_name: str = "display"
        self._output_target_node_id: Optional[str] = None

        # Optional business-level context for timeline clip source nodes
        # Populated by Comp Clip integration (EditController / CompositeFrameRenderer)
        self._runtime_context = None           # CompositeRuntimeContext
        self._timeline_clip_gpu_provider = None  # TimelineClipFrameProvider
        self._project = None                   # EditProject for ToposColor stack lookup
        # NodeEditorActions reconnects graph edges before emitting its
        # removal signal. Keep the pre-edit layer/node snapshot so deleting a
        # material node can be committed as one undoable Layer transaction.
        self._pending_layer_node_removal_snapshot = None
        self._pending_layer_node_removal_ids: list[str] = []
        # 片段在时间线上的起始帧（由 set_timeline_clip_context 设置），
        # 供 page_controller 做 transport bar 全局帧↔clip 本地帧映射。
        self._comp_clip_timeline_start: int = 0

        # P8: current composition-local frame used to drive keyframe animation.
        # For a Comp Clip this mirrors clip_local_frame; for an independent
        # composite it is the Keyframes/Spline playhead position. All preview
        # snapshot() calls pass this so animated params interpolate correctly
        # (matching the export path, which iterates frames explicitly).
        self._composite_current_frame: int = 0
        # P0-1: anti-recursion guard. When a panel emits current_frame_changed
        # and we write the new frame BACK to the other panels, those set_*
        # calls must not re-emit and bounce back. Set True while we are the
        # originator of a frame sync.
        self._syncing_composite_frame: bool = False
        # Comp-clip timeline metadata (loaded by load_composite_data). Drives
        # the independent comp-clip playback timer + bottom transport bar when
        # there is no timeline/video controller backing this composition.
        self._composite_duration_frames: int = 0
        self._composite_fps: float = 24.0
        self._is_comp_clip: bool = False
        # Image Color mode: when True, the page is used for static image
        # compositing only — keyframe/animation/playback inputs are hidden and
        # disabled. Node effects (color/blur/transform/mix/...) stay fully
        # usable. Set via set_image_color_mode() from page_controller when
        # entering the page, based on the studio work_mode preference.
        self._is_image_color_mode: bool = False

        # ── Viewer Interaction State (S5-13) ───────────────────────────────
        self._viewer = None  # CompositeViewer reference (set by set_viewer())
        self._interaction_original_params: dict = {}  # Original params before interaction
        self._interaction_active = False  # Flag for active viewer interaction
        # Snapshot of the dragged node's keyframe_tracks at interaction start
        # so animated handle drags get track-based undo (AE style).
        self._interaction_tracks_before: dict = {}
        # Milestone B / P1：3D 导航（Alt+orbit/pan/zoom）交互态。
        # handle 拖拽用 _interaction_active；Alt 导航用 _nav3d_active。两者都应
        # 让 Scene3D 进入 NAVIGATION profile（降 MSAA/IBL/分辨率）。request 的
        # interaction_active = _interaction_active OR _nav3d_active。
        self._nav3d_active: bool = False
        # P1-5：导航恢复 timer——鼠标释放后延迟 restore_delay_ms 才真正回到高质量
        # （PREVIEW profile）。避免连续小停顿时在低/高质量间反复抖动。timer 触发时
        # 清 _nav3d_active 并请求一帧高质量渲染。新一次 nav_started 会 stop 此 timer。
        # 延迟从全局 3D Scene Settings 的 nav_restore_delay_ms 读（默认 150ms）。
        self._nav3d_restore_timer = QTimer(self)
        self._nav3d_restore_timer.setSingleShot(True)
        self._nav3d_restore_timer.timeout.connect(self._on_nav3d_restore)
        # Phase 2: 3D navigation undo bookkeeping (fused viewer, no separate widget)
        self._viewer3d_nav_before: dict = {}  # cam params before a 3D nav drag
        self._viewer3d_node_id: Optional[str] = None  # node being navigated in 3D
        self._viewer3d_active_camera_id: Optional[str] = None

        # ── Node-Preview Pipeline ───────────────────────────────────────────
        # None ⇒ viewer shows the Output node's display (legacy behaviour).
        # Otherwise the viewer shows this node's output (Fusion/Nuke-style
        # "preview any node"). Toggled by the "1" shortcut on the selected node,
        # with a dashed preview connection drawn node → Output. See
        # ``docs/composite_node_preview_pipeline_plan_2026-06-29.md``.
        self._preview_node_id: Optional[str] = None
        self._interaction_debounce_timer = QTimer(self)
        self._interaction_debounce_timer.setSingleShot(True)
        self._interaction_debounce_timer.setInterval(16)  # ms - throttle for drag updates
        self._interaction_debounce_timer.timeout.connect(self._flush_interaction_update)
        self._pending_interaction_update: Optional[dict] = None  # Pending node param update

        # ── Thumbnail Throttling State ─────────────────────────────────
        self._thumbnail_policy = CompositeThumbnailPolicy()
        self._thumbnail_state = ThumbnailThrottleState()
        self._thumbnail_display_settings: Optional[dict] = None

        self._init_ui()

    def _init_ui(self) -> None:
        """Build the node editor panel layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Toolbar
        toolbar = QWidget()
        toolbar.setFixedHeight(32)
        toolbar.setStyleSheet(COMPOSITE_TOOLBAR_STYLE)
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(8, 0, 8, 0)

        title = QLabel(I18N.tr("panel.node_graph", "Node Graph"))
        title.setStyleSheet(COMPOSITE_TOOLBAR_TITLE_STYLE)
        toolbar_layout.addWidget(title)

        # B3-1: group-editing breadcrumb ("Root / GroupName") + back button.
        # Hidden unless the editor is inside a group view.
        self._group_breadcrumb = QLabel("")
        self._group_breadcrumb.setStyleSheet(COMPOSITE_TOOLBAR_TITLE_STYLE)
        self._group_breadcrumb.hide()
        toolbar_layout.addWidget(self._group_breadcrumb)
        self._group_back_btn = QPushButton(I18N.tr("composite.page_back", "⬅ Back"))
        self._group_back_btn.setStyleSheet(COMPOSITE_ADD_NODE_BTN_STYLE)
        self._group_back_btn.setFixedHeight(22)
        self._group_back_btn.setToolTip(
            I18N.tr("composite.exit_group_tooltip", "Exit the current group edit view (also Esc)"))
        self._group_back_btn.clicked.connect(self._exit_group_view)
        self._group_back_btn.hide()
        toolbar_layout.addWidget(self._group_back_btn)

        toolbar_layout.addStretch()

        btn_style = COMPOSITE_ADD_NODE_BTN_STYLE

        # Add node buttons (only the most commonly used - others available via context menu)
        for label, callback in [
            ("+ Constant", self._add_constant_node),
            ("+ Merge", self._add_merge_node),
            ("+ Blur", self._add_blur_node),
            ("+ Transform", self._add_transform_node),
        ]:
            btn = QPushButton(label)
            btn.setStyleSheet(btn_style)
            btn.clicked.connect(callback)
            toolbar_layout.addWidget(btn)
            self._node_toolbar_controls.append(btn)

        # Settings button (⋯) — node-graph view options menu
        self._settings_btn = QToolButton()
        self._settings_btn.setText("⋯")
        self._settings_btn.setFixedWidth(24)
        self._settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_btn.setStyleSheet(COMPOSITE_SETTINGS_BTN_STYLE)
        self._settings_btn.setToolTip(I18N.tr("composite.settings_tooltip", "Settings"))
        self._settings_btn.clicked.connect(self._show_settings_menu)
        toolbar_layout.addWidget(self._settings_btn)
        self._node_toolbar_controls.append(self._settings_btn)

        # Zoom level indicator
        self._zoom_label = QLabel("100%")
        self._zoom_label.setStyleSheet(ZOOM_LABEL_STYLE)
        self._zoom_label.setFixedWidth(40)
        toolbar_layout.addWidget(self._zoom_label)

        # M08.1c: bake staleness badge — plan audit (preview/export same data source)
        # is visible when non-empty; tooltip lists bake nodes with stale/unsigned status one by one.
        self._bake_badge = QLabel("")
        self._bake_badge.setStyleSheet(
            "QLabel { background-color: rgba(180, 90, 30, 40);"
            " color: #e0a030; border: 1px solid rgba(224, 160, 48, 120);"
            " border-radius: 3px; padding: 0px 6px; font-size: 11px; }")
        self._bake_badge.hide()
        toolbar_layout.addWidget(self._bake_badge)

        layout.addWidget(toolbar)

        # Node editor with minimap overlay
        editor_container = QWidget()
        editor_layout = QVBoxLayout(editor_container)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)

        self._node_editor = NodeEditorWidget(self)
        self._node_editor.node_double_clicked.connect(self._on_node_double_clicked)
        self._node_editor.node_connected.connect(self._on_nodes_connected)
        self._node_editor.node_disconnected.connect(self._on_nodes_disconnected)
        self._node_editor.node_about_to_be_removed.connect(self._on_node_about_to_be_removed)
        self._node_editor.nodes_about_to_be_removed.connect(self._on_nodes_about_to_be_removed)
        self._node_editor.node_removed.connect(self._on_node_removed)
        # "1" shortcut → toggle node-preview of the selected node (Nuke-style).
        self._node_editor.preview_requested.connect(self.toggle_preview_selected_node)
        # "Esc" shortcut → leave node-preview and return the viewer to Output.
        self._node_editor.preview_exit_requested.connect(self._exit_node_preview)
        # B3-1: group edit view enter/exit (undo recorded on the page).
        if hasattr(self._node_editor, "group_enter_requested"):
            self._node_editor.group_enter_requested.connect(
                self._on_group_enter_requested)
            self._node_editor.group_view_changed.connect(
                self._on_group_view_changed)
        # Note: graph_changed is NOT connected to _evaluate_graph here.
        # Each specific signal handler calls _evaluate_graph() directly,
        # avoiding double evaluation (graph_changed is used for minimap only).
        self._node_editor.node_selected.connect(self._on_node_selected)
        self._node_editor.nodes_selected.connect(self._on_nodes_selected)
        self._node_editor.selection_cleared.connect(self._on_selection_cleared)
        self._node_editor.node_added.connect(self._on_node_added)
        self._node_editor.node_bypass_toggled.connect(self._on_node_bypass_toggled)
        # U-07：多选旁路单条 group undo
        self._node_editor.nodes_bypass_batch_toggled.connect(
            self._on_nodes_bypass_batch_toggled)
        self._node_editor.node_lock_changed.connect(self._on_node_lock_changed)
        # B4: canvas rename → model change + undoable
        self._node_editor.node_rename_requested.connect(self._on_node_rename_requested)
        # C5: version binning
        self._node_editor.node_version_step_requested.connect(
            self._on_node_version_step_requested)
        self._node_editor.set_all_reads_latest_requested.connect(
            self._on_set_all_reads_latest)
        self._node_editor.nodes_moved.connect(self._on_nodes_moved)
        self._node_editor.undo_requested.connect(self.undo)
        self._node_editor.redo_requested.connect(self.redo)
        self._node_editor.project_settings_requested.connect(self._on_project_settings_requested)
        self._node_editor.graph_changed.connect(self._update_minimap)
        # Backdrop annotation lifecycle → undo
        self._node_editor.backdrop_created.connect(self._on_backdrop_created)
        self._node_editor.backdrop_deleted.connect(self._on_backdrop_deleted)
        self._node_editor.backdrops_deleted.connect(self._on_backdrops_deleted)
        self._node_editor.backdrop_modified.connect(self._on_backdrop_modified)
        # Sticky note annotation lifecycle → undo
        self._node_editor.sticky_note_created.connect(self._on_sticky_note_created)
        self._node_editor.sticky_note_deleted.connect(self._on_sticky_note_deleted)
        self._node_editor.sticky_notes_deleted.connect(self._on_sticky_notes_deleted)
        self._node_editor.sticky_note_modified.connect(self._on_sticky_note_modified)
        # Group membership lifecycle → undo (AUD-9920)
        self._node_editor.group_commands_applied.connect(
            self._on_group_commands_applied)
        self._node_editor.nodes_moved.connect(lambda *_: self._update_minimap())
        self._node_editor.viewport_moved.connect(self._update_minimap_viewport)
        self._node_editor.zoom_changed.connect(self._on_zoom_changed)
        self._node_editor.nodes_pasted.connect(self._on_nodes_pasted)
        # U-16：粘贴/预设装配开始 → 静默事务（内部连线不单独入历史）
        self._node_editor.nodes_pasting.connect(self._on_nodes_pasting)
        self._node_editor.nodes_duplicated.connect(self._on_nodes_duplicated)
        self._node_editor.nodes_batch_removed.connect(self._on_nodes_batch_removed)
        self._node_editor.placeholder_node_added.connect(self._on_placeholder_node_added)
        # AUD-10503 页面侧接线：媒体池素材拖到节点画布 → asset-backed 节点。
        # 编辑器只识别媒体池 MIME 并发信号；asset 解析与建节点归页面
        # （见 _on_editor_assets_dropped）。
        self._node_editor.assets_dropped.connect(self._on_editor_assets_dropped)
        editor_layout.addWidget(self._node_editor)

        # Minimap overlay (bottom-right corner)
        self._minimap = MinimapWidget(self._node_editor, self._node_editor)
        self._minimap.navigate_requested.connect(
            lambda pos: self._node_editor.centerOn(pos)
        )
        # Track selected node for minimap highlight
        self._node_editor.node_selected.connect(self._minimap.set_selected_node)
        self._node_editor.selection_cleared.connect(
            lambda: self._minimap.set_selected_node(None)
        )
        # Initial position (will be corrected on first resize)
        self._minimap.move(
            self._node_editor.width() - self._minimap.width() - 8,
            self._node_editor.height() - self._minimap.height() - 8,
        )

        editor_container.setLayout(editor_layout)
        layout.addWidget(editor_container)
        self._node_toolbar = toolbar

        # Build default graph
        self._build_default_graph()

        # Initialize thumbnail manager
        self._thumbnail_manager = NodeThumbnailManager(self._node_graph)

        self._build_empty_state_overlay()
        self.set_composition_active(False)

    def _build_empty_state_overlay(self) -> None:
        """Build the read-only overlay shown before a composition is opened.

        This widget is intentionally parented to CompositePage rather than to
        the main window.  It therefore blocks only the node workspace while
        the shared Media Pool dock remains available for creating/opening a
        Comp Clip.
        """
        overlay = QWidget(self)
        overlay.setObjectName("compositeEmptyStateOverlay")
        overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        overlay.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        overlay.setStyleSheet(
            f"QWidget#compositeEmptyStateOverlay {{"
            " background-color: rgba(10, 11, 14, 178);"
            "}"
            f"QFrame#compositeEmptyStateCard {{"
            f" background-color: {C.BG_PANEL};"
            f" border: 1px solid {C.BORDER};"
            " border-radius: 5px;"
            "}"
            f"QLabel#compositeEmptyStateTitle {{"
            f" color: {C.TEXT_PRIMARY};"
            " font-size: 13px;"
            " font-weight: 600;"
            "}"
            f"QLabel#compositeEmptyStateHint {{"
            f" color: {C.TEXT_HINT};"
            " font-size: 11px;"
            "}"
            f"QPushButton#compositeEmptyStateCreateButton {{"
            f" color: #ffffff;"
            f" background-color: {C.ORANGE};"
            f" border: 1px solid {C.ORANGE};"
            " border-radius: 3px;"
            " padding: 5px 14px;"
            " min-width: 132px;"
            " font-size: 11px;"
            "}"
            f"QPushButton#compositeEmptyStateCreateButton:hover {{"
            f" background-color: {C.ORANGE_HOVER};"
            "}"
            f"QPushButton#compositeEmptyStateCreateButton:pressed {{"
            f" background-color: {C.ORANGE_PRESS};"
            "}"
        )

        overlay_layout = QVBoxLayout(overlay)
        overlay_layout.setContentsMargins(20, 20, 20, 20)
        overlay_layout.addStretch(1)

        card = QFrame(overlay)
        card.setObjectName("compositeEmptyStateCard")
        card.setMaximumWidth(440)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(10)

        title = QLabel(
            I18N.tr("composite.empty_state.title", "No Comp Clip Open"),
            card,
        )
        title.setObjectName("compositeEmptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)

        hint = QLabel(
            I18N.tr(
                "composite.empty_state.hint",
                "Create or open a Comp Clip to begin editing.",
            ),
            card,
        )
        hint.setObjectName("compositeEmptyStateHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(hint)

        create_button = QPushButton(
            I18N.tr("composite.empty_state.create", "New Comp Clip"),
            card,
        )
        create_button.setObjectName("compositeEmptyStateCreateButton")
        create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        create_button.clicked.connect(self.create_empty_comp_clip_requested.emit)
        card_layout.addWidget(create_button, alignment=Qt.AlignmentFlag.AlignCenter)

        overlay_layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        overlay_layout.addStretch(1)

        self._empty_state_overlay = overlay
        self._empty_state_create_button = create_button
        overlay.setGeometry(self.rect())
        overlay.raise_()

    def set_composition_active(self, active: bool) -> None:
        """Enable or disable the node workspace for the current composition.

        The state is based on the active composition identity, not on the
        current Media Pool selection.  Clearing a Media Pool selection after
        opening a Comp Clip therefore does not unexpectedly lock the graph.
        """
        if not active:
            # A page reset can happen while either the independent comp timer
            # or an injected timeline transport is playing. Stop that source
            # before disabling the panels so no stale graph keeps advancing.
            self.comp_pause()
        self._composition_active = bool(active)
        self._sync_fit_composition_shortcut()

        if self._node_toolbar is not None:
            self._node_toolbar.setEnabled(self._composition_active)
        for control in self._node_toolbar_controls:
            control.setEnabled(self._composition_active)
        if hasattr(self, "_node_editor"):
            self._node_editor.setEnabled(self._composition_active)
            self._node_editor.set_shortcuts_enabled(
                self._composition_active and self._is_active
            )

        keyframes_panel = getattr(self, "_keyframes_panel", None)
        if keyframes_panel is not None and hasattr(
                keyframes_panel, "set_composition_active"):
                keyframes_panel.set_composition_active(self._composition_active)

        if not self._composition_active:
            self._clear_model3d_auxiliary_panels()
        else:
            self._sync_import_report_for_selected_node()

        if self._empty_state_overlay is not None:
            self._empty_state_overlay.setVisible(not self._composition_active)
            if not self._composition_active:
                self._empty_state_overlay.raise_()

    def has_active_composition(self) -> bool:
        """Return whether the node workspace currently has an open composition."""
        return self._composition_active

    def _sync_fit_composition_shortcut(self) -> None:
        """Enable Composite's Shift+Z only for its active composition page."""
        shortcut = getattr(self, "_fit_composition_shortcut", None)
        if shortcut is not None:
            shortcut.setEnabled(bool(self._is_active and
                                     self._composition_active))

    def _fit_keyframes_to_composition(self) -> None:
        """Fit the Keyframes timeline to the active composition extent."""
        if not self._is_active or not self._composition_active:
            return
        # WindowShortcut fires even while a parented Tool window (the Motion
        # Graphic editor) is active on macOS — bail so Shift+Z stays owned by
        # whoever holds activation.  See src/shared/ui/shortcut_scope.py.
        if not shortcut_owner_window_active(self):
            return
        panel = getattr(self, "_keyframes_panel", None)
        fit = getattr(panel, "fit_time_range", None)
        if callable(fit):
            fit()

    def _build_default_graph(self) -> None:
        """Create the default TimelineClipInput → Output graph."""
        self._node_graph.create_default_graph()
        self._node_editor.build_from_graph(self._node_graph)

    def reset_to_empty_workspace(self) -> None:
        """Reset the page to an unbound, non-persistent scratch workspace.

        This is used when the user opens Composite directly without selecting
        an existing Comp Clip.  Keep a standalone Output node so the graph
        editor remains usable, but clear all composition/runtime identity so
        edits cannot be written into a previously opened Comp Clip.
        """
        self.comp_pause()
        self.set_keyframes_transport(None)
        self._current_composite_id = ""
        self._last_published_graph_data = None
        self._last_published_graph_generation = -1  # AUD-9508: force republish
        self._user_edit_fired = False
        self._last_texture_id = None
        self._last_texture_width = 0
        self._last_texture_height = 0
        self._last_metadata = {}
        self._last_model_animation_clips = []
        self._last_model_import_report = None
        self._last_model_import_status = ""
        self._last_model_import_phase = ""
        self._last_model_import_progress = 0.0
        self._last_model_import_diagnostics = ()
        self._last_model_import_resource_estimate = None
        self._last_model_import_probe = None
        self._last_model_runtime_diagnostics = ()
        self._last_model_source_path = ""
        self._last_model_node_id = ""
        self._last_model_hierarchy = None
        self._last_model_hidden_mesh_ids = ()
        self._last_model_locked_mesh_ids = ()
        self._last_model_solo_mesh_id = ""
        self._last_color_texture_id = 0
        self._last_color_width = 0
        self._last_color_height = 0
        self._last_color_revision = 0
        self._runtime_context = None
        self._timeline_clip_gpu_provider = None
        self._comp_clip_timeline_start = 0
        self._composite_current_frame = 0
        self._composite_duration_frames = 0
        self._composite_fps = 24.0
        self._is_comp_clip = False
        self._comp_in_frame = None
        self._comp_out_frame = None
        self._preview_node_id = None
        self._last_selected_node_id = None
        self._last_selected_layer_id = None
        self._synced_output_image_size = None
        self._output_resolution_user_locked = False
        self._source_image_size = None
        self.set_composition_active(False)

        self._node_graph.clear()
        self._node_graph.fps = self._composite_fps
        self._node_graph.duration_frames = 0
        self._node_editor.clear_all()

        # An Output-only graph represents an empty composition without
        # inventing a source clip or a persistent composition record.
        output_node = OutputNode()
        output_node.position = (300, 200)
        output_node.set_parameter("format_mode", "custom")
        self._node_graph.add_node(output_node)
        self._node_editor.build_from_graph(self._node_graph)
        if self._viewer is not None:
            self._viewer.set_texture(0, 0, 0)
        self._rebind_properties_panel_to_live_node()
        self._refresh_keyframes_panel()
        self._node_graph._force_full_eval = True
        self._color_dirty = True
        self._evaluate_graph()

    def requires_color_output_input(self) -> bool:
        """Return True only for legacy graphs that still contain COLOR input."""
        return any(
            getattr(node, "node_type", "") == "color_input"
            for node in self._node_graph.nodes.values()
        )

    def set_edit_project(self, project: Any = None) -> None:
        """Set the project used by ToposColor nodes to resolve ColorStacks."""
        self._project = project

    def _handle_keyframes_media_drop(self, mime_data: object,
                                     _target_layer_id: Optional[str],
                                     insert_index: int) -> bool:
        """Resolve a MediaPool drag and add each asset as one layer.

        The tree only supplies the insertion position; this host owns asset
        resolution and graph mutation so the widget remains feature-boundary
        agnostic.  A failed payload leaves the graph untouched.
        """
        if insert_index < 0:
            return False
        payloads = resolve_drop_payloads(mime_data, project=self._project)
        if not payloads:
            return False
        grouped = self._undo_stack is not None
        if grouped:
            self._undo_stack.begin_group()
        added = 0
        try:
            for offset, payload in enumerate(payloads):
                if getattr(payload, "media_type", "") == MEDIA_TYPE_MODEL3D \
                        or getattr(payload, "node_type", "") == "model3d":
                    self._layer_stack_controller.add_model3d_payload(
                        payload,
                        insert_index=insert_index + offset,
                        description="Add 3D model layer",
                    )
                else:
                    self._layer_stack_controller.add_media_payload(
                        payload,
                        insert_index=insert_index + offset,
                        description="Add media layer",
                    )
                added += 1
            if grouped:
                self._undo_stack.end_group("Add media layers")
        except Exception as exc:
            if grouped:
                self._undo_stack.end_group("Add media layers")
                if added:
                    self._undo_stack.undo()
            logger.warning("Composite media drop failed: %s", exc)
            return False
        self._node_editor.build_from_graph(self._node_graph)
        self._refresh_keyframes_panel()
        self._evaluate_graph()
        return True

    def _on_editor_assets_dropped(self, asset_ids: List[str],
                                  scene_pos: Tuple[float, float]) -> None:
        """AUD-10503 页面侧消费者：媒体池素材拖放到节点画布。

        编辑器识别媒体池 MIME 后发射 ``assets_dropped``（asset id 列表 +
        场景坐标）；本方法解析 asset 并创建节点，与 Keyframes 图层树 drop
        保持同一职责边界（widget 不做 asset 解析）。每个素材按 media_type
        建 ``image_input`` / ``video_frame`` 节点或 3D 分裂图
        （Scene3D+Model3D，与 URL 文件拖放一致），水平排开 220px，整次
        拖拽成组为单步 undo。
        """
        actions = self._node_editor.editor_actions()
        if actions is None or not asset_ids:
            return
        payloads = resolve_asset_ids(list(asset_ids), project=self._project)
        if not payloads:
            return
        grouped = self._undo_stack is not None
        if grouped:
            self._undo_stack.begin_group()
        added = 0
        try:
            x = float(scene_pos[0])
            y = float(scene_pos[1])
            for payload in payloads:
                if payload.media_type == MEDIA_TYPE_MODEL3D:
                    scene = actions.create_3d_scene_with_children(
                        x, y, model_path=payload.path or None)
                    if scene is not None:
                        added += 1
                else:
                    node = actions.add_node(
                        payload.node_type, x, y,
                        parameters={"file_path": payload.path})
                    if node is not None:
                        added += 1
                        if (payload.media_type == MEDIA_TYPE_VIDEO
                                and payload.path):
                            # AUD-10502：元数据探测在后台线程，Keyframes
                            # 随后可显示完整素材长度（与 URL 拖放一致）。
                            probe = getattr(
                                self._node_editor,
                                "_schedule_video_metadata_probe", None)
                            if callable(probe):
                                probe(payload.path)
                x += 220.0
            if grouped:
                self._undo_stack.end_group("Add media nodes")
        except Exception as exc:
            if grouped:
                self._undo_stack.end_group("Add media nodes")
                if added:
                    self._undo_stack.undo()
            logger.warning("Composite node-canvas media drop failed: %s", exc)

    def _sync_layer_stack_after_node_edit(self) -> None:
        """Refresh layer awareness after a NodeGraph-owned edit.

        Node edits remain authoritative in Nodes mode. The controller performs
        a dry-run conflict/topology check, while the Keyframes Layers view is
        refreshed so missing roots and stale managed wiring are visible
        immediately instead of only after switching panels.
        """
        controller = getattr(self, "_layer_stack_controller", None)
        if controller is None:
            return
        try:
            controller.sync_from_node_graph(repair=False)
        except Exception:
            logger.exception("LayerStack sync after node edit failed")
        self._refresh_keyframes_panel()

    def switch_to_color_output_graph(self) -> bool:
        """Replace the current graph with COLOR(ColorOutput) → Output."""
        self.set_composition_active(True)
        self._node_graph.clear()
        self._node_editor.clear_all()
        # New graph → reset resolution auto-follow so the Output node picks up
        # the new source image size on the next color-output update.
        self._synced_output_image_size = None
        self._output_resolution_user_locked = False
        self._source_image_size = None

        color_node = COLORNode()
        color_node.position = (100, 200)
        output_node = OutputNode()
        output_node.position = (400, 200)

        self._node_graph.add_node(color_node)
        self._node_graph.add_node(output_node)
        self._node_graph.connect(color_node.id, "result", output_node.id, "image")
        self._node_editor.build_from_graph(self._node_graph)

        self._runtime_context = None
        self._timeline_clip_gpu_provider = None
        self._node_graph._force_full_eval = True
        self._color_dirty = True
        self._evaluate_graph()
        self._rebind_properties_panel_to_live_node()
        return True

    def ensure_topos_color_node(self, color_stack_id: str = "") -> bool:
        """Ensure the current timeline graph routes through Topos Color.

        ALWAYS ensures a Topos Color node exists in the graph:
        - If one already exists, updates its color_stack_id if needed.
        - If none exists, inserts it between the TimelineClipInput and
          whatever node its output currently feeds into.
        - When no color_stack_id is provided, sets bypass=True (pass-through).

        Returns True if any change was made.
        """
        # Only the primary clip-grade node is managed by timeline/Color
        # synchronization. A manually added Topos Color is an independent
        # final-grade node and must retain its own source/stack settings.
        primary_node = next(
            (
                node
                for node in self._node_graph.nodes.values()
                if isinstance(node, ToposColorNode)
                and node.parameters.get("role") == "primary_clip_grade"
            ),
            None,
        )
        if primary_node is None:
            # Backward-compatible migration for old graphs that predate role.
            primary_node = next(
                (
                    node
                    for node in self._node_graph.nodes.values()
                    if isinstance(node, ToposColorNode)
                    and node.parameters.get("stack_source", "clip_stack") == "clip_stack"
                    and not node.parameters.get("role")
                ),
                None,
            )
            if primary_node is not None:
                primary_node.set_parameter("role", "primary_clip_grade")
                primary_node.set_parameter("linked", True)

        if primary_node is not None:
            current_stack_id = primary_node.parameters.get("color_stack_id", "")
            needs_update = False
            if current_stack_id != (color_stack_id or ""):
                primary_node.set_parameter("stack_source", "clip_stack")
                primary_node.set_parameter("color_stack_id", color_stack_id or "")
                needs_update = True
            expected_bypass = not bool(color_stack_id)
            if primary_node.parameters.get("bypass", False) != expected_bypass:
                primary_node.set_parameter("bypass", expected_bypass)
                needs_update = True
            if needs_update:
                self._evaluate_graph()
                return True
            return False

        # No ToposColorNode found -- insert one.
        # Find the source (TimelineClipInput) node
        source = next(
            (node for node in self._node_graph.nodes.values()
             if getattr(node, "node_type", "") == "timeline_clip_input"),
            None,
        )
        output = next(
            (node for node in self._node_graph.nodes.values()
             if isinstance(node, OutputNode)),
            None,
        )
        if source is None or output is None:
            return False

        source.set_parameter("input_stage", "source_raw")

        # Find the direct connection from source -> output (to break it)
        # or the first connection from source's output port (to insert between)
        source_output_conns = [
            conn for conn in list(self._node_graph.connections)
            if conn.src_node_id == source.id and conn.src_port_name == "output"
        ]

        topos = ToposColorNode()
        topos.position = (
            (source.position[0] + output.position[0]) / 2,
            (source.position[1] + output.position[1]) / 2,
        )
        topos.set_parameter("stack_source", "clip_stack")
        topos.set_parameter("color_stack_id", color_stack_id or "")
        topos.set_parameter("role", "primary_clip_grade")
        runtime = self._runtime_context
        topos.set_parameter("source_clip_id", getattr(runtime, "clip_id", "") if runtime else "")
        topos.set_parameter("source_timeline_id", getattr(runtime, "timeline_id", "") if runtime else "")
        topos.set_parameter("linked", True)
        topos.set_parameter("bypass", not bool(color_stack_id))
        topos.set_parameter("intensity", 1.0)
        topos.set_state_callback(self._on_node_state_changed)
        self._node_graph.add_node(topos)

        # Disconnect source -> downstream and reconnect source -> topos -> downstream
        for old_conn in source_output_conns:
            self._node_graph.disconnect(
                old_conn.src_node_id,
                old_conn.src_port_name,
                old_conn.dst_node_id,
                old_conn.dst_port_name,
            )
            self._node_graph.connect(topos.id, "result", old_conn.dst_node_id, old_conn.dst_port_name)

        self._node_graph.connect(source.id, "output", topos.id, "image")
        self._node_editor.build_from_graph(self._node_graph)
        self._evaluate_graph()
        return True

    # ------------------------------------------------------------------
    # Standalone-image graph support
    # ------------------------------------------------------------------

    def switch_to_image_input_graph(self, image_path: str, color_stack_id: str = "") -> bool:
        """Replace the current graph with ImageInput → ToposColor → Output.

        Called when the user enters Composite directly from the Color page
        with a standalone image (no Edit timeline clip context).

        If the graph already contains an ImageInputNode pointing to the same
        file, only the color_stack_id is updated (no rebuild).

        Args:
            image_path: Absolute path to the image file.
            color_stack_id: Color grading stack ID for the Topos Color node.

        Returns True if the graph was changed or rebuilt.
        """
        if not image_path:
            return False

        self.set_composition_active(True)

        # Fast path: graph already has an ImageInputNode for the same file
        existing_img = next(
            (n for n in self._node_graph.nodes.values()
             if isinstance(n, ImageInputNode)
             and n.parameters.get("file_path") == image_path),
            None,
        )
        if existing_img is not None:
            changed = False
            # Update ToposColor color_stack_id if needed
            for node in self._node_graph.nodes.values():
                if (
                    isinstance(node, ToposColorNode)
                    and node.parameters.get("role", "primary_clip_grade")
                    == "primary_clip_grade"
                ):
                    cur_sid = node.parameters.get("color_stack_id", "")
                    if cur_sid != (color_stack_id or ""):
                        node.set_parameter("stack_source", "clip_stack")
                        node.set_parameter("color_stack_id", color_stack_id or "")
                        node.set_parameter("bypass", not bool(color_stack_id))
                        changed = True
                    break
            if changed:
                self._node_graph._force_full_eval = True
                self._node_graph._generation += 1
                self._evaluate_graph()
            return changed

        self._node_graph.clear()
        self._node_editor.clear_all()
        self._node_graph.create_image_default_graph(image_path, color_stack_id)
        self._node_editor.build_from_graph(self._node_graph)

        # Set missing_policy to "checker" for visual feedback during async load
        for node in self._node_graph.nodes.values():
            if isinstance(node, ImageInputNode):
                node.set_parameter("missing_policy", "checker")
                break

        # Reset runtime context (no timeline backing)
        self._runtime_context = None
        self._timeline_clip_gpu_provider = None

        # Mark graph as needing full re-evaluation
        self._node_graph._force_full_eval = True
        self._color_dirty = True

        self._evaluate_graph()
        logger.info(
            f"[CompositePage] Switched to image input graph: {image_path}, "
            f"stack={color_stack_id[:8] if color_stack_id else 'none'}"
        )
        return True

    def has_image_input_for_path(self, image_path: str) -> bool:
        """Check if the graph already has an ImageInputNode for the given path."""
        return any(
            isinstance(n, ImageInputNode) and n.parameters.get("file_path") == image_path
            for n in self._node_graph.nodes.values()
        )

    def switch_to_timeline_input_graph(self, color_stack_id: str = "") -> bool:
        """Replace the current graph with TimelineClipInput → ToposColor → Output."""
        self._node_graph.clear()
        self._node_editor.clear_all()
        self._node_graph.create_default_graph()
        self._node_editor.build_from_graph(self._node_graph)
        self.ensure_topos_color_node(color_stack_id)

        self._node_graph._force_full_eval = True
        self._color_dirty = True
        self._evaluate_graph()
        self._rebind_properties_panel_to_live_node()
        return True

    def has_timeline_clip_input(self) -> bool:
        """Check if the current graph has a TimelineClipInput source."""
        return any(
            getattr(n, "node_type", "") == "timeline_clip_input"
            for n in self._node_graph.nodes.values()
        )

    def _resolve_main_gl_context(self):
        """向上遍历父链，定位 canvas.gl_viewport 并取其 GL context。

        成功时缓存 gl_viewport 引用（供 gl_initialized 信号槽使用）。
        返回 (context, gl_viewport)；失败返回 (None, gl_viewport_or_None)。
        """
        parent = self.parent()
        while parent:
            if hasattr(parent, 'canvas') and hasattr(parent.canvas, 'gl_viewport'):
                gl_viewport = parent.canvas.gl_viewport
                self._gl_viewport_ref = gl_viewport
                if gl_viewport.isValid():
                    ctx = gl_viewport.context()
                    if ctx is not None:
                        return ctx, gl_viewport
                # viewport 存在但尚未 initializeGL：返回 viewport 引用让调用方
                # 去连 gl_initialized 信号，而不是 break 后空转。
                return None, gl_viewport
            parent = parent.parent()
        return None, None

    def initialize_renderer(self) -> None:
        """Start the CompositeRenderer thread.

        GL context 通常在 canvas.gl_viewport 完成 initializeGL() 后才可用。
        本方法会尝试解析 context；若尚未 ready，则降级日志（避免刷屏）并
        由调用方决定是否连 gl_initialized 信号或重试。
        """
        if self._renderer is not None:
            return

        if self._main_gl_context is None:
            ctx, _vp = self._resolve_main_gl_context()
            self._main_gl_context = ctx

        if self._main_gl_context is None:
            # 仅首次 warning 一次，后续降级为 debug —— 这是预期内的“等待 GL
            # 初始化”状态，重复 warning 会刷屏且无信息增量。
            self._gl_retry_count += 1
            if not self._gl_init_warned:
                self._gl_init_warned = True
                logger.warning("No GL context available yet; waiting for gl_initialized")
            else:
                logger.debug("No GL context available yet (retry #%d)", self._gl_retry_count)
            return

        self._renderer = CompositeRenderer(
            self._main_gl_context,
            ai_cache_manager=self._ai_cache_manager,
            ai_compute_manager=self._ai_compute_manager,
        )
        self._renderer.frame_ready.connect(self._on_frame_ready)
        self._renderer.metadata_ready.connect(self._on_metadata_ready)
        self._renderer.error_occurred.connect(self._on_error)
        self._renderer.node_thumbnail_ready.connect(self._on_node_thumbnail_ready)
        self._renderer.preview_image_ready.connect(self._on_preview_image_ready)
        self._renderer.gpu_scope_ready.connect(self._on_gpu_scope_ready)
        # M08.1c：烘焙陈旧徽标（渲染线程审计 → 主线程 UI，跨线程自动排队）
        self._renderer.bake_staleness_changed.connect(
            self._on_bake_staleness_changed)
        self._renderer.start()

        # The AI compute manager was created on the MAIN thread (this
        # object's thread, which runs a Qt event loop) — its worker slots
        # are queued connections and would never be delivered on the
        # render thread's manual queue loop. Signals can be connected
        # immediately; no initialization race to wait out.
        self._connect_ai_signals()

    def _ensure_renderer_initialized(self) -> None:
        """统一的 renderer 初始化入口（事件驱动 + 兜底重试）。

        策略：
        1. 先直接尝试 initialize_renderer（GL context 多数情况下已 ready）。
        2. 仍未 ready：连 canvas.gl_viewport 的 gl_initialized 信号，GL 就绪
           后由 _on_gl_initialized 触发初始化（不刷屏的主路径）。
        3. 信号也连不上（找不到 viewport 等）：退化为定时器重试，但封顶
           次数避免无限刷屏。
        """
        if self._renderer is not None:
            return
        # 路径 1：直接尝试（多数场景一次成功）。
        self.initialize_renderer()
        if self._renderer is not None:
            return
        # 路径 2：连 gl_initialized 信号 —— GL viewport 尚未 initializeGL 时
        # 这是最可靠的就绪同步点。
        if self._gl_viewport_ref is None:
            _, vp = self._resolve_main_gl_context()
        else:
            vp = self._gl_viewport_ref
        if self._connect_gl_initialized_signal(vp):
            return
        # 路径 3：兜底重试（封顶，避免刷屏）。
        if self._gl_retry_count < 5:
            self._renderer_retry_timer.start()

    def _connect_gl_initialized_signal(self, gl_viewport) -> bool:
        """连 gl_viewport 的 gl_initialized 信号，GL ready 时触发 renderer 初始化。

        这是 GL context 就绪的正确同步点：canvas 的 GL viewport 在
        initializeGL() 完成后发射 gl_initialized，比定时器盲轮询更可靠、
        不刷屏。若 viewport 已 ready（_gl_initialized=True），立即触发一次。

        返回是否成功连接（viewport 无效或已连过则返回 False）。
        """
        if gl_viewport is None or self._gl_signal_connected:
            return False
        try:
            # 若 GL 已初始化完成，直接立即触发，避免漏掉。
            if getattr(gl_viewport, '_gl_initialized', False):
                self._gl_signal_connected = True
                self._on_gl_initialized()
                return True
            gl_viewport.gl_initialized.connect(self._on_gl_initialized)
            self._gl_signal_connected = True
            logger.debug("Waiting for canvas gl_initialized signal")
            return True
        except (AttributeError, RuntimeError) as e:
            logger.debug("gl_initialized signal unavailable: %s", e)
            return False

    def _on_gl_initialized(self) -> None:
        """canvas GL viewport 初始化完成回调：启动 renderer 并提交首帧。

        幂等：已启动则跳过。停止盲轮询定时器。
        """
        # 解析并缓存 context（此时 initializeGL 已完成，context 必然可用）。
        if self._main_gl_context is None:
            ctx, _vp = self._resolve_main_gl_context()
            self._main_gl_context = ctx
        if self._main_gl_context is None:
            return
        # 停掉盲轮询定时器 —— 信号已就绪，不再需要重试。
        self._renderer_retry_timer.stop()
        if self._renderer is not None:
            return
        self.initialize_renderer()
        # 首次就绪：若页面处于活动状态，补提交一次渲染请求。
        if self._renderer is not None and self._is_active:
            self._retry_renderer_init()

    def on_enter(self, color_texture_id: Optional[int] = None,
                 color_width: int = 0, color_height: int = 0,
                 color_revision: int = 0) -> None:
        """Called when switching TO the Composite page.

        Args:
            color_texture_id: ColorOutput texture ID (or None if not yet available)
            color_width: ColorOutput texture width
            color_height: ColorOutput texture height
            color_revision: ColorOutput revision counter
        """
        self._is_active = True
        self._sync_fit_composition_shortcut()

        # Center nodes in viewport if no saved viewport state exists
        if self._node_graph and not self._node_graph._viewport_state:
            if self._node_editor and self._node_editor._node_items:
                QTimer.singleShot(0, self._node_editor.center_on_nodes)

        # Timeline-backed Composite pages must wait for the current context's
        # render result. Replaying _last_texture_id here can show a stale
        # placeholder/previous clip on the first page switch, then appear fixed
        # on the second switch once the new render has populated the cache.
        if self._runtime_context is not None:
            self._last_texture_id = None
            self._last_texture_width = 0
            self._last_texture_height = 0
            self._last_metadata = {}

        # Enable node editor shortcuts
        self._node_editor.set_shortcuts_enabled(self._composition_active)

        self._ensure_renderer_initialized()

        # Cache ColorOutput state
        if color_texture_id is not None:
            self._last_color_texture_id = color_texture_id
            self._last_color_width = color_width
            self._last_color_height = color_height
            self._last_color_revision = color_revision
            # Sync the Output node resolution before Project Settings is shown
            # so the panel opens with the correct image dimensions.
            self._sync_output_resolution_to_image(color_width, color_height)

        # Force full re-evaluation if Color was dirty while we were away
        if self._color_dirty:
            self._color_dirty = False
            self._node_graph._force_full_eval = True

        needs_color_output = self.requires_color_output_input()

        # New timeline graphs use TimelineClipInput/ToposColor and do not need
        # the Color page's legacy ColorOutput texture to render.
        if self._renderer and (not needs_color_output or (color_texture_id is not None and color_texture_id > 0)):
            import time as _time
            rid = self._request_id_gen.next_id()
            self._last_submitted_request_id = rid
            _snap = self._node_graph.snapshot(
                color_texture_id=color_texture_id or 0,
                color_width=color_width,
                color_height=color_height,
                color_revision=color_revision,
                frame_number=self._current_keyframe_frame(),
                fps=self.comp_clip_fps(),
            )
            _snap = self._inject_viewer3d_scene_snapshot(_snap)
            _vmo = self._apply_view_mode_overrides(_snap)
            resolve_viewer_node_id = getattr(self, "_resolve_viewer_node_id", None)
            viewer_node_id = (
                resolve_viewer_node_id()
                if callable(resolve_viewer_node_id)
                else self._resolve_preview_node_id()
            )
            request = RenderRequest(
                snapshot=_snap,
                request_id=rid,
                timestamp=_time.monotonic(),
                runtime_context=self._runtime_context,
                timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
                edit_project=self._project,
                viewer_node_id=viewer_node_id,
                view_signature=self._view_signature(),
                quad_view=getattr(self, "_is_quad_view", lambda: False)(),
                single_ortho_view=getattr(
                    self, "_single_ortho_view_code", lambda: None
                )() or None,
                quad_active_view=getattr(
                    self, "_quad_active_view_code", lambda: None
                )() or None,
                # P7：选中 / 隔离节点 id（outline / isolate 渲染用）
                selected_3d_node_id=getattr(
                    self, "_selected_3d_node_id_for_request", lambda: None
                )(),
                isolate_3d_node_id=getattr(
                    self, "_isolate_3d_node_id_for_request", lambda: None
                )(),
                global_3d_scene_settings=getattr(
                    self, "_global_3d_scene_settings", lambda: None
                )(),
                view_mode_overrides=_vmo,
                proxy_scale=getattr(self, "_preview_quality", 1.0),
                output_name=getattr(self, "_output_target_name", "display"),
                output_node_id=getattr(self, "_output_target_node_id", None),
                playback_drive=self._build_playback_drive(_snap),
            )
            self._renderer.submit(CompositeCommand.EVALUATE_GRAPH, {
                'request': request,
            })
        elif self._renderer:
            # Legacy COLOR graph without a valid ColorOutput yet.
            pass
        else:
            # GL context not ready yet — retry after a short delay
            self._renderer_retry_timer.start()

        if self._last_texture_id is not None:
            self.composite_frame_ready.emit(
                0,  # request_id=0 for cached replay
                self._last_texture_id,
                self._last_texture_width,
                self._last_texture_height,
            )

        # Default the properties panel to Project Settings so the user sees the
        # composition's resolution/fps/duration/background at a glance on entry
        # (not pinned — user can close it or pin it to keep it). Deferred one
        # frame so the properties panel and node graph are fully wired up.
        if self._properties_panel is not None and self._node_graph is not None:
            QTimer.singleShot(0, lambda: self._show_default_project_settings())

    def _show_default_project_settings(self) -> None:
        """Show Project Settings on page entry (no pin)."""
        if (self._properties_panel is None or self._node_graph is None
                or not self._is_active):
            return
        self._properties_panel.show_project_settings(
            self._node_graph,
            settings_callback=self._apply_project_settings,
            width=(int(getattr(self._runtime_context, "width", 1920) or 1920)
                   if getattr(self, "_runtime_context", None) is not None
                   else None),
            height=(int(getattr(self._runtime_context, "height", 1080) or 1080)
                    if getattr(self, "_runtime_context", None) is not None
                    else None),
            fps=(float(getattr(self._runtime_context, "fps", 24.0) or 24.0)
                 if getattr(self, "_runtime_context", None) is not None
                 else float(getattr(self, "_composite_fps", 24.0) or 24.0)),
            duration_frames=(int(getattr(self._runtime_context, "duration_frames", 0) or 0)
                             if getattr(self, "_runtime_context", None) is not None
                             else int(getattr(self, "_composite_duration_frames", 0) or 0)),
            timeline_bound=getattr(self, "_runtime_context", None) is not None,
        )

    def _load_project_settings_metadata(self, runtime_context=None) -> None:
        """Refresh the visible Project Settings from composition metadata."""
        panel = getattr(self, "_properties_panel", None)
        section = getattr(panel, "_project_settings_section", None)
        if section is None or not hasattr(section, "load_from_graph"):
            return
        runtime = (runtime_context if runtime_context is not None
                   else getattr(self, "_runtime_context", None))
        if runtime is not None:
            section.load_from_graph(
                self._node_graph,
                width=int(getattr(runtime, "width", 1920) or 1920),
                height=int(getattr(runtime, "height", 1080) or 1080),
                fps=float(getattr(runtime, "fps", 24.0) or 24.0),
                duration_frames=int(
                    getattr(runtime, "duration_frames", 0) or 0
                ),
                timeline_bound=True,
            )
        else:
            section.load_from_graph(
                self._node_graph,
                fps=float(getattr(self, "_composite_fps", 24.0) or 24.0),
                duration_frames=int(
                    getattr(self, "_composite_duration_frames", 0) or 0
                ),
                timeline_bound=False,
            )

    def _retry_renderer_init(self) -> None:
        """Retry renderer initialization if it failed on first on_enter.

        Called by _renderer_retry_timer when the GL context may now be available.
        """
        if self._renderer is not None or not self._is_active:
            return

        self.initialize_renderer()
        if self._renderer:
            import time as _time
            rid = self._request_id_gen.next_id()
            self._last_submitted_request_id = rid
            _snap = self._node_graph.snapshot(
                color_texture_id=self._last_color_texture_id or 0,
                color_width=self._last_color_width,
                color_height=self._last_color_height,
                color_revision=self._last_color_revision,
                frame_number=self._current_keyframe_frame(),
                fps=self.comp_clip_fps(),
            )
            _snap = self._inject_viewer3d_scene_snapshot(_snap)
            _vmo = self._apply_view_mode_overrides(_snap)
            # AUD-9500: resolve the viewer node here — the retry path runs in
            # its own scope where no ``viewer_node_id`` local exists (the only
            # definition lives in on_enter), so referencing it raised NameError
            # the moment the deferred init succeeded.
            resolve_viewer_node_id = getattr(self, "_resolve_viewer_node_id", None)
            viewer_node_id = (
                resolve_viewer_node_id()
                if callable(resolve_viewer_node_id)
                else self._resolve_preview_node_id()
            )
            request = RenderRequest(
                snapshot=_snap,
                request_id=rid,
                timestamp=_time.monotonic(),
                runtime_context=self._runtime_context,
                timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
                edit_project=self._project,
                viewer_node_id=viewer_node_id,
                view_signature=self._view_signature(),
                quad_view=self._is_quad_view(),
                single_ortho_view=self._single_ortho_view_code() or None,
                quad_active_view=self._quad_active_view_code() or None,
                # P7：选中 / 隔离节点 id（outline / isolate 渲染用）
                selected_3d_node_id=self._selected_3d_node_id_for_request(),
                isolate_3d_node_id=self._isolate_3d_node_id_for_request(),
                global_3d_scene_settings=self._global_3d_scene_settings(),
                view_mode_overrides=_vmo,
                proxy_scale=self._preview_quality,
                output_name=getattr(self, "_output_target_name", "display"),
                output_node_id=getattr(self, "_output_target_node_id", None),
            )
            self._renderer.submit(CompositeCommand.EVALUATE_GRAPH, {
                'request': request,
            })
        else:
            # Still not ready — 重试封顶，避免无限刷屏。事件驱动路径
            # （gl_initialized 信号）一旦就绪会立即接管并停止此定时器。
            if self._gl_retry_count < 5:
                self._renderer_retry_timer.start()
            else:
                logger.debug(
                    "Renderer init retry capped at %d attempts; "
                    "will resume on gl_initialized signal or next on_enter",
                    self._gl_retry_count)

    def on_leave(self) -> None:
        """Called when switching AWAY from the Composite page."""
        self._is_active = False
        self._sync_fit_composition_shortcut()
        
        # Disable node editor shortcuts
        self._node_editor.set_shortcuts_enabled(False)

    def mark_color_dirty(self) -> None:
        """Mark that the Color page output has changed.

        Called regardless of whether the Composite page is active.
        When entering Composite next time, this triggers a full re-evaluation.
        """
        self._color_dirty = True
        if self._node_graph is not None:
            self._node_graph._force_full_eval = True
            self._node_graph._generation += 1

    def _on_node_state_changed(self, node_id: str, new_state) -> None:
        """Callback when a node's state changes — update visual."""
        self._update_node_item_state_by_id(node_id)

    def _update_node_item_state(self, node) -> None:
        """Update the NodeItem visual to match node state."""
        item = self._node_editor._node_items.get(node.id)
        if item:
            item.set_state(
                node._state.name.lower(),
                progress=node.progress,
                error_msg=node.error_message,
            )

    def _update_node_item_state_by_id(self, node_id: str) -> None:
        """Update the NodeItem visual for a node by ID."""
        node = self._node_graph.get_node(node_id)
        if node:
            self._update_node_item_state(node)

    def apply_preflight_diagnostics(self, report: object) -> None:
        """Show the latest export preflight issues on their graph nodes."""
        if self._node_editor is None:
            return
        self._node_editor.set_node_diagnostics(report)

    def _connect_ai_signals(self) -> None:
        """Connect AI compute manager signals (manager is page-owned)."""
        ai_mgr = getattr(self, "_ai_compute_manager", None)
        if ai_mgr is not None:
            try:
                ai_mgr.compute_finished.disconnect(self._on_ai_compute_finished)
            except (TypeError, RuntimeError):
                pass
            try:
                ai_mgr.compute_failed.disconnect(self._on_ai_compute_failed)
            except (TypeError, RuntimeError):
                pass
            try:
                ai_mgr.compute_progress.disconnect(self._on_ai_compute_progress)
            except (TypeError, RuntimeError):
                pass
            ai_mgr.compute_finished.connect(self._on_ai_compute_finished)
            ai_mgr.compute_failed.connect(self._on_ai_compute_failed)
            ai_mgr.compute_progress.connect(self._on_ai_compute_progress)

    def _ai_node_for(self, cache_key: str, pop: bool = False) -> Optional[str]:
        """Resolve the node that originated a compute request."""
        mgr = getattr(self, "_ai_compute_manager", None)
        node_id = mgr.take_node_id(cache_key) if mgr is not None else None
        if node_id is None:
            node_id = (self._ai_pending.pop(cache_key, None) if pop
                       else self._ai_pending.get(cache_key))
        return node_id

    # ── AI Compute Callbacks ──

    def _on_ai_compute_finished(self, cache_key: str) -> None:
        """Handle AI computation completion — force re-evaluate graph.

        Uses AI_RESULT_READY instead of EVALUATE_GRAPH because generation
        and color_revision may not have changed, causing the normal path
        to skip. AI_RESULT_READY bypasses the generation guard and forces
        a full cache clear + re-evaluation.
        """
        node_id = self._ai_node_for(cache_key, pop=True)
        if node_id:
            node = self._node_graph.get_node(node_id)
            if node:
                node.state = NodeState.CACHED
                node.progress = 1.0
        # Force re-evaluation bypassing the generation/revision guard
        if self._renderer:
            self._renderer.submit(CompositeCommand.AI_RESULT_READY, {})

    def _on_ai_compute_failed(self, cache_key: str, error_msg: str) -> None:
        """Handle AI computation failure."""
        node_id = self._ai_node_for(cache_key, pop=True)
        if node_id:
            node = self._node_graph.get_node(node_id)
            if node:
                node.state = NodeState.FAILED
                node.error_message = error_msg
                node.progress = 0.0

    def _on_ai_compute_progress(self, cache_key: str, pct: int) -> None:
        """Handle AI computation progress update."""
        node_id = self._ai_node_for(cache_key)
        if node_id:
            node = self._node_graph.get_node(node_id)
            if node:
                node.progress = pct / 100.0
                self._update_node_item_state_by_id(node_id)

    def has_connected_output(self) -> bool:
        """Check if the composite graph has an Output node with connected input."""
        return self._node_graph.has_connected_output()

    def update_color_output(self, texture_id: int, width: int, height: int,
                            color_revision: int = 0) -> None:
        """Update the COLOR input from a fresh ColorOutput render.

        Called when the ColorOutput render completes while the Composite
        page is active. Submits a fresh snapshot with the new texture.
        """
        # Cache ColorOutput state
        self._last_color_texture_id = texture_id
        self._last_color_width = width
        self._last_color_height = height
        self._last_color_revision = color_revision

        # Keep the Output node's width/height parameters in sync with the
        # actual image dimensions so the Project Settings panel reflects the
        # real canvas size. Only applies while format_mode == "input" (the
        # default for standalone-image graphs); once the user picks a custom
        # resolution in Project Settings, format_mode becomes "custom" and we
        # stop overriding their choice.
        self._sync_output_resolution_to_image(width, height)

        if self._renderer and texture_id is not None and texture_id > 0:
            if not self.requires_color_output_input():
                # ToposColor graphs read ColorStack data directly; ColorOutput is
                # a legacy COLOR-node input and should not trigger a duplicate
                # Composite evaluation.
                return
            logger.info(f"[COMPOSITE-DEBUG] update_color_output: submitting eval, tex={texture_id}, rev={color_revision}")
            import time as _time
            rid = self._request_id_gen.next_id()
            self._last_submitted_request_id = rid
            _snap = self._node_graph.snapshot(
                color_texture_id=texture_id,
                color_width=width,
                color_height=height,
                color_revision=color_revision,
                frame_number=self._current_keyframe_frame(),
                fps=self.comp_clip_fps(),
            )
            _snap = self._inject_viewer3d_scene_snapshot(_snap)
            _vmo = self._apply_view_mode_overrides(_snap)
            request = RenderRequest(
                snapshot=_snap,
                request_id=rid,
                timestamp=_time.monotonic(),
                runtime_context=self._runtime_context,
                timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
                edit_project=self._project,
                viewer_node_id=self._resolve_viewer_node_id(),
                view_signature=self._view_signature(),
                quad_view=self._is_quad_view(),
                single_ortho_view=self._single_ortho_view_code() or None,
                quad_active_view=self._quad_active_view_code() or None,
                # P7：选中 / 隔离节点 id（outline / isolate 渲染用）
                selected_3d_node_id=self._selected_3d_node_id_for_request(),
                isolate_3d_node_id=self._isolate_3d_node_id_for_request(),
                global_3d_scene_settings=self._global_3d_scene_settings(),
                view_mode_overrides=_vmo,
                proxy_scale=self._preview_quality,
                output_name=getattr(self, "_output_target_name", "display"),
                output_node_id=getattr(self, "_output_target_node_id", None),
            )
            self._renderer.submit(CompositeCommand.EVALUATE_GRAPH, {
                'request': request,
            })

    def set_source_image_size(self, width: int, height: int) -> None:
        """Inject the full-resolution source image dimensions.

        The Composite preview always renders at the 2048-px proxy for
        interactivity, so ``update_color_output`` receives proxy dimensions.
        Project Settings and the Output node, however, should reflect the
        *real* source size so exports default to full resolution and the
        resolution readout is honest. The adapter calls this with
        ``engine.img_full_original.shape`` so we can distinguish the two.

        Falls back to the proxy dimensions when this is never called.
        """
        try:
            w = int(width)
            h = int(height)
        except (TypeError, ValueError):
            return
        if w > 0 and h > 0:
            self._source_image_size = (w, h)

    def _sync_output_resolution_to_image(self, proxy_width: int, proxy_height: int) -> None:
        """Mirror the source image size into the Output node's width/height.

        Project Settings reads ``output.width``/``output.height`` to display
        the composition resolution, and the Output node uses those params to
        size the rendered canvas when ``format_mode != "input"``. Graphs
        loaded from a .topos file are migrated to ``format_mode="custom"``
        with the 1920x1080 default, so without this sync the composition
        canvas (and the Project Settings readout) stay at 1080p regardless
        of the actual source image.

        The composition canvas follows the source image until the user picks
        a custom resolution in Project Settings. Detection of a user override
        relies on comparing the current Output size against the last value we
        synced: once the two diverge (and it is not just the new image size),
        the user has taken manual control and we stop following the image.

        Prefers the full-resolution source size (``_source_image_size``)
        over the proxy dimensions so the readout shows the real source
        resolution (e.g. 4688×7040), not the 2048-px preview proxy.
        """
        # Once the user has manually set a resolution via Project Settings,
        # never override it — their choice is authoritative.
        if self._output_resolution_user_locked:
            return

        # Prefer full-res source dimensions; fall back to the proxy size.
        if self._source_image_size:
            new_w, new_h = self._source_image_size
        else:
            if not proxy_width or not proxy_height:
                return
            new_w, new_h = int(proxy_width), int(proxy_height)
        if self._node_graph is None:
            return

        output_node = next(
            (n for n in self._node_graph.nodes.values()
             if getattr(n, "node_type", "") == "output"),
            None,
        )
        if output_node is None:
            return

        cur_w = int(output_node.parameters.get("width", 0) or 0)
        cur_h = int(output_node.parameters.get("height", 0) or 0)
        synced = self._synced_output_image_size

        # If the current size no longer matches what we last synced AND it is
        # not already the new image size, the user has set a custom resolution
        # — relinquish auto-follow so their choice is respected.
        if synced is not None and (cur_w, cur_h) != synced and (cur_w, cur_h) != (new_w, new_h):
            self._synced_output_image_size = None
            return
        if synced is None and (cur_w, cur_h) not in ((new_w, new_h), (1920, 1080), (0, 0)):
            # Already a non-default, non-image size on the very first sample
            # → treat as a pre-existing user override.
            return

        if (cur_w, cur_h) == (new_w, new_h):
            # Already matches; just remember it so future overrides are
            # detected correctly.
            self._synced_output_image_size = (new_w, new_h)
            return

        # Follow the image: write the source dimensions into the Output node.
        self._node_graph.set_node_parameter(output_node.id, "width", new_w)
        self._node_graph.set_node_parameter(output_node.id, "height", new_h)
        self._synced_output_image_size = (new_w, new_h)

        # Refresh Project Settings so the spinboxes follow the image.
        section = getattr(self._properties_panel, "_project_settings_section", None) \
            if self._properties_panel is not None else None
        if section is not None:
            try:
                section.load_from_graph(self._node_graph)
            except Exception:
                pass

    def cleanup(self) -> None:
        """Stop playback, panel timers, renderer and release resources."""
        # AUD-9505: stop every single-shot/debounce timer BEFORE anything
        # else. An armed ``_renderer_retry_timer`` firing ~300 ms after this
        # method would pass the ``_renderer is None`` guard and rebuild +
        # start a fresh renderer thread against a torn-down page (orphan GL
        # resources). Dropping the pending payloads too, so a late flush
        # cannot resurrect a half-dead interaction, and clearing
        # ``_is_active`` disables the retry path entirely (double guard —
        # ``_retry_renderer_init`` early-returns when inactive).
        for timer_attr in (
            "_renderer_retry_timer",
            "_eval_debounce_timer",
            "_prefetch_timer",
            "_param_debounce_timer",
            "_batch_param_debounce_timer",
            "_minimap_timer",
            "_interaction_debounce_timer",
            "_nav3d_restore_timer",
        ):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                timer.stop()
        self._pending_param_change = None
        self._pending_batch_param_change = None
        self._pending_interaction_update = None
        self._is_active = False
        self.comp_pause()
        # Cancel in-flight tracker analyze workers before teardown — a
        # QThread destroyed while run() is still executing aborts the
        # process ("Destroyed while thread is still running").
        for worker in list(getattr(self, "_tracker_workers", {}).values()):
            try:
                worker.request_cancel()
                worker.wait(2000)
                worker.deleteLater()
            except RuntimeError:
                logger.debug("Tracker worker cleanup race", exc_info=True)
        self._tracker_workers = {}
        # Cancel page-owned AI compute workers (manager lives on this
        # thread's event loop; its QThreads must not outlive the page).
        ai_mgr = getattr(self, "_ai_compute_manager", None)
        if ai_mgr is not None:
            try:
                ai_mgr.cancel_all()
            except Exception:
                logger.debug("AI manager cleanup failed", exc_info=True)
        keyframes_panel = getattr(self, "_keyframes_panel", None)
        if keyframes_panel is not None and hasattr(keyframes_panel, "dispose"):
            try:
                keyframes_panel.dispose()
            except (AttributeError, RuntimeError):
                logger.debug("Composite keyframes panel cleanup failed",
                             exc_info=True)
        if self._renderer:
            stopped = self._renderer.stop_thread()
            if stopped:
                self._renderer = None
                self._renderer_stop_result = "stopped"
            else:
                # Keep the live QThread referenced after a bounded timeout;
                # dropping it here could let Python collect wrappers while
                # the render thread still owns and uses GL resources.
                # R-24：如实记录超时结果——pipeline_shutdown_complete()
                # 让 MainWindow 不把整条管线标记为已停机（保留重试）。
                self._renderer_stop_result = "timeout"
                logger.error(
                    "Composite renderer is still running during cleanup "
                    "(result=timeout)")

    def pipeline_shutdown_complete(self) -> bool:
        """R-24：合成渲染线程是否确已停止。

        cleanup() 超时只保留引用并记日志——旧合同把“已尽力等待”当成
        “已停止”，MainWindow 随后把整条管线永久标记 done 并继续销毁
        窗口树。本方法如实上报：仅当渲染器引用已被清空（stop_thread
        确认线程退出）才返回 True。
        """
        return getattr(self, "_renderer", None) is None

    # ── Comp Clip Promotion Signal ──

    def _emit_user_edit_once(self) -> None:
        """Emit composite_user_edit_started signal once per composite session.

        Called from node edit callbacks (_on_node_added, _on_nodes_connected,
        etc.) to notify EditController that the user has made a real edit.
        The signal fires only once; subsequent edits are ignored until
        reset_composite_session() is called.
        """
        if not self._user_edit_fired and self._current_composite_id:
            self._user_edit_fired = True
            self.composite_user_edit_started.emit(self._current_composite_id)

    def reset_composite_session(self) -> None:
        """Reset session-scoped state when another composition loads.

        旧实现只清 one-shot edit 标志，跨合成 load 会残留：

        - 撤销历史（U-02）：旧 action 的 lambda 解析 ``self._node_graph``
          的当前（新）图，Ctrl+Z 会用旧图的 node_id/参数改写新合成；
        - pending 防抖事务（U-02）：图被整体替换，提交旧图 pending 只会
          给新图压入错误历史——取消而不是提交；
        - 编辑器跨图会话态（U-09/U-13）：锁与分组过滤属于上一张图的
          UI schema，残留会让新图节点被隐藏或不可编辑；
        - 上一合成的纹理/元数据缓存：避免空图短暂显示旧渲染。
        """
        self._user_edit_fired = False

        # U-02: stop debounce timers and abandon pending transactions.
        self._param_debounce_timer.stop()
        self._batch_param_debounce_timer.stop()
        self._pending_param_change = None
        self._pending_batch_param_change = None
        # U-16：跨图切换同样退出可能残留的静默装配事务。
        self._compound_paste_active = False
        # End coalescing BEFORE clearing: end_merge_coalescing flushes any
        # pending merged action into the tree, which clear() then drops.
        self._undo_stack.end_merge_coalescing()
        self._undo_stack.clear()

        # U-09/U-13: editor transient state belongs to the previous graph.
        editor = getattr(self, "_node_editor", None)
        reset_editor = getattr(editor, "reset_session_state", None)
        if callable(reset_editor):
            reset_editor()

        # Drop stale viewer/preview artifacts from the previous composition.
        self._last_texture_id = None
        self._last_texture_width = 0
        self._last_texture_height = 0
        self._last_metadata = {}

    # ── Node Operations ──

    def _add_image_input_node(self) -> None:
        """Add an Image Input node with file dialog."""
        from PyQt6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Image", "",
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.exr *.hdr *.dpx);;All Files (*)"
        )
        if path:
            node = ImageInputNode()
            node.set_parameter("file_path", path)
            node.position = (100, 400)
            node.set_state_callback(self._on_node_state_changed)
            self._node_graph.add_node(node)
            self._node_editor.add_node_visual(node)
            self._update_node_item_state(node)
            self._push_add_undo(node)
            self._evaluate_graph()
            self._refresh_keyframes_and_spline_for_node(node.id)

    def _add_merge_node(self) -> None:
        self._add_node_by_type(MergeNode, (350, 300))

    def _add_shuffle_node(self) -> None:
        self._add_node_by_type(ShuffleNode, (350, 300))

    def _add_copy_channel_node(self) -> None:
        self._add_node_by_type(CopyChannelNode, (350, 300))

    def _add_premult_node(self) -> None:
        self._add_node_by_type(PremultNode, (350, 300))

    def _add_unpremult_node(self) -> None:
        self._add_node_by_type(UnpremultNode, (350, 300))

    def _add_cs_transform_node(self) -> None:
        self._add_node_by_type(CSTransformNode, (350, 300))

    def _add_blur_node(self) -> None:
        self._add_node_by_type(BlurNode, (350, 300))

    def _add_grade_node(self) -> None:
        self._add_node_by_type(GradeNode, (350, 300))

    def _add_transform_node(self) -> None:
        self._add_node_by_type(TransformNode, (350, 300))

    def _add_reformat_node(self) -> None:
        self._add_node_by_type(ReformatNode, (350, 300))

    def _add_crop_node(self) -> None:
        self._add_node_by_type(CropNode, (350, 300))

    def _add_invert_node(self) -> None:
        self._add_node_by_type(InvertNode, (350, 300))

    def _add_constant_node(self) -> None:
        self._add_node_by_type(ConstantNode, (100, 400))

    def _add_glow_node(self) -> None:
        self._add_node_by_type(GlowNode, (350, 300))

    def _add_denoise_node(self) -> None:
        self._add_node_by_type(DenoiseNode, (350, 300))

    def _add_mask_node(self) -> None:
        self._add_node_by_type(MaskNode, (350, 300))

    def _add_ai_generate_node(self) -> None:
        # AUD-9501：AIGenerateNode 定义在 nodes/ai_generate_node.py（并经
        # node_engine 的 NODE_REGISTRY 注册为 "ai_generate"），本文件从未在
        # 头部导入——原实现调用即 NameError（Qt 槽内逃逸 = qFatal 崩溃）。
        # 按文件内惰性导入风格在方法内导入。
        from src.features.composite.nodes.ai_generate_node import AIGenerateNode
        self._add_node_by_type(AIGenerateNode, (100, 400))

    def _add_node_by_type(self, node_cls, position=(350, 300)) -> None:
        """Generic method to add a node.

        If a node is currently selected in the editor, the new node is
        automatically inserted after it in the connection chain:
            Before: selected -> downstream
            After:  selected -> new_node -> downstream
        """
        self._emit_user_edit_once()

        # Check if a node is selected — auto-connect after it if so
        selected_node_id = self._node_editor.get_selected_node_id()
        if selected_node_id:
            source_item = self._node_editor.get_node_item(selected_node_id)
            if source_item:
                # Position new node to the right of the selected node
                new_x = source_item.pos().x() + source_item.rect().width() + 80
                new_y = source_item.pos().y()
                position = (new_x, new_y)

        # Avoid overlapping existing nodes
        position = self._node_editor._find_non_overlapping_position(
            position[0], position[1]
        )

        node = node_cls()
        node.position = position
        self._initialize_new_topos_color_node(node)
        self._initialize_new_3d_helper_node(node)
        # Camera/Light/Geo/Model are scene objects in the global 3D Viewer, not
        # 2D image processors. Creating them must not auto-insert them into the
        # selected node's chain; users connect them to Scene3D explicitly when
        # they want the Scene3D renderer to consume them.
        if getattr(node, "node_type", None) in {
                "camera3d", "light3d", "geo3d", "model3d"}:
            selected_node_id = None
        # 命名升级（P1）：分配实例序号（Camera1/Light2…）
        from src.features.composite.core.node_engine import assign_instance_index
        assign_instance_index(node, self._node_graph)
        # Register state callback for UI updates
        node.set_state_callback(self._on_node_state_changed)
        self._node_graph.add_node(node)
        self._node_editor.add_node_visual(node)
        # Apply initial state to visual
        self._update_node_item_state(node)

        # Auto-connect after selected node
        if selected_node_id:
            self._node_editor._auto_connect_after(selected_node_id, node)

        self._push_add_undo(node)
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(node.id)

    def load_composite_data(self, composite_data: dict) -> None:
        """
        Load composite graph from CompositeGraphData dict (P9.2 + P1).

        Clears current graph and builds new graph from saved data.
        Supports both old format (node_id/node_type) and new format (id/type).

        Args:
            composite_data: CompositeGraphData serialized dict
        """
        from src.features.composite.core.node_engine import NODE_REGISTRY
        from src.features.composite.nodes.unknown_placeholder_node import UnknownPlaceholderNode

        logger.info(f"[CompositePage] Loading composite data: {composite_data.get('composite_id', 'unknown')}")

        # U-05：先验证再切换身份。缺失 nodes 字段（或非 list）是格式
        # 无效——保持当前图/身份/历史并给出诊断；nodes=[] 是合法空图，
        # 继续走完整清图+元数据恢复。
        nodes_data = composite_data.get("nodes")
        if not isinstance(nodes_data, list):
            logger.warning(
                "[CompositePage] Invalid composite payload (missing/invalid "
                "'nodes'); keeping current graph: composite_id=%r",
                composite_data.get("composite_id"))
            return

        # Track the current composite_id for edit-started signal
        loaded_id = composite_data.get("composite_id", "")
        if loaded_id and loaded_id != self._current_composite_id:
            self._current_composite_id = loaded_id
            self.reset_composite_session()
            self._last_published_graph_data = None
            # AUD-9508: force the next request to republish the loaded graph.
            self._last_published_graph_generation = -1

        # A loaded composition is editable even when the corresponding Media
        # Pool item is no longer selected.  The composition id is the source
        # of truth for this state.
        if loaded_id:
            self.set_composition_active(True)

        if not nodes_data:
            logger.info("[CompositePage] Loading legal empty composite; "
                        "clearing previous graph and applying metadata")

        # Older empty Comp Clips did not mark their default Constant node.
        # Migrate the first legacy background generator in memory so both
        # Project Settings and future Comp Clip edits leave added Constants
        # alone.  New graphs carry the marker from the factory directly.
        if composite_data.get("source_kind") == "empty_comp_clip":
            has_background_marker = any(
                node.get("type") == "constant"
                and bool(node.get("parameters", {}).get(
                    PROJECT_BACKGROUND_PARAM, False))
                for node in nodes_data
            )
            if not has_background_marker:
                migrated_nodes = list(nodes_data)
                for index, node in enumerate(migrated_nodes):
                    if node.get("type") != "constant":
                        continue
                    migrated_node = dict(node)
                    params = dict(migrated_node.get("parameters", {}))
                    params[PROJECT_BACKGROUND_PARAM] = True
                    migrated_node["parameters"] = params
                    migrated_nodes[index] = migrated_node
                    break
                nodes_data = migrated_nodes

        # Clear current graph
        self._node_graph.clear()
        self._node_editor.clear_all()

        # Load nodes (support both old and new format)
        for node_dict in nodes_data:
            # Support both formats: node_id/node_type (old) and id/type (new)
            node_type = node_dict.get("type") or node_dict.get("node_type") or ""

            # Create node from registry
            node_cls = NODE_REGISTRY.get(node_type)
            if node_cls is None:
                # Unknown type — create UnknownPlaceholderNode to preserve data
                logger.warning(f"Unknown node type: {node_type}, creating placeholder")
                node = UnknownPlaceholderNode.from_dict(node_dict)
            else:
                node = node_cls.from_dict(node_dict)

            # Set state callback
            node.set_state_callback(self._on_node_state_changed)

            # 命名升级（P1）：加载后重新分配实例序号（派生量，不依赖持久化）
            from src.features.composite.core.node_engine import assign_instance_index
            assign_instance_index(node, self._node_graph)

            self._node_graph.add_node(node)
            self._node_editor.add_node_visual(node)
            self._update_node_item_state(node)

        # Load connections
        connections_data = composite_data.get("connections", [])
        for conn_dict in connections_data:
            src_node_id = conn_dict.get("src_node_id", "")
            src_port_name = conn_dict.get("src_port_name", "")
            dst_node_id = conn_dict.get("dst_node_id", "")
            dst_port_name = conn_dict.get("dst_port_name", "")

            # Connect nodes
            success = self._node_graph.connect(
                src_node_id, src_port_name,
                dst_node_id, dst_port_name,
            )
            if success:
                self._node_editor.add_connection_visual(
                    src_node_id, src_port_name,
                    dst_node_id, dst_port_name,
                )
            else:
                logger.warning(
                    f"Invalid composite connection: "
                    f"{src_node_id}.{src_port_name} -> {dst_node_id}.{dst_port_name}"
                )

        # LayerStack/Layer state is part of the same composite payload as the
        # graph.  The older page loader rebuilt only nodes and connections,
        # which made every saved AE layer order/visibility/parent disappear on
        # the next open even though ``NodeGraph.to_dict`` persisted it.
        self._node_graph._load_layer_state(
            composite_data.get("layer_stacks"),
            composite_data.get("layers"),
        )

        # P1: Load viewer_state and viewport_state
        # 载入的 state/装饰字典必须深拷贝：NodeGraph.clear() 会原地清空
        # 这些容器，直接引用 payload 会让下一次 load 清空调用方的数据
        # （如 EditProject 里的 composition 记录）。
        viewer_state = copy.deepcopy(composite_data.get("viewer_state", {}))
        viewport_state = copy.deepcopy(composite_data.get("viewport_state", {}))

        if viewer_state:
            self._node_graph._viewer_state = viewer_state
            self._node_editor.set_viewer_state(viewer_state)
            self._output_target_name = str(
                viewer_state.get("output_target_name", "display") or "display")
            self._output_target_node_id = viewer_state.get("output_target_node_id")

        raw_asset_roots = (
            viewer_state.get("asset_search_roots", ())
            if isinstance(viewer_state, dict) else ()
        )
        if isinstance(raw_asset_roots, (list, tuple)):
            self.set_asset_search_roots(
                tuple(str(root) for root in raw_asset_roots), persist=False)
        else:
            self.set_asset_search_roots((), persist=False)

        if viewport_state:
            self._node_graph._viewport_state = viewport_state
            self._node_editor.set_viewport_state(viewport_state)

        # P1: Load backdrops, sticky notes, and groups
        backdrops = copy.deepcopy(composite_data.get("backdrops", {}))
        sticky_notes = copy.deepcopy(composite_data.get("sticky_notes", {}))
        groups = copy.deepcopy(composite_data.get("groups", {}))

        # Backdrops and sticky notes are stored in NodeGraph, applied by NodeEditorWidget
        self._node_graph.backdrops = backdrops
        self._node_graph._sticky_notes = sticky_notes
        if groups:
            self._node_graph._node_groups = groups

        # U-06：装饰数据恢复到模型后必须同时重建视觉对象——旧 loader
        # 只写 model 计数，画布上不出现任何 Backdrop/便签。复用
        # build_from_graph 的既有恢复入口与包含关系刷新。
        if isinstance(backdrops, dict):
            for backdrop_data in backdrops.values():
                self._node_editor.add_backdrop_visual(backdrop_data)
        if isinstance(sticky_notes, dict):
            for sticky_note_data in sticky_notes.values():
                self._node_editor.add_sticky_note_visual(sticky_note_data)
        self._node_editor._refresh_all_backdrop_containment()

        # Comp-clip timeline metadata. An independent comp clip (created via
        # "New Comp Clip") carries its own fps/duration even without a runtime
        # context; caching it lets the comp-clip playback timer + the bottom
        # transport bar drive frame playback without a timeline/video controller.
        # AUD-9506: this dict comes from the persisted .topos payload (external
        # data). Convert defensively per field — a hand-edited/corrupt file
        # (e.g. fps="24fps") must fall back to the default instead of raising
        # ValueError/TypeError out of the load path (same style as
        # set_source_image_size above).
        try:
            self._composite_duration_frames = int(
                composite_data.get("duration_frames", 0) or 0)
        except (TypeError, ValueError):
            logger.warning(
                "[CompositePage] Invalid duration_frames=%r in composite "
                "data; falling back to 0",
                composite_data.get("duration_frames"))
            self._composite_duration_frames = 0
        try:
            self._composite_fps = float(
                composite_data.get("fps", 24.0) or 24.0)
        except (TypeError, ValueError):
            logger.warning(
                "[CompositePage] Invalid fps=%r in composite data; "
                "falling back to 24.0",
                composite_data.get("fps"))
            self._composite_fps = 24.0
        self._node_graph.fps = self._composite_fps
        self._node_graph.duration_frames = self._composite_duration_frames
        self._is_comp_clip = bool(
            composite_data.get("comp_clip_id")
            or composite_data.get("source_kind") == "empty_comp_clip"
        )

        # Evaluate graph
        self._evaluate_graph()
        self._update_minimap()
        self._refresh_keyframes_panel()
        self._refresh_asset_dependency_panel()

        logger.info(f"[CompositePage] Loaded {len(nodes_data)} nodes, {len(connections_data)} connections")

    def get_composite_data(self) -> dict:
        """
        Get current composite graph data for saving (P1).

        Returns NodeGraph.to_dict() format, compatible with CompositeGraphData.
        Includes: nodes, connections, viewer_state, viewport_state, backdrops, sticky_notes, groups.

        Returns:
            dict: Complete composite graph data
        """
        # Persist the editor-owned selection/view state before serializing the
        # graph.  Without this call, a save performed through the Composite
        # page could omit the currently selected Model3D and reopen with the
        # animation/import panels detached from the user's last context.
        node_editor = getattr(self, "_node_editor", None)
        save_ui_state = getattr(node_editor, "save_ui_state", None)
        if callable(save_ui_state):
            save_ui_state()
        viewer_state = dict(getattr(self._node_graph, "_viewer_state", {}) or {})
        # Some persistence tests use CompositePage.__new__ without invoking
        # the Qt superclass constructor. Reading QObject attributes through
        # getattr can raise in that state, while __dict__ remains safe.
        page_state = self.__dict__
        viewer_state["output_target_name"] = page_state.get(
            "_output_target_name", "display") or "display"
        viewer_state["output_target_node_id"] = page_state.get(
            "_output_target_node_id")
        self._node_graph._viewer_state = viewer_state
        data = self._node_graph.to_dict()
        # Keep metadata explicit for older/injected NodeGraph instances that
        # do not expose the project-level attributes.
        data["fps"] = float(getattr(self, "_composite_fps", 24.0) or 24.0)
        data["duration_frames"] = int(
            getattr(self, "_composite_duration_frames", 0) or 0
        )
        return data

    def _on_node_double_clicked(self, node_id: str) -> None:
        node = self._node_graph.get_node(node_id)
        if isinstance(node, (COLORNode, ToposColorNode)):
            self.switch_to_color_requested.emit()

    def _on_nodes_connected(self, src_node_id: str, src_port: str,
                            dst_node_id: str, dst_port: str) -> None:
        self._emit_user_edit_once()
        # Get data_type for visual before connecting
        src_node = self._node_graph.get_node(src_node_id)
        data_type = "image"
        if src_node:
            port = src_node.get_output_port(src_port)
            if port:
                data_type = port.data_type

        # U-14：connect 提交前捕获该输入端口的旧边。NodeGraph.connect
        # 对单输入端口会静默替换旧边——不捕获的话 undo 只断新线，
        # 旧线永久丢失。
        dst_node = self._node_graph.get_node(dst_node_id)
        dst_port_obj = dst_node.get_input_port(dst_port) if dst_node else None
        is_multi_input = (dst_port_obj is not None
                          and dst_port_obj.max_connections > 1)
        old_edges: list = []
        if not is_multi_input:
            old_edges = [
                (c.src_node_id, c.src_port_name)
                for c in self._node_graph.get_input_connections(dst_node_id)
                if c.dst_port_name == dst_port
            ]

        # Connect in data model FIRST — if it fails, visual stays unchanged
        success = self._node_graph.connect(src_node_id, src_port, dst_node_id, dst_port)
        if not success:
            return

        # Sync visual: 单输入端口按精确 quad 移除旧线（U-15：by-dst 会
        # 误删多输入端口的其他视觉线）；多输入端口直接追加。
        if not is_multi_input:
            for old_src, old_port in old_edges:
                self._node_editor.remove_connection_visual_exact(
                    old_src, old_port, dst_node_id, dst_port)
        self._node_editor.add_connection_visual(
            src_node_id, src_port, dst_node_id, dst_port, data_type
        )
        if self.__dict__.get("_compound_paste_active"):
            # U-16：粘贴/预设装配期——模型+视觉照常，undo 由
            # nodes_pasted 统一成组压栈，不产生游离的 Connect 记录。
            pass
        elif old_edges == [(src_node_id, src_port)]:
            # 重复连接同一条边：图与视觉均恢复原状，不生成历史。
            pass
        elif old_edges:
            # U-14：替换是一个原子命令（removed + added），一步undo
            # 恢复旧线。
            self._push_replace_connection_undo(
                old_edges, (src_node_id, src_port, dst_node_id, dst_port))
        else:
            self._push_connect_undo(src_node_id, src_port, dst_node_id, dst_port)
        self._sync_layer_stack_after_node_edit()
        self._evaluate_graph()

    def _on_nodes_disconnected(self, src_node_id, src_port, dst_node_id, dst_port) -> None:
        self._emit_user_edit_once()
        # Disconnect in data model FIRST
        self._node_graph.disconnect(src_node_id, src_port, dst_node_id, dst_port)
        # U-15：按精确 quad 删除视觉线。by-dst 只删目标端口第一条，
        # 多输入端口上会误删其他来源的线。
        self._node_editor.remove_connection_visual_exact(
            src_node_id, src_port, dst_node_id, dst_port)
        self._push_disconnect_undo(src_node_id, src_port, dst_node_id, dst_port)
        self._sync_layer_stack_after_node_edit()
        self._evaluate_graph()

    def _on_node_about_to_be_removed(self, node_id: str) -> None:
        """Detach transient UI/viewer state before the QGraphicsItem is removed."""
        self._prepare_nodes_for_removal([node_id])
        self._capture_layer_node_removal_snapshot([node_id])

    def _on_nodes_about_to_be_removed(self, node_ids: list) -> None:
        """Detach transient UI/viewer state before multiple node items are removed."""
        ids = list(node_ids or [])
        self._prepare_nodes_for_removal(ids)
        self._capture_layer_node_removal_snapshot(ids)

    def _capture_layer_node_removal_snapshot(self, node_ids: list[str]) -> None:
        """Capture state before NodeEditorActions reconnects deleted nodes."""
        self._pending_layer_node_removal_snapshot = None
        self._pending_layer_node_removal_ids = [str(nid) for nid in node_ids]
        controller = getattr(self, "_layer_stack_controller", None)
        if controller is None or not node_ids:
            return
        try:
            affected = {
                layer_id
                for node_id in node_ids
                for layer_id in controller.layer_ids_for_node(node_id)
            }
            if affected:
                self._pending_layer_node_removal_snapshot = controller.snapshot()
        except Exception:
            logger.exception("Unable to capture Layer delete snapshot")

    def _restore_layer_node_snapshot_for_ui(self, snapshot: dict) -> None:
        """Restore a controller snapshot and rebuild every dependent view."""
        controller = getattr(self, "_layer_stack_controller", None)
        if controller is None:
            return
        controller.restore_snapshot(snapshot)
        self._node_editor.clear_all()
        self._node_editor.build_from_graph(self._node_graph)
        self._refresh_keyframes_panel()
        self._evaluate_graph()
        self._clear_stale_properties()

    def _push_layer_node_delete_undo(self, before: dict, after: dict) -> None:
        """Push one undo action covering Layer + Transform + Merge cleanup."""
        payload = {
            "before": copy.deepcopy(before),
            "after": copy.deepcopy(after),
        }
        self._undo_stack.push(UndoAction(
            "delete_layer_nodes", payload,
            undo_fn=lambda data: self._restore_layer_node_snapshot_for_ui(
                data["before"]),
            redo_fn=lambda data: self._restore_layer_node_snapshot_for_ui(
                data["after"]),
            description="Delete layer and owned nodes",
        ))

    def _delete_nodes_with_owned_layers(self, node_ids: list[str],
                                        before: Optional[dict]) -> bool:
        """Delete selected nodes and any Layer chains that own them.

        NodeEditorActions reconnects around nodes before notifying the page.
        Restoring the pre-edit snapshot here makes the Layer compiler see the
        original source → Transform → Merge topology, and lets one native
        undo restore the whole transaction.
        """
        controller = getattr(self, "_layer_stack_controller", None)
        if controller is None:
            return False
        node_ids = [str(nid) for nid in node_ids]
        if before is None:
            try:
                before = controller.snapshot()
            except Exception:
                return False

        controller.restore_snapshot(before)
        affected_layer_ids = {
            layer_id
            for node_id in node_ids
            for layer_id in controller.layer_ids_for_node(node_id)
        }
        if not affected_layer_ids:
            return False

        layers = controller.layers_map()
        source_layer_ids = {
            layer_id for layer_id in affected_layer_ids
            if any(node_id == layers[layer_id].source_node_id
                   for node_id in node_ids)
        }
        root_only_layer_ids = affected_layer_ids - source_layer_ids

        # Deleting only an effect/root node is the node-editor equivalent of
        # removing an AE effect: keep the source layer and let the compiler
        # connect its upstream image directly to Merge.
        for layer_id in list(root_only_layer_ids):
            layer = layers.get(layer_id)
            if layer is None or not getattr(layer, "managed", True):
                continue
            if not controller.remove_layer_root_node(
                    layer_id, description="Delete layer Transform",
                    record_undo=False):
                self._restore_layer_node_snapshot_for_ui(before)
                return True

        # Deleting the actual source still removes the complete material
        # layer, including its owned Transform and generated Merge chain.
        for layer_id in list(source_layer_ids):
            if not controller.remove_layer(
                    layer_id, delete_nodes=True,
                    description="Delete material node layer",
                    record_undo=False):
                # A locked layer is protected in both editing metaphors. Put
                # the graph back and treat this as a handled no-op.
                self._restore_layer_node_snapshot_for_ui(before)
                return True

        # A multi-selection may include ordinary free-graph nodes too. Remove
        # those with the existing Nuke-style reconnect semantics.
        for node_id in node_ids:
            if self._node_graph.get_node(node_id) is not None:
                self._node_graph.remove_node_and_reconnect(node_id)

        after = controller.snapshot()
        removed_node_ids = set(before.get("nodes", {})) - set(
            after.get("nodes", {}))
        for removed_id in removed_node_ids:
            if self._renderer:
                self._renderer.submit(
                    CompositeCommand.CLEANUP_NODE, {"node_id": removed_id})
        self._last_selected_node_id = None
        self._last_selected_layer_id = None
        self._push_layer_node_delete_undo(before, after)
        self._node_editor.clear_all()
        self._node_editor.build_from_graph(self._node_graph)
        self._refresh_keyframes_panel()
        self._evaluate_graph()
        self._clear_stale_properties()
        return True

    def _prepare_nodes_for_removal(self, node_ids: list[str]) -> None:
        """Clear UI-only references while the nodes still exist in the graph.

        Camera3D/Light3D/Geo3D/Model3D can be referenced by the 3D viewport
        overlay, gizmo drag state, active-camera state, and the properties
        panel at the same time. Clearing those references before the node item
        leaves the QGraphicsScene avoids a paint/mouse event observing a
        half-deleted Camera item.
        """
        if not node_ids:
            return
        node_set = set(node_ids)
        for node_id in node_ids:
            self._clear_deleted_node_viewer_state(node_id)

        if self._viewer is not None:
            try:
                if hasattr(self._viewer, "clear_selection"):
                    self._viewer.clear_selection()
                elif hasattr(self._viewer, "set_selected_node"):
                    self._viewer.set_selected_node(None)
                if hasattr(self._viewer, "set_node_info"):
                    self._viewer.set_node_info(None, {}, None, node_id=None)
                if hasattr(self._viewer, "_cancel_3d_interaction_state"):
                    self._viewer._cancel_3d_interaction_state()
            except Exception:
                pass

        if getattr(self, "_last_selected_node_id", None) in node_set:
            self._last_selected_node_id = None
        if self._thumbnail_manager is not None:
            try:
                self._thumbnail_manager.set_selected_node(None)
            except Exception:
                pass
        if hasattr(self, "_properties_panel") and self._properties_panel is not None:
            try:
                current = getattr(self._properties_panel, "_current_node_id", None)
                current_ids = set(getattr(self._properties_panel, "_current_node_ids", []) or [])
                if current in node_set or (current_ids and current_ids.intersection(node_set)):
                    self._properties_panel.clear()
            except Exception:
                pass

    def _on_node_removed(self, node_id: str) -> None:
        self._emit_user_edit_once()
        pending_snapshot = self._pending_layer_node_removal_snapshot
        pending_ids = list(self._pending_layer_node_removal_ids or [node_id])
        self._pending_layer_node_removal_snapshot = None
        self._pending_layer_node_removal_ids = []
        if self._delete_nodes_with_owned_layers(
                pending_ids, pending_snapshot):
            return
        node = self._node_graph.get_node(node_id)
        if node:
            self._push_remove_undo(node)
        self._clear_deleted_node_viewer_state(node_id)
        # If the deleted node was being previewed, exit node-preview so the
        # viewer gracefully falls back to the Output node (no stale id leak).
        if self._preview_node_id == node_id:
            self._preview_node_id = None
            self._update_preview_connection()
            self._update_preview_status_label()
        # Schedule GPU cleanup by node_id so render thread cleans its own instances
        if self._renderer:
            self._renderer.submit(CompositeCommand.CLEANUP_NODE, {'node_id': node_id})
        self._node_graph.remove_node(node_id)
        self._sync_layer_stack_after_node_edit()
        self._evaluate_graph()
        self._clear_stale_properties()

    def _on_node_added(self, node_id: str, node_type: str) -> None:
        """Handle node added from context menu or duplication."""
        self._emit_user_edit_once()
        node = self._node_graph.get_node(node_id)
        if node:
            self._initialize_new_topos_color_node(node)
            self._initialize_new_3d_helper_node(node)
            node.set_state_callback(self._on_node_state_changed)
            self._update_node_item_state(node)
            self._push_add_undo(node)
            # Trigger render refresh after node is added
            # Note: Toolbar _add_node_by_type() directly calls _evaluate_graph()
            # and does NOT emit node_added signal, so no double-evaluation occurs.
            self._evaluate_graph()
            self._refresh_keyframes_and_spline_for_node(node_id)
            self._sync_layer_stack_after_node_edit()

    def _initialize_new_topos_color_node(self, node) -> None:
        """Mark an interactively added Topos Color as final-grade-only.

        The primary node is created by the timeline graph factory and carries
        ``role=primary_clip_grade``. Interactive nodes use the complementary
        role and an unlinked source, so Color-page synchronization cannot
        overwrite them with the source clip's ColorStack.
        """
        if not isinstance(node, ToposColorNode):
            return
        if node.parameters.get("role") == "primary_clip_grade":
            return

        node.set_parameter("role", "composite_final_grade")
        node.set_parameter("linked", False)
        node.set_parameter("source_clip_id", "")
        node.set_parameter("source_timeline_id", "")
        if not node.parameters.get("color_stack_id"):
            # Neutral independent mode. It deliberately does not resolve the
            # timeline clip stack; the user can later choose an embedded/preset
            # grade for this final-stage node.
            node.set_parameter("stack_source", "embedded")
            node.set_parameter("color_stack_id", "")
            node.set_parameter("embedded_stack", None)
        node.set_parameter("bypass", False)

    def _initialize_new_3d_helper_node(self, node) -> None:
        """Initialize newly created 3D scene/helper nodes for the current project."""
        node_type = getattr(node, "node_type", "")
        if node_type == "scene3d":
            width, height = self._project_output_size()
            try:
                node.set_parameter("width", width)
                node.set_parameter("height", height)
            except Exception:
                params = getattr(node, "parameters", None)
                if isinstance(params, dict):
                    params["width"] = width
                    params["height"] = height
            return
        if self._viewer is None:
            return
        if node_type == "camera3d":
            getter = getattr(self._viewer, "suggested_camera3d_params_for_new_node", None)
        elif node_type == "light3d":
            getter = getattr(self._viewer, "suggested_light3d_params_for_new_node", None)
        else:
            return
        if not callable(getter):
            return
        try:
            params = getter()
            for key, value in params.items():
                node.set_parameter(key, value)
        except Exception:
            return

    def _on_placeholder_node_added(self, node_title: str) -> None:
        """Handle placeholder node added — show warning message to user."""
        from src.shared.i18n.i18n import I18N
        msg = I18N.tr(
            "composite.placeholder_warning",
            f"'{node_title}' is a placeholder node — rendering not implemented"
        )
        self.status_message.emit(msg)

    def _on_nodes_pasting(self, pasted_data: list) -> None:
        """粘贴/预设装配开始：进入静默连线事务（U-16）。

        装配期间的 node_connected 只同步模型与视觉，不压独立 undo；
        全部节点与内部连线由随后的 nodes_pasted 处理器成组记录。
        """
        self._compound_paste_active = True

    def _on_nodes_pasted(self, pasted_data: list) -> None:
        """Handle nodes pasted from clipboard — push grouped undo."""
        self._emit_user_edit_once()
        try:
            if not pasted_data:
                return
            pasted_ids = {d.get("id") for d in pasted_data}
            self._undo_stack.begin_group()
            for data in pasted_data:
                node_id = data.get("id")
                node = self._node_graph.get_node(node_id)
                if node:
                    node.set_state_callback(self._on_node_state_changed)
                    self._update_node_item_state(node)
                    self._push_add_undo(node)
            # Record internal connections among pasted nodes (already connected
            # in NodeEditorWidget._paste_nodes, but we need undo entries).
            # Use a set to avoid pushing the same connection twice.
            seen_conns = set()
            for data in pasted_data:
                node_id = data.get("id")
                if node_id:
                    conns = self._node_graph.get_connections_for_node(node_id)
                    for c in conns:
                        if c.src_node_id in pasted_ids and c.dst_node_id in pasted_ids:
                            conn_key = (c.src_node_id, c.src_port_name,
                                        c.dst_node_id, c.dst_port_name)
                            if conn_key not in seen_conns:
                                seen_conns.add(conn_key)
                                self._push_connect_undo(*conn_key)
            self._undo_stack.end_group("Paste nodes")
            self._sync_layer_stack_after_node_edit()
            self._evaluate_graph()
            self._refresh_keyframes_and_spline_for_node(next(iter(pasted_ids)))
        finally:
            # U-16：无论装配是否抛异常都退出静默事务，避免后续正常
            # 连线被静默吞掉 undo。
            self._compound_paste_active = False

    def _on_nodes_duplicated(self, new_ids: list) -> None:
        """Handle nodes duplicated — push grouped undo."""
        self._emit_user_edit_once()
        if not new_ids:
            return
        self._undo_stack.begin_group()
        for node_id in new_ids:
            node = self._node_graph.get_node(node_id)
            if node:
                node.set_state_callback(self._on_node_state_changed)
                self._update_node_item_state(node)
                self._push_add_undo(node)
        self._undo_stack.end_group("Duplicate nodes")
        self._sync_layer_stack_after_node_edit()
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(new_ids[0])

    def _on_nodes_batch_removed(self, node_ids: list) -> None:
        """Handle batch node removal (cut/delete) — push grouped undo."""
        self._emit_user_edit_once()
        if not node_ids:
            return
        pending_snapshot = self._pending_layer_node_removal_snapshot
        self._pending_layer_node_removal_snapshot = None
        self._pending_layer_node_removal_ids = []
        if self._delete_nodes_with_owned_layers(
                list(node_ids), pending_snapshot):
            return
        self._undo_stack.begin_group()
        for node_id in node_ids:
            node = self._node_graph.get_node(node_id)
            if node:
                self._push_remove_undo(node)
            self._clear_deleted_node_viewer_state(node_id)
            if self._renderer:
                self._renderer.submit(CompositeCommand.CLEANUP_NODE, {'node_id': node_id})
            # Use remove_node_and_reconnect for auto-reconnection on single-input/output nodes
            self._node_graph.remove_node_and_reconnect(node_id)
        self._undo_stack.end_group("Delete nodes")
        self._sync_layer_stack_after_node_edit()
        self._evaluate_graph()
        self._clear_stale_properties()

    def _rebind_properties_panel_to_live_node(self) -> None:
        """Point the properties panel at a live node after a graph rebuild.

        ``switch_to_color_output_graph`` / ``switch_to_timeline_input_graph``
        clear and rebuild the node graph but used to leave the panel's
        ``_current_node_id`` pointing at a now-deleted node, so DragValue drags
        emitted ``parameter_changed`` against a dead id and did nothing (the
        drag broke after switching to a 2nd image). Re-select a live node.
        """
        if not getattr(self, "_properties_panel", None):
            return
        if self._node_graph is None or not self._node_graph.nodes:
            self._properties_panel.clear()
            return
        preferred = getattr(self, "_last_selected_node_id", None)
        if preferred and self._node_graph.get_node(preferred) is not None:
            node_id = preferred
        else:
            # Prefer a node with parameters (not the Output node) so the panel
            # doesn't show an empty section right after the switch.
            node_id = next(
                (n.id for n in self._node_graph.nodes.values()
                 if n.node_type != "output"),
                None,
            )
            if node_id is None:
                node_id = next(iter(self._node_graph.nodes))
        self._on_node_selected(node_id)

    def _on_project_settings_requested(self) -> None:
        """Toggle the properties panel's Project Settings view (S shortcut)."""
        if self._properties_panel is None or self._node_graph is None:
            return
        self._properties_panel.toggle_project_settings(
            self._node_graph, settings_callback=self._apply_project_settings)

    def _on_viewer3d_settings_requested(self) -> None:
        """Toggle the default 3D Viewer scene settings section."""
        if self._properties_panel is None or self._node_graph is None:
            return
        self._properties_panel.toggle_viewer3d_settings(
            self._node_graph, settings_callback=self._apply_viewer3d_settings)

    _PROJECT_SETTINGS_PARAM_SPEC = {
        "output": ("format_mode", "width", "height"),
        "scene3d": ("width", "height"),
        "constant": ("color_r", "color_g", "color_b", "color_a",
                     CONSTANT_COLOR_SPACE_PARAM, "width", "height"),
    }

    def _snapshot_project_settings_state(self) -> dict:
        """Capture every piece of state Project Settings can write (U-08)."""
        params = {}
        for node in self._node_graph.nodes.values():
            spec = self._PROJECT_SETTINGS_PARAM_SPEC.get(node.node_type)
            if spec:
                params[node.id] = {
                    key: node.parameters.get(key) for key in spec}
        return {
            # __dict__ 读防御 __new__ 桩页面（Qt 包装属性查找会抛
            # RuntimeError）。
            "fps": self.__dict__.get("_composite_fps", 24.0),
            "duration_frames": self.__dict__.get(
                "_composite_duration_frames", 0),
            "params": params,
            "synced_output_image_size": self.__dict__.get(
                "_synced_output_image_size"),
            "output_resolution_user_locked": self.__dict__.get(
                "_output_resolution_user_locked", False),
        }

    def _restore_project_settings_state(self, state: dict) -> None:
        """Restore a Project Settings snapshot (undo/redo, U-08)."""
        if self._node_graph is None:
            return
        self._composite_fps = state["fps"]
        self._composite_duration_frames = state["duration_frames"]
        self._node_graph.fps = self._composite_fps
        self._node_graph.duration_frames = self._composite_duration_frames
        for node_id, node_params in state["params"].items():
            if self._node_graph.get_node(node_id) is None:
                continue
            for key, value in node_params.items():
                self._node_graph.set_node_parameter(node_id, key, value)
        self._synced_output_image_size = state["synced_output_image_size"]
        self._output_resolution_user_locked = state["output_resolution_user_locked"]
        if hasattr(self, '_evaluate_graph'):
            self._evaluate_graph()
        if ((self.__dict__.get("_keyframes_panel") is not None
             or self.__dict__.get("_spline_panel") is not None)
                and getattr(type(self), "_sync_keyframes_panel_frame_range", None)
                is not None):
            self._sync_keyframes_panel_frame_range()

    def _apply_project_settings(self, settings: dict) -> None:
        """Write Project Settings changes back into the graph.

        Resolution/fps/width/height → output node (authoritative).
        Background color → constant node (background layer).
        Triggers a graph re-evaluation which persists via composite_graph_changed.

        U-08：整项设置以 before/after 快照形成单个 ProjectSettings 命令，
        分辨率/fps/duration/背景色均可一步撤销/重做。
        """
        if self._node_graph is None:
            return

        # An Edit timeline owns timing for a timeline-backed composite. Keep
        # stale settings payloads from changing the clip's FPS or duration.
        runtime = self.__dict__.get("_runtime_context")
        if runtime is not None:
            settings = dict(settings)
            settings["fps"] = float(getattr(runtime, "fps", 24.0) or 24.0)
            duration_frames = int(getattr(runtime, "duration_frames", 0) or 0)
            settings["duration_seconds"] = (
                duration_frames / settings["fps"] if settings["fps"] > 0 else 0.0
            )

        before_state = self._snapshot_project_settings_state()

        width = int(settings.get("width", 1920))
        height = int(settings.get("height", 1080))

        if runtime is None:
            self._composite_fps = max(
                0.001, float(settings.get("fps", 24.0) or 24.0)
            )
            duration_seconds = max(
                0.0, float(settings.get("duration_seconds", 0.0) or 0.0)
            )
            self._composite_duration_frames = max(
                0, int(round(duration_seconds * self._composite_fps))
            )
            self._node_graph.fps = self._composite_fps
            self._node_graph.duration_frames = self._composite_duration_frames

        # Output node owns the composition resolution. Renderer-style source
        # nodes that define a full canvas should follow the same project size.
        background_constant_ids = {
            node.id for node in self._node_graph.nodes.values()
            if node.node_type == "constant"
            and bool(node.parameters.get(PROJECT_BACKGROUND_PARAM, False))
        }
        all_constant_ids = {
            node.id for node in self._node_graph.nodes.values()
            if node.node_type == "constant"
        }
        # Keep legacy/timeline graphs compatible when no explicit background
        # marker exists. Empty Comp Clips are migrated on load above.
        target_constant_ids = background_constant_ids or all_constant_ids

        for node in self._node_graph.nodes.values():
            if node.node_type == "output":
                self._node_graph.set_node_parameter(node.id, "format_mode", "custom")
                self._node_graph.set_node_parameter(node.id, "width", width)
                self._node_graph.set_node_parameter(node.id, "height", height)
            elif node.node_type == "scene3d":
                self._node_graph.set_node_parameter(node.id, "width", width)
                self._node_graph.set_node_parameter(node.id, "height", height)
            elif node.node_type == "constant":
                # All full-frame Constant sources follow project resolution;
                # only the marked background Constant follows project color.
                if node.id in target_constant_ids:
                    bg = settings.get("background_color")
                    if bg is not None:
                        r, g, b, a = bg
                        self._node_graph.set_node_parameter(
                            node.id, "color_r", float(r))
                        self._node_graph.set_node_parameter(
                            node.id, "color_g", float(g))
                        self._node_graph.set_node_parameter(
                            node.id, "color_b", float(b))
                        self._node_graph.set_node_parameter(
                            node.id, "color_a", float(a))
                        self._node_graph.set_node_parameter(
                            node.id, CONSTANT_COLOR_SPACE_PARAM, "linear")
                self._node_graph.set_node_parameter(node.id, "width", width)
                self._node_graph.set_node_parameter(node.id, "height", height)

        # Re-evaluate so the viewer updates and changes persist.
        # The user has taken manual control of the composition resolution, so
        # stop auto-following the source image size.
        self._synced_output_image_size = None
        self._output_resolution_user_locked = True
        self._emit_user_edit_once()
        if hasattr(self, '_evaluate_graph'):
            self._evaluate_graph()
        if ((self.__dict__.get("_keyframes_panel") is not None
             or self.__dict__.get("_spline_panel") is not None)
                and getattr(type(self), "_sync_keyframes_panel_frame_range", None)
                is not None):
            self._sync_keyframes_panel_frame_range()

        # U-08：与提交前快照比较，实际变化才形成单个 ProjectSettings
        # 撤销命令；undo/redo 都走快照恢复，不重放 settings 字典。
        # （__new__ 桩页面没有 _undo_stack 时跳过记录。）
        after_state = self._snapshot_project_settings_state()
        undo_stack = self.__dict__.get("_undo_stack")
        if undo_stack is not None and after_state != before_state:
            undo_stack.push(UndoAction(
                "project_settings",
                {"before": before_state, "after": after_state},
                undo_fn=lambda d: self._restore_project_settings_state(d["before"]),
                redo_fn=lambda d: self._restore_project_settings_state(d["after"]),
                description="Project settings",
            ))

    def _apply_viewer3d_settings(self, settings: dict) -> None:
        """Write default 3D Viewer scene settings to graph viewer_state."""
        if self._node_graph is None:
            return
        viewer_state = dict(getattr(self._node_graph, "_viewer_state", {}) or {})
        viewer_state["default_3d_scene"] = dict(settings)
        self._node_graph._viewer_state = viewer_state

        nav_params = {
            key: settings[key]
            for key in (
                "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
                "cam_target_x", "cam_target_y", "cam_target_z",
            )
            if key in settings
        }
        if self._viewer is not None and nav_params:
            self._viewer.sync_scene3d_params(nav_params)

        self._force_next_view_refresh = True
        self._emit_user_edit_once()
        self._evaluate_graph()
        # 视口外框底色（viewport_bg_*）不是节点渲染产物，不会随图求值刷新，
        # 需显式触发 viewport 重绘以即时反馈（其它场景参数靠上面的求值链路刷新）。
        if self._viewer is not None:
            self._viewer.update()

    def _clear_model3d_auxiliary_panels(self) -> None:
        """Clear optional Model3D panels when no model is selected/active."""
        report_panel = getattr(self, "_import_report_panel", None)
        if report_panel is not None:
            try:
                if hasattr(report_panel, "update_import_state"):
                    report_panel.update_import_state("", 0.0, "")
                if hasattr(report_panel, "update_resource_estimate"):
                    report_panel.update_resource_estimate(None)
                if hasattr(report_panel, "update_format_probe"):
                    report_panel.update_format_probe(None)
                if hasattr(report_panel, "update_diagnostics"):
                    report_panel.update_diagnostics(())
                if hasattr(report_panel, "update_summary"):
                    report_panel.update_summary(None)
                else:
                    report_panel.update_report(None)
            except (AttributeError, RuntimeError):
                pass
        material_panel = self.__dict__.get("_material_panel")
        if material_panel is not None and hasattr(material_panel, "clear"):
            try:
                material_panel.clear()
            except (AttributeError, RuntimeError):
                pass

    def _refresh_asset_dependency_panel(self) -> None:
        """Refresh the optional asset report from the current graph snapshot."""
        panel = self.__dict__.get("_asset_dependency_panel")
        if panel is None:
            return
        try:
            from src.features.composite.core.asset_dependency import (
                collect_asset_dependencies,
            )
            report = collect_asset_dependencies(
                self.get_composite_data(), check_fs=True,
                project_dir=self._asset_project_dir,
                asset_search_roots=self._asset_search_roots,
            )
            panel.update_report(report)
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            logger.warning("Asset dependency scan failed: %s", exc)
            if hasattr(panel, "set_operation_message"):
                panel.set_operation_message(f"资产扫描失败：{exc}", error=True)

    def set_asset_project_dir(self, project_dir: Optional[str]) -> None:
        """Set the project root used to resolve portable asset paths."""
        import os
        raw = str(project_dir or "").strip()
        self._asset_project_dir = (
            os.path.abspath(os.path.expanduser(raw)) if raw else None
        )
        self._refresh_asset_dependency_panel()

    def set_asset_search_roots(
            self, roots: tuple[str, ...] | list[str], *, persist: bool = True
    ) -> None:
        """Set search roots and optionally persist them in viewer_state."""
        import os
        normalized = tuple(
            os.path.abspath(os.path.expanduser(str(root)))
            for root in roots if str(root).strip()
        )
        if normalized == self._asset_search_roots:
            panel = self.__dict__.get("_asset_dependency_panel")
            if panel is not None and hasattr(panel, "set_search_roots"):
                panel.set_search_roots(normalized)
            return
        self._asset_search_roots = normalized
        panel = self.__dict__.get("_asset_dependency_panel")
        if panel is not None and hasattr(panel, "set_search_roots"):
            panel.set_search_roots(normalized)
        if persist and self._node_graph is not None:
            viewer_state = dict(
                getattr(self._node_graph, "_viewer_state", {}) or {})
            viewer_state["asset_search_roots"] = list(normalized)
            self._node_graph._viewer_state = viewer_state
            self._publish_asset_graph_change()
            return
        self._refresh_asset_dependency_panel()

    def _on_asset_search_root_requested(self) -> None:
        """Add one offline-asset search root and persist the updated list."""
        from PyQt6.QtWidgets import QFileDialog

        start_dir = self._asset_search_roots[-1] if self._asset_search_roots else ""
        root = QFileDialog.getExistingDirectory(
            self, "选择资产搜索目录", start_dir)
        if not root:
            return
        self.set_asset_search_roots(
            self._asset_search_roots + (root,), persist=True)

    def _publish_asset_graph_change(self) -> None:
        """Persist an asset mutation and request the normal graph re-render."""
        self._emit_user_edit_once()
        graph_data = self.get_composite_data()
        if self._current_composite_id:
            self.composite_graph_changed.emit(
                self._current_composite_id, graph_data)
            self._last_published_graph_data = graph_data
            # AUD-9508: keep the generation gate in sync so the next request
            # evaluation doesn't consider this publish stale.
            graph = getattr(self, "_node_graph", None)
            if graph is not None:
                self._last_published_graph_generation = graph.generation
        self._refresh_asset_dependency_panel()
        self._evaluate_graph()

    def _on_asset_relink_requested(self, node_id: str, parameter: str) -> None:
        """Choose and safely relink one dependency through AssetRelinkService."""
        from PyQt6.QtWidgets import QFileDialog
        import os

        path, _filter = QFileDialog.getOpenFileName(
            self, "选择替换素材", "", "所有文件 (*)")
        if not path:
            return
        try:
            from src.features.composite.core.asset_dependency import (
                AssetRelinkError, AssetRelinkService,
            )
            service = AssetRelinkService(self._node_graph, self._undo_stack)
            result = service.apply(
                {(str(node_id), str(parameter)): path},
                project_dir=self._asset_project_dir,
                asset_search_roots=(os.path.dirname(path),) +
                self._asset_search_roots,
                store_relative_to_project=bool(self._asset_project_dir),
            )
        except (AssetRelinkError, OSError, TypeError, ValueError) as exc:
            logger.warning("Asset relink rejected: %s", exc)
            panel = self.__dict__.get("_asset_dependency_panel")
            if panel is not None and hasattr(panel, "set_operation_message"):
                panel.set_operation_message(f"重链接失败：{exc}", error=True)
            self.status_message.emit(f"资产重链接失败：{exc}")
            return
        self._publish_asset_graph_change()
        self.status_message.emit(f"已重链接 {result.changed_count} 项资产")

    def _on_asset_collect_requested(self) -> None:
        """Collect resolved dependencies into a user-selected directory."""
        from PyQt6.QtWidgets import QFileDialog

        target_dir = QFileDialog.getExistingDirectory(
            self, "选择资产收集目录", self._asset_project_dir or "")
        if not target_dir:
            return
        try:
            from src.features.composite.core.asset_dependency import (
                AssetCollectError, AssetCollectService,
            )
            service = AssetCollectService(self._node_graph, self._undo_stack)
            plan = service.plan(
                target_dir, project_dir=self._asset_project_dir,
                asset_search_roots=self._asset_search_roots,
                store_relative_to_project=bool(self._asset_project_dir),
            )
            result = service.apply(plan)
        except (AssetCollectError, OSError, TypeError, ValueError) as exc:
            logger.warning("Asset collection rejected: %s", exc)
            panel = self.__dict__.get("_asset_dependency_panel")
            if panel is not None and hasattr(panel, "set_operation_message"):
                panel.set_operation_message(f"资产收集失败：{exc}", error=True)
            self.status_message.emit(f"资产收集失败：{exc}")
            return
        self._publish_asset_graph_change()
        self.status_message.emit(
            f"已收集 {result.copied_count} 项资产，"
            f"复用 {result.reused_count} 项")

    def _selected_model3d_node(self) -> Optional[Any]:
        """Return the selected Model3D node, if the selection is compatible."""
        node_id = getattr(self, "_last_selected_node_id", None)
        graph = getattr(self, "_node_graph", None)
        if not node_id or graph is None:
            return None
        node = graph.get_node(node_id)
        return node if node is not None and node.node_type == "model3d" else None

    @staticmethod
    def _animation_clip_specs(raw_specs: object) -> list[dict]:
        """Normalize metadata clip descriptors for defensive UI consumption."""
        result: list[dict] = []
        if not isinstance(raw_specs, (list, tuple)):
            return result
        for raw in raw_specs:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name", ""))
            if not name:
                continue
            try:
                duration = max(0.0, float(raw.get("duration", 0.0)))
            except (TypeError, ValueError):
                duration = 0.0
            result.append({"name": name, "duration": duration})
        return result

    def _sync_import_report_for_selected_node(self) -> None:
        """Synchronize the import report with the selected Model3D node."""
        report_panel = getattr(self, "_import_report_panel", None)
        node = self._selected_model3d_node()
        if node is None:
            self._clear_model3d_auxiliary_panels()
            return
        source_path = str(node.parameters.get("model_path", "") or "")
        if (self._last_model_source_path
                and source_path != self._last_model_source_path):
            # Metadata from a different Model3D must not leak into this node.
            report = None
            import_status = ""
            import_phase = ""
            import_progress = 0.0
            import_resource_estimate = None
            import_probe = None
            import_diagnostics = ()
        else:
            report = getattr(self, "_last_model_import_report", None)
            # __new__-constructed page doubles in tests may not have run the
            # QWidget initializer; __dict__ avoids PyQt's RuntimeError for a
            # missing attribute on such an object.
            page_state = self.__dict__
            import_status = str(
                page_state.get("_last_model_import_status", "") or "")
            import_phase = str(
                page_state.get("_last_model_import_phase", "") or "")
            import_progress = float(
                page_state.get("_last_model_import_progress", 0.0) or 0.0)
            import_resource_estimate = page_state.get(
                "_last_model_import_resource_estimate")
            import_probe = page_state.get("_last_model_import_probe")
            import_diagnostics = page_state.get(
                "_last_model_runtime_diagnostics", ())
        if report_panel is not None:
            try:
                if hasattr(report_panel, "update_import_state"):
                    report_panel.update_import_state(
                        import_status, import_progress, import_phase)
                if hasattr(report_panel, "update_resource_estimate"):
                    report_panel.update_resource_estimate(import_resource_estimate)
                if hasattr(report_panel, "update_format_probe"):
                    report_panel.update_format_probe(import_probe)
                if hasattr(report_panel, "update_diagnostics"):
                    report_panel.update_diagnostics(import_diagnostics)
                if hasattr(report_panel, "update_summary"):
                    report_panel.update_summary(report)
                else:
                    report_panel.update_report(None)
            except (AttributeError, RuntimeError):
                pass
    def set_import_report_panel(self, panel) -> None:
        """Bind the optional read-only Model3D import report panel."""
        self._import_report_panel = panel
        if panel is not None:
            self._sync_import_report_for_selected_node()

    def set_asset_dependency_panel(self, panel) -> None:
        """Bind the optional asset dependency panel to the page boundary."""
        self._asset_dependency_panel = panel
        if panel is None:
            return
        if hasattr(panel, "refresh_requested"):
            panel.refresh_requested.connect(self._refresh_asset_dependency_panel)
        if hasattr(panel, "relink_requested"):
            panel.relink_requested.connect(self._on_asset_relink_requested)
        if hasattr(panel, "collect_requested"):
            panel.collect_requested.connect(self._on_asset_collect_requested)
        if hasattr(panel, "search_root_requested"):
            panel.search_root_requested.connect(
                self._on_asset_search_root_requested)
        if hasattr(panel, "set_search_roots"):
            panel.set_search_roots(self._asset_search_roots)
        self._refresh_asset_dependency_panel()

    def set_material_panel(self, panel) -> None:
        """Bind the optional per-material editor to Model3D parameters."""
        self._material_panel = panel
        if panel is None:
            return
        if hasattr(panel, "overrides_changed"):
            panel.overrides_changed.connect(
                self._on_material_panel_overrides_changed)
        self._sync_material_panel_for_selected_node()

    def _sync_material_panel_for_selected_node(self) -> None:
        """Show material summaries only for the selected, matching Model3D."""
        panel = self.__dict__.get("_material_panel")
        if panel is None:
            return
        node = self._selected_model3d_node()
        if node is None:
            panel.clear()
            return
        source_path = str(node.parameters.get("model_path", "") or "")
        if ((self._last_model_node_id and self._last_model_node_id != str(node.id))
                or (self._last_model_source_path
                    and source_path != self._last_model_source_path)):
            panel.clear()
            return
        panel.set_materials(
            list(getattr(self, "_last_model_materials", [])),
            str(node.parameters.get("material_overrides", "") or ""),
        )

    def _on_material_panel_overrides_changed(self, serialized: str) -> None:
        """Write material overrides through the normal undo/render path."""
        node = self._selected_model3d_node()
        if node is None:
            return
        self._on_parameter_changed(
            node.id, "material_overrides", str(serialized or ""))
        self._sync_material_panel_for_selected_node()

    def set_scene_outliner_panel(self, panel) -> None:
        """Bind the optional Model3D Outliner to the page state boundary."""
        self._scene_outliner_panel = panel
        if panel is None:
            return
        if hasattr(panel, "scene_node_selected"):
            panel.scene_node_selected.connect(
                self._on_scene_outliner_node_selected)
        if hasattr(panel, "visibility_changed"):
            panel.visibility_changed.connect(
                self._on_scene_outliner_visibility_changed)
        if hasattr(panel, "lock_changed"):
            panel.lock_changed.connect(self._on_scene_outliner_lock_changed)
        if hasattr(panel, "solo_changed"):
            panel.solo_changed.connect(self._on_scene_outliner_solo_changed)
        self._sync_scene_outliner_for_selected_node()

    def _sync_scene_outliner_for_selected_node(self) -> None:
        """Show hierarchy metadata only for the selected Model3D node."""
        panel = self.__dict__.get("_scene_outliner_panel")
        if panel is None:
            return
        node = self._selected_model3d_node()
        if node is None:
            panel.clear()
            return
        source_path = str(node.parameters.get("model_path", "") or "")
        if (self._last_model_node_id
                and self._last_model_node_id != str(node.id)):
            panel.clear()
            return
        if (self._last_model_source_path
                and source_path != self._last_model_source_path):
            panel.clear()
            return
        panel.set_hierarchy(
            self._last_model_hierarchy,
            self._last_model_hidden_mesh_ids,
            self._last_model_locked_mesh_ids,
            self._last_model_solo_mesh_id,
        )
        selected_scene_id = None
        if self._viewer is not None and hasattr(
                self._viewer, "get_selected_3d_scene_node"):
            selected_scene_id = self._viewer.get_selected_3d_scene_node()
        if selected_scene_id:
            panel.set_selected_node(selected_scene_id)

    def _scene_outliner_root_id(self) -> Optional[str]:
        hierarchy = self._last_model_hierarchy
        if not isinstance(hierarchy, dict):
            return None
        node_id = str(hierarchy.get("node_id", "") or "")
        return node_id or None

    def _on_scene_outliner_node_selected(self, scene_node_id: str) -> None:
        """Synchronize an Outliner selection with graph node and 3D outline."""
        node = self._selected_model3d_node()
        model_node_id = self._last_model_node_id or (
            str(node.id) if node is not None else "")
        if not model_node_id:
            return
        if node is None or str(node.id) != model_node_id:
            if self._node_editor.select_node(model_node_id):
                node = self._node_graph.get_node(model_node_id)
        if node is None:
            return
        target_id = str(scene_node_id or "")
        if target_id == self._scene_outliner_root_id():
            # The root represents the complete Model3D graph node in the ID
            # pass; child mesh IDs represent individual imported meshes.
            target_id = model_node_id
        if self._viewer is not None and hasattr(
                self._viewer, "set_selected_3d_scene_node"):
            self._viewer.set_selected_3d_scene_node(target_id or None)
        self._force_next_view_refresh = True
        self._evaluate_graph()

    def _on_scene_outliner_visibility_changed(self, scene_node_id: str,
                                               visible: bool) -> None:
        """Persist Outliner visibility through Model3D's normal undo path."""
        node = self._selected_model3d_node()
        if node is None:
            return
        hidden = {
            value.strip()
            for value in str(node.parameters.get("hidden_mesh_ids", "") or "").split(",")
            if value.strip()
        }
        scene_id = str(scene_node_id or "").strip()
        if not scene_id:
            return
        if visible:
            hidden.discard(scene_id)
        else:
            hidden.add(scene_id)
        self._on_parameter_changed(
            node.id, "hidden_mesh_ids", ",".join(sorted(hidden)))
        self._last_model_hidden_mesh_ids = tuple(sorted(hidden))
        self._sync_scene_outliner_for_selected_node()

    def _on_scene_outliner_lock_changed(self, scene_node_id: str,
                                        locked: bool) -> None:
        """Persist Outliner edit protection through the graph undo path."""
        node = self._selected_model3d_node()
        if node is None:
            return
        locked_ids = {
            value.strip()
            for value in str(node.parameters.get("locked_mesh_ids", "") or "").split(",")
            if value.strip()
        }
        scene_id = str(scene_node_id or "").strip()
        if not scene_id:
            return
        if locked:
            locked_ids.add(scene_id)
        else:
            locked_ids.discard(scene_id)
        self._on_parameter_changed(
            node.id, "locked_mesh_ids", ",".join(sorted(locked_ids)))
        self._last_model_locked_mesh_ids = tuple(sorted(locked_ids))
        self._sync_scene_outliner_for_selected_node()

    def _on_scene_outliner_solo_changed(self, scene_node_id: str) -> None:
        """Persist the single temporary Outliner solo target."""
        node = self._selected_model3d_node()
        if node is None:
            return
        solo_id = str(scene_node_id or "").strip()
        self._on_parameter_changed(node.id, "solo_mesh_id", solo_id)
        self._last_model_solo_mesh_id = solo_id
        self._sync_scene_outliner_for_selected_node()

    def _on_node_selected(self, node_id: str) -> None:
        """Handle node selection — update properties panel, thumbnail, and viewer handles."""
        self._last_selected_node_id = node_id
        node = self._node_graph.get_node(node_id)
        if self._viewer is not None and hasattr(
                self._viewer, "set_selected_3d_scene_node"):
            self._viewer.set_selected_3d_scene_node(None)
        if node and hasattr(self, '_properties_panel'):
            param_options = node.get_param_options() if hasattr(node, 'get_param_options') else {}
            param_defs = node.get_param_defs() if hasattr(node, 'get_param_defs') else {}
            # 同步当前视图模式给面板（Scene3D 开关据此高亮当前模式）。
            if hasattr(self._properties_panel, 'set_view_mode'):
                cur_vm = getattr(self._viewer, '_view_mode', '2d') if self._viewer else '2d'
                self._properties_panel.set_view_mode(cur_vm)
            self._properties_panel.set_node(
                node_id, node.node_type, node.node_title,
                node.parameters, param_options, param_defs,
                state=node._state.name.lower(),
                progress=node.progress,
                error_message=node.error_message,
                enabled=not node.disabled,
                node_color=getattr(node, 'header_color', '#666666'),
                read_only=_page_node_is_locked(self, node_id),
                # 传当前图像尺寸：让 Properties 面板在 pivot_mode="center" 时
                # 把 pivot_x/y 显示为真实图像中心（只读），与渲染实际行为一致。
                # 用 getattr 兜底：测试 / 部分初始化阶段可能还没建好这两个属性。
                image_size=(
                    getattr(self, '_last_texture_width', 0),
                    getattr(self, '_last_texture_height', 0),
                ),
            )
            # Update thumbnail manager
            if self._thumbnail_manager:
                self._thumbnail_manager.set_selected_node(node_id)

            # Update viewer tool handles (S5-12)
            if self._viewer:
                self._viewer.set_selected_node(node_id)
                # Resolve the node's primary input image size so geometry-
                # transform handles (transform/cornerpin) hug the actual
                # input image rather than the composite canvas.
                input_size = self._resolve_node_input_size(node)
                self._viewer.set_node_info(
                    node.node_type,
                    dict(node.parameters),
                    node.format if hasattr(node, 'format') else None,
                    input_size=input_size,
                    node_instance=node,
                    node_id=node_id,
                )
                # 视图模式（2D/3D）由用户用 Tab 键或 Scene3D 参数面板的开关手动
                # 切换，选中节点不再自动切模式（避免只想挪一下 Scene3D 就被切走）。
                # 3D Viewer context 独立于 Output 连接：Scene3D 是可编辑的
                # 3D 场景容器；Camera/Light/Geo/Model 只是该场景里的源对象。
                self._update_viewer3d_context_for_selection(node_id, node.node_type)
                if getattr(self._viewer, '_view_mode', '2d') == "3d":
                    self._force_next_view_refresh = True
                    self._evaluate_graph_now()
        # P2: refresh transport-bar keyframe markers for the newly selected node
        # (or clear them when the selection is empty). Runs for every selection
        # change, independent of the properties-panel branch above. Guarded so
        # test stand-ins (SimpleNamespace) that rebind _on_node_selected but
        # don't carry this helper don't blow up.
        _refresh_kf = getattr(self, 'refresh_transport_keyframe_markers', None)
        if callable(_refresh_kf):
            _refresh_kf()
        sync_report = getattr(
            self, "_sync_import_report_for_selected_node", None)
        if callable(sync_report):
            sync_report()
        sync_material = getattr(
            self, "_sync_material_panel_for_selected_node", None)
        if callable(sync_material):
            sync_material()

    def _resolve_node_input_size(self, node: Any) -> Optional[tuple[int, int]]:
        """Resolve the pixel size of a node's primary 'image' input.

        Used to size on-canvas transform/cornerpin handles to the input image
        (e.g. 1500×800) instead of the composite canvas. Source node types
        store their resolved size in different places (image_input uses
        ``_width``/``_height``, color_node uses ``_cached_width``/``_cached_height``,
        constant/image nodes use the ``width``/``height`` parameters), so we
        probe each. Returns None when no size is available (caller falls back
        to the composite texture size).
        """
        try:
            conns = self._node_graph.get_input_connections(node.id)
        except Exception:
            return None
        for c in conns:
            if c.dst_port_name != "image":
                continue
            src = self._node_graph.get_node(c.src_node_id)
            if src is None:
                continue
            # ``color_input`` is special: the live Color output texture and
            # its dimensions belong to the render snapshot, not the UI-side
            # node instance.  The latter deliberately remains a lightweight
            # graph model, so its ``_cached_width/_cached_height`` stay 0.
            # Falling through to the displayed output texture here made a
            # Transform before Merge use the *composite* dimensions for its
            # gizmo whenever the Color image was smaller than the canvas.
            if getattr(src, "node_type", "") == "color_input":
                color_w = int(getattr(self, "_last_color_width", 0) or 0)
                color_h = int(getattr(self, "_last_color_height", 0) or 0)
                if color_w > 0 and color_h > 0:
                    return (color_w, color_h)
            # Probe 1: instance attributes (image_input._width/_height,
            # color_node._cached_width/_cached_height).
            for w_attr, h_attr in (
                ("_width", "_height"),
                ("_cached_width", "_cached_height"),
            ):
                w = getattr(src, w_attr, None)
                h = getattr(src, h_attr, None)
                if isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0:
                    return (w, h)
            # Probe 2: parameters dict (constant_node / image params store
            # explicit width/height). VideoFrame uses video_width/video_height
            # for the same metadata.
            params = getattr(src, "parameters", {}) or {}
            for w_key, h_key in (
                    ("width", "height"),
                    ("video_width", "video_height")):
                w = params.get(w_key)
                h = params.get(h_key)
                if w and h and int(w) > 0 and int(h) > 0:
                    return (int(w), int(h))
        return None

    def _resolve_transform_interaction_size(self, node: Any) -> tuple[float, float]:
        """Return the selected Transform input dimensions for drag math.

        Transform handles live in the primary input's pixel space, while the
        viewer texture is the final composite canvas.  Keep all drag paths in
        the former space; the final canvas is only used for view↔canvas
        coordinate conversion.

        Text is a source node with no image input; its "input" is its own
        output canvas (the width/height parameters), so we return those so
        the handles size to the text layer rather than the composite canvas.
        """
        if getattr(node, "node_type", "") == "text":
            params = getattr(node, "parameters", {}) or {}
            w = int(params.get("width", 0)) or 0
            h = int(params.get("height", 0)) or 0
            if w > 0 and h > 0:
                return (float(w), float(h))
        input_size = self._resolve_node_input_size(node)
        if input_size is not None:
            return (float(input_size[0]), float(input_size[1]))
        return (
            float(max(1, getattr(self, "_last_texture_width", 0))),
            float(max(1, getattr(self, "_last_texture_height", 0))),
        )

    @staticmethod
    def _text_user_facing_params(params: dict, node: Any) -> dict:
        """Return user-facing transform params for a Text node's delta math.

        compute_transform_delta_params needs original params in the same space
        as the drag coordinates. For Text that's bbox-texture-local space:
        translate is the user offset (0-baselined), and a custom pivot is
        remapped from canvas to bbox-local space. 'center' pivot resolves
        correctly to bbox centre inside compute_transform_delta_params.
        """
        out = dict(params)
        if str(params.get("pivot_mode", "center")) == "custom":
            try:
                bx, by, bw, bh = node.text_handle_bbox()
                out["pivot_x"] = float(params.get("pivot_x", 0.0)) - bx
                out["pivot_y"] = float(params.get("pivot_y", 0.0)) - by
            except Exception:
                pass
        return out

    def _on_nodes_selected(self, node_ids: list) -> None:
        """Handle multi-node selection — update properties panel with common parameters."""
        self._clear_model3d_auxiliary_panels()
        if self._viewer is not None and hasattr(
                self._viewer, "set_selected_3d_scene_node"):
            self._viewer.set_selected_3d_scene_node(None)
        panel = getattr(self, "_scene_outliner_panel", None)
        if panel is not None:
            panel.clear()
        if not hasattr(self, '_properties_panel'):
            return

        # 收集所有选中节点的数据
        nodes_data = []
        for node_id in node_ids:
            node = self._node_graph.get_node(node_id)
            if node:
                param_options = node.get_param_options() if hasattr(node, 'get_param_options') else {}
                param_defs = node.get_param_defs() if hasattr(node, 'get_param_defs') else {}
                nodes_data.append({
                    'node_id': node_id,
                    'node_type': node.node_type,
                    'node_title': node.node_title,
                    'parameters': node.parameters,
                    'param_options': param_options,
                    'param_defs': param_defs,
                    'state': node._state.name.lower(),
                    'progress': node.progress,
                    'error_message': node.error_message,
                    'enabled': not node.disabled,
                    'node_color': getattr(node, 'header_color', '#666666'),
                    'read_only': _page_node_is_locked(self, node_id),
                })

        # 更新属性面板显示公共参数
        self._properties_panel.set_nodes(nodes_data)

        # 更新缩略图管理器（使用第一个节点）
        if self._thumbnail_manager and node_ids:
            self._thumbnail_manager.set_selected_node(node_ids[0])

    def _on_selection_cleared(self) -> None:
        if hasattr(self, '_properties_panel'):
            self._properties_panel.clear()
        self._clear_model3d_auxiliary_panels()
        if self._viewer is not None and hasattr(
                self._viewer, "set_selected_3d_scene_node"):
            self._viewer.set_selected_3d_scene_node(None)
        panel = getattr(self, "_scene_outliner_panel", None)
        if panel is not None:
            panel.clear()

    def _on_node_bypass_toggled(self, node_id: str) -> None:
        """Handle bypass toggle — update data model and re-evaluate."""
        if _page_node_is_locked(self, node_id):
            return
        # Find visual item to get current disabled state
        item = self._node_editor._node_items.get(node_id)
        if item:
            self._node_graph.set_node_disabled(node_id, item._disabled)
            # U-07：旁路/启停必须有撤销记录。toggle 语义是取反，
            # before/after 可由当前态直接推导。
            self._push_node_enabled_undo({node_id: item._disabled})
            self._evaluate_graph()

    def _on_nodes_bypass_batch_toggled(self, changes: dict) -> None:
        """多选旁路：一次求值 + 单条 group 撤销记录（U-07）。"""
        applied = {
            nid: state for nid, state in (changes or {}).items()
            if not _page_node_is_locked(self, nid)
        }
        if not applied:
            return
        for nid, state in applied.items():
            self._node_graph.set_node_disabled(nid, state)
        self._push_node_enabled_undo(applied)
        self._evaluate_graph()

    def _push_node_enabled_undo(self, changes: dict) -> None:
        """Record a node enable/disable change set as one undo action (U-07).

        ``changes`` maps node_id → new disabled state；toggle 语义下旧值
        即取反。undo/redo 同步 header 视觉、属性面板与求值。
        """
        before = {nid: (not state) for nid, state in changes.items()}
        after = dict(changes)

        def restore(data: dict) -> None:
            for nid, state in data["state"].items():
                if self._node_graph.get_node(nid) is None:
                    continue
                self._node_graph.set_node_disabled(nid, state)
                item = self._node_editor._node_items.get(nid)
                if item:
                    item.set_disabled(state)
                self._refresh_properties_panel(nid)
            self._evaluate_graph()

        count = len(after)
        description = "Toggle bypass" if count == 1 else f"Toggle bypass ×{count}"
        undo_stack = self.__dict__.get("_undo_stack")
        if undo_stack is None:
            return
        undo_stack.push(UndoAction(
            "node_enabled_change",
            {"before": before, "after": after},
            undo_fn=lambda data: restore({"state": data["before"]}),
            redo_fn=lambda data: restore({"state": data["after"]}),
            description=description,
        ))
        self._emit_user_edit_once()

    def _on_node_lock_changed(self, node_id: str, locked: bool) -> None:
        """Refresh selected properties when a canvas lock changes."""
        if self._properties_panel is None:
            return
        selected_ids = [
            nid for nid, item in self._node_editor._node_items.items()
            if item.isSelected()
        ]
        if node_id not in selected_ids:
            return
        if len(selected_ids) > 1:
            self._on_nodes_selected(selected_ids)
        else:
            self._refresh_properties_panel(node_id)

    # ── B4: canvas node rename ──

    def _apply_node_user_label(self, node_id: str, label) -> None:
        """Set a node's user_label and refresh every UI showing the name."""
        self._node_editor.set_node_user_label(node_id, label)
        self._update_minimap()
        self._refresh_properties_panel(node_id)

    def _on_node_rename_requested(self, node_id: str, new_label: str) -> None:
        """Commit a canvas rename as one undoable step (B4).

        Empty/whitespace text clears the custom label so display falls back
        to the auto number; undo restores the previous label (None included).
        """
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        old_label = getattr(node, "user_label", None)
        normalized = (new_label or "").strip() or None
        if normalized == old_label:
            return
        self._emit_user_edit_once()
        # Apply FIRST (the undo stack only records actions — push() never
        # executes redo), then record the step so undo/redo are symmetric.
        self._apply_node_user_label(node_id, normalized)
        self._undo_stack.push(UndoAction(
            "rename_node",
            {"node_id": node_id, "old": old_label, "new": normalized},
            undo_fn=lambda d: self._apply_node_user_label(d["node_id"], d["old"]),
            redo_fn=lambda d: self._apply_node_user_label(d["node_id"], d["new"]),
            description=f"Rename node to {normalized}" if normalized else "Clear node name",
        ))

    # ── C5: Read/Write version binning ──

    def _on_node_version_step_requested(self, node_id: str, delta: int) -> None:
        """[ / ] on a selected Read/Write node — reuse the canonical param
        commit path so undo/debounce/prefetch behave like an edit."""
        from src.features.composite.utils import path_versioning
        node = self._node_graph.get_node(node_id)
        if node is None or node.node_type not in ("image_input", "file_output"):
            return
        if _page_node_is_locked(self, node_id):
            return
        path = str(node.parameters.get("file_path", "") or "")
        new_path = path_versioning.stepped_version(path, int(delta))
        if not new_path or new_path == path:
            return
        self._on_parameter_changed(node_id, "file_path", new_path)
        # The generic param path doesn't refresh the properties panel —
        # the file_path row would keep showing the previous version.
        self._refresh_properties_panel(node_id)

    def _on_set_all_reads_latest(self) -> None:
        """Batch version-up: every Read node switches to its highest
        existing version in one undo group."""
        from src.features.composite.utils import path_versioning
        targets = []
        for node in self._node_graph.nodes.values():
            if node.node_type != "image_input":
                continue
            if _page_node_is_locked(self, node.id):
                continue
            path = str(node.parameters.get("file_path", "") or "")
            latest = path_versioning.latest_version(path)
            if latest and latest != path:
                targets.append((node.id, path, latest))
        if not targets:
            return
        self._emit_user_edit_once()
        self._undo_stack.begin_group()
        for node_id, old_path, new_path in targets:
            # UndoStack.push never executes redo — APPLY the new path first
            # (B4-rename-class bug: record-only made the menu item a no-op
            # until an accidental redo "applied" it later).
            self._redo_param({
                "node_id": node_id, "param": "file_path", "new": new_path})
            self._undo_stack.push(create_param_change_action(
                node_id=node_id,
                param_name="file_path",
                old_value=old_path,
                new_value=new_path,
                undo_fn=self._undo_param,
                redo_fn=self._redo_param,
                merge_window_ms=0,
            ))
        self._undo_stack.end_group("Set All Reads to Latest Version")
        self._evaluate_graph()

    # ── A1: Tracker node analyze / bake ──────────────────────────────

    def _on_tracker_analyze_requested(self, node_id: str) -> None:
        """Analyze button: resolve the upstream file source and start the
        QThread analysis (progress surfaces through the node's progress)."""
        from src.features.composite.render.tracker_analyze_worker import (
            TrackerAnalyzeWorker)

        node = self._node_graph.get_node(node_id)
        if node is None or node.node_type != "tracker":
            return
        if getattr(self, "_tracker_workers", None) and node_id in self._tracker_workers:
            return  # already analyzing

        # The tracker's frame source is the upstream Read node's file.
        media_path = ""
        for conn in self._node_graph.get_input_connections(node_id):
            upstream = self._node_graph.get_node(conn.src_node_id)
            if upstream is not None and upstream.node_type == "image_input":
                media_path = str(
                    upstream.parameters.get("file_path", "") or "")
                break
        if not media_path:
            self._show_tracker_message(
                "Tracker needs an upstream Read node with a file path.")
            return

        # Frame range: analyze the whole source; the sampler clamps.
        worker = TrackerAnalyzeWorker(
            node_id=node_id,
            media_path=media_path,
            params=dict(node.parameters),
            frame_start=0,
            frame_end=int(node.parameters.get("reference_frame", 0) or 0) + 100000,
        )
        worker.progress.connect(self._on_tracker_analyze_progress)
        worker.finished_ok.connect(self._on_tracker_analyze_finished)
        worker.failed.connect(self._on_tracker_analyze_failed)
        if not hasattr(self, "_tracker_workers"):
            self._tracker_workers = {}
        self._tracker_workers[node_id] = worker
        node.progress = 0.0
        worker.start()

    def _show_tracker_message(self, text: str) -> None:
        logger.warning("tracker: %s", text)

    def _on_tracker_analyze_progress(self, node_id: str, current: int,
                                     total: int) -> None:
        node = self._node_graph.get_node(node_id)
        if node is None or total <= 0:
            return
        node.progress = max(0.0, min(1.0, float(current) / float(total)))
        self._update_node_item_state_by_id(node_id)

    def _on_tracker_analyze_finished(self, node_id: str, payload: dict) -> None:
        worker = getattr(self, "_tracker_workers", {}).pop(node_id, None)
        if worker is not None:
            worker.wait(2000)
            worker.deleteLater()
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        from src.features.composite.nodes.tracker_node import encode_track_data
        node.set_parameter(
            "track_data", encode_track_data(payload))
        node.progress = 1.0
        self._refresh_properties_panel(node_id)
        self._evaluate_graph()

    def _on_tracker_analyze_failed(self, node_id: str, error: str) -> None:
        worker = getattr(self, "_tracker_workers", {}).pop(node_id, None)
        if worker is not None:
            # The failed signal is queued from the worker thread — give the
            # run() loop a moment to exit before dropping the last reference
            # (a destroyed-while-running QThread can abort the process).
            worker.wait(2000)
            worker.deleteLater()
        node = self._node_graph.get_node(node_id)
        if node is not None:
            node.progress = 0.0
        # AUD-8508: 取消哨兵串与生产方共用常量，不再散落魔法字符串
        from src.features.composite.render.tracker_analyze_worker import (
            TRACKER_ANALYZE_CANCELLED)
        if error == TRACKER_ANALYZE_CANCELLED:
            self._show_tracker_message("Tracker analyze cancelled.")
        else:
            self._show_tracker_message(f"Tracker analyze failed: {error}")

    def _on_tracker_import_color_requested(self, node_id: str) -> None:
        """A1.3: import a Color-page tracking result into this tracker.

        Uses the Color feature's facade (the sanctioned cross-page
        boundary).  The binding with the most tracked frames wins; media
        geometry already stored on the tracker is preserved.
        """

        node = self._node_graph.get_node(node_id)
        if node is None or node.node_type != "tracker":
            return
        facade = getattr(self.window(), "color_facade", None)
        export = None
        if facade is not None and hasattr(facade, "export_tracking_bindings"):
            try:
                export = facade.export_tracking_bindings()
            except Exception:
                logger.exception("tracker: color export failed")
        if not export:
            self._show_tracker_message(
                "No Color-page tracking results available.")
            return
        best = max(
            export,
            key=lambda e: len(e.get("transforms") or {}),
        )
        existing = node.get_track_data() or {}
        payload = dict(existing)
        payload.update({
            "mode": best.get("mode", "translation"),
            "reference_frame": best.get("reference_frame", 0),
            "transforms": best.get("transforms", {}),
        })
        # Only trust real media geometry — a zero size would silently turn
        # the CornerPin bake's normalization into a division by 1.0.
        if float(best.get("width") or 0) > 0:
            payload["width"] = float(best["width"])
        if float(best.get("height") or 0) > 0:
            payload["height"] = float(best["height"])
        payload["imported_from_color"] = {
            "binding_id": best.get("binding_id", ""),
            "shape_name": best.get("shape_name", ""),
            "clip_id": best.get("clip_id", ""),
        }
        from src.features.composite.nodes.tracker_node import encode_track_data
        old_value = node.parameters.get("track_data", "")
        new_value = encode_track_data(payload)
        self._emit_user_edit_once()
        # Apply first — push() only records, it never executes redo.
        self._apply_tracker_track_data(node_id, new_value)
        self._undo_stack.push(UndoAction(
            "tracker_import",
            {"node_id": node_id, "old": old_value, "new": new_value},
            undo_fn=lambda d: self._apply_tracker_track_data(
                d["node_id"], d["old"]),
            redo_fn=lambda d: self._apply_tracker_track_data(
                d["node_id"], d["new"]),
            description="Import tracking from Color page",
        ))

    def _apply_tracker_track_data(self, node_id: str, raw: str) -> None:
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        node.set_parameter("track_data", raw)
        self._refresh_properties_panel(node_id)
        self._evaluate_graph()

    def _on_tracker_stabilize_requested(self, node_id: str) -> None:
        """A2: bake smoothed-inverted motion to the downstream Transform.

        Stabilize = inverse of the smoothed camera motion.  The window
        comes from the tracker's ``stabilize_window`` parameter; rotation
        and scale are included when the analysis mode is trs.
        """
        import copy as _copy

        from src.shared.tracking.stabilize import stabilize_transforms
        from src.shared.keyframe import Keyframe, KeyframeTrack

        tracker = self._node_graph.get_node(node_id)
        if tracker is None or tracker.node_type != "tracker":
            return
        track = tracker.get_track_data()
        if not track or not track.get("transforms"):
            self._show_tracker_message("Nothing to stabilize — run Analyze first.")
            return
        target = self._find_downstream_node(node_id, "transform")
        if target is None:
            self._show_tracker_message("No downstream transform node connected.")
            return

        try:
            window = max(1, int(tracker.parameters.get("stabilize_window", 15) or 1))
        except (TypeError, ValueError):
            window = 15
        mode = str(track.get("mode", "translation"))
        # WEAK (hold-last estimates) and MANUAL (user corrections) carry a
        # usable transform — only FAILED frames are unusable. Filtering to
        # valid-only would drop exactly the hand-fixed frames.
        valid = {f: e for f, e in (track.get("transforms") or {}).items()
                 if str(e.get("status", "valid")) != "failed"}
        try:
            stabilized = stabilize_transforms(
                valid, window=window,
                include_rotation_scale=(mode == "trs"))
        except Exception as exc:
            # NaN/singular trajectories must not kill the UI slot.
            logger.warning("tracker stabilize failed: %s", exc)
            self._show_tracker_message(
                "Stabilize failed — the trajectory contains invalid frames.")
            return

        target_pdefs = target.get_param_defs()

        def _track(name, values):
            t = KeyframeTrack(param_name=name)
            pdef = target_pdefs.get(name)
            if pdef is not None:
                t.min_val = float(pdef.min_val)
                t.max_val = float(pdef.max_val)
            for frame, value in values:
                t.add_keyframe(Keyframe(frame=frame, value=value))
            return t

        frames = sorted(int(f) for f in stabilized.keys())
        new_tracks = {
            "translate_x": _track("translate_x",
                                  [(f, stabilized[str(f)]["tx"]) for f in frames]),
            "translate_y": _track("translate_y",
                                  [(f, stabilized[str(f)]["ty"]) for f in frames]),
        }
        if mode == "trs":
            new_tracks["rotation"] = _track(
                "rotation", [(f, stabilized[str(f)]["rotation"]) for f in frames])
            new_tracks["scale_x"] = _track(
                "scale_x", [(f, stabilized[str(f)]["scale_x"]) for f in frames])
            new_tracks["scale_y"] = _track(
                "scale_y", [(f, stabilized[str(f)]["scale_y"]) for f in frames])

        def _apply(tracks_data: dict) -> None:
            target.keyframe_tracks = _copy.deepcopy(tracks_data)
            target.param_revision = getattr(target, "param_revision", 0) + 1
            target.bump_output_revision()
            self._refresh_properties_panel(target.id)
            self._evaluate_graph()

        # Serialize through the node API (bounds, expression mutex), then
        # capture the merged state for redo. Old state captured BEFORE.
        old_tracks = _copy.deepcopy(target.keyframe_tracks)
        for name, track_obj in new_tracks.items():
            target.set_keyframe_track(name, track_obj)
        merged = _copy.deepcopy(target.keyframe_tracks)

        self._emit_user_edit_once()
        self._undo_stack.push(UndoAction(
            "tracker_stabilize",
            {"target_id": target.id, "old": old_tracks, "new": merged},
            undo_fn=lambda d: _apply(d["old"]),
            redo_fn=lambda d: _apply(d["new"]),
            description="Bake stabilize to transform",
        ))
        _apply(merged)

    def _on_tracker_cancel_requested(self, node_id: str) -> None:
        worker = getattr(self, "_tracker_workers", {}).get(node_id)
        if worker is not None:
            worker.request_cancel()

    def _find_downstream_node(self, node_id: str, node_type: str):
        """First downstream node of ``node_type`` (A1 bake target).

        BFS through passthrough hops (Tracker → Dot → Transform resolves
        instead of reporting "not connected").
        """
        from collections import deque
        visited = {node_id}
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            for conn in self._node_graph.get_output_connections(current):
                nxt = conn.dst_node_id
                if nxt in visited:
                    continue
                visited.add(nxt)
                target = self._node_graph.get_node(nxt)
                if target is None:
                    continue
                if target.node_type == node_type:
                    return target
                if target.node_type in ("dot", "tracker"):
                    queue.append(nxt)
        return None

    def _on_tracker_bake_requested(self, node_id: str, target_kind: str) -> None:
        """Bake tracked frames into keyframes on a downstream node.

        transform:  translate_x/y (pixels) + rotation + scale for trs mode
        cornerpin:  normalized to/from corner positions from the planar
                    matrices
        One undo step per bake (captures the target's previous tracks).
        """
        import copy as _copy

        from src.shared.keyframe import Keyframe, KeyframeTrack

        tracker = self._node_graph.get_node(node_id)
        if tracker is None or tracker.node_type != "tracker":
            return
        track = tracker.get_track_data()
        if not track or not track.get("transforms"):
            self._show_tracker_message("Nothing to bake — run Analyze first.")
            return
        target_node_type = ("cornerpin" if target_kind == "cornerpin"
                            else "transform")
        target = self._find_downstream_node(node_id, target_node_type)
        if target is None:
            self._show_tracker_message(
                f"No downstream {target_node_type} node connected.")
            return

        transforms = track.get("transforms") or {}
        src_w_raw = float(track.get("width") or 0)
        src_h_raw = float(track.get("height") or 0)
        if target_node_type == "cornerpin" and (src_w_raw <= 0 or src_h_raw <= 0):
            self._show_tracker_message(
                "Track data has no source size — cannot bake to CornerPin.")
            return
        src_w = src_w_raw or 1.0
        src_h = src_h_raw or 1.0
        mode = str(track.get("mode", "translation"))

        def _valid_frames():
            # WEAK/MANUAL frames hold usable transforms (see the stabilize
            # filter above) — only FAILED is excluded.
            return sorted(
                int(f) for f, e in transforms.items()
                if str(e.get("status", "valid")) != "failed")

        target_pdefs = target.get_param_defs()

        def _track(name, values):
            t = KeyframeTrack(param_name=name)
            # get_value clamps to [min_val, max_val] — take the real range
            # from the target's ParamDef so pixel translations survive.
            pdef = target_pdefs.get(name)
            if pdef is not None:
                t.min_val = float(pdef.min_val)
                t.max_val = float(pdef.max_val)
            for frame, value in values:
                t.add_keyframe(Keyframe(frame=frame, value=value))
            return t

        old_tracks = _copy.deepcopy(target.keyframe_tracks)
        new_tracks: dict = {}

        if target_node_type == "transform":
            tx, ty, rot, sx, sy = [], [], [], [], []
            for f in _valid_frames():
                e = transforms[str(f)]
                tx.append((f, float(e.get("tx", 0.0))))
                ty.append((f, float(e.get("ty", 0.0))))
                if mode == "trs":
                    rot.append((f, float(e.get("rotation", 0.0))))
                    sx.append((f, float(e.get("scale_x", 1.0))))
                    sy.append((f, float(e.get("scale_y", 1.0))))
            new_tracks["translate_x"] = _track("translate_x", tx)
            new_tracks["translate_y"] = _track("translate_y", ty)
            if mode == "trs":
                new_tracks["rotation"] = _track("rotation", rot)
                new_tracks["scale_x"] = _track("scale_x", sx)
                new_tracks["scale_y"] = _track("scale_y", sy)
        else:  # cornerpin
            to_keys = ("to1", "to2", "to3", "to4")
            from_keys = ("from1", "from2", "from3", "from4")
            # reference corners = full source frame corners (pixels)
            corners_ref_px = [(0.0, 0.0), (src_w, 0.0),
                              (src_w, src_h), (0.0, src_h)]
            to_tracks = {f"{k}_x": [] for k in to_keys}
            to_tracks.update({f"{k}_y": [] for k in to_keys})
            from_tracks = {f"{k}_x": [] for k in from_keys}
            from_tracks.update({f"{k}_y": [] for k in from_keys})
            for f in _valid_frames():
                e = transforms[str(f)]
                matrix = e.get("matrix")
                if not matrix or len(matrix) != 3:
                    continue
                # Compute ALL corner projections first; a singular row (w≈0)
                # must skip the WHOLE frame, not leave earlier corners of
                # this frame in some tracks and not others (per-channel
                # keyframe sets would desync and tear the plate).
                projected = []
                placed = True
                for (cx, cy), tk, fk in zip(corners_ref_px, to_keys, from_keys):
                    w_row = (matrix[2][0] * cx + matrix[2][1] * cy
                             + matrix[2][2])
                    if abs(w_row) < 1e-9:
                        placed = False
                        break
                    px = matrix[0][0] * cx + matrix[0][1] * cy + matrix[0][2]
                    py = matrix[1][0] * cx + matrix[1][1] * cy + matrix[1][2]
                    projected.append((tk, fk, px / src_w, py / src_h,
                                      cx / src_w, cy / src_h))
                if not placed:
                    continue
                for tk, fk, nx, ny, fx, fy in projected:
                    to_tracks[f"{tk}_x"].append((f, nx))
                    to_tracks[f"{tk}_y"].append((f, ny))
                    from_tracks[f"{fk}_x"].append((f, fx))
                    from_tracks[f"{fk}_y"].append((f, fy))
            for name, values in to_tracks.items():
                new_tracks[name] = _track(name, values)
            for name, values in from_tracks.items():
                new_tracks[name] = _track(name, values)

        new_tracks = {k: t for k, t in new_tracks.items() if t.keyframes}
        if not new_tracks:
            self._show_tracker_message("No valid tracked frames to bake.")
            return

        def _apply(tracks_data: dict) -> None:
            # keyframe_tracks holds serialized track dicts — restore the
            # captured snapshot wholesale and bump revision.
            target.keyframe_tracks = _copy.deepcopy(tracks_data)
            target.param_revision = getattr(target, "param_revision", 0) + 1
            target.bump_output_revision()
            self._refresh_properties_panel(target.id)
            self._evaluate_graph()

        # Serialize through the node API (expression mutex, revision, bounds).
        for name, track_obj in new_tracks.items():
            target.set_keyframe_track(name, track_obj)

        merged = _copy.deepcopy(target.keyframe_tracks)

        self._emit_user_edit_once()
        self._undo_stack.push(UndoAction(
            "tracker_bake",
            {"target_id": target.id, "old": old_tracks, "new": merged},
            undo_fn=lambda d: _apply(d["old"]),
            redo_fn=lambda d: _apply(d["new"]),
            description=f"Bake tracker to {target_node_type}",
        ))
        _apply(merged)

    def _on_nodes_moved(self, node_ids: list, old_positions: list) -> None:
        """Handle node move — push undo with old/new positions."""
        new_positions = []
        valid_ids = []
        valid_old = []
        for nid, old_pos in zip(node_ids, old_positions):
            item = self._node_editor._node_items.get(nid)
            if item:
                new_pos = (item.pos().x(), item.pos().y())
                new_positions.append(new_pos)
                valid_ids.append(nid)
                valid_old.append(old_pos)

        if not valid_ids:
            return

        # Use factory function to create mergeable move_nodes action
        action = create_move_nodes_action(
            node_ids=valid_ids,
            old_positions=valid_old,
            new_positions=new_positions,
            undo_fn=lambda d: self._undo_move(d),
            redo_fn=lambda d: self._redo_move(d),
            merge_window_ms=500,
        )
        self._undo_stack.push(action)

    def _undo_move(self, data) -> None:
        for nid, pos in zip(data["node_ids"], data["old_positions"]):
            item = self._node_editor._node_items.get(nid)
            if item:
                item.setPos(*pos)
            node = self._node_graph.get_node(nid)
            if node:
                node.position = pos

    def _redo_move(self, data) -> None:
        for nid, pos in zip(data["node_ids"], data["new_positions"]):
            item = self._node_editor._node_items.get(nid)
            if item:
                item.setPos(*pos)
            node = self._node_graph.get_node(nid)
            if node:
                node.position = pos

    # ── Backdrop Undo ──

    def _on_backdrop_created(self, backdrop_id: str, data: dict) -> None:
        """Register an undo step for a newly created backdrop."""
        self._undo_stack.push(UndoAction(
            "backdrop_create",
            {"id": backdrop_id, "data": dict(data)},
            undo_fn=lambda d: self._remove_backdrop_for_undo(d),
            redo_fn=lambda d: self._restore_backdrop_for_undo(d),
            description="Add backdrop",
        ))

    def _on_backdrop_deleted(self, backdrop_id: str, data: dict) -> None:
        """Register an undo step for a deleted backdrop."""
        self._undo_stack.push(UndoAction(
            "backdrop_delete",
            {"id": backdrop_id, "data": dict(data)},
            undo_fn=lambda d: self._restore_backdrop_for_undo(d),
            redo_fn=lambda d: self._remove_backdrop_for_undo(d),
            description="Delete backdrop",
        ))

    def _on_backdrops_deleted(self, backdrop_ids: list, data_map: dict) -> None:
        """Register a single undo step for a batch backdrop deletion."""
        self._undo_stack.push(UndoAction(
            "backdrops_delete",
            {"ids": list(backdrop_ids),
             "data": {k: dict(v) for k, v in data_map.items()}},
            undo_fn=lambda d: self._restore_backdrops_batch(d),
            redo_fn=lambda d: self._remove_backdrops_batch(d),
            description=f"Delete {len(backdrop_ids)} backdrop(s)",
        ))

    def _on_backdrop_modified(self, backdrop_id: str, old_data: dict,
                              new_data: dict, node_moves: list) -> None:
        """Register one grouped undo step for a backdrop edit.

        Move/resize/edit/color/z changes swap the backdrop state; if the edit
        was a move that dragged contained nodes, their position change is
        bundled into the same group so one Ctrl+Z restores everything.
        """
        self._undo_stack.begin_group()
        self._undo_stack.push(UndoAction(
            "backdrop_modify",
            {"backdrop_id": backdrop_id, "old": dict(old_data), "new": dict(new_data)},
            undo_fn=lambda d: self._apply_backdrop_state(d["backdrop_id"], d["old"]),
            redo_fn=lambda d: self._apply_backdrop_state(d["backdrop_id"], d["new"]),
            description="Modify backdrop",
        ))
        if node_moves:
            self._undo_stack.push(create_move_nodes_action(
                node_ids=[m[0] for m in node_moves],
                old_positions=[m[1] for m in node_moves],
                new_positions=[m[2] for m in node_moves],
                undo_fn=lambda d: self._undo_move(d),
                redo_fn=lambda d: self._redo_move(d),
                merge_window_ms=0,
            ))
        self._undo_stack.end_group("Modify backdrop")

    def _restore_backdrop_for_undo(self, data: dict) -> None:
        """Re-create a backdrop visual from serialized data (undo/redo helper)."""
        self._node_editor.add_backdrop_visual(dict(data["data"]))

    def _remove_backdrop_for_undo(self, data: dict) -> None:
        """Remove a backdrop visual without re-emitting signals (undo/redo helper)."""
        self._node_editor.remove_backdrop_visual(data["id"], emit_signal=False)

    def _restore_backdrops_batch(self, data: dict) -> None:
        for bid in data["ids"]:
            if bid in data["data"]:
                self._node_editor.add_backdrop_visual(dict(data["data"][bid]))

    def _remove_backdrops_batch(self, data: dict) -> None:
        for bid in data["ids"]:
            self._node_editor.remove_backdrop_visual(bid, emit_signal=False)

    def _apply_backdrop_state(self, backdrop_id: str, state: dict) -> None:
        """Restore a backdrop's state and re-sync the graph (undo/redo helper)."""
        item = self._node_editor._backdrop_items.get(backdrop_id)
        if item is not None:
            item.apply_state(state)
            self._node_editor._sync_backdrop_to_graph(item)

    # ── Sticky note undo integration (mirrors backdrop) ──

    def _on_sticky_note_created(self, note_id: str, data: dict) -> None:
        """Register an undo step for a newly created sticky note."""
        self._undo_stack.push(UndoAction(
            "sticky_create",
            {"id": note_id, "data": dict(data)},
            undo_fn=lambda d: self._remove_sticky_note_for_undo(d),
            redo_fn=lambda d: self._restore_sticky_note_for_undo(d),
            description="Add sticky note",
        ))

    def _on_sticky_note_deleted(self, note_id: str, data: dict) -> None:
        """Register an undo step for a deleted sticky note."""
        self._undo_stack.push(UndoAction(
            "sticky_delete",
            {"id": note_id, "data": dict(data)},
            undo_fn=lambda d: self._restore_sticky_note_for_undo(d),
            redo_fn=lambda d: self._remove_sticky_note_for_undo(d),
            description="Delete sticky note",
        ))

    def _on_sticky_notes_deleted(self, note_ids: list, data_map: dict) -> None:
        """Register a single undo step for a batch sticky note deletion."""
        self._undo_stack.push(UndoAction(
            "stickies_delete",
            {"ids": list(note_ids),
             "data": {k: dict(v) for k, v in data_map.items()}},
            undo_fn=lambda d: self._restore_sticky_notes_batch(d),
            redo_fn=lambda d: self._remove_sticky_notes_batch(d),
            description=f"Delete {len(note_ids)} sticky note(s)",
        ))

    def _on_sticky_note_modified(self, note_id: str, old_data: dict,
                                 new_data: dict) -> None:
        """Register one undo step for a sticky note edit (move/resize/edit/color/z)."""
        self._undo_stack.push(UndoAction(
            "sticky_modify",
            {"note_id": note_id, "old": dict(old_data), "new": dict(new_data)},
            undo_fn=lambda d: self._apply_sticky_note_state(d["note_id"], d["old"]),
            redo_fn=lambda d: self._apply_sticky_note_state(d["note_id"], d["new"]),
            description="Modify sticky note",
        ))

    def _restore_sticky_note_for_undo(self, data: dict) -> None:
        """Re-create a sticky note visual from serialized data (undo/redo helper)."""
        self._node_editor.add_sticky_note_visual(dict(data["data"]))

    def _remove_sticky_note_for_undo(self, data: dict) -> None:
        """Remove a sticky note visual without re-emitting signals (undo/redo helper)."""
        self._node_editor.remove_sticky_note_visual(data["id"], emit_signal=False)

    def _restore_sticky_notes_batch(self, data: dict) -> None:
        for nid in data["ids"]:
            if nid in data["data"]:
                self._node_editor.add_sticky_note_visual(dict(data["data"][nid]))

    def _remove_sticky_notes_batch(self, data: dict) -> None:
        for nid in data["ids"]:
            self._node_editor.remove_sticky_note_visual(nid, emit_signal=False)

    def _apply_sticky_note_state(self, note_id: str, state: dict) -> None:
        """Restore a sticky note's state and re-sync the graph (undo/redo helper)."""
        item = self._node_editor._sticky_note_items.get(note_id)
        if item is not None:
            item.apply_state(state)
            self._node_editor._sync_sticky_note_to_graph(item)

    def _evaluate_graph(self) -> None:
        """Request graph evaluation (debounced).

        Multiple rapid calls (e.g., from add_node + auto_connect)
        are coalesced into a single render submission after 16ms.
        Use _evaluate_graph_now() for cases that need immediate evaluation.
        """
        if not self._eval_debounce_timer.isActive():
            self._eval_debounce_timer.start()

    def _emit_video_node_frame_ready(self) -> None:
        """VideoContext 单例帧就绪回调的目标（经弱引用蹦床调用）。

        由解码 worker 线程调用；跨线程 emit 经自动队列连接回 UI 线程。
        页面 C++ 已亡而包装仍在时，emit 抛 RuntimeError 由
        VideoContext 捕获降级——不会段错误（见 __init__ 注册处注释）。
        """
        self._video_node_frame_ready.emit()

    def _on_video_node_frame_ready(self) -> None:
        """视频节点后台解码完成（持帧预览路径）→ 重求值上屏就绪帧。

        立即求值（跳过 16ms 防抖）：这条回环在每个显示帧的关键路径上，
        防抖会让播放中解码完成的帧多等一拍才上屏；渲染线程对
        EVALUATE_GRAPH 本身有单飞/合并，不会因高频提交堆积。
        """
        if self._renderer is None:
            return
        if getattr(self, "_drive_active", False):
            # 渲染线程自驱中：解码结果由驱动循环直接消费，无需回 UI
            # 触发补拍（这正是自驱消灭的那个双往返回路）。
            return
        self._evaluate_graph_now()

    def _evaluate_graph_now(self) -> None:
        """Force immediate graph evaluation (skips debounce)."""
        self._eval_debounce_timer.stop()
        self._do_evaluate_graph()

    def _current_keyframe_frame(self) -> int:
        """Resolve the composition-local frame used to animate keyframed params.

        P8 (A-5): all preview snapshot() calls pass this so animated params
        interpolate at the right frame, matching the export loop (which
        iterates frames explicitly). Precedence:

          1. runtime_context.clip_local_frame (Comp Clip in an Edit timeline)
          2. runtime_context.timeline_frame   (timeline-bound independent comp)
          3. _composite_current_frame         (Keyframes panel / independent comp)
          4. 0

        P0-3: use explicit ``is not None`` checks so frame 0 is honoured
        instead of falling through (the old ``if local:`` treated 0 as False).
        """
        rc = self._runtime_context
        if rc is not None:
            local = getattr(rc, 'clip_local_frame', None)
            if local is not None:
                return int(local)
            tf = getattr(rc, 'timeline_frame', None)
            if tf is not None:
                return int(tf)
        return int(getattr(self, '_composite_current_frame', 0) or 0)

    def set_composite_current_frame(self, frame: int) -> None:
        """Set the current composition frame (Keyframes/Spline panel / playhead).

        P0-1: a frame change must (a) update the authoritative frame, (b) sync
        the Keyframes/Spline/Properties UI so all playheads move together, and
        (c) trigger a re-evaluation so the viewer shows the animated frame
        immediately. A guard prevents the panel write-backs from re-emitting.

        Timeline-backed comps: the render frame comes from the runtime context
        (``clip_local_frame``), not from ``_composite_current_frame`` — so a
        panel seek must go through the same transport delegate the viewport
        bar uses (EditPlaybackController.seek). The controller's frame echo
        returns via ``set_timeline_clip_context`` → ``_apply_composite_frame``
        (local-only, never re-seeks the transport → loop-free while playing).
        """
        frame = int(frame or 0)
        if self._syncing_composite_frame:
            return
        transport = getattr(self, '_keyframes_transport', None)
        if transport is not None and hasattr(transport, 'seek'):
            if frame == self._composite_current_frame:
                return
            try:
                transport.seek(frame)
                # The transport echo (_apply_composite_frame via the runtime
                # context update) performs the UI sync + re-render.
                return
            except Exception:
                self._keyframes_transport = None  # broken delegate → fallback
        self._apply_composite_frame(frame)

    def _apply_composite_frame(self, frame: int) -> None:
        """Apply a frame change locally: authoritative value + UI sync + render.

        Used by both the fallback seek path and the timeline echo path
        (``set_timeline_clip_context``). Never touches the transport delegate,
        so playback ticks and seek echoes cannot bounce back into it.
        """
        frame = int(frame or 0)
        if self._syncing_composite_frame:
            return
        changed = (frame != self._composite_current_frame)
        self._composite_current_frame = frame
        if not changed:
            return

        # 播放中用户 seek：重锚驱动基准并令渲染线程退出当前驱动循环，
        # 随即提交新求值（携带新基准的 playback_drive）让驱动立即重启。
        if getattr(self, "_drive_active", False) and self._comp_playing:
            import time as _t
            self._comp_play_start_time = _t.perf_counter()
            self._comp_play_start_frame = frame
            if self._renderer is not None:
                try:
                    self._renderer.stop_playback_drive()
                except Exception:
                    pass
            self._evaluate_graph()
            self._refresh_viewer_handles_for_frame()
            try:
                self.composite_frame_changed.emit(frame)
            except RuntimeError:
                pass
            return

        self._syncing_composite_frame = True
        try:
            # Keep the Keyframes panel's range aligned with the comp clip
            # (cheap: the panel only re-fits when the range actually changes).
            self._sync_keyframes_panel_frame_range()
            # Sync the three animation UI surfaces without re-emitting.
            props = getattr(self, '_properties_panel', None)
            if props is not None:
                try:
                    props.set_timeline_state(
                        self._total_frames_for_node(), frame)
                except Exception:
                    pass
            kf_panel = getattr(self, '_keyframes_panel', None)
            if kf_panel is not None and hasattr(kf_panel, 'set_current_frame'):
                try:
                    kf_panel.set_current_frame(frame)
                except Exception:
                    pass
            spline_panel = getattr(self, '_spline_panel', None)
            if spline_panel is not None and hasattr(spline_panel, 'set_current_frame'):
                try:
                    spline_panel.set_current_frame(frame)
                except Exception:
                    pass
            # A3: also sync the open keyframe editor dialog's playhead so
            # scrubbing the main transport bar moves the dialog playhead too
            # (bidirectional — the dialog's own drag already seeks the viewport).
            if props is not None and hasattr(props, 'push_current_frame_to_editor'):
                try:
                    props.push_current_frame_to_editor(frame)
                except Exception:
                    pass
        finally:
            self._syncing_composite_frame = False

        # Re-evaluate so the viewer reflects the new animated frame (P0-1).
        # 渲染线程自驱播放中：求值由驱动循环承担，这里只保留显示同步。
        if getattr(self, "_drive_active", False):
            self._refresh_viewer_handles_for_frame()
            try:
                self.composite_frame_changed.emit(frame)
            except RuntimeError:
                pass
            return
        self._evaluate_graph()
        # Viewer control handles must follow keyframe animation: an animated
        # Transform's gizmo/handles track the interpolated value per frame.
        self._refresh_viewer_handles_for_frame()
        # Notify the transport bar / comp-clip playback listeners of the move.
        try:
            self.composite_frame_changed.emit(frame)
        except RuntimeError:
            pass

    def _total_frames_for_node(self) -> int:
        """Best-effort total frame count for the current composition.

        P0-3: explicit None check so a duration of 0 is still honoured.
        Comp-clip fallback: an independent comp clip caches its duration in
        load_composite_data, so playback works without a runtime context.
        """
        rc = self._runtime_context
        if rc is not None:
            dur = getattr(rc, 'duration_frames', None)
            if dur is not None:
                return max(1, int(dur))
        # Comp-clip cached duration (loaded from graph_data.duration_frames).
        comp_dur = getattr(self, '_composite_duration_frames', 0) or 0
        if comp_dur > 0:
            return max(1, int(comp_dur))
        return max(1, self._composite_current_frame + 1)

    # ── Comp-clip independent playback engine ──────────────────────────────
    # The third playback path (after timeline-backed and video_controller):
    # an independent comp clip has no timeline/video controller, so this
    # self-contained QTimer advances the composite playhead via
    # set_composite_current_frame at the clip's fps. The bottom transport bar
    # (CompositeViewer.video_control_bar) is wired to these methods.
    def _ensure_comp_playback_timer(self) -> None:
        """Lazily create the comp-clip playback timer (once)."""
        if getattr(self, "_comp_playback_timer", None) is not None:
            return
        self._comp_playback_timer = QTimer(self)
        # 视频帧推进对定时间隔敏感；与剪辑页/调色页播放定时器一致使用
        # PreciseTimer，避免 CoarseTimer 的间隔抖动放大丢帧。
        self._comp_playback_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._comp_playback_timer.setSingleShot(False)
        self._comp_playback_timer.timeout.connect(self._on_comp_playback_tick)
        self._comp_playing: bool = False
        self._comp_play_start_time: float = 0.0
        self._comp_play_start_frame: int = 0
        # I/O playback range for comp clips (None = full range).
        self._comp_in_frame: int | None = None
        self._comp_out_frame: int | None = None
        # B2: user-controllable loop toggle for comp-clip playback. The bar's
        # ⟳ button was previously cosmetic here — looping was hardcoded in
        # _on_comp_playback_tick whenever an Out point existed. Now False means
        # "stop at the Out point" (NLE one-shot behaviour), True means loop.
        self._comp_loop: bool = True
        self._drive_active: bool = False

    def set_image_color_mode(self, enabled: bool) -> None:
        """Toggle Image Color mode for this composite page.

        In Image mode the page is used for static image compositing only:
        keyframe/animation/time-driven inputs are hidden or disabled, while
        all node effects (color, blur, transform, mix, generators, masks,
        static 3D) stay fully usable. Entering video/timeline mode restores
        the full keyframe workflow.
        """
        if self._is_image_color_mode == enabled:
            return
        self._is_image_color_mode = enabled

        if enabled:
            # 1. Stop the independent comp-clip playback engine if any.
            timer = getattr(self, "_comp_playback_timer", None)
            if timer is not None:
                timer.stop()
            self._comp_playing = False
            # 2. Detach the timeline transport delegate so keyframe panels
            #    cannot drive playback.
            self.set_keyframes_transport(None)
            # 3. Hide the transport bar (play/seek/frame slider). The viewer
            #    exposes a video_control_bar attribute once set_viewer ran.
            self._set_image_mode_control_bar(False)
            # 4. Hide every keyframe (◆) button in the properties panel.
            panel = getattr(self, "_properties_panel", None)
            if panel is not None and hasattr(panel, "set_keyframe_buttons_visible"):
                panel.set_keyframe_buttons_visible(False)
        else:
            # Restore keyframe buttons; transport bar reappears when a video/
            # timeline source is bound (handled by the controller wiring).
            panel = getattr(self, "_properties_panel", None)
            if panel is not None and hasattr(panel, "set_keyframe_buttons_visible"):
                panel.set_keyframe_buttons_visible(True)
            # Defensive: also restore the transport bar visibility in case a
            # future runtime mode switch leaves it hidden.
            self._set_image_mode_control_bar(True)

    def _set_image_mode_control_bar(self, visible: bool) -> None:
        """Show or hide the viewer's transport bar for image mode."""
        viewer = getattr(self, "_viewer", None)
        if viewer is None:
            return
        bar = getattr(viewer, "video_control_bar", None)
        if bar is None:
            return
        try:
            if visible:
                if hasattr(bar, "show_for_video"):
                    bar.show_for_video()
                else:
                    bar.setVisible(True)
            else:
                if hasattr(bar, "hide_for_image"):
                    bar.hide_for_image()
                else:
                    bar.setVisible(False)
                    if hasattr(bar, "set_enabled"):
                        bar.set_enabled(False)
        except Exception:
            pass

    def is_comp_clip_playback_active(self) -> bool:
        """True when this composition is an independent comp clip that should
        drive playback via the comp-clip timer (no timeline/video controller).
        """
        return bool(getattr(self, "_is_comp_clip", False)) and self._runtime_context is None

    def comp_clip_total_frames(self) -> int:
        return self._total_frames_for_node()

    def comp_clip_fps(self) -> float:
        return float(getattr(self, "_composite_fps", 24.0) or 24.0)

    def _comp_effective_out(self) -> int:
        total = self.comp_clip_total_frames()
        out = getattr(self, "_comp_out_frame", None)
        return min(out, total - 1) if out is not None else total - 1

    def comp_play(self) -> None:
        """Start comp-clip playback.

        Timeline-backed comps delegate to the transport (the Edit playback
        controller that also drives the viewport bar); the internal timer is
        only for independent comp clips / standalone compositions.
        """
        # Image Color mode: no time-driven playback is meaningful.
        if (self._is_image_color_mode
                or not getattr(self, "_composition_active", True)):
            return
        transport = getattr(self, '_keyframes_transport', None)
        if transport is not None:
            try:
                if not transport.is_playing():
                    transport.play()
                self.notify_external_play_state(True)
                return
            except Exception:
                self._keyframes_transport = None
        self._ensure_comp_playback_timer()
        if self._comp_playing:
            return
        # If at/past the end (or the Out point), restart from the In/start.
        start = getattr(self, "_comp_in_frame", None) or 0
        out = self._comp_effective_out()
        if self._composite_current_frame >= out:
            self.set_composite_current_frame(start)
        import time
        self._comp_playing = True
        self._comp_play_start_time = time.perf_counter()
        self._comp_play_start_frame = self._composite_current_frame
        # 渲染线程自驱：下一次求值请求将携带驱动参数（见
        # _build_playback_drive），渲染线程接管帧率推进。
        self._drive_active = True
        fps = max(1.0, self.comp_clip_fps())
        self._comp_playback_timer.start(int(1000.0 / fps))
        # 自驱启动：立即提交首帧求值（携带 playback_drive，渲染线程由此
        # 进入驱动循环）。tick 定时器在驱动模式下只做播放头显示记账，
        # 不会提交求值——没有这次提交驱动就永远不会开始。
        self._evaluate_graph_now()
        self._notify_kf_panel_play_state()

    def comp_pause(self) -> None:
        """Stop comp-clip playback (or the transport delegate's engine)."""
        transport = getattr(self, '_keyframes_transport', None)
        if transport is not None:
            try:
                if transport.is_playing():
                    transport.pause()
                self.notify_external_play_state(False)
                return
            except Exception:
                self._keyframes_transport = None
        if (not getattr(self, "_comp_playing", False)
                and not getattr(self, "_drive_active", False)):
            return
        self._stop_comp_playback()

    def _stop_comp_playback(self) -> None:
        """Stop the page-owned comp timer and render-thread drive state."""
        timer = getattr(self, "_comp_playback_timer", None)
        if timer is not None:
            timer.stop()
        was_active = bool(getattr(self, "_comp_playing", False)
                          or getattr(self, "_drive_active", False))
        self._comp_playing = False
        self._drive_active = False
        if was_active and self._renderer is not None:
            try:
                self._renderer.stop_playback_drive()
            except (AttributeError, RuntimeError):
                logger.debug("Composite playback-drive stop failed",
                             exc_info=True)
        if was_active:
            self._notify_kf_panel_play_state()

    def comp_toggle_play(self) -> None:
        self._ensure_comp_playback_timer()
        if self._comp_playing:
            self.comp_pause()
        else:
            self.comp_play()

    def comp_seek(self, frame: int) -> None:
        total = self.comp_clip_total_frames()
        self.set_composite_current_frame(max(0, min(int(frame), total - 1)))

    def comp_next_frame(self) -> None:
        self.comp_seek(self._composite_current_frame + 1)

    def comp_prev_frame(self) -> None:
        self.comp_seek(self._composite_current_frame - 1)

    def comp_first_frame(self) -> None:
        self.comp_seek(getattr(self, "_comp_in_frame", None) or 0)

    def comp_last_frame(self) -> None:
        self.comp_seek(self._comp_effective_out())

    def _on_comp_playback_tick(self) -> None:
        """Advance the comp-clip playhead by elapsed real time."""
        if not getattr(self, "_composition_active", True):
            self._stop_comp_playback()
            return
        import time
        elapsed = time.perf_counter() - self._comp_play_start_time
        fps = max(1.0, self.comp_clip_fps())
        target = self._comp_play_start_frame + int(elapsed * fps)
        out = self._comp_effective_out()
        start = getattr(self, "_comp_in_frame", None) or 0
        if target >= out + 1:
            # B2: honour the loop toggle. Loop restarts from the In/start;
            # one-shot parks at the Out point and stops.
            if self._comp_loop:
                self.set_composite_current_frame(start)
                self._comp_play_start_time = time.perf_counter()
                self._comp_play_start_frame = start
            else:
                self.set_composite_current_frame(out)
                self.comp_pause()
            return
        if target != self._composite_current_frame:
            if getattr(self, "_drive_active", False):
                # 渲染线程自驱中：本 tick 只做最轻量的播放头记账（面板同步
                # 每帧全做会拖住 UI 线程——Keyframes/Properties/Spline 的
                # 跟随在暂停/seek 时由正常求值路径刷新到当前位置）。
                self._composite_current_frame = target
                try:
                    self.composite_frame_changed.emit(target)
                except RuntimeError:
                    pass
                return
            self.set_composite_current_frame(target)

    def set_comp_clip_in_out(self, in_frame: int | None,
                              out_frame: int | None) -> None:
        """Set the comp-clip I/O playback range (None clears a point)."""
        self._ensure_comp_playback_timer()
        self._comp_in_frame = in_frame
        self._comp_out_frame = out_frame

    def set_comp_loop(self, enabled: bool) -> None:
        """B2: toggle comp-clip playback looping (bound to the bar's ⟳ button).

        ``True`` (default) loops the I/O range / full range; ``False`` stops at
        the Out point. Previously the toggle was cosmetic on the comp-clip path
        — looping was hardcoded whenever an Out point existed.
        """
        self._ensure_comp_playback_timer()
        self._comp_loop = bool(enabled)

    def clear_comp_clip_in_out(self) -> None:
        self._ensure_comp_playback_timer()
        self._comp_in_frame = None
        self._comp_out_frame = None

    def _apply_view_mode_overrides(self, snapshot) -> dict:
        """Apply preview-context-dependent bg/grid overrides to a snapshot.

        网格与不透明背景都作为真实几何/填充进入 Scene3D 当前渲染纹理，所以要按
        **渲染上下文**统一门控：3D 预览走预览参考开关，合成输出走显式输出开关。

        - 3D 模式 + 正在预览某 Scene3D 节点 → 该节点使用 show_grid/bg_opaque
          /show_axes（默认开，看 3D 参考，硬件遮挡正确）。
        - 其他（2D 模式 / 预览 Output / 预览非 Scene3D 节点）→ 使用
          output_show_grid/output_bg_opaque（默认关，用户可显式输出网格/背景）。

        场景参数已移至全局「3D Scene Settings」（不再在节点参数上），所以本方法
        不再修改快照节点参数，而是构建一个 ``{node_id: {key: value}}`` 覆盖字典，
        由 RenderRequest → eval_ctx.view_mode_overrides 传给 Scene3D，在 _sp()
        读取时按 node_id 精确覆盖（优先级最高）。

        Returns:
            view_mode_overrides dict（{node_id: {show_grid/bg_opaque/show_axes}}）。
        """
        nodes = getattr(snapshot, "nodes", {})
        # 全局默认设置（预览开关 show_grid/bg_opaque/show_axes + 输出开关
        # output_show_grid/output_bg_opaque 都从这里读）。
        gs = self._global_3d_scene_settings()
        overrides: dict[str, dict] = {}
        # 判定 3D 预览上下文：3D 模式 + 当前预览目标是一个 scene3d 节点。
        in_3d = (self._viewer is not None
                 and self._viewer.get_view_mode() == "3d")
        resolver = getattr(self, "_resolve_viewer_node_id", None)
        if callable(resolver):
            preview_id = resolver()
        else:
            preview_id = self._resolve_preview_node_id()
        target = preview_id if (in_3d and preview_id is not None) else None
        if target is not None:
            ns_target = nodes.get(target)
            if ns_target is None or getattr(ns_target, "node_type", None) != "scene3d":
                target = None  # 预览的不是 scene3d → 不显参考元素

        for _nid, _nsnap in nodes.items():
            if getattr(_nsnap, "node_type", None) == "scene3d":
                _is_target = (_nid == target)
                if _is_target:
                    # 3D 预览目标：用预览参考开关（show_grid/bg_opaque/show_axes）
                    ov = {
                        "show_grid": bool(gs.get("show_grid", True)),
                        "bg_opaque": bool(gs.get("bg_opaque", True)),
                        "show_axes": bool(gs.get("show_axes", True)),
                    }
                    overrides[_nid] = ov
                else:
                    # 合成输出（或非 3D 预览）：用 output_* 开关（默认关）
                    overrides[_nid] = {
                        "show_grid": bool(gs.get("output_show_grid", False)),
                        "bg_opaque": bool(gs.get("output_bg_opaque", False)),
                        "show_axes": False,  # 轴线为预览辅助，不进合成输出
                    }
        return overrides

    # ── Node-Preview Pipeline (Fusion/Nuke-style "preview any node") ────────

    def _view_signature(self) -> str:
        """Opaque signature of the current view-dependent render state.

        Encodes everything that changes Scene3D's baked-in overrides (the grid +
        opaque background) without changing graph topology: the viewer's
        view_mode (2d/3d) plus the current preview target node. The renderer
        includes this in its request-dedup signature so a Tab toggle or a "1"
        preview switch forces a re-evaluate even when generation is unchanged —
        otherwise the stale grid-on/grid-off result would be shown.

        P6 最小闭环：3D 模式下追加相机签名段（cam_orbit_yaw/pitch/dist/target）。
        这样 Alt+orbit 时相机变化对 renderer 的 Layer-1 dedup 可见。模型/几何
        transform 预览另由 graph generation + force_refresh 保证实时刷新。
        """
        mode = ""
        quad = ""
        ortho = ""
        cam_sig = ""
        if self._viewer is not None:
            mode = self._viewer.get_view_mode() or ""
            # M2: quad 视图开关进签名——切换 quad 触发重渲染（Scene3D 据此渲染正交纹理）
            if hasattr(self._viewer, 'is_quad_view') and self._viewer.is_quad_view():
                quad = "quad"
            # M2: 单视图正交 override 进签名——中键进入正交单视图触发重渲染
            sov = self._single_ortho_view_code()
            if sov:
                ortho = f"o{sov}"
            # P6：3D 模式追加相机签名段（让相机变化绕过 Layer-1 dedup）。
            # 只在 3D 模式追加——2D 模式相机不影响渲染，且 _nav_params_dict 可能
            # 返回陈旧值。量化到 1e-4 避免浮点抖动导致签名逐帧变化（即使相机没动）。
            # 注意：active Camera3D/free camera 可能只回传 cam_eye_* 或额外 optics
            # 参数，不能只签 cam_orbit_*，否则拖动视角会被 renderer dedup 当成 no-op。
            if mode == "3d":
                nav = getattr(self._viewer, '_nav_params_dict', None)
                if callable(nav):
                    try:
                        p = nav()
                        cam_keys = (
                            "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
                            "cam_target_x", "cam_target_y", "cam_target_z",
                            "cam_eye_x", "cam_eye_y", "cam_eye_z",
                            "cam_roll", "cam_ortho_scale",
                        )
                        cam_sig = "|".join(
                            f"{k}={float(p.get(k, 0.0)):.4f}"
                            for k in cam_keys
                            if k in p)
                    except Exception:
                        cam_sig = ""
        # P6：追加交互态标志——导航态(NAVIGATION profile)与静止态(PREVIEW)的
        # Scene3D 输出不同（MSAA/IBL 开关），交互态变化必须触发重渲染。纳入签名
        # 后 _on_nav3d_restore（恢复高质量）不再需要 force_refresh 兜底。
        nav_active = "nav1" if (self._interaction_active or self._nav3d_active) else "nav0"
        # P6 回归修复：gizmo 拖拽物体（object_pos/rot/scale）必须触发重渲染。
        # 这些参数不在 generation/color_revision 里（改 model3d 参数不 bump color_revision），
        # 也不在 cam_sig 里。若不纳入 view_signature，renderer Layer-1 dedup 会把连续
        # 拖拽当重复 skip → 拖动物体不实时刷新（需转视角才更新）。
        obj_sig = ""
        if self._viewer is not None:
            _gop = getattr(self._viewer, "_gizmo_obj_pos", None)
            if _gop is not None:
                try:
                    obj_sig = f"obj={float(_gop[0]):.4f},{float(_gop[1]):.4f},{float(_gop[2]):.4f}"
                except (TypeError, IndexError, ValueError):
                    obj_sig = ""
        return f"{mode}|{quad}|{ortho}|{self._resolve_viewer_node_id() or ''}|cam={cam_sig}|{nav_active}|{obj_sig}"

    def _view_signature_cam(self) -> str:
        """P6-B：视图签名的相机段（仅相机参数）。

        用于快速路径判定：若 _view_signature_non_cam 不变但 _view_signature_cam 变，
        说明是纯相机导航（orbit/pan/dolly），可走 RENDER_3D_VIEW 快速路径（绕过
        完整 eval）。与 _view_signature 的 cam= 段保持一致。
        """
        cam_sig = ""
        if self._viewer is not None:
            mode = self._viewer.get_view_mode() or ""
            if mode == "3d":
                nav = getattr(self._viewer, '_nav_params_dict', None)
                if callable(nav):
                    try:
                        p = nav()
                        cam_keys = (
                            "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
                            "cam_target_x", "cam_target_y", "cam_target_z",
                            "cam_eye_x", "cam_eye_y", "cam_eye_z",
                            "cam_roll", "cam_ortho_scale",
                        )
                        cam_sig = "|".join(
                            f"{k}={float(p.get(k, 0.0)):.4f}"
                            for k in cam_keys if k in p)
                    except Exception:
                        cam_sig = ""
        return cam_sig

    def _view_signature_non_cam(self) -> str:
        """P6-B：视图签名的非相机段（mode/quad/ortho/node/nav）。

        快速路径判定：此签名不变 = 场景拓扑 + 视图模式 + 交互态都稳定，
        只有相机变 → 可走 RENDER_3D_VIEW。此签名变 = 走完整 EVALUATE_GRAPH。
        """
        mode = ""
        quad = ""
        ortho = ""
        if self._viewer is not None:
            mode = self._viewer.get_view_mode() or ""
            if hasattr(self._viewer, 'is_quad_view') and self._viewer.is_quad_view():
                quad = "quad"
            sov = self._single_ortho_view_code()
            if sov:
                ortho = f"o{sov}"
        nav_active = "nav1" if (self._interaction_active or self._nav3d_active) else "nav0"
        # P6 回归修复：gizmo object_pos 也纳入 non_cam（拖物体应走完整 eval）。
        obj_sig = ""
        if self._viewer is not None:
            _gop = getattr(self._viewer, "_gizmo_obj_pos", None)
            if _gop is not None:
                try:
                    obj_sig = f"obj={float(_gop[0]):.4f},{float(_gop[1]):.4f},{float(_gop[2]):.4f}"
                except (TypeError, IndexError, ValueError):
                    obj_sig = ""
        return f"{mode}|{quad}|{ortho}|{self._resolve_viewer_node_id() or ''}|{nav_active}|{obj_sig}"

    def _project_output_size(self, snapshot=None) -> tuple[int, int]:
        """Return the current project/output canvas size.

        Output nodes own the composite project resolution. Persistent Scene3D
        nodes use this size so their render output matches the project canvas.
        """
        def _size_from_params(params: dict) -> Optional[tuple[int, int]]:
            try:
                width = int(params.get("width", 0))
                height = int(params.get("height", 0))
            except Exception:
                return None
            if width > 0 and height > 0:
                return width, height
            return None

        nodes = getattr(snapshot, "nodes", None) if snapshot is not None else None
        if isinstance(nodes, dict):
            for ns in nodes.values():
                if getattr(ns, "node_type", None) == "output":
                    size = _size_from_params(getattr(ns, "parameters", {}) or {})
                    if size is not None:
                        return size

        graph = getattr(self, "_node_graph", None)
        graph_nodes = getattr(graph, "nodes", None) if graph is not None else None
        if isinstance(graph_nodes, dict):
            for node in graph_nodes.values():
                if getattr(node, "node_type", None) == "output":
                    size = _size_from_params(getattr(node, "parameters", {}) or {})
                    if size is not None:
                        return size

        state = self.__dict__
        color_w = int(state.get("_last_color_width", 0) or 0)
        color_h = int(state.get("_last_color_height", 0) or 0)
        if color_w > 0 and color_h > 0:
            return color_w, color_h
        return 1920, 1080

    def _viewer3d_render_size(self, snapshot=None) -> tuple[int, int]:
        """Return the live 3D Viewer render size for the current panel."""
        viewer = getattr(self, "_viewer", None)
        getter = getattr(viewer, "viewer3d_content_size", None)
        if callable(getter):
            try:
                size = getter()
            except Exception:
                size = None
            if isinstance(size, (tuple, list)) and len(size) >= 2:
                try:
                    width = int(round(float(size[0])))
                    height = int(round(float(size[1])))
                except Exception:
                    width = 0
                    height = 0
                if width > 0 and height > 0:
                    return width, height
        return self._project_output_size(snapshot)

    def _is_quad_view(self) -> bool:
        """Whether the viewer is currently in 3D quad-view mode (M2)."""
        if self._viewer is None or not hasattr(self._viewer, 'is_quad_view'):
            return False
        return self._viewer.is_quad_view()

    def _single_ortho_view_code(self) -> int:
        """M2: 单视图正交 override（0=perspective，1=top，2=right，3=front）。

        仅 SINGLE+3D 模式有意义；quad 模式返回 0（quad 各象限独立渲染）。
        """
        if (self._viewer is None
                or not hasattr(self._viewer, 'get_single_ortho_view')):
            return 0
        return self._viewer.get_single_ortho_view()

    def _quad_active_view_code(self) -> int:
        """Milestone C / P2：四视图活动视图代码（0=perspective，1=top，2=right，3=front）。

        quad 模式下返回鼠标所在象限（viewer.get_quad_active_view）；
        非 quad / 非 3D 返回 0。Scene3D 据此对非活动正交视图降质/降频。
        """
        if (self._viewer is None
                or not hasattr(self._viewer, 'get_quad_active_view')):
            return 0
        return self._viewer.get_quad_active_view()

    def _selected_3d_node_id_for_request(self) -> Optional[str]:
        """P7：当前选中的 3D 节点 id（outline pass 用）。从 viewer 选中状态读。"""
        viewer = self._viewer
        if viewer is None:
            return None
        if hasattr(viewer, "get_selected_3d_scene_node"):
            scene_nid = viewer.get_selected_3d_scene_node()
            if scene_nid:
                return str(scene_nid)
        # viewer 持有选中节点 id（_interaction_state 或 set_selected_node 设的）
        nid = getattr(viewer, '_viewing_node_id', None)
        if nid:
            return str(nid)
        sel = getattr(viewer, '_interaction_state', None)
        if sel is not None:
            snid = getattr(sel, 'selected_node_id', None)
            if snid:
                return str(snid)
        return None

    def _isolate_3d_node_id_for_request(self) -> Optional[str]:
        """P7：隔离模式节点 id（只渲染该节点）。从 viewer Viewport3DState 读。"""
        viewer = self._viewer
        if viewer is None:
            return None
        ctrl = getattr(viewer, '_viewport3d_ctrl', None)
        if ctrl is None:
            return None
        iso = getattr(ctrl.state, 'isolate_node_id', None)
        return str(iso) if iso else None

    def _on_quad_view_changed(self, is_quad: bool) -> None:
        """M2: 四视图开关变化（MMB click toggle）。

        view_signature 含 quad → 下次提交会带正确的 quad_view；强制刷新跳过脏检查，
        立即重新提交渲染（Scene3D 据此决定是否渲染正交纹理）。
        """
        if not self._is_active:
            return
        self._force_next_view_refresh = True
        self._evaluate_graph_now()

    def _on_viewer3d_content_size_changed(self, width: int, height: int) -> None:
        """Re-render the live 3D Viewer when its usable panel size changes."""
        if not self._is_active:
            return
        if self._viewer is None or self._viewer.get_view_mode() != "3d":
            return
        self._force_next_view_refresh = True
        self._evaluate_graph()

    def _resolve_preview_node_id(self) -> Optional[str]:
        """Return the node id whose output the viewer should show, or None.

        Validates that the previewed node still exists in the current graph.
        A stale id (node deleted) is cleared lazily and resolves to None so the
        viewer falls back to the Output node — graceful degradation without an
        extra event handler hot-path. Output is intentionally excluded: previewing
        the Output node is a no-op (it's the default), and excluding it keeps the
        dashed preview connection meaningful.
        """
        nid = self._preview_node_id
        if nid is None:
            return None
        node = self._node_graph.get_node(nid) if self._node_graph else None
        if node is None or node.node_type == "output":
            self._preview_node_id = None
            self._update_preview_connection()
            self._update_preview_status_label()
            return None
        return nid

    def _resolve_viewer_node_id(self) -> Optional[str]:
        """Return the node id the viewer should render directly.

        In 3D mode the viewer renders a virtual global 3D work scene injected
        into the render snapshot. Scene3D nodes remain renderers for their
        connected inputs and are not required just to enter the 3D editor. In
        2D mode, keep the existing Nuke/Fusion-style manual node preview
        behavior and otherwise fall back to Output.
        """
        if self._viewer is not None and self._viewer.get_view_mode() == "3d":
            return self._VIEWER3D_VIRTUAL_NODE_ID
        return self._resolve_preview_node_id()

    def _inject_viewer3d_scene_snapshot(self, snapshot):
        """Inject a non-persistent global 3D Viewer scene into a snapshot.

        AUD-48006: GraphSnapshot 冻结后本方法不再原地重绑定字段——构建
        新的 nodes/connections/eval_order 集合后经 ``dataclasses.replace``
        返回**替换快照**；调用方必须使用返回值。viewer 未激活时原样返回。
        """
        if self._viewer is None or self._viewer.get_view_mode() != "3d":
            return snapshot
        from src.features.composite.core.graph_snapshot import (
            ConnectionSnapshot, NodeSnapshot)
        from src.features.composite.nodes.scene3d_node import Scene3DNode
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings)

        virtual_id = self._VIEWER3D_VIRTUAL_NODE_ID
        scene = Scene3DNode(node_id=virtual_id)
        viewer_state = getattr(self._node_graph, "_viewer_state", {}) or {}
        default_scene = merged_viewer3d_scene_settings(
            viewer_state.get("default_3d_scene", {}))
        params = dict(scene.parameters)
        params.update(default_scene)
        width, height = self._viewer3d_render_size(snapshot)
        params.update({
            "width": width,
            "height": height,
            "primitive": "none",
            "output_show_grid": False,
            "output_bg_opaque": False,
        })
        try:
            params.update(self._viewer._nav_params_dict())
        except Exception:
            pass

        # P6 最小闭环修复：虚拟节点 param_revision 必须随相机参数变化 bump，
        # 否则去掉 nav 路径的 force_refresh 后，Layer-2 per-node dirty 检查看不到
        # 相机变化（param_revision 0==0），Scene3D 不被标记 dirty → 默认场景摄影
        # 完全不响应操作。用相机参数的稳定 hash 当 revision——相机一动 revision 变 →
        # 虚拟节点 + 下游 Output 标记 dirty → Scene3D 重求值；上游节点 revision 不变
        # → cache-hit（P6 的收益保留）。
        import hashlib as _hashlib
        try:
            nav = self._viewer._nav_params_dict()
            _cam_rev_str = "|".join(
                f"{k}={float(nav.get(k, 0.0)):.4f}"
                for k in ("cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
                          "cam_target_x", "cam_target_y", "cam_target_z"))
            # 32 位 hash 转 int（正数），稳定可复现
            _virtual_param_revision = (int(_hashlib.md5(
                _cam_rev_str.encode("utf-8")).hexdigest(), 16) & 0x7fffffff) or 1
        except Exception:
            _virtual_param_revision = 1

        new_nodes = dict(snapshot.nodes)
        new_nodes[virtual_id] = NodeSnapshot(
            node_id=virtual_id,
            node_type="scene3d",
            parameters=params,
            disabled=False,
            input_ports=[("in", "scene")],
            output_ports=[("display", "image")],
            state="clean",
            param_revision=_virtual_param_revision,
        )
        new_connections = [
            c for c in snapshot.connections
            if c.src_node_id != virtual_id and c.dst_node_id != virtual_id
        ]
        active_camera_id = str(getattr(self, "_viewer3d_active_camera_id", "") or "")
        for nid, ns in new_nodes.items():
            if nid == virtual_id:
                continue
            # Camera3D is a visible scene object in the global 3D Viewer, but it
            # must not automatically become the viewing camera. Icons/frustums
            # are drawn from node graph data by the viewport overlay. Only the
            # explicitly active Camera3D is connected to the virtual renderer,
            # so optical params such as FOV/ortho/lens shift drive the rendered
            # view without letting every newly-created camera take over.
            if (ns.node_type in {"light3d", "geo3d", "model3d"}
                    or (ns.node_type == "camera3d" and nid == active_camera_id)):
                new_connections.append(ConnectionSnapshot(
                    src_node_id=nid,
                    src_port_name="display",
                    dst_node_id=virtual_id,
                    dst_port_name="in",
                ))
        new_eval_order = [
            nid for nid in snapshot.eval_order if nid != virtual_id
        ] + [virtual_id]
        from dataclasses import replace as _dc_replace
        return _dc_replace(
            snapshot, nodes=new_nodes, connections=new_connections,
            eval_order=new_eval_order)

    def _global_3d_scene_settings(self) -> dict:
        """Return the merged global 3D scene settings for the current graph.

        Reads ``viewer_state["default_3d_scene"]`` and merges it with
        ``DEFAULT_VIEWER3D_SCENE_SETTINGS`` so every Scene3D node (real or
        virtual viewer scene) reads the same scene defaults. Injected into
        RenderRequest → CompositeEvalContext so Scene3D reads it at render
        time instead of from its own node parameters.
        """
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings)
        viewer_state = getattr(self._node_graph, "_viewer_state", {}) or {}
        return merged_viewer3d_scene_settings(
            viewer_state.get("default_3d_scene", {}))

    @staticmethod
    def _viewer3d_nav_param_keys() -> tuple[str, ...]:
        """Camera-navigation keys persisted in the global 3D Viewer scene."""
        return (
            "cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
            "cam_target_x", "cam_target_y", "cam_target_z",
        )

    def _viewer3d_default_nav_snapshot(self) -> dict:
        """Return current global 3D Viewer camera params for drag undo bookkeeping."""
        if not self._node_graph:
            return {}
        settings = self._global_3d_scene_settings()
        return {
            key: settings.get(key)
            for key in self._viewer3d_nav_param_keys()
        }

    def _apply_viewer3d_default_nav_params(self, params: dict) -> bool:
        """Persist virtual 3D Viewer navigation params into default scene settings."""
        if not self._node_graph or not params:
            return False
        from src.features.composite.ui.viewer3d_settings_section import (
            merged_viewer3d_scene_settings)

        viewer_state = dict(getattr(self._node_graph, "_viewer_state", {}) or {})
        default_scene = merged_viewer3d_scene_settings(
            viewer_state.get("default_3d_scene", {}))
        changed = False
        for key in self._viewer3d_nav_param_keys():
            if key not in params:
                continue
            value = float(params[key])
            if default_scene.get(key) != value:
                default_scene[key] = value
                changed = True
        if not changed:
            return False
        viewer_state["default_3d_scene"] = default_scene
        self._node_graph._viewer_state = viewer_state
        return True

    def _resolve_viewer3d_scene_context_id(self) -> Optional[str]:
        """Return the Scene3D node currently used as the 3D Viewer context."""
        if not self._node_graph:
            return None
        scene_id = self._viewer3d_node_id
        if scene_id:
            node = self._node_graph.get_node(scene_id)
            if node is not None and getattr(node, "node_type", None) == "scene3d":
                return scene_id
        return None

    def _first_scene3d_node_id(self) -> Optional[str]:
        """Return the first Scene3D node id in the graph, if any."""
        if not self._node_graph:
            return None
        nodes = getattr(self._node_graph, "nodes", None)
        if not nodes:
            return None
        for nid, node in nodes.items():
            if getattr(node, "node_type", None) == "scene3d":
                return nid
        return None

    def _scene3d_for_source_node(self, source_node_id: str) -> Optional[str]:
        """Return a Scene3D connected to a 3D source node, preferring context."""
        if not self._node_graph:
            return None
        current = self._resolve_viewer3d_scene_context_id()
        connected: list[str] = []
        for conn in self._node_graph.connections:
            if conn.src_node_id != source_node_id:
                continue
            dst = self._node_graph.get_node(conn.dst_node_id)
            if dst is not None and getattr(dst, "node_type", None) == "scene3d":
                connected.append(conn.dst_node_id)
        if current in connected:
            return current
        return connected[0] if connected else None

    def _update_viewer3d_context_for_selection(self, node_id: str, node_type: str) -> None:
        """Update the Scene3D context without binding it to Output wiring."""
        if not self._node_graph:
            self._viewer3d_node_id = None
            return
        if node_type == "scene3d":
            self._viewer3d_node_id = node_id
            return
        if node_type in {"camera3d", "light3d", "geo3d", "model3d", "deformer3d"}:
            connected_scene = self._scene3d_for_source_node(node_id)
            if connected_scene is not None:
                self._viewer3d_node_id = connected_scene
                return
            if self._resolve_viewer3d_scene_context_id() is None:
                self._viewer3d_node_id = self._first_scene3d_node_id()
            return
        if self._viewer is None or self._viewer.get_view_mode() != "3d":
            self._viewer3d_node_id = None

    def _is_node_previewable(self, node_id: str) -> bool:
        """Whether a node can be previewed (has image output and isn't Output)."""
        if not self._node_graph:
            return False
        node = self._node_graph.get_node(node_id)
        if node is None or node.node_type == "output":
            return False
        return True

    def toggle_preview_selected_node(self) -> None:
        """Toggle node-preview for the currently selected node (the "1" shortcut).

        Behaviour (single-slot, Nuke single-viewer model):
        - nothing selected / selection not previewable → exit preview (back to Output)
        - selected node already being previewed → exit preview
        - otherwise → enter preview for the selected node
        Entering/exiting redraws the dashed preview connection and re-renders.
        """
        selected = self._node_editor.get_selected_node_id() if self._node_editor else None
        if selected is None or not self._is_node_previewable(selected):
            self._exit_node_preview()
            return
        if self._preview_node_id == selected:
            # Toggle off: same node pressed again → back to Output.
            self._exit_node_preview()
            return
        self._preview_node_id = selected
        self._update_preview_connection()
        self._update_preview_status_label()
        self._evaluate_graph_now()

    # ── B3-1: group edit view (enter/exit with undo) ──────────────────────

    def _on_group_enter_requested(self, group_id: str) -> None:
        """Apply EnterGroupCommand, record ONE undo action, then switch the
        editor canvas to the group's member subset."""
        editor = getattr(self, "_node_editor", None)
        graph = getattr(self, "_node_graph", None)
        if editor is None or graph is None:
            return
        from src.features.composite.ui.group_commands import EnterGroupCommand

        cmd = EnterGroupCommand(graph=graph, group_id=str(group_id))
        if not cmd.redo():
            return

        def _apply(enter: bool) -> None:
            if enter:
                EnterGroupCommand(graph=graph,
                                  group_id=str(group_id)).redo()
                editor.enter_group_view(str(group_id))
            else:
                editor.exit_group_view()

        self._undo_stack.push(UndoAction(
            "group_enter_view",
            {"group_id": str(group_id), "target_state": True},
            undo_fn=lambda data: _apply(bool(not data["target_state"])),
            redo_fn=lambda data: _apply(bool(data["target_state"])),
            description=f"Enter group {group_id}",
        ))
        # Switch the canvas to the member subset (also re-applies the
        # idempotent is_expanded flag and emits group_view_changed).
        editor.enter_group_view(str(group_id))

    def _on_group_view_changed(self, group_id) -> None:
        """Sync the toolbar breadcrumb with the editor's group view."""
        crumb = getattr(self, "_group_breadcrumb", None)
        back = getattr(self, "_group_back_btn", None)
        if crumb is None or back is None:
            return
        stack = []
        editor = getattr(self, "_node_editor", None)
        if editor is not None and hasattr(editor, "group_view_stack"):
            stack = editor.group_view_stack
        if not stack:
            crumb.hide()
            back.hide()
            return
        names = []
        graph = getattr(self, "_node_graph", None)
        for gid in stack:
            label = gid[:8]
            if graph is not None and hasattr(graph, "get_group"):
                try:
                    g = graph.get_group(gid)
                    if isinstance(g, dict) and g.get("title"):
                        label = str(g["title"])
                except Exception:
                    pass
            names.append(label)
        crumb.setText("Root / " + " / ".join(names))
        crumb.show()
        back.show()

    def _exit_group_view(self) -> None:
        """Exit the deepest group view and record the undo action."""
        editor = getattr(self, "_node_editor", None)
        graph = getattr(self, "_node_graph", None)
        if editor is None or not editor.is_in_group_view():
            return
        stack = editor.group_view_stack
        group_id = stack[-1] if stack else None
        if not group_id:
            return
        from src.features.composite.ui.group_commands import (
            EnterGroupCommand, ExitGroupCommand)

        def _apply(enter: bool) -> None:
            if enter:
                EnterGroupCommand(graph=graph,
                                  group_id=str(group_id)).redo()
                editor.enter_group_view(str(group_id))
            else:
                ExitGroupCommand(graph=graph,
                                 group_id=str(group_id)).redo()
                editor.exit_group_view()

        # Apply first (push never executes redo). target_state=False makes
        # undo re-ENTER and redo re-EXIT — with True both sides entered,
        # so every undo→redo cycle appended a duplicate level to the
        # group-view stack (Root / G / G).
        _apply(False)
        self._undo_stack.push(UndoAction(
            "group_exit_view",
            {"group_id": str(group_id), "target_state": False},
            undo_fn=lambda data: _apply(bool(not data["target_state"])),
            redo_fn=lambda data: _apply(bool(data["target_state"])),
            description=f"Exit group {group_id}",
        ))

    # ── AUD-9920: group membership undo (group/ungroup/add/remove) ────────

    def _on_group_commands_applied(self, description: str,
                                   commands: list) -> None:
        """Record ONE undo step for group commands the editor just applied.

        The editor's group operations used to call ``cmd.redo()`` and drop
        the command object, so grouping/ungrouping/membership changes were
        never registered on the undo stack (AUD-9920) — Ctrl+Z skipped them
        and later undos could restore a graph/group state mismatch. The
        signal carries the already-applied command objects; the closures
        below keep them alive for the lifetime of the history entry.
        """
        applied = [c for c in commands if c is not None]
        if not applied:
            return
        self._undo_stack.push(UndoAction(
            "group_change",
            {"description": str(description),
             "group_ids": [getattr(c, "group_id", None) for c in applied]},
            undo_fn=lambda _d: self._apply_group_commands(applied, undo=True),
            redo_fn=lambda _d: self._apply_group_commands(applied, undo=False),
            description=str(description),
        ))

    def _apply_group_commands(self, commands: list, undo: bool) -> None:
        """Undo/redo helper: replay group commands and re-sync the editor."""
        for cmd in commands:
            try:
                if undo:
                    cmd.undo()
                else:
                    cmd.redo()
            except Exception:
                # Qt slot guard: one failing command must not abort the rest
                # of the batch nor escape into the undo()/redo() slot.
                logger.exception("group undo/redo failed for %r", cmd)
        editor = getattr(self, "_node_editor", None)
        if editor is not None:
            # Groups are UI metadata (no render dirty), but graph_changed
            # keeps project dirty/save and minimap refresh working — same
            # contract the live group operations use.
            editor.graph_changed.emit()

    def _exit_node_preview(self, *, evaluate: bool = True) -> None:
        """Clear node-preview state and return the viewer to the Output node."""
        # B3-1: Esc leaves the group edit view first when inside one.
        editor = getattr(self, "_node_editor", None)
        if editor is not None and hasattr(editor, "is_in_group_view") \
                and editor.is_in_group_view():
            self._exit_group_view()
            return
        if self.__dict__.get("_preview_node_id") is None:
            return
        self._preview_node_id = None
        self._update_preview_connection()
        self._update_preview_status_label()
        if evaluate:
            self._evaluate_graph_now()

    def _update_preview_connection(self) -> None:
        """Refresh the dashed preview connection in the node editor."""
        if self._node_editor is not None and hasattr(self._node_editor, "set_preview_node"):
            self._node_editor.set_preview_node(self._preview_node_id)

    def _update_preview_status_label(self) -> None:
        """Show/hide the 'Previewing: <node>' label in the viewer chrome."""
        if self._viewer is not None and hasattr(self._viewer, "set_preview_node"):
            label = None
            if self._preview_node_id and self._node_graph:
                node = self._node_graph.get_node(self._preview_node_id)
                if node is not None:
                    label = node.node_title
            self._viewer.set_preview_node(self._preview_node_id, label)

    def _build_render_request(self) -> Optional[RenderRequest]:
        """Build the current immutable render request without evaluating it.

        The normal graph path and the Scene3D view-only path must use the same
        snapshot/context construction. Keeping this step separate lets the
        latter publish the current virtual viewer camera to the render thread
        without running the graph evaluator a second time.
        """
        if not self._renderer or not self._node_graph or not self._is_active:
            return None

        # Skip background rendering while the Composite page is not visible.
        # on_enter() submits a fresh request when the page becomes active.
        # AUD-9508: to_dict() serialises the whole graph; gate it on the
        # NodeGraph mutation counter so the per-frame/per-mouse-move paths
        # only pay it after an actual graph mutation.
        graph_generation = self._node_graph.generation
        if (
            self._current_composite_id
            and graph_generation != getattr(
                self, "_last_published_graph_generation", -1)
        ):
            graph_data = self._node_graph.to_dict()
            if graph_data != getattr(self, "_last_published_graph_data", None):
                self._refresh_asset_dependency_panel()
                self.composite_graph_changed.emit(
                    self._current_composite_id, graph_data)
                self._last_published_graph_data = graph_data
            self._last_published_graph_generation = graph_generation

        request_id = self._request_id_gen.next_id()
        self._last_submitted_request_id = request_id
        fps_resolver = getattr(self, "comp_clip_fps", None)
        if callable(fps_resolver):
            snapshot_fps = fps_resolver()
        else:
            snapshot_fps = getattr(self._runtime_context, "fps", 24.0) or 24.0
        snapshot = self._node_graph.snapshot(
            color_texture_id=self._last_color_texture_id,
            color_width=self._last_color_width,
            color_height=self._last_color_height,
            color_revision=self._last_color_revision,
            frame_number=self._current_keyframe_frame(),
            fps=float(snapshot_fps),
        )
        inject_scene_snapshot = getattr(
            self, "_inject_viewer3d_scene_snapshot", None
        )
        if callable(inject_scene_snapshot):
            inject_scene_snapshot(snapshot)
        view_mode_overrides = self._apply_view_mode_overrides(snapshot)
        force_refresh = bool(self.__dict__.pop("_force_next_view_refresh", False))
        return RenderRequest(
            snapshot=snapshot,
            request_id=request_id,
            timestamp=time.monotonic(),
            render_mode=self._current_render_mode,
            runtime_context=self._runtime_context,
            timeline_clip_gpu_provider=self._timeline_clip_gpu_provider,
            edit_project=self._project,
            interaction_active=(
                getattr(self, "_interaction_active", False)
                or getattr(self, "_nav3d_active", False)
            ),
            cancel_on_supersede=self._runtime_context is None,
            viewer_node_id=(
                getattr(self, "_resolve_viewer_node_id", self._resolve_preview_node_id)()
            ),
            view_signature=self._view_signature(),
            force_refresh=force_refresh,
            quad_view=getattr(self, "_is_quad_view", lambda: False)(),
            single_ortho_view=getattr(
                self, "_single_ortho_view_code", lambda: None
            )() or None,
            quad_active_view=getattr(
                self, "_quad_active_view_code", lambda: None
            )() or None,
            selected_3d_node_id=getattr(
                self, "_selected_3d_node_id_for_request", lambda: None
            )(),
            isolate_3d_node_id=getattr(
                self, "_isolate_3d_node_id_for_request", lambda: None
            )(),
            global_3d_scene_settings=getattr(
                self, "_global_3d_scene_settings", lambda: None
            )(),
            view_mode_overrides=view_mode_overrides,
            proxy_scale=getattr(self, "_preview_quality", 1.0),
            output_name=getattr(self, "_output_target_name", "display"),
            output_node_id=getattr(self, "_output_target_node_id", None),
            playback_drive=getattr(
                self, "_build_playback_drive", lambda _snapshot: None
            )(snapshot),
            # B1: user viewer ROI — preview-only evaluation region
            viewer_roi=self._viewer_user_roi_bbox(),
        )

    def _viewer_user_roi_bbox(self):
        """User ROI BBox from the viewer, or None (full frame).

        Only meaningful in preview; export builds its own requests.
        """
        viewer = getattr(self, "_viewer", None)
        if viewer is None or not hasattr(viewer, "get_user_roi_bbox"):
            return None
        try:
            return viewer.get_user_roi_bbox()
        except Exception:
            return None

    def _on_viewer_user_roi_changed(self, _bbox) -> None:
        """B1: the viewer ROI changed — schedule a preview re-evaluate."""
        self._evaluate_graph()

    def _do_evaluate_graph(self) -> None:
        """Actually submit the graph snapshot for evaluation.

        Called by the debounce timer or by _evaluate_graph_now().
        """
        request = self._build_render_request()
        if request is not None:
            self._renderer.submit(CompositeCommand.EVALUATE_GRAPH, {
                'request': request,
            })
        return

    def _set_preview_quality(self, scale: float) -> None:
        """切换预览渲染分辨率（Full / 1/2 / 1/4）。

        仅作用于预览渲染：proxy_scale 经 base_evaluator 推导为 force_lod，
        在 plan() 里强制所有节点降采样（LOD），加速交互。
        导出路径不经过 RenderRequest，恒走原始分辨率，不受此设置影响。
        """
        if abs(scale - self._preview_quality) < 1e-6:
            return
        self._preview_quality = scale
        # force_refresh 绕过 skip signature，确保立即按新分辨率重渲染
        self._force_next_view_refresh = True
        self._evaluate_graph()

    _RESOLUTION_MODE_SCALES = {
        "full": 1.0,
        "half": 0.5,
        "quarter": 0.25,
    }

    def _apply_resolution_mode(self, mode: str, evaluate: bool = True) -> None:
        """B2-1: apply a viewer resolution tier ("full"/"half"/"quarter"/"auto")."""
        mode = str(mode or "full").strip().lower()
        if mode not in {"full", "half", "quarter", "auto"}:
            mode = "full"
        self._auto_proxy_saved_quality = None
        if mode == "auto":
            self._resolution_auto = True
            # Auto idles at full resolution; interactions downgrade to half.
            if evaluate:
                self._set_preview_quality(1.0)
            else:
                self._preview_quality = 1.0
            return
        self._resolution_auto = False
        scale = self._RESOLUTION_MODE_SCALES[mode]
        if evaluate:
            self._set_preview_quality(scale)
        else:
            self._preview_quality = scale

    def _on_resolution_mode_changed(self, mode: str) -> None:
        """B2-1: viewer View menu changed the resolution tier."""
        self._apply_resolution_mode(mode)

    def _on_auto_proxy_interaction(self, active: bool) -> None:
        """B2-1: temporarily render at half resolution while interacting.

        Only downgrades from the full tier — a user-chosen quarter preview
        stays as-is. Restores the base tier once the idle timer fires.
        """
        if not self._resolution_auto:
            return
        if active:
            if self._auto_proxy_saved_quality is None \
                    and self._preview_quality > 0.5 + 1e-6:
                self._auto_proxy_saved_quality = self._preview_quality
                self._set_preview_quality(0.5)
        else:
            saved = self._auto_proxy_saved_quality
            if saved is not None:
                self._auto_proxy_saved_quality = None
                self._set_preview_quality(float(saved))

    def _available_output_targets(self) -> tuple[dict[str, str], ...]:
        """Return named terminal outputs in graph insertion order."""
        from src.features.composite.core.output_targets import (
            output_target_records)

        graph = getattr(self, "_node_graph", None)
        nodes = getattr(graph, "nodes", {}) if graph is not None else {}
        return output_target_records(
            nodes.values() if isinstance(nodes, dict) else ())

    def _set_output_target(self, name: str, node_id: Optional[str] = None) -> None:
        """Select a terminal output target and schedule one fresh evaluation."""
        from src.features.composite.core.output_targets import (
            normalize_output_name)

        target_name = normalize_output_name(name)
        if (target_name == getattr(self, "_output_target_name", "display")
                and node_id == getattr(self, "_output_target_node_id", None)):
            return
        self._output_target_name = target_name
        self._output_target_node_id = node_id
        viewer_state = dict(getattr(self._node_graph, "_viewer_state", {}) or {})
        viewer_state["output_target_name"] = target_name
        viewer_state["output_target_node_id"] = node_id
        self._node_graph._viewer_state = viewer_state
        self._force_next_view_refresh = True
        self._evaluate_graph()

    def available_output_targets(self) -> tuple[dict[str, str], ...]:
        """Public read-only snapshot for host integrations and render queues."""
        return self._available_output_targets()

    def output_dependency_graph(self) -> tuple[dict[str, object], ...]:
        """Return the read-only dependency cones for named Output targets."""
        from src.features.composite.core.output_targets import (
            output_dependency_records)

        graph = getattr(self, "_node_graph", None)
        nodes = getattr(graph, "nodes", {}) if graph is not None else {}
        node_values = nodes.values() if isinstance(nodes, dict) else ()
        connections = getattr(graph, "connections", ()) if graph is not None else ()
        return output_dependency_records(node_values, connections)

    def _show_settings_menu(self) -> None:
        """弹出 Node Graph 设置菜单（节点图自身的视图选项）。

        注意：viewport 相关的选项（display transform、BBox/format overlay、
        channel、perf overlay 等）归属 CompositeViewer 的 View 菜单，不在此处；
        Project / 3D Scene Settings 归属右侧 NodePropertiesPanel。
        本菜单仅承载节点图自身的开关。
        """
        from .node_editor_widget import NodeEditorWidget
        btn = self._settings_btn
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background-color: {C.BG_PANEL}; color: {C.TEXT_SECONDARY};
                border: 1px solid {C.BORDER}; padding: 4px; font-size: 12px;
            }}
            QMenu::item:selected {{ background-color: {C.ORANGE_PRESS}; color: #fff; }}
            QMenu::separator {{ height: 1px; background-color: {C.BORDER}; margin: 4px 8px; }}
        """)

        # ── Grid Snap（节点拖拽对齐网格）──
        snap_act = menu.addAction(I18N.tr("composite.grid_snap", "Grid Snap"))
        snap_act.setCheckable(True)
        snap_act.setChecked(self._node_editor.get_snap_state())
        snap_act.triggered.connect(self._node_editor.set_snap_state)

        # ── Minimap（右下角小地图显隐）──
        minimap_act = menu.addAction(I18N.tr("composite.minimap", "Minimap"))
        minimap_act.setCheckable(True)
        minimap_act.setChecked(self._minimap.isVisible())
        minimap_act.triggered.connect(self._minimap.setVisible)

        menu.addSeparator()

        # ── Node Preview（节点缩略图：None / Selected / All，互斥单选）──
        current_mode = self._node_editor._resolve_preview_mode()
        group = QActionGroup(menu)
        group.setExclusive(True)
        for label, mode in [
            ("Node Preview: Off", NodeEditorWidget.PREVIEW_NONE),
            ("Node Preview: Selected", NodeEditorWidget.PREVIEW_SELECTED_SINGLE),
            ("Node Preview: All", NodeEditorWidget.PREVIEW_ALL),
        ]:
            act = group.addAction(label)
            act.setCheckable(True)
            act.setChecked(mode == current_mode)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._node_editor._set_node_preview_mode(m)
            )
            menu.addAction(act)

        menu.addSeparator()

        # ── Preview Quality（预览渲染分辨率，互斥单选）──
        # 仅影响预览（经 LOD 强制降采样加速交互），导出始终原始分辨率。
        # B2-1: 与视口 View 菜单的 Resolution 同源；Auto 在交互时临时降半档。
        menu.addAction(I18N.tr("composite.preview_quality", "Preview Quality")).setEnabled(False)
        q_group = QActionGroup(menu)
        q_group.setExclusive(True)
        for label, mode in [
            ("Full", "full"),
            ("1/2", "half"),
            ("1/4", "quarter"),
            ("Auto", "auto"),
        ]:
            act = q_group.addAction(label)
            act.setCheckable(True)
            if mode == "auto":
                act.setChecked(self._resolution_auto)
            else:
                act.setChecked(
                    not self._resolution_auto
                    and abs(self._RESOLUTION_MODE_SCALES[mode]
                            - self._preview_quality) < 1e-6)
            act.triggered.connect(
                lambda _checked=False, m=mode: self._apply_resolution_mode(m)
            )
            menu.addAction(act)

        # ── Output Target（多个 Output 节点的命名终端选择）──
        menu.addSeparator()
        menu.addAction(I18N.tr("composite.output_target", "Output Target")).setEnabled(False)
        output_group = QActionGroup(menu)
        output_group.setExclusive(True)
        output_targets = self._available_output_targets()
        name_counts: dict[str, int] = {}
        for target in output_targets:
            name = str(target.get("name", "display"))
            name_counts[name] = name_counts.get(name, 0) + 1
        for target in output_targets:
            name = str(target.get("name", "display"))
            node_id = str(target.get("node_id", ""))
            label = name
            if name_counts[name] > 1:
                label = f"{name} ({node_id[:8]})"
            act = output_group.addAction(label)
            act.setCheckable(True)
            act.setChecked(
                name == getattr(self, "_output_target_name", "display")
                and node_id == getattr(self, "_output_target_node_id", None)
            )
            act.triggered.connect(
                lambda _checked=False, n=name, nid=node_id:
                self._set_output_target(n, nid)
            )
            menu.addAction(act)

        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    # ── Renderer Callbacks ──

    def _on_frame_ready(self, request_id: int, tex_id: int, width: int, height: int) -> None:
        # Render completions can trail the newest submitted playhead request.
        # Keep the most recent completion, rather than demanding an impossible
        # exact match with _last_submitted_request_id during continuous playback.
        if (request_id > 0
                and not getattr(self, "_drive_active", False)
                and request_id <= getattr(self, "_last_presented_frame_request_id", 0)):
            return
        if request_id > 0:
            self._last_presented_frame_request_id = request_id
        self._last_texture_id = tex_id
        self._last_texture_width = width
        self._last_texture_height = height
        # Update performance badges on node items
        self._update_perf_badges()
        # R-7: Delay thumbnail requests 300ms to prioritize main frame display first.
        # Only request thumbnails for visible/selected nodes after main frame is shown.
        # Guard: only schedule if not already pending to avoid stacking requests.
        if not hasattr(self, '_thumbnail_pending') or not self._thumbnail_pending:
            self._thumbnail_pending = True
            QTimer.singleShot(300, self._request_thumbnails_deferred)
        logger.info(f"[COMPOSITE-DEBUG] _on_frame_ready: req={request_id}, tex={tex_id}, "
                     f"size={width}x{height}, is_active={self._is_active}")
        # A source may finish decoding after its Transform was selected.
        # Re-resolve the selected input size so the overlay switches from the
        # composition fallback (e.g. 2K) to the native source size (e.g. 4K)
        # without requiring a second selection click.
        if self._is_active and self._viewer is not None:
            self.update_viewer_handles()
        if self._is_active:
            self.composite_frame_ready.emit(request_id, tex_id, width, height)

    def _build_playback_drive(self, snapshot) -> dict | None:
        """播放中的求值请求附带渲染线程自驱参数。

        仅当：独立 comp clip 播放激活（无时间线上下文）+ 正在播放 +
        图中无关键帧动画（动画参数由 UI 侧按帧号预插值，自驱改帧号
        不会重新插值——有动画时回退 UI 驱动回路）。
        """
        if not getattr(self, "_drive_active", False):
            return None
        import os as _os
        if _os.environ.get("TOPOS_DISABLE_DRIVE"):
            self._drive_active = False
            return None
        if not self.is_comp_clip_playback_active():
            return None
        if snapshot is not None:
            for ns in snapshot.nodes.values():
                if getattr(ns, "keyframe_tracks", None):
                    self._drive_active = False
                    return None
        import time as _t
        return {
            "start_time": self._comp_play_start_time or _t.perf_counter(),
            "start_frame": self._comp_play_start_frame or 0,
            "fps": max(1.0, self.comp_clip_fps()),
            "in_frame": getattr(self, "_comp_in_frame", None) or 0,
            "out_frame": self._comp_effective_out(),
            "loop": bool(getattr(self, "_comp_loop", True)),
        }

    def _on_bake_staleness_changed(self, entries: list) -> None:
        """M08.1c：烘焙陈旧徽标（渲染线程 plan 审计 → 工具栏）。

        空列表清除徽标；非空显示陈旧/无签名计数，tooltip 逐节点列
        reason（重烘焙/回原图策略可见，不静默播放旧烘焙）。
        """
        badge = getattr(self, "_bake_badge", None)
        if badge is None:
            return
        entries = list(entries or [])
        if not entries:
            badge.hide()
            badge.setText("")
            return
        stale_n = sum(1 for e in entries if e.get("stale") is True)
        unsigned_n = sum(1 for e in entries if e.get("stale") is None)
        parts = []
        if stale_n:
            parts.append(f"{stale_n} 陈旧")
        if unsigned_n:
            parts.append(f"{unsigned_n} 无签名")
        badge.setText(f"⚠ 烘焙 {('，').join(parts)}")
        tips = ["正在消费旧烘焙产物（预览/导出均受影响）："]
        for e in entries:
            state = ("陈旧" if e.get("stale") is True
                     else "无签名（旧工程）")
            tips.append(
                f"· {e.get('bake_node_id')}（目标 {e.get('target_id')}）"
                f"— {state}：{e.get('reason', '')}")
        badge.setToolTip("\n".join(tips))
        badge.show()

    def _on_metadata_ready(self, meta: dict) -> None:
        """Receive format/bbox metadata from renderer and forward to viewport."""
        self._last_metadata = meta
        # M2: 四视图正交纹理 glo（Scene3D 渲染后经 eval_ctx 旁路 + metadata 传递）。
        # 直接转发给 viewer（不经 controller——ortho_views 是 viewer 专用数据）。
        ortho_views = meta.get("ortho_views") if isinstance(meta, dict) else None
        if ortho_views and self._viewer is not None:
            if hasattr(self._viewer, 'set_quad_textures'):
                self._viewer.set_quad_textures(ortho_views)
        elif self._viewer is not None and hasattr(self._viewer, 'clear_quad_textures'):
            is_quad = (hasattr(self._viewer, 'is_quad_view')
                       and self._viewer.is_quad_view())
            if not is_quad:
                # quad 关闭：清掉 viewer 残留。quad 开启但本帧没有新正交纹理时，
                # 保留上一帧 owned textures，避免静态/cached metadata 把三视图清空。
                self._viewer.clear_quad_textures()
        # P0 / Milestone A：转发 render_diagnostics（含 perf_stats）给 viewer debug overlay。
        render_diagnostics = (meta.get("render_diagnostics")
                              if isinstance(meta, dict) else None)
        self._update_model_panels_from_diagnostics(render_diagnostics)
        if self._viewer is not None and render_diagnostics is not None:
            if hasattr(self._viewer, 'set_render_diagnostics'):
                self._viewer.set_render_diagnostics(render_diagnostics)
        # P7：转发 ID picking 信息（{tex_glo, w, h, pick_map}）给 viewer。
        id_pick = meta.get("id_pick") if isinstance(meta, dict) else None
        if self._viewer is not None and isinstance(meta, dict):
            if hasattr(self._viewer, 'set_aov_textures'):
                # The viewer copies these renderer-owned textures into its
                # own context before paintGL; the raw IDs never become UI
                # resource ownership.
                self._viewer.set_aov_textures(meta.get("aov") or {})
            if hasattr(self._viewer, 'set_id_pick_info'):
                self._viewer.set_id_pick_info(id_pick)
        if self._is_active:
            # 不把 viewer 专用纹理/诊断往下游 controller 传
            downstream_meta = ({k: v for k, v in meta.items()
                                if k not in ("ortho_views", "render_diagnostics",
                                              "id_pick", "aov")}
                               if isinstance(meta, dict) else meta)
            self.composite_metadata_ready.emit(downstream_meta)

    def _update_model_panels_from_diagnostics(self, diagnostics: object) -> None:
        """Consume Model3D clip/report summaries emitted by Scene3D."""
        if not isinstance(diagnostics, dict):
            return
        self._last_model_animation_clips = self._animation_clip_specs(
            diagnostics.get("model_animation_clips"))
        self._last_model_import_report = diagnostics.get("model_import_report")
        self._last_model_import_status = str(
            diagnostics.get("model_import_status", "") or "")
        self._last_model_import_phase = str(
            diagnostics.get("model_import_phase", "") or "")
        try:
            self._last_model_import_progress = max(0.0, min(
                1.0, float(diagnostics.get("model_import_progress", 0.0) or 0.0)))
        except (TypeError, ValueError):
            self._last_model_import_progress = 0.0
        raw_import_diagnostics = diagnostics.get("model_import_diagnostics", ())
        self._last_model_import_diagnostics = tuple(
            str(value) for value in raw_import_diagnostics
            if str(value)
        ) if isinstance(raw_import_diagnostics, (list, tuple)) else ()
        raw_resource_estimate = diagnostics.get(
            "model_import_resource_estimate")
        self._last_model_import_resource_estimate = (
            dict(raw_resource_estimate)
            if isinstance(raw_resource_estimate, dict) else None
        )
        raw_import_probe = diagnostics.get("model_import_probe")
        self._last_model_import_probe = (
            dict(raw_import_probe)
            if isinstance(raw_import_probe, dict) else None
        )
        raw_runtime_diagnostics = diagnostics.get("diagnostic_records", ())
        self._last_model_runtime_diagnostics = tuple(
            dict(record) for record in raw_runtime_diagnostics
            if isinstance(record, dict)
        ) if isinstance(raw_runtime_diagnostics, (list, tuple)) else ()
        self._last_model_source_path = str(
            diagnostics.get("model_source_path", "") or "")
        self._last_model_node_id = str(
            diagnostics.get("model_node_id", "") or "")
        hierarchy = diagnostics.get("model_hierarchy")
        self._last_model_hierarchy = hierarchy if isinstance(hierarchy, dict) else None
        if "model_user_hidden_mesh_ids" in diagnostics:
            raw_hidden = diagnostics.get("model_user_hidden_mesh_ids", ())
        else:
            raw_hidden = diagnostics.get("model_hidden_mesh_ids", ())
        if isinstance(raw_hidden, (list, tuple, set)):
            self._last_model_hidden_mesh_ids = tuple(sorted({
                str(value) for value in raw_hidden if str(value)
            }))
        else:
            self._last_model_hidden_mesh_ids = ()
        raw_locked = diagnostics.get("model_locked_mesh_ids", ())
        if isinstance(raw_locked, (list, tuple, set)):
            self._last_model_locked_mesh_ids = tuple(sorted({
                str(value) for value in raw_locked if str(value)
            }))
        else:
            self._last_model_locked_mesh_ids = ()
        self._last_model_solo_mesh_id = str(
            diagnostics.get("model_solo_mesh_id", "") or "")
        self._sync_import_report_for_selected_node()
        self._sync_scene_outliner_for_selected_node()
        raw_materials = diagnostics.get("model_materials", ())
        self._last_model_materials = [dict(item) for item in raw_materials
                                      if isinstance(item, dict)] \
        if isinstance(raw_materials, (list, tuple)) else []
        self._sync_material_panel_for_selected_node()
        report_panel = getattr(self, "_import_report_panel", None)
        if report_panel is not None and hasattr(report_panel, "update_import_state"):
            try:
                report_panel.update_import_state(
                    self._last_model_import_status,
                    self._last_model_import_progress,
                    self._last_model_import_phase,
                )
                if hasattr(report_panel, "update_resource_estimate"):
                    report_panel.update_resource_estimate(
                        self._last_model_import_resource_estimate)
                if hasattr(report_panel, "update_format_probe"):
                    report_panel.update_format_probe(
                        self._last_model_import_probe)
                if hasattr(report_panel, "update_diagnostics"):
                    report_panel.update_diagnostics(
                        self._last_model_runtime_diagnostics)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass

    def _on_error(self, error_msg: str) -> None:
        logger.error(f"Renderer error: {error_msg}")

    def _on_preview_image_ready(self, request_id: int, data: object, width: int, height: int) -> None:
        """Handle async preview image from renderer's non-blocking readback."""
        # PBO readback is asynchronous, so this result often belongs to the
        # most recent *completed* render rather than the newest submission.
        # Only reject temporal reversal; accepting that completed frame keeps
        # the viewer moving under sustained playback load.
        if request_id > 0 and request_id <= getattr(self, "_last_presented_preview_request_id", 0):
            logger.debug(
                f"[COMPOSITE-PRESENT] drop non-monotonic readback request_id={request_id}"
            )
            return
        if request_id > 0:
            self._last_presented_preview_request_id = request_id
        if self._is_active:
            self.composite_preview_image_ready.emit(request_id, data, width, height)

    def _on_gpu_scope_ready(self, frame) -> None:
        """Forward the render-thread GPU scope descriptor while active."""
        if self._is_active:
            self.composite_gpu_scope_ready.emit(frame)

    def _on_node_thumbnail_ready(self, node_id: str, width: int, height: int, data_bytes: bytes) -> None:
        """Handle thumbnail data from the render thread.

        Converts the received bytes to a QPixmap and updates the NodeItem.

        Args:
            node_id: The node ID this thumbnail belongs to
            width: Thumbnail width (64)
            height: Thumbnail height (64)
            data_bytes: Raw BGR float32 data as bytes
        """
        import numpy as np
        from PyQt6.QtGui import QImage, QPixmap

        try:
            # Convert bytes back to numpy array
            data = np.frombuffer(data_bytes, dtype=np.float32).reshape(height, width, 3)

            # Renderer readback is scene-linear BGR.  Encode it with the same
            # display transform used by CompositeViewer before showing it in
            # the node card; otherwise linear pinks become dark saturated red.
            display_settings = self._sync_thumbnail_display_settings()
            rgb = data[:, :, [2, 1, 0]]  # BGR -> RGB
            rgb8 = (
                linear_rgb_to_display(rgb, **display_settings) * 255.0
            ).astype(np.uint8)

            # Create QImage
            bytes_per_line = 3 * width
            qimg = QImage(rgb8.data.tobytes(), width, height, bytes_per_line, QImage.Format.Format_RGB888)

            if qimg.isNull():
                return

            # Convert to QPixmap and update NodeItem
            pixmap = QPixmap.fromImage(qimg)
            item = self._node_editor.get_node_item(node_id)
            if item:
                item.set_thumbnail(pixmap)

            # Also update thumbnail manager if available
            if self._thumbnail_manager:
                self._thumbnail_manager.update_thumbnail_from_data(node_id, data)

        except Exception as e:
            logger.warning(f"Thumbnail processing error: {e}")

    def _request_thumbnails_deferred(self) -> None:
        """Wrapper that clears the pending flag before requesting thumbnails."""
        self._thumbnail_pending = False
        self._request_thumbnails_for_visible_nodes()

    def _request_thumbnails_for_visible_nodes(self) -> None:
        """Request thumbnails for nodes in the viewport or selected.

        Delegates the throttling decision to ``compute_thumbnail_requests``
        (pure function in thumbnail_policy.py) and submits the resulting
        node IDs to the renderer.

        Throttling rules:
        - During playback: skip entirely (unless policy allows it).
        - During parameter interaction: only selected node, max 5 fps.
        - During idle: visible nodes, max 10 fps, max 4 per tick.
        - Priority: selected > other visible.
        - Cache-key check: skip nodes whose output hasn't changed.
        - Main-view priority: if a frame render is pending, delay thumbnails.
        """
        if not self._renderer:
            return

        # Keep cached thumbnails invalidated when the viewer's ODT changes
        # (for example, switching from sRGB stills to Rec.709 video).
        self._sync_thumbnail_display_settings()

        # Gather inputs
        visible_node_ids = self._node_editor.get_nodes_in_viewport()
        selected_node_id = self._node_editor.get_selected_node_id()
        if selected_node_id and selected_node_id not in visible_node_ids:
            visible_node_ids.append(selected_node_id)

        renderer_cache_keys = {}
        if hasattr(self._renderer, '_node_cache_keys'):
            renderer_cache_keys = self._renderer._node_cache_keys

        render_pending = (self._last_submitted_request_id > 0
                          and self._last_texture_id is None)

        # Delegate to pure function
        nodes = compute_thumbnail_requests(
            self._thumbnail_policy,
            self._thumbnail_state,
            is_interacting=self._interaction_active,
            render_pending=render_pending,
            visible_node_ids=visible_node_ids,
            selected_node_id=selected_node_id,
            renderer_cache_keys=renderer_cache_keys,
        )

        if not nodes:
            return

        # Submit thumbnail requests
        for node_id in nodes:
            self._renderer.submit(CompositeCommand.READBACK_NODE_THUMBNAIL, {
                'node_id': node_id,
                'thumbnail_size': 180,
            })
            # Record cache key so we can skip unchanged nodes next tick
            ck = renderer_cache_keys.get(node_id)
            if ck is not None:
                self._thumbnail_state.last_cache_key[node_id] = ck

        self._thumbnail_state.last_request_at = time.monotonic()

    def _get_thumbnail_display_settings(self) -> dict:
        """Read the active CompositeViewer ODT for thumbnail conversion."""
        viewer = self._viewer
        return {
            "transfer": int(getattr(viewer, "_display_transfer", 1)),
            "exposure": float(getattr(viewer, "_exposure", 0.0)),
            "display_primaries": int(getattr(viewer, "_display_primaries", 0)),
            "tonemap": int(getattr(viewer, "_tonemap", 0)),
            "target_peak": float(getattr(viewer, "_target_peak", 100.0)),
            "viewer_gamma": float(getattr(viewer, "_viewer_gamma", 1.0)),
        }

    def _sync_thumbnail_display_settings(self) -> dict:
        """Synchronize thumbnail caches with the viewer's display ODT."""
        settings = self._get_thumbnail_display_settings()
        if settings != self._thumbnail_display_settings:
            self._thumbnail_display_settings = settings
            if self._thumbnail_manager:
                self._thumbnail_manager.set_display_settings(**settings)
            # Cache keys describe rendered linear data, not its display ODT.
            # Force a fresh thumbnail request after a display-space change.
            self._thumbnail_state.last_cache_key.clear()
        return settings

    def _update_perf_badges(self) -> None:
        """Read perf snapshot and update NodeItem badges + perf panel."""
        if not self._renderer or not self._renderer.perf_monitor:
            return
        snapshot = self._renderer.perf_monitor.get_snapshot()
        if snapshot is None:
            return

        for node_id, node_stats in snapshot.nodes.items():
            item = self._node_editor._node_items.get(node_id)
            if item:
                item.set_perf_badge(
                    eval_time_ms=node_stats.eval_time_ms,
                    was_cached=node_stats.was_cached,
                    error=node_stats.error,
                )

        # Emit perf data for panel consumption
        if hasattr(self, '_perf_panel') and self._perf_panel:
            history_provider = getattr(self._renderer.perf_monitor,
                                       'get_history', None)
            if callable(getattr(self._perf_panel,
                                'set_history_provider', None)):
                self._perf_panel.set_history_provider(
                    history_provider if callable(history_provider) else None)
            self._perf_panel.update_from_snapshot(snapshot)

    # ── Properties ──

    @property
    def renderer(self):
        return self._renderer

    @property
    def node_editor(self):
        return self._node_editor

    @property
    def node_graph(self):
        return self._node_graph

    @property
    def undo_stack(self):
        return self._undo_stack

    @property
    def properties_panel(self):
        return getattr(self, '_properties_panel', None)

    @properties_panel.setter
    def properties_panel(self, panel) -> None:
        self._properties_panel = panel
        if panel:
            panel.parameter_changed.connect(self._on_parameter_changed)
            # B3-2: "Promote to Group" on numeric parameter rows.
            if hasattr(panel, "set_group_promote_hook"):
                panel.set_group_promote_hook(self._promote_param_to_group)
            panel.tracker_analyze_requested.connect(self._on_tracker_analyze_requested)
            panel.tracker_bake_requested.connect(self._on_tracker_bake_requested)
            panel.tracker_cancel_requested.connect(self._on_tracker_cancel_requested)
            panel.tracker_import_color_requested.connect(
                self._on_tracker_import_color_requested)
            panel.tracker_stabilize_requested.connect(
                self._on_tracker_stabilize_requested)
            panel.parameters_changed_batch.connect(self._on_parameters_changed_batch)
            # Connect new section-based signals
            if hasattr(panel, 'node_enabled_toggled'):
                panel.node_enabled_toggled.connect(self._on_props_node_enabled_toggled)
            if hasattr(panel, 'node_color_changed'):
                panel.node_color_changed.connect(self._on_props_node_color_changed)
            # P6/A-4: keyframe changes from the properties panel ◆ button must
            # reach the graph (re-evaluate) and the undo stack. The per-node
            # track itself is already written by the panel via its NodeGraph
            # reference; this slot handles re-render + undo + save-dirty.
            if hasattr(panel, 'keyframes_changed'):
                panel.keyframes_changed.connect(self._on_props_keyframes_changed)
            # AE-style expression edits share the same graph, render and undo
            # path as keyframes. The panel only emits validated text; this
            # slot owns the authoritative NodeGraph mutation.
            if hasattr(panel, 'expression_changed'):
                panel.expression_changed.connect(self._on_props_expression_changed)
            # P1: dragging the playhead inside the KeyframeEditorWidget dialog
            # seeks the composite viewport so the user can scrub the animation
            # while editing. Mirrors how the KeyframesPanel/SplinePanel docks
            # already drive set_composite_current_frame.
            if hasattr(panel, 'current_frame_changed'):
                panel.current_frame_changed.connect(self.set_composite_current_frame)
            # P6: give the panel access to the live NodeGraph so ◆ reads/writes
            # the authoritative per-node keyframe_tracks.
            if self._node_graph is not None and hasattr(panel, 'set_node_graph'):
                panel.set_node_graph(self._node_graph)
            # Output node "Render Output" button
            if hasattr(panel, 'render_output_requested'):
                panel.render_output_requested.connect(self.render_output_requested)
            # Scene3D 参数面板的 2D/3D 预览模式开关（纯视图状态）
            if hasattr(panel, 'view_mode_toggle_requested'):
                panel.view_mode_toggle_requested.connect(self._on_view_mode_toggle_requested)
            if hasattr(panel, 'look_through_camera_requested'):
                panel.look_through_camera_requested.connect(self._on_look_through_camera_requested)
            if hasattr(panel, 'camera_activation_toggled'):
                panel.camera_activation_toggled.connect(self._on_camera_activation_toggled)
            if hasattr(panel, 'project_settings_requested'):
                panel.project_settings_requested.connect(self._on_project_settings_requested)
            if hasattr(panel, 'viewer3d_settings_requested'):
                panel.viewer3d_settings_requested.connect(self._on_viewer3d_settings_requested)

    def _on_props_node_enabled_toggled(self, node_id: str, enabled: bool) -> None:
        """Handle enable/disable toggle from properties panel section header."""
        if _page_node_is_locked(self, node_id):
            return
        self._node_graph.set_node_disabled(node_id, not enabled)
        # Sync the visual node item state
        item = self._node_editor._node_items.get(node_id)
        if item:
            item._disabled = not enabled
            item.update()
        # U-07：属性面板启停与 H 快捷键共用同一撤销命令
        self._push_node_enabled_undo({node_id: not enabled})
        self._evaluate_graph()

    def _on_props_node_color_changed(self, node_id: str, color: str) -> None:
        """Handle color change from properties panel section header.

        P6/A-11: persist the color on the node instance (custom_header_color)
        so it survives save/reload, not just the runtime header_color. Also
        refresh the Keyframes panel so its range bars / row swatches update
        live without re-selecting the node (UI-16/F-16).
        """
        if _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node:
            old_color = node.custom_header_color
            node.header_color = color
            node.custom_header_color = color
            # Mark graph dirty so the change is saved.
            self._emit_user_edit_once()
            # Push undo so the color change is reversible.
            self._push_node_color_undo(node_id, old_color, color)
        # Update visual node item
        item = self._node_editor._node_items.get(node_id)
        if item:
            from PyQt6.QtGui import QColor
            item.header_color = QColor(color)
            item.update()
        # A-11: refresh the Keyframes panel so range bars + row swatches
        # follow the new color immediately (UI-16).
        self._refresh_keyframes_for_color(node_id)

    def _on_props_keyframes_changed(self, node_id: str, node_type: str,
                                    param_name: str, keyframes: list) -> None:
        """Handle keyframe changes from the properties panel ◆ button.

        P6/A-4: the per-node track has already been written by the panel (via
        its NodeGraph reference). This slot is responsible for:
          - pushing an undo action
          - marking the graph dirty (save)
          - requesting a re-render so the preview reflects the new animation
        """
        if _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return

        # P2: the properties panel stashed the BEFORE track before writing.
        import copy
        before = None
        stash = getattr(self._properties_panel, '_pending_keyframe_before', {})
        key = (node_id, param_name)
        if key in stash:
            before = stash.pop(key)
        after = copy.deepcopy(node.keyframe_tracks.get(param_name))
        self._push_keyframe_track_undo(node_id, param_name, before, after)

        # Save-dirty + re-render
        self._emit_user_edit_once()
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(node_id)
        # P2: keep transport-bar markers in sync with the edited track.
        self.refresh_transport_keyframe_markers()

    def _on_props_expression_changed(self, node_id: str, param_name: str,
                                     expression: Optional[str]) -> None:
        """Apply/clear a panel expression with a complete undo record.

        Setting an expression intentionally removes a keyframe track on the
        same parameter (and vice versa). The undo payload therefore stores both
        pieces of state so switching between keyframes and expressions is fully
        reversible, just like AE's property animation controls.
        """
        if _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return

        before = {
            "expression": node.get_expression(param_name),
            "track": copy.deepcopy(node.keyframe_tracks.get(param_name)),
        }

        text = (expression or "").strip()
        if text:
            success, error = self._node_graph.set_node_expression(
                node_id, param_name, text)
            if not success:
                logger.warning("Failed to set expression %s.%s: %s",
                               node_id, param_name, error)
                self._refresh_properties_panel(node_id)
                return
        else:
            self._node_graph.remove_node_expression(node_id, param_name)

        after = {
            "expression": node.get_expression(param_name),
            "track": copy.deepcopy(node.keyframe_tracks.get(param_name)),
        }
        if before == after:
            return

        self._undo_stack.push(UndoAction(
            "expression_change",
            {"node_id": node_id, "param_name": param_name,
             "before": before, "after": after},
            undo_fn=lambda data: self._restore_expression_state(data, "before"),
            redo_fn=lambda data: self._restore_expression_state(data, "after"),
            description=f"expression {param_name}",
        ))
        self._emit_user_edit_once()
        self._refresh_properties_panel(node_id)
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _restore_expression_state(self, data: dict, which: str) -> None:
        """Restore an expression/track pair during undo or redo."""
        node_id = data["node_id"]
        param_name = data["param_name"]
        desired = data[which]
        node = self._node_graph.get_node(node_id)
        if node is None:
            return

        current = node.get_expression(param_name)
        target_expression = desired.get("expression")
        if current and current != target_expression:
            self._node_graph.remove_node_expression(node_id, param_name)
        if target_expression and node.get_expression(param_name) != target_expression:
            success, error = self._node_graph.set_node_expression(
                node_id, param_name, target_expression)
            if not success:
                logger.warning("Failed to restore expression %s.%s: %s",
                               node_id, param_name, error)
                return
        elif not target_expression and node.has_expression(param_name):
            self._node_graph.remove_node_expression(node_id, param_name)

        # An expression and a keyframe track are mutually exclusive. Restore
        # the serialized track only for the non-expression state.
        target_track = desired.get("track")
        if target_expression:
            if param_name in node.keyframe_tracks:
                node.remove_keyframe_track(param_name)
        else:
            self._restore_keyframe_track(
                {"node_id": node_id, "param_name": param_name,
                 "target": target_track}, "target")
            return  # helper already refreshes and evaluates

        self._refresh_properties_panel(node_id)
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _on_graph_expression_changed(self, node_id: str, param_name: str,
                                     text: str) -> None:
        """T120/Bug-1: apply a Graph Expression Editor commit through the SAME
        authoritative expression/undo path as the properties panel ◆ button.
        This guarantees the edit is reversible (the old panel path wrote the
        node directly with no undo record). Validation failure is reported
        back to the Keyframes panel's editor so the user keeps their WIP text.
        """
        if self._node_graph is None:
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        stripped = (text or "").strip()
        # Pre-validate WITHOUT mutating so a rejected expression never touches
        # the node. Empty text always "validates" (it means clear).
        if stripped:
            try:
                from src.features.composite.utils.expression_parser import \
                    ExpressionParser
                result = ExpressionParser().validate(stripped)
                if not result.success:
                    error = result.error.message if result.error else \
                        "Invalid expression"
                    kp = getattr(self, "_keyframes_panel", None)
                    if kp is not None and hasattr(kp,
                                                  "_on_graph_expression_failed"):
                        kp._on_graph_expression_failed(node_id, param_name,
                                                       error)
                    return
            except Exception as exc:
                kp = getattr(self, "_keyframes_panel", None)
                if kp is not None and hasattr(kp, "_on_graph_expression_failed"):
                    kp._on_graph_expression_failed(node_id, param_name, str(exc))
                return
        # Delegate the authoritative write + undo record + re-render to the
        # shared properties-panel slot so both paths behave identically.
        self._on_props_expression_changed(node_id, param_name, stripped or None)

    def _on_graph_expression_enabled_changed(self, node_id: str,
                                              param_name: str,
                                              enabled: bool) -> None:
        """Toggle expression evaluation with undo and shared refresh."""
        if self._node_graph is None or _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node is None or not node.has_expression(param_name):
            return
        before = node.is_expression_enabled(param_name)
        success, error = self._node_graph.set_node_expression_enabled(
            node_id, param_name, bool(enabled))
        if not success:
            logger.warning("Failed to toggle expression %s.%s: %s",
                           node_id, param_name, error)
            self._refresh_keyframes_and_spline_for_node(node_id)
            return
        after = node.is_expression_enabled(param_name)
        if before == after:
            return
        self._undo_stack.push(UndoAction(
            "expression_enabled_change",
            {"node_id": node_id, "param_name": param_name,
             "before": before, "after": after},
            undo_fn=lambda data: self._restore_expression_enabled_state(
                data, "before"),
            redo_fn=lambda data: self._restore_expression_enabled_state(
                data, "after"),
            description=f"expression {'enable' if after else 'disable'} {param_name}",
        ))
        self._emit_user_edit_once()
        self._refresh_properties_panel(node_id)
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _restore_expression_enabled_state(self, data: dict, which: str) -> None:
        """Undo/redo helper for the expression enabled switch."""
        if self._node_graph is None:
            return
        self._node_graph.set_node_expression_enabled(
            data["node_id"], data["param_name"], bool(data[which]))
        self._refresh_properties_panel(data["node_id"])
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(data["node_id"])

    def _push_node_color_undo(self, node_id: str, old_color: Optional[str],
                              new_color: str) -> None:
        """Push an undo record for a node color change (A-11)."""
        try:
            self._undo_stack.push(
                self._undo_node_color,
                {"node_id": node_id, "old": old_color, "new": new_color},
            )
        except Exception as e:
            logger.warning("Undo push failed for node color change: %s", e)

    def _undo_node_color(self, data) -> None:
        node = self._node_graph.get_node(data["node_id"])
        if node is None:
            return
        color = data.get("old")
        if color:
            node.header_color = color
            node.custom_header_color = color
        else:
            # Restore class default
            node.custom_header_color = None
            node.header_color = type(node).header_color
        item = self._node_editor._node_items.get(data["node_id"])
        if item:
            from PyQt6.QtGui import QColor
            item.header_color = QColor(node.header_color)
            item.update()
        # A-11: keep the Keyframes panel color in sync on undo/redo too.
        self._refresh_keyframes_for_color(data["node_id"])
        self._evaluate_graph()

    def _refresh_keyframes_for_color(self, node_id: str) -> None:
        """Refresh Keyframes and Spline after a node's color change.

        A-11/UI-16/P5-1: both timeline surfaces update their row colors.
        """
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _push_keyframe_track_undo(self, node_id: str, param_name: str,
                                  before: Optional[dict], after: Optional[dict]) -> None:
        """Push a real before/after undo record for a keyframe track change (P2).

        ``before``/``after`` are deep-copied serialized track dicts (or None if
        the track didn't / doesn't exist). Undo restores ``before``; redo
        restores ``after``. Replaces the old placeholder that only re-evaluated.
        """
        import copy
        try:
            self._undo_stack.push(UndoAction(
                action_type="keyframe_track_change",
                data={
                    "node_id": node_id,
                    "param_name": param_name,
                    "before": copy.deepcopy(before),
                    "after": copy.deepcopy(after),
                },
                undo_fn=lambda d: self._restore_keyframe_track(d, "before"),
                redo_fn=lambda d: self._restore_keyframe_track(d, "after"),
                description=f"keyframe {param_name}",
            ))
        except Exception as e:
            logger.warning("Undo push failed for keyframe track: %s", e)

    def _restore_keyframe_track(self, data: dict, which: str) -> None:
        """Apply the ``before`` or ``after`` track snapshot to the live node."""
        node = self._node_graph.get_node(data["node_id"])
        if node is None:
            return
        param_name = data["param_name"]
        track_data = data.get(which)
        if track_data is None:
            node.remove_keyframe_track(param_name)
        else:
            from src.shared.keyframe import keyframe_track_from_dict
            pdef = node.get_param_defs().get(param_name)
            track = keyframe_track_from_dict(
                {
                    **track_data,
                    "param_name": param_name,
                    "value_type": getattr(pdef, "ptype", "") or track_data.get(
                        "value_type", "scalar"),
                    "min_val": track_data.get(
                        "min_val", pdef.min_val if pdef else 0.0),
                    "max_val": track_data.get(
                        "max_val", pdef.max_val if pdef else 1.0),
                },
                value_type=getattr(pdef, "ptype", None),
            )
            node.set_keyframe_track(param_name, track)
        self._evaluate_graph()
        self._refresh_properties_panel(data["node_id"])
        self._refresh_keyframes_and_spline_for_node(data["node_id"])

    @staticmethod
    def _video_trim_snapshot(node: Any) -> dict:
        """Capture the persisted video trim state for one undo step."""
        return {
            "source_in": int(node.parameters.get("source_in", 0) or 0),
            "source_out": int(node.parameters.get("source_out", -1)),
            "time_range": copy.deepcopy(getattr(node, "time_range", None)),
        }

    @staticmethod
    def _video_trim_bounds(node: Any) -> Tuple[int, int, int]:
        """Return ``(source_in, effective_out, max_source_frame)``."""
        try:
            total = max(0, int(node.parameters.get("total_frames", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            total = 0
        try:
            source_in = max(0, int(node.parameters.get("source_in", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            source_in = 0
        try:
            raw_out = int(node.parameters.get("source_out", -1))
        except (AttributeError, TypeError, ValueError):
            raw_out = -1
        # Frame indices are zero-based: a one-frame source has only frame 0.
        # Keep an explicitly persisted out point in the bounds as a recovery
        # path for projects whose media metadata is not loaded yet.
        max_frame = max(0, total - 1, source_in, raw_out)
        effective_out = max_frame if raw_out < 0 else max(source_in, raw_out)
        return source_in, min(effective_out, max_frame), max_frame

    @classmethod
    def _apply_video_trim_range(cls, node: Any, start: int, end: int) -> None:
        """Apply a source trim and make the node layer span the same length."""
        _, _, maximum = cls._video_trim_bounds(node)
        start = max(0, min(int(start), maximum))
        end = max(start, min(int(end), maximum))
        node.set_parameter("source_in", start)
        node.set_parameter("source_out", end)

        current = (copy.deepcopy(node.time_range)
                   if isinstance(node.time_range, dict) else {})
        try:
            comp_start = int(current.get("comp_start", 0) or 0)
        except (TypeError, ValueError):
            comp_start = 0
        current.update({
            "enabled": True,
            "comp_start": comp_start,
            "comp_end": comp_start + end - start,
            "source_in": start,
            "source_out": end,
        })
        node.time_range = current

    @classmethod
    def _sync_video_trim_layer(cls, node: Any) -> None:
        """Recompute the layer end after a direct source_in/source_out edit."""
        start, end, _ = cls._video_trim_bounds(node)
        current = (copy.deepcopy(node.time_range)
                   if isinstance(node.time_range, dict) else {})
        try:
            comp_start = int(current.get("comp_start", 0) or 0)
        except (TypeError, ValueError):
            comp_start = 0
        current.update({
            "enabled": True,
            "comp_start": comp_start,
            "comp_end": comp_start + end - start,
            "source_in": start,
            "source_out": end,
        })
        node.time_range = current

    @classmethod
    def _restore_video_trim_snapshot(cls, node: Any, snapshot: dict) -> None:
        """Restore source trim and its linked layer range during undo/redo."""
        node.set_parameter("source_in", int(snapshot.get("source_in", 0)))
        node.set_parameter("source_out", int(snapshot.get("source_out", -1)))
        node.time_range = copy.deepcopy(snapshot.get("time_range"))

    def _on_video_trim_parameter_changed(self, node_id: str,
                                         value: object) -> None:
        """Handle the combined Trim row as one debounced undoable edit."""
        if _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node is None or node.node_type != "video_frame":
            return
        try:
            start, end = value  # type: ignore[misc]
            start, end = int(start), int(end)
        except (TypeError, ValueError):
            return
        before = self._video_trim_snapshot(node)
        self._apply_video_trim_range(node, start, end)
        after = self._video_trim_snapshot(node)
        # AUD-9502：trim 与普通参数共用同一累加器，归属不一致时先 flush。
        pending = self._pending_param_change
        if pending is not None and (pending[0], pending[1]) != (node_id, "trim_range"):
            self._flush_param_changes()
        if self._pending_param_change is None:
            self._pending_param_change = (node_id, "trim_range", before, after)
            self._undo_stack.begin_merge_coalescing()
        else:
            _, _, original, _ = self._pending_param_change
            self._pending_param_change = (node_id, "trim_range", original, after)
        self._param_debounce_timer.start()
        self._evaluate_graph()
        # The TrimRangeEditor already updates its own handles and fields. Do
        # not rebuild the Properties panel on every mouse-move: replacing the
        # widget here would cancel the active drag after its first tick.
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _on_parameter_changed(self, node_id: str, param_name: str,
                              value: Any) -> None:
        """Handle parameter change from properties panel.

        Uses a debounce timer to coalesce rapid slider ticks into a single
        undo action and render submission. Only the first old_value is kept
        so undo restores the value from before the drag started.

        Also implements merge coalescing mode for continuous parameter changes
        (e.g., slider dragging). Consecutive changes within merge window are
        merged into a single undo record.

        Also implements prefetch system: records parameter value history
        for trend analysis and triggers prefetch during debounce period.
        """
        if _page_node_is_locked(self, node_id):
            return
        self._emit_user_edit_once()
        node = self._node_graph.get_node(node_id)
        if not node:
            return

        if node.node_type == "video_frame" and param_name == "trim_range":
            self._on_video_trim_parameter_changed(node_id, value)
            return

        old_value = node.parameters.get(param_name)
        self._node_graph.set_node_parameter(node_id, param_name, value)
        if (node.node_type == "video_frame"
                and param_name in ("source_in", "source_out")):
            self._sync_video_trim_layer(node)
        self._sync_active_viewer_camera_if_needed(node_id, node)

        # Constant's Keyframes rows are mode-dependent.  Changing the mode in
        # Properties changes the graph immediately, so rebuild the animation
        # model at the same boundary; otherwise the dock keeps the previous
        # mode's stale parameter rows until another unrelated refresh.
        if param_name == "mode" and node.node_type == "constant":
            self._refresh_keyframes_and_spline_for_node(node_id)

        # Accumulate: keep the *original* old_value, update new_value
        # AUD-9502：累加前验证归属。80ms 防抖窗口内若发生第二个参数（或
        # 另一节点）的修改，必须先 flush 旧条目再开新条目——否则新元组会
        # 沿用旧参数的 old_value（undo 把新参数写成旧参数的值，图状态损坏），
        # 且首个参数的真实修改永远丢失 undo 记录。
        pending = self._pending_param_change
        if pending is not None and (pending[0], pending[1]) != (node_id, param_name):
            self._flush_param_changes()
        # U-03：batch/single pending 互斥——单参数修改进入时先提交批量
        # pending，保证两类事务的提交顺序与用户操作顺序一致。
        # （__new__ 桩页面上 Qt 包装的属性查找会抛 RuntimeError，
        # __dict__ 读取与文件内其他防御读一致。）
        if self.__dict__.get("_pending_batch_param_change") is not None:
            self._flush_batch_param_changes()
        if self._pending_param_change is None:
            # First tick of a new drag — record the original value
            self._pending_param_change = (node_id, param_name, old_value, value)
            # Begin merge coalescing mode (for slider dragging)
            self._undo_stack.begin_merge_coalescing()
        else:
            # Subsequent ticks — keep original old, update new
            _, _, orig_old, _ = self._pending_param_change
            self._pending_param_change = (node_id, param_name, orig_old, value)

        # ── Prefetch system: record trend and schedule prefetch ──
        if self._prefetch_enabled and isinstance(value, (int, float)):
            import time
            self._prefetch_scheduler.record_param_change(
                node_id, param_name, float(value), time.monotonic()
            )
            # Start prefetch timer (fires during debounce period)
            self._prefetch_timer.start()

        # Restart undo debounce timer.
        self._param_debounce_timer.start()
        # 3D transform/camera/light parameters need true live redraw while
        # scrubbing; ordinary image parameters keep the regular render debounce.
        if self._is_realtime_3d_transform_preview(node, {param_name: value}):
            self._force_next_view_refresh = True
            self._evaluate_graph_now()
        else:
            self._evaluate_graph()
        # Sync on-canvas control handles (e.g. transform gizmo) every tick so
        # they follow the value live, not only after the debounce flush.
        self.update_viewer_handles()

    def _undo_param(self, data) -> None:
        node = self._node_graph.get_node(data["node_id"])
        if node is not None and data["param"] == "trim_range":
            self._restore_video_trim_snapshot(node, data["old"])
        else:
            self._node_graph.set_node_parameter(
                data["node_id"], data["param"], data["old"])
            if (node is not None and node.node_type == "video_frame"
                    and data["param"] in ("source_in", "source_out")):
                self._sync_video_trim_layer(node)
        self._refresh_properties_panel(data["node_id"])
        if data["param"] == "trim_range":
            self._refresh_keyframes_and_spline_for_node(data["node_id"])
        self._evaluate_graph()
        # Update viewer handles to sync with new value (S5-14)
        self.update_viewer_handles()

    def _redo_param(self, data) -> None:
        node = self._node_graph.get_node(data["node_id"])
        if node is not None and data["param"] == "trim_range":
            self._restore_video_trim_snapshot(node, data["new"])
        else:
            self._node_graph.set_node_parameter(
                data["node_id"], data["param"], data["new"])
            if (node is not None and node.node_type == "video_frame"
                    and data["param"] in ("source_in", "source_out")):
                self._sync_video_trim_layer(node)
        self._refresh_properties_panel(data["node_id"])
        if data["param"] == "trim_range":
            self._refresh_keyframes_and_spline_for_node(data["node_id"])
        self._evaluate_graph()
        # Update viewer handles to sync with new value (S5-14)
        self.update_viewer_handles()

    def _flush_param_changes(self) -> None:
        """Flush the accumulated parameter change as a single undo action.

        Also checks if prefetch prediction was accurate (hit) and
        clears prefetch cache if prediction was incorrect.

        Ends merge coalescing mode to finalize the undo record.

        Also updates viewer handles to sync with new parameter values (S5-14).
        """
        if self._pending_param_change is None:
            # End merge coalescing mode even if no pending change
            self._undo_stack.end_merge_coalescing()
            return

        node_id, param_name, old_value, new_value = self._pending_param_change
        self._pending_param_change = None

        # End merge coalescing mode
        self._undo_stack.end_merge_coalescing()

        # Stop prefetch timer
        self._prefetch_timer.stop()

        # Check if prefetch prediction was accurate
        if self._active_prefetch_prediction is not None:
            prediction_hit = self._prefetch_scheduler.check_prediction_hit(
                node_id, param_name, float(new_value) if isinstance(new_value, (int, float)) else 0.0
            )
            self._active_prefetch_prediction = None

            # If prediction was incorrect, clear prefetch cache to free memory
            if not prediction_hit and self._renderer:
                self._renderer.submit(CompositeCommand.CLEAR_PREFETCH_CACHE, {})

        if old_value == new_value:
            return

        # Update viewer handles to sync with new value (S5-14)
        self.update_viewer_handles()

        # Use factory function to create mergeable param_change action
        action = create_param_change_action(
            node_id=node_id,
            param_name=param_name,
            old_value=old_value,
            new_value=new_value,
            undo_fn=lambda d: self._undo_param(d),
            redo_fn=lambda d: self._redo_param(d),
            merge_window_ms=500,  # Merge within 500ms
        )
        self._undo_stack.push(action)

    # ── Multi-Selection Parameter Change ──

    def _on_parameters_changed_batch(self, node_ids: list, param_name: str,
                                      new_value: object, old_values: dict) -> None:
        """Handle batch parameter change for multiple selected nodes.

        Uses a debounce timer to coalesce rapid slider ticks into a single
        undo action and render submission.

        Args:
            node_ids: List of node IDs affected
            param_name: Parameter name
            new_value: New parameter value
            old_values: Dict mapping node_id to old value
        """
        if (not node_ids
                or any(_page_node_is_locked(self, node_id)
                       for node_id in node_ids)):
            return

        # 更新所有节点的参数值
        for node_id in node_ids:
            self._node_graph.set_node_parameter(node_id, param_name, new_value)

        # Accumulate batch changes for undo
        # U-03（AUD 批量归属）：80ms 防抖窗口内若第二个参数（或另一节点
        # 集合）进入，必须先 flush 旧条目再开新条目——否则新元组沿用旧
        # 参数的 old_values，undo 会把旧参数的原值写进新参数（与单参数
        # 路径 AUD-9502 同一契约）。batch/single pending 亦互斥。
        pending = self._pending_batch_param_change
        if pending is not None:
            pending_ids, pending_param, _, _ = pending
            if (pending_param != param_name
                    or tuple(sorted(pending_ids)) != tuple(sorted(node_ids))):
                self._flush_batch_param_changes()
        # 同上：__dict__ 读防御 __new__ 桩页面。
        if self.__dict__.get("_pending_param_change") is not None:
            self._flush_param_changes()

        if self._pending_batch_param_change is None:
            # First tick of a new drag — record the original values
            import copy as _copy
            self._pending_batch_param_change = (
                list(node_ids), param_name,
                _copy.deepcopy(old_values), new_value)
        else:
            # Subsequent ticks — keep original old_values, update new_value
            _, _, orig_old, _ = self._pending_batch_param_change
            self._pending_batch_param_change = (node_ids, param_name, orig_old, new_value)

        # Restart debounce timer
        self._batch_param_debounce_timer.start()
        if self._is_realtime_3d_batch_param_preview(node_ids, param_name):
            self._force_next_view_refresh = True
            self._evaluate_graph_now()
        else:
            self._evaluate_graph()
        # Keep on-canvas control handles in sync during the drag.
        self.update_viewer_handles()

    def _is_realtime_3d_batch_param_preview(self, node_ids: list, param_name: str) -> bool:
        """Return True when a multi-node parameter scrub should redraw 3D now."""
        if not self._node_graph:
            return False
        for node_id in node_ids:
            node = self._node_graph.get_node(node_id)
            if node and self._is_realtime_3d_transform_preview(node, {param_name: None}):
                return True
        return False

    def _undo_batch_param(self, data) -> None:
        """Undo batch parameter change for multiple nodes."""
        node_ids = data["node_ids"]
        param_name = data["param"]
        old_values = data["old_values"]

        for node_id in node_ids:
            # U-03：按“键存在”精确恢复。old_values 显式记录的 None 也
            # 是合法原值（参数原为 None/未设置），用 `is not None` 判断
            # 会把这类参数静默留在新值上。
            if node_id in old_values:
                self._node_graph.set_node_parameter(
                    node_id, param_name, old_values.get(node_id))

        # Refresh properties panel
        if hasattr(self, '_properties_panel') and self._properties_panel:
            nodes_data = []
            for node_id in node_ids:
                node = self._node_graph.get_node(node_id)
                if node:
                    nodes_data.append({
                        'node_id': node_id,
                        'node_type': node.node_type,
                        'node_title': node.node_title,
                        'parameters': node.parameters,
                        'enabled': not node.disabled,
                        'node_color': getattr(node, 'header_color', '#666666'),
                        'read_only': _page_node_is_locked(self, node_id),
                    })
            self._properties_panel.set_nodes(nodes_data)

        self._evaluate_graph()

    def _redo_batch_param(self, data) -> None:
        """Redo batch parameter change for multiple nodes."""
        node_ids = data["node_ids"]
        param_name = data["param"]
        new_value = data["new"]

        for node_id in node_ids:
            self._node_graph.set_node_parameter(node_id, param_name, new_value)

        # Refresh properties panel
        if hasattr(self, '_properties_panel') and self._properties_panel:
            nodes_data = []
            for node_id in node_ids:
                node = self._node_graph.get_node(node_id)
                if node:
                    nodes_data.append({
                        'node_id': node_id,
                        'node_type': node.node_type,
                        'node_title': node.node_title,
                        'parameters': node.parameters,
                        'enabled': not node.disabled,
                        'node_color': getattr(node, 'header_color', '#666666'),
                        'read_only': _page_node_is_locked(self, node_id),
                    })
            self._properties_panel.set_nodes(nodes_data)

        self._evaluate_graph()

    def _flush_batch_param_changes(self) -> None:
        """Flush the accumulated batch parameter change as a single undo action."""
        if self._pending_batch_param_change is None:
            return

        node_ids, param_name, old_values, new_value = self._pending_batch_param_change
        self._pending_batch_param_change = None

        # Check if any values actually changed
        has_change = False
        for node_id in node_ids:
            old_val = old_values.get(node_id)
            if old_val != new_value:
                has_change = True
                break

        if not has_change:
            return

        # Create undo action for batch change
        self._undo_stack.push(UndoAction(
            "batch_param_change",
            {
                "node_ids": node_ids,
                "param": param_name,
                "old_values": old_values,
                "new": new_value
            },
            lambda d: self._undo_batch_param(d),
            lambda d: self._redo_batch_param(d),
            f"Change {param_name} for {len(node_ids)} nodes"
        ))

    # ── Prefetch System ──

    def _on_prefetch_timeout(self) -> None:
        """Prefetch timer timeout — analyze trend and trigger prefetch.

        Called 40ms after the first parameter change during a slider drag,
        before the debounce timer fires (80ms). This allows prefetching
        the predicted value during the user's interaction.
        """
        if not self._prefetch_enabled or self._pending_param_change is None:
            return

        node_id, param_name, old_value, new_value = self._pending_param_change

        # Only prefetch for numeric parameters
        if not isinstance(new_value, (int, float)):
            return

        # Get parameter bounds from node's ParamDef for accurate clamping
        node = self._node_graph.get_node(node_id)
        param_bounds = PrefetchScheduler.get_param_bounds(node, param_name) if node else None

        # Analyze trend and predict next value
        prediction = self._prefetch_scheduler.predict_next_value(
            node_id, param_name, float(new_value), param_bounds
        )

        # Check if prediction confidence is high enough
        if prediction and self._prefetch_scheduler.should_prefetch(prediction):
            self._active_prefetch_prediction = prediction
            # Trigger prefetch (callback will be called)
            _pf_snap = self._node_graph.snapshot(
                color_texture_id=self._last_color_texture_id,
                color_width=self._last_color_width,
                color_height=self._last_color_height,
                color_revision=self._last_color_revision,
                frame_number=self._current_keyframe_frame(),
                fps=self.comp_clip_fps(),
            )
            self._inject_viewer3d_scene_snapshot(_pf_snap)
            # AUD-9513: 原 `_pf_vmo = self._apply_view_mode_overrides(_pf_snap)`
            # 计算结果从未被读取，属死代码，已移除。
            self._prefetch_scheduler.trigger_prefetch(prediction, _pf_snap)

    def _trigger_prefetch_request(self, node_id: str, param_name: str,
                                   predicted_value: float,
                                   snapshot=None) -> None:
        """Callback from PrefetchScheduler to submit a prefetch request.

        Creates a PrefetchRequest with a modified snapshot (predicted params)
        and submits it to the renderer for background evaluation.

        Args:
            node_id: Node ID whose parameter is being prefetched.
            param_name: Parameter name to prefetch.
            predicted_value: Predicted value to use for prefetch.
            snapshot: Base graph snapshot (current state).
        """
        if not self._renderer or snapshot is None:
            return

        import time as _time
        rid = self._request_id_gen.next_id()

        prefetch_request = PrefetchRequest(
            snapshot=snapshot,
            request_id=rid,
            node_id=node_id,
            param_name=param_name,
            predicted_value=predicted_value,
            confidence=self._active_prefetch_prediction.confidence if self._active_prefetch_prediction else 0.5,
            target_node_ids=self._get_downstream_nodes(node_id),
            timestamp=_time.monotonic(),
        )

        # Submit prefetch request to renderer
        self._renderer.submit(CompositeCommand.PREFETCH_EVALUATE, {
            'prefetch_request': prefetch_request,
        })

    def _get_downstream_nodes(self, node_id: str) -> List[str]:
        """Get all downstream node IDs that depend on the given node.

        Args:
            node_id: Node ID to find downstream nodes for.

        Returns:
            List of downstream node IDs in topological order.
        """
        downstream = []
        visited = set()

        # BFS from the given node following connections
        connections = self._node_graph.connections
        downstream_map = {}  # src_node_id -> list of dst_node_ids

        for conn in connections:
            if conn.src_node_id not in downstream_map:
                downstream_map[conn.src_node_id] = []
            downstream_map[conn.src_node_id].append(conn.dst_node_id)

        # BFS traversal
        queue = [node_id]
        while queue:
            current = queue.pop(0)
            for dst_id in downstream_map.get(current, []):
                if dst_id not in visited:
                    visited.add(dst_id)
                    downstream.append(dst_id)
                    queue.append(dst_id)

        return downstream

    # ── Undo helpers ──

    def _push_add_undo(self, node) -> None:
        """Push undo for node addition.

        Uses factory function to ensure this action is kept during compression.
        """
        node_id = node.id
        node_data = node.to_dict()

        action = create_add_node_action(
            node_data={"node_id": node_id, "node_type": node.node_type, "node_title": node.node_title},
            undo_fn=lambda d: self._undo_add_node(d),
            redo_fn=lambda d: self._redo_add_node(d, node_data),
        )
        self._undo_stack.push(action)

    def _undo_add_node(self, data) -> None:
        node_id = data["node_id"]
        self._clear_deleted_node_viewer_state(node_id)
        # AUD-9503：与直接删除路径（_on_node_removed）对齐——undo 撤销加节点
        # 也要提交 CLEANUP_NODE，否则渲染线程按 node_id 持有的 GPU/实例资源
        # 不会收到清理指令，反复加节点→Ctrl+Z 在渲染线程累积泄漏。
        if self._renderer:
            self._renderer.submit(CompositeCommand.CLEANUP_NODE, {'node_id': node_id})
        self._node_graph.remove_node(node_id)
        self._node_editor.remove_node_visual(node_id)
        self._evaluate_graph()

    def _redo_add_node(self, data, node_data) -> None:
        node_cls = NODE_REGISTRY.get(node_data["type"])
        if node_cls:
            node = node_cls.from_dict(node_data)
            self._node_graph.add_node(node)
            self._node_editor.add_node_visual(node)
            self._evaluate_graph()

    def _push_remove_undo(self, node) -> None:
        """Push undo for node removal.

        Uses factory function to ensure this action is kept during compression.
        Saves all connections for restoration on undo.
        """
        node_id = node.id
        node_data = node.to_dict()

        # Save connections
        conns = []
        for c in self._node_graph.get_connections_for_node(node_id):
            conns.append({
                "src_node_id": c.src_node_id,
                "src_port_name": c.src_port_name,
                "dst_node_id": c.dst_node_id,
                "dst_port_name": c.dst_port_name,
            })

        action = create_remove_node_action(
            node_data={"node_id": node_id, "node_type": node.node_type, "node_title": node.node_title},
            undo_fn=lambda d: self._undo_remove_node(d, node_data, conns),
            redo_fn=lambda d: self._redo_remove_node(d),
        )
        self._undo_stack.push(action)

    def _undo_remove_node(self, data, node_data, conns) -> None:
        node_cls = NODE_REGISTRY.get(node_data["type"])
        if node_cls:
            node = node_cls.from_dict(node_data)
            self._node_graph.add_node(node)
            self._node_editor.add_node_visual(node)
            # Restore connections
            for c in conns:
                self._node_graph.connect(c["src_node_id"], c["src_port_name"],
                                         c["dst_node_id"], c["dst_port_name"])
                self._node_editor.add_connection_visual(
                    c["src_node_id"], c["src_port_name"],
                    c["dst_node_id"], c["dst_port_name"],
                )
            self._evaluate_graph()

    def _redo_remove_node(self, data) -> None:
        node_id = data["node_id"]
        self._clear_deleted_node_viewer_state(node_id)
        # AUD-9503：重做删除与直接删除同样需要渲染线程侧清理（对齐
        # _on_node_removed / _on_nodes_batch_removed 的提交约定）。
        if self._renderer:
            self._renderer.submit(CompositeCommand.CLEANUP_NODE, {'node_id': node_id})
        self._node_graph.remove_node(node_id)
        self._node_editor.remove_node_visual(node_id)
        self._evaluate_graph()

    def _push_connect_undo(self, src_node, src_port, dst_node, dst_port) -> None:
        """Push undo for connection.

        Uses factory function to ensure this action is kept during compression.
        """
        action = create_connect_action(
            src_node=src_node,
            src_port=src_port,
            dst_node=dst_node,
            dst_port=dst_port,
            undo_fn=lambda d: self._undo_connect(d),
            redo_fn=lambda d: self._redo_connect(d),
        )
        self._undo_stack.push(action)

    def _port_data_type(self, node_id: str, port_name: str) -> str:
        """Resolve a source port's data_type for visual creation."""
        node = self._node_graph.get_node(node_id)
        if node:
            port = node.get_output_port(port_name)
            if port:
                return port.data_type
        return "image"

    def _restore_connection_edge(self, edge: dict) -> None:
        """Reconnect one edge in model + visual (undo/redo helper, U-14)."""
        quad = (edge["src_node"], edge["src_port"],
                edge["dst_node"], edge["dst_port"])
        if self._node_graph.connect(*quad):
            self._node_editor.remove_connection_visual_exact(*quad)
            self._node_editor.add_connection_visual(
                *quad, self._port_data_type(quad[0], quad[1]))

    def _remove_connection_edge(self, edge: dict) -> None:
        """Disconnect one edge in model + visual (undo/redo helper, U-14)."""
        quad = (edge["src_node"], edge["src_port"],
                edge["dst_node"], edge["dst_port"])
        self._node_graph.disconnect(*quad)
        self._node_editor.remove_connection_visual_exact(*quad)

    def _push_replace_connection_undo(self, removed_edges, added_edge) -> None:
        """单输入连线替换 = 一个原子撤销命令（U-14）。

        ``removed_edges`` 是被替换掉的旧输入 [(src, src_port), ...]，
        ``added_edge`` 是新边 (src, src_port, dst, dst_port)。undo 恢复
        全部旧线，redo 重新替换；重复替换三次也能逐步还原。
        """
        dst_node, dst_port = added_edge[2], added_edge[3]
        removed = [
            {"src_node": s, "src_port": sp,
             "dst_node": dst_node, "dst_port": dst_port}
            for (s, sp) in removed_edges
        ]
        added = {"src_node": added_edge[0], "src_port": added_edge[1],
                 "dst_node": dst_node, "dst_port": dst_port}

        def restore(present: list, absent: list) -> None:
            for edge in absent:
                self._remove_connection_edge(edge)
            for edge in present:
                self._restore_connection_edge(edge)
            self._evaluate_graph()

        self._undo_stack.push(UndoAction(
            "connect_replace",
            {"removed": removed, "added": added},
            undo_fn=lambda d: restore(d["removed"], [d["added"]]),
            redo_fn=lambda d: restore([d["added"]], d["removed"]),
            description=(f"Connect {added_edge[1]} → {dst_port} "
                         f"(replace)"),
        ))

    def _undo_connect(self, data) -> None:
        self._node_graph.disconnect(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        # U-15：按本命令自己的 quad 精确删除视觉线（by-dst 会误删
        # 多输入端口上其他来源的线）。
        self._node_editor.remove_connection_visual_exact(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        self._evaluate_graph()

    def _redo_connect(self, data) -> None:
        success = self._node_graph.connect(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        if success:
            self._node_editor.remove_connection_visual_exact(
                data["src_node"], data["src_port"],
                data["dst_node"], data["dst_port"]
            )
            self._node_editor.add_connection_visual(
                data["src_node"], data["src_port"],
                data["dst_node"], data["dst_port"],
                self._port_data_type(data["src_node"], data["src_port"]),
            )
        self._evaluate_graph()

    def _push_disconnect_undo(self, src_node, src_port, dst_node, dst_port) -> None:
        """Push undo for disconnection.

        Uses factory function to ensure this action is kept during compression.
        """
        action = create_disconnect_action(
            src_node=src_node,
            src_port=src_port,
            dst_node=dst_node,
            dst_port=dst_port,
            undo_fn=lambda d: self._undo_disconnect(d),
            redo_fn=lambda d: self._redo_disconnect(d),
        )
        self._undo_stack.push(action)

    def _undo_disconnect(self, data) -> None:
        success = self._node_graph.connect(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        if success:
            # U-15：只清本 quad 的陈旧视觉（by-dst 会误删多输入端口上
            # 其他来源的线），再重建本线。
            self._node_editor.remove_connection_visual_exact(
                data["src_node"], data["src_port"],
                data["dst_node"], data["dst_port"]
            )
            self._node_editor.add_connection_visual(
                data["src_node"], data["src_port"],
                data["dst_node"], data["dst_port"],
                self._port_data_type(data["src_node"], data["src_port"]),
            )
        self._evaluate_graph()

    def _redo_disconnect(self, data) -> None:
        self._node_graph.disconnect(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        self._node_editor.remove_connection_visual_exact(
            data["src_node"], data["src_port"],
            data["dst_node"], data["dst_port"]
        )
        self._evaluate_graph()

    def set_parameter(self, node_id: str, param_name: str,
                      value: Any) -> None:
        """Set a parameter on a node and re-evaluate."""
        if _page_node_is_locked(self, node_id):
            return
        self._node_graph.set_node_parameter(node_id, param_name, value)
        self._evaluate_graph()

    def _refresh_properties_panel(self, node_id: str) -> None:
        """Refresh the properties panel for a given node."""
        node = self._node_graph.get_node(node_id)
        props = getattr(self, '_properties_panel', None)
        if node and props is not None:
            param_options = node.get_param_options() if hasattr(node, 'get_param_options') else {}
            param_defs = node.get_param_defs() if hasattr(node, 'get_param_defs') else {}
            # The node store keeps the static/base value. Properties should
            # display the value evaluated at the shared composition playhead,
            # just like the Keyframes Value column does.
            display_parameters = self._evaluated_node_params(
                node, self._current_keyframe_frame())
            props.set_node(
                node_id, node.node_type, node.display_title,
                display_parameters, param_options, param_defs,
                enabled=not node.disabled,
                node_color=getattr(node, 'header_color', '#666666'),
                read_only=_page_node_is_locked(self, node_id),
            )

    def _clear_stale_properties(self) -> None:
        """Clear properties panel if it shows a node that no longer exists."""
        if not hasattr(self, '_properties_panel') or not self._properties_panel:
            return
        current_id = getattr(self._properties_panel, '_current_node_id', None)
        if current_id and self._node_graph.get_node(current_id) is None:
            self._properties_panel.clear()

    def _commit_pending_edit_transactions(self) -> None:
        """Flush pending debounce transactions in operation order (U-04).

        所有撤销/重做入口（快捷键、toolbar、历史树）都必须先经过这里：
        未提交的参数修改若不先入历史，Ctrl+Z 会撤掉上一步而已防抖的
        修改悬空，之后 timer flush 再压栈形成错误的历史分支。
        """
        self._param_debounce_timer.stop()
        self._batch_param_debounce_timer.stop()
        if self._pending_batch_param_change is not None:
            self._flush_batch_param_changes()
        if self._pending_param_change is not None:
            self._flush_param_changes()

    def undo(self) -> None:
        """Undo the last action."""
        self._commit_pending_edit_transactions()
        self._undo_stack.undo()
        self._evaluate_graph()
        self._clear_stale_properties()
        self._refresh_asset_dependency_panel()

    def redo(self) -> None:
        """Redo the last undone action."""
        self._commit_pending_edit_transactions()
        self._undo_stack.redo()
        self._evaluate_graph()
        self._clear_stale_properties()
        self._refresh_asset_dependency_panel()

    def record_external_composite_edit(self, before: dict, after: dict | None) -> None:
        """Record a script/AI Composite edit as one native undo action.

        外部调用方不能直接操作 ``UndoStack`` 或 UI 对象；它只把编辑前后的
        graph payload 交给这里。这样脚本、AI 和鼠标编辑共享同一个撤销入口，
        并且 graph_changed 信号会继续负责工程 dirty、保存和预览刷新。
        """
        if not after:
            return
        self._emit_user_edit_once()

        def restore(data: dict) -> None:
            from src.features.composite.edit_service import _restore_graph_in_place

            _restore_graph_in_place(self._node_graph, data)
            self._node_editor.clear_all()
            self._node_editor.build_from_graph(self._node_graph)
            self._update_minimap()
            self._evaluate_graph_now()
            self._clear_stale_properties()

        payload = {"before": copy.deepcopy(before), "after": copy.deepcopy(after)}
        self._undo_stack.push(UndoAction(
            "external_composite_edit", payload,
            undo_fn=lambda data: restore(data["before"]),
            redo_fn=lambda data: restore(data["after"]),
            description="Composite edit",
        ))
        self._evaluate_graph_now()

    # ── Minimap & Zoom ──

    def _update_minimap(self) -> None:
        """Update the minimap to reflect current graph state (debounced)."""
        if hasattr(self, '_minimap') and hasattr(self, '_minimap_timer'):
            if not self._minimap_timer.isActive():
                self._minimap_timer.start()
        elif hasattr(self, '_minimap'):
            self._minimap.update_from_editor()

    def _update_minimap_deferred(self) -> None:
        """Minimap 防抖 timer 回调（T-P1-71：绑定方法形态，见 __init__ 注释）。"""
        minimap = getattr(self, "_minimap", None)
        if minimap is not None:
            minimap.update_from_editor()

    def _update_minimap_viewport(self) -> None:
        """Lightweight viewport-only update (for scroll/pan)."""
        if hasattr(self, '_minimap'):
            self._minimap.refresh_viewport()

    def _on_zoom_changed(self, zoom_level: float) -> None:
        """Handle zoom level change from editor."""
        self._update_minimap()
        if hasattr(self, '_zoom_label'):
            self._zoom_label.setText(f"{int(zoom_level * 100)}%")

    def _reposition_minimap(self) -> None:
        """Reposition minimap to bottom-right corner of the editor."""
        if hasattr(self, '_minimap') and hasattr(self, '_node_editor'):
            editor_rect = self._node_editor.rect()
            self._minimap.move(
                editor_rect.width() - self._minimap.width() - 8,
                editor_rect.height() - self._minimap.height() - 8,
            )

    def resizeEvent(self, event) -> None:
        """Reposition minimap on resize."""
        super().resizeEvent(event)
        self._reposition_minimap()
        if self._empty_state_overlay is not None:
            self._empty_state_overlay.setGeometry(self.rect())
            if not self._composition_active:
                self._empty_state_overlay.raise_()

    def showEvent(self, event) -> None:
        """Ensure minimap is positioned correctly when first shown."""
        super().showEvent(event)
        self._reposition_minimap()

    # ── Thumbnail Preview ──

    def set_properties_panel(self, panel) -> None:
        """Set reference to properties panel for thumbnail updates."""
        self._properties_panel = panel

    def set_keyframes_panel(self, panel) -> None:
        """Bind the Fusion-style Keyframes panel (P3/P5).

        Wires the panel's edit signals back into the NodeGraph + undo stack,
        and seeds it with the current graph + timeline state.
        """
        self._keyframes_panel = panel
        if panel is None:
            return
        if hasattr(panel, "set_view_mode"):
            try:
                panel.set_view_mode("timeline")
            except (AttributeError, RuntimeError):
                pass
        if hasattr(panel, "set_expression_editor_expanded"):
            try:
                panel.set_expression_editor_expanded(False)
            except (AttributeError, RuntimeError):
                pass
        if hasattr(panel, "set_composition_active"):
            try:
                panel.set_composition_active(self._composition_active)
            except (AttributeError, RuntimeError):
                pass
        # Keep the Keyframes empty-state CTA on the same creation path as the
        # Composite node workspace.  The panel owns its presentation, while
        # CompositePage remains the single signal boundary for MainWindow.
        if hasattr(panel, "create_empty_comp_clip_requested"):
            panel.create_empty_comp_clip_requested.connect(
                self.create_empty_comp_clip_requested.emit
            )
        # Timeline is the stable entry surface for the Keyframes dock.  The
        # Graph Editor is an opt-in inspection/editing view (or an explicit
        # expression/spline redirect); do not let a reused panel instance or
        # dock construction order make it the default surface.
        if hasattr(panel, "set_layer_stack_controller"):
            panel.set_layer_stack_controller(self._layer_stack_controller)
            # Composite's public timeline is the AE-style layer view. The
            # panel can still switch back to Nodes mode for graph inspection,
            # but a fresh Composite page starts with one row per layer.
            if hasattr(panel, "set_layer_view_mode"):
                try:
                    panel.set_layer_view_mode("layers")
                except (AttributeError, RuntimeError, ValueError):
                    pass
        if hasattr(panel, "set_media_drop_handler"):
            panel.set_media_drop_handler(self._handle_keyframes_media_drop)
        # P5: forward edits into the graph + re-render + undo.
        if hasattr(panel, 'current_frame_changed'):
            panel.current_frame_changed.connect(self.set_composite_current_frame)
        if hasattr(panel, 'track_changed'):
            panel.track_changed.connect(self._on_keyframes_panel_track_changed)
        if hasattr(panel, 'undo_requested'):
            panel.undo_requested.connect(self.undo)
        if hasattr(panel, 'redo_requested'):
            panel.redo_requested.connect(self.redo)
        # AE-style Value-column edits use the same debounced parameter path as
        # the Properties panel, so sliders, inline values and scripts share
        # one undo/render implementation.
        if hasattr(panel, 'parameter_changed'):
            panel.parameter_changed.connect(self._on_parameter_changed)
        if hasattr(panel, 'property_value_drag_finished'):
            panel.property_value_drag_finished.connect(
                self._on_keyframes_property_value_drag_finished)
        if hasattr(panel, 'parameter_binding_changed'):
            panel.parameter_binding_changed.connect(
                self._on_keyframes_panel_parameter_binding_changed)
        if hasattr(panel, 'parent_binding_changed'):
            panel.parent_binding_changed.connect(
                self._on_keyframes_panel_parent_binding_changed)
        # T123: wrap a Graph-mode drag's (possibly multi-series) commit burst
        # in ONE undo group so a single Ctrl+Z reverts the whole drag.
        if hasattr(panel, 'graph_drag_about_to_commit'):
            panel.graph_drag_about_to_commit.connect(
                self._on_graph_drag_about_to_commit)
        if hasattr(panel, 'graph_drag_committed'):
            panel.graph_drag_committed.connect(
                self._on_graph_drag_committed)
        if hasattr(panel, 'track_batch_about_to_commit'):
            panel.track_batch_about_to_commit.connect(
                self._on_keyframes_batch_about_to_commit)
        if hasattr(panel, 'track_batch_committed'):
            panel.track_batch_committed.connect(
                self._on_keyframes_batch_committed)
        # T120/Bug-1: Graph Expression Editor commits share the properties
        # panel's authoritative expression/undo path so they're reversible.
        if hasattr(panel, 'graph_expression_changed'):
            panel.graph_expression_changed.connect(
                self._on_graph_expression_changed)
        if hasattr(panel, 'graph_expression_enabled_changed'):
            panel.graph_expression_enabled_changed.connect(
                self._on_graph_expression_enabled_changed)
        # Prefer the host-facing range signal when available: it preserves
        # the transient trim_in/trim_out/slide gesture kind needed to link a
        # layer-bar edit back to a Video Frame source. Older panels expose
        # only range_changed, so keep that as the compatibility fallback.
        if hasattr(panel, 'range_edit_changed'):
            panel.range_edit_changed.connect(
                self._on_keyframes_panel_time_range_changed)
        elif hasattr(panel, 'range_changed'):
            panel.range_changed.connect(
                self._on_keyframes_panel_time_range_changed)
        # AE-style work area (I/O play range) → transport / comp-clip timer.
        if hasattr(panel, 'play_range_changed'):
            panel.play_range_changed.connect(
                self._on_keyframes_panel_play_range_changed)
        # P9: selecting a node row in the Keyframes tree selects it everywhere.
        if hasattr(panel, 'node_selected'):
            panel.node_selected.connect(self._on_keyframes_panel_node_selected)
        if hasattr(panel, 'layer_selected'):
            panel.layer_selected.connect(
                self._on_keyframes_panel_layer_selected)
        if hasattr(panel, 'layer_delete_requested'):
            panel.layer_delete_requested.connect(
                self._on_keyframes_panel_layer_delete)
        if hasattr(panel, 'promote_node_requested'):
            panel.promote_node_requested.connect(
                self._on_keyframes_promote_node)
        if hasattr(panel, 'create_matte_layer_requested'):
            panel.create_matte_layer_requested.connect(
                self._on_keyframes_create_matte_layer)
        if hasattr(panel, 'attach_node_requested'):
            panel.attach_node_requested.connect(
                self._on_keyframes_attach_node)
        # Playback: the panel's play button drives the comp-clip engine.
        if hasattr(panel, 'set_playback_controller'):
            panel.set_playback_controller(self)
        # New keyframes should capture the node's live parameter value.
        if hasattr(panel, 'set_value_provider'):
            panel.set_value_provider(self._kf_panel_param_value)
        # Align the panel's timeline range with the comp clip's frame range.
        if hasattr(self, "_sync_keyframes_panel_frame_range"):
            self._sync_keyframes_panel_frame_range()
        # Seed the panel with the current graph + selected node.
        self._refresh_keyframes_panel()
        # T56/T61: if the Spline panel was bound before the Keyframes panel,
        # re-attach it now so the shared-selection sync is live in both
        # directions regardless of bind order.
        sp = getattr(self, '_spline_panel', None)
        if (sp is not None and hasattr(sp, 'attach_to_shared_selection')
                and hasattr(panel, 'selection_model')):
            try:
                sp.attach_to_shared_selection(panel.selection_model)
            except Exception:
                pass

    def _max_video_media_frames(self) -> int:
        """Return the longest known video source length in frames.

        This is the timeline view extent, not the composition playback
        extent. A source may be much longer than the current comp, and the
        Keyframes timeline must expose that tail for trimming or sliding.
        """
        graph = getattr(self, '_node_graph', None)
        if graph is None:
            return 0
        try:
            nodes = graph.get_nodes_by_type("video_frame")
        except Exception:
            return 0
        longest = 0
        for node in nodes:
            try:
                longest = max(
                    longest, int(node.parameters.get("total_frames", 0) or 0))
            except (AttributeError, TypeError, ValueError):
                continue
        return max(0, longest)

    def _sync_keyframes_panel_frame_range(self) -> None:
        """Push the comp clip's frame range + fps into the Keyframes panel.

        The panel re-fits its ruler only when the range actually changes, so
        this is safe to call on every frame sync. The ruler exposes the
        longest known source when it exceeds the comp, while the composition
        extent is declared separately so the default work area remains the
        actual comp duration.
        """
        panel = getattr(self, '_keyframes_panel', None)
        spline = getattr(self, '_spline_panel', None)
        if ((panel is None or not hasattr(panel, 'set_frame_range'))
                and (spline is None or not hasattr(spline, 'set_frame_range'))):
            return
        comp_total = max(1, self._total_frames_for_node())
        if panel is not None and hasattr(panel, 'set_frame_range'):
            try:
                ruler_total = max(comp_total, self._max_video_media_frames())
                panel.set_frame_range(
                    0, ruler_total - 1, fps=self.comp_clip_fps())
                if hasattr(panel, 'set_comp_extent'):
                    panel.set_comp_extent(0, comp_total - 1)
            except Exception:
                pass
        if spline is not None and hasattr(spline, 'set_frame_range'):
            try:
                spline.set_frame_range(0, comp_total - 1, fps=self.comp_clip_fps())
            except Exception:
                pass

    def _kf_panel_param_value(self, node_id: str,
                              param_name: str) -> Any:
        """Return the value visible at the panel's current frame.

        The value editor and horizontal-drag gesture both use this provider
        as their starting point. Sampling the active track here keeps a drag
        on an animated property anchored to what the user actually sees,
        instead of jumping back to the node's static/base parameter value.
        """
        if self._node_graph is None:
            return None
        try:
            node = self._node_graph.get_node(node_id)
        except Exception:
            return None
        if node is None:
            return None
        try:
            track_data = getattr(node, "keyframe_tracks", {}).get(param_name)
            if isinstance(track_data, dict) and track_data.get("keyframes"):
                from src.shared.keyframe import keyframe_track_from_dict
                pdef = node.get_param_defs().get(param_name)
                value_type = getattr(pdef, "ptype", "") or track_data.get(
                    "value_type", "scalar")
                track = keyframe_track_from_dict({
                    "param_name": param_name,
                    "value_type": value_type,
                    "keyframes": track_data.get("keyframes", []),
                    "min_val": track_data.get("min_val", 0.0),
                    "max_val": track_data.get("max_val", 1.0),
                    "pre_extrap": track_data.get("pre_extrap", "constant"),
                    "post_extrap": track_data.get("post_extrap", "constant"),
                })
                base = getattr(node, "parameters", {}).get(param_name, 0.0)
                value = track.get_value(
                    self.get_composite_current_frame(), default=base)
                return value
        except (TypeError, ValueError, AttributeError, KeyError):
            pass
        try:
            raw = node.parameters.get(param_name)
            return raw
        except (TypeError, ValueError, AttributeError):
            pass
        try:
            pdef = node.get_param_defs().get(param_name)
            if pdef is not None:
                return pdef.default
        except (TypeError, ValueError, AttributeError):
            pass
        return None

    def set_keyframes_transport(self, transport: object | None) -> None:
        """Install the transport delegate that drives the viewport bar.

        The Keyframes panel's play button must animate the *same* playhead the
        viewport transport bar shows. Which engine that is depends on how the
        composition is hosted:

        - timeline-backed comp (runtime context) → the app layer injects an
          adapter around EditPlaybackController here;
        - independent comp clip / standalone → ``None`` and the panel falls
          back to the self-contained comp-clip timer below.

        Delegate protocol: ``play()``, ``pause()``, ``is_playing() -> bool``,
        optional ``seek(frame)`` (clip-local frame).
        """
        self._keyframes_transport = transport

    def notify_external_play_state(self, playing: bool) -> None:
        """Sync the Keyframes panel play button from an external engine
        (e.g. the user pressed play on the viewport transport bar)."""
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, 'set_playing_state'):
            try:
                panel.set_playing_state(bool(playing))
            except Exception:
                pass
        spline = getattr(self, '_spline_panel', None)
        if spline is not None and hasattr(spline, 'set_playing_state'):
            try:
                spline.set_playing_state(bool(playing))
            except Exception:
                pass
    def keyframes_panel_play_toggle(self, want_play: bool) -> None:
        """Play/pause requested from the Keyframes panel toolbar.

        Preference order: (1) the transport delegate injected by the app
        layer — the exact engine the viewport bar uses, so both surfaces
        stay in lockstep; (2) the comp-clip playback engine (it advances
        the composite playhead at the clip's fps). The resulting state is
        reported back so the panel button icon stays in sync.
        """
        transport = getattr(self, '_keyframes_transport', None)
        if transport is not None:
            try:
                if want_play:
                    if not transport.is_playing():
                        transport.play()
                else:
                    if transport.is_playing():
                        transport.pause()
                self.notify_external_play_state(bool(transport.is_playing()))
                return
            except Exception:
                # Delegate broken — fall back to the comp-clip engine.
                self._keyframes_transport = None
        self._ensure_comp_playback_timer()
        if want_play:
            self.comp_play()
        else:
            self.comp_pause()
        self._notify_kf_panel_play_state()

    def _notify_kf_panel_play_state(self) -> None:
        panel = getattr(self, '_keyframes_panel', None)
        playing = bool(getattr(self, '_comp_playing', False))
        if panel is not None and hasattr(panel, 'set_playing_state'):
            try:
                panel.set_playing_state(playing)
            except Exception:
                pass
        spline = getattr(self, '_spline_panel', None)
        if spline is not None and hasattr(spline, 'set_playing_state'):
            try:
                spline.set_playing_state(playing)
            except Exception:
                pass
    def _refresh_keyframes_panel(self) -> None:
        """Rebuild the Keyframes panel model from the current graph."""
        panel = getattr(self, '_keyframes_panel', None)
        if panel is None or self._node_graph is None:
            return
        # Keep the ruler aligned with the comp clip's frame range.
        self._sync_keyframes_panel_frame_range()
        selected = getattr(self, '_last_selected_node_id', None)
        selected_layer = getattr(self, '_last_selected_layer_id', None)
        try:
            panel.set_node_graph(
                self._node_graph,
                selected_node_id=selected,
                selected_layer_id=selected_layer)
        except TypeError:
            # Keep compatibility with lightweight/legacy panels that expose
            # only the original two-argument binding API.
            try:
                panel.set_node_graph(self._node_graph,
                                     selected_node_id=selected)
            except Exception:
                pass
        except Exception:
            pass
        # Phase 3: keep the Spline panel in sync with the same graph.
        spline = getattr(self, '_spline_panel', None)
        if spline is not None:
            try:
                spline.set_node_graph(self._node_graph)
            except Exception:
                pass

    def _on_spline_graph_mode_redirect(self) -> None:
        """T129: the user opened the standalone Spline entry — switch the
        Keyframes panel to Graph mode (the shared Graph Editor) and raise
        the Keyframes dock so focus lands on the canonical surface. The
        Spline panel itself stays functional (old entry still works); this
        just avoids maintaining a second curve-editor state.

        BUG-6 fix: if the Keyframes panel is ALREADY in graph mode, the
        redirect has already happened — don't re-raise the dock on every
        Spline re-show (that would yank focus away from whatever the user is
        now doing). Only the first transition into graph mode raises."""
        kp = getattr(self, '_keyframes_panel', None)
        if kp is None:
            return
        already_graph = False
        try:
            already_graph = getattr(kp, '_view_mode', None) == "graph"
        except Exception:
            already_graph = False
        try:
            kp.set_view_mode("graph")
        except Exception:
            pass
        # Raise the Keyframes dock only when this is a real transition
        # (wasn't already in graph mode) so repeated Spline shows don't steal
        # focus from the user's current surface.
        if not already_graph:
            dock = getattr(self, '_keyframes_dock', None)
            if dock is not None:
                try:
                    dock.setVisible(True)
                    dock.raise_()
                except Exception:
                    pass

    def set_spline_panel(self, panel) -> None:
        """Bind the Spline curve editor panel (phase 3 / P5-2)."""
        self._spline_panel = panel
        if panel is None:
            return
        if hasattr(panel, 'keyframe_changed'):
            # Reuse the same slot as the Keyframes panel — the node write +
            # undo + re-render logic is identical.
            panel.keyframe_changed.connect(self._on_keyframes_panel_track_changed)
        if hasattr(panel, 'curve_settings_changed'):
            panel.curve_settings_changed.connect(
                self._on_spline_curve_settings_changed)
        if hasattr(panel, 'current_frame_changed'):
            panel.current_frame_changed.connect(self.set_composite_current_frame)
        if hasattr(panel, 'set_playback_controller'):
            panel.set_playback_controller(self)
        # T129: when the user opens the standalone Spline entry, redirect to
        # the shared Graph Editor (Keyframes panel's graph mode) instead of
        # maintaining a second curve-editor state. The old entry still
        # works — the panel shows — but focus switches to the canonical
        # Graph surface (plan: '旧入口仍可用，但只打开共享 Graph 模式').
        if hasattr(panel, 'graph_mode_redirect'):
            panel.graph_mode_redirect.connect(
                self._on_spline_graph_mode_redirect)
        # T56/T61: attach the Spline to the Keyframes panel's shared
        # selection model so selecting a curve in either surface mirrors to
        # the other (without resetting the Spline's viewport). The attach is
        # idempotent — re-attaching unsubscribes the prior callback first.
        kp = getattr(self, '_keyframes_panel', None)
        if (kp is not None and hasattr(kp, 'selection_model')
                and hasattr(panel, 'attach_to_shared_selection')):
            try:
                panel.attach_to_shared_selection(kp.selection_model)
            except Exception:
                pass
        self._sync_keyframes_panel_frame_range()
        # P5-2: seed the panel with the current graph + frame immediately, so a
        # late-bound Spline dock (or rebuild) shows curves right away instead
        # of waiting for the next graph edit.
        if self._node_graph is not None:
            try:
                panel.set_node_graph(self._node_graph)
            except Exception:
                pass
        try:
            panel.set_current_frame(self._current_keyframe_frame())
        except Exception:
            pass

    def _on_graph_drag_about_to_commit(self, count: int) -> None:
        """T123: begin an undo group for a Graph-mode drag's commit burst.
        A multi-series Transform Box drag emits N track_changed signals;
        this brackets them so a single undo reverts the whole drag
        (plan §22.1 '每次拖动/批量变换一次 undo')."""
        if count <= 1:
            return  # single-series drag = already one undo entry
        try:
            self._undo_stack.begin_group()
        except Exception as e:
            logger.warning("Undo group begin failed (graph drag): %s", e)

    def _on_graph_drag_committed(self, count: int) -> None:
        """T123: close the undo group opened by _on_graph_drag_about_to_commit."""
        if count <= 1:
            return
        try:
            from src.shared.i18n.i18n import I18N
            self._undo_stack.end_group(
                I18N.tr("undo.graph_drag", "Graph keyframe drag"))
        except Exception as e:
            logger.warning("Undo group end failed (graph drag): %s", e)

    def _on_keyframes_batch_about_to_commit(self, label: str,
                                             count: int) -> None:
        """Group a multi-track timeline edit into one undo transaction."""
        if int(count) <= 1:
            return
        try:
            self._undo_stack.begin_group()
        except Exception as e:
            logger.warning("Undo group begin failed (timeline batch): %s", e)

    def _on_keyframes_batch_committed(self, label: str, count: int) -> None:
        """Close the undo group opened for a timeline batch edit."""
        if int(count) <= 1:
            return
        try:
            from src.shared.i18n.i18n import I18N
            readable = str(label or "edit").replace("_", " ")
            self._undo_stack.end_group(
                I18N.tr("undo.keyframes_batch",
                        "Keyframes {label}").format(label=readable))
        except Exception as e:
            logger.warning("Undo group end failed (timeline batch): %s", e)

    def _on_keyframes_panel_track_changed(self, node_id: str, param_name: str,
                                          keyframes: list) -> None:
        """Write a Keyframes-panel keyframe edit into the node + re-render.

        P6/P5: the panel already updated its visual model; this slot persists
        the change on the live node, pushes undo (P2), marks dirty, and
        requests a re-evaluation.
        """
        import copy
        if _page_node_is_locked(self, node_id):
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return

        # P2: capture the BEFORE state (deep copy) before mutating the node.
        before = copy.deepcopy(node.keyframe_tracks.get(param_name))

        if not keyframes:
            node.remove_keyframe_track(param_name)
            after = None
        else:
            from src.shared.keyframe import keyframe_track_from_dict
            pdef = node.get_param_defs().get(param_name)
            track = keyframe_track_from_dict(
                {
                    "param_name": param_name,
                    "value_type": getattr(pdef, "ptype", "") or "scalar",
                    "keyframes": keyframes,
                    "min_val": pdef.min_val if pdef else 0.0,
                    "max_val": pdef.max_val if pdef else 1.0,
                    "pre_extrap": (before or {}).get("pre_extrap", "constant"),
                    "post_extrap": (before or {}).get("post_extrap", "constant"),
                },
                value_type=getattr(pdef, "ptype", None),
            )
            node.set_keyframe_track(param_name, track)
            after = copy.deepcopy(node.keyframe_tracks.get(param_name))
        # P2: push a real before/after undo record.
        self._push_keyframe_track_undo(node_id, param_name, before, after)
        self._emit_user_edit_once()
        self._evaluate_graph()
        kf_panel = getattr(self, '_keyframes_panel', None)
        dragging_value = bool(
            getattr(kf_panel, 'property_value_drag_active', False))
        props = getattr(self, '_properties_panel', None)
        if dragging_value and props is not None and hasattr(
                props, 'sync_animated_values'):
            # Track points and the Properties control update live, but avoid
            # rebuilding either panel while the mouse is still down.
            props.sync_animated_values(
                node_id, self._current_keyframe_frame(), [param_name])
        else:
            # Discrete edits use the complete refresh path so row state,
            # keyframe colors, values and the graph editor all agree.
            self._refresh_properties_panel(node_id)
            self._refresh_keyframes_and_spline_for_node(node_id)
        # P2: keep transport-bar markers in sync with the edited track.
        self.refresh_transport_keyframe_markers()

    def _on_keyframes_property_value_drag_finished(
            self, node_id: str, param_name: str) -> None:
        """Complete a Keyframes Value scrub after the mouse is released."""
        self._refresh_properties_panel(node_id)
        self._refresh_keyframes_and_spline_for_node(node_id)
        self.refresh_transport_keyframe_markers()

    def _on_keyframes_panel_parameter_binding_changed(
            self, node_id: str, param_name: str,
            source_node_id: str, source_param_name: str) -> None:
        """Persist one AE pick-whip edit as a single undoable graph edit."""
        if _page_node_is_locked(self, node_id):
            return
        import copy
        before = copy.deepcopy(self._node_graph.to_dict())
        if source_node_id and source_param_name:
            success, error = self._node_graph.set_parameter_binding(
                node_id, param_name, source_node_id, source_param_name)
        else:
            success = self._node_graph.remove_parameter_binding(
                node_id, param_name)
            error = None if success else "No parameter binding to remove"
        panel = getattr(self, "_keyframes_panel", None)
        if panel is not None and hasattr(panel, "show_binding_feedback"):
            panel.show_binding_feedback(
                node_id, param_name, bool(success),
                str(error or "Expression binding applied") if success
                else str(error or "Parameter binding rejected"))
        if not success:
            return
        after = copy.deepcopy(self._node_graph.to_dict())
        self.record_external_composite_edit(before, after)
        self._refresh_properties_panel(node_id)
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _on_keyframes_panel_parent_binding_changed(
            self, node_id: str, parent_node_id) -> None:
        """Persist a parent pick-whip edit and reject hierarchy cycles."""
        if _page_node_is_locked(self, node_id):
            return
        import copy
        before = copy.deepcopy(self._node_graph.to_dict())
        success, error = self._node_graph.set_node_parent(
            node_id, parent_node_id)
        panel = getattr(self, "_keyframes_panel", None)
        if panel is not None and hasattr(panel, "show_binding_feedback"):
            panel.show_binding_feedback(
                node_id, "", bool(success),
                str(error or "Parent binding applied") if success
                else str(error or "Parent binding rejected"))
        if not success:
            return
        after = copy.deepcopy(self._node_graph.to_dict())
        self.record_external_composite_edit(before, after)
        self._refresh_keyframes_and_spline_for_node(node_id)

    def _on_spline_curve_settings_changed(self, node_id: str,
                                           param_name: str,
                                           pre_extrap: str,
                                           post_extrap: str) -> None:
        """Persist Spline curve-level settings onto the authoritative track."""
        if _page_node_is_locked(self, node_id):
            return
        import copy
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        track_data = node.keyframe_tracks.get(param_name)
        if not isinstance(track_data, dict):
            return
        before = copy.deepcopy(track_data)
        from src.shared.keyframe import keyframe_track_from_dict
        pdef = node.get_param_defs().get(param_name)
        track = keyframe_track_from_dict(
            track_data, value_type=getattr(pdef, "ptype", None))
        track.pre_extrap = pre_extrap
        track.post_extrap = post_extrap
        node.set_keyframe_track(param_name, track)
        after = copy.deepcopy(node.keyframe_tracks.get(param_name))
        self._push_keyframe_track_undo(node_id, param_name, before, after)
        self._emit_user_edit_once()
        self._evaluate_graph()
        self._refresh_properties_panel(node_id)
        self._refresh_keyframes_and_spline_for_node(node_id)
        self.refresh_transport_keyframe_markers()

    def _refresh_keyframes_and_spline_for_node(self, node_id: str) -> None:
        """Refresh both timeline surfaces after a node edit (P5-1)."""
        if self._node_graph is None:
            return
        selected = getattr(self, '_last_selected_node_id', None)
        kf = getattr(self, '_keyframes_panel', None)
        if kf is not None:
            try:
                kf.set_node_graph(self._node_graph, selected_node_id=selected)
            except Exception:
                pass
        spline = getattr(self, '_spline_panel', None)
        if spline is not None:
            try:
                spline.set_node_graph(self._node_graph)
            except Exception:
                pass

    def refresh_transport_keyframe_markers(self) -> None:
        """P2: push the selected node's keyframe frames to the transport bar.

        Collects every keyframe frame across all animated parameters of the
        currently selected node and hands the sorted set to VideoControlBar so
        its Fusion ruler can draw diamond markers. Called after keyframe
        edits and whenever the node selection changes.
        """
        # AE 语义：◆ 按钮红色代表播放头正停在关键帧上——任何外部关键帧
        # 编辑（Keyframes 面板/手柄拖动）都要重新计算按钮状态。
        props = getattr(self, '_properties_panel', None)
        if props is not None and hasattr(props, 'refresh_keyframe_button_states'):
            try:
                props.refresh_keyframe_button_states()
            except Exception:
                pass
        if self._node_graph is None or self._viewer is None:
            return
        bar = getattr(self._viewer, 'video_control_bar', None)
        if bar is None or not hasattr(bar, 'set_keyframe_frames'):
            return
        node_id = getattr(self, '_last_selected_node_id', None)
        if node_id is None:
            node_id = self._node_editor.get_selected_node_id() if self._node_editor else None
        frames: set = set()
        if node_id:
            try:
                node = self._node_graph.get_node(node_id)
            except Exception:
                node = None
            if node is not None:
                for track_data in getattr(node, 'keyframe_tracks', {}).values():
                    if not isinstance(track_data, dict):
                        continue
                    for kf in track_data.get("keyframes", []):
                        try:
                            frames.add(int(kf.get("frame", 0)))
                        except (TypeError, ValueError):
                            continue
        try:
            bar.set_keyframe_frames(sorted(frames))
        except Exception:
            pass

    def _on_keyframes_panel_time_range_changed(self, node_id: str,
                                               time_range: dict) -> None:
        """Write a range-bar drag into node.time_range + re-render (P5/P2).

        Merge semantics: existing keys (source_in/source_out/offset/enabled)
        survive a trim; AE-style default layer bars (node.time_range is None)
        become explicit ranges on their first trim.

        Video media rows additionally get NLE trim semantics applied to the
        source node's playback range (source_in/source_out) so the material
        stays anchored while the clip is trimmed (see
        timeline_commands.apply_video_range_edit).
        """
        if _page_node_is_locked(self, node_id):
            return
        import copy
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        before = copy.deepcopy(node.time_range)
        # Link the gesture to the source while node.time_range still contains
        # the previous comp_start. The trim helper uses that old start to
        # calculate how many source frames the layer's head moved; assigning
        # the new range first would make a default bar look like a zero-frame
        # move on its first head trim.
        video_param_edit = None
        video_source = None
        try:
            from src.features.composite.ui.timeline_commands import \
                apply_video_range_edit, video_source_node_for
            layers_map = None
            controller = getattr(self, "_layer_stack_controller", None)
            if controller is not None:
                layers_map = controller.layers_map()
            video_source = video_source_node_for(
                node, self._node_graph, layers_map)
            video_param_edit = apply_video_range_edit(
                node, self._node_graph, layers_map, time_range)
        except Exception:
            video_param_edit = None
        # "edit" is a transient gesture kind (trim_in/trim_out/slide), not
        # part of the persisted time_range schema.
        range_payload = {k: v for k, v in time_range.items() if k != "edit"}
        merged = dict(node.time_range) if isinstance(node.time_range, dict) else {}
        merged.update(range_payload)
        node.time_range = merged
        # A layer bar can live on a Transform/Merge root while the actual
        # media is a Video Frame source upstream. Gate the source as well as
        # the root, otherwise an effect-node bypass before comp_start can
        # still pass the old video frame through to the output.
        source_time_range_before = None
        source_time_range_after = None
        if video_source is not None and video_source is not node:
            source_time_range_before = copy.deepcopy(
                getattr(video_source, "time_range", None))
            video_source.time_range = copy.deepcopy(merged)
            source_time_range_after = copy.deepcopy(video_source.time_range)
        after = copy.deepcopy(node.time_range)
        # P2: real before/after undo for the range bar edit. The video
        # param linkage folds into the same action so one undo reverts the
        # whole gesture.
        undo_data = {"node_id": node_id, "before": before, "after": after}
        if video_param_edit is not None:
            undo_data["video_node_id"] = video_param_edit["node_id"]
            undo_data["video_params_before"] = dict(video_param_edit["before"])
            undo_data["video_params_after"] = dict(video_param_edit["after"])
        if video_source is not None and video_source is not node:
            undo_data["video_node_id"] = video_source.id
            undo_data["video_time_range_before"] = source_time_range_before
            undo_data["video_time_range_after"] = source_time_range_after
        try:
            self._undo_stack.push(UndoAction(
                action_type="node_time_range_change",
                data=undo_data,
                undo_fn=lambda d: self._restore_node_time_range(d, "before"),
                redo_fn=lambda d: self._restore_node_time_range(d, "after"),
                description="time range",
            ))
        except Exception as e:
            logger.warning("Undo push failed for time range change: %s", e)
        self._emit_user_edit_once()
        self._evaluate_graph()
        # The selected Video Frame node exposes the same source trim through
        # its Properties panel; keep the In/Out handles and fields in sync
        # after a Keyframes layer-bar edit.
        self._refresh_properties_panel(node_id)

    def _restore_node_time_range(self, data: dict, which: str) -> None:
        """Apply the before/after time_range snapshot (P2)."""
        node = self._node_graph.get_node(data["node_id"])
        if node is not None:
            node.time_range = copy.deepcopy(data.get(which))
        # Restore the video source playback-range params linked to the edit.
        video_node_id = data.get("video_node_id")
        params_key = ("video_params_before" if which == "before"
                      else "video_params_after")
        params = data.get(params_key)
        if video_node_id and params:
            video_node = self._node_graph.get_node(video_node_id)
            if video_node is not None:
                for key, value in params.items():
                    video_node.set_parameter(key, value)
        video_time_range_key = ("video_time_range_before"
                                if which == "before"
                                else "video_time_range_after")
        if video_node_id and video_time_range_key in data:
            video_node = self._node_graph.get_node(video_node_id)
            if video_node is not None:
                video_node.time_range = copy.deepcopy(
                    data.get(video_time_range_key))
        self._evaluate_graph()
        self._refresh_keyframes_and_spline_for_node(data["node_id"])

    def _on_keyframes_panel_play_range_changed(self, in_frame: int,
                                               out_frame: int) -> None:
        """Keyframes 面板工作区（I/O）变化 → 播放引擎 + viewport 播放条。

        时间线联动合成：转发给 transport 适配器（clip 本地帧 → 全局帧 →
        EditPlaybackController.set_playback_range）。无委托的独立合成：
        写入 comp-clip 定时器的 I/O 范围。两条路径最后都把范围同步到
        viewport 底部播放条的 I/O 标记（两者必须保持一致）。
        """
        handled = False
        transport = getattr(self, '_keyframes_transport', None)
        if transport is not None and hasattr(transport, 'set_play_range'):
            try:
                transport.set_play_range(int(in_frame), int(out_frame))
                handled = True
            except Exception:
                self._keyframes_transport = None
        if not handled:
            self.set_comp_clip_in_out(int(in_frame), int(out_frame))
        # 双向同步：viewport 播放条的 I/O 标记跟随 Keyframes 面板工作区。
        self._sync_viewport_io_bar()

    # ── 播放范围（I/O 工作区）双向同步 ──────────────────────────────
    # Keyframes 面板的工作区是唯一事实来源（single source of truth）；
    # viewport 底部播放条的 I/O 标记与它互相联动：面板拖动/I/O 键 →
    # 播放条标记；播放条 I/O/清除按钮 → 面板工作区。

    def play_range_marks(self) -> tuple:
        """当前 I/O 播放范围标记 ``(in, out)``；``(None, None)`` = 未设置。

        优先取 Keyframes 面板（事实来源），无面板时退回 comp-clip
        定时器的 I/O。
        """
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, 'play_range_points'):
            try:
                return panel.play_range_points()
            except Exception:
                pass
        return (getattr(self, '_comp_in_frame', None),
                getattr(self, '_comp_out_frame', None))

    def _sync_viewport_io_bar(self) -> None:
        """把当前 I/O 范围推送到 viewport 播放条（含清除）。"""
        viewer = getattr(self, '_viewer', None)
        bar = getattr(viewer, 'video_control_bar', None) if viewer is not None else None
        if bar is None or not hasattr(bar, 'set_in_out_points'):
            return
        pin, pout = self.play_range_marks()
        try:
            bar.set_in_out_points(pin, pout)
        except Exception:
            pass

    def mark_play_in(self) -> None:
        """Viewport 播放条的 I 按钮：在当前播放头设置入点。

        委托给 Keyframes 面板（set_play_range 会发 play_range_changed，
        经 _on_keyframes_panel_play_range_changed 同步播放引擎 + 播放条）；
        无面板时直接写 comp-clip 定时器 I/O。
        """
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, 'set_play_in_at_playhead'):
            panel.set_play_in_at_playhead()
            return
        frame = self._current_keyframe_frame()
        total = self._total_frames_for_node()
        out = getattr(self, '_comp_out_frame', None)
        out = min(out, total - 1) if out is not None else total - 1
        self.set_comp_clip_in_out(min(frame, out), max(frame, out))
        self._sync_viewport_io_bar()

    def mark_play_out(self) -> None:
        """Viewport 播放条的 O 按钮：在当前播放头设置出点。"""
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, 'set_play_out_at_playhead'):
            panel.set_play_out_at_playhead()
            return
        frame = self._current_keyframe_frame()
        pin = getattr(self, '_comp_in_frame', None) or 0
        self.set_comp_clip_in_out(min(frame, pin), max(frame, pin))
        self._sync_viewport_io_bar()

    def clear_play_range_marks(self) -> None:
        """Viewport 播放条的清除按钮：取消 I/O 工作区。"""
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, 'clear_play_range'):
            panel.clear_play_range()
            return
        self.clear_comp_clip_in_out()
        self._sync_viewport_io_bar()

    def _on_keyframes_panel_node_selected(self, node_id: str) -> None:
        """A node row click in the Keyframes tree selects the node (P9)."""
        self._last_selected_node_id = node_id
        # Reuse the existing node selection handler so the canvas + properties
        # panel stay in sync.
        if hasattr(self, '_on_node_selected'):
            try:
                self._on_node_selected(node_id)
            except Exception as e:
                logger.warning("Keyframes node selection failed: %s", e)
        # Refresh the keyframes panel selection highlight.
        panel = getattr(self, '_keyframes_panel', None)
        if panel is not None and hasattr(panel, '_model'):
            panel._model.set_selected_node(node_id)
            panel._tree_view.refresh()

    def _on_keyframes_panel_layer_selected(self, layer_id: str) -> None:
        """Select a Layer row and its live root node as one shared target."""
        self._last_selected_layer_id = str(layer_id)
        controller = getattr(self, '_layer_stack_controller', None)
        if controller is None:
            return
        try:
            layer = controller.layers_map().get(str(layer_id))
            if layer is not None:
                self._on_keyframes_panel_node_selected(layer.root_node_id)
        except Exception:
            logger.exception("Unable to select Keyframes layer %s", layer_id)

    def _on_keyframes_panel_layer_delete(self, layer_id: str) -> None:
        """Delete one AE-style Layer and all controller-owned graph nodes."""
        controller = getattr(self, "_layer_stack_controller", None)
        if controller is None:
            return
        before_nodes = set(self._node_graph.nodes)
        if not controller.remove_layer(
                str(layer_id), delete_nodes=True,
                description="Delete media layer"):
            return
        removed_nodes = before_nodes - set(self._node_graph.nodes)
        for node_id in removed_nodes:
            if self._renderer:
                self._renderer.submit(
                    CompositeCommand.CLEANUP_NODE, {"node_id": node_id})
        if getattr(self, "_last_selected_layer_id", None) == str(layer_id):
            self._last_selected_layer_id = None
        self._last_selected_node_id = None
        self._node_editor.build_from_graph(self._node_graph)
        self._refresh_keyframes_panel()
        self._evaluate_graph()
        self._clear_stale_properties()

    def _on_keyframes_promote_node(self, node_id: str) -> None:
        """Promote a free-graph node only after an explicit user command."""
        controller = getattr(self, '_layer_stack_controller', None)
        if controller is None:
            return
        layer_id = controller.promote_node_to_layer(node_id)
        if layer_id:
            self._last_selected_layer_id = layer_id
            self._node_editor.build_from_graph(self._node_graph)
            self._refresh_keyframes_panel()
            self._evaluate_graph()

    def _on_keyframes_create_matte_layer(self, node_id: str) -> None:
        """Create a shape layer from a Mask/Roto node explicitly."""
        controller = getattr(self, '_layer_stack_controller', None)
        if controller is None:
            return
        layer_id = controller.create_matte_layer(node_id)
        if layer_id:
            self._last_selected_layer_id = layer_id
            self._node_editor.build_from_graph(self._node_graph)
            self._refresh_keyframes_panel()
            self._evaluate_graph()

    def _on_keyframes_attach_node(self, node_id: str) -> None:
        """Attach a free-graph node to the currently selected Layer.

        This is the reversible bridge between the two editing metaphors:
        Node mode can remain completely free-form, while a deliberate
        context-menu action promotes the selected node into the active AE
        layer chain.  The controller owns the snapshot/undo boundary.
        """
        controller = getattr(self, '_layer_stack_controller', None)
        layer_id = getattr(self, '_last_selected_layer_id', None)
        if controller is None or not layer_id:
            return
        if controller.add_node_to_layer(str(layer_id), str(node_id)):
            self._last_selected_node_id = str(node_id)
            self._node_editor.build_from_graph(self._node_graph)
            self._refresh_keyframes_panel()
            self._evaluate_graph()

    def update_output_preview(self, data: Optional[np.ndarray]) -> None:
        """Update the output preview thumbnail.

        Called externally (e.g., from MainWindow) after a render completes.

        Args:
            data: BGR float32 numpy array from renderer readback,
                  or None to clear the preview.
        """
        if hasattr(self, '_properties_panel') and self._properties_panel:
            self._properties_panel.update_thumbnail(data)

    def request_output_readback(self, timeout: float = 5.0) -> Optional[np.ndarray]:
        """Request synchronous readback of the current output texture.

        Returns BGR float32 numpy array, or None on failure.

        **IMPORTANT**: This is a blocking call that freezes the UI thread.
        Only use for:
        - Debug / diagnostic probes
        - Export / encoder fallback
        - Unit tests with explicit sync requirement

        NEVER call from page switch, playback, or real-time parameter drag.
        For UI hot paths, use READBACK_PRESENT_ASYNC instead.

        Args:
            timeout: Maximum seconds to wait for readback. Default 5.0s.
        """
        if self._renderer:
            return self._renderer.readback_output(timeout=timeout)
        return None

    @property
    def thumbnail_manager(self):
        """Access the thumbnail manager."""
        return self._thumbnail_manager

    # ── Thumbnail Playback Control ─────────────────────────────────────────

    def set_thumbnail_playback_suspended(self, suspended: bool) -> None:
        """Suspend or resume thumbnail updates during playback.

        Call from playback controllers to prevent GPU readback from
        competing with real-time frame rendering.

        Args:
            suspended: True to suspend thumbnail updates, False to resume.
        """
        self._thumbnail_state.playback_suspended = suspended

    @property
    def thumbnail_policy(self):
        """Access the thumbnail policy (mutable, change in-place)."""
        return self._thumbnail_policy

    # ── Viewer Interaction (S5-13: Manipulation Transactions and Undo) ────────

    def set_viewer(self, viewer) -> None:
        """Connect the CompositeViewer for interaction handling.

        This method is called by MainWindow after creating the viewer.
        It connects the viewer's interaction signals to this page's
        handlers for undo/redo support.

        Phase 2: the fused viewer also emits 3D navigation signals
        (scene3d_nav_started/preview/committed) when in 3D mode. We connect
        those here so the same viewer serves both 2D and 3D workflows
        (Nuke-style single viewport with Tab to toggle).

        Args:
            viewer: CompositeViewer instance.
        """
        self._viewer = viewer
        if viewer:
            # The fused 3D Viewer resolves virtual-scene navigation through its
            # owning page. Without this back-reference Alt navigation emits an
            # empty target id, so preview/commit never reach the render path.
            try:
                viewer._composite_page = self
            except Exception:
                pass
            viewer.interaction_started.connect(self._on_viewer_interaction_started)
            viewer.interaction_updated.connect(self._on_viewer_interaction_updated)
            viewer.interaction_ended.connect(self._on_viewer_interaction_ended)
            # C2-3: PAINT tool strokes → node strokes JSON, one undo each.
            if hasattr(viewer, 'paint_stroke_committed'):
                viewer.paint_stroke_committed.connect(
                    self._on_paint_stroke_committed)
            # CP-056: 吸管取色 → 属性面板最近编辑的颜色参数
            if hasattr(viewer, 'color_picked'):
                viewer.color_picked.connect(self._on_viewer_color_picked)
            if hasattr(viewer, 'paint_brush_size_changed'):
                viewer.paint_brush_size_changed.connect(
                    self._on_paint_brush_size_changed)
            # B2-1: View-menu resolution tier → proxy_scale; "auto" also
            # listens to the interaction hint to downgrade while navigating.
            if hasattr(viewer, 'resolution_mode_changed'):
                viewer.resolution_mode_changed.connect(
                    self._on_resolution_mode_changed)
            if hasattr(viewer, 'auto_proxy_interaction'):
                viewer.auto_proxy_interaction.connect(
                    self._on_auto_proxy_interaction)
            # B1: ROI committed/cleared → re-render preview with the new
            # viewer_roi (renderer skip-signature treats ROI change as dirty)
            if hasattr(viewer, 'user_roi_changed'):
                viewer.user_roi_changed.connect(
                    self._on_viewer_user_roi_changed)
            # Phase 2: 3D navigation (fused viewer, not a separate widget)
            if hasattr(viewer, 'scene3d_nav_started'):
                viewer.scene3d_nav_started.connect(self._on_viewer3d_nav_started)
            if hasattr(viewer, 'scene3d_nav_preview'):
                viewer.scene3d_nav_preview.connect(self._on_viewer3d_nav_preview)
            if hasattr(viewer, 'scene3d_nav_committed'):
                viewer.scene3d_nav_committed.connect(self._on_viewer3d_nav_committed)
            # Tab 切 2D/3D 模式：viewer 只切自身的 _view_mode，需由 page 同步
            # _viewer3d_node_id（3D 焦点）并触发重渲染。
            if hasattr(viewer, 'view_mode_changed'):
                viewer.view_mode_changed.connect(self._on_view_mode_changed)
            # M2: 四视图切换（MMB click）→ 带 quad_view 重新提交渲染。
            if hasattr(viewer, 'quad_view_changed'):
                viewer.quad_view_changed.connect(self._on_quad_view_changed)
            if hasattr(viewer, 'viewer3d_content_size_changed'):
                viewer.viewer3d_content_size_changed.connect(
                    self._on_viewer3d_content_size_changed)
            # 活动摄影机切换（双击 Camera3D 图标）→ 写 scene3d.active_camera_id。
            if hasattr(viewer, 'active_camera_requested'):
                viewer.active_camera_requested.connect(self._on_active_camera_requested)
            # 播放条 I/O 按钮 → 页面（再联动 Keyframes 面板工作区）。
            bar = getattr(viewer, 'video_control_bar', None)
            if bar is not None and not getattr(bar, '_io_wired_to_page', False):
                if hasattr(bar, 'mark_in_requested'):
                    bar.mark_in_requested.connect(self.mark_play_in)
                if hasattr(bar, 'mark_out_requested'):
                    bar.mark_out_requested.connect(self.mark_play_out)
                if hasattr(bar, 'clear_in_out_requested'):
                    bar.clear_in_out_requested.connect(self.clear_play_range_marks)
                bar._io_wired_to_page = True
                # 初始推送：已有工作区时播放条立即显示 I/O 标记。
                self._sync_viewport_io_bar()

    def _resolve_nav_target(self, node_id: str) -> str:
        """Resolve the actual node that navigation params should write to.

        The global 3D Viewer owns its default navigation camera. Camera3D nodes
        are scene objects until the user explicitly activates one from the
        properties header, so navigation must not redirect through Scene3D's
        connected cameras.
        """
        return node_id

    def _on_view_mode_toggle_requested(self, mode: str) -> None:
        """Scene3D 参数面板的 2D/3D 开关被切换 → 调 viewer.set_view_mode。

        与 Tab 键走完全相同的路径：viewer.set_view_mode 切 _view_mode →
        view_mode_changed → _on_view_mode_changed（同步 _viewer3d_node_id + 重渲染）。
        纯视图状态，不进节点图/undo。
        """
        if self._viewer is not None and hasattr(self._viewer, 'set_view_mode'):
            self._viewer.set_view_mode("3d" if mode == "3d" else "2d")

    def _on_look_through_camera_requested(self, camera_id: str) -> None:
        """Camera3D 参数面板按钮：激活该 Camera 作为 3D Viewer 观察相机。"""
        self._activate_viewer3d_camera(camera_id)

    def _on_camera_activation_toggled(self, camera_id: str, active: bool) -> None:
        """Camera3D header activation toggle, C4D-style."""
        if active:
            self._activate_viewer3d_camera(camera_id)
        elif getattr(self, "_viewer3d_active_camera_id", None) == camera_id:
            self._deactivate_viewer3d_camera()

    def _activate_viewer3d_camera(self, camera_id: str) -> None:
        """Use a Camera3D node as the current 3D Viewer camera."""
        if not self._node_graph or self._viewer is None:
            return
        node = self._node_graph.get_node(camera_id)
        if node is None or getattr(node, "node_type", None) != "camera3d":
            return
        connected_scene = self._scene3d_for_source_node(camera_id)
        if connected_scene is not None:
            self._viewer3d_node_id = connected_scene
        elif self._resolve_viewer3d_scene_context_id() is None:
            self._viewer3d_node_id = self._first_scene3d_node_id()
        if hasattr(self._viewer, "set_view_mode"):
            self._viewer.set_view_mode("3d")
        if hasattr(self._viewer, "look_through_camera_params"):
            self._viewer.look_through_camera_params(dict(node.parameters))
        self._viewer3d_active_camera_id = camera_id
        if hasattr(self._viewer, "_update_gizmo_visibility"):
            self._viewer._update_gizmo_visibility()
        if (self._properties_panel is not None
                and hasattr(self._properties_panel, 'set_view_mode')):
            self._properties_panel.set_view_mode("3d")
        if (self._properties_panel is not None
                and hasattr(self._properties_panel, 'set_active_camera_id')):
            self._properties_panel.set_active_camera_id(camera_id)
        self._force_next_view_refresh = True
        self._evaluate_graph_now()

    def _deactivate_viewer3d_camera(self) -> None:
        """Return the 3D Viewer to the global/default navigation camera."""
        self._viewer3d_active_camera_id = None
        if self._viewer is not None and hasattr(self._viewer, "sync_scene3d_params"):
            nav_params = self._viewer3d_default_nav_snapshot()
            self._viewer.sync_scene3d_params(nav_params)
        if self._viewer is not None and hasattr(self._viewer, "_update_gizmo_visibility"):
            self._viewer._update_gizmo_visibility()
        if (self._properties_panel is not None
                and hasattr(self._properties_panel, 'set_active_camera_id')):
            self._properties_panel.set_active_camera_id("")
        self._force_next_view_refresh = True
        self._evaluate_graph_now()

    def _clear_deleted_node_viewer_state(self, node_id: str) -> None:
        """Clear Viewer references before a node is removed from the graph."""
        changed = False
        if self.__dict__.get("_viewer3d_active_camera_id") == node_id:
            self._viewer3d_active_camera_id = None
            changed = True
            if self._viewer is not None and hasattr(self._viewer, "sync_scene3d_params"):
                nav_params = self._viewer3d_default_nav_snapshot()
                self._viewer.sync_scene3d_params(nav_params)
        if self.__dict__.get("_viewer3d_node_id") == node_id:
            self._viewer3d_node_id = None
            changed = True
        if self.__dict__.get("_preview_node_id") == node_id:
            self._preview_node_id = None
            changed = True
            self._update_preview_connection()
            self._update_preview_status_label()
        if self.__dict__.get("_last_selected_node_id") == node_id:
            self._last_selected_node_id = None
            changed = True
        if self._viewer is not None:
            try:
                if hasattr(self._viewer, "get_selected_node") and self._viewer.get_selected_node() == node_id:
                    self._viewer.set_selected_node(None)
                    if hasattr(self._viewer, "set_node_info"):
                        self._viewer.set_node_info(None, {}, None, node_id=None)
                    changed = True
                elif hasattr(self._viewer, "_cancel_3d_interaction_state"):
                    self._viewer._cancel_3d_interaction_state()
            except Exception:
                pass
            try:
                self._viewer.update()
            except Exception:
                pass
        if (changed and self._properties_panel is not None
                and hasattr(self._properties_panel, 'set_active_camera_id')):
            active_id = getattr(self, "_viewer3d_active_camera_id", None)
            self._properties_panel.set_active_camera_id(active_id or "")

    def _toggle_view_mode_from_shortcut(self) -> None:
        """Page-level Tab shortcut fallback for focus outside the viewport."""
        if not self._is_active or self._viewer is None:
            return
        if not (hasattr(self._viewer, 'get_view_mode') and hasattr(self._viewer, 'set_view_mode')):
            return
        current = self._viewer.get_view_mode() or "2d"
        self._viewer.set_view_mode("2d" if current == "3d" else "3d")

    def _on_view_mode_changed(self, mode: str) -> None:
        """Tab 切换 2D/3D 时同步 page 侧状态并触发重渲染。

        viewer 的 event() 仅切自身 _view_mode，page 的 _viewer3d_node_id（3D 焦点，
        Alt 导航/gizmo 用）和 Scene3D 参数面板的开关高亮需在这里同步。
        2D/3D 显示现在完全相同（都是项目比例 letterbox），切换模式只是改交互
        （Alt 导航/gizmo 在 3D 才生效），并触发重渲染确保最新画面。

        - 切到 3D：进入全局虚拟 3D Viewer scene，不要求 Scene3D 节点。
        - 切到 2D：不清空焦点（保留便于切回），只是交互退出 3D 模式。
        """
        if not self._is_active:
            return
        mode = "3d" if mode == "3d" else "2d"
        if self._viewer is not None:
            try:
                current_mode = (
                    self._viewer.get_view_mode()
                    if hasattr(self._viewer, "get_view_mode")
                    else getattr(self._viewer, "_view_mode", None)
                )
                if current_mode != mode:
                    # Some callers restore/sync page view-mode directly instead
                    # of going through viewer.set_view_mode(). Keep the viewer's
                    # own event routing in lockstep so 3D nav reaches the
                    # Alt/MMB/wheel branches.
                    self._viewer._view_mode = mode
                    if hasattr(self._viewer, "_update_gizmo_visibility"):
                        self._viewer._update_gizmo_visibility()
                    if hasattr(self._viewer, "update"):
                        self._viewer.update()
            except Exception:
                pass
        if mode == "3d":
            self._exit_node_preview(evaluate=False)
        else:
            # 切回 2D：退出节点预览，Viewer 回到 Output（合成干净，无参考网格）。
            self._exit_node_preview(evaluate=False)
        # 切到 2D 不清空 _viewer3d_node_id：override 已据 _view_mode 门控，
        # 保留焦点便于切回，且避免选中 scene3d 后按 Tab 来回切时丢失焦点。
        # 同步 Scene3D 参数面板的开关高亮（Tab 切换 → 开关跟上）。
        if (self._properties_panel is not None
                and hasattr(self._properties_panel, 'set_view_mode')):
            self._properties_panel.set_view_mode(mode)
        self._force_next_view_refresh = True
        # View-mode changes alter Scene3D's snapshot-only grid/background
        # overrides without mutating node parameters. Submit immediately after
        # all view state is coherent so the viewer cannot keep presenting the
        # previous 3D preview texture until the next user parameter edit.
        self._evaluate_graph_now()

    def _auto_preview_for_3d(self) -> None:
        """In 3D mode, auto-preview the focused scene3d node (if any).

        Lets the viewer show the scene3d node's raw output (with the reference
        grid + hardware depth occlusion) as soon as the user presses Tab, without
        requiring a manual "1". Only scene3d nodes carry an image output, so a
        camera3d focus node is skipped (nothing to preview). If a non-scene3d
        node is already being previewed, leave it untouched (user intent wins).
        """
        nid = self._viewer3d_node_id
        if not nid or not self._node_graph:
            return
        node = self._node_graph.get_node(nid)
        if node is None:
            return
        # Only auto-preview scene3d (has image output). camera3d has no texture.
        if getattr(node, "node_type", None) != "scene3d":
            return
        # Don't clobber an existing non-scene3d preview (user manually picked one).
        if self._preview_node_id and self._preview_node_id != nid:
            existing = self._node_graph.get_node(self._preview_node_id)
            if existing is not None and getattr(existing, "node_type", None) != "scene3d":
                return
        if self._preview_node_id == nid:
            return  # already previewing it
        self._preview_node_id = nid
        self._update_preview_connection()
        self._update_preview_status_label()

    def _on_viewer3d_nav_started(self, node_id: str) -> None:
        """Record original camera + object params before a 3D drag (undo before).

        Covers both camera navigation (cam_*) and the Phase 3 transform gizmo
        (object_pos_*), since both flow through the same nav signal path.

        图标拖动（Camera3D/Light3D 视口图标）也复用此信号：当 node_id 指向
        camera3d/light3d 时，额外记录 cam_eye_*/light_pos_*/icon_pos_* 参数，
        供 release 时 _on_viewer3d_nav_committed 匹配 undo。

        Milestone B / P1：进入 3D 导航态，标记 _nav3d_active=True，让 Scene3D
        进入 NAVIGATION profile（降 MSAA/IBL 保流畅）。
        P1-5：取消任何挂起的恢复 timer（连续导航不应被上一次的恢复打断）。
        """
        # getattr 兜底：测试/未完成 __init__ 的 page 可能没建 timer。
        _restore_timer = getattr(self, "_nav3d_restore_timer", None)
        if _restore_timer is not None:
            _restore_timer.stop()
        self._nav3d_active = True
        if node_id == self._VIEWER3D_VIRTUAL_NODE_ID:
            self._viewer3d_nav_before = self._viewer3d_default_nav_snapshot()
            return
        # 阶段七：选中 Scene3D 但连了 Camera3D → 导航重定向到 Camera3D
        node_id = self._resolve_nav_target(node_id)
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if not node:
            return
        if node.node_type == "scene3d":
            self._viewer3d_node_id = node_id
        else:
            connected_scene = self._scene3d_for_source_node(node_id)
            if connected_scene is not None:
                self._viewer3d_node_id = connected_scene
            elif self._resolve_viewer3d_scene_context_id() is None:
                self._viewer3d_node_id = self._first_scene3d_node_id()
        # 基础参数集（Scene3D 内置相机 + gizmo object 变换）
        keys = ["cam_orbit_yaw", "cam_orbit_pitch", "cam_dist",
                "cam_target_x", "cam_target_y", "cam_target_z",
                "object_pos_x", "object_pos_y", "object_pos_z"]
        # 图标拖动：Camera3D/Light3D 的位置参数（按节点类型补充）
        nt = node.node_type
        if nt == "camera3d":
            keys += ["cam_eye_x", "cam_eye_y", "cam_eye_z",
                     "cam_roll", "cam_ortho_scale"]
        elif nt == "light3d":
            keys += ["light_pos_x", "light_pos_y", "light_pos_z",
                     "icon_pos_x", "icon_pos_y", "icon_pos_z"]
        elif nt == "deformer3d":
            # F13：Deformer3D gizmo 拖拽改变 center_x/y/z（变形中心）
            keys += ["center_x", "center_y", "center_z"]
        self._viewer3d_nav_before = {k: node.parameters.get(k) for k in keys}

    def _on_viewer3d_nav_preview(self, node_id: str, params: dict) -> None:
        """Apply preview camera params during drag (throttled re-render).

        Writes params directly to the graph (no undo entry — the drag may
        fire many preview updates) and triggers a PREVIEW render.

        P6 最小闭环：相机变化通过 _view_signature 的相机段绕过 renderer Layer-1
        dedup；模型/几何 transform 预览走 NodeGraph.set_node_parameter，并对下一帧
        设 force_refresh，避免 Model3D/Geo3D 的缓存结果在拖动中复用旧 transform。
        """
        if node_id == self._VIEWER3D_VIRTUAL_NODE_ID:
            if not getattr(self, "_viewer3d_active_camera_id", None):
                self._apply_viewer3d_default_nav_params(params)
                if self._submit_view_only_3d_render(params):
                    return
            self._evaluate_graph_now()
            return
        # 阶段七：选中 Scene3D 但连了 Camera3D → 导航重定向到 Camera3D
        node_id = self._resolve_nav_target(node_id)
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if not node:
            return
        realtime_preview = self._is_realtime_3d_transform_preview(node, params)
        graph_setter = getattr(self._node_graph, "set_node_parameter", None)
        for k, v in params.items():
            if realtime_preview and callable(graph_setter):
                graph_setter(node_id, k, v)
            else:
                node.set_parameter(k, v)
        # graph_setter mutates the graph node in place; refresh the local ref for
        # sync paths backed by light-weight test doubles or alternate graph impls.
        node = self._node_graph.get_node(node_id) if self._node_graph else node
        self._sync_active_viewer_camera_if_needed(node_id, node)
        if realtime_preview:
            if getattr(node, "node_type", "") in {"geo3d", "model3d"}:
                self._force_next_view_refresh = True
            self._evaluate_graph_now()
        else:
            self._evaluate_graph()

    def _submit_view_only_3d_render(self, params: dict) -> bool:
        """Submit a cached Scene3D view-only redraw when it is safe to do so.

        The virtual viewer scene owns the default orbit camera. Its scene
        payloads and GPU resources are already resident in the render thread,
        so camera navigation can publish a fresh request and redraw only that
        Scene3D instance. External Camera3D navigation and any forced refresh
        continue through the complete graph path because they may change scene
        payloads or graph wiring.
        """
        renderer = self.__dict__.get("_renderer")
        node_graph = self.__dict__.get("_node_graph")
        viewer = self.__dict__.get("_viewer")
        if (
            renderer is None
            or node_graph is None
            or not self.__dict__.get("_is_active", False)
            or viewer is None
            or viewer.get_view_mode() != "3d"
            or self.__dict__.get("_viewer3d_active_camera_id")
            or self.__dict__.get("_current_render_mode") != RenderMode.PREVIEW
            or self.__dict__.get("_force_next_view_refresh", False)
        ):
            return False
        allowed = set(self._viewer3d_nav_param_keys())
        if not params or any(key not in allowed for key in params):
            return False
        request = self._build_render_request()
        if request is None or request.force_refresh:
            return False
        if request.viewer_node_id != self._VIEWER3D_VIRTUAL_NODE_ID:
            return False
        if request.playback_drive is not None:
            return False
        renderer.submit(CompositeCommand.RENDER_3D_VIEW, {
            "node_id": self._VIEWER3D_VIRTUAL_NODE_ID,
            "cam_params": {
                key: float(value) for key, value in params.items()
                if key in allowed
            },
            "request": request,
        })
        return True

    def _is_realtime_3d_transform_preview(self, node, params: dict) -> bool:
        """Return True for 3D node edits that should redraw immediately.

        Numeric scrubbing in the 3D toolchain is expected to behave like a DCC:
        transforms, camera optics, light settings, and Scene3D render controls
        all need direct visual feedback while the mouse is down.
        """
        node_type = getattr(node, "node_type", "")
        if node_type not in {"geo3d", "model3d", "camera3d", "light3d",
                             "scene3d", "deformer3d"}:
            return False
        return bool(params)

    def _sync_active_viewer_camera_if_needed(self, node_id: str, node) -> None:
        """Keep the live 3D Viewer camera in sync with active Camera3D edits."""
        if (getattr(node, "node_type", None) != "camera3d"
                or getattr(self, "_viewer3d_active_camera_id", None) != node_id
                or self._viewer is None):
            return
        sync = getattr(self._viewer, "look_through_camera_params", None)
        if callable(sync):
            sync(dict(getattr(node, "parameters", {}) or {}))

    def _on_viewer3d_nav_committed(self, node_id: str, params: dict) -> None:
        """Push a single merged undo entry for the completed navigation.

        Builds one param_change undo action per changed camera param, all
        wrapped in a merge coalescing window so they collapse into one undo
        step (yaw + pitch + dist move together).

        Milestone B / P1 + P1-5：导航结束**不立即**恢复高质量。启动恢复 timer
        （nav_restore_delay_ms，默认 150ms），延迟后才清 _nav3d_active 并请求高质量
        帧。这样连续小停顿（拖一下→松→又拖）不会反复在低/高质量间抖动。undo 提交
        和 nav_preview 渲染仍立即进行（参数已落地）。
        """
        # P1-5：启动延迟恢复 timer（不立即清 _nav3d_active）。
        self._schedule_nav3d_restore()
        if node_id == self._VIEWER3D_VIRTUAL_NODE_ID:
            if not getattr(self, "_viewer3d_active_camera_id", None):
                self._apply_viewer3d_default_nav_params(params)
            # P6：相机变化由 _view_signature 捕获，不需要 force_refresh
            self._evaluate_graph()
            self._viewer3d_nav_before = {}
            return
        # 阶段七：选中 Scene3D 但连了 Camera3D → 导航重定向到 Camera3D
        node_id = self._resolve_nav_target(node_id)
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if not node:
            return
        before = self._viewer3d_nav_before
        if not before:
            return
        # Commit each changed param as an undo entry, merged into one step.
        self._undo_stack.begin_merge_coalescing()
        try:
            for k, new_val in params.items():
                old_val = before.get(k, new_val)
                if old_val == new_val:
                    continue
                action = create_param_change_action(
                    node_id=node_id,
                    param_name=k,
                    old_value=old_val,
                    new_value=new_val,
                    undo_fn=lambda nid=node_id, pn=k, ov=old_val: self._undo_viewer3d_param(nid, pn, ov),
                    redo_fn=lambda nid=node_id, pn=k, nv=new_val: self._undo_viewer3d_param(nid, pn, nv),
                )
                self._undo_stack.push(action)
        finally:
            self._undo_stack.end_merge_coalescing()
        self._viewer3d_nav_before = {}

    def _schedule_nav3d_restore(self) -> None:
        """P1-5：启动导航恢复 timer（延迟回到高质量）。

        从全局 3D Scene Settings 读 nav_restore_delay_ms（默认 150ms）。timer 已
        在跑则不重启（避免连续释放时反复延迟）。新一次 nav_started 会 stop。
        """
        _restore_timer = getattr(self, "_nav3d_restore_timer", None)
        if _restore_timer is None:
            return  # 未初始化（测试/构造未完成），直接返回（不延迟恢复）
        if _restore_timer.isActive():
            return
        delay_ms = 150
        gs = self._global_3d_scene_settings() or {}
        try:
            delay_ms = max(0, int(gs.get("nav_restore_delay_ms", 150)))
        except (TypeError, ValueError):
            delay_ms = 150
        # delay_ms=0 表示立即恢复（兼容想要即时反馈的用户）
        if delay_ms <= 0:
            self._on_nav3d_restore()
        else:
            _restore_timer.setInterval(delay_ms)
            _restore_timer.start()

    def _on_nav3d_restore(self) -> None:
        """P1-5：恢复 timer 触发——清导航交互态，请求一帧高质量渲染。

        清 _nav3d_active 后，下一次 _evaluate_graph 的 interaction_active 变 False
        → Scene3D 回到 PREVIEW profile（恢复完整 MSAA/IBL/PBR）。
        P6：交互态(nav0/nav1)已纳入 _view_signature，profile 变化对 dedup 可见，
        不再需要 force_refresh（避免炸掉 eval cache）。
        """
        if not self._nav3d_active:
            return
        self._nav3d_active = False
        self._evaluate_graph()

    def _undo_viewer3d_param(self, node_id: str, param_name: str, value) -> None:
        """Apply a camera param (used by undo/redo callbacks)."""
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if node:
            node.set_parameter(param_name, value)
            self._refresh_properties_panel()
            self._evaluate_graph()
            # Sync the fused viewer's internal 3D state so redo keeps the cursor.
            if self._viewer is not None and hasattr(self._viewer, 'sync_scene3d_params'):
                self._viewer.sync_scene3d_params({param_name: value})

    def _on_active_camera_requested(self, scene3d_id: str, camera_id: str) -> None:
        """双击 Camera3D 图标 → 设为 Scene3D 的活动渲染相机（带 undo）。

        写 scene3d 的 active_camera_id 参数（旧值→新值），推一个 param_change
        undo action，立即重渲染。若已是活动相机则无操作。
        """
        if not self._node_graph:
            return
        node = self._node_graph.get_node(scene3d_id)
        if node is None or node.node_type != "scene3d":
            return
        old_val = str(node.parameters.get("active_camera_id", ""))
        if old_val == camera_id:
            return  # 已是活动相机，无需切换
        # 立即应用 + 重渲染（让视口即时反馈）
        node.set_parameter("active_camera_id", camera_id)
        self._evaluate_graph()
        # 推 undo（用 nav 同款回调，写参数 + 刷新 + 重渲染）
        action = create_param_change_action(
            node_id=scene3d_id,
            param_name="active_camera_id",
            old_value=old_val,
            new_value=camera_id,
            undo_fn=lambda nid=scene3d_id, ov=old_val: self._undo_viewer3d_param(nid, "active_camera_id", ov),
            redo_fn=lambda nid=scene3d_id, nv=camera_id: self._undo_viewer3d_param(nid, "active_camera_id", nv),
        )
        self._undo_stack.push(action)
        if self._viewer is not None:
            self._viewer.update()

    @property
    def viewer(self):
        """Access the connected CompositeViewer."""
        return self._viewer

    def set_timeline_clip_context(
        self,
        runtime_context: Any = None,
        gpu_provider: Any = None,
    ) -> None:
        """Set the runtime context and GPU provider for timeline clip input.

        Called by MainWindow/EditController when entering Composite from
        a timeline clip context so that TimelineClipInputNode can access
        the correct frame data.

        Args:
            runtime_context: CompositeRuntimeContext instance.
            gpu_provider: TimelineClipFrameProvider instance.
        """
        previous = self._runtime_context
        previous_clip_key = (
            getattr(previous, "timeline_id", None),
            getattr(previous, "clip_id", None),
            getattr(previous, "asset_id", None),
        )
        next_clip_key = (
            getattr(runtime_context, "timeline_id", None),
            getattr(runtime_context, "clip_id", None),
            getattr(runtime_context, "asset_id", None),
        )
        clip_changed = previous_clip_key != next_clip_key
        self._runtime_context = runtime_context
        self._timeline_clip_gpu_provider = gpu_provider
        if runtime_context is not None:
            self._composite_fps = float(
                getattr(runtime_context, "fps", 24.0) or 24.0
            )
            self._composite_duration_frames = int(
                getattr(runtime_context, "duration_frames", 0) or 0
            )
            self._node_graph.fps = self._composite_fps
            self._node_graph.duration_frames = self._composite_duration_frames
        if getattr(type(self), "_load_project_settings_metadata", None) is not None:
            self._load_project_settings_metadata(runtime_context)
        if getattr(type(self), "_sync_keyframes_panel_frame_range", None) is not None:
            self._sync_keyframes_panel_frame_range()
        # 缓存片段在时间线上的起始帧，供 page_controller 做全局帧↔clip本地帧映射。
        self._comp_clip_timeline_start = int(
            getattr(runtime_context, 'clip_timeline_start', 0) or 0
        ) if runtime_context is not None else 0
        # P8: keep the composition-local frame in sync with the runtime context
        # so keyframe animation uses clip_local_frame during playback.
        if runtime_context is not None:
            local = getattr(runtime_context, 'clip_local_frame', None)
            if local is None:
                local = getattr(runtime_context, 'timeline_frame', 0)
            if local is not None:
                # Local-only apply: syncs the Keyframes/Spline/Properties
                # playheads with the timeline echo without re-seeking the
                # transport (loop-free during playback).
                if getattr(type(self), "_apply_composite_frame", None) is not None:
                    self._apply_composite_frame(int(local or 0))
        if clip_changed:
            self._last_texture_id = None
            self._last_texture_width = 0
            self._last_texture_height = 0
            self._last_metadata = {}
            # 同素材的段切换（刀片切开的相邻段）保留解码纹理缓存与当前画面：
            # 持帧是跳转路径的核心——清空后 _hold_texture_for_asset 落空，新段
            # 首帧退化为渲染线程同步 GOP 重解（可达数百 ms），拖拽就会卡顿。
            # 素材真正变化时才清缓存并黑屏等待新源首帧。
            same_asset = (
                previous_clip_key[2] is not None
                and previous_clip_key[2] == next_clip_key[2]
            )
            if not same_asset:
                if hasattr(gpu_provider, "clear_source_cache"):
                    gpu_provider.clear_source_cache()
                if self._viewer is not None:
                    self._viewer.set_texture(0, 0, 0)

        if runtime_context is not None:
            for node in self._node_graph.nodes.values():
                if getattr(node, "node_type", "") == "timeline_clip_input":
                    if clip_changed and hasattr(node, "set_cached_texture"):
                        node.set_cached_texture(None)
                    node.set_parameter("timeline_id", getattr(runtime_context, "timeline_id", "") or "")
                    node.set_parameter("clip_id", getattr(runtime_context, "clip_id", "") or "")
                    node.set_parameter("asset_id", getattr(runtime_context, "asset_id", "") or "")
        if self._is_active and self._renderer and self._node_graph:
            # TimelineClipInput is a dynamic source and is marked dirty by the
            # render scheduler on every request. Mutating graph generation for
            # every playhead tick incorrectly invalidates every node/cache.
            self._evaluate_graph()

    def _on_viewer_interaction_started(self, state) -> None:
        """Handle viewer interaction start — record original node params.

        This is called when the user starts dragging a handle in the viewer.
        We record the original node parameters for undo and set render mode
        to PREVIEW for faster feedback during the drag.

        Args:
            state: ViewerInteractionState from the viewer.
        """
        node_id = state.selected_node_id
        if not node_id:
            return

        node = self._node_graph.get_node(node_id)
        if not node:
            return

        # Record original parameters for undo
        self._interaction_original_params = dict(node.parameters)
        self._interaction_active = True
        # Snapshot keyframe tracks: animated handle drags write keyframes at
        # the current frame and get track-based undo instead of param undo.
        import copy as _copy
        self._interaction_tracks_before = _copy.deepcopy(
            getattr(node, 'keyframe_tracks', {}) or {})

        # Set PREVIEW render mode for faster feedback
        self._current_render_mode = RenderMode.PREVIEW

        # Update interaction state with original params. Animated params must
        # start from the *interpolated* value at the current frame, otherwise
        # a handle drag on an animated node jumps back to the stale static
        # value before moving.
        state.original_params = self._evaluated_node_params(
            node, self._current_keyframe_frame())

        logger.debug(f"Viewer interaction started: node={node_id}, "
                     f"handle={state.active_handle}, params={self._interaction_original_params}")

    def _on_viewer_interaction_updated(self, state) -> None:
        """Handle viewer interaction update — throttle node param updates.

        This is called during drag to update node parameters for preview.
        We use a debounce timer to throttle updates and avoid excessive renders.

        Args:
            state: ViewerInteractionState from the viewer.
        """
        if not self._interaction_active:
            return

        node_id = state.selected_node_id
        if not node_id:
            return

        # Compute new parameter values from interaction delta
        # This depends on the tool mode and active handle
        new_params = self._compute_params_from_interaction(state)
        if not new_params:
            return

        # Accumulate pending update (keep original old, update new)
        if self._pending_interaction_update is None:
            self._pending_interaction_update = {
                "node_id": node_id,
                "params": new_params,
            }
        else:
            # Merge new params with existing pending update
            self._pending_interaction_update["params"].update(new_params)

        # CornerPin drags preview the warp live so moving a source corner
        # re-anchors the deformation in real time. Holding Shift switches to
        # Nuke-style "freeze" (destination-corner drags only): the image stays
        # at its current state while the viewer overlay (quad outline +
        # handles) follows the pointer, and the accumulated params are flushed
        # once on release — useful for precise corner alignment when the live
        # warp would drift under the pointer. Source-corner drags stay live:
        # without Shift they keep from==to (identity, no warp anyway) and
        # Alt+Shift source-region editing needs live feedback too.
        if (state.tool == ViewerToolMode.CORNERPIN
                and _modifiers_have_shift(state.modifier_keys)
                and state.active_handle
                and state.active_handle.startswith("to")):
            self._interaction_debounce_timer.stop()
            return

        # Throttle rather than debounce: keep the latest params while the timer
        # is active, but do not restart it on every mouse move. Otherwise a
        # continuous fast drag can postpone rendering until mouse release.
        if not self._interaction_debounce_timer.isActive():
            self._interaction_debounce_timer.start()

    def _flush_interaction_update(self) -> None:
        """Flush pending interaction update to node graph.

        Called by debounce timer to apply parameter changes.
        Uses PREVIEW render mode for fast feedback.

        Also updates properties panel and viewer handles for real-time
        value synchronization (S5-14).
        """
        if self._pending_interaction_update is None:
            return

        node_id = self._pending_interaction_update["node_id"]
        params = self._pending_interaction_update["params"]

        # Apply parameter changes through the graph-level setter so the graph
        # generation bumps. The renderer's skip-if-same-signature logic keys on
        # snapshot.generation; a direct node.set_parameter() leaves generation
        # unchanged, which froze the image for the entire drag (it only
        # re-rendered on release, when the PREVIEW→FULL mode flip broke the
        # skip). This is the same path every other param change already uses.
        node = self._node_graph.get_node(node_id)
        animated = set(getattr(node, 'keyframe_tracks', {}).keys()) \
            if node is not None else set()
        frame = self._current_keyframe_frame()
        for param_name, value in params.items():
            if param_name in animated:
                # AE style: dragging an animated param rewrites the keyframe
                # at the current frame — the static value is shadowed by the
                # track, so writing it would have no visible effect.
                self._write_interaction_keyframe(node, param_name, frame, value)
            else:
                self._node_graph.set_node_parameter(node_id, param_name, value)

        # Clear pending update
        self._pending_interaction_update = None

        # Trigger preview render immediately rather than via the eval debounce:
        # the interaction timer already throttles this flush to ~60fps, so the
        # extra 16ms eval debounce only added drag-to-render latency without
        # coalescing benefit during a drag.
        self._evaluate_graph_now()

        # Update properties panel to show current values (S5-14)
        self._refresh_properties_panel(node_id)

        # Update viewer handles to match new parameter values (S5-14)
        self.update_viewer_handles()

    def _on_viewer_interaction_ended(self, state, original_params: dict) -> None:
        """Handle viewer interaction end — commit undo command.

        This is called when the user releases the mouse after dragging.
        We record the final parameters and create an undo command for the
        complete change, then trigger a FULL quality render.

        Args:
            state: ViewerInteractionState from the viewer.
            original_params: Original node parameters (from interaction_started).
        """
        node_id = state.selected_node_id
        if not node_id or not self._interaction_active:
            self._interaction_active = False
            return

        node = self._node_graph.get_node(node_id)
        if not node:
            self._interaction_active = False
            return

        # Flush any pending update first
        self._interaction_debounce_timer.stop()
        if self._pending_interaction_update:
            self._flush_interaction_update()

        # Get final parameters
        final_params = dict(node.parameters)

        # Set final params in state for external reference
        state.set_final_params(final_params)

        # Create undo command if parameters changed
        has_change = False
        animated = set(self._interaction_tracks_before.keys()) | \
            set(getattr(node, 'keyframe_tracks', {}).keys())
        for param_name, final_value in final_params.items():
            if param_name in animated:
                continue  # animated params get track-based undo below
            original_value = original_params.get(param_name)
            if original_value != final_value:
                has_change = True
                break

        if has_change:
            # Create a single undo action for the entire drag
            # Use mergeable action so consecutive drags can be merged
            self._undo_stack.begin_merge_coalescing()

            for param_name, final_value in final_params.items():
                if param_name in animated:
                    continue
                original_value = original_params.get(param_name)
                if original_value != final_value:
                    action = create_param_change_action(
                        node_id=node_id,
                        param_name=param_name,
                        old_value=original_value,
                        new_value=final_value,
                        undo_fn=lambda d: self._undo_param(d),
                        redo_fn=lambda d: self._redo_param(d),
                        merge_window_ms=500,
                    )
                    self._undo_stack.push(action)

            self._undo_stack.end_merge_coalescing()
            logger.debug(f"Viewer interaction ended: node={node_id}, "
                         f"undo recorded for {len(final_params)} params")

        # Track-based undo for keyframes written during the drag.
        import copy as _copy
        tracks_changed = False
        for pname in animated:
            before = self._interaction_tracks_before.get(pname)
            after = _copy.deepcopy(node.keyframe_tracks.get(pname))
            if before == after:
                continue
            if not tracks_changed:
                self._undo_stack.begin_merge_coalescing()
                tracks_changed = True
            self._push_keyframe_track_undo(node_id, pname, before, after)
        if tracks_changed:
            self._undo_stack.end_merge_coalescing()
            # The keyframes panel's model must reflect the new keyframes.
            self._refresh_keyframes_and_spline_for_node(node_id)
            self.refresh_transport_keyframe_markers()

        # Switch to FULL render mode for final quality
        self._current_render_mode = RenderMode.FULL
        self._evaluate_graph()

        # Reset interaction state
        self._interaction_active = False
        self._interaction_original_params.clear()
        self._interaction_tracks_before = {}

        # Refresh properties panel
        self._refresh_properties_panel(node_id)
        self.update_viewer_handles()

    # ── C2-3: PAINT tool slots ────────────────────────────────────────────

    def _on_viewer_color_picked(self, r: float, g: float, b: float,
                                 a: float) -> None:
        """CP-056: viewer 吸管取色写回属性面板最近编辑的颜色参数。"""
        panel = getattr(self, "_properties_panel", None)
        if panel is not None and hasattr(panel, "apply_picked_color"):
            panel.apply_picked_color(r, g, b)

    def _on_paint_stroke_committed(self, node_id: str, geometry: dict) -> None:
        """Assemble one stroke from the node's brush defaults, append it to
        the node's stroke list, and record exactly ONE undo action."""
        graph = getattr(self, "_node_graph", None)
        if graph is None:
            return
        node = graph.get_node(str(node_id))
        if node is None or getattr(node, "node_type", "") != "paint":
            return
        points = geometry.get("points") or []
        if not points:
            return
        params = node.parameters
        stroke = node.make_stroke(
            str(params.get("brush_type", "clone")),
            points,
            size=float(geometry.get("size", 0.02)),
            feather=float(params.get("brush_feather", 0.5)),
            opacity=float(params.get("brush_opacity", 1.0)),
            src_offset=list(geometry.get("src_offset") or [0.0, 0.0]),
            color=[
                float(params.get("brush_color_r", 0.0)),
                float(params.get("brush_color_g", 0.0)),
                float(params.get("brush_color_b", 0.0)),
                float(params.get("brush_color_a", 1.0)),
            ],
        )
        old_json = str(params.get("strokes", "[]"))
        new_strokes = node.get_strokes() + [stroke]
        node.set_strokes(new_strokes)
        new_json = str(params.get("strokes", "[]"))

        def _set_strokes_json(node_id: str, json_text: str) -> None:
            graph = getattr(self, "_node_graph", None)
            target = graph.get_node(node_id) if graph is not None else None
            if target is not None and getattr(target, "node_type", "") == "paint":
                target.set_parameter("strokes", json_text)
                self._evaluate_graph_now()

        node_id_text = str(node_id)
        self._undo_stack.push(UndoAction(
            "paint_add_stroke",
            {
                "node_id": node_id_text,
                "undo": {"node_id": node_id_text, "json": old_json},
                "redo": {"node_id": node_id_text, "json": new_json},
            },
            undo_fn=lambda data: _set_strokes_json(
                data["undo"]["node_id"], data["undo"]["json"]),
            redo_fn=lambda data: _set_strokes_json(
                data["redo"]["node_id"], data["redo"]["json"]),
            description=f"Paint stroke ({stroke['type']})",
        ))
        self._evaluate_graph_now()

    def _on_paint_brush_size_changed(self, node_id: str, size_px: float) -> None:
        """Mirror the [ / ] tool shortcut onto the node's brush_size
        parameter. Not undoable — a tool preference, not a content edit."""
        graph = getattr(self, "_node_graph", None)
        if graph is None:
            return
        node = graph.get_node(str(node_id))
        if node is None or getattr(node, "node_type", "") != "paint":
            return
        self._node_graph.set_node_parameter(
            str(node_id), "brush_size", float(size_px))
        self._evaluate_graph_now()

    # ── B3-2: parameter promotion (publish) ───────────────────────────────

    def _promote_param_to_group(self, node_id: str, param_name: str) -> None:
        """Publish a member parameter onto its owning group (panel menu).

        Lazily materializes the group's GroupNode instance (Ctrl+G groups
        only carry _node_groups metadata), promotes the parameter via
        GroupNode.promote_param (engine binding drives the member), and
        records the whole thing as one external composite edit (undo).
        """
        graph = getattr(self, "_node_graph", None)
        if graph is None:
            return
        try:
            group_meta = graph.get_group_for_node(node_id)
        except Exception:
            group_meta = None
        if not isinstance(group_meta, dict):
            return
        gid = str(group_meta.get("id") or "")
        if not gid:
            return

        node_id_text = f"published_{gid}"
        # Snapshot BEFORE materializing the container — the undo restore
        # must land on a graph without it, or the first promote's undo
        # leaves a permanent ghost GroupNode on the canvas (and in saves).
        before = graph.to_dict()
        group_node = graph.get_node(node_id_text)
        created_container = group_node is None
        if group_node is None:
            group_node = GroupNode(node_id=node_id_text)
            group_node.member_node_ids = list(group_meta.get("node_ids", []))
            graph.add_node(group_node)
            # Mirror the group title onto the container for readability.
            if group_meta.get("title"):
                group_node.set_parameter("group_name", str(group_meta["title"]))
        else:
            group_node.member_node_ids = list(group_meta.get("node_ids", []))

        ok, result = group_node.promote_param(node_id, param_name, graph)
        if not ok:
            if created_container:
                # Failed promote must not leave the container behind.
                try:
                    graph.remove_node(node_id_text)
                except Exception:
                    logger.debug("promote rollback failed", exc_info=True)
            return
        after = graph.to_dict()
        self.record_external_composite_edit(before, after)

    def _compute_params_from_interaction(self, state) -> dict:
        """Compute new node parameters from interaction state.

        This method translates viewer interaction deltas to node parameter
        changes based on the tool mode and active handle.

        Uses preview_params from interaction state which contains constrained
        and snapped delta values (S5-14).

        Args:
            state: ViewerInteractionState containing drag info.

        Returns:
            Dict of parameter name -> new value, or empty dict if no change.
        """
        node_id = state.selected_node_id
        if not node_id:
            return {}

        node = self._node_graph.get_node(node_id)
        if not node:
            return {}

        transform_input_w, transform_input_h = self._resolve_transform_interaction_size(node)

        # Text allows scaling up to 20x (its PARAM_DEFS max_val), vs the 5x
        # cap that suits Transform nodes.
        max_scale = 20.0 if node.node_type == "text" else 5.0

        new_params = {}
        tool = state.tool
        handle = state.active_handle

        # Check for precomputed preview params (S5-14)
        preview_params = state.preview_params
        if preview_params:
            # Use constrained and snapped values from viewer
            if tool == ViewerToolMode.TRANSFORM:
                delta_x = preview_params.get("delta_x", 0.0)
                delta_y = preview_params.get("delta_y", 0.0)

                # Handle different transform operations
                if handle in ("center", "move"):
                    # Translate: add delta to original translate values
                    orig_params = state.original_params
                    orig_tx = orig_params.get("translate_x", 0.0)
                    orig_ty = orig_params.get("translate_y", 0.0)
                    new_params["translate_x"] = orig_tx + delta_x
                    new_params["translate_y"] = orig_ty + delta_y

                elif handle == "pivot":
                    # Pivot: add delta to original pivot values
                    orig_params = state.original_params
                    if orig_params.get("pivot_mode", "center") == "center":
                        orig_px = transform_input_w / 2.0
                        orig_py = transform_input_h / 2.0
                    else:
                        orig_px = orig_params.get("pivot_x", transform_input_w / 2.0)
                        orig_py = orig_params.get("pivot_y", transform_input_h / 2.0)
                    new_params["pivot_x"] = orig_px + delta_x
                    new_params["pivot_y"] = orig_py + delta_y
                    new_params["pivot_mode"] = "custom"

                elif preview_params.get("is_scale"):
                    # Scale: use compute_transform_delta_params for scale handles
                    from .viewer_handles import compute_transform_delta_params
                    start_pos = (state.drag_start_image_pos.x(), state.drag_start_image_pos.y())
                    current_pos = (state.drag_current_image_pos.x(), state.drag_current_image_pos.y())
                    delta_input_w = transform_input_w
                    delta_input_h = transform_input_h
                    delta_original = state.original_params
                    # Text nodes: drag coords are canvas-space but the handle
                    # system works in bbox-texture-local space. Remap so
                    # compute_transform_delta_params produces correct ratios.
                    if node.node_type == "text":
                        try:
                            bx, by, bw, bh = node.text_handle_bbox()
                            start_pos = (start_pos[0] - bx, start_pos[1] - by)
                            current_pos = (current_pos[0] - bx, current_pos[1] - by)
                            delta_input_w, delta_input_h = bw, bh
                            delta_original = self._text_user_facing_params(state.original_params, node)
                        except Exception:
                            pass
                    new_params = compute_transform_delta_params(
                        handle,
                        start_pos,
                        current_pos,
                        delta_original,
                        delta_input_w,
                        delta_input_h,
                        state.modifier_keys,
                        max_scale=max_scale,
                    )

                elif preview_params.get("is_rotate"):
                    # Rotation: use compute_transform_delta_params for rotate handle
                    from .viewer_handles import compute_transform_delta_params
                    start_pos = (state.drag_start_image_pos.x(), state.drag_start_image_pos.y())
                    current_pos = (state.drag_current_image_pos.x(), state.drag_current_image_pos.y())
                    delta_input_w = transform_input_w
                    delta_input_h = transform_input_h
                    delta_original = state.original_params
                    if node.node_type == "text":
                        try:
                            bx, by, bw, bh = node.text_handle_bbox()
                            start_pos = (start_pos[0] - bx, start_pos[1] - by)
                            current_pos = (current_pos[0] - bx, current_pos[1] - by)
                            delta_input_w, delta_input_h = bw, bh
                            delta_original = self._text_user_facing_params(state.original_params, node)
                        except Exception:
                            pass
                    new_params = compute_transform_delta_params(
                        handle,
                        start_pos,
                        current_pos,
                        delta_original,
                        delta_input_w,
                        delta_input_h,
                        state.modifier_keys,
                        max_scale=max_scale,
                    )

            elif tool == ViewerToolMode.CROP:
                # Use precomputed crop params from viewer
                if "x" in preview_params:
                    new_params = preview_params.copy()

            return new_params

        # Fallback: compute from raw delta (legacy behavior)
        delta = state.get_image_delta()

        # Tool-specific parameter computation
        if tool == ViewerToolMode.TRANSFORM:
            # Transform node parameters: translate_x, translate_y, scale, rotation
            # Use compute_transform_delta_params for all transform handles
            from .viewer_handles import compute_transform_delta_params
            start_pos = (state.drag_start_image_pos.x(), state.drag_start_image_pos.y())
            current_pos = (state.drag_current_image_pos.x(), state.drag_current_image_pos.y())
            delta_input_w = transform_input_w
            delta_input_h = transform_input_h
            delta_original = state.original_params
            if node.node_type == "text":
                try:
                    bx, by, bw, bh = node.text_handle_bbox()
                    start_pos = (start_pos[0] - bx, start_pos[1] - by)
                    current_pos = (current_pos[0] - bx, current_pos[1] - by)
                    delta_input_w, delta_input_h = bw, bh
                    delta_original = self._text_user_facing_params(state.original_params, node)
                except Exception:
                    pass
            new_params = compute_transform_delta_params(
                handle or "center",
                start_pos,
                current_pos,
                delta_original,
                delta_input_w,
                delta_input_h,
                state.modifier_keys,
                max_scale=max_scale,
            )

        elif tool == ViewerToolMode.CROP:
            # Crop node parameters: x, y, width, height
            if handle and handle.startswith("crop_"):
                from .viewer_tool_system import compute_crop_rect_from_handles
                original = state.original_params.copy()
                if original:
                    new_params = compute_crop_rect_from_handles(handle, original, delta)

        elif tool == ViewerToolMode.CORNERPIN:
            # CornerPin parameters: to1-4 (destination) and from1-4 (source,
            # dragged with Alt). Both use the same normalized-delta math.
            if handle and handle.startswith(("to", "from")):
                # Handle format: "to1", "from2", etc.
                prefix = "to" if handle.startswith("to") else "from"
                idx = handle[len(prefix):] or "1"
                orig_params = state.original_params
                # Convert delta to normalized coordinates
                if self._last_texture_width > 0 and self._last_texture_height > 0:
                    norm_dx = delta.x() / self._last_texture_width
                    norm_dy = delta.y() / self._last_texture_height
                    orig_x = orig_params.get(f"{prefix}{idx}_x", 0.0)
                    orig_y = orig_params.get(f"{prefix}{idx}_y", 0.0)
                    new_params[f"{prefix}{idx}_x"] = orig_x + norm_dx
                    new_params[f"{prefix}{idx}_y"] = orig_y + norm_dy
                    if (prefix == "from"
                            and not _modifiers_have_shift(state.modifier_keys)):
                        # Alignment mode (screen replacement / corner pinning):
                        # carry the matching destination corner along by the same
                        # delta. While every from/to pair coincides the
                        # homography stays identity, so the image never warps
                        # while the user pins the corners onto e.g. a TV
                        # screen. Alt+Shift moves the source corner alone
                        # (redefine the source region).
                        twin = f"to{idx}"
                        new_params[f"{twin}_x"] = (
                            orig_params.get(f"{twin}_x", orig_x) + norm_dx)
                        new_params[f"{twin}_y"] = (
                            orig_params.get(f"{twin}_y", orig_y) + norm_dy)

        elif tool == ViewerToolMode.MASK or tool == ViewerToolMode.ROTO:
            # Mask/Roto handles use normalized shape parameters.  The viewer
            # already computes the complete handle set (center, rotate,
            # edges, corners and feather controls); route the drag through
            # the same geometry helper instead of the old center-only stub.
            # This is the part that makes the Composite mask feel like the
            # Color page mask tool during interactive editing.
            if handle and handle != "new_point":
                if tool == ViewerToolMode.ROTO and (
                        handle.startswith("roto_point_")
                        or handle.startswith("roto_ctrl_")):
                    # Roto point/tangent handles edit the node's JSON path
                    # directly. The renderer receives the same data, so Layer
                    # and Node views never maintain separate shapes.
                    import json
                    from .nodes.roto_shape_node import RotoShapeNode
                    try:
                        points = RotoShapeNode.parse_points(
                            state.original_params.get("points", "[]"))
                        parts = handle.split("_")
                        index = int(parts[2])
                        if 0 <= index < len(points):
                            x = (state.drag_current_image_pos.x()
                                 / max(1.0, float(self._last_texture_width)))
                            y = (state.drag_current_image_pos.y()
                                 / max(1.0, float(self._last_texture_height)))
                            point = points[index]
                            names = RotoShapeNode._point_param_names(index)
                            if handle.endswith("_c1"):
                                point["c1"] = [x, y]
                                new_params[names["c1_x"]] = x
                                new_params[names["c1_y"]] = y
                            elif handle.endswith("_c2"):
                                point["c2"] = [x, y]
                                new_params[names["c2_x"]] = x
                                new_params[names["c2_y"]] = y
                            else:
                                point["pos"] = [x, y]
                                new_params[names["x"]] = x
                                new_params[names["y"]] = y
                            new_params["points"] = json.dumps(
                                points, separators=(",", ":"))
                    except (TypeError, ValueError, IndexError, KeyError):
                        logger.debug("Invalid roto handle: %s", handle)
                elif tool == ViewerToolMode.ROTO and (
                        handle.startswith("warp_point_")):
                    # C7: mesh warp lattice points. The handle sits at
                    # base grid position + displacement; the drag target
                    # becomes the new displacement for that point. The
                    # lattice is read from the LIVE node so repeated
                    # move events accumulate on the latest state.
                    import json
                    from .nodes.warp_node import WarpNode
                    try:
                        warp_node = self._node_graph.get_node(
                            state.selected_node_id) \
                            if self._node_graph is not None else None
                        if warp_node is not None:
                            grid = warp_node.current_grid()
                            offsets = warp_node.get_offsets()
                            index = int(handle.split("_")[2])
                            row, col = divmod(index, grid + 1)
                            x = (state.drag_current_image_pos.x()
                                 / max(1.0, float(self._last_texture_width)))
                            y = (state.drag_current_image_pos.y()
                                 / max(1.0, float(self._last_texture_height)))
                            dx = max(-1.0, min(1.0, x - col / grid))
                            dy = max(-1.0, min(1.0, y - row / grid))
                            offsets[index] = [dx, dy]
                            names = WarpNode._point_param_names(index)
                            new_params[names["dx"]] = dx
                            new_params[names["dy"]] = dy
                            new_params["offsets"] = json.dumps(
                                offsets, separators=(",", ":"))
                    except (TypeError, ValueError, IndexError, KeyError):
                        logger.debug("Invalid warp handle: %s", handle)
                elif tool == ViewerToolMode.ROTO:
                    from .viewer_handles import compute_roto_delta_params
                    start_pos = (state.drag_start_image_pos.x(),
                                 state.drag_start_image_pos.y())
                    current_pos = (state.drag_current_image_pos.x(),
                                   state.drag_current_image_pos.y())
                    new_params = compute_roto_delta_params(
                        handle, start_pos, current_pos, state.original_params,
                        max(1, int(self._last_texture_width)),
                        max(1, int(self._last_texture_height)),
                        state.modifier_keys)
                else:
                    from .viewer_handles import compute_mask_delta_params
                    start_pos = (state.drag_start_image_pos.x(),
                                 state.drag_start_image_pos.y())
                    current_pos = (state.drag_current_image_pos.x(),
                                   state.drag_current_image_pos.y())
                    new_params = compute_mask_delta_params(
                        handle, start_pos, current_pos,
                        state.original_params,
                        max(1, int(self._last_texture_width)),
                        max(1, int(self._last_texture_height)),
                        state.modifier_keys)
            elif tool == ViewerToolMode.ROTO and handle == "new_point":
                import json
                from .nodes.roto_shape_node import RotoShapeNode
                points = RotoShapeNode.parse_points(
                    state.original_params.get("points", "[]"))
                x = (state.drag_current_image_pos.x()
                     / max(1.0, float(self._last_texture_width)))
                y = (state.drag_current_image_pos.y()
                     / max(1.0, float(self._last_texture_height)))
                points.append({"pos": [x, y], "c1": [x, y], "c2": [x, y]})
                new_params["points"] = json.dumps(
                    points, separators=(",", ":"))
                names = RotoShapeNode._point_param_names(len(points) - 1)
                new_params.update({
                    names["x"]: x, names["y"]: y,
                    names["c1_x"]: x, names["c1_y"]: y,
                    names["c2_x"]: x, names["c2_y"]: y,
                })
            elif tool == ViewerToolMode.MASK:
                return new_params

        elif tool == ViewerToolMode.SELECT:
            # Constant gradient/radial handles are always-on (selected node
            # shows its handles in SELECT mode without a dedicated tool). Route
            # the drag through compute_constant_delta_params.
            if handle and handle.startswith("constant_"):
                from .viewer_handles import compute_constant_delta_params
                start_pos = (state.drag_start_image_pos.x(), state.drag_start_image_pos.y())
                current_pos = (state.drag_current_image_pos.x(), state.drag_current_image_pos.y())
                new_params = compute_constant_delta_params(
                    handle,
                    start_pos,
                    current_pos,
                    state.original_params,
                    self._last_texture_width,
                    self._last_texture_height,
                    state.modifier_keys,
                )

        return new_params

    def get_node_output_regions(self) -> list:
        """Get node output regions for viewer hit testing (S5-10).

        Returns a list of NodeOutputRegion objects representing each
        node's output bounding box in image pixel coordinates.

        Returns:
            List of NodeOutputRegion objects.
        """
        regions = []
        for node_id, node in self._node_graph._nodes.items():
            if node.is_bypassed():
                continue

            # Get node's output bbox if available
            # For now, use full image as placeholder
            if self._last_texture_width > 0 and self._last_texture_height > 0:
                bbox = (0, 0, self._last_texture_width, self._last_texture_height)
                regions.append(NodeOutputRegion(
                    node_id=node_id,
                    node_type=node.node_type,
                    bbox=bbox,
                    label=node.node_title,
                    z_order=0,
                ))

        return regions

    def update_viewer_handles(self) -> None:
        """Update viewer tool handles based on selected node.

        Called when node selection changes or node parameters are updated.
        Computes handle positions in view coordinates and sends to viewer.
        Animated params are evaluated at the current frame so handles follow
        keyframe animation (AE style).
        """
        if not self._viewer:
            return

        node_id = self._viewer.get_selected_node()
        if not node_id and hasattr(self, "_node_editor"):
            node_id = self._node_editor.get_selected_node_id()
        if not node_id:
            self._viewer.set_tool_handles({})
            return

        node = self._node_graph.get_node(node_id)
        if not node:
            self._viewer.set_tool_handles({})
            return

        self._viewer.set_selected_node(node_id)
        self._viewer.set_node_info(
            node.node_type,
            self._evaluated_node_params(node, self._current_keyframe_frame()),
            node.format if hasattr(node, 'format') else None,
            input_size=self._resolve_node_input_size(node),
            node_instance=node,
            node_id=node_id,
        )

    def _evaluated_node_params(self, node, frame: int) -> dict:
        """node.parameters overlaid with interpolated keyframe values.

        Returns a copy; nodes without animation return the plain parameters.
        """
        params = dict(node.parameters)
        tracks = getattr(node, 'keyframe_tracks', None) or {}
        if not tracks:
            return params
        from src.shared.keyframe import ColorKeyframeTrack, Keyframe, KeyframeTrack
        for pname, track_data in tracks.items():
            if not isinstance(track_data, dict):
                continue
            kfs = track_data.get('keyframes')
            if not kfs:
                continue
            try:
                pdef = node.get_param_defs().get(pname)
                track = (ColorKeyframeTrack if getattr(pdef, "ptype", "") == "color"
                         or track_data.get("value_type") == "color"
                         else KeyframeTrack)(
                    param_name=pname,
                    min_val=float(track_data.get('min_val', 0.0)),
                    max_val=float(track_data.get('max_val', 1.0)),
                    pre_extrap=track_data.get('pre_extrap', 'constant'),
                    post_extrap=track_data.get('post_extrap', 'constant'))
                for kf_data in kfs:
                    track.add_keyframe(Keyframe.from_dict(kf_data))
                default = params.get(pname, (0.0, 0.0, 0.0, 1.0)
                                     if getattr(pdef, "ptype", "") == "color"
                                     else 0.0)
                params[pname] = track.get_value(int(frame), default=default)
            except Exception:
                continue
        return params

    def _refresh_viewer_handles_for_frame(self) -> None:
        """Follow the playhead: refresh handles of the animated selected node.

        Skipped during an active handle drag (the drag path owns the handle
        state) and for nodes without keyframe tracks (nothing to follow).
        """
        if self._interaction_active or not self._viewer:
            return
        node_id = None
        try:
            node_id = self._viewer.get_selected_node()
        except Exception:
            node_id = None
        if not node_id:
            return
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if node is None or not getattr(node, 'keyframe_tracks', None):
            return
        self.update_viewer_handles()

    def _write_interaction_keyframe(self, node, param_name: str,
                                    frame: int, value: float) -> None:
        """Write/update the keyframe at ``frame`` during a handle drag.

        Preserves the existing keyframe's interpolation/handle fields when a
        point already exists at the frame; otherwise inserts a linear point.
        """
        from src.shared.keyframe import Keyframe, KeyframeTrack
        track_data = node.keyframe_tracks.get(param_name)
        if not isinstance(track_data, dict):
            return
        frame = max(0, int(frame))
        kfs = []
        replaced = False
        for kf_data in track_data.get('keyframes', []):
            if not isinstance(kf_data, dict):
                continue
            if int(kf_data.get('frame', -1)) == frame:
                merged = dict(kf_data)
                merged['value'] = float(value)
                kfs.append(merged)
                replaced = True
            else:
                kfs.append(kf_data)
        if not replaced:
            kfs.append({'frame': frame, 'value': float(value),
                        'interpolation': 'linear'})
        pdef = node.get_param_defs().get(param_name)
        track = KeyframeTrack(
            param_name=param_name,
            min_val=pdef.min_val if pdef else 0.0,
            max_val=pdef.max_val if pdef else 1.0,
            pre_extrap=track_data.get('pre_extrap', 'constant'),
            post_extrap=track_data.get('post_extrap', 'constant'))
        for kf_data in kfs:
            try:
                track.add_keyframe(Keyframe.from_dict(kf_data))
            except Exception:
                continue
        node.set_keyframe_track(param_name, track)

    def _compute_node_handles(self, node) -> dict:
        """Compute viewer handle positions for a node.

        Args:
            node: Node instance with parameters.

        Returns:
            Dict mapping handle identifiers to view coordinates (QPointF).
        """
        from PyQt6.QtCore import QPointF

        handles = {}
        node_type = node.node_type

        # Center point for most transformable nodes
        if node_type in ("transform", "crop", "reformat", "cornerpin"):
            # Get center position from node parameters or default to image center
            center_x = node.parameters.get("center_x", self._last_texture_width / 2.0)
            center_y = node.parameters.get("center_y", self._last_texture_height / 2.0)

            if self._viewer:
                # Convert image position to view position
                center_view = self._viewer.get_view_pos_at_image_pos(
                    QPointF(center_x, center_y)
                )
                if center_view:
                    handles["center"] = center_view

        # Transform-specific handles
        if node_type == "transform":
            # Scale handles at corners and edges
            # This is placeholder - full implementation needs bbox calculation
            pass

        # Crop-specific handles
        if node_type == "crop":
            # Crop boundary handles
            # This is placeholder - full implementation needs crop bbox
            pass

        # CornerPin handles
        if node_type == "cornerpin":
            # Four corner positions
            for i in range(4):
                pos = node.parameters.get(f"to{i}", (0.0, 0.0))
                if self._viewer:
                    handle_view = self._viewer.get_view_pos_at_image_pos(
                        QPointF(pos[0], pos[1])
                    )
                    if handle_view:
                        handles[f"cornerpin_{i}"] = handle_view

        return handles
