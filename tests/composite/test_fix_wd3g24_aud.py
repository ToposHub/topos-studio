"""Regression tests for remediation batch D, wave 3, group 24 (wd3-g24).

Covers the 16 Major findings in the composite keyframe-editor domain:
AUD-10001/10002/10003/10007, AUD-10100/10101/10102, AUD-10200..10204,
AUD-10301/10302, AUD-10401/10402.

All tests are headless (offscreen QApplication via the ``qapp`` fixture);
no OpenGL and no full CompositePage is required.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest
from PyQt6.QtCore import QEvent, QObject, QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.base_node import ParamDef
from src.features.composite.nodes.color_node import COLORNode
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframe_editor import KeyframeEditorWidget
from src.features.composite.ui import keyframes_model as km
from src.features.composite.ui.keyframes_model import (
    FrameMapper, KeyframesModel, KEYFRAMES_RULER_H, KEYFRAMES_ROW_H,
)
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_timeline_view import (
    KeyframesTimelineView,
)
from src.features.composite.ui.keyframes_tree_view import KeyframesTreeView
from src.features.composite.ui.node_editor_actions import NodeEditorActions


# ── shared fakes ───────────────────────────────────────────────────────


class _FakeNode:
    """Minimal duck-typed node driving KeyframesModel projections."""

    def __init__(self, node_id: str = "n1",
                 tracks: Optional[Dict[str, Any]] = None) -> None:
        self.id = node_id
        self.node_type = "test"
        self.node_title = "Test Node"
        self.display_title = "Test Node"
        self.header_color = "#555555"
        self.position = (0.0, 0.0)
        self.parameters: Dict[str, Any] = {}
        self.keyframe_tracks: Dict[str, Any] = tracks or {}

    def effective_header_color(self) -> str:
        return self.header_color

    def get_param_defs(self) -> Dict[str, ParamDef]:
        return {}


class _FakeGraph:
    def __init__(self, nodes: List[_FakeNode]) -> None:
        self.nodes: Dict[str, _FakeNode] = {n.id: n for n in nodes}
        self.connections: List[Any] = []

    def get_node(self, node_id: str) -> Optional[_FakeNode]:
        return self.nodes.get(node_id)


def _built_model(node: _FakeNode) -> KeyframesModel:
    model = KeyframesModel()
    model.set_comp_range(0, 99)
    model.rebuild(_FakeGraph([node]))
    return model


def _param_track(node: _FakeNode, frames: List[int]) -> None:
    node.keyframe_tracks["opacity"] = {
        "keyframes": [
            {"frame": f, "value": 0.1 * f, "interpolation": "linear"}
            for f in frames
        ]
    }


def _select_frames(model: KeyframesModel, row_id: str,
                   frames: List[int]) -> None:
    track = model.track_for(row_id)
    assert track is not None
    for kf in track.keyframes:
        kf.selected = kf.frame in frames


def _param_row_id(node_id: str) -> str:
    return f"param:{node_id}:opacity"


class _Spy(QObject):
    """Signal recorder: connect(spy) then inspect .calls."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: List[Tuple[Any, ...]] = []

    def __call__(self, *args: Any) -> None:
        self.calls.append(args)


# ── AUD-10001: selection-driven programmatic updates must not recommit ─


def _make_editor(animated: bool = True,
                 keyframes: Optional[List[Dict[str, Any]]] = None,
                 name: str = "x") -> KeyframeEditorWidget:
    pdef = ParamDef(
        name=name, ptype="float", default=0.5, min_val=0.0, max_val=1.0,
        decimals=3, animated=animated,
        keyframes=keyframes if keyframes is not None else [
            {"frame": 0, "value": 0.0, "interpolation": "linear"},
            {"frame": 10, "value": 1.0, "interpolation": "linear"},
        ])
    editor = KeyframeEditorWidget()
    editor.set_parameter(name, pdef, total_frames=20, current_frame=0)
    return editor


@pytest.mark.usefixtures("qapp")
def test_aud_10001_selection_update_does_not_recommit():
    editor = _make_editor()
    spy = _Spy()
    editor.keyframes_changed.connect(spy)

    editor._on_keyframe_selected(10)

    assert editor._frame_spin.value() == 10
    assert editor._value_spin.value() == pytest.approx(1.0)
    # The programmatic setValue/setCurrentIndex must not re-enter the edit
    # handlers and emit a redundant keyframes_changed commit.
    assert spy.calls == []


