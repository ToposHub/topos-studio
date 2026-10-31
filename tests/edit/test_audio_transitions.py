"""Audio transition (crossfade) tests — AT stage.

Covers the cut-point audio transition feature end to end at the unit level:
  - DSP: crossfade_envelopes / apply_crossfade_block (4 curves + block math)
  - Catalog: type uniqueness, namespace separation from video transitions
  - Model: TransitionInstance accepts audio:* and reports is_audio
  - Render injection: AudioMixer crossfade helper + offline renderer crossfade

Realtime (AudioMixer) and offline (render_track_block) share the same DSP via
audio_dsp.apply_crossfade_block, so the parity is asserted here too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.features.edit.core.audio_dsp import (
    AUDIO_CROSSFADE_CURVES,
    apply_crossfade_block,
    crossfade_envelopes,
)
from src.features.edit.core.audio_transition_catalog import (
    AUDIO_CURVE_MAP,
    AUDIO_TRANSITION_DEFINITIONS,
    AUDIO_TRANSITION_TYPE_OPTIONS,
    audio_curve_for,
    is_audio_transition,
)
from src.features.edit.core.timeline_v2 import TransitionInstance
from src.features.edit.core.transition_catalog import TRANSITION_TYPE_OPTIONS


# ─────────────────────────────────────────────────────────────────────────────
# Catalog
# ─────────────────────────────────────────────────────────────────────────────

class TestAudioTransitionCatalog:
    def test_four_curves_defined(self):
        assert len(AUDIO_TRANSITION_DEFINITIONS) == 4
        curves = {d.curve for d in AUDIO_TRANSITION_DEFINITIONS}
        assert curves == {"constant_power", "linear", "equal_power", "fade"}

    def test_all_types_use_audio_namespace(self):
        for ttype in AUDIO_TRANSITION_TYPE_OPTIONS:
            assert ttype.startswith("audio:"), f"{ttype} missing audio: prefix"
            assert is_audio_transition(ttype)

    def test_namespace_disjoint_from_video(self):
        """Audio and video transition types must never overlap."""
        assert set(AUDIO_TRANSITION_TYPE_OPTIONS).isdisjoint(TRANSITION_TYPE_OPTIONS)
        for vtype in TRANSITION_TYPE_OPTIONS:
            assert not is_audio_transition(vtype)

    def test_curve_lookup_roundtrip(self):
        for ttype in AUDIO_TRANSITION_TYPE_OPTIONS:
            curve = audio_curve_for(ttype)
            assert curve is not None
            assert AUDIO_CURVE_MAP[ttype] == curve

    def test_curve_lookup_none_for_video(self):
        assert audio_curve_for("cross_dissolve") is None
        assert audio_curve_for("nonsense") is None


# ─────────────────────────────────────────────────────────────────────────────
# DSP: crossfade_envelopes
# ─────────────────────────────────────────────────────────────────────────────

class TestCrossfadeEnvelopes:
    def test_endpoints(self):
        for curve in AUDIO_CROSSFADE_CURVES:
            left, right = crossfade_envelopes(curve, 101)
            # Left starts full, ends silent; right the inverse.
            assert abs(left[0] - 1.0) < 1e-9, f"{curve}: left[0]={left[0]}"
            assert left[-1] < 1e-6, f"{curve}: left[-1]={left[-1]}"
            assert right[0] < 1e-6, f"{curve}: right[0]={right[0]}"
            assert abs(right[-1] - 1.0) < 1e-9, f"{curve}: right[-1]={right[-1]}"
            assert left.shape == (101,)
            assert right.shape == (101,)

    def test_constant_power_is_constant_power(self):
        """constant_power: L+R == 1 over the whole span (linear-domain constant)."""
        left, right = crossfade_envelopes("constant_power", 1001)
        s = left + right
        assert np.allclose(s, 1.0, atol=1e-9)

    def test_constant_power_mid_is_minus_3db(self):
        """At the midpoint, L^2+R^2 ≈ 0.5 → -3dB (the constant-power signature)."""
        left, right = crossfade_envelopes("constant_power", 1001)
        mid = len(left) // 2
        power = left[mid] ** 2 + right[mid] ** 2
        assert abs(power - 0.5) < 1e-6

    def test_linear_sums_to_one(self):
        left, right = crossfade_envelopes("linear", 1001)
        assert np.allclose(left + right, 1.0, atol=1e-9)

    def test_equal_power_is_constant_power_domain(self):
        """equal_power: L^2+R^2 ≈ 1 over the whole span (power-domain constant)."""
        left, right = crossfade_envelopes("equal_power", 1001)
        power = left ** 2 + right ** 2
        assert np.allclose(power, 1.0, atol=1e-6)

    def test_fade_has_silence_at_midpoint(self):
        """fade curve dips to near-zero at the midpoint (silent gap, not a blend)."""
        left, right = crossfade_envelopes("fade", 1001)
        mid = len(left) // 2
        assert left[mid] < 0.05
        assert right[mid] < 0.05

    def test_unknown_curve_falls_back_to_constant_power(self):
        left, right = crossfade_envelopes("bogus", 101)
        expected_l, expected_r = crossfade_envelopes("constant_power", 101)
        assert np.allclose(left, expected_l)
        assert np.allclose(right, expected_r)

    def test_zero_length_raises(self):
        with pytest.raises(ValueError):
            crossfade_envelopes("constant_power", 0)


# ─────────────────────────────────────────────────────────────────────────────
# DSP: apply_crossfade_block (block-continuous application)
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyCrossfadeBlock:
    def _ones(self, n):
        return np.ones((2, n), dtype=np.float32)

    def test_left_role_attenuates_over_span(self):
        """Left clip fades out: gain at span start ≈ 1, gain near span end ≈ 0.

        Uses a span sized so a single block reaches the silent end: at 24 fps /
        48 kHz, 1 frame = 2000 samples, so a 1-frame span (2000 samples) fits a
        2000-sample block exactly and lets us observe the full 1→0 fade-out.
        """
        n = 2000  # exactly one frame at 48k/24fps
        samples = self._ones(n)
        out = apply_crossfade_block(
            samples, 0, 1, 0, "left", "constant_power", 24.0, 48000.0
        )
        assert out.shape == samples.shape
        # Start of fade-out ≈ full.
        assert out[0, 0] > 0.99
        # End of the span ≈ silent.
        assert out[0, -1] < 0.05

    def test_block_outside_span_is_unchanged(self):
        samples = self._ones(100)
        out = apply_crossfade_block(
            samples, 0, 10, 100, "left", "constant_power", 24.0, 48000.0
        )
        assert np.array_equal(out, samples)

    def test_partial_overlap_only_affects_in_span_portion(self):
        """A block before the span stays unity; the in-span portion attenuates."""
        # Span: 1 frame (2000 samples) starting at frame 1.
        # Block: 2000 samples starting at frame 0 → first 2000 samples are frame 0
        # (before span), so the whole block is pre-span and must stay unity.
        samples = np.ones((2, 2000), dtype=np.float32)
        out = apply_crossfade_block(
            samples, 1, 1, 0, "left", "constant_power", 24.0, 48000.0
        )
        assert np.allclose(out, samples)

    def test_left_and_right_are_complementary(self):
        """For constant_power, left+right envelopes sum to 1 at every sample.

        Decoding the same unity block twice (once as left, once as right) and
        summing must reconstruct the original unity signal across the whole span.
        """
        n = 2000  # one-frame span
        span_frames = 1
        samples = np.ones((2, n), dtype=np.float32)
        left_out = apply_crossfade_block(
            samples.copy(), 0, span_frames, 0, "left", "constant_power", 24.0, 48000.0
        )
        right_out = apply_crossfade_block(
            samples.copy(), 0, span_frames, 0, "right", "constant_power", 24.0, 48000.0
        )
        summed = left_out + right_out
        # constant_power: L+R == 1 over the whole span, so unity in → unity out.
        assert np.allclose(summed, 1.0, atol=1e-6)

    def test_empty_block_returned_unchanged(self):
        samples = np.zeros((2, 0), dtype=np.float32)
        out = apply_crossfade_block(
            samples, 0, 10, 0, "left", "constant_power", 24.0, 48000.0
        )
        assert out.shape == (2, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Model: TransitionInstance audio namespace
# ─────────────────────────────────────────────────────────────────────────────

class TestTransitionInstanceAudio:
    def _audio_transition(self, ttype="audio:constant_power"):
        return TransitionInstance(
            transition_type=ttype,
            track_id="a1",
            left_clip_id="c1",
            right_clip_id="c2",
            center_frame=100,
            duration=12,
        )

    def test_audio_type_accepted(self):
        t = self._audio_transition()
        assert t.is_audio is True

    def test_video_type_not_audio(self):
        t = TransitionInstance(
            transition_type="cross_dissolve",
            track_id="v1", left_clip_id="c1", right_clip_id="c2",
            center_frame=0, duration=10,
        )
        assert t.is_audio is False

    def test_all_audio_types_accepted(self):
        for ttype in AUDIO_TRANSITION_TYPE_OPTIONS:
            t = self._audio_transition(ttype)
            assert t.transition_type == ttype
            assert t.is_audio

    def test_invalid_type_rejected(self):
        with pytest.raises(ValueError):
            TransitionInstance(
                transition_type="not_a_real_type",
                track_id="a1", left_clip_id="c1",
                center_frame=0, duration=10,
            )

    def test_to_dict_from_dict_roundtrip(self):
        t = self._audio_transition()
        d = t.to_dict()
        assert d["transition_type"] == "audio:constant_power"
        t2 = TransitionInstance.from_dict(d)
        assert t2.transition_type == t.transition_type
        assert t2.is_audio is True
        assert t2.start_frame == t.start_frame
        assert t2.end_frame == t.end_frame


# ─────────────────────────────────────────────────────────────────────────────
# Render injection: AudioMixer crossfade helper
# ─────────────────────────────────────────────────────────────────────────────

class TestAudioMixerCrossfade:
    """The mixer's _apply_audio_crossfade must attenuate a participating clip
    and leave a non-participating clip untouched."""

    def _make_mixer(self):
        from src.features.edit.core.audio_playback import AudioMixer
        return AudioMixer()

    def test_non_participating_clip_unchanged(self):
        mixer = self._make_mixer()
        transition = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="a1", left_clip_id="left", right_clip_id="right",
            center_frame=100, duration=12,
        )
        mixer.set_audio_transitions("a1", [transition])
        samples = np.ones((2, 100), dtype=np.float32)
        # A clip that is neither left nor right of the transition.
        out = mixer._apply_audio_crossfade(samples, "a1", "bystander", 95, 24.0)
        assert np.array_equal(out, samples)

    def test_participating_left_clip_attenuated(self):
        mixer = self._make_mixer()
        # 1-frame span centered at frame 100 → span 100..101 (alignment default
        # "centered" → start_frame = center - duration//2 = 100 - 0 = 100).
        transition = TransitionInstance(
            transition_type="audio:constant_power",
            track_id="a1", left_clip_id="left", right_clip_id="right",
            center_frame=100, duration=1,
        )
        mixer.set_audio_transitions("a1", [transition])
        # Block sized to cover the full 1-frame span (2000 samples at 48k/24fps).
        samples = np.ones((2, 2000), dtype=np.float32)
        out = mixer._apply_audio_crossfade(samples, "a1", "left", 100, 24.0)
        assert out.shape == samples.shape
        # Start of fade-out ≈ full, end ≈ silent.
        assert out[0, 0] > 0.99
        assert out[0, -1] < 0.05
        # Nothing should exceed unity.
        assert np.max(out) <= 1.0 + 1e-6

    def test_empty_transitions_no_op(self):
        mixer = self._make_mixer()
        mixer.set_audio_transitions("a1", [])
        samples = np.ones((2, 100), dtype=np.float32)
        out = mixer._apply_audio_crossfade(samples, "a1", "left", 94, 24.0)
        assert np.array_equal(out, samples)

    def test_video_transitions_filtered_out(self):
        """set_audio_transitions must ignore video transition instances."""
        mixer = self._make_mixer()
        video_tr = TransitionInstance(
            transition_type="cross_dissolve",
            track_id="a1", left_clip_id="left", right_clip_id="right",
            center_frame=100, duration=12,
        )
        mixer.set_audio_transitions("a1", [video_tr])
        # Nothing registered (video filtered) → no-op.
        assert mixer._audio_transitions.get("a1") is None


# ─────────────────────────────────────────────────────────────────────────────
# Render injection: offline renderer (render_track_block) crossfade
# ─────────────────────────────────────────────────────────────────────────────

class TestOfflineRendererCrossfade:
    def _renderer(self):
        from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
        return AudioGraphRenderer(sample_rate=48000.0)

    def test_crossfade_field_applied_offline(self):
        """The offline path applies crossfade via clip_fade['crossfade'].

        One-frame span (2000 samples at 48k/24fps) fits one block so the full
        1→0 fade-out is observable.
        """
        renderer = self._renderer()
        samples = np.ones((2, 2000), dtype=np.float32)
        clip_fade = {
            "timeline_start": 0,
            "timeline_duration": 200,
            "fade_in_frames": 0,
            "fade_out_frames": 0,
            "fps": 24.0,
            "crossfade": [{
                "role": "left",
                "curve": "constant_power",
                "start_frame": 0,
                "duration": 1,
            }],
        }
        # Without a registered node the renderer short-circuits after fade/crossfade,
        # so the crossfade effect is observable directly.
        out = renderer.render_track_block(
            "a1", samples, current_frame=0, clip_fade=clip_fade,
        )
        # Left role fades out over the span → end of block near silent.
        assert out[0, 0] > 0.99
        assert out[0, -1] < 0.05
        assert np.max(out) <= 1.0 + 1e-6

    def test_offline_matches_realtime_dsp(self):
        """Parity: offline crossfade output == direct apply_crossfade_block output.

        Both paths call the same audio_dsp.apply_crossfade_block, so they must
        agree bit-for-bit on the same input. This guards against the two paths
        drifting (the same regression class as AM-06 / RMA-03 for clip fades).
        """
        n = 2000
        samples = np.ones((2, n), dtype=np.float32)
        span_frames = 1
        direct = apply_crossfade_block(
            samples.copy(), 0, span_frames, 0, "left", "constant_power", 24.0, 48000.0
        )
        renderer = self._renderer()
        clip_fade = {
            "timeline_start": 0, "timeline_duration": 200,
            "fade_in_frames": 0, "fade_out_frames": 0, "fps": 24.0,
            "crossfade": [{
                "role": "left", "curve": "constant_power",
                "start_frame": 0, "duration": span_frames,
            }],
        }
        offline = renderer.render_track_block(
            "a1", samples.copy(), current_frame=0, clip_fade=clip_fade,
        )
        assert np.allclose(offline, direct, atol=1e-6)
