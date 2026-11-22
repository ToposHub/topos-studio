"""B05: composition audio plan — per-layer PCM segments and envelopes.

Pure contract tests: shared sample endpoints (no duration drift), mute/solo
priority, cross-rate segment math against the B03 frame contract, waveform
bucketing and the per-layer audio field round-trip.
"""

import sys
import os
from fractions import Fraction

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pytest

from src.features.composite.core.comp_audio import (
    AudioPlan, AudioSegment, AudioStreamInfo, LayerAudio, LayerAudioTiming,
    audible_layers, build_audio_plan, db_to_linear, plan_epoch,
    sample_boundary, waveform_cache_key, waveform_envelope,
)
from src.features.composite.core.layer_model import Layer

pytestmark = pytest.mark.composite_core


def test_sample_boundary_ntsc_matches_plan_example():
    # Verbatim from the implementation plan: adjacent windows share the
    # boundary sample; cumulative duration never drifts.
    start = sample_boundary(100, Fraction(30000, 1001), 48000)
    stop = sample_boundary(101, Fraction(30000, 1001), 48000)
    assert (start, stop) == (160160, 161761)


def test_sample_boundary_shared_endpoints_do_not_drift():
    fps = Fraction(30000, 1001)
    rate = 48000
    # Exact floor value for 1001 NTSC frames at 48 kHz.
    assert sample_boundary(1001, fps, rate) - sample_boundary(0, fps, rate) == 1603201
    # Whole seconds stay sample-exact: 24 fps at 48 kHz = 2000 samples.
    assert sample_boundary(1, Fraction(24), rate) == 2000
    # Per-frame windows partition the timeline without gaps or drift.
    boundaries = [sample_boundary(f, fps, rate) for f in range(1200)]
    assert all(a <= b for a, b in zip(boundaries, boundaries[1:]))
    assert boundaries[0] == 0


def test_mute_solo_priority():
    a = LayerAudio(asset_id="A", stream_index=0)
    muted_solo = LayerAudio(asset_id="B", stream_index=0, mute=True, solo=True)
    solo = LayerAudio(asset_id="C", stream_index=0, solo=True)
    plain = LayerAudio(asset_id="D", stream_index=0)

    # Mute wins over the layer's own solo.
    assert audible_layers([("a", a), ("ms", muted_solo)]) == [("a", a)]
    # Any solo silences non-solo layers.
    assert audible_layers([("a", a), ("s", solo), ("p", plain)]) == [("s", solo)]
    # No solo → all unmuted layers audible.
    assert audible_layers([("a", a), ("p", plain)]) == [("a", a), ("p", plain)]


def test_db_to_linear_and_silence():
    assert db_to_linear(0.0) == pytest.approx(1.0)
    assert db_to_linear(-6.0206) == pytest.approx(0.5, rel=1e-3)
    assert db_to_linear(-200.0) == 0.0
    assert db_to_linear("bad") == 1.0


def _streams(*entries):
    return {
        (asset, index): AudioStreamInfo(
            asset_id=asset, stream_index=index, sample_rate=48000,
            channels=2, time_base=Fraction(1, 48000), start_pts=0)
        for asset, index in entries
    }


def test_build_audio_plan_segments_share_boundaries():
    fps = Fraction(30000, 1001)
    streams = _streams(("clipA", 0), ("clipB", 1))
    timings = [
        # Two layers over the same material at different times (B03 §4).
        LayerAudioTiming("l1", LayerAudio("clipA", 0), 0, 100,
                         source_origin_samples=0),
        LayerAudioTiming("l2", LayerAudio("clipB", 1), 50, 150,
                         source_origin_samples=48000),
    ]
    plan = build_audio_plan(timings, fps=fps, sample_rate=48000,
                            streams=streams)
    assert isinstance(plan, AudioPlan)
    l1, l2 = plan.segments
    assert (l1.in_frame, l1.out_frame) == (0, 100)
    assert (l2.in_frame, l2.out_frame) == (50, 150)
    # Shared-endpoint windows: each segment covers the exact comp duration.
    assert l1.sample_count == sample_boundary(101, fps, 48000)
    assert l2.source_sample_start == 48000
    assert l2.sample_count == sample_boundary(151, fps, 48000) - sample_boundary(50, fps, 48000)
    assert plan.total_samples == sample_boundary(151, fps, 48000)
    # Frame lookup during playback.
    assert set(plan.segments_at_frame(75)) == {l1, l2}
    assert plan.segments_at_frame(0) == (l1,)
    assert plan.segments_at_frame(150) == (l2,)