@pytest.mark.usefixtures("qapp")
def test_aud_10002_set_parameter_does_not_toggle_with_stale_track():
    # First parameter: animated → checkbox is checked.
    editor = _make_editor(animated=True, name="x")
    assert editor._anim_checkbox.isChecked() is True

    # Rebinding to a second, NON-animated parameter fires setChecked(True→
    # False). Unguarded, _on_animation_toggled ran with the OLD parameter's
    # track + the NEW param name: it snapshotted the old track into
    # _disabled_backup and emitted a spurious empty commit for "y".
    spy = _Spy()
    editor.keyframes_changed.connect(spy)
    pdef2 = ParamDef(name="y", ptype="float", default=0.25,
                     min_val=0.0, max_val=1.0, animated=False)
    editor.set_parameter("y", pdef2, total_frames=20, current_frame=0)

    assert spy.calls == []
    assert editor._is_animated is False
    assert editor._param_name == "y"
    # S5 contract preserved: the new parameter starts without a stale backup
    # captured from the previously edited parameter.
    assert editor._disabled_backup is None


@pytest.mark.usefixtures("qapp")
def test_aud_10003_frame_spin_collision_clamp_and_selection_sync():
    editor = _make_editor(keyframes=[
        {"frame": 0, "value": 0.0, "interpolation": "linear"},
        {"frame": 10, "value": 1.0, "interpolation": "linear"},
    ])
    spy = _Spy()
    editor.keyframes_changed.connect(spy)

    editor._timeline_widget._selected_frame = 0
    # Typing 10 collides with the existing keyframe at 10 → nearest free.
    editor._on_frame_spin_changed(10)

    frames = sorted(kf.frame for kf in editor._track.keyframes)
    assert frames == [10, 11]  # selected kf 0 → nearest free frame, no merge
    assert editor._timeline_widget._selected_frame == 11  # selection follows
    assert editor._frame_spin.value() == 11  # spinbox reflects destination
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "x"


@pytest.mark.usefixtures("qapp")
def test_aud_10003_frame_spin_clamps_to_total_frames():
    editor = _make_editor()
    editor._timeline_widget._selected_frame = 0

    editor._on_frame_spin_changed(5000)  # far past the 20-frame comp

    frames = sorted(kf.frame for kf in editor._track.keyframes)
    assert frames == [10, 19]  # clamped to total_frames - 1


@pytest.mark.usefixtures("qapp")
def test_aud_10003_frame_spin_range_follows_timeline():
    editor = _make_editor()
    assert editor._frame_spin.maximum() == 19  # total_frames(20) - 1
    editor.set_total_frames(50)
    assert editor._frame_spin.maximum() == 49


# ── AUD-10007: no broad except Exception left in keyframes_model ───────


def test_aud_10007_no_broad_except_in_keyframes_model():
    import inspect
    src = inspect.getsource(km)
    assert "except Exception" not in src


def test_aud_10007_ordered_nodes_tolerates_shape_errors():
    class _Broken:
        @property
        def nodes(self):
            raise TypeError("bad shape")

    assert km.KeyframesModel._ordered_nodes(_Broken()) == []


# ── AUD-10100: narrowed excepts in keyframes_panel ─────────────────────


@pytest.mark.usefixtures("qapp")
def test_aud_10100_display_value_no_longer_masks_domain_errors():
    panel = KeyframesPanel()

    class _RaisingNode:
        def get_param_defs(self):
            raise RuntimeError("domain error")

    class _Graph:
        def get_node(self, _nid):
            return _RaisingNode()

    panel._node_graph = _Graph()
    # Unexpected domain errors must propagate instead of being swallowed by
    # a blanket ``except Exception`` (the caller's own guards decide).
    with pytest.raises(RuntimeError):
        panel._compute_display_value("n", "p")
    panel.dispose()


# ── AUD-10101: playback controller rebinding disconnects the old one ──


