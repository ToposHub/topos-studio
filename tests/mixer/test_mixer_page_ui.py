"""Real mixer widgets: dock interaction, local state and no production backend."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtTest import QTest
from PyQt6.QtCore import QPoint
from PyQt6.QtWidgets import QDockWidget, QMainWindow
import pytest

from src.features.mixer.ui.mixer_page import MixerPage
from src.features.mixer.ui.console_controls import LevelMeter, StudioFader


@pytest.fixture
def workspace(qtbot):
    host = QMainWindow()
    host.resize(1460, 980)
    host.setDockOptions(QMainWindow.DockOption.AllowNestedDocks | QMainWindow.DockOption.AllowTabbedDocks)
    page = MixerPage(host)
    docks = []
    for name, panel in (("dockAudioConsole", page.console_panel), ("dockAudioViewer", page.viewer_panel),
                         ("dockAudioTimeline", page.timeline_panel), ("dockAudioClipEditor", page.clip_editor_panel)):
        dock = QDockWidget(name, host)
        dock.setObjectName(name)
        dock.setWidget(panel)
        host.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea if len(docks) < 2 else Qt.DockWidgetArea.BottomDockWidgetArea, dock)
        docks.append(dock)
    host.splitDockWidget(docks[0], docks[1], Qt.Orientation.Horizontal)
    docks[-1].hide()
    qtbot.addWidget(host)
    host.show()
    page.on_enter()
    yield page, host, docks
    page.dispose_ui()
    host.close()


def test_console_fader_flag_and_undo_keep_shared_local_state(workspace):
    page, _host, _docks = workspace
    strip = page.console_panel.strips["dx"]
    strip.fader.setDb(-7.3)
    assert page.session.track_by_id("dx").volume_db == -7.3
    strip.flag_buttons["muted"].click()
    assert page.session.track_by_id("dx").muted
    page.session.undo_stack.undo()
    assert not page.session.track_by_id("dx").muted
    assert not strip.flag_buttons["muted"].isChecked()
    page.session.undo_stack.undo()
    assert page.session.track_by_id("dx").volume_db == 0
    assert strip.fader.db == 0


def test_original_console_meter_and_fader_supply_local_controls(workspace):
    page, _host, _docks = workspace
    strip = page.console_panel.strips["dx"]
    assert type(strip.meter) is LevelMeter
    assert strip.meter.stereo and strip.meter.show_gr
    assert strip.meter.level_l == strip.meter.level_r == -60.0
    assert isinstance(strip.fader, StudioFader)
    start = QPoint(strip.fader.width() // 2, strip.fader.height() - 25)
    end = QPoint(start.x(), start.y() - 25)
    QTest.mousePress(strip.fader, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(strip.fader, end)
    QTest.mouseRelease(strip.fader, Qt.MouseButton.LeftButton, pos=end)
    assert page.session.undo_stack.count() == 1
    assert page.session.track_by_id("dx").volume_db != 0.0
    page.session.undo_stack.undo()
    assert strip.fader.db == 0.0


def test_bus_rename_updates_output_and_send_in_one_undo(workspace):
    page, _host, _docks = workspace
    session = page.session
    session.set_track_value("fx", "send_target", "DX Bus")
    session.set_track_value("dx_bus", "name", "Dialogue Bus")
    assert session.track_by_id("dx").output == "Dialogue Bus"
    assert session.track_by_id("fx").send_target == "Dialogue Bus"
    assert page.console_panel.strips["dx"].routing.currentText() == "Dialogue Bus"
    session.undo_stack.undo()
    assert session.track_by_id("dx").output == "DX Bus"


def test_editor_opens_selected_clip_floats_and_tabifies(workspace):
    page, host, docks = workspace
    page.timeline_panel.clipEditorRequested.emit("clip_FX")
    assert docks[-1].isFloating()
    assert page._current_editor_clip == "clip_FX"
    assert page._clip_panels["clip_FX"]._clip_name == "地下城堡音效.mp3"
    assert page._clip_panels["clip_FX"]._duration == pytest.approx(86.913)
    page.merge_editor_button.click()
    assert not docks[-1].isFloating()
    assert docks[-1] in host.tabifiedDockWidgets(docks[2])
    page.close_clip_editor()
    page.open_clip_editor("clip_FX")
    assert not docks[-1].isFloating()
    assert docks[-1] in host.tabifiedDockWidgets(docks[2])
    page.float_editor_button.click()
    assert docks[-1].isFloating()
    original = page._clip_panels["clip_FX"]
    page.close_clip_editor()
    page.open_clip_editor("clip_FX")
    assert page._clip_panels["clip_FX"] is original
    assert all(not timer.isActive() for timer in original.findChildren(QTimer))


def test_effect_browser_insert_and_parameter_edit_survive_other_commands(workspace):
    page, _host, _docks = workspace
    page.insert_effect("eq")
    effect = page.session.track_by_id("dx").effects[0]
    window = page.effects_manager.window_for(effect)
    page.session.set_track_value("dx", "volume_db", -3.0)
    state = window.panel.get_state()
    state["master_gain"] = -9.0
    window.panel.set_state(state)
    window.panel.stateChanged.emit(state)
    assert page.session.track_by_id("dx").effects[0].parameters["master_gain"] == -9
    page.session.undo_stack.undo()
    assert window.panel.get_state()["master_gain"] != -9


def test_clip_effects_can_bypass_reorder_and_remove(workspace):
    page, _host, _docks = workspace
    page.browser_owner.setCurrentIndex(1)
    page.insert_effect("eq")
    page.insert_effect("dynamics")
    page._move_clip_effect("clip_DX", 1, -1)
    clip = page.session.clip_by_id("clip_DX")
    assert [effect.effect_id for effect in clip.effects] == ["dynamics", "eq"]
    page._toggle_clip_effect(clip.effects[0])
    clip = page.session.clip_by_id("clip_DX")
    assert not clip.effects[0].enabled
    page._remove_clip_effect(clip.effects[0])
    assert [effect.effect_id for effect in page.session.clip_by_id("clip_DX").effects] == ["eq"]
    page.session.undo_stack.undo()
    assert not page.session.clip_by_id("clip_DX").effects[0].enabled


def test_page_leave_stops_silent_transport_and_hides_parameter_windows(workspace):
    page, _host, _docks = workspace
    page.insert_effect("eq")
    effect = page.session.track_by_id("dx").effects[0]
    window = page.effects_manager.window_for(effect)
    page.viewer_panel.play.click()
    assert page.viewer_panel.timer.isActive()
    page.on_leave()
    assert not page.viewer_panel.timer.isActive()
    assert not page.session.playing
    assert not window.isVisible()
    assert not page.browser_window.isVisible()
    page.on_enter()
    assert not page.viewer_panel.timer.isActive()


def test_closing_floating_viewer_stops_timer(workspace):
    page, _host, docks = workspace
    docks[1].setFloating(True)
    page.viewer_panel.play.click()
    assert page.viewer_panel.timer.isActive()
    docks[1].close()
    assert not page.viewer_panel.timer.isActive()


def test_frame_rate_change_preserves_clips_and_source_mapping(workspace):
    page, _host, _docks = workspace
    session = page.session
    original = [(c.start, c.duration, c.source_duration) for c in session.clips]
    session.set_rate(0)
    page.viewer_panel.step(1)
    assert session.playhead == pytest.approx(1001 / 24000)
    assert [(c.start, c.duration, c.source_duration) for c in session.clips] == original
    clip = session.clip_by_id("clip_DX")
    source_duration = clip.source_duration
    session.split_clip(clip.id, 4)
    assert session.clip_by_id(clip.id).source_duration == source_duration


def test_import_is_explicit_pending_request_and_never_fakes_audio(workspace):
    page, _host, _docks = workspace
    before = page.session.snapshot()
    page.session.import_media_paths(["missing.wav"])
    assert page.session.snapshot() == before
    assert "等待后端" in page.viewer_panel.status.text()
