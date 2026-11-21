"""Selected offline processors preserve clip coordinates, channels and cancellation."""
from __future__ import annotations

import numpy as np
import pytest

from src.features.edit.core.audio_edit import (
    make_step, new_audio_edit, render_audio_edit_full, validate_audio_edit,
)
from src.features.edit.core.audio_effects.doubler import DoublerVoice, render_doubler
from src.features.edit.core.audio_effects.pitch_correction import (
    render_pitch_correction, track_f0,
)
from src.features.edit.core.de_click_dehum import (
    ClickCandidate, _interpolate_repair, detect_hum_base, remove_clicks, remove_hum,
)

RATE = 8000


def tone(hz: float = 443.0, channels: int = 2) -> np.ndarray:
    signal = (0.2 * np.sin(2 * np.pi * hz * np.arange(RATE * 2) / RATE)).astype(np.float32)
    return np.stack([signal] * channels)


def test_interpolation_uses_absolute_context_and_merges_overlaps() -> None:
    clean = np.linspace(-0.15, 0.15, 100, dtype=np.float32)[None]
    dirty = clean.copy()
    dirty[:, 43:55] += 0.8
    direct = _interpolate_repair(dirty, 43, 55)
    np.testing.assert_allclose(direct, clean, atol=1e-8)
    repaired, handled = remove_clicks(
        dirty, RATE, candidates=[ClickCandidate(47, 4, 1.0), ClickCandidate(51, 4, 1.0)])
    assert len(handled) == 2
    np.testing.assert_allclose(repaired, clean, atol=1e-8)
    np.testing.assert_array_equal(dirty[:, :43], repaired[:, :43])


def test_no_bilateral_repair_context_preserves_input() -> None:
    pcm = tone()[:, :32]
    out, handled = remove_clicks(pcm, RATE, candidates=[ClickCandidate(0, 4, 1.0)])
    assert not handled
    np.testing.assert_array_equal(out, pcm)


@pytest.mark.parametrize("processor,options", [
    ("de_hum", {"hum_base_mode": "50"}),
    ("de_click", {}),
    ("pitch_correction", {"scale_notes": [9], "retune_ms": 0.0}),
    ("doubler", {"voices": [{"interval_st": 12.0}]}),
])
def test_stack_respects_selected_range_channels_and_input(processor: str, options: dict) -> None:
    pcm = tone()
    pcm[0] += (0.08 * np.sin(2 * np.pi * 50 * np.arange(pcm.shape[-1]) / RATE)).astype(np.float32)
    pcm[0, 6000:6003] += 0.8
    before = pcm.copy()
    edit = new_audio_edit([make_step(processor, 24000, 72000, [0], **options)])
    assert not validate_audio_edit(edit, clip_output_samples=96000, channel_count=2)
    out = render_audio_edit_full(pcm, edit, sample_rate=RATE)
    assert out.shape == pcm.shape
    np.testing.assert_array_equal(out[1], before[1])
    np.testing.assert_array_equal(out[0, :4000], before[0, :4000])
    np.testing.assert_array_equal(out[0, 12000:], before[0, 12000:])
    np.testing.assert_array_equal(pcm, before)
    assert np.max(np.abs(out[0, 4000:12000] - pcm[0, 4000:12000])) > 1e-4


def test_scale_selection_reaches_renderer() -> None:
    pcm = tone(443.0, 1)
    edit = new_audio_edit([make_step("pitch_correction", 0, 96000, [0],
                                    scale_notes=[0], retune_ms=0.0)])
    out = render_audio_edit_full(pcm, edit, sample_rate=RATE)
    steady = out[0, RATE // 2:RATE + RATE // 2]
    frequencies = np.fft.rfftfreq(steady.size, 1.0 / RATE)
    peak = frequencies[np.abs(np.fft.rfft(steady * np.hanning(steady.size))).argmax()]
    assert peak == pytest.approx(523.25, abs=2.0)


def test_tracking_and_hum_are_safe_for_opposite_polarities() -> None:
    pcm = tone()
    pcm[1] *= -1.0
    assert any(frame.voiced for frame in track_f0(pcm, RATE))
    hum = tone(50.0)
    hum[1] *= -1.0
    assert detect_hum_base(hum, RATE) == 50.0


def test_auto_hum_does_not_invent_frequency() -> None:
    rng = np.random.default_rng(80)
    pcm = rng.normal(0.0, 0.02, (2, RATE * 2)).astype(np.float32)
    assert detect_hum_base(pcm, RATE) is None
    np.testing.assert_array_equal(remove_hum(pcm, RATE), pcm)


def test_doubler_preserves_right_source_multichannel_and_mono_gain() -> None:
    pcm = tone(channels=6)
    pcm[0] = 0.0
    out = render_doubler(pcm, RATE, [DoublerVoice()], dry_gain_db=-120.0)
    assert out.shape == pcm.shape
    assert np.max(np.abs(out[1])) > 0.1
    assert np.max(np.abs(out[0])) < 1e-6
    np.testing.assert_allclose(out[2:], pcm[2:], atol=1e-6)
    mono = tone(channels=1)
    left = render_doubler(mono, RATE, [DoublerVoice(pan_pct=-100.0)], dry_gain_db=-120.0)
    right = render_doubler(mono, RATE, [DoublerVoice(pan_pct=100.0)], dry_gain_db=-120.0)
    np.testing.assert_array_equal(left, right)
    np.testing.assert_array_equal(render_doubler(mono, RATE, [DoublerVoice(enabled=False)]), mono)


def test_retune_changes_transition_and_cancel_stops_analysis() -> None:
    pcm = tone(channels=1)
    fast = render_pitch_correction(pcm, RATE, retune_ms=0.0)
    slow = render_pitch_correction(pcm, RATE, retune_ms=200.0)
    assert np.max(np.abs(fast - slow)) > 1e-3
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 3

    np.testing.assert_array_equal(render_pitch_correction(pcm, RATE, cancel_check=cancelled), pcm)
    assert calls <= 4


@pytest.mark.parametrize("processor,options", [
    ("pitch_correction", {"scale_notes": []}),
    ("pitch_correction", {"retune_ms": float("nan")}),
    ("doubler", {"voices": [{"interval_st": 24.0, "detune_cents": 100.0}]}),
    ("de_hum", {"hum_base_mode": "47"}),
    ("de_click", {"click_threshold_sigma": 0.0}),
])
def test_bad_processor_parameters_block_delivery(processor: str, options: dict) -> None:
    edit = new_audio_edit([make_step(processor, 0, 100, [0], **options)])
    assert validate_audio_edit(edit, clip_output_samples=100, channel_count=1)


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_pre_render_common_reference_clock_for_length_changes(rate: int) -> None:
    pcm = np.ones((2, rate), np.float32)
    edit = new_audio_edit([make_step("delete_range", 9600, 10560, [0, 1], output_length=47040)])
    out = render_audio_edit_full(pcm, edit, sample_rate=rate)
    assert out.shape == (2, rate - round(0.02 * rate))
    assert edit["steps"][0]["params"]["start"] == 9600
