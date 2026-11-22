"""B06: composition audio mixdown — exact PCM lengths and pulse placement.

Mixdown consumes the B05 AudioPlan: export ranges map to exact PCM
lengths (shared sample endpoints), segments land at analytically predicted
sample offsets, two layers sum linearly, and export states distinguish
silence / missing media / no-stream / error.
"""

import sys
import os
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest

from src.features.composite.core.comp_audio import (
    AudioStreamInfo, LayerAudio, LayerAudioTiming, build_audio_plan, sample_boundary,
)
from src.features.composite.core.comp_audio_mix import (
    AUDIO_STATE_MISSING_MEDIA, AUDIO_STATE_NO_STREAM, AUDIO_STATE_OK,
    AUDIO_STATE_SILENT, AudioExportRange, classify_audio_state, mixdown,
    mix_segment,
)
from src.features.composite.core.comp_audio import AudioSegment

pytestmark = pytest.mark.composite_core

FPS = Fraction(24)
RATE = 48000


def _streams(*entries):
    return {
        (asset, index): AudioStreamInfo(
            asset_id=asset, stream_index=index, sample_rate=RATE,
            channels=2, time_base=Fraction(1, RATE), start_pts=0)
        for asset, index in entries
    }


def _segment(layer_id="l1", asset="a", in_frame=0, out_frame=23,
             origin=0, gain=1.0):
    start = in_frame * RATE // FPS.numerator if FPS == Fraction(24) else 0
    # 24 fps @ 48 kHz → exactly 2000 samples per frame.
    return AudioSegment(
        layer_id=layer_id, asset_id=asset, stream_index=0,
        in_frame=in_frame, out_frame=out_frame,
        source_sample_start=origin + in_frame * 2000,
        source_sample_end=origin + (out_frame + 1) * 2000,
        sample_rate=RATE, channels=2, gain_linear=gain)


def test_export_range_pcm_length_is_exact():
    # 24 fps @ 48 kHz: 2 000 samples per frame, no rounding.
    assert AudioExportRange(0, 23).pcm_length(FPS, RATE) == 48_000
    # NTSC: 1001 frames → the shared-endpoint window, not per-frame sums.
    ntsc = Fraction(30000, 1001)
    assert AudioExportRange(0, 1000).pcm_length(ntsc, RATE) == 1_603_201
    # Adjacent ranges share the boundary sample.
    first = AudioExportRange(0, 23)
    second = AudioExportRange(24, 47)
    assert first.sample_window(FPS, RATE)[1] == second.sample_window(FPS, RATE)[0]


def test_pulse_lands_at_predicted_sample_index():
    segment = _segment()
    buffer = np.zeros((48_000, 2), dtype=np.float32)
    pcm = np.zeros(48_000, dtype=np.float32)
    pulse_source_sample = segment.source_sample_start + 7_500  # frame 3.75
    pcm[pulse_source_sample - segment.source_sample_start] = 1.0
    mix_segment(buffer, segment, pcm, 0, FPS, RATE)
    assert np.count_nonzero(buffer[:, 0]) == 1
    assert buffer[7_500, 0] == pytest.approx(1.0)
    assert buffer[7_500, 1] == pytest.approx(1.0)  # mono upmixed


def test_two_segments_sum_linearly_with_gain():
    plan_specs = [
        LayerAudioTiming("l1", LayerAudio("a", 0, gain_db=0.0), 0, 23),
        LayerAudioTiming("l2", LayerAudio("b", 0, gain_db=-6.0206), 12, 35),
    ]
    plan = build_audio_plan(plan_specs, fps=FPS, sample_rate=RATE,
                            streams=_streams(("a", 0), ("b", 0)),
                            stream_key=None)
    # Distinct assets so both layers stay audible.
    decoded = {}
    for segment in plan.segments:
        pcm = np.zeros(segment.sample_count, dtype=np.float32)
        pcm[:] = 0.25
        decoded[f"{segment.layer_id}:{segment.asset_id}#{segment.stream_index}"] = pcm
    buffer, state = mixdown(plan, decoded, AudioExportRange(0, 47), fps=FPS)
    assert state == AUDIO_STATE_OK
    # Frames 0-11: only l1 at 0.25; frames 12-23: l1 + l2(0.5×0.25);
    # frames 24-35: only l2; frames 36-47: silence.
    assert buffer[100, 0] == pytest.approx(0.25)
    assert buffer[25 * 2000, 0] == pytest.approx(0.25 * 0.5)
    assert buffer[30 * 2000, 0] == pytest.approx(0.25 * 0.5)
    assert buffer[40 * 2000, 0] == pytest.approx(0.0)


