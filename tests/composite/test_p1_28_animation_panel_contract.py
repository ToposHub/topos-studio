"""P1-28 Keyframes timeline contracts.

These tests exercise the first runtime slice of the professional timeline
upgrade: semantic property filters, atomic time scaling, and one host undo
boundary for a batch edit.
"""

import pytest
from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QKeyEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_model import FrameMapper, KeyframesModel
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.spline_panel import SplinePanel
from src.features.composite.ui.timeline_view_model import AnimationSelectionModel
from src.features.composite.ui.keyframes_timeline_view import (
    KeyframesTimelineView,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


@pytest.mark.usefixtures("qapp")
def test_keyframes_panel_exposes_professional_property_filters(qapp):
    panel = KeyframesPanel()
    data = [panel._filter_combo.itemData(i)
            for i in range(panel._filter_combo.count())]
    assert data == ["all", "animated", "modified", "expression", "selected"]
    panel.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_timeline_selection_reaches_shared_selection_model(qapp):
    graph = NodeGraph()
    node = TransformNode(node_id="transform")
    track = KeyframeTrack(param_name="rotation", min_val=-360.0,
                          max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    node.set_keyframe_track("rotation", track)
    graph.add_node(node)

    panel = KeyframesPanel()
    panel.set_node_graph(graph)
    panel._timeline_view._select_keyframe("param:transform:rotation", 0)
    assert panel.selection_model.selected_keyframes() == [
        ("param:transform:rotation", 0)]
    assert panel.selection_model.selected_rows() == [
        "param:transform:rotation"]
    panel.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_canonical_shortcut_dispatcher_drives_panel_filter(qapp):
    panel = KeyframesPanel()
    event = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_U,
                      Qt.KeyboardModifier.NoModifier, "u")
    assert panel.eventFilter(panel._timeline_view, event) is True
    assert panel._model.filter_mode == "animated"
    panel.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_spline_point_selection_back_syncs_to_shared_model(qapp):
    graph = NodeGraph()
    node = TransformNode(node_id="transform")
    track = KeyframeTrack(param_name="rotation", min_val=-360.0,
                          max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    node.set_keyframe_track("rotation", track)
    graph.add_node(node)

    spline = SplinePanel()
    shared = AnimationSelectionModel()
    spline.attach_to_shared_selection(shared)
    spline.set_node_graph(graph)
    curve = spline._model.curve_by_id("transform:rotation")
    assert curve is not None
    curve.selected = True
    curve.keys[0].selected = True
    spline._view._emit_key_selection_changed()

    assert shared.selected_keyframes() == [("param:transform:rotation", 0)]
    assert shared.selected_rows() == ["param:transform:rotation"]
    spline.deleteLater()


@pytest.mark.usefixtures("qapp")
def test_scale_selected_keyframes_preserves_selection_and_emits_one_batch(qapp):
    graph = NodeGraph()
    node = TransformNode(node_id="transform")
    track = KeyframeTrack(param_name="rotation", min_val=-360.0,
                          max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    node.set_keyframe_track("rotation", track)
    graph.add_node(node)

    model = KeyframesModel()
    model.rebuild(graph)
    view = KeyframesTimelineView(model, FrameMapper(pixels_per_frame=4.0))
    row_id = "param:transform:rotation"
    view._select_keyframe(row_id, 0)
    view._select_keyframe(row_id, 10, additive=True)

    batches = []
    emitted = []
    view.track_batch_about_to_commit.connect(
        lambda label, count: batches.append(("start", label, count)))
    view.track_batch_committed.connect(
        lambda label, count: batches.append(("end", label, count)))
    view.track_changed.connect(
        lambda node_id, param_name, keyframes:
        emitted.append((node_id, param_name, keyframes)))

    assert view.scale_selected_keyframes(2.0, pivot_frame=0) is True
    assert [kf.frame for kf in model.track_for(row_id).keyframes] == [0, 20]
    assert view.selected_keyframes() == [(row_id, 0), (row_id, 20)]
    assert batches == [("start", "scale", 1), ("end", "scale", 1)]
    assert len(emitted) == 1
    assert [item["frame"] for item in emitted[0][2]] == [0, 20]


@pytest.mark.usefixtures("qapp")
def test_scale_selected_keyframes_rejects_rounding_collision_atomically(qapp):
    graph = NodeGraph()
    node = TransformNode(node_id="transform")
    track = KeyframeTrack(param_name="rotation", min_val=-360.0,
                          max_val=360.0)
    for frame, value in ((0, 0.0), (1, 10.0), (10, 20.0)):
        track.add_keyframe(Keyframe(frame, value))
    node.set_keyframe_track("rotation", track)
    graph.add_node(node)

    model = KeyframesModel()
    model.rebuild(graph)
    view = KeyframesTimelineView(model, FrameMapper(pixels_per_frame=4.0))
    row_id = "param:transform:rotation"
    view._select_keyframe(row_id, 0)
    view._select_keyframe(row_id, 1, additive=True)
    emitted = []
    view.track_changed.connect(lambda *_args: emitted.append(True))

    assert view.scale_selected_keyframes(0.5, pivot_frame=0) is False
    assert [kf.frame for kf in model.track_for(row_id).keyframes] == [0, 1, 10]
    assert emitted == []
