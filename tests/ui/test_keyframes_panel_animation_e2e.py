"""End-to-end tests: KeyframesPanel keyframe animation workflow.

Simulates the real CompositePage wiring (track_changed → node write,
current_frame_changed → playhead sync, value provider → live param value)
and drives the panel the way a user would:

  1. align the frame range with the comp clip (0..99 @ 24fps)
  2. select a parameter row (Translate X)
  3. set keyframes at frame 0 and frame 50 via the toolbar action
  4. verify the node's track interpolates between them (real animation)
  5. verify playback advances the playhead and loops over the clip range
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100   # comp clip duration in frames (0..99)
CLIP_FPS = 24.0


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    """Panel wired exactly like CompositePage wires it."""
    p = KeyframesPanel()
    p.resize(1000, 300)

    # track_changed → persist on the node (mirrors CompositePage slot).
    def on_track_changed(node_id, param_name, keyframes):
        node = graph.get_node(node_id)
        if node is None:
            return
        if not keyframes:
            node.remove_keyframe_track(param_name)
            return
        pdef = node.get_param_defs().get(param_name)
        track = KeyframeTrack(
            param_name=param_name,
            min_val=pdef.min_val if pdef else 0.0,
            max_val=pdef.max_val if pdef else 1.0)
        for kf_data in keyframes:
            track.add_keyframe(Keyframe.from_dict(kf_data))
        node.set_keyframe_track(param_name, track)

    p.track_changed.connect(on_track_changed)

    # value provider → live node param value (mirrors CompositePage).
    def value_provider(node_id, param_name):
        node = graph.get_node(node_id)
        if node is None:
            return None
        try:
            return float(node.parameters.get(param_name, 0.0))
        except (TypeError, ValueError):
            return None

    p.set_value_provider(value_provider)

    p.set_node_graph(graph)
    # Comp clip frame range: 0..99 @ 24fps.
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


def _transform(panel, graph):
    node_id = panel._model.rows[0].node_id
    return node_id, graph.get_node(node_id)


# ── frame range alignment ───────────────────────────────────────────────

def test_frame_range_matches_comp_clip(panel):
    assert panel._state.start_frame == 0
    assert panel._state.end_frame == CLIP_FRAMES - 1
    assert panel._state.fps == CLIP_FPS
    # Footer shows the clip range.
    assert "0-99" in panel._range_label.text()
    assert "(100)" in panel._range_label.text()


def test_ruler_fits_comp_clip_range(panel):
    panel.fit_time_range()
    ppf = panel._frame_mapper.pixels_per_frame
    # The full 100-frame clip should span roughly the view width.
    expected = panel._timeline_view.width() / CLIP_FRAMES
    assert abs(ppf - expected) / expected < 0.05


# ── adding keyframes via the toolbar action ─────────────────────────────

def test_add_keyframes_and_animate(panel, graph):
    node_id, node = _transform(panel, graph)

    # Select the Translate X param row (tree click equivalent).
    panel._on_param_selected(node_id, "translate_x")

    # Frame 0: value 0 → set keyframe.
    node.parameters["translate_x"] = 0.0
    panel.set_current_frame(0)
    panel.add_keyframe_at_playhead()

    # Frame 50: value 100 → set keyframe.
    node.parameters["translate_x"] = 100.0
    panel.set_current_frame(50)
    panel.add_keyframe_at_playhead()

    # The node now owns a real keyframe track.
    track_data = node.keyframe_tracks.get("translate_x")
    assert track_data is not None
    frames = {kf["frame"]: kf["value"] for kf in track_data["keyframes"]}
    assert frames == {0: 0.0, 50: 100.0}

    # The animation actually interpolates between the two keyframes.
    track = KeyframeTrack.from_dict(track_data)
    assert track.get_value(0) == pytest.approx(0.0)
    assert track.get_value(25) == pytest.approx(50.0)
    assert track.get_value(50) == pytest.approx(100.0)


def test_set_keyframe_updates_existing(panel, graph):
    node_id, node = _transform(panel, graph)
    panel._on_param_selected(node_id, "scale_x")

    node.parameters["scale_x"] = 1.0
    panel.set_current_frame(10)
    panel.add_keyframe_at_playhead()

    # Same frame, new value → update in place, no duplicate keyframe.
    node.parameters["scale_x"] = 2.5
    panel.add_keyframe_at_playhead()

    kfs = node.keyframe_tracks["scale_x"]["keyframes"]
    assert len(kfs) == 1
    assert kfs[0]["frame"] == 10
    assert kfs[0]["value"] == pytest.approx(2.5)


def test_remove_keyframe_at_playhead(panel, graph):
    node_id, node = _transform(panel, graph)
    panel._on_param_selected(node_id, "rotation")

    node.parameters["rotation"] = 45.0
    panel.set_current_frame(20)
    panel.add_keyframe_at_playhead()
    assert "rotation" in node.keyframe_tracks

    panel.remove_keyframe_at_playhead()
    # Empty track → removed from the node entirely (CompositePage semantics).
    assert "rotation" not in node.keyframe_tracks


# ── playback ─────────────────────────────────────────────────────────────

def test_internal_playback_advances_and_loops(panel):
    frames = []
    panel.current_frame_changed.connect(frames.append)

    panel.set_current_frame(0)
    panel._internal_set_playing(True)
    assert panel._play_timer.isActive()
    assert panel._playing is True

    # Simulate ticks across the end of the clip: must loop to the start.
    for _ in range(CLIP_FRAMES + 2):
        panel._on_internal_play_tick()
    assert max(frames) == CLIP_FRAMES - 1
    assert frames[-1] == 2  # wrapped: ...98, 99, 0, 1, 2

    panel._internal_set_playing(False)
    assert not panel._play_timer.isActive()
    assert panel._playing is False


def test_play_button_state_sync(panel):
    panel.set_playing_state(True)
    assert panel._act_play.isChecked()
    panel.set_playing_state(False)
    assert not panel._act_play.isChecked()


def test_play_button_prefers_external_controller(panel):
    class FakePage:
        def __init__(self):
            self.requests = []

        def keyframes_panel_play_toggle(self, want_play):
            self.requests.append(want_play)

    fake = FakePage()
    panel.set_playback_controller(fake)
    panel._on_play_toggled(True)
    panel._on_play_toggled(False)
    assert fake.requests == [True, False]
    # The internal timer must not run when an external controller is bound.
    assert not panel._play_timer.isActive()


def test_seek_clamps_to_clip_range(panel):
    frames = []
    panel.current_frame_changed.connect(frames.append)
    panel.seek_to_frame(5000)
    panel.seek_to_frame(-10)
    assert frames[-2] == CLIP_FRAMES - 1
    assert frames[-1] == 0


# ── multi-row drag commit ──────────────────────────────────────────────

def test_multi_row_drag_commits_every_param(panel, graph):
    """框选多个参数的关键帧整体拖动，松手后每个参数都要落盘。

    回归：CompositePage 在每次 track_changed 后同步重建面板模型，
    旧实现在循环里边发边读模型，导致只有第一行的移动幸存。
    """
    from src.features.composite.ui import keyframes_timeline_view as ktv

    node_id, node = _transform(panel, graph)

    # 两个参数各在 10/20 帧加关键帧。
    for param in ("translate_x", "translate_y"):
        panel._on_param_selected(node_id, param)
        node.parameters[param] = 0.0
        panel.set_current_frame(10)
        panel.add_keyframe_at_playhead()
        panel.set_current_frame(20)
        panel.add_keyframe_at_playhead()

    # 换成 CompositePage 风格的 handler：写节点 + 同步重建面板模型。
    try:
        panel.track_changed.disconnect()
    except TypeError:
        pass

    def on_track_changed(node_id_, param_name, keyframes):
        node_ = graph.get_node(node_id_)
        if node_ is None:
            return
        pdef = node_.get_param_defs().get(param_name)
        track = KeyframeTrack(
            param_name=param_name,
            min_val=pdef.min_val if pdef else 0.0,
            max_val=pdef.max_val if pdef else 1.0)
        for kf_data in keyframes:
            track.add_keyframe(Keyframe.from_dict(kf_data))
        node_.set_keyframe_track(param_name, track)
        # CompositePage 每次写入后都会 set_node_graph 重建面板。
        panel.set_node_graph(graph)

    panel.track_changed.connect(on_track_changed)

    # 选中两个参数行的全部关键帧。
    first_row_id = None
    for row in panel._model.rows:
        if row.row_type != "param" or row.param_name not in (
                "translate_x", "translate_y"):
            continue
        if first_row_id is None:
            first_row_id = row.row_id
        track = panel._model.track_for(row.row_id)
        for kf in track.keyframes:
            kf.selected = True
    assert first_row_id is not None

    # 整体拖动 +5 帧（10/20 → 15/25）并提交。
    view = panel._timeline_view
    x_press = view._fm.frame_to_x(10, ktv._ORIGIN_X)
    x_drop = view._fm.frame_to_x(15, ktv._ORIGIN_X)
    view._start_multi_keyframe_drag(x_press, first_row_id)
    view._do_multi_keyframe_drag(x_drop)
    view._commit_drag_edit()

    # 两个参数都必须移动，而不是只有排序靠前的那个。
    for param in ("translate_x", "translate_y"):
        frames = sorted(
            kf["frame"] for kf in node.keyframe_tracks[param]["keyframes"])
        assert frames == [15, 25], f"{param} 的关键帧没有跟随拖动"