def test_export_range_offsets_segment_placement():
    # Exporting frames 12..35 places the segment's frame-12 content at 0.
    segment = _segment(in_frame=12, out_frame=35)
    buffer = np.zeros((48_000, 2), dtype=np.float32)
    pcm = np.zeros(segment.sample_count, dtype=np.float32)
    pcm[0] = 1.0
    mix_segment(buffer, segment, pcm, 12, FPS, RATE)
    assert buffer[0, 0] == pytest.approx(1.0)


def test_export_start_inside_the_layer_offsets_the_decoded_pcm():
    segment = _segment(in_frame=0, out_frame=2)
    pcm = np.zeros(segment.sample_count, dtype=np.float32)
    pcm[100] = 0.5
    pcm[2100] = 1.0
    buffer = np.zeros((2000, 2), dtype=np.float32)
    mix_segment(buffer, segment, pcm, 1, FPS, RATE)
    assert np.flatnonzero(buffer[:, 0]).tolist() == [100]
    assert buffer[100, 0] == 1.0


def test_mix_uses_output_rate_and_subtracts_absolute_ntsc_boundaries():
    ntsc = Fraction(30_000, 1001)
    timing = LayerAudioTiming("l1", LayerAudio("a"), 2, 3)
    stream = AudioStreamInfo("a", 0, 44_100, 2)
    plan = build_audio_plan([timing], fps=ntsc, sample_rate=RATE, streams={("a", 0): stream})
    expected_start = sample_boundary(2, ntsc, RATE) - sample_boundary(1, ntsc, RATE)
    expected_end = sample_boundary(4, ntsc, RATE) - sample_boundary(1, ntsc, RATE)
    pcm = np.zeros(expected_end - expected_start, dtype=np.float32)
    pcm[0] = pcm[-1] = 1.0
    mix, state = mixdown(plan, {"l1:a#0": pcm}, AudioExportRange(1, 5), fps=ntsc)
    assert state == AUDIO_STATE_OK
    assert expected_start == 1602
    assert np.flatnonzero(mix[:, 0]).tolist() == [expected_start, expected_end - 1]


def test_out_of_range_segment_needs_no_decode_and_mono_mix_is_supported():
    timing = LayerAudioTiming("l1", LayerAudio("a"), 0, 0)
    plan = build_audio_plan([timing], fps=FPS, sample_rate=RATE, streams=_streams(("a", 0)))
    mix, state = mixdown(plan, {}, AudioExportRange(1, 2), fps=FPS)
    assert state == AUDIO_STATE_SILENT
    assert not mix.any()
    pcm = np.full((2000, 2), (0.25, 0.75), dtype=np.float32)
    mix, state = mixdown(plan, {"l1:a#0": pcm}, AudioExportRange(0, 0), fps=FPS, channels=1)
    assert state == AUDIO_STATE_OK
    assert mix.shape == (2000, 1)
    np.testing.assert_array_equal(mix[:, 0], 0.5)


def test_mixdown_states():
    plan = build_audio_plan(
        [LayerAudioTiming("l1", LayerAudio("a", 0), 0, 10)],
        fps=FPS, sample_rate=RATE, streams=_streams(("a", 0)))
    # Missing decode for a planned segment.
    buffer, state = mixdown(plan, {}, AudioExportRange(0, 10), fps=FPS)
    assert state == AUDIO_STATE_MISSING_MEDIA
    assert not buffer.any()

    # Empty plan → deliberate silence.
    empty = build_audio_plan([], fps=FPS, sample_rate=RATE, streams={})
    _, state = mixdown(empty, {}, AudioExportRange(0, 10), fps=FPS)
    assert state == AUDIO_STATE_SILENT

    assert classify_audio_state(None) == AUDIO_STATE_SILENT
    assert classify_audio_state(plan, decode_errors=("boom",)) == "error"
    assert classify_audio_state(
        None, missing_media=("clip",)) == AUDIO_STATE_MISSING_MEDIA
    assert classify_audio_state(
        None, containers_without_streams=("clip",)) == AUDIO_STATE_NO_STREAM


def test_offline_stream_plan_reports_no_stream_state():
    plan = build_audio_plan(
        [LayerAudioTiming("l1", LayerAudio("offline", 0), 0, 10)],
        fps=FPS, sample_rate=RATE, streams={})
    assert plan.missing_streams
    # No segments were planned, so nothing can be "missing at decode";
    # the no-stream diagnosis from the plan carries through.
    _, state = mixdown(plan, {}, AudioExportRange(0, 10), fps=FPS)
    assert state == AUDIO_STATE_NO_STREAM
    assert classify_audio_state(plan) == AUDIO_STATE_NO_STREAM
