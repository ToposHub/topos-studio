"""SplinePanel — top-level Spline curve editor dock (phase 3, plan §11.1).

Layout (Fusion-style):
  - top title bar (Spline + zoom / fit / tangent-mode buttons)
  - left curve tree (node → param rows with checkboxes + color dots)
  - right SplineView curve chart
  - bottom toolbar (tangent modes, fit, time/value unit combos)

Edit flow: SplineView emits keyframe_changed → this panel forwards it to
CompositePage, which writes the node + pushes undo + re-renders. Tangent-mode
buttons call set_key_tangent_mode() on the selected key.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt, QTimer, QSize, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QPainter, QPen
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from src.shared.ui.panel_splitter import PanelSplitter
from src.shared.assets.icon_manager import get_icon, IconColors
from src.shared.i18n.i18n import I18N
from src.shared.ui.style_sheet import (
    C, COMPOSITE_KEYFRAMES_COMBO_STYLE, COMPOSITE_KEYFRAMES_ICON_BTN_STYLE,
    COMPOSITE_KEYFRAMES_PANEL_STYLE, COMPOSITE_KEYFRAMES_TOOLBAR_STYLE,
    COMPOSITE_KEYFRAMES_TREE_STYLE,
)
from .spline_model import SplineModel
from .spline_view import SplineView, set_key_tangent_mode


class SplineCurveTree(QWidget):
    """Left side: node/param rows with visibility checkboxes + color dots.

    Phase 3 paints a minimal list; full tree expansion mirrors the Keyframes
    tree and is left for iteration once the curve editing is proven.
    """

    curve_visibility_toggled = pyqtSignal(str, bool)
    curve_selected = pyqtSignal(str)

    def __init__(self, model: SplineModel, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        self.setMinimumWidth(180)
        # Hover highlight: track the row under the cursor so paintEvent can
        # paint a BG_HOVER band. Mirrors KeyframesTreeView's approach.
        self.setMouseTracking(True)
        self._hover_idx: Optional[int] = None
        self._filter_text = ""

    def set_filter(self, text: str) -> None:
        """Filter curve rows by their friendly node/parameter label."""
        self._filter_text = str(text or "").strip().casefold()
        self._hover_idx = None
        self.update()

    def _filtered_curves(self):
        if not self._filter_text:
            return self._model.curves
        return [c for c in self._model.curves
                if self._filter_text in c.label.casefold()]

    def refresh(self) -> None:
        self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(C.BG_PANEL))
        rows = self._filtered_curves()
        if not rows:
            p.setPen(QPen(QColor(C.TEXT_HINT)))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       I18N.tr("spline.empty", "No curves"))
            return
        row_h = 22
        for i, c in enumerate(rows):
            y = i * row_h
            # Row background: selected takes priority over hover. Matches the
            # KeyframesTreeView palette (BG_ACTIVE / BG_HOVER).
            if c.selected:
                p.fillRect(0, y, self.width(), row_h, QColor(C.BG_ACTIVE))
            elif self._hover_idx == i:
                p.fillRect(0, y, self.width(), row_h, QColor(C.BG_HOVER))
            # P4-1: visibility checkbox (0-18px)
            self._draw_checkbox(p, 4, y + row_h // 2 - 5, c.visible)
            # color dot
            p.setBrush(QColor(c.color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(20, y + row_h // 2 - 4, 8, 8)
            # label
            p.setPen(QPen(QColor(C.TEXT_SECONDARY if c.visible else C.TEXT_HINT)))
            font = p.font()
            font.setPointSize(10)
            p.setFont(font)
            p.drawText(34, y, self.width() - 38, row_h,
                       Qt.AlignmentFlag.AlignVCenter, c.label)
            p.setPen(QPen(QColor(C.BORDER_DARK)))
            p.drawLine(0, y + row_h - 1, self.width(), y + row_h - 1)

    def _draw_checkbox(self, p, x, y, checked) -> None:
        """P4-1: draw a small visibility checkbox."""
        p.setBrush(QColor(C.BG_INPUT))
        p.setPen(QPen(QColor(C.BORDER_MID)))
        p.drawRect(x, y, 10, 10)
        if checked:
            p.setPen(QPen(QColor(C.ORANGE), 2))
            # checkmark
            p.drawLine(x + 2, y + 5, x + 4, y + 8)
            p.drawLine(x + 4, y + 8, x + 8, y + 2)

    def mouseMoveEvent(self, event) -> None:
        """Track the hovered row so paintEvent can highlight it."""
        y = int(event.position().y())
        row_h = 22
        idx = y // row_h
        # AUD-10907: hover must index the FILTERED subset that paintEvent
        # draws (see _filtered_curves there) — indexing the full curve set
        # painted the highlight band on the wrong row while a filter was
        # active.
        if not (0 <= idx < len(self._filtered_curves())):
            idx = None
        if idx != self._hover_idx:
            self._hover_idx = idx
            self.update()

    def leaveEvent(self, _event) -> None:
        """Clear hover highlight when the cursor leaves the tree."""
        if self._hover_idx is not None:
            self._hover_idx = None
            self.update()

    def mousePressEvent(self, event) -> None:
        """P4-1: click checkbox → toggle visibility; click row → select curve."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        x, y = int(pos.x()), int(pos.y())
        row_h = 22
        idx = y // row_h
        curves = self._filtered_curves()
        if not (0 <= idx < len(curves)):
            return
        c = curves[idx]
        if x <= 18:
            # checkbox → toggle visibility
            self._model.set_visible(c.curve_id, not c.visible)
            self.curve_visibility_toggled.emit(c.curve_id, c.visible)
        else:
            # row → select (single selection)
            for cc in curves:
                cc.selected = (cc.curve_id == c.curve_id)
            self.curve_selected.emit(c.curve_id)
        self.refresh()


