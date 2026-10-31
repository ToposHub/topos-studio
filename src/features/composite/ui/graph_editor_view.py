"""GraphEditorView — the QPainter canvas for Graph Editor mode (T105/T108/T109).

Paints the value (or speed) graph for the currently-selected parameter(s)
on the Keyframes panel's right side when view_mode == 'graph'. Plan §22.6
defines the fixed paint order; this view follows it.

What lands here (batch 18b):
  * T105 value-graph coordinate transforms (delegated to graph_model)
  * T108 background / minor+major grid / zero line / legend
  * T109 interpolation curves + keyframe points + Bezier handles
  * current-time indicator (playhead) overlay

What's deferred to batch 18c+:
  * T111 zoom/pan (the viewport auto-fits for now)
  * T112 hit-test + marquee
  * T113 selection sync (reads selection; doesn't write yet)
  * T114-T117 single-point / handle drag + Transform Box
  * T110 the bottom control bar (Show Properties / Graph Type / Fit / Snap)

The view is GUI-bearing but its layout math lives in graph_model, so the
data contract is testable headlessly. These tests render under offscreen
and assert "paint completes without throwing" + "non-blank output".
"""

from __future__ import annotations

import math
from typing import List, Optional

from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPainterPath
from PyQt6.QtWidgets import QWidget

from . import graph_model, timeline_layout
from .graph_model import (
    GraphOptions, GraphPoint, GraphSeries, GraphViewport,
    compute_value_bounds, frame_to_x, sample_speed_curve,
    sample_value_curve, value_to_y,
)


# Fixed paint margins (plan §22.6 leaves room for axes + legend).
_MARGIN_LEFT = 40.0     # value-axis labels
_MARGIN_RIGHT = 12.0
_MARGIN_TOP = 12.0
_MARGIN_BOTTOM = 24.0   # frame-axis labels
_KEYFRAME_HIT_RADIUS = 10
_HANDLE_HIT_RADIUS = 8
# T117 Transform Box hit zones.
_BOX_EDGE_TOL = 6.0      # px tolerance for edge hits
_BOX_CORNER_TOL = 8.0    # px tolerance for corner hits
_BOX_ANCHOR_TOL = 9.0    # px tolerance for the anchor handle
_BOX_MIN_SPAN = 3        # px: smaller than this and we don't show the box


def _additive_modifiers(modifiers) -> bool:
    """T78 cross-platform: the modifier(s) that mean 'add to selection'.

    Shift is universal; on macOS Cmd (Meta) is the AE convention for
    additive keyframe selection (Ctrl is rarely used on Mac so we don't
    include it there to avoid hijacking right-click-style chords). On
    Windows/Linux Ctrl is the additive modifier. We accept Shift + either
    Ctrl or Meta everywhere so the user's platform habit works."""
    shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
    ctrl_or_cmd = bool(modifiers & (Qt.KeyboardModifier.ControlModifier |
                                    Qt.KeyboardModifier.MetaModifier))
    return shift or ctrl_or_cmd


