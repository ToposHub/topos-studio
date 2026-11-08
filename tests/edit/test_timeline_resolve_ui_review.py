"""Real event regressions for the second Resolve timeline UI review.

These checks exercise graphics-view routing and the existing edit commands;
advanced speed options remain explicit requests without simulated edits.
"""
from __future__ import annotations

import math
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.change_clip_speed_dialog import ChangeClipSpeedDialog
from src.features.edit.ui.timeline_items import ClipItem, EditTool
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.features.edit.ui.timeline_view import TimelineView
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


def _sequence(duration: int = 100) -> TimelineSequence:
    video = ClipInstance(
        clip_id="vc", asset_id="asset", track_id="v1", timeline_start=100,
        timeline_duration=duration, source_start=50, source_duration=duration,
        linked_clip_id="ac",
    )
    audio = ClipInstance(
        clip_id="ac", asset_id="asset", track_id="a1", timeline_start=100,
        timeline_duration=duration, source_start=50, source_duration=duration,
        linked_clip_id="vc",
    )
    return TimelineSequence(
        timeline_id="ui-review", name="UI Review", fps=Rational(24, 1),
        video_tracks=(EditTrack("v1", "Picture", "video", 0, clips=(video,)),
                      EditTrack("v2", "Cutaways", "video", 1)),
        audio_tracks=(EditTrack("a1", "Dialogue", "audio", 0, clips=(audio,)),),
    )


def _project() -> EditProject:
    return EditProject("review-project", "Review", assets={
        "asset": MediaAsset(
            "asset", "/tmp/resolve-ui-review-missing.mp4", "video", "Picture",
            metadata={"fps": 24.0, "total_frames": 10000,
                      "audio_info": {"channels": 2, "channel_layout": "stereo"}},
        ),
    })


def _view(qtbot: Any) -> TimelineView:
    view = TimelineView()
    qtbot.addWidget(view)
    view.resize(1400, 650)
    view.show()
    view.set_project(_project())
    view.load_timeline(_sequence())
    view.set_zoom(3.0)
    view.set_snap_enabled(False)
    view.set_tool(EditTool.SELECT)
    for item in view._items_map.values():
        if isinstance(item, ClipItem):
            item._max_source_frames = 10000
    QTest.qWait(5)
    return view


def _body(view: TimelineView, clip_id: str = "vc") -> QPoint:
    item = view._items_map[clip_id]
    return view.mapFromScene(item.mapToScene(QPointF(item._width / 2.0, 32.0)))


def _panel(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> TimelinePanel:
    panel = TimelinePanel()
    qtbot.addWidget(panel)
    monkeypatch.setattr(panel, "_defer_background_media_work_for_zoom", lambda: None)
    panel.resize(1200, 650)
    panel.show()
    panel.set_project(_project())
    panel.load_timeline(_sequence(2400))
    QTest.qWait(5)
    return panel


def test_normal_click_selects_both_linked_members_and_borders(qtbot: Any) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    assert set(view._selection.selected_clip_ids) == {"vc", "ac"}
    assert all(view._items_map[clip_id]._is_selected for clip_id in ("vc", "ac"))
    assert view._linked_selection_enabled


def test_option_click_isolates_one_side_without_partner_selection_glow(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.KeyboardModifier.AltModifier))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, pos=_body(view))
    assert set(view._selection.selected_clip_ids) == {"vc"}
    assert view._items_map["vc"]._is_selected
    assert not view._items_map["ac"]._is_selected
    assert not view._items_map["ac"]._linked_to_selection
    assert view._linked_selection_enabled


def test_option_edge_trim_leaves_linked_partner_untouched(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _view(qtbot)
    original_audio = view.get_timeline().get_clip_by_id("ac")
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    item = view._items_map["vc"]
    start = view.mapFromScene(item.mapToScene(QPointF(item._width - 4.0, 32.0)))
    end = start - QPoint(30, 0)
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: Qt.KeyboardModifier.AltModifier))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, pos=start)
    QTest.mouseMove(view.viewport(), end, 10)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.AltModifier, pos=end)
    assert view.get_timeline().get_clip_by_id("vc").timeline_duration == 90
    assert view.get_timeline().get_clip_by_id("ac") == original_audio
    assert view._linked_selection_enabled
    assert len(view._command_stack._undo_stack) == 1


