"""P1 tests: unified Timeline ViewModel (T04, T07-T14).

Pure-data tests — no QWidget/QPainter. Covers:
- stable, reversible row_id scheme (T04)
- TimelineRow dataclass + adapter from legacy KeyframesModel (T07/T10)
- AnimationSelection cross-track/row selection + primary anchor (T08)
- TimelineViewState centralised viewport state (T09)
- AnimationSelectionModel observable + legacy model mirror (T08/T10/T13/T14)
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.ui.timeline_view_model import (
    AnimationSelection,
    AnimationSelectionModel,
    TimelineRow,
    TimelineViewState,
    build_timeline_rows,
    children_of,
    find_row,
    make_channel_row_id,
    make_expression_row_id,
    make_group_row_id,
    make_node_row_id,
    make_param_row_id,
    node_rows,
    parameter_rows,
    parse_row_id,
)
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.composite_core


def _graph_two_nodes_animated():
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    track.add_keyframe(Keyframe(20, 90.0))
    t.set_keyframe_track("rotation", track)
    b = BlurNode()
    g.add_node(t)
    g.add_node(b)
    return g, t, b


# ── T04: row_id scheme ─────────────────────────────────────────────────


class TestRowIdScheme:
    def test_node_row_id_round_trip(self):
        rid = make_node_row_id("ab12")
        assert rid == "node:ab12"
        assert parse_row_id(rid) == {"row_type": "node", "node_id": "ab12"}

    def test_group_row_id_round_trip(self):
        rid = make_group_row_id("ab12", "Transform")
        assert rid == "group:ab12:Transform"
        parsed = parse_row_id(rid)
        assert parsed["row_type"] == "group"
        assert parsed["node_id"] == "ab12"
        assert parsed["group_name"] == "Transform"

    def test_param_row_id_round_trip(self):
        rid = make_param_row_id("ab12", "rotation")
        assert rid == "param:ab12:rotation"
        assert parse_row_id(rid) == {
            "row_type": "param", "node_id": "ab12", "param_name": "rotation"}

    def test_channel_row_id_round_trip(self):
        rid = make_channel_row_id("ab12", "scale", "x")
        assert rid == "channel:ab12:scale:x"
        parsed = parse_row_id(rid)
        assert parsed["row_type"] == "channel"
        assert parsed["node_id"] == "ab12"
        assert parsed["param_name"] == "scale"
        assert parsed["channel"] == "x"

    def test_expression_row_id_round_trip(self):
        rid = make_expression_row_id("ab12", "opacity")
        assert rid == "expr:ab12:opacity"
        parsed = parse_row_id(rid)
        assert parsed["row_type"] == "expr"
        assert parsed["node_id"] == "ab12"
        assert parsed["param_name"] == "opacity"

    def test_parse_row_id_malformed_does_not_raise(self):
        # Garbage in, unknown out — never raise. Callers (AI/script) pass
        # arbitrary ids through.
        assert parse_row_id("")["row_type"] == "unknown"
        assert parse_row_id("notanid")["row_type"] == "unknown"
        assert parse_row_id("frobnicate:x:y")["row_type"] == "unknown"
        assert parse_row_id(None)["row_type"] == "unknown"

    def test_legacy_row_ids_parse_to_same_type_views_emit(self):
        # The legacy KeyframesModel emits exactly "node:{id}" / "param:...".
        # parse_row_id must accept them so existing view code keeps working.
        assert parse_row_id("node:xyz")["row_type"] == "node"
        assert parse_row_id("param:xyz:radius")["row_type"] == "param"


# ── T07/T10: TimelineRow + adapter ─────────────────────────────────────


class TestTimelineRowAdapter:
    def test_adapter_preserves_legacy_row_count_and_ids(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        legacy_rows = m.rows
        trows = build_timeline_rows(m)

        # 1:1 today: every legacy row maps to a TimelineRow, same id/order.
        assert [r.row_id for r in trows] == [r.row_id for r in legacy_rows]

    def test_adapter_node_row_fields(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        t_node = find_row(trows, make_node_row_id(t.id))
        assert t_node is not None
        assert t_node.is_node is True
        assert t_node.node_id == t.id
        assert t_node.depth == 0
        assert t_node.parent_row_id is None
        # Node row has a (default or explicit) range bar → is_animated True
        # in the AE "has a layer bar" sense.
        assert t_node.is_animated is True

    def test_adapter_param_row_fields_and_parent(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        rot_row = find_row(trows, make_param_row_id(t.id, "rotation"))
        assert rot_row is not None
        assert rot_row.is_parameter is True
        assert rot_row.parameter_name == "rotation"
        assert rot_row.node_id == t.id
        assert rot_row.parent_row_id == make_node_row_id(t.id)
        assert rot_row.depth == 1
        # Three keyframes → is_animated True
        assert rot_row.is_animated is True

    def test_adapter_non_animated_param_not_marked_animated(self):
        # Blur radius has no track in our fixture → is_animated False.
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        radius = find_row(trows, make_param_row_id(b.id, "radius"))
        assert radius is not None
        assert radius.is_animated is False

    def test_node_and_parameter_helpers(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        # 2 nodes in fixture (transform + blur)
        assert len(node_rows(trows)) == 2
        # many animatable params on transform + radius on blur
        assert all(r.is_parameter for r in parameter_rows(trows))
        assert any(r.parameter_name == "rotation"
                   for r in parameter_rows(trows))

    def test_children_of_returns_param_rows(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        kids = children_of(trows, make_node_row_id(t.id))
        assert all(k.parent_row_id == make_node_row_id(t.id) for k in kids)
        assert any(k.parameter_name == "rotation" for k in kids)

    def test_find_row_missing_returns_none(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        trows = build_timeline_rows(m)
        assert find_row(trows, "nope:nothing") is None

    def test_timeline_row_is_properties(self):
        node_row = TimelineRow(row_id="node:x", node_id="x", row_type="node",
                               display_name="n")
        param_row = TimelineRow(row_id="param:x:p", node_id="x",
                                row_type="param", display_name="p")
        chan_row = TimelineRow(row_id="channel:x:p:y", node_id="x",
                               row_type="channel", display_name="p.y")
        grp_row = TimelineRow(row_id="group:x:g", node_id="x",
                              row_type="group", display_name="g")
        expr_row = TimelineRow(row_id="expr:x:p", node_id="x",
                               row_type="expr", display_name="fx")
        assert node_row.is_node and not node_row.is_parameter
        assert param_row.is_parameter and not param_row.is_node
        assert chan_row.is_channel
        assert grp_row.is_group
        assert expr_row.is_expression

    def test_timeline_row_default_channels_is_immutable_and_isolated(self):
        # channels must default to an immutable empty container (tuple, not
        # list) so two rows never share a mutable default that one mutation
        # could leak into the other. CPython interns the empty tuple, so we
        # assert immutability + isolation rather than identity.
        r1 = TimelineRow(row_id="node:x", node_id="x", row_type="node",
                         display_name="n")
        r2 = TimelineRow(row_id="node:y", node_id="y", row_type="node",
                         display_name="n")
        assert r1.channels == ()
        assert r2.channels == ()
        assert isinstance(r1.channels, tuple)  # immutable
        # Assigning a new channels tuple on one row does not affect the other.
        r1.channels = ("x", "y")
        assert r1.channels == ("x", "y")
        assert r2.channels == ()


# ── T08: AnimationSelection data ───────────────────────────────────────


class TestAnimationSelection:
    def test_empty_and_clear(self):
        s = AnimationSelection()
        assert s.is_empty()
        s.keyframes.add(("param:n:p", 5))
        assert not s.is_empty()
        s.clear()
        assert s.is_empty()

    def test_keyframes_for_row_filters_and_sorts(self):
        s = AnimationSelection()
        s.keyframes = {("param:n:p", 10), ("param:n:p", 2),
                       ("param:n:q", 7)}
        assert s.keyframes_for_row("param:n:p") == [2, 10]
        assert s.keyframes_for_row("param:n:q") == [7]
        assert s.keyframes_for_row("param:n:zzz") == []

    def test_clone_is_independent(self):
        s = AnimationSelection()
        s.keyframes.add(("param:n:p", 1))
        s.rows.add("node:n")
        s.primary_row_id = "node:n"
        c = s.clone()
        c.keyframes.add(("param:n:p", 2))
        c.rows.discard("node:n")
        # clone mutation does not leak back
        assert s.keyframes == {("param:n:p", 1)}
        assert s.rows == {"node:n"}

    def test_keyframe_count(self):
        s = AnimationSelection()
        s.keyframes = {("a", 1), ("b", 2), ("a", 3)}
        assert s.keyframe_count() == 3


# ── T09: TimelineViewState ─────────────────────────────────────────────


class TestTimelineViewState:
    def test_defaults_are_sane(self):
        s = TimelineViewState()
        assert s.current_frame == 0
        assert s.start_frame == 0
        assert s.display_mode == "time"
        assert s.view_mode == "timeline"
        assert s.filter_mode == "all"
        assert s.snap_enabled is True

    def test_view_mode_field_exists_for_graph_editor(self):
        # T100 reserves this. Ensure the field is present now so P3+ code
        # can read/write it without a schema churn.
        s = TimelineViewState(view_mode="graph")
        assert s.view_mode == "graph"


# ── T08/T10/T13/T14: AnimationSelectionModel ───────────────────────────


class TestAnimationSelectionModel:
    def test_subscribe_and_notify(self):
        sm = AnimationSelectionModel()
        events = []
        sm.subscribe(events.append)
        sm.select_keyframe("param:n:p", 5)
        assert len(events) == 1
        assert events[0].keyframes == {("param:n:p", 5)}

    def test_select_keyframe_replace_vs_additive(self):
        sm = AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 1)
        sm.select_keyframe("param:n:p", 5)  # replace
        assert sm.selection.keyframes == {("param:n:p", 5)}
        sm.select_keyframe("param:n:p", 9, additive=True)
        assert sm.selection.keyframes == {("param:n:p", 5), ("param:n:p", 9)}

    def test_select_keyframe_sets_primary(self):
        sm = AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 5)
        assert sm.primary_row_id == "param:n:p"
        assert sm.primary_frame == 5

    def test_toggle_keyframe(self):
        sm = AnimationSelectionModel()
        sm.toggle_keyframe("param:n:p", 5)
        assert sm.keyframe_count() == 1
        sm.toggle_keyframe("param:n:p", 5)
        assert sm.is_empty()

    def test_set_keyframes_deterministic_primary(self):
        sm = AnimationSelectionModel()
        sm.set_keyframes([("param:n:p", 10), ("param:n:p", 2),
                          ("param:n:q", 5)])
        # Primary anchored on the lowest-sorted pair → deterministic for tests
        assert sm.primary_row_id == "param:n:p"
        assert sm.primary_frame == 2

    def test_add_keyframes_keeps_existing_primary_when_possible(self):
        sm = AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 5)
        sm.add_keyframes([("param:n:p", 10)])
        assert sm.primary_frame == 5  # untouched

    def test_clear_and_clear_keyframes(self):
        sm = AnimationSelectionModel()
        sm.select_keyframe("param:n:p", 1)
        sm.select_row("node:n", additive=True)
        sm.clear_keyframes()
        assert sm.keyframe_count() == 0
        # AUD-11115: a selected keyframe implies its property row is
        # selected (sync_from_model invariant), so the keyframe's row stays
        # alongside the explicitly selected node row; clear_keyframes never
        # drops rows.
        assert sm.selected_rows() == ["node:n", "param:n:p"]  # rows survive
        sm.clear()
        assert sm.is_empty()

    def test_select_row_replace_vs_additive(self):
        sm = AnimationSelectionModel()
        sm.select_row("node:a")
        sm.select_row("node:b")  # replace
        assert sm.selected_rows() == ["node:b"]
        sm.select_row("node:a", additive=True)
        assert set(sm.selected_rows()) == {"node:a", "node:b"}

    def test_batch_coalesces_notifications(self):
        sm = AnimationSelectionModel()
        events = []
        sm.subscribe(events.append)
        sm.begin_batch()
        sm.select_keyframe("param:n:p", 1)
        sm.select_keyframe("param:n:p", 2, additive=True)
        sm.select_keyframe("param:n:p", 3, additive=True)
        assert events == []  # no mid-batch notification
        sm.end_batch()
        assert len(events) == 1
        assert events[0].keyframe_count() == 3

    def test_buggy_subscriber_does_not_break_others(self):
        sm = AnimationSelectionModel()
        good = []
        def bad(_):
            raise RuntimeError("boom")
        sm.subscribe(bad)
        sm.subscribe(good.append)
        sm.select_keyframe("param:n:p", 1)  # must not raise
        assert len(good) == 1

    def test_unsubscribe(self):
        sm = AnimationSelectionModel()
        events = []
        cb = events.append
        sm.subscribe(cb)
        sm.unsubscribe(cb)
        sm.select_keyframe("param:n:p", 1)
        assert events == []


class TestSelectionModelMirror:
    """T10/T14: the shared model mirrors the legacy per-point flags so the
    Spline panel and future Graph Editor read/write the *same* selection as
    the Keyframes timeline, without each view keeping its own copy.
    """

    def _model_with_selected_kf(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g)
        rot_row_id = f"param:{t.id}:rotation"
        track = m.track_for(rot_row_id)
        # Mark two keyframes as selected in the legacy model, as a
        # marquee / Shift-click would.
        track.keyframes[0].selected = True   # frame 0
        track.keyframes[2].selected = True   # frame 20
        return m, t, rot_row_id

    def test_sync_from_model_pulls_point_flags(self):
        m, t, rot_row_id = self._model_with_selected_kf()
        sm = AnimationSelectionModel()
        sm.sync_from_model(m)
        assert sm.keyframe_count() == 2
        assert (rot_row_id, 0) in sm.selection.keyframes
        assert (rot_row_id, 20) in sm.selection.keyframes
        # primary anchored on the first key encountered
        assert sm.primary_frame == 0

    def test_apply_to_model_pushes_point_flags(self):
        m, t, rot_row_id = self._model_with_selected_kf()
        sm = AnimationSelectionModel()
        sm.set_keyframes([(rot_row_id, 10)])  # only frame 10
        sm.apply_to_model(m)
        track = m.track_for(rot_row_id)
        sel = [kf.frame for kf in track.keyframes if kf.selected]
        assert sel == [10]

    def test_round_trip_sync_apply_sync(self):
        m, t, rot_row_id = self._model_with_selected_kf()
        sm = AnimationSelectionModel()
        sm.sync_from_model(m)
        # Mutate via shared model, push back
        sm.toggle_keyframe(rot_row_id, 10)
        sm.apply_to_model(m)
        track = m.track_for(rot_row_id)
        frames = sorted(kf.frame for kf in track.keyframes if kf.selected)
        assert frames == [0, 10, 20]

    def test_sync_picks_up_selected_node_row(self):
        g, t, b = _graph_two_nodes_animated()
        m = KeyframesModel()
        m.rebuild(g, selected_node_id=t.id)
        sm = AnimationSelectionModel()
        sm.sync_from_model(m)
        assert make_node_row_id(t.id) in sm.selection.rows
        # A single selected row becomes primary_row_id
        assert sm.primary_row_id == make_node_row_id(t.id)
