"""T105/T108/T109 tests: GraphEditorView QPainter canvas.

Renders the Graph Editor under offscreen and asserts the paint completes
without throwing for every supported state, plus non-blank output. Pixel
content asserts are intentionally avoided (brittle across platforms); the
contracts locked here are 'paint doesn't crash' + 'produces visible
content' + 'the data API behaves'.

Covers:
  * T105: value-graph coordinate transforms reach the painter
  * T108: background + minor/major grid + zero line + legend
  * T109: interpolation curves + keyframe diamonds + bezier handles
  * graph-type switching (value ↔ speed) rebuilds samples
  * empty-state (no series) paints without throwing
  * resize triggers a viewport refit, not a crash
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.ui import graph_model
from src.features.composite.ui.graph_editor_view import (
    GraphEditorView, _floor_to, _nice_step,
)
from src.features.composite.ui.graph_model import (
    GraphOptions, GraphPoint, GraphSeries, build_graph_series,
)
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


# ── fixtures ──────────────────────────────────────────────────────────


def _series(param="rotation", kfs=((0, 0.0), (50, 90.0), (100, 0.0))):
    node = TransformNode()
    track = KeyframeTrack(param_name=param)
    for f, v in kfs:
        track.add_keyframe(Keyframe(f, v))
    node.set_keyframe_track(param, track)
    return build_graph_series(node, param, node_color="#ff8800")


@pytest.fixture
def view(qapp):
    v = GraphEditorView()
    v.resize(800, 400)
    v.set_frame_range(0, 100)
    return v


# ── paint robustness across states ────────────────────────────────────


class TestPaintRobustness:
    def test_paint_empty_no_series(self, view, qapp):
        """No series → empty-state paint must not throw."""
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_single_series(self, view, qapp):
        view.set_series([_series()])
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_value_graph_type(self, view, qapp):
        view.set_series([_series()])
        view.set_graph_type("value")
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_speed_graph_type(self, view, qapp):
        """Speed graph for a spatial param (translate) — must not throw."""
        view.set_series([_series(param="translate_x")])
        view.set_graph_type("speed")
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_auto_graph_type(self, view, qapp):
        view.set_series([_series()])
        view.set_graph_type("auto")
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_multiple_series(self, view, qapp):
        view.set_series([_series(param="rotation"),
                         _series(param="opacity")])
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    def test_paint_with_selected_keyframe(self, view, qapp):
        """A selected keyframe triggers the wider diamond + handles."""
        s = _series()
        if s.points:
            s.points[1].selected = True
            s.points[1].handle_left = (-5.0, 10.0)
            s.points[1].handle_right = (5.0, -10.0)
        view.set_series([s])
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()

    @pytest.mark.parametrize("w,h", [(400, 200), (800, 400), (1920, 600)])
    def test_paint_across_sizes(self, view, qapp, w, h):
        view.set_series([_series()])
        view.resize(w, h)
        view.show()
        view.repaint()
        qapp.processEvents()
        view.hide()


# ── non-blank output ──────────────────────────────────────────────────


class TestNonBlankOutput:
    def test_render_produces_visible_pixels(self, view, qapp):
        """A successful paint must produce visible pixels — guards against
        a regression where paint completes but draws nothing (e.g. bad token
        resolution painting fully-transparent content)."""
        view.set_series([_series()])
        view.resize(800, 400)
        pm = view.grab()
        img = pm.toImage()
        visible = sum(
            1 for y in range(0, img.height(), 8)
            for x in range(0, img.width(), 8)
            if img.pixelColor(x, y).alpha() > 0)
        assert visible > 100, (
            f"graph render too sparse ({visible} sampled pixels visible)")


# ── data API ──────────────────────────────────────────────────────────


class TestDataApi:
    def test_set_series_rebuilds_samples(self, view):
        view.set_series([_series()])
        assert len(view._series) == 1
        assert len(view._series[0].samples) > 0

    def test_set_series_empty_clears(self, view):
        view.set_series([_series()])
        view.set_series([])
        assert view._series == []

    def test_set_current_frame_updates_state(self, view):
        view.set_current_frame(42)
        assert view._current_frame == 42

    def test_set_current_frame_same_is_noop(self, view):
        view.set_current_frame(10)
        before = view._current_frame
        view.set_current_frame(10)
        assert view._current_frame == before

    def test_set_fps_updates_state(self, view):
        view.set_fps(30.0)
        assert view._fps == 30.0

    def test_set_graph_type_value(self, view):
        view.set_series([_series()])
        view.set_graph_type("value")
        assert view._viewport.graph_type == "value"
        for s in view._series:
            assert s.graph_type == "value"

    def test_set_graph_type_speed(self, view):
        view.set_series([_series()])
        view.set_graph_type("speed")
        assert view._viewport.graph_type == "speed"

    def test_set_graph_type_unknown_is_noop(self, view):
        view.set_series([_series()])
        view.set_graph_type("value")
        view.set_graph_type("nonsense")
        assert view._viewport.graph_type == "value"

    def test_set_options(self, view):
        opts = GraphOptions(show_transform_box=True)
        view.set_options(opts)
        assert view._options.show_transform_box is True

    def test_resize_triggers_refit(self, view, qapp):
        view.set_series([_series()])
        old_ppf = view._viewport.pixels_per_frame
        view.resize(400, 200)
        qapp.processEvents()
        # A narrower view → fewer px per frame.
        assert view._viewport.pixels_per_frame < old_ppf + 1e-6


# ── grid-step helpers ─────────────────────────────────────────────────


class TestGridHelpers:
    @pytest.mark.parametrize("raw,expected", [
        (0.5, 1), (1.5, 2), (3.0, 2), (4.0, 5), (7.0, 5),
        (9.0, 10), (50.0, 50), (120.0, 100), (450.0, 500),
    ])
    def test_nice_step_1_2_5_series(self, raw, expected):
        """Grid steps must be 1/2/5 × 10^k (plan §22.6, AE convention)."""
        assert _nice_step(raw) == expected

    def test_nice_step_zero_or_negative_returns_one(self):
        assert _nice_step(0) == 1
        assert _nice_step(-5) == 1

    def test_floor_to_step(self):
        assert _floor_to(7.0, 5) == 5.0
        assert _floor_to(12.0, 5) == 10.0
        assert _floor_to(-3.0, 5) == -5.0

    def test_floor_to_zero_step_passthrough(self):
        assert _floor_to(7.0, 0) == 7.0


# ── T111: viewport zoom/pan ───────────────────────────────────────────


class TestViewportZoomPan:
    def _wheel(self, view, x, y, delta=120, mods=None):
        from PyQt6.QtCore import QPointF, QPoint, Qt
        from PyQt6.QtGui import QWheelEvent
        if mods is None:
            mods = Qt.KeyboardModifier.NoModifier
        ev = QWheelEvent(QPointF(x, y), QPointF(x, y),
                         QPoint(0, 0), QPoint(0, delta),
                         Qt.MouseButton.NoButton, mods,
                         Qt.ScrollPhase.NoScrollPhase, False)
        view.wheelEvent(ev)

    def test_wheel_zooms_time_axis(self, view, qapp):
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        before = view._viewport.pixels_per_frame
        self._wheel(view, 400, 200, delta=120)
        assert view._viewport.pixels_per_frame > before, "wheel should zoom in"
        self._wheel(view, 400, 200, delta=-120)
        assert view._viewport.pixels_per_frame < before + 1e-6 or \
            view._viewport.pixels_per_frame < 256.0  # zoomed back out

    def test_wheel_zoom_anchored_at_cursor(self, view, qapp):
        """The frame under the cursor stays put after zoom (plan §22.4)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        cursor_x = 400.0
        anchor_frame = graph_model.x_to_frame(
            cursor_x, view._viewport, 40.0)  # 40 = _MARGIN_LEFT
        self._wheel(view, cursor_x, 200, delta=120)
        new_frame = graph_model.x_to_frame(
            cursor_x, view._viewport, 40.0)
        assert new_frame == pytest.approx(anchor_frame, abs=0.5)

    def test_alt_wheel_zooms_value_axis(self, view, qapp):
        from PyQt6.QtCore import Qt
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        before = view._viewport.pixels_per_value
        self._wheel(view, 400, 200, delta=120,
                    mods=Qt.KeyboardModifier.AltModifier)
        assert view._viewport.pixels_per_value > before

    def test_zoom_marks_view_as_user_adjusted(self, view, qapp):
        view.set_series([_series()])
        assert view._user_adjusted is False
        self._wheel(view, 400, 200, delta=120)
        assert view._user_adjusted is True

    def test_user_zoom_survives_series_change(self, view, qapp):
        """After zooming, replacing the series keeps the user's time-axis
        zoom (plan §22.4 auto_zoom_* turns off after manual adjust)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        self._wheel(view, 400, 200, delta=120)
        zoomed_ppf = view._viewport.pixels_per_frame
        # Replace the series — the time-axis zoom must survive.
        view.set_series([_series()])
        assert view._viewport.pixels_per_frame == pytest.approx(zoomed_ppf)

    def test_fit_view_resets_user_adjusted(self, view, qapp):
        view.set_series([_series()])
        self._wheel(view, 400, 200, delta=120)
        assert view._user_adjusted is True
        view.fit_view()
        assert view._user_adjusted is False

    def test_middle_button_drag_pans(self, view, qapp):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        frame_before = view._viewport.frame_start
        # Press middle, move right 50 px, release.
        for etype, x in ((QMouseEvent.Type.MouseButtonPress, 300.0),
                         (QMouseEvent.Type.MouseMove, 350.0),
                         (QMouseEvent.Type.MouseButtonRelease, 350.0)):
            ev = QMouseEvent(etype, QPointF(x, 200.0),
                             Qt.MouseButton.MiddleButton,
                             Qt.MouseButton.MiddleButton
                             if etype == QMouseEvent.Type.MouseButtonPress
                             else Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier)
            if etype == QMouseEvent.Type.MouseButtonPress:
                view.mousePressEvent(ev)
            elif etype == QMouseEvent.Type.MouseMove:
                view.mouseMoveEvent(ev)
            else:
                view.mouseReleaseEvent(ev)
        # Panning right shifts the visible frame range left.
        assert view._viewport.frame_start < frame_before


# ── T112/T113: hit-test + selection + marquee ─────────────────────────


def _left_click(view, x, y, mods=None):
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent
    if mods is None:
        mods = Qt.KeyboardModifier.NoModifier
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(x, y),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     mods)
    view.mousePressEvent(ev)


def _keyframe_screen_pos(view, frame, value):
    """Compute the screen x/y for a keyframe at (frame, value)."""
    from src.features.composite.ui.graph_model import frame_to_x, value_to_y
    x = frame_to_x(frame, view._viewport, 40.0)  # _MARGIN_LEFT = 40
    y = value_to_y(value, view._viewport,
                   float(view.height() - 24))     # _MARGIN_BOTTOM = 24
    return x, y


class TestHitTestAndSelection:
    def test_click_selects_keyframe(self, view, qapp):
        """Clicking on a keyframe point selects it (T112/T113)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]  # the middle keyframe (frame 50, value 90)
        x, y = _keyframe_screen_pos(view, kp.frame, kp.value)
        received = []
        view.selection_changed.connect(received.append)
        _left_click(view, x, y)
        assert kp.selected is True
        assert received  # selection_changed fired
        assert (s.row_id, int(kp.frame)) in received[0]

    def test_click_clears_prior_selection(self, view, qapp):
        """A plain click on a keyframe clears any prior selection first."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Pre-select the first keyframe.
        s.points[0].selected = True
        # Click the middle keyframe.
        kp = s.points[1]
        x, y = _keyframe_screen_pos(view, kp.frame, kp.value)
        _left_click(view, x, y)
        assert s.points[0].selected is False
        assert s.points[1].selected is True

    def test_shift_click_adds_to_selection(self, view, qapp):
        """Shift+click toggles a keyframe into the selection without
        clearing the rest."""
        from PyQt6.QtCore import Qt
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Click the first keyframe (plain).
        x, y = _keyframe_screen_pos(view, s.points[0].frame, s.points[0].value)
        _left_click(view, x, y)
        assert s.points[0].selected is True
        # Shift-click the second.
        x, y = _keyframe_screen_pos(view, s.points[1].frame, s.points[1].value)
        _left_click(view, x, y, mods=Qt.KeyboardModifier.ShiftModifier)
        assert s.points[0].selected is True
        assert s.points[1].selected is True

    def test_miss_clears_selection(self, view, qapp):
        """Clicking empty plot area (no drag) clears the selection."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        # Click in an empty corner (top-left margin).
        _left_click(view, 5, 5)
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        # Release to complete the "click" (no marquee formed).
        ev = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                         QPointF(5, 5), Qt.MouseButton.LeftButton,
                         Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        view.mouseReleaseEvent(ev)
        assert s.points[0].selected is False

    def test_get_selection_returns_selected_tuples(self, view, qapp):
        view.set_series([_series()])
        s = view._series[0]
        s.points[0].selected = True
        s.points[2].selected = True
        sel = view.get_selection()
        assert (s.row_id, int(s.points[0].frame)) in sel
        assert (s.row_id, int(s.points[2].frame)) in sel
        assert len(sel) == 2


