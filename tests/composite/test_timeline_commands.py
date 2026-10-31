"""P2 tests: unified TimelineCommand layer (T15-T21, T65).

Pure-data tests — no QWidget/QPainter. The command builder is driven
through a fake host so every code path (add/remove, move transaction,
copy/paste, interpolation/tangent/handles/extrap, expression, node range,
track lock) is exercised headlessly and verified against the live graph
state + the undo hooks the host would receive.

These tests are the contract for P2: the AI/script wiring (T23), the
properties-panel wiring (T22) and the view rewrites (P3+) all rely on
these commands producing the exact before/after snapshots + error codes
codified here.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.ui.timeline_commands import (
    Clipboard,
    ERR_EXPR_INVALID,
    ERR_KEYFRAME_NOT_FOUND,
    ERR_LOCKED,
    ERR_NODE_NOT_FOUND,
    ERR_OK,
    ERR_PARAM_NOT_FOUND,
    ERR_PASTE_TYPE_MISMATCH,
    ERR_VALUE_OUT_OF_RANGE,
    NodeRangeChange,
    TimelineCommandBuilder,
    TrackChange,
    TrackLockRegistry,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

pytestmark = pytest.mark.composite_core


# ── fake host ───────────────────────────────────────────────────────────


class FakeHost:
    """Minimal TimelineHost: holds the live graph + records undo callbacks."""

    def __init__(self, graph):
        self._graph = graph
        self.track_changes = []
        self.range_changes = []

    def get_node_graph(self):
        return self._graph

    def record_track_change(self, change: TrackChange) -> None:
        self.track_changes.append(change)

    def record_node_range_change(self, change: NodeRangeChange) -> None:
        self.range_changes.append(change)


def _graph_with_rotation():
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation",
                          min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    track.add_keyframe(Keyframe(20, 90.0,
                                interpolation=InterpolationType.BEZIER))
    t.set_keyframe_track("rotation", track)
    b = BlurNode()
    g.add_node(t)
    g.add_node(b)
    return g, t, b


def _track_frames(node, param):
    """Sorted (frame, value) pairs on a node's track."""
    data = node.keyframe_tracks.get(param) or {}
    return sorted((int(kf["frame"]), float(kf["value"]))
                  for kf in data.get("keyframes", []))


# ── T16: add / remove ───────────────────────────────────────────────────


