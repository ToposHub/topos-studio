"""Batch D (wd8-g41) regression tests for Major audit findings.

Covers:
  * AUD-9504 — CompositePage.__init__ initializes ``_properties_panel`` so
               slots reading it before host binding see None, not
               AttributeError
  * AUD-9505 — cleanup() stops every single-shot/debounce timer, drops
               pending payloads and clears ``_is_active`` so an armed
               ``_renderer_retry_timer`` cannot resurrect a renderer thread
  * AUD-9506 — load_composite_data() validates fps/duration_frames from
               the persisted payload instead of letting ValueError escape
  * AUD-9600 — CompositeRenderDialog keeps start<=end (cross-clamp,
               constructor normalization, get_settings contract and a
               render-click backstop)
  * AUD-9700 — PAINT press engages the interaction state (move reachable)
               and the release path commits paint_stroke_committed
  * AUD-9701 — ROI press engages the interaction state; drag updates the
               rubber band and release commits the rectangle
  * AUD-9702 — _compute_uv_at_mouse reuses _compute_vertices (content-rect
               aware) instead of a full-viewport letterbox
  * AUD-9703 — context rebuild resets every owned GL handle, not just the
               ODT LUT
  * AUD-9704 — set_state validates numeric fields from the project JSON
               and falls back to defaults instead of raising
  * AUD-9705 — A/B snapshot probes glIsTexture only after makeCurrent
  * AUD-9800 — _would_create_cycle walks the scene's ConnectionItems
               (PortItem has no _connections list)
  * AUD-9905 — curve samplers clip while sampling (window-bounded cost)
               with bit-identical output to the old generate-then-filter
  * AUD-9906 — evaluator unavailability flags has_error (no silent
               fallback); evaluate() exceptions outside the documented
               set propagate
  * AUD-9907 — build_graph_series skips malformed keyframes instead of
               raising into the UI build path
  * AUD-9918 — UpdateGroupCommand snapshots are deep and never polluted by
               redo
  * AUD-9919 — Ungroup/AddToGroup no longer share the node_ids list;
               interleaved undo/redo keeps group members
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.composite_core

from PyQt6.QtCore import QEvent, QPointF, QPoint, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QGraphicsRectItem, QGraphicsScene

from src.features.composite.ui.graph_model import (
    GraphPoint,
    build_graph_series,
    sample_expression_curves,
    sample_speed_curve,
    sample_value_curve,
)
from src.features.composite.ui.group_commands import (
    AddToGroupCommand,
    CreateGroupCommand,
    UngroupCommand,
    UpdateGroupCommand,
)


def _pts(frame_values, interp="linear"):
    return [GraphPoint(row_id="r", node_id="n", parameter_name="p",
                       channel=None, frame=float(f), value=float(v),
                       interpolation_in=interp, interpolation_out=interp)
            for f, v in frame_values]


# ── AUD-9504 / AUD-9505 / AUD-9506 (composite_page) ────────────────────


class TestCompositePageFixes:
    def _make_page(self, qapp):
        from src.features.composite.ui.composite_page import CompositePage
        return CompositePage(parent=None)

    def test_aud9504_properties_panel_initialized_in_init(self, qapp):
        page = self._make_page(qapp)
        try:
            # Direct attribute read (the on_enter pattern) must not raise
            # before the host ever binds a panel.
            assert page._properties_panel is None
        finally:
            page.cleanup()
            page.deleteLater()

    def test_aud9505_cleanup_stops_timers_and_clears_active(self, qapp):
        page = self._make_page(qapp)
        try:
            timers = [
                page._renderer_retry_timer,
                page._eval_debounce_timer,
                page._prefetch_timer,
                page._param_debounce_timer,
                page._batch_param_debounce_timer,
                page._minimap_timer,
                page._interaction_debounce_timer,
                page._nav3d_restore_timer,
            ]
            for timer in timers:
                timer.start()
            page._pending_param_change = ("n", "p", 0, 1)
            page._pending_batch_param_change = (["n"], "p", [0], 1)
            page._pending_interaction_update = {"node_id": "n"}
            page._is_active = True

            page.cleanup()

            for timer in timers:
                assert not timer.isActive(), timer.objectName() or timer
            assert page._is_active is False
            assert page._pending_param_change is None
            assert page._pending_batch_param_change is None
            assert page._pending_interaction_update is None
        finally:
            page.cleanup()
            page.deleteLater()

    def test_aud9505_retry_path_inert_after_cleanup(self, qapp):
        page = self._make_page(qapp)
        try:
            page.cleanup()
            page._renderer = None
            calls = []
            page.initialize_renderer = lambda: calls.append(1)  # type: ignore[assignment]
            # Must return before re-creating a renderer: page is inactive.
            page._retry_renderer_init()
            assert calls == []
        finally:
            page.cleanup()
            page.deleteLater()

    def test_aud9506_load_composite_data_invalid_fps_duration(self, qapp):
        page = self._make_page(qapp)
        try:
            payload = {
                "composite_id": "bad-meta",
                "nodes": [{"id": "bg", "type": "constant",
                           "parameters": {}, "position": [0, 0]}],
                "connections": [],
                "fps": "24fps",
                "duration_frames": "many",
            }
            page.load_composite_data(payload)  # must not raise
            assert page._composite_fps == 24.0
            assert page._composite_duration_frames == 0

            payload["fps"] = 30.0
            payload["duration_frames"] = 48
            page.load_composite_data(payload)
            assert page._composite_fps == 30.0
            assert page._composite_duration_frames == 48
        finally:
            page.cleanup()
            page.deleteLater()


# ── AUD-9600 (composite_render_dialog) ─────────────────────────────────


class TestRenderDialogFrameRange:
    def _make_dialog(self, qapp, frame_range=(0, 0)):
        from src.features.composite.render.composite_render_output import (
            CompositeMode)
        from src.features.composite.ui.composite_render_dialog import (
            CompositeRenderDialog)
        dlg = CompositeRenderDialog(
            parent=None, mode=CompositeMode.VIDEO,
            frame_range=frame_range, fps=24.0)
        yield dlg
        dlg.close()
        dlg.deleteLater()

    def test_aud9600_inverted_ctor_range_normalized(self, qapp):
        dlg = next(self._make_dialog(qapp, frame_range=(100, 0)))
        assert dlg.start_frame_spin.value() == 0
        assert dlg.end_frame_spin.value() == 100

    def test_aud9600_cross_clamp_pushes_end_up(self, qapp):
        dlg = next(self._make_dialog(qapp))
        dlg.start_frame_spin.setValue(100)
        assert dlg.end_frame_spin.value() == 100
        assert dlg.start_frame_spin.value() <= dlg.end_frame_spin.value()

    def test_aud9600_cross_clamp_pulls_start_down(self, qapp):
        dlg = next(self._make_dialog(qapp, frame_range=(50, 200)))
        dlg.end_frame_spin.setValue(20)
        assert dlg.start_frame_spin.value() == 20

    def test_aud9600_cross_clamp_has_no_feedback_loop(self, qapp):
        dlg = next(self._make_dialog(qapp, frame_range=(0, 10)))
        # A pathological programmatic storm of updates must terminate and
        # leave a valid range (state lock prevents mutual recursion).
        for start, end in ((30, 5), (0, 0), (7, 3), (99, 1)):
            dlg.start_frame_spin.setValue(start)
            dlg.end_frame_spin.setValue(end)
        assert dlg.start_frame_spin.value() <= dlg.end_frame_spin.value()

    def test_aud9600_get_settings_enforces_contract(self, qapp):
        dlg = next(self._make_dialog(qapp, frame_range=(0, 100)))
        # Bypass the UI clamp (blocked signals) — get_settings must still
        # never hand an inverted range to the render pipeline.
        dlg.start_frame_spin.blockSignals(True)
        dlg.start_frame_spin.setValue(100)
        dlg.start_frame_spin.blockSignals(False)
        dlg.end_frame_spin.blockSignals(True)
        dlg.end_frame_spin.setValue(40)
        dlg.end_frame_spin.blockSignals(False)
        assert dlg.start_frame_spin.value() > dlg.end_frame_spin.value()
        settings = dlg.get_settings()
        assert settings["start_frame"] == 100
        assert settings["end_frame"] >= settings["start_frame"]

    def test_aud9600_render_click_blocked_on_inverted_range(self, qapp,
                                                            monkeypatch):
        from PyQt6.QtWidgets import QDialog, QMessageBox
        import src.features.composite.ui.composite_render_dialog as dlg_mod
        dlg = next(self._make_dialog(qapp, frame_range=(0, 100)))
        dlg.start_frame_spin.blockSignals(True)
        dlg.start_frame_spin.setValue(100)
        dlg.start_frame_spin.blockSignals(False)
        dlg.end_frame_spin.blockSignals(True)
        dlg.end_frame_spin.setValue(40)
        dlg.end_frame_spin.blockSignals(False)
        assert dlg.start_frame_spin.value() > dlg.end_frame_spin.value()
        warnings = []
        monkeypatch.setattr(
            QMessageBox, "warning",
            staticmethod(lambda *a, **k: warnings.append(a)))
        dlg._on_render_clicked()
        assert warnings, "invalid range must surface a message"
        assert dlg.result() != QDialog.DialogCode.Accepted

    def test_aud9600_render_click_accepts_valid_range(self, qapp):
        from PyQt6.QtWidgets import QDialog
        dlg = next(self._make_dialog(qapp, frame_range=(0, 100)))
        dlg._on_render_clicked()
        assert dlg.result() == QDialog.DialogCode.Accepted


# ── AUD-9700..AUD-9705 (composite_viewport) ─────────────────────────────


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    viewer._tex_width = 100
    viewer._tex_height = 100
    yield viewer
    viewer.close()


def _release_event(x: float, y: float) -> QMouseEvent:
    return QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        QPointF(x, y), QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


class TestPaintToolDispatch:
    def test_aud9700_press_engages_drag_and_release_commits(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer._selected_node_type = "paint"
        viewer.get_selected_node = lambda: "paint-1"  # type: ignore[assignment]
        viewer.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(  # type: ignore[assignment]
            float(vp.x()), float(vp.y()))

        assert viewer._handle_paint_press(
            QPointF(10.0, 10.0), Qt.KeyboardModifier.NoModifier) is True
        state = viewer._interaction_state
        assert state.is_dragging is True
        from src.features.composite.ui.viewer_tool_system import ViewerToolMode
        assert state.tool == ViewerToolMode.PAINT
        assert viewer._paint_drag_points and len(viewer._paint_drag_points) == 1

        # Move far enough to clear the 2-px decimation threshold.
        viewer._handle_paint_move(QPointF(40.0, 40.0))
        assert len(viewer._paint_drag_points) == 2

        committed = []
        viewer.paint_stroke_committed.connect(
            lambda node_id, geom: committed.append((node_id, geom)))
        viewer.mouseReleaseEvent(_release_event(40.0, 40.0))
        assert len(committed) == 1, "stroke must be committed on release"
        node_id, geometry = committed[0]
        assert node_id == "paint-1"
        assert len(geometry["points"]) == 2
        assert viewer._interaction_state.is_dragging is False

    def test_aud9700_alt_click_sets_clone_source_without_drag(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer._selected_node_type = "paint"
        viewer.get_selected_node = lambda: "paint-1"  # type: ignore[assignment]
        viewer.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(  # type: ignore[assignment]
            float(vp.x()), float(vp.y()))

        assert viewer._handle_paint_press(
            QPointF(10.0, 10.0),
            Qt.KeyboardModifier.AltModifier) is True
        assert viewer._paint_clone_src is not None
        assert viewer._interaction_state.is_dragging is False

    def test_aud9700_escape_cancels_in_flight_stroke(self, qapp):
        from PyQt6.QtGui import QKeyEvent
        viewer = next(_make_viewer(qapp))
        viewer._selected_node_type = "paint"
        viewer.get_selected_node = lambda: "paint-1"  # type: ignore[assignment]
        viewer.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(  # type: ignore[assignment]
            float(vp.x()), float(vp.y()))
        viewer._handle_paint_press(QPointF(10.0, 10.0),
                                   Qt.KeyboardModifier.NoModifier)
        assert viewer._paint_drag_points is not None
        ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                       Qt.KeyboardModifier.NoModifier)
        viewer.keyPressEvent(ev)
        assert viewer._paint_drag_points is None
        assert viewer._interaction_state.is_dragging is False


class TestRoiToolDispatch:
    def test_aud9701_drag_rectangle_updates_and_commits(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(  # type: ignore[assignment]
            float(vp.x()), float(vp.y()))

        assert viewer._handle_roi_press(QPointF(10.0, 10.0)) is True
        assert viewer._interaction_state.is_dragging is True
        assert viewer._user_roi_drag_rect == (10.0, 10.0, 0.0, 0.0)

        viewer._handle_roi_move(QPointF(60.0, 50.0))
        assert viewer._user_roi_drag_rect == (10.0, 10.0, 50.0, 40.0)

        roi_events = []
        viewer.user_roi_changed.connect(lambda bbox: roi_events.append(bbox))
        viewer.mouseReleaseEvent(_release_event(60.0, 50.0))
        assert viewer._user_roi_rect == (10.0, 10.0, 50.0, 40.0)
        assert roi_events, "committed ROI must notify listeners"
        assert viewer._interaction_state.is_dragging is False

    def test_aud9701_click_without_drag_clears_roi(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.get_image_pos_at_view_pos = lambda vp, check_bounds=True: QPointF(  # type: ignore[assignment]
            float(vp.x()), float(vp.y()))
        viewer._user_roi_rect = (1.0, 1.0, 4.0, 4.0)
        viewer._handle_roi_press(QPointF(10.0, 10.0))
        viewer.mouseReleaseEvent(_release_event(11.0, 11.0))
        assert viewer._user_roi_rect is None  # quick-reset gesture


class TestInspectorUvMapping:
    def test_aud9702_uv_follows_compute_vertices(self, qapp, monkeypatch):
        viewer = next(_make_viewer(qapp))
        viewer._mouse_in_viewer = True
        viewer._mouse_pos = QPoint(100, 20)
        monkeypatch.setattr(viewer, "devicePixelRatio", lambda: 1.0)
        # Reserve a 20-px top bar: content rect (0, 20, 200, 80).
        monkeypatch.setattr(
            viewer, "_content_rect_pixels",
            lambda dpr: (0.0, 20.0, 200.0, 80.0))

        uv = viewer._compute_uv_at_mouse(200, 100, 100, 100)
        # Mouse at the content-rect top edge → top of the image (v == 1).
        # The old full-viewport letterbox would place it at v == 0.8.
        assert uv is not None
        assert uv[0] == pytest.approx(0.5, abs=1e-6)
        assert uv[1] == pytest.approx(1.0, abs=1e-6)

    def test_aud9702_uv_center_matches_content_center(self, qapp, monkeypatch):
        viewer = next(_make_viewer(qapp))
        viewer._mouse_in_viewer = True
        viewer._mouse_pos = QPoint(100, 50)
        monkeypatch.setattr(viewer, "devicePixelRatio", lambda: 1.0)
        monkeypatch.setattr(
            viewer, "_content_rect_pixels",
            lambda dpr: (0.0, 20.0, 200.0, 80.0))
        uv = viewer._compute_uv_at_mouse(200, 100, 100, 100)
        assert uv is not None
        assert uv[0] == pytest.approx(0.5, abs=1e-6)
        assert uv[1] == pytest.approx(0.5, abs=1e-6)


class TestContextRebuildHandleReset:
    def test_aud9703_all_owned_handles_reset(self, qapp):
        viewer = next(_make_viewer(qapp))
        # Simulate stale handles from a dead context.
        viewer._odt_lut_tex_id = 55
        viewer._odt_lut_key = ("stale",)
        viewer._odt_lut_failed_key = ("stale",)
        viewer._current_tex_id = 77
        viewer._owned_display_tex_id = 78
        viewer._owned_display_tex_size = (640, 480)
        viewer._owned_aov_tex_ids = {"depth": 79, "normal": 80}
        viewer._owned_aov_tex_sizes = {"depth": (640, 480)}
        viewer._owned_secondary_tex_id = 81
        viewer._owned_secondary_tex_size = (640, 480)
        viewer._owned_secondary_b_tex_id = 82
        viewer._owned_secondary_b_tex_size = (640, 480)
        viewer._owned_quad_tex_ids = {"top": 83, "right": 84, "front": 85}
        viewer._owned_quad_tex_size = (640, 480)
        viewer._copy_read_fbo = 86
        viewer._copy_draw_fbo = 87

        viewer._reset_owned_gl_handles_for_rebuild()

        assert viewer._odt_lut_tex_id == 0
        assert viewer._odt_lut_key is None
        assert viewer._odt_lut_failed_key is None
        assert viewer._current_tex_id is None
        assert viewer._owned_display_tex_id is None
        assert viewer._owned_display_tex_size is None
        assert viewer._owned_aov_tex_ids == {}
        assert viewer._owned_aov_tex_sizes == {}
        assert viewer._owned_secondary_tex_id == 0
        assert viewer._owned_secondary_tex_size is None
        assert viewer._owned_secondary_b_tex_id == 0
        assert viewer._owned_secondary_b_tex_size is None
        assert set(viewer._owned_quad_tex_ids.values()) == {0}
        assert viewer._owned_quad_tex_size is None
        assert viewer._copy_read_fbo is None
        assert viewer._copy_draw_fbo is None


class TestSetStateValidation:
    def test_aud9704_corrupt_state_falls_back_without_raising(self, qapp):
        viewer = next(_make_viewer(qapp))
        corrupt = {
            "zoom_level": None,
            "pan_x": "left",
            "pan_y": [],
            "channel_mode": "three",
            "exposure": "bright",
            "viewer_gamma": "gamma",
            "display_transfer": None,
            "display_primaries": "wide",
            "tonemap": {},
            "target_peak": "bright",
            "split_difference_scale": "scale",
            "magnifier_zoom": None,
        }
        viewer.set_state(corrupt)  # must not raise
        assert viewer._zoom_level == 1.0
        assert viewer._pan_x == 0.0
        assert viewer._pan_y == 0.0
        assert isinstance(viewer._channel_mode, int)
        assert viewer._exposure == 0.0
        assert viewer._viewer_gamma == 1.0
        assert isinstance(viewer._display_transfer, int)
        assert isinstance(viewer._display_primaries, int)
        assert isinstance(viewer._tonemap, int)
        assert viewer._target_peak == 100.0
        assert 0.0 <= viewer._split_difference_scale <= 20.0
        assert viewer._magnifier_zoom in (4.0, 8.0)

    def test_aud9704_valid_state_applies(self, qapp):
        viewer = next(_make_viewer(qapp))
        viewer.set_state({
            "zoom_level": 2.5,
            "pan_x": 0.25,
            "pan_y": -0.5,
            "exposure": 1.5,
            "viewer_gamma": 2.0,
            "target_peak": 400.0,
        })
        assert viewer._zoom_level == 2.5
        assert viewer._pan_x == 0.25
        assert viewer._pan_y == -0.5
        assert viewer._exposure == 1.5
        assert viewer._viewer_gamma == 2.0
        assert viewer._target_peak == 400.0


class TestAbSnapshotContextOrder:
    def test_aud9705_glistexture_only_after_make_current(self, qapp,
                                                          monkeypatch):
        import src.features.composite.ui.composite_viewport as vp_mod
        viewer = next(_make_viewer(qapp))
        viewer._gl_initialized = True
        viewer._current_tex_id = 7
        viewer._owned_display_tex_size = (4, 4)
        viewer._selected_render_pass = "beauty"

        order = []
        monkeypatch.setattr(
            viewer, "makeCurrent", lambda: order.append("makeCurrent"))
        monkeypatch.setattr(
            viewer, "doneCurrent", lambda: order.append("doneCurrent"))
        monkeypatch.setattr(
            vp_mod._GLWrapper, "glIsTexture",
            staticmethod(lambda tid: order.append("glIsTexture") or False),
            raising=True)
        monkeypatch.setattr(
            vp_mod._GLWrapper, "glBindTexture",
            staticmethod(lambda *a, **k: order.append("glBindTexture")),
            raising=True)

        ok = viewer._snapshot_display_texture_to_slot("A")
        assert ok is False  # probe rejects the (fake) texture name
        assert order[0] == "makeCurrent"
        probe_index = order.index("glIsTexture")
        assert probe_index > order.index("makeCurrent"), (
            "glIsTexture must run inside the current-context window "
            "(AUD-9705)")


# ── AUD-9800 (connection_item cycle guard) ─────────────────────────────


class _GraphStub:
    def __init__(self):
        self._node_groups = {}


class TestCycleDetection:
    def _build_chain(self):
        """Scene with nodes A→B→C connected; plus a dragged connection."""
        from src.features.composite.ui.connection_item import ConnectionItem
        from src.features.composite.ui.port_item import PortItem

        scene = QGraphicsScene()
        nodes = {}
        ports = {}
        for name in ("A", "B", "C", "D"):
            node = QGraphicsRectItem(0, 0, 10, 10)
            scene.addItem(node)
            nodes[name] = node
            ports[(name, "out")] = PortItem(
                "out", "output", "image", parent=node)
            ports[(name, "in")] = PortItem(
                "in", "input", "image", parent=node)

        def connect(src, dst):
            conn = ConnectionItem(
                ports[(src, "out")], ports[(dst, "in")], "image")
            scene.addItem(conn)
            return conn

        connect("A", "B")
        connect("B", "C")
        return scene, nodes, ports, connect

    def test_aud9800_cycle_detected_when_closing_loop(self, qapp):
        scene, nodes, ports, connect = self._build_chain()
        dragged = connect("C", "A")  # would close A→B→C→A
        dragged._is_reconnecting = True
        dragged._reconnect_src = False  # dragging the destination end
        assert dragged._would_create_cycle(ports[("A", "in")]) is True

    def test_aud9800_no_cycle_for_acyclic_target(self, qapp):
        scene, nodes, ports, connect = self._build_chain()
        dragged = connect("A", "D")
        dragged._is_reconnecting = True
        dragged._reconnect_src = False
        assert dragged._would_create_cycle(ports[("D", "in")]) is False

    def test_aud9800_can_reconnect_rejects_cyclic_target(self, qapp):
        scene, nodes, ports, connect = self._build_chain()
        dragged = connect("C", "A")
        dragged._is_reconnecting = True
        dragged._reconnect_src = False
        # Same-node, type and direction checks all pass; the cycle guard
        # must be the reason the reconnect is refused.
        assert dragged._can_reconnect(ports[("A", "in")]) is False

    def test_aud9800_direction_follows_disconnect_mode(self, qapp):
        scene, nodes, ports, connect = self._build_chain()
        dragged = connect("D", "A")
        # Alt+disconnect dragging the SOURCE end onto B.out: the potential
        # new edge is B.out → A.in. DFS starts at the destination (A) and
        # reaches B through the existing A→B edge → cycle.
        dragged._is_reconnecting = False
        dragged._disconnect_src = True
        assert dragged._would_create_cycle(ports[("B", "out")]) is True


# ── AUD-9905 (window-bounded sampling) ──────────────────────────────────


class TestWindowBoundedSampling:
    def test_aud9905_value_samples_bounded_and_phase_preserved(self):
        pts = _pts([(0.0, 0.0), (100000.0, 1.0)])
        samples = sample_value_curve(pts, 100.0, 200.0, pixels_per_frame=10.0)
        assert samples, "window overlaps the segment"
        for f, _v in samples:
            assert 100.0 <= f <= 200.0
        # Phase preserved: the first in-window sample sits on the old
        # k0.frame + n*step grid (repeated addition, not re-anchored).
        step = max(0.25, 3.0 / 10.0)
        expected_first = 0.0
        while expected_first < 100.0:
            expected_first += step
        assert samples[0][0] == pytest.approx(expected_first, rel=1e-9)
        # Window-bounded work: far below the full-span sample count.
        assert len(samples) <= int((200.0 - 100.0) / step) + 4

    def test_aud9905_value_no_clip_path_unchanged(self):
        pts = _pts([(0.0, 0.0), (100.0, 100.0)])
        # frame_end <= frame_start keeps the historical full-span output.
        samples = sample_value_curve(pts, 0.0, 0.0, pixels_per_frame=10.0)
        assert samples[0] == (0.0, 0.0)
        assert samples[-1] == (100.0, 100.0)

    def test_aud9905_hold_jump_kept_inside_window(self):
        pts = _pts([(0.0, 10.0), (50.0, 90.0)], interp="hold")
        samples = sample_value_curve(pts, 40.0, 50.0, pixels_per_frame=10.0)
        assert (50.0, 10.0) in samples
        assert (50.0, 90.0) in samples
        for f, _v in samples:
            assert 40.0 <= f <= 50.0

    def test_aud9905_speed_samples_bounded(self):
        pts = _pts([(0.0, 0.0), (100000.0, 1.0)])
        samples = sample_speed_curve(pts, 100.0, 200.0, pixels_per_frame=10.0,
                                     fps=24.0)
        assert samples
        for f, _v in samples:
            assert 100.0 <= f <= 200.0
        step = max(0.25, 3.0 / 10.0)
        assert len(samples) <= int((200.0 - 100.0) / step) + 4


# ── AUD-9906 (expression evaluator fallback) ────────────────────────────


class TestExpressionEvaluatorFallback:
    def _ctx(self):
        return SimpleNamespace(frame=0, time=0.0, current_value=0.0)

    def test_aud9906_unavailable_evaluator_flags_error(self, monkeypatch):
        # ``from ... import ExpressionParser`` raises ImportError when the
        # module entry is None.
        monkeypatch.setitem(
            sys.modules, "src.features.composite.utils.expression_parser",
            None)
        pre, post, err = sample_expression_curves(
            [], "frame + 1", 0.0, 10.0, static_value=5.0,
            context=self._ctx(), parser=None)
        assert err is True, "unavailable evaluator must not look healthy"
        assert post == pre

    def test_aud9906_domain_exception_is_per_frame_error(self):
        class RaisingParser:
            def evaluate(self, expression, context):
                raise ValueError("domain failure")

        pre, post, err = sample_expression_curves(
            [], "frame + 1", 0.0, 10.0, static_value=5.0,
            context=self._ctx(), parser=RaisingParser())
        assert err is True
        assert post == pre

    def test_aud9906_unexpected_exception_propagates(self):
        class BugParser:
            def evaluate(self, expression, context):
                raise RuntimeError("implementation bug")

        with pytest.raises(RuntimeError):
            sample_expression_curves(
                [], "frame + 1", 0.0, 10.0, static_value=5.0,
                context=self._ctx(), parser=BugParser())


# ── AUD-9907 (malformed keyframes skipped) ──────────────────────────────


class TestBuildGraphSeriesValidation:
    def test_aud9907_malformed_keyframes_skipped(self):
        node = SimpleNamespace(
            id="n1",
            keyframe_tracks={
                "opacity": {"keyframes": [
                    {"frame": None, "value": 1.0},       # frame None
                    {"frame": "ten", "value": 0.0},      # frame str
                    {"frame": 5.0, "value": "high"},     # value str
                    {"frame": 7.0, "value": 0.5},        # valid
                    "not-a-dict",                        # not a dict
                ]},
            },
            _expressions={},
            parameters={"opacity": 0.4},
        )
        series = build_graph_series(node, "opacity")
        assert [p.frame for p in series.points] == [7.0]

    def test_aud9907_valid_tracks_unaffected(self):
        node = SimpleNamespace(
            id="n2",
            keyframe_tracks={
                "blur": {"keyframes": [
                    {"frame": 0, "value": 1.0},
                    {"frame": 24, "value": 2.0},
                ]},
            },
            _expressions={},
            parameters={},
        )
        series = build_graph_series(node, "blur")
        assert [p.frame for p in series.points] == [0.0, 24.0]


# ── AUD-9918 / AUD-9919 (group command snapshots) ───────────────────────


class TestGroupCommandSnapshots:
    def _graph_with_group(self):
        graph = _GraphStub()
        graph._node_groups = {
            "g": {"id": "g", "node_ids": ["a", "b"], "title": "old"},
        }
        return graph

    def test_aud9918_update_undo_redo_undo_roundtrip(self):
        graph = self._graph_with_group()
        cmd = UpdateGroupCommand(graph, "g", {"title": "new"})
        cmd.redo()
        assert graph._node_groups["g"]["title"] == "new"
        cmd.undo()
        assert graph._node_groups["g"]["title"] == "old"
        cmd.redo()
        assert graph._node_groups["g"]["title"] == "new"
        # Regression: the second undo used to restore "new" (polluted
        # snapshot).
        cmd.undo()
        assert graph._node_groups["g"]["title"] == "old"
        assert graph._node_groups["g"]["node_ids"] == ["a", "b"]

    def test_aud9918_snapshot_is_deep(self):
        graph = self._graph_with_group()
        cmd = UpdateGroupCommand(graph, "g", {"title": "new"})
        cmd.redo()
        assert cmd._old_data is not None
        assert cmd._old_data is not graph._node_groups["g"]
        assert (cmd._old_data["node_ids"]
                is not graph._node_groups["g"]["node_ids"])

    def test_aud9919_add_ungroup_interleaved_keeps_members(self):
        graph = self._graph_with_group()
        add_cmd = AddToGroupCommand(graph, "g", ["c"])
        add_cmd.redo()
        assert graph._node_groups["g"]["node_ids"] == ["a", "b", "c"]

        ungroup_cmd = UngroupCommand(graph, "g")
        assert ungroup_cmd.redo() is not None
        assert "g" not in graph._node_groups

        ungroup_cmd.undo()
        assert graph._node_groups["g"]["node_ids"] == ["a", "b", "c"]
        # undo(Add) removes only "c" — and must not write through into the
        # armed Ungroup snapshot.
        add_cmd.undo()
        assert graph._node_groups["g"]["node_ids"] == ["a", "b"]

        # Further ungroup/undo rounds keep every member (regression: "c"
        # used to vanish after the interleaved sequence).
        ungroup_cmd.redo()
        assert "g" not in graph._node_groups
        ungroup_cmd.undo()
        assert graph._node_groups["g"]["node_ids"] == ["a", "b"]

    def test_aud9919_ungroup_snapshot_not_aliased_to_live(self):
        graph = self._graph_with_group()
        ungroup_cmd = UngroupCommand(graph, "g")
        ungroup_cmd.redo()
        ungroup_cmd.undo()
        live = graph._node_groups["g"]
        assert live is not ungroup_cmd._group_data
        assert live["node_ids"] is not ungroup_cmd._group_data["node_ids"]

    def test_aud9919_add_does_not_mutate_shared_list(self):
        graph = self._graph_with_group()
        original = graph._node_groups["g"]["node_ids"]
        add_cmd = AddToGroupCommand(graph, "g", ["z"])
        add_cmd.redo()
        # The pre-command list object must be left untouched (snapshots
        # held by other commands stay valid).
        assert original == ["a", "b"]
        assert graph._node_groups["g"]["node_ids"] == ["a", "b", "z"]

    def test_aud9919_create_group_snapshot_list_fresh(self):
        graph = _GraphStub()
        cmd = CreateGroupCommand(graph, ["x"], "g1", {"title": "t"})
        cmd.redo()
        assert graph._node_groups["g1"]["node_ids"] == ["x"]
        assert graph._node_groups["g1"]["node_ids"] is not cmd.node_ids
