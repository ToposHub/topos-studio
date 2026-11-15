"""Numeric FX automation must wake a neutral chain at every graph position."""

from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import SaturationEffect
from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.audio_playback import AudioMixer, AudioTrackState, MASTER_BUS_KEY
from src.features.edit.core.timeline_v2 import (
    EditTrack, EffectChainParams, SaturationParams, TimelineSequence,
    TrackEffectInstance, build_track_fx_sampler,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack


SR = 48000.0
BLOCK = 960


class _Decoder:
    _channels = 2

    def decode_samples_for_frame(self, frame: int, fps: float,
                                num_samples: int) -> np.ndarray:
        return np.full((2, num_samples), 0.2, dtype=np.float32)

    def close(self) -> None:
        pass


def _setup(location: str) -> tuple[TimelineSequence, EditTrack]:
    chain = EffectChainParams(effects=(TrackEffectInstance(
        "saturation", SaturationParams(enabled=True, drive=0.0).to_dict(),
        instance_id="saturation",
    ),))
    curve = KeyframeTrack(param_name="fx/saturation/drive", min_val=0.0, max_val=100.0)
    for frame, value in enumerate((0.0, 70.0, 0.0, 70.0)):
        curve.add_keyframe(Keyframe(frame=frame, value=value,
                                   interpolation=InterpolationType.HOLD))
    animated = EditTrack("B" if location == "bus" else "T", "Animated", "audio", 0,
                         is_bus=location == "bus", effect_chain=chain,
                         automation={"fx/saturation/drive": curve}, automation_mode="read")
    if location == "master":
        return TimelineSequence(
            audio_tracks=(EditTrack("T", "Source", "audio", 0),),
            master_effect_chain=chain, master_automation=animated.automation,
            master_automation_mode="read"), animated
    if location == "bus":
        return TimelineSequence(audio_tracks=(
            EditTrack("T", "Source", "audio", 0, output_bus_id="B"), animated)), animated
    return TimelineSequence(audio_tracks=(animated,)), animated


def _expected(frame: int) -> np.ndarray:
    effect = SaturationEffect(SR)
    effect.set_params(SaturationParams(enabled=True, drive=70.0 if frame % 2 else 0.0).to_dict())
    return effect.process(np.full((2, BLOCK), 0.2, dtype=np.float32))


@pytest.mark.parametrize("location", ["track", "bus", "master"])
def test_playback_wakes_and_suspends_numeric_automation(location: str) -> None:
    _sequence, animated = _setup(location)
    mixer = AudioMixer()
    mixer.set_sample_rate(SR)
    mixer.add_track("T", _Decoder(), AudioTrackState(track_id="T"))
    sampler = build_track_fx_sampler(animated)
    if location == "master":
        mixer.set_master_effect_chain(animated.effect_chain, animated_sampler=sampler)
        target = MASTER_BUS_KEY
    else:
        target = animated.track_id
        if location == "bus":
            mixer.register_bus_track("B")
            mixer.set_track_routing("B", None, [], is_bus=True)
            mixer.set_track_routing("T", "B", [])
        mixer.set_track_effect_chain(target, animated.effect_chain, animated_sampler=sampler)
    for frame in range(4):
        actual, _levels = mixer.mix_samples(frame, 50.0, BLOCK)
        np.testing.assert_allclose(actual, _expected(frame), atol=1e-6, rtol=0.0)
        if frame > 0:
            assert mixer._effect_chains[target].tap_levels()["saturation"]["bypassed"] is (frame % 2 == 0)


@pytest.mark.parametrize("location", ["track", "bus", "master"])
def test_renderer_wakes_and_suspends_numeric_automation(location: str) -> None:
    sequence, _animated = _setup(location)
    renderer = AudioGraphRenderer(SR)
    renderer.configure_from_sequence(sequence)
    for frame in range(4):
        input_block = np.full((2, BLOCK), 0.2, dtype=np.float32)
        if location == "master":
            actual = renderer.apply_master(input_block, start_frame=frame, fps=50.0)
            chain = renderer._master_chain
        elif location == "bus":
            actual = np.zeros_like(input_block)
            renderer.render_bus_stage({"B": input_block}, actual, has_solo=False,
                                       start_frame=frame, fps=50.0)
            chain = renderer.get_node("B").effect_chain
        else:
            actual = renderer.render_track_block("T", input_block, frame, fps=50.0)
            chain = renderer.get_node("T").effect_chain
        np.testing.assert_allclose(actual, _expected(frame), atol=1e-6, rtol=0.0)
        if frame > 0:
            assert chain.tap_levels()["saturation"]["bypassed"] is (frame % 2 == 0)
