"""Behavioral coverage for the compact Resolve-style timeline instruments."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from PyQt6.QtCore import QPoint, QTimer, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QStyle, QStyleOptionMenuItem, QStyleOptionSpinBox, QToolButton

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
    assert tuple(panel._advanced_tool_actions) == (
        EditTool.RIPPLE, EditTool.ROLL, EditTool.SLIP, EditTool.SLIDE, EditTool.RATE_STRETCH,
    )
    menu = panel.btn_advanced_tools.menu()
    menu.popup(panel.btn_advanced_tools.mapToGlobal(QPoint(0, panel.btn_advanced_tools.height())))
    qapp.processEvents()
    menu.setActiveAction(panel._advanced_tool_actions[EditTool.SLIP])
    QTest.keyClick(menu, Qt.Key.Key_Return)
    assert not menu.isVisible()
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
    assert panel.btn_zoom_custom.isHidden()
    panel._sync_overflow_menu()
    panel._overflow_button_actions[panel.btn_zoom_custom].trigger()
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


def test_clip_height_follows_zoom_and_preserves_mixer_audio_scope(panel: TimelinePanel) -> None:
    panel.resize(1440, 460)
    assert panel._tools_bar.indexOf(panel.zoom_slider) < panel._tools_bar.indexOf(panel._track_height_control)
    assert panel._track_height_control.isVisible()
    view = panel.timeline_view
    sequence = view.get_timeline()
    assert view.track_height_ratio() == (4, 3)
    assert view._layout_engine.track_height("v1") == 82
    assert view._layout_engine.track_height("a1") == 62
    panel._track_height_slider.setValue(125)
    assert view.layout_state()["track_heights"] == {"v1": 125, "a1": 94}
    panel._track_height_slider.setValue(panel._track_height_slider.value() + 8)
    assert view.layout_state()["track_heights"] == {"v1": 133, "a1": 100}
    panel._track_height_slider.setValue(panel._track_height_slider.value() - 8)
    assert view.layout_state()["track_heights"] == {"v1": 125, "a1": 94}
    assert view.get_timeline() is sequence
    assert not view._command_stack.can_undo()

    panel.set_display_mode("mixer")
    assert panel._track_height_slider.value() == 94
    panel._track_height_slider.setValue(58)
    assert view.layout_state()["track_heights"] == {"v1": 125, "a1": 58}
    panel._track_height_slider.setValue(panel._track_height_slider.value() - 8)
    assert view.layout_state()["track_heights"] == {"v1": 125, "a1": 50}
    panel._track_height_slider.setValue(panel._track_height_slider.value() + 8)
    assert view.layout_state()["track_heights"] == {"v1": 125, "a1": 58}
    panel.set_display_mode("edit")
    assert panel._track_height_slider.value() == 125
    view.set_layout_state({"track_heights": {"v1": 96, "a1": 51}})
    assert panel._track_height_slider.value() == 96


def test_track_height_ratio_controls_support_inverse_and_extreme_values(panel: TimelinePanel, qapp: Any) -> None:
    view = panel.timeline_view
    menu = panel.btn_view_options.menu()
    menu.popup(panel.btn_view_options.mapToGlobal(QPoint(0, panel.btn_view_options.height())))
    qapp.processEvents()
    video, audio = panel._track_height_ratio_video, panel._track_height_ratio_audio
    assert video.isVisible() and audio.isVisible()
    assert (video.value(), audio.value()) == (4, 3)
    assert (video.minimum(), video.maximum()) == (audio.minimum(), audio.maximum()) == (1, 4)
    video.setValue(1)
    spin_style = QStyleOptionSpinBox()
    audio.initStyleOption(spin_style)
    up = audio.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox, spin_style, QStyle.SubControl.SC_SpinBoxUp, audio,
    )
    down = audio.style().subControlRect(
        QStyle.ComplexControl.CC_SpinBox, spin_style, QStyle.SubControl.SC_SpinBoxDown, audio,
    )
    assert not up.isEmpty() and not down.isEmpty() and not up.intersects(down)
    QTest.mouseClick(audio, Qt.MouseButton.LeftButton, pos=down.center())
    assert view.track_height_ratio() == (1, 2)
    QTest.mouseClick(audio, Qt.MouseButton.LeftButton, pos=up.center())
    assert view.track_height_ratio() == (1, 3)
    QTest.mouseClick(audio, Qt.MouseButton.LeftButton, pos=up.center())
    assert view.track_height_ratio() == (1, 4)
    assert view.layout_state()["track_heights"] == {"v1": 60, "a1": 240}
    assert panel._track_height_sliders["video"].value() == 60
    assert panel._track_height_sliders["audio"].value() == 240
    assert (panel._track_height_slider.minimum(), panel._track_height_slider.maximum()) == (36, 60)
    panel._track_height_slider.setValue(48)
    assert view.layout_state()["track_heights"] == {"v1": 48, "a1": 192}
    view.set_audio_only(True)
    assert view._layout_engine.track_height("a1") == 384
    view.set_audio_only(False)
    assert view._layout_engine.track_height("a1") == 192

    video.setValue(4)
    audio.setValue(1)
    assert view.track_height_ratio() == (4, 1)
    assert (panel._track_height_slider.minimum(), panel._track_height_slider.maximum()) == (112, 160)
    panel._track_height_slider.setValue(128)
    assert view.layout_state()["track_heights"] == {"v1": 128, "a1": 32}
    assert not view._command_stack.can_undo()
    menu.close()


def test_mixer_doubles_edit_audio_height_and_restores_edit_layout(panel: TimelinePanel) -> None:
    view = panel.timeline_view
    default = view.default_track_height("audio")
    saved = view.layout_state()["track_heights"]

    panel.set_display_mode("mixer")
    view.set_audio_only(True)
    assert view._track_headers["a1"].track_height == default * 2
    assert panel._track_height_slider.value() == default * 2
    assert view.layout_state()["track_heights"] == saved

    view.set_audio_only(False)
    panel.set_display_mode("edit")
    view.set_track_height_for_type("audio", 80)
    panel.set_display_mode("mixer")
    view.set_audio_only(True)
    assert view._layout_engine.track_height("a1") == 160
    panel._track_height_slider.setValue(180)
    assert view._track_headers["a1"].track_height == 180
    view.set_track_height_preset("default")
    assert view._track_headers["a1"].track_height == 160
    view._on_track_height_changed("a1", 135)
    assert panel._track_height_slider.value() == 135
    assert view.layout_state()["track_heights"]["a1"] == 80

    view.set_audio_only(False)
    panel.set_display_mode("edit")
    assert view._track_headers["a1"].track_height == 80
    assert not view._command_stack.can_undo()

@pytest.mark.parametrize("mode", ["edit", "mixer"])
def test_toolbar_groups_tools_and_compact_zoom_controls(
    panel: TimelinePanel, qapp: Any, mode: str,
) -> None:
    from src.features.mixer.ui.output_meter import MixerOutputMeter

    panel.install_mixer_toolbar(MixerOutputMeter())
    panel.set_display_mode(mode)
    width = 1440
    panel.resize(width, 460)
    qapp.processEvents()
    horizontal = (panel._zoom_width_icon, panel.zoom_slider)
    vertical = (panel._track_height_icon, panel._track_height_slider)
    assert all(widget.isVisible() for widget in (*horizontal, *vertical))
    assert not panel._zoom_width_icon.pixmap().isNull()
    assert not panel._track_height_icon.pixmap().isNull()
    assert panel.zoom_slider.width() == 92
    assert panel._track_height_slider.width() == round(panel.zoom_slider.width() * 2 / 3)
    if mode == "edit":
        ordered = (panel.btn_insert, panel.btn_overwrite, panel.btn_replace, panel.btn_select)
        assert all(left.geometry().right() < right.geometry().left() for left, right in zip(ordered, ordered[1:]))
        assert panel.btn_select.geometry().left() - panel.btn_replace.geometry().right() - 1 >= 8
    meter = panel._audio_level_meter if mode == "edit" else panel._mixer_output_meter
    slider_right = panel._monitor_gain_control.x() + panel._monitor_gain_slider.geometry().right()
    assert meter.geometry().left() - slider_right - 1 >= 12
    if mode == "edit":
        recording = panel.btn_recording_mode
        assert recording.isVisible()
        assert slider_right < recording.geometry().left()
        assert recording.geometry().right() < meter.geometry().left()
    zoom_start = panel.btn_zoom_fit if mode == "edit" else panel._zoom_width_icon
    assert zoom_start.geometry().left() - panel.btn_marker.geometry().right() >= 32
    for row in (horizontal, vertical):
        assert all(left.geometry().right() < right.geometry().left() for left, right in zip(row, row[1:]))
    visible = sorted(
        (widget for widget in panel._timeline_toolbar_widgets if widget.isVisible()),
        key=panel._tools_bar.indexOf,
    )
    assert all(left.geometry().right() < right.geometry().left() for left, right in zip(visible, visible[1:]))
    assert visible[-1] is panel.btn_overflow
    assert width - panel.btn_overflow.geometry().right() <= 10

    # Leave space for the horizontal slider while the vertical control is hidden.
    if mode == "edit":
        narrow_width = 700
    else:
        preceding = (panel.btn_blade, panel.btn_snap, panel.btn_audio_automation,
                     panel.btn_audio_view, panel.btn_trim, panel.btn_marker)
        narrow_width = panel._mixer_toolbar_minimum_width + sum(
            widget.minimumWidth() + panel._tools_bar.spacing() for widget in preceding
        )
    panel.resize(narrow_width, 460)
    qapp.processEvents()
    assert panel._track_height_control.isHidden()
    assert all(not widget.isVisible() for widget in vertical)
    assert all(widget.isVisible() for widget in horizontal)
    remaining = [widget for widget in visible if widget.isVisible()]
    assert all(left.geometry().right() < right.geometry().left() for left, right in zip(remaining, remaining[1:]))
    assert remaining[-1].geometry().right() < panel.width()

    panel.resize(width, 460)
    qapp.processEvents()
    assert all(widget.isVisible() for widget in vertical)


def test_toolbar_monitor_volume_follows_local_control_room_state(panel: TimelinePanel, qtbot: Any) -> None:
    from src.features.edit.ui.edit_page import EditPage
    from src.features.mixer.ui.control_room import ControlRoomDialog

    page = EditPage()
    qtbot.addWidget(page)
    page.set_timeline_panel(panel)
    assert page._audio_manager is None
    assert panel._tools_bar.indexOf(panel._monitor_gain_control) < panel._tools_bar.indexOf(panel._audio_level_meter)
    speaker = panel._monitor_gain_icon
    assert not speaker.icon().isNull()
    slider = panel._monitor_gain_slider
    assert "0.0 dB" in slider.toolTip()
    assert panel._monitor_gain_control.layout().count() == 2
    qtbot.mouseClick(speaker, Qt.MouseButton.LeftButton)
    slider.setValue(-123)
    manager = page._audio_manager
    assert manager.control_room_state.muted
    assert manager.control_room_state.gain_db == -12.3
    assert speaker.isChecked()
    assert manager._output is None and manager._playback_thread is None
    manager.audio_level_changed.emit([-13.0, -17.0])
    assert panel._audio_level_meter.peak_text == "-13.0"

    dialog = ControlRoomDialog()
    qtbot.addWidget(dialog)
    dialog.set_session(None, manager.control_room_state, manager.control_room_patch, {})
    manager.monitor_gain_changed.connect(dialog.set_monitor_gain_db)
    manager.monitor_muted_changed.connect(dialog.set_monitor_muted)
    dialog.state_requested.connect(lambda state: manager.set_control_room(state, manager.control_room_patch))
    dialog.gain.setValue(-22.7)
    assert slider.value() == -227
    slider.setValue(-310)
    assert dialog.gain.value() == -31.0
    assert dialog.mute.isChecked()
    dialog.mute.setChecked(False)
    assert not speaker.isChecked()
    assert "−31.0 dB" in slider.toolTip()
    qtbot.mouseClick(speaker, Qt.MouseButton.LeftButton)
    assert dialog.mute.isChecked()
    qtbot.mouseClick(speaker, Qt.MouseButton.LeftButton)
    assert not dialog.mute.isChecked()
    assert manager.control_room_state.gain_db == -31.0


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
    visible = [widget for widget in panel._timeline_toolbar_widgets if not widget.isHidden()]
    assert all(left.geometry().right() < right.geometry().left() for left, right in zip(visible, visible[1:]))
    assert visible[-1].geometry().right() < panel.width()
    meter = panel._audio_level_meter
    assert meter is not None
    assert not meter.isHidden()
    recording = panel.btn_recording_mode
    assert not recording.isHidden()
    assert panel._monitor_gain_control.geometry().right() < recording.geometry().left()
    assert recording.geometry().right() < meter.geometry().left()
    assert meter.width() >= 56
    panel.update_audio_levels([-12.0, -6.0])
    assert meter.levels_db == (-12.0, -6.0)
    assert meter.peak_text == "-6.0"
    assert meter.height() == 24
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
    panel.recording_mode_requested.connect(lambda active: requests.append(("recording_mode", active)))
    panel.flag_requested.connect(lambda color: requests.append(("flag", color)))
    panel.set_source_edit_buttons_enabled(True)
    panel.btn_replace.click()
    panel.btn_keyframe_tray.click()
    panel.btn_recording_mode.click()
    panel.set_recording_mode_checked(False)
    panel._flag_actions["#E06B66"].trigger()
    panel.btn_flag.click()
    assert requests == [("replace", None), ("keyframes", True), ("recording_mode", True),
                        ("flag", "#E06B66"), ("flag", "#E06B66")]
    assert not panel.btn_recording_mode.isChecked()
    assert panel.findChild(QToolButton, "timelineVoiceover") is None


def test_flag_popup_keeps_clear_first_and_colors_clickable(panel: TimelinePanel, qapp: Any) -> None:
    requested: list[str] = []
    panel.flag_requested.connect(requested.append)
    menu = panel.btn_flag.menu()
    clear, divider, *colors = menu.actions()
    assert divider.isSeparator()
    assert colors == list(panel._flag_actions.values())

    for action in (panel._flag_actions["#E06B66"], clear):
        menu.popup(panel.btn_flag.mapToGlobal(QPoint(0, panel.btn_flag.height())))
        qapp.processEvents()
        QTest.mouseClick(menu, Qt.MouseButton.LeftButton, pos=menu.actionGeometry(action).center())
        assert not menu.isVisible()
    assert requested == ["#E06B66", ""]


def test_toolbar_popup_icons_survive_platform_menu_icon_default(panel: TimelinePanel, qapp: Any) -> None:
    attribute = Qt.ApplicationAttribute.AA_DontShowIconsInMenus
    previous = qapp.testAttribute(attribute)
    qapp.setAttribute(attribute, True)
    try:
        for button in (panel.btn_flag, panel.btn_marker, panel.btn_advanced_tools, panel.btn_overflow):
            menu = button.menu()
            actions = [action for action in menu.actions() if not action.icon().isNull()]
            assert actions
            for action in actions:
                option = QStyleOptionMenuItem()
                menu.initStyleOption(option, action)
                assert not option.icon.isNull(), action.text()
    finally:
        qapp.setAttribute(attribute, previous)


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


def test_waveform_submenu_switches_style_with_mouse_and_keyboard(panel: TimelinePanel, qapp: Any) -> None:
    host = panel.btn_view_options
    menu = host.menu()
    view = panel.timeline_view
    bottom = panel._waveform_style_actions["bottom_fill"]
    centered = panel._waveform_style_actions["centered"]
    submenu = next(action.menu() for action in menu.actions()
                   if action.menu() is not None and bottom in action.menu().actions())
    origin = host.mapToGlobal(QPoint(0, host.height()))
    menu.popup(origin)
    qapp.processEvents()
    assert bottom.isChecked() and not centered.isChecked()
    assert bottom.icon().isNull() and centered.icon().isNull()
    menu.setActiveAction(submenu.menuAction())
    QTest.keyClick(menu, Qt.Key.Key_Right)
    assert submenu.isVisible()
    QTest.mouseClick(submenu, Qt.MouseButton.LeftButton, pos=submenu.actionGeometry(centered).center())
    assert view.waveform_style() == "centered"
    assert view._items_map["clip"]._waveform_style == "centered"
    view.set_thumbnail_display_mode("none")
    view.set_waveforms_visible(False)
    view._create_alt_copy_ghosts(SimpleNamespace(original_positions={"clip": 0}))
    try:
        ghost = view._alt_copy_ghosts["clip"]
        assert ghost._waveform_style == "centered"
        assert ghost._thumbnail_display_mode == "none"
        assert ghost._waveforms_visible is False
    finally:
        view._clear_alt_copy_ghosts()

    menu.popup(origin)
    qapp.processEvents()
    assert centered.isChecked() and not bottom.isChecked()
    menu.setActiveAction(submenu.menuAction())
    QTest.keyClick(menu, Qt.Key.Key_Right)
    submenu.setActiveAction(bottom)
    QTest.keyClick(submenu, Qt.Key.Key_Return)
    assert view.waveform_style() == "bottom_fill"
    menu.close()
    assert not view._command_stack.can_undo()


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


@pytest.mark.parametrize("name", ["btn_marker", "btn_flag"])
def test_small_split_arrow_opens_menu_without_running_primary_action(
    panel: TimelinePanel, qapp: Any, name: str,
) -> None:
    from PyQt6.QtCore import QPoint

    button = getattr(panel, name)
    menu = button.menu()
    primary_clicks: list[bool] = []
    opened: list[bool] = []
    button.clicked.connect(primary_clicks.append)

    def inspect_and_close() -> None:
        opened.append(menu.isVisible())
        menu.close()

    QTimer.singleShot(0, inspect_and_close)
    QTest.mouseClick(button, Qt.MouseButton.LeftButton,
                     pos=QPoint(button.width() - 6, button.height() // 2))
    qapp.processEvents()
    assert opened == [True]
    assert primary_clicks == []


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
    assert panel.zoom_slider.isHidden() == (deliver_width < 560)
    assert panel.btn_replace.isHidden() == (deliver_width < 900)
    assert all(button.isHidden() for button in panel._secondary_tool_buttons)
    meter = panel._audio_level_meter
    assert meter is not None
    assert not meter.isHidden()
    assert meter.width() == (124 if deliver_width >= 1200 else 68)
    assert all(widget.geometry().right() < deliver_width
               for widget in panel._timeline_toolbar_widgets if not widget.isHidden())
