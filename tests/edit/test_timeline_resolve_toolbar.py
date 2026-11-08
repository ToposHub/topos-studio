"""Behavioral coverage for the compact Resolve-style timeline instruments."""

from __future__ import annotations

from typing import Any

import pytest
from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtTest import QTest

from src.features.edit.core.timeline_v2 import ClipInstance, EditTrack, TimelineSequence
from src.features.edit.ui.timeline_items import EditTool
from src.features.edit.ui.timeline_panel import TimelinePanel
from src.shared.media.metadata import Rational

pytestmark = pytest.mark.edit_ui


@pytest.fixture
def panel(qapp: Any, monkeypatch: pytest.MonkeyPatch) -> TimelinePanel:
    widget = TimelinePanel()
    monkeypatch.setattr(widget, "_defer_background_media_work_for_zoom", lambda: None)
    widget.resize(1200, 460)
    widget.show()
    qapp.processEvents()
    clip = ClipInstance("clip", "asset", "v1", 0, 2400, 0, 2400)
    widget.load_timeline(TimelineSequence(
        timeline_id="toolbar", name="Toolbar", fps=Rational(24, 1),
        width=1920, height=1080,
        video_tracks=(EditTrack("v1", "Picture", "video", 0, clips=(clip,)),),
        audio_tracks=(EditTrack("a1", "Sound", "audio", 0),),
    ))
    qapp.processEvents()
    yield widget
    widget._zoom_apply_timer.stop()
    widget._jkl_timer.stop()
    widget.close()
    widget.deleteLater()


def test_secondary_tools_remain_accessible_in_menu_after_resize(panel: TimelinePanel, qapp: Any) -> None:
    for width in (1000, 400, 1200):
        panel.resize(width, 460)
        qapp.processEvents()
        assert all(button.isHidden() for button in panel._secondary_tool_buttons)
    panel._advanced_tool_actions[EditTool.SLIP].trigger()
    assert panel.timeline_view._active_tool == EditTool.SLIP
    assert panel.btn_slip.isChecked()
    assert panel.btn_advanced_tools.isChecked()
    panel._set_tool(EditTool.SELECT)
    assert not panel.btn_advanced_tools.isChecked()


def test_toolbar_never_takes_keyboard_focus(panel: TimelinePanel) -> None:
    for widget in panel._timeline_toolbar_widgets:
        assert widget.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_full_detail_custom_restore_manual_view(panel: TimelinePanel, qapp: Any) -> None:
    view = panel.timeline_view
    view.set_zoom(2.0)
    view.horizontalScrollBar().setValue(350)
    custom = (view._pixels_per_frame, view.horizontalScrollBar().value())
    panel.btn_zoom_fit.click()
    assert panel._zoom_preset == "full"
    assert panel.btn_zoom_fit.isChecked()
    assert view.horizontalScrollBar().value() == view.horizontalScrollBar().minimum()
    panel.btn_zoom_detail.click()
    assert panel._zoom_preset == "detail"
    assert panel.btn_zoom_detail.isChecked()
    panel.btn_zoom_custom.click()
    qapp.processEvents()
    assert view._pixels_per_frame == pytest.approx(custom[0])
    assert view.horizontalScrollBar().value() == custom[1]
    assert panel.btn_zoom_custom.isChecked()


def test_manual_zoom_leaves_preset_and_becomes_new_custom(panel: TimelinePanel) -> None:
    panel.btn_zoom_fit.click()
    panel.timeline_view.set_zoom(3.0)
    assert panel._zoom_preset == "custom"
    assert panel.btn_zoom_custom.isChecked()
    assert not panel.btn_zoom_fit.isChecked()
    panel.btn_zoom_detail.click()
    panel.btn_zoom_custom.click()
    assert panel.timeline_view._pixels_per_frame == pytest.approx(3.0)


def test_position_lock_button_tracks_view_state(panel: TimelinePanel) -> None:
    panel.btn_position_lock.click()
    assert panel.timeline_view.is_position_lock_enabled()
    panel.timeline_view.set_position_lock_enabled(False)
    assert not panel.btn_position_lock.isChecked()


def test_view_options_change_display_and_height(panel: TimelinePanel) -> None:
    view = panel.timeline_view
    panel._thumbnail_actions["none"].trigger()
    assert view.thumbnail_display_mode() == "none"
    panel._sync_view_options()
    assert panel._thumbnail_actions["none"].isChecked()
    panel._waveforms_action.trigger()
    assert not view.waveforms_visible()
    panel._track_height_sliders["video"].setValue(110)
    assert view.layout_state()["track_heights"]["v1"] == 110
    # Restore explicit mode so a subsequent view is not affected by legacy
    # class-level thumbnail settings while that old API is being migrated.
    view.set_thumbnail_display_mode("auto")


def test_overflow_uses_original_enabled_state_and_signal(panel: TimelinePanel) -> None:
    inserted: list[bool] = []
    panel.source_insert_requested.connect(lambda: inserted.append(True))
    panel._sync_overflow_menu()
    action = panel._overflow_button_actions[panel.btn_insert]
    assert not action.isEnabled()
    panel.set_source_edit_buttons_enabled(True)
    panel._sync_overflow_menu()
    action.trigger()
    assert inserted == [True]


