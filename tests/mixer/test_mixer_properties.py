"""Mixer inspector, Clip Edit and the library share clip commands and windows."""
from __future__ import annotations

import wave
from dataclasses import replace

import pytest
from PyQt6.QtCore import QMimeData, QPointF, Qt
from PyQt6.QtGui import QDropEvent

from src.features.edit.core.project import EditProject, MediaAsset
from src.features.edit.core.selection import TimelineSelection
from src.features.edit.core.timeline_v2 import TimelineSequence
from tests.mixer.test_project_workspace import ProjectHost, project_host as project_host


@pytest.fixture
def inspector_host(project_host, tmp_path, monkeypatch):
    host = project_host
    path = tmp_path / "dialogue.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x00\x00" * 48000 * 5)
    asset = MediaAsset(asset_id="audio", path=str(path), media_type="audio",
                       display_name="Dialogue", metadata={"duration_seconds": 5.0})
    host._current_edit_project = EditProject("properties", "Properties", assets={"audio": asset})
    host.timeline_preview_renderer = None
    monkeypatch.setattr(host, "_request_program_viewer_update", lambda: None)
    monkeypatch.setattr(host, "_invalidate_clip_render_cache", lambda _clip_id: None)
    props = host.edit_properties_panel
    props.effect_enabled_changed.connect(host._on_edit_effect_enabled_changed)
    props.effect_remove_requested.connect(host._on_edit_effect_remove_requested)
    props.effect_reorder_requested.connect(host._on_edit_effect_reorder_requested)
    view = host.timeline_panel.timeline_view
    view.selection_changed.connect(host._on_timeline_selection_changed)
    view.effect_added.connect(host._on_edit_effect_added)
    host.show_mixer()
    host.dock_audio_properties.show()
    clip = view.get_timeline().audio_tracks[0].clips[0]
    view.set_selection(TimelineSelection().with_clip_selected(
        clip.clip_id, clip.track_id, view.get_timeline().timeline_id))
    yield host
    manager = getattr(host, "_clip_audio_fx_dialog_mgr", None)
    if manager is not None:
        manager.close_all()
    host.dock_audio_clip_editor.widget().reset()


def _effects(host: ProjectHost) -> list:
    return host.edit_properties_panel._effect_sections_in_order()


def _clip(host: ProjectHost):
    return host.timeline_panel.timeline_view.get_timeline().audio_tracks[0].clips[0]


def test_shared_inspector_returns_to_edit_without_reconnecting(inspector_host):
    host = inspector_host
    props = host.edit_properties_panel
    identity = id(props)
    clip_id = _clip(host).clip_id
    assert props.parentWidget() is host.dock_audio_properties.widget()
    assert f"clip:{clip_id}:audio_clip" in props._sections
    assert f"clip:{clip_id}:transform" not in props._sections
    host._restore_edit_panels_from_mixer()
    host.current_page = "Edit"
    assert props is host.dock_properties.widget()
    assert f"clip:{clip_id}:transform" in props._sections
    host.show_mixer()
    assert id(host.edit_properties_panel) == identity
    assert props.parentWidget() is host.dock_audio_properties.widget()


def test_library_and_clip_edit_share_properties_chain_dialog_and_history(inspector_host, qtbot):
    host = inspector_host
    view = host.timeline_panel.timeline_view
    clip = _clip(host)
    host.dock_audio_effects.widget().effect_activated.emit("delay")
    delay, = _clip(host).effects
    assert [section.effect_type for section in _effects(host)] == ["delay"]
    host._open_project_audio_clip_editor(clip.clip_id)
    editor = host.dock_audio_clip_editor.widget()
    rack = editor._panel.effect_rack
    assert [row[0] for row in rack._rows] == [delay.effect_id]

    manager = host._get_clip_audio_effect_dialog_manager()
    dialog = manager._dialogs[clip.clip_id, delay.effect_id]
    _effects(host)[0]._effect_open_dialog_btn.click()
    rack.settings_requested.emit(clip.clip_id, delay.effect_id, "delay")
    assert manager._dialogs[clip.clip_id, delay.effect_id] is dialog
    assert len(manager._dialogs) == 1

    rack.add_requested.emit("reverb", clip.clip_id)
    assert [section.effect_type for section in _effects(host)] == ["delay", "reverb"]
    assert len(rack._rows) == 2
    view.undo()
    assert [section.effect_type for section in _effects(host)] == ["delay"]
    assert len(rack._rows) == 1
    view.redo()
    assert [section.effect_type for section in _effects(host)] == ["delay", "reverb"]

    qtbot.mouseClick(_effects(host)[0]._effect_enable_btn, Qt.MouseButton.LeftButton)
    assert not _clip(host).effects[0].is_enabled
    assert not rack._rows[0][2]
    view.undo()
    assert _effects(host)[0]._effect_enabled and rack._rows[0][2]
    rack.reorder_requested.emit(clip.clip_id, delay.effect_id, 1)
    assert [section.effect_type for section in _effects(host)] == ["reverb", "delay"]
    view.undo()
    rack.remove_requested.emit(clip.clip_id, delay.effect_id)
    assert [section.effect_type for section in _effects(host)] == ["reverb"]
    view.undo()
    assert [section.effect_type for section in _effects(host)] == ["delay", "reverb"]
    persisted = TimelineSequence.from_dict(view.get_timeline().to_dict())
    assert persisted.get_clip_by_id(clip.clip_id).effects == _clip(host).effects


