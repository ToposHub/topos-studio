"""SplineView — self-painted curve chart for the Spline editor (phase 3).

Draws (plan §11.3/§11.5):
  - background + plot rect
  - frame/value grid + axis labels
  - non-selected then selected curves (cubic bezier via screen-space cubicTo)
  - handle lines + handle squares (selected curve/point only)
  - keyframe points
  - red playhead (drawn last, overlays everything)

Coordinate transforms are centralized in data_to_screen / screen_to_data
(§11.3). The view owns a SplineViewport (frame/value min/max + plot rect).

P11 (this file): read-only painting + a render_to_painter() entry point for
headless off-screen render tests (§11.11). P12 adds mouse interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QRectF, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import (QColor, QPainter, QPainterPath, QPen, QPolygonF)
from PyQt6.QtWidgets import QInputDialog, QMenu, QWidget

from src.shared.ui.style_sheet import C
from .spline_model import (
    SplineCurve, SplineKey, SplineSegment, SplineModel,
    build_segments, sample_segment,
    INTERP_LINEAR, INTERP_HOLD, INTERP_BEZIER,
    INTERP_EASE_IN, INTERP_EASE_OUT, INTERP_EASE_IN_OUT)


@dataclass
class SplineViewport:
    frame_min: float
    frame_max: float
    value_min: float
    value_max: float
    plot_rect: QRectF

    def expand_degenerate(self) -> None:
        """Ensure non-zero ranges so divisions don't blow up (§11.3)."""
        if self.frame_max - self.frame_min < 1e-9:
            self.frame_min -= 1.0
            self.frame_max += 1.0
        if self.value_max - self.value_min < 1e-9:
            self.value_min -= 1.0
            self.value_max += 1.0