def test_position_lock_delete_has_no_exception_or_partial_ui_change(qtbot: Any) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    view.set_position_lock_enabled(True)
    original, selection = view.get_timeline(), view._selection
    messages: list[str] = []
    view.status_message_requested.connect(messages.append)
    QTest.keyClick(view, Qt.Key.Key_Delete)
    assert view.get_timeline() is original
    assert view._selection == selection
    assert not view._command_stack._undo_stack
    assert messages


def test_unset_blue_badge_matches_effective_red_target(qtbot: Any) -> None:
    view = _view(qtbot)
    header = view._track_headers["v2"]
    target = view.mapFromScene(header.mapToScene(header._get_track_badge_rect().center()))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=target)
    assert view.get_target_track_id() == "v2"
    assert view.source_patch_track_id("video") == view.resolve_source_destination_track_id("video") == "v2"
    assert header._source_patch_enabled
    assert not view._track_headers["v1"]._source_patch_enabled


@pytest.mark.parametrize("ppf", [float("inf"), float("nan"), True, -1.0])
def test_corrupt_presentation_numbers_are_safe(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, ppf: float,
) -> None:
    panel = _panel(qtbot, monkeypatch)
    before = panel.timeline_view.get_timeline()
    panel.apply_presentation_state({
        "zoom_preset": "custom", "custom_pixels_per_frame": ppf,
        "custom_scroll": "garbage", "waveforms_visible": False,
    })
    assert math.isfinite(panel.timeline_view._pixels_per_frame)
    assert not panel.timeline_view.waveforms_visible()
    assert panel.timeline_view.get_timeline() is before
    assert not panel.timeline_view._command_stack._undo_stack


@pytest.mark.parametrize("preset", ["full", "detail"])
def test_restoring_preset_keeps_its_remembered_custom_view(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, preset: str,
) -> None:
    first = _panel(qtbot, monkeypatch)
    first.timeline_view.set_zoom(2.5)
    first.timeline_view.horizontalScrollBar().setValue(350)
    custom = (first.timeline_view._pixels_per_frame, first.timeline_view.horizontalScrollBar().value())
    first._set_zoom_preset(preset)
    state = first.export_presentation_state()
    second = _panel(qtbot, monkeypatch)
    second.apply_presentation_state(state)
    QTest.qWait(5)
    QTest.mouseClick(second.btn_zoom_custom, Qt.MouseButton.LeftButton)
    QTest.qWait(5)
    assert second.timeline_view._pixels_per_frame == pytest.approx(custom[0])
    assert second.timeline_view.horizontalScrollBar().value() == custom[1]


def test_clear_discards_source_routes_and_notifies_selection_consumers(qtbot: Any) -> None:
    view = _view(qtbot)
    view._on_source_patch_requested("v2", False)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    selections: list[tuple[str, ...]] = []
    view.selection_changed.connect(lambda selection: selections.append(tuple(selection.selected_clip_ids)))
    view.clear()
    assert view.get_timeline() is None
    assert selections and selections[-1] == ()
    assert view._source_patch_routes == {}
    view.load_timeline(_sequence())
    assert not view.source_destination_stream_disabled("video")
    assert view.source_patch_track_id("video") == "v1"


def _speed_settings(**updates: Any) -> dict[str, Any]:
    settings = {
        "speed": 2.0, "reverse": True, "freeze": False, "preserve_pitch": True,
        "ripple": False, "keyframe_timing": "stretch", "duration_frames": 50,
    }
    settings.update(updates)
    return settings


