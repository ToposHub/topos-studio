"""Preserve single-band automation after adding bands to a Dynamic EQ."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_graph_renderer import _build_master_fx_sampler
from src.features.edit.core.audio_retime import animated_fx_sampler, clip_fx_entries
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.timeline_v2 import (
    DynamicEqParams, EditTrack, EffectChainParams, TrackEffectInstance, build_track_fx_sampler,
)
from src.shared.keyframe import Keyframe, KeyframeTrack


@pytest.mark.parametrize("owner", ["track", "bus", "master", "clip"])
@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "existing-multiband"])
def test_single_band_curve_only_reaches_the_migrated_band(owner: str, migrated: bool) -> None:
    first = {
        "frequency_hz": 3000.0, "static_gain_db": -9.0, "dynamic_enabled": False,
        "band_label": "Dialogue",
    }
    if migrated:
        first["legacy_automation"] = True
    second = {"frequency_hz": 1200.0, "static_gain_db": 0.0, "dynamic_enabled": False}
    params = DynamicEqParams(gain_db=-9.0).to_dict() | {"bands": [first, second]}

    if owner == "clip":
        clip_fx = EffectInstance(effect_id="eq", effect_type="dynamic_eq", params=params)
        clip_fx = clip_fx.with_keyframe_added("gain_db", 0, -9.0, min_value=-18.0, max_value=18.0)
        clip_fx = clip_fx.with_keyframe_added("gain_db", 120, 9.0)
        clip_fx = EffectInstance.from_dict(clip_fx.to_dict())
        sampler = animated_fx_sampler([clip_fx])
        assert clip_fx_entries([clip_fx])[0][1]["bands"][0]["static_gain_db"] == -9.0
    else:
        fx = TrackEffectInstance("dynamic_eq", params, instance_id="eq")
        chain = EffectChainParams((TrackEffectInstance.from_dict(fx.to_dict()),))
        curve = KeyframeTrack(param_name="fx/eq/gain_db", min_val=-18.0, max_val=18.0)
        curve.add_keyframe(Keyframe(frame=0, value=-9.0))
        curve.add_keyframe(Keyframe(frame=120, value=9.0))
        automation = {"fx/eq/gain_db": curve}
        if owner == "master":
            sampler = _build_master_fx_sampler(True, "read", automation, chain)
        else:
            track = EditTrack(track_id="audio", name="Audio", track_type="audio",
                              is_bus=owner == "bus", effect_chain=chain,
                              automation_mode="read", automation=automation)
            sampler = build_track_fx_sampler(track)
        assert chain.to_dsp_entries()[0][1]["bands"][0]["static_gain_db"] == -9.0

    assert sampler is not None
    pcm = (0.15 * np.sin(2.0 * np.pi * 3000.0 * np.arange(12000) / 48000.0))[None].astype(np.float32)
    for frame, curve_db in [(120, 9.0), (0, -9.0)]:
        entries = sampler(frame)
        sampled = entries[0][1]
        assert sampled["gain_db"] == curve_db
        expected_gain = curve_db if migrated else -9.0
        assert sampled["bands"][0]["static_gain_db"] == expected_gain
        assert sampled["bands"][0]["band_label"] == "Dialogue"
        assert sampled["bands"][1]["static_gain_db"] == 0.0

        chain_dsp = EffectChain()
        chain_dsp.set_params(entries)
        output = chain_dsp.process(pcm)
        magnitude = np.linalg.norm(output[:, 4096:]) / np.linalg.norm(pcm[:, 4096:])
        assert 20.0 * np.log10(magnitude) == pytest.approx(expected_gain, abs=0.02)

    assert first["static_gain_db"] == -9.0
