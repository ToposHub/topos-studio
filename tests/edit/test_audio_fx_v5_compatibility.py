"""Pre-v5 audio remains reproducible; corrected DSP requires an explicit upgrade."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.fx_versioning import (
    algorithm_compat_bucket,
    collect_chain_diagnostics,
    upgrade_chain_algorithm_version,
)
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance


REFERENCE = json.loads((Path(__file__).parents[1] / "fixtures" /
                        "audio_fx_pre_v5_pcm.json").read_text())


@pytest.mark.parametrize("effect_type", sorted(REFERENCE["effects"]))
@pytest.mark.parametrize("version", [0, 1, 4])
def test_old_version_matches_pcm_from_historical_commit(
    effect_type: str, version: int,
) -> None:
    fixture = REFERENCE["effects"][effect_type]
    pcm = np.random.default_rng(REFERENCE["seed"]).normal(
        0, 0.7, (2, REFERENCE["samples"])).astype(np.float32)
    chain = EffectChain(REFERENCE["sample_rate"])
    chain.set_params([(effect_type, fixture["params"], True, "old", version)])
    block = REFERENCE["block_size"]
    output = np.concatenate([
        chain.process(pcm[:, pos:pos + block])
        for pos in range(0, pcm.shape[-1], block)
    ], axis=-1)
    # Numerical references were generated from git show <source>, never from
    # the new compatibility wrappers (the provenance lives in the fixture).
    np.testing.assert_allclose(output[:, REFERENCE["indices"]],
                               fixture["expected"], rtol=0, atol=1e-7)
    assert ".legacy_v4." in type(chain._effects[0]._impl).__module__


@pytest.mark.parametrize("effect_type", sorted(REFERENCE["effects"]))
def test_current_version_selects_corrected_kernel(effect_type: str) -> None:
    chain = EffectChain(48000)
    chain.set_params([(effect_type, {}, True, "new", ALGORITHM_VERSION)])
    assert ".legacy_v4." not in type(chain._effects[0]._impl).__module__
    assert algorithm_compat_bucket(ALGORITHM_VERSION, effect_type) == "current"


@pytest.mark.parametrize("effect_type", sorted(REFERENCE["effects"]))
def test_explicit_upgrade_preserves_original_instance_and_records_new_version(
    effect_type: str,
) -> None:
    old = TrackEffectInstance.from_dict({
        "effect_type": effect_type, "params": {}, "algorithm_version": 4,
    })
    chain = EffectChainParams(effects=(old,))
    upgraded = upgrade_chain_algorithm_version(chain)
    assert chain.effects[0].algorithm_version == 4
    assert upgraded.effects[0].algorithm_version == ALGORITHM_VERSION
    assert upgraded.effects[0].instance_id == old.instance_id
    assert "v5" in collect_chain_diagnostics(chain)[0]["reason"]
    assert collect_chain_diagnostics(upgraded)[0]["compat_bucket"] == "current"


@pytest.mark.parametrize("linked", [True, False])
def test_historical_limiter_accepts_final_short_blocks(linked: bool) -> None:
    chain = EffectChain(48000)
    chain.set_params([("true_peak_limiter", {
        "lookahead_ms": 5.0, "linked": linked,
    }, True, "old", 4)])
    for size in (513, 127, 1, 32):
        output = chain.process(np.ones((2, size), dtype=np.float32))
        assert output.shape == (2, size)
        assert np.isfinite(output).all()


def test_adapter_without_explicit_version_keeps_current_direct_dsp_contract() -> None:
    from src.features.edit.core.audio_effects.adapters import TruePeakLimiterAdapter

    adapter = TruePeakLimiterAdapter(48000)
    adapter.set_params({})
    assert ".legacy_v4." not in type(adapter._impl).__module__


def test_new_track_instance_uses_current_version_old_dict_stays_unstamped() -> None:
    assert TrackEffectInstance("phaser", {}).algorithm_version == ALGORITHM_VERSION
    assert TrackEffectInstance.from_dict({
        "effect_type": "phaser", "params": {},
    }).algorithm_version == 0
