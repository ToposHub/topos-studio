"""AE 风格 Keyframes 面板交互测试。

覆盖四项优化：
  1. 滚轮缩放时间线刻度（以光标为锚点，方便逐帧操作）
  2. 鼠标中键拖拽平移时间线
  3. 范围播放条（work area）+ I/O 键设置播放范围
  4. 素材图层条：每个节点都显示图层条，拖拽调节长度控制播放范围
"""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest

from PyQt6.QtCore import QPoint, QPointF, Qt, QEvent
from PyQt6.QtGui import QMouseEvent, QWheelEvent

from src.app.controllers.page_controller import _KeyframesTimelineTransport
from src.features.composite.core.node_engine import NodeGraph
from src.features.composite.nodes.transform_node import TransformNode
from src.features.composite.ui.composite_page import CompositePage
from src.features.composite.ui.keyframes_panel import KeyframesPanel
from src.shared.keyframe import Keyframe, KeyframeTrack


CLIP_FRAMES = 100
CLIP_FPS = 24.0
CLIP_START = 1000  # comp clip 在时间线上的起始帧（全局坐标）


@pytest.fixture
def graph():
    g = NodeGraph()
    g.add_node(TransformNode())
    return g


@pytest.fixture
def panel(qapp, graph):
    p = KeyframesPanel()
    p.resize(1000, 300)
    p.set_node_graph(graph)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    return p


def _wheel_at(view, x, y, delta=120, mods=Qt.KeyboardModifier.NoModifier):
    ev = QWheelEvent(QPointF(x, y), QPointF(x, y),
                     QPoint(0, 0), QPoint(0, delta),
                     Qt.MouseButton.NoButton, mods,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)


def _mouse(view, etype, x, y, button, buttons=Qt.MouseButton.NoButton):
    ev = QMouseEvent(etype, QPointF(x, y), button, buttons,
                     Qt.KeyboardModifier.NoModifier)
    view.mousePressEvent(ev) if etype == QEvent.Type.MouseButtonPress \
        else view.mouseMoveEvent(ev) if etype == QEvent.Type.MouseMove \
        else view.mouseReleaseEvent(ev)


# ── 1. 滚轮缩放 ─────────────────────────────────────────────────────────