class SplineView(QWidget):
    """Right-side curve chart of the Spline panel.

    P12 adds mouse interaction: drag keyframe points, drag bezier handles,
    box-select, and tangent-mode changes. Edits emit keyframe_changed so
    CompositePage persists them onto the live node + pushes undo.
    """

    # Emitted when a key is edited (node_id, param_name, key_dicts).
    keyframe_changed = pyqtSignal(str, str, list)
    # Emitted when the user drags the playhead.
    current_frame_changed = pyqtSignal(int)
    # Emitted when the selected key set changes (batch 4) so the panel can
    # refresh its frame/value readout. Payload is the sole selected key's
    # (frame, value) or None when nothing/an edge is selected.
    selected_key_changed = pyqtSignal(object)
    # T61: emitted when the curve selection changes as a result of a view
    # interaction (clicking a keyframe on a different curve, or a curve-row
    # click in the tree). Carries the now-selected curve_id, or "" when the
    # selection was cleared. The Spline panel listens to push the matching
    # parameter-row selection back to the Keyframes panel via the shared
    # selection model.
    curve_selection_changed = pyqtSignal(str)
    # Shared Keyframes/Spline selection bridge. Payload is a tuple of
    # (param_row_id, frame) pairs using the Keyframes row-id vocabulary.
    key_selection_changed = pyqtSignal(object)
    curve_settings_changed = pyqtSignal(str, str, str, str)

    # Hit radii (§11.8)
    _HANDLE_HIT = 9.0
    _KEY_HIT = 9.0
    _CURVE_HIT = 5.0

    def __init__(self, model: SplineModel,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._model = model
        self._current_frame = 0
        self._viewport: Optional[SplineViewport] = None
        self._margin_left = 40.0
        self._margin_top = 22.0
        self._margin_right = 8.0
        self._margin_bottom = 18.0
        self.setMinimumSize(300, 200)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # P12 drag state — none|key|handle|box|pan|playhead
        self._drag_mode = "none"
        self._drag_curve: Optional[SplineCurve] = None
        self._drag_key_idx: Optional[int] = None
        self._drag_handle: Optional[str] = None  # 'left'|'right'
        self._box_start: Optional[QPointF] = None
        self._box_end: Optional[QPointF] = None
        self._box_additive: bool = False  # Shift-box adds to selection
        # Pan state (middle/right-button drag): remember the screen anchor and
        # the data coords under it so the viewport follows the cursor.
        self._pan_anchor_screen: Optional[QPointF] = None
        self._pan_anchor_data: Optional[QPointF] = None
        # Keyframe clipboard (batch 2/3): list of dicts with rel_frame + key
        # payload, so paste re-anchors the block at the playhead.
        self._key_clipboard: list = []
        # Multi-key drag (batch 3): when the grabbed key is already selected
        # we move every selected key by the same delta. These snapshot the
        # grab point + each selected key's origin so the delta is exact.
        self._multi_drag: bool = False
        self._multi_drag_anchor: Optional[QPointF] = None
        self._multi_drag_origins: Optional[list] = None  # [(curve,idx,key,frame,value,hl,hr)]
        # Batch 5: axis display modes (driven by the footer combos).
        # _time_mode: "frame" | "seconds" | "timecode"  (X axis label format)
        # _value_mode: "value" | "normalized"           (Y axis label format)
        # _fps: frames per second for seconds/timecode conversion.
        self._time_mode: str = "frame"
        self._value_mode: str = "value"
        self._fps: float = 24.0
        # Batch 6: snapping guide lines (drawn while a key drag is active).
        self._snap_guide_frame: Optional[float] = None
        self._snap_guide_value: Optional[float] = None

    # ── viewport / transforms (§11.3) ──

    def _default_viewport(self, curves=None) -> SplineViewport:
        bounds = None
        try:
            from .spline_model import fit_bounds
            bounds = fit_bounds(curves if curves is not None
                                else self._model.visible_curves())
        except Exception:
            bounds = None
        if bounds is None:
            bounds = (-10.0, 110.0, -1.0, 2.0)
        fmin, fmax, vmin, vmax = bounds
        plot = QRectF(self._margin_left, self._margin_top,
                      max(1.0, self.width() - self._margin_left - self._margin_right),
                      max(1.0, self.height() - self._margin_top - self._margin_bottom))
        vp = SplineViewport(fmin, fmax, vmin, vmax, plot)
        vp.expand_degenerate()
        return vp

    def _ensure_viewport(self) -> SplineViewport:
        if self._viewport is None:
            self._viewport = self._default_viewport()
        return self._viewport

    def resizeEvent(self, event) -> None:
        """Keep a fitted viewport's plot area aligned with the widget size.

        A Spline panel can be created before its dock has received its final
        width.  Without refreshing this rectangle, a fit performed during
        graph binding would keep drawing into the initial size after the dock
        expands.
        """
        super().resizeEvent(event)
        if self._viewport is not None:
            self._viewport.plot_rect = QRectF(
                self._margin_left,
                self._margin_top,
                max(1.0, self.width() - self._margin_left - self._margin_right),
                max(1.0, self.height() - self._margin_top - self._margin_bottom),
            )
        self.update()

    def fit_view(self) -> None:
        self._viewport = None
        self.update()

    def fit_selected_view(self) -> None:
        """Fit the selected curve(s), avoiding mixed-unit scale distortion."""
        selected = [c for c in self._model.visible_curves() if c.selected]
        self._viewport = None if not selected else self._default_viewport(selected)
        self.update()

    def data_to_screen(self, p: QPointF) -> QPointF:
        vp = self._ensure_viewport()
        x = vp.plot_rect.left() + (p.x() - vp.frame_min) / (vp.frame_max - vp.frame_min) * vp.plot_rect.width()
        y = vp.plot_rect.bottom() - (p.y() - vp.value_min) / (vp.value_max - vp.value_min) * vp.plot_rect.height()
        return QPointF(x, y)

    def screen_to_data(self, p: QPointF) -> QPointF:
        vp = self._ensure_viewport()
        frame = vp.frame_min + (p.x() - vp.plot_rect.left()) / vp.plot_rect.width() * (vp.frame_max - vp.frame_min)
        value = vp.value_min + (vp.plot_rect.bottom() - p.y()) / vp.plot_rect.height() * (vp.value_max - vp.value_min)
        return QPointF(frame, value)

    # ── zoom / pan (batch 1) ──

    def _zoom_at(self, factor: float, center_data: QPointF) -> None:
        """Zoom the viewport around a data-space anchor point.

        ``factor`` > 1 zooms in (range shrinks), < 1 zooms out (range grows).
        The data point under the cursor stays at the same screen position.
        """
        vp = self._ensure_viewport()
        # Clamp factor so a single wheel tick can't collapse or invert the range.
        factor = max(0.05, min(20.0, factor))
        cf, cv = center_data.x(), center_data.y()
        new_frange = (vp.frame_max - vp.frame_min) / factor
        new_vrange = (vp.value_max - vp.value_min) / factor
        # Keep the anchor's ratio within the viewport constant.
        if vp.frame_max - vp.frame_min > 1e-9:
            rx = (cf - vp.frame_min) / (vp.frame_max - vp.frame_min)
        else:
            rx = 0.5
        if vp.value_max - vp.value_min > 1e-9:
            ry = (cv - vp.value_min) / (vp.value_max - vp.value_min)
        else:
            ry = 0.5
        vp.frame_min = cf - rx * new_frange
        vp.frame_max = cf + (1.0 - rx) * new_frange
        vp.value_min = cv - ry * new_vrange
        vp.value_max = cv + (1.0 - ry) * new_vrange
        self.update()

    def _pan_by(self, dframe: float, dvalue: float) -> None:
        """Shift the viewport by (dframe, dvalue) in data space."""
        vp = self._ensure_viewport()
        vp.frame_min += dframe
        vp.frame_max += dframe
        vp.value_min += dvalue
        vp.value_max += dvalue
        self.update()

    def wheelEvent(self, event) -> None:
        """AE/DaVinci-style wheel navigation (batch 1).

        - Plain wheel: zoom both axes around the cursor.
        - Ctrl+wheel: finer zoom.
        - Alt+wheel: horizontal pan (along frames).
        - Shift+wheel: vertical pan (along values).
        Mirrors keyframes_timeline_view.wheelEvent so users have one mental
        model across both editors.
        """
        delta = event.angleDelta().y()
        if delta == 0:
            super().wheelEvent(event)
            return
        mods = event.modifiers()
        center = self.screen_to_data(event.position())
        if mods & Qt.KeyboardModifier.AltModifier:
            # Horizontal pan: wheel up → see later frames.
            self._pan_by(delta * 0.01 * (self._ensure_viewport().frame_max
                                          - self._ensure_viewport().frame_min), 0.0)
            return
        if mods & Qt.KeyboardModifier.ShiftModifier:
            # Vertical pan: wheel up → see larger values.
            vrange = self._ensure_viewport().value_max - self._ensure_viewport().value_min
            self._pan_by(0.0, delta * 0.01 * vrange)
            return
        # Zoom anchored at cursor. Ctrl = finer steps.
        # delta > 0 (wheel up) → zoom IN → viewport range shrinks → factor > 1.
        base = 1.0016 ** delta   # smooth; one tick ≈ ±18%
        factor = base ** (0.5 if (mods & Qt.KeyboardModifier.ControlModifier) else 1.0)
        self._zoom_at(factor, center)

    # ── state ──

    def set_current_frame(self, frame: int) -> None:
        if frame != self._current_frame:
            self._current_frame = int(frame)
            self.update()

    def refresh(self) -> None:
        self._viewport = None  # viewport depends on curves → recompute
        self.update()

    # ── batch 5: axis display modes ──

    def set_time_mode(self, mode: str) -> None:
        """X-axis label format: 'frame' | 'seconds' | 'timecode'."""
        if mode != self._time_mode and mode in ("frame", "seconds", "timecode"):
            self._time_mode = mode
            self.update()

    def set_value_mode(self, mode: str) -> None:
        """Y-axis label format: 'value' | 'normalized'."""
        if mode != self._value_mode and mode in ("value", "normalized"):
            self._value_mode = mode
            self.update()

    def set_fps(self, fps: float) -> None:
        if fps > 0 and fps != self._fps:
            self._fps = float(fps)
            if self._time_mode != "frame":
                self.update()

    def _fmt_frame_label(self, frame: float) -> str:
        """Format an X-axis tick per the current time mode."""
        f = int(round(frame))
        if self._time_mode == "seconds":
            return f"{f / self._fps:.2f}s"
        if self._time_mode == "timecode":
            return _frame_to_timecode(f, self._fps)
        return str(f)

    def _fmt_value_label(self, value: float,
                         curve: Optional[SplineCurve] = None) -> str:
        """Format a Y-axis tick per the current value mode.

        In 'normalized' mode the tick is shown as 0..1 mapped across the
        curve's [min_value, max_value]; the underlying data is unchanged.
        """
        if self._value_mode == "normalized" and curve is not None:
            span = curve.max_value - curve.min_value
            if abs(span) < 1e-9:
                return "0.00"
            norm = (value - curve.min_value) / span
            return f"{norm:.2f}"
        return _fmt_num(value)

    # ── batch 6: snapping ──
    #
    # Snap candidates are judged in SCREEN space (pixel distance) so the feel
    # is consistent regardless of zoom level. Shift disables snapping for a
    # precise drag.

    _SNAP_PX = 6.0  # screen pixels within which a candidate snaps

    def _snap_frame(self, frame: float, curve: Optional["SplineCurve"],
                    exclude_key: Optional["SplineKey"] = None) -> float:
        """Return ``frame`` snapped to the playhead or a sibling key's frame.

        Candidates closer than _SNAP_PX screen pixels win. The dragged key
        itself is excluded via ``exclude_key``. Always returns an int-friendly
        float (snapped frame) or the original when nothing is close.
        """
        if frame is None:
            return frame
        # Playhead is always a candidate.
        candidates = [float(self._current_frame)]
        if curve is not None:
            for k in curve.keys:
                if k is exclude_key:
                    continue
                candidates.append(float(k.frame))
        anchor_x = self.data_to_screen(QPointF(frame, 0.0)).x()
        best = frame
        best_dist = self._SNAP_PX
        for cf in candidates:
            cx = self.data_to_screen(QPointF(cf, 0.0)).x()
            d = abs(cx - anchor_x)
            if d < best_dist:
                best_dist = d
                best = cf
        # Last snapped target is remembered so the view can draw a guide line.
        self._snap_guide_frame = best if best_dist < self._SNAP_PX else None
        return best

    def _snap_value(self, value: float,
                    curve: Optional["SplineCurve"]) -> float:
        """Snap a value to quartiles (normalized mode) or nice grid steps."""
        self._snap_guide_value = None
        if value is None:
            return value
        anchor_y = self.data_to_screen(QPointF(0.0, value)).y()
        best = value
        best_dist = self._SNAP_PX
        if self._value_mode == "normalized" and curve is not None:
            span = curve.max_value - curve.min_value
            if abs(span) > 1e-9:
                for q in (0.0, 0.25, 0.5, 0.75, 1.0):
                    cv = curve.min_value + q * span
                    cy = self.data_to_screen(QPointF(0.0, cv)).y()
                    d = abs(cy - anchor_y)
                    if d < best_dist:
                        best_dist = d
                        best = cv
        else:
            vp = self._ensure_viewport()
            vstep = self._nice_step(vp.value_max - vp.value_min)
            v = _round_step(vp.value_min, vstep)
            while v <= vp.value_max:
                cy = self.data_to_screen(QPointF(0.0, v)).y()
                d = abs(cy - anchor_y)
                if d < best_dist:
                    best_dist = d
                    best = v
                v += vstep
        if best_dist < self._SNAP_PX:
            self._snap_guide_value = best
        return best

    # ── painting (§11.5 draw order) ──

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw(p)

    def render_to_painter(self, painter: QPainter, rect: QRectF) -> None:
        """Headless entry point for off-screen render tests (§11.11).

        Overrides the widget geometry with ``rect`` so a QImage of any size can
        be painted without showing the widget.
        """
        # Temporarily swap margins to fill the given rect.
        self._margin_left = 40.0
        self._margin_top = 22.0
        self._margin_right = 8.0
        self._margin_bottom = 18.0
        # Fake the widget size by computing the plot rect from rect directly.
        vp = self._default_viewport_for(rect)
        self._viewport = vp
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw(painter)
        self._viewport = None

    def _default_viewport_for(self, rect: QRectF) -> SplineViewport:
        try:
            from .spline_model import fit_bounds
            bounds = fit_bounds(self._model.visible_curves())
        except Exception:
            bounds = None
        if bounds is None:
            bounds = (-10.0, 110.0, -1.0, 2.0)
        fmin, fmax, vmin, vmax = bounds
        plot = QRectF(rect.left() + self._margin_left,
                      rect.top() + self._margin_top,
                      max(1.0, rect.width() - self._margin_left - self._margin_right),
                      max(1.0, rect.height() - self._margin_top - self._margin_bottom))
        vp = SplineViewport(fmin, fmax, vmin, vmax, plot)
        vp.expand_degenerate()
        return vp

    def _draw(self, p: QPainter) -> None:
        w = float(self.width())
        h = float(self.height())
        # 1. background
        p.fillRect(QRectF(0, 0, w, h), QColor(C.BG_DARKEST))
        vp = self._ensure_viewport()
        # 2. plot rect
        p.fillRect(vp.plot_rect, QColor(C.BG_DEEPEST))
        # 3. grid
        self._draw_grid(p, vp)
        # 4. axis labels
        self._draw_axis_labels(p, vp)

        p.setClipRect(vp.plot_rect.adjusted(0, 0, 1, 1))
        # 5/6. non-selected then selected curves
        curves = self._model.visible_curves()
        for c in curves:
            if not c.selected:
                self._draw_curve(p, c, selected=False)
        for c in curves:
            if c.selected:
                self._draw_curve(p, c, selected=True)
        # 7/8/9. handles + keys for selected curve
        for c in curves:
            if c.selected:
                self._draw_handles_and_keys(p, c)

        p.setClipping(False)
        # Batch 6: snap guide lines (drawn while a key drag snaps onto a
        # playhead/sibling frame or value grid line).
        self._draw_snap_guides(p, vp)
        # 10. playhead
        self._draw_playhead(p, vp, h)

    def _draw_snap_guides(self, p: QPainter, vp: "SplineViewport") -> None:
        if self._drag_mode not in ("key", "handle"):
            return
        pen = QPen(QColor(C.KEYFRAME_SELECTED))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidthF(1.0)
        p.setPen(pen)
        if self._snap_guide_frame is not None:
            x = vp.plot_rect.left() + (self._snap_guide_frame - vp.frame_min) / (vp.frame_max - vp.frame_min) * vp.plot_rect.width()
            p.drawLine(QPointF(x, vp.plot_rect.top()),
                       QPointF(x, vp.plot_rect.bottom()))
        if self._snap_guide_value is not None:
            y = vp.plot_rect.bottom() - (self._snap_guide_value - vp.value_min) / (vp.value_max - vp.value_min) * vp.plot_rect.height()
            p.drawLine(QPointF(vp.plot_rect.left(), y),
                       QPointF(vp.plot_rect.right(), y))

    def _draw_grid(self, p: QPainter, vp: SplineViewport) -> None:
        pen = QPen(QColor(C.BORDER_DARK))
        pen.setStyle(Qt.PenStyle.DotLine)
        p.setPen(pen)
        # vertical frame grid
        fstep = self._nice_step(vp.frame_max - vp.frame_min)
        f = _round_step(vp.frame_min, fstep)
        while f <= vp.frame_max:
            x = vp.plot_rect.left() + (f - vp.frame_min) / (vp.frame_max - vp.frame_min) * vp.plot_rect.width()
            p.drawLine(QPointF(x, vp.plot_rect.top()), QPointF(x, vp.plot_rect.bottom()))
            f += fstep
        # horizontal value grid
        vstep = self._nice_step(vp.value_max - vp.value_min)
        v = _round_step(vp.value_min, vstep)
        while v <= vp.value_max:
            y = vp.plot_rect.bottom() - (v - vp.value_min) / (vp.value_max - vp.value_min) * vp.plot_rect.height()
            p.drawLine(QPointF(vp.plot_rect.left(), y), QPointF(vp.plot_rect.right(), y))
            v += vstep

    def _draw_axis_labels(self, p: QPainter, vp: SplineViewport) -> None:
        p.setPen(QPen(QColor(C.TEXT_HINT)))
        font = p.font()
        font.setPointSize(8)
        p.setFont(font)
        # Normalized Y axis needs a reference curve; prefer the selected one.
        ref_curve = next((c for c in self._model.visible_curves() if c.selected),
                         None)
        fstep = self._nice_step(vp.frame_max - vp.frame_min)
        f = _round_step(vp.frame_min, fstep)
        while f <= vp.frame_max:
            x = vp.plot_rect.left() + (f - vp.frame_min) / (vp.frame_max - vp.frame_min) * vp.plot_rect.width()
            p.drawText(QRectF(x - 30, 2, 60, self._margin_top - 4),
                       Qt.AlignmentFlag.AlignCenter, self._fmt_frame_label(f))
            f += fstep
        vstep = self._nice_step(vp.value_max - vp.value_min)
        v = _round_step(vp.value_min, vstep)
        while v <= vp.value_max:
            y = vp.plot_rect.bottom() - (v - vp.value_min) / (vp.value_max - vp.value_min) * vp.plot_rect.height()
            p.drawText(QRectF(0, y - 8, self._margin_left - 4, 16),
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       self._fmt_value_label(v, ref_curve))
            v += vstep

    def _draw_curve(self, p: QPainter, curve: SplineCurve,
                    selected: bool) -> None:
        color = QColor(curve.color)
        if not selected:
            color.setAlpha(180)
        pen = QPen(color)
        pen.setWidthF(2.0 if selected else 1.0)
        p.setPen(pen)
        segs = build_segments(curve)
        for seg in segs:
            self._draw_segment(p, seg)
        # Batch 7: extrapolation tails beyond the first/last key (dashed).
        self._draw_extrapolation(p, curve, color, selected)

    def _draw_extrapolation(self, p: QPainter, curve: SplineCurve,
                            color: QColor, selected: bool) -> None:
        if not curve.keys:
            return
        keys = sorted(curve.keys, key=lambda k: k.frame)
        first, last = keys[0], keys[-1]
        vp = self._ensure_viewport()
        ep = QPen(color)
        ep.setStyle(Qt.PenStyle.DashLine)
        ep.setWidthF(1.0 if selected else 0.8)
        p.setPen(ep)
        # Pre-extrapolation: from plot left edge to the first key.
        if first.frame > vp.frame_min:
            left_x = vp.plot_rect.left()
            kp = self.data_to_screen(QPointF(first.frame, first.value))
            if curve.pre_extrap == "linear" and len(keys) >= 2:
                # Extend the first segment's incoming slope.
                slope = (first.value - keys[1].value) / (first.frame - keys[1].frame) \
                        if first.frame != keys[1].frame else 0.0
                left_frame = vp.frame_min
                left_val = first.value + slope * (left_frame - first.frame)
                p.drawLine(self.data_to_screen(QPointF(left_frame, left_val)), kp)
            else:  # constant
                left_val = first.value
                y = self.data_to_screen(QPointF(0.0, left_val)).y()
                p.drawLine(QPointF(left_x, y), QPointF(kp.x(), y))
        # Post-extrapolation: from the last key to the plot right edge.
        if last.frame < vp.frame_max:
            right_x = vp.plot_rect.right()
            kp = self.data_to_screen(QPointF(last.frame, last.value))
            if curve.post_extrap == "linear" and len(keys) >= 2:
                prev = keys[-2]
                slope = (last.value - prev.value) / (last.frame - prev.frame) \
                        if last.frame != prev.frame else 0.0
                right_frame = vp.frame_max
                right_val = last.value + slope * (right_frame - last.frame)
                p.drawLine(kp, self.data_to_screen(QPointF(right_frame, right_val)))
            else:  # constant
                right_val = last.value
                y = self.data_to_screen(QPointF(0.0, right_val)).y()
                p.drawLine(QPointF(kp.x(), y), QPointF(right_x, y))

    def _draw_segment(self, p: QPainter, seg: SplineSegment) -> None:
        s0 = self.data_to_screen(seg.p0)
        s3 = self.data_to_screen(seg.p3)
        if seg.interpolation == INTERP_HOLD:
            # horizontal then vertical
            corner = QPointF(s3.x(), s0.y())
            path = QPainterPath()
            path.moveTo(s0)
            path.lineTo(corner)
            path.lineTo(s3)
            p.drawPath(path)
            return
        if seg.interpolation == INTERP_LINEAR:
            path = QPainterPath()
            path.moveTo(s0)
            path.lineTo(s3)
            p.drawPath(path)
            return
        if seg.interpolation in (INTERP_EASE_IN, INTERP_EASE_OUT,
                                 INTERP_EASE_IN_OUT):
            # Ease modes are named progress functions. Sample those functions
            # directly instead of drawing all three as one cubic shape.
            points = sample_segment(seg, 48)
            path = QPainterPath()
            path.moveTo(self.data_to_screen(points[0]))
            for point in points[1:]:
                path.lineTo(self.data_to_screen(point))
            p.drawPath(path)
            return
        # bezier / ease: screen-space cubicTo of screen-space control points
        s1 = self.data_to_screen(seg.p1)
        s2 = self.data_to_screen(seg.p2)
        path = QPainterPath()
        path.moveTo(s0)
        path.cubicTo(s1, s2, s3)
        p.drawPath(path)

    def _draw_handles_and_keys(self, p: QPainter, curve: SplineCurve) -> None:
        # handle lines + squares (only for bezier keys)
        for k in curve.keys:
            if k.interpolation == INTERP_BEZIER:
                kp = self.data_to_screen(QPointF(k.frame, k.value))
                hl = self.data_to_screen(k.handle_left)
                hr = self.data_to_screen(k.handle_right)
                pen = QPen(QColor(C.TEXT_HINT))
                pen.setWidthF(1.0)
                p.setPen(pen)
                p.drawLine(kp, hl)
                p.drawLine(kp, hr)
                p.setBrush(QColor(C.TEXT_LABEL))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawRect(QRectF(hl.x() - 3, hl.y() - 3, 6, 6))
                p.drawRect(QRectF(hr.x() - 3, hr.y() - 3, 6, 6))
        # keyframe points
        for k in curve.keys:
            kp = self.data_to_screen(QPointF(k.frame, k.value))
            color = QColor(C.KEYFRAME_SELECTED) if k.selected else QColor(curve.color)
            p.setBrush(color)
            p.setPen(QPen(color.darker(160)))
            size = 4.0
            poly = QPolygonF([
                QPointF(kp.x(), kp.y() - size), QPointF(kp.x() + size, kp.y()),
                QPointF(kp.x(), kp.y() + size), QPointF(kp.x() - size, kp.y()),
            ])
            p.drawPolygon(poly)

    def _draw_playhead(self, p: QPainter, vp: SplineViewport, h: float) -> None:
        x = vp.plot_rect.left() + (self._current_frame - vp.frame_min) / (vp.frame_max - vp.frame_min) * vp.plot_rect.width()
        if x < vp.plot_rect.left() - 5 or x > vp.plot_rect.right() + 5:
            return
        pen = QPen(QColor(C.KEYFRAME_PLAYHEAD), 1)
        p.setPen(pen)
        p.drawLine(QPointF(x, 0), QPointF(x, h))
        p.setBrush(QColor(C.KEYFRAME_PLAYHEAD))
        p.setPen(Qt.PenStyle.NoPen)
        cap = QPolygonF([QPointF(x - 5, 0), QPointF(x + 5, 0), QPointF(x, 6)])
        p.drawPolygon(cap)

    @staticmethod
    def _nice_step(span: float) -> float:
        """Pick a 'nice' grid step (1/2/5 × 10^n) for a value span."""
        if span <= 0:
            return 1.0
        import math
        raw = span / 6.0
        mag = 10.0 ** math.floor(math.log10(raw))
        norm = raw / mag
        if norm < 1.5:
            step = 1.0
        elif norm < 3.0:
            step = 2.0
        elif norm < 7.0:
            step = 5.0
        else:
            step = 10.0
        return step * mag


def _round_step(v: float, step: float) -> float:
    import math
    return math.floor(v / step) * step


def _fmt_num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}"