class _Controller(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.toggles = 0

    def keyframes_panel_play_toggle(self) -> None:
        self.toggles += 1


@pytest.mark.usefixtures("qapp")
def test_aud_10101_rebind_and_clear_disconnect_previous():
    panel = KeyframesPanel()
    first, second = _Controller(), _Controller()
    panel.set_playback_controller(first)
    panel.set_playback_controller(second)

    panel.play_toggle_requested.emit(False)
    assert first.toggles == 0
    assert second.toggles == 1

    panel.set_playback_controller(None)
    panel.play_toggle_requested.emit(False)
    assert second.toggles == 1  # cleared → nothing fires
    panel.dispose()


# ── AUD-10102: display-value memoization ───────────────────────────────


class _CountingNode:
    def __init__(self) -> None:
        self.lookups = 0
        self.parameters: Dict[str, Any] = {"gain": 0.5}

    def get_param_defs(self) -> Dict[str, ParamDef]:
        self.lookups += 1
        return {"gain": ParamDef(name="gain", ptype="float", default=0.5,
                                 min_val=0.0, max_val=1.0)}


@pytest.mark.usefixtures("qapp")
def test_aud_10102_display_value_cached_per_frame():
    panel = KeyframesPanel()
    node = _CountingNode()

    class _Graph:
        def get_node(self, _nid):
            return node

    panel._node_graph = _Graph()
    panel._state.current_frame = 5

    first = panel._display_value_for("n", "gain")
    second = panel._display_value_for("n", "gain")
    assert first == second == "0.5"
    assert node.lookups == 1  # repeated calls reuse the memo

    panel._state.current_frame = 6  # new frame → fresh sample
    panel._display_value_for("n", "gain")
    assert node.lookups == 2

    panel._invalidate_display_values("n", "gain")  # param write → dropped
    panel._display_value_for("n", "gain")
    assert node.lookups == 3
    panel.dispose()


@pytest.mark.usefixtures("qapp")
def test_aud_10102_numeric_write_invalidates_cache():
    panel = KeyframesPanel()
    node = _CountingNode()

    class _Graph:
        def get_node(self, _nid):
            return node

    panel._node_graph = _Graph()
    panel._state.current_frame = 5
    assert panel._display_value_for("n", "gain") == "0.5"

    node.parameters["gain"] = 0.75
    panel._apply_property_numeric_value("n", "gain", 0.75)

    assert panel._display_value_for("n", "gain") == "0.75"
    panel.dispose()


# ── AUD-10200: plain range-bar click must not commit a range edit ─────


@pytest.mark.usefixtures("qapp")
def test_aud_10200_click_without_drag_commits_nothing():
    node = _FakeNode("n1")
    model = _built_model(node)
    row = next(r for r in model.rows if r.row_type == "node")
    track = model.track_for(row.row_id)
    assert track.range_bar is not None and track.range_bar.default

    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.range_changed.connect(spy)

    view._drag_mode = "range_body"
    view._drag_row_id = row.row_id
    view._range_press_bounds = (track.range_bar.start_frame,
                                track.range_bar.end_frame)
    view._commit_drag_edit()

    assert spy.calls == []  # no movement → no explicit time_range, no undo

    # A real drag that moves the bar still commits exactly once.
    track.range_bar.end_frame += 5
    view._drag_mode = "range_body"
    view._range_press_bounds = (track.range_bar.start_frame,
                                track.range_bar.end_frame - 5)
    view._commit_drag_edit()
    assert len(spy.calls) == 1
    assert spy.calls[0][1]["edit"] == "slide"


# ── AUD-10202: double-click add respects the lock; no redundant emits ──


@pytest.mark.usefixtures("qapp")
def test_aud_10202_set_keyframe_without_hint_does_not_reemit():
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)

    view.add_keyframe_at("n1", "opacity", 5)  # exists, nothing to set
    assert spy.calls == []
    assert len(model.track_for(_param_row_id("n1")).keyframes) == 1

    view.add_keyframe_at("n1", "opacity", 5, value_hint=0.9)  # real update
    assert len(spy.calls) == 1
    assert model.track_for(_param_row_id("n1")).keyframes[0].value == 0.9


