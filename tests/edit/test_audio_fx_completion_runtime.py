"""Runtime regressions found while auditing the November FX delivery."""
from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest

from src.features.edit.core.audio_dsp import ALGORITHM_VERSION, EffectChain
from src.features.edit.core.audio_effects import convolution_reverb as ir_module
from src.features.edit.core.audio_effects.convolution_reverb import ConvolutionReverbEffect, load_ir
from src.features.edit.core.audio_effects.spectral_mangler import SpectralManglerEffect
from src.features.edit.core.audio_retime import clip_fx_entries
from src.features.edit.core.delivery_dither import DeliveryQuantizer, DitherSettings
from src.features.edit.core.effect_instance import EffectInstance
from src.features.edit.core.stem_export import write_stem_wav
from src.features.edit.core.timeline_export import TimelineExportConfig, TimelineExporter


def _wav(path: Path, width: int = 2, rate: int = 48000) -> None:
    values = np.array([0, 4194304, -4194304, 2097152], dtype=np.int32)
    if width == 3:
        packed = np.stack([values & 255, (values >> 8) & 255, (values >> 16) & 255], axis=-1)
        raw = packed.astype(np.uint8).tobytes()
    else:
        raw = (values >> 8).astype('<i2').tobytes()
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, width, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(raw)


def _wait_ready(effect: ConvolutionReverbEffect) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if effect.is_active:
            return
        time.sleep(0.005)
    raise AssertionError(effect.collect_telemetry())


def test_ir_24bit_and_short_ir_resampling(tmp_path: Path) -> None:
    path = tmp_path / 'short.wav'
    _wav(path, width=3, rate=24000)
    decoded, resource = load_ir(str(path), 48000, edge_fade_ms=0)
    assert decoded.shape == (1, 8)
    assert resource.samples == 8 and resource.sample_rate == 48000
    assert np.isfinite(decoded).all() and decoded.max() > 0.4


