"""P2 tests: KeyframesModel row/track generation from a NodeGraph.

Pure-data tests — no QWidget needed. Covers:
- node + param row generation (P2.1/P2.2)
- expansion + filtering (P2.3/P2.4)
- frame→x mapping (P2.5)
- node color propagation (P2.6)
- keyframe point output
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.blur_node import BlurNode
from src.features.composite.nodes.output_node import OutputNode
from src.features.composite.nodes.video_frame_node import VideoFrameNode
from src.features.composite.nodes.roto_shape_node import RotoShapeNode
from src.features.composite.ui.keyframes_model import (
    KeyframesModel, FrameMapper, CompositeTimelineState, _channel_color_for)
from src.shared.keyframe import Keyframe, KeyframeTrack


pytestmark = pytest.mark.composite_core


def _graph_with_animation():
    g = NodeGraph()
    t = TransformNode()
    track = KeyframeTrack(param_name="rotation", min_val=-360.0, max_val=360.0)
    track.add_keyframe(Keyframe(0, 0.0))
    track.add_keyframe(Keyframe(10, 45.0))
    t.set_keyframe_track("rotation", track)
    b = BlurNode()
    out = OutputNode()
    g.add_node(t)
    g.add_node(b)
    g.add_node(out)
    return g, t, b


# ── P2.1/P2.2: row generation ───────────────────────────────────────────

def test_model_generates_node_rows():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    node_rows = [r for r in m.rows if r.row_type == "node"]
    assert len(node_rows) == 3  # transform, blur, output
    labels = {r.node_id for r in node_rows}
    assert t.id in labels and b.id in labels


def test_model_generates_param_rows_only_for_tracks():
    """Without 'animated' filter, animatable params show; non-animatable don't."""
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    param_rows = [r for r in m.rows if r.row_type == "param"]
    # transform has many animatable params; blur has radius.
    param_names = {(r.node_id, r.param_name) for r in param_rows}
    assert (t.id, "rotation") in param_names
    assert (b.id, "radius") in param_names


def test_hidden_numeric_metadata_is_not_an_animatable_row():
    """Implementation metadata must not appear as user-facing keyframe rows."""
    node = VideoFrameNode()
    names = {name for name, _pdef in KeyframesModel._animatable_params(node)}

    assert names.isdisjoint({
        "fps", "total_frames", "video_width", "video_height", "bit_depth",
    })


def test_roto_point_channels_are_projected_as_animatable_rows():
    node = RotoShapeNode(node_id="roto")
    names = {name for name, pdef in KeyframesModel._animatable_params(node)}
    assert "roto_point_0_x" in names
    assert "roto_point_0_c1_y" in names


def test_param_row_carries_keyframe_points():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    track = m.track_for(f"param:{t.id}:rotation")
    assert track is not None
    assert len(track.keyframes) == 2
    assert track.keyframes[0].frame == 0
    assert track.keyframes[1].frame == 10
    assert pytest.approx(track.keyframes[1].value) == 45.0


# ── P2.3: filtering ─────────────────────────────────────────────────────

def test_filter_animated_hides_non_tracked_params():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.set_filter("animated")
    m.rebuild(g)
    param_rows = [r for r in m.rows if r.row_type == "param"]
    # Only rotation (transform) and radius (blur) have tracks.
    assert all(
        (r.node_id == t.id and r.param_name == "rotation")
        or (r.node_id == b.id and r.param_name == "radius")
        for r in param_rows
    )


def test_filter_modified_includes_authored_values_without_tracks():
    graph = NodeGraph()
    transform = TransformNode(node_id="transform")
    transform.set_parameter("rotation", 12.0)
    graph.add_node(transform)

    model = KeyframesModel()
    model.set_filter("modified")
    model.rebuild(graph)

    param_ids = {row.row_id for row in model.rows if row.row_type == "param"}
    assert "param:transform:rotation" in param_ids
    assert "param:transform:position" not in param_ids


def test_filter_expression_only_includes_expression_properties():
    graph = NodeGraph()
    transform = TransformNode(node_id="transform")
    assert transform.set_expression("rotation", "0") is True
    graph.add_node(transform)

    model = KeyframesModel()
    model.set_filter("expression")
    model.rebuild(graph)

    param_rows = [row for row in model.rows if row.row_type == "param"]
    assert [row.param_name for row in param_rows] == ["rotation"]


# ── P2.4: expansion ─────────────────────────────────────────────────────

def test_collapsed_node_hides_param_rows():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    m.set_expanded(t.id, False)
    visible = m.visible_rows()
    # transform node row present, but none of its param rows.
    assert any(r.row_type == "node" and r.node_id == t.id for r in visible)
    assert not any(r.row_type == "param" and r.node_id == t.id for r in visible)


def test_expand_all_shows_every_param():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    m.expand_all(True)
    visible = m.visible_rows()
    assert any(r.row_type == "param" and r.node_id == t.id for r in visible)


# ── P2.5: frame mapping ─────────────────────────────────────────────────

def test_frame_mapper_round_trip():
    fm = FrameMapper(pixels_per_frame=4.0, scroll_offset=10.0)
    x = fm.frame_to_x(5, origin_x=0.0)
    assert x == 10.0 + 5 * 4.0
    assert fm.x_to_frame(x, origin_x=0.0) == 5.0


