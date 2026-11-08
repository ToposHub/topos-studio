"""W03：Edit 域 Keyframe Tray 宿主与命令桥（契约 D5）。

- 托管/回写：EditPage 请求 → panel.set_keyframe_tray_visible → 实际
  可见性回写工具栏按钮（无"假打开"）。
- 投影零命令：选区/播放头/时间线变化只重绘参数轨。
- 命令桥：添加/删除单步 undo；时间拖动释放 = Remove+Add 宏单步 undo；
  Esc 取消零写入。坐标域为 timeline 帧。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)

pytestmark = pytest.mark.edit_ui


def _clip(clip_id: str = "kc", start: int = 0, duration: int = 60) -> ClipInstance:
    clip = ClipInstance(
        clip_id=clip_id, asset_id="a", track_id="v1",
        timeline_start=start, timeline_duration=duration,
        source_start=0, source_duration=duration,
    )
    clip = clip.with_keyframe_added("opacity", 10, 0.2)
    clip = clip.with_keyframe_added("opacity", 40, 0.8)
    return clip


def _timeline() -> TimelineSequence:
    return TimelineSequence(
        timeline_id="tray", name="Tray", width=64, height=48,
        fps=Rational(24, 1),
        video_tracks=(EditTrack(
            "v1", "V1", "video", 0, clips=(_clip(),)),),
        audio_tracks=(EditTrack("a1", "A1", "audio", 0),),
    )


def _panel(qtbot: Any) -> Any:
    from src.features.edit.ui.timeline_panel import TimelinePanel

    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.resize(1200, 460)
    panel.load_timeline(_timeline())
    return panel


def _tray(panel: Any) -> Any:
    panel.set_keyframe_tray_visible(True)
    return panel._keyframe_tray


def test_tray_hosting_writes_back_button(qtbot: Any,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.edit_page import EditPage

    monkeypatch.setattr(EditPage, "_ensure_audio_manager", lambda self: None)
    page = EditPage()
    qtbot.addWidget(page)
    panel = _panel(qtbot)
    page.set_timeline_panel(panel)
    qtbot.addWidget(panel)

    emitted: list[bool] = []
    panel.keyframe_tray_requested.connect(emitted.append)
    # 用户点击 → 请求 → host 提供面板 → 回写（不再发第二条请求）
    page._on_timeline_keyframe_tray_requested(True)
    assert panel.is_keyframe_tray_visible() is True
    assert panel.btn_keyframe_tray.isChecked() is True
    assert emitted == []  # 回写用 blocker，无递归

    page._on_timeline_keyframe_tray_requested(False)
    assert panel.is_keyframe_tray_visible() is False
    assert panel.btn_keyframe_tray.isChecked() is False


def test_tray_projects_animated_params_without_commands(qtbot: Any) -> None:
    panel = _panel(qtbot)
    tray = _tray(panel)
    view = panel.get_timeline_view()
    view.set_selection(_selection("kc"))
    tray.refresh_from_model()
    assert tray._clip_id == "kc"
    assert [name for name, _keys in tray._lanes] == ["opacity"]
    assert [frame for frame, _v in tray._lanes[0][1]] == [10, 40]
    assert not view._command_stack._undo_stack  # 投影零命令


def test_tray_playhead_clip_fallback(qtbot: Any) -> None:
    panel = _panel(qtbot)
    tray = _tray(panel)
    view = panel.get_timeline_view()
    view._playhead_frame = 20  # kc: [0, 60)
    tray.refresh_from_model()
    assert tray._clip_id == "kc"


def test_add_and_remove_keyframe_single_undo(qtbot: Any) -> None:
    panel = _panel(qtbot)
    tray = _tray(panel)
    view = panel.get_timeline_view()
    view.set_selection(_selection("kc"))
    tray.refresh_from_model()

    tray._add_keyframe("opacity", 25)
    clip = view.get_timeline().get_clip_by_id("kc")
    assert clip.animated_params["opacity"].has_keyframe_at(25)
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert not view.get_timeline().get_clip_by_id(
        "kc").animated_params["opacity"].has_keyframe_at(25)

    tray._remove_keyframe("opacity", 10)
    assert not view.get_timeline().get_clip_by_id(
        "kc").animated_params["opacity"].has_keyframe_at(10)
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id(
        "kc").animated_params["opacity"].has_keyframe_at(10)


def test_drag_move_commits_macro_single_undo(qtbot: Any) -> None:
    panel = _panel(qtbot)
    tray = _tray(panel)
    view = panel.get_timeline_view()
    view.set_selection(_selection("kc"))
    tray.refresh_from_model()

    tray._drag_param = "opacity"
    tray._drag_from_frame = 10
    tray._drag_to_frame = 30
    tray._drag_value = 0.2
    tray._commit_drag()

    clip = view.get_timeline().get_clip_by_id("kc")
    param = clip.animated_params["opacity"]
    assert not param.has_keyframe_at(10)
    assert param.has_keyframe_at(30)
    assert param.has_keyframe_at(40)  # 未动的关键帧保持
    assert len(view._command_stack._undo_stack) == 1  # 宏 = 单步
    view.undo()
    param = view.get_timeline().get_clip_by_id("kc").animated_params["opacity"]
    assert param.has_keyframe_at(10)
    assert not param.has_keyframe_at(30)


def test_escape_cancels_drag_zero_writes(qtbot: Any) -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    panel = _panel(qtbot)
    tray = _tray(panel)
    view = panel.get_timeline_view()
    view.set_selection(_selection("kc"))
    tray.refresh_from_model()
    tray.setFocus()
    tray._drag_param = "opacity"
    tray._drag_from_frame = 10
    tray._drag_to_frame = 30
    tray._drag_value = 0.2
    QTest.keyClick(tray, Qt.Key.Key_Escape)
    assert tray._drag_param is None
    assert not view._command_stack._undo_stack
    clip = view.get_timeline().get_clip_by_id("kc")
    assert clip.animated_params["opacity"].has_keyframe_at(10)


def _selection(clip_id: str) -> Any:
    from src.features.edit.core.selection import TimelineSelection

    return TimelineSelection().with_clip_selected(clip_id, "v1", "tray")