class TestAddRemoveKeyframe:
    def test_add_keyframe_applies_and_records_undo(self):
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        r = cmds.add_keyframe(t.id, "rotation", 30, 100.0)
        assert r.ok
        assert (30, 100.0) in _track_frames(t, "rotation")
        # Exactly one undo record for the affected track.
        assert len(host.track_changes) == 1
        ch = host.track_changes[0]
        assert ch.node_id == t.id and ch.param_name == "rotation"
        # before has 3 kfs, after has 4.
        assert len(ch.before["keyframes"]) == 3
        assert len(ch.after["keyframes"]) == 4

    def test_add_keyframe_replaces_existing_frame_no_duplicate(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # frame 10 already exists with value 45 — overwrite.
        r = cmds.add_keyframe(t.id, "rotation", 10, 99.0)
        assert r.ok
        frames = dict(_track_frames(t, "rotation"))
        assert frames[10] == 99.0
        assert len(frames) == 3  # no duplicate

    def test_remove_keyframe(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.remove_keyframe(t.id, "rotation", 10)
        assert r.ok
        frames = [f for f, _ in _track_frames(t, "rotation")]
        assert 10 not in frames
        assert frames == [0, 20]

    def test_remove_missing_keyframe_returns_stable_code(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.remove_keyframe(t.id, "rotation", 999)
        assert not r.ok
        assert r.error_code == ERR_KEYFRAME_NOT_FOUND
        # Graph untouched on failure.
        assert len(_track_frames(t, "rotation")) == 3

    def test_add_value_out_of_range_returns_stable_code(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # rotation range is [-360, 360]
        r = cmds.add_keyframe(t.id, "rotation", 5, 5000.0)
        assert not r.ok
        assert r.error_code == ERR_VALUE_OUT_OF_RANGE

    def test_unknown_node_returns_stable_code(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.add_keyframe("does-not-exist", "rotation", 5, 10.0)
        assert r.error_code == ERR_NODE_NOT_FOUND

    def test_clear_track_removes_all_keyframes(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.clear_track(t.id, "rotation")
        assert r.ok
        assert "rotation" not in t.keyframe_tracks


# ── T17: move transaction ──────────────────────────────────────────────


class TestMoveTransaction:
    def test_move_press_move_release_one_undo_per_track(self):
        """A 2-keyframe drag on one track = one undo record.

        AUD-11010 regression contract: a drag is a MOVE — the source frames
        disappear (previously they stayed, duplicating every dragged key),
        the moved keys keep their payload, and one undo record is pushed.
        """
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        # Drag frames 0 and 10 forward by 5 frames (→ 5 and 15), keep value.
        sel = [(t.id, "rotation", 0), (t.id, "rotation", 10)]
        assert cmds.begin_move(sel).ok
        # The view computes new frames; values stay the same.
        cmds.update_move_preview([
            (t.id, "rotation", 0, 5, 0.0),
            (t.id, "rotation", 10, 15, 45.0),
        ])
        r = cmds.commit_move()
        assert r.ok
        frames = dict(_track_frames(t, "rotation"))
        assert frames[5] == 0.0
        assert frames[15] == 45.0
        # AUD-11010: source frames are GONE (move, not copy).
        assert 0 not in frames
        assert 10 not in frames
        # Unmoved kf at frame 20 preserved.
        assert frames[20] == 90.0
        # ONE undo record for the track (not one per keyframe).
        assert len(host.track_changes) == 1

    def test_move_across_two_tracks_one_undo_each(self):
        """Multi-track drag: one undo per track, not per keyframe."""
        g, t, b = _graph_with_rotation()
        # put a track on blur radius so we have a second affected track
        bt = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
        bt.add_keyframe(Keyframe(0, 1.0))
        bt.add_keyframe(Keyframe(10, 5.0))
        b.set_keyframe_track("radius", bt)
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        sel = [(t.id, "rotation", 0), (b.id, "radius", 0)]
        assert cmds.begin_move(sel).ok
        cmds.update_move_preview([
            (t.id, "rotation", 0, 7, 0.0),
            (b.id, "radius", 0, 7, 1.0),
        ])
        r = cmds.commit_move()
        assert r.ok
        # one undo record per affected track = 2 total
        assert len(host.track_changes) == 2
        affected = {(c.node_id, c.param_name) for c in host.track_changes}
        assert affected == {(t.id, "rotation"), (b.id, "radius")}

    def test_cancel_move_does_not_touch_graph(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        before = _track_frames(t, "rotation")
        assert cmds.begin_move([(t.id, "rotation", 0)]).ok
        cmds.update_move_preview([(t.id, "rotation", 0, 50, 0.0)])
        cmds.cancel_move()
        assert _track_frames(t, "rotation") == before

    def test_move_blocked_by_lock_aborts_whole_drag(self):
        """A locked track aborts the drag BEFORE it starts (no partial)."""
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        cmds.set_track_lock(t.id, "rotation", True)
        r = cmds.begin_move([(t.id, "rotation", 0)])
        assert not r.ok and r.error_code == ERR_LOCKED
        # commit on an aborted drag is a no-op error
        r2 = cmds.commit_move()
        assert r2.error_code != ERR_OK


# ── T18: copy / paste ──────────────────────────────────────────────────


class TestCopyPaste:
    def test_copy_then_paste_anchors_at_playhead(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # Copy rotation kfs at 10 and 20 (earliest = 10 → anchor).
        n = cmds.copy_keyframes([
            (t.id, "rotation", 10, 45.0, "linear"),
            (t.id, "rotation", 20, 90.0, "bezier"),
        ])
        assert n == 2
        # Paste at playhead 100 → new kfs at 100 and 110.
        r = cmds.paste_keyframes(t.id, "rotation", 100)
        assert r.ok
        frames = dict(_track_frames(t, "rotation"))
        assert frames[100] == 45.0
        assert frames[110] == 90.0
        # Original keyframes are preserved (paste merges, doesn't clobber).
        assert frames[10] == 45.0
        assert frames[20] == 90.0

    def test_paste_empty_clipboard_returns_no_target(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.paste_keyframes(t.id, "rotation", 0)
        assert not r.ok
        assert r.error_code == "no_target"

    def test_paste_type_mismatch_rejected(self):
        """Scalar → scalar OK, but a source/target ptype mismatch is refused
        rather than silently truncating (plan §19.10)."""
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # rotation is float; pretend we copied from an int param by setting
        # the clipboard source ptype directly.
        cmds.copy_keyframes([(t.id, "rotation", 10, 45.0, "linear")])
        cmds.clipboard.source_ptype = "int"
        r = cmds.paste_keyframes(t.id, "rotation", 0)
        assert not r.ok
        assert r.error_code == ERR_PASTE_TYPE_MISMATCH

    def test_paste_preserves_interpolation_from_source(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        cmds.copy_keyframes([(t.id, "rotation", 20, 90.0, "bezier")])
        cmds.paste_keyframes(t.id, "rotation", 100)
        data = t.keyframe_tracks["rotation"]
        pasted = next(kf for kf in data["keyframes"] if kf["frame"] == 100)
        assert pasted["interpolation"] == "bezier"

    def test_clipboard_is_shared_singleton(self):
        """The builder's clipboard is the same object across calls — Spline,
        Graph Editor and Keyframes all paste the same source."""
        cmds = TimelineCommandBuilder(FakeHost(NodeGraph()))
        assert isinstance(cmds.clipboard, Clipboard)
        cmds.copy_keyframes([])  # clears
        assert cmds.clipboard.is_empty()


# ── T19: interpolation / tangent / handles / extrap ────────────────────


class TestInterpolationTangentHandles:
    def test_set_interpolation(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_interpolation(t.id, "rotation", 0, "hold")
        assert r.ok
        data = t.keyframe_tracks["rotation"]
        kf0 = next(kf for kf in data["keyframes"] if kf["frame"] == 0)
        assert kf0["interpolation"] == "hold"

    def test_set_tangent_mode(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_tangent_mode(t.id, "rotation", 10, "broken")
        assert r.ok
        data = t.keyframe_tracks["rotation"]
        kf = next(kf for kf in data["keyframes"] if kf["frame"] == 10)
        assert kf["tangent_mode"] == "broken"

    def test_set_bezier_handles(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_bezier_handles(t.id, "rotation", 10,
                                    handle_left=(-2.0, -10.0),
                                    handle_right=(2.0, 10.0))
        assert r.ok
        data = t.keyframe_tracks["rotation"]
        kf = next(kf for kf in data["keyframes"] if kf["frame"] == 10)
        assert list(kf["handle_left"]) == [-2.0, -10.0]
        assert list(kf["handle_right"]) == [2.0, 10.0]

    def test_set_extrapolation(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_extrapolation(t.id, "rotation",
                                   pre_extrap="linear",
                                   post_extrap="linear")
        assert r.ok
        data = t.keyframe_tracks["rotation"]
        assert data["pre_extrap"] == "linear"
        assert data["post_extrap"] == "linear"


# ── T20: expression ────────────────────────────────────────────────────


class TestExpression:
    def test_set_expression_keeps_keyframe_track(self):
        """N-04（2026-10-14 审计）：表达式不再清除关键帧轨道——轨道构成
        base，启用的表达式在求值时覆盖 base（loopOut/valueAtTime 采样
        pre-expression 轨道）。A-15 互斥撤销；track 不再进入删除记录。"""
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        r = cmds.set_expression(t.id, "rotation", "value + 10")
        assert r.ok
        # rotation keyframe track survives alongside the expression.
        assert "rotation" in t.keyframe_tracks
        assert t.get_expression("rotation") == "value + 10"
        # Track record (if emitted) keeps before==after — the track is NOT
        # removed; undo restores the expression text, not a deleted track.
        for ch in host.track_changes:
            assert ch.before is not None
            assert ch.after is not None
            assert len(ch.after["keyframes"]) == len(ch.before["keyframes"])

    def test_invalid_expression_returns_stable_code(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_expression(t.id, "rotation", "   ")  # empty → invalid
        assert not r.ok
        assert r.error_code == ERR_EXPR_INVALID
        # track untouched on failure.
        assert "rotation" in t.keyframe_tracks

    def test_remove_expression(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # First set, then remove.
        assert cmds.set_expression(t.id, "rotation", "value").ok
        r = cmds.remove_expression(t.id, "rotation")
        assert r.ok
        assert not t.has_expression("rotation")


# ── T21: node range ────────────────────────────────────────────────────


class TestNodeRange:
    def test_set_node_range_emits_range_change(self):
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        r = cmds.set_node_range(t.id, start=5, end=50)
        assert r.ok
        assert len(host.range_changes) == 1
        ch = host.range_changes[0]
        assert ch.after["comp_start"] == 5
        assert ch.after["comp_end"] == 50

    def test_set_node_range_rejects_end_before_start(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.set_node_range(t.id, start=50, end=5)
        assert not r.ok
        assert r.error_code == ERR_VALUE_OUT_OF_RANGE

    def test_set_node_range_partial_update(self):
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        # Set an explicit range first.
        cmds.set_node_range(t.id, start=5, end=50)
        # Then only update end.
        cmds.set_node_range(t.id, end=80)
        tr = t.time_range
        assert tr["comp_start"] == 5
        assert tr["comp_end"] == 80


# ── T65: track lock ────────────────────────────────────────────────────


class TestTrackLock:
    def test_locked_track_blocks_every_command(self):
        """Lock is enforced at the service layer: mouse, keyboard, AI and
        script all hit the same check (plan §5.4 / §19.2)."""
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        cmds.set_track_lock(t.id, "rotation", True)
        for r in (
            cmds.add_keyframe(t.id, "rotation", 5, 1.0),
            cmds.remove_keyframe(t.id, "rotation", 0),
            cmds.set_interpolation(t.id, "rotation", 0, "hold"),
            cmds.set_tangent_mode(t.id, "rotation", 0, "flat"),
            cmds.set_bezier_handles(t.id, "rotation", 0,
                                    handle_left=(-1.0, 0.0)),
            cmds.set_expression(t.id, "rotation", "value"),
            cmds.clear_track(t.id, "rotation"),
        ):
            assert not r.ok, f"command should be blocked by lock: {r}"
            assert r.error_code == ERR_LOCKED
        # Graph untouched.
        assert len(_track_frames(t, "rotation")) == 3

    def test_unlock_allows_edits_again(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        cmds.set_track_lock(t.id, "rotation", True)
        assert not cmds.add_keyframe(t.id, "rotation", 5, 1.0).ok
        cmds.set_track_lock(t.id, "rotation", False)
        assert cmds.add_keyframe(t.id, "rotation", 5, 1.0).ok

    def test_lock_is_session_only(self):
        """LockRegistry is plain in-memory state — never serialised."""
        reg = TrackLockRegistry()
        reg.set_locked("a", "x", True)
        assert reg.is_locked("a", "x")
        reg.clear()
        assert not reg.is_locked("a", "x")


# ── T15: error code stability ──────────────────────────────────────────


class TestErrorCodes:
    def test_param_not_found_stable_code(self):
        g, t, b = _graph_with_rotation()
        cmds = TimelineCommandBuilder(FakeHost(g))
        r = cmds.add_keyframe(t.id, "no_such_param", 5, 1.0)
        assert r.error_code == ERR_PARAM_NOT_FOUND

    def test_result_to_dict_round_trip(self):
        g, t, b = _graph_with_rotation()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        r = cmds.add_keyframe(t.id, "rotation", 30, 100.0)
        d = r.to_dict()
        assert d["applied"] is True
        assert d["error_code"] == ERR_OK
        assert len(d["affected_tracks"]) == 1
        assert "before" in d["affected_tracks"][0]
        assert "after" in d["affected_tracks"][0]

    def test_all_error_codes_exported(self):
        # Importing the module surface-checks the error-code vocabulary is
        # complete and stable (T15 contract).
        from src.features.composite.ui import timeline_commands as tc
        for code in (
            "ERR_OK", "ERR_NODE_NOT_FOUND", "ERR_PARAM_NOT_FOUND",
            "ERR_NOT_ANIMATABLE", "ERR_KEYFRAME_NOT_FOUND",
            "ERR_FRAME_INVALID", "ERR_VALUE_OUT_OF_RANGE",
            "ERR_INTERP_INVALID", "ERR_TANGENT_INVALID",
            "ERR_EXTRAP_INVALID", "ERR_HANDLE_INVALID", "ERR_LOCKED",
            "ERR_EXPR_INVALID", "ERR_EXPR_NOT_ALLOWED",
            "ERR_PASTE_TYPE_MISMATCH", "ERR_PLAN_FAILED", "ERR_NO_TARGET",
        ):
            assert hasattr(tc, code), f"missing error code: {code}"