class SplinePanel(QWidget):
    """Top-level Spline dock panel."""

    # Forwarded to CompositePage: persists the edit + undo + re-render.
    keyframe_changed = pyqtSignal(str, str, list)
    curve_settings_changed = pyqtSignal(str, str, str, str)
    current_frame_changed = pyqtSignal(int)
    play_toggle_requested = pyqtSignal(bool)
    # T129: emitted when the panel is shown so CompositePage can redirect
    # the user to the shared Graph Editor (the Keyframes panel's graph mode)
    # instead of maintaining a second curve-editor state. The old Spline
    # entry still works (the panel constructs + shows); this signal lets the
    # host switch focus to the canonical Graph mode surface.
    graph_mode_redirect = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("CompositeSplinePanel")
        self.setMinimumHeight(220)

        self._model = SplineModel()
        self._tangent_buttons = {}
        self._playback_controller = None
        self._playback_slot = None
        self._playing = False
        self._start_frame = 0
        self._end_frame = 100
        self._fps = 24.0
        # T129: the first showEvent after construction is the page-enter
        # layout pass — we don't redirect on that, only on a real user
        # show (the dock's toggleViewAction). This flag flips True once
        # the initial layout show completes.
        self._initial_show_done = False
        # The real application hosts this widget inside a QDockWidget. Dock
        # layout restoration can show/hide the child several times while the
        # window is starting; those lifecycle events are not user requests.
        # The adapter wires the dock's toggleViewAction separately, so keep
        # showEvent-based compatibility only for standalone/legacy hosts.
        self._dock_hosted = False
        self._play_timer = QTimer(self)
        self._play_timer.timeout.connect(self._on_internal_play_tick)
        # P6/T56: shared selection subscription. None until the host wires
        # attach_to_shared_selection — detached Spline keeps its independent
        # selection (legacy behaviour).
        self._shared_selection_model = None
        self._syncing_shared_selection = False

        self._init_ui()
        self.setStyleSheet(COMPOSITE_KEYFRAMES_PANEL_STYLE)

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_toolbar())

        self._splitter = PanelSplitter(Qt.Orientation.Horizontal, self)

        self._tree = SplineCurveTree(self._model, parent=self._splitter)
        self._tree.setStyleSheet(COMPOSITE_KEYFRAMES_TREE_STYLE)
        # Tree → view: repaint handles/keys whenever the selection or a curve's
        # visibility changes. Previously these signals were emitted but never
        # connected, so the right-hand chart didn't follow the left-hand tree.
        self._tree.curve_visibility_toggled.connect(
            self._on_curve_visibility_toggled)
        self._tree.curve_selected.connect(self._on_curve_selected)
        self._view = SplineView(self._model, parent=self._splitter)
        self._view.keyframe_changed.connect(self.keyframe_changed)
        self._view.curve_settings_changed.connect(self.curve_settings_changed)
        # T61: when the user clicks a keyframe on a different curve in the
        # chart, the view changes the curve selection and emits
        # curve_selection_changed. Forward it through the same handler as
        # the tree-row click so the shared selection model gets the
        # matching param row (back-sync to the Keyframes panel).
        self._view.curve_selection_changed.connect(self._on_curve_selected)
        self._view.key_selection_changed.connect(
            self._on_key_selection_changed)
        # AUD-10905: the view emits current_frame_changed when the user
        # drags the playhead in the ruler; route it through seek_to_frame so
        # the main composition follows (range-clamped) instead of leaving the
        # view's internal frame stranded and snapping back on the next sync.
        self._view.current_frame_changed.connect(self._on_view_frame_changed)

        self._splitter.addWidget(self._tree)
        self._splitter.addWidget(self._view)
        self._splitter.setStretchFactor(0, 0)
        self._splitter.setStretchFactor(1, 1)
        self._splitter.setSizes([235, 800])
        root.addWidget(self._splitter, stretch=1)

        root.addWidget(self._build_footer())

    def _build_toolbar(self) -> QWidget:
        bar = QToolBar(self)
        bar.setMovable(False)
        bar.setIconSize(QSize(14, 14))
        bar.setStyleSheet(COMPOSITE_KEYFRAMES_TOOLBAR_STYLE +
                          COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)

        # Keep the same transport cluster as the Keyframes panel so Spline is
        # a complete animation workspace instead of a read-only companion.
        self._act_first = QAction(
            get_icon("fa5s.fast-backward", IconColors.DEFAULT),
            I18N.tr("keyframes.first_frame", "Go to start frame"), self)
        self._act_prev_frame = QAction(
            get_icon("fa5s.step-backward", IconColors.DEFAULT),
            I18N.tr("keyframes.prev_frame", "Previous frame"), self)
        self._act_play = QAction(
            get_icon("fa5s.play", IconColors.DEFAULT),
            I18N.tr("keyframes.play", "Play"), self)
        self._act_play.setCheckable(True)
        self._act_next_frame = QAction(
            get_icon("fa5s.step-forward", IconColors.DEFAULT),
            I18N.tr("keyframes.next_frame", "Next frame"), self)
        self._act_last = QAction(
            get_icon("fa5s.fast-forward", IconColors.DEFAULT),
            I18N.tr("keyframes.last_frame", "Go to end frame"), self)
        self._act_first.triggered.connect(
            lambda: self.seek_to_frame(self._start_frame))
        self._act_prev_frame.triggered.connect(
            lambda: self.seek_to_frame(self._view._current_frame - 1))
        self._act_play.toggled.connect(self._on_play_toggled)
        self._act_next_frame.triggered.connect(
            lambda: self.seek_to_frame(self._view._current_frame + 1))
        self._act_last.triggered.connect(
            lambda: self.seek_to_frame(self._end_frame))
        for action in (self._act_first, self._act_prev_frame, self._act_play,
                       self._act_next_frame, self._act_last):
            bar.addAction(action)
        bar.addSeparator()

        act_prev_kf = QAction(
            get_icon("fa5s.backward", IconColors.DEFAULT),
            I18N.tr("keyframes.prev_kf", "Previous keyframe"), self)
        act_next_kf = QAction(
            get_icon("fa5s.forward", IconColors.DEFAULT),
            I18N.tr("keyframes.next_kf", "Next keyframe"), self)
        act_prev_kf.triggered.connect(lambda: self._seek_nearest_keyframe(-1))
        act_next_kf.triggered.connect(lambda: self._seek_nearest_keyframe(1))
        bar.addAction(act_prev_kf)
        bar.addAction(act_next_kf)
        bar.addSeparator()

        title = QLabel(I18N.tr("panel.spline", "Spline"))
        title.setStyleSheet(
            f"QLabel {{ color: {C.TEXT_PRIMARY}; font-size: 13px;"
            " font-weight: 600; padding-right: 8px; }")
        bar.addWidget(title)
        self._curve_status = QLabel(I18N.tr("spline.curves_initial", "0 curves"))
        self._curve_status.setStyleSheet(
            f"QLabel {{ color: {C.TEXT_HINT}; font-size: 10px; }}")
        bar.addWidget(self._curve_status)
        bar.addSeparator()

        self._curve_filter = QLineEdit()
        self._curve_filter.setPlaceholderText(
            I18N.tr("spline.filter_curves", "Filter curves…"))
        self._curve_filter.setClearButtonEnabled(True)
        self._curve_filter.setFixedWidth(170)
        self._curve_filter.setStyleSheet(
            "QLineEdit{background:#242424;color:#ddd;border:1px solid #4a4a4a;"
            "border-radius:3px;padding:2px 6px;}"
            "QLineEdit:focus{border-color:#cc6600;}")
        # The tree is created immediately after the toolbar; defer lookup
        # until the user types so construction order stays safe.
        self._curve_filter.textChanged.connect(
            lambda text: self._tree.set_filter(text))
        bar.addWidget(self._curve_filter)

        act_fit = QAction(get_icon("fa5s.expand-arrows-alt", IconColors.DEFAULT),
                          I18N.tr("spline.fit", "Fit curves"), self)
        act_fit.triggered.connect(lambda: self._view.fit_view())
        act_fit.setToolTip(I18N.tr("spline.fit_all", "Fit all visible curves"))
        bar.addAction(act_fit)
        act_focus = QAction(
            I18N.tr("spline.focus_selected", "Focus selected"), self)
        act_focus.setToolTip(
            I18N.tr("spline.focus_selected_tip",
                    "Fit the selected curve to the chart"))
        act_focus.triggered.connect(lambda: self._view.fit_selected_view())
        bar.addAction(act_focus)
        act_all = QAction(I18N.tr("spline.show_all", "Show all"), self)
        act_all.triggered.connect(lambda: self._set_all_curve_visibility(True))
        bar.addAction(act_all)
        act_none = QAction(I18N.tr("spline.hide_all", "Hide all"), self)
        act_none.triggered.connect(lambda: self._set_all_curve_visibility(False))
        bar.addAction(act_none)
        for button in bar.findChildren(QToolButton):
            button.setFixedSize(24, 22)
        return bar

    def _build_footer(self) -> QWidget:
        footer = QWidget(self)
        footer.setFixedHeight(38)
        footer.setStyleSheet(
            f"QWidget {{ background-color: {C.BG_PANEL};"
            f" border-top: 1px solid {C.BORDER_DARK}; }}")
        layout = QHBoxLayout(footer)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        tangent_label = QLabel(I18N.tr("spline.tangent", "Tangent"), footer)
        tangent_label.setStyleSheet(
            f"QLabel {{ color: {C.TEXT_HINT}; padding-right: 3px; }}")
        layout.addWidget(tangent_label)

        # Tangent mode buttons (§11.1). Batch 4 adds auto + broken so all six
        # modes are reachable from the toolbar (matches AE/Nuke).
        for mode, icon, tip in [
            ("auto", "fa5s.magic", I18N.tr("spline.auto", "Auto")),
            ("smooth", "fa5s.wave-square", I18N.tr("spline.smooth", "Smooth")),
            ("linear", "fa5s.bezier-curve", I18N.tr("spline.linear", "Linear")),
            ("flat", "fa5s.minus", I18N.tr("spline.flat", "Flat")),
            ("step", "fa5s.grip-lines", I18N.tr("spline.step", "Step/Hold")),
            ("broken", "fa5s.unlink", I18N.tr("spline.broken", "Broken")),
        ]:
            act = QAction(get_icon(icon, IconColors.DEFAULT), tip, self)
            act.triggered.connect(lambda _checked=False, m=mode: self._set_tangent(m))
            btn = QToolButton(footer)
            btn.setDefaultAction(act)
            btn.setCheckable(True)
            btn.setToolTip(tip)
            btn.setStyleSheet(COMPOSITE_KEYFRAMES_ICON_BTN_STYLE)
            self._tangent_buttons[mode] = btn
            layout.addWidget(btn)

        layout.addStretch()

        # Batch 4: live frame/value readout for the sole selected key.
        self._sel_frame_label = QLabel("—", footer)
        self._sel_value_label = QLabel("—", footer)
        for lbl in (self._sel_frame_label, self._sel_value_label):
            lbl.setStyleSheet(
                f"QLabel {{ color: {C.TEXT_SECONDARY};"
                f" padding: 0 6px; font-family: monospace; }}")
        layout.addWidget(self._sel_frame_label)
        layout.addWidget(self._sel_value_label)
        self._view.selected_key_changed.connect(self._on_selected_key_changed)

        units_label = QLabel(I18N.tr("spline.units", "Units"), footer)
        units_label.setStyleSheet(
            f"QLabel {{ color: {C.TEXT_HINT}; padding-left: 8px; }}")
        layout.addWidget(units_label)
        self._time_combo = QComboBox(footer)
        self._time_combo.addItems(["Frame", "Seconds", "Timecode"])
        self._time_combo.setStyleSheet(COMPOSITE_KEYFRAMES_COMBO_STYLE)
        # Batch 5: connect the combo → view X-axis format.
        self._time_combo.currentTextChanged.connect(self._on_time_mode_changed)
        layout.addWidget(self._time_combo)

        self._value_combo = QComboBox(footer)
        self._value_combo.addItems(["Value", "Normalized"])
        self._value_combo.setStyleSheet(COMPOSITE_KEYFRAMES_COMBO_STYLE)
        # Batch 5: connect the combo → view Y-axis format.
        self._value_combo.currentTextChanged.connect(self._on_value_mode_changed)
        layout.addWidget(self._value_combo)
        return footer

    def _on_time_mode_changed(self, text: str) -> None:
        """Batch 5: map combo label → view time mode and repaint."""
        mode = {"Frame": "frame", "Seconds": "seconds",
                "Timecode": "timecode"}.get(text, "frame")
        self._view.set_time_mode(mode)

    def _on_value_mode_changed(self, text: str) -> None:
        """Batch 5: map combo label → view value mode and repaint."""
        mode = "normalized" if text == "Normalized" else "value"
        self._view.set_value_mode(mode)

    def _on_selected_key_changed(self, payload) -> None:
        """Batch 4: refresh the footer frame/value readout."""
        if payload is None:
            self._sel_frame_label.setText(I18N.tr("spline.frame_empty", "F: —"))
            self._sel_value_label.setText(I18N.tr("spline.value_empty", "V: —"))
        else:
            frame, value = payload
            self._sel_frame_label.setText(f"F: {int(frame)}")
            self._sel_value_label.setText(f"V: {value:.4f}")

    def _set_all_curve_visibility(self, visible: bool) -> None:
        """Toolbar Show/Hide all (AUD-10904: preserve the user's zoom).

        Refit only when "Show all" brings the chart back from fully hidden
        (there is no viewport worth preserving then); otherwise keep the
        current viewport and just repaint.
        """
        had_visible = any(c.visible for c in self._model.curves)
        for curve in self._model.curves:
            self._model.set_visible(curve.curve_id, visible)
        self._tree.refresh()
        if visible and not had_visible:
            self._view.refresh()
        else:
            self._view.update()

    def showEvent(self, event) -> None:
        """T129: when the user opens the standalone Spline entry, redirect
        to the shared Graph Editor (the Keyframes panel's graph mode). The
        first show after construction is the page-enter layout pass and is
        skipped. In the real dock-hosted application the dock action is the
        authoritative user-open signal; Qt may call showEvent repeatedly
        while restoring the layout, so those lifecycle events must not switch
        the Keyframes panel into Graph mode."""
        super().showEvent(event)
        if not self._initial_show_done:
            self._initial_show_done = True
            return
        if not self._dock_hosted:
            self.graph_mode_redirect.emit()

    def set_dock_hosted(self, hosted: bool = True) -> None:
        """Mark whether this panel is managed by the application's dock.

        A dock's visibility lifecycle is not equivalent to a user opening
        the Spline entry. The main-window adapter uses this flag to prevent
        startup/layout ``showEvent`` calls from activating Graph Editor.
        """
        self._dock_hosted = bool(hosted)

    def request_graph_mode_redirect(self) -> None:
        """Redirect an explicit user-open action to the shared Graph Editor."""
        self.graph_mode_redirect.emit()

    # ── transport ──

    def set_frame_range(self, start_frame: int, end_frame: int,
                        fps: Optional[float] = None) -> None:
        """Align Spline transport bounds with the composition clip."""
        self._start_frame = int(start_frame)
        self._end_frame = max(self._start_frame, int(end_frame))
        if fps is not None and float(fps) > 0:
            self._fps = float(fps)
        self._view.set_fps(self._fps)
        self.set_current_frame(
            max(self._start_frame,
                min(self._view._current_frame, self._end_frame)))

    def set_playback_controller(self, controller: Optional[object]) -> None:
        """Bind the same playback engine used by the Keyframes panel."""
        if controller is self._playback_controller:
            return
        if self._playback_slot is not None:
            try:
                self.play_toggle_requested.disconnect(self._playback_slot)
            except (TypeError, RuntimeError):
                pass
            self._playback_slot = None
        self._playback_controller = controller
        if controller is not None:
            try:
                slot = controller.keyframes_panel_play_toggle
                self.play_toggle_requested.connect(slot)
                self._playback_slot = slot
            except (AttributeError, TypeError, RuntimeError):
                self._playback_controller = None

    def set_playing_state(self, playing: bool) -> None:
        playing = bool(playing)
        self._playing = playing
        self._act_play.blockSignals(True)
        self._act_play.setChecked(playing)
        self._act_play.blockSignals(False)
        self._act_play.setIcon(get_icon(
            "fa5s.pause" if playing else "fa5s.play", IconColors.DEFAULT))
        self._act_play.setText(
            I18N.tr("keyframes.pause", "Pause") if playing else
            I18N.tr("keyframes.play", "Play"))
        if not playing and self._play_timer.isActive():
            self._play_timer.stop()

    def set_current_frame(self, frame: int) -> None:
        """Silently sync the Spline playhead from the main composition."""
        frame = max(self._start_frame, min(int(frame), self._end_frame))
        self._view.set_current_frame(frame)

    def seek_to_frame(self, frame: int) -> None:
        frame = max(self._start_frame, min(int(frame), self._end_frame))
        self.set_current_frame(frame)
        self.current_frame_changed.emit(frame)

    def _on_play_toggled(self, checked: bool) -> None:
        if self._playback_controller is not None:
            self.play_toggle_requested.emit(bool(checked))
            return
        if checked:
            if self._view._current_frame >= self._end_frame:
                self.seek_to_frame(self._start_frame)
            self._play_timer.start(max(1, int(1000.0 / self._fps)))
        else:
            self._play_timer.stop()
        self.set_playing_state(checked)

    def _on_internal_play_tick(self) -> None:
        next_frame = self._view._current_frame + 1
        if next_frame > self._end_frame:
            next_frame = self._start_frame
        self.seek_to_frame(next_frame)

    def _seek_nearest_keyframe(self, direction: int) -> None:
        current = self._view._current_frame
        frames = sorted({k.frame for curve in self._model.visible_curves()
                         for k in curve.keys})
        candidates = [frame for frame in frames
                      if (frame < current if direction < 0 else frame > current)]
        if candidates:
            self.seek_to_frame(max(candidates) if direction < 0 else min(candidates))

    # ── public API ──

    def set_node_graph(self, node_graph) -> None:
        # Batch 7: preserve the user's viewport + selected curve across the
        # rebuild. Previously a refresh after every edit fit the view and
        # reset the selection to the first curve, discarding zoom/selection.
        prev_viewport = self._view._viewport
        prev_selected = [c.curve_id for c in self._model.curves if c.selected]
        self._model.rebuild(node_graph)
        self._curve_status.setText(f"{len(self._model.curves)} curves")
        # Restore selection if those curves still exist; else pick the first.
        restored = False
        if prev_selected:
            still = {c.curve_id for c in self._model.curves}
            for c in self._model.curves:
                c.selected = c.curve_id in prev_selected and c.curve_id in still
            if any(c.selected for c in self._model.curves):
                restored = True
        if not restored and self._model.curves:
            for c in self._model.curves:
                c.selected = False
            self._model.curves[0].selected = True
        # Restore the viewport (don't fit) so zoom/pan survives edits.
        if prev_viewport is not None:
            self._view._viewport = prev_viewport
            self._view.update()
        else:
            self._view.fit_selected_view()
        self._tree.refresh()

    # ── P6/T56/T61: shared selection sync with Keyframes ──

    def attach_to_shared_selection(self, selection_model) -> None:
        """Subscribe this panel to the Keyframes panel's shared selection.

        When the user selects a parameter row in Keyframes, the matching
        Spline curve is selected (and the previously-selected one cleared),
        without resetting the viewport (plan §7.2 — 'selection does not
        reset zoom'). Detached panels (no call to this method) keep their
        independent selection, preserving the legacy behaviour the existing
        Spline tests were written against.
        """
        # Detach any prior subscription so re-attaching doesn't double-fire.
        if getattr(self, "_shared_selection_model", None) is not None:
            try:
                self._shared_selection_model.unsubscribe(self._on_shared_selection)
            except Exception:
                pass
        self._shared_selection_model = selection_model
        if selection_model is not None:
            selection_model.subscribe(self._on_shared_selection)
            # Apply the current selection immediately so attaching after the
            # user already selected something in Keyframes still syncs.
            self._on_shared_selection(selection_model.selection)

    def _on_shared_selection(self, selection) -> None:
        """Mirror the Keyframes selection onto the Spline curve selection.

        Picks the first selected parameter row that has a matching curve and
        selects it; clears the rest. When NO parameter row is selected
        (node-only selection, or empty selection), the Spline keeps its
        current curve selection — a node has many curves so its selection is
        ambiguous, and clearing would throw away the user's Spline focus.
        Selection notifications arriving during our own update are ignored
        via a reentry guard.
        """
        if getattr(self, "_syncing_shared_selection", False):
            return
        self._syncing_shared_selection = True
        try:
            from src.features.composite.ui.timeline_view_model import (
                parse_row_id, ROW_PARAM)
            target_curve_ids = set()
            for row_id in sorted(selection.rows):
                parsed = parse_row_id(row_id)
                if parsed.get("row_type") == ROW_PARAM:
                    cid = f"{parsed['node_id']}:{parsed['param_name']}"
                    if self._model.curve_by_id(cid) is not None:
                        target_curve_ids.add(cid)
            # No param row selected → leave the Spline's current curve
            # selection alone (node selection is ambiguous; empty is no-op).
            if not target_curve_ids:
                return
            selected_points = {
                (str(row_id), int(frame))
                for row_id, frame in getattr(selection, "keyframes", set())
            }
            changed = False
            for c in self._model.curves:
                want = c.curve_id in target_curve_ids
                if c.selected != want:
                    c.selected = want
                    changed = True
                # Timeline point selection uses the legacy param row id while
                # Spline curves use ``node_id:param_name``. Translate the
                # shared pairs without disturbing a curve-only selection.
                if selected_points:
                    row_id = f"param:{c.node_id}:{c.param_name}"
                    for key in c.keys:
                        key_want = (row_id, int(key.frame)) in selected_points
                        if key.selected != key_want:
                            key.selected = key_want
                            changed = True
            if changed:
                self._tree.refresh()
                self._view.update()
        finally:
            self._syncing_shared_selection = False

    # ── internal ──

    def _set_tangent(self, mode: str) -> None:
        """Apply a tangent mode to the selected key of the selected curve."""
        for c in self._model.curves:
            if not c.selected:
                continue
            for i, k in enumerate(c.keys):
                if k.selected:
                    set_key_tangent_mode(self._view, c, i, mode)
                    for name, button in self._tangent_buttons.items():
                        button.setChecked(name == mode)
                    return

    def _on_curve_visibility_toggled(self, _curve_id: str,
                                     visible: bool) -> None:
        """Tree checkbox toggled → repaint the chart so the curve hides/shows.

        AUD-10904: keep the user's zoom/pan — ``refresh()`` drops the fitted
        viewport and used to reset the zoom on every checkbox click. Refit
        only when the toggle brings the chart from zero visible curves back
        to at least one (no meaningful viewport to preserve in that case).
        """
        others_visible = any(c.visible for c in self._model.curves
                             if c.curve_id != _curve_id)
        if visible and not others_visible:
            self._view.refresh()
        else:
            self._view.update()

    def _on_curve_selected(self, curve_id: str) -> None:
        """Tree row selected → repaint handles/keys for the new selection.

        AUD-10904: repaint only (``update()``) — selection does not reset
        zoom (plan §7.2), same as the shared-selection path in
        ``_on_shared_selection``.

        T61 (P6 back-sync): also push the curve selection back into the
        shared selection model so the Keyframes panel highlights the
        matching parameter row. The forward direction (Keyframes → Spline)
        is handled by ``_on_shared_selection``; this is the reverse, so the
        two surfaces stay locked regardless of which one the user touched.

        A reentry guard prevents the loop: when the forward handler writes
        the curve selection it sets ``_syncing_shared_selection``, and this
        method checks it before writing back.
        """
        self._view.update()
        if getattr(self, "_syncing_shared_selection", False):
            return
        shared = getattr(self, "_shared_selection_model", None)
        if shared is None:
            return
        # Build the param row_id from the curve_id ("{node_id}:{param_name}")
        # and push it as the sole selected row.
        from src.features.composite.ui.timeline_view_model import make_param_row_id
        parts = curve_id.split(":", 1)
        if len(parts) != 2:
            return
        node_id, param_name = parts
        row_id = make_param_row_id(node_id, param_name)
        try:
            shared.select_row(row_id, additive=False)
        except Exception:
            # The shared model is optional — never let a back-sync failure
            # break the Spline's own selection.
            pass

    def _on_view_frame_changed(self, frame: int) -> None:
        """AUD-10905: ruler playhead drag in the chart seeks the composition.

        Goes through ``seek_to_frame`` so the frame is range-clamped to the
        transport bounds before the panel-level ``current_frame_changed``
        reaches CompositePage (the same path the toolbar transport uses).
        """
        self.seek_to_frame(int(frame))

    def _on_key_selection_changed(self, selection) -> None:
        """Push Spline point selection into the shared timeline model.

        AUD-10906: use the shared model's PUBLIC API only. This handler used
        to write ``shared._selection`` fields directly and call the private
        ``_notify()`` (a no-op inside the batch — the real notification came
        from ``end_batch``), which would silently break if the shared model's
        internals were ever refactored.
        """
        if getattr(self, "_syncing_shared_selection", False):
            return
        shared = getattr(self, "_shared_selection_model", None)
        if shared is None:
            return
        try:
            pairs = {(str(row_id), int(frame))
                     for row_id, frame in selection}
        except (TypeError, ValueError):
            return
        try:
            shared.begin_batch()
            if pairs:
                shared.set_keyframes(sorted(pairs))
                # Mirror the point selection onto the row dimension so the
                # Keyframes panel highlights the affected parameter rows
                # (select_row with additive=False replaces the row set).
                row_ids = sorted({row_id for row_id, _ in pairs})
                for i, row_id in enumerate(row_ids):
                    shared.select_row(row_id, additive=(i > 0))
            else:
                shared.clear_keyframes()
        finally:
            shared.end_batch()
