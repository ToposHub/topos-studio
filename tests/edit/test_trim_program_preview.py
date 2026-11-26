"""Program Viewer scrubbing during timeline trim gestures."""

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.edit_page import EditPage
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_panel import TimelinePanel

pytestmark = pytest.mark.edit_ui


@pytest.mark.parametrize("end_gesture", ["release", "escape", "hide"])
@pytest.mark.parametrize("delta,preview_frame", [(-20, 129), (20, 149)])
def test_edge_trim_uses_program_preview_and_returns_to_playhead(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, end_gesture: str, delta: int, preview_frame: int,
) -> None:
    panel = TimelinePanel()
    page = EditPage()
    qtbot.addWidget(panel)
    qtbot.addWidget(page)
    panel.resize(1100, 500)
    panel.show()
    panel.load_timeline(TimelineSequence("trim", "Trim", fps=24, video_tracks=(
        EditTrack("v", "V1", "video", 0, clips=(
            ClipInstance("clip", "asset", "v", 50, 100, 100, 100),
        )),
    )))
    page.set_timeline_panel(panel)
    view = panel.get_timeline_view()
    view.set_tool(EditTool.SELECT)
    view.set_zoom(3)
    view.set_snap_enabled(False)
    view.set_playhead(65)
    requests: list[int] = []
    finished: list[tuple[int, int]] = []
    switches: list[bool] = []
    subtitle_frames: list[int] = []
    pauses: list[bool] = []
    monkeypatch.setattr(page, "_update_subtitle_overlays", subtitle_frames.append)
    monkeypatch.setattr(panel, "is_playing", lambda: not pauses)
    monkeypatch.setattr(panel, "pause_playback", lambda: pauses.append(True))

    def show_program(*, focus: bool) -> None:
        switches.append(focus)
        viewport.active_monitor = "program"

    viewport = SimpleNamespace(dual_view=False, active_monitor="source", show_program=show_program)
    page._main_window = SimpleNamespace(current_page="Edit", edit_viewport=viewport)
    page.timeline_scrub_frame_requested.connect(requests.append)
    page.timeline_scrub_finished.connect(
        lambda frame: finished.append((frame, view.get_timeline().get_clip_by_id("clip").timeline_duration))
    )

    item = view._items_map["clip"]
    item._max_source_frames = 1000
    QTest.qWait(5)
    point = view.mapFromScene(item.mapToScene(item._width - 3, 32))
    target = point + QPoint(3 * delta, 0)
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert item._drag_mode == "trim_right"
    QTest.mouseMove(view.viewport(), target, 10)
    assert requests == [preview_frame]
    assert subtitle_frames == [preview_frame]
    assert pauses == [True]
    assert switches == [False]
    assert view._playhead_frame == 65
    assert view.get_timeline().get_clip_by_id("clip").timeline_duration == 100

    if end_gesture == "escape":
        QTest.keyClick(view, Qt.Key.Key_Escape)
    elif end_gesture == "hide":
        panel.hide()
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert finished == [(65, 100 + delta if end_gesture == "release" else 100)]
    assert subtitle_frames == [preview_frame, 65]
    assert view._interaction.is_idle
    assert view._command_stack.can_undo() == (end_gesture == "release")


def test_dynamic_trim_keys_reach_view_before_panel_transport_shortcuts(qtbot: Any) -> None:
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    panel.resize(1000, 500)
    panel.show()
    panel.activateWindow()
    sequence = TimelineSequence('keys', 'Keys', fps=24, video_tracks=(
        EditTrack('v', 'V1', 'video', 0, clips=(
            ClipInstance('left', 'asset', 'v', 0, 100, 0, 100),
            ClipInstance('right', 'asset', 'v', 100, 100, 100, 100),
        )),))
    panel.load_timeline(sequence)
    view = panel.get_timeline_view()
    view.setFocus()
    QTest.qWait(5)
    transport_shortcuts = []
    panel._shortcut_l.activated.connect(lambda: transport_shortcuts.append('L'))
    panel._shortcut_j.activated.connect(lambda: transport_shortcuts.append('J'))
    panel._shortcut_k.activated.connect(lambda: transport_shortcuts.append('K'))
    for item in view._items_map.values():
        item._max_source_frames = 1000
    view._on_roll_trim_started('left', 'right', 100)
    QTest.keyClick(view, Qt.Key.Key_L)
    assert view._roll_preview_state['delta'] == 1
    QTest.keyClick(view, Qt.Key.Key_J)
    assert view._roll_preview_state['delta'] == 0
    QTest.keyClick(view, Qt.Key.Key_K)
    assert view._roll_preview_state['delta'] == 0
    assert transport_shortcuts == []
    QTest.keyClick(view, Qt.Key.Key_Escape)
    assert not view._command_stack._undo_stack


def test_window_transport_shortcut_respects_active_roll_even_if_toolbar_has_focus(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    timeline = TimelineSequence('shortcut-roll', 'Shortcut roll', fps=24,
        video_tracks=(EditTrack('v', 'V1', 'video', 0, clips=(
            ClipInstance('left', 'asset', 'v', 0, 100, 0, 100),
            ClipInstance('right', 'asset', 'v', 100, 100, 100, 100),
        )),))
    panel.load_timeline(timeline)
    view = panel.get_timeline_view()
    for item in view._items_map.values():
        item._max_source_frames = 1000
    monkeypatch.setattr(panel, '_transport_context_active', lambda: True)
    view._on_roll_trim_started('left', 'right', 100)
    panel._on_l_pressed()
    assert view._roll_preview_state['delta'] == 1
    panel._on_j_pressed()
    panel._on_k_pressed()
    assert view._roll_preview_state['delta'] == 0
    assert panel._playback_controller.state.value != 'playing'
    view._cancel_current_interaction()
