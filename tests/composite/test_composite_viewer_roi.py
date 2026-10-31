"""B1: user viewer ROI — interaction, persistence and conversion tests.

The ROI pipeline split:
- viewer: drag rect (image coords, top-left origin) → ``user_roi_changed``
  with a GL-convention BBox (y-up); Shift+R arms the tool, Ctrl+Shift+R
  clears; both state pairs persist ``user_roi``.
- page/request: ``viewer_roi`` rides RenderRequest (existing plumbing).
- evaluator/renderer: preview source cropping + full-frame recomposite
  (GL side — covered by manual smoke, not here).

No GL window required — CompositeViewer instantiates offscreen like the
other viewer tests.
"""

from __future__ import annotations

import pytest

from src.features.composite.core.format import BBox


def _make_viewer(qapp):
    from src.features.composite.ui.composite_viewport import CompositeViewer
    viewer = CompositeViewer()
    yield viewer
    viewer.close()


def test_roi_state_round_trip(qapp) -> None:
    viewer = next(_make_viewer(qapp))
    viewer.set_user_roi((100.0, 80.0, 320.0, 240.0))
    state = viewer.get_state()
    assert state["user_roi"] == [100.0, 80.0, 320.0, 240.0]

    viewer.set_user_roi(None)
    assert viewer.get_state()["user_roi"] is None

    # restore through set_state (both pairs share the key)
    viewer.set_state(state)
    assert viewer._user_roi_rect == (100.0, 80.0, 320.0, 240.0)


def test_persisted_state_pair_round_trip(qapp) -> None:
    viewer = next(_make_viewer(qapp))
    # the project-persistence pair (get_state/set_state near the bottom)
    viewer.set_user_roi((10.0, 20.0, 30.0, 40.0))
    state = viewer.get_state()
    assert state["user_roi"] == [10.0, 20.0, 30.0, 40.0]
    viewer._user_roi_rect = None
    viewer.set_state(state)
    assert viewer._user_roi_rect == (10.0, 20.0, 30.0, 40.0)


def test_roi_bbox_y_flip_and_clamp(qapp) -> None:
    viewer = next(_make_viewer(qapp))
    viewer._tex_width, viewer._tex_height = 1920, 1080

    # rect in the middle of the frame, top-left origin
    viewer.set_user_roi((100.0, 80.0, 320.0, 240.0))
    bbox = viewer.get_user_roi_bbox()
    assert isinstance(bbox, BBox)
    # y flip: bbox.y = frame_h - (y + h) = 1080 - 320 = 760
    assert (bbox.x, bbox.y, bbox.width, bbox.height) == (100, 760, 320, 240)

    # clamped against the frame edges (dragged past bottom-right)
    viewer.set_user_roi((1800.0, 1000.0, 500.0, 500.0))
    bbox = viewer.get_user_roi_bbox()
    assert (bbox.x, bbox.y) == (1800, 1080 - 1080)
    assert (bbox.width, bbox.height) == (120, 80)


def test_roi_drag_interaction_commits(qapp) -> None:
    from PyQt6.QtCore import QPointF
    viewer = next(_make_viewer(qapp))
    viewer._tex_width, viewer._tex_height = 1000, 1000

    from src.features.composite.ui.viewer_tool_system import ViewerToolMode
    viewer._tool_mode = ViewerToolMode.ROI
    viewer._interaction_state.tool = ViewerToolMode.ROI

    # view→image mapping depends on zoom/pan/widget size; use the same
    # conversion as the handlers so the assertion is resolution-independent
    p1 = viewer.get_image_pos_at_view_pos(QPointF(100.0, 100.0),
                                          check_bounds=False)
    p2 = viewer.get_image_pos_at_view_pos(QPointF(300.0, 250.0),
                                          check_bounds=False)
    assert viewer._handle_tool_press(QPointF(100.0, 100.0), 0) is True
    viewer._handle_tool_move(QPointF(300.0, 250.0), 0)
    assert viewer._user_roi_drag_rect is not None
    viewer._handle_tool_release(QPointF(300.0, 250.0))

    assert viewer._user_roi_drag_rect is None
    expected = (p1.x(), p1.y(), p2.x() - p1.x(), p2.y() - p1.y())
    got = viewer._user_roi_rect
    assert got is not None
    for g, e in zip(got, expected):
        assert abs(g - e) < 1e-6
    # committed rect round-trips through the BBox conversion (endpoint
    # rounding keeps width/height consistent with x/y within a pixel)
    bbox = viewer.get_user_roi_bbox()
    assert abs(bbox.width - expected[2]) <= 1.0
    assert abs(bbox.height - expected[3]) <= 1.0
    assert abs((viewer._tex_height - (expected[1] + expected[3])) - bbox.y) <= 1.0


