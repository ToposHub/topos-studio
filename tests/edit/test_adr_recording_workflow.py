"""ADR cue persistence, rehearsal, recording and take selection."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QWidget
from pytestqt.qtbot import QtBot

from src.app.controllers.adr_recording_controller import AdrRecordingControllerMixin
from src.features.edit.core.adr_cues import AdrCue, SetAdrCuesCommand, read_adr_cues
from src.features.edit.core.commands import CommandStack
from src.features.edit.core.recording_manager import TakeInfo
from src.features.edit.core.take_sheet import TakeSheet
from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel


def test_cues_roundtrip_undo_and_follow_timeline_fps() -> None:
    original = TimelineSequence(format_settings={"program_guide": {"type": "mask"}})
    cue = AdrCue("dialogue", 48, 96, "Alex", "Ready.")
    stack = CommandStack()
    stored = stack.execute(SetAdrCuesCommand.create(original, (cue,)), original)
    roundtripped = TimelineSequence.from_dict(stored.to_dict())
    assert read_adr_cues(roundtripped) == (cue,)
    assert read_adr_cues(replace(roundtripped, fps=48)) == (replace(cue, in_frame=96, out_frame=192),)
    assert stack.undo(stored).format_settings == original.format_settings


class _Transport(QWidget):
    playhead_changed = pyqtSignal(int)
    playback_state_changed = pyqtSignal(bool, float, int)

    def __init__(self, timeline: TimelineSequence) -> None:
        super().__init__()
        self.timeline = timeline
        self.frame = 0
        self.playing = False
        self.commands = CommandStack()

    def get_timeline_view(self) -> _Transport:
        return self

    def get_timeline(self) -> TimelineSequence:
        return self.timeline

    def get_current_frame(self) -> int:
        return self.frame

    def set_playhead_frame(self, frame: int) -> None:
        self.frame = frame
        self.playhead_changed.emit(frame)

    def execute_command(self, command: SetAdrCuesCommand) -> None:
        self.timeline = self.commands.execute(command, self.timeline)

    def start_playback(self) -> None:
        self.playing = True
        self.playback_state_changed.emit(True, 1.0, 1)

    def pause_playback(self) -> None:
        if self.playing:
            self.playing = False
            self.playback_state_changed.emit(False, 1.0, 1)

    def is_playing(self) -> bool:
        return self.playing

    def is_track_locked(self, _track_id: str) -> bool:
        return False


class _Host(AdrRecordingControllerMixin):
    def __init__(self, transport: _Transport, panel: AdrTakeSheetPanel) -> None:
        self.timeline_panel = transport
        self.panel = panel
        self._recording_managers: dict[str, Any] = {}
        self.started: list[tuple[str, dict[str, Any]]] = []
        self.stopped: list[str] = []
        self._wire_adr_session_controls(panel)

    def _adr_panel(self) -> AdrTakeSheetPanel:
        return self.panel

    def _start_recording(self, track_id: str, **options: Any) -> Any:
        self.started.append((track_id, options))
        manager = SimpleNamespace(is_recording=True, on_state_changed=self.panel.set_recording_state)
        self._recording_managers[track_id] = manager
        self.panel.set_recording_state("pre_roll")
        return manager

    def _stop_recording_and_import(self, track_id: str) -> None:
        self.stopped.append(track_id)
        self._recording_managers.pop(track_id).on_state_changed("idle")


def test_adr_record_tab_rehearses_then_records_selected_cue(qtbot: QtBot) -> None:
    track = EditTrack(track_id="dialogue", track_type="audio", name="Dialogue")
    transport = _Transport(TimelineSequence(audio_tracks=(track,)))
    panel = AdrTakeSheetPanel()
    qtbot.addWidget(transport)
    qtbot.addWidget(panel)
    host = _Host(transport, panel)
    cue = AdrCue("cue-1", 72, 120, "Alex", "Ready.")
    panel.cue_changed.emit(cue)
    assert panel.current_cue() == cue
    assert not panel._record_btn.isEnabled()

    panel._rehearse_btn.click()
    assert transport.frame == 24
    assert transport.is_playing()
    assert not host.started
    transport.set_playhead_frame(144)
    assert not transport.is_playing()

    panel._track_combo.setCurrentIndex(panel._track_combo.findData(track.track_id))
    panel._record_btn.click()
    assert host.started[0][0] == track.track_id
    assert host.started[0][1]["anchor_frame"] == 72
    assert host.started[0][1]["scene_name"] == cue.scene_name
    transport.set_playhead_frame(120)
    assert host.stopped == [track.track_id]
    assert not transport.is_playing()

    sheet = TakeSheet()
    sheet.add_take(TakeInfo(take_id="this-take", scene_name=cue.scene_name))
    sheet.add_take(TakeInfo(take_id="other-take", scene_name="other-cue"))
    panel.set_take_sheet(sheet)
    assert panel._table.rowCount() == 1

    panel.host_caption_tools(QWidget(), "Animation")
    assert panel._table.rowCount() == 2
    panel.leave_caption_workspace()
    assert panel._table.rowCount() == 1