def test_audio_plan_accepts_iterables_and_uses_native_stream_origin_and_length():
    timing = LayerAudioTiming("l1", LayerAudio("clip"), 10, 11, source_origin_samples=17)
    stream = AudioStreamInfo("clip", 0, 44100, 2)
    plan = build_audio_plan(iter([timing]), fps=24, sample_rate=48000, streams={("clip", 0): stream})
    segment, = plan.segments
    assert segment.source_sample_start == 17
    assert segment.source_sample_end == 17 + 3675
    assert segment.sample_rate == 44100
    assert plan.total_samples == 24000


def test_build_audio_plan_resolves_gain_and_missing_streams():
    streams = _streams(("quiet", 0))
    timings = [
        LayerAudioTiming("l1", LayerAudio("quiet", 0, gain_db=-6.0), 0, 10),
        LayerAudioTiming("l2", LayerAudio("offline", 0), 5, 15),
    ]
    plan = build_audio_plan(timings, fps=Fraction(24), sample_rate=48000,
                            streams=streams)
    assert len(plan.segments) == 1
    assert plan.segments[0].gain_linear == pytest.approx(0.501187, rel=1e-3)
    assert plan.missing_streams == ("offline#0",)


def test_layer_audio_field_round_trips_and_defaults():
    layer = Layer(layer_id="l1", name="A", root_node_id="r")
    assert layer.audio is None
    old = layer.to_dict()
    assert "audio" in old and old["audio"] is None
    # Old projects without the key load with audio=None.
    legacy = {k: v for k, v in old.items() if k != "audio"}
    assert Layer.from_dict(legacy).audio is None

    layer.audio = LayerAudio(asset_id="asset-7", stream_index=1,
                             gain_db=-3.0, solo=True)
    restored = Layer.from_dict(layer.to_dict())
    assert restored.audio == layer.audio
    # Malformed audio blocks degrade to None instead of breaking load.
    broken = dict(old, audio={"gain_db": "x"})
    assert Layer.from_dict(broken).audio is None


def test_waveform_envelope_buckets_min_max():
    pcm = np.linspace(-1.0, 1.0, 100, dtype=np.float32)
    mins, maxs = waveform_envelope(pcm, 10)
    assert len(mins) == len(maxs) == 10
    assert mins[0] == pytest.approx(-1.0)
    assert maxs[-1] == pytest.approx(1.0)
    # Trailing partial bucket dropped: 105 samples / 10 → 10 buckets.
    mins, maxs = waveform_envelope(np.ones(105, dtype=np.float32), 10)
    assert len(mins) == 10
    # Each stereo bucket spans the same time interval as its mono counterpart.
    stereo = np.stack([pcm, -pcm], axis=1)
    mins, maxs = waveform_envelope(stereo, 10)
    assert len(mins) == len(maxs) == 10
    assert mins[0] == pytest.approx(-1.0)
    assert maxs[0] == pytest.approx(1.0)
    assert waveform_cache_key("rev1", 0, 0, 48000, 10) != \
        waveform_cache_key("rev2", 0, 0, 48000, 10)


def test_plan_epoch_changes_with_content():
    streams = _streams(("a", 0))
    base = [LayerAudioTiming("l1", LayerAudio("a", 0), 0, 10)]
    plan_a = build_audio_plan(base, fps=Fraction(24), sample_rate=48000,
                              streams=streams)
    moved = [LayerAudioTiming("l1", LayerAudio("a", 0), 5, 15)]
    plan_b = build_audio_plan(moved, fps=Fraction(24), sample_rate=48000,
                              streams=streams)
    assert plan_epoch(plan_a) != plan_epoch(plan_b)
    assert plan_epoch(plan_a) == plan_epoch(build_audio_plan(
        base, fps=Fraction(24), sample_rate=48000, streams=streams))


def test_plan_epoch_tracks_stream_timestamps():
    timing = [LayerAudioTiming("layer", LayerAudio("asset"), 0, 10)]
    streams = {
        ("asset", 0): AudioStreamInfo("asset", 0, 48000, 2, Fraction(1, 48000), start_pts=0)
    }
    start = build_audio_plan(timing, fps=24, sample_rate=48000, streams=streams)
    streams[("asset", 0)] = AudioStreamInfo("asset", 0, 48000, 2, Fraction(1, 48000), start_pts=1024)
    delayed = build_audio_plan(timing, fps=24, sample_rate=48000, streams=streams)
    assert plan_epoch(start) != plan_epoch(delayed)