def test_plain_wheel_zooms_timeline_view(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.pixels_per_frame
    _wheel_at(view, 200, 100, delta=120)
    assert panel._frame_mapper.pixels_per_frame > before, "滚轮应放大刻度"
    _wheel_at(view, 200, 100, delta=-120)
    _wheel_at(view, 200, 100, delta=-120)
    assert panel._frame_mapper.pixels_per_frame < before, "反向滚轮应缩小"


def test_wheel_zoom_anchored_at_cursor(panel):
    """缩放时鼠标下的帧保持不动（逐帧精调的关键）。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    x = 250.0
    anchor_frame = fm.x_to_frame(x)
    _wheel_at(view, x, 100, delta=120)
    assert fm.x_to_frame(x) == pytest.approx(anchor_frame, abs=0.01)


def test_wheel_zoom_clamped_for_frame_precision(panel):
    """可放大到足够逐帧操作的刻度（每帧几十像素）。"""
    view = panel._timeline_view
    for _ in range(80):
        _wheel_at(view, 100, 100, delta=120)
    assert panel._frame_mapper.pixels_per_frame >= 32.0
    assert panel._frame_mapper.pixels_per_frame <= 256.0


def test_shift_wheel_pans_horizontally(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.scroll_offset
    _wheel_at(view, 100, 100, delta=120,
              mods=Qt.KeyboardModifier.ShiftModifier)
    assert panel._frame_mapper.scroll_offset != before


def test_alt_wheel_bubbles_for_vertical_scroll(panel):
    """Alt+滚轮不在轨道侧消费 → 冒泡给面板做垂直滚动。"""
    view = panel._timeline_view
    ev = QWheelEvent(QPointF(100, 100), QPointF(100, 100),
                     QPoint(0, 0), QPoint(0, -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.AltModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    view.wheelEvent(ev)
    assert not ev.isAccepted()


# ── 2. 中键平移 ─────────────────────────────────────────────────────────

def test_middle_button_drag_pans(panel):
    view = panel._timeline_view
    before = panel._frame_mapper.scroll_offset
    _mouse(view, QEvent.Type.MouseButtonPress, 300, 100,
           Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseMove, 380, 100,
           Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 380, 100,
           Qt.MouseButton.MiddleButton)
    delta = panel._frame_mapper.scroll_offset - before
    assert delta == pytest.approx(80.0), "中键拖拽应 1:1 平移刻度"


def test_middle_pan_does_not_move_playhead(panel):
    frames: list[int] = []
    panel.current_frame_changed.connect(frames.append)
    view = panel._timeline_view
    _mouse(view, QEvent.Type.MouseButtonPress, 300, 100,
           Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseMove, 400, 100,
           Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, 400, 100,
           Qt.MouseButton.MiddleButton)
    assert frames == [], "中键平移绝不能触发 seek"


# ── 3. 范围播放条 + I/O 键 ──────────────────────────────────────────────

def test_io_keys_set_play_range_at_playhead(panel):
    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))

    panel.set_current_frame(20)
    panel.set_play_in_at_playhead()      # I
    panel.set_current_frame(60)
    panel.set_play_out_at_playhead()     # O

    assert panel.effective_play_range() == (20, 60)
    assert (20, 60) in ranges
    # 视图上的工作区条同步
    assert panel._timeline_view.play_range() == (20, 60)
    # 底部标签显示 In/Out
    assert "20-60" in panel._range_label.text()


def test_shortcut_i_o_registered(panel):
    """I/O 快捷键确实注册在面板上（WidgetWithChildrenShortcut）。"""
    assert panel._shortcut_set_in.key().toString() == "I"
    assert panel._shortcut_set_out.key().toString() == "O"


def test_composite_shift_z_fits_only_active_composition(qapp):
    """Composite 的 Shift+Z 只在本页有打开合成时调用 Fit。"""
    page = CompositePage()
    calls: list[bool] = []
    page._keyframes_panel = SimpleNamespace(
        fit_time_range=lambda: calls.append(True),
    )

    page._is_active = True
    page.set_composition_active(True)

    assert page._fit_composition_shortcut.key().toString() == "Shift+Z"
    assert page._fit_composition_shortcut.isEnabled()
    page._fit_keyframes_to_composition()
    assert calls == [True]

    page._is_active = False
    page._sync_fit_composition_shortcut()
    assert not page._fit_composition_shortcut.isEnabled()


def test_internal_playback_loops_inside_work_area(panel):
    frames: list[int] = []
    panel.current_frame_changed.connect(frames.append)

    panel.set_play_range(10, 13)
    panel.set_current_frame(10)
    panel._internal_set_playing(True)
    for _ in range(6):
        panel._on_internal_play_tick()
    panel._internal_set_playing(False)

    # 10 → 11,12,13,10,11,12 —— 只在 In/Out 内循环
    assert frames == [11, 12, 13, 10, 11, 12]


def test_workarea_band_drag_updates_range(panel):
    """拖拽标尺上的工作区 Out 边缘 → 范围实时更新。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    panel.set_play_range(20, 60)

    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))

    x_out = fm.frame_to_x(61)  # end+1 边缘
    _mouse(view, QEvent.Type.MouseButtonPress, x_out, 2,
           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseMove, fm.frame_to_x(71), 2,
           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton)
    _mouse(view, QEvent.Type.MouseButtonRelease, fm.frame_to_x(71), 2,
           Qt.MouseButton.LeftButton)

    assert panel.effective_play_range() == (20, 71)
    assert ranges[-1] == (20, 71)


