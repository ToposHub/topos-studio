"""Run ADR rehearsals and range recording through the timeline transport."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from src.shared.i18n.i18n import I18N

if TYPE_CHECKING:
    from src.features.edit.core.adr_cues import AdrCue
    from src.features.edit.core.recording_manager import RecordingManager
    from src.features.edit.core.timeline_v2 import TimelineSequence
    from src.features.edit.ui.adr_take_sheet_panel import AdrTakeSheetPanel


@dataclass
class _AdrPass:
    timeline_id: str
    cue: AdrCue
    fps: float
    track_id: str | None
    end_frame: int
    beep: bool
    repeat: bool
    manager: RecordingManager | None = None
    deadline: QTimer | None = None
    count_in: int = -1
    ending: bool = False
    failed: bool = False


class AdrRecordingControllerMixin:
    """Cue command and transport integration for the ADR dock."""

    _adr_pass: _AdrPass | None = None
    _adr_repeat_generation = 0

    def _wire_adr_session_controls(self, panel: AdrTakeSheetPanel) -> None:
        panel.cue_changed.connect(self._on_adr_cue_changed)
        panel.cue_removed.connect(self._on_adr_cue_removed)
        panel.cue_selected.connect(self._on_adr_cue_selected)
        panel.rehearse_requested.connect(self._on_adr_rehearse)
        panel.record_requested.connect(self._on_adr_record)
        panel.stop_requested.connect(self._on_adr_stop_requested)
        edit_page = getattr(self, "edit_page", None)
        start_voiceover = getattr(edit_page, "record_voiceover_at_playhead", None)
        if callable(start_voiceover):
            panel.playhead_record_requested.connect(start_voiceover)
        transport = getattr(self, "timeline_panel", None)
        if transport is not None:
            transport.playback_state_changed.connect(self._on_adr_playback_state)
            view = transport.get_timeline_view()
            view.playhead_changed.connect(self._on_adr_playhead)
            self._sync_adr_session_timeline(view.get_timeline())

    def _sync_adr_session_timeline(self, timeline: TimelineSequence | None) -> None:
        panel = self._adr_panel()
        if panel is None:
            return
        from src.features.edit.core.timeline_v2 import TimelineSequence

        timeline = timeline if isinstance(timeline, TimelineSequence) else None
        sync_recordings = getattr(self, "_sync_mixer_timeline_recorders", None)
        if callable(sync_recordings):
            sync_recordings(timeline)
        timeline_id = timeline.timeline_id if timeline is not None else None
        session = self._adr_pass
        if session is not None and (timeline_id != session.timeline_id or (
                session.track_id and timeline.get_track_by_id(session.track_id) is None)):
            self._stop_adr_session()
        panel.set_adr_context(timeline)
        if timeline is not None:
            panel.set_timeline_frame(self.timeline_panel.get_current_frame())

    def _on_adr_cue_changed(self, cue: AdrCue) -> None:
        from src.features.edit.core.adr_cues import read_adr_cues

        view = self.timeline_panel.get_timeline_view()
        timeline = view.get_timeline()
        if timeline is not None:
            cues = tuple(item for item in read_adr_cues(timeline) if item.cue_id != cue.cue_id)
            self._commit_adr_cues(timeline, (*cues, cue))

    def _on_adr_cue_removed(self, cue_id: str) -> None:
        from src.features.edit.core.adr_cues import read_adr_cues

        view = self.timeline_panel.get_timeline_view()
        timeline = view.get_timeline()
        if timeline is not None:
            cues = tuple(cue for cue in read_adr_cues(timeline) if cue.cue_id != cue_id)
            self._commit_adr_cues(timeline, cues)

    def _commit_adr_cues(self, timeline: TimelineSequence, cues: tuple[AdrCue, ...]) -> None:
        from src.features.edit.core.adr_cues import SetAdrCuesCommand

        try:
            view = self.timeline_panel.get_timeline_view()
            view.execute_command(SetAdrCuesCommand.create(timeline, cues))
        except (RuntimeError, ValueError) as exc:
            self._show_adr_message(str(exc))
            return
        self._sync_adr_session_timeline(view.get_timeline())

    def _on_adr_cue_selected(self, cue: AdrCue) -> None:
        if self._adr_pass is None:
            self.timeline_panel.set_playhead_frame(cue.in_frame)

    def _on_adr_rehearse(self, cue: AdrCue) -> None:
        self._begin_adr_pass(cue)

    def _on_adr_record(self, cue: AdrCue) -> None:
        panel = self._adr_panel()
        track_id = panel.adr_settings()["record_track_id"] if panel is not None else None
        if track_id:
            self._begin_adr_pass(cue, track_id=track_id)

    def _begin_adr_pass(self, cue: AdrCue, *, track_id: str | None = None) -> None:
        from src.features.edit.core.adr_cues import read_adr_cues

        panel = self._adr_panel()
        view = self.timeline_panel.get_timeline_view()
        timeline = view.get_timeline()
        if panel is None or timeline is None or self._adr_pass is not None:
            return
        if any(manager.is_recording for manager in getattr(self, "_recording_managers", {}).values()):
            self._show_adr_message(I18N.tr("edit.adr.recording_busy", "Finish the current recording first."))
            return
        if track_id is not None:
            track = timeline.get_track_by_id(track_id)
            if track is None or track.track_type != "audio" or track.is_bus or view.is_track_locked(track_id):
                self._show_adr_message(I18N.tr("edit.adr.select_track", "Select a track"))
                return
        if not any(item == cue for item in read_adr_cues(timeline)):
            self._sync_adr_session_timeline(timeline)
            return

        settings = panel.adr_settings()
        fps = float(timeline.fps)
        start_frame = max(0, cue.in_frame - round(settings["pre_roll_seconds"] * fps))
        end_frame = cue.out_frame + round(settings["post_roll_seconds"] * fps)
        actual_pre_roll = (cue.in_frame - start_frame) / fps
        session = _AdrPass(
            timeline.timeline_id, cue, fps, track_id, end_frame,
            settings["count_in_beeps"], bool(track_id and settings["loop"]),
        )
        self._adr_repeat_generation += 1
        self.timeline_panel.pause_playback()
        self.timeline_panel.set_playhead_frame(start_frame)
        self._adr_pass = session
        if track_id is not None:
            from src.features.edit.core.shared_recording_input import SharedRecordingInput
            from PyQt6.QtMultimedia import QMediaDevices

            device_id = settings["device_id"]
            if not device_id:
                device_id = bytes(QMediaDevices.defaultAudioInput().id()).decode("utf-8", "replace") or None
            source = getattr(self, "_mixer_capture_inputs", {}).get(device_id)
            channels = tuple(range(settings["channels"]))
            if source is None:
                source = SharedRecordingInput(device_id, len(channels))
            manager = self._start_recording(
                track_id, voiceover=True, anchor_frame=cue.in_frame, scene_name=cue.scene_name,
                recording_settings={**settings, "pre_roll_seconds": actual_pre_roll, "loop": False},
                shared_input=source, input_channels=channels,
            )
            if manager is None:
                self._adr_pass = None
                panel.set_recording_state("failed")
                self._show_adr_message(I18N.tr("edit.adr.recording_failed", "Unable to start the selected input."))
                return
            session.manager = manager
            manager.enable_background_flush = True
            on_state = manager.on_state_changed

            def state_changed(state: str) -> None:
                if state == "failed":
                    session.failed = True
                if on_state is not None:
                    on_state(state)
                if state == "idle":
                    managers = getattr(self, "_recording_managers", {})
                    if managers.get(track_id) is manager:
                        managers.pop(track_id)
                    self._end_adr_pass(session, "failed" if session.failed else state)

            manager.on_state_changed = state_changed
        else:
            panel.set_recording_state("rehearsing")

        self._on_adr_playhead(start_frame)
        self.timeline_panel.start_playback()
        if self._adr_pass is not session:
            return
        if not self.timeline_panel.is_playing():
            self._stop_adr_session()
            return
        # Keep audio capture bounded even if the viewer's playback stalls.
        deadline_frame = cue.out_frame if track_id is not None else end_frame
        timer = QTimer(panel)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._on_adr_deadline(session))
        session.deadline = timer
        timer.start(max(1, round(1000 * (deadline_frame - start_frame) / fps)) + 2000)

    def _on_adr_playhead(self, frame: int) -> None:
        panel = self._adr_panel()
        if panel is None:
            return
        panel.set_timeline_frame(frame)
        session = self._adr_pass
        if session is None:
            return
        count_in = max(0, math.ceil((session.cue.in_frame - frame) / session.fps))
        if count_in != session.count_in:
            session.count_in = count_in
            panel.set_count_in(count_in)
            if session.beep and 1 <= count_in <= 3:
                QApplication.beep()
        end_frame = session.cue.out_frame if session.manager is not None else session.end_frame
        if frame >= end_frame:
            self._on_adr_deadline(session)

    def _on_adr_deadline(self, session: _AdrPass) -> None:
        if self._adr_pass is not session or session.ending:
            return
        if session.track_id is None:
            self._end_adr_pass(session, "idle")
            return
        session.ending = True
        self._stop_recording_and_import(session.track_id)

    def _on_adr_playback_state(self, playing: bool, _speed: float, _direction: int) -> None:
        if not playing and self._adr_pass is not None and not self._adr_pass.ending:
            self._stop_adr_session()

    def _on_adr_stop_requested(self) -> None:
        if self._adr_pass is not None:
            self._stop_adr_session()
            return
        edit_page = getattr(self, "edit_page", None)
        stop_voiceover = getattr(edit_page, "stop_playhead_voiceover", None)
        if callable(stop_voiceover):
            stop_voiceover()

    def _stop_adr_session(self) -> None:
        self._adr_repeat_generation += 1
        session = self._adr_pass
        if session is None:
            return
        session.repeat = False
        if session.manager is not None:
            if not session.ending:
                self._on_adr_deadline(session)
            self.timeline_panel.pause_playback()
        else:
            self._end_adr_pass(session, "idle")

    def _finish_adr_session(self) -> None:
        self._stop_adr_session()
        session = self._adr_pass
        if session is not None and session.manager is not None and session.manager.state == "stopping":
            session.manager.stop_recording()

    def _end_adr_pass(self, session: _AdrPass, state: str) -> None:
        if self._adr_pass is not session:
            return
        self._adr_pass = None
        if session.deadline is not None:
            session.deadline.stop()
            session.deadline.deleteLater()
        self.timeline_panel.pause_playback()
        if getattr(self, "_mixer_recording_close_pending", False):
            QTimer.singleShot(0, self.close)
        panel = self._adr_panel()
        if panel is not None:
            panel.end_monitor()
            panel.set_recording_state(state)
            if session.repeat and state == "idle":
                generation = self._adr_repeat_generation

                def repeat() -> None:
                    if self._adr_repeat_generation == generation:
                        self._begin_adr_pass(session.cue, track_id=session.track_id)

                QTimer.singleShot(0, repeat)

    def _show_adr_message(self, text: str) -> None:
        status_bar: Any = getattr(self, "status_bar", None)
        if status_bar is not None:
            status_bar.showMessage(text, 4000)