@pytest.mark.usefixtures("qapp")
def test_aud_10202_double_click_skips_locked_rows(qapp):
    node = _FakeNode("n1")
    _param_track(node, [5, 10])
    model = _built_model(node)
    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)

    row_index = 1  # 0 = node row, 1 = param row
    y = KEYFRAMES_RULER_H + row_index * KEYFRAMES_ROW_H + KEYFRAMES_ROW_H / 2
    x = 5 * 4.0  # default FrameMapper: 4 px/frame, frame 5

    def _dbl_click(frame_x: float) -> None:
        event = QMouseEvent(
            QEvent.Type.MouseButtonDblClick, QPointF(frame_x, y),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier)
        view.mouseDoubleClickEvent(event)

    row = model.rows[row_index]
    row.locked = True
    _dbl_click(x)
    assert spy.calls == []  # P9: locked rows are not editable

    row.locked = False
    _dbl_click(x)
    assert len(model.track_for(_param_row_id("n1")).keyframes) == 2
    assert spy.calls == []  # existing diamond → no redundant re-emit

    _dbl_click(20 * 4.0)  # empty frame → genuinely new keyframe
    assert len(model.track_for(_param_row_id("n1")).keyframes) == 3
    assert len(spy.calls) == 1


# ── AUD-10203: Alt+click without drag must not duplicate in place ──────


@pytest.mark.usefixtures("qapp")
def test_aud_10203_alt_click_without_drag_keeps_invariant():
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    _select_frames(model, _param_row_id("n1"), [5])
    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)
    batch_spy = _Spy()
    view.track_batch_about_to_commit.connect(batch_spy)

    view._drag_copy = True
    view._start_multi_keyframe_drag(5 * 4.0, _param_row_id("n1"))

    track = model.track_for(_param_row_id("n1"))
    # Duplication is deferred: press alone leaves exactly one keyframe.
    assert [kf.frame for kf in track.keyframes] == [5]

    # Release without moving: no commit, no duplicate, no undo batch.
    view._commit_drag_edit()
    view._drag_mode = "none"
    view._drag_copy = False
    view._drag_copy_pending = False
    assert [kf.frame for kf in track.keyframes] == [5]
    assert spy.calls == []
    assert batch_spy.calls == []


@pytest.mark.usefixtures("qapp")
def test_aud_10203_alt_drag_still_duplicates_then_moves():
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    _select_frames(model, _param_row_id("n1"), [5])
    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)

    view._drag_copy = True
    view._start_multi_keyframe_drag(5 * 4.0, _param_row_id("n1"))
    # First real move materializes the copies and drags them to frame 8.
    view._do_multi_keyframe_drag(8 * 4.0)

    track = model.track_for(_param_row_id("n1"))
    frames = sorted(kf.frame for kf in track.keyframes)
    assert frames == [5, 8]  # original kept, copy moved — still unique
    assert [kf.frame for kf in track.keyframes if kf.selected] == [8]

    view._commit_drag_edit()
    view._drag_mode = "none"
    view._drag_copy = False
    view._drag_copy_pending = False
    assert len(spy.calls) == 1


# ── AUD-10204: hidden rows keep model-mutation and commit consistent ──


@pytest.mark.usefixtures("qapp")
def test_aud_10204_scale_on_collapsed_row_commits():
    node = _FakeNode("n1")
    _param_track(node, [10, 20])
    model = _built_model(node)
    _select_frames(model, _param_row_id("n1"), [10, 20])
    model.set_expanded("n1", False)  # collapse → param row invisible
    assert _param_row_id("n1") not in {r.row_id for r in model.visible_rows()}

    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)

    assert view.scale_selected_keyframes(2.0) is True
    # The edit is committed (not silently mutated away): one track_changed
    # with the scaled frames (pivot = first selected frame 10).
    assert len(spy.calls) == 1
    assert spy.calls[0][0] == "n1" and spy.calls[0][1] == "opacity"
    frames = sorted(kf["frame"] for kf in spy.calls[0][2])
    assert frames == [10, 30]
    # Visual model and commit agree.
    assert [kf.frame for kf in
            model.track_for(_param_row_id("n1")).keyframes] == [10, 30]


@pytest.mark.usefixtures("qapp")
def test_aud_10204_interpolation_on_collapsed_row_commits():
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    _select_frames(model, _param_row_id("n1"), [5])
    model.set_expanded("n1", False)  # collapse → param row invisible

    view = KeyframesTimelineView(model, FrameMapper())
    spy = _Spy()
    view.track_changed.connect(spy)

    assert view.set_selected_interpolation("hold") is True
    assert len(spy.calls) == 1
    assert spy.calls[0][2][0]["interpolation"] == "hold"
    assert model.track_for(_param_row_id("n1")).keyframes[
        0].interpolation == "hold"


# ── AUD-10301: Eye/Solo/Shy read+write the model's layer fields ───────