def test_frame_mapper_supports_negative_frames():
    fm = FrameMapper(pixels_per_frame=2.0)
    x_neg = fm.frame_to_x(-50)
    x_zero = fm.frame_to_x(0)
    x_pos = fm.frame_to_x(50)
    assert x_neg < x_zero < x_pos


def test_frame_mapper_zoom_clamped():
    fm = FrameMapper()
    fm.set_zoom(0.0)  # too small → clamped
    assert fm.pixels_per_frame >= 0.05


# ── P2.6: node color ────────────────────────────────────────────────────

def test_node_color_propagates_to_rows_and_tracks():
    g, t, b = _graph_with_animation()
    t.custom_header_color = "#abcdef"
    m = KeyframesModel()
    m.rebuild(g)
    node_row = next(r for r in m.rows if r.row_type == "node" and r.node_id == t.id)
    assert node_row.node_color == "#abcdef"
    track = m.track_for(node_row.row_id)
    assert track.node_color == "#abcdef"


def test_color_override_updates_without_reselect():
    """Changing a node color then rebuilding reflects the new color."""
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g)
    t.custom_header_color = "#111111"
    m.rebuild(g)
    node_row = next(r for r in m.rows if r.row_type == "node" and r.node_id == t.id)
    assert node_row.node_color == "#111111"


# ── channel color ───────────────────────────────────────────────────────

def test_rotation_param_gets_angle_channel_color():
    color = _channel_color_for("rotation", "#555555")
    from src.shared.ui.style_sheet import C
    assert color == C.CURVE_ANGLE


def test_generic_param_inherits_node_color():
    color = _channel_color_for("radius", "#aabbcc")
    assert color == "#aabbcc"


# ── time range bar ──────────────────────────────────────────────────────

def test_node_time_range_becomes_range_bar():
    g = NodeGraph()
    b = BlurNode()
    b.time_range = {"comp_start": 5, "comp_end": 50}
    g.add_node(b)
    m = KeyframesModel()
    m.rebuild(g)
    track = m.track_for(f"node:{b.id}")
    assert track.range_bar is not None
    assert track.range_bar.start_frame == 5
    assert track.range_bar.end_frame == 50


def test_node_without_time_range_gets_default_layer_bar():
    """AE 风格：无显式 time_range 的节点显示横跨合成的默认图层条。

    拖拽该条的边缘会写入第一个显式 time_range（逐帧控制素材播放范围）。
    """
    g = NodeGraph()
    b = BlurNode()
    g.add_node(b)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    track = m.track_for(f"node:{b.id}")
    assert track.range_bar is not None
    assert track.range_bar.default is True
    assert track.range_bar.start_frame == 0
    assert track.range_bar.end_frame == 99


def test_set_comp_range_refits_default_bars_only():
    """合成范围变化：默认图层条跟随，显式 range 不受影响。"""
    g = NodeGraph()
    b = BlurNode()
    g.add_node(b)
    t = BlurNode()
    t.time_range = {"comp_start": 5, "comp_end": 50}
    g.add_node(t)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    m.set_comp_range(0, 199)
    assert m.track_for(f"node:{b.id}").range_bar.end_frame == 199
    assert m.track_for(f"node:{t.id}").range_bar.end_frame == 50


# ── selection ───────────────────────────────────────────────────────────

def test_selected_node_marked():
    g, t, b = _graph_with_animation()
    m = KeyframesModel()
    m.rebuild(g, selected_node_id=t.id)
    node_row = next(r for r in m.rows if r.row_type == "node" and r.node_id == t.id)
    assert node_row.selected is True
    other = next(r for r in m.rows if r.row_type == "node" and r.node_id == b.id)
    assert other.selected is False


# ── video media length bars ─────────────────────────────────────────────

def test_video_node_default_bar_spans_media_length():
    """视频节点默认条 = 素材全长（可比合成长），不再是合成长度。"""
    g = NodeGraph()
    v = VideoFrameNode()
    v.set_parameter("total_frames", 500)
    g.add_node(v)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    track = m.track_for(f"node:{v.id}")
    assert track.range_bar is not None
    assert track.range_bar.default is True
    assert track.range_bar.start_frame == 0
    assert track.range_bar.end_frame == 499
    assert track.source_total == 500


def test_video_node_default_bar_falls_back_to_comp_range_without_metadata():
    """total_frames 未知时保持旧行为（合成长度条）。"""
    g = NodeGraph()
    v = VideoFrameNode()
    g.add_node(v)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    track = m.track_for(f"node:{v.id}")
    assert track.range_bar is not None
    assert track.range_bar.end_frame == 99
    assert track.source_total == 0


def test_video_node_explicit_range_still_wins():
    """显式 trim 后条仍显示 comp_start/comp_end。"""
    g = NodeGraph()
    v = VideoFrameNode()
    v.set_parameter("total_frames", 500)
    v.time_range = {"comp_start": 20, "comp_end": 80, "enabled": True}
    g.add_node(v)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    track = m.track_for(f"node:{v.id}")
    assert (track.range_bar.start_frame, track.range_bar.end_frame) == (20, 80)
    # source_total 仍然可用于拖拽钳制
    assert track.source_total == 500


def test_non_video_default_bar_unchanged():
    """非视频节点的默认条不受素材长度逻辑影响。"""
    g = NodeGraph()
    b = BlurNode()
    g.add_node(b)
    m = KeyframesModel()
    m.set_comp_range(0, 99)
    m.rebuild(g)
    track = m.track_for(f"node:{b.id}")
    assert track.range_bar.end_frame == 99
    assert track.source_total == 0
