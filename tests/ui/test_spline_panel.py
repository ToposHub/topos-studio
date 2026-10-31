"""Phase 3 tests: SplinePanel instantiation, binding, and registration."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.ui.spline_panel import SplinePanel
from src.features.composite.ui.spline_model import SplineModel
from src.shared.panels.registry import PanelRegistry
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.fixture
def graph():
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation")
    tr.add_keyframe(Keyframe(0, 0.0))
    tr.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", tr)
    g.add_node(t)
    g.add_node(OutputNode())
    return g


def test_panel_instantiates(qapp):
    panel = SplinePanel()
    assert panel.objectName() == "CompositeSplinePanel"
    assert panel.minimumHeight() >= 220


def test_panel_binds_graph(qapp, graph):
    panel = SplinePanel()
    panel.set_node_graph(graph)
    assert len(panel._model.curves) >= 1


def test_panel_selects_first_curve_by_default(qapp, graph):
    panel = SplinePanel()
    panel.set_node_graph(graph)
    selected = [c for c in panel._model.curves if c.selected]
    assert len(selected) == 1


def test_panel_forwards_keyframe_changed(qapp, graph):
    panel = SplinePanel()
    panel.set_node_graph(graph)
    received = []
    panel.keyframe_changed.connect(lambda nid, pn, kfs: received.append((nid, pn)))
    # Emit from the underlying view.
    panel._view.keyframe_changed.emit("n1", "rotation", [])
    assert received


def test_panel_registered():
    from src.features.composite.module import CompositeModule
    reg = PanelRegistry()
    try:
        CompositeModule().register_panels(reg)
    except Exception:
        pass
    desc = reg.get("composite.spline")
    assert desc is not None
    assert desc.fallback_title == "Spline"
    assert desc.dock_attr == "dock_composite_spline"


def test_panel_set_current_frame(qapp, graph):
    panel = SplinePanel()
    panel.set_node_graph(graph)
    panel.set_current_frame(7)
    assert panel._view._current_frame == 7


def test_panel_transport_actions_seek_within_composition_range(qapp, graph):
    panel = SplinePanel()
    panel.set_node_graph(graph)
    panel.set_frame_range(10, 20, fps=30.0)
    panel.set_current_frame(10)

    received = []
    panel.current_frame_changed.connect(received.append)
    panel._act_next_frame.trigger()
    assert panel._view._current_frame == 11
    assert received[-1] == 11

    panel._act_first.trigger()
    panel._act_last.trigger()
    assert panel._view._current_frame == 20
    assert received[-1] == 20


def test_panel_playback_controller_is_connected_once(qapp):
    panel = SplinePanel()

    class Controller:
        def __init__(self):
            self.requests = []

        def keyframes_panel_play_toggle(self, want_play):
            self.requests.append(bool(want_play))

    controller = Controller()
    panel.set_playback_controller(controller)
    panel.set_playback_controller(controller)
    panel.play_toggle_requested.emit(True)
    assert controller.requests == [True]

    panel.set_playback_controller(None)
    panel.play_toggle_requested.emit(False)
    assert controller.requests == [True]


def test_panel_playback_button_works_without_controller(qapp):
    panel = SplinePanel()
    panel.set_frame_range(0, 2, fps=60.0)
    panel.set_current_frame(0)
    panel._act_play.trigger()
    assert panel._playing is True
    assert panel._play_timer.isActive()
    panel._act_play.trigger()
    assert panel._playing is False
    assert not panel._play_timer.isActive()


def test_panel_fit_viewport_tracks_late_dock_resize(qapp, graph):
    panel = SplinePanel()
    panel.resize(1200, 600)
    panel.set_node_graph(graph)
    panel.show()
    qapp.processEvents()
    assert panel._view._viewport is not None
    assert panel._view._viewport.plot_rect.width() > 900
    panel.close()