@pytest.mark.usefixtures("qapp")
def test_aud_10301_eye_click_toggles_layer_enabled(qapp):
    node = _FakeNode("n1")
    model = _built_model(node)
    tree = KeyframesTreeView(model, header_height=30)
    tree.resize(600, 200)
    row = model.rows[0]
    assert row.row_type == "node"
    row.layer_enabled = True

    eye_spy = _Spy()
    tree.eye_changed.connect(eye_spy)

    geom = tree._column_geometry()
    x = (geom["eye"][0] + geom["eye"][1]) / 2
    y = 30 + KEYFRAMES_ROW_H / 2
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    tree.mousePressEvent(event)

    assert row.layer_enabled is False  # real model field updated
    assert eye_spy.calls == [(row.node_id, False)]
    # The painted state reads the same field the click writes; the legacy
    # ``row.eye`` attribute is never created.
    assert getattr(row, "eye", "missing") == "missing"


# ── AUD-10302: provider guarded on interaction paths ──────────────────


def _raising_provider(_nid: str, _pn: str) -> str:
    raise RuntimeError("provider boom")


@pytest.mark.usefixtures("qapp")
def test_aud_10302_safe_value_text_guards_provider():
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    tree = KeyframesTreeView(model, header_height=30)
    tree.resize(600, 200)
    tree.set_value_provider(_raising_provider)

    assert tree._safe_value_text("n1", "opacity") is None


@pytest.mark.usefixtures("qapp")
def test_aud_10302_value_cell_click_survives_raising_provider(qapp):
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    tree = KeyframesTreeView(model, header_height=30)
    tree.resize(600, 200)
    tree.set_value_provider(_raising_provider)
    drag_spy = _Spy()
    tree.value_drag_started.connect(drag_spy)

    geom = tree._column_geometry()
    x = (geom["value"][0] + geom["value"][1]) / 2
    y = 30 + 1 * KEYFRAMES_ROW_H + KEYFRAMES_ROW_H / 2  # row 1 = param row
    event = QMouseEvent(
        QEvent.Type.MouseButtonPress, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    # Must not raise even though the host provider raises; the drag starts
    # from the numeric fallback base (provider yielded nothing).
    tree.mousePressEvent(event)
    assert drag_spy.calls and drag_spy.calls[0][:2] == ("n1", "opacity")


@pytest.mark.usefixtures("qapp")
def test_aud_10302_double_click_edit_survives_raising_provider(qapp):
    node = _FakeNode("n1")
    _param_track(node, [5])
    model = _built_model(node)
    tree = KeyframesTreeView(model, header_height=30)
    tree.resize(600, 200)
    tree.set_value_provider(_raising_provider)

    geom = tree._column_geometry()
    x = (geom["value"][0] + geom["value"][1]) / 2
    y = 30 + 1 * KEYFRAMES_ROW_H + KEYFRAMES_ROW_H / 2
    event = QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(x, y),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier)
    tree.mouseDoubleClickEvent(event)  # must not raise
    assert tree._value_editor is not None
    assert tree._value_editor.text() == ""
    tree._finish_value_edit(commit=False)


# ── AUD-10401/10402: connection signal contract ───────────────────────


class _ConnItem:
    def __init__(self, selected: bool = False) -> None:
        self._selected = selected

    def isSelected(self) -> bool:
        return self._selected


class _FakeEditor(QObject):
    node_connected = pyqtSignal(str, str, str, str)
    node_disconnected = pyqtSignal(str, str, str, str)
    graph_changed = pyqtSignal()

    def __init__(self, graph: NodeGraph) -> None:
        super().__init__()
        self._node_graph = graph
        self._node_items: Dict[str, Any] = {}
        self._connection_items: List[Tuple[Any, str, str, str, str]] = []
        self._batch_mode = False

    def add_connection_visual(self, s: str, sp: str, d: str, dp: str,
                              _data_type: str = "image") -> None:
        self._connection_items.append((_ConnItem(), s, sp, d, dp))

    def _remove_connection_item(self, item: _ConnItem) -> None:
        self._connection_items = [c for c in self._connection_items
                                  if c[0] is not item]

    def remove_connection_visual_by_dst(self, dst: str, dst_port: str) -> None:
        # Mirrors composite_page._on_nodes_disconnected: first dst match.
        for conn_data in list(self._connection_items):
            if conn_data[3] == dst and conn_data[4] == dst_port:
                self._remove_connection_item(conn_data[0])
                return

    def is_node_locked(self, _nid: str) -> bool:
        return False


