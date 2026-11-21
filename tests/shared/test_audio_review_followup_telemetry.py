"""Natural gaps clear real FX windows through the playback-thread output path."""

from typing import Optional

import numpy as np
import pytest

from src.features.edit.core.audio_playback import (
    AudioPlaybackManager, AudioPlaybackThread, AudioTrackState, MASTER_BUS_KEY,
)
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


class _Decoder:
    _channels = 2

    def __init__(self, frames: Optional[set[int]] = None) -> None:
        self.frames = frames

    def decode_samples_for_frame(self, frame: int, fps: float,
                                 num_samples: int) -> Optional[np.ndarray]:
        if self.frames is not None and frame not in self.frames:
            return None
        t = np.arange(num_samples) / 48000.0
        signal = (0.2 * np.sin(2.0 * np.pi * 1000.0 * t)).astype(np.float32)
        return np.stack([signal, signal])

    def close(self) -> None:
        pass


class _Output:
    """Only the physical device is replaced; mixing and signals remain real."""
    _channels = 2

    def bytes_per_frame(self) -> int:
        return 8

    def bytes_free(self) -> int:
        return 1000000

    def write_samples(self, samples: np.ndarray) -> int:
        return samples.shape[-1]


def _thread(manager: AudioPlaybackManager) -> AudioPlaybackThread:
    thread = AudioPlaybackThread()
    thread._mixer = manager._mixer
    thread._clock = manager._clock
    thread._fps = 25.0
    thread._samples_per_frame = 1920
    thread._samples_per_frame_exact = 1920.0
    thread._output = _Output()
    thread.audio_track_levels.connect(manager._on_audio_track_levels)
    thread.audio_level.connect(manager._on_audio_level)
    manager._clock.start(0, 25.0)
    return thread


def _write(thread: AudioPlaybackThread, prefill: bool = True) -> None:
    # Deterministically make this output cycle eligible for the 30ms meter tick.
    thread._last_track_level_emit_ms = 0.0
    if prefill:
        thread._prefill_ring_to(1920)
    assert thread._write_next_output_chunk()


@pytest.mark.parametrize("prefill", [True, False], ids=["prefill", "direct-mix"])
def test_natural_gap_clears_five_open_fx_windows(qapp, prefill: bool) -> None:
    manager = AudioPlaybackManager()
    manager._mixer.add_track("T", _Decoder({0}), AudioTrackState(track_id="T"))
    manager._mixer.set_spectrum_metering(True)
    panel = AudioMixerPanel()
    panel.register_track("T", "Track", "A1")
    strip = panel._track_mixers["T"]
    chain = EffectChainParams(effects=(
        TrackEffectInstance(effect_type="reverb", params={}, instance_id="R"),
        TrackEffectInstance(effect_type="delay", params={}, instance_id="D", order=1),
    ))
    strip.set_effect_chain(chain)
    strip._open_eq_from_badge()
    strip._open_dynamics_panel()
    strip._open_panner_dialog()
    strip._open_reverb_editor(chain.effects[0])
    strip._open_delay_editor(chain.effects[1])
    windows = [strip._eq_dialog, strip._dynamics_dialog, strip._panner_dialog,
               strip._reverb_dialogs["R"], strip._delay_dialogs["D"]]
    manager.audio_track_levels_changed.connect(panel.update_per_track_levels)
    manager.audio_level_changed.connect(panel.update_master_level)
    events: list[dict] = []
    manager.audio_track_levels_changed.connect(events.append)
    thread = _thread(manager)
    try:
        _write(thread, prefill)
        qapp.processEvents()
        assert all(window.isVisible() for window in windows)
        assert strip._eq_dialog.panel.master_strip._meter_db > -50.0
        assert strip._eq_dialog.eq_graph.has_spectrum()
        assert strip._dynamics_dialog.panel.meter_input._value_db > -50.0
        assert strip._reverb_dialogs["R"].panel.output_meter._meter_db > -50.0
        assert strip._delay_dialogs["D"].panel.output_meter._l_db > -50.0

        _write(thread, prefill)
        qapp.processEvents()
        for tid in ("T", MASTER_BUS_KEY):
            assert events[-1][tid]["silent"] is True
            assert events[-1][tid]["peak"] == [-100.0, -100.0]
            assert events[-1][tid]["gr"] == events[-1][tid]["sc"] == {}
            assert events[-1][tid]["taps"] == {}
            assert events[-1][tid]["spectrum"] is None
        assert strip._eq_dialog.panel.master_strip._meter_db == -60.0
        assert not strip._eq_dialog.eq_graph.has_spectrum()
        assert strip._dynamics_dialog.panel.meter_input._value_db <= -55.0
        assert strip._dynamics_dialog.panel.meter_output._value_db <= -55.0
        assert strip._dynamics_dialog.panel.meter_gr_comp._value_db == 0.0
        assert strip._reverb_dialogs["R"].panel.output_meter._meter_db == -60.0
        assert strip._delay_dialogs["D"].panel.output_meter._l_db == -55.0
        assert all(window.isVisible() for window in windows)
    finally:
        manager.cleanup()
        for window in windows:
            window.close()
            window.deleteLater()
        panel.close()
        panel.deleteLater()


def test_missing_track_clears_while_another_track_keeps_playing(qapp) -> None:
    manager = AudioPlaybackManager()
    manager._mixer.add_track("ends", _Decoder({0}), AudioTrackState(track_id="ends"))
    manager._mixer.add_track("continues", _Decoder(), AudioTrackState(track_id="continues"))
    thread = _thread(manager)
    events: list[dict] = []
    manager.audio_track_levels_changed.connect(events.append)
    try:
        _write(thread)
        _write(thread)
        assert events[-1]["ends"]["silent"] is True
        assert events[-1]["continues"]["peak"][0] > -50.0
        assert not events[-1]["continues"].get("silent", False)
    finally:
        manager.cleanup()


def test_ring_read_without_new_mix_does_not_clear_valid_meters(qapp) -> None:
    manager = AudioPlaybackManager()
    manager._mixer.add_track("T", _Decoder(), AudioTrackState(track_id="T"))
    thread = _thread(manager)
    events: list[dict] = []
    manager.audio_track_levels_changed.connect(events.append)
    try:
        thread._prefill_ring_to(3 * 1920)
        _write(thread, prefill=False)
        assert len(events) == 1
        assert events[0]["T"]["peak"][0] > -50.0
        # Two more output writes consume existing ring samples, not fresh silence.
        _write(thread, prefill=False)
        _write(thread, prefill=False)
        assert len(events) == 1
    finally:
        manager.cleanup()


def test_silent_mix_replaces_measurement_waiting_for_throttle(qapp) -> None:
    manager = AudioPlaybackManager()
    manager._mixer.add_track("T", _Decoder({0, 1}), AudioTrackState(track_id="T"))
    thread = _thread(manager)
    events: list[dict] = []
    manager.audio_track_levels_changed.connect(events.append)
    try:
        _write(thread)
        thread._prefill_ring_to(1920)
        # The next mix is already silent before a new UI tick is available.
        thread._prefill_ring_to(2 * 1920)
        _write(thread, prefill=False)
        assert events[-1]["T"]["silent"] is True
    finally:
        manager.cleanup()
