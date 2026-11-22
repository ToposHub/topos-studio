"""B16 tests exercise project identity, the FX chain and real subprocess PCM."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from threading import Event

import numpy as np
import pytest

from src.features.edit.core import hosted_vst3 as host
from src.features.edit.core.audio_dsp import EffectChain
from src.features.edit.core.audio_playback import AudioPlaybackManager
from src.features.edit.core.fx_versioning import collect_chain_diagnostics
from src.features.edit.core.timeline_v2 import EffectChainParams, TrackEffectInstance


def test_project_plugin_cannot_supply_local_library_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_registry_dir", lambda: tmp_path)
    payload = host.HostedVst3Params("0" * 64, "missing").to_dict()
    payload["path"] = str(tmp_path / "forged.vst3")
    chain = EffectChainParams(effects=(TrackEffectInstance(host.EFFECT_TYPE, payload),))
    restored = EffectChainParams.from_dict(chain.to_dict())
    assert restored.effects[0].params == payload
    diagnostic, = collect_chain_diagnostics(restored)
    assert diagnostic["status"] == "unsupported"
    effect = EffectChain()
    effect.set_params(restored.to_dsp_entries())
    signal = np.ones((2, 32), np.float32)
    with pytest.raises(host.HostedPluginError, match="Scan this exact"):
        effect.process(signal)
    bypassed = replace(restored.effects[0], params={**payload, "enabled": False})
    effect.set_params(EffectChainParams(effects=(bypassed,)).to_dsp_entries())
    assert effect.process(signal) is signal


def test_realtime_plugin_fault_returns_silence_and_instance_diagnostic() -> None:
    instance = TrackEffectInstance(host.EFFECT_TYPE, {"host_version": 21})
    effect = EffectChain(realtime_resources=True)
    effect.set_params(EffectChainParams(effects=(instance,)).to_dsp_entries())
    pcm = np.full((2, 256), 0.1, dtype=np.float32)
    assert np.count_nonzero(effect.process(pcm)) == 0
    taps = effect.tap_levels()
    assert "identity or version" in taps[instance.instance_id]["analysis"]["error"]
    assert np.all(pcm == np.float32(0.1))


def test_movie_preflight_requires_explicit_bypass_for_unavailable_vst3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.features.edit.core.timeline_export import TimelineExportConfig, preflight_audio_export
    from src.features.edit.core.timeline_v2 import TimelineSequence

    monkeypatch.setattr(host, "_registry_dir", lambda: tmp_path / "registered")
    instance = TrackEffectInstance(
        host.EFFECT_TYPE, host.HostedVst3Params("0" * 64, "missing").to_dict(),
    )
    timeline = TimelineSequence(master_effect_chain=EffectChainParams(effects=(instance,)))
    config = TimelineExportConfig(output_path=str(tmp_path / "out.mov"), allow_algorithm_degraded=True)
    result = preflight_audio_export(config, timeline)
    assert not result.ok
    assert any(item.code == "hosted_effect_unavailable" for item in result.blockers)
    bypassed = replace(instance, params={**instance.params, "enabled": False})
    assert preflight_audio_export(config, timeline.with_master_effect_chain(
        EffectChainParams(effects=(bypassed,)),
    )).ok


def test_manager_cleanup_retires_bus_and_master_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeHost:
        def __init__(self) -> None:
            self.closed = Event()

        def close(self) -> None:
            self.closed.set()

    clients = [FakeHost(), FakeHost()]
    pending = iter(clients)
    monkeypatch.setattr(host, "open_plugin", lambda *_args: (next(pending), ()))
    manager = AudioPlaybackManager()
    instance = TrackEffectInstance(
        host.EFFECT_TYPE, host.HostedVst3Params("0" * 64, "fixture").to_dict(),
    )
    chain = EffectChainParams(effects=(instance,))
    manager.register_bus_track("bus")
    manager.set_track_effect_chain("bus", chain)
    manager.set_master_effect_chain(chain)
    for processor in manager._mixer._effect_chains.values():
        processor._effects[0]._future.result(timeout=2)
    manager.cleanup()
    assert all(client.closed.wait(timeout=2) for client in clients)


@pytest.fixture
def real_plugin_path() -> Path:
    value = os.environ.get("TOPOS_TEST_VST3", "")
    if not value or not Path(value).exists() or host.backend_issue():
        pytest.skip("Set TOPOS_TEST_VST3 to a locally installed zero-latency stereo effect")
    return Path(value)


def test_real_plugin_scan_edit_dsp_crash_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, real_plugin_path: Path,
) -> None:
    monkeypatch.setattr(host, "_registry_dir", lambda: tmp_path)
    config, *_ = host.scan_vst3(str(real_plugin_path))
    fields = host.inspect_plugin(config, 48000)
    if fields:
        field = fields[0]
        value = 0.6 if field["value"] < 0.5 else 0.4
        config = replace(config, parameters=((field["id"], value),))
        configured = {item["id"]: item["value"] for item in host.inspect_plugin(config, 48000)}
        assert configured[field["id"]] != field["value"]
    chain = EffectChainParams(effects=(TrackEffectInstance(host.EFFECT_TYPE, config.to_dict()),))
    restored = EffectChainParams.from_dict(chain.to_dict())
    assert collect_chain_diagnostics(restored)[0]["status"] == "supported"
    pcm = np.random.default_rng(22).normal(0.0, 0.03, (2, 480)).astype(np.float32)
    pcm.setflags(write=False)
    first = EffectChain()
    second = EffectChain()
    first.set_params(chain.to_dsp_entries())
    second.set_params(restored.to_dsp_entries())
    checked_processing = False
    try:
        for _ in range(3):
            left = first.process(pcm)
            right = second.process(pcm)
            assert left.shape == pcm.shape and np.isfinite(left).all()
            assert right.shape == pcm.shape and np.isfinite(right).all()
            # Vendor DSP may have its own noise or asynchronous initialization.
            # Validate level agreement and an audible processing path, rather
            # than applying the bit-exact unity oracle to arbitrary plugins.
            assert np.isclose(np.linalg.norm(left), np.linalg.norm(right), rtol=.01)
            checked_processing |= not np.array_equal(left, pcm)
        assert checked_processing
        with pytest.raises(host.HostedPluginError, match="stereo"):
            first.process(np.zeros((6, 480), dtype=np.float32))
    finally:
        first.set_params(None)
        second.set_params(None)
    transport, _ = host.open_plugin(config, 48000)
    try:
        assert transport._process is not None
        transport._process.kill()
        transport._process.wait(timeout=2)
        with pytest.raises(host.HostedPluginError, match="exited"):
            transport.request("process", .2, frames=480, reset=True)
    finally:
        transport.close()