def test_speed_dialog_change_uses_existing_command_for_supported_options(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    monkeypatch.setattr(ChangeClipSpeedDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ChangeClipSpeedDialog, "settings", lambda self: _speed_settings())
    view._on_change_clip_speed_dialog_requested("vc", "v1")
    clip = view.get_timeline().get_clip_by_id("vc")
    assert clip.speed.speed == 2.0 and clip.speed.reverse and clip.speed.preserve_pitch
    assert clip.timeline_duration == 50
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("vc") == original.get_clip_by_id("vc")


def test_speed_dialog_cancel_has_no_content_write(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    monkeypatch.setattr(ChangeClipSpeedDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
    view._on_change_clip_speed_dialog_requested("vc", "v1")
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack


@pytest.mark.parametrize("option", ["ripple", "freeze"])
def test_unsupported_speed_semantics_emit_request_without_faking_an_edit(
    qtbot: Any, monkeypatch: pytest.MonkeyPatch, option: str,
) -> None:
    view = _view(qtbot)
    original = view.get_timeline()
    requests: list[tuple[str, str, dict[str, Any]]] = []
    view.clip_speed_settings_requested.connect(lambda *args: requests.append(args))
    monkeypatch.setattr(ChangeClipSpeedDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ChangeClipSpeedDialog, "settings", lambda self: _speed_settings(**{option: True}))
    view._on_change_clip_speed_dialog_requested("vc", "v1")
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack
    assert len(requests) == 1
    assert requests[0][0:2] == ("vc", "v1")
    assert requests[0][2][option] is True


def test_cross_track_move_preview_uses_destination_lane_height(qtbot: Any) -> None:
    view = _view(qtbot)
    view.set_linked_selection_enabled(False)
    view._layout_state.track_heights["v2"] = 146
    view._layout_track_items()
    item = view._items_map["vc"]
    origin = _body(view)
    destination = view.mapFromScene(QPointF(item.x() + item._width / 2.0, view._layout_engine.track_top("v2") + 50))
    QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=origin)
    QTest.mouseMove(view.viewport(), destination, 10)
    assert item._drag_target_track_id == "v2"
    assert item._track_height == 146
    QTest.keyClick(view, Qt.Key.Key_Escape)
    QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=destination)
    assert view.get_timeline().get_clip_by_id("vc").track_id == "v1"
    assert view._items_map["vc"]._track_height == view._layout_engine.track_height("v1")
    assert not view._command_stack._undo_stack


def test_linked_selection_keeps_clicked_primary_and_clip_marker_scope(qtbot: Any) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    assert set(view._selection.selected_clip_ids) == {"vc", "ac"}
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view, "ac"))
    assert view._selection.primary_item.item_id == "ac"
    assert set(view._selection.selected_clip_ids) == {"vc", "ac"}
    view.set_playhead(120)
    view.add_marker_at_playhead()
    assert view.get_timeline().markers[-1].clip_id == "ac"


def test_linked_pair_inspector_uses_clicked_side(qtbot: Any) -> None:
    from types import SimpleNamespace
    from src.app.controllers.edit_controller import EditControllerMixin
    from src.features.edit.core.selection import TimelineSelection

    sequence = _sequence()
    selection = TimelineSelection().with_clip_selected("vc", "v1", "ui-review")
    selection = selection.with_clip_selected("ac", "a1", "ui-review", replace=False)
    properties: list[Any] = []
    effects: list[Any] = []
    controller = EditControllerMixin.__new__(EditControllerMixin)
    controller.edit_page = SimpleNamespace(_current_timeline=sequence, _fps=24.0)
    controller.edit_properties_panel = SimpleNamespace(set_clips=lambda clips, *_a, **_kw: properties.append(clips))
    controller.edit_effects_panel = SimpleNamespace(set_selected_clip=effects.append)
    controller._clear_subtitle_viewer_selection = lambda: None
    controller._show_clip_in_info = lambda _clip: None
    controller._on_timeline_selection_changed(selection)
    assert [clip.clip_id for clip in properties[-1]] == ["ac"]
    assert effects[-1] == "ac"