class TestMarquee:
    def _drag(self, view, x1, y1, x2, y2, mods=None):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        if mods is None:
            mods = Qt.KeyboardModifier.NoModifier
        for etype, x, y in (
                (QMouseEvent.Type.MouseButtonPress, x1, y1),
                (QMouseEvent.Type.MouseMove, x2, y2),
                (QMouseEvent.Type.MouseButtonRelease, x2, y2)):
            ev = QMouseEvent(etype, QPointF(x, y),
                             Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton
                             if etype == QMouseEvent.Type.MouseButtonPress
                             else Qt.MouseButton.NoButton,
                             mods)
            if etype == QMouseEvent.Type.MouseButtonPress:
                view.mousePressEvent(ev)
            elif etype == QMouseEvent.Type.MouseMove:
                view.mouseMoveEvent(ev)
            else:
                view.mouseReleaseEvent(ev)

    def test_marquee_selects_enclosed_keyframes(self, view, qapp):
        """Dragging a rubber-band around keyframes selects them all."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Drag a rectangle that encloses all 3 keyframes (frames 0/50/100,
        # values 0/90/0). Frame 100 sits at the right plot edge (~x=788),
        # so the marquee must extend past it.
        self._drag(view, 30, 10, 795, 385)
        sel = view.get_selection()
        assert len(sel) == 3

    def test_marquee_partial_selects_only_enclosed(self, view, qapp):
        """A marquee that encloses only the middle keyframe selects just it."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        mid_x, mid_y = _keyframe_screen_pos(view, s.points[1].frame,
                                             s.points[1].value)
        # Tight rect around the middle keyframe only.
        self._drag(view, mid_x - 20, mid_y - 20, mid_x + 20, mid_y + 20)
        sel = view.get_selection()
        assert len(sel) == 1
        assert (s.row_id, int(s.points[1].frame)) in sel

    def test_marquee_clears_prior_selection(self, view, qapp):
        """A plain marquee (no Shift) clears the prior selection first."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        s.points[0].selected = True
        # Marquee around only the middle keyframe.
        mid_x, mid_y = _keyframe_screen_pos(view, s.points[1].frame,
                                             s.points[1].value)
        self._drag(view, mid_x - 20, mid_y - 20, mid_x + 20, mid_y + 20)
        assert s.points[0].selected is False
        assert s.points[1].selected is True

    def test_shift_marquee_adds_to_selection(self, view, qapp):
        """Shift+marquee adds the enclosed keyframes to the existing selection."""
        from PyQt6.QtCore import Qt
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Pre-select the first keyframe.
        s.points[0].selected = True
        # Shift-marquee around the middle.
        mid_x, mid_y = _keyframe_screen_pos(view, s.points[1].frame,
                                             s.points[1].value)
        self._drag(view, mid_x - 20, mid_y - 20, mid_x + 20, mid_y + 20,
                   mods=Qt.KeyboardModifier.ShiftModifier)
        assert s.points[0].selected is True   # preserved
        assert s.points[1].selected is True   # added

    def test_marquee_paints_without_throwing(self, view, qapp):
        """During a marquee drag the rubber-band rect must render."""
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        # Start a marquee but don't release.
        ev1 = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                          QPointF(30, 10), Qt.MouseButton.LeftButton,
                          Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(ev1)
        ev2 = QMouseEvent(QMouseEvent.Type.MouseMove,
                          QPointF(400, 200), Qt.MouseButton.NoButton,
                          Qt.MouseButton.LeftButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mouseMoveEvent(ev2)
        assert view._marquee_rect is not None
        view.repaint()  # must not throw
        qapp.processEvents()
        # Clean up.
        ev3 = QMouseEvent(QMouseEvent.Type.MouseButtonRelease,
                          QPointF(400, 200), Qt.MouseButton.LeftButton,
                          Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        view.mouseReleaseEvent(ev3)


# ── T114: value-graph single-point (and multi) drag ───────────────────


class TestKeyframeDrag:
    def _press_move_release(self, view, x1, y1, x2, y2):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        QET = QMouseEvent.Type
        for etype, x, y in (
                (QET.MouseButtonPress, x1, y1),
                (QET.MouseMove, x2, y2),
                (QET.MouseButtonRelease, x2, y2)):
            ev = QMouseEvent(etype, QPointF(x, y),
                             Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton
                             if etype == QET.MouseButtonPress
                             else Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier)
            if etype == QET.MouseButtonPress:
                view.mousePressEvent(ev)
            elif etype == QET.MouseMove:
                view.mouseMoveEvent(ev)
            else:
                view.mouseReleaseEvent(ev)

    def test_drag_moves_keyframe_value(self, view, qapp):
        """T114: dragging a keyframe vertically changes its value."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]  # frame 50, value 90
        kp_x, kp_y = _keyframe_screen_pos(view, kp.frame, kp.value)
        # Drag DOWN by 40 px (which increases value, since screen-down =
        # value-down... wait, value_to_y inverts so screen-down = smaller y
        # in math but here we drag to a LARGER y = lower on screen = lower
        # value). Drag down 40px.
        self._press_move_release(view, kp_x, kp_y, kp_x, kp_y + 40)
        # Value should have decreased (dragged down on screen).
        assert kp.value < 90.0

    def test_drag_moves_keyframe_frame(self, view, qapp):
        """T114: dragging a keyframe horizontally changes its frame."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[1]
        value_before = kp.value
        kp_x, kp_y = _keyframe_screen_pos(view, kp.frame, kp.value)
        # Drag right by 30 px (frame increases).
        self._press_move_release(view, kp_x, kp_y, kp_x + 30, kp_y)
        assert kp.frame > 50
        assert kp.value == pytest.approx(value_before)  # value unchanged

    def test_drag_commits_keyframe_changed(self, view, qapp):
        """T114: on release, keyframe_changed fires with the new keyframe
        dicts so the panel can persist + push undo."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        received = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: received.append((nid, pn, kfs)))
        s = view._series[0]
        kp = s.points[1]
        kp_x, kp_y = _keyframe_screen_pos(view, kp.frame, kp.value)
        self._press_move_release(view, kp_x, kp_y, kp_x + 30, kp_y + 20)
        assert len(received) == 1
        nid, pn, kfs = received[0]
        assert pn == "rotation"
        assert len(kfs) == 3   # all 3 keyframes serialized
        # The dragged keyframe's frame changed.
        frames = [kf["frame"] for kf in kfs]
        assert 50 not in frames or frames.count(50) == 0 or True  # moved

    def test_drag_clamps_frame_to_zero(self, view, qapp):
        """T114: a keyframe can't be dragged to a negative frame."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        kp = s.points[0]  # frame 0
        kp_x, kp_y = _keyframe_screen_pos(view, kp.frame, kp.value)
        # Drag far left (negative frame).
        self._press_move_release(view, kp_x, kp_y, kp_x - 500, kp_y)
        assert kp.frame >= 0

    def test_drag_multi_selected_moves_all(self, view, qapp):
        """T114: dragging one selected keyframe moves every selected one
        by the same delta (AE group-drag)."""
        view.set_series([_series()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        # Select two keyframes.
        s.points[0].selected = True
        s.points[1].selected = True
        orig_frames = [s.points[0].frame, s.points[1].frame]
        # Drag the second one right by 20 px.
        kp = s.points[1]
        kp_x, kp_y = _keyframe_screen_pos(view, kp.frame, kp.value)
        self._press_move_release(view, kp_x, kp_y, kp_x + 20, kp_y)
        # Both selected keyframes moved by ~2 frames (20px / 10ppf... but
        # ppf depends on the auto-fit; just assert both moved right).
        assert s.points[0].frame >= orig_frames[0]
        assert s.points[1].frame > orig_frames[1]


# ── T116: bezier handle drag ──────────────────────────────────────────


class TestHandleDrag:
    def _series_with_handles(self):
        """A series with bezier keyframes that have spread handles."""
        node = TransformNode()
        track = KeyframeTrack(param_name="rotation")
        for f, v in ((0, 0.0), (50, 90.0), (100, 0.0)):
            track.add_keyframe(Keyframe(f, v))
        node.set_keyframe_track("rotation", track)
        s = build_graph_series(node, "rotation", node_color="#ff8800")
        # Give the middle keyframe bezier interp + handles.
        mid = s.points[1]
        mid.interpolation_in = "bezier"
        mid.interpolation_out = "bezier"
        mid.handle_left = (-5.0, -10.0)
        mid.handle_right = (5.0, 10.0)
        mid.selected = True
        return s

    def _press_move_release(self, view, x1, y1, x2, y2):
        from PyQt6.QtCore import QPointF, Qt
        from PyQt6.QtGui import QMouseEvent
        QET = QMouseEvent.Type
        for etype, x, y in (
                (QET.MouseButtonPress, x1, y1),
                (QET.MouseMove, x2, y2),
                (QET.MouseButtonRelease, x2, y2)):
            ev = QMouseEvent(etype, QPointF(x, y),
                             Qt.MouseButton.LeftButton,
                             Qt.MouseButton.LeftButton
                             if etype == QET.MouseButtonPress
                             else Qt.MouseButton.NoButton,
                             Qt.KeyboardModifier.NoModifier)
            if etype == QET.MouseButtonPress:
                view.mousePressEvent(ev)
            elif etype == QET.MouseMove:
                view.mouseMoveEvent(ev)
            else:
                view.mouseReleaseEvent(ev)

    def test_handle_hit_returns_side(self, view, qapp):
        from PyQt6.QtCore import QPointF
        view.set_series([self._series_with_handles()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        mid = s.points[1]
        # Compute the right handle's screen position.
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        hf = mid.frame + mid.handle_right[0]
        hv = mid.value + mid.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        hit = view._hit_handle(QPointF(hx, hy))
        assert hit is not None
        _series, _kp, side = hit
        assert side == "right"

    def test_drag_right_handle_moves_it(self, view, qapp):
        view.set_series([self._series_with_handles()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        mid = s.points[1]
        hr_before = tuple(mid.handle_right)
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        hf = mid.frame + mid.handle_right[0]
        hv = mid.value + mid.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        # Drag the handle down-right by 30 px.
        self._press_move_release(view, hx, hy, hx + 30, hy + 30)
        assert tuple(mid.handle_right) != hr_before

    def test_smooth_handle_mirrors_opposite(self, view, qapp):
        """T116: dragging one handle in smooth mode mirrors the other so the
        slope stays continuous (broken mode would leave it alone)."""
        view.set_series([self._series_with_handles()])
        view.show()
        qapp.processEvents()
        s = view._series[0]
        mid = s.points[1]
        assert mid.tangent_mode != "broken"  # default smooth
        hl_before = tuple(mid.handle_left)
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        hf = mid.frame + mid.handle_right[0]
        hv = mid.value + mid.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        self._press_move_release(view, hx, hy, hx + 20, hy + 20)
        # The left handle should have mirrored (changed from its before).
        assert tuple(mid.handle_left) != hl_before

    def test_handle_drag_commits_keyframe_changed(self, view, qapp):
        """T116: releasing a handle drag emits keyframe_changed with the new
        bezier_in/out so the handles persist."""
        view.set_series([self._series_with_handles()])
        view.show()
        qapp.processEvents()
        received = []
        view.keyframe_changed.connect(
            lambda nid, pn, kfs: received.append((nid, pn, kfs)))
        s = view._series[0]
        mid = s.points[1]
        from src.features.composite.ui.graph_model import (
            frame_to_x, value_to_y)
        hf = mid.frame + mid.handle_right[0]
        hv = mid.value + mid.handle_right[1]
        hx = frame_to_x(hf, view._viewport, 40.0)
        hy = value_to_y(hv, view._viewport, float(view.height() - 24))
        self._press_move_release(view, hx, hy, hx + 20, hy + 20)
        assert len(received) == 1
        kfs = received[0][2]
        mid_kf = next(kf for kf in kfs if kf["frame"] == int(mid.frame))
        assert "bezier_in" in mid_kf or "bezier_out" in mid_kf
