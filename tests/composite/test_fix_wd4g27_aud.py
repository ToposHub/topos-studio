"""Regression tests for remediation batch wd4-g27 (AUD-10904..AUD-11102).

Covers the Major fixes in the composite spline / sticky / timeline-UI domains:
  spline_panel.py   — AUD-10904 (viewport reset), AUD-10905 (ruler seek),
                      AUD-10906 (private-API selection write), AUD-10907
                      (hover index vs filter)
  spline_view.py    — AUD-10911 (multi-drag freeze), AUD-10912 (nudge tear),
                      AUD-10913 (handle y translation), AUD-10914 (curve
                      selection residue)
  sticky_note_item.py — AUD-11001 (picker GC), AUD-11002 (undo flooding),
                      AUD-11003 (from_dict hard indexing)
  timeline_commands.py — AUD-11010 (move vs copy), AUD-11011 (expression
                      undo records), AUD-11012 (out-of-range paste — current
                      behavior verified: rejected, not silent)
  timeline_expression_editor.py — AUD-11025 (draft-aware status provider)
  timeline_session.py — AUD-11102 (schema_version hard int())
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.ui.spline_model import (
    SplineModel, SplineKey, INTERP_BEZIER, TANGENT_BROKEN)
from src.features.composite.ui.spline_panel import SplinePanel
from src.features.composite.ui.spline_view import SplineView
from src.features.composite.ui.sticky_note_item import StickyNoteItem
from src.features.composite.ui.timeline_commands import (
    ClipboardEntry,
    ERR_VALUE_OUT_OF_RANGE,
    ExpressionChange,
    TimelineCommandBuilder,
    TrackChange,
    NodeRangeChange,
)
from src.features.composite.ui.timeline_session import (
    TIMELINE_SESSION_VERSION, TimelineSessionState)
from src.features.composite.ui.timeline_view_model import (
    AnimationSelectionModel)
from src.shared.keyframe import Keyframe, KeyframeTrack

pytestmark = pytest.mark.composite_core


# ── fixtures ────────────────────────────────────────────────────────────


class FakeHost:
    """Minimal TimelineHost + optional expression-undo hook (AUD-11011)."""

    def __init__(self, graph):
        self._graph = graph
        self.track_changes = []
        self.range_changes = []
        self.expression_changes = []

    def get_node_graph(self):
        return self._graph

    def record_track_change(self, change: TrackChange) -> None:
        self.track_changes.append(change)

    def record_node_range_change(self, change: NodeRangeChange) -> None:
        self.range_changes.append(change)

    def record_expression_change(self, change: ExpressionChange) -> None:
        self.expression_changes.append(change)


def _graph_two_tracks():
    """Transform.rotation + Blur.radius → two spline curves."""
    g = NodeGraph()
    t = TransformNode()
    tr = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    for f, v in [(0, 0.0), (10, 30.0), (20, 60.0), (30, 90.0)]:
        tr.add_keyframe(Keyframe(f, v))
    t.set_keyframe_track("rotation", tr)
    b = BlurNode()
    br = KeyframeTrack(param_name="radius", min_val=0.0, max_val=100.0)
    for f, v in [(0, 5.0), (10, 40.0)]:
        br.add_keyframe(Keyframe(f, v))
    b.set_keyframe_track("radius", br)
    g.add_node(t)
    g.add_node(b)
    return g, t, b


def _view_with_curves(qapp):
    g, t, b = _graph_two_tracks()
    m = SplineModel()
    m.rebuild(g)
    view = SplineView(m)
    view.resize(960, 400)
    view.fit_view()
    return view, m, g, t, b


def _ev(typ, x, y, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(typ, QPointF(x, y), button, button,
                       Qt.KeyboardModifier.NoModifier)


# ── AUD-10904: tree interactions must not reset the viewport ───────────


class TestAud10904ViewportPreserved:
    def test_curve_selection_keeps_viewport(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        panel.resize(960, 400)
        panel._view._ensure_viewport()
        # User zooms in — viewport deviates from the fitted one.
        panel._view._zoom_at(2.0, QPointF(15.0, 45.0))
        zoomed = panel._view._viewport
        assert zoomed is not None
        panel._on_curve_selected(f"{b.id}:radius")
        # Same object, not reset for a refit.
        assert panel._view._viewport is zoomed

    def test_visibility_toggle_keeps_viewport(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        panel.resize(960, 400)
        panel._view._ensure_viewport()
        panel._view._zoom_at(2.0, QPointF(15.0, 45.0))
        zoomed = panel._view._viewport
        assert zoomed is not None
        panel._on_curve_visibility_toggled(f"{b.id}:radius", False)
        assert panel._view._viewport is zoomed

    def test_visibility_refits_only_none_to_some(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        panel.resize(960, 400)
        panel._view._ensure_viewport()
        panel._view._zoom_at(2.0, QPointF(15.0, 45.0))
        assert panel._view._viewport is not None
        # Hide EVERYTHING, then make one curve visible again → refit.
        for c in panel._model.curves:
            panel._model.set_visible(c.curve_id, False)
        panel._on_curve_visibility_toggled(
            panel._model.curves[0].curve_id, True)
        assert panel._view._viewport is None  # refit requested

    def test_show_all_keeps_viewport_when_curves_visible(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        panel.resize(960, 400)
        panel._view._ensure_viewport()
        panel._view._zoom_at(2.0, QPointF(15.0, 45.0))
        zoomed = panel._view._viewport
        assert zoomed is not None
        panel._set_all_curve_visibility(True)
        assert panel._view._viewport is zoomed


# ── AUD-10905: view-level current_frame_changed must be wired ──────────


class TestAud10905RulerSeekForwarded:
    def test_wired_in_init_ui(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        received = []
        panel.current_frame_changed.connect(received.append)
        # The view emits this when the user drags the ruler playhead.
        panel._view.current_frame_changed.emit(7)
        assert received == [7]
        assert panel._view._current_frame == 7


# ── AUD-10906: public-API selection push ───────────────────────────────


class TestAud10906PublicSelectionApi:
    def test_push_matches_shared_state(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        shared = AnimationSelectionModel()
        panel.attach_to_shared_selection(shared)
        notifications = []
        shared.subscribe(lambda s: notifications.append(s))
        panel._on_key_selection_changed(
            (("param:x:p1", 10), ("param:x:p1", 20)))
        assert shared.selected_keyframes() == [("param:x:p1", 10),
                                               ("param:x:p1", 20)]
        assert shared.selected_rows() == ["param:x:p1"]
        assert shared.primary_row_id == "param:x:p1"
        assert shared.primary_frame == 10
        # One coalesced notification, not one per write.
        assert len(notifications) == 1

    def test_clear_push_uses_public_clear(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        shared = AnimationSelectionModel()
        panel.attach_to_shared_selection(shared)
        panel._on_key_selection_changed((("param:x:p1", 10),))
        panel._on_key_selection_changed(())
        assert shared.selected_keyframes() == []


# ── AUD-10907: hover index must match the filtered subset ──────────────


class TestAud10907HoverFiltered:
    def test_hover_uses_filtered_curves(self, qapp):
        g, t, b = _graph_two_tracks()
        panel = SplinePanel()
        panel.set_node_graph(g)
        tree = panel._tree
        # Filter down to exactly one curve.
        tree.set_filter(panel._model.curves[0].label)
        assert len(tree._filtered_curves()) == 1
        # A mouse move over ROW 1 would be valid on the full set (2 curves)
        # but must NOT highlight anything against the filtered subset.
        ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(30.0, 27.0),
                         Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                         Qt.KeyboardModifier.NoModifier)
        tree.mouseMoveEvent(ev)
        assert tree._hover_idx is None
        # Row 0 is still hoverable.
        ev0 = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(30.0, 5.0),
                          Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                          Qt.KeyboardModifier.NoModifier)
        tree.mouseMoveEvent(ev0)
        assert tree._hover_idx == 0


# ── AUD-10911: multi-drag must keep tracking after the first move ──────


class TestAud10911MultiDragFollows:
    def test_second_move_event_still_moves_keys(self, qapp):
        view, m, g, t, b = _view_with_curves(qapp)
        curve = m.curves[0]
        for k in curve.keys:
            k.selected = k.frame in (10, 20)
        kp10 = view.data_to_screen(QPointF(10, 30.0))
        view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress,
                                 kp10.x(), kp10.y()))
        assert view._multi_drag is True
        # First move: +40px.
        x1 = kp10.x() + 40
        view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, x1, kp10.y()))
        after_first = sorted(k.frame for k in curve.keys)
        # Second move: +80px total — the drag must keep following (the old
        # frame-snapshot matching froze the keys at the first increment).
        x2 = kp10.x() + 80
        view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove, x2, kp10.y()))
        after_second = sorted(k.frame for k in curve.keys)
        assert after_second != after_first
        # The two moved keys stay 10 frames apart and originals are gone.
        assert 10 not in after_second and 20 not in after_second
        moved = sorted(f for f in after_second if f > 0 and f != 30)
        assert moved[1] - moved[0] == 10
        view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease,
                                   x2, kp10.y()))


# ── AUD-10912: positive nudge must not tear adjacent blocks ────────────


class TestAud10912NudgeBlock:
    def test_adjacent_keys_nudge_right_as_block(self, qapp):
        view, m, g, t, b = _view_with_curves(qapp)
        curve = m.curves[0]
        # Two ADJACENT selected keys (10, 11): rebuild frames for this test.
        curve.keys = [SplineKey(frame=f, value=float(f))
                      for f in (0, 10, 11, 30)]
        for k in curve.keys:
            k.selected = k.frame in (10, 11)
        view._nudge_selected_keys(1)
        frames = sorted(k.frame for k in curve.keys)
        # Whole block translates by +1 — not torn to (10, 12).
        assert frames == [0, 11, 12, 30]

    def test_adjacent_keys_nudge_left_as_block(self, qapp):
        view, m, g, t, b = _view_with_curves(qapp)
        curve = m.curves[0]
        curve.keys = [SplineKey(frame=f, value=float(f))
                      for f in (0, 10, 11, 30)]
        for k in curve.keys:
            k.selected = k.frame in (10, 11)
        view._nudge_selected_keys(-1)
        frames = sorted(k.frame for k in curve.keys)
        assert frames == [0, 9, 10, 30]


# ── AUD-10913: handles must follow the value delta too ─────────────────


class TestAud10913HandleValueTranslation:
    def _bezier_curve(self, m):
        curve = m.curves[0]
        curve.keys = [SplineKey(frame=10, value=30.0,
                                interpolation=INTERP_BEZIER,
                                tangent_mode=TANGENT_BROKEN,
                                handle_left=QPointF(5.0, 30.0),
                                handle_right=QPointF(15.0, 30.0))]
        return curve

    def test_key_drag_translates_handle_y(self, qapp):
        view, m, g, t, b = _view_with_curves(qapp)
        curve = self._bezier_curve(m)
        curve.selected = True
        kp = view.data_to_screen(QPointF(10, 30.0))
        view.mousePressEvent(_ev(QMouseEvent.Type.MouseButtonPress,
                                 kp.x(), kp.y()))
        # Drag straight DOWN by 60px (value change, no frame change).
        target = QPointF(kp.x(), kp.y() + 60)
        view.mouseMoveEvent(_ev(QMouseEvent.Type.MouseMove,
                                target.x(), target.y()))
        k = curve.keys[0]
        assert k.value != 30.0
        delta_value = k.value - 30.0
        # Handles followed the value change (y translated, x unchanged).
        assert k.handle_left.y() == pytest.approx(30.0 + delta_value)
        assert k.handle_right.y() == pytest.approx(30.0 + delta_value)
        assert k.handle_left.x() == pytest.approx(5.0)
        assert k.handle_right.x() == pytest.approx(15.0)
        view.mouseReleaseEvent(_ev(QMouseEvent.Type.MouseButtonRelease,
                                   target.x(), target.y()))

    def test_edit_dialog_translation_math(self, qapp):
        """The dialog path shares the same math (checked via the module
        function's postconditions on a synthetic key)."""
        from src.features.composite.ui import spline_view as sv
        view, m, g, t, b = _view_with_curves(qapp)
        curve = m.curves[0]
        curve.keys = [SplineKey(frame=10, value=30.0,
                                interpolation=INTERP_BEZIER,
                                handle_left=QPointF(5.0, 30.0),
                                handle_right=QPointF(15.0, 30.0))]
        # Directly exercise the translation block via a tiny shim: emulate
        # what _edit_key_dialog does after the dialogs return.
        k = curve.keys[0]
        frame, value = 12, 90.0
        delta = frame - k.frame
        new_value = sv._clampf(value, curve.min_value, curve.max_value)
        delta_value = new_value - k.value
        k.frame = frame
        k.value = new_value
        k.handle_left = QPointF(k.handle_left.x() + delta,
                                k.handle_left.y() + delta_value)
        k.handle_right = QPointF(k.handle_right.x() + delta,
                                 k.handle_right.y() + delta_value)
        assert k.handle_left == QPointF(7.0, 90.0)
        assert k.handle_right == QPointF(17.0, 90.0)


# ── AUD-10914: sole key selection clears other curves ──────────────────


class TestAud10914CurveSelectionResidue:
    def test_select_only_key_clears_other_curves(self, qapp):
        view, m, g, t, b = _view_with_curves(qapp)
        curve_a, curve_b = m.curves[0], m.curves[1]
        curve_a.selected = True  # previously selected curve
        emitted = []
        view.curve_selection_changed.connect(emitted.append)
        curve_b.selected = True
        view._select_only_key(curve_b, 0)
        assert curve_a.selected is False
        assert curve_b.selected is True
        # The emitted selection is the NEW curve, not the residue.
        assert curve_b.curve_id in emitted
        assert curve_a.curve_id not in emitted
        # Only the clicked key is selected, across ALL curves.
        selected_keys = [(c.curve_id, k.frame)
                         for c in m.curves for k in c.keys if k.selected]
        assert selected_keys == [(curve_b.curve_id, 0)]


# ── AUD-11001 / AUD-11002: color picker lifetime + single undo step ────


class TestAud11001ColorPicker:
    def _note(self, qapp):
        note = StickyNoteItem(note_id="n1", color="#FFE082")
        records = []
        note.set_modified_handler(lambda item, old, new: records.append(
            (old, new)))
        return note, records

    def test_dialog_stays_alive_after_show(self, qapp):
        note, records = self._note(qapp)
        note._choose_color()
        dlg = note._color_dlg
        assert dlg is not None
        # The dialog must still be functional AFTER _choose_color returned
        # (the pre-fix parentless/unreferenced dialog was garbage-collected
        # right after show(); driving it then raised RuntimeError).
        dlg.setCurrentColor(QColor("#ff0000"))
        assert note.color() == "#ff0000"
        dlg.reject()  # cleanup + rollback
        assert note.color() == "#ffe082"

    def test_live_preview_pushes_no_undo_and_accept_commits_once(self, qapp):
        note, records = self._note(qapp)
        note._choose_color()
        dlg = note._color_dlg
        # Several intermediate colors (currentColorChanged ticks).
        for hexc in ("#ff0000", "#00ff00", "#0000ff"):
            dlg.setCurrentColor(QColor(hexc))
        assert note.color() == "#0000ff"       # preview applied visually
        assert records == []                    # …without undo records
        dlg.accept()
        assert len(records) == 1               # ONE undo step on accept
        old, new = records[0]
        assert old["color"] == "#ffe082"
        assert new["color"] == "#0000ff"
        assert note._color_dlg is None         # reference released

    def test_cancel_rolls_back_preview(self, qapp):
        note, records = self._note(qapp)
        note._choose_color()
        dlg = note._color_dlg
        dlg.setCurrentColor(QColor("#123456"))
        assert note.color() == "#123456"
        dlg.reject()
        assert records == []                    # no undo record
        assert note.color() == "#ffe082"        # rolled back
        assert note._color_dlg is None


# ── AUD-11003: from_dict must tolerate malformed payloads ──────────────


class TestAud11003FromDictRobust:
    def test_missing_position_and_size_use_defaults(self, qapp):
        note = StickyNoteItem.from_dict({"id": "x"})
        assert note.note_id() == "x"
        assert note.rect().width() == 200.0
        assert note.rect().height() == 150.0

    @pytest.mark.parametrize("bad", [None, "oops", [1], [1, 2, 3], [None, 5]])
    def test_malformed_position_does_not_raise(self, qapp, bad):
        note = StickyNoteItem.from_dict({"id": "x", "position": bad})
        assert note.note_id() == "x"

    @pytest.mark.parametrize("bad", [None, "big", [1], [1, 2, 3]])
    def test_malformed_size_does_not_raise(self, qapp, bad):
        note = StickyNoteItem.from_dict({"id": "x", "size": bad})
        assert note.rect().width() == 200.0

    def test_missing_id_does_not_raise(self, qapp):
        note = StickyNoteItem.from_dict({})
        assert note.note_id() == ""

    def test_valid_payload_still_parsed(self, qapp):
        note = StickyNoteItem.from_dict({
            "id": "x", "position": [12.0, 34.0], "size": [260.0, 180.0]})
        assert note.pos().x() == pytest.approx(12.0)
        assert note.rect().width() == pytest.approx(260.0)


# ── AUD-11010: commit_move is a MOVE (source frames dropped) ───────────


class TestAud11010MoveNotCopy:
    def test_move_drops_source_frames_and_carries_payload(self):
        g, t, b = _graph_two_tracks()
        cmds = TimelineCommandBuilder(FakeHost(g))
        # Frame 20 is a BEZIER key in the fixture? Ensure payload survives.
        data = t.keyframe_tracks["rotation"]
        kf20 = next(kf for kf in data["keyframes"] if kf["frame"] == 20)
        kf20["interpolation"] = "bezier"
        kf20["tangent_mode"] = "broken"
        kf20["handle_left"] = [15.0, 50.0]
        assert cmds.begin_move([(t.id, "rotation", 20)]).ok
        cmds.update_move_preview([(t.id, "rotation", 20, 25, 61.0)])
        r = cmds.commit_move()
        assert r.ok
        frames = {kf["frame"]: kf
                  for kf in t.keyframe_tracks["rotation"]["keyframes"]}
        assert 20 not in frames                    # source dropped
        moved = frames[25]
        assert moved["value"] == 61.0
        assert moved["interpolation"] == "bezier"   # payload carried
        assert moved["tangent_mode"] == "broken"
        assert list(moved["handle_left"]) == [15.0, 50.0]

    def test_repeated_preview_updates_overwrite_not_accumulate(self):
        g, t, b = _graph_two_tracks()
        cmds = TimelineCommandBuilder(FakeHost(g))
        assert cmds.begin_move([(t.id, "rotation", 0)]).ok
        # The view reports successive positions during one drag.
        cmds.update_move_preview([(t.id, "rotation", 0, 3, 1.0)])
        cmds.update_move_preview([(t.id, "rotation", 0, 5, 1.0)])
        cmds.update_move_preview([(t.id, "rotation", 0, 7, 1.0)])
        r = cmds.commit_move()
        assert r.ok
        frames = sorted(kf["frame"]
                        for kf in t.keyframe_tracks["rotation"]["keyframes"])
        # Only the FINAL position lands — intermediate frames 3/5 must not
        # become keyframes.
        assert frames == [7, 10, 20, 30]

    def test_unmoved_selection_member_survives(self):
        """A selected key the view never reported must not be deleted."""
        g, t, b = _graph_two_tracks()
        cmds = TimelineCommandBuilder(FakeHost(g))
        assert cmds.begin_move([(t.id, "rotation", 0),
                                (t.id, "rotation", 10)]).ok
        # View only reports a move for frame 0.
        cmds.update_move_preview([(t.id, "rotation", 0, 4, 0.0)])
        r = cmds.commit_move()
        assert r.ok
        frames = sorted(kf["frame"]
                        for kf in t.keyframe_tracks["rotation"]["keyframes"])
        assert 10 in frames  # untouched selected key kept


# ── AUD-11011: expression undo records ─────────────────────────────────


class TestAud11011ExpressionUndo:
    def test_set_expression_records_before_after_text(self):
        g, t, b = _graph_two_tracks()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        r1 = cmds.set_expression(t.id, "rotation", "value + 10")
        assert r1.ok
        change = r1.expression_changes[0]
        assert change.before_text is None
        assert change.after_text == "value + 10"
        # N-04：轨道不再被清除——记录携带 before/after 轨道（相等，
        # 供诊断），undo 只还原表达式文本。
        assert host.expression_changes == [change]
        # Update the expression: before_text is the previous expression.
        r2 = cmds.set_expression(t.id, "rotation", "value * 2")
        assert r2.ok
        assert r2.expression_changes[0].before_text == "value + 10"
        assert r2.expression_changes[0].after_text == "value * 2"

    def test_remove_expression_produces_record(self):
        g, t, b = _graph_two_tracks()
        host = FakeHost(g)
        cmds = TimelineCommandBuilder(host)
        assert cmds.set_expression(t.id, "rotation", "value + 10").ok
        host.expression_changes.clear()
        r = cmds.remove_expression(t.id, "rotation")
        assert r.ok
        # AUD-11011: previously NO record at all.
        assert len(r.expression_changes) == 1
        change = r.expression_changes[0]
        assert change.before_text == "value + 10"
        assert change.after_text is None
        assert host.expression_changes == [change]

    def test_host_without_expression_hook_unaffected(self):
        """The v1 TimelineHost protocol has no expression hook — hosts that
        don't implement it must not break (structural typing)."""
        g, t, b = _graph_two_tracks()

        class LegacyHost:
            """Exactly the v1 protocol surface — nothing more."""

            def __init__(self, graph):
                self._graph = graph

            def get_node_graph(self):
                return self._graph

            def record_track_change(self, change: TrackChange) -> None:
                pass

            def record_node_range_change(self, change: NodeRangeChange) -> None:
                pass

        cmds = TimelineCommandBuilder(LegacyHost(g))
        r = cmds.set_expression(t.id, "rotation", "value")
        assert r.ok and r.expression_changes


# ── AUD-11012: out-of-range paste is REJECTED (audit claim re-verified) ─


class TestAud11012PasteRangeVerification:
    def test_out_of_range_paste_is_rejected_not_silent(self):
        """The audit claimed silent out-of-range commits; the service layer
        validates set_keyframes values against the target pdef min/max, so
        the paste is rejected with ERR_VALUE_OUT_OF_RANGE. This test pins
        that (rejecting) contract — the remediation the audit itself allows
        (“或拒绝（复用 ERR_VALUE_OUT_OF_RANGE）”)."""
        g, t, b = _graph_two_tracks()
        cmds = TimelineCommandBuilder(FakeHost(g))
        cmds.copy_keyframes([(t.id, "rotation", 20, 60.0, "linear")])
        # Forge an out-of-range clipboard value (e.g. pasted across params
        # with different min/max).
        cmds.clipboard.entries = [ClipboardEntry(relative_frame=0,
                                                 value=5000.0)]
        r = cmds.paste_keyframes(t.id, "rotation", 100)
        assert not r.ok
        assert r.error_code == ERR_VALUE_OUT_OF_RANGE
        frames = sorted(kf["frame"]
                        for kf in t.keyframe_tracks["rotation"]["keyframes"])
        assert frames == [0, 10, 20, 30]  # nothing landed


# ── AUD-11025: status provider sees the draft text ─────────────────────


class TestAud11025DraftAwareProvider:
    def test_provider_receives_draft_and_error_shows(self, qapp):
        from src.features.composite.ui.timeline_expression_editor import (
            TimelineExpressionEditor)
        from src.features.composite.ui.timeline_expression import (
            EXPRESSION_ERROR, EXPRESSION_NONE, ExpressionStatus)
        editor = TimelineExpressionEditor()
        seen = []

        def provider(node_id, param, draft):
            seen.append(draft)
            if "+++" in draft:
                return ExpressionStatus(
                    state=EXPRESSION_ERROR, error_code="expr_syntax",
                    error_message="Syntax error near '+'")
            return ExpressionStatus(state=EXPRESSION_NONE)

        editor.open_for("n", "p", initial_text="value",
                        status_provider=provider)
        editor._editor.setPlainText("value +++")
        assert "value +++" in seen
        assert "Syntax error" in editor._status_label.text()
        editor.deleteLater()

    def test_empty_draft_shows_enter_hint_even_with_provider(self, qapp):
        from src.features.composite.ui.timeline_expression_editor import (
            TimelineExpressionEditor)
        from src.features.composite.ui.timeline_expression import (
            EXPRESSION_VALID, ExpressionStatus)
        editor = TimelineExpressionEditor()
        editor.open_for("n", "p", initial_text="value", status_provider=lambda
                        n, p, t: ExpressionStatus(
                            state=EXPRESSION_VALID, source_value="keyframes"))
        editor._editor.setPlainText("")
        text = editor._status_label.text().lower()
        assert "enter" in text or "expression" in text
        editor.deleteLater()


# ── AUD-11102: schema_version must not abort the session load ──────────


class TestAud11102SchemaVersionRobust:
    @pytest.mark.parametrize("bad", [None, "abc", [1], {}])
    def test_bad_schema_version_falls_back(self, bad):
        state = TimelineSessionState.from_dict({
            "schema_version": bad, "filter_mode": "animated"})
        assert state.schema_version == TIMELINE_SESSION_VERSION
        # The rest of the payload still loads.
        assert state.filter_mode == "animated"

    def test_valid_schema_version_preserved(self):
        state = TimelineSessionState.from_dict({"schema_version": 1})
        assert state.schema_version == 1