def test_clear_play_range_restores_full_clip(panel):
    ranges: list[tuple[int, int]] = []
    panel.play_range_changed.connect(lambda i, o: ranges.append((i, o)))
    panel.set_play_range(20, 60)
    panel.clear_play_range()
    assert panel._timeline_view.play_range() is None
    assert panel.effective_play_range() == (0, CLIP_FRAMES - 1)
    assert ranges[-1] == (0, CLIP_FRAMES - 1)


def test_page_forwards_play_range_to_transport(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)

    class FakeController:
        is_playing = False
        playback_range = None

        def play(self):
            pass

        def pause(self):
            pass

        def set_playback_range(self, s, e):
            self.playback_range = (s, e)

    ctrl = FakeController()
    page.set_keyframes_transport(
        _KeyframesTimelineTransport(ctrl, CLIP_START, CLIP_FRAMES))

    panel.set_play_range(10, 40)

    # clip 本地帧 → 时间线全局帧
    assert ctrl.playback_range == (CLIP_START + 10, CLIP_START + 40)
    # play() 使用工作区范围而不是整个 clip
    page._keyframes_transport.play()
    assert ctrl.playback_range == (CLIP_START + 10, CLIP_START + 40)


def test_no_transport_play_range_falls_back_to_comp_timer(qapp):
    page = CompositePage()
    page._composite_duration_frames = CLIP_FRAMES
    panel = KeyframesPanel()
    panel.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    page.set_keyframes_panel(panel)
    page.set_keyframes_transport(None)

    panel.set_play_range(5, 25)
    assert page._comp_in_frame == 5
    assert page._comp_out_frame == 25


# ── 4. 素材图层条 ───────────────────────────────────────────────────────

def test_every_node_shows_layer_bar(panel, graph):
    """无 time_range 的节点也显示横跨整个合成的图层条（AE 风格）。"""
    node_id = panel._model.rows[0].node_id
    track = panel._model.track_for(f"node:{node_id}")
    assert track is not None and track.range_bar is not None
    assert track.range_bar.default is True
    assert track.range_bar.start_frame == 0
    assert track.range_bar.end_frame == CLIP_FRAMES - 1


def test_trim_default_layer_bar_writes_time_range(panel, graph):
    """拖拽默认图层条边缘 → 节点获得显式 time_range（控制播放范围）。"""
    node_id = panel._model.rows[0].node_id
    node = graph.get_node(node_id)
    assert node.time_range is None

    emitted: list[tuple[str, dict]] = []
    panel.range_changed.connect(lambda nid, tr: emitted.append((nid, tr)))

    # 模拟视图侧的提交（拖拽 Out 边缘到 70）
    track = panel._model.track_for(f"node:{node_id}")
    track.range_bar.start_frame = 10
    track.range_bar.end_frame = 70
    view = panel._timeline_view
    view._drag_mode = "range_edge"
    view._drag_row_id = f"node:{node_id}"
    view._commit_drag_edit()

    assert emitted == [(node_id, {
        "comp_start": 10, "comp_end": 70, "enabled": True})]


def test_page_merges_time_range_preserving_source_keys(qapp, graph):
    """CompositePage 合并写入：trim 不丢 source_in/source_out。"""
    page = CompositePage()
    page._node_graph = graph
    node = graph.nodes[next(iter(graph.nodes))]
    node.time_range = {"source_in": 5, "source_out": 200, "offset": 0}

    page._on_keyframes_panel_time_range_changed(
        node.id, {"comp_start": 10, "comp_end": 70, "enabled": True})

    assert node.time_range["comp_start"] == 10
    assert node.time_range["comp_end"] == 70
    assert node.time_range["enabled"] is True
    assert node.time_range["source_in"] == 5, "原有 source_in 必须保留"
    assert node.time_range["source_out"] == 200


def test_explicit_time_range_bar_not_default(qapp):
    """已有 time_range 的节点：图层条反映显式范围且 default=False。"""
    g = NodeGraph()
    t = TransformNode()
    t.time_range = {"comp_start": 3, "comp_end": 40, "enabled": True}
    g.add_node(t)
    p = KeyframesPanel()
    p.resize(800, 300)
    p.set_frame_range(0, CLIP_FRAMES - 1, fps=CLIP_FPS)
    p.set_node_graph(g)

    track = p._model.track_for(f"node:{t.id}")
    assert track.range_bar.default is False
    assert track.range_bar.start_frame == 3
    assert track.range_bar.end_frame == 40


