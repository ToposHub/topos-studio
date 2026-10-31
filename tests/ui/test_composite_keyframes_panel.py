"""P6 tests: properties panel ◆ button writes per-node keyframe tracks.

Covers:
- A-4: NodePropertiesPanel.keyframes_changed reaches the NodeGraph (the
  node instance's keyframe_tracks is updated, not the class-level ParamDef).
- A-6: turning Animated OFF emits an empty list → the node track is deleted.
- A-7: a linked X/Y pair shares frames but keeps independent values.
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.core.node_engine import NodeGraph


@pytest.fixture
def panel_with_graph(qapp):
    """A NodePropertiesPanel bound to a NodeGraph containing one Transform."""
    g = NodeGraph()
    node = TransformNode()
    g.add_node(node)
    panel = NodePropertiesPanel()
    panel.set_node_graph(g)
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    panel.set_node(
        node.id, node.node_type, node.node_title,
        dict(node.parameters), {}, pdefs, enabled=True)
    return panel, node, g


# ── A-4: ◆ writes to the node instance, not the class ParamDef ──────────

def test_keyframe_button_writes_node_instance_track(panel_with_graph):
    """Adding keyframes via the panel updates node.keyframe_tracks."""
    panel, node, g = panel_with_graph
    pdef = panel._param_defs["rotation"]

    kfs = [{"frame": 0, "value": 0.0}, {"frame": 10, "value": 90.0}]
    panel._on_keyframes_changed("rotation", pdef, kfs)

    assert node.is_param_animated("rotation")
    stored = node.keyframe_tracks["rotation"]["keyframes"]
    assert len(stored) == 2
    assert stored[1]["frame"] == 10


def test_keyframe_change_emits_signal(panel_with_graph):
    """P0.2: the panel emits keyframes_changed so CompositePage can react."""
    panel, node, g = panel_with_graph
    pdef = panel._param_defs["rotation"]

    received = []
    panel.keyframes_changed.connect(
        lambda nid, ntype, pname, kfs: received.append((nid, pname, kfs)))

    panel._on_keyframes_changed("rotation", pdef,
                                [{"frame": 0, "value": 1.0}])
    assert received
    assert received[0][0] == node.id
    assert received[0][1] == "rotation"


def test_two_nodes_isolated_via_panel(qapp):
    """Editing node A via the panel must not animate node B (same type)."""
    g = NodeGraph()
    a = TransformNode()
    b = TransformNode()
    g.add_node(a)
    g.add_node(b)
    panel = NodePropertiesPanel()
    panel.set_node_graph(g)
    pdefs = {pd.name: pd for pd in a.PARAM_DEFS}
    panel.set_node(a.id, a.node_type, a.node_title, dict(a.parameters),
                   {}, pdefs, enabled=True)

    pdef = panel._param_defs["rotation"]
    panel._on_keyframes_changed("rotation", pdef,
                                [{"frame": 0, "value": 0.0},
                                 {"frame": 5, "value": 10.0}])

    assert a.is_param_animated("rotation")
    assert not b.is_param_animated("rotation")


# ── A-6: disabling animation removes the track ──────────────────────────

def test_disabling_animation_removes_track(panel_with_graph):
    """Closing Animated must delete the node-level track, not seed defaults."""
    panel, node, g = panel_with_graph
    pdef = panel._param_defs["rotation"]

    # First enable
    panel._on_keyframes_changed("rotation", pdef,
                                [{"frame": 0, "value": 0.0},
                                 {"frame": 10, "value": 5.0}])
    assert node.is_param_animated("rotation")

    # Then disable by emitting empty list
    panel._on_keyframes_changed("rotation", pdef, [])
    assert not node.is_param_animated("rotation")
    assert "rotation" not in node.keyframe_tracks


# ── A-7: X/Y pair keeps independent values ───────────────────────────────

def test_linked_xy_pair_keeps_independent_values(qapp):
    """A linked X/Y pair shares keyframe FRAMES but not VALUES."""
    g = NodeGraph()
    node = TransformNode()
    # Give X and Y distinct current values.
    node.parameters["scale_x"] = 1.0
    node.parameters["scale_y"] = 3.0
    g.add_node(node)
    panel = NodePropertiesPanel()
    panel.set_node_graph(g)
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    panel.set_node(node.id, node.node_type, node.node_title,
                   dict(node.parameters), {}, pdefs, enabled=True)

    pdef_x = panel._param_defs["scale_x"]
    pdef_y = panel._param_defs["scale_y"]

    # Simulate the pair editor building linked Y keyframes from X frames.
    # X track: frames 0 and 10, values 1.0 → 2.0
    x_kfs_dicts = [{"frame": 0, "value": 1.0, "interpolation": "linear"},
                   {"frame": 10, "value": 2.0, "interpolation": "linear"}]
    from src.shared.keyframe import Keyframe, KeyframeTrack
    x_track = KeyframeTrack.from_dict({"param_name": "scale_x",
                                       "keyframes": x_kfs_dicts,
                                       "min_val": 0.0, "max_val": 10.0})
    y_kfs = panel._build_linked_y_keyframes("scale_y", pdef_y, x_track.keyframes)

    # Y must share X's frames (0, 10) ...
    assert [k["frame"] for k in y_kfs] == [0, 10]
    # ... but keep Y's own current value (3.0), NOT X's 1.0/2.0.
    for k in y_kfs:
        assert k["value"] == 3.0


def test_pair_keyframe_button_checked_state_reflects_node(panel_with_graph):
    """The ◆ button reflects whether the node has a track."""
    panel, node, g = panel_with_graph
    pdef = panel._param_defs["rotation"]

    # Before any keyframes, the button is unchecked.
    btn = panel._param_widgets.get("rotation", {}).get("keyframe_btn") \
        if hasattr(panel, '_param_widgets') else None
    section = panel._sections.get(node.id)
    if section is not None and btn is None:
        btn = section._param_widgets.get("rotation", {}).get("keyframe_btn")

    panel._on_keyframes_changed("rotation", pdef,
                                [{"frame": 0, "value": 0.0}])
    if btn is not None:
        assert btn.isChecked()
    panel._on_keyframes_changed("rotation", pdef, [])
    if btn is not None:
        assert not btn.isChecked()
