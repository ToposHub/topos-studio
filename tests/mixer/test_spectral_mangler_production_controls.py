"""Check production Spectral Mangler control and stream behavior."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PyQt6.QtCore import QPoint, Qt
from pytestqt.qtbot import QtBot

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.audio_effects.spectral_mangler import SpectralManglerEffect
from src.features.edit.core.audio_playback import AudioMixer
from src.features.edit.core.freeze_capture_store import FreezeCaptureStore
from src.features.mixer.effects.production_dialog import ProductionEffectDialog


def _signal(channels: int = 2, length: int = 16000) -> np.ndarray:
    return np.random.default_rng(72).uniform(-0.4, 0.4, (channels, length)).astype(np.float32)


@pytest.mark.parametrize("channels", [1, 2, 6])
def test_processor_keeps_mono_stereo_and_multichannel_layouts(channels: int) -> None:
    effect = SpectralManglerEffect()
    effect.set_params({"ring_enabled": True, "grain_enabled": True, "dist_enabled": True,
                       "routing": "parallel"})
    pcm = _signal(channels)
    original = pcm.copy()
    output = effect.process(pcm)
    assert output.shape == pcm.shape
    assert np.isfinite(output).all()
    assert np.max(np.abs(output - pcm)) > 0.01
    np.testing.assert_array_equal(pcm, original)


def test_parallel_residual_shares_distortion_delay_on_current_processor() -> None:
    pcm = _signal()
    values = {"ring_enabled": True, "ring_mix_pct": 100.0, "global_mix_pct": 100.0}
    normal = SpectralManglerEffect()
    normal.set_params(values)
    dry_and_ring = pcm * 0.5 + normal.process(pcm) * 0.5
    parallel = SpectralManglerEffect()
    parallel.set_params(values | {"dist_enabled": True, "dist_mix_pct": 0.0,
                                  "routing": "parallel"}, algorithm_version=ALGORITHM_VERSION)
    latency = parallel.latency_samples()
    assert latency > 30
    output = parallel.process(pcm)
    np.testing.assert_array_equal(output[:, :latency], 0.0)
    np.testing.assert_allclose(output[:, latency:], dry_and_ring[:, :-latency], rtol=0, atol=7e-8)


def test_bitcrush_reports_delay_without_an_unused_oversampling_stage() -> None:
    params = {"dist_enabled": True, "dist_curve": "bitcrush", "global_mix_pct": 0.0}
    current = SpectralManglerEffect()
    legacy = SpectralManglerEffect()
    current.set_params(params, algorithm_version=ALGORITHM_VERSION)
    legacy.set_params(params, algorithm_version=4)
    assert legacy.latency_samples() - current.latency_samples() == 31
    impulse = np.zeros((1, 512), dtype=np.float32)
    impulse[0, 0] = 1.0
    assert np.argmax(current.process(impulse)) == current.latency_samples()


def test_frozen_clip_asset_replays_after_seek_at_different_block_sizes(tmp_path: Path) -> None:
    values = {"grain_enabled": True, "grain_density_hz": 35.0, "grain_size_ms": 73.0,
              "grain_mix_pct": 100.0, "random_seed": 439}
    source = SpectralManglerEffect()
    source.set_params(values)
    source.process(_signal())
    capture = source.take_freeze_capture()
    store = FreezeCaptureStore(str(tmp_path))
    assert store.save(capture)
    capture_id = capture["id"]
    loaded = store.load(capture_id)
    assert loaded is not None
    mixer = AudioMixer()
    mixer.set_freeze_registry({capture_id: loaded})
    enabled = values | {"grain_freeze": True, "freeze_capture_id": capture_id}
    mixer.set_clip_effects("clip", [("spectral_mangler", enabled, True, "fx", ALGORITHM_VERSION)])
    chain = mixer._clip_effect_chains["clip"]
    silence = np.zeros_like(_signal())
    first = chain.process(silence)
    assert np.max(np.abs(first)) > 0.001
    assert chain.tap_levels()["fx"]["analysis"]["capture_status"] == "installed"
    chain.reset_state()
    second = np.concatenate([chain.process(silence[:, n:n + 347])
                             for n in range(0, silence.shape[-1], 347)], axis=-1)
    np.testing.assert_array_equal(second, first)


def test_freeze_before_playback_fills_one_deterministic_history_span() -> None:
    params = {"grain_enabled": True, "grain_freeze": True, "grain_mix_pct": 100.0}
    pcm = _signal()
    whole = SpectralManglerEffect()
    split = SpectralManglerEffect()
    whole.set_params(params)
    split.set_params(params)
    expected = whole.process(pcm)
    output = np.concatenate([split.process(pcm[:, n:n + 93])
                             for n in range(0, pcm.shape[-1], 93)], axis=-1)
    assert split.collect_telemetry()["frozen"]
    assert whole.take_freeze_capture()["anchor"] == split.take_freeze_capture()["anchor"]
    assert np.max(np.abs(expected)) > 0.001
    np.testing.assert_array_equal(output, expected)


def test_spectral_waveform_monitor_only_runs_when_subscribed() -> None:
    chain = EffectChain(realtime_resources=True)
    chain.set_params([("spectral_mangler", {"ring_enabled": True}, True, "fx", ALGORITHM_VERSION)])
    pcm = _signal(length=512)
    chain.process(pcm)
    assert "waveform" not in chain.tap_levels()["fx"]["analysis"]
    chain.set_spectrum_instances(frozenset({"fx"}))
    out = chain.process(pcm)
    tap = chain.tap_levels()["fx"]
    assert "spectrum" not in tap
    slots = np.linspace(0, 511, 128, dtype=np.intp)
    np.testing.assert_array_equal(tap["analysis"]["waveform"], out[0, slots])
    chain.set_spectrum_instances(frozenset())
    chain.process(pcm)
    assert "waveform" not in chain.tap_levels()["fx"]["analysis"]


def test_spectral_panel_updates_limiter_banks_and_clears_discarded_capture(qtbot: QtBot) -> None:
    dialog = ProductionEffectDialog("spectral_mangler", "Track", {
        "grain_enabled": True, "grain_freeze": True, "freeze_capture_id": "fz-captured",
    })
    qtbot.addWidget(dialog)
    dialog.show()
    panel = dialog.panel
    assert not panel._demo_timer.isActive()
    assert panel.visualizer._processor_controls
    assert panel.visualizer._external_wave is None
    panel.grain_card.freeze_btn.click()
    assert dialog.current_params()["freeze_capture_id"] == ""
    assert not dialog.current_params()["grain_freeze"]
    qtbot.mouseClick(panel.master_strip, Qt.MouseButton.LeftButton, pos=QPoint(83, 10))
    assert dialog.current_params()["output_limiter_enabled"]
    panel.btn_b.click()
    assert not panel.master_strip._limiter
    panel.btn_a.click()
    assert panel.master_strip._limiter
    panel.update_telemetry(-12.0, -2.0, wave_samples=[0.0, 0.3, -0.2],
                           grains=[{"x": 0.4, "y": 0.42, "age": 0.2}], clipped=True)
    assert panel.master_strip._clipped
    assert panel.visualizer._external_wave == [0.0, 0.3, -0.2]
    assert not panel.visualizer.grab().isNull()