def _graph() -> Tuple[NodeGraph, str, str]:
    g = NodeGraph()
    color = COLORNode(node_id="color-1")
    xform = TransformNode(node_id="xform-1")
    g.add_node(color)
    g.add_node(xform)
    return g, "color-1", "xform-1"


QUAD = ("color-1", "result", "xform-1", "image")


def _mirror_page_connect(editor: _FakeEditor, graph: NodeGraph) -> None:
    """Subscribe with composite_page._on_nodes_connected semantics."""
    def on_connect(s: str, sp: str, d: str, dp: str) -> None:
        if graph.connect(s, sp, d, dp):
            editor.add_connection_visual(s, sp, d, dp)
    editor.node_connected.connect(on_connect)


def _quad_count(graph: NodeGraph) -> int:
    return sum(1 for c in graph.connections
               if (c.src_node_id, c.src_port_name,
                   c.dst_node_id, c.dst_port_name) == QUAD)


@pytest.mark.usefixtures("qapp")
def test_aud_10401_connect_submits_once():
    graph, src, dst = _graph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)
    _mirror_page_connect(editor, graph)

    assert actions.connect(*QUAD) is True
    # Exactly one model connection and one visual cable — the old code did
    # the work itself AND the page handler redid it.
    assert _quad_count(graph) == 1
    assert len(editor._connection_items) == 1

    # Duplicate request is refused instead of appending a second copy.
    assert actions.connect(*QUAD) is False
    assert _quad_count(graph) == 1
    assert len(editor._connection_items) == 1


@pytest.mark.usefixtures("qapp")
def test_aud_10402_disconnect_removes_exactly_one_cable():
    graph, _src, _dst = _graph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)
    # Two cables painted into the same destination port; only QUAD has a
    # model connection (the first-painted one does not — ghost order is
    # irrelevant to the count contract).
    editor._connection_items = [
        (_ConnItem(), "other", "result", "xform-1", "image"),
        (_ConnItem(), *QUAD),
    ]
    graph.connect(*QUAD)

    editor.node_disconnected.connect(
        lambda s, sp, d, dp: (
            graph.disconnect(s, sp, d, dp),
            editor.remove_connection_visual_by_dst(d, dp)))

    assert actions.disconnect(*QUAD) is True
    # Model: the requested quad is gone.
    assert _quad_count(graph) == 0
    # Visuals: exactly one removed. The old code removed the exact cable
    # itself AND the page's by-dst cleanup removed a second one.
    assert len(editor._connection_items) == 1


@pytest.mark.usefixtures("qapp")
def test_aud_10402_disconnect_ghost_visual_without_signal():
    graph, _src, _dst = _graph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)
    ghost = _ConnItem()
    editor._connection_items = [(ghost, *QUAD)]  # visual only, no model conn

    spy = _Spy()
    editor.node_disconnected.connect(spy)

    assert actions.disconnect(*QUAD) is True
    assert editor._connection_items == []  # stale visual cleaned up
    assert spy.calls == []  # no phantom undo/model write


@pytest.mark.usefixtures("qapp")
def test_aud_10402_delete_selected_connections_mixed_ghosts():
    graph, _src, _dst = _graph()
    editor = _FakeEditor(graph)
    actions = NodeEditorActions(editor, graph)
    live_sel, ghost_sel = _ConnItem(True), _ConnItem(True)
    editor._connection_items = [
        (live_sel, *QUAD),
        (ghost_sel, "other", "result", "xform-1", "image"),
    ]
    graph.connect(*QUAD)  # only the first has a model connection

    spy = _Spy()

    def _mirror_disconnect(s: str, sp: str, d: str, dp: str) -> None:
        spy(s, sp, d, dp)
        editor.remove_connection_visual_by_dst(d, dp)

    editor.node_disconnected.connect(_mirror_disconnect)

    deleted = actions.delete_selected_connections()
    assert sorted(deleted) == sorted(
        [QUAD, ("other", "result", "xform-1", "image")])
    assert len(spy.calls) == 1  # only the real connection is signalled
    assert spy.calls[0] == QUAD
    assert editor._connection_items == []  # ghost dropped directly