class GraphEditorView(QWidget):
    """QPainter canvas drawing the Graph Editor's value/speed curves."""

    # T113: emitted when a keyframe's selection changes from a view click.
    # Payload is the set of (row_id, frame) tuples now selected.
    selection_changed = pyqtSignal(object)
    # T114/T116: emitted when a drag commits an edit. Matches the timeline
    # view's signature (node_id, param_name, keyframe_dicts) so the panel
    # wires it through the same track_changed path.
    keyframe_changed = pyqtSignal(str, str, list)
    # Emitted when the user clicks a keyframe point (row_id, frame).
    point_activated = pyqtSignal(str, float)
    # T123: emitted around a drag's commit burst so the host can wrap the
    # (possibly multi-series) keyframe_changed emits in ONE undo group.
    # drag_about_to_commit fires before the first emit; drag_committed
    # fires after the last. Payload is the count of affected series.
    drag_about_to_commit = pyqtSignal(int)
    drag_committed = pyqtSignal(int)

    def __init__(self, parent: Optional[QWidget] = None,
                 fps: float = 24.0) -> None:
        super().__init__(parent)
        self.setObjectName("GraphEditorView")
        self._fps = max(1.0, float(fps))
        self._series: List[GraphSeries] = []
        self._options = GraphOptions()
        self._viewport = GraphViewport(graph_type="value")
        self._current_frame: int = 0
        # The visible frame range; the view auto-fits to this on rebuild.
        self._frame_range = (0, 100)
        # T111: pan/zoom state. Auto-zoom stays on until the user zooms or
        # pans manually, at which point we stop auto-fitting so their view
        # survives series changes (plan §22.4 auto_zoom_* semantics).
        self._user_adjusted: bool = False
        self._pan_active: bool = False
        self._pan_anchor: Optional[QPointF] = None
        self._WHEEL_ZOOM_FACTOR = 1.2
        # T112: marquee (rubber-band) selection state.
        self._marquee_active: bool = False
        self._marquee_start: Optional[QPointF] = None
        self._marquee_rect: Optional[QRectF] = None
        self._marquee_additive: bool = False
        self._MARQUEE_THRESHOLD = 4
        # T114/T116/T117: keyframe / handle / transform-box drag state.
        # Modes: none | keyframe | handle | box_move | box_scale | box_anchor
        self._drag_mode: str = "none"
        self._drag_handle_side: Optional[str] = None  # "left" | "right"
        self._drag_series: Optional[GraphSeries] = None
        self._drag_point: Optional[GraphPoint] = None
        # Snapshot of (frame, value) for every selected point at drag start
        # so each move recomputes from the press point (no drift).
        self._drag_snapshot: List = []
        # T123: snapshot of BOTH bezier handles at handle-drag start, so an
        # Esc cancel restores the grab + mirrored opposite exactly.
        self._drag_handle_snap: Optional[dict] = None
        # never written to the project. It appears only when ≥2 selected
        # keyframes span ≥2 distinct frames (selection_bbox returns a bbox).
        self._tbox_anchor: Optional[List[float]] = None  # [frame, value]
        self._tbox_bbox: Optional[tuple] = None          # cached bbox
        # box_scale: which edge/corner the user grabbed.
        self._tbox_grip: Optional[str] = None            # n/s/e/w/ne/nw/se/sw
        # box_scale snapshot: original (frame,value) of every selected point
        # + the bbox/anchor at drag start, so moves recompute from the press.
        self._tbox_snap_points: List = []
        self._tbox_snap_bbox: Optional[tuple] = None
        self._tbox_snap_anchor: Optional[List[float]] = None
        self._tbox_cross_param: bool = False  # multi-param → value-frozen
        # T121: shared overlay state — work-area (play range) + markers.
        # Both are pushed from the panel (the canonical owner) so the Graph
        # canvas and the Timeline use the SAME reference points + snap
        # targets (plan §22.9 'Graph 与 Timeline 使用同一参考点和吸附目标').
        self._play_range: Optional[tuple] = None   # (in_frame, out_frame) incl.
        self._markers: List = []                    # TimelineMarker-like
        self.setMinimumSize(200, 150)
        self.setMouseTracking(True)

    # ── public API ──

    def set_fps(self, fps: float) -> None:
        fps = max(1.0, float(fps))
        if abs(fps - self._fps) < 1e-9:
            return
        self._fps = fps
        # D1 fix: invalidate the cached expression context so expressions
        # that read `fps` see the new value. (The parser is stateless, but
        # the context carries the fps field baked in at construction.)
        self._expr_ctx = None
        self._rebuild_samples()
        self.update()

    def set_frame_range(self, start: int, end: int) -> None:
        """The composition's frame range — the view auto-fits its x axis
        (unless the user has zoomed)."""
        start, end = int(start), int(end)
        if (start, end) == self._frame_range:
            return
        self._frame_range = (start, end)
        if not self._user_adjusted:
            self._refit_viewport()
        self.update()

    def set_current_frame(self, frame: int) -> None:
        frame = int(frame)
        if frame == self._current_frame:
            return
        self._current_frame = frame
        self.update()

    def set_play_range(self, play_range: Optional[tuple]) -> None:
        """T121: set the work-area (in/out) overlay. ``play_range`` is
        ``(in_frame, out_frame)`` inclusive, or None to clear. Shared with
        the Timeline so both views show the same reference points."""
        if play_range is None:
            new = None
        else:
            new = (int(play_range[0]), int(play_range[1]))
        if new == self._play_range:
            return
        self._play_range = new
        self.update()

    def play_range(self) -> Optional[tuple]:
        """T121: read-only accessor mirroring the timeline view."""
        return self._play_range

    def set_markers(self, markers: List) -> None:
        """T121: set the marker overlay. Each item is a TimelineMarker-like
        object with at least ``.frame`` and ``.color``/``.name`` attributes
        (or a dict with those keys). Shared with the Timeline."""
        norm = []
        for m in markers or []:
            if isinstance(m, dict):
                norm.append({
                    "frame": int(m.get("frame", 0)),
                    "color": str(m.get("color", "#ff3d3d")),
                    "name": str(m.get("name", "")),
                })
            else:
                norm.append({
                    "frame": int(getattr(m, "frame", 0)),
                    "color": str(getattr(m, "color", "#ff3d3d")),
                    "name": str(getattr(m, "name", "")),
                })
        self._markers = norm
        self.update()

    def markers(self) -> List:
        """T121: read-only accessor for the marker list (normalised dicts)."""
        return list(self._markers)

    def set_series(self, series: List[GraphSeries]) -> None:
        """Replace the plotted series. Triggers a sample rebuild + refit
        (unless the user has zoomed/panned — then their viewport survives).

        F2 fix: if a drag is in flight, abort it first (restore the snapshot)
        so the rebuilt series can't leave _drag_snapshot holding stale
        GraphPoint references that _commit_drag would later write back."""
        self._abort_active_drag()
        self._series = list(series)
        self._refresh_transform_box()
        self._rebuild_samples()
        if not self._user_adjusted:
            self._refit_viewport()
        else:
            # User zoomed: keep their frame range but refit the value axis
            # so the new series stays visible vertically.
            self._refit_value_axis_only()
        self.update()

    def set_options(self, options: GraphOptions) -> None:
        """T119: changing show_post_expression_graph must re-evaluate the
        expression curves, so trigger a rebuild when that flag flips."""
        prev = self._options.show_post_expression_graph
        self._options = options
        if options.show_post_expression_graph != prev:
            self._rebuild_samples()
        self.update()

    def set_graph_type(self, graph_type: str) -> None:
        """Switch the active projection ('value' | 'speed'). 'auto' resolves
        per-series via graph_model.auto_graph_type.

        F3 fix: abort any in-flight drag first, same rationale as set_series."""
        if graph_type not in ("value", "speed", "auto"):
            return
        self._abort_active_drag()
        self._viewport.graph_type = graph_type
        # Per-series graph_type follows the viewport when 'auto'.
        for s in self._series:
            if graph_type == "auto":
                s.graph_type = graph_model.auto_graph_type(
                    s.points[0].parameter_name if s.points
                    else s.display_name)
            else:
                s.graph_type = graph_type
        self._rebuild_samples()
        self._refit_viewport()
        self.update()

    def refresh(self) -> None:
        self.update()

    # ── internals: sampling + viewport ──

    def _rebuild_samples(self) -> None:
        """Re-sample every series' curve for the current viewport + type.

        T119: for series with an expression AND show_post_expression_graph
        enabled, also populate pre/post_expression_samples. An expression-
        only param (no keyframes — expressions & keyframes are mutually
        exclusive here) shows just the static-value pre line + the post
        expression curve, with no editable keyframe points."""
        start, end = self._frame_range
        ppf = max(1e-9, self._viewport.pixels_per_frame)
        show_post = self._options.show_post_expression_graph
        for s in self._series:
            # T119: expression curves are evaluated for value graphs with an
            # active expression + the toggle on, whether or not the series
            # has keyframes. (An expression-only param has zero keyframes.)
            want_expr = (show_post and s.graph_type != "speed"
                         and s.has_expression and s.expression)
            if not s.points:
                # No keyframes → no editable keyframe curve. The pre baseline
                # is the static value; the post curve is the expression.
                s.samples = []
                if want_expr:
                    pre, post, err = graph_model.sample_expression_curves(
                        [], s.expression, start, end, ppf,
                        fps=self._fps,
                        static_value=s.static_value,
                        context=self._expression_context(),
                        parser=self._expression_parser())
                    s.pre_expression_samples = pre
                    s.post_expression_samples = post
                    s.expression_error = err
                else:
                    s.pre_expression_samples = []
                    s.post_expression_samples = []
                    s.expression_error = False
                continue
            if s.graph_type == "speed":
                s.samples = sample_speed_curve(s.points, start, end, ppf,
                                               fps=self._fps)
            else:
                s.samples = sample_value_curve(s.points, start, end, ppf,
                                               fps=self._fps)
            # T119: pre/post expression curves (only for value graphs with
            # an active expression + the toggle on).
            if want_expr:
                pre, post, err = graph_model.sample_expression_curves(
                    s.points, s.expression, start, end, ppf,
                    fps=self._fps,
                    static_value=s.static_value,
                    context=self._expression_context(),
                    parser=self._expression_parser())
                s.pre_expression_samples = pre
                s.post_expression_samples = post
                s.expression_error = err
            else:
                s.pre_expression_samples = []
                s.post_expression_samples = []
                s.expression_error = False

    def _expression_context(self):
        """T119: build a fresh ExpressionContext for per-sample evaluation.
        Cached per-view; the per-sample frame/time/value are set by the
        sampler. Returns None if the evaluator can't be imported."""
        cached = getattr(self, "_expr_ctx", None)
        if cached is not None:
            return cached
        try:
            from src.features.composite.utils.expression_parser import \
                ExpressionContext
            ctx = ExpressionContext(fps=self._fps)
            self._expr_ctx = ctx
            return ctx
        except Exception:
            return None

    def _expression_parser(self):
        """T119: a cached ExpressionParser instance (or None)."""
        cached = getattr(self, "_expr_parser", None)
        if cached is not None:
            return cached
        try:
            from src.features.composite.utils.expression_parser import \
                ExpressionParser
            p = ExpressionParser()
            self._expr_parser = p
            return p
        except Exception:
            return None

    def _refit_viewport(self) -> None:
        """Auto-fit the x axis to the frame range and the y axis to the
        visible samples (plan §22.4 auto_zoom_*)."""
        w = max(1, self.width())
        h = max(1, self.height())
        start, end = self._frame_range
        plot_w = max(1, w - _MARGIN_LEFT - _MARGIN_RIGHT)
        self._viewport.frame_start = float(start)
        self._viewport.frame_end = float(max(start + 1, end))
        span = max(1.0, self._viewport.frame_end - self._viewport.frame_start)
        self._viewport.pixels_per_frame = plot_w / span

        # Value bounds from all visible samples + keyframe points.
        all_samples: List = []
        all_points: List[GraphPoint] = []
        for s in self._series:
            all_samples.extend(s.samples)
            all_points.extend(s.points)
        lo, hi = compute_value_bounds(all_samples, all_points)
        self._viewport.value_min = lo
        self._viewport.value_max = hi
        plot_h = max(1, h - _MARGIN_TOP - _MARGIN_BOTTOM)
        val_span = max(1e-9, hi - lo)
        self._viewport.pixels_per_value = plot_h / val_span

    def _refit_value_axis_only(self) -> None:
        """Refit only the y axis (used after a series change when the user
        has zoomed the time axis — keep their zoom, refit the value range
        so the new series is vertically visible)."""
        h = max(1, self.height())
        all_samples: List = []
        all_points: List[GraphPoint] = []
        for s in self._series:
            all_samples.extend(s.samples)
            all_points.extend(s.points)
        lo, hi = compute_value_bounds(all_samples, all_points)
        self._viewport.value_min = lo
        self._viewport.value_max = hi
        plot_h = max(1, h - _MARGIN_TOP - _MARGIN_BOTTOM)
        val_span = max(1e-9, hi - lo)
        self._viewport.pixels_per_value = plot_h / val_span

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Re-fit on resize so the graph fills the new geometry.
        self._refit_viewport()

    # ── T111: viewport zoom/pan ──

    def wheelEvent(self, event) -> None:
        """Zoom the time axis around the cursor (plan §22.4 'auto_zoom_time').

        Plain wheel = zoom; the value axis auto-refits. Alt+wheel zooms the
        value axis instead. Marks the view as user-adjusted so subsequent
        series changes don't auto-refit and discard the zoom.
        """
        from PyQt6.QtGui import QWheelEvent
        if not isinstance(event, QWheelEvent):
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return
        factor = self._WHEEL_ZOOM_FACTOR if delta > 0 else \
            1.0 / self._WHEEL_ZOOM_FACTOR
        alt = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
        if alt:
            self._zoom_value(factor)
        else:
            cursor_x = float(event.position().x())
            self._zoom_time(factor, cursor_x)
        self._user_adjusted = True
        self._rebuild_samples()
        self.update()
        event.accept()

    def _zoom_time(self, factor: float, cursor_x: float) -> None:
        """Zoom the time axis so the frame under ``cursor_x`` stays put."""
        plot_left = _MARGIN_LEFT
        ppf = self._viewport.pixels_per_frame * factor
        # Clamp to a sane range (matches FrameMapper's clamp).
        ppf = max(0.05, min(256.0, ppf))
        if ppf == self._viewport.pixels_per_frame:
            return
        # Anchor: keep the frame under the cursor stable.
        anchor_frame = graph_model.x_to_frame(
            cursor_x, self._viewport, plot_left)
        self._viewport.pixels_per_frame = ppf
        # Recompute frame_start so the anchor frame maps to the same x.
        # frame_to_x(f) = plot_left + (f - frame_start) * ppf == cursor_x
        # → frame_start = anchor_frame - (cursor_x - plot_left) / ppf
        self._viewport.frame_start = anchor_frame - \
            (cursor_x - plot_left) / ppf
        self._viewport.frame_end = self._viewport.frame_start + \
            (self.width() - _MARGIN_LEFT - _MARGIN_RIGHT) / ppf

    def _zoom_value(self, factor: float) -> None:
        """Zoom the value axis around its centre (no cursor anchor)."""
        ppv = self._viewport.pixels_per_value * factor
        ppv = max(1e-6, ppv)
        if ppv == self._viewport.pixels_per_value:
            return
        # Keep the value-range midpoint stable.
        mid = (self._viewport.value_min + self._viewport.value_max) / 2.0
        half_range = (self.height() - _MARGIN_TOP - _MARGIN_BOTTOM) / 2.0 / ppv
        self._viewport.value_min = mid - half_range
        self._viewport.value_max = mid + half_range
        self._viewport.pixels_per_value = ppv

    def mousePressEvent(self, event) -> None:
        """Middle/right button starts a viewport pan (AE/NLE convention).
        Left button: T112 keyframe hit-test + selection, or start a marquee
        (rubber-band) on empty plot area.

        A5 fix: a mid/right press during an active left-button drag is
        IGNORED (not turned into a pan) — otherwise the pan hijacks the
        gesture, leaves _drag_mode dangling, and the next move jumps hugely
        because the drag's press origin is now stale."""
        if event.button() in (Qt.MouseButton.MiddleButton,
                              Qt.MouseButton.RightButton):
            if self._drag_mode != "none" or self._marquee_active:
                # A drag/marquee is in progress — don't let a mid/right click
                # hijack it. Swallow the press so the ongoing drag continues.
                event.accept()
                return
            self._pan_active = True
            self._pan_anchor = QPointF(event.position())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        sp = QPointF(event.position())
        # T116: handle hit-test first (plan §22.6 'handle 优先于 keyframe').
        handle_hit = self._hit_handle(sp)
        if handle_hit is not None:
            series, point, side = handle_hit
            # Handles only exist on selected keyframes; select it if needed.
            if not point.selected:
                self._select_point(series, point, additive=False)
            self._begin_handle_drag(series, point, side, sp)
            event.accept()
            return
        # T112: hit-test keyframes next.
        hit = self._hit_keyframe(sp)
        if hit is not None:
            series, point = hit
            additive = _additive_modifiers(event.modifiers())
            already_sel = point.selected
            self._select_point(series, point, additive=additive)
            self.point_activated.emit(series.row_id, point.frame)
            # T114: begin a keyframe drag. Only when the point was already
            # selected (AE group-drag) or just got sole-selected by this
            # click — not when Shift-toggling it off.
            if point.selected and (already_sel or not additive):
                self._begin_keyframe_drag(sp)
            event.accept()
            return
        # T117: Transform Box hit-test (only when a box is visible — ≥2
        # selected keyframes spanning ≥2 frames). Checked AFTER keyframes
        # so clicking a point inside the box still selects the point; the
        # box's edges/corners/anchor are hit only on empty space.
        box_hit = self._hit_transform_box(sp)
        if box_hit is not None:
            grip = box_hit  # 'inside' | 'anchor' | n/s/e/w/ne/nw/se/sw
            if grip == "anchor":
                self._begin_anchor_drag(sp)
            elif grip == "inside":
                self._begin_box_move_drag(sp)
            else:
                self._begin_box_scale_drag(sp, grip)
            event.accept()
            return
        # No keyframe hit → start a pending marquee (becomes a real marquee
        # once the cursor moves past the threshold; a plain click without
        # movement clears the selection).
        self._marquee_active = True
        self._marquee_start = sp
        self._marquee_rect = None
        self._marquee_additive = bool(
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._pan_active and self._pan_anchor is not None:
            sp = QPointF(event.position())
            dx = sp.x() - self._pan_anchor.x()
            dy = sp.y() - self._pan_anchor.y()
            self._pan_anchor = sp
            # Pan time axis (dx) and value axis (dy). 1:1 pixel drag.
            ppf = max(1e-9, self._viewport.pixels_per_frame)
            ppv = max(1e-9, self._viewport.pixels_per_value)
            self._viewport.frame_start -= dx / ppf
            self._viewport.frame_end -= dx / ppf
            # Screen-down = value-up, so invert dy.
            self._viewport.value_min += dy / ppv
            self._viewport.value_max += dy / ppv
            self._user_adjusted = True
            self._rebuild_samples()
            self.update()
            event.accept()
            return
        if self._drag_mode == "keyframe":
            self._do_keyframe_drag(QPointF(event.position()))
            event.accept()
            return
        if self._drag_mode == "handle":
            self._do_handle_drag(QPointF(event.position()))
            event.accept()
            return
        if self._drag_mode == "box_move":
            self._do_box_move_drag(QPointF(event.position()))
            event.accept()
            return
        if self._drag_mode == "box_scale":
            self._do_box_scale_drag(QPointF(event.position()))
            event.accept()
            return
        if self._drag_mode == "box_anchor":
            self._do_anchor_drag(QPointF(event.position()))
            event.accept()
            return
        if self._marquee_active and self._marquee_start is not None:
            sp = QPointF(event.position())
            moved = (sp - self._marquee_start).manhattanLength()
            if moved > self._MARQUEE_THRESHOLD:
                self._marquee_rect = QRectF(self._marquee_start, sp).normalized()
                self._apply_marquee_selection()
            self.update()
            event.accept()
            return

    def mouseReleaseEvent(self, event) -> None:
        if self._pan_active:
            self._pan_active = False
            self._pan_anchor = None
            self.unsetCursor()
            event.accept()
            return
        if self._drag_mode in ("keyframe", "handle",
                               "box_move", "box_scale"):
            self._commit_drag()
            event.accept()
            return
        if self._drag_mode == "box_anchor":
            # Anchor drag doesn't edit keyframes — just end the gesture.
            self._drag_mode = "none"
            self._tbox_grip = None
            self.unsetCursor()
            event.accept()
            return
        if self._marquee_active:
            # If the mouse never crossed the threshold, this was a plain
            # click on empty area → clear the selection (unless Shift held).
            if self._marquee_rect is None and not self._marquee_additive:
                self._clear_selection()
            self._marquee_active = False
            self._marquee_start = None
            self._marquee_rect = None
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _abort_active_drag(self) -> None:
        """F2/F3: if a drag/marquee is in flight when the series is rebuilt
        (e.g. an external graph edit refreshes the view mid-drag), restore
        the pre-drag snapshot and clear drag state. Unlike _cancel_drag this
        is silent (no repaint of its own — the caller's rebuild repaints).
        This prevents _drag_snapshot from holding stale GraphPoint references
        that _commit_drag would write back to the wrong row_id."""
        if self._drag_mode == "none" and not self._marquee_active:
            return
        # Restore moved points/handles/anchor from their snapshots (same logic
        # as _cancel_drag, kept inline to avoid a repaint here).
        if self._drag_mode == "keyframe":
            for _s, kp, of, ov in self._drag_snapshot:
                kp.frame, kp.value = float(of), float(ov)
        elif self._drag_mode == "handle" and self._drag_point is not None \
                and self._drag_handle_snap is not None:
            kp = self._drag_point
            snap = self._drag_handle_snap
            kp.handle_left = snap.get("left")
            kp.handle_right = snap.get("right")
        elif self._drag_mode in ("box_move", "box_scale"):
            for entry in self._tbox_snap_points:
                _s, kp, of, ov = entry
                kp.frame, kp.value = float(of), float(ov)
        elif self._drag_mode == "box_anchor" and \
                self._tbox_snap_anchor is not None:
            self._tbox_anchor = list(self._tbox_snap_anchor)
        self._drag_mode = "none"
        self._marquee_active = False
        self._marquee_start = None
        self._marquee_rect = None
        self._clear_drag_state()

    def keyPressEvent(self, event) -> None:
        """T123: Esc cancels an in-flight drag, restoring the pre-drag
        snapshot without emitting keyframe_changed (so the node isn't
        mutated and no undo entry is recorded). The view must have focus
        during the drag for this to fire; the panel ensures that.

        A1 fix: box_anchor drags are now cancellable too — the anchor
        restores from _tbox_snap_anchor instead of being stranded at the
        cursor position."""
        if event.key() == Qt.Key.Key_Escape and \
                self._drag_mode in ("keyframe", "handle",
                                    "box_move", "box_scale", "box_anchor"):
            self._cancel_drag()
            event.accept()
            return
        super().keyPressEvent(event)

    def _cancel_drag(self) -> None:
        """T123: restore every selected point + handle to its pre-drag
        position from the active snapshot, then reset drag state. Does NOT
        emit keyframe_changed — the cancelled drag is discarded entirely
        (no node write, no undo record)."""
        if self._drag_mode == "keyframe":
            for series, kp, of, ov in self._drag_snapshot:
                kp.frame = float(of)
                kp.value = float(ov)
            for series, _kp, _of, _ov in self._drag_snapshot:
                series.points.sort(key=lambda p: p.frame)
        elif self._drag_mode == "handle" and self._drag_point is not None \
                and self._drag_handle_snap is not None:
            kp = self._drag_point
            snap = self._drag_handle_snap
            kp.handle_left = snap.get("left")
            kp.handle_right = snap.get("right")
        elif self._drag_mode in ("box_move", "box_scale"):
            # _tbox_snap_points entries are (series, kp, orig_frame,
            # orig_value) — restore each.
            for entry in self._tbox_snap_points:
                series, kp, of, ov = entry
                kp.frame = float(of)
                kp.value = float(ov)
            for entry in self._tbox_snap_points:
                series = entry[0]
                series.points.sort(key=lambda p: p.frame)
        elif self._drag_mode == "box_anchor" and \
                self._tbox_snap_anchor is not None:
            # A1 fix: restore the anchor to its pre-drag position. The anchor
            # doesn't move keyframes, so this only needs to restore _tbox_anchor.
            self._tbox_anchor = list(self._tbox_snap_anchor)
        # Reset all drag state (mirrors _commit_drag's cleanup tail).
        self._drag_mode = "none"
        self._drag_snapshot = []
        self._tbox_snap_points = []
        self._tbox_grip = None
        self._drag_series = None
        self._drag_point = None
        self._drag_handle_side = None
        self._drag_handle_snap = None
        self._drag_opp_handle = None
        self._refresh_transform_box()
        self._rebuild_samples()
        self.unsetCursor()
        self.update()

    # ── T112: hit-testing ──

    def _hit_keyframe(self, sp: QPointF):
        """Return (series, GraphPoint) under ``sp``, or None.

        Plan §22.6: 'handle 优先于 keyframe, keyframe 优先于 curve segment'.
        Handles aren't tested here (T116); only keyframe points. The first
        hit wins (topmost series paints last, so iterate in reverse).
        """
        radius_sq = (_KEYFRAME_HIT_RADIUS * _KEYFRAME_HIT_RADIUS)
        for s in reversed(self._series):
            for kp in s.points:
                x = frame_to_x(kp.frame, self._viewport, _MARGIN_LEFT)
                y = value_to_y(kp.value, self._viewport,
                               self._plot_bottom())
                dx = sp.x() - x
                dy = sp.y() - y
                if dx * dx + dy * dy <= radius_sq:
                    return s, kp
        return None

    def _hit_handle(self, sp: QPointF):
        """T116: return (series, GraphPoint, side) when ``sp`` is over a
        bezier handle, or None. Only handles of selected bezier keyframes
        are hit-testable (you can't grab a handle you can't see)."""
        radius_sq = (_HANDLE_HIT_RADIUS * _HANDLE_HIT_RADIUS)
        for s in reversed(self._series):
            for kp in s.points:
                if not kp.selected:
                    continue
                if kp.interpolation_in not in ("bezier",) and \
                        kp.interpolation_out not in ("bezier",):
                    continue
                for side, handle in (("left", kp.handle_left),
                                     ("right", kp.handle_right)):
                    if handle is None:
                        continue
                    hf = kp.frame + handle[0]
                    hv = kp.value + handle[1]
                    hx = frame_to_x(hf, self._viewport, _MARGIN_LEFT)
                    hy = value_to_y(hv, self._viewport, self._plot_bottom())
                    dx = sp.x() - hx
                    dy = sp.y() - hy
                    if dx * dx + dy * dy <= radius_sq:
                        return s, kp, side
        return None

    # ── T114/T116: drags ──

    def _begin_keyframe_drag(self, sp: QPointF) -> None:
        """T114: start a multi-keyframe drag. Snapshots every selected
        point's (frame, value) so each move recomputes from the press."""
        self._drag_mode = "keyframe"
        self._drag_handle_side = None
        # Record the press frame/value as the drag origin.
        self._drag_press_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        self._drag_press_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        snap = []
        for s in self._series:
            for kp in s.points:
                if kp.selected:
                    snap.append((s, kp, kp.frame, kp.value))
        self._drag_snapshot = snap

    def _begin_handle_drag(self, series, point, side: str,
                           sp: QPointF) -> None:
        """T116: start a single-handle drag. Only the grabbed handle moves;
        the opposite handle is untouched (broken mode) or mirrors (smooth)."""
        self._drag_mode = "handle"
        self._drag_series = series
        self._drag_point = point
        self._drag_handle_side = side
        self._drag_press_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        self._drag_press_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        # Snapshot the opposite handle so smooth-mirror can restore it.
        opp = point.handle_right if side == "left" else point.handle_left
        self._drag_opp_handle = tuple(opp) if opp else None
        # T123: snapshot BOTH handles' pre-drag positions so an Esc cancel
        # can restore them exactly (the grab + the mirrored opposite).
        grabbed = point.handle_left if side == "left" else point.handle_right
        self._drag_handle_snap = {
            "left": tuple(point.handle_left) if point.handle_left else None,
            "right": tuple(point.handle_right) if point.handle_right else None,
        }
        _ = grabbed  # documented for clarity; the snap dict holds both

    def _do_keyframe_drag(self, sp: QPointF) -> None:
        """T114/T115: move every selected point by the cursor's delta.

        In a Speed Graph (T115) the Y axis is derived (slope/speed), so
        dragging a keyframe moves only its time — the value component is
        dropped to avoid mis-editing the original keyframe value (plan
        §22.7 '拖动关键帧 Speed Graph 默认移动时间')."""
        cur_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        cur_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        df = cur_frame - self._drag_press_frame
        dv = cur_value - self._drag_press_value
        # T115: speed-graph drag is frame-only.
        allow_value = not graph_model.is_speed_drag(
            [s for s, _k, _of, _ov in self._drag_snapshot]
            if self._drag_snapshot else self._series)
        if not allow_value:
            dv = 0.0
        if abs(df) < 0.01 and abs(dv) < 1e-9:
            return
        snap_on = getattr(self._options, "snap_enabled", False)
        for series, kp, orig_frame, orig_value in self._drag_snapshot:
            # Frame is an integer (keyframe frames are whole numbers). When
            # snap is on, snap the dragged target to the shared targets
            # (work-area edges + markers + other keyframes — plan §22.9).
            target = orig_frame + df
            new_frame = self._snap_frame(target) if snap_on else \
                int(round(target))
            new_value = orig_value + dv
            # Clamp frame to >= 0 (no negative keyframe frames).
            kp.frame = max(0, new_frame)
            kp.value = new_value
        # Re-sort each affected series by frame so the curve stays ordered.
        for series, _kp, _of, _ov in self._drag_snapshot:
            series.points.sort(key=lambda p: p.frame)
        self._rebuild_samples()
        self.update()

    def _do_handle_drag(self, sp: QPointF) -> None:
        """T116: move the grabbed handle to the cursor's data position."""
        kp = self._drag_point
        if kp is None:
            return
        cur_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        cur_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        # Handle is stored as a (df, dv) delta from the keyframe.
        hdf = cur_frame - kp.frame
        hdv = cur_value - kp.value
        if self._drag_handle_side == "left":
            kp.handle_left = (hdf, hdv)
            # Smooth tangent: mirror the right handle horizontally so the
            # slope stays continuous. Broken mode (tangent_mode='broken')
            # leaves the right handle alone.
            if (kp.tangent_mode != "broken"
                    and self._drag_opp_handle is not None):
                kp.handle_right = (-hdf, -hdv)
        else:
            kp.handle_right = (hdf, hdv)
            if (kp.tangent_mode != "broken"
                    and self._drag_opp_handle is not None):
                kp.handle_left = (-hdf, -hdv)
        self._rebuild_samples()
        self.update()

    def _series_for(self, row_id: str) -> Optional[GraphSeries]:
        """Look up a series by its row_id (or None)."""
        return next((s for s in self._series if s.row_id == row_id), None)

    def _commit_drag(self) -> None:
        """T114/T116/T117: emit keyframe_changed for every series whose
        points moved, so the panel persists + pushes undo. One emit per
        series. Handles keyframe drag, handle drag, and Transform Box
        move/scale drags (which snapshot into _tbox_snap_points).

        F4 fix: a click-press-release with NO movement (no point actually
        changed) emits nothing and records no undo entry — matching AE,
        where a plain click on a keyframe only selects, never mutates."""
        affected = set()
        # Keyframe drag: every series in the snapshot.
        for series, _kp, _of, _ov in self._drag_snapshot:
            affected.add(series.row_id)
        # Handle drag: the single dragged series.
        if self._drag_point is not None and self._drag_series is not None:
            affected.add(self._drag_series.row_id)
        # T117 Transform Box: every series touched by the box snapshot.
        for entry in self._tbox_snap_points:
            # Snapshot entries are (series, kp, orig_frame, orig_value).
            series = entry[0]
            affected.add(series.row_id)
        self._drag_mode = "none"
        # Re-sort every affected series so the curve stays frame-ordered.
        for row_id in affected:
            series = next((s for s in self._series if s.row_id == row_id),
                          None)
            if series is not None and series.points:
                series.points.sort(key=lambda p: p.frame)
        # F4 fix: detect whether anything actually moved. If no point/handle
        # changed vs its snapshot, skip the emit burst entirely (no spurious
        # keyframe_changed, no undo entry) — a no-op click just selects.
        if not self._drag_actually_changed():
            self._clear_drag_state()
            return
        # T123: bracket the emit burst so the host wraps it in ONE undo
        # group (a multi-series Transform Box drag becomes a single undo
        # entry, plan §22.1 '每次拖动/批量变换一次 undo').
        emit_targets = [rid for rid in affected
                        if self._series_for(rid) is not None]
        if emit_targets:
            self.drag_about_to_commit.emit(len(emit_targets))
        # Bug-2 fix: wrap the emit burst in try/finally so the matching
        # drag_committed is ALWAYS fired — even if a downstream slot raises.
        # Without this, an exception in the track_changed chain would leave
        # the host's undo stack permanently locked in begin_group mode.
        try:
            for row_id in emit_targets:
                series = next((s for s in self._series if s.row_id == row_id),
                              None)
                if series is None or not series.points:
                    continue
                # Rebuild the keyframe dict list the panel persists.
                kfs = []
                for kp in sorted(series.points, key=lambda p: p.frame):
                    d = {"frame": int(kp.frame), "value": float(kp.value),
                         "interpolation": kp.interpolation_in}
                    if kp.handle_left is not None:
                        d["bezier_in"] = list(kp.handle_left)
                    if kp.handle_right is not None:
                        d["bezier_out"] = list(kp.handle_right)
                    kfs.append(d)
                # node_id and param_name come from the series row_id grammar.
                parts = row_id.split(":", 2)
                if len(parts) == 3 and parts[0] == "param":
                    self.keyframe_changed.emit(parts[1], parts[2], kfs)
        finally:
            # T123: close the undo group opened above (always, even on error).
            if emit_targets:
                self.drag_committed.emit(len(emit_targets))
        self._clear_drag_state()

    def _clear_drag_state(self) -> None:
        """Reset all drag-related fields after a commit or a no-op cancel.
        Mirrors the tail of _commit_drag / _cancel_drag."""
        self._drag_snapshot = []
        self._tbox_snap_points = []
        self._tbox_grip = None
        self._drag_series = None
        self._drag_point = None
        self._drag_handle_side = None
        self._drag_handle_snap = None
        self._drag_opp_handle = None
        self.unsetCursor()
        # Refresh the cached box bbox for the (possibly moved) selection.
        self._refresh_transform_box()

    def _drag_actually_changed(self) -> bool:
        """F4: return True only when at least one dragged point or handle
        actually moved vs its pre-drag snapshot. A click-press-release with
        no movement returns False so the commit can be skipped (no spurious
        keyframe_changed, no undo entry)."""
        changed = False
        # Keyframe drag / Transform Box: compare each snapshotted point's
        # current (frame, value) to its snapshot (orig_frame, orig_value).
        for series, kp, of, ov in self._drag_snapshot:
            if abs(kp.frame - of) > 1e-9 or abs(kp.value - ov) > 1e-9:
                changed = True
                break
        if not changed:
            for entry in self._tbox_snap_points:
                _series, kp, of, ov = entry
                if abs(kp.frame - of) > 1e-9 or abs(kp.value - ov) > 1e-9:
                    changed = True
                    break
        # Handle drag: compare current handles to the snapshot. _drag_mode is
        # already "none" by the time we get here, so we key off _drag_point +
        # _drag_handle_snap being populated (they're cleared in
        # _clear_drag_state, which runs AFTER this check).
        if not changed and self._drag_point is not None and \
                self._drag_handle_snap is not None:
            snap = self._drag_handle_snap
            kp = self._drag_point
            for side, cur in (("left", kp.handle_left),
                              ("right", kp.handle_right)):
                snap_h = snap.get(side)
                if snap_h is None and cur is None:
                    continue
                if snap_h is None or cur is None:
                    changed = True
                    break
                if abs(cur[0] - snap_h[0]) > 1e-9 or \
                        abs(cur[1] - snap_h[1]) > 1e-9:
                    changed = True
                    break
        return changed

    # ── T113: selection ──

    def _select_point(self, series, point, additive: bool) -> None:
        """Select (or toggle) a single keyframe. Emits selection_changed."""
        if additive:
            point.selected = not point.selected
        else:
            # Single-select: clear everything else first.
            for s in self._series:
                for kp in s.points:
                    kp.selected = False
            point.selected = True
        self._refresh_transform_box()
        self._emit_selection()
        self.update()

    def _clear_selection(self) -> None:
        changed = any(kp.selected for s in self._series for kp in s.points)
        for s in self._series:
            for kp in s.points:
                kp.selected = False
        if changed:
            self._emit_selection()
        self._refresh_transform_box()
        self.update()

    # ── T121: shared snap (work-area edges + markers + other keyframes) ─

    _SNAP_PX_TOL = 8.0  # px tolerance, mirroring the Timeline view

    def _snap_frame(self, frame: float) -> int:
        """T121: snap ``frame`` to the nearest snap target when the snap
        toggle is on. Candidates mirror the Timeline's _snap_frame (plan
        §22.9 'Graph 与 Timeline 使用同一参考点和吸附目标'):

          * integer frames (always, via rounding);
          * work-area In/Out edges (when a play range is set);
          * markers (shared with the Timeline);
          * every other selected series' keyframe frames.

        Returns the integer frame (keyframe frames are whole numbers)."""
        # Always include the rounded integer as a baseline candidate.
        candidates = [int(round(frame))]
        ppf = max(1e-9, self._viewport.pixels_per_frame)
        tol = self._SNAP_PX_TOL / ppf  # frame tolerance at this zoom
        # Work-area edges.
        if self._play_range is not None:
            candidates.extend(self._play_range)
        # Markers.
        for m in self._markers:
            candidates.append(int(m.get("frame", 0)))
        # Other keyframes on visible series (snap to neighbours).
        for s in self._series:
            for kp in s.points:
                candidates.append(int(kp.frame))
        # Pick the closest candidate within tolerance.
        best = int(round(frame))
        best_d = tol + 1.0
        for c in candidates:
            d = abs(c - frame)
            if d < best_d:
                best_d = d
                best = c
        return best if best_d <= tol else int(round(frame))

    # ── T117: Transform Box ───────────────────────────────────────────

    def _refresh_transform_box(self) -> None:
        """Recompute the cached bbox for the current selection. The box only
        exists when ≥2 selected keyframes span ≥2 distinct frames. When the
        selection collapses, the box (and the anchor) are cleared."""
        bbox = graph_model.selection_bbox(self._series)
        self._tbox_bbox = bbox
        if bbox is None:
            # No usable box → drop the anchor so _hit_transform_box no-ops.
            self._tbox_anchor = None
            return
        # Keep the user's anchor if it still lies inside the new bbox;
        # otherwise reset to the centre. A None anchor defaults to centre.
        if self._tbox_anchor is not None:
            af, av = self._tbox_anchor
            fmin, fmax, vmin, vmax = bbox
            if not (fmin <= af <= fmax and vmin <= av <= vmax):
                self._tbox_anchor = list(graph_model.transform_box_anchor(bbox))
        else:
            self._tbox_anchor = list(graph_model.transform_box_anchor(bbox))

    def _box_rect_px(self) -> Optional[QRectF]:
        """The box's pixel rectangle (margin-inflated), or None when hidden."""
        bbox = self._tbox_bbox
        if bbox is None:
            return None
        fmin, fmax, vmin, vmax = bbox
        x0 = frame_to_x(fmin, self._viewport, _MARGIN_LEFT)
        x1 = frame_to_x(fmax, self._viewport, _MARGIN_LEFT)
        y0 = value_to_y(vmax, self._viewport, self._plot_bottom())  # top = max
        y1 = value_to_y(vmin, self._viewport, self._plot_bottom())  # bot = min
        # Inflated margin so edges/corners are grabbable outside the points.
        return QRectF(x0 - _BOX_CORNER_TOL, y0 - _BOX_CORNER_TOL,
                      (x1 - x0) + 2 * _BOX_CORNER_TOL,
                      (y1 - y0) + 2 * _BOX_CORNER_TOL)

    def _box_core_rect_px(self) -> Optional[QRectF]:
        """The box's tight pixel rectangle (the actual selection bounds),
        without the grab margin. Used for drawing the box outline."""
        bbox = self._tbox_bbox
        if bbox is None:
            return None
        fmin, fmax, vmin, vmax = bbox
        x0 = frame_to_x(fmin, self._viewport, _MARGIN_LEFT)
        x1 = frame_to_x(fmax, self._viewport, _MARGIN_LEFT)
        y0 = value_to_y(vmax, self._viewport, self._plot_bottom())
        y1 = value_to_y(vmin, self._viewport, self._plot_bottom())
        return QRectF(x0, y0, x1 - x0, y1 - y0)

    def _hit_transform_box(self, sp: QPointF) -> Optional[str]:
        """T117: return the grip under ``sp`` or None. Grip values:
        'anchor' | 'inside' | 'n'|'s'|'e'|'w'|'ne'|'nw'|'se'|'sw'.

        Hit order: anchor first (it sits on top), then corners, then edges,
        then the interior. Corners beat edges (a corner is also on two
        edges). Returns None when no box is visible.

        B1/B4 fix: edges use _BOX_EDGE_TOL (6 px) and corners use
        _BOX_CORNER_TOL (8 px); the interior is reachable as long as the
        click is strictly inside the rect and NOT within the edge tolerance
        on the orthogonal axis. This keeps narrow boxes (< 2*corner tol)
        moveable instead of having both vertical edges claim every interior
        click."""
        rect = self._box_core_rect_px()
        if rect is None:
            return None
        # Anchor handle: a small circle at the anchor position.
        if self._tbox_anchor is not None:
            af, av = self._tbox_anchor
            ax = frame_to_x(af, self._viewport, _MARGIN_LEFT)
            ay = value_to_y(av, self._viewport, self._plot_bottom())
            if (sp - QPointF(ax, ay)).manhattanLength() <= _BOX_ANCHOR_TOL:
                return "anchor"
        x, y = sp.x(), sp.y()
        # Distance to each edge.
        d_left = abs(x - rect.left())
        d_right = abs(x - rect.right())
        d_top = abs(y - rect.top())
        d_bot = abs(y - rect.bottom())
        # A click is "on" an edge when within EDGE tolerance; it's a corner
        # when within CORNER tolerance on BOTH axes (corners are fatter).
        on_left = d_left <= _BOX_EDGE_TOL
        on_right = d_right <= _BOX_EDGE_TOL
        on_top = d_top <= _BOX_EDGE_TOL
        on_bot = d_bot <= _BOX_EDGE_TOL
        near_left_c = d_left <= _BOX_CORNER_TOL
        near_right_c = d_right <= _BOX_CORNER_TOL
        near_top_c = d_top <= _BOX_CORNER_TOL
        near_bot_c = d_bot <= _BOX_CORNER_TOL
        # Corners (within corner tolerance on both axes).
        if near_top_c and near_left_c:
            return "nw"
        if near_top_c and near_right_c:
            return "ne"
        if near_bot_c and near_left_c:
            return "sw"
        if near_bot_c and near_right_c:
            return "se"
        # Edges (within edge tolerance on one axis only).
        if on_top:
            return "n"
        if on_bot:
            return "s"
        if on_left:
            return "w"
        if on_right:
            return "e"
        # Interior: strictly inside the rect and not on any edge. Because
        # edges now use the narrower EDGE tolerance, a click near the centre
        # of a narrow box still reaches here (B4).
        if rect.contains(sp):
            return "inside"
        return None

    def _selected_points(self) -> List:
        """All (series, GraphPoint) pairs that are currently selected."""
        return [(s, kp) for s in self._series for kp in s.points
                if kp.selected]

    def _cross_param(self) -> bool:
        """True when the selection spans more than one parameter (different
        row_ids). Cross-parameter transforms may only shift time — scaling
        values from one param onto another is forbidden (plan §22.9)."""
        row_ids = {s.row_id for s, _kp in self._selected_points()}
        return len(row_ids) > 1

    def _begin_box_move_drag(self, sp: QPointF) -> None:
        """T117: start an interior drag — translate the whole selection."""
        self._drag_mode = "box_move"
        self._cross_param = self._cross_param()  # snapshot decision
        self._drag_press_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        self._drag_press_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        # Snapshot (series, point, orig_frame, orig_value).
        self._tbox_snap_points = [
            (s, kp, kp.frame, kp.value)
            for s, kp in self._selected_points()
        ]
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def _begin_box_scale_drag(self, sp: QPointF, grip: str) -> None:
        """T117: start an edge/corner drag — scale around the anchor."""
        self._drag_mode = "box_scale"
        self._tbox_grip = grip
        self._cross_param = self._cross_param()
        self._drag_press_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        self._drag_press_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        # Snapshot the selection + bbox + anchor at press time.
        self._tbox_snap_points = [
            (s, kp, kp.frame, kp.value)
            for s, kp in self._selected_points()
        ]
        self._tbox_snap_bbox = self._tbox_bbox
        self._tbox_snap_anchor = list(self._tbox_anchor) \
            if self._tbox_anchor else None
        self.setCursor(self._grip_cursor(grip))

    def _begin_anchor_drag(self, sp: QPointF) -> None:
        """T117: start an anchor drag — move only the anchor, no keyframe
        edits. The anchor changes where scaling pivots on the NEXT scale."""
        self._drag_mode = "box_anchor"
        self._tbox_grip = "anchor"
        self._drag_press_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        self._drag_press_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        self._tbox_snap_anchor = list(self._tbox_anchor) \
            if self._tbox_anchor else None
        self.setCursor(Qt.CursorShape.CrossCursor)

    @staticmethod
    def _grip_cursor(grip: str):
        cursors = {
            "n": Qt.CursorShape.SizeVerCursor,
            "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor,
            "w": Qt.CursorShape.SizeHorCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor,
            "sw": Qt.CursorShape.SizeBDiagCursor,
            "nw": Qt.CursorShape.SizeFDiagCursor,
            "se": Qt.CursorShape.SizeFDiagCursor,
        }
        return cursors.get(grip, Qt.CursorShape.ArrowCursor)

    def _do_box_move_drag(self, sp: QPointF) -> None:
        """T117: translate every selected point by the cursor delta. Value
        component is dropped for speed-graph or cross-parameter moves.

        C4 fix: when snap is enabled, the leading point snaps to the shared
        targets (work-area edges + markers + other keyframes) and the whole
        group shifts by the snapped delta — consistent with keyframe drag."""
        cur_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        cur_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        df = int(round(cur_frame - self._drag_press_frame))
        dv = cur_value - self._drag_press_value
        # C4: snap the group delta. Pick the first snapshotted point as the
        # "leader", snap its target frame, recompute df from the snapped target.
        snap_on = getattr(self._options, "snap_enabled", False)
        if snap_on and self._tbox_snap_points:
            _s0, _kp0, of0, _ov0 = self._tbox_snap_points[0]
            leader_target = of0 + df
            snapped = self._snap_frame(leader_target)
            df = int(round(snapped - of0))
        # Value allowed only for single-param, value-graph selections.
        allow_value = (not self._cross_param
                       and not graph_model.is_speed_drag(self._series))
        for series, kp, of, ov in self._tbox_snap_points:
            nf = max(0, int(round(of + df)))
            kp.frame = float(nf)
            kp.value = float(ov + dv) if allow_value else ov
        # Re-sort affected series.
        for series, _kp, _of, _ov in self._tbox_snap_points:
            series.points.sort(key=lambda p: p.frame)
        self._refresh_transform_box()
        self._rebuild_samples()
        self.update()

    def _do_box_scale_drag(self, sp: QPointF) -> None:
        """T117: scale the selection around the anchor. Horizontal cursor
        motion scales time; vertical scales value (frozen for cross-param
        or speed-graph). Ctrl/Cmd scales symmetrically around the anchor."""
        if self._tbox_snap_bbox is None or self._tbox_snap_anchor is None:
            return
        grip = self._tbox_grip or ""
        cur_frame = graph_model.x_to_frame(
            sp.x(), self._viewport, _MARGIN_LEFT)
        cur_value = graph_model.y_to_value(
            sp.y(), self._viewport, self._plot_bottom())
        fmin, fmax, vmin, vmax = self._tbox_snap_bbox
        af, av = self._tbox_snap_anchor
        # Time scale: ratio of (cursor - anchor) to (gripped edge - anchor).
        # The gripped edge depends on which side the grip is on.
        grip_left = "w" in grip
        grip_right = "e" in grip
        grip_top = "n" in grip
        grip_bot = "s" in grip
        scale_time = 1.0
        if grip_left or grip_right:
            ref = fmin if grip_left else fmax
            denom = max(1e-9, abs(ref - af))
            scale_time = max(1e-6, abs(cur_frame - af) / denom)
        scale_value = 1.0
        allow_value = (not self._cross_param
                       and not graph_model.is_speed_drag(self._series))
        if allow_value and (grip_top or grip_bot):
            ref = vmax if grip_top else vmin
            denom = max(1e-9, abs(ref - av))
            scale_value = max(1e-6, abs(cur_value - av) / denom)
        # Build lightweight stand-in points carrying the snapshot frame/value
        # (apply_scale_transform doesn't mutate and returns aligned results).
        from .graph_model import GraphPoint as _GP
        standins = [
            _GP(row_id=kp.row_id, node_id=kp.node_id,
                parameter_name=kp.parameter_name, channel=kp.channel,
                frame=of, value=ov, selected=True)
            for _series, kp, of, ov in self._tbox_snap_points
        ]
        results = graph_model.apply_scale_transform(
            standins, self._tbox_snap_bbox, self._tbox_snap_anchor,
            scale_time, scale_value, allow_value=allow_value)
        # results align 1:1 with _tbox_snap_points (same order).
        for (_series, kp, _of, _ov), (_pt, nf, nv) in zip(
                self._tbox_snap_points, results):
            kp.frame = nf
            kp.value = nv
        for series, _kp, _of, _ov in self._tbox_snap_points:
            series.points.sort(key=lambda p: p.frame)
        self._refresh_transform_box()
        self._rebuild_samples()
        self.update()

    def _do_anchor_drag(self, sp: QPointF) -> None:
        """T117: move the anchor to the cursor's data position. Clamped to
        the current bbox so it can't escape the box. No keyframe edits."""
        if self._tbox_bbox is None:
            return
        fmin, fmax, vmin, vmax = self._tbox_bbox
        cf = graph_model.x_to_frame(sp.x(), self._viewport, _MARGIN_LEFT)
        cv = graph_model.y_to_value(sp.y(), self._viewport,
                                    self._plot_bottom())
        af = min(fmax, max(fmin, cf))
        av = min(vmax, max(vmin, cv))
        self._tbox_anchor = [af, av]
        self.update()

    def _draw_transform_box(self, p: QPainter, rect: QRectF) -> None:
        """T117: paint the bounding box, its anchor, and grip handles."""
        core = self._box_core_rect_px()
        if core is None:
            return
        # Skip if the box is too small to be meaningful on screen.
        if core.width() < _BOX_MIN_SPAN and core.height() < _BOX_MIN_SPAN:
            return
        accent = QColor(timeline_layout.resolve_token("accent_blue"))
        pen = QPen(accent)
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(core)
        # Corner squares + mid-edge ticks so the grips are visible.
        p.setBrush(accent)
        p.setPen(QPen(QColor(timeline_layout.resolve_token("text_primary"))))
        for cx, cy in ((core.left(), core.top()),
                       (core.right(), core.top()),
                       (core.left(), core.bottom()),
                       (core.right(), core.bottom())):
            p.drawRect(QRectF(cx - 3, cy - 3, 6, 6))
        # Mid-edge ticks.
        for cx, cy in ((core.center().x(), core.top()),
                       (core.center().x(), core.bottom()),
                       (core.left(), core.center().y()),
                       (core.right(), core.center().y())):
            p.setBrush(accent)
            p.drawEllipse(QPointF(cx, cy), 2.5, 2.5)
        # Anchor: a distinct circle with a crosshair.
        if self._tbox_anchor is not None:
            af, av = self._tbox_anchor
            ax = frame_to_x(af, self._viewport, _MARGIN_LEFT)
            ay = value_to_y(av, self._viewport, self._plot_bottom())
            p.setBrush(QColor(timeline_layout.resolve_token("accent_orange")))
            p.setPen(QPen(QColor(timeline_layout.resolve_token(
                "text_primary"))))
            p.drawEllipse(QPointF(ax, ay), 5.0, 5.0)
            anchor_pen = QPen(QColor(timeline_layout.resolve_token(
                "text_primary")))
            anchor_pen.setWidth(1)
            p.setPen(anchor_pen)
            p.drawLine(QPointF(ax - 7, ay), QPointF(ax + 7, ay))
            p.drawLine(QPointF(ax, ay - 7), QPointF(ax, ay + 7))

    def _apply_marquee_selection(self) -> None:
        """T112: select every keyframe inside the current marquee rect."""
        if self._marquee_rect is None:
            return
        rect = self._marquee_rect
        if not self._marquee_additive:
            for s in self._series:
                for kp in s.points:
                    kp.selected = False
        changed = False
        for s in self._series:
            for kp in s.points:
                x = frame_to_x(kp.frame, self._viewport, _MARGIN_LEFT)
                y = value_to_y(kp.value, self._viewport,
                               self._plot_bottom())
                if rect.contains(QPointF(x, y)):
                    kp.selected = True
                    changed = True
        if changed:
            self._emit_selection()
        self._refresh_transform_box()

    def _emit_selection(self) -> None:
        """T113: broadcast the current selection as a set of (row_id, frame)."""
        sel = set()
        for s in self._series:
            for kp in s.points:
                if kp.selected:
                    sel.add((s.row_id, int(kp.frame)))
        self.selection_changed.emit(sel)

    def get_selection(self) -> set:
        """Read-only view of the current selection as (row_id, frame) tuples."""
        sel = set()
        for s in self._series:
            for kp in s.points:
                if kp.selected:
                    sel.add((s.row_id, int(kp.frame)))
        return sel

    def fit_view(self) -> None:
        """Reset the viewport to auto-fit (clears user zoom/pan)."""
        self._user_adjusted = False
        self._refit_viewport()
        self._rebuild_samples()
        self.update()

    # ── painting (plan §22.6 fixed order) ──

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w = self.width()
        h = self.height()

        # 1. background
        bg = timeline_layout.resolve_token("timeline_bg")
        p.fillRect(0, 0, w, h, QColor(bg))

        plot_rect = QRectF(_MARGIN_LEFT, _MARGIN_TOP,
                           max(1, w - _MARGIN_LEFT - _MARGIN_RIGHT),
                           max(1, h - _MARGIN_TOP - _MARGIN_BOTTOM))

        # 2. T121 work-area overlay (out-of-range shade + top band), shared
        # with the Timeline so both views show the same reference points.
        # F1 fix: honour the show_layer_in_out toggle.
        if getattr(self._options, "show_layer_in_out", True):
            self._draw_out_of_range_shade(p, plot_rect)
        # 3. minor grid
        self._draw_grid(p, plot_rect, minor=True)
        # 4. major grid + zero line
        self._draw_grid(p, plot_rect, minor=False)
        self._draw_zero_line(p, plot_rect)
        # 5. T121 markers (ruler ticks at the top, shared with the Timeline).
        # F1 fix: honour the show_markers toggle.
        if getattr(self._options, "show_markers", True):
            self._draw_markers(p, plot_rect)
        # 6/7. T119 reference (pre) + post-expression curves — drawn UNDER
        # the main keyframe curve so the editable curve stays on top.
        self._draw_expression_curves(p, plot_rect)
        # 8/9. curves (unselected then selected)
        self._draw_curves(p, plot_rect)
        # 10. keyframe points
        self._draw_keyframes(p, plot_rect)
        # 11. Bezier handles (selected keyframes only)
        self._draw_handles(p, plot_rect)
        # 12. T117 transform box (multi-keyframe free-transform bounds).
        # The box only paints when a valid selection bbox exists (≥2 selected
        # keyframes spanning ≥2 frames), gated inside _draw_transform_box by
        # _box_core_rect_px() returning non-None. The show_transform_box
        # option is intentionally NOT honoured here: it was never wired and
        # the box's visibility is selection-driven, not option-driven (F1
        # audit: gating on the dormant option would hide the box permanently).
        self._draw_transform_box(p, plot_rect)
        # 13. current-time indicator
        self._draw_playhead(p, plot_rect, h)
        # 14. hover tooltip — deferred (drag-time)
        # 14b. T112 marquee (rubber-band) overlay.
        self._draw_marquee(p)
        # 15. legend
        self._draw_legend(p, w, h)

    def _draw_marquee(self, p: QPainter) -> None:
        """T112: paint the rubber-band rectangle during a marquee drag."""
        if self._marquee_rect is None:
            return
        rect = self._marquee_rect.normalized()
        # Semi-transparent accent fill + accent border.
        accent = QColor(timeline_layout.resolve_token("accent_blue"))
        fill = QColor(accent)
        fill.setAlpha(40)
        p.fillRect(rect, fill)
        pen = QPen(accent)
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(rect)

    def _plot_bottom(self) -> float:
        return float(self.height() - _MARGIN_BOTTOM)

    def _draw_out_of_range_shade(self, p: QPainter, rect: QRectF) -> None:
        """T121: shade the plot area outside the work-area band, mirroring
        the Timeline's _draw_out_of_range_shade (plan §22.9 'Graph 与
        Timeline 使用同一参考点'). When no play range is set, nothing is
        shaded (the whole comp is in range)."""
        if self._play_range is None:
            return
        in_f, out_f = self._play_range
        x_in = frame_to_x(float(in_f), self._viewport, rect.left())
        x_out = frame_to_x(float(out_f + 1), self._viewport, rect.left())
        shade = QColor(0, 0, 0, 90)
        # Left shade (before the in-point).
        if x_in > rect.left():
            p.fillRect(QRectF(rect.left(), rect.top(),
                              x_in - rect.left(), rect.height()), shade)
        # Right shade (after the out-point).
        if x_out < rect.right():
            p.fillRect(QRectF(x_out, rect.top(),
                              rect.right() - x_out, rect.height()), shade)
        # Work-area band tick at the very top (a thin accent strip).
        band = QColor(timeline_layout.resolve_token("accent_orange"))
        band.setAlpha(180)
        p.fillRect(QRectF(max(rect.left(), x_in), rect.top(),
                          max(0.0, min(rect.right(), x_out) -
                              max(rect.left(), x_in)), 3.0), band)

    def _draw_markers(self, p: QPainter, rect: QRectF) -> None:
        """T121: draw marker ticks at the top of the plot, shared with the
        Timeline (plan §22.9). Markers are reference + snap targets."""
        if not self._markers:
            return
        for m in self._markers:
            f = m.get("frame", 0)
            x = frame_to_x(float(f), self._viewport, rect.left())
            if not (rect.left() <= x <= rect.right()):
                continue
            color = QColor(m.get("color", "#ff3d3d"))
            # A small triangle tick at the top.
            from PyQt6.QtGui import QPolygonF
            tick_h = 8.0
            poly = QPolygonF([
                QPointF(x, rect.top()),
                QPointF(x - 4, rect.top() - tick_h),
                QPointF(x + 4, rect.top() - tick_h),
            ])
            p.setBrush(color)
            p.setPen(QPen(QColor(timeline_layout.resolve_token("text_primary"))))
            p.drawPolygon(poly)
            p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_grid(self, p: QPainter, rect: QRectF,
                   minor: bool) -> None:
        """Draw the frame (vertical) + value (horizontal) grid lines."""
        token = "grid_minor" if minor else "grid_major"
        color = QColor(timeline_layout.resolve_token(token))
        pen = QPen(color)
        pen.setWidth(1)
        p.setPen(pen)
        # Vertical lines: pick a frame step that gives ~6-12 lines.
        start = int(self._viewport.frame_start)
        end = int(self._viewport.frame_end) + 1
        span = max(1, end - start)
        step = _nice_step(span / 8)
        f = start
        while f <= end:
            x = frame_to_x(float(f), self._viewport, rect.left())
            if rect.left() <= x <= rect.right():
                p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
            f += step
        # Horizontal lines: pick a value step.
        vlo, vhi = self._viewport.value_min, self._viewport.value_max
        vspan = max(1e-9, vhi - vlo)
        vstep = _nice_step(vspan / 6)
        v = _floor_to(vlo, vstep)
        while v <= vhi:
            y = value_to_y(v, self._viewport, self._plot_bottom())
            if rect.top() <= y <= rect.bottom():
                p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            v += vstep

    def _draw_zero_line(self, p: QPainter, rect: QRectF) -> None:
        """The zero-value line — brighter than the regular grid (plan §22.4)."""
        if not (self._viewport.value_min <= 0.0 <= self._viewport.value_max):
            return
        y = value_to_y(0.0, self._viewport, self._plot_bottom())
        if not (rect.top() <= y <= rect.bottom()):
            return
        pen = QPen(QColor(timeline_layout.resolve_token("border")))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

    def _draw_curves(self, p: QPainter, rect: QRectF) -> None:
        """Draw every series' sampled curve (unselected first, then selected
        on top — plan §22.6 steps 8/9)."""
        # Sort: unselected series first so the selected one draws on top.
        ordered = sorted(self._series,
                         key=lambda s: any(pt.selected for pt in s.points))
        for s in ordered:
            if not s.samples:
                continue
            is_selected = any(pt.selected for pt in s.points)
            color = QColor(s.color if s.color else
                           timeline_layout.resolve_token("accent_orange"))
            pen = QPen(color)
            pen.setWidth(2 if is_selected else 1)
            p.setPen(pen)
            path = QPainterPath()
            first = True
            for frame, value in s.samples:
                x = frame_to_x(frame, self._viewport, rect.left())
                y = value_to_y(value, self._viewport, self._plot_bottom())
                pt = QPointF(x, y)
                if first:
                    path.moveTo(pt)
                    first = False
                else:
                    path.lineTo(pt)
            p.drawPath(path)

    def _draw_expression_curves(self, p: QPainter, rect: QRectF) -> None:
        """T119: draw the pre (reference) and post-expression curves.

        Plan §22.11: the pre curve uses a dark/grey low-opacity line (it's
        the un-modified keyframe/static value); the post curve uses the
        series colour at higher weight (it's the final property result).
        Keyframe diamonds are ALWAYS drawn from the pre curve — the post
        curve never gets diamond markers. On evaluation error the post
        curve falls back to the pre curve and a red error marker is drawn.
        """
        for s in self._series:
            if not s.pre_expression_samples and not s.post_expression_samples:
                continue
            # F1 fix: the pre (reference) curve only draws when the user has
            # enabled show_reference_graph; the post curve is gated by
            # show_post_expression_graph in _rebuild_samples already.
            show_ref = getattr(self._options, "show_reference_graph", False)
            # Pre curve: dark, thin, dashed (reference).
            if show_ref and s.pre_expression_samples:
                pre_pen = QPen(QColor(timeline_layout.resolve_token("text_hint")))
                pre_pen.setWidth(1)
                pre_pen.setStyle(Qt.PenStyle.DashLine)
                p.setPen(pre_pen)
                path = QPainterPath()
                first = True
                for frame, value in s.pre_expression_samples:
                    x = frame_to_x(frame, self._viewport, rect.left())
                    y = value_to_y(value, self._viewport, self._plot_bottom())
                    pt = QPointF(x, y)
                    if first:
                        path.moveTo(pt)
                        first = False
                    else:
                        path.lineTo(pt)
                p.drawPath(path)
            # Post curve: bright, solid (the expression result).
            if s.post_expression_samples:
                post_color = QColor(s.color if s.color else
                                    timeline_layout.resolve_token("accent_orange"))
                if s.expression_error:
                    post_color = QColor(timeline_layout.resolve_token("error"))
                post_pen = QPen(post_color)
                post_pen.setWidth(2)
                p.setPen(post_pen)
                path = QPainterPath()
                first = True
                for frame, value in s.post_expression_samples:
                    x = frame_to_x(frame, self._viewport, rect.left())
                    y = value_to_y(value, self._viewport, self._plot_bottom())
                    pt = QPointF(x, y)
                    if first:
                        path.moveTo(pt)
                        first = False
                    else:
                        path.lineTo(pt)
                p.drawPath(path)
            # Error marker: a small red circle at the plot's top-right when
            # the expression failed (the post curve is a fallback there).
            if s.expression_error:
                err_pen = QPen(QColor(timeline_layout.resolve_token("error")))
                err_pen.setWidth(1)
                p.setPen(err_pen)
                p.setBrush(QColor(timeline_layout.resolve_token("error")))
                mx = rect.right() - 12
                my = rect.top() + 12
                p.drawEllipse(QPointF(mx, my), 4.0, 4.0)
                p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_keyframes(self, p: QPainter, rect: QRectF) -> None:
        """Draw keyframe points as diamonds (plan §22.6 step 10)."""
        from PyQt6.QtGui import QPolygonF
        for s in self._series:
            for kp in s.points:
                x = frame_to_x(kp.frame, self._viewport, rect.left())
                y = value_to_y(kp.value, self._viewport, self._plot_bottom())
                if not rect.contains(QPointF(x, y)):
                    continue
                size = 5 if not kp.selected else 6
                color = QColor(timeline_layout.resolve_token(
                    "keyframe_selected" if kp.selected else "accent_orange"))
                p.setBrush(color)
                p.setPen(QPen(QColor(timeline_layout.resolve_token(
                    "text_primary" if kp.selected else "border"))))
                poly = QPolygonF([
                    QPointF(x, y - size), QPointF(x + size, y),
                    QPointF(x, y + size), QPointF(x - size, y),
                ])
                p.drawPolygon(poly)

    def _draw_handles(self, p: QPainter, rect: QRectF) -> None:
        """Draw Bezier handles for selected keyframes (plan §22.6 step 11)."""
        pen = QPen(QColor(timeline_layout.resolve_token("text_hint")))
        pen.setWidth(1)
        p.setPen(pen)
        for s in self._series:
            for kp in s.points:
                if not kp.selected:
                    continue
                if kp.handle_left is None and kp.handle_right is None:
                    continue
                kx = frame_to_x(kp.frame, self._viewport, rect.left())
                ky = value_to_y(kp.value, self._viewport, self._plot_bottom())
                for handle, side in ((kp.handle_left, "L"),
                                     (kp.handle_right, "R")):
                    if handle is None:
                        continue
                    hf = kp.frame + handle[0]
                    hv = kp.value + handle[1]
                    hx = frame_to_x(hf, self._viewport, rect.left())
                    hy = value_to_y(hv, self._viewport, self._plot_bottom())
                    p.drawLine(QPointF(kx, ky), QPointF(hx, hy))
                    # Small circle at the handle end.
                    p.setBrush(QColor(timeline_layout.resolve_token(
                        "text_hint")))
                    p.drawEllipse(QPointF(hx, hy), 3.0, 3.0)
                    p.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_playhead(self, p: QPainter, rect: QRectF, h: float) -> None:
        """The current-time indicator — a vertical line across the plot."""
        x = frame_to_x(float(self._current_frame), self._viewport,
                       rect.left())
        if not (rect.left() <= x <= rect.right()):
            return
        pen = QPen(QColor(timeline_layout.resolve_token("playhead")))
        pen.setWidth(1)
        p.setPen(pen)
        p.drawLine(QPointF(x, 0), QPointF(x, h))

    def _draw_legend(self, p: QPainter, w: int, h: int) -> None:
        """Bottom-right legend listing the visible series + their colours."""
        if not self._series:
            return
        font = QFont(p.font())
        font.setPointSize(8)
        p.setFont(font)
        pen = QPen(QColor(timeline_layout.resolve_token("text_hint")))
        p.setPen(pen)
        y = h - _MARGIN_BOTTOM + 4
        x = _MARGIN_LEFT
        for s in self._series:
            if not s.points:
                continue
            # Colour swatch.
            p.fillRect(QRectF(x, y + 2, 10, 8),
                       QColor(s.color if s.color else
                              timeline_layout.resolve_token("accent_orange")))
            label = s.display_name
            if s.channel:
                label += f" ({s.channel})"
            label += f" [{s.graph_type}]"
            p.drawText(QRectF(x + 14, y, 200, 12),
                       Qt.AlignmentFlag.AlignVCenter
                       | Qt.TextFlag.TextSingleLine, label)
            x += 14 + p.fontMetrics().horizontalAdvance(label) + 16
            if x > w - 100:
                break


# ── helpers ────────────────────────────────────────────────────────────


def _nice_step(raw: float) -> int:
    """Pick a grid step from the 1/2/5 × 10^k series nearest to ``raw``.

    Returns an int (grid steps are whole frames). For sub-1.0 inputs the
    floor of the chosen 1/2/5 value is returned (1 is the minimum).
    """
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm < 1.5:
        val = 1 * mag
    elif norm < 3.5:
        val = 2 * mag
    elif norm < 7.5:
        val = 5 * mag
    else:
        val = 10 * mag
    # Round to int, clamped to a minimum of 1 (grid steps are whole frames).
    return max(1, int(round(val)))


def _floor_to(value: float, step: int) -> float:
    if step <= 0:
        return float(value)
    return float(int(value // step) * step)
