"""Retime header gestures and segment menu dispatch through the real Qt view."""
from dataclasses import replace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMenu

from src.features.edit.core.clip_params import SpeedParams
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


def _view(qtbot: Any, speed: SpeedParams = SpeedParams()) -> TimelineView:
    clip = ClipInstance('c', 'source', 'v', 50, 100, 100, 200, speed=speed,
        _source_fps_num=48, _source_fps_den=1, _timeline_fps_num=24, _timeline_fps_den=1)
    sequence = TimelineSequence('retime', 'Retime', fps=Rational(24, 1),
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(clip,)),))
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1200, 600)
    view.show()
    view.load_timeline(sequence)
    view.set_zoom(3)
    view._on_retime_controls_toggled('c', 'v', True)
    QTest.qWait(5)
    return view


def _drag(view: TimelineView, delta: int, cancel: str = '') -> None:
    item = view._items_map['c']
    point = view.mapFromScene(item.mapToScene(QPointF(item._width - 3, 10)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert item._drag_mode == 'retime_stretch'
    QTest.mouseMove(view.viewport(), point + QPoint(delta, 0), 10)
    assert not view._command_stack._undo_stack
    if cancel == 'escape':
        QTest.keyClick(view, Qt.Key.Key_Escape)
    elif cancel == 'close':
        view._on_retime_controls_toggled('c', 'v', False)
    elif cancel == 'return':
        QTest.mouseMove(view.viewport(), point, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(delta, 0))


def test_header_drag_preserves_source_and_speed_flags_and_undo(qtbot: Any) -> None:
    view = _view(qtbot, SpeedParams(speed=1, reverse=True, preserve_pitch=True))
    before = view.get_timeline()
    _drag(view, -150)
    clip = view.get_timeline().get_clip_by_id('c')
    assert clip.timeline_duration == 50
    assert clip.speed.speed == 2
    assert clip.speed.reverse and clip.speed.preserve_pitch
    assert (clip.timeline_start, clip.source_start, clip.source_duration) == (50, 100, 200)
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id('c') == before.get_clip_by_id('c')


@pytest.mark.parametrize('cancel', ['escape', 'close', 'return'])
def test_header_drag_cancellation_restores_geometry_and_speed(qtbot: Any, cancel: str) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    _drag(view, 150, cancel)
    item = view._items_map['c']
    assert view.get_timeline() == before
    assert (item._duration_frames, item._speed) == (100, 1)
    assert not view._command_stack._undo_stack


def test_advanced_header_drag_requests_backend_without_edit(qtbot: Any) -> None:
    params = SpeedParams().with_speed_point(100, 200)
    view = _view(qtbot, params)
    before = view.get_timeline()
    requests = []
    view.clip_speed_settings_requested.connect(lambda *args: requests.append(args))
    _drag(view, 150)
    assert view.get_timeline() == before
    assert not view._command_stack._undo_stack
    assert requests[0][2]['action'] == 'stretch'
    assert view._items_map['c']._duration_frames == 100


def test_segment_menu_is_grouped_and_cancel_is_noop(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view(qtbot)
    item = view._items_map['c']
    seen = []
    def inspect(menu: QMenu, _pos: Any) -> None:
        seen.extend(a.objectName() for a in menu.actions() if a.objectName())
        groups = [a.menu() for a in menu.actions() if a.menu()]
        assert len(groups) == 3
        assert len(groups[0].actions()) == 12
        assert not next(a for a in menu.actions() if a.objectName() == 'retime_clear_point').isEnabled()
        assert next(a for a in menu.actions() if a.objectName() == 'retime_pitch').isCheckable()
        return None
    monkeypatch.setattr(QMenu, 'exec', inspect)
    item._show_retime_segment_speed_menu(QPoint(10, 10), item._width / 2)
    assert {'retime_add_point', 'retime_freeze', 'retime_reverse', 'retime_reset_clip'} <= set(seen)
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize('key', ['rewind', 'ramp_up'])
def test_advanced_segment_menu_request_preserves_intent(qtbot: Any, key: str) -> None:
    """freeze 自 R10 起是已支持事务（SetClipFreezeCommand），不再转发——
    见 test_retime_stage_advanced。"""
    view = _view(qtbot)
    before = view.get_timeline()
    requests = []
    view.clip_speed_settings_requested.connect(lambda *args: requests.append(args))
    view._on_retime_action_requested('c', 'v', key, 25, 2.0)
    assert requests == [('c', 'v', {'operation': 'retime_controls', 'action': key, 'source_offset': 25, 'value': 2.0})]
    assert view.get_timeline() == before


def test_clear_point_executes_single_point_removal(qtbot: Any) -> None:
    """B14 stage-3：clear_point 是已支持能力——单点删除并入前段速度，
    不再发高级请求（回归见 test_speed_settings_reception）。"""
    from src.features.edit.core.clip_params import SpeedPoint
    view = _view(qtbot, SpeedParams(speed=1.0, speed_points=(
        SpeedPoint(source_offset=25, speed=2.0),)))
    requests = []
    view.clip_speed_settings_requested.connect(lambda *args: requests.append(args))
    view._on_retime_action_requested('c', 'v', 'clear_point', 25, 2.0)
    assert requests == []  # 不发请求：本地执行
    assert view.get_timeline().get_clip_by_id('c').speed.speed_points == ()
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    restored = view.get_timeline().get_clip_by_id('c').speed.speed_points
    assert [p.source_offset for p in restored] == [25]


def test_basic_menu_reverse_and_pitch_refresh_and_undo(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_retime_action_requested('c', 'v', 'reverse', 0, None)
    assert view.get_timeline().get_clip_by_id('c').speed.reverse
    view._on_retime_action_requested('c', 'v', 'pitch', 0, True)
    assert view._items_map['c']._retime_preserve_pitch
    view.undo()
    assert not view._items_map['c']._retime_preserve_pitch
    assert view._items_map['c']._retime_reverse


def test_retime_expands_upward_once_and_restores_saved_height(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_retime_controls_toggled('c', 'v', False)
    view._on_track_height_changed('v', 96)
    item = view._items_map['c']
    original_bottom = item.sceneBoundingRect().bottom()
    original_top = item.y()
    stored = dict(view._layout_state.track_heights)
    before = view.get_timeline()
    view._on_retime_controls_toggled('c', 'v', True)
    assert item._track_height == 120
    assert item.y() == original_top - 24
    assert item.sceneBoundingRect().bottom() == original_bottom
    view._on_retime_controls_toggled('c', 'v', True)
    assert item._track_height == 120
    assert view._layout_state.track_heights == stored
    view._on_retime_controls_toggled('c', 'v', False)
    assert item._track_height == 96
    assert item.y() == original_top
    assert view.get_timeline() == before
    assert not view._command_stack._undo_stack


def test_only_open_clip_expands_on_shared_track(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_retime_controls_toggled('c', 'v', False)
    timeline = view.get_timeline()
    track = timeline.video_tracks[0]
    neighbor = replace(track.clips[0], clip_id='neighbor', timeline_start=300)
    view.load_timeline(replace(timeline, video_tracks=(replace(track, clips=(*track.clips, neighbor)),)))
    first, second = view._items_map['c'], view._items_map['neighbor']
    base = first._track_height
    neighbor_bottom = second.sceneBoundingRect().bottom()
    view._on_retime_controls_toggled('c', 'v', True)
    assert first._track_height == base + 24
    assert second._track_height == base
    assert second.sceneBoundingRect().bottom() == neighbor_bottom
    view._on_retime_controls_toggled('neighbor', 'v', True)
    assert first._track_height == second._track_height == base + 24
    view._on_retime_controls_toggled('c', 'v', False)
    assert first._track_height == base
    assert second._track_height == base + 24


def test_bottom_triangle_opens_real_menu_and_reverse_dispatches(qtbot: Any) -> None:
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication
    view = _view(qtbot)
    item = view._items_map['c']
    point = view.mapFromScene(item.mapToScene(QPointF(item._width / 2, item._track_height - 9)))
    observed = []
    def choose_reverse() -> None:
        menu = QApplication.activePopupWidget()
        if isinstance(menu, QMenu):
            observed.append(menu.objectName())
            action = next(a for a in menu.actions() if a.objectName() == 'retime_reverse')
            QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
    chooser = QTimer(view)
    chooser.setSingleShot(True)
    chooser.timeout.connect(choose_reverse)
    chooser.start(20)
    # Stop the safety timer after the menu exits so it cannot leak into another test.
    safety = QTimer(view)
    safety.setSingleShot(True)
    safety.timeout.connect(lambda: QApplication.activePopupWidget().close()
                           if QApplication.activePopupWidget() else None)
    safety.start(200)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    safety.stop()
    assert QApplication.activePopupWidget() is None
    assert QApplication.mouseButtons() == Qt.MouseButton.NoButton
    assert observed == ['retimeSegmentMenu']
    assert view.get_timeline().get_clip_by_id('c').speed.reverse
    assert len(view._command_stack._undo_stack) == 1


def test_retime_header_wins_over_shared_cut_handle(qtbot: Any) -> None:
    view = _view(qtbot)
    timeline = view.get_timeline()
    track = timeline.video_tracks[0]
    neighbor = replace(track.clips[0], clip_id='neighbor', timeline_start=150)
    view.load_timeline(replace(timeline, video_tracks=(replace(track, clips=(*track.clips, neighbor)),)))
    item = view._items_map['c']
    point = view.mapFromScene(item.mapToScene(QPointF(item._width - 1, 21)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert item._drag_mode == 'retime_stretch'
    QTest.mouseMove(view.viewport(), point + QPoint(-30, 0), 10)
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert not view._command_stack._undo_stack


def test_position_lock_blocks_retime_header_drag(qtbot: Any) -> None:
    view = _view(qtbot)
    before = view.get_timeline()
    view.set_position_lock_enabled(True)
    item = view._items_map['c']
    point = view.mapFromScene(item.mapToScene(QPointF(item._width / 2, 10)))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    QTest.mouseMove(view.viewport(), point + QPoint(50, 0), 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=point + QPoint(50, 0))
    assert item._drag_mode is None
    assert view.get_timeline() == before
    assert not view._command_stack._undo_stack


def test_retime_keeps_video_bottom_anchored_when_stack_overflows(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_retime_controls_toggled('c', 'v', False)
    timeline = view.get_timeline()
    track = timeline.video_tracks[0]
    second = replace(track, track_id='v2', index=1, clips=())
    view.load_timeline(replace(timeline, video_tracks=(track, second)))
    view._on_track_height_changed('v', 180)
    view._on_track_height_changed('v2', 180)
    view._align_video_stack_to_splitter()
    view._layout_track_items()
    item = view._items_map['c']
    bottom = item.sceneBoundingRect().bottom()
    view._on_retime_controls_toggled('c', 'v', True)
    assert item.sceneBoundingRect().bottom() == bottom
    view._on_retime_controls_toggled('c', 'v', False)
    assert item.sceneBoundingRect().bottom() == bottom


def test_smooth_ramp_menu_state_survives_initial_load_and_reload(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    params = SpeedParams().with_speed_point(100, 200).with_point_easing(100, 'smooth')
    view = _view(qtbot, params)
    for _ in range(2):
        item = view._items_map['c']
        observed = []
        def inspect(menu: QMenu, _pos: Any) -> None:
            ramp = next(a.menu() for a in menu.actions()
                        if a.menu() and any(child.objectName() == 'retime_smooth' for child in a.menu().actions()))
            smooth = next(a for a in ramp.actions() if a.objectName() == 'retime_smooth')
            observed.append(smooth.isChecked())
            return None
        monkeypatch.setattr(QMenu, 'exec', inspect)
        item._show_retime_segment_speed_menu(QPoint(1, 1), item._width * .75)
        assert observed == [True]
        view.load_timeline(view.get_timeline())


def test_sliced_curve_menu_uses_clip_relative_boundary(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.core.clip_params import SpeedPoint
    params = SpeedParams(speed=1, speed_points=(SpeedPoint(40, 2),), curve_offset=24, curve_length=120)
    view = _view(qtbot, params)
    timeline = view.get_timeline()
    track = timeline.video_tracks[0]
    clip = replace(track.clips[0], source_duration=80)
    view.load_timeline(replace(timeline, video_tracks=(replace(track, clips=(clip,)),)))
    item = view._items_map['c']
    assert item._retime_points == ((16, 2),)
    assert [segment[0] for segment in item._retime_segments()] == [0, 16]
    def choose_clear(menu: QMenu, _pos: Any) -> Any:
        return next(a for a in menu.actions() if a.objectName() == 'retime_clear_point')
    monkeypatch.setattr(QMenu, 'exec', choose_clear)
    item._show_retime_segment_speed_menu(QPoint(1, 1), item._width * .75)
    assert view.get_timeline().get_clip_by_id('c').speed.speed_points == ()
    view.undo()
    assert view._items_map['c']._retime_points == ((16, 2),)


def test_clear_speed_point_rejection_reports_locked_partner_without_exception(qtbot: Any) -> None:
    params = SpeedParams().with_speed_point(100, 200)
    view = _view(qtbot, params)
    timeline = view.get_timeline()
    video = replace(timeline.video_tracks[0].clips[0], linked_clip_id='audio')
    audio = replace(video, clip_id='audio', track_id='a', linked_clip_id='c')
    view.load_timeline(replace(timeline,
        video_tracks=(replace(timeline.video_tracks[0], clips=(video,)),),
        audio_tracks=(EditTrack('a', 'A1', 'audio', 0, clips=(audio,), is_locked=True),)))
    before = view.get_timeline()
    errors = []
    view.status_message_requested.connect(errors.append)
    view._on_retime_action_requested('c', 'v', 'clear_point', 100, None)
    assert errors
    assert view.get_timeline() == before
    assert not view._command_stack._undo_stack