def test_ir_preparation_never_reads_on_caller_and_clearing_discards_late_result(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'async.wav'
    _wav(path)
    started, finish = threading.Event(), threading.Event()
    caller = threading.get_ident()
    original = ir_module.load_ir

    def slow_load(*args: object, **kwargs: object) -> tuple:
        assert threading.get_ident() != caller
        started.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(ir_module, 'load_ir', slow_load)
    effect = ConvolutionReverbEffect()
    effect.enable_async_resources()
    effect.set_params({'ir_path': str(path)}, algorithm_version=5)
    assert started.wait(3)
    effect.set_params({'ir_path': ''}, algorithm_version=5)
    finish.set()
    assert not effect.is_active
    assert effect.tail_estimate_samples() == 0
    assert effect.collect_telemetry()['resource_status'] == 'empty'
    np.testing.assert_array_equal(effect.process(np.ones((2, 17), np.float32)), 1)


def test_ir_realtime_chain_activates_prepared_kernel(tmp_path: Path) -> None:
    path = tmp_path / 'chain.wav'
    _wav(path)
    chain = EffectChain(realtime_resources=True)
    chain.set_params([('convolution_reverb', {'ir_path': str(path), 'wet': 1., 'dry': 0.}, True, 'ir-a', 5)])
    effect = chain._effects[0]._impl
    _wait_ready(effect)
    x = np.zeros((2, 32), np.float32)
    x[:, 0] = 1
    out = chain.process(x)
    assert np.max(np.abs(out)) > 0.4
    assert chain.tap_levels()['ir-a']['analysis']['resource_status'] == 'ready'
    chain.set_params([])
    assert effect._prepare_future is None


def test_ir_failure_is_diagnostic_not_stale_kernel(tmp_path: Path) -> None:
    path = tmp_path / 'broken.wav'
    path.write_bytes(b'broken')
    effect = ConvolutionReverbEffect()
    effect.enable_async_resources()
    effect.set_params({'ir_path': str(path)}, algorithm_version=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        effect.is_active
        if effect.collect_telemetry()['resource_status'] == 'failed':
            break
        time.sleep(.005)
    assert effect.collect_telemetry()['resource_status'] == 'failed'
    assert effect.collect_telemetry()['resource_error']
    assert effect.tail_estimate_samples() == 0


@pytest.mark.parametrize('depth', [16, 24])
def test_dither_stream_is_chunk_invariant_and_channels_independent(depth: int) -> None:
    settings = DitherSettings(enabled=True, bit_depth=depth, seed=71, dither_silence=True)
    x = np.full((2, 4001), 0.1234567, np.float32)
    whole = DeliveryQuantizer(settings, depth).apply(x)
    quantizer = DeliveryQuantizer(settings, depth)
    chunked = np.concatenate([quantizer.apply(x[:, i:i+127]) for i in range(0, 4001, 127)], axis=-1)
    np.testing.assert_array_equal(whole, chunked)
    assert not np.array_equal(whole[0], whole[1])
    assert not np.array_equal(whole[:, :127], whole[:, 127:254])


def test_dither_rejects_depth_mismatch_and_float_ignores_it() -> None:
    settings = DitherSettings(enabled=True, bit_depth=16, seed=7)
    with pytest.raises(ValueError, match='bit depth'):
        DeliveryQuantizer(settings, 24)
    x = np.array([[1.25, -1.5]], np.float32)
    np.testing.assert_array_equal(DeliveryQuantizer(settings, 32).apply(x), x)


@pytest.mark.parametrize('depth', [16, 24, 32])
def test_real_wav_delivery_roundtrip(tmp_path: Path, depth: int) -> None:
    import av
    path = tmp_path / f'output-{depth}.wav'
    x = np.random.default_rng(99).uniform(-.9, .9, (2, 4001)).astype(np.float32)
    if depth == 32:
        x *= 2
    settings = DitherSettings(enabled=True, bit_depth=depth, seed=77)
    assert write_stem_wav(str(path), x, bit_depth=depth, dither=settings)
    with av.open(str(path)) as container:
        resampler = av.AudioResampler(format='fltp', layout='stereo', rate=48000)
        frames = [f.to_ndarray() for decoded in container.decode(audio=0) for f in resampler.resample(decoded)]
    decoded = np.concatenate(frames, axis=-1)
    expected = DeliveryQuantizer(settings, depth).apply(x)
    np.testing.assert_allclose(decoded, expected, rtol=0, atol=0 if depth == 32 else 2. ** -(depth-1))
    assert decoded.shape == x.shape


def test_timeline_final_quantization_matches_stem_and_bypasses_float() -> None:
    x = np.random.default_rng(3).normal(0, .2, (2, 501)).astype(np.float32)
    settings = DitherSettings(enabled=True, bit_depth=24, seed=3)
    config = TimelineExportConfig(audio_codec='pcm_s24le', audio_dither=settings.to_dict())
    expected = DeliveryQuantizer(settings, 24).apply(x)
    np.testing.assert_array_equal(TimelineExporter._quantize_audio_delivery(x, config), expected)
    np.testing.assert_array_equal(TimelineExporter._quantize_audio_delivery(x, config, 'pcm_f32le'), x)


def test_spectral_peak_meter_updates_with_limiter_off_and_reset() -> None:
    effect = SpectralManglerEffect()
    effect.set_params({'enabled': True, 'output_limiter_enabled': False, 'output_gain_db': 6.})
    effect.process(np.full((2, 400), .8, np.float32))
    assert effect.collect_telemetry()['clip']
    assert effect.collect_telemetry()['pre_limiter_peak'] > 1
    effect.process(np.full((2, 400), .1, np.float32))
    assert not effect.collect_telemetry()['clip']
    assert effect.collect_telemetry()['output_peak'] < .3
    effect.reset_state()
    assert effect.collect_telemetry()['output_peak'] == 0


def test_clip_identity_version_survive_mutation_reopen_and_animation() -> None:
    current = EffectInstance(effect_type='phaser', params={'rate_hz': .5})
    assert current.algorithm_version == ALGORITHM_VERSION == 5
    old = EffectInstance.from_dict({'effect_type': 'phaser', 'effect_id': 'old', 'params': {}})
    assert old.algorithm_version == 0
    updated = old.with_param('rate_hz', 1).with_enabled(False).with_order(1).with_effect_id('copied')
    assert updated.algorithm_version == 0
    assert EffectInstance.from_dict(updated.to_dict()).algorithm_version == 0
    entries = clip_fx_entries([current, updated])
    assert entries[0][3:] == (current.effect_id, 5)
    assert entries[1][3:] == ('copied', 0)


def test_instance_spectra_are_real_separate_and_reset() -> None:
    chain = EffectChain()
    chain.set_params([('saturation', {'drive': 1.}, True, 'one', 5),
                      ('saturation', {'drive': 80.}, True, 'two', 5)])
    chain.set_spectrum_metering(True)
    x = np.stack([np.sin(2 * np.pi * np.arange(4096) * 1000 / 48000)] * 2).astype(np.float32) * .2
    chain.process(x)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        levels = chain.tap_levels()
        if all(levels[iid].get('spectrum') for iid in ('one', 'two')):
            break
        time.sleep(.005)
    assert len(levels['one']['spectrum']['freqs']) == 96
    assert levels['one']['spectrum']['output_db'] != levels['two']['spectrum']['output_db']
    chain.reset_state()
    assert not chain.tap_levels()['one']['spectrum']
    chain.set_spectrum_metering(False)
    assert 'spectrum' not in chain.tap_levels()['one']


@pytest.mark.parametrize('kind,params', [
    ('dynamic_eq', {'bands': [{'frequency_hz': 1000., 'threshold_dbfs': -40., 'max_cut_db': 12.}]}),
    ('transient_designer', {'attack_pct': 100., 'sustain_pct': -50.}),
])
def test_external_detectors_reach_actual_chain(kind: str, params: dict) -> None:
    params = params | {'sidechain_source_id': 'source'}
    chain = EffectChain()
    chain.set_params([(kind, params, True, 'fx', 5)])
    assert chain.sidechain_sources() == {'source'}
    n = 6001
    tone = np.sin(2*np.pi*np.arange(n)*1000/48000).astype(np.float32)[None, :]
    x = np.concatenate([tone, -tone]) * .1
    sc = np.concatenate([tone, -tone]) * .8
    result = chain.process(x, sidechain_by_id={'fx': sc})
    assert result.shape == x.shape and np.isfinite(result).all()
    assert chain.tap_levels()['fx']['analysis']['sidechain_status'] == 'external'
    chain.process(x)
    assert chain.tap_levels()['fx']['analysis']['sidechain_status'] == 'missing'


def test_tremolo_manual_sync_duty_smoothing_and_waveform_switch() -> None:
    from src.features.edit.core.audio_effects.modulation_tools import TremoloAutoPanEffect
    from src.features.edit.core.timeline_v2 import TremoloAutoPanParams
    params = {'waveform': 'square', 'sync': True, 'bpm': 120., 'sync_div': '1/4',
              'depth': 1., 'duty_cycle_pct': 25., 'smooth_ms': 0.}
    restored = TremoloAutoPanParams.from_dict(params).to_dict()
    assert restored['duty_cycle_pct'] == 25.
    fx = TremoloAutoPanEffect()
    fx.set_params(restored)
    x = np.ones((2, 48000), np.float32)
    out = fx.process(x)
    assert np.count_nonzero(out[0] > .5) == 12000  # two cycles, 25% duty
    fx.set_params({'waveform': 'sine'})
    fx.process(x[:, :6000])
    phase = fx.collect_telemetry()['phase_cycles']
    fx.set_params({'waveform': 'triangle'})
    fx.process(x[:, :1])
    assert fx.collect_telemetry()['phase_cycles'] == pytest.approx((phase + 2/48000) % 1)
    fx.set_params(params | {'smooth_ms': 5.})
    fx.reset_state()
    smooth = fx.process(x)
    assert np.max(np.abs(np.diff(smooth[0]))) < .01
    fx.set_params({}, sample_rate=96000)
    assert fx.collect_telemetry()['phase_cycles'] == 0


def test_multiband_saturation_repeated_parameters_keep_fir_and_crossover_history() -> None:
    from src.features.edit.core.audio_effects.saturation import MultibandSaturationEffect
    params = {'mb_oversampling': 2, 'mb_bands': [{'enabled': True, 'model': 'tape', 'drive_db': 9.}] * 3}
    x = np.random.default_rng(2).normal(0, .1, (2, 1001)).astype(np.float32)
    whole = MultibandSaturationEffect()
    whole.set_params(params)
    expected = whole.process(x)
    stream = MultibandSaturationEffect()
    output = []
    for i in range(0, 1001, 127):
        stream.set_params(params)
        output.append(stream.process(x[:, i:i+127]))
    np.testing.assert_allclose(np.concatenate(output, axis=-1), expected, rtol=0, atol=1e-6)


def test_modulation_zero_feedback_still_reports_first_echo_tail() -> None:
    from src.features.edit.core.audio_effects.modulation_tools import ChorusFlangerEffect
    effect = ChorusFlangerEffect()
    effect.set_params({'base_ms': 5., 'depth_ms': 0., 'mix': 1., 'voices': 1, 'feedback': 0.})
    pulse = np.ones((2, 1), np.float32)
    first = effect.process(pulse)
    budget = effect.tail_estimate_samples()
    tail = effect.process(np.zeros((2, budget), np.float32))
    full = np.concatenate([first, tail], axis=-1)
    assert np.argmax(np.abs(full[0])) == 240
    assert budget >= 240


def test_freeze_rate_conversion_preserves_chronological_ring_content() -> None:
    from src.features.edit.core.audio_effects.spectral_mangler import _GrainModule
    source = _GrainModule(8000)
    anchor = source._cap + 113
    chronological = np.linspace(-.8, .8, source._cap)
    history = np.zeros((2, source._cap), dtype=np.float64)
    positions = np.arange(anchor-source._cap, anchor) % source._cap
    history[:, positions] = chronological
    target = _GrainModule(16000)
    assert target.install_capture({'history': history, 'anchor': anchor, 'sample_rate': 8000., 'seed': 3})
    restored = target._hist[0, np.arange(target._written-target._cap, target._written) % target._cap]
    # Mid-window slope is preserved; only the resampler's end transients differ.
    assert np.max(np.abs(restored[64:-64] - np.linspace(-.8, .8, target._cap)[64:-64])) < .002
    assert not target.install_capture({'history': history, 'anchor': -1})