def _frame_to_timecode(frame: int, fps: float) -> str:
    """Convert a frame index to SMPTE timecode HH:MM:SS:FF (batch 5)."""
    if fps <= 0:
        fps = 1.0
    total_sec = frame / fps
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = int(total_sec % 60)
    f = int(round((total_sec - int(total_sec)) * fps)) % int(round(fps))
    return f"{h:02d}:{m:02d}:{s:02d}:{f:02d}"


# ── P12 interaction methods (appended onto SplineView) ──────────────────
# These are defined as module functions and bound via __init__ to keep the
# class body focused on painting. They use the same viewport/transform API.


def _sv_mousePressEvent(self, event) -> None:
    # Middle / right button → viewport pan (AE/DaVinci convention, batch 1).
    if event.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
        sp = QPointF(event.position().x(), event.position().y())
        self._drag_mode = "pan"
        self._pan_anchor_screen = sp
        self._pan_anchor_data = self.screen_to_data(sp)
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        return
    if event.button() != Qt.MouseButton.LeftButton:
        return
    pos = event.position()
    sp = QPointF(pos.x(), pos.y())
    # P4-2: ruler area (top margin band) → playhead drag/seek.
    if sp.y() <= self._margin_top:
        self._drag_mode = "playhead"
        self._do_playhead_seek(sp.x())
        return
    # 1. handle hit (highest priority) on selected curves
    for c in self._model.visible_curves():
        if not c.selected:
            continue
        for i, k in enumerate(c.keys):
            if k.interpolation != INTERP_BEZIER:
                continue
            hl = self.data_to_screen(k.handle_left)
            hr = self.data_to_screen(k.handle_right)
            if (sp - hl).manhattanLength() <= self._HANDLE_HIT:
                self._begin_key_drag(c, i, "handle", sp, handle="left")
                return
            if (sp - hr).manhattanLength() <= self._HANDLE_HIT:
                self._begin_key_drag(c, i, "handle", sp, handle="right")
                return
    # 2. key hit
    for c in self._model.visible_curves():
        for i, k in enumerate(c.keys):
            kp = self.data_to_screen(QPointF(k.frame, k.value))
            if (sp - kp).manhattanLength() <= self._KEY_HIT:
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                if shift:
                    # Batch 3: toggle membership of the selection set; don't drag.
                    k.selected = not k.selected
                    c.selected = True
                    self._emit_key_selection_changed()
                    self.update()
                    return
                if not k.selected:
                    # Clicking an unselected key → becomes the sole selection.
                    c.selected = True
                    self._select_only_key(c, i)
                # Clicking an already-selected key → keep the whole set and
                # drag them together (handled in _begin_key_drag).
                self._begin_key_drag(c, i, "key", sp)
                return
    # 3. start box-select
    self._drag_mode = "box"
    self._box_start = sp
    self._box_end = sp
    # Shift-drag adds to the current selection instead of replacing it.
    self._box_additive = bool(event.modifiers()
                              & Qt.KeyboardModifier.ShiftModifier)
    self.update()


