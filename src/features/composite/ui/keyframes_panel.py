"""KeyframesPanel — Fusion-style bottom Dock for the Composite page.

This is the top-level panel widget. It assembles (per plan §3):
  - a top toolbar (nav / zoom / fit / expand / more)
  - a main editing area: left node tree + right timeline track view
  - a compact header readout (timecode)

Phase 3 (this file) delivers the *static* layout and the model binding. The
left tree and right timeline are real QWidget subclasses that paint their
own content; interaction (drag playhead / keyframe / range bar) lands in P5.

Visual styling comes from COMPOSITE_KEYFRAMES_* in style_sheet.py so the
panel matches the global dark theme instead of hard-coded DaVinci colors.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

from PyQt6.QtCore import QDateTime, QEvent, Qt, QTimer, QSize, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QCloseEvent,
    QCursor,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QLabel, QLineEdit, QPushButton,
    QSizePolicy, QSplitter, QMenu, QToolBar, QToolButton, QVBoxLayout,
    QWidget,
)

from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, COMPOSITE_KEYFRAMES_COMBO_STYLE, COMPOSITE_KEYFRAMES_ICON_BTN_STYLE,
    COMPOSITE_KEYFRAMES_PANEL_STYLE, COMPOSITE_KEYFRAMES_TOOLBAR_STYLE,
    COMPOSITE_KEYFRAMES_TREE_STYLE,
)

# Compact toolbar button metrics — the old default QToolBar sizing made the
# buttons visually dominate the slim Fusion-style dock.
_TOOLBAR_ICON_PX = 14
_TOOLBAR_BTN_W = 24
_TOOLBAR_BTN_H = 22

from .keyframes_model import (
    CompositeTimelineState, FrameMapper, KeyframesModel, KEYFRAMES_RULER_H,
    KEYFRAMES_ROW_H)
from . import timeline_layout
from .keyframes_tree_view import KeyframesTreeView
from .keyframes_timeline_view import KeyframesTimelineView
from .graph_view import GraphCanvasHost
from .timeline_icons import make_qicon
from .timeline_time_display import TimelineTimeDisplay
from .timeline_view_model import AnimationSelectionModel
from .timeline_expression import (
    EXPR_ERR_UNKNOWN,
    EXPRESSION_ERROR,
    ExpressionPolicy,
    ExpressionStatus,
    derive_expression_status,
)
from . import timeline_shortcuts

logger = logging.getLogger(__name__)

# AUD-10102: upper bound for the Value-column display cache. The cache is
# keyed by (node_id, param_name, frame); a comp with many animated params
# scrubbed across thousands of frames would otherwise grow without limit.
_DISPLAY_VALUE_CACHE_MAX = 8192
_DISPLAY_VALUE_MISS = object()


class KeyframesPanel(QWidget):
    """Top-level Composite Keyframes dock panel."""

    # Emitted when the user moves the playhead. CompositePage connects this to
    # set_composite_current_frame() so preview + keyframe eval stay in sync.
    current_frame_changed = pyqtSignal(int)
    # Emitted when a keyframe track is edited (add/remove/move). CompositePage
    # pushes undo + re-renders.
    track_changed = pyqtSignal(str, str, list)  # node_id, param_name, keyframes
    # Bracket multi-track timeline operations in one host undo group.
    track_batch_about_to_commit = pyqtSignal(str, int)
    track_batch_committed = pyqtSignal(str, int)
    undo_requested = pyqtSignal()
    redo_requested = pyqtSignal()
    # T123: emitted around a Graph-mode drag's commit burst so CompositePage
    # can wrap the (possibly multi-series) track_changed emits in ONE undo
    # group. Payload is the count of affected series.
    graph_drag_about_to_commit = pyqtSignal(int)
    graph_drag_committed = pyqtSignal(int)
    # Emitted when a node's time range bar is dragged. CompositePage writes
    # node.time_range and pushes undo.
    range_changed = pyqtSignal(str, dict)       # node_id, time_range dict
    # Internal host-facing variant that retains the transient drag kind
    # (trim_in/trim_out/slide). The public range_changed payload stays
    # backwards-compatible and contains only persisted time_range fields.
    range_edit_changed = pyqtSignal(str, dict)
    # P9: emitted when the user selects a node row in the tree.
    node_selected = pyqtSignal(str)
    # Emitted when the user selects a Layer-mode row.
    layer_selected = pyqtSignal(str)
    # Emitted when Delete/Backspace is pressed on a selected Layer row.
    layer_delete_requested = pyqtSignal(str)
    promote_node_requested = pyqtSignal(str)
    create_matte_layer_requested = pyqtSignal(str)
    attach_node_requested = pyqtSignal(str)
    # Emitted after a Layer-mode row drag is accepted by the controller.
    layer_reordered = pyqtSignal(str, int)
    # Emitted when the panel's play/pause button is toggled. CompositePage
    # connects this to its comp-clip playback engine; when nobody handles it
    # the panel falls back to its own internal playback timer.
    play_toggle_requested = pyqtSignal(bool)  # True = play, False = pause
    # Emitted when the AE-style work area (play range) changes — set via the
    # I/O keys or by dragging the band on the ruler. CompositePage forwards
    # it to the transport delegate (set_play_range) or the comp-clip timer.
    play_range_changed = pyqtSignal(int, int)  # in_frame, out_frame
    # T120: emitted when the Graph Expression Editor commits a validated edit.
    # CompositePage owns the authoritative NodeGraph write + undo record
    # (mirroring the properties-panel ◆ path). Payload: (node_id, param_name,
    # text). Validation failure is reported back via _expr_editor.show_error.
    graph_expression_changed = pyqtSignal(str, str, str)
    # T120: emitted when the Graph Expression Editor toggles evaluation while
    # keeping the source text. CompositePage owns mutation + undo.
    graph_expression_enabled_changed = pyqtSignal(str, str, bool)
    # AE property-row edits. The panel exposes intent; CompositePage owns the
    # graph mutation, undo record and render refresh.
    parameter_changed = pyqtSignal(str, str, object)
    # Bracket Value-column drags so the host can avoid rebuilding the whole
    # panel on every mouse-move event. The visual track still updates live;
    # the complete cross-panel refresh is performed on release.
    property_value_drag_started = pyqtSignal(str, str)
    property_value_drag_finished = pyqtSignal(str, str)
    parameter_binding_changed = pyqtSignal(str, str, str, str)
    parent_binding_changed = pyqtSignal(str, object)
    # Reuse CompositePage's existing New Comp Clip workflow from the empty
    # state, while keeping the dock itself available for the CTA click.
    create_empty_comp_clip_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("CompositeKeyframesPanel")
        self.setMinimumHeight(180)

        self._model = KeyframesModel()
        self._state = CompositeTimelineState()
        self._frame_mapper = FrameMapper()

        # Playback state. The external controller (CompositePage) owns the
        # real playback engine; the internal QTimer is a fallback so the
        # panel can animate standalone (unit tests / headless docks).
        self._playback_controller: Optional[object] = None
        self._disposed = False
        self._playing: bool = False
        # CP-058: 变速/往复播放（0.25x-4x；pingpong 在入出点翻转方向）
        self._playback_speed: float = 1.0
        self._play_pingpong: bool = False
        self._play_direction: int = 1
        self._play_accum: float = 0.0
        self._play_timer = QTimer(self)
        self._play_timer.setSingleShot(False)
        self._play_timer.timeout.connect(self._on_internal_play_tick)

        # Currently selected param row (target of add/remove-keyframe).
        self._selected_param: Optional[Tuple[str, str]] = None
        # Provides the live node param value when seeding a new keyframe.
        self._value_provider: Optional[Callable[[str, str], Optional[Any]]] = None
        # Deferred fit: set when the clip range changes before the dock has
        # a real width; consumed by resizeEvent.
        self._fit_pending: bool = False
        # Real composition extent (start, end). The ruler range may be
        # extended beyond it to reveal video media longer than the comp;
        # default layer bars and the work area stay clamped to this extent.
        # None = follow the ruler range exactly.
        self._comp_extent: Optional[Tuple[int, int]] = None
        # AE-style work area (play range). None = whole clip range.
        self._play_in: Optional[int] = None
        self._play_out: Optional[int] = None
        # The full composition is the initial AE-style work area. Keep this
        # separate from the coordinates so a deliberate Clear action can
        # still remove the overlay without being mistaken for an uninitialised
        # panel state on the next composition-range sync.
        self._play_range_explicit = False
        # Base values for AE-style horizontal value scrubbing in the property
        # column. The tree reports pixel delta; this panel applies ParamDef
        # step/bounds and routes the result through the normal graph path.
        self._value_drag_bases: dict[Tuple[str, str], object] = {}
        self._property_value_drag_active = False
        self._property_value_drag_target: Optional[Tuple[str, str]] = None
        self._pending_expression_binding: Optional[Tuple[str, str]] = None
        self._binding_feedback_serial = 0

        # P1/T11/T13: shared, observable selection model. The Keyframes
        # timeline remains the source of truth for the per-point 'selected'
        # flags it has always painted; this AnimationSelectionModel mirrors
        # them so the Spline panel (and future Graph Editor) read/write the
        # SAME selection without each view keeping a private copy. Synced
        # from the model on every rebuild (set_node_graph) and after every
        # in-panel selection gesture.
        self._selection_model = AnimationSelectionModel()
        # AUD-10102: memoized Value-column strings keyed by
        # (node_id, param_name, frame). During playback the tree refresh calls
        # the value provider for every visible row on every frame; without the
        # memo each call re-deserialized the node's whole keyframe track.
        # Invalidated on every graph rebuild (set_node_graph) and on
        # in-panel parameter writes.
        self._display_value_cache: Dict[Tuple[str, str, int], Optional[str]] = {}
        # Live NodeGraph reference (None until set_node_graph). Used by the
        # expression-status provider installed on the tree view (T49 host).
        self._node_graph: Optional[object] = None
        # AE/Nuke hybrid layer projection is optional so the panel remains
        # usable as a standalone node timeline in older hosts.
        self._layer_stack_controller: Optional[object] = None
        self._layer_view_mode: str = KeyframesModel.VIEW_MODE_NODES
        self._selected_layer_id: Optional[str] = None
        # Expression policy (P0 §5.3 decision). Defaults preserve A-15; the
        # host can flip this once node-side coexistence storage lands.
        self._expression_policy = ExpressionPolicy()

        # T06 "AE-habits" toggle. When True (default), plain J/K navigate
        # between keyframes and Ctrl/Cmd+K adds one — matching AE. When
        # False, plain K reverts to the legacy "add keyframe" behaviour.
        self._ae_nav_enabled: bool = True

        # T27/T46: snap toggle. When True the timeline snaps dragged
        # keyframes to integer frames, neighbouring keyframes and the work-
        # area edges. Off by default so the legacy drag-to-any-frame
        # behaviour is preserved until T46 wires the snap math into the view.
        self._snap_enabled: bool = False
        # T100: Graph Editor / Timeline mode. 'timeline' (default) shows the
        # existing ruler+tracks canvas; 'graph' swaps the right side to the
        # Graph Editor. The swap lives in GraphCanvasHost (T101) and never
        # tears down the shared tree / selection / current frame.
        self._view_mode: str = "timeline"
        # The panel is also used standalone in tests/older hosts. In the real
        # Composite page this is synchronized immediately by
        # CompositePage.set_composition_active().
        self._composition_active: bool = True
        # Expression editing is an explicit secondary surface. Keep it
        # collapsed until the user asks to edit an expression.
        self._expression_editor_expanded: bool = False
        self._expression_editor_height: int = 120
        self._empty_state_overlay: Optional[QWidget] = None
        self._empty_state_create_button: Optional[QPushButton] = None
        self._toolbar: Optional[QWidget] = None
        self._footer: Optional[QWidget] = None
        self._shortcut_enabled = True
        self._shortcut_dispatcher = timeline_shortcuts.ShortcutDispatcher()
        self._shortcut_filter_widgets = []

        self._init_ui()
        # Standalone/legacy hosts may not push a timeline state immediately.
        # Still show the same finite default composition work area as the
        # fully bound Composite page.
        self._timeline_view.set_composition_range(
            self._state.start_frame, self._state.end_frame)
        self._play_in = self._state.start_frame
        self._play_out = self._state.end_frame
        self._timeline_view.set_play_range(self._play_in, self._play_out)
        self._graph_view.set_play_range((self._play_in, self._play_out))
        self._update_range_label()
        self.setStyleSheet(COMPOSITE_KEYFRAMES_PANEL_STYLE)

        self._install_shortcut_event_filters()
        self._rebuild_shortcuts()

    # ── keyboard shortcuts (P4 / T06 AE-habits toggle) ──

    def _rebuild_shortcuts(self) -> None:
        """(Re)bind the canonical AE shortcut registry to panel actions."""
        # Keep the former QShortcut attributes as a compatibility surface for
        # older hosts/plugins that inspected them directly. The dispatcher
        # below remains authoritative; these shortcuts mirror only the small
        # set of legacy bindings and are disabled before being replaced.
        compat_attrs = (
            "_shortcut_add_kf", "_shortcut_nav_prev", "_shortcut_nav_next",
            "_shortcut_add_kf_mod", "_shortcut_set_in", "_shortcut_set_out",
            "_shortcut_toggle_graph",
        )
        for attr in compat_attrs:
            old = getattr(self, attr, None)
            if old is not None:
                old.setEnabled(False)
                old.deleteLater()
            setattr(self, attr, None)

        def add_compat_shortcut(attr: str, sequence: str,
                                handler: Callable[[], None]) -> None:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(handler)
            setattr(self, attr, shortcut)

        if self._ae_nav_enabled:
            add_compat_shortcut("_shortcut_nav_prev", "J",
                                self._goto_prev_keyframe)
            add_compat_shortcut("_shortcut_nav_next", "K",
                                self._goto_next_keyframe)
        else:
            add_compat_shortcut("_shortcut_add_kf", "K",
                                self.add_keyframe_at_playhead)
        add_compat_shortcut("_shortcut_add_kf_mod", "Ctrl+K",
                            self.add_keyframe_at_playhead)
        add_compat_shortcut("_shortcut_set_in", "I",
                            self.set_play_in_at_playhead)
        add_compat_shortcut("_shortcut_set_out", "O",
                            self.set_play_out_at_playhead)
        add_compat_shortcut("_shortcut_toggle_graph", "Shift+F3",
                            self.toggle_view_mode)

        dispatcher = self._shortcut_dispatcher
        dispatcher.registry = timeline_shortcuts.ShortcutRegistry()
        if not self._ae_nav_enabled:
            dispatcher.registry.disable_ae_nav_conflicts()
        dispatcher.handlers.clear()

        ts = timeline_shortcuts
        bindings = {
            ts.CMD_NAV_PREV_KEYFRAME: self._goto_prev_keyframe,
            ts.CMD_NAV_NEXT_KEYFRAME: self._goto_next_keyframe,
            ts.CMD_NAV_FIRST_FRAME: self._goto_first_frame,
            ts.CMD_NAV_LAST_FRAME: self._goto_last_frame,
            ts.CMD_NAV_STEP_BACK: self._step_frame_backward,
            ts.CMD_NAV_STEP_FWD: self._step_frame_forward,
            ts.CMD_NAV_STEP_BACK_BIG: lambda: self.seek_to_frame(
                self._state.current_frame - 10),
            ts.CMD_NAV_STEP_FWD_BIG: lambda: self.seek_to_frame(
                self._state.current_frame + 10),
            ts.CMD_PLAY_TOGGLE: self._toggle_play_from_shortcut,
            ts.CMD_ADD_KEYFRAME: self.add_keyframe_at_playhead,
            ts.CMD_REMOVE_KEYFRAME: self.remove_keyframe_at_playhead,
            ts.CMD_SET_WORK_IN: self.set_play_in_at_playhead,
            ts.CMD_SET_WORK_OUT: self.set_play_out_at_playhead,
            ts.CMD_FOCUS_POSITION: lambda: self._focus_property("position"),
            ts.CMD_FOCUS_SCALE: lambda: self._focus_property("scale"),
            ts.CMD_FOCUS_ROTATION: lambda: self._focus_property("rotation"),
            ts.CMD_FOCUS_ANCHOR: lambda: self._focus_property("anchor"),
            ts.CMD_FOCUS_OPACITY: lambda: self._focus_property("opacity"),
            ts.CMD_FILTER_ANIMATED: lambda: self._set_filter_mode("animated"),
            ts.CMD_FILTER_MODIFIED: lambda: self._set_filter_mode("modified"),
            ts.CMD_FILTER_EXPRESSION: lambda: self._set_filter_mode("expression"),
            ts.CMD_FILTER_ALL: lambda: self._set_filter_mode("all"),
            ts.CMD_SELECT_ALL: self._timeline_view.select_all_keyframes,
            ts.CMD_COPY: self._timeline_view.copy_selected_keyframes,
            ts.CMD_PASTE: self._timeline_view.paste_keyframes,
            ts.CMD_DELETE: self._timeline_view._delete_selected_keyframes,
            ts.CMD_UNDO: self.undo_requested.emit,
            ts.CMD_REDO: self.redo_requested.emit,
            ts.CMD_FIT_SELECTION: self.fit_selection,
            ts.CMD_FIT_ALL: self.fit_time_range,
            ts.CMD_ZOOM_IN: self._zoom_in,
            ts.CMD_ZOOM_OUT: self._zoom_out,
            ts.CMD_CENTER_PLAYHEAD: self._pan_to_playhead,
            ts.CMD_TOGGLE_SNAP: self._toggle_snap_from_shortcut,
            ts.CMD_EDIT_VALUE: self._edit_selected_property,
            ts.CMD_CANCEL: self._cancel_timeline_edit,
            ts.CMD_TOGGLE_GRAPH_EDITOR: self.toggle_view_mode,
        }
        for command, handler in bindings.items():
            dispatcher.bind(command, handler)

    def _install_shortcut_event_filters(self) -> None:
        """Capture timeline keys while leaving text editors to Qt."""
        widgets = [self, self._timeline_view, self._tree_view,
                   self._graph_view]
        for widget in widgets:
            if widget is None or widget in self._shortcut_filter_widgets:
                continue
            widget.installEventFilter(self)
            self._shortcut_filter_widgets.append(widget)

    @staticmethod
    def _shortcut_sequence(event) -> str:
        """Convert a Qt key event into the registry's portable sequence."""
        key = event.key()
        key_value = getattr(key, "value", key)
        key_names = {
            Qt.Key.Key_Space: "Space", Qt.Key.Key_Left: "Left",
            Qt.Key.Key_Right: "Right", Qt.Key.Key_Home: "Home",
            Qt.Key.Key_End: "End", Qt.Key.Key_Delete: "Delete",
            Qt.Key.Key_Backspace: "Backspace", Qt.Key.Key_Return: "Enter",
            Qt.Key.Key_Enter: "Enter", Qt.Key.Key_Escape: "Esc",
            Qt.Key.Key_F3: "F3", Qt.Key.Key_Plus: "+",
            Qt.Key.Key_Equal: "=", Qt.Key.Key_Minus: "-",
        }
        name = key_names.get(key)
        if name is None:
            text = str(event.text() or "")
            if len(text) == 1 and text.isalpha():
                name = text.upper()
            elif isinstance(key_value, int) and 65 <= key_value <= 90:
                name = chr(key_value)
            else:
                return ""

        modifiers = event.modifiers()
        prefix = []
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            prefix.append("Ctrl")
        elif modifiers & Qt.KeyboardModifier.MetaModifier:
            prefix.append("Cmd")
        if (modifiers & Qt.KeyboardModifier.ShiftModifier
                and name not in {"+", "="}):
            prefix.append("Shift")
        if modifiers & Qt.KeyboardModifier.AltModifier:
            prefix.append("Alt")
        return "+".join(prefix + [name])

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if (self._shortcut_enabled
                and event.type() == QEvent.Type.KeyPress
                and not event.isAutoRepeat()):
            sequence = self._shortcut_sequence(event)
            if sequence:
                command = self._shortcut_dispatcher.handle_key(
                    sequence,
                    now_ms=float(QDateTime.currentMSecsSinceEpoch()))
                if command is not None \
                        and command in self._shortcut_dispatcher.handlers:
                    return True
        return super().eventFilter(watched, event)

    def _toggle_play_from_shortcut(self) -> None:
        self._act_play.setChecked(not self._act_play.isChecked())

    def _toggle_snap_from_shortcut(self) -> None:
        self._act_snap.setChecked(not self._act_snap.isChecked())

    def _set_filter_mode(self, mode: str) -> None:
        index = self._filter_combo.findData(mode)
        if index >= 0 and self._filter_combo.currentIndex() != index:
            self._filter_combo.setCurrentIndex(index)

    def _focus_property(self, token: str) -> None:
        token = str(token).casefold()
        for row in self._model.visible_rows():
            if row.row_type != "param" or not row.param_name:
                continue
            if token not in row.param_name.casefold():
                continue
            self._on_param_selected(row.node_id, row.param_name)
            return

    def _edit_selected_property(self) -> None:
        if self._selected_param is not None:
            self._open_keyframe_editor_for_param(*self._selected_param)

    def _cancel_timeline_edit(self) -> None:
        self._timeline_view.clear_keyframe_selection()
        self._tree_view._close_keyframe_editor()

    def set_ae_nav_enabled(self, enabled: bool) -> None:
        """Toggle the AE navigation habits (T06 AE-habits switch).

        When enabled (default): J/K navigate, Ctrl/Cmd+K adds.
        When disabled: plain K reverts to the legacy add-keyframe behaviour.
        """
        if enabled == self._ae_nav_enabled:
            return
        self._ae_nav_enabled = bool(enabled)
        self._rebuild_shortcuts()

    @property
    def ae_nav_enabled(self) -> bool:
        return self._ae_nav_enabled

    # ── Graph Editor / Timeline mode (T100) ──

    @property
    def view_mode(self) -> str:
        """'timeline' (default) or 'graph' (Graph Editor)."""
        return self._view_mode

    def set_view_mode(self, mode: str) -> None:
        """Switch the right-hand canvas between Timeline and Graph Editor.

        Plan §22.1: the swap must NOT reset current frame, work area,
        left-tree expansion, filter, or keyframe selection. All of that
        state lives on the panel / shared model, which the GraphCanvasHost
        never touches — so this method is a pure stack-index change.
        """
        if mode not in ("timeline", "graph"):
            return
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._canvas_host.set_view_mode(mode)
        # Refresh the graph series when ENTERING graph mode so the user
        # sees the current param's curve immediately (not a stale/empty
        # view from the last time they toggled).
        if mode == "graph":
            self._refresh_graph_editor()
        # T110: show the Graph Type combo only in graph mode.
        self._set_graph_toolbar_controls_visible(mode == "graph")
        if mode == "graph":
            self._update_graph_info()
        # T120: the Graph Expression Editor shows only in graph mode; bind it
        # to the current param selection on entry.
        if hasattr(self, "_expr_editor"):
            self._set_expression_editor_visible(
                mode == "graph" and self._expression_editor_expanded)
            if mode == "graph" and self._expression_editor_expanded:
                self._refresh_expression_editor()
        # Keep the toolbar toggle in sync (in case set_view_mode was called
        # programmatically — e.g. from the Shift+F3 shortcut).
        if hasattr(self, "_act_graph_editor"):
            self._act_graph_editor.setChecked(mode == "graph")

    @property
    def layer_view_mode(self) -> str:
        """Return ``nodes`` or ``layers`` for the left tree projection."""
        return self._layer_view_mode

    def set_layer_view_mode(self, mode: str) -> None:
        """Switch the left tree between Node and AE Layer projections."""
        if mode not in (KeyframesModel.VIEW_MODE_NODES,
                        KeyframesModel.VIEW_MODE_LAYERS):
            return
        if mode == self._layer_view_mode:
            return
        if (mode == KeyframesModel.VIEW_MODE_LAYERS
                and self._layer_stack_controller is not None
                and self._layer_stack_controller.main_stack() is None):
            # First entry for a legacy graph creates the non-destructive
            # external projection promised by the migration contract.
            self._layer_stack_controller.project_from_node_graph()
        self._layer_view_mode = mode
        self._model.set_view_mode(mode)
        self._sync_layer_mode_actions()
        self._rebuild_filtered_model()

    def set_layer_stack_controller(self, controller: Optional[object]) -> None:
        """Bind the optional LayerStackController used by Layers mode."""
        old = self._layer_stack_controller
        if old is not None and hasattr(old, "layer_stack_changed"):
            try:
                old.layer_stack_changed.disconnect(self._on_layer_stack_changed)
            except (TypeError, RuntimeError):
                pass
        self._layer_stack_controller = controller
        if controller is not None and hasattr(controller, "layer_stack_changed"):
            try:
                controller.layer_stack_changed.connect(self._on_layer_stack_changed)
            except (TypeError, RuntimeError):
                logger.warning("Layer stack change signal connect failed",
                               exc_info=True)
        self._rebuild_filtered_model()

    def set_media_drop_handler(self, handler) -> None:
        """Install the host-owned media drop callback on the whole panel."""
        self._tree_view.set_media_drop_handler(handler)
        self._timeline_view.set_media_drop_handler(
            lambda mime, y: self._handle_timeline_media_drop(handler, mime, y))

    def _handle_timeline_media_drop(self, handler, mime_data, y: float) -> bool:
        """Map a right-side timeline drop onto the shared Layer row grid."""
        if handler is None:
            return False
        row_index, before = self._tree_view._row_index_at_y(float(y))
        target_layer_id, insert_index = self._tree_view._resolve_drop_target(
            row_index, before)
        if insert_index < 0:
            return False
        try:
            return bool(handler(mime_data, target_layer_id, insert_index))
        except Exception:
            # AUD-10100: the handler is host-owned code executing arbitrary
            # graph mutations inside a Qt drop event — it must never propagate
            # into the event loop, but failures must be diagnosable instead of
            # silently turning every drop into "nothing happened".
            logger.warning("Timeline media drop handler failed",
                           exc_info=True)
            return False

    def _on_layer_stack_changed(self, _stack_id: str = "") -> None:
        if self._layer_view_mode == KeyframesModel.VIEW_MODE_LAYERS:
            self._rebuild_filtered_model()

    def toggle_view_mode(self) -> None:
        """Shift+F3 handler: flip between Timeline and Graph Editor."""
        self.set_view_mode("graph" if self._view_mode == "timeline"
                           else "timeline")

    def _refresh_graph_editor(self) -> None:
        """T103/T105 host glue: rebuild the Graph Editor's series from the
        current parameter selection.

        Called when graph mode is entered, when the node graph rebuilds,
        and when the param selection changes while in graph mode. Reads the
        selected param row (or the first animated param of the selected
        node, per plan §22.1) and builds a GraphSeries via build_graph_series.
        Empty selection → empty series → the graph view paints its empty
        state.
        """
        if self._node_graph is None:
            self._graph_view.set_series([])
            return
        from .graph_model import build_graph_series
        series = []
        # Try the selected param first; fall back to the selected node's
        # first animated param; fall back to nothing (empty state).
        sel = self._selection_model.selection
        target_node = None
        target_param = None
        if sel.primary_row_id:
            from .timeline_view_model import parse_row_id, ROW_PARAM
            parsed = parse_row_id(sel.primary_row_id)
            if parsed.get("row_type") == ROW_PARAM:
                target_node = self._node_graph.get_node(
                    parsed.get("node_id", ""))
                target_param = parsed.get("param_name")
        if target_node is None or target_param is None:
            # Fall back to the first row's node + first animated param.
            for row in self._model.rows:
                if row.row_type == "param":
                    target_node = self._node_graph.get_node(row.node_id)
                    target_param = row.param_name
                    break
        if target_node is not None and target_param is not None:
            color = getattr(target_node, "effective_header_color",
                            lambda: "#bdbdbd")
            try:
                node_color = color() if callable(color) else color
            except (AttributeError, TypeError, RuntimeError):
                logger.debug("node header color unavailable; graph series "
                             "uses default", exc_info=True)
                node_color = "#bdbdbd"
            s = build_graph_series(target_node, target_param,
                                   node_color=node_color)
            series.append(s)
        self._graph_view.set_series(series)

    def _on_graph_selection_changed(self, selection) -> None:
        """T113 back-sync: the Graph Editor's keyframe selection flows into
        the shared AnimationSelectionModel so the Keyframes timeline (and
        the Spline panel) reflect it.

        ``selection`` is a set of (row_id, frame) tuples. We push the
        row_ids into the shared model's rows set and the (row_id, frame)
        pairs into its keyframes set, then the model notifies subscribers.
        Also refreshes the T122 Graph Info summary label.
        """
        # T122: refresh the graph summary whatever the selection state.
        self._update_graph_info()
        try:
            pairs = list(selection)
        except TypeError:
            return
        if not pairs:
            return
        # The graph emits (row_id, frame); the shared model stores both.
        keyframe_pairs = []
        rows = set()
        for row_id, frame in pairs:
            keyframe_pairs.append((row_id, int(frame)))
            rows.add(row_id)
        # Coalesce: one notification for the whole batch.
        sm = self._selection_model
        sm.begin_batch()
        try:
            sm._selection.keyframes = set(keyframe_pairs)
            sm._selection.rows = rows
            if pairs:
                first_row, first_frame = pairs[0]
                sm._selection.primary_row_id = first_row
                sm._selection.primary_frame = int(first_frame)
        finally:
            sm.end_batch()

    def _update_graph_info(self) -> None:
        """T122: refresh the toolbar's Graph Info summary from the graph
        view's current selection. Shows property/frame/value/speed/
        interpolation for a single selected keyframe, or a count when many
        are selected. Only called in graph mode."""
        if not hasattr(self, "_graph_info_label"):
            return
        from .graph_model import summarize_selection
        summary = summarize_selection(self._graph_view._series,
                                      fps=self._state.fps)
        if summary["count"] == 0:
            self._graph_info_label.setText("")
            return
        if summary["count"] == 1:
            speed_str = (f"{summary['speed']:.2f}/s"
                         if summary["speed"] is not None else "—")
            text = (f"{summary['property']} · "
                    f"f{summary['frame']} · "
                    f"v{summary['value']:.3g} · "
                    f"{speed_str} · "
                    f"{summary['interpolation']}")
        else:
            text = f"{summary['property']} · {summary['count']} selected"
        self._graph_info_label.setText(text)

    def _refresh_expression_editor(self) -> None:
        """T120: bind the Graph Expression Editor to the primary selected
        param. With no param selected the editor shows its empty state."""
        if not hasattr(self, "_expr_editor"):
            return
        if self._selected_param is None:
            self._expr_editor.clear_target()
            return
        node_id, param_name = self._selected_param
        node = None
        if self._node_graph is not None:
            try:
                node = self._node_graph.get_node(node_id)
            except Exception:
                node = None
        display = param_name.replace("_", " ").title()
        expr = None
        if node is not None:
            expr = node.get_expression(param_name)
        expression_enabled = (
            node.is_expression_enabled(param_name)
            if node is not None and expr else True)
        # Multi-select count comes from the shared selection model.
        multi = 1
        sm = getattr(self, "_selection_model", None)
        if sm is not None:
            try:
                multi = max(1, len(getattr(sm._selection, "rows", set())))
            except Exception:
                multi = 1
        self._expr_editor.set_target(node_id, param_name, display,
                                     expression=expr,
                                     multi_select_count=multi,
                                     expression_enabled=expression_enabled)

    def _on_expression_committed(self, node_id: str, param_name: str,
                                 text: str) -> None:
        """T120: the user edited the expression field. Forward the raw text
        to CompositePage, which owns the authoritative NodeGraph write +
        undo record (mirroring the properties-panel ◆ path). The page reports
        validation failure back via ``_on_graph_expression_failed`` so the
        editor keeps the user's WIP text. This keeps the Graph Editor and the
        properties panel on ONE consistent expression/undo path."""
        # No graph yet (panel not bound) — nothing to do; the text stays in
        # the field for the user.
        if self._node_graph is None:
            return
        self.graph_expression_changed.emit(node_id, param_name, text)

    def _on_expression_enabled_changed(self, node_id: str,
                                       param_name: str,
                                       enabled: bool) -> None:
        """Forward the expression switch to the CompositePage owner."""
        if self._node_graph is None:
            return
        self.graph_expression_enabled_changed.emit(
            node_id, param_name, bool(enabled))

    def _on_graph_expression_failed(self, node_id: str, param_name: str,
                                    error: Optional[str]) -> None:
        """Callback from CompositePage when a Graph expression commit failed
        validation. Shows the error in the editor without discarding WIP."""
        if not hasattr(self, "_expr_editor"):
            return
        if error:
            self._expr_editor.show_error(
                I18N.tr("keyframes.expression_invalid", "Invalid expression"))
        else:
            self._expr_editor.show_error(None)
        # The post curve may still reflect the old (now-rejected) expression —
        # refresh so the editor and the canvas agree.
        self._refresh_graph_editor()

    # ── layout ──

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._toolbar = self._build_toolbar()
        root.addWidget(self._toolbar)

        # Main area: left tree | right editor host, sharing vertical scroll.
        self._splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self._splitter.setHandleWidth(1)
        self._splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {C.PANEL_DIVIDER}; }}")
        self._splitter_default_applied = False
        self._splitter_user_moved = False
        self._splitter_applying_defaults = False
        self._splitter.splitterMoved.connect(self._on_splitter_moved)

        # The right side owns both the timeline/graph canvas and the optional
        # expression editor. Keeping this vertical splitter inside the right
        # horizontal pane prevents the editor from taking any layer-column
        # space, while still allowing the user to resize it.
        self._right_splitter = QSplitter(Qt.Orientation.Vertical, self._splitter)
        self._right_splitter.setObjectName("KeyframesRightSplitter")
        self._right_splitter.setHandleWidth(3)
        self._right_splitter.setChildrenCollapsible(True)
        self._right_splitter.setStyleSheet(
            f"QSplitter::handle {{ background-color: {C.PANEL_DIVIDER}; }}")
        self._right_splitter.splitterMoved.connect(
            self._on_right_splitter_moved)

        self._tree_view = KeyframesTreeView(
            self._model, parent=self._splitter,
            header_height=KEYFRAMES_RULER_H)
        self._tree_view.setObjectName("KeyframesTreeSide")
        # The current-time readout belongs with the property header rather
        # than a separate footer: it replaces the low-value Name/Value
        # captions and stays visible beside the playhead ruler.
        self._time_display = TimelineTimeDisplay(
            self._tree_view, fps=self._state.fps, compact=True)
        self._time_display.set_display_mode(
            "timecode" if self._state.display_mode == "time"
            else self._state.display_mode)
        self._time_display.set_current_frame(self._state.current_frame)
        self._time_display.frame_committed.connect(self.seek_to_frame)
        self._time_display.display_mode_requested.connect(
            self._on_time_display_mode_requested)
        self._time_display.set_menu_display_mode(self._state.display_mode)
        self._tree_view.set_header_time_display(self._time_display)
        # Keep the range label as a hidden compatibility readout. Frames and
        # In/Out are useful to the work-area logic but too noisy for the
        # compact header, so they are no longer painted in the UI.
        self._range_label = QLabel(self)
        self._range_label.hide()
        self._build_time_unit_compatibility_control()
        # Keep the old private label as a non-layout compatibility readout;
        # all visible range information now lives in the property header.
        self._frame_label = QLabel(self)
        self._frame_label.hide()
        # T49 host: install the expression-status provider so the tree paints
        # the 4-state marker from the live graph. The provider reads the
        # panel's current _node_graph (None until set_node_graph → no marker).
        self._tree_view.set_expression_status_provider(self._expression_status_for)
        self._tree_view.set_value_provider(self._display_value_for)
        self._tree_view.set_value_control_provider(
            self._value_control_metadata)
        self._tree_view.set_keyframe_status_provider(
            self._keyframe_status_for)
        self._tree_view.set_binding_provider(self._binding_status_for)
        self._tree_view.set_parent_binding_provider(
            self._parent_binding_supported)
        self._timeline_view = KeyframesTimelineView(
            self._model, self._frame_mapper, parent=self._splitter)

        # T101: wrap the timeline canvas in a GraphCanvasHost so the right
        # side can swap to Graph Editor mode (T100) without rebuilding the
        # splitter / tree / shared state. ``_timeline_view`` stays a direct
        # attribute so the existing 200+ tests that reference it keep working.
        self._canvas_host = GraphCanvasHost(self._timeline_view,
                                            parent=self._splitter)

        # T105/T109: the Graph Editor canvas. Installed into the host so
        # toggling view_mode='graph' swaps to it. The view starts empty
        # (no series); _refresh_graph_editor() populates it from the
        # current param selection when graph mode is entered.
        from .graph_editor_view import GraphEditorView
        self._graph_view = GraphEditorView(parent=self._canvas_host,
                                           fps=self._state.fps)
        self._graph_view.set_frame_range(self._state.start_frame,
                                         self._state.end_frame)
        # T113: when the graph view's keyframe selection changes, push it
        # into the shared AnimationSelectionModel so the Keyframes timeline
        # and the Spline panel reflect it.
        self._graph_view.selection_changed.connect(
            self._on_graph_selection_changed)
        self._canvas_host.set_graph_view(self._graph_view)

        # T120: Graph Expression Editor — a resizable bottom panel that edits
        # the PRIMARY selected param's expression. Hidden in timeline mode;
        # shown under the canvas in graph mode. The panel (canonical owner)
        # validates the committed text via node.set_expression.
        from .graph_expression_editor import GraphExpressionEditor
        self._expr_editor = GraphExpressionEditor(parent=self)
        self._expr_editor.expression_committed.connect(
            self._on_expression_committed)
        if hasattr(self._expr_editor, "expression_enabled_changed"):
            self._expr_editor.expression_enabled_changed.connect(
                self._on_expression_enabled_changed)
        if hasattr(self._expr_editor, "collapse_requested"):
            self._expr_editor.collapse_requested.connect(
                lambda: self.set_expression_editor_expanded(False))
        self._expr_editor.setVisible(False)

        # Keep the two sides vertically in sync. Connect to the host (not the
        # raw timeline view) so a future Graph canvas can share the scroll.
        self._tree_view.vertical_scroll_changed.connect(
            self._timeline_view.set_vertical_scroll)
        self._timeline_view.vertical_scroll_changed.connect(
            self._tree_view.set_vertical_scroll)
        # P9: tree node selection forwards to CompositePage.
        self._tree_view.node_selected.connect(self._on_node_selected)
        self._tree_view.layer_selected.connect(self._on_layer_selected)
        self._tree_view.layer_delete_requested.connect(
            self.layer_delete_requested.emit)
        self._tree_view.promote_node_requested.connect(
            self.promote_node_requested)
        self._tree_view.create_matte_layer_requested.connect(
            self.create_matte_layer_requested)
        self._tree_view.attach_node_requested.connect(
            self.attach_node_requested)
        self._tree_view.lock_changed.connect(self._on_layer_lock_changed)
        self._tree_view.eye_changed.connect(self._on_layer_eye_changed)
        self._tree_view.solo_changed.connect(self._on_layer_solo_changed)
        self._tree_view.layer_reorder_requested.connect(
            self._on_layer_reorder_requested)
        self._tree_view.expression_edit_requested.connect(
            self._open_expression_for_param)
        # Param row selection decides the target of add/remove-keyframe.
        self._tree_view.param_selected.connect(self._on_param_selected)
        self._tree_view.keyframe_toggle_requested.connect(
            self._on_property_keyframe_toggle)
        self._tree_view.keyframe_edit_requested.connect(
            self._open_keyframe_editor_for_param)
        self._tree_view.keyframe_edit_commit_requested.connect(
            self._on_keyframe_editor_commit)
        self._tree_view.keyframe_edit_remove_requested.connect(
            self._on_keyframe_editor_remove)
        self._tree_view.binding_requested.connect(
            self._open_binding_menu)
        self._tree_view.value_changed_requested.connect(
            self._on_property_value_changed)
        self._tree_view.value_drag_started.connect(
            self._on_property_value_drag_started)
        self._tree_view.value_dragged.connect(
            self._on_property_value_dragged)
        self._tree_view.value_drag_finished.connect(
            self._on_property_value_drag_finished)
        self._tree_view.value_control_changed.connect(
            self._on_property_value_control_changed)
        self._tree_view.parameter_binding_dragged.connect(
            self._on_parameter_binding_dragged)
        self._tree_view.parent_binding_dragged.connect(
            self._on_parent_binding_dragged)
        self._tree_view.expand_changed.connect(self._on_expand_changed)
        # Playhead seeks from the timeline side.
        self._timeline_view.current_frame_changed.connect(
            self._on_playhead_moved)
        self._timeline_view.selection_changed.connect(
            self._on_timeline_selection_changed)
        self._timeline_view.expression_edit_requested.connect(
            self._open_expression_for_param)
        self._timeline_view.track_changed.connect(
            self._on_timeline_track_changed)
        self._timeline_view.track_batch_about_to_commit.connect(
            self.track_batch_about_to_commit)
        self._timeline_view.track_batch_committed.connect(
            self.track_batch_committed)
        # T114/T116: graph editor keyframe/handle drag commits route through
        # the same track_changed path as timeline edits (same node write +
        # undo + re-render in CompositePage).
        self._graph_view.keyframe_changed.connect(
            self._on_timeline_track_changed)
        # T123: forward the drag transaction signals so CompositePage can
        # wrap a multi-series drag in ONE undo group.
        self._graph_view.drag_about_to_commit.connect(
            self.graph_drag_about_to_commit)
        self._graph_view.drag_committed.connect(
            self.graph_drag_committed)
        # P5: range bar edits forward to clients. Keep the transient gesture
        # kind on a separate signal so the public payload remains the
        # persisted time_range schema used by older integrations.
        self._timeline_view.range_changed.connect(
            self._on_timeline_range_changed)
        # Work-area (play range) band edits.
        self._timeline_view.play_range_changed.connect(
            self._on_timeline_play_range_changed)
        # Ruler label mode switched via the view's context menu.
        self._timeline_view.display_mode_changed.connect(
            self._on_view_display_mode_changed)

        self._splitter.addWidget(self._tree_view)
        self._right_splitter.addWidget(self._canvas_host)
        self._right_splitter.addWidget(self._expr_editor)
        self._right_splitter.setStretchFactor(0, 1)
        self._right_splitter.setStretchFactor(1, 0)
        self._right_splitter.setCollapsible(0, False)
        self._right_splitter.setCollapsible(1, True)
        self._right_splitter.setSizes([1, 0])
        self._splitter.addWidget(self._right_splitter)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        # Apply the AE-like 43/57 split after the dock has a real width. The
        # previous hard-coded [232, 800] left the layer/property column only
        # 232 px wide even on a 1242 px panel, forcing the old fixed-x label
        # painter to collide with the switch strip.
        self._splitter.setSizes([300, 830])
        self._tree_view.setStyleSheet(COMPOSITE_KEYFRAMES_TREE_STYLE)
        root.addWidget(self._splitter, stretch=1)
        self._set_graph_toolbar_controls_visible(self._view_mode == "graph")

        # The former footer consumed a permanent 38 px strip. The time
        # display now owns its unit menu, so the editor can use the full
        # remaining height.
        self._footer = None

        self._build_empty_state_overlay()
        self.set_composition_active(self._composition_active)

    def _build_empty_state_overlay(self) -> None:
        """Create the shared-style empty state with an actionable CTA."""
        overlay = QWidget(self)
        overlay.setObjectName("KeyframesEmptyStateOverlay")
        overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        overlay.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        overlay.setStyleSheet(
            f"QWidget#KeyframesEmptyStateOverlay {{"
            " background-color: rgba(10, 11, 14, 178);"
            "}"
            f"QFrame#KeyframesEmptyStateCard {{"
            f" background-color: {C.BG_PANEL};"
            f" border: 1px solid {C.BORDER};"
            " border-radius: 5px;"
            "}"
            f"QLabel#KeyframesEmptyStateTitle {{"
            f" color: {C.TEXT_PRIMARY};"
            " font-size: 13px;"
            " font-weight: 600;"
            "}"
            f"QLabel#KeyframesEmptyStateHint {{"
            f" color: {C.TEXT_HINT};"
            " font-size: 11px;"
            "}"
            f"QPushButton#KeyframesEmptyStateCreateButton {{"
            " color: #ffffff;"
            f" background-color: {C.ORANGE};"
            f" border: 1px solid {C.ORANGE};"
            " border-radius: 3px;"
            " padding: 5px 14px;"
            " min-width: 132px;"
            " font-size: 11px;"
            "}"
            f"QPushButton#KeyframesEmptyStateCreateButton:hover {{"
            f" background-color: {C.ORANGE_HOVER};"
            "}"
            f"QPushButton#KeyframesEmptyStateCreateButton:pressed {{"
            f" background-color: {C.ORANGE_PRESS};"
            "}"
        )
        layout = QVBoxLayout(overlay)
        layout.setContentsMargins(20, 20, 20, 20)

        card = QFrame(overlay)
        card.setObjectName("KeyframesEmptyStateCard")
        card.setMaximumWidth(440)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(10)

        title = QLabel(
            I18N.tr("composite.empty_state.title", "No Comp Clip Open"),
            card,
        )
        title.setObjectName("KeyframesEmptyStateTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(title)

        hint = QLabel(
            I18N.tr(
                "composite.empty_state.hint",
                "Create or open a Comp Clip to begin editing.",
            ),
            card,
        )
        hint.setObjectName("KeyframesEmptyStateHint")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(hint)

        create_button = QPushButton(
            I18N.tr("composite.empty_state.create", "New Comp Clip"),
            card,
        )
        create_button.setObjectName("KeyframesEmptyStateCreateButton")
        create_button.setCursor(Qt.CursorShape.PointingHandCursor)
        create_button.clicked.connect(self.create_empty_comp_clip_requested.emit)
        card_layout.addWidget(create_button, alignment=Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        self._empty_state_overlay = overlay
        self._empty_state_create_button = create_button
        overlay.setGeometry(self.rect())
        overlay.raise_()

    def set_composition_active(self, active: bool) -> None:
        """Enable or disable all Keyframes interactions for the active graph."""
        was_active = self._composition_active
        self._composition_active = bool(active)
        if not self._composition_active:
            # The page owns an external transport when one is bound, but the
            # panel still owns its fallback timer. Stop both the timer and the
            # visual playing state whenever the composition is detached so an
            # old graph cannot keep receiving ticks after page reset/close.
            self._play_timer.stop()
            if self._playing:
                self.set_playing_state(False)
        if was_active and not self._composition_active:
            # Do not carry an old composition's custom I/O into the next
            # composition. The next active composition starts with its full
            # own range, just like AE.
            self._play_in = None
            self._play_out = None
            self._play_range_explicit = False
            self._timeline_view.set_play_range(None, None)
            self._graph_view.set_play_range(None)
        elif (not was_active and self._composition_active
              and self._play_in is None and self._play_out is None):
            comp_start, comp_end = self._effective_comp_range()
            self._play_in = int(comp_start)
            self._play_out = int(comp_end)
            self._timeline_view.set_play_range(self._play_in, self._play_out)
            self._graph_view.set_play_range((self._play_in, self._play_out))
            self._update_range_label()
        if not self._composition_active:
            # An empty Composite workspace always returns to the stable
            # Timeline surface and collapses the expression editor. This also
            # prevents a previous graph-mode session from leaking into the
            # next empty-page entry.
            self.set_view_mode("timeline")
            self.set_expression_editor_expanded(False)
        # Keep the top-level panel enabled so the empty-state CTA remains
        # clickable. Disable only the actual editing surfaces underneath it.
        for widget in (self._toolbar, self._splitter, self._footer):
            if widget is not None:
                widget.setEnabled(self._composition_active)
        self._set_shortcuts_enabled(self._composition_active)
        if self._empty_state_overlay is not None:
            self._empty_state_overlay.setVisible(not self._composition_active)
            if not self._composition_active:
                self._empty_state_overlay.raise_()

    def has_active_composition(self) -> bool:
        """Return whether Keyframes is attached to an editable composition."""
        return self._composition_active

    def dispose(self) -> None:
        """Stop panel-owned playback and make later callbacks harmless.

        CompositePage calls this before releasing the dock during shutdown.
        Keeping this operation explicit is important because a QWidget's
        destruction order is not a reliable playback lifecycle boundary on
        macOS/Qt, especially when a standalone fallback QTimer is active.
        """
        if self._disposed:
            return
        self._disposed = True
        self._play_timer.stop()
        self._playing = False
        self._playback_controller = None
        self._value_provider = None
        self._set_shortcuts_enabled(False)
        try:
            self._act_play.blockSignals(True)
            self._act_play.setChecked(False)
            self._act_play.blockSignals(False)
            self._act_play.setIcon(make_qicon("play"))
        except (AttributeError, RuntimeError):
            # ``dispose`` may be called from a late Qt teardown path after a
            # child action has already been destroyed.
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        """Stop timers before Qt begins destroying the panel hierarchy."""
        self.dispose()
        super().closeEvent(event)

    def _set_shortcuts_enabled(self, enabled: bool) -> None:
        """Enable/disable panel shortcuts without disabling the CTA overlay."""
        self._shortcut_enabled = bool(enabled)
        for shortcut in self.findChildren(QShortcut):
            shortcut.setEnabled(self._shortcut_enabled)

    def _build_toolbar(self) -> QWidget:
        bar = QToolBar(self)
        bar.setObjectName("KeyframesToolbar")
        bar.setMovable(False)
        bar.setIconSize(QSize(_TOOLBAR_ICON_PX, _TOOLBAR_ICON_PX))
        bar.setStyleSheet(COMPOSITE_KEYFRAMES_TOOLBAR_STYLE +
                          COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)

        # ── Transport cluster: first / prev / play-pause / next / last ──
        self._act_first = QAction(
            make_qicon("fast_backward"),
            I18N.tr("keyframes.first_frame", "Go to start frame"), self)
        self._act_prev_frame = QAction(
            make_qicon("step_backward"),
            I18N.tr("keyframes.prev_frame", "Previous frame"), self)
        self._act_play = QAction(
            make_qicon("play"),
            I18N.tr("keyframes.play", "Play"), self)
        self._act_play.setCheckable(True)
        self._act_next_frame = QAction(
            make_qicon("step_forward"),
            I18N.tr("keyframes.next_frame", "Next frame"), self)
        self._act_last = QAction(
            make_qicon("fast_forward"),
            I18N.tr("keyframes.last_frame", "Go to end frame"), self)
        self._act_first.triggered.connect(self._goto_first_frame)
        self._act_prev_frame.triggered.connect(self._step_frame_backward)
        self._act_play.toggled.connect(self._on_play_toggled)
        self._act_next_frame.triggered.connect(self._step_frame_forward)
        self._act_last.triggered.connect(self._goto_last_frame)
        # CP-058: 播放键右键 → 变速（0.25x-4x）/ 往复播放
        self._act_play.setToolTip(
            I18N.tr("keyframes.play", "Play")
            + "（右键：变速/往复 / right-click: speed & ping-pong）")
        play_btn_ref = self

        def _show_play_menu() -> None:
            from PyQt6.QtWidgets import QMenu
            menu = QMenu(play_btn_ref)
            speed_menu = menu.addMenu("播放速度")
            for label, val in (("0.25\u00d7", 0.25), ("0.5\u00d7", 0.5),
                               ("1\u00d7", 1.0), ("2\u00d7", 2.0),
                               ("4\u00d7", 4.0)):
                act = speed_menu.addAction(label)
                act.setCheckable(True)
                act.setChecked(
                    abs(self._playback_speed - val) < 1e-3)
                act.triggered.connect(
                    lambda _c=False, v=val: self.set_playback_speed(v))
            pp_act = menu.addAction(I18N.tr("keyframes.playback_pingpong", "往复播放 (Ping-Pong)"))
            pp_act.setCheckable(True)
            pp_act.setChecked(self._play_pingpong)
            pp_act.triggered.connect(
                lambda checked=False: self.set_play_pingpong(bool(checked)))
            btn = self._toolbar.widgetForAction(self._act_play)
            menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))
        for act in (self._act_first, self._act_prev_frame, self._act_play,
                    self._act_next_frame, self._act_last):
            bar.addAction(act)

        bar.addSeparator()

        # Previous / next keyframe navigation
        act_prev = QAction(make_qicon("step_backward"),
                            I18N.tr("keyframes.prev_kf", "Previous keyframe"),
                            self)
        act_next = QAction(make_qicon("step_forward"),
                            I18N.tr("keyframes.next_kf", "Next keyframe"),
                            self)
        act_prev.triggered.connect(self._goto_prev_keyframe)
        act_next.triggered.connect(self._goto_next_keyframe)
        bar.addAction(act_prev)
        bar.addAction(act_next)

        bar.addSeparator()

        # ── Keyframe editing at the playhead (T27: diamond imagery) ──
        # Plan §3.2.B: keyframe buttons use diamond glyphs, not plain +/-,
        # so the user doesn't mistake them for "add node/param". The AE
        # icon painter renders diamond_plus/diamond_minus (batch 12); these
        # fall back to icon_manager glyphs only if the painter is missing,
        # which it never is in practice.
        self._act_add_kf = QAction(
            make_qicon("diamond_plus"),
            I18N.tr("keyframes.add_kf",
                    "Add keyframe at current frame (Ctrl/Cmd+K)"), self)
        self._act_del_kf = QAction(
            make_qicon("diamond_minus"),
            I18N.tr("keyframes.del_kf",
                    "Remove keyframe at current frame"), self)
        self._act_add_kf.triggered.connect(self.add_keyframe_at_playhead)
        self._act_del_kf.triggered.connect(self.remove_keyframe_at_playhead)
        bar.addAction(self._act_add_kf)
        bar.addAction(self._act_del_kf)

        bar.addSeparator()

        # ── View + zoom group (plan §3.2.D) ──
        # Fit / Fit-selection / Zoom out / Zoom in / Center playhead / Snap.
        # All six are required by the spec; previously only Fit + Zoom-in
        # were wired. Snap is a checkable toggle whose state the timeline
        # view reads during keyframe drag (T46 lands the snap math; this
        # button is the UI precondition).
        act_fit = QAction(make_qicon("fit"),
                          I18N.tr("keyframes.fit",
                                  "Fit entire composition to view"), self)
        act_fit.triggered.connect(self.fit_time_range)
        bar.addAction(act_fit)

        self._act_fit_selection = QAction(
            make_qicon("fit"),
            I18N.tr("keyframes.fit_selection",
                    "Fit selected keyframes to view"), self)
        self._act_fit_selection.triggered.connect(self.fit_selection)
        bar.addAction(self._act_fit_selection)

        act_zoom_out = QAction(
            make_qicon("zoom_out"),
            I18N.tr("keyframes.zoom_out", "Zoom out"), self)
        act_zoom_out.triggered.connect(self._zoom_out)
        bar.addAction(act_zoom_out)

        act_zoom_in = QAction(make_qicon("zoom_in"),
                              I18N.tr("keyframes.zoom_in", "Zoom in"), self)
        act_zoom_in.triggered.connect(self._zoom_in)
        bar.addAction(act_zoom_in)

        act_center = QAction(
            make_qicon("playhead"),
            I18N.tr("keyframes.center_playhead", "Center playhead"), self)
        act_center.triggered.connect(self._pan_to_playhead)
        bar.addAction(act_center)

        self._act_snap = QAction(
            make_qicon("snap"),
            I18N.tr("keyframes.snap",
                    "Snap to frames / keyframes / work-area edges"), self)
        self._act_snap.setCheckable(True)
        self._act_snap.setChecked(self._snap_enabled)
        self._act_snap.toggled.connect(self._on_snap_toggled)
        bar.addAction(self._act_snap)

        # T100: Graph Editor toggle. Checkable; when checked the right side
        # swaps from the Timeline canvas to the Graph Editor (plan §22.2).
        # Tooltip advertises Shift+F3 so the shortcut is discoverable.
        self._act_graph_editor = QAction(
            make_qicon("graph_editor"),
            I18N.tr("keyframes.graph_editor",
                    "Graph Editor (Shift+F3)"), self)
        self._act_graph_editor.setCheckable(True)
        self._act_graph_editor.setChecked(self._view_mode == "graph")
        self._act_graph_editor.toggled.connect(self._on_graph_editor_toggled)
        bar.addAction(self._act_graph_editor)

        # AE/Nuke hybrid tree projection. This is deliberately separate from
        # the Timeline/Graph canvas toggle: the right side can stay on the
        # same timeline while the left side changes its row authority.
        bar.addSeparator()
        layer_mode_group = QActionGroup(self)
        layer_mode_group.setExclusive(True)
        self._act_nodes_mode = QAction(
            make_qicon("nodes_mode"),
            I18N.tr("keyframes.nodes_mode", "Nodes"), self)
        self._act_layers_mode = QAction(
            make_qicon("layers_mode"),
            I18N.tr("keyframes.layers_mode", "Layers"), self)
        self._act_nodes_mode.setToolTip(
            I18N.tr("keyframes.nodes_mode_tooltip",
                    "Nodes view — show the compositing node graph"))
        self._act_layers_mode.setToolTip(
            I18N.tr("keyframes.layers_mode_tooltip",
                    "Layers view — show the AE-style layer stack"))
        for action, checked in ((self._act_nodes_mode, True),
                                (self._act_layers_mode, False)):
            action.setCheckable(True)
            action.setChecked(checked)
            layer_mode_group.addAction(action)
            bar.addAction(action)
        self._act_nodes_mode.triggered.connect(
            lambda: self.set_layer_view_mode(KeyframesModel.VIEW_MODE_NODES))
        self._act_layers_mode.triggered.connect(
            lambda: self.set_layer_view_mode(KeyframesModel.VIEW_MODE_LAYERS))

        # Expand / collapse all
        act_expand = QAction(
            make_qicon("expand"),
            I18N.tr("keyframes.expand_all", "Expand all"), self)
        act_expand.triggered.connect(lambda: self._expand_all(True))
        act_collapse = QAction(
            make_qicon("collapse"),
            I18N.tr("keyframes.collapse_all", "Collapse all"), self)
        act_collapse.triggered.connect(lambda: self._expand_all(False))
        bar.addAction(act_expand)
        bar.addAction(act_collapse)

        # AE-style property search + filter. Keep these controls on the
        # toolbar rather than adding a second header row; the editor area then
        # has one stable ruler and one stable property header.
        bar.addSeparator()
        self._search_edit = QLineEdit(bar)
        self._search_edit.setObjectName("KeyframesPropertySearch")
        self._search_edit.setPlaceholderText(
            I18N.tr("keyframes.search", "Search properties"))
        self._search_edit.setClearButtonEnabled(True)
        clear_button = self._search_edit.findChild(QToolButton)
        if clear_button is not None:
            clear_button.setAccessibleName(
                I18N.tr("keyframes.clear_search", "Clear property search"))
            clear_button.setToolTip(
                I18N.tr("keyframes.clear_search", "Clear property search"))
        self._search_edit.setFixedWidth(156)
        self._search_edit.setToolTip(
            I18N.tr("keyframes.search_tip",
                    "Filter layers and properties by name"))
        self._search_edit.textChanged.connect(self._on_search_changed)
        bar.addWidget(self._search_edit)

        self._filter_combo = QComboBox(bar)
        self._filter_combo.setObjectName("KeyframesPropertyFilter")
        self._filter_combo.addItem(
            I18N.tr("keyframes.filter_all", "All"), "all")
        self._filter_combo.addItem(
            I18N.tr("keyframes.filter_animated", "Animated"), "animated")
        self._filter_combo.addItem(
            I18N.tr("keyframes.filter_modified", "Modified"), "modified")
        self._filter_combo.addItem(
            I18N.tr("keyframes.filter_expression", "Expression"),
            "expression")
        self._filter_combo.addItem(
            I18N.tr("keyframes.filter_selected", "Selected"), "selected")
        self._filter_combo.setFixedWidth(112)
        self._filter_combo.setStyleSheet(COMPOSITE_KEYFRAMES_COMBO_STYLE)
        self._filter_combo.setToolTip(
            I18N.tr("keyframes.filter_tip",
                    "Show all / animated / modified / expression / selected"))
        self._filter_combo.currentIndexChanged.connect(self._on_filter_changed)
        bar.addWidget(self._filter_combo)

        # Graph-only controls stay in the toolbar after the footer is removed.
        self._graph_type_label = QLabel(
            I18N.tr("keyframes.graph_type", "Graph") + ":", bar)
        self._graph_type_label.setStyleSheet(
            f"color: {C.TEXT_HINT}; font-size: 11px;")
        self._graph_type_label_action = bar.addWidget(self._graph_type_label)
        self._graph_type_combo = QComboBox(bar)
        self._graph_type_combo.addItems([
            I18N.tr("keyframes.graph_type_auto", "Auto"),
            I18N.tr("keyframes.graph_type_value", "Value"),
            I18N.tr("keyframes.graph_type_speed", "Speed"),
        ])
        self._graph_type_combo.setStyleSheet(COMPOSITE_KEYFRAMES_COMBO_STYLE)
        self._graph_type_combo.currentIndexChanged.connect(
            self._on_graph_type_changed)
        self._graph_type_combo_action = bar.addWidget(self._graph_type_combo)
        self._graph_info_label = QLabel("", bar)
        self._graph_info_label.setStyleSheet(
            f"color: {C.TEXT_SECONDARY}; font-size: 11px;")
        self._graph_info_label_action = bar.addWidget(self._graph_info_label)
        self._binding_feedback_label = QLabel(bar)
        self._binding_feedback_label.setVisible(False)
        self._binding_feedback_label.setMinimumWidth(120)
        bar.addWidget(self._binding_feedback_label)
        self._graph_type_label.setVisible(False)
        self._graph_type_combo.setVisible(False)
        self._graph_info_label.setVisible(False)

        spacer = QWidget(bar)
        spacer.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)

        # Compact every tool button the toolbar created (QToolBar ignores
        # per-action sizing, so size the concrete QToolButton children).
        # T77: also give each icon-only button an accessible name + tooltip
        # (derived from its action's text) and make it keyboard-focusable.
        for btn in bar.findChildren(QToolButton):
            btn.setFixedSize(_TOOLBAR_BTN_W, _TOOLBAR_BTN_H)
            btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            act = btn.defaultAction()
            if act is not None:
                label = act.text().replace("&", "")
                if label:
                    if not btn.accessibleName().strip():
                        btn.setAccessibleName(label)
                    if not btn.toolTip().strip():
                        btn.setToolTip(label)
                    btn.setStatusTip(label)
        return bar

    def _set_graph_toolbar_controls_visible(self, visible: bool) -> None:
        """Keep graph-only toolbar widgets hidden in the normal timeline."""
        for name in ("_graph_type_label", "_graph_type_combo",
                     "_graph_info_label"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(bool(visible))
            action = getattr(self, f"{name}_action", None)
            if action is not None:
                action.setVisible(bool(visible))

    def _build_time_unit_compatibility_control(self) -> None:
        """Keep the old private combo API without rendering a footer widget."""
        self._time_unit_combo = QComboBox(self)
        self._time_unit_combo.addItems([
            I18N.tr("keyframes.unit_time", "Time"),
            I18N.tr("keyframes.unit_frame", "Frames"),
            I18N.tr("keyframes.unit_seconds", "Seconds"),
            I18N.tr("keyframes.unit_timecode", "Timecode"),
        ])
        self._time_unit_combo.setStyleSheet(COMPOSITE_KEYFRAMES_COMBO_STYLE)
        self._time_unit_combo.currentIndexChanged.connect(self._on_time_unit_changed)
        self._time_unit_combo.hide()

    def _icon_button(self, action: QAction) -> QToolButton:
        btn = QToolButton(self)
        btn.setDefaultAction(action)
        btn.setStyleSheet(COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)
        btn.setFixedSize(_TOOLBAR_BTN_W, _TOOLBAR_BTN_H)
        # T77 accessibility: icon-only buttons need an explicit accessible
        # name + tooltip so screen readers announce them and colour isn't
        # the only status cue (plan §accessibility). The action's text is
        # the human label; strip any leading '&' mnemonic markers.
        label = action.text().replace("&", "")
        if label:
            btn.setAccessibleName(label)
            btn.setToolTip(label)
            btn.setStatusTip(label)
        # Focus policy: make icon buttons keyboard-focusable (tab chain).
        btn.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return btn

    # ── public API ──

    def set_node_graph(self, node_graph, selected_node_id: Optional[str] = None,
                       selected_layer_id: Optional[str] = None) -> None:
        """Rebuild the model from the given NodeGraph and refresh both views."""
        # AUD-10102: a rebuild means node/track state may have changed under
        # the panel — drop the Value-column memo so freshly written
        # parameters are never shown from a stale cache.
        self._display_value_cache.clear()
        # Default layer bars span the actual composition range — push it
        # before rebuild. The ruler may be extended to expose longer source
        # media, but that must not enlarge ordinary node bars.
        self._model.set_comp_range(*self._effective_comp_range())
        self._node_graph = node_graph
        if selected_layer_id is not None:
            self._selected_layer_id = str(selected_layer_id)
        if (self._selected_layer_id
                and self._layer_view_mode == KeyframesModel.VIEW_MODE_LAYERS
                and self._layer_stack_controller is not None):
            # AUD-10100: narrowed to the concrete failure modes of the
            # layer-stack lookups; anything else is a real domain error that
            # must surface instead of silently clearing the selection.
            try:
                if self._selected_layer_id not in self._layer_stack_controller.layers_map():
                    self._selected_layer_id = None
                else:
                    active_layers_fn = getattr(
                        self._layer_stack_controller,
                        "active_layer_ids_for_output", None)
                    if callable(active_layers_fn):
                        active_ids = set(active_layers_fn())
                        if self._selected_layer_id not in active_ids:
                            self._selected_layer_id = None
            except (AttributeError, KeyError, TypeError, ValueError):
                logger.debug("Selected layer validation failed during "
                             "rebuild", exc_info=True)
        if (self._layer_view_mode == KeyframesModel.VIEW_MODE_LAYERS
                and self._layer_stack_controller is not None):
            self._model.rebuild_layers(
                self._layer_stack_controller,
                selected_layer_id=self._selected_layer_id)
        else:
            self._model.rebuild(node_graph, selected_node_id=selected_node_id)
        # Keep the param selection alive across rebuilds so the add-keyframe
        # button keeps working after every graph edit refresh.
        if self._selected_param is not None:
            self._apply_param_selection(self._selected_param, refresh=False)
        # T11/T13: mirror the rebuild's per-row/per-point selection into the
        # shared AnimationSelectionModel so the Spline panel (and future
        # Graph Editor) stay in sync without re-querying the legacy flags.
        self._selection_model.sync_from_model(self._model)
        self._tree_view.set_selected_layer_id(self._selected_layer_id)
        self._tree_view.refresh()
        self._timeline_view.refresh()
        self._clamp_vertical_scroll()
        # T105 host glue: refresh the graph editor so a graph-mode session
        # open during a graph edit picks up the new series. No-op paint
        # when in timeline mode (the view is hidden behind the stack).
        if self._view_mode == "graph":
            self._refresh_graph_editor()

    def _sync_layer_mode_actions(self) -> None:
        if hasattr(self, "_act_nodes_mode"):
            self._act_nodes_mode.setChecked(
                self._layer_view_mode == KeyframesModel.VIEW_MODE_NODES)
            self._act_layers_mode.setChecked(
                self._layer_view_mode == KeyframesModel.VIEW_MODE_LAYERS)

    def _display_value_for(self, node_id: str,
                           param_name: Optional[str]) -> Optional[str]:
        """Return the AE-style value shown in the left Value column.

        The provider is deliberately read-only. It samples the authoritative
        track at the current frame when one exists and otherwise falls back
        to the node's current parameter value. This removes the old row of
        meaningless dashes while keeping rendering/undo ownership in the
        graph model.

        AUD-10102: results are memoized per (node, param, frame). During
        playback the tree repaint asks for every visible row on every frame
        and the control sync asks again; without the memo each call
        re-deserialized the node's entire keyframe track dict.
        """
        if self._node_graph is None or not param_name:
            return None
        frame = int(self._state.current_frame)
        key = (str(node_id), str(param_name), frame)
        cached = self._display_value_cache.get(key, _DISPLAY_VALUE_MISS)
        if cached is not _DISPLAY_VALUE_MISS:
            return cached  # type: ignore[return-value]
        value = self._compute_display_value(node_id, param_name)
        if len(self._display_value_cache) >= _DISPLAY_VALUE_CACHE_MAX:
            self._display_value_cache.clear()
        self._display_value_cache[key] = value
        return value

    def _compute_display_value(self, node_id: str,
                               param_name: str) -> Optional[str]:
        """Sample and format the live value for one property (uncached)."""
        try:
            node = self._node_graph.get_node(node_id)
            if node is None:
                return None
            pdef = node.get_param_defs().get(param_name)
            if getattr(pdef, "ptype", "") == "color":
                from src.features.composite.nodes.constant_node import ConstantNode
                from src.shared.keyframe import ColorKeyframeTrack
                base = getattr(node, "parameters", {}).get(
                    param_name, getattr(pdef, "default", (0.0, 0.0, 0.0, 1.0)))
                track_data = getattr(node, "keyframe_tracks", {}).get(param_name)
                if isinstance(track_data, dict) and track_data.get("keyframes"):
                    base = ColorKeyframeTrack.from_dict(track_data).get_value(
                        int(self._state.current_frame), default=base)
                return ConstantNode.rgba_to_hex(*tuple(base)[:3])
            track_data = getattr(node, "keyframe_tracks", {}).get(param_name)
            value = None
            if isinstance(track_data, dict) and track_data.get("keyframes"):
                from src.shared.keyframe import keyframe_track_from_dict
                track = keyframe_track_from_dict({
                    "param_name": param_name,
                    "value_type": getattr(pdef, "ptype", "scalar"),
                    "keyframes": track_data.get("keyframes", []),
                    "min_val": track_data.get("min_val", 0.0),
                    "max_val": track_data.get("max_val", 1.0),
                    "pre_extrap": track_data.get("pre_extrap", "constant"),
                    "post_extrap": track_data.get("post_extrap", "constant"),
                })
                value = track.get_value(int(self._state.current_frame),
                                         default=getattr(node, "parameters", {})
                                         .get(param_name, 0.0))
            if value is None:
                value = getattr(node, "parameters", {}).get(param_name)
            if value is None:
                return None
            if isinstance(value, bool):
                return "On" if value else "Off"
            if isinstance(value, (list, tuple)):
                return "[" + ", ".join(f"{float(component):.3f}".rstrip("0").rstrip(".")
                                       for component in value) + "]"
            if isinstance(value, float):
                return f"{value:.3f}".rstrip("0").rstrip(".")
            return str(value)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            # AUD-10100: only data-shape failures fall back to the dash — a
            # real domain error now propagates to the caller's guard instead
            # of being swallowed here.
            logger.debug("Display value unavailable for %s/%s",
                         node_id, param_name, exc_info=True)
            return None

    @staticmethod
    def _is_color_param(node, param_name: Optional[str]) -> bool:
        if node is None or not param_name:
            return False
        try:
            return getattr(node.get_param_defs().get(param_name), "ptype", "") == "color"
        except (AttributeError, TypeError):
            return False

    def _color_value_at_frame(self, node, param_name: str, frame: int):
        from src.shared.keyframe import ColorKeyframeTrack
        default = getattr(node, "parameters", {}).get(
            param_name, (0.0, 0.0, 0.0, 1.0))
        data = getattr(node, "keyframe_tracks", {}).get(param_name)
        if isinstance(data, dict) and data.get("keyframes"):
            return ColorKeyframeTrack.from_dict(data).get_value(
                int(frame), default=default)
        return tuple(float(value) for value in default)

    def _on_timeline_track_changed(self, node_id: str, param_name: str,
                                   keyframes: list) -> None:
        """Normalize timeline marker payloads before they leave the panel."""
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if not self._is_color_param(node, param_name):
            self.track_changed.emit(node_id, param_name, keyframes)
            return
        existing = getattr(node, "keyframe_tracks", {}).get(param_name, {})
        existing_by_frame = {
            int(kf.get("frame", -1)): kf
            for kf in (existing.get("keyframes", []) if isinstance(existing, dict) else [])
            if isinstance(kf, dict)
        }
        normalized = []
        for keyframe in keyframes or []:
            if not isinstance(keyframe, dict):
                continue
            try:
                frame = int(keyframe.get("frame", 0))
            except (TypeError, ValueError):
                continue
            source = existing_by_frame.get(frame)
            value = source.get("value") if source else None
            if not isinstance(value, (list, tuple)) or len(value) != 4:
                value = self._color_value_at_frame(node, param_name, frame)
            item = dict(keyframe)
            item["value"] = [float(component) for component in value]
            normalized.append(item)
        self.track_changed.emit(node_id, param_name, normalized)

    def _emit_color_keyframes(self, node_id: str, param_name: str,
                              keyframes: list) -> None:
        self.track_changed.emit(node_id, param_name, keyframes)

    def _write_color_keyframe_at(self, node_id: str, param_name: str,
                                 frame: int, remove: bool = False) -> None:
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if not self._is_color_param(node, param_name):
            return
        data = getattr(node, "keyframe_tracks", {}).get(param_name, {})
        keyframes = [dict(kf) for kf in data.get("keyframes", [])] \
            if isinstance(data, dict) else []
        keyframes = [kf for kf in keyframes
                     if int(kf.get("frame", -1)) != int(frame)]
        if not remove:
            keyframes.append({
                "frame": int(frame),
                "value": list(self._color_value_at_frame(
                    node, param_name, int(frame))),
                "interpolation": "linear",
            })
            keyframes.sort(key=lambda kf: int(kf.get("frame", 0)))
        self._emit_color_keyframes(node_id, param_name, keyframes)

    def _value_control_metadata(self, node_id: str,
                                param_name: str) -> Optional[dict]:
        """Return the same numeric metadata used by Properties DragValue."""
        if self._node_graph is None:
            return None
        try:
            node = self._node_graph.get_node(node_id)
            if node is None:
                return None
            # A color is displayed as a swatch/hex value, not as a scalar
            # DragValue. Its diamond controls one atomic RGBA track.
            if self._is_color_param(node, param_name):
                return None
            pdef = node.get_param_defs().get(param_name)
            # Roto point channels are dynamic parameters generated from the
            # path JSON.  They intentionally do not live in the class-level
            # PARAM_DEFS, but the Properties/Keyframes editor should treat
            # them exactly like normalized float controls.
            if (pdef is None and getattr(node, "node_type", "") == "roto_shape"
                    and str(param_name).startswith("roto_point_")):
                pdef = None
                min_val, max_val, step, decimals, default = (
                    0.0, 1.0, 0.01, 3,
                    float(getattr(node, "parameters", {}).get(param_name, 0.0)
                          or 0.0))
            elif pdef is None or getattr(pdef, "ptype", "float") not in {
                    "float", "int"}:
                return None
            else:
                is_int = getattr(pdef, "ptype", "float") == "int"
                step = 1.0 if is_int else float(
                    getattr(pdef, "step", 0.01) or 0.01)
                decimals = 0 if is_int else int(
                    getattr(pdef, "decimals", 2) or 0)
                min_val = float(getattr(pdef, "min_val", 0.0))
                max_val = float(getattr(pdef, "max_val", 1.0))
                default = float(getattr(pdef, "default", 0.0) or 0.0)
            if step < 1.0 and decimals <= 0:
                text = f"{step:.10f}".rstrip("0")
                decimals = len(text.split(".", 1)[1]) if "." in text else 0
            displayed = self._display_value_for(node_id, param_name)
            try:
                value = float(displayed) if displayed is not None \
                    else default
            except (TypeError, ValueError):
                value = default
            return {
                "min_val": min_val,
                "max_val": max_val,
                "step": step,
                "decimals": decimals,
                "value": float(value),
            }
        except (TypeError, ValueError, AttributeError, KeyError):
            return None

    def _keyframe_status_for(self, node_id: str, param_name: str) -> dict:
        """Return the state used by the AE-style per-property diamond."""
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        data = getattr(node, "keyframe_tracks", {}).get(param_name) if node else None
        keyframes = data.get("keyframes", []) if isinstance(data, dict) else []
        frame = int(self._state.current_frame)
        return {
            "animated": bool(keyframes),
            "at_current": any(int(k.get("frame", -1)) == frame
                               for k in keyframes if isinstance(k, dict)),
        }

    def _binding_status_for(self, node_id: str,
                            param_name: Optional[str]) -> bool:
        if self._node_graph is None:
            return False
        if param_name:
            return bool(self._node_graph.get_parameter_binding(
                node_id, param_name))
        if not self._parent_binding_supported(node_id):
            return False
        return bool(self._node_graph.get_node_parent(node_id))

    def _parent_binding_supported(self, node_id: str) -> bool:
        """Whether this node participates in the 2D AE-style parent system."""
        if self._node_graph is None:
            return False
        node = self._node_graph.get_node(str(node_id))
        return bool(getattr(node, "supports_parent_transform", False))

    def _on_property_keyframe_toggle(self, node_id: str,
                                     param_name: str) -> None:
        """Toggle the current-frame keyframe for exactly one property."""
        status = self._keyframe_status_for(node_id, param_name)
        frame = int(self._state.current_frame)
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if self._is_color_param(node, param_name):
            self._write_color_keyframe_at(
                node_id, param_name, frame, remove=status["at_current"])
            return
        if status["at_current"]:
            self._timeline_view.remove_keyframe_at(node_id, param_name, frame)
        else:
            value = None
            value = self._value_provider(node_id, param_name) \
                if self._value_provider else None
            self._timeline_view.add_keyframe_at(
                node_id, param_name, frame, value_hint=value)

    def _on_property_value_changed(self, node_id: str, param_name: str,
                                   text: str) -> None:
        """Commit an inline Value-column edit with the node's declared type."""
        try:
            value = float(text)
        except (TypeError, ValueError):
            self._tree_view.refresh()
            return
        self._apply_property_numeric_value(node_id, param_name, value)

    def _on_property_value_control_changed(self, node_id: str,
                                            param_name: str,
                                            value: float) -> None:
        """Handle the shared Properties-style DragValue in the Value cell."""
        self._apply_property_numeric_value(node_id, param_name, value)

    def _apply_property_numeric_value(self, node_id: str, param_name: str,
                                      value: float) -> None:
        """Apply one absolute numeric value using the common AE semantics."""
        if self._node_graph is None:
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        old = node.parameters.get(param_name)
        pdef = node.get_param_defs().get(param_name)
        if pdef is not None:
            value = max(float(pdef.min_val), min(float(pdef.max_val), value))
            if pdef.ptype == "int":
                value = int(round(value))
            else:
                value = float(value)
        elif (getattr(node, "node_type", "") == "roto_shape"
              and str(param_name).startswith("roto_point_")):
            value = max(0.0, min(1.0, float(value)))
        frame = int(self._state.current_frame)
        if self._keyframe_status_for(node_id, param_name)["at_current"]:
            self._timeline_view.add_keyframe_at(
                node_id, param_name, frame, value_hint=float(value))
        elif old != value:
            self.parameter_changed.emit(node_id, param_name, value)
        # AUD-10102: the write may be applied without a full panel rebuild —
        # drop this property's memoized display values so the Value column
        # reflects the new value immediately.
        self._invalidate_display_values(node_id, param_name)
        self._tree_view.update()

    def _invalidate_display_values(self, node_id: Optional[str] = None,
                                   param_name: Optional[str] = None) -> None:
        """Drop memoized Value-column strings (all, or one property's)."""
        if node_id is None or param_name is None:
            self._display_value_cache.clear()
            return
        stale = [key for key in self._display_value_cache
                 if key[0] == node_id and key[1] == param_name]
        for key in stale:
            del self._display_value_cache[key]

    def _on_property_value_drag_started(self, node_id: str,
                                         param_name: str,
                                         base_value: object) -> None:
        self._value_drag_bases[(node_id, param_name)] = base_value
        self._property_value_drag_active = True
        self._property_value_drag_target = (node_id, param_name)
        self.property_value_drag_started.emit(node_id, param_name)

    def _on_property_value_dragged(self, node_id: str, param_name: str,
                                   delta_pixels: float) -> None:
        """Apply horizontal AE-style value scrubbing.

        One pixel equals one declared ``ParamDef.step``. This keeps Translate
        comfortable at 1 px, Scale at 0.01 and Rotation at 0.5 degrees while
        remaining deterministic for scripts and automated tests.
        """
        if self._node_graph is None:
            return
        node = self._node_graph.get_node(node_id)
        if node is None:
            return
        base = self._value_drag_bases.get((node_id, param_name))
        if base is None:
            return
        pdef = node.get_param_defs().get(param_name)
        ptype = getattr(pdef, "ptype", "float")

        # Non-numeric parameters are not keyframe tracks, but they can still
        # use the same compact horizontal scrub gesture: option values move
        # one label per 24 px and bool values switch after a short threshold.
        if ptype == "option":
            labels = list(getattr(pdef, "labels", None) or [])
            if not labels:
                return
            current = str(base)
            try:
                start_index = labels.index(current)
            except ValueError:
                start_index = 0
            index = max(0, min(len(labels) - 1,
                               start_index + int(round(delta_pixels / 24.0))))
            value = labels[index]
            if node.parameters.get(param_name) != value:
                self.parameter_changed.emit(node_id, param_name, value)
                self._invalidate_display_values(node_id, param_name)
            self._tree_view.update()
            return
        if ptype == "bool":
            current = str(base).strip().lower() in {"1", "true", "yes", "on"}
            value = current if abs(float(delta_pixels)) < 4.0 \
                else float(delta_pixels) > 0.0
            if node.parameters.get(param_name) != value:
                self.parameter_changed.emit(node_id, param_name, value)
                self._invalidate_display_values(node_id, param_name)
            self._tree_view.update()
            return

        try:
            base = float(base)
        except (TypeError, ValueError):
            return
        is_roto_point = bool(
            getattr(node, "node_type", "") == "roto_shape"
            and str(param_name).startswith("roto_point_"))
        step = 0.01 if is_roto_point else float(
            getattr(pdef, "step", 1.0) or 1.0)
        value = base + float(delta_pixels) * step
        if pdef is not None:
            value = max(float(pdef.min_val), min(float(pdef.max_val), value))
            if pdef.ptype == "int":
                value = int(round(value))
        elif is_roto_point:
            value = max(0.0, min(1.0, float(value)))
        frame = int(self._state.current_frame)
        if self._keyframe_status_for(node_id, param_name)["at_current"]:
            self._timeline_view.add_keyframe_at(
                node_id, param_name, frame, value_hint=float(value))
        else:
            self.parameter_changed.emit(node_id, param_name, value)
        self._invalidate_display_values(node_id, param_name)
        self._tree_view.update()

    def _on_property_value_drag_finished(self, node_id: str,
                                          param_name: str) -> None:
        self._value_drag_bases.pop((node_id, param_name), None)
        self._property_value_drag_active = False
        self._property_value_drag_target = None
        self.property_value_drag_finished.emit(node_id, param_name)

    @property
    def property_value_drag_active(self) -> bool:
        """Whether a Value-column scrub is currently being committed."""
        return self._property_value_drag_active

    def _on_parameter_binding_dragged(self, target_node_id: str,
                                      target_param_name: str,
                                      source_node_id: str,
                                      source_param_name: str) -> None:
        self._pending_expression_binding = (target_node_id, target_param_name)
        self.parameter_binding_changed.emit(
            target_node_id, target_param_name,
            source_node_id, source_param_name)

    def _on_parent_binding_dragged(self, child_node_id: str,
                                   parent_node_id: str) -> None:
        if (not self._parent_binding_supported(child_node_id)
                or not self._parent_binding_supported(parent_node_id)):
            self.show_binding_feedback(
                child_node_id, "", False,
                "Parent binding is not supported by this node")
            return
        self._pending_expression_binding = None
        self.parent_binding_changed.emit(child_node_id, parent_node_id)

    def show_binding_feedback(self, target_node_id: str, param_name: str,
                              success: bool, message: str = "") -> None:
        """Show the result of a pick-whip operation and open expressions.

        NodeGraph owns type checks and cycle detection, so the CompositePage
        calls this after its authoritative write. A successful parameter
        binding is a generated expression; opening the expression editor
        immediately makes that relationship inspectable and editable.
        """
        text = str(message or ("Binding applied" if success
                               else "Binding rejected"))
        if hasattr(self, "_binding_feedback_label"):
            self._binding_feedback_serial += 1
            serial = self._binding_feedback_serial
            self._binding_feedback_label.setText(text)
            self._binding_feedback_label.setStyleSheet(
                f"color: {timeline_layout.resolve_token('accent_blue' if success else 'error')};"
                f" font-size: 10px;")
            self._binding_feedback_label.setVisible(True)
            QTimer.singleShot(2600, lambda: self._clear_binding_feedback(serial))
        pending = self._pending_expression_binding
        self._pending_expression_binding = None
        if success and param_name and pending == (target_node_id, param_name):
            QTimer.singleShot(0, lambda: self._open_expression_for_param(
                target_node_id, param_name))

    def _clear_binding_feedback(self, serial: int) -> None:
        if serial != self._binding_feedback_serial:
            return
        label = getattr(self, "_binding_feedback_label", None)
        if label is not None:
            label.clear()
            label.setVisible(False)

    def _open_keyframe_editor_for_param(self, node_id: str,
                                        param_name: str) -> None:
        """Open the Alt-click keyframe editor for the current/nearest point."""
        node = self._node_graph.get_node(node_id) if self._node_graph else None
        if self._is_color_param(node, param_name):
            # A scalar inline editor cannot represent a whole RGBA value.
            # Use the row diamond for atomic add/remove instead.
            return
        pdef = node.get_param_defs().get(param_name) if node is not None else None
        if getattr(pdef, "ptype", "float") not in {"float", "int"}:
            # Discrete/vector properties use the stopwatch/timeline contract;
            # the scalar inline editor cannot represent their value domain.
            return
        data = getattr(node, "keyframe_tracks", {}).get(param_name) if node else None
        keyframes = data.get("keyframes", []) if isinstance(data, dict) else []
        if not keyframes:
            # Alt-click on an unanimated property follows AE's useful affordance:
            # create the first point at the playhead, then expose its controls.
            value = self._value_provider(node_id, param_name) \
                if self._value_provider else 0.0
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = 0.0
            self._timeline_view.add_keyframe_at(
                node_id, param_name, int(self._state.current_frame),
                value_hint=value)
            data = getattr(node, "keyframe_tracks", {}).get(param_name) if node else None
            keyframes = data.get("keyframes", []) if isinstance(data, dict) else []
        if not keyframes:
            return
        current = int(self._state.current_frame)
        source = next((kf for kf in keyframes if int(kf.get("frame", -1)) == current), None)
        if source is None:
            source = min(keyframes, key=lambda k: abs(int(k.get("frame", 0)) - current))
        is_roto_point = bool(
            node is not None and getattr(node, "node_type", "") == "roto_shape"
            and str(param_name).startswith("roto_point_"))
        integer = bool(pdef is not None and getattr(pdef, "ptype", "float") == "int")
        min_val = 0.0 if is_roto_point else float(
            getattr(pdef, "min_val", -1_000_000.0))
        max_val = 1.0 if is_roto_point else float(
            getattr(pdef, "max_val", 1_000_000.0))
        step = 0.01 if is_roto_point else float(
            getattr(pdef, "step", 0.01) or 0.01)
        decimals = 3 if is_roto_point else int(
            getattr(pdef, "decimals", 3) or 3)
        self._on_param_selected(node_id, param_name)
        self._tree_view.open_keyframe_editor(
            node_id, param_name, int(source.get("frame", current)),
            float(source.get("value", 0.0)),
            str(source.get("interpolation", "linear")),
            min_val, max_val, step, decimals, integer)

    def _on_keyframe_editor_commit(self, node_id: str, param_name: str,
                                   old_frame: int, new_frame: int,
                                   value: float, interpolation: str) -> None:
        self._timeline_view.update_keyframe_at(
            node_id, param_name, old_frame, new_frame, value, interpolation)
        self._open_keyframe_editor_for_param(node_id, param_name)

    def _on_keyframe_editor_remove(self, node_id: str, param_name: str,
                                   frame: int) -> None:
        self._timeline_view.remove_keyframe_at(node_id, param_name, frame)
        self._tree_view._close_keyframe_editor()

    def _open_binding_menu(self, node_id: str, param_name: str) -> None:
        """Open a compact AE pick-whip menu at the clicked property cell."""
        if self._node_graph is None:
            return
        menu = QMenu(self)
        if param_name:
            current = self._node_graph.get_parameter_binding(node_id, param_name)
            if current:
                clear = menu.addAction(I18N.tr(
                    "keyframes.unbind", "Remove parameter binding"))
                clear.triggered.connect(
                    lambda: self.parameter_binding_changed.emit(
                        node_id, param_name, "", ""))
                menu.addSeparator()
            title = menu.addAction(I18N.tr(
                "keyframes.bind_to", "Bind to parameter"))
            title.setEnabled(False)
            for source in self._node_graph.nodes.values():
                if source.id == node_id:
                    continue
                source_menu = menu.addMenu(source.display_title)
                # B3-2: get_param_defs() (not the class attribute) so
                # dynamically promoted group parameters are bindable too.
                for pdef in source.get_param_defs().values():
                    if not pdef.is_animatable() or getattr(pdef, "hidden", False):
                        continue
                    action = source_menu.addAction(
                        pdef.name.replace("_", " ").title())
                    action.triggered.connect(
                        lambda _checked=False, sn=source.id,
                        sp=pdef.name: self.parameter_binding_changed.emit(
                            node_id, param_name, sn, sp))
            menu.exec(QCursor.pos())
            return

        # Node-row bind cell = parent picker.
        if not self._parent_binding_supported(node_id):
            return
        current_parent = self._node_graph.get_node_parent(node_id)
        if current_parent:
            clear = menu.addAction(I18N.tr(
                "keyframes.clear_parent", "Clear parent"))
            clear.triggered.connect(
                lambda: self.parent_binding_changed.emit(node_id, None))
            menu.addSeparator()
        title = menu.addAction(I18N.tr(
            "keyframes.parent_to", "Parent to"))
        title.setEnabled(False)
        for parent in self._node_graph.nodes.values():
            if (parent.id == node_id
                    or not self._parent_binding_supported(parent.id)):
                continue
            action = menu.addAction(parent.display_title)
            action.triggered.connect(
                lambda _checked=False, pid=parent.id:
                self.parent_binding_changed.emit(node_id, pid))
        menu.exec(QCursor.pos())

    def _rebuild_filtered_model(self) -> None:
        """Rebuild rows after a toolbar search/filter change."""
        if self._node_graph is None:
            return
        selected = getattr(self._model, "_selected_node_id", None)
        self.set_node_graph(self._node_graph, selected_node_id=selected,
                            selected_layer_id=self._selected_layer_id)

    def _on_search_changed(self, text: str) -> None:
        self._model.set_text_filter(text)
        self._rebuild_filtered_model()

    def _open_expression_for_param(self, node_id: str,
                                   param_name: str) -> None:
        """Select a property and open the shared Graph Expression Editor."""
        self._on_param_selected(node_id, param_name)
        self.set_view_mode("graph")
        self.set_expression_editor_expanded(True)
        self._refresh_expression_editor()

    def set_expression_editor_expanded(self, expanded: bool) -> None:
        """Show or collapse the expression editor in the right pane.

        The editor is deliberately independent from Graph/Timeline mode:
        entering Graph mode does not automatically consume vertical space.
        The explicit expression-row action opens it, and its splitter handle
        lets the user choose the height without affecting the layer column.
        """
        self._expression_editor_expanded = bool(expanded)
        if hasattr(self, "_expr_editor"):
            self._set_expression_editor_visible(
                self._view_mode == "graph" and self._expression_editor_expanded)

    def _set_expression_editor_visible(self, visible: bool) -> None:
        """Apply expression-editor visibility and preserve its user height."""
        if not hasattr(self, "_expr_editor"):
            return
        if not visible:
            self._expr_editor.hide()
            if hasattr(self, "_right_splitter"):
                sizes = self._right_splitter.sizes()
                total = max(1, sum(sizes), self._right_splitter.height())
                self._right_splitter.setSizes([total, 0])
            return

        self._expr_editor.show()
        if not hasattr(self, "_right_splitter"):
            return
        # The panel may not have a real height during construction. The next
        # resizeEvent will apply the remembered height once it does.
        total = max(1, self._right_splitter.height(),
                    sum(self._right_splitter.sizes()))
        editor_height = min(
            max(self._expr_editor.minimumHeight(), self._expression_editor_height),
            max(self._expr_editor.minimumHeight(), total - 1),
        )
        self._right_splitter.setSizes([max(1, total - editor_height),
                                       editor_height])

    def _on_right_splitter_moved(self, _position: int, _index: int) -> None:
        """Remember the expression editor height after a user drag."""
        if not self._expression_editor_expanded or not hasattr(
                self, "_right_splitter"):
            return
        sizes = self._right_splitter.sizes()
        if len(sizes) > 1 and sizes[1] > 0:
            self._expression_editor_height = int(sizes[1])

    def _on_filter_changed(self, _index: int) -> None:
        combo = getattr(self, "_filter_combo", None)
        if combo is None:
            return
        self._model.set_filter(str(combo.currentData() or "all"))
        self._rebuild_filtered_model()

    # ── shared selection + expression status (P1/T11/T13/T49 host) ──

    @property
    def selection_model(self) -> AnimationSelectionModel:
        """The shared, observable selection model (T11/T13).

        CompositePage / SplinePanel / future Graph Editor subscribe to this
        to stay in sync with the Keyframes timeline's selection. Reads +
        writes flow through here; the panel keeps the legacy per-point
        'selected' flags as the painted source of truth and mirrors them.
        """
        return self._selection_model

    def sync_selection_to_shared_model(self) -> None:
        """Push the panel's current selection into the shared model.

        Call after any in-panel selection gesture (tree click, marquee,
        keyboard) so subscribers see the update. The rebuild path in
        set_node_graph already calls this; gesture handlers invoke it too.
        """
        self._selection_model.sync_from_model(self._model)

    def _on_timeline_selection_changed(self, _selection) -> None:
        """Mirror live timeline point selection into the shared model."""
        self.sync_selection_to_shared_model()

    def apply_shared_selection_to_views(self) -> None:
        """Pull selection from the shared model back onto the views.

        Used when another surface (Spline panel, AI, script) changes the
        selection and the Keyframes timeline needs to reflect it.
        """
        self._selection_model.apply_to_model(self._model)
        self._tree_view.refresh()
        self._timeline_view.refresh()

    def _expression_status_for(self, node_id: str, param_name: str):
        """T49 host: provider the tree view calls at paint time.

        Returns an ExpressionStatus from the live graph, or None when the
        node/param can't be resolved (no marker drawn). Uses the panel's
        ExpressionPolicy so the precedence decision is centralised.
        """
        graph = self._node_graph
        if graph is None:
            return None
        try:
            node = graph.get_node(node_id)
        except Exception:
            # AUD-10100: broad on purpose — this runs from a paint callback
            # and a buggy host graph must never crash painting (locked by
            # test_keyframes_panel_shared_selection). The failure is logged
            # so it is no longer silently masked.
            logger.debug("Expression status: node lookup failed for %s",
                         node_id, exc_info=True)
            return None
        if node is None:
            return None
        try:
            status = derive_expression_status(
                node, param_name, policy=self._expression_policy)
            if status.state == EXPRESSION_ERROR:
                return status

            # Syntax-only status is enough before the first frame, but a
            # runtime failure (missing dependency, non-finite result, etc.)
            # must also be visible in the row marker.  Evaluate diagnostics
            # once per graph generation/frame; painting a tree row must not
            # re-run the complete graph for every call.
            get_errors = getattr(graph, "get_expression_errors", None)
            if callable(get_errors) and status.has_expression and status.enabled:
                frame = int(getattr(self._state, "current_frame", 0))
                fps = float(getattr(self._state, "fps", 24.0) or 24.0)
                cache_key = (id(graph), int(getattr(graph, "generation", 0)),
                             frame, round(fps, 6))
                if getattr(self, "_expression_diagnostics_key", None) != cache_key:
                    self._expression_diagnostics_key = cache_key
                    try:
                        self._expression_diagnostics = get_errors(
                            frame_number=frame, fps=fps)
                    except Exception:
                        # get_errors evaluates user-authored expressions
                        # across the whole graph — arbitrary user code can
                        # raise anything, but a paint callback must stay
                        # alive. Logged so the failing expression is still
                        # diagnosable.
                        logger.debug("Expression diagnostics evaluation "
                                     "failed", exc_info=True)
                        self._expression_diagnostics = {}
                message = (getattr(self, "_expression_diagnostics", {})
                           .get(node_id, {}).get(param_name))
                if message:
                    return ExpressionStatus(
                        state=EXPRESSION_ERROR,
                        text=status.text,
                        error_code=EXPR_ERR_UNKNOWN,
                        error_message=str(message),
                        source_value=status.source_value,
                        enabled=status.enabled,
                    )
            return status
        except (AttributeError, KeyError, TypeError, ValueError):
            logger.debug("Expression status unavailable for %s/%s",
                         node_id, param_name, exc_info=True)
            return None

    def set_timeline_state(self, state: CompositeTimelineState) -> None:
        range_changed = (
            state.start_frame != self._state.start_frame or
            state.end_frame != self._state.end_frame or
            abs(float(state.fps) - float(self._state.fps)) > 1e-6)
        self._state = state
        self._timeline_view.set_current_frame(state.current_frame)
        self._tree_view.set_current_frame(state.current_frame)
        # The timeline view uses the actual composition bounds for the orange
        # work-area band. A longer media bar remains visible on the extended
        # ruler and is editable outside this playback range.
        self._timeline_view.set_composition_range(
            *self._effective_comp_range())
        # Ruler labels follow the panel's display unit (frames/timecode).
        self._timeline_view.set_display(state.display_mode, state.fps)
        self._frame_label.setText(str(state.current_frame))
        # T25: push fps + display mode + current frame to the time display.
        if hasattr(self, "_time_display"):
            self._time_display.set_fps(state.fps)
            self._time_display.set_display_mode(
                "timecode" if state.display_mode == "time"
                else state.display_mode)
            self._time_display.set_menu_display_mode(state.display_mode)
            self._time_display.set_current_frame(state.current_frame)
        if hasattr(self, "_time_unit_combo"):
            self._time_unit_combo.blockSignals(True)
            try:
                self._time_unit_combo.setCurrentIndex({
                    "time": 0, "frame": 1, "seconds": 2,
                    "timecode": 3,
                }.get(state.display_mode, 0))
            finally:
                self._time_unit_combo.blockSignals(False)
        # T105: push fps + frame range + current frame to the graph editor
        # so its x axis and sampling stay aligned with the comp clip.
        if hasattr(self, "_graph_view"):
            self._graph_view.set_fps(state.fps)
            self._graph_view.set_frame_range(state.start_frame,
                                             state.end_frame)
            self._graph_view.set_current_frame(state.current_frame)
        self._update_range_label()
        if range_changed:
            # Default (AE-style) layer bars follow the composition extent,
            # not any source-media length outside the composition.
            self._model.set_comp_range(*self._effective_comp_range())
            # Show a full-composition work area on first bind and after a
            # composition change. Once the user edits I/O, preserve that
            # explicit range and clamp it to the new comp bounds.
            if not self._play_range_explicit:
                comp_start, comp_end = self._effective_comp_range()
                self._play_in = int(comp_start)
                self._play_out = int(comp_end)
                self._timeline_view.set_play_range(
                    self._play_in, self._play_out)
                self._graph_view.set_play_range(
                    (self._play_in, self._play_out))
            else:
                self._clamp_play_range()
            # The ruler may include a longer media source, but the default
            # viewport fit is deliberately based on the composition extent.
            # Defer the calculation one event-loop turn even when a temporary
            # splitter width is already available. A dock can receive its
            # range before the splitter's final width is applied; fitting to
            # that temporary width leaves the timeline compressed after show.
            self._fit_pending = True
            QTimer.singleShot(0, self._complete_deferred_fit)

    def set_frame_range(self, start_frame: int, end_frame: int,
                        fps: Optional[float] = None) -> None:
        """Align the panel's ruler range with the host timeline view.

        ``end_frame`` is inclusive (last playable frame). Re-fits the ruler
        only when the range actually changes so user zoom is preserved.
        """
        state = CompositeTimelineState(
            start_frame=int(start_frame),
            end_frame=int(end_frame),
            current_frame=self._state.current_frame,
            fps=float(fps) if fps else self._state.fps,
            display_mode=self._state.display_mode)
        self.set_timeline_state(state)

    def set_comp_extent(self, start_frame: int, end_frame: int) -> None:
        """Declare the real composition extent (inclusive frames).

        The work area stays on this extent. Video media longer than the comp
        keeps its source length in the model and on the extended ruler. Call
        after :meth:`set_frame_range` when the host updates comp metadata.
        """
        extent = (int(start_frame), int(end_frame))
        if extent == self._comp_extent:
            return
        self._comp_extent = extent
        self._model.set_comp_range(*extent)
        self._timeline_view.set_composition_range(*extent)
        if not self._play_range_explicit:
            self._play_in, self._play_out = extent
            self._timeline_view.set_play_range(*extent)
            self._graph_view.set_play_range(extent)
        else:
            self._clamp_play_range()
        # A changed composition extent is a new default viewport target even
        # when the extended media/ruler range itself did not change.
        self._fit_pending = True
        QTimer.singleShot(0, self._complete_deferred_fit)

    def _effective_comp_range(self) -> Tuple[int, int]:
        """Composition extent, falling back to the ruler range."""
        if self._comp_extent is not None:
            return self._comp_extent
        return self._state.start_frame, self._state.end_frame

    def set_playback_controller(self, controller: Optional[object]) -> None:
        """Bind the external playback engine (CompositePage).

        The controller receives ``play_toggle_requested`` signals and calls
        ``set_playing_state()`` back to keep the button icon in sync.

        AUD-10101: rebinding or clearing first disconnects the previous
        controller — otherwise a second bind left the old object connected
        alongside the new one (double play toggles) and clearing never
        released it.
        """
        old = getattr(self, "_playback_controller", None)
        if old is not None:
            try:
                self.play_toggle_requested.disconnect(
                    old.keyframes_panel_play_toggle)
            except (AttributeError, TypeError, RuntimeError):
                # Not connected (or already gone) — nothing to clean up.
                pass
        self._playback_controller = controller
        if controller is not None:
            try:
                self.play_toggle_requested.connect(
                    controller.keyframes_panel_play_toggle)
            except (AttributeError, TypeError, RuntimeError):
                self._playback_controller = None

    def set_value_provider(self, provider: Optional[
            Callable[[str, str], Optional[Any]]]) -> None:
        """Provider(node_id, param_name) → current live value for new kfs."""
        self._value_provider = provider

    def set_playing_state(self, playing: bool) -> None:
        """Update the play button icon without re-triggering playback."""
        playing = bool(playing)
        if not playing:
            # CP-058 复查修复：停止时清掉变速累加器与往复方向，
            # 避免下次播放从上次残留的半帧偏移/反向开始。
            self._play_accum = 0.0
            self._play_direction = 1
        if playing == self._playing:
            # Still sync the checkable action's visual state.
            if self._act_play.isChecked() != playing:
                self._act_play.blockSignals(True)
                self._act_play.setChecked(playing)
                self._act_play.blockSignals(False)
            return
        self._playing = playing
        self._act_play.blockSignals(True)
        self._act_play.setChecked(playing)
        self._act_play.blockSignals(False)
        self._act_play.setIcon(make_qicon("pause" if playing else "play"))
        self._act_play.setText(I18N.tr(
            "keyframes.pause", "Pause") if playing else I18N.tr(
            "keyframes.play", "Play"))
        if not playing and self._play_timer.isActive():
            self._play_timer.stop()

    def set_current_frame(self, frame: int) -> None:
        """Silently update the panel playhead (no signal emitted).

        P0-1: this is the write-back path called by CompositePage while it is
        syncing frames across panels. It must NOT emit current_frame_changed,
        otherwise the page's anti-recursion guard is the only thing saving us
        from a bounce. User-initiated seeks use seek_to_frame() instead.
        """
        self._state.current_frame = int(frame)
        self._timeline_view.set_current_frame(int(frame))
        self._tree_view.set_current_frame(int(frame))
        # Refresh the shared DragValue controls so their displayed value and
        # Properties-style edit base follow the same sampled frame.
        self._tree_view.refresh()
        self._frame_label.setText(str(int(frame)))
        # T25: keep the time display in sync (it shows timecode + frame).
        if hasattr(self, "_time_display"):
            self._time_display.set_current_frame(int(frame))
        # T105: keep the graph editor's playhead in sync.
        if hasattr(self, "_graph_view"):
            self._graph_view.set_current_frame(int(frame))

    def seek_to_frame(self, frame: int) -> None:
        """User-initiated playhead seek: update visuals AND emit the signal.

        P0-1: toolbar prev/next-keyframe buttons and any in-panel seek go
        through here so CompositePage receives the change and re-renders.
        The frame is clamped to the composition clip's range.
        """
        frame = max(self._state.start_frame,
                    min(int(frame), max(self._state.start_frame,
                                        self._state.end_frame)))
        self.set_current_frame(frame)
        self.current_frame_changed.emit(int(frame))

    def on_node_color_changed(self, node_id: str) -> None:
        """Notify that a node's color changed so the panel can refresh."""
        # The model reads effective_header_color on rebuild; a targeted refresh
        # is enough for phase 3.
        self._timeline_view.update()

    # ── work area (play range) ──

    def set_play_in_at_playhead(self) -> None:
        """I key: set the work-area In point at the current playhead."""
        frame = int(self._state.current_frame)
        out = self._play_out if self._play_out is not None else self._state.end_frame
        self.set_play_range(min(frame, out), max(frame, out))

    def set_play_out_at_playhead(self) -> None:
        """O key: set the work-area Out point at the current playhead."""
        frame = int(self._state.current_frame)
        pin = self._play_in if self._play_in is not None else self._state.start_frame
        self.set_play_range(min(frame, pin), max(frame, pin))

    def set_play_range(self, in_frame: int, out_frame: int) -> None:
        """Set the work area (inclusive) and notify listeners."""
        comp_start, comp_end = self._effective_comp_range()
        in_frame = max(comp_start, int(in_frame))
        out_frame = min(comp_end, int(out_frame))
        if out_frame < in_frame:
            out_frame = in_frame
        if (in_frame, out_frame) == (self._play_in, self._play_out):
            self._play_range_explicit = True
            return
        self._play_in = in_frame
        self._play_out = out_frame
        self._play_range_explicit = True
        self._timeline_view.set_play_range(in_frame, out_frame)
        # T121: keep the Graph canvas in sync (shared work-area overlay +
        # snap target).
        self._graph_view.set_play_range((in_frame, out_frame))
        self._update_range_label()
        self.play_range_changed.emit(in_frame, out_frame)

    def clear_play_range(self) -> None:
        """Reset the work area to the whole composition range."""
        if self._play_in is None and self._play_out is None:
            return
        self._play_in = None
        self._play_out = None
        self._play_range_explicit = False
        self._timeline_view.set_play_range(None, None)
        # T121: clear the Graph canvas's overlay too.
        self._graph_view.set_play_range(None)
        comp_start, comp_end = self._effective_comp_range()
        self._update_range_label()
        self.play_range_changed.emit(comp_start, comp_end)

    def effective_play_range(self) -> Tuple[int, int]:
        """The inclusive [in, out] playback range (or full composition)."""
        if self._play_in is not None and self._play_out is not None:
            return (self._play_in, self._play_out)
        return self._effective_comp_range()

    def play_range_points(self) -> Tuple[Optional[int], Optional[int]]:
        """Displayed work-area points.

        A newly bound panel returns the full composition range because the
        AE-style In/Out markers are visible by default. After an explicit
        clear this returns ``(None, None)`` until the next composition bind.
        """
        return (self._play_in, self._play_out)

    def _clamp_play_range(self) -> None:
        """Keep the work area inside the composition extent."""
        if self._play_in is None or self._play_out is None:
            return
        lo, hi = self._effective_comp_range()
        pin = max(lo, min(self._play_in, hi))
        pout = max(pin, min(self._play_out, hi))
        if (pin, pout) != (self._play_in, self._play_out):
            self._play_in, self._play_out = pin, pout
            self._timeline_view.set_play_range(pin, pout)
            self._graph_view.set_play_range((pin, pout))
            self.play_range_changed.emit(pin, pout)

    def _on_timeline_play_range_changed(self, in_frame: int,
                                        out_frame: int) -> None:
        """Work-area band dragged on the ruler — adopt + broadcast."""
        lo, hi = self._effective_comp_range()
        self._play_in = max(lo, min(int(in_frame), hi))
        self._play_out = max(self._play_in,
                             min(int(out_frame), hi))
        self._play_range_explicit = True
        self._timeline_view.set_play_range(self._play_in, self._play_out)
        self._graph_view.set_play_range((self._play_in, self._play_out))
        self._update_range_label()
        self.play_range_changed.emit(self._play_in, self._play_out)

    def _on_timeline_range_changed(self, node_id: str,
                                   payload: dict) -> None:
        """Forward a layer-bar edit with a stable public payload.

        ``edit`` describes the current mouse gesture only; it is needed by
        the host to distinguish a head trim from a tail trim, but it is not a
        persisted node time-range field. Keep it on the host-facing signal
        and strip it from the long-standing public signal.
        """
        data = dict(payload) if isinstance(payload, dict) else {}
        public_payload = {key: value for key, value in data.items()
                          if key != "edit"}
        self.range_changed.emit(node_id, public_payload)
        self.range_edit_changed.emit(node_id, data)

    # ── internal slots ──

    def _on_playhead_moved(self, frame: int) -> None:
        self._state.current_frame = int(frame)
        self._frame_label.setText(str(int(frame)))
        self.current_frame_changed.emit(int(frame))

    def _on_expand_changed(self, node_id: str, expanded: bool) -> None:
        """Refresh the timeline side when a node's expansion changes (P9)."""
        # The Value column uses real DragValue child widgets layered above the
        # tree's paint surface.  Collapsing a node changes ``visible_rows``;
        # refreshing only the timeline repaints the rows but leaves those
        # child widgets at their old positions until the next scroll/resize.
        # Re-sync the tree first so controls for hidden parameters are hidden
        # immediately as well.
        self._tree_view.refresh()
        self._timeline_view.refresh()
        self._clamp_vertical_scroll()

    def _on_time_unit_changed(self, _index: int) -> None:
        self._state.display_mode = {
            0: "time", 1: "frame", 2: "seconds", 3: "timecode",
        }.get(int(self._time_unit_combo.currentIndex()), "time")
        # The ruler labels follow the same unit.
        self._timeline_view.set_display(self._state.display_mode,
                                        self._state.fps)
        self._timeline_view.refresh()
        # Keep the time display, ruler and context menu in lockstep.
        if hasattr(self, "_time_display"):
            self._time_display.set_display_mode(
                "timecode" if self._state.display_mode == "time"
                else self._state.display_mode)
            self._time_display.set_menu_display_mode(self._state.display_mode)

    def _on_time_display_mode_requested(self, mode: str) -> None:
        """Apply a unit selected from the timecode's context menu."""
        index = {"time": 0, "frame": 1, "seconds": 2,
                 "timecode": 3}.get(str(mode))
        if index is None:
            return
        self._time_unit_combo.setCurrentIndex(index)

    def _on_view_display_mode_changed(self, mode: str) -> None:
        """Keep the hidden compatibility combo in sync with the ruler menu."""
        self._state.display_mode = str(mode)
        index = 3 if str(mode) == "timecode" else 1
        self._time_unit_combo.blockSignals(True)
        try:
            self._time_unit_combo.setCurrentIndex(index)
        finally:
            self._time_unit_combo.blockSignals(False)
        if hasattr(self, "_time_display"):
            self._time_display.set_display_mode(
                "timecode" if str(mode) == "time" else str(mode))
            self._time_display.set_menu_display_mode(str(mode))

    def _expand_all(self, expanded: bool) -> None:
        self._model.expand_all(expanded)
        self._tree_view.refresh()
        self._timeline_view.refresh()
        self._clamp_vertical_scroll()

    def fit_time_range(self) -> None:
        # Fit the active composition, not an optional media-extended ruler.
        # Longer source media remains available through horizontal scrolling
        # and the existing trim handles, without wasting the initial view.
        comp_start, comp_end = self._effective_comp_range()
        self._timeline_view.fit_range(comp_start, comp_end, self._state)

    def _pan_to_playhead(self) -> None:
        self._timeline_view.pan_to_frame(self._state.current_frame)

    def _zoom_in(self) -> None:
        self._frame_mapper.set_zoom(self._frame_mapper.pixels_per_frame * 1.25)
        self._timeline_view.refresh()

    def _zoom_out(self) -> None:
        """T27: zoom-out counterpart to ``_zoom_in``. Symmetric 1/1.25 step
        so zoom-in then zoom-out returns to the same scale (no drift)."""
        self._frame_mapper.set_zoom(self._frame_mapper.pixels_per_frame / 1.25)
        self._timeline_view.refresh()

    def fit_selection(self) -> None:
        """T27/§3.2.D: fit the selected keyframes (not the whole comp) to
        the view. Falls back to :meth:`fit_time_range` when nothing is
        selected — same behaviour as AE's "fit selected" with no selection."""
        frames: list = []
        for track in self._model.tracks().values():
            for kf in track.keyframes:
                if getattr(kf, "selected", False):
                    frames.append(kf.frame)
        if len(frames) < 2:
            # Need at least two points to define a span; otherwise behave
            # like the full-range fit so the button isn't a no-op.
            self.fit_time_range()
            return
        lo, hi = min(frames), max(frames)
        # Pad by 10 % on each side so the selection isn't flush with the
        # edges (AE leaves a small margin around the fitted selection).
        pad = max(1, int((hi - lo) * 0.1))
        self._timeline_view.fit_range(lo - pad, hi + pad, self._state)

    def _on_snap_toggled(self, enabled: bool) -> None:
        """T27 snap toggle handler. Forwards the new state to the timeline
        view; the snap math itself lands with T46 (drag snapping). Kept as
        a separate method (not a lambda) so the wiring is testable and so
        future side-effects (e.g. persisting the toggle in session state)
        have an obvious home."""
        self._snap_enabled = bool(enabled)
        # The view exposes a snap setter so the drag handler can read it
        # without importing the panel; if the view doesn't have one yet
        # (pre-T46 build), this is a harmless no-op.
        setter = getattr(self._timeline_view, "set_snap_enabled", None)
        if callable(setter):
            setter(self._snap_enabled)

    def _on_graph_editor_toggled(self, enabled: bool) -> None:
        """T100 Graph Editor toggle handler. Flips the right-hand canvas
        via set_view_mode (which goes through the GraphCanvasHost stack).
        Connected to the toolbar button; the Shift+F3 shortcut calls
        toggle_view_mode directly, which keeps the button in sync."""
        self.set_view_mode("graph" if enabled else "timeline")

    def _on_graph_type_changed(self, index: int) -> None:
        """T110: toolbar Graph Type combo → push the new projection to the
        graph view. Index 0=auto, 1=value, 2=speed (matches addItems order)."""
        mode = {0: "auto", 1: "value", 2: "speed"}.get(int(index), "auto")
        if hasattr(self, "_graph_view"):
            self._graph_view.set_graph_type(mode)

    def _goto_prev_keyframe(self) -> None:
        frame = self._nearest_keyframe(self._state.current_frame, -1)
        if frame is not None:
            # P0-1: emit so CompositePage re-renders the new frame.
            self.seek_to_frame(frame)

    def _goto_next_keyframe(self) -> None:
        frame = self._nearest_keyframe(self._state.current_frame, +1)
        if frame is not None:
            # P0-1: emit so CompositePage re-renders the new frame.
            self.seek_to_frame(frame)

    def _nearest_keyframe(self, frame: int, direction: int) -> Optional[int]:
        """Find the nearest keyframe strictly before/after ``frame``."""
        candidates = []
        for track in self._model.tracks().values():
            for kf in track.keyframes:
                if direction < 0 and kf.frame < frame:
                    candidates.append(kf.frame)
                elif direction > 0 and kf.frame > frame:
                    candidates.append(kf.frame)
        if not candidates:
            return None
        if direction < 0:
            return max(candidates)
        return min(candidates)

    # ── transport / playback ──

    def _goto_first_frame(self) -> None:
        self.seek_to_frame(self._state.start_frame)

    def _goto_last_frame(self) -> None:
        self.seek_to_frame(self._state.end_frame)

    def _step_frame_backward(self) -> None:
        self.seek_to_frame(self._state.current_frame - 1)

    def _step_frame_forward(self) -> None:
        self.seek_to_frame(self._state.current_frame + 1)

    def _on_play_toggled(self, checked: bool) -> None:
        """Play/pause button: prefer the external engine, else internal timer."""
        if self._disposed or not self._composition_active:
            self._internal_set_playing(False)
            return
        if self._playback_controller is not None:
            # CompositePage drives playback; it calls set_playing_state back.
            self.play_toggle_requested.emit(bool(checked))
            return
        self._internal_set_playing(bool(checked))

    def _internal_set_playing(self, playing: bool) -> None:
        """Standalone playback engine (no external controller bound)."""
        if self._disposed or not self._composition_active:
            playing = False
        if playing:
            fps = max(1.0, float(self._state.fps or 24.0))
            in_f, out_f = self.effective_play_range()
            # Restart from the In point when parked at/past the Out point
            # or outside the work area.
            cur = self._state.current_frame
            if cur >= out_f or cur < in_f:
                self.seek_to_frame(in_f)
            self._play_timer.start(max(1, int(1000.0 / fps)))
        else:
            self._play_timer.stop()
        self.set_playing_state(playing)

    def _on_internal_play_tick(self) -> None:
        if self._disposed or not self._composition_active or not self._playing:
            self._play_timer.stop()
            return
        in_f, out_f = self.effective_play_range()
        # CP-058: 以 speed 累加推进（<1x 时多 tick 走一帧，>1x 时一 tick
        # 走多帧）；pingpong 在边界翻转方向，普通模式循环回入点。
        self._play_accum += self._playback_speed * self._play_direction
        if abs(self._play_accum) < 1.0:
            return
        step = int(self._play_accum)
        self._play_accum -= step
        nxt = self._state.current_frame + step
        if self._play_pingpong:
            if nxt > out_f:
                self._play_direction = -1
                self._play_accum = 0.0
                nxt = out_f - 1 if out_f > in_f else out_f
            elif nxt < in_f:
                self._play_direction = 1
                self._play_accum = 0.0
                nxt = in_f + 1 if out_f > in_f else in_f
        elif nxt > out_f:
            nxt = in_f + (nxt - out_f - 1)
            nxt = max(in_f, min(out_f, nxt))
        self.seek_to_frame(nxt)

    def set_playback_speed(self, speed: float) -> None:
        """CP-058: playback speed multiplier (0.25-4)."""
        self._playback_speed = max(0.25, min(4.0, float(speed)))
        self._play_accum = 0.0

    def set_play_pingpong(self, enabled: bool) -> None:
        """CP-058: ping-pong playback (reverse at range ends)."""
        self._play_pingpong = bool(enabled)
        self._play_direction = 1
        self._play_accum = 0.0

    # ── selection + keyframe editing ──

    def _on_node_selected(self, node_id: str) -> None:
        # Selecting a node clears the param target (add-kf falls back to all
        # animatable params of that node).
        self._selected_param = None
        for row in self._model.rows:
            if row.row_type == "param":
                row.selected = False
        # T11/T13: mirror the row-level selection into the shared model so
        # subscribers see the node highlight immediately.
        self._selection_model.sync_from_model(self._model)
        self.node_selected.emit(node_id)
        self._tree_view.refresh()
        self._timeline_view.refresh()

    def _layer_for_root(self, root_node_id: str):
        """Resolve a root-node row to a Layer only in Layers mode."""
        if (self._layer_view_mode != KeyframesModel.VIEW_MODE_LAYERS
                or self._layer_stack_controller is None):
            return None
        try:
            for layer in self._layer_stack_controller.layers_map().values():
                if layer.root_node_id == root_node_id:
                    return layer
        except Exception:
            return None
        return None

    def _on_layer_selected(self, layer_id: str) -> None:
        self._selected_layer_id = str(layer_id)
        self._tree_view.set_selected_layer_id(self._selected_layer_id)
        self.layer_selected.emit(layer_id)

    def _on_layer_lock_changed(self, root_node_id: str, locked: bool) -> None:
        layer = self._layer_for_root(root_node_id)
        if layer is None:
            return
        if not self._layer_stack_controller.set_layer_locked(
                layer.layer_id, locked):
            self._rebuild_filtered_model()

    def _on_layer_eye_changed(self, root_node_id: str, enabled: bool) -> None:
        layer = self._layer_for_root(root_node_id)
        if layer is None:
            return
        if not self._layer_stack_controller.set_layer_visibility(
                layer.layer_id, enabled):
            self._rebuild_filtered_model()

    def _on_layer_solo_changed(self, layer_id: str, solo: bool) -> None:
        if (self._layer_view_mode != KeyframesModel.VIEW_MODE_LAYERS
                or self._layer_stack_controller is None):
            return
        if not self._layer_stack_controller.set_layer_solo(layer_id, solo):
            self._rebuild_filtered_model()

    def _on_layer_reorder_requested(self, layer_id: str,
                                    insert_index: int) -> None:
        if (self._layer_view_mode != KeyframesModel.VIEW_MODE_LAYERS
                or self._layer_stack_controller is None):
            return
        stack = self._layer_stack_controller.main_stack()
        if stack is None or layer_id not in stack.layer_ids:
            return
        old_index = stack.layer_ids.index(layer_id)
        # The tree reports a boundary in the pre-removal list. Controller
        # indices are post-pop insertion indices, so moving downward shifts
        # the boundary by one.
        target = int(insert_index)
        if old_index < target:
            target -= 1
        if old_index == target:
            return
        if self._layer_stack_controller.move_layer(layer_id, target):
            self.layer_reordered.emit(layer_id, target)

    def _on_param_selected(self, node_id: str, param_name: str) -> None:
        self._selected_param = (node_id, param_name)
        self._apply_param_selection(self._selected_param, refresh=True)
        # T11/T13: param selection updates the shared selection's primary row.
        self._selection_model.sync_from_model(self._model)
        # T120: keep the Graph Expression Editor bound to the new param.
        if hasattr(self, "_expr_editor") and self._view_mode == "graph":
            self._refresh_expression_editor()

    def _apply_param_selection(self, target: Tuple[str, str],
                               refresh: bool) -> None:
        node_id, param_name = target
        row_id = f"param:{node_id}:{param_name}"
        found = False
        for row in self._model.rows:
            if row.row_type != "param":
                continue
            is_target = (row.row_id == row_id)
            row.selected = is_target
            found = found or is_target
        if not found:
            self._selected_param = None
        if refresh:
            self._tree_view.refresh()
            self._timeline_view.refresh()

    def _keyframe_targets(self) -> list:
        """Resolve (node_id, param_name) targets for add/remove at playhead.

        Priority: selected param row → all animatable params of the selected
        node → all animatable params in the graph.
        """
        if self._selected_param is not None:
            return [self._selected_param]
        targets = []
        selected_node = None
        for row in self._model.rows:
            if row.row_type == "node" and row.selected:
                selected_node = row.node_id
                break
        for row in self._model.rows:
            if row.row_type != "param":
                continue
            if selected_node is not None and row.node_id != selected_node:
                continue
            targets.append((row.node_id, row.param_name))
        return targets

    def add_keyframe_at_playhead(self) -> None:
        """Add (or update) a keyframe at the current frame for the targets."""
        frame = int(self._state.current_frame)
        for node_id, param_name in self._keyframe_targets():
            node = self._node_graph.get_node(node_id) if self._node_graph else None
            if self._is_color_param(node, param_name):
                self._write_color_keyframe_at(node_id, param_name, frame)
                continue
            value = None
            if self._value_provider is not None:
                try:
                    value = self._value_provider(node_id, param_name)
                except Exception:
                    value = None
            self._timeline_view.add_keyframe_at(
                node_id, param_name, frame, value_hint=value)

    def remove_keyframe_at_playhead(self) -> None:
        """Remove the keyframe at the current frame for the targets."""
        frame = int(self._state.current_frame)
        for node_id, param_name in self._keyframe_targets():
            node = self._node_graph.get_node(node_id) if self._node_graph else None
            if self._is_color_param(node, param_name):
                self._write_color_keyframe_at(
                    node_id, param_name, frame, remove=True)
                continue
            self._timeline_view.remove_keyframe_at(node_id, param_name, frame)

    def _update_range_label(self) -> None:
        total = max(0, self._state.end_frame - self._state.start_frame + 1)
        if hasattr(self, "_range_label"):
            full_text = (I18N.tr("keyframes.range", "Frames") +
                         f" {self._state.start_frame}-{self._state.end_frame}"
                         f" ({total})")
            text = f"{self._state.start_frame}-{self._state.end_frame}"
            if self._play_in is not None and self._play_out is not None:
                text += f" · I/O {self._play_in}-{self._play_out}"
                full_text += f" · In/Out {self._play_in}-{self._play_out}"
            # Keep the visible footer useful at a glance: the inclusive
            # frame count is part of the same readout, not tooltip-only.
            visible_text = full_text
            self._range_label.setText(visible_text)
            self._range_label.setToolTip(full_text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_ae_splitter_defaults()
        self._complete_deferred_fit()
        self._clamp_vertical_scroll()
        if self._empty_state_overlay is not None:
            self._empty_state_overlay.setGeometry(self.rect())
            if not self._composition_active:
                self._empty_state_overlay.raise_()

    def _apply_ae_splitter_defaults(self) -> None:
        """Give the layer/property side the same visual weight as AE.

        The first layout used a fixed 232 px tree regardless of dock width.
        The current compact default gives the property side roughly 32% of
        the panel, so the value and binding controls stay close to the names
        while the timeline keeps the larger editing area.
        once, after the widget has a real size, while leaving later user
        splitter drags untouched.
        """
        # A panel is often constructed at a small default size and then
        # stretched by its dock. If the first pass was constrained to the
        # narrow minimum, allow the later real-size pass to establish the
        # compact 32/68 ratio as well.
        if self._splitter_user_moved or self._splitter_applying_defaults:
            return
        total = self._splitter.width()
        if total <= 0:
            return
        handle = self._splitter.handleWidth()
        usable = max(1, total - handle)
        # Keep the property controls close to the labels; the original 32%
        # split left a wide, visually empty Name cell on large docks.
        ratio_left = int(round(usable * 0.30))
        if total < 900:
            ratio_left = max(timeline_layout.NARROW_LEFT_MIN, ratio_left)
            right_min = 420
        else:
            ratio_left = max(280, ratio_left)
            right_min = 420
        ratio_left = min(ratio_left, max(240, usable - right_min))
        target_sizes = [ratio_left, max(1, usable - ratio_left)]
        if self._splitter.sizes() != target_sizes:
            self._splitter_applying_defaults = True
            try:
                self._splitter.setSizes(target_sizes)
            finally:
                self._splitter_applying_defaults = False
        self._splitter_default_applied = True

    def _on_splitter_moved(self, _position: int, _index: int) -> None:
        """Preserve an intentional user splitter drag across window resizes."""
        if not self._splitter_applying_defaults:
            self._splitter_user_moved = True

    def _max_vertical_scroll(self) -> int:
        """Return the scroll range required by the visible rows only."""
        content_height = len(self._model.visible_rows()) * KEYFRAMES_ROW_H
        viewport_height = max(
            0, self._timeline_view.height() - KEYFRAMES_RULER_H)
        return max(0, content_height - viewport_height)

    def _set_vertical_scroll(self, value: int) -> int:
        """Apply one bounded vertical offset to both synchronized views."""
        # Headless/offscreen callers may exercise the panel before it has a
        # layout. Keep that legacy behavior; the visible panel is always
        # clamped using its actual viewport height.
        if not self.isVisible():
            raw = int(value)
            self._timeline_view.set_vertical_scroll(raw)
            self._tree_view.set_vertical_scroll(raw)
            return raw
        clamped = max(0, min(int(value), self._max_vertical_scroll()))
        self._timeline_view.set_vertical_scroll(clamped)
        self._tree_view.set_vertical_scroll(clamped)
        return clamped

    def _clamp_vertical_scroll(self) -> None:
        current = self._timeline_view._scroll_y
        clamped = self._set_vertical_scroll(current)
        if clamped != current:
            self._timeline_view.vertical_scroll_changed.emit(clamped)

    def _complete_deferred_fit(self) -> None:
        """Fit once the splitter has its final post-show width."""
        if (self._disposed or not getattr(self, "_fit_pending", False)
                or not self.isVisible()):
            return
        # During dock show, the panel can briefly report the splitter's
        # minimum timeline width before QSplitter applies the saved sizes.
        # Keep the request pending until that transient width is gone. The
        # next resizeEvent/showEvent will retry it; recursively queueing a
        # zero-delay timer here can otherwise spin forever when a dock keeps
        # reporting its minimum child width (notably during teardown and in
        # offscreen Qt tests).
        expected = (self._splitter.width() - self._tree_view.width() -
                    self._splitter.handleWidth())
        if (expected > 16 and
                self._timeline_view.width() + 1 < expected):
            return
        if self._timeline_view.width() > 16:
            self._fit_pending = False
            self.fit_time_range()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # A dock can be shown after it has already received its frame range.
        # Defer one event-loop turn so the splitter has its final width before
        # calculating pixels-per-frame.
        QTimer.singleShot(0, self._complete_deferred_fit)

    def wheelEvent(self, event) -> None:
        """Wheel navigation (AE-style).

        The timeline side handles its own wheel: plain/Ctrl+wheel zooms the
        time axis anchored at the cursor, Shift+wheel pans. Events that reach
        the panel (tree side / toolbar / Alt+wheel from the timeline) scroll
        vertically, Shift+wheel pans, Ctrl+wheel zooms at the playhead.
        """
        mods = event.modifiers()
        delta = event.angleDelta().y()
        if mods & Qt.KeyboardModifier.ControlModifier:
            # Zoom anchored at the playhead so it doesn't jump.
            factor = 1.15 if delta > 0 else (1.0 / 1.15)
            new_ppf = self._frame_mapper.pixels_per_frame * factor
            self._frame_mapper.set_zoom(new_ppf)
            self._timeline_view.refresh()
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            # Horizontal pan.
            self._frame_mapper.set_scroll(
                self._frame_mapper.scroll_offset - delta * 0.5)
            self._timeline_view.refresh()
        else:
            # Vertical scroll — sync both sides, but never allow a short list
            # to be scrolled out of view. This keeps the parameter names
            # stable when the user accidentally wheels over the left column.
            step = -delta // 2
            new_y = self._set_vertical_scroll(
                self._timeline_view._scroll_y + step)
            self._timeline_view.vertical_scroll_changed.emit(new_y)
