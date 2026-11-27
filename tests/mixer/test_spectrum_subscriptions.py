"""Mixer FFT subscriptions follow the visible analyzers."""
from __future__ import annotations

import numpy as np
from PyQt6.QtCore import Qt
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState, MASTER_BUS_KEY
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance
from src.shared.ui.audio_mixer.audio_mixer_panel import AudioMixerPanel


class _Tone:
    _channels = 2

    def decode_samples_for_frame(self, frame: int, fps: float, count: int) -> np.ndarray:
        time = (frame * 48000.0 / fps + np.arange(count)) / 48000.0
        tone = (0.1 * np.sin(2.0 * np.pi * 1000.0 * time)).astype(np.float32)
        return np.stack((tone, tone))


def _eq_chain() -> EffectChainParams:
    return EffectChainParams((
        TrackEffectInstance("strip_eq", {"master_gain": -2.0}, instance_id="first"),
        TrackEffectInstance("strip_eq", {"master_gain": -4.0}, instance_id="second", order=1),
    ))


def _mixer() -> AudioMixer:
    mixer = AudioMixer()
    for owner in ("A", "B", "C"):
        mixer.add_track(owner, _Tone(), AudioTrackState(track_id=owner))
        mixer.set_track_effect_chain(owner, _eq_chain())
    mixer.set_track_state("Bus", AudioTrackState(track_id="Bus"))
    mixer.set_track_routing("Bus", None, [], is_bus=True)
    mixer.set_track_routing("C", "Bus", [])
    mixer.set_track_effect_chain("Bus", _eq_chain())
    mixer.set_master_effect_chain(_eq_chain())
    return mixer


def test_playback_analyzes_only_subscribed_channels_and_instances() -> None:
    mixer = _mixer()
    control = _mixer()
    mixer.set_spectrum_targets({
        "A": frozenset({"first"}),
        "B": frozenset({""}),
        MASTER_BUS_KEY: frozenset({"second"}),
    })
    output, levels = mixer.mix_samples(0, 25.0, 1920)
    np.testing.assert_array_equal(output, control.mix_samples(0, 25.0, 1920)[0])
    assert mixer.collect_spectra().keys() == {"B"}

    def spectrum_ids(owner: str) -> set[str]:
        return {iid for iid, tap in levels[owner].taps.items() if "spectrum" in tap}

    assert spectrum_ids("A") == {"first"}
    assert spectrum_ids("B") == spectrum_ids("C") == set()
    assert spectrum_ids(MASTER_BUS_KEY) == {"second"}

    mixer.set_spectrum_targets({"Bus": frozenset({"second"})})
    assert not mixer.collect_spectra()
    mixer.set_sample_rate(44100.0)
    _, levels = mixer.mix_samples(1, 25.0, 1764)
    assert spectrum_ids("Bus") == {"second"}
    assert spectrum_ids("A") == spectrum_ids(MASTER_BUS_KEY) == set()

    mixer.set_spectrum_metering(True)
    _, levels = mixer.mix_samples(2, 25.0, 1764)
    assert mixer.collect_spectra().keys() == {"A", "B", "C", "Bus", MASTER_BUS_KEY}
    assert spectrum_ids("C") == {"first", "second"}
    mixer.set_spectrum_metering(False)
    _, levels = mixer.mix_samples(3, 25.0, 1764)
    assert not mixer.collect_spectra()
    assert not spectrum_ids("C")


def test_window_changes_publish_scope_even_while_metering_stays_on(qtbot: QtBot) -> None:
    panel = AudioMixerPanel()
    qtbot.addWidget(panel)
    panel.register_track("A", "Track A", "A1")
    strip = panel._track_mixers["A"]
    chain = _eq_chain()
    strip.set_effect_chain(chain)
    events: list[dict[str, frozenset[str]]] = []
    enabled: list[bool] = []
    panel.spectrum_targets_changed.connect(events.append)
    panel.spectrum_metering_changed.connect(enabled.append)

    for effect in chain.effects:
        strip._open_insert_effect(effect)
    graph = strip._strip_eq_dialogs["second"].eq_graph
    qtbot.waitUntil(lambda: not graph._btn_spec_rect.isEmpty())
    spec_position = graph._btn_spec_rect.center().toPoint()
    qtbot.mouseClick(graph, Qt.MouseButton.LeftButton, pos=spec_position)
    qtbot.mouseClick(graph, Qt.MouseButton.LeftButton, pos=spec_position)
    strip._strip_eq_dialogs["second"].showMinimized()
    qtbot.waitUntil(lambda: panel.spectrum_targets() == {"A": frozenset({"first"})})
    strip._open_insert_effect(chain.effects[1])
    qtbot.waitUntil(lambda: panel.spectrum_targets() == {"A": frozenset({"first", "second"})})
    strip._strip_eq_dialogs["first"].close()
    panel.deregister_track("A")

    assert events == [
        {"A": frozenset({"first"})},
        {"A": frozenset({"first", "second"})},
        {"A": frozenset({"first"})},
        {"A": frozenset({"first", "second"})},
        {"A": frozenset({"first"})},
        {"A": frozenset({"first", "second"})},
        {"A": frozenset({"second"})},
        {},
    ]
    assert enabled == [True, False]
    assert panel.spectrum_targets() == {}