def _sv_mouseMoveEvent(self, event) -> None:
    sp = event.position()
    if self._drag_mode == "none":
        return
    if self._drag_mode == "playhead":
        self._do_playhead_seek(sp.x())
    elif self._drag_mode == "key":
        self._do_key_drag(QPointF(sp.x(), sp.y()))
    elif self._drag_mode == "handle":
        self._do_handle_drag(QPointF(sp.x(), sp.y()))
    elif self._drag_mode == "box":
        self._box_end = QPointF(sp.x(), sp.y())
        self.update()
    elif self._drag_mode == "pan":
        _do_pan_drag(self, QPointF(sp.x(), sp.y()))


def _do_pan_drag(self, sp) -> None:
    """Follow the cursor: keep the anchor data point under the cursor."""
    if self._pan_anchor_data is None or self._pan_anchor_screen is None:
        return
    cur = self.screen_to_data(sp)
    dframe = self._pan_anchor_data.x() - cur.x()
    dvalue = self._pan_anchor_data.y() - cur.y()
    self._pan_by(dframe, dvalue)
    # Re-anchor so the next move is incremental.
    self._pan_anchor_screen = sp


def _do_playhead_seek(self, x) -> None:
    """P4-2: ruler drag → seek playhead to the frame under cursor x."""
    dp = self.screen_to_data(QPointF(x, 0.0))
    frame = int(round(dp.x()))
    self.set_current_frame(frame)
    self.current_frame_changed.emit(frame)