def test_drop_on_another_clip_shows_that_clips_effects(inspector_host, monkeypatch):
    host = inspector_host
    view = host.timeline_panel.timeline_view
    first = _clip(host)
    other = replace(first, clip_id="another", timeline_start=first.timeline_duration)
    sequence = view.get_timeline().with_clip_added(first.track_id, other)
    view.load_timeline(sequence)
    host.sync_timeline(sequence)
    view._drag_effect_type = "delay"
    monkeypatch.setattr(view, "_get_track_and_frame_from_pos", lambda _pos: (0, other.timeline_start + 1))
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, QMimeData(),
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    view._handle_effect_drop(event)
    assert event.isAccepted()
    assert view.selected_audio_clip_id() == other.clip_id
    assert host.edit_properties_panel._current_clips[0].clip_id == other.clip_id
    assert [section.effect_type for section in _effects(host)] == ["delay"]
    assert view.get_timeline().get_clip_by_id(first.clip_id).effects == ()


def test_gain_and_sample_fades_share_clip_edit_values_and_undo(inspector_host, qtbot):
    host = inspector_host
    view = host.timeline_panel.timeline_view
    clip = _clip(host)
    host._open_project_audio_clip_editor(clip.clip_id)
    editor = host.dock_audio_clip_editor.widget()
    props = host.edit_properties_panel

    def value_widget(key):
        return props._sections[f"clip:{clip.clip_id}:audio_clip"]._param_widgets[key]["widget"]

    value_widget("gain_db").setValue(-6.0)
    assert _clip(host).audio_gain_db == -6.0
    assert editor._clip.audio_gain_db == -6.0
    value_widget("fade_in_ms").setValue(12.5)
    assert _clip(host).fade_in_samples == 600
    assert editor._clip.fade_in_samples == 600
    value_widget("fade_out_ms").setValue(25.0)
    assert _clip(host).fade_out_samples == 1200
    assert _clip(host).fade_in_samples == 600
    view.undo()
    assert value_widget("fade_out_ms").value() == 0.0
    view.redo()
    assert value_widget("fade_out_ms").value() == pytest.approx(25.0)
    props._on_section_reset(f"clip:{clip.clip_id}:audio_clip")
    assert _clip(host).audio_gain_db == 0.0
    assert _clip(host).fade_in_samples == _clip(host).fade_out_samples == 0
    view.undo()
    assert _clip(host).audio_gain_db == -6.0
    assert _clip(host).fade_in_samples == 600 and _clip(host).fade_out_samples == 1200

    editor.gain_committed.emit(clip.clip_id, -9.0)
    assert value_widget("gain_db").value() == -9.0
    editor.sample_fade_committed.emit(clip.clip_id, 2400, -1)
    assert value_widget("fade_in_ms").value() == pytest.approx(50.0)


def test_track_fader_changes_survive_properties_effect_rebuild(inspector_host):
    host = inspector_host
    host.dock_audio_effects.widget().effect_activated.emit("delay")
    view = host.timeline_panel.timeline_view
    from src.features.edit.core.commands import SetTrackVolumeCommand, SetTrackPanCommand

    view.execute_command(SetTrackVolumeCommand.create("audio-track", 0.25))
    view.execute_command(SetTrackPanCommand.create("audio-track", -0.5))
    refs = host.edit_properties_panel._audio_widget_refs["audio-track"]
    assert refs["volume"][0].value() == 25
    assert refs["volume"][1].value() == 25.0
    assert refs["pan"][1].value() == -50.0
    # Rebuild the effect sections after the fader's canonical track changed.
    host._on_edit_effect_enabled_changed(_clip(host).clip_id, _clip(host).effects[0].effect_id, False)
    refs = host.edit_properties_panel._audio_widget_refs["audio-track"]
    assert refs["volume"][1].value() == 25.0
    assert refs["pan"][1].value() == -50.0
    view.undo()
    view.undo()
    assert host.edit_properties_panel._audio_widget_refs["audio-track"]["pan"][1].value() == 0.0


def test_locked_track_rejects_properties_edit_and_restores_value(inspector_host):
    host = inspector_host
    view = host.timeline_panel.timeline_view
    sequence = view.get_timeline()
    track = sequence.audio_tracks[0]
    sequence = sequence.with_track_updated(replace(track, is_locked=True))
    view.load_timeline(sequence)
    host.sync_timeline(sequence)
    clip = _clip(host)
    field = host.edit_properties_panel._sections[f"clip:{clip.clip_id}:audio_clip"]._param_widgets["gain_db"]["widget"]
    field.setValue(-6.0)
    assert view.get_timeline() is sequence
    assert field.value() == 0.0
