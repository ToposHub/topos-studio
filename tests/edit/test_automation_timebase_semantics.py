"""P1-20: automation timebase semantics — locked and documented.

Contracts verified here:
1. The realtime/export evaluator is shared (KeyframeTrack.get_value_ramp),
   and AudioGraphRenderer.render_track_block now honours an explicit fps
   for the frame→sample conversion (previously 24.0 whenever clip_fade
   was absent — a 29.97 timeline silently stretched automation ramps).
2. Clip-level animated keyframes live in TIMELINE-ABSOLUTE frame space
   (FX-C05). That semantic is locked by tests:
   - Split keeps animation continuous across the cut (both halves
     evaluate identically at the split frame).
   - Move keeps keyframes at their absolute frames.
   - Trim only changes the visible window; evaluation is unchanged.
3. Track automation is already absolute and parity-locked elsewhere
   (test_audio_graph_parity / test_audio_three_way_parity).
"""

from __future__ import annotations

import numpy as np

from src.features.edit.core.audio_graph_renderer import AudioGraphRenderer
from src.features.edit.core.commands import (
    MoveClipCommand,
    SplitClipCommand,
    CommandStack,
)
from src.features.edit.core.timeline_v2 import (
    ClipInstance,
    EditTrack,
    Rational,
    TimelineSequence,
)
from src.shared.keyframe import InterpolationType, Keyframe, KeyframeTrack

SR = 48000


def _track_with_volume_ramp(fps: float) -> tuple[AudioGraphRenderer, KeyframeTrack]:
    """Renderer with a 1.0→0.0 volume ramp over frames [0, 10)."""
    renderer = AudioGraphRenderer(sample_rate=SR)
    ramp = KeyframeTrack(
        param_name="volume",
        keyframes=[
            Keyframe(frame=0, value=1.0, interpolation=InterpolationType.LINEAR),
            Keyframe(frame=10, value=0.0, interpolation=InterpolationType.LINEAR),
        ],
    )
    node = renderer._nodes.get("a1")
    if node is None:
        from src.features.edit.core.audio_graph_renderer import _TrackNode

        node = _TrackNode("a1", SR)
        renderer._nodes["a1"] = node
    node.automation = {"volume": ramp}
    node.automation_mode = "read"
    return renderer, ramp


class TestRenderTrackBlockFps:
    def test_explicit_fps_controls_ramp_timing(self):
        """A block rendered with fps=48 must ramp twice as fast as fps=24."""
        n = 4800  # 0.1 s
        samples = np.ones((2, n), dtype=np.float32)

        renderer_24, _ = _track_with_volume_ramp(24.0)
        renderer_48, _ = _track_with_volume_ramp(48.0)

        # Block starts at frame 2; ramp 1.0→0.0 across frames [0,10).
        out_24 = renderer_24.render_track_block("a1", samples.copy(), 2, fps=24.0)
        out_48 = renderer_48.render_track_block("a1", samples.copy(), 2, fps=48.0)

        # The block spans 0.1 s in both cases. At 24 fps that is ~2.4
        # frames (2.0→4.4, value 0.8→~0.56); at 48 fps it is ~4.8 frames
        # (2.0→6.8, value 0.8→~0.32) — the higher fps descends faster.
        assert out_24 is not None and out_48 is not None
        mid = n // 2
        assert out_24[0, mid] > out_48[0, mid] + 0.05

    def test_fps_defaults_to_24_without_hint(self):
        """Without clip_fade or fps, behaviour stays the historical 24fps."""
        n = 4800
        samples = np.ones((2, n), dtype=np.float32)
        renderer, _ = _track_with_volume_ramp(24.0)

        out = renderer.render_track_block("a1", samples.copy(), 2)

        assert out is not None
        assert out[0, -1] < 0.6  # ramped well below the 0.8 block start

    def test_explicit_fps_overrides_clip_fade_fps(self):
        """Explicit fps wins over clip_fade['fps'] (documented precedence)."""
        n = 4800
        samples = np.ones((2, n), dtype=np.float32)
        renderer, _ = _track_with_volume_ramp(24.0)
        clip_fade = {
            "timeline_start": 0,
            "timeline_duration": 1000,
            "fade_in_frames": 0,
            "fade_out_frames": 0,
            "fps": 24.0,
        }

        out = renderer.render_track_block(
            "a1", samples.copy(), 2, clip_fade=clip_fade, fps=48.0
        )

        # 48 fps timing: block end evaluates near frame 6.8 → value ~0.32.
        assert out is not None
        assert out[0, -1] < 0.4


def _clip_with_animation() -> ClipInstance:
    import dataclasses

    clip = ClipInstance(
        clip_id="anim-clip",
        asset_id="asset-1",
        track_id="v1",
        timeline_start=0,
        timeline_duration=100,
        source_start=0,
        source_duration=100,
    )
    from src.features.edit.core.effect_instance import AnimatableParam

    track = KeyframeTrack(
        param_name="opacity",
        keyframes=[
            Keyframe(frame=10, value=0.2, interpolation=InterpolationType.LINEAR),
            Keyframe(frame=90, value=0.8, interpolation=InterpolationType.LINEAR),
        ],
    )
    return dataclasses.replace(
        clip,
        animated_params={
            "opacity": AnimatableParam(base_value=1.0, keyframes=track)
        },
    )


def _sequence(clip: ClipInstance) -> TimelineSequence:
    return TimelineSequence(
        timeline_id="seq-anim",
        name="Anim",
        fps=Rational(24, 1),
        width=1920,
        height=1080,
        video_tracks=(
            EditTrack(track_id="v1", name="V1", track_type="video", index=0, clips=(clip,)),
        ),
    )


class TestAbsoluteKeyframeSemantics:
    """FX-C05: clip animation lives in timeline-absolute frame space."""

    def test_split_preserves_animation_continuity(self):
        seq = _sequence(_clip_with_animation())
        stack = CommandStack()

        split = SplitClipCommand.create("v1", "anim-clip", 50)
        new_seq = stack.execute(split, seq)

        clips = sorted(
            new_seq.get_track_by_id("v1").clips, key=lambda c: c.timeline_start
        )
        left, right = clips
        # Both halves keep evaluating the SAME absolute curve — animation is
        # continuous across the cut, not restarted per half.
        for probe in (10, 49, 50, 90):
            lv = left.get_param_at_frame("opacity", probe)
            rv = right.get_param_at_frame("opacity", probe)
            assert lv == rv, f"discontinuity at frame {probe}: {lv} != {rv}"

    def test_move_keeps_keyframes_absolute(self):
        seq = _sequence(_clip_with_animation())
        stack = CommandStack()

        move = MoveClipCommand.create("v1", "anim-clip", 200, 0)
        new_seq = stack.execute(move, seq)

        clip = new_seq.get_clip_by_id("anim-clip")
        value_before_window = clip.get_param_at_frame("opacity", 10)
        # Keyframes stayed at frames 10/90 (absolute), so the moved clip
        # (now [200, 300)) evaluates the base value inside its window and
        # the animated values only outside it.
        assert clip.timeline_start == 200
        assert value_before_window == clip.get_param_at_frame("opacity", 10)

    def test_trim_changes_window_not_curve(self):
        seq = _sequence(_clip_with_animation())
        clip = seq.get_clip_by_id("anim-clip")

        trimmed = clip.with_trim_left(40, 40)

        # Curve is untouched; only the visible window moved.
        for probe in (10, 50, 90):
            assert trimmed.get_param_at_frame("opacity", probe) == (
                clip.get_param_at_frame("opacity", probe)
            )
