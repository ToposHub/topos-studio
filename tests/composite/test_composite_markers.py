"""B04: composition-owned markers with stable UUIDs and persistence.

Markers belong to the composition (NodeGraph), carry a stable UUID,
integer frame, name, optional duration and color.  Same-frame markers are
independently editable; every command is one undo step; serialization and
migration preserve ids and positions (out-of-bounds markers survive a
shortened composition).
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.core.composite_markers import (
    CompositeMarker, markers_report, parse_markers,
)
from src.features.composite.core.graph_migration import (
    COMPOSITE_SCHEMA_VERSION, migrate_graph,
)
from src.features.composite.core.layer_stack_controller import LayerStackController
from src.features.composite.core.undo_stack import UndoStack

pytestmark = pytest.mark.composite_core


def _controller():
    graph = NodeGraph()
    undo = UndoStack()
    controller = LayerStackController(graph, undo)
    return graph, undo, controller


def test_same_frame_markers_are_independently_editable():
    graph, undo, controller = _controller()
    first = controller.add_marker(24, "Impact")
    second = controller.add_marker(24, "Flash")
    assert first and second and first != second
    assert [m.frame for m in controller.markers()] == [24, 24]

    # Deleting one same-frame marker keeps the other.
    assert controller.remove_marker(first)
    remaining = controller.markers()
    assert len(remaining) == 1
    assert remaining[0].marker_id == second
    assert remaining[0].name == "Flash"


def test_rename_and_move_are_single_undo_steps():
    graph, undo, controller = _controller()
    marker_id = controller.add_marker(10, "Beat")
    assert controller.rename_marker(marker_id, "Downbeat")
    assert controller.markers()[0].name == "Downbeat"
    undo.undo()
    assert controller.markers()[0].name == "Beat"
    undo.redo()
    assert controller.markers()[0].name == "Downbeat"

    assert controller.move_marker(marker_id, 42)
    assert controller.markers()[0].frame == 42
    undo.undo()
    assert controller.markers()[0].frame == 10
    undo.undo()  # the rename
    assert controller.markers()[0].name == "Beat"

    # Unknown ids are refused without touching state.
    assert not controller.rename_marker("missing", "X")
    assert not controller.move_marker("missing", 5)
    assert not controller.remove_marker("missing")


def test_duration_and_color_edits():
    graph, undo, controller = _controller()
    marker_id = controller.add_marker(0, "Chapter")
    assert controller.set_marker_duration(marker_id, 12)
    assert controller.markers()[0].duration == 12
    assert controller.set_marker_duration(marker_id, None)  # back to point
    assert controller.markers()[0].duration is None
    assert not controller.set_marker_duration(marker_id, -3)
    assert controller.set_marker_color(marker_id, "#ff0000")
    assert controller.markers()[0].color == "#ff0000"
    undo.undo()
    assert controller.markers()[0].color != "#ff0000"


def test_markers_round_trip_through_serialization():
    graph, _, controller = _controller()
    one = controller.add_marker(1, "One", duration=5)
    two = controller.add_marker(3, "Two", color="#00ff00")
    three = controller.add_marker(3, "Three")

    data = graph.to_dict()
    assert data["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION
    reloaded = NodeGraph.from_dict(data)
    restored = {m.marker_id: m for m in reloaded.get_markers()}
    # Same-frame markers tie-break on id, so compare by identity not order.
    assert set(restored) == {one, two, three}
    assert restored[one].duration == 5
    assert restored[two].color == "#00ff00"
    assert restored[three].name == "Three"
    # Stable ids survive the round trip (checked above via set equality).
    assert {m.frame for m in restored.values()} == {1, 3}


def test_migration_normalizes_markers_and_bumps_version():
    legacy = {"composite_schema_version": 11, "nodes": {}, "connections": []}
    migrated = migrate_graph(legacy)
    assert migrated["composite_schema_version"] == COMPOSITE_SCHEMA_VERSION
    assert "markers" not in migrated

    messy = {
        "composite_schema_version": 11,
        "markers": [
            {"marker_id": "m1", "frame": 7, "name": "Keep"},
            {"frame": 9},                       # no id → dropped
            {"marker_id": "m1", "frame": 11},   # duplicate id → first kept
            "garbage",                          # not a dict → dropped
        ],
    }
    migrated = migrate_graph(messy)
    assert [m["marker_id"] for m in migrated["markers"]] == ["m1"]
    graph = NodeGraph.from_dict(migrated)
    assert [m.frame for m in graph.get_markers()] == [7]


def test_shortened_composition_keeps_out_of_bounds_markers():
    graph, undo, controller = _controller()
    inside = controller.add_marker(5, "Inside")
    outside = controller.add_marker(90, "Outside")

    # Shorten the composition to 50 frames: the marker stays but is
    # reported out of bounds (recoverable on re-lengthening).
    graph.duration_frames = 50
    visible, oob = markers_report(graph.get_marker_map(), 50)
    assert [m.marker_id for m in visible] == [inside]
    assert [m.marker_id for m in oob] == [outside]

    # Save/reload with the shorter duration keeps the marker position.
    reloaded = NodeGraph.from_dict(graph.to_dict())
    assert {m.marker_id: m.frame for m in reloaded.get_markers()} == {
        inside: 5, outside: 90}

    # Re-lengthening restores visibility without any edit.
    graph.duration_frames = 100
    visible, oob = markers_report(graph.get_marker_map(), 100)
    assert {m.marker_id for m in visible} == {inside, outside}
    assert oob == []


def test_parse_markers_tolerates_corruption():
    markers = parse_markers([
        {"marker_id": "ok", "frame": 3},
        None,
        {"marker_id": "", "frame": 4},
        {"marker_id": "bad", "frame": "x"},
        {"marker_id": "neg", "frame": 2, "duration": -4},
        {"marker_id": "malformed_duration", "frame": 8, "duration": {}},
        {"marker_id": "invalid_frame", "frame": float("inf")},
    ])
    assert set(markers) == {"ok", "neg", "malformed_duration"}
    assert markers["neg"].duration is None
    assert markers["malformed_duration"].duration is None
    assert parse_markers("not-a-list") == {}