def _sv_mouseReleaseEvent(self, _event) -> None:
    # Batch 3: multi-key drag commits every affected curve, not just the
    # grabbed one.
    if self._drag_mode == "key" and self._multi_drag:
        seen = set()
        for origin in (self._multi_drag_origins or []):
            cc = origin[0]
            if id(cc) in seen:
                continue
            seen.add(id(cc))
            _commit_key_edit(self, cc)
    elif self._drag_mode in ("key", "handle") and self._drag_curve is not None:
        _commit_key_edit(self, self._drag_curve)
    elif self._drag_mode == "box":
        self._finish_box_select()
    elif self._drag_mode == "pan":
        self.unsetCursor()
    self._drag_mode = "none"
    self._drag_curve = None
    self._drag_key_idx = None
    self._drag_handle = None
    self._box_start = None
    self._box_end = None
    self._pan_anchor_screen = None
    self._pan_anchor_data = None
    self._multi_drag = False
    self._multi_drag_anchor = None
    self._multi_drag_origins = None
    # Batch 6: clear snap guides when the drag ends.
    self._snap_guide_frame = None
    self._snap_guide_value = None
    self._emit_key_selection_changed()
    _emit_selected_key_changed(self)
    self.update()


# ── drag helpers ──

def _begin_key_drag(self, curve, key_idx, mode, sp, handle=None) -> None:
    self._drag_mode = mode
    self._drag_curve = curve
    self._drag_key_idx = key_idx
    self._drag_handle = handle
    # Batch 3: if grabbing a key that's already selected (and no Shift held),
    # drag every selected key together. Snapshot each origin so the delta is
    # computed against the press point, not the live cursor.
    if mode == "key" and curve.keys[key_idx].selected:
        origins = []
        for cc in self._model.visible_curves():
            for ki, kk in enumerate(cc.keys):
                if kk.selected:
                    # AUD-10911: keep the key OBJECT in the snapshot so later
                    # ticks can match origins by identity — the press-time
                    # frame alone stops matching as soon as the key moves.
                    origins.append((cc, ki, kk, kk.frame, kk.value,
                                    QPointF(kk.handle_left),
                                    QPointF(kk.handle_right)))
        if len(origins) > 1:
            self._multi_drag = True
            self._multi_drag_anchor = self.screen_to_data(sp)
            self._multi_drag_origins = origins


def _do_multi_key_drag(self, sp) -> None:
    """Move every selected key by the same (dframe, dvalue) delta (batch 3).

    Keys are clamped to stay ordered and within [min_value, max_value].

    AUD-10911: origins are matched to live keys by object identity. The old
    frame-snapshot matching (``k.frame in sel_set``) froze the drag after
    the first non-zero move event — once a key left its press-time frame,
    every later tick skipped it. Each tick now resets the tracked keys to
    their origins and applies the TOTAL delta, so neighbour clamping always
    judges stable origin-relative targets.

    AUD-10913: handles are translated by the full (frame, value) delta so
    they keep their shape relative to the key (previously only x moved).
    """
    if (self._multi_drag_origins is None
            or self._multi_drag_anchor is None):
        return
    dp = self.screen_to_data(sp)
    dframe = int(round(dp.x() - self._multi_drag_anchor.x()))
    dvalue = dp.y() - self._multi_drag_anchor.y()
    if dframe == 0 and dvalue == 0.0:
        return
    # Group origins by curve so we can clamp each key against its in-curve
    # neighbours (which themselves may be moving).
    by_curve: dict = {}
    for cc, _ki, kk, f0, v0, hl0, hr0 in self._multi_drag_origins:
        by_curve.setdefault(id(cc), (cc, {}))[1][id(kk)] = \
            (kk, f0, v0, hl0, hr0)
    for cc, origin_map in by_curve.values():
        keys = sorted(cc.keys, key=lambda k: k.frame)
        pos = {id(k): i for i, k in enumerate(keys)}
        # Reset the tracked keys to their press-time origins so the clamp
        # below always works from the same starting frames.
        for kk, f0, _v0, _hl0, _hr0 in origin_map.values():
            kk.frame = f0
        # Compute clamped delta per key: a key can't move past its neighbour.
        # Right-moving keys move rightmost-first so each clamps against the
        # already-moved neighbour; left-moving keys leftmost-first (same
        # ordering rule as _nudge_selected_keys, AUD-10912).
        ordered = keys if dframe <= 0 else list(reversed(keys))
        for k in ordered:
            origin = origin_map.get(id(k))
            if origin is None:
                continue
            _, f0, v0, hl0, hr0 = origin
            i = pos[id(k)]
            lo = keys[i - 1].frame + 1 if i > 0 else 0
            hi = (keys[i + 1].frame - 1 if i + 1 < len(keys)
                  else 10 ** 9)
            new_frame = max(lo, min(hi, f0 + dframe))
            actual = new_frame - k.frame
            new_value = _clampf(v0 + dvalue, cc.min_value, cc.max_value)
            delta_value = new_value - v0
            k.frame = new_frame
            k.value = new_value
            k.handle_left = QPointF(hl0.x() + actual, hl0.y() + delta_value)
            k.handle_right = QPointF(hr0.x() + actual, hr0.y() + delta_value)
    self.update()


def _do_key_drag(self, sp) -> None:
    # Batch 3: multi-key path delegates to _do_multi_key_drag.
    if self._multi_drag:
        _do_multi_key_drag(self, sp)
        return
    if self._drag_curve is None or self._drag_key_idx is None:
        return
    dp = self.screen_to_data(sp)
    key = self._drag_curve.keys[self._drag_key_idx]
    new_frame = dp.x()
    new_value = dp.y()
    # Batch 6: snap to playhead / sibling frames + value grid, unless Shift
    # is held (precise mode). Snap is judged in screen pixels.
    if not _shift_held():
        new_frame = self._snap_frame(new_frame, self._drag_curve, exclude_key=key)
        new_value = self._snap_value(new_value, self._drag_curve)
    else:
        self._snap_guide_frame = None
        self._snap_guide_value = None
    new_frame = int(round(new_frame))
    # Don't allow crossing neighbours.
    if self._drag_key_idx > 0:
        new_frame = max(new_frame, self._drag_curve.keys[self._drag_key_idx - 1].frame + 1)
    if self._drag_key_idx < len(self._drag_curve.keys) - 1:
        new_frame = min(new_frame, self._drag_curve.keys[self._drag_key_idx + 1].frame - 1)
    # Shift handles by the same delta so the curve shape follows the key.
    # AUD-10913: translate BOTH axes — y must follow the value change too,
    # otherwise the handles stop hugging the key after a vertical drag and
    # the bezier overshoots/kinks.
    delta_frame = new_frame - key.frame
    new_value = _clampf(new_value, self._drag_curve.min_value,
                        self._drag_curve.max_value)
    delta_value = new_value - key.value
    key.frame = new_frame
    key.value = new_value
    key.handle_left = QPointF(key.handle_left.x() + delta_frame,
                              key.handle_left.y() + delta_value)
    key.handle_right = QPointF(key.handle_right.x() + delta_frame,
                               key.handle_right.y() + delta_value)
    self.update()


