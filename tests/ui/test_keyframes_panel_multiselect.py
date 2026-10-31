"""Keyframes 面板多选/复制粘贴/插值/手柄联动测试。

覆盖本轮功能：
  1. 框选多个关键帧（rubber-band marquee）
  2. 多选后整体拖动（含冲突保护）
  3. 复制 / 粘贴关键帧（Ctrl+C / Ctrl+V，粘贴以播放头为锚点）
  4. 右键菜单 / API 设置插值类型（线性 / bezier / 保持）
  5. Transform 手柄跟随关键帧动画（CompositePage 求值 + 拖动写关键帧）
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPointF, Qt, QEvent
from PyQt6.QtGui import QKeyEvent, QMouseEvent

from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0
_RULER_H = 30
_ROW_H = 24
PPF = 10.0  # 测试用固定缩放：1 帧 = 10px


def _make_graph_with_track(kfs=((10, 0.0), (20, 50.0), (30, 100.0)),
                           param="translate_x"):
    g = NodeGraph()
    node = TransformNode()
    track = KeyframeTrack(param_name=param, min_val=-8192.0, max_val=8192.0)
    for f, v in kfs:
        track.add_keyframe(Keyframe(frame=f, value=v))
    node.set_keyframe_track(param, track)
    g.add_node(node)
    return g, node, param


@pytest.fixture
def panel(qapp):
    g, node, param = _make_graph_with_track()
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(g)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    # 固定缩放/滚动 → frame_to_x(f) = f * 10，断言坐标可预测。
    p._frame_mapper.set_zoom(PPF)
    p._frame_mapper.set_scroll(0.0)
    return p


def _param_row_y(panel, node_id, param):
    """参数行的垂直中心（widget 坐标）。"""
    for i, row in enumerate(panel._model.visible_rows()):
        if row.row_id == f"param:{node_id}:{param}":
            return _RULER_H + i * _ROW_H - panel._timeline_view._scroll_y \
                + _ROW_H // 2
    raise AssertionError("param row not found")


def _mouse(view, etype, x, y, button=Qt.MouseButton.LeftButton,
           buttons=Qt.MouseButton.NoButton, mods=Qt.KeyboardModifier.NoModifier):
    ev = QMouseEvent(etype, QPointF(x, y), button, buttons, mods)
    if etype == QEvent.Type.MouseButtonPress:
        view.mousePressEvent(ev)
    elif etype == QEvent.Type.MouseMove:
        view.mouseMoveEvent(ev)
    else:
        view.mouseReleaseEvent(ev)


def _track_frames(panel, node_id, param):
    track = panel._model.track_for(f"param:{node_id}:{param}")
    return [kf.frame for kf in track.keyframes]


# ── 1. 框选 ─────────────────────────────────────────────────────────────

def test_marquee_selects_keyframes_inside_rect(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    cy = _param_row_y(panel, node_id, "translate_x")
    # 框住 frame 10/20（x=100/200），不碰 frame 30（x=300）
    _mouse(view, QEvent.Type.MouseButtonPress, 80, cy - 10)
    _mouse(view, QEvent.Type.MouseMove, 260, cy + 10,
           buttons=Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 260, cy + 10)

    sel = view.selected_keyframes()
    assert sorted(f for _, f in sel) == [10, 20]


def test_plain_click_on_empty_area_seeks_and_clears_selection(panel):
    """空白处普通单击仍是 seek 播放头（框选只在移动超过阈值后生效）。"""
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    cy = _param_row_y(panel, node_id, "translate_x")
    # 先选中一个关键帧
    view._select_keyframe(f"param:{node_id}:translate_x", 10)
    assert len(view.selected_keyframes()) == 1

    frames: list = []
    view.current_frame_changed.connect(frames.append)
    _mouse(view, QEvent.Type.MouseButtonPress, 500, cy)
    _mouse(view, QEvent.Type.MouseButtonRelease, 500, cy)

    assert frames == [50] and view._current_frame == 50
    assert view.selected_keyframes() == [], "单击应清除选择"


def test_shift_click_adds_to_selection(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    cy = _param_row_y(panel, node_id, "translate_x")
    row_id = f"param:{node_id}:translate_x"

    _mouse(view, QEvent.Type.MouseButtonPress, 100, cy)   # frame 10
    _mouse(view, QEvent.Type.MouseButtonRelease, 100, cy)
    _mouse(view, QEvent.Type.MouseButtonPress, 300, cy,
           mods=Qt.KeyboardModifier.ShiftModifier)         # frame 30 加选
    _mouse(view, QEvent.Type.MouseButtonRelease, 300, cy,
           mods=Qt.KeyboardModifier.ShiftModifier)

    assert sorted(f for _, f in view.selected_keyframes()) == [10, 30]


# ── 2. 多选整体拖动 ─────────────────────────────────────────────────────

def test_multi_drag_moves_all_selected_keyframes(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"
    cy = _param_row_y(panel, node_id, param)

    # 框选 frame 10/20
    _mouse(view, QEvent.Type.MouseButtonPress, 80, cy - 10)
    _mouse(view, QEvent.Type.MouseMove, 260, cy + 10,
           buttons=Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 260, cy + 10)

    emitted: list = []
    panel.track_changed.connect(lambda nid, p, kfs: emitted.append((p, kfs)))

    # 拖 frame 10 的关键帧到 frame 15（delta +5）→ 两个都 +5
    _mouse(view, QEvent.Type.MouseButtonPress, 100, cy)
    _mouse(view, QEvent.Type.MouseMove, 150, cy,
           buttons=Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 150, cy)

    assert _track_frames(panel, node_id, param) == [15, 25, 30]
    assert len(emitted) == 1
    assert [k["frame"] for k in emitted[0][1]] == [15, 25, 30]


def test_multi_drag_refused_on_conflict(panel):
    """目标帧被未移动的关键帧占用时，整体移动被拒绝（不覆盖邻居）。"""
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"
    cy = _param_row_y(panel, node_id, param)

    view._select_keyframe(f"param:{node_id}:{param}", 10)
    view._select_keyframe(f"param:{node_id}:{param}", 20, additive=True)

    # delta +20 → 目标 30/40，frame 30 有关键帧未参与移动 → 拒绝
    _mouse(view, QEvent.Type.MouseButtonPress, 100, cy)
    _mouse(view, QEvent.Type.MouseMove, 300, cy,
           buttons=Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 300, cy)

    assert _track_frames(panel, node_id, param) == [10, 20, 30]


def test_ctrl_a_selects_all_keyframes(panel):
    view = panel._timeline_view
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                   Qt.KeyboardModifier.ControlModifier)
    view.keyPressEvent(ev)
    assert len(view.selected_keyframes()) == 3


# ── 3. 复制 / 粘贴 ──────────────────────────────────────────────────────

def test_copy_paste_reanchors_at_playhead(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"

    view._select_keyframe(f"param:{node_id}:{param}", 10)
    view._select_keyframe(f"param:{node_id}:{param}", 20, additive=True)
    assert view.copy_selected_keyframes() == 2

    emitted: list = []
    panel.track_changed.connect(lambda nid, p, kfs: emitted.append(kfs))

    view.set_current_frame(50)
    assert view.paste_keyframes() is True
    # 相对偏移保留：10/20 → 50/60，值保持 0/50
    frames = _track_frames(panel, node_id, param)
    assert frames == [10, 20, 30, 50, 60]
    track = panel._model.track_for(f"param:{node_id}:{param}")
    by_frame = {kf.frame: kf.value for kf in track.keyframes}
    assert by_frame[50] == 0.0 and by_frame[60] == 50.0
    assert emitted, "粘贴必须通过 track_changed 提交（带 undo）"


def test_paste_replaces_keyframe_at_same_frame(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"

    view._select_keyframe(f"param:{node_id}:{param}", 10)
    view.copy_selected_keyframes()
    view.set_current_frame(30)  # frame 30 已有关键帧
    view.paste_keyframes()

    track = panel._model.track_for(f"param:{node_id}:{param}")
    assert _track_frames(panel, node_id, param) == [10, 20, 30]
    kf30 = next(kf for kf in track.keyframes if kf.frame == 30)
    assert kf30.value == 0.0, "粘贴应覆盖同帧关键帧的值"


def test_ctrl_c_ctrl_v_keyboard(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"
    view._select_keyframe(f"param:{node_id}:{param}", 20)

    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_C,
                                 Qt.KeyboardModifier.ControlModifier))
    assert view.has_clipboard()

    view.set_current_frame(80)
    view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_V,
                                 Qt.KeyboardModifier.ControlModifier))
    assert 80 in _track_frames(panel, node_id, param)


# ── 4. 插值类型 ─────────────────────────────────────────────────────────

def test_set_selected_interpolation_hold(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"
    view._select_keyframe(f"param:{node_id}:{param}", 20)

    emitted: list = []
    panel.track_changed.connect(lambda nid, p, kfs: emitted.append(kfs))
    assert view.set_selected_interpolation("hold") is True

    track = panel._model.track_for(f"param:{node_id}:{param}")
    kf20 = next(kf for kf in track.keyframes if kf.frame == 20)
    kf10 = next(kf for kf in track.keyframes if kf.frame == 10)
    assert kf20.interpolation == "hold"
    assert kf10.interpolation == "linear", "未选中的关键帧不受影响"
    assert emitted and emitted[0][1]["interpolation"] == "hold"


def test_selected_interpolation_reports_common_mode(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    param = "translate_x"
    row_id = f"param:{node_id}:{param}"

    view._select_keyframe(row_id, 10)
    view._select_keyframe(row_id, 20, additive=True)
    assert view.selected_interpolation() == "linear"

    view.set_selected_interpolation("bezier")
    assert view.selected_interpolation() == "bezier"

    # 混选（bezier + linear）→ None
    view._select_keyframe(row_id, 30, additive=True)
    assert view.selected_interpolation() is None


def test_invalid_interpolation_rejected(panel):
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    view._select_keyframe(f"param:{node_id}:translate_x", 10)
    assert view.set_selected_interpolation("banana") is False


@pytest.mark.parametrize("mode", ["ease_in", "ease_out", "ease_in_out"])
def test_ease_interpolation_modes_are_supported(panel, mode):
    """The timeline menu/API exposes every interpolation the editor supports."""
    view = panel._timeline_view
    node_id = panel._model.rows[0].node_id
    row_id = f"param:{node_id}:translate_x"
    view._select_keyframe(row_id, 20)

    assert view.set_selected_interpolation(mode) is True
    assert view.selected_interpolation() == mode


# ── 5. CompositePage：手柄跟随动画 + 拖动写关键帧 ───────────────────────

def test_evaluated_node_params_overlay_animation(qapp):
    page = CompositePage()
    g, node, param = _make_graph_with_track(kfs=((0, 0.0), (100, 100.0)))
    params = page._evaluated_node_params(node, 50)
    assert params[param] == pytest.approx(50.0), "播放头处应取插值"
    assert params[param] != node.parameters[param], "静态值被动画覆盖"

    # 无动画节点 → 原样返回
    plain = TransformNode()
    assert page._evaluated_node_params(plain, 50) == dict(plain.parameters)


def test_write_interaction_keyframe_updates_track(qapp):
    page = CompositePage()
    g, node, param = _make_graph_with_track(kfs=((0, 0.0), (100, 100.0)))

    # 已有帧上更新值（保留插值字段）
    page._write_interaction_keyframe(node, param, 100, 321.0)
    kfs = node.keyframe_tracks[param]["keyframes"]
    kf100 = next(k for k in kfs if k["frame"] == 100)
    assert kf100["value"] == 321.0

    # 新帧上插入关键点
    page._write_interaction_keyframe(node, param, 40, 7.0)
    kfs = node.keyframe_tracks[param]["keyframes"]
    assert sorted(k["frame"] for k in kfs) == [0, 40, 100]

    # 求值跟随新关键帧 → 手柄/画面能体现拖动结果
    assert page._evaluated_node_params(node, 40)[param] == pytest.approx(7.0)


def test_flush_interaction_routes_animated_params_to_keyframes(qapp):
    """拖动已动画参数时写入当前帧关键帧，而不是无效的静态参数。"""
    page = CompositePage()
    g, node, param = _make_graph_with_track(kfs=((0, 0.0), (100, 100.0)))
    page._node_graph = g
    page._composite_current_frame = 20
    page._pending_interaction_update = {
        "node_id": node.id,
        "params": {param: 42.0, "rotation": 15.0},
    }

    page._flush_interaction_update()

    kfs = node.keyframe_tracks[param]["keyframes"]
    assert any(k["frame"] == 20 and k["value"] == 42.0 for k in kfs), \
        "动画参数应写入当前帧关键帧"
    assert node.parameters["rotation"] == 15.0, "非动画参数走静态写入"
