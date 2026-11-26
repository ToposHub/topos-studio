"""Exercise the device-facing playback block with real programme mixing."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from src.features.edit.core.audio_playback import (
    MASTER_BUS_KEY, AudioClock, AudioMixer, AudioPlaybackManager, AudioPlaybackThread, AudioTrackState,
)
from src.features.edit.core.monitor_service import (
    ControlRoomInput, ControlRoomPatch, MonitorSessionState,
)


class ConstantSource:
    _channels = 2
    _timeline_start = 0
    _timeline_duration = 100

    def __init__(self, left: float, right: float) -> None:
        self._frame = np.array([[left], [right]], dtype=np.float32)

    def decode_samples_for_frame(self, frame: int, fps: float, count: int) -> np.ndarray:
        return np.repeat(self._frame, count, axis=1)


class ImpulseSource(ConstantSource):
    def decode_samples_for_frame(self, frame: int, fps: float, count: int) -> np.ndarray:
        output = np.zeros((2, count), np.float32)
        if frame == 0:
            output[:, :1] = self._frame
        return output


class DeviceProbe:
    _channels = 2

    def __init__(self) -> None:
        self.writes: list[np.ndarray] = []

    def bytes_per_frame(self) -> int:
        return 8

    def bytes_free(self) -> int:
        return 16384

    def write_samples(self, samples: np.ndarray) -> int:
        self.writes.append(samples.copy())
        return samples.shape[-1]


class OutputOwnerProbe(DeviceProbe):
    def __init__(self, sample_rate: int = 48000, channels: int = 2, **options) -> None:
        super().__init__()
        self._sample_rate = sample_rate
        self._channels = channels
        self.is_playing = False
        self.resets: list[bool] = []

    def start(self) -> bool:
        self.is_playing = True
        return True

    def bytes_per_frame(self) -> int:
        return 4 * self._channels

    def stop(self) -> None:
        self.is_playing = False

    def suspend(self) -> None:
        self.is_playing = False

    def resume(self) -> None:
        self.is_playing = True

    def reset_buffer(self, *, keep_suspended: bool) -> None:
        self.resets.append(keep_suspended)
        self.is_playing = not keep_suspended


def test_real_device_feed_uses_pre_and_post_taps_without_changing_programme() -> None:
    mixer = AudioMixer()
    mixer.add_track("A", ConstantSource(0.12, -0.08), AudioTrackState("A", solo=True))
    mixer.add_track("B", ConstantSource(-0.16, 0.24), AudioTrackState("B", volume=0.25))
    sink = DeviceProbe()
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, sink, 24.0)
    state = MonitorSessionState(source_tap="pfl", selected_listen_ids=("B",))
    renderer.control_room_state = lambda: state

    renderer._prefill_ring_to(2000)
    assert renderer._write_next_output_chunk()
    np.testing.assert_allclose(sink.writes[-1], np.repeat([[-0.16], [0.24]], 2000, axis=1))
    assert renderer._programme_peaks == pytest.approx((0.12, 0.08))
    assert mixer.mix_samples(0, 24.0, 8)[1][MASTER_BUS_KEY].peak_l == pytest.approx(0.12)

    state = replace(state, source_tap="afl", dim=True, mono=True)
    renderer._prefill_ring_to(2000)
    assert renderer._write_next_output_chunk()
    # B's post fader levels average to .01, then DIM reaches x0.10.
    np.testing.assert_allclose(sink.writes[-1][:, 240:], 0.001, atol=1e-8)
    assert renderer._programme_peaks == pytest.approx((0.12, 0.08))


def test_monitor_gain_changes_device_pcm_with_master_levels_unchanged(qapp, monkeypatch) -> None:
    monkeypatch.setattr(ControlRoomPatch, "load_local", ControlRoomPatch)
    manager = AudioPlaybackManager()
    mixer = manager._mixer
    mixer.add_track("A", ConstantSource(0.2, -0.1))
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, None, 24.0)
    renderer.control_room_state = lambda: manager.control_room_state
    before, before_levels = renderer._render_output_block(0, 500, 2)
    manager._prepared_frame = 20
    generation = manager._scrub_preview_generation
    manager.set_monitor_gain_db(-20.0)
    after, after_levels = renderer._render_output_block(1, 500, 2)

    np.testing.assert_allclose(after[:, -1], before[:, -1] * 0.1)
    assert before_levels[MASTER_BUS_KEY].peak_l == after_levels[MASTER_BUS_KEY].peak_l
    assert manager._scrub_preview_generation == generation
    assert manager._prepared_frame is None


def test_bus_taps_keep_fader_and_master_processing_boundaries() -> None:
    mixer = AudioMixer()
    mixer.add_track("A", ConstantSource(0.2, 0.1))
    mixer.register_bus_track("bus", AudioTrackState("bus", volume=0.5))
    mixer.set_track_routing("A", "bus", [])
    mixer.set_track_routing("bus", None, [], is_bus=True)
    mixer.set_master_volume(0.25)
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, None, 24.0)
    state = MonitorSessionState(source_tap="pfl", selected_listen_ids=("bus",))
    renderer.control_room_state = lambda: state

    pre, _ = renderer._render_output_block(0, 500, 2)
    state = replace(state, source_tap="afl")
    post, levels = renderer._render_output_block(1, 500, 2)
    state = replace(state, source_tap="sip")
    master, _ = renderer._render_output_block(2, 500, 2)
    np.testing.assert_allclose(pre[:, 0], [0.2, 0.1])
    np.testing.assert_allclose(post[:, 0], [0.1, 0.05])
    np.testing.assert_allclose(master[:, 0], [0.025, 0.0125])
    assert levels[MASTER_BUS_KEY].peak_l == pytest.approx(0.025)


def test_afl_listen_taps_are_latency_aligned_without_programme_delay_state() -> None:
    from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance

    mixer = AudioMixer()
    mixer.add_track("dry", ImpulseSource(0.1, 0.1))
    mixer.add_track("lookahead", ImpulseSource(0.2, 0.2))
    mixer.set_track_effect_chain("lookahead", EffectChainParams((TrackEffectInstance(
        effect_type="true_peak_limiter", enabled=True,
        params={"ceiling_dbtp": -1.0, "lookahead_ms": 5.0},
    ),)))
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, None, 24.0)
    renderer.control_room_state = lambda: MonitorSessionState(
        source_tap="afl", selected_listen_ids=("dry", "lookahead"))

    output, _ = renderer._render_output_block(0, 500, 2)
    # 5 ms lookahead plus the limiter's ten-sample FIR group delay.
    assert np.flatnonzero(output[0]).tolist() == [250]
    assert output[0, 250] == pytest.approx(0.3)


def test_input_uses_same_output_patch_without_reaching_master() -> None:
    mixer = AudioMixer()
    mixer.add_track("programme", ConstantSource(0.1, -0.1))
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, None, 24.0, channels=10)
    renderer.control_room_state = lambda: MonitorSessionState(source_tap="input")
    renderer.control_room_patch = ControlRoomPatch(channels=(7, 4), device_channels=10, trim_db=(-6.0, 0.0))
    capture = ControlRoomInput(48000, 2)
    renderer.control_room_input = lambda: capture
    captured = np.repeat(np.float32([[0.4], [-0.2]]), 2000, axis=1)
    capture.push(captured)
    output, levels = renderer._render_output_block(0, 2000, 10)
    expected = np.zeros((10, 2000), np.float32)
    expected[7] = 0.4 * 10 ** (-6.0 / 20.0)
    expected[4] = -0.2
    np.testing.assert_allclose(output, expected)
    np.testing.assert_allclose(captured, np.repeat(np.float32([[0.4], [-0.2]]), 2000, axis=1))
    assert levels[MASTER_BUS_KEY].peak_l == pytest.approx(0.1)


def test_adr_return_adds_input_after_programme_meter() -> None:
    mixer = AudioMixer()
    mixer.add_track("guide", ConstantSource(0.1, 0.2))
    renderer = AudioPlaybackThread()
    renderer.setup(AudioClock(), mixer, None, 24.0)
    renderer.control_room_state = MonitorSessionState
    capture = ControlRoomInput(48000, 2)
    renderer.control_room_return = lambda: capture
    capture.push(np.repeat(np.float32([[0.3], [-0.4]]), 100, axis=1))
    output, levels = renderer._render_output_block(0, 100, 2)
    np.testing.assert_allclose(output[:, 0], [0.4, -0.2])
    assert levels[MASTER_BUS_KEY].peak_l == pytest.approx(0.1)


def test_stopped_input_uses_patch_and_releases_before_timeline_play(monkeypatch) -> None:
    from src.features.edit.core import audio_playback

    monkeypatch.setattr(audio_playback, "AudioOutput", OutputOwnerProbe)
    manager = AudioPlaybackManager()
    patch = ControlRoomPatch(device_id=b"room", channels=(2, 0), device_channels=4)
    state = MonitorSessionState(source_tap="input", selected_listen_ids=("A",), mono=True)
    manager.set_control_room(state, patch)
    manager.set_timeline_duration(50)
    capture = ControlRoomInput(48000, 2)
    manager.set_control_room_input(capture)
    pcm = np.repeat(np.float32([[0.4], [0.2]]), 500, axis=1)

    manager.submit_control_room_input(capture, pcm)
    stopped_device = manager._scrub_preview_output
    np.testing.assert_allclose(stopped_device.writes[-1][:, 0], [0.3, 0.0, 0.3, 0.0])
    manager.set_control_room(replace(state, dim=True), patch)
    manager.submit_control_room_input(capture, pcm)
    assert stopped_device is manager._scrub_preview_output
    np.testing.assert_allclose(stopped_device.writes[-1][:, -1], [0.03, 0.0, 0.03, 0.0])
    manager.set_control_room(replace(state, muted=True), patch)
    manager.submit_control_room_input(capture, pcm)
    np.testing.assert_array_equal(stopped_device.writes[-1][:, -1], np.zeros(4))

    assert not manager.preview_scrub_audio(3)
    assert not manager.prepare_for_playback(3)
    assert stopped_device.is_playing
    assert manager.get_current_frame() == 3

    def start_timeline_output(thread: AudioPlaybackThread) -> None:
        assert not stopped_device.is_playing

    monkeypatch.setattr(AudioPlaybackThread, "start", start_timeline_output)
    manager.play(3)
    manager.submit_control_room_input(capture, pcm)
    assert not stopped_device.is_playing
    assert capture.available_samples == 500
    manager.cleanup()
    writes = len(stopped_device.writes)
    manager.submit_control_room_input(capture, pcm)
    assert len(stopped_device.writes) == writes
    assert not stopped_device.is_playing


def test_paused_input_reuses_thread_sink_and_flushes_live_audio_on_resume() -> None:
    sink = OutputOwnerProbe()
    clock = AudioClock()
    renderer = AudioPlaybackThread()
    renderer.setup(clock, AudioMixer(), sink, 24.0, start_frame=10, start_paused=True)
    state = MonitorSessionState(source_tap="input")
    renderer.control_room_state = lambda: state
    capture = ControlRoomInput(48000, 2)
    renderer.control_room_input = lambda: capture
    pcm = np.repeat(np.float32([[0.4], [0.2]]), 960, axis=1)
    capture.push(pcm)
    renderer._ring.write(pcm)

    assert renderer._play_paused_input()
    assert sink.resets == [False]
    np.testing.assert_allclose(sink.writes[-1][:, 0], [0.4, 0.2])
    assert renderer._next_audio_frame == 10
    assert capture.available_samples == 480

    state = MonitorSessionState(source_tap="program")
    assert not renderer._play_paused_input()
    assert not sink.is_playing
    renderer._process_command(("resume", None))
    assert sink.resets == [False, True, False]
    assert sink.is_playing
    assert renderer._ring.filled == capture.available_samples == 0
    assert renderer._next_audio_frame == 10


def test_stopped_input_rate_mismatch_reports_once_and_never_writes(monkeypatch) -> None:
    manager = AudioPlaybackManager()
    manager.set_control_room(MonitorSessionState(source_tap="input"), ControlRoomPatch())
    capture = ControlRoomInput(48000, 2)
    manager.set_control_room_input(capture)
    sink = OutputOwnerProbe(sample_rate=44100)
    monkeypatch.setattr(manager, "_ensure_scrub_preview_output", lambda: sink)
    manager._scrub_preview_output = sink
    sink.start()
    failures = []
    manager.control_room_output_failed.connect(failures.append)
    for _ in range(2):
        manager.submit_control_room_input(capture, np.zeros((2, 256), np.float32))
    assert failures == ["control_room_input_rate"]
    assert not sink.writes
    assert not sink.is_playing


def test_control_room_dialog_uses_current_sequence_and_emits_local_state(qtbot) -> None:
    from src.features.edit.core.timeline_v2 import EditTrack, TimelineSequence
    from src.features.mixer.ui.control_room import ControlRoomDialog

    dialog = ControlRoomDialog()
    qtbot.addWidget(dialog)
    sequence = TimelineSequence(audio_tracks=(EditTrack("a", "Dialogue", "audio", 0),))
    dialog.set_session(sequence, MonitorSessionState(), ControlRoomPatch(), {"a": "1 · Input"})
    events = []
    dialog.state_requested.connect(events.append)
    dialog.source.setCurrentIndex(dialog.source.findData("pfl"))
    dialog.tracks.item(0).setSelected(True)
    dialog.dim.setChecked(True)
    assert events[-1].selected_listen_ids == ("a",)
    assert events[-1].source_tap == "pfl"
    assert events[-1].dim
    assert sequence.audio_tracks[0].volume == 1.0
