"""Input routing, track arming and transport for the Studio Mixer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDockWidget, QWidget

from src.shared.i18n.i18n import I18N
from src.shared.ui.shortcut_scope import text_input_focused

if TYPE_CHECKING:
    from PyQt6.QtCore import QObject
    from PyQt6.QtGui import QKeyEvent

    from src.features.edit.core.shared_recording_input import SharedRecordingInput
    from src.features.edit.core.timeline_v2 import TimelineSequence


_INPUTS_KEY = "mixer_recording_inputs"


@dataclass(frozen=True)
class _InputPatch:
    device_id: str
    device_name: str
    channels: tuple[int, ...]

    @classmethod
    def from_dict(cls, value: object) -> _InputPatch | None:
        if not isinstance(value, dict):
            return None
        device_id = value.get("device_id")
        channels = value.get("channels")
        if (not isinstance(device_id, str) or not device_id
                or not isinstance(channels, list) or len(channels) not in (1, 2)
                or any(type(channel) is not int or channel < 0 for channel in channels)
                or len(set(channels)) != len(channels)):
            return None
        name = value.get("device_name")
        return cls(device_id, name if isinstance(name, str) else device_id, tuple(channels))

    def to_dict(self) -> dict[str, object]:
        return {"device_id": self.device_id, "device_name": self.device_name,
                "channels": list(self.channels)}

    def label(self) -> str:
        channels = "/".join(str(channel + 1) for channel in self.channels)
        return f"{channels} · {self.device_name}"


def _input_patches(timeline: TimelineSequence | None) -> dict[str, _InputPatch]:
    payload = timeline.format_settings.get(_INPUTS_KEY) if timeline is not None else None
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    tracks = payload.get("tracks")
    if not isinstance(tracks, dict):
        return {}
    return {
        track_id: patch for track_id, value in tracks.items()
        if (patch := _InputPatch.from_dict(value)) is not None
    }


class MixerRecordingControllerMixin:
    """Keep arming scoped to the visible project; persist only input routing."""

    _mixer_recording_workflow_active = False
    _mixer_recording_scope: tuple[str, str] | None = None
    _mixer_transport_starting = False
    _mixer_transport_playing = False

    def _set_mixer_recording_workflow(self, active: bool) -> None:
        self._mixer_recording_workflow_active = active
        panel = getattr(self, "audio_mixer_panel", None)
        if panel is not None and hasattr(panel, "set_recording_workflow_active"):
            panel.set_recording_workflow_active(active)
        if not active:
            self._finish_mixer_timeline_recordings()
            self._clear_mixer_armed_inputs()
            self._mixer_recording_scope = None
            # _refresh_mixer_recording_controls only mirrors states while the
            # workflow is on, so retire the header indicators explicitly.
            view = self._edit_timeline_view()
            timeline = view.get_timeline() if view is not None else None
            if view is not None and timeline is not None:
                view.set_mixer_record_arm_busy(False)
                for track in timeline.audio_tracks:
                    view.set_track_recording_state(track.track_id, "unpatched")
        self._refresh_mixer_recording_controls()

    def _refresh_mixer_recording_controls(self) -> None:
        active = self._mixer_recording_workflow_active
        view = self._edit_timeline_view()
        timeline = view.get_timeline() if view is not None else None
        project = getattr(self, "_current_edit_project", None)
        scope = (project.project_id, timeline.timeline_id) if project is not None and timeline is not None else None
        if scope != self._mixer_recording_scope:
            self._mixer_recording_scope = scope
            self._clear_mixer_armed_inputs()

        armed = getattr(self, "_mixer_armed_inputs", {})
        recorders = getattr(self, "_mixer_timeline_recorders", {})
        patches = _input_patches(timeline) if scope is not None and active else {}
        eligible = {track.track_id for track in timeline.audio_tracks
                    if not track.is_bus and not track.is_locked} if timeline is not None else set()
        for track_id, patch in tuple(armed.items()):
            if track_id not in eligible or patches.get(track_id) != patch:
                self._disarm_mixer_input(track_id)

        busy = (bool(recorders) or self._mixer_transport_starting
                or getattr(self, "_mixer_recording_close_pending", False))
        panel = getattr(self, "audio_mixer_panel", None)
        if panel is not None and hasattr(panel, "set_record_arm_state") and active:
            panel.set_recording_inputs({track_id: patch.label() for track_id, patch in patches.items()})
            panel.set_record_arm_state(armed, busy=busy)
        if view is not None and active and timeline is not None:
            view.set_mixer_record_arm_busy(busy)
            for track in timeline.audio_tracks:
                track_id = track.track_id
                recorder = recorders.get(track_id)
                if recorder is not None and recorder.state not in {"idle", "failed"}:
                    state = recorder.state
                elif track_id in armed:
                    state = "armed"
                else:
                    state = "idle" if track_id in patches else "unpatched"
                view.set_track_recording_state(track_id, state)
        viewport = getattr(self, "edit_viewport", None)
        if viewport is not None and hasattr(viewport, "set_mixer_recording_controls"):
            recording = any(manager.state in {"pre_roll", "recording"} for manager in recorders.values())
            viewport.set_mixer_recording_controls(
                active, can_record=bool(armed) and not busy, recording=recording,
            )

    def _on_mixer_input_requested(self, track_id: str) -> None:
        if not self._mixer_recording_workflow_active or getattr(self, "_mixer_timeline_recorders", None):
            return
        view = self._edit_timeline_view()
        timeline = view.get_timeline() if view is not None else None
        project = getattr(self, "_current_edit_project", None)
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        if project is None or track is None or track.track_type != "audio" or track.is_bus or track.is_locked:
            return

        previous = _input_patches(timeline).get(track_id)
        accepted, patch = self._choose_mixer_input(track.name or track_id, previous)
        if not accepted or patch == previous:
            return
        current_project = getattr(self, "_current_edit_project", None)
        current = view.get_timeline()
        track = current.get_track_by_id(track_id) if current is not None else None
        if (current_project is None or current_project.project_id != project.project_id
                or current is None or current.timeline_id != timeline.timeline_id
                or track is None or track.is_bus or track.is_locked
                or not self._mixer_recording_workflow_active
                or getattr(self, "_mixer_timeline_recorders", None)):
            return
        self._commit_mixer_input(view, current, track_id, patch)

    def _choose_mixer_input(
        self, track_name: str, previous: _InputPatch | None,
    ) -> tuple[bool, _InputPatch | None]:
        from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout
        from src.features.edit.core.shared_recording_input import SharedRecordingInput

        devices = {device_id: (name, count) for device_id, name, count in SharedRecordingInput.available_devices()}
        dialog = QDialog(self._main_window_ref() or None)
        dialog.setWindowTitle(I18N.tr("mixer.record.input", "Recording input") + f" · {track_name}")
        form = QFormLayout(dialog)
        device = QComboBox()
        device.addItem(I18N.tr("mixer.record.no_input", "No input"), None)
        for device_id, (name, _count) in devices.items():
            device.addItem(name, device_id)
        if previous is not None and previous.device_id not in devices:
            device.addItem(previous.device_name + " · " + I18N.tr("mixer.record.unavailable", "Unavailable"),
                           previous.device_id)
        first = QComboBox()
        second = QComboBox()

        def sync_channels() -> None:
            _name, count = devices.get(device.currentData(), ("", 0))
            first.clear()
            second.clear()
            second.addItem(I18N.tr("mixer.record.mono", "Mono"), None)
            for channel in range(count):
                first.addItem(str(channel + 1), channel)
                second.addItem(str(channel + 1), channel)
            first.setEnabled(count > 0)
            second.setEnabled(count > 1)

        device.currentIndexChanged.connect(sync_channels)
        if previous is not None:
            device.setCurrentIndex(device.findData(previous.device_id))
        sync_channels()
        if previous is not None and previous.device_id in devices:
            first.setCurrentIndex(max(0, first.findData(previous.channels[0])))
            if len(previous.channels) == 2:
                second.setCurrentIndex(max(0, second.findData(previous.channels[1])))
        form.addRow(I18N.tr("edit.adr.device", "Input device"), device)
        form.addRow(I18N.tr("mixer.record.first_channel", "First channel"), first)
        form.addRow(I18N.tr("mixer.record.second_channel", "Second channel"), second)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)

        def validate() -> None:
            buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
                second.currentData() is None or second.currentData() != first.currentData())

        first.currentIndexChanged.connect(validate)
        second.currentIndexChanged.connect(validate)
        validate()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False, previous
        device_id = device.currentData()
        if device_id is None:
            return True, None
        if device_id not in devices:
            return True, previous
        channels = (first.currentData(),)
        if second.currentData() is not None:
            channels += (second.currentData(),)
        return True, _InputPatch(device_id, devices[device_id][0], channels)

    def _commit_mixer_input(
        self, view: Any, timeline: TimelineSequence, track_id: str, patch: _InputPatch | None,
    ) -> None:
        from src.features.edit.core.timeline_settings_command import TimelineSettingsCommand

        settings = dict(timeline.format_settings)
        stored = settings.get(_INPUTS_KEY, {"version": 1})
        if not isinstance(stored, dict) or stored.get("version") != 1:
            self._show_mixer_recording_message(
                I18N.tr("mixer.record.patch_version", "This timeline uses a newer recording input format."))
            return
        tracks = stored.get("tracks")
        tracks = dict(tracks) if isinstance(tracks, dict) else {}
        if patch is None:
            tracks.pop(track_id, None)
        else:
            old = tracks.get(track_id)
            tracks[track_id] = {**(old if isinstance(old, dict) else {}), **patch.to_dict()}
        settings[_INPUTS_KEY] = {**stored, "tracks": tracks}
        update = replace(timeline, format_settings=settings)
        view.execute_command(TimelineSettingsCommand(
            str(uuid4()), I18N.tr("mixer.record.input", "Recording input"), timeline, update,
        ))
        self._refresh_mixer_recording_controls()

    def _on_mixer_record_arm_toggled(self, track_id: str, armed: bool) -> None:
        """Workflow mode arms the patched track; legacy mode records directly."""
        if self._mixer_recording_workflow_active:
            self._set_mixer_track_armed(track_id, armed)
        else:
            self._legacy_mixer_record_arm(track_id, armed)

    def _on_mixer_timeline_record_requested(self, track_id: str) -> None:
        self._set_mixer_track_armed(track_id, track_id not in getattr(self, "_mixer_armed_inputs", {}))

    def _set_mixer_track_armed(self, track_id: str, armed: bool) -> None:
        from src.features.edit.core.shared_recording_input import SharedRecordingInput

        if not self._mixer_recording_workflow_active or getattr(self, "_mixer_timeline_recorders", None):
            return
        if not armed:
            self._disarm_mixer_input(track_id)
            self._refresh_mixer_recording_controls()
            return
        self._refresh_mixer_recording_controls()
        view = self._edit_timeline_view()
        timeline = view.get_timeline() if view is not None else None
        track = timeline.get_track_by_id(track_id) if timeline is not None else None
        patch = _input_patches(timeline).get(track_id)
        if (self._mixer_recording_scope is None or track is None or track.track_type != "audio"
                or track.is_bus or track.is_locked or patch is None):
            return
        if not hasattr(self, "_mixer_armed_inputs"):
            self._mixer_armed_inputs: dict[str, _InputPatch] = {}
            self._mixer_capture_inputs: dict[str, SharedRecordingInput] = {}
        if track_id in self._mixer_armed_inputs:
            return
        counts = {device_id: count for device_id, _name, count in SharedRecordingInput.available_devices()}
        if max(patch.channels) >= counts.get(patch.device_id, 0):
            self._on_mixer_input_error(track_id, I18N.tr(
                "mixer.record.input_unavailable", "The selected input or channel is unavailable."))
            return
        source = self._mixer_capture_inputs.get(patch.device_id)
        if source is None:
            source = SharedRecordingInput(patch.device_id, counts[patch.device_id])
            self._mixer_capture_inputs[patch.device_id] = source
        self._mixer_armed_inputs[track_id] = patch
        panel = self.audio_mixer_panel
        source.monitor(
            track_id, patch.channels,
            lambda dbfs: panel.set_recording_input_level(track_id, dbfs),
            lambda message: self._on_mixer_input_error(track_id, message),
        )
        self._refresh_mixer_recording_controls()

    def _on_mixer_input_error(self, track_id: str, message: str) -> None:
        self._disarm_mixer_input(track_id)
        self._refresh_mixer_recording_controls()
        self._show_mixer_recording_message(message)

    def _disarm_mixer_input(self, track_id: str) -> None:
        armed = getattr(self, "_mixer_armed_inputs", {})
        patch = armed.pop(track_id, None)
        if patch is None:
            return
        source = self._mixer_capture_inputs.get(patch.device_id)
        if source is not None:
            source.unmonitor(track_id)
            if all(item.device_id != patch.device_id for item in armed.values()):
                self._mixer_capture_inputs.pop(patch.device_id, None)

    def _clear_mixer_armed_inputs(self) -> None:
        for track_id in tuple(getattr(self, "_mixer_armed_inputs", {})):
            self._disarm_mixer_input(track_id)

    def _handle_mixer_record_shortcut(self, target: QObject, event: QKeyEvent) -> bool:
        if (event.key() != Qt.Key.Key_Space
                or event.modifiers() != Qt.KeyboardModifier.ShiftModifier
                or getattr(self, "current_page", None) != "Mixer"
                or not self._mixer_recording_workflow_active
                or not isinstance(target, QWidget)
                or QApplication.activeModalWidget() is not None
                or QApplication.activePopupWidget() is not None
                or text_input_focused()):
            return False
        window = target.window()
        if window is not self and not (
            isinstance(window, QDockWidget) and window.parentWidget() is self
        ):
            return False
        if not event.isAutoRepeat():
            self._on_mixer_transport_record_requested()
        return True

    def _on_mixer_transport_record_requested(self) -> None:
        if (not self._mixer_recording_workflow_active or self._mixer_transport_starting
                or getattr(self, "_mixer_recording_close_pending", False)):
            return
        if (getattr(self, "_mixer_timeline_recorders", None)
                or getattr(self, "_adr_pass", None) is not None
                or any(manager.is_recording for manager in getattr(self, "_recording_managers", {}).values())):
            self._show_mixer_recording_message(I18N.tr("mixer.record.busy", "Finish the current recording first"))
            return
        self._refresh_mixer_recording_controls()
        armed = tuple(getattr(self, "_mixer_armed_inputs", {}).items())
        if not armed:
            self._show_mixer_recording_message(I18N.tr("mixer.record.transport_arm_first", "Arm a track to record"))
            return
        self.timeline_panel.pause_playback()
        anchor = self.timeline_panel.get_current_frame()
        self._mixer_transport_starting = True
        try:
            for track_id, patch in armed:
                source = self._mixer_capture_inputs.get(patch.device_id)
                if source is not None:
                    self._start_recording(
                        track_id, voiceover=True, from_mixer_timeline=True, anchor_frame=anchor,
                        recording_settings={"device_id": patch.device_id, "channels": len(patch.channels),
                                            "pre_roll_seconds": 0, "post_roll_seconds": 0, "monitor": False},
                        shared_input=source, input_channels=patch.channels,
                    )
        finally:
            self._mixer_transport_starting = False
            self._refresh_mixer_recording_controls()
        if any(manager.is_recording for manager in getattr(self, "_mixer_timeline_recorders", {}).values()):
            self._mixer_transport_playing = True
            self.timeline_panel.start_playback()

    def _on_mixer_recording_playback_state(self, state: str) -> None:
        # Playback naturally reaches the end of existing material. Capture keeps
        # going there until the user stops it. An explicit pause also stops capture.
        if state == "paused" and self._mixer_transport_playing:
            self._stop_mixer_transport_recording()

    def _on_program_transport_stop_requested(self) -> None:
        # The ADR session lives in AdrRecordingControllerMixin, which hosts
        # may load separately from this mixin.
        stop_adr = getattr(self, "_stop_adr_session", None)
        if callable(stop_adr):
            stop_adr()
        if not self._stop_mixer_transport_recording():
            controller = self.timeline_panel.get_playback_controller()
            if controller is not None:
                controller.stop_in_place()

    def _stop_mixer_transport_recording(self) -> bool:
        recorders = tuple(getattr(self, "_mixer_timeline_recorders", {}).items())
        if not recorders:
            self._mixer_transport_playing = False
            return False
        self._mixer_transport_playing = False
        for track_id, recorder in recorders:
            if recorder.state in {"pre_roll", "recording"}:
                self._stop_recording_and_import(track_id)
        controller = self.timeline_panel.get_playback_controller()
        if controller is not None:
            controller.stop_in_place()
        return True

    def _show_mixer_recording_message(self, message: str) -> None:
        status = getattr(self, "status_bar", None)
        if status is not None:
            status.showMessage(message, 5000)
