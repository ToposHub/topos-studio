"""AE 语义优化测试：

1. ◆ 关键帧按钮红色仅代表「播放头正停在关键帧上」——移动播放头/增删
   关键帧时按钮状态实时重算（含 X/Y 共享按钮）。
2. Keyframes 面板的播放范围（工作区）与 viewport 底部播放条的 I/O
   标记双向联动：面板 → 播放条；播放条 I/O/清除按钮 → 面板。
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import pyqtSignal, QObject
from PyQt6.QtGui import QColor

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.nodes.constant_node import ConstantNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.features.composite.ui.keyframes_model import KeyframesModel
from src.features.composite.ui.node_properties_panel import NodePropertiesPanel
from src.shared.color.color_conversion import srgb_to_linear


CLIP_FRAMES = 100
CLIP_FPS = 24.0


class _FakeIOBar:
    """记录 set_in_out_points 调用的最小播放条替身。"""

    def __init__(self):
        self.calls = []

    def set_in_out_points(self, in_point, out_point):
        self.calls.append((in_point, out_point))


class _FakeViewer(QObject):
    """set_viewer 只连线交互信号 + 读取 video_control_bar。"""

    interaction_started = pyqtSignal(object)
    interaction_updated = pyqtSignal(object)
    interaction_ended = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.video_control_bar = _FakeIOBar()


# ── 夹具 ─────────────────────────────────────────────────────────────────

@pytest.fixture
def props_panel(qapp):
    """绑定 NodeGraph 的属性面板，当前节点为一个 Transform。"""
    g = NodeGraph()
    node = TransformNode()
    g.add_node(node)
    panel = NodePropertiesPanel()
    panel.set_node_graph(g)
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    panel.set_node(node.id, node.node_type, node.node_title,
                   dict(node.parameters), {}, pdefs, enabled=True)
    return panel, node


def _kf_btn(panel, key):
    info = panel._param_widgets.get(key)
    if info is None:
        section = panel._sections.get(panel._current_node_id)
        assert section is not None
        info = section._param_widgets.get(key)
    assert info is not None, f"找不到参数行 {key}"
    return info['keyframe_btn']


def _add_track(panel, node, param, kfs):
    pdef = panel._param_defs[param]
    panel._on_keyframes_changed(param, pdef, kfs)


# ── 1. ◆ 按钮红色 = 播放头下有帧 ────────────────────────────────────────

def test_button_grey_without_keyframes(props_panel):
    panel, node = props_panel
    assert _kf_btn(panel, "rotation").isChecked() is False


def test_button_red_only_when_playhead_on_keyframe(props_panel):
    panel, node = props_panel
    _add_track(panel, node, "rotation",
               [{"frame": 10, "value": 0.0}, {"frame": 30, "value": 90.0}])

    btn = _kf_btn(panel, "rotation")
    panel.set_timeline_state(CLIP_FRAMES, 10)
    assert btn.isChecked() is True, "播放头在关键帧 10 上 → 红色"

    panel.set_timeline_state(CLIP_FRAMES, 15)
    assert btn.isChecked() is False, "播放头在两帧之间 → 灰色"

    panel.set_timeline_state(CLIP_FRAMES, 30)
    assert btn.isChecked() is True, "播放头在关键帧 30 上 → 红色"


def test_properties_value_follows_animated_playhead(props_panel):
    """Properties displays the same sampled value as the animation track."""
    panel, node = props_panel
    _add_track(panel, node, "scale_x",
               [{"frame": 0, "value": 1.0},
                {"frame": 10, "value": 2.0}])
    panel.set_timeline_state(CLIP_FRAMES, 5)
    section = panel._sections[node.id]
    assert section._param_widgets["scale_x"]["widget"].value() == pytest.approx(1.5)

    panel.set_timeline_state(CLIP_FRAMES, 10)
    assert section._param_widgets["scale_x"]["widget"].value() == pytest.approx(2.0)


def test_button_follows_keyframe_add_and_remove(props_panel):
    panel, node = props_panel
    panel.set_timeline_state(CLIP_FRAMES, 20)
    btn = _kf_btn(panel, "rotation")
    assert btn.isChecked() is False

    # 在当前帧添加关键帧 → 立即变红
    panel._toggle_keyframe_at_current_frame("rotation",
                                            panel._param_defs["rotation"])
    assert btn.isChecked() is True

    # 再次点击移除当前帧关键帧（最后一个 → 整轨删除）→ 变灰
    panel._toggle_keyframe_at_current_frame("rotation",
                                            panel._param_defs["rotation"])
    assert btn.isChecked() is False
    assert "rotation" not in node.keyframe_tracks


def test_pair_button_red_when_any_axis_keyed_at_playhead(props_panel):
    panel, node = props_panel
    # translate_x/y 共享一个 ◆（X/Y 对）。只给 Y 轴打关键帧。
    _add_track(panel, node, "translate_y", [{"frame": 5, "value": 10.0}])

    btn = _kf_btn(panel, "translate_x")
    assert btn is _kf_btn(panel, "translate_y"), "X/Y 应共享同一个按钮"
    panel.set_timeline_state(CLIP_FRAMES, 5)
    assert btn.isChecked() is True

    panel.set_timeline_state(CLIP_FRAMES, 6)
    assert btn.isChecked() is False


def test_pair_button_uses_live_values_when_added_on_later_frame(props_panel):
    """Clicking the stopwatch after editing Scale must capture that edit."""
    panel, node = props_panel
    btn = _kf_btn(panel, "scale_x")

    node.set_parameter("scale_x", 1.0)
    node.set_parameter("scale_y", 1.0)
    panel.set_timeline_state(CLIP_FRAMES, 0)
    btn.click()

    node.set_parameter("scale_x", 2.0)
    node.set_parameter("scale_y", 2.0)
    panel.set_timeline_state(CLIP_FRAMES, 50)
    btn.click()

    for key in ("scale_x", "scale_y"):
        values = [
            (kf["frame"], kf["value"])
            for kf in node.keyframe_tracks[key]["keyframes"]
        ]
        assert values == [(0, 1.0), (50, 2.0)]


def test_refresh_keyframe_button_states_is_idempotent(props_panel):
    panel, node = props_panel
    _add_track(panel, node, "rotation", [{"frame": 7, "value": 1.0}])
    panel.set_timeline_state(CLIP_FRAMES, 7)
    panel.refresh_keyframe_button_states()
    panel.refresh_keyframe_button_states()
    assert _kf_btn(panel, "rotation").isChecked() is True


def _constant_panel(qapp, mode="solid"):
    graph = NodeGraph()
    node = ConstantNode()
    node.set_parameter("mode", mode)
    graph.add_node(node)
    panel = NodePropertiesPanel()
    panel.set_node_graph(graph)
    pdefs = {pd.name: pd for pd in node.PARAM_DEFS}
    panel.set_node(node.id, node.node_type, node.node_title,
                   dict(node.parameters), {}, pdefs, enabled=True)
    return panel, node


def test_constant_color_swatch_has_shared_rgba_keyframe_button(qapp):
    panel, node = _constant_panel(qapp)
    section = panel._sections[node.id]
    info = section._param_widgets["color_hex"]
    assert info["keyframe_btn_keys"] == ["color"]

    panel.set_timeline_state(CLIP_FRAMES, 12)
    info["keyframe_btn"].click()

    keyframe = node.keyframe_tracks["color"]["keyframes"][0]
    assert keyframe["frame"] == 12
    assert keyframe["value"] == list(node.parameters["color"])
    assert keyframe["interpolation"] == "linear"


def test_constant_color_picker_stores_scene_linear_values(qapp, monkeypatch):
    """The Constant UI displays sRGB but writes linear graph channels."""
    panel, node = _constant_panel(qapp)

    picked = QColor("#808080")

    def fake_get_color(*, parent, initial, on_live):
        return picked

    import src.shared.color as shared_color
    monkeypatch.setattr(shared_color, "get_color", fake_get_color)

    panel._sections[node.id]._param_widgets["color_hex"]["widget"].click()

    params = panel._sections[node.id].parameters
    assert params["color"][:3] == (
        srgb_to_linear(picked.redF()),
        srgb_to_linear(picked.greenF()),
        srgb_to_linear(picked.blueF()),
    )
    assert params["color"][3] == 1.0
    assert params["color_hex"] == "#808080"


def test_constant_checker_size_has_keyframe_button_and_render_value(qapp):
    panel, node = _constant_panel(qapp, mode="checker")
    section = panel._sections[node.id]
    info = section._param_widgets["checker_size"]
    assert info["animatable"] is True
    assert info["keyframe_btn_keys"] == ["checker_size"]

    panel.set_timeline_state(CLIP_FRAMES, 8)
    info["keyframe_btn"].click()

    keyframe = node.keyframe_tracks["checker_size"]["keyframes"][0]
    assert keyframe["frame"] == 8
    assert keyframe["value"] == 64.0
    assert keyframe["interpolation"] == "linear"


@pytest.mark.parametrize(
    "mode, expected",
    [
        ("solid", ["width", "height", "color"]),
        ("checker", ["width", "height", "color", "color2", "checker_size"]),
        ("gradient", [
            "width", "height", "color", "color2",
            "start_x", "start_y", "end_x", "end_y", "feather",
        ]),
        ("radial", [
            "width", "height", "color", "color2",
            "center_x", "center_y", "radial_radius", "feather",
        ]),
    ],
)
def test_constant_keyframes_only_show_active_mode_parameters(qapp, mode, expected):
    graph = NodeGraph()
    node = ConstantNode()
    node.set_parameter("mode", mode)
    graph.add_node(node)

    model = KeyframesModel()
    model.rebuild(graph)
    rows = [row.param_name for row in model.rows if row.row_type == "param"]

    assert rows == expected
    assert not any(name.endswith(("_r", "_g", "_b", "_a")) for name in rows)


def test_constant_mode_change_refreshes_keyframes_rows(qapp, monkeypatch):
    graph = NodeGraph()
    node = ConstantNode()
    graph.add_node(node)
    page = CompositePage()
    panel = KeyframesPanel()
    page._node_graph = graph
    page._keyframes_panel = panel
    panel.set_node_graph(graph, selected_node_id=node.id)

    # Keep this test focused on the Properties → graph → Keyframes refresh
    # boundary; rendering is intentionally not involved.
    monkeypatch.setattr(page, "_evaluate_graph", lambda: None)
    monkeypatch.setattr(page, "update_viewer_handles", lambda: None)

    page._on_parameter_changed(node.id, "mode", "checker")
    rows = [row.param_name for row in panel._model.rows
            if row.row_type == "param"]
    assert rows[-1] == "checker_size"
    assert "start_x" not in rows

    page._on_parameter_changed(node.id, "mode", "radial")
    rows = [row.param_name for row in panel._model.rows
            if row.row_type == "param"]
    assert rows[-2:] == ["radial_radius", "feather"]
    assert "checker_size" not in rows


def test_constant_keyframes_color_row_uses_one_atomic_color_track(qapp):
    graph = NodeGraph()
    node = ConstantNode()
    graph.add_node(node)
    from src.shared.keyframe import ColorKeyframeTrack, Keyframe

    track = ColorKeyframeTrack("color", min_val=0.0, max_val=1.0)
    track.add_keyframe(Keyframe(frame=12, value=(0.2, 0.4, 0.6, 1.0)))
    node.set_keyframe_track("color", track)

    model = KeyframesModel()
    model.rebuild(graph)
    color_rows = [
        row for row in model.rows
        if row.row_type == "param" and row.param_name == "color"
    ]
    assert len(color_rows) == 1
    visual = model.track_for(color_rows[0].row_id)
    assert visual is not None
    assert [point.frame for point in visual.keyframes] == [12]
    assert set(node.keyframe_tracks) == {"color"}
    assert node.keyframe_tracks["color"]["value_type"] == "color"


def test_constant_color_keyframe_interpolates_rgba_as_one_value(qapp):
    from src.shared.keyframe import ColorKeyframeTrack, Keyframe

    track = ColorKeyframeTrack("color")
    track.add_keyframe(Keyframe(frame=0, value=(0.0, 0.2, 0.4, 0.5)))
    track.add_keyframe(Keyframe(frame=10, value=(1.0, 0.8, 0.6, 1.0)))

    assert track.get_value(5) == pytest.approx((0.5, 0.5, 0.5, 0.75))


# ── 2. 播放范围条双向同步 ───────────────────────────────────────────────

@pytest.fixture
def page_setup(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)
    page.set_keyframes_transport(None)
    viewer = _FakeViewer()
    page._viewer = viewer
    return page, panel, viewer.video_control_bar


def test_panel_play_range_pushed_to_viewport_bar(page_setup):
    page, panel, bar = page_setup
    panel.set_play_range(10, 40)
    assert bar.calls[-1] == (10, 40), "面板工作区 → 播放条 I/O 标记"
    assert page._comp_in_frame == 10
    assert page._comp_out_frame == 40


def test_clear_panel_play_range_clears_viewport_bar(page_setup):
    page, panel, bar = page_setup
    panel.set_play_range(10, 40)
    panel.clear_play_range()
    assert bar.calls[-1] == (None, None), "清除工作区 → 清除播放条标记"


def test_viewport_mark_in_writes_panel_work_area(page_setup):
    page, panel, bar = page_setup
    panel.set_current_frame(30)
    page.mark_play_in()
    assert panel.play_range_points()[0] == 30, "播放条 I 按钮 → 面板入点"
    assert bar.calls[-1][0] == 30


def test_viewport_mark_out_writes_panel_work_area(page_setup):
    page, panel, bar = page_setup
    panel.set_current_frame(12)
    page.mark_play_in()
    panel.set_current_frame(55)
    page.mark_play_out()
    assert panel.play_range_points() == (12, 55)
    assert bar.calls[-1] == (12, 55)


def test_viewport_clear_removes_panel_work_area(page_setup):
    page, panel, bar = page_setup
    panel.set_play_range(8, 24)
    page.clear_play_range_marks()
    assert panel.play_range_points() == (None, None)
    assert bar.calls[-1] == (None, None)


def test_mark_out_before_in_swaps_safely(page_setup):
    page, panel, bar = page_setup
    panel.set_current_frame(60)
    page.mark_play_out()          # out=60, in 缺省取 clip 起点
    panel.set_current_frame(20)
    page.mark_play_in()           # in 不能超过 out
    pin, pout = panel.play_range_points()
    assert pin is not None and pout is not None
    assert pin <= pout


def test_set_viewer_wires_bar_and_pushes_initial_marks(qapp):
    """set_viewer 接线播放条按钮，且已有工作区时立即推送标记。"""
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)
    page.set_keyframes_transport(None)
    panel.set_play_range(5, 45)

    viewer = _FakeViewer()
    page.set_viewer(viewer)
    bar = viewer.video_control_bar
    assert bar.calls and bar.calls[-1] == (5, 45), "set_viewer 初始推送"


# ── 3. 播放头在关键帧上调参 → 只更新该关键帧 ──────────────────────

def test_autokey_on_keyframe_updates_value_and_preserves_shape(props_panel):
    """播放头停在关键帧上重新调参：只改该帧的值，插值类型与手柄保留。"""
    panel, node = props_panel
    _add_track(panel, node, "rotation", [
        {"frame": 10, "value": 0.0},
        {"frame": 20, "value": 45.0, "interpolation": "bezier",
         "handle_left": [-3.0, -10.0], "handle_right": [3.0, 10.0]},
        {"frame": 40, "value": 90.0},
    ])
    panel.set_timeline_state(CLIP_FRAMES, 20)

    panel._autokey_if_animated("rotation", 60.0)

    kfs = node.keyframe_tracks["rotation"]["keyframes"]
    assert len(kfs) == 3, "不能新增/删除关键帧"
    at_20 = next(k for k in kfs if k["frame"] == 20)
    assert at_20["value"] == pytest.approx(60.0)
    assert at_20["interpolation"] == "bezier", "插值类型不能被重置"
    assert at_20.get("handle_left") == [-3.0, -10.0], "手柄必须保留"
    assert at_20.get("handle_right") == [3.0, 10.0]


def test_autokey_between_keyframes_still_adds(props_panel):
    """播放头不在任何关键帧上：维持原行为——在当前帧新增关键帧。"""
    panel, node = props_panel
    _add_track(panel, node, "rotation",
               [{"frame": 10, "value": 0.0}, {"frame": 30, "value": 90.0}])
    panel.set_timeline_state(CLIP_FRAMES, 15)

    panel._autokey_if_animated("rotation", 30.0)

    frames = sorted(
        k["frame"] for k in node.keyframe_tracks["rotation"]["keyframes"])
    assert frames == [10, 15, 30]