def test_marquee_expands_linked_unit_and_option_marquee_is_independent(qtbot: Any) -> None:
    view = _view(qtbot)
    item = view._items_map["vc"]
    start = view.mapFromScene(item.mapToScene(QPointF(-10, 22)))
    end = view.mapFromScene(item.mapToScene(QPointF(item._width / 2, 52)))
    for modifiers, expected in ((Qt.KeyboardModifier.NoModifier, {"vc", "ac"}),
                                (Qt.KeyboardModifier.AltModifier, {"vc"})):
        QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, modifiers, pos=start)
        QTest.mouseMove(view.viewport(), end, 10)
        QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, modifiers, pos=end)
        assert set(view._selection.selected_clip_ids) == expected
    assert not view._command_stack._undo_stack


def test_retime_close_and_bottom_percentage_hit_do_not_move_clip(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view(qtbot)
    item = view._items_map["vc"]
    original = view.get_timeline()
    view._on_retime_controls_toggled("vc", "v1", True)
    menus: list[float] = []
    monkeypatch.setattr(item, "_show_retime_segment_speed_menu", lambda _screen, x: menus.append(x))
    bottom = view.mapFromScene(item.mapToScene(QPointF(item._width / 2, item._track_height - 10)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=bottom)
    assert menus and not item._drag_pending
    close = view.mapFromScene(item.mapToScene(QPointF(8, 8)))
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=close)
    assert not item._retime_controls_visible
    assert "vc" not in view._retime_control_clip_ids
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack


def test_animated_speed_draft_waits_for_safe_backend_transaction(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    view = _view(qtbot)
    sequence = view.get_timeline()
    clip = sequence.get_clip_by_id("vc").with_keyframe_added("opacity", 101, 0.2)
    view.load_timeline(sequence.with_clip_updated(clip))
    original = view.get_timeline()
    requests: list[Any] = []
    view.clip_speed_settings_requested.connect(lambda *_args: requests.append(_args))
    monkeypatch.setattr(ChangeClipSpeedDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    monkeypatch.setattr(ChangeClipSpeedDialog, "settings", lambda self: _speed_settings())
    view._on_change_clip_speed_dialog_requested("vc", "v1")
    assert view.get_timeline() is original
    assert not view._command_stack._undo_stack
    assert len(requests) == 1


def test_retime_and_link_shortcuts_use_selection_without_extra_history(qtbot: Any) -> None:
    view = _view(qtbot)
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=_body(view))
    QTest.keyClick(view, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    assert view._items_map["vc"]._retime_controls_visible
    QTest.keyClick(view, Qt.Key.Key_R, Qt.KeyboardModifier.ControlModifier)
    assert not view._items_map["vc"]._retime_controls_visible
    assert not view._command_stack._undo_stack
    QTest.keyClick(view, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
    assert view.get_timeline().get_clip_by_id("vc").linked_clip_id is None
    assert view.get_timeline().get_clip_by_id("ac").linked_clip_id is None
    assert len(view._command_stack._undo_stack) == 1
    view.undo()
    assert view.get_timeline().get_clip_by_id("vc").linked_clip_id == "ac"


def test_linked_selection_shortcut_updates_toolbar(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    panel = _panel(qtbot, monkeypatch)
    view = panel.timeline_view
    QTest.keyClick(view, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert not view._linked_selection_enabled
    assert not panel.btn_linked_selection.isChecked()
    QTest.keyClick(view, Qt.Key.Key_L, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    assert view._linked_selection_enabled
    assert panel.btn_linked_selection.isChecked()
    assert not view._command_stack._undo_stack


def test_presentation_huge_integers_are_clamped_before_qt_conversion(qtbot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.features.edit.ui.timeline_view import MAX_ZOOM

    panel = _panel(qtbot, monkeypatch)
    panel.apply_presentation_state({"zoom_preset": "custom", "custom_pixels_per_frame": 10**1000,
                                    "custom_scroll": 10**1000})
    assert panel.timeline_view._pixels_per_frame == MAX_ZOOM
    assert panel._custom_zoom[1] <= 2**31 - 1
