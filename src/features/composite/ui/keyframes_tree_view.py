"""KeyframesTreeView — the left node-tree side of the Keyframes panel.

A self-painted QWidget (not QTreeView) so it can stay vertically in sync
with the right-hand timeline view via a shared scroll offset, and so row
heights / lock icons / expand arrows match the timeline rows exactly.

P3 view (T28): the left side now paints the AE column layout from
``timeline_layout.LEFT_COLUMNS`` — Eye / Solo / Lock / Shy / Label / Name /
Value / Expression — instead of the old fixed ``_LOCK_COL_W`` /
``_NODE_LABEL_X`` constants. Widths resolve through
``timeline_layout.left_column_widths(width)`` so resizing the splitter or
the dock re-flows the columns without overlap (plan §3.3 / T36).

Column states are stored on each row (``row.eye``/``row.solo``/…) when the
host model populates them; absent fields are painted in their default
state so this view never crashes on a legacy model. The plan's
"shape encodes state, not colour alone" rule (§16.6) is enforced by giving
each active toggle a distinct glyph (open vs. filled, eye vs. dot).
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Dict, Optional, Tuple

from PyQt6.QtCore import Qt, pyqtSignal, QPoint, QPointF, QRectF, QEvent, QSignalBlocker
from PyQt6.QtGui import QColor, QDropEvent, QFontMetrics, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QSpinBox, QToolButton, QWidget,
)

from src.features.composite.core.media_drop_payload import accepts_layer_media_drop
from src.shared.i18n.i18n import I18N
from src.shared.ui.widgets.drag_value import DragValue
from src.shared.ui.style_sheet import C
from . import timeline_layout
from . import timeline_icons
from .keyframes_model import KEYFRAMES_ROW_H, KeyframesModel, KeyframesRow
from .scale_link import ScaleLinkState

logger = logging.getLogger(__name__)


_ROW_H = KEYFRAMES_ROW_H
_KEYFRAME_EDITOR_H = 34
# Header column labels (plan §16.4 — short caps so they fit 22 px columns).
# Hidden when the column's resolved width is too small to read the glyph.
_HEADER_LABELS: Dict[str, str] = {
    "eye": "",
    "solo": "",
    "lock": "",
    "shy": "",
    "label": "",
    "index": "#",
    "name": "Name",
    "keyframe": "",
    "value": "Value",
    "bind": "",
    "expr": "",
}


class _BindingDragOverlay(QWidget):
    """Top-most, mouse-transparent layer for AE pick-whip feedback.

    The tree owns real ``DragValue`` child widgets in the Value column. A
    normal parent ``paintEvent`` is rendered underneath those children, so a
    pick-whip line painted there can disappear behind a value box. Keeping a
    transparent child overlay makes the draw order explicit and stable.
    """

    def __init__(self, owner: "KeyframesTreeView") -> None:
        super().__init__(owner)
        self._owner = owner
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        self._owner._paint_binding_drag_overlay(painter)


class KeyframesTreeView(QWidget):
    """Left side: node/param rows with the AE column layout."""

    # Emitted when the widget scrolls vertically (shared with timeline side).
    vertical_scroll_changed = pyqtSignal(int)
    # Emitted when the user clicks a node row (selects the node).
    node_selected = pyqtSignal(str)
    # Emitted when the user clicks a Layer-mode row.
    layer_selected = pyqtSignal(str)
    layer_disclosure_requested = pyqtSignal(str)
    layer_menu_requested = pyqtSignal(str, QPoint)
    dimension_toggle_requested = pyqtSignal(str)
    layer_shy_changed = pyqtSignal(str, bool)
    layer_motion_blur_changed = pyqtSignal(str, bool)
    empty_layer_menu_requested = pyqtSignal(QPoint)
    layer_name_edited = pyqtSignal(str, str)
    layer_parent_menu_requested = pyqtSignal(str, QPoint)
    # Emitted when the user clicks a param row.
    param_selected = pyqtSignal(str, str)
    # Explicit graph/layer conversion commands.  The tree only emits intent;
    # the LayerStackController owns the mutation and undo boundary.
    promote_node_requested = pyqtSignal(str)
    create_matte_layer_requested = pyqtSignal(str)
    attach_node_requested = pyqtSignal(str)
    # P9: emitted when a row's lock state is toggled (node_id, locked).
    lock_changed = pyqtSignal(str, bool)
    # P9: emitted when a node's expansion is toggled (node_id, expanded).
    expand_changed = pyqtSignal(str, bool)
    # T28: emitted when a node/param row's eye (timeline visibility) toggles.
    eye_changed = pyqtSignal(str, bool)
    # Layers-mode Solo toggle. The payload is a layer id.
    solo_changed = pyqtSignal(str, bool)
    # Layers-mode row drag reorder request: (layer_id, insertion_index).
    layer_reorder_requested = pyqtSignal(str, int)
    # Delete the selected AE-style Layer row. The panel/page owns the graph
    # mutation; the tree only reports the user's intent.
    layer_delete_requested = pyqtSignal(str)
    # AE-style property-row context entry point. The panel owns the actual
    # expression editor/graph transition; the tree only reports the target.
    expression_edit_requested = pyqtSignal(str, str)
    # AE-style property controls. The tree reports intent; KeyframesPanel
    # performs the authoritative graph/track mutation.
    keyframe_toggle_requested = pyqtSignal(str, str)
    paired_keyframe_toggle_requested = pyqtSignal(str, tuple)
    binding_requested = pyqtSignal(str, str)
    value_changed_requested = pyqtSignal(str, str, str)
    # Value-column scrubbing: start carries the sampled base value, subsequent
    # events carry horizontal pixel delta. The panel applies ParamDef.step,
    # bounds and keyframe semantics centrally.
    # The provider returns numeric values for animatable parameters and the
    # raw label for option/bool rows, so the panel can scrub every visible
    # property without coercing strings to ``0``.
    value_drag_started = pyqtSignal(str, str, object)
    value_dragged = pyqtSignal(str, str, float)
    value_drag_finished = pyqtSignal(str, str)
    # The real Value-column controls reuse the shared Properties DragValue.
    # This signal carries the absolute value emitted by that control; the
    # legacy pixel-delta signals above remain as a headless-test fallback for
    # direct mouse events sent to this self-painted view.
    value_control_changed = pyqtSignal(str, str, float)
    # AE pick-whip drag results. The source row is the property/layer the user
    # started from; the destination row supplies the binding source.
    parameter_binding_dragged = pyqtSignal(str, str, str, str)
    parent_binding_dragged = pyqtSignal(str, str)
    # Alt-clicking a property diamond opens this compact AE-style editor row.
    keyframe_edit_requested = pyqtSignal(str, str)
    keyframe_edit_commit_requested = pyqtSignal(
        str, str, int, int, float, str)
    keyframe_edit_remove_requested = pyqtSignal(str, str, int)

    def __init__(self, model: KeyframesModel,
                 parent: Optional[QWidget] = None,
                 header_height: int = 0) -> None:
        super().__init__(parent)
        self._model = model
        self._scroll_y = 0
        self._hover_row = -1
        # The timeline side owns the ruler. The tree reserves the same header
        # height so both sides paint the same row index on the same y position.
        # Standalone tree users can keep the default zero-height header.
        self._header_height = max(0, int(header_height))
        self._header_time_display: Optional[QWidget] = None
        self._layer_header_tools: Optional[QWidget] = None
        self._header_range_label: Optional[QWidget] = None
        # P5/T49: optional provider (node_id, param_name) -> ExpressionStatus.
        # The host (KeyframesPanel) installs this so the tree can paint the
        # 4-state expression marker without holding a reference to the graph.
        # When None, no expression marker is drawn (preserves legacy look).
        self._expression_status_provider: Optional[
            Callable[[str, str], Optional[object]]] = None
        # T28: optional (node_id, param_name) -> value string provider. When
        # installed the Value column shows the resolved value at the playhead;
        # otherwise it paints a faint dash so the column reads as "no data"
        # instead of looking broken.
        self._value_provider: Optional[
            Callable[[str, Optional[str]], Optional[str]]] = None
        self._keyframe_status_provider: Optional[Callable[
            [str, str], object]] = None
        self._binding_provider: Optional[Callable[
            [str, Optional[str]], object]] = None
        self._value_editor: Optional[QLineEdit] = None
        self._value_editor_target: Optional[Tuple[str, str]] = None
        self._layer_name_editor: Optional[QLineEdit] = None
        self._layer_name_target: Optional[KeyframesRow] = None
        self._value_drag: Optional[Tuple[str, str, float, float]] = None
        self._binding_drag: Optional[Dict[str, object]] = None
        self._binding_drag_active = False
        self._binding_drag_pos = QPointF()
        self._binding_drag_hover_index = -1
        self._parent_binding_provider: Optional[Callable[[str], bool]] = None
        self._keyframe_drag: Optional[Tuple[str, str, float, object, bool]] = None
        self._current_frame = 0
        self._keyframe_editor: Optional[QFrame] = None
        self._keyframe_editor_target: Optional[Tuple[str, str, int]] = None
        self._value_control_provider: Optional[
            Callable[[str, str], Optional[Dict[str, object]]]] = None
        self._value_controls: Dict[Tuple[str, str], DragValue] = {}
        self._value_left_by_row: dict[tuple[str | None, str], int] = {}
        self._scale_link_state = ScaleLinkState(self)
        self._scale_buttons: dict[str, QToolButton] = {}
        # Keep enough room for the AE switch strip plus a readable Name
        # column. The compact resolver still handles standalone 180–239 px
        # test widgets without painting outside their bounds.
        self.setMinimumWidth(220)
        self.setMouseTracking(True)
        self._binding_drag_overlay = _BindingDragOverlay(self)
        self._binding_drag_overlay.setGeometry(self.rect())
        self._binding_drag_overlay.hide()
        # ── Media-drop support (AE/Nuke hybrid, plan §5) ──
        # The tree accepts media-pool drags only when the host installs a
        # ``_media_drop_handler``.  The handler receives
        # ``(mime_data, target_layer_id, insert_before)`` and is expected
        # to resolve payloads + call the LayerStackController; keeping the
        # resolution outside the view avoids coupling it to EditProject.
        self.setAcceptDrops(True)
        self._media_drop_handler: Optional[
            Callable[[object, Optional[str], bool], bool]] = None
        # ``_drop_indicator`` is (row_index, before?) painted as an insertion
        # line; None hides it.  Updated in dragMoveEvent, cleared on leave/drop.
        self._drop_indicator: Optional[Tuple[int, bool]] = None
        # Lightweight in-widget Layer reorder gesture. MediaPool drops use
        # Qt's drag/drop events; layer rows are reordered without starting a
        # second application-wide MIME drag, which keeps parameter controls
        # and the shared scroll model stable.
        self._layer_drag_candidate: Optional[Tuple[str, float, float]] = None
        self._layer_drag_active = False
        self._selected_layer_id: Optional[str] = None
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_expression_status_provider(self, provider) -> None:
        """Install the (node_id, param_name) -> ExpressionStatus callback.

        Passing None disables the marker and restores the pre-P5 paint path.
        """
        self._expression_status_provider = provider
        self.update()

    def set_value_provider(self, provider) -> None:
        """Install the (node_id, param_name|None) -> value-string callback.

        The Value column (T28) is blank by default because the legacy panel
        had no live value source. Hosts that want to populate it install a
        provider here; pass None to revert to the dash placeholder.
        """
        self._value_provider = provider
        self.update()

    def set_value_control_provider(self, provider) -> None:
        """Install metadata for the shared Properties-style value control.

        The callback returns ``min_val``, ``max_val``, ``step``, ``decimals``
        and the current-frame ``value`` for a numeric parameter.
        """
        self._value_control_provider = provider
        self._sync_value_controls()
        self.update()

    def set_keyframe_status_provider(self, provider) -> None:
        """Install a provider returning ``animated``/``at_current`` state."""
        self._keyframe_status_provider = provider
        self.update()

    def set_binding_provider(self, provider) -> None:
        """Install a provider for parameter or parent pick-whip state."""
        self._binding_provider = provider
        self.update()

    def set_parent_binding_provider(self, provider) -> None:
        """Install ``node_id -> supports parent transform`` capability lookup."""
        self._parent_binding_provider = provider
        self._binding_drag_hover_index = -1
        self._binding_drag_overlay.update()
        self.update()

    def set_current_frame(self, frame: int) -> None:
        """Keep the left-side editor and Alt-click state on the playhead."""
        self._current_frame = int(frame)
        self._sync_keyframe_editor()
        self.update()

    def _safe_value_text(self, node_id: str,
                         param_name: Optional[str]) -> Optional[str]:
        """Call the value provider with protection on every path.

        AUD-10302: the paint path already guarded provider failures, but the
        mouse-press and double-click paths called the same host callback
        bare — a raising provider crashed the Qt event handler. All call
        sites now share this guard.
        """
        provider = self._value_provider
        if provider is None:
            return None
        try:
            return provider(node_id, param_name)
        except Exception:
            # Host-owned callback; must not escape into the event loop, but
            # the failure is logged so it stays diagnosable.
            logger.debug("Value provider failed for %s/%s",
                         node_id, param_name, exc_info=True)
            return None

    def set_header_time_display(self, display: Optional[QWidget]) -> None:
        """Place the shared current-time display in the property header.

        The header is painted by this view, so the display is kept as a child
        widget and repositioned from the same resolved column geometry. The
        Name/Value captions are hidden while it is installed; the display
        becomes the single, more useful header readout.
        """
        if self._header_time_display is display:
            self._position_header_time_display()
            return
        if self._header_time_display is not None:
            self._header_time_display.hide()
        self._header_time_display = display
        if display is not None:
            display.setParent(self)
            display.show()
            display.raise_()
        self._position_header_time_display()
        self.update()

    def set_header_height(self, height: int) -> None:
        if height == self._header_height:
            return
        self._header_height = height
        self._position_header_time_display()
        self.refresh()

    def set_layer_header_tools(self, tools: QWidget) -> None:
        """Place the Layer controls beside the time readout in the shared header."""
        self._layer_header_tools = tools
        self._position_header_time_display()

    def set_header_range_label(self, label: Optional[QWidget]) -> None:
        """Place the compact Frames/In-Out summary beside the time display."""
        if self._header_range_label is not None:
            self._header_range_label.hide()
        self._header_range_label = label
        if label is not None:
            label.setParent(self)
            label.show()
        self._position_header_time_display()
        self.update()

    def _position_header_time_display(self) -> None:
        display = self._header_time_display
        if display is None or not self._header_height:
            return
        tools = self._layer_header_tools
        if tools is not None:
            tools.setVisible(self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS)
        if self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS:
            width = max(0, self.width() - 8)
            if tools is not None:
                width = min(width, timeline_layout.LAYER_HEADER_TIMECODE_WIDTH)
                tools_x = 4 + width
                tools.setGeometry(tools_x, 0, max(0, self.width() - tools_x - 4),
                                  self._header_height - 1)
            display.setGeometry(4, 0, width, self._header_height - 1)
            display.show()
            return
        geom = self._column_geometry()
        name_x0, _ = geom.get("name", (0, 0))
        _, value_x1 = geom.get("value", (name_x0, name_x0))
        span_width = max(0, value_x1 - name_x0)
        display_width = min(display.width(), max(0, span_width - 8))
        if display_width <= 0:
            display.hide()
            return
        x = name_x0 + max(4, (span_width - display_width) // 2)
        display.setGeometry(x, 0, display_width, self._header_height)
        display.show()
        label = self._header_range_label
        if label is not None:
            label_x = x + display_width + 2
            label_width = max(0, name_x0 + span_width - label_x - 4)
            label.setGeometry(label_x, 0, label_width, self._header_height)
            label.setVisible(label_width >= 24)

    def open_keyframe_editor(self, node_id: str, param_name: str,
                             frame: int, value: float,
                             interpolation: str = "linear",
                             min_value: float = -1_000_000.0,
                             max_value: float = 1_000_000.0,
                             step: float = 0.01,
                             decimals: int = 3,
                             integer: bool = False) -> None:
        """Show the inline keyframe row requested by an Alt-click.

        The row is an overlay immediately below the property row. Keeping it
        as an overlay means the left and right canvases retain their shared
        24px row grid while the editor remains directly under the property,
        just like AE's expanded property controls.
        """
        self._close_keyframe_editor()
        editor = QFrame(self)
        editor.setObjectName("KeyframesInlineKeyframeEditor")
        editor.setFrameShape(QFrame.Shape.StyledPanel)
        editor.setStyleSheet(
            f"QFrame#KeyframesInlineKeyframeEditor {{"
            f" background: {timeline_layout.resolve_token('header_bg')};"
            f" border: 1px solid {timeline_layout.resolve_token('accent_orange')}; }}"
            f"QLabel {{ color: {timeline_layout.resolve_token('text_hint')};"
            f" font-size: 9px; }}"
            f"QSpinBox, QDoubleSpinBox, QComboBox {{"
            f" background: {timeline_layout.resolve_token('timeline_bg')};"
            f" color: {timeline_layout.resolve_token('text_primary')};"
            f" border: 1px solid {timeline_layout.resolve_token('border')};"
            f" padding: 0 2px; }}"
            f"QPushButton {{ background: transparent; color: "
            f"{timeline_layout.resolve_token('text_secondary')}; border: none; }}"
            f"QPushButton:hover {{ color: {timeline_layout.resolve_token('accent_orange')}; }}"
        )
        layout = QHBoxLayout(editor)
        layout.setContentsMargins(5, 2, 3, 2)
        layout.setSpacing(3)
        layout.addWidget(QLabel(I18N.tr("keyframes.kf_label", "KF")))
        frame_box = QSpinBox(editor)
        frame_box.setRange(-1_000_000, 1_000_000)
        frame_box.setValue(int(frame))
        frame_box.setFixedWidth(68)
        frame_box.setToolTip(I18N.tr("keyframes.kf_frame_tooltip", "Keyframe frame"))
        layout.addWidget(frame_box)
        value_label = QLabel(I18N.tr("keyframes.col_value", "Value"), editor)
        layout.addWidget(value_label)
        if integer:
            value_box = QSpinBox(editor)
            value_box.setRange(int(min_value), int(max_value))
            value_box.setValue(int(round(value)))
        else:
            value_box = QDoubleSpinBox(editor)
            value_box.setRange(float(min_value), float(max_value))
            value_box.setDecimals(max(0, int(decimals)))
            value_box.setSingleStep(max(1e-9, float(step)))
            value_box.setValue(float(value))
        value_box.setFixedWidth(82)
        layout.addWidget(value_box)
        interp_box = QComboBox(editor)
        interp_box.addItems(["linear", "bezier", "hold", "ease_in",
                             "ease_out", "ease_in_out"])
        interp_box.setCurrentText(str(interpolation or "linear"))
        interp_box.setFixedWidth(84)
        interp_box.setToolTip(I18N.tr("keyframes.interpolation", "Interpolation"))
        layout.addWidget(interp_box)
        layout.addStretch(1)
        apply_btn = QPushButton(I18N.tr("keyframes.apply", "Apply"), editor)
        apply_btn.setFixedWidth(42)
        remove_btn = QPushButton("×", editor)
        remove_btn.setFixedWidth(20)
        remove_btn.setToolTip(I18N.tr("keyframes.remove_kf", "Remove keyframe"))
        close_btn = QPushButton("▴", editor)
        close_btn.setFixedWidth(20)
        close_btn.setToolTip(I18N.tr("keyframes.close_editor", "Close keyframe editor"))
        layout.addWidget(apply_btn)
        layout.addWidget(remove_btn)
        layout.addWidget(close_btn)

        target = (str(node_id), str(param_name), int(frame))
        self._keyframe_editor = editor
        self._keyframe_editor_target = target

        def commit() -> None:
            if self._keyframe_editor_target is None:
                return
            self.keyframe_edit_commit_requested.emit(
                target[0], target[1], target[2], int(frame_box.value()),
                float(value_box.value()), str(interp_box.currentText()))

        apply_btn.clicked.connect(commit)
        frame_box.editingFinished.connect(commit)
        value_box.editingFinished.connect(commit)
        interp_box.currentTextChanged.connect(lambda _text: commit())
        remove_btn.clicked.connect(lambda: self.keyframe_edit_remove_requested.emit(
            target[0], target[1], target[2]))
        close_btn.clicked.connect(self._close_keyframe_editor)
        editor.show()
        self._sync_keyframe_editor()

    def _close_keyframe_editor(self) -> None:
        editor = self._keyframe_editor
        self._keyframe_editor = None
        self._keyframe_editor_target = None
        if editor is not None:
            editor.deleteLater()

    def _sync_keyframe_editor(self) -> None:
        """Position the inline editor under its visible property row."""
        editor = self._keyframe_editor
        target = self._keyframe_editor_target
        if editor is None or target is None:
            return
        rows = self._model.visible_rows()
        row_index = next((i for i, row in enumerate(rows)
                          if row.node_id == target[0]
                          and row.param_name == target[1]), -1)
        if row_index < 0:
            editor.hide()
            return
        y = self._header_height + row_index * _ROW_H - self._scroll_y + _ROW_H
        editor.setGeometry(4, y, max(80, self.width() - 8), _KEYFRAME_EDITOR_H)
        editor.setVisible(-_KEYFRAME_EDITOR_H < y < self.height())

    # ── model binding ──

    def refresh(self) -> None:
        if self._value_editor is not None:
            self._finish_value_edit(commit=False)
        self._sync_value_controls()
        self._sync_keyframe_editor()
        self._position_layer_name_editor()
        self.update()

    def sync_parameter_values(self, node_id: str, param_names: tuple[str, ...]) -> None:
        """Refresh existing controls, reflowing the row when a value needs more space."""
        if "pivot_mode" in param_names:
            self._sync_value_controls()
            self.update()
            return
        provider = self._value_control_provider
        if provider is not None:
            for param_name in param_names:
                control = self._value_controls.get((node_id, param_name))
                if control is None:
                    continue
                meta = provider(node_id, param_name)
                if meta is not None:
                    factor, _ = self._value_units(param_name)
                    slot_width = control.width() + 6
                    text_width = self._numeric_slot_width(param_name, meta)
                    previous_width = self._numeric_slot_width(
                        param_name, dict(meta, value=control.value() / factor))
                    if text_width > slot_width or previous_width >= slot_width > text_width:
                        self._sync_value_controls()
                        break
                    with QSignalBlocker(control):
                        control.setValue(float(meta["value"]) * factor)
        self.update()

    def set_vertical_scroll(self, value: int) -> None:
        if value != self._scroll_y:
            self._scroll_y = value
            self._sync_value_controls()
            self._sync_keyframe_editor()
            self._position_layer_name_editor()
            self.update()

    def set_selected_layer_id(self, layer_id: Optional[str]) -> None:
        """Keep keyboard deletion tied to the currently selected Layer row."""
        self._selected_layer_id = str(layer_id) if layer_id else None
        self._position_layer_name_editor()
        for row in self._model.visible_rows():
            if row.row_type == "layer":
                row.selected = bool(
                    self._selected_layer_id
                    and row.layer_id == self._selected_layer_id)
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._binding_drag_overlay.setGeometry(self.rect())
        self._position_header_time_display()
        self._sync_value_controls()
        self._sync_keyframe_editor()
        self._position_layer_name_editor()

    def open_layer_name_editor(self, layer_id: str) -> bool:
        """Edit the selected layer name in its Name cell."""
        found = self._find_renamable_layer_row(layer_id)
        if found is None:
            return False
        index, row = found
        if row is self._layer_name_target and self._layer_name_editor is not None:
            self._layer_name_editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
            return True

        self._close_layer_name_editor()
        self._finish_value_edit(commit=False)
        viewport_height = self.height() - self._header_height
        if viewport_height < _ROW_H:
            return False
        row_top = index * _ROW_H
        scroll = max(row_top + _ROW_H - viewport_height, min(self._scroll_y, row_top))
        if scroll != self._scroll_y:
            self.set_vertical_scroll(scroll)
            self.vertical_scroll_changed.emit(scroll)

        editor = QLineEdit(row.label, self)
        editor.setObjectName("KeyframesInlineLayerNameEditor")
        editor.setAccessibleName(I18N.tr("keyframes.layer_rename", "Rename layer"))
        editor.setStyleSheet(
            f"QLineEdit {{ background: {C.BG_INPUT}; color: {C.TEXT_PRIMARY}; "
            f"border: 1px solid {C.ORANGE}; padding: 0 3px; }}")
        editor.installEventFilter(self)
        self._layer_name_editor = editor
        self._layer_name_target = row
        if not self._position_layer_name_editor():
            return False
        editor.show()
        editor.raise_()
        editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        editor.selectAll()
        return True

    def _find_renamable_layer_row(self, layer_id: Optional[str]) -> Optional[Tuple[int, KeyframesRow]]:
        if (not layer_id or layer_id != self._selected_layer_id
                or self._model.view_mode != KeyframesModel.VIEW_MODE_LAYERS):
            return None
        return next(((index, row) for index, row in enumerate(self._model.visible_rows())
                     if row.row_type == "layer" and row.layer_id == layer_id and not row.locked), None)

    def _position_layer_name_editor(self) -> bool:
        editor, target = self._layer_name_editor, self._layer_name_target
        if editor is None or target is None:
            return False
        found = self._find_renamable_layer_row(target.layer_id)
        if found is None or found[1] is not target:
            self._close_layer_name_editor()
            return False
        y = self._header_height + found[0] * _ROW_H - self._scroll_y
        x0, x1 = self._column_geometry()["name"]
        x = x0 + 19
        width = x1 - x - 2
        if y < self._header_height or y + _ROW_H > self.height() or width < 16:
            self._close_layer_name_editor()
            return False
        editor.setGeometry(x, y + 2, width, _ROW_H - 4)
        return True

    def _commit_layer_name(self) -> None:
        editor, target = self._layer_name_editor, self._layer_name_target
        if editor is None or target is None:
            return
        found = self._find_renamable_layer_row(target.layer_id)
        name = editor.text().strip()
        self._close_layer_name_editor(restore_focus=True)
        if found is None or found[1] is not target or not target.layer_id:
            return
        if name and name != target.label:
            self.layer_name_edited.emit(target.layer_id, name)

    def _close_layer_name_editor(self, *, restore_focus: bool = False) -> None:
        editor = self._layer_name_editor
        self._layer_name_editor = None
        self._layer_name_target = None
        if editor is not None:
            editor.hide()
            editor.deleteLater()
            if restore_focus:
                self.setFocus(Qt.FocusReason.OtherFocusReason)

    def _sync_value_controls(self) -> None:
        """Create and place shared DragValue controls over visible rows."""
        provider = self._value_control_provider
        self._value_left_by_row.clear()
        if provider is None:
            for button in self._scale_buttons.values():
                button.hide()
            for control in self._value_controls.values():
                control.hide()
            self._binding_drag_overlay.raise_()
            return

        visible_keys: set[Tuple[str, str]] = set()
        visible_scale_links: set[str] = set()
        geom = self._column_geometry()
        value_x0, value_x1 = geom.get("value", (0, 0))
        cell_w = value_x1 - value_x0
        if cell_w < 16:
            for button in self._scale_buttons.values():
                button.hide()
            for control in self._value_controls.values():
                control.hide()
            self._binding_drag_overlay.raise_()
            return

        for index, row in enumerate(self._model.visible_rows()):
            y = self._header_height + index * _ROW_H - self._scroll_y
            if y < self._header_height or y >= self.height():
                continue
            params = (row.param_name,) if row.param_name else ()
            if row.row_type == "group" and not row.expanded and cell_w >= 68:
                params = row.component_params
            if not params:
                continue
            scale = (row.layer_id is not None and row.node_id not in visible_scale_links
                     and (params == ("scale_x", "scale_y")
                          or (row.param_name in ("scale_x", "scale_y")
                              and self._model.dimension_group(row) is not None)))
            cells = []
            right = geom["bind"][1] if len(params) > 1 else value_x1
            for param_name in params:
                key = (row.node_id, param_name)
                if key in visible_keys:
                    continue
                try:
                    meta = provider(*key)
                except (AttributeError, KeyError, TypeError, ValueError):
                    continue
                if meta is not None:
                    minimum = max(44, cell_w) if len(params) == 1 else 44
                    cells.append((key, meta, max(minimum, self._numeric_slot_width(param_name, meta))))
            if not cells:
                continue
            left = right - sum(width for _, _, width in cells) - (20 if scale else 0)
            self._value_left_by_row[(row.layer_id, row.row_id)] = left
            x = left
            if scale:
                visible_scale_links.add(row.node_id)
                button = self._scale_buttons.get(row.node_id)
                if button is None:
                    button = self._scale_link_state.make_button(row.node_id)
                    button.setParent(self)
                    button.setObjectName("KeyframesScaleLink")
                    self._scale_buttons[row.node_id] = button
                button.move(x, y + (_ROW_H - button.height()) // 2)
                button.setEnabled(not row.locked)
                button.show()
                x += 20
            for key, meta, width in cells:
                if self._place_value_control(key, meta, x, y, width, row.locked):
                    visible_keys.add(key)
                x += width

        # AUD-10305: rows that scrolled out of view used to keep their
        # DragValue child alive forever — controls accumulated without bound
        # over a long scrolling session. Destroy the stale ones instead of
        # only hiding them; re-scrolling simply recreates a fresh control.
        for key in [key for key in self._value_controls
                    if key not in visible_keys]:
            control = self._value_controls.pop(key)
            control.hide()
            control.deleteLater()
        for node_id in self._scale_buttons.keys() - visible_scale_links:
            button = self._scale_buttons.pop(node_id)
            button.hide()
            button.deleteLater()
        # New value controls are shown after the overlay was constructed;
        # restore the intended stacking order after every sync.
        self._binding_drag_overlay.raise_()

    def set_scale_link_state(self, state: ScaleLinkState) -> None:
        self._scale_link_state = state
        for button in self._scale_buttons.values():
            button.hide()
            button.deleteLater()
        self._scale_buttons.clear()
        self._sync_value_controls()

    def scale_is_linked(self, node_id: str) -> bool:
        """The proportional edit applies to the combined scale row."""
        button = self._scale_buttons.get(node_id)
        return button is not None and not button.isHidden() and button.isChecked()

    def _numeric_slot_width(self, param_name: str, meta: Dict[str, object]) -> int:
        factor, suffix = self._value_units(param_name)
        decimals = 1 if factor != 1.0 else int(meta.get("decimals", 2))
        number = float(meta.get("value", 0.0)) * factor
        font = self.font()
        font.setPixelSize(11)
        font.setBold(False)
        metrics = QFontMetrics(font)
        return metrics.horizontalAdvance(f"{number:.{decimals}f}{suffix}") + 12

    def _place_value_control(self, key: Tuple[str, str], meta: Optional[Dict[str, object]],
                             x: int, y: int, width: int, locked: bool) -> bool:
        if not isinstance(meta, dict):
            return False
        try:
            factor, suffix = self._value_units(key[1])
            minimum = float(meta.get("min_val", 0.0)) * factor
            maximum = float(meta.get("max_val", 1.0)) * factor
            value = float(meta.get("value", 0.0)) * factor
            decimals = int(meta.get("decimals", 2))
            if factor != 1.0:
                decimals = 1
            step = float(meta.get("step", 0.01)) * factor
        except (TypeError, ValueError):
            return False

        control = self._value_controls.get(key)
        if control is None:
            node_id, param_name = key
            control = DragValue("", minimum, maximum, value, decimals=decimals, parent=self)
            control.setObjectName("KeyframesSharedDragValue")
            self._set_flat_value_control_style(control)
            control.installEventFilter(self)
            control.interaction_started.connect(
                lambda: self.value_drag_started.emit(
                    node_id, param_name, control.value() / self._value_units(param_name)[0]))
            control.valueChanged.connect(
                lambda displayed: self.value_control_changed.emit(
                    node_id, param_name, float(displayed) / self._value_units(param_name)[0]))
            control.interaction_ended.connect(
                lambda: self.value_drag_finished.emit(node_id, param_name))
            self._value_controls[key] = control
        with QSignalBlocker(control):
            control.setRange(minimum, maximum)
            control.setDecimals(decimals)
            control.setSingleStep(step)
            control.setSuffix(suffix)
            control.setValue(value)
        cell_width = max(1, width - 6)
        control.setAccessibleName(key[1])
        control.setEnabled(not locked and not bool(meta.get("read_only")))
        control.setFixedValueWidth(cell_width)
        control.setGeometry(x + 3, y, cell_width, _ROW_H)
        control.show()
        return True

    def _value_units(self, param_name: str) -> Tuple[float, str]:
        if self._model.view_mode != KeyframesModel.VIEW_MODE_LAYERS:
            return 1.0, ""
        if param_name in ("scale_x", "scale_y", "opacity"):
            return 100.0, "%"
        return 1.0, "°" if param_name == "rotation" else ""

    @staticmethod
    def _set_flat_value_control_style(control: DragValue) -> None:
        """Render Keyframes values as AE-style text instead of input wells."""
        control._value_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        css = "font-size: 10px; font-weight: normal; background: transparent; border: none; padding: 0px;"
        normal = f"QLabel {{ color: {timeline_layout.resolve_token('text_label')}; {css} }}"
        active = f"QLabel {{ color: {timeline_layout.resolve_token('text_primary')}; {css} }}"
        disabled = f"QLabel {{ color: {timeline_layout.resolve_token('text_hint')}; {css} }}"
        control.set_value_styles(normal, active, disabled)
        control._edit.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        control._edit.setStyleSheet(
            f"QLineEdit {{ color: {timeline_layout.resolve_token('text_primary')};"
            " font-size: 10px; background: transparent; border: 0px;"
            " padding: 0px; }")

    def eventFilter(self, watched, event) -> bool:
        """Scope inline editor input and preserve Value-cell property selection."""
        if watched is self.__dict__.get("_layer_name_editor"):
            if event.type() == QEvent.Type.ShortcutOverride and event.key() in (
                    Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
                event.accept()
                return True
            if event.type() == QEvent.Type.KeyPress:
                if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._commit_layer_name()
                    event.accept()
                    return True
                if event.key() == Qt.Key.Key_Escape:
                    self._close_layer_name_editor(restore_focus=True)
                    event.accept()
                    return True
            if event.type() == QEvent.Type.FocusOut:
                self._close_layer_name_editor()
        if (event.type() == QEvent.Type.MouseButtonPress
                and watched in self.__dict__.get("_value_controls", {}).values()):
            for (node_id, param_name), control in self._value_controls.items():
                if control is watched:
                    self.param_selected.emit(node_id, param_name)
                    break
        return super().eventFilter(watched, event)

    # ── column geometry (T28) ──

    def _column_geometry(self) -> Dict[str, Tuple[int, int]]:
        """Resolve the AE column x-ranges for the current widget width.

        Returns ``{name: (x_start, x_end)}`` keyed by the column ids in
        ``timeline_layout.LEFT_COLUMNS``. Cached for one paint cycle; callers
        must not retain it across resizes.
        """
        return {name: (left, right) for name, left, right in timeline_layout.column_x_offsets(
            self.width(), layers_mode=self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS)}

    def _row_geometry(self, row: KeyframesRow,
                      columns: Dict[str, Tuple[int, int]]) -> Dict[str, Tuple[int, int]]:
        """Use the switch strip for indented property names on child rows."""
        if row.layer_id is None or row.row_type == "layer":
            return columns
        geom = dict(columns)
        left = 23
        right = geom["value"][0]
        right = min(right, self._value_left_by_row.get((row.layer_id, row.row_id), right))
        if row.row_type == "param" or row.component_params:
            left += self._row_indent(row)
            key_end = min(left + 20, right)
            geom["keyframe"] = (min(left, key_end), key_end)
            left = key_end
        geom["name"] = (left, right)
        return geom

    # ── painting ──

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        w = self.width()
        h = self.height()
        geom = self._column_geometry()

        # Background
        p.fillRect(0, 0, w, h, QColor(timeline_layout.resolve_token(
            "left_panel_bg")))

        # Header: AE column titles aligned with the body columns. Painted only
        # when the host reserved a header band (the panel always does; the
        # standalone tree in unit tests can pass header_height=0).
        if self._header_height:
            self._draw_header(p, geom)

        rows = self._model.visible_rows()
        if not rows:
            self._draw_empty(p, w, h)
            return

        # Rows
        for i, row in enumerate(rows):
            y = self._header_height + i * _ROW_H - self._scroll_y
            if y + _ROW_H < 0 or y > h:
                continue
            self._draw_row(p, row, y, w, self._row_geometry(row, geom))

        # AE-style media-drop insertion line (plan §5.2).  Drawn last so it
        # sits above row content; harmless no-op when no drop is in flight.
        self._paint_drop_indicator(p)

    def _parent_binding_supported(self, node_id: str) -> bool:
        provider = self._parent_binding_provider
        if provider is None:
            return False
        try:
            return bool(provider(str(node_id)))
        except Exception:
            return False

    def _binding_target_supported(self, row) -> bool:
        """Return whether the hovered row is a valid pick-whip target type."""
        drag = self._binding_drag
        if drag is None:
            return False
        source_param = str(drag.get("param_name", ""))
        if source_param:
            # Parameter binding is independent from node parent capability.
            return row.row_type == "param" and bool(row.param_name)
        return (row.row_type in ("node", "layer")
                and self._parent_binding_supported(row.node_id)
                and self._parent_binding_supported(str(drag.get("node_id", ""))))

    def _binding_target_is_source(self, row) -> bool:
        drag = self._binding_drag
        return bool(drag is not None
                    and row.node_id == drag.get("node_id")
                    and (row.param_name or "") == drag.get("param_name"))

    def _paint_binding_drag_overlay(self, p: QPainter) -> None:
        """Paint pick-whip line/target tint above all value child widgets."""
        if not self._binding_drag_active or self._binding_drag is None:
            return
        rows = self._model.visible_rows()
        idx = self._binding_drag_hover_index
        if 0 <= idx < len(rows):
            row = rows[idx]
            if self._binding_target_supported(row):
                y = self._header_height + idx * _ROW_H - self._scroll_y
                tint = (QColor(220, 70, 70, 52)
                        if self._binding_target_is_source(row)
                        else QColor(40, 180, 100, 52))
                p.fillRect(0, y, self.width(), _ROW_H, tint)

        origin = self._binding_drag["origin"]
        p.save()
        pen = QPen(QColor(timeline_layout.resolve_token("accent_blue")))
        pen.setWidth(2)
        p.setPen(pen)
        p.drawLine(origin, self._binding_drag_pos)
        p.setBrush(QColor(timeline_layout.resolve_token("accent_blue")))
        p.drawEllipse(self._binding_drag_pos, 3.0, 3.0)
        p.restore()

    def _refresh_binding_drag_overlay(self) -> None:
        overlay = self._binding_drag_overlay
        overlay.setVisible(bool(self._binding_drag_active))
        if self._binding_drag_active:
            overlay.raise_()
        overlay.update()

    def _draw_header(self, p: QPainter,
                     geom: Dict[str, Tuple[int, int]]) -> None:
        """Paint the column-header band above the rows (plan §16.4)."""
        h = self._header_height
        w = self.width()
        layer_readout = (self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS
                         and self._header_time_display is not None)
        bg = C.TIMELINE_TIMECODE_BG if layer_readout else timeline_layout.resolve_token("header_bg")
        p.fillRect(0, 0, w, h, QColor(bg))
        if layer_readout:
            p.setPen(QPen(QColor(timeline_layout.resolve_token("grid_minor"))))
            p.drawLine(0, h - 1, w, h - 1)
            return
        # Per-column labels. Skip the toggle columns (eye/solo/lock/shy/label/
        # expr) — those have no text header in AE; their meaning is conveyed
        # by the glyphs painted on each row. Name/Value/Index get short caps.
        p.setPen(QPen(QColor(timeline_layout.resolve_token("text_hint"))))
        font = p.font()
        font.setPointSize(8)
        font.setBold(False)
        p.setFont(font)
        for name, label in _HEADER_LABELS.items():
            if self._header_time_display is not None and name in ("name", "value"):
                continue
            if not label or name not in geom:
                continue
            x0, x1 = geom[name]
            if x1 - x0 < 16:
                continue  # column too narrow for a header glyph
            if name in ("name", "value"):
                label = I18N.tr(f"keyframes.col_{name}", label)
            p.drawText(QRectF(x0, 0, x1 - x0, h),
                       Qt.AlignmentFlag.AlignVCenter
                       | Qt.AlignmentFlag.AlignHCenter
                       | Qt.TextFlag.TextSingleLine,
                       label)
        p.setPen(QPen(QColor(timeline_layout.resolve_token("grid_minor"))))
        p.drawLine(0, h - 1, w, h - 1)

    def _draw_row(self, p: QPainter, row, y: int, w: int,
                  geom: Dict[str, Tuple[int, int]]) -> None:
        rect_h = _ROW_H
        # Selection / hover background spans the full row (plan §3.4 — the
        # whole row carries the selection highlight, not just the name cell).
        layers_mode = self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS
        if layers_mode and row.row_type == "group" and not row.component_params:
            p.fillRect(0, y, w, rect_h, QColor(C.BG_ACTIVE))
        elif row.row_type in ("node", "layer"):
            p.fillRect(0, y, w, rect_h,
                       QColor(timeline_layout.resolve_token("layer_row_bg")))
        if row.selected:
            p.fillRect(0, y, w, rect_h,
                       QColor(timeline_layout.resolve_token("timeline_bg")))
            if layers_mode:
                p.fillRect(0, y, 2, rect_h, QColor(C.ORANGE))
        elif self._hover_row == id(row):
            p.fillRect(0, y, w, rect_h,
                       QColor(timeline_layout.resolve_token("grid_minor")))

        # Toggle columns (eye/solo/lock/shy). Each one reads its flag from the
        # row's KeyframesRow fields (layer_enabled/layer_solo/layer_shy,
        # populated from layer.enabled/solo/shy by the model) so the painted
        # state reflects the real layer state. ``getattr`` defaults keep the
        # view alive on a legacy model that never populated them.
        # AUD-10301: these used to read non-existent ``row.eye``/``row.solo``/
        # ``row.shy`` attributes, so the icons always painted their defaults
        # and never reflected (nor updated) the model's layer state.
        # AE layer switches belong to the layer row, not every child
        # property. Repeating them on Translate X/Y/Scale/etc. made the old
        # panel look like the name text was colliding with a second layer.
        if row.row_type == "layer" or (not layers_mode and row.row_type == "node"):
            self._draw_eye(p, geom, y, getattr(row, "layer_enabled", True))
            self._draw_solo(p, geom, y, getattr(row, "layer_solo", False))
            self._draw_lock(p, geom, y, row.locked)
            if row.row_type == "layer":
                self._draw_layer_switch(p, geom["shy"], y, "shy", row.layer_shy,
                                        not row.locked)
                self._draw_layer_switch(p, geom["motion_blur"], y, "motion_blur",
                                        bool(row.layer_motion_blur),
                                        row.layer_motion_blur is not None and not row.locked)

            # Label column = node color swatch. Params inherit the node
            # color through their timeline track and need no second swatch.
            self._draw_color_swatch(p, geom, y, row.node_color)
        if row.row_type == "layer":
            x0, x1 = geom["index"]
            if x1 > x0:
                p.setPen(QColor(timeline_layout.resolve_token("text_secondary")))
                p.drawText(QRectF(x0, y, x1 - x0, rect_h), Qt.AlignmentFlag.AlignCenter,
                           str(row.layer_index or ""))

        # Name column: expand arrow (nodes) + label. Params indent inside the
        # name cell so they read as children (plan §3.3 "node params indent
        # 18-24 px, channel rows another 16 px").
        self._draw_name(p, geom, y, row)

        # Value column. Empty by default — the host can install a value
        # provider; until then we paint a faint dash so the column reads as
        # "no live value" rather than blank space.
        self._draw_value(p, geom, y, row)

        if row.row_type == "param":
            self._draw_keyframe_control(p, geom, y, row)
            self._draw_binding_control(p, geom, y, row)
        elif row.row_type == "group" and row.component_params and not row.expanded:
            self._draw_keyframe_control(p, geom, y, row)
        elif row.row_type in ("node", "layer"):
            # The same pick-whip cell is the layer parent selector on a node
            # row, matching AE's parent & link column.
            self._draw_binding_control(p, geom, y, row)
            if row.row_type == "layer" and self._layer_parent_visible(row):
                self._draw_layer_parent(p, geom["value"], y, row)

        # P5/T49: 4-state expression marker in the expr column (param rows).
        if row.row_type == "param":
            self._draw_expression_status(p, geom, y, row)

        # Row separator
        p.setPen(QPen(QColor(timeline_layout.resolve_token("grid_minor"))))
        p.drawLine(0, y + rect_h - 1, w, y + rect_h - 1)

    # ── per-column painters ──

    def _draw_eye(self, p: QPainter,
                  geom: Dict[str, Tuple[int, int]],
                  y: int, visible: bool) -> None:
        x0, x1 = geom.get("eye", (0, 0))
        if x1 - x0 < 16:
            return
        cx = (x0 + x1) / 2
        cy = y + _ROW_H / 2
        icon_w = min(x1 - x0 - 4, _ROW_H - 6)
        rect = QRectF(cx - icon_w / 2, cy - icon_w / 2, icon_w, icon_w)
        # Hidden (eye off) gets the slashed-eye; visible gets the open eye.
        # State colour follows AE: visible rows use text_secondary, hidden
        # rows use text_hint so the toggle reads as "dimmed" not "broken".
        state = "normal" if visible else "disabled"
        name = "eye" if visible else "eye_off"
        timeline_icons.draw_icon(p, name, rect, state)

    def _draw_solo(self, p: QPainter,
                   geom: Dict[str, Tuple[int, int]],
                   y: int, solo: bool) -> None:
        x0, x1 = geom.get("solo", (0, 0))
        if x1 - x0 < 16:
            return
        cx = (x0 + x1) / 2
        cy = y + _ROW_H / 2
        icon_w = min(x1 - x0 - 4, _ROW_H - 6)
        rect = QRectF(cx - icon_w / 2, cy - icon_w / 2, icon_w, icon_w)
        # AE leaves inactive Solo cells quiet; only paint the active switch.
        # This avoids a row of repeated dots that visually competes with the
        # property name while retaining the same click target.
        if not solo:
            return
        # AUD-10307: 此处 solo 恒为 True（上方已提前返回），死分支收敛。
        timeline_icons.draw_icon(p, "solo", rect, "checked")

    def _draw_layer_switch(self, p: QPainter, span: Tuple[int, int], y: int,
                           icon: str, active: bool, enabled: bool) -> None:
        x0, x1 = span
        if x1 - x0 < 14:
            return
        size = min(14, x1 - x0 - 2)
        rect = QRectF((x0 + x1 - size) / 2, y + (_ROW_H - size) / 2, size, size)
        state = "disabled" if not enabled else "checked" if active else "normal"
        p.save()
        if not active or not enabled:
            p.setOpacity(.6)
        timeline_icons.draw_icon(p, icon, rect, state)
        p.restore()

    def _draw_lock(self, p: QPainter,
                   geom: Dict[str, Tuple[int, int]],
                   y: int, locked: bool) -> None:
        """Lock icon via the AE icon painter (T35), positioned in the lock
        column. Falls back to icon_manager only if draw_icon is unavailable
        — but the painter is always present, so this path is the main one."""
        x0, x1 = geom.get("lock", (0, 0))
        if x1 - x0 < 16:
            return
        cx = (x0 + x1) / 2
        cy = y + _ROW_H / 2
        icon_w = min(x1 - x0 - 4, _ROW_H - 6)
        rect = QRectF(cx - icon_w / 2, cy - icon_w / 2, icon_w, icon_w)
        # An unlocked AE layer has an empty lock cell. Painting an open lock
        # on every row was one of the main sources of visual noise in the
        # old panel.
        if not locked:
            return
        timeline_icons.draw_icon(p, "lock", rect, "checked")

    def _draw_color_swatch(self, p: QPainter,
                           geom: Dict[str, Tuple[int, int]],
                           y: int, color: str) -> None:
        x0, x1 = geom.get("label", (0, 0))
        if x1 - x0 < 4:
            return
        cx = (x0 + x1) / 2
        cy = y + _ROW_H / 2
        swatch_w = max(4, min(x1 - x0, 8))
        p.setBrush(QColor(color))
        p.setPen(QPen(QColor(timeline_layout.resolve_token("border"))))
        p.drawRect(QRectF(cx - swatch_w / 2, cy - swatch_w / 2,
                          swatch_w, swatch_w))

    def _draw_name(self, p: QPainter,
                   geom: Dict[str, Tuple[int, int]],
                   y: int, row) -> None:
        x0, x1 = geom.get("name", (0, self.width()))
        cell_w = x1 - x0
        if cell_w <= 4:
            return

        # Expand arrow for node/layer rows stays inside the Name cell. It must never
        # be drawn at a legacy fixed x because the switch strip is resizable.
        inline = row.layer_id is not None and (row.row_type == "param" or bool(row.component_params))
        if row.row_type in ("node", "layer", "group") and not inline:
            glyph = "disclosure_down" if row.expanded else "disclosure_right"
            start = x0 + 4 + self._row_indent(row)
            if start + 12 <= x1:
                timeline_icons.draw_icon(p, glyph, QRectF(start, y + (_ROW_H - 12) / 2, 12, 12))

        # Indent within the resolved Name cell. All text is clipped/elided to
        # the cell's right edge, so it can never run into Value or Expr.
        label_x = x0 + 3 if inline else x0 + 22 + self._row_indent(row)
        if row.row_type in ("node", "group"):
            text_token = "text_secondary"
            font_pt = 10 if row.row_type == "group" or not row.depth else 9
        elif row.row_type == "layer":
            text_token = "text_secondary"
            font_pt = 10
        else:
            text_token = "text_label"
            font_pt = 9

        p.setPen(QPen(QColor(timeline_layout.resolve_token(text_token))))
        font = p.font()
        font.setPointSize(font_pt)
        font.setBold(row.row_type == "group" and not row.component_params)
        p.setFont(font)

        # Reserve a few px on the right so the label never collides with the
        # Value column's left edge even on a tight splitter.
        right_reserve = 5
        relation_badge = (str(getattr(row, "relation_badge", "") or "")
                          if row.row_type == "layer" else "")
        badge_width = min(42, max(0, len(relation_badge) * 8 + 8))
        avail = max(1, x1 - label_x - right_reserve - badge_width)
        label = p.fontMetrics().elidedText(
            row.label, Qt.TextElideMode.ElideRight, avail)
        p.drawText(QRectF(label_x, y, avail, _ROW_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                   label)
        if relation_badge and badge_width >= 12:
            badge_state = getattr(row, "relation_badge_state", "normal")
            color_token = ("error" if badge_state == "error"
                           else "accent_blue" if badge_state == "source"
                           else "accent_orange")
            badge_x = x1 - right_reserve - badge_width
            badge_rect = QRectF(badge_x, y + 4, badge_width, _ROW_H - 8)
            p.setBrush(QColor(timeline_layout.resolve_token(color_token)))
            p.setPen(QPen(QColor(timeline_layout.resolve_token("border"))))
            p.drawRoundedRect(badge_rect, 3, 3)
            p.setPen(QPen(QColor("#17181a")))
            p.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter,
                       relation_badge)

    def _row_indent(self, row) -> int:
        step = 8 if self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS else 14
        return max(0, int(getattr(row, "depth", 0))) * step

    def _draw_value(self, p: QPainter,
                    geom: Dict[str, Tuple[int, int]],
                    y: int, row) -> None:
        x0, x1 = geom.get("value", (0, 0))
        cell_w = x1 - x0
        if cell_w < 12:
            return
        # Only param rows carry a value; node rows leave the cell empty.
        if row.row_type != "param":
            return
        text: Optional[str] = self._safe_value_text(row.node_id,
                                                    row.param_name)
        if not text:
            # Faint dash so the column reads as "no live value yet" rather
            # than "broken". Uses text_hint (low contrast) intentionally.
            p.setPen(QPen(QColor(timeline_layout.resolve_token("text_hint"))))
            p.drawText(QRectF(x0, y, cell_w, _ROW_H),
                       Qt.AlignmentFlag.AlignVCenter
                       | Qt.AlignmentFlag.AlignHCenter,
                       "—")
            return
        if row.row_type == "param" and (row.node_id, row.param_name) \
                in self._value_controls:
            # The shared DragValue child paints this cell and owns its text,
            # focus, double-click editor and active-drag styling.
            return
        p.setPen(QPen(QColor(timeline_layout.resolve_token("text_label"))))
        font = p.font()
        font.setPointSize(9)
        p.setFont(font)
        p.drawText(QRectF(x0 + 4, y, cell_w - 6, _ROW_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine,
                   str(text))

    def _draw_keyframe_control(self, p: QPainter,
                               geom: Dict[str, Tuple[int, int]],
                               y: int, row) -> None:
        x0, x1 = geom.get("keyframe", (0, 0))
        if x1 - x0 < 16:
            return
        animated = False
        at_current = False
        provider = self._keyframe_status_provider
        if provider is not None:
            try:
                params = row.component_params or (row.param_name,)
                status = [provider(row.node_id, param) for param in params]
                if all(isinstance(item, dict) for item in status):
                    animated = any(item.get("animated") for item in status)
                    at_current = any(item.get("at_current") for item in status)
                elif len(status) == 1 and isinstance(status[0], tuple):
                    animated, at_current = map(bool, status[0][:2])
            except (AttributeError, IndexError, TypeError, ValueError):
                pass
        icon_size = min(16.0, max(0.0, x1 - x0 - 4.0))
        rect = QRectF((x0 + x1 - icon_size) / 2.0,
                      y + (_ROW_H - icon_size) / 2.0,
                      icon_size, icon_size)
        if self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS:
            timeline_icons.draw_property_keyframe(p, rect, at_current=at_current,
                                                  enabled=not row.locked)
            return
        if at_current:
            icon, state = "diamond", "selected"
        elif animated:
            # An animated property away from the playhead remains a distinct
            # blue filled diamond; the orange selected diamond is reserved for
            # the keyframe exactly under the playhead.
            icon, state = "diamond", "checked"
        else:
            icon, state = "diamond_plus", "normal"
        timeline_icons.draw_icon(p, icon, rect, state)

    def _draw_binding_control(self, p: QPainter,
                              geom: Dict[str, Tuple[int, int]],
                              y: int, row) -> None:
        if (row.row_type in ("node", "layer")
                and not self._parent_binding_supported(row.node_id)):
            return
        x0, x1 = geom.get("bind", (0, 0))
        if x1 - x0 < 16:
            return
        bound = False
        provider = self._binding_provider
        if provider is not None:
            try:
                bound = bool(provider(row.node_id,
                                      row.param_name if row.row_type == "param"
                                      else None))
            except Exception:
                bound = False
        icon_size = min(16.0, max(0.0, x1 - x0 - 4.0))
        rect = QRectF((x0 + x1 - icon_size) / 2.0,
                      y + (_ROW_H - icon_size) / 2.0,
                      icon_size, icon_size)
        timeline_icons.draw_icon(p, "pick_whip" if row.layer_id is not None else "link", rect,
                                 "checked" if bound else "normal")

    def _layer_parent_visible(self, row: KeyframesRow) -> bool:
        return bool(row.parent_layer_name or (row.layer_managed and self._parent_binding_supported(row.node_id)))

    def _draw_layer_parent(self, p: QPainter, span: Tuple[int, int], y: int, row: KeyframesRow) -> None:
        left, right = span
        if right - left < 35:
            return
        text = row.parent_layer_name or I18N.tr("keyframes.layer_no_relation", "None")
        text = p.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, right - left - 25)
        editable = row.layer_managed and not row.locked
        text_token = "text_secondary" if editable else "text_hint"
        p.setPen(QPen(QColor(timeline_layout.resolve_token(text_token))))
        p.drawText(QRectF(left + 6, y, right - left - 20, _ROW_H), Qt.AlignmentFlag.AlignVCenter, text)
        if editable:
            timeline_icons.draw_icon(p, "disclosure_down", QRectF(right - 16, y + 6, 12, 12), "normal")

    def _draw_expression_status(self, p: QPainter,
                                geom: Dict[str, Tuple[int, int]],
                                y: int, row) -> None:
        """P5/T49: paint the 4-state expression marker for a param row.

        States (per timeline_expression.EXPRESSION_*):
          - none      → nothing drawn (no expression on this param)
          - valid     → small '=' marker in the param colour
          - error     → '!' marker + the row keeps its content readable
          - disabled  → greyed '=' marker

        T28: now painted inside the dedicated ``expr`` column instead of the
        ad-hoc 22 px reserved strip on the right of the name cell. Errors are
        also flagged with a subtle left-edge tint so a colourblind user
        notices the row even without parsing the icon.
        """
        provider = self._expression_status_provider
        if provider is None or not getattr(row, "param_name", None):
            return
        try:
            status = provider(row.node_id, row.param_name)
        except Exception:
            return
        if status is None:
            return
        state = getattr(status, "state", None)
        if state is None or state == "none":
            return  # no expression → no marker, no visual change

        x0, x1 = geom.get("expr", (0, 0))
        cell_w = x1 - x0
        if cell_w < 8:
            return

        # Error: subtle left-edge tint so the row reads as "needs attention"
        # even without the icon (colourblind-friendly, plan §3.4).
        if state == "error":
            p.fillRect(0, y, 3, _ROW_H,
                       QColor(timeline_layout.resolve_token("error")))

        # Marker glyph in the expr column. '=' for valid/disabled, '!' for
        # error — so the glyph itself differs by state (shape not colour
        # alone, plan §16.6).
        color = self._expression_color(state)
        p.setPen(QPen(QColor(color)))
        font = p.font()
        font.setPointSize(8)
        font.setBold(state == "error")
        p.setFont(font)
        glyph = "!" if state == "error" else "="
        p.drawText(QRectF(x0, y, cell_w, _ROW_H),
                   Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter,
                   glyph)

    @staticmethod
    def _expression_color(state: str) -> str:
        if state == "error":
            return timeline_layout.resolve_token("error")
        if state == "disabled":
            return timeline_layout.resolve_token("text_hint")
        # valid → use the param accent colour indirectly via text_label so it
        # doesn't fight the row's own colour (the swatch already carries it).
        return timeline_layout.resolve_token("text_label")

    def _draw_empty(self, p: QPainter, w: int, h: int) -> None:
        p.setPen(QPen(QColor(timeline_layout.resolve_token("text_hint"))))
        p.drawText(0, 0, w, h, Qt.AlignmentFlag.AlignCenter,
                   I18N.tr("keyframes.empty", "No animated parameters"))

    # ── interaction (P9) ──

    def row_at(self, y: int) -> int:
        """Return the visible-row index at widget y, or -1."""
        if y < self._header_height:
            return -1
        idx = (y - self._header_height + self._scroll_y) // _ROW_H
        rows = self._model.visible_rows()
        if 0 <= idx < len(rows):
            return idx
        return -1

    def mouseMoveEvent(self, event) -> None:
        pos = event.position()
        if (self._layer_drag_candidate is not None
                and event.buttons() & Qt.MouseButton.LeftButton):
            layer_id, start_x, start_y = self._layer_drag_candidate
            distance = abs(float(pos.x()) - start_x) + abs(float(pos.y()) - start_y)
            if not self._layer_drag_active and distance >= 4.0:
                self._layer_drag_active = True
            if self._layer_drag_active:
                row_index, before = self._row_index_at_y(float(pos.y()))
                self._drop_indicator = (row_index, before)
                self.update()
                return

        if self._keyframe_drag is not None and event.buttons() & \
                Qt.MouseButton.LeftButton:
            node_id, param_name, start_x, base_value, active = self._keyframe_drag
            delta = float(pos.x()) - float(start_x)
            if not active and abs(delta) < 2.0:
                return
            if not active:
                self._keyframe_drag = (node_id, param_name, start_x,
                                       base_value, True)
                self.value_drag_started.emit(node_id, param_name, base_value)
            self.value_dragged.emit(node_id, param_name, delta)
            self.update()
            return

        if self._value_drag is not None and event.buttons() & \
                Qt.MouseButton.LeftButton:
            node_id, param_name, start_x, _last_x = self._value_drag
            delta = float(pos.x()) - start_x
            self._value_drag = (node_id, param_name, start_x, float(pos.x()))
            self.value_dragged.emit(node_id, param_name, delta)
            self.update()
            return

        if self._binding_drag is not None:
            self._binding_drag_pos = pos
            self._binding_drag_hover_index = self.row_at(int(pos.y()))
            if not self._binding_drag_active:
                origin = self._binding_drag["origin"]
                if (abs(float(pos.x() - origin.x()))
                        + abs(float(pos.y() - origin.y())) >= 4.0):
                    self._binding_drag_active = True
                    self.setCursor(Qt.CursorShape.CrossCursor)
            if self._binding_drag_active:
                self._refresh_binding_drag_overlay()
                self.update()
                return

        idx = self.row_at(int(pos.y()))
        rows = self._model.visible_rows()
        row = rows[idx] if 0 <= idx < len(rows) else None
        new_hover = id(row) if row is not None else -1
        tooltip = row.relation_tooltip if row is not None else ""
        geom = self._row_geometry(row, self._column_geometry()) if row is not None else {}
        if row is not None and row.row_type == "layer":
            if self._hit_column(geom, "shy", float(pos.x())):
                tooltip = I18N.tr("keyframes.mark_layer_shy", "Mark layer to hide")
            elif self._hit_column(geom, "motion_blur", float(pos.x())):
                tooltip = I18N.tr("keyframes.layer_motion_blur", "Layer motion blur")
        if row is not None and self._disclosure_hit(
                row, geom["name"], float(pos.x()), recursive=True):
            modifier = "Cmd" if sys.platform == "darwin" else "Ctrl"
            hint = I18N.tr(
                "keyframes.recursive_disclosure",
                "{modifier}+click to expand or collapse nested groups.",
            ).format(modifier=modifier)
            tooltip = "\n".join(filter(None, (tooltip, hint)))
        if tooltip != self.toolTip():
            self.setToolTip(tooltip)
        if new_hover != self._hover_row:
            self._hover_row = new_hover
            self.update()

    def _disclosure_hit(self, row: KeyframesRow, name_span: Tuple[int, int], x: float,
                        *, recursive: bool = False) -> bool:
        if row.component_params or row.row_type not in ("node", "layer", "group"):
            return False
        x0, x1 = name_span
        if recursive and row.layer_id is None:
            return False
        if row.row_type == "group" and not recursive:
            return x0 <= x < x1
        arrow_left = x0 + self._row_indent(row)
        return arrow_left <= x < min(x1, arrow_left + 22)

    def mousePressEvent(self, event) -> None:
        """P9: click selects node / toggles expand / toggles lock / eye."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        pos = event.position()
        x, y = float(pos.x()), float(pos.y())
        idx = self.row_at(int(y))
        rows = self._model.visible_rows()
        if not (0 <= idx < len(rows)):
            return
        row = rows[idx]

        # Start a potential reorder only from the body of a Layer row. The
        # toggle columns below must retain their independent click behavior.
        geom = self._row_geometry(row, self._column_geometry())
        # Qt reports Command as ControlModifier on macOS, and Control
        # as ControlModifier on the other desktop platforms.
        recursive = (row.layer_id is not None
                     and bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier))
        if self._disclosure_hit(row, geom["name"], x, recursive=recursive):
            expanded = not row.expanded
            if row.row_type == "layer" and row.layer_id:
                self.layer_disclosure_requested.emit(row.layer_id)
            if self._model.set_row_expanded(row.row_id, expanded, recursive=recursive):
                self.expand_changed.emit(row.node_id, expanded)
                self.refresh()
            return
        if row.row_type == "group":
            if (row.component_params and not row.expanded and not row.locked
                    and self._hit_column(geom, "keyframe", x)):
                self.paired_keyframe_toggle_requested.emit(row.node_id, row.component_params)
            return

        if row.row_type == "layer" and row.layer_id:
            if self._hit_column(geom, "shy", x):
                if not row.locked:
                    self.layer_shy_changed.emit(row.layer_id, not row.layer_shy)
                return
            if self._hit_column(geom, "motion_blur", x):
                if row.layer_motion_blur is not None and not row.locked:
                    self.layer_motion_blur_changed.emit(row.layer_id, not row.layer_motion_blur)
                return
            if self._layer_parent_visible(row) and self._hit_column(geom, "value", x):
                if row.layer_managed and not row.locked:
                    bottom = (idx + 1) * _ROW_H + self._header_height - self._scroll_y
                    position = self.mapToGlobal(QPoint(geom["value"][0], bottom))
                    self.layer_parent_menu_requested.emit(
                        row.layer_id, position)
                return
            if not any(self._hit_column(geom, column, x)
                       for column in ("eye", "solo", "lock", "label", "bind")):
                self._layer_drag_candidate = (
                    row.layer_id, float(pos.x()), float(pos.y()))
                self._layer_drag_active = False

        # Eye column click → toggle the row's enabled state (T28).
        # AUD-10301: write the model's real field (``layer_enabled``).
        # The old code assigned a transient ``row.eye`` attribute that no
        # paint path or model rebuild ever read back.
        has_switches = (row.row_type == "layer" or (row.row_type == "node"
                        and self._model.view_mode == KeyframesModel.VIEW_MODE_NODES))
        if has_switches and self._hit_column(geom, "eye", x):
            new_val = not getattr(row, "layer_enabled", True)
            row.layer_enabled = new_val
            self.eye_changed.emit(row.node_id, new_val)
            self.update()
            return

        # Solo is a Layer-mode property. In Nodes mode there is no layer
        # authority, so leave the cell inert rather than mutating a transient
        # row flag that cannot be persisted.
        if self._hit_column(geom, "solo", x):
            if row.row_type != "layer" or not row.layer_id:
                return
            new_val = not getattr(row, "layer_solo", False)
            row.layer_solo = new_val
            self.solo_changed.emit(row.layer_id, new_val)
            self.update()
            return

        # Lock column click → toggle lock. Unlike the old implementation this
        # is strictly the resolved Lock cell, so an Eye click can never also
        # toggle the lock because the two hit regions overlap.
        if has_switches and self._hit_column(geom, "lock", x):
            row.locked = not row.locked
            self.lock_changed.emit(row.node_id, row.locked)
            self.update()
            return

        if row.row_type == "param" and self._hit_column(geom, "keyframe", x):
            if row.locked:
                return
            status = {}
            if self._keyframe_status_provider is not None:
                try:
                    status = self._keyframe_status_provider(
                        row.node_id, row.param_name) or {}
                except Exception:
                    status = {}
            if event.modifiers() & Qt.KeyboardModifier.AltModifier:
                # AE's Alt/Option click on the keyframe control expands the
                # property into an editable keyframe row. It is deliberately
                # different from Alt-clicking a timeline diamond, which still
                # opens the expression editor.
                self.keyframe_edit_requested.emit(row.node_id, row.param_name)
                return
            if bool(status.get("at_current")):
                base_text = self._safe_value_text(row.node_id, row.param_name)
                try:
                    base_value = float(base_text) if base_text is not None else 0.0
                except (TypeError, ValueError):
                    base_value = 0.0
                self._keyframe_drag = (row.node_id, row.param_name, x,
                                       base_value, False)
                self.param_selected.emit(row.node_id, row.param_name)
            else:
                self.keyframe_toggle_requested.emit(row.node_id, row.param_name)
                self.update()
            return

        if self._hit_column(geom, "value", x) and row.row_type == "param":
            if row.locked:
                return
            # A simple click still selects the property. Holding the button
            # and moving horizontally turns the same cell into a scrubber.
            self.param_selected.emit(row.node_id, row.param_name)
            base_text = self._safe_value_text(row.node_id, row.param_name)
            try:
                base_value = (float(base_text) if base_text is not None
                              else 0.0)
            except (TypeError, ValueError):
                # Option/bool rows are scrubbed by index/state in the panel;
                # preserve their label instead of silently turning it into 0.
                base_value = base_text if base_text is not None else ""
            self._value_drag = (row.node_id, row.param_name,
                                x, x)
            self.value_drag_started.emit(row.node_id, row.param_name,
                                         base_value)
            return

        if self._hit_column(geom, "bind", x):
            # Node-row parent binding is opt-in. Parameter pick-whip remains
            # available for every parameter row regardless of node type.
            if row.locked or (row.row_type in ("node", "layer")
                    and not self._parent_binding_supported(row.node_id)):
                return
            self._binding_drag = {
                "node_id": row.node_id,
                "param_name": row.param_name or "",
                "origin": QPointF(x, y),
            }
            self._binding_drag_pos = QPointF(x, y)
            self._binding_drag_active = False
            return

        # Label click → select. A Layer row still selects its root node for
        # global canvas/property synchronisation, but must not be mistaken
        # for a parameter row with a missing param name.
        if row.row_type == "layer":
            self._model.set_selected_node(row.node_id)
            if row.layer_id:
                self.set_selected_layer_id(row.layer_id)
                self.layer_selected.emit(row.layer_id)
            self.node_selected.emit(row.node_id)
        elif row.row_type == "node":
            self._selected_layer_id = None
            self._model.set_selected_node(row.node_id)
            self.node_selected.emit(row.node_id)
        else:
            self._selected_layer_id = None
            self.param_selected.emit(row.node_id, row.param_name)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return

        if self._layer_drag_candidate is not None:
            layer_id, _start_x, _start_y = self._layer_drag_candidate
            was_active = self._layer_drag_active
            self._layer_drag_candidate = None
            self._layer_drag_active = False
            if was_active:
                pos = event.position()
                row_index, before = self._row_index_at_y(float(pos.y()))
                _target_layer_id, insert_index = self._resolve_drop_target(
                    row_index, before)
                self._drop_indicator = None
                self.update()
                if insert_index >= 0:
                    self.layer_reorder_requested.emit(layer_id, insert_index)
                return
            self._drop_indicator = None

        if self._value_drag is not None:
            node_id, param_name, _start_x, _last_x = self._value_drag
            self._value_drag = None
            self.value_drag_finished.emit(node_id, param_name)
            self.update()
            return

        if self._keyframe_drag is not None:
            node_id, param_name, _start_x, _base_value, active = self._keyframe_drag
            self._keyframe_drag = None
            if active:
                self.value_drag_finished.emit(node_id, param_name)
            else:
                # A click on the orange diamond retains the normal AE toggle.
                self.keyframe_toggle_requested.emit(node_id, param_name)
            self.update()
            return

        if self._binding_drag is not None:
            drag = self._binding_drag
            active = self._binding_drag_active
            pos = event.position()
            idx = self.row_at(int(pos.y()))
            rows = self._model.visible_rows()
            self._binding_drag = None
            self._binding_drag_active = False
            self._binding_drag_hover_index = -1
            self._refresh_binding_drag_overlay()
            self.unsetCursor()
            # The press origin identifies the binding target for both the
            # pick-whip path and the plain-click path below; resolve it
            # before branching so the click path can never reference an
            # unassigned local (AUD-10300).
            target_node = str(drag["node_id"])
            target_param = str(drag["param_name"])
            if active and 0 <= idx < len(rows):
                source = rows[idx]
                if target_param and source.row_type == "param":
                    if (source.node_id != target_node
                            or source.param_name != target_param):
                        self.parameter_binding_dragged.emit(
                            target_node, target_param,
                            source.node_id, source.param_name)
                elif not target_param and source.row_type in ("node", "layer"):
                    if (source.node_id != target_node
                            and self._parent_binding_supported(target_node)
                            and self._parent_binding_supported(source.node_id)):
                        self.parent_binding_dragged.emit(
                            target_node, source.node_id)
            elif not active:
                self.binding_requested.emit(
                    target_node, target_param)
            self.update()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        """Open a small in-cell editor when the Value cell is double-clicked."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        x, y = float(event.position().x()), float(event.position().y())
        idx = self.row_at(int(y))
        rows = self._model.visible_rows()
        if not (0 <= idx < len(rows)):
            return
        row = rows[idx]
        geom = self._column_geometry()
        if row.row_type == "param" and not row.locked and self._hit_column(geom, "value", x):
            self._begin_value_edit(row, geom["value"], int(y))
            return
        super().mouseDoubleClickEvent(event)

    def _begin_value_edit(self, row, span: Tuple[int, int], y: int) -> None:
        self._finish_value_edit(commit=False)
        editor = QLineEdit(self)
        # AUD-10302: guarded provider call — the double-click path used to
        # invoke the host callback bare.
        current = self._safe_value_text(row.node_id, row.param_name)
        editor.setText(str(current) if current is not None else "")
        editor.selectAll()
        editor.setObjectName("KeyframesInlineValueEditor")
        editor.setStyleSheet(
            "QLineEdit { background: #292c31; color: #f0f2f5; "
            "border: 1px solid #e8872f; padding: 0 3px; }")
        editor.setGeometry(span[0] + 2, y + 2,
                           max(12, span[1] - span[0] - 4), _ROW_H - 4)
        editor.editingFinished.connect(self._commit_value_edit)
        self._value_editor = editor
        self._value_editor_target = (row.node_id, row.param_name)
        editor.show()
        editor.setFocus(Qt.FocusReason.MouseFocusReason)

    def _commit_value_edit(self) -> None:
        if self._value_editor is None or self._value_editor_target is None:
            return
        text = self._value_editor.text().strip()
        target = self._value_editor_target
        self._finish_value_edit(commit=False)
        if text:
            self.value_changed_requested.emit(target[0], target[1], text)

    def _finish_value_edit(self, commit: bool = False) -> None:
        del commit  # retained for callers that want an explicit cancel path
        editor = self._value_editor
        self._value_editor = None
        self._value_editor_target = None
        if editor is not None:
            editor.deleteLater()

    def keyPressEvent(self, event) -> None:
        if (event.key() == Qt.Key.Key_Escape
                and self._value_editor is not None):
            self._finish_value_edit(commit=False)
            event.accept()
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            layer_id = self._selected_layer_id
            if layer_id:
                row = next((r for r in self._model.visible_rows()
                            if r.row_type == "layer"
                            and r.layer_id == layer_id), None)
                if row is not None:
                    self.layer_delete_requested.emit(layer_id)
                    event.accept()
                    return
        super().keyPressEvent(event)

    def contextMenuEvent(self, event) -> None:
        """Expose expression editing from the property row itself.

        This keeps the row uncluttered (no permanent ``fx`` button) while
        still making the feature discoverable in the same place AE users
        expect: right-click the property name.
        """
        # QContextMenuEvent uses .pos() (QPoint), NOT .position() (QPointF,
        # which only exists on QMouseEvent/QSinglePointEvent subclasses).
        idx = self.row_at(int(event.pos().y()))
        rows = self._model.visible_rows()
        if not (0 <= idx < len(rows)):
            if (self._model.view_mode == KeyframesModel.VIEW_MODE_LAYERS
                    and event.pos().y() >= self._header_height):
                self.empty_layer_menu_requested.emit(event.globalPos())
                event.accept()
            return
        row = rows[idx]
        menu = QMenu(self)
        vector = self._model.dimension_group(row) if row.layer_id is not None else None
        if vector is not None:
            label = (I18N.tr("keyframes.combine_dimensions", "Combine Dimensions")
                     if vector.expanded else
                     I18N.tr("keyframes.separate_dimensions", "Separate Dimensions"))
            menu.addAction(label, lambda: self.dimension_toggle_requested.emit(vector.row_id))
        if row.row_type == "node":
            attach = menu.addAction(I18N.tr("keyframes.add_to_active_layer", "Add to Active Layer"))
            promote = menu.addAction(I18N.tr("keyframes.promote_to_layer", "Promote to Layer"))
            matte = menu.addAction(I18N.tr("keyframes.create_matte_layer", "Create Matte Layer"))
            chosen = menu.exec(event.globalPos())
            if chosen is attach:
                self.attach_node_requested.emit(row.node_id)
            elif chosen is promote:
                self.promote_node_requested.emit(row.node_id)
            elif chosen is matte:
                self.create_matte_layer_requested.emit(row.node_id)
            return
        if row.row_type == "param" and row.param_name:
            edit = menu.addAction(
                I18N.tr("keyframes.edit_expression", "Edit Expression"))
            edit.setToolTip(
                I18N.tr("keyframes.edit_expression_tooltip", "Edit this property's expression"))
            chosen = menu.exec(event.globalPos())
            if chosen is edit:
                self.expression_edit_requested.emit(row.node_id,
                                                    row.param_name)
            return
        if row.row_type == "layer" and row.layer_id:
            self.layer_menu_requested.emit(row.layer_id, event.globalPos())
        elif vector is not None:
            menu.exec(event.globalPos())

    @staticmethod
    def _hit_column(geom: Dict[str, Tuple[int, int]],
                    name: str, x: float) -> bool:
        """Return True if ``x`` falls inside the named column's x-range."""
        span = geom.get(name)
        if span is None:
            return False
        return span[0] <= x < span[1]

    # ── Media-drop support (AE/Nuke hybrid, plan §5) ────────────────────
    # The view stays MIME-agnostic: it only knows how to compute the
    # insertion index from a y position and how to paint an indicator.
    # The host installs ``_media_drop_handler`` and decides whether a given
    # drag is acceptable, so this widget never imports the edit/media_pool
    # module (which would violate the feature boundary).

    def set_media_drop_handler(self, handler) -> None:
        """Install ``(mime_data, target_layer_id, insert_before) -> bool``.

        When set, the view accepts media drags and forwards them to
        the handler (the host resolves payloads and calls the controller).
        Pass None to disable drop support entirely.
        """
        self._media_drop_handler = handler

    def _is_media_drop(self, event: QDropEvent) -> bool:
        """Return whether this event can reach the media drop handler."""
        return (self._media_drop_handler is not None
                and accepts_layer_media_drop(event.mimeData()))

    def _row_index_at_y(self, y: float) -> Tuple[int, bool]:
        """Map a viewport y to ``(row_index, before?)``.

        ``before=True`` means "insert above this row" (pointer in the upper
        half), ``False`` means "insert below".  The returned index may equal
        ``len(visible_rows)`` for a pointer past the last row (append at the
        bottom / background).
        """
        rel = y - self._header_height + self._scroll_y
        if rel < 0:
            return 0, True
        idx = int(rel // _ROW_H)
        rows = self._model.visible_rows()
        if idx >= len(rows):
            return len(rows), True  # past the end → append (bottom)
        within = rel - idx * _ROW_H
        before = within < _ROW_H / 2
        return idx, before

    def _resolve_drop_target(self, row_index: int,
                             before: bool) -> Tuple[Optional[str], int]:
        """Translate a ``(row_index, before)`` into a layer target.

        Returns ``(target_layer_id, insert_index)`` where ``insert_index``
        is the position in ``stack.layer_ids`` (0 = top / foreground) the
        dropped media should occupy.  ``target_layer_id`` is the layer the
        indicator is anchored to (None when dropping into empty space).
        Only ``layer`` rows are valid drop anchors; parameter rows are
        rejected. ``insert_index == -1`` is the rejection sentinel.
        """
        rows = self._model.visible_rows()
        if not rows:
            return None, 0
        if row_index >= len(rows):
            # Past the end → bottom of the stack.
            n = sum(1 for r in rows if r.row_type == "layer")
            return None, n
        row = rows[row_index]
        if row.row_type != "layer":
            return None, -1
        # row.layer_index is 1-based foreground-first.
        idx = max(0, (row.layer_index or 1) - 1)
        if not before:
            idx += 1
        return row.layer_id, idx

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._is_media_drop(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if not self._is_media_drop(event):
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        row_index, before = self._row_index_at_y(float(pos.y()))
        self._drop_indicator = (row_index, before)
        self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        self._drop_indicator = None
        self.update()
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if not self._is_media_drop(event):
            self._drop_indicator = None
            self.update()
            event.ignore()
            return
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        row_index, before = self._row_index_at_y(float(pos.y()))
        target_layer_id, insert_index = self._resolve_drop_target(
            row_index, before)
        self._drop_indicator = None
        self.update()
        handler = self._media_drop_handler
        if handler is None or insert_index < 0:
            event.ignore()
            return
        try:
            handled = bool(handler(event.mimeData(), target_layer_id,
                                   insert_index))
        except Exception:
            handled = False
        if handled:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _paint_drop_indicator(self, painter: QPainter) -> None:
        """Draw the AE-style insertion line for the active media drag.

        Called from ``paintEvent`` after rows are drawn; the line spans the
        full row width at the boundary indicated by ``_drop_indicator``.
        """
        if self._drop_indicator is None:
            return
        row_index, before = self._drop_indicator
        y = self._header_height + row_index * _ROW_H - self._scroll_y
        if not before:
            y += _ROW_H
        # Keep the line inside the viewport even when scrolled.
        y = max(self._header_height, min(y, self.height() - 1))
        painter.save()
        # Match the binding drag overlay's accent colour so all interactive
        # guides read as one family.
        pen = QPen(QColor(timeline_layout.resolve_token("accent_blue")))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawLine(0, int(y), self.width(), int(y))
        painter.restore()