def _do_handle_drag(self, sp) -> None:
    if (self._drag_curve is None or self._drag_key_idx is None
            or self._drag_handle is None):
        return
    dp = self.screen_to_data(sp)
    key = self._drag_curve.keys[self._drag_key_idx]
    # clamp handle x to neighbour range (§11.4)
    prev_frame = self._drag_curve.keys[self._drag_key_idx - 1].frame if self._drag_key_idx > 0 else key.frame
    next_frame = self._drag_curve.keys[self._drag_key_idx + 1].frame if self._drag_key_idx < len(self._drag_curve.keys) - 1 else key.frame
    if self._drag_handle == "left":
        x = _clampf(dp.x(), prev_frame, key.frame)
        key.handle_left = QPointF(x, dp.y())
    else:
        x = _clampf(dp.x(), key.frame, next_frame)
        key.handle_right = QPointF(x, dp.y())
    # smooth mode mirrors the opposite handle (§11.8); broken mode leaves
    # the two sides independent (batch 4).
    if key.tangent_mode == "smooth":
        from .spline_model import apply_smooth_opposite
        apply_smooth_opposite(key, self._drag_handle)
    self.update()


def _commit_key_edit(self, curve) -> None:
    """Emit keyframe_changed so CompositePage persists the edit."""
    kfs = []
    for k in sorted(curve.keys, key=lambda kk: kk.frame):
        kfs.append(k.to_dict())
    self.keyframe_changed.emit(curve.node_id, curve.param_name, kfs)


def _select_only_key(self, curve, key_idx) -> None:
    """Sole-select one key — mutually exclusive, like a tree-row click.

    AUD-10914: other curves' ``selected`` flag is cleared too. Previously
    only the keys were cleared, so the previously-selected curve stayed
    "selected" (still drawn with handles/keys) and
    ``curve_selection_changed`` kept reporting the OLD curve, highlighting
    the wrong parameter row in the Keyframes panel.
    """
    for c in self._model.visible_curves():
        c.selected = c is curve
        for k in c.keys:
            k.selected = False
    curve.keys[key_idx].selected = True
    # T61: a key click may have flipped the curve selection too — notify.
    _emit_curve_selection(self)
    _emit_key_selection_changed(self)


def _emit_curve_selection(self) -> None:
    """T61 helper: find the currently-selected curve and emit
    ``curve_selection_changed``. Called after any view interaction that may
    have changed which curve is selected (key click, box-select landing on
    a different curve). No-op when the selection didn't change."""
    sel = ""
    for c in self._model.visible_curves():
        if c.selected:
            sel = c.curve_id
            break
    # Avoid spurious emits when the selection is unchanged.
    if getattr(self, "_last_emitted_curve_sel", None) != sel:
        self._last_emitted_curve_sel = sel
        self.curve_selection_changed.emit(sel)


def _finish_box_select(self) -> None:
    if self._box_start is None or self._box_end is None:
        return
    r = QRectF(self._box_start, self._box_end).normalized()
    for c in self._model.visible_curves():
        for k in c.keys:
            kp = self.data_to_screen(QPointF(k.frame, k.value))
            if self._box_additive:
                # Shift-box: only ADD to selection, never remove existing.
                if r.contains(kp):
                    k.selected = True
            else:
                k.selected = r.contains(kp)
    _emit_key_selection_changed(self)


def _delete_selected_keys(self) -> None:
    for c in list(self._model.visible_curves()):
        kept = [k for k in c.keys if not k.selected]
        if len(kept) == len(c.keys):
            continue
        c.keys = kept
        kfs = [k.to_dict() for k in sorted(kept, key=lambda kk: kk.frame)]
        self.keyframe_changed.emit(c.node_id, c.param_name, kfs)
    _emit_key_selection_changed(self)
    self.update()


def _clampf(v, lo, hi):
    return max(lo, min(hi, v))


def event_modifiers_shift():
    # In headless tests there's no real modifier; default to False.
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            return bool(app.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier)
    except Exception:
        pass
    return False


def _shift_held() -> bool:
    """Batch 6: read the live Shift modifier (False in headless tests)."""
    return event_modifiers_shift()


# ── batch 2: selection / clipboard / menu / hotkeys ─────────────────────


def _selected_keys(self):
    """Yield (curve, idx, key) for every selected key across visible curves."""
    for c in self._model.visible_curves():
        for i, k in enumerate(c.keys):
            if k.selected:
                yield c, i, k


def _select_all_keys(self) -> None:
    """Select every key on every visible curve (Ctrl+A)."""
    any_sel = False
    for c in self._model.visible_curves():
        for k in c.keys:
            k.selected = True
            any_sel = True
    if any_sel:
        _emit_key_selection_changed(self)
        self.update()


def _deselect_all_keys(self) -> None:
    for c in self._model.visible_curves():
        for k in c.keys:
            k.selected = False
    _emit_key_selection_changed(self)
    self.update()


def copy_selected_keys(self) -> int:
    """Copy selected keys into the clipboard, anchored to the earliest frame.

    Each entry stores (curve_id, rel_frame, key_dict) so paste re-anchors the
    whole block at the playhead (AE convention). Returns copied count.
    """
    sel = list(_selected_keys(self))
    if not sel:
        return 0
    base_frame = min(k.frame for _, _, k in sel)
    clip = []
    for c, _, k in sel:
        clip.append({
            "curve_id": c.curve_id,
            "node_id": c.node_id,
            "param_name": c.param_name,
            "rel_frame": k.frame - base_frame,
            "data": k.to_dict(),
        })
    self._key_clipboard = clip
    return len(clip)


def has_key_clipboard(self) -> bool:
    return bool(self._key_clipboard)


def paste_keys_at_frame(self, at_frame: Optional[int] = None) -> bool:
    """Paste the clipboard re-anchored at ``at_frame`` (default: playhead).

    Collisions (existing key at the same frame on the same curve) are replaced.
    Emits keyframe_changed per affected curve so CompositePage persists + undo.
    Returns True if anything was pasted.
    """
    if not self._key_clipboard:
        return False
    if at_frame is None:
        at_frame = self._current_frame
    at_frame = max(0, int(at_frame))
    _deselect_all_keys(self)
    affected: dict = {}
    for item in self._key_clipboard:
        curve = self._model.curve_by_id(item.get("curve_id"))
        if curve is None:
            continue
        data = dict(item.get("data") or {})
        frame = at_frame + int(item.get("rel_frame", 0))
        data["frame"] = frame
        # Rebuild a SplineKey from the payload so handles/tangent survive.
        try:
            from .spline_model import SplineKey
            hl = data.get("handle_left", [0, 0])
            hr = data.get("handle_right", [0, 0])
            nk = SplineKey(
                frame=frame,
                value=float(data.get("value", 0.0)),
                interpolation=str(data.get("interpolation", "linear")),
                tangent_mode=str(data.get("tangent_mode", "auto")),
                handle_left=QPointF(frame + float(hl[0]),
                                    float(data.get("value", 0.0)) + float(hl[1])),
                handle_right=QPointF(frame + float(hr[0]),
                                     float(data.get("value", 0.0)) + float(hr[1])),
                selected=True,
            )
        except (TypeError, ValueError, IndexError):
            continue
        # Replace any key at the target frame, then insert sorted.
        curve.keys = [kk for kk in curve.keys if kk.frame != frame]
        curve.keys.append(nk)
        curve.keys.sort(key=lambda kk: kk.frame)
        affected[curve.curve_id] = curve
    for curve in affected.values():
        kfs = [k.to_dict() for k in sorted(curve.keys, key=lambda kk: kk.frame)]
        self.keyframe_changed.emit(curve.node_id, curve.param_name, kfs)
    _emit_key_selection_changed(self)
    self.update()
    return bool(affected)


def cut_selected_keys(self) -> int:
    """Cut = copy then delete (Ctrl+Shift+C)."""
    n = copy_selected_keys(self)
    if n:
        _delete_selected_keys(self)
    return n