# ── 6. 刻度条（自适应刻度 + 帧数/时间码双模式） ─────────────────────

def test_tick_steps_adapt_to_zoom(panel):
    """任意缩放级别下，主刻度间距足够宽，标签永不重叠。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    for ppf in (0.05, 0.2, 1.0, 2.0, 8.0, 32.0, 256.0):
        fm.set_zoom(ppf)
        minor, major = view._tick_steps()
        assert 1 <= minor <= major, f"ppf={ppf}: 步长非法"
        if major > 1:
            assert major % minor == 0, f"ppf={ppf}: 次刻度需整除主刻度"
        # 主刻度(带标签)像素间距必须 >= 最小间隔
        assert major * ppf >= 50.0 - 1e-6, \
            f"ppf={ppf}: 主刻度间距 {major * ppf:.1f}px 过密"


def test_tick_steps_follow_125_series(panel):
    """主刻度步长应为 1/2/5×10^k（剪辑页风格的整齐刻度）。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    for ppf in (0.1, 0.7, 3.0, 20.0):
        fm.set_zoom(ppf)
        _minor, major = view._tick_steps()
        # 剥离 10^k 后尾数只能是 1/2/5
        m = major
        while m % 10 == 0:
            m //= 10
        assert m in (1, 2, 5), f"ppf={ppf}: major={major} 不在 1-2-5 系列"


def test_ruler_labels_frames_and_timecode(panel):
    """刻度标签支持帧数 / 时间码两种显示方式。"""
    view = panel._timeline_view
    view.set_display("frame", CLIP_FPS)
    assert view._ruler_label(96) == "96"

    view.set_display("timecode", CLIP_FPS)
    assert view._ruler_label(0) == "00:00:00:00"
    assert view._ruler_label(24) == "00:00:01:00"      # 24fps → 1 秒
    assert view._ruler_label(24 * 60 + 12) == "00:01:00:12"


def test_timecode_mode_widens_tick_spacing(panel):
    """时间码标签更宽，主刻度间距要求也更大，避免重叠。"""
    view = panel._timeline_view
    fm = panel._frame_mapper
    fm.set_zoom(2.0)
    view.set_display("frame", CLIP_FPS)
    _m1, major_frame = view._tick_steps()
    view.set_display("timecode", CLIP_FPS)
    _m2, major_tc = view._tick_steps()
    assert major_tc >= major_frame


def test_context_menu_switch_syncs_footer_combo(panel):
    """时间轴右键切换刻度模式后，底部单位下拉框同步更新。"""
    view = panel._timeline_view
    view._set_display_mode("timecode")
    assert panel._state.display_mode == "timecode"
    assert panel._time_unit_combo.currentIndex() == 3
    view._set_display_mode("frame")
    assert panel._time_unit_combo.currentIndex() == 1


def test_footer_combo_switches_ruler_mode(panel):
    """底部下拉框选 Timecode 后，刻度标签跟随切换。"""
    panel._time_unit_combo.setCurrentIndex(3)  # Timecode
    view = panel._timeline_view
    assert view._display_mode() == "timecode"
    assert view._ruler_label(48) == "00:00:02:00"
    panel._time_unit_combo.setCurrentIndex(1)  # Frame
    assert view._display_mode() == "frame"
    assert view._ruler_label(48) == "48"


def test_paint_draws_grid_before_rows(panel):
    """网格必须在图层条之前绘制（画在图层下方，不再叠在上面）。"""
    import inspect
    src = inspect.getsource(panel._timeline_view.paintEvent)
    gi = src.index("_draw_grid")
    ri = src.index("_draw_row")
    assert gi < ri, "_draw_grid 必须出现在 _draw_row 之前"