def test_meter_survives_compact_layout_and_deliver_round_trip(panel: TimelinePanel, qapp: Any) -> None:
    panel.resize(400, 460)
    qapp.processEvents()
    assert panel.width() == 400
    assert all(widget.geometry().right() < panel.width()
               for widget in panel._timeline_toolbar_widgets if not widget.isHidden())
    meter = panel._audio_level_meter
    assert meter is not None
    assert not meter.isHidden()
    assert meter.width() >= 56
    panel.update_audio_levels([-12.0, -6.0])
    assert meter._value_decibels == [-12.0, -6.0]
    assert not panel.btn_overflow.isHidden()
    panel.set_display_mode("deliver")
    assert panel.btn_view_options.isHidden()
    assert panel.btn_position_lock.isHidden()
    assert not panel.btn_zoom_fit.isHidden()
    panel.set_display_mode("edit")
    assert not meter.isHidden()
    assert all(button.isHidden() for button in panel._secondary_tool_buttons)


def test_annotation_and_source_requests_are_explicit(panel: TimelinePanel) -> None:
    requests: list[tuple[str, Any]] = []
    panel.source_replace_requested.connect(lambda: requests.append(("replace", None)))
    panel.keyframe_tray_requested.connect(lambda visible: requests.append(("keyframes", visible)))
    panel.voiceover_requested.connect(lambda: requests.append(("voiceover", None)))
    panel.flag_requested.connect(lambda color: requests.append(("flag", color)))
    panel.set_source_edit_buttons_enabled(True)
    panel.btn_replace.click()
    panel.btn_keyframe_tray.click()
    panel.btn_voiceover.click()
    panel._flag_actions["#E06B66"].trigger()
    panel.btn_flag.click()
    assert requests == [("replace", None), ("keyframes", True), ("voiceover", None),
                        ("flag", "#E06B66"), ("flag", "#E06B66")]


def test_marker_primary_action_adds_marker_instead_of_opening_list(panel: TimelinePanel) -> None:
    panel.btn_marker.click()
    timeline = panel.timeline_view.get_timeline()
    assert timeline is not None
    assert len(timeline.markers) == 1
    assert timeline.markers[0].frame == panel.timeline_view.get_playhead_frame()
    assert getattr(panel, "_marker_list_dock", None) is None


def test_source_patch_request_is_forwarded(panel: TimelinePanel) -> None:
    requests: list[tuple[str, str, bool]] = []
    panel.source_patch_changed.connect(lambda kind, track, enabled: requests.append((kind, track, enabled)))
    panel.timeline_view.source_patch_changed.emit("video", "v1", True)
    assert requests == [("video", "v1", True)]


@pytest.mark.parametrize("popup", ["view_options", "compact_overflow"])
def test_mouse_press_opens_and_closes_toolbar_popup(
    panel: TimelinePanel, qapp: Any, popup: str,
) -> None:
    """Exercise native button press dispatch, including InstantPopup's event loop."""
    if popup == "compact_overflow":
        panel.resize(400, 460)
        qapp.processEvents()
        button = panel.btn_overflow
    else:
        button = panel.btn_view_options
    menu = button.menu()
    assert menu is not None
    observed: list[tuple[bool, bool]] = []

    def inspect_popup_and_close() -> None:
        try:
            contents_visible = (
                all(slider.isVisible() for slider in panel._track_height_sliders.values())
                if popup == "view_options"
                else panel._overflow_button_actions[panel.btn_replace].isVisible()
            )
            observed.append((menu.isVisible(), contents_visible))
        finally:
            menu.close()

    # QToolButton's InstantPopup opens a nested event loop during mousePress;
    # close from that loop so mouseRelease and pytest can complete normally.
    QTimer.singleShot(0, inspect_popup_and_close)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton, pos=button.rect().center())
    qapp.processEvents()

    assert observed == [(True, True)]
    assert not menu.isVisible()


@pytest.mark.parametrize("edit_width,deliver_width", [(1200, 400), (400, 1200)])
def test_edit_restores_responsive_layout_after_deliver_resize(
    panel: TimelinePanel, qapp: Any, edit_width: int, deliver_width: int,
) -> None:
    panel.resize(edit_width, 460)
    qapp.processEvents()
    panel.set_display_mode("deliver")
    panel.resize(deliver_width, 460)
    qapp.processEvents()
    panel.set_display_mode("edit")
    qapp.processEvents()

    assert panel.width() == deliver_width
    assert panel.zoom_slider.isHidden() == (deliver_width < 750)
    assert panel.btn_replace.isHidden() == (deliver_width < 900)
    assert all(button.isHidden() for button in panel._secondary_tool_buttons)
    meter = panel._audio_level_meter
    assert meter is not None
    assert not meter.isHidden()
    assert meter.width() == (140 if deliver_width >= 1200 else 56)
    assert all(widget.geometry().right() < deliver_width
               for widget in panel._timeline_toolbar_widgets if not widget.isHidden())