def _nudge_selected_keys(self, dframe: int) -> None:
    """Move every selected key by ±dframe frames (arrow keys).

    Keys keep their order: clamped to >= 0 and to not crossing neighbours.
    Shift+arrow passes dframe=±10 for a bigger step.
    """
    if dframe == 0:
        return
    targets = []
    for c in self._model.visible_curves():
        keys = sorted(c.keys, key=lambda k: k.frame)
        # AUD-10912: iterate right-moving keys rightmost-first (and
        # left-moving leftmost-first) so each key clamps against its
        # ALREADY-MOVED neighbour. Ascending order in both directions clamped
        # right-moving keys against not-yet-moved neighbours and tore a block
        # of adjacent selected keys apart ((10, 11) +1 → (10, 12)).
        indexed = list(enumerate(keys))
        if dframe > 0:
            indexed.reverse()
        for i, k in indexed:
            if not k.selected:
                continue
            lo = keys[i - 1].frame + 1 if i > 0 else 0
            hi = (keys[i + 1].frame - 1 if i + 1 < len(keys)
                  else 10 ** 9)
            new_frame = max(lo, min(hi, k.frame + dframe))
            if new_frame != k.frame:
                delta = new_frame - k.frame
                k.frame = new_frame
                k.handle_left = QPointF(k.handle_left.x() + delta,
                                        k.handle_left.y())
                k.handle_right = QPointF(k.handle_right.x() + delta,
                                         k.handle_right.y())
                targets.append(c)
    for c in targets:
        _commit_key_edit(self, c)
    if targets:
        self.update()


def _set_selected_interpolation(self, mode: str) -> None:
    """Apply an interpolation type to every selected key (menu action)."""
    changed = False
    for c, _, k in list(_selected_keys(self)):
        if k.interpolation != mode:
            k.interpolation = mode
            changed = True
    if changed:
        # Commit per affected curve.
        seen = set()
        for c, _, _ in _selected_keys(self):
            if id(c) in seen:
                continue
            seen.add(id(c))
            _commit_key_edit(self, c)
        self.update()


def _set_selected_tangent(self, mode: str) -> None:
    """Apply a tangent mode to every selected key (menu action)."""
    seen = set()
    for c, i, k in list(_selected_keys(self)):
        set_key_tangent_mode(self, c, i, mode)
        seen.add(id(c))


def _set_selected_extrapolation(self, side: str, mode: str) -> None:
    """Set pre/post extrapolation on every selected curve (batch 7)."""
    if side not in ("pre", "post") or mode not in ("constant", "linear"):
        return
    changed = False
    for c in self._model.visible_curves():
        if not c.selected:
            continue
        if side == "pre":
            if c.pre_extrap != mode:
                c.pre_extrap = mode
                changed = True
        else:
            if c.post_extrap != mode:
                c.post_extrap = mode
                changed = True
    if changed:
        for c in self._model.visible_curves():
            if c.selected:
                self.curve_settings_changed.emit(
                    c.node_id, c.param_name, c.pre_extrap, c.post_extrap)
        self.update()


def _sv_keyPressEvent(self, event) -> None:
    """Batch 2 hotkeys: Delete, Ctrl+C/V/A/X, arrows, F (fit)."""
    key = event.key()
    mods = event.modifiers()
    sel_changed = False
    if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
        _delete_selected_keys(self)
        sel_changed = True
    elif key == Qt.Key.Key_F and not mods:
        self.fit_view()
    elif key == Qt.Key.Key_A and (mods & Qt.KeyboardModifier.ControlModifier):
        _select_all_keys(self)
        sel_changed = True
    elif key == Qt.Key.Key_D and (mods & Qt.KeyboardModifier.ControlModifier):
        _deselect_all_keys(self)
        sel_changed = True
    elif key == Qt.Key.Key_C and (mods & Qt.KeyboardModifier.ControlModifier):
        if mods & Qt.KeyboardModifier.ShiftModifier:
            cut_selected_keys(self)
            sel_changed = True
        else:
            copy_selected_keys(self)
    elif key == Qt.Key.Key_X and (mods & Qt.KeyboardModifier.ControlModifier):
        cut_selected_keys(self)
        sel_changed = True
    elif key == Qt.Key.Key_V and (mods & Qt.KeyboardModifier.ControlModifier):
        paste_keys_at_frame(self)
        sel_changed = True
    elif key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
        step = 10 if (mods & Qt.KeyboardModifier.ShiftModifier) else 1
        _nudge_selected_keys(self, -step if key == Qt.Key.Key_Left else step)
        sel_changed = True
    else:
        QWidget.keyPressEvent(self, event)
    if sel_changed:
        _emit_selected_key_changed(self)


def _sv_contextMenuEvent(self, event) -> None:
    """Right-click menu (batch 2): edit ops, interpolation, tangent, view."""
    from src.shared.i18n.i18n import I18N
    has_sel = any(True for _ in _selected_keys(self))
    menu = QMenu(self)
    if has_sel:
        act_copy = menu.addAction(I18N.tr("keyframes.copy_kf", "Copy keyframes"))
        act_copy.triggered.connect(lambda: copy_selected_keys(self))
        act_cut = menu.addAction(I18N.tr("keyframes.cut_kf", "Cut keyframes"))
        act_cut.triggered.connect(lambda: cut_selected_keys(self))
    act_paste = menu.addAction(I18N.tr("keyframes.paste_kf",
                                       "Paste keyframes at playhead"))
    act_paste.setEnabled(has_key_clipboard(self))
    act_paste.triggered.connect(lambda: paste_keys_at_frame(self))
    if has_sel:
        act_del = menu.addAction(I18N.tr("keyframes.delete_kf",
                                         "Delete keyframes"))
        # AUD-10910: ``_delete_selected_keys`` is a module-level function taking
        # the view as ``self``; connecting it directly lets ``triggered(bool)``
        # pass ``checked=False`` as that argument (False._model → AttributeError
        # → abort). Bind the view explicitly like the other menu entries.
        act_del.triggered.connect(lambda _checked=False: _delete_selected_keys(self))
        menu.addSeparator()
        # Interpolation submenu.
        interp_menu = menu.addMenu(I18N.tr("keyframes.interpolation",
                                           "Interpolation"))
        for mode, label in [("linear", "Linear"),
                            ("bezier", "Bezier"),
                            ("hold", "Hold / Step"),
                            ("ease_in", "Ease In"),
                            ("ease_out", "Ease Out"),
                            ("ease_in_out", "Ease In / Out")]:
            ia = interp_menu.addAction(label)
            ia.triggered.connect(lambda _checked=False, m=mode:
                                 _set_selected_interpolation(self, m))
        # Tangent submenu.
        tan_menu = menu.addMenu(I18N.tr("keyframes.tangent_menu", "Tangent"))
        for mode, label in [("smooth", "Smooth"),
                            ("linear", "Linear"),
                            ("flat", "Flat"),
                            ("step", "Step"),
                            ("auto", "Auto"),
                            ("broken", "Broken")]:
            ta = tan_menu.addAction(label)
            ta.triggered.connect(lambda _checked=False, m=mode:
                                 _set_selected_tangent(self, m))
        # Batch 7: extrapolation submenu for the selected curve.
        extrap_menu = menu.addMenu(I18N.tr("spline.extrapolation",
                                           "Extrapolation"))
        for side, side_label in [("pre", "Pre (before first key)"),
                                 ("post", "Post (after last key)")]:
            sub = extrap_menu.addMenu(side_label)
            for mode, mlabel in [("constant", "Constant"),
                                 ("linear", "Linear")]:
                ea = sub.addAction(mlabel)
                ea.triggered.connect(lambda _checked=False, s=side, m=mode:
                                     _set_selected_extrapolation(self, s, m))
    menu.addSeparator()
    act_all = menu.addAction(I18N.tr("keyframes.select_all", "Select all"))
    # AUD-10910: same as Delete above — bind the view, do not let triggered(bool)
    # inject ``False`` as the module function's ``self`` argument.
    act_all.triggered.connect(lambda _checked=False: _select_all_keys(self))
    act_fit = menu.addAction(I18N.tr("spline.fit", "Fit curves"))
    act_fit.triggered.connect(self.fit_view)
    menu.exec(event.globalPos())


def _emit_selected_key_changed(self) -> None:
    """Notify listeners (footer readout) of the current sole-selected key.

    Emits (frame, value) when exactly one key is selected across all curves,
    otherwise None. Batch 4.
    """
    sel = [(k.frame, k.value) for _, _, k in _selected_keys(self)]
    self.selected_key_changed.emit(sel[0] if len(sel) == 1 else None)