def test_roi_click_clears(qapp) -> None:
    from PyQt6.QtCore import QPointF
    from src.features.composite.ui.viewer_tool_system import ViewerToolMode
    viewer = next(_make_viewer(qapp))
    viewer._tex_width, viewer._tex_height = 1000, 1000
    viewer.set_user_roi((10.0, 10.0, 50.0, 50.0))
    viewer._tool_mode = ViewerToolMode.ROI
    viewer._interaction_state.tool = ViewerToolMode.ROI

    viewer._handle_tool_press(QPointF(500.0, 500.0), 0)
    viewer._handle_tool_release(QPointF(500.0, 500.0))  # no drag → clear
    assert viewer._user_roi_rect is None


def test_clear_user_roi_emits_signal(qapp) -> None:
    viewer = next(_make_viewer(qapp))
    events = []
    viewer.user_roi_changed.connect(lambda bbox: events.append(bbox))
    viewer.set_user_roi((10.0, 10.0, 50.0, 50.0))
    # set_user_roi is the state-restore entry (no signal); commit/clear emit
    assert events == []
    viewer.clear_user_roi()
    assert len(events) == 1 and events[0] is None


def test_shortcut_shift_r_arms_roi_and_bare_r_stays_roto(qapp, monkeypatch) -> None:
    from PyQt6.QtCore import Qt
    from src.features.composite.ui.composite_viewport import CompositeViewer
    from src.features.composite.ui.viewer_tool_system import ViewerToolMode

    viewer = next(_make_viewer(qapp))
    viewer._view_mode = "2d"

    class FakeEvent:
        def __init__(self, key, mods):
            self._key = key
            self._mods = mods
            self.accepted = False

        def key(self):
            return self._key

        def text(self):
            return chr(self._key)

        def modifiers(self):
            return self._mods

        def accept(self):
            self.accepted = True

    shift = Qt.KeyboardModifier.ShiftModifier
    ctrl = Qt.KeyboardModifier.ControlModifier

    # Shift+R → ROI tool
    ev = FakeEvent(Qt.Key.Key_R.value, shift)
    viewer.keyPressEvent(ev)
    assert viewer.get_tool_mode() == ViewerToolMode.ROI

    # bare R → ROTO (unchanged behaviour)
    viewer.set_tool_mode(ViewerToolMode.SELECT)
    ev = FakeEvent(Qt.Key.Key_R.value, Qt.KeyboardModifier.NoModifier)
    viewer.keyPressEvent(ev)
    assert viewer.get_tool_mode() == ViewerToolMode.ROTO

    # Ctrl+Shift+R → clear ROI (also drops back to PAN when in ROI tool)
    viewer.set_tool_mode(ViewerToolMode.ROI)
    viewer.set_user_roi((10.0, 10.0, 50.0, 50.0))
    ev = FakeEvent(Qt.Key.Key_R.value, ctrl | shift)
    viewer.keyPressEvent(ev)
    assert viewer._user_roi_rect is None
    assert viewer.get_tool_mode() == ViewerToolMode.PAN


def test_render_request_carries_viewer_roi(qapp) -> None:
    """RenderRequest.viewer_roi plumbing (page side uses the same field)."""
    from src.features.composite.render.render_scheduler import RenderRequest
    from src.features.composite.core.graph_snapshot import GraphSnapshot
    import time

    snap = GraphSnapshot(generation=1)
    req = RenderRequest(
        snapshot=snap, request_id=1, timestamp=time.monotonic(),
        render_mode="preview", viewer_roi=BBox(10, 20, 30, 40),
    )
    assert req.viewer_roi == BBox(10, 20, 30, 40)


def test_roi_disabled_for_export_mode() -> None:
    """Plan-level guard: ROI propagation is preview-only (existing contract,
    re-checked here so the B1 request path cannot silently enable it)."""
    from src.features.composite.render.render_scheduler import RenderScheduler
    from src.features.composite.render.render_scheduler import RenderMode
    from src.features.composite.core.graph_snapshot import GraphSnapshot, NodeSnapshot

    snap = GraphSnapshot(
        generation=1,
        nodes={"n1": NodeSnapshot(
            node_id="n1", node_type="blur", parameters={}, disabled=False,
            input_ports=[("input", "image")], output_ports=[("result", "image")])},
        eval_order=["n1"],
    )
    plan = RenderScheduler().plan(
        snap, RenderMode.EXPORT, viewer_roi=BBox(0, 0, 16, 16))
    assert plan.roi_enabled is False