def _emit_key_selection_changed(self) -> None:
    """Publish selected Spline points using shared timeline row ids."""
    pairs = []
    for curve in self._model.visible_curves():
        row_id = f"param:{curve.node_id}:{curve.param_name}"
        for key in curve.keys:
            if key.selected:
                pairs.append((row_id, int(key.frame)))
    self.key_selection_changed.emit(tuple(pairs))


def _sv_mouseDoubleClickEvent(self, event) -> None:
    """Double-click a key → precise frame/value edit dialog (batch 4).

    Double-click empty plot space at the playhead frame → add a new key on
    the selected curve at that frame/value (AE "add key at cursor").
    """
    if event.button() != Qt.MouseButton.LeftButton:
        return
    sp = QPointF(event.position().x(), event.position().y())
    # 1. Did we hit an existing key?
    for c in self._model.visible_curves():
        for i, k in enumerate(c.keys):
            kp = self.data_to_screen(QPointF(k.frame, k.value))
            if (sp - kp).manhattanLength() <= self._KEY_HIT:
                _edit_key_dialog(self, c, i)
                return
    # 2. Empty space → add a key on the selected curve at the cursor.
    sel_curve = next((c for c in self._model.visible_curves() if c.selected),
                     None)
    if sel_curve is None:
        return
    dp = self.screen_to_data(sp)
    frame = int(round(dp.x()))
    value = _clampf(dp.y(), sel_curve.min_value, sel_curve.max_value)
    # Don't duplicate an existing frame.
    if any(k.frame == frame for k in sel_curve.keys):
        return
    from .spline_model import SplineKey
    nk = SplineKey(frame=frame, value=value)
    sel_curve.keys.append(nk)
    sel_curve.keys.sort(key=lambda kk: kk.frame)
    # Recompute auto-tangent for the new key + its neighbours.
    from .spline_model import apply_auto_tangent
    keys = sel_curve.keys
    ni = keys.index(nk)
    apply_auto_tangent(keys[ni - 1] if ni > 0 else None, nk,
                       keys[ni + 1] if ni + 1 < len(keys) else None)
    _commit_key_edit(self, sel_curve)
    self.update()


def _edit_key_dialog(view, curve, key_idx) -> None:
    """Pop a two-field dialog to set a key's frame/value precisely (batch 4)."""
    from src.shared.i18n.i18n import I18N
    k = curve.keys[key_idx]
    # Frame field.
    frame, fok = QInputDialog.getInt(
        view,
        I18N.tr("spline.edit_frame", "Edit keyframe frame"),
        I18N.tr("spline.frame_label", "Frame:"),
        value=k.frame, min=0, max=10 ** 8)
    if not fok:
        return
    # Value field.
    value, vok = QInputDialog.getDouble(
        view,
        I18N.tr("spline.edit_value", "Edit keyframe value"),
        I18N.tr("spline.value_label", "Value:"),
        value=k.value, decimals=4)
    if not vok:
        return
    # Clamp frame into the neighbour range so order is preserved.
    keys = sorted(curve.keys, key=lambda kk: kk.frame)
    pos = keys.index(k)
    lo = keys[pos - 1].frame + 1 if pos > 0 else 0
    hi = (keys[pos + 1].frame - 1 if pos + 1 < len(keys)
          else 10 ** 8)
    frame = max(lo, min(hi, frame))
    # AUD-10913: translate handles by the full (frame, value) delta so they
    # keep their shape relative to the key after a value change too.
    delta = frame - k.frame
    new_value = _clampf(value, curve.min_value, curve.max_value)
    delta_value = new_value - k.value
    k.frame = frame
    k.value = new_value
    k.handle_left = QPointF(k.handle_left.x() + delta,
                            k.handle_left.y() + delta_value)
    k.handle_right = QPointF(k.handle_right.x() + delta,
                             k.handle_right.y() + delta_value)
    _commit_key_edit(view, curve)
    view.update()


# Bind interaction methods onto SplineView (kept out of the class body for
# painting focus, but functionally identical to inline methods).
SplineView._begin_key_drag = _begin_key_drag
SplineView._do_key_drag = _do_key_drag
SplineView._do_handle_drag = _do_handle_drag
SplineView._do_playhead_seek = _do_playhead_seek
SplineView._do_pan_drag = _do_pan_drag
SplineView._commit_key_edit = _commit_key_edit
SplineView._select_only_key = _select_only_key
SplineView._emit_curve_selection = _emit_curve_selection
SplineView._finish_box_select = _finish_box_select
SplineView._delete_selected_keys = _delete_selected_keys
SplineView._selected_keys = _selected_keys
SplineView._select_all_keys = _select_all_keys
SplineView._deselect_all_keys = _deselect_all_keys
SplineView.copy_selected_keys = copy_selected_keys
SplineView.has_key_clipboard = has_key_clipboard
SplineView.paste_keys_at_frame = paste_keys_at_frame
SplineView.cut_selected_keys = cut_selected_keys
SplineView._nudge_selected_keys = _nudge_selected_keys
SplineView._set_selected_interpolation = _set_selected_interpolation
SplineView._set_selected_tangent = _set_selected_tangent
SplineView._set_selected_extrapolation = _set_selected_extrapolation
SplineView.mousePressEvent = _sv_mousePressEvent
SplineView.mouseMoveEvent = _sv_mouseMoveEvent
SplineView.mouseReleaseEvent = _sv_mouseReleaseEvent
SplineView.mouseDoubleClickEvent = _sv_mouseDoubleClickEvent
SplineView.keyPressEvent = _sv_keyPressEvent
SplineView.contextMenuEvent = _sv_contextMenuEvent
SplineView._emit_selected_key_changed = _emit_selected_key_changed
SplineView._emit_key_selection_changed = _emit_key_selection_changed


def set_key_tangent_mode(view, curve, key_idx, mode) -> None:
    """Switch a key's tangent mode (smooth/linear/broken/flat/step/auto) — P12.

    After switching, the key's handles are recomputed so the curve reflects
    the new mode immediately:
    - auto/smooth: auto-tangent from neighbours (collinear).
    - flat: handles horizontal at the key's value.
    - linear: handles collapse onto the key (straight segments).
    - step: segment becomes hold (no interpolation).
    - broken: handles left as-is; the two sides are now independent, so
      dragging one side never mirrors the other (batch 4).
    """
    from .spline_model import (apply_auto_tangent, INTERP_BEZIER,
                                INTERP_HOLD, INTERP_LINEAR)
    keys = curve.keys
    k = keys[key_idx]
    prev_k = keys[key_idx - 1] if key_idx > 0 else None
    next_k = keys[key_idx + 1] if key_idx < len(keys) - 1 else None
    k.tangent_mode = mode
    if mode in ("auto", "smooth"):
        # A tangent mode only has a visible/evaluated effect on a Bezier
        # segment.  Promote a linear key when the user explicitly chooses a
        # handle-based tangent, otherwise the handles appear to do nothing.
        k.interpolation = INTERP_BEZIER
        apply_auto_tangent(prev_k, k, next_k)
    elif mode == "flat":
        k.interpolation = INTERP_BEZIER
        k.handle_left = QPointF(k.frame - 5, k.value)
        k.handle_right = QPointF(k.frame + 5, k.value)
    elif mode == "linear":
        k.interpolation = INTERP_LINEAR
        k.handle_left = QPointF(k.frame, k.value)
        k.handle_right = QPointF(k.frame, k.value)
    elif mode == "step":
        k.interpolation = INTERP_HOLD
    elif mode == "broken":
        k.interpolation = INTERP_BEZIER
        # Keep current handles; just flag independence. If the key had no real
        # handles yet (collapsed on the key), give it small defaults so the
        # user has something to grab on each side.
        if (abs(k.handle_left.x() - k.frame) < 1e-6
                and abs(k.handle_left.y() - k.value) < 1e-6):
            k.handle_left = QPointF(k.frame - 5, k.value)
        if (abs(k.handle_right.x() - k.frame) < 1e-6
                and abs(k.handle_right.y() - k.value) < 1e-6):
            k.handle_right = QPointF(k.frame + 5, k.value)
    view._commit_key_edit(curve)
    view.update()
